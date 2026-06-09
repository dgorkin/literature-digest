"""PubMed fetch via NCBI E-utilities (spec §6).

Flow: esearch.fcgi (PMIDs for a date window) -> efetch.fcgi (full records as XML)
-> normalized dicts shared with the rest of the pipeline.

Correctness choices that matter:
- datetype=edat (Entrez date = when it ENTERED PubMed), not pdat (publication
  date). PubMed back-dates pdat, so pdat-windowing silently drops late-indexed
  papers; edat is monotonic in indexing time. See spec §6.
- Rate limiting: ~3 req/s without an API key, ~10/s with one. We throttle to the
  applicable ceiling and back off on HTTP 429/5xx.

Parameters were chosen from the E-utilities docs (NBK25499/NBK25501); verify
there before changing them.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH_PAGE = 200   # PMIDs per esearch page
_EFETCH_CHUNK = 200   # records per efetch call


class PubMedClient:
    def __init__(self, api_key=None, tool="literature-digest", email=None, timeout=30):
        self.api_key = api_key
        self.tool = tool
        self.email = email
        self.timeout = timeout
        self.session = requests.Session()
        # 10/s with a key, 3/s without; add a small margin.
        self._min_interval = 0.105 if api_key else 0.34
        self._last = 0.0

    # -- low-level HTTP -----------------------------------------------------
    def _common_params(self) -> dict:
        p = {"tool": self.tool}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    def _request(self, method: str, path: str, params=None, data=None, retries=4):
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.request(
                    method, f"{EUTILS}/{path}", params=params, data=data,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                wait = 2 ** attempt
                log.warning("PubMed %s network error (%s); retry in %ss", path, exc, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("PubMed %s HTTP %s; backoff %ss", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"PubMed request failed after {retries} attempts: {path}")

    # -- esearch ------------------------------------------------------------
    def esearch(self, term: str, mindate: date, maxdate: date, max_results: int) -> list[str]:
        """Return PMIDs entering PubMed within [mindate, maxdate], capped at
        max_results. If the result set exceeds the cap it is truncated and logged
        (never silently — spec §5)."""
        pmids: list[str] = []
        retstart = 0
        total = None
        empty_retries = 0
        while len(pmids) < max_results:
            page = min(_ESEARCH_PAGE, max_results - len(pmids))
            params = {
                **self._common_params(),
                "db": "pubmed",
                "term": term,
                "datetype": "edat",
                "mindate": mindate.strftime("%Y/%m/%d"),
                "maxdate": maxdate.strftime("%Y/%m/%d"),
                "retmode": "json",
                "retstart": retstart,
                "retmax": page,
            }
            resp = self._request("GET", "esearch.fcgi", params=params)
            result = resp.json().get("esearchresult", {})
            if total is None:
                total = int(result.get("count", 0))
                log.info("esearch matched %d records in window", total)
            idlist = result.get("idlist", [])
            if not idlist:
                # NCBI occasionally returns HTTP 200 + an empty page under load.
                # Only treat empty as "done" once we've collected what we expect;
                # otherwise it's a transient — retry the same page before giving up
                # (silent truncation is forbidden, spec §5).
                expected = min(total, max_results)
                if retstart >= expected or empty_retries >= 3:
                    break
                empty_retries += 1
                wait = 2 ** empty_retries
                log.warning("esearch empty page at retstart=%d of %d (transient?); "
                            "retry %d/3 in %ss", retstart, total, empty_retries, wait)
                time.sleep(wait)
                continue
            empty_retries = 0
            pmids.extend(idlist)
            retstart += len(idlist)
            if retstart >= total:
                break
        if total is not None and total > max_results:
            log.warning(
                "esearch returned %d but max_candidates_per_run=%d; truncating to %d. "
                "Raise the cap or narrow the window if this recurs.",
                total, max_results, max_results,
            )
        elif total is not None and len(pmids) < total:
            log.warning(
                "esearch collected only %d of %d in-window records (transient empty "
                "page). The shortfall self-heals next run via the window+dedup ledger.",
                len(pmids), total,
            )
        return pmids[:max_results]

    # -- efetch -------------------------------------------------------------
    def fetch_records(self, pmids: list[str]) -> list[dict]:
        """Fetch and normalize full records for the given PMIDs (chunked)."""
        records: list[dict] = []
        for i in range(0, len(pmids), _EFETCH_CHUNK):
            chunk = pmids[i:i + _EFETCH_CHUNK]
            params = {**self._common_params(), "db": "pubmed", "retmode": "xml"}
            # POST the id list so large batches don't blow the URL length limit.
            resp = self._request("POST", "efetch.fcgi", params=params,
                                  data={"id": ",".join(chunk)})
            records.extend(parse_pubmed_xml(resp.text))
        return records


# -- XML normalization ------------------------------------------------------
def _text(el) -> str | None:
    if el is None:
        return None
    return "".join(el.itertext()).strip() or None


def _pub_date(article) -> str | None:
    pd = article.find(".//Journal/JournalIssue/PubDate")
    if pd is None:
        return None
    medline = pd.findtext("MedlineDate")
    if medline:
        return medline.strip()
    parts = [pd.findtext(tag) for tag in ("Year", "Month", "Day")]
    parts = [p for p in parts if p]
    return "-".join(parts) if parts else None


def _entrez_date(pubmed_data) -> str | None:
    """The Entrez (edat) date from the article history; the date we windowed on."""
    if pubmed_data is None:
        return None
    hist = pubmed_data.find("History")
    if hist is None:
        return None
    by_status = {pd.get("PubStatus"): pd for pd in hist.findall("PubMedPubDate")}
    chosen = by_status.get("entrez") or by_status.get("pubmed")
    if chosen is None:
        return None
    parts = [chosen.findtext(tag) for tag in ("Year", "Month", "Day")]
    parts = [p for p in parts if p]
    return "-".join(parts) if parts else None


def _authors(article) -> str | None:
    names = []
    for au in article.findall(".//AuthorList/Author"):
        last = au.findtext("LastName")
        if last:
            initials = au.findtext("Initials") or ""
            names.append(f"{last} {initials}".strip())
        else:
            coll = au.findtext("CollectiveName")
            if coll:
                names.append(coll.strip())
    return ", ".join(names) if names else None


def _abstract(article) -> str | None:
    nodes = article.findall(".//Abstract/AbstractText")
    if not nodes:
        return None
    parts = []
    for n in nodes:
        label = n.get("Label")
        body = _text(n)
        if not body:
            continue
        parts.append(f"{label}: {body}" if label else body)
    return "\n".join(parts) if parts else None


def parse_pubmed_xml(xml_text: str) -> list[dict]:
    """Parse a PubmedArticleSet into normalized records (spec §6)."""
    root = ET.fromstring(xml_text)
    out: list[dict] = []
    for art in root.findall(".//PubmedArticle"):
        medline = art.find("MedlineCitation")
        pubmed_data = art.find("PubmedData")
        if medline is None:
            continue
        article = medline.find("Article")
        pmid = medline.findtext("PMID")

        doi = None
        if pubmed_data is not None:
            for aid in pubmed_data.findall(".//ArticleIdList/ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = (aid.text or "").strip().lower() or None
                    break
        if doi is None and article is not None:
            for elid in article.findall(".//ELocationID"):
                if elid.get("EIdType") == "doi":
                    doi = (elid.text or "").strip().lower() or None
                    break

        pub_types = {
            (pt.text or "").lower()
            for pt in art.findall(".//PublicationTypeList/PublicationType")
        }
        is_preprint = "preprint" in pub_types

        out.append({
            "source": "pubmed",
            "source_id": pmid,
            "doi": doi,
            "pmid": pmid,
            "title": _text(article.find("ArticleTitle")) if article is not None else None,
            "authors": _authors(article) if article is not None else None,
            "journal": (
                article.findtext(".//Journal/ISOAbbreviation")
                or article.findtext(".//Journal/Title")
                if article is not None else None
            ),
            "pub_date": _pub_date(article) if article is not None else None,
            "source_date": _entrez_date(pubmed_data),
            "abstract": _abstract(article) if article is not None else None,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
            "is_preprint": is_preprint,
        })
    return out

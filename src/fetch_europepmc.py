"""Europe PMC fetch via the REST search API (spec §6, Phase 4).

Second feed into the same normalized pipeline. Rationale: PubMed's preprint
coverage is partial and lagged, and in epigenomics a lot of relevant work hits
bioRxiv/medRxiv first. Europe PMC indexes preprints (source `SRC:PPR`) with a
clean JSON REST API and no API key.

We fetch PREPRINTS only here — published papers already arrive via PubMed, and a
preprint's later published version is re-surfaced by the normalized-title match
in store.find_sent_preprint, not by re-fetching it from Europe PMC.

Flow: GET /search (query + CREATION_DATE window, paged via cursorMark) ->
normalized dicts identical in shape to the PubMed records.

Field names/date syntax were verified against a live probe; confirm against
https://europepmc.org/RestfulWebService before changing them.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_PAGE_SIZE = 1000  # max Europe PMC allows per page


class EuropePMCClient:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.session = requests.Session()
        self._min_interval = 0.2  # polite; EPMC has no key but asks for restraint
        self._last = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    def _request(self, params: dict, retries=4):
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(SEARCH_URL, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                wait = 2 ** attempt
                log.warning("EuropePMC network error (%s); retry in %ss", exc, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("EuropePMC HTTP %s; backoff %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("EuropePMC request failed after retries")

    def search(self, query: str, mindate: date, maxdate: date,
               max_results: int) -> list[dict]:
        """Return normalized records created within [mindate, maxdate], capped at
        max_results. CREATION_DATE is when Europe PMC indexed the record — the
        analogue of PubMed's edat — so it's monotonic in indexing time."""
        full_query = (
            f"({query}) AND (CREATION_DATE:["
            f"{mindate.isoformat()} TO {maxdate.isoformat()}])"
        )
        out: list[dict] = []
        cursor = "*"
        total = None
        while len(out) < max_results:
            params = {
                "query": full_query, "format": "json", "resultType": "core",
                "pageSize": min(_PAGE_SIZE, max_results - len(out)),
                "cursorMark": cursor,
            }
            data = self._request(params).json()
            if total is None:
                total = int(data.get("hitCount", 0))
                log.info("EuropePMC matched %d records in window", total)
            results = data.get("resultList", {}).get("result", [])
            if not results:
                break
            out.extend(normalize(r) for r in results)
            nxt = data.get("nextCursorMark")
            if not nxt or nxt == cursor:  # no more pages / cursor stalled
                break
            cursor = nxt
        if total is not None and total > max_results:
            log.warning(
                "EuropePMC returned %d but cap=%d; truncating to %d.",
                total, max_results, max_results)
        return out[:max_results]


def _journal(r: dict) -> str | None:
    """Published records carry journalInfo.journal.title; preprints carry the
    server (e.g. 'bioRxiv') in bookOrReportDetails.publisher."""
    ji = r.get("journalInfo") or {}
    journal = (ji.get("journal") or {}).get("title")
    if journal:
        return journal
    return (r.get("bookOrReportDetails") or {}).get("publisher")


def normalize(r: dict) -> dict:
    """Map one Europe PMC core result to the shared internal record shape."""
    src = r.get("source")  # 'PPR' preprint, 'MED' medline, etc.
    epmc_id = r.get("id")
    doi = (r.get("doi") or "").strip().lower() or None
    pub_types = {(t or "").lower() for t in (r.get("pubTypeList") or {}).get("pubType", [])}
    is_preprint = src == "PPR" or "preprint" in pub_types
    return {
        "source": "europepmc",
        "source_id": f"{src}:{epmc_id}",
        "doi": doi,
        "pmid": r.get("pmid"),
        "title": (r.get("title") or "").strip() or None,
        "authors": r.get("authorString"),
        "journal": _journal(r),
        "pub_date": r.get("firstPublicationDate") or r.get("pubYear"),
        "source_date": r.get("dateOfCreation") or r.get("firstIndexDate"),
        "abstract": r.get("abstractText"),
        "url": f"https://europepmc.org/article/{src}/{epmc_id}"
               if src and epmc_id else None,
        "is_preprint": is_preprint,
    }

"""Top-journal matching for the broad (Tier B) digest scope.

PubMed stores the journal as its NLM/ISO abbreviation first (fetch_pubmed.py:236),
e.g. "Nat Genet", "Mol Cell" — NOT the full names a human writes. The curated
library.json, by contrast, uses full names ("Nature genetics"). So a matcher for
the configured top-journal list must accept BOTH forms. This module owns that
normalization and the alias table for the known venues.

Add a journal to the broad tier by its full name in config/query.yaml. If it is
one of the venues in ALIASES below, the ISO-abbreviation form is matched too;
otherwise only the full name (normalized) is matched — extend ALIASES if PubMed
returns an abbreviation for it.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable


def _norm(journal: str | None) -> str:
    """Normalize a journal string for comparison: lowercase, drop periods, collapse
    whitespace, strip a trailing period. 'Nat. Genet.' -> 'nat genet'."""
    if not journal:
        return ""
    s = journal.lower().replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# full-name (normalized) -> additional accepted forms (normalized), e.g. ISO abbrev.
# The full name itself is always accepted; list only the EXTRA forms here.
ALIASES: dict[str, set[str]] = {
    "science": set(),
    "nature": set(),
    "cell": set(),
    "nature genetics": {"nat genet"},
    "cell genomics": {"cell genom"},
    "molecular cell": {"mol cell"},
    "genome biology": {"genome biol"},
    "genome research": {"genome res"},
    "nature neuroscience": {"nat neurosci"},
    "neuron": set(),  # PubMed abbreviation is identical
    "nature structural & molecular biology": {"nat struct mol biol"},
    "nature biotechnology": {"nat biotechnol"},
    "nature methods": {"nat methods"},
    "cell reports": {"cell rep"},
    "nature communications": {"nat commun"},
}


def build_matcher(top_journals: Iterable[str] | None) -> Callable[[str | None], bool]:
    """Return a predicate journal_str -> bool for the configured top-journal set.
    Accepts both the full name and (for known venues) the ISO abbreviation."""
    accepted: set[str] = set()
    for name in top_journals or []:
        key = _norm(name)
        if not key:
            continue
        accepted.add(key)
        accepted |= ALIASES.get(key, set())
    if not accepted:
        return lambda journal: False
    return lambda journal: _norm(journal) in accepted

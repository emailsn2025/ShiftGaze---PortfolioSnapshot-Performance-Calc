"""
scheme_matching.py
-------------------
Fuzzy-matches a mutual fund scheme name (as written by one source - Kuvera,
MF Central, whatever) against a set of candidate schemes from another source
(usually a CAS's own scheme names, or AMFI's master list), gated to the same
fund house first so category words like "large cap" can't cause a cross-AMC
false match ("HDFC Large Cap" matching "SBI Large Cap" just because both
contain those two words).

Originally lived inside kuvera_import.py; pulled out here once MF Central
needed the exact same matching logic against a different candidate set
(AMFI's scheme master, for ISIN backfill) - see mfcentral_parser.py.
"""

from __future__ import annotations

import re

AMC_KEYWORDS = [
    "aditya birla", "icici prudential", "nippon india", "franklin india", "canara robeco",
    "motilal oswal", "parag parikh", "mirae asset", "white oak", "whiteoak", "hdfc", "sbi",
    "kotak", "axis", "bandhan", "quant", "uti", "dsp", "tata", "invesco", "edelweiss",
    "idfc", "l&t", "hsbc", "baroda bnp paribas", "pgim", "sundaram", "union", "jm financial",
    "mahindra manulife", "360 one", "bajaj finserv", "trust", "bank of india", "itaka",
]
NOISE = {
    "fund", "plan", "direct", "growth", "the", "option", "regular", "scheme",
    "erstwhile", "standard", "dir", "gr", "of", "amc", "mutual", "ltd", "limited",
}


def stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 4 and tok.endswith("s") else tok


def amc_of(name: str) -> str | None:
    low = name.lower()
    for kw in AMC_KEYWORDS:
        if kw in low:
            return kw
    return None


def normalize(name: str, strip_amc: str | None = None) -> set[str]:
    name = re.sub(r"^[A-Z0-9]+\s*-\s*", "", name)       # strip leading scheme code, e.g. "85NGZ - "
    name = re.sub(r"\(.*?\)", " ", name)                 # strip "(Erstwhile ...)" style asides
    if strip_amc:
        name = re.sub(re.escape(strip_amc), " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^a-zA-Z0-9& ]", " ", name)
    return {stem(t.lower()) for t in name.split() if t.lower() not in NOISE}


def jaccard(a: set, b: set) -> tuple[float, int]:
    if not a or not b:
        return 0.0, 0
    inter = a & b
    return len(inter) / len(a | b), len(inter)


def best_match(
    query_name: str,
    candidates: list[tuple],
    threshold: float = 0.45,
    min_intersect: int = 1,
    prefer_substring: str | None = None,
) -> tuple | None:
    """
    candidates: list of (key, display_name) tuples, e.g. [(isin, scheme_name), ...].
    Returns (key, display_name, score) for the best match, or None if nothing
    clears the threshold. Gated to the same fund house first.

    prefer_substring: when multiple candidates tie closely, prefer ones whose
    display_name contains this substring (case-insensitive) - used to bias
    toward "Growth" plan variants when backfilling ISINs from AMFI's master
    list, which lists every dividend/IDCW option alongside Growth under
    near-identical names.
    """
    amc = amc_of(query_name)
    qn = normalize(query_name, strip_amc=amc)

    if amc:
        # Gate strictly: if this query has an identifiable fund house and
        # none of the candidates share it, this is not a match - falling
        # back to the unfiltered candidate pool here would let category
        # words alone ("large cap") match a completely different AMC's
        # scheme of the same category, which is the exact failure mode
        # this gate exists to prevent.
        pool = [c for c in candidates if amc_of(c[1]) == amc]
        if not pool:
            return None
    else:
        pool = candidates
    if not pool:
        return None

    scored = []
    for key, display_name in pool:
        cn = normalize(display_name, strip_amc=amc)
        score, inter_n = jaccard(qn, cn)
        prefer_bonus = 1 if (prefer_substring and prefer_substring.lower() in display_name.lower()) else 0
        scored.append((score, inter_n, prefer_bonus, key, display_name))
    scored.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    best = scored[0]
    if best[0] >= threshold and best[1] >= min_intersect:
        return best[3], best[4], best[0]
    return None


# --------------------------------------------------------------------------
# Equity (listed shares) matching - a different normalizer from the MF one
# above. Company names don't have an "AMC" concept to gate on, and CDSL's
# security names carry a long, variable boilerplate suffix ("#NEW EQUITY
# SHARES WITH FACE VALUE RS.2/- AFTER SUB DIVISION") that the MF-oriented
# NOISE set doesn't strip - so this uses Jaccard directly (no AMC gate),
# after cutting that suffix off entirely. Jaccard rather than the MF
# matcher's overlap-coefficient matters here specifically because Indian
# corporate demergers can leave several similarly-named listed entities
# (e.g. Vedanta Limited vs Vedanta Power Limited vs Vedanta Aluminium Metal
# Limited) - Jaccard penalises a candidate's extra unmatched tokens, which
# is what correctly prefers the exact "Vedanta Limited" over its siblings
# for a plain "Vedanta Ltd" query; an overlap coefficient does not.
# --------------------------------------------------------------------------

EQUITY_NOISE = {"limited", "ltd", "private", "pvt", "company", "co", "the", "new"}


def normalize_equity(name: str) -> set[str]:
    name = re.sub(r"\bEQ(UITY)?\s*SH(ARE)?S?\b.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^a-zA-Z0-9& ]", " ", name)
    return {t.lower() for t in name.split() if t.lower() not in EQUITY_NOISE}


def best_match_equity(
    query_name: str, candidates: list[tuple], threshold: float = 0.5, min_intersect: int = 1
) -> tuple | None:
    """Same shape as best_match(), for listed-share security names instead of fund scheme names."""
    if not candidates:
        return None
    qn = normalize_equity(query_name)
    scored = []
    for key, display_name in candidates:
        cn = normalize_equity(display_name)
        score, inter_n = jaccard(qn, cn)
        scored.append((score, inter_n, key, display_name))
    scored.sort(reverse=True)
    best = scored[0]
    if best[0] >= threshold and best[1] >= min_intersect:
        return best[2], best[3], best[0]
    return None

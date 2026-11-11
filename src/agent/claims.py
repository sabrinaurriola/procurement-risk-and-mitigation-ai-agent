"""Deterministic wording rules for claims (no LLM): price direction, other-market price quotations, hypothetical shocks,
cadence wording attributed to a policy, and the approval-related wording of supplier recommendations.

Everything here is lexical and intentionally conservative; it complements, and never replaces, the citation checks."""
from __future__ import annotations

import re

_I = re.IGNORECASE

# ───────────── observed price direction ─────────────
# the direction word must sit next to the word "price(s)": "rising sunflower oil SUPPLY" is not a rising price
_DOWN = r"(?:fall|falls|falling|fell|fallen|declin\w*|decreas\w*|drop\w*|lost|lose|lower|slid\w*|weaken\w*|eas(?:e|es|ed|ing)|under pressure|slump\w*)"
_UP = r"(?:rise|rises|rising|rose|risen|increas\w*|higher|climb\w*|surg\w*|jump\w*|gain\w*|rall\w*)"
_GAP = r"(?:\s+[\w$€,./–-]+){0,7}?\s+"
PRICE_DOWN = re.compile(rf"\bprices?{_GAP}{_DOWN}\b|\b{_DOWN}\b{_GAP}prices?\b", _I)
PRICE_UP = re.compile(rf"\bprices?{_GAP}{_UP}\b|\b{_UP}\b{_GAP}prices?\b", _I)
_SUPPLY_WORDS = re.compile(r"\b(?:rising|increasing|increased|growing|higher|large|record|sharp)\s+(?:\w+\s+){0,3}(?:supply|supplies|stocks|inventor\w+|production|harvest\w*|volumes?)\b", _I)


def price_direction(title: str, snippet: str) -> str:
    """'down' | 'up' | 'mixed' | 'none' from price-adjacent wording in an evidence item (title and snippet)."""
    text = _SUPPLY_WORDS.sub(" ", f"{title}. {snippet}")
    d, u = len(PRICE_DOWN.findall(text)), len(PRICE_UP.findall(text))
    if d == u:
        return "mixed" if d else "none"
    return "down" if d > u else "up"


def observed_price_direction(evidence) -> str:
    """'falling' | 'rising' | 'mixed' | 'unknown' across the direct, strong-tier, dated risk items."""
    counts = {"down": 0, "up": 0, "mixed": 0}
    for e in evidence:
        if e.kind == "risk" and e.relevance == "direct" and e.credibility in ("official", "trade_press") and not other_market_markers(e.title):
            k = price_direction(e.title, e.snippet)
            if k in counts:
                counts[k] += 1
    d, u, m = counts["down"], counts["up"], counts["mixed"]
    if d >= 2 and d > u + m:
        return "falling"
    if u >= 2 and u > d + m:
        return "rising"
    return "mixed" if d + u + m else "unknown"


UP_CLAIM = re.compile(r"\b(?:upward|rising|increasing|higher|elevated)\s+(?:[\w-]+\s+){0,2}(?:price|prices|cost|costs)\b|"
                      r"\bprices?\s+(?:are |is |remain |continue to |will )?(?:rising|increasing|climbing|higher|rise|increase)\b|"
                      r"\b(?:price|pricing|upward)\s+pressure\b|\bcontinuing\s+(?:[\w-]+\s+){0,3}pressure\b|\bprice\s+(?:increases?|rises?|rally)\b", _I)
DOWN_CLAIM = re.compile(r"\b(?:falling|declining|lower|weaker|easing)\s+(?:[\w-]+\s+){0,2}(?:price|prices|cost|costs)\b|"
                        r"\bprices?\s+(?:are |is |remain |continue to |have |has )?(?:falling|fall|fell|fallen|declin\w+|decreas\w+|eas\w+|lower)\b", _I)
FALL_WORD = re.compile(r"\b(?:fall\w*|fell|declin\w*|decreas\w*|lower|eas(?:e|es|ed|ing)|drop\w*|weaker|lost)\b", _I)
RISE_WORD = re.compile(r"\b(?:ris(?:e|es|ing)|rose|increas\w*|higher|climb\w*|surg\w*|upward)\b", _I)

# ───────────── price quotations from another market ─────────────
PRICE_QUOTE = re.compile(r"[$€]\s?\d|\b(?:usd|eur|myr)\s?\d|\d\s?(?:usd|eur|myr)\b|/\s?(?:t|mt|tonne|ton)\b|\bper\s+(?:metric\s+)?(?:tonne|ton)\b", _I)
LOCAL_MARKET = re.compile(r"\b(?:spain|spanish|eu|europe|european|rotterdam|barcelona|valencia|euros?)\b|€", _I)
OTHER_MARKETS = ("india", "mumbai", "kolkata", "pakistan", "bangladesh", "china", "malaysia", "indonesia", "united states", "u.s.", "russia",
                 "russian", "ukraine", "ukrainian", "black sea", "turkey", "egypt", "brazil", "argentina", "fob", "cif", "fca")
NAMING_MARKERS_EXCLUDED = {"black sea", "fob", "cif", "fca", "unspecified market"}   # delivery bases/regions do not name a market
PRICE_STATEMENT = re.compile(r"\bprices?\b|\bquotations?\b", _I)
SEED_PRICE = re.compile(r"\bsunflower\s+seeds?\b|\bpurchase prices? for sunflower\b", _I)
PRICE_WORD = re.compile(r"\b(?:price|prices|pricing|cost|costs|benchmark|premium|expensive|cheaper)\b", _I)


def other_market_markers(quote: str) -> set[str]:
    """Markers of the market/delivery basis named in a PRICE quotation that is not about Spain/the EU, else an empty set.
    A quote without a price figure (e.g. a supply-chain mechanism) never counts as an other-market price."""
    figure, word = PRICE_QUOTE.search(quote), PRICE_STATEMENT.search(quote)
    if not (figure or word) or LOCAL_MARKET.search(quote):
        return set()
    low = quote.casefold()
    found = {m for m in OTHER_MARKETS if re.search(rf"(?<![\w]){re.escape(m)}(?![\w])", low)}
    if SEED_PRICE.search(quote):
        found.add("sunflower seed (different commodity)")      # seed prices are not sunflower-OIL prices
    if figure:
        return found or {"unspecified market"}
    return found                                               # a price statement without a figure counts only if it names another market


# ───────────── hypothetical shocks ─────────────
SHOCK = re.compile(r"\bshocks?\b|[+\-−]\s?\d+(?:\.\d+)?\s?%\s+(?:benchmark|price)", _I)
SCENARIO = re.compile(r"\b(?:scenarios?|sensitivit\w+|hypothetical|what-if|illustrat\w+|assum\w+)\b|\bwould\s+(?:add|increase|raise|reduce)\b|\bif the benchmark\b", _I)
FORECAST = re.compile(r"\b(?:forecast\w*|project\w*|predict\w*|expected|expects?|anticipated|likely|will)\b", _I)
NOT_FORECAST = re.compile(r"\bnot\s+(?:a\s+|an\s+)?(?:price\s+)?(?:forecast|prediction|projection)\b|\bno\s+forecast\b", _I)


def shock_problems(text: str) -> list[str]:
    out = []
    if SHOCK.search(text) and not SCENARIO.search(text):
        out.append("a benchmark shock must be presented as a hypothetical sensitivity scenario")
    for sent in re.split(r"(?<=[.;])\s+", text):
        if SHOCK.search(sent) and FORECAST.search(NOT_FORECAST.sub(" ", sent)):
            out.append("a benchmark shock is a sensitivity scenario, not a price forecast")
            break
    return out


# ───────────── internal exposure inside an external-only finding ─────────────
INTERNAL_EXPOSURE = re.compile(r"\bbaseline modelled\b|\bmodelled (?:procurement )?cost\b|\bshocks?\b|\bcommitted volume\b|\buncovered\b|\bSUP-\d+|\bCT-\d+", _I)

# ───────────── cadence / process wording that may not be attributed to a policy ─────────────
CADENCE = re.compile(r"\b(?:monthly|quarterly|weekly|annual(?:ly)?|bi-?weekly)\s+(?:[\w-]+\s+){0,2}(?:review|reviews|meeting|check|report|reporting|monitoring|cycle|process)\b|"
                     r"\bshort-?list\w*|\bwithin\s+\d+\s+(?:days|weeks|months)\b|\bevery\s+(?:month|week|quarter|\d+\s+\w+)\b", _I)

# ───────────── approval wording for supplier recommendations ─────────────
SIGN_OFF = re.compile(r"\bsign-?\s?off\b|\bProcurement Director\b|\bapproval authority\b", _I)
ESCALATE = re.compile(r"\bescalat\w+", _I)
APPROVAL_QUOTE = re.compile(r"\bsign-?\s?off\b|\bapprov\w+|\bProcurement Director\b|\bLegal\b|\breview\w*", _I)
EXCEPTION = re.compile(r"\b(?:exception|waive\w*|waiver|bypass\w*|override\w*|fast-?track\w*|expedit\w*)\b", _I)
BLOCKED = re.compile(r"(?<!not )(?<!n't )\b(?:blocked|blacklist\w*|disqualif\w*|barred|banned)\b", _I)
AGENT_DECISION = re.compile(r"^\s*(?:approve|authori[sz]e|renew|award|place|order)\b", _I)
SUPPLIER_CONTEXT = re.compile(r"\bconditional\w*|\bSUP-\d+|\bCT-\d+", _I)
ENTITY = re.compile(r"\b(?:SUP|CT)-\d+", _I)


def entity_ids(text: str) -> set[str]:
    return {m.upper() for m in ENTITY.findall(text)}


def passage_entities(policy_ref: str, passage_text: str) -> set[str]:
    """Supplier/contract ids a policy passage is about: ids in its text plus the supplier of an assessment document (ASM-003 -> SUP-003)."""
    ids = entity_ids(passage_text)
    m = re.match(r"ASM-(\d+)", policy_ref)
    if m:
        ids.add(f"SUP-{m.group(1)}")
    return ids


# ───────────── logistics / shipping publications ─────────────
LOGISTICS_DOMAINS = ("safety4sea.com", "splash247.com", "lloydslist.com", "tradewindsnews.com", "joc.com", "freightwaves.com", "gcaptain.com",
                     "maritime-executive.com", "seatrade-maritime.com", "ship-technology.com", "porttechnology.org", "hellenicshippingnews.com")
LOGISTICS_WORDS = re.compile(r"\b(?:port|ports|shipping|shipments?|vessels?|freight|export|exports|route|routes|rerout\w*|re-rout\w*|logistic\w*|"
                             r"corridor|blockade|suspend\w*|delays?|disrupt\w*|transit|terminals?|trade flows?|congestion)\b", _I)


def logistics_support(domain: str, quote: str) -> bool:
    """A shipping/logistics publication may carry a SUPPLY-DISRUPTION finding when the quoted passage itself describes a logistics
    disruption. It never carries a price, supplier or exposure claim."""
    d = domain.lower()
    return any(d == x or d.endswith("." + x) for x in LOGISTICS_DOMAINS) and bool(LOGISTICS_WORDS.search(quote))

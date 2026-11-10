"""Source-credibility tiers, contact-detail redaction and country aliases. Deliberately small and rule-based:
a proportionate guard for an MVP, not a fact-checking platform.

Tiers
  official    – public institutions / statistical or regulatory bodies
  trade_press – established news agencies and commodity/shipping/grain trade press
  commercial  – market-research vendors, directories, marketplaces, press-release wires (self-interested or aggregated)
  social      – social media and user-generated content (lowest confidence; never supports a finding)
  unrated     – anything else (e.g. a company's own website: a source for what the company says about itself only)
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

TIER_DOMAINS: dict[str, tuple[str, ...]] = {
    "official": ("europa.eu", "ec.europa.eu", "eur-lex.europa.eu", "eurostat.eu", "fao.org", "usda.gov", "oecd.org",
                 "worldbank.org", "imf.org", "mapa.gob.es", "ine.es", "bde.es", "gov.uk"),
    "trade_press": ("reuters.com", "bloomberg.com", "ft.com", "wsj.com", "ofimagazine.com", "hellenicshippingnews.com",
                    "graintrade.com.ua", "fastmarkets.com", "euractiv.com", "agrimoney.com", "world-grain.com", "spglobal.com",
                    "argusmedia.com", "oilworld.biz", "biofuels-news.com", "agriland.ie", "agroberichtenbuitenland.nl",
                    "agronews.com", "euromonitor.com"),
    "commercial": ("imarcgroup.com", "chemanalyst.com", "europages.co.uk", "europages.com", "europages.es", "industrystock.com",
                   "alibaba.com", "indiamart.com", "tradekey.com", "made-in-china.com", "globenewswire.com", "prnewswire.com",
                   "businesswire.com", "expertmarketresearch.com", "mordorintelligence.com", "marketresearch.com"),
    "social": ("facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com", "reddit.com", "youtube.com", "tiktok.com",
               "quora.com", "t.me", "whatsapp.com", "pinterest.com", "medium.com"),
}
MOCK_SUFFIX = ".example.invalid"   # test/demo convention: the first host label names the tier (e.g. social.example.invalid)
STRONG_TIERS = frozenset({"official", "trade_press"})


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def credibility(url: str) -> str:
    host = host_of(url)
    if host.endswith(MOCK_SUFFIX):
        label = host[: -len(MOCK_SUFFIX)].split(".")[-1]
        return {"official": "official", "tradepress": "trade_press", "commercial": "commercial", "social": "social"}.get(label, "unrated")
    for tier, domains in TIER_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return tier
    return "unrated"


# ───────────── contact-detail redaction ─────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INTL_PHONE_RE = re.compile(r"(?<![\w.])(?:\+|00)\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,6}")
CUED_PHONE_RE = re.compile(r"(?i)\b(tel|telephone|phone|mobile|mob|whatsapp|fax|call)\b[\s:.-]*\(?\+?\d[\d\s().-]{6,}\d")


def redact_contacts(text: str) -> str:
    """Remove third-party email addresses and phone numbers. Conservative on purpose: prices, dates and tonnages
    (which look like numbers) are untouched because phone numbers are only matched with a + / 00 prefix or a cue word."""
    text = EMAIL_RE.sub("[email removed]", text)
    text = CUED_PHONE_RE.sub(lambda m: f"{m.group(1)} [phone removed]", text)
    return INTL_PHONE_RE.sub("[phone removed]", text)


# ───────────── country evidence ─────────────
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "Spain": ("spain", "spanish", "españa", "espana"), "Romania": ("romania", "romanian"), "France": ("france", "french"),
    "Germany": ("germany", "german", "deutschland"), "Italy": ("italy", "italian", "italia"),
    "Netherlands": ("netherlands", "dutch", "holland"), "Portugal": ("portugal", "portuguese"),
    "Ukraine": ("ukraine", "ukrainian"), "Bulgaria": ("bulgaria", "bulgarian"), "Hungary": ("hungary", "hungarian"),
    "Turkey": ("turkey", "turkish", "türkiye"), "Argentina": ("argentina", "argentine", "argentinian"),
    "Russia": ("russia", "russian"), "Poland": ("poland", "polish"), "Greece": ("greece", "greek"),
}


def country_supported(country: str, quote: str) -> bool:
    """A country is only accepted when the quote names it (or its adjective) explicitly. Phone prefixes, domain
    endings and vague regions ('across Europe') are never evidence of a country."""
    aliases = COUNTRY_ALIASES.get(country)
    q = quote.lower()
    return bool(aliases) and any(re.search(rf"\b{re.escape(a)}\b", q) for a in aliases)

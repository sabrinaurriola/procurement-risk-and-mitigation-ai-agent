"""Deterministic evidence pre-filtering and identification (before any LLM relevance judgement).

Nothing here modifies a result: items are accepted or rejected with a recorded reason, so filtering is auditable."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

from src.agent.config import IngredientProfile
from src.agent.credibility import credibility, host_of, redact_contacts
from src.agent.schemas import EvidenceItem
from src.tools.search import RawResult

ALLOWED_LANGUAGES = {None, "", "english", "spanish"}


@dataclass(frozen=True)
class Rejection:
    url: str
    title: str
    reason: str


def normalise_url(url: str) -> str:
    p = urlsplit(url.strip())
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def prefilter(raws: list[RawResult], profile: IngredientProfile, seen_urls: set[str], today: date,
              lookback_days: int) -> tuple[list[RawResult], list[Rejection]]:
    """Accept items that are new, in the date window, in a supported language and that mention the ingredient."""
    accepted: list[RawResult] = []
    rejected: list[Rejection] = []
    seen = set(seen_urls)
    earliest = today - timedelta(days=lookback_days)
    for r in raws:
        key = normalise_url(r.url)
        reason = None
        if key in seen:
            reason = "DUPLICATE_URL"
        elif (r.language or "") not in ALLOWED_LANGUAGES:
            reason = "UNSUPPORTED_LANGUAGE"
        elif not any(k in f"{r.title} {r.snippet}".lower() for k in profile.keywords):
            reason = "NO_INGREDIENT_MATCH"
        elif r.kind == "risk" and r.published_date is None:
            reason = "UNDATED"                      # risk claims need a dated source
        elif r.published_date is not None and r.published_date > today:
            reason = "FUTURE_DATE"
        elif r.kind == "risk" and r.published_date is not None and r.published_date < earliest:
            reason = "OUTSIDE_DATE_WINDOW"
        if reason:
            rejected.append(Rejection(r.url, redact_contacts(r.title[:120]), reason))
            continue
        seen.add(key)
        accepted.append(r)
    return accepted, rejected


def to_evidence(raws: list[RawResult], start_index: int, retrieved_on: date, is_mock: bool) -> list[EvidenceItem]:
    """Identifiers run E{start_index}, E{start_index+1}, ... (pass len(existing)+1 to continue a sequence)."""
    return [EvidenceItem(evidence_id=f"E{start_index + i}", source=r.source, kind=r.kind, url=r.url, domain=host_of(r.url),
                         credibility=credibility(r.url), title=redact_contacts(r.title), snippet=redact_contacts(r.snippet),
                         published_date=r.published_date, retrieved_on=retrieved_on, query=r.query, is_mock=is_mock)
            for i, r in enumerate(raws)]

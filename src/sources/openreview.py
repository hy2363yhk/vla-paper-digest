"""OpenReview v2 API source for CoRL / ICLR / NeurIPS accepted papers.

The OpenReview API is unauthenticated for read access. We keep the integration
intentionally defensive: any failure is logged and returns an empty list so
that one flaky data source never poisons the overall run.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.models import Paper
from src.utils import now_utc

OPENREVIEW_API = "https://api2.openreview.net"

# venue_id prefixes we know are useful — OpenReview stores them as
# e.g. "ICLR.cc/2025/Conference".
VENUE_INVITATIONS: dict[str, list[str]] = {
    "ICLR": ["ICLR.cc/{year}/Conference"],
    "NeurIPS": ["NeurIPS.cc/{year}/Conference"],
    "CoRL": ["CoRL.cc/{year}/Conference", "robot-learning.org/CoRL/{year}/Conference"],
}


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def _get(url: str, params: dict[str, Any]) -> dict:
    with httpx.Client(
        headers={"User-Agent": "vla-paper-digest/0.1"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _fetch_venue(venue_id: str, limit_per_page: int = 200) -> list[dict]:
    """Fetch all notes under a venue invitation. OpenReview paginates via offset."""

    out: list[dict] = []
    offset = 0
    while True:
        try:
            data = _get(
                f"{OPENREVIEW_API}/notes",
                params={
                    "content.venueid": venue_id,
                    "details": "replyCount",
                    "offset": offset,
                    "limit": limit_per_page,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(f"OpenReview fetch failed for venue='{venue_id}': {exc}")
            break
        notes = data.get("notes") or []
        if not notes:
            break
        out.extend(notes)
        if len(notes) < limit_per_page:
            break
        offset += limit_per_page
    return out


def _note_to_paper(note: dict, venue_label: str) -> Paper | None:
    content = note.get("content") or {}

    def _val(key: str) -> Any:
        v = content.get(key)
        if isinstance(v, dict):  # OR v2 wraps values in {"value": ...}
            return v.get("value")
        return v

    title = (_val("title") or "").strip()
    abstract = (_val("abstract") or "").strip()
    if not title:
        return None
    authors = _val("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    # Many OpenReview submissions include an ``aff`` or ``affiliation`` field;
    # it may be a single string, a semicolon-joined list, or a list of strings.
    aff_raw = _val("aff") or _val("affiliation") or _val("affiliations")
    affiliations: list[str] = []
    seen_aff: set[str] = set()
    if isinstance(aff_raw, str):
        parts: list[str] = [p.strip() for p in aff_raw.replace("\n", ";").split(";") if p.strip()]
    elif isinstance(aff_raw, list):
        parts = [str(p).strip() for p in aff_raw if p]
    else:
        parts = []
    for aff in parts:
        if aff.lower() in seen_aff:
            continue
        seen_aff.add(aff.lower())
        affiliations.append(aff)
        if len(affiliations) >= 3:
            break
    decision = (_val("decision") or "").lower()
    if decision and "reject" in decision:
        return None
    note_id = note.get("id") or ""
    pdate_raw = _val("pdate")
    year = _extract_year(venue_label, pdate_raw)
    pub_date = _pdate_to_date(pdate_raw) or (_date_from_year(year) if year else None)
    return Paper(
        paper_id=f"openreview:{note_id}" if note_id else f"openreview:{title}",
        title=title,
        abstract=abstract,
        authors=[a for a in authors if a],
        affiliations=affiliations,
        venue=venue_label,
        year=year,
        publication_date=pub_date,
        citation_count=0,
        influential_citation_count=0,
        arxiv_id=None,
        external_url=f"https://openreview.net/forum?id={note_id}" if note_id else None,
        source="openreview",
        first_seen_at=now_utc(),
    )


def _pdate_to_date(pdate: Any):
    from datetime import datetime, timezone

    if isinstance(pdate, int) and pdate > 0:
        return datetime.fromtimestamp(pdate / 1000, tz=timezone.utc).date()
    return None


def _date_from_year(year: int):
    from datetime import date

    # Conferences without fine-grained dates get an "annual anchor" so the
    # freshness score can still differentiate them from older venues.
    try:
        return date(year, 1, 1)
    except ValueError:
        return None


def _extract_year(venue_label: str, pdate: Any) -> int | None:
    import re

    m = re.search(r"(20\d{2})", venue_label)
    if m:
        return int(m.group(1))
    if isinstance(pdate, int) and pdate > 0:
        # OpenReview stores epoch ms
        from datetime import datetime, timezone

        return datetime.fromtimestamp(pdate / 1000, tz=timezone.utc).year
    return None


def fetch_openreview_accepted(years: list[int]) -> list[Paper]:
    """Fetch accepted notes from ICLR/NeurIPS/CoRL for the given years."""

    collected: list[Paper] = []
    for venue_key, templates in VENUE_INVITATIONS.items():
        for year in years:
            for tpl in templates:
                venue_id = tpl.format(year=year)
                venue_label = f"{venue_key} {year}"
                logger.info(f"[OpenReview] fetching {venue_id} ...")
                notes = _fetch_venue(venue_id)
                for note in notes:
                    paper = _note_to_paper(note, venue_label)
                    if paper:
                        collected.append(paper)
    logger.info(f"[OpenReview] collected {len(collected)} candidate papers")
    return collected

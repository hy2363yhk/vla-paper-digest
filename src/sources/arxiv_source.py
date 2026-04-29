"""arXiv ingestion, upgraded to cover industry labs.

Previously this module only queried ``cat:cs.RO`` with a topic keyword OR.
That misses any NVIDIA / DeepSeek / Meta / Google paper whose primary category
is ``cs.LG`` or ``cs.CL``, or any paper whose arXiv ``<author>`` tags carry
collective names like ``DeepSeek-AI`` / ``Physical Intelligence``.

The strategy here mirrors ``paper_pulse``'s multi-branch search:

    1. Topic-keyword sweep over configured arXiv categories (broad → pre-filter
       by VLA keywords).
    2. Author search ``au:"Name"`` using the ``arxiv_au`` list from
       ``config/labs.yaml`` — priority papers, tagged with ``lab_key``.
    3. Title search ``ti:"Brand"`` for model/series names (π0, GR00T, Kimi…).
    4. All-field search ``all:"Institution"`` for affiliation terms.
    5. Watchlist ``id_list=`` for explicitly-pinned arXiv IDs.

All branches dedup by arXiv id before merging. We implement the HTTP layer
ourselves (via ``httpx`` + ``feedparser``) rather than the ``arxiv`` package
to avoid a new dependency.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import feedparser
import httpx
from loguru import logger

from src.config_labs import LabsConfig
from src.models import Paper
from src.utils import now_utc

ARXIV_API = "https://export.arxiv.org/api/query"
# urlencode ``safe=`` must not live inside an f-string ``{...}`` (backslashes illegal there).
_ARXIV_URLENC_SAFE = ':()" '

# arXiv asks us to pace our requests. 3s between calls is their recommended
# interval; we sometimes relax this to 1s when they return cached responses.
_ARXIV_PACE_SECONDS = 3.0


# ── Low-level HTTP helper ──────────────────────────────────────────────────

def _get_feed(params: dict, *, pace: float = _ARXIV_PACE_SECONDS) -> feedparser.FeedParserDict | None:
    """Fire one arXiv API call, return parsed feed or ``None`` on network error."""
    url = f"{ARXIV_API}?{urlencode(params, safe=_ARXIV_URLENC_SAFE)}"
    logger.debug(f"[arXiv] GET {url}")
    try:
        with httpx.Client(
            headers={"User-Agent": "vla-paper-digest/0.1"},
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"arXiv fetch failed: {exc}")
        return None
    time.sleep(pace)
    return feedparser.parse(resp.text)


def _extract_arxiv_id(entry_id: str) -> str | None:
    if not entry_id:
        return None
    tail = entry_id.rsplit("/", 1)[-1]
    return tail.split("v")[0] if tail else None


def _feed_to_papers(
    parsed: feedparser.FeedParserDict,
    *,
    cutoff: datetime,
    source_tag: str = "arxiv",
) -> list[Paper]:
    """Convert an arXiv Atom feed into :class:`Paper` objects with lookback filter."""
    out: list[Paper] = []
    for entry in getattr(parsed, "entries", []) or []:
        published_str = getattr(entry, "published", "") or getattr(entry, "updated", "")
        try:
            published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            published_dt = None
        if published_dt and published_dt < cutoff:
            continue
        arxiv_id = _extract_arxiv_id(getattr(entry, "id", ""))
        if not arxiv_id:
            continue
        authors = [a.name for a in getattr(entry, "authors", []) if getattr(a, "name", None)]
        title = (entry.title or "").strip().replace("\n", " ")
        abstract = (getattr(entry, "summary", "") or "").strip()
        pub_date = published_dt.astimezone(timezone.utc).date() if published_dt else None
        out.append(
            Paper(
                paper_id=f"arxiv:{arxiv_id}",
                title=title,
                abstract=abstract,
                authors=authors,
                venue="arXiv",
                year=pub_date.year if pub_date else None,
                publication_date=pub_date,
                citation_count=0,
                influential_citation_count=0,
                arxiv_id=arxiv_id,
                external_url=f"https://arxiv.org/abs/{arxiv_id}",
                source=source_tag,
                first_seen_at=now_utc(),
            )
        )
    return out


def _quoted(term: str) -> str:
    t = term.replace('"', " ").strip()
    return f'"{t}"' if " " in t else t


def _cat_clause(categories: Iterable[str]) -> str:
    return " OR ".join(f"cat:{c}" for c in categories) or "cat:cs.RO"


# ── Branch 1: broad topic-keyword sweep ────────────────────────────────────

def fetch_arxiv_keyword_sweep(
    categories: list[str],
    keywords: list[str],
    *,
    lookback_days: int = 90,
    max_results: int = 300,
) -> list[Paper]:
    """Multi-category × keyword sweep (replaces the old cs.RO-only branch)."""
    if not keywords:
        return []
    quoted_kws = [_quoted(k) for k in keywords if k.strip()]
    if not quoted_kws:
        return []
    kw_clause = " OR ".join(f"(ti:{q} OR abs:{q})" for q in quoted_kws)
    cat_clause = _cat_clause(categories)
    query = f"({cat_clause}) AND ({kw_clause})"
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    parsed = _get_feed(params)
    if parsed is None:
        return []
    cutoff = now_utc() - timedelta(days=lookback_days)
    papers = _feed_to_papers(parsed, cutoff=cutoff)
    logger.info(f"[arXiv] keyword-sweep: {len(papers)} papers across {categories}")
    return papers


# ── Branch 2: author search ────────────────────────────────────────────────

def fetch_arxiv_by_authors(
    categories: list[str],
    author_terms: list[str],
    *,
    lookback_days: int = 30,
    max_results: int = 100,
    batch_size: int = 8,
) -> list[Paper]:
    """``au:"Name"`` search across categories, batched to keep URLs sane."""
    author_terms = [a.strip() for a in author_terms if a.strip()]
    if not author_terms:
        return []
    cat_clause = _cat_clause(categories)
    cutoff = now_utc() - timedelta(days=lookback_days)
    all_papers: list[Paper] = []
    seen: set[str] = set()
    for i in range(0, len(author_terms), batch_size):
        batch = author_terms[i : i + batch_size]
        au_clause = " OR ".join(f'au:{_quoted(a)}' for a in batch)
        query = f"({cat_clause}) AND ({au_clause})"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        parsed = _get_feed(params)
        if parsed is None:
            continue
        for p in _feed_to_papers(parsed, cutoff=cutoff):
            if p.arxiv_id and p.arxiv_id not in seen:
                seen.add(p.arxiv_id)
                all_papers.append(p)
    logger.info(f"[arXiv] author-search: {len(all_papers)} papers ({len(author_terms)} authors)")
    return all_papers


# ── Branch 3: title keyword search ─────────────────────────────────────────

def fetch_arxiv_by_title_keywords(
    categories: list[str],
    title_keywords: list[str],
    *,
    lookback_days: int = 30,
    max_results: int = 100,
    batch_size: int = 10,
) -> list[Paper]:
    title_keywords = [k.strip() for k in title_keywords if k.strip()]
    if not title_keywords:
        return []
    cat_clause = _cat_clause(categories)
    cutoff = now_utc() - timedelta(days=lookback_days)
    all_papers: list[Paper] = []
    seen: set[str] = set()
    for i in range(0, len(title_keywords), batch_size):
        batch = title_keywords[i : i + batch_size]
        ti_clause = " OR ".join(f'ti:{_quoted(k)}' for k in batch)
        query = f"({cat_clause}) AND ({ti_clause})"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        parsed = _get_feed(params)
        if parsed is None:
            continue
        for p in _feed_to_papers(parsed, cutoff=cutoff):
            if p.arxiv_id and p.arxiv_id not in seen:
                seen.add(p.arxiv_id)
                all_papers.append(p)
    logger.info(f"[arXiv] title-keyword-search: {len(all_papers)} papers")
    return all_papers


# ── Branch 4: all-field affiliation search ─────────────────────────────────

def fetch_arxiv_by_affiliations(
    categories: list[str],
    affiliation_terms: list[str],
    *,
    lookback_days: int = 30,
    max_results: int = 100,
    batch_size: int = 6,
) -> list[Paper]:
    affiliation_terms = [a.strip() for a in affiliation_terms if a.strip()]
    if not affiliation_terms:
        return []
    cat_clause = _cat_clause(categories)
    cutoff = now_utc() - timedelta(days=lookback_days)
    all_papers: list[Paper] = []
    seen: set[str] = set()
    for i in range(0, len(affiliation_terms), batch_size):
        batch = affiliation_terms[i : i + batch_size]
        all_clause = " OR ".join(f'all:{_quoted(a)}' for a in batch)
        query = f"({cat_clause}) AND ({all_clause})"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        parsed = _get_feed(params)
        if parsed is None:
            continue
        for p in _feed_to_papers(parsed, cutoff=cutoff):
            if p.arxiv_id and p.arxiv_id not in seen:
                seen.add(p.arxiv_id)
                all_papers.append(p)
    logger.info(f"[arXiv] affiliation-search: {len(all_papers)} papers")
    return all_papers


# ── Branch 5: explicit watchlist ───────────────────────────────────────────

def fetch_arxiv_watchlist_ids(
    arxiv_ids: list[str],
    *,
    lookback_days: int = 365,
    batch_size: int = 50,
) -> list[Paper]:
    """Pull exactly these arXiv IDs (e.g. papers whose metadata has no org clue)."""
    seen: set[str] = set()
    ids: list[str] = []
    for raw in arxiv_ids or []:
        s = str(raw).strip().split("v")[0]
        if s and s not in seen:
            seen.add(s)
            ids.append(s)
    if not ids:
        return []
    cutoff = now_utc() - timedelta(days=lookback_days)
    all_papers: list[Paper] = []
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        params = {
            "id_list": ",".join(batch),
            "start": 0,
            "max_results": len(batch),
        }
        parsed = _get_feed(params)
        if parsed is None:
            continue
        all_papers.extend(_feed_to_papers(parsed, cutoff=cutoff))
    logger.info(f"[arXiv] watchlist: {len(all_papers)}/{len(ids)} papers")
    return all_papers


# ── Aggregator ─────────────────────────────────────────────────────────────

def fetch_arxiv_all(
    keywords: list[str],
    labs_cfg: LabsConfig,
    *,
    lookback_days: int | None = None,
) -> list[Paper]:
    """Run all five branches, merge, and dedupe.

    ``lookback_days`` overrides the keyword-sweep cutoff; lab-targeted searches
    use ``labs_cfg.arxiv_institution_lookback_days`` (tighter) because we only
    care about *new* releases from those labs.
    """
    categories = list(labs_cfg.arxiv_categories)
    sweep_lookback = lookback_days if lookback_days is not None else labs_cfg.arxiv_lookback_days

    all_papers: dict[str, Paper] = {}

    def _merge(batch: list[Paper]) -> None:
        for p in batch:
            if p.arxiv_id and p.arxiv_id not in all_papers:
                all_papers[p.arxiv_id] = p

    # 1. keyword sweep (broad)
    _merge(
        fetch_arxiv_keyword_sweep(
            categories,
            keywords,
            lookback_days=sweep_lookback,
            max_results=labs_cfg.arxiv_max_results_per_query,
        )
    )

    # 2-3. per-lab author & title batches
    all_authors: list[str] = []
    all_titles: list[str] = []
    for lab in labs_cfg.labs:
        all_authors.extend(lab.arxiv_au)
        all_titles.extend(lab.title_keywords)
    _merge(
        fetch_arxiv_by_authors(
            categories,
            all_authors,
            lookback_days=labs_cfg.arxiv_institution_lookback_days,
            max_results=100,
        )
    )
    _merge(
        fetch_arxiv_by_title_keywords(
            categories,
            all_titles,
            lookback_days=labs_cfg.arxiv_institution_lookback_days,
            max_results=100,
        )
    )

    # 4. affiliation search (expensive — only if we have terms)
    _merge(
        fetch_arxiv_by_affiliations(
            categories,
            list(labs_cfg.affiliation_terms),
            lookback_days=labs_cfg.arxiv_institution_lookback_days,
            max_results=100,
        )
    )

    # 5. watchlist (force-pull)
    _merge(
        fetch_arxiv_watchlist_ids(
            labs_cfg.all_watchlist_ids(),
            lookback_days=labs_cfg.arxiv_watchlist_lookback_days,
        )
    )

    papers = list(all_papers.values())
    logger.info(
        f"[arXiv] merged total: {len(papers)} unique arxiv papers "
        f"(sweep lookback {sweep_lookback}d, lab lookback {labs_cfg.arxiv_institution_lookback_days}d)"
    )
    return papers


# ── Backwards-compat shim (kept so main.py's older call-site still imports) ─

def fetch_arxiv_cs_ro(
    keywords: list[str],
    *,
    lookback_days: int = 90,
    max_results: int = 200,
) -> list[Paper]:
    """Legacy entry point: cs.RO-only keyword sweep. Prefer :func:`fetch_arxiv_all`."""
    return fetch_arxiv_keyword_sweep(
        ["cs.RO"],
        keywords,
        lookback_days=lookback_days,
        max_results=max_results,
    )

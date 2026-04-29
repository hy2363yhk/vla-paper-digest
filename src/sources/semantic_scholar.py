"""Semantic Scholar Graph API v1 client.

Public entry points used by the pipeline:
    - :class:`SemanticScholarClient` — thin wrapper with rate-limit aware retries
    - :func:`bulk_search_by_venues` — used by the bootstrap step
    - :func:`lookup_papers_by_title` — used by the classic-paper whitelist loader

Design notes
------------
- Semantic Scholar is famously rate-limited. We treat HTTP 429 specially and
  sleep for 60 seconds before the next retry (the public window is 1 req/sec
  without a key, or 1 req/sec per IP with a free key; the API however returns
  bursty 429s even under the declared quota).
- All network calls go through tenacity. Non-429 network failures use
  exponential backoff.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models import Paper
from src.utils import now_utc, parse_date, title_jaccard

API_BASE = "https://api.semanticscholar.org/graph/v1"

DEFAULT_FIELDS = (
    "paperId,title,abstract,venue,year,publicationDate,"
    "citationCount,influentialCitationCount,"
    "authors.name,authors.affiliations,externalIds"
)


class RateLimited(Exception):
    """Raised when Semantic Scholar returns 429 so tenacity can sleep specifically."""


class UpstreamServerError(Exception):
    """Raised for 5xx — retryable."""


class NotFound(Exception):
    """Raised for 404 — NOT retryable (propagated up as a clean signal)."""


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        f"Semantic Scholar retry #{retry_state.attempt_number} after error: {exc}"
    )


class SemanticScholarClient:
    """Small synchronous client around the Semantic Scholar Graph API."""

    def __init__(self, api_key: str | None, timeout: float = 30.0) -> None:
        self.api_key = api_key
        headers = {"User-Agent": "vla-paper-digest/0.1"}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SemanticScholarClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── Low-level request with tenacity ──────────────────────────────────
    # Only retry on transient errors: 429, 5xx, and network-level failures
    # (httpx.TransportError). 4xx is considered terminal.
    @retry(
        retry=retry_if_exception_type(
            (RateLimited, UpstreamServerError, httpx.TransportError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=_log_retry,
        reraise=True,
    )
    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{API_BASE}{path}"
        resp = self._client.get(url, params=params)
        if resp.status_code == 429:
            logger.warning("Semantic Scholar 429 rate limited; sleeping 60s before retry.")
            time.sleep(60)
            raise RateLimited("429 Too Many Requests")
        if resp.status_code == 404:
            raise NotFound(f"404 Not Found: {url}")
        if resp.status_code >= 500:
            raise UpstreamServerError(f"upstream {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    # ── High-level helpers ───────────────────────────────────────────────
    def search_by_query(
        self,
        query: str,
        *,
        year_from: int | None = None,
        venue: str | None = None,
        limit: int = 100,
        max_pages: int = 5,
    ) -> list[Paper]:
        """Paginate `/paper/search` and return parsed :class:`Paper` objects."""

        offset = 0
        out: list[Paper] = []
        for page in range(max_pages):
            params: dict[str, Any] = {
                "query": query,
                "offset": offset,
                "limit": limit,
                "fields": DEFAULT_FIELDS,
            }
            if year_from:
                params["year"] = f"{year_from}-"
            if venue:
                params["venue"] = venue
            try:
                data = self._request("/paper/search", params=params)
            except NotFound:
                break  # no results for this query
            except (httpx.HTTPError, UpstreamServerError, RateLimited) as exc:
                logger.error(f"SS search_by_query failed on page {page}: {exc}")
                break
            batch = data.get("data") or []
            for raw in batch:
                paper = _to_paper(raw)
                if paper:
                    out.append(paper)
            next_offset = data.get("next")
            if next_offset is None or not batch:
                break
            offset = next_offset
            time.sleep(1.0)  # polite pacing; prevents bursty 429s
        return out

    def get_paper_by_id(self, paper_id: str) -> Paper | None:
        try:
            data = self._request(f"/paper/{paper_id}", params={"fields": DEFAULT_FIELDS})
        except NotFound:
            return None
        except (httpx.HTTPError, UpstreamServerError, RateLimited) as exc:
            logger.warning(f"SS get_paper_by_id({paper_id}) failed: {exc}")
            return None
        return _to_paper(data)

    def match_by_title(self, title: str) -> Paper | None:
        """Use the `/paper/search/match` endpoint for best-effort title match.

        A 404 from this endpoint is normal (no match) and is returned as ``None``
        without warnings or retries.
        """
        try:
            data = self._request(
                "/paper/search/match",
                params={"query": title, "fields": DEFAULT_FIELDS},
            )
        except NotFound:
            return None
        except (httpx.HTTPError, UpstreamServerError, RateLimited) as exc:
            logger.warning(f"SS match_by_title failed for '{title[:60]}': {exc}")
            return None
        candidates = data.get("data") or []
        if not candidates:
            return None
        raw = candidates[0]
        candidate_title = raw.get("title") or ""
        # Guard against wildly different matches — the endpoint occasionally
        # returns high-ranked but unrelated results.
        if title_jaccard(title, candidate_title) < 0.4:
            logger.warning(
                f"Low-confidence match: query='{title[:60]}' got='{candidate_title[:60]}'"
            )
        return _to_paper(raw)


def _to_paper(raw: dict | None) -> Paper | None:
    if not raw:
        return None
    paper_id = raw.get("paperId")
    title = raw.get("title")
    if not paper_id or not title:
        return None
    externals = raw.get("externalIds") or {}
    arxiv_id = externals.get("ArXiv") or externals.get("arXiv")
    authors_raw = raw.get("authors") or []
    authors = [a.get("name") for a in authors_raw if a and a.get("name")]
    # Affiliations: SS returns ``authors[i].affiliations`` as a list of strings.
    # We keep the order (first author first) and dedupe while preserving order,
    # then cap at 3 to avoid noisy cards when there are many institutions.
    affiliations: list[str] = []
    seen_aff: set[str] = set()
    for a in authors_raw:
        for aff in (a or {}).get("affiliations") or []:
            norm = (aff or "").strip()
            if not norm:
                continue
            if norm.lower() in seen_aff:
                continue
            seen_aff.add(norm.lower())
            affiliations.append(norm)
            if len(affiliations) >= 3:
                break
        if len(affiliations) >= 3:
            break
    pub_date = parse_date(raw.get("publicationDate")) or (
        parse_date(f"{raw.get('year')}-01-01") if raw.get("year") else None
    )
    return Paper(
        paper_id=paper_id,
        title=title.strip(),
        abstract=(raw.get("abstract") or "").strip(),
        authors=authors,
        affiliations=affiliations,
        venue=(raw.get("venue") or "").strip(),
        year=raw.get("year"),
        publication_date=pub_date,
        citation_count=int(raw.get("citationCount") or 0),
        influential_citation_count=int(raw.get("influentialCitationCount") or 0),
        arxiv_id=arxiv_id,
        external_url=f"https://www.semanticscholar.org/paper/{paper_id}",
        source="semantic_scholar",
        first_seen_at=now_utc(),
    )


def bulk_search_by_venues(
    client: SemanticScholarClient,
    venues: Iterable[str],
    *,
    year_from: int,
    query: str = "robot",
) -> list[Paper]:
    """Bootstrap helper: pull recent papers for each target venue.

    We pass a broad ``robot`` seed query because SS ``/paper/search`` requires a
    non-empty query even when a venue filter is set. Relevance scoring happens
    later — here we cast a wide net.
    """

    seen: dict[str, Paper] = {}
    for venue in venues:
        logger.info(f"[SS] searching venue='{venue}' year>={year_from} ...")
        papers = client.search_by_query(
            query=query, year_from=year_from, venue=venue, max_pages=5
        )
        logger.info(f"    → {len(papers)} raw results")
        for p in papers:
            seen.setdefault(p.paper_id, p)
    return list(seen.values())


def lookup_papers_by_title(
    client: SemanticScholarClient, titles: Iterable[str]
) -> dict[str, Paper]:
    """Best-effort paper lookup by title for the classic-paper whitelist."""

    out: dict[str, Paper] = {}
    for title in titles:
        paper = client.match_by_title(title)
        if paper is None:
            logger.warning(f"Classic paper not found on Semantic Scholar: '{title}'")
            continue
        out[title] = paper
        time.sleep(1.0)
    return out

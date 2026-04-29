"""Hugging Face Daily Papers source.

The HF Daily Papers feed is a human-curated top-N list of that day's notable
arXiv papers. We treat it as a "hot signal" input: the papers are already on
arXiv (so ``fetch_arxiv_all`` may also have them), but HF's feed surfaces
industry releases (NVIDIA, DeepSeek, Meta, Google) that can otherwise take a
few days to propagate through Semantic Scholar.

We fetch ~14 days back and trust the feed's own recency ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger

from src.models import Paper
from src.utils import now_utc

HF_DAILY_API = "https://huggingface.co/api/daily_papers"


def fetch_hf_daily_papers(
    *,
    lookback_days: int = 14,
    max_results: int = 60,
) -> list[Paper]:
    """Pull HF Daily Papers within the lookback window."""
    cutoff = now_utc() - timedelta(days=lookback_days)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(HF_DAILY_API)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"HF Daily Papers fetch failed: {exc}")
        return []

    papers: list[Paper] = []
    seen: set[str] = set()
    for item in data[:max_results] if isinstance(data, list) else []:
        paper_info = item.get("paper", {}) if isinstance(item, dict) else {}
        title = str(paper_info.get("title", "")).replace("\n", " ").strip()
        abstract = str(paper_info.get("summary", "")).replace("\n", " ").strip()
        arxiv_id = paper_info.get("id") or None
        if not title or not arxiv_id:
            continue
        if arxiv_id in seen:
            continue
        seen.add(arxiv_id)

        pub_str = paper_info.get("publishedAt") or ""
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pub_dt = None
        if pub_dt and pub_dt < cutoff:
            continue

        authors: list[str] = []
        for au in paper_info.get("authors") or []:
            name = au.get("name") if isinstance(au, dict) else None
            if name:
                authors.append(name)

        pub_date = pub_dt.astimezone(timezone.utc).date() if pub_dt else None
        papers.append(
            Paper(
                paper_id=f"arxiv:{arxiv_id}",
                title=title,
                abstract=abstract,
                authors=authors[:10],
                venue="arXiv (HF Daily)",
                year=pub_date.year if pub_date else None,
                publication_date=pub_date,
                citation_count=0,
                influential_citation_count=0,
                arxiv_id=arxiv_id,
                external_url=f"https://arxiv.org/abs/{arxiv_id}",
                source="hf_daily",
                first_seen_at=now_utc(),
            )
        )
    logger.info(f"[HF Daily] {len(papers)} papers in last {lookback_days}d")
    return papers

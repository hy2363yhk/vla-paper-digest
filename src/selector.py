"""Daily selection.

Current rule (simple rerank):
    * ``top_ranked`` × ``top_ranked_count`` (default 2): highest composite
      score among unpushed, non-classic papers.
    * ``classic`` × 1: next paper from the whitelist round-robin pointer.

The old three-bucket recipe (``latest_hot`` / ``top_cited`` / ``classic``) is
preserved as ``pick_latest_hot`` / ``pick_top_cited`` below but is no longer
wired into :func:`pick_daily_three`. The legacy history-entry bucket values
(``"latest_hot"``, ``"top_cited"``) are still accepted by the ``Bucket``
``Literal`` so old ``history.json`` files keep validating.
"""

from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.config import SelectorThresholds
from src.models import (
    Bucket,
    HistoryEntry,
    Paper,
    RotationState,
    ScoredPaper,
    SelectedPaper,
)
from src.storage import pushed_paper_ids
from src.utils import today_utc

BUCKET_LABELS_CN: dict[Bucket, str] = {
    "top_ranked": "综合高分",
    "latest_hot": "最新高相关",
    "top_cited": "近两年高引",
    "classic": "经典轮播",
}


def _to_selected(scored: ScoredPaper, bucket: Bucket) -> SelectedPaper:
    return SelectedPaper(
        paper=scored.paper,
        scores=scored.scores,
        bucket=bucket,
        bucket_label_cn=BUCKET_LABELS_CN[bucket],
        lab_key=scored.lab_key,
        lab_label=scored.lab_label,
        notable_author_matches=scored.notable_author_matches,
    )


# ── New primary selector: top-N by composite score ─────────────────────────
def pick_top_ranked(
    scored: list[ScoredPaper],
    *,
    excluded: set[str],
    count: int,
    min_composite: float,
    max_per_lab: int = 0,
) -> list[SelectedPaper]:
    """Return the ``count`` highest-composite unpushed non-classic papers.

    When ``max_per_lab > 0`` we enforce a diversity cap so that a single lab
    (e.g. Sergey Levine's group on a prolific day) cannot occupy every slot.
    Papers without any lab tag are allowed through unconstrained.
    """

    candidates = [
        s
        for s in scored
        if s.paper.paper_id not in excluded
        and not s.paper.is_classic
        and s.scores.composite >= min_composite
    ]
    candidates.sort(key=lambda s: s.scores.composite, reverse=True)
    if not candidates:
        logger.warning(
            f"[selector] 'top_ranked' empty: no paper with composite >= {min_composite}"
        )
        return []

    picked: list[ScoredPaper] = []
    per_lab: dict[str, int] = {}
    for s in candidates:
        if len(picked) >= count:
            break
        if max_per_lab > 0 and s.lab_key:
            if per_lab.get(s.lab_key, 0) >= max_per_lab:
                continue
        picked.append(s)
        if s.lab_key:
            per_lab[s.lab_key] = per_lab.get(s.lab_key, 0) + 1

    if len(picked) < count:
        # Diversity cap may have over-constrained us; backfill without the cap.
        leftover = [s for s in candidates if s not in picked]
        for s in leftover:
            if len(picked) >= count:
                break
            picked.append(s)

    return [_to_selected(s, "top_ranked") for s in picked]


# ── Classic round-robin (unchanged) ────────────────────────────────────────
def pick_classic(
    db: dict[str, Paper],
    classic_whitelist: list[dict],
    *,
    excluded: set[str],
    rotation: RotationState,
    scored_by_id: dict[str, ScoredPaper],
) -> tuple[SelectedPaper | None, RotationState]:
    """Advance the whitelist pointer until we find an unpushed, resolved entry."""

    n = len(classic_whitelist)
    if n == 0:
        logger.warning("[selector] 'classic' empty: whitelist is empty")
        return None, rotation

    start_index = rotation.next_index % n
    for step in range(n):
        idx = (start_index + step) % n
        entry = classic_whitelist[idx]
        paper_id = entry.get("paper_id")
        title = entry.get("title", "")
        if not paper_id:
            logger.warning(
                f"[selector] classic[{idx}] '{title}' has no paper_id yet; skip"
            )
            continue
        if paper_id in excluded:
            continue
        scored = scored_by_id.get(paper_id)
        if scored is None:
            paper = db.get(paper_id)
            if paper is None:
                logger.warning(
                    f"[selector] classic[{idx}] paper_id={paper_id} not in DB; skip"
                )
                continue
            from src.scoring import score_papers

            scored = score_papers([paper])[0]
        selected = _to_selected(scored, "classic")
        new_state = RotationState(next_index=(idx + 1) % n)
        return selected, new_state

    logger.warning("[selector] 'classic' empty: all whitelist entries already pushed")
    return None, rotation


# ── Top-level orchestrator ─────────────────────────────────────────────────
def pick_daily_three(
    scored_papers: list[ScoredPaper],
    db: dict[str, Paper],
    classic_whitelist: list[dict],
    history: list[HistoryEntry],
    rotation: RotationState,
    thresholds: SelectorThresholds,
) -> tuple[list[SelectedPaper], RotationState]:
    """Select today's papers: top-N by composite + 1 classic.

    Function name kept for historical reasons; with default thresholds this
    returns up to ``top_ranked_count + 1`` papers (i.e. 3 when count=2).
    """

    already = pushed_paper_ids(history)
    scored_by_id = {s.paper.paper_id: s for s in scored_papers}

    picked: list[SelectedPaper] = []
    excluded = set(already)

    top = pick_top_ranked(
        scored_papers,
        excluded=excluded,
        count=thresholds.top_ranked_count,
        min_composite=thresholds.top_ranked_min_composite,
        max_per_lab=thresholds.max_per_lab,
    )
    for sp in top:
        picked.append(sp)
        excluded.add(sp.paper.paper_id)

    classic, rotation = pick_classic(
        db,
        classic_whitelist,
        excluded=excluded,
        rotation=rotation,
        scored_by_id=scored_by_id,
    )
    if classic:
        picked.append(classic)

    logger.info(
        f"[selector] picked {len(picked)} papers: {[p.bucket for p in picked]}"
    )
    return picked, rotation


# ── Legacy selectors (unused by default, kept for reference / future) ──────
def pick_latest_hot(
    scored: list[ScoredPaper],
    *,
    excluded: set[str],
    thresholds: SelectorThresholds,
) -> SelectedPaper | None:
    today = today_utc()
    cutoff = today - timedelta(days=thresholds.latest_hot_days)
    candidates = [
        s
        for s in scored
        if s.paper.paper_id not in excluded
        and s.paper.publication_date is not None
        and s.paper.publication_date >= cutoff
        and s.scores.composite >= thresholds.latest_hot_min_composite
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda s: s.scores.composite)
    return _to_selected(best, "latest_hot")


def pick_top_cited(
    scored: list[ScoredPaper],
    *,
    excluded: set[str],
    thresholds: SelectorThresholds,
) -> SelectedPaper | None:
    today = today_utc()
    year_cutoff = today - timedelta(days=thresholds.lookback_years * 365)
    pool = [
        s
        for s in scored
        if s.paper.paper_id not in excluded
        and s.paper.publication_date is not None
        and s.paper.publication_date >= year_cutoff
        and s.paper.citation_count > 0
    ]
    if not pool:
        return None
    citations = sorted([s.paper.citation_count for s in pool])
    q_idx = max(int(len(citations) * thresholds.top_cited_percentile) - 1, 0)
    threshold_cite = citations[q_idx]
    candidates = [
        s
        for s in pool
        if s.paper.citation_count >= threshold_cite
        and s.scores.relevance >= thresholds.top_cited_min_relevance
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda s: (s.scores.relevance, s.paper.citation_count))
    return _to_selected(best, "top_cited")

"""Unit tests for the (rerank) selector: top-N by composite + 1 classic."""

from __future__ import annotations

from datetime import date, timedelta

from src.config import SelectorThresholds
from src.models import (
    ComponentScores,
    HistoryEntry,
    Paper,
    RotationState,
    ScoredPaper,
)
from src.selector import pick_daily_three, pick_top_ranked
from src.utils import today_utc

TODAY = today_utc()


def _scored(
    *,
    paper_id: str,
    title: str,
    composite: float,
    relevance: float = 5.0,
    pub_date: date | None = None,
    citations: int = 0,
    venue: str = "CoRL",
    is_classic: bool = False,
) -> ScoredPaper:
    return ScoredPaper(
        paper=Paper(
            paper_id=paper_id,
            title=title,
            venue=venue,
            year=(pub_date.year if pub_date else None),
            publication_date=pub_date,
            citation_count=citations,
            is_classic=is_classic,
        ),
        scores=ComponentScores(
            relevance=relevance,
            venue=8.0,
            freshness=8.0,
            velocity=5.0,
            composite=composite,
        ),
    )


def _thr(**kwargs) -> SelectorThresholds:
    # Tests were designed around a 2 + 1 selection size; the production default
    # now lifts this to 4 + 1. We pin count=2 here so expectations stay stable.
    kwargs.setdefault("top_ranked_count", 2)
    return SelectorThresholds(**kwargs)


# ── pick_top_ranked ─────────────────────────────────────────────────────────
def test_top_ranked_picks_two_by_composite():
    scored = [
        _scored(paper_id="a", title="A", composite=5.0, pub_date=TODAY),
        _scored(paper_id="b", title="B", composite=8.5, pub_date=TODAY),
        _scored(paper_id="c", title="C", composite=3.0, pub_date=TODAY),
        _scored(paper_id="d", title="D", composite=7.0, pub_date=TODAY),
    ]
    picks = pick_top_ranked(scored, excluded=set(), count=2, min_composite=2.0)
    assert [p.paper.paper_id for p in picks] == ["b", "d"]
    assert all(p.bucket == "top_ranked" for p in picks)


def test_top_ranked_excludes_classics():
    scored = [
        _scored(paper_id="a", title="A", composite=9.0, pub_date=TODAY, is_classic=True),
        _scored(paper_id="b", title="B", composite=6.0, pub_date=TODAY),
    ]
    picks = pick_top_ranked(scored, excluded=set(), count=2, min_composite=0.0)
    ids = [p.paper.paper_id for p in picks]
    assert ids == ["b"]  # classic is skipped here — chosen by pick_classic separately


def test_top_ranked_respects_min_composite_floor():
    scored = [_scored(paper_id="a", title="A", composite=1.5, pub_date=TODAY)]
    picks = pick_top_ranked(scored, excluded=set(), count=2, min_composite=2.0)
    assert picks == []


def test_top_ranked_honors_excluded():
    scored = [
        _scored(paper_id="a", title="A", composite=9.0, pub_date=TODAY),
        _scored(paper_id="b", title="B", composite=8.0, pub_date=TODAY),
    ]
    picks = pick_top_ranked(
        scored, excluded={"a"}, count=2, min_composite=0.0
    )
    assert [p.paper.paper_id for p in picks] == ["b"]


# ── pick_daily_three (orchestrator) ────────────────────────────────────────
def test_daily_three_returns_two_top_plus_one_classic():
    scored = [
        _scored(paper_id="p1", title="P1", composite=8.0, pub_date=TODAY),
        _scored(paper_id="p2", title="P2", composite=7.0, pub_date=TODAY),
        _scored(paper_id="p3", title="P3", composite=6.0, pub_date=TODAY),
    ]
    db = {s.paper.paper_id: s.paper for s in scored}
    db["cx"] = Paper(
        paper_id="cx", title="Classic X", venue="NeurIPS", year=2023,
        publication_date=date(2023, 1, 1), citation_count=200, is_classic=True,
    )
    whitelist = [{"title": "Classic X", "paper_id": "cx", "category": "foo"}]

    picks, new_rot = pick_daily_three(
        scored_papers=scored,
        db=db,
        classic_whitelist=whitelist,
        history=[],
        rotation=RotationState(),
        thresholds=_thr(),
    )
    buckets = [p.bucket for p in picks]
    assert buckets.count("top_ranked") == 2
    assert buckets.count("classic") == 1
    ids = [p.paper.paper_id for p in picks]
    assert ids[0] == "p1"   # highest composite first
    assert ids[1] == "p2"
    assert ids[2] == "cx"
    assert new_rot.next_index == 0  # (0 + 1) % 1


def test_daily_three_skips_pushed_papers():
    scored = [
        _scored(paper_id="pushed", title="Already", composite=9.5, pub_date=TODAY),
        _scored(paper_id="fresh1", title="Fresh1", composite=8.5, pub_date=TODAY),
        _scored(paper_id="fresh2", title="Fresh2", composite=7.5, pub_date=TODAY),
    ]
    db = {s.paper.paper_id: s.paper for s in scored}
    history = [
        HistoryEntry(
            paper_id="pushed",
            pushed_on=TODAY - timedelta(days=1),
            bucket="top_ranked",
            title="Already",
        )
    ]
    picks, _ = pick_daily_three(
        scored_papers=scored,
        db=db,
        classic_whitelist=[],
        history=history,
        rotation=RotationState(),
        thresholds=_thr(),
    )
    ids = [p.paper.paper_id for p in picks]
    assert "pushed" not in ids
    assert ids[:2] == ["fresh1", "fresh2"]


def test_daily_three_accepts_legacy_history_buckets():
    # Old history.json may contain `latest_hot` / `top_cited` bucket names;
    # the selector should still dedupe against them.
    scored = [
        _scored(paper_id="old_hot", title="Old Hot", composite=9.0, pub_date=TODAY),
        _scored(paper_id="new1", title="New1", composite=8.0, pub_date=TODAY),
        _scored(paper_id="new2", title="New2", composite=7.0, pub_date=TODAY),
    ]
    db = {s.paper.paper_id: s.paper for s in scored}
    history = [
        HistoryEntry(
            paper_id="old_hot",
            pushed_on=TODAY - timedelta(days=7),
            bucket="latest_hot",
            title="Old Hot",
        )
    ]
    picks, _ = pick_daily_three(
        scored_papers=scored,
        db=db,
        classic_whitelist=[],
        history=history,
        rotation=RotationState(),
        thresholds=_thr(),
    )
    ids = [p.paper.paper_id for p in picks]
    assert "old_hot" not in ids


def test_daily_three_empty_when_nothing_above_threshold():
    scored = [_scored(paper_id="a", title="A", composite=0.5, pub_date=TODAY)]
    picks, _ = pick_daily_three(
        scored_papers=scored,
        db={},
        classic_whitelist=[],
        history=[],
        rotation=RotationState(),
        thresholds=_thr(),
    )
    assert picks == []


# ── Classic rotation (unchanged behaviour) ─────────────────────────────────
def test_classic_rotation_advances_and_wraps():
    db: dict[str, Paper] = {}
    for i in range(3):
        pid = f"c{i}"
        db[pid] = Paper(
            paper_id=pid, title=f"Classic {i}", venue="ICLR", year=2023,
            publication_date=date(2023, 1, 1), citation_count=50, is_classic=True,
        )
    whitelist = [{"title": f"Classic {i}", "paper_id": f"c{i}"} for i in range(3)]

    _, new_rot = pick_daily_three(
        scored_papers=[],
        db=db,
        classic_whitelist=whitelist,
        history=[],
        rotation=RotationState(next_index=2),
        thresholds=_thr(),
    )
    assert new_rot.next_index == 0  # wraps from 2 → 0


def test_classic_skips_unresolved_entries():
    db: dict[str, Paper] = {
        "c1": Paper(
            paper_id="c1", title="Resolved", venue="NeurIPS", year=2023,
            publication_date=date(2023, 1, 1), citation_count=10, is_classic=True,
        )
    }
    whitelist = [
        {"title": "Unresolved classic"},              # no paper_id → skip
        {"title": "Resolved", "paper_id": "c1"},
    ]
    picks, new_rot = pick_daily_three(
        scored_papers=[],
        db=db,
        classic_whitelist=whitelist,
        history=[],
        rotation=RotationState(next_index=0),
        thresholds=_thr(),
    )
    classic_picks = [p for p in picks if p.bucket == "classic"]
    assert len(classic_picks) == 1
    assert classic_picks[0].paper.paper_id == "c1"
    assert new_rot.next_index == 0  # picked index 1 → (1+1) % 2 = 0

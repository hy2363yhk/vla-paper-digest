"""Unit tests for the scoring pipeline."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.config import COMPOSITE_WEIGHTS, FRESHNESS_BUCKETS, FRESHNESS_FLOOR
from src.models import Paper
from src.scoring import (
    citation_velocity_score,
    composite_score,
    freshness_score,
    relevance_score,
    score_papers,
    venue_score,
)

REF_DAY = date(2026, 1, 1)


def _mkpaper(
    *,
    title: str = "t",
    abstract: str = "",
    venue: str = "",
    pub_date: date | None = None,
    citations: int = 0,
) -> Paper:
    return Paper(
        paper_id=f"pid-{title[:20]}",
        title=title,
        abstract=abstract,
        venue=venue,
        year=pub_date.year if pub_date else None,
        publication_date=pub_date,
        citation_count=citations,
    )


# ── relevance ────────────────────────────────────────────────────────────
def test_relevance_empty_text_is_zero():
    score, hits = relevance_score("", "")
    assert score == 0.0
    assert all(v == 0 for v in hits.values())


def test_relevance_single_category_respects_weight():
    # One hit in dynamics (weight 3.0); contribution = 3.0 / 20.0 * 10 = 1.5
    score, hits = relevance_score("paper on jerk minimisation", "")
    assert hits["dynamics_smoothness"] == 1
    assert score == pytest.approx(1.5, abs=0.01)


def test_relevance_multi_category_accumulates():
    text_title = "jerk-aware action chunk VLA diffusion policy"
    score, hits = relevance_score(text_title, "")
    assert hits["dynamics_smoothness"] >= 1
    assert hits["semantic_smoothness"] >= 1
    assert hits["vla_carrier"] >= 1
    assert score > 3.0  # significantly above a single-hit paper


def test_relevance_discount_within_same_category():
    # 4 distinct dynamics terms should apply 1.0/0.5/0.3/0.1 discounts
    text = "jerk chattering smooth trajectory action smoothness"
    score_multi, hits = relevance_score(text, "")
    assert hits["dynamics_smoothness"] == 4
    expected_sum = 3.0 * (1.0 + 0.5 + 0.3 + 0.1)
    expected = min(expected_sum, 20.0) / 20.0 * 10.0
    assert score_multi == pytest.approx(expected, abs=0.01)


def test_relevance_case_insensitive():
    score_upper, _ = relevance_score("JERK analysis of VLA", "")
    score_lower, _ = relevance_score("jerk analysis of vla", "")
    assert score_upper == pytest.approx(score_lower)


# ── venue ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "venue,expected",
    [
        ("CoRL", 10.0),
        ("Conference on Robot Learning", 10.0),
        ("RSS 2024", 10.0),
        ("NeurIPS 2024", 9.0),
        ("ICRA", 8.0),
        ("ICLR 2025", 8.0),
        ("arXiv", 4.0),
        ("", 2.0),
        ("Unknown Workshop", 2.0),
    ],
)
def test_venue_score(venue, expected):
    assert venue_score(venue) == pytest.approx(expected)


# ── freshness ────────────────────────────────────────────────────────────
def test_freshness_none_date_is_floor():
    assert freshness_score(None, REF_DAY) == FRESHNESS_FLOOR


@pytest.mark.parametrize(
    "age_days,expected",
    [
        (0, 10.0),
        (15, 10.0),
        (30, 10.0),
        (31, 8.0),
        (89, 8.0),
        (91, 6.0),
        (150, 6.0),
        (200, 4.0),
        (400, 2.0),
        (800, FRESHNESS_FLOOR),
    ],
)
def test_freshness_buckets(age_days, expected):
    pub = REF_DAY - timedelta(days=age_days)
    assert freshness_score(pub, REF_DAY) == pytest.approx(expected)


def test_freshness_buckets_are_monotone():
    values = [s for _, s in FRESHNESS_BUCKETS]
    assert values == sorted(values, reverse=True)


# ── velocity ─────────────────────────────────────────────────────────────
def test_velocity_zero_citations_is_zero():
    paper = _mkpaper(pub_date=REF_DAY - timedelta(days=60), citations=0)
    assert citation_velocity_score(paper, reference=REF_DAY) == 0.0


def test_velocity_higher_cites_higher_score():
    p_low = _mkpaper(pub_date=REF_DAY - timedelta(days=60), citations=1)
    p_hi = _mkpaper(pub_date=REF_DAY - timedelta(days=60), citations=40)
    assert citation_velocity_score(p_hi, reference=REF_DAY) > citation_velocity_score(
        p_low, reference=REF_DAY
    )


def test_velocity_caps_at_ten():
    paper = _mkpaper(pub_date=REF_DAY - timedelta(days=30), citations=10000)
    assert citation_velocity_score(paper, reference=REF_DAY) <= 10.0


# ── composite ────────────────────────────────────────────────────────────
def test_composite_formula():
    comp = composite_score(relevance=8.0, venue=10.0, freshness=6.0, velocity=4.0)
    expected = (
        8.0 * COMPOSITE_WEIGHTS["relevance"]
        + 10.0 * COMPOSITE_WEIGHTS["venue"]
        + 6.0 * COMPOSITE_WEIGHTS["freshness"]
        + 4.0 * COMPOSITE_WEIGHTS["velocity"]
    )
    assert comp == pytest.approx(expected)


def test_score_papers_end_to_end():
    papers = [
        _mkpaper(
            title="Jerk-aware diffusion policy for bimanual VLA",
            abstract="We propose temporal consistency with action chunking...",
            venue="CoRL",
            pub_date=REF_DAY - timedelta(days=45),
            citations=12,
        ),
        _mkpaper(
            title="An unrelated robotics paper",
            abstract="motion planning via RRT",
            venue="",
            pub_date=REF_DAY - timedelta(days=800),
            citations=0,
        ),
    ]
    scored = score_papers(papers, reference=REF_DAY)
    assert scored[0].scores.composite > scored[1].scores.composite
    assert scored[0].scores.relevance > 3.0
    assert scored[1].scores.relevance < 1.0

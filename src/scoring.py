"""Paper scoring: relevance / venue / freshness / citation velocity / composite.

All four components are normalised to the ``[0, 10]`` range so the composite
formula in :data:`~src.config.COMPOSITE_WEIGHTS` is easy to reason about.

The relevance sub-score intentionally uses a hit-count with discount factors
(1.0, 0.5, 0.3, 0.1) instead of TF-IDF: the keyword library is small and
hand-curated, so the model should reward diversity across categories rather
than amplify repeated mentions of the same term.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

from src.config import (
    COMPOSITE_WEIGHTS,
    FRESHNESS_BUCKETS,
    FRESHNESS_FLOOR,
    KEYWORDS,
    RELEVANCE_HIT_DISCOUNTS,
    RELEVANCE_SCORE_CAP,
    VENUE_WEIGHTS,
)
from src.config_labs import LabsConfig, load_labs_config
from src.models import ComponentScores, Paper, ScoredPaper
from src.utils import today_utc


def _count_hits(text: str, terms: list[str]) -> int:
    """Case-insensitive count of how many distinct ``terms`` appear in ``text``.

    We count each term at most once to avoid noisy abstracts inflating the score.
    """

    lower = text.lower()
    hits = 0
    for term in terms:
        if term.lower() in lower:
            hits += 1
    return hits


def relevance_score(title: str, abstract: str) -> tuple[float, dict[str, int]]:
    """Return the relevance score in [0, 10] plus per-category hit counts."""

    text = f"{title}\n{abstract}"
    weighted_sum = 0.0
    hits_by_cat: dict[str, int] = {}
    for cat, spec in KEYWORDS.items():
        weight: float = spec["weight"]
        terms: list[str] = spec["terms"]
        hits = _count_hits(text, terms)
        hits_by_cat[cat] = hits
        for i in range(hits):
            discount = (
                RELEVANCE_HIT_DISCOUNTS[i]
                if i < len(RELEVANCE_HIT_DISCOUNTS)
                else RELEVANCE_HIT_DISCOUNTS[-1]
            )
            weighted_sum += weight * discount
    normalised = min(weighted_sum, RELEVANCE_SCORE_CAP) / RELEVANCE_SCORE_CAP * 10.0
    return normalised, hits_by_cat


def venue_score(venue: str) -> float:
    """Map a venue string to a score in [0, 10]. Unknown venues score 2."""

    if not venue:
        return 2.0
    v_lower = venue.lower()
    best = 0.0
    for key, weight in VENUE_WEIGHTS.items():
        if key.lower() in v_lower:
            best = max(best, float(weight))
    return best if best > 0 else 2.0


def freshness_score(publication_date: date | None, reference: date | None = None) -> float:
    """Map an age in days to a stair-step freshness score in [1, 10]."""

    if publication_date is None:
        return FRESHNESS_FLOOR
    ref = reference or today_utc()
    age_days = max((ref - publication_date).days, 0)
    for bound, score in FRESHNESS_BUCKETS:
        if age_days <= bound:
            return float(score)
    return FRESHNESS_FLOOR


def _months_since(publication_date: date | None, reference: date | None = None) -> float:
    if publication_date is None:
        return 1.0
    ref = reference or today_utc()
    days = max((ref - publication_date).days, 1)
    return days / 30.0


def citation_velocity_score(
    paper: Paper,
    *,
    reference: date | None = None,
    p95_velocity: float | None = None,
) -> float:
    """Citations per month (log-compressed), normalised against the corpus P95."""

    months = max(_months_since(paper.publication_date, reference), 1.0)
    raw = paper.citation_count / months
    compressed = math.log1p(max(raw, 0.0))
    # If we don't know the corpus-level P95 yet (e.g. test suite), fall back to
    # a reasonable constant that treats ~10 citations/month as "very fast".
    ceiling = math.log1p(p95_velocity) if p95_velocity and p95_velocity > 0 else math.log1p(10.0)
    if ceiling <= 0:
        return 0.0
    return min(compressed / ceiling, 1.0) * 10.0


def composite_score(
    relevance: float, venue: float, freshness: float, velocity: float
) -> float:
    return (
        relevance * COMPOSITE_WEIGHTS["relevance"]
        + venue * COMPOSITE_WEIGHTS["venue"]
        + freshness * COMPOSITE_WEIGHTS["freshness"]
        + velocity * COMPOSITE_WEIGHTS["velocity"]
    )


def _compute_p95_velocity(papers: Iterable[Paper], reference: date | None = None) -> float:
    velocities: list[float] = []
    for p in papers:
        months = max(_months_since(p.publication_date, reference), 1.0)
        velocities.append(p.citation_count / months)
    velocities.sort()
    if not velocities:
        return 10.0
    idx = min(int(len(velocities) * 0.95), len(velocities) - 1)
    return max(velocities[idx], 1.0)


def _compute_lab_boost(
    paper: Paper, labs_cfg: LabsConfig
) -> tuple[float, float, str | None, str | None, list[str]]:
    """Compute (lab_boost, notable_boost, lab_key, lab_label, notable_matches).

    Lab boost is the **raw composite delta** we add on top of the weighted
    sum. Tier-1 flagship labs (weight=1.0) get ``lab_boost_base`` (default 2.0)
    on top of a 0-10 composite; that's ~20% lift — meaningful enough to bump a
    medium-relevance DeepSeek/NVIDIA preprint above generic high-citation noise,
    but still bounded so off-topic lab papers can't dominate.

    The rule is additive but mutually-exclusive: only the *best matching* lab
    contributes. Notable-author boost stacks on top (smaller).
    """
    lab = labs_cfg.match_lab(paper.authors, paper.title, paper.arxiv_id)
    lab_boost = labs_cfg.lab_boost_base * lab.weight if lab else 0.0
    notable_matches = labs_cfg.match_notable_authors(paper.authors)
    notable_boost = labs_cfg.notable_author_boost if notable_matches else 0.0
    return (
        lab_boost,
        notable_boost,
        lab.key if lab else None,
        lab.label if lab else None,
        notable_matches,
    )


def score_papers(
    papers: list[Paper],
    *,
    reference: date | None = None,
    labs_cfg: LabsConfig | None = None,
) -> list[ScoredPaper]:
    """Score a batch of papers; citation velocity uses corpus-level P95.

    When ``labs_cfg`` is provided (or loaded by default) the composite score
    additionally absorbs a **lab boost** — see :func:`_compute_lab_boost`.
    """

    ref = reference or today_utc()
    cfg = labs_cfg or load_labs_config()
    p95 = _compute_p95_velocity(papers, ref)
    out: list[ScoredPaper] = []
    for paper in papers:
        rel, hits = relevance_score(paper.title, paper.abstract)
        ven = venue_score(paper.venue)
        fresh = freshness_score(paper.publication_date, ref)
        vel = citation_velocity_score(paper, reference=ref, p95_velocity=p95)
        base = composite_score(rel, ven, fresh, vel)
        lab_boost, notable_boost, lab_key, lab_label, notable_matches = _compute_lab_boost(
            paper, cfg
        )
        comp = base + lab_boost + notable_boost
        out.append(
            ScoredPaper(
                paper=paper,
                scores=ComponentScores(
                    relevance=rel,
                    venue=ven,
                    freshness=fresh,
                    velocity=vel,
                    composite=comp,
                    lab_boost=lab_boost,
                    notable_author_boost=notable_boost,
                    category_hits=hits,
                ),
                lab_key=lab_key,
                lab_label=lab_label,
                notable_author_matches=notable_matches,
            )
        )
    return out

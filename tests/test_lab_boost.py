"""Tests for config_labs matching + scoring lab-boost."""

from __future__ import annotations

from datetime import date, timedelta

from src.config_labs import Lab, LabsConfig, load_labs_config
from src.models import Paper
from src.scoring import score_papers
from src.utils import today_utc

TODAY = today_utc()


def _fake_cfg() -> LabsConfig:
    labs = (
        Lab(
            key="deepseek",
            label="DeepSeek",
            tier=1,
            weight=1.0,
            arxiv_au=("DeepSeek-AI", "Damai Dai"),
            title_keywords=("DeepSeek",),
            watchlist_ids=("2601.07372",),
        ),
        Lab(
            key="nvidia_gear",
            label="NVIDIA GEAR",
            tier=1,
            weight=1.0,
            arxiv_au=("Jim Fan",),
            title_keywords=("GR00T",),
        ),
        Lab(
            key="tier2_lab",
            label="Tier-2 Lab",
            tier=2,
            weight=0.5,
            arxiv_au=("Some PI",),
        ),
    )
    return LabsConfig(
        labs=labs,
        affiliation_terms=("DeepSeek",),
        arxiv_categories=("cs.RO", "cs.LG"),
        arxiv_max_results_per_query=300,
        arxiv_lookback_days=90,
        arxiv_institution_lookback_days=30,
        arxiv_watchlist_lookback_days=365,
        notable_authors=("Fei-Fei Li",),
        lab_boost_base=2.0,
        notable_author_boost=0.5,
    )


# ── LabsConfig.match_lab ───────────────────────────────────────────────────

def test_match_lab_by_author():
    cfg = _fake_cfg()
    lab = cfg.match_lab(authors=["Damai Dai", "Other"], title="Some paper")
    assert lab is not None and lab.key == "deepseek"


def test_match_lab_by_title_keyword():
    cfg = _fake_cfg()
    lab = cfg.match_lab(authors=["Anonymous"], title="GR00T: a humanoid model")
    assert lab is not None and lab.key == "nvidia_gear"


def test_match_lab_by_watchlist_id():
    cfg = _fake_cfg()
    lab = cfg.match_lab(authors=[], title="no org clue", arxiv_id="2601.07372v2")
    assert lab is not None and lab.key == "deepseek"


def test_match_lab_returns_none():
    cfg = _fake_cfg()
    assert cfg.match_lab(authors=["Unknown"], title="generic robotics") is None


def test_match_notable_authors_dedup():
    cfg = _fake_cfg()
    # case-insensitive match + dedup
    hits = cfg.match_notable_authors(["fei-fei li", "Fei-Fei Li", "Nobody"])
    assert hits == ["fei-fei li"]


# ── score_papers lab boost ──────────────────────────────────────────────────

def _mk(title: str, authors: list[str], paper_id: str = "p") -> Paper:
    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        venue="arXiv",
        year=TODAY.year,
        publication_date=TODAY - timedelta(days=10),
        citation_count=0,
    )


def test_lab_boost_applies_to_tier1_paper():
    cfg = _fake_cfg()
    lab_paper = _mk("MoE at scale", ["Damai Dai"], paper_id="p1")
    plain_paper = _mk("MoE at scale", ["Anonymous"], paper_id="p2")
    scored = score_papers([lab_paper, plain_paper], reference=TODAY, labs_cfg=cfg)
    by_id = {s.paper.paper_id: s for s in scored}
    assert by_id["p1"].lab_key == "deepseek"
    assert by_id["p1"].scores.lab_boost == 2.0
    assert by_id["p1"].scores.composite == by_id["p2"].scores.composite + 2.0


def test_lab_boost_scales_with_tier_weight():
    cfg = _fake_cfg()
    t1 = _mk("MoE", ["Jim Fan"], paper_id="t1")
    t2 = _mk("MoE", ["Some PI"], paper_id="t2")
    scored = score_papers([t1, t2], reference=TODAY, labs_cfg=cfg)
    by_id = {s.paper.paper_id: s for s in scored}
    # tier-1 (weight 1.0) gets +2.0; tier-2 (weight 0.5) gets +1.0
    assert by_id["t1"].scores.lab_boost == 2.0
    assert by_id["t2"].scores.lab_boost == 1.0


def test_notable_author_boost_stacks():
    cfg = _fake_cfg()
    notable = _mk("Generic paper", ["Fei-Fei Li"], paper_id="n1")
    scored = score_papers([notable], reference=TODAY, labs_cfg=cfg)
    s = scored[0]
    assert s.notable_author_matches == ["Fei-Fei Li"]
    assert s.scores.notable_author_boost == 0.5


def test_real_labs_yaml_loads():
    # Smoke test: the repo's labs.yaml must parse and expose the primary fields.
    cfg = load_labs_config()
    assert len(cfg.labs) > 5
    assert any(lab.label == "DeepSeek" for lab in cfg.labs)
    assert cfg.lab_boost_base > 0

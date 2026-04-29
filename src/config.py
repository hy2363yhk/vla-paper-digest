"""Central configuration.

All tunable knobs live here so that the rest of the pipeline stays purely
mechanical. Edit this file to change keyword weights, venue priorities, freshness
buckets and bucket thresholds. Runtime secrets come from environment variables
and are collected via :func:`get_settings`.

Design note: we intentionally keep this a plain Python module (not YAML) so
that the weight schema is type-checked by the interpreter itself and refactors
are discoverable with IDE jump-to-definition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE_DIR = PROJECT_ROOT / "templates"

PAPER_DB_PATH = DATA_DIR / "paper_db.json"
HISTORY_PATH = DATA_DIR / "history.json"
CLASSIC_PAPERS_PATH = DATA_DIR / "classic_papers.json"
ROTATION_STATE_PATH = DATA_DIR / "classic_rotation_state.json"
LABS_CONFIG_PATH = PROJECT_ROOT / "config" / "labs.yaml"


# ── Keyword Library ────────────────────────────────────────────────────────
# Layered by weight. Within a category, multiple hits are discounted
# (1.0 / 0.5 / 0.3 / 0.1) to prevent one long abstract from dominating the score.
KEYWORDS: dict[str, dict] = {
    "dynamics_smoothness": {
        "weight": 3.0,
        "terms": [
            "jerk",
            "acceleration continuity",
            "action smoothness",
            "trajectory smoothness",
            "motion smoothness",
            "chattering",
            "smooth trajectory",
            "smooth action",
            "dynamic feasibility",
        ],
    },
    "semantic_smoothness": {
        "weight": 3.0,
        "terms": [
            "action chunk",
            "action chunking",
            "temporal consistency",
            "temporal ensemble",
            "action coherence",
            "multi-modal action",
            "action sequence consistency",
            "prediction horizon",
        ],
    },
    "vla_carrier": {
        "weight": 2.0,
        "terms": [
            "vision language action",
            "VLA",
            "robot foundation model",
            "diffusion policy",
            "flow matching policy",
            "action head",
            "action decoder",
            "imitation learning",
            "behavior cloning",
            "world model",
            "world action model",
            "zero-shot policy",
            "generalist policy",
            "embodied agent",
            "embodied foundation model",
            "humanoid policy",
        ],
    },
    "foundation_support": {
        # Broader terms so that infra-flavoured but relevant papers (MoE, long
        # context, reasoning) still register with a modest score. This is what
        # lets e.g. DeepSeek MoE / KV-cache papers enter the pool even though
        # they don't mention "VLA" directly — the lab-boost in scoring.py then
        # decides whether they bubble up.
        "weight": 1.0,
        "terms": [
            "mixture of experts",
            "MoE",
            "long context",
            "reasoning model",
            "chain of thought",
            "reinforcement learning from",
            "preference optimization",
            "scaling law",
            "test-time compute",
            "speculative decoding",
            "KV cache",
            "conditional memory",
        ],
    },
    "task_scenario": {
        "weight": 0.5,
        "terms": [
            "manipulation",
            "bimanual",
            "dexterous",
            "mobile manipulation",
            "autonomous driving",
            "navigation",
        ],
    },
}

# Discount factors applied to the 2nd, 3rd and 4th+ hit within the same category.
RELEVANCE_HIT_DISCOUNTS: list[float] = [1.0, 0.5, 0.3, 0.1]

# Linear cap used to normalise the raw weighted hit sum to [0, 10].
# 20 is deliberately conservative: a paper needs roughly two hits in each
# heavy category plus carrier terms to reach the ceiling.
RELEVANCE_SCORE_CAP: float = 20.0


# ── Venue Weights ──────────────────────────────────────────────────────────
# Keys are matched case-insensitively with substring semantics against the
# `venue` field returned by Semantic Scholar / OpenReview.
VENUE_WEIGHTS: dict[str, float] = {
    "CoRL": 10,
    "Conference on Robot Learning": 10,
    "RSS": 10,
    "Robotics: Science and Systems": 10,
    "NeurIPS": 9,
    "NeurIPS Robotics": 9,
    "ICRA": 8,
    "International Conference on Robotics and Automation": 8,
    "ICLR": 8,
    "ICML": 8,
    "arXiv": 4,
}

# Canonical venue set we attempt to pull in bulk from Semantic Scholar at
# bootstrap time. OpenReview is used to cross-verify / add missing acceptances.
SEMANTIC_SCHOLAR_VENUES: list[str] = [
    "CoRL",
    "Conference on Robot Learning",
    "RSS",
    "Robotics: Science and Systems",
    "NeurIPS",
    "ICRA",
    "ICLR",
    "ICML",
]


# ── Freshness Buckets (days -> score) ──────────────────────────────────────
# Stair-step rather than exponential because the requirement explicitly
# enumerates cut-offs and "2-year old but still on topic" papers should still
# score a non-zero freshness instead of vanishing.
FRESHNESS_BUCKETS: list[tuple[int, float]] = [
    (30, 10.0),
    (90, 8.0),
    (180, 6.0),
    (365, 4.0),
    (730, 2.0),
]
FRESHNESS_FLOOR: float = 1.0  # > 730 days


# ── Composite weights ──────────────────────────────────────────────────────
COMPOSITE_WEIGHTS = {
    "relevance": 0.5,
    "venue": 0.2,
    "freshness": 0.15,
    "velocity": 0.15,
}


# ── Selector thresholds (overridable by env) ───────────────────────────────
@dataclass(frozen=True)
class SelectorThresholds:
    """Parameters of the daily selector.

    The current selection rule is simple:
        * pick the **top N papers by composite score** (excluding classics and
          papers that have been pushed before) → bucket ``top_ranked``
        * plus **1 classic paper** from the round-robin whitelist
    Legacy fields (``latest_hot_*``, ``top_cited_*``) are kept for backwards
    compatibility with older history entries and unit tests but no longer drive
    selection.
    """

    # New rerank rule (daily push = top_ranked_count + 1 classic)
    top_ranked_count: int = 4
    top_ranked_min_composite: float = 2.0  # sanity floor; set to 0 to disable
    # Diversity cap: at most this many top_ranked papers from the same lab.
    # Prevents Sergey-Levine-group-of-the-day from occupying all 4 slots.
    # Set to 0 to disable the cap.
    max_per_lab: int = 2

    # Legacy (kept for backwards compat, not used by selector)
    latest_hot_days: int = 90
    latest_hot_min_composite: float = 4.0
    top_cited_min_relevance: float = 5.0
    top_cited_percentile: float = 0.75
    lookback_years: int = 2


def load_selector_thresholds() -> SelectorThresholds:
    return SelectorThresholds(
        top_ranked_count=int(os.getenv("VLA_TOP_RANKED_COUNT", "4")),
        top_ranked_min_composite=float(os.getenv("VLA_TOP_RANKED_MIN_SCORE", "2.0")),
        max_per_lab=int(os.getenv("VLA_MAX_PER_LAB", "2")),
        # Legacy overrides still read so existing .env keeps working.
        latest_hot_days=int(os.getenv("VLA_LATEST_HOT_DAYS", "90")),
        latest_hot_min_composite=float(os.getenv("VLA_LATEST_HOT_MIN_SCORE", "4.0")),
        top_cited_min_relevance=float(os.getenv("VLA_TOP_CITED_MIN_RELEVANCE", "5.0")),
    )


# ── Runtime settings ───────────────────────────────────────────────────────
@dataclass
class RuntimeSettings:
    """Secrets & runtime knobs. Populated from environment variables."""

    semantic_scholar_api_key: str | None
    openai_api_key: str | None
    openai_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_to: str
    # Derived
    has_openai: bool = field(init=False)
    has_smtp: bool = field(init=False)

    def __post_init__(self) -> None:
        self.has_openai = bool(self.openai_api_key)
        self.has_smtp = bool(self.smtp_host and self.smtp_user and self.smtp_password and self.email_to)


def get_settings() -> RuntimeSettings:
    """Read env vars into a :class:`RuntimeSettings`.

    Missing non-critical values yield empty strings / falsy flags so that the
    caller (``main.py``) can decide whether to bail or degrade gracefully.
    """

    return RuntimeSettings(
        semantic_scholar_api_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        email_to=os.getenv("EMAIL_TO", ""),
    )

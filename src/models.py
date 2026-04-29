"""Pydantic data models used across the pipeline.

All internal structures derive from :class:`Paper`. Downstream stages wrap it
into :class:`ScoredPaper` and :class:`SelectedPaper` while preserving the
original fields to keep the HTML template rendering simple.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Bucket = Literal["top_ranked", "latest_hot", "top_cited", "classic"]


class Paper(BaseModel):
    """Canonical paper record. ``paper_id`` is the primary key everywhere."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    paper_id: str = Field(..., description="Semantic Scholar paperId, or `arxiv:<id>` fallback.")
    title: str
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(
        default_factory=list,
        description="Unique author affiliations (lab/university/company), top 3 kept.",
    )
    venue: str = ""
    year: int | None = None
    publication_date: date | None = None
    citation_count: int = 0
    influential_citation_count: int = 0
    arxiv_id: str | None = None
    external_url: str | None = None
    source: str = "semantic_scholar"  # "semantic_scholar" | "openreview" | "arxiv"
    is_classic: bool = False
    classic_category: str | None = None
    first_seen_at: datetime | None = None

    @property
    def best_url(self) -> str:
        if self.external_url:
            return self.external_url
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        return f"https://www.semanticscholar.org/paper/{self.paper_id}"

    @property
    def primary_affiliation(self) -> str | None:
        """Best-effort primary affiliation for display (first non-empty)."""
        for aff in self.affiliations:
            if aff and aff.strip():
                return aff.strip()
        return None


class ComponentScores(BaseModel):
    relevance: float
    venue: float
    freshness: float
    velocity: float
    composite: float
    lab_boost: float = 0.0  # contribution from lab/notable-author boost, already folded into composite
    notable_author_boost: float = 0.0
    category_hits: dict[str, int] = Field(default_factory=dict)


class ScoredPaper(BaseModel):
    paper: Paper
    scores: ComponentScores
    lab_key: str | None = None
    lab_label: str | None = None
    notable_author_matches: list[str] = Field(default_factory=list)

    @property
    def paper_id(self) -> str:
        return self.paper.paper_id


class LLMSummary(BaseModel):
    """6-field structured summary produced by GPT-4o-mini."""

    direction: str = ""
    core_problem: str = ""
    key_method: str = ""
    conclusion: str = ""
    limitation: str = ""
    relevance_to_smoothness: str = ""
    failed: bool = False
    error: str | None = None


class SelectedPaper(BaseModel):
    paper: Paper
    scores: ComponentScores
    bucket: Bucket
    bucket_label_cn: str
    summary: LLMSummary | None = None
    lab_key: str | None = None
    lab_label: str | None = None
    notable_author_matches: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    paper_id: str
    pushed_on: date
    bucket: Bucket
    title: str


class RotationState(BaseModel):
    next_index: int = 0

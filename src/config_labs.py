"""Loader for :file:`config/labs.yaml`.

The YAML file carries all editorial curation — which labs / PIs / brand names
we actively track, plus arXiv search parameters and lab-boost weights. Keeping
this off-code lets non-developers add a new lab without touching Python.

All data is loaded eagerly at import time via :func:`load_labs_config` so that
callers get the same object and we don't re-parse the file for every source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

from .config import LABS_CONFIG_PATH


@dataclass(frozen=True)
class Lab:
    key: str                 # dict key, e.g. "deepseek"
    label: str               # human-readable label, e.g. "DeepSeek"
    tier: int                # 1 (flagship) or 2 (secondary)
    weight: float            # lab-boost multiplier (applied on top of lab_boost_base)
    arxiv_au: tuple[str, ...] = ()
    title_keywords: tuple[str, ...] = ()
    watchlist_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabsConfig:
    labs: tuple[Lab, ...]
    affiliation_terms: tuple[str, ...]
    arxiv_categories: tuple[str, ...]
    arxiv_max_results_per_query: int
    arxiv_lookback_days: int
    arxiv_institution_lookback_days: int
    arxiv_watchlist_lookback_days: int
    notable_authors: tuple[str, ...] = ()
    lab_boost_base: float = 2.0
    notable_author_boost: float = 0.5

    # Pre-computed lookup tables (case-insensitive) — filled in __post_init__.
    _author_to_lab: dict[str, Lab] = field(default_factory=dict, hash=False)
    _title_keyword_to_lab: dict[str, Lab] = field(default_factory=dict, hash=False)
    _watchlist_to_lab: dict[str, Lab] = field(default_factory=dict, hash=False)
    _notable_authors_lc: frozenset[str] = field(default_factory=frozenset, hash=False)

    def __post_init__(self) -> None:
        a2l: dict[str, Lab] = {}
        t2l: dict[str, Lab] = {}
        w2l: dict[str, Lab] = {}
        for lab in self.labs:
            for au in lab.arxiv_au:
                a2l[au.lower()] = lab
            for kw in lab.title_keywords:
                t2l[kw.lower()] = lab
            for aid in lab.watchlist_ids:
                w2l[aid] = lab
        # dataclass is frozen → bypass via object.__setattr__
        object.__setattr__(self, "_author_to_lab", a2l)
        object.__setattr__(self, "_title_keyword_to_lab", t2l)
        object.__setattr__(self, "_watchlist_to_lab", w2l)
        object.__setattr__(
            self,
            "_notable_authors_lc",
            frozenset(name.lower() for name in self.notable_authors),
        )

    # ── public lookups ──────────────────────────────────────────────────
    def match_lab(self, authors: list[str], title: str, arxiv_id: str | None = None) -> Lab | None:
        """Return the first lab that claims this paper, or None."""
        if arxiv_id:
            base = arxiv_id.split("v", 1)[0]
            if base in self._watchlist_to_lab:
                return self._watchlist_to_lab[base]

        for au in authors:
            if au.lower() in self._author_to_lab:
                return self._author_to_lab[au.lower()]

        if title:
            tl = title.lower()
            for kw, lab in self._title_keyword_to_lab.items():
                if kw in tl:
                    return lab
        return None

    def match_notable_authors(self, authors: list[str]) -> list[str]:
        hits: list[str] = []
        seen: set[str] = set()
        for au in authors:
            lc = au.lower()
            if lc in self._notable_authors_lc and lc not in seen:
                hits.append(au)
                seen.add(lc)
        return hits

    def all_watchlist_ids(self) -> list[str]:
        ids: list[str] = []
        for lab in self.labs:
            ids.extend(lab.watchlist_ids)
        return ids


@lru_cache(maxsize=1)
def load_labs_config(path: Path | None = None) -> LabsConfig:
    """Parse labs.yaml. Cached so that re-import is cheap."""
    p = path or LABS_CONFIG_PATH
    if not p.exists():
        logger.warning(f"labs config not found at {p}; lab-boost disabled")
        return LabsConfig(
            labs=(),
            affiliation_terms=(),
            arxiv_categories=("cs.RO",),
            arxiv_max_results_per_query=200,
            arxiv_lookback_days=90,
            arxiv_institution_lookback_days=30,
            arxiv_watchlist_lookback_days=365,
        )

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    labs: list[Lab] = []
    for key, entry in (raw.get("labs") or {}).items():
        labs.append(
            Lab(
                key=key,
                label=str(entry.get("label") or key),
                tier=int(entry.get("tier") or 2),
                weight=float(entry.get("weight") or 1.0),
                arxiv_au=tuple(entry.get("arxiv_au") or ()),
                title_keywords=tuple(entry.get("title_keywords") or ()),
                watchlist_ids=tuple(str(x) for x in (entry.get("watchlist_ids") or ())),
            )
        )

    cfg = LabsConfig(
        labs=tuple(labs),
        affiliation_terms=tuple(raw.get("affiliation_terms") or ()),
        arxiv_categories=tuple(raw.get("arxiv_categories") or ("cs.RO",)),
        arxiv_max_results_per_query=int(raw.get("arxiv_max_results_per_query") or 300),
        arxiv_lookback_days=int(raw.get("arxiv_lookback_days") or 90),
        arxiv_institution_lookback_days=int(raw.get("arxiv_institution_lookback_days") or 30),
        arxiv_watchlist_lookback_days=int(raw.get("arxiv_watchlist_lookback_days") or 365),
        notable_authors=tuple(raw.get("notable_authors") or ()),
        lab_boost_base=float(raw.get("lab_boost_base") or 2.0),
        notable_author_boost=float(raw.get("notable_author_boost") or 0.5),
    )
    logger.info(
        f"labs.yaml loaded: {len(cfg.labs)} labs, "
        f"{len(cfg.affiliation_terms)} affiliations, "
        f"{len(cfg.notable_authors)} notable authors"
    )
    return cfg

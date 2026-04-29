"""Data source adapters: Semantic Scholar, OpenReview, arXiv, HF Daily."""

from src.sources.arxiv_source import (
    fetch_arxiv_all,
    fetch_arxiv_by_affiliations,
    fetch_arxiv_by_authors,
    fetch_arxiv_by_title_keywords,
    fetch_arxiv_cs_ro,
    fetch_arxiv_keyword_sweep,
    fetch_arxiv_watchlist_ids,
)
from src.sources.hf_papers_source import fetch_hf_daily_papers
from src.sources.openreview import fetch_openreview_accepted
from src.sources.semantic_scholar import (
    SemanticScholarClient,
    bulk_search_by_venues,
    lookup_papers_by_title,
)

__all__ = [
    "SemanticScholarClient",
    "bulk_search_by_venues",
    "lookup_papers_by_title",
    "fetch_openreview_accepted",
    "fetch_arxiv_cs_ro",
    "fetch_arxiv_all",
    "fetch_arxiv_keyword_sweep",
    "fetch_arxiv_by_authors",
    "fetch_arxiv_by_title_keywords",
    "fetch_arxiv_by_affiliations",
    "fetch_arxiv_watchlist_ids",
    "fetch_hf_daily_papers",
]

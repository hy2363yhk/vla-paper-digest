"""Entry point: orchestrate fetch → score → select → summarise → email.

CLI flags:
    --dry-run           fetch + score + select, but do NOT send email or commit history
    --no-ai             skip LLM summaries (emails will only show raw abstract)
    --force-bootstrap   force a full bulk refresh from Semantic Scholar by venue
    --verbose / -v      DEBUG logging

Run manually for local testing:
    python -m src.main --dry-run
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from src.config import (
    KEYWORDS,
    SEMANTIC_SCHOLAR_VENUES,
    get_settings,
    load_selector_thresholds,
)
from src.config_labs import load_labs_config
from src.emailer import send_digest_email
from src.models import HistoryEntry, Paper
from src.scoring import score_papers
from src.selector import pick_daily_three
from src.sources import (
    SemanticScholarClient,
    bulk_search_by_venues,
    fetch_arxiv_all,
    fetch_hf_daily_papers,
    fetch_openreview_accepted,
    lookup_papers_by_title,
)
from src.storage import (
    load_classic_whitelist,
    load_history,
    load_paper_db,
    load_rotation_state,
    save_history,
    save_paper_db,
    save_rotation_state,
    upsert_papers,
)
from src.summarizer import summarize_batch
from src.utils import setup_logging, today_utc
from src.wechat_notifier import send_wechat


def _banner(args: argparse.Namespace, settings: Any, bootstrap: bool, thresholds: Any) -> None:
    lines = [
        "=" * 68,
        " VLA Paper Digest",
        "=" * 68,
        f"  run date (UTC)        : {today_utc().isoformat()}",
        f"  mode                  : {'BOOTSTRAP (bulk 2y)' if bootstrap else 'daily incremental'}",
        f"  dry-run               : {args.dry_run}",
        f"  skip LLM summary      : {args.no_ai}",
        f"  Semantic Scholar key  : {'set' if settings.semantic_scholar_api_key else 'MISSING (public quota)'}",
        f"  OpenAI key            : {'set' if settings.has_openai else 'MISSING (no summary)'}",
        f"  SMTP configured       : {settings.has_smtp}",
        f"  venues targeted       : {len(SEMANTIC_SCHOLAR_VENUES)}",
        f"  keyword categories    : {list(KEYWORDS.keys())}",
        f"  daily push size       : {thresholds.top_ranked_count} + 1 classic = {thresholds.top_ranked_count + 1}",
        "=" * 68,
    ]
    for line in lines:
        logger.info(line)


def _flat_arxiv_keywords() -> list[str]:
    """Flatten the high-weight keywords for the arXiv pre-filter.

    We include ``foundation_support`` too so MoE / long-context / reasoning
    papers from DeepSeek et al. are surfaced by the keyword sweep even when
    they don't match a tracked author.
    """
    terms: list[str] = []
    priority_categories = (
        "dynamics_smoothness",
        "semantic_smoothness",
        "vla_carrier",
        "foundation_support",
    )
    for cat, spec in KEYWORDS.items():
        if cat in priority_categories:
            terms.extend(spec["terms"])
    return terms


def bootstrap_fetch(client: SemanticScholarClient) -> list[Paper]:
    year_from = today_utc().year - 2
    logger.info(f"[bootstrap] bulk by venue (year >= {year_from})")
    ss_papers = bulk_search_by_venues(client, SEMANTIC_SCHOLAR_VENUES, year_from=year_from)
    logger.info(f"[bootstrap] Semantic Scholar returned {len(ss_papers)} unique papers")
    return ss_papers


def incremental_fetch(client: SemanticScholarClient) -> list[Paper]:
    """Query SS with each keyword category as a textual search.

    We don't have a true "since" filter on Semantic Scholar's public API, so
    we re-query keyword-focused searches limited to the current year and rely
    on the upsert step in :mod:`src.storage` to deduplicate.
    """
    year_from = today_utc().year - 1
    collected: list[Paper] = []
    for cat, spec in KEYWORDS.items():
        if cat not in ("dynamics_smoothness", "semantic_smoothness", "vla_carrier"):
            continue
        for term in spec["terms"][:4]:  # top few to save API quota
            logger.info(f"[incremental] SS search '{term}' year>={year_from}")
            papers = client.search_by_query(term, year_from=year_from, max_pages=2)
            collected.extend(papers)
    return collected


def maybe_resolve_classic_papers(
    client: SemanticScholarClient, classic_whitelist: list[dict]
) -> bool:
    """For classic entries without a resolved paper_id, try to fetch one.

    Returns True if the whitelist was mutated and should be re-saved.
    """
    unresolved = [e for e in classic_whitelist if not e.get("paper_id")]
    if not unresolved:
        return False
    logger.info(f"[classic] resolving {len(unresolved)} whitelist entries ...")
    titles = [e["title"] for e in unresolved]
    matches = lookup_papers_by_title(client, titles)
    dirty = False
    for entry in unresolved:
        paper = matches.get(entry["title"])
        if not paper:
            continue
        entry["paper_id"] = paper.paper_id
        entry["abstract"] = paper.abstract
        entry["authors"] = paper.authors[:8]
        entry["venue"] = paper.venue
        entry["year_resolved"] = paper.year
        entry["arxiv_id"] = paper.arxiv_id
        dirty = True
    return dirty


def inject_classics_into_db(
    db: dict[str, Paper], classic_whitelist: list[dict]
) -> None:
    """Ensure every resolved classic paper is present in the DB."""

    from src.utils import now_utc, parse_date

    for entry in classic_whitelist:
        paper_id = entry.get("paper_id")
        if not paper_id:
            continue
        if paper_id in db:
            db[paper_id].is_classic = True
            db[paper_id].classic_category = entry.get("category")
            continue
        y = entry.get("year_resolved") or entry.get("year")
        db[paper_id] = Paper(
            paper_id=paper_id,
            title=entry.get("title", ""),
            abstract=entry.get("abstract", ""),
            authors=entry.get("authors", []),
            venue=entry.get("venue", ""),
            year=y,
            publication_date=parse_date(f"{y}-01-01") if y else None,
            arxiv_id=entry.get("arxiv_id"),
            external_url=f"https://www.semanticscholar.org/paper/{paper_id}",
            source="semantic_scholar",
            is_classic=True,
            classic_category=entry.get("category"),
            first_seen_at=now_utc(),
        )


def _print_next_steps() -> None:
    next_steps = """
=============================== NEXT STEPS ===============================
1) 创建 GitHub 仓库并推送本项目：
     cd vla-paper-digest
     git init && git add . && git commit -m "init vla paper digest"
     git branch -M main
     git remote add origin git@github.com:<YOUR>/<REPO>.git
     git push -u origin main

2) 在 GitHub 仓库 Settings → Secrets and variables → Actions 里配置：
     SEMANTIC_SCHOLAR_API_KEY   — https://www.semanticscholar.org/product/api#api-key-form
     OPENAI_API_KEY             — https://platform.openai.com/api-keys
     SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / EMAIL_TO
       （建议 163 或 Gmail；密码用邮箱的「授权码 / 应用专用密码」，不是登录密码）
     WECHAT_PUSHPLUS_TOKEN     — 可选，https://www.pushplus.plus 获取
     WECHAT_PUSHPLUS_TOPIC     — 可选，一对多群组推送时才填

3) 到 Actions 页手动触发一次 "Daily VLA Paper Digest"，走一遍 bootstrap。
   首次运行大约 10-20 分钟，后续日常增量约 3-5 分钟。

4) 本地调试（不发邮件）：
     cp .env.example .env   # 手动填入所有值
     python -m src.main --dry-run
==========================================================================
"""
    print(next_steps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VLA Paper Digest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--force-bootstrap", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Smoke-test mode: skip ALL Semantic Scholar and OpenReview calls; "
            "only refresh arXiv cs.RO against the existing paper_db.json."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--print-next-steps",
        action="store_true",
        help="Print the post-install checklist and exit.",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    setup_logging(verbose=args.verbose)

    if args.print_next_steps:
        _print_next_steps()
        return 0

    settings = get_settings()
    thresholds = load_selector_thresholds()
    labs_cfg = load_labs_config()
    db = load_paper_db()
    history = load_history()
    rotation = load_rotation_state()
    classic_whitelist = load_classic_whitelist()

    bootstrap = args.force_bootstrap or len(db) < 50
    _banner(args, settings, bootstrap, thresholds)

    # ── 1. Resolve classic whitelist paperIds on first run ─────────────
    fetched: list[Paper] = []
    if args.quick:
        logger.info(
            "[quick] skipping ALL Semantic Scholar calls (classic resolve + bulk + incremental)"
        )
    else:
        with SemanticScholarClient(settings.semantic_scholar_api_key) as ss_client:
            if classic_whitelist:
                dirty = maybe_resolve_classic_papers(ss_client, classic_whitelist)
                if dirty:
                    from src.config import CLASSIC_PAPERS_PATH
                    from src.storage import _write_json_atomic

                    _write_json_atomic(CLASSIC_PAPERS_PATH, classic_whitelist)

            # ── 2. Fetch ──────────────────────────────────────────────
            try:
                if bootstrap:
                    fetched.extend(bootstrap_fetch(ss_client))
                else:
                    fetched.extend(incremental_fetch(ss_client))
            except Exception as exc:
                logger.error(f"Semantic Scholar fetch failed: {exc}")

    try:
        arxiv_papers = fetch_arxiv_all(_flat_arxiv_keywords(), labs_cfg)
        fetched.extend(arxiv_papers)
    except Exception as exc:
        logger.error(f"arXiv fetch failed: {exc}")

    # HF Daily Papers: industry-heavy, surfaces NVIDIA/DeepSeek/Meta releases
    # before they propagate to Semantic Scholar.
    try:
        hf_papers = fetch_hf_daily_papers(lookback_days=14)
        fetched.extend(hf_papers)
    except Exception as exc:
        logger.error(f"HF Daily fetch failed: {exc}")

    if bootstrap and not args.quick:
        try:
            current_year = today_utc().year
            or_papers = fetch_openreview_accepted(years=[current_year - 1, current_year])
            fetched.extend(or_papers)
        except Exception as exc:
            logger.error(f"OpenReview fetch failed: {exc}")

    if args.quick:
        # In quick mode, also drop the --force-bootstrap flag semantically:
        # we don't want to accidentally enter full bulk mode next run.
        pass

    logger.info(f"total fetched: {len(fetched)} papers (pre-dedup)")

    # ── 3. Merge into DB ───────────────────────────────────────────────
    added = upsert_papers(db, fetched)
    logger.info(f"DB: {len(db)} papers total (+{added} new)")
    inject_classics_into_db(db, classic_whitelist)

    # ── 4. Score ───────────────────────────────────────────────────────
    scored = score_papers(list(db.values()), labs_cfg=labs_cfg)
    logger.info(f"scored {len(scored)} papers")
    lab_tagged = sum(1 for s in scored if s.lab_key)
    logger.info(f"  of which {lab_tagged} tagged with a followed lab (boost applied)")

    # ── 5. Select 3 by recipe ──────────────────────────────────────────
    picked, new_rotation = pick_daily_three(
        scored_papers=scored,
        db=db,
        classic_whitelist=classic_whitelist,
        history=history,
        rotation=rotation,
        thresholds=thresholds,
    )

    if not picked:
        logger.warning("nothing picked for today; exiting without email")
        save_paper_db(db)
        return 0

    # ── 6. LLM summarise ───────────────────────────────────────────────
    if not args.no_ai and settings.has_openai:
        summaries = summarize_batch(
            settings.openai_api_key or "",
            settings.openai_model,
            [sp.paper for sp in picked],
        )
        for sp, summary in zip(picked, summaries, strict=True):
            sp.summary = summary
    else:
        logger.info("[LLM] skipped (no key or --no-ai)")

    # ── 7. Dispatch ────────────────────────────────────────────────────
    today = today_utc()
    if args.dry_run:
        logger.info("=" * 50)
        logger.info(f"DRY RUN — would have emailed {len(picked)} papers:")
        for sp in picked:
            logger.info(
                f"  [{sp.bucket_label_cn}] composite={sp.scores.composite:.2f} "
                f"relevance={sp.scores.relevance:.2f} | {sp.paper.title}"
            )
            if sp.lab_label:
                logger.info(f"      🏢 lab: {sp.lab_label} (boost +{sp.scores.lab_boost:.2f})")
            if sp.summary and not sp.summary.failed:
                logger.info(f"      ↪ {sp.summary.relevance_to_smoothness}")
    else:
        sent = send_digest_email(picked, settings, today=today)
        # WeChat push is best-effort, independent of email success.
        try:
            send_wechat(picked, today=today)
        except Exception as exc:
            logger.error(f"WeChat push unexpected failure: {exc}")

        if sent:
            for sp in picked:
                history.append(
                    HistoryEntry(
                        paper_id=sp.paper.paper_id,
                        pushed_on=today,
                        bucket=sp.bucket,
                        title=sp.paper.title,
                    )
                )
            save_history(history)
            save_rotation_state(new_rotation)

    save_paper_db(db)
    logger.info("run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""JSON-backed persistence for paper DB, push history and classic rotation state.

We deliberately use flat JSON files (not SQLite) because:
    1. GitHub Actions commits data/ back to the repo, and JSON diffs are
       human-readable.
    2. The dataset is small (at most a few thousand papers) so there's no
       performance concern.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from pydantic import TypeAdapter

from src.config import (
    CLASSIC_PAPERS_PATH,
    DATA_DIR,
    HISTORY_PATH,
    PAPER_DB_PATH,
    ROTATION_STATE_PATH,
)
from src.models import HistoryEntry, Paper, RotationState


# ── Paper DB ─────────────────────────────────────────────────────────────
def load_paper_db() -> dict[str, Paper]:
    if not PAPER_DB_PATH.exists():
        return {}
    try:
        raw = json.loads(PAPER_DB_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error(f"paper_db.json is corrupt: {exc}; starting from empty")
        return {}
    papers: dict[str, Paper] = {}
    for item in raw:
        try:
            paper = Paper.model_validate(item)
            papers[paper.paper_id] = paper
        except Exception as exc:  # pragma: no cover — permissive loader
            logger.warning(f"skip bad paper entry: {exc}")
    return papers


def save_paper_db(papers: dict[str, Paper]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [p.model_dump(mode="json") for p in papers.values()]
    _write_json_atomic(PAPER_DB_PATH, payload)
    logger.info(f"paper_db saved: {len(payload)} records → {PAPER_DB_PATH}")


def upsert_papers(db: dict[str, Paper], new_papers: list[Paper]) -> int:
    """Merge new papers into the DB, preferring richer/newer metadata.

    Returns the number of papers that were *added* (not just updated).
    """

    added = 0
    for incoming in new_papers:
        existing = db.get(incoming.paper_id)
        if existing is None:
            db[incoming.paper_id] = incoming
            added += 1
            continue
        # Prefer the record with more fields populated, but keep the earliest
        # first_seen_at so we can reason about "new" vs "old" later.
        merged = _merge_paper(existing, incoming)
        db[incoming.paper_id] = merged
    return added


def _merge_paper(existing: Paper, incoming: Paper) -> Paper:
    data = existing.model_dump()
    inc = incoming.model_dump()
    for key, value in inc.items():
        if value in (None, "", 0, []):
            continue
        current = data.get(key)
        if current in (None, "", 0, []):
            data[key] = value
        elif key == "citation_count" and isinstance(value, int) and value > int(current or 0):
            data[key] = value
        elif key == "influential_citation_count" and isinstance(value, int) and value > int(current or 0):
            data[key] = value
        elif key == "abstract" and isinstance(value, str) and len(value) > len(str(current or "")):
            data[key] = value
    # preserve earliest first_seen_at
    data["first_seen_at"] = existing.first_seen_at or incoming.first_seen_at
    data["is_classic"] = existing.is_classic or incoming.is_classic
    data["classic_category"] = existing.classic_category or incoming.classic_category
    return Paper.model_validate(data)


# ── History ──────────────────────────────────────────────────────────────
_HISTORY_ADAPTER = TypeAdapter(list[HistoryEntry])


def load_history() -> list[HistoryEntry]:
    if not HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.error("history.json is corrupt; starting from empty")
        return []
    try:
        return _HISTORY_ADAPTER.validate_python(raw)
    except Exception as exc:  # pragma: no cover
        logger.warning(f"history validation failed: {exc}")
        return []


def save_history(entries: list[HistoryEntry]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [e.model_dump(mode="json") for e in entries]
    _write_json_atomic(HISTORY_PATH, payload)
    logger.info(f"history saved: {len(payload)} entries → {HISTORY_PATH}")


def pushed_paper_ids(history: list[HistoryEntry]) -> set[str]:
    return {e.paper_id for e in history}


# ── Classic rotation state ───────────────────────────────────────────────
def load_rotation_state() -> RotationState:
    if not ROTATION_STATE_PATH.exists():
        return RotationState()
    try:
        raw = json.loads(ROTATION_STATE_PATH.read_text(encoding="utf-8"))
        return RotationState.model_validate(raw)
    except Exception as exc:  # pragma: no cover
        logger.warning(f"rotation state corrupt: {exc}; reset to 0")
        return RotationState()


def save_rotation_state(state: RotationState) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(ROTATION_STATE_PATH, state.model_dump(mode="json"))


# ── Classic whitelist ────────────────────────────────────────────────────
def load_classic_whitelist() -> list[dict]:
    if not CLASSIC_PAPERS_PATH.exists():
        logger.warning(f"{CLASSIC_PAPERS_PATH} missing; classic rotation disabled")
        return []
    try:
        return json.loads(CLASSIC_PAPERS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error(f"classic_papers.json is corrupt: {exc}")
        return []


# ── Atomic write ─────────────────────────────────────────────────────────
def _write_json_atomic(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)

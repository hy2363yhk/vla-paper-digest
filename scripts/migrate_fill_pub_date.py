"""One-shot migration: fill missing ``publication_date`` from ``year`` in the DB.

Run once after upgrading the openreview source to the new pub-date logic:
    python -m scripts.migrate_fill_pub_date
"""

from __future__ import annotations

from datetime import date

from src.storage import load_paper_db, save_paper_db


def main() -> None:
    db = load_paper_db()
    patched = 0
    for paper in db.values():
        if paper.publication_date is None and paper.year:
            try:
                paper.publication_date = date(int(paper.year), 1, 1)
                patched += 1
            except (TypeError, ValueError):
                continue
    save_paper_db(db)
    print(f"migration done: patched {patched} records; total {len(db)}")


if __name__ == "__main__":
    main()

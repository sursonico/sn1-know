"""
migrate.py — Import entries from the v1 knowledge_base.db into v2.

Usage:
    python migrate.py \
        --v1-db  ~/sn1-data-catalogue/knowledge_base.db \
        --v1-docs ~/sn1-data-catalogue/sample_docs

What it does:
    1. Creates the v2 DB schema.
    2. Copies every v1 entry (documents + snippets) with field mapping.
    3. For documents whose file is accessible, extracts per-page/slide chunks
       and builds the FTS5 index.
    4. For snippets, stores the full_text as a single chunk and indexes it.
    5. Copies document files into DOCS_DIR (symlink-safe hard-copy).
"""

import argparse
import logging
import shutil
import sqlite3
from pathlib import Path

from config import DB_PATH, DOCS_DIR
from kb import db
from kb.ingest import extract as extract_file, content_hash

log = logging.getLogger("sn1.migrate")


# ── V1 field mapping ──────────────────────────────────────────────────────────

def _map_v1_entry(row: dict) -> dict:
    """Map a v1 entries row dict to v2 upsert_document kwargs."""
    return dict(
        source      = row.get("source", ""),
        entry_date  = row.get("entry_date", ""),
        file_type   = row.get("file_type", ""),
        doc_type    = row.get("doc_type", ""),
        org_tags    = row.get("org_tags", ""),
        market_tags = row.get("market_tags", ""),
        sport_tags  = row.get("sport_tags", ""),
        topic_tags  = row.get("topic_tags", ""),
        summary     = row.get("summary", ""),
        notes       = row.get("notes", ""),
        file_path   = "",   # will be set after copy
        content_hash= "",   # will be computed after copy
    )


# ── Main migration ────────────────────────────────────────────────────────────

def migrate(v1_db_path: Path, v1_docs_dir: Path) -> None:
    if not v1_db_path.exists():
        raise FileNotFoundError(f"v1 DB not found: {v1_db_path}")

    log.info("Initialising v2 database at %s", DB_PATH)
    db.init_db()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(v1_db_path))
    con.row_factory = sqlite3.Row

    rows = con.execute("SELECT * FROM entries ORDER BY id").fetchall()
    log.info("Found %d entries in v1 database", len(rows))

    ok = skip = err = 0

    for row in rows:
        row = dict(row)
        etype = row.get("entry_type", "document")
        source = row.get("source", "")
        log.info("  %s: %s", etype, source)

        try:
            if etype == "document":
                # Copy file to v2 docs dir
                v1_file = v1_docs_dir / source
                v2_file = DOCS_DIR / source
                if v1_file.exists() and not v2_file.exists():
                    shutil.copy2(v1_file, v2_file)
                    log.info("    Copied %s", source)

                # Compute hash if file is accessible
                h = content_hash(v2_file) if v2_file.exists() else ""

                kwargs = _map_v1_entry(row)
                kwargs["file_path"]   = str(v2_file.resolve()) if v2_file.exists() else ""
                kwargs["content_hash"] = h

                # Skip if hash already in v2
                if h and db.hash_exists(h):
                    log.info("    Already in v2 (hash match) — skipping")
                    skip += 1
                    continue

                entry_id = db.upsert_document(**kwargs)

                # Extract per-page/slide chunks from the copied file
                if v2_file.exists():
                    result = extract_file(v2_file)
                    if not result.error and result.chunks:
                        db_chunks = [
                            db.Chunk(c.chunk_num, c.chunk_type, c.text)
                            for c in result.chunks
                        ]
                        db.store_chunks(entry_id, db_chunks)
                        log.info("    Stored %d chunks", len(db_chunks))

                db.index_entry(entry_id)
                ok += 1

            else:  # snippet
                full_text = row.get("full_text", "")
                entry_id = db.add_snippet(
                    source      = source,
                    entry_date  = row.get("entry_date", ""),
                    full_text   = full_text,
                    summary     = row.get("summary", ""),
                    org_tags    = row.get("org_tags", ""),
                    market_tags = row.get("market_tags", ""),
                    sport_tags  = row.get("sport_tags", ""),
                    topic_tags  = row.get("topic_tags", ""),
                )
                db.index_entry(entry_id)
                ok += 1

        except Exception as e:
            log.error("  FAILED %s: %s", source, e)
            err += 1

    con.close()
    log.info("Migration complete: %d imported, %d skipped, %d errors", ok, skip, err)
    print(f"\nMigration complete: {ok} imported  {skip} skipped  {err} errors")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Import v1 catalogue into v2 knowledge base")
    ap.add_argument(
        "--v1-db",
        default=str(Path.home() / "sn1-data-catalogue" / "knowledge_base.db"),
        help="Path to the v1 knowledge_base.db SQLite file",
    )
    ap.add_argument(
        "--v1-docs",
        default=str(Path.home() / "sn1-data-catalogue" / "sample_docs"),
        help="Path to the v1 sample_docs directory",
    )
    args = ap.parse_args()

    migrate(Path(args.v1_db), Path(args.v1_docs))

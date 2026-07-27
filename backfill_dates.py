#!/usr/bin/env python3
"""
backfill_dates.py — Infer doc_date and coverage_period for existing entries
that are missing these values, using a targeted LLM call over their text.

Run:
  python3 backfill_dates.py --dry-run   # show what would be updated
  python3 backfill_dates.py             # write updates to DB
  python3 backfill_dates.py --entry 42  # single entry
"""

import argparse
import json
import sys
import textwrap

from kb import db
from kb.llm import call_claude
from config import CLASSIFY_MODEL

_DATE_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights research analyst.
    Given a text excerpt, extract date information and return ONLY a JSON object:
    {
      "doc_date":        "when this content was written/published — year or date (e.g. '2024', '2024-03', or '')",
      "coverage_period": "the rights cycle or time span this content describes, if different from doc_date (e.g. '2025-2028', '2022-2025 cycle', or '')"
    }
    Rules:
    - doc_date: the publication or creation date of the document itself, not dates mentioned in its content.
    - coverage_period: only fill in if the content explicitly describes a specific future or ongoing rights period.
    - Return empty strings rather than guessing.
    - Respond with only the JSON object, no markdown fences.
""").strip()


def _extract_dates(text: str) -> dict:
    excerpt = text[:3000]
    raw = call_claude(
        _DATE_SYSTEM,
        f"Text excerpt:\n\n{excerpt}",
        model=CLASSIFY_MODEL,
        max_tokens=100,
        timeout=60,
    )
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"doc_date": "", "coverage_period": ""}


def backfill_entry(entry: dict, dry_run: bool = False) -> bool:
    eid    = entry["id"]
    source = entry["source"]
    etype  = entry["entry_type"]

    existing_date = (entry.get("entry_date") or "").strip()
    existing_cov  = (entry.get("coverage_period") or "").strip()

    # Only skip if both are already populated (non-empty, non-Unknown)
    has_date = existing_date and existing_date.lower() != "unknown"
    has_cov  = bool(existing_cov)
    if has_date and has_cov:
        print(f"  #{eid} [{etype}] {source[:55]} — already has date='{existing_date}' cov='{existing_cov}', skip")
        return False

    # Get full text from chunks
    chunks_by = db.get_chunks_for_entries([eid])
    chunks    = chunks_by.get(eid, [])
    if not chunks:
        # Fall back to summary
        text = entry.get("summary", "")
    else:
        text = "\n\n".join(c["text"] for c in chunks[:3] if (c.get("text") or "").strip())

    if not text.strip():
        print(f"  #{eid} [{etype}] {source[:55]} — no text, skip")
        return False

    result = _extract_dates(text)
    doc_date        = (result.get("doc_date") or "").strip()
    coverage_period = (result.get("coverage_period") or "").strip()

    updates = {}
    if not has_date and doc_date:
        updates["entry_date"] = doc_date
    if not has_cov and coverage_period:
        updates["coverage_period"] = coverage_period

    if not updates:
        print(f"  #{eid} [{etype}] {source[:55]} — LLM found nothing new (date='{doc_date}' cov='{coverage_period}')")
        return False

    update_str = "  ".join(f"{k}='{v}'" for k, v in updates.items())
    print(f"  #{eid} [{etype}] {source[:55]} → {update_str}")

    if not dry_run:
        db.update_enrichment(eid, **updates)

    return True


def main():
    parser = argparse.ArgumentParser(description="Backfill doc_date and coverage_period for existing entries")
    parser.add_argument("--entry", type=int, help="Process a specific entry ID only")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be updated without writing")
    args = parser.parse_args()

    db.init_db()

    if args.entry:
        all_entries = db.get_all_entries()
        entry = next((e for e in all_entries if e["id"] == args.entry), None)
        if not entry:
            print(f"Entry #{args.entry} not found")
            sys.exit(1)
        targets = [entry]
    else:
        # All entries that are missing coverage_period (or have Unknown/empty date)
        import sqlite3
        from config import DB_PATH
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT * FROM entries
            WHERE (coverage_period IS NULL OR coverage_period = '')
              AND ingest_error = ''
            ORDER BY entry_type, created_at
        """).fetchall()
        con.close()
        targets = [dict(r) for r in rows]

    if not targets:
        print("No entries need backfilling.")
        return

    mode = "DRY RUN — " if args.dry_run else ""
    print(f"{mode}Backfilling dates for {len(targets)} entries...\n")

    updated = 0
    for entry in targets:
        if backfill_entry(entry, dry_run=args.dry_run):
            updated += 1

    print(f"\n{'Would update' if args.dry_run else 'Updated'} {updated} of {len(targets)} entries.")


if __name__ == "__main__":
    main()

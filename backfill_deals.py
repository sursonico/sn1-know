#!/usr/bin/env python3
"""
backfill_deals.py — Re-run structured deal/broadcaster extraction over existing
entries that have no deals, so entity pages get populated without re-uploading.

Targets:
  - All snippets with zero deals linked to them
  - All documents with zero deals linked to them (catches PDFs where extraction
    may have failed silently at ingest time)

Run:
  python3 backfill_deals.py              # process all entries with zero deals
  python3 backfill_deals.py --entry 169  # process a specific entry ID
  python3 backfill_deals.py --dry-run    # print what would be extracted, no writes
"""

import argparse
import sys
from pathlib import Path

from kb import db
from kb.llm import extract_deals

DB_PATH = Path(__file__).parent / "knowledge_base.db"


def backfill_entry(entry: dict, dry_run: bool = False) -> int:
    """
    Run deal extraction on a single entry. Returns number of new deals added.
    """
    eid    = entry["id"]
    source = entry["source"]
    etype  = entry["entry_type"]
    rel    = entry.get("reliability", "reported") or "reported"

    # Get full text from chunks
    chunks_by = db.get_chunks_for_entries([eid])
    chunks = chunks_by.get(eid, [])
    if not chunks:
        print(f"  #{eid} [{etype}] {source[:60]} — no chunks, skipping")
        return 0

    full_text = "\n\n".join(c["text"] for c in chunks if (c.get("text") or "").strip())
    if not full_text.strip():
        print(f"  #{eid} [{etype}] {source[:60]} — empty text, skipping")
        return 0

    # Get linked entity names — prioritise competition/federation/rights_holder entities
    # for the entity_name field; include all so the LLM can also use broadcaster names
    # as entity_name if the content is genuinely about that broadcaster's rights.
    linked_entities = db.get_entities_for_entry(eid)
    if not linked_entities:
        print(f"  #{eid} [{etype}] {source[:60]} — no linked entities, skipping")
        return 0

    # Primary targets first so LLM picks them for entity_name; broadcasters last
    _RIGHTS_TYPES = {"competition", "federation", "rights_holder", "client", "other"}
    primary   = [e for e in linked_entities if e.get("entity_type") in _RIGHTS_TYPES]
    secondary = [e for e in linked_entities if e.get("entity_type") not in _RIGHTS_TYPES]
    ordered   = primary + secondary
    entity_names = [e["canonical_name"] for e in ordered]
    print(f"  #{eid} [{etype}] {source[:60]}")
    print(f"    entities: {', '.join(entity_names)}")
    print(f"    text: {len(full_text)} chars")

    # Run deal extraction
    raw_deals = extract_deals(full_text, entity_names, source_hint=source)
    if not raw_deals:
        print(f"    → 0 deals extracted")
        return 0

    print(f"    → {len(raw_deals)} deal(s) extracted")

    if dry_run:
        for d in raw_deals:
            parts = []
            if d.get("entity_name"): parts.append(f"entity={d['entity_name']}")
            if d.get("territory"):   parts.append(f"territory={d['territory']}")
            if d.get("broadcaster"): parts.append(f"broadcaster={d['broadcaster']}")
            if d.get("value"):       parts.append(f"value={d['value']}{d.get('currency','')}")
            print(f"      DRY: {' · '.join(parts)}")
        return len(raw_deals)

    saved = 0
    for d in raw_deals:
        en = (d.get("entity_name") or "").strip()
        entity_row = db.find_entity_by_name_or_alias(en)
        if not entity_row:
            print(f"      skip (no entity match for '{en}')")
            continue
        confidence = d.get("confidence", "medium")
        deal_status = "unverified" if confidence == "low" else "current"
        deal_id = db.add_deal(
            entity_id       = entity_row["id"],
            territory       = d.get("territory", ""),
            broadcaster     = d.get("broadcaster", ""),
            rights_holder   = d.get("rights_holder", ""),
            value           = d.get("value"),
            currency        = d.get("currency", ""),
            value_note      = d.get("value_note", ""),
            period_start    = d.get("period_start", ""),
            period_end      = d.get("period_end", ""),
            platform        = d.get("platform", ""),
            source_entry_id = eid,
            source_note     = source,
            status          = deal_status,
            reliability     = rel,
        )
        saved += 1
        ter = d.get("territory", "")[:25]
        bc  = d.get("broadcaster", "")[:25]
        print(f"      saved #{deal_id}: {ter} / {bc}")

    return saved


def main():
    parser = argparse.ArgumentParser(description="Backfill deal extraction for entries with no deals")
    parser.add_argument("--entry", type=int, help="Process a specific entry ID only")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be extracted without saving")
    parser.add_argument("--all", dest="all_entries", action="store_true",
                        help="Process all entries even if they already have deals")
    args = parser.parse_args()

    db.init_db()

    if args.entry:
        # Single entry mode
        all_entries = db.get_all_entries()
        entry = next((e for e in all_entries if e["id"] == args.entry), None)
        if not entry:
            print(f"Entry #{args.entry} not found")
            sys.exit(1)
        targets = [entry]
    else:
        import sqlite3
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row

        if args.all_entries:
            rows = con.execute(
                "SELECT * FROM entries ORDER BY entry_type, created_at"
            ).fetchall()
        else:
            # Only entries with zero deals currently linked to them
            rows = con.execute("""
                SELECT e.*
                FROM entries e
                WHERE NOT EXISTS (
                    SELECT 1 FROM deals d WHERE d.source_entry_id = e.id
                )
                  AND e.ingest_error = ''
                ORDER BY e.entry_type, e.created_at
            """).fetchall()
        con.close()
        targets = [dict(r) for r in rows]

    if not targets:
        print("No entries need backfilling.")
        return

    mode = "DRY RUN — " if args.dry_run else ""
    print(f"{mode}Backfilling {len(targets)} entries...\n")

    total_saved = 0
    for entry in targets:
        n = backfill_entry(entry, dry_run=args.dry_run)
        total_saved += n

    print(f"\n{'Would save' if args.dry_run else 'Saved'} {total_saved} deal row(s) across {len(targets)} entries.")


if __name__ == "__main__":
    main()

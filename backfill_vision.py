#!/usr/bin/env python3
"""
backfill_vision.py — Re-process image-heavy pages/slides with Claude vision.

Targets:
  - PDF pages containing "[page N — no extractable text]" (no text extracted)
  - PDF pages / PPTX slides with very short text but embedded images (sparse)
  - PDF pages / PPTX slides with significant embedded images + modest text (hybrid,
    e.g. broadcaster-logo/territory tables) — requires --hybrid flag

Usage:
  python3 backfill_vision.py              # sparse image pages only
  python3 backfill_vision.py --hybrid     # also catch hybrid logo-table pages
  python3 backfill_vision.py --entry 42   # specific entry
  python3 backfill_vision.py --dry-run    # show what would be processed without writing
  python3 backfill_vision.py --force      # re-run even if [Vision-extracted] already present
"""
import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb import db
from kb.llm import vision_sdk_available, extract_deals
from kb.ingest import (
    Chunk, ExtractionResult, extract,
    _render_pdf_page_image, _extract_pptx_slide_images,
    _vision_enrich_async, _should_vision_chunk,
)
from config import DB_PATH, VISION_CHAR_THRESHOLD


async def backfill_entry(
    entry: dict,
    dry_run: bool = False,
    force: bool = False,
    hybrid: bool = False,
) -> int:
    """Returns number of chunks updated."""
    eid    = entry["id"]
    source = entry["source"]
    fpath  = entry.get("file_path", "")
    ftype  = (entry.get("file_type") or "").lower()

    if not fpath or not Path(fpath).exists():
        print(f"  #{eid} [{ftype}] {source[:60]} — file not found, skipping")
        return 0

    path = Path(fpath)
    if path.suffix.lower() not in (".pdf", ".pptx"):
        return 0

    # Always re-extract so we get image_count populated per chunk.
    fresh_result = extract(path)
    if fresh_result.error:
        print(f"  #{eid} [{ftype}] {source[:60]} — extraction error: {fresh_result.error}")
        return 0

    # Load current DB chunks (may include previous vision or OCR enrichment).
    chunks_by  = db.get_chunks_for_entries([eid])
    raw_chunks = chunks_by.get(eid, [])
    if not raw_chunks:
        print(f"  #{eid} [{ftype}] {source[:60]} — no chunks in DB, skipping")
        return 0

    # Build merged chunks: image_count from fresh extraction, text from DB.
    # DB text is preferred — it may contain OCR or prior vision content.
    db_text_by_num = {c["chunk_num"]: (c["text"] or "") for c in raw_chunks}
    merged_chunks = []
    for fc in fresh_result.chunks:
        db_text = db_text_by_num.get(fc.chunk_num, fc.text)
        merged_chunks.append(
            Chunk(fc.chunk_num, fc.chunk_type, db_text, image_count=fc.image_count)
        )

    # Determine which chunks need vision.
    if force:
        targets = [i for i, c in enumerate(merged_chunks) if _should_vision_chunk(c)]
    else:
        targets = [
            i for i, c in enumerate(merged_chunks)
            if _should_vision_chunk(c) and "[Vision-extracted]" not in c.text
        ]

    if not targets:
        print(f"  #{eid} [{ftype}] {source[:60]} — no pages need vision")
        return 0

    n_sparse = sum(
        1 for i in targets
        if len(merged_chunks[i].text.strip()) < VISION_CHAR_THRESHOLD
    )
    n_hybrid = len(targets) - n_sparse
    label = f"{len(targets)} page(s)"
    if n_sparse or n_hybrid:
        parts = []
        if n_sparse:
            parts.append(f"{n_sparse} sparse")
        if n_hybrid:
            parts.append(f"{n_hybrid} hybrid")
        label += f" ({', '.join(parts)})"
    print(f"  #{eid} [{ftype}] {source[:60]} — {label}")

    if dry_run:
        for i in targets:
            c = merged_chunks[i]
            img_note = f", {c.image_count} images" if c.image_count else ""
            print(f"    would process: {c.chunk_type} {c.chunk_num} ({len(c.text)} chars{img_note})")
        return len(targets)

    result   = ExtractionResult(file_type=fresh_result.file_type, chunks=merged_chunks)
    enriched = await _vision_enrich_async(path, result)

    updated = 0
    for old, new in zip(merged_chunks, enriched.chunks):
        if new.text != old.text:
            con = sqlite3.connect(str(DB_PATH))
            con.execute(
                "UPDATE chunks SET text=? WHERE entry_id=? AND chunk_num=? AND chunk_type=?",
                (new.text, eid, new.chunk_num, new.chunk_type),
            )
            con.commit()
            con.close()
            print(f"    updated {new.chunk_type} {new.chunk_num}: {len(new.text)} chars")
            updated += 1

    if updated:
        db.index_entry(eid)

        linked_entities = db.get_entities_for_entry(eid)
        if linked_entities:
            full_text = "\n\n".join(c.text for c in enriched.chunks if c.text.strip())
            entity_names = [e["canonical_name"] for e in linked_entities]
            try:
                raw_deals = extract_deals(full_text, entity_names, source_hint=source)
                saved = 0
                rel = entry.get("reliability", "reported") or "reported"
                for d in raw_deals:
                    en = (d.get("entity_name") or "").strip()
                    entity_row = db.find_entity_by_name_or_alias(en)
                    if not entity_row:
                        continue
                    confidence  = d.get("confidence", "medium")
                    deal_status = "unverified" if confidence == "low" else "current"
                    db.add_deal(
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
                if saved:
                    print(f"    → {saved} deal(s) extracted from vision content")
            except Exception as e:
                print(f"    deal extraction failed: {e}")

    return updated


async def main():
    parser = argparse.ArgumentParser(description="Backfill vision analysis for image-heavy pages")
    parser.add_argument("--entry",   type=int, help="Process a specific entry ID")
    parser.add_argument("--hybrid",  action="store_true",
                        help="Also target hybrid pages: images + modest text (e.g. logo/broadcaster tables)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true", help="Re-run even if already vision-enriched")
    args = parser.parse_args()

    if not args.dry_run and not vision_sdk_available():
        print("ERROR: ANTHROPIC_API_KEY is not set. Vision analysis requires the SDK.")
        print("       Set ANTHROPIC_API_KEY and re-run.")
        sys.exit(1)

    db.init_db()

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    if args.entry:
        rows = con.execute("SELECT * FROM entries WHERE id=?", (args.entry,)).fetchall()
    elif args.hybrid:
        # For hybrid mode, examine all PDF/PPTX entries — re-extraction determines
        # which pages need vision (can't query image_count from DB).
        rows = con.execute("""
            SELECT DISTINCT e.*
            FROM entries e
            WHERE e.file_type IN ('PDF', 'PowerPoint')
              AND e.ingest_error = ''
            ORDER BY e.created_at DESC
        """).fetchall()
    else:
        rows = con.execute("""
            SELECT DISTINCT e.*
            FROM entries e
            JOIN chunks c ON c.entry_id = e.id
            WHERE e.file_type IN ('PDF', 'PowerPoint')
              AND e.ingest_error = ''
              AND (
                length(trim(c.text)) < ?
                OR c.text LIKE '%no extractable text%'
              )
            ORDER BY e.created_at DESC
        """, (VISION_CHAR_THRESHOLD,)).fetchall()
    con.close()

    targets = [dict(r) for r in rows]
    if not targets:
        print("No entries need vision backfill.")
        return

    mode = "DRY RUN — " if args.dry_run else ""
    print(f"{mode}Vision backfill for {len(targets)} entries...\n")

    total = 0
    for entry in targets:
        n = await backfill_entry(
            entry,
            dry_run=args.dry_run,
            force=args.force,
            hybrid=args.hybrid,
        )
        total += n

    print(f"\n{'Would update' if args.dry_run else 'Updated'} {total} chunk(s).")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
backfill_deal_entities.py — Populate deal_entities for existing deal rows.

The deal_entities join table (deal_id, entity_id, role) lets a deal be found
from its property's page AND every market/broadcaster page it touches, not
just deals.entity_id's page (which only ever points at the property). Every
app startup already auto-backfills the 'property' link from deals.entity_id
in kb.db.init_db() — that's a lossless 1:1 copy with zero ambiguity, so it
needs no review.

This script does the other half: splitting each existing deal's territory
and broadcaster strings on ; / , and resolving each part against the entities
table (market/broadcaster respectively) — the same resolution add_deal() now
runs at write time. That's a judgment call (name/alias matching), not a
lossless copy, so this defaults to dry-run/report-only and makes NO writes
unless you pass --apply. Read the dry-run report before applying anything.

Run against whichever database SN1_DB_PATH resolves to (see config.py) —
pointing it at Render's persistent-disk path backfills production without
any code changes.

Usage:
  python3 backfill_deal_entities.py             # report only, no writes
  python3 backfill_deal_entities.py --apply     # write the links shown
"""

import argparse
import sqlite3

from config import DB_PATH
from kb.db import split_multi_value, resolve_entity_ids, link_deal_to_entity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the links shown in the report (default: dry-run only).",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    print(f"Database: {DB_PATH}")
    print(f"Mode: {'DRY RUN (no writes)' if dry_run else 'APPLY (writing links)'}\n")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    deals = conn.execute("SELECT * FROM deals WHERE deleted_at IS NULL ORDER BY id").fetchall()
    entity_names = {r["id"]: r["canonical_name"] for r in conn.execute("SELECT id, canonical_name FROM entities").fetchall()}
    print(f"Checking {len(deals)} deal(s)...\n")

    n_new_links = 0
    n_deals_touched = 0
    n_unchanged = 0

    for d in deals:
        existing_links = {
            (r["entity_id"], r["role"])
            for r in conn.execute(
                "SELECT entity_id, role FROM deal_entities WHERE deal_id=?", (d["id"],)
            ).fetchall()
        }

        market_ids = resolve_entity_ids(split_multi_value(d["territory"] or ""), "market", path=DB_PATH)
        broadcaster_ids = resolve_entity_ids(split_multi_value(d["broadcaster"] or ""), "broadcaster", path=DB_PATH)

        to_link = [(eid, "market") for eid in market_ids if (eid, "market") not in existing_links]
        to_link += [(eid, "broadcaster") for eid in broadcaster_ids if (eid, "broadcaster") not in existing_links]

        if not to_link:
            n_unchanged += 1
            continue

        n_deals_touched += 1
        n_new_links += len(to_link)
        link_desc = ", ".join(f"{entity_names.get(eid, f'#{eid}')} ({role})" for eid, role in to_link)
        print(f"  Deal #{d['id']}  territory={d['territory']!r}  broadcaster={d['broadcaster']!r}")
        print(f"    → would link: {link_desc}")

        if not dry_run:
            for eid, role in to_link:
                link_deal_to_entity(d["id"], eid, role, path=DB_PATH)

    print()
    action = "Linked" if not dry_run else "Would link"
    print(f"{action} {n_new_links} new entity link(s) across {n_deals_touched} deal(s).")
    print(f"{n_unchanged} deal(s) already fully linked (or had no resolvable market/broadcaster).")

    if dry_run and n_new_links:
        print("\nThis was a dry run — nothing was written. Re-run with --apply to apply it.")


if __name__ == "__main__":
    main()

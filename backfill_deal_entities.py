#!/usr/bin/env python3
"""
backfill_deal_entities.py — Populate deal_entities for existing deal rows.

The deal_entities join table (deal_id, entity_id, role) lets a deal be found
from its property's page AND every market/broadcaster page it touches, not
just deals.entity_id's page (which only ever points at the property). Every
app startup already auto-backfills the 'property' link from deals.entity_id
in kb.db.init_db() — that's a lossless 1:1 copy with zero ambiguity, so it
needs no review.

This module does the other half: splitting each existing deal's territory
and broadcaster strings on ; / , and resolving each part against the entities
table (market/broadcaster respectively) — the same resolution add_deal() now
runs at write time. That's a judgment call (name/alias matching), not a
lossless copy, so compute_plan() never writes anything — call apply_plan()
separately once you've reviewed the plan. apply_plan() is additive only:
every write is INSERT OR IGNORE into deal_entities — it never updates or
deletes a deals row or an existing deal_entities link.

Used two ways:
  - CLI, against whichever database SN1_DB_PATH resolves to (see config.py):
      python3 backfill_deal_entities.py             # report only, no writes
      python3 backfill_deal_entities.py --apply     # write the links shown
  - Admin page (pages/admin.py, "Backfill deal entity links" expander) —
    imports compute_plan()/apply_plan() directly rather than duplicating this
    logic, for databases (e.g. Render) with no shell access to run the CLI.
"""

import argparse
import sqlite3

from config import DB_PATH
from kb.db import split_multi_value, find_entity_by_name_or_alias, link_deal_to_entity


_BROAD_REGION_DENYLIST = {
    "global", "worldwide", "international", "rest of world", "row", "world",
    "europe", "asia", "africa", "americas", "oceania",
    "north america", "south america", "central america",
}


def is_broad_region(text: str) -> bool:
    """
    True for continental/global catch-all labels ("Europe", "Global",
    "Asia") that shouldn't become market entities even once seeded: once
    individual countries (France, Germany, Portugal) are tracked, a deal
    tagged only "Europe" isn't a genuine distinct rights territory — it's
    vague source text that would just create a confusing, overlapping
    rollup. Established multi-country GROUPINGS this app already treats as
    real markets (MENA, Nordics, Balkans, DACH, Sub-Saharan Africa) are
    deliberately NOT on this list — the distinction is "bounded, commercially
    meaningful territory" vs. "broad continental/global label used as a lazy
    fallback", not simply single- vs. multi-country.
    """
    return text.strip().lower() in _BROAD_REGION_DENYLIST


def compute_plan(path=DB_PATH) -> dict:
    """
    Compute what backfilling deal_entities' market/broadcaster links would
    do, without writing anything. Returns:
      total_deals            — deals examined
      to_link                — [{deal_id, territory, broadcaster,
                                  links: [(entity_id, role, entity_name), ...]}]
                                deals with at least one new link to write
      n_new_links            — total links across all of to_link
      n_deals_touched        — len(to_link)
      n_already_linked       — deals where everything resolvable is already linked
      fully_unresolved_deal_ids — deal ids where NEITHER territory nor
                                broadcaster resolved to any entity at all —
                                these deals get NO market/broadcaster links at
                                all until at least one side resolves.
      unresolved_territory / unresolved_broadcaster — every distinct raw
                                string (post ; / , split) that failed to
                                resolve, across ALL deals — not just the
                                fully-unresolved ones, since a deal with one
                                resolved and one unresolved side still leaves
                                a real gap on the unresolved side's market/
                                broadcaster page. Each entry has {text, count,
                                deal_ids}; territory entries also carry
                                broad_region (see is_broad_region) so the
                                caller can default those out of bulk entity
                                creation. Sorted by count desc — the "which
                                entities are missing" signal for deciding
                                what to seed next.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    deals = conn.execute("SELECT * FROM deals WHERE deleted_at IS NULL ORDER BY id").fetchall()

    to_link: list[dict] = []
    n_new_links = 0
    n_already_linked = 0
    fully_unresolved_deal_ids: list[int] = []
    unresolved_territory: dict[str, dict] = {}
    unresolved_broadcaster: dict[str, dict] = {}

    for d in deals:
        territory_parts = split_multi_value(d["territory"] or "")
        broadcaster_parts = split_multi_value(d["broadcaster"] or "")

        resolved_market = []
        for part in territory_parts:
            row = find_entity_by_name_or_alias(part, entity_type="market", path=path)
            if row:
                resolved_market.append((row["id"], row["canonical_name"]))
            else:
                key = part.strip().lower()
                bucket = unresolved_territory.setdefault(key, {"text": part.strip(), "deal_ids": set()})
                bucket["deal_ids"].add(d["id"])

        resolved_broadcaster = []
        for part in broadcaster_parts:
            row = find_entity_by_name_or_alias(part, entity_type="broadcaster", path=path)
            if row:
                resolved_broadcaster.append((row["id"], row["canonical_name"]))
            else:
                key = part.strip().lower()
                bucket = unresolved_broadcaster.setdefault(key, {"text": part.strip(), "deal_ids": set()})
                bucket["deal_ids"].add(d["id"])

        if not resolved_market and not resolved_broadcaster:
            fully_unresolved_deal_ids.append(d["id"])
            continue

        existing_links = {
            (r["entity_id"], r["role"]) for r in conn.execute(
                "SELECT entity_id, role FROM deal_entities WHERE deal_id=?", (d["id"],)
            ).fetchall()
        }
        new_links = [(eid, "market", name) for eid, name in resolved_market if (eid, "market") not in existing_links]
        new_links += [(eid, "broadcaster", name) for eid, name in resolved_broadcaster if (eid, "broadcaster") not in existing_links]

        if new_links:
            to_link.append({
                "deal_id": d["id"], "territory": d["territory"], "broadcaster": d["broadcaster"],
                "links": new_links,
            })
            n_new_links += len(new_links)
        else:
            n_already_linked += 1

    def _summarize(bucket: dict, is_territory: bool) -> list[dict]:
        rows = []
        for v in bucket.values():
            row = {"text": v["text"], "count": len(v["deal_ids"])}
            if is_territory:
                row["broad_region"] = is_broad_region(v["text"])
            rows.append(row)
        return sorted(rows, key=lambda r: -r["count"])

    return {
        "total_deals": len(deals),
        "to_link": to_link,
        "n_new_links": n_new_links,
        "n_deals_touched": len(to_link),
        "n_already_linked": n_already_linked,
        "fully_unresolved_deal_ids": fully_unresolved_deal_ids,
        "unresolved_territory": _summarize(unresolved_territory, is_territory=True),
        "unresolved_broadcaster": _summarize(unresolved_broadcaster, is_territory=False),
    }


def apply_plan(plan: dict, path=DB_PATH) -> int:
    """
    Write every link in plan['to_link']. Additive only: each write is
    link_deal_to_entity() → INSERT OR IGNORE INTO deal_entities — never an
    UPDATE or DELETE against deals or an existing deal_entities row. Returns
    the number of links written.
    """
    n = 0
    for item in plan["to_link"]:
        for entity_id, role, _name in item["links"]:
            link_deal_to_entity(item["deal_id"], entity_id, role, path=path)
            n += 1
    return n


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

    plan = compute_plan(DB_PATH)
    print(f"Checking {plan['total_deals']} deal(s)...\n")

    for item in plan["to_link"]:
        link_desc = ", ".join(f"{name} ({role})" for _eid, role, name in item["links"])
        print(f"  Deal #{item['deal_id']}  territory={item['territory']!r}  broadcaster={item['broadcaster']!r}")
        print(f"    → would link: {link_desc}")

    n_written = apply_plan(plan, DB_PATH) if not dry_run else plan["n_new_links"]

    print()
    action = "Would link" if dry_run else "Linked"
    print(f"{action} {n_written} new entity link(s) across {plan['n_deals_touched']} deal(s).")
    print(f"{plan['n_already_linked']} deal(s) already fully linked.")
    print(f"{len(plan['fully_unresolved_deal_ids'])} deal(s) had nothing resolvable at all.")

    if plan["unresolved_territory"]:
        print("\nUnresolved territory strings (no matching market entity):")
        for row in plan["unresolved_territory"][:30]:
            flag = "  [broad region — probably exclude]" if row["broad_region"] else ""
            print(f"  {row['count']:4d}  {row['text']}{flag}")

    if plan["unresolved_broadcaster"]:
        print("\nUnresolved broadcaster strings (no matching broadcaster entity):")
        for row in plan["unresolved_broadcaster"][:30]:
            print(f"  {row['count']:4d}  {row['text']}")

    if dry_run and plan["n_new_links"]:
        print("\nThis was a dry run — nothing was written. Re-run with --apply to apply it.")


if __name__ == "__main__":
    main()

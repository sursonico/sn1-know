#!/usr/bin/env python3
"""
repair_sky_broadcasters.py — Split "Sky Deutschland" and "Sky Italia" out of
the "Sky Sports" broadcaster entity, and re-point the deals that were
collapsed into it.

Background: seed_entities.py seeded "Sky Sports" with aliases
"Sky,Sky UK,Sky Deutschland,Sky Italia" — collapsing three distinct regional
operating companies into one UK-branded entity. add_deal()'s
_normalize_deal_party() resolves broadcaster text through that alias list at
write time, so a deal whose source said "Sky Deutschland" or "Sky Italia"
got stored with broadcaster="Sky Sports" instead. (Sky Mexico is already
seeded as its own standalone entity with no aliases pointing back to Sky
Sports — the app's data model already treats other regional Sky operators as
distinct companies; Germany and Italy were the exception, not the rule.)

This script:
  1. compute_plan() reports the blast radius: every existing deal row with
     broadcaster='Sky Sports', classified by the row's own territory field
     into "genuinely UK" (left alone) vs. "Germany/DACH" or "Italy" (should
     become Sky Deutschland / Sky Italia). Each reclassified row is labeled
     'confirmed' (a chunk from its source entry literally names the regional
     brand) or 'inferred' (territory-only — no explicit regional-brand text
     found, e.g. the source just said bare "Sky"). Territory decides the
     classification either way — a bare "Sky" against a Germany-territory
     row is exactly the pattern that got mis-collapsed in the first place,
     so 'inferred' rows are still real bugs, just without a literal quote to
     point at. compute_plan() never writes anything.
  2. apply_plan() executes exactly the plan compute_plan() returned: first
     strips "Sky Deutschland"/"Sky Italia" out of Sky Sports' own alias list
     (they must go BEFORE the next step — find_or_create_entity() resolves
     by alias too, so with the old aliases still in place it would just
     resolve "Sky Deutschland" straight back to Sky Sports' id and silently
     no-op the split), then find_or_create_entity() the two new broadcaster
     entities (idempotent — safe to re-run), then for each flagged deal,
     update_deal() its broadcaster field and swap its deal_entities
     broadcaster link from Sky Sports to the new entity.

Usage:
  python3 repair_sky_broadcasters.py             # report only, no writes
  python3 repair_sky_broadcasters.py --apply      # write the changes shown

Also usable from the Admin page (see pages/admin.py, "Split Sky regional
broadcasters" expander) — imports compute_plan()/apply_plan() directly,
for a database with no shell access to run the CLI (e.g. Render).
"""

import argparse
import re
import sqlite3
from typing import Optional

from config import DB_PATH
from kb.db import (
    find_entity_by_name_or_alias, find_or_create_entity, get_entity, update_deal, update_entity,
    link_deal_to_entity, unlink_deal_from_entity,
)

SKY_SPORTS_NAME = "Sky Sports"

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_VALUE_RE = re.compile(r"([\$€£]|USD|EUR|GBP)\s*~?([\d]+\.?[\d]*)")


def _classify_territory(territory: str) -> Optional[str]:
    """
    Best-effort regional classification of an existing Sky Sports row by its
    own territory field. Returns the target canonical broadcaster name, or
    None when the territory doesn't clearly indicate a non-UK Sky operator
    (covers UK/Ireland rows, which are correctly left as Sky Sports).
    """
    t = (territory or "").lower()
    if "ital" in t:
        return "Sky Italia"
    if any(k in t for k in ("germany", "dach", "austria", "switzerland")):
        return "Sky Deutschland"
    return None


def _find_confirming_text(
    con: sqlite3.Connection, source_entry_id: Optional[int], target_name: str,
    value: Optional[float], currency: str,
) -> Optional[str]:
    """
    Best-effort evidence only: does a chunk from this deal's source entry
    name the target regional brand near a value+currency matching this
    row's own (within 5%, or 0.5 absolute for small figures)? A source
    document typically mentions the same brand multiple times for different
    properties/territories in the same deck, so matching on brand name
    alone would attribute one deal's confirming text to an unrelated deal
    that merely shares a territory — the value anchor is what ties a
    specific mention to THIS row. A deal with no stored value (nothing to
    anchor on) can never be 'confirmed' this way, only 'inferred'.
    Classification itself is territory-based regardless of what this finds
    — see module docstring.
    """
    if not source_entry_id or value is None:
        return None
    rows = con.execute(
        "SELECT text FROM chunks WHERE entry_id=?", (source_entry_id,)
    ).fetchall()
    for r in rows:
        text = r["text"] or ""
        for m in re.finditer(re.escape(target_name), text):
            window = text[max(0, m.start() - 80): m.end() + 100]
            for vm in _VALUE_RE.finditer(window):
                found_currency = _CURRENCY_SYMBOLS.get(vm.group(1), vm.group(1))
                if currency and found_currency != currency:
                    continue
                try:
                    found_val = float(vm.group(2))
                except ValueError:
                    continue
                if abs(found_val - value) <= max(0.5, value * 0.05):
                    return window.replace("\n", " ").strip()
    return None


def compute_plan(path=DB_PATH) -> dict:
    """
    Compute the blast radius and the re-pointing plan without writing
    anything. Returns:
      sky_sports_found  — False if no 'Sky Sports' broadcaster entity exists
                           at this path (nothing to do)
      sky_sports_id     — its entity id
      rows              — every deal row currently broadcaster='Sky Sports',
                           each: {deal_id, entity_id (property), territory,
                           value, currency, period_start, period_end,
                           source_entry_id, source_note, target_broadcaster
                           (None if left unchanged), confidence
                           ('confirmed'/'inferred'/None), evidence}
      n_total           — len(rows)
      n_reclassify       — rows with a target_broadcaster (would change)
      n_confirmed / n_inferred — split of n_reclassify by evidence strength
      n_unchanged        — rows staying broadcaster='Sky Sports' (UK/Ireland)
      new_entities        — canonical names apply_plan() will find_or_create
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    sky = find_entity_by_name_or_alias(SKY_SPORTS_NAME, entity_type="broadcaster", path=path)
    if not sky:
        return {
            "sky_sports_found": False, "rows": [], "n_total": 0, "n_reclassify": 0,
            "n_confirmed": 0, "n_inferred": 0, "n_unchanged": 0, "new_entities": [],
        }

    deals = conn.execute(
        "SELECT * FROM deals WHERE deleted_at IS NULL AND broadcaster=?",
        (SKY_SPORTS_NAME,),
    ).fetchall()

    rows = []
    for d in deals:
        target = _classify_territory(d["territory"])
        evidence = (
            _find_confirming_text(conn, d["source_entry_id"], target, d["value"], d["currency"])
            if target else None
        )
        rows.append({
            "deal_id": d["id"], "entity_id": d["entity_id"], "territory": d["territory"],
            "value": d["value"], "currency": d["currency"],
            "period_start": d["period_start"], "period_end": d["period_end"],
            "source_entry_id": d["source_entry_id"], "source_note": d["source_note"],
            "target_broadcaster": target,
            "confidence": ("confirmed" if evidence else "inferred") if target else None,
            "evidence": evidence,
        })

    to_change = [r for r in rows if r["target_broadcaster"]]
    return {
        "sky_sports_found": True, "sky_sports_id": sky["id"],
        "rows": rows,
        "n_total": len(rows),
        "n_reclassify": len(to_change),
        "n_confirmed": sum(1 for r in to_change if r["confidence"] == "confirmed"),
        "n_inferred": sum(1 for r in to_change if r["confidence"] == "inferred"),
        "n_unchanged": len(rows) - len(to_change),
        "new_entities": sorted({r["target_broadcaster"] for r in to_change}),
    }


def apply_plan(plan: dict, path=DB_PATH) -> int:
    """
    Write exactly what compute_plan() reported: find_or_create_entity() each
    new broadcaster (idempotent), then for every flagged row update_deal()
    its broadcaster field and swap its deal_entities broadcaster link from
    Sky Sports to the new entity. Returns the number of deals re-pointed.
    """
    if not plan.get("sky_sports_found") or not plan["new_entities"]:
        return 0

    sky_id = plan["sky_sports_id"]

    # Must happen BEFORE find_or_create_entity() below — as long as these
    # names are still Sky Sports' own aliases, resolving "Sky Deutschland"
    # would find Sky Sports itself (alias match) and hand back its id,
    # silently no-op'ing the split.
    sky = get_entity(sky_id, path=path)
    remaining_aliases = [
        a.strip() for a in (sky.get("aliases") or "").split(",")
        if a.strip() and a.strip().lower() not in {n.lower() for n in plan["new_entities"]}
    ]
    update_entity(sky_id, aliases=",".join(remaining_aliases), path=path)

    name_to_id = {
        name: find_or_create_entity(name, "broadcaster", path=path)
        for name in plan["new_entities"]
    }
    n = 0
    for r in plan["rows"]:
        target = r["target_broadcaster"]
        if not target:
            continue
        update_deal(r["deal_id"], broadcaster=target, path=path)
        unlink_deal_from_entity(r["deal_id"], sky_id, "broadcaster", path=path)
        link_deal_to_entity(r["deal_id"], name_to_id[target], "broadcaster", path=path)
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the re-pointing shown (default: dry-run only).",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    print(f"Database: {DB_PATH}")
    print(f"Mode: {'DRY RUN (no writes)' if dry_run else 'APPLY (writing changes)'}\n")

    plan = compute_plan(DB_PATH)
    if not plan["sky_sports_found"]:
        print("No 'Sky Sports' broadcaster entity found at this path — nothing to do.")
        return

    print(f"{plan['n_total']} deal row(s) currently have broadcaster = 'Sky Sports'.")
    print(f"  {plan['n_unchanged']} genuinely UK/Ireland — left unchanged.")
    print(f"  {plan['n_reclassify']} to re-point to a regional Sky entity:")
    print(f"    {plan['n_confirmed']} confirmed  — source text literally names the regional brand")
    print(f"    {plan['n_inferred']} inferred    — territory-only, no explicit regional-brand text found\n")

    for r in plan["rows"]:
        if not r["target_broadcaster"]:
            continue
        val = f"{r['value']}{r['currency']}" if r["value"] is not None else "—"
        period = f"{r['period_start']}–{r['period_end']}" if (r["period_start"] or r["period_end"]) else "—"
        print(
            f"  Deal #{r['deal_id']}  territory={r['territory']!r}  value={val}  period={period}  "
            f"source_entry_id={r['source_entry_id']}"
        )
        print(f"    → {r['target_broadcaster']}  [{r['confidence']}]" + (f"  evidence: …{r['evidence']}…" if r["evidence"] else ""))

    n_written = apply_plan(plan, DB_PATH) if not dry_run else plan["n_reclassify"]

    print()
    action = "Would re-point" if dry_run else "Re-pointed"
    print(f"{action} {n_written} deal(s) to {', '.join(plan['new_entities']) or '(none)'}.")

    if dry_run and plan["n_reclassify"]:
        print("\nThis was a dry run — nothing was written. Re-run with --apply to apply it.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
migrate_deal_values.py — One-time migration to separate numeric deal values
from descriptive qualifiers in deals.value_note.

After this migration:
  deals.value      = numeric amount in millions only (or null if truly undisclosed)
  deals.currency   = 3-letter currency code
  deals.value_note = ONLY qualifying phrase: 'per season', 'total', 'annually', etc.
                     (never a number, currency symbol, or monetary amount)

Run once:
  python3 migrate_deal_values.py

Safe to re-run — it skips rows that are already clean.
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "knowledge_base.db"

# Matches a standalone value expression at the START of value_note.
# Group 'sym'  → currency symbol  (optional)
# Group 'num'  → digits (with optional comma-thousands separator)
# Group 'unit' → 'm' / 'mn' / 'million' / 'bn' / 'billion'  (optional)
# Group 'rest' → everything after the value (the qualifier to keep)
_VALUE_RE = re.compile(
    r'^'
    r'(?P<approx>approx\.?\s*|~\s*|circa\s*|c\.\s*)?'
    r'(?P<sym>[£€$])?'
    r'(?P<num>[\d,]+(?:\.\d+)?)'
    r'\s*(?P<unit>m|mn|mil|million|bn|billion)\b'
    r'(?P<rest>.*)',
    re.IGNORECASE,
)

# Also match bare numbers with a currency symbol: "£300" or "$50"
_BARE_CURRENCY_RE = re.compile(
    r'^(?P<sym>[£€$])(?P<num>[\d,]+(?:\.\d+)?)(?P<rest>.*)$'
)

# Detect a value range — can't collapse to one number, flag it
_RANGE_RE = re.compile(r'\d\s*[-–—]\s*[£€$]?\d')

# Numbers that are NOT financial values (e.g. "3 seasons", "2 years", period years)
_COUNT_RE = re.compile(r'\d+\s*(season|year|match|game|round|leg|year)\b', re.IGNORECASE)

CURRENCY_MAP = {"£": "GBP", "€": "EUR", "$": "USD"}
UNIT_MULT = {
    "m": 1, "mn": 1, "mil": 1, "million": 1,
    "bn": 1000, "billion": 1000,
}


def _parse(note: str) -> dict:
    """
    Try to extract a numeric value from value_note.
    Returns:
      status   : "clean" | "flagged" | "unchanged"
      value    : float or None
      currency : str
      note     : cleaned qualifier string
    """
    note = note.strip()
    if not note:
        return {"status": "unchanged", "value": None, "currency": "", "note": note}

    # No digit at all → pure qualifier, nothing to fix
    if not re.search(r"\d", note):
        return {"status": "unchanged", "value": None, "currency": "", "note": note}

    # Count-style numbers only (e.g. "2 seasons") → not a monetary value, skip
    if _COUNT_RE.search(note) and not re.search(r"[£€$]|\d+m\b|\d+bn\b", note, re.IGNORECASE):
        return {"status": "unchanged", "value": None, "currency": "", "note": note}

    # Range detected → flag
    if _RANGE_RE.search(note):
        return {"status": "flagged", "value": None, "currency": "", "note": note}

    # Try standard pattern: [sym][num][unit][rest]
    m = _VALUE_RE.match(note)
    if not m:
        # Try bare currency+number: "£300" "€1500"
        m = _BARE_CURRENCY_RE.match(note)
        if not m:
            # Has a digit but didn't match any pattern → flag
            return {"status": "flagged", "value": None, "currency": "", "note": note}
        sym  = m.group("sym")
        num  = float(m.group("num").replace(",", ""))
        unit_mult = 1  # bare number — assume millions if it's a plausible deal value
        rest = (m.group("rest") or "").strip().lstrip("·-— ,").strip()
        currency = CURRENCY_MAP.get(sym, "")
        qualifier = ("approx.  " if False else "") + rest
        return {"status": "clean", "value": num * unit_mult, "currency": currency,
                "note": qualifier.strip()}

    approx = (m.group("approx") or "").strip()
    sym    = m.group("sym") or ""
    num    = float(m.group("num").replace(",", ""))
    unit   = (m.group("unit") or "").lower()
    rest   = (m.group("rest") or "").strip().lstrip("·-— ,").strip()

    value_millions = num * UNIT_MULT.get(unit, 1)
    currency = CURRENCY_MAP.get(sym, "")

    qualifier_parts = []
    if approx:
        qualifier_parts.append("approx.")
    if rest:
        qualifier_parts.append(rest)
    cleaned_note = "  ".join(qualifier_parts).strip()

    return {"status": "clean", "value": value_millions, "currency": currency,
            "note": cleaned_note}


def run():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    deals = con.execute("SELECT * FROM deals ORDER BY id").fetchall()
    print(f"Checking {len(deals)} deals...\n")

    updated: list[dict] = []
    flagged: list[dict] = []
    skipped = 0

    for d in deals:
        deal_id     = d["id"]
        value       = d["value"]
        currency    = (d["currency"]    or "").strip()
        value_note  = (d["value_note"]  or "").strip()
        territory   = (d["territory"]   or "").strip()
        broadcaster = (d["broadcaster"] or "").strip()

        if not value_note:
            skipped += 1
            continue

        # No digits → pure qualifier (e.g. "per season") — already correct
        if not re.search(r"\d", value_note):
            skipped += 1
            continue

        parsed = _parse(value_note)

        if parsed["status"] == "unchanged":
            skipped += 1
            continue

        if parsed["status"] == "flagged":
            flagged.append({
                "id": deal_id, "territory": territory, "broadcaster": broadcaster,
                "value": value, "value_note": value_note,
                "reason": "ambiguous — range or unrecognised format",
            })
            continue

        # "clean" parse
        new_value    = parsed["value"]
        new_currency = parsed["currency"] or currency
        new_note     = parsed["note"]

        if value is not None:
            # Value already set — check if value_note just repeats it
            if abs(value - new_value) < 0.5:
                # Same number: strip from note, keep qualifier
                con.execute(
                    "UPDATE deals SET value_note=?, updated_at=datetime('now') WHERE id=?",
                    (new_note, deal_id),
                )
                updated.append({
                    "id": deal_id, "territory": territory, "broadcaster": broadcaster,
                    "old_note": value_note, "new_note": new_note,
                    "value": value, "currency_changed": new_currency != currency,
                    "action": "stripped duplicate number from note",
                })
            else:
                # Different number: can't auto-resolve
                flagged.append({
                    "id": deal_id, "territory": territory, "broadcaster": broadcaster,
                    "value": value, "value_note": value_note,
                    "reason": f"value={value} but note implies {new_value:.4g} — conflict",
                })
        else:
            # Value is null: extract from note
            con.execute(
                "UPDATE deals SET value=?, currency=?, value_note=?, updated_at=datetime('now') WHERE id=?",
                (new_value, new_currency, new_note, deal_id),
            )
            updated.append({
                "id": deal_id, "territory": territory, "broadcaster": broadcaster,
                "old_note": value_note, "new_note": new_note,
                "value": new_value, "currency": new_currency,
                "action": f"extracted value={new_value:.4g}m {new_currency} from note",
            })

    con.commit()
    con.close()

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"=== MIGRATION COMPLETE ===\n")
    print(f"Skipped (already clean): {skipped}")
    print(f"Updated: {len(updated)}")
    print(f"Flagged for review: {len(flagged)}\n")

    if updated:
        print("── Updated deals ──")
        for u in updated:
            print(f"  #{u['id']} {u['territory']} / {u['broadcaster']}")
            print(f"    {u['action']}")
            print(f"    note: '{u['old_note']}' → '{u['new_note']}'")
        print()

    if flagged:
        print("── Flagged for manual review ──")
        print("These were NOT changed — inspect and edit manually via Admin or the deal form:\n")
        for f in flagged:
            print(f"  Deal #{f['id']} — {f['territory']} / {f['broadcaster']}")
            print(f"    Current: value={f['value']},  value_note='{f['value_note']}'")
            print(f"    Reason:  {f['reason']}\n")
    else:
        print("No deals flagged — all ambiguous cases were resolved automatically.")


if __name__ == "__main__":
    run()

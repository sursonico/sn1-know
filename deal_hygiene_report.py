#!/usr/bin/env python3
"""
deal_hygiene_report.py — Report (and optionally apply) the two one-off deal
hygiene passes already implemented in kb/db.py:

  - run_deal_dedupe()          merges rows that duplicate an existing deal on
                                (entity, normalized territory, normalized
                                broadcaster, period_start, period_end) —
                                created before add_deal() deduped on write.
  - flag_deals_missing_currency()  flags legacy rows storing a bare numeric
                                    value with no currency (e.g. "2050.0m per
                                    season" with currency='') for manual review.

Defaults to dry-run / report-only — it makes NO database writes unless you
pass --apply. Always run without --apply first and read the report before
applying anything.

Run against whichever database SN1_DB_PATH resolves to (see config.py) —
this is the same env var the app itself uses, so pointing it at Render's
persistent-disk path reports against production without any code changes.

Usage:
  python3 deal_hygiene_report.py              # report only, no writes
  python3 deal_hygiene_report.py --apply      # apply the dedupe merges and
                                               # currency flags shown in the
                                               # dry-run report
"""

import argparse

from config import DB_PATH
from kb.db import run_deal_dedupe, flag_deals_missing_currency


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the changes shown in the report (default: dry-run only).",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    print(f"Database: {DB_PATH}")
    print(f"Mode: {'DRY RUN (no writes)' if dry_run else 'APPLY (writing changes)'}\n")

    print("=" * 70)
    print("DEDUPE — duplicate deal rows")
    print("=" * 70)
    dedupe_plan = run_deal_dedupe(dry_run=dry_run)
    if not dedupe_plan:
        print("No duplicate groups found.\n")
    else:
        for p in dedupe_plan:
            merge_ids = ", ".join(f"#{i}" for i in p["merge"])
            print(f"  KEEP #{p['keep']}  ←  merge {merge_ids}")
            print(f"    {p['entity']} / {p['territory']} / {p['broadcaster']} / {p['period']}")
            if p["fills"]:
                print(f"    fills on keeper: {p['fills']}")
            print()
        action = "Merged" if not dry_run else "Would merge"
        print(f"{action} {len(dedupe_plan)} duplicate group(s), "
              f"{sum(len(p['merge']) for p in dedupe_plan)} row(s) soft-deleted.\n")

    print("=" * 70)
    print("CURRENCY — value stored with no currency")
    print("=" * 70)
    currency_flags = flag_deals_missing_currency(dry_run=dry_run)
    if not currency_flags:
        print("No rows with a value and no currency found.\n")
    else:
        for f in currency_flags:
            print(f"  #{f['id']}  {f['entity']} / {f['territory']} / {f['broadcaster']}")
            print(f"    value={f['value']}  value_note={f['value_note']!r}")
            print(f"    {f['reason']}")
            print()
        action = "Flagged" if not dry_run else "Would flag"
        print(f"{action} {len(currency_flags)} row(s) for manual review.\n")

    if dry_run and (dedupe_plan or currency_flags):
        print("This was a dry run — nothing was written. Re-run with --apply to apply it.")


if __name__ == "__main__":
    main()

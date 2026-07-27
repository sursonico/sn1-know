"""
migrate_entities.py — Normalise existing entries onto the entity registry.

For each entry, pulls sport_tags / org_tags / market_tags / topic_tags,
matches tokens against seed entities (canonical + aliases), links matches,
then sends any remaining tokens to Claude for resolution.

Run after seed_entities.py:
    python seed_entities.py
    python migrate_entities.py
"""
import logging
from kb.db import (
    init_db, get_all_entries,
    find_entity_by_name_or_alias, find_or_create_entity, link_entry_to_entity,
)
from kb.llm import resolve_entities

log = logging.getLogger("sn1.migrate_entities")


def _tokens_from_entry(entry: dict) -> list[str]:
    """Split every tag field into individual tokens."""
    raw = ",".join(filter(None, [
        entry.get("sport_tags", ""),
        entry.get("org_tags", ""),
        entry.get("market_tags", ""),
    ]))
    return [t.strip() for t in raw.split(",") if t.strip()]


def migrate_one(entry: dict, use_llm: bool = True) -> tuple[int, int]:
    """
    Link entities for a single entry.
    Returns (matched_count, llm_proposed_count).
    """
    tokens = _tokens_from_entry(entry)
    if not tokens:
        return 0, 0

    matched: list[int] = []
    unmatched: list[str] = []

    for token in tokens:
        entity = find_entity_by_name_or_alias(token)
        if entity:
            link_entry_to_entity(entry["id"], entity["id"])
            matched.append(entity["id"])
        else:
            unmatched.append(token)

    # Ask Claude to resolve unmatched tokens
    llm_count = 0
    if unmatched and use_llm:
        pseudo_meta = {"sports_leagues": ", ".join(unmatched)}
        resolved = resolve_entities(pseudo_meta)
        for r in resolved:
            canonical = r.get("canonical", "").strip()
            if not canonical:
                continue
            # See if Claude resolved it to an existing entity
            entity = find_entity_by_name_or_alias(canonical)
            if entity:
                link_entry_to_entity(entry["id"], entity["id"])
            else:
                # Create as proposed — needs admin review
                eid = find_or_create_entity(
                    canonical,
                    r.get("type", "other"),
                    proposed=True,
                )
                link_entry_to_entity(entry["id"], eid)
                llm_count += 1

    return len(matched), llm_count


def migrate_all(use_llm: bool = True) -> None:
    init_db()
    entries = get_all_entries()
    log.info("Migrating %d entries onto entities…", len(entries))

    total_matched = total_proposed = 0
    for entry in entries:
        m, p = migrate_one(entry, use_llm=use_llm)
        total_matched   += m
        total_proposed  += p
        source = entry["source"][:50]
        log.info("  %-50s  %d linked  %d proposed", source, m, p)

    log.info("Done: %d entity links created, %d proposed entities queued for review",
             total_matched, total_proposed)
    print(f"\nDone: {total_matched} links  •  {total_proposed} proposed entities (see Admin tab)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    migrate_all()

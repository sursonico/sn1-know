"""
seed_entities.py — Load canonical entity list into the knowledge base.

Run once (or safely re-run — upsert_entity is idempotent):
    python seed_entities.py
"""
from kb.db import init_db, upsert_entity

# (canonical_name, entity_type, aliases_csv)
SEEDS: list[tuple[str, str, str]] = [
    # ── Competitions ──────────────────────────────────────────────────────
    ("UEFA Champions League",          "competition", "UCL,Champions League,CL"),
    ("UEFA Europa League",             "competition", "UEL,Europa League"),
    ("UEFA Europa Conference League",  "competition", "UECL,Conference League,UECL"),
    ("UEFA Women's Champions League",  "competition", "UWCL,Women's Champions League"),
    ("UEFA Women's European Championship","competition","Women's Euros,Women's European Championship"),
    ("Premier League",                 "competition", "English Premier League,EPL,PL"),
    ("Bundesliga",                     "competition", "German Bundesliga,1. Bundesliga"),
    ("LaLiga",                         "competition", "La Liga,Spanish La Liga"),
    ("Ligue 1",                        "competition", "French Ligue 1"),
    ("Serie A",                        "competition", "Italian Serie A"),
    ("Eredivisie",                     "competition", "Dutch Eredivisie"),
    ("FIFA World Cup",                 "competition", "World Cup,Men's World Cup"),
    ("FIFA Club World Cup",            "competition", "Club World Cup,FCWC"),
    ("FIFA Women's World Cup",         "competition", "Women's World Cup,WWC"),
    ("Olympic Games",                  "competition", "Olympics,Summer Olympics"),
    ("Winter Olympics",                "competition", "Winter Games"),
    ("NFL",                            "competition", "National Football League,American Football"),
    ("NHL",                            "competition", "National Hockey League,Ice Hockey"),
    ("NBA",                            "competition", "National Basketball Association,Basketball"),
    ("UFC",                            "competition", "Ultimate Fighting Championship,MMA"),
    ("MotoGP",                         "competition", "Moto GP,Motorsports"),
    ("Saudi Pro League",               "competition", "SPL,Saudi Football League"),
    ("Kings League",                   "competition", ""),
    ("Queens League",                  "competition", ""),
    ("Baller League",                  "competition", ""),
    # ── Federations / governing bodies ───────────────────────────────────
    ("UEFA",             "federation", "Union of European Football Associations"),
    ("FIFA",             "federation", "Fédération Internationale de Football Association"),
    ("EHF",             "federation", "European Handball Federation"),
    ("Volleyball World", "federation", "FIVB,World Volleyball"),
    ("IOC",             "federation", "International Olympic Committee"),
    ("World Athletics", "federation", "IAAF"),
    # ── Broadcasters ─────────────────────────────────────────────────────
    ("Amazon Prime Video", "broadcaster", "Amazon,Prime Video,Amazon Sports"),
    ("DAZN",               "broadcaster", ""),
    ("Paramount+",         "broadcaster", "Paramount Plus"),
    ("Sky Sports",         "broadcaster", "Sky,Sky UK,Sky Deutschland,Sky Italia"),
    ("ESPN",               "broadcaster", "ESPN+"),
    ("TNT Sports",         "broadcaster", "BT Sport,BT"),
    ("beIN Sports",        "broadcaster", "beIN"),
    ("Canal+",             "broadcaster", "Canal Plus"),
    ("Apple TV+",          "broadcaster", "Apple TV,Apple"),
    ("Netflix",            "broadcaster", ""),
    ("YouTube",            "broadcaster", "YouTube TV"),
    # ── Rights holders / commercial partners ──────────────────────────────
    ("Relevent Sports Group", "rights_holder", "Relevent"),
    ("Team Marketing",        "rights_holder", "UEFA Team Marketing"),
    # ── Markets ───────────────────────────────────────────────────────────
    ("United Kingdom",            "market", "UK,Britain,England"),
    ("Germany",                   "market", "German"),
    ("France",                    "market", "French"),
    ("Italy",                     "market", "Italian"),
    ("Spain",                     "market", "Spanish"),
    ("United States",             "market", "USA,US,America"),
    ("Middle East & North Africa","market", "MENA"),
    ("Asia Pacific",              "market", "APAC"),
]


def seed(verbose: bool = True) -> int:
    init_db()
    count = 0
    for canonical, etype, aliases in SEEDS:
        upsert_entity(canonical, etype, aliases, is_proposed=0)
        count += 1
        if verbose:
            print(f"  {etype:12}  {canonical}")
    return count


def seed_if_empty(verbose: bool = False) -> int:
    """Seed canonical entities only when the non-proposed entity table is empty."""
    init_db()
    from kb.db import get_stats

    if get_stats().get("entities", 0) > 0:
        return 0
    return seed(verbose=verbose)


if __name__ == "__main__":
    n = seed()
    print(f"\nSeeded {n} entities.")

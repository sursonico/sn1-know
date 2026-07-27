"""
kb/db.py — Database layer for the SN1 Knowledge Base.

Schema:
  entries        — one row per document or snippet
  chunks         — one row per page / slide / sheet
  search_idx     — FTS5 virtual table (porter tokenizer)
  search_idx_map — maps FTS rowid → entry_id + optional chunk_id
  entities       — canonical entity registry (competitions, broadcasters, …)
  entry_entities — many-to-many link between entries and entities
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import DB_PATH


# ── Schema ─────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type     TEXT NOT NULL CHECK(entry_type IN ('document','snippet')),
    source         TEXT NOT NULL DEFAULT '',
    entry_date     TEXT          DEFAULT '',
    file_type      TEXT          DEFAULT '',
    doc_type       TEXT          DEFAULT '',
    org_tags       TEXT          DEFAULT '',
    market_tags    TEXT          DEFAULT '',
    sport_tags     TEXT          DEFAULT '',
    topic_tags     TEXT          DEFAULT '',
    summary        TEXT          DEFAULT '',
    notes          TEXT          DEFAULT '',
    file_path      TEXT          DEFAULT '',
    content_hash   TEXT          DEFAULT '',
    is_duplicate   INTEGER       DEFAULT 0,
    ocr_used       INTEGER       DEFAULT 0,
    ingest_error   TEXT          DEFAULT '',
    status         TEXT          DEFAULT 'current',
    superseded_by  INTEGER       REFERENCES entries(id),
    created_at     TEXT          DEFAULT (datetime('now')),
    updated_at     TEXT          DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    chunk_num   INTEGER NOT NULL,
    chunk_type  TEXT    DEFAULT 'page',
    text        TEXT    NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(
    body,
    source,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    entity_type    TEXT NOT NULL DEFAULT 'other',
    aliases        TEXT          DEFAULT '',
    is_proposed    INTEGER       DEFAULT 0,
    is_featured    INTEGER       DEFAULT 0,
    overview       TEXT          DEFAULT '',
    overview_at    TEXT          DEFAULT '',
    created_at     TEXT          DEFAULT (datetime('now')),
    updated_at     TEXT          DEFAULT (datetime('now')),
    UNIQUE(canonical_name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS entry_entities (
    entry_id  INTEGER NOT NULL REFERENCES entries(id)  ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT    DEFAULT 'secondary',   -- 'primary' | 'secondary'
    PRIMARY KEY (entry_id, entity_id)
);

-- Add role column to existing DBs that were created without it
CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS search_idx_map (
    rowid    INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    chunk_id INTEGER
);

CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    territory       TEXT    DEFAULT '',
    broadcaster     TEXT    DEFAULT '',
    rights_holder   TEXT    DEFAULT '',
    value           REAL,
    currency        TEXT    DEFAULT '',
    value_note      TEXT    DEFAULT '',
    period_start    TEXT    DEFAULT '',
    period_end      TEXT    DEFAULT '',
    platform        TEXT    DEFAULT '',
    source_entry_id INTEGER REFERENCES entries(id),
    source_note     TEXT    DEFAULT '',
    status          TEXT    DEFAULT 'current',
    superseded_by   INTEGER REFERENCES deals(id),
    date_added      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);
"""


@dataclass
class Chunk:
    chunk_num: int
    chunk_type: str
    text: str


# ── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def _conn(path: Path = DB_PATH):
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Init ────────────────────────────────────────────────────────────────────

def init_db(path: Path = DB_PATH) -> None:
    """Create all tables and virtual tables if they don't exist."""
    with _conn(path) as con:
        con.executescript(_DDL)
        # Live migration: add role column to entry_entities if missing
        ee_cols = [r[1] for r in con.execute("PRAGMA table_info(entry_entities)").fetchall()]
        if "role" not in ee_cols:
            con.execute("ALTER TABLE entry_entities ADD COLUMN role TEXT DEFAULT 'secondary'")
        # Live migration: add freshness/conflict columns to entries if missing
        e_cols = [r[1] for r in con.execute("PRAGMA table_info(entries)").fetchall()]
        if "status" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN status TEXT DEFAULT 'current'")
        if "superseded_by" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN superseded_by INTEGER REFERENCES entries(id)")
        if "reliability" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN reliability TEXT DEFAULT 'reported'")
        # Live migration: add reliability to deals if missing
        d_cols = [r[1] for r in con.execute("PRAGMA table_info(deals)").fetchall()]
        if "reliability" not in d_cols:
            con.execute("ALTER TABLE deals ADD COLUMN reliability TEXT DEFAULT 'reported'")
        # Live migration: add is_featured to entities if missing
        ent_cols = [r[1] for r in con.execute("PRAGMA table_info(entities)").fetchall()]
        if "is_featured" not in ent_cols:
            con.execute("ALTER TABLE entities ADD COLUMN is_featured INTEGER DEFAULT 0")
        # Live migration: add coverage_period to entries if missing
        if "coverage_period" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN coverage_period TEXT DEFAULT ''")


# ── Entry reads ─────────────────────────────────────────────────────────────

def get_all_entries(path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM entries ORDER BY entry_type, created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_entries_by_ids(ids: list[int], path: Path = DB_PATH) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with _conn(path) as con:
        rows = con.execute(
            f"SELECT * FROM entries WHERE id IN ({ph})", list(ids)
        ).fetchall()
        id_order = {v: i for i, v in enumerate(ids)}
        return sorted([dict(r) for r in rows], key=lambda r: id_order.get(r["id"], 999))


def get_entries_needing_enrichment(path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        rows = con.execute("""
            SELECT * FROM entries
            WHERE entry_type='document'
              AND (summary='' OR topic_tags='')
              AND ingest_error=''
            ORDER BY created_at
        """).fetchall()
        return [dict(r) for r in rows]


def hash_exists(content_hash: str, path: Path = DB_PATH) -> bool:
    if not content_hash:
        return False
    with _conn(path) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM entries WHERE content_hash=?", (content_hash,)
        ).fetchone()[0]
        return n > 0


# ── Chunk reads ─────────────────────────────────────────────────────────────

def get_chunks_for_entries(
    entry_ids: list[int], path: Path = DB_PATH
) -> dict[int, list[dict]]:
    """Return {entry_id: [chunk_dict, ...]} for the given entry IDs."""
    if not entry_ids:
        return {}
    ph = ",".join("?" * len(entry_ids))
    with _conn(path) as con:
        rows = con.execute(
            f"SELECT * FROM chunks WHERE entry_id IN ({ph}) ORDER BY entry_id, chunk_num",
            list(entry_ids),
        ).fetchall()
    result: dict[int, list[dict]] = {eid: [] for eid in entry_ids}
    for r in rows:
        result[r["entry_id"]].append(dict(r))
    return result


# ── FTS search ──────────────────────────────────────────────────────────────

_FTS_STOP_WORDS = frozenset([
    "a","an","the","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","could",
    "should","may","might","must","shall","can",
    "who","what","where","when","why","how","which",
    "this","that","these","those","it","its","i","me","my",
    "we","our","you","your","they","their","he","she","him","her",
    "of","in","on","at","to","for","or","and","but","not","no",
    "so","as","by","from","with","about","through","into","over",
    "up","out","if","then","than","very","just","also",
])


def _build_fts_query(question: str) -> str:
    """
    Build an FTS5 query from a natural-language question.
    Filters stop words and joins content terms with OR so any relevant keyword
    triggers a hit; BM25 ranking then promotes entries that match more terms.
    Falls back to a single unquoted term for single-word queries.
    """
    tokens = [t.strip('.,!?;:\'"()[]').lower() for t in question.split()]
    content = [t for t in tokens if t and t not in _FTS_STOP_WORDS and len(t) > 2]
    if not content:
        # Whole question was stop words — search the full string verbatim
        return question.replace('"', '""')
    if len(content) == 1:
        return content[0]
    return " OR ".join(f'"{t}"' for t in content)


def fts_search(question: str, limit: int = 20, path: Path = DB_PATH) -> list[int]:
    """
    Run a porter-stemmed FTS5 query and return up to `limit` distinct entry IDs,
    ranked by BM25 relevance. Returns [] on query-parse errors.
    Stop words are stripped and terms are OR-combined so natural-language questions
    surface entries that contain any of the content keywords.
    """
    fts_query = _build_fts_query(question)

    with _conn(path) as con:
        try:
            rows = con.execute("""
                SELECT DISTINCT m.entry_id
                FROM search_idx s
                JOIN search_idx_map m ON s.rowid = m.rowid
                WHERE search_idx MATCH ?
                ORDER BY s.rank
                LIMIT ?
            """, (fts_query, limit * 4)).fetchall()
            # Deduplicate preserving BM25-rank order
            seen: set[int] = set()
            ids: list[int] = []
            for r in rows:
                eid = r[0]
                if eid not in seen:
                    seen.add(eid)
                    ids.append(eid)
                    if len(ids) >= limit:
                        break
            return ids
        except sqlite3.OperationalError:
            return []


# ── Entry writes ─────────────────────────────────────────────────────────────

def upsert_document(
    source: str,
    entry_date: str = "",
    coverage_period: str = "",
    file_type: str = "",
    doc_type: str = "",
    org_tags: str = "",
    market_tags: str = "",
    sport_tags: str = "",
    topic_tags: str = "",
    summary: str = "",
    notes: str = "",
    file_path: str = "",
    content_hash: str = "",
    is_duplicate: int = 0,
    ocr_used: int = 0,
    ingest_error: str = "",
    status: str = "current",
    reliability: str = "reported",
    path: Path = DB_PATH,
) -> int:
    """Insert or update a document entry, keyed on source (filename)."""
    with _conn(path) as con:
        existing = con.execute(
            "SELECT id FROM entries WHERE entry_type='document' AND source=?", (source,)
        ).fetchone()
        if existing:
            con.execute("""
                UPDATE entries SET
                    entry_date=?, coverage_period=?, file_type=?, doc_type=?,
                    org_tags=?, market_tags=?, sport_tags=?, topic_tags=?,
                    summary=?, notes=?, file_path=?, content_hash=?,
                    is_duplicate=?, ocr_used=?, ingest_error=?,
                    updated_at=datetime('now')
                WHERE id=?
            """, (
                entry_date, coverage_period, file_type, doc_type,
                org_tags, market_tags, sport_tags, topic_tags,
                summary, notes, file_path, content_hash,
                is_duplicate, ocr_used, ingest_error,
                existing["id"],
            ))
            return existing["id"]
        cur = con.execute("""
            INSERT INTO entries
                (entry_type, source, entry_date, coverage_period, file_type, doc_type,
                 org_tags, market_tags, sport_tags, topic_tags, summary, notes,
                 file_path, content_hash, is_duplicate, ocr_used, ingest_error, status, reliability)
            VALUES ('document',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            source, entry_date, coverage_period, file_type, doc_type,
            org_tags, market_tags, sport_tags, topic_tags,
            summary, notes, file_path, content_hash,
            is_duplicate, ocr_used, ingest_error, status, reliability,
        ))
        return cur.lastrowid


def add_snippet(
    source: str,
    entry_date: str,
    full_text: str,
    summary: str = "",
    coverage_period: str = "",
    org_tags: str = "",
    market_tags: str = "",
    sport_tags: str = "",
    topic_tags: str = "",
    status: str = "current",
    reliability: str = "reported",
    path: Path = DB_PATH,
) -> int:
    """Insert a snippet and its single chunk, returning the entry ID."""
    with _conn(path) as con:
        cur = con.execute("""
            INSERT INTO entries
                (entry_type, source, entry_date, coverage_period, file_type,
                 org_tags, market_tags, sport_tags, topic_tags, summary, status, reliability)
            VALUES ('snippet',?,?,?,'Note',?,?,?,?,?,?,?)
        """, (source, entry_date, coverage_period, org_tags, market_tags, sport_tags, topic_tags, summary, status, reliability))
        entry_id = cur.lastrowid
        # Store the full text as a single chunk
        con.execute(
            "INSERT INTO chunks (entry_id, chunk_num, chunk_type, text) VALUES (?,1,'body',?)",
            (entry_id, full_text),
        )
        return entry_id


def update_enrichment(entry_id: int, path: Path = DB_PATH, **fields) -> None:
    """Patch arbitrary metadata columns on an existing entry."""
    if not fields:
        return
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k}=datetime('now')" if v == "datetime('now')" else f"{k}=?"
        for k, v in fields.items()
    )
    values = [v for v in fields.values() if v != "datetime('now')"]
    with _conn(path) as con:
        con.execute(f"UPDATE entries SET {set_clause} WHERE id=?", values + [entry_id])


# ── Chunk writes ─────────────────────────────────────────────────────────────

def store_chunks(
    entry_id: int, chunks: list[Chunk], path: Path = DB_PATH
) -> list[int]:
    """Delete existing chunks for the entry and insert new ones. Returns new IDs."""
    with _conn(path) as con:
        con.execute("DELETE FROM chunks WHERE entry_id=?", (entry_id,))
        ids = []
        for c in chunks:
            cur = con.execute(
                "INSERT INTO chunks (entry_id, chunk_num, chunk_type, text) VALUES (?,?,?,?)",
                (entry_id, c.chunk_num, c.chunk_type, c.text),
            )
            ids.append(cur.lastrowid)
        return ids


# ── FTS index writes ──────────────────────────────────────────────────────────

def index_entry(entry_id: int, path: Path = DB_PATH) -> None:
    """
    (Re-)index an entry in search_idx.
    Indexes: per-chunk text rows + one entry-level row (summary + tags).
    """
    with _conn(path) as con:
        # Remove stale index rows for this entry
        stale_rowids = [
            r[0] for r in con.execute(
                "SELECT rowid FROM search_idx_map WHERE entry_id=?", (entry_id,)
            ).fetchall()
        ]
        if stale_rowids:
            ph = ",".join("?" * len(stale_rowids))
            con.execute(f"DELETE FROM search_idx WHERE rowid IN ({ph})", stale_rowids)
            con.execute(f"DELETE FROM search_idx_map WHERE rowid IN ({ph})", stale_rowids)

        entry = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            return
        entry = dict(entry)

        # Index-level row: summary + all tags concatenated
        tags_blob = " ".join(filter(None, [
            entry.get("sport_tags", ""), entry.get("topic_tags", ""),
            entry.get("org_tags", ""),  entry.get("market_tags", ""),
            entry.get("doc_type", ""),  entry.get("notes", ""),
        ]))
        entry_text = f"{entry.get('summary','')} {tags_blob}".strip()
        if entry_text:
            cur = con.execute(
                "INSERT INTO search_idx (body, source) VALUES (?,?)",
                (entry_text, entry["source"]),
            )
            con.execute(
                "INSERT INTO search_idx_map (rowid, entry_id) VALUES (?,?)",
                (cur.lastrowid, entry_id),
            )

        # Per-chunk rows
        chunks = con.execute(
            "SELECT * FROM chunks WHERE entry_id=? ORDER BY chunk_num", (entry_id,)
        ).fetchall()
        for chunk in chunks:
            if chunk["text"].strip():
                cur = con.execute(
                    "INSERT INTO search_idx (body, source) VALUES (?,?)",
                    (chunk["text"], entry["source"]),
                )
                con.execute(
                    "INSERT INTO search_idx_map (rowid, entry_id, chunk_id) VALUES (?,?,?)",
                    (cur.lastrowid, entry_id, chunk["id"]),
                )


# ── Stats ────────────────────────────────────────────────────────────────────

def get_stats(path: Path = DB_PATH) -> dict:
    with _conn(path) as con:
        total = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        docs  = con.execute(
            "SELECT COUNT(*) FROM entries WHERE entry_type='document'"
        ).fetchone()[0]
        snips = con.execute(
            "SELECT COUNT(*) FROM entries WHERE entry_type='snippet'"
        ).fetchone()[0]
        recent = con.execute("""
            SELECT source, entry_type, created_at FROM entries
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        n_entities = con.execute("SELECT COUNT(*) FROM entities WHERE is_proposed=0").fetchone()[0]
        return {
            "total": total, "documents": docs, "snippets": snips,
            "entities": n_entities,
            "recent": [dict(r) for r in recent],
        }


# ── Entity CRUD ───────────────────────────────────────────────────────────────

def upsert_entity(
    canonical_name: str,
    entity_type: str = "other",
    aliases: str = "",
    is_proposed: int = 0,
    path: Path = DB_PATH,
) -> int:
    """Insert entity if it doesn't exist; return its ID either way."""
    with _conn(path) as con:
        row = con.execute(
            "SELECT id FROM entities WHERE canonical_name=? COLLATE NOCASE",
            (canonical_name,),
        ).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases, is_proposed) VALUES (?,?,?,?)",
            (canonical_name, entity_type, aliases, is_proposed),
        )
        return cur.lastrowid


def find_entity_by_name_or_alias(name: str, path: Path = DB_PATH) -> Optional[dict]:
    """Return the entity row whose canonical_name or any alias matches `name` (case-insensitive)."""
    name_lo = name.strip().lower()
    with _conn(path) as con:
        # Exact canonical match
        row = con.execute(
            "SELECT * FROM entities WHERE LOWER(canonical_name)=?", (name_lo,)
        ).fetchone()
        if row:
            return dict(row)
        # Alias scan (comma-separated aliases column)
        rows = con.execute("SELECT * FROM entities").fetchall()
        for r in rows:
            aliases = [a.strip().lower() for a in (r["aliases"] or "").split(",") if a.strip()]
            if name_lo in aliases:
                return dict(r)
    return None


def find_or_create_entity(
    canonical_name: str,
    entity_type: str = "other",
    proposed: bool = False,
    path: Path = DB_PATH,
) -> int:
    """Find entity by name/alias; create (optionally as proposed) if not found."""
    existing = find_entity_by_name_or_alias(canonical_name, path)
    if existing:
        return existing["id"]
    return upsert_entity(canonical_name, entity_type, "", int(proposed), path)


def get_all_entities(
    include_proposed: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    with _conn(path) as con:
        q = "SELECT * FROM entities"
        if not include_proposed:
            q += " WHERE is_proposed=0"
        q += " ORDER BY canonical_name COLLATE NOCASE"
        return [dict(r) for r in con.execute(q).fetchall()]


def get_entity(entity_id: int, path: Path = DB_PATH) -> Optional[dict]:
    with _conn(path) as con:
        row = con.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        return dict(row) if row else None


def update_entity(entity_id: int, path: Path = DB_PATH, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k}=datetime('now')" if v == "datetime('now')" else f"{k}=?"
        for k, v in fields.items()
    )
    values = [v for v in fields.values() if v != "datetime('now')"]
    with _conn(path) as con:
        con.execute(f"UPDATE entities SET {set_clause} WHERE id=?", values + [entity_id])


def merge_entities(keep_id: int, discard_id: int, path: Path = DB_PATH) -> None:
    """Re-point all entry_entities rows from discard_id → keep_id, then delete discard."""
    with _conn(path) as con:
        # Move links (ignore conflicts — entry already linked to keep_id)
        con.execute("""
            INSERT OR IGNORE INTO entry_entities (entry_id, entity_id)
            SELECT entry_id, ? FROM entry_entities WHERE entity_id=?
        """, (keep_id, discard_id))
        con.execute("DELETE FROM entry_entities WHERE entity_id=?", (discard_id,))
        con.execute("DELETE FROM entities WHERE id=?", (discard_id,))


# ── Entry ↔ Entity links ──────────────────────────────────────────────────────

def link_entry_to_entity(
    entry_id: int,
    entity_id: int,
    role: str = "secondary",
    path: Path = DB_PATH,
) -> None:
    with _conn(path) as con:
        con.execute(
            "INSERT OR REPLACE INTO entry_entities (entry_id, entity_id, role) VALUES (?,?,?)",
            (entry_id, entity_id, role),
        )


def get_entities_for_entry(entry_id: int, path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        rows = con.execute("""
            SELECT e.* FROM entities e
            JOIN entry_entities ee ON ee.entity_id = e.id
            WHERE ee.entry_id = ?
            ORDER BY e.canonical_name COLLATE NOCASE
        """, (entry_id,)).fetchall()
        return [dict(r) for r in rows]


def get_entries_for_entity(
    entity_id: int,
    include_superseded: bool = True,
    path: Path = DB_PATH,
) -> list[dict]:
    with _conn(path) as con:
        status_filter = "" if include_superseded else "AND en.status != 'superseded'"
        rows = con.execute(f"""
            SELECT en.* FROM entries en
            JOIN entry_entities ee ON ee.entry_id = en.id
            WHERE ee.entity_id = ? {status_filter}
            ORDER BY en.entry_date DESC, en.created_at DESC
        """, (entity_id,)).fetchall()
        return [dict(r) for r in rows]


def mark_entry_superseded(
    entry_id: int,
    superseded_by_id: int,
    path: Path = DB_PATH,
) -> None:
    """Mark entry_id as superseded by superseded_by_id. Preserves the row — audit trail only."""
    with _conn(path) as con:
        con.execute(
            "UPDATE entries SET status='superseded', superseded_by=?, updated_at=datetime('now') WHERE id=?",
            (superseded_by_id, entry_id),
        )


def set_entry_status(entry_id: int, status: str, path: Path = DB_PATH) -> None:
    """Set entry status to 'current', 'superseded', or 'unverified'."""
    with _conn(path) as con:
        con.execute(
            "UPDATE entries SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, entry_id),
        )


def get_recent_entries_for_entities(
    entity_ids: list[int],
    limit: int = 10,
    path: Path = DB_PATH,
) -> list[dict]:
    """Return the most recent current/unverified entries linked to any of the given entity IDs."""
    if not entity_ids:
        return []
    ph = ",".join("?" * len(entity_ids))
    with _conn(path) as con:
        rows = con.execute(f"""
            SELECT DISTINCT en.*
            FROM entries en
            JOIN entry_entities ee ON ee.entry_id = en.id
            WHERE ee.entity_id IN ({ph})
              AND en.status != 'superseded'
            ORDER BY en.entry_date DESC, en.created_at DESC
            LIMIT ?
        """, list(entity_ids) + [limit]).fetchall()
        return [dict(r) for r in rows]


def get_entity_stats(path: Path = DB_PATH) -> list[dict]:
    """
    Return one row per entity with doc_count, snippet_count, and last_updated,
    sorted by total entry count descending (most active first).
    """
    with _conn(path) as con:
        rows = con.execute("""
            SELECT
                e.id, e.canonical_name, e.entity_type, e.aliases,
                e.is_proposed, e.is_featured, e.overview, e.overview_at,
                SUM(CASE WHEN en.entry_type='document' THEN 1 ELSE 0 END) AS doc_count,
                SUM(CASE WHEN en.entry_type='snippet'  THEN 1 ELSE 0 END) AS snip_count,
                COUNT(en.id) AS total_count,
                MAX(en.created_at) AS last_updated
            FROM entities e
            LEFT JOIN entry_entities ee ON ee.entity_id = e.id
            LEFT JOIN entries en        ON en.id = ee.entry_id
            WHERE e.is_proposed = 0
            GROUP BY e.id
            ORDER BY total_count DESC, e.canonical_name COLLATE NOCASE
        """).fetchall()
        return [dict(r) for r in rows]


def get_proposed_entities(path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM entities WHERE is_proposed=1 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Deals CRUD ────────────────────────────────────────────────────────────────

def add_deal(
    entity_id: int,
    territory: str = "",
    broadcaster: str = "",
    rights_holder: str = "",
    value: Optional[float] = None,
    currency: str = "",
    value_note: str = "",
    period_start: str = "",
    period_end: str = "",
    platform: str = "",
    source_entry_id: Optional[int] = None,
    source_note: str = "",
    status: str = "current",
    reliability: str = "reported",
    path: Path = DB_PATH,
) -> int:
    """Insert a deal row. Skips silently if identical deal already exists (dedup). Returns deal id."""
    with _conn(path) as con:
        existing = con.execute("""
            SELECT id FROM deals
            WHERE entity_id=?
              AND LOWER(TRIM(territory))=LOWER(TRIM(?))
              AND LOWER(TRIM(broadcaster))=LOWER(TRIM(?))
              AND TRIM(period_start)=TRIM(?)
              AND TRIM(period_end)=TRIM(?)
        """, (entity_id, territory, broadcaster, period_start, period_end)).fetchone()
        if existing:
            return existing["id"]
        cur = con.execute("""
            INSERT INTO deals
                (entity_id, territory, broadcaster, rights_holder, value, currency,
                 value_note, period_start, period_end, platform,
                 source_entry_id, source_note, status, reliability)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            entity_id, territory.strip(), broadcaster.strip(), rights_holder.strip(),
            value, currency.strip(), value_note.strip(),
            period_start.strip(), period_end.strip(), platform.strip(),
            source_entry_id, source_note.strip(), status, reliability,
        ))
        return cur.lastrowid


def get_deals_for_entity(
    entity_id: int,
    include_superseded: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    """Return deals for an entity, newest first."""
    with _conn(path) as con:
        status_filter = "" if include_superseded else "AND status != 'superseded'"
        rows = con.execute(f"""
            SELECT * FROM deals
            WHERE entity_id = ? {status_filter}
            ORDER BY date_added DESC
        """, (entity_id,)).fetchall()
        return [dict(r) for r in rows]


def get_deals_for_entities(
    entity_ids: list[int],
    include_superseded: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    """Return all deals for a set of entities, with entity_name joined in, sorted by entity then territory."""
    if not entity_ids:
        return []
    ph = ",".join("?" * len(entity_ids))
    status_filter = "" if include_superseded else "AND d.status != 'superseded'"
    with _conn(path) as con:
        rows = con.execute(f"""
            SELECT d.*, e.canonical_name AS entity_name
            FROM deals d
            JOIN entities e ON d.entity_id = e.id
            WHERE d.entity_id IN ({ph}) {status_filter}
            ORDER BY e.canonical_name COLLATE NOCASE, d.territory COLLATE NOCASE
        """, list(entity_ids)).fetchall()
    return [dict(r) for r in rows]


def get_deal(deal_id: int, path: Path = DB_PATH) -> Optional[dict]:
    with _conn(path) as con:
        row = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        return dict(row) if row else None


def update_deal(deal_id: int, path: Path = DB_PATH, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k}=datetime('now')" if v == "datetime('now')" else f"{k}=?"
        for k, v in fields.items()
    )
    values = [v for v in fields.values() if v != "datetime('now')"]
    with _conn(path) as con:
        con.execute(f"UPDATE deals SET {set_clause} WHERE id=?", values + [deal_id])


def mark_deal_superseded(deal_id: int, superseded_by_id: int, path: Path = DB_PATH) -> None:
    """Mark a deal as superseded. Preserves the row — audit trail only."""
    with _conn(path) as con:
        con.execute(
            "UPDATE deals SET status='superseded', superseded_by=?, updated_at=datetime('now') WHERE id=?",
            (superseded_by_id, deal_id),
        )


def find_conflicting_deal_territories(entity_id: int, path: Path = DB_PATH) -> set[str]:
    """Return lowercase territory names that have 2+ current deals for an entity."""
    with _conn(path) as con:
        rows = con.execute("""
            SELECT LOWER(TRIM(territory)) as t, COUNT(*) as n
            FROM deals
            WHERE entity_id=? AND status='current' AND TRIM(territory) != ''
            GROUP BY LOWER(TRIM(territory))
            HAVING n > 1
        """, (entity_id,)).fetchall()
        return {r["t"] for r in rows}

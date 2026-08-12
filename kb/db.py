"""
kb/db.py — Database layer for the SN1 Knowledge Base.

Schema:
  entries        — one row per document or snippet
  chunks         — one row per page / slide / sheet
  search_idx     — FTS5 virtual table (porter tokenizer)
  search_idx_map — maps FTS rowid → entry_id + optional chunk_id
  entities       — canonical entity registry (competitions, broadcasters, …)
  entry_entities — many-to-many link between entries and entities
  deals          — structured broadcaster/territory rows extracted from entries

Soft deletion:
  entries, deals and entities all carry a `deleted_at` timestamp. Every read in
  this module filters `deleted_at IS NULL`, so a soft-deleted row disappears from
  Browse, Ask/retrieval, entity hubs and stats while the record (and the original
  file on disk) survives. Rows cascaded out by an entry deletion also record
  `deleted_with_entry`, which is what `restore_entry()` uses to put them back.
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
    deleted_at     TEXT,
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
    deleted_at     TEXT,
    deleted_with_entry INTEGER,
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
    deleted_at      TEXT,
    deleted_with_entry INTEGER,
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
        # Live migration: soft-delete columns (entries, deals, entities)
        if "deleted_at" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN deleted_at TEXT")
        if "deleted_at" not in d_cols:
            con.execute("ALTER TABLE deals ADD COLUMN deleted_at TEXT")
        if "deleted_with_entry" not in d_cols:
            con.execute("ALTER TABLE deals ADD COLUMN deleted_with_entry INTEGER")
        if "deleted_at" not in ent_cols:
            con.execute("ALTER TABLE entities ADD COLUMN deleted_at TEXT")
        if "deleted_with_entry" not in ent_cols:
            con.execute("ALTER TABLE entities ADD COLUMN deleted_with_entry INTEGER")


# ── Entry reads ─────────────────────────────────────────────────────────────

def get_all_entries(path: Path = DB_PATH) -> list[dict]:
    """All live (non-deleted) entries."""
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM entries WHERE deleted_at IS NULL ORDER BY entry_type, created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_entries_by_ids(
    ids: list[int],
    include_deleted: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    del_filter = "" if include_deleted else " AND deleted_at IS NULL"
    with _conn(path) as con:
        rows = con.execute(
            f"SELECT * FROM entries WHERE id IN ({ph}){del_filter}", list(ids)
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
              AND deleted_at IS NULL
            ORDER BY created_at
        """).fetchall()
        return [dict(r) for r in rows]


def hash_exists(content_hash: str, path: Path = DB_PATH) -> bool:
    if not content_hash:
        return False
    with _conn(path) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM entries WHERE content_hash=? AND deleted_at IS NULL",
            (content_hash,),
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
            # Join entries so soft-deleted sources never surface as candidates —
            # the index rows are left in place so a restore needs no re-indexing.
            rows = con.execute("""
                SELECT DISTINCT m.entry_id
                FROM search_idx s
                JOIN search_idx_map m ON s.rowid = m.rowid
                JOIN entries en       ON en.id   = m.entry_id
                WHERE search_idx MATCH ?
                  AND en.deleted_at IS NULL
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
    """
    Insert or update a document entry, keyed on source (filename).

    If the matching entry was soft-deleted, re-ingesting the same source revives
    it along with the deal rows and entities that were cascaded out with it.
    """
    with _conn(path) as con:
        existing = con.execute(
            "SELECT id, deleted_at FROM entries WHERE entry_type='document' AND source=?",
            (source,),
        ).fetchone()
        if existing:
            if existing["deleted_at"]:
                _undelete_cascade(con, existing["id"])
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
        total = con.execute(
            "SELECT COUNT(*) FROM entries WHERE deleted_at IS NULL"
        ).fetchone()[0]
        docs  = con.execute(
            "SELECT COUNT(*) FROM entries WHERE entry_type='document' AND deleted_at IS NULL"
        ).fetchone()[0]
        snips = con.execute(
            "SELECT COUNT(*) FROM entries WHERE entry_type='snippet' AND deleted_at IS NULL"
        ).fetchone()[0]
        recent = con.execute("""
            SELECT source, entry_type, created_at FROM entries
            WHERE deleted_at IS NULL
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        n_entities = con.execute(
            "SELECT COUNT(*) FROM entities WHERE is_proposed=0 AND deleted_at IS NULL"
        ).fetchone()[0]
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
    """
    Insert entity if it doesn't exist; return its ID either way.
    A soft-deleted entity with the same name is revived rather than duplicated
    (canonical_name is UNIQUE).
    """
    with _conn(path) as con:
        row = con.execute(
            "SELECT id, deleted_at FROM entities WHERE canonical_name=? COLLATE NOCASE",
            (canonical_name,),
        ).fetchone()
        if row:
            if row["deleted_at"]:
                con.execute(
                    "UPDATE entities SET deleted_at=NULL, deleted_with_entry=NULL,"
                    " updated_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
            return row["id"]
        cur = con.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases, is_proposed) VALUES (?,?,?,?)",
            (canonical_name, entity_type, aliases, is_proposed),
        )
        return cur.lastrowid


def find_entity_by_name_or_alias(
    name: str,
    include_deleted: bool = False,
    path: Path = DB_PATH,
) -> Optional[dict]:
    """Return the entity row whose canonical_name or any alias matches `name` (case-insensitive)."""
    name_lo = name.strip().lower()
    del_filter = "" if include_deleted else " AND deleted_at IS NULL"
    with _conn(path) as con:
        # Exact canonical match
        row = con.execute(
            f"SELECT * FROM entities WHERE LOWER(canonical_name)=?{del_filter}", (name_lo,)
        ).fetchone()
        if row:
            return dict(row)
        # Alias scan (comma-separated aliases column)
        rows = con.execute(
            f"SELECT * FROM entities WHERE 1=1{del_filter}"
        ).fetchall()
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
    """
    Find entity by name/alias; create (optionally as proposed) if not found.
    A soft-deleted match is revived — a new source mentioning it brings it back.
    """
    existing = find_entity_by_name_or_alias(canonical_name, include_deleted=True, path=path)
    if existing:
        if existing.get("deleted_at"):
            update_entity(existing["id"], path=path, deleted_at=None, deleted_with_entry=None)
        return existing["id"]
    return upsert_entity(canonical_name, entity_type, "", int(proposed), path)


def get_all_entities(
    include_proposed: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    with _conn(path) as con:
        q = "SELECT * FROM entities WHERE deleted_at IS NULL"
        if not include_proposed:
            q += " AND is_proposed=0"
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
            WHERE ee.entry_id = ? AND e.deleted_at IS NULL
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
            WHERE ee.entity_id = ? AND en.deleted_at IS NULL {status_filter}
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
              AND en.deleted_at IS NULL
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
            LEFT JOIN entries en        ON en.id = ee.entry_id AND en.deleted_at IS NULL
            WHERE e.is_proposed = 0 AND e.deleted_at IS NULL
            GROUP BY e.id
            ORDER BY total_count DESC, e.canonical_name COLLATE NOCASE
        """).fetchall()
        return [dict(r) for r in rows]


def get_proposed_entities(path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM entities WHERE is_proposed=1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC"
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
              AND deleted_at IS NULL
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
            WHERE entity_id = ? AND deleted_at IS NULL {status_filter}
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
            WHERE d.entity_id IN ({ph})
              AND d.deleted_at IS NULL AND e.deleted_at IS NULL {status_filter}
            ORDER BY e.canonical_name COLLATE NOCASE, d.territory COLLATE NOCASE
        """, list(entity_ids)).fetchall()
    return [dict(r) for r in rows]


def get_deal(deal_id: int, path: Path = DB_PATH) -> Optional[dict]:
    with _conn(path) as con:
        row = con.execute(
            "SELECT * FROM deals WHERE id=? AND deleted_at IS NULL", (deal_id,)
        ).fetchone()
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
            WHERE entity_id=? AND status='current' AND deleted_at IS NULL
              AND TRIM(territory) != ''
            GROUP BY LOWER(TRIM(territory))
            HAVING n > 1
        """, (entity_id,)).fetchall()
        return {r["t"] for r in rows}


# ── Soft delete / restore / purge ─────────────────────────────────────────────

def _orphaned_proposed_entities(con, entry_id: int, entry_still_live: bool) -> list[dict]:
    """
    Proposed entities linked to `entry_id` that would have no live source left once
    the entry is gone. Seeded/accepted entities are never touched — they are a
    curated registry, not per-document extractions.

    `entry_still_live` tells us whether the entry itself must be discounted when
    checking for remaining sources (True when previewing an impending delete).
    """
    exclude = "AND en.id != ?" if entry_still_live else ""
    params = [entry_id, entry_id] if entry_still_live else [entry_id]
    rows = con.execute(f"""
        SELECT e.id, e.canonical_name, e.entity_type
        FROM entities e
        JOIN entry_entities ee ON ee.entity_id = e.id
        WHERE ee.entry_id = ?
          AND e.is_proposed = 1
          AND e.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM entry_entities ee2
              JOIN entries en ON en.id = ee2.entry_id
              WHERE ee2.entity_id = e.id AND en.deleted_at IS NULL {exclude}
          )
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_delete_impact(entry_id: int, path: Path = DB_PATH) -> Optional[dict]:
    """
    Preview what a soft delete of `entry_id` would remove from view.

    Returns None if the entry doesn't exist or is already deleted, else:
      {"entry": row, "chunks": int, "deals": int, "entities": [{id, canonical_name, entity_type}]}
    """
    with _conn(path) as con:
        row = con.execute(
            "SELECT * FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchone()
        if not row:
            return None
        n_chunks = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        n_deals = con.execute(
            "SELECT COUNT(*) FROM deals WHERE source_entry_id=? AND deleted_at IS NULL",
            (entry_id,),
        ).fetchone()[0]
        return {
            "entry":    dict(row),
            "chunks":   n_chunks,
            "deals":    n_deals,
            "entities": _orphaned_proposed_entities(con, entry_id, entry_still_live=True),
        }


def soft_delete_entry(entry_id: int, path: Path = DB_PATH) -> dict:
    """
    Mark an entry deleted, cascading to everything extracted from it:
      • deal rows whose source_entry_id is this entry
      • proposed entities left with no other live source

    Nothing is removed from disk or from the tables — `restore_entry()` reverses
    it exactly. Returns {"deals": int, "entities": [names]}; both empty if the
    entry was already deleted or does not exist.
    """
    with _conn(path) as con:
        row = con.execute(
            "SELECT id FROM entries WHERE id=? AND deleted_at IS NULL", (entry_id,)
        ).fetchone()
        if not row:
            return {"deals": 0, "entities": []}

        con.execute(
            "UPDATE entries SET deleted_at=datetime('now'), updated_at=datetime('now')"
            " WHERE id=?",
            (entry_id,),
        )
        cur = con.execute("""
            UPDATE deals
            SET deleted_at=datetime('now'), deleted_with_entry=?, updated_at=datetime('now')
            WHERE source_entry_id=? AND deleted_at IS NULL
        """, (entry_id, entry_id))
        n_deals = cur.rowcount

        # The entry is already flagged above, so remaining-source checks see the
        # post-delete world — no need to discount it again.
        orphans = _orphaned_proposed_entities(con, entry_id, entry_still_live=False)
        for ent in orphans:
            con.execute(
                "UPDATE entities SET deleted_at=datetime('now'), deleted_with_entry=?,"
                " updated_at=datetime('now') WHERE id=?",
                (entry_id, ent["id"]),
            )
        return {"deals": n_deals, "entities": [e["canonical_name"] for e in orphans]}


def _undelete_cascade(con, entry_id: int) -> None:
    """Clear deleted_at on an entry and everything cascaded out with it."""
    con.execute(
        "UPDATE entries SET deleted_at=NULL, updated_at=datetime('now') WHERE id=?",
        (entry_id,),
    )
    con.execute(
        "UPDATE deals SET deleted_at=NULL, deleted_with_entry=NULL,"
        " updated_at=datetime('now') WHERE deleted_with_entry=?",
        (entry_id,),
    )
    con.execute(
        "UPDATE entities SET deleted_at=NULL, deleted_with_entry=NULL,"
        " updated_at=datetime('now') WHERE deleted_with_entry=?",
        (entry_id,),
    )


def restore_entry(entry_id: int, path: Path = DB_PATH) -> bool:
    """Undo a soft delete, bringing back cascaded deals and entities. False if not deleted."""
    with _conn(path) as con:
        row = con.execute(
            "SELECT id FROM entries WHERE id=? AND deleted_at IS NOT NULL", (entry_id,)
        ).fetchone()
        if not row:
            return False
        _undelete_cascade(con, entry_id)
        return True


def get_deleted_entries(path: Path = DB_PATH) -> list[dict]:
    """
    Soft-deleted entries, most recently deleted first, each annotated with
    `days_deleted` and the number of deal/entity rows held with it.
    """
    with _conn(path) as con:
        rows = con.execute("""
            SELECT en.*,
                   CAST(julianday('now') - julianday(en.deleted_at) AS INTEGER) AS days_deleted,
                   (SELECT COUNT(*) FROM deals d
                     WHERE d.deleted_with_entry = en.id AND d.deleted_at IS NOT NULL) AS held_deals,
                   (SELECT COUNT(*) FROM entities e
                     WHERE e.deleted_with_entry = en.id AND e.deleted_at IS NOT NULL) AS held_entities
            FROM entries en
            WHERE en.deleted_at IS NOT NULL
            ORDER BY en.deleted_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def purge_entry(entry_id: int, path: Path = DB_PATH) -> bool:
    """
    Permanently remove a soft-deleted entry: its chunks, FTS index rows, entity
    links, and the deal/entity rows that were cascaded out with it. The source
    file on disk is left untouched. Refuses to purge a live entry.
    """
    with _conn(path) as con:
        row = con.execute(
            "SELECT id FROM entries WHERE id=? AND deleted_at IS NOT NULL", (entry_id,)
        ).fetchone()
        if not row:
            return False

        # FTS index rows (search_idx_map is not FK-linked, so clear it by hand)
        rowids = [
            r[0] for r in con.execute(
                "SELECT rowid FROM search_idx_map WHERE entry_id=?", (entry_id,)
            ).fetchall()
        ]
        if rowids:
            ph = ",".join("?" * len(rowids))
            con.execute(f"DELETE FROM search_idx WHERE rowid IN ({ph})", rowids)
            con.execute(f"DELETE FROM search_idx_map WHERE rowid IN ({ph})", rowids)

        # Deals held with this entry — clear inbound superseded_by refs first so
        # the FK constraint holds, then drop them.
        doomed = [
            r[0] for r in con.execute(
                "SELECT id FROM deals WHERE deleted_with_entry=? AND deleted_at IS NOT NULL",
                (entry_id,),
            ).fetchall()
        ]
        if doomed:
            ph = ",".join("?" * len(doomed))
            con.execute(f"UPDATE deals SET superseded_by=NULL WHERE superseded_by IN ({ph})", doomed)
            con.execute(f"DELETE FROM deals WHERE id IN ({ph})", doomed)
        # Any surviving deal that still cites this entry loses its source pointer.
        con.execute(
            "UPDATE deals SET source_entry_id=NULL, updated_at=datetime('now')"
            " WHERE source_entry_id=?",
            (entry_id,),
        )
        # Entities held with this entry (proposed-only by construction)
        con.execute(
            "DELETE FROM entities WHERE deleted_with_entry=? AND deleted_at IS NOT NULL",
            (entry_id,),
        )

        con.execute("DELETE FROM chunks WHERE entry_id=?", (entry_id,))
        con.execute("DELETE FROM entry_entities WHERE entry_id=?", (entry_id,))
        con.execute(
            "UPDATE entries SET superseded_by=NULL WHERE superseded_by=?", (entry_id,)
        )
        con.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        return True


def get_purgeable_entry_ids(retention_days: int, path: Path = DB_PATH) -> list[int]:
    """IDs of soft-deleted entries whose retention window has elapsed."""
    with _conn(path) as con:
        rows = con.execute("""
            SELECT id FROM entries
            WHERE deleted_at IS NOT NULL
              AND julianday('now') - julianday(deleted_at) >= ?
            ORDER BY deleted_at
        """, (retention_days,)).fetchall()
        return [r[0] for r in rows]


def purge_expired_deleted(retention_days: int, path: Path = DB_PATH) -> int:
    """Permanently purge every soft-deleted entry past the retention window."""
    ids = get_purgeable_entry_ids(retention_days, path)
    return sum(1 for eid in ids if purge_entry(eid, path))

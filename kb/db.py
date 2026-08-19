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

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
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
    validation_warning TEXT      DEFAULT '',
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

CREATE TABLE IF NOT EXISTS deal_entities (
    deal_id   INTEGER NOT NULL REFERENCES deals(id)    ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT    NOT NULL DEFAULT 'market',   -- 'property' | 'market' | 'broadcaster'
    PRIMARY KEY (deal_id, entity_id, role)
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
    flagged_for_review TEXT DEFAULT '',
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
        # Live migration: post-ingest validation flag on entries
        if "validation_warning" not in e_cols:
            con.execute("ALTER TABLE entries ADD COLUMN validation_warning TEXT DEFAULT ''")
        # Live migration: flag column for deals that need manual review (e.g. a
        # value with no currency)
        if "flagged_for_review" not in d_cols:
            con.execute("ALTER TABLE deals ADD COLUMN flagged_for_review TEXT DEFAULT ''")
        # Live migration: backfill deal_entities from deals.entity_id (role='property').
        # Lossless 1:1 copy, safe to re-run every startup — INSERT OR IGNORE is a
        # no-op once a deal's property link already exists. deals.entity_id is
        # intentionally left in place (not dropped) so nothing depends solely on
        # this table being fully populated yet; see backfill_deal_entities.py for
        # the separate (dry-run-first) market/broadcaster backfill.
        con.execute("""
            INSERT OR IGNORE INTO deal_entities (deal_id, entity_id, role)
            SELECT id, entity_id, 'property' FROM deals WHERE entity_id IS NOT NULL
        """)


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


def set_validation_warning(entry_id: int, warning: str, path: Path = DB_PATH) -> None:
    """Set (or clear, with warning='') the post-ingest validation flag on an entry."""
    with _conn(path) as con:
        con.execute("UPDATE entries SET validation_warning=? WHERE id=?", (warning, entry_id))


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

def update_chunk_text(chunk_id: int, text: str, path: Path = DB_PATH) -> None:
    """Overwrite a single chunk's text (manual correction). Caller re-indexes."""
    with _conn(path) as con:
        con.execute("UPDATE chunks SET text=? WHERE id=?", (text, chunk_id))


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
    entity_type: Optional[str] = None,
    path: Path = DB_PATH,
) -> Optional[dict]:
    """
    Return the entity row whose canonical_name or any alias matches `name`
    (case-insensitive). Pass entity_type to restrict the match to one type
    (e.g. 'market') — needed when resolving a deal's territory/broadcaster
    fragment, where an unqualified name could otherwise coincidentally match
    an entity of the wrong kind.
    """
    name_lo = name.strip().lower()
    del_filter = "" if include_deleted else " AND deleted_at IS NULL"
    type_filter = " AND entity_type=?" if entity_type else ""
    type_args = [entity_type] if entity_type else []
    with _conn(path) as con:
        # Exact canonical match
        row = con.execute(
            f"SELECT * FROM entities WHERE LOWER(canonical_name)=?{del_filter}{type_filter}",
            [name_lo] + type_args,
        ).fetchone()
        if row:
            return dict(row)
        # Alias scan (comma-separated aliases column)
        rows = con.execute(
            f"SELECT * FROM entities WHERE 1=1{del_filter}{type_filter}", type_args
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

def _normalize_deal_party(name: str, path: Path = DB_PATH) -> str:
    """
    Resolve a raw broadcaster/territory string to its canonical entity name
    via exact canonical-name-or-alias match (case-insensitive), so "Sky",
    "Sky UK" and "Sky Sports" all collapse to one name instead of creating
    three separate deal rows. Returns the trimmed input unchanged when no
    entity matches — not every broadcaster/territory needs to be seeded.
    """
    name = (name or "").strip()
    if not name:
        return name
    row = find_entity_by_name_or_alias(name, path=path)
    return row["canonical_name"] if row else name


_FULL_YEAR_RE = re.compile(r"(\d{4})")
_SPLIT_YEAR_RE = re.compile(r"(\d{2})\s*/\s*(\d{2})\b")


def _infer_deal_end_year(period_end: str) -> Optional[int]:
    """
    Best-effort year extraction from messy free-text period_end values like
    '2028', '2027/28', '27/28', '30/06/2028'. Returns None when nothing
    extractable — callers must leave status untouched in that case rather
    than guess.
    """
    s = (period_end or "").strip()
    if not s:
        return None
    m = _SPLIT_YEAR_RE.search(s)
    if m:
        yy = max(int(m.group(1)), int(m.group(2)))
        return 2000 + yy
    years = _FULL_YEAR_RE.findall(s)
    if years:
        return max(int(y) for y in years)
    return None


def _infer_deal_status(period_end: str, requested_status: str) -> str:
    """
    A deal whose period has clearly already ended is 'superseded' regardless
    of what was requested — that's a factual/temporal state, not a judgment
    call. Otherwise the requested status (e.g. 'current'/'unverified' from
    extraction confidence) is left as-is. Explicit 'superseded' always wins.
    """
    if requested_status == "superseded":
        return requested_status
    end_year = _infer_deal_end_year(period_end)
    if end_year is not None and end_year < date.today().year:
        return "superseded"
    return requested_status


_DEAL_FILLABLE_FIELDS = [
    "rights_holder", "value", "currency", "value_note", "platform",
    "source_entry_id", "source_note", "flagged_for_review",
]

_MULTI_VALUE_RE = re.compile(r"[;/,]")


def split_multi_value(s: str) -> list[str]:
    """Split a compound territory/broadcaster string ('Turkey, Ukraine',
    'Nine/Stan Sport') into its parts, so each can be resolved and linked to
    its own entity instead of the whole row only ever matching one."""
    return [p.strip() for p in _MULTI_VALUE_RE.split(s or "") if p.strip()]


def resolve_entity_ids(parts: list[str], entity_type: str, path: Path = DB_PATH) -> list[int]:
    """Resolve each part to an entity of the given type; sorted, deduped, unresolved parts skipped."""
    ids = set()
    for part in parts:
        row = find_entity_by_name_or_alias(part, entity_type=entity_type, path=path)
        if row:
            ids.add(row["id"])
    return sorted(ids)


def link_deal_to_entity(deal_id: int, entity_id: int, role: str, path: Path = DB_PATH) -> None:
    with _conn(path) as con:
        con.execute(
            "INSERT OR IGNORE INTO deal_entities (deal_id, entity_id, role) VALUES (?,?,?)",
            (deal_id, entity_id, role),
        )


def _link_deal_to_entity_on_conn(con, deal_id: int, entity_id: int, role: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO deal_entities (deal_id, entity_id, role) VALUES (?,?,?)",
        (deal_id, entity_id, role),
    )


def get_entities_for_deal(deal_id: int, role: Optional[str] = None, path: Path = DB_PATH) -> list[dict]:
    with _conn(path) as con:
        q = """
            SELECT e.*, de.role AS link_role FROM entities e
            JOIN deal_entities de ON de.entity_id = e.id
            WHERE de.deal_id = ? AND e.deleted_at IS NULL
        """
        params: list = [deal_id]
        if role:
            q += " AND de.role = ?"
            params.append(role)
        rows = con.execute(q, params).fetchall()
        return [dict(r) for r in rows]


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
    """
    Insert a deal row, or fill gaps on a matching existing one instead of
    duplicating it. Also links the deal to every entity it touches via
    deal_entities — the property (entity_id, role='property'), and every
    market/broadcaster the territory/broadcaster fields resolve to
    (role='market'/'broadcaster') — so the deal is findable from its
    property's page AND every market/broadcaster page it covers, not just
    entity_id's page. Compound fields ("Turkey, Ukraine", "Nine/Stan Sport")
    are split on ; / , first so a multi-territory row links to every market
    it names (see split_multi_value), rather than the whole row only ever
    matching one.

    Match key: (entity_id, period_start, period_end) narrows candidates, then
    each candidate's linked market/broadcaster entity-id sets are compared
    against this call's resolved sets. When BOTH sides have at least one
    resolved entity, the sets must match exactly. When either side has none
    (nothing resolved — not every territory/broadcaster string is a seeded
    entity, and pre-migration rows may not have deal_entities links yet),
    matching falls back to the plain normalized string comparison this
    function used before deal_entities existed. This fallback matters: an
    empty set is not itself a meaningful match key, and comparing two empty
    sets as "equal" would silently merge two genuinely different unresolved
    territories/broadcasters (e.g. "DACH" and "Nordics", neither seeded) just
    because neither resolved to anything. A residual, accepted risk on the
    other side: a PARTIALLY-resolved compound value (only one of two parts
    matches a seeded entity) still uses set comparison, so a row with one
    resolved id can fail to match a row with two even when they describe the
    same deal — this trades a possible near-duplicate (recoverable via the
    run_deal_dedupe() hygiene pass) for avoiding a false merge, consistent
    with this app's "flag for review" bias over silent guessing elsewhere.

    A value with no currency is never stored as a bare number: it's set to
    null, the original figure is preserved in value_note, and the row is
    flagged for review. Status is inferred from period_end (see
    _infer_deal_status) — a clearly past period is always 'superseded'.
    """
    territory   = _normalize_deal_party(territory, path=path)
    broadcaster = _normalize_deal_party(broadcaster, path=path)
    rights_holder = (rights_holder or "").strip()
    value_note    = (value_note or "").strip()
    platform      = (platform or "").strip()
    source_note   = (source_note or "").strip()
    currency      = (currency or "").strip()
    period_start  = (period_start or "").strip()
    period_end    = (period_end or "").strip()

    flagged = ""
    if value is not None and value != 0 and not currency:
        flagged = (
            f"Value stated without currency ({value}"
            f"{(' ' + value_note) if value_note else ''}) — needs manual currency confirmation."
        )
        value_note = f"[unconfirmed currency: {value}] {value_note}".strip()
        value = None

    status = _infer_deal_status(period_end, status)

    market_ids      = resolve_entity_ids(split_multi_value(territory), "market", path=path)
    broadcaster_ids = resolve_entity_ids(split_multi_value(broadcaster), "broadcaster", path=path)

    def _norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    with _conn(path) as con:
        candidates = con.execute("""
            SELECT * FROM deals
            WHERE entity_id=?
              AND deleted_at IS NULL
              AND TRIM(period_start)=TRIM(?)
              AND TRIM(period_end)=TRIM(?)
        """, (entity_id, period_start, period_end)).fetchall()

        existing = None
        for cand in candidates:
            cand_markets = [r["entity_id"] for r in con.execute(
                "SELECT entity_id FROM deal_entities WHERE deal_id=? AND role='market' ORDER BY entity_id",
                (cand["id"],),
            ).fetchall()]
            cand_broadcasters = [r["entity_id"] for r in con.execute(
                "SELECT entity_id FROM deal_entities WHERE deal_id=? AND role='broadcaster' ORDER BY entity_id",
                (cand["id"],),
            ).fetchall()]

            if market_ids and cand_markets:
                market_match = market_ids == cand_markets
            else:
                market_match = _norm(cand["territory"]) == _norm(territory)

            if broadcaster_ids and cand_broadcasters:
                broadcaster_match = broadcaster_ids == cand_broadcasters
            else:
                broadcaster_match = _norm(cand["broadcaster"]) == _norm(broadcaster)

            if market_match and broadcaster_match:
                existing = cand
                break

        if existing:
            fillable = dict(
                rights_holder=rights_holder, value=value, currency=currency,
                value_note=value_note, platform=platform,
                source_entry_id=source_entry_id, source_note=source_note,
                flagged_for_review=flagged,
            )
            fills = {
                field: new_val
                for field, new_val in fillable.items()
                if existing[field] in (None, "") and new_val not in (None, "")
            }
            if status == "superseded" and existing["status"] != "superseded":
                fills["status"] = status
            if fills:
                con.execute(
                    f"UPDATE deals SET {', '.join(f'{f}=?' for f in fills)}, "
                    f"updated_at=datetime('now') WHERE id=?",
                    list(fills.values()) + [existing["id"]],
                )
            # Always ensure links exist, even on a pure no-op merge — an
            # existing row may predate deal_entities entirely (pre-migration)
            # or may have matched via the string fallback above, in which case
            # this call's freshly-resolved ids still need attaching.
            _link_deal_to_entity_on_conn(con, existing["id"], entity_id, "property")
            for mid in market_ids:
                _link_deal_to_entity_on_conn(con, existing["id"], mid, "market")
            for bid in broadcaster_ids:
                _link_deal_to_entity_on_conn(con, existing["id"], bid, "broadcaster")
            return existing["id"]

        cur = con.execute("""
            INSERT INTO deals
                (entity_id, territory, broadcaster, rights_holder, value, currency,
                 value_note, period_start, period_end, platform,
                 source_entry_id, source_note, status, reliability, flagged_for_review)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            entity_id, territory, broadcaster, rights_holder,
            value, currency, value_note,
            period_start, period_end, platform,
            source_entry_id, source_note, status, reliability, flagged,
        ))
        new_id = cur.lastrowid
        _link_deal_to_entity_on_conn(con, new_id, entity_id, "property")
        for mid in market_ids:
            _link_deal_to_entity_on_conn(con, new_id, mid, "market")
        for bid in broadcaster_ids:
            _link_deal_to_entity_on_conn(con, new_id, bid, "broadcaster")
        return new_id


def flag_deals_missing_currency(dry_run: bool = True, path: Path = DB_PATH) -> list[dict]:
    """
    One-off hygiene pass for legacy rows written before add_deal() enforced
    "currency required whenever a value is present" — e.g. a row storing
    '2050.0m per season' with no currency. Unlike add_deal()'s handling of a
    *new* extraction (where the raw figure can be preserved in value_note),
    an existing row's value is left untouched here — we only mark it
    flagged_for_review, since silently nulling a historical figure would be
    a real data loss for a row we can't re-derive. Returns the rows that are
    (or would be) flagged; dry_run=True makes no writes.
    """
    with _conn(path) as con:
        rows = [dict(r) for r in con.execute("""
            SELECT * FROM deals
            WHERE deleted_at IS NULL AND value IS NOT NULL AND value != 0
              AND TRIM(COALESCE(currency, '')) = '' AND TRIM(COALESCE(flagged_for_review, '')) = ''
        """).fetchall()]
        entity_names = {
            r["id"]: r["canonical_name"]
            for r in con.execute("SELECT id, canonical_name FROM entities").fetchall()
        }

    results = []
    for r in rows:
        reason = f"Value {r['value']} stored with no currency — needs manual currency confirmation."
        results.append({
            "id": r["id"], "entity": entity_names.get(r["entity_id"], f"#{r['entity_id']}"),
            "territory": r.get("territory"), "broadcaster": r.get("broadcaster"),
            "value": r["value"], "value_note": r.get("value_note"), "reason": reason,
        })
        if not dry_run:
            with _conn(path) as con:
                con.execute(
                    "UPDATE deals SET flagged_for_review=?, updated_at=datetime('now') WHERE id=?",
                    (reason, r["id"]),
                )
    return results


def run_deal_dedupe(dry_run: bool = True, path: Path = DB_PATH) -> list[dict]:
    """
    One-off pass over every existing (non-deleted) deal row — for duplicates
    created before add_deal() normalized/deduped on write. Groups on
    (entity_id, normalized territory, normalized broadcaster, period_start,
    period_end) — the plain string-based key add_deal() used before
    deal_entities existed. NOT the same key add_deal() uses today (which
    prefers matching on resolved market/broadcaster entity-id sets — see
    add_deal()'s docstring); this pass is a coarser, standalone hygiene sweep
    over the whole table and hasn't been updated to the entity-set key.
    Within a group the row with the most filled-in fields is kept, missing
    fields on it are backfilled from the others, and the others are
    soft-deleted (deleted_at, recoverable like every other soft delete in
    this app — never a hard delete).

    Returns the merge plan either way: [{"keep": id, "merge": [ids...],
    "entity": canonical_name, "territory":, "broadcaster":, "period": "..",
    "fills": {...}}, ...]. With dry_run=True (the default) nothing is written
    — call again with dry_run=False to actually apply the plan shown.
    """
    with _conn(path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM deals WHERE deleted_at IS NULL"
        ).fetchall()]
        entity_names = {
            r["id"]: r["canonical_name"]
            for r in con.execute("SELECT id, canonical_name FROM entities").fetchall()
        }

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (
            r["entity_id"],
            _normalize_deal_party(r.get("territory") or "", path=path).lower(),
            _normalize_deal_party(r.get("broadcaster") or "", path=path).lower(),
            (r.get("period_start") or "").strip(),
            (r.get("period_end") or "").strip(),
        )
        groups.setdefault(key, []).append(r)

    def _richness(d: dict) -> int:
        return sum(1 for f in _DEAL_FILLABLE_FIELDS if d.get(f) not in (None, ""))

    plans: list[dict] = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=_richness, reverse=True)
        keeper, dupes = group[0], group[1:]

        fills = {}
        for f in _DEAL_FILLABLE_FIELDS:
            if keeper.get(f) in (None, ""):
                for d in dupes:
                    if d.get(f) not in (None, ""):
                        fills[f] = d[f]
                        break

        plans.append({
            "keep": keeper["id"],
            "merge": [d["id"] for d in dupes],
            "entity": entity_names.get(key[0], f"#{key[0]}"),
            "territory": keeper.get("territory"),
            "broadcaster": keeper.get("broadcaster"),
            "period": f"{keeper.get('period_start','')}–{keeper.get('period_end','')}",
            "fills": fills,
        })

        if not dry_run:
            with _conn(path) as con:
                if fills:
                    con.execute(
                        f"UPDATE deals SET {', '.join(f'{f}=?' for f in fills)}, "
                        f"updated_at=datetime('now') WHERE id=?",
                        list(fills.values()) + [keeper["id"]],
                    )
                for d in dupes:
                    con.execute(
                        "UPDATE deals SET deleted_at=datetime('now') WHERE id=?", (d["id"],)
                    )

    return plans


def get_deals_for_entity(
    entity_id: int,
    include_superseded: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    """
    Return deals for an entity, newest first — as its property, or via any
    market/broadcaster it's linked to through deal_entities. Matches on
    entity_id directly too, not just the join table: a deal always has
    entity_id set (it's kept in place during the deal_entities migration),
    but deal_entities may not be populated yet for rows written before this
    linking existed — matching on both means nothing regresses mid-deploy,
    and coverage only grows as deal_entities fills in (see
    backfill_deal_entities.py).
    """
    with _conn(path) as con:
        status_filter = "" if include_superseded else "AND d.status != 'superseded'"
        rows = con.execute(f"""
            SELECT DISTINCT d.* FROM deals d
            WHERE d.deleted_at IS NULL {status_filter}
              AND (
                d.entity_id = ?
                OR d.id IN (SELECT deal_id FROM deal_entities WHERE entity_id = ?)
              )
            ORDER BY d.date_added DESC
        """, (entity_id, entity_id)).fetchall()
        return [dict(r) for r in rows]


def get_deals_for_entities(
    entity_ids: list[int],
    include_superseded: bool = False,
    path: Path = DB_PATH,
) -> list[dict]:
    """
    Return all deals for a set of entities, with entity_name joined in
    (always the deal's PROPERTY name, even when the row matched via a
    market/broadcaster link — see get_deals_for_entity's docstring for why
    both entity_id and deal_entities are checked), sorted by entity then
    territory.
    """
    if not entity_ids:
        return []
    ph = ",".join("?" * len(entity_ids))
    status_filter = "" if include_superseded else "AND d.status != 'superseded'"
    with _conn(path) as con:
        rows = con.execute(f"""
            SELECT DISTINCT d.*, e.canonical_name AS entity_name
            FROM deals d
            JOIN entities e ON d.entity_id = e.id
            WHERE d.deleted_at IS NULL AND e.deleted_at IS NULL {status_filter}
              AND (
                d.entity_id IN ({ph})
                OR d.id IN (SELECT deal_id FROM deal_entities WHERE entity_id IN ({ph}))
              )
            ORDER BY e.canonical_name COLLATE NOCASE, d.territory COLLATE NOCASE
        """, list(entity_ids) + list(entity_ids)).fetchall()
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


def delete_deal(deal_id: int, path: Path = DB_PATH) -> None:
    """Soft-delete a deal row — recoverable at the DB level like every other
    soft delete in this app, though there's no dedicated recycle-bin UI for
    deals specifically (unlike entries)."""
    with _conn(path) as con:
        con.execute(
            "UPDATE deals SET deleted_at=datetime('now') WHERE id=?", (deal_id,)
        )


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

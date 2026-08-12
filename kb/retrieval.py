"""
kb/retrieval.py — Hybrid retrieval and answer generation.

Flow:
  1. FTS5 keyword search → candidate entry IDs (fast, no LLM)
  2. Claude Stage 1    → select from full catalogue, boosted by FTS hits
  3. Load chunks       → build source context with page/slide citations
  4. Claude Stage 2    → generate cited answer (strong Sonnet model)
"""

import logging
import re
from pathlib import Path
from typing import Optional

from config import (
    DB_PATH, STAGE2_MAX_CHUNKS, STAGE2_BROAD_MAX_CHUNKS,
    STAGE2_MULTISOURCE_MAX_CHUNKS, STAGE2_FALLBACK_MAX_CHUNKS, FTS_CANDIDATE_LIMIT,
)
from kb import db
from kb.files import resolve_source_file
from kb.ingest import extract, full_text as _raw_full_text
from kb.llm import select_relevant_entries, generate_answer

log = logging.getLogger("sn1.retrieval")


# ── Broad / exhaustive question detection ────────────────────────────────────

_BROAD_TOKENS = frozenset([
    "all ", " all ", "every ", "each ", "full ", "complete ", "entire ",
    "all of ", "list all", "list every",
    "breakdown", "market-by-market", "territory-by-territory",
    "cycle-by-cycle", "deal-by-deal", "country-by-country",
    "how many ", "which markets", "which territories", "which countries",
    "all deals", "all markets", "all territories", "all countries",
    "full list", "complete list", "full breakdown", "exhaustive",
    "comprehensive", "every deal", "every market",
])


def _is_broad_question(question: str) -> bool:
    """Return True if the question asks for an exhaustive enumeration or complete breakdown."""
    q = question.lower()
    return any(tok in q for tok in _BROAD_TOKENS)


def _citation(row: dict) -> str:
    """Human-readable citation string for an entry."""
    if row.get("entry_type") == "snippet":
        return f"logged note — {row.get('entry_date','?')}, {row.get('source','?')}"
    return row.get("source", "?")


def _entry_date_key(entry: dict) -> str:
    """Sort key: entry_date if present (ISO-ish), else ingestion date, else '0000'."""
    d = (entry.get("entry_date") or "")[:10].strip()
    return d if d else (entry.get("created_at") or "0000")[:10]


# Reliability adjusts the effective year for sorting: confirmed sources are boosted,
# rumoured sources are penalised, so a confirmed 2022 source outranks a rumoured 2024 source.
_RELIABILITY_YEARS = {"confirmed": 2, "reported": 0, "rumoured": -2}


def _combined_sort_key(entry: dict) -> float:
    """
    Combined recency + reliability sort key — higher = more preferred for Stage 1.
    Extracts the 4-digit year from entry_date (falling back to created_at), then
    applies a ±2yr reliability adjustment before ranking.
    """
    raw = (entry.get("entry_date") or entry.get("created_at") or "")[:4].strip()
    try:
        year = float(raw) if len(raw) == 4 and raw.isdigit() else 2020.0
    except ValueError:
        year = 2020.0
    return year + _RELIABILITY_YEARS.get(entry.get("reliability", "reported"), 0)


def _chunk_label(chunk: dict) -> str:
    """'p.4', 'slide 3', 'sheet 2', etc."""
    ctype = chunk.get("chunk_type", "page")
    num   = chunk.get("chunk_num", "?")
    labels = {"page": f"p.{num}", "slide": f"slide {num}", "sheet": f"sheet {num}", "body": ""}
    return labels.get(ctype, f"{ctype} {num}")


def _source_meta_line(row: dict) -> str:
    """One-line metadata: date + coverage period + reliability + status badge for the answer model."""
    parts = []
    date = (row.get("entry_date") or "").strip()
    if date:
        parts.append(f"Date: {date}")
    cov = (row.get("coverage_period") or "").strip()
    if cov and cov != date:
        parts.append(f"Covers: {cov}")
    reliability = row.get("reliability", "reported")
    parts.append(f"Reliability: {reliability}")
    status = row.get("status", "current")
    if status != "current":
        parts.append(f"[{status.upper()}]")
    return " · ".join(parts) if parts else ""


def _format_deals_section(deals: list[dict]) -> str:
    """
    Format structured deal rows (from the deals table) as a concise context preamble.
    One line per broadcaster–territory row. Grouped by entity.
    """
    if not deals:
        return ""
    lines = [
        "=== STRUCTURED DEALS DATABASE ===",
        "Rows extracted from source documents at ingest. Each row = one broadcaster–territory pair.",
        "CITE AS: [Deals database — <entity_name>, <territory>]",
        "",
    ]
    current_entity: str | None = None
    for d in deals:
        ename = d.get("entity_name", "?")
        if ename != current_entity:
            lines.append(f"── {ename} ──")
            current_entity = ename
        parts: list[str] = []
        if d.get("territory"):    parts.append(f"Territory: {d['territory']}")
        if d.get("broadcaster"):  parts.append(f"Broadcaster: {d['broadcaster']}")
        period = "–".join(filter(None, [d.get("period_start",""), d.get("period_end","")]))
        if period: parts.append(f"Period: {period}")
        if d.get("value") is not None:
            val_str = f"{d.get('currency','')} {d['value']:g}M".strip()
            if d.get("value_note"): val_str += f" ({d['value_note']})"
            parts.append(f"Value: {val_str}")
        if d.get("platform"):      parts.append(f"Platform: {d['platform']}")
        if d.get("rights_holder"): parts.append(f"Rights holder: {d['rights_holder']}")
        rel = d.get("reliability", "reported")
        src = d.get("source_note", "")
        meta = f"[{rel}" + (f", {src[:50]}" if src else "") + "]"
        parts.append(meta)
        lines.append("• " + "  |  ".join(parts))
    return "\n".join(lines)


_RETRIEVAL_STOP_WORDS = frozenset([
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


# "2025/26" in a question should also match "2025-26", "2025–26" or "2025/2026"
# in a deck — season formatting is never consistent across sources.
_SEASON_RE = re.compile(r"^(\d{4})[/\-–](\d{2}|\d{4})$")


def _season_variants(term: str) -> set[str]:
    m = _SEASON_RE.match(term)
    if not m:
        return set()
    start, end = m.group(1), m.group(2)
    end2, out = end[-2:], {start}
    for sep in ("/", "-", "–"):
        out.add(f"{start}{sep}{end2}")
        out.add(f"{start}{sep}{start[:2]}{end2}")
    return out


def _question_terms(question: str) -> set[str]:
    """Content tokens from the question, lowercased, stop-words removed, plus
    season-format variants so '2025/26' also matches '2025-26' and '2025/2026'."""
    terms: set[str] = set()
    for tok in question.split():
        t = tok.strip(".,!?;:'\"()[]").lower()
        if len(t) > 2 and t not in _RETRIEVAL_STOP_WORDS:
            terms.add(t)
            terms |= _season_variants(t)
    return terms


def _chunk_score(chunk: dict, terms: set[str]) -> float:
    """
    Relevance of one page/slide to the question: how many distinct question terms
    it contains, with a small density bonus so a page that repeats a term outranks
    one that mentions it once. 0.0 means no term appears at all.
    """
    if not terms:
        return 0.0
    text = (chunk.get("text") or "").lower()
    matched = occurrences = 0
    for t in terms:
        c = text.count(t)
        if c:
            matched += 1
            occurrences += min(c, 3)
    return matched + 0.1 * occurrences if matched else 0.0


def _allocate_chunks(
    selected_rows: list[dict],
    chunks_by_entry: dict[int, list[dict]],
    max_chunks: int,
    terms: set[str],
) -> dict[int, list[dict]]:
    """
    Decide which pages to load, across all sources at once.

    Relevance-first, not document-order: after each source keeps a single anchor
    page (page 1, so every cited source has identifying context), the whole
    remaining budget is contested globally by keyword score. A slide 45 that
    matches the question therefore beats slide 2 of a source that doesn't —
    which is what makes the wide "catalogue sweep" fallback usable: 20 sources
    no longer each burn their share on their own front matter.

    Any budget left after the scoring pass is filled round-robin in document
    order, so no single long deck consumes the remainder.

    Returns {entry_id: [chunk, ...]} with chunks in natural document order.
    """
    picked: dict[int, set[int]] = {row["id"]: set() for row in selected_rows}
    budget = max_chunks

    # 1. Anchor page per source (in FTS-priority order, so if the budget is smaller
    #    than the number of sources the most promising ones are still represented).
    for row in selected_rows:
        if budget <= 0:
            break
        if chunks_by_entry.get(row["id"]):
            picked[row["id"]].add(0)
            budget -= 1

    # 2. Global relevance contest for everything that's left.
    if budget > 0 and terms:
        scored: list[tuple[float, int, int, int]] = []
        for order, row in enumerate(selected_rows):
            eid = row["id"]
            for idx, chunk in enumerate(chunks_by_entry.get(eid, [])):
                if idx in picked[eid]:
                    continue
                score = _chunk_score(chunk, terms)
                if score > 0:
                    scored.append((score, order, idx, eid))
        # Highest score first; ties by source priority then page order.
        scored.sort(key=lambda s: (-s[0], s[1], s[2]))
        for _score, _order, idx, eid in scored[:budget]:
            picked[eid].add(idx)
            budget -= 1

    # 3. Round-robin fill with whatever remains, in document order.
    if budget > 0:
        cursors = {row["id"]: 0 for row in selected_rows}
        progressed = True
        while budget > 0 and progressed:
            progressed = False
            for row in selected_rows:
                if budget <= 0:
                    break
                eid = row["id"]
                chunks = chunks_by_entry.get(eid, [])
                i = cursors[eid]
                while i < len(chunks) and i in picked[eid]:
                    i += 1
                if i < len(chunks):
                    picked[eid].add(i)
                    cursors[eid] = i + 1
                    budget -= 1
                    progressed = True

    return {
        eid: [chunks_by_entry.get(eid, [])[i] for i in sorted(idxs)]
        for eid, idxs in picked.items()
    }


def build_source_context(
    selected_rows: list[dict],
    chunks_by_entry: dict[int, list[dict]],
    max_chunks: int = STAGE2_MAX_CHUNKS,
    question: str = "",
) -> tuple[str, list[dict]]:
    """
    Build the source context block for Stage 2.
    Returns (context_str, truncation_events).

    truncation_events: [{"cite": str, "entry_id": int, "loaded": int, "total": int}, ...]
    — one entry per source that was partially or entirely omitted.

    Pages are chosen by relevance to the question across all sources at once (see
    `_allocate_chunks`), not by document order within a per-source quota, so the one
    slide that answers the question is loaded even when 20 sources are in play.
    """
    if not selected_rows:
        return "", []

    terms = _question_terms(question) if question else set()
    allocated = _allocate_chunks(selected_rows, chunks_by_entry, max_chunks, terms)

    # ── Build context ──────────────────────────────────────────────────────────
    parts: list[str] = []
    truncations: list[dict] = []

    for row in selected_rows:
        eid = row["id"]
        cite = _citation(row)
        entry_chunks = chunks_by_entry.get(eid, [])
        n_total = len(entry_chunks)
        selected_chunks = allocated.get(eid, [])

        if not selected_chunks and n_total > 0:
            parts.append(
                f"=== SOURCE NOT LOADED: {cite} ===\n"
                f"[⚠ Context budget reached — all {n_total} page{'s' if n_total!=1 else ''} "
                f"of this source were not retrieved. It may contain additional relevant data.]"
            )
            truncations.append({"cite": cite, "entry_id": eid, "loaded": 0, "total": n_total})
            continue

        meta_line = _source_meta_line(row)
        header = f"=== SOURCE: {cite} ===" + (f"\n{meta_line}" if meta_line else "")
        cite_as = f"CITE AS: [{cite}, <location>]  (replace <location> with e.g. p.4 or slide 3)"

        chunk_parts: list[str] = []
        for chunk in selected_chunks:
            label = _chunk_label(chunk)
            chunk_parts.append(f"[{label}]\n{chunk['text']}" if label else chunk["text"])

        n_loaded = len(chunk_parts)
        truncation_note = ""
        if n_loaded < n_total:
            omitted = n_total - n_loaded
            # Note whether any pages were omitted due to relevance filtering vs. budget
            truncation_note = (
                f"\n\n[⚠ TRUNCATED: {omitted} of {n_total} page{'s' if n_total!=1 else ''} not loaded "
                f"({n_loaded} most relevant pages shown within context budget). "
                f"Additional content may exist in this source.]"
            )
            truncations.append({"cite": cite, "entry_id": eid, "loaded": n_loaded, "total": n_total})

        if chunk_parts:
            parts.append(f"{header}\n{cite_as}\n\n" + "\n\n".join(chunk_parts) + truncation_note)
        else:
            parts.append(f"{header}\n{cite_as}\n\n[No content available]")

    context = "\n\n" + ("\n\n" + "─" * 60 + "\n\n").join(parts)
    return context, truncations


def _load_doc_chunks_from_file(row: dict) -> list[db.Chunk]:
    """
    For a document entry that has no chunks in the DB, fall back to reading
    the source file and extracting on-the-fly.
    """
    path = resolve_source_file(row)
    if path is None:
        return []
    result = extract(path)
    if result.error:
        return []
    return [db.Chunk(c.chunk_num, c.chunk_type, c.text) for c in result.chunks]


def build_deals_preamble(entity_ids: list[int], db_path: Path = DB_PATH) -> str:
    """
    Public helper: return a formatted deals-database section for the given entity IDs.
    Used by the entity hub scoped Ask to prepend deal rows to its context.
    """
    if not entity_ids:
        return ""
    deals = db.get_deals_for_entities(entity_ids, path=db_path)
    return _format_deals_section(deals)


def retrieve_and_answer(
    question: str,
    db_path: Path = DB_PATH,
    conversation_history: Optional[list[dict]] = None,
    include_superseded: bool = False,
) -> dict:
    """
    Full retrieval + answer pipeline.

    Returns:
        {
          "answer": str,
          "selected": [{"id", "source", "entry_type", "rationale"}, ...],
          "rationale": str,
          "fts_hit_ids": [int, ...],
          "is_fallback": bool,
        }

    By default, superseded entries are excluded from retrieval. Pass
    include_superseded=True to include them (e.g. for historical queries).
    """
    all_entries = db.get_all_entries(db_path)
    if not all_entries:
        return {
            "answer": "The knowledge base is empty. Please add documents first.",
            "selected": [], "rationale": "", "fts_hit_ids": [], "is_fallback": False,
        }

    # Filter superseded unless the caller explicitly wants history
    if not include_superseded:
        all_entries = [e for e in all_entries if e.get("status", "current") != "superseded"]

    # Sort by combined recency+reliability so Stage 1 Claude sees the most credible
    # and recent entries first (confirmed sources get a +2yr bonus over rumoured).
    all_entries.sort(key=_combined_sort_key, reverse=True)

    # ── Stage 1a: FTS keyword search ─────────────────────────────────────────
    fts_ids = db.fts_search(question, limit=FTS_CANDIDATE_LIMIT, path=db_path)

    # ── Stage 1b: Claude selects from full catalogue ─────────────────────────
    stage1 = select_relevant_entries(all_entries, question, fts_boost_ids=fts_ids)
    selected_ids = stage1.ids
    rationale    = stage1.rationale
    is_fallback  = stage1.is_fallback
    if stage1.error:
        log.warning("Stage 1 problem (mode=%s): %s", stage1.mode, stage1.error)

    if not selected_ids:
        return {
            "answer": "The knowledge base does not contain sufficient information to answer this question.",
            "selected": [], "rationale": rationale,
            "fts_hit_ids": fts_ids, "is_fallback": is_fallback,
            "stage1_mode": stage1.mode, "stage1_error": stage1.error,
            "stage1_raw": stage1.raw, "stage1_attempts": stage1.attempts,
        }

    selected_rows = db.get_entries_by_ids(selected_ids, path=db_path)

    # Sort selected rows: FTS-hit entries first (they're the primary sources for this query),
    # then by recency+reliability within each group — ensures primary sources load their pages
    # before the chunk budget is exhausted by less-directly-relevant entries.
    fts_hit_set = set(fts_ids)
    selected_rows.sort(
        key=lambda r: (0 if r["id"] in fts_hit_set else 1, -_combined_sort_key(r))
    )

    # ── Adaptive chunk budget ────────────────────────────────────────────────
    # Raise the budget for exhaustive questions ("all markets", "every deal"), for
    # multi-source questions (3+ sources), and most of all when Stage 1 failed and
    # we're sweeping the whole catalogue — that sweep only works if there is room
    # for the relevance ranking to pull in the pages that actually match.
    is_broad = _is_broad_question(question)
    chunk_budget = STAGE2_MAX_CHUNKS
    if is_broad:
        chunk_budget = max(chunk_budget, STAGE2_BROAD_MAX_CHUNKS)
    if len(selected_rows) >= 3:
        chunk_budget = max(chunk_budget, STAGE2_MULTISOURCE_MAX_CHUNKS)
    if is_fallback:
        chunk_budget = max(chunk_budget, STAGE2_FALLBACK_MAX_CHUNKS)

    # ── Load chunks ──────────────────────────────────────────────────────────
    chunks_by_entry = db.get_chunks_for_entries(selected_ids, db_path)

    # Fill in from file for docs with no stored chunks
    for row in selected_rows:
        eid = row["id"]
        if not chunks_by_entry.get(eid):
            live_chunks = _load_doc_chunks_from_file(row)
            if live_chunks:
                db.store_chunks(eid, live_chunks)
                db.index_entry(eid)
                chunks_by_entry[eid] = [
                    {"chunk_num": c.chunk_num, "chunk_type": c.chunk_type, "text": c.text}
                    for c in live_chunks
                ]

    # ── Deals database preamble ──────────────────────────────────────────────
    # Query the structured deals table for all entities linked to the selected entries.
    # Deals are extracted at ingest time, so this returns complete market-by-market rows
    # regardless of page truncation — directly answers deal/broadcaster/market questions.
    entity_ids: set[int] = set()
    for row in selected_rows:
        for en in db.get_entities_for_entry(row["id"], db_path):
            entity_ids.add(en["id"])
    deals = db.get_deals_for_entities(list(entity_ids), path=db_path) if entity_ids else []
    deals_prefix = _format_deals_section(deals)

    # ── Stage 2: generate cited answer ──────────────────────────────────────
    context, truncations = build_source_context(selected_rows, chunks_by_entry, max_chunks=chunk_budget, question=question)
    if deals_prefix:
        context = deals_prefix + "\n\n" + "─" * 60 + "\n\n" + context

    answer_max_tokens = 2500 if (is_broad or len(selected_rows) >= 3) else 2000
    answer = generate_answer(context, question, conversation_history, max_tokens=answer_max_tokens)

    selected_summary = [
        {
            "id":         row["id"],
            "source":     row["source"],
            "entry_type": row.get("entry_type", "document"),
            "cite":       _citation(row),
        }
        for row in selected_rows
    ]

    return {
        "answer":            answer,
        "selected":          selected_summary,
        "rationale":         rationale,
        "fts_hit_ids":       fts_ids,
        "is_fallback":       is_fallback,
        "truncated_sources": truncations,
        "stage1_mode":       stage1.mode,
        "stage1_error":      stage1.error,
        "stage1_raw":        stage1.raw,
        "stage1_attempts":   stage1.attempts,
    }

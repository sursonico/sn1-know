"""
kb/retrieval.py — Hybrid retrieval and answer generation.

Flow:
  1. FTS5 keyword search → candidate entry IDs (fast, no LLM)
  2. Claude Stage 1    → select from full catalogue, boosted by FTS hits
  3. Load chunks       → build source context with page/slide citations
  4. Claude Stage 2    → generate cited answer (strong Sonnet model)
"""

from pathlib import Path
from typing import Optional

from config import DB_PATH, STAGE2_MAX_CHUNKS, STAGE2_BROAD_MAX_CHUNKS, STAGE2_MULTISOURCE_MAX_CHUNKS, FTS_CANDIDATE_LIMIT
from kb import db
from kb.ingest import extract, full_text as _raw_full_text
from kb.llm import select_relevant_entries, generate_answer


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


def _question_terms(question: str) -> set[str]:
    """Return content tokens from the question, lowercased, stop-words removed."""
    return {
        t.strip(".,!?;:'\"()[]").lower()
        for t in question.split()
        if len(t) > 2 and t.lower() not in _RETRIEVAL_STOP_WORDS
    }


def _select_chunks_for_question(
    chunks: list[dict],
    limit: int,
    terms: set[str],
) -> list[dict]:
    """
    Choose up to `limit` chunks from the list, preferring those most relevant to
    the question. Strategy:
      - Always include chunks 0 and 1 (intro/title context), up to the limit.
      - Fill remaining budget with chunks ranked by keyword-overlap count descending.
      - Ties broken by original position (earlier first).
    Returns the selected chunks in their natural order (by chunk_num) so the
    reader/model sees them in document order, not relevance order.
    """
    if limit >= len(chunks) or not terms:
        return chunks[:limit]

    n_head = min(2, limit)  # always-include head
    head_set = set(range(n_head))

    # Score non-head chunks
    scored = []
    for i, chunk in enumerate(chunks):
        if i in head_set:
            continue
        text = (chunk.get("text") or "").lower()
        score = sum(1 for t in terms if t in text)
        scored.append((score, i))

    scored.sort(key=lambda x: (-x[0], x[1]))  # high score first, stable by position
    picked = head_set | {i for _, i in scored[: limit - n_head]}

    # Return in natural document order
    return [chunks[i] for i in sorted(picked)]


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

    Fair-share allocation: each source receives floor(max_chunks / n_sources) chunks in
    round 1; remaining budget flows to sources with more content (FTS-priority order).
    Prevents early entries from exhausting the budget and leaving later sources blank.

    Within each source's budget, chunks are selected by keyword relevance to the
    question (not just pages 1-N), so a relevant slide 45 isn't dropped while
    slide 1 of another deck is loaded twice.
    """
    if not selected_rows:
        return "", []

    n = len(selected_rows)
    terms = _question_terms(question) if question else set()

    # ── Fair-share pre-allocation ─────────────────────────────────────────────
    # Round 1: equal base share (floor division guarantees n*base <= max_chunks).
    base = max_chunks // n
    allocs: dict[int, int] = {}
    used_r1 = 0
    for row in selected_rows:
        eid = row["id"]
        avail = len(chunks_by_entry.get(eid, []))
        allocs[eid] = min(avail, base)
        used_r1 += allocs[eid]

    # Round 2: leftover budget flows to sources with more content (list order = FTS-priority).
    leftover = max_chunks - used_r1
    if leftover > 0:
        for row in selected_rows:
            eid = row["id"]
            avail = len(chunks_by_entry.get(eid, []))
            extra = min(avail - allocs[eid], leftover)
            if extra > 0:
                allocs[eid] += extra
                leftover -= extra
            if leftover <= 0:
                break

    # ── Build context ──────────────────────────────────────────────────────────
    parts: list[str] = []
    truncations: list[dict] = []

    for row in selected_rows:
        eid = row["id"]
        cite = _citation(row)
        entry_chunks = chunks_by_entry.get(eid, [])
        n_total = len(entry_chunks)
        limit = allocs.get(eid, 0)

        if limit == 0 and n_total > 0:
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

        # Select chunks by relevance within the allocated budget
        selected_chunks = _select_chunks_for_question(entry_chunks, limit, terms)

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
    file_path = row.get("file_path", "")
    if not file_path:
        return []
    path = Path(file_path)
    if not path.exists():
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
    selected_ids, rationale = select_relevant_entries(
        all_entries, question, fts_boost_ids=fts_ids
    )
    is_fallback = "Falling back" in rationale or "fall back" in rationale.lower()

    if not selected_ids:
        return {
            "answer": "The knowledge base does not contain sufficient information to answer this question.",
            "selected": [], "rationale": rationale,
            "fts_hit_ids": fts_ids, "is_fallback": is_fallback,
        }

    selected_rows = db.get_entries_by_ids(selected_ids, db_path)

    # Sort selected rows: FTS-hit entries first (they're the primary sources for this query),
    # then by recency+reliability within each group — ensures primary sources load their pages
    # before the chunk budget is exhausted by less-directly-relevant entries.
    fts_hit_set = set(fts_ids)
    selected_rows.sort(
        key=lambda r: (0 if r["id"] in fts_hit_set else 1, -_combined_sort_key(r))
    )

    # ── Adaptive chunk budget ────────────────────────────────────────────────
    # Raise budget for exhaustive questions ("all markets", "every deal") and for
    # multi-source questions (3+ sources selected) so fair-share allocation has
    # enough room to give each source adequate coverage.
    is_broad = _is_broad_question(question)
    chunk_budget = STAGE2_MAX_CHUNKS
    if is_broad:
        chunk_budget = max(chunk_budget, STAGE2_BROAD_MAX_CHUNKS)
    if len(selected_rows) >= 3:
        chunk_budget = max(chunk_budget, STAGE2_MULTISOURCE_MAX_CHUNKS)

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
    }

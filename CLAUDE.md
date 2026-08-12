# SN1 Knowledge Base v2 — CLAUDE.md

## Purpose
A private sports-media-rights intelligence platform for SN1 Consulting.
Ingests research documents (PDF, PPTX, XLSX) and analyst notes ("snippets"),
stores them in a searchable SQLite knowledge base, and lets users explore a
structured entity registry and ask plain-English questions answered with
page-level citations.

## Repository layout
```
config.py           — all settings (models, paths, batch size)
app.py              — Home page (entity cards, search bar, stats)
migrate.py          — one-shot import from v1 catalogue.db
migrate_entities.py — link existing entries to entities
seed_entities.py    — canonical entity seed list
kb/
  db.py             — database layer: schema, CRUD, FTS5, entity CRUD
  files.py          — source-file resolution: SN1_DOCS_DIR + stored relative path
  ingest.py         — ingestion pipeline: extract → hash → classify → store → resolve entities
  llm.py            — LLM client: classify, enrich, entity resolution, answer, overview
  retrieval.py      — hybrid retrieval: FTS5 + Claude Stage 1 → Stage 2 cited answer
  ui.py             — shared Streamlit utilities: styles, nav, components
pages/
  entity.py         — entity hub (overview, deals, broadcaster coverage, sources)
  browse.py         — browse & filter table + per-row open file / soft delete
  ask.py            — global Ask (FTS + two-stage retrieval)
  add_log.py        — Add Documents + Log Snippet
  admin.py          — entity rename, merge, alias editing, recycle bin
sample_docs/        — source files (gitignored)
```

## Database schema (knowledge_base.db)
```
entries        id · entry_type · source · entry_date · file_type · doc_type
               org_tags · market_tags · sport_tags · topic_tags
               summary · notes · file_path · content_hash
               is_duplicate · ocr_used · ingest_error · deleted_at
               created_at · updated_at

chunks         id · entry_id (→entries) · chunk_num · chunk_type · text
               (one row per PDF page / PPTX slide / XLSX sheet / snippet body)

search_idx     FTS5 virtual table — porter tokenizer
search_idx_map rowid → entry_id + optional chunk_id

entities       id · canonical_name · entity_type · aliases · is_proposed
               overview · overview_at · deleted_at · deleted_with_entry
               created_at · updated_at

entry_entities entry_id (→entries) · entity_id (→entities)  [many-to-many]

deals          id · entity_id (→entities) · territory · broadcaster · rights_holder
               value · currency · period_start · period_end · platform
               source_entry_id (→entries) · status · reliability
               deleted_at · deleted_with_entry
```

## Soft deletion
Deleting from Browse never removes a row or the source file — it stamps
`deleted_at` and cascades to everything extracted from that entry:

- **deals** with `source_entry_id = entry` → `deleted_at` + `deleted_with_entry`
- **proposed entities** left with no other live source → same two columns
  (seeded/accepted entities are a curated registry and are never touched)

Every read in `kb/db.py` filters `deleted_at IS NULL`, including `fts_search()`
(which joins `entries`), so deleted content disappears from Browse, Ask/retrieval,
entity hubs and stats. FTS index rows are deliberately left in place so a restore
needs no re-indexing.

`restore_entry()` reverses a delete exactly, using `deleted_with_entry` to know
what to bring back. Re-ingesting a deleted file revives it the same way.
Admin → Recently Deleted lists the bin with restore + permanent purge;
`purge_entry()` is the only destructive path and still leaves the file on disk.
`DELETED_RETENTION_DAYS` (config, default 30) is the recovery window — nothing is
purged automatically.

## Entity model
**Entity types:** competition | federation | broadcaster | market | rights_holder | club | other

**Alias resolution:** `find_entity_by_name_or_alias()` checks canonical name then
comma-separated aliases (case-insensitive). Use this for matching during ingestion.

**`is_proposed=1`** means Claude spotted a new entity during ingestion that isn't in
the seed list. The Admin page shows a review queue for these.

**Ingestion flow** (new files):
1. `extract_file_text()` → per-page/slide chunks
2. `classify_document_async()` → full metadata (Haiku, parallel batches of 6)
3. `resolve_entities_async()` → list of {canonical, type, is_new}
4. For each resolved entity: `find_or_create_entity()` → `link_entry_to_entity()`
5. `store_chunks()` + `index_entry()` (FTS5)

## Retrieval pipeline
1. **FTS5 keyword pass** — `fts_search(question)` → candidate entry IDs
2. **Claude Stage 1** — catalogue context with FTS hits boosted → `select_relevant_entries()`
3. **Stage 2** — load chunks for selected entries → `generate_answer()` (Sonnet)
4. Citations: `[filename.pdf, p.4]` or `[logged note — 2026-06-10, Source]`

### Stage 1 robustness
`select_relevant_entries()` returns a `Stage1Result` with a `mode`:
`selected` · `salvaged` (JSON truncated/malformed but IDs recovered) · `empty`
(model validly found nothing) · `fallback_all` (both attempts failed → sweep the
catalogue, FTS hits first). A malformed reply is **retried once** with a stricter
instruction before falling back, `max_tokens` is high enough that the JSON isn't
cut off mid-array, and every failure is logged *and* returned on `.error`/`.raw`
so Ask can show what actually came back instead of a silent "parse failed".

### Page selection (Stage 2)
`_allocate_chunks()` picks pages **globally by relevance**, not per-source in
document order: each source keeps one anchor page, then the whole remaining
budget is contested by keyword score across every source, and anything left is
filled round-robin. This is what makes the `fallback_all` sweep usable — 20
sources no longer each spend their quota on their own front matter, so a matching
slide 31 beats slide 2 of an unrelated deck. Question terms include season
variants (`2025/26` also matches `2025-26`, `2025/2026`). Budgets live in
config: `STAGE2_MAX_CHUNKS` → broad → multisource → `STAGE2_FALLBACK_MAX_CHUNKS`.

## Conventions
- All DB access via `kb/db.py`. No raw sqlite3 elsewhere.
- All LLM calls via `kb/llm.py` (SDK if `ANTHROPIC_API_KEY`, else `claude` CLI).
- `config.py` owns all constants — import with `from config import X`.
- Every page starts with `from kb.ui import page_setup; page_setup("nav_key")`.
- `file_path` stores the path **relative to `DOCS_DIR`** (absolute only for files
  outside it). Never `Path(entry["file_path"])` directly — call
  `kb.files.resolve_source_file(entry)`, which handles relative paths, legacy
  absolute paths from another machine, and filename-only lookup, returning `None`
  when the file genuinely isn't there.
- Content hash (SHA-256) prevents re-processing unchanged files.

## SN1 brand
| Token    | Value     | Usage                                              |
|----------|-----------|----------------------------------------------------|
| Navy     | `#2B383E` | Header background, headings, dark text             |
| Gold     | `#AA925C` | Accent: active nav, primary buttons, rule, focus   |
| Cream    | `#F3ECE0` | Panel backgrounds, overview boxes, table headers   |
| White    | `#FFFFFF` | Card backgrounds, input backgrounds                |
| Open Sans| Headings  | Google Fonts — weights 300/400/600/700             |
| Lato     | Body text | Google Fonts — weights 300/400/700 + italic        |

**Type scale:** headings `h1=2rem h2=1.5rem h3=1.1rem`, body `0.93rem`, labels `0.7rem`
**Spacing:** base unit `0.25rem`; cards `1.25rem` padding; grid `1rem` gap
**Cards:** `border-radius:10px`, `border:1px solid #E8E1D6`, hover lift `translateY(-3px)` + gold border
**Entity type colors:** competition=`#AA925C`, federation=`#2B383E`, broadcaster=`#2A7F7F`,
market=`#5B7B8A`, rights_holder=`#7B5B2A`, club=`#4A7B4A`, other=`#8A9598`

### CSS gotcha (from v1)
**NEVER** include `[class*="st-"]` in a global `font-family` rule.
Streamlit's emotion-cache classes are used for Material Symbols icons; overriding them
breaks the CSS ligature rendering and makes chevrons appear as literal text like
"keyboard_arrow_right". Scope brand fonts to `html, body, .stApp` only.

## Running locally
```bash
pip install -r requirements.txt
python seed_entities.py            # seed canonical entity list
python migrate.py                  # import from v1 (run once)
python migrate_entities.py         # link entries → entities (run once)
python -m kb.ingest                # ingest/re-ingest sample_docs/
streamlit run app.py
```
Set `ANTHROPIC_API_KEY` for SDK access + parallel async ingestion.
Without it the app uses the `claude` CLI (Claude Code OAuth).

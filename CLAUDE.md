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
  ingest.py         — ingestion pipeline: extract → hash → classify → store → resolve entities
  llm.py            — LLM client: classify, enrich, entity resolution, answer, overview
  retrieval.py      — hybrid retrieval: FTS5 + Claude Stage 1 → Stage 2 cited answer
  ui.py             — shared Streamlit utilities: styles, nav, components
pages/
  entity.py         — entity hub (overview, linked entries, scoped Ask)
  browse.py         — browse & filter table
  ask.py            — global Ask (FTS + two-stage retrieval)
  add_log.py        — Add Documents + Log Snippet
  admin.py          — entity rename, merge, alias editing
sample_docs/        — source files (gitignored)
```

## Database schema (knowledge_base.db)
```
entries        id · entry_type · source · entry_date · file_type · doc_type
               org_tags · market_tags · sport_tags · topic_tags
               summary · notes · file_path · content_hash
               is_duplicate · ocr_used · ingest_error · created_at · updated_at

chunks         id · entry_id (→entries) · chunk_num · chunk_type · text
               (one row per PDF page / PPTX slide / XLSX sheet / snippet body)

search_idx     FTS5 virtual table — porter tokenizer
search_idx_map rowid → entry_id + optional chunk_id

entities       id · canonical_name · entity_type · aliases · is_proposed
               overview · overview_at · created_at · updated_at

entry_entities entry_id (→entries) · entity_id (→entities)  [many-to-many]
```

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

## Conventions
- All DB access via `kb/db.py`. No raw sqlite3 elsewhere.
- All LLM calls via `kb/llm.py` (SDK if `ANTHROPIC_API_KEY`, else `claude` CLI).
- `config.py` owns all constants — import with `from config import X`.
- Every page starts with `from kb.ui import page_setup; page_setup("nav_key")`.
- `file_path` always stores the **absolute** path.
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

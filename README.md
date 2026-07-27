# SN1 Knowledge Base v2

Private sports-media-rights intelligence platform — search, retrieve, and ask questions
across your document library and analyst notes.

## Quick start

```bash
pip install -r requirements.txt

# One-time: import from v1
python migrate.py --v1-db ~/sn1-data-catalogue/knowledge_base.db \
                  --v1-docs ~/sn1-data-catalogue/sample_docs

# Ingest any new documents in sample_docs/
python -m kb.ingest

# Launch the app
streamlit run app.py
```

Set `ANTHROPIC_API_KEY` in your environment for SDK access (faster, parallel ingestion).
Without it the app falls back to the `claude` CLI.
If you deploy on Render without shell access, the app seeds the canonical entity list automatically on the first launch.

## Deploy on Render

Render is the simplest option here because it supports a mounted disk, which the app needs for the SQLite database and uploaded documents.

1. Create a new Render Web Service from this repo.
1. Use the blueprint in [render.yaml](render.yaml) or set the start command to `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`.
1. If you created a plain Python service, Render should also pick up the [Procfile](Procfile) and start Streamlit from there.
1. Set `ANTHROPIC_API_KEY` as a secret environment variable.
1. Add `SN1_SHARE_PASSWORD` if you want the built-in password gate.
1. Keep the disk mounted at `/var/data`; the app writes the DB to `/var/data/knowledge_base.db` and uploaded docs to `/var/data/sample_docs`.

If you want Railway instead, the app code will work there too, but the persistent disk setup is a little less direct than Render for this SQLite-backed app.

## Features

| Feature | Detail |
|---|---|
| **Hybrid search** | FTS5 keyword + Claude Stage-1 catalogue scan |
| **Page citations** | Answers cite `[file.pdf, p.4]` or `[deck.pptx, slide 7]` |
| **Follow-up Q&A** | Conversation context kept in session |
| **Snippet logging** | Log a note; Claude extracts entities and topics automatically |
| **Incremental ingestion** | SHA-256 content hash; unchanged files are skipped |
| **Parallel classification** | Async batches of 6 via Haiku (fast + cheap) |
| **OCR fallback** | Detected when page text density < 80 chars (requires pytesseract) |
| **Dashboard** | Coverage counts by sport, period, doc type; recent additions |

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full architecture, conventions, and brand guide.

## OCR for scanned PDFs

Install optional dependencies:

```bash
brew install tesseract
pip install pytesseract pdf2image Pillow
```

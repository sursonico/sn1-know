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

### Render persistent disk setup (SQLite + docs)

This app is already configured for Render disk paths in [render.yaml](render.yaml):

- `SN1_DB_PATH=/var/data/knowledge_base.db`
- `SN1_DOCS_DIR=/var/data/sample_docs`

The blueprint currently sets:

- `plan: starter` (cheapest paid instance that supports persistent disks)
- `disk.sizeGB: 5`

1. In Render Dashboard, set the service instance type to **Starter**.
1. Add/confirm a persistent disk on the service:
    - mount path: `/var/data`
    - size: `5 GB`
1. Upload your local `knowledge_base.db` and `sample_docs/` to `/var/data`.

Use the helper script in this repo:

```bash
chmod +x scripts/render_upload_and_verify.sh
export RENDER_SSH_HOST="YOUR_SERVICE@ssh.YOUR_REGION.render.com"
export LOCAL_DB_PATH="/absolute/path/to/knowledge_base.db"
export LOCAL_DOCS_DIR="/absolute/path/to/sample_docs"
./scripts/render_upload_and_verify.sh
```

Equivalent direct commands (if you prefer not to use the script):

```bash
ssh YOUR_SERVICE@ssh.YOUR_REGION.render.com "mkdir -p /var/data/sample_docs"
scp -s /absolute/path/to/knowledge_base.db YOUR_SERVICE@ssh.YOUR_REGION.render.com:/var/data/knowledge_base.db
scp -s -r /absolute/path/to/sample_docs/. YOUR_SERVICE@ssh.YOUR_REGION.render.com:/var/data/sample_docs/
ssh YOUR_SERVICE@ssh.YOUR_REGION.render.com "ls -lah /var/data && ls -lah /var/data/sample_docs | head"
```

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
Trigger deploy
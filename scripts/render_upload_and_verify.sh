#!/usr/bin/env bash
set -euo pipefail

# Upload SN1 SQLite DB + source documents to Render persistent disk, then verify.
#
# Required env vars:
#   RENDER_SSH_HOST   e.g. "sn1-know@ssh.oregon.render.com"
# Optional env vars:
#   LOCAL_DB_PATH     default: ./knowledge_base.db
#   LOCAL_DOCS_DIR    default: ./sample_docs
#   REMOTE_DATA_DIR   default: /var/data

if [[ -z "${RENDER_SSH_HOST:-}" ]]; then
  echo "ERROR: Set RENDER_SSH_HOST, e.g. export RENDER_SSH_HOST='sn1-know@ssh.oregon.render.com'"
  exit 1
fi

LOCAL_DB_PATH="${LOCAL_DB_PATH:-./knowledge_base.db}"
LOCAL_DOCS_DIR="${LOCAL_DOCS_DIR:-./sample_docs}"
REMOTE_DATA_DIR="${REMOTE_DATA_DIR:-/var/data}"
REMOTE_DB_PATH="${REMOTE_DATA_DIR}/knowledge_base.db"
REMOTE_DOCS_DIR="${REMOTE_DATA_DIR}/sample_docs"

if [[ ! -f "${LOCAL_DB_PATH}" ]]; then
  echo "ERROR: DB file not found at ${LOCAL_DB_PATH}"
  exit 1
fi

if [[ ! -d "${LOCAL_DOCS_DIR}" ]]; then
  echo "ERROR: docs directory not found at ${LOCAL_DOCS_DIR}"
  exit 1
fi

echo "Creating remote data directories..."
ssh "${RENDER_SSH_HOST}" "mkdir -p '${REMOTE_DOCS_DIR}'"

echo "Uploading SQLite database to ${REMOTE_DB_PATH}..."
scp -s "${LOCAL_DB_PATH}" "${RENDER_SSH_HOST}:${REMOTE_DB_PATH}"

echo "Uploading documents from ${LOCAL_DOCS_DIR} to ${REMOTE_DOCS_DIR}..."
scp -s -r "${LOCAL_DOCS_DIR}/." "${RENDER_SSH_HOST}:${REMOTE_DOCS_DIR}/"

echo "Running remote verification checks..."
ssh "${RENDER_SSH_HOST}" "set -e; \
  ls -lah '${REMOTE_DATA_DIR}'; \
  echo '---'; \
  ls -lah '${REMOTE_DOCS_DIR}' | head -n 30; \
  echo '---'; \
  python - <<'PY'\nfrom config import DB_PATH, DOCS_DIR\nprint('DB_PATH =', DB_PATH)\nprint('DOCS_DIR =', DOCS_DIR)\nPY"

echo "Done. Data uploaded and basic checks passed."

"""
config.py — single source of truth for all v2 settings.
Override any value with an environment variable of the same name.
"""
import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT_DIR  = Path(__file__).parent
# Root folder for original source files. Entries store `file_path` relative to it,
# so the same database resolves locally (./sample_docs) and on Render (persistent
# disk) just by pointing SN1_DOCS_DIR at the right folder. See kb/files.py.
DOCS_DIR  = Path(os.getenv("SN1_DOCS_DIR", str(ROOT_DIR / "sample_docs")))
DB_PATH   = Path(os.getenv("SN1_DB_PATH", str(ROOT_DIR / "knowledge_base.db")))
LOGO_PATH = ROOT_DIR / "sn1-logo.png"

# ── LLM models ─────────────────────────────────────────────────────────────
# Haiku for cheap/fast classification; Sonnet for final answers
CLASSIFY_MODEL  = os.getenv("SN1_CLASSIFY_MODEL",  "claude-haiku-4-5-20251001")
RETRIEVE_MODEL  = os.getenv("SN1_RETRIEVE_MODEL",  "claude-haiku-4-5-20251001")
ANSWER_MODEL    = os.getenv("SN1_ANSWER_MODEL",     "claude-sonnet-4-6")

# ── Ingestion ───────────────────────────────────────────────────────────────
INGEST_BATCH_SIZE  = int(os.getenv("SN1_BATCH_SIZE",  "6"))
MAX_CHARS_PER_DOC  = int(os.getenv("SN1_MAX_CHARS",   "15000"))
MAX_CHARS_PER_CHUNK = 3000      # chars stored per page/slide
OCR_CHAR_THRESHOLD    = 80      # chars/page below which OCR is attempted
VISION_CHAR_THRESHOLD        = int(os.getenv("SN1_VISION_THRESHOLD",    "50"))   # chars below which vision is attempted
VISION_IMAGE_COUNT_THRESHOLD = int(os.getenv("SN1_VISION_IMAGE_COUNT", "3"))    # embedded images to trigger hybrid vision
VISION_HYBRID_MAX_CHARS      = int(os.getenv("SN1_VISION_HYBRID_MAX",  "600"))  # max text chars for a hybrid page
VISION_MODEL                 = os.getenv("SN1_VISION_MODEL", "claude-sonnet-4-6")

# ── Retrieval ───────────────────────────────────────────────────────────────
FTS_CANDIDATE_LIMIT   = 30      # max entries returned from FTS keyword pass
STAGE2_MAX_CHUNKS          = 80   # max chunks sent to final-answer model (default)
STAGE2_BROAD_MAX_CHUNKS    = 160  # max chunks for exhaustive/list questions ("all markets", "every deal")
STAGE2_MULTISOURCE_MAX_CHUNKS = 120  # raised budget when a question spans 3+ distinct sources
STAGE2_FALLBACK_MAX_CHUNKS = 200  # Stage 1 failed → wide catalogue sweep needs room to find the answer
CONVERSATION_HISTORY  = 2       # prior turns included in follow-up context

# ── Deletion ────────────────────────────────────────────────────────────────
# Soft-deleted entries stay recoverable in Admin → Recently Deleted for this many
# days. Nothing is purged automatically; the Admin page offers a manual purge of
# entries past this window.
DELETED_RETENTION_DAYS = int(os.getenv("SN1_DELETED_RETENTION_DAYS", "30"))

# ── Access ────────────────────────────────────────────────────────────────
SHARE_PASSWORD = os.getenv("SN1_SHARE_PASSWORD", "")

# ── Brand ───────────────────────────────────────────────────────────────────
BRAND_NAVY  = "#2B383E"
BRAND_GOLD  = "#AA925C"
BRAND_CREAM = "#F3ECE0"
BRAND_WHITE = "#FFFFFF"

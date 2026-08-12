"""
kb/files.py — Locating the original source file for a document entry.

The document root is `SN1_DOCS_DIR` (config.DOCS_DIR), which falls back to
`<repo>/sample_docs` when the variable is unset. Entries store `file_path`
relative to that root, so the same database works locally and on Render, where
the folder lives on the persistent disk under a different absolute path.

Rows written by older versions stored an absolute path; `resolve_source_file()`
still finds those by matching their trailing path segments — and finally the bare
filename — underneath the current root.

Streamlit is deliberately not imported here: ingestion and retrieval run headless.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from config import DOCS_DIR


def store_path(path: Path, docs_dir: Path = DOCS_DIR) -> str:
    """
    The value to persist in `entries.file_path`: relative to the docs root when the
    file lives inside it (the normal case — ingestion copies files in), else the
    absolute path so an out-of-tree file is still addressable on this machine.
    """
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path(docs_dir).resolve()))
    except ValueError:
        return str(resolved)


def _under_root(candidate: Path, root: Path) -> bool:
    """True if `candidate` stays inside `root` — blocks '../..' escapes from stored paths."""
    try:
        candidate.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


@lru_cache(maxsize=512)
def _find_by_name(root_str: str, name: str) -> Optional[str]:
    """Last resort: recursive search for `name` under the docs root. Cached per name."""
    root = Path(root_str)
    if not name or not root.is_dir():
        return None
    try:
        for hit in root.rglob(name):
            if hit.is_file():
                return str(hit)
    except OSError:
        return None
    return None


def resolve_source_file(
    entry: Union[dict, str, Path, None],
    docs_dir: Path = DOCS_DIR,
) -> Optional[Path]:
    """
    Return the readable location of an entry's original file, or None if it can't
    be found. Accepts an entry dict (uses `file_path`, falling back to `source`)
    or a bare path.

    Tried in order:
      1. stored path relative to the docs root (how new entries are written)
      2. the stored path as-is, if absolute and present on this machine
      3. trailing segments of a stale absolute path, re-rooted at the docs root
      4. `<docs root>/<source>` — the filename recorded on the entry
      5. a recursive search for that filename under the docs root
    """
    if entry is None:
        return None
    if isinstance(entry, dict):
        stored = (entry.get("file_path") or "").strip()
        source = (entry.get("source") or "").strip()
    else:
        stored, source = str(entry).strip(), ""

    root = Path(docs_dir).resolve()

    if stored:
        raw = Path(stored)
        if not raw.is_absolute():
            candidate = root / raw
            if _under_root(candidate, root) and candidate.is_file():
                return candidate
        elif raw.is_file():
            return raw

        # A path from another machine (or another disk layout): keep re-rooting
        # progressively longer tails — '<root>/file.pdf', '<root>/sub/file.pdf', …
        parts = raw.parts[1:] if raw.is_absolute() else raw.parts
        for depth in range(1, min(len(parts), 4) + 1):
            candidate = root.joinpath(*parts[-depth:])
            if candidate.is_file():
                return candidate

    if source:
        candidate = root / source
        if _under_root(candidate, root) and candidate.is_file():
            return candidate
        found = _find_by_name(str(root), Path(source).name)
        if found:
            return Path(found)

    if stored:
        found = _find_by_name(str(root), Path(stored).name)
        if found:
            return Path(found)

    return None


def clear_lookup_cache() -> None:
    """Drop the filename-search cache — call after ingesting new files."""
    _find_by_name.cache_clear()

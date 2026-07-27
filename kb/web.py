"""
kb/web.py — Server-side URL fetching and article text extraction.

Extraction strategy (three-pass with increasing recall):
  1. trafilatura — most accurate, handles most news/blog pages
  2. BeautifulSoup content-region heuristic — catches pages trafilatura under-extracts;
     renders HTML tables as readable pipe-separated rows and preserves lists
  3. (Planned) headless browser — for JS-heavy sites; not implemented yet

Returns a plain dict so the caller decides what to do with errors.
"""

from __future__ import annotations

import re
import requests
import trafilatura


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

MAX_ARTICLE_CHARS = 25_000   # cap sent to Claude
SHORT_WARN_WORDS  = 300      # warn user if extraction looks thin


# ── Extraction ────────────────────────────────────────────────────────────────

def _trafilatura_extract(html: str) -> str:
    """
    Attempt extraction with all recall-oriented trafilatura settings.
    Returns empty string if the result is too short to be useful.
    """
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        include_links=True,
        include_formatting=True,
        no_fallback=False,
        favor_recall=True,
        deduplicate=False,     # don't drop repeated sentences (common in tables)
    ) or ""
    return text


def _render_table(table) -> str:
    """Convert a BeautifulSoup table element to pipe-separated text rows."""
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
        if any(c for c in cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _bs_extract(html: str) -> str:
    """
    BeautifulSoup content-region extraction.
    Finds the main article body using structural/class heuristics, then
    renders paragraphs, headings, tables, and lists as clean text.
    """
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html, "html.parser")

    # Strip boilerplate elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside",
                              "header", "form", "iframe", "noscript",
                              "figure", "figcaption"]):
        tag.decompose()

    # Locate main content region — try progressively broader selectors
    _content_classes = re.compile(
        r"article|content|post|body|story|entry|main|text|copy", re.I
    )
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find(class_=_content_classes)
        or soup.find(id=_content_classes)
        or soup.body
    )
    if not main:
        return ""

    parts: list[str] = []
    seen: set[str] = set()   # deduplicate identical short strings (nav remnants)

    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "table", "ul", "ol"], recursive=True):
        # Skip elements nested inside already-processed table cells
        if el.find_parent("table") and el.name not in ("table",):
            continue

        if el.name == "table":
            rendered = _render_table(el)
            if rendered:
                parts.append(rendered)
        elif el.name in ("ul", "ol"):
            # Only process top-level lists (nested handled by parent iteration)
            if el.find_parent(["ul", "ol"]):
                continue
            items = [
                f"• {li.get_text(' ', strip=True)}"
                for li in el.find_all("li", recursive=False)
                if li.get_text(strip=True)
            ]
            if items:
                parts.append("\n".join(items))
        elif el.name in ("h1", "h2", "h3", "h4"):
            text = el.get_text(" ", strip=True)
            if text and text not in seen:
                seen.add(text)
                level = "#" * int(el.name[1])
                parts.append(f"{level} {text}")
        else:  # <p>
            text = el.get_text(" ", strip=True)
            if text and len(text) > 20 and text not in seen:
                seen.add(text)
                parts.append(text)

    return "\n\n".join(parts)


def fetch_article(url: str) -> dict:
    """
    Fetch *url* and extract the main article text using a two-pass strategy.

    Returns::

        {
            "title":       str,
            "text":        str,    # extracted body (up to MAX_ARTICLE_CHARS)
            "word_count":  int,
            "char_count":  int,
            "short":       bool,   # True if extraction looks suspiciously thin
            "method":      str,    # "trafilatura" | "beautifulsoup" | "none"
            "error":       None | str,
        }

    Error codes: "timeout" | "http_NNN" | "unreachable" | "paywall" | "other:…"
    """
    # ── HTTP fetch ────────────────────────────────────────────────────────────
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return _err("timeout")
    except requests.exceptions.HTTPError:
        return _err(f"http_{resp.status_code}")
    except requests.exceptions.ConnectionError:
        return _err("unreachable")
    except Exception as exc:
        return _err(f"other:{exc}")

    html = resp.text

    # ── Page title ────────────────────────────────────────────────────────────
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception:
        pass

    # ── Pass 1: trafilatura ───────────────────────────────────────────────────
    body = _trafilatura_extract(html)
    method = "trafilatura"

    # ── Pass 2: BeautifulSoup fallback if trafilatura got too little ──────────
    if len(body.split()) < 150:
        bs_body = _bs_extract(html)
        if len(bs_body.split()) > len(body.split()):
            body = bs_body
            method = "beautifulsoup"

    if len(body.strip()) < 80:
        return {**_err("paywall"), "title": title, "method": "none"}

    text       = body[:MAX_ARTICLE_CHARS]
    word_count = len(text.split())
    char_count = len(text)
    short      = word_count < SHORT_WARN_WORDS

    return {
        "title":      title,
        "text":       text,
        "word_count": word_count,
        "char_count": char_count,
        "short":      short,
        "method":     method,
        "error":      None,
    }


# ── Error messages ────────────────────────────────────────────────────────────

def error_message(error: str) -> tuple[str, str]:
    """Return (headline, detail) for a fetch error code."""
    if error == "timeout":
        return (
            "Request timed out",
            "The server took too long to respond. Try again, or paste the text manually.",
        )
    if error in ("http_401", "http_403"):
        code = error.split("_")[1]
        return (
            f"Access denied (HTTP {code})",
            "This page requires a login or subscription. Paste the article text manually instead.",
        )
    if error == "http_402":
        return (
            "Paywalled article",
            "This article requires payment. Copy the text from your subscription and paste it manually.",
        )
    if error == "http_404":
        return (
            "Page not found (404)",
            "Check the URL — the page may have moved or been deleted.",
        )
    if error.startswith("http_5"):
        code = error.split("_")[1]
        return (
            f"Server error (HTTP {code})",
            "The site is having problems. Try again later, or paste the text manually.",
        )
    if error == "paywall":
        return (
            "Couldn't extract article text",
            (
                "The page loaded but no readable body was found. "
                "This usually means the content is behind a paywall, requires a login, "
                "or is rendered by JavaScript after page load. "
                "Paste the article text manually instead."
            ),
        )
    if error == "unreachable":
        return (
            "Could not connect",
            "Check the URL and your internet connection, or paste the text manually.",
        )
    return (
        "Unexpected error",
        f"Something went wrong ({error}). Try pasting the text manually.",
    )


def _err(code: str) -> dict:
    return {
        "title": "", "text": "", "word_count": 0,
        "char_count": 0, "short": True, "method": "none", "error": code,
    }

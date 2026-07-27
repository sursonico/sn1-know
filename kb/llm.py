"""
kb/llm.py — LLM client for the SN1 Knowledge Base.

Uses the Anthropic SDK (async + sync) when ANTHROPIC_API_KEY is set;
falls back to the `claude` CLI subprocess (Claude Code OAuth) otherwise.
"""

import json
import os
import subprocess
import textwrap
from typing import Optional

from config import CLASSIFY_MODEL, RETRIEVE_MODEL, ANSWER_MODEL

# ── Raw LLM call ─────────────────────────────────────────────────────────────

def call_claude(
    system: str,
    user: str,
    model: str = ANSWER_MODEL,
    max_tokens: int = 1024,
    timeout: int = 90,
) -> str:
    """Synchronous LLM call. SDK if API key present, else CLI."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text.strip()

    result = subprocess.run(
        ["claude", "-p", f"{system}\n\n{user}", "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


async def call_claude_async(
    system: str,
    user: str,
    model: str = CLASSIFY_MODEL,
    max_tokens: int = 700,
) -> str:
    """Async LLM call for batch processing. Requires ANTHROPIC_API_KEY."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Graceful fallback: run synchronous CLI in a thread
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: call_claude(system, user, model, max_tokens)
        )
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


# ── Vision (SDK only) ─────────────────────────────────────────────────────────

def vision_sdk_available() -> bool:
    """True only when ANTHROPIC_API_KEY is set — CLI path cannot handle images."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_VISION_SYSTEM = textwrap.dedent("""
    You are analyzing a page or slide from a sports media rights intelligence document.
    Extract all visible information so it can be indexed and searched.

    Identify and describe:
    1. Logos and channel/broadcaster branding — name each broadcaster or channel you recognise
    2. Sports competitions, properties, or events — note which broadcaster they appear under or alongside
    3. All visible text (headings, labels, captions, dates, territory or market names)
    4. Grid or table layouts — which sports properties are grouped under which broadcaster channel

    Use this output format (include all applicable sections):

    VISUAL SUMMARY:
    [Overall description of the page or slide]

    BROADCASTERS AND PROPERTIES:
    [Broadcaster/channel name]: [sports property 1], [sports property 2], ...
    [Broadcaster/channel name]: [sports property 1], ...
    (one line per broadcaster; repeat for every broadcaster visible)

    TERRITORY: [Country or region if visible anywhere]

    OTHER TEXT:
    [Any remaining visible text not captured above]

    Rules:
    - Only state what is actually visible — do not invent or guess beyond what you can see
    - If you cannot identify a logo with confidence, describe its appearance and any text on it
    - Include every broadcaster/channel you can identify even if you cannot read all associated properties
""").strip()


async def describe_page_images_async(
    image_blobs: list,
    context_hint: str = "",
) -> Optional[str]:
    """
    Send page/slide image(s) to Claude vision (SDK only). Returns description or None.

    image_blobs: list of PNG or JPEG bytes. Up to 10 are sent.
    context_hint: any text already extracted from the page, shown as context.
    """
    if not vision_sdk_available():
        return None

    import anthropic
    import base64
    from config import VISION_MODEL

    def _media_type(blob: bytes) -> Optional[str]:
        if len(blob) < 4:
            return None
        if blob[:4] == b'\x89PNG':
            return "image/png"
        if blob[:2] == b'\xff\xd8':
            return "image/jpeg"
        if blob[:4] == b'RIFF' and len(blob) > 12 and blob[8:12] == b'WEBP':
            return "image/webp"
        return None  # unsupported (WMF, EMF, BMP…)

    valid: list = []
    for blob in image_blobs:
        mt = _media_type(blob)
        if mt and len(blob) < 5_000_000:
            valid.append((blob, mt))
        if len(valid) >= 10:
            break

    if not valid:
        return None

    content: list = []
    if context_hint:
        content.append({"type": "text", "text": f"Context (text already extracted from this page):\n{context_hint}"})

    for blob, mt in valid:
        b64 = base64.standard_b64encode(blob).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mt, "data": b64},
        })

    content.append({
        "type": "text",
        "text": "Describe all content visible in this page or slide.",
    })

    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model=VISION_MODEL,
        max_tokens=1500,
        system=_VISION_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text.strip()


def _parse_json(raw: str, fallback: dict) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {**fallback, "notes": raw[:120]}


# ── Classification ────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights research analyst.
    Given a filename and document text excerpt, return ONLY a JSON object:
    {
      "sports_leagues":  "comma-separated sports/leagues (or 'Unknown')",
      "doc_date":        "when this document was written or published — year or date (e.g. '2024', '2024-03', or 'Unknown')",
      "coverage_period": "the time period this document DESCRIBES, if different from doc_date — e.g. a rights cycle '2025-2028', a season, a future contract period. Leave blank if the same as doc_date or not applicable.",
      "doc_type":        "one of: Market Assessment | Industry Report | Trade Publication | Pitch Deck | Data / Raw Data | Financial Analysis | Rights Sales Deck | Legal / Contract | Press Release | Strategy Memo | Other",
      "notes":           "one sentence ≤20 words, most important fact (blank if nothing distinctive)",
      "summary":         "2–3 sentences: main content, purpose, key findings",
      "topics":          "8–12 comma-separated keywords for retrieval",
      "org_tags":        "comma-separated organisations/companies mentioned",
      "market_tags":     "comma-separated geographic markets/regions",
      "reliability":     "one of: confirmed | reported | rumoured — infer from source type and language:
                          confirmed = official press release from rights holder or broadcaster; signed contract; regulatory filing; official club/league statement
                          reported  = trade press article (SportBusiness, Front Office Sports, etc.); mainstream journalism; analyst report; market data; industry research
                          rumoured  = call note; internal memo; meeting notes; speculative language ('reportedly', 'expected to', 'said to', 'may', 'could', 'sources say'); unattributed intel; draft document"
    }
    Respond with only the JSON object, no markdown fences.
""").strip()

_CLASSIFY_FALLBACK = {
    "sports_leagues": "Unknown", "doc_date": "Unknown", "coverage_period": "",
    "doc_type": "Unknown", "notes": "", "summary": "",
    "topics": "", "org_tags": "", "market_tags": "", "reliability": "reported",
}


def classify_document(filename: str, text: str) -> dict:
    raw = call_claude(
        _CLASSIFY_SYSTEM,
        f"Filename: {filename}\n\nText excerpt:\n\n{text}",
        model=CLASSIFY_MODEL, max_tokens=700,
    )
    return _parse_json(raw, _CLASSIFY_FALLBACK)


async def classify_document_async(filename: str, text: str) -> dict:
    raw = await call_claude_async(
        _CLASSIFY_SYSTEM,
        f"Filename: {filename}\n\nText excerpt:\n\n{text}",
        model=CLASSIFY_MODEL, max_tokens=700,
    )
    return _parse_json(raw, _CLASSIFY_FALLBACK)


# ── Enrichment (backfill) ─────────────────────────────────────────────────────

_ENRICH_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights research analyst.
    Return ONLY a JSON object with two keys:
    {
      "summary": "2–3 sentences: main content, purpose, key findings",
      "topics":  "8–12 comma-separated keywords for retrieval"
    }
    Respond with only the JSON object, no markdown fences.
""").strip()


def enrich_document(filename: str, text: str) -> dict:
    raw = call_claude(
        _ENRICH_SYSTEM,
        f"Filename: {filename}\n\nText excerpt:\n\n{text}",
        model=CLASSIFY_MODEL, max_tokens=400,
    )
    return _parse_json(raw, {"summary": "", "topics": ""})


# ── Snippet enrichment ────────────────────────────────────────────────────────

_SNIPPET_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights intelligence analyst.
    Return ONLY a JSON object:
    {
      "summary":         "one sentence ≤20 words capturing the key information",
      "coverage_period": "the rights cycle, season, or time span this note describes (e.g. '2025-2028', '2024-25 season'). Leave blank if not applicable.",
      "org_tags":        "comma-separated organisations/companies mentioned",
      "market_tags":     "comma-separated geographic markets or regions",
      "sport_tags":      "comma-separated sports and leagues mentioned",
      "topic_tags":      "4–8 comma-separated key topics and concepts",
      "reliability":     "one of: confirmed | reported | rumoured — infer from the note's language and apparent source:
                          confirmed = official announcement, signed deal, confirmed fact from an authoritative source
                          reported  = trade press, journalism, analyst commentary, secondhand information
                          rumoured  = speculative language ('reportedly', 'expected to', 'said to', 'may', 'could', 'sources say'); call notes; unattributed intel; internal discussion"
    }
    Return empty string for any category with nothing clearly mentioned.
    Respond with only the JSON object, no markdown fences.
""").strip()


# ── URL article enrichment ───────────────────────────────────────────────────

_URL_ARTICLE_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights intelligence analyst processing an article for a
    deal-tracking knowledge base. Your job is EXTRACTION, not summarisation.

    Return ONLY a JSON object with these keys:

    "summary"  — 2–3 sentences: what the article is about and why it matters.
                 This appears in Browse view as a quick-scan label.

    "detail"   — The concrete intelligence content, preserved with full specificity.
                 Include ALL of the following that appear in the article:
                   • Broadcaster / platform names and the exact markets/territories
                     they cover (e.g. "Sky Sports: UK & Ireland exclusive")
                   • Rights deal values, durations, and start/end dates
                   • Competition or event names with governing body
                   • Named sub-licences, sub-packages, or digital/streaming carve-outs
                   • Any figures, percentages, audience numbers, or financial terms
                   • Broadcaster-by-market tables — preserve the full table structure
                     as a readable list, not compressed into one line
                 Write this as structured bullet points or short labelled paragraphs.
                 DO NOT compress or omit specifics. If a table lists 15 countries and
                 broadcasters, keep all 15 rows.

    "coverage_period" — the rights cycle or time span this article describes (e.g. '2025-2028', '2024-25 season'). Leave blank if not a rights-deal article or the period is unclear.
    "org_tags"        — comma-separated organisations, broadcasters, or rights holders featured
    "market_tags"     — comma-separated geographic markets or regions featured
    "sport_tags"      — comma-separated sports and competitions featured
    "topic_tags"      — comma-separated key topics (5–8 items)
    "reliability"     — one of: confirmed | reported | rumoured
                        confirmed = official press release or broadcaster announcement; signed contract; regulatory filing
                        reported  = trade press (SportBusiness, etc.); mainstream journalism; analyst report; industry data
                        rumoured  = speculative language ('reportedly', 'expected to', 'said to', 'may', 'sources say'); unattributed intel

    Return empty strings for any category with nothing clearly present.
    Respond with ONLY the JSON object, no markdown fences.
""").strip()


_URL_MAX_CLAUDE_CHARS = 12_000   # keep prompts manageable; key facts are near the top


def enrich_url_article(text: str, url: str) -> dict:
    """
    Extract structured intelligence from a web article.
    Returns summary + detail (facts/tables/deal specifics) + entity tags.
    Uses the answer model (Sonnet) for better extraction fidelity.
    """
    # Trim to _URL_MAX_CLAUDE_CHARS — the most important content is early in articles;
    # this also keeps the prompt within the CLI subprocess timeout.
    trimmed = text[:_URL_MAX_CLAUDE_CHARS]
    if len(text) > _URL_MAX_CLAUDE_CHARS:
        trimmed += f"\n\n[… article continues — {len(text) - _URL_MAX_CLAUDE_CHARS:,} chars omitted …]"

    raw = call_claude(
        _URL_ARTICLE_SYSTEM,
        f"URL: {url}\n\nArticle text:\n\n{trimmed}",
        model=ANSWER_MODEL,   # Sonnet — extraction fidelity matters more than speed
        max_tokens=2000,
        timeout=150,          # longer than default; large payloads are slow via CLI
    )
    return _parse_json(raw, {
        "summary": "", "detail": "", "coverage_period": "", "org_tags": "", "market_tags": "",
        "sport_tags": "", "topic_tags": "",
    })


def enrich_snippet(text: str, source: str) -> dict:
    raw = call_claude(
        _SNIPPET_SYSTEM,
        f"Source: {source}\n\nNote:\n\n{text}",
        model=CLASSIFY_MODEL, max_tokens=500,
    )
    return _parse_json(raw, {
        "summary": "", "coverage_period": "", "org_tags": "", "market_tags": "",
        "sport_tags": "", "topic_tags": "",
    })


# ── Stage 1 retrieval ─────────────────────────────────────────────────────────

_RETRIEVE_SYSTEM = textwrap.dedent("""
    You are a retrieval assistant for a sports media rights knowledge base (documents + notes).
    Given a user question and a catalogue, identify which entries most likely contain the answer.
    Return ONLY a JSON object:
    {
      "selected_ids": [3, 7, 12],
      "rationale": "one sentence explaining the selection"
    }
    Rules:
    - IDs match the "Entry #N" labels in the catalogue.
    - Include an entry if its summary, topics, sport/league, date, or source are relevant.
    - Prefer CURRENT entries over SUPERSEDED ones — include superseded only if no current source covers the topic.
    - Prefer CONFIRMED entries over RUMOURED ones when both cover the same topic.
    - Be selective but err toward inclusion for borderline entries.
    - Return [] only if nothing is relevant.
    Respond with only the JSON object, no markdown fences.
""").strip()


def build_catalogue_context(rows: list[dict]) -> str:
    parts = []
    for row in rows:
        etype       = row.get("entry_type", "document")
        source      = row.get("source", "?")
        edate       = row.get("entry_date", "")
        status      = row.get("status", "current")
        reliability = row.get("reliability", "reported")
        status_badge = f" [{status.upper()}]" if status != "current" else ""
        rel_badge    = f" [reliability:{reliability}]"

        cov = row.get("coverage_period", "")
        period_str = edate
        if cov and cov != edate:
            period_str = f"{edate} (covers: {cov})" if edate else f"covers: {cov}"

        if etype == "document":
            header = f"Entry #{row['id']} [document]{status_badge}{rel_badge}: {source}"
            meta   = (
                f"  Format: {row.get('file_type','')} | "
                f"Doc type: {row.get('doc_type','')} | "
                f"Period: {period_str}"
            )
        else:
            header = f"Entry #{row['id']} [note]{status_badge}{rel_badge}: {edate}, {source}"
            meta   = f"  Type: Note | Date: {edate}" + (f" | Covers: {cov}" if cov and cov != edate else "")

        lines = [header, meta]
        for label, key in [
            ("Orgs", "org_tags"), ("Markets", "market_tags"),
            ("Sport/League", "sport_tags"), ("Summary", "summary"),
            ("Topics", "topic_tags"),
        ]:
            val = row.get(key, "")
            if val:
                lines.append(f"  {label}: {val}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def select_relevant_entries(
    all_rows: list[dict],
    question: str,
    fts_boost_ids: Optional[list[int]] = None,
) -> tuple[list[int], str]:
    """
    Stage 1: Claude selects relevant entry IDs from the catalogue.
    FTS-boosted IDs are highlighted at the top of the context.
    Returns (selected_ids, rationale).
    """
    # Build context, highlighting FTS hits
    boost_set = set(fts_boost_ids or [])
    boosted   = [r for r in all_rows if r["id"] in boost_set]
    rest      = [r for r in all_rows if r["id"] not in boost_set]

    parts = []
    if boosted:
        parts.append("--- Keyword-search matches (likely relevant) ---")
        parts.append(build_catalogue_context(boosted))
        parts.append("--- Full catalogue ---")
    parts.append(build_catalogue_context(rest))
    context = "\n\n".join(parts)

    raw = call_claude(
        _RETRIEVE_SYSTEM,
        f"Catalogue:\n\n{context}\n\nQuestion: {question}",
        model=RETRIEVE_MODEL, max_tokens=400, timeout=60,
    )
    result   = _parse_json(raw, {"selected_ids": [], "rationale": ""})
    selected = result.get("selected_ids", [])
    rationale = result.get("rationale", "")

    all_ids  = {r["id"] for r in all_rows}
    if not isinstance(selected, list) or not selected:
        # Fall back to FTS results only, then all
        fallback = list(boost_set & all_ids) or list(all_ids)
        return fallback[:20], "Falling back (could not parse Stage 1 selection)."

    valid = [int(i) for i in selected if int(i) in all_ids]
    if not valid:
        return list(boost_set & all_ids) or [], "No valid IDs returned."
    return valid, rationale


# ── Stage 2 answer ────────────────────────────────────────────────────────────

_ANSWER_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights research analyst. Answer the user's question
    accurately and concisely using only the provided sources. After each relevant
    claim, cite the exact source in brackets as shown in each "CITE AS:" header,
    adding the reliability level and date.
    Examples: [UEFA_rights.pptx, slide 3, confirmed, 2024] or [logged note — 2026-06-10, SportBusiness, rumoured].

    Data freshness, reliability, and conflict rules (IMPORTANT):
    - Each source block includes its Date (when written), Covers (the rights period described), and Reliability.
    - Include BOTH reliability and date in every citation of a time-sensitive claim.
    - Always surface the rights period in your answer when it is known — e.g. "as of the 2025–2028 cycle" or "for the 2024–2027 deal period".
    - Lead with the most recent/current deal cycle; clearly mark older cycles as historical context.
    - Reliability meanings:
        confirmed = official press release, signed contract, regulatory filing, official league/club statement
        reported  = trade press, journalism, analyst report, market data
        rumoured  = call note, internal memo, speculative language, unattributed intel
    - Reliability + recency precedence:
        1. confirmed beats rumoured regardless of recency (unless the confirmed source is 5+ years old and clearly outdated)
        2. confirmed beats reported unless the reported source is substantially more recent AND more specific
        3. reported beats rumoured at equal recency
        4. Same reliability → prefer the more recent source
    - If two sources state contradictory facts about the same entity, flag it immediately after
      stating the facts using this format:
        "⚠ Conflict [confirmed vs rumoured, 2024-03 vs 2026-01]: [source A, p.2, confirmed, 2024-03]
        states X; [source B, reported, 2026-01] states Y — the confirmed source / the more recent source takes precedence."
      Name both sources, their reliability levels, their dates, and which takes precedence and why.
    - Sources marked [SUPERSEDED]: cite only for historical context and note they have been superseded.
    - Sources marked [UNVERIFIED]: hedge with "reportedly" or "according to unverified intelligence".

    If the sources do not contain enough information to answer, say:
    "The knowledge base does not contain sufficient information to answer this question."
    Do not speculate beyond the provided sources.

    Coverage transparency (REQUIRED):
    - The context may include a STRUCTURED DEALS DATABASE section at the top — treat those rows
      as authoritative structured data and cite them as [Deals database — <entity>, <territory>].
    - Sources marked [⚠ TRUNCATED] or [⚠ Context budget reached — N pages not retrieved] mean
      some pages of that source were not loaded. If you can infer from visible content that the
      missing pages likely contain additional relevant data (e.g. a table is partially shown,
      a total figure implies more rows than are visible, or a source mentions "X markets" but
      fewer than X are listed), say so explicitly:
        "Note: [source] was only partially retrieved (pages N–M not loaded). The answer above
         may be incomplete — [describe what appears to be missing, e.g. 'deal data for the
         remaining markets']. A focused follow-up question could surface the missing sections."
    - For list or enumeration questions: if your answer covers only part of what exists, say
      so and state the count if you can infer it (e.g. "5 of an estimated 20 deals visible").
""").strip()


def generate_answer(
    context: str,
    question: str,
    conversation_history: Optional[list[dict]] = None,
    max_tokens: int = 2000,
) -> str:
    """
    Stage 2: Generate a cited answer from the provided chunk context.
    Optionally includes recent conversation turns for follow-up support.
    max_tokens is raised by the caller for broad/exhaustive questions.
    """
    history_text = ""
    if conversation_history:
        turns = conversation_history[-2:]  # last 2 turns
        history_text = "\n\n".join(
            f"Previous question: {t['q']}\nPrevious answer (summary): {t['a'][:400]}…"
            for t in turns
        )

    user_parts = []
    if history_text:
        user_parts.append(f"Conversation context:\n{history_text}")
    user_parts.append(f"Sources:\n\n{context}")
    user_parts.append(f"Question: {question}")

    return call_claude(
        _ANSWER_SYSTEM,
        "\n\n".join(user_parts),
        model=ANSWER_MODEL,
        max_tokens=max_tokens,
    )


# ── Input conflict detection ──────────────────────────────────────────────────

_CONFLICT_CHECK_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights intelligence analyst reviewing a knowledge base for contradictions.
    You will be given a NEW entry being added, followed by EXISTING entries for the same entities.
    Identify genuine factual contradictions: different rights holders for the same territory,
    different deal values or durations, conflicting broadcaster information, inconsistent
    contract dates. Do NOT flag differences that could be explained by different time periods
    (a 2023 deal can coexist with a 2026 deal), different sub-markets, or additional detail.

    When identifying conflicts, note the reliability of both the new and existing entries:
    confirmed = official source; reported = trade press/journalism; rumoured = speculative/call note.
    A confirmed new entry contradicting a rumoured existing entry is still a conflict, but
    the confirmed entry is almost certainly correct.

    Return ONLY a JSON object:
    {
      "has_conflict": true or false,
      "conflicts": [
        {
          "existing_entry_id": 42,
          "existing_entry_source": "SportBusiness",
          "existing_entry_date": "2025-03",
          "existing_entry_reliability": "reported",
          "description": "one sentence describing the specific contradiction",
          "field": "one of: rights_holder | deal_value | territory | dates | broadcaster | other",
          "likely_winner": "new or existing — which is more likely correct, based on reliability and recency"
        }
      ],
      "summary": "one sentence summarising the conflict(s), or empty string if none"
    }
    Respond with only the JSON object, no markdown fences.
""").strip()


def check_new_entry_conflicts(
    new_summary: str,
    new_detail: str,
    existing_entries: list[dict],
) -> dict:
    """
    Check whether a new entry contradicts existing entries for the same entities.
    Returns {"has_conflict": bool, "conflicts": [...], "summary": str}.
    Fast — uses CLASSIFY_MODEL (Haiku).
    """
    if not existing_entries:
        return {"has_conflict": False, "conflicts": [], "summary": ""}

    new_part = f"NEW ENTRY:\n{new_summary}"
    if new_detail:
        new_part += f"\n\nDetail:\n{new_detail[:2000]}"

    existing_parts = []
    for e in existing_entries[:8]:  # cap to keep prompt manageable
        date_str = (e.get("entry_date") or e.get("created_at", "unknown"))[:10]
        existing_parts.append(
            f"Entry #{e['id']} [{date_str}] — {e.get('source','?')}\n"
            f"Summary: {e.get('summary','(no summary)')}"
        )

    user_msg = (
        f"{new_part}\n\n"
        f"---\n\n"
        f"EXISTING ENTRIES FOR THE SAME ENTITIES:\n\n"
        + "\n\n".join(existing_parts)
    )

    raw = call_claude(
        _CONFLICT_CHECK_SYSTEM, user_msg,
        model=CLASSIFY_MODEL, max_tokens=600,
    )
    return _parse_json(raw, {"has_conflict": False, "conflicts": [], "summary": ""})


# ── Deal extraction ───────────────────────────────────────────────────────────

_DEAL_EXTRACT_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights intelligence analyst. Extract broadcaster–territory
    mappings and deal terms from the provided text.
    Return a JSON array — one element per broadcaster–territory pair:
    {
      "entity_name":  "canonical name of the entity whose rights are licensed
                       (must exactly match one of the provided known entities)",
      "territory":    "geographic market — be specific (e.g. 'United Kingdom', 'Germany', 'MENA')",
      "broadcaster":  "licensee/broadcaster name (e.g. 'Sky Sports', 'DAZN', 'Rally.TV')",
      "rights_holder":"who owns/sells the rights — leave blank if same as the entity",
      "value":        null or numeric in millions (500 = £500m, 1200 = £1.2bn),
      "currency":     "'GBP' / 'EUR' / 'USD' / etc., or blank if not stated",
      "value_note":   "ONLY a brief qualifying phrase about payment structure — e.g. 'per season',\n                'total across deal', 'annually'. NEVER include numbers, currency symbols, or\n                monetary amounts here — those belong in 'value' and 'currency'. Use\n                'undisclosed' if a deal exists but no amount is stated. Blank if no qualifier.",
      "period_start": "start year or date e.g. '2022' or '2022-08'",
      "period_end":   "end year or date e.g. '2025' or '2025-05'",
      "platform":     "'TV' / 'streaming' / 'digital' / 'all rights' / etc.",
      "confidence":   "'high' (values and dates explicit) / 'medium' / 'low'"
    }
    Rules:
    - Require both broadcaster AND territory — omit rows missing either.
    - If one broadcaster covers N territories, create N separate rows.
    - A broadcaster–territory mapping in a rights guide, broadcast schedule, or
      viewing guide IS a deal row even with no financial terms — use value=null
      and value_note blank (or 'undisclosed' if the text implies a deal exists
      but gives no amount). Do NOT skip rows merely because price/period is absent.
    - Never invent values — use value=null for unknown amounts.
    - entity_name MUST match one of the known entities listed below, exactly.
    - Return [] only if no broadcaster–territory mappings at all are found.
      Skip historical examples used only for comparison.
    - A row represents a CONFIRMED or HISTORICAL rights grant — a deal that exists or existed.
      DO NOT create a row for:
      · Declined or withdrawn bids: "X declined", "X withdrew before the deadline",
        "no bid was submitted", "X chose not to participate", "X did not renew"
      · Failed negotiations: "no agreement was reached", "talks collapsed",
        "negotiations broke down", "the offer was rejected"
      · Explicit negations of a deal: "X did not acquire", "X showed no interest",
        "no deal between X and Y", "beIN did not show interest in the UEFA rights"
      · Unresolved speculation: "X is considering", "X may bid", "X expressed interest
        but no deal was signed", "rumoured to be exploring" — omit unless the deal
        is confirmed elsewhere in the same text.
      These are valuable intelligence facts. They are captured in the searchable
      source text for retrieval via Ask — they must NOT become deal rows.
    Known entities:
""").strip()


def extract_deals(
    text: str,
    entity_names: list[str],
    source_hint: str = "",
) -> list[dict]:
    """
    Extract structured deal terms from text. entity_names are the canonical entity names
    relevant to this document/snippet. Returns [] on failure or if nothing found.
    """
    if not entity_names or not text.strip():
        return []
    system = _DEAL_EXTRACT_SYSTEM + "\n" + "\n".join(f"- {n}" for n in entity_names)
    hint = f"Source: {source_hint}\n\n" if source_hint else ""
    user = f"{hint}Text:\n\n{text[:15000]}"
    try:
        raw = call_claude(system, user, model=CLASSIFY_MODEL, max_tokens=2000, timeout=150)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:]).rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


async def extract_deals_async(
    text: str,
    entity_names: list[str],
    source_hint: str = "",
) -> list[dict]:
    """Async version of extract_deals for use during batch ingestion."""
    if not entity_names or not text.strip():
        return []
    system = _DEAL_EXTRACT_SYSTEM + "\n" + "\n".join(f"- {n}" for n in entity_names)
    hint = f"Source: {source_hint}\n\n" if source_hint else ""
    user = f"{hint}Text:\n\n{text[:15000]}"
    try:
        raw = await call_claude_async(system, user, model=CLASSIFY_MODEL, max_tokens=1500)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:]).rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Entity resolution ─────────────────────────────────────────────────────────

_ENTITY_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights intelligence analyst building a knowledge base.
    Given metadata extracted from a document or note, identify only the entities the
    document is SUBSTANTIVELY ABOUT — primary subjects and genuinely significant
    secondary ones. Do NOT include every passing mention.

    Return a JSON array where each element has:
      "canonical"  — standard full name (e.g. "UEFA Champions League", never "UCL")
      "type"       — one of: competition | federation | broadcaster | market |
                             rights_holder | club | client | other
      "role"       — "primary"   if the document is substantially about this entity
                     "secondary" if it features significantly but is not the main subject
      "is_new"     — true only for unusual/niche entities not in standard reference lists

    Rules:
    - Resolve abbreviations: UCL→UEFA Champions League, EPL→Premier League, etc.
    - Exclude generic terms: "Football", "Media Rights", "Streaming", "Sport" — only
      named entities (specific leagues, organisations, brands, geographic markets)
    - Geographic markets: use standard names (UK→United Kingdom, MENA→Middle East & North Africa)
    - Be conservative: 3–6 entities per document is normal; 10+ is almost always wrong
    - Correct types: governing bodies (UEFA, FIFA, EHF) are federations; Amazon/DAZN/Sky
      are broadcasters; UK/Germany/France are markets
    - Return [] if no named entities are substantively present
    Respond with only the JSON array, no markdown fences.
""").strip()


def resolve_entities(metadata: dict) -> list[dict]:
    """
    Given classification metadata dict, return resolved entity list.
    Each item: {"canonical": str, "type": str, "is_new": bool}
    """
    text = " | ".join(filter(None, [
        metadata.get("sports_leagues", ""),
        metadata.get("org_tags", ""),
        metadata.get("market_tags", ""),
        metadata.get("topic_tags", ""),
    ]))
    if not text.strip():
        return []
    raw = call_claude(_ENTITY_SYSTEM, f"Metadata: {text}", model=CLASSIFY_MODEL, max_tokens=600)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:]).rsplit("```", 1)[0].strip()
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


async def resolve_entities_async(metadata: dict) -> list[dict]:
    text = " | ".join(filter(None, [
        metadata.get("sports_leagues", ""),
        metadata.get("org_tags", ""),
        metadata.get("market_tags", ""),
        metadata.get("topic_tags", ""),
    ]))
    if not text.strip():
        return []
    raw = await call_claude_async(_ENTITY_SYSTEM, f"Metadata: {text}", model=CLASSIFY_MODEL, max_tokens=600)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:]).rsplit("```", 1)[0].strip()
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


# ── Entity overview (AI "what we know" summary) ───────────────────────────────

_OVERVIEW_SYSTEM = textwrap.dedent("""
    You are a sports-media-rights research analyst writing an intelligence profile briefing.
    Based on the provided sources (documents, notes, and deal records), write a briefing
    about the named entity in 4–5 short paragraphs (~350 words):

    Paragraph 1 — Identity & significance: What is this entity? What is its role in the
      sports media landscape? Why does it matter to rights buyers and sellers?

    Paragraph 2 — Rights landscape: Who holds or sells its rights? Which key broadcasters
      and platforms are involved? Which markets and territories are covered?

    Paragraph 3 — Key deals & values: Specific deal terms, values, durations, or structures
      mentioned in the sources. Cite approximate figures where stated.

    Paragraph 4 — Recent developments: Latest news, changes in strategy, rights renewals,
      new entrants, market shifts, or notable trends from the sources.

    Paragraph 5 (include only if warranted) — Gaps & caveats: Significant gaps in the
      intelligence, conflicting signals between sources, or areas where coverage is thin.

    Write in the third person, confident analyst voice. Do not invent information — use only
    what is in the provided sources. For entities with limited intelligence, cover what you
    can and note what is missing rather than padding with generalities.
""").strip()


def generate_entity_overview(entity_name: str, context: str) -> str:
    """Generate an AI intelligence profile summary for an entity hub page."""
    return call_claude(
        _OVERVIEW_SYSTEM,
        f"Entity: {entity_name}\n\nSources:\n\n{context}",
        model=ANSWER_MODEL,
        max_tokens=1000,
    )

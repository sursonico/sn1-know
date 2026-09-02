"""Add Documents & Log Snippet page — /add_log"""
import asyncio
from datetime import date
from pathlib import Path
from typing import Optional
import streamlit as st
from kb.ui import page_setup, section_title, reliability_badge_html, ENTITY_TYPE_META
from kb.db import (
    add_snippet, index_entry, get_entries_needing_enrichment,
    update_enrichment, init_db,
    find_or_create_entity, link_entry_to_entity,
    get_recent_entries_for_entities, mark_entry_superseded,
    add_deal, update_deal, delete_deal, get_deals_for_entity, find_entity_by_name_or_alias,
    get_entities_for_entry, get_chunks_for_entries, get_all_entries,
    get_all_entities, update_chunk_text, set_validation_warning,
    NON_PROPERTY_ENTITY_TYPES,
)
from kb.llm import (
    enrich_snippet, enrich_url_article, enrich_document,
    resolve_entities, check_new_entry_conflicts, extract_deals,
)
from kb.files import resolve_source_file
from kb.ingest import (
    ingest_all_async, extract as extract_file, full_text as _ft,
    _compute_validation_warning, _render_pdf_page_image, _extract_pptx_slide_images,
)
from kb.web import fetch_article, error_message
from config import DOCS_DIR

page_setup("add_log", title="Add & Log — SN1 Knowledge Base")

tab_add, tab_log = st.tabs(["Add Documents", "Log a Snippet"])

_ETYPE_COLORS = {
    "competition": "#AA925C", "federation": "#2B383E", "broadcaster": "#2A7F7F",
    "market": "#5B7B8A", "rights_holder": "#7B5B2A", "club": "#4A7B4A", "other": "#8A9598",
}


@st.cache_data(show_spinner=False)
def _page_thumbnail(path_str: str, mtime: float, chunk_type: str, page_idx: int):
    """
    Best-effort page/slide image for visual comparison against extracted text.
    Returns PNG/JPEG bytes, or None when no image is available for this format/page.
    PDF pages render fully (pymupdf/pdfplumber). PPTX has no full-slide rendering
    available via python-pptx — only embedded picture blobs are shown, so a
    text-only slide (e.g. a grouped-shapes layout with no images) has none.
    XLSX sheets have no visual form at all.
    """
    path = Path(path_str)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _render_pdf_page_image(path, page_idx)
    if ext == ".pptx":
        blobs, _ = _extract_pptx_slide_images(path, page_idx)
        return blobs[0] if blobs else None
    return None

_DEAL_CURRENCIES = ["", "GBP", "EUR", "USD", "AUD", "CAD", "Other"]
_DEAL_STATUSES = ["current", "unverified"]
_DEAL_RELIABILITY = ["confirmed", "reported", "rumoured"]


def _render_deal_row_form(deal: Optional[dict], entity_id: int, entry: dict, key: str) -> None:
    """Inline add/edit form for one deal row. `deal` is None for a fresh add."""
    editing = deal is not None
    d = deal or {}
    with st.form(f"{key}_form_{d.get('id','new')}"):
        fa1, fa2 = st.columns(2)
        d_territory   = fa1.text_input("Territory *",   value=d.get("territory", ""))
        d_broadcaster = fa2.text_input("Broadcaster *", value=d.get("broadcaster", ""))
        fb1, fb2 = st.columns(2)
        d_rights_holder = fb1.text_input("Rights holder", value=d.get("rights_holder", ""))
        d_platform      = fb2.text_input("Platform",      value=d.get("platform", ""))
        fc1, fc2, fc3 = st.columns(3)
        d_value = fc1.number_input("Value (millions)", min_value=0.0, value=float(d.get("value") or 0.0), step=0.1, format="%.2f")
        cur_default = d.get("currency", "")
        d_currency = fc2.selectbox("Currency", _DEAL_CURRENCIES, index=_DEAL_CURRENCIES.index(cur_default) if cur_default in _DEAL_CURRENCIES else 0)
        d_value_note = fc3.text_input("Qualifier", value=d.get("value_note", ""))
        fd1, fd2 = st.columns(2)
        d_period_start = fd1.text_input("Period start", value=d.get("period_start", ""))
        d_period_end   = fd2.text_input("Period end",   value=d.get("period_end", ""))
        st_default  = d.get("status", "current")
        rel_default = d.get("reliability", "reported")
        fe1, fe2 = st.columns(2)
        d_status      = fe1.selectbox("Status",      _DEAL_STATUSES,    index=_DEAL_STATUSES.index(st_default) if st_default in _DEAL_STATUSES else 0)
        d_reliability = fe2.selectbox("Reliability", _DEAL_RELIABILITY, index=_DEAL_RELIABILITY.index(rel_default) if rel_default in _DEAL_RELIABILITY else 1)
        c1, c2 = st.columns(2)
        submitted = c1.form_submit_button("Update" if editing else "Save", type="primary")
        cancelled = c2.form_submit_button("Cancel")

    if cancelled:
        st.session_state.pop(f"{key}_editing", None)
        st.session_state.pop(f"{key}_adding", None)
        st.rerun()

    if submitted:
        if not d_territory.strip() or not d_broadcaster.strip():
            st.error("Territory and Broadcaster are required.")
            return
        fields = dict(
            territory=d_territory.strip(), broadcaster=d_broadcaster.strip(),
            rights_holder=d_rights_holder.strip(), platform=d_platform.strip(),
            value=(d_value if d_value > 0 else None),
            currency=(d_currency if d_currency != "Other" else ""),
            value_note=d_value_note.strip(),
            period_start=d_period_start.strip(), period_end=d_period_end.strip(),
            status=d_status, reliability=d_reliability,
        )
        if editing:
            update_deal(d["id"], **fields)
        else:
            add_deal(
                entity_id=entity_id, source_entry_id=entry["id"],
                source_note=entry.get("source", ""), **fields,
            )
        st.session_state.pop(f"{key}_editing", None)
        st.session_state.pop(f"{key}_adding", None)
        st.rerun()


def _render_deal_editor(entity_row: dict, entry: dict, cid: int) -> None:
    """
    Structured deal display/correction for one entity, scoped to one page —
    this is the write that actually populates the entity hub's rights table
    (kb.db.get_deals_for_entity / the `deals` table); entity tagging alone
    (link_entry_to_entity) never touches it. Rows here are populated
    automatically by _save_page_corrections() extracting this page's deal
    terms when the entity tag is saved — this control is for reviewing what
    was written (inline edit/delete per row), not for typing deals in from
    scratch. "Add a deal manually" stays, but as the fallback for gaps
    extraction missed, not the primary path.
    """
    entity_id = entity_row["id"]
    entity_name = entity_row["canonical_name"]
    key = f"deal_{cid}_{entity_id}"

    st.markdown(f"**Deal terms — {entity_name}**")
    existing = get_deals_for_entity(entity_id, include_superseded=True)
    editing_id = st.session_state.get(f"{key}_editing")

    if not existing:
        st.caption(
            "_No deal rows for this entity yet. Extraction runs automatically "
            "against this page's text when you click Save corrections below — "
            "if it still comes up empty, use \"Add a deal manually\"._"
        )
    for d in existing:
        if editing_id == d["id"]:
            _render_deal_row_form(d, entity_id, entry, key)
            continue
        bits = [d.get("territory") or "?", d.get("broadcaster") or "?"]
        if d.get("value"):
            bits.append(f"{d.get('currency','')} {d['value']}m{(' ' + d['value_note']) if d.get('value_note') else ''}")
        period = "–".join(p for p in [d.get("period_start"), d.get("period_end")] if p)
        if period:
            bits.append(period)
        status_tag = f"  ·  [{d['status']}]" if d.get("status") != "current" else ""
        flag = f"  ·  ⚠ {d['flagged_for_review']}" if d.get("flagged_for_review") else ""

        row_c, edit_c, del_c = st.columns([7, 1, 1])
        row_c.caption(f"#{d['id']} · " + "  ·  ".join(str(b) for b in bits if b) + status_tag + flag)
        if edit_c.button("✏", key=f"{key}_editbtn_{d['id']}", help="Edit this deal"):
            st.session_state[f"{key}_editing"] = d["id"]
            st.rerun()
        if del_c.button("🗑", key=f"{key}_delbtn_{d['id']}", help="Delete (soft — recoverable in the DB)"):
            delete_deal(d["id"])
            st.rerun()

    if st.session_state.get(f"{key}_adding"):
        _render_deal_row_form(None, entity_id, entry, key)
    elif st.button("➕ Add a deal manually", key=f"{key}_addbtn"):
        st.session_state[f"{key}_adding"] = True
        st.rerun()


def _render_page_editor(entry: dict, chunk: dict, src_path) -> None:
    """
    One page/slide's manual-review block: extracted text next to a rendered
    thumbnail (when available), an editable text box pre-filled with the
    extracted text, and an entity tagger. Nothing is saved here — values live
    in widget state (keyed by chunk id) until "Save corrections" is clicked.
    """
    cid   = chunk["id"]
    text  = chunk.get("text") or ""
    label = f"{chunk.get('chunk_type', 'page').capitalize()} {chunk['chunk_num']}"
    is_empty = not text.strip()
    header = f"{'⚠ ' if is_empty else ''}{label}" + (
        "  ·  (empty — nothing extracted)" if is_empty else f"  ·  {len(text)} chars"
    )

    with st.expander(header, expanded=False):
        col_text, col_img = st.columns([3, 2])
        with col_text:
            st.caption("Extracted text (as currently stored)")
            st.text(text if text.strip() else "_(nothing extracted for this page)_")
        with col_img:
            st.caption("Page image")
            thumb = None
            if src_path is not None:
                try:
                    thumb = _page_thumbnail(str(src_path), src_path.stat().st_mtime, chunk.get("chunk_type", ""), chunk["chunk_num"] - 1)
                except Exception:
                    thumb = None
            if thumb:
                st.image(thumb, use_container_width=True)
            else:
                st.caption(
                    "_No rendered image available — PDF pages render fully, but "
                    "PowerPoint slides can only show an embedded picture (python-pptx "
                    "can't rasterize a full slide layout), and Excel sheets have no "
                    "visual form._"
                )

        st.text_area(
            "Correct this page's text",
            value=text,
            height=140,
            key=f"pgtxt_{cid}",
            help="Pre-filled with whatever was extracted, even if empty or wrong — "
                 "edit or replace it entirely, then use Save corrections below.",
        )

        all_names = [e["canonical_name"] for e in get_all_entities(include_proposed=True)]
        st.multiselect(
            "This page is about (entities)",
            options=all_names,
            key=f"pgent_{cid}",
            accept_new_options=True,
            placeholder="Pick existing entities or type a new name and press enter",
            help="New names become proposed entities (type 'other') — finish "
                 "classifying them in Admin → Proposed (Pending Review). Tagging "
                 "alone does not touch the rights table below — that's a separate "
                 "write, see 'Deal terms' for each tagged entity.",
        )

        for name in st.session_state.get(f"pgent_{cid}", []) or []:
            entity_row = find_entity_by_name_or_alias(name.strip())
            if entity_row:
                _render_deal_editor(entity_row, entry, cid)
            else:
                st.caption(
                    f"_'{name}' will be created as a new entity when you click Save "
                    f"corrections — reopen this page afterward to add deal terms for it._"
                )


def _save_page_corrections(entry: dict, chunks: list[dict]) -> tuple[int, int, int]:
    """
    Apply every pgtxt_/pgent_ widget value for this entry's chunks: update
    changed chunk text, link any newly-tagged entities, auto-extract and write
    deal terms for each page's tagged entities, re-index, and recompute the
    validation banner from the corrected data. Returns
    (n_text_edits, n_entities_added, n_deals_written).

    Deal extraction runs against every page that has a tagged entity, every
    time this is clicked — not just newly-tagged ones — because add_deal()'s
    dedup-and-fill logic makes a repeat run harmless (it updates gaps on the
    existing row rather than duplicating), and confirming an entity tag is
    exactly the "entity resolved" moment that should write deal terms
    immediately rather than leaving a blank form as the only path.
    """
    eid = entry["id"]
    n_text_edits = 0
    tagged_names: set[str] = set()

    for chunk in chunks:
        cid = chunk["id"]
        new_text = st.session_state.get(f"pgtxt_{cid}", chunk.get("text") or "")
        if new_text != (chunk.get("text") or ""):
            update_chunk_text(cid, new_text)
            n_text_edits += 1
        tagged_names.update(st.session_state.get(f"pgent_{cid}", []) or [])

    n_entities_added = 0
    for name in tagged_names:
        name = name.strip()
        if not name:
            continue
        existing = find_entity_by_name_or_alias(name)
        if existing:
            entity_id = existing["id"]
        else:
            entity_id = find_or_create_entity(name, "other", proposed=True)
        link_entry_to_entity(eid, entity_id, role="primary")
        n_entities_added += 1

    entry_rel = entry.get("reliability", "reported") or "reported"
    n_deals_written = 0
    chunk_deal_counts: list[tuple[int, str, int]] = []
    for chunk in chunks:
        cid = chunk["id"]
        page_names = [n.strip() for n in (st.session_state.get(f"pgent_{cid}", []) or []) if n.strip()]
        if not page_names:
            continue
        page_text = st.session_state.get(f"pgtxt_{cid}", chunk.get("text") or "")
        if not page_text.strip():
            continue
        try:
            raw_deals = extract_deals(
                page_text, page_names,
                source_hint=f"{entry.get('source','')} — {chunk.get('chunk_type','page')} {chunk.get('chunk_num','')}",
            )
        except Exception:
            raw_deals = []
        chunk_deal_counts.append((chunk.get("chunk_num"), page_text, len(raw_deals)))
        for d in raw_deals:
            en = (d.get("entity_name") or "").strip()
            if en not in page_names:
                continue
            entity_row = find_entity_by_name_or_alias(en, exclude_types=NON_PROPERTY_ENTITY_TYPES)
            if not entity_row:
                continue
            confidence  = d.get("confidence", "medium")
            deal_status = "unverified" if confidence == "low" else "current"
            add_deal(
                entity_id=entity_row["id"], territory=d.get("territory", ""),
                broadcaster=d.get("broadcaster", ""), rights_holder=d.get("rights_holder", ""),
                value=d.get("value"), currency=d.get("currency", ""),
                value_note=d.get("value_note", ""), period_start=d.get("period_start", ""),
                period_end=d.get("period_end", ""), platform=d.get("platform", ""),
                source_entry_id=eid, source_note=entry.get("source", ""),
                status=deal_status, reliability=entry_rel,
            )
            n_deals_written += 1

    index_entry(eid)

    fresh_chunks = get_chunks_for_entries([eid]).get(eid, [])
    fresh_entities = get_entities_for_entry(eid)
    file_size = 0
    src_path = resolve_source_file(entry)
    if src_path is not None:
        try:
            file_size = src_path.stat().st_size
        except OSError:
            file_size = 0
    warning = _compute_validation_warning(
        [c.get("text") or "" for c in fresh_chunks], file_size, len(fresh_entities), chunk_deal_counts,
    )
    set_validation_warning(eid, warning)

    return n_text_edits, n_entities_added, n_deals_written


def _render_review_card(entry: dict) -> None:
    fname     = entry.get("source", "(untitled)")
    eid       = entry["id"]
    summary   = entry.get("summary") or ""
    chunks_by = get_chunks_for_entries([eid])
    chunks    = chunks_by.get(eid, [])
    n_pages   = len(chunks)
    entities  = get_entities_for_entry(eid)
    rel       = entry.get("reliability", "reported") or "reported"

    thin = []
    if not summary.strip():       thin.append("no summary extracted")
    elif len(summary) < 100:      thin.append("summary is short")
    if not entities:              thin.append("no entities linked")
    if entry.get("ingest_error"): thin.append(entry["ingest_error"][:60])
    if entry.get("validation_warning"): thin.append(entry["validation_warning"])

    # ── Card header ───────────────────────────────────────────────────
    thin_badge = (
        '&nbsp;<span style="font-size:0.7rem;background:#fff3cd;color:#856404;'
        'padding:2px 7px;border-radius:10px;font-weight:600">⚠ THIN</span>'
    ) if thin else ""
    st.markdown(
        f'<div style="margin-top:0.9rem">'
        f'<span style="font-weight:600;font-size:0.95rem;color:#2B383E">📄 {fname}</span>'
        f'&nbsp;&nbsp;<span style="font-size:0.7rem;background:#d4edda;color:#155724;'
        f'padding:2px 7px;border-radius:10px;font-weight:600">✓ INGESTED</span>'
        f'{thin_badge}</div>',
        unsafe_allow_html=True,
    )

    # Metrics
    m_parts = []
    if n_pages:               m_parts.append(f"{n_pages} page{'s' if n_pages!=1 else ''}")
    if entry.get("doc_type"): m_parts.append(entry["doc_type"])
    m_parts.append(f"reliability: {rel}")
    st.caption(" · ".join(m_parts))

    # Summary preview
    if summary:
        st.markdown(
            f'<div style="font-size:0.86rem;color:#444;margin:0.15rem 0 0.25rem">'
            f'{summary[:300]}{"…" if len(summary)>300 else ""}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("_(no summary extracted)_")

    # Tags
    t_parts = []
    if entry.get("org_tags"):    t_parts.append(f"**Org:** {entry['org_tags']}")
    if entry.get("market_tags"): t_parts.append(f"**Markets:** {entry['market_tags']}")
    if entry.get("sport_tags"):  t_parts.append(f"**Sport:** {entry['sport_tags']}")
    if entry.get("topic_tags"):  t_parts.append(f"**Topics:** {entry['topic_tags']}")
    if t_parts:
        st.markdown("  ·  ".join(t_parts))
    else:
        st.caption("_(no tags extracted)_")

    # Entities
    if entities:
        badges = []
        for en in entities:
            c = _ETYPE_COLORS.get(en.get("entity_type", "other"), "#8A9598")
            badges.append(
                f'<span style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                f'color:{c}">{en["entity_type"]}</span>'
                f'&nbsp;<span style="font-size:0.82rem">{en["canonical_name"]}</span>'
            )
        st.markdown(
            '<div style="margin:0.2rem 0">' + "&ensp;·&ensp;".join(badges) + "</div>",
            unsafe_allow_html=True,
        )

    # Thin warning
    if thin:
        st.warning("⚠ Low-confidence extraction  ·  " + "  ·  ".join(thin), icon=None)

    # ── Manual per-page review ───────────────────────────────────────────
    if chunks:
        st.markdown("**Pages** — review extracted text and images, correct or tag each one:")
        src_path = resolve_source_file(entry)
        for chunk in chunks:
            _render_page_editor(entry, chunk, src_path)

        if st.button("💾 Save corrections", type="primary", key=f"save_corrections_{eid}"):
            with st.spinner("Saving corrections and extracting deal terms for tagged pages…"):
                n_edits, n_added, n_deals = _save_page_corrections(entry, chunks)
            st.session_state[f"corrections_saved_{eid}"] = (n_edits, n_added, n_deals)
            st.rerun()

        saved = st.session_state.pop(f"corrections_saved_{eid}", None)
        if saved:
            n_edits, n_added, n_deals = saved
            st.success(
                f"Saved — {n_edits} page{'s' if n_edits != 1 else ''} text-corrected, "
                f"{n_added} entity link{'s' if n_added != 1 else ''} applied, "
                f"{n_deals} deal row{'s' if n_deals != 1 else ''} written/updated. Re-indexed "
                f"for Ask, and the validation check has re-run (see the badge above)."
            )

    # Inline edit
    with st.expander("✏  Edit tags & summary"):
        fe_sum = st.text_area("Summary", value=summary, height=80, key=f"fe_sum_{eid}")
        fd1, fd2 = st.columns(2)
        fe_date = fd1.text_input(
            "Content date",
            value=entry.get("entry_date", ""),
            key=f"fe_date_{eid}",
            placeholder="e.g. 2024, 2024-03",
            help="When this document was written or published.",
        )
        fe_cov = fd2.text_input(
            "Rights period covered",
            value=entry.get("coverage_period", ""),
            key=f"fe_cov_{eid}",
            placeholder="e.g. 2025-2028",
            help="The rights cycle this document describes, if different from the content date.",
        )
        c1, c2 = st.columns(2)
        fe_org = c1.text_input("Organisations", value=entry.get("org_tags", ""), key=f"fe_org_{eid}")
        fe_mkt = c2.text_input("Markets",       value=entry.get("market_tags", ""), key=f"fe_mkt_{eid}")
        c3, c4 = st.columns(2)
        fe_spt = c3.text_input("Sport / League", value=entry.get("sport_tags", ""), key=f"fe_spt_{eid}")
        fe_top = c4.text_input("Topics",         value=entry.get("topic_tags", ""), key=f"fe_top_{eid}")
        rels    = ["confirmed", "reported", "rumoured"]
        cur_rel = rel if rel in rels else "reported"
        fe_rel  = st.selectbox("Reliability", rels, index=rels.index(cur_rel), key=f"fe_rel_{eid}")
        if st.button("Save changes", type="secondary", key=f"fe_save_{eid}"):
            update_enrichment(eid, summary=fe_sum, entry_date=fe_date, coverage_period=fe_cov,
                              org_tags=fe_org, market_tags=fe_mkt,
                              sport_tags=fe_spt, topic_tags=fe_top, reliability=fe_rel)
            index_entry(eid)
            st.session_state[f"fe_saved_{eid}"] = True

    if st.session_state.pop(f"fe_saved_{eid}", False):
        st.success(f"Updated **{fname}**")

    st.divider()


# ── Add Documents ─────────────────────────────────────────────────────────────
with tab_add:
    section_title("Add documents")

    # ── Post-ingest review ─────────────────────────────────────────────────────
    if "doc_review" in st.session_state:
        dr = st.session_state["doc_review"]
        n_ok   = sum(1 for r in dr["statuses"] if r.startswith("OK"))
        n_skip = sum(1 for r in dr["statuses"] if r.startswith("SKIP"))
        n_err  = len(dr["statuses"]) - n_ok - n_skip
        sum_parts = []
        if n_ok:   sum_parts.append(f"**{n_ok} ingested**")
        if n_skip: sum_parts.append(f"{n_skip} skipped (unchanged)")
        if n_err:  sum_parts.append(f"**{n_err} failed**")
        st.caption(
            ", ".join(sum_parts)
            + ". Review extracted intelligence below — edit anything that needs correcting, then click Done."
        )

        # Keyed on filename: uploads land in DOCS_DIR under their own name, and
        # `source` is that filename (file_path is stored relative to DOCS_DIR).
        all_e = {
            e["source"]: e for e in get_all_entries()
            if e.get("entry_type") == "document"
        }

        for path_str, result in zip(dr["paths"], dr["statuses"]):
            fname = Path(path_str).name

            if result.startswith("SKIP"):
                st.info(f"**⏭  {fname}** — unchanged, skipped", icon=None)
                continue
            if not result.startswith("OK"):
                st.error(f"**✗  {fname}** — {result}")
                continue

            entry = all_e.get(fname)
            if not entry:
                st.warning(f"📄 **{fname}** — processed but entry not found in database")
                continue

            _render_review_card(entry)

        if st.button("Done reviewing", type="primary", key="doc_review_done"):
            st.session_state.pop("doc_review", None)
            st.rerun()

    else:
        # ── Normal upload form ─────────────────────────────────────────────────
        st.caption("Files are SHA-256 hashed; unchanged files are skipped automatically.")

        uploaded = st.file_uploader(
            "Drag files here", type=["pdf","pptx","xlsx","xls"],
            accept_multiple_files=True, label_visibility="collapsed",
        )
        if uploaded:
            st.write(f"**{len(uploaded)} file(s) ready:**")
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
            for uf in uploaded:
                exists = (DOCS_DIR / uf.name).exists()
                st.write(f"- {uf.name}{'  _(already exists)_' if exists else ''}")

        if st.button(f"Process {len(uploaded)} file(s)" if uploaded else "Process files",
                     type="primary", disabled=not uploaded):
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
            saved = []
            for uf in uploaded:
                dest = DOCS_DIR / uf.name
                dest.write_bytes(uf.getvalue())
                saved.append(dest)
            with st.spinner("Classifying with Claude (parallel batches)…"):
                results = asyncio.run(ingest_all_async(saved))
            st.session_state["doc_review"] = {
                "paths":    [str(p) for p in saved],
                "statuses": results,
            }
            st.rerun()

        # ── Review or correct an existing document ─────────────────────────────────
        st.divider()
        with st.expander("🔍 Review or correct an existing document"):
            st.caption(
                "Open any previously-ingested document in the same per-page review tool "
                "shown right after upload — flagged documents (⚠) are listed first."
            )
            doc_entries = [e for e in get_all_entries() if e.get("entry_type") == "document"]
            doc_entries.sort(key=lambda e: (not e.get("validation_warning"), e.get("source", "")))
            if not doc_entries:
                st.info("No documents in the library yet.")
            else:
                options = {
                    f"{'⚠ ' if e.get('validation_warning') else ''}{e['source']}": e["id"]
                    for e in doc_entries
                }
                choice = st.selectbox(
                    "Document", list(options.keys()),
                    key="existing_doc_picker", label_visibility="collapsed",
                )
                chosen_id = options[choice]
                chosen_entry = next(e for e in doc_entries if e["id"] == chosen_id)
                _render_review_card(chosen_entry)

        # ── Backfill ──────────────────────────────────────────────────────────────
        st.divider()
        with st.expander("Backfill missing summaries & topics"):
            st.caption("Enriches document entries that are missing summary or topic tags.")
            needs = get_entries_needing_enrichment()
            if not needs:
                st.info("All documents already have summaries and topics.")
            else:
                st.write(f"**{len(needs)} document(s) need enriching:**")
                for r in needs: st.write(f"- {r['source']}")
                if st.button("Backfill now", type="secondary"):
                    prog = st.progress(0); n = len(needs)
                    for i, entry in enumerate(needs):
                        prog.progress(i/n, text=f"Enriching {entry['source']}…")
                        with st.status(f"**{entry['source']}**", expanded=False) as s:
                            try:
                                path = resolve_source_file(entry)
                                if path is None:
                                    raise FileNotFoundError(
                                        f"{entry['source']} not found under {DOCS_DIR}"
                                    )
                                result = extract_file(path)
                                meta   = enrich_document(entry["source"], _ft(result))
                                update_enrichment(entry["id"], summary=meta.get("summary",""), topic_tags=meta.get("topics",""))
                                index_entry(entry["id"])
                                s.update(label=f"**{entry['source']}** ✓", state="complete")
                            except Exception as exc:
                                s.update(label=f"**{entry['source']}** ✗", state="error")
                                st.error(str(exc))
                    prog.progress(1.0, text="Done.")
                    st.success("Backfill complete.")
                    st.rerun()

# ── Log a Snippet ─────────────────────────────────────────────────────────────
with tab_log:
    section_title("Log a snippet")
    st.caption("Paste text or fetch a URL → Claude extracts entities, topics, and a summary → review and edit before saving.")

    # ─────────────────────────────────────────────────────────────────────────
    # Helper: resolve entities, find existing entries, run conflict check.
    # Returns (entity_id_list, existing_entries, conflict_result).
    # ─────────────────────────────────────────────────────────────────────────
    def _run_conflict_check(summary: str, detail: str, org: str, market: str, sport: str, topic: str, source_hint: str = "", raw_text: str = ""):
        meta = {"sports_leagues": sport, "org_tags": org, "market_tags": market, "topic_tags": topic, "summary": summary}
        resolved = resolve_entities(meta, source=source_hint)
        entity_ids = []
        for r in resolved:
            if r.get("canonical"):
                xid = find_or_create_entity(r["canonical"], r.get("type","other"), proposed=r.get("is_new",False))
                entity_ids.append(xid)
        existing = get_recent_entries_for_entities(entity_ids, limit=10) if entity_ids else []
        conflict = check_new_entry_conflicts(summary, detail, existing) if existing else {"has_conflict": False, "conflicts": [], "summary": ""}
        # Extract structured deals — use raw_text (full note body) when available,
        # otherwise fall back to summary+detail (URL path already provides full detail)
        entity_names = [r.get("canonical","") for r in resolved if r.get("canonical","")]
        deal_text = raw_text or (f"{summary}\n\n{detail}".strip() if detail else summary)
        extracted_deals = extract_deals(deal_text, entity_names, source_hint=source_hint) if entity_names else []
        return resolved, entity_ids, existing, conflict, extracted_deals

    # ─────────────────────────────────────────────────────────────────────────
    # Helper: execute the actual DB save after user confirms.
    # ─────────────────────────────────────────────────────────────────────────
    def _save_pending(pending: dict, supersede_ids: list[int]) -> int:
        reliability = pending.get("reliability", "reported")
        eid = add_snippet(
            source          = pending["source"],
            entry_date      = pending["entry_date"],
            full_text       = pending["full_text"],
            summary         = pending["summary"],
            coverage_period = pending.get("coverage_period", ""),
            org_tags        = pending["org_tags"],
            market_tags     = pending["market_tags"],
            sport_tags      = pending["sport_tags"],
            topic_tags      = pending["topic_tags"],
            reliability     = reliability,
        )
        index_entry(eid)
        for r in pending["resolved_entities"]:
            if r.get("canonical"):
                xid = find_or_create_entity(r["canonical"], r.get("type","other"), proposed=r.get("is_new",False))
                link_entry_to_entity(eid, xid)
        for old_id in supersede_ids:
            mark_entry_superseded(old_id, eid)
        # Save extracted deals — inherit entry reliability
        for d in pending.get("extracted_deals", []):
            en = (d.get("entity_name") or "").strip()
            entity_row = find_entity_by_name_or_alias(en, exclude_types=NON_PROPERTY_ENTITY_TYPES)
            if entity_row:
                confidence = d.get("confidence", "medium")
                add_deal(
                    entity_id       = entity_row["id"],
                    territory       = d.get("territory", ""),
                    broadcaster     = d.get("broadcaster", ""),
                    rights_holder   = d.get("rights_holder", ""),
                    value           = d.get("value"),
                    currency        = d.get("currency", ""),
                    value_note      = d.get("value_note", ""),
                    period_start    = d.get("period_start", ""),
                    period_end      = d.get("period_end", ""),
                    platform        = d.get("platform", ""),
                    source_entry_id = eid,
                    source_note     = pending["source"],
                    status          = "unverified" if confidence == "low" else "current",
                    reliability     = reliability,
                )
        return eid

    # ═══════════════════════════════════════════════════════════════════════════
    # PENDING CONFIRMATION STATE — shown after enrichment + conflict check
    # ═══════════════════════════════════════════════════════════════════════════
    if "pending_snippet" in st.session_state:
        p = st.session_state["pending_snippet"]
        conflict = p.get("conflict_result", {})

        st.info("**Review** the extracted intelligence below, edit anything that's wrong, then save.", icon="📋")

        # ── Input / article text preview ───────────────────────────────────────
        raw_text = p.get("article_raw_text") or p.get("full_text") or ""
        if raw_text:
            is_article = bool(p.get("article_raw_text"))
            label      = "Article text" if is_article else "Input text"
            words      = len(raw_text.split())
            with st.expander(f"{label}  ·  {words:,} words", expanded=False):
                preview = raw_text[:2000]
                st.text(preview + ("…" if len(raw_text) > 2000 else ""))

        # ── Summary ────────────────────────────────────────────────────────────
        edit_summary = st.text_area(
            "Summary",
            value=p.get("summary", ""),
            height=90,
            key="edit_summary",
        )

        # ── Dates ──────────────────────────────────────────────────────────────
        date_c1, date_c2 = st.columns(2)
        date_c1.text_input(
            "Content date",
            value=p.get("entry_date", ""),
            key="edit_entry_date",
            disabled=True,
            help="Date of the source content — set at input stage.",
        )
        edit_coverage = date_c2.text_input(
            "Rights period covered",
            value=p.get("coverage_period", ""),
            key="edit_coverage",
            placeholder="e.g. 2025-2028",
            help="The rights cycle or time span this content describes, if different from the content date.",
        )

        # ── Detail (URL mode — read-only) ──────────────────────────────────────
        if p.get("detail"):
            with st.expander("Full extracted detail", expanded=False):
                st.markdown(p["detail"])

        # ── Tags ───────────────────────────────────────────────────────────────
        tc1, tc2 = st.columns(2)
        tc3, tc4 = st.columns(2)
        edit_org    = tc1.text_input("Organisations",  value=p.get("org_tags", ""),    key="edit_org")
        edit_market = tc2.text_input("Markets",        value=p.get("market_tags", ""), key="edit_mkt")
        edit_sport  = tc3.text_input("Sport / League", value=p.get("sport_tags", ""),  key="edit_spt")
        edit_topic  = tc4.text_input("Topics",         value=p.get("topic_tags", ""),  key="edit_top")

        # ── Entities ───────────────────────────────────────────────────────────
        resolved = p.get("resolved_entities", [])
        entity_keep_flags: list[tuple] = []
        if resolved:
            st.markdown("**Entities to link** — uncheck any that are incorrect")
            ecols = st.columns(min(len(resolved), 3))
            for i, r in enumerate(resolved):
                name   = r.get("canonical", "")
                etype  = r.get("type", r.get("entity_type", "other"))
                is_new = r.get("is_new", False)
                col    = ecols[i % len(ecols)]
                color  = _ETYPE_COLORS.get(etype, "#8A9598")
                col.markdown(
                    f'<span style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                    f'color:{color}">{etype}</span>',
                    unsafe_allow_html=True,
                )
                new_tag = "  _(new)_" if is_new else ""
                keep = col.checkbox(f"{name}{new_tag}", value=True, key=f"ent_{i}")
                entity_keep_flags.append((r, keep))

        # ── Reliability ────────────────────────────────────────────────────────
        st.divider()
        inferred_rel = p.get("reliability", "reported")
        rel_options  = ["confirmed", "reported", "rumoured"]
        rel_c1, rel_c2 = st.columns([2, 3])
        rel_c1.markdown("**Reliability**")
        rel_c1.markdown(reliability_badge_html(inferred_rel), unsafe_allow_html=True)
        rel_c1.caption("Claude inferred")
        override_rel = rel_c2.selectbox(
            "Override reliability",
            rel_options,
            index=rel_options.index(inferred_rel) if inferred_rel in rel_options else rel_options.index("reported"),
            key="pending_rel_override",
            label_visibility="collapsed",
        )

        # ── Deal terms ─────────────────────────────────────────────────────────
        extracted_deals = p.get("extracted_deals", [])
        if extracted_deals:
            n_deals = len(extracted_deals)
            with st.expander(
                f"**{n_deals} deal term{'s' if n_deals != 1 else ''} extracted** — will be saved to the deals table",
                expanded=True,
            ):
                for d in extracted_deals:
                    parts: list[str] = []
                    if d.get("entity_name"): parts.append(f"**{d['entity_name']}**")
                    if d.get("territory"):   parts.append(d["territory"])
                    if d.get("broadcaster"): parts.append(f"broadcaster: {d['broadcaster']}")
                    if d.get("rights_holder"): parts.append(f"rights: {d['rights_holder']}")
                    val_str = ""
                    if d.get("value"):
                        val_str = f"{d.get('currency', '')} {d['value']:,}".strip()
                        if d.get("value_note"): val_str += f" {d['value_note']}"
                    if val_str: parts.append(f"value: {val_str}")
                    period = "–".join(filter(None, [d.get("period_start",""), d.get("period_end","")]))
                    if period: parts.append(f"period: {period}")
                    if d.get("platform"): parts.append(f"platform: {d['platform']}")
                    conf = d.get("confidence", "")
                    if conf: parts.append(f"_{conf} confidence_")
                    st.markdown("• " + " · ".join(parts))

        # ── Conflict warning ────────────────────────────────────────────────────
        st.divider()
        supersede_ids: list[int] = []
        if conflict.get("has_conflict") and conflict.get("conflicts"):
            st.warning(
                f"**Potential conflict detected** — this entry may contradict existing intelligence:\n\n"
                f"{conflict.get('summary','')}",
                icon=None,
            )
            st.markdown(
                '<div style="background:#FFF8EE;border-left:3px solid #E07000;'
                'border-radius:6px;padding:0.75rem 1rem;margin:0.25rem 0 0.5rem">'
                '<strong style="color:#2B383E">Conflicting records</strong><br>'
                '<span style="font-size:0.82rem;color:#5A5A5A">'
                'Check the box next to any entry this new record supersedes. '
                'The old record will be kept as history — nothing is deleted.'
                '</span></div>',
                unsafe_allow_html=True,
            )
            for c in conflict["conflicts"]:
                old_id   = c.get("existing_entry_id")
                old_src  = c.get("existing_entry_source", "?")
                old_date = c.get("existing_entry_date", "?")
                desc     = c.get("description", "")
                label    = f"Mark entry #{old_id} as superseded — *{old_src}* ({old_date})"
                if st.checkbox(label, key=f"sup_{old_id}"):
                    if old_id not in supersede_ids:
                        supersede_ids.append(old_id)
                if desc:
                    st.caption(f"  Conflict: {desc}")
        else:
            st.success("No conflicts detected with existing intelligence.", icon=None)

        # ── Save / Cancel ───────────────────────────────────────────────────────
        col_save, col_cancel = st.columns([2, 1])
        if col_save.button("Save", type="primary", key="pending_confirm"):
            p["summary"]         = edit_summary
            p["coverage_period"] = edit_coverage
            p["org_tags"]        = edit_org
            p["market_tags"]     = edit_market
            p["sport_tags"]      = edit_sport
            p["topic_tags"]      = edit_topic
            p["reliability"]     = override_rel
            if entity_keep_flags:
                p["resolved_entities"] = [r for r, keep in entity_keep_flags if keep]
            eid = _save_pending(p, supersede_ids)
            st.session_state.pop("pending_snippet", None)
            st.session_state.pop("url_article", None)
            sup_msg = f" {len(supersede_ids)} older entry/entries marked as superseded." if supersede_ids else ""
            st.success(f"Snippet saved (entry #{eid}).{sup_msg}")
            st.rerun()
        if col_cancel.button("Cancel", type="secondary", key="pending_cancel"):
            st.session_state.pop("pending_snippet", None)
            st.rerun()

    else:
        # ── Mode selector ─────────────────────────────────────────────────────
        mode = st.radio(
            "Input source",
            ["Paste text", "From URL"],
            horizontal=True,
            label_visibility="collapsed",
            key="log_mode",
        )

        # ═══════════════════════════════════════════════════════════════════════
        # MODE A — Paste text
        # ═══════════════════════════════════════════════════════════════════════
        if mode == "Paste text":
            note = st.text_area("Note", height=200,
                                 placeholder="Paste or type the note, extract, or observation…",
                                 key="log_paste_text")
            ca, cb = st.columns(2)
            source  = ca.text_input("Source", placeholder="e.g. SportBusiness, internal meeting",
                                    key="log_paste_source")
            ndate   = cb.date_input("Date", value=date.today(), key="log_paste_date")

            with st.expander("Optional: pre-fill tags"):
                t1,t2 = st.columns(2); t3,t4 = st.columns(2)
                m_org    = t1.text_input("Organisations",     placeholder="e.g. UEFA, DAZN",    key="lp_org")
                m_market = t2.text_input("Markets / Regions", placeholder="e.g. UK, Germany",   key="lp_mkt")
                m_sport  = t3.text_input("Sport / League",    placeholder="e.g. Football, NFL", key="lp_spt")
                m_topic  = t4.text_input("Topics",            placeholder="e.g. streaming, OTT",key="lp_top")

            if st.button("Analyse", type="primary", key="log_paste_save") and note.strip():
                with st.spinner("Extracting key information…"):
                    enrichment = enrich_snippet(note.strip(), source.strip() or "Unknown")
                final_org    = m_org.strip()    or enrichment.get("org_tags","")
                final_market = m_market.strip() or enrichment.get("market_tags","")
                final_sport  = m_sport.strip()  or enrichment.get("sport_tags","")
                final_topic  = m_topic.strip()  or enrichment.get("topic_tags","")
                summary      = enrichment.get("summary","")
                reliability  = enrichment.get("reliability", "reported")
                with st.spinner("Checking for conflicts and extracting deals…"):
                    resolved, entity_ids, existing, conflict, extracted_deals = _run_conflict_check(
                        summary, "", final_org, final_market, final_sport, final_topic,
                        source_hint=source.strip() or "Unknown",
                        raw_text=note.strip(),
                    )
                st.session_state["pending_snippet"] = {
                    "source":            source.strip() or "Unknown",
                    "entry_date":        str(ndate),
                    "full_text":         note.strip(),
                    "summary":           summary,
                    "coverage_period":   enrichment.get("coverage_period", ""),
                    "detail":            "",
                    "org_tags":          final_org,
                    "market_tags":       final_market,
                    "sport_tags":        final_sport,
                    "topic_tags":        final_topic,
                    "reliability":       reliability,
                    "resolved_entities": resolved,
                    "entity_ids":        entity_ids,
                    "existing_entries":  existing,
                    "conflict_result":   conflict,
                    "extracted_deals":   extracted_deals,
                }
                st.rerun()

        # ═══════════════════════════════════════════════════════════════════════
        # MODE B — From URL
        # ═══════════════════════════════════════════════════════════════════════
        else:
            st.caption(
                "Paste a link — the app fetches the page, extracts the full article body "
                "(including tables and lists), then Claude pulls out the concrete deal "
                "intelligence. A short summary plus the structured detail are stored; "
                "the raw article text is not."
            )

            url_input = st.text_input(
                "Article URL",
                placeholder="https://www.sportbusiness.com/...",
                key="log_url_input",
            )

            fetch_col, _ = st.columns([2, 5])
            fetch_btn = fetch_col.button(
                "Fetch article",
                type="secondary",
                key="log_url_fetch",
            )

            # ── Fetch ──────────────────────────────────────────────────────────
            if fetch_btn and url_input.strip():
                st.session_state.pop("url_article", None)
                st.session_state.pop("url_fetch_error", None)

                with st.spinner("Fetching and extracting article content…"):
                    result = fetch_article(url_input.strip())

                if result["error"]:
                    st.session_state["url_fetch_error"] = (url_input.strip(), result["error"])
                else:
                    st.session_state["url_article"] = {
                        "url":        url_input.strip(),
                        "title":      result["title"],
                        "text":       result["text"],
                        "word_count": result["word_count"],
                        "char_count": result["char_count"],
                        "short":      result["short"],
                        "method":     result["method"],
                    }

            # ── Error state ────────────────────────────────────────────────────
            if "url_fetch_error" in st.session_state:
                failed_url, err_code = st.session_state["url_fetch_error"]
                headline, err_detail = error_message(err_code)
                st.error(f"**{headline}**\n\n{err_detail}")
                st.info(
                    "Switch to **Paste text** above to manually enter the article content.",
                    icon="📋",
                )

            # ── Success state — preview + save form ───────────────────────────
            elif "url_article" in st.session_state:
                art = st.session_state["url_article"]
                title_display = art["title"] or art["url"]
                method_label  = {"trafilatura": "trafilatura", "beautifulsoup": "fallback (BeautifulSoup)"}.get(art["method"], art["method"])

                stats_html = (
                    f'<span style="color:#2B383E;font-weight:600">{art["word_count"]:,} words</span>'
                    f'<span style="color:#8A9598"> · {art["char_count"]:,} chars · via {method_label}</span>'
                )
                if art["short"]:
                    st.warning(
                        f"⚠ Only **{art['word_count']} words** extracted — the page may be "
                        "paywalled, JS-rendered, or structured unusually. Check the preview "
                        "below before saving.",
                        icon=None,
                    )

                st.markdown(
                    f'<div style="background:#F3ECE0;border-left:3px solid #AA925C;'
                    f'border-radius:6px;padding:0.9rem 1.1rem;margin:0.25rem 0 0.75rem">'
                    f'<div style="font-family:Open Sans,sans-serif;font-weight:600;'
                    f'font-size:0.95rem;color:#2B383E;margin-bottom:0.3rem">{title_display}</div>'
                    f'<div style="font-size:0.78rem">{stats_html}</div>'
                    f'<div style="font-size:0.75rem;margin-top:0.2rem">'
                    f'<a href="{art["url"]}" target="_blank" style="color:#AA925C">'
                    f'{art["url"][:80]}{"…" if len(art["url"])>80 else ""}</a></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                with st.expander(
                    f"Preview extracted text ({art['word_count']:,} words) — check before saving",
                    expanded=art["short"],
                ):
                    st.text(art["text"])

                _, ub = st.columns([3, 1])
                url_date = ub.date_input("Date fetched", value=date.today(), key="log_url_date")

                with st.expander("Optional: pre-fill tags (Claude will also suggest these)"):
                    u1,u2 = st.columns(2); u3,u4 = st.columns(2)
                    u_org    = u1.text_input("Organisations",     placeholder="e.g. UEFA, DAZN",    key="lu_org")
                    u_market = u2.text_input("Markets / Regions", placeholder="e.g. UK, Germany",   key="lu_mkt")
                    u_sport  = u3.text_input("Sport / League",    placeholder="e.g. Football, NFL", key="lu_spt")
                    u_topic  = u4.text_input("Topics",            placeholder="e.g. streaming, OTT",key="lu_top")

                save_col2, clear_col = st.columns([3, 1])
                save_url_btn  = save_col2.button(
                    "Analyse", type="primary", key="log_url_save"
                )
                clear_url_btn = clear_col.button("Clear", type="secondary", key="log_url_clear")

                if clear_url_btn:
                    st.session_state.pop("url_article", None)
                    st.rerun()

                if save_url_btn:
                    with st.spinner("Extracting intelligence with Claude (Sonnet)…"):
                        enrichment = enrich_url_article(art["text"], art["url"])

                    final_org    = u_org.strip()    or enrichment.get("org_tags","")
                    final_market = u_market.strip() or enrichment.get("market_tags","")
                    final_sport  = u_sport.strip()  or enrichment.get("sport_tags","")
                    final_topic  = u_topic.strip()  or enrichment.get("topic_tags","")
                    summary      = enrichment.get("summary", "")
                    detail       = enrichment.get("detail", "")
                    reliability  = enrichment.get("reliability", "reported")
                    stored_body  = f"{summary}\n\n{detail}".strip() if detail else summary

                    with st.spinner("Checking for conflicts and extracting deals…"):
                        resolved, entity_ids, existing, conflict, extracted_deals = _run_conflict_check(
                            summary, detail, final_org, final_market, final_sport, final_topic,
                            source_hint=art["url"],
                        )
                    st.session_state["pending_snippet"] = {
                        "source":            art["url"],
                        "entry_date":        str(url_date),
                        "full_text":         stored_body,
                        "article_raw_text":  art["text"],
                        "summary":           summary,
                        "coverage_period":   enrichment.get("coverage_period", ""),
                        "detail":            detail,
                        "org_tags":          final_org,
                        "market_tags":       final_market,
                        "sport_tags":        final_sport,
                        "topic_tags":        final_topic,
                        "reliability":       reliability,
                        "resolved_entities": resolved,
                        "entity_ids":        entity_ids,
                        "existing_entries":  existing,
                        "conflict_result":   conflict,
                        "extracted_deals":   extracted_deals,
                    }
                    st.rerun()

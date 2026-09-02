"""Entity hub — full intelligence profile"""

import re
from datetime import date

import streamlit as st

from kb.ui import (
    page_setup, entity_type_badge, section_title,
    reliability_badge_html, open_file_button, remove_from_home_control,
    home_removal_pending,
)
from kb import db
from kb.llm import generate_entity_overview
from kb.retrieval import _citation

page_setup("home")


# ── Deal formatting helpers ───────────────────────────────────────────────────

_MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _fmt_date_part(s: str) -> str:
    """Format a single stored date component for human reading.
    YYYY-MM where MM≤12 → 'Mon YYYY'.  YYYY-YY season / YYYY-only → unchanged.
    """
    s = s.strip()
    m = re.match(r'^(\d{4})-(\d{2})$', s)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{_MONTH_ABBR[mo - 1]} {yr}"
    return s  # year-only, season format, or anything else: display as-is


def _fmt_value(deal: dict) -> str:
    """Return value + currency only — no qualifier text (that goes in the Notes column)."""
    v        = deal.get("value")
    currency = (deal.get("currency") or "").upper()
    SYMBOLS  = {"GBP": "£", "EUR": "€", "USD": "$", "AUD": "A$", "CAD": "C$"}
    sym      = SYMBOLS.get(currency, f"{currency} " if currency else "")
    if v is not None:
        return f"{sym}{v/1000:.2g}bn" if v >= 1000 else f"{sym}{v:.4g}m"
    return "—"


def _fmt_period(deal: dict) -> str:
    s = _fmt_date_part((deal.get("period_start") or "").strip())
    e = _fmt_date_part((deal.get("period_end")   or "").strip())
    if s and e: return f"{s}–{e}"
    if s:       return f"{s}–"
    if e:       return f"–{e}"
    return "—"


def _deal_table_html(deals: list[dict], conflict_territories: set[str], show_property: bool = False) -> str:
    if not deals:
        return ""
    rows_html = ""
    for d in deals:
        property_name = (d.get("property_name") or "—")
        territory     = (d.get("territory")     or "—")
        val_display   = _fmt_value(d)
        rights_holder = (d.get("rights_holder") or "—")
        broadcaster   = (d.get("broadcaster")   or "").strip()
        value_note    = (d.get("value_note")    or "").strip()
        period        = _fmt_period(d)
        source_raw    = (d.get("source_note")   or "").strip()
        status        = d.get("status", "current")

        # Notes column: broadcaster (bold) · qualifier · source
        note_parts: list[str] = []
        if broadcaster:
            note_parts.append(f"<strong>{broadcaster}</strong>")
        if value_note and value_note.lower() not in ("", "undisclosed"):
            note_parts.append(value_note)
        elif value_note.lower() == "undisclosed":
            note_parts.append('<em style="color:#8A9598">undisclosed</em>')
        if source_raw:
            src_disp = source_raw[:45] + "…" if len(source_raw) > 45 else source_raw
            note_parts.append(f'<span style="color:#AAA9A0;font-size:0.78rem">{src_disp}</span>')
        if status == "superseded":
            note_parts.insert(0, '<em style="color:#8A9598">superseded</em>')
        notes_html = "  ·  ".join(note_parts) if note_parts else "—"

        is_conflict   = territory.lower() in conflict_territories and status == "current"
        is_superseded = status == "superseded"
        row_class     = "conflict-row" if is_conflict else ("superseded-row" if is_superseded else "deal-row")
        conflict_icon = " ⚠" if is_conflict else ""

        reliability    = (d.get("reliability") or "reported")
        rel_badge_html = reliability_badge_html(reliability)

        property_cell = f'<td>{property_name}</td>' if show_property else ""

        rows_html += (
            f'<tr class="{row_class}">'
            f'{property_cell}'
            f'<td><strong>{territory}</strong>{conflict_icon}</td>'
            f'<td style="white-space:nowrap;color:#5A5A5A">{period}</td>'
            f'<td style="font-variant-numeric:tabular-nums;white-space:nowrap">{val_display}</td>'
            f'<td>{rights_holder}</td>'
            f'<td style="font-size:0.83rem">{notes_html}</td>'
            f'<td style="text-align:center">{rel_badge_html}</td>'
            f'</tr>\n'
        )

    property_header = "<th>Property</th>" if show_property else ""
    return (
        '<div style="overflow-x:auto;border:1px solid #E0D8CC;border-radius:8px">'
        '<table class="deals-table">'
        '<thead><tr>'
        f'{property_header}'
        '<th>Territory</th><th>Period</th><th>Value</th><th>Rights holder</th><th>Notes</th><th></th>'
        '</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        '</table></div>'
    )


# ── Resolve entity ────────────────────────────────────────────────────────────
entity_id = st.session_state.get("entity_id")
if not entity_id:
    st.error("No entity selected. Return to [Home](/) and click a card.")
    st.stop()

try:
    entity_id = int(entity_id)
except (ValueError, TypeError):
    st.error("Invalid entity ID.")
    st.stop()

entity = db.get_entity(entity_id)
if not entity:
    st.error(f"Entity #{entity_id} not found.")
    st.stop()


# ── Load all data up-front ────────────────────────────────────────────────────
entries         = db.get_entries_for_entity(entity_id)
docs            = [e for e in entries if e["entry_type"] == "document"]
snips           = [e for e in entries if e["entry_type"] == "snippet"]
current_entries = [e for e in entries if e.get("status", "current") != "superseded"]
sup_entries     = [e for e in entries if e.get("status", "current") == "superseded"]
deals_base      = db.get_deals_for_entity(entity_id, include_superseded=False)

# The conflict check is already scoped to deals whose property is this entity
# (find_conflicting_deal_territories filters on deals.entity_id, the property
# FK). On a market/broadcaster page that's structurally almost always empty —
# a market is essentially never itself a deal's property — but "many current
# deals for the same territory across different properties" is normal there,
# not a conflict, so skip the check entirely rather than rely on it staying
# empty by chance.
conflict_territories = (
    set() if entity.get("entity_type") == "market"
    else db.find_conflicting_deal_territories(entity_id)
)

n_entries = len(entries)
n_current = len(current_entries)
n_docs    = len(docs)
n_snips   = len(snips)
n_deals   = len(deals_base)


# ── Header ────────────────────────────────────────────────────────────────────
etype = entity.get("entity_type", "other")

st.markdown(
    f'{entity_type_badge(etype)}'
    f'<h1 style="margin:0.4rem 0 0.1rem;font-size:2rem">{entity["canonical_name"]}</h1>',
    unsafe_allow_html=True,
)
if entity.get("aliases"):
    st.markdown(
        f'<p style="color:#8A9598;font-size:0.85rem;margin:0 0 0.1rem">Also known as: '
        f'{entity["aliases"]}</p>',
        unsafe_allow_html=True,
    )

# ── Freshness line ────────────────────────────────────────────────────────────
latest_ts  = max((e.get("created_at") or "" for e in entries), default="") if entries else ""
overview_at = (entity.get("overview_at") or "")[:10]

_fp: list[str] = [
    f"{n_entries} source{'s' if n_entries != 1 else ''}"
    f" ({n_docs} doc{'s' if n_docs != 1 else ''}"
    f", {n_snips} snippet{'s' if n_snips != 1 else ''})"
]
if n_deals:
    _fp.append(f"{n_deals} deal record{'s' if n_deals != 1 else ''}")
if latest_ts:
    _fp.append(f"latest added {latest_ts[:10]}")
if overview_at:
    _fp.append(f"summary {overview_at}")

st.markdown(
    f'<p style="color:#8A9598;font-size:0.78rem;margin:0.25rem 0 0.75rem;'
    f'font-family:Lato,sans-serif">{"  ·  ".join(_fp)}</p>',
    unsafe_allow_html=True,
)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Intelligence summary (AI-generated, cached)
# ═══════════════════════════════════════════════════════════════════════════════
section_title("Intelligence summary")


def _build_overview_context() -> str:
    """Build context string for the overview LLM call."""
    entry_ids  = [e["id"] for e in entries]
    chunks_by  = db.get_chunks_for_entries(entry_ids)
    rows_by_id = {e["id"]: e for e in entries}
    parts: list[str] = []

    # Lead with the deal records — key structured intelligence for the overview
    if deals_base:
        lines = ["DEAL RECORDS (structured):"]
        for d in deals_base[:30]:
            line = f"- {d.get('territory','?')}: {d.get('broadcaster','?')}"
            val = _fmt_value(d)
            if val != "—": line += f" · {val}"
            period = _fmt_period(d)
            if period != "—": line += f" · {period}"
            if d.get("platform"): line += f" · {d['platform']}"
            if d.get("rights_holder"): line += f" (rights: {d['rights_holder']})"
            lines.append(line)
        parts.append("\n".join(lines))

    for eid in entry_ids:
        row    = rows_by_id[eid]
        chunks = chunks_by.get(eid, [])
        cite   = _citation(row)
        text   = "\n\n".join(
            f"[{c.get('chunk_type','page')} {c.get('chunk_num','')}] {c['text']}"
            for c in chunks[:2]
        ) or row.get("summary", "")
        if text:
            parts.append(f"SOURCE: {cite}\n{text}")
    return "\n\n---\n\n".join(parts)


# Stale check: any entry added after the last overview generation?
overview_stale = bool(
    entries
    and overview_at
    and max((e.get("created_at") or "")[:10] for e in entries) > overview_at
)

refresh_btn = st.button("↻ Refresh summary", type="secondary", key="refresh_overview")

overview = entity.get("overview", "")
if not overview or refresh_btn:
    if entries:
        with st.spinner("Generating intelligence summary…"):
            ctx      = _build_overview_context()
            overview = generate_entity_overview(entity["canonical_name"], ctx)
            db.update_entity(entity_id, overview=overview, overview_at="datetime('now')")
            overview_stale = False  # just refreshed

if overview_stale and not refresh_btn:
    st.caption("⚠ New sources added since this summary was last generated — consider refreshing.")

if overview:
    paras = [p.strip() for p in overview.split("\n\n") if p.strip()]
    inner = "".join(f"<p>{p}</p>" for p in paras)
    st.markdown(f'<div class="overview-box">{inner}</div>', unsafe_allow_html=True)
elif not entries:
    st.info(
        "No sources are linked to this entity yet. "
        "Add documents or snippets to generate an intelligence summary.",
        icon=None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Media rights deals
# ═══════════════════════════════════════════════════════════════════════════════
section_title("Media rights deals")

_show_sup = st.toggle("Include superseded deals", value=False, key="deals_show_sup")
deals_all = db.get_deals_for_entity(entity_id, include_superseded=_show_sup)

def _period_end_int(d: dict) -> int:
    end = (d.get("period_end") or "").strip()
    try:
        return int(end[:4]) if end and end[:4].isdigit() else 9999
    except ValueError:
        return 9999

deals_all.sort(key=lambda d: (
    0 if d.get("status") != "superseded" else 1,
    -_period_end_int(d),    # descending: 9999 (no end date = ongoing) sorts first
    (d.get("territory") or "").lower(),
))

if not deals_all:
    st.markdown(
        '<div class="deal-empty">'
        '<div class="deal-empty-title">No deal data yet</div>'
        '<div style="font-size:0.88rem">Use the form below to add a deal manually, '
        'or ingest a document containing deal terms.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
else:
    n_curr_d = sum(1 for d in deals_all if d.get("status") == "current")
    n_terr   = len({(d.get("territory") or "").lower() for d in deals_all if d.get("territory")})
    show_conflicts = entity.get("entity_type") != "market"
    if show_conflicts:
        n_conf = len(conflict_territories)
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("Current deals", n_curr_d)
        mc2.metric("Territories",   n_terr)
        mc3.metric("⚠ Conflicts",   n_conf)
        if conflict_territories:
            ter_list = ", ".join(sorted(t.title() for t in conflict_territories))
            st.warning(
                f"Multiple current deals exist for: **{ter_list}**. "
                "Consider marking older deals as superseded using the tool below.",
                icon=None,
            )
    else:
        mc1, mc2 = st.columns(2)
        mc1.metric("Current deals", n_curr_d)
        mc2.metric("Territories",   n_terr)
    # Property is implicit on a property's own page (every row is its deal) —
    # only worth a column when this page can show deals from more than one
    # property, i.e. whenever some row's property isn't this page's entity.
    show_property = any(d.get("entity_id") != entity_id for d in deals_all)
    st.markdown(
        _deal_table_html(deals_all, conflict_territories, show_property=show_property),
        unsafe_allow_html=True,
    )

# Mark superseded
current_deals_q = db.get_deals_for_entity(entity_id, include_superseded=False)
if len(current_deals_q) >= 2:
    with st.expander("Mark a deal as superseded"):
        def _deal_label(d: dict) -> str:
            return f"#{d['id']} · {d.get('territory','?')} · {d.get('broadcaster','?')} · {_fmt_period(d)}"
        sup_opts = {_deal_label(d): d["id"] for d in current_deals_q}
        sel_old = st.selectbox("Deal to mark superseded", list(sup_opts.keys()), key="sup_old_deal")
        sel_new = st.selectbox("Superseded by",           list(sup_opts.keys()), key="sup_new_deal")
        if st.button("Mark superseded", type="secondary", key="sup_deal_btn"):
            old_id = sup_opts[sel_old]; new_id = sup_opts[sel_new]
            if old_id == new_id:
                st.error("Select two different deals.")
            else:
                db.mark_deal_superseded(old_id, new_id)
                st.success(f"Deal #{old_id} marked as superseded by #{new_id}.")
                st.rerun()

# Add manually
with st.expander("➕ Add a deal manually"):
    with st.form("add_deal_form"):
        st.markdown("**New deal for** " + entity["canonical_name"])
        fa1, fa2 = st.columns(2)
        d_territory   = fa1.text_input("Territory *",   placeholder="e.g. United Kingdom")
        d_broadcaster = fa2.text_input("Broadcaster *", placeholder="e.g. Sky Sports")
        fb1, fb2 = st.columns(2)
        d_rights_holder = fb1.text_input("Rights holder", placeholder="e.g. Premier League Productions")
        d_platform      = fb2.text_input("Platform",      placeholder="e.g. TV + streaming")
        fc1, fc2, fc3 = st.columns(3)
        d_value      = fc1.number_input("Value (millions)", min_value=0.0, value=0.0, step=0.1, format="%.2f")
        d_currency   = fc2.selectbox("Currency", ["", "GBP", "EUR", "USD", "AUD", "CAD", "Other"])
        d_value_note = fc3.text_input("Qualifier", placeholder="e.g. per season, total")
        fd1, fd2 = st.columns(2)
        d_period_start = fd1.text_input("Period start", placeholder="e.g. 2022")
        d_period_end   = fd2.text_input("Period end",   placeholder="e.g. 2025")
        d_source_note  = st.text_input("Source", placeholder="e.g. SportBusiness, Annual Report 2024")
        fe1, fe2 = st.columns(2)
        d_status      = fe1.selectbox("Status",      ["current", "unverified"])
        d_reliability = fe2.selectbox("Reliability", ["confirmed", "reported", "rumoured"], index=1)
        submitted_deal = st.form_submit_button("Save deal", type="primary")

    if submitted_deal:
        if not d_territory.strip() or not d_broadcaster.strip():
            st.error("Territory and Broadcaster are required.")
        else:
            new_deal_id = db.add_deal(
                entity_id     = entity_id,
                territory     = d_territory.strip(),
                broadcaster   = d_broadcaster.strip(),
                rights_holder = d_rights_holder.strip(),
                value         = d_value if d_value > 0 else None,
                currency      = d_currency if d_currency != "Other" else "",
                value_note    = d_value_note.strip(),
                period_start  = d_period_start.strip(),
                period_end    = d_period_end.strip(),
                platform      = d_platform.strip(),
                source_note   = d_source_note.strip(),
                status        = d_status,
                reliability   = d_reliability,
            )
            st.success(f"Deal #{new_deal_id} saved.")
            existing_same = [
                d for d in current_deals_q
                if (d.get("territory") or "").lower() == d_territory.strip().lower()
                and d["id"] != new_deal_id
            ]
            if existing_same:
                others = ", ".join(f"#{d['id']} ({d.get('broadcaster','?')})" for d in existing_same)
                st.warning(
                    f"There are already current deals for **{d_territory.strip()}**: {others}. "
                    "Use 'Mark a deal as superseded' above to resolve the conflict if needed.",
                    icon=None,
                )
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Broadcaster coverage
# ═══════════════════════════════════════════════════════════════════════════════
if deals_base:
    section_title("Broadcaster coverage")

    _CUR_YEAR   = date.today().year
    _STATUS_ORD = {"active": 0, "not_stated": 1, "expired": 2}

    def _bc_period_str(d: dict) -> str:
        s = _fmt_date_part((d.get("period_start") or "").strip())
        e = _fmt_date_part((d.get("period_end")   or "").strip())
        if s and e: return f"{s}–{e}"
        if s:       return f"from {s}"
        if e:       return f"until {e}"
        return ""

    def _bc_period_status(d: dict) -> str:
        """'active', 'expired', or 'not_stated' — derived from period fields, never invented."""
        e = (d.get("period_end")   or "").strip()
        s = (d.get("period_start") or "").strip()
        if not e and not s:
            return "not_stated"
        if e:
            sm = re.match(r'(\d{4})[-/](\d{2})$', e)  # season format e.g. "2025-26"
            if sm:
                end_year = int(sm.group(1)[:2] + sm.group(2))
            else:
                nums = re.findall(r'\d{4}', e)         # take last year in string
                end_year = int(nums[-1]) if nums else None
            if end_year is not None:
                return "active" if end_year >= _CUR_YEAR else "expired"
        return "active"  # has start date only → treat as ongoing

    # Group by (broadcaster, period_str) → territories
    _groups: dict[tuple, list[str]] = {}
    _grp_stat: dict[tuple, str] = {}

    for _d in deals_base:
        _bc  = (_d.get("broadcaster") or "").strip()
        _ter = (_d.get("territory")   or "").strip()
        if not _bc or not _ter:
            continue
        _per  = _bc_period_str(_d)
        _stat = _bc_period_status(_d)
        _key  = (_bc, _per)
        if _key not in _groups:
            _groups[_key] = []
            _grp_stat[_key] = _stat
        if _ter not in _groups[_key]:
            _groups[_key].append(_ter)
        # Upgrade status within group: active beats not_stated beats expired
        if _STATUS_ORD.get(_stat, 1) < _STATUS_ORD.get(_grp_stat[_key], 2):
            _grp_stat[_key] = _stat

    # Best status per broadcaster (for overall ordering)
    _bc_best: dict[str, int] = {}
    for (_bc, _per), _stat in _grp_stat.items():
        r = _STATUS_ORD.get(_stat, 1)
        if r < _bc_best.get(_bc, 99):
            _bc_best[_bc] = r

    # Sort: active broadcasters first → not_stated → expired; then alphabetical by name; then period
    _sorted_bc = sorted(
        _groups.items(),
        key=lambda kv: (
            _bc_best.get(kv[0][0], 1),
            kv[0][0].lower(),
            kv[0][1],
        ),
    )

    if _sorted_bc:
        _rows_html = ""
        _prev_bc   = None

        for (_bc, _per), _ters in _sorted_bc:
            _stat     = _grp_stat.get((_bc, _per), "not_stated")
            _ters_str = ", ".join(sorted(_ters))

            # Period cell
            if _per:
                if _stat == "active":
                    _per_html = (
                        f'<span style="color:#2A7F4F;font-weight:500">{_per}</span>'
                    )
                elif _stat == "expired":
                    _per_html = (
                        f'<span style="color:#AAA9A0;text-decoration:line-through">'
                        f'{_per}</span>'
                        f'<em style="color:#AAA9A0;font-size:0.78rem;margin-left:0.3rem">'
                        f'expired</em>'
                    )
                else:
                    _per_html = f'<span style="color:#5B6B72">{_per}</span>'
            else:
                _per_html = (
                    '<em style="color:#AAA9A0;font-size:0.82rem">Not stated</em>'
                )

            # Broadcaster cell — show name only on first row of each broadcaster block
            if _bc != _prev_bc:
                _bc_html = (
                    f'<td style="white-space:nowrap;font-weight:600;'
                    f'padding:0.5rem 1.5rem 0.25rem 0;vertical-align:top;'
                    f'color:#2B383E;border-top:1px solid #F0EBE1">{_bc}</td>'
                )
                _prev_bc = _bc
            else:
                _bc_html = (
                    '<td style="padding:0.1rem 1.5rem 0.1rem 0;border-top:none"></td>'
                )

            _rows_html += (
                f'<tr>'
                f'{_bc_html}'
                f'<td style="white-space:nowrap;padding:0.25rem 1.5rem 0.25rem 0;'
                f'font-size:0.88rem">{_per_html}</td>'
                f'<td style="color:#5B6B72;font-size:0.88rem;padding:0.25rem 0">'
                f'{_ters_str}</td>'
                f'</tr>\n'
            )

        st.markdown(
            '<div style="margin-top:0.25rem;overflow-x:auto">'
            '<table style="width:100%;border-collapse:collapse">'
            '<thead><tr>'
            '<th style="text-align:left;padding:0 1.5rem 0.5rem 0;font-size:0.72rem;'
            'text-transform:uppercase;letter-spacing:0.05em;color:#8A9598;'
            'border-bottom:2px solid #E0D8CC">Broadcaster</th>'
            '<th style="text-align:left;padding:0 1.5rem 0.5rem 0;font-size:0.72rem;'
            'text-transform:uppercase;letter-spacing:0.05em;color:#8A9598;'
            'border-bottom:2px solid #E0D8CC">Rights period</th>'
            '<th style="text-align:left;padding:0 0 0.5rem;font-size:0.72rem;'
            'text-transform:uppercase;letter-spacing:0.05em;color:#8A9598;'
            'border-bottom:2px solid #E0D8CC">Territories</th>'
            '</tr></thead>'
            f'<tbody>{_rows_html}</tbody>'
            '</table>'
            '<p style="font-size:0.74rem;color:#AAA9A0;margin:0.6rem 0 0;'
            'font-family:Lato,sans-serif">'
            '<span style="color:#2A7F4F">Active</span>'
            '&ensp;·&ensp;<span style="color:#AAA9A0;text-decoration:line-through">Expired</span>'
            '&ensp;·&ensp;<em>Not stated</em> = no period recorded in source data'
            '</p>'
            '</div>',
            unsafe_allow_html=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Source documents
# ═══════════════════════════════════════════════════════════════════════════════
section_title("Sources")

if not entries:
    st.caption("No documents or notes are linked to this entity yet.")
else:
    for _doc in sorted(docs, key=lambda e: (e.get("entry_date") or "", e["source"]), reverse=True):
        _meta = [b for b in (
            _doc.get("file_type", ""), _doc.get("doc_type", ""), _doc.get("entry_date", "")
        ) if b]
        _c_label, _c_btn = st.columns([5, 1])
        _c_label.markdown(f"📄 **{_doc['source']}**")
        if _meta:
            _c_label.caption("  ·  ".join(_meta))
        with _c_btn:
            open_file_button(_doc, key_suffix=f"ent_{entity_id}", use_container_width=True)

    if snips:
        st.caption(
            f"Plus {len(snips)} logged note{'s' if len(snips) != 1 else ''}: "
            + ", ".join(f"{s['source']} ({s.get('entry_date', '?')})" for s in snips[:8])
            + ("…" if len(snips) > 8 else "")
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Page actions
# ═══════════════════════════════════════════════════════════════════════════════
if entity.get("is_featured") or home_removal_pending(entity_id, key_suffix="entity_page"):
    st.markdown('<div style="height:0.5rem"></div>', unsafe_allow_html=True)
    _pa_col, _ = st.columns([1, 3])
    with _pa_col:
        remove_from_home_control(entity, key_suffix="entity_page")


"""
SN1 Knowledge Base — Home page (served via st.navigation in app.py)
"""

import streamlit as st

from kb.ui import (
    page_setup, entity_card_html, section_title, stats_strip, ENTITY_TYPE_META,
    home_removal_pending, home_card_remove_trigger, home_card_remove_followup,
)
from kb.db import get_entity_stats, get_stats, get_all_entries

page_setup("home")

# ── Pre-flight: ensure DB is ready ────────────────────────────────────────────
from kb.db import init_db
init_db()

# ── Hero search bar ───────────────────────────────────────────────────────────
st.markdown('<div style="height:1.5rem"></div>', unsafe_allow_html=True)
st.markdown(
    '<h1 style="font-family:Open Sans,sans-serif;font-size:1.9rem;font-weight:700;'
    'color:#2B383E;margin-bottom:0.15rem">Media Rights Intelligence</h1>',
    unsafe_allow_html=True,
)
st.markdown(
    '<p style="color:#7e8e94;font-size:1rem;margin-bottom:1.25rem">'
    'Search every document, note and entity in the knowledge base — or ask a question in plain English.</p>',
    unsafe_allow_html=True,
)

col_search, col_btn = st.columns([8, 1])
with col_search:
    st.markdown('<div class="search-wrap">', unsafe_allow_html=True)
    question = st.text_input(
        "search_bar",
        label_visibility="collapsed",
        placeholder="Ask anything — e.g. 'What are the UEFA Champions League rights deals for 2027+'",
        key="home_search",
    )
    st.markdown("</div>", unsafe_allow_html=True)
with col_btn:
    st.markdown('<div style="padding-top:0.35rem"></div>', unsafe_allow_html=True)
    ask_btn = st.button("Ask →", type="primary", use_container_width=True)

if ask_btn and question.strip():
    st.session_state["pending_question"] = question.strip()
    st.switch_page(st.session_state["_pages"]["ask"])

# ── Stats strip ───────────────────────────────────────────────────────────────
stats = get_stats()
stats_strip(stats)

# ── Featured entity cards ─────────────────────────────────────────────────────
entity_stats = get_entity_stats()
featured = [
    e for e in entity_stats
    if e.get("is_featured") or home_removal_pending(e["id"], key_suffix="home_card")
]
COLS = 3

if featured:
    # Sort: by entity type order then name
    TYPE_ORDER = ["competition", "federation", "broadcaster", "rights_holder", "market", "client", "club", "other"]
    featured.sort(key=lambda e: (TYPE_ORDER.index(e["entity_type"]) if e["entity_type"] in TYPE_ORDER else 99,
                                 e["canonical_name"].lower()))
    for row_start in range(0, len(featured), COLS):
        cols = st.columns(COLS)
        for col_i, entity in enumerate(featured[row_start:row_start + COLS]):
            with cols[col_i]:
                st.markdown(entity_card_html(entity, show_badge=True), unsafe_allow_html=True)
                open_col, remove_col = st.columns([4, 1])
                with open_col:
                    if st.button(
                        "Open →",
                        key=f"card_{entity['id']}",
                        use_container_width=True,
                        type="secondary",
                    ):
                        st.session_state["entity_id"] = entity["id"]
                        st.switch_page(st.session_state["_pages"]["entity"])
                with remove_col:
                    home_card_remove_trigger(entity, key_suffix="home_card")
                home_card_remove_followup(entity, key_suffix="home_card")
else:
    st.info(
        "No entities are featured on this page yet. "
        "Go to **Admin → Active Entities** and tick 'Show on Home page' for the ones you want here. "
        "All entities remain accessible via Browse and Ask."
    )

# ── Recent additions ──────────────────────────────────────────────────────────
section_title("Recent additions")
recent = get_stats().get("recent", [])
if recent:
    for item in recent[:6]:
        icon = "📄" if item["entry_type"] == "document" else "📝"
        added = (item.get("created_at") or "")[:10]
        st.markdown(
            f'<div style="padding:0.4rem 0;border-bottom:1px solid #F0EBE3;'
            f'font-size:0.88rem;color:#2B383E">'
            f'{icon} <strong>{item["source"]}</strong>'
            f'<span style="color:#8A9598;margin-left:0.5rem;font-size:0.78rem">{added}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

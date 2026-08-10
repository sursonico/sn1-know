"""
app.py — Router. Uses st.navigation() so all pages are explicitly registered
and st.switch_page() works reliably. The sidebar nav is hidden; each page
renders its own branded header with custom nav links.
"""

import streamlit as st
from kb.ui import ensure_share_auth
from seed_entities import seed_if_empty

st.set_page_config(
    page_title="SN1 — Media Rights Intelligence",
    page_icon="📋",
    layout="wide",
)

# Bootstrap canonical entities on first launch so Render deployments do not
# require shell access for the initial database population.
seed_if_empty(verbose=False)

ensure_share_auth()

HOME   = st.Page("pages/home.py",    title="Home",       icon="🏠", default=True)
ENTITY = st.Page("pages/entity.py",  title="Entity",     icon="📋")
BROWSE = st.Page("pages/browse.py",  title="Browse",     icon="📂")
ASK    = st.Page("pages/ask.py",     title="Ask",        icon="💬")
ADDLOG = st.Page("pages/add_log.py", title="Add & Log",  icon="➕")
ADMIN  = st.Page("pages/admin.py",   title="Admin",      icon="⚙️")

pg = st.navigation(
    [HOME, ENTITY, BROWSE, ASK, ADDLOG, ADMIN],
    position="hidden",   # hide the default sidebar nav
)

# Store page refs in session_state so any page can call st.switch_page(ref)
st.session_state["_pages"] = {
    "home":   HOME,
    "entity": ENTITY,
    "browse": BROWSE,
    "ask":    ASK,
    "add_log":ADDLOG,
    "admin":  ADMIN,
}

pg.run()

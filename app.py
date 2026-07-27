"""
app.py — Router. Uses st.navigation() so all pages are explicitly registered
and st.switch_page() works reliably. The sidebar nav is hidden; each page
renders its own branded header with custom nav links.
"""

import hashlib
import os
import streamlit as st
from seed_entities import seed_if_empty

st.set_page_config(
    page_title="SN1 — Media Rights Intelligence",
    page_icon="📋",
    layout="wide",
)

# Bootstrap canonical entities on first launch so Render deployments do not
# require shell access for the initial database population.
seed_if_empty(verbose=False)

# ── Temporary sharing password gate ───────────────────────────────────────────
# Only active when SN1_SHARE_PASSWORD env var is set. Local dev is unaffected.
#
# Auth survives page navigation because the token travels as a URL query param.
# After the user authenticates, all nav links include ?_auth=TOKEN so that each
# full-page reload (from <a href> nav) re-validates without re-prompting.
_SHARE_PW = os.environ.get("SN1_SHARE_PASSWORD", "")
if _SHARE_PW:
    _TOKEN = hashlib.sha256(_SHARE_PW.encode()).hexdigest()[:24]

    # Validate: session state (same WebSocket) OR matching URL token (new reload)
    if not st.session_state.get("_authenticated"):
        if st.query_params.get("_auth") == _TOKEN:
            st.session_state["_authenticated"] = True

    if not st.session_state.get("_authenticated"):
        st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@300;600;700&display=swap');
        header[data-testid="stHeader"] { display:none!important; }
        section[data-testid="stMainBlockContainer"],.block-container {
            padding-top:0!important; max-width:100%!important;
        }
        .gate-wrap { max-width:340px; margin:6rem auto 0; text-align:center; }
        .gate-logo {
            font-family:'Open Sans',sans-serif; font-size:1.5rem; font-weight:700;
            color:#2B383E; letter-spacing:-0.02em; margin-bottom:0.2rem;
        }
        .gate-sub {
            font-family:'Open Sans',sans-serif; font-size:0.78rem; font-weight:300;
            letter-spacing:0.2em; text-transform:uppercase;
            color:#8A9598; margin-bottom:2.5rem;
        }
        .gate-wrap .stTextInput input {
            text-align:center; border:1.5px solid #D8D0C4!important;
            border-radius:6px!important; font-size:1rem!important;
        }
        .gate-wrap .stTextInput input:focus {
            border-color:#AA925C!important;
            box-shadow:0 0 0 2px rgba(170,146,92,.15)!important;
        }
        </style>
        <div class="gate-wrap">
          <div class="gate-logo">SN1</div>
          <div class="gate-sub">Media Rights Intelligence</div>
        </div>
        """, unsafe_allow_html=True)

        with st.container():
            col = st.columns([1, 2, 1])[1]
            pw = col.text_input("", type="password", placeholder="Access password",
                                key="_gate_pw", label_visibility="collapsed")
            if pw:
                if pw == _SHARE_PW:
                    st.session_state["_authenticated"] = True
                    st.session_state["_auth_token"] = _TOKEN
                    st.query_params["_auth"] = _TOKEN
                    st.rerun()
                else:
                    col.error("Incorrect password.")
        st.stop()
    else:
        # Already authenticated — keep token in session state for nav links
        st.session_state["_auth_token"] = _TOKEN

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

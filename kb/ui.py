"""
kb/ui.py — Shared Streamlit UI utilities for every page.

Call page_setup(active) as the very first thing in each page module.
This handles set_page_config, CSS injection, and the branded header + nav.
"""

import base64
import hashlib
from pathlib import Path

import streamlit as st

from config import LOGO_PATH, DB_PATH, SHARE_PASSWORD
from kb import db

# ── Type metadata ─────────────────────────────────────────────────────────────

ENTITY_TYPE_META: dict[str, dict] = {
    "competition":  {"icon": "🏆", "color": "#AA925C",  "label": "Competition"},
    "federation":   {"icon": "🏛️", "color": "#2B383E",  "label": "Federation"},
    "broadcaster":  {"icon": "📺", "color": "#2A7F7F",  "label": "Broadcaster"},
    "market":       {"icon": "🌍", "color": "#5B7B8A",  "label": "Market"},
    "rights_holder":{"icon": "📋", "color": "#7B5B2A",  "label": "Rights Holder"},
    "club":         {"icon": "⚽", "color": "#4A7B4A",  "label": "Club"},
    "other":        {"icon": "◦",  "color": "#8A9598",  "label": "Other"},
}

NAV_ITEMS = [
    ("Home",      "/",          "home"),
    ("Browse",    "/browse",    "browse"),
    ("Ask",       "/ask",       "ask"),
    ("Add & Log", "/add_log",   "add_log"),
    ("Admin",     "/admin",     "admin"),
]

# ── CSS + styling ─────────────────────────────────────────────────────────────

def _logo_b64() -> str:
    return base64.b64encode(LOGO_PATH.read_bytes()).decode() if LOGO_PATH.exists() else ""


_STYLES = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@300;400;600;700&family=Lato:ital,wght@0,300;0,400;0,700;1,400&display=swap');

/* ── Base — body only; NOT [class*="st-"] (breaks Material Symbols icon font) */
html, body, .stApp { font-family: 'Lato', sans-serif; background: #FAFAF8; }
h1,h2,h3,h4,h5,h6,
.stMarkdown h1,.stMarkdown h2,.stMarkdown h3 {
    font-family: 'Open Sans', sans-serif !important;
    font-weight: 600; color: #2B383E; letter-spacing: -0.02em; margin-top: 0.1rem;
}

/* ── Remove default chrome */
header[data-testid="stHeader"] { display: none !important; }
[data-testid="stSidebarNav"], section[data-testid="stSidebar"] { display: none !important; }
section[data-testid="stMainBlockContainer"], .block-container {
    padding-top: 0 !important; padding-bottom: 3rem !important; max-width: 100% !important;
}

/* ── SN1 branded header */
.sn1-header {
    background: #2B383E; padding: 0.8rem 2.5rem;
    display: flex; align-items: center; gap: 1.5rem; position: sticky; top: 0; z-index: 999;
}
.sn1-logo-pill { background:#fff; border-radius:4px; padding:4px 9px; line-height:0; flex-shrink:0; }
.sn1-logo { height:26px; width:auto; display:block; }
.sn1-wordmark {
    font-family:'Open Sans',sans-serif; font-weight:300; font-size:0.85rem;
    color:rgba(255,255,255,.65); letter-spacing:.22em; text-transform:uppercase;
    padding-left:1.1rem; border-left:1px solid rgba(170,146,92,.4); line-height:1;
    flex-shrink:0;
}
.sn1-nav { display:flex; align-items:center; gap:0.25rem; margin-left:auto; }
.sn1-nav a {
    font-family:'Open Sans',sans-serif; font-size:0.72rem; font-weight:700;
    letter-spacing:0.09em; text-transform:uppercase; color:rgba(255,255,255,.55) !important;
    padding:0.45rem 0.85rem; border-radius:4px; text-decoration:none !important;
    transition:color .15s, background .15s;
}
.sn1-nav a:hover { color:#AA925C !important; background:rgba(170,146,92,.1); }
.sn1-nav a.active { color:#AA925C !important; border-bottom: 2px solid #AA925C; }
.sn1-rule { height:2px; background:#AA925C; margin:0; }

/* ── Entity cards */
.entity-card {
    background:#fff; border:1px solid #E8E1D6; border-radius:10px;
    padding:1.2rem 1.25rem 1rem; cursor:pointer;
    transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}
.entity-card:hover {
    transform:translateY(-3px);
    box-shadow:0 6px 20px rgba(43,56,62,.1);
    border-color:#AA925C;
}
.entity-card-type {
    font-family:'Open Sans',sans-serif; font-size:0.65rem; font-weight:700;
    letter-spacing:0.12em; text-transform:uppercase;
    padding:2px 8px; border-radius:20px; display:inline-block; margin-bottom:0.6rem;
    color:#fff;
}
.entity-card-name {
    font-family:'Open Sans',sans-serif; font-size:1rem; font-weight:700;
    color:#2B383E; margin-bottom:0.5rem; line-height:1.25;
}
.entity-card-stats {
    font-family:'Lato',sans-serif; font-size:0.8rem; color:#7e8e94; margin-top:0.3rem;
}
.entity-card-updated {
    font-family:'Lato',sans-serif; font-size:0.73rem; color:#A0B0B8; margin-top:0.15rem;
    font-style:italic;
}

/* ── Stats strip */
.stats-strip {
    display:flex; gap:2rem; padding:0.85rem 0; margin-bottom:0.5rem;
    border-bottom:1px solid #E8E1D6;
}
.stats-item { text-align:center; }
.stats-num {
    font-family:'Open Sans',sans-serif; font-size:1.5rem; font-weight:700;
    color:#2B383E; line-height:1;
}
.stats-label {
    font-family:'Lato',sans-serif; font-size:0.72rem; color:#8A9598;
    text-transform:uppercase; letter-spacing:0.08em; margin-top:0.15rem;
}

/* ── Search bar */
.search-wrap { margin:1.5rem 0 1rem; }
.search-wrap .stTextInput input {
    font-family:'Lato',sans-serif !important; font-size:1.05rem !important;
    border:2px solid #E8E1D6 !important; border-radius:8px !important;
    padding:0.75rem 1.1rem !important; background:#fff !important;
    transition:border-color .15s !important;
}
.search-wrap .stTextInput input:focus {
    border-color:#AA925C !important;
    box-shadow:0 0 0 3px rgba(170,146,92,.1) !important;
}

/* ── Section headings */
.section-title {
    font-family:'Open Sans',sans-serif; font-size:0.75rem; font-weight:700;
    letter-spacing:0.1em; text-transform:uppercase; color:#8A9598;
    margin:1.5rem 0 0.75rem; padding-bottom:0.4rem;
    border-bottom:1px solid #E8E1D6;
}

/* ── Overview box (entity hub) */
.overview-box {
    background:#F3ECE0; border-radius:8px; padding:1.25rem 1.5rem; margin:1rem 0 1.5rem;
    border-left:3px solid #AA925C;
}
.overview-box p { font-size:0.95rem; line-height:1.65; color:#2B383E; margin-bottom:0.6rem; }
.overview-box p:last-child { margin-bottom:0; }

/* ── Buttons */
button[data-testid="stBaseButton-primary"] {
    background:#AA925C !important; color:#fff !important; border:none !important;
    font-family:'Open Sans',sans-serif !important; font-weight:700 !important;
    font-size:0.72rem !important; letter-spacing:0.1em !important; text-transform:uppercase !important;
    border-radius:5px !important; padding:0.55rem 1.5rem !important; transition:background .15s !important;
}
button[data-testid="stBaseButton-primary"]:hover:not(:disabled) { background:#8B7540 !important; }
button[data-testid="stBaseButton-primary"]:disabled { background:#C9B98A !important; opacity:.65 !important; }
button[data-testid="stBaseButton-secondary"] {
    background:transparent !important; color:#2B383E !important;
    border:1.5px solid #AA925C !important;
    font-family:'Open Sans',sans-serif !important; font-weight:700 !important;
    font-size:0.72rem !important; letter-spacing:0.08em !important; text-transform:uppercase !important;
    border-radius:5px !important; padding:0.5rem 1.25rem !important;
}
button[data-testid="stBaseButton-secondary"]:hover:not(:disabled) { background:#F3ECE0 !important; }

/* ── Inputs */
.stTextInput input, .stTextArea textarea, [data-baseweb="select"] > div:first-child {
    font-family:'Lato',sans-serif !important; font-size:0.93rem !important;
    border:1.5px solid #D8D0C4 !important; border-radius:6px !important;
    color:#2B383E !important; background:#fff !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color:#AA925C !important; box-shadow:0 0 0 2px rgba(170,146,92,.12) !important;
}
.stTextInput label, .stTextArea label, .stSelectbox label,
.stMultiSelect label, .stDateInput label {
    font-family:'Open Sans',sans-serif !important; font-size:0.7rem !important;
    font-weight:700 !important; letter-spacing:0.08em !important;
    text-transform:uppercase !important; color:#5B6B72 !important;
}

/* ── Dataframe */
[data-testid="stDataFrame"] > div {
    border:1px solid #E0D8CC !important; border-radius:8px !important; overflow:hidden;
}
[data-testid="stDataFrame"] thead th {
    font-family:'Open Sans',sans-serif !important; font-size:0.69rem !important;
    font-weight:700 !important; letter-spacing:0.07em !important;
    text-transform:uppercase !important; background:#F3ECE0 !important; color:#2B383E !important;
}

/* ── Deals table */
.deals-table {
    width:100%; border-collapse:collapse; font-size:0.88rem; margin:0.5rem 0 1rem;
}
.deals-table th {
    font-family:'Open Sans',sans-serif; font-size:0.67rem; font-weight:700;
    letter-spacing:0.09em; text-transform:uppercase; color:#2B383E;
    background:#F3ECE0; padding:0.5rem 0.75rem; text-align:left;
    border-bottom:2px solid #E0D8CC;
}
.deals-table td {
    padding:0.5rem 0.75rem; border-bottom:1px solid #EDE7DC;
    color:#2B383E; vertical-align:middle;
}
.deals-table tr:last-child td { border-bottom:none; }
.deals-table tr.deal-row:hover td { background:#FAFAF5; }
.deals-table tr.conflict-row td { background:#FFF8EE; }
.deals-table tr.conflict-row:hover td { background:#FFF2DC; }
.deals-table tr.superseded-row td { color:#B0A898; }
.deal-badge {
    font-size:0.63rem; font-weight:700; letter-spacing:0.08em; text-transform:uppercase;
    padding:2px 7px; border-radius:10px; display:inline-block; white-space:nowrap;
}
.deal-badge-current    { background:#E8F4EC; color:#2D7A4A; }
.deal-badge-superseded { background:#EFE8DE; color:#7B5B2A; }
.deal-badge-unverified { background:#EEF0F4; color:#5B6B72; }
.deal-empty {
    background:#F3ECE0; border-radius:8px; padding:2rem; text-align:center; color:#8A9598;
    margin:0.5rem 0 1rem; border:1px dashed #D0C8B8;
}
.deal-empty-title {
    font-family:'Open Sans',sans-serif; font-size:1rem; font-weight:600;
    color:#2B383E; margin-bottom:0.4rem;
}

/* ── Reliability badges */
.rel-badge {
    font-size:0.62rem; font-weight:700; letter-spacing:0.08em; text-transform:uppercase;
    padding:2px 7px; border-radius:10px; display:inline-block; white-space:nowrap;
}
.rel-confirmed { background:#E8F4EC; color:#2D7A4A; }
.rel-reported  { background:#FBF1DC; color:#7B5C1A; }
.rel-rumoured  { background:#EEF0F4; color:#5B6B72; }

/* ── Misc */
.stCaption p { font-size:0.8rem !important; color:#8A9598 !important; font-style:italic; }
[data-testid="stExpander"] { border:1px solid #DDD5C5 !important; border-radius:8px !important; }
[data-testid="stExpander"] summary {
    font-family:'Open Sans',sans-serif !important; font-weight:600 !important; font-size:0.85rem !important;
    color:#2B383E !important;
}
[data-testid="stExpander"] summary:hover { color:#AA925C !important; }
hr { border:none !important; border-top:1px solid #E0D8CC !important; margin:1.25rem 0 !important; }
a, a:visited { color:#AA925C !important; }
a:hover { color:#7A6C3E !important; text-decoration:underline !important; }
code { font-size:0.85em; background:#F3ECE0 !important; color:#2B383E !important; border-radius:4px; padding:.1em .35em; }
[data-testid="stAlert"] p { font-family:'Lato',sans-serif !important; }
[data-testid="stProgressBar"] > div > div { background:#AA925C !important; }
.stMetric label { font-family:'Open Sans',sans-serif !important; font-size:0.7rem !important; font-weight:700 !important; letter-spacing:0.08em !important; text-transform:uppercase !important; color:#8A9598 !important; }
.stMetric [data-testid="stMetricValue"] { font-family:'Open Sans',sans-serif !important; font-weight:700 !important; color:#2B383E !important; }
</style>
"""


def inject_styles() -> None:
    st.markdown(_STYLES, unsafe_allow_html=True)


def ensure_share_auth() -> None:
    """Require SN1 share password across all pages when enabled."""
    if not SHARE_PASSWORD:
        return

    token = hashlib.sha256(SHARE_PASSWORD.encode()).hexdigest()[:24]

    # Validate via current session, or URL token after full-page nav reload.
    if not st.session_state.get("_authenticated"):
        if st.query_params.get("_auth") == token:
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
            pw = col.text_input(
                "",
                type="password",
                placeholder="Access password",
                key="_gate_pw",
                label_visibility="collapsed",
            )
            if pw:
                if pw == SHARE_PASSWORD:
                    st.session_state["_authenticated"] = True
                    st.session_state["_auth_token"] = token
                    st.query_params["_auth"] = token
                    st.rerun()
                else:
                    col.error("Incorrect password.")
        st.stop()

    st.session_state["_auth_token"] = token


def render_header(active: str = "home") -> None:
    b64 = _logo_b64()
    logo_html = (
        f'<div class="sn1-logo-pill"><img src="data:image/png;base64,{b64}" class="sn1-logo" alt="SN1"></div>'
        if b64 else '<span style="color:#AA925C;font-family:Open Sans;font-weight:700;font-size:1.1rem">SN1</span>'
    )
    # Append the auth token to nav hrefs when sharing is active, so the gate
    # auto-passes on each full-page reload caused by <a href> navigation.
    auth_token = st.session_state.get("_auth_token", "")
    auth_suffix = f"?_auth={auth_token}" if auth_token else ""

    # target="_parent" navigates the top-level window, not just the iframe
    # that Streamlit's WebSocket client runs in.
    nav_links = "".join(
        f'<a href="{url}{auth_suffix}" target="_parent" rel="noopener" '
        f'class="{"active" if key == active else ""}">{label}</a>'
        for label, url, key in NAV_ITEMS
    )
    st.markdown(f"""
    <div class="sn1-header">
        {logo_html}
        <span class="sn1-wordmark">Media Rights Intelligence</span>
        <nav class="sn1-nav">{nav_links}</nav>
    </div>
    <div class="sn1-rule"></div>
    """, unsafe_allow_html=True)


def page_setup(
    active: str = "home",
    title: str = "SN1 — Media Rights Intelligence",
    layout: str = "wide",
) -> None:
    """
    Call at the top of each sub-page.
    Does NOT call st.set_page_config — that is handled by app.py via st.navigation().
    """
    ensure_share_auth()
    inject_styles()
    db.init_db()
    render_header(active)


# ── Reusable UI components ────────────────────────────────────────────────────

def entity_type_badge(entity_type: str) -> str:
    meta = ENTITY_TYPE_META.get(entity_type, ENTITY_TYPE_META["other"])
    color = meta["color"]
    label = meta["label"]
    return (
        f'<span class="entity-card-type" style="background:{color}">{label}</span>'
    )


def entity_card_html(entity: dict, show_badge: bool = True) -> str:
    etype  = entity.get("entity_type", "other")
    meta   = ENTITY_TYPE_META.get(etype, ENTITY_TYPE_META["other"])
    color  = meta["color"]
    label  = meta["label"]
    name   = entity.get("canonical_name", "")
    docs   = entity.get("doc_count", 0) or 0
    snips  = entity.get("snip_count", 0) or 0
    last   = (entity.get("last_updated") or "")[:10] or "—"
    total  = (docs or 0) + (snips or 0)
    counts = f"{docs} doc{'s' if docs!=1 else ''}"
    if snips:
        counts += f" · {snips} snippet{'s' if snips!=1 else ''}"
    if total == 0:
        counts = "No entries yet"
    # Build as a compact single-line string — blank lines inside a <div> cause
    # Streamlit's markdown renderer to switch context and escape inner HTML tags.
    badge_html = (
        f'<span class="entity-card-type" style="background:{color}">{label}</span>'
        if show_badge else ""
    )
    return (
        f'<div class="entity-card">{badge_html}'
        f'<div class="entity-card-name">{name}</div>'
        f'<div class="entity-card-stats">{counts}</div>'
        f'<div class="entity-card-updated">Updated {last}</div>'
        f'</div>'
    )


def section_title(text: str) -> None:
    st.markdown(f'<div class="section-title">{text}</div>', unsafe_allow_html=True)


def stats_strip(stats: dict) -> None:
    items = [
        (stats.get("total", 0),     "Entries"),
        (stats.get("documents", 0), "Documents"),
        (stats.get("snippets", 0),  "Snippets"),
        (stats.get("entities", 0),  "Entities"),
    ]
    parts = "".join(
        f'<div class="stats-item"><div class="stats-num">{n}</div>'
        f'<div class="stats-label">{lbl}</div></div>'
        for n, lbl in items
    )
    st.markdown(f'<div class="stats-strip">{parts}</div>', unsafe_allow_html=True)


def reliability_badge_html(level: str) -> str:
    """Return an inline HTML reliability badge for the given level."""
    cls = {"confirmed": "rel-confirmed", "reported": "rel-reported", "rumoured": "rel-rumoured"}.get(
        level, "rel-reported"
    )
    return f'<span class="rel-badge {cls}">{level}</span>'


_DOWNLOAD_MIME: dict[str, str] = {
    ".pdf":  "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
}


def download_button_for_entry(entry: dict, key_suffix: str = "") -> None:
    """
    Render a download button for a document entry's source file.
    Falls back to a muted notice if the file is missing — never errors.
    """
    fp = (entry.get("file_path") or "").strip()
    if not fp:
        st.caption("Source file not available.")
        return
    p = Path(fp)
    if not p.exists():
        st.caption("Source file not available on this machine.")
        return
    try:
        data = p.read_bytes()
    except OSError:
        st.caption("Source file could not be read.")
        return
    ext  = p.suffix.lower()
    mime = _DOWNLOAD_MIME.get(ext, "application/octet-stream")
    label = ext.upper().lstrip(".") if ext else "FILE"
    st.download_button(
        label=f"⬇ Download {label}",
        data=data,
        file_name=p.name,
        mime=mime,
        key=f"dl_{entry.get('id', 'x')}_{key_suffix}",
    )

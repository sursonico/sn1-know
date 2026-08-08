"""Browse & Filter page — /browse"""
import pandas as pd
import streamlit as st
from kb.ui import page_setup, section_title, reliability_badge_html, download_button_for_entry
from kb.db import get_all_entries

page_setup("browse", title="Browse — SN1 Knowledge Base")

_REL_COLORS = {
    "confirmed": "#E8F4EC",
    "reported":  "#FBF1DC",
    "rumoured":  "#EEF0F4",
}


@st.cache_data(ttl=30)
def load_df() -> pd.DataFrame:
    rows = get_all_entries()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Fill blanks only in text columns so numeric dtypes (e.g., float) remain valid.
    text_cols = [
        c for c in df.columns
        if pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])
    ]
    if text_cols:
        df[text_cols] = df[text_cols].fillna("")
    if "reliability" not in df.columns:
        df["reliability"] = "reported"
    else:
        df["reliability"] = df["reliability"].fillna("reported")
    return df


def _tags(df, col):
    seen: set[str] = set()
    for v in df.get(col, pd.Series(dtype=str)):
        for t in str(v).split(","):
            t = t.strip()
            if t:
                seen.add(t)
    return sorted(seen)


section_title("Browse documents & notes")
df = load_df()

c1, c2, c3, c4, c5, c6 = st.columns([2.2, 1.1, 1.8, 1.4, 1.6, 1.4])
search       = c1.text_input("Search", placeholder="Any field…", label_visibility="hidden")
etype        = c2.selectbox("Type", ["All","Documents","Snippets"], label_visibility="hidden")
leagues      = c3.multiselect("Sport/League", _tags(df,"sport_tags"), label_visibility="hidden", placeholder="Sport / League")
period       = c4.selectbox("Period", ["All"]+sorted({str(v) for v in df.get("entry_date",[]) if v}), label_visibility="hidden")
doctype      = c5.selectbox("Doc type", ["All"]+sorted({str(v) for v in df.get("doc_type",[]) if v}), label_visibility="hidden")
reliability  = c6.selectbox("Reliability", ["All","confirmed","reported","rumoured"], label_visibility="hidden")

mask = pd.Series([True]*len(df), index=df.index)
if search:
    q = search.lower()
    mask &= df.apply(lambda r: any(q in str(v).lower() for v in r), axis=1)
if etype == "Documents": mask &= df["entry_type"]=="document"
elif etype == "Snippets": mask &= df["entry_type"]=="snippet"
if leagues: mask &= df["sport_tags"].apply(lambda v: any(lg in str(v) for lg in leagues))
if period != "All": mask &= df["entry_date"]==period
if doctype != "All": mask &= df["doc_type"]==doctype
if reliability != "All": mask &= df["reliability"]==reliability
filtered = df[mask].reset_index(drop=True)

n_d = int((filtered["entry_type"]=="document").sum()) if "entry_type" in filtered else 0
n_s = int((filtered["entry_type"]=="snippet").sum())  if "entry_type" in filtered else 0
st.caption(f"Showing {len(filtered)} of {len(df)} entries — {n_d} documents, {n_s} snippets")

cols = ["source","entry_type","reliability","file_type","entry_date","doc_type","sport_tags","org_tags","market_tags","summary","topic_tags"]
st.dataframe(
    filtered[[c for c in cols if c in filtered.columns]],
    use_container_width=True, hide_index=True,
    column_config={
        "source":      st.column_config.TextColumn("Source",        width="medium"),
        "entry_type":  st.column_config.TextColumn("Type",          width="small"),
        "reliability": st.column_config.TextColumn("Reliability",   width="small"),
        "file_type":   st.column_config.TextColumn("Format",        width="small"),
        "entry_date":  st.column_config.TextColumn("Date / Period", width="small"),
        "doc_type":    st.column_config.TextColumn("Doc Type",      width="medium"),
        "sport_tags":  st.column_config.TextColumn("Sport/League",  width="medium"),
        "org_tags":    st.column_config.TextColumn("Organisations", width="medium"),
        "market_tags": st.column_config.TextColumn("Markets",       width="medium"),
        "summary":     st.column_config.TextColumn("Summary",       width="large"),
        "topic_tags":  st.column_config.TextColumn("Topics",        width="large"),
    },
)

# ── Download source files ─────────────────────────────────────────────────────
if "entry_type" in filtered.columns:
    doc_rows = filtered[filtered["entry_type"] == "document"]
    if not doc_rows.empty:
        n_docs = len(doc_rows)
        with st.expander(f"⬇ Download source file ({n_docs} document{'s' if n_docs != 1 else ''} in current filter)"):
            source_opts = [r["source"] for _, r in doc_rows.iterrows() if r.get("source")]
            if source_opts:
                sel_source = st.selectbox(
                    "Select document",
                    source_opts,
                    key="browse_dl_select",
                    label_visibility="collapsed",
                )
                sel_row = doc_rows[doc_rows["source"] == sel_source]
                if not sel_row.empty:
                    download_button_for_entry(dict(sel_row.iloc[0]), key_suffix="browse")

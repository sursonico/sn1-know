"""Browse & Filter page — /browse"""
import pandas as pd
import streamlit as st
from config import DELETED_RETENTION_DAYS
from kb.ui import (
    page_setup, section_title, reliability_badge_html, download_button_for_entry,
    open_file_dialog,
)
from kb.db import get_all_entries, get_delete_impact, soft_delete_entry

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


def _reset_row_actions() -> None:
    """Bump the editor key so the 📄 / 🗑 checkboxes all come back unticked."""
    st.session_state["browse_editor_nonce"] = st.session_state.get("browse_editor_nonce", 0) + 1


@st.dialog("Delete entry")
def confirm_delete(entry_id: int) -> None:
    """Name the file, spell out the cascade, and require an explicit confirmation."""
    impact = get_delete_impact(entry_id)
    if not impact:
        st.warning("That entry has already been deleted.")
        if st.button("Close", key="del_gone"):
            _reset_row_actions()
            st.rerun()
        return

    entry = impact["entry"]
    label = "note" if entry.get("entry_type") == "snippet" else "document"
    st.markdown(f"Delete this {label}?")
    st.markdown(f"### {entry.get('source', '(untitled)')}")

    meta_bits = [b for b in (
        entry.get("file_type", ""), entry.get("doc_type", ""), entry.get("entry_date", "")
    ) if b]
    if meta_bits:
        st.caption(" · ".join(meta_bits))

    also = [f"{impact['chunks']} indexed page{'s' if impact['chunks'] != 1 else ''}"]
    if impact["deals"]:
        also.append(f"{impact['deals']} deal row{'s' if impact['deals'] != 1 else ''} extracted from it")
    if impact["entities"]:
        names = ", ".join(e["canonical_name"] for e in impact["entities"])
        also.append(f"{len(impact['entities'])} proposed entity/entities with no other source ({names})")
    st.markdown("**Also hidden:** " + "; ".join(also) + ".")

    st.info(
        f"This is a soft delete — the entry disappears from Browse and is excluded from "
        f"Ask, but the record and the original file are kept. Restore it from "
        f"**Admin → Recently Deleted** within {DELETED_RETENTION_DAYS} days.",
        icon="↩",
    )

    c_cancel, c_delete = st.columns(2)
    if c_cancel.button("Cancel", key="del_cancel", use_container_width=True):
        _reset_row_actions()
        st.rerun()
    if c_delete.button("Delete", key="del_confirm", type="primary", use_container_width=True):
        result = soft_delete_entry(entry_id)
        _reset_row_actions()
        load_df.clear()
        st.session_state["browse_flash"] = (entry.get("source", ""), result)
        st.rerun()


def _tags(df, col):
    seen: set[str] = set()
    for v in df.get(col, pd.Series(dtype=str)):
        for t in str(v).split(","):
            t = t.strip()
            if t:
                seen.add(t)
    return sorted(seen)


section_title("Browse documents & notes")

flash = st.session_state.pop("browse_flash", None)
if flash:
    name, result = flash
    extra = []
    if result.get("deals"):
        extra.append(f"{result['deals']} deal row{'s' if result['deals'] != 1 else ''}")
    if result.get("entities"):
        extra.append(f"{len(result['entities'])} proposed entity/entities")
    tail = f" — also hidden: {', '.join(extra)}." if extra else "."
    st.success(
        f"Deleted '{name}'{tail} Restore it from Admin → Recently Deleted "
        f"within {DELETED_RETENTION_DAYS} days."
    )

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

cols = ["source","validation_warning","entry_type","reliability","file_type","entry_date","doc_type","sport_tags","org_tags","market_tags","summary","topic_tags"]
show_cols = [c for c in cols if c in filtered.columns]

# Tick 📄 to view the original file, 🗑 to delete. The editor is read-only apart
# from those two columns; the key nonce resets the ticks after any action.
table = filtered[show_cols].copy()
table["📄"] = False   # trailing columns — to the right of every data column
table["🗑"] = False

edited = st.data_editor(
    table,
    use_container_width=True, hide_index=True, num_rows="fixed",
    disabled=show_cols,
    key=f"browse_editor_{st.session_state.get('browse_editor_nonce', 0)}",
    column_config={
        "source":      st.column_config.TextColumn("Source",        width="medium"),
        "validation_warning": st.column_config.TextColumn(
            "⚠ Review", width="medium",
            help="Post-ingest check: extraction looked thin relative to file size, "
                 "or a multi-page document linked far fewer entities than expected. "
                 "Advisory only — the entry was still ingested.",
        ),
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
        "📄":           st.column_config.CheckboxColumn("📄", width="small", help="Open the original file"),
        "🗑":           st.column_config.CheckboxColumn("🗑", width="small", help="Delete this entry"),
    },
)
st.caption(
    "Tick 📄 on a row to view or download the original file, or 🗑 to delete that "
    "entry — deletions ask for confirmation first."
)

if "id" in filtered.columns:
    opened = [i for i in edited.index if bool(edited.at[i, "📄"])]
    ticked = [i for i in edited.index if bool(edited.at[i, "🗑"])]
    if ticked:
        confirm_delete(int(filtered.at[ticked[0], "id"]))
    elif opened:
        row = dict(filtered.loc[opened[0]])
        if row.get("entry_type") == "document":
            open_file_dialog(row, on_close=_reset_row_actions)
        else:
            st.info("Logged notes have no source file — their full text is on the Ask page.")

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

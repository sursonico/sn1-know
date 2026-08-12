"""Global Ask page — /ask"""
import streamlit as st
from kb.ui import page_setup, section_title, download_button_for_entry
from kb.db import get_all_entries
from kb.retrieval import retrieve_and_answer
from config import DB_PATH

page_setup("ask", title="Ask — SN1 Knowledge Base")
section_title("Ask a question")
st.caption(
    "Stage 1 — FTS5 keyword search + Claude catalogue scan → selects relevant entries.  "
    "Stage 2 — reads those entries page by page → cited answer.  "
    "Follow-up questions reuse this session's context."
)

if "conversation" not in st.session_state:
    st.session_state.conversation = []

# Pre-fill from home page search bar (write to key before widget renders so
# the value survives subsequent reruns — avoids reset-to-empty on second render)
if "pending_question" in st.session_state:
    st.session_state["ask_question_input"] = st.session_state.pop("pending_question")

# Auto-run flag: set by gap-fill follow-up buttons so the focused question
# fires automatically without the user having to click Ask again.
_autorun = st.session_state.pop("ask_autorun", False)

question = st.text_area(
    "Your question",
    height=80,
    key="ask_question_input",
    placeholder='e.g. "What are the key UEFA Champions League rights deals for 2027 onwards?"',
)

ask_col, clear_col = st.columns([6, 1])
ask_btn   = ask_col.button("Ask", type="primary")
clear_btn = clear_col.button("Clear", type="secondary", disabled=not st.session_state.conversation)

if clear_btn:
    st.session_state.conversation = []
    st.rerun()

if (ask_btn or _autorun) and question.strip():
    history = st.session_state.conversation or None

    with st.spinner("Stage 1 — searching catalogue…"):
        result = retrieve_and_answer(question.strip(), DB_PATH, history)

    answer      = result["answer"]
    selected    = result["selected"]
    rationale   = result["rationale"]
    fts_ids     = result["fts_hit_ids"]
    is_fb       = result["is_fallback"]
    truncations = result.get("truncated_sources", [])
    s1_mode     = result.get("stage1_mode", "")
    s1_error    = result.get("stage1_error", "")
    s1_raw      = result.get("stage1_raw", "")
    entries     = get_all_entries()

    def _stage1_diagnostics() -> None:
        """Show what actually went wrong in Stage 1 rather than hiding it."""
        with st.expander("⚙ Stage 1 diagnostics — what went wrong", expanded=False):
            st.markdown(f"**Outcome:** `{s1_mode}` after {result.get('stage1_attempts', 1)} attempt(s)")
            if s1_error:
                st.markdown("**Error:**")
                st.code(s1_error, language="text")
            if s1_raw:
                st.markdown("**Raw Stage 1 response:**")
                st.code(s1_raw[:2000] or "(empty)", language="text")
            st.caption(
                "Stage 1 asks Claude to pick relevant entries from the catalogue as JSON. "
                "A malformed reply is retried once, then IDs are salvaged if possible; "
                "only if that fails does the app sweep the whole catalogue. "
                "The same detail is written to the server log."
            )

    if not selected:
        st.info(answer)
        if s1_error:
            st.warning("Stage 1 did not return a usable selection — details below.")
            _stage1_diagnostics()
    else:
        if is_fb:
            st.warning(
                f"Stage 1 catalogue scan failed — sweeping {len(selected)} entries and "
                "ranking pages by relevance instead. The answer may still be correct; "
                "expand the diagnostics to see the underlying error."
            )
            _stage1_diagnostics()
        else:
            if s1_error:
                st.info(
                    f"Stage 1 recovered from a malformed response (`{s1_mode}`) — "
                    "selection below is still usable."
                )
                _stage1_diagnostics()
            icons = {"document": "📄", "snippet": "📝"}
            lines = [f"- {icons.get(s['entry_type'],'📌')} `{s['cite']}`" for s in selected]
            st.success(
                f"**Selected {len(selected)} of {len(entries)} entries:**\n"
                + "\n".join(lines)
                + (f"\n\n*{rationale}*" if rationale else "")
                + (f"  *(FTS hits: {len(fts_ids)})*" if fts_ids else "")
            )
        st.markdown("### Answer")
        st.markdown(answer)

        # Download buttons for document sources cited in this answer
        doc_selected = [s for s in selected if s.get("entry_type") == "document"]
        if doc_selected:
            entries_by_id = {e["id"]: e for e in entries}
            with st.expander(f"⬇ Source files ({len(doc_selected)} document{'s' if len(doc_selected) != 1 else ''})"):
                for s in doc_selected:
                    entry = entries_by_id.get(s["id"])
                    if entry:
                        col_label, col_btn = st.columns([4, 1])
                        col_label.markdown(f"📄 `{s['cite']}`")
                        with col_btn:
                            download_button_for_entry(entry, key_suffix=f"ask_{s['id']}")

        # Gap-fill callout — surfaces when budget forced partial coverage of a source.
        # Each button auto-runs a focused follow-up scoped to that source so the user
        # doesn't have to manually reformulate the question.
        if truncations:
            with st.expander(
                f"⚠ {len(truncations)} source{'s' if len(truncations) != 1 else ''} partially loaded"
                " — click to fill gaps",
                expanded=True,
            ):
                for t in truncations:
                    gap_col, btn_col = st.columns([5, 2])
                    omitted = t["total"] - t["loaded"]
                    loaded_str = (
                        f"{t['loaded']} of {t['total']} pages loaded, {omitted} omitted"
                        if t["total"] > 0 else "not loaded"
                    )
                    gap_col.caption(f"**{t['cite']}** — {loaded_str}")
                    if btn_col.button(
                        "Run follow-up",
                        key=f"gap_{t['entry_id']}",
                        type="secondary",
                    ):
                        st.session_state["ask_question_input"] = (
                            f"From '{t['cite']}': {question.strip()}"
                        )
                        st.session_state["ask_autorun"] = True
                        st.rerun()

    st.session_state.conversation.append({"q": question.strip(), "a": answer})

if st.session_state.conversation:
    with st.expander(f"Conversation history ({len(st.session_state.conversation)} turns)"):
        for i, turn in enumerate(reversed(st.session_state.conversation), 1):
            st.markdown(f"**Q{i}:** {turn['q']}")
            st.markdown(f"**A:** {turn['a'][:300]}{'…' if len(turn['a'])>300 else ''}")
            st.divider()

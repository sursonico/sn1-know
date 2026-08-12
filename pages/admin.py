"""Entity admin page — /admin"""
import streamlit as st
from config import DELETED_RETENTION_DAYS
from kb.ui import page_setup, section_title, entity_type_badge, ENTITY_TYPE_META
from kb.db import (
    get_all_entities, get_proposed_entities, get_entity, get_entity_stats,
    update_entity, merge_entities, upsert_entity, index_entry,
    get_entries_for_entity, get_deleted_entries, restore_entry, purge_entry,
    get_purgeable_entry_ids, purge_expired_deleted,
)
from seed_entities import seed

page_setup("admin", title="Admin — SN1 Knowledge Base")
section_title("Entity management")

with st.expander("Bootstrap canonical entities", expanded=False):
    st.caption(
        "Run this once on a new database to load the canonical entity list. "
        "It is safe to re-run because inserts are idempotent."
    )
    if st.button("Seed canonical entities", type="primary"):
        n = seed(verbose=False)
        st.success(f"Seeded {n} canonical entities.")
        st.rerun()

_deleted_entries = get_deleted_entries()
_deleted_label = (
    f"Recently Deleted ({len(_deleted_entries)})" if _deleted_entries else "Recently Deleted"
)

tab_active, tab_proposed, tab_cleanup, tab_create, tab_deleted = st.tabs([
    "Active Entities", "Proposed (Pending Review)", "Cleanup — No Entries", "Create New",
    _deleted_label,
])

# ── Active entities ───────────────────────────────────────────────────────────
with tab_active:
    all_stats = get_entity_stats()
    if not all_stats:
        st.info("No entities yet. Run `python seed_entities.py` to populate.")
    else:
        search = st.text_input("Search entities", placeholder="Filter by name…", key="admin_search")
        filtered = [e for e in all_stats if not search or search.lower() in e["canonical_name"].lower()]
        st.caption(f"{len(filtered)} entities")

        for entity in filtered:
            eid   = entity["id"]
            name  = entity["canonical_name"]
            etype = entity["entity_type"]
            badge = entity_type_badge(entity.get("entity_type","other"))
            docs  = entity.get("doc_count") or 0
            snips = entity.get("snip_count") or 0

            with st.expander(f"{name}  —  {docs}d · {snips}s"):
                st.markdown(f'{badge}', unsafe_allow_html=True)

                col_n, col_t = st.columns(2)
                new_name = col_n.text_input("Canonical name", value=name, key=f"name_{eid}")
                new_type = col_t.selectbox(
                    "Type",
                    list(ENTITY_TYPE_META.keys()),
                    index=list(ENTITY_TYPE_META.keys()).index(etype),
                    key=f"type_{eid}",
                )
                new_aliases = st.text_input(
                    "Aliases (comma-separated)",
                    value=entity.get("aliases",""),
                    key=f"aliases_{eid}",
                )
                is_featured = st.checkbox(
                    "Show on Home page",
                    value=bool(entity.get("is_featured", 0)),
                    key=f"feat_{eid}",
                    help="Featured entities appear on the Home page. All entities remain reachable via Browse and Ask.",
                )

                save_col, merge_col = st.columns(2)
                if save_col.button("Save changes", key=f"save_{eid}", type="primary"):
                    update_entity(eid, canonical_name=new_name, entity_type=new_type, aliases=new_aliases, is_featured=int(is_featured))
                    st.success(f"Updated '{new_name}'")
                    st.rerun()

                # Merge into another entity
                other_names = [e["canonical_name"] for e in all_stats if e["id"] != eid]
                merge_target = merge_col.selectbox(
                    "Merge INTO →", ["(don't merge)"] + other_names, key=f"merge_{eid}"
                )
                if merge_col.button("Merge", key=f"do_merge_{eid}", type="secondary"):
                    if merge_target != "(don't merge)":
                        target_id = next(e["id"] for e in all_stats if e["canonical_name"] == merge_target)
                        merge_entities(keep_id=target_id, discard_id=eid)
                        st.success(f"Merged '{name}' → '{merge_target}'")
                        st.rerun()


# ── Proposed entities ─────────────────────────────────────────────────────────
with tab_proposed:
    proposed = get_proposed_entities()
    if not proposed:
        st.info("No proposed entities awaiting review.")
    else:
        st.caption(
            f"{len(proposed)} proposed entity/entities were flagged during ingestion as "
            "potentially new. Review, accept (promotes to active), or reject each one."
        )
        for entity in proposed:
            eid  = entity["id"]
            name = entity["canonical_name"]
            entries_linked = get_entries_for_entity(eid)

            with st.expander(f"⚡ {name}  ({entity['entity_type']})  — {len(entries_linked)} entry linked"):
                st.write(f"Linked to: {', '.join(e['source'][:40] for e in entries_linked)}")
                all_active = get_all_entities(include_proposed=False)
                merge_target = st.selectbox(
                    "Accept as-is or merge into existing",
                    ["Accept as new entity"] + [e["canonical_name"] for e in all_active],
                    key=f"p_merge_{eid}",
                )
                a_col, r_col = st.columns(2)
                if a_col.button("Accept", key=f"p_accept_{eid}", type="primary"):
                    if merge_target == "Accept as new entity":
                        update_entity(eid, is_proposed=0)
                        st.success(f"Accepted '{name}' as a new entity.")
                    else:
                        target = next(e for e in all_active if e["canonical_name"] == merge_target)
                        merge_entities(keep_id=target["id"], discard_id=eid)
                        st.success(f"Merged '{name}' → '{merge_target}'")
                    st.rerun()
                if r_col.button("Reject", key=f"p_reject_{eid}", type="secondary"):
                    merge_entities(keep_id=eid, discard_id=eid)   # no-op links
                    from kb.db import _conn, DB_PATH
                    with _conn(DB_PATH) as con:
                        con.execute("DELETE FROM entry_entities WHERE entity_id=?", (eid,))
                        con.execute("DELETE FROM entities WHERE id=?", (eid,))
                    st.warning(f"Rejected and deleted '{name}'")
                    st.rerun()


# ── Cleanup: entities with zero entries ──────────────────────────────────────
with tab_cleanup:
    section_title("Entities with no linked entries")
    st.caption(
        "These entities exist in the registry but are not linked to any document or snippet. "
        "They were likely seeded from the canonical list or proposed during ingestion but never "
        "matched to content. Merge into an existing entity or delete."
    )
    all_stats = get_entity_stats()
    empty_entities = [e for e in get_all_entities() if not any(
        s["id"] == e["id"] and (s.get("total_count") or 0) > 0
        for s in all_stats
    )]
    if not empty_entities:
        st.success("No content-less entities — the registry is clean.")
    else:
        st.write(f"**{len(empty_entities)} entities have no linked content:**")
        active_names = [e["canonical_name"] for e in get_all_entities()]
        for entity in empty_entities:
            eid  = entity["id"]
            name = entity["canonical_name"]
            with st.expander(f"{name}  ({entity['entity_type']})"):
                col_m, col_d = st.columns(2)
                merge_target = col_m.selectbox(
                    "Merge into",
                    ["(no merge)"] + [n for n in active_names if n != name],
                    key=f"cu_merge_{eid}",
                )
                if col_m.button("Merge", key=f"cu_do_merge_{eid}", type="secondary"):
                    if merge_target != "(no merge)":
                        target = next(e for e in get_all_entities() if e["canonical_name"] == merge_target)
                        merge_entities(keep_id=target["id"], discard_id=eid)
                        st.success(f"Merged '{name}' → '{merge_target}'")
                        st.rerun()
                if col_d.button("Delete", key=f"cu_delete_{eid}", type="secondary"):
                    from kb.db import _conn, DB_PATH
                    with _conn(DB_PATH) as con:
                        con.execute("DELETE FROM entry_entities WHERE entity_id=?", (eid,))
                        con.execute("DELETE FROM entities WHERE id=?", (eid,))
                    st.warning(f"Deleted '{name}'")
                    st.rerun()


# ── Create new entity ─────────────────────────────────────────────────────────
with tab_create:
    section_title("Create a new entity")
    new_name    = st.text_input("Canonical name *", placeholder="e.g. UEFA Europa Conference League")
    new_type    = st.selectbox("Type *", list(ENTITY_TYPE_META.keys()))
    new_aliases = st.text_input("Aliases (comma-separated)", placeholder="e.g. UECL, Conference League")
    if st.button("Create entity", type="primary") and new_name.strip():
        eid = upsert_entity(new_name.strip(), new_type, new_aliases.strip(), is_proposed=0)
        st.success(f"Created entity '{new_name}' (id={eid})")
        st.rerun()


# ── Recently deleted: restore or purge ────────────────────────────────────────
with tab_deleted:
    section_title("Recently deleted entries")
    st.caption(
        f"Entries deleted from Browse are hidden from Browse, Ask and every entity hub, "
        f"but the record — plus any deal rows and proposed entities extracted from it — "
        f"is kept and can be restored here. Nothing is removed automatically; after "
        f"{DELETED_RETENTION_DAYS} days an entry becomes eligible for permanent purge. "
        f"Purging never deletes the original file from disk."
    )

    if not _deleted_entries:
        st.success("Nothing in the recycle bin.")
    else:
        expired_ids = set(get_purgeable_entry_ids(DELETED_RETENTION_DAYS))
        if expired_ids:
            st.warning(
                f"{len(expired_ids)} entr{'y is' if len(expired_ids) == 1 else 'ies are'} "
                f"past the {DELETED_RETENTION_DAYS}-day window and eligible for permanent purge."
            )
            if st.button(
                f"Purge all {len(expired_ids)} expired permanently",
                key="purge_expired",
                type="secondary",
            ):
                n = purge_expired_deleted(DELETED_RETENTION_DAYS)
                st.success(f"Permanently purged {n} entr{'y' if n == 1 else 'ies'}.")
                st.rerun()

        st.write(f"**{len(_deleted_entries)} deleted entr{'y' if len(_deleted_entries) == 1 else 'ies'}:**")
        for entry in _deleted_entries:
            eid       = entry["id"]
            source    = entry.get("source") or "(untitled)"
            days      = entry.get("days_deleted") or 0
            when      = (entry.get("deleted_at") or "")[:10]
            expired   = eid in expired_ids
            age_label = "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} ago"
            marker    = "⚠ " if expired else ""

            with st.expander(f"{marker}{source}  —  deleted {age_label}"):
                bits = [
                    f"Type: {entry.get('entry_type', '?')}",
                    f"Deleted: {when}",
                ]
                if entry.get("entry_date"):
                    bits.append(f"Date: {entry['entry_date']}")
                if entry.get("held_deals"):
                    bits.append(f"{entry['held_deals']} deal row(s) held")
                if entry.get("held_entities"):
                    bits.append(f"{entry['held_entities']} proposed entity/entities held")
                st.caption("  ·  ".join(bits))
                if entry.get("summary"):
                    st.write(entry["summary"][:400])

                if expired:
                    st.caption(
                        f"Past the {DELETED_RETENTION_DAYS}-day retention window — eligible for purge."
                    )
                else:
                    st.caption(
                        f"Recoverable for {max(DELETED_RETENTION_DAYS - days, 0)} more day(s)."
                    )

                col_r, col_p = st.columns(2)
                if col_r.button("Restore", key=f"restore_{eid}", type="primary"):
                    if restore_entry(eid):
                        st.success(f"Restored '{source}' — it is back in Browse and Ask.")
                    else:
                        st.warning("That entry is no longer in the recycle bin.")
                    st.rerun()

                confirm_key = f"purge_confirm_{eid}"
                confirmed = col_p.checkbox(
                    "Confirm permanent purge", key=confirm_key,
                    help="Removes the record, its pages, index rows and held deals for good. "
                         "The source file on disk is not touched.",
                )
                if col_p.button(
                    "Purge permanently", key=f"purge_{eid}",
                    type="secondary", disabled=not confirmed,
                ):
                    if purge_entry(eid):
                        st.success(f"Permanently purged '{source}'.")
                    else:
                        st.warning("That entry is no longer in the recycle bin.")
                    st.rerun()

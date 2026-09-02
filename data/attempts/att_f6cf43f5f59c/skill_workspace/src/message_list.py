"""Conversation list state + editable UI.

The conversation is rendered as an extendable list of rows, each made of a
``st.text_area`` (large free-form message) and a ``st.selectbox`` choosing the
speaker.  Row values live in ``st.session_state`` under namespaced keys so the
user's edits survive Streamlit reruns.  ``frontend.py`` calls :func:`render`
and reads :func:`to_api_messages` on SUBMIT.
"""

from __future__ import annotations

import streamlit as st
from typing import List

#: Role labels shown in the dropdown -> values accepted by the OpenAI API.
ROLE_LABELS: List[str] = ["LLM", "User"]
ROLE_MAP = {"LLM": "assistant", "User": "user"}

_TEXT_PREFIX = "msg_text"
_ROLE_PREFIX = "msg_role"


def _row_count() -> int:
    return st.session_state.get("row_count", 0)


def _set_row_count(n: int) -> None:
    st.session_state["row_count"] = n


def load_or_init() -> int:
    """Seed the session with a single empty row if this is a fresh session.

    Returns the current number of rows.
    """
    if "row_count" not in st.session_state:
        st.session_state["row_count"] = 1
        st.session_state[f"{_TEXT_PREFIX}_0"] = ""
        st.session_state[f"{_ROLE_PREFIX}_0"] = "User"
    return _row_count()


def add_row() -> None:
    """Append a new empty row (an unbounded number is allowed)."""
    idx = _row_count()
    st.session_state[f"{_TEXT_PREFIX}_{idx}"] = ""
    st.session_state[f"{_ROLE_PREFIX}_{idx}"] = "User"
    _set_row_count(idx + 1)


def remove_row() -> None:
    """Drop the last row; keep at least one."""
    if _row_count() <= 1:
        return
    idx = _row_count() - 1
    for key in (f"{_TEXT_PREFIX}_{idx}", f"{_ROLE_PREFIX}_{idx}"):
        st.session_state.pop(key, None)
    _set_row_count(idx)


def render() -> None:
    """Render the editable conversation list into the current Streamlit scope."""
    n = load_or_init()
    for i in range(n):
        c_left, c_role, c_del = st.columns([8, 2, 1], vertical_alignment="bottom")
        with c_left:
            st.text_area(
                "Message" if i == 0 else " ",
                key=f"{_TEXT_PREFIX}_{i}",
                height=110,
                label_visibility="collapsed",
                placeholder="Type a message…",
            )
        with c_role:
            st.selectbox(
                " " if i == 0 else "  ",
                ROLE_LABELS,
                key=f"{_ROLE_PREFIX}_{i}",
                label_visibility="collapsed",
            )
        with c_del:
            if n > 1:
                if st.button("✕", key=f"del_{i}", help="Delete this message"):
                    _delete_row(i)
                    st.rerun()

    action_cols = st.columns([3, 3])
    with action_cols[0]:
        if st.button("＋ Add message", use_container_width=True):
            add_row()
            st.rerun()


def _delete_row(i: int) -> None:
    """Remove row *i* and shift the remaining rows' state down."""
    n = _row_count()
    for key in (f"{_TEXT_PREFIX}_{i}", f"{_ROLE_PREFIX}_{i}"):
        st.session_state.pop(key, None)
    for j in range(i + 1, n):
        st.session_state[f"{_TEXT_PREFIX}_{j - 1}"] = st.session_state.pop(
            f"{_TEXT_PREFIX}_{j}"
        )
        st.session_state[f"{_ROLE_PREFIX}_{j - 1}"] = st.session_state.pop(
            f"{_ROLE_PREFIX}_{j}"
        )
    _set_row_count(n - 1)


def to_api_messages() -> List[dict]:
    """Exported messages, skipping empty rows (OpenAI refuses blank content)."""
    from _helpers import chat_messages

    return chat_messages(
        [
            {"role": ROLE_MAP[st.session_state[f"{_ROLE_PREFIX}_{i}"]] , "content": st.session_state[f"{_TEXT_PREFIX}_{i}"]}  # noqa: E203
            for i in range(_row_count())
        ]
    )
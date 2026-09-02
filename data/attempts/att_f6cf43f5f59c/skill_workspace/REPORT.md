# REPORT — OpenAI Response Comparator

## 1. What was built

| Requirement | Location | Status |
| --- | --- | --- |
| Conversation as an unbounded list of `{text, speaker}` rows | `src/message_list.py` | ✅ boots with 1 empty row, ＋ Add message appends, per-row delete |
| Text field per row accepting large messages | Streamlit `st.text_area` (key-bound) | ✅ |
| Dropdown per row (`LLM` / `User`) | Streamlit `st.selectbox` | ✅ |
| Numeric field, values only `0..100` | `src/frontend.py` `st.number_input(min_value=0, max_value=100)` | ✅ |
| SUBMIT button | `src/frontend.py` | ✅ |
| Conversation sent to OpenAI API **X times in parallel** | `src/backend.py` (one thread per request) | ✅ |
| Responses shown as a list of expandable elements, labelled `Request N` | `src/frontend_render.py` (`st.expander`) | ✅ |
| **Token-by-token streaming** displayed in real time | `src/stream_handler.py` + polling redraw loop (`frontend_render`) | ✅ |
| Tailwind via CDN (not installed), styling in `src/styles.css` | `src/styles.css` + JS injection in `frontend.py` | ✅ |
| Mock LLM responses to avoid OpenAI key usage | `src/mock_llm.py` (default provider) | ✅ |

## 2. Offline / mock fallback (adaptation requirement)

**Constraint:** the sandbox has limited network and must not depend on private
credentials. The app therefore defaults to a **fully offline, deterministic
mock provider**; a real OpenAI key is never required.

- Every parallel request answers from `MockLLMStream` (`src/mock_llm.py`).
- The mock mirrors the real OpenAI `chat.completions` streaming contract:
  - replies are yielded **word-by-word** with a small per-token delay
    (`TOKEN_DELAY_SECONDS`, default 20 ms) → exercises the UI's real-time path,
  - a stop sentinel (`<|mock_stop|>`, defined in `backend.py`) ends the stream,
  - the reply is **deterministic** for a given (conversation, request-index)
    pair (seeded SHA-256 of the conversation + index) but **varies across
    request indices**, so a run of N parallel requests produces N visibly
    different answers — just like sampling the real API.
- The real OpenAI SSE path is implemented and switchable via
  `USE_REAL_OPENAI=1` + `OPENAI_API_KEY` (docs in `REPORT`/`README`), but the
  default is the mock, so the app is reproducible anywhere with no cost.
- **Tailwind fallback:** `src/styles.css` sources utilities from the Tailwind
  CDN (per spec — nothing installed) and also contains plain-CSS fallback rules
  so the app looks acceptable even when the CDN is unreachable.

## 3. Verified evidence

- `python tests/test_core.py` → **6/6 passed**
  (mock determinism & variance, stop-sentinel handling, N-parallel streaming
  to completion, error surfacing, message filtering of blank rows).
- `python tests/test_app_flow.py` → **3/3 passed** using the Streamlit
  `AppTest` harness end-to-end: app boots with 1 row → type → set count →
  click SUBMIT → responsively asserts 3 `Request N` expanders each containing
  the full streamed answer; also verifies ＋Add message grows the list and
  count=0 produces no cards.
- `streamlit run src/frontend.py --server.headless true` served `HTTP 200` on
  `/` with a healthy app bundle (log captured in this run).

## 4. Notes & decisions

- **Threads, not async:** Streamlit is a synchronous script model, so one
  daemon thread per request is the cleanest way to fan out; a thread-safe
  `StreamSlot` (lock + event) lets the UI poll and redraw at ~20 Hz.
- **Widgets are key-bound into `st.session_state`**, so user edits survive
  reruns and deletion/append of rows stays consistent.
- Blank rows are dropped before hitting the provider (OpenAI rejects empty
  content).
- Page re-injection of the CDN script is guarded by a module flag to avoid
  duplicate styling on rerun.
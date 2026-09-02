# ⚖️ OpenAI Response Comparator

A Streamlit app that sends the **same** conversation to the OpenAI API **N times in
parallel** and shows, side by side in expandable cards, how the model's answers
diverge — streamed **token-by-token** in real time.

```
streamlit run src/frontend.py
```

## What it does

1. **Conversation editor** — an unbounded list of rows, each with a large text
   area (a potentially long message) and a dropdown marking the speaker
   (`LLM` / `User`). The app boots with one empty row; **＋ Add message** grows
   the list as far as you like. Handled in `src/message_list.py`.
2. **Parallelism field** — a numeric input restricted to `0..100`
   (`src/frontend.py`). This is X.
3. **SUBMIT** — the full conversation is dispatched **X times in parallel**.
   Each request runs in its own worker thread (`src/backend.py`).
4. **Live response cards** — one expandable element per request (`Request 1`,
   `Request 2`, …). Because the provider returns a *stream*, tokens are rendered
   into the cards as they arrive — no waiting for the full reply
   (`src/frontend_render.py`, `src/stream_handler.py`).
5. **Styling** — Tailwind utility classes loaded from the **CDN** (never
   installed/bundled) plus local overrides in `src/styles.css`.

## Provider: real API vs offline mock

The app never *requires* an OpenAI key.

| Mode                  | When                                               | Source                              |
| --------------------- | -------------------------------------------------- | ----------------------------------- |
| Real OpenAI streaming | `USE_REAL_OPENAI=1` **and** `OPENAI_API_KEY` set    | `openai` SDK, `stream=True` (SSE)   |
| **Offline mock**      | default — no credentials needed                     | `src/mock_llm.py` (deterministic)   |

The mock imitates the OpenAI SSE stream closely enough to exercise the full
real-time rendering path: it yields the reply as space-delimited word tokens
with a small per-token delay, emits a stop sentinel, and is *deterministic per
request index yet varied across indices* — so repeated runs of the same query
visibly differ, exactly like the real API. See `REPORT.md` for design notes.

## Layout

```
src/
  frontend.py         Streamlit entry point (layout, 0–100 field, SUBMIT)
  message_list.py     Conversation-list state + editable rows (text + role)
  backend.py          N parallel requests; real-API / mock provider switch
  frontend_render.py  Expandable response cards + live token rendering
  stream_handler.py   Thread-safe streaming primitives (StreamSlot/Manager)
  mock_llm.py         Deterministic offline token generator
  styles.css          Tailwind-CDN styling + offline fallback rules
tests/
  test_core.py        Unit tests (mock, streams, backend, helpers)
  test_app_flow.py    End-to-end AppTest: edit → SUBMIT → streamed cards
```

## Validation

```
python tests/test_core.py      # 6/6 unit tests pass
python tests/test_app_flow.py  # 3/3 UI flow tests pass (real Streamlit)
```
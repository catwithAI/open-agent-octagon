"""Mock LLM streaming responses.

The OpenAI comparison app is designed to be fully usable offline: instead of
burning API credits (or failing when no ``OPENAI_API_KEY`` is set), every
parallel request is answered by a deterministic, token-by-token mock stream
that mimics the OpenAI ``chat.completions`` SSE behaviour closely enough to
exercise the frontend's real-time rendering path.
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Iterator, List

#: Artificial latency between tokens; approximates a fast real stream.
TOKEN_DELAY_SECONDS = 0.02

#: Templates the mock uses to build a plausible assistant reply.
_TEMPLATES: List[str] = [
    "Here is my analysis of your request.\n\n{points}\n\nIn summary, {summary}",
    "Thanks for the prompt. Reading it carefully:\n\n{points}\n\nOverall, {summary}",
    "An interesting question. Let me break it down:\n\n{points}\n\nTo conclude, {summary}",
    "I've reviewed what you sent me.\n\n{points}\n\nMy takeaway: {summary}",
    "Happy to help with that.\n\n{points}\n\nSo, in short, {summary}",
]

_SUMMARIES: List[str] = [
    "the proposal is feasible with some caveats around timing and scope.",
    "the answer depends heavily on the exact inputs you provide.",
    "a pragmatic middle ground would satisfy most of your constraints.",
    "your intuition points in the right direction, with one adjustment.",
    "the trade-offs are manageable, but careful validation is worthwhile.",
    "the core idea holds up; the details will need iteration.",
    "there are a couple of edge cases worth watching before you commit.",
    "the response is positive — just keep the requirements explicit.",
]

_POINTS: List[str] = [
    "- The key assumption underneath your question is testable.",
    "- Several prior approaches have converged on a similar conclusion.",
    "- The main risk is that the input space is larger than it first appears.",
    "- A simple baseline would get you 80% of the way there.",
    "- The ordering of steps matters more than the individual steps.",
    "- Precedence in similar systems suggests this is a known pattern.",
    "- The constraint can be relaxed without changing the overall result.",
    "- Reusing existing components would save a meaningful amount of work.",
]


class MockLLMStream:
    """Yields a simulated assistant completion token-by-token."""

    def __init__(self, conversation: List[dict], request_index: int = 0) -> None:
        """Build a mock stream for *conversation* (OpenAI-style message dicts).

        Arguments:
            conversation: OpenAI-style ``[{"role": ...,
                "content": ...}, ...]`` message list.
            request_index: per-request index (0-based); the response is derived
                from it deterministically to imitate API nondeterminism across
                runs of the same query.
        """
        self.conversation = conversation
        self.request_index = request_index
        self.message_count = len(conversation)

    # -- building blocks ----------------------------------------------------
    def _seed(self) -> int:
        """Stable, reproducible seed for this conversation + request index."""
        blob = repr(self.conversation) + f"::mock-request-{self.request_index}"
        return int(hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16], 16)

    def _build_content(self) -> str:
        rng = random.Random(self._seed())
        user_messages = [m["content"] for m in self.conversation if m["role"] == "user"]
        last_user = user_messages[-1] if user_messages else "(no user message supplied)"

        template = rng.choice(_TEMPLATES)
        summary = rng.choice(_SUMMARIES)
        # Vary how many supporting points each reply contains so the UI shows
        # that responses genuinely differ from one request to the next.
        n_points = 2 + int(self._seed() % 4)
        points = "\n".join(rng.sample(_POINTS, k=n_points))

        tail = (
            f"\n\nYou asked: “{last_user[:160]}”. "
            f"(You are viewing simulated reply {self.request_index + 1} of this mock run.)"
        )
        return template.format(points=points, summary=summary) + tail

    # -- streaming ----------------------------------------------------------
    def tokens(self) -> Iterator[str]:
        """Yield the reply as space-delimited word tokens, then the stop token."""
        content = self._build_content()
        for word in content.split(" "):
            time.sleep(TOKEN_DELAY_SECONDS)
            yield word + " "
        yield "<|mock_stop|>"


def stream_tokens(conversation: List[dict], request_index: int = 0) -> Iterator[str]:
    """Convenience wrapper: token stream (with stop sentinel) for *conversation*."""
    return MockLLMStream(conversation, request_index=request_index).tokens()
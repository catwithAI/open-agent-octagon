from __future__ import annotations

import random
from typing import Any

from ..hashing import variant_content_hash
from ..models import Applicability, MutationInput, VariantResult, VariantSpec
from .common import normalized_prompt, prompt_spans, verify_protected_values


def _blocks(source: MutationInput) -> list[dict[str, Any]] | None:
    value = source.context.get("instruction_blocks")
    if not isinstance(value, list) or len(value) < 2:
        return None
    if not all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in value):
        return None
    return value


class InstructionPositionMutator:
    id = "instruction-position"
    version = "1"
    invariants = ("instruction_block_identity", "protected_spans", "context_identity")
    modalities = ("text",)

    def check(self, source: MutationInput, spec: VariantSpec) -> Applicability:
        blocks = _blocks(source)
        if blocks is None:
            return Applicability(
                status="unsupported", reason="structured instruction_blocks required"
            )
        prompt = normalized_prompt(source)
        texts = [item["text"] for item in blocks]
        if any(prompt.count(text) != 1 for text in texts):
            return Applicability(
                status="unsupported", reason="instruction blocks must occur exactly once"
            )
        try:
            protected = prompt_spans(source)
        except ValueError as exc:
            return Applicability(status="unsupported", reason=str(exc))
        for span in protected:
            if any(text in prompt[span.start : span.end] for text in texts):
                return Applicability(
                    status="unsupported", reason="instruction block overlaps protected span"
                )
        position = spec.params.get("position", "tail")
        if position not in {"head", "tail", "shuffle"}:
            return Applicability(status="unsupported", reason="unknown target position")
        return Applicability(status="supported")

    def mutate(self, source: MutationInput, spec: VariantSpec) -> VariantResult:
        applicability = self.check(source, spec)
        if applicability.status == "unsupported":
            raise ValueError(applicability.reason or "mutation unsupported")
        prompt = normalized_prompt(source)
        texts = [item["text"] for item in _blocks(source) or []]
        remainder = prompt
        for text in texts:
            remainder = remainder.replace(text, "", 1)
        remainder = remainder.strip()
        position = spec.params.get("position", "tail")
        ordered = list(texts)
        if position == "shuffle":
            random.Random(spec.seed).shuffle(ordered)
        block_text = "\n\n".join(ordered)
        output = (
            f"{block_text}\n\n{remainder}" if position == "head" else f"{remainder}\n\n{block_text}"
        ).strip()
        if output == prompt:
            raise ValueError("instruction-position produced no change")
        verify_protected_values(source, output)
        return VariantResult(
            prompt=output,
            context_delta={},
            content_hash=variant_content_hash(
                source, spec, prompt=output, context_delta={}
            ),
            summary={"mutator": self.id, "position": position, "block_count": len(texts)},
        )

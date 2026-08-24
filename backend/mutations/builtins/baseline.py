from __future__ import annotations

from ..hashing import variant_content_hash
from ..models import Applicability, MutationInput, VariantResult, VariantSpec
from .common import normalized_prompt


class BaselineMutator:
    id = "baseline"
    version = "1"
    invariants = ("source_semantics", "prompt_identity")
    modalities = ("text", "image", "audio", "file")

    def check(self, source: MutationInput, spec: VariantSpec) -> Applicability:
        return Applicability(status="supported")

    def mutate(self, source: MutationInput, spec: VariantSpec) -> VariantResult:
        prompt = normalized_prompt(source)
        return VariantResult(
            prompt=prompt,
            content_hash=variant_content_hash(
                source, spec, prompt=prompt, context_delta={}
            ),
            summary={"kind": "baseline", "changed_characters": 0},
        )

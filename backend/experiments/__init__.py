"""Experiment-domain contracts for research capability expansion."""

from .hashing import canonical_hash, canonical_json_bytes, content_id, new_id
from .models import (
    ExperimentProtocol,
    LeaderConfig,
    MatrixCell,
    RunGroupPlan,
    VariantSpec,
)

__all__ = [
    "ExperimentProtocol",
    "LeaderConfig",
    "MatrixCell",
    "RunGroupPlan",
    "VariantSpec",
    "canonical_hash",
    "canonical_json_bytes",
    "content_id",
    "new_id",
]

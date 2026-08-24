"""Offline, batch-driven Rubric Evolution.

The package is deliberately outside the live judge path: judges execute the
currently frozen rubric; this loop reads persisted evidence and proposes a new,
versioned rubric for human publication.
"""

from .batching import prepare_evolution_batch
from .models import (
    EvolutionBatch,
    EvolutionBatchPreparation,
    JudgeCheckRecord,
    JudgeRecord,
    RubricEvolutionResult,
    RubricScoreView,
)
from .contracts import (
    FrozenWorkspace,
    freeze_association_workspace,
    freeze_process_workspace,
    freeze_product_workspace,
    persist_product_batch_once,
    project_historical_product_evidence_sync,
    replay_process_evidence_sync,
    replay_product_evidence_sync,
    replay_score_views,
    validate_association_result,
    validate_process_result,
    validate_product_result,
)
from .active_loop import (
    FrozenCorpus,
    RubricGap,
    RubricHypothesis,
    RubricProposal,
    compile_proposal,
    freeze_corpus,
    mine_gap_from_claim,
    run_active_evolution,
)
from .process_product_map import (
    ProcessProductMapping,
    map_process_to_product,
    opportunities_to_gaps,
)
from .pipeline import RubricEvolutionOutcome, evolve_rubric
from .replay import RubricReplayCase, RubricReplayOutcome, run_rubric_replay
from .scoring import compute_rubric_score_views
from .store import (
    append_score_revision_sync,
    get_active_rubric_version_sync,
    get_published_rubric_sync,
    list_score_revisions_sync,
    publish_candidate_sync,
    register_active_rubric_sync,
)

__all__ = [
    "EvolutionBatch",
    "EvolutionBatchPreparation",
    "FrozenCorpus",
    "FrozenWorkspace",
    "JudgeCheckRecord",
    "RubricGap",
    "RubricHypothesis",
    "RubricProposal",
    "ProcessProductMapping",
    "JudgeRecord",
    "RubricEvolutionOutcome",
    "RubricEvolutionResult",
    "RubricReplayCase",
    "RubricReplayOutcome",
    "RubricScoreView",
    "append_score_revision_sync",
    "compile_proposal",
    "compute_rubric_score_views",
    "evolve_rubric",
    "freeze_corpus",
    "map_process_to_product",
    "mine_gap_from_claim",
    "opportunities_to_gaps",
    "run_active_evolution",
    "freeze_association_workspace",
    "freeze_process_workspace",
    "freeze_product_workspace",
    "persist_product_batch_once",
    "project_historical_product_evidence_sync",
    "replay_process_evidence_sync",
    "replay_product_evidence_sync",
    "replay_score_views",
    "validate_association_result",
    "validate_process_result",
    "validate_product_result",
    "get_active_rubric_version_sync",
    "get_published_rubric_sync",
    "list_score_revisions_sync",
    "prepare_evolution_batch",
    "publish_candidate_sync",
    "register_active_rubric_sync",
    "run_rubric_replay",
]

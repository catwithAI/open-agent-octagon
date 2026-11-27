"""DEPRECATED / FROZEN (D1): Legacy agent-first black-box runtrace analysis.

This package is unwired from the active API and runner paths, superseded by
``backend.insights`` and ``backend.attribution_client``. It is frozen pending
removal — do not add new features or wire new callers here.
"""

from .pipeline import AnalysisOutcome, analyze_run
from .snapshot import build_run_analysis_snapshot

__all__ = ["AnalysisOutcome", "analyze_run", "build_run_analysis_snapshot"]

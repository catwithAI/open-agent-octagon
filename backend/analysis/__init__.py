"""Agent-first black-box runtrace analysis."""

from .pipeline import AnalysisOutcome, analyze_run
from .snapshot import build_run_analysis_snapshot

__all__ = ["AnalysisOutcome", "analyze_run", "build_run_analysis_snapshot"]

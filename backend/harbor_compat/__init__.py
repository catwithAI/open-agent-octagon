"""Harbor-Index task compatibility primitives.

This package is deliberately small at the first integration step. It validates
frozen task metadata and creates a task-specific Docker launcher; verifier and
sidecar lifecycle are added in the attempt-runtime layer, not in env scorers.
"""

from .runtime import HarborAttemptRuntime, HarborRuntimeError
from .spec import HarborTaskSpec, HarborTaskSpecError

__all__ = ["HarborAttemptRuntime", "HarborRuntimeError", "HarborTaskSpec", "HarborTaskSpecError"]

"""Out-of-process worker modules for scoring and isolated environment execution.

Modules in this package decouple untrusted or resource-heavy workloads from the
FastAPI web process:

- :mod:`backend.workers.scoring_worker`: standalone lease-based scoring daemon
  and single-job subprocess runner with OS-level process-group timeout/cancel
  termination.
- :mod:`backend.workers.env_worker`: credential-scrubbed subprocess runner for
  ``envs/<name>/core.py`` tool execution, metadata inspection, and
  ``scorer.py`` execution.
"""

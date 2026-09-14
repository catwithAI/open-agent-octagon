# Harbor-Index compatibility runtime

Status: pilot runtime wiring implemented for the Codex path. The dispatch layer
now prepares a task-specific overlay, materializes the Harbor image workspace,
mounts `/logs` and `/tmp`, runs the agent, then invokes the separate verifier
before scoring. Sidecar and `verifier.collect` support remain gated because the
five selected pilot tasks do not use them.

## Invariants

1. The official Harbor task instruction is passed verbatim; no Octagon rubric or
   adaptation prose is prepended.
2. The task, agent image, verifier image, and source revision are all digest/
   commit pinned in the env catalog.
3. The agent never receives `tests/`, `solution/`, verifier commands, oracle
   data, or private task source files.
4. The official raw reward is retained separately from Octagon's 0--100 display
   score. No generic Octagon rubric is allowed to replace the Harbor verifier.
5. Missing images, verifier failures, and sidecar failures are infrastructure
   failures, not reward 0.

## Container topology

Do not run an unrelated Octagon agent container next to a Harbor task container
with only a shared workspace: shell commands would execute in the wrong image.
The compatibility runtime must use a reproducible agent overlay image:

```dockerfile
FROM <harbor-agent-environment>@<digest> AS task
FROM <octagon-agent-runtime>@<digest> AS octagon
FROM task
# Copy the selected agent runtime/CLI, or install it reproducibly.
# Keep a label for both parent digests and the resulting image digest.
```

The resulting overlay is the Octagon agent sandbox for that task. The verifier
remains a separate container built from the exact Harbor verifier image.

For the five-task pilot, the platform lazily builds only the Codex runtime
needed by the selected task rather than the full six-agent runtime. The
explicit commands below remain useful for prewarming a node, but are not
required for normal UI runs:

```bash
export HARBOR_CODEX_BASE_IMAGE='<the-first-task-agent-image-ref>'
docker/harbor-codex-runtime/build.sh harbor-octagon-codex-runtime:pilot
export HARBOR_OCTAGON_RUNTIME_IMAGE=harbor-octagon-codex-runtime:pilot
```

The builder uses `--pull=false`; the runtime prepare path first pulls only
the selected task's digest-pinned agent image, then builds and caches the
runtime. It never performs a catalog-wide image pull.

The current `DockerLauncher` now supports the required task-specific boundary:

- `workspace_container`: maps the host attempt workspace to `/app`, `/workspace`,
  `/testbed`, etc. from the Harbor task Dockerfile.
- `network_mode`: maps Harbor `no-network` to Docker `none`; public mode keeps
  the existing bridge behavior.
- `limits_override`: applies task-level CPU/memory/pids values instead of the
  global Octagon defaults.
- `build_exec_argv()` translates host workspace paths to the task workdir while
  accepting dsh's already-translated path form.

## Dispatch lifecycle

`HarborAttemptRuntime` is called by dispatch around `run_attempt()`:

```python
runtime = HarborAttemptRuntime(settings=settings, spec=spec, agent_name="codex")
await runtime.prepare(attempt_dir=attempt_dir)
adapter = build_adapter(..., launcher_override=runtime.launcher())
result = await run_attempt(
    ..., defer_scoring=True, post_agent=runtime.finalize,
)
```

For the current five-task pilot, `finalize()` performs:

1. Freeze an immutable workspace snapshot and copy the public `/logs/artifacts`
   tree into verifier input.
2. Start the exact verifier image with `--pull never` and the expected artifact
   paths; never mount the agent image's `/tests` or `/solution` files.
3. Capture verifier stdout/stderr, exit code, reward files, image digests, and
   the submission hash under `attempts/<id>/harbor/`.
4. Write `result.json` before the scoring queue runs.

`verifier.collect` hooks and sidecar lifecycle are intentionally not enabled
until a task manifest carries their complete commands and health-check policy.

Recommended attempt artifact layout:

```text
attempts/<id>/harbor/
├── spec.json
├── initial_snapshot.json
├── agent_container.json
├── submission_manifest.json
├── verifier_container.json
├── verifier-stdout.txt
├── verifier-stderr.txt
├── sidecars.json
└── result.json
```

## Score schema

`result.json` should contain at least:

```json
{
  "schema_version": 1,
  "status": "completed",
  "release": "1.4",
  "task_name": "harbor-index/<task>",
  "task_digest": "sha256:...",
  "agent_image": "...@sha256:...",
  "verifier_image": "...@sha256:...",
  "reward": 0.0,
  "verifier_exit_code": 0,
  "submission_sha256": "...",
  "verifier_elapsed_seconds": 0.0
}
```

The env `scorer.py` reads only this file and returns:

```text
harbor_reward = round(reward * 100, 6)
```

If the file is missing, malformed, or reports a verifier/runtime failure, it
raises `ScorerUnavailableError` so the attempt is not confused with a genuine
reward of zero.

## Parity gates

Before enabling all 80 tasks:

- **Gate A — catalog:** exactly 80 task records; task digest and both image
  references are pinned; task prompt hash equals `instruction.md` hash.
- **Gate B — initial state:** reference solution starts from the same workspace
  tree and expected workdir as Harbor.
- **Gate C — verifier:** the reference solution and a known failing artifact
  receive byte-for-byte identical raw reward from Harbor and Octagon.
- **Gate D — lifecycle:** timeout, SIGTERM, verifier failure, and sidecar failure
  clean every attempt-scoped container/network.
- **Gate E — end-to-end:** run a small fixed pilot, preserving Harbor raw reward
  and Octagon trace independently. Stochastic agent scores need repeated trials;
  exact trajectory equality is not required.

Only after Gates A--E pass should the compatibility env be exposed as an
official comparison condition.

## Remote Blade Agent bridge

Blade is not a local CLI and must not be faked as one. For Harbor runs the
`blade-agent` condition uses a generic Docker wrapper instead of the Codex
binary overlay:

```text
Harbor task image (input materialization only)
        + local blade-wrapper image (SDK + bridge)
        ↓
Docker container running harbor-blade-wrapper
        ↓ HTTPS/SDK
cloud Blade Agent
```

The wrapper protocol is task-agnostic. The backend writes one JSON request to
`/attempt/blade-request.json` (read-only), containing the frozen prompt,
workspace context, Blade endpoint/model/entry, and timeout. The wrapper then:

1. creates a cloud session through `blade_agent_kit`;
2. uploads every public file under `/app` using relative paths;
3. sends the frozen Harbor instruction, with only a mechanical `/app` → remote
   workspace and `/logs` → `logs/` path mapping;
4. retrieves the SDK `RunTrace` through `collect_trace()` and writes
   `/logs/runtrace.json`;
5. downloads the remote workspace and copies `logs/artifacts/*` into the local
   `/logs/artifacts/*` tree;
6. writes `/logs/blade-result.json`, which the outer adapter converts to the
   normal Octagon `AdapterResult` contract.

The outer Octagon verifier therefore sees the same local artifact boundary for
Codex and Blade: `/logs/artifacts/*` → Harbor verifier → `result.json` → scorer.
The Blade API token is stored only in the attempt-scoped read-only request file
inside the wrapper container and is never placed in the command line.

Build the wrapper explicitly when the Blade pilot is enabled:

```bash
# 默认使用本地已有 python:3.12-slim，不触发基础镜像下载
docker/harbor-blade-wrapper/build.sh harbor-octagon-blade-wrapper:pilot
export HARBOR_BLADE_WRAPPER_IMAGE=harbor-octagon-blade-wrapper:pilot
```

The current implementation is intentionally single-turn and uses the neutral
`general_chat` solution for Harbor tasks. If a task requires a registered Blade
skill/solution, the manifest must add an explicit runtime entry; the wrapper
must not infer one from the Harbor task name.

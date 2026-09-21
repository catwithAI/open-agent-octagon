.PHONY: sandbox-image sandbox-smoke

# agent 运行时镜像（spec: docs/specs/260909-agent-sandbox）。本地开发用；正式 tag 由 CI 发布。
SANDBOX_IMAGE ?= octagon-agent-runtime:dev

sandbox-image:
	docker/agent-runtime/build.sh $(SANDBOX_IMAGE)

sandbox-smoke:
	docker/agent-runtime/smoke.sh $(SANDBOX_IMAGE)

.PHONY: archive-attempts archive-attempts-apply

# attempt 归档（spec: docs/specs/260921-eval-storage-and-artifact-recovery）。
# 默认 dry-run：只打印将删什么、能回收多少。
DATA_PATH ?= ./data

archive-attempts:
	uv run python -m backend.tools.archive_attempts --data-path $(DATA_PATH)

archive-attempts-apply:
	uv run python -m backend.tools.archive_attempts --data-path $(DATA_PATH) --yes

.PHONY: sandbox-image sandbox-smoke

# agent 运行时镜像（spec: docs/specs/260909-agent-sandbox）。本地开发用；正式 tag 由 CI 发布。
SANDBOX_IMAGE ?= octagon-agent-runtime:dev

sandbox-image:
	docker/agent-runtime/build.sh $(SANDBOX_IMAGE)

sandbox-smoke:
	docker/agent-runtime/smoke.sh $(SANDBOX_IMAGE)

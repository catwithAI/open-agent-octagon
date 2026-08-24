"""Octagon env-facing API.

env 模块通过 `from octagon.env_api import env_tool, EnvContext, TraceWriter`
拿到注册和 trace 工具。本顶层包刻意保持极薄,只为 env `core.py` 提供窄接口,
不暴露 backend 内部数据模型。
"""

"""Neu Box worker package (节点侧设备沙盒).

2026-08-25 起本仓库仅包含 worker 角色 + 聚合职能（e2e 测试、
webui/goClient 两个 submodule 的兼容矩阵）。
WebUI 见 neu_box_webui 仓库，Go 客户端见 neu_box_goClient 仓库。
"""

__version__ = "0.3.0"

# worker HTTP API 版本：仅破坏性变更（删字段、改语义）时 +1
API_VERSION = 1

"""Current-only workflow contract authority."""

from __future__ import annotations


CURRENT_CONTRACT = "word-only-v1"
SUPPORTED_CONTRACTS = frozenset({CURRENT_CONTRACT})
OLD_PROJECT_ERROR = "当前插件仅支持 word-only-v1 项目。请使用当前 Word-only 工作流创建新项目。"


def require_supported_contract(run: dict) -> str:
    """Return the sole supported contract for a persisted project state."""
    if run.get("workflow_contract_version") != CURRENT_CONTRACT:
        raise ValueError(OLD_PROJECT_ERROR)
    return CURRENT_CONTRACT


def contract_features(version: str) -> frozenset[str]:
    """Return the capabilities currently available to a Word-only project."""
    if version != CURRENT_CONTRACT:
        raise ValueError(OLD_PROJECT_ERROR)
    return frozenset({"style_confirmation"})

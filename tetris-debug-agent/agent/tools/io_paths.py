"""路径 jail：所有文件读写统一经此解析，越出 SANDBOX_ROOT 即拒绝。"""
from __future__ import annotations

from pathlib import Path

from agent.config import SANDBOX_ROOT


class SandboxViolation(PermissionError):
    """试图访问沙盒之外的路径。"""


def resolve_sandbox_path(p: Path | str, *, must_exist: bool = False) -> Path:
    """把任意路径解析到沙盒内绝对路径；越界抛 SandboxViolation。

    :param p: 相对路径相对 SANDBOX_ROOT 解释，绝对路径必须在沙盒内
    :param must_exist: True 时要求路径已存在
    """
    path = Path(p)
    if not path.is_absolute():
        path = SANDBOX_ROOT / path
    resolved = path.resolve()
    if resolved != SANDBOX_ROOT and SANDBOX_ROOT not in resolved.parents:
        raise SandboxViolation(f"路径越出沙盒: {resolved}")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"路径不存在: {resolved}")
    return resolved

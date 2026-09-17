r"""patch — SEARCH/REPLACE 补丁原语 + pytest 执行器。

补丁格式（弃 unified diff）：blocks = [{"search": "...", "replace": "..."}]
SEARCH 必须在目标文件规范化后恰好出现 1 次，否则 fail-fast 不动文件；
全部块通过预检后才备份并写盘，失败可从备份恢复。

run_pytest 供 tester 节点跑受控测试子集（tests/test_bugs 指定文件 +
tests/golden，golden 用 GOLDEN_ALLOWED_PH 门控放行）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent.config import BACKUP_DIR, PYTEST_TIMEOUT, SANDBOX_ROOT, TARGET_FILE
from agent.tools.io_paths import resolve_sandbox_path


@dataclass
class PatchResult:
    ok: bool
    error: str = ""
    backup_path: Path | None = None


@dataclass
class TestResult:
    returncode: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def apply_patch(blocks: list[dict], target: Path | None = None) -> PatchResult:
    """顺序应用 SEARCH/REPLACE 块；任何块命中次数 != 1 即整体不动。"""
    path = resolve_sandbox_path(target or TARGET_FILE, must_exist=True)
    source = _normalize(path.read_text(encoding="utf-8"))

    for i, block in enumerate(blocks):
        search = _normalize(block["search"])
        n = source.count(search)
        if n != 1:
            return PatchResult(
                ok=False,
                error=f"块 {i + 1}/{len(blocks)} 的 SEARCH 命中 {n} 次（要求恰好 1 次），文件未改动",
            )

    backup_path = _backup(path)
    patched = source
    for block in blocks:
        patched = patched.replace(_normalize(block["search"]), _normalize(block["replace"]))
    path.write_text(patched, encoding="utf-8")
    return PatchResult(ok=True, backup_path=backup_path)


def _backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    n = 1
    while (BACKUP_DIR / f"{path.stem}.attempt{n}{path.suffix}").exists():
        n += 1
    dest = BACKUP_DIR / f"{path.stem}.attempt{n}{path.suffix}"
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def revert_backup(backup_path: Path, target: Path | None = None) -> None:
    src = resolve_sandbox_path(backup_path, must_exist=True)
    dest = resolve_sandbox_path(target or TARGET_FILE)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def run_pytest(test_paths: list[Path], allowed_ph: list[str] | None = None,
               timeout: int = PYTEST_TIMEOUT) -> TestResult:
    """跑受控测试子集；allowed_ph 为当前假设/已修复的现象号（golden 门控放行）。"""
    args = [sys.executable, "-m", "pytest", "-q", *(str(resolve_sandbox_path(p)) for p in test_paths)]
    env = {**os.environ, "GOLDEN_ALLOWED_PH": ",".join(allowed_ph or [])}
    try:
        proc = subprocess.run(
            args, cwd=SANDBOX_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return TestResult(returncode=-1, output=f"pytest 超时（{timeout}s）")

    passed = failed = skipped = 0
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) failed", out)
    failed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) skipped", out)
    skipped = int(m.group(1)) if m else 0
    return TestResult(returncode=proc.returncode, passed=passed,
                      failed=failed, skipped=skipped, output=out[-8000:])

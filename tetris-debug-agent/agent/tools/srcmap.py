"""srcmap — ast 解析 game/tetris_buggy.py：函数名 → 源码片段。

给 diagnostician/patcher 提供嫌疑函数的当前源码（每次读取即时解析，
反映补丁后的最新状态）。仅沙盒内目标文件。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from agent.config import TARGET_FILE
from agent.tools.io_paths import resolve_sandbox_path


@dataclass
class SourceMap:
    functions: dict[str, str]          # 顶层函数名 → 源码段
    constants: dict[str, str]          # 顶层赋值目标 → 源码段
    total_lines: int


def build_srcmap(target: Path | None = None) -> SourceMap:
    path = resolve_sandbox_path(target or TARGET_FILE, must_exist=True)
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    functions: dict[str, str] = {}
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            functions[node.name] = seg
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seg = "\n".join(lines[item.lineno - 1 : item.end_lineno])
                    functions[item.name] = seg   # 平铺方法名（_on_right 等）
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            name = t.id if isinstance(t, ast.Name) else None
            if name:
                constants[name] = "\n".join(lines[node.lineno - 1 : node.end_lineno])

    return SourceMap(functions=functions, constants=constants, total_lines=len(lines))

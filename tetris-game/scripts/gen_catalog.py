r"""gen_catalog.py — 出题方侧现象级 catalog 生成器（plan 第 3 步）。

读 bugs.yaml（单一事实源），抽取玩家视角字段（name / symptom / signal），
按 B01..B12 顺序生成沙盒目录下的 PH-01..PH-12，另附 PH→B 真值映射
（仅 tester 门控与离线 eval 可读，绝不进任何 prompt）。

泄漏红线：catalog 文本中不得出现 injection / clean_code / buggy_code /
B\d{2} 等内部标记；truth_map 本身是 PH→B 映射，只检查不含注入叙述字样。
自检不通过即以非零码退出且不落盘。

用法：
    python scripts/gen_catalog.py [--sandbox-root PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import yaml

BUG_ORDER = [f"B{i:02d}" for i in range(1, 13)]
INTERNAL_PATTERN = re.compile(r"injection|clean_code|buggy_code", re.IGNORECASE)
CATALOG_LEAK_PATTERN = re.compile(
    INTERNAL_PATTERN.pattern + r"|\bB\d{2}\b|_play_again", re.IGNORECASE
)


def derive_observable_via(signal: str) -> list[str]:
    """从 signal 文本派生可观测通道（与 plan 派生规则一致）。"""
    via: list[str] = []
    if "埋点" in signal and "埋点无法感知" not in signal:
        via.append("telemetry")
    if "截图" in signal:
        via.append("screenshot")
    if "反馈" in signal or "文字" in signal:
        via.append("feedback")
    return via


def build_catalog(bugs_path: Path) -> tuple[dict, dict]:
    raw = yaml.safe_load(bugs_path.read_text(encoding="utf-8"))
    missing = [b for b in BUG_ORDER if b not in raw]
    if missing:
        raise SystemExit(f"bugs.yaml 缺少条目: {missing}")

    phenomena = []
    truth_map = {}
    for i, bug_id in enumerate(BUG_ORDER, start=1):
        entry = raw[bug_id]
        signal = " ".join(str(entry["signal"]).split())
        symptom = " ".join(str(entry["symptom"]).split())
        via = derive_observable_via(signal)
        if not via:
            raise SystemExit(f"{bug_id} 的 signal 未派生出任何 observable_via")
        ph_id = f"PH-{i:02d}"
        phenomena.append(
            {
                "id": ph_id,
                "name": str(entry["name"]),
                "symptom": symptom,
                "signal": signal,
                "observable_via": via,
            }
        )
        truth_map[ph_id] = bug_id

    catalog = {
        "catalog_version": 1,
        "source_sha256": hashlib.sha256(bugs_path.read_bytes()).hexdigest(),
        "phenomena": phenomena,
    }
    return catalog, truth_map


def leak_check(catalog_path: Path, truth_path: Path) -> list[str]:
    hits: list[str] = []
    text = catalog_path.read_text(encoding="utf-8")
    for m in CATALOG_LEAK_PATTERN.finditer(text):
        hits.append(f"{catalog_path.name}: {m.group(0)!r}")
    text = truth_path.read_text(encoding="utf-8")
    for m in INTERNAL_PATTERN.finditer(text):
        hits.append(f"{truth_path.name}: {m.group(0)!r}")
    return hits


def main() -> int:
    here = Path(__file__).resolve()
    default_sandbox = here.parents[1].parent / "tetris-debug-agent"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bugs", type=Path, default=here.parents[1] / "bugs.yaml")
    parser.add_argument("--sandbox-root", type=Path, default=default_sandbox)
    args = parser.parse_args()

    catalog, truth_map = build_catalog(args.bugs)
    out_dir = args.sandbox_root / "data" / "catalog"
    gold_dir = args.sandbox_root / "data" / "gold"
    catalog_path = out_dir / "catalog.yaml"
    truth_path = gold_dir / "truth_map.json"

    import json

    catalog_text = yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False, width=100)
    truth_text = json.dumps(truth_map, ensure_ascii=False, indent=2)

    # 先在内存里做泄漏自检，不通过则不落盘
    staged = []
    for p, text, pattern in (
        (catalog_path, catalog_text, CATALOG_LEAK_PATTERN),
        (truth_path, truth_text, INTERNAL_PATTERN),
    ):
        for m in pattern.finditer(text):
            staged.append(f"{p.name}: {m.group(0)!r}")
    if staged:
        print("泄漏自检未通过，未写盘：", file=sys.stderr)
        print("\n".join(staged), file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    gold_dir.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(catalog_text, encoding="utf-8")
    truth_path.write_text(truth_text, encoding="utf-8")

    # 落盘后复核
    residual = leak_check(catalog_path, truth_path)
    n = len(catalog["phenomena"])
    print(f"catalog: {catalog_path} ({n} phenomena)")
    print(f"truth_map: {truth_path}")
    print(f"泄漏自检: {'PASS' if not residual else 'FAIL ' + str(residual)}")
    return 0 if not residual else 1


if __name__ == "__main__":
    raise SystemExit(main())

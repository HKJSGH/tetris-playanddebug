r"""gen_catalog.py — 出题方侧线索级 catalog 生成器（阶段四重构）。

现象知识来源改为 game/tetris_buggy_v0.py 中注入的开发者备注（"# NOTE:" 行）：
扫描 v0 基线的 NOTE 注释，经 bugs.yaml 各 bug 的 buggy_code 唯一锚定行贪心
关联到 bug_id，生成沙盒 data/catalog/catalog.yaml：

    phenomena: [{id: PH-xx, clue: 注释裸文本, observable_via: [...]}]

clue 是弱线索（开发者遗留备注），不再含 name / symptom / signal 答案字段。
truth_map（PH→B）生成不变，仅 tester 门控与离线 eval 可读，绝不进任何 prompt。

关联算法：对每个 bug 取 buggy_code 非注释行中在 v0 源码（strip 后）恰好唯一
出现的行作锚定行；全局按 |锚定行 - NOTE 行| 距离升序贪心认领，超过 MAX_DIST
的配对不参与；任一 bug / NOTE 未认领即 SystemExit 拒绝落盘（fail-loud）。

泄漏红线：catalog 文本不得出现 injection / clean_code / buggy_code /
B\\d{2} / NOTE / tetris_buggy / tetris_v0 等内部标记；truth_map 本身是
PH→B 映射，只检查不含注入叙述字样。自检不通过即非零退出且不落盘。

用法：
    python scripts/gen_catalog.py [--bugs PATH] [--buggy PATH] [--sandbox-root PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

BUG_ORDER = [f"B{i:02d}" for i in range(1, 13)]
NOTE_PREFIX = "# NOTE:"
MAX_DIST = 6  # 锚定行与 NOTE 行的最大距离（超出即视为不可能的配对）
INTERNAL_PATTERN = re.compile(r"injection|clean_code|buggy_code", re.IGNORECASE)
CATALOG_LEAK_PATTERN = re.compile(
    INTERNAL_PATTERN.pattern + r"|\bB\d{2}\b|_play_again|\bNOTE\b|tetris_buggy|tetris_v0",
    re.IGNORECASE,
)
# 每个现象的证据通道（静态表：signal 字段已不再读取，杜绝从答案型文本派生）
CHANNELS = {
    "B01": ["telemetry"], "B02": ["telemetry"], "B03": ["telemetry"],
    "B04": ["telemetry"], "B05": ["telemetry"], "B06": ["screenshot", "feedback"],
    "B07": ["telemetry"], "B08": ["telemetry"], "B09": ["telemetry"],
    "B10": ["telemetry"], "B11": ["screenshot", "feedback"], "B12": ["feedback"],
}


def _code_lines(text: str) -> list[str]:
    """strip 后去掉空行与纯注释行（buggy_code 的首行是定位叙述注释）。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def extract_clues(bugs: dict, buggy_path: Path) -> dict[str, dict]:
    """扫描 v0 的 NOTE 注释并关联到 bug_id。

    返回 {bug_id: {clue, line, dist}}；关联失败 / 认领冲突 / 文本为空均
    SystemExit（fail-loud，拒绝产出 catalog）。
    """
    raw_lines = buggy_path.read_text(encoding="utf-8").splitlines()
    stripped = [l.strip() for l in raw_lines]
    note_rows = [i for i, s in enumerate(stripped) if s.startswith(NOTE_PREFIX)]
    if len(note_rows) != len(BUG_ORDER):
        raise SystemExit(
            f"{buggy_path.name} 中 NOTE 注释数应为 {len(BUG_ORDER)}，实际 {len(note_rows)}"
        )
    counts = Counter(s for s in stripped if s)

    # 每 bug 的唯一锚定行集合（buggy_code 中在源码恰好出现一次的行）
    anchors: dict[str, list[int]] = {}
    for bug_id in BUG_ORDER:
        rows: list[int] = []
        for s in _code_lines(bugs[bug_id].get("buggy_code", "")):
            if counts.get(s, 0) == 1:
                rows.extend(i for i, t in enumerate(stripped) if t == s)
        if not rows:
            raise SystemExit(f"{bug_id}: buggy_code 找不到任何唯一锚定行")
        anchors[bug_id] = rows

    # 全局贪心认领：距离升序，每 bug / 每 NOTE 至多认领一次，MAX_DIST 内才可配
    pairs = sorted(
        (abs(a - n), a, n, b)
        for b, arows in anchors.items()
        for a in arows
        for n in note_rows
        if abs(a - n) <= MAX_DIST
    )
    claimed_bug: dict[str, tuple[int, int]] = {}
    claimed_note: dict[int, str] = {}
    for dist, _a, n, b in pairs:
        if b in claimed_bug or n in claimed_note:
            continue
        claimed_bug[b] = (n, dist)
        claimed_note[n] = b

    unclaimed_bugs = [b for b in BUG_ORDER if b not in claimed_bug]
    unclaimed_notes = sorted(set(note_rows) - set(claimed_note))
    if unclaimed_bugs or unclaimed_notes:
        raise SystemExit(
            f"注释↔bug 关联失败: 未认领 bug={unclaimed_bugs}，"
            f"未被认领 NOTE 行={[(n + 1) for n in unclaimed_notes]}"
        )

    out: dict[str, dict] = {}
    for bug_id, (n, dist) in claimed_bug.items():
        clue = stripped[n].removeprefix(NOTE_PREFIX).strip()
        if not clue:
            raise SystemExit(f"{bug_id}: NOTE 注释文本为空（行 {n + 1}）")
        out[bug_id] = {"clue": clue, "line": n + 1, "dist": dist}
    return out


def build_catalog(bugs_path: Path, buggy_path: Path) -> tuple[dict, dict, dict]:
    raw = yaml.safe_load(bugs_path.read_text(encoding="utf-8"))
    missing = [b for b in BUG_ORDER if b not in raw]
    if missing:
        raise SystemExit(f"bugs.yaml 缺少条目: {missing}")

    clues = extract_clues(raw, buggy_path)

    phenomena = []
    truth_map = {}
    for i, bug_id in enumerate(BUG_ORDER, start=1):
        ph_id = f"PH-{i:02d}"
        phenomena.append(
            {
                "id": ph_id,
                "clue": clues[bug_id]["clue"],
                "observable_via": list(CHANNELS[bug_id]),
            }
        )
        truth_map[ph_id] = bug_id

    catalog = {
        "catalog_version": 2,
        "source_sha256": hashlib.sha256(buggy_path.read_bytes()).hexdigest(),
        "bugs_sha256": hashlib.sha256(bugs_path.read_bytes()).hexdigest(),
        "phenomena": phenomena,
    }
    return catalog, truth_map, clues


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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    here = Path(__file__).resolve()
    default_sandbox = here.parents[1].parent / "tetris-debug-agent"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bugs", type=Path, default=here.parents[1] / "bugs.yaml")
    parser.add_argument("--buggy", type=Path,
                        default=here.parents[1] / "game" / "tetris_buggy_v0.py")
    parser.add_argument("--sandbox-root", type=Path, default=default_sandbox)
    args = parser.parse_args()

    catalog, truth_map, clues = build_catalog(args.bugs, args.buggy)
    out_dir = args.sandbox_root / "data" / "catalog"
    gold_dir = args.sandbox_root / "data" / "gold"
    catalog_path = out_dir / "catalog.yaml"
    truth_path = gold_dir / "truth_map.json"

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
    print(f"catalog: {catalog_path} ({n} phenomena, version={catalog['catalog_version']})")
    print(f"truth_map: {truth_path}")
    print("bug_id → 行号 → 线索：")
    for bug_id in BUG_ORDER:
        c = clues[bug_id]
        print(f"  {bug_id}  L{c['line']:>3}  (dist={c['dist']})  {c['clue']}")
    print(f"泄漏自检: {'PASS' if not residual else 'FAIL ' + str(residual)}")
    return 0 if not residual else 1


if __name__ == "__main__":
    raise SystemExit(main())

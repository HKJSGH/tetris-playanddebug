r"""probes — 局数据加载 + 12 个现象探针（纯代码特征提取）。

探针三态：
  signal      有证据怀疑（该现象成立）
  no_signal   有数据且符合正常行为
  no_evidence 本局该机制未触发，无法判定（无事件 ≠ 正常）

用法：python -m agent.tools.probes data/runs/round_1
探针 key 用 B01..B12 内部编号（与 catalog PH 顺序一一对应）；呈现给 LLM
前由 nodes 层经 truth_map 转换成 PH 编号，B 编号不进任何 prompt。
"""
from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent.tools.io_paths import resolve_sandbox_path
from agent.tools.replay import replay_events

SIGNAL, NO_SIGNAL, NO_EVIDENCE = "signal", "no_signal", "no_evidence"


@dataclass
class RoundData:
    round_dir: Path
    meta: dict
    events: list[dict]
    errors_text: str
    feedback_text: str
    game_state: dict
    screenshots: list[Path] = field(default_factory=list)


@dataclass
class ProbeResult:
    status: str
    confidence: float
    evidence: list[str] = field(default_factory=list)


# ---- 数据加载 ---------------------------------------------------------------

def load_round(run_dir: Path | str) -> RoundData:
    d = resolve_sandbox_path(run_dir, must_exist=True)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    events = [
        json.loads(line)
        for line in (d / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    errors_text = (d / "errors.log").read_text(encoding="utf-8") if (d / "errors.log").exists() else ""
    feedback_text = (d / "feedback_text.md").read_text(encoding="utf-8") if (d / "feedback_text.md").exists() else ""
    gs_path = d / "game_state.json"
    game_state = json.loads(gs_path.read_text(encoding="utf-8")) if gs_path.exists() else {}
    shots = sorted(d.glob("screenshot_*.png"))
    return RoundData(
        round_dir=d, meta=meta, events=events, errors_text=errors_text,
        feedback_text=feedback_text, game_state=game_state, screenshots=shots,
    )


# ---- 统计与分段 -------------------------------------------------------------

def _segments(events: list[dict]) -> list[list[dict]]:
    """按 spawn→lock 切段：每段是一个方块的完整生命周期。"""
    segs: list[list[dict]] = []
    cur: list[dict] = []
    for ev in events:
        cur.append(ev)
        if ev["event"] == "lock":
            segs.append(cur)
            cur = []
    if any(e["event"] == "spawn" for e in cur):
        segs.append(cur)
    return segs


def _fall_gap_by_kind(segments: list[list[dict]]) -> dict[str, float]:
    gaps: dict[str, list[float]] = {}
    for seg in segments:
        piece = next((e["piece"] for e in seg if e["event"] == "spawn"), None)
        if piece is None:
            continue
        falls = [e["ts"] for e in seg if e["event"] == "fall"]
        gaps.setdefault(piece, []).extend(b - a for a, b in zip(falls, falls[1:]))
    return {k: round(statistics.median(v), 4) for k, v in gaps.items() if v}


def _move_delta_by_action(events: list[dict]) -> dict[str, float]:
    by: dict[str, list[int]] = {}
    last_cr: list[int] | None = None
    for ev in events:
        if ev["event"] == "spawn":
            last_cr = list(ev["cr"])
        elif ev["event"] == "move" and last_cr is not None:
            by.setdefault(ev.get("action", "?"), []).append(ev["cr"][0] - last_cr[0])
            last_cr = list(ev["cr"])
        elif ev["event"] in ("rotate", "fall", "hard_drop") and last_cr is not None:
            last_cr = list(ev["cr"])
    return {k: round(statistics.median(v), 3) for k, v in by.items() if v}


def build_stats(events: list[dict]) -> dict:
    segments = _segments(events)
    score_deltas = []
    prev_score = None
    for ev in events:
        if ev["event"] == "line_clear":
            s = ev.get("score")
            if prev_score is not None and s is not None:
                score_deltas.append(s - prev_score)
            prev_score = s
    pause = sum(1 for e in events if e["event"] == "pause")
    resume = sum(1 for e in events if e["event"] == "resume")
    return {
        "fall_gap_median_by_kind": _fall_gap_by_kind(segments),
        "move_delta_by_action": _move_delta_by_action(events),
        "score_deltas_on_clear": score_deltas,
        "hard_drop_distances": [e.get("distance") for e in events if e["event"] == "hard_drop"],
        "pause_resume": {"pause": pause, "resume": resume},
    }


def _context(rd: RoundData) -> dict:
    events = rd.events
    previews = [e for e in events if e["event"] == "preview"]
    spawns = [e for e in events if e["event"] == "spawn"]
    return {
        "segments": _segments(events),
        "stats": build_stats(events),
        "previews": previews,
        "spawns": spawns,
        "has_line_clear": any(e["event"] == "line_clear" for e in events),
        "has_game_over": any(e["event"] == "game_over" for e in events),
        "replay": replay_events(events, rd.game_state or None),
    }


# ---- 12 探针 ----------------------------------------------------------------

def probe_B01(rd: RoundData, ctx: dict) -> ProbeResult:
    gaps = ctx["stats"]["fall_gap_median_by_kind"]
    if "O" not in gaps or len(gaps) < 2:
        return ProbeResult(NO_EVIDENCE, 0.0, [f"fall_gap_by_kind={gaps}"])
    others = statistics.median([v for k, v in gaps.items() if k != "O"])
    o = gaps["O"]
    ev = [f"O fall_gap_median={o}s", f"others_median={others}s"]
    if others > 0 and o < others * 0.5:
        return ProbeResult(SIGNAL, 0.9, ev + [f"O 间隔为其他方块的 {round(o / others, 2)} 倍"])
    return ProbeResult(NO_SIGNAL, 0.6, ev)


def probe_B02(rd: RoundData, ctx: dict) -> ProbeResult:
    has_rotate = any(e["event"] == "rotate" for e in rd.events)
    bad = ctx["replay"].overlaps_after_rotate
    if not has_rotate:
        return ProbeResult(NO_EVIDENCE, 0.0, ["本局无 rotate 事件"])
    if bad:
        return ProbeResult(SIGNAL, 0.85, [
            f"{len(bad)} 次旋转后与占格/边界重叠，如 {bad[0]}"
        ])
    return ProbeResult(NO_SIGNAL, 0.6, ["旋转均未穿入已占格"])


def probe_B03(rd: RoundData, ctx: dict) -> ProbeResult:
    if not ctx["has_line_clear"]:
        return ProbeResult(NO_EVIDENCE, 0.0, ["本局无 line_clear 事件"])
    deltas = ctx["stats"]["score_deltas_on_clear"]
    if all(d <= 0 for d in deltas):
        return ProbeResult(SIGNAL, 0.85, [f"line_clear 后 score 增量={deltas}（应 >0）"])
    return ProbeResult(NO_SIGNAL, 0.6, [f"score 增量={deltas}"])


def probe_B04(rd: RoundData, ctx: dict) -> ProbeResult:
    colls = ctx["replay"].spawn_collisions
    if not colls:
        return ProbeResult(NO_SIGNAL, 0.5, ["未重建出生即碰撞局面"])
    if not ctx["has_game_over"]:
        return ProbeResult(SIGNAL, 0.8, [
            f"{len(colls)} 次生成即碰撞但无 game_over 事件，如 {colls[0]}"
        ])
    return ProbeResult(NO_SIGNAL, 0.6, ["生成碰撞后正常 game_over 收尾"])


def probe_B05(rd: RoundData, ctx: dict) -> ProbeResult:
    deltas = ctx["stats"]["move_delta_by_action"]
    if "right" not in deltas:
        return ProbeResult(NO_EVIDENCE, 0.0, [f"move_delta_by_action={deltas}"])
    d = deltas["right"]
    if d < 0:
        return ProbeResult(SIGNAL, 0.95, [f"action=right 的列增量中位数={d}（应 +1）"])
    return ProbeResult(NO_SIGNAL, 0.6, [f"move_delta_by_action={deltas}"])


def probe_B06(rd: RoundData, ctx: dict) -> ProbeResult:
    return ProbeResult(NO_EVIDENCE, 0.0, ["颜色异常埋点无法感知，需截图/文字反馈"])


def probe_B07(rd: RoundData, ctx: dict) -> ProbeResult:
    previews, spawns = ctx["previews"], ctx["spawns"]
    if len(previews) < 2 or len(spawns) < 2:
        return ProbeResult(NO_EVIDENCE, 0.0, [f"preview={len(previews)} spawn={len(spawns)}"])
    n = min(len(previews), len(spawns))
    same_frame = all(previews[i]["kind"] == spawns[i]["piece"] for i in range(n))
    next_frame = all(previews[i]["kind"] == spawns[i + 1]["piece"] for i in range(n - 1))
    if same_frame and not next_frame:
        return ProbeResult(SIGNAL, 0.9, [
            f"{n}/{n} 组 preview.kind 与同帧 spawn.piece 恒相等（正确应为下一帧生成）"
        ])
    if next_frame:
        return ProbeResult(NO_SIGNAL, 0.7, ["preview 与下一帧 spawn 一致（正确不变式）"])
    return ProbeResult(NO_SIGNAL, 0.5, ["preview 与 spawn 关联无稳定模式"])


def probe_B08(rd: RoundData, ctx: dict) -> ProbeResult:
    if not ctx["has_line_clear"]:
        return ProbeResult(NO_EVIDENCE, 0.0, ["本局无 line_clear 事件"])
    mm = ctx["replay"].full_rows_mismatch
    if mm:
        return ProbeResult(SIGNAL, 0.85, [
            f"{len(mm)} 次网格多满行但仅消 1 行，如 {mm[0]}"
        ])
    return ProbeResult(NO_SIGNAL, 0.6, ["满行数与消行 count 一致"])


def probe_B09(rd: RoundData, ctx: dict) -> ProbeResult:
    pr = ctx["stats"]["pause_resume"]
    if pr["pause"] == 0:
        return ProbeResult(NO_EVIDENCE, 0.0, ["本局无 pause 事件"])
    if pr["resume"] == 0:
        ended = rd.meta.get("outcome", "?")
        return ProbeResult(SIGNAL, 0.9, [f"pause={pr['pause']} 而 resume=0，本局以 {ended} 收尾"])
    return ProbeResult(NO_SIGNAL, 0.6, [f"pause/resume={pr}"])


def probe_B10(rd: RoundData, ctx: dict) -> ProbeResult:
    ds = ctx["stats"]["hard_drop_distances"]
    if not ds:
        return ProbeResult(NO_EVIDENCE, 0.0, ["本局无 hard_drop 事件"])
    if all(d == 0 for d in ds):
        return ProbeResult(SIGNAL, 0.95, [f"{len(ds)} 次 hard_drop distance 恒为 0"])
    return ProbeResult(NO_SIGNAL, 0.6, [f"hard_drop distances={ds[:10]}..."])


def probe_B11(rd: RoundData, ctx: dict) -> ProbeResult:
    return ProbeResult(NO_EVIDENCE, 0.0, ["渲染残影埋点无法感知，需截图/文字反馈"])


def probe_B12(rd: RoundData, ctx: dict) -> ProbeResult:
    return ProbeResult(NO_EVIDENCE, 0.0, ["窗口位置埋点无法感知，需玩家文字反馈"])


PROBES = {f"B{i:02d}": fn for i, fn in enumerate(
    [probe_B01, probe_B02, probe_B03, probe_B04, probe_B05, probe_B06,
     probe_B07, probe_B08, probe_B09, probe_B10, probe_B11, probe_B12], start=1)}


def run_all_probes(rd: RoundData) -> dict:
    ctx = _context(rd)
    event_counts: dict[str, int] = {}
    for e in rd.events:
        event_counts[e["event"]] = event_counts.get(e["event"], 0) + 1
    probes = {key: {"status": r.status, "confidence": r.confidence, "evidence": r.evidence}
              for key, r in ((k, fn(rd, ctx)) for k, fn in PROBES.items())}
    return {
        "round_id": rd.meta.get("round_id", rd.round_dir.name),
        "meta": rd.meta,
        "n_events": len(rd.events),
        "event_counts": event_counts,
        "stats": ctx["stats"],
        "probes": probes,
        "replay": {
            "trust": ctx["replay"].trust,
            "grid_match": ctx["replay"].grid_match,
            "full_rows_mismatch": ctx["replay"].full_rows_mismatch,
            "overlaps_after_rotate": ctx["replay"].overlaps_after_rotate[:5],
            "spawn_collisions": ctx["replay"].spawn_collisions[:5],
        },
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python -m agent.tools.probes <run_dir>", file=sys.stderr)
        return 2
    report = run_all_probes(load_round(sys.argv[1]))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

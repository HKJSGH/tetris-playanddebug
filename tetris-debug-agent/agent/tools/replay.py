r"""replay — 按事件流重建网格（ReplayGrid），产出 B02/B04/B08 证据。

不 import game 模块：SHAPES 本地常量副本，与游戏代码解耦（修复过程会改
game/tetris_buggy.py，重放器必须独立存活）。

坐标系与事件协议（与 game/telemetry 一致）：
  cr = [c, r]（列, 行）；SHAPES 相对坐标 (dc, dr)
  spawn{piece, cr} → fall/move{action, cr}（cr 为移动后绝对位置）
  rotate{cr}（cr 不变）→ hard_drop{distance, cr} → lock{piece, cr}
消行模拟忠实于事件流：line_clear count=k → 清除最上面的满行 k 次
（与 buggy 行为同构；clean 时逐个满行清除，效果一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

C, R = 12, 20

SHAPES: dict[str, list[tuple[int, int]]] = {
    "O": [(-1, -1), (0, -1), (-1, 0), (0, 0)],
    "S": [(-1, 0), (0, 0), (0, -1), (1, -1)],
    "T": [(-1, 0), (0, 0), (0, -1), (1, 0)],
    "I": [(0, 1), (0, 0), (0, -1), (0, -2)],
    "L": [(-1, 0), (0, 0), (-1, -1), (-1, -2)],
    "J": [(-1, 0), (0, 0), (0, -1), (0, -2)],
    "Z": [(-1, -1), (0, -1), (0, 0), (1, 0)],
}


@dataclass
class Block:
    kind: str
    cells: list[tuple[int, int]]
    cr: list[int]

    def occupied(self) -> list[tuple[int, int]]:
        return [(self.cr[0] + dc, self.cr[1] + dr) for dc, dr in self.cells]


@dataclass
class ReplayResult:
    grid_final: list[list[str]] = field(default_factory=lambda: [[""] * C for _ in range(R)])
    trust: float = 0.0                     # 终局网格 vs game_state.json 一致率
    grid_match: bool = False
    overlaps_after_rotate: list[dict] = field(default_factory=list)   # B02 证据
    spawn_collisions: list[dict] = field(default_factory=list)        # B04 证据
    full_rows_mismatch: list[dict] = field(default_factory=list)      # B08 证据
    n_locks: int = 0
    n_replay_events: int = 0


def _rotate_cells(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(dr, -dc) for dc, dr in cells]


def _clear_top_full_rows(grid: list[list[str]], times: int) -> None:
    """清除最上面的满行 times 次，每次上方整体下移一格（与游戏逻辑同构）。"""
    for _ in range(times):
        for ri in range(R):
            if all(grid[ri]):
                for cur in range(ri, 0, -1):
                    grid[cur] = grid[cur - 1][:]
                grid[0] = [""] * C
                break
        else:
            break


def replay_events(events: list[dict], final_state: dict | None = None) -> ReplayResult:
    """重放事件流；final_state 传 game_state.json 内容时计算 trust。"""
    res = ReplayResult()
    grid = [[""] * C for _ in range(R)]
    current: Block | None = None

    for ev in events:
        res.n_replay_events += 1
        name = ev["event"]

        if name == "spawn":
            piece = ev["piece"]
            block = Block(kind=piece, cells=list(SHAPES[piece]), cr=list(ev["cr"]))
            cells = block.occupied()
            hits = [(c, r) for c, r in cells if 0 <= c < C and 0 <= r < R and grid[r][c]]
            if hits:
                res.spawn_collisions.append({"cr": block.cr, "piece": piece, "hits": hits})
            current = block

        elif name in ("fall", "move", "rotate", "hard_drop") and current is not None:
            if "cr" in ev:
                current.cr = list(ev["cr"])
            if name == "rotate":
                current.cells = _rotate_cells(current.cells)
                cells = current.occupied()
                hits = [
                    (c, r) for c, r in cells
                    if not (0 <= c < C and 0 <= r < R) or (r >= 0 and grid[r][c])
                ]
                if hits:
                    res.overlaps_after_rotate.append({
                        "cr": current.cr, "piece": current.kind,
                        "hits": [h for h in hits if 0 <= h[0] < C and 0 <= h[1] < R],
                        "out_of_bounds": any(not (0 <= c < C and 0 <= r < R) for c, r in hits),
                    })

        elif name == "lock":
            piece = ev["piece"]
            # lock 携带权威位置：重同步抵消重建漂移
            if current is None or current.kind != piece:
                current = Block(kind=piece, cells=list(SHAPES[piece]), cr=list(ev["cr"]))
            else:
                current.cr = list(ev["cr"])
            for c, r in current.occupied():
                if 0 <= c < C and 0 <= r < R:
                    grid[r][c] = piece
            res.n_locks += 1
            current = None
            full = [ri for ri in range(R) if all(grid[ri])]

        elif name == "line_clear":
            count = int(ev.get("count", 0))
            full = [ri for ri in range(R) if all(grid[ri])]
            if len(full) > count:
                res.full_rows_mismatch.append({
                    "grid_full_rows": len(full), "event_count": count,
                    "score_in_event": ev.get("score"),
                })
            _clear_top_full_rows(grid, count)

    res.grid_final = grid

    if final_state and "grid" in final_state:
        target = final_state["grid"]
        same = sum(
            1
            for ri in range(R)
            for ci in range(C)
            if (grid[ri][ci] or "") == (target[ri][ci] or "")
        )
        total = C * R
        res.trust = round(same / total, 4)
        res.grid_match = same == total

    return res

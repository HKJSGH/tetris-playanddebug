"""pipeline 全局配置：沙盒路径、模型路由、环境变量名。

红线：本包任何代码只允许读写 SANDBOX_ROOT 之内（io_paths 强制校验）；
data/gold/（truth_map、golden.json）仅 tester 门控与离线 eval 的纯代码可读，
绝不进任何 LLM prompt。
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """读取 KEY=VALUE 本地密钥文件（.env.local 已 gitignore），不覆盖已有环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


# ---- 沙盒路径 ---------------------------------------------------------------
SANDBOX_ROOT = Path(__file__).resolve().parents[1]      # tetris-debug-agent/
_load_env_file(SANDBOX_ROOT / ".env.local")
DATA_ROOT = SANDBOX_ROOT / "data"
RUNS_ROOT = DATA_ROOT / "runs"
CATALOG_PATH = DATA_ROOT / "catalog" / "catalog.yaml"
REFERENCE_DIR = DATA_ROOT / "reference"          # 基准截图（正确渲染对照，gen_reference 产出）
GOLD_DIR = DATA_ROOT / "gold"
TRUTH_MAP_PATH = GOLD_DIR / "truth_map.json"
GOLDEN_PATH = GOLD_DIR / "golden.json"
FIXES_PATH = DATA_ROOT / "fixes.json"
BACKUP_DIR = DATA_ROOT / "backup"
PATCH_HISTORY_PATH = DATA_ROOT / "patch_history.jsonl"   # 补丁尝试历史（append-only 审计账本）
EVAL_DIR = SANDBOX_ROOT / "eval"

# 待修目标（沙盒内）
TARGET_FILE = SANDBOX_ROOT / "game" / "tetris_buggy.py"
TESTS_DIR = SANDBOX_ROOT / "tests"

# ---- 模型路由（OpenAI 兼容协议，默认 OpenRouter；均可被环境变量覆盖） --------
# key 变量名沿用历史命名（.env.local 各放一个 OpenRouter key）；
# 换官方端点示例：TEXT_BASE_URL=https://api.deepseek.com TEXT_MODEL=deepseek-chat
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "https://openrouter.ai/api/v1")
VISION_API_KEY_ENV = os.environ.get("VISION_API_KEY_ENV", "DASHSCOPE_API_KEY")

TEXT_MODEL = os.environ.get("TEXT_MODEL", "deepseek/deepseek-chat")
TEXT_BASE_URL = os.environ.get("TEXT_BASE_URL", "https://openrouter.ai/api/v1")
TEXT_API_KEY_ENV = os.environ.get("TEXT_API_KEY_ENV", "DEEPSEEK_API_KEY")

# ---- pipeline 参数 ----------------------------------------------------------
PATCH_BUDGET_PER_HYPOTHESIS = 3   # 单假设补丁预算：用完即放弃该假设换下一个
PATCH_BUDGET_TOTAL = 20           # 单局补丁总预算：所有假设共享，用完即收场
GIVEUP_REPEATS = 3                # 同一失败原因在该假设尝试历史中重复 N 次 → 提前放弃当前假设
MAX_REDIAG_ROUNDS = 1             # 当前假设清单已检验完后，携带失败实证重诊断的轮数上限
MAX_HYPOTHESES = 5          # 每批假设清单最多推进的假设数（重诊断新批次同样适用）
REJECTED_PRIOR_LIMIT = 10         # 跨局记忆 rejected_prior 注入上限（取最近 N 条）
LESSON_MAX_CHARS = 120            # 失败改法教训单条长度上限
PYTEST_TIMEOUT = 600        # tester 跑 pytest 超时（秒）

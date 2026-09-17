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
GOLD_DIR = DATA_ROOT / "gold"
TRUTH_MAP_PATH = GOLD_DIR / "truth_map.json"
GOLDEN_PATH = GOLD_DIR / "golden.json"
FIXES_PATH = DATA_ROOT / "fixes.json"
BACKUP_DIR = DATA_ROOT / "backup"
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
MAX_PATCH_ATTEMPTS = 3      # 单假设补丁重试上限
MAX_HYPOTHESES = 5          # 每局最多推进的假设数
PYTEST_TIMEOUT = 300        # tester 跑 pytest 超时（秒）

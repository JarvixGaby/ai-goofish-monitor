"""
配置加载模块。

从 .env 文件或环境变量读取 AI 客户端和词库相关配置。
优先级：.env 文件 > 环境变量 > 默认值。

用法：
    from config import get_settings
    s = get_settings()
    print(s.ai_base_url)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# python-dotenv：若存在 .env 则自动加载
try:
    from dotenv import load_dotenv as _load_dotenv

    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        _load_dotenv(_env_path, override=False)
except ImportError:
    pass  # python-dotenv 未安装时静默忽略


@dataclass(frozen=True)
class Settings:
    # --- AI 接入（轻量任务：分类/关键词评估/词库学习） ---
    ai_base_url: str           # OpenAI 兼容接口地址（中转 API 填 /v1 结尾）
    ai_api_key: str            # API Key
    ai_model: str              # 轻量模型（默认 haiku，量大省钱）

    # --- AI 接入（深度分析：选品建议/高销量分析/赛道发现） ---
    ai_analysis_model: str     # 分析模型（默认 sonnet，调用少但需要洞察力）

    # --- 词库学习阈值 ---
    vocab_auto_threshold: float    # 置信度 >= 此值自动加入词库，默认 0.85
    vocab_review_threshold: float  # 置信度 >= 此值写入 pending_review，默认 0.60

    # --- 路径 ---
    vocab_dir: Path                # 词库目录，默认 gap_scanner/vocab/

    # --- AI 请求参数 ---
    ai_timeout: int                # 请求超时（秒），默认 30
    ai_max_retries: int            # 最大重试次数，默认 2

    # --- 详情页采集 ---
    detail_limit: int              # 单次采集最大详情页数，默认 20
    detail_interval_min: float     # 详情页间隔下限（秒），默认 10
    detail_interval_max: float     # 详情页间隔上限（秒），默认 20


def get_settings() -> Settings:
    """
    加载并返回 Settings 实例。

    必填项（缺失时抛出 ValueError）：
        AI_BASE_URL, AI_API_KEY, AI_MODEL

    可选项（有默认值）：
        VOCAB_AUTO_ADD_THRESHOLD, VOCAB_REVIEW_THRESHOLD,
        VOCAB_DIR, AI_TIMEOUT, AI_MAX_RETRIES
    """
    # 必填项
    base_url = os.getenv("AI_BASE_URL", "").strip()
    api_key = os.getenv("AI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()

    # 中转 API 支持：也接受 ANTHROPIC_* 前缀（用户 .zshrc 中已有）
    if not base_url:
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if not api_key:
        api_key = (
            os.getenv("ANTHROPIC_AUTH_TOKEN", "")
            or os.getenv("OPENAI_API_KEY", "")
        ).strip()
    if not model:
        model = os.getenv("OPENAI_MODEL_NAME", "claude-sonnet-4-5-20250929").strip()

    missing = [name for name, val in [
        ("AI_BASE_URL / ANTHROPIC_BASE_URL", base_url),
        ("AI_API_KEY / ANTHROPIC_AUTH_TOKEN", api_key),
    ] if not val]
    if missing:
        raise ValueError(
            f"缺少必要的 AI 配置：{', '.join(missing)}\n"
            "请在 gap_scanner/.env 中填写，参考 .env.example"
        )

    analysis_model = os.getenv("AI_ANALYSIS_MODEL", "").strip()
    if not analysis_model:
        analysis_model = "claude-sonnet-4-5-20250929"

    # 可选项
    auto_threshold = float(os.getenv("VOCAB_AUTO_ADD_THRESHOLD", "0.85"))
    review_threshold = float(os.getenv("VOCAB_REVIEW_THRESHOLD", "0.60"))
    vocab_dir = Path(os.getenv("VOCAB_DIR", str(Path(__file__).parent / "vocab")))
    ai_timeout = int(os.getenv("AI_TIMEOUT", "30"))
    ai_max_retries = int(os.getenv("AI_MAX_RETRIES", "2"))
    detail_limit = int(os.getenv("DETAIL_LIMIT", "20"))
    detail_interval_min = float(os.getenv("DETAIL_INTERVAL_MIN", "10"))
    detail_interval_max = float(os.getenv("DETAIL_INTERVAL_MAX", "20"))

    return Settings(
        ai_base_url=base_url,
        ai_api_key=api_key,
        ai_model=model,
        ai_analysis_model=analysis_model,
        vocab_auto_threshold=auto_threshold,
        vocab_review_threshold=review_threshold,
        vocab_dir=vocab_dir,
        ai_timeout=ai_timeout,
        ai_max_retries=ai_max_retries,
        detail_limit=detail_limit,
        detail_interval_min=detail_interval_min,
        detail_interval_max=detail_interval_max,
    )

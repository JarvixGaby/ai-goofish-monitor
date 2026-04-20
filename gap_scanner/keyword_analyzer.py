"""
AI 关键词质量评估与细分建议。

在每次关键词扫描完成后，评估搜索结果标题与关键词（虚拟商品意图）的相关度，
并在结果接近满页时建议更精准的子关键词。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ai_client import AIClient, AIClientError

logger = logging.getLogger(__name__)

KEYWORD_STATUS_FILENAME = "keyword_status.json"


@dataclass
class KeywordEvaluation:
    keyword: str
    status: str  # "valid" | "noisy" | "invalid"
    relevance_score: float
    reason: str
    suggested_alternatives: list[str]


_EVAL_SYSTEM_PROMPT = """\
你是中国二手平台「闲鱼」的虚拟商品（教程、资料、网盘、代做等数字内容）检索质量分析助手。

任务：根据用户搜索词与搜索结果的前若干条商品标题，判断这些标题是否与该搜索词的「虚拟商品」意图相关。

虚拟商品意图：用户想找的是可下载/可在线交付的教程、资料、模板、课程、代做服务等，而不是无关实体书、小说、完全不相关品类。

请只依据给定标题判断整体相关度，输出 JSON：
{
  "relevance_score": 0.0 到 1.0 的小数,
  "reason": "一句话说明判断依据",
  "suggested_alternatives": ["若相关度偏低，给出 1-3 个更具体、更易搜到虚拟商品的替代关键词；若相关度高可为空数组"]
}

评分参考：
- 多数标题明显与搜索词意图无关（如搜教程却全是小说/无关实物）→ 低分
- 部分相关、混杂噪音 → 中分
- 多数标题符合虚拟商品/教程资料类意图 → 高分

只输出 JSON 对象，不要其它文字。"""


_SUBDIV_SYSTEM_PROMPT = """\
你是闲鱼虚拟商品选品与搜索词优化助手。

用户搜索某关键词后，返回了接近满页的商品。请根据下列商品标题的共性/细分方向，
建议 2-3 个更精准、更易筛出目标虚拟商品的子关键词（中文，适合直接用于闲鱼搜索）。

输出 JSON：
{
  "subdivisions": ["子关键词1", "子关键词2", "子关键词3"]
}

子关键词应互不重复、比原词更具体；只输出 JSON。"""


def _status_from_score(score: float) -> str:
    if score < 0.3:
        return "invalid"
    if score <= 0.6:
        return "noisy"
    return "valid"


def _titles_preview(items: list[dict], limit: int = 10) -> list[str]:
    out: list[str] = []
    for item in items[:limit]:
        t = item.get("title")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


async def evaluate_keyword_relevance(
    keyword: str,
    items: list[dict],
    client: AIClient,
) -> KeywordEvaluation:
    """
    用 AI 判断搜索结果标题与关键词的虚拟商品意图相关度。
    """
    titles = _titles_preview(items, 10)
    if not titles:
        return KeywordEvaluation(
            keyword=keyword,
            status="valid",
            relevance_score=1.0,
            reason="无搜索结果，跳过相关度判断。",
            suggested_alternatives=[],
        )

    user_msg = (
        f"搜索关键词：{keyword!r}\n\n"
        f"以下是最多 10 条商品标题（按列表顺序）：\n"
        + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    )

    try:
        response = await client.chat(_EVAL_SYSTEM_PROMPT, user_msg)
        score = float(response.get("relevance_score", 0.5))
        score = max(0.0, min(1.0, score))
        reason = str(response.get("reason", "")).strip() or "（模型未给出原因）"
        alts = response.get("suggested_alternatives", [])
        if not isinstance(alts, list):
            alts = []
        alts = [str(a).strip() for a in alts if str(a).strip()][:5]
        status = _status_from_score(score)
        return KeywordEvaluation(
            keyword=keyword,
            status=status,
            relevance_score=round(score, 4),
            reason=reason,
            suggested_alternatives=alts,
        )
    except (AIClientError, ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"[keyword_analyzer] 关键词相关度评估失败：{e}")
        return KeywordEvaluation(
            keyword=keyword,
            status="valid",
            relevance_score=0.75,
            reason=f"评估请求失败，已跳过标记：{e}",
            suggested_alternatives=[],
        )
    except Exception as e:
        logger.warning(f"[keyword_analyzer] 关键词相关度评估意外错误：{e}")
        return KeywordEvaluation(
            keyword=keyword,
            status="valid",
            relevance_score=0.75,
            reason=f"评估异常，已跳过标记：{e}",
            suggested_alternatives=[],
        )


async def suggest_subdivisions(
    keyword: str,
    items: list[dict],
    client: AIClient,
) -> list[str]:
    """
    当搜索结果条数接近满页时，建议 2-3 个更精准的子关键词。
    """
    titles = _titles_preview(items, 15)
    if not titles:
        return []

    user_msg = (
        f"原关键词：{keyword!r}\n\n"
        f"当前页代表性标题（至多 15 条）：\n"
        + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    )

    try:
        response = await client.chat(_SUBDIV_SYSTEM_PROMPT, user_msg)
        subs = response.get("subdivisions", [])
        if not isinstance(subs, list):
            return []
        out = [str(s).strip() for s in subs if str(s).strip()]
        # 去重保序，最多 3 个
        seen: set[str] = set()
        unique: list[str] = []
        for s in out:
            if s not in seen:
                seen.add(s)
                unique.append(s)
            if len(unique) >= 3:
                break
        return unique[:3]
    except (AIClientError, ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"[keyword_analyzer] 细分建议失败：{e}")
        return []
    except Exception as e:
        logger.warning(f"[keyword_analyzer] 细分建议意外错误：{e}")
        return []


def load_keyword_status(vocab_dir: Path) -> dict:
    """读取 vocab/keyword_status.json；不存在则返回空 dict。"""
    path = vocab_dir / KEYWORD_STATUS_FILENAME
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[keyword_analyzer] 读取 keyword_status 失败：{e}")
        return {}


def save_keyword_status(vocab_dir: Path, status: dict) -> None:
    """将关键词评估状态写入 vocab/keyword_status.json。"""
    vocab_dir.mkdir(parents=True, exist_ok=True)
    path = vocab_dir / KEYWORD_STATUS_FILENAME
    path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

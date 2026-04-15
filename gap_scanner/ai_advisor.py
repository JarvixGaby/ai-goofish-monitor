"""
AI 选品顾问模块（Phase 2）。

对缺口分排名 Top N 的关键词，进行竞品分析并生成差异化的上架文案建议。

功能：
    1. 分析竞品现有标题的切入角度（避免同质化竞争）
    2. 提出 3 条差异化推荐标题
    3. 生成商品描述
    4. 给出定价建议和理由

依赖：ai_client.AIClient
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ai_client import AIClient, AIClientError

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一位专注于中国二手交易平台「闲鱼」的资深卖家顾问，尤其擅长虚拟商品（教程/资料/服务）的选品与差异化定位。

你的任务：分析某个关键词的市场缺口，给出具体可执行的上架建议。

请以卖家视角分析：
1. 竞品现有的主要切入角度（总结规律，2-3 点）
2. 差异化机会点（哪个角度竞争较少但需求明确）
3. 3 条推荐标题（融合「闲鱼黑话」，如秒发、保姆级、手把手等）
4. 一段商品描述（50-100字，自然、有说服力）
5. 定价建议和逻辑（参考竞品均价，给出入场价区间）

「闲鱼黑话」参考：秒发、百度云、保姆级教程、手把手带你做、从0到1、永久更新、附送售后群、一对一答疑

请以 JSON 格式返回：
{{
  "competitor_analysis": "竞品分析摘要（2-3句话）",
  "differentiation": "差异化机会点（1-2句话）",
  "recommended_titles": [
    "标题1（含黑话，40字以内）",
    "标题2",
    "标题3"
  ],
  "recommended_description": "商品描述正文（50-100字）",
  "pricing_rationale": "定价建议，如：竞品均价¥XX，建议首发¥XX-YY，后期稳定到¥ZZ"
}}
"""


@dataclass
class OpportunityAdvice:
    """单个关键词的 AI 选品建议。"""
    keyword: str
    competitor_analysis: str
    differentiation: str
    recommended_titles: list[str] = field(default_factory=list)
    recommended_description: str = ""
    pricing_rationale: str = ""
    error: str = ""   # 生成失败时记录原因，其他字段保持默认

    @property
    def has_content(self) -> bool:
        return bool(self.competitor_analysis and not self.error)


async def generate_advice(
    keyword: str,
    gap_data: dict,
    client: AIClient,
) -> OpportunityAdvice:
    """
    为单个关键词生成选品建议。

    参数：
        keyword:   搜索关键词
        gap_data:  calculate_gap() 返回的缺口数据字典
        client:    AIClient 实例
    """
    # 构建用户消息
    top_titles = gap_data.get("top_titles", [])
    titles_block = "\n".join(f"  - {t}" for t in top_titles) if top_titles else "  （无竞品数据）"

    user_msg = f"""关键词：「{keyword}」

市场数据：
- 缺口分：{gap_data.get('gap_score', 0)}（需求帖 {gap_data.get('demand_posts', 0)} 个 / 虚拟供给 {gap_data.get('virtual_supply', 0)} 个）
- 总挂牌量：{gap_data.get('total_listings', 0)} 条
- 竞品均价：¥{gap_data.get('avg_price', 0):.0f}
- 竞品平均想要数：{gap_data.get('avg_want', 0):.0f}
- 建议定价区间：{gap_data.get('suggested_price', '待分析')}

竞品代表性标题（前5条）：
{titles_block}

请根据以上数据，给出差异化的上架建议。"""

    try:
        response = await client.chat(_SYSTEM_PROMPT, user_msg)
        return _parse_advice(keyword, response)
    except AIClientError as e:
        logger.warning(f"[advisor] 「{keyword}」生成建议失败：{e}")
        return OpportunityAdvice(keyword=keyword, competitor_analysis="", differentiation="", error=str(e))
    except Exception as e:
        logger.warning(f"[advisor] 意外错误：{e}")
        return OpportunityAdvice(keyword=keyword, competitor_analysis="", differentiation="", error=str(e))


def _parse_advice(keyword: str, response: dict) -> OpportunityAdvice:
    """解析 AI 返回的建议 JSON。"""
    try:
        titles = response.get("recommended_titles", [])
        if not isinstance(titles, list):
            titles = []

        return OpportunityAdvice(
            keyword=keyword,
            competitor_analysis=str(response.get("competitor_analysis", "")),
            differentiation=str(response.get("differentiation", "")),
            recommended_titles=titles[:3],   # 最多取3条
            recommended_description=str(response.get("recommended_description", "")),
            pricing_rationale=str(response.get("pricing_rationale", "")),
        )
    except Exception as e:
        return OpportunityAdvice(
            keyword=keyword,
            competitor_analysis="",
            differentiation="",
            error=f"解析失败：{e}",
        )


async def generate_advice_for_top(
    top_gaps: list[dict],
    client: AIClient,
) -> dict[str, OpportunityAdvice]:
    """
    批量为 Top N 缺口关键词生成建议（串行执行，避免并发触发限速）。

    返回：{keyword: OpportunityAdvice} 字典
    """
    result: dict[str, OpportunityAdvice] = {}
    for gap in top_gaps:
        kw = gap.get("keyword", "")
        if not kw:
            continue
        print(f"  [advisor] 分析「{kw}」...")
        advice = await generate_advice(kw, gap, client)
        result[kw] = advice
    return result

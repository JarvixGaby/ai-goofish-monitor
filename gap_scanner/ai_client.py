"""
统一 AI 客户端模块。

封装对 OpenAI 兼容接口的调用，支持：
- 用户的 cc.codesome.ai 中转 API（Anthropic 兼容）
- OpenAI 官方接口
- 任何 OpenAI 兼容接口（ModelScope、硅基流动等）

所有 AI 模块（classifier、learner、advisor）都通过本模块发起请求。
"""

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError

from config import Settings

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """AI 调用失败时抛出，业务层可捕获并降级处理。"""


class AIClient:
    """
    异步 AI 客户端。

    用法：
        client = AIClient(settings)
        result = await client.chat(system="...", user="...")
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # 处理 base_url：确保以 /v1 结尾（部分中转 API 需要）
        base_url = settings.ai_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = base_url + "/v1"

        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=settings.ai_api_key,
            timeout=settings.ai_timeout,
            max_retries=0,          # 重试逻辑由本类自己控制
        )

    async def chat(
        self,
        system: str,
        user: str,
        response_format: str = "json",
    ) -> dict[str, Any]:
        """
        发送 chat 请求，返回解析后的 JSON 字典。

        参数：
            system: 系统提示词
            user:   用户消息
            response_format: "json"（默认）或 "text"

        返回：解析后的字典（response_format="json"）或 {"text": "..."} 格式

        异常：AIClientError — 重试后仍失败时抛出
        """
        last_error: Exception | None = None

        for attempt in range(self._settings.ai_max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._settings.ai_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                if response_format == "json":
                    kwargs["response_format"] = {"type": "json_object"}

                response = await self._client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content or ""

                if response_format == "json":
                    return self._parse_json(content)
                else:
                    return {"text": content}

            except APITimeoutError as e:
                last_error = e
                logger.warning(f"[ai_client] 请求超时（第 {attempt + 1} 次）")
                if attempt < self._settings.ai_max_retries:
                    await asyncio.sleep(2 ** attempt)  # 指数退避

            except APIError as e:
                last_error = e
                logger.warning(f"[ai_client] API 错误（第 {attempt + 1} 次）：{e}")
                if attempt < self._settings.ai_max_retries:
                    # 429 并发/限流错误需要更长等待；其它错误用常规指数退避
                    err_str = str(e)
                    if "429" in err_str or "rate_limit" in err_str.lower():
                        # 带抖动的长退避：10s / 30s / 60s
                        import random
                        wait = (10, 30, 60)[min(attempt, 2)] + random.uniform(0, 5)
                        logger.warning(f"[ai_client] 限流，等待 {wait:.1f}s 后重试")
                        await asyncio.sleep(wait)
                    else:
                        await asyncio.sleep(2 ** attempt)

            except json.JSONDecodeError as e:
                # JSON 解析失败：不重试，直接抛出
                raise AIClientError(f"AI 返回了非 JSON 内容：{e}") from e

        raise AIClientError(
            f"AI 请求失败（重试 {self._settings.ai_max_retries} 次）：{last_error}"
        )

    async def chat_text(self, system: str, user: str) -> str:
        """
        发送 chat 请求，返回纯文本内容（不要求 JSON）。
        """
        result = await self.chat(system, user, response_format="text")
        return result.get("text", "")

    def _parse_json(self, content: str) -> dict[str, Any]:
        """
        解析 AI 返回的 JSON 字符串。

        容错策略：
            1. 去除 markdown 代码块包装（```json ... ```）
            2. 提取第一个 { ... } 块（处理 AI 在 JSON 后追加解释文本的情况）
            3. 标准 json.loads()
        """
        content = content.strip()

        # 去除 markdown 代码块包装
        if content.startswith("```"):
            lines = content.splitlines()
            inner = "\n".join(
                line for line in lines[1:]
                if not line.strip().startswith("```")
            )
            content = inner.strip()

        # 先尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 提取第一个完整的 JSON 对象（处理 AI 在 JSON 后加说明文字）
        brace_start = content.find("{")
        if brace_start >= 0:
            depth = 0
            in_string = False
            escape_next = False
            for i, ch in enumerate(content[brace_start:], start=brace_start):
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\":
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = content[brace_start:i + 1]
                        return json.loads(candidate)

        raise json.JSONDecodeError("无法提取有效 JSON 对象", content, 0)


def build_client_optional(settings: Settings | None) -> AIClient | None:
    """
    尝试构建 AIClient（轻量模型）；若 settings 为 None 或缺少必要配置，返回 None。
    """
    if settings is None:
        return None
    if not settings.ai_base_url or not settings.ai_api_key:
        return None
    return AIClient(settings)


def build_analysis_client(settings: Settings) -> AIClient:
    """
    构建使用 ai_analysis_model（如 sonnet）的 AIClient，用于深度分析任务。
    与轻量客户端共享 base_url/api_key，只是模型不同。
    """
    from dataclasses import replace
    analysis_settings = replace(settings, ai_model=settings.ai_analysis_model)
    return AIClient(analysis_settings)

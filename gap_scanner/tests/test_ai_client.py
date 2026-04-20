"""
测试 ai_client.py

使用 pytest-asyncio + unittest.mock 模拟 HTTP 响应，不发起真实 API 调用。
"""

import json
import pytest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ai_client import AIClient, AIClientError, build_client_optional
from config import Settings


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    defaults = dict(
        ai_base_url="https://test.api.example.com",
        ai_api_key="sk-test-key",
        ai_model="test-model",
        ai_analysis_model="test-analysis-model",
        vocab_auto_threshold=0.85,
        vocab_review_threshold=0.60,
        vocab_dir=Path("/tmp/vocab"),
        ai_timeout=5,
        ai_max_retries=1,
        detail_limit=20,
        detail_interval_min=10.0,
        detail_interval_max=20.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_choice(content: str):
    """构造 OpenAI choices[0].message.content 结构。"""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ─────────────────────────────────────────────
# 基础调用测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_returns_parsed_json():
    settings = make_settings()
    client = AIClient(settings)
    payload = {"result": "ok", "items": [1, 2, 3]}

    with patch.object(
        client._client.chat.completions, "create",
        new_callable=AsyncMock,
        return_value=make_choice(json.dumps(payload)),
    ):
        result = await client.chat("system", "user")

    assert result == payload


@pytest.mark.asyncio
async def test_chat_strips_markdown_code_block():
    """AI 返回 ```json ... ``` 包装时应正确解析。"""
    settings = make_settings()
    client = AIClient(settings)
    wrapped = '```json\n{"key": "value"}\n```'

    with patch.object(
        client._client.chat.completions, "create",
        new_callable=AsyncMock,
        return_value=make_choice(wrapped),
    ):
        result = await client.chat("system", "user")

    assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_chat_text_returns_string():
    settings = make_settings()
    client = AIClient(settings)

    with patch.object(
        client._client.chat.completions, "create",
        new_callable=AsyncMock,
        return_value=make_choice("hello world"),
    ):
        result = await client.chat_text("system", "user")

    assert result == "hello world"


# ─────────────────────────────────────────────
# 重试测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_on_api_error_succeeds():
    """第一次 API 错误，第二次成功。"""
    from openai import APIError, APIStatusError

    settings = make_settings(ai_max_retries=1)
    client = AIClient(settings)
    success_resp = make_choice('{"ok": true}')

    call_count = 0

    async def fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 构造一个最简单的 APIError 子类
            err = APIError("test error", request=MagicMock(), body=None)
            raise err
        return success_resp

    with patch.object(
        client._client.chat.completions, "create",
        side_effect=fake_create,
    ):
        result = await client.chat("system", "user")

    assert result == {"ok": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_raises_after_all_retries():
    """超过最大重试次数后抛出 AIClientError。"""
    from openai import APIError

    settings = make_settings(ai_max_retries=1)
    client = AIClient(settings)

    async def always_fail(**kwargs):
        raise APIError("always fail", request=MagicMock(), body=None)

    with patch.object(
        client._client.chat.completions, "create",
        side_effect=always_fail,
    ):
        with pytest.raises(AIClientError):
            await client.chat("system", "user")


# ─────────────────────────────────────────────
# JSON 解析失败测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_json_raises_error():
    """AI 返回非 JSON 内容时抛出 AIClientError。"""
    settings = make_settings()
    client = AIClient(settings)

    with patch.object(
        client._client.chat.completions, "create",
        new_callable=AsyncMock,
        return_value=make_choice("这不是 JSON 内容"),
    ):
        with pytest.raises(AIClientError, match="非 JSON"):
            await client.chat("system", "user")


# ─────────────────────────────────────────────
# base_url 处理测试
# ─────────────────────────────────────────────

def test_base_url_gets_v1_appended():
    """base_url 不以 /v1 结尾时应自动追加。"""
    settings = make_settings(ai_base_url="https://cc.codesome.ai")
    client = AIClient(settings)
    assert str(client._client.base_url).rstrip("/").endswith("/v1")


def test_base_url_v1_not_duplicated():
    """base_url 已有 /v1 时不应重复追加。"""
    settings = make_settings(ai_base_url="https://api.openai.com/v1")
    client = AIClient(settings)
    url = str(client._client.base_url).rstrip("/")
    assert url.endswith("/v1")
    assert "/v1/v1" not in url


# ─────────────────────────────────────────────
# build_client_optional 测试
# ─────────────────────────────────────────────

def test_build_client_optional_returns_none_for_none():
    assert build_client_optional(None) is None


def test_build_client_optional_returns_none_when_no_key():
    settings = make_settings(ai_api_key="")
    assert build_client_optional(settings) is None


def test_build_client_optional_returns_client():
    settings = make_settings()
    client = build_client_optional(settings)
    assert isinstance(client, AIClient)

"""OpenAI 兼容翻译接口。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx

from gsuid_core.logger import logger

from .api import USER_AGENT, get_http_client
from ..xanalyse_config import XAnalyseSettings


def _object(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _translation_from_payload(payload: object) -> str | None:
    root = _object(payload)
    if root is None or "choices" not in root:
        return None
    choices = root["choices"]
    if not isinstance(choices, list) or not choices:
        return None
    first = _object(choices[0])
    if first is None or "message" not in first:
        return None
    message = _object(first["message"])
    if message is None or "content" not in message:
        return None
    content = message["content"]
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            item_obj = _object(item)
            if item_obj is not None and "text" in item_obj and isinstance(item_obj["text"], str):
                parts.append(item_obj["text"])
        result = "".join(parts).strip()
        return result or None
    return None


async def translate_text(
    text: str,
    settings: XAnalyseSettings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """翻译文本；失败时返回原文，避免解析结果被错误提示覆盖。"""

    if not text or not settings.translate_enabled or not settings.api_key:
        return text
    if not settings.api_url or not settings.model:
        logger.warning("[XAnalyse] 翻译配置不完整，跳过翻译")
        return text

    endpoint = f"{settings.api_url.rstrip('/')}/chat/completions"
    payload: dict[str, object] = {
        "model": settings.model,
        "messages": [
            {
                "role": "user",
                "content": settings.prompt.replace("{text}", text),
            }
        ],
        "stream": False,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    active_client = client or await get_http_client(settings.proxy)
    timeout = httpx.Timeout(8.0, connect=5.0, pool=3.0)
    for attempt in range(settings.translate_retries):
        try:
            response = await active_client.post(endpoint, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            translated = _translation_from_payload(response.json())
            if translated is None:
                raise ValueError("翻译接口返回中缺少 choices[0].message.content")
            return translated
        except (httpx.HTTPError, ValueError) as error:
            if attempt + 1 >= settings.translate_retries:
                logger.warning(f"[XAnalyse] 翻译失败，返回原文：{error}")
                break
            await asyncio.sleep(min(float(attempt + 1), 5.0))
    return text

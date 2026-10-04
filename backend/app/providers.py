"""Protocol adapters: both providers produce the same delta/complete events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from .config import Settings
from .request_policy import Limiter, allow_cloud


class ProviderError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


async def check_response(response: httpx.Response, label="模型"):
    if response.is_error:
        # Do not expose raw provider bodies, which can echo credentials or requests.
        code = response.status_code
        hints = {
            400: "检查模型 ID、请求参数及输出上限",
            401: "检查 API Key",
            403: "检查服务权限",
            404: "检查基础地址和模型 ID",
            429: "额度或频率受限，请稍后重试",
        }
        raise ProviderError(
            f"{label}返回 HTTP {code}：{hints.get(code, '上游服务暂不可用')}",
            retryable=code in {408, 429, 500, 502, 503, 504},
        )


class Provider:
    def __init__(self, client: httpx.AsyncClient, oauth=None, store=None):
        self.client, self.oauth, self.store = client, oauth, store
        self.limiter = Limiter()

    async def stream(
        self, cfg: Settings, messages: list, tools: list, system: str
    ) -> AsyncIterator[dict]:
        if cfg.plugin_max_output_tokens is not None:
            cfg = cfg.model_copy(update={"max_tokens": cfg.plugin_max_output_tokens})
        has_images = any(m.get("attachments") for m in messages)
        requested = (cfg.vision_model if has_images else None) or cfg.fallback_primary or cfg.model
        models = (
            [requested] if has_images else list(dict.fromkeys([requested, *cfg.fallback_models]))
        )
        last_error = None
        for index, model in enumerate(models):
            attempt_cfg = cfg.model_copy(update={"model": model})
            route = cfg.provider_routes.get(model)
            import os

            from dotenv import dotenv_values

            env = {
                **(dotenv_values(self.store.root / ".env") if self.store else {}),
                **(getattr(self.store, "runtime_environment", {}) if self.store else {}),
                **os.environ,
            }
            if route:
                key = env.get(route.api_key_env)
                if not key:
                    if index == 0:
                        raise ProviderError("主模型缺少环境变量凭据：" + route.api_key_env)
                    yield {
                        "type": "provider_fallback",
                        "model": model,
                        "status": "skipped",
                        "reason": "missing_credential_reference",
                    }
                    continue
                if self.store:
                    self.store.extra_secrets.add(key)
                attempt_cfg = attempt_cfg.model_copy(
                    update={
                        "provider": route.provider,
                        "base_url": route.base_url,
                        "api_key": key,
                        "auth_mode": "api-key",
                        "model": route.model or model,
                    }
                )
            else:
                from .model_routing import route as resolve_route

                try:
                    attempt_cfg = resolve_route(attempt_cfg, env)
                except ValueError:
                    if index == 0:
                        raise
                    yield {
                        "type": "provider_fallback",
                        "model": model,
                        "status": "skipped",
                        "reason": "invalid_or_missing_route",
                    }
                    continue
            if self.store and attempt_cfg.api_key:
                self.store.extra_secrets.add(attempt_cfg.api_key)
            emitted = False
            try:
                async for event in self._stream_one(attempt_cfg, messages, tools, system):
                    emitted = True
                    yield event
                return
            except (httpx.TransportError, ProviderError) as error:
                retryable = isinstance(error, httpx.TransportError) or error.retryable
                if emitted or not retryable or index + 1 == len(models):
                    raise
                last_error = error
                yield {
                    "type": "provider_fallback",
                    "model": model,
                    "next_model": models[index + 1],
                    "status": "retryable_failure",
                }
        raise last_error or ProviderError("没有可用的模型连接")

    async def _stream_one(self, cfg, messages, tools, system):
        from .telemetry import record_http

        if cfg.model in cfg.model_aliases:
            cfg = cfg.model_copy(update={"model": cfg.model_aliases[cfg.model]})
        if cfg.auth_mode == "oauth":
            if self.oauth is None:
                raise ProviderError("OAuth 服务不可用")
            cfg = cfg.model_copy(update={"api_key": await self.oauth.access_token()})
        if not cfg.api_key:
            raise ProviderError("请在配置页面填写对话模型 API Key")
        allow_cloud(cfg, cfg.base_url)
        if any(m.get("attachments") for m in messages):
            if not self.store:
                raise ValueError("附件存储不可用")
            from types import SimpleNamespace

            from .db import Database
            from .jobs import Jobs
            from .media import Media

            media = Media(
                SimpleNamespace(
                    store=self.store,
                    jobs=Jobs(
                        Database(self.store.data / "app.sqlite", recover=False),
                        self.store,
                    ),
                )
            )
            messages = [
                {
                    **m,
                    "content": (
                        [{"type": "text", "text": m.get("content") or "请分析图片"}]
                        + [media.image_content(key, cfg.provider) for key in m["attachments"]]
                    ),
                }
                if m.get("attachments")
                else m
                for m in messages
            ]
        adapter = self._anthropic if cfg.provider == "anthropic" else self._openai
        for attempt in range(cfg.provider_retries + 1):
            emitted = False
            path = "/messages" if cfg.provider == "anthropic" else "/chat/completions"
            record_http("started", attempt, path, model=cfg.model, provider=cfg.provider)
            try:
                async with self.limiter.slot(cfg):
                    async for event in adapter(cfg, messages, tools, system):
                        emitted = True
                        if event["type"] == "complete":
                            event = {**event, "model": cfg.model, "provider": cfg.provider}
                        yield event
                record_http("succeeded", attempt, path, model=cfg.model)
                return
            except (httpx.TransportError, ProviderError) as error:
                retryable = isinstance(error, httpx.TransportError) or error.retryable
                record_http(
                    "failed",
                    attempt,
                    path,
                    error=type(error).__name__,
                    retryable=retryable,
                    output_started=emitted,
                )
                if emitted or not retryable or attempt >= cfg.provider_retries:
                    raise
                await asyncio.sleep(min(2**attempt, 8))

    async def _openai(self, cfg, messages, tools, system):
        from .sse import data_frames

        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                *[
                    {
                        key: value
                        for key, value in message.items()
                        if key in {"role", "content", "tool_calls", "tool_call_id", "name"}
                        and not (key == "name" and not value)
                    }
                    for message in messages
                ],
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        token_key = "max_completion_tokens" if cfg.model.startswith("gpt-5") else "max_tokens"
        payload[token_key] = (
            min(cfg.max_tokens, 32768) if cfg.model == "qwen-flash" else cfg.max_tokens
        )
        canonical = cfg.model.lower().rsplit("/", 1)[-1]
        if not (
            canonical.startswith(("gpt-5", "o1", "o3", "o4", "qwen-qwq", "qwq"))
            or canonical == "grok-3-mini"
            or "thinking" in canonical
        ):
            payload["temperature"] = cfg.temperature
        if cfg.reasoning_effort:
            payload["reasoning_effort"] = cfg.reasoning_effort
        if canonical.startswith("deepseek-v4"):
            payload["thinking"] = {"type": "enabled"}
            for source, target in zip(messages, payload["messages"][1:]):
                if source["role"] == "assistant":
                    target["reasoning_content"] = "".join(
                        b.get("thinking", "") for b in source.get("thinking", [])
                    )
        if tools:
            payload["tools"] = tools
        text, calls, usage, stop, thinking = "", {}, {}, None, ""
        async with self.client.stream(
            "POST",
            cfg.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + cfg.api_key},
            json=payload,
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=cfg.connect_timeout_seconds),
        ) as response:
            await check_response(response)
            async for raw in data_frames(response):
                raw = raw.strip()
                if raw == "[DONE]":
                    break
                if not raw:
                    continue
                obj = json.loads(raw)
                if obj.get("error"):
                    raise ProviderError("模型在流式响应中返回错误")
                if obj.get("usage"):
                    cached = obj["usage"].get("prompt_tokens_details", {}).get("cached_tokens", 0)
                    usage = {
                        "input_tokens": max(0, obj["usage"].get("prompt_tokens", 0) - cached),
                        "output_tokens": obj["usage"].get("completion_tokens", 0),
                        "cache_read_input_tokens": obj["usage"]
                        .get("prompt_tokens_details", {})
                        .get("cached_tokens", 0),
                        "cache_creation_input_tokens": 0,
                    }
                for choice in obj.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    reasoning = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                        or ""
                    )
                    thinking += (
                        reasoning.get("text", "") if isinstance(reasoning, dict) else reasoning
                    )
                    if delta.get("content"):
                        text += delta["content"]
                        yield {"type": "delta", "text": delta["content"]}
                    for tc in delta.get("tool_calls", []):
                        part = calls.setdefault(
                            tc["index"],
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            part["id"] = tc["id"]
                        fn = tc.get("function", {})
                        part["function"]["name"] += fn.get("name") or ""
                        part["function"]["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        stop = choice["finish_reason"]
        if stop is None:
            raise ProviderError("模型连接提前中断，未收到结束事件")
        message = {"role": "assistant", "content": text or None}
        if thinking:
            message["thinking"] = [{"type": "thinking", "thinking": thinking}]
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        yield {"type": "complete", "message": message, "usage": usage, "stop": stop}

    @staticmethod
    def anthropic_messages(messages: list) -> list:
        result = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"],
                        "is_error": message.get("is_error", False),
                    }
                ]
                role = "user"
            elif role == "assistant":
                blocks = list(message.get("thinking", [])) + (
                    [{"type": "text", "text": message["content"]}] if message.get("content") else []
                )
                for call in message.get("tool_calls", []):
                    try:
                        args = json.loads(call["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["function"]["name"],
                            "input": args,
                        }
                    )
            else:
                blocks = (
                    message["content"]
                    if isinstance(message["content"], list)
                    else [{"type": "text", "text": message["content"]}]
                )
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(blocks)
            else:
                result.append({"role": role, "content": blocks})
        return result

    async def _anthropic(self, cfg, messages, tools, system):
        from .sse import data_frames

        summaries = [m.get("content") or "" for m in messages if m["role"] == "system"]
        messages = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": cfg.model,
            "messages": self.anthropic_messages(messages),
            "system": "\n\n".join([system, *summaries]),
            "max_tokens": cfg.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]
        text, calls, usage, stop, ended, thinking = "", {}, {}, None, False, {}
        url = cfg.base_url + ("/messages" if cfg.base_url.endswith("/v1") else "/v1/messages")
        async with self.client.stream(
            "POST",
            url,
            headers={
                **(
                    {"Authorization": "Bearer " + cfg.api_key}
                    if cfg.auth_mode == "oauth"
                    else {"x-api-key": cfg.api_key}
                ),
                "anthropic-version": "2023-06-01",
            },
            json=payload,
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=cfg.connect_timeout_seconds),
        ) as response:
            await check_response(response)
            async for raw in data_frames(response):
                if not raw.strip():
                    continue
                obj = json.loads(raw)
                kind = obj.get("type")
                if kind == "error":
                    raise ProviderError("Anthropic 流式响应返回错误")
                if kind == "message_start":
                    source_usage = obj["message"].get("usage", {})
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ):
                        usage[key] = source_usage.get(key, 0)
                elif kind == "content_block_start" and obj["content_block"]["type"] == "thinking":
                    thinking[obj["index"]] = dict(obj["content_block"])
                elif kind == "content_block_start" and obj["content_block"]["type"] == "tool_use":
                    b = obj["content_block"]
                    calls[obj["index"]] = {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": ""},
                    }
                elif kind == "content_block_delta":
                    delta = obj["delta"]
                    if delta["type"] == "text_delta":
                        text += delta["text"]
                        yield {"type": "delta", "text": delta["text"]}
                    elif delta["type"] == "input_json_delta":
                        calls[obj["index"]]["function"]["arguments"] += delta["partial_json"]
                    elif delta["type"] in {"thinking_delta", "signature_delta"}:
                        field = "thinking" if delta["type"] == "thinking_delta" else "signature"
                        block = thinking.setdefault(
                            obj["index"], {"type": "thinking", "thinking": ""}
                        )
                        block[field] = (block.get(field) or "") + delta[field]
                elif kind == "message_delta":
                    stop = obj["delta"].get("stop_reason")
                    usage["output_tokens"] = obj.get("usage", {}).get("output_tokens", 0)
                elif kind == "message_stop":
                    ended = True
        if not ended:
            raise ProviderError("Anthropic 连接提前中断")
        message = {"role": "assistant", "content": text or None}
        if thinking:
            message["thinking"] = [thinking[index] for index in sorted(thinking)]
        if calls:
            message["tool_calls"] = list(calls.values())
        yield {"type": "complete", "message": message, "usage": usage, "stop": stop}

    async def embed(self, cfg: Settings, texts: list[str]) -> list[list[float]]:
        key = cfg.embedding_api_key or cfg.api_key
        if not key:
            raise ProviderError("请配置向量模型 API Key")
        allow_cloud(cfg, cfg.embedding_base_url)
        async with self.limiter.slot(cfg):
            response = await self.client.post(
                cfg.embedding_base_url + "/embeddings",
                headers={"Authorization": "Bearer " + key},
                json={"model": cfg.embedding_model, "input": texts},
            )
        await check_response(response, "向量模型")
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        vectors = [row["embedding"] for row in rows]
        if (
            len(vectors) != len(texts)
            or not vectors
            or not vectors[0]
            or len({len(v) for v in vectors}) != 1
        ):
            raise ProviderError("向量模型返回的数量或维度不一致")
        return vectors

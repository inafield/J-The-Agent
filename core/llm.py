"""Provider-neutral LLM clients used by every agent mode."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.config import LLMProvider, LLMSettings


class LLMError(RuntimeError):
    """A normalized error raised by any LLM backend."""


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolSpec(BaseModel):
    """Portable function-tool description."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str | None = None
    finish_reason: str | None = None

    def as_message(self) -> ChatMessage:
        """Convert the response to a history item for the next ReAct iteration."""

        return ChatMessage(
            role="assistant",
            content=self.content,
            tool_calls=self.tool_calls,
        )


class LLMClient(ABC):
    """Small synchronous interface suitable for a ReAct loop."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    @abstractmethod
    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        """Generate the next assistant message or tool calls."""

    def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Stream when supported; otherwise emit one completed response."""

        response = self.complete(messages, tools)
        if on_text and response.content:
            on_text(response.content)
        return response


def _decode_arguments(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Provider returned invalid tool arguments: {value}") from exc
    if not isinstance(decoded, dict):
        raise LLMError("Tool arguments must be a JSON object")
    return decoded


def _as_openai_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return payload


class OpenAICompatibleClient(LLMClient):
    """Client for OpenAI, OpenRouter, and Groq."""

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        api_key = settings.resolved_api_key
        if not api_key:
            raise LLMError(f"API key is required for provider '{settings.provider}'")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("Install the 'openai' package to use this provider") from exc

        default_headers = None
        if settings.provider is LLMProvider.OPENROUTER:
            default_headers = {"X-Title": "J the Agent"}
        self._client = OpenAI(
            api_key=api_key,
            base_url=settings.resolved_base_url,
            timeout=settings.request_timeout,
            default_headers=default_headers,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        payload_messages = [_as_openai_message(message) for message in messages]
        request: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": payload_messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if tools:
            request["tools"] = [tool.as_openai() for tool in tools]

        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise LLMError(f"{self.settings.provider} request failed: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_decode_arguments(call.function.arguments),
            )
            for call in (message.tool_calls or [])
        ]
        usage = response.usage
        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
            model=response.model,
            finish_reason=choice.finish_reason,
        )

    def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": [_as_openai_message(message) for message in messages],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = [tool.as_openai() for tool in tools]
        content: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        usage = TokenUsage()
        model = self.settings.resolved_model
        finish_reason = None
        try:
            try:
                stream = self._client.chat.completions.create(**request)
            except Exception as exc:
                if "stream_options" not in str(exc):
                    raise
                request.pop("stream_options", None)
                stream = self._client.chat.completions.create(**request)
            for chunk in stream:
                model = getattr(chunk, "model", model)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = TokenUsage(
                        prompt_tokens=chunk_usage.prompt_tokens or 0,
                        completion_tokens=chunk_usage.completion_tokens or 0,
                    )
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                if delta.content:
                    content.append(delta.content)
                    if on_text:
                        on_text(delta.content)
                for call in delta.tool_calls or []:
                    item = pending.setdefault(
                        call.index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    item["id"] += call.id or ""
                    if call.function:
                        item["name"] += call.function.name or ""
                        item["arguments"] += call.function.arguments or ""
        except Exception as exc:
            raise LLMError(f"{self.settings.provider} stream failed: {exc}") from exc
        calls = [
            ToolCall(
                id=item["id"] or f"stream-{index}",
                name=item["name"],
                arguments=_decode_arguments(item["arguments"]),
            )
            for index, item in sorted(pending.items())
        ]
        return LLMResponse(
            content="".join(content),
            tool_calls=calls,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
        )


class OllamaClient(LLMClient):
    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        try:
            import ollama
        except ImportError as exc:
            raise LLMError("Install the 'ollama' package to use Ollama") from exc
        self._client = ollama.Client(
            host=settings.resolved_base_url,
            timeout=settings.request_timeout,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": [_as_openai_message(message) for message in messages],
            "options": {
                "temperature": self.settings.temperature,
                "num_predict": self.settings.max_tokens,
            },
        }
        if tools:
            request["tools"] = [tool.as_openai() for tool in tools]
        try:
            response = self._client.chat(**request)
        except Exception as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        raw_message = response.message
        raw_calls = raw_message.tool_calls or []
        calls = [
            ToolCall(
                id=getattr(call, "id", None) or f"ollama-{index}",
                name=call.function.name,
                arguments=_decode_arguments(call.function.arguments),
            )
            for index, call in enumerate(raw_calls)
        ]
        return LLMResponse(
            content=raw_message.content or "",
            tool_calls=calls,
            usage=TokenUsage(
                prompt_tokens=getattr(response, "prompt_eval_count", 0) or 0,
                completion_tokens=getattr(response, "eval_count", 0) or 0,
            ),
            model=getattr(response, "model", self.settings.resolved_model),
            finish_reason=getattr(response, "done_reason", None),
        )

    def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": [_as_openai_message(message) for message in messages],
            "options": {
                "temperature": self.settings.temperature,
                "num_predict": self.settings.max_tokens,
            },
            "stream": True,
        }
        if tools:
            request["tools"] = [tool.as_openai() for tool in tools]
        content: list[str] = []
        calls: list[ToolCall] = []
        prompt_tokens = completion_tokens = 0
        finish_reason = None
        try:
            for chunk in self._client.chat(**request):
                message = chunk.message
                if message.content:
                    content.append(message.content)
                    if on_text:
                        on_text(message.content)
                for index, call in enumerate(message.tool_calls or []):
                    calls.append(
                        ToolCall(
                            id=getattr(call, "id", None) or f"ollama-stream-{index}",
                            name=call.function.name,
                            arguments=_decode_arguments(call.function.arguments),
                        )
                    )
                prompt_tokens = getattr(chunk, "prompt_eval_count", 0) or prompt_tokens
                completion_tokens = getattr(chunk, "eval_count", 0) or completion_tokens
                finish_reason = getattr(chunk, "done_reason", None) or finish_reason
        except Exception as exc:
            raise LLMError(f"Ollama stream failed: {exc}") from exc
        return LLMResponse(
            content="".join(content),
            tool_calls=calls,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            model=self.settings.resolved_model,
            finish_reason=finish_reason,
        )


class AnthropicClient(LLMClient):
    """Minimal Anthropic Messages API client using the standard library."""

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        self._api_key = settings.resolved_api_key
        if not self._api_key:
            raise LLMError("API key is required for provider 'anthropic'")

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        system_parts = [message.content for message in messages if message.role == "system"]
        conversation = self._convert_messages(
            [message for message in messages if message.role != "system"]
        )
        payload: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": conversation,
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]

        request = urllib.request.Request(
            f"{self.settings.resolved_base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "x-api-key": self._api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL comes from trusted configuration.
                request,
                timeout=self.settings.request_timeout,
            ) as result:
                response = json.loads(result.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        content_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.get("content", []):
            if block.get("type") == "text":
                content_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input", {}),
                    )
                )
        usage = response.get("usage", {})
        return LLMResponse(
            content="".join(content_parts),
            tool_calls=calls,
            usage=TokenUsage(
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
            ),
            model=response.get("model"),
            finish_reason=response.get("stop_reason"),
        )

    def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        system_parts = [message.content for message in messages if message.role == "system"]
        payload: dict[str, Any] = {
            "model": self.settings.resolved_model,
            "messages": self._convert_messages(
                [message for message in messages if message.role != "system"]
            ),
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        request = urllib.request.Request(
            f"{self.settings.resolved_base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "anthropic-version": "2023-06-01",
                "accept": "text/event-stream",
                "content-type": "application/json",
                "x-api-key": self._api_key,
            },
            method="POST",
        )
        content: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        usage = TokenUsage()
        finish_reason = None
        model = self.settings.resolved_model
        try:
            with urllib.request.urlopen(  # noqa: S310
                request,
                timeout=self.settings.request_timeout,
            ) as result:
                for raw_line in result:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    raw_data = line.removeprefix("data:").strip()
                    if not raw_data or raw_data == "[DONE]":
                        continue
                    event = json.loads(raw_data)
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = event.get("message", {})
                        model = message.get("model", model)
                        usage.prompt_tokens = message.get("usage", {}).get("input_tokens", 0)
                    elif event_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending[event["index"]] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            content.append(text)
                            if on_text:
                                on_text(text)
                        elif delta.get("type") == "input_json_delta":
                            pending[event["index"]]["arguments"] += delta.get("partial_json", "")
                    elif event_type == "message_delta":
                        finish_reason = event.get("delta", {}).get("stop_reason")
                        usage.completion_tokens = event.get("usage", {}).get("output_tokens", 0)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"Anthropic stream failed: {exc}") from exc
        calls = [
            ToolCall(
                id=item["id"] or f"anthropic-stream-{index}",
                name=item["name"],
                arguments=_decode_arguments(item["arguments"]),
            )
            for index, item in sorted(pending.items())
        ]
        return LLMResponse(
            content="".join(content),
            tool_calls=calls,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _convert_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Translate portable history to Anthropic's content-block format."""

        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue

            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            converted.append(
                {
                    "role": message.role,
                    "content": blocks or [{"type": "text", "text": ""}],
                }
            )
        return converted


def create_llm_client(settings: LLMSettings) -> LLMClient:
    """Build the correct backend without exposing provider details to a mode."""

    if settings.provider is LLMProvider.OLLAMA:
        return OllamaClient(settings)
    if settings.provider is LLMProvider.ANTHROPIC:
        return AnthropicClient(settings)
    return OpenAICompatibleClient(settings)

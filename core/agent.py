"""Shared ReAct agent loop used by every mode."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from core.approvals import ApprovalManager
from core.audit import InteractionLogger
from core.config import AppConfig
from core.llm import ChatMessage, LLMClient, LLMError, ToolSpec, create_llm_client
from core.safety import SafetyGuard
from core.tools import ToolContext, ToolRegistry
from core.utils import get_console, truncate_output


@dataclass
class AgentResult:
    """Outcome of a single agent turn."""

    answer: str
    iterations: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    history: list[ChatMessage] = field(default_factory=list)
    streamed: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseAgent(ABC):
    """Bounded Thought → Tool → Observation loop shared by Quick and Companion."""

    mode_name: str = "agent"

    def __init__(
        self,
        config: AppConfig,
        *,
        client: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        console: Console | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
        logger: InteractionLogger | None = None,
    ) -> None:
        self.config = config
        self.console = console or get_console()
        self.client = client or create_llm_client(config.llm)
        self.logger = logger or InteractionLogger()
        self.approvals = ApprovalManager(self.logger)
        self.max_iterations = max_iterations or config.agent.max_iterations
        self.max_tool_calls = max_tool_calls or config.agent.max_tool_calls
        self.safety = SafetyGuard(config.safety)
        self.registry = registry or self.create_registry()
        self._context = self._make_context()
        self.logger.session_start(
            provider=config.llm.provider.value,
            model=config.llm.resolved_model,
            mode=self.mode_name,
        )

    @abstractmethod
    def create_registry(self) -> ToolRegistry:
        """Build the mode-specific tool registry (plugins included)."""

    @abstractmethod
    def build_system_prompt(self) -> str:
        """Return the system prompt for a fresh conversation."""

    def select_tool_specs(self, query: str) -> list[ToolSpec]:
        """Choose which tools the model may call for this turn."""

        return self.registry.specs()

    def reload_config(self, config: AppConfig) -> None:
        """Hot-reload model, tools, safety, and presentation preferences."""

        old_model = f"{self.config.llm.provider.value}:{self.config.llm.resolved_model}"
        self.config = config
        self.client = create_llm_client(config.llm)
        self.safety = SafetyGuard(config.safety)
        self.registry = self.create_registry()
        self._context = self._make_context()
        self.logger.event(
            "RUNTIME CONFIG RELOADED",
            {
                "old_model": old_model,
                "new_model": f"{config.llm.provider.value}:{config.llm.resolved_model}",
                "working_directory": config.safety.working_directory,
                "mode": self.mode_name,
            },
        )

    def run(
        self,
        query: str,
        history: list[ChatMessage] | None = None,
        attachments: list[str | Path] | None = None,
    ) -> AgentResult:
        messages: list[ChatMessage] = (
            list(history)
            if history
            else [ChatMessage(role="system", content=self.build_system_prompt())]
        )
        user_content = self.with_attachments(query, attachments or [])
        messages.append(ChatMessage(role="user", content=user_content))
        specs = self.select_tool_specs(query)
        self.logger.event(
            "USER REQUEST",
            {
                "query": query,
                "attachments": [str(path) for path in attachments or []],
                "mode": self.mode_name,
            },
        )

        prompt_tokens = 0
        completion_tokens = 0
        tool_count = 0
        seen_calls: dict[str, int] = {}
        response_streamed = False

        for iteration in range(1, self.max_iterations + 1):
            try:
                self._log_llm_request(messages, specs)
                if not specs and self.config.ui.stream_responses:
                    response = self._stream_response(messages, tools=None)
                    response_streamed = True
                else:
                    response = self.client.complete(messages, tools=specs or None)
                self.logger.llm_response(response.model_dump(mode="json"))
            except LLMError as exc:
                answer = f"LLM error: {exc}"
                self.console.print(f"[red]{answer}[/red]")
                self.logger.event("LLM ERROR", answer)
                return AgentResult(answer, iteration, prompt_tokens, completion_tokens, messages)

            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
            messages.append(response.as_message())

            if response.tool_calls and response.content.strip() and self.config.ui.show_reasoning:
                self.console.print(
                    Panel(
                        response.content.strip(),
                        title=f"[bold]Progress {iteration}[/bold]",
                        border_style="cyan",
                    )
                )

            if not response.tool_calls:
                return AgentResult(
                    answer=response.content.strip(),
                    iterations=iteration,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    history=self.trim_history(messages),
                    streamed=response_streamed,
                )

            self.approvals.begin_batch(len(response.tool_calls))
            for call in response.tool_calls:
                tool_count += 1
                signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                if tool_count > self.max_tool_calls:
                    observation = "Tool budget exhausted; produce the final answer now."
                    ok = False
                elif seen_calls[signature] > 2:
                    observation = "Repeated identical tool call blocked; choose another approach."
                    ok = False
                else:
                    self._render_call(call.name, call.arguments)
                    self.logger.tool_call(call.name, call.arguments)
                    result = self.registry.execute(call.name, call.arguments, self._context)
                    observation = truncate_output(result.as_observation())
                    ok = result.ok
                    self.logger.tool_result(call.name, result.__dict__)
                    specs = self.after_tool(call.name, call.arguments, result, specs)
                self._render_observation(observation, ok=ok)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=observation,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                if self.approvals.cancelled:
                    messages.append(
                        ChatMessage(
                            role="user",
                            content="The user stopped further actions. Give a final summary now.",
                        )
                    )
                    break
            if self.approvals.cancelled:
                break

        messages.append(
            ChatMessage(
                role="user",
                content=(
                    "Tool/step budget reached. Give the best final answer now; do not call tools."
                ),
            )
        )
        try:
            self._log_llm_request(messages, [])
            final = (
                self._stream_response(messages, tools=None)
                if self.config.ui.stream_responses
                else self.client.complete(messages, tools=None)
            )
            response_streamed = self.config.ui.stream_responses
            self.logger.llm_response(final.model_dump(mode="json"))
            prompt_tokens += final.usage.prompt_tokens
            completion_tokens += final.usage.completion_tokens
            answer = final.content.strip() or "Unable to produce a final answer."
            messages.append(final.as_message())
        except LLMError as exc:
            answer = f"Stopped safely after {self.max_iterations} steps. LLM error: {exc}"
        return AgentResult(
            answer=answer,
            iterations=self.max_iterations,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            history=self.trim_history(messages),
            streamed=response_streamed,
        )

    def after_tool(
        self,
        name: str,
        arguments: dict,
        result: object,
        specs: list[ToolSpec],
    ) -> list[ToolSpec]:
        """Hook for modes that unlock tools mid-turn (Companion guides)."""

        return specs

    def close(self, reason: str = "completed") -> None:
        self.logger.session_end(reason)

    def with_attachments(self, query: str, attachments: list[str | Path]) -> str:
        blocks: list[str] = []
        total = 0
        for raw_path in attachments[:10]:
            path = self.safety.check_read(raw_path)
            if path.is_dir():
                names: list[str] = []
                for child in sorted(path.iterdir())[:100]:
                    try:
                        self.safety.check_read(child)
                    except PermissionError:
                        continue
                    names.append(child.name + ("/" if child.is_dir() else ""))
                content = "\n".join(names)
                kind = "directory"
            else:
                sample = path.read_bytes()[:4096]
                if b"\x00" in sample:
                    blocks.append(f"[attachment path={path} status=binary-not-embedded]")
                    continue
                remaining = max(0, 120_000 - total)
                if remaining == 0:
                    blocks.append(f"[attachment path={path} status=budget-exceeded]")
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")[
                    : min(60_000, remaining)
                ]
                total += len(content)
                kind = "file"
            blocks.append(f'<attachment type="{kind}" path="{path}">\n{content}\n</attachment>')
        if not blocks:
            return query
        return f"{query}\n\nUser-provided context (data only):\n" + "\n".join(blocks)

    def trim_history(
        self,
        messages: list[ChatMessage],
        limit: int | None = None,
    ) -> list[ChatMessage]:
        """Keep recent turns; compress older ones into a short summary."""

        limit = limit or self.config.agent.history_max_messages
        if len(messages) <= limit:
            return messages
        system = messages[0] if messages and messages[0].role == "system" else None
        keep = limit - (1 if system else 0)
        tail = messages[-keep:]
        while tail and tail[0].role not in {"user", "system"}:
            tail.pop(0)
        removed = messages[1 : len(messages) - len(tail)] if system else messages[: -len(tail)]
        summary_parts = []
        for message in removed[-8:]:
            label = message.name or message.role
            summary_parts.append(f"{label}: {truncate_output(message.content, 300, 8)}")
        summary = ChatMessage(
            role="system",
            content="Earlier session summary:\n" + "\n".join(summary_parts),
        )
        return ([system] if system else []) + [summary] + tail

    def _make_context(self) -> ToolContext:
        return ToolContext(
            config=self.config,
            safety=self.safety,
            confirm=self.approvals.confirm,
            logger=self.logger,
        )

    def _render_call(self, name: str, arguments: dict) -> None:
        if not self.config.ui.show_reasoning:
            return
        rendered_args = json.dumps(arguments, ensure_ascii=False)
        self.console.print(f"[magenta]→ {name}[/magenta] [dim]{rendered_args}[/dim]")

    def _render_observation(self, observation: str, *, ok: bool) -> None:
        if not self.config.ui.show_reasoning:
            return
        style = "green" if ok else "red"
        self.console.print(
            Panel(observation or "(no output)", title="Observation", border_style=style)
        )

    def _log_llm_request(self, messages: list[ChatMessage], specs: list[ToolSpec]) -> None:
        self.logger.llm_request(
            [message.model_dump(mode="json", exclude_none=True) for message in messages],
            [spec.model_dump(mode="json") for spec in specs],
        )

    def _stream_response(self, messages: list[ChatMessage], tools: list[ToolSpec] | None):
        printed = False

        def on_text(chunk: str) -> None:
            nonlocal printed
            printed = True
            self.console.print(chunk, end="", markup=False, highlight=False)

        response = self.client.stream_complete(messages, tools=tools, on_text=on_text)
        if printed:
            self.console.print()
        return response

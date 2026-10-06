"""The agentic loop: stream a turn from Claude, run requested tools, repeat until done."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from . import notes, prompts, sysinfo, tools

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 40
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class View(Protocol):
    """What the agent needs from the UI layer."""
    def thinking_started(self) -> None: ...
    def thinking_delta(self, text: str) -> None: ...
    def text_delta(self, text: str) -> None: ...
    def block_done(self) -> None: ...
    def tool_started(self, name: str, args: dict) -> None: ...
    def tool_finished(self, name: str, result: str, is_error: bool) -> None: ...
    def web_search(self, query: str) -> None: ...
    def notice(self, text: str) -> None: ...


@dataclass
class Agent:
    view: View
    ctx: tools.ToolContext
    model: str = DEFAULT_MODEL
    effort: str = "high"
    web: bool = True
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.system = prompts.build_system(sysinfo.snapshot(), notes.load_notes())
        self.tool_defs = tools.TOOL_DEFS + ([tools.WEB_SEARCH_TOOL] if self.web else [])

    def reset(self) -> None:
        self.messages.clear()

    def _add_user(self, content: str | list[dict]) -> None:
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        # after a refusal the history can end on a user turn; merge rather than send two in a row
        if self.messages and self.messages[-1]["role"] == "user":
            prev = self.messages[-1]["content"]
            prev = [{"type": "text", "text": prev}] if isinstance(prev, str) else list(prev)
            self.messages[-1]["content"] = prev + blocks
        else:
            self.messages.append({"role": "user", "content": blocks})

    def _stream_turn(self):
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=self.system,
            tools=self.tool_defs,
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "thinking":
                        self.view.thinking_started()
                    elif block.type == "server_tool_use" and block.name == "web_search":
                        pass  # query arrives in input deltas; reported at block stop
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        self.view.text_delta(event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        self.view.thinking_delta(event.delta.thinking)
                elif event.type == "content_block_stop":
                    block = getattr(event, "content_block", None)
                    if block is not None and block.type == "server_tool_use":
                        self.view.web_search(str((block.input or {}).get("query", "")))
                    self.view.block_done()
            return stream.get_final_message()

    def repair(self) -> None:
        """After Ctrl-C, make sure every tool_use has a tool_result so the history stays valid."""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        pending = [b for b in self.messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
        if pending:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": "Interrupted by the user.",
                 "is_error": True} for b in pending]})

    def ask(self, user_text: str) -> None:
        self._add_user(user_text)
        json_retries = 0
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = self._stream_turn()
                json_retries = 0
            except ValueError:
                # a streamed tool input the SDK could not parse at all; re-issue the turn (bounded)
                json_retries += 1
                if json_retries > 2:
                    raise
                continue

            if response.stop_reason == "refusal":
                # discard the partial turn; the history now ends on the user turn, which _add_user merges into
                self.view.notice("Claude declined to continue with this request.")
                return

            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue  # a server tool (web search) paused mid-turn; re-send to resume

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    self.view.notice("Response hit the output limit and was cut off.")
                return

            results = []
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    # a truncated input still parses into a partial object; never run it
                    content, is_error = "Tool input was truncated by the output limit; not run.", True
                else:
                    self.view.tool_started(block.name, block.input)
                    content, is_error = tools.dispatch(self.ctx, block.name, block.input)
                    self.view.tool_finished(block.name, content, is_error)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})

        self.view.notice(f"Stopped after {MAX_TOOL_ROUNDS} tool rounds. Say 'continue' to keep going.")

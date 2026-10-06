"""Gemini backend: the same agentic loop as agent.py, on Google's Gemini API.

Works with a free API key from https://aistudio.google.com/apikey. Talks to the REST API directly
so it needs no extra dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tux import monitor, notes, sysinfo

from . import prompts, tools
from .agent import MAX_TOOL_ROUNDS, View

DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
KEY_URL = "https://aistudio.google.com/apikey"
KEY_FILE = notes.CONFIG_DIR / "gemini_key"
KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


class GeminiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GeminiAuthError(GeminiError):
    pass


class GeminiRateLimit(GeminiError):
    pass


def load_key() -> str | None:
    """The key from the environment, else the one saved by `tux gemini-key`."""
    for var in KEY_ENV_VARS:
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def save_key(key: str) -> Path:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip() + "\n")
    os.chmod(KEY_FILE, 0o600)
    return KEY_FILE


def forget_key() -> bool:
    try:
        KEY_FILE.unlink()
        return True
    except OSError:
        return False


def _schema(node: Any) -> Any:
    """Gemini accepts an OpenAPI subset of JSON Schema; drop what it rejects."""
    if isinstance(node, dict):
        return {k: _schema(v) for k, v in node.items() if k not in ("additionalProperties", "eager_input_streaming")}
    return node


def function_declarations() -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "parameters": _schema(t["input_schema"])}
            for t in tools.TOOL_DEFS]


def _error_from(status: int, body: str) -> GeminiError:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = body.strip()[:300] or f"HTTP {status}"
    if status in (401, 403) or (status == 400 and "api key" in message.lower()):
        return GeminiAuthError(message, status)
    if status == 429:
        return GeminiRateLimit(message, status)
    return GeminiError(message, status)


def _urlopen(req: urllib.request.Request):
    return urllib.request.urlopen(req, timeout=120)


@dataclass
class GeminiAgent:
    view: View
    ctx: tools.ToolContext
    api_key: str
    model: str = DEFAULT_MODEL
    show_thinking: bool = False
    opener: Callable[[urllib.request.Request], Any] = _urlopen
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        findings = monitor.active_findings()
        self.system = prompts.build_system(sysinfo.snapshot(), notes.load_notes(),
                                           monitor.format_findings(findings) if findings else "")
        self.system += ("\n\nYou are running on Gemini, which cannot search the web here. Rely on local "
                        "inspection and what you know; say so if an answer would need a web lookup.")
        self.tool_defs = [{"functionDeclarations": function_declarations()}]

    def reset(self) -> None:
        self.messages.clear()

    def _add_user(self, parts: list[dict]) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["parts"] += parts  # never two user turns in a row
        else:
            self.messages.append({"role": "user", "parts": parts})

    def _stream_turn(self) -> tuple[list[dict], str]:
        body = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": self.messages,
            "tools": self.tool_defs,
            "generationConfig": {"thinkingConfig": {"includeThoughts": self.show_thinking}},
        }
        req = urllib.request.Request(
            ENDPOINT.format(model=self.model), data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "x-goog-api-key": self.api_key})
        try:
            resp = self.opener(req)
        except urllib.error.HTTPError as e:
            raise _error_from(e.code, e.read().decode("utf-8", "replace")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GeminiError(f"Couldn't reach the Gemini API: {e}") from None

        parts: list[dict] = []
        finish = ""
        thinking = False
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    chunk = json.loads(line[5:])
                except ValueError:
                    continue
                if "error" in chunk:
                    raise _error_from(int(chunk["error"].get("code", 0)), json.dumps(chunk))
                if chunk.get("promptFeedback", {}).get("blockReason"):
                    finish = "SAFETY"
                for cand in chunk.get("candidates", []):
                    finish = cand.get("finishReason", finish)
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("thought"):
                            if not thinking:
                                self.view.thinking_started()
                                thinking = True
                            self.view.thinking_delta(part.get("text", ""))
                            continue
                        if thinking:
                            self.view.block_done()
                            thinking = False
                        if "text" in part:
                            self.view.text_delta(part["text"])
                        parts.append(part)
        if thinking:
            self.view.block_done()
        self.view.block_done()
        return parts, finish

    def repair(self) -> None:
        """After Ctrl-C, make sure every function call has a response so the history stays valid."""
        if not self.messages or self.messages[-1]["role"] != "model":
            return
        calls = [p["functionCall"] for p in self.messages[-1]["parts"] if "functionCall" in p]
        if calls:
            self.messages.append({"role": "user", "parts": [
                {"functionResponse": {"name": c["name"], "response": {"error": "Interrupted by the user."}}}
                for c in calls]})

    def ask(self, user_text: str) -> None:
        self._add_user([{"text": user_text}])
        for _ in range(MAX_TOOL_ROUNDS):
            parts, finish = self._stream_turn()

            if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
                self.view.notice("Gemini declined to continue with this request.")
                return
            if not parts:
                if finish == "MAX_TOKENS":
                    self.view.notice("Response hit the output limit and was cut off.")
                else:
                    self.view.notice("Gemini returned an empty response. Try rephrasing.")
                return

            self.messages.append({"role": "model", "parts": parts})
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                if finish == "MAX_TOKENS":
                    self.view.notice("Response hit the output limit and was cut off.")
                return

            responses = []
            for call in calls:
                args = call.get("args") or {}
                if finish == "MAX_TOKENS":
                    content, is_error = "Tool input was truncated by the output limit; not run.", True
                else:
                    self.view.tool_started(call["name"], args)
                    content, is_error = tools.dispatch(self.ctx, call["name"], args)
                    self.view.tool_finished(call["name"], content, is_error)
                responses.append({"functionResponse": {
                    "name": call["name"], "response": {"error" if is_error else "result": content}}})
            self.messages.append({"role": "user", "parts": responses})

        self.view.notice(f"Stopped after {MAX_TOOL_ROUNDS} tool rounds. Say 'continue' to keep going.")

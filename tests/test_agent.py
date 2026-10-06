"""Drive the agent loop through the real SDK streaming parser with a mocked HTTP transport."""

import json

import anthropic
import httpx2 as httpx
import pytest

from tux import agent as agent_mod
from tux import tools
from tests.test_tools import FakeApprover, isolated_state  # noqa: F401  (fixture)


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_events(blocks: list[dict], stop_reason: str) -> list[dict]:
    events = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "text_delta", "text": block["text"]}})
        elif block["type"] == "tool_use":
            events.append({"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        events.append({"type": "content_block_stop", "index": i})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": 5}})
    events.append({"type": "message_stop"})
    return events


class RecordingView:
    def __init__(self):
        self.text = ""
        self.tools = []
        self.notices = []

    def thinking_started(self): pass
    def thinking_delta(self, text): pass
    def text_delta(self, text): self.text += text
    def block_done(self): pass
    def tool_started(self, name, args): self.tools.append((name, args))
    def tool_finished(self, name, result, is_error): pass
    def web_search(self, query): pass
    def notice(self, text): self.notices.append(text)


def make_agent(responses: list[bytes], requests: list[dict], approver=None):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=responses[len(requests) - 1],
                              headers={"content-type": "text/event-stream"})

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    view = RecordingView()
    ctx = tools.ToolContext(approver=approver or FakeApprover())
    return agent_mod.Agent(view=view, ctx=ctx, client=client), view


def test_tool_round_trip():
    requests: list[dict] = []
    agent, view = make_agent([
        sse(message_events([{"type": "text", "text": "Checking."},
                            {"type": "tool_use", "id": "tu_1", "name": "run_command",
                             "input": {"command": "echo kernel-ok", "purpose": "check"}}], "tool_use")),
        sse(message_events([{"type": "text", "text": " All good."}], "end_turn")),
    ], requests)

    agent.ask("is my system ok?")

    assert view.text == "Checking. All good."
    assert view.tools == [("run_command", {"command": "echo kernel-ok", "purpose": "check"})]
    first, second = requests
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["thinking"]["type"] == "adaptive"
    assert {t["name"] for t in first["tools"]} >= {"run_command", "hardware_scan", "web_search"}
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "kernel-ok" in tool_result["content"]
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]


def test_refusal_discards_turn_and_next_message_merges():
    requests: list[dict] = []
    agent, view = make_agent([
        sse(message_events([{"type": "text", "text": "partial"}], "refusal")),
        sse(message_events([{"type": "text", "text": "ok"}], "end_turn")),
    ], requests)
    agent.ask("first")
    assert view.notices and agent.messages[-1]["role"] == "user"
    agent.ask("second")
    roles = [m["role"] for m in requests[1]["messages"]]
    assert roles == ["user"]  # merged, never two user turns in a row
    assert len(requests[1]["messages"][0]["content"]) == 2


def test_truncated_tool_input_is_not_run(tmp_path):
    requests: list[dict] = []
    target = tmp_path / "should-not-exist"
    approver = FakeApprover()
    agent, view = make_agent([
        sse(message_events([{"type": "tool_use", "id": "tu_1", "name": "run_command",
                             "input": {"command": f"touch {target}", "purpose": "x"}}], "max_tokens")),
        sse(message_events([{"type": "text", "text": "sorry"}], "end_turn")),
    ], requests, approver)
    agent.ask("go")
    assert not target.exists() and approver.commands == []
    assert requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_repair_after_interrupt():
    agent, _ = make_agent([], [])
    from types import SimpleNamespace
    agent.messages = [{"role": "user", "content": "x"},
                      {"role": "assistant", "content": [SimpleNamespace(type="tool_use", id="tu_9")]}]
    agent.repair()
    assert agent.messages[-1]["content"][0]["tool_use_id"] == "tu_9"

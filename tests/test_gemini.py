"""Drive the Gemini agent loop with a fake transport."""

import io
import json
import urllib.error

import pytest

from tux_app import gemini, tools
from tests.test_agent import RecordingView
from tests.test_tools import FakeApprover, isolated_state  # noqa: F401  (fixture)


def sse(chunks: list[dict]) -> io.BytesIO:
    return io.BytesIO("".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode())


def chunk(parts: list[dict], finish: str = "") -> dict:
    cand: dict = {"content": {"role": "model", "parts": parts}}
    if finish:
        cand["finishReason"] = finish
    return {"candidates": [cand]}


def make_agent(responses: list, requests: list[dict], approver=None):
    def opener(req):
        requests.append({"url": req.full_url, "key": req.get_header("X-goog-api-key"),
                         "body": json.loads(req.data)})
        r = responses[len(requests) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    view = RecordingView()
    ctx = tools.ToolContext(approver=approver or FakeApprover())
    return gemini.GeminiAgent(view=view, ctx=ctx, api_key="k", opener=opener), view


def test_tool_round_trip():
    requests: list[dict] = []
    agent, view = make_agent([
        sse([chunk([{"text": "Checking."}]),
             chunk([{"functionCall": {"name": "run_command",
                                      "args": {"command": "echo kernel-ok", "purpose": "check"}}}], "STOP")]),
        sse([chunk([{"text": " All good."}], "STOP")]),
    ], requests)

    agent.ask("is my system ok?")

    assert view.text == "Checking. All good."
    assert view.tools == [("run_command", {"command": "echo kernel-ok", "purpose": "check"})]
    first, second = requests
    assert first["key"] == "k" and "gemini-2.5-flash:streamGenerateContent" in first["url"]
    names = {d["name"] for d in first["body"]["tools"][0]["functionDeclarations"]}
    assert names >= {"run_command", "hardware_scan"}
    assert "additionalProperties" not in json.dumps(first["body"]["tools"])
    reply = second["body"]["contents"][-1]["parts"][0]["functionResponse"]
    assert reply["name"] == "run_command" and "kernel-ok" in reply["response"]["result"]
    assert [m["role"] for m in agent.messages] == ["user", "model", "user", "model"]


def test_blocked_turn_notice():
    requests: list[dict] = []
    agent, view = make_agent([sse([chunk([{"text": "x"}], "SAFETY")])], requests)
    agent.ask("hi")
    assert view.notices


def test_http_errors_map_to_types():
    body = json.dumps({"error": {"message": "API key not valid. Please pass a valid API key."}}).encode()
    err = urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(body))
    agent, _ = make_agent([err], [])
    with pytest.raises(gemini.GeminiAuthError):
        agent.ask("hi")
    err = urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO(b"{}"))
    agent, _ = make_agent([err], [])
    with pytest.raises(gemini.GeminiRateLimit):
        agent.ask("hi")


def test_repair_after_interrupt():
    agent, _ = make_agent([], [])
    agent.messages = [{"role": "user", "parts": [{"text": "x"}]},
                      {"role": "model", "parts": [{"functionCall": {"name": "run_command", "args": {}}}]}]
    agent.repair()
    assert agent.messages[-1]["parts"][0]["functionResponse"]["name"] == "run_command"


def test_key_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini, "KEY_FILE", tmp_path / "cfg" / "gemini_key")
    for var in gemini.KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert gemini.load_key() is None
    path = gemini.save_key(" abc123 ")
    assert path.stat().st_mode & 0o777 == 0o600
    assert gemini.load_key() == "abc123"
    monkeypatch.setenv("GEMINI_API_KEY", "fromenv")
    assert gemini.load_key() == "fromenv"
    monkeypatch.delenv("GEMINI_API_KEY")
    assert gemini.forget_key() and gemini.load_key() is None

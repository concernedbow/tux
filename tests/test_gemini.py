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


def test_thought_parts_stream_to_view_and_are_not_kept():
    requests: list[dict] = []
    events = []
    agent, view = make_agent([sse([chunk([{"text": "hmm", "thought": True}]),
                                   chunk([{"text": "Answer."}], "STOP")])], requests)
    view.thinking_started = lambda: events.append("start")
    view.thinking_delta = lambda t: events.append(t)
    agent.ask("hi")
    assert events == ["start", "hmm"]
    assert view.text == "Answer."
    assert agent.messages[-1]["parts"] == [{"text": "Answer."}]


def test_truncated_tool_call_is_not_run(tmp_path):
    requests: list[dict] = []
    target = tmp_path / "should-not-exist"
    approver = FakeApprover()
    agent, view = make_agent([
        sse([chunk([{"functionCall": {"name": "run_command",
                                      "args": {"command": f"touch {target}", "purpose": "x"}}}], "MAX_TOKENS")]),
        sse([chunk([{"text": "sorry"}], "STOP")]),
    ], requests, approver)
    agent.ask("go")
    assert not target.exists() and approver.commands == []
    reply = requests[1]["body"]["contents"][-1]["parts"][0]["functionResponse"]
    assert "error" in reply["response"]


def test_empty_and_max_tokens_responses_notify():
    agent, view = make_agent([sse([chunk([], "STOP")])], [])
    agent.ask("hi")
    assert any("empty" in n for n in view.notices)
    agent, view = make_agent([sse([chunk([{"text": "cut"}], "MAX_TOKENS")])], [])
    agent.ask("hi")
    assert any("output limit" in n for n in view.notices)


def test_invalid_tool_args_return_error_not_crash():
    requests: list[dict] = []
    agent, _ = make_agent([
        sse([chunk([{"functionCall": {"name": "run_command", "args": {}}}], "STOP")]),
        sse([chunk([{"text": "ok"}], "STOP")]),
    ], requests)
    agent.ask("go")
    reply = requests[1]["body"]["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert "INVALID_INPUT" in reply["error"]


def test_consecutive_user_turns_merge():
    requests: list[dict] = []
    agent, _ = make_agent([sse([chunk([{"text": "x"}], "SAFETY")]), sse([chunk([{"text": "ok"}], "STOP")])],
                          requests)
    agent.ask("first")
    agent.ask("second")
    contents = requests[1]["body"]["contents"]
    assert [c["role"] for c in contents] == ["user"] and len(contents[0]["parts"]) == 2


# --- CLI wiring -----------------------------------------------------------

@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    from tux_app import cli
    monkeypatch.setattr(gemini, "KEY_FILE", tmp_path / "gemini_key")
    for var in gemini.KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    calls = {}
    monkeypatch.setattr(cli, "run_api", lambda args, q, provider="anthropic": calls.update(api=provider, q=q))
    monkeypatch.setattr(cli, "run_claude_code", lambda args, q: calls.update(claude=q))
    return cli, calls


def test_gemini_flag_skips_claude_code(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    cli.main(["--gemini", "is", "my", "ssd", "ok?"])
    assert calls == {"api": "gemini", "q": "is my ssd ok?"}


def test_plain_tux_prefers_claude_code(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    cli.main(["hello"])
    assert calls == {"claude": "hello"}


def test_falls_back_to_gemini_when_only_key_available(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(cli, "_has_credentials", lambda: False)
    gemini.save_key("abc")
    cli.main(["hello"])
    assert calls["api"] == "gemini"


def test_anthropic_wins_without_gemini_key(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(cli, "_has_credentials", lambda: False)
    cli.main(["hello"])
    assert calls["api"] == "anthropic"


def test_gemini_key_command_saves_and_removes(cli_env, monkeypatch):
    cli, calls = cli_env
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "  secret  ")
    cli.main(["gemini-key"])
    assert gemini.load_key() == "secret" and not calls
    cli.main(["gemini-key", "--remove"])
    assert gemini.load_key() is None


def test_run_api_without_key_exits(monkeypatch, tmp_path):
    import argparse
    from tux_app import cli
    monkeypatch.setattr(gemini, "KEY_FILE", tmp_path / "gemini_key")
    for var in gemini.KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        cli.run_api(argparse.Namespace(), "", provider="gemini")

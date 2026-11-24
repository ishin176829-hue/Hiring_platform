import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


TOKEN = "test-token-kept-out-of-output"


@pytest.fixture
def api(tmp_path, monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, json.loads(body) if body else None))
            status = 200
            result = {"path": self.path}
            if urlsplit(self.path).path in {"/api/jobs", "/api/batches", "/api/events"}:
                result = [result]
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                status, result = 401, {"detail": "需要令牌"}
            elif self.command == "POST" and self.headers.get("Content-Type") != "application/json":
                status, result = 415, {"detail": "需要 JSON"}
            elif self.path == "/api/batches/9/run":
                status, result = 400, {"detail": "清单未获确认"}
            elif self.path == "/api/batches/8/run":
                status, result = 302, {"detail": "请求被重定向"}
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/token-catcher")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        do_GET = do_POST = handle_request

        def log_message(self, format, *args):
            pass

    state = tmp_path / "service-state"
    state.mkdir()
    (state / "api-token").write_text(TOKEN)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        monkeypatch.setenv("BOSS_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("BOSS_STATE_DIR", str(state))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield calls
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    ("arguments", "method", "path", "query"),
    [
        (["status"], "GET", "/api/status", {}),
        (["collect"], "POST", "/api/collect", {}),
        (["jobs"], "GET", "/api/jobs", {}),
        (["jobs", "--city", "上海", "--q", "Agent + AI&应用"], "GET", "/api/jobs", {"city": ["上海"], "q": ["Agent + AI&应用"]}),
        (["batches"], "GET", "/api/batches", {}),
        (["run-batch", "7"], "POST", "/api/batches/7/run", {}),
        (["pause"], "POST", "/api/pause", {}),
        (["resume"], "POST", "/api/resume", {}),
        (["login"], "POST", "/api/browser/open", {}),
        (["events", "--limit", "12"], "GET", "/api/events", {"limit": ["12"]}),
    ],
)
def test_cli_calls_authenticated_api(arguments, method, path, query, api, capsys):
    from boss_hiring.cli import main

    assert main([*arguments, "--json"]) == 0
    output = capsys.readouterr()
    actual_method, actual_path, actual_body = api[0]
    assert actual_method == method
    assert urlsplit(actual_path).path == path
    assert parse_qs(urlsplit(actual_path).query) == query
    assert actual_body == ({} if method == "POST" else None)
    result = {"path": actual_path}
    if path in {"/api/jobs", "/api/batches", "/api/events"}:
        result = [result]
    assert json.loads(output.out) == result
    assert not output.err


def test_cli_returns_api_error_without_printing_token(api, capsys):
    from boss_hiring.cli import main

    assert main(["run-batch", "9"]) != 0
    output = capsys.readouterr()
    assert not output.out
    assert "清单未获确认" in output.err
    assert TOKEN not in output.err


def test_cli_never_uses_http_proxy_for_loopback(api, capsys, monkeypatch):
    from boss_hiring.cli import main

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": "/api/status"}


def test_cli_does_not_follow_redirects(api, capsys):
    from boss_hiring.cli import main

    assert main(["run-batch", "8"]) != 0
    assert api == [("POST", "/api/batches/8/run", {})]
    assert "302" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://example.com", "http://localhost@example.com", "http://example.com@localhost", "https://localhost", "http://127.0.0.1/api", "http://localhost?query=yes"])
def test_cli_rejects_untrusted_base_urls(url, api, capsys, monkeypatch):
    from boss_hiring.cli import main

    monkeypatch.setenv("BOSS_BASE_URL", url)
    assert main(["status"]) != 0
    assert "BOSS_BASE_URL" in capsys.readouterr().err
    assert api == []


def test_cli_does_not_create_state_for_missing_service(monkeypatch, tmp_path, capsys):
    from boss_hiring.cli import main

    state = tmp_path / "absent"
    monkeypatch.setenv("BOSS_STATE_DIR", str(state))
    monkeypatch.delenv("BOSS_BASE_URL", raising=False)
    assert main(["status"]) != 0
    assert "令牌" in capsys.readouterr().err
    assert not state.exists()


def test_cli_serve_configures_single_loopback_worker(monkeypatch, tmp_path):
    from boss_hiring.cli import main

    seen = {}

    def record(app, **options):
        seen.update(app=app, **options)
        assert Path(os.environ["BOSS_STATE_DIR"]) == tmp_path
        assert os.environ["BOSS_HEADLESS"] == "1"

    monkeypatch.setattr("uvicorn.run", record)
    assert main(["serve", "--state-dir", str(tmp_path), "--headless", "--port", "9876"]) == 0
    assert seen["app"] == "boss_hiring.app:create_app"
    assert seen["factory"] is True
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 9876
    assert seen["workers"] == 1


async def test_mcp_stdio_exposes_only_management_and_surfaces_api_errors(api):
    env = {key: os.environ[key] for key in ("BOSS_BASE_URL", "BOSS_STATE_DIR")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "boss_hiring.mcp_server"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialization = await session.initialize()
        assert initialization.serverInfo.name == "boss-hiring"
        tools = {tool.name for tool in (await session.list_tools()).tools}
        expected_calls = {
            "get_status": ({}, "GET", "/api/status"),
            "list_jobs": ({"city": "杭州"}, "GET", "/api/jobs?city=%E6%9D%AD%E5%B7%9E"),
            "collect_jobs": ({}, "POST", "/api/collect"),
            "list_batches": ({}, "GET", "/api/batches"),
            "run_approved_batch": ({"batch_id": 7}, "POST", "/api/batches/7/run"),
            "pause_worker": ({}, "POST", "/api/pause"),
            "resume_worker": ({}, "POST", "/api/resume"),
            "open_login": ({}, "POST", "/api/browser/open"),
            "get_events": ({"limit": 25}, "GET", "/api/events?limit=25"),
        }
        assert tools == expected_calls.keys()
        for tool, (arguments, method, path) in expected_calls.items():
            result = await session.call_tool(tool, arguments)
            assert not result.isError, result
            assert api[-1] == (method, path, {} if method == "POST" else None)
        unapproved = await session.call_tool("run_approved_batch", {"batch_id": 9})
        assert unapproved.isError
        assert "清单未获确认" in unapproved.content[0].text
        assert TOKEN not in unapproved.content[0].text

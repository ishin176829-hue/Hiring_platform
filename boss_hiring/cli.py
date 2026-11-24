import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import default_state_dir


DEFAULT_PORT = 8771
DEFAULT_STATE_DIR = default_state_dir()
JSONResponse = dict[str, Any] | list[dict[str, Any]]


class APIError(RuntimeError):
    """A management request failed."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_request(
    method: str,
    path: str,
    *,
    state_dir: Path | None = None,
    query: dict[str, str | int] | None = None,
    body: dict[str, Any] | None = None,
) -> JSONResponse:
    base_url = os.environ.get("BOSS_BASE_URL", f"http://127.0.0.1:{DEFAULT_PORT}")
    match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?/?", base_url, re.IGNORECASE)
    if not match or not 1 <= int(match[1] or 80) <= 65535:
        raise APIError("BOSS_BASE_URL 必须使用本机 HTTP 地址，例如 http://127.0.0.1:8771")
    # Resolve localhost ourselves so the token goes only to the IPv4 loopback address.
    origin = f"http://127.0.0.1:{int(match[1] or 80)}"
    token_file = Path(state_dir or os.environ.get("BOSS_STATE_DIR", DEFAULT_STATE_DIR)) / "api-token"
    try:
        token = token_file.read_text().strip()
    except (OSError, UnicodeError):
        raise APIError("无法读取本地访问令牌。请启动工作台并核对 BOSS_STATE_DIR") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", token):
        raise APIError("本地访问令牌格式无效")

    url = f"{origin}/api/{path}"
    encoded_query = urlencode({key: value for key, value in (query or {}).items() if value != ""})
    if encoded_query:
        url += "?" + encoded_query
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(body or {}, ensure_ascii=False).encode()
    request = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            detail = None
            try:
                payload = json.load(exc)
                if isinstance(payload, dict):
                    detail = payload.get("detail")
            except (ValueError, OSError):
                pass
        if isinstance(detail, str) and detail:
            raise APIError(f"HTTP {exc.code}，{detail.replace(token, '[已隐藏]')}") from None
        raise APIError(f"工作台拒绝请求（HTTP {exc.code}）") from None
    except (URLError, OSError):
        raise APIError("无法连接本地工作台。请确认服务已启动并核对端口") from None
    except ValueError:
        raise APIError("本地工作台返回了无效的 JSON") from None


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("请输入正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("请输入正整数")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boss-hiring", description="管理本机 BOSS 求职工作台")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本机工作台")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--port", type=positive_integer, default=DEFAULT_PORT)
    serve.add_argument("--headless", action="store_true", help="以无界面模式运行浏览器")

    routes = {
        "status": ("GET", "status", "查询工作台状态"),
        "collect": ("POST", "collect", "按当前设置排队采集岗位"),
        "jobs": ("GET", "jobs", "查询已采集岗位"),
        "batches": ("GET", "batches", "查询候选清单"),
        "run-batch": ("POST", "batches/{batch_id}/run", "执行已确认的清单"),
        "pause": ("POST", "pause", "暂停工作台任务"),
        "resume": ("POST", "resume", "解除全局暂停"),
        "login": ("POST", "browser/open", "打开需要人工登录的浏览器"),
        "events": ("GET", "events", "查询最近事件"),
    }
    for name, (_, _, description) in routes.items():
        command = commands.add_parser(name, help=description)
        command.add_argument("--state-dir", type=Path)
        command.add_argument("--json", action="store_true", help="输出单行 JSON")
        if name == "jobs":
            command.add_argument("--city", choices=["杭州", "上海"], default="")
            command.add_argument("--q", default="", help="搜索已采集的岗位")
        elif name == "run-batch":
            command.add_argument("batch_id", type=positive_integer)
        elif name == "events":
            command.add_argument("--limit", type=positive_integer, default=100)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        if args.port > 65535:
            serve.error("--port 范围为 1 至 65535")
        if args.state_dir is not None:
            os.environ["BOSS_STATE_DIR"] = str(args.state_dir.resolve())
        if args.headless:
            os.environ["BOSS_HEADLESS"] = "1"
        uvicorn.run("boss_hiring.app:create_app", factory=True, host="127.0.0.1", port=args.port, workers=1)
        return 0

    method, route, _ = routes[args.command]
    query = None
    if args.command == "jobs":
        query = {"city": args.city, "q": args.q}
    elif args.command == "events":
        query = {"limit": args.limit}

    try:
        result = api_request(method, route.format(**vars(args)), state_dir=args.state_dir, query=query)
    except APIError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

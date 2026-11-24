#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_LABEL="local.boss-hiring"
SERVICE_DOMAIN="gui/$(id -u)"
PLIST_PATH="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/BOSS-Hiring"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$SERVICE_DOMAIN/$SERVICE_LABEL" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "已停止并移除 BOSS 机器人登录启动项；项目和数据保留。"
  exit 0
fi

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
if [ ! -x "$RUNTIME_DIR/.venv/bin/python" ]; then
  "$HOME/.local/bin/uv" venv --python "$PROJECT_DIR/.venv/bin/python" "$RUNTIME_DIR/.venv" --cache-dir "$PROJECT_DIR/.cache/uv"
fi
"$HOME/.local/bin/uv" export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt --output-file "$PROJECT_DIR/.cache/runtime-requirements.txt" --directory "$PROJECT_DIR" --cache-dir "$PROJECT_DIR/.cache/uv" > /dev/null
"$HOME/.local/bin/uv" pip install --python "$RUNTIME_DIR/.venv/bin/python" --cache-dir "$PROJECT_DIR/.cache/uv" --requirements "$PROJECT_DIR/.cache/runtime-requirements.txt"
"$HOME/.local/bin/uv" pip install --offline --python "$RUNTIME_DIR/.venv/bin/python" --cache-dir "$PROJECT_DIR/.cache/uv" --reinstall-package boss-hiring --no-deps "$PROJECT_DIR"

"$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" "$PLIST_PATH" "$RUNTIME_DIR" <<'PY'
import os
import plistlib
import sys
import json
from pathlib import Path

project, target, runtime = map(Path, sys.argv[1:])
script = str(project / "scripts/run.sh")
arguments = [str(runtime / ".venv/bin/boss-hiring"), "serve", "--state-dir", str(runtime / "state"), "--port", "8771"]
if target.exists():
    existing = plistlib.loads(target.read_bytes())
    if existing.get("ProgramArguments") not in (["/bin/bash", script], arguments):
        raise SystemExit("同名启动项指向其它项目，未修改。")
state = runtime / "state"
state.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(state, 0o700)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(plistlib.dumps({
    "Label": "local.boss-hiring",
    "ProgramArguments": arguments,
    "WorkingDirectory": str(runtime),
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 10,
    "EnvironmentVariables": {"BOSS_STATE_DIR": str(state), "BOSS_BASE_URL": "http://127.0.0.1:8771"},
    "StandardOutPath": str(state / "service.stdout.log"),
    "StandardErrorPath": str(state / "service.stderr.log"),
}))
target.chmod(0o600)
metadata = runtime / "deployment.json"
metadata.write_text(json.dumps({"source":str(project), "base_url":"http://127.0.0.1:8771", "state_dir":str(state)}, ensure_ascii=False))
metadata.chmod(0o600)
PY

launchctl bootout "$SERVICE_DOMAIN/$SERVICE_LABEL" 2>/dev/null || true
"$PROJECT_DIR/.venv/bin/python" - "$SERVICE_DOMAIN/$SERVICE_LABEL" <<'PY'
import socket
import subprocess
import sys
import time

for attempt in range(40):
    registered = subprocess.run(["launchctl", "print", sys.argv[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    with socket.socket() as sock:
        sock.settimeout(0.1)
        listening = sock.connect_ex(("127.0.0.1", 8771)) == 0
    if not registered and not listening:
        break
    time.sleep(0.1)
else:
    raise SystemExit("旧服务尚未退出或端口被其它程序占用，未重复启动。")
PY
launchctl bootstrap "$SERVICE_DOMAIN" "$PLIST_PATH"
launchctl kickstart "$SERVICE_DOMAIN/$SERVICE_LABEL"
"$PROJECT_DIR/.venv/bin/python" - <<'PY'
import json
import time
from urllib.request import urlopen

for attempt in range(40):
    try:
        with urlopen("http://127.0.0.1:8771/health", timeout=1) as response:
            result = json.load(response)
        if result.get("service") != "boss-hiring":
            raise SystemExit("端口上不是本项目服务，请检查启动项。")
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("启动项已安装，但服务尚未就绪，请查看运行日志。")
PY
echo "BOSS 机器人已启动并通过健康检查：http://127.0.0.1:8771"

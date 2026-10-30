"""Local-only launcher for the installed DeepTutor 1.7.0 release.

Uses the release's configuration initialization and packaged frontend resolver.
Does not modify installed DeepTutor code. Keeps both listeners on loopback.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
NODE_DIR = ROOT / "tools" / "node-v22.23.4-win-x64"
STATE = ROOT / "processes.json"
FRONTEND_URL = "http://127.0.0.1:3782"
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def healthy(url: str) -> bool:
    try:
        with LOOPBACK_OPENER.open(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False

def read_state() -> dict:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))

def owned_process(item: dict):
    import psutil
    try:
        process = psutil.Process(item["pid"])
        if abs(process.create_time() - item["created"]) > 0.1:
            return None
        if process.exe().lower() != item["exe"].lower():
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
        return None

def stop() -> None:
    import psutil
    state = read_state()
    targets = []
    for item in state.get("processes", []):
        process = owned_process(item)
        if process is not None:
            targets.extend(process.children(recursive=True))
            targets.append(process)
    unique = {process.pid: process for process in targets}
    for process in unique.values():
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(unique.values()), timeout=8)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=3)
    if STATE.exists():
        STATE.unlink()
    print("DeepTutor stopped. Runtime data is preserved.")

def prepare():
    os.environ["DEEPTUTOR_HOME"] = str(RUNTIME)
    os.environ["PATH"] = str(NODE_DIR) + os.pathsep + os.environ.get("PATH", "")
    from deeptutor.runtime.launcher import _reset_runtime_singletons, _resolve_frontend
    _reset_runtime_singletons()
    from deeptutor.services.config import (
        ensure_runtime_settings_files,
        export_runtime_settings_to_env,
        load_auth_settings,
    )
    from deeptutor.services.setup import init_user_directories
    RUNTIME.mkdir(parents=True, exist_ok=True)
    init_user_directories(RUNTIME)
    ensure_runtime_settings_files()
    runtime_env = export_runtime_settings_to_env(overwrite=True)
    auth_enabled = bool(load_auth_settings()["enabled"])
    api_url = "http://127.0.0.1:8001"
    frontend = _resolve_frontend(
        RUNTIME, 3782, api_base=api_url, auth_enabled=auth_enabled, dev=False
    )
    env = os.environ.copy()
    env.update(runtime_env)
    env.update({
        "DEEPTUTOR_HOME": str(RUNTIME),
        "BACKEND_PORT": "8001",
        "FRONTEND_PORT": "3782",
        "PORT": "3782",
        "HOSTNAME": "127.0.0.1",
        "NEXT_PUBLIC_API_BASE": api_url,
        "DEEPTUTOR_API_BASE_URL": api_url,
        "NEXT_PUBLIC_AUTH_ENABLED": "true" if auth_enabled else "false",
        "DEEPTUTOR_AUTH_ENABLED": "true" if auth_enabled else "false",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8:replace",
    })
    return frontend, env

def start(open_browser: bool) -> None:
    import psutil
    old_state = read_state()
    old = old_state.get("processes", [])
    if old and all(owned_process(item) is not None for item in old) and healthy(FRONTEND_URL):
        print("DeepTutor is already running: " + FRONTEND_URL)
        if open_browser:
            webbrowser.open(FRONTEND_URL)
        return
    if old:
        stop()
    for port in (8001, 3782):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Port {port} is in use. No foreign process was stopped.")
    frontend, env = prepare()
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    entries = []
    commands = [
        ("backend", [sys.executable, "-m", "uvicorn", "deeptutor.api.main:app",
                     "--host", "127.0.0.1", "--port", "8001", "--no-access-log",
                     "--no-proxy-headers"], RUNTIME),
        ("frontend", frontend.command, frontend.cwd),
    ]
    try:
        for role, command, cwd in commands:
            with (log_dir / f"{role}.log").open("a", encoding="utf-8") as output:
                process = subprocess.Popen(
                    command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
            owner = psutil.Process(process.pid)
            entries.append({
                "role": role, "pid": process.pid,
                "created": owner.create_time(), "exe": owner.exe(),
            })
            STATE.write_text(json.dumps({"processes": entries}, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if not all(owned_process(item) is not None for item in entries):
                raise RuntimeError("A service exited. Check D:/DeepTutor/logs.")
            if healthy("http://127.0.0.1:8001/openapi.json") and healthy(FRONTEND_URL):
                print("DeepTutor is running: " + FRONTEND_URL)
                print("Configure the model locally in Settings; credentials stay on this computer.")
                if open_browser:
                    webbrowser.open(FRONTEND_URL)
                return
            time.sleep(1)
        raise RuntimeError("Startup did not become ready within 75 seconds. Check D:/DeepTutor/logs.")
    except Exception:
        stop()
        raise

def status() -> None:
    state = read_state()
    print(json.dumps({
        "url": FRONTEND_URL,
        "frontend_ok": healthy(FRONTEND_URL),
        "backend_ok": healthy("http://127.0.0.1:8001/openapi.json"),
        "processes": [
            {"role": item.get("role"), "pid": item.get("pid"), "running": owned_process(item) is not None}
            for item in state.get("processes", [])
        ],
    }, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.action == "start":
        start(not args.no_browser)
    elif args.action == "stop":
        stop()
    else:
        status()

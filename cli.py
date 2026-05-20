"""CLI commands for the Cron Observatory plugin."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parent
SERVER_MODULE = PLUGIN_DIR / "server.py"
PID_FILE = Path.home() / ".hermes" / "cronobs.pid"
DEFAULT_PORT = 8700
DEFAULT_HOST = "127.0.0.1"


def _is_running() -> dict | None:
    """Check if cronobs server is running. Returns dict with pid/port or None."""
    # 1. Check PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return {"pid": pid, "port": DEFAULT_PORT, "host": DEFAULT_HOST}
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            PID_FILE.unlink(missing_ok=True)

    # 2. Check port
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{DEFAULT_PORT}"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            pid = int(out.splitlines()[0])
            return {"pid": pid, "port": DEFAULT_PORT, "host": DEFAULT_HOST}
    except (subprocess.CalledProcessError, ValueError):
        pass

    return None


def _kill_running() -> bool:
    """Kill running cronobs server. Returns True if something was killed."""
    info = _is_running()
    if not info:
        return False
    try:
        os.kill(info["pid"], signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return False


def _start_server(foreground: bool = False, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST, open_browser: bool = True):
    """Start the cronobs server."""
    # Kill existing instance
    killed = _kill_running()
    if killed:
        print(f"Processo anterior encerrado.")

    env = os.environ.copy()
    env["CRONOBS_PORT"] = str(port)
    env["CRONOBS_HOST"] = host
    if not open_browser:
        env["CRONOBS_NO_BROWSER"] = "1"

    if foreground:
        print(f"cronobs ● http://{host}:{port}")
        print("Ctrl+C para encerrar\n")
        os.execve(sys.executable, [sys.executable, str(SERVER_MODULE)], env)
    else:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER_MODULE)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"cronobs iniciado (PID {proc.pid})")
        print(f"  http://{host}:{port}")


def cronobs_command(args) -> None:
    """Main handler for `hermes cronobs`."""
    action = getattr(args, "cronobs_action", None) or "start"

    if action == "status":
        info = _is_running()
        if info:
            print(f"cronobs rodando (PID {info['pid']})")
            print(f"  http://{info['host']}:{info['port']}")
            # Try to get job count
            try:
                import urllib.request
                resp = urllib.request.urlopen(f"http://{info['host']}:{info['port']}/api/jobs", timeout=3)
                data = json.loads(resp.read())
                print(f"  {len(data.get('jobs', []))} jobs carregados")
            except Exception:
                pass
        else:
            print("cronobs não está rodando")

    elif action == "stop":
        if _kill_running():
            print("cronobs encerrado")
        else:
            print("cronobs não estava rodando")

    elif action == "start":
        port = getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT
        host = getattr(args, "host", DEFAULT_HOST) or DEFAULT_HOST
        foreground = getattr(args, "foreground", False)
        no_browser = getattr(args, "no_browser", False)
        _start_server(
            foreground=foreground,
            port=port,
            host=host,
            open_browser=not no_browser,
        )

    else:
        # Default: start
        _start_server()


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Register `hermes cronobs` subcommand and its sub-subcommands."""
    subs = subparser.add_subparsers(dest="cronobs_action")

    # start (default)
    start_p = subs.add_parser("start", help="Iniciar o dashboard cronobs")
    start_p.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Porta do servidor (default: {DEFAULT_PORT})")
    start_p.add_argument("--host", default=DEFAULT_HOST, help=f"Host do servidor (default: {DEFAULT_HOST})")
    start_p.add_argument("--foreground", "-f", action="store_true", help="Rodar em foreground (bloqueia o terminal)")
    start_p.add_argument("--no-browser", action="store_true", help="Não abrir browser automaticamente")

    # stop
    subs.add_parser("stop", help="Encerrar o dashboard cronobs")

    # status
    subs.add_parser("status", help="Verificar se o cronobs está rodando")

    # Also accept bare `hermes cronobs` (no subcommand) → start
    subparser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"Porta (default: {DEFAULT_PORT})")
    subparser.add_argument("--host", default=DEFAULT_HOST, help=f"Host (default: {DEFAULT_HOST})")
    subparser.add_argument("--foreground", "-f", action="store_true", help="Rodar em foreground")
    subparser.add_argument("--no-browser", action="store_true", help="Não abrir browser")

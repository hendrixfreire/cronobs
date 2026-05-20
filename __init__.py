"""Cron Observatory — Hermes plugin for cron job dashboard.

Registers:
  - CLI command: ``hermes cronobs [start|stop|status]``
  - Slash command: ``/cronobs`` — shows status or opens dashboard
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import urllib.request
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PID_FILE = Path.home() / ".hermes" / "cronobs.pid"
DEFAULT_PORT = 8700
DEFAULT_HOST = "127.0.0.1"


def _is_running() -> dict | None:
    """Check if cronobs server is running."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return {"pid": pid, "port": DEFAULT_PORT, "host": DEFAULT_HOST}
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            PID_FILE.unlink(missing_ok=True)

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


def _slash_cronobs(raw_args: str) -> str | None:
    """Handler for /cronobs slash command."""
    info = _is_running()
    if not info:
        return "cronobs não está rodando. Use `hermes cronobs start` para iniciar."

    url = f"http://{info['host']}:{info['port']}"
    lines = [f"cronobs rodando (PID {info['pid']})", f"  {url}"]

    try:
        resp = urllib.request.urlopen(f"{url}/api/jobs", timeout=3)
        data = json.loads(resp.read())
        jobs = data.get("jobs", [])
        active = sum(1 for j in jobs if j.get("enabled", True))
        paused = len(jobs) - active
        profiles = len(set(j.get("_profile", "default") for j in jobs))
        lines.append(f"  {len(jobs)} jobs · {active} ativos · {paused} pausados · {profiles} profiles")
    except Exception:
        pass

    return "\n".join(lines)


def register(ctx) -> None:
    """Register cronobs CLI command and slash command."""
    import importlib.util
    from pathlib import Path as _Path

    cli_path = _Path(__file__).resolve().parent / "cli.py"
    spec = importlib.util.spec_from_file_location("cronobs_cli", cli_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {cli_path}")
    cli_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_mod)

    ctx.register_cli_command(
        name="cronobs",
        help="Cron Observatory — dashboard administrativo de cron jobs",
        setup_fn=cli_mod.register_cli,
        handler_fn=cli_mod.cronobs_command,
        description=(
            "Dashboard web local para visualizar e gerenciar cron jobs do Hermes. "
            "Suporta multi-profile, edição, duplicação, movimentação e rollback de jobs."
        ),
    )

    ctx.register_command(
        name="cronobs",
        handler=_slash_cronobs,
        description="Mostrar status do Cron Observatory",
    )

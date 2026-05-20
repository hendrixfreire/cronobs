#!/usr/bin/env python3
"""
cronobs.py — Hermes Cron Observatory v2
Servidor HTTP local na porta 8700. Lê jobs.json de todos os profiles em tempo real.
"""

import difflib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
PROFILES_DIR = HERMES_HOME / "profiles"
PORT = int(os.environ.get("CRONOBS_PORT", "8700"))
HOST = os.environ.get("CRONOBS_HOST", "127.0.0.1")


def get_all_jobs():
    """Lê jobs de todos os profiles disponíveis."""
    result = []

    # default
    default_path = HERMES_HOME / "cron" / "jobs.json"
    if default_path.exists():
        try:
            data = json.loads(default_path.read_bytes())
            for job in data.get("jobs", []):
                job["_profile"] = "default"
                result.append(job)
        except Exception:
            pass

    # profiles
    if PROFILES_DIR.exists():
        for profile_dir in sorted(PROFILES_DIR.iterdir()):
            if not profile_dir.is_dir():
                continue
            jobs_path = profile_dir / "cron" / "jobs.json"
            if jobs_path.exists():
                try:
                    data = json.loads(jobs_path.read_bytes())
                    for job in data.get("jobs", []):
                        job["_profile"] = profile_dir.name
                        result.append(job)
                except Exception:
                    pass

    return result



def now_iso():
    return datetime.now().astimezone().isoformat()


def profile_jobs_path(profile):
    profile = profile or "default"
    if profile == "default":
        return HERMES_HOME / "cron" / "jobs.json"
    return HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"


def load_jobs_data(profile):
    path = profile_jobs_path(profile)
    if not path.exists():
        raise FileNotFoundError(f"jobs.json não encontrado para profile {profile}: {path}")
    return json.loads(path.read_text(encoding="utf-8")), path


def load_or_empty_jobs_data(profile):
    path = profile_jobs_path(profile)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), path
    return {"jobs": [], "created_at": now_iso()}, path


def find_job(data, job_id):
    for idx, job in enumerate(data.get("jobs", [])):
        if job.get("id") == job_id:
            return idx, job
    raise KeyError(f"job não encontrado: {job_id}")


def parse_schedule_text(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("schedule vazio")

    interval = re.match(r"^(?:every\s+)?(\d+)\s*([mh])$", text, re.I)
    if interval:
        n = int(interval.group(1))
        unit = interval.group(2).lower()
        minutes = n * 60 if unit == "h" else n
        if minutes <= 0:
            raise ValueError("intervalo precisa ser maior que zero")
        display = f"every {minutes}m"
        return {"kind": "interval", "minutes": minutes, "display": display}, display

    parts = text.split()
    if len(parts) == 5:
        # Validação mínima: campos cron sem espaços internos, sem caracteres absurdos.
        allowed = re.compile(r"^[\d\*/,\-]+$")
        if not all(allowed.match(part) for part in parts):
            raise ValueError("cron expression contém caracteres inválidos")
        return {"kind": "cron", "expr": text, "display": text}, text

    raise ValueError("schedule inválido. Use cron de 5 campos (ex: 0 8 * * 1-5) ou intervalo (ex: every 2h, 30m)")


def job_public_copy(job, profile):
    item = dict(job)
    item["_profile"] = profile or "default"
    return item


def split_csv_lines(value):
    if value is None:
        return []
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[,\n]", str(value))
    return [str(item).strip() for item in parts if str(item).strip()]


def is_empty_value(value):
    return value is None or value == "" or value == [] or value == {}


def set_or_delete(job, key, value, *, empty_deletes=True):
    if empty_deletes and is_empty_value(value):
        # Não transformar `null`/vazio existente em deleção ruidosa só porque o
        # campo veio vazio do formulário. Se o usuário quiser limpar algo real,
        # removemos; se já está vazio, mantemos estável.
        if key in job and not is_empty_value(job.get(key)):
            del job[key]
            return True
        return False
    if job.get(key) != value:
        job[key] = value
        return True
    return False


def apply_job_updates(job, payload):
    changed = False
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("nome do job não pode ficar vazio")
        if job.get("name", "") != name:
            job["name"] = name
            changed = True

    if "prompt" in payload:
        prompt = payload.get("prompt") or ""
        if job.get("prompt", "") != prompt:
            job["prompt"] = prompt
            changed = True

    if "schedule_text" in payload:
        schedule_text = payload.get("schedule_text") or ""
        schedule, display = parse_schedule_text(schedule_text)
        if job.get("schedule") != schedule or job.get("schedule_display") != display:
            job["schedule"] = schedule
            job["schedule_display"] = display
            # O scheduler recalcula no tick; limpar evita mostrar próximo horário obsoleto.
            job["next_run_at"] = None
            changed = True

    if "deliver" in payload:
        deliver = (payload.get("deliver") or "").strip()
        changed = set_or_delete(job, "deliver", deliver) or changed

    if "skills_text" in payload:
        changed = set_or_delete(job, "skills", split_csv_lines(payload.get("skills_text"))) or changed

    if "toolsets_text" in payload:
        changed = set_or_delete(job, "enabled_toolsets", split_csv_lines(payload.get("toolsets_text"))) or changed

    if "no_agent" in payload:
        no_agent = bool(payload.get("no_agent"))
        if job.get("no_agent", False) != no_agent:
            job["no_agent"] = no_agent
            changed = True

    if "script" in payload:
        script = (payload.get("script") or "").strip()
        changed = set_or_delete(job, "script", script) or changed

    if "context_from_text" in payload:
        changed = set_or_delete(job, "context_from", split_csv_lines(payload.get("context_from_text"))) or changed

    if "model_provider" in payload or "model_name" in payload:
        provider = (payload.get("model_provider") or "").strip()
        model_name = (payload.get("model_name") or "").strip()
        current_model = job.get("model")
        if model_name:
            # Compatibilidade: jobs antigos podem ter `model` como string + `base_url`.
            # Sem provider explícito, preservamos o formato string para não quebrar job legado.
            if provider:
                model = {"model": model_name, "provider": provider}
            elif isinstance(current_model, str):
                model = model_name
            else:
                model = {"model": model_name}
            changed = set_or_delete(job, "model", model, empty_deletes=False) or changed
        else:
            changed = set_or_delete(job, "model", {}) or changed

    if "base_url" in payload:
        base_url = (payload.get("base_url") or "").strip()
        changed = set_or_delete(job, "base_url", base_url) or changed

    if job.get("no_agent") and not job.get("script"):
        raise ValueError("no-agent=True exige um script. Sem script, o scheduler não tem o que executar.")

    return changed


def diff_jobs(old_job, new_job):
    old = json.dumps(old_job, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    new = json.dumps(new_job, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(old, new, fromfile="antes", tofile="depois", lineterm=""))


def write_jobs_data(profile, data):
    data["updated_at"] = now_iso()
    path = profile_jobs_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)

    backups_dir = path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backups_dir / f"jobs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    if path.exists():
        shutil.copy2(path, backup_path)

    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(body, encoding="utf-8")
    # Valida que o arquivo temporário é JSON antes de substituir.
    json.loads(tmp_path.read_text(encoding="utf-8"))
    os.replace(tmp_path, path)
    return str(backup_path)



def list_profiles():
    profiles = ["default"]
    if PROFILES_DIR.exists():
        for profile_dir in sorted(PROFILES_DIR.iterdir()):
            if profile_dir.is_dir():
                profiles.append(profile_dir.name)
    return profiles


def public_profiles():
    result = []
    for profile in list_profiles():
        path = profile_jobs_path(profile)
        count = 0
        if path.exists():
            try:
                count = len(json.loads(path.read_text(encoding="utf-8")).get("jobs", []))
            except Exception:
                count = 0
        result.append({"name": profile, "jobs_path": str(path), "job_count": count, "has_jobs_file": path.exists()})
    return result


def new_job_id(existing_ids):
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing_ids:
            return candidate


def all_job_ids():
    ids = set()
    for profile in list_profiles():
        path = profile_jobs_path(profile)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ids.update(job.get("id") for job in data.get("jobs", []) if job.get("id"))
        except Exception:
            pass
    return ids


def reset_runtime_fields(job):
    job["repeat"] = {"times": job.get("repeat", {}).get("times"), "completed": 0}
    job["last_run_at"] = None
    job["last_status"] = None
    job["last_error"] = None
    job["last_delivery_error"] = None
    job["next_run_at"] = None
    job["paused_at"] = now_iso()
    job["paused_reason"] = "duplicated via cronobs dashboard"
    job["enabled"] = False
    job["state"] = "paused"
    return job


def duplicate_job(profile, job_id, target_profile=None, name=None):
    source_profile = profile or "default"
    target_profile = target_profile or source_profile
    source_data, _ = load_jobs_data(source_profile)
    _, source_job = find_job(source_data, job_id)
    target_data, target_path = load_or_empty_jobs_data(target_profile)
    job = json.loads(json.dumps(source_job, ensure_ascii=False))
    job["id"] = new_job_id(all_job_ids())
    job["name"] = (name or f"{source_job.get('name', job_id)} cópia").strip()
    job["created_at"] = now_iso()
    reset_runtime_fields(job)
    target_data.setdefault("jobs", []).append(job)
    backup = write_jobs_data(target_profile, target_data)
    tick = run_hermes(target_profile, "cron", "tick")
    return {"ok": True, "action": "duplicate", "source_profile": source_profile, "target_profile": target_profile,
            "source_job_id": job_id, "new_job_id": job["id"], "backup": backup, "tick": tick,
            "job": job_public_copy(job, target_profile), "path": str(target_path)}


def move_job(profile, job_id, target_profile):
    source_profile = profile or "default"
    target_profile = target_profile or "default"
    if source_profile == target_profile:
        raise ValueError("profile de destino precisa ser diferente do profile atual")
    source_data, source_path = load_jobs_data(source_profile)
    idx, job = find_job(source_data, job_id)
    target_data, target_path = load_or_empty_jobs_data(target_profile)
    if any(existing.get("id") == job_id for existing in target_data.get("jobs", [])):
        raise ValueError(f"já existe job com id {job_id} no profile {target_profile}")
    moved_job = json.loads(json.dumps(job, ensure_ascii=False))
    source_data["jobs"].pop(idx)
    target_data.setdefault("jobs", []).append(moved_job)
    source_backup = write_jobs_data(source_profile, source_data)
    target_backup = write_jobs_data(target_profile, target_data)
    source_tick = run_hermes(source_profile, "cron", "tick")
    target_tick = run_hermes(target_profile, "cron", "tick")
    return {"ok": True, "action": "move", "source_profile": source_profile, "target_profile": target_profile,
            "job_id": job_id, "source_backup": source_backup, "target_backup": target_backup,
            "source_tick": source_tick, "target_tick": target_tick,
            "source_path": str(source_path), "target_path": str(target_path), "job": job_public_copy(moved_job, target_profile)}


def delete_job(profile, job_id):
    profile = profile or "default"
    data, path = load_jobs_data(profile)
    idx, job = find_job(data, job_id)
    removed = data["jobs"].pop(idx)
    backup = write_jobs_data(profile, data)
    tick = run_hermes(profile, "cron", "tick")
    return {"ok": True, "action": "delete", "profile": profile, "job_id": job_id,
            "backup": backup, "tick": tick, "path": str(path), "removed_job": job_public_copy(removed, profile)}


def backup_public_item(profile, path):
    stat = path.stat()
    return {"profile": profile, "name": path.name, "path": str(path), "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()}


def list_backups(profile=None, limit=30):
    profiles = [profile or "default"] if profile else list_profiles()
    items = []
    for prof in profiles:
        backups_dir = profile_jobs_path(prof).parent / "backups"
        if not backups_dir.exists():
            continue
        for path in backups_dir.glob("jobs-*.json"):
            if path.is_file():
                items.append(backup_public_item(prof, path))
    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items[:max(1, min(int(limit or 30), 200))]


def safe_backup_path(profile, backup_name):
    profile = profile or "default"
    if not re.match(r"^jobs-\d{8}-\d{6}\.json$", backup_name or ""):
        raise ValueError("nome de backup inválido")
    path = profile_jobs_path(profile).parent / "backups" / backup_name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"backup não encontrado: {backup_name}")
    return path


def restore_backup(profile, backup_name):
    profile = profile or "default"
    backup_path = safe_backup_path(profile, backup_name)
    current_data, current_path = load_jobs_data(profile)
    restore_data = json.loads(backup_path.read_text(encoding="utf-8"))
    if not isinstance(restore_data, dict) or not isinstance(restore_data.get("jobs"), list):
        raise ValueError("backup não parece ser um jobs.json válido")
    pre_restore_backup = write_jobs_data(profile, current_data)
    body = json.dumps(restore_data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = current_path.with_name(f".{current_path.name}.restore.tmp")
    tmp_path.write_text(body, encoding="utf-8")
    json.loads(tmp_path.read_text(encoding="utf-8"))
    os.replace(tmp_path, current_path)
    tick = run_hermes(profile, "cron", "tick")
    return {"ok": True, "action": "restore", "profile": profile, "restored_from": str(backup_path),
            "pre_restore_backup": pre_restore_backup, "tick": tick, "path": str(current_path)}


def hermes_cmd(profile, *args):
    hermes = shutil.which("hermes") or str(HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes")
    cmd = [hermes]
    if profile and profile != "default":
        cmd += ["-p", profile]
    cmd += list(args)
    return cmd


def run_hermes(profile, *args, timeout=45):
    cmd = hermes_cmd(profile, *args)
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": res.returncode == 0,
            "cmd": " ".join(cmd),
            "stdout": res.stdout[-4000:],
            "stderr": res.stderr[-4000:],
            "returncode": res.returncode,
        }
    except Exception as e:
        return {"ok": False, "cmd": " ".join(cmd), "stderr": str(e), "returncode": -1}


SKILLS_CACHE = {}


def parse_skills_table(output):
    """Extrai skills da tabela Rich do `hermes skills list --enabled-only`."""
    skills = []
    seen = set()
    for line in (output or "").splitlines():
        if "│" not in line:
            continue
        cols = [col.strip() for col in line.split("│")[1:-1]]
        if len(cols) < 5:
            continue
        name, category, source, trust, status = cols[:5]
        if not name or name == "Name" or status != "enabled":
            continue
        if any(ch in name for ch in "┏┡└━"):
            continue
        if name in seen:
            continue
        seen.add(name)
        skills.append({"name": name, "category": category, "source": source, "trust": trust, "status": status})
    skills.sort(key=lambda item: (item.get("category") or "", item.get("name") or ""))
    return skills


def available_skills(profile):
    """Lista skills habilitadas no profile responsável pelo job."""
    profile = profile or "default"
    cache_key = profile
    if cache_key in SKILLS_CACHE:
        return SKILLS_CACHE[cache_key]
    cmd = hermes_cmd(profile, "skills", "list", "--enabled-only")
    env = os.environ.copy()
    env["COLUMNS"] = "240"
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=40, env=env)
        skills = parse_skills_table(res.stdout)
        payload = {"ok": res.returncode == 0, "profile": profile, "skills": skills,
                   "cmd": " ".join(cmd), "stderr": res.stderr[-1200:], "count": len(skills)}
    except Exception as e:
        payload = {"ok": False, "profile": profile, "skills": [], "error": str(e), "count": 0}
    SKILLS_CACHE[cache_key] = payload
    return payload


def update_job_payload(profile, job_id, payload, commit=False):
    data, path = load_jobs_data(profile)
    idx, job = find_job(data, job_id)
    old_job = dict(job)
    new_job = json.loads(json.dumps(job, ensure_ascii=False))
    changed = apply_job_updates(new_job, payload)
    diff = diff_jobs(old_job, new_job) if changed else ""

    result = {
        "ok": True,
        "profile": profile or "default",
        "job_id": job_id,
        "changed": changed,
        "diff": diff,
        "path": str(path),
        "job": job_public_copy(new_job, profile or "default"),
    }

    if commit and changed:
        data["jobs"][idx] = new_job
        backup = write_jobs_data(profile, data)
        tick = run_hermes(profile, "cron", "tick")
        result.update({"backup": backup, "tick": tick})
    return result


def set_job_status(profile, job_id, action):
    data, path = load_jobs_data(profile)
    idx, job = find_job(data, job_id)
    active = action == "resume"
    if action not in ("pause", "resume"):
        raise ValueError("ação inválida")
    job["enabled"] = active
    job["state"] = "scheduled" if active else "paused"
    job["paused_at"] = None if active else now_iso()
    job["paused_reason"] = None if active else "paused via cronobs dashboard"
    if not active:
        job["next_run_at"] = None
    data["jobs"][idx] = job
    backup = write_jobs_data(profile, data)
    tick = run_hermes(profile, "cron", "tick")
    return {"ok": True, "profile": profile, "job_id": job_id, "action": action, "backup": backup, "tick": tick, "job": job_public_copy(job, profile)}


HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cron Observatory · Hermes</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Doto:wght@400;700&family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  /* ── TOKENS ────────────────────────────────────────────────────────── */
  :root {
    --font-scale: 1;
    /* dark mode (default) */
    --bg: #0a0a0a;
    --bg-elevated: #111111;
    --bg-raised: #161616;
    --border: #1e1e1e;
    --border-visible: #2a2a2a;
    --text-display: #ffffff;
    --text-primary: #e8e8e8;
    --text-secondary: #999999;
    --text-disabled: #555555;
    --accent: #c41a1a;
    --accent-bright: #e02020;
    --accent-dim: rgba(196,26,26,0.15);
    --success: #2ecc6a;
    --success-subtle: rgba(46,204,106,0.1);
    --warning: #d4a843;
    --warning-subtle: rgba(212,168,67,0.1);
    --info: #5b9bf6;
    --info-subtle: rgba(91,155,246,0.1);
    --purple: #b87dff;
    --purple-subtle: rgba(184,125,255,0.1);
  }

  body.light {
    --bg: #f5f5f5;
    --bg-elevated: #ffffff;
    --bg-raised: #efefef;
    --border: #e0e0e0;
    --border-visible: #cccccc;
    --text-display: #000000;
    --text-primary: #1a1a1a;
    --text-secondary: #666666;
    --text-disabled: #aaaaaa;
    --accent: #b01515;
    --accent-bright: #cc1a1a;
    --accent-dim: rgba(176,21,21,0.1);
    --success: #1a7f3c;
    --success-subtle: rgba(26,127,60,0.08);
    --warning: #8a6800;
    --warning-subtle: rgba(138,104,0,0.08);
    --info: #1a5ab8;
    --info-subtle: rgba(26,90,184,0.08);
    --purple: #7340cc;
    --purple-subtle: rgba(115,64,204,0.08);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text-primary);
    font-family: 'Space Grotesk', sans-serif;
    font-size: calc(14px * var(--font-scale));
    line-height: 1.5;
    min-height: 100vh;
    transition: background 0.3s ease, color 0.3s ease;
  }

  /* ── PROGRESS BAR ── */
  #progress-bar {
    position: fixed;
    top: 0; left: 0;
    height: 2px;
    width: 0%;
    background: var(--accent);
    z-index: 1000;
    transition: width 0.3s ease;
  }

  /* ── NEXT JOB BANNER ── */
  #next-banner {
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
    padding: 14px 48px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
  }

  .next-left {
    display: flex;
    align-items: center;
    gap: 24px;
    min-width: 0;
    flex-shrink: 1;
  }

  .next-label {
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--accent-bright);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .next-divider-v {
    width: 1px;
    height: 32px;
    background: var(--border-visible);
    flex-shrink: 0;
  }

  .next-timer-wrap {
    width: 4px;
    height: 48px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
    flex-shrink: 0;
    position: relative;
  }

  .next-timer-bar {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 0%;
    border-radius: 2px;
    transition: height 0.3s ease, background 0.5s ease;
  }

  .next-countdown {
    font-family: 'Doto', monospace;
    font-size: calc(32px * var(--font-scale));
    font-weight: 700;
    color: var(--accent-bright);
    line-height: 1;
    white-space: nowrap;
    flex-shrink: 0;
    min-width: 170px;
  }

  .next-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .next-job-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .next-job-name {
    font-size: calc(15px * var(--font-scale));
    font-weight: 600;
    color: var(--text-display);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .next-profile-tag-inline {
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 2px;
    border: 1px solid rgba(196,26,26,0.25);
    flex-shrink: 0;
    white-space: nowrap;
  }

  .next-job-meta {
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale));
    color: var(--text-secondary);
    letter-spacing: 0.04em;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .next-job-meta span { white-space: nowrap; }

  .next-meta {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }

  .next-meta #clock {
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale));
    color: var(--text-secondary);
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  .next-meta .last-update {
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale));
    color: var(--text-disabled);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .next-meta .theme-toggle,
  .next-meta .sync-btn,
  .next-meta .header-backup-btn {
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 3px 8px;
    border: 1px solid var(--border-visible);
    border-radius: 3px;
    background: transparent;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
  }
  .next-meta .theme-toggle { color: var(--text-disabled); display: flex; align-items: center; gap: 4px; }
  .next-meta .sync-btn { color: var(--text-disabled); display: flex; align-items: center; gap: 4px; }
  .next-meta .header-backup-btn { color: var(--text-disabled); }
  .next-meta .theme-toggle:hover,
  .next-meta .sync-btn:hover,
  .next-meta .header-backup-btn:hover { border-color: var(--accent); color: var(--accent-bright); }
  .next-meta .refresh-indicator {
    display: flex; align-items: center; gap: 4px;
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale)); color: var(--text-disabled);
    letter-spacing: 0.04em; text-transform: uppercase;
  }
  .next-meta .refresh-dot {
    width: 4px; height: 4px; border-radius: 50%;
    background: var(--accent);
    animation: pulse 2s ease-in-out infinite;
  }

  /* ── HEADER ── */
  header {
    border-bottom: 1px solid var(--border);
    padding: 24px 48px 20px;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 100;
    transition: background 0.3s ease;
  }

  body.light header { background: rgba(245,245,245,0.95); backdrop-filter: blur(8px); }
  body:not(.light) header { background: rgba(10,10,10,0.95); backdrop-filter: blur(8px); }

  .header-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
  }

  .brand { display: flex; align-items: baseline; gap: 14px; }

  h1 {
    font-family: 'Space Grotesk', sans-serif;
    font-size: calc(17px * var(--font-scale));
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-display);
  }

  .brand-sub {
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale));
    color: var(--text-disabled);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  /* theme toggle */
  .theme-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale));
    color: var(--text-disabled);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 5px 10px;
    border: 1px solid var(--border-visible);
    border-radius: 4px;
    background: transparent;
    transition: border-color 0.2s, color 0.2s;
    user-select: none;
  }
  .theme-toggle:hover { border-color: var(--accent); color: var(--accent-bright); }
  .theme-icon { font-size: calc(12px * var(--font-scale)); }

  /* font size controls */
  .font-controls {
    display: flex; align-items: center; gap: 2px;
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale));
    color: var(--text-disabled);
    letter-spacing: 0.04em;
    user-select: none;
  }
  .font-btn {
    cursor: pointer;
    border: 1px solid var(--border-visible);
    border-radius: 4px;
    background: transparent;
    color: var(--text-disabled);
    width: 24px; height: 24px;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Space Grotesk', sans-serif;
    font-size: calc(13px * var(--font-scale));
    font-weight: 600;
    line-height: 1;
    padding: 0;
    transition: border-color 0.2s, color 0.2s;
  }
  .font-btn:hover { border-color: var(--accent); color: var(--accent-bright); }
  .font-label {
    min-width: 32px; text-align: center;
    font-size: calc(9px * var(--font-scale));
    color: var(--text-secondary);
    letter-spacing: 0.02em;
  }

  .sync-btn {
    display: flex; align-items: center; gap: 4px;
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale)); color: var(--text-disabled); letter-spacing: 0.05em;
    text-transform: uppercase; padding: 5px 10px;
    border: 1px solid var(--border-visible);
    border-radius: 4px; background: transparent; cursor: pointer;
    transition: all 0.2s; user-select: none;
  }
  .sync-btn:hover { border-color: var(--accent); color: var(--accent-bright); }
  .sync-btn.spinning .sync-icon { animation: spin 0.7s ease-in-out; }
  @keyframes spin { 0%{transform:rotate(0deg)} 100%{transform:rotate(360deg)} }

  .refresh-indicator {
    display: flex; align-items: center; gap: 6px;
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale)); color: var(--text-disabled);
    letter-spacing: 0.05em; text-transform: uppercase;
  }

  .refresh-dot {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--accent);
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse { 0%,100%{opacity:0.3} 50%{opacity:1} }

  #clock {
    font-family: 'Space Mono', monospace;
    font-size: calc(11px * var(--font-scale)); color: var(--text-secondary);
    letter-spacing: 0.04em;
  }

  .header-backup-btn {
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale)); color: var(--text-disabled);
    letter-spacing: 0.05em; text-transform: uppercase;
    padding: 5px 10px;
    border: 1px solid var(--border-visible);
    border-radius: 4px; background: transparent; cursor: pointer;
    transition: all 0.2s; user-select: none;
  }
  .header-backup-btn:hover { border-color: var(--accent); color: var(--accent-bright); }

  /* ── STATS ── */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
  }

  .stat-block {
    background: var(--bg);
    padding: 14px 18px;
    display: flex; flex-direction: column; gap: 3px;
    transition: background 0.3s;
  }

  .stat-value {
    font-family: 'Doto', monospace;
    font-size: calc(32px * var(--font-scale)); font-weight: 700;
    color: var(--text-display); line-height: 1;
  }
  .stat-value.accent { color: var(--accent-bright); }
  .stat-value.success { color: var(--success); }
  .stat-value.muted { color: var(--text-secondary); }
  .stat-value.info { color: var(--info); }

  .stat-label {
    font-family: 'Space Mono', monospace;
    font-size: calc(9px * var(--font-scale)); letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--text-disabled);
  }

  .last-update {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    color: var(--text-disabled); letter-spacing: 0.04em; text-transform: uppercase;
  }

  /* ── CONTROLS ── */
  .controls {
    padding: 14px 48px;
    display: flex; align-items: center; gap: 10px;
    border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }

  .filter-group {
    display: flex; gap: 2px;
    background: var(--bg-elevated);
    border: 1px solid var(--border-visible);
    border-radius: 4px; padding: 3px;
  }

  .filter-btn {
    font-family: 'Space Mono', monospace;
    font-size: calc(10px * var(--font-scale)); letter-spacing: 0.08em; text-transform: uppercase;
    padding: 4px 10px; border: none; background: transparent;
    color: var(--text-secondary); cursor: pointer;
    border-radius: 2px; transition: all 0.15s ease; white-space: nowrap;
  }
  .filter-btn.active { background: var(--accent); color: white; }
  .filter-btn:hover:not(.active) { color: var(--text-primary); background: var(--border-visible); }

  .ctrl-label {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--text-disabled); margin-left: 6px;
  }

  select {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    letter-spacing: 0.05em;
    background: var(--bg-elevated);
    border: 1px solid var(--border-visible);
    color: var(--text-secondary);
    padding: 4px 10px; border-radius: 4px; cursor: pointer;
    outline: none; appearance: none; -webkit-appearance: none;
    transition: border-color 0.2s, color 0.2s;
  }
  select:focus { border-color: var(--accent); color: var(--text-primary); }

  .spacer { flex: 1; }

  #job-count {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    color: var(--text-disabled); letter-spacing: 0.05em;
  }

  /* ── GRID ── */
  main { padding: 20px 48px 48px; }

  #grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
    gap: 14px;
  }

  /* ── JOB CARD ── */
  .card {
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    display: flex; flex-direction: column;
    transition: border-color 0.2s ease, background 0.3s ease;
  }
  .card:hover { border-color: var(--border-visible); }
  .card.paused { opacity: 0.55; }
  .card.paused:hover { opacity: 0.8; }

  /* card header */
  .card-header {
    padding: 14px 16px 10px;
    display: flex; align-items: flex-start;
    justify-content: space-between; gap: 10px;
  }

  .card-title-row {
    display: flex; flex-direction: column; gap: 4px; min-width: 0;
  }

  .card-name {
    font-size: calc(13px * var(--font-scale)); font-weight: 600;
    color: var(--text-display); letter-spacing: -0.01em;
    line-height: 1.3; word-break: break-word;
  }

  .card-profile-tag {
    font-family: 'Space Mono', monospace;
    font-size: calc(8px * var(--font-scale)); letter-spacing: 0.1em; text-transform: uppercase;
    padding: 2px 6px; border-radius: 2px;
    background: var(--accent-dim);
    color: var(--accent-bright);
    border: 1px solid rgba(196,26,26,0.2);
    align-self: flex-start;
  }

  .card-badges { display: flex; gap: 5px; align-items: center; flex-shrink: 0; }

  .status-badge {
    font-family: 'Space Mono', monospace;
    font-size: calc(8px * var(--font-scale)); letter-spacing: 0.1em; text-transform: uppercase;
    padding: 3px 7px; border-radius: 2px; white-space: nowrap;
  }
  .status-badge.active {
    background: var(--success-subtle); color: var(--success);
    border: 1px solid rgba(46,204,106,0.25);
  }
  .status-badge.paused {
    background: rgba(85,85,85,0.1); color: var(--text-disabled);
    border: 1px solid var(--border);
  }

  /* card divider */
  .card-divider { height: 1px; background: var(--border); margin: 0 16px; }

  /* card body — schedule + timing */
  .card-body {
    padding: 10px 16px; display: flex; flex-direction: column; gap: 6px;
  }

  .info-row { display: flex; align-items: baseline; gap: 8px; }

  .info-label {
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--text-disabled); width: 58px; flex-shrink: 0;
  }

  .info-value { font-size: calc(12px * var(--font-scale)); color: var(--text-secondary); }
  .info-value.highlight { color: var(--text-primary); font-weight: 500; }
  .info-value.urgent { color: var(--accent-bright); font-weight: 600; }
  .info-value.soon { color: var(--warning); }

  .status-ok {
    color: var(--success); font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
  }
  .status-error {
    color: var(--accent-bright); font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
  }
  .status-none {
    color: var(--text-disabled); font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
  }

  /* ── CARD SECTIONS ── */
  .card-section {
    border-top: 1px solid var(--border);
    padding: 10px 16px;
  }

  .section-title {
    font-family: 'Space Mono', monospace; font-size: calc(8px * var(--font-scale));
    letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--text-disabled); margin-bottom: 7px;
  }

  /* execution + deliver row */
  .card-stats-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
  }

  .executions-block { display: flex; flex-direction: column; gap: 1px; }

  .executions-number {
    font-family: 'Doto', monospace; font-size: calc(26px * var(--font-scale)); font-weight: 700;
    color: var(--text-display); line-height: 1;
  }

  .executions-label {
    font-family: 'Space Mono', monospace; font-size: calc(8px * var(--font-scale));
    letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-disabled);
  }

  .deliver-block { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; }

  .deliver-platform {
    font-family: 'Space Mono', monospace; font-size: calc(8px * var(--font-scale));
    letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-disabled);
  }

  .deliver-channel { font-size: calc(12px * var(--font-scale)); font-weight: 500; color: var(--text-primary); }

  /* ── TAG GROUPS ── */
  .tag-group { display: flex; flex-wrap: wrap; gap: 4px; }

  .tag {
    font-family: 'Space Mono', monospace; font-size: calc(8px * var(--font-scale));
    letter-spacing: 0.06em; text-transform: uppercase;
    padding: 2px 6px; border-radius: 2px;
  }

  /* skills */
  .tag-skill {
    background: var(--success-subtle); color: var(--success);
    border: 1px solid rgba(46,204,106,0.2);
  }
  /* toolsets */
  .tag-toolset {
    background: var(--info-subtle); color: var(--info);
    border: 1px solid rgba(91,155,246,0.2);
  }
  /* llm / model */
  .tag-llm {
    background: var(--purple-subtle); color: var(--purple);
    border: 1px solid rgba(184,125,255,0.2);
  }
  /* no-agent */
  .tag-no-agent {
    background: var(--accent-dim); color: var(--accent-bright);
    border: 1px solid rgba(196,26,26,0.2);
  }
  /* script */
  .tag-script {
    background: var(--warning-subtle); color: var(--warning);
    border: 1px solid rgba(212,168,67,0.2);
  }
  /* chain */
  .tag-chain {
    background: var(--purple-subtle); color: var(--purple);
    border: 1px solid rgba(184,125,255,0.2);
  }



  /* ── VIEW MODES / COLLAPSE ── */
  .view-toggle.active { background: var(--text-display); color: var(--bg); }
  .view-options { display: flex; align-items: center; gap: 8px; }
  #kanban-dimension-wrap { display: none; align-items: center; gap: 8px; }

  .collapse-btn {
    width: 24px; height: 24px; border-radius: 4px;
    border: 1px solid var(--border-visible); background: transparent;
    color: var(--text-secondary); cursor: pointer;
    font-family: 'Space Mono', monospace; font-size: calc(12px * var(--font-scale));
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s ease;
  }
  .collapse-btn:hover { color: var(--accent-bright); border-color: var(--accent); }
  .card.collapsed .collapse-btn { transform: rotate(-90deg); }
  .card.collapsed .card-collapsible { display: none; }
  .card.collapsed { opacity: 0.9; }

  .card-footer {
    margin-top: auto; padding: 10px 16px 12px;
    display: flex; justify-content: flex-end; align-items: center;
    border-top: 1px solid var(--border);
  }
  .card.compact .card-header { padding-bottom: 8px; }
  .card.compact .card-body { padding-top: 8px; padding-bottom: 8px; }
  .card.compact .info-label { width: 58px; }
  .card.compact.collapsed .card-collapsible { display: block !important; }
  .card.compact .prompt-icon,
  .card.compact .card-top-metrics,
  .card.compact .collapse-btn,
  .card.compact .card-divider,
  .card.compact .card-section,
  .card.compact .compact-hide { display: none !important; }

  .card-header-actions {
    display: flex; align-items: flex-start; gap: 8px; flex-shrink: 0;
  }
  .card-top-metrics {
    display: flex; align-items: flex-start; gap: 7px;
  }
  .executions-top {
    min-width: 44px; text-align: right;
    display: flex; flex-direction: column; gap: 1px; align-items: flex-end;
  }
  .executions-top-number {
    font-family: 'Doto', monospace; font-size: calc(25px * var(--font-scale)); font-weight: 700;
    color: var(--text-display); line-height: 0.9;
  }
  .executions-top-label {
    font-family: 'Space Mono', monospace; font-size: calc(7px * var(--font-scale));
    letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-disabled);
  }

  .profile-filter-hint {
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.05em; color: var(--text-disabled);
  }

  #grid.view-kanban {
    display: flex !important; align-items: flex-start; gap: 12px; overflow-x: auto;
  }
  .kanban-column {
    min-width: 330px; max-width: 390px; flex: 1;
    background: var(--bg-elevated); border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden;
  }
  .kanban-header {
    padding: 10px 12px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; background: var(--bg-elevated); z-index: 1;
  }
  .kanban-title {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-primary);
  }
  .kanban-count {
    font-family: 'Doto', monospace; font-size: calc(20px * var(--font-scale)); color: var(--text-secondary);
  }
  .kanban-cards { padding: 10px; display: flex; flex-direction: column; gap: 10px; min-height: 120px; }
  .kanban-cards.drag-over { outline: 1px dashed var(--accent-bright); outline-offset: -5px; background: var(--accent-dim); }
  .kanban-cards .card { width: 100%; }
  .card.dragging { opacity: 0.45; }

  .kanban-save-bar {
    position: fixed; right: 24px; bottom: 24px; z-index: 7000;
    display: none; align-items: center; gap: 10px;
    padding: 10px 12px; border-radius: 6px;
    background: var(--bg-elevated); border: 1px solid var(--border-visible);
    box-shadow: 0 12px 40px rgba(0,0,0,0.35);
  }
  .kanban-save-bar.open { display: flex; }
  .kanban-save-meta { font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.08em; }
  .kanban-save-btn { background: var(--success); border: 1px solid var(--success); color: #07130b; font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
  .kanban-save-btn:hover { filter: brightness(1.08); }

  #grid.view-list { display: block !important; }
  .jobs-table-wrap {
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg-elevated); overflow: auto;
  }
  table.jobs-table { width: 100%; border-collapse: collapse; min-width: 980px; }
  .jobs-table th, .jobs-table td {
    padding: 10px 12px; border-bottom: 1px solid var(--border);
    text-align: left; vertical-align: middle; font-size: calc(12px * var(--font-scale));
  }
  .jobs-table th {
    position: sticky; top: 0; z-index: 2;
    background: var(--bg-elevated); color: var(--text-disabled);
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale));
    letter-spacing: 0.1em; text-transform: uppercase; cursor: pointer;
    user-select: none;
  }
  .jobs-table th:hover { color: var(--text-primary); }
  .jobs-table tr:hover td { background: var(--bg-raised); }
  .jobs-table .sort-mark { color: var(--accent-bright); margin-left: 4px; }
  .jobs-table th[draggable="true"] { cursor: grab; }
  .jobs-table th.dragging { opacity: 0.45; color: var(--accent-bright); }
  .jobs-table th.drag-over { outline: 1px dashed var(--accent-bright); outline-offset: -4px; }
  .table-name { color: var(--text-display); font-weight: 600; }
  .table-muted { color: var(--text-disabled); font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); }

  .prompt-icon {
    display: inline-flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; margin-left: 6px;
    border: 1px solid var(--border-visible); border-radius: 50%;
    color: var(--text-disabled); background: transparent;
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale));
    cursor: help; vertical-align: middle; user-select: none;
    transition: all 0.15s ease;
  }
  .prompt-icon:hover { color: var(--accent-bright); border-color: var(--accent); background: var(--accent-dim); }
  .prompt-tooltip {
    position: fixed; z-index: 9999; display: none;
    max-width: min(760px, calc(100vw - 48px)); max-height: min(520px, calc(100vh - 48px));
    overflow: auto; padding: 14px 16px;
    background: var(--bg-elevated); color: var(--text-primary);
    border: 1px solid var(--border-visible); border-radius: 6px;
    box-shadow: 0 18px 60px rgba(0,0,0,0.45);
  }
  .prompt-tooltip-title {
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale)); letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--text-disabled); margin-bottom: 8px;
  }
  .prompt-tooltip pre {
    margin: 0; white-space: pre-wrap; word-break: break-word;
    font-family: 'Space Mono', monospace; font-size: calc(11px * var(--font-scale)); line-height: 1.55;
    color: var(--text-primary);
  }


  /* ── ADMIN ACTIONS / MODAL ── */
  .job-actions { display: flex; gap: 6px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }
  .job-action-btn {
    font-family: 'Space Mono', monospace; font-size: calc(8px * var(--font-scale));
    letter-spacing: 0.08em; text-transform: uppercase;
    padding: 3px 6px; border-radius: 3px;
    border: 1px solid var(--border-visible); background: transparent;
    color: var(--text-disabled); cursor: pointer; transition: all 0.15s ease;
  }
  .job-action-btn:hover { color: var(--text-primary); border-color: var(--accent); }
  .job-action-btn.edit:hover { color: #fff; border-color: #fff; background: rgba(255,255,255,0.04); }
  .job-action-btn.danger:hover { color: var(--accent-bright); border-color: var(--accent); }
  .job-action-btn.run:hover { color: var(--success); border-color: var(--success); }
  .job-action-btn.struct:hover { color: var(--warning); border-color: var(--warning); }
  .job-action-btn.delete:hover { color: var(--accent-bright); border-color: var(--accent-bright); background: var(--accent-dim); }

  .backup-list { display: flex; flex-direction: column; gap: 8px; }
  .backup-item {
    border: 1px solid var(--border); border-radius: 4px; padding: 10px;
    display: flex; justify-content: space-between; gap: 12px; align-items: center;
    background: var(--bg);
  }
  .backup-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .backup-name { font-family: 'Space Mono', monospace; font-size: calc(11px * var(--font-scale)); color: var(--text-primary); word-break: break-all; }
  .backup-meta { font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale)); color: var(--text-disabled); }

  .modal-backdrop {
    position: fixed; inset: 0; z-index: 8000; display: none;
    background: rgba(0,0,0,0.55); backdrop-filter: blur(6px);
  }
  .modal-backdrop.open { display: block; }
  .edit-modal {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 8001;
    width: min(760px, 100vw); display: none; flex-direction: column;
    background: var(--bg-elevated); border-left: 1px solid var(--border-visible);
    box-shadow: -18px 0 60px rgba(0,0,0,0.45);
  }
  .edit-modal.open { display: flex; }
  .modal-header {
    padding: 18px 20px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; gap: 14px; align-items: flex-start;
  }
  .modal-title { font-size: calc(16px * var(--font-scale)); font-weight: 700; color: var(--text-display); }
  .modal-subtitle { font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); color: var(--text-disabled); margin-top: 4px; }
  .modal-body { padding: 18px 20px; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
  .modal-field { display: flex; flex-direction: column; gap: 6px; }
  .modal-field label {
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale)); letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--text-disabled);
  }
  .modal-field input, .modal-field textarea {
    width: 100%; box-sizing: border-box; resize: vertical;
    font-family: 'Space Mono', monospace; font-size: calc(12px * var(--font-scale)); line-height: 1.55;
    color: var(--text-primary); background: var(--bg); border: 1px solid var(--border-visible);
    border-radius: 4px; padding: 10px; outline: none;
  }
  .modal-field input[type="checkbox"] { width: auto; accent-color: var(--accent); }
  .modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .modal-check-row {
    display: flex; align-items: center; gap: 8px;
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.08em;
  }
  .modal-help { font-size: calc(11px * var(--font-scale)); color: var(--text-disabled); line-height: 1.45; }
  .multi-select { position: relative; }
  .multi-select-button {
    width: 100%; min-height: 39px; text-align: left;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-family: 'Space Mono', monospace; font-size: calc(11px * var(--font-scale)); line-height: 1.4;
    background: var(--bg-raised); color: var(--text-secondary);
    border: 1px solid var(--border-visible); border-radius: 4px; padding: 8px 10px;
    cursor: pointer;
  }
  .multi-select-button:hover, .multi-select.open .multi-select-button { border-color: var(--accent); color: var(--text-primary); }
  .multi-select-caret { color: var(--text-disabled); flex: 0 0 auto; }
  .skill-menu {
    display: none; position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 9002;
    background: var(--bg-elevated); border: 1px solid var(--border-visible); border-radius: 6px;
    box-shadow: 0 18px 40px rgba(0,0,0,0.4); padding: 8px;
  }
  .multi-select.open .skill-menu { display: block; }
  .skill-search {
    width: 100%; font-family: 'Space Mono', monospace; font-size: calc(11px * var(--font-scale));
    background: var(--bg-raised); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 4px; padding: 8px; margin-bottom: 8px;
  }
  .skill-options { max-height: 260px; overflow: auto; display: flex; flex-direction: column; gap: 2px; }
  .skill-option {
    display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center;
    padding: 7px 6px; border-radius: 4px; cursor: pointer; color: var(--text-secondary);
    font-size: calc(12px * var(--font-scale));
  }
  .skill-option:hover { background: var(--bg-raised); color: var(--text-primary); }
  .skill-option input { accent-color: var(--success); }
  .skill-option-name { font-family: 'Space Mono', monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .skill-option-category { font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale)); color: var(--text-disabled); text-transform: uppercase; }
  .skill-picker-actions { display: flex; justify-content: space-between; gap: 8px; margin-top: 8px; }
  .skill-mini-btn {
    font-family: 'Space Mono', monospace; font-size: calc(9px * var(--font-scale)); text-transform: uppercase; letter-spacing: 0.08em;
    border: 1px solid var(--border-visible); background: transparent; color: var(--text-disabled); border-radius: 4px;
    padding: 5px 7px; cursor: pointer;
  }
  .skill-mini-btn:hover { color: var(--text-primary); border-color: var(--accent); }
  @media (max-width: 720px) { .modal-grid { grid-template-columns: 1fr; } }
  .modal-field textarea { min-height: 260px; }
  .modal-field input:focus, .modal-field textarea:focus { border-color: var(--accent); }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px; border-top: 1px solid var(--border); }
  .modal-btn {
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); letter-spacing: 0.08em;
    text-transform: uppercase; padding: 8px 12px; border-radius: 4px;
    border: 1px solid var(--border-visible); background: transparent; color: var(--text-secondary); cursor: pointer;
  }
  .modal-btn.primary { background: var(--success); border-color: var(--success); color: #07130b; font-weight: 700; }
  .modal-btn:hover { color: var(--text-primary); border-color: var(--accent); }
  .modal-btn.primary:hover { color: #07130b; border-color: var(--success); filter: brightness(1.08); }
  .diff-box {
    display: none; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow: auto;
    font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale)); line-height: 1.45;
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 10px;
    color: var(--text-primary);
  }
  .diff-box.open { display: block; }
  .toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    z-index: 9000; display: none; padding: 10px 14px; border-radius: 4px;
    background: var(--bg-elevated); border: 1px solid var(--border-visible);
    color: var(--text-primary); font-family: 'Space Mono', monospace; font-size: calc(10px * var(--font-scale));
    box-shadow: 0 12px 40px rgba(0,0,0,0.35);
  }
  .toast.open { display: block; }

  /* ── EMPTY ── */
  #empty-state { display: none; padding: 64px 0; text-align: center; }
  #empty-state p {
    font-family: 'Space Mono', monospace; font-size: calc(12px * var(--font-scale));
    color: var(--text-disabled); letter-spacing: 0.05em; text-transform: uppercase;
  }

  #loading {
    padding: 64px 0; text-align: center;
    font-family: 'Space Mono', monospace; font-size: calc(12px * var(--font-scale));
    color: var(--text-disabled); letter-spacing: 0.1em; text-transform: uppercase;
  }

  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border-visible); border-radius: 2px; }

  @media (max-width: 768px) {
    #next-banner { padding: 12px 20px; flex-wrap: wrap; gap: 10px; }
    .next-left { gap: 16px; flex-shrink: 1; min-width: 0; }
    .next-left .next-divider-v { height: 24px; }
    .next-meta { flex-wrap: wrap; gap: 8px; }
    .next-meta #clock { font-size: calc(10px * var(--font-scale)); }
    .next-meta .last-update { display: none; }
    .next-meta .refresh-indicator { display: none; }
    header { padding: 16px 20px 14px; }
    .controls { padding: 10px 20px; }
    main { padding: 14px 20px 32px; }
    .stats-row { grid-template-columns: repeat(3, 1fr); }
    #grid { grid-template-columns: 1fr; }
    .next-countdown { font-size: calc(24px * var(--font-scale)); }
    .next-timer-wrap { height: 36px; }
  }
</style>
</head>
<body>

<div id="progress-bar"></div>

<!-- NEXT JOB BANNER -->
<div id="next-banner">
  <div class="next-left">
    <span class="next-label">↗ Próximo</span>
    <div class="next-divider-v"></div>
    <div class="next-timer-wrap">
      <div class="next-timer-bar" id="next-timer-bar"></div>
    </div>
    <div class="next-countdown" id="next-countdown">—</div>
    <div class="next-info">
      <div class="next-job-name-row">
        <div class="next-job-name" id="next-name">carregando...</div>
        <div class="next-profile-tag-inline" id="next-profile-tag" style="display:none">—</div>
      </div>
      <div class="next-job-meta">
        <span id="next-schedule">—</span>
        <span id="next-deliver">—</span>
      </div>
    </div>
  </div>
  <div class="next-meta">
    <div id="clock">—</div>
    <div class="last-update" id="last-update">atualizado: —</div>
    <div class="refresh-indicator">
      <div class="refresh-dot"></div>
      <span id="refresh-label">ao vivo · 30s</span>
    </div>
    <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">
      <span class="theme-icon" id="theme-icon">☀</span>
      <span id="theme-label">claro</span>
    </button>
    <div class="font-controls">
      <button class="font-btn" onclick="decreaseFont()" title="Diminuir fonte">A-</button>
      <span class="font-label" id="font-label">100%</span>
      <button class="font-btn" onclick="increaseFont()" title="Aumentar fonte">A+</button>
    </div>
    <button class="sync-btn" id="sync-btn" onclick="manualRefresh()">
      <span class="sync-icon">↻</span>
      <span>agora</span>
    </button>
    <button class="header-backup-btn" onclick="openRollbackModal()">Backups</button>
  </div>
</div>

<header>
  <div class="header-top">
    <div class="brand">
      <h1>Cron Observatory</h1>
      <span class="brand-sub">Hermes Agent</span>
    </div>
  </div>

  <div class="stats-row">
    <div class="stat-block">
      <div class="stat-value" id="stat-total">—</div>
      <div class="stat-label">jobs totais</div>
    </div>
    <div class="stat-block">
      <div class="stat-value success" id="stat-active">—</div>
      <div class="stat-label">ativos</div>
    </div>
    <div class="stat-block">
      <div class="stat-value accent" id="stat-paused">—</div>
      <div class="stat-label">pausados</div>
    </div>
    <div class="stat-block">
      <div class="stat-value info" id="stat-executions">—</div>
      <div class="stat-label">execuções totais</div>
    </div>
    <div class="stat-block">
      <div class="stat-value" id="stat-profiles">—</div>
      <div class="stat-label">profiles</div>
    </div>
  </div>
</header>

<div class="controls">
  <!-- status filter -->
  <div class="filter-group" id="filter-status">
    <button class="filter-btn" data-filter="all">Todos</button>
    <button class="filter-btn active" data-filter="active">Ativos</button>
    <button class="filter-btn" data-filter="paused">Pausados</button>
  </div>

  <!-- profile filter -->
  <span class="ctrl-label">Profile</span>
  <div class="filter-group" id="filter-profile">
    <button class="filter-btn active" data-profile="all">Todos</button>
  </div>
  <span class="profile-filter-hint">Shift = multi</span>

  <!-- view mode -->
  <span class="ctrl-label" style="margin-left:6px">Visual</span>
  <div class="filter-group" id="view-mode">
    <button class="filter-btn view-toggle active" data-view="cards">Cards</button>
    <button class="filter-btn view-toggle" data-view="kanban">Kanban</button>
    <button class="filter-btn view-toggle" data-view="list">Lista</button>
  </div>

  <div id="kanban-dimension-wrap">
    <span class="ctrl-label" style="margin-left:0">Colunas</span>
    <select id="kanban-dimension">
      <option value="profile" selected>Profile</option>
      <option value="status">Status</option>
      <option value="deliver">Entrega</option>
      <option value="agent">Agente</option>
      <option value="next_window">Próxima janela</option>
    </select>
  </div>

  <span class="ctrl-label" id="density-label" style="margin-left:6px">Densidade</span>
  <select id="density-select">
    <option value="full" selected>Completa</option>
    <option value="compact">Compacta</option>
  </select>

  <!-- sort -->
  <span class="ctrl-label" style="margin-left:6px">Ordenar</span>
  <select id="sort-select">
    <option value="next_run">Próximo run</option>
    <option value="last_run">Último run</option>
    <option value="name">Nome</option>
    <option value="executions">Execuções</option>
    <option value="profile">Profile</option>
  </select>

  <div class="spacer"></div>
  <span id="job-count">—</span>
</div>

<main>
  <div id="loading">Carregando...</div>
  <div id="grid" style="display:none"></div>
  <div id="empty-state"><p>Nenhum job encontrado</p></div>
</main>
<div id="prompt-tooltip" class="prompt-tooltip">
  <div class="prompt-tooltip-title">Prompt completo</div>
  <pre id="prompt-tooltip-content"></pre>
</div>

<div id="modal-backdrop" class="modal-backdrop" onclick="closeAllModals()"></div>
<aside id="edit-modal" class="edit-modal">
  <div class="modal-header">
    <div>
      <div class="modal-title" id="modal-title">Editar job</div>
      <div class="modal-subtitle" id="modal-subtitle">—</div>
    </div>
    <button class="modal-btn" onclick="closeEditModal()">Fechar</button>
  </div>
  <div class="modal-body">
    <div class="modal-field">
      <label>Nome</label>
      <input id="edit-name" placeholder="nome-do-job">
      <div class="modal-help">Renomeia o job no painel e no scheduler. ID continua igual.</div>
    </div>
    <div class="modal-field">
      <label>Schedule</label>
      <input id="edit-schedule" placeholder="0 8 * * 1-5 ou every 2h">
    </div>
    <div class="modal-field">
      <label>Prompt</label>
      <textarea id="edit-prompt" spellcheck="false" placeholder="Texto que o agente recebe quando o cron roda. Em jobs no-agent pode ficar vazio."></textarea>
      <div class="modal-help">Aqui você altera a instrução do job. Se só quiser mudar horário, entrega, skills ou script, não mexa neste campo.</div>
    </div>

    <div class="modal-grid">
      <div class="modal-field">
        <label>Entrega</label>
        <input id="edit-deliver" placeholder="origin, local, telegram, discord:...">
      </div>
      <div class="modal-field">
        <label>Script</label>
        <input id="edit-script" placeholder="caminho relativo/absoluto do script">
      </div>
      <div class="modal-field">
        <label>Skills</label>
        <input id="edit-skills" type="hidden">
        <div id="skills-picker" class="multi-select">
          <button type="button" class="multi-select-button" onclick="toggleSkillsPicker(event)">
            <span id="skills-picker-label">Carregando skills...</span>
            <span class="multi-select-caret">▾</span>
          </button>
          <div class="skill-menu" onclick="event.stopPropagation()">
            <input id="skill-search" class="skill-search" placeholder="filtrar skills disponíveis..." oninput="renderSkillOptions()">
            <div id="skill-options" class="skill-options"></div>
            <div class="skill-picker-actions">
              <button type="button" class="skill-mini-btn" onclick="clearSelectedSkills()">Limpar</button>
              <button type="button" class="skill-mini-btn" onclick="closeSkillsPicker()">Fechar</button>
            </div>
          </div>
        </div>
        <div class="modal-help">Mostra as skills habilitadas no profile deste job. Multi-seleção sem vírgula manual, porque sofrer digitando nome de skill é atraso civilizatório.</div>
      </div>
      <div class="modal-field">
        <label>Toolsets</label>
        <input id="edit-toolsets" placeholder="web, terminal, file">
      </div>
      <div class="modal-field">
        <label>Modelo</label>
        <input id="edit-model-name" placeholder="anthropic/claude-sonnet-4">
      </div>
      <div class="modal-field">
        <label>Provider</label>
        <input id="edit-model-provider" placeholder="openrouter, anthropic, custom:nome">
      </div>
      <div class="modal-field">
        <label>Base URL</label>
        <input id="edit-base-url" placeholder="https://api.exemplo.com/v1">
      </div>
    </div>

    <div class="modal-field">
      <label>Contexto de outros jobs</label>
      <input id="edit-context-from" placeholder="job_id_1, job_id_2">
      <div class="modal-help">IDs separados por vírgula. Deixe vazio para remover.</div>
    </div>

    <label class="modal-check-row">
      <input id="edit-no-agent" type="checkbox">
      Rodar como no-agent
    </label>
    <div class="modal-help">No-agent exige script configurado. Se ativar sem script, o preview bloqueia. Sim, até o botão tem bom senso.</div>

    <div class="modal-field">
      <label>Diff / validação</label>
      <pre id="edit-diff" class="diff-box"></pre>
      <div class="modal-help">Clique em “Preview diff” para ver exatamente o que vai mudar antes de salvar.</div>
    </div>
  </div>
  <div class="modal-actions">
    <button class="modal-btn" onclick="previewEdit()">Preview diff</button>
    <button class="modal-btn primary" onclick="saveEdit()">Salvar alteração</button>
  </div>
</aside>

<aside id="rollback-modal" class="edit-modal">
  <div class="modal-header">
    <div>
      <div class="modal-title">Backups & rollback</div>
      <div class="modal-subtitle">Restaura o jobs.json inteiro de um profile</div>
    </div>
    <button class="modal-btn" onclick="closeRollbackModal()">Fechar</button>
  </div>
  <div class="modal-body">
    <div class="modal-grid">
      <div class="modal-field">
        <label>Profile</label>
        <select id="rollback-profile"></select>
      </div>
      <div class="modal-field">
        <label>&nbsp;</label>
        <button class="modal-btn" onclick="loadBackups()">Atualizar lista</button>
      </div>
    </div>
    <div class="modal-help">Rollback troca o arquivo inteiro do profile. Antes de restaurar, o estado atual também vira backup. Cinto e suspensório, versão JSON.</div>
    <div id="backup-list" class="backup-list"></div>
  </div>
</aside>
<div id="toast" class="toast"></div>
<div id="kanban-save-bar" class="kanban-save-bar">
  <span class="kanban-save-meta" id="kanban-save-meta">0 alterações pendentes</span>
  <button class="kanban-save-btn" onclick="saveKanbanEdits()">Salvar edição</button>
</div>

<script>
// ── STATE ─────────────────────────────────────────────────────────────────
let allJobs = [];
let availableProfiles = [];
let currentStatusFilter = 'active';
let selectedProfiles = new Set(['all']);
let currentSort = 'next_run';
let currentView = 'cards';
let kanbanDimension = 'profile';
let currentDensity = localStorage.getItem('cronobs-density') || 'full';
let listSorts = [{ key: 'next_run', dir: 'asc' }];
let collapsedJobs = new Set(JSON.parse(localStorage.getItem('cronobs-collapsed') || '[]'));
let isDark = true; // set properly on init
let refreshTimer;
let editingJob = null;
let lastFetchAt = null;
let draggedListColumn = null;
let draggedKanbanJobKey = null;
let pendingKanbanEdits = new Map();
let profileSkillsCache = new Map();
let selectedEditSkills = new Set();
let currentSkillOptions = [];
let currentSkillProfile = 'default';

// ── THEME ─────────────────────────────────────────────────────────────────
function isDayTime() {
  const h = new Date().getHours();
  return h >= 7 && h < 19; // 07h-19h = dia
}

function applyTheme(light) {
  isDark = !light;
  document.body.classList.toggle('light', light);
  document.getElementById('theme-icon').textContent = light ? '☾' : '☀';
  document.getElementById('theme-label').textContent = light ? 'escuro' : 'claro';
  localStorage.setItem('cronobs-theme', light ? 'light' : 'dark');
}

function toggleTheme() {
  applyTheme(isDark); // se dark, vai pra light; se light, vai pra dark
}

function initTheme() {
  const saved = localStorage.getItem('cronobs-theme');
  if (saved) {
    applyTheme(saved === 'light');
  } else {
    applyTheme(isDayTime());
  }
}

// ── FONT SCALE ───────────────────────────────────────────────────────────
const FONT_MIN = 0.6;
const FONT_MAX = 1.8;
const FONT_STEP = 0.1;
let currentFontScale = parseFloat(localStorage.getItem('cronobs-font-scale') || '1');

function applyFontScale(scale) {
  scale = Math.round(scale * 10) / 10; // 1 decimal
  scale = Math.max(FONT_MIN, Math.min(FONT_MAX, scale));
  currentFontScale = scale;
  document.documentElement.style.setProperty('--font-scale', scale);
  document.getElementById('font-label').textContent = Math.round(scale * 100) + '%';
  localStorage.setItem('cronobs-font-scale', scale);
}

function increaseFont() { applyFontScale(currentFontScale + FONT_STEP); }
function decreaseFont() { applyFontScale(currentFontScale - FONT_STEP); }

function initFontScale() {
  applyFontScale(currentFontScale);
}

// ── DELIVER LABELS ────────────────────────────────────────────────────────
const DELIVER_MAP = {
  'local':                          { platform: 'LOCAL',    channel: 'arquivo local' },
  'telegram:18996509':              { platform: 'TELEGRAM', channel: 'Pessoal' },
  'telegram:-1003506608201:25':     { platform: 'TELEGRAM', channel: 'Grupo' },
  'telegram':                       { platform: 'TELEGRAM', channel: 'Padrão' },
  'origin':                         { platform: 'ORIGIN',   channel: 'canal de origem' },
  'discord:1503603774273359942':    { platform: 'DISCORD',  channel: '#time-dados' },
  'discord:1503548619738054677':    { platform: 'DISCORD',  channel: '#hermes-daily' },
  'discord:1503558689498464377':    { platform: 'DISCORD',  channel: '#emails-pessoal' },
  'discord:1503558243010482296':    { platform: 'DISCORD',  channel: '#emails-trabalho' },
  'discord:1503757624384950414':    { platform: 'DISCORD',  channel: '#sessions' },
  'discord:1503549453171429426':    { platform: 'DISCORD',  channel: '#analista' },
  'discord:1504106577073012746':    { platform: 'DISCORD',  channel: '#engenheiro' },
};

function parseDeliver(deliver) {
  if (!deliver) return { platform: '—', channel: '—' };
  if (DELIVER_MAP[deliver]) return DELIVER_MAP[deliver];
  const [platform, ...rest] = deliver.split(':');
  const id = rest.join(':');
  const short = id ? `…${id.slice(-6)}` : '—';
  return { platform: platform.toUpperCase(), channel: short };
}

// ── CRON HUMAN LABEL ──────────────────────────────────────────────────────
function parseCron(expr) {
  if (!expr) return '—';
  const p = expr.trim().split(/\s+/);
  if (p.length < 5) return expr;
  const [min, hr, dom, month, dow] = p;
  const time = `${hr.padStart(2,'0')}:${min.padStart(2,'0')}`;
  const DOW = { '*':'Diário','1-5':'Seg–Sex','0-6':'Diário',
    '1,5':'Seg, Sex','1':'Seg','2':'Ter','3':'Qua','4':'Qui','5':'Sex','6':'Sáb','0':'Dom' };
  const dayLabel = DOW[dow] ?? dow;
  return `${dayLabel} · ${time}`;
}

// ── TIME UTILS ────────────────────────────────────────────────────────────
function countdown(isoStr) {
  if (!isoStr) return { text: '—', hm: '—', urgency: null, pct: 0 };
  const diff = new Date(isoStr) - new Date();
  if (diff < 0) return { text: 'PASSOU', hm: 'passou', urgency: 'urgent', pct: 100 };
  const totalSec = Math.floor(diff / 1000);
  const totalMin = Math.floor(totalSec / 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  const hm = h > 0 ? `${h}h ${m}min` : `${m}min`;
  const pct = Math.max(0, Math.min(100, Math.round((1 - diff / (24 * 60 * 60 * 1000)) * 100)));
  if (totalMin < 60) return { text: `${m}min`, hm, urgency: 'urgent', pct };
  if (h < 4)         return { text: `${h}h${m > 0 ? m+'m' : ''}`, hm, urgency: 'soon', pct };
  if (h < 24)        return { text: `${h}h`, hm, urgency: null, pct };
  const d = Math.floor(h / 24); const rh = h % 24;
  return { text: `${d}d${rh > 0 ? rh+'h' : ''}`, hm: `${d}d ${rh}h`, urgency: null, pct };
}

function relativeTime(isoStr) {
  if (!isoStr) return 'nunca rodou';
  const diff = new Date() - new Date(isoStr);
  const m = Math.floor(diff / 60000);
  if (m < 1)  return 'agora mesmo';
  if (m < 60) return `há ${m}min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `há ${h}h`;
  const d = Math.floor(h / 24);
  return `há ${d}d`;
}

function fmtDateTime(isoStr) {
  if (!isoStr) return '—';
  return new Date(isoStr).toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit',
    timeZone: 'America/Sao_Paulo'
  }).replace(',', ' ·');
}

function fmtClockTime(date) {
  return date.toLocaleTimeString('pt-BR', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    timeZone: 'America/Sao_Paulo'
  });
}

function updateLastUpdateLabel() {
  const el = document.getElementById('last-update');
  if (!el) return;
  el.textContent = lastFetchAt ? `atualizado: ${fmtClockTime(lastFetchAt)}` : 'atualizado: —';
}

// ── PROFILE COLORS (cycle) ─────────────────────────────────────────────────
const PROFILE_COLORS = {
  // Evita verde/azul/roxo para não confundir com Skills/Tools/LLM.
  default:    { bg: 'var(--bg-raised)', color: 'var(--text-primary)', border: 'var(--border-visible)' },
  analista:   { bg: 'rgba(255,107,107,0.12)', color: '#ff6b6b', border: 'rgba(255,107,107,0.28)' },
  engenheiro: { bg: 'rgba(245,158,11,0.12)', color: '#f59e0b', border: 'rgba(245,158,11,0.28)' },
  particular: { bg: 'rgba(249,115,22,0.12)', color: '#f97316', border: 'rgba(249,115,22,0.28)' },
};

function profileStyle(profile) {
  const c = PROFILE_COLORS[profile] || { bg: 'rgba(255,255,255,0.08)', color: '#d4d4d4', border: 'rgba(255,255,255,0.20)' };
  return `background:${c.bg};color:${c.color};border:1px solid ${c.border}`;
}

// ── NEXT JOB BANNER ───────────────────────────────────────────────────────
function updateNextBanner(jobs) {
  const active = jobs.filter(j => j.enabled && j.state === 'scheduled' && j.next_run_at);
  if (!active.length) return;
  active.sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at));
  const next = active[0];
  const cd = countdown(next.next_run_at);
  const deliver = parseDeliver(next.deliver);
  const profile = next._profile || 'default';
  const diff = new Date(next.next_run_at) - new Date();
  let text;
  if (diff < 0) {
    text = 'PASSOU';
  } else {
    const s = Math.floor(diff / 1000) % 60;
    const m = Math.floor(diff / 60000) % 60;
    const h = Math.floor(diff / 3600000);
    text = h > 0 ? `${h}h ${m}min ${s}s` : `${m}min ${s}s`;
  }
  document.getElementById('next-countdown').textContent = text;
  document.getElementById('next-name').textContent = next.name;
  document.getElementById('next-schedule').textContent = parseCron(next.schedule?.expr || next.schedule_display || '');
  document.getElementById('next-deliver').textContent = `${deliver.platform} · ${deliver.channel}`;
  const ptag = document.getElementById('next-profile-tag');
  ptag.textContent = profile;
  ptag.setAttribute('style', profileStyle(profile));
  ptag.style.display = '';
  const bar = document.getElementById('next-timer-bar');
  if (bar) {
    bar.style.height = cd.pct + '%';
    const t = cd.pct / 100;
    const r = Math.round(47 + 149 * t);
    const g = Math.round(196 - 169 * t);
    const b = Math.round(102 - 76 * t);
    bar.style.background = `rgb(${r},${g},${b})`;
  }
}

function tickBanner() {
  const active = allJobs.filter(j => j.enabled && j.state === 'scheduled' && j.next_run_at);
  if (!active.length) return;
  active.sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at));
  const cd = countdown(active[0].next_run_at);
  const diff = new Date(active[0].next_run_at) - new Date();
  let text;
  if (diff < 0) {
    text = 'PASSOU';
  } else {
    const s = Math.floor(diff / 1000) % 60;
    const m = Math.floor(diff / 60000) % 60;
    const h = Math.floor(diff / 3600000);
    text = h > 0 ? `${h}h ${m}min ${s}s` : `${m}min ${s}s`;
  }
  document.getElementById('next-countdown').textContent = text;
  const bar = document.getElementById('next-timer-bar');
  if (bar) {
    bar.style.height = cd.pct + '%';
    const t = cd.pct / 100;
    const r = Math.round(47 + 149 * t);
    const g = Math.round(196 - 169 * t);
    const b = Math.round(102 - 76 * t);
    bar.style.background = `rgb(${r},${g},${b})`;
  }
}

// ── PROFILE FILTER BUTTONS ────────────────────────────────────────────────
function buildProfileFilters(jobs) {
  const profiles = [...new Set(jobs.map(j => j._profile || 'default'))].sort();
  const container = document.getElementById('filter-profile');
  const previously = new Set([...selectedProfiles].filter(p => p === 'all' || profiles.includes(p)));
  selectedProfiles = previously.size ? previously : new Set(['all']);

  container.innerHTML = '<button class="filter-btn" data-profile="all">Todos</button>';
  profiles.forEach(p => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.dataset.profile = p;
    btn.textContent = p;
    container.appendChild(btn);
  });
  syncProfileButtons();

  container.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      const profile = btn.dataset.profile;
      if (profile === 'all' || !ev.shiftKey) {
        selectedProfiles = new Set([profile]);
      } else {
        selectedProfiles.delete('all');
        if (selectedProfiles.has(profile)) selectedProfiles.delete(profile);
        else selectedProfiles.add(profile);
        if (!selectedProfiles.size) selectedProfiles.add('all');
      }
      syncProfileButtons();
      render();
    });
  });
}

function syncProfileButtons() {
  document.querySelectorAll('#filter-profile .filter-btn').forEach(btn => {
    btn.classList.toggle('active', selectedProfiles.has(btn.dataset.profile));
  });
}

// ── CARD ─────────────────────────────────────────────────────────────────
function buildCard(job) {
  const isActive = job.state === 'scheduled' && job.enabled;
  const deliver = parseDeliver(job.deliver);
  const schedule = parseCron(job.schedule?.expr || job.schedule_display || '');
  const cd = countdown(job.next_run_at);
  const executions = job.repeat?.completed ?? 0;
  const noAgent = job.no_agent === true;
  const skills = job.skills || [];
  const toolsets = job.enabled_toolsets || [];
  const hasScript = !!job.script;
  const hasModel = !!(job.model && job.model !== 'null');
  const hasChain = !!(job.context_from?.length > 0);
  const profile = job._profile || 'default';
  const jobKey = job.id || `${profile}:${job.name}`;
  const collapsed = collapsedJobs.has(jobKey);

  const lastStatusEl = job.last_status === 'ok'
    ? '<span class="status-ok">✓ OK</span>'
    : job.last_status === 'error'
    ? '<span class="status-error">✗ ERRO</span>'
    : '<span class="status-none">—</span>';

  const skillsHtml = skills.length > 0
    ? `<div class="card-section">
        <div class="section-title">Skills</div>
        <div class="tag-group">
          ${skills.map(s => `<span class="tag tag-skill">${s}</span>`).join('')}
        </div>
       </div>`
    : '';

  const llmParts = [];
  if (hasModel) llmParts.push(`<span class="tag tag-llm">modelo: ${job.model}</span>`);
  if (job.provider) llmParts.push(`<span class="tag tag-llm">${job.provider}</span>`);
  if (noAgent) llmParts.push(`<span class="tag tag-no-agent">no-agent</span>`);
  const llmHtml = llmParts.length > 0
    ? `<div class="card-section">
        <div class="section-title">LLM</div>
        <div class="tag-group">${llmParts.join('')}</div>
       </div>`
    : '';

  const toolParts = [];
  toolsets.forEach(t => toolParts.push(`<span class="tag tag-toolset">${t}</span>`));
  if (hasScript) toolParts.push(`<span class="tag tag-script">script</span>`);
  if (hasChain) toolParts.push(`<span class="tag tag-chain">encadeado</span>`);
  const toolsHtml = toolParts.length > 0
    ? `<div class="card-section">
        <div class="section-title">Tools & Runtime</div>
        <div class="tag-group">${toolParts.join('')}</div>
       </div>`
    : '';

  return `
    <div class="card${currentDensity === 'compact' ? ' compact' : ''}${!isActive ? ' paused' : ''}${collapsed ? ' collapsed' : ''}"
         draggable="${currentView === 'kanban' ? 'true' : 'false'}"
         ondragstart="handleKanbanDragStart(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')"
         ondragend="handleKanbanDragEnd(event)"
         data-job-key="${escapeHtml(jobKey)}"
         data-job-id="${escapeHtml(job.id)}"
         data-state="${job.state}"
         data-profile="${profile}"
         data-next="${job.next_run_at || ''}"
         data-last="${job.last_run_at || ''}"
         data-exec="${executions}"
         data-name="${escapeHtml(job.name)}">
      <div class="card-header">
        <div class="card-title-row">
          <div class="card-name">${escapeHtml(job.name)}${promptIcon(job)}</div>
          <div class="card-profile-tag" style="${profileStyle(profile)}">${profile}</div>
        </div>
        <div class="card-header-actions">
          <div class="card-top-metrics">
            <div class="status-badge ${isActive ? 'active' : 'paused'}">${isActive ? 'ATIVO' : 'PAUSADO'}</div>
            <div class="executions-top">
              <div class="executions-top-number">${executions}</div>
              <div class="executions-top-label">exec.</div>
            </div>
          </div>
          <button class="collapse-btn" title="Fechar/abrir card" onclick="toggleCardCollapse(event, '${escapeJs(jobKey)}')">▾</button>
        </div>
      </div>

      <div class="card-collapsible">
        <div class="card-divider"></div>

        <div class="card-body">
          <div class="info-row">
            <span class="info-label">Schedule</span>
            <span class="info-value highlight">${schedule}</span>
          </div>
          <div class="info-row">
            <span class="info-label">Próximo</span>
            <span class="info-value ${cd.urgency || ''}" data-countdown="${job.next_run_at || ''}">${cd.text}</span>
            <span class="info-value" style="font-size: calc(11px * var(--font-scale));color:var(--text-disabled);margin-left:4px">${job.next_run_at ? fmtDateTime(job.next_run_at) : ''}</span>
          </div>
          <div class="info-row compact-hide">
            <span class="info-label">Último</span>
            <span class="info-value">${relativeTime(job.last_run_at)} · ${lastStatusEl}</span>
          </div>
        </div>

        <div class="card-section compact-hide">
          <div class="card-stats-row">
            <div class="deliver-block" style="align-items:flex-start">
              <div class="deliver-platform">${deliver.platform}</div>
              <div class="deliver-channel">${deliver.channel}</div>
            </div>
          </div>
        </div>

        ${skillsHtml}
        ${llmHtml}
        ${toolsHtml}
      </div>
      <div class="card-footer">
        <div class="job-actions">
          <button class="job-action-btn edit" onclick="openEditModal(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Editar</button>
          <button class="job-action-btn ${isActive ? 'danger' : ''}" onclick="toggleJobStatus(event, '${escapeJs(profile)}', '${escapeJs(job.id)}', '${isActive ? 'pause' : 'resume'}')">${isActive ? 'Pausar' : 'Ativar'}</button>
          <button class="job-action-btn run" onclick="runJobNow(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Rodar</button>
          <button class="job-action-btn struct" onclick="duplicateJob(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Duplicar</button>
          <button class="job-action-btn struct" onclick="moveJob(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Mover</button>
          <button class="job-action-btn delete" onclick="deleteJob(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Excluir</button>
        </div>
      </div>
    </div>
  `;
}

function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function escapeJs(v) {
  return String(v ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}
function encodePrompt(v) {
  return btoa(unescape(encodeURIComponent(String(v || ''))));
}
function decodePrompt(v) {
  try { return decodeURIComponent(escape(atob(v || ''))); }
  catch(e) { return 'Prompt indisponível ou inválido.'; }
}
function showPromptTooltip(event, encodedPrompt) {
  const tip = document.getElementById('prompt-tooltip');
  const content = document.getElementById('prompt-tooltip-content');
  content.textContent = decodePrompt(encodedPrompt);
  tip.style.display = 'block';
  movePromptTooltip(event);
}
function movePromptTooltip(event) {
  const tip = document.getElementById('prompt-tooltip');
  if (tip.style.display !== 'block') return;
  const pad = 18;
  let left = event.clientX + pad;
  let top = event.clientY + pad;
  const rect = tip.getBoundingClientRect();
  if (left + rect.width > window.innerWidth - 12) left = window.innerWidth - rect.width - 12;
  if (top + rect.height > window.innerHeight - 12) top = window.innerHeight - rect.height - 12;
  tip.style.left = Math.max(12, left) + 'px';
  tip.style.top = Math.max(12, top) + 'px';
}
function hidePromptTooltip() {
  document.getElementById('prompt-tooltip').style.display = 'none';
}
function promptIcon(job) {
  const encoded = encodePrompt(job.prompt || '');
  return `<span class="prompt-icon" title="Ver prompt" onmouseenter="showPromptTooltip(event, '${encoded}')" onmousemove="movePromptTooltip(event)" onmouseleave="hidePromptTooltip()">i</span>`;
}
function toggleCardCollapse(event, jobKey) {
  event.stopPropagation();
  if (collapsedJobs.has(jobKey)) collapsedJobs.delete(jobKey);
  else collapsedJobs.add(jobKey);
  localStorage.setItem('cronobs-collapsed', JSON.stringify([...collapsedJobs]));
  render();
}

// ── FILTER + SORT ─────────────────────────────────────────────────────────
function jobValue(job, key) {
  const deliver = parseDeliver(job.deliver);
  const isActive = job.state === 'scheduled' && job.enabled;
  if (key === 'name') return job.name || '';
  if (key === 'profile') return job._profile || 'default';
  if (key === 'status') return isActive ? 'ativo' : 'pausado';
  if (key === 'next_run') return job.next_run_at ? new Date(job.next_run_at).getTime() : 9e15;
  if (key === 'last_run') return job.last_run_at ? new Date(job.last_run_at).getTime() : 0;
  if (key === 'executions') return job.repeat?.completed || 0;
  if (key === 'deliver') return `${deliver.platform} ${deliver.channel}`;
  if (key === 'agent') return job.no_agent ? 'no-agent' : 'agent';
  return job[key] ?? '';
}

function compareJobs(a, b, sorts) {
  for (const sort of sorts) {
    const av = jobValue(a, sort.key);
    const bv = jobValue(b, sort.key);
    let cmp = 0;
    if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
    else cmp = String(av).localeCompare(String(bv));
    if (cmp !== 0) return sort.dir === 'desc' ? -cmp : cmp;
  }
  return 0;
}

function getVisible() {
  let jobs = [...allJobs];
  if (currentStatusFilter === 'active')  jobs = jobs.filter(j => j.state === 'scheduled' && j.enabled);
  if (currentStatusFilter === 'paused')  jobs = jobs.filter(j => !j.enabled || j.state !== 'scheduled');
  if (!selectedProfiles.has('all')) jobs = jobs.filter(j => selectedProfiles.has(j._profile || 'default'));

  const sortDir = currentSort === 'last_run' || currentSort === 'executions' ? 'desc' : 'asc';
  jobs.sort((a, b) => compareJobs(a, b, [{ key: currentSort, dir: sortDir }]));
  return jobs;
}

const LIST_COLUMNS_DEFAULT = [
  ['name','Job'], ['profile','Profile'], ['status','Status'], ['executions','Exec.'],
  ['next_run','Próximo'], ['last_run','Último'], ['deliver','Entrega'], ['agent','Agente'], ['_actions','Ações']
];
let listColumnOrder = JSON.parse(localStorage.getItem('cronobs-list-columns') || 'null') || LIST_COLUMNS_DEFAULT.map(c => c[0]);

function listColumns() {
  const byKey = new Map(LIST_COLUMNS_DEFAULT.map(c => [c[0], c]));
  const valid = listColumnOrder.filter(k => byKey.has(k));
  const missing = LIST_COLUMNS_DEFAULT.map(c => c[0]).filter(k => !valid.includes(k));
  listColumnOrder = [...valid, ...missing];
  return listColumnOrder.map(k => byKey.get(k));
}

function saveListColumnOrder() {
  localStorage.setItem('cronobs-list-columns', JSON.stringify(listColumnOrder));
}

function handleColumnDragStart(ev, key) {
  draggedListColumn = key;
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', key);
  ev.currentTarget.classList.add('dragging');
}

function handleColumnDragOver(ev) {
  ev.preventDefault();
  ev.currentTarget.classList.add('drag-over');
}

function handleColumnDragLeave(ev) {
  ev.currentTarget.classList.remove('drag-over');
}

function handleColumnDrop(ev, targetKey) {
  ev.preventDefault();
  document.querySelectorAll('.jobs-table th').forEach(th => th.classList.remove('dragging','drag-over'));
  const sourceKey = draggedListColumn || ev.dataTransfer.getData('text/plain');
  draggedListColumn = null;
  if (!sourceKey || sourceKey === targetKey) return;
  const order = listColumnOrder.filter(k => k !== sourceKey);
  const idx = order.indexOf(targetKey);
  order.splice(idx < 0 ? order.length : idx, 0, sourceKey);
  listColumnOrder = order;
  saveListColumnOrder();
  render();
  showToast('Ordem das colunas salva');
}

function handleColumnDragEnd() {
  draggedListColumn = null;
  document.querySelectorAll('.jobs-table th').forEach(th => th.classList.remove('dragging','drag-over'));
}

function jobKeyOf(profile, jobId) {
  return `${profile || 'default'}::${jobId}`;
}

function kanbanGroup(job) {
  return kanbanGroupForDimension(job, kanbanDimension).label;
}

function kanbanGroupForDimension(job, dimension) {
  const deliver = parseDeliver(job.deliver);
  const isActive = job.state === 'scheduled' && job.enabled;
  if (dimension === 'status') return { label: isActive ? 'Ativos' : 'Pausados', value: isActive ? 'active' : 'paused' };
  if (dimension === 'profile') return { label: job._profile || 'default', value: job._profile || 'default' };
  if (dimension === 'deliver') return { label: `${deliver.platform} · ${deliver.channel}`, value: job.deliver || '' };
  if (dimension === 'agent') return { label: job.no_agent ? 'No-agent' : 'Com LLM', value: job.no_agent ? 'no-agent' : 'llm' };
  if (dimension === 'next_window') {
    if (!job.next_run_at) return { label: 'Sem próximo run', value: 'none' };
    const min = Math.floor((new Date(job.next_run_at) - new Date()) / 60000);
    if (min < 0) return { label: 'Passou', value: 'passed' };
    if (min < 60) return { label: '< 1h', value: 'lt1h' };
    if (min < 240) return { label: '1–4h', value: '1-4h' };
    if (min < 1440) return { label: 'Hoje', value: 'today' };
    if (min < 10080) return { label: 'Esta semana', value: 'week' };
    return { label: 'Depois', value: 'later' };
  }
  return { label: 'Outros', value: 'other' };
}

function kanbanTargets(jobs) {
  if (kanbanDimension === 'status') return [
    { label: 'Ativos', value: 'active' },
    { label: 'Pausados', value: 'paused' }
  ];
  if (kanbanDimension === 'profile') {
    const profiles = availableProfiles.length ? availableProfiles : [...new Set(allJobs.map(j => j._profile || 'default'))];
    return profiles.map(p => ({ label: p, value: p }));
  }
  if (kanbanDimension === 'agent') return [
    { label: 'Com LLM', value: 'llm' },
    { label: 'No-agent', value: 'no-agent' }
  ];
  if (kanbanDimension === 'next_window') return [
    { label: '< 1h', value: 'lt1h' },
    { label: '1–4h', value: '1-4h' },
    { label: 'Hoje', value: 'today' },
    { label: 'Esta semana', value: 'week' },
    { label: 'Depois', value: 'later' },
    { label: 'Sem próximo run', value: 'none' }
  ];
  if (kanbanDimension === 'deliver') {
    const map = new Map();
    allJobs.forEach(job => {
      const g = kanbanGroupForDimension(job, 'deliver');
      map.set(g.value, g.label);
    });
    return [...map.entries()].map(([value,label]) => ({ label, value }));
  }
  const seen = new Map();
  jobs.forEach(job => {
    const g = kanbanGroupForDimension(job, kanbanDimension);
    seen.set(g.value, g.label);
  });
  return [...seen.entries()].map(([value,label]) => ({ label, value }));
}

function handleKanbanDragStart(ev, profile, jobId) {
  if (currentView !== 'kanban') return;
  draggedKanbanJobKey = jobKeyOf(profile, jobId);
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', draggedKanbanJobKey);
  ev.currentTarget.classList.add('dragging');
}

function handleKanbanDragEnd(ev) {
  draggedKanbanJobKey = null;
  ev.currentTarget?.classList?.remove('dragging');
  document.querySelectorAll('.kanban-cards').forEach(el => el.classList.remove('drag-over'));
}

function handleKanbanDragOver(ev) {
  ev.preventDefault();
  ev.currentTarget.classList.add('drag-over');
}

function handleKanbanDragLeave(ev) {
  ev.currentTarget.classList.remove('drag-over');
}

async function handleKanbanDrop(ev, targetValue, targetLabel) {
  ev.preventDefault();
  ev.currentTarget.classList.remove('drag-over');
  const key = draggedKanbanJobKey || ev.dataTransfer.getData('text/plain');
  if (!key) return;
  const [profile, jobId] = key.split('::');
  const job = allJobs.find(j => (j._profile || 'default') === profile && j.id === jobId);
  if (!job) return showToast('Job não encontrado');
  const current = kanbanGroupForDimension(job, kanbanDimension);
  if (String(current.value) === String(targetValue)) return;
  const edit = pendingEditForDrop(job, targetValue, targetLabel);
  if (!edit) return;

  // Move entre profiles: salva imediatamente (sem pending edit).
  // Outros tipos (status, deliver, agent, schedule) continuam no fluxo de batch.
  if (edit.type === 'move') {
    // Aplica localmente primeiro (UX responsiva)
    const prevProfile = job._profile;
    job._profile = edit.target_profile;
    render();
    try {
      await apiPost('/api/job/move', { profile: edit.profile, id: edit.id, target_profile: edit.target_profile });
      showToast(`Movido para ${edit.target_profile}`);
      await fetchJobs();
    } catch (e) {
      // Rollback local
      job._profile = prevProfile;
      render();
      showToast(e.message);
      await fetchJobs();
    }
    draggedKanbanJobKey = null;
    return;
  }

  pendingKanbanEdits.set(key, edit);
  applyPendingEditLocally(job, edit);
  updateKanbanSaveBar();
  render();
}

function pendingEditForDrop(job, targetValue, targetLabel) {
  const profile = job._profile || 'default';
  if (kanbanDimension === 'profile') return { type: 'move', profile, id: job.id, target_profile: targetValue };
  if (kanbanDimension === 'status') return { type: 'status', profile, id: job.id, action: targetValue === 'active' ? 'resume' : 'pause' };
  if (kanbanDimension === 'deliver') return { type: 'update', profile, id: job.id, deliver: targetValue };
  if (kanbanDimension === 'agent') {
    if (targetValue === 'no-agent' && !job.script) {
      showToast('No-agent exige script. Edite o script antes. Botão te salvando de você mesmo.');
      return null;
    }
    return { type: 'update', profile, id: job.id, no_agent: targetValue === 'no-agent' };
  }
  if (kanbanDimension === 'next_window') {
    const scheduleMap = {
      lt1h: '30m',
      '1-4h': 'every 2h',
      today: 'every 6h',
      week: '0 9 * * 1',
      later: '0 9 1 * *',
      none: null
    };
    if (targetValue === 'none') return { type: 'status', profile, id: job.id, action: 'pause' };
    return { type: 'update', profile, id: job.id, schedule_text: scheduleMap[targetValue] || scheduleText(job) };
  }
  return null;
}

function applyPendingEditLocally(job, edit) {
  if (edit.type === 'move') job._profile = edit.target_profile;
  if (edit.type === 'status') {
    if (edit.action === 'pause') { job.enabled = false; job.state = 'paused'; }
    else { job.enabled = true; job.state = 'scheduled'; }
  }
  if (edit.type === 'update') {
    if ('deliver' in edit) job.deliver = edit.deliver;
    if ('no_agent' in edit) job.no_agent = edit.no_agent;
    if ('schedule_text' in edit) {
      job.schedule_display = edit.schedule_text;
      job.schedule = { kind: edit.schedule_text.includes(' ') ? 'cron' : 'interval', expr: edit.schedule_text };
      job.next_run_at = null;
    }
  }
}

function applyPendingEditsToAllJobs() {
  pendingKanbanEdits.forEach((edit, key) => {
    const [profile, jobId] = key.split('::');
    const job = allJobs.find(j => (j._profile || 'default') === profile && j.id === jobId);
    if (job) applyPendingEditLocally(job, edit);
  });
}

function updateKanbanSaveBar() {
  const bar = document.getElementById('kanban-save-bar');
  const meta = document.getElementById('kanban-save-meta');
  if (!bar || !meta) return;
  const count = pendingKanbanEdits.size;
  meta.textContent = `${count} alteração${count === 1 ? '' : 'es'} pendente${count === 1 ? '' : 's'}`;
  bar.classList.toggle('open', count > 0);
}

async function saveKanbanEdits() {
  if (!pendingKanbanEdits.size) return;
  if (!confirm(`Salvar ${pendingKanbanEdits.size} alteração(ões) feitas no kanban? Backups serão criados.`)) return;
  const edits = [...pendingKanbanEdits.values()];
  try {
    for (const edit of edits) {
      if (edit.type === 'move') await apiPost('/api/job/move', { profile: edit.profile, id: edit.id, target_profile: edit.target_profile });
      if (edit.type === 'status') await apiPost('/api/job/status', { profile: edit.profile, id: edit.id, action: edit.action });
      if (edit.type === 'update') await apiPost('/api/job/update', edit);
    }
    pendingKanbanEdits.clear();
    updateKanbanSaveBar();
    showToast('Kanban salvo');
    await fetchJobs();
  } catch(e) {
    showToast(e.message);
    await fetchJobs();
  }
}

function renderKanban(jobs, grid) {
  const groups = new Map();
  const targets = kanbanTargets(jobs);
  targets.forEach(t => groups.set(t.value, { label: t.label, value: t.value, items: [] }));
  jobs.forEach(job => {
    const g = kanbanGroupForDimension(job, kanbanDimension);
    if (!groups.has(g.value)) groups.set(g.value, { label: g.label, value: g.value, items: [] });
    groups.get(g.value).items.push(job);
  });
  grid.className = 'view-kanban';
  grid.innerHTML = [...groups.values()].map(group => `
    <section class="kanban-column">
      <div class="kanban-header">
        <span class="kanban-title">${escapeHtml(group.label)}</span>
        <span class="kanban-count">${group.items.length}</span>
      </div>
      <div class="kanban-cards"
           data-kanban-value="${escapeHtml(group.value)}"
           ondragover="handleKanbanDragOver(event)"
           ondragleave="handleKanbanDragLeave(event)"
           ondrop="handleKanbanDrop(event, '${escapeJs(group.value)}', '${escapeJs(group.label)}')">
        ${group.items.map(buildCard).join('')}
      </div>
    </section>
  `).join('');
  updateKanbanSaveBar();
}

function listSortMark(key) {
  const idx = listSorts.findIndex(s => s.key === key);
  if (idx < 0) return '';
  return `<span class="sort-mark">${listSorts[idx].dir === 'asc' ? '↑' : '↓'}${idx > 0 ? idx + 1 : ''}</span>`;
}

function setListSort(key, ev) {
  const existing = listSorts.find(s => s.key === key);
  const nextDir = existing?.dir === 'asc' ? 'desc' : 'asc';
  if (ev.shiftKey) {
    listSorts = listSorts.filter(s => s.key !== key);
    listSorts.push({ key, dir: nextDir });
    listSorts = listSorts.slice(-2);
  } else {
    listSorts = [{ key, dir: nextDir }];
  }
  render();
}

function renderListCell(job, key) {
  const isActive = job.state === 'scheduled' && job.enabled;
  const deliver = parseDeliver(job.deliver);
  const profile = job._profile || 'default';
  if (key === 'name') return `<td><span class="table-name">${escapeHtml(job.name)}</span>${promptIcon(job)}</td>`;
  if (key === 'profile') return `<td><span class="card-profile-tag" style="${profileStyle(profile)}">${profile}</span></td>`;
  if (key === 'status') return `<td><span class="status-badge ${isActive ? 'active' : 'paused'}">${isActive ? 'ATIVO' : 'PAUSADO'}</span></td>`;
  if (key === 'executions') return `<td class="table-muted">${job.repeat?.completed || 0}</td>`;
  if (key === 'next_run') return `<td>${job.next_run_at ? `${countdown(job.next_run_at).text} <span class="table-muted">${fmtDateTime(job.next_run_at)}</span>` : '<span class="table-muted">—</span>'}</td>`;
  if (key === 'last_run') return `<td>${relativeTime(job.last_run_at)}</td>`;
  if (key === 'deliver') return `<td>${deliver.platform} · ${deliver.channel}</td>`;
  if (key === 'agent') return `<td><span class="tag ${job.no_agent ? 'tag-no-agent' : 'tag-llm'}">${job.no_agent ? 'no-agent' : 'LLM'}</span></td>`;
  if (key === '_actions') return `<td>
    <div class="job-actions">
      <button class="job-action-btn edit" onclick="openEditModal(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Editar</button>
      <button class="job-action-btn ${isActive ? 'danger' : ''}" onclick="toggleJobStatus(event, '${escapeJs(profile)}', '${escapeJs(job.id)}', '${isActive ? 'pause' : 'resume'}')">${isActive ? 'Pausar' : 'Ativar'}</button>
      <button class="job-action-btn run" onclick="runJobNow(event, '${escapeJs(profile)}', '${escapeJs(job.id)}')">Rodar</button>
    </div>
  </td>`;
  return '<td>—</td>';
}

function renderList(jobs, grid) {
  const sorted = [...jobs].sort((a, b) => compareJobs(a, b, listSorts));
  const cols = listColumns();
  grid.className = 'view-list';
  grid.innerHTML = `
    <div class="jobs-table-wrap">
      <table class="jobs-table">
        <thead><tr>
          ${cols.map(([key,label]) => {
            const sortClick = key === '_actions' ? '' : `onclick="setListSort('${key}', event)"`;
            return `<th draggable="true" data-col="${key}" ${sortClick}
              ondragstart="handleColumnDragStart(event, '${key}')"
              ondragover="handleColumnDragOver(event)"
              ondragleave="handleColumnDragLeave(event)"
              ondrop="handleColumnDrop(event, '${key}')"
              ondragend="handleColumnDragEnd()">${label}${key === '_actions' ? '' : listSortMark(key)}</th>`;
          }).join('')}
        </tr></thead>
        <tbody>
          ${sorted.map(job => `<tr>${cols.map(([key]) => renderListCell(job, key)).join('')}</tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

function render() {
  const jobs = getVisible();
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty-state');
  document.getElementById('job-count').textContent = `${jobs.length} job${jobs.length !== 1 ? 's' : ''}`;
  document.getElementById('kanban-dimension-wrap').style.display = currentView === 'kanban' ? 'flex' : 'none';
  document.getElementById('sort-select').style.display = currentView === 'cards' ? 'block' : 'none';
  const showDensity = currentView === 'cards' || currentView === 'kanban';
  document.getElementById('density-select').style.display = showDensity ? 'block' : 'none';
  document.getElementById('density-label').style.display = showDensity ? 'inline' : 'none';

  if (!jobs.length) {
    grid.style.display = 'none';
    empty.style.display = 'block';
    return;
  }

  grid.style.display = currentView === 'cards' ? 'grid' : (currentView === 'kanban' ? 'flex' : 'block');
  empty.style.display = 'none';

  if (currentView === 'cards') {
    grid.className = '';
    grid.innerHTML = jobs.map(buildCard).join('');
  } else if (currentView === 'kanban') {
    renderKanban(jobs, grid);
  } else {
    renderList(jobs, grid);
  }
}

function updateStats() {
  const total = allJobs.length;
  const active = allJobs.filter(j => j.state === 'scheduled' && j.enabled).length;
  const exec = allJobs.reduce((s, j) => s + (j.repeat?.completed||0), 0);
  const profiles = new Set(allJobs.map(j => j._profile||'default')).size;
  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-active').textContent = active;
  document.getElementById('stat-paused').textContent = total - active;
  document.getElementById('stat-executions').textContent = exec;
  document.getElementById('stat-profiles').textContent = profiles;
}

function updateCountdowns() {
  document.querySelectorAll('[data-countdown]').forEach(el => {
    const iso = el.getAttribute('data-countdown');
    if (!iso) return;
    const cd = countdown(iso);
    el.textContent = cd.text;
    el.className = `info-value ${cd.urgency||''}`;
  });
}

// ── FETCH ─────────────────────────────────────────────────────────────────
async function fetchProfiles() {
  try {
    const res = await fetch('/api/profiles');
    const data = await res.json();
    availableProfiles = (data.profiles || []).map(p => p.name);
  } catch (e) {
    availableProfiles = [...new Set(allJobs.map(j => j._profile || 'default'))];
  }
}

function profileChoices(excludeProfile) {
  const profiles = availableProfiles.length ? availableProfiles : [...new Set(allJobs.map(j => j._profile || 'default'))];
  return profiles.filter(p => p !== excludeProfile);
}

async function fetchJobs() {
  const bar = document.getElementById('progress-bar');
  bar.style.width = '40%';
  try {
    const res = await fetch('/api/jobs');
    const data = await res.json();
    allJobs = data.jobs || [];
    applyPendingEditsToAllJobs();
    lastFetchAt = new Date();
    updateLastUpdateLabel();
    await fetchProfiles();
    bar.style.width = '100%';
    setTimeout(() => bar.style.width = '0%', 400);
    updateStats();
    buildProfileFilters(allJobs);
    updateNextBanner(allJobs);
    render();
    document.getElementById('loading').style.display = 'none';
    document.getElementById('grid').style.display = currentView === 'cards' ? 'grid' : (currentView === 'kanban' ? 'flex' : 'block');
    document.getElementById('refresh-label').textContent = 'ao vivo · 30s';
  } catch(e) {
    bar.style.width = '0%';
    document.getElementById('refresh-label').textContent = 'erro conexão';
    console.error(e);
  }
}


// ── ADMIN API / EDITOR ────────────────────────────────────────────────────
function showToast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('open');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('open'), 3200);
}

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {})
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `Erro HTTP ${res.status}`);
  return data;
}

async function fetchSkillsForProfile(profile) {
  profile = profile || 'default';
  currentSkillProfile = profile;
  if (profileSkillsCache.has(profile)) return profileSkillsCache.get(profile);
  const res = await fetch(`/api/skills?profile=${encodeURIComponent(profile)}`);
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || `Erro HTTP ${res.status}`);
  profileSkillsCache.set(profile, data.skills || []);
  return data.skills || [];
}

function selectedSkillsLabel() {
  const skills = [...selectedEditSkills];
  if (!skills.length) return 'Nenhuma skill selecionada';
  if (skills.length <= 3) return skills.join(', ');
  return `${skills.length} skills selecionadas · ${skills.slice(0, 3).join(', ')}...`;
}

function syncSelectedSkills() {
  const hidden = document.getElementById('edit-skills');
  if (hidden) hidden.value = [...selectedEditSkills].join(', ');
  const label = document.getElementById('skills-picker-label');
  if (label) label.textContent = selectedSkillsLabel();
}

function renderSkillOptions() {
  const box = document.getElementById('skill-options');
  if (!box) return;
  const q = (document.getElementById('skill-search')?.value || '').trim().toLowerCase();
  const known = new Set(currentSkillOptions.map(s => s.name));
  const missingSelected = [...selectedEditSkills]
    .filter(name => !known.has(name))
    .map(name => ({ name, category: 'fora da lista', source: 'job' }));
  const options = [...missingSelected, ...currentSkillOptions]
    .filter(skill => !q || `${skill.name} ${skill.category || ''}`.toLowerCase().includes(q));
  if (!options.length) {
    box.innerHTML = '<div class="modal-help" style="padding:8px">Nenhuma skill encontrada para este filtro.</div>';
    return;
  }
  box.innerHTML = options.map(skill => {
    const checked = selectedEditSkills.has(skill.name) ? 'checked' : '';
    return `<label class="skill-option">
      <input type="checkbox" ${checked} onchange="toggleSkillSelection('${escapeJs(skill.name)}', this.checked)">
      <span class="skill-option-name">${escapeHtml(skill.name)}</span>
      <span class="skill-option-category">${escapeHtml(skill.category || skill.source || '')}</span>
    </label>`;
  }).join('');
}

function toggleSkillSelection(name, checked) {
  if (checked) selectedEditSkills.add(name);
  else selectedEditSkills.delete(name);
  syncSelectedSkills();
  renderSkillOptions();
}

function clearSelectedSkills() {
  selectedEditSkills.clear();
  syncSelectedSkills();
  renderSkillOptions();
}

function toggleSkillsPicker(event) {
  event?.stopPropagation();
  document.getElementById('skills-picker')?.classList.toggle('open');
}

function closeSkillsPicker() {
  document.getElementById('skills-picker')?.classList.remove('open');
}

async function loadSkillPicker(profile, selectedSkills) {
  selectedEditSkills = new Set(selectedSkills || []);
  currentSkillOptions = [];
  document.getElementById('skill-search').value = '';
  document.getElementById('skills-picker-label').textContent = 'Carregando skills...';
  syncSelectedSkills();
  renderSkillOptions();
  try {
    currentSkillOptions = await fetchSkillsForProfile(profile);
    renderSkillOptions();
    syncSelectedSkills();
  } catch (e) {
    document.getElementById('skills-picker-label').textContent = 'Erro ao carregar skills';
    document.getElementById('skill-options').innerHTML = `<div class="modal-help" style="padding:8px">${escapeHtml(e.message)}</div>`;
  }
}

function scheduleText(job) {
  if (job.schedule?.kind === 'interval') return job.schedule.display || `every ${job.schedule.minutes}m`;
  if (job.schedule?.kind === 'cron') return job.schedule.expr || job.schedule.display || job.schedule_display || '';
  return job.schedule_display || '';
}

function joinList(value) {
  return Array.isArray(value) ? value.join(', ') : '';
}

function setInputValue(id, value) {
  document.getElementById(id).value = value ?? '';
}

function modelNameValue(job) {
  if (typeof job.model === 'string') return job.model;
  return job.model?.model || '';
}

function modelProviderValue(job) {
  if (typeof job.model === 'object' && job.model) return job.model.provider || '';
  return '';
}

function openEditModal(event, profile, jobId) {
  event?.stopPropagation();
  const job = allJobs.find(j => (j._profile || 'default') === profile && j.id === jobId);
  if (!job) return showToast('Job não encontrado');
  editingJob = { profile, jobId };
  document.getElementById('modal-title').textContent = job.name;
  document.getElementById('modal-subtitle').textContent = `${profile} · ${job.id}`;
  document.getElementById('edit-name').value = job.name || '';
  document.getElementById('edit-schedule').value = scheduleText(job);
  document.getElementById('edit-prompt').value = job.prompt || '';
  setInputValue('edit-deliver', job.deliver || '');
  setInputValue('edit-script', job.script || '');
  loadSkillPicker(profile, Array.isArray(job.skills) ? job.skills : []);
  setInputValue('edit-toolsets', joinList(job.enabled_toolsets));
  setInputValue('edit-model-name', modelNameValue(job));
  setInputValue('edit-model-provider', modelProviderValue(job));
  setInputValue('edit-base-url', job.base_url || '');
  setInputValue('edit-context-from', joinList(job.context_from));
  document.getElementById('edit-no-agent').checked = !!job.no_agent;
  const diff = document.getElementById('edit-diff');
  diff.textContent = '';
  diff.classList.remove('open');
  document.getElementById('modal-backdrop').classList.add('open');
  document.getElementById('edit-modal').classList.add('open');
}

function closeEditModal() {
  editingJob = null;
  closeSkillsPicker();
  document.getElementById('modal-backdrop').classList.remove('open');
  document.getElementById('edit-modal').classList.remove('open');
}

function closeAllModals() {
  closeEditModal();
  closeRollbackModal();
}

function editPayload() {
  if (!editingJob) throw new Error('Nenhum job em edição');
  return {
    profile: editingJob.profile,
    id: editingJob.jobId,
    name: document.getElementById('edit-name').value,
    prompt: document.getElementById('edit-prompt').value,
    schedule_text: document.getElementById('edit-schedule').value,
    deliver: document.getElementById('edit-deliver').value,
    script: document.getElementById('edit-script').value,
    skills_text: document.getElementById('edit-skills').value,
    toolsets_text: document.getElementById('edit-toolsets').value,
    model_name: document.getElementById('edit-model-name').value,
    model_provider: document.getElementById('edit-model-provider').value,
    base_url: document.getElementById('edit-base-url').value,
    context_from_text: document.getElementById('edit-context-from').value,
    no_agent: document.getElementById('edit-no-agent').checked
  };
}

async function previewEdit() {
  try {
    const data = await apiPost('/api/job/preview', editPayload());
    const box = document.getElementById('edit-diff');
    box.textContent = data.changed ? data.diff : 'Sem alterações.';
    box.classList.add('open');
    showToast(data.changed ? 'Preview gerado' : 'Nada mudou');
  } catch (e) {
    const box = document.getElementById('edit-diff');
    box.textContent = e.message;
    box.classList.add('open');
    showToast(e.message);
  }
}

async function saveEdit() {
  if (!editingJob) return;
  try {
    const preview = await apiPost('/api/job/preview', editPayload());
    const box = document.getElementById('edit-diff');
    box.textContent = preview.changed ? preview.diff : 'Sem alterações.';
    box.classList.add('open');
    if (!preview.changed) return showToast('Sem alterações para salvar');
    if (!confirm('Salvar alterações neste cron job? Um backup será criado antes.')) return;
    const data = await apiPost('/api/job/update', editPayload());
    showToast(`Salvo. Backup: ${data.backup ? data.backup.split('/').slice(-2).join('/') : 'ok'}`);
    closeEditModal();
    await fetchJobs();
  } catch (e) {
    showToast(e.message);
  }
}

async function toggleJobStatus(event, profile, jobId, action) {
  event?.stopPropagation();
  const label = action === 'pause' ? 'pausar' : 'ativar';
  if (!confirm(`Confirmar ${label} este job?`)) return;
  try {
    await apiPost('/api/job/status', { profile, id: jobId, action });
    showToast(action === 'pause' ? 'Job pausado' : 'Job ativado');
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

async function runJobNow(event, profile, jobId) {
  event?.stopPropagation();
  if (!confirm('Rodar este job agora? Isso pode consumir créditos/tokens e enviar mensagem no canal configurado.')) return;
  try {
    const data = await apiPost('/api/job/run', { profile, id: jobId });
    showToast(data.run?.ok ? 'Job enviado para execução' : `Falha ao rodar: ${data.run?.stderr || 'erro'}`);
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

async function duplicateJob(event, profile, jobId) {
  event?.stopPropagation();
  const job = allJobs.find(j => (j._profile || 'default') === profile && j.id === jobId);
  const target = prompt(`Duplicar para qual profile?\nOpções: ${profileChoices('').join(', ')}`, profile);
  if (!target) return;
  const defaultName = `${job?.name || jobId} cópia`;
  const name = prompt('Nome da cópia:', defaultName);
  if (!name) return;
  if (!confirm('Duplicar este job? A cópia será criada pausada e com histórico zerado.')) return;
  try {
    const data = await apiPost('/api/job/duplicate', { profile, id: jobId, target_profile: target.trim(), name: name.trim() });
    showToast(`Job duplicado: ${data.new_job_id}`);
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

async function moveJob(event, profile, jobId) {
  event?.stopPropagation();
  const choices = profileChoices(profile);
  const target = prompt(`Mover para qual profile?\nOpções: ${choices.join(', ')}`, choices[0] || 'default');
  if (!target) return;
  if (target.trim() === profile) return showToast('Destino igual ao profile atual');
  if (!confirm(`Mover este job de ${profile} para ${target.trim()}? Backups serão criados nos dois profiles.`)) return;
  try {
    await apiPost('/api/job/move', { profile, id: jobId, target_profile: target.trim() });
    showToast(`Job movido para ${target.trim()}`);
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

async function deleteJob(event, profile, jobId) {
  event?.stopPropagation();
  const job = allJobs.find(j => (j._profile || 'default') === profile && j.id === jobId);
  const typed = prompt(`Excluir job "${job?.name || jobId}"?\nDigite EXCLUIR para confirmar. Backup será criado antes.`);
  if (typed !== 'EXCLUIR') return showToast('Exclusão cancelada');
  try {
    await apiPost('/api/job/delete', { profile, id: jobId });
    showToast('Job excluído com backup');
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

function openRollbackModal() {
  document.getElementById('modal-backdrop').classList.add('open');
  document.getElementById('rollback-modal').classList.add('open');
  const sel = document.getElementById('rollback-profile');
  sel.innerHTML = (availableProfiles.length ? availableProfiles : ['default']).map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('');
  loadBackups();
}

function closeRollbackModal() {
  document.getElementById('rollback-modal')?.classList.remove('open');
  if (!document.getElementById('edit-modal')?.classList.contains('open')) {
    document.getElementById('modal-backdrop')?.classList.remove('open');
  }
}

async function loadBackups() {
  const profile = document.getElementById('rollback-profile').value || 'default';
  const list = document.getElementById('backup-list');
  list.innerHTML = '<div class="modal-help">Carregando backups...</div>';
  try {
    const res = await fetch(`/api/backups?profile=${encodeURIComponent(profile)}&limit=50`);
    const data = await res.json();
    const backups = data.backups || [];
    if (!backups.length) {
      list.innerHTML = '<div class="modal-help">Nenhum backup encontrado para este profile.</div>';
      return;
    }
    list.innerHTML = backups.map(b => `
      <div class="backup-item">
        <div class="backup-main">
          <div class="backup-name">${escapeHtml(b.name)}</div>
          <div class="backup-meta">${escapeHtml(b.profile)} · ${new Date(b.modified_at).toLocaleString('pt-BR')} · ${(b.size/1024).toFixed(1)} KB</div>
        </div>
        <button class="job-action-btn danger" onclick="restoreBackup('${escapeJs(b.profile)}', '${escapeJs(b.name)}')">Restaurar</button>
      </div>`).join('');
  } catch (e) {
    list.innerHTML = `<div class="modal-help">${escapeHtml(e.message)}</div>`;
  }
}

async function restoreBackup(profile, backup) {
  if (!confirm(`Restaurar ${backup} no profile ${profile}? O jobs.json atual será salvo como backup antes.`)) return;
  const typed = prompt('Digite RESTAURAR para confirmar rollback do profile inteiro.');
  if (typed !== 'RESTAURAR') return showToast('Rollback cancelado');
  try {
    await apiPost('/api/backup/restore', { profile, backup });
    showToast('Backup restaurado');
    closeRollbackModal();
    await fetchJobs();
  } catch (e) { showToast(e.message); }
}

// ── CLOCK ─────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  const t = now.toLocaleTimeString('pt-BR', {
    hour:'2-digit', minute:'2-digit', second:'2-digit',
    timeZone:'America/Sao_Paulo'
  });
  const d = now.toLocaleDateString('pt-BR', {
    weekday:'short', day:'2-digit', month:'2-digit',
    timeZone:'America/Sao_Paulo'
  });
  document.getElementById('clock').textContent = `${d.toUpperCase()} · ${t}`;
}

// ── CONTROLS EVENTS ───────────────────────────────────────────────────────
document.querySelectorAll('#filter-status .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#filter-status .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentStatusFilter = btn.dataset.filter;
    render();
  });
});

document.getElementById('sort-select').addEventListener('change', e => {
  currentSort = e.target.value;
  render();
});

document.querySelectorAll('#view-mode .view-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#view-mode .view-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentView = btn.dataset.view;
    render();
  });
});

document.getElementById('kanban-dimension').addEventListener('change', e => {
  kanbanDimension = e.target.value;
  render();
});

document.getElementById('density-select').value = currentDensity;
document.getElementById('density-select').addEventListener('change', e => {
  currentDensity = e.target.value;
  localStorage.setItem('cronobs-density', currentDensity);
  render();
});

document.getElementById('rollback-profile')?.addEventListener('change', loadBackups);

document.addEventListener('click', (event) => {
  const picker = document.getElementById('skills-picker');
  if (picker && !picker.contains(event.target)) closeSkillsPicker();
});

// ── INIT ──────────────────────────────────────────────────────────────────
function manualRefresh() {
  const btn = document.getElementById('sync-btn');
  btn.classList.add('spinning');
  fetchJobs().finally(() => {
    btn.classList.remove('spinning');
    document.getElementById('refresh-label').textContent = 'ao vivo · 30s';
    // reset auto-refresh timer so manual refresh doesn't get followed by stale auto-refresh
    clearInterval(refreshTimer);
    refreshTimer = setInterval(fetchJobs, 30000);
  });
}

initTheme();
initFontScale();
fetchJobs();

// Keyboard shortcuts: Cmd/Ctrl + = (increase), Cmd/Ctrl + - (decrease), Cmd/Ctrl + 0 (reset)
document.addEventListener('keydown', function(e) {
  if (!(e.metaKey || e.ctrlKey)) return;
  if (e.key === '=' || e.key === '+') { e.preventDefault(); increaseFont(); }
  else if (e.key === '-')             { e.preventDefault(); decreaseFont(); }
  else if (e.key === '0')             { e.preventDefault(); applyFontScale(1); }
});
refreshTimer = setInterval(fetchJobs, 30000);
setInterval(updateCountdowns, 30000);
setInterval(tickBanner, 1000);
setInterval(updateClock, 1000);
updateClock();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._html()
        elif self.path == "/api/jobs":
            self._jobs()
        elif self.path == "/api/profiles":
            self._profiles()
        elif self.path.startswith("/api/skills"):
            self._skills()
        elif self.path.startswith("/api/backups"):
            self._backups()
        else:
            self._json({"ok": False, "error": "not found"}, status=404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            payload = self._read_json()
            profile = payload.get("profile") or "default"
            job_id = payload.get("id") or payload.get("job_id")
            job_paths = ("/api/job/preview", "/api/job/update", "/api/job/status", "/api/job/run", "/api/job/duplicate", "/api/job/move", "/api/job/delete")
            if self.path in job_paths and not job_id:
                raise ValueError("id do job é obrigatório")

            if self.path == "/api/job/preview":
                self._json(update_job_payload(profile, job_id, payload, commit=False))
            elif self.path == "/api/job/update":
                self._json(update_job_payload(profile, job_id, payload, commit=True))
            elif self.path == "/api/job/status":
                self._json(set_job_status(profile, job_id, payload.get("action")))
            elif self.path == "/api/job/run":
                run = run_hermes(profile, "cron", "run", job_id)
                # API ok=true significa que o endpoint respondeu; run.ok indica se o CLI aceitou.
                self._json({"ok": True, "profile": profile, "job_id": job_id, "run": run})
            elif self.path == "/api/job/duplicate":
                self._json(duplicate_job(profile, job_id, payload.get("target_profile"), payload.get("name")))
            elif self.path == "/api/job/move":
                self._json(move_job(profile, job_id, payload.get("target_profile")))
            elif self.path == "/api/job/delete":
                self._json(delete_job(profile, job_id))
            elif self.path == "/api/backup/restore":
                self._json(restore_backup(profile, payload.get("backup") or payload.get("backup_name")))
            else:
                self._json({"ok": False, "error": "not found"}, status=404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, status=400)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _jobs(self):
        try:
            jobs = get_all_jobs()
            self._json({"jobs": jobs})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, status=500)

    def _profiles(self):
        try:
            self._json({"profiles": public_profiles()})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, status=500)

    def _skills(self):
        try:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            profile = (qs.get("profile") or ["default"])[0]
            self._json(available_skills(profile))
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, status=500)

    def _backups(self):
        try:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            profile = (qs.get("profile") or [None])[0]
            limit = int((qs.get("limit") or [30])[0])
            self._json({"backups": list_backups(profile, limit)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, status=500)

    def log_message(self, format, *args): pass  # noqa: silence request logs


def _kill_port(port):
    """Mata processo que esteja escutando na porta, se houver."""
    import subprocess
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
        if out:
            for pid in out.splitlines():
                pid = pid.strip()
                if pid:
                    subprocess.run(["kill", pid], capture_output=True)
            print(f"Processo anterior na porta {port} morto.")
    except subprocess.CalledProcessError:
        pass  # porta livre, nada pra matar


if __name__ == "__main__":
    _kill_port(PORT)
    url = f"http://{HOST}:{PORT}"

    # Write PID file for plugin lifecycle management
    pid_file = Path.home() / ".hermes" / "cronobs.pid"
    pid_file.write_text(str(os.getpid()))

    print(f"cronobs ● {url}")
    print("Ctrl+C para encerrar\n")

    # Open browser unless suppressed via env var
    if not os.environ.get("CRONOBS_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    server = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncronobs encerrado.")
    finally:
        pid_file.unlink(missing_ok=True)

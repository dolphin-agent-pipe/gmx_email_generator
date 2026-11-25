"""Mailbox studio.

Import signup rows, run them through one number service, and keep the
finished mailboxes.

    python studio.py

That opens the desktop window. Start the chosen number service first
(sms_code_service, hero_sms_service, or anosim_service). Only one provider
is used for a batch.

Each imported line is split on semicolons:

    email;first;last;day;month;year;plz;city;street;password
    email;first;last;day;month;year;plz;city;street;password;country

The email domain chooses the signup. GMX and WEB.DE order an SMS number.
A Proton address (proton.me, protonmail.com, proton.com, or pm.me) uses proton_gen.py
and does not order a number. Proton rows only need the address and password:

    name@proton.me;password
    name@proton.me;First;Last;password

If a GMX or WEB.DE address is taken, signup uses the provider suggestion
and marks the row as suggested.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import live_numbers

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STORE_FILE = ROOT / "studio_queue.json"
ACCOUNTS_FILE = ROOT / "accounts.json"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


load_env(ENV_FILE)
HOST = os.environ.get("STUDIO_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("STUDIO_PORT", "8790") or "8790")
GMX_URL = os.environ.get("TARGET_URL1", "https://www.gmx.net/").strip()
WEBDE_URL = os.environ.get("WEBDE_URL", "https://www.web.de/").strip()
PROVIDERS = {
    "smscode": {
        "label": "SMS code",
        "base": f"http://127.0.0.1:{os.environ.get('SMS_CODE_PORT', '8780')}",
    },
    "hero": {
        "label": "Hero SMS",
        "base": f"http://127.0.0.1:{os.environ.get('HERO_SMS_PORT', '8781')}",
    },
    "anosim": {
        "label": "Anosim",
        "base": f"http://127.0.0.1:{os.environ.get('ANOSIM_PORT', '8782')}",
    },
}
COLUMNS = ("email", "first", "last", "day", "month", "year", "plz", "city", "street", "password")
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@(?:web\.de|gmx\.net|gmx\.de|gmx\.com|gmx\.at|gmx\.ch|proton\.me|protonmail\.com|proton\.com|pm\.me)$",
    re.IGNORECASE,
)

state_lock = threading.Lock()
order_lock = threading.Lock()
state: dict = {
    "rows": [], "history": [], "running": False,
    "provider": "smscode", "kind": "gmx",
    "target_success": 0, "batch_start": 0.0,
    "max_concurrent": 1,
}
worker: threading.Thread | None = None
inflight = 0
store_blocked = False
cron_lock = threading.Lock()
cron_job_cancel: threading.Event | None = None
cron_generation = 0
cron_info = {"active": False, "message": "", "started": 0, "total": 0}


def park_broken(path: Path) -> Path | None:
    backup = path.with_name(f"{path.stem}.broken-{time.time_ns()}{path.suffix}")
    try:
        path.replace(backup)
    except OSError:
        return None
    return backup


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(text, encoding="utf-8")
    last: Exception | None = None
    try:
        for _attempt in range(5):
            try:
                temp.replace(path)
                return
            except OSError as exc:
                last = exc
                time.sleep(0.15)
        raise OSError(f"Could not save {path.name}") from last
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def load_store() -> None:
    global store_blocked
    if not STORE_FILE.is_file():
        return
    try:
        loaded = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = park_broken(STORE_FILE)
        if backup is None:
            store_blocked = True
            print(f"Could not read the queue file ({exc}). It will not be overwritten.", flush=True)
        else:
            print(f"Could not read the queue file ({exc}). Kept it as {backup.name}.", flush=True)
        return
    if not isinstance(loaded, dict):
        return
    rows = loaded.get("rows")
    history = loaded.get("history")
    if isinstance(rows, list):
        state["rows"] = [item for item in rows if isinstance(item, dict)]
    if isinstance(history, list):
        state["history"] = [item for item in history if isinstance(item, dict)]
    changed = False
    for row in state["rows"]:
        if row.get("status") in {"running", "queued"}:
            row["cancel"] = False
            if row.get("task") == "pop3":
                row["status"] = "pop3"
                row["detail"] = "Interrupted. Enable POP3/IMAP again."
            else:
                row["status"] = "ready"
                row["task"] = ""
                row["detail"] = "Interrupted. Start again."
            changed = True
        elif row.get("status") == "failed" and settings_failure(str(row.get("detail") or "")) and "@" in str(row.get("email") or ""):
            row["status"] = "pop3"
            row["task"] = "pop3"
            changed = True
    if changed:
        save_store()


def save_store() -> None:
    if store_blocked:
        print("Skipped saving the queue because the current file could not be read", flush=True)
        return
    payload = {"rows": state["rows"], "history": state["history"]}
    write_text_atomic(STORE_FILE, json.dumps(payload, indent=2))


def read_store_file() -> tuple[list[dict], list[dict]]:
    if not STORE_FILE.is_file():
        return [], []
    try:
        loaded = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read the queue file") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("Could not read the queue file")
    rows = loaded.get("rows")
    history = loaded.get("history")
    disk_rows = [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    disk_history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    return disk_rows, disk_history


def account_records() -> list[dict]:
    if not ACCOUNTS_FILE.is_file():
        return []
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and str(item.get("email") or "").strip()]


PROFILE_FIELDS = (
    "email",
    "requested_email",
    "suggested",
    "first",
    "last",
    "day",
    "month",
    "year",
    "plz",
    "city",
    "street",
    "password",
    "country",
)


def person_name(row: dict) -> str:
    return f"{str(row.get('first') or '').strip()} {str(row.get('last') or '').strip()}".strip()


def kind_label(kind: str) -> str:
    return {"webde": "WEB.DE", "gmx": "GMX", "proton": "Proton"}.get(kind, kind)


def history_kind(row: dict) -> str:
    return kind_label(str(row.get("kind") or ""))


def history_provider(row: dict) -> str:
    key = str(row.get("provider") or "")
    provider = PROVIDERS.get(key)
    return str(provider["label"]) if provider else key


def apply_profile(item: dict, row: dict) -> None:
    name = person_name(row)
    if name:
        item["name"] = name
    phone = str(row.get("phone") or "").strip()
    if phone:
        item["phone"] = phone
    password = str(row.get("password") or "")
    if password and not str(item.get("password") or ""):
        item["password"] = password
    if not str(item.get("id") or "") and row.get("id"):
        item["id"] = row["id"]
    if not str(item.get("created_at") or "") and row.get("created_at"):
        item["created_at"] = row["created_at"]
    if not str(item.get("provider") or ""):
        item["provider"] = history_provider(row)
    if not str(item.get("kind") or ""):
        item["kind"] = history_kind(row)


def reload_store() -> None:
    with state_lock:
        disk_rows, disk_history = read_store_file()
        accounts = account_records()
        live = {
            str(row.get("id")): row
            for row in state["rows"]
            if row.get("status") in {"running", "queued"} and row.get("id")
        }
        merged: list[dict] = []
        seen: set[str] = set()
        for row in disk_rows:
            row_id = str(row.get("id") or "")
            current = live.get(row_id)
            if current is not None:
                for key in PROFILE_FIELDS:
                    if key in row:
                        current[key] = row[key]
                merged.append(current)
            else:
                merged.append(row)
            if row_id:
                seen.add(row_id)
        for row_id, row in live.items():
            if row_id not in seen:
                merged.append(row)
        by_id = {str(row.get("id") or ""): row for row in merged if row.get("id")}
        by_email: dict[str, dict] = {}
        for row in merged:
            email = str(row.get("email") or "").strip().lower()
            if "@" in email:
                by_email.setdefault(email, row)
        history: list[dict] = []
        known: set[str] = set()
        for item in disk_history:
            row = by_id.get(str(item.get("id") or ""))
            if row is None:
                row = by_email.get(str(item.get("email") or "").strip().lower())
            if row is not None:
                apply_profile(item, row)
            email = str(item.get("email") or "").strip().lower()
            if email:
                known.add(email)
            history.append(item)
        for account in accounts:
            email = str(account.get("email") or "").strip()
            key = email.lower()
            if not key or key in known:
                continue
            row = by_email.get(key)
            item = {
                "id": "",
                "created_at": "",
                "email": email,
                "password": str(account.get("password") or ""),
                "provider": "",
                "kind": "",
                "name": "",
                "phone": "",
            }
            if row is not None:
                apply_profile(item, row)
            history.append(item)
            known.add(key)
        state["rows"] = merged
        state["history"] = history
        save_store()


def public_state() -> dict:
    providers = working_providers()
    with cron_lock:
        cron = dict(cron_info)
    with state_lock:
        done_count = sum(1 for r in state["rows"] if r.get("status") == "done")
        target = state.get("target_success", 0)
        batch_start = state.get("batch_start", 0.0)
        elapsed = time.time() - batch_start if batch_start else 0.0
        time_left = max(0.0, 24 * 3600 - elapsed) if target > 0 and batch_start else 0.0
        return {
            "running": state["running"],
            "provider": state["provider"],
            "kind": state["kind"],
            "providers": providers,
            "rows": list(state["rows"]),
            "history": list(state["history"]),
            "reuse_numbers": live_numbers.enabled(),
            "live_count": live_numbers.live_count(),
            "target_success": target,
            "done_count": done_count,
            "time_left": int(time_left),
            "max_concurrent": state.get("max_concurrent", 1),
            "cron": cron,
        }


def parse_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.split(";")]
        if parts and parts[0].lower() in {"email", "first"}:
            continue
        row = {key: "" for key in COLUMNS}
        row["country"] = "DE"
        email = parts[0].strip() if parts else ""
        kind = kind_from_email(email)
        if kind == "proton" and 1 < len(parts) < len(COLUMNS):
            row["email"] = email
            row["password"] = parts[-1]
            if len(parts) >= 3:
                row["first"] = parts[1]
            if len(parts) >= 4:
                row["last"] = parts[2]
        else:
            for key, value in zip(COLUMNS, parts):
                row[key] = value
            if len(parts) >= 11 and parts[10]:
                row["country"] = parts[10].upper()
            email = str(row.get("email") or "").strip()
            kind = kind_from_email(email)
        row.update(
            {
                "id": uuid.uuid4().hex[:12],
                "status": "ready",
                "detail": "",
                "phone": "",
                "email": email,
                "requested_email": email,
                "suggested": False,
                "created_at": "",
                "provider": "",
                "kind": kind,
            }
        )
        if kind == "proton":
            if not EMAIL_RE.match(email):
                row["status"] = "failed"
                row["detail"] = "Email must be a Proton address"
            elif not str(row.get("password") or "").strip():
                row["status"] = "failed"
                row["detail"] = "Missing password"
        else:
            missing = [key for key in COLUMNS if not row[key]]
            if missing:
                row["status"] = "failed"
                row["detail"] = "Missing " + ", ".join(missing)
            elif not EMAIL_RE.match(email) or not kind:
                row["status"] = "failed"
                row["detail"] = "Email must be a GMX, WEB.DE, or Proton address"
        rows.append(row)
    return rows


def request_json(base: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode(errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw[:240] or f"HTTP {exc.code}"}
    if not isinstance(payload, dict):
        raise RuntimeError("Number service returned an unexpected response")
    return payload


def provider_base(provider: str) -> str:
    """Use the port the SMS service actually bound when 8780 was already taken."""
    fallback = PROVIDERS[provider]["base"]
    if provider != "smscode":
        return fallback
    path = ROOT / "logs" / "sms.port"
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return fallback
    if not 1 <= port <= 65535:
        return fallback
    return f"http://127.0.0.1:{port}"


def provider_up(provider: str, timeout: float = 3) -> bool:
    try:
        payload = request_json(provider_base(provider), "/health", timeout=timeout)
        return payload.get("ok") is True
    except Exception:
        return False


_health_lock = threading.Lock()
_health_cache = {"at": 0.0, "ids": []}


def working_providers() -> list[dict]:
    """Number services whose /health answers. Down services stay out of the menu."""
    now = time.time()
    with _health_lock:
        if now - _health_cache["at"] < 5:
            return [{"id": key, "label": PROVIDERS[key]["label"]} for key in _health_cache["ids"]]
    found: list[str] = []
    keys = list(PROVIDERS)
    if keys:
        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            for key in pool.map(lambda item: item if provider_up(item, timeout=0.8) else "", keys):
                if key:
                    found.append(key)
    with _health_lock:
        _health_cache["at"] = time.time()
        _health_cache["ids"] = found
    return [{"id": key, "label": PROVIDERS[key]["label"]} for key in found]


def phone_values(payload: dict) -> list[str]:
    found = []
    for item in payload.get("phones") or []:
        if isinstance(item, str):
            found.append(item.strip())
        elif isinstance(item, dict) and item.get("phone"):
            found.append(str(item["phone"]).strip())
    return [phone for phone in found if phone]


def kind_from_email(email: str) -> str:
    addr = str(email or "").strip().lower()
    if addr.endswith("@web.de"):
        return "webde"
    if re.search(r"@gmx\.(net|de|com|at|ch)$", addr):
        return "gmx"
    if re.search(r"@(?:proton\.me|protonmail\.com|proton\.com|pm\.me)$", addr):
        return "proton"
    return ""


def service_email(row: dict, kind: str) -> str:
    wanted = str(row.get("email") or "").strip()
    if EMAIL_RE.match(wanted):
        return wanted
    local = re.sub(r"[^a-z0-9]+", ".", f"{row['first']}.{row['last']}".lower()).strip(".")
    local = local[:40] or row["id"]
    if kind == "proton":
        domain = "proton.me"
    elif kind == "webde":
        domain = "web.de"
    else:
        domain = "gmx.net"
    return f"{local}@{domain}"


class RowCancelled(Exception):
    pass


def settings_failure(message: str) -> bool:
    lowered = message.lower()
    return any(part in lowered for part in ("pop3", "imap", "mailbox settings", "e-mail empfangen"))


def console(row: dict, detail: str) -> None:
    name = f"{row.get('first', '')} {row.get('last', '')}".strip() or row.get("id", "")
    print(f"[{name}] {detail}", flush=True)


def set_detail(row: dict, detail: str) -> None:
    console(row, detail)
    with state_lock:
        row["detail"] = detail
        save_store()


def row_cancelled(row: dict) -> bool:
    with state_lock:
        return bool(row.get("cancel"))


def ensure_active(row: dict) -> None:
    if row_cancelled(row):
        raise RowCancelled()


def call_service(row: dict, base: str, path: str, body: dict | None, timeout: float) -> dict:
    box: dict = {}

    def work() -> None:
        try:
            box["result"] = request_json(base, path, body, timeout=timeout)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=work, name="studio-request", daemon=True)
    thread.start()
    while thread.is_alive():
        ensure_active(row)
        thread.join(0.4)
    if "error" in box:
        raise box["error"]
    return box.get("result") or {}


def finish_numbers(provider: str, email: str, row: dict) -> bool:
    try:
        payload = call_service(row, provider_base(provider), "/code_passed", {"email": email}, 180)
    except RowCancelled:
        raise
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        set_detail_by_email(email, f"Could not close numbers: {exc}")
        return False
    if isinstance(payload, dict) and payload.get("ok"):
        return True
    if isinstance(payload, dict) and payload.get("error") == "unknown email":
        return True
    detail = str(payload.get("error") or "waiting") if isinstance(payload, dict) else "waiting"
    set_detail(row, f"Previous price order is still open: {detail}")
    return False


def set_detail_by_email(email: str, detail: str) -> None:
    with state_lock:
        for row in state["rows"]:
            if row.get("service_email") == email and row.get("status") == "running":
                row["detail"] = detail
        save_store()


def proton_display_name(row: dict, username: str) -> str:
    parts = [str(row.get("first") or "").strip(), str(row.get("last") or "").strip()]
    return " ".join(part for part in parts if part) or username


def run_proton_row(row: dict) -> None:
    from proton_gen import generate_mailbox, normalize_username

    desired = str(row.get("requested_email") or row.get("email") or "").strip()
    password = str(row.get("password") or "")
    try:
        username = normalize_username(desired)
    except SystemExit as exc:
        message = str(getattr(exc, "code", "") or exc).strip() or "Username is missing"
        with state_lock:
            row["status"] = "failed"
            row["kind"] = "proton"
            row["detail"] = message[:300]
            save_store()
        console(row, row["detail"])
        return
    if not password.strip():
        with state_lock:
            row["status"] = "failed"
            row["kind"] = "proton"
            row["detail"] = "Missing password"
            save_store()
        console(row, row["detail"])
        return
    profile = re.sub(r"[^A-Za-z0-9._-]+", "-", username).strip("-") or row["id"]
    with state_lock:
        row["status"] = "running"
        row["provider"] = ""
        row["kind"] = "proton"
        row["task"] = ""
        row["requested_email"] = desired
        row["email"] = desired
        row["suggested"] = False
        row["service_email"] = desired
        row["detail"] = "Creating the Proton mailbox"
        save_store()
    console(row, "Creating the Proton mailbox")

    def log(message: str) -> None:
        ensure_active(row)
        set_detail(row, message)

    try:
        email = generate_mailbox(
            profile,
            desired,
            password,
            proton_display_name(row, username),
            log=log,
            stop=lambda: ensure_active(row),
        )
        if email is None:
            from proton_gen import manual_pause_stage

            pause = manual_pause_stage()
            detail = (
                "Paused after scroll down — continue in Dolphin"
                if pause == "scroll"
                else "Paused after confirm password — continue in Dolphin"
            )
            with state_lock:
                row["status"] = "running"
                row["detail"] = detail
                save_store()
            console(row, row["detail"])
            return
        created = now_stamp()
        with state_lock:
            row["status"] = "done"
            row["email"] = email
            row["requested_email"] = desired
            row["suggested"] = False
            row["phone"] = ""
            row["created_at"] = created
            row["detail"] = "Mailbox created"
            state["history"].insert(
                0,
                {
                    "id": row["id"],
                    "created_at": created,
                    "email": email,
                    "password": password,
                    "provider": "Proton",
                    "kind": "Proton",
                    "name": person_name(row) or username,
                    "phone": "",
                    "suggested": False,
                    "requested_email": desired,
                },
            )
            save_store()
        console(row, f"Mailbox created: {email}")
    except RowCancelled:
        console(row, "Cancelled")
        with state_lock:
            row["status"] = "ready"
            row["cancel"] = False
            row["detail"] = "Cancelled"
            save_store()
    except (SystemExit, OSError, TimeoutError, RuntimeError, ValueError) as exc:
        message = str(getattr(exc, "code", "") or exc).strip() or "Generation failed"
        if password:
            message = message.replace(password, "***")
        with state_lock:
            row["status"] = "failed"
            row["cancel"] = False
            row["detail"] = message[:300]
            save_store()
        console(row, row["detail"])
    except Exception as exc:
        traceback.print_exc()
        message = str(exc).replace(password, "***") if password else str(exc)
        with state_lock:
            row["status"] = "failed"
            row["cancel"] = False
            row["detail"] = message[:300] or "Generation failed"
            save_store()
        console(row, row["detail"])


def run_row(row: dict, provider: str, kind: str) -> None:
    from gmx_gen import MailboxSettingsError, NoPhonesLeft, SignupRestart, generate_mailbox, mailbox_address

    desired = str(row.get("requested_email") or row.get("email") or "").strip()
    kind = kind_from_email(desired) or str(kind or "").strip()
    if kind == "proton":
        run_proton_row(row)
        return
    if not EMAIL_RE.match(desired) or kind not in {"gmx", "webde"}:
        with state_lock:
            row["status"] = "failed"
            row["detail"] = "Email must be a GMX, WEB.DE, or Proton address"
            save_store()
        console(row, row["detail"])
        return

    email_key = service_email(row, kind)
    with state_lock:
        row["status"] = "running"
        row["provider"] = provider
        row["kind"] = kind
        row["requested_email"] = desired
        row["suggested"] = False
        row["service_email"] = email_key
        row["detail"] = "Ordering numbers"
        save_store()
    console(row, f"Ordering numbers for {kind_label(kind)}")
    ordered = False
    try:
        price_index = 0
        failed_batches = 0
        suggested = False

        def cancel_previous_price(force: bool = False) -> bool:
            nonlocal ordered
            if not ordered and not force:
                return True
            console(row, "Cancelling the previous price order")
            set_detail(row, "Cancelling the previous price order")
            closed = False
            for _attempt in range(4):
                ensure_active(row)
                closed = finish_numbers(provider, email_key, row)
                if closed:
                    break
                time.sleep(1.5)
            ordered = False
            return closed

        while True:
            ensure_active(row)
            with order_lock:
                ensure_active(row)
                started = call_service(
                    row,
                    provider_base(provider),
                    "/get_started",
                    {"email": email_key, "type": kind, "count": 3, "price_index": price_index},
                    240,
                )
            phones = phone_values(started)[:3]
            if not started.get("ok") or not phones:
                error = str(started.get("error") or "The number service did not return a phone")
                lowered = error.lower()
                if "could not cancel the previous price order" in lowered:
                    if not cancel_previous_price(force=True):
                        raise RuntimeError("Could not cancel the previous price order")
                    console(row, "Previous price order cleared. Ordering again.")
                    continue
                if price_index >= 20 or any(
                    phrase in lowered
                    for phrase in ("no cheaper price", "no germany numbers", "no price with stock", "numbers with stock remain")
                ):
                    raise RuntimeError("No cheaper phone price is left")
                if ordered and not cancel_previous_price():
                    raise RuntimeError("Could not cancel the previous price order")
                price_index += 1
                console(row, f"{error} Trying the next price.")
                continue
            ordered = True
            set_detail(row, f"{len(phones)} numbers ready")

            def fetch_code(phone: str) -> str:
                ensure_active(row)
                with state_lock:
                    row["phone"] = phone
                    row["detail"] = f"Waiting for SMS on {phone}"
                    save_store()
                console(row, f"Waiting for SMS on {phone}")
                payload = call_service(row, provider_base(provider), "/get_code", {"phone": phone}, 240)
                return str(payload.get("code") or "") if payload.get("ok") else ""

            def log(message: str) -> None:
                ensure_active(row)
                set_detail(row, message)

            def stop() -> None:
                ensure_active(row)

            url = WEBDE_URL if kind == "webde" else GMX_URL
            profile = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{row['first']}-{row['last']}").strip("-") or row["id"]
            mailbox = ""
            used = ""
            restarted = False
            for browser_try in range(3):
                ensure_active(row)
                try:
                    mailbox, used, suggested = generate_mailbox(
                        profile,
                        row["first"],
                        row["last"],
                        row["day"],
                        row["month"],
                        row["year"],
                        row["plz"],
                        row["city"],
                        row["street"],
                        row.get("country") or "DE",
                        row["password"],
                        url,
                        phones,
                        fetch_code,
                        log=log,
                        stop=stop,
                        desired_email=desired,
                    )
                    break
                except SignupRestart:
                    restarted = True
                    break
                except SystemExit as exc:
                    message = str(getattr(exc, "code", "") or exc).strip() or "Generation failed"
                    lowered = message.lower()
                    finished = any(
                        phrase in lowered
                        for phrase in (
                            "created mailbox",
                            "could not read the created mailbox",
                            "could not save the mailbox",
                            "zustimmen",
                        )
                    )
                    if browser_try == 4 or "phone" in lowered or finished:
                        raise
                    console(row, f"{message}. Opening the signup again.")
            if restarted:
                if ordered and not cancel_previous_price():
                    raise RuntimeError("Could not cancel the previous price order")
                failed_batches += 1
                price_index += 1
                if failed_batches >= 3:
                    raise RuntimeError("Nine phone numbers failed to verify")
                if price_index >= 21:
                    raise RuntimeError("No cheaper phone price is left")
                console(row, "Three numbers failed. Restarting with the next cheaper price.")
                continue
            if not mailbox:
                raise RuntimeError("Generation failed")
            break
        created = now_stamp()
        with state_lock:
            row["status"] = "done"
            row["email"] = mailbox
            row["requested_email"] = desired
            row["suggested"] = bool(suggested)
            row["phone"] = used or row.get("phone") or ""
            row["created_at"] = created
            row["detail"] = "Suggested mailbox created" if suggested else "Mailbox created"
            state["history"].insert(
                0,
                {
                    "id": row["id"],
                    "created_at": created,
                    "email": mailbox,
                    "password": row["password"],
                    "provider": PROVIDERS[provider]["label"],
                    "kind": kind_label(kind),
                    "name": f"{row['first']} {row['last']}",
                    "phone": row["phone"],
                    "suggested": bool(suggested),
                    "requested_email": desired,
                },
            )
            save_store()
        console(row, f"Mailbox created: {mailbox}" + (" (suggested)" if suggested else ""))
    except RowCancelled:
        console(row, "Cancelled")
        with state_lock:
            row["status"] = "ready"
            row["cancel"] = False
            row["detail"] = "Cancelled"
            save_store()
    except (NoPhonesLeft, SystemExit, OSError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
        message = str(getattr(exc, "code", "") or exc).strip() or "Generation failed"
        email = mailbox_address(str(getattr(exc, "email", "") or ""))
        known = email or str(row.get("email") or "")
        requested = str(row.get("requested_email") or desired or "").strip()
        was_suggested = bool(email and requested and email.lower() != requested.lower())
        pop3_failed = (isinstance(exc, MailboxSettingsError) or settings_failure(message)) and "@" in known
        with state_lock:
            row["status"] = "pop3" if pop3_failed else "failed"
            row["task"] = "pop3" if pop3_failed else ""
            row["detail"] = message[:300]
            row["cancel"] = False
            if email:
                row["email"] = email
                row["suggested"] = was_suggested
                if not row.get("created_at"):
                    row["created_at"] = now_stamp()
                if not any(item.get("id") == row.get("id") and item.get("email") == email for item in state["history"]):
                    state["history"].insert(
                        0,
                        {
                            "id": row["id"],
                            "created_at": row["created_at"],
                            "email": email,
                            "password": row["password"],
                            "provider": history_provider(row),
                            "kind": history_kind(row),
                            "name": person_name(row),
                            "phone": row.get("phone") or "",
                            "suggested": was_suggested,
                            "requested_email": requested,
                        },
                    )
            save_store()
        console(row, row["detail"])
    except Exception as exc:
        traceback.print_exc()
        with state_lock:
            row["status"] = "failed"
            row["cancel"] = False
            row["detail"] = str(exc)[:300] or "Generation failed"
            save_store()
        console(row, row["detail"])
    finally:
        if ordered:
            try:
                finish_numbers(provider, email_key, row)
            except Exception:
                traceback.print_exc()


def finish_row_thread() -> None:
    global inflight
    with state_lock:
        inflight = max(0, inflight - 1)


def run_pop3(row: dict) -> None:
    email = str(row.get("email") or "").strip()
    password = str(row.get("password") or "")
    try:
        if "@" not in email or not password:
            raise SystemExit("This row has no mailbox to enable")

        def log(message: str) -> None:
            ensure_active(row)
            set_detail(row, message)

        from _test_pop3 import enable_existing_mailbox

        enable_existing_mailbox(email, password, log=log, stop=lambda: ensure_active(row))
        with state_lock:
            row["status"] = "done"
            row["task"] = ""
            row["cancel"] = False
            row["detail"] = "POP3/IMAP is on"
            save_store()
        console(row, f"POP3/IMAP is on for {email}")
    except RowCancelled:
        console(row, "Cancelled")
        with state_lock:
            row["status"] = "pop3"
            row["task"] = "pop3"
            row["cancel"] = False
            row["detail"] = "Cancelled"
            save_store()
    except (SystemExit, OSError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
        message = str(getattr(exc, "code", "") or exc).strip() or "Could not turn on POP3/IMAP"
        if password:
            message = message.replace(password, "***")
        with state_lock:
            row["status"] = "pop3"
            row["task"] = "pop3"
            row["cancel"] = False
            row["detail"] = message[:300]
            save_store()
        console(row, row["detail"])
    except Exception as exc:
        traceback.print_exc()
        message = str(exc).replace(password, "***") if password else str(exc)
        with state_lock:
            row["status"] = "pop3"
            row["task"] = "pop3"
            row["cancel"] = False
            row["detail"] = message[:300] or "Could not turn on POP3/IMAP"
            save_store()
        console(row, row["detail"])


def run_row_thread(row: dict) -> None:
    try:
        if row.get("task") == "pop3":
            run_pop3(row)
        else:
            run_row(row, str(row.get("provider") or ""), str(row.get("kind") or "gmx"))
    except Exception:
        traceback.print_exc()
    finally:
        finish_row_thread()


def worker_loop() -> None:
    global inflight
    while True:
        row = None
        started = False
        try:
            with state_lock:
                max_concurrent = state.get("max_concurrent", 1)
                if inflight >= max_concurrent:
                    pass  # handled below — release lock and wait
            if inflight >= max_concurrent:
                time.sleep(0.3)
                continue
            with state_lock:
                target = state.get("target_success", 0)
                batch_start = state.get("batch_start", 0.0)
                elapsed = time.time() - batch_start if batch_start else 0.0
                deadline_reached = target > 0 and elapsed >= 24 * 3600
                done_count = sum(1 for r in state["rows"] if r.get("status") == "done")
                target_met = target > 0 and done_count >= target

                if deadline_reached and not target_met:
                    print(f"[worker] 24-Stunden-Grenze erreicht. {done_count}/{target} erfolgreich.", flush=True)
                    state["running"] = False
                    state["target_success"] = 0
                    state["batch_start"] = 0.0
                    save_store()
                    return

                if target_met:
                    print(f"[worker] Ziel erreicht: {done_count}/{target} erfolgreich.", flush=True)
                    state["running"] = False
                    state["target_success"] = 0
                    state["batch_start"] = 0.0
                    save_store()
                    return

                row = next((item for item in state["rows"] if item.get("status") == "queued"), None)
                if row is None:
                    if inflight == 0:
                        if target > 0 and not target_met and not deadline_reached:
                            retried = 0
                            for r in state["rows"]:
                                prev_status = r.get("status")
                                if prev_status in {"failed", "pop3"} and not r.get("cancel"):
                                    r["status"] = "queued"
                                    r["task"] = "pop3" if prev_status == "pop3" else ""
                                    r["cancel"] = False
                                    r["detail"] = "Wartet (Wiederholung)"
                                    retried += 1
                            if retried:
                                save_store()
                                print(f"[worker] {retried} Zeile(n) werden wiederholt ({done_count}/{target} fertig).", flush=True)
                            else:
                                print(f"[worker] Keine Zeilen zum Wiederholen. Warte…", flush=True)
                        else:
                            state["running"] = False
                            save_store()
                            return
                elif row.get("cancel"):
                    row["status"] = "pop3" if row.get("task") == "pop3" else "ready"
                    row["cancel"] = False
                    row["detail"] = "Cancelled"
                    save_store()
                    console(row, "Cancelled")
                    row = None
                else:
                    row["status"] = "running"
                    inflight += 1
                    save_store()
            if row is None:
                time.sleep(0.2)
                continue
            threading.Thread(target=run_row_thread, args=(row,), name=f"studio-row-{row.get('id')}", daemon=True).start()
            started = True
        except Exception:
            traceback.print_exc()
            if row is not None and not started:
                with state_lock:
                    if row.get("status") == "running":
                        if row.get("task") == "pop3":
                            row["status"] = "pop3"
                            row["detail"] = "Could not start POP3/IMAP. Enable it again."
                        else:
                            row["status"] = "ready"
                            row["task"] = ""
                            row["detail"] = "Could not start this row. Start it again."
                        inflight = max(0, inflight - 1)
                        save_store()
            time.sleep(0.5)


def ensure_worker() -> None:
    global worker
    if worker is not None and worker.is_alive():
        return
    worker = threading.Thread(target=worker_loop, name="studio-batch", daemon=True)
    worker.start()


def _needs_sms(rows: list[dict]) -> bool:
    return any(
        kind_from_email(str(row.get("requested_email") or row.get("email") or "")) in {"gmx", "webde"}
        for row in rows
    )


def _enqueue(provider: str, ids: list[str]) -> int:
    wanted = {str(item) for item in ids if str(item).strip()}
    with state_lock:
        queued = 0
        for row in state["rows"]:
            if row.get("id") not in wanted or row.get("status") not in {"ready", "failed"}:
                continue
            email = str(row.get("requested_email") or row.get("email") or "").strip()
            kind = kind_from_email(email)
            if not kind:
                row["status"] = "failed"
                row["detail"] = "Email must be a GMX, WEB.DE, or Proton address"
                continue
            row["status"] = "queued"
            row["task"] = ""
            row["cancel"] = False
            row["provider"] = "" if kind == "proton" else provider
            row["kind"] = kind
            row["requested_email"] = email
            row["suggested"] = False
            row["detail"] = "Waiting"
            queued += 1
            if kind == "proton":
                console(row, "Queued Proton signup")
            else:
                console(row, f"Queued on {PROVIDERS[provider]['label']} ({kind_label(kind)})")
        if queued:
            state["running"] = True
            if provider in PROVIDERS:
                state["provider"] = provider
            save_store()
            ensure_worker()
        return queued


def start_rows(provider: str, ids: list[str], target: int = 0) -> dict:
    wanted = {str(item) for item in ids if str(item).strip()}
    if not wanted:
        raise RuntimeError("Select at least one row")
    with state_lock:
        chosen = [
            row for row in state["rows"]
            if row.get("id") in wanted and row.get("status") in {"ready", "failed"}
        ]
        needs_sms = _needs_sms(chosen)
    if needs_sms:
        if provider not in PROVIDERS:
            raise ValueError("Choose one number service")
        if not provider_up(provider):
            label = PROVIDERS[provider]["label"]
            base = provider_base(provider)
            raise RuntimeError(f"{label} is not running at {base}")
    queued = _enqueue(provider, ids)
    if not queued:
        raise RuntimeError("Select a row that is waiting or failed")
    with state_lock:
        if target > 0:
            state["target_success"] = target
            state["batch_start"] = time.time()
            print(f"[worker] Ziel: {target} erfolgreiche Mailboxen innerhalb von 24 Stunden.", flush=True)
        else:
            state["target_success"] = 0
            state["batch_start"] = 0.0
        save_store()
    return public_state()


def select_cron_rows(rows: list[dict], target: int) -> list[dict]:
    chosen: list[dict] = []
    for row in rows:
        if row.get("status") not in {"ready", "failed"}:
            continue
        chosen.append(row)
        if len(chosen) >= target:
            break
    return chosen


def _parse_local_time(value: str) -> float:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError("Enter a start time")


def _cron_live(generation: int) -> bool:
    with cron_lock:
        return generation == cron_generation


def _cron_finish(message: str, generation: int | None = None) -> None:
    with cron_lock:
        if generation is not None and generation != cron_generation:
            return
        cron_info["active"] = False
        cron_info["message"] = message


def _cron_loop(
    provider: str,
    ids: list[str],
    batch_size: int,
    start_at: float,
    interval: int,
    cancel: threading.Event,
    generation: int,
) -> None:
    try:
        while time.time() < start_at:
            if not _cron_live(generation):
                return
            if cancel.is_set():
                _cron_finish("Stopped", generation)
                return
            time.sleep(min(0.5, max(0.0, start_at - time.time())))
        index = 0
        batch_no = 0
        total = len(ids)
        while index < total:
            if not _cron_live(generation):
                return
            if cancel.is_set():
                _cron_finish("Stopped", generation)
                return
            batch = ids[index:index + batch_size]
            index += len(batch)
            batch_no += 1
            _enqueue(provider, batch)
            with cron_lock:
                if generation != cron_generation:
                    return
                cron_info["started"] = index
                cron_info["message"] = f"Batch {batch_no} started ({index}/{total})"
            if index >= total:
                break
            wake = time.time() + max(0, interval)
            while time.time() < wake:
                if not _cron_live(generation):
                    return
                if cancel.is_set():
                    _cron_finish("Stopped", generation)
                    return
                time.sleep(0.5)
        _cron_finish(f"Finished ({total}/{total} started)", generation)
    except Exception as exc:
        traceback.print_exc()
        _cron_finish(str(exc) or "Cronjob failed", generation)


def start_cron(provider: str, concurrent: int, target: int, start_at: float, interval: int) -> dict:
    if target < 1:
        raise RuntimeError("Set Ziel Erfolge above 0")
    if concurrent < 1 or concurrent > 20:
        raise RuntimeError("Gleichzeitig must be between 1 and 20")
    if interval < 0:
        raise ValueError("Interval must be 0 or more seconds")
    with cron_lock:
        if cron_info["active"]:
            raise RuntimeError("A cronjob is already running")
        generation = cron_generation
        cron_info["active"] = True
        cron_info["started"] = 0
        cron_info["total"] = 0
        cron_info["message"] = "Waiting for the start time"
    try:
        with state_lock:
            chosen = select_cron_rows(state["rows"], target)
            needs_sms = _needs_sms(chosen)
            ids = [str(row.get("id") or "") for row in chosen if row.get("id")]
        if not _cron_live(generation):
            return public_state()
        if not ids:
            raise RuntimeError("No profiles are available to start")
        if needs_sms:
            if provider not in PROVIDERS:
                raise RuntimeError("Choose a number service")
            if not provider_up(provider):
                raise RuntimeError(f"{PROVIDERS[provider]['label']} is not running")
        with state_lock:
            state["max_concurrent"] = concurrent
        cancel = threading.Event()
        global cron_job_cancel
        with cron_lock:
            if generation != cron_generation:
                return public_state()
            cron_job_cancel = cancel
            cron_info["total"] = len(ids)
        threading.Thread(
            target=_cron_loop,
            args=(provider, ids, concurrent, start_at, interval, cancel, generation),
            name="studio-cron",
            daemon=True,
        ).start()
    except Exception:
        _cron_finish("", generation)
        raise
    return public_state()


def reset_cron() -> dict:
    """Drop any schedule. Opening the window starts with no cronjob."""
    global cron_generation
    with cron_lock:
        cron_generation += 1
        cancel = cron_job_cancel
        cron_info["active"] = False
        cron_info["message"] = ""
        cron_info["started"] = 0
        cron_info["total"] = 0
    if cancel is not None:
        cancel.set()
    return public_state()


def stop_cron() -> dict:
    with cron_lock:
        cancel = cron_job_cancel
        if cron_info["active"]:
            cron_info["message"] = "Stopping"
    if cancel is not None:
        cancel.set()
    return public_state()


def remove_rows(ids: list[str]) -> dict:
    wanted = {str(item) for item in ids if str(item).strip()}
    if not wanted:
        raise RuntimeError("Select at least one row")
    with state_lock:
        kept: list[dict] = []
        removed = 0
        for row in state["rows"]:
            row_id = str(row.get("id") or "")
            if row_id not in wanted:
                kept.append(row)
                continue
            if row.get("status") in {"queued", "running"}:
                raise RuntimeError("Cancel running rows before removing them")
            removed += 1
            console(row, "Removed")
        if not removed:
            raise RuntimeError("Select a row that is not running")
        state["rows"] = kept
        save_store()
    return public_state()


def read_accounts_file() -> list[dict]:
    if not ACCOUNTS_FILE.is_file():
        return []
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read accounts.json, so nothing was deleted") from exc
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise RuntimeError("Could not read accounts.json, so nothing was deleted")
    return [item for item in raw if isinstance(item, dict)]


def _row_matches_deleted_history(row: dict, removed_ids: set[str], removed_emails: set[str]) -> bool:
    if row.get("status") in {"queued", "running"}:
        return False
    row_id = str(row.get("id") or "").strip()
    if row_id and row_id in removed_ids:
        return True
    for key in ("email", "requested_email", "service_email"):
        value = str(row.get(key) or "").strip().lower()
        if "@" in value and value in removed_emails:
            return True
    return False


def delete_history(emails: list[str]) -> dict:
    wanted = {str(item).strip().lower() for item in emails if "@" in str(item)}
    if not wanted:
        raise RuntimeError("Select at least one mailbox")
    with state_lock:
        kept_history: list[dict] = []
        removed_ids: set[str] = set()
        removed = 0
        for item in state["history"]:
            email = str(item.get("email") or "").strip().lower()
            if email and email in wanted:
                removed += 1
                row_id = str(item.get("id") or "").strip()
                if row_id:
                    removed_ids.add(row_id)
                continue
            kept_history.append(item)
        if not removed:
            raise RuntimeError("Select a saved mailbox")
        kept_rows: list[dict] = []
        for row in state["rows"]:
            if _row_matches_deleted_history(row, removed_ids, wanted):
                continue
            kept_rows.append(row)
        accounts = [
            item
            for item in read_accounts_file()
            if str(item.get("email") or "").strip().lower() not in wanted
        ]
        previous_history = state["history"]
        previous_rows = state["rows"]
        state["history"] = kept_history
        state["rows"] = kept_rows
        save_store()
        if ACCOUNTS_FILE.is_file():
            try:
                write_text_atomic(ACCOUNTS_FILE, json.dumps(accounts, indent=2))
            except OSError:
                state["history"] = previous_history
                state["rows"] = previous_rows
                save_store()
                raise
    return public_state()


def enable_pop3(row_id: str) -> dict:
    with state_lock:
        row = next((item for item in state["rows"] if item.get("id") == row_id), None)
        if row is None:
            raise ValueError("Row not found")
        if row.get("status") != "pop3":
            raise RuntimeError("POP3/IMAP can be enabled after that step fails")
        email = str(row.get("email") or "")
        if "@" not in email or not row.get("password"):
            raise RuntimeError("This row has no mailbox to enable")
        row["status"] = "queued"
        row["task"] = "pop3"
        row["cancel"] = False
        row["detail"] = "Waiting to enable POP3/IMAP"
        state["running"] = True
        save_store()
        console(row, "Queued POP3/IMAP")
        ensure_worker()
    return public_state()


def cancel_row(row_id: str) -> dict:
    with state_lock:
        row = next((item for item in state["rows"] if item.get("id") == row_id), None)
        if row is None:
            raise ValueError("Row not found")
        status = row.get("status")
        if status == "queued":
            row["status"] = "ready"
            row["cancel"] = False
            row["detail"] = "Cancelled"
            save_store()
            console(row, "Cancelled")
        elif status == "running":
            row["cancel"] = True
            row["detail"] = "Cancelling"
            save_store()
            console(row, "Cancelling")
        else:
            raise RuntimeError("This row is not running")
    return public_state()


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mailbox studio</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #101010;
    --surface: #171717;
    --raised: #222222;
    --ink: #f4f4f4;
    --muted: #a3a3a3;
    --faint: #6e6e6e;
    --line: #2e2e2e;
    --accent: #f4f4f4;
    --accent-press: #d8d8d8;
    --on-accent: #111111;
    --ok: #9dffc3;
    --ok-mark: #2fce74;
    --bad: #ffb4b4;
    --bad-mark: #ff5a5a;
    --warn: #ffe1a8;
    --warn-mark: #e0a84a;
  }
  * { box-sizing: border-box; border-radius: 0; }
  button, input, select, textarea { box-shadow: none; }
  html, body { height: 100%; }
  body {
    margin: 0;
    color: var(--ink);
    background: var(--bg);
    font: 13px/1.45 "Segoe UI", system-ui, sans-serif;
    display: flex;
    flex-direction: column;
  }
  header {
    height: 52px;
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    background: var(--bg);
    border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .mark { width: 8px; height: 8px; background: var(--accent); }
  h1 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em; }
  .brand span { color: var(--muted); font-size: 12px; }
  .switch { display: inline-flex; background: var(--surface); border: 1px solid var(--line); }
  .switch button { border: 0; border-left: 1px solid var(--line); background: transparent; color: var(--muted); padding: 6px 12px; font: inherit; cursor: pointer; }
  .switch button:first-child { border-left: 0; }
  .switch button.active { background: var(--ink); color: var(--bg); }
  .header-side { display: flex; align-items: center; gap: 12px; }
  main { flex: 1; min-height: 0; display: flex; padding: 16px 20px 20px; }
  .panel {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--line);
    overflow: hidden;
  }
  .bar {
    flex: none;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    background: var(--bg);
  }
  .fields { display: flex; gap: 14px; }
  label { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; }
  select, input[type="search"], input[type="date"] {
    height: 32px;
    border: 1px solid var(--line);
    padding: 0 10px;
    background: var(--raised);
    color: var(--ink);
    font: inherit;
    color-scheme: dark;
  }
  select { min-width: 128px; }
  select:focus, input:focus { outline: none; border-color: var(--muted); }
  select:disabled, input:disabled { color: var(--faint); }
  .stats { display: flex; gap: 14px; min-height: 16px; color: var(--muted); font-size: 12px; }
  .stats b { color: var(--ink); font-weight: 600; }
  input[type="checkbox"] {
    appearance: none;
    width: 14px;
    height: 14px;
    margin: 0;
    border: 1px solid var(--line);
    background: var(--bg);
    display: inline-grid;
    place-content: center;
  }
  input[type="checkbox"]:checked { background: var(--ink); border-color: var(--ink); }
  input[type="checkbox"]:checked::after {
    content: "";
    width: 7px;
    height: 3px;
    border-left: 2px solid var(--bg);
    border-bottom: 2px solid var(--bg);
    transform: rotate(-45deg) translate(0, -1px);
  }
  .reuse input { width: 14px; height: 14px; }
  .spacer { flex: 1; }
  .actions { display: flex; gap: 8px; }
  .btn {
    height: 32px;
    padding: 0 12px;
    border: 1px solid var(--line);
    background: var(--raised);
    color: var(--ink);
    font: inherit;
    cursor: pointer;
  }
  .btn:hover { background: #2a2a2a; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
  .btn.primary:hover { background: var(--accent-press); }
  .btn.quiet { border-color: transparent; background: transparent; color: var(--muted); }
  .btn.quiet:hover { background: var(--raised); color: var(--ink); }
  .btn:disabled { color: var(--faint); cursor: default; }
  .btn:disabled:hover { background: var(--raised); }
  .btn.primary:disabled, .btn.primary:disabled:hover { background: #2a2a2a; border-color: #2a2a2a; color: #6a6a6a; }
  .stats .goal { color: var(--ok); }
  .stats .clock { color: var(--ink); }
  .grow { flex: 1; }
  .grow input { width: 220px; max-width: 100%; }
  .table-wrap { flex: 1; min-height: 0; overflow: auto; }
  .table-wrap::-webkit-scrollbar { width: 10px; height: 10px; }
  .table-wrap::-webkit-scrollbar-thumb { background: #3a3a3a; }
  .table-wrap::-webkit-scrollbar-track { background: transparent; }
  table { width: 100%; min-width: 980px; border-collapse: collapse; }
  th {
    position: sticky;
    top: 0;
    z-index: 1;
    text-align: left;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--faint);
    padding: 9px 14px;
    background: #1c1c1c;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  td { padding: 11px 14px; border-bottom: 1px solid #262626; vertical-align: middle; white-space: nowrap; }
  tbody tr:hover td { background: #202020; }
  .who { font-weight: 600; }
  .sub { display: block; margin-top: 2px; color: var(--muted); font-weight: 400; font-size: 12px; }
  .mono { font-family: Consolas, ui-monospace, monospace; font-size: 12px; color: #e6e6e6; }
  .detail { color: var(--muted); font-size: 12px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
  .blank { color: var(--faint); }
  .status { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); }
  .status i { width: 6px; height: 6px; background: var(--faint); }
  .status.running, .status.queued { color: var(--ink); }
  .status.running i, .status.queued i { background: var(--ink); }
  .status.done { color: var(--ok); }
  .status.done i { background: var(--ok-mark); }
  .status.failed { color: var(--bad); }
  .status.failed i { background: var(--bad-mark); }
  .status.pop3 { color: var(--warn); }
  .status.pop3 i { background: var(--warn-mark); }
  th.check, td.check { width: 42px; text-align: center; padding-left: 12px; padding-right: 8px; }
  .row-btn { height: 28px; padding: 0 10px; }
  .row-btn.cancel { background: transparent; border-color: var(--bad-mark); color: var(--bad); }
  .row-btn.cancel:hover { background: #2a1616; }
  .row-btn.enable { background: transparent; border-color: var(--warn-mark); color: var(--warn); }
  .row-btn.enable:hover { background: #2a2416; }
  .empty {
    flex: 1;
    display: grid;
    place-items: center;
    text-align: center;
    color: var(--muted);
    padding: 32px;
  }
  .empty strong { display: block; margin-bottom: 6px; color: var(--ink); font-size: 14px; font-weight: 600; }
  .empty code { color: #d4d4d4; font-family: Consolas, ui-monospace, monospace; font-size: 12px; }
  .dialog {
    position: fixed;
    inset: 0;
    z-index: 5;
    display: grid;
    place-items: center;
    background: rgba(0, 0, 0, 0.72);
  }
  .dialog-card {
    width: min(440px, calc(100vw - 32px));
    background: var(--surface);
    border: 1px solid var(--line);
    padding: 20px;
  }
  .dialog-card h2 { margin: 0 0 16px; font-size: 16px; font-weight: 600; }
  .cron-facts { display: grid; grid-template-columns: 1fr auto; gap: 8px 16px; margin: 0 0 16px; }
  .cron-facts dt { color: var(--muted); }
  .cron-facts dd { margin: 0; text-align: right; font-weight: 600; }
  .dialog-card label { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  .dialog-card .locked-value { width: 220px; text-align: right; color: var(--ink); }
  .dialog-card input {
    width: 220px;
    height: 32px;
    border: 1px solid var(--line);
    background: var(--raised);
    color: var(--ink);
    padding: 0 8px;
    font: inherit;
    color-scheme: dark;
  }
  .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  #cron-note { min-height: 18px; margin: 8px 0 0; color: var(--muted); font-size: 12px; }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="mark"></span>
    <h1>Mailbox</h1>
    <span>GMX · WEB.DE · Proton</span>
  </div>
  <div class="header-side">
    <button type="button" class="btn" id="cronjob">Cronjob</button>
    <button type="button" class="btn" id="remove-selected" title="Remove selected rows from the table">Remove</button>
    <button type="button" class="btn" id="reload">Reload</button>
    <div class="switch" role="tablist">
      <button type="button" id="tab-generate" class="active">Generate</button>
      <button type="button" id="tab-history">History</button>
    </div>
  </div>
</header>
<main>
  <section id="generate" class="panel">
    <div class="bar">
      <div class="fields">
        <label>Service
          <select id="provider"></select>
        </label>
        <label>Status
          <select id="status-filter">
            <option value="">All</option>
            <option value="ready">Ready</option>
            <option value="queued">Queued</option>
            <option value="running">Running</option>
            <option value="done">Done</option>
            <option value="failed">Signup failed</option>
            <option value="pop3">POP3 failed</option>
          </select>
        </label>
        <label class="reuse" title="After a code arrives, keep the number and use it again until it expires">
          <input id="reuse" type="checkbox">
          Reuse phone number
        </label>
      </div>
      <div class="stats" id="summary"></div>
      <div class="spacer"></div>
      <div class="actions">
        <label style="display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap">
          Gleichzeitig
          <input id="concurrent" type="number" min="1" max="20" value="1" title="Wie viele Browser-Profile gleichzeitig laufen. Empfehlung: 1–3." style="width:56px;height:32px;border:1px solid var(--line);padding:0 8px;background:var(--raised);color:var(--ink);font:inherit;color-scheme:dark">
        </label>
        <label style="display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap">
          Ziel&nbsp;Erfolge
          <input id="target" type="number" min="0" value="0" title="0 = kein Ziel. Zahl > 0: Wiederholt fehlgeschlagene Zeilen bis dieses Ziel erreicht ist (max. 24 h)." style="width:72px;height:32px;border:1px solid var(--line);padding:0 8px;background:var(--raised);color:var(--ink);font:inherit;color-scheme:dark">
        </label>
        <button type="button" class="btn quiet" id="clear">Clear</button>
        <button type="button" class="btn" id="import-file">Import</button>
        <button type="button" class="btn" id="export-selected" title="Save selected mailboxes as email;password" disabled>Export</button>
        <button type="button" class="btn primary" id="start-selected">Start selected</button>
      </div>
      <input id="file" type="file" accept=".txt,.csv,text/plain" class="hidden">
    </div>
    <div class="empty" id="queue-empty">
      <div>
        <strong>Nothing imported</strong>
        <span>One person per line · <code>email;first;last;day;month;year;plz;city;street;password</code></span>
        <span style="display:block">Proton · <code>name@proton.me;password</code></span>
      </div>
    </div>
    <div class="empty hidden" id="queue-filter-empty">
      <div><strong>No rows with this status</strong></div>
    </div>
    <div class="table-wrap hidden" id="queue-wrap">
      <table>
        <thead>
          <tr>
            <th class="check"><input id="pick-all" type="checkbox" aria-label="Select all"></th>
            <th>Person</th><th>Address</th><th>Status</th><th>Phone</th><th>Email</th><th>Password</th><th>Progress</th><th></th>
          </tr>
        </thead>
        <tbody id="queue"></tbody>
      </table>
    </div>
  </section>
  <section id="history" class="panel hidden">
    <div class="bar">
      <label>Created
        <input id="filter-date" type="date">
      </label>
      <label class="grow">Email or name
        <input id="filter-query" type="search" placeholder="Search">
      </label>
      <div class="actions">
        <button type="button" class="btn" id="export-history" title="Save selected mailboxes as email;password" disabled>Export</button>
        <button type="button" class="btn" id="delete-history" title="Delete selected mailboxes" disabled>Delete</button>
      </div>
    </div>
    <div class="empty" id="history-empty">No mailboxes match this search.</div>
    <div class="table-wrap hidden" id="history-wrap">
      <table>
        <thead>
          <tr>
            <th class="check"><input id="history-pick-all" type="checkbox" aria-label="Select all"></th>
            <th>Created</th><th>Email</th><th>Password</th><th>Name</th><th>Service</th><th>Phone</th>
          </tr>
        </thead>
        <tbody id="history-body"></tbody>
      </table>
    </div>
  </section>
</main>
</section>
<div id="cron-dialog" class="dialog hidden">
  <div class="dialog-card" role="dialog" aria-labelledby="cron-title">
    <h2 id="cron-title">Cronjob</h2>
    <dl class="cron-facts">
      <dt>Gleichzeitig</dt><dd id="cron-concurrent">1</dd>
      <dt>Ziel Erfolge</dt><dd id="cron-target">0</dd>
      <dt>Available</dt><dd id="cron-available">0</dd>
    </dl>
    <label>Start time <input id="cron-start-time" type="datetime-local"></label>
    <label>Interval (seconds) <input id="cron-interval" type="number" min="0" step="1" value="60"></label>
    <label>End time <span id="cron-end" class="locked-value">—</span></label>
    <p id="cron-note"></p>
    <div class="dialog-actions">
      <button type="button" class="btn" id="cron-close">Close</button>
      <button type="button" class="btn hidden" id="cron-stop">Stop</button>
      <button type="button" class="btn primary" id="cron-go">Start</button>
    </div>
  </div>
</div>
<script>
const provider = document.querySelector("#provider");
const reuse = document.querySelector("#reuse");
const queueBody = document.querySelector("#queue");
const historyBody = document.querySelector("#history-body");
let latest = {rows: [], history: [], running: false};
let viewSeq = 0;
const selected = new Set();
const historySelected = new Set();

function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

const STATUS_LABEL = {
  ready: "Ready",
  queued: "Queued",
  running: "Running",
  done: "Done",
  failed: "Signup failed",
  pop3: "POP3 failed",
};

function pill(status) {
  const key = status || "ready";
  const label = STATUS_LABEL[key] || key;
  return `<span class="status ${esc(key)}"><i></i>${esc(label)}</span>`;
}

function textCell(value, cls) {
  const text = String(value ?? "").trim();
  if (!text) return `<td class="blank">—</td>`;
  return `<td class="${cls}" title="${esc(text)}">${esc(text)}</td>`;
}

function emailCell(row) {
  const text = String(row.email || "").trim();
  if (!text) return `<td class="blank">—</td>`;
  const note = row.suggested ? `<span class="sub">suggested</span>` : "";
  const title = row.suggested && row.requested_email ? `${text} (requested ${row.requested_email})` : text;
  return `<td class="mono" title="${esc(title)}">${esc(text)}${note}</td>`;
}

function canStart(row) {
  return row.status === "ready" || row.status === "failed";
}

function canRemove(row) {
  return row.status !== "queued" && row.status !== "running";
}

function rowAction(row) {
  if (row.status === "queued" || row.status === "running") {
    return `<button type="button" class="btn row-btn cancel" data-act="cancel" data-id="${esc(row.id)}">Cancel</button>`;
  }
  if (row.status === "pop3") {
    return `<button type="button" class="btn row-btn enable" data-act="enable" data-id="${esc(row.id)}">Enable POP3/IMAP</button>`;
  }
  if (row.status === "done") return "";
  return `<button type="button" class="btn row-btn" data-act="start" data-id="${esc(row.id)}">Start</button>`;
}

function visibleRows() {
  const all = latest.rows || [];
  const filter = document.querySelector("#status-filter").value;
  return filter ? all.filter((row) => (row.status || "ready") === filter) : all;
}

function renderQueue() {
  const all = latest.rows || [];
  const rows = visibleRows();
  const known = new Set(all.map((row) => row.id));
  for (const id of [...selected]) if (!known.has(id)) selected.delete(id);
  queueBody.innerHTML = rows.map((row) => {
    const birth = [row.day, row.month, row.year].filter(Boolean).join(".");
    const place = [row.plz, row.city].filter(Boolean).join(" ");
    const pickable = canRemove(row);
    const kind = row.kind === "webde" ? "WEB.DE" : (row.kind === "gmx" ? "GMX" : (row.kind === "proton" ? "Proton" : ""));
    return `<tr>
      <td class="check"><input class="pick" type="checkbox" value="${esc(row.id)}" ${selected.has(row.id) ? "checked" : ""} ${pickable ? "" : "disabled"}></td>
      <td><span class="who">${esc(row.first)} ${esc(row.last)}</span><span class="sub">${esc(birth)}${kind ? " · " + esc(kind) : ""}</span></td>
      <td title="${esc(row.street)}"><span class="line">${esc(row.street)}</span><span class="sub">${esc(place)}</span></td>
      <td>${pill(row.status)}</td>
      ${textCell(row.phone, "mono")}
      ${emailCell(row)}
      ${textCell(row.password, "mono")}
      ${textCell(row.detail, "detail")}
      <td>${rowAction(row)}</td>
    </tr>`;
  }).join("");
  document.querySelector("#queue-empty").classList.toggle("hidden", all.length > 0);
  document.querySelector("#queue-filter-empty").classList.toggle("hidden", all.length === 0 || rows.length > 0);
  document.querySelector("#queue-wrap").classList.toggle("hidden", rows.length === 0);
  const counts = all.reduce((sum, row) => { sum[row.status] = (sum[row.status] || 0) + 1; return sum; }, {});
  const bits = ["ready", "queued", "running", "done", "failed", "pop3"]
    .filter((key) => counts[key])
    .map((key) => `<span><b>${counts[key]}</b> ${esc(STATUS_LABEL[key] || key)}</span>`);
  if (latest.reuse_numbers) bits.push(`<span><b>${Number(latest.live_count) || 0}</b> live</span>`);
  const target = Number(latest.target_success) || 0;
  if (target > 0) {
    const done = Number(latest.done_count) || 0;
    const tl = Number(latest.time_left) || 0;
    const h = Math.floor(tl / 3600);
    const m = Math.floor((tl % 3600) / 60);
    const timeStr = tl > 0 ? `${h}h ${m}m übrig` : "Zeit abgelaufen";
    bits.push(`<span class="goal"><b>${done}</b>/${target} Ziel</span>`);
    bits.push(`<span class="clock">${timeStr}</span>`);
  }
  document.querySelector("#summary").innerHTML = bits.join("");
  const startable = all.filter((row) => canStart(row) && selected.has(row.id));
  document.querySelector("#start-selected").disabled = startable.length === 0;
  const removable = all.filter((row) => canRemove(row) && selected.has(row.id));
  document.querySelector("#remove-selected").disabled = removable.length === 0;
  const exportable = all.filter((row) => selected.has(row.id) && String(row.email || "").includes("@"));
  document.querySelector("#export-selected").disabled = exportable.length === 0;
  const pickable = rows.filter(canRemove);
  const pickAll = document.querySelector("#pick-all");
  pickAll.checked = pickable.length > 0 && pickable.every((row) => selected.has(row.id));
  pickAll.indeterminate = !pickAll.checked && pickable.some((row) => selected.has(row.id));
  pickAll.disabled = pickable.length === 0;
  const busy = all.some((row) => row.status === "queued" || row.status === "running");
  document.querySelector("#provider").disabled = busy;
}

function historyKey(item) {
  return String(item.email || "").trim().toLowerCase();
}

function filteredHistory() {
  const day = document.querySelector("#filter-date").value;
  const query = document.querySelector("#filter-query").value.trim().toLowerCase();
  return (latest.history || []).filter((item) => {
    const created = String(item.created_at || "");
    if (day && !created.startsWith(day)) return false;
    if (!query) return true;
    return `${item.email} ${item.name}`.toLowerCase().includes(query);
  });
}

function mailboxLines(rows) {
  return rows.map((item) => `${item.email || ""};${item.password || ""}`).join("\n") + "\n";
}

async function saveMailboxFile(text) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.export_mailboxes) {
    await window.pywebview.api.export_mailboxes(text);
    return;
  }
  const blob = new Blob([text], {type: "text/plain;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "mailboxes.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderHistory() {
  const rows = filteredHistory();
  const known = new Set((latest.history || []).map(historyKey).filter((key) => key.includes("@")));
  for (const key of [...historySelected]) if (!known.has(key)) historySelected.delete(key);
  historyBody.innerHTML = rows.map((item) => {
    const key = historyKey(item);
    const pickable = key.includes("@");
    return `<tr>
      <td class="check"><input class="history-pick" type="checkbox" value="${esc(key)}" ${historySelected.has(key) ? "checked" : ""} ${pickable ? "" : "disabled"}></td>
      <td>${esc(item.created_at)}</td>
      ${emailCell(item)}
      ${textCell(item.password, "mono")}
      <td>${esc(item.name)}</td>
      <td>${esc(item.provider)}<span class="sub">${esc(item.kind)}</span></td>
      ${textCell(item.phone, "mono")}
    </tr>`;
  }).join("");
  document.querySelector("#history-empty").classList.toggle("hidden", rows.length > 0);
  document.querySelector("#history-wrap").classList.toggle("hidden", rows.length === 0);
  document.querySelector("#export-history").disabled = historySelected.size === 0;
  document.querySelector("#delete-history").disabled = historySelected.size === 0;
  const pickable = rows.filter((item) => historyKey(item).includes("@"));
  const pickAll = document.querySelector("#history-pick-all");
  pickAll.checked = pickable.length > 0 && pickable.every((item) => historySelected.has(historyKey(item)));
  pickAll.indeterminate = !pickAll.checked && pickable.some((item) => historySelected.has(historyKey(item)));
  pickAll.disabled = pickable.length === 0;
}

function syncProviders() {
  const options = latest.providers || [];
  const signature = options.map((item) => item.id).join(",");
  if (provider.dataset.signature === signature) return;
  const current = provider.value || latest.provider || "";
  provider.dataset.signature = signature;
  provider.innerHTML = options.map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
  if (options.some((item) => item.id === current)) provider.value = current;
  else if (options.length) provider.value = options[0].id;
}

function localStamp(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function cronSchedule() {
  const batch = Math.max(1, parseInt(concurrent.value, 10) || 1);
  const target = Math.max(0, parseInt(document.querySelector("#target").value, 10) || 0);
  const startable = (latest.rows || []).filter((row) => row.status === "ready" || row.status === "failed").length;
  const count = Math.min(target, startable);
  const interval = Math.max(0, parseInt(document.querySelector("#cron-interval").value, 10) || 0);
  const batches = count > 0 ? Math.ceil(count / batch) : 0;
  return {batch, target, count, interval, batches};
}

function fillCronFacts() {
  const plan = cronSchedule();
  document.querySelector("#cron-concurrent").textContent = String(plan.batch);
  document.querySelector("#cron-target").textContent = String(plan.target);
  const available = (latest.rows || []).filter((row) => row.status !== "done").length;
  document.querySelector("#cron-available").textContent = String(available);
  const startValue = document.querySelector("#cron-start-time").value;
  const start = startValue ? new Date(startValue) : null;
  if (plan.target > 0 && plan.count > 0 && start && !Number.isNaN(start.getTime())) {
    const end = new Date(start.getTime() + Math.max(0, plan.batches - 1) * plan.interval * 1000);
    document.querySelector("#cron-end").textContent = localStamp(end).replace("T", " ");
  } else {
    document.querySelector("#cron-end").textContent = "—";
  }
  const cron = latest.cron || {};
  const note = cron.message || (plan.target < 1 ? "Set Ziel Erfolge above 0" : "");
  document.querySelector("#cron-note").textContent = note;
  document.querySelector("#cron-stop").classList.toggle("hidden", !cron.active);
  document.querySelector("#cron-go").disabled = !!cron.active || plan.target < 1;
}

function applyState(payload) {
  latest = payload;
  syncProviders();
  if (document.activeElement !== reuse) reuse.checked = !!latest.reuse_numbers;
  if (document.activeElement !== concurrent) concurrent.value = Number(latest.max_concurrent) || 1;
  renderQueue();
  renderHistory();
  if (!document.querySelector("#cron-dialog").classList.contains("hidden")) fillCronFacts();
}

async function refresh() {
  const seq = ++viewSeq;
  const response = await fetch("/api/state");
  const payload = await response.json();
  if (seq !== viewSeq) return;
  applyState(payload);
}

async function send(path, body) {
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

document.querySelector("#tab-generate").onclick = () => {
  document.querySelector("#generate").classList.remove("hidden");
  document.querySelector("#history").classList.add("hidden");
  document.querySelector("#tab-generate").classList.add("active");
  document.querySelector("#tab-history").classList.remove("active");
};
document.querySelector("#tab-history").onclick = () => {
  document.querySelector("#history").classList.remove("hidden");
  document.querySelector("#generate").classList.add("hidden");
  document.querySelector("#tab-history").classList.add("active");
  document.querySelector("#tab-generate").classList.remove("active");
};
async function act(path, body) {
  try {
    await send(path, body);
    await refresh();
  } catch (error) {
    alert(error.message);
  }
}
document.querySelector("#import-file").onclick = () => document.querySelector("#file").click();
reuse.onchange = () => act("/api/reuse", {enabled: reuse.checked});
const concurrent = document.querySelector("#concurrent");
concurrent.onchange = () => act("/api/concurrent", {value: parseInt(concurrent.value, 10) || 1});
document.querySelector("#file").onchange = async (event) => {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  await act("/api/import", {text: await file.text()});
};
document.querySelector("#clear").onclick = () => act("/api/clear", {});
document.querySelector("#remove-selected").onclick = () => {
  const ids = (latest.rows || []).filter((row) => canRemove(row) && selected.has(row.id)).map((row) => row.id);
  if (!ids.length) {
    alert("Select at least one row");
    return;
  }
  ids.forEach((id) => selected.delete(id));
  act("/api/remove", {ids});
};
document.querySelector("#reload").onclick = async () => {
  const button = document.querySelector("#reload");
  button.disabled = true;
  try {
    const payload = await send("/api/reload", {});
    viewSeq += 1;
    applyState(payload);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
};
document.querySelector("#export-selected").onclick = async () => {
  const rows = (latest.rows || []).filter((row) => selected.has(row.id) && String(row.email || "").includes("@"));
  if (!rows.length) {
    alert("Select at least one mailbox");
    return;
  }
  try {
    await saveMailboxFile(mailboxLines(rows));
  } catch (error) {
    alert(error.message || "Could not export");
  }
};
document.querySelector("#start-selected").onclick = () => {
  const ids = (latest.rows || []).filter((row) => canStart(row) && selected.has(row.id)).map((row) => row.id);
  ids.forEach((id) => selected.delete(id));
  const target = Math.max(0, parseInt(document.querySelector("#target").value, 10) || 0);
  act("/api/start", {provider: provider.value, ids, target});
};
document.querySelector("#history-pick-all").onchange = (event) => {
  filteredHistory().forEach((item) => {
    const key = historyKey(item);
    if (!key.includes("@")) return;
    if (event.target.checked) historySelected.add(key);
    else historySelected.delete(key);
  });
  renderHistory();
};
historyBody.addEventListener("change", (event) => {
  const box = event.target.closest("input.history-pick");
  if (!box || !box.value) return;
  if (box.checked) historySelected.add(box.value);
  else historySelected.delete(box.value);
  renderHistory();
});
document.querySelector("#export-history").onclick = async () => {
  const rows = (latest.history || []).filter((item) => historySelected.has(historyKey(item)));
  if (!rows.length) {
    alert("Select at least one mailbox");
    return;
  }
  try {
    await saveMailboxFile(mailboxLines(rows));
  } catch (error) {
    alert(error.message || "Could not export");
  }
};
document.querySelector("#delete-history").onclick = async () => {
  const emails = [...historySelected];
  if (!emails.length) {
    alert("Select at least one mailbox");
    return;
  }
  const label = emails.length === 1 ? "this mailbox" : `${emails.length} mailboxes`;
  if (!confirm(`Delete ${label}?`)) return;
  try {
    const payload = await send("/api/history/delete", {emails});
    emails.forEach((email) => historySelected.delete(email));
    viewSeq += 1;
    applyState(payload);
  } catch (error) {
    alert(error.message);
  }
};
document.querySelector("#pick-all").onchange = (event) => {
  visibleRows().filter(canRemove).forEach((row) => {
    if (event.target.checked) selected.add(row.id);
    else selected.delete(row.id);
  });
  renderQueue();
};
queueBody.addEventListener("change", (event) => {
  const box = event.target.closest("input.pick");
  if (!box) return;
  if (box.checked) selected.add(box.value);
  else selected.delete(box.value);
  renderQueue();
});
queueBody.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  if (button.dataset.act === "start") {
    const target = Math.max(0, parseInt(document.querySelector("#target").value, 10) || 0);
    act("/api/start", {provider: provider.value, ids: [button.dataset.id], target});
    return;
  }
  if (button.dataset.act === "enable") {
    act("/api/enable", {id: button.dataset.id});
    return;
  }
  if (button.dataset.act === "cancel") act("/api/cancel", {id: button.dataset.id});
});
document.querySelector("#filter-date").oninput = renderHistory;
document.querySelector("#filter-query").oninput = renderHistory;
document.querySelector("#status-filter").onchange = renderQueue;
document.querySelector("#cronjob").onclick = () => {
  const start = document.querySelector("#cron-start-time");
  if (!start.value) start.value = localStamp(new Date());
  document.querySelector("#cron-dialog").classList.remove("hidden");
  fillCronFacts();
};
document.querySelector("#cron-close").onclick = () => {
  document.querySelector("#cron-dialog").classList.add("hidden");
};
document.querySelector("#cron-start-time").oninput = fillCronFacts;
document.querySelector("#cron-interval").oninput = fillCronFacts;
document.querySelector("#target").oninput = fillCronFacts;
document.querySelector("#concurrent").oninput = fillCronFacts;
document.querySelector("#cron-go").onclick = async () => {
  const plan = cronSchedule();
  if (plan.target < 1) {
    alert("Set Ziel Erfolge above 0");
    return;
  }
  try {
    const payload = await send("/api/cron/start", {
      provider: provider.value,
      concurrent: plan.batch,
      target: plan.target,
      start: document.querySelector("#cron-start-time").value,
      interval: plan.interval,
    });
    viewSeq += 1;
    applyState(payload);
  } catch (error) {
    alert(error.message);
  }
};
document.querySelector("#cron-stop").onclick = async () => {
  try {
    const payload = await send("/api/cron/stop", {});
    viewSeq += 1;
    applyState(payload);
  } catch (error) {
    alert(error.message);
  }
};
async function openStudio() {
  try {
    const payload = await send("/api/cron/reset", {});
    viewSeq += 1;
    applyState(payload);
  } catch (error) {
    refresh();
  }
}
openStudio();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[studio] {fmt % args}", flush=True)

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/state":
                self._send(public_state(), 200)
                return
            self._send({"ok": False, "error": "not found"}, 404)
        except Exception:
            traceback.print_exc()
            try:
                self._send({"ok": False, "error": "studio error"}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            body = self._read()
            if path == "/api/import":
                rows = parse_rows(str(body.get("text") or ""))
                if not rows:
                    raise ValueError("No rows found. Split each line with semicolons.")
                with state_lock:
                    state["rows"].extend(rows)
                    save_store()
                self._send(public_state(), 200)
                return
            if path == "/api/start":
                raw_ids = body.get("ids") or []
                if not isinstance(raw_ids, list):
                    raise ValueError("ids must be a list")
                raw_target = body.get("target") or 0
                try:
                    target_val = max(0, int(raw_target))
                except (TypeError, ValueError):
                    target_val = 0
                self._send(start_rows(str(body.get("provider") or ""), [str(item) for item in raw_ids], target=target_val), 200)
                return
            if path == "/api/enable":
                self._send(enable_pop3(str(body.get("id") or "")), 200)
                return
            if path == "/api/cancel":
                self._send(cancel_row(str(body.get("id") or "")), 200)
                return
            if path == "/api/clear":
                with state_lock:
                    if state["running"]:
                        raise RuntimeError("Wait until the current batch finishes")
                    state["rows"] = [row for row in state["rows"] if row.get("status") == "done"]
                    save_store()
                self._send(public_state(), 200)
                return
            if path == "/api/remove":
                raw_ids = body.get("ids") or []
                if not isinstance(raw_ids, list):
                    raise ValueError("ids must be a list")
                self._send(remove_rows([str(item) for item in raw_ids]), 200)
                return
            if path == "/api/history/delete":
                raw_emails = body.get("emails") or []
                if not isinstance(raw_emails, list):
                    raise ValueError("emails must be a list")
                self._send(delete_history([str(item) for item in raw_emails]), 200)
                return
            if path == "/api/reload":
                reload_store()
                self._send(public_state(), 200)
                return
            if path == "/api/reuse":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false")
                live_numbers.set_enabled(enabled)
                print(f"Reuse phone number {'on' if enabled else 'off'}", flush=True)
                self._send(public_state(), 200)
                return
            if path == "/api/concurrent":
                try:
                    val = max(1, min(20, int(body.get("value") or 1)))
                except (TypeError, ValueError):
                    raise ValueError("value must be a number between 1 and 20")
                with state_lock:
                    state["max_concurrent"] = val
                print(f"Max. gleichzeitig: {val}", flush=True)
                self._send(public_state(), 200)
                return
            if path == "/api/cron/start":
                self._send(
                    start_cron(
                        str(body.get("provider") or ""),
                        int(body.get("concurrent") or 0),
                        int(body.get("target") or 0),
                        _parse_local_time(str(body.get("start") or "")),
                        int(body.get("interval") or 0),
                    ),
                    200,
                )
                return
            if path == "/api/cron/stop":
                self._send(stop_cron(), 200)
                return
            if path == "/api/cron/reset":
                self._send(reset_cron(), 200)
                return
            self._send({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            self._send({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            traceback.print_exc()
            self._send({"ok": False, "error": str(exc) or "studio error"}, 500)

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 2_000_000:
            raise ValueError("invalid request body")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send(self, payload: dict, status: int) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StudioServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


class WindowApi:
    def export_mailboxes(self, text: str) -> str:
        import webview

        body = text if isinstance(text, str) else ""
        if not body.strip():
            return ""
        if not webview.windows:
            raise RuntimeError("The Mailbox window is not open")
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.SAVE if dialog is not None else webview.SAVE_DIALOG
        chosen = webview.windows[0].create_file_dialog(
            kind,
            save_filename="mailboxes.txt",
            file_types=("Text files (*.txt)", "All files (*.*)"),
        )
        if not chosen:
            return ""
        path = Path(chosen if isinstance(chosen, str) else chosen[0])
        if path.suffix == "":
            path = path.with_suffix(".txt")
        path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        print(f"Exported mailboxes to {path}", flush=True)
        return "saved"


def main() -> None:
    load_store()
    reset_cron()
    if port_in_use(HOST, PORT):
        raise SystemExit(
            f"Mailbox is already open on port {PORT}. Close the other Mailbox window, then run start-ui.bat again."
        )
    try:
        server = StudioServer((HOST, PORT), Handler)
    except OSError as exc:
        busy = getattr(exc, "winerror", None) in {10013, 10048} or exc.errno in {13, 48, 98}
        if busy:
            raise SystemExit(
                f"Mailbox is already open on port {PORT}. Close the other Mailbox window, then run start-ui.bat again."
            ) from exc
        raise
    threading.Thread(target=server.serve_forever, name="studio-http", daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"Mailbox window at {url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
    except Exception as exc:
        server.shutdown()
        server.server_close()
        raise SystemExit(f"Mailbox HTTP server did not start on port {PORT}: {exc}") from exc
    try:
        import webview
    except ImportError as exc:
        server.shutdown()
        server.server_close()
        raise SystemExit("Install the window library with: pip install pywebview") from exc
    webview.create_window(
        "Mailbox",
        url,
        width=1280,
        height=820,
        min_size=(980, 640),
        maximized=True,
        background_color="#101010",
        text_select=True,
        js_api=WindowApi(),
    )
    try:
        webview.start()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

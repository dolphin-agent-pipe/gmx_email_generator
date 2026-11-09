"""Mailbox studio.

Import signup rows, run them through one number service, and keep the
finished mailboxes.

    python studio.py

That opens the desktop window. Start the chosen number service first
(sms_code_service, hero_sms_service, or anosim_service). Only one provider
is used for a batch.

Each imported line is split on semicolons:

    first;last;day;month;year;plz;city;street;password
    first;last;day;month;year;plz;city;street;password;country
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STORE_FILE = ROOT / "studio_queue.json"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
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
COLUMNS = ("first", "last", "day", "month", "year", "plz", "city", "street", "password")

state_lock = threading.Lock()
order_lock = threading.Lock()
state: dict = {"rows": [], "history": [], "running": False, "provider": "smscode", "kind": "gmx"}
worker: threading.Thread | None = None
inflight = 0


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def load_store() -> None:
    if not STORE_FILE.is_file():
        return
    try:
        loaded = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    rows = loaded.get("rows")
    history = loaded.get("history")
    if isinstance(rows, list):
        state["rows"] = [item for item in rows if isinstance(item, dict)]
    if isinstance(history, list):
        state["history"] = [item for item in history if isinstance(item, dict)]
    for row in state["rows"]:
        if row.get("status") in {"running", "queued"}:
            row["status"] = "ready"
            row["cancel"] = False
            row["detail"] = "Interrupted. Start again."


def save_store() -> None:
    payload = {"rows": state["rows"], "history": state["history"]}
    STORE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def public_state() -> dict:
    with state_lock:
        return {
            "running": state["running"],
            "provider": state["provider"],
            "kind": state["kind"],
            "providers": [{"id": key, "label": item["label"]} for key, item in PROVIDERS.items()],
            "rows": list(state["rows"]),
            "history": list(state["history"]),
        }


def parse_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.split(";")]
        if parts and parts[0].lower() == "first":
            continue
        row = {key: "" for key in COLUMNS}
        row["country"] = "DE"
        for key, value in zip(COLUMNS, parts):
            row[key] = value
        if len(parts) >= 10 and parts[9]:
            row["country"] = parts[9].upper()
        row.update(
            {
                "id": uuid.uuid4().hex[:12],
                "status": "ready",
                "detail": "",
                "phone": "",
                "email": "",
                "created_at": "",
                "provider": "",
                "kind": "",
            }
        )
        missing = [key for key in COLUMNS if not row[key]]
        if missing:
            row["status"] = "failed"
            row["detail"] = "Missing " + ", ".join(missing)
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
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw[:240] or f"HTTP {exc.code}"}
    if not isinstance(payload, dict):
        raise RuntimeError("Number service returned an unexpected response")
    return payload


def provider_up(provider: str) -> bool:
    base = PROVIDERS[provider]["base"]
    try:
        request_json(base, "/health", timeout=3)
        return True
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
        return False


def phone_values(payload: dict) -> list[str]:
    found = []
    for item in payload.get("phones") or []:
        if isinstance(item, str):
            found.append(item.strip())
        elif isinstance(item, dict) and item.get("phone"):
            found.append(str(item["phone"]).strip())
    return [phone for phone in found if phone]


def service_email(row: dict, kind: str) -> str:
    local = re.sub(r"[^a-z0-9]+", ".", f"{row['first']}.{row['last']}".lower()).strip(".")
    local = local[:40] or row["id"]
    domain = "web.de" if kind == "webde" else "gmx.net"
    return f"{local}@{domain}"


class RowCancelled(Exception):
    pass


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


def finish_numbers(provider: str, email: str, row: dict) -> None:
    try:
        call_service(row, PROVIDERS[provider]["base"], "/code_passed", {"email": email}, 180)
    except RowCancelled:
        raise
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        set_detail_by_email(email, f"Could not close numbers: {exc}")


def set_detail_by_email(email: str, detail: str) -> None:
    with state_lock:
        for row in state["rows"]:
            if row.get("service_email") == email and row.get("status") == "running":
                row["detail"] = detail
        save_store()


def run_row(row: dict, provider: str, kind: str) -> None:
    from gmx_gen import NoPhonesLeft, SignupRestart, generate_mailbox

    email_key = service_email(row, kind)
    with state_lock:
        row["status"] = "running"
        row["provider"] = provider
        row["kind"] = kind
        row["service_email"] = email_key
        row["detail"] = "Ordering numbers"
        save_store()
    console(row, "Ordering numbers")
    ordered = False
    try:
        price_index = 0
        failed_batches = 0
        while True:
            ensure_active(row)
            with order_lock:
                ensure_active(row)
                started = call_service(
                    row,
                    PROVIDERS[provider]["base"],
                    "/get_started",
                    {"email": email_key, "type": kind, "count": 3, "price_index": price_index},
                    240,
                )
            phones = phone_values(started)[:3]
            if not started.get("ok") or not phones:
                error = str(started.get("error") or "The number service did not return a phone")
                lowered = error.lower()
                if price_index >= 20 or any(
                    phrase in lowered
                    for phrase in ("no cheaper price", "no germany numbers", "no price with stock", "numbers with stock remain")
                ):
                    raise RuntimeError("No cheaper phone price is left")
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
                payload = call_service(row, PROVIDERS[provider]["base"], "/get_code", {"phone": phone}, 180)
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
                    mailbox, used = generate_mailbox(
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
                    )
                    break
                except SignupRestart:
                    restarted = True
                    break
                except SystemExit as exc:
                    message = str(getattr(exc, "code", "") or exc).strip() or "Generation failed"
                    if browser_try == 2 or "phone" in message.lower():
                        raise
                    console(row, f"{message}. Opening the signup again.")
            if restarted:
                if ordered:
                    try:
                        finish_numbers(provider, email_key, row)
                    except RowCancelled:
                        raise
                    ordered = False
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
            row["phone"] = used or row.get("phone") or ""
            row["created_at"] = created
            row["detail"] = "Mailbox created"
            state["history"].insert(
                0,
                {
                    "id": row["id"],
                    "created_at": created,
                    "email": mailbox,
                    "password": row["password"],
                    "provider": PROVIDERS[provider]["label"],
                    "kind": "WEB.DE" if kind == "webde" else "GMX",
                    "name": f"{row['first']} {row['last']}",
                    "phone": row["phone"],
                },
            )
            save_store()
        console(row, f"Mailbox created: {mailbox}")
    except RowCancelled:
        console(row, "Cancelled")
        with state_lock:
            row["status"] = "ready"
            row["cancel"] = False
            row["detail"] = "Cancelled"
            save_store()
    except (NoPhonesLeft, SystemExit, OSError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
        message = str(getattr(exc, "code", "") or exc).strip() or "Generation failed"
        with state_lock:
            row["status"] = "failed"
            row["detail"] = message[:300]
            row["cancel"] = False
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
            except RowCancelled:
                pass


def finish_row_thread() -> None:
    global inflight
    with state_lock:
        inflight = max(0, inflight - 1)


def run_row_thread(row: dict) -> None:
    try:
        run_row(row, str(row.get("provider") or ""), str(row.get("kind") or "gmx"))
    except Exception:
        traceback.print_exc()
    finally:
        finish_row_thread()


def worker_loop() -> None:
    global inflight
    try:
        while True:
            with state_lock:
                row = next((item for item in state["rows"] if item.get("status") == "queued"), None)
                if row is None:
                    if inflight == 0:
                        state["running"] = False
                        save_store()
                        return
                elif row.get("cancel"):
                    row["status"] = "ready"
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
    except Exception:
        traceback.print_exc()
        with state_lock:
            state["running"] = False
            save_store()


def ensure_worker() -> None:
    global worker
    if worker is not None and worker.is_alive():
        return
    worker = threading.Thread(target=worker_loop, name="studio-batch", daemon=True)
    worker.start()


def start_rows(provider: str, kind: str, ids: list[str]) -> dict:
    if provider not in PROVIDERS:
        raise ValueError("Choose one number service")
    if kind not in {"gmx", "webde"}:
        raise ValueError("Mailbox type must be gmx or webde")
    wanted = {str(item) for item in ids if str(item).strip()}
    if not wanted:
        raise RuntimeError("Select at least one row")
    if not provider_up(provider):
        label = PROVIDERS[provider]["label"]
        base = PROVIDERS[provider]["base"]
        raise RuntimeError(f"{label} is not running at {base}")
    with state_lock:
        queued = 0
        for row in state["rows"]:
            if row.get("id") not in wanted or row.get("status") not in {"ready", "failed"}:
                continue
            row["status"] = "queued"
            row["cancel"] = False
            row["provider"] = provider
            row["kind"] = kind
            row["detail"] = "Waiting"
            queued += 1
            console(row, f"Queued on {PROVIDERS[provider]['label']}")
        if not queued:
            raise RuntimeError("Select a row that is waiting or failed")
        state["running"] = True
        state["provider"] = provider
        state["kind"] = kind
        save_store()
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
    --bg: #0c0e12;
    --surface: #14171d;
    --raised: #1c212a;
    --ink: #e8eaef;
    --muted: #8b93a1;
    --faint: #5c6573;
    --line: #2a303b;
    --accent: #4c84f5;
    --accent-press: #3b6fd4;
  }
  * { box-sizing: border-box; }
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
  .mark { width: 8px; height: 8px; border-radius: 2px; background: var(--accent); }
  h1 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em; }
  .brand span { color: var(--muted); font-size: 12px; }
  .switch { display: inline-flex; padding: 2px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
  .switch button { border: 0; background: transparent; color: var(--muted); border-radius: 6px; padding: 5px 12px; font: inherit; cursor: pointer; }
  .switch button.active { background: var(--raised); color: var(--ink); }
  main { flex: 1; min-height: 0; display: flex; padding: 16px 20px 20px; }
  .panel {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }
  .bar {
    flex: none;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    background: #12151b;
  }
  .fields { display: flex; gap: 14px; }
  label { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; }
  select, input[type="search"], input[type="date"] {
    height: 32px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0 10px;
    background: var(--raised);
    color: var(--ink);
    font: inherit;
    color-scheme: dark;
  }
  select { min-width: 128px; }
  select:focus, input:focus { outline: none; border-color: #3d5f9e; }
  select:disabled, input:disabled { color: var(--faint); }
  .stats { display: flex; gap: 14px; min-height: 16px; color: var(--muted); font-size: 12px; }
  .stats b { color: var(--ink); font-weight: 600; }
  .spacer { flex: 1; }
  .actions { display: flex; gap: 8px; }
  .btn {
    height: 32px;
    padding: 0 12px;
    border-radius: 7px;
    border: 1px solid var(--line);
    background: var(--raised);
    color: var(--ink);
    font: inherit;
    cursor: pointer;
  }
  .btn:hover { background: #242a34; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.primary:hover { background: var(--accent-press); }
  .btn.quiet { border-color: transparent; background: transparent; color: var(--muted); }
  .btn.quiet:hover { background: var(--raised); color: var(--ink); }
  .btn:disabled { color: var(--faint); cursor: default; }
  .btn:disabled:hover { background: var(--raised); }
  .btn.primary:disabled, .btn.primary:disabled:hover { background: #243044; border-color: #243044; color: #6d7888; }
  .grow { flex: 1; }
  .grow input { width: 220px; max-width: 100%; }
  .table-wrap { flex: 1; min-height: 0; overflow: auto; }
  .table-wrap::-webkit-scrollbar { width: 10px; height: 10px; }
  .table-wrap::-webkit-scrollbar-thumb { background: #303744; border-radius: 99px; }
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
    background: #171b22;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  td { padding: 11px 14px; border-bottom: 1px solid #232833; vertical-align: middle; white-space: nowrap; }
  tbody tr:hover td { background: #1a1f28; }
  .who { font-weight: 600; }
  .sub { display: block; margin-top: 2px; color: var(--muted); font-weight: 400; font-size: 12px; }
  .mono { font-family: Consolas, ui-monospace, monospace; font-size: 12px; color: #d5dbe6; }
  .detail { color: var(--muted); font-size: 12px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
  .blank { color: var(--faint); }
  .status { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); text-transform: capitalize; }
  .status i { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); }
  .status.running { color: #b9ccff; }
  .status.running i { background: #6ea8ff; }
  .status.done { color: #8ee0b2; }
  .status.done i { background: #3dce8c; }
  .status.failed { color: #f3b0b4; }
  .status.failed i { background: #f07178; }
  .status.queued { color: #b9ccff; }
  .status.queued i { background: #6ea8ff; }
  th.check, td.check { width: 42px; text-align: center; padding-left: 12px; padding-right: 8px; }
  input[type="checkbox"] { width: 15px; height: 15px; accent-color: var(--accent); }
  .row-btn { height: 28px; padding: 0 10px; }
  .row-btn.cancel { background: transparent; border-color: #6a3b3b; color: #f3b0b4; }
  .row-btn.cancel:hover { background: #2a1c1e; }
  .empty {
    flex: 1;
    display: grid;
    place-items: center;
    text-align: center;
    color: var(--muted);
    padding: 32px;
  }
  .empty strong { display: block; margin-bottom: 6px; color: var(--ink); font-size: 14px; font-weight: 600; }
  .empty code { color: #c5ceda; font-family: Consolas, ui-monospace, monospace; font-size: 12px; }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="mark"></span>
    <h1>Mailbox</h1>
    <span>GMX · WEB.DE</span>
  </div>
  <div class="switch" role="tablist">
    <button type="button" id="tab-generate" class="active">Generate</button>
    <button type="button" id="tab-history">History</button>
  </div>
</header>
<main>
  <section id="generate" class="panel">
    <div class="bar">
      <div class="fields">
        <label>Service
          <select id="provider"></select>
        </label>
        <label>Mailbox
          <select id="kind">
            <option value="gmx">GMX</option>
            <option value="webde">WEB.DE</option>
          </select>
        </label>
      </div>
      <div class="stats" id="summary"></div>
      <div class="spacer"></div>
      <div class="actions">
        <button type="button" class="btn quiet" id="clear">Clear</button>
        <button type="button" class="btn" id="import-file">Import</button>
        <button type="button" class="btn primary" id="start-selected">Start selected</button>
      </div>
      <input id="file" type="file" accept=".txt,.csv,text/plain" class="hidden">
    </div>
    <div class="empty" id="queue-empty">
      <div>
        <strong>Nothing imported</strong>
        <span>One person per line · <code>first;last;day;month;year;plz;city;street;password</code></span>
      </div>
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
    </div>
    <div class="empty" id="history-empty">No mailboxes match this search.</div>
    <div class="table-wrap hidden" id="history-wrap">
      <table>
        <thead>
          <tr><th>Created</th><th>Email</th><th>Password</th><th>Name</th><th>Service</th><th>Phone</th></tr>
        </thead>
        <tbody id="history-body"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const provider = document.querySelector("#provider");
const kind = document.querySelector("#kind");
const queueBody = document.querySelector("#queue");
const historyBody = document.querySelector("#history-body");
let latest = {rows: [], history: [], running: false};
const selected = new Set();

function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function pill(status) {
  const label = status || "ready";
  return `<span class="status ${esc(label)}"><i></i>${esc(label)}</span>`;
}

function textCell(value, cls) {
  const text = String(value ?? "").trim();
  if (!text) return `<td class="blank">—</td>`;
  return `<td class="${cls}" title="${esc(text)}">${esc(text)}</td>`;
}

function canStart(row) {
  return row.status === "ready" || row.status === "failed";
}

function rowAction(row) {
  if (row.status === "queued" || row.status === "running") {
    return `<button type="button" class="btn row-btn cancel" data-act="cancel" data-id="${esc(row.id)}">Cancel</button>`;
  }
  if (row.status === "done") return "";
  return `<button type="button" class="btn row-btn" data-act="start" data-id="${esc(row.id)}">Start</button>`;
}

function renderQueue() {
  const rows = latest.rows || [];
  const known = new Set(rows.map((row) => row.id));
  for (const id of [...selected]) if (!known.has(id)) selected.delete(id);
  queueBody.innerHTML = rows.map((row) => {
    const birth = [row.day, row.month, row.year].filter(Boolean).join(".");
    const place = [row.plz, row.city].filter(Boolean).join(" ");
    const pickable = canStart(row);
    return `<tr>
      <td class="check"><input class="pick" type="checkbox" value="${esc(row.id)}" ${selected.has(row.id) ? "checked" : ""} ${pickable ? "" : "disabled"}></td>
      <td><span class="who">${esc(row.first)} ${esc(row.last)}</span><span class="sub">${esc(birth)}</span></td>
      <td title="${esc(row.street)}"><span class="line">${esc(row.street)}</span><span class="sub">${esc(place)}</span></td>
      <td>${pill(row.status)}</td>
      ${textCell(row.phone, "mono")}
      ${textCell(row.email, "mono")}
      ${textCell(row.password, "mono")}
      ${textCell(row.detail, "detail")}
      <td>${rowAction(row)}</td>
    </tr>`;
  }).join("");
  document.querySelector("#queue-empty").classList.toggle("hidden", rows.length > 0);
  document.querySelector("#queue-wrap").classList.toggle("hidden", rows.length === 0);
  const counts = rows.reduce((sum, row) => { sum[row.status] = (sum[row.status] || 0) + 1; return sum; }, {});
  document.querySelector("#summary").innerHTML = ["ready", "queued", "running", "done", "failed"]
    .filter((key) => counts[key])
    .map((key) => `<span><b>${counts[key]}</b> ${esc(key)}</span>`)
    .join("");
  const startable = rows.filter((row) => canStart(row) && selected.has(row.id));
  document.querySelector("#start-selected").disabled = startable.length === 0;
  const pickable = rows.filter(canStart);
  const pickAll = document.querySelector("#pick-all");
  pickAll.checked = pickable.length > 0 && pickable.every((row) => selected.has(row.id));
  pickAll.indeterminate = !pickAll.checked && pickable.some((row) => selected.has(row.id));
  pickAll.disabled = pickable.length === 0;
  const busy = rows.some((row) => row.status === "queued" || row.status === "running");
  document.querySelector("#provider").disabled = busy;
  document.querySelector("#kind").disabled = busy;
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

function renderHistory() {
  const rows = filteredHistory();
  historyBody.innerHTML = rows.map((item) => `<tr>
    <td>${esc(item.created_at)}</td>
    ${textCell(item.email, "mono")}
    ${textCell(item.password, "mono")}
    <td>${esc(item.name)}</td>
    <td>${esc(item.provider)}<span class="sub">${esc(item.kind)}</span></td>
    ${textCell(item.phone, "mono")}
  </tr>`).join("");
  document.querySelector("#history-empty").classList.toggle("hidden", rows.length > 0);
  document.querySelector("#history-wrap").classList.toggle("hidden", rows.length === 0);
}

async function refresh() {
  const response = await fetch("/api/state");
  latest = await response.json();
  if (!provider.options.length) {
    provider.innerHTML = (latest.providers || []).map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
    if (latest.provider) provider.value = latest.provider;
    if (latest.kind) kind.value = latest.kind;
  }
  renderQueue();
  renderHistory();
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
document.querySelector("#file").onchange = async (event) => {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  await act("/api/import", {text: await file.text()});
};
document.querySelector("#clear").onclick = () => act("/api/clear", {});
document.querySelector("#start-selected").onclick = () => {
  const ids = (latest.rows || []).filter((row) => canStart(row) && selected.has(row.id)).map((row) => row.id);
  ids.forEach((id) => selected.delete(id));
  act("/api/start", {provider: provider.value, kind: kind.value, ids});
};
document.querySelector("#pick-all").onchange = (event) => {
  (latest.rows || []).filter(canStart).forEach((row) => {
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
    act("/api/start", {provider: provider.value, kind: kind.value, ids: [button.dataset.id]});
    return;
  }
  if (button.dataset.act === "cancel") act("/api/cancel", {id: button.dataset.id});
});
document.querySelector("#filter-date").oninput = renderHistory;
document.querySelector("#filter-query").oninput = renderHistory;
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[studio] {fmt % args}", flush=True)

    def do_GET(self) -> None:
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
                self._send(start_rows(str(body.get("provider") or ""), str(body.get("kind") or "gmx"), [str(item) for item in raw_ids]), 200)
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


def main() -> None:
    load_store()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        busy = getattr(exc, "winerror", None) in {10013, 10048} or exc.errno in {13, 48, 98}
        if busy:
            raise SystemExit(f"Mailbox is already open on port {PORT}.") from exc
        raise
    threading.Thread(target=server.serve_forever, name="studio-http", daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"Mailbox window at {url}", flush=True)
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("Install the window library with: pip install pywebview") from exc
    webview.create_window(
        "Mailbox",
        url,
        width=1280,
        height=820,
        min_size=(980, 640),
        background_color="#0c0e12",
        text_select=True,
    )
    try:
        webview.start()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Long-running phone-number service for SERVICE_URL3.

Keeps one browser signed in and serves three calls:

    POST /get_started   {"email": "name@gmx.net", "type": "gmx"}
    POST /get_code      {"phone": "491635021584"}
    POST /code_passed   {"email": "name@gmx.net"}

`type` is `gmx` or `webde`. Each get_started opens SERVICE_URL3/activation,
searches ANOSIM_COUNTRY, clicks the first visible country, searches the
service (`gmx` or `web`), clicks the first visible service, sets the amount
to ANOSIM_ORDER_COUNT (default 3, maximum 10), and clicks Book number once.
It then reads the new rows at the top of SERVICE_URL3/customer/dashboard and
saves those numbers under the email in anosim_orders.json.

get_code opens a new SERVICE_URL3/customer/dashboard page, finds the matching
number, waits up to 3 minutes for an OTP in that row's "Last received SMS"
table, and closes that page when it returns. Each get_code uses its own page,
so several lookups can run at once. code_passed clicks Done on the number
whose code was read, cancels the other numbers for that email, and deletes
those saved numbers.

get_started and code_passed wait in one queue so they do not share the order page.

Requires ANOSIM_EMAIL and ANOSIM_PASSWORD in .env, plus SERVICE_URL3.
Listens on ANOSIM_PORT (default 8782) so it can run beside the other number services.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import live_numbers

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STORE_FILE = ROOT / "anosim_orders.json"
PROFILE_DIR = ROOT / "anosim_browser"
OTP_WAIT_SECONDS = 120
ORDER_WAIT_SECONDS = 45
CLOSE_WAIT_SECONDS = 180
ACTION_PAUSE = 0.8


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
BASE_URL = os.environ.get("SERVICE_URL3", "").strip().rstrip("/")
LOGIN_URL = urllib.parse.urljoin(f"{BASE_URL}/", "login") if BASE_URL else ""
ACTIVATION_URL = urllib.parse.urljoin(f"{BASE_URL}/", "activation") if BASE_URL else ""
DASHBOARD_URL = urllib.parse.urljoin(f"{BASE_URL}/", "customer/dashboard") if BASE_URL else ""
ANOSIM_EMAIL = os.environ.get("ANOSIM_EMAIL", "").strip()
ANOSIM_PASSWORD = os.environ.get("ANOSIM_PASSWORD", "").strip()
ANOSIM_HOST = os.environ.get("ANOSIM_HOST", "127.0.0.1").strip() or "127.0.0.1"
ANOSIM_PORT = int(os.environ.get("ANOSIM_PORT", "8782") or "8782")
DEBUG_PORT = int(os.environ.get("ANOSIM_DEBUG_PORT", "9335") or "9335")
HEADLESS = os.environ.get("ANOSIM_HEADLESS", "").strip() == "1"
COUNTRY = os.environ.get("ANOSIM_COUNTRY", "Germany").strip() or "Germany"
SERVICE_QUERY = {
    "gmx": os.environ.get("ANOSIM_GMX_QUERY", "gmx").strip() or "gmx",
    "webde": os.environ.get("ANOSIM_WEBDE_QUERY", "web").strip() or "web",
}


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    text = f"[{stamp}] {message}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), flush=True)


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def same_phone(left: str, right: str) -> bool:
    a, b = digits(left), digits(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 10 and long.endswith(short)


def plus_phone(value: str) -> str:
    number = digits(value)
    return f"+{number}" if number else ""


def fold_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def latest_code(messages: list[dict]) -> str:
    for item in reversed(messages or []):
        text = str(item.get("text") or "")
        labeled = re.search(r"(?:lautet|code|pin)\s*[:：]?\s*(\d{4,8})", text, re.IGNORECASE)
        if labeled:
            return labeled.group(1)
        match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
        if match:
            return match.group(1)
    return ""


def normalize_type(value: str) -> str:
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    if key == "gmx":
        return "gmx"
    if key in {"webde", "web"}:
        return "webde"
    raise ValueError("type must be gmx or webde")


def order_count(override: int | None = None) -> int:
    if override is None:
        raw = os.environ.get("ANOSIM_ORDER_COUNT", "3").strip() or "3"
        if not raw.isdigit():
            raise ValueError("ANOSIM_ORDER_COUNT must be a number from 1 to 10")
        override = int(raw)
    if override < 1 or override > 10:
        raise ValueError("count must be from 1 to 10")
    return override


def price_index(value) -> int:
    if value is None or str(value).strip() == "":
        return 0
    raw = str(value).strip()
    if not raw.isdigit():
        raise ValueError("price_index must be a number from 0 to 20")
    index = int(raw)
    if index > 20:
        raise ValueError("price_index must be a number from 0 to 20")
    return index


class SmsError(Exception):
    pass


def park_broken(path: Path) -> Path | None:
    backup = path.with_name(f"{path.stem}.broken-{time.time_ns()}{path.suffix}")
    try:
        path.replace(backup)
    except OSError:
        return None
    return backup


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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


class OrderStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"emails": {}, "phones": {}}
        self._blocked = False
        if not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            backup = park_broken(path)
            if backup is None:
                self._blocked = True
                log(f"Could not read {path.name}. New numbers will not be written over it. ({exc})")
            else:
                log(f"Could not read {path.name}. Kept it as {backup.name}.")
            return
        if isinstance(loaded, dict):
            emails = loaded.get("emails")
            phones = loaded.get("phones")
            if isinstance(emails, dict):
                self.data["emails"] = emails
            if isinstance(phones, dict):
                self.data["phones"] = phones

    def save(self) -> None:
        if self._blocked:
            log(f"Skipped saving {self.path.name} because the current file could not be read")
            return
        save_json(self.path, self.data)

    def add(self, email: str, kind: str, phone: str, booking_id: str, service: str) -> None:
        with self._lock:
            self._add(email, kind, phone, booking_id, service)

    def _add(self, email: str, kind: str, phone: str, booking_id: str, service: str) -> None:
        key = digits(phone)
        if not key:
            return
        entry = self.data["emails"].setdefault(email, {"type": kind, "phones": []})
        entry["type"] = kind
        phones = entry.setdefault("phones", [])
        if key not in phones:
            phones.append(key)
        self.data["phones"][key] = {
            "email": email,
            "type": kind,
            "phone": plus_phone(key),
            "booking_id": booking_id,
            "service": service,
            "status": "active",
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.save()

    def phone_keys(self) -> set[str]:
        with self._lock:
            return {digits(str(key)) for key in self.data.get("phones", {}) if digits(str(key))}

    def phones_for(self, email: str) -> list[dict]:
        with self._lock:
            return self._phones_for(email)

    def _phones_for(self, email: str) -> list[dict]:
        entry = self.data["emails"].get(email) or {}
        found = []
        for key in entry.get("phones") or []:
            record = self.data["phones"].get(key)
            if isinstance(record, dict):
                found.append(record)
        return found

    def find_phone(self, phone: str) -> dict | None:
        with self._lock:
            record = self.data["phones"].get(digits(phone))
            return record if isinstance(record, dict) else None

    def mark(self, phone: str, status: str) -> None:
        with self._lock:
            record = self.data["phones"].get(digits(phone))
            if not isinstance(record, dict):
                return
            record["status"] = status
            self.save()

    def remove_email(self, email: str) -> list[str]:
        with self._lock:
            return self._remove_email(email)

    def _remove_email(self, email: str) -> list[str]:
        entry = self.data["emails"].pop(email, None)
        keys = []
        if isinstance(entry, dict):
            keys.extend(str(key) for key in entry.get("phones") or [])
        removed = []
        for key, record in list(self.data["phones"].items()):
            owned = key in keys or (isinstance(record, dict) and record.get("email") == email)
            if not owned:
                continue
            removed.append(str((record or {}).get("phone") or key))
            self.data["phones"].pop(key, None)
        self.save()
        return removed

    def drop_phones(self, phones: list[str]) -> list[str]:
        with self._lock:
            keys = {digits(phone) for phone in phones if digits(phone)}
            if not keys:
                return []
            removed = []
            for key in keys:
                record = self.data["phones"].pop(key, None)
                if isinstance(record, dict):
                    removed.append(str(record.get("phone") or key))
            for email, entry in list(self.data["emails"].items()):
                if not isinstance(entry, dict):
                    continue
                entry["phones"] = [key for key in entry.get("phones") or [] if str(key) not in keys]
                if not entry["phones"]:
                    self.data["emails"].pop(email, None)
            self.save()
            return removed


class CdpSocket:
    def __init__(self, ws_url: str, timeout: float = 30):
        parsed = urllib.parse.urlparse(ws_url)
        if not parsed.hostname or not parsed.port:
            raise RuntimeError(f"Bad DevTools websocket URL: {ws_url}")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools handshake closed")
            buffer += chunk
        head, extra = buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(status.decode(errors="replace"))
        self._extra = extra

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_exact(self, size: int) -> bytes:
        buffer = bytearray()
        if self._extra:
            take = self._extra[:size]
            self._extra = self._extra[len(take) :]
            buffer.extend(take)
        while len(buffer) < size:
            chunk = self.sock.recv(size - len(buffer))
            if not chunk:
                raise RuntimeError("DevTools socket closed")
            buffer.extend(chunk)
        return bytes(buffer)

    def send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self) -> dict:
        chunks: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read_exact(8), "big")
            if second & 0x80:
                mask = self._read_exact(4)
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._read_exact(length)))
            else:
                payload = self._read_exact(length)
            fin = bool(first & 0x80)
            if opcode == 0x8:
                raise RuntimeError("DevTools closed the connection")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0x1:
                chunks = [payload]
            elif opcode == 0x0:
                chunks.append(payload)
            else:
                continue
            if fin and chunks:
                return json.loads(b"".join(chunks).decode())

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x8A, 0x80 | len(payload)])
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)


class Cdp:
    def __init__(self, ws: CdpSocket):
        self.ws = ws
        self._next = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._next += 1
        message_id = self._next
        self.ws.sock.settimeout(timeout)
        self.ws.send_json({"id": message_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.ws.recv_json()
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message.get("result") or {}
        raise TimeoutError(method)


def browser_executable() -> str:
    configured = os.environ.get("ANOSIM_BROWSER", "").strip().strip('"') or os.environ.get("SMS_BROWSER", "").strip().strip('"')
    if configured:
        if not Path(configured).is_file():
            raise SystemExit(f"ANOSIM_BROWSER was not found: {configured}")
        return configured
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise SystemExit("Install Chrome or Edge, or set ANOSIM_BROWSER to the browser executable.")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def list_pages(port: int) -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, list):
        raise RuntimeError("DevTools target list was not a list")
    return [item for item in payload if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]


def browser_call(port: int, method: str, params: dict | None = None) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
        payload = json.loads(response.read().decode())
    ws_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise RuntimeError("Browser DevTools websocket was missing")
    ws = CdpSocket(ws_url)
    try:
        return Cdp(ws).call(method, params, timeout=30)
    finally:
        ws.close()


VISIBLE_FN = (
    " const visible = (el) => {"
    "   if (!el) return false;"
    "   const style = getComputedStyle(el);"
    "   const rect = el.getBoundingClientRect();"
    "   return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1;"
    " };"
    " const norm = (el) => ((el && (el.innerText || el.textContent || el.value)) || '').replace(/\\s+/g, ' ').trim();"
)

BOOKINGS_JS = r"""(() => {
  const grid = document.querySelector('#gridCurrentUsedSimCards');
  if (!grid) return [];
  const table = grid.querySelector(':scope > .mvc-grid-table > table');
  if (!table || !table.tBodies || !table.tBodies[0]) return [];
  const items = [];
  for (const row of table.tBodies[0].rows) {
    const phoneLabel = [...row.querySelectorAll('label')].find((el) => /\+\d{6,}/.test(el.textContent || ''));
    if (!phoneLabel) continue;
    const phone = (phoneLabel.textContent || '').replace(/\s+/g, '').trim();
    const blob = row.innerHTML || '';
    const copy = blob.match(/btnCopyPhoneNumber_(\d+)/);
    const cancel = blob.match(/cancelsimcardbooking\/(\d+)/);
    const cells = [...row.cells].map((td) => (td.innerText || '').replace(/\s+/g, ' ').trim());
    const stateEl = row.querySelector('.state');
    const stateLabel = stateEl ? stateEl.querySelector('label') : null;
    const state = ((stateLabel || stateEl) ? (stateLabel || stateEl).innerText : '').replace(/\s+/g, ' ').trim();
    const next = row.nextElementSibling;
    let messages = [];
    if (next && /last received sms/i.test(next.innerText || '')) {
      messages = [...next.querySelectorAll('table tbody tr')].map((tr) => {
        const cols = [...tr.cells].map((td) => (td.innerText || '').replace(/\s+/g, ' ').trim());
        return {date: cols[0] || '', sender: cols[1] || '', text: cols[2] || ''};
      }).filter((item) => item.text);
    }
    items.push({
      phone,
      bookingId: (copy && copy[1]) || (cancel && cancel[1]) || '',
      country: cells[1] || '',
      rental: cells[2] || '',
      service: cells[3] || '',
      state,
      messages
    });
  }
  return items;
})()"""


class Dashboard:
    def __init__(self):
        self.cdp: Cdp | None = None
        self.socket: CdpSocket | None = None
        self.proc: subprocess.Popen | None = None
        self.page_id = ""
        self.main_page_id = ""
        self._tab_id = ""
        self.code_page_ids: list[str] = []
        self._launch_lock = threading.Lock()

    def ensure_browser(self) -> None:
        with self._launch_lock:
            self._ensure_browser()

    def _ensure_browser(self) -> None:
        if self.cdp is not None:
            try:
                self.evaluate("1", timeout=5)
                return
            except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError):
                log("Browser connection lost. Reconnecting.")
                self._drop_socket()
        if not port_open(DEBUG_PORT):
            self.launch()
        self.attach()

    def launch(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = [
            browser_executable(),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--remote-allow-origins=*",
            "--window-size=1400,1000",
        ]
        if HEADLESS:
            args.append("--headless=new")
        args.append(LOGIN_URL or "about:blank")
        log("Starting the Anosim browser")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            if port_open(DEBUG_PORT):
                return
            if self.proc.poll() is not None:
                raise SmsError("The browser closed during startup")
            time.sleep(0.3)
        raise SmsError(f"Browser did not open DevTools on port {DEBUG_PORT}")

    def _page(self, page_id: str) -> dict | None:
        if not page_id:
            return None
        try:
            pages = list_pages(DEBUG_PORT)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError):
            return None
        return next((item for item in pages if str(item.get("id") or "") == page_id), None)

    def _wait_page(self, page_id: str) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            page = self._page(page_id)
            if page:
                return page
            time.sleep(0.3)
        raise SmsError("Could not open the dashboard page")

    def attach(self) -> None:
        if self._tab_id:
            self._connect(self._wait_page(self._tab_id))
            return
        deadline = time.time() + 30
        last_error = "no page"
        host = urllib.parse.urlparse(BASE_URL).netloc
        while time.time() < deadline:
            try:
                pages = list_pages(DEBUG_PORT)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = str(exc)
                time.sleep(0.4)
                continue
            pages = [item for item in pages if str(item.get("id") or "") not in self.code_page_ids]
            if not pages:
                last_error = "browser has no order page yet"
                time.sleep(0.4)
                continue
            remembered = next((item for item in pages if str(item.get("id") or "") == self.main_page_id), None)
            if remembered:
                pages = [remembered]
            else:
                pages.sort(key=lambda item: host in str(item.get("url") or ""), reverse=True)
            try:
                self._connect(pages[0])
                self.main_page_id = self.page_id
                return
            except (OSError, RuntimeError, TimeoutError) as exc:
                last_error = str(exc)
                self._drop_socket()
                time.sleep(0.4)
        raise SmsError(f"Could not attach to the browser on port {DEBUG_PORT}: {last_error}")

    def open_code_page(self, number: str) -> Dashboard:
        if not DASHBOARD_URL:
            raise SmsError("Set SERVICE_URL3 in the environment")
        with self._launch_lock:
            self._ensure_browser()
            if self.page_id:
                self.main_page_id = self.page_id
            log(f"Opening a new page for {number}")
            try:
                created = browser_call(DEBUG_PORT, "Target.createTarget", {"url": DASHBOARD_URL, "newWindow": True})
            except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError, TimeoutError):
                created = browser_call(DEBUG_PORT, "Target.createTarget", {"url": DASHBOARD_URL})
            target_id = str(created.get("targetId") or "")
            if not target_id:
                raise SmsError("Could not open a new page")
            self.code_page_ids.append(target_id)
        lookup = Dashboard()
        lookup.proc = self.proc
        lookup._tab_id = target_id
        try:
            lookup._connect(self._wait_page(target_id))
            try:
                browser_call(DEBUG_PORT, "Target.activateTarget", {"targetId": target_id})
            except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError, TimeoutError):
                pass
            if "customer/dashboard" not in lookup.href().lower():
                lookup.goto(DASHBOARD_URL)
            if lookup.on_login_form():
                lookup.login()
                if "customer/dashboard" not in lookup.href().lower():
                    lookup.goto(DASHBOARD_URL)
            if not lookup.wait_css("#gridCurrentUsedSimCards", 20):
                raise SmsError("Dashboard did not load")
            lookup.widen_grid()
        except Exception:
            self.close_code_page(lookup)
            raise
        return lookup

    def close_code_page(self, lookup: Dashboard) -> None:
        target_id = lookup._tab_id
        lookup._tab_id = ""
        lookup._drop_socket()
        with self._launch_lock:
            if target_id in self.code_page_ids:
                self.code_page_ids.remove(target_id)
        if not target_id:
            return
        log("Closing the code page")
        try:
            browser_call(DEBUG_PORT, "Target.closeTarget", {"targetId": target_id})
        except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError, TimeoutError) as exc:
            log(f"Could not close the code page: {exc}")

    def _drop_socket(self) -> None:
        if self.socket is not None:
            self.socket.close()
        self.socket = None
        self.cdp = None
        self.page_id = ""

    def _connect(self, page: dict) -> None:
        page_id = str(page.get("id") or "")
        if self.cdp is not None and self.page_id == page_id:
            try:
                self.evaluate("1", timeout=5)
                return
            except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError):
                self._drop_socket()
        self._drop_socket()
        self.socket = CdpSocket(str(page["webSocketDebuggerUrl"]))
        self.cdp = Cdp(self.socket)
        self.page_id = page_id
        self.evaluate("1", timeout=10)
        try:
            self.cdp.call("Page.bringToFront", timeout=5)
        except (RuntimeError, TimeoutError, OSError):
            pass

    def evaluate(self, expression: str, timeout: float = 30):
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        result = self.cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            text = (details.get("exception") or {}).get("description") or details.get("text") or details
            raise RuntimeError(str(text))
        return (result.get("result") or {}).get("value")

    def href(self) -> str:
        return str(self.evaluate("location.href") or "")

    def pause(self) -> None:
        time.sleep(ACTION_PAUSE)

    def install_hooks(self) -> None:
        try:
            self.evaluate(
                "(() => { window.confirm = () => true; window.alert = () => {}; window.prompt = () => ''; return true; })()",
                timeout=10,
            )
        except (RuntimeError, TimeoutError, OSError):
            pass

    def goto(self, url: str) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        log(f"Opening {url}")
        self.cdp.call("Page.enable", timeout=15)
        self.cdp.call("Page.navigate", {"url": url}, timeout=45)
        self.wait_document(45)
        self.install_hooks()
        self.dismiss_overlays()

    def reload(self) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        log("Refreshing the page")
        self.cdp.call("Page.enable", timeout=15)
        self.cdp.call("Page.reload", {"ignoreCache": True}, timeout=45)
        self.wait_document(45)
        self.install_hooks()
        self.dismiss_overlays()

    def wait_document(self, timeout: float) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                value = str(self.evaluate("document.readyState + '\\n' + location.href", timeout=10) or "")
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            state, _, current = value.partition("\n")
            last = current or last
            if state == "complete" and current.startswith("http"):
                return
            time.sleep(0.4)
        if last.startswith("http"):
            return
        raise SmsError(f"Page did not finish loading ({last or BASE_URL})")

    def wait_css(self, selector: str, timeout: float) -> bool:
        expression = f"!!document.querySelector({json.dumps(selector)})"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expression, timeout=10):
                    return True
            except (RuntimeError, TimeoutError, OSError):
                pass
            time.sleep(0.3)
        return False

    def click_js(self, selector: str) -> bool:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " el.click();"
            " return true;"
            "})()"
        )
        try:
            return bool(self.evaluate(expression))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def set_value(self, selector: str, value: str) -> str:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return '';"
            " el.focus();"
            " const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
            f" desc.set.call(el, {json.dumps(value)});"
            " el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: el.value}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));"
            " return el.value;"
            "})()"
        )
        return str(self.evaluate(expression) or "")

    def fill(self, selector: str, value: str) -> None:
        if not self.click_js(selector):
            raise SmsError(f"Could not find {selector}")
        typed = self.set_value(selector, value)
        if typed != value:
            raise SmsError(f"Could not fill {selector}")

    def dismiss_overlays(self) -> None:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const labels = ['accept all', 'accept', 'agree', 'akzeptieren', 'alle akzeptieren', 'zustimmen'];"
            " const button = [...document.querySelectorAll('button, a.btn')].find((el) => visible(el) && labels.includes(norm(el).toLowerCase()));"
            " if (!button) return '';"
            " button.click();"
            " return norm(button);"
            "})()"
        )
        try:
            clicked = str(self.evaluate(expression, timeout=8) or "")
        except (RuntimeError, TimeoutError, OSError):
            return
        if clicked:
            log(f"Dismissed {clicked}")

    def on_login_form(self) -> bool:
        try:
            return bool(self.evaluate("!!document.querySelector('#Input_Email')", timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def error_notice(self) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const nodes = [...document.querySelectorAll('.alert-danger, .validation-summary-errors, .field-validation-error')];"
            " const text = nodes.filter((el) => visible(el) && !el.closest('button')).map(norm).filter(Boolean);"
            " return text[0] || '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def ensure_session(self) -> None:
        self.ensure_browser()
        href = ""
        try:
            href = self.href().lower()
        except (RuntimeError, TimeoutError, OSError):
            href = ""
        if not href.startswith("http") or "login" in href or self.on_login_form():
            self.goto(LOGIN_URL)
        if self.on_login_form():
            self.login()
            return
        log("Anosim session already signed in")

    def login(self) -> None:
        if not ANOSIM_EMAIL or not ANOSIM_PASSWORD:
            raise SmsError("Set ANOSIM_EMAIL and ANOSIM_PASSWORD in the environment")
        if not self.wait_css("#Input_Email", 20):
            raise SmsError("Anosim login form did not appear")
        log("Signing in to Anosim")
        self.fill("#Input_Email", ANOSIM_EMAIL)
        self.fill("#Input_Password", ANOSIM_PASSWORD)
        if not self.click_js("#login-submit"):
            raise SmsError("Could not find the login button")
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                if not self.on_login_form():
                    href = self.href().lower()
                    if href.startswith("http") and "login" not in href:
                        log("Signed in")
                        return
            except (RuntimeError, TimeoutError, OSError):
                pass
            time.sleep(0.4)
        notice = self.error_notice() or "Login did not finish"
        raise SmsError(notice)

    def open_activation(self) -> None:
        self.goto(ACTIVATION_URL)
        if self.wait_css("#ftCountryCode", 20):
            return
        if self.on_login_form():
            self.login()
            self.goto(ACTIVATION_URL)
        if not self.wait_css("#ftCountryCode", 20):
            raise SmsError("Activation page did not load")

    def list_text(self, selector: str) -> str:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " return el ? (el.innerText || '').replace(/\\s+/g, ' ').trim() : '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def click_filtered(self, field: str, list_id: str, query: str, label: str) -> str:
        log(f"Typing {query} into {label}")
        if not self.click_js(field):
            raise SmsError(f"Could not find the {label} search")
        typed = self.set_value(field, query)
        if typed != query:
            raise SmsError(f"Could not type {query!r} into the {label} search")
        self.pause()
        needle = fold_text(query)
        deadline = time.time() + 15
        while time.time() < deadline:
            picked = self._activate_first_match(list_id, needle)
            if picked:
                log(f"Selected {picked}")
                self.pause()
                return picked
            time.sleep(0.3)
        raise SmsError(f"No {label} matched {query!r}")

    def _activate_first_match(self, list_id: str, needle: str) -> str:
        expression = (
            "(() => {"
            f" const root = document.querySelector({json.dumps('#' + list_id)});"
            f" const needle = {json.dumps(needle)};"
            " if (!root) return '';"
            " const fold = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]/g, '');"
            " const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();"
            " const buttons = [...root.querySelectorAll('button')].filter((el) => !el.classList.contains('listitem-filter-hidden'));"
            " if (!buttons.length || !fold(textOf(buttons[0])).includes(needle)) return '';"
            " buttons[0].scrollIntoView({block: 'center', inline: 'center'});"
            " buttons[0].click();"
            " return textOf(buttons[0]);"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def wait_service_list(self, previous: str) -> None:
        deadline = time.time() + 15
        stable = ""
        repeats = 0
        while time.time() < deadline:
            current = self.list_text("#lbService")
            if current and current != previous:
                if current == stable:
                    repeats += 1
                    if repeats >= 2:
                        return
                else:
                    stable = current
                    repeats = 0
            time.sleep(0.3)
        if self.list_text("#lbService"):
            return
        raise SmsError("Service list did not load")

    def book_ready(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const button = document.querySelector('#btnPlaceOrderClick');"
            " const input = document.querySelector('#inputProductAmount');"
            " return !!(button && input && visible(button) && visible(input) && !button.disabled);"
            "})()"
        )
        try:
            return bool(self.evaluate(expression, timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def click_extra_list(self) -> str:
        expression = (
            "(() => {"
            " const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();"
            " const lists = [...document.querySelectorAll('div.listbox-filter[id^=\"lb\"]')]"
            "   .filter((el) => el.id !== 'lbCountryCode' && el.id !== 'lbService');"
            " for (const list of lists) {"
            "   const button = [...list.querySelectorAll('button')].find((el) => !el.classList.contains('listitem-filter-hidden'));"
            "   if (!button) continue;"
            "   button.scrollIntoView({block: 'center', inline: 'center'});"
            "   button.click();"
            "   return textOf(button);"
            " }"
            " return '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def wait_book_form(self) -> None:
        clicked_extra = False
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.book_ready():
                return
            if not clicked_extra:
                extra = self.click_extra_list()
                if extra:
                    clicked_extra = True
                    log(f"Selected {extra}")
                    self.pause()
            time.sleep(0.4)
        if not self.book_ready():
            raise SmsError("Book number did not appear")

    def set_amount(self, count: int) -> int:
        expression = (
            "(() => {"
            f" const count = {int(count)};"
            " const el = document.querySelector('#inputProductAmount');"
            " if (!el) return null;"
            " const max = parseInt(el.getAttribute('max') || '', 10);"
            " const min = parseInt(el.getAttribute('min') || '1', 10) || 1;"
            " let next = count;"
            " if (Number.isFinite(max) && max > 0 && next > max) next = max;"
            " if (next < min) next = min;"
            " if (Number.isFinite(max) && max === 0) return {value: '0', max: 0};"
            " const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
            " desc.set.call(el, String(next));"
            " el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: el.value}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return {value: el.value, max: Number.isFinite(max) ? max : null};"
            "})()"
        )
        raw = self.evaluate(expression)
        if not isinstance(raw, dict):
            raise SmsError("Could not find the amount field")
        value = str(raw.get("value") or "")
        if not value.isdigit() or int(value) < 1:
            raise SmsError("No numbers are available for this service")
        log(f"Set order amount to {value}")
        return int(value)

    def page_busy(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const spinner = [...document.querySelectorAll('.spinner-border, .spinner-grow')].some(visible);"
            " const modal = [...document.querySelectorAll('.modal.show, .swal2-popup')].some(visible);"
            " return spinner || modal;"
            "})()"
        )
        try:
            return bool(self.evaluate(expression, timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def confirm_modal(self) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const labels = ['confirm', 'yes', 'book number', 'book', 'ok', 'buy', 'order', 'ja', 'bestätigen', 'bestaetigen'];"
            " const skip = ['cancel', 'close', 'no', 'abbrechen', 'zurück', 'zurueck'];"
            " const dialogs = [...document.querySelectorAll('.modal.show, .swal2-popup, [role=dialog]')].filter(visible);"
            " for (const dialog of dialogs) {"
            "   const button = [...dialog.querySelectorAll('button, input[type=submit]')].find((el) => {"
            "     const text = norm(el).toLowerCase();"
            "     return visible(el) && !el.disabled && labels.includes(text) && !skip.includes(text);"
            "   });"
            "   if (!button) continue;"
            "   button.click();"
            "   return norm(button);"
            " }"
            " return '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def prepare_order(self, kind: str, count: int) -> int:
        self.open_activation()
        previous = self.list_text("#lbService")
        self.click_filtered("#ftCountryCode", "lbCountryCode", COUNTRY, "country")
        self.wait_service_list(previous)
        self.click_filtered("#ftService", "lbService", SERVICE_QUERY[kind], "service")
        self.wait_book_form()
        return self.set_amount(count)

    def click_book(self) -> str:
        self.install_hooks()
        if not self.click_js("#btnPlaceOrderClick"):
            raise SmsError("Could not find Book number")
        log("Clicked Book number")
        notice = ""
        deadline = time.time() + 20
        time.sleep(1.5)
        while time.time() < deadline:
            confirmed = self.confirm_modal()
            if confirmed:
                log(f"Confirmed {confirmed}")
                time.sleep(1.5)
                continue
            notice = self.order_error()
            if notice:
                log(notice)
                break
            if not self.page_busy():
                break
            time.sleep(0.4)
        time.sleep(2)
        return notice

    def order_error(self) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const nodes = [...document.querySelectorAll('.alert-danger, .validation-summary-errors')];"
            " const text = nodes.filter((el) => visible(el) && !el.closest('button')).map(norm).filter(Boolean);"
            " return text[0] || '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def widen_grid(self) -> None:
        expression = (
            "(() => {"
            " const select = document.querySelector('#gridCurrentUsedSimCards select.mvc-grid-pager-rows');"
            " if (!select) return 'missing';"
            " if (select.value === '50') return 'ready';"
            " const option = [...select.options].find((item) => item.value === '50');"
            " if (!option) return 'ready';"
            " select.value = '50';"
            " select.dispatchEvent(new Event('change', {bubbles: true}));"
            " return 'changed';"
            "})()"
        )
        try:
            changed = str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return
        if changed == "changed":
            time.sleep(0.4)

    def bookings(self) -> list[dict]:
        try:
            value = self.evaluate(BOOKINGS_JS, timeout=15)
        except (RuntimeError, TimeoutError, OSError):
            return []
        if not isinstance(value, list):
            return []
        rows = []
        for item in value:
            if not isinstance(item, dict):
                continue
            phone = str(item.get("phone") or "").strip()
            if not digits(phone):
                continue
            messages = []
            for message in item.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                messages.append(
                    {
                        "date": str(message.get("date") or ""),
                        "sender": str(message.get("sender") or ""),
                        "text": str(message.get("text") or ""),
                    }
                )
            rows.append(
                {
                    "phone": phone,
                    "booking_id": str(item.get("bookingId") or ""),
                    "service": str(item.get("service") or ""),
                    "rental": str(item.get("rental") or ""),
                    "state": str(item.get("state") or ""),
                    "messages": messages,
                }
            )
        return rows

    def grid_loading(self) -> bool:
        expression = (
            "(() => {"
            " const grid = document.querySelector('#gridCurrentUsedSimCards');"
            " if (!grid) return true;"
            " if (grid.classList.contains('mvc-grid-loading')) return true;"
            " if (grid.querySelector('.mvc-grid-loader, .mvc-grid-loading, .spinner-border, .spinner-grow')) return true;"
            " const table = grid.querySelector(':scope > .mvc-grid-table > table');"
            " return !(table && table.tBodies && table.tBodies[0]);"
            "})()"
        )
        try:
            return bool(self.evaluate(expression, timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def wait_bookings(self) -> list[dict]:
        self.widen_grid()
        deadline = time.time() + 12
        empty_reads = 0
        while time.time() < deadline:
            if self.grid_loading():
                empty_reads = 0
                time.sleep(0.4)
                continue
            rows = self.bookings()
            if rows:
                return rows
            empty_reads += 1
            if empty_reads >= 5:
                return []
            time.sleep(0.5)
        return self.bookings()

    def show_dashboard(self, refresh: bool = False) -> list[dict]:
        self.ensure_browser()
        on_dashboard = "customer/dashboard" in self.href().lower()
        if refresh and on_dashboard:
            self.reload()
        elif not on_dashboard:
            self.goto(DASHBOARD_URL)
        if self.on_login_form():
            self.login()
            self.goto(DASHBOARD_URL)
        if not self.wait_css("#gridCurrentUsedSimCards", 20):
            raise SmsError("Dashboard did not load")
        return self.wait_bookings()

    def button_labels(self) -> str:
        expression = (
            "(() => [...document.querySelectorAll('button, input[type=submit], a.btn')]"
            ".slice(0, 8).map((el) => (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim()).filter(Boolean))()"
        )
        try:
            value = self.evaluate(expression, timeout=10)
        except (RuntimeError, TimeoutError, OSError):
            return ""
        if not isinstance(value, list):
            return ""
        return ", ".join(str(item) for item in value if str(item).strip())

    def wait_leave(self, previous: str, timeout: float = 12) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                current = self.href()
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.3)
                continue
            if current and current != previous:
                try:
                    self.wait_document(20)
                except SmsError:
                    self._drop_socket()
                    self.attach()
                return True
            time.sleep(0.3)
        return False

    def submit_cancel_form(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " if (!location.pathname.toLowerCase().includes('cancelsimcardbooking')) return '';"
            " const buttons = [...document.querySelectorAll('button, input[type=submit]')].filter((el) => visible(el) && !el.disabled);"
            " const submit = buttons.find((el) => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0];"
            " if (!submit) return '';"
            " submit.click();"
            " return norm(submit);"
            "})()"
        )
        try:
            clicked = str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return False
        if clicked:
            log(f"Confirmed cancel with {clicked}")
        return bool(clicked)

    def finish_cancel_page(self, previous: str) -> bool:
        moved = self.wait_leave(previous)
        try:
            href = self.href().lower()
        except (RuntimeError, TimeoutError, OSError):
            self._drop_socket()
            self.attach()
            href = self.href().lower()
        if not moved and "cancelsimcardbooking" not in href:
            return False
        if "cancelsimcardbooking" in href:
            if not self.submit_cancel_form():
                log(f"Cancel page needs a button ({self.button_labels() or 'none visible'})")
                self.goto(DASHBOARD_URL)
                return False
            try:
                self.wait_document(25)
            except SmsError:
                self._drop_socket()
                self.attach()
        if "customer/dashboard" not in self.href().lower():
            self.goto(DASHBOARD_URL)
        return True

    def press_booking(self, booking_id: str, phone: str, action: str) -> str:
        previous = ""
        try:
            previous = self.href()
        except (RuntimeError, TimeoutError, OSError):
            previous = ""
        expression = (
            "(() => {"
            f" const bookingId = {json.dumps(booking_id)};"
            f" const phone = {json.dumps(digits(phone))};"
            f" const action = {json.dumps(action)};"
            " const table = document.querySelector('#gridCurrentUsedSimCards > .mvc-grid-table > table');"
            " if (!table || !table.tBodies[0]) return 'missing';"
            " const only = (value) => (value || '').replace(/\\D/g, '');"
            " const row = [...table.tBodies[0].rows].find((tr) => {"
            "   const blob = tr.innerHTML || '';"
            "   if (bookingId && blob.includes('btnCopyPhoneNumber_' + bookingId)) return true;"
            "   if (bookingId && blob.includes('cancelsimcardbooking/' + bookingId)) return true;"
            "   const label = [...tr.querySelectorAll('label')].find((el) => /\\+\\d{6,}/.test(el.textContent || ''));"
            "   return !!(phone && label && only(label.textContent) === phone);"
            " });"
            " if (!row) return 'missing';"
            " const labelOf = (el) => ((el.getAttribute('title') || '') + ' ' + (el.innerText || '') + ' ' + (el.getAttribute('onclick') || '') + ' ' + (el.getAttribute('href') || '')).toLowerCase();"
            " const buttons = [...row.querySelectorAll('button, a')];"
            " const done = buttons.find((el) => /done|finish|complete|erledigt|fertig|abschlie/.test(labelOf(el)) && !/cancel|storno/.test(labelOf(el)));"
            " const cancel = buttons.find((el) => /cancel booking|cancelsimcardbooking|storn/.test(labelOf(el)));"
            " const button = action === 'done' ? done : cancel;"
            " if (!button) return 'absent';"
            " button.scrollIntoView({block: 'center', inline: 'center'});"
            " button.click();"
            " return action;"
            "})()"
        )
        try:
            result = str(self.evaluate(expression, timeout=15) or "missing")
        except (RuntimeError, TimeoutError, OSError):
            result = "navigated"
        if result == "absent" and action == "done":
            log(f"No done button for {plus_phone(phone)}. Leaving that booking active.")
            return "done"
        if result == "done":
            return "done"
        if result in {"cancel", "navigated"} and action == "cancel":
            if not self.finish_cancel_page(previous):
                return "waiting"
            return "cancel"
        if result == "navigated" and action == "done":
            if "customer/dashboard" not in self.href().lower():
                self.goto(DASHBOARD_URL)
            return "done"
        return result if result in {"missing", "waiting", "absent"} else "waiting"


class NumberService:
    def __init__(self):
        self.browser = Dashboard()
        self.store = OrderStore(STORE_FILE)
        self.jobs: queue.Queue = queue.Queue()
        self._state = threading.Lock()
        self._active = False
        self._waiting = 0
        self._worker = threading.Thread(target=self._run_queue, name="anosim-queue", daemon=True)
        self._worker.start()

    def _run_queue(self) -> None:
        while True:
            label, func, box, done = self.jobs.get()
            with self._state:
                self._waiting -= 1
                self._active = True
            log(f"Running {label}")
            try:
                box["result"] = func()
            except Exception as exc:
                box["error"] = exc
            finally:
                with self._state:
                    self._active = False
                done.set()
                self.jobs.task_done()

    def _submit(self, label: str, func):
        done = threading.Event()
        box: dict = {}
        with self._state:
            ahead = self._waiting + (1 if self._active else 0)
            self._waiting += 1
            self.jobs.put((label, func, box, done))
        if ahead:
            log(f"Queued {label}. {ahead} call(s) ahead.")
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def open(self) -> None:
        self._submit("sign in", self.browser.ensure_session)

    def get_started(self, email: str, kind: str, count: int | None = None, tier: int = 0) -> dict:
        email = email.strip()
        if "@" not in email or len(email) > 200:
            raise ValueError("email is invalid")
        provider = normalize_type(kind)
        wanted = order_count(count)
        chosen = price_index(tier)
        return self._submit(
            f"get_started {email} ({provider}) price {chosen + 1}",
            lambda email=email, provider=provider, wanted=wanted, chosen=chosen: self._get_started(email, provider, wanted, chosen),
        )

    def _snapshot(self) -> set[str]:
        known = self.store.phone_keys()
        try:
            rows = self.browser.show_dashboard(refresh=False)
        except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
            log(f"Could not read current numbers before ordering: {exc}")
            return known
        for row in rows:
            key = digits(row["phone"])
            if key:
                known.add(key)
        log(f"Dashboard already has {len(rows)} number(s)")
        return known

    def _fresh_rows(self, rows: list[dict], snapshot: set[str], count: int) -> list[dict]:
        fresh = []
        for row in rows:
            status = re.sub(r"[^a-z]", "", str(row.get("state") or "").lower())
            if status in {"ended", "cancelled", "canceled", "storno", "expired"}:
                continue
            key = digits(row["phone"])
            if not key or key in snapshot or any(same_phone(key, seen) for seen in snapshot):
                continue
            fresh.append(row)
            if len(fresh) >= count:
                break
        return fresh

    def _collect_new(self, snapshot: set[str], count: int, timeout: float = ORDER_WAIT_SECONDS) -> list[dict]:
        deadline = time.time() + timeout
        refresh = False
        found: list[dict] = []
        reported: tuple[int, int] | None = None
        while time.time() < deadline:
            try:
                rows = self.browser.show_dashboard(refresh=refresh)
            except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                log(f"Could not read the dashboard: {exc}")
                time.sleep(2)
                refresh = True
                continue
            found = self._fresh_rows(rows, snapshot, count)
            marker = (len(rows), len(found))
            if marker != reported:
                log(f"Dashboard shows {len(rows)} number(s), {len(found)} new")
                reported = marker
            if len(found) >= count:
                return found
            refresh = True
            time.sleep(2)
        return found

    def _reuse_or_none(self, email: str, kind: str, count: int) -> dict | None:
        reused = live_numbers.take("anosim", kind, count)
        if not reused:
            return None
        for item in reused["orders"]:
            remain = int(item.get("remain_seconds") or 0)
            log(f"Reusing {item.get('phone')} ({max(1, (remain + 59) // 60)} min left)")
        reused["email"] = email
        return reused

    def _remember_code(self, number: str, code: str, row: dict | None) -> None:
        if not live_numbers.enabled():
            return
        stored = self.store.find_phone(number) or live_numbers.record(number)
        kind = str(stored.get("type") or "gmx")
        booking_id = str((row or {}).get("booking_id") or stored.get("booking_id") or "")
        saved = live_numbers.prove(
            "anosim",
            kind,
            number,
            code=code,
            ordered_at=str(stored.get("created_at") or stored.get("ordered_at") or ""),
            booking_id=booking_id,
            ttl_seconds=live_numbers.rental_seconds(str((row or {}).get("rental") or "")),
        )
        remain = int(saved.get("remain_seconds") or 0)
        log(f"Keeping {plus_phone(number)} for {max(1, (remain + 59) // 60)} min")

    def _release_expired(self) -> None:
        gone = live_numbers.expired("anosim")
        if not gone:
            return
        log(f"Removing {len(gone)} timed out number(s)")
        try:
            self.browser.ensure_session()
            self.browser.show_dashboard(refresh=False)
        except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
            log(f"Could not open the dashboard for timed out numbers: {exc}")
        for item in gone:
            phone = str(item.get("phone") or "")
            booking_id = str(item.get("booking_id") or "")
            try:
                action = self.browser.press_booking(booking_id, phone, "done")
                log(f"Timed out {plus_phone(phone)} {action}")
            except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                log(f"Could not close timed out {plus_phone(phone)}: {exc}")
            live_numbers.drop(phone)
            self.store.drop_phones([phone])

    def _get_started(self, email: str, kind: str, count: int, tier: int = 0) -> dict:
        self._release_expired()
        previous = [
            item
            for item in self.store.phones_for(email)
            if item.get("status") in {"active", "coded"} and item.get("phone")
        ]
        if previous:
            _kept, closing = live_numbers.partition(previous)
            if closing:
                log("Cancelling the previous price order")
                closed = self._code_passed(email)
                if not closed.get("ok") and closed.get("error") != "unknown email":
                    return {
                        "ok": False,
                        "error": "Could not cancel the previous price order",
                        "email": email,
                        "type": kind,
                        "phones": [],
                        "orders": [],
                    }
        reused = self._reuse_or_none(email, kind, count)
        if reused:
            return reused
        if tier:
            return {"ok": False, "error": "no cheaper price", "email": email, "type": kind, "phones": [], "orders": []}
        self.browser.ensure_session()
        snapshot = self._snapshot()
        phones: list[str] = []
        orders: list[dict] = []
        failure = "failed get number"
        amount = count
        try:
            amount = self.browser.prepare_order(kind, count)
            notice = self.browser.click_book()
            found = self._collect_new(snapshot, amount, timeout=15 if notice else ORDER_WAIT_SECONDS)
            for row in found:
                phone = str(row.get("phone") or "")
                key = digits(phone)
                if not key or any(same_phone(phone, existing) for existing in phones):
                    continue
                booking_id = str(row.get("booking_id") or "")
                service = str(row.get("service") or "")
                self.store.add(email, kind, phone, booking_id, service)
                snapshot.add(key)
                phones.append(plus_phone(key))
                orders.append({"phone": plus_phone(key), "booking_id": booking_id, "service": service})
                log(f"Saved {plus_phone(key)} for {email}")
            if phones and len(phones) < amount:
                log(f"Booked {len(phones)} of {amount}")
            elif not phones and notice:
                failure = notice
        except SmsError as exc:
            failure = str(exc)
            log(failure)
        payload = {"email": email, "type": kind, "phones": phones, "orders": orders}
        if not phones:
            payload["ok"] = False
            payload["error"] = failure
            return payload
        payload["ok"] = True
        return payload

    def get_code(self, phone: str) -> dict:
        number = digits(phone)
        if len(number) < 6:
            raise ValueError("phone is invalid")
        page = self.browser.open_code_page(number)
        try:
            return self._get_code(page, number)
        finally:
            self.browser.close_code_page(page)

    def _get_code(self, page: Dashboard, number: str) -> dict:
        deadline = time.monotonic() + OTP_WAIT_SECONDS
        found_row = False
        warned = False
        while time.monotonic() < deadline:
            try:
                rows = page.show_dashboard(refresh=False)
            except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                log(f"Could not read the dashboard: {exc}")
                time.sleep(2)
                continue
            row = next((item for item in rows if same_phone(item["phone"], number)), None)
            if row and not found_row:
                found_row = True
                log(f"Found {row['phone']} ({row.get('service') or 'no service'})")
            code = latest_code(row["messages"]) if row else ""
            if code and live_numbers.same_as_last(number, code):
                if not warned:
                    log(f"Waiting for a new code on {plus_phone(number)}")
                    warned = True
                code = ""
            if code:
                self.store.mark(number, "coded")
                self._remember_code(number, code, row)
                log(f"OTP for {number} is {code}")
                return {"ok": True, "phone": plus_phone(number), "code": code}
            time.sleep(2)
        if live_numbers.is_live(number):
            live_numbers.pause(number, 45)
            log(f"No new code for {plus_phone(number)}. It stays saved for the next order.")
        else:
            log(f"No OTP for {number} within 3 minutes")
        return {"ok": False, "error": "failed", "phone": plus_phone(number)}

    def code_passed(self, email: str) -> dict:
        email = email.strip()
        if "@" not in email:
            raise ValueError("email is invalid")
        return self._submit(f"code_passed {email}", lambda email=email: self._code_passed(email))

    def _code_passed(self, email: str) -> dict:
        self._release_expired()
        records = [
            item
            for item in self.store.phones_for(email)
            if item.get("status") in {"active", "coded"} and item.get("phone")
        ]
        if not records:
            return {"ok": False, "error": "unknown email", "email": email, "results": []}
        kept, records = live_numbers.partition(records)
        if not records:
            log(f"Keeping {len(kept)} live number(s)")
            return {"ok": True, "email": email, "results": [], "kept": True}
        self.browser.ensure_session()
        self._note_codes(records)
        return self._finish_numbers(email, records)

    def _note_codes(self, records: list[dict]) -> None:
        try:
            rows = self.browser.show_dashboard(refresh=True)
        except (SmsError, RuntimeError, TimeoutError, OSError):
            rows = []
        for record in records:
            if record.get("status") == "coded":
                continue
            phone = str(record.get("phone") or "")
            row = next((item for item in rows if same_phone(item["phone"], phone)), None)
            if row and latest_code(row.get("messages") or []):
                record["status"] = "coded"
                self.store.mark(phone, "coded")
                log(f"{plus_phone(phone)} already has an OTP")

    def _finish_numbers(self, email: str, records: list[dict]) -> dict:
        pending = list(records)
        results = []
        deadline = time.time() + CLOSE_WAIT_SECONDS
        while pending and time.time() < deadline:
            try:
                self.browser.show_dashboard(refresh=False)
            except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                log(f"Could not open the dashboard: {exc}")
                time.sleep(2)
                continue
            still = []
            for record in pending:
                phone = str(record.get("phone") or "")
                booking_id = str(record.get("booking_id") or "")
                action = "done" if record.get("status") == "coded" else "cancel"
                try:
                    outcome = self.browser.press_booking(booking_id, phone, action)
                except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not close {plus_phone(phone)}: {exc}")
                    still.append(record)
                    continue
                if outcome in {"done", "cancel"}:
                    self.store.mark(phone, outcome)
                    results.append({"phone": plus_phone(phone), "booking_id": booking_id, "action": outcome})
                    log(f"{plus_phone(phone)} {outcome}")
                else:
                    still.append(record)
            pending = still
            if pending:
                time.sleep(2)
        for record in pending:
            results.append(
                {
                    "phone": plus_phone(str(record.get("phone") or "")),
                    "booking_id": str(record.get("booking_id") or ""),
                    "action": "waiting",
                }
            )
        closed = [item for item in results if item["action"] in {"done", "cancel"}]
        payload = {"email": email, "results": results}
        if pending or len(closed) != len(records):
            payload["ok"] = False
            payload["error"] = "waiting"
            return payload
        removed = self.store.drop_phones([str(record.get("phone") or "") for record in records])
        log(f"Removed {len(removed)} number(s) for {email}")
        payload["removed"] = removed
        payload["ok"] = True
        return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: NumberService

    def log_message(self, fmt: str, *args) -> None:
        log(fmt % args)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send({"ok": True}, 200)
            return
        self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            body = self._read_json()
            if path == "/get_started":
                count = body.get("count")
                parsed = int(count) if count is not None and str(count).strip() != "" else None
                payload = self.service.get_started(
                    str(body.get("email") or ""),
                    str(body.get("type") or ""),
                    parsed,
                    price_index(body.get("price_index")),
                )
                self._send(payload, 200)
                return
            if path == "/get_code":
                phone = body.get("phone", body.get("phonenumber", ""))
                self._send(self.service.get_code(str(phone or "")), 200)
                return
            if path == "/code_passed":
                self._send(self.service.code_passed(str(body.get("email") or "")), 200)
                return
            self._send({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
        except SmsError as exc:
            log(str(exc))
            self._send({"ok": False, "error": str(exc)}, 200)
        except Exception as exc:
            log(f"Request failed: {exc}")
            self._send({"ok": False, "error": "service error"}, 500)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 100_000:
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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True


class ServiceServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def listen() -> ServiceServer:
    try:
        return ServiceServer((ANOSIM_HOST, ANOSIM_PORT), Handler)
    except OSError as exc:
        busy = getattr(exc, "winerror", None) in {10013, 10048} or exc.errno in {13, 48, 98}
        if busy:
            raise SystemExit(f"Port {ANOSIM_PORT} is already in use. Set ANOSIM_PORT in {ENV_FILE} to a free port.") from exc
        raise


def main() -> None:
    if not BASE_URL.startswith("http"):
        raise SystemExit(f"Set SERVICE_URL3 in {ENV_FILE}")
    if not ANOSIM_EMAIL or not ANOSIM_PASSWORD:
        raise SystemExit(f"Set ANOSIM_EMAIL and ANOSIM_PASSWORD in {ENV_FILE}")
    if not 1 <= ANOSIM_PORT <= 65535 or not 1 <= DEBUG_PORT <= 65535:
        raise SystemExit("ANOSIM_PORT and ANOSIM_DEBUG_PORT must be valid ports")
    wanted = order_count()
    service = NumberService()
    Handler.service = service
    server = listen()
    log(f"Opening {BASE_URL}")
    try:
        service.open()
    except Exception:
        server.server_close()
        raise
    log(f"Listening on http://{ANOSIM_HOST}:{ANOSIM_PORT}")
    log(f"Ordering {wanted} {COUNTRY} numbers with one Book number click")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SmsError as exc:
        raise SystemExit(str(exc)) from exc

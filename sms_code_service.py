"""Long-running phone-number service for SERVICE_URL1.

Keeps one browser signed in to the number dashboard and serves three calls:

    POST /get_started   {"email": "name@gmx.net", "type": "gmx"}
    POST /get_code      {"phone": "4915907997950"}
    POST /code_passed   {"email": "name@gmx.net"}

`type` is `gmx` or `webde`. Each get_started types SMS_CODE_ORDER_COUNT into
the quantity field, clicks Create Order once, waits 5 seconds, and saves the
new numbers under that email in sms_orders.json. Each get_code opens its own
page immediately, waits up to 5 minutes for an OTP, and closes that page when
it returns. code_passed clicks Done, or Cancel when Done is still disabled,
then deletes the saved phone numbers for that email.

Several get_started calls at once wait in a queue and run one after another.
get_code does not wait in that queue: every lookup opens a page of its own.

Requires SMS_CODE_EMAIL and SMS_CODE_PASSWORD in .env, plus SERVICE_URL1.
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

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STORE_FILE = ROOT / "sms_orders.json"
PROFILE_DIR = ROOT / "sms_browser"
OTP_WAIT_SECONDS = 300
ORDER_WAIT_SECONDS = 30
CLOSE_WAIT_SECONDS = 180
PILL_IDS = {"webde": "99", "gmx": "97"}
PILL_LABELS = {"webde": "WEBDE", "gmx": "GMX"}


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
BASE_URL = os.environ.get("SERVICE_URL1", "").strip().rstrip("/")
SMS_CODE_EMAIL = os.environ.get("SMS_CODE_EMAIL", "").strip()
SMS_CODE_PASSWORD = os.environ.get("SMS_CODE_PASSWORD", "").strip()
SMS_HOST = os.environ.get("SMS_HOST", "127.0.0.1").strip() or "127.0.0.1"
SMS_CODE_PORT = int(os.environ.get("SMS_CODE_PORT", "8780") or "8780")
DEBUG_PORT = int(os.environ.get("SMS_DEBUG_PORT", "9333") or "9333")
HEADLESS = os.environ.get("SMS_HEADLESS", "").strip() == "1"


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


def parse_otp(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned or "waiting for sms" in cleaned.lower():
        return ""
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", cleaned)
    return match.group(1) if match else ""


def parse_price(attr: str, text: str) -> float | None:
    raw = (attr or "").strip().replace(",", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    shown = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    if not shown:
        return None
    try:
        return float(shown.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_stock(attr: str, text: str) -> int | None:
    raw = (attr or "").strip().replace(",", "").replace(".", "")
    if raw.isdigit():
        return int(raw)
    match = re.search(r"(\d+)", (text or "").replace(",", "").replace(".", ""))
    return int(match.group(1)) if match else None


def is_germany(label: str) -> bool:
    text = (label or "").lower()
    return "germany" in text or "deutschland" in text


def normalize_type(value: str) -> str:
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    if key == "gmx":
        return "gmx"
    if key == "webde":
        return "webde"
    raise ValueError("type must be gmx or webde")


def order_count(override: int | None = None) -> int:
    if override is None:
        raw = os.environ.get("SMS_CODE_ORDER_COUNT", "3").strip() or "3"
        if not raw.isdigit():
            raise ValueError("SMS_CODE_ORDER_COUNT must be a number from 1 to 100")
        override = int(raw)
    if override < 1 or override > 100:
        raise ValueError("count must be from 1 to 100")
    return override


class SmsError(Exception):
    pass


class OrderStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"emails": {}, "phones": {}}
        if not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            emails = loaded.get("emails")
            phones = loaded.get("phones")
            if isinstance(emails, dict):
                self.data["emails"] = emails
            if isinstance(phones, dict):
                self.data["phones"] = phones

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def add(self, email: str, kind: str, phone: str, order_id: str, product_id: str) -> None:
        with self._lock:
            self._add(email, kind, phone, order_id, product_id)

    def _add(self, email: str, kind: str, phone: str, order_id: str, product_id: str) -> None:
        key = digits(phone) or f"order:{order_id}"
        entry = self.data["emails"].setdefault(email, {"type": kind, "phones": []})
        entry["type"] = kind
        phones = entry.setdefault("phones", [])
        if key not in phones:
            phones.append(key)
        self.data["phones"][key] = {
            "email": email,
            "type": kind,
            "phone": phone,
            "order_id": order_id,
            "product_id": product_id,
            "status": "active",
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.save()

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
            return dict(record) if isinstance(record, dict) else None

    def mark(self, phone: str, order_id: str, status: str) -> None:
        with self._lock:
            self._mark(phone, order_id, status)

    def _mark(self, phone: str, order_id: str, status: str) -> None:
        record = self.data["phones"].get(digits(phone)) if phone else None
        if not isinstance(record, dict):
            record = None
        if record is None:
            for item in self.data["phones"].values():
                if isinstance(item, dict) and str(item.get("order_id") or "") == order_id:
                    record = item
                    break
        if record is None:
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
        for key, record in list(self.data["phones"].items()):
            if isinstance(record, dict) and record.get("email") == email and key not in keys:
                keys.append(key)
        removed = []
        for key in keys:
            record = self.data["phones"].pop(key, None)
            if isinstance(record, dict) and record.get("phone"):
                removed.append(str(record["phone"]))
            else:
                removed.append(key)
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
    configured = os.environ.get("SMS_BROWSER", "").strip().strip('"')
    if configured:
        if not Path(configured).is_file():
            raise SystemExit(f"SMS_BROWSER was not found: {configured}")
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
    raise SystemExit("Install Chrome or Edge, or set SMS_BROWSER to the browser executable.")


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


def browser_debugger_url(port: int) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("Browser DevTools version was not an object")
    url = str(payload.get("webSocketDebuggerUrl") or "")
    if not url:
        raise RuntimeError("Browser DevTools endpoint is missing")
    return url


class Dashboard:
    def __init__(self):
        self.cdp: Cdp | None = None
        self.socket: CdpSocket | None = None
        self._tab_id = ""
        self._launch_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.login_url = BASE_URL + "/auth/login"
        self.dashboard_url = BASE_URL + "/dashboard"

    def _browser_call(self, method: str, params: dict | None = None) -> dict:
        ws = CdpSocket(browser_debugger_url(DEBUG_PORT))
        try:
            return Cdp(ws).call(method, params, timeout=30)
        finally:
            ws.close()

    def _ensure_process(self) -> None:
        if port_open(DEBUG_PORT):
            return
        with self._launch_lock:
            if port_open(DEBUG_PORT):
                return
            self.launch()
            if self.cdp is None:
                self.attach()

    def spawn_lookup(self, number: str) -> Dashboard:
        self._ensure_process()
        created = self._browser_call("Target.createTarget", {"url": self.dashboard_url})
        target_id = str(created.get("targetId") or "")
        if not target_id:
            raise SmsError("Could not open a new page")
        log(f"Opened a new page for {number}")
        deadline = time.time() + 20
        page = None
        while time.time() < deadline:
            try:
                pages = list_pages(DEBUG_PORT)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError):
                time.sleep(0.3)
                continue
            page = next((item for item in pages if item.get("id") == target_id and item.get("webSocketDebuggerUrl")), None)
            if page:
                break
            time.sleep(0.3)
        lookup = Dashboard()
        lookup.proc = self.proc
        lookup._tab_id = target_id
        if not page:
            lookup.close_lookup()
            raise SmsError("New page did not appear")
        try:
            lookup.socket = CdpSocket(str(page["webSocketDebuggerUrl"]))
            lookup.cdp = Cdp(lookup.socket)
            try:
                lookup._browser_call("Target.activateTarget", {"targetId": target_id})
            except (OSError, RuntimeError, TimeoutError):
                pass
            lookup._wait_until_loaded()
        except Exception:
            lookup.close_lookup()
            raise
        if "/auth/login" in lookup.href() or not lookup.wait_css("button.pill", 8):
            lookup.login()
        return lookup

    def _wait_until_loaded(self) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                value = str(self.evaluate("document.readyState + '\\n' + location.href", timeout=10) or "")
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            state, _, current = value.partition("\n")
            if state == "complete" and current and not current.startswith("about:"):
                self.install_hooks()
                return
            time.sleep(0.4)
        self.install_hooks()

    def close_lookup(self) -> None:
        target_id = self._tab_id
        self._tab_id = ""
        self._drop_socket()
        if not target_id:
            return
        try:
            self._browser_call("Target.closeTarget", {"targetId": target_id})
            log("Closed the code page")
        except (OSError, RuntimeError, TimeoutError) as exc:
            log(f"Could not close the code page: {exc}")

    def ensure_browser(self) -> None:
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
        args.append("about:blank")
        log("Starting the number dashboard browser")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            if port_open(DEBUG_PORT):
                return
            if self.proc.poll() is not None:
                raise SmsError("The browser closed during startup")
            time.sleep(0.3)
        raise SmsError(f"Browser did not open DevTools on port {DEBUG_PORT}")

    def attach(self) -> None:
        deadline = time.time() + 30
        last_error = "no page"
        while time.time() < deadline:
            try:
                pages = list_pages(DEBUG_PORT)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = str(exc)
                time.sleep(0.4)
                continue
            if not pages:
                last_error = "browser has no page yet"
                time.sleep(0.4)
                continue
            host = urllib.parse.urlparse(BASE_URL).netloc
            pages.sort(key=lambda item: host in str(item.get("url") or ""), reverse=True)
            try:
                self.socket = CdpSocket(str(pages[0]["webSocketDebuggerUrl"]))
                self.cdp = Cdp(self.socket)
                self.evaluate("1", timeout=10)
                return
            except (OSError, RuntimeError, TimeoutError) as exc:
                last_error = str(exc)
                self._drop_socket()
                time.sleep(0.4)
        raise SmsError(f"Could not attach to the browser on port {DEBUG_PORT}: {last_error}")

    def _drop_socket(self) -> None:
        if self.socket is not None:
            self.socket.close()
        self.socket = None
        self.cdp = None

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
        value = self.evaluate("location.href")
        return str(value or "")

    def goto(self, url: str) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        self.cdp.call("Page.enable", timeout=15)
        self.cdp.call("Page.navigate", {"url": url}, timeout=45)
        deadline = time.time() + 45
        last = ""
        while time.time() < deadline:
            try:
                value = str(self.evaluate("document.readyState + '\\n' + location.href", timeout=10) or "")
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            state, _, current = value.partition("\n")
            last = current or last
            if state == "complete" and current and not current.startswith("about:") and not current.startswith("chrome"):
                self.install_hooks()
                return
            time.sleep(0.4)
        if last:
            self.install_hooks()
            return
        raise SmsError(f"Page did not finish loading: {url}")

    def install_hooks(self) -> None:
        try:
            self.evaluate(
                "(() => { window.confirm = () => true; window.alert = () => {}; window.prompt = () => ''; return true; })()",
                timeout=10,
            )
        except (RuntimeError, TimeoutError, OSError):
            pass

    def wait_css(self, selector: str, timeout: float) -> bool:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false;"
            " const style = getComputedStyle(el);"
            " const rect = el.getBoundingClientRect();"
            " return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1;"
            "})()"
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expression, timeout=10):
                    return True
            except (RuntimeError, TimeoutError, OSError):
                pass
            time.sleep(0.4)
        return False

    def fill(self, selector: str, value: str) -> None:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return '';"
            " el.focus();"
            " const proto = Object.getPrototypeOf(el);"
            " const desc = Object.getOwnPropertyDescriptor(proto, 'value')"
            "   || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
            f" desc.set.call(el, {json.dumps(value)});"
            " el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: el.value}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return el.value;"
            "})()"
        )
        typed = self.evaluate(expression)
        if typed != value:
            raise SmsError(f"Could not fill {selector}")

    def click_selector(self, selector: str) -> bool:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " el.click();"
            " return true;"
            "})()"
        )
        return bool(self.evaluate(expression))

    def page_error(self) -> str:
        value = self.evaluate(
            "(() => {"
            " const nodes = [...document.querySelectorAll('[role=alert], .error, .auth-error, .form-error, .result-title')];"
            " return nodes.map((node) => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 4).join(' | ');"
            "})()"
        )
        return str(value or "").strip()

    def on_dashboard(self) -> bool:
        current = self.href()
        return "/dashboard" in current and "/auth/login" not in current

    def ensure_session(self) -> None:
        self.ensure_browser()
        current = ""
        try:
            current = self.href()
        except (RuntimeError, TimeoutError, OSError):
            current = ""
        if "/dashboard" in current and self.wait_css("button.pill", 5):
            self.install_hooks()
            return
        self.login()

    def login(self) -> None:
        log(f"Signing in as {SMS_CODE_EMAIL}")
        self.goto(self.login_url)
        if self.on_dashboard() and self.wait_css("button.pill", 8):
            log("Already signed in")
            return
        if not self.wait_css("#email", 30) or not self.wait_css("#password", 10):
            if self.on_dashboard():
                log("Already signed in")
                return
            raise SmsError("Could not find the login form")
        self.fill("#email", SMS_CODE_EMAIL)
        self.fill("#password", SMS_CODE_PASSWORD)
        if not self.click_selector("#login-btn"):
            raise SmsError("Could not find the Sign in button")
        if self._wait_dashboard(25):
            log("Signed in")
            return
        self.evaluate(
            "(() => { const form = document.querySelector('#login-btn') && document.querySelector('#login-btn').form;"
            " if (form && form.requestSubmit) form.requestSubmit(); return true; })()"
        )
        if self._wait_dashboard(15):
            log("Signed in")
            return
        detail = ""
        try:
            detail = self.page_error()
        except (RuntimeError, TimeoutError, OSError):
            detail = ""
        raise SmsError(detail or "Sign in did not open the dashboard")

    def _wait_dashboard(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.on_dashboard() and self.wait_css("button.pill", 2):
                    self.install_hooks()
                    return True
            except (RuntimeError, TimeoutError, OSError):
                pass
            time.sleep(0.4)
        return False

    def select_provider(self, kind: str) -> None:
        if not self.on_dashboard():
            self.goto(self.dashboard_url)
        if not self.wait_css("button.pill", 20):
            raise SmsError("Could not find the GMX and WEBDE options")
        pill_id = PILL_IDS[kind]
        label = PILL_LABELS[kind]
        expression = (
            "(() => {"
            f" const id = {json.dumps(pill_id)};"
            f" const label = {json.dumps(label)};"
            " const pills = [...document.querySelectorAll('button.pill')];"
            " const byId = pills.find((node) => node.getAttribute('data-id') === id);"
            " const byText = pills.find((node) => (node.innerText || '').replace(/\\s+/g, '').toUpperCase().includes(label));"
            " const el = byId || byText;"
            " if (!el) return false;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " el.click();"
            " return true;"
            "})()"
        )
        if not self.evaluate(expression):
            raise SmsError(f"Could not find the {label} option")
        log(f"Selected {label}")
        time.sleep(0.8)

    def list_tiles(self) -> list[dict]:
        value = self.evaluate(
            "(() => {"
            " return [...document.querySelectorAll('button.product-tile')].map((el) => {"
            "   const style = getComputedStyle(el);"
            "   const rect = el.getBoundingClientRect();"
            "   const stock = (el.querySelector('.tile-stock') || {}).textContent || '';"
            "   const price = (el.querySelector('.tile-price') || {}).textContent || '';"
            "   const label = ((el.querySelector('.tile-label') || {}).textContent || '').replace(/\\s+/g, ' ').trim();"
            "   return {"
            "     id: el.getAttribute('data-product-id') || '',"
            "     label,"
            "     priceAttr: el.getAttribute('data-price') || '',"
            "     priceText: price.replace(/\\s+/g, ' ').trim(),"
            "     stockAttr: el.getAttribute('data-stock') || '',"
            "     stockText: stock.replace(/\\s+/g, ' ').trim(),"
            "     visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1"
            "   };"
            " }).filter((item) => item.visible);"
            "})()"
        )
        if not isinstance(value, list):
            return []
        tiles = []
        for item in value:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "")
            if not is_germany(label):
                continue
            product_id = str(item.get("id") or "")
            if not product_id:
                continue
            price_text = str(item.get("priceText") or "")
            tiles.append(
                {
                    "id": product_id,
                    "label": label,
                    "price": parse_price(str(item.get("priceAttr") or ""), price_text),
                    "price_text": price_text,
                    "stock": parse_stock(str(item.get("stockAttr") or ""), str(item.get("stockText") or "")),
                }
            )
        tiles.sort(key=lambda tile: (tile["price"] is None, tile["price"] if tile["price"] is not None else 0))
        return tiles

    def wait_tiles(self, timeout: float = 12) -> list[dict]:
        deadline = time.time() + timeout
        seen: list[dict] = []
        while time.time() < deadline:
            try:
                seen = self.list_tiles()
            except (RuntimeError, TimeoutError, OSError):
                seen = []
            if seen:
                return seen
            time.sleep(0.4)
        return seen

    def cards(self) -> list[dict]:
        value = self.evaluate(
            "(() => {"
            " const root = document.querySelector('.active-orders-grid') || document;"
            " return [...root.querySelectorAll('.active-order-card')].map((card) => {"
            "   const phoneEl = card.querySelector('.ao-phone');"
            "   const copy = card.querySelector('.ao-copy-btn');"
            "   const otp = card.querySelector('.ao-otp-field .ao-field-value');"
            "   const phone = ((phoneEl && phoneEl.textContent) || (copy && copy.getAttribute('data-copy')) || '').trim();"
            "   return {"
            "     order_id: card.getAttribute('data-order-id') || '',"
            "     product_id: card.getAttribute('data-product-id') || '',"
            "     phone,"
            "     otp_text: otp ? (otp.innerText || '').replace(/\\s+/g, ' ').trim() : '',"
            "     waiting: !!(otp && otp.querySelector('.ao-waiting')),"
            "     cancel_at: card.getAttribute('data-cancel-available-at') || ''"
            "   };"
            " });"
            "})()"
        )
        if not isinstance(value, list):
            return []
        cards = []
        for item in value:
            if isinstance(item, dict) and item.get("order_id"):
                cards.append(
                    {
                        "order_id": str(item.get("order_id") or ""),
                        "product_id": str(item.get("product_id") or ""),
                        "phone": str(item.get("phone") or "").strip(),
                        "otp_text": str(item.get("otp_text") or ""),
                        "waiting": bool(item.get("waiting")),
                        "cancel_at": str(item.get("cancel_at") or ""),
                    }
                )
        return cards

    def order_state(self) -> dict:
        value = self.evaluate(
            "(() => {"
            " const el = document.querySelector('#order-result');"
            " const spinner = document.querySelector('#order-btn .btn-spinner');"
            " let shown = false;"
            " if (el) {"
            "   const style = getComputedStyle(el);"
            "   shown = el.classList.contains('order-result-error') && !el.classList.contains('is-hidden') && style.display !== 'none';"
            " }"
            " return {"
            "   errorShown: shown,"
            "   errorText: shown ? (el.innerText || '').replace(/\\s+/g, ' ').trim() : '',"
            "   loading: !!(spinner && !spinner.classList.contains('is-hidden'))"
            " };"
            "})()"
        )
        if not isinstance(value, dict):
            return {"error_shown": False, "error_text": "", "loading": False}
        return {
            "error_shown": bool(value.get("errorShown")),
            "error_text": str(value.get("errorText") or ""),
            "loading": bool(value.get("loading")),
        }

    def known_order_ids(self) -> set[str]:
        previous: set[str] | None = None
        last: set[str] = set()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                last = {card["order_id"] for card in self.cards()}
            except (RuntimeError, TimeoutError, OSError):
                last = set()
            if previous is not None and last == previous:
                return last
            previous = last
            time.sleep(0.35)
        return last

    def _fresh_cards(self, before: set[str], product_id: str) -> list[dict]:
        fresh = [card for card in self.cards() if card["order_id"] not in before]
        matched = [card for card in fresh if card["product_id"] == product_id]
        return matched or fresh

    def attempt_order(self, product_id: str, count: int) -> dict:
        before = self.known_order_ids()
        expression = (
            "(() => {"
            f" const id = {json.dumps(product_id)};"
            " const el = [...document.querySelectorAll('button.product-tile')]"
            "   .find((node) => node.getAttribute('data-product-id') === id);"
            " if (!el) return false;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " el.click();"
            " return true;"
            "})()"
        )
        if not self.evaluate(expression):
            return {"status": "error", "text": "Germany option disappeared before the click"}
        time.sleep(0.4)
        if not self.wait_css("#qty-input", 8):
            return {"status": "error", "text": "Could not find the quantity field"}
        self.fill("#qty-input", str(count))
        log(f"Set order quantity to {count}")
        deadline = time.time() + 8
        while time.time() < deadline:
            enabled = self.evaluate(
                "(() => { const button = document.querySelector('#order-btn');"
                " return !!(button && !button.disabled); })()"
            )
            if enabled:
                break
            time.sleep(0.3)
        else:
            return {"status": "error", "text": "Create Order stayed disabled"}
        before_state = self.order_state()
        if not self.click_selector("#order-btn"):
            clicked = self.evaluate(
                "(() => {"
                " const button = [...document.querySelectorAll('button')]"
                "   .find((node) => (node.innerText || '').replace(/\\s+/g, ' ').trim() === 'Create Order');"
                " if (!button) return false;"
                " button.scrollIntoView({block: 'center', inline: 'center'});"
                " button.click();"
                " return true;"
                "})()"
            )
            if not clicked:
                return {"status": "error", "text": "Could not find Create Order"}
        log(f"Creating one order of {count} for product {product_id}")
        log("Waiting 5 seconds for the order")
        time.sleep(5)
        try:
            state = self.order_state()
            created = [card for card in self._fresh_cards(before, product_id) if card["phone"]]
        except (RuntimeError, TimeoutError, OSError):
            state = {"error_shown": False, "error_text": "", "loading": False}
            created = []
        if created:
            return {"status": "ok", "cards": created}
        if state["error_shown"] and not state["loading"] and (
            not before_state["error_shown"] or state["error_text"] != before_state["error_text"]
        ):
            return {"status": "error", "text": state["error_text"] or "No numbers available right now. Try another option."}
        deadline = time.time() + ORDER_WAIT_SECONDS
        while time.time() < deadline:
            try:
                created = [card for card in self._fresh_cards(before, product_id) if card["phone"]]
                state = self.order_state()
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            if created:
                return {"status": "ok", "cards": created}
            if state["error_shown"] and not state["loading"]:
                return {"status": "error", "text": state["error_text"] or "No numbers available right now. Try another option."}
            time.sleep(0.4)
        if created:
            return {"status": "ok", "cards": created}
        return {"status": "error", "text": "Create Order timed out"}

    def finish_order(self, order_id: str) -> str:
        self.install_hooks()
        expression = (
            "(() => {"
            f" const id = {json.dumps(order_id)};"
            " const card = [...document.querySelectorAll('.active-order-card')]"
            "   .find((node) => node.getAttribute('data-order-id') === id);"
            " if (!card) return 'missing';"
            " const enabled = (button) => button && !button.disabled && button.getAttribute('aria-disabled') !== 'true';"
            " const press = (button) => { button.scrollIntoView({block: 'center', inline: 'center'}); button.click(); };"
            " const done = card.querySelector('.ao-btn-finish');"
            " const cancel = card.querySelector('.ao-btn-cancel');"
            " if (enabled(done)) { press(done); return 'done'; }"
            " if (enabled(cancel)) { press(cancel); return 'cancel'; }"
            " return 'waiting';"
            "})()"
        )
        value = self.evaluate(expression)
        if value in {"done", "cancel", "waiting", "missing"}:
            return str(value)
        return "waiting"


class SmsService:
    def __init__(self):
        self.browser = Dashboard()
        self.store = OrderStore(STORE_FILE)
        self.jobs: queue.Queue = queue.Queue()
        self._state = threading.Lock()
        self._active = False
        self._waiting = 0
        self._worker = threading.Thread(target=self._run_queue, name="sms-queue", daemon=True)
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

    def get_started(self, email: str, kind: str, count: int | None = None) -> dict:
        email = email.strip()
        if "@" not in email or len(email) > 200:
            raise ValueError("email is invalid")
        provider = normalize_type(kind)
        wanted = order_count(count)
        return self._submit(
            f"get_started {email} ({provider})",
            lambda email=email, provider=provider, wanted=wanted: self._get_started(email, provider, wanted),
        )

    def _get_started(self, email: str, kind: str, count: int) -> dict:
        self.browser.ensure_session()
        self.browser.goto(self.browser.dashboard_url)
        self.browser.select_provider(kind)
        self.browser.wait_tiles()
        exhausted: set[str] = set()
        phones: list[str] = []
        orders: list[dict] = []
        attempts = 0
        while not orders and attempts < 6:
            attempts += 1
            tiles = [tile for tile in self.browser.list_tiles() if tile["id"] not in exhausted and tile["stock"] != 0]
            if not tiles:
                log("No Germany numbers with stock remain")
                break
            tile = tiles[0]
            price_label = tile["price_text"] or (tile["price"] if tile["price"] is not None else "unknown price")
            stock_label = tile["stock"] if tile["stock"] is not None else "unknown"
            log(f"Ordering cheapest Germany product {tile['id']} at {price_label} ({stock_label} avail)")
            try:
                result = self.browser.attempt_order(tile["id"], count)
            except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                log(f"Order click failed: {exc}")
                exhausted.add(tile["id"])
                continue
            if result["status"] != "ok":
                log(result.get("text") or "Order was rejected")
                exhausted.add(tile["id"])
                continue
            for card in result.get("cards") or []:
                phone = str(card.get("phone") or "").strip()
                order_id = str(card.get("order_id") or "")
                if not order_id or any(item["order_id"] == order_id for item in orders):
                    continue
                self.store.add(email, kind, phone, order_id, card.get("product_id") or tile["id"])
                orders.append({"phone": phone, "order_id": order_id, "product_id": card.get("product_id") or tile["id"]})
                if phone:
                    phones.append(phone)
                    log(f"Saved {phone} for {email}")
        payload = {"email": email, "type": kind, "phones": phones, "orders": orders}
        if not phones:
            payload["ok"] = False
            payload["error"] = "failed get number"
            return payload
        payload["ok"] = True
        return payload

    def get_code(self, phone: str) -> dict:
        number = digits(phone)
        if len(number) < 6:
            raise ValueError("phone is invalid")
        record = self.store.find_phone(number) or {}
        kind = str(record.get("type") or "gmx")
        order_id = str(record.get("order_id") or "")
        try:
            kind = normalize_type(kind)
        except ValueError:
            kind = "gmx"
        page = self.browser.spawn_lookup(number)
        try:
            return self._get_code(page, number, kind, order_id)
        finally:
            page.close_lookup()

    def _get_code(self, page: Dashboard, number: str, kind: str, order_id: str) -> dict:
        deadline = time.monotonic() + OTP_WAIT_SECONDS
        next_reload = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_reload:
                try:
                    page.goto(page.dashboard_url)
                    page.select_provider(kind)
                except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not refresh the code page for {number}: {exc}")
                next_reload = time.monotonic() + 15
            code, found_id = self._read_code(page, number, order_id)
            if found_id:
                order_id = found_id
            if code:
                log(f"OTP for {number} is {code}")
                return {"ok": True, "phone": number, "code": code, "order_id": order_id}
            time.sleep(2)
        log(f"No OTP for {number} within 5 minutes")
        return {"ok": False, "error": "failed", "phone": number, "order_id": order_id}

    def _read_code(self, page: Dashboard, number: str, order_id: str) -> tuple[str, str]:
        try:
            cards = page.cards()
        except (RuntimeError, TimeoutError, OSError):
            return "", order_id
        matches = [card for card in cards if same_phone(card["phone"], number)]
        if not matches and order_id:
            matches = [card for card in cards if card["order_id"] == order_id]
        if not matches:
            return "", order_id
        card = matches[0]
        if card["waiting"]:
            return "", card["order_id"]
        return parse_otp(card["otp_text"]), card["order_id"]

    def code_passed(self, email: str) -> dict:
        email = email.strip()
        if "@" not in email:
            raise ValueError("email is invalid")
        return self._submit(f"code_passed {email}", lambda email=email: self._code_passed(email))

    def _code_passed(self, email: str) -> dict:
        records = [item for item in self.store.phones_for(email) if item.get("status") == "active" and item.get("order_id")]
        if not records:
            return {"ok": False, "error": "unknown email", "email": email, "results": []}
        kind = str(records[0].get("type") or "gmx")
        try:
            kind = normalize_type(kind)
        except ValueError:
            kind = "gmx"
        self.browser.ensure_session()
        self.browser.goto(self.browser.dashboard_url)
        self.browser.select_provider(kind)
        pending = list(records)
        results = []
        deadline = time.time() + CLOSE_WAIT_SECONDS
        next_reload = time.time() + 15
        while pending and time.time() < deadline:
            if time.time() >= next_reload:
                try:
                    self.browser.goto(self.browser.dashboard_url)
                    self.browser.select_provider(kind)
                except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not refresh the dashboard: {exc}")
                next_reload = time.time() + 15
            still = []
            for record in pending:
                order_id = str(record.get("order_id") or "")
                phone = str(record.get("phone") or "")
                try:
                    action = self.browser.finish_order(order_id)
                except (RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not close order {order_id}: {exc}")
                    still.append(record)
                    continue
                if action in {"done", "cancel"}:
                    self.store.mark(phone, order_id, action)
                    results.append({"phone": phone, "order_id": order_id, "action": action})
                    log(f"Order {order_id} {action}")
                else:
                    still.append(record)
            pending = still
            if pending:
                time.sleep(2)
        for record in pending:
            results.append(
                {
                    "phone": str(record.get("phone") or ""),
                    "order_id": str(record.get("order_id") or ""),
                    "action": "waiting",
                }
            )
        closed = [item for item in results if item["action"] in {"done", "cancel"}]
        removed = self.store.remove_email(email)
        log(f"Removed {len(removed)} phone numbers for {email}")
        payload = {"email": email, "results": results, "removed": removed}
        if pending or len(closed) != len(records):
            payload["ok"] = False
            payload["error"] = "waiting"
            return payload
        payload["ok"] = True
        return payload


class Handler(BaseHTTPRequestHandler):
    service: SmsService

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
                payload = self.service.get_started(str(body.get("email") or ""), str(body.get("type") or ""), parsed)
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServiceServer(ThreadingHTTPServer):
    # Windows reports WinError 10013 when SO_REUSEADDR is set and the port is taken.
    allow_reuse_address = False
    daemon_threads = True


def listen() -> ServiceServer:
    try:
        return ServiceServer((SMS_HOST, SMS_CODE_PORT), Handler)
    except OSError as exc:
        busy = getattr(exc, "winerror", None) in {10013, 10048} or exc.errno in {13, 48, 98}
        if busy:
            raise SystemExit(
                f"Port {SMS_CODE_PORT} is already in use. Set SMS_CODE_PORT in {ENV_FILE} to a free port."
            ) from exc
        raise


def main() -> None:
    if not BASE_URL:
        raise SystemExit(f"Set SERVICE_URL1 in {ENV_FILE}")
    if not SMS_CODE_EMAIL or not SMS_CODE_PASSWORD:
        raise SystemExit(f"Set SMS_CODE_EMAIL and SMS_CODE_PASSWORD in {ENV_FILE}")
    if not 1 <= SMS_CODE_PORT <= 65535 or not 1 <= DEBUG_PORT <= 65535:
        raise SystemExit("SMS_CODE_PORT and SMS_DEBUG_PORT must be valid ports")
    wanted = order_count()
    service = SmsService()
    Handler.service = service
    server = listen()
    log("Opening the number dashboard")
    try:
        service.open()
    except Exception:
        server.server_close()
        raise
    log(f"Listening on http://{SMS_HOST}:{SMS_CODE_PORT}")
    log(f"Ordering {wanted} German numbers per get_started call")
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

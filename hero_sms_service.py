"""Long-running phone-number service for TARGET_URL3.

Keeps one browser signed in and serves three calls:

    POST /get_started   {"email": "name@gmx.net", "type": "gmx"}
    POST /get_code      {"phone": "4915212225017"}
    POST /code_passed   {"email": "name@gmx.net"}

`type` is `gmx` or `webde`. Each get_started searches that service, chooses
Germany, and buys HERO_SMS_ORDER_COUNT numbers (default 3, maximum 4). It always
picks the cheapest price whose stock is above zero. Numbers are saved under
that email in kka_orders.json. get_code waits up to 5 minutes for an OTP.
code_passed clicks the check mark on numbers whose code was read, and the
close icon on the other numbers for that email. It then deletes that
email's phone numbers from kka_orders.json.

Calls wait in one queue so the browser does one thing at a time.

Requires HERO_SMS_EMAIL and HERO_SMS_PASSWORD in .env, plus TARGET_URL3.
Listens on HERO_SMS_PORT (default 8781) so it can run beside the other number service.
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
STORE_FILE = ROOT / "kka_orders.json"
PROFILE_DIR = ROOT / "kka_browser"
OTP_WAIT_SECONDS = 300
ORDER_WAIT_SECONDS = 30
CLOSE_WAIT_SECONDS = 180
CAPTCHA_WAIT_SECONDS = 90


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
BASE_URL = os.environ.get("TARGET_URL3", "").strip().rstrip("/")
HERO_SMS_EMAIL = os.environ.get("HERO_SMS_EMAIL", "").strip()
HERO_SMS_PASSWORD = os.environ.get("HERO_SMS_PASSWORD", "").strip()
HERO_SMS_HOST = os.environ.get("HERO_SMS_HOST", "127.0.0.1").strip() or "127.0.0.1"
HERO_SMS_PORT = int(os.environ.get("HERO_SMS_PORT", "8781") or "8781")
DEBUG_PORT = int(os.environ.get("HERO_SMS_DEBUG_PORT", "9334") or "9334")
HEADLESS = os.environ.get("HERO_SMS_HEADLESS", "").strip() == "1"
COUNTRY = os.environ.get("HERO_SMS_COUNTRY", "germany").strip() or "germany"
SERVICE_QUERY = {
    "gmx": os.environ.get("HERO_SMS_GMX_QUERY", "gmx").strip() or "gmx",
    "webde": os.environ.get("HERO_SMS_WEBDE_QUERY", "webde").strip() or "webde",
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


def parse_otp(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned or "waiting for sms" in cleaned.lower():
        return ""
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", cleaned)
    return match.group(1) if match else ""


def parse_money(value: str, text: str) -> float | None:
    raw = (value or "").strip().replace(",", "")
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


def parse_pcs(text: str) -> int | None:
    match = re.search(r"(\d[\d\s.,]*)\s*pcs", text or "", re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1).strip().replace(" ", "").replace(",", "")
    if re.fullmatch(r"\d+\.\d{3}", raw):
        raw = raw.replace(".", "")
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    return None


def normalize_type(value: str) -> str:
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    if key == "gmx":
        return "gmx"
    if key == "webde":
        return "webde"
    raise ValueError("type must be gmx or webde")


def order_count(override: int | None = None) -> int:
    if override is None:
        raw = os.environ.get("HERO_SMS_ORDER_COUNT", "3").strip() or "3"
        if not raw.isdigit():
            raise ValueError("HERO_SMS_ORDER_COUNT must be a number from 1 to 4")
        override = int(raw)
    if override < 1 or override > 4:
        raise ValueError("count must be from 1 to 4")
    return override


def clock_distance(text: str, now: datetime.datetime | None = None) -> int:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not match:
        return 24 * 60
    current = now or datetime.datetime.now()
    then = int(match.group(1)) * 60 + int(match.group(2))
    diff = abs(current.hour * 60 + current.minute - then)
    return min(diff, 24 * 60 - diff)


class SmsError(Exception):
    pass


class OrderStore:
    def __init__(self, path: Path):
        self.path = path
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

    def add(self, email: str, kind: str, phone: str, price: str, bought_at: str) -> None:
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
            "price": price,
            "time": bought_at,
            "status": "active",
            "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.save()

    def phones_for(self, email: str) -> list[dict]:
        entry = self.data["emails"].get(email) or {}
        found = []
        for key in entry.get("phones") or []:
            record = self.data["phones"].get(key)
            if isinstance(record, dict):
                found.append(record)
        return found

    def find_phone(self, phone: str) -> dict | None:
        record = self.data["phones"].get(digits(phone))
        return record if isinstance(record, dict) else None

    def mark(self, phone: str, status: str) -> None:
        record = self.find_phone(phone)
        if record is None:
            return
        record["status"] = status
        self.save()

    def remove_email(self, email: str) -> list[str]:
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
    configured = os.environ.get("HERO_SMS_BROWSER", "").strip().strip('"') or os.environ.get("SMS_BROWSER", "").strip().strip('"')
    if configured:
        if not Path(configured).is_file():
            raise SystemExit(f"HERO_SMS_BROWSER was not found: {configured}")
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
    raise SystemExit("Install Chrome or Edge, or set HERO_SMS_BROWSER to the browser executable.")


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


VISIBLE_FN = (
    " const visible = (el) => {"
    "   if (!el) return false;"
    "   const style = getComputedStyle(el);"
    "   const rect = el.getBoundingClientRect();"
    "   return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1;"
    " };"
    " const norm = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();"
)


class Dashboard:
    def __init__(self):
        self.cdp: Cdp | None = None
        self.socket: CdpSocket | None = None
        self.proc: subprocess.Popen | None = None
        self._last_captcha_click = 0.0

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
        args.append(BASE_URL or "about:blank")
        log("Starting the number browser")
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
        host = urllib.parse.urlparse(BASE_URL).netloc
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
            pages.sort(key=lambda item: host in str(item.get("url") or ""), reverse=True)
            try:
                self.socket = CdpSocket(str(pages[0]["webSocketDebuggerUrl"]))
                self.cdp = Cdp(self.socket)
                self.evaluate("1", timeout=10)
                try:
                    self.cdp.call("Page.bringToFront", timeout=5)
                except (RuntimeError, TimeoutError, OSError):
                    pass
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

    def mouse_click(self, x: float, y: float) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        )
        time.sleep(0.05)
        self.cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    def press_key(self, key: str, code: str, virtual_key: int, modifiers: int = 0) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        params = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": virtual_key,
            "nativeVirtualKeyCode": virtual_key,
            "modifiers": modifiers,
        }
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", **params})
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **params})

    def press_enter(self) -> None:
        self.press_key("Enter", "Enter", 13)

    def install_hooks(self) -> None:
        try:
            self.evaluate(
                "(() => { window.confirm = () => true; window.alert = () => {}; window.prompt = () => ''; return true; })()",
                timeout=10,
            )
        except (RuntimeError, TimeoutError, OSError):
            pass

    def href(self) -> str:
        value = self.evaluate("location.href")
        return str(value or "")

    def goto(self, url: str) -> None:
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        self.cdp.call("Page.enable", timeout=15)
        self.cdp.call("Page.navigate", {"url": url}, timeout=45)
        self.wait_document(45)
        self.install_hooks()

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
                value = str(self.evaluate("document.readyState + '\\n' + location.href + '\\n' + document.title", timeout=10) or "")
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            state, _, rest = value.partition("\n")
            current, _, title = rest.partition("\n")
            last = current or last
            challenge = "cdn-cgi" in current or "challenges.cloudflare.com" in current or "Just a moment" in title
            if challenge and time.time() - self._last_captcha_click > 3:
                try:
                    self.click_turnstile()
                except (RuntimeError, TimeoutError, OSError):
                    pass
            if state == "complete" and current.startswith("http") and not challenge:
                return
            time.sleep(0.4)
        if last.startswith("http") and "cdn-cgi" not in last:
            return
        raise SmsError(f"Page did not finish loading ({last or BASE_URL})")

    def dismiss_overlays(self) -> list[str]:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const clickLabel = (wanted) => {"
            "   const button = [...document.querySelectorAll('button')].find((el) => visible(el) && norm(el) === wanted);"
            "   if (!button) return false;"
            "   button.scrollIntoView({block: 'center', inline: 'center'});"
            "   button.click();"
            "   return true;"
            " };"
            " const actions = [];"
            " if (clickLabel('Accept all cookies')) actions.push('cookies');"
            " if (clickLabel('Remind me later')) actions.push('later');"
            " return actions;"
            "})()"
        )
        try:
            value = self.evaluate(expression, timeout=10)
        except (RuntimeError, TimeoutError, OSError):
            return []
        if not isinstance(value, list):
            return []
        actions = [str(item) for item in value]
        if "cookies" in actions:
            log("Accepted cookies")
        if "later" in actions:
            log("Clicked Remind me later")
        return actions

    def session_state(self) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const email = [...document.querySelectorAll('input.text-field__input[type=\"email\"], input[type=\"email\"]')].find(visible);"
            " const search = [...document.querySelectorAll('input')].find((el) => visible(el) && /search by service/i.test(el.getAttribute('placeholder') || ''));"
            " const table = [...document.querySelectorAll('tr.base-table__tr, .purchases-table')].find(visible);"
            " if (email) return 'form';"
            " if (search || table) return 'ready';"
            " const login = [...document.querySelectorAll('button')].find((el) => visible(el) && norm(el) === 'Log in');"
            " if (login) return 'logged-out';"
            " return 'unknown';"
            "})()"
        )
        value = self.evaluate(expression, timeout=10)
        return str(value or "unknown")

    def click_label(self, label: str, submit: bool = False) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const wanted = {json.dumps(label)};"
            f" const submitOnly = {json.dumps(submit)};"
            " const enabled = (el) => el && !el.disabled && el.getAttribute('aria-disabled') !== 'true';"
            " let buttons = [...document.querySelectorAll('button')].filter((el) => visible(el) && norm(el) === wanted);"
            " if (submitOnly) {"
            "   const submitted = buttons.filter((el) => el.type === 'submit');"
            "   if (submitted.length) buttons = submitted;"
            " }"
            " const button = buttons.find(enabled);"
            " if (!button) return false;"
            " button.scrollIntoView({block: 'center', inline: 'center'});"
            " button.click();"
            " return true;"
            "})()"
        )
        try:
            return bool(self.evaluate(expression))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def click_buy(self, value: str) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const wanted = {json.dumps(value)};"
            " const enabled = (el) => el && !el.disabled && el.getAttribute('aria-disabled') !== 'true';"
            " const buttons = [...document.querySelectorAll('button')].filter((el) => visible(el) && norm(el).startsWith('Buy for') && norm(el).includes(wanted));"
            " const button = buttons.find(enabled);"
            " if (!button) return '';"
            " button.scrollIntoView({block: 'center', inline: 'center'});"
            " const text = norm(button);"
            " button.click();"
            " return text;"
            "})()"
        )
        try:
            return str(self.evaluate(expression) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def box(self, expression: str) -> dict | None:
        value = self.evaluate(expression, timeout=10)
        return value if isinstance(value, dict) and "x" in value else None

    def click_selector(self, selector: str) -> bool:
        expression = (
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const rect = el.getBoundingClientRect();"
            " if (rect.width < 1 || rect.height < 1) return null;"
            " return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};"
            "})()"
        )
        try:
            point = self.box(expression)
        except (RuntimeError, TimeoutError, OSError):
            return False
        if not point:
            return False
        self.mouse_click(float(point["x"]), float(point["y"]))
        return True

    def fill(self, selector: str, value: str) -> None:
        self.click_selector(selector)
        time.sleep(0.1)
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
            " el.dispatchEvent(new Event('blur', {bubbles: true}));"
            " return el.value;"
            "})()"
        )
        typed = self.evaluate(expression)
        if typed != value:
            raise SmsError(f"Could not fill {selector}")

    def placeholder_box(self, part: str) -> dict | None:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const part = {json.dumps(part.lower())};"
            " const el = [...document.querySelectorAll('input')].find((node) => {"
            "   return visible(node) && (node.getAttribute('placeholder') || '').toLowerCase().includes(part);"
            " });"
            " if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const rect = el.getBoundingClientRect();"
            " if (rect.width < 1 || rect.height < 1) return null;"
            " return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};"
            "})()"
        )
        try:
            return self.box(expression)
        except (RuntimeError, TimeoutError, OSError):
            return None

    def focus_placeholder(self, part: str, timeout: float = 15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            point = self.placeholder_box(part)
            if point:
                self.mouse_click(float(point["x"]), float(point["y"]))
                return True
            self.dismiss_overlays()
            time.sleep(0.3)
        return False

    def input_value(self, part: str) -> str:
        expression = (
            "(() => {"
            f" const part = {json.dumps(part.lower())};"
            " const el = [...document.querySelectorAll('input')].find((node) => (node.getAttribute('placeholder') || '').toLowerCase().includes(part));"
            " return el ? el.value : '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def replace_text(self, part: str, text: str) -> None:
        if not self.focus_placeholder(part):
            raise SmsError(f"Could not find the {part} search")
        time.sleep(0.15)
        self.press_key("a", "KeyA", 65, modifiers=2)
        self.press_key("Backspace", "Backspace", 8)
        if self.cdp is None:
            raise SmsError("Browser is not connected")
        self.cdp.call("Input.insertText", {"text": text})
        time.sleep(0.2)
        if self.input_value(part) == text:
            return
        expression = (
            "(() => {"
            f" const part = {json.dumps(part.lower())};"
            f" const text = {json.dumps(text)};"
            " const el = [...document.querySelectorAll('input')].find((node) => (node.getAttribute('placeholder') || '').toLowerCase().includes(part));"
            " if (!el) return '';"
            " const proto = Object.getPrototypeOf(el);"
            " const desc = Object.getOwnPropertyDescriptor(proto, 'value')"
            "   || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
            " desc.set.call(el, text);"
            " el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return el.value;"
            "})()"
        )
        typed = self.evaluate(expression)
        if typed != text:
            raise SmsError(f"Could not type {text!r} into the {part} search")

    def first_item_text(self, list_class: str) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const lists = [...document.querySelectorAll({json.dumps('ul.' + list_class)})];"
            " const list = lists.find((ul) => visible(ul) && ul.querySelector('li')) || lists.find((ul) => ul.querySelector('li'));"
            " if (!list) return '';"
            " const item = [...list.querySelectorAll('li')].find(visible) || list.querySelector('li');"
            " return item ? norm(item) : '';"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def click_first_item(self, list_class: str) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const lists = [...document.querySelectorAll({json.dumps('ul.' + list_class)})];"
            " const list = lists.find((ul) => visible(ul) && ul.querySelector('li'));"
            " if (!list) return '';"
            " const item = [...list.querySelectorAll('li')].find(visible);"
            " if (!item) return '';"
            " item.scrollIntoView({block: 'center', inline: 'center'});"
            " const target = item.querySelector('button, a, label') || item;"
            " const text = norm(item);"
            " target.click();"
            " return text;"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def search_pick(self, part: str, query: str, list_class: str) -> str:
        self.replace_text(part, query)
        wanted = re.sub(r"[^a-z0-9]", "", query.lower())
        seen = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            text = self.first_item_text(list_class)
            folded = re.sub(r"[^a-z0-9]", "", text.lower())
            if text and wanted and wanted in folded:
                seen = text
                break
            time.sleep(0.3)
        if not seen:
            raise SmsError(f"No {part} matched {query!r}")
        self.focus_placeholder(part, timeout=3)
        self.press_enter()
        time.sleep(0.4)
        picked = self.click_first_item(list_class) or seen
        log(f"Selected {picked}")
        time.sleep(0.6)
        return picked

    def offers_visible(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " return [...document.querySelectorAll('ul.free-price-offers__list')].some((ul) => visible(ul) && ul.querySelector('li'));"
            "})()"
        )
        try:
            return bool(self.evaluate(expression, timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def wait_offers(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.offers_visible():
                return True
            self.dismiss_overlays()
            time.sleep(0.4)
        return False

    def list_offers(self) -> list[dict]:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const lists = [...document.querySelectorAll('ul.free-price-offers__list')].filter(visible);"
            " const list = lists[0];"
            " if (!list) return [];"
            " return [...list.querySelectorAll('li')].filter(visible).map((li) => {"
            "   const input = li.querySelector('input[type=radio]');"
            "   const price = li.querySelector('.heading-small');"
            "   const stock = li.querySelector('.free-price-offers__info .body-medium');"
            "   return {"
            "     value: input ? (input.value || '') : '',"
            "     priceText: norm(price),"
            "     stockText: norm(stock)"
            "   };"
            " });"
            "})()"
        )
        try:
            value = self.evaluate(expression)
        except (RuntimeError, TimeoutError, OSError):
            return []
        if not isinstance(value, list):
            return []
        offers = []
        for item in value:
            if not isinstance(item, dict):
                continue
            price_text = str(item.get("priceText") or "")
            stock_text = str(item.get("stockText") or "")
            radio = str(item.get("value") or "")
            price = parse_money(radio, price_text)
            if price is None:
                continue
            offers.append(
                {
                    "value": radio or price_text,
                    "price": price,
                    "price_text": price_text or radio,
                    "stock": parse_pcs(stock_text),
                    "stock_text": stock_text,
                }
            )
        offers.sort(key=lambda offer: offer["price"])
        return offers

    def select_offer(self, value: str) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const wanted = {json.dumps(value)};"
            " const items = [...document.querySelectorAll('ul.free-price-offers__list li')].filter(visible);"
            " const li = items.find((node) => {"
            "   const input = node.querySelector('input[type=radio]');"
            "   return input && input.value === wanted;"
            " });"
            " if (!li) return false;"
            " li.scrollIntoView({block: 'center', inline: 'center'});"
            " const input = li.querySelector('input[type=radio]');"
            " const label = li.querySelector('label') || li;"
            " if (input) input.click();"
            " label.click();"
            " return true;"
            "})()"
        )
        try:
            return bool(self.evaluate(expression))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def list_rows(self) -> list[dict]:
        expression = (
            "(() => {"
            " const norm = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();"
            " return [...document.querySelectorAll('tr.base-table__tr')].map((row) => {"
            "   const title = row.querySelector('.services-el__title');"
            "   const img = row.querySelector('.services-el__logo-service img');"
            "   const status = row.querySelector('.status-card .text');"
            "   const chip = row.querySelector('.sms-chip.first-chip');"
            "   const when = row.querySelector('.purchases-table__time');"
            "   const price = row.querySelector('.purchases-table__price');"
            "   return {"
            "     title: norm(title),"
            "     alt: img ? (img.getAttribute('alt') || '') : '',"
            "     status: norm(status),"
            "     otp: norm(chip),"
            "     time: norm(when),"
            "     price: norm(price)"
            "   };"
            " });"
            "})()"
        )
        value = self.evaluate(expression)
        if not isinstance(value, list):
            return []
        rows = []
        for item in value:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "title": str(item.get("title") or ""),
                    "alt": str(item.get("alt") or ""),
                    "status": str(item.get("status") or ""),
                    "otp": str(item.get("otp") or ""),
                    "time": str(item.get("time") or ""),
                    "price": str(item.get("price") or ""),
                }
            )
        return rows

    def row_key(self, row: dict) -> str:
        return digits(row.get("alt") or "") or digits(row.get("title") or "")

    def click_turnstile(self) -> bool:
        expression = (
            "(() => {"
            " const interesting = (frame) => {"
            "   const blob = ((frame.getAttribute('src') || '') + ' ' + (frame.getAttribute('title') || '')).toLowerCase();"
            "   return blob.includes('cloudflare') || blob.includes('turnstile') || blob.includes('challenge');"
            " };"
            " const frames = [...document.querySelectorAll('iframe')].filter(interesting);"
            " const el = frames[0] || document.querySelector('.cf-turnstile, #turnstile');"
            " if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const rect = el.getBoundingClientRect();"
            " if (rect.width < 2 || rect.height < 2) return null;"
            " return {left: rect.left, top: rect.top, width: rect.width, height: rect.height,"
            "   x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};"
            "})()"
        )
        try:
            point = self.box(expression)
        except (RuntimeError, TimeoutError, OSError):
            return False
        if not point:
            return False
        width = float(point.get("width") or 0)
        height = float(point.get("height") or 0)
        if width > 400 and height > 250:
            x, y = float(point["x"]), float(point["y"])
        else:
            x = float(point["left"]) + min(28.0, width / 2)
            y = float(point["top"]) + height / 2
        self._last_captcha_click = time.time()
        self.mouse_click(x, y)
        return True

    def login_enabled(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const buttons = [...document.querySelectorAll('button')].filter((el) => visible(el) && norm(el) === 'Login');"
            " const button = buttons.find((el) => el.type === 'submit') || buttons[0];"
            " if (!button) return false;"
            " return !button.disabled && button.getAttribute('aria-disabled') !== 'true';"
            "})()"
        )
        try:
            return bool(self.evaluate(expression, timeout=10))
        except (RuntimeError, TimeoutError, OSError):
            return False

    def captcha_token(self) -> int:
        expression = (
            "(() => {"
            " const token = document.querySelector('[name=\"cf-turnstile-response\"]');"
            " return token && token.value ? token.value.length : 0;"
            "})()"
        )
        try:
            return int(self.evaluate(expression, timeout=10) or 0)
        except (RuntimeError, TimeoutError, OSError, ValueError):
            return 0

    def pass_captcha(self) -> bool:
        log("Waiting for the captcha")
        deadline = time.time() + CAPTCHA_WAIT_SECONDS
        clicks = 0
        while time.time() < deadline:
            self.dismiss_overlays()
            if self.login_enabled():
                log("Captcha passed")
                return True
            if clicks < 2 and self.captcha_token() == 0 and time.time() - self._last_captcha_click > 4:
                if self.click_turnstile():
                    clicks += 1
                    log("Clicked the captcha")
            time.sleep(1)
        return False

    def page_hint(self) -> str:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const alerts = [...document.querySelectorAll('[role=alert], .toast, .notification, .alert')]"
            "   .filter(visible).map(norm).filter(Boolean).slice(0, 3).join(' | ');"
            " return [location.href, document.title, alerts].filter(Boolean).join(' — ');"
            "})()"
        )
        try:
            return str(self.evaluate(expression, timeout=10) or "")[:300]
        except (RuntimeError, TimeoutError, OSError):
            return ""

    def wait_state(self, expected: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.dismiss_overlays()
                if self.session_state() == expected:
                    return True
            except (RuntimeError, TimeoutError, OSError):
                pass
            time.sleep(0.4)
        return False

    def ensure_session(self) -> None:
        self.ensure_browser()
        self.dismiss_overlays()
        try:
            state = self.session_state()
        except (RuntimeError, TimeoutError, OSError):
            state = "unknown"
        if state == "ready":
            self.install_hooks()
            return
        self.login()

    def login(self) -> None:
        log(f"Signing in as {HERO_SMS_EMAIL}")
        self.goto(BASE_URL)
        self.dismiss_overlays()
        try:
            state = self.session_state()
        except (RuntimeError, TimeoutError, OSError):
            state = "unknown"
        if state == "ready":
            log("Already signed in")
            return
        if state != "form":
            if not self.click_label("Log in"):
                raise SmsError("Could not find the Log in button")
            if not self.wait_state("form", 20):
                if self.session_state() == "ready":
                    log("Already signed in")
                    return
                raise SmsError("Could not find the email and password fields")
        self.fill('input.text-field__input[type="email"]', HERO_SMS_EMAIL)
        self.fill('input.text-field__input[type="password"]', HERO_SMS_PASSWORD)
        if not self.pass_captcha():
            raise SmsError("Captcha did not finish, so Login stayed disabled. Complete it in the browser window and start the service again.")
        time.sleep(0.5)
        self.dismiss_overlays()
        clicked = False
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.click_label("Login", submit=True):
                clicked = True
                break
            time.sleep(0.3)
        if not clicked:
            raise SmsError("Could not click Login")
        if self.wait_state("ready", 40):
            self.dismiss_overlays()
            log("Signed in")
            return
        hint = self.page_hint()
        raise SmsError(hint or "Sign in did not finish")

    def prepare_catalog(self, kind: str) -> None:
        self.ensure_session()
        self.dismiss_overlays()
        if self.placeholder_box("service") is None:
            self.goto(BASE_URL)
            self.ensure_session()
            self.dismiss_overlays()
        queries = [SERVICE_QUERY[kind]]
        if kind == "webde" and "web.de" not in queries:
            queries.append("web.de")
        picked = False
        last_error = "Could not choose a service"
        for index, query in enumerate(queries):
            try:
                self.search_pick("service", query, "service-list")
                picked = True
                break
            except SmsError as exc:
                last_error = str(exc)
                if index == len(queries) - 1:
                    raise SmsError(last_error) from exc
                log(f"{last_error}. Trying another service name.")
        if not picked:
            raise SmsError(last_error)
        self.search_pick("country", COUNTRY, "country-list")
        if self.offers_visible():
            return
        if not self.click_label("Buy with Settings"):
            time.sleep(0.8)
            self.dismiss_overlays()
            if not self.click_label("Buy with Settings"):
                raise SmsError("Could not find Buy with Settings")
        if not self.wait_offers(15):
            raise SmsError("Could not find the price list")

    def purchase(self, value: str) -> dict:
        self.dismiss_overlays()
        try:
            before = {self.row_key(row) for row in self.list_rows()}
        except (RuntimeError, TimeoutError, OSError):
            before = set()
        if not self.select_offer(value):
            return {"ok": False, "error": "Price option disappeared"}
        enabled = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            enabled = self.click_buy(value)
            if enabled:
                break
            time.sleep(0.3)
        if not enabled:
            return {"ok": False, "error": "Buy button stayed disabled"}
        log(f"Clicked {enabled}")
        deadline = time.time() + ORDER_WAIT_SECONDS
        while time.time() < deadline:
            self.dismiss_overlays()
            try:
                rows = self.list_rows()
            except (RuntimeError, TimeoutError, OSError):
                time.sleep(0.4)
                continue
            fresh = [row for row in rows if self.row_key(row) and self.row_key(row) not in before]
            fresh = [row for row in fresh if len(self.row_key(row)) >= 10]
            near = [row for row in fresh if clock_distance(row.get("time") or "") <= 5]
            pool = near or fresh
            if pool:
                pool.sort(key=lambda row: clock_distance(row.get("time") or ""))
                row = pool[0]
                phone = plus_phone(self.row_key(row))
                if phone:
                    return {
                        "ok": True,
                        "phone": phone,
                        "time": row.get("time") or "",
                        "price": row.get("price") or "",
                    }
            time.sleep(0.5)
        return {"ok": False, "error": "Buy did not create a new number"}

    def read_otp(self, phone: str) -> str:
        for row in self.list_rows():
            if not (same_phone(row["alt"], phone) or same_phone(row["title"], phone)):
                continue
            if "waiting" in row["status"].lower():
                return ""
            return parse_otp(row["otp"])
        return ""

    def confirm_dialog(self) -> bool:
        expression = (
            "(() => {"
            + VISIBLE_FN
            + " const labels = ['Confirm', 'Yes', 'Cancel number', 'Yes, cancel'];"
            " const dialogs = [...document.querySelectorAll('[role=dialog], .modal, .info-modal')].filter(visible);"
            " for (const dialog of dialogs) {"
            "   if (norm(dialog).includes('Protect your account')) continue;"
            "   const button = [...dialog.querySelectorAll('button')].find((el) => visible(el) && labels.includes(norm(el)) && !el.disabled);"
            "   if (!button) continue;"
            "   button.click();"
            "   return norm(button);"
            " }"
            " return '';"
            "})()"
        )
        try:
            value = str(self.evaluate(expression, timeout=10) or "")
        except (RuntimeError, TimeoutError, OSError):
            return False
        if value:
            log(f"Confirmed {value}")
        return bool(value)

    def press_row(self, phone: str, icon: str) -> str:
        try:
            rows = self.list_rows()
        except (RuntimeError, TimeoutError, OSError):
            return "missing"
        match = next((row for row in rows if same_phone(row["alt"], phone) or same_phone(row["title"], phone)), None)
        if match is None:
            return "missing"
        key = self.row_key(match)
        expression = (
            "(() => {"
            + VISIBLE_FN
            + f" const want = {json.dumps(key)};"
            f" const icon = {json.dumps(icon)};"
            " const only = (value) => (value || '').replace(/\\D/g, '');"
            " const row = [...document.querySelectorAll('tr.base-table__tr')].find((tr) => {"
            "   const title = only((tr.querySelector('.services-el__title') || {}).textContent || '');"
            "   const alt = only((tr.querySelector('.services-el__logo-service img') || {}).getAttribute('alt') || '');"
            "   return title === want || alt === want;"
            " });"
            " if (!row) return 'missing';"
            " const button = [...row.querySelectorAll('button')].find((el) => (el.innerHTML || '').includes('i-hero:' + icon));"
            " if (!button) return 'waiting';"
            " if (button.disabled || button.getAttribute('aria-disabled') === 'true') return 'waiting';"
            " button.scrollIntoView({block: 'center', inline: 'center'});"
            " button.click();"
            " return 'clicked';"
            "})()"
        )
        try:
            result = str(self.evaluate(expression) or "waiting")
        except (RuntimeError, TimeoutError, OSError):
            return "missing"
        if result != "clicked":
            return result
        time.sleep(0.4)
        self.confirm_dialog()
        return "done" if icon == "check" else "cancel"


class NumberService:
    def __init__(self):
        self.browser = Dashboard()
        self.store = OrderStore(STORE_FILE)
        self.jobs: queue.Queue = queue.Queue()
        self._state = threading.Lock()
        self._active = False
        self._waiting = 0
        self._worker = threading.Thread(target=self._run_queue, name="kka-queue", daemon=True)
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
        phones: list[str] = []
        orders: list[dict] = []
        exhausted: set[str] = set()
        attempts = 0
        limit = max(count * 6, 6)
        failure = "failed get number"
        try:
            while len(phones) < count and attempts < limit:
                attempts += 1
                self.browser.dismiss_overlays()
                if not self.browser.offers_visible():
                    self.browser.prepare_catalog(kind)
                offers = [
                    offer
                    for offer in self.browser.list_offers()
                    if offer["stock"] is not None and offer["stock"] > 0 and offer["value"] not in exhausted
                ]
                if not offers:
                    seen = self.browser.list_offers()
                    if seen:
                        summary = ", ".join(
                            f"{offer['price_text']} ({offer['stock_text'] or 'no stock'})" for offer in seen[:6]
                        )
                        log(f"No price with stock above zero. Saw {summary}")
                    else:
                        log("No price list to buy from")
                    failure = "failed get number"
                    break
                offer = offers[0]
                stock_label = offer["stock_text"] or f"{offer['stock']} pcs"
                log(f"Buying cheapest at {offer['price_text']} ({stock_label})")
                try:
                    result = self.browser.purchase(offer["value"])
                except (RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Buy failed: {exc}")
                    exhausted.add(offer["value"])
                    continue
                if not result.get("ok"):
                    log(str(result.get("error") or "Buy was rejected"))
                    exhausted.add(offer["value"])
                    continue
                phone = str(result.get("phone") or "")
                if any(same_phone(phone, existing) for existing in phones):
                    log("Buy did not create a new number")
                    exhausted.add(offer["value"])
                    continue
                price = str(result.get("price") or offer["price_text"])
                bought_at = str(result.get("time") or "")
                self.store.add(email, kind, phone, price, bought_at)
                phones.append(phone)
                orders.append({"phone": phone, "price": price, "time": bought_at})
                log(f"Saved {phone} for {email}")
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
        return self._submit(f"get_code {number}", lambda number=number: self._get_code(number))

    def _get_code(self, number: str) -> dict:
        self.browser.ensure_session()
        deadline = time.monotonic() + OTP_WAIT_SECONDS
        next_reload = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.browser.dismiss_overlays()
            try:
                code = self.browser.read_otp(number)
            except (RuntimeError, TimeoutError, OSError):
                code = ""
            if code:
                self.store.mark(number, "coded")
                log(f"OTP for {number} is {code}")
                return {"ok": True, "phone": plus_phone(number), "code": code}
            if time.monotonic() >= next_reload:
                try:
                    self.browser.reload()
                    self.browser.ensure_session()
                except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not refresh the page: {exc}")
                next_reload = time.monotonic() + 15
            time.sleep(2)
        log(f"No OTP for {number} within 5 minutes")
        return {"ok": False, "error": "failed", "phone": plus_phone(number)}

    def code_passed(self, email: str) -> dict:
        email = email.strip()
        if "@" not in email:
            raise ValueError("email is invalid")
        return self._submit(f"code_passed {email}", lambda email=email: self._code_passed(email))

    def _code_passed(self, email: str) -> dict:
        records = [
            item
            for item in self.store.phones_for(email)
            if item.get("status") in {"active", "coded"} and item.get("phone")
        ]
        if not records:
            return {"ok": False, "error": "unknown email", "email": email, "results": []}
        self.browser.ensure_session()
        if not any(item.get("status") == "coded" for item in records):
            try:
                rows = self.browser.list_rows()
            except (RuntimeError, TimeoutError, OSError):
                rows = []
            for record in records:
                phone = str(record.get("phone") or "")
                row = next((item for item in rows if same_phone(item["alt"], phone) or same_phone(item["title"], phone)), None)
                if row and parse_otp(row.get("otp") or ""):
                    record["status"] = "coded"
            if any(item.get("status") == "coded" for item in records):
                log("Closing numbers that already show an OTP as done")
        pending = list(records)
        results = []
        deadline = time.time() + CLOSE_WAIT_SECONDS
        next_reload = time.time() + 15
        while pending and time.time() < deadline:
            self.browser.dismiss_overlays()
            still = []
            missing = False
            for record in pending:
                phone = str(record.get("phone") or "")
                icon = "check" if record.get("status") == "coded" else "close"
                try:
                    action = self.browser.press_row(phone, icon)
                except (RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not close {phone}: {exc}")
                    still.append(record)
                    continue
                if action in {"done", "cancel"}:
                    self.store.mark(phone, action)
                    results.append({"phone": plus_phone(phone), "action": action})
                    log(f"{plus_phone(phone)} {action}")
                else:
                    if action == "missing":
                        missing = True
                    still.append(record)
            pending = still
            if missing and time.time() >= next_reload:
                try:
                    self.browser.reload()
                    self.browser.ensure_session()
                except (SmsError, RuntimeError, TimeoutError, OSError) as exc:
                    log(f"Could not refresh the page: {exc}")
                next_reload = time.time() + 15
            if pending:
                time.sleep(2)
        for record in pending:
            results.append({"phone": plus_phone(str(record.get("phone") or "")), "action": "waiting"})
        closed = [item for item in results if item["action"] in {"done", "cancel"}]
        removed = self.store.remove_email(email)
        log(f"Removed {len(removed)} number(s) for {email}")
        payload = {"email": email, "results": results, "removed": removed}
        if pending or len(closed) != len(records):
            payload["ok"] = False
            payload["error"] = "waiting"
            return payload
        payload["ok"] = True
        return payload


class Handler(BaseHTTPRequestHandler):
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
    allow_reuse_address = False
    daemon_threads = True


def listen() -> ServiceServer:
    try:
        return ServiceServer((HERO_SMS_HOST, HERO_SMS_PORT), Handler)
    except OSError as exc:
        busy = getattr(exc, "winerror", None) in {10013, 10048} or exc.errno in {13, 48, 98}
        if busy:
            raise SystemExit(f"Port {HERO_SMS_PORT} is already in use. Set HERO_SMS_PORT in {ENV_FILE} to a free port.") from exc
        raise


def main() -> None:
    if not BASE_URL.startswith("http"):
        raise SystemExit(f"Set TARGET_URL3 in {ENV_FILE}")
    if not HERO_SMS_EMAIL or not HERO_SMS_PASSWORD:
        raise SystemExit(f"Set HERO_SMS_EMAIL and HERO_SMS_PASSWORD in {ENV_FILE}")
    if not 1 <= HERO_SMS_PORT <= 65535 or not 1 <= DEBUG_PORT <= 65535:
        raise SystemExit("HERO_SMS_PORT and HERO_SMS_DEBUG_PORT must be valid ports")
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
    log(f"Listening on http://{HERO_SMS_HOST}:{HERO_SMS_PORT}")
    log(f"Ordering {wanted} {COUNTRY} numbers per get_started call")
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

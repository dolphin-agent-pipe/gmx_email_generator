"""Create a Dolphin Anty profile with the name you provide, open TARGET_URL from .env, click through to registration, and fill the signup form.

A random proxy from proxies.txt is attached to that profile.
Dolphin Anty must already be running and logged in on this machine.
The browser window stays open after this script exits.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import random
import re
import socket
import string
import sys
import time
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dolphin.api import STABLE_CHROME_VERSION, DolphinAPI
from dolphin.apikey import api_key

ENV_FILE = ROOT / ".env"
USAGE_FILE = ROOT / "freenumber_usage.json"
ACCOUNTS_FILE = ROOT / "accounts.json"
MAX_FREE_NUMBER_USES = 20
ANOSIM_LOGIN = "https://anosim.net/login"
ANOSIM_NUMBERS = "https://anosim.net/freenumbers/germany"


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
TARGET_URL = os.environ.get("TARGET_URL", "").strip()
LOCAL_API = os.environ.get("DOLPHIN_LOCAL_API", "http://127.0.0.1:3001").rstrip("/")


def parse_proxy(line: str) -> dict | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if "@" in raw:
        creds, hostport = raw.rsplit("@", 1)
        if ":" not in creds or ":" not in hostport:
            return None
        login, password = creds.split(":", 1)
        host, port = hostport.rsplit(":", 1)
    else:
        parts = raw.split(":")
        if len(parts) < 4:
            return None
        if parts[1].isdigit():
            host, port = parts[0], parts[1]
            login, password = parts[2], ":".join(parts[3:])
        else:
            login, password = parts[0], ":".join(parts[1:-2])
            host, port = parts[-2], parts[-1]
    if not port.isdigit() or not host or not login:
        return None
    return {
        "host": host,
        "port": int(port),
        "login": login,
        "password": password,
    }


def load_random_proxy(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Proxy file not found: {path}")
    proxies = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_proxy(line)
        if parsed:
            proxies.append(parsed)
    if not proxies:
        raise SystemExit(f"No proxies in {path}")
    return random.choice(proxies)


def proxy_label(proxy: dict, proxy_type: str) -> str:
    match = re.search(r"session-([A-Za-z0-9]+)", proxy["password"])
    session = match.group(1) if match else "custom"
    return f"{proxy_type}://{proxy['host']}:{proxy['port']} session={session}"


def proxy_body(proxy: dict, proxy_type: str) -> dict:
    return {
        "type": proxy_type,
        "host": proxy["host"],
        "port": proxy["port"],
        "login": proxy["login"],
        "password": proxy["password"],
        "name": f"{proxy['host']}:{proxy['port']}",
    }


def recover_local_session_token() -> str:
    """Protected local routes such as proxy check need Dolphin's own session token."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return ""
    session_dir = Path(appdata) / "dolphin_anty" / "Session Storage"
    if not session_dir.is_dir():
        return ""
    pattern = re.compile(r"map-(\d+)-sessionToken([\s\S]*?)map-\1-intercom-test")
    found: list[tuple[int, str]] = []
    for path in session_dir.iterdir():
        if path.suffix.lower() not in {".log", ".ldb"}:
            continue
        try:
            raw = path.read_bytes().decode("latin1", errors="ignore")
        except OSError:
            continue
        for match in pattern.finditer(raw):
            token = re.sub(r"[^A-Za-z0-9\-_]", "", match.group(2).replace("\x00", ""))
            if len(token) >= 100:
                found.append((int(match.group(1)), token))
    if not found:
        return ""
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0][1]


def local_login(session: requests.Session) -> None:
    session_token = recover_local_session_token()
    if session_token:
        session.headers["X-Anty-Session-Token"] = session_token
    else:
        print("Dolphin local session token was not found. Proxy check may fail.")
    try:
        response = session.post(
            f"{LOCAL_API}/v1.0/auth/login-with-token",
            json={"token": api_key},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise SystemExit(
            "Dolphin Anty local API is not reachable. Open Dolphin Anty and log in, "
            f"then run this again. ({exc})"
        ) from exc
    if response.status_code != 200:
        body = (response.text or "")[:300]
        raise SystemExit(f"Dolphin local API rejected the token: HTTP {response.status_code} {body}")


def check_proxy(session: requests.Session, proxy: dict, proxy_type: str) -> bool:
    body = proxy_body(proxy, proxy_type)
    try:
        response = session.post(f"{LOCAL_API}/v1.0/check/proxy", json=body, timeout=45)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Proxy check skipped: {exc}")
        return True
    if isinstance(payload, dict) and payload.get("success") is False:
        message = payload.get("error") or payload.get("message") or payload
        if "invalid session token" in str(message).lower():
            print("Proxy check skipped: Dolphin rejected the local session token")
            return True
        print(f"Proxy check failed for {proxy_type}: {message}")
        return False
    ip = ""
    if isinstance(payload, dict):
        ip = str(payload.get("ip") or payload.get("origin") or "")
    print(f"Proxy check ok{f' ip={ip}' if ip else ''}")
    return True


def unwrap_fingerprint(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Bad fingerprint response: {payload!r}")
    for key in ("fingerprint", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict) and ("userAgent" in inner or "os" in inner):
            return inner
    if "userAgent" in payload or "os" in payload:
        return payload
    raise RuntimeError(f"Unrecognized fingerprint response keys: {list(payload)[:12]}")


def align_platform(data: dict, fingerprint: dict) -> None:
    os_name = str((fingerprint.get("os") or {}).get("name", "windows")).lower()
    os_version = (fingerprint.get("os") or {}).get("version")
    if "windows" in os_name:
        data["platformName"] = "Win32"
        data["osVersion"] = os_version or "10.0.0"
    elif "mac" in os_name:
        data["platformName"] = "MacIntel"
    elif "linux" in os_name:
        data["platformName"] = "Linux x86_64"


def normalize_profile_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > 80:
        raise SystemExit("Profile name must be 1 to 80 characters")
    return name


def apply_hardware(data: dict, cores: int, memory_gb: int) -> None:
    cpu = data.setdefault("cpu", {})
    cpu["mode"] = "manual"
    cpu["value"] = cores
    memory = data.setdefault("memory", {})
    memory["mode"] = "manual"
    memory["value"] = memory_gb


def build_profile(api: DolphinAPI, name: str, proxy: dict, proxy_type: str) -> dict:
    errors = []
    versions = []
    for version in (STABLE_CHROME_VERSION - 1, STABLE_CHROME_VERSION - 2, 136, 131):
        if version and version not in versions:
            versions.append(version)
    for version in versions:
        try:
            raw = api.generate_fingerprint(platform="windows", browser_version=version)
            fingerprint = unwrap_fingerprint(raw)
            data = api.fingerprint_to_profile(name, ["xxx"], fingerprint)
            align_platform(data, fingerprint)
            apply_hardware(data, 8, 8)
            data["proxy"] = proxy_body(proxy, proxy_type)
            launch_args = data.setdefault("args", [])
            if "--start-maximized" not in launch_args:
                launch_args.append("--start-maximized")
            return data
        except Exception as exc:
            errors.append(f"chrome {version}: {exc}")
    raise SystemExit("Could not build a Dolphin profile:\n" + "\n".join(errors))


def profile_id_from(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected profile response: {payload!r}")
    for key in ("browserProfileId", "id"):
        if payload.get(key):
            return str(payload[key])
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("browserProfileId", "id"):
            if data.get(key):
                return str(data[key])
    message = payload.get("error") or payload.get("message") or payload
    raise RuntimeError(f"Dolphin did not return a profile id: {message}")


def create_profile(api: DolphinAPI, email: str, proxy: dict, proxy_type: str) -> str:
    data = build_profile(api, email, proxy, proxy_type)
    last_error: Exception | None = None
    for cores, memory_gb in ((8, 8), (4, 8), (16, 8), (8, 4)):
        apply_hardware(data, cores, memory_gb)
        try:
            profile_id = profile_id_from(api.create_profile(data))
            break
        except RuntimeError as exc:
            if "E_HARDWARE" not in str(exc):
                raise
            last_error = exc
    else:
        raise last_error or RuntimeError("Dolphin rejected the profile hardware")
    print(f"Created Dolphin profile {profile_id} named {email}")
    return profile_id


def maximize_window(cdp: Cdp, target_id: str = "") -> None:
    params = {"targetId": target_id} if target_id else {}
    try:
        info = cdp.call("Browser.getWindowForTarget", params)
        window_id = info.get("windowId")
        if window_id is None:
            return
        cdp.call(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "maximized"}},
        )
        print("Opened the browser at full width")
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"Could not maximize the browser: {exc}")


def start_profile(session: requests.Session, profile_id: str, headless: bool) -> dict:
    query = "automation=1"
    if headless:
        query += "&headless=1"
    url = f"{LOCAL_API}/v1.0/browser_profiles/{profile_id}/start?{query}"
    last_error = "start failed"
    for attempt in range(1, 4):
        try:
            response = session.get(url, timeout=120)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            time.sleep(2 + attempt)
            continue
        automation = payload.get("automation") if isinstance(payload, dict) else None
        if not isinstance(automation, dict):
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                automation = data.get("automation") or data
        if isinstance(automation, dict) and automation.get("port"):
            return automation
        text = json.dumps(payload)[:400] if isinstance(payload, dict) else str(payload)
        last_error = text
        if "already" in text.lower() and "start" in text.lower():
            session.get(f"{LOCAL_API}/v1.0/browser_profiles/{profile_id}/stop", timeout=60)
            time.sleep(2)
            continue
        time.sleep(2 + attempt)
    raise SystemExit(f"Could not start Dolphin profile {profile_id}: {last_error}")


def stop_profile(session: requests.Session, profile_id: str) -> None:
    try:
        response = session.get(f"{LOCAL_API}/v1.0/browser_profiles/{profile_id}/stop", timeout=60)
    except requests.RequestException as exc:
        print(f"Could not close Dolphin profile {profile_id}: {exc}")
        return
    if response.ok:
        print(f"Closed Dolphin profile {profile_id}")
        return
    print(f"Could not close Dolphin profile {profile_id}: HTTP {response.status_code}")


def remove_profile(api: DolphinAPI, profile_id: str) -> None:
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            api.delete_profile(profile_id)
            print(f"Removed Dolphin profile {profile_id}")
            return
        except RuntimeError as exc:
            last_error = exc
            time.sleep(2)
    print(f"Could not remove Dolphin profile {profile_id}: {last_error}")


def finish_profile(session: requests.Session, api: DolphinAPI, profile_id: str, cdp: Cdp | None, ws: CdpSocket | None) -> None:
    if cdp is not None:
        try:
            cdp.call("Browser.close", timeout=10)
        except (RuntimeError, TimeoutError, OSError):
            pass
    if ws is not None:
        try:
            ws.close()
        except OSError:
            pass
    stop_profile(session, profile_id)
    time.sleep(1)
    remove_profile(api, profile_id)


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


def connect_page(port: int) -> tuple[CdpSocket, str]:
    deadline = time.time() + 30
    last_error = "no page"
    while time.time() < deadline:
        try:
            targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            time.sleep(1)
            continue
        pages = [item for item in targets if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
        if not pages:
            last_error = "browser has no page target yet"
            time.sleep(1)
            continue
        page = pages[0]
        try:
            return CdpSocket(page["webSocketDebuggerUrl"]), str(page.get("id") or "")
        except (OSError, RuntimeError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise SystemExit(f"Could not attach to Dolphin on port {port}: {last_error}")


class Cdp:
    def __init__(self, ws: CdpSocket):
        self.ws = ws
        self._next = 0

    def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30,
        session_id: str | None = None,
    ) -> dict:
        self._next += 1
        message_id = self._next
        self.ws.sock.settimeout(timeout)
        payload = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send_json(payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.ws.recv_json()
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message.get("result") or {}
        raise TimeoutError(method)


def evaluate(cdp: Cdp, expression: str, session_id: str | None = None, timeout: float = 30, await_promise: bool = False):
    result = cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        timeout=timeout,
        session_id=session_id,
    )
    details = result.get("exceptionDetails")
    if details:
        text = (details.get("exception") or {}).get("description") or details.get("text") or details
        raise RuntimeError(str(text))
    return (result.get("result") or {}).get("value")


def navigate(cdp: Cdp, url: str) -> str:
    cdp.call("Page.enable")
    cdp.call("Page.navigate", {"url": url}, timeout=45)
    deadline = time.time() + 45
    last = ""
    while time.time() < deadline:
        value = str(evaluate(cdp, "document.readyState + '\\n' + location.href + '\\n' + document.title") or "")
        state, _, rest = value.partition("\n")
        href, _, title = rest.partition("\n")
        last = href or last
        if state == "complete" and href and not href.startswith("about:") and not href.startswith("chrome"):
            print(f"Opened {href}")
            if title:
                print(f"Title: {title}")
            return href
        time.sleep(1)
    if last:
        print(f"Opened {last}")
        return last
    raise SystemExit(f"Browser started, but {url} did not finish loading")


def element_box(cdp: Cdp, selector: str, session_id: str | None = None) -> dict | None:
    expression = (
        "(() => {"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return null;"
        " const visible = (rect) => { const x = rect.left + rect.width / 2; const y = rect.top + rect.height / 2; return rect.width > 1 && rect.height > 1 && x > 0 && y > 0 && x < window.innerWidth && y < window.innerHeight; };"
        " let r = el.getBoundingClientRect();"
        " if (!visible(r)) { el.scrollIntoView({block: 'center', inline: 'center'}); r = el.getBoundingClientRect(); }"
        " if (r.width < 1 || r.height < 1) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2, left: r.left, top: r.top, width: r.width, height: r.height};"
        "})()"
    )
    box = evaluate(cdp, expression, session_id=session_id)
    return box if isinstance(box, dict) else None


def mouse_click(cdp: Cdp, x: float, y: float) -> None:
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
    )
    time.sleep(0.05)
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )


def click_selector(cdp: Cdp, selector: str) -> bool:
    box = element_box(cdp, selector)
    if not box:
        return False
    mouse_click(cdp, box["x"], box["y"])
    return True


def field_value(cdp: Cdp, selector: str) -> str:
    value = evaluate(cdp, f"(document.querySelector({json.dumps(selector)}) || {{}}).value || ''")
    return str(value or "")


def type_into(cdp: Cdp, selector: str, text: str) -> None:
    deadline = time.time() + 30
    while time.time() < deadline and not element_box(cdp, selector):
        time.sleep(0.4)
    if not click_selector(cdp, selector):
        raise SystemExit(f"Could not find {selector}")
    time.sleep(0.1)
    cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
    )
    cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
    )
    cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
    cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
    cdp.call("Input.insertText", {"text": text})
    time.sleep(0.15)
    if field_value(cdp, selector) == text:
        return
    typed = evaluate(
        cdp,
        "(() => {"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return '';"
        " const proto = Object.getPrototypeOf(el);"
        " const desc = Object.getOwnPropertyDescriptor(proto, 'value');"
        f" desc.set.call(el, {json.dumps(text)});"
        " el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: el.value}));"
        " el.dispatchEvent(new Event('change', {bubbles: true}));"
        " return el.value;"
        "})()",
    )
    if typed != text:
        raise SystemExit(f"{selector} stayed {typed!r} after typing {text!r}")


def captcha_frame_session(cdp: Cdp) -> str | None:
    payload = cdp.call("Target.getTargets")
    for info in payload.get("targetInfos") or []:
        url = str(info.get("url") or "").lower()
        if "captchafox" not in url or not info.get("targetId"):
            continue
        attached = cdp.call("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
        session_id = attached.get("sessionId")
        if session_id:
            return str(session_id)
    return None


def click_human_check(cdp: Cdp) -> None:
    deadline = time.time() + 25
    frame = None
    while time.time() < deadline:
        direct = element_box(cdp, "#captchafox .cf-checkbox, #captchafox [role='checkbox']")
        if direct:
            mouse_click(cdp, direct["x"], direct["y"])
            print("Clicked I am human")
            return
        frame = element_box(cdp, "#captchafox iframe, iframe[src*='captchafox']")
        session_id = captcha_frame_session(cdp) if frame else None
        if frame and session_id:
            local = element_box(cdp, ".cf-checkbox, [role='checkbox']", session_id=session_id)
            if local:
                mouse_click(cdp, frame["left"] + local["x"], frame["top"] + local["y"])
                print("Clicked I am human")
                return
        time.sleep(0.5)
    if frame:
        mouse_click(cdp, frame["left"] + min(28, frame["width"] / 2), frame["y"])
        print("Clicked I am human")
        return
    raise SystemExit("Could not find the I am human checkbox")


def captcha_status(cdp: Cdp) -> dict:
    raw = evaluate(
        cdp,
        "(() => {"
        " const box = document.querySelector(\"#captchafox [role='checkbox'], #captchafox .cf-checkbox\");"
        " const token = document.querySelector(\"textarea[name='cf-captcha-response']\");"
        " return {checked: box ? box.getAttribute('aria-checked') : '', token: token && token.value ? token.value.length : 0};"
        "})()",
    )
    return raw if isinstance(raw, dict) else {"checked": "", "token": 0}


def find_labeled(cdp: Cdp, selector: str, label: str) -> dict | None:
    expression = (
        "(() => {"
        f" const wanted = {json.dumps(label)};"
        f" const nodes = [...document.querySelectorAll({json.dumps(selector)})];"
        " const norm = (node) => (node.innerText || '').replace(/\\s+/g, ' ').trim();"
        " const exact = nodes.filter((node) => norm(node) === wanted);"
        " const el = exact[0] || nodes.find((node) => norm(node).includes(wanted) && norm(node).length < 80);"
        " if (!el) return null;"
        " const visible = (rect) => { const x = rect.left + rect.width / 2; const y = rect.top + rect.height / 2; return rect.width > 1 && rect.height > 1 && x > 0 && y > 0 && x < window.innerWidth && y < window.innerHeight; };"
        " let r = el.getBoundingClientRect();"
        " if (!visible(r)) { el.scrollIntoView({block: 'center', inline: 'center'}); r = el.getBoundingClientRect(); }"
        " if (r.width < 1 || r.height < 1) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()"
    )
    box = evaluate(cdp, expression)
    return box if isinstance(box, dict) else None


def click_labeled(cdp: Cdp, selector: str, label: str, timeout: float = 30, required: bool = True) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            box = find_labeled(cdp, selector, label)
        except (RuntimeError, TimeoutError):
            box = None
        if box:
            mouse_click(cdp, box["x"], box["y"])
            print(f"Clicked {label}")
            return True
        time.sleep(0.4)
    if required:
        raise SystemExit(f"Could not find {label}")
    return False


def reload_page(cdp: Cdp) -> None:
    print("Refreshing the page")
    cdp.call("Page.enable")
    cdp.call("Page.reload", {"ignoreCache": True}, timeout=45)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            state = str(evaluate(cdp, "document.readyState + '\\n' + location.href") or "")
        except (RuntimeError, TimeoutError):
            time.sleep(0.5)
            continue
        ready, _, href = state.partition("\n")
        if ready == "complete" and href and not href.startswith("about:"):
            print(f"Opened {href}")
            return
        time.sleep(0.5)


ACCEPT_SELECTOR = "#save-all-pur, button[data-id='accept-button'], button[aria-label='Akzeptieren und weiter']"


def accept_button_box(cdp: Cdp) -> dict | None:
    box = element_box(cdp, ACCEPT_SELECTOR)
    if box:
        return box
    shadow = evaluate(
        cdp,
        "(() => {"
        f" const sel = {json.dumps(ACCEPT_SELECTOR)};"
        " const visit = (root) => {"
        " const found = root.querySelector?.(sel);"
        " if (found) return found;"
        " for (const node of root.querySelectorAll?.('*') || []) {"
        " if (!node.shadowRoot) continue;"
        " const nested = visit(node.shadowRoot);"
        " if (nested) return nested;"
        " }"
        " return null;"
        " };"
        " const el = visit(document);"
        " if (!el) return null;"
        " el.scrollIntoView({block: 'center', inline: 'center'});"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 1 || r.height < 1) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()",
    )
    if isinstance(shadow, dict):
        return shadow
    same_origin = evaluate(
        cdp,
        "(() => {"
        f" const sel = {json.dumps(ACCEPT_SELECTOR)};"
        " for (const frame of document.querySelectorAll('iframe')) {"
        " let doc = null;"
        " try { doc = frame.contentDocument; } catch (error) { continue; }"
        " if (!doc) continue;"
        " const el = doc.querySelector(sel);"
        " if (!el) continue;"
        " const inner = el.getBoundingClientRect();"
        " const outer = frame.getBoundingClientRect();"
        " if (inner.width < 1 || inner.height < 1) continue;"
        " return {x: outer.left + inner.left + inner.width / 2, y: outer.top + inner.top + inner.height / 2};"
        " }"
        " return null;"
        "})()",
    )
    if isinstance(same_origin, dict):
        return same_origin
    button_box = consent_button_box(cdp)
    if not button_box:
        return None
    overlay = element_box(cdp, "iframe.permission-core-iframe, iframe[src*='permission/live']")
    if not overlay:
        return button_box
    return {
        "x": float(overlay["left"]) + float(button_box["x"]),
        "y": float(overlay["top"]) + float(button_box["y"]),
    }


def child_frames(cdp: Cdp) -> list[dict]:
    try:
        cdp.call("Page.enable")
        tree = cdp.call("Page.getFrameTree")
    except (RuntimeError, TimeoutError):
        return []
    frames: list[dict] = []

    def walk(node: dict) -> None:
        frame = node.get("frame") or {}
        if frame.get("id"):
            frames.append(frame)
        for child in node.get("childFrames") or []:
            if isinstance(child, dict):
                walk(child)

    walk(tree.get("frameTree") or {})
    return frames


def eval_in_frame(cdp: Cdp, frame_id: str, expression: str):
    try:
        world = cdp.call("Page.createIsolatedWorld", {"frameId": frame_id, "grantUniveralAccess": True})
        result = cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": world.get("executionContextId"),
                "returnByValue": True,
            },
        )
    except (RuntimeError, TimeoutError):
        return None
    if result.get("exceptionDetails"):
        return None
    return (result.get("result") or {}).get("value")


def consent_button_box(cdp: Cdp) -> dict | None:
    expression = (
        "(() => {"
        f" const el = document.querySelector({json.dumps(ACCEPT_SELECTOR)});"
        " if (!el) return null;"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 1 || r.height < 1) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()"
    )
    for frame in child_frames(cdp):
        box = eval_in_frame(cdp, str(frame.get("id")), expression)
        if isinstance(box, dict):
            return box
    return None


def consent_still_open(cdp: Cdp) -> bool:
    state = str(evaluate(cdp, "location.href + '\\n' + (!!document.querySelector('iframe.permission-core-iframe, iframe[src*=\"permission\"]'))") or "")
    href, _, flag = state.partition("\n")
    if "consent-management" in href:
        return True
    return flag == "true"


def accept_cookies(cdp: Cdp, timeout: float = 6) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        box = accept_button_box(cdp)
        if box:
            mouse_click(cdp, box["x"], box["y"])
            print("Clicked Akzeptieren und weiter")
            until = time.time() + 10
            while time.time() < until and consent_still_open(cdp):
                time.sleep(0.4)
            return
        time.sleep(0.4)


def wait_for_text(cdp: Cdp, label: str, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    needle = json.dumps(label)
    while time.time() < deadline:
        if evaluate(cdp, f"!!(document.body && document.body.innerText.includes({needle}))"):
            return True
        time.sleep(0.4)
    return False


def open_registration(cdp: Cdp) -> None:
    register_selector = "a[data-testid='register_button'], button.account-avatar__button, a, button"
    for attempt in range(3):
        accept_cookies(cdp, timeout=20)
        if click_labeled(cdp, register_selector, "Registrieren", timeout=15, required=False):
            break
        if attempt == 2:
            raise SystemExit("Could not find Registrieren")
        reload_page(cdp)
    if not wait_for_text(cdp, "Jetzt registrieren", timeout=15):
        print("Jetzt registrieren did not appear")
        click_labeled(cdp, "a[data-testid='register_button']", "Registrieren")
        if not wait_for_text(cdp, "Jetzt registrieren"):
            raise SystemExit("Jetzt registrieren did not appear")
    accept_cookies(cdp)
    click_labeled(cdp, "a[href*='register.gmx.net'], a[role='button'], a.key.button", "Jetzt registrieren")
    deadline = time.time() + 30
    while time.time() < deadline:
        href = str(evaluate(cdp, "location.href") or "")
        if "register.gmx.net" in href or element_box(cdp, "#firstName"):
            print(f"Registration form: {href}")
            return
        time.sleep(0.4)
    raise SystemExit("Registration form did not open")


def weiter_state(cdp: Cdp) -> dict:
    raw = evaluate(
        cdp,
        "(() => {"
        " const button = [...document.querySelectorAll(\"button[data-testid='next-button']\")]"
        " .find((el) => /Weiter/i.test((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')));"
        " const text = document.body ? document.body.innerText : '';"
        " return {"
        " found: !!button,"
        " disabled: button ? !!(button.disabled || button.hasAttribute('disabled')) : true,"
        " taken: text.includes('Leider schon vergeben')"
        " };"
        "})()",
    )
    return raw if isinstance(raw, dict) else {"found": False, "disabled": True, "taken": False}


def email_selector(cdp: Cdp) -> str:
    selector = evaluate(
        cdp,
        "(() => {"
        " const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };"
        " const nodes = [...document.querySelectorAll('input')].filter((el) => {"
        " const key = ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.getAttribute('data-testid') || '') + ' ' + (el.placeholder || '')).toLowerCase();"
        " return el.type === 'email' || /mail|alias|localpart|wunsch/.test(key);"
        " });"
        " const el = nodes.find(visible);"
        " if (!el) return '';"
        " if (el.id) return '#' + CSS.escape(el.id);"
        " if (el.getAttribute('data-testid')) return 'input[data-testid=\"' + el.getAttribute('data-testid') + '\"]';"
        " if (el.name) return 'input[name=\"' + el.name + '\"]';"
        " return '';"
        "})()",
    )
    return str(selector or "")


def tweak_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(1, 3)))
    local = re.sub(r"[^A-Za-z0-9._-]", "", local) + suffix
    if len(local) > 40:
        local = local[: 40 - len(suffix)] + suffix
    return local + (separator + domain if separator else "")


def visible_mailbox(cdp: Cdp, fallback: str) -> str:
    try:
        found = evaluate(
            cdp,
            "(() => {"
            " const text = document.body ? document.body.innerText : '';"
            " const match = text.match(/[A-Za-z0-9._%+-]+@gmx\\.[A-Za-z.]+/);"
            " return match ? match[0] : '';"
            "})()",
        )
    except (RuntimeError, TimeoutError):
        found = ""
    mailbox = str(found or "").strip()
    return mailbox or fallback


def save_account(email: str, password: str) -> None:
    accounts: list[dict[str, str]] = []
    if ACCOUNTS_FILE.is_file():
        try:
            raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, dict):
            raw = [raw]
        if isinstance(raw, list):
            accounts = [item for item in raw if isinstance(item, dict)]
    accounts.append({"email": email, "password": password})
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2), encoding="utf-8")
    print(f"Saved {email} to {ACCOUNTS_FILE.name}")


def choose_email(cdp: Cdp, fallback_email: str) -> str:
    deadline = time.time() + 25
    attempts = 0
    while time.time() < deadline:
        if element_box(cdp, "#postalCode"):
            print("Address form is already open")
            return visible_mailbox(cdp, fallback_email)
        state = weiter_state(cdp)
        if state.get("found") and not state.get("disabled") and not state.get("taken"):
            if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
                raise SystemExit("Could not find the Weiter button")
            print("Clicked Weiter")
            return visible_mailbox(cdp, fallback_email)
        selector = email_selector(cdp)
        needs_change = bool(selector) and (state.get("taken") or (state.get("found") and state.get("disabled")))
        if needs_change and not state.get("taken"):
            time.sleep(3)
            if element_box(cdp, "#postalCode"):
                print("Address form is already open")
                return visible_mailbox(cdp, fallback_email)
            state = weiter_state(cdp)
            if state.get("found") and not state.get("disabled") and not state.get("taken"):
                if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
                    raise SystemExit("Could not find the Weiter button")
                print("Clicked Weiter")
                return visible_mailbox(cdp, fallback_email)
            needs_change = bool(email_selector(cdp)) and (state.get("taken") or (state.get("found") and state.get("disabled")))
            selector = email_selector(cdp)
        if needs_change:
            if attempts >= 8:
                raise SystemExit("Weiter stayed disabled after changing the email")
            attempts += 1
            current = field_value(cdp, selector).strip() or fallback_email.split("@", 1)[0]
            updated = tweak_email(current)
            try:
                type_into(cdp, selector, updated)
            except SystemExit:
                time.sleep(1)
                if element_box(cdp, "#postalCode"):
                    print("Address form is already open")
                    return visible_mailbox(cdp, updated if "@" in updated else fallback_email)
                raise
            print(f"Email updated to {updated}")
            time.sleep(1.5)
            continue
        time.sleep(0.4)
    raise SystemExit("Could not get past the email step")


def choose_female(cdp: Cdp) -> None:
    deadline = time.time() + 20
    while time.time() < deadline and not element_box(cdp, "#FEMALE, label[for='FEMALE']"):
        time.sleep(0.4)
    if not click_selector(cdp, "#FEMALE") and not click_selector(cdp, "label[for='FEMALE']"):
        raise SystemExit("Could not find the female option")
    checked = evaluate(cdp, "!!(document.querySelector('#FEMALE') && document.querySelector('#FEMALE').checked)")
    if not checked:
        evaluate(
            cdp,
            "(() => { const el = document.querySelector('#FEMALE'); if (!el) return false;"
            " el.checked = true; el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); return el.checked; })()",
        )
    print("Selected female")


def select_country(cdp: Cdp, country: str) -> None:
    selected = evaluate(
        cdp,
        "(() => {"
        " const el = document.querySelector('#country');"
        " if (!el) return '';"
        f" el.value = {json.dumps(country)};"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true}));"
        " return el.value;"
        "})()",
    )
    if selected != country:
        raise SystemExit(f"Could not select country {country}")
    print(f"Selected country {country}")


def fill_address(cdp: Cdp, postal_code: str, city: str, street: str, country: str) -> None:
    deadline = time.time() + 20
    while time.time() < deadline and not element_box(cdp, "#postalCode"):
        time.sleep(0.4)
    if not element_box(cdp, "#postalCode"):
        raise SystemExit("Address form did not appear")
    choose_female(cdp)
    type_into(cdp, "#postalCode", postal_code)
    type_into(cdp, "#city", city)
    type_into(cdp, "#street", street)
    select_country(cdp, country)
    print(f"Filled {postal_code} {city}, {street}")
    if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
        raise SystemExit("Could not find the address Weiter button")
    print("Clicked Weiter")


def fill_password(cdp: Cdp, password: str) -> None:
    deadline = time.time() + 20
    while time.time() < deadline and not element_box(cdp, "#password"):
        time.sleep(0.4)
    if not element_box(cdp, "#password"):
        raise SystemExit("Password form did not appear")
    type_into(cdp, "#password", password)
    type_into(cdp, "#confirmPassword", password)
    print("Filled password and confirmation")
    if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
        raise SystemExit("Could not find the password Weiter button")
    print("Clicked Weiter")


def select_option(cdp: Cdp, selector: str, value: str) -> str:
    selected = evaluate(
        cdp,
        "(() => {"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return '';"
        f" el.value = {json.dumps(value)};"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true}));"
        " return el.value;"
        "})()",
    )
    return str(selected or "")


def load_store() -> tuple[dict[str, int], list[str]]:
    if not USAGE_FILE.is_file():
        return {}, []
    try:
        raw = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, []
    if not isinstance(raw, dict):
        return {}, []
    usage: dict[str, int] = {}
    for key, value in raw.items():
        if key == "banned":
            continue
        try:
            usage[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    banned_raw = raw.get("banned")
    banned = [str(number) for number in banned_raw] if isinstance(banned_raw, list) else []
    return usage, banned


def save_store(usage: dict[str, int], banned: list[str]) -> None:
    payload: dict[str, int | list[str]] = dict(usage)
    payload["banned"] = banned
    USAGE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_usage() -> dict[str, int]:
    usage, _banned = load_store()
    return usage


def save_usage(usage: dict[str, int]) -> None:
    _usage, banned = load_store()
    save_store(usage, banned)


def record_number_use(number: str) -> int:
    usage, banned = load_store()
    count = usage.get(number, 0) + 1
    usage[number] = count
    save_store(usage, banned)
    return count


def ban_number(number: str) -> None:
    usage, banned = load_store()
    if number not in banned:
        banned.append(number)
    save_store(usage, banned)
    print(f"Banned {number}")


def anosim_test_mode() -> bool:
    return os.environ.get("ANOSIM_MODE", "").strip().lower() in {"test", "1", "true", "yes"}


def bump_phone(number: str) -> str:
    raw = number.strip()
    digits = raw.lstrip("+")
    if not digits.isdigit():
        raise SystemExit(f"Cannot adjust phone number {number}")
    bumped = str(int(digits) + 1).zfill(len(digits))
    return f"+{bumped}" if raw.startswith("+") else bumped


def anosim_credentials() -> tuple[str, str]:
    email = os.environ.get("ANOSIM_EMAIL", "").strip()
    password = os.environ.get("ANOSIM_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit(f"Set ANOSIM_EMAIL and ANOSIM_PASSWORD in {ENV_FILE}")
    return email, password


def split_phone(number: str) -> tuple[str, str]:
    digits = number.strip().lstrip("+")
    for code in ("49", "43", "41"):
        if digits.startswith(code) and len(digits) > len(code):
            return f"+{code}", digits[len(code) :]
    raise SystemExit(f"Unsupported phone number {number}")


def open_tab(cdp: Cdp, port: int, url: str) -> tuple[Cdp, CdpSocket]:
    created = cdp.call("Target.createTarget", {"url": url})
    target_id = str(created.get("targetId") or "")
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
        except (requests.RequestException, ValueError):
            time.sleep(0.4)
            continue
        for item in targets:
            if item.get("id") == target_id and item.get("webSocketDebuggerUrl"):
                socket = CdpSocket(item["webSocketDebuggerUrl"])
                return Cdp(socket), socket
        time.sleep(0.4)
    raise SystemExit("Could not open the Anosim tab")


def login_anosim(cdp: Cdp) -> None:
    email, password = anosim_credentials()
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            if element_box(cdp, "#Input_Email"):
                break
            href = str(evaluate(cdp, "location.href") or "")
        except (RuntimeError, TimeoutError):
            time.sleep(0.4)
            continue
        if href.startswith("http") and "login" not in href.lower():
            print("Anosim session already signed in")
            return
        time.sleep(0.4)
    if not element_box(cdp, "#Input_Email"):
        raise SystemExit("Anosim login form did not appear")
    type_into(cdp, "#Input_Email", email)
    type_into(cdp, "#Input_Password", password)
    if not click_selector(cdp, "#login-submit"):
        raise SystemExit("Could not find the Anosim login button")
    print("Logged in to Anosim")
    until = time.time() + 20
    while time.time() < until:
        try:
            href = str(evaluate(cdp, "location.href") or "")
        except (RuntimeError, TimeoutError):
            time.sleep(0.4)
            continue
        if href and "login" not in href.lower():
            return
        time.sleep(0.4)
    raise SystemExit("Anosim login did not finish")


def free_numbers(cdp: Cdp) -> list[dict]:
    navigate(cdp, ANOSIM_NUMBERS)
    deadline = time.time() + 20
    raw = None
    while time.time() < deadline:
        raw = evaluate(
            cdp,
            "(() => [...document.querySelectorAll('a.js-free-number')].filter((link) => !link.classList.contains('opacity-50')).map((link) => ({number: link.dataset.number || '', href: link.getAttribute('href') || ''})).filter((item) => item.number && item.href))()",
        )
        if isinstance(raw, list) and raw:
            break
        time.sleep(0.5)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


class NoPhonesLeft(Exception):
    pass


def pick_free_number(cdp: Cdp, skip: set[str] | None = None) -> dict:
    usage, banned = load_store()
    banned_numbers = set(banned)
    skipped = skip or set()
    available = [
        item for item in free_numbers(cdp)
        if str(item.get("number")) not in banned_numbers
        and str(item.get("number")) not in skipped
        and usage.get(str(item.get("number")), 0) < MAX_FREE_NUMBER_USES
    ]
    if not available:
        raise NoPhonesLeft("No enabled German free numbers are left. Each remaining number is banned, already used 20 times, or failed to receive a GMX SMS.")
    available.sort(key=lambda item: usage.get(str(item.get("number")), 0))
    chosen = available[0]
    number = str(chosen["number"])
    print(f"Using {number} ({usage.get(number, 0)}/{MAX_FREE_NUMBER_USES} uses)")
    return chosen


def number_url(item: dict) -> str:
    href = str(item.get("href") or "")
    if href.startswith("/"):
        return "https://anosim.net" + href
    return href


def inbox_messages(cdp: Cdp) -> list[str]:
    raw = evaluate(
        cdp,
        "(() => {"
        " const nodes = [...document.querySelectorAll('.message-item, .free-numbers-list-group-messages .list-group-item')];"
        " return nodes.map((item) => (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);"
        "})()",
    )
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def first_message_text(cdp: Cdp) -> str:
    messages = inbox_messages(cdp)
    return messages[0] if messages else ""


def code_from_message(text: str) -> str:
    if "GMX" not in text.upper() and "identit" not in text.lower():
        return ""
    labeled = re.search(r"(?:lautet|code)\s*[:：]?\s*(\d{4,8})", text, re.IGNORECASE)
    if labeled:
        return labeled.group(1)
    match = re.search(r"\b(\d{6})\b", text)
    return match.group(1) if match else ""


def code_from_inbox(messages: list[str]) -> tuple[str, str]:
    for text in messages:
        code = code_from_message(text)
        if code:
            return code, text
    return "", messages[0] if messages else ""


def click_refresh(cdp: Cdp, number: str) -> bool:
    selectors = [
        f"button.js-refresh[data-number={json.dumps(number)}]",
        f"button.js-refresh[data-number={json.dumps(number.lstrip('+'))}]",
        "button.js-refresh",
    ]
    for selector in selectors:
        if click_selector(cdp, selector):
            return True
    clicked = evaluate(
        cdp,
        "(() => {"
        f" const wanted = [{json.dumps(number)}, {json.dumps(number.lstrip('+'))}];"
        " const buttons = [...document.querySelectorAll('button.js-refresh')];"
        " const button = buttons.find((el) => wanted.includes(el.dataset.number || '')) || buttons[0];"
        " if (!button) return false;"
        " button.click();"
        " return true;"
        "})()",
    )
    return bool(clicked)


def open_number_page(cdp: Cdp, item: dict) -> str:
    number = str(item.get("number") or "")
    navigate(cdp, number_url(item))
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            if inbox_messages(cdp) or element_box(cdp, "button.js-refresh"):
                break
        except (RuntimeError, TimeoutError):
            pass
        time.sleep(0.4)
    if not inbox_messages(cdp):
        navigate(cdp, ANOSIM_NUMBERS)
        selector = f"a.js-free-number[data-number={json.dumps(number)}]"
        if not click_selector(cdp, selector):
            navigate(cdp, number_url(item))
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                if inbox_messages(cdp) or element_box(cdp, "button.js-refresh"):
                    break
            except (RuntimeError, TimeoutError):
                pass
            time.sleep(0.4)
    try:
        return first_message_text(cdp)
    except (RuntimeError, TimeoutError):
        return ""


def fetch_sms_code(
    cdp: Cdp,
    item: dict,
    previous: str,
    test_mode: bool = False,
    require_new: bool = False,
    attempts: int = 1,
) -> tuple[str, str] | None:
    number = str(item.get("number") or "")
    open_number_page(cdp, item)
    for _attempt in range(attempts):
        if not click_refresh(cdp, number):
            open_number_page(cdp, item)
            if not click_refresh(cdp, number):
                print("Could not click the Anosim refresh button")
                continue
        print("Refreshed Anosim messages")
        pause = random.uniform(10, 15)
        print(f"Waiting {pause:.1f}s for the refreshed inbox")
        time.sleep(pause)
        try:
            messages = inbox_messages(cdp)
        except (RuntimeError, TimeoutError):
            messages = []
        if not messages and test_mode and not require_new and previous:
            messages = [previous]
        text = messages[0] if messages else ""
        if not text or (text == previous and (not test_mode or require_new)):
            preview = text[:160] if text else "empty inbox"
            print(f"No new GMX message yet: {preview}")
            continue
        code, matched = code_from_inbox(messages)
        if not code and test_mode and not require_new:
            code = code_from_message(previous)
            matched = previous
        if code:
            print(f"SMS code {code}")
            return code, matched or text
        preview = " | ".join(message[:80] for message in messages[:3])
        print(f"No GMX code in the inbox yet: {preview}")
    return None


def page_url(cdp: Cdp) -> str:
    try:
        return str(evaluate(cdp, "location.href") or "")
    except (RuntimeError, TimeoutError):
        return ""


def registration_failed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc == "hilfe.gmx.net" and parsed.path.rstrip("/").endswith("flexreg.html")


def go_back(cdp: Cdp) -> None:
    try:
        cdp.call("Page.enable")
        history = cdp.call("Page.getNavigationHistory")
        index = int(history.get("currentIndex") or 0)
        entries = history.get("entries") or []
        if index > 0 and isinstance(entries[index - 1], dict):
            cdp.call("Page.navigateToHistoryEntry", {"entryId": entries[index - 1].get("id")})
            print("Moved back to the previous page")
            return
    except (RuntimeError, TimeoutError, TypeError, ValueError, IndexError) as exc:
        print(f"Could not use navigation history: {exc}")
    try:
        evaluate(cdp, "history.back()")
    except (RuntimeError, TimeoutError) as exc:
        raise SystemExit(f"Could not move back to the phone form: {exc}") from exc
    print("Moved back to the previous page")


def phone_form_ready(cdp: Cdp) -> bool:
    if registration_failed(page_url(cdp)):
        return False
    try:
        return bool(element_box(cdp, "#mobilePhone"))
    except (RuntimeError, TimeoutError):
        return False


def return_to_phone_form(cdp: Cdp) -> None:
    for _step in range(3):
        if phone_form_ready(cdp):
            return
        go_back(cdp)
        deadline = time.time() + 15
        while time.time() < deadline:
            if phone_form_ready(cdp):
                return
            time.sleep(0.4)
    raise SystemExit("Phone form did not return after the rejected number")


def phone_number_rejected(cdp: Cdp, timeout: float = 8) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if registration_failed(page_url(cdp)):
            return True
        try:
            if element_box(cdp, "[name='confirmationCode.0.digit']"):
                return False
        except (RuntimeError, TimeoutError):
            pass
        time.sleep(0.4)
    return registration_failed(page_url(cdp))


def submit_confirmation_code(cdp: Cdp, code: str) -> str:
    digit = "[name='confirmationCode.0.digit']"
    consent = "button[aria-label='Zustimmen und weiter'][data-testid='next-button']"
    deadline = time.time() + 25
    while time.time() < deadline and not element_box(cdp, digit):
        time.sleep(0.4)
    if not element_box(cdp, digit):
        return "closed"
    for index, character in enumerate(code[:6]):
        type_into(cdp, f"[name='confirmationCode.{index}.digit']", character)
    print("Entered confirmation code")
    if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
        raise SystemExit("Could not find the confirmation Weiter button")
    print("Clicked Weiter")
    print("Waiting 2s before Zustimmen und weiter")
    time.sleep(2)
    if registration_failed(page_url(cdp)):
        return "banned"
    deadline = time.time() + 8
    while time.time() < deadline:
        if registration_failed(page_url(cdp)):
            return "banned"
        if element_box(cdp, consent) and click_selector(cdp, consent):
            print("Clicked Zustimmen und weiter")
            return "accepted"
        time.sleep(0.4)
    if element_box(cdp, digit):
        print("Confirmation code was rejected")
        return "rejected"
    return "closed"


def verify_phone(gmx: Cdp, port: int) -> None:
    anosim, anosim_socket = open_tab(gmx, port, ANOSIM_LOGIN)
    try:
        login_anosim(anosim)
        test_mode = anosim_test_mode()
        skipped: set[str] = set()
        while True:
            chosen = pick_free_number(anosim, skipped)
            source = str(chosen["number"])
            previous_message = open_number_page(anosim, chosen)
            submit = bump_phone(source) if test_mode else source
            if test_mode:
                print(f"Test mode: GMX gets {submit}; SMS is read from {source}")
            prefix, national = split_phone(submit)
            deadline = time.time() + 25
            while time.time() < deadline and not element_box(gmx, "#mobilePhone"):
                time.sleep(0.4)
            if not element_box(gmx, "#mobilePhone"):
                raise SystemExit("Phone form did not appear")
            selected = select_option(gmx, "#mobilePhonePrefix", prefix)
            if selected != prefix:
                raise SystemExit(f"Could not select country code {prefix}")
            type_into(gmx, "#mobilePhone", national)
            print(f"Filled {prefix} {national}")
            if not click_selector(gmx, "button[aria-label='Weiter'][data-testid='next-button']"):
                raise SystemExit("Could not find the phone Weiter button")
            print("Clicked Weiter")
            if phone_number_rejected(gmx):
                print(f"{source} opened https://hilfe.gmx.net/kundenservice/flexreg.html")
                ban_number(source)
                return_to_phone_form(gmx)
                continue
            if test_mode:
                print(f"Test mode leaves {source} unused")
            else:
                count = record_number_use(source)
                print(f"{source} use count is now {count}")
            pause = random.uniform(3, 4)
            print(f"Waiting {pause:.1f}s for the SMS")
            time.sleep(pause)
            message = previous_message
            switch_number = False
            for attempt in range(3):
                fetched = fetch_sms_code(anosim, chosen, message, test_mode, require_new=attempt > 0, attempts=1)
                if fetched is None:
                    print(f"No GMX SMS for {source} on try {attempt + 1} of 3")
                    continue
                code, message = fetched
                result = submit_confirmation_code(gmx, code)
                if result == "banned":
                    print(f"{source} opened https://hilfe.gmx.net/kundenservice/flexreg.html")
                    ban_number(source)
                    return_to_phone_form(gmx)
                    switch_number = True
                    break
                if result == "accepted":
                    return
                if result == "closed":
                    print("Confirmation step closed")
                    return
                print("Confirmation code was rejected")
            else:
                print(f"No working GMX SMS for {source} after 3 tries")
                skipped.add(source)
                return_to_phone_form(gmx)
                continue
            if switch_number:
                continue
    finally:
        anosim_socket.close()


def fill_signup(
    cdp: Cdp,
    email: str,
    first_name: str,
    last_name: str,
    day: str,
    month: str,
    year: str,
    postal_code: str,
    city: str,
    street: str,
    country: str,
    password: str,
    port: int,
) -> str:
    accept_cookies(cdp, timeout=3)
    deadline = time.time() + 30
    while time.time() < deadline and not element_box(cdp, "#firstName"):
        time.sleep(0.4)
    if not element_box(cdp, "#firstName"):
        raise SystemExit("Signup form did not appear")

    type_into(cdp, "#firstName", first_name)
    type_into(cdp, "#lastName", last_name)
    type_into(cdp, "#birthDay", day)
    type_into(cdp, "#birthMonth", month)
    type_into(cdp, "#birthYear", year)
    print(f"Filled {first_name} {last_name}, {day}.{month}.{year}")
    pause = random.uniform(3, 4)
    print(f"Waiting {pause:.1f}s before I am human")
    time.sleep(pause)

    click_human_check(cdp)
    until = time.time() + 20
    status = captcha_status(cdp)
    while time.time() < until and status.get("checked") != "true" and not status.get("token"):
        time.sleep(1)
        status = captcha_status(cdp)
    if status.get("checked") == "true" or status.get("token"):
        print("I am human check completed")
    else:
        print("I am human was clicked. CaptchaFox has not confirmed it yet.")

    if not click_selector(cdp, "button[data-testid='next-button']"):
        raise SystemExit("Could not find the Get started button")
    print("Clicked Get started")
    mailbox = choose_email(cdp, email)
    pause = random.uniform(3, 4)
    print(f"Waiting {pause:.1f}s before the address form")
    time.sleep(pause)
    fill_address(cdp, postal_code, city, street, country)
    fill_password(cdp, password)
    verify_phone(cdp, port)
    return visible_mailbox(cdp, mailbox)


def ask(value: str | None, label: str) -> str:
    text = (value or "").strip()
    if text:
        return text
    if sys.stdin.isatty():
        text = input(f"{label}: ").strip()
    if not text:
        raise SystemExit(f"Missing {label}")
    return text


def normalize_name(value: str, label: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > 40:
        raise SystemExit(f"{label} must be 1 to 40 characters")
    return name


def normalize_birth(day: str, month: str, year: str) -> tuple[str, str, str]:
    try:
        born = datetime.date(int(year), int(month), int(day))
    except ValueError as exc:
        raise SystemExit(f"Birth date is not a real calendar date: {exc}") from exc
    today = datetime.date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if born > today or born.year < 1900 or age < 18:
        raise SystemExit("Birth date must be 18 or older")
    return f"{born.day:02d}", f"{born.month:02d}", f"{born.year:04d}"


def resolve_proxy_type(session: requests.Session, proxy: dict, proxy_type: str, skip_check: bool) -> str:
    if skip_check:
        return proxy_type
    if check_proxy(session, proxy, proxy_type):
        return proxy_type
    fallback = "socks5" if proxy_type == "http" else "http"
    print(f"Retrying proxy check as {fallback}")
    if check_proxy(session, proxy, fallback):
        return fallback
    raise SystemExit("Proxy check failed for http and socks5. Pass --skip-check to open anyway.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a Dolphin profile, open TARGET_URL, and submit the signup form."
    )
    parser.add_argument("email", nargs="?", help="Name for the Dolphin profile")
    parser.add_argument("--first-name", help="First name for the signup form")
    parser.add_argument("--last-name", help="Last name for the signup form")
    parser.add_argument("--day", help="Birth day")
    parser.add_argument("--month", help="Birth month")
    parser.add_argument("--year", help="Birth year")
    parser.add_argument("--plz", help="Postal code")
    parser.add_argument("--city", help="City")
    parser.add_argument("--street", help="Street and house number")
    parser.add_argument("--country", default="DE", help="Country code, for example DE")
    parser.add_argument("--password", help="Password for the new mailbox")
    parser.add_argument("--url", default=TARGET_URL, help="Page to open (default: TARGET_URL from .env)")
    parser.add_argument(
        "--proxies",
        default=str(ROOT / "proxies.txt"),
        help="Proxy list, one user:pass@host:port per line",
    )
    parser.add_argument("--proxy-type", choices=("http", "https", "socks5", "socks4"), default="http")
    parser.add_argument("--headless", action="store_true", help="Ask Dolphin to start headless")
    parser.add_argument("--skip-check", action="store_true", help="Do not ask Dolphin to test the proxy first")
    args = parser.parse_args()
    if not args.email:
        if sys.stdin.isatty():
            args.email = input("Dolphin profile name: ")
        if not args.email:
            raise SystemExit("Pass the profile name: python gmx_gen.py lena-hoffmann")
    email = normalize_profile_name(args.email)
    first_name = normalize_name(ask(args.first_name, "First name"), "First name")
    last_name = normalize_name(ask(args.last_name, "Last name"), "Last name")
    day, month, year = normalize_birth(
        ask(args.day, "Birth day"),
        ask(args.month, "Birth month"),
        ask(args.year, "Birth year"),
    )
    postal_code = ask(args.plz, "PLZ")
    city = normalize_name(ask(args.city, "Ort"), "Ort")
    street = " ".join(ask(args.street, "Straße & Hausnummer").split())
    country = ask(args.country, "Country").upper()
    country_codes = {
        "DE", "BE", "BG", "DK", "EE", "FI", "FR", "GR", "IE", "IT", "HR", "LV", "LT",
        "LU", "MT", "NL", "PL", "PT", "RO", "SE", "CH", "SK", "SI", "ES", "CZ", "HU",
        "GB", "CY",
    }
    if country not in country_codes:
        raise SystemExit("Country must be one of the form codes, for example DE")
    if not street or len(street) > 49:
        raise SystemExit("Straße & Hausnummer must be 1 to 49 characters")
    if not postal_code or len(postal_code) > 10:
        raise SystemExit("PLZ must be 1 to 10 characters")
    password = ask(args.password, "Password")
    if not password or len(password) > 100:
        raise SystemExit("Password must be 1 to 100 characters")
    if not args.url:
        raise SystemExit(f"Set TARGET_URL in {ENV_FILE}")

    proxy = load_random_proxy(Path(args.proxies))
    session = requests.Session()
    local_login(session)
    proxy_type = resolve_proxy_type(session, proxy, args.proxy_type, args.skip_check)
    print(f"Using {proxy_label(proxy, proxy_type)}")

    api = DolphinAPI(collect_garbage_on_init=False)
    profile_id = create_profile(api, email, proxy, proxy_type)
    ws: CdpSocket | None = None
    cdp: Cdp | None = None
    mailbox = email
    try:
        automation = start_profile(session, profile_id, args.headless)
        port = int(automation["port"])
        print(f"Started {email} on port {port}")
        ws, target_id = connect_page(port)
        cdp = Cdp(ws)
        if target_id:
            try:
                cdp.call("Target.activateTarget", {"targetId": target_id})
            except (RuntimeError, TimeoutError) as exc:
                print(f"Could not focus the tab: {exc}")
        if not args.headless:
            maximize_window(cdp, target_id)
        navigate(cdp, args.url)
        open_registration(cdp)
        mailbox = fill_signup(
            cdp,
            email,
            first_name,
            last_name,
            day,
            month,
            year,
            postal_code,
            city,
            street,
            country,
            password,
            port,
        )
    except NoPhonesLeft as exc:
        print(exc)
        finish_profile(session, api, profile_id, cdp, ws)
        raise SystemExit(str(exc)) from exc
    except BaseException:
        if ws is not None:
            try:
                ws.close()
            except OSError:
                pass
        print("Browser left open.")
        raise
    save_account(mailbox, password)
    finish_profile(session, api, profile_id, cdp, ws)


if __name__ == "__main__":
    main()

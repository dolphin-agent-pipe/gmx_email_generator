"""Create a Dolphin Anty profile with the name you provide, open TARGET_URL1 from .env, click through to registration, and fill the signup form.

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
import math
import random
import re
import socket
import string
import struct
import sys
import threading
import time
import urllib.parse
import zlib
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
TARGET_URL1 = os.environ.get("TARGET_URL1", "").strip()
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
            {
                "windowId": window_id,
                "bounds": {"left": 40, "top": 40, "width": 1400, "height": 900, "windowState": "normal"},
            },
        )
        cdp.call(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "maximized"}},
        )
        try:
            cdp.call("Page.bringToFront")
        except (RuntimeError, TimeoutError):
            pass
        print("Opened the browser at full width")
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"Could not maximize the browser: {exc}")


def show_profile_window(profile_id: str) -> None:
    if os.name != "nt" or not profile_id:
        return
    import ctypes
    import subprocess
    from ctypes import wintypes

    env = os.environ.copy()
    env["PROFILE_ID"] = str(profile_id)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:PROFILE_ID) } | ForEach-Object { $_.ProcessId }",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    pids = {int(line) for line in completed.stdout.splitlines() if line.strip().isdigit()}
    if not pids:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    chosen: list[tuple[int, int]] = []

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        chosen.append((area, int(hwnd)))
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(enum_proc, 0)
    if not chosen:
        return
    hwnd = wintypes.HWND(max(chosen)[1])
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    print("Brought the signup browser to the front")


def start_profile(session: requests.Session, profile_id: str, headless: bool) -> dict:
    query = "automation=1&headless=1" if headless else "automation=1&headless=0"
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
        self._lock = threading.Lock()

    def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30,
        session_id: str | None = None,
    ) -> dict:
        with self._lock:
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
    last = ""
    for attempt in range(2):
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
        if attempt == 0:
            print("Page failed to load. Opening it again.")
            continue
        break
    if last:
        print(f"Opened {last}")
    raise SystemExit(f"Browser started, but {url} did not finish loading")


def on_screen(cdp: Cdp, selector: str) -> bool:
    found = evaluate(
        cdp,
        "(() => {"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return false;"
        " const r = el.getBoundingClientRect();"
        " const x = r.left + r.width / 2;"
        " const y = r.top + r.height / 2;"
        " return r.width > 1 && r.height > 1 && x > 0 && y > 0 && x < window.innerWidth && y < window.innerHeight;"
        "})()",
    )
    return found is True


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


def press_enter(cdp: Cdp) -> None:
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "rawKeyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        },
    )
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "char",
            "key": "Enter",
            "code": "Enter",
            "text": "\r",
            "unmodifiedText": "\r",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        },
    )
    cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
    )


def submit_phone_form(cdp: Cdp) -> None:
    press_enter(cdp)
    print("Pressed Enter")
    time.sleep(1.2)
    try:
        advanced = bool(element_box(cdp, "[name='confirmationCode.0.digit']"))
        still_there = bool(element_box(cdp, "#mobilePhone"))
    except (RuntimeError, TimeoutError):
        return
    if advanced or not still_there:
        return
    evaluate(
        cdp,
        "(() => {"
        " const el = document.querySelector('#mobilePhone');"
        " const form = el && el.form;"
        " if (!form) return false;"
        " if (form.requestSubmit) form.requestSubmit();"
        " else form.submit();"
        " return true;"
        "})()",
    )
    print("Submitted the phone form")


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


def signup_buttons(cdp: Cdp) -> str:
    raw = evaluate(
        cdp,
        "(() => {"
        " const nodes = [...document.querySelectorAll('button, [data-testid]')].slice(0, 25);"
        " return nodes.map((el) => {"
        " const r = el.getBoundingClientRect();"
        " const text = (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 60);"
        " return [el.tagName, el.getAttribute('data-testid') || '', text, Math.round(r.width), Math.round(r.height), el.disabled ? 'disabled' : ''].filter(Boolean).join(' ');"
        " }).join(' | ');"
        "})()",
    )
    return str(raw or "")


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


def eval_in_frame(cdp: Cdp, frame_id: str, expression: str, await_promise: bool = False, timeout: float = 30):
    try:
        world = cdp.call(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "grantUniveralAccess": True},
            timeout=timeout,
        )
        result = cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": world.get("executionContextId"),
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
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


def mailbox_address(value: str) -> str:
    match = re.search(
        r"[A-Za-z0-9._%+-]+@(?:web\.de|gmx\.net|gmx\.de|gmx\.com|gmx\.at|gmx\.ch)\b",
        value or "",
        re.IGNORECASE,
    )
    return match.group(0) if match else ""


def visible_mailbox(cdp: Cdp, fallback: str) -> str:
    try:
        found = evaluate(
            cdp,
            "(() => {"
            " const domains = 'web\\\\.de|gmx\\\\.net|gmx\\\\.de|gmx\\\\.com|gmx\\\\.at|gmx\\\\.ch';"
            " const full = new RegExp('[A-Za-z0-9._%+-]+@(?:' + domains + ')', 'i');"
            " const bareDomain = new RegExp('^(?:' + domains + ')$', 'i');"
            " const localOk = (value) => /^[A-Za-z0-9._-]{1,64}$/.test(value);"
            " const fromText = (text) => {"
            " const match = String(text || '').match(full);"
            " return match ? match[0] : '';"
            " };"
            " const inputs = [...document.querySelectorAll('input, textarea')];"
            " for (const el of inputs) {"
            " const direct = fromText(el.value);"
            " if (direct) return direct;"
            " }"
            " for (const el of inputs) {"
            " const local = String(el.value || '').trim();"
            " const key = ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.getAttribute('data-testid') || '') + ' ' + (el.placeholder || '')).toLowerCase();"
            " if (!localOk(local) || (el.type !== 'email' && !/mail|alias|localpart|wunsch/.test(key))) continue;"
            " let node = el.parentElement;"
            " for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {"
            " const select = node.querySelector('select');"
            " const option = select && select.options[select.selectedIndex];"
            " const selected = String((select && select.value) || (option && option.text) || '').trim().replace(/^@/, '');"
            " if (bareDomain.test(selected)) return local + '@' + selected;"
            " const domain = String(node.innerText || '').match(new RegExp('@(' + domains + ')', 'i'));"
            " if (domain) return local + '@' + domain[1];"
            " }"
            " }"
            " const text = document.body ? document.body.innerText : '';"
            " const direct = fromText(text);"
            " if (direct) return direct;"
            " const lines = text.split(/\\n/);"
            " for (let i = 0; i < lines.length; i += 1) {"
            " const joined = (lines[i] + (lines[i + 1] || '')).replace(/\\s+/g, '');"
            " const match = fromText(joined);"
            " if (match) return match;"
            " }"
            " return '';"
            "})()",
        )
    except (RuntimeError, TimeoutError):
        found = ""
    return mailbox_address(str(found or "")) or mailbox_address(fallback)


def confirm_visible_email(cdp: Cdp, fallback: str) -> str:
    captured = visible_mailbox(cdp, fallback)
    if not click_selector(cdp, "button[aria-label='Weiter'][data-testid='next-button']"):
        raise SystemExit("Could not find the Weiter button")
    print("Clicked Weiter")
    mailbox = visible_mailbox(cdp, captured or fallback)
    if mailbox:
        print(f"Using mailbox {mailbox}")
    return mailbox


_accounts_lock = threading.Lock()


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


def save_account(email: str, password: str) -> None:
    email = mailbox_address(email)
    if not email:
        raise SystemExit("Could not save the mailbox because the address was incomplete")
    with _accounts_lock:
        accounts: list[dict[str, str]] = []
        if ACCOUNTS_FILE.is_file():
            try:
                raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup = park_broken(ACCOUNTS_FILE)
                if backup is None:
                    raise SystemExit(f"Could not read {ACCOUNTS_FILE.name}, so the new mailbox was not written over it") from exc
                print(f"Could not read saved mailboxes. Kept the old file as {backup.name}.")
                raw = []
            if isinstance(raw, dict):
                raw = [raw]
            if isinstance(raw, list):
                accounts = [item for item in raw if isinstance(item, dict)]
        replaced = False
        for item in accounts:
            if str(item.get("email") or "").lower() == email.lower():
                item["email"] = email
                item["password"] = password
                replaced = True
                break
        if not replaced:
            accounts.append({"email": email, "password": password})
        write_text_atomic(ACCOUNTS_FILE, json.dumps(accounts, indent=2))
    print(f"Saved {email} to {ACCOUNTS_FILE.name}")


def chosen_mailbox(cdp: Cdp, fallback: str) -> str:
    found = visible_mailbox(cdp, fallback)
    if found:
        print(f"Using mailbox {found}")
    return found


def choose_email(cdp: Cdp, fallback_email: str) -> str:
    deadline = time.time() + 25
    attempts = 0
    while time.time() < deadline:
        selector = email_selector(cdp)
        alias_open = bool(selector) and on_screen(cdp, selector)
        if not alias_open and on_screen(cdp, "#postalCode"):
            print("Address form is already open")
            return chosen_mailbox(cdp, fallback_email)
        state = weiter_state(cdp)
        if state.get("found") and not state.get("disabled") and not state.get("taken"):
            return confirm_visible_email(cdp, fallback_email)
        needs_change = bool(selector) and (state.get("taken") or (state.get("found") and state.get("disabled")))
        if needs_change and not state.get("taken"):
            time.sleep(3)
            selector = email_selector(cdp)
            alias_open = bool(selector) and on_screen(cdp, selector)
            if not alias_open and on_screen(cdp, "#postalCode"):
                print("Address form is already open")
                return chosen_mailbox(cdp, fallback_email)
            state = weiter_state(cdp)
            if state.get("found") and not state.get("disabled") and not state.get("taken"):
                return confirm_visible_email(cdp, fallback_email)
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
                selector = email_selector(cdp)
                if not (selector and on_screen(cdp, selector)) and on_screen(cdp, "#postalCode"):
                    print("Address form is already open")
                    return chosen_mailbox(cdp, updated if "@" in updated else fallback_email)
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


_usage_lock = threading.RLock()
_usage_blocked = False


def load_store() -> tuple[dict[str, int], list[str]]:
    global _usage_blocked
    with _usage_lock:
        if not USAGE_FILE.is_file():
            return {}, []
        try:
            raw = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            backup = park_broken(USAGE_FILE)
            if backup is None:
                _usage_blocked = True
                print(f"Could not read {USAGE_FILE.name}. Number use counts will not be written over it.")
            else:
                print(f"Could not read {USAGE_FILE.name} ({exc}). Kept it as {backup.name}.")
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
    if _usage_blocked:
        print(f"Skipped saving {USAGE_FILE.name} because the current file could not be read")
        return
    payload: dict[str, int | list[str]] = dict(usage)
    payload["banned"] = banned
    with _usage_lock:
        write_text_atomic(USAGE_FILE, json.dumps(payload, indent=2))


def load_usage() -> dict[str, int]:
    usage, _banned = load_store()
    return usage


def save_usage(usage: dict[str, int]) -> None:
    with _usage_lock:
        _usage, banned = load_store()
        save_store(usage, banned)


def record_number_use(number: str) -> int:
    with _usage_lock:
        usage, banned = load_store()
        count = usage.get(number, 0) + 1
        usage[number] = count
        save_store(usage, banned)
        return count


def ban_number(number: str) -> None:
    with _usage_lock:
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


class SignupRestart(Exception):
    pass


class MailboxSettingsError(SystemExit):
    def __init__(self, email: str, reason: str):
        self.email = mailbox_address(email)
        reason = reason.strip()
        prefix = "Could not finish the created mailbox settings"
        if reason.lower().startswith(prefix.lower()):
            text = reason
        else:
            text = f"{prefix}: {reason}"
        if self.email and self.email not in text:
            text = f"{text} ({self.email})"
        super().__init__(text)


PHONE_EDIT_BUTTON = "button.mobile-phone-step__edit-inline"
MAX_SUPPLIED_PHONES = 3


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


def phone_entry_details(cdp: Cdp) -> str:
    try:
        raw = evaluate(
            cdp,
            "(() => {"
            " const input = document.querySelector('#mobilePhone');"
            " const prefix = document.querySelector('#mobilePhonePrefix');"
            " const options = prefix ? [...prefix.options].slice(0, 6).map((option) => option.value + '=' + option.text.trim()) : [];"
            " return {placeholder: input ? input.placeholder : '', value: input ? input.value : '', prefix: prefix ? prefix.value : '', options};"
            "})()",
        )
    except (RuntimeError, TimeoutError):
        return ""
    return str(raw or "").encode("ascii", errors="replace").decode("ascii")


def phone_variants(national: str) -> list[str]:
    variants = [national]
    if national.startswith("0"):
        trimmed = national[1:]
        if trimmed:
            variants.append(trimmed)
    else:
        variants.append("0" + national)
    return variants


def form_message(cdp: Cdp) -> str:
    try:
        raw = evaluate(
            cdp,
            "(() => {"
            " const nodes = [...document.querySelectorAll('[role=\"alert\"], [class*=\"error\"], [class*=\"Error\"]')];"
            " return nodes.map((node) => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 3).join(' / ');"
            "})()",
        )
    except (RuntimeError, TimeoutError):
        return ""
    return str(raw or "")[:240]


def keep_browser_open(cdp: Cdp, func):
    stop = threading.Event()

    def ping() -> None:
        while not stop.wait(5):
            try:
                cdp.call("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=10)
            except (RuntimeError, TimeoutError, OSError):
                return

    thread = threading.Thread(target=ping, name="signup-browser-keepalive", daemon=True)
    thread.start()
    try:
        return func()
    finally:
        stop.set()
        thread.join(12)


def page_url(cdp: Cdp) -> str:
    try:
        return str(evaluate(cdp, "location.href") or "")
    except (RuntimeError, TimeoutError):
        return ""


def registration_failed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() not in {"hilfe.gmx.net", "hilfe.web.de"}:
        return False
    return parsed.path.rstrip("/").endswith("flexreg.html")


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


def prepare_phone_entry(cdp: Cdp, replace: bool, stop=None) -> None:
    if replace:
        deadline = time.time() + 12
        clicked = False
        while time.time() < deadline:
            if stop:
                stop()
            if click_selector(cdp, PHONE_EDIT_BUTTON):
                print("Clicked the phone edit button")
                clicked = True
                break
            try:
                if element_box(cdp, "#mobilePhone"):
                    break
            except (RuntimeError, TimeoutError):
                pass
            time.sleep(0.4)
        if not clicked:
            try:
                ready = bool(element_box(cdp, "#mobilePhone"))
            except (RuntimeError, TimeoutError):
                ready = False
            if not ready:
                raise SystemExit("Could not find the phone edit button")
    deadline = time.time() + 20
    while time.time() < deadline:
        if stop:
            stop()
        try:
            if element_box(cdp, "#mobilePhone"):
                return
        except (RuntimeError, TimeoutError):
            pass
        time.sleep(0.4)
    raise SystemExit("Phone form did not appear")


def phone_submission_state(cdp: Cdp, timeout: float = 8) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if registration_failed(page_url(cdp)):
            return "rejected"
        try:
            if element_box(cdp, "[name='confirmationCode.0.digit']"):
                return "code"
        except (RuntimeError, TimeoutError):
            pass
        time.sleep(0.4)
    if registration_failed(page_url(cdp)):
        return "rejected"
    try:
        if element_box(cdp, "[name='confirmationCode.0.digit']"):
            return "code"
    except (RuntimeError, TimeoutError):
        pass
    return "rejected"


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


def visible_consent(cdp: Cdp) -> dict | None:
    labels = ["Zustimmen und fortfahren", "Zustimmen und weiter"]
    found = evaluate(
        cdp,
        "(() => {"
        f" const labels = {json.dumps(labels)};"
        " const buttons = [...document.querySelectorAll(\"button[data-testid='next-button']\")];"
        " const box = (el) => {"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 1 || r.height < 1) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        " };"
        " for (const label of labels) {"
        " for (const el of buttons) {"
        " const name = (el.getAttribute('aria-label') || el.innerText || '').replace(/\\s+/g, ' ').trim();"
        " if (name !== label || !box(el)) continue;"
        " el.scrollIntoView({block: 'center', inline: 'center'});"
        " const point = box(el);"
        " if (!point) continue;"
        " return {label, x: point.x, y: point.y};"
        " }"
        " }"
        " return null;"
        "})()",
    )
    if not isinstance(found, dict) or not found.get("label"):
        return None
    return found


def click_visible_consent(cdp: Cdp) -> str:
    found = visible_consent(cdp)
    if not found:
        return ""
    label = str(found["label"])
    mouse_click(cdp, float(found["x"]), float(found["y"]))
    time.sleep(0.5)
    still = visible_consent(cdp)
    if still and still.get("label") == label:
        evaluate(
            cdp,
            "(() => {"
            f" const label = {json.dumps(label)};"
            " const button = [...document.querySelectorAll(\"button[data-testid='next-button']\")].find((el) => {"
            " const name = (el.getAttribute('aria-label') || el.innerText || '').replace(/\\s+/g, ' ').trim();"
            " const r = el.getBoundingClientRect();"
            " return name === label && r.width > 1 && r.height > 1;"
            " });"
            " if (button) button.click();"
            " return !!button;"
            "})()",
        )
    print(f"Clicked {label}")
    return label


def submit_confirmation_code(cdp: Cdp, code: str) -> str:
    digit = "[name='confirmationCode.0.digit']"
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
    print("Waiting 2s before the consent button")
    time.sleep(2)
    if registration_failed(page_url(cdp)):
        return "banned"
    deadline = time.time() + 20
    while time.time() < deadline:
        if registration_failed(page_url(cdp)):
            return "banned"
        current = visible_consent(cdp)
        if not current:
            time.sleep(0.4)
            continue
        label = str(current["label"])
        click_visible_consent(cdp)
        pause = random.uniform(5, 7)
        print(f"Waiting {pause:.1f}s after {label}")
        time.sleep(pause)
        print("Opening mailbox settings")
        enable_mailbox_access(cdp)
        return "accepted"
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
        replace_phone = False
        while True:
            chosen = pick_free_number(anosim, skipped)
            source = str(chosen["number"])
            previous_message = open_number_page(anosim, chosen)
            submit = bump_phone(source) if test_mode else source
            if test_mode:
                print(f"Test mode: GMX gets {submit}; SMS is read from {source}")
            prefix, national = split_phone(submit)
            try:
                prepare_phone_entry(gmx, replace_phone)
            except SystemExit:
                if not replace_phone:
                    raise
                return_to_phone_form(gmx)
                prepare_phone_entry(gmx, False)
            selected = select_option(gmx, "#mobilePhonePrefix", prefix)
            if selected != prefix:
                raise SystemExit(f"Could not select country code {prefix}")
            type_into(gmx, "#mobilePhone", national)
            print(f"Filled {prefix} {national}")
            submit_phone_form(gmx)
            replace_phone = True
            if phone_number_rejected(gmx):
                print(f"{source} opened https://hilfe.gmx.net/kundenservice/flexreg.html")
                ban_number(source)
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
                continue
            if switch_number:
                continue
    finally:
        anosim_socket.close()


def mail_session(cdp: Cdp) -> str | None:
    saved = getattr(cdp, "_mail_session", None)
    if saved:
        return str(saved)
    try:
        payload = cdp.call("Target.getTargets")
    except (RuntimeError, TimeoutError):
        return None
    for info in payload.get("targetInfos") or []:
        url = str(info.get("url") or "").lower()
        if not any(part in url for part in ("weblink.", "webmailer.")) or not info.get("targetId"):
            continue
        try:
            attached = cdp.call("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
        except (RuntimeError, TimeoutError):
            continue
        session_id = attached.get("sessionId")
        if session_id:
            cdp._mail_session = str(session_id)
            return str(session_id)
    return None


def mail_offset(cdp: Cdp) -> tuple[float, float]:
    offset = evaluate(
        cdp,
        "(() => {"
        " const frame = [...document.querySelectorAll('iframe')].find((el) => { const src = el.src || ''; return src.includes('weblink.') || src.includes('webmailer.'); });"
        " if (!frame) return null;"
        " const r = frame.getBoundingClientRect();"
        " if (r.width < 20 || r.height < 20) return null;"
        " return {left: r.left, top: r.top};"
        "})()",
    )
    if not isinstance(offset, dict):
        return 0.0, 0.0
    return float(offset.get("left") or 0), float(offset.get("top") or 0)


def shift_box(box: dict, dx: float, dy: float) -> dict:
    moved = dict(box)
    for key in ("x", "y", "left", "top"):
        if key in moved:
            moved[key] = float(moved[key]) + (dx if key in {"x", "left"} else dy)
    return moved


def control_box(cdp: Cdp, selector: str, text: str = "") -> dict | None:
    expression = (
        "(() => {"
        f" const selector = {json.dumps(selector)};"
        f" const text = {json.dumps(text)};"
        " const norm = (el) => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim();"
        " const point = (el) => {"
        " el.scrollIntoView({block: 'center', inline: 'center'});"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2) return null;"
        " const x = r.left + r.width / 2;"
        " const y = r.top + r.height / 2;"
        " if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return null;"
        " return {x, y, left: r.left, top: r.top, width: r.width, height: r.height};"
        " };"
        " const matches = (el) => !text || norm(el) === text || norm(el).includes(text);"
        " const visit = (root) => {"
        " const nodes = [...(root.querySelectorAll?.(selector) || [])];"
        " for (const el of nodes) {"
        " if (!matches(el)) continue;"
        " const found = point(el);"
        " if (found) return found;"
        " }"
        " for (const node of root.querySelectorAll?.('*') || []) {"
        " if (!node.shadowRoot) continue;"
        " const nested = visit(node.shadowRoot);"
        " if (nested) return nested;"
        " }"
        " return null;"
        " };"
        " const direct = visit(document);"
        " if (direct) return direct;"
        " for (const frame of document.querySelectorAll('iframe')) {"
        " let doc = null;"
        " try { doc = frame.contentDocument; } catch (error) { continue; }"
        " if (!doc) continue;"
        " const inner = visit(doc);"
        " if (!inner) continue;"
        " const outer = frame.getBoundingClientRect();"
        " return {x: outer.left + inner.x, y: outer.top + inner.y, left: outer.left + inner.left, top: outer.top + inner.top, width: inner.width, height: inner.height};"
        " }"
        " return null;"
        "})()"
    )
    found = evaluate(cdp, expression)
    if isinstance(found, dict):
        return found
    local_script = (
        "(() => {"
        f" const selector = {json.dumps(selector)};"
        f" const text = {json.dumps(text)};"
        " const norm = (el) => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim();"
        " const matches = (el) => !text || norm(el) === text || norm(el).includes(text);"
        " const point = (el) => {"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2, left: r.left, top: r.top, width: r.width, height: r.height};"
        " };"
        " const visit = (root) => {"
        " for (const el of root.querySelectorAll?.(selector) || []) {"
        " if (!matches(el)) continue;"
        " const found = point(el);"
        " if (found) return found;"
        " }"
        " for (const node of root.querySelectorAll?.('*') || []) {"
        " if (!node.shadowRoot) continue;"
        " const nested = visit(node.shadowRoot);"
        " if (nested) return nested;"
        " }"
        " return null;"
        " };"
        " return visit(document);"
        "})()"
    )
    frames = child_frames(cdp)
    root_id = str((frames[0] if frames else {}).get("id") or "")
    for frame in frames:
        frame_url = str(frame.get("url") or "")
        local = eval_in_frame(cdp, str(frame.get("id")), local_script)
        if not isinstance(local, dict):
            continue
        if str(frame.get("id") or "") == root_id:
            return local
        host = "webmailer." if "webmailer." in frame_url else "weblink." if "weblink." in frame_url else ""
        if host:
            overlay = evaluate(
                cdp,
                "(() => {"
                f" const frame = [...document.querySelectorAll('iframe')].find((el) => (el.src || '').includes({json.dumps(host)}));"
                " if (!frame) return null;"
                " const r = frame.getBoundingClientRect();"
                " if (r.width < 20 || r.height < 20) return null;"
                " return {left: r.left, top: r.top};"
                "})()",
            )
            if isinstance(overlay, dict):
                return shift_box(local, float(overlay["left"]), float(overlay["top"]))
        overlays = evaluate(
            cdp,
            "(() => [...document.querySelectorAll('iframe')].map((frame) => {"
            " const r = frame.getBoundingClientRect();"
            " return {left: r.left, top: r.top, width: r.width, height: r.height};"
            " }).filter((r) => r.width > 20 && r.height > 20))()",
        )
        if isinstance(overlays, list):
            for overlay in overlays:
                if not isinstance(overlay, dict):
                    continue
                if float(local["x"]) <= float(overlay["width"]) and float(local["y"]) <= float(overlay["height"]):
                    return {
                        "x": float(overlay["left"]) + float(local["x"]),
                        "y": float(overlay["top"]) + float(local["y"]),
                        "left": float(overlay["left"]) + float(local["left"]),
                        "top": float(overlay["top"]) + float(local["top"]),
                        "width": float(local["width"]),
                        "height": float(local["height"]),
                    }
    session_id = mail_session(cdp)
    if session_id:
        local = evaluate(cdp, local_script, session_id=session_id)
        if isinstance(local, dict):
            dx, dy = mail_offset(cdp)
            return shift_box(local, dx, dy)
    return None


def click_control(
    cdp: Cdp,
    selector: str,
    text: str = "",
    timeout: float = 8,
    required: bool = False,
    label: str = "",
) -> bool:
    deadline = time.time() + timeout
    label = label or text or selector
    while time.time() < deadline:
        try:
            box = control_box(cdp, selector, text)
        except (RuntimeError, TimeoutError):
            box = None
        if box:
            mouse_click(cdp, float(box["x"]), float(box["y"]))
            print(f"Clicked {label}")
            time.sleep(0.8)
            return True
        time.sleep(0.35)
    if required:
        if selector == "button.label":
            for frame in child_frames(cdp):
                raw = eval_in_frame(
                    cdp,
                    str(frame.get("id")),
                    "(() => [...document.querySelectorAll('button.label')].map((el) => (el.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 12).join(' | '))()",
                )
                if raw:
                    print("LABELS", str(frame.get("url") or "")[:80], str(raw).encode("ascii", "replace").decode()[:300])
        raise SystemExit(f"Could not find {label}")
    return False


def click_mailbox_prompt(cdp: Cdp) -> str:
    expression = (
        "(() => {"
        " const prompts = ["
        " ['#save-all-pur, button[aria-label=\"Akzeptieren und weiter\"]', 'Akzeptieren und weiter'],"
        " ['#cta, button[data-component-path=\"accept-button\"]', 'Zustimmen und weiter'],"
        " ['button.layer-accept-all, button[data-component-path=\"acceptall-button\"]', 'Beiden zustimmen und weiter'],"
        " ['button.lux-close-button[title=\"Schließen\"], button[data-component-path=\"icon.x\"][title=\"Schließen\"]', 'Schließen']"
        " ];"
        " const norm = (el) => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim();"
        " const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };"
        " const visit = (root) => {"
        " for (const [selector, label] of prompts) {"
        " for (const el of root.querySelectorAll?.(selector) || []) {"
        " if (!visible(el) || !norm(el).includes(label)) continue;"
        " el.click();"
        " return label;"
        " }"
        " }"
        " for (const node of root.querySelectorAll?.('*') || []) {"
        " if (!node.shadowRoot) continue;"
        " const found = visit(node.shadowRoot);"
        " if (found) return found;"
        " }"
        " return '';"
        " };"
        " return visit(document);"
        "})()"
    )
    for frame in child_frames(cdp):
        found = eval_in_frame(cdp, str(frame.get("id")), expression)
        if isinstance(found, str) and found:
            print(f"Clicked {found}")
            return found
    return ""


def dismiss_mailbox_prompts(cdp: Cdp, seconds: float = 12) -> None:
    deadline = time.time() + seconds
    idle = 0
    while time.time() < deadline:
        if click_mailbox_prompt(cdp):
            idle = 0
            time.sleep(1.0)
            continue
        idle += 1
        if idle >= 3:
            return
        time.sleep(0.4)


POP3_SWITCH_JS = (
    "(() => {"
    " const find = (root) => {"
    " const input = root.querySelector?.(\"input[data-webdriver='pop3imap-access-toggle']\");"
    " if (input) return input;"
    " for (const node of root.querySelectorAll?.('*') || []) {"
    " if (!node.shadowRoot) continue;"
    " const nested = find(node.shadowRoot);"
    " if (nested) return nested;"
    " }"
    " return null;"
    " };"
    " const input = find(document);"
    " if (!input) return null;"
    " const target = input.getBoundingClientRect().width > 8 ? input : (input.closest('label') || input.parentElement || input);"
    " target.scrollIntoView({block: 'center', inline: 'center'});"
    " const r = target.getBoundingClientRect();"
    " if (r.width < 2 || r.height < 2) return null;"
    " return {checked: !!input.checked, x: r.left + r.width / 2, y: r.top + r.height / 2};"
    "})()"
)


def pop3_switch(cdp: Cdp) -> dict | None:
    found = evaluate(cdp, POP3_SWITCH_JS)
    if isinstance(found, dict):
        return found
    for frame in child_frames(cdp):
        local = eval_in_frame(cdp, str(frame.get("id")), POP3_SWITCH_JS)
        if not isinstance(local, dict):
            continue
        overlays = evaluate(
            cdp,
            "(() => [...document.querySelectorAll('iframe')].map((frame) => {"
            " const r = frame.getBoundingClientRect();"
            " return {left: r.left, top: r.top, width: r.width, height: r.height};"
            " }).filter((r) => r.width > 20 && r.height > 20))()",
        )
        if not isinstance(overlays, list):
            return local
        for overlay in overlays:
            if not isinstance(overlay, dict):
                continue
            if float(local["x"]) <= float(overlay["width"]) and float(local["y"]) <= float(overlay["height"]):
                return {
                    "checked": local.get("checked"),
                    "x": float(overlay["left"]) + float(local["x"]),
                    "y": float(overlay["top"]) + float(local["y"]),
                }
        return local
    session_id = mail_session(cdp)
    if session_id:
        local = evaluate(cdp, POP3_SWITCH_JS, session_id=session_id)
        if isinstance(local, dict):
            dx, dy = mail_offset(cdp)
            return {"checked": local.get("checked"), "x": float(local["x"]) + dx, "y": float(local["y"]) + dy}
    return None


def captcha_surface(cdp: Cdp) -> tuple[str | None, float, float] | None:
    frame = element_box(cdp, "iframe[src*='captchafox'], #captchafox iframe, iframe[src*='captcha']")
    if frame:
        return captcha_frame_session(cdp), float(frame["left"]), float(frame["top"])
    if element_box(cdp, "#captchafox, .captchafox"):
        return None, 0.0, 0.0
    session_id = mail_session(cdp)
    if session_id:
        dx, dy = mail_offset(cdp)
        inner = element_box(cdp, "iframe[src*='captchafox'], #captchafox iframe, iframe[src*='captcha']", session_id=session_id)
        if inner:
            return captcha_frame_session(cdp), dx + float(inner["left"]), dy + float(inner["top"])
        if element_box(cdp, "#captchafox, .captchafox", session_id=session_id):
            return None, dx, dy
    return None


def challenge_layout(cdp: Cdp, session_id: str | None) -> dict:
    layout = evaluate(
        cdp,
        "(() => {"
        " const rect = (el) => {"
        " const r = el.getBoundingClientRect();"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2, left: r.left, top: r.top, width: r.width, height: r.height};"
        " };"
        " const icons = [...document.querySelectorAll('img, svg, canvas, span, div')].filter((el) => {"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 18 || r.height < 18 || r.width > 170 || r.height > 170) return false;"
        " const text = (el.innerText || '').trim();"
        " const media = ['IMG', 'SVG', 'CANVAS'].includes(el.tagName) || !!el.querySelector?.('img, svg, canvas');"
        " const emoji = text.length > 0 && text.length <= 4;"
        " return media || emoji;"
        " }).slice(0, 10).map(rect);"
        " const handle = [...document.querySelectorAll('button, [role=slider]')].find((el) => /schieben|slide/i.test((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')));"
        " const track = handle ? (handle.parentElement || handle) : null;"
        " return {icons, handle: handle ? rect(handle) : null, track: track ? rect(track) : null};"
        "})()",
        session_id=session_id,
    )
    return layout if isinstance(layout, dict) else {}


def icon_pair(icons: list) -> tuple[dict, dict] | None:
    best: tuple[float, dict, dict] | None = None
    points = [item for item in icons if isinstance(item, dict)]
    for index, left in enumerate(points):
        for right in points[index + 1 :]:
            if abs(float(left["y"]) - float(right["y"])) > 46:
                continue
            gap = abs(float(left["x"]) - float(right["x"]))
            if gap < 24:
                continue
            if best is None or gap > best[0]:
                best = (gap, left, right)
    if best is None:
        return None
    ordered = sorted((best[1], best[2]), key=lambda item: float(item["x"]))
    return ordered[0], ordered[1]


def mouse_down(cdp: Cdp, x: float, y: float) -> None:
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
    )


def mouse_move(cdp: Cdp, x: float, y: float) -> None:
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1},
    )


def mouse_up(cdp: Cdp, x: float, y: float) -> None:
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
    )


def solve_slide_captcha(cdp: Cdp) -> bool:
    surface = captcha_surface(cdp)
    if surface is None:
        return False
    session_id, origin_x, origin_y = surface
    print("Solving the slide captcha")
    layout = challenge_layout(cdp, session_id)
    handle = layout.get("handle") if isinstance(layout.get("handle"), dict) else None
    track = layout.get("track") if isinstance(layout.get("track"), dict) else None
    if not handle:
        return False
    pair = icon_pair(layout.get("icons") or [])
    target_x = float(pair[1]["x"]) if pair else None
    grab_x = origin_x + float(handle["left"]) + min(22, float(handle["width"]) / 3)
    grab_y = origin_y + float(handle["y"])
    limit = origin_x + float((track or handle)["left"]) + float((track or handle)["width"]) - 10
    mouse_down(cdp, grab_x, grab_y)
    x = grab_x
    aligned = False
    try:
        while x < limit:
            x = min(limit, x + 7)
            mouse_move(cdp, x, grab_y)
            time.sleep(0.03)
            if target_x is None:
                continue
            moved = challenge_layout(cdp, session_id)
            current = icon_pair(moved.get("icons") or [])
            if current and abs(float(current[0]["x"]) - target_x) <= 16:
                aligned = True
                break
    finally:
        mouse_up(cdp, x, grab_y)
    if aligned:
        print("Moved the slide captcha onto the target")
    return aligned or captcha_surface(cdp) is None


def describe_settings(cdp: Cdp) -> None:
    for frame in child_frames(cdp):
        url = str(frame.get("url") or "")
        if not any(part in url for part in ("webmailer.", "weblink.", "mail_settings", "permission")):
            continue
        raw = eval_in_frame(
            cdp,
            str(frame.get("id")),
            "(() => {"
            " const bits = [];"
            " const visit = (root, depth) => {"
            " if (depth > 3) return;"
            " for (const el of root.querySelectorAll?.('button, a, input, label') || []) {"
            " const text = ((el.innerText || el.getAttribute('aria-label') || el.getAttribute('data-webdriver') || el.id || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 60);"
            " if (text) bits.push(text);"
            " }"
            " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot, depth + 1);"
            " };"
            " visit(document, 0);"
            " const frames = [...document.querySelectorAll('iframe')].map((frame) => (frame.src || frame.id || 'iframe').slice(0, 100));"
            " if (!bits.length && !frames.length) return '';"
            " return bits.slice(0, 30).join(' | ') + ' || ' + frames.join(' | ');"
            "})()",
        )
        if raw:
            print("SETTINGS", url[:120])
            print(str(raw).encode("ascii", "replace").decode()[:700])


def enable_pop3(cdp: Cdp) -> None:
    if hasattr(cdp, "_mail_session"):
        delattr(cdp, "_mail_session")
    deadline = time.time() + 20
    switch = None
    while time.time() < deadline:
        switch = pop3_switch(cdp)
        if switch:
            break
        time.sleep(0.4)
    if not switch:
        describe_settings(cdp)
        raise SystemExit("Could not find the POP3/IMAP switch")
    if switch.get("checked"):
        print("POP3/IMAP access is already on")
        return
    mouse_click(cdp, float(switch["x"]), float(switch["y"]))
    print("Clicked the POP3/IMAP switch")
    deadline = time.time() + 12
    while time.time() < deadline:
        current = pop3_switch(cdp)
        if current and current.get("checked"):
            print("POP3/IMAP access is on")
            return
        if captcha_surface(cdp):
            break
        time.sleep(0.4)
    if captcha_surface(cdp):
        if not solve_slide_captcha(cdp):
            try:
                click_human_check(cdp)
            except SystemExit:
                print("The POP3/IMAP captcha is still open")
    deadline = time.time() + 15
    while time.time() < deadline:
        current = pop3_switch(cdp)
        if current and current.get("checked"):
            print("POP3/IMAP access is on")
            return
        time.sleep(0.4)
    raise SystemExit("Could not turn on POP3/IMAP access")


def mailset_frames(cdp: Cdp) -> list[dict]:
    return [frame for frame in child_frames(cdp) if "mailset-root." in str(frame.get("url") or "")]


def click_mailset_button(cdp: Cdp, label: str) -> bool:
    expression = (
        "(() => {"
        f" const label = {json.dumps(label)};"
        " let found = null;"
        " const visit = (root) => {"
        " for (const el of root.querySelectorAll?.('button.label') || []) {"
        " if ((el.innerText || '').replace(/\\s+/g, ' ').trim() === label) found = el;"
        " }"
        " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " visit(document);"
        " if (!found) return false;"
        " found.click();"
        " return true;"
        "})()"
    )
    for frame in mailset_frames(cdp):
        if eval_in_frame(cdp, str(frame.get("id")), expression) is True:
            print(f"Clicked {label}")
            return True
    return False


def mailset_origin(cdp: Cdp, frame: dict) -> tuple[float, float]:
    parent_id = str(frame.get("parentId") or "")
    if not parent_id:
        return 0.0, 0.0
    box = eval_in_frame(
        cdp,
        parent_id,
        "(() => {"
        " let found = document.querySelector('#thirdPartyFrame_mail_settings, iframe[src*=\"mailset-root.\"]');"
        " const visit = (root) => {"
        " for (const el of root.querySelectorAll?.('iframe') || []) if ((el.src || '').includes('mailset-root.')) found = el;"
        " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " if (!found) visit(document);"
        " if (!found) return null;"
        " const r = found.getBoundingClientRect();"
        " return {left: r.left, top: r.top};"
        "})()",
    )
    if not isinstance(box, dict):
        return 0.0, 0.0
    return float(box.get("left") or 0), float(box.get("top") or 0)


def mailset_pop3_state(cdp: Cdp) -> tuple[dict, dict] | None:
    for frame in mailset_frames(cdp):
        local = eval_in_frame(cdp, str(frame.get("id")), POP3_SWITCH_JS)
        if isinstance(local, dict):
            return frame, local
    return None


def mailset_slide(cdp: Cdp, frame_id: str) -> dict | None:
    layout = eval_in_frame(
        cdp,
        frame_id,
        "(() => {"
        " const rect = (el) => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, width: r.width, height: r.height, x: r.left + r.width / 2, y: r.top + r.height / 2}; };"
        " let button = null, slider = null;"
        " const icons = [];"
        " const visit = (root) => {"
        " for (const el of root.querySelectorAll?.('.cf-challenge, .cf-challenge *') || []) {"
        " const cls = String(el.className || '');"
        " const r = el.getBoundingClientRect();"
        " if (cls.includes('cf-slider__button') && r.width > 8) button = rect(el);"
        " else if (cls.split(' ').includes('cf-slider') && r.width > 40) slider = rect(el);"
        " else if (!cls.includes('cf-slider') && !cls.includes('cf-challenge__button') && r.width >= 24 && r.height >= 24 && r.width <= 150 && r.height <= 150) icons.push(rect(el));"
        " }"
        " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " visit(document);"
        " if (!button || !slider) return null;"
        " return {button, slider, icons: icons.slice(0, 8)};"
        "})()",
    )
    return layout if isinstance(layout, dict) else None


def decode_png(data: bytes) -> tuple[int, int, list[bytearray], int] | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    width = height = color = 0
    compressed = b""
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, compression, png_filter, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or compression or png_filter or interlace:
                return None
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
    if not compressed or color not in {2, 6} or width < 2 or height < 2:
        return None
    raw = zlib.decompress(compressed)
    channels = 3 if color == 2 else 4
    stride = width * channels
    if len(raw) < height * (stride + 1):
        return None

    def paeth(left: int, up: int, upper_left: int) -> int:
        estimate = left + up - upper_left
        if abs(estimate - left) <= abs(estimate - up) and abs(estimate - left) <= abs(estimate - upper_left):
            return left
        if abs(estimate - up) <= abs(estimate - upper_left):
            return up
        return upper_left

    rows: list[bytearray] = []
    index = 0
    for _y in range(height):
        filter_type = raw[index]
        index += 1
        row = bytearray(raw[index : index + stride])
        index += stride
        previous = rows[-1] if rows else bytearray(stride)
        if filter_type == 1:
            for pixel_index in range(stride):
                left = row[pixel_index - channels] if pixel_index >= channels else 0
                row[pixel_index] = (row[pixel_index] + left) & 255
        elif filter_type == 2:
            for pixel_index in range(stride):
                row[pixel_index] = (row[pixel_index] + previous[pixel_index]) & 255
        elif filter_type == 3:
            for pixel_index in range(stride):
                left = row[pixel_index - channels] if pixel_index >= channels else 0
                row[pixel_index] = (row[pixel_index] + ((left + previous[pixel_index]) // 2)) & 255
        elif filter_type == 4:
            for pixel_index in range(stride):
                left = row[pixel_index - channels] if pixel_index >= channels else 0
                upper_left = previous[pixel_index - channels] if pixel_index >= channels else 0
                row[pixel_index] = (row[pixel_index] + paeth(left, previous[pixel_index], upper_left)) & 255
        rows.append(row)
    return width, height, rows, channels


def slide_gap_css(data: bytes, css_width: float, ignore_left: float = 40) -> float | None:
    decoded = decode_png(data)
    if decoded is None or css_width < 80:
        return None
    width, height, rows, channels = decoded
    piece = max(12, int(round(50 * width / css_width)))
    start = max(2, int(round(ignore_left * width / css_width)))
    stop = width - piece - 2
    if stop <= start:
        return None
    y0 = max(0, height // 8)
    y1 = height - y0
    if y1 <= y0:
        y0, y1 = 0, height
    energy = [0.0] * width
    shade = [0.0] * width
    for y in range(y0, y1):
        row = rows[y]
        for x in range(width):
            offset = x * channels
            shade[x] += row[offset] * 0.299 + row[offset + 1] * 0.587 + row[offset + 2] * 0.114
        for x in range(1, width - 1):
            left = (x - 1) * channels
            right = (x + 1) * channels
            energy[x] += abs(int(row[right]) - int(row[left])) + abs(int(row[right + 1]) - int(row[left + 1])) + abs(int(row[right + 2]) - int(row[left + 2]))
    samples = max(1, y1 - y0)
    shade = [value / samples for value in shade]
    smooth = energy[:]
    for x in range(2, width - 2):
        smooth[x] = sum(energy[x - 2 : x + 3]) / 5
    def peak(x: int) -> float:
        return smooth[x] - (smooth[max(0, x - 8)] + smooth[min(width - 1, x + 8)]) / 2

    best: tuple[float, int] | None = None
    for left in range(start, stop):
        right = min(width - 2, left + piece)
        score = max(0.0, peak(left)) + max(0.0, peak(right))
        interior = sum(shade[left + 3 : right - 3]) / max(1, right - left - 6)
        before = sum(shade[max(0, left - 14) : max(0, left - 2)]) / max(1, max(0, left - 2) - max(0, left - 14))
        after = sum(shade[right + 2 : right + 14]) / max(1, min(width, right + 14) - (right + 2))
        if interior + 6 < before and interior + 6 < after:
            score += samples * 12
        if best is None or score > best[0]:
            best = (score, left)
    if best is None:
        return None
    css_x = best[1] * css_width / width
    if css_x < ignore_left or css_x > css_width - 52:
        return None
    return css_x


def slide_background(cdp: Cdp, frame_id: str, origin_x: float, origin_y: float) -> tuple[bytes, float, float] | None:
    found = eval_in_frame(
        cdp,
        frame_id,
        "(() => {"
        " let bg = null;"
        " const visit = (root) => {"
        " for (const el of root.querySelectorAll?.('canvas') || []) {"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 200 || r.height < 45 || r.height > 100) continue;"
        " if (getComputedStyle(el).position === 'absolute') bg = el;"
        " }"
        " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " visit(document);"
        " if (!bg) return null;"
        " const r = bg.getBoundingClientRect();"
        " let png = '';"
        " try { png = bg.toDataURL('image/png'); } catch (error) { png = ''; }"
        " return {png, left: r.left, top: r.top, width: r.width, height: r.height};"
        "})()",
    )
    if not isinstance(found, dict):
        return None
    css_width = float(found.get("width") or 0)
    if css_width < 80:
        return None
    encoded = str(found.get("png") or "")
    if encoded.startswith("data:image/png"):
        payload = encoded.split(",", 1)[1] if "," in encoded else ""
        try:
            return base64.b64decode(payload), css_width, 40.0
        except (ValueError, IndexError):
            pass
    try:
        shot = cdp.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": {
                    "x": origin_x + float(found["left"]),
                    "y": origin_y + float(found["top"]),
                    "width": css_width,
                    "height": float(found["height"]),
                    "scale": 1,
                },
            },
        )
    except (RuntimeError, TimeoutError, KeyError):
        return None
    data = base64.b64decode(shot.get("data") or "")
    if not data:
        return None
    return data, css_width, 58.0


def drag_pointer(cdp: Cdp, x0: float, y0: float, x1: float, y1: float) -> None:
    mouse_down(cdp, x0, y0)
    steps = 28
    try:
        for step in range(1, steps + 1):
            t = step / steps
            ease = t * t * (3 - 2 * t)
            mouse_move(cdp, x0 + (x1 - x0) * ease, y0 + math.sin(t * math.pi) * 1.2)
            time.sleep(0.016 + random.random() * 0.012)
        time.sleep(0.12)
    finally:
        mouse_up(cdp, x1, y0)


def drag_mailset_slide(cdp: Cdp, frame_id: str, origin_x: float, origin_y: float) -> None:
    picture = None
    for _ in range(4):
        picture = slide_background(cdp, frame_id, origin_x, origin_y)
        if picture is not None:
            break
        time.sleep(0.35)
    if picture is None:
        print("Could not see the slide picture")
        return
    data, css_width, ignore_left = picture
    gap = slide_gap_css(data, css_width, ignore_left)
    if gap is None:
        print("Could not see the slide gap")
        return
    gap = max(12.0, min(248.0, gap))
    print("Solving the slide captcha")
    layout = mailset_slide(cdp, frame_id)
    if layout is None:
        print("Could not move the slide handle")
        return
    button = layout["button"]
    grab_x = origin_x + float(button["x"])
    grab_y = origin_y + float(button["y"])
    drag_pointer(cdp, grab_x, grab_y, grab_x + gap, grab_y)
    print(f"Slid the captcha {gap:.0f}px")


def pop3_is_on(cdp: Cdp) -> bool:
    current = mailset_pop3_state(cdp)
    if current and current[1].get("checked"):
        print("POP3/IMAP access is on")
        return True
    return False


def turn_mailset_pop3_on(cdp: Cdp) -> bool:
    found = None
    deadline = time.time() + 15
    while time.time() < deadline and found is None:
        found = mailset_pop3_state(cdp)
        if found is None:
            time.sleep(0.4)
    if found is None:
        return False
    if found[1].get("checked"):
        print("POP3/IMAP access is on")
        return True
    for attempt in range(3):
        found = mailset_pop3_state(cdp)
        if found is None:
            return False
        frame, switch = found
        if switch.get("checked"):
            print("POP3/IMAP access is on")
            return True
        origin_x, origin_y = mailset_origin(cdp, frame)
        frame_id = str(frame.get("id"))
        if mailset_slide(cdp, frame_id) is None:
            mouse_click(cdp, origin_x + float(switch["x"]), origin_y + float(switch["y"]))
            print("Clicked the POP3/IMAP switch")
            deadline = time.time() + 8
            while time.time() < deadline:
                if pop3_is_on(cdp):
                    return True
                if mailset_slide(cdp, frame_id):
                    break
                time.sleep(0.4)
        if mailset_slide(cdp, frame_id):
            drag_mailset_slide(cdp, frame_id, origin_x, origin_y)
        deadline = time.time() + 8
        while time.time() < deadline:
            if pop3_is_on(cdp):
                return True
            time.sleep(0.4)
        if attempt < 2:
            print("POP3/IMAP slide was not accepted")
            time.sleep(0.8)
    return False


def click_settings_button(cdp: Cdp) -> bool:
    expression = (
        "(() => {"
        " const visit = (root) => {"
        " const el = root.querySelector?.('#settingsButton');"
        " if (el) return el;"
        " for (const node of root.querySelectorAll?.('*') || []) {"
        " if (!node.shadowRoot) continue;"
        " const found = visit(node.shadowRoot);"
        " if (found) return found;"
        " }"
        " return null;"
        " };"
        " const el = visit(document);"
        " if (!el) return false;"
        " el.click();"
        " return true;"
        "})()"
    )
    for frame in child_frames(cdp):
        url = str(frame.get("url") or "")
        if "webmailer." not in url and "weblink." not in url:
            continue
        if eval_in_frame(cdp, str(frame.get("id")), expression) is True:
            print(f"Settings button in {url[:100]}")
            return True
    session_id = mail_session(cdp)
    if session_id and evaluate(cdp, expression, session_id=session_id) is True:
        print("Settings button in the mail session")
        return True
    return False


def enable_mailbox_access(cdp: Cdp) -> None:
    dismiss_mailbox_prompts(cdp)
    deadline = time.time() + 45
    clicked_at = 0.0
    while time.time() < deadline and not mailset_frames(cdp):
        if click_mailbox_prompt(cdp):
            time.sleep(1.0)
            continue
        if time.time() - clicked_at >= 6:
            if click_settings_button(cdp):
                print("Clicked mailbox settings")
                clicked_at = time.time()
            elif click_control(cdp, "#settingsButton", timeout=2, label="mailbox settings"):
                clicked_at = time.time()
        time.sleep(0.5)
    if not mailset_frames(cdp):
        print(f"Settings page did not open: {page_url(cdp)[:160]}")
        for frame in child_frames(cdp):
            print(f"FRAME {str(frame.get('url') or '')[:140]}")
        raise SystemExit("Could not find mailbox settings")
    dismiss_mailbox_prompts(cdp, seconds=6)
    deadline = time.time() + 25
    opened = False
    while time.time() < deadline:
        if click_mailbox_prompt(cdp):
            time.sleep(0.8)
            continue
        if click_mailset_button(cdp, "E-Mail empfangen"):
            opened = True
            break
        time.sleep(0.5)
    if not opened:
        raise SystemExit("Could not find E-Mail empfangen")
    time.sleep(1)
    dismiss_mailbox_prompts(cdp, seconds=4)
    if not turn_mailset_pop3_on(cdp):
        if mailset_pop3_state(cdp):
            raise SystemExit("Could not turn on POP3/IMAP access")
        enable_pop3(cdp)


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
    phone_step=None,
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
    pause = random.uniform(4, 6)
    print(f"Waiting {pause:.1f}s before Get started")
    time.sleep(pause)
    deadline = time.time() + 25
    clicked = False
    retried_human = False
    reported_captcha = False
    while time.time() < deadline:
        status = captcha_status(cdp)
        confirmed = status.get("checked") == "true" or bool(status.get("token"))
        if confirmed and not reported_captcha:
            print("I am human check completed")
            reported_captcha = True
        if registration_failed(page_url(cdp)):
            print(f"Registration was sent to {page_url(cdp)}")
            raise SystemExit("Registration was sent to the help page")
        if click_selector(cdp, "button[data-testid='next-button'], [data-testid='next-button']"):
            clicked = True
            break
        checkbox = "#captchafox .cf-checkbox, #captchafox [role='checkbox'], #captchafox iframe, iframe[src*='captchafox']"
        if not confirmed and not retried_human and element_box(cdp, checkbox):
            print("I am human was clicked. CaptchaFox has not confirmed it yet.")
            retried_human = True
            try:
                click_human_check(cdp)
            except SystemExit:
                print("I am human checkbox was gone")
            extra = random.uniform(4, 6)
            print(f"Waiting {extra:.1f}s before Get started")
            time.sleep(extra)
            continue
        time.sleep(0.5)
    if not clicked:
        print(f"Get started button missing at {page_url(cdp)}")
        print(signup_buttons(cdp).encode("ascii", errors="replace").decode("ascii"))
        raise SystemExit("Could not find the Get started button")
    print("Clicked Get started")
    mailbox = choose_email(cdp, email)
    pause = random.uniform(3, 4)
    print(f"Waiting {pause:.1f}s before the address form")
    time.sleep(pause)
    fill_address(cdp, postal_code, city, street, country)
    fill_password(cdp, password)
    try:
        if phone_step is None:
            verify_phone(cdp, port)
        else:
            phone_step(cdp)
    except SystemExit as exc:
        message = str(exc)
        lowered = message.lower()
        if any(part in lowered for part in ("pop3", "imap", "mailbox settings", "e-mail empfangen")):
            known = mailbox_address(mailbox)
            if not known:
                try:
                    known = visible_mailbox(cdp, mailbox)
                except (RuntimeError, TimeoutError, OSError):
                    known = ""
            if known:
                save_account(known, password)
            raise MailboxSettingsError(known, message) from exc
        raise
    deadline = time.time() + 10
    final = ""
    while time.time() < deadline:
        final = visible_mailbox(cdp, mailbox)
        if final:
            break
        time.sleep(0.5)
    if not final:
        raise SystemExit("Could not read the created mailbox")
    print(f"Created mailbox {final}")
    return final


def verify_supplied_phones(gmx: Cdp, phones: list[str], fetch_code, log=print, stop=None) -> str:
    pending = [str(phone).strip() for phone in phones if str(phone).strip()][:MAX_SUPPLIED_PHONES]
    if not pending:
        raise NoPhonesLeft("No phone numbers were returned")
    for index, source in enumerate(pending):
        if stop:
            stop()
        number = source if source.startswith("+") else f"+{re.sub(r'[^0-9]', '', source)}"
        log(f"Trying {number} ({index + 1} of {len(pending)})")
        prefix, national = split_phone(number)
        reached_code = False
        for variant_index, variant in enumerate(phone_variants(national)):
            if stop:
                stop()
            try:
                prepare_phone_entry(gmx, index > 0 or variant_index > 0, stop)
            except SystemExit as exc:
                if index == 0 and variant_index == 0:
                    raise
                print(str(exc))
                try:
                    return_to_phone_form(gmx)
                    prepare_phone_entry(gmx, False, stop)
                except SystemExit as back_exc:
                    print(str(back_exc))
                    raise SignupRestart("The phone form closed before the numbers finished") from back_exc
            if stop:
                stop()
            selected = select_option(gmx, "#mobilePhonePrefix", prefix)
            if selected != prefix:
                raise SystemExit(f"Could not select country code {prefix}")
            type_into(gmx, "#mobilePhone", variant)
            print(f"Filled {prefix} {variant}")
            print(phone_entry_details(gmx))
            submit_phone_form(gmx)
            if phone_submission_state(gmx) == "code":
                reached_code = True
                break
            note = page_url(gmx)
            detail = form_message(gmx)
            print(f"{number} was rejected as {variant} ({note}) {detail}".rstrip())
            if registration_failed(note):
                break
            lowered = detail.lower()
            if lowered and not any(word in lowered for word in ("ung", "invalid", "telefonnummer")):
                break
        if not reached_code:
            log(f"{number} was rejected")
            continue
        log(f"Waiting for SMS on {number}")
        code = str(keep_browser_open(gmx, lambda: fetch_code(number)) or "").strip()
        if not code:
            print(f"No SMS code for {number}")
            log(f"No SMS code for {number}")
            continue
        result = submit_confirmation_code(gmx, code)
        if result == "accepted":
            log(f"Verified {number}")
            return number
        if result == "closed":
            print("Confirmation step closed")
            return number
        print(f"Confirmation failed for {number}: {result}")
        log(f"{number} did not verify")
    raise SignupRestart("Three phone numbers failed to verify")


def generate_mailbox(
    profile: str,
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
    url: str,
    phones: list[str],
    fetch_code,
    log=print,
    stop=None,
    proxies: Path | None = None,
    proxy_type: str = "http",
    headless: bool = False,
    skip_check: bool = True,
) -> tuple[str, str]:
    profile = normalize_profile_name(profile)
    first_name = normalize_name(first_name, "First name")
    last_name = normalize_name(last_name, "Last name")
    day, month, year = normalize_birth(day, month, year)
    postal_code = postal_code.strip()
    city = normalize_name(city, "Ort")
    street = " ".join(street.split())
    country = country.strip().upper() or "DE"
    if not url:
        raise SystemExit(f"Set TARGET_URL1 in {ENV_FILE}")
    if not street or len(street) > 49:
        raise SystemExit("Straße & Hausnummer must be 1 to 49 characters")
    if not postal_code or len(postal_code) > 10:
        raise SystemExit("PLZ must be 1 to 10 characters")
    if not password or len(password) > 100:
        raise SystemExit("Password must be 1 to 100 characters")
    used = {"phone": ""}

    def phone_step(cdp: Cdp) -> None:
        if stop:
            stop()
        used["phone"] = verify_supplied_phones(cdp, phones, fetch_code, log=log, stop=stop)

    proxy = load_random_proxy(proxies or (ROOT / "proxies.txt"))
    session = requests.Session()
    local_login(session)
    resolved = resolve_proxy_type(session, proxy, proxy_type, skip_check)
    print(f"Using {proxy_label(proxy, resolved)}")
    api = DolphinAPI(collect_garbage_on_init=False)
    profile_id = create_profile(api, profile, proxy, resolved)
    ws: CdpSocket | None = None
    cdp: Cdp | None = None
    try:
        automation = start_profile(session, profile_id, headless)
        port = int(automation["port"])
        print(f"Started {profile} on port {port}")
        if stop:
            stop()
        log("Opening the signup form")
        ws, target_id = connect_page(port)
        cdp = Cdp(ws)
        if target_id:
            try:
                cdp.call("Target.activateTarget", {"targetId": target_id})
            except (RuntimeError, TimeoutError) as exc:
                print(f"Could not focus the tab: {exc}")
        if not headless:
            maximize_window(cdp, target_id)
            show_profile_window(profile_id)
        navigate(cdp, url)
        open_registration(cdp)
        mailbox = fill_signup(
            cdp,
            profile,
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
            phone_step=phone_step,
        )
    except BaseException:
        finish_profile(session, api, profile_id, cdp, ws)
        raise
    save_account(mailbox, password)
    finish_profile(session, api, profile_id, cdp, ws)
    return mailbox, used["phone"]


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
        description="Create a Dolphin profile, open TARGET_URL1, and submit the signup form."
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
    parser.add_argument("--url", default=TARGET_URL1, help="Page to open (default: TARGET_URL1 from .env)")
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
        raise SystemExit(f"Set TARGET_URL1 in {ENV_FILE}")

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
            show_profile_window(profile_id)
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

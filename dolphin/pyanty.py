import json
import requests
import os
import sys
import io
import time
import zipfile
import shutil
import platform
import threading

from .pyanty import *
from .api import DolphinAPI, STABLE_CHROME_VERSION
from .apikey import api_key

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
#from playwright_stealth import stealth_async

try:
    from pyppeteer import connect
except:
    pass

try:
    from playwright.async_api import async_playwright
except:
    pass

from .utils import collect_garbage

_RETRYABLE_STATUS = {429, 502, 503, 504}
_LOCAL_MAX_RETRIES = 5
# Dolphin often binds IPv4 only; Windows `localhost` can resolve to ::1 and miss the API.
_LOCAL_API_BASE = os.environ.get("DOLPHIN_LOCAL_API", "http://127.0.0.1:3001").rstrip("/")
_AUTH_TTL_S = 50 * 60
_ALREADY_RUNNING_MARKERS = (
    "already started",
    "already running",
    "profile is running",
    "browser is already",
    "has been already started",
)
_UNAUTHORIZED_MARKERS = ("unauthorized", "unauthorised", "401")

_local_session = requests.Session()
_local_lock = threading.Lock()
_local_authed_at = 0.0

ERROR_KIND_UNREACHABLE = "local_api_unreachable"
ERROR_KIND_UNAUTHORIZED = "local_api_unauthorized"
ERROR_KIND_ALREADY_RUNNING = "already_running"


def _local_url(path: str) -> str:
    return f"{_LOCAL_API_BASE}{path}"


def _payload_error_text(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload or "")
    err = (
        payload.get("error")
        or payload.get("message")
        or payload.get("errorText")
        or ""
    )
    if isinstance(err, dict):
        err = (
            err.get("text")
            or err.get("error")
            or err.get("message")
            or str(err)
        )
    return str(err)


def extract_automation(payload):
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("automation")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("automation"))
        candidates.append(data)
    for auto in candidates:
        if isinstance(auto, dict) and auto.get("port") is not None:
            return auto
    return None


def classify_run_profile_error(payload) -> str | None:
    if extract_automation(payload):
        return None
    text = _payload_error_text(payload).lower()
    status = 0
    if isinstance(payload, dict):
        try:
            status = int(payload.get("_http_status") or 0)
        except (TypeError, ValueError):
            status = 0
        kind = payload.get("error_kind")
        if kind:
            return str(kind)
    if status == 401 or any(m in text for m in _UNAUTHORIZED_MARKERS):
        return ERROR_KIND_UNAUTHORIZED
    if any(m in text for m in _ALREADY_RUNNING_MARKERS):
        return ERROR_KIND_ALREADY_RUNNING
    conn_markers = (
        "connection refused",
        "failed to establish",
        "max retries exceeded",
        "newconnectionerror",
        "10061",
        "10060",
        "timed out",
        ERROR_KIND_UNREACHABLE,
        "local_api_unreachable",
    )
    if any(m in text for m in conn_markers):
        return ERROR_KIND_UNREACHABLE
    return "start_failed"


def _response_payload(response: requests.Response) -> dict:
    try:
        data = response.json()
        if not isinstance(data, dict):
            data = {"error": data}
    except (json.JSONDecodeError, ValueError):
        data = {"error": (response.text or "")[:300] or f"HTTP {response.status_code}"}
    data["_http_status"] = response.status_code
    return data


def ensure_local_api_auth(*, force: bool = False):
    """Authorize the Dolphin local API. GUI login is not enough for /start?automation=1."""
    global _local_authed_at
    now = time.time()
    with _local_lock:
        if not force and _local_authed_at and (now - _local_authed_at) < _AUTH_TTL_S:
            return True, "ok"
        try:
            response = _local_session.post(
                _local_url("/v1.0/auth/login-with-token"),
                json={"token": api_key},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            _local_authed_at = 0.0
            return False, f"{ERROR_KIND_UNREACHABLE}: {exc}"
        if response.status_code == 200:
            _local_authed_at = time.time()
            return True, "ok"
        _local_authed_at = 0.0
        body = (response.text or "")[:300]
        return False, f"{ERROR_KIND_UNAUTHORIZED}: HTTP {response.status_code} {body}"


def probe_local_api():
    ok, detail = ensure_local_api_auth()
    if ok:
        return True, "ok"
    kind = (
        ERROR_KIND_UNAUTHORIZED
        if ERROR_KIND_UNAUTHORIZED in detail
        else ERROR_KIND_UNREACHABLE
    )
    return False, {"error": detail, "error_kind": kind}


def _local_request(method: str, path: str, *, timeout: int = 120, **kwargs):
    ok, detail = ensure_local_api_auth()
    if not ok:
        return None, {"error": detail, "error_kind": classify_run_profile_error({"error": detail})}
    try:
        response = _local_session.request(
            method, _local_url(path), timeout=timeout, **kwargs
        )
    except requests.exceptions.RequestException as exc:
        return None, {"error": str(exc), "error_kind": ERROR_KIND_UNREACHABLE}

    if response.status_code == 401:
        ok, detail = ensure_local_api_auth(force=True)
        if not ok:
            return None, {"error": detail, "error_kind": ERROR_KIND_UNAUTHORIZED}
        try:
            response = _local_session.request(
                method, _local_url(path), timeout=timeout, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            return None, {"error": str(exc), "error_kind": ERROR_KIND_UNREACHABLE}

    payload = _response_payload(response)
    if response.status_code == 401:
        payload["error_kind"] = ERROR_KIND_UNAUTHORIZED
    return response, payload


def run_profile(profile_id, headless=False):
    # Official local API: ?automation=1&headless=1
    # (headless=true is ignored by current Dolphin builds and still opens a window.)
    qs = "automation=1"
    if headless:
        qs += "&headless=1"
    path = f"/v1.0/browser_profiles/{profile_id}/start?{qs}"
    last_payload = None
    already_restarted = False
    for attempt in range(1, _LOCAL_MAX_RETRIES + 1):
        response, payload = _local_request("GET", path, timeout=120)
        last_payload = payload
        if extract_automation(payload):
            return payload

        kind = classify_run_profile_error(payload)
        if kind in {ERROR_KIND_UNREACHABLE, ERROR_KIND_UNAUTHORIZED}:
            payload["error_kind"] = kind
            return payload

        if kind == ERROR_KIND_ALREADY_RUNNING and not already_restarted:
            already_restarted = True
            try:
                close_profile(profile_id)
            except Exception:
                pass
            time.sleep(2)
            continue

        if response is not None and response.status_code in _RETRYABLE_STATUS:
            if attempt < _LOCAL_MAX_RETRIES:
                time.sleep(2 + attempt)
                continue
            payload["error_kind"] = "start_failed"
            return payload

        if attempt < _LOCAL_MAX_RETRIES and kind == "start_failed":
            time.sleep(2 + attempt)
            continue
        payload["error_kind"] = kind or "start_failed"
        return payload

    if isinstance(last_payload, dict):
        last_payload.setdefault("error_kind", "start_failed")
        return last_payload
    return {"error": "run_profile failed", "error_kind": "start_failed"}


def close_profile(profile_id):
    _response, payload = _local_request(
        "GET", f"/v1.0/browser_profiles/{profile_id}/stop", timeout=60
    )
    return payload


def import_cookies(profileid, cookies):
    print('import cookies')
    payload = {
        "cookies": cookies,
        "profileId": profileid,
        "transfer": 0,
        "cloudSyncDisabled": False
    }
    _response, body = _local_request(
        "POST",
        "/v1.0/cookies/import",
        timeout=60,
        json=payload,
    )
    print(body)
    return None



def download_driver_to_memory(driver_url):
    response = requests.get(driver_url, stream=True)
    
    if response.status_code != 200 or 'zip' not in response.headers.get('Content-Type', ''):
        raise ValueError("The requested driver is not available.")

    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024
    progress_bar_size = 30

    driver_content = io.BytesIO()
    downloaded_size = 0
    for data in response.iter_content(block_size):
        driver_content.write(data)
        downloaded_size += len(data)
        progress_fraction = downloaded_size / total_size
        progress_hash_count = int(progress_fraction * progress_bar_size)
        progress_bar = (
            f"Downloading the driver... [{progress_hash_count * '#'}{(progress_bar_size - progress_hash_count) * '-'}] "
            f"{downloaded_size / (1024 * 1024):.2f}/{total_size / (1024 * 1024):.2f}MB"
        )
        print(f"\r{progress_bar}", end="")
    
    driver_content.seek(0)
    print("\nDownload completed.")
    return driver_content

def get_dolphin_driver():
    docs_url = 'https://intercom.help/dolphinteam/en/articles/7127390-basic-automation-dolphin-anty'
    html = requests.get(docs_url).text
    driver_slices = html.split('/chromedriver')
    server_url = driver_slices[0].split('"')[-1]
    version = driver_slices[1].split('"')[0]
    archive_name = 'chromedriver' + version
    driver_url = server_url + '/' + archive_name
    return download_driver_to_memory(driver_url)


def unzip_driver_from_memory(driver_content):
    with zipfile.ZipFile(driver_content, 'r') as zip_ref:
        zip_ref.extractall(path=os.getcwd())

def clean_trash(zip_files):
    for zip_file in zip_files:
        os.remove(zip_file)

    if os.path.exists('__MACOSX') and sys.platform != 'darwin':
        shutil.rmtree(os.path.abspath('__MACOSX'))

def unpack_subarchives(system, architecture):
    zip_files = [f for f in os.listdir() if f.lower().endswith('.zip')]
    file_architecture = '64' if '64' in architecture else '86'

    for zip_file in zip_files:
        if system == 'Windows' and 'win' in zip_file.lower() and file_architecture in zip_file:
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                zip_ref.extractall(path=os.getcwd())
            break
        elif system == 'Linux' and 'linux' in zip_file.lower():
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                zip_ref.extractall(path=os.getcwd())
            break
        elif system == 'Darwin' and 'mac' in zip_file.lower():
            if 'arm' in architecture and 'arm' in zip_file:
                with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                    zip_ref.extractall(path=os.getcwd())
                break
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                zip_ref.extractall(path=os.getcwd())
            break
    clean_trash(zip_files)


def select_driver_executable(system, architecture):
    unpack_subarchives(system, architecture)
    if system == 'Windows':
        executable_name = 'chromedriver.exe' if '64' in architecture else 'chromedriver_x86.exe'
    elif system == 'Darwin' or (system == 'Linux' and '64' in architecture):
        executable_name = 'chromedriver'
    else:
        raise ValueError("Unsupported operating system or architecture")
    
    if system != 'Windows':
        os.chmod(executable_name, 0o755)
    
    return executable_name


def get_driver(port=9222):
    system = platform.system()
    architecture = platform.machine()
    driver_path = select_driver_executable(system, architecture)

    if not os.path.exists(driver_path):
        driver_content = get_dolphin_driver()
        unzip_driver_from_memory(driver_content)
        driver_path = select_driver_executable(system, architecture)
   
    options = Options()
    options.add_experimental_option('debuggerAddress', f'127.0.0.1:{port}')
    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    return driver


async def get_browser(ws_endpoint, port, core='pyppeteer'):
    if core == 'pyppeteer':
        browser = await connect(browserWSEndpoint=f'ws://127.0.0.1:{port}{ws_endpoint}')
        pages = await browser.pages()
        page = pages[0]

        await page.bringToFront()
        return browser

    elif core == 'playwright':
        p = await async_playwright().start()
        browser = await p.chromium.connect_over_cdp(f'ws://127.0.0.1:{port}{ws_endpoint}')
        context = browser.contexts[0]
        page = context.pages[0]
        #await stealth_async(page)
        await page.bring_to_front()
        return browser
    else:
        raise ValueError(f'{core} is unsupported!')


if sys.platform == 'win32':
    def authorize_in_window(login, password):
        from pywinauto.application import Application

        app = Application(backend='uia').connect(title_re='Dolphin{anty}')
        main_window = app.window(title_re='Dolphin{anty}')

        if main_window.exists():
            main_window.set_focus()
            descendants = main_window.descendants(control_type='Edit')
            email_field = None
            password_field = None

            for desc in descendants:
                if 'Email' in desc.window_text() or 'email' in desc.legacy_properties().get('Value', '').lower():
                    email_field = desc
                elif 'Password' in desc.window_text() or 'password' in desc.legacy_properties().get('Value', '').lower():
                    password_field = desc
            
            if email_field and password_field:
                email_field.click_input()
                email_field.type_keys(login)
                
                password_field.click_input()
                password_field.type_keys(password)

                sign_in_button = main_window.descendants(title='SIGN IN', control_type='Button')[0]
                sign_in_button.click()
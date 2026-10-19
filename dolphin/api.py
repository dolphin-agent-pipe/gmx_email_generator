import os
import re
import sys
import json
import time
import chardet
import requests
from .apikey import api_key  

from .utils import collect_garbage

data = requests.get('https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json').json()
STABLE_CHROME_VERSION = int(data['channels']['Stable']['version'].split('.')[0])
del data

_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_CACHE_DIR, 'anty_release_cache.json')
CACHE_EXPIRY = 3600
_RETRYABLE_STATUS = {429, 502, 503, 504}
_API_MAX_RETRIES = 5
_memory_cached_release = None
_memory_cached_at = 0.0
_github_rate_limit_logged = False

def get_cached_release(*, allow_stale=False):
    global _memory_cached_release, _memory_cached_at
    if _memory_cached_release and (
        allow_stale or time.time() - _memory_cached_at < CACHE_EXPIRY
    ):
        return _memory_cached_release
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as cache_file:
            try:
                cache_data = json.load(cache_file)
                latest_release = cache_data.get('latest_release')
                timestamp = cache_data.get('timestamp', 0)
                if latest_release and (
                    allow_stale or time.time() - timestamp < CACHE_EXPIRY
                ):
                    _memory_cached_release = latest_release
                    _memory_cached_at = timestamp
                    return latest_release
            except (json.JSONDecodeError, OSError, TypeError):
                pass
    return None

def save_cached_release(latest_release):
    global _memory_cached_release, _memory_cached_at
    _memory_cached_release = latest_release
    _memory_cached_at = time.time()
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as cache_file:
        json.dump({'latest_release': latest_release, 'timestamp': _memory_cached_at}, cache_file)

def fetch_latest_release():
    global _github_rate_limit_logged
    stale = get_cached_release(allow_stale=True)
    try:
        response = requests.get(
            'https://api.github.com/repos/dolphinrucom/anty-releases/releases/latest',
            timeout=15,
        )
        response.raise_for_status()
        latest_release = response.json().get('name')
        if latest_release:
            save_cached_release(latest_release)
            return latest_release
        raise ValueError("Release name not found in the API response.")
    except requests.exceptions.RequestException as e:
        if stale:
            return stale
        if not _github_rate_limit_logged:
            print(f"Network error occurred: {e}")
            _github_rate_limit_logged = True
    except ValueError as ve:
        if stale:
            return stale
        print(f"Error in API response: {ve}")
    except Exception as ex:
        if stale:
            return stale
        print(f"An unexpected error occurred: {ex}")
    return stale or "unknown_version"

class DolphinAPI:
    def __init__(self, *, collect_garbage_on_init=True):
        self.s = requests.Session()
        self.s.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'ru',
            'User-Agent': f'dolphin_anty_{self._get_latest_release()}_Windows_NT',
            'Referer': 'https://app.dolphin-anty-ru.online/',
            'Authorization': f'Bearer {api_key}'
        })
        if collect_garbage_on_init:
            self._collect_garbage()

    def _error_text(self, response):
        text = (response.text or "").strip()
        if text.startswith("<"):
            title = re.search(r"<title>([^<]+)</title>", text, re.I)
            if title:
                return f"HTTP {response.status_code} {title.group(1).strip()}"
            return f"HTTP {response.status_code} (HTML error page)"
        return text or f"HTTP {response.status_code}"

    def _request_json(self, method, url, **kwargs):
        timeout = kwargs.pop("timeout", 60)
        last_exc = None
        for attempt in range(1, _API_MAX_RETRIES + 1):
            try:
                response = self.s.request(method, url, timeout=timeout, **kwargs)
                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = RuntimeError(self._error_text(response))
                    if attempt < _API_MAX_RETRIES:
                        time.sleep(2 + attempt)
                        continue
                    raise last_exc
                try:
                    return response.json()
                except (json.JSONDecodeError, ValueError):
                    raise RuntimeError(self._error_text(response))
            except requests.exceptions.RequestException as exc:
                last_exc = RuntimeError(str(exc))
                if attempt < _API_MAX_RETRIES:
                    time.sleep(2 + attempt)
                    continue
                raise last_exc from exc
        if last_exc:
            raise last_exc
        raise RuntimeError("Dolphin API request failed")

    def get_profile(self, id):
        return self._request_json(
            "get", f"https://dolphin-anty-api.com/browser_profiles/{id}"
        )

    def get_profiles(self, page=1, limit=50, query=None):
        url = (
            f"https://dolphin-anty-api.com/browser_profiles?page={page}&limit={limit}"
        )
        if query:
            url = f"{url}&query={requests.utils.quote(str(query))}"
        return self._request_json("get", url)

    def get_webgl(self, plattform="windows"):
        return self._request_json(
            "get",
            f"https://anty-api.com/fingerprints/webgl?browser_type=anty&platform={plattform}",
        )

    def get_extensions(self, page=1, limit=50):
        return self._request_json(
            "get",
            f"https://api.dolphin-anty-ru.online/extensions?page={page}&limit={limit}",
        )

    def load_extension_from_url(self, url):
        return self._request_json(
            "post",
            "https://api.dolphin-anty-ru.online/extensions",
            json={
                "url": url,
                "sharedToEntireTeam": False,
                "mainWebsite": ["all"],
            },
        )

    def load_extension_from_zip(self, extension_name, path):
        with open(path, "rb") as file:
            files = {"file": ("a.zip", file, "application/x-zip-compressed")}
            data = {
                "extensionName": extension_name,
                "sharedToEntireTeam": "0",
                "mainWebsite[]": "all",
            }
            return self._request_json(
                "post",
                "https://api.dolphin-anty-ru.online/extensions/upload-zipped",
                files=files,
                data=data,
            )

    def delete_extensions(self, ids):
        return self._request_json(
            "delete",
            "https://api.dolphin-anty-ru.online/extensions",
            json={"ids": ids},
        )

    def create_profile(self, data):
        return self._request_json(
            "post", "https://dolphin-anty-api.com/browser_profiles", json=data
        )

    def edit_profile(self, id, data):
        return self._request_json(
            "patch", f"https://dolphin-anty-api.com/browser_profiles/{id}", json=data
        )

    def delete_profile(self, id):
        return self._request_json(
            "delete",
            f"https://dolphin-anty-api.com/browser_profiles/{id}?forceDelete=1",
        )

    def delete_profiles(self, ids):
        return self._request_json(
            "delete",
            "https://dolphin-anty-api.com/browser_profiles?forceDelete=1",
            json={"ids": ids},
        )

    def generate_fingerprint(
        self,
        platform="windows",
        browser_version=STABLE_CHROME_VERSION - 1,
        screen="1920x1080",
    ):
        return self._request_json(
            "get",
            "https://dolphin-anty-api.com/fingerprints/fingerprint"
            f"?platform={platform}&browser_type=anty&browser_version={browser_version}"
            f"&type=fingerprint&screen={screen}",
        )

    def generate_useragent(
        self, platform="windows", browser_version=STABLE_CHROME_VERSION - 1
    ):
        return self._request_json(
            "get",
            "https://dolphin-anty-api.com/fingerprints/useragent"
            f"?browser_type=anty&browser_version={browser_version}&platform={platform}",
        )

    def check_proxy(self, **kwargs):
        return self._request_json(
            "post", "http://localhost:3001/v1.0/check/proxy", json=kwargs
        )

    def import_cookies(self, id, data=""):
        return self._request_json(
            "post",
            f"https://sync.anty-api.com/?actionType=importCookies&browserProfileId={id}",
            data=data,
        )

    def generate_mac(self):
        return self._request_json(
            "get", "https://dolphin-anty-api.com/browser_profiles/generate-mac"
        )

    def fingerprint_to_profile(self, name, tags=[], fingerprint={}):
        data = dict()
        data['name'] = name
        data['tags'] = tags

        data['platform'] = fingerprint['os']['name'].lower()
        data['browserType'] = 'anty'
        data['mainWebsite'] = 'none'

        data['useragent'] = {
            'mode': 'manual',
            'value': fingerprint['userAgent']
        }

        data['deviceName'] = {
            'mode': 'off',
            'value': None
        }

        data['macAddress'] = {
            'mode': 'random',
            'value': None
        }

        data['webrtc'] = {
            'mode': 'altered',
            'ipAddress': None
        }

        data['canvas'] = {
            'mode': 'noise'
        }

        data['webgl'] = {
            'mode': 'noise'
        }

        data['webglInfo'] = {
            'mode': 'manual',
            'vendor': fingerprint['webgl']['unmaskedVendor'],
            'renderer': fingerprint['webgl']['unmaskedRenderer'],
            'webgl2Maximum': fingerprint['webgl2Maximum']
        }

        data['webgpu'] = {
            'mode': 'manual',
            'value': fingerprint['webgpu']
        }

        data['clientRect'] = {
            'mode': 'real'
        }

        data['timezone'] = {
            'mode': 'auto',
            'value': None
        }

        data['locale'] = {
            'mode': 'auto',
            'value': None
        }

        data['proxy'] = {}

        data['geolocation'] = {
            'mode': 'auto',
            'latitude': None,
            'longitude': None,
            'accuracy': None
        }

        data['cpu'] = {
            'mode': 'manual',
            'value': fingerprint['hardwareConcurrency']
        }

        data['memory'] = {
            'mode': 'manual',
            'value': fingerprint['deviceMemory']
        }

        data['screen'] = {
            'mode': 'real',
            'resolution': None
        }

        data['audio'] = {
            'mode': 'real'
        }

        data['mediaDevices'] = {
            'mode': 'real',
            'audioInputs': None,
            'videoInputs': None,
            'audioOutputs': None
        }

        data['ports'] = {
            'mode': 'protect',
            'blacklist': '3389,5900,5800,7070,6568,5938'
        }

        data['doNotTrack'] = False
        data['args'] = []
        data['platformVersion'] = fingerprint['platformVersion']
        data['uaFullVersion'] = fingerprint['browser']['version']
        data['login'] = ''
        data['password'] = ''
        data['appCodeName'] = 'Mozilla'
        data['platformName'] = 'MacIntel'
        data['connectionDownlink'] = fingerprint['connection']['downlink']
        data['connectionEffectiveType'] = fingerprint['connection']['effectiveType']
        data['connectionRtt'] = fingerprint['connection']['rtt']
        data['connectionSaveData'] = fingerprint['connection']['saveData']
        data['cpuArchitecture'] = fingerprint['cpu']['architecture']
        data['osVersion'] = '10.15.7'
        data['vendorSub'] = ''
        data['productSub'] = fingerprint['productSub']
        data['vendor'] = fingerprint['vendor']
        data['product'] = fingerprint['product']

        return data

    def _get_latest_release(self):
        cached_release = get_cached_release()
        if cached_release:
            return cached_release
        return fetch_latest_release()

    def _collect_garbage(self):
        try:
            collect_garbage(self)
        except Exception:
            pass

    def _scan_api_token(self):
        if sys.platform == 'win32':
            storage_path = os.path.join(os.getenv('APPDATA'), 'dolphin_anty', 'Local Storage', 'leveldb')
        elif sys.platform == 'darwin':
            storage_path = os.path.expanduser('~/Library/Application Support/dolphin_anty/Local Storage/leveldb')
        else:
            storage_path = os.path.expanduser('~/.config/dolphin_anty/Local Storage/leveldb')

        if os.path.exists(storage_path):
            files = os.listdir(storage_path)
            for file in files:
                file = os.path.join(storage_path, file)
                try:
                    with open(file, 'rb') as f:
                        detector = chardet.UniversalDetector()
                        for chunk in iter(lambda: f.read(4096), b''):
                            detector.feed(chunk)
                            if detector.done:
                                break
                        detector.close()
                        encoding = detector.result['encoding']
                    with open(file, 'r', encoding=encoding) as f:
                        logs = f.read()
                    token_pattern = r'accessToken.*?(.{100,})"'
                    match = re.search(token_pattern, logs)
                    if match:
                        return match.group(1).split('')[2]
                except Exception as e:
                    print(e, file)
                    pass
        raise ValueError('Token Parse Exception')

"""Numbers that already received a code and can be used again until they expire."""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE_FILE = ROOT / "live_numbers.json"
RENTAL_SECONDS = 20 * 60
_lock = threading.RLock()


def rental_seconds(text: str) -> int:
    raw = str(text or "").strip().lower()
    total = 0
    for number, unit in re.findall(r"(\d+)\s*(days?|hours?|hrs?|h|minutes?|mins?|m)\b", raw):
        amount = int(number)
        if unit.startswith("d"):
            total += amount * 86400
        elif unit.startswith("h"):
            total += amount * 3600
        else:
            total += amount * 60
    return total or RENTAL_SECONDS


def digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def plus_phone(number: str) -> str:
    key = digits(number)
    return f"+{key}" if key else ""


def _now() -> float:
    return time.time()


def _stamp(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _load() -> dict:
    if not STORE_FILE.is_file():
        return {"enabled": False, "phones": {}}
    try:
        loaded = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": False, "phones": {}}
    if not isinstance(loaded, dict):
        return {"enabled": False, "phones": {}}
    phones = loaded.get("phones")
    if not isinstance(phones, dict):
        phones = {}
    return {"enabled": bool(loaded.get("enabled")), "phones": phones}


def _save(data: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STORE_FILE.with_name(f".{STORE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(STORE_FILE)


def _fresh(record: dict, now: float) -> dict:
    expires = float(record.get("expires_at_unix") or 0)
    remain = max(0, int(expires - now))
    record["remain_seconds"] = remain
    return record


def enabled() -> bool:
    with _lock:
        return bool(_load().get("enabled"))


def set_enabled(value: bool) -> bool:
    with _lock:
        data = _load()
        data["enabled"] = bool(value)
        _save(data)
        return data["enabled"]


def live_count() -> int:
    with _lock:
        now = _now()
        data = _load()
        return sum(1 for item in data["phones"].values() if isinstance(item, dict) and float(item.get("expires_at_unix") or 0) > now)


def is_live(phone: str) -> bool:
    key = digits(phone)
    if not key:
        return False
    with _lock:
        record = _load()["phones"].get(key)
        return isinstance(record, dict) and float(record.get("expires_at_unix") or 0) > _now()


def last_code(phone: str) -> str:
    key = digits(phone)
    if not key:
        return ""
    with _lock:
        record = _load()["phones"].get(key)
        if not isinstance(record, dict):
            return ""
        return str(record.get("last_code") or "")


def record(phone: str) -> dict:
    key = digits(phone)
    if not key:
        return {}
    with _lock:
        item = _load()["phones"].get(key)
        if not isinstance(item, dict):
            return {}
        return _fresh(dict(item), _now())


def same_as_last(phone: str, code: str) -> bool:
    if not enabled() or not code:
        return False
    previous = last_code(phone)
    return bool(previous) and previous == code


def pause(phone: str, seconds: int = 45) -> None:
    key = digits(phone)
    if not key:
        return
    with _lock:
        data = _load()
        item = data["phones"].get(key)
        if not isinstance(item, dict):
            return
        item["pause_until"] = _now() + max(1, int(seconds))
        _save(data)


def partition(records: list) -> tuple[list, list]:
    if not enabled():
        return [], [item for item in records if isinstance(item, dict)]
    keep = []
    close = []
    for item in records:
        if not isinstance(item, dict):
            continue
        if is_live(str(item.get("phone") or "")):
            keep.append(item)
        else:
            close.append(item)
    return keep, close


def take(provider: str, kind: str, count: int) -> dict | None:
    if not enabled():
        return None
    pool = usable(provider, kind)
    if not pool:
        return None
    chosen = []
    for item in pool[: max(1, int(count or 1))]:
        copy = dict(item)
        copy.pop("last_code", None)
        chosen.append(copy)
    return {
        "ok": True,
        "type": kind,
        "phones": [str(item.get("phone") or "") for item in chosen],
        "orders": chosen,
        "reused": True,
    }


def usable(provider: str, kind: str) -> list[dict]:
    with _lock:
        now = _now()
        data = _load()
        changed = False
        found = []
        for item in data["phones"].values():
            if not isinstance(item, dict):
                continue
            remain = max(0, int(float(item.get("expires_at_unix") or 0) - now))
            if item.get("remain_seconds") != remain:
                item["remain_seconds"] = remain
                changed = True
            if str(item.get("provider") or "") != provider or str(item.get("type") or "") != kind:
                continue
            if float(item.get("expires_at_unix") or 0) <= now + 90:
                continue
            if float(item.get("pause_until") or 0) > now:
                continue
            found.append(_fresh(dict(item), now))
        if changed:
            _save(data)
        found.sort(key=lambda item: float(item.get("expires_at_unix") or 0), reverse=True)
        return found


def expired(provider: str) -> list[dict]:
    with _lock:
        now = _now()
        found = []
        for record in _load()["phones"].values():
            if not isinstance(record, dict):
                continue
            if str(record.get("provider") or "") != provider:
                continue
            if float(record.get("expires_at_unix") or 0) > now:
                continue
            found.append(dict(record))
        return found


def drop(phone: str) -> None:
    key = digits(phone)
    if not key:
        return
    with _lock:
        data = _load()
        if key in data["phones"]:
            data["phones"].pop(key, None)
            _save(data)


def prove(
    provider: str,
    kind: str,
    phone: str,
    *,
    code: str = "",
    ordered_at: str = "",
    order_id: str = "",
    booking_id: str = "",
    ttl_seconds: int = RENTAL_SECONDS,
) -> dict:
    key = digits(phone)
    if not key:
        return {}
    with _lock:
        data = _load()
        current = data["phones"].get(key)
        if not isinstance(current, dict):
            current = {}
        ordered = _parse_time(ordered_at) or _parse_time(str(current.get("ordered_at") or "")) or _now()
        expires = float(current.get("expires_at_unix") or 0)
        if expires <= ordered:
            expires = ordered + max(60, int(ttl_seconds))
        uses = int(current.get("uses") or 0) + 1
        record = {
            "phone": plus_phone(key),
            "provider": provider,
            "type": kind,
            "order_id": order_id or str(current.get("order_id") or ""),
            "booking_id": booking_id or str(current.get("booking_id") or ""),
            "ordered_at": _stamp(ordered),
            "expires_at": _stamp(expires),
            "expires_at_unix": expires,
            "remain_seconds": max(0, int(expires - _now())),
            "uses": uses,
            "last_code": code or str(current.get("last_code") or ""),
            "pause_until": 0,
        }
        data["phones"][key] = record
        _save(data)
        return dict(record)

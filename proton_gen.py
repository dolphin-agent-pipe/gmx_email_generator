"""Create a Dolphin Anty profile, open TARGET_URL2/signup, and create a Proton mailbox.

This follows the same browser setup as gmx_gen.py and does not ask for an SMS code.
Dolphin Anty must already be running and logged in on this machine.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import requests

import gmx_gen
from gmx_gen import (
    ACCOUNTS_FILE,
    Cdp,
    CdpSocket,
    DolphinAPI,
    _accounts_lock,
    child_frames,
    connect_page,
    create_profile,
    eval_in_frame,
    evaluate,
    field_value,
    finish_profile,
    load_random_proxy,
    local_login,
    mouse_click,
    navigate,
    normalize_profile_name,
    park_broken,
    press_enter,
    proxy_body,
    proxy_label,
    resolve_proxy_type,
    start_profile,
    stop_profile,
    write_text_atomic,
)

ROOT = Path(__file__).resolve().parent
TARGET_URL2 = os.environ.get("TARGET_URL2", "").strip()
CAPTCHA_ATTEMPTS = 5
CAPTCHA_LOAD_TRIES = 3
SIGNUP_RESTARTS = 3
_PRINT = print
_HOOKS = threading.local()


def hook_stop() -> None:
    stop = getattr(_HOOKS, "stop", None)
    if stop:
        stop()


def note(message: str) -> None:
    hook_stop()
    log = getattr(_HOOKS, "log", None) or _PRINT
    log(message)


class SignupRestart(Exception):
    """The captcha did not load, so the signup starts again."""


def signup_url(explicit: str = "") -> str:
    chosen = (explicit or "").strip()
    base = TARGET_URL2.rstrip("/")
    if chosen and chosen.rstrip("/") != base:
        return chosen
    if not base:
        raise SystemExit(f"Set TARGET_URL2 in {gmx_gen.ENV_FILE}")
    if base.endswith("/signup"):
        return base
    return base + "/signup"


PROTON_DOMAINS = ("proton.me", "protonmail.com", "proton.com", "pm.me")


def proton_address(value: str) -> tuple[str, str]:
    text = " ".join((value or "").split())
    domain = "proton.me"
    if "@" in text:
        local, domain = text.split("@", 1)
        text = local.strip()
        domain = domain.strip().lower()
    text = text.lower()
    if domain not in PROTON_DOMAINS:
        raise SystemExit("Use a proton.me, protonmail.com, or proton.com address")
    if not text or any(char.isspace() for char in text):
        raise SystemExit("Username is missing")
    return text, domain


def signup_page(domain: str, explicit: str = "") -> str:
    if domain not in PROTON_DOMAINS:
        raise SystemExit("Use a proton.me, protonmail.com, or proton.com address")
    if (explicit or "").strip():
        return signup_url(explicit)
    return signup_url("")


def normalize_username(value: str) -> str:
    return proton_address(value)[0]


def mouse_drag(cdp: Cdp, x1: float, y1: float, x2: float, y2: float, session_id: str | None = None) -> None:
    def move(x: float, y: float, buttons: int = 0) -> None:
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": buttons},
            session_id=session_id,
        )

    move(x1, y1)
    time.sleep(0.12)
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x1, "y": y1, "button": "left", "buttons": 1, "clickCount": 1},
        session_id=session_id,
    )
    steps = 28
    for index in range(1, steps + 1):
        t = index / steps
        ease = t * t * (3 - 2 * t)
        move(x1 + (x2 - x1) * ease, y1 + (y2 - y1) * ease, 1)
        time.sleep(0.016)
    time.sleep(0.08)
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x2, "y": y2, "button": "left", "clickCount": 1},
        session_id=session_id,
    )


def mouse_click_in(cdp: Cdp, x: float, y: float, session_id: str | None = None) -> None:
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id=session_id)
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        session_id=session_id,
    )
    time.sleep(0.05)
    cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        session_id=session_id,
    )


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    nearest_left = abs(estimate - left)
    nearest_up = abs(estimate - up)
    nearest_upper_left = abs(estimate - upper_left)
    if nearest_left <= nearest_up and nearest_left <= nearest_upper_left:
        return left
    if nearest_up <= nearest_upper_left:
        return up
    return upper_left


def decode_png(blob: bytes) -> tuple[int, int, bytes]:
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("screenshot was not a PNG")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(blob):
        length = struct.unpack(">I", blob[pos : pos + 4])[0]
        kind = blob[pos + 4 : pos + 8]
        chunk = blob[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif kind == b"IDAT":
            idat.extend(chunk)
        elif kind == b"IEND":
            break
    if width < 1 or height < 1 or bit_depth != 8 or color_type not in (2, 6):
        raise ValueError(f"unsupported screenshot PNG {width}x{height} depth={bit_depth} type={color_type}")
    channels = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[bytes] = []
    cursor = 0
    previous = bytearray(stride)
    for _y in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        if filter_type == 1:
            for x in range(stride):
                left = row[x - channels] if x >= channels else 0
                row[x] = (row[x] + left) & 255
        elif filter_type == 2:
            for x in range(stride):
                row[x] = (row[x] + previous[x]) & 255
        elif filter_type == 3:
            for x in range(stride):
                left = row[x - channels] if x >= channels else 0
                row[x] = (row[x] + ((left + previous[x]) // 2)) & 255
        elif filter_type == 4:
            for x in range(stride):
                left = row[x - channels] if x >= channels else 0
                up = previous[x]
                upper_left = previous[x - channels] if x >= channels else 0
                row[x] = (row[x] + _paeth(left, up, upper_left)) & 255
        elif filter_type != 0:
            raise ValueError(f"bad PNG filter {filter_type}")
        previous = row
        if channels == 4:
            rows.append(bytes(row))
            continue
        rgba = bytearray(width * 4)
        for x in range(width):
            rgba[x * 4 : x * 4 + 3] = row[x * 3 : x * 3 + 3]
            rgba[x * 4 + 3] = 255
        rows.append(bytes(rgba))
    return width, height, b"".join(rows)


def screenshot(cdp: Cdp, session_id: str | None = None) -> tuple[int, int, bytes, float]:
    cdp.call("Page.enable", session_id=session_id)
    payload = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True}, session_id=session_id)
    width, height, pixels = decode_png(base64.b64decode(payload.get("data") or ""))
    view = evaluate(cdp, "({w: window.innerWidth, h: window.innerHeight})", session_id=session_id)
    css_width = float((view or {}).get("w") or width)
    scale = width / css_width if css_width else 1.0
    return width, height, pixels, scale


def crop_rgba(pixels: bytes, width: int, height: int, x: int, y: int, w: int, h: int) -> tuple[int, int, bytes]:
    x0 = min(max(0, x), width)
    y0 = min(max(0, y), height)
    x1 = min(width, x0 + max(0, w))
    y1 = min(height, y0 + max(0, h))
    cw = x1 - x0
    ch = y1 - y0
    if cw < 2 or ch < 2:
        return 0, 0, b""
    out = bytearray(cw * ch * 4)
    for row in range(ch):
        src = ((y0 + row) * width + x0) * 4
        dst = row * cw * 4
        out[dst : dst + cw * 4] = pixels[src : src + cw * 4]
    return cw, ch, bytes(out)


def _shrink(pixels: bytes, width: int, height: int, max_width: int) -> tuple[int, int, bytes, float]:
    if width <= max_width:
        return width, height, pixels, 1.0
    scale = max_width / width
    nw = max_width
    nh = max(1, int(round(height * scale)))
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = min(height - 1, int(y / scale))
        for x in range(nw):
            sx = min(width - 1, int(x / scale))
            src = (sy * width + sx) * 4
            dst = (y * nw + x) * 4
            out[dst : dst + 4] = pixels[src : src + 4]
    return nw, nh, bytes(out), scale


def _piece_mask(pixels: bytes, width: int, height: int) -> bytearray | None:
    def corner(x: int, y: int) -> tuple[int, int, int]:
        offset = (y * width + x) * 4
        return pixels[offset], pixels[offset + 1], pixels[offset + 2]

    corners = (corner(0, 0), corner(width - 1, 0), corner(0, height - 1), corner(width - 1, height - 1))
    background = tuple(sum(channel) // 4 for channel in zip(*corners))

    def outside(index: int, limit: int) -> bool:
        offset = index * 4
        if pixels[offset + 3] < 16:
            return True
        distance = (
            abs(pixels[offset] - background[0])
            + abs(pixels[offset + 1] - background[1])
            + abs(pixels[offset + 2] - background[2])
        )
        return distance <= limit

    def flood(limit: int) -> bytearray:
        seen = bytearray(width * height)
        stack = [x for x in range(width)]
        stack.extend((height - 1) * width + x for x in range(width))
        stack.extend(y * width for y in range(height))
        stack.extend(y * width + width - 1 for y in range(height))
        while stack:
            index = stack.pop()
            if index < 0 or index >= width * height or seen[index] or not outside(index, limit):
                continue
            seen[index] = 1
            x = index % width
            if x > 0:
                stack.append(index - 1)
            if x + 1 < width:
                stack.append(index + 1)
            if index >= width:
                stack.append(index - width)
            if index + width < width * height:
                stack.append(index + width)
        mask = bytearray(width * height)
        for index, flag in enumerate(seen):
            if not flag and pixels[index * 4 + 3] > 16:
                mask[index] = 1
        return mask

    for limit in (28, 18, 42):
        mask = flood(limit)
        count = sum(mask)
        if width * height * 0.12 <= count <= width * height * 0.82:
            return mask
    return None


def _components(mask: bytearray, width: int, height: int, minimum: int) -> list[list[int]]:
    seen = bytearray(width * height)
    found: list[list[int]] = []
    for start, flag in enumerate(mask):
        if not flag or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        cells = [start]
        while stack:
            index = stack.pop()
            x = index % width
            for nxt in (index - 1 if x else -1, index + 1 if x + 1 < width else -1, index - width, index + width):
                if nxt < 0 or nxt >= width * height or seen[nxt] or not mask[nxt]:
                    continue
                seen[nxt] = 1
                stack.append(nxt)
                cells.append(nxt)
        if len(cells) >= minimum:
            found.append(cells)
    return found


def _raster(cells: list[int], width: int, size: int = 24) -> bytes:
    xs = [cell % width for cell in cells]
    ys = [cell // width for cell in cells]
    x0, x1 = min(xs), max(xs) + 1
    y0, y1 = min(ys), max(ys) + 1
    bw = max(1, x1 - x0)
    bh = max(1, y1 - y0)
    grid = bytearray(size * size)
    points = {(cell % width, cell // width) for cell in cells}
    for y in range(size):
        sy = y0 + min(bh - 1, int(y * bh / size))
        for x in range(size):
            sx = x0 + min(bw - 1, int(x * bw / size))
            if (sx, sy) in points:
                grid[y * size + x] = 1
    return bytes(grid)


def _mismatch(left: bytes, right: bytes) -> float:
    differ = 0
    either = 0
    for a, b in zip(left, right):
        if a or b:
            either += 1
        if a != b:
            differ += 1
    if not either:
        return 1.0
    return differ / either


def _hole_mask(pixels: bytes, width: int, height: int) -> bytearray:
    dark = bytearray(width * height)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            samples = []
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    offset = ((y + dy) * width + x + dx) * 4
                    samples.append((pixels[offset] + pixels[offset + 1] + pixels[offset + 2]) // 3)
            if sum(samples) / len(samples) <= 78 and max(samples) - min(samples) <= 36:
                dark[y * width + x] = 1
    return dark


def _bright_border(pixels: bytes, width: int, height: int, cells: list[int]) -> float:
    member = bytearray(width * height)
    for cell in cells:
        member[cell] = 1
    edge = 0
    bright = 0
    for cell in cells:
        x = cell % width
        y = cell // width
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx = x + dx
            ny = y + dy
            if nx < 0 or ny < 0 or nx >= width or ny >= height or member[ny * width + nx]:
                continue
            edge += 1
            for step in range(1, 7):
                sx = x + dx * step
                sy = y + dy * step
                if sx < 0 or sy < 0 or sx >= width or sy >= height:
                    break
                offset = (sy * width + sx) * 4
                if pixels[offset] > 175 and pixels[offset + 1] > 175 and pixels[offset + 2] > 175:
                    bright += 1
                    break
            break
    if not edge:
        return 0.0
    return bright / edge


def find_hole(piece: bytes, piece_w: int, piece_h: int, board: bytes, board_w: int, board_h: int) -> tuple[float, float, float] | None:
    pw, ph, piece_small, piece_scale = _shrink(piece, piece_w, piece_h, 96)
    bw, bh, board_small, board_scale = _shrink(board, board_w, board_h, 280)
    mask = _piece_mask(piece_small, pw, ph)
    if not mask:
        return None
    piece_cells = [index for index, flag in enumerate(mask) if flag]
    if len(piece_cells) < 20:
        return None
    piece_shape = _raster(piece_cells, pw)
    piece_area = len(piece_cells) / (piece_scale * piece_scale)
    holes = _hole_mask(board_small, bw, bh)
    minimum = max(12, int(piece_area * 0.2 * board_scale * board_scale))
    ranked: list[tuple[float, float, float, float]] = []
    for cells in _components(holes, bw, bh, minimum):
        hole_area = len(cells) / (board_scale * board_scale)
        if hole_area < piece_area * 0.25 or hole_area > piece_area * 3.5:
            continue
        score = _mismatch(piece_shape, _raster(cells, bw))
        border = _bright_border(board_small, bw, bh, cells)
        xs = [cell % bw for cell in cells]
        ys = [cell // bw for cell in cells]
        center_x = (sum(xs) / len(xs) + 0.5) / board_scale
        center_y = (sum(ys) / len(ys) + 0.5) / board_scale
        ranked.append((score, border, center_x, center_y))
    outlined = [item for item in ranked if item[1] >= 0.15]
    pool = outlined or ranked
    if not pool:
        return None
    score, _border, center_x, center_y = min(pool, key=lambda item: item[0])
    return (center_x, center_y, score)


def piece_grab(piece: bytes, piece_w: int, piece_h: int) -> tuple[float, float] | None:
    pw, ph, small, scale = _shrink(piece, piece_w, piece_h, 96)
    mask = _piece_mask(small, pw, ph)
    if not mask:
        return None
    xs = [index % pw for index, flag in enumerate(mask) if flag]
    ys = [index // pw for index, flag in enumerate(mask) if flag]
    if not xs:
        return None
    return ((sum(xs) / len(xs) + 0.5) / scale, (sum(ys) / len(ys) + 0.5) / scale)


LAYOUT_JS = r"""
(() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.left, y: r.top, w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2};
  };
  const roots = [];
  const visit = (root) => {
    if (!root || !root.querySelectorAll || roots.includes(root)) return;
    roots.push(root);
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
  const queryAll = (selector) => roots.flatMap((root) => [...root.querySelectorAll(selector)]);
  const buttons = queryAll('button, [role="button"], a').map((el) => Object.assign({label: clean(el.innerText)}, boxOf(el))).filter((box) => box.w > 2 && box.h > 2);
  const text = clean(document.body && document.body.innerText).slice(0, 900);
  const retry = buttons.find((box) => /^(retry|erneut versuchen|wiederholen|noch einmal)$/i.test(box.label));
  if (retry && /did not pass|nicht bestanden|nicht geschafft|leider/i.test(text)) {
    const other = buttons.find((box) => /different challenge|andere aufgabe/i.test(box.label));
    return {state: 'retry', retry, other: other || null};
  }
  const next = buttons.find((box) => /^(next|weiter)$/i.test(box.label))
    || buttons.find((box) => /\b(next|weiter)\b/i.test(box.label) && box.label.length < 24);
  const marker = /puzzleteil|puzzle piece|human verification|menschliche verifizierung|reset puzzle|zur[uü]cksetzen/i.test(text);
  const visuals = queryAll('img, canvas, svg').filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 24 && r.height > 24;
  });
  if (visuals.length < 2) {
    for (const el of queryAll('div')) {
      const image = (getComputedStyle(el).backgroundImage || '');
      const r = el.getBoundingClientRect();
      if (image.includes('url(') && r.width > 24 && r.height > 24) visuals.push(el);
    }
  }
  if (!next || visuals.length < 2) {
    const canvas = queryAll('canvas').filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 120 && r.height > 120;
    }).sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return rb.width * rb.height - ra.width * ra.height;
    })[0];
    if (next && canvas) return {state: 'canvas', canvas: boxOf(canvas), next, buttons: buttons.map((box) => box.label).slice(0, 8), pictures: visuals.length};
    if (marker || (next && visuals.length >= 1)) return {state: 'present', buttons: buttons.map((box) => box.label).slice(0, 8), pictures: visuals.length};
    return {state: 'waiting', buttons: buttons.map((box) => box.label).slice(0, 8), pictures: visuals.length};
  }
  const scrollTo = (el) => {
    try { el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); }
    catch (err) { el.scrollIntoView(); }
  };
  const biggest = visuals.slice().sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return rb.width * rb.height - ra.width * ra.height;
  })[0];
  scrollTo(biggest);
  const buttonsNow = queryAll('button, [role="button"], a').map((el) => Object.assign({label: clean(el.innerText)}, boxOf(el))).filter((box) => box.w > 2 && box.h > 2);
  const nextNow = buttonsNow.find((box) => /^(next|weiter)$/i.test(box.label)) || next;
  const boxes = visuals.map(boxOf).sort((a, b) => b.w * b.h - a.w * a.h);
  const board = boxes[0];
  const piece = boxes.find((box) => {
    if (box === board || box.w * box.h >= board.w * board.h * 0.5) return false;
    const above = box.cy < board.y + 48;
    const onCorner = box.y < board.y + board.h * 0.4 && box.x < board.x + board.w * 0.45;
    return above || onCorner;
  }) || boxes.find((box) => box !== board);
  if (!piece) return {state: 'present', buttons: buttonsNow.map((box) => box.label).slice(0, 8), pictures: boxes.length};
  return {state: 'puzzle', board, piece, next: nextNow};
})()
"""


def iframe_shift(cdp: Cdp, parent_id: str, url: str) -> tuple[float, float] | None:
    expression = (
        "(() => {"
        f" const want = {json.dumps(url)};"
        " const frames = [];"
        " const walk = (root) => {"
        "  if (!root || !root.querySelectorAll || frames.some((item) => item.root === root)) return;"
        "  for (const frame of root.querySelectorAll('iframe')) frames.push({frame, root, src: frame.src || frame.getAttribute('src') || '', name: frame.name || '', title: frame.title || '', id: frame.id || ''});"
        "  for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);"
        " };"
        " walk(document);"
        " const exact = frames.find((item) => item.src && (item.src === want || want.startsWith(item.src) || item.src.split('?')[0] === want.split('?')[0]));"
        " const named = /captcha|verify|pcaptcha/i.test(want) ? frames.find((item) => item.name === 'pcaptcha' || /captcha|verify|pcaptcha/i.test(item.src + ' ' + item.name + ' ' + item.title + ' ' + item.id)) : null;"
        " const only = frames.length === 1 ? frames[0] : null;"
        " const hit = exact || named || only;"
        " if (!hit) return null;"
        " const frame = hit.frame;"
        " try { frame.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); }"
        " catch (err) { frame.scrollIntoView(); }"
        " const r = frame.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2) return null;"
        " return {left: r.left, top: r.top};"
        "})()"
    )
    box = eval_in_frame(cdp, parent_id, expression)
    if not isinstance(box, dict):
        return None
    return float(box.get("left") or 0), float(box.get("top") or 0)


def frame_origin(cdp: Cdp, frame_id: str) -> tuple[float, float] | None:
    frames = {str(frame.get("id")): frame for frame in child_frames(cdp) if frame.get("id")}
    current = frames.get(frame_id)
    if not current:
        return None
    if not current.get("parentId"):
        return 0.0, 0.0
    x = y = 0.0
    seen: set[str] = set()
    while current and current.get("parentId") and str(current.get("id")) not in seen:
        seen.add(str(current.get("id")))
        parent_id = str(current.get("parentId"))
        shift = iframe_shift(cdp, parent_id, str(current.get("url") or ""))
        if shift is None:
            return None
        x += shift[0]
        y += shift[1]
        current = frames.get(parent_id)
    return x, y


_CAPTCHA_SESSIONS: dict[str, str] = {}


def captcha_sessions(cdp: Cdp) -> list[str]:
    try:
        cdp.call("Target.setDiscoverTargets", {"discover": True})
        payload = cdp.call("Target.getTargets")
    except (RuntimeError, TimeoutError):
        return []
    sessions: list[str] = []
    for info in payload.get("targetInfos") or []:
        url = str(info.get("url") or "").lower()
        target_id = str(info.get("targetId") or "")
        title = str(info.get("title") or "").lower()
        if not target_id or not any(part in url or part in title for part in ("captcha", "verify-api", "verify.proton", "pcaptcha", "verifizierung")):
            continue
        known = _CAPTCHA_SESSIONS.get(target_id)
        if known:
            sessions.append(known)
            continue
        try:
            attached = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        except (RuntimeError, TimeoutError):
            continue
        session_id = str(attached.get("sessionId") or "")
        if session_id:
            _CAPTCHA_SESSIONS[target_id] = session_id
            sessions.append(session_id)
    return sessions


def captcha_frame_visible(cdp: Cdp) -> bool:
    return bool(
        evaluate(
            cdp,
            "(() => {"
            " const walk = (root) => {"
            "  if (!root || !root.querySelectorAll) return false;"
            "  for (const frame of root.querySelectorAll('iframe')) {"
            "   const label = (frame.src || frame.getAttribute('src') || '') + ' ' + (frame.name || '') + ' ' + (frame.title || '') + ' ' + (frame.id || '');"
            "   const r = frame.getBoundingClientRect();"
            "   if (/captcha|pcaptcha|verify/i.test(label) && r.width > 80 && r.height > 80 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight) return true;"
            "  }"
            "  for (const el of root.querySelectorAll('*')) if (el.shadowRoot && walk(el.shadowRoot)) return true;"
            "  return false;"
            " };"
            " return walk(document);"
            "})()",
        )
    )


def locate_captcha(cdp: Cdp) -> dict | None:
    for session_id in captcha_sessions(cdp):
        try:
            found = evaluate(cdp, LAYOUT_JS, session_id=session_id)
        except (RuntimeError, TimeoutError):
            continue
        if not isinstance(found, dict) or found.get("state") in (None, "waiting"):
            continue
        found["ox"], found["oy"] = 0.0, 0.0
        found["session_id"] = session_id
        return found
    for frame in child_frames(cdp):
        frame_id = str(frame.get("id") or "")
        if not frame_id:
            continue
        found = eval_in_frame(cdp, frame_id, LAYOUT_JS)
        if not isinstance(found, dict) or found.get("state") in (None, "waiting"):
            continue
        origin = frame_origin(cdp, frame_id)
        found["ox"], found["oy"] = origin if origin else (0.0, 0.0)
        found["frame_id"] = frame_id
        return found
    if captcha_frame_visible(cdp):
        return {"state": "present", "ox": 0.0, "oy": 0.0}
    return None


def _abs_point(found: dict, key: str) -> tuple[float, float] | None:
    point = found.get(key)
    if not isinstance(point, dict):
        return None
    return float(point.get("cx") or point.get("x") or 0) + float(found["ox"]), float(point.get("cy") or point.get("y") or 0) + float(found["oy"])


def _abs_rect(found: dict, key: str) -> dict | None:
    rect = found.get(key)
    if not isinstance(rect, dict):
        return None
    return {
        "x": float(rect["x"]) + float(found["ox"]),
        "y": float(rect["y"]) + float(found["oy"]),
        "w": float(rect["w"]),
        "h": float(rect["h"]),
    }


def log_captcha(found: dict) -> None:
    buttons = found.get("buttons") or []
    label = ", ".join(str(item) for item in buttons) or "none"
    note(
        f"Captcha found: state={found.get('state')} buttons={label} pictures={found.get('pictures', 0)} "
        f"at ({float(found.get('ox') or 0):.0f}, {float(found.get('oy') or 0):.0f})"
    )


def split_canvas(pixels: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Return piece crop (x, y, w, h) and the photo top. The loose piece sits on the white margin."""

    def white(x: int, y: int) -> bool:
        offset = (y * width + x) * 4
        return pixels[offset] > 245 and pixels[offset + 1] > 245 and pixels[offset + 2] > 245

    photo_top = next((y for y in range(height) if sum(white(x, y) for x in range(0, width, 2)) < width * 0.15), None)
    if photo_top is None or photo_top < 16:
        return None
    seen: set[tuple[int, int]] = set()
    best: list[tuple[int, int]] = []
    for y in range(photo_top):
        for x in range(width):
            if (x, y) in seen or white(x, y):
                continue
            stack = [(x, y)]
            seen.add((x, y))
            cells: list[tuple[int, int]] = []
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= photo_top or (nx, ny) in seen or white(nx, ny):
                        continue
                    seen.add((nx, ny))
                    stack.append((nx, ny))
            if len(cells) > len(best):
                best = cells
    if len(best) < 40:
        return None
    xs = [cell[0] for cell in best]
    return max(0, min(xs) - 4), 0, min(width, max(xs) + 5) - max(0, min(xs) - 4), photo_top


def solve_canvas(cdp: Cdp, found: dict) -> bool:
    frame_id = str(found.get("frame_id") or "")
    canvas = found.get("canvas")
    nxt = _abs_point(found, "next")
    if not frame_id or not isinstance(canvas, dict) or not nxt:
        return False
    encoded = eval_in_frame(
        cdp,
        frame_id,
        "(() => { const canvas = document.querySelector('canvas'); if (!canvas) return ''; try { return canvas.toDataURL('image/png'); } catch (err) { return ''; } })()",
    )
    if not isinstance(encoded, str) or "," not in encoded:
        note("Could not read the captcha canvas")
        return False
    try:
        bitmap_w, bitmap_h, pixels = decode_png(base64.b64decode(encoded.split(",", 1)[1]))
    except (ValueError, base64.binascii.Error) as exc:
        note(f"Could not read the captcha canvas: {exc}")
        return False
    piece_box = split_canvas(pixels, bitmap_w, bitmap_h)
    if piece_box is None:
        note("Could not see the loose puzzle piece on the canvas")
        return False
    piece_x, piece_y, piece_w, piece_h = piece_box
    _pw, _ph, piece_px = crop_rgba(pixels, bitmap_w, bitmap_h, piece_x, piece_y, piece_w, piece_h)
    board_y = piece_h
    _bw, _bh, board_px = crop_rgba(pixels, bitmap_w, bitmap_h, 0, board_y, bitmap_w, bitmap_h - board_y)
    if _pw < 8 or _bw < 8:
        note("Could not see the loose puzzle piece on the canvas")
        return False
    hole = find_hole(piece_px, _pw, _ph, board_px, _bw, _bh)
    grab = piece_grab(piece_px, _pw, _ph)
    if hole is None or grab is None:
        note("Could not see where the puzzle piece belongs")
        return False
    scale_x = float(canvas["w"]) / bitmap_w
    scale_y = float(canvas["h"]) / bitmap_h
    origin_x = float(found.get("ox") or 0) + float(canvas["x"])
    origin_y = float(found.get("oy") or 0) + float(canvas["y"])
    start_x = origin_x + (piece_x + grab[0]) * scale_x
    start_y = origin_y + (piece_y + grab[1]) * scale_y
    end_x = origin_x + hole[0] * scale_x
    end_y = origin_y + (board_y + hole[1]) * scale_y
    note(f"Dragging the puzzle piece from ({start_x:.0f}, {start_y:.0f}) to ({end_x:.0f}, {end_y:.0f}) (match {hole[2]:.2f})")
    session_id = frame_session(cdp, "captcha/v1/assets") or None
    shift_x = float(found.get("ox") or 0)
    shift_y = float(found.get("oy") or 0)
    if session_id:
        mouse_drag(cdp, start_x - shift_x, start_y - shift_y, end_x - shift_x, end_y - shift_y, session_id)
        time.sleep(0.45)
        mouse_click_in(cdp, nxt[0] - shift_x, nxt[1] - shift_y, session_id)
    else:
        mouse_drag(cdp, start_x, start_y, end_x, end_y, None)
        time.sleep(0.45)
        mouse_click_in(cdp, nxt[0], nxt[1], None)
    note("Clicked Weiter")
    return True


def solve_puzzle(cdp: Cdp, found: dict) -> bool:
    board = _abs_rect(found, "board")
    piece = _abs_rect(found, "piece")
    nxt = _abs_point(found, "next")
    if not board or not piece or not nxt:
        return False
    time.sleep(0.3)
    session_id = str(found.get("session_id") or "") or None
    try:
        width, height, pixels, scale = screenshot(cdp, session_id)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        note(f"Could not photograph the captcha: {exc}")
        return False

    def region(rect: dict) -> tuple[int, int, bytes]:
        return crop_rgba(
            pixels,
            width,
            height,
            int(rect["x"] * scale),
            int(rect["y"] * scale),
            max(1, int(rect["w"] * scale)),
            max(1, int(rect["h"] * scale)),
        )

    piece_w, piece_h, piece_px = region(piece)
    board_w, board_h, board_px = region(board)
    if piece_w < 8 or board_w < 8:
        note("Captcha picture was outside the window")
        return False
    hole = find_hole(piece_px, piece_w, piece_h, board_px, board_w, board_h)
    grab = piece_grab(piece_px, piece_w, piece_h)
    if hole is None or grab is None:
        note("Could not see where the puzzle piece belongs")
        return False
    hole_x, hole_y, score = hole
    start_x = piece["x"] + grab[0] / piece_w * piece["w"]
    start_y = piece["y"] + grab[1] / piece_h * piece["h"]
    end_x = board["x"] + hole_x / board_w * board["w"]
    end_y = board["y"] + hole_y / board_h * board["h"]
    note(f"Dragging the puzzle piece from ({start_x:.0f}, {start_y:.0f}) to ({end_x:.0f}, {end_y:.0f}) (match {score:.2f})")
    mouse_drag(cdp, start_x, start_y, end_x, end_y, session_id)
    time.sleep(0.45)
    mouse_click_in(cdp, nxt[0], nxt[1], session_id)
    note("Clicked Weiter")
    return True


def abuse_blocked(cdp: Cdp) -> bool:
    return bool(
        evaluate(
            cdp,
            "(() => {"
            " const text = document.body ? document.body.innerText : '';"
            " return /abusive traffic|blocked any further signups|missbräuchlich/i.test(text);"
            "})()",
        )
    )


def captcha_ready(cdp: Cdp) -> bool:
    return bool(locate_captcha(cdp) or captcha_passed(cdp))


def mail_plus_decline(cdp: Cdp) -> dict | None:
    box = evaluate(
        cdp,
        "(() => {"
        " const button = [...document.querySelectorAll('button.button-ghost-norm')].find((el) => {"
        "  const label = (el.innerText || '').replace(/\\s+/g, ' ').trim();"
        "  if (!/^nein, danke$/i.test(label) || el.disabled || el.getAttribute('aria-busy') === 'true') return false;"
        "  const r = el.getBoundingClientRect();"
        "  return r.width > 2 && r.height > 2;"
        " });"
        " if (!button) return null;"
        " let r = button.getBoundingClientRect();"
        " if (r.bottom < 0 || r.top > window.innerHeight) {"
        "  button.scrollIntoView({block: 'center', inline: 'nearest'});"
        "  r = button.getBoundingClientRect();"
        " }"
        " if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > window.innerHeight) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()",
    )
    return box if isinstance(box, dict) else None


def dismiss_mail_plus(cdp: Cdp, wait: float = 3) -> bool:
    """Decline the Mail Plus offer when it covers the form."""
    clicked = False
    deadline = time.time() + wait
    while time.time() < deadline:
        box = mail_plus_decline(cdp)
        if not box:
            if clicked:
                return True
            time.sleep(0.3)
            continue
        mouse_click(cdp, float(box["x"]), float(box["y"]))
        clear_selection(cdp)
        if not clicked:
            note("Clicked Nein, danke")
        clicked = True
        time.sleep(0.45)
    return clicked


def open_captcha(cdp: Cdp) -> None:
    """Submit the password form until the puzzle appears. Three misses restarts the signup."""
    for attempt in range(1, CAPTCHA_LOAD_TRIES + 1):
        stop_if_username_taken(cdp)
        if abuse_blocked(cdp):
            note("Proton blocked further signups on this network.")
        dismiss_mail_plus(cdp, 0.6)
        if not captcha_ready(cdp):
            press_enter(cdp)
            note("Pressed Enter")
            click_deadline = time.time() + 3
            while time.time() < click_deadline and not captcha_ready(cdp):
                if click_create_account(cdp):
                    break
                time.sleep(0.4)
        deadline = time.time() + 6
        while time.time() < deadline:
            stop_if_username_taken(cdp)
            found = locate_captcha(cdp)
            if found:
                note("Captcha loaded")
                log_captcha(found)
                return
            time.sleep(0.4)
        if attempt == CAPTCHA_LOAD_TRIES:
            note("Captcha did not load after 3 tries. Restarting on a new proxy.")
            raise SignupRestart()
        pause = random.uniform(5, 7)
        note(f"Captcha did not load. Waiting {pause:.1f}s and trying again ({attempt}/3)")
        time.sleep(pause)


def captcha_passed(cdp: Cdp) -> bool:
    return bool(
        evaluate(
            cdp,
            "(() => {"
            " if (document.querySelector('#displayName, #understood-recovery-necessity')) return true;"
            " const text = document.body ? document.body.innerText : '';"
            " return /captcha complete|display name|recovery method|maybe later/i.test(text);"
            "})()",
        )
    )


def solve_captcha(cdp: Cdp) -> None:
    note("Looking for the captcha")
    announced = False
    deadline = time.time() + 50
    while time.time() < deadline and not locate_captcha(cdp):
        stop_if_username_taken(cdp)
        if captcha_passed(cdp):
            note("Captcha already complete")
            return
        time.sleep(0.4)
    for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
        if captcha_passed(cdp):
            note("Captcha complete")
            return
        found = None
        until = time.time() + 25
        while time.time() < until:
            stop_if_username_taken(cdp)
            found = locate_captcha(cdp)
            if found and found.get("state") != "present":
                break
            if found:
                time.sleep(0.4)
                continue
            if captcha_passed(cdp):
                note("Captcha complete")
                return
            time.sleep(0.4)
        if not found:
            break
        if found.get("state") == "present":
            log_captcha(found)
            continue
        if found.get("state") == "retry":
            point = _abs_point(found, "retry")
            if point:
                note(f"Captcha failed. Clicking Retry ({attempt}/{CAPTCHA_ATTEMPTS})")
                mouse_click_in(cdp, point[0], point[1], str(found.get("session_id") or "") or None)
                time.sleep(1.2)
                continue
        log_captcha(found)
        note(f"Solving captcha ({attempt}/{CAPTCHA_ATTEMPTS})")
        if not announced:
            note("Captcha puzzle is open")
            announced = True
        solved = solve_canvas(cdp, found) if found.get("state") == "canvas" else solve_puzzle(cdp, found)
        if not solved:
            other = _abs_point(found, "other") or _abs_point(found, "retry")
            if other:
                mouse_click_in(cdp, other[0], other[1], str(found.get("session_id") or "") or None)
                time.sleep(1.2)
            continue
        after = time.time() + 12
        while time.time() < after:
            if captcha_passed(cdp):
                note("Captcha complete")
                return
            failed = locate_captcha(cdp)
            if isinstance(failed, dict) and failed.get("state") == "retry":
                point = _abs_point(failed, "retry")
                if point:
                    note(f"Captcha failed. Clicking Retry ({attempt}/{CAPTCHA_ATTEMPTS})")
                    mouse_click_in(cdp, point[0], point[1], str(failed.get("session_id") or "") or None)
                    time.sleep(1.2)
                break
            time.sleep(0.4)
    raise SystemExit("Captcha was not solved")


def clear_selection(cdp: Cdp) -> None:
    evaluate(
        cdp,
        "(() => { const sel = window.getSelection && window.getSelection(); if (sel) sel.removeAllRanges(); return true; })()",
    )


def click_free_plan(cdp: Cdp) -> None:
    expression = (
        "(() => {"
        " const label = document.querySelector('#free-text');"
        " const button = label && label.closest('button');"
        " if (!button) return null;"
        " let r = button.getBoundingClientRect();"
        " if (r.bottom < 0 || r.top > window.innerHeight) {"
        "  button.scrollIntoView({block: 'center', inline: 'nearest'});"
        "  r = button.getBoundingClientRect();"
        " }"
        " const x = r.left + Math.min(r.width / 2, 48);"
        " const y = r.top + 36;"
        " const hit = document.elementFromPoint(x, y);"
        " const inside = !!(hit && button.contains(hit));"
        " if (button.getAttribute('aria-pressed') !== 'true') button.click();"
        " const sel = window.getSelection && window.getSelection();"
        " if (sel) sel.removeAllRanges();"
        " return {pressed: button.getAttribute('aria-pressed'), x, y, inside};"
        "})()"
    )
    for attempt in range(1, 4):
        deadline = time.time() + 20
        while time.time() < deadline:
            info = evaluate(cdp, expression)
            if isinstance(info, dict):
                if str(info.get("pressed")).lower() == "true":
                    clear_selection(cdp)
                    note("Selected Free")
                    return
                if info.get("inside"):
                    mouse_click(cdp, float(info["x"]), float(info["y"]))
                    clear_selection(cdp)
                time.sleep(0.5)
                continue
            time.sleep(0.4)
        if attempt == 3:
            title = evaluate(cdp, "document.title || ''")
            url = evaluate(cdp, "location.href || ''")
            text = evaluate(cdp, "(document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 240)")
            raise SystemExit(f"Could not find the Free plan ({title} | {url} | {text})")
        pause = random.uniform(8, 12)
        note(f"Free plan is not on the page yet. Waiting {pause:.1f}s and opening it again ({attempt}/3)")
        widen_signup_window(cdp, "", _PROFILE_ID)
        time.sleep(pause)
        try:
            cdp.call("Page.reload", {"ignoreCache": True}, timeout=30)
        except (RuntimeError, TimeoutError) as exc:
            note(f"Could not reload the signup page: {exc}")
        time.sleep(random.uniform(2, 4))


def username_taken(cdp: Cdp) -> bool:
    return bool(
        evaluate(
            cdp,
            "(() => {"
            " const spans = [...document.querySelectorAll('span')];"
            " return spans.some((el) => {"
            "  const text = (el.textContent || '').replace(/\\s+/g, ' ').trim().replace(/\\.$/, '');"
            "  return text === 'Benutzername wird bereits verwendet' || text === 'Username already used';"
            " });"
            "})()",
        )
    )


def stop_if_username_taken(cdp: Cdp) -> None:
    if username_taken(cdp):
        note("Benutzername wird bereits verwendet. Stopped.")
        raise SystemExit("Benutzername wird bereits verwendet.")


_SYMBOL_KEYS = {
    " ": (" ", "Space", 32, 0),
    "-": ("-", "Minus", 189, 0),
    "_": ("_", "Minus", 189, 8),
    "=": ("=", "Equal", 187, 0),
    "+": ("+", "Equal", 187, 8),
    "[": ("[", "BracketLeft", 219, 0),
    "{": ("{", "BracketLeft", 219, 8),
    "]": ("]", "BracketRight", 221, 0),
    "}": ("}", "BracketRight", 221, 8),
    "\\": ("\\", "Backslash", 220, 0),
    "|": ("|", "Backslash", 220, 8),
    ";": (";", "Semicolon", 186, 0),
    ":": (":", "Semicolon", 186, 8),
    "'": ("'", "Quote", 222, 0),
    '"': ('"', "Quote", 222, 8),
    ",": (",", "Comma", 188, 0),
    "<": ("<", "Comma", 188, 8),
    ".": (".", "Period", 190, 0),
    ">": (">", "Period", 190, 8),
    "/": ("/", "Slash", 191, 0),
    "?": ("?", "Slash", 191, 8),
    "`": ("`", "Backquote", 192, 0),
    "~": ("~", "Backquote", 192, 8),
    "!": ("!", "Digit1", 49, 8),
    "@": ("@", "Digit2", 50, 8),
    "#": ("#", "Digit3", 51, 8),
    "$": ("$", "Digit4", 52, 8),
    "%": ("%", "Digit5", 53, 8),
    "^": ("^", "Digit6", 54, 8),
    "&": ("&", "Digit7", 55, 8),
    "*": ("*", "Digit8", 56, 8),
    "(": ("(", "Digit9", 57, 8),
    ")": (")", "Digit0", 48, 8),
}


def key_spec(char: str) -> tuple[str, str, int, int] | None:
    if len(char) != 1:
        return None
    if char.isalpha():
        return char, f"Key{char.upper()}", ord(char.upper()), 8 if char.isupper() else 0
    if char.isdigit():
        return char, f"Digit{char}", ord(char), 0
    return _SYMBOL_KEYS.get(char)


def press_key(cdp: Cdp, key: str, code: str, vk: int, modifiers: int = 0, text: str = "", session_id: str | None = None) -> None:
    down = {
        "type": "keyDown",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": modifiers,
    }
    cdp.call("Input.dispatchKeyEvent", down, session_id=session_id)
    if text:
        cdp.call(
            "Input.dispatchKeyEvent",
            {
                "type": "char",
                "key": key,
                "code": code,
                "text": text,
                "unmodifiedText": text,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "modifiers": modifiers,
            },
            session_id=session_id,
        )
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        },
        session_id=session_id,
    )


def human_pause(longer: bool = False) -> None:
    if longer or random.random() < 0.08:
        time.sleep(random.uniform(0.18, 0.42))
        return
    time.sleep(random.uniform(0.05, 0.16))


def main_field_point(cdp: Cdp, selector: str) -> dict | None:
    point = evaluate(
        cdp,
        "(() => {"
        f" const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return null;"
        " if (el.closest('[aria-hidden=\"true\"], .visibility-hidden')) return null;"
        " let r = el.getBoundingClientRect();"
        " const onScreen = r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;"
        " if (!onScreen) {"
        "  el.scrollIntoView({block: 'center', inline: 'nearest'});"
        "  r = el.getBoundingClientRect();"
        " }"
        " if (r.width < 2 || r.height < 2) return null;"
        " if (r.bottom < 0 || r.right < 0 || r.left >= window.innerWidth || r.top >= window.innerHeight) return null;"
        " return {x: r.left + Math.min(28, r.width / 2), y: r.top + r.height / 2};"
        "})()",
    )
    if not isinstance(point, dict):
        return None
    return {"x": float(point["x"]), "y": float(point["y"]), "frame_id": ""}


_LAST_USERNAME_SCROLL = 0.0


def reveal_username(cdp: Cdp) -> str:
    """Bring the username box into view once. Later checks leave the page where it is."""
    global _LAST_USERNAME_SCROLL
    allow = time.time() - _LAST_USERNAME_SCROLL >= 1.5
    state = evaluate(
        cdp,
        "(() => {"
        f" const allow = {json.dumps(allow)};"
        " const roots = [];"
        " const visit = (node) => {"
        "  if (!node || !node.querySelectorAll) return;"
        "  roots.push(node);"
        "  for (const el of node.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " visit(document);"
        " const hidden = (el) => !!(el.closest && el.closest('[aria-hidden=\"true\"], .visibility-hidden'));"
        " const visible = (el) => {"
        "  const r = el.getBoundingClientRect();"
        "  return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;"
        " };"
        " let target = null;"
        " for (const root of roots) {"
        "  target = [...root.querySelectorAll('iframe')].find((el) => /name=email/i.test((el.src || '') + ' ' + (el.name || '') + ' ' + (el.title || '') + ' ' + (el.id || '')));"
        "  if (target) break;"
        " }"
        " if (!target) {"
        "  for (const root of roots) {"
        "   target = [...root.querySelectorAll('#username')].find((el) => !hidden(el) && el.getBoundingClientRect().width > 2);"
        "   if (target) break;"
        "  }"
        " }"
        " if (target && visible(target)) return 'ready';"
        " if (!allow) return target ? 'moved' : 'down';"
        " if (!target) {"
        "  const scroller = document.scrollingElement || document.documentElement;"
        "  const before = scroller.scrollTop;"
        "  scroller.scrollBy(0, Math.round((window.innerHeight || 800) * 0.45));"
        "  return scroller.scrollTop > before + 2 ? 'down' : '';"
        " }"
        " target.scrollIntoView({block: 'center', inline: 'nearest'});"
        " return visible(target) ? 'ready' : 'moved';"
        "})()",
    )
    if state in {"down", "moved"}:
        _LAST_USERNAME_SCROLL = time.time()
    return str(state or "")


def email_challenge_point(cdp: Cdp) -> dict | None:
    frame_id = ""
    for frame in child_frames(cdp):
        url = str(frame.get("url") or "")
        if "name=email" not in url.lower():
            continue
        frame_id = str(frame.get("id") or "")
        break
    if not frame_id:
        return None
    origin = evaluate(
        cdp,
        "(() => {"
        " const roots = [];"
        " const visit = (node) => {"
        "  if (!node || !node.querySelectorAll) return;"
        "  roots.push(node);"
        "  for (const el of node.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);"
        " };"
        " visit(document);"
        " let frame = null;"
        " for (const root of roots) {"
        "  frame = [...root.querySelectorAll('iframe')].find((el) => /name=email/i.test(el.src || ''));"
        "  if (frame) break;"
        " }"
        " if (!frame) return null;"
        " const r = frame.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2) return null;"
        " if (r.bottom < 0 || r.top >= window.innerHeight || r.right < 0 || r.left >= window.innerWidth) return null;"
        " return {left: r.left, top: r.top};"
        "})()",
    )
    local = eval_in_frame(
        cdp,
        frame_id,
        "(() => {"
        " const el = document.querySelector('#username');"
        " if (!el) return null;"
        " const r = el.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2) return null;"
        " return {x: r.left + Math.min(28, r.width / 2), y: r.top + r.height / 2, value: el.value || '', focused: document.activeElement === el};"
        "})()",
    )
    if not isinstance(origin, dict) or not isinstance(local, dict):
        return None
    return {
        "x": float(origin["left"]) + float(local["x"]),
        "y": float(origin["top"]) + float(local["y"]),
        "frame_id": frame_id,
        "value": str(local.get("value") or ""),
        "focused": bool(local.get("focused")),
    }


def field_target(cdp: Cdp, selector: str) -> dict | None:
    point = main_field_point(cdp, selector)
    if point:
        return point
    if selector == "#username":
        return email_challenge_point(cdp)
    return None


_FRAME_SESSIONS: dict[str, str] = {}


def frame_session(cdp: Cdp, url_part: str) -> str:
    try:
        cdp.call("Target.setDiscoverTargets", {"discover": True})
        payload = cdp.call("Target.getTargets")
    except (RuntimeError, TimeoutError):
        return ""
    for info in payload.get("targetInfos") or []:
        url = str(info.get("url") or "").lower()
        target_id = str(info.get("targetId") or "")
        if not target_id or url_part not in url:
            continue
        known = _FRAME_SESSIONS.get(target_id)
        if known:
            return known
        try:
            attached = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        except (RuntimeError, TimeoutError):
            continue
        session_id = str(attached.get("sessionId") or "")
        if session_id:
            _FRAME_SESSIONS[target_id] = session_id
            return session_id
    return ""


def focus_node(cdp: Cdp, selector: str, session_id: str) -> bool:
    try:
        cdp.call("DOM.enable", session_id=session_id)
        document = cdp.call("DOM.getDocument", {"depth": 1, "pierce": True}, session_id=session_id)
        root = (document.get("root") or {}).get("nodeId")
        if not root:
            return False
        found = cdp.call("DOM.querySelector", {"nodeId": root, "selector": selector}, session_id=session_id)
        node_id = found.get("nodeId")
        if not node_id:
            return False
        cdp.call("DOM.focus", {"nodeId": node_id}, session_id=session_id)
        return True
    except (RuntimeError, TimeoutError):
        return False


def field_state(cdp: Cdp, selector: str, frame_id: str, session_id: str = "") -> tuple[str, bool]:
    if session_id:
        state = evaluate(
            cdp,
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return null;"
            " return {value: el.value || '', focused: document.activeElement === el};"
            "})()",
            session_id=session_id,
        )
        if not isinstance(state, dict):
            return "", False
        return str(state.get("value") or ""), bool(state.get("focused"))
    if frame_id:
        state = eval_in_frame(
            cdp,
            frame_id,
            "(() => {"
            f" const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return null;"
            " return {value: el.value || '', focused: document.activeElement === el};"
            "})()",
        )
        if not isinstance(state, dict):
            return "", False
        return str(state.get("value") or ""), bool(state.get("focused"))
    return field_value(cdp, selector), bool(
        evaluate(cdp, f"document.activeElement === document.querySelector({json.dumps(selector)})")
    )


def focus_field(cdp: Cdp, selector: str) -> dict:
    deadline = time.time() + 20
    announced = False
    while time.time() < deadline:
        if selector == "#username":
            state = reveal_username(cdp)
            if state in {"down", "moved"} and not announced:
                note("Scrolling to the username field")
                announced = True
                time.sleep(0.4)
        session_id = frame_session(cdp, "name=email") if selector == "#username" else ""
        target = field_target(cdp, selector)
        if target:
            mouse_click(cdp, float(target["x"]), float(target["y"]))
            time.sleep(random.uniform(0.12, 0.25))
        if session_id and focus_node(cdp, selector, session_id):
            return {"frame_id": "", "session_id": session_id, "x": 0, "y": 0}
        if target:
            _value, focused = field_state(cdp, selector, str(target.get("frame_id") or ""), session_id)
            if focused:
                target["session_id"] = session_id
                return target
        time.sleep(0.4)
    raise SystemExit(f"Could not focus {selector}")


def fill_input(cdp: Cdp, selector: str, text: str) -> None:
    """Click the visible field and type one character at a time."""
    deadline = time.time() + 20
    while time.time() < deadline and not field_target(cdp, selector) and not (selector == "#username" and frame_session(cdp, "name=email")):
        time.sleep(0.3)
    target = focus_field(cdp, selector)
    frame_id = str(target.get("frame_id") or "")
    session_id = str(target.get("session_id") or "") or None
    existing, _focused = field_state(cdp, selector, frame_id, session_id or "")
    if existing == text:
        clear_selection(cdp)
        return
    press_key(cdp, "End", "End", 35, session_id=session_id)
    time.sleep(random.uniform(0.05, 0.12))
    for _ in range(len(existing) + 2):
        press_key(cdp, "Backspace", "Backspace", 8, session_id=session_id)
        time.sleep(random.uniform(0.03, 0.08))
        current, _focused = field_state(cdp, selector, frame_id, session_id or "")
        if not current:
            break
    for char in text:
        spec = key_spec(char)
        if spec is None:
            cdp.call("Input.insertText", {"text": char}, session_id=session_id)
        else:
            key, code, vk, modifiers = spec
            press_key(cdp, key, code, vk, modifiers, char, session_id)
        human_pause()
    clear_selection(cdp)
    typed, _focused = field_state(cdp, selector, frame_id, session_id or "")
    if typed != text:
        raise SystemExit(f"{selector} was not filled")


def wait_for_username(cdp: Cdp) -> None:
    deadline = time.time() + 25
    announced = False
    while time.time() < deadline:
        state = reveal_username(cdp)
        if state == "ready" or email_challenge_point(cdp):
            return
        if state in {"down", "moved"} and not announced:
            note("Scrolling to the username field")
            announced = True
        time.sleep(0.4)
    raise SystemExit("Could not find the username field")


def _email_frame(cdp: Cdp) -> tuple[str, dict | None]:
    frame_id = ""
    for frame in child_frames(cdp):
        if "name=email" in str(frame.get("url") or "").lower():
            frame_id = str(frame.get("id") or "")
            break
    if not frame_id:
        return "", None
    origin = evaluate(
        cdp,
        "(() => {"
        " const frame = [...document.querySelectorAll('iframe')].find((el) => /name=email/i.test(el.src || ''));"
        " if (!frame) return null;"
        " const r = frame.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top >= window.innerHeight) return null;"
        " return {left: r.left, top: r.top};"
        "})()",
    )
    return frame_id, origin if isinstance(origin, dict) else None


def _domain_choice(domain: str) -> str:
    return (
        "(() => {"
        f" const wanted = {json.dumps('@' + domain)};"
        " const button = [...document.querySelectorAll('[role=\"dialog\"] button, li button')].find((el) => {"
        "  const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();"
        "  return text === wanted;"
        " });"
        " if (!button) return null;"
        " const r = button.getBoundingClientRect();"
        " if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > window.innerHeight) return null;"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()"
    )


def select_domain(cdp: Cdp, domain: str) -> None:
    """Open the domain chevron beside the username and choose the requested ending."""
    deadline = time.time() + 20
    while time.time() < deadline:
        reveal_username(cdp)
        frame_id, origin = _email_frame(cdp)
        if not frame_id or not origin:
            time.sleep(0.4)
            continue
        state = eval_in_frame(
            cdp,
            frame_id,
            "(() => {"
            " const button = document.querySelector('#select-domain');"
            " if (!button) return null;"
            " const r = button.getBoundingClientRect();"
            " return {"
            "  text: (button.innerText || '').replace(/\\s+/g, ' ').trim().replace(/^@/, '').toLowerCase(),"
            "  expanded: button.getAttribute('aria-expanded') === 'true',"
            "  x: r.right - 8,"
            "  y: r.top + r.height / 2"
            " };"
            "})()",
        )
        if not isinstance(state, dict):
            time.sleep(0.4)
            continue
        if state.get("text") == domain and not state.get("expanded"):
            note(f"Selected @{domain}")
            return
        if not state.get("expanded"):
            mouse_click_in(cdp, float(origin["left"]) + float(state["x"]), float(origin["top"]) + float(state["y"]))
            clear_selection(cdp)
            time.sleep(0.45)
            continue
        choice = evaluate(cdp, _domain_choice(domain))
        if isinstance(choice, dict):
            mouse_click_in(cdp, float(choice["x"]), float(choice["y"]))
            clear_selection(cdp)
            time.sleep(0.45)
            continue
        time.sleep(0.4)
    raise SystemExit(f"Could not select @{domain}")


def enter_username(cdp: Cdp, username: str, domain: str = "proton.me") -> None:
    wait_for_username(cdp)
    select_domain(cdp, domain)
    fill_input(cdp, "#username", username)
    press_key(cdp, "Tab", "Tab", 9)
    clear_selection(cdp)
    time.sleep(random.uniform(0.3, 0.6))
    deadline = time.time() + 8
    while time.time() < deadline:
        stop_if_username_taken(cdp)
        time.sleep(0.4)
    note(f"Entered username {username}")


def enter_password(cdp: Cdp, password: str) -> None:
    deadline = time.time() + 20
    while time.time() < deadline and not evaluate(cdp, "!!document.querySelector('#password')"):
        stop_if_username_taken(cdp)
        time.sleep(0.4)
    if not evaluate(cdp, "!!document.querySelector('#password')"):
        raise SystemExit("Could not find the password field")
    fill_input(cdp, "#password", password)
    time.sleep(random.uniform(0.25, 0.6))
    confirm_deadline = time.time() + 5
    while time.time() < confirm_deadline and not evaluate(cdp, "!!document.querySelector('#password-confirm')"):
        time.sleep(0.3)
    if evaluate(cdp, "!!document.querySelector('#password-confirm')"):
        fill_input(cdp, "#password-confirm", password)
    else:
        note("No confirm-password field on this form")
    clear_selection(cdp)
    dismiss_mail_plus(cdp)
    open_captcha(cdp)


def click_labeled_button(cdp: Cdp, pattern: str) -> bool:
    clicked = evaluate(
        cdp,
        "(() => {"
        f" const pattern = {json.dumps(pattern)};"
        " const button = [...document.querySelectorAll('button')].find((el) => {"
        "  const label = (el.innerText || '').replace(/\\s+/g, ' ').trim();"
        "  if (!new RegExp(pattern, 'i').test(label) || el.disabled) return false;"
        "  const r = el.getBoundingClientRect();"
        "  return r.width > 2 && r.height > 2;"
        " });"
        " if (!button) return false;"
        " button.scrollIntoView({block: 'center', inline: 'center'});"
        " button.click();"
        " const sel = window.getSelection && window.getSelection();"
        " if (sel) sel.removeAllRanges();"
        " return (button.innerText || '').replace(/\\s+/g, ' ').trim();"
        "})()",
    )
    if not clicked:
        return False
    note(f"Clicked {clicked}")
    return True


def click_create_account(cdp: Cdp) -> bool:
    box = evaluate(
        cdp,
        "(() => {"
        " const pattern = /^(beginne\\b|create account|jetzt (registrieren|starten)|continue|weiter)$|proton mail jetzt zu verwenden/i;"
        " const button = [...document.querySelectorAll('button')].find((el) => {"
        "  const label = (el.innerText || '').replace(/\\s+/g, ' ').trim();"
        "  if (!pattern.test(label) || el.disabled) return false;"
        "  const r = el.getBoundingClientRect();"
        "  return r.width > 2 && r.height > 2;"
        " });"
        " if (!button) return null;"
        " button.scrollIntoView({block: 'center', inline: 'center'});"
        " const r = button.getBoundingClientRect();"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2, label: (button.innerText || '').replace(/\\s+/g, ' ').trim()};"
        "})()",
    )
    if not isinstance(box, dict):
        return False
    mouse_click(cdp, float(box["x"]), float(box["y"]))
    clear_selection(cdp)
    note(f"Clicked {box.get('label') or 'create account'}")
    return True


def click_continue(cdp: Cdp) -> bool:
    box = evaluate(
        cdp,
        "(() => {"
        " const pattern = /^continue$|^weiter$|^fortfahren$/i;"
        " const button = [...document.querySelectorAll('button')].find((el) => {"
        "  const label = (el.innerText || '').replace(/\\s+/g, ' ').trim();"
        "  if (!pattern.test(label) || el.disabled || el.getAttribute('aria-busy') === 'true') return false;"
        "  const r = el.getBoundingClientRect();"
        "  return r.width > 2 && r.height > 2;"
        " });"
        " if (!button) return null;"
        " button.scrollIntoView({block: 'center', inline: 'center'});"
        " const r = button.getBoundingClientRect();"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2, label: (button.innerText || '').replace(/\\s+/g, ' ').trim()};"
        "})()",
    )
    if not isinstance(box, dict):
        return False
    mouse_click(cdp, float(box["x"]), float(box["y"]))
    clear_selection(cdp)
    note(f"Clicked {box.get('label') or 'continue'}")
    return True


def accept_recovery_notice(cdp: Cdp) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        if evaluate(cdp, "!!document.querySelector('#displayName')") and not evaluate(cdp, "!!document.querySelector('#understood-recovery-necessity')"):
            return
        checked = evaluate(
            cdp,
            "(() => {"
            " const input = document.querySelector('#understood-recovery-necessity');"
            " if (!input) return 'missing';"
            " if (input.checked) return 'checked';"
            " const label = input.closest('label') || document.querySelector('label[for=\"understood-recovery-necessity\"]') || input;"
            " label.scrollIntoView({block: 'center', inline: 'center'});"
            " const r = input.getBoundingClientRect();"
            " const box = r.width > 2 && r.height > 2 ? r : label.getBoundingClientRect();"
            " input.click();"
            " return {x: box.left + Math.max(box.width, 4) / 2, y: box.top + Math.max(box.height, 4) / 2, checked: input.checked};"
            "})()",
        )
        if isinstance(checked, dict) and not checked.get("checked"):
            mouse_click(cdp, float(checked["x"]), float(checked["y"]))
            note("Checked the recovery notice")
            time.sleep(0.3)
        elif isinstance(checked, dict):
            note("Checked the recovery notice")
        if evaluate(cdp, "!!(document.querySelector('#understood-recovery-necessity') && document.querySelector('#understood-recovery-necessity').checked)"):
            if click_continue(cdp):
                time.sleep(1)
                continue
        time.sleep(0.4)


def enter_display_name(cdp: Cdp, display_name: str) -> None:
    deadline = time.time() + 25
    while time.time() < deadline and not evaluate(cdp, "!!document.querySelector('#displayName')"):
        time.sleep(0.4)
    if not evaluate(cdp, "!!document.querySelector('#displayName')"):
        raise SystemExit("Could not find the display name field")
    fill_input(cdp, "#displayName", display_name)
    note(f"Entered display name {display_name}")
    deadline = time.time() + 20
    last_click = 0.0
    while time.time() < deadline:
        still = evaluate(cdp, "!!document.querySelector('#displayName')")
        moved = evaluate(
            cdp,
            "(() => {"
            " const text = document.body ? document.body.innerText : '';"
            " return /maybe later|später|recovery phone|recovery email|your account is ready|inbox|posteingang/i.test(text);"
            "})()",
        )
        if moved or not still:
            return
        if time.time() - last_click > 1.5:
            if not click_continue(cdp):
                press_enter(cdp)
                note("Pressed Enter")
            last_click = time.time()
        time.sleep(0.4)
    raise SystemExit("Display name was not accepted")


def chosen_email(cdp: Cdp, username: str, domain: str) -> str:
    found = evaluate(
        cdp,
        "(() => {"
        " const text = document.body ? document.body.innerText : '';"
        " const match = text.match(/[A-Za-z0-9._-]+@(?:proton\\.me|protonmail\\.com|proton\\.com|pm\\.me)/i);"
        " return match ? match[0] : '';"
        "})()",
    )
    if found:
        return str(found)
    return f"{username}@{domain}"


def save_proton_account(email: str, password: str) -> None:
    with _accounts_lock:
        accounts: list[dict] = []
        if ACCOUNTS_FILE.is_file():
            try:
                raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup = park_broken(ACCOUNTS_FILE)
                if backup is None:
                    raise SystemExit(f"Could not read {ACCOUNTS_FILE.name}, so the new mailbox was not written over it") from exc
                note(f"Could not read saved mailboxes. Kept the old file as {backup.name}.")
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
                item.pop("suggested", None)
                replaced = True
                break
        if not replaced:
            accounts.append({"email": email, "password": password})
        write_text_atomic(ACCOUNTS_FILE, json.dumps(accounts, indent=2))
    note(f"Saved {email} to {ACCOUNTS_FILE.name}")


def fill_proton(cdp: Cdp, username: str, password: str, display_name: str, domain: str = "proton.me") -> str:
    click_free_plan(cdp)
    enter_username(cdp, username, domain)
    enter_password(cdp, password)
    solve_captcha(cdp)
    email = f"{username}@{domain}"
    save_proton_account(email, password)
    accept_recovery_notice(cdp)
    enter_display_name(cdp, display_name)
    email = chosen_email(cdp, username, domain)
    if email.lower() != f"{username}@{domain}".lower():
        save_proton_account(email, password)
    note(f"Proton mailbox ready: {email}")
    return email


def ask(value: str | None, label: str) -> str:
    text = (value or "").strip()
    if text:
        return text
    if sys.stdin.isatty():
        text = input(f"{label}: ").strip()
    if not text:
        raise SystemExit(f"Missing {label}")
    return text


def another_proxy(path: Path, current: dict) -> dict:
    picked = current
    for _ in range(8):
        picked = load_random_proxy(path)
        same = (
            picked["host"] == current["host"]
            and picked["port"] == current["port"]
            and picked["password"] == current["password"]
        )
        if not same:
            return picked
    return picked


def replace_proxy(
    session: requests.Session,
    api: DolphinAPI,
    profile_id: str,
    proxies_path: Path,
    proxy_type: str,
    skip_check: bool,
    current: dict,
) -> dict:
    proxy = another_proxy(proxies_path, current)
    resolved = resolve_proxy_type(session, proxy, proxy_type, skip_check)
    note(f"Updating proxy to {proxy_label(proxy, resolved)}")
    payload = api.edit_profile(profile_id, {"proxy": proxy_body(proxy, resolved)})
    if isinstance(payload, dict) and payload.get("success") is False:
        message = payload.get("error") or payload.get("message") or payload
        raise RuntimeError(f"Dolphin did not update the proxy: {message}")
    note("Updated the proxy")
    return proxy


_PROFILE_ID = ""


def profile_hwnd(profile_id: str) -> int | None:
    if os.name != "nt" or not profile_id:
        return None
    import subprocess

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
        return None
    pids = {int(line) for line in completed.stdout.splitlines() if line.strip().isdigit()}
    if not pids:
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    chosen: list[tuple[int, int]] = []

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        if area > 0:
            chosen.append((area, int(hwnd)))
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(enum_proc, 0)
    if not chosen:
        return None
    return max(chosen)[1]


def hide_dolphin_app() -> None:
    """The signup browser is the only window. Dolphin Anty stays running so the local API still works."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == "Dolphin{anty}":
            user32.ShowWindow(hwnd, 0)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(enum_proc, 0)


def widen_signup_window(cdp: Cdp, target_id: str, profile_id: str) -> None:
    """Fill the screen. Bringing the window forward restores it, so size it again from Windows."""
    if os.name == "nt" and profile_id:
        import ctypes
        from ctypes import wintypes

        hwnd = profile_hwnd(profile_id)
        if hwnd:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            user32.ShowWindow(hwnd, 3)
            work = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                user32.SetWindowPos(
                    hwnd,
                    0,
                    int(work.left),
                    int(work.top),
                    int(work.right - work.left),
                    int(work.bottom - work.top),
                    0x0040,
                )
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    hide_dolphin_app()
    params = {"targetId": target_id} if target_id else {}
    try:
        info = cdp.call("Browser.getWindowForTarget", params)
        window_id = info.get("windowId")
        size = evaluate(cdp, "({w: screen.availWidth || 1920, h: screen.availHeight || 1080})")
        width = int(size.get("w") or 1920) if isinstance(size, dict) else 1920
        height = int(size.get("h") or 1080) if isinstance(size, dict) else 1080
        if window_id is not None:
            cdp.call(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"left": 0, "top": 0, "width": width, "height": height, "windowState": "normal"},
                },
            )
            cdp.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
        note("Opened the browser at full width")
    except (RuntimeError, TimeoutError, OSError) as exc:
        note(f"Could not maximize the browser: {exc}")


def open_browser(
    session: requests.Session,
    profile_id: str,
    profile: str,
    headless: bool,
) -> tuple[CdpSocket, Cdp, str]:
    global _PROFILE_ID
    _PROFILE_ID = profile_id
    automation = start_profile(session, profile_id, headless)
    port = int(automation["port"])
    note(f"Started {profile} on port {port}")
    ws, target_id = connect_page(port)
    cdp = Cdp(ws)
    if target_id:
        try:
            cdp.call("Target.activateTarget", {"targetId": target_id})
        except (RuntimeError, TimeoutError) as exc:
            note(f"Could not focus the tab: {exc}")
    if not headless:
        widen_signup_window(cdp, target_id, profile_id)
    return ws, cdp, target_id


def generate_mailbox(
    profile: str,
    email: str,
    password: str,
    display_name: str = "",
    log=None,
    stop=None,
    proxies: Path | None = None,
    proxy_type: str = "http",
    headless: bool = False,
    skip_check: bool = True,
    url: str = "",
) -> str:
    """Open Proton signup for this address. No SMS step. Returns the created address."""
    previous_log = getattr(_HOOKS, "log", None)
    previous_stop = getattr(_HOOKS, "stop", None)
    if log is not None:
        _HOOKS.log = log
    if stop is not None:
        _HOOKS.stop = stop
    try:
        return _run_signup(profile, email, password, display_name, proxies, proxy_type, headless, skip_check, url)
    finally:
        _HOOKS.log = previous_log
        _HOOKS.stop = previous_stop


def _run_signup(
    profile: str,
    email: str,
    password: str,
    display_name: str,
    proxies: Path | None,
    proxy_type: str,
    headless: bool,
    skip_check: bool,
    url: str,
) -> str:
    profile = normalize_profile_name(profile)
    username, domain = proton_address(email)
    password = (password or "").strip()
    if not password:
        raise SystemExit("Missing password")
    display_name = (display_name or "").strip() or username
    page = signup_page(domain, url)
    proxy_path = proxies or (ROOT / "proxies.txt")
    proxy = load_random_proxy(proxy_path)
    session = requests.Session()
    local_login(session)
    resolved = resolve_proxy_type(session, proxy, proxy_type, skip_check)
    note(f"Using {proxy_label(proxy, resolved)}")

    api = DolphinAPI(collect_garbage_on_init=False)
    profile_id = create_profile(api, profile, proxy, resolved)
    ws: CdpSocket | None = None
    cdp: Cdp | None = None
    try:
        ws, cdp, _target_id = open_browser(session, profile_id, profile, headless)
        navigate(cdp, page)
        created = ""
        for restart in range(SIGNUP_RESTARTS):
            try:
                created = fill_proton(cdp, username, password, display_name, domain)
                break
            except SignupRestart:
                if restart == SIGNUP_RESTARTS - 1:
                    raise SystemExit("Captcha did not load. Stopped after 3 restarts.")
                note(f"Restarting signup ({restart + 2}/{SIGNUP_RESTARTS})")
                try:
                    ws.close()
                except OSError:
                    pass
                ws = None
                stop_profile(session, profile_id)
                time.sleep(1)
                proxy = replace_proxy(session, api, profile_id, proxy_path, proxy_type, skip_check, proxy)
                ws, cdp, _target_id = open_browser(session, profile_id, profile, headless)
                navigate(cdp, page)
        if not created:
            raise SystemExit("Proton signup did not finish")
    except BaseException:
        try:
            finish_profile(session, api, profile_id, cdp, ws)
        except Exception as exc:
            _PRINT(f"Could not close the browser: {exc}")
        raise
    finish_profile(session, api, profile_id, cdp, ws)
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Dolphin profile, open TARGET_URL2/signup, and create a Proton mailbox.")
    parser.add_argument("profile", nargs="?", help="Name for the Dolphin profile")
    parser.add_argument("--username", help="Proton username")
    parser.add_argument("--password", help="Password for the new mailbox")
    parser.add_argument("--display-name", help="Display name. Defaults to the username")
    parser.add_argument("--url", default="", help="Signup page. Default is TARGET_URL2/signup")
    parser.add_argument("--proxies", default=str(ROOT / "proxies.txt"), help="Proxy list, one user:pass@host:port per line")
    parser.add_argument("--proxy-type", choices=("http", "https", "socks5", "socks4"), default="http")
    parser.add_argument("--headless", action="store_true", help="Ask Dolphin to start headless")
    parser.add_argument("--skip-check", action="store_true", help="Do not ask Dolphin to test the proxy first")
    args = parser.parse_args()
    if not args.profile:
        if sys.stdin.isatty():
            args.profile = input("Dolphin profile name: ")
        if not args.profile:
            raise SystemExit("Pass the profile name: python proton_gen.py reymark0227 --username reymark0227 --password 'your password'")
    profile = normalize_profile_name(args.profile)
    username, domain = proton_address(ask(args.username or args.profile, "Username"))
    password = ask(args.password, "Password")
    display_name = (args.display_name or "").strip() or username
    generate_mailbox(
        profile,
        f"{username}@{domain}",
        password,
        display_name,
        proxies=Path(args.proxies),
        proxy_type=args.proxy_type,
        headless=args.headless,
        skip_check=args.skip_check,
        url=args.url,
    )


if __name__ == "__main__":
    main()

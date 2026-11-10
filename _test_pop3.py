"""Sign in with a saved or passed mailbox and turn on POP3/IMAP.

    python _test_pop3.py neumann.ben86@web.de
    python _test_pop3.py neumann.ben86@web.de "the password"
"""

import argparse
import imaplib
import json
import time

from gmx_gen import (
    ACCOUNTS_FILE,
    Cdp,
    DolphinAPI,
    ROOT,
    accept_cookies,
    child_frames,
    click_labeled,
    connect_page,
    create_profile,
    element_box,
    enable_mailbox_access,
    eval_in_frame,
    evaluate,
    finish_profile,
    load_random_proxy,
    local_login,
    maximize_window,
    navigate,
    page_url,
    proxy_label,
    requests,
    resolve_proxy_type,
    show_profile_window,
    start_profile,
    type_into,
)

USERNAME_FIELDS = (
    "#input-username",
    "#username",
    "input[name='username']",
    "input[autocomplete='username']",
    "input[type='email']",
)
PASSWORD_FIELDS = (
    "#input-password",
    "#password",
    "input[name='password']",
    "input[type='password']",
    "#freemailLoginPassword",
)
CONSENT_CLICK_JS = (
    "(() => {"
    " const all = [];"
    " const visit = (root) => {"
    " for (const el of root.querySelectorAll?.('button, a, [role=button]') || []) all.push(el);"
    " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
    " };"
    " visit(document);"
    " const norm = (el) => ((el.innerText || el.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim();"
    " const visible = all.filter((el) => { const r = el.getBoundingClientRect(); return r.width > 20 && r.height > 16; });"
    " const wanted = (text) => {"
    " const t = text.toLowerCase();"
    " return t === 'akzeptieren und weiter' || t === 'alle akzeptieren' || t === 'zustimmen und weiter'"
    " || t === 'einverstanden' || t.includes('weiter zur homepage') || t.includes('weiter zur web.de');"
    " };"
    " const el = visible.find((item) => wanted(norm(item)));"
    " if (!el) return visible.map(norm).filter(Boolean).slice(0, 8).join(' | ');"
    " el.click();"
    " return 'clicked ' + norm(el);"
    "})()"
)
CLICK_LOGIN_JS = (
    "(() => {"
    " const all = [];"
    " const visit = (root) => {"
    " for (const el of root.querySelectorAll?.('button, a, [role=button]') || []) all.push(el);"
    " for (const el of root.querySelectorAll?.('*') || []) if (el.shadowRoot) visit(el.shadowRoot);"
    " };"
    " visit(document);"
    " const el = all.find((item) => {"
    " const text = ((item.innerText || item.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim();"
    " if (!/^(login|anmelden|einloggen)$/i.test(text)) return false;"
    " const r = item.getBoundingClientRect();"
    " return r.width > 20 && r.height > 20;"
    " });"
    " if (!el) return false;"
    " el.click();"
    " return true;"
    "})()"
)


def credentials() -> tuple[str, str]:
    parser = argparse.ArgumentParser(description="Sign in and turn on POP3/IMAP for one mailbox.")
    parser.add_argument("email", help="Mailbox address, for example name@web.de")
    parser.add_argument("password", nargs="?", help="Password. Read from accounts.json when omitted.")
    args = parser.parse_args()
    email = args.email.strip()
    if "@" not in email:
        raise SystemExit("Pass the full mailbox address, for example name@web.de")
    if args.password:
        return email, args.password
    if not ACCOUNTS_FILE.is_file():
        raise SystemExit(f"Pass the password: python _test_pop3.py {email} \"password\"")
    saved = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    for item in saved:
        if str(item.get("email") or "").lower() == email.lower():
            password = str(item.get("password") or "")
            if password:
                return email, password
    raise SystemExit(f"{email} is not in accounts.json. Pass the password as the second argument.")


def home_for(email: str) -> str:
    host = email.rsplit("@", 1)[-1].lower()
    if host.startswith("gmx."):
        return "https://www.gmx.net/"
    return "https://web.de/"


def imap_host(email: str) -> str:
    host = email.rsplit("@", 1)[-1].lower()
    if host == "web.de":
        return "imap.web.de"
    if host.startswith("gmx."):
        return "imap.gmx.net"
    return ""


def wait_for(cdp: Cdp, selectors: tuple[str, ...], seconds: float) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for selector in selectors:
            if element_box(cdp, selector):
                return selector
        time.sleep(0.4)
    return ""


def auth_href(cdp: Cdp) -> str:
    raw = evaluate(
        cdp,
        "(() => {"
        " const html = document.documentElement.innerHTML;"
        " const marks = ['https://auth.web.de/login', 'https://auth.gmx.net/login'];"
        " for (const mark of marks) {"
        " const start = html.indexOf(mark);"
        " if (start < 0) continue;"
        " let end = start;"
        " const stop = new Set([String.fromCharCode(34), String.fromCharCode(39), ' ', '<']);"
        " while (end < html.length && !stop.has(html[end])) end += 1;"
        " return html.slice(start, end).replaceAll('&amp;', '&');"
        " }"
        " return '';"
        "})()",
    )
    return str(raw or "")


def consent_open(cdp: Cdp) -> bool:
    return "consent" in page_url(cdp).lower()


def dismiss_consent(cdp: Cdp) -> None:
    if not consent_open(cdp):
        return
    accept_cookies(cdp, timeout=3)
    seen = ""
    targets = [None, *[str(frame.get("id") or "") for frame in child_frames(cdp)]]
    for frame_id in targets:
        try:
            result = evaluate(cdp, CONSENT_CLICK_JS) if frame_id is None else eval_in_frame(cdp, frame_id, CONSENT_CLICK_JS)
        except (RuntimeError, TimeoutError):
            result = None
        if not isinstance(result, str) or not result:
            continue
        if result.startswith("clicked"):
            print(result[:120])
            time.sleep(1)
            return
        seen = seen or result
    if seen:
        print(f"Consent buttons: {seen[:200]}")


def open_login(cdp: Cdp, email: str) -> None:
    navigate(cdp, home_for(email))
    deadline = time.time() + 25
    while time.time() < deadline and consent_open(cdp):
        dismiss_consent(cdp)
        time.sleep(0.6)
    if consent_open(cdp):
        raise SystemExit(f"Could not leave the consent page at {page_url(cdp)}")
    accept_cookies(cdp, timeout=8)
    deadline = time.time() + 20
    while time.time() < deadline:
        if consent_open(cdp):
            dismiss_consent(cdp)
            time.sleep(0.6)
            continue
        if wait_for(cdp, USERNAME_FIELDS, 0.4):
            return
        href = page_url(cdp).lower()
        if "auth.web.de" in href or "auth.gmx.net" in href:
            if wait_for(cdp, USERNAME_FIELDS, 8):
                return
        clicked = False
        try:
            clicked = evaluate(cdp, CLICK_LOGIN_JS) is True
        except (RuntimeError, TimeoutError):
            clicked = False
        if clicked:
            print("Clicked Login")
            time.sleep(1)
            continue
        link = auth_href(cdp)
        if link:
            print("Opening the login page")
            navigate(cdp, link)
        time.sleep(0.6)
    raise SystemExit(f"Could not find the email field at {page_url(cdp)}")


def login(cdp: Cdp, email: str, password: str) -> None:
    open_login(cdp, email)
    username = wait_for(cdp, USERNAME_FIELDS, 15)
    if not username:
        raise SystemExit(f"Could not find the email field at {page_url(cdp)}")
    type_into(cdp, username, email)
    print("Entered the mailbox address")
    if not click_labeled(cdp, "button, input[type=submit]", "Weiter", timeout=8, required=False):
        click_labeled(cdp, "button", "Login", timeout=8, required=True)
    password_field = wait_for(cdp, PASSWORD_FIELDS, 25)
    if not password_field:
        raise SystemExit(f"Could not find the password field at {page_url(cdp)}")
    type_into(cdp, password_field, password)
    print("Entered the password")
    if not (
        click_labeled(cdp, "button", "Login", timeout=8, required=False)
        or click_labeled(cdp, "button, input[type=submit]", "Einloggen", timeout=4, required=False)
        or click_labeled(cdp, "button, input[type=submit]", "Anmelden", timeout=4, required=False)
    ):
        raise SystemExit("Could not submit the login form")
    deadline = time.time() + 70
    while time.time() < deadline:
        href = page_url(cdp).lower()
        if "navigator" in href and "login" not in href:
            print("Signed in")
            return
        click_labeled(cdp, "button, a", "Nein danke", timeout=0.3, required=False)
        if "obligation." in href:
            click_labeled(cdp, "button", "Weiter", timeout=0.3, required=False)
        accept_cookies(cdp, timeout=1)
        time.sleep(0.6)
    raise SystemExit(f"Mailbox did not open at {page_url(cdp)}")


def confirm_imap(email: str, password: str, log=print) -> None:
    server = imap_host(email)
    if not server:
        log("Skipped the IMAP login check for this domain")
        return
    box = imaplib.IMAP4_SSL(server, 993, timeout=25)
    try:
        box.login(email, password)
    except imaplib.IMAP4.error:
        raise SystemExit(f"The switch is on, but {server} rejected the login") from None
    finally:
        try:
            box.logout()
        except Exception:
            pass
    log(f"IMAP login accepted by {server}")


def profile_name(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in local).strip("-")
    return (cleaned or "mailbox")[:40] + "-pop3"


def enable_existing_mailbox(email: str, password: str, log=print, stop=None) -> None:
    """Sign in to an existing mailbox and turn on POP3/IMAP. Does not create an account."""
    if stop:
        stop()
    session = requests.Session()
    local_login(session)
    proxy = load_random_proxy(ROOT / "proxies.txt")
    resolved = resolve_proxy_type(session, proxy, "http", True)
    log(f"Using {proxy_label(proxy, resolved)}")
    api = DolphinAPI(collect_garbage_on_init=False)
    profile_id = create_profile(api, profile_name(email), proxy, resolved)
    ws = None
    cdp = None
    try:
        automation = start_profile(session, profile_id, False)
        port = int(automation["port"])
        log(f"Started browser on port {port}")
        ws, target_id = connect_page(port)
        cdp = Cdp(ws)
        if target_id:
            try:
                cdp.call("Target.activateTarget", {"targetId": target_id})
            except (RuntimeError, TimeoutError):
                pass
        maximize_window(cdp, target_id)
        show_profile_window(profile_id)
        if stop:
            stop()
        log("Signing in")
        login(cdp, email, password)
        if stop:
            stop()
        log("Turning on POP3/IMAP")
        enable_mailbox_access(cdp)
        confirm_imap(email, password, log=log)
        log(f"POP3/IMAP is on for {email}")
    except BaseException:
        if cdp is not None:
            try:
                print("PAGE", page_url(cdp)[:180])
            except Exception:
                pass
        raise
    finally:
        finish_profile(session, api, profile_id, cdp, ws)


def main() -> None:
    email, password = credentials()
    try:
        enable_existing_mailbox(email, password)
    except BaseException as exc:
        text = str(exc).replace(password, "***")
        raise SystemExit(text) from None


if __name__ == "__main__":
    main()

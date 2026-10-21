# GMX email generator

`gmx_gen.py` creates a [Dolphin Anty](https://dolphin-anty.com/) profile, opens GMX through a random proxy, and walks the registration form. The phone number and SMS code come from an [Anosim](https://anosim.net/) German free-number inbox.

Dolphin Anty must already be running and logged in on this machine. The local API is `http://127.0.0.1:3001`.

## Setup

Python 3.12 or newer. Install the packages the Dolphin client imports:

```powershell
pip install requests chardet
```

Create these local files. All of them are gitignored:

| File | Purpose |
| --- | --- |
| `.env` | GMX start URL and Anosim login |
| `dolphin/apikey.py` | Dolphin cloud API token, as `api_key = "..."` |
| `proxies.txt` | One proxy per line: `user:pass@host:port` |

`.env` example:

```
TARGET_URL=https://www.gmx.net/
ANOSIM_EMAIL=
ANOSIM_PASSWORD=
ANOSIM_MODE=TEST
```

`ANOSIM_MODE=TEST` submits the Anosim number plus one on the GMX form (`+4917616236940` becomes `+4917616236941`) and still reads the SMS from the original number. The 20-use counter is not increased. Remove `ANOSIM_MODE` to submit the real number and count that use.

## Run

```powershell
python gmx_gen.py lena-hoffmann --first-name Lena --last-name Hoffmann --day 12 --month 3 --year 1991 --plz 80331 --city Muenchen --street "Sendlinger Str. 8" --country DE --password "Rote Welle 27?Haus"
```

The first argument is the Dolphin profile name. It does not have to be an email address. Omitted fields are prompted on the keyboard.

| Option | Meaning |
| --- | --- |
| `--first-name`, `--last-name` | 1–40 characters |
| `--day`, `--month`, `--year` | Birth date, age 18 or older |
| `--plz` | Postal code, 1–10 characters |
| `--city` | City, 1–40 characters |
| `--street` | Street and house number, 1–49 characters |
| `--country` | Form country code, default `DE` |
| `--password` | Mailbox password, 1–100 characters. GMX expects at least 12 characters with letters, numbers, and symbols |
| `--url` | Page to open. Default is `TARGET_URL` |
| `--proxies` | Proxy file. Default is `proxies.txt` |
| `--proxy-type` | `http` (default), `https`, `socks5`, or `socks4`. A failed `http` check is retried as `socks5` |
| `--skip-check` | Skip the Dolphin proxy test |
| `--headless` | Ask Dolphin to start headless |

Allowed country codes: `DE BE BG DK EE FI FR GR IE IT HR LV LT LU MT NL PL PT RO SE CH SK SI ES CZ HU GB CY`.

## What the script does

1. Builds a Windows Dolphin profile, attaches a random proxy, and opens the window maximized.
2. Accepts the GMX consent dialog when it is present, then clicks **Registrieren**. If that button is missing, the page is reloaded up to 3 times. It then clicks **Jetzt registrieren**.
3. Fills the name and birth date, waits 3–4 seconds, clicks **I am human**, then **Get started**.
4. Continues through the email, address, and password steps. If GMX says the alias is taken, or **Weiter** stays disabled, the alias gets a short random suffix.
5. Logs into Anosim and opens [the Germany free-number list](https://anosim.net/freenumbers/germany). Archived numbers (`opacity-50`) are ignored. A number already used 20 times, or listed under `banned`, is skipped.
6. Enters the number on GMX and clicks **Weiter**. If GMX opens [Registrierung fehlgeschlagen](https://hilfe.gmx.net/kundenservice/flexreg.html), that number is banned, the script goes back, and another number is used.
7. Refreshes the Anosim inbox and waits 10–15 seconds. Only a GMX message is accepted. Each number gets 3 tries. After that, another number is used.
8. Types the code into the six confirmation fields, clicks **Weiter**, waits 2 seconds, and clicks **Zustimmen und weiter**.

When the signup finishes, the mailbox address and password are appended to `accounts.json`, the browser closes, and the Dolphin profile is deleted.

If a step fails earlier, the browser stays open so the page can be inspected. If no usable phone number remains, the run stops, the browser closes, and the profile is deleted. A failed run does not write `accounts.json`.

## Saved files

`freenumber_usage.json` stores how many times each real Anosim number was submitted, and which numbers GMX rejected:

```json
{
  "+4917616236940": 2,
  "banned": [
    "+4917614119074"
  ]
}
```

`accounts.json` is a list of finished mailboxes:

```json
[
  {
    "email": "lena.hoffmann@gmx.net",
    "password": "Rote Welle 27?Haus"
  }
]
```

The saved address is the `@gmx` address shown on the registration page.

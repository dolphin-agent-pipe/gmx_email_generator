# Mailbox studio

Creates GMX and WEB.DE mailboxes. A number service orders the phone numbers and reads the SMS codes. Dolphin Anty opens the signup page through a proxy, fills the form, and turns on POP3/IMAP.

Dolphin Anty must already be running and logged in on this machine. Its local API is `http://127.0.0.1:3001`.

The usual way to work is the desktop window in `studio.py`. `gmx_gen.py` still creates one mailbox from the command line and reads a code from an Anosim free-number inbox.

## Setup

Python 3.10 or newer on Windows. Chrome or Edge is used by the number services.

```powershell
pip install requests chardet pywebview
```

Create these local files. All of them are gitignored:

| File | Purpose |
| --- | --- |
| `.env` | Signup URLs and number-service logins |
| `dolphin/apikey.py` | Dolphin cloud API token, as `api_key = "..."` |
| `proxies.txt` | One proxy per line |

Proxy lines accept either of these forms:

```
user:pass@host:port
host:port:user:pass
```

`.env` example. Fill in the logins for the services you use. Leave the others blank.

```
TARGET_URL1=https://www.gmx.net/
WEBDE_URL=https://www.web.de/

SERVICE_URL1=https://smscode.gg
SMS_CODE_EMAIL=
SMS_CODE_PASSWORD=
SMS_CODE_ORDER_COUNT=3
SMS_CODE_PORT=8780

SERVICE_URL2=https://hero-sms.com/
HERO_SMS_EMAIL=
HERO_SMS_PASSWORD=
HERO_SMS_ORDER_COUNT=3
HERO_SMS_PORT=8781
HERO_SMS_COUNTRY=germany

SERVICE_URL3=https://anosim.net/
ANOSIM_EMAIL=
ANOSIM_PASSWORD=
ANOSIM_ORDER_COUNT=3
ANOSIM_PORT=8782
ANOSIM_COUNTRY=Germany
```

`ANOSIM_MODE=TEST` applies only to `gmx_gen.py`. It submits the Anosim free number plus one on the form and still reads the SMS from the original number. The 20-use counter is not increased. Remove `ANOSIM_MODE` to submit the real free number and count that use.

Each service orders `*_ORDER_COUNT` numbers per signup, default 3, maximum 10. Studio always asks for 3.

## Run

Start Dolphin Anty, then start the one number service you want to use. Leave that window open.

```powershell
python sms_code_service.py
python hero_sms_service.py
python anosim_service.py
```

Each one signs into its dashboard in its own browser profile and listens locally:

| Service | Script | Port |
| --- | --- | --- |
| SMS code | `sms_code_service.py` | 8780 |
| Hero SMS | `hero_sms_service.py` | 8781 |
| Anosim | `anosim_service.py` | 8782 |

Then open the studio:

```powershell
python studio.py
```

That opens the Mailbox window at `http://127.0.0.1:8790`. A second copy exits if that port is already taken.

## Usage

Import a text file, one person per line. `sample_import.txt` is a template. A header line is ignored. Country is optional and defaults to `DE`.

```
first;last;day;month;year;plz;city;street;password
first;last;day;month;year;plz;city;street;password;country
```

```
Lena;Hoffmann;12;3;1991;80331;Muenchen;Sendlinger Str. 8;Rote Welle 27?Haus
```

Limits: names 1–40 characters, street 1–49, postal code 1–10, password 1–100. The birth date must be 18 or older. GMX and WEB.DE expect a password of at least 12 characters with letters, numbers, and symbols.

In the window:

1. Choose **Service** and **Mailbox** (GMX or WEB.DE). One service is used for the rows you start.
2. Import the file. A line with a missing field is marked failed and shows what is missing.
3. Select the rows and click **Start selected**, or **Start** on one row. The chosen service must already be listening. Ready and failed rows can be started. Done rows stay done.
4. **Cancel** stops a queued or running row. A queued row returns to ready. A running row stops at the next check.
5. **Clear** removes every row that is not done. It waits until nothing is running.
6. **Reload** reads `studio_queue.json` again.
7. **History** lists finished mailboxes. Filter by the day they were created, or search by email or name.

**Reuse phone number** keeps a number that already received a code and offers it again until the rental expires. The count next to the checkbox is how many numbers are still live. Turn it off and new signups order fresh numbers.

While a row runs it orders three numbers, opens a Dolphin profile, and walks the signup form. If GMX or WEB.DE rejects a number, or no SMS arrives, the next number is tried. When all three fail, the open order is closed and the next cheaper price is ordered. After three failed batches the row is marked failed.

A finished mailbox is written to `accounts.json` and to History. The address is the one shown on the signup page, which may differ from `first.last`. The Dolphin profile is then closed and deleted.

If the mailbox is created but POP3/IMAP does not turn on, the row stays on **POP3 failed** and shows **Enable POP3/IMAP**. That signs in again and retries the setting. You can also do that for a saved mailbox:

```powershell
python _test_pop3.py name@web.de
python _test_pop3.py name@web.de "the password"
```

When the password is omitted, it is read from `accounts.json`.

Rows that were running or queued when the window closed come back as ready, with a note to start them again. A POP3 retry that was interrupted stays on POP3 failed.

## One mailbox from the command line

This path does not use the three number services. It logs into Anosim and picks a German free number.

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
| `--password` | Mailbox password, 1–100 characters |
| `--url` | Page to open. Default is `TARGET_URL1` |
| `--proxies` | Proxy file. Default is `proxies.txt` |
| `--proxy-type` | `http` (default), `https`, `socks5`, or `socks4`. A failed `http` check is retried as `socks5` |
| `--skip-check` | Skip the Dolphin proxy test |
| `--headless` | Ask Dolphin to start headless |

Allowed country codes: `DE BE BG DK EE FI FR GR IE IT HR LV LT LU MT NL PL PT RO SE CH SK SI ES CZ HU GB CY`.

The script accepts the consent dialog, fills the form, and clicks **I am human**. Archived Anosim numbers are ignored. A number already used 20 times, or listed under `banned`, is skipped. If GMX opens the registration-failed help page, that number is banned and another is used. Each number gets 3 SMS tries.

When the signup finishes, the mailbox is appended to `accounts.json`, the browser closes, and the Dolphin profile is deleted. If a step fails earlier, the browser stays open. If no usable free number remains, the browser closes and the profile is deleted. A failed run does not write `accounts.json`, except when the mailbox was created and only the POP3/IMAP step failed.

## Saved files

These stay on this machine and are gitignored.

| File | Contents |
| --- | --- |
| `accounts.json` | Finished mailboxes: `email` and `password` |
| `studio_queue.json` | Import queue and history |
| `sms_orders.json` | Open SMS code orders |
| `kka_orders.json` | Open Hero SMS orders |
| `anosim_orders.json` | Open Anosim orders |
| `live_numbers.json` | Numbers kept for reuse, and whether reuse is on |
| `freenumber_usage.json` | Free-number use counts and bans for `gmx_gen.py` |
| `sms_browser/`, `kka_browser/`, `anosim_browser/` | Signed-in browser profiles for the three services |

`accounts.json`:

```json
[
  {
    "email": "lena.hoffmann@gmx.net",
    "password": "Rote Welle 27?Haus"
  }
]
```

`freenumber_usage.json`:

```json
{
  "+4917616236940": 2,
  "banned": [
    "+4917614119074"
  ]
}
```

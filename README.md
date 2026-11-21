# PiwigoDownloader

**English** | [简体中文](README_zh.md)

Download every photo from a [Piwigo](https://piwigo.org/) gallery. Public galleries are crawled straight away; galleries that require credentials prompt for a username and password, log in, and then continue.

## Features

- **Official API** — everything goes through the Piwigo Web Service (`/ws.php?format=json`); no fragile HTML scraping.
- **Login only when needed** — if anonymous access returns content, the download starts immediately; otherwise (or on HTTP 401/403) the script asks for an account.
- **Dual login path** — `pwg.session.login` first, automatic fallback to the `identification.php` form, and the session is re-verified after either path.
- **Originals first** — uses `element_url`; falls back to the download endpoint or the largest derivative when it is missing.
- **Album structure preserved** — directory tree is rebuilt from `uppercats`, and each photo lands in its deepest category folder.
- **Concurrency + resume** — multi-threaded downloads, `.part` + `Range` resume, existing files skipped on the next run.
- **Manifest** — writes `manifest.json` with id, URL, dimensions and on-disk path for every photo.
- **Safe naming** — illegal characters stripped, Windows reserved names escaped, same-name-different-photo suffixed with the image id, never overwritten.

## Install

```bash
python -m pip install -r requirements.txt
```

Only `requests` is required; Python 3.9+.

## Usage

Public gallery (no credentials, crawling starts immediately):

```bash
python piwigo_downloader.py https://gallery.example.com/ -o downloads
```

Credential-protected gallery (the script asks by itself):

```bash
python piwigo_downloader.py https://example.com/piwigo/ -o downloads
# Output: this site requires an account to read the gallery, preparing to log in.
# Piwigo 用户名: ______
# Piwigo 密码:   ______
```

> The script's console messages are in Simplified Chinese; the prompts above are its literal output.
> Set `PIWIGO_PASSWORD` (or pass `-u`/`-p`) to run it unattended.

Credentials can also be supplied explicitly (passwords on the command line end up in shell history):

```bash
python piwigo_downloader.py https://example.com/ -u alice -p 'xxx' --threads 8
# username only, password prompted
python piwigo_downloader.py https://example.com/ -u alice --ask-password
# or via environment variable
export PIWIGO_PASSWORD='xxx'   # Windows: $env:PIWIGO_PASSWORD="xxx"
python piwigo_downloader.py https://example.com/ -u alice
```

Look before you download:

```bash
python piwigo_downloader.py https://gallery.example.com/ --list           # categories and counts
python piwigo_downloader.py https://gallery.example.com/ --dry-run        # enumerate only
python piwigo_downloader.py https://gallery.example.com/ --max-images 20  # grab 20 as a smoke test
python piwigo_downloader.py https://gallery.example.com/ --category 154   # one category only
```

## Options

| Option | Description |
| --- | --- |
| `url` | Site root; a sub-directory path such as `https://host/piwigo/` works too |
| `-o, --output` | Download directory, default `downloads` |
| `-u, --username` | Username; prompted for when omitted and required |
| `-p, --password` | Password; lower priority than `--ask-password` |
| `--ask-password` | Always prompt for the password |
| `--force-login` | Log in even if the gallery is publicly readable, to include private albums |
| `--no-login-prompt` | Never prompt; exit when credentials are required (useful for cron jobs) |
| `--threads` | Download concurrency, default 6 |
| `--per-page` | API page size, default 100 |
| `--max-images` | Stop after N photos, 0 means unlimited |
| `--category` | Restrict to a category id, repeatable |
| `--flat` | Put every file in one directory |
| `--dry-run` | Enumerate only, download nothing |
| `--list` | Print categories and photo counts only |
| `--overwrite` | Re-download files that already exist |
| `--no-progress` | Disable the progress line |
| `--timeout / --retries / --delay` | Timeout, retry count, per-page delay |
| `--proxy` | HTTP(S) proxy |
| `--insecure` | Skip TLS verification |
| `-v, --verbose` | Debug logging |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (including skipped existing files) |
| `1` | Some photos failed, or the site/network is broken |
| `2` | Credentials supplied but the login failed |
| `3` | The site requires a login and no usable credentials were obtained |
| `130` | Interrupted by the user |

## How it works

1. `pwg.session.getStatus` reports the Piwigo version and the current identity.
2. `pwg.categories.getList&recursive=true` lists every category; zero categories or zero photos means a login is required.
3. When a login is required, credentials are collected and `pwg.session.login` is tried, falling back to the `identification.php` form; a failed attempt asks again.
4. `pwg.categories.getImages` (100 per page) is walked category by category, de-duplicated globally by image id.
5. Each photo is fetched from `element_url` into `album path/filename` with `.part` + `Range` resume, in parallel.
6. `manifest.json` is written at the end.

## Directory layout

```
downloads/
├── 最近更新/
│   └── 11月第二批/
│       ├── 一之濑明日奈901285表情3.jpg
│       └── 魔女950288B.png
├── 图库搜索方式(建议使用)/
│   └── 7c02f556949b25a658eb34ac79a5e0c1.jpg
└── manifest.json
```

## Tests

```bash
python -m unittest discover -s tests -v
```

27 offline cases, no network: name sanitising, category tree rebuild, cross-category de-duplication, URL fallback order, login-required detection, login fallback, range resume, name collision handling, 404 counted as failure, manifest output.

## Notes

- Only content visible to the current identity is downloaded; private albums need an account with the matching permissions.
- The default page delay is 0.12 s; raise it with `--delay 0.5` and lower `--threads 3` if the server is under load.
- Interrupt and re-run at any time: finished files are skipped and partial ones resume.

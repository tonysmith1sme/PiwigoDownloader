# PiwigoDownloader

**English** | [简体中文](README_zh.md)

Download every photo from a [Piwigo](https://piwigo.org/) gallery. Public galleries are crawled straight away; galleries that require credentials prompt for a username and password, log in, and then continue.

## Features

- **Official API** — everything goes through the Piwigo Web Service (`/ws.php?format=json`); no fragile HTML scraping.
- **Login only when needed** — if anonymous access returns content, the download starts immediately; otherwise (or on HTTP 401/403) the script asks for an account.
- **Dual login path** — `pwg.session.login` first, automatic fallback to the `identification.php` form, and the session is re-verified after either path.
- **Originals first** — uses `element_url`; falls back to the download endpoint or the largest derivative when it is missing.
- **Album structure preserved** — directory tree is rebuilt from `uppercats`, and each photo lands in its deepest category folder.
- **Concurrency + resume** — multi-threaded (16 by default), `.part` + `Range` resume, finished files skipped on the next run.
- **Config file** — `piwigo.toml` holds threads, output dir, timeouts, proxy and more; any flag can still override it on the command line.
- **Incremental sync** — `downloads/.piwigo_index.json` remembers what was downloaded, so a re-run only fetches newly added photos.
- **Manifest** — writes `manifest.json` with id, URL, dimensions and on-disk path for every photo.
- **Safe naming** — illegal characters stripped, Windows reserved names escaped, same-name-different-photo suffixed with the image id, never overwritten.

## Install

```bash
python -m pip install -r requirements.txt
```

Only `requests` is required; Python 3.9+ (3.11+ for TOML configs, or use a `.json` config).

## Configuration file

The repo ships `piwigo.toml`:

```toml
[site]
url = ""                    # default site, or pass it on the command line

[download]
output = "downloads"        # download root
threads = 16                # concurrency — change this to go faster
per_page = 100              # API page size
timeout = 30.0              # per-request timeout (s)
retries = 3                 # per-photo retries
delay = 0.12                # delay between API pages (s)
max_images = 0              # 0 = unlimited
overwrite = false
flat = false
progress = true
index = true                # local index: re-runs fetch only new photos
index_file = ".piwigo_index.json"

[network]
proxy = ""
insecure = false

[auth]
username = ""
force_login = false
no_login_prompt = false
```

Precedence: **CLI > config file > built-in defaults**.

```bash
python piwigo_downloader.py --init-config          # write a fresh config template
python piwigo_downloader.py --config my.toml       # use another config file
python piwigo_downloader.py --threads 32           # one-off override
```

## Incremental downloads

The first run writes `.piwigo_index.json` into the output directory: `photo id → relative path + file size`.

- Re-running against the same site produces **no requests** for indexed photos; only newly added photos are fetched. The log prints `本地索引已有 N 张，待补 M 张`.
- Deleted or resized files are treated as missing and downloaded again.
- `--check-local` verifies everything offline and prunes stale index entries.

```bash
python piwigo_downloader.py --check-local
# 17:20:01 INFO    https://gallery.example.com：正常 20431 张，缺失 3 张，大小不符 0 张
```

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
| `url` | Site root; a sub-directory path such as `https://host/piwigo/` works too, or set it in the config |
| `-c, --config` | Config file path, defaults to `piwigo.toml` |
| `--no-config` | Ignore the config file |
| `--init-config` | Write a config template and exit |
| `-o, --output` | Download directory, default `downloads` |
| `-u, --username` | Username; prompted for when omitted and required |
| `-p, --password` | Password; lower priority than `--ask-password` |
| `--ask-password` | Always prompt for the password |
| `--force-login` | Log in even if the gallery is publicly readable, to include private albums |
| `--no-login-prompt` | Never prompt; exit when credentials are required (useful for cron jobs) |
| `--threads` | Download concurrency, default 16 (also settable in the config) |
| `--per-page` | API page size, default 100 |
| `--max-images` | Stop after N photos, 0 means unlimited |
| `--category` | Restrict to a category id, repeatable |
| `--flat` | Put every file in one directory |
| `--dry-run` | Enumerate only, download nothing |
| `--list` | Print categories and photo counts only |
| `--check-local` | Offline check of downloaded files, prunes stale index entries |
| `--overwrite` | Re-download files that already exist |
| `--no-index` | Disable the local index, fall back to plain file-existence checks |
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
5. Every photo is checked against the local index first: already-downloaded and intact means no request at all.
6. The rest is fetched from `element_url` into `album path/filename` with `.part` + `Range` resume, in parallel, updating the index as it goes.
7. `manifest.json` (this run) and the full index are written at the end.

## Directory layout

```
downloads/
├── .piwigo_index.json      # local index: what has been downloaded
├── manifest.json           # snapshot of this run
├── 最近更新/
│   └── 11月第二批/
│       ├── 一之濑明日奈901285表情3.jpg
│       └── 魔女950288B.png
└── 图库搜索方式(建议使用)/
    └── 7c02f556949b25a658eb34ac79a5e0c1.jpg
```

## Tests

```bash
python -m unittest discover -s tests -v
```

41 offline cases, no network: name sanitising, category tree rebuild, cross-category de-duplication, URL fallback order, login-required detection, login fallback, dirty-response parsing, range resume, name collision handling, 404 counted as failure, manifest output, config precedence, index round-trip and per-site isolation, indexed photos skipped without any request.

## Notes

- Only content visible to the current identity is downloaded; private albums need an account with the matching permissions.
- The default page delay is 0.12 s; raise it with `--delay 0.5` and lower `--threads 4` if the server is under load.
- Interrupt and re-run at any time: finished files and index entries are skipped, partial files resume.

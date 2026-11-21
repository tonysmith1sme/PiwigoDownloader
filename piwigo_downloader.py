#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""piwigo_downloader.py

Piwigo 图库批量下载器（公开图库直连 / 私有图库自动询问账号密码）。

设计要点:
  * 全部走 Piwigo 官方 Web Service（/ws.php?format=json），不依赖脆弱的 HTML 结构；
  * 匿名探测：能读到内容就直接爬；读不到或接口返回 401/403 就提示输入用户名密码；
  * 登录优先 pwg.session.login，失败回退到 identification.php 表单登录，两条路径都会复验会话；
  * 图片列表接口本身带 element_url（原图），无需逐张 getInfo，一次分页 100 张，速度可控；
  * 断点续传（.part + Range）、并发下载、文件名净化、清单落盘。

用法:
    python piwigo_downloader.py https://gallery.example.com/ -o downloads
    python piwigo_downloader.py https://gallery.example.com/ -u USER -p PASS --threads 8
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import requests
from requests.adapters import HTTPAdapter

VERSION = "0.1.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PiwigoDownloader/" + VERSION
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
DEFAULT_PER_PAGE = 100

log = logging.getLogger("piwigo")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class PiwigoError(RuntimeError):
    """Piwigo 接口或网络层错误。"""


class AuthRequired(PiwigoError):
    """该站点需要登录后才能读取目标内容。"""


class LoginFailed(PiwigoError):
    """提供了凭据但登录未成功。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class ImageRef:
    """一张图片在 Piwigo 侧的完整描述。"""

    id: int
    file: str = ""
    name: str = ""
    element_url: str = ""
    download_url: str = ""
    page_url: str = ""
    width: int = 0
    height: int = 0
    date_available: str = ""
    categories: list[dict[str, Any]] = field(default_factory=list)
    derivatives: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "ImageRef":
        return cls(
            id=int(raw.get("id") or 0),
            file=str(raw.get("file") or ""),
            name=str(raw.get("name") or ""),
            element_url=str(raw.get("element_url") or ""),
            download_url=str(raw.get("download_url") or ""),
            page_url=str(raw.get("page_url") or ""),
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
            date_available=str(raw.get("date_available") or ""),
            categories=list(raw.get("categories") or []),
            derivatives=dict(raw.get("derivatives") or {}),
        )

    def best_url(self) -> str:
        """原图优先；缺失时退到下载入口；再退到最大尺寸衍生图。"""
        if self.element_url:
            return self.element_url
        if self.download_url:
            return self.download_url
        best, area = "", -1
        for item in self.derivatives.values():
            if not isinstance(item, dict) or not item.get("url"):
                continue
            try:
                a = int(item.get("width") or 0) * int(item.get("height") or 0)
            except (TypeError, ValueError):
                a = 0
            if a > area:
                best, area = str(item["url"]), a
        return best


# --------------------------------------------------------------------------- #
# Piwigo Web Service 客户端
# --------------------------------------------------------------------------- #
class PiwigoClient:
    """封装 Piwigo ws.php 的读取与登录。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        delay: float = 0.12,
        verify_tls: bool = True,
        proxy: str | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.ws_url = f"{self.base_url}/ws.php"
        self.timeout = timeout
        self.retries = max(0, retries)
        self.delay = max(0.0, delay)
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
        )
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        if not verify_tls:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.status_info: dict[str, Any] = {}
        self.logged_in = False
        self.username: str | None = None

    # -- 基础请求 ---------------------------------------------------------- #
    def _request(
        self, method: str, params: dict[str, Any] | None = None, *, http_method: str = "GET"
    ) -> requests.Response:
        query = {"format": "json", "method": method}
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                if http_method == "POST":
                    resp = self.session.post(
                        self.ws_url,
                        params=query,
                        data=payload,
                        timeout=self.timeout,
                        verify=self.verify_tls,
                    )
                else:
                    merged = dict(query)
                    merged.update(payload)
                    resp = self.session.get(
                        self.ws_url,
                        params=merged,
                        timeout=self.timeout,
                        verify=self.verify_tls,
                    )
                if resp.status_code in (401, 403):
                    raise AuthRequired(f"接口 {method} 返回 HTTP {resp.status_code}，站点要求登录")
                if resp.status_code >= 500 and attempt < self.retries:
                    last_err = PiwigoError(f"HTTP {resp.status_code}")
                    time.sleep(0.4 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp
            except AuthRequired:
                raise
            except requests.RequestException as exc:
                last_err = exc
                if attempt >= self.retries:
                    break
                time.sleep(0.4 * (attempt + 1))
        raise PiwigoError(f"请求接口 {method} 失败: {last_err}")

    def api(self, method: str, *, http_method: str = "GET", **params: Any) -> Any:
        """调用 WS 方法，返回 result 字段；stat!=ok 时抛错。"""
        resp = self._request(method, params, http_method=http_method)
        text = resp.text or ""
        try:
            data = resp.json()
        except ValueError as exc:
            head = text.strip()[:160].replace("\n", " ")
            raise PiwigoError(f"接口 {method} 返回非 JSON 内容: {head!r}") from exc
        if not isinstance(data, dict):
            raise PiwigoError(f"接口 {method} 返回结构异常: {type(data).__name__}")
        if data.get("stat") != "ok":
            err = data.get("err")
            msg = data.get("message") or data.get("err") or "未知错误"
            if str(err) in ("401", "403") or "denied" in str(msg).lower():
                raise AuthRequired(f"接口 {method} 拒绝访问: {msg}")
            raise PiwigoError(f"接口 {method} 失败: {msg}")
        return data.get("result")

    # -- 会话 -------------------------------------------------------------- #
    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh or not self.status_info:
            info = self.api("pwg.session.getStatus") or {}
            if isinstance(info, dict):
                self.status_info = info
        return self.status_info

    def status_name(self) -> str:
        return str(self.get_status().get("status") or "guest")

    def is_guest(self) -> bool:
        return self.status_name() == "guest"

    def login(self, username: str, password: str) -> bool:
        """登录并复验；WS 登录失败时回退表单登录。"""
        self.get_status(refresh=True)
        ws_ok = False
        try:
            res = self.api(
                "pwg.session.login",
                http_method="POST",
                username=username,
                password=password,
            )
            ws_ok = bool(res)
        except PiwigoError as exc:
            log.debug("pwg.session.login 失败: %s", exc)

        info = self.get_status(refresh=True)
        if str(info.get("status") or "guest") != "guest":
            self.logged_in = True
            self.username = str(info.get("username") or username)
            log.debug("WS 登录成功（stat=%s）", "ok" if ws_ok else "fallback")
            return True

        self._form_login(username, password)
        info = self.get_status(refresh=True)
        if str(info.get("status") or "guest") != "guest":
            self.logged_in = True
            self.username = str(info.get("username") or username)
            log.debug("表单登录成功")
            return True
        return False

    def _form_login(self, username: str, password: str) -> None:
        """identification.php 表单登录兜底。"""
        login_url = f"{self.base_url}/identification.php"
        try:
            self.session.get(login_url, timeout=self.timeout, verify=self.verify_tls)
            self.session.post(
                login_url,
                data={"username": username, "password": password, "login": "Submit"},
                timeout=self.timeout,
                verify=self.verify_tls,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            log.debug("表单登录请求异常: %s", exc)

    # -- 内容读取 ---------------------------------------------------------- #
    def list_categories(self, recursive: bool = True) -> list[dict[str, Any]]:
        res = self.api(
            "pwg.categories.getList",
            recursive="true" if recursive else "false",
            public="false",
        )
        if isinstance(res, dict):
            return list(res.get("categories") or [])
        return list(res or [])

    def category_images_page(self, cat_id: int, page: int, per_page: int = DEFAULT_PER_PAGE) -> tuple[list[dict[str, Any]], int]:
        res = self.api(
            "pwg.categories.getImages",
            cat_id=cat_id,
            per_page=per_page,
            page=page,
            recursive="false",
        )
        res = res or {}
        images = list(res.get("images") or [])
        paging = res.get("paging") or {}
        try:
            total = int(paging.get("total_count") or len(images))
        except (TypeError, ValueError):
            total = len(images)
        return images, total

    def iter_category_images(
        self, cat_id: int, per_page: int = DEFAULT_PER_PAGE
    ) -> Iterator[dict[str, Any]]:
        page = 0
        seen = 0
        guard = 0
        while guard < 20000:
            guard += 1
            images, total = self.category_images_page(cat_id, page, per_page)
            if not images:
                return
            for raw in images:
                yield raw
            seen += len(images)
            page += 1
            if seen >= total:
                return
            if self.delay:
                time.sleep(self.delay)

    def probe(self) -> dict[str, Any]:
        """匿名探测：返回可达内容概况，判断是否需要登录。"""
        info = self.get_status(refresh=True)
        result: dict[str, Any] = {
            "version": info.get("version"),
            "status": info.get("status"),
            "needs_auth": False,
            "categories": [],
            "total_images": 0,
            "reason": "",
        }
        try:
            cats = self.list_categories(recursive=True)
        except AuthRequired:
            result["needs_auth"] = True
            result["reason"] = "接口返回 401/403"
            return result
        except PiwigoError as exc:
            raise exc

        total = 0
        for c in cats:
            try:
                total += int(c.get("nb_images") or 0)
            except (TypeError, ValueError):
                pass
        result["categories"] = cats
        result["total_images"] = total
        if not cats:
            result["needs_auth"] = True
            result["reason"] = "匿名状态下读不到任何分类"
        elif total == 0 and not any(int(c.get("nb_categories") or 0) for c in cats):
            result["needs_auth"] = True
            result["reason"] = "匿名状态下所有分类均为空"
        return result


def normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise PiwigoError("站点地址为空")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url.rstrip("/")


# --------------------------------------------------------------------------- #
# 命名与目录规划
# --------------------------------------------------------------------------- #
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_component(text: str, fallback: str = "item", max_len: int = 80) -> str:
    """把任意字符串净化成可跨平台的文件/目录名片段。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVALID_CHARS.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.split(".")[0].upper() in _WIN_RESERVED:
        value = f"_{value}"
    value = value[:max_len].rstrip(" .")
    return value or fallback


def _url_suffix(url: str) -> str:
    path = (url or "").split("?", 1)[0].split("#", 1)[0]
    _, ext = os.path.splitext(path)
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return ext if 1 < len(ext) <= 11 else ""


def safe_filename(image: ImageRef, max_len: int = 120) -> str:
    """原文件名优先，缺失时用 id；扩展名从 file 或原图 URL 推断。"""
    raw = str(image.file or image.name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(raw)
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    if len(ext) < 2:
        ext = _url_suffix(image.element_url) or _url_suffix(image.download_url)
    if not stem:
        stem = f"image-{image.id}"
    if not ext:
        ext = ".jpg"
    stem = safe_component(stem, f"image-{image.id}", max(16, max_len - len(ext)))
    return f"{stem}{ext}"


def build_category_paths(categories: Sequence[dict[str, Any]]) -> dict[int, list[str]]:
    """按 uppercats 还原目录层级；同级重名追加 id 以免互相覆盖。"""
    by_id: dict[int, dict[str, Any]] = {}
    for cat in categories:
        try:
            by_id[int(cat.get("id"))] = cat
        except (TypeError, ValueError):
            continue

    name_cache: dict[int, str] = {}
    used: dict[tuple[int | None, str], int] = {}
    for cid in sorted(by_id):
        cat = by_id[cid]
        parent = _parent_id(cat, by_id)
        name = safe_component(str(cat.get("name") or ""), f"cat_{cid}", 60)
        key = (parent, name.casefold())
        if key in used:
            name = f"{name} ({cid})"
        used[key] = cid
        name_cache[cid] = name

    paths: dict[int, list[str]] = {}
    for cid in by_id:
        chain: list[str] = []
        current: int | None = cid
        guard = 0
        while current is not None and guard < 128:
            guard += 1
            chain.append(name_cache.get(current, f"cat_{current}"))
            current = _parent_id(by_id.get(current, {}), by_id)
        paths[cid] = list(reversed(chain))
    return paths


def _parent_id(cat: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> int | None:
    parent = cat.get("id_uppercat")
    if parent in (None, "", 0, "0"):
        return None
    try:
        pid = int(parent)
    except (TypeError, ValueError):
        return None
    return pid if pid in by_id else None


@dataclass
class CatalogItem:
    """一张待下载的图片及其目标子目录。"""

    image: ImageRef
    rel_dir: Path


def collect_images(
    client: PiwigoClient,
    categories: Sequence[dict[str, Any]],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    only_categories: set[int] | None = None,
    max_images: int | None = None,
    flat: bool = False,
) -> list[CatalogItem]:
    """遍历全部分类，去重后返回待下载清单。"""
    paths = build_category_paths(categories)
    seen: set[int] = set()
    items: list[CatalogItem] = []

    for cat in categories:
        try:
            cid = int(cat.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if only_categories and cid not in only_categories:
            continue
        try:
            nb_images = int(cat.get("nb_images") or 0)
        except (TypeError, ValueError):
            nb_images = 0
        if nb_images <= 0:
            continue

        log.info("枚举分类 #%s %s（%d 张）", cid, cat.get("name"), nb_images)
        for raw in client.iter_category_images(cid, per_page):
            image = ImageRef.from_api(raw)
            if image.id in seen:
                continue
            seen.add(image.id)
            rel_dir = Path() if flat else _image_dir(image, cid, paths)
            items.append(CatalogItem(image=image, rel_dir=rel_dir))
            if max_images and len(items) >= max_images:
                log.info("达到 --max-images=%d 限制，停止枚举", max_images)
                return items
        log.debug("分类 #%s 完成，累计 %d 张", cid, len(items))
    return items


def _image_dir(image: ImageRef, fallback_cid: int, paths: dict[int, list[str]]) -> Path:
    """取该图片所属层级最深的分类作为存放目录。"""
    best: list[str] | None = None
    for cat in image.categories:
        try:
            cid = int(cat.get("id") or 0)
        except (TypeError, ValueError):
            continue
        candidate = paths.get(cid)
        if candidate and (best is None or len(candidate) > len(best)):
            best = candidate
    if best is None:
        best = paths.get(fallback_cid) or [f"cat_{fallback_cid}"]
    return Path(*best)


# --------------------------------------------------------------------------- #
# 下载器
# --------------------------------------------------------------------------- #
@dataclass
class DownloadStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    failures: list[tuple[int, str]] = field(default_factory=list)


class Progress:
    """轻量进度条：不依赖 tqdm，非 TTY 时降级为节流日志。"""

    def __init__(self, total: int, enabled: bool = True, stream: Any = None) -> None:
        self.total = max(0, total)
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.start = time.monotonic()
        self.done = 0
        self.bytes = 0
        self._last_render = 0.0
        self._lock = threading.Lock()
        self._isatty = bool(getattr(self.stream, "isatty", lambda: False)())

    def update(self, nbytes: int = 0, results: dict[str, int] | None = None) -> None:
        with self._lock:
            self.done += 1
            self.bytes += max(0, nbytes)
            if not self.enabled:
                return
            now = time.monotonic()
            if now - self._last_render < 0.2 and self.done < self.total:
                return
            self._last_render = now
            self._render(results or {})

    def _render(self, results: dict[str, int]) -> None:
        elapsed = max(1e-6, time.monotonic() - self.start)
        pct = (self.done / self.total * 100) if self.total else 100.0
        speed = self.bytes / elapsed
        line = (
            f"下载 {self.done}/{self.total} ({pct:5.1f}%) "
            f"{human_size(self.bytes)}  {human_size(int(speed))}/s"
        )
        if results:
            extra = " ".join(f"{k}={v}" for k, v in results.items() if v)
            if extra:
                line = f"{line}  {extra}"
        width = 0
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 100
        line = line[: max(20, width - 1)]
        if self._isatty:
            self.stream.write("\r" + line.ljust(max(20, width - 1)))
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        if not self.enabled:
            return
        if self._isatty:
            self.stream.write("\n")
            self.stream.flush()


def human_size(num: float) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TB"


class Downloader:
    """并发下载 + 断点续传 + 结果清单。"""

    def __init__(
        self,
        client: PiwigoClient,
        out_dir: Path,
        *,
        threads: int = 6,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        overwrite: bool = False,
        progress: bool = True,
    ) -> None:
        self.client = client
        self.out_dir = Path(out_dir)
        self.threads = max(1, threads)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.overwrite = overwrite
        self.show_progress = progress
        self._local = threading.local()
        self._claimed: dict[Path, int] = {}
        self._claim_lock = threading.Lock()

    # -- 会话 -------------------------------------------------------------- #
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.client.session.headers)
            session.cookies.update(self.client.session.cookies)
            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.proxies.update(self.client.session.proxies)
            self._local.session = session
        return session

    # -- 目标路径 ---------------------------------------------------------- #
    def resolve_dest(self, item: CatalogItem) -> Path:
        directory = self.out_dir / item.rel_dir
        name = safe_filename(item.image)
        dest = directory / name
        with self._claim_lock:
            owner = self._claimed.get(dest)
            if owner is not None and owner != item.image.id:
                # 同目录同名但不同图片：加 id 区分，绝不互相覆盖
                stem, ext = os.path.splitext(name)
                dest = directory / f"{stem}~{item.image.id}{ext}"
            self._claimed.setdefault(dest, item.image.id)
        return dest

    # -- 单张下载 ---------------------------------------------------------- #
    def download_one(self, item: CatalogItem) -> tuple[str, int]:
        url = item.image.best_url()
        if not url:
            raise PiwigoError(f"图片 #{item.image.id} 没有可用地址")

        dest = self.resolve_dest(item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        if dest.exists() and dest.stat().st_size > 0 and not self.overwrite:
            return "skipped", dest.stat().st_size

        written = 0
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                written = self._stream_to_file(url, part, dest)
                os.replace(part, dest)
                return "done", written
            except AuthRequired:
                raise
            except (requests.RequestException, PiwigoError, OSError) as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
        raise PiwigoError(f"下载失败 {url}: {last_err}")

    def _stream_to_file(self, url: str, part: Path, dest: Path) -> int:
        headers: dict[str, str] = {}
        offset = 0
        if part.exists() and part.stat().st_size > 0:
            offset = part.stat().st_size
            headers["Range"] = f"bytes={offset}-"

        session = self._session()
        with session.get(
            url,
            headers=headers or None,
            timeout=self.timeout,
            stream=True,
            verify=self.client.verify_tls,
            allow_redirects=True,
        ) as resp:
            if resp.status_code in (401, 403):
                raise AuthRequired(f"图片地址需要登录: {url}")
            if resp.status_code == 416 and offset:
                part.unlink(missing_ok=True)
                return self._stream_to_file(url, part, dest)
            if offset and resp.status_code != 206:
                offset = 0
                part.unlink(missing_ok=True)
            resp.raise_for_status()

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise PiwigoError(f"返回了 HTML（可能是登录页或错误页）: {url}")

            mode = "ab" if offset else "wb"
            total = 0
            with part.open(mode) as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    total += len(chunk)
            if total == 0 and not (part.exists() and part.stat().st_size > 0):
                raise PiwigoError(f"下载内容为空: {url}")
        return part.stat().st_size if part.exists() else 0

    # -- 批量下载 ---------------------------------------------------------- #
    def run(self, items: Sequence[CatalogItem]) -> DownloadStats:
        with self._claim_lock:
            self._claimed.clear()
        stats = DownloadStats(total=len(items))
        progress = Progress(len(items), enabled=self.show_progress)
        counters = {"done": 0, "skipped": 0, "failed": 0}
        lock = threading.Lock()

        def work(item: CatalogItem) -> None:
            try:
                status, size = self.download_one(item)
            except AuthRequired as exc:
                with lock:
                    stats.failed += 1
                    stats.failures.append((item.image.id, f"需要登录: {exc}"))
                    counters["failed"] += 1
                return
            except Exception as exc:  # noqa: BLE001 - 单图失败不终止整体
                with lock:
                    stats.failed += 1
                    stats.failures.append((item.image.id, str(exc)))
                    counters["failed"] += 1
                log.debug("图片 #%s 失败: %s", item.image.id, exc)
                progress.update(0, counters)
                return
            with lock:
                stats.bytes += size
                if status == "skipped":
                    stats.skipped += 1
                    counters["skipped"] += 1
                else:
                    stats.done += 1
                    counters["done"] += 1
            progress.update(size, counters)

        if self.threads == 1:
            for item in items:
                work(item)
        else:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                list(pool.map(work, items))
        progress.close()
        if stats.failures:
            log.warning("有 %d 张下载失败，前 5 条：", len(stats.failures))
            for image_id, reason in stats.failures[:5]:
                log.warning("  #%s %s", image_id, reason)
        return stats

    # -- 清单 -------------------------------------------------------------- #
    def write_manifest(self, items: Sequence[CatalogItem], stats: DownloadStats) -> Path:
        path = self.out_dir / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "site": self.client.base_url,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": f"PiwigoDownloader/{VERSION}",
            "username": self.client.username,
            "total": stats.total,
            "done": stats.done,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "items": [
                {
                    "id": item.image.id,
                    "file": safe_filename(item.image),
                    "dir": str(item.rel_dir).replace("\\", "/"),
                    "url": item.image.best_url(),
                    "page_url": item.image.page_url,
                    "width": item.image.width,
                    "height": item.image.height,
                    "date": item.image.date_available,
                }
                for item in items
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# 入口（后续扩展）
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="下载 Piwigo 图库中的全部照片")
    parser.add_argument("url", help="Piwigo 站点根地址，例如 https://gallery.example.com/")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    client = PiwigoClient(args.url)
    info = client.probe()
    log.info("站点版本: %s / 当前身份: %s", info["version"], info["status"])
    log.info(
        "公开可读分类: %d 个，图片约 %d 张%s",
        len(info["categories"]),
        info["total_images"],
        "，需要登录" if info["needs_auth"] else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

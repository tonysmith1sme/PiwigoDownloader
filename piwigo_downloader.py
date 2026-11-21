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

VERSION = "0.2.0"
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

    @staticmethod
    def parse_payload(text: str) -> dict[str, Any] | None:
        """解析响应体；容忍 PHP Notice/Warning 先于 JSON 输出的脏响应。"""
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except ValueError:
            pass
        # 有些站点在 JSON 之前打印了 6~7KB 的 PHP 通知，直接切出 JSON 片段
        start = text.find('{"stat"')
        if start < 0:
            start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def api(self, method: str, *, http_method: str = "GET", **params: Any) -> Any:
        """调用 WS 方法，返回 result 字段；stat!=ok 时抛错。"""
        resp = self._request(method, params, http_method=http_method)
        text = resp.text or ""
        data = self.parse_payload(text)
        if data is None:
            head = text.strip()[:160].replace("\n", " ")
            raise PiwigoError(f"接口 {method} 返回非 JSON 内容: {head!r}")
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
            try:
                images, total = self.category_images_page(cat_id, page, per_page)
            except PiwigoError as exc:
                log.warning("分类 #%s 第 %d 页读取失败，停止该分类：%s", cat_id, page, exc)
                return
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
    """接受站点根、带页面路径或直接粘贴的入口文件，统一成根地址。"""
    url = (url or "").strip().strip("'\"")
    if not url:
        raise PiwigoError("站点地址为空")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    # 允许直接粘贴 index.php / ws.php / picture.php 等入口
    url = re.sub(
        r"/(?:index|ws|picture|identification|action|admin|search)\.php$", "", url, flags=re.I
    )
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


def _rel_to(root: Path, path: Path) -> str:
    """相对 root 的 POSIX 路径，写入索引时用。"""
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


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
        try:
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
        except AuthRequired:
            raise
        except PiwigoError as exc:
            log.warning("分类 #%s 枚举失败，跳过：%s", cid, exc)
            continue
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
# 本地索引：记录已下载图片，重跑只补新增
# --------------------------------------------------------------------------- #
class LocalIndex:
    """<output>/.piwigo_index.json 中的图片索引，按站点隔离。

    结构: {"version": 1, "sites": {"https://site": {"images": {"<id>": {...}}}}}
    """

    def __init__(self, path: Path, site: str) -> None:
        self.path = Path(path)
        self.site = site
        self.images: dict[str, dict[str, Any]] = {}
        self._doc: dict[str, Any] = {"version": 1, "sites": {}}
        self._lock = threading.Lock()
        self._dirty = False

    # -- 读写 -------------------------------------------------------------- #
    def load(self) -> "LocalIndex":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        if not isinstance(data, dict):
            return self
        self._doc = data
        sites = data.get("sites")
        if isinstance(sites, dict):
            entry = sites.get(self.site)
            if isinstance(entry, dict) and isinstance(entry.get("images"), dict):
                self.images = dict(entry["images"])
        return self

    def save(self) -> bool:
        with self._lock:
            if not self._dirty and self.path.exists():
                return False
            sites = self._doc.setdefault("sites", {})
            sites[self.site] = {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "images": self.images,
            }
            self._doc["version"] = 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            return True

    # -- 查询 / 记录 -------------------------------------------------------- #
    def record(self, image_id: int, rel_path: str, size: int) -> None:
        with self._lock:
            self.images[str(image_id)] = {
                "path": rel_path.replace("\\", "/"),
                "size": int(size),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._dirty = True

    def lookup(self, image_id: int) -> dict[str, Any] | None:
        return self.images.get(str(image_id))

    def is_downloaded(self, image_id: int, out_dir: Path) -> tuple[bool, int]:
        """索引命中且文件仍在、大小一致，才算已下载。"""
        rec = self.lookup(image_id)
        if not isinstance(rec, dict) or not rec.get("path"):
            return False, 0
        target = out_dir / str(rec["path"])
        try:
            size = target.stat().st_size
        except OSError:
            return False, 0
        if size <= 0:
            return False, 0
        expected = rec.get("size")
        if isinstance(expected, int) and expected > 0 and size != expected:
            return False, 0
        return True, size

    def __len__(self) -> int:
        return len(self.images)


def verify_local_index(path: Path, out_dir: Path) -> dict[str, dict[str, int]]:
    """离线体检索引：逐个站点检查文件是否存在、大小是否一致，并剔除失效记录。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    sites = data.get("sites")
    if not isinstance(sites, dict):
        return {}
    results: dict[str, dict[str, int]] = {}
    for site, entry in sites.items():
        if not isinstance(entry, dict):
            continue
        images = entry.get("images")
        if not isinstance(images, dict):
            continue
        ok = missing = mismatch = 0
        for key in list(images):
            rec = images.get(key)
            rel = str((rec or {}).get("path") or "")
            target = out_dir / rel if rel else None
            try:
                size = target.stat().st_size if target is not None else 0
            except OSError:
                size = 0
            if size <= 0:
                images.pop(key, None)
                missing += 1
                continue
            expected = (rec or {}).get("size")
            if isinstance(expected, int) and expected > 0 and size != expected:
                images.pop(key, None)
                mismatch += 1
                continue
            ok += 1
        entry["images"] = images
        results[site] = {"ok": ok, "missing": missing, "mismatch": mismatch}
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("索引写回失败: %s", exc)
    return results


# --------------------------------------------------------------------------- #
# 下载器
# --------------------------------------------------------------------------- #
@dataclass
class DownloadStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    indexed: int = 0
    failed: int = 0
    bytes: int = 0
    skipped_bytes: int = 0
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
    """并发下载 + 断点续传 + 本地索引增量 + 结果清单。"""

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
        index: "LocalIndex | None" = None,
    ) -> None:
        self.client = client
        self.out_dir = Path(out_dir)
        self.threads = max(1, threads)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.overwrite = overwrite
        self.show_progress = progress
        self.index = index
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

        # 1) 本地索引命中：文件还在、大小一致 → 直接跳过，不产生任何请求
        if self.index is not None and not self.overwrite:
            hit, size = self.index.is_downloaded(item.image.id, self.out_dir)
            if hit:
                return "indexed", size

        dest = self.resolve_dest(item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        if dest.exists() and dest.stat().st_size > 0 and not self.overwrite:
            size = dest.stat().st_size
            if self.index is not None:
                self.index.record(item.image.id, _rel_to(self.out_dir, dest), size)
            return "skipped", size

        written = 0
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                written = self._stream_to_file(url, part, dest)
                os.replace(part, dest)
                if self.index is not None:
                    self.index.record(item.image.id, _rel_to(self.out_dir, dest), written)
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
        counters = {"done": 0, "skipped": 0, "indexed": 0, "failed": 0}
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
                if status == "indexed":
                    stats.indexed += 1
                    stats.skipped_bytes += size
                    counters["indexed"] += 1
                elif status == "skipped":
                    stats.skipped += 1
                    stats.skipped_bytes += size
                    counters["skipped"] += 1
                else:
                    stats.done += 1
                    stats.bytes += size
                    counters["done"] += 1
                served = size if status == "done" else 0
                snap = len(self.index) if self.index is not None else 0
            progress.update(served, counters)
            # 每 500 条落一次索引，进程被杀也不至于全丢
            if self.index is not None and snap and snap % 500 == 0:
                self.index.save()

        if self.threads == 1:
            for item in items:
                work(item)
        else:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                list(pool.map(work, items))
        progress.close()
        if self.index is not None:
            self.index.save()
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
# 配置
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG_FILE = "piwigo.toml"
DEFAULT_THREADS = 16

CONFIG_TEMPLATE = """# PiwigoDownloader 配置文件（TOML）
# 命令行参数优先级更高，可临时覆盖任意一项。
# 用 --init-config 生成此模板，用 --config 指定其它路径。

[site]
# 默认站点地址；命令行给了 url 时以命令行为准
url = ""

[download]
output = "downloads"        # 下载根目录
threads = 16                # 并发下载线程数
per_page = 100              # 接口分页大小
timeout = 30.0              # 单请求超时（秒）
retries = 3                 # 单张图片失败重试次数
delay = 0.12                # 接口分页间隔（秒）
max_images = 0              # 最多下载张数，0 为不限
overwrite = false           # 已存在文件是否重下
flat = false                # 平铺保存，不建分类目录
progress = true             # 是否显示进度
index = true                # 启用本地索引，重跑只补新增图片
index_file = ".piwigo_index.json"

[network]
proxy = ""                  # 例如 http://127.0.0.1:7890
insecure = false            # 跳过 TLS 证书校验

[auth]
username = ""               # 留空则按需询问
force_login = false         # 匿名可读也先登录，纳入私有相册
no_login_prompt = false     # 需要登录时不询问，直接退出
"""


@dataclass
class Settings:
    """配置文件与命令行合并后的最终配置。"""

    url: str = ""
    output: Path = Path("downloads")
    username: str | None = None
    password: str = ""
    ask_password: bool = False
    force_login: bool = False
    no_login_prompt: bool = False
    threads: int = DEFAULT_THREADS
    per_page: int = DEFAULT_PER_PAGE
    max_images: int = 0
    categories: list[int] = field(default_factory=list)
    flat: bool = False
    dry_run: bool = False
    list_only: bool = False
    check_local: bool = False
    overwrite: bool = False
    progress: bool = True
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    delay: float = 0.12
    proxy: str | None = None
    insecure: bool = False
    verbose: bool = False
    use_index: bool = True
    index_file: str = ".piwigo_index.json"


def load_config_file(path: Path) -> dict[str, Any]:
    """读取 TOML / JSON 配置；文件不存在返回空字典。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PiwigoError(f"配置文件解析失败（JSON）{path}: {exc}") from exc
        return data if isinstance(data, dict) else {}
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - py<3.11
        raise PiwigoError("当前 Python 无 tomllib（需 3.11+），请改用 .json 配置文件") from exc
    try:
        return tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 - tomllib 抛 TOMLDecodeError
        raise PiwigoError(f"配置文件解析失败（TOML）{path}: {exc}") from exc


def _pick(*values: Any, default: Any = None) -> Any:
    """取第一个非 None 且非空串的值。"""
    for value in values:
        if value is None or value == "":
            continue
        return value
    return default


def resolve_settings(args: argparse.Namespace, cfg: dict[str, Any]) -> Settings:
    """合并命令行与配置文件：命令行 > 配置 > 内置默认。"""
    site = cfg.get("site") or {}
    dl = cfg.get("download") or {}
    net = cfg.get("network") or {}
    auth = cfg.get("auth") or {}

    threads = int(_pick(args.threads, dl.get("threads"), default=DEFAULT_THREADS))
    per_page = int(_pick(args.per_page, dl.get("per_page"), default=DEFAULT_PER_PAGE))

    return Settings(
        url=str(_pick(args.url, site.get("url"), default="")),
        output=Path(str(_pick(args.output, dl.get("output"), default="downloads"))),
        username=_pick(args.username, auth.get("username"), default=None),
        password=str(args.password or ""),
        ask_password=bool(args.ask_password),
        force_login=bool(args.force_login) or bool(auth.get("force_login", False)),
        no_login_prompt=bool(args.no_login_prompt) or bool(auth.get("no_login_prompt", False)),
        threads=threads,
        per_page=per_page,
        max_images=int(_pick(args.max_images, dl.get("max_images"), default=0)),
        categories=list(args.category or []),
        flat=bool(args.flat) or bool(dl.get("flat", False)),
        dry_run=bool(args.dry_run),
        list_only=bool(args.list),
        check_local=bool(args.check_local),
        overwrite=bool(args.overwrite) or bool(dl.get("overwrite", False)),
        progress=not args.no_progress and bool(dl.get("progress", True)),
        timeout=float(_pick(args.timeout, dl.get("timeout"), default=DEFAULT_TIMEOUT)),
        retries=int(_pick(args.retries, dl.get("retries"), default=DEFAULT_RETRIES)),
        delay=float(_pick(args.delay, dl.get("delay"), default=0.12)),
        proxy=_pick(args.proxy, net.get("proxy"), default=None),
        insecure=bool(args.insecure) or bool(net.get("insecure", False)),
        verbose=bool(args.verbose),
        use_index=not args.no_index and bool(dl.get("index", True)),
        index_file=str(dl.get("index_file") or ".piwigo_index.json"),
    )


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_FAILED = 2
EXIT_CREDENTIALS_NEEDED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piwigo_downloader.py",
        description="下载 Piwigo 图库中的全部照片（公开站点直连，私有站点登录后下载）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python piwigo_downloader.py --init-config          # 生成 piwigo.toml 配置模板\n"
            "  python piwigo_downloader.py https://gallery.example.com/\n"
            "  python piwigo_downloader.py --config my.toml --threads 32\n"
            "  python piwigo_downloader.py --check-local          # 离线体检本地图片\n"
        ),
    )
    parser.add_argument(
        "url", nargs="?", help="Piwigo 站点根地址；也可写在配置文件的 [site] url 里"
    )
    parser.add_argument("-c", "--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG_FILE}）")
    parser.add_argument("--no-config", action="store_true", help="忽略配置文件，只用命令行与内置默认")
    parser.add_argument("--init-config", action="store_true", help="在当前目录生成配置模板后退出")
    parser.add_argument("-o", "--output", help="下载目录（默认 downloads）")
    parser.add_argument("-u", "--username", help="Piwigo 用户名；不给则按需交互询问")
    parser.add_argument("-p", "--password", help="Piwigo 密码；不建议明文写在命令里")
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help="强制交互输入密码（覆盖 -p 与 PIWIGO_PASSWORD）",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="即使匿名可读也先登录（用于纳入私有相册）",
    )
    parser.add_argument(
        "--no-login-prompt",
        action="store_true",
        help="匿名不可读时不再询问，直接报错退出（适合脚本/定时任务）",
    )
    parser.add_argument("--threads", type=int, help="并发下载线程数")
    parser.add_argument("--per-page", type=int, help="接口分页大小")
    parser.add_argument("--max-images", type=int, help="最多下载多少张（0 表示不限）")
    parser.add_argument("--category", type=int, action="append", help="只下载指定分类 id，可重复")
    parser.add_argument("--flat", action="store_true", help="不建分类子目录，全部平铺")
    parser.add_argument("--dry-run", action="store_true", help="只枚举不下载，输出统计")
    parser.add_argument("--list", action="store_true", help="只列出分类与张数")
    parser.add_argument(
        "--check-local",
        action="store_true",
        help="只体检本地已下载图片（不联网），并剔除索引里的失效记录",
    )
    parser.add_argument("--overwrite", action="store_true", help="已存在文件也重新下载")
    parser.add_argument("--no-index", action="store_true", help="关闭本地索引，仅按文件是否存在跳过")
    parser.add_argument("--no-progress", action="store_true", help="关闭进度条")
    parser.add_argument("--timeout", type=float, help="单请求超时秒数")
    parser.add_argument("--retries", type=int, help="失败重试次数")
    parser.add_argument("--delay", type=float, help="接口分页间隔秒数")
    parser.add_argument("--proxy", help="HTTP(S) 代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"PiwigoDownloader {VERSION}")
    return parser


def prompt_credentials(attempt: int = 1) -> tuple[str, str] | None:
    """交互式询问账号密码；非 TTY 返回 None。"""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print("", file=sys.stderr)
    if attempt > 1:
        print(f"（第 {attempt} 次）账号或密码不正确，请重试。", file=sys.stderr)
    try:
        username = input("Piwigo 用户名: ").strip()
        if not username:
            return None
        password = getpass.getpass("Piwigo 密码: ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    return username, password


def collect_credentials(settings: Settings, allow_prompt: bool) -> tuple[str, str] | None:
    """按 -u/-p/环境变量/交互的顺序取凭据。"""
    username = (settings.username or "").strip()
    password = settings.password or ""
    if settings.ask_password:
        password = ""
    if not password:
        password = os.environ.get("PIWIGO_PASSWORD", "")
    if username and password:
        return username, password
    if not allow_prompt:
        return None

    for attempt in range(1, 4):
        if not username:
            pair = prompt_credentials(attempt)
            if pair is None:
                return None
            username, password = pair
        elif not password:
            try:
                password = getpass.getpass(f"用户 {username} 的 Piwigo 密码: ")
            except (EOFError, KeyboardInterrupt):
                print("", file=sys.stderr)
                return None
        if username and password:
            return username, password
    return None


def authenticate(client: PiwigoClient, settings: Settings, *, allow_prompt: bool) -> bool:
    """需要登录时取凭据并尝试登录，失败允许有限次重试。"""
    log.info("该站点需要账号密码才能读取图库内容，准备登录。")
    attempts = 3 if allow_prompt else 1
    for attempt in range(1, attempts + 1):
        creds = collect_credentials(settings, allow_prompt=allow_prompt)
        if not creds or not creds[0] or not creds[1]:
            return False
        username, password = creds
        if client.login(username, password):
            log.info("登录成功，当前身份: %s", client.status_name())
            return True
        log.warning("登录失败：账号或密码不正确")
        # 清空已用凭据，下一轮重新询问
        settings.username, settings.password = None, ""
    return False


def run(settings: Settings) -> int:
    out_dir = settings.output.expanduser()

    # 0) 纯本地体检，不联网
    if settings.check_local:
        if not settings.use_index:
            log.error("--check-local 需要启用本地索引（去掉 --no-index）")
            return EXIT_ERROR
        path = out_dir / settings.index_file
        if not path.exists():
            log.error("还没有本地索引: %s（先跑一次下载再说）", path)
            return EXIT_ERROR
        results = verify_local_index(path, out_dir)
        if not results:
            log.warning("索引里没有可用记录: %s", path)
            return EXIT_OK
        for site, stat in results.items():
            log.info(
                "%s：正常 %d 张，缺失 %d 张，大小不符 %d 张",
                site,
                stat["ok"],
                stat["missing"],
                stat["mismatch"],
            )
        log.info("索引已更新，失效记录已剔除，下次运行会重新下载这些图片")
        return EXIT_OK

    client = PiwigoClient(
        settings.url,
        timeout=settings.timeout,
        retries=settings.retries,
        delay=settings.delay,
        verify_tls=not settings.insecure,
        proxy=settings.proxy,
    )

    # 1) 匿名探测
    try:
        info = client.probe()
    except PiwigoError as exc:
        log.error("站点不可用或不是 Piwigo: %s", exc)
        return EXIT_ERROR
    log.info(
        "连接成功: %s (Piwigo %s)，当前身份 %s",
        client.base_url,
        info.get("version") or "?",
        info.get("status") or "guest",
    )

    # 2) 需要时登录
    if info["needs_auth"] or (settings.force_login and client.is_guest()):
        if info["needs_auth"]:
            log.info("匿名可见内容为空（%s）", info.get("reason") or "需要登录")
        else:
            log.info("按 --force-login 要求先登录")
        if not authenticate(client, settings, allow_prompt=not settings.no_login_prompt):
            if settings.no_login_prompt:
                log.error("登录未成功：请检查 -u/--username 与 -p/--password（当前已禁用交互提示）")
            else:
                log.error("未获得有效凭据，退出")
            return EXIT_CREDENTIALS_NEEDED
        try:
            info = client.probe()
        except PiwigoError as exc:
            log.error("登录后读取分类失败: %s", exc)
            return EXIT_ERROR
    elif settings.username:
        # 站点匿名可读，但用户仍提供了账号：登录以纳入私有内容
        log.info("站点匿名可读，尝试用提供的账号纳入私有内容")
        creds = collect_credentials(settings, allow_prompt=not settings.no_login_prompt)
        if creds and client.login(*creds):
            info = client.probe()
        else:
            log.warning("账号登录未成功，继续以游客身份下载公开内容")

    categories = info["categories"]
    if not categories:
        log.error("没有读到任何分类，站点可能是空的或权限不足")
        return EXIT_ERROR

    if settings.list_only:
        for cat in categories:
            depth = str(cat.get("uppercats") or "").count(",")
            indent = "  " * depth
            log.info(
                "%s#%s %s (%s 张)",
                indent,
                cat.get("id"),
                cat.get("name"),
                cat.get("nb_images") or 0,
            )
        return EXIT_OK

    # 3) 枚举
    only = set(settings.categories) if settings.categories else None
    try:
        items = collect_images(
            client,
            categories,
            per_page=settings.per_page,
            only_categories=only,
            max_images=settings.max_images or None,
            flat=settings.flat,
        )
    except AuthRequired:
        log.error("枚举过程中被要求登录，请提供有效凭据后重试")
        return EXIT_CREDENTIALS_NEEDED
    except PiwigoError as exc:
        log.error("枚举图片失败: %s", exc)
        return EXIT_ERROR

    log.info("站点图片: %d 张（已去重）", len(items))
    if not items:
        log.warning("没有符合条件的图片")
        return EXIT_OK

    # 4) 本地索引：统计已有、待补
    index: LocalIndex | None = None
    if settings.use_index:
        index = LocalIndex(out_dir / settings.index_file, client.base_url).load()
        known = sum(1 for item in items if index.is_downloaded(item.image.id, out_dir)[0])
        log.info("本地索引已有 %d 张，待补 %d 张", known, len(items) - known)

    if settings.dry_run:
        pending = [
            i for i in items if not (index and index.is_downloaded(i.image.id, out_dir)[0])
        ]
        for item in pending[:10]:
            log.info("  #%s %s/%s", item.image.id, item.rel_dir, safe_filename(item.image))
        if len(pending) > 10:
            log.info("  ... 其余 %d 张省略", len(pending) - 10)
        log.info("dry-run 结束，未下载任何文件")
        return EXIT_OK

    # 5) 下载
    downloader = Downloader(
        client,
        out_dir,
        threads=settings.threads,
        timeout=max(settings.timeout, 60.0),
        retries=max(1, min(settings.retries, 6)),
        overwrite=settings.overwrite,
        progress=settings.progress,
        index=index,
    )
    started = time.monotonic()
    stats = downloader.run(items)
    elapsed = max(1e-6, time.monotonic() - started)
    manifest = downloader.write_manifest(items, stats)

    log.info(
        "完成：新增 %d 张 / 索引跳过 %d 张 / 本地跳过 %d 张 / 失败 %d 张，新下载 %s，用时 %.1fs（%s/s）",
        stats.done,
        stats.indexed,
        stats.skipped,
        stats.failed,
        human_size(stats.bytes),
        elapsed,
        human_size(int(stats.bytes / elapsed)),
    )
    if stats.skipped_bytes:
        log.info("已存在文件共 %s，未重复下载", human_size(stats.skipped_bytes))
    if index is not None:
        log.info("本地索引: %s（%d 张）", index.path, len(index))
    log.info("本次清单: %s", manifest)
    return EXIT_OK if stats.failed == 0 else EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.init_config:
        target = Path(args.config or DEFAULT_CONFIG_FILE)
        if target.exists():
            log.error("配置模板已存在: %s（如需重建请先删除）", target)
            return EXIT_ERROR
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        log.info("已生成配置模板: %s", target)
        return EXIT_OK

    cfg: dict[str, Any] = {}
    if not args.no_config:
        cfg_path = Path(args.config) if args.config else Path(DEFAULT_CONFIG_FILE)
        if cfg_path.exists():
            cfg = load_config_file(cfg_path)
            log.info("读取配置: %s", cfg_path)
        elif args.config:
            log.error("配置文件不存在: %s", cfg_path)
            return EXIT_ERROR

    settings = resolve_settings(args, cfg)
    if settings.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not settings.url and not settings.check_local:
        parser.error("缺少站点地址：请给出 url 参数，或在配置文件的 [site] url 中设置")
    if settings.threads < 1:
        parser.error("--threads 必须 >= 1")
    if settings.per_page < 1 or settings.per_page > 500:
        parser.error("--per-page 需在 1..500 之间")

    try:
        return run(settings)
    except KeyboardInterrupt:
        log.warning("已中断")
        return 130
    except PiwigoError as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

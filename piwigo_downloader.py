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

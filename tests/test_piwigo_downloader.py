# -*- coding: utf-8 -*-
"""离线回归测试：不访问真实站点，全部用内存桩与本地 HTTP 服务。"""

from __future__ import annotations

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import piwigo_downloader as pd  # noqa: E402


# --------------------------------------------------------------------------- #
# 命名与目录
# --------------------------------------------------------------------------- #
class SafeNameTest(unittest.TestCase):
    def test_removes_invalid_chars(self):
        self.assertEqual(pd.safe_component('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_reserved_windows_name(self):
        self.assertTrue(pd.safe_component("CON").startswith("_"))

    def test_empty_uses_fallback(self):
        self.assertEqual(pd.safe_component("   ", "x"), "x")

    def test_filename_keeps_extension(self):
        img = pd.ImageRef(id=1, file="photos/人物 A.JPG")
        self.assertEqual(pd.safe_filename(img), "人物 A.JPG")

    def test_filename_infers_extension_from_url(self):
        img = pd.ImageRef(id=2, file="", element_url="https://h/upload/2026/01/01/a-b.png")
        self.assertEqual(pd.safe_filename(img), "image-2.png")

    def test_filename_without_any_hint(self):
        self.assertEqual(pd.safe_filename(pd.ImageRef(id=3)), "image-3.jpg")


class CategoryPathTest(unittest.TestCase):
    def test_nested_path(self):
        cats = [
            {"id": 1, "name": "A", "id_uppercat": None},
            {"id": 2, "name": "B", "id_uppercat": "1"},
            {"id": 3, "name": "C", "id_uppercat": 2},
        ]
        paths = pd.build_category_paths(cats)
        self.assertEqual(paths[3], ["A", "B", "C"])

    def test_duplicate_sibling_names_get_suffix(self):
        cats = [
            {"id": 1, "name": "A", "id_uppercat": None},
            {"id": 2, "name": "X", "id_uppercat": 1},
            {"id": 3, "name": "X", "id_uppercat": 1},
        ]
        paths = pd.build_category_paths(cats)
        self.assertEqual(paths[2], ["A", "X"])
        self.assertEqual(paths[3], ["A", "X (3)"])


# --------------------------------------------------------------------------- #
# 地址规范化
# --------------------------------------------------------------------------- #
class NormalizeUrlTest(unittest.TestCase):
    def test_adds_scheme_and_strips_slash(self):
        self.assertEqual(pd.normalize_base_url("gallery.example.com/"), "https://gallery.example.com")

    def test_strips_entry_file_and_query(self):
        self.assertEqual(
            pd.normalize_base_url("https://h/index.php?/category/154"),
            "https://h",
        )
        self.assertEqual(pd.normalize_base_url("https://h/ws.php"), "https://h")

    def test_keeps_subdirectory(self):
        self.assertEqual(
            pd.normalize_base_url(" http://h/piwigo/index.php "), "http://h/piwigo"
        )


# --------------------------------------------------------------------------- #
# 响应解析（脏响应容错）
# --------------------------------------------------------------------------- #
class ParsePayloadTest(unittest.TestCase):
    def test_plain_json(self):
        data = pd.PiwigoClient.parse_payload('{"stat":"ok","result":{"a":1}}')
        self.assertEqual(data["result"]["a"], 1)

    def test_php_notice_prefix_is_skipped(self):
        noisy = (
            '<br />\n<b>Notice</b>:  Undefined offset: 150 in '
            '<b>/www/wwwroot/site/include/ws_functions/pwg.categories.php</b> '
            'on line <b>188</b><br />\n<br />\n'
            '{"stat":"ok","result":{"paging":{"page":1},"images":[]}}'
        )
        data = pd.PiwigoClient.parse_payload(noisy)
        self.assertIsNotNone(data)
        self.assertEqual(data["stat"], "ok")

    def test_html_error_returns_none(self):
        self.assertIsNone(pd.PiwigoClient.parse_payload("<html><body>502</body></html>"))
        self.assertIsNone(pd.PiwigoClient.parse_payload(""))


class FailingCategoryTest(unittest.TestCase):
    def test_broken_category_is_skipped(self):
        cats = [
            {"id": 1, "name": "A", "id_uppercat": None, "nb_images": 1},
            {"id": 2, "name": "B", "id_uppercat": None, "nb_images": 1},
        ]

        class Client:
            def iter_category_images(self, cat_id, per_page=100):
                if cat_id == 1:
                    raise pd.PiwigoError("接口 pwg.categories.getImages 返回非 JSON 内容")
                yield {"id": 20, "file": "20.jpg"}

        items = pd.collect_images(Client(), cats)
        self.assertEqual([i.image.id for i in items], [20])


# --------------------------------------------------------------------------- #
# 配置文件与本地索引
# --------------------------------------------------------------------------- #
class ConfigTest(unittest.TestCase):
    def parse(self, argv):
        return pd.build_parser().parse_args(argv)

    def test_defaults(self):
        s = pd.resolve_settings(self.parse(["https://x/"]), {})
        self.assertEqual(s.threads, 16)
        self.assertEqual(str(s.output), "downloads")
        self.assertTrue(s.use_index)

    def test_config_overrides_default(self):
        cfg = {"download": {"threads": 4, "output": "pics", "index": False}}
        s = pd.resolve_settings(self.parse(["https://x/"]), cfg)
        self.assertEqual(s.threads, 4)
        self.assertEqual(str(s.output), "pics")
        self.assertFalse(s.use_index)

    def test_cli_overrides_config(self):
        cfg = {"download": {"threads": 4}}
        s = pd.resolve_settings(self.parse(["https://x/", "--threads", "32"]), cfg)
        self.assertEqual(s.threads, 32)

    def test_site_url_from_config(self):
        s = pd.resolve_settings(self.parse([]), {"site": {"url": "https://cfg.example/"}})
        self.assertEqual(s.url, "https://cfg.example/")

    def test_toml_file_is_loaded(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "piwigo.toml"
            path.write_text('[download]\nthreads = 2\noutput = "o"\n', encoding="utf-8")
            cfg = pd.load_config_file(path)
            self.assertEqual(cfg["download"]["threads"], 2)


class LocalIndexTest(unittest.TestCase):
    def test_roundtrip_and_lookup(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "cat").mkdir()
            (out / "cat" / "a.jpg").write_bytes(b"x" * 10)
            idx = pd.LocalIndex(out / ".piwigo_index.json", "https://site")
            idx.record(1, "cat/a.jpg", 10)
            idx.save()

            again = pd.LocalIndex(out / ".piwigo_index.json", "https://site").load()
            self.assertEqual(len(again), 1)
            self.assertEqual(again.is_downloaded(1, out), (True, 10))

    def test_size_mismatch_is_not_downloaded(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "a.jpg").write_bytes(b"x" * 3)
            idx = pd.LocalIndex(out / ".piwigo_index.json", "https://site")
            idx.record(1, "a.jpg", 999)
            self.assertFalse(idx.is_downloaded(1, out)[0])

    def test_sites_are_isolated(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = out / ".piwigo_index.json"
            a = pd.LocalIndex(path, "https://a").load()
            a.record(1, "a.jpg", 1)
            a.save()
            b = pd.LocalIndex(path, "https://b").load()
            b.record(1, "b.jpg", 1)
            b.save()
            self.assertEqual(len(pd.LocalIndex(path, "https://a").load()), 1)
            self.assertEqual(len(pd.LocalIndex(path, "https://b").load()), 1)

    def test_verify_prunes_missing_files(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "keep.jpg").write_bytes(b"x" * 5)
            path = out / ".piwigo_index.json"
            idx = pd.LocalIndex(path, "https://site")
            idx.record(1, "keep.jpg", 5)
            idx.record(2, "gone.jpg", 5)
            idx.save()
            result = pd.verify_local_index(path, out)
            self.assertEqual(result["https://site"], {"ok": 1, "missing": 1, "mismatch": 0})
            self.assertEqual(len(pd.LocalIndex(path, "https://site").load()), 1)


class IndexedSkipTest(unittest.TestCase):
    def test_indexed_file_is_not_downloaded_again(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "cat").mkdir()
            (out / "cat" / "a.jpg").write_bytes(b"x" * 7)
            idx = pd.LocalIndex(out / ".piwigo_index.json", "https://site")
            idx.record(1, "cat/a.jpg", 7)

            class NoNetwork:
                verify_tls = True
                username = None

                def __init__(self):
                    import requests

                    self.session = requests.Session()
                    self.base_url = "https://site"

            dl = pd.Downloader(NoNetwork(), out, threads=1, progress=False, index=idx)
            item = pd.CatalogItem(
                image=pd.ImageRef(id=1, file="a.jpg", element_url="http://127.0.0.1:1/a.jpg"),
                rel_dir=Path("cat"),
            )
            status, size = dl.download_one(item)
            self.assertEqual((status, size), ("indexed", 7))


# --------------------------------------------------------------------------- #
# 图片地址选择
# --------------------------------------------------------------------------- #
class BestUrlTest(unittest.TestCase):
    def test_prefers_element_url(self):
        img = pd.ImageRef(id=1, element_url="u1", download_url="u2")
        self.assertEqual(img.best_url(), "u1")

    def test_falls_back_to_download_url(self):
        self.assertEqual(pd.ImageRef(id=1, download_url="u2").best_url(), "u2")

    def test_falls_back_to_largest_derivative(self):
        img = pd.ImageRef(
            id=1,
            derivatives={
                "small": {"url": "s", "width": 10, "height": 10},
                "xxlarge": {"url": "xl", "width": 800, "height": 900},
            },
        )
        self.assertEqual(img.best_url(), "xl")


# --------------------------------------------------------------------------- #
# 枚举去重
# --------------------------------------------------------------------------- #
class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def iter_category_images(self, cat_id, per_page=100):
        for raw in self.pages.get(cat_id, []):
            yield raw


class CollectImagesTest(unittest.TestCase):
    def setUp(self):
        self.cats = [
            {"id": 1, "name": "A", "id_uppercat": None, "nb_images": 2},
            {"id": 2, "name": "B", "id_uppercat": 1, "nb_images": 2},
        ]
        self.pages = {
            1: [{"id": 10, "file": "10.jpg"}, {"id": 11, "file": "11.jpg"}],
            # 11 同时属于子分类，必须去重
            2: [
                {"id": 11, "file": "11.jpg", "categories": [{"id": 2}]},
                {"id": 12, "file": "12.jpg", "categories": [{"id": 2}]},
            ],
        }

    def test_dedup_across_categories(self):
        items = pd.collect_images(FakeClient(self.pages), self.cats)
        self.assertEqual([i.image.id for i in items], [10, 11, 12])

    def test_max_images_limit(self):
        items = pd.collect_images(FakeClient(self.pages), self.cats, max_images=2)
        self.assertEqual(len(items), 2)

    def test_flat_mode_has_empty_dir(self):
        items = pd.collect_images(FakeClient(self.pages), self.cats, flat=True)
        self.assertTrue(all(str(i.rel_dir) == "." for i in items))

    def test_deepest_category_directory(self):
        items = pd.collect_images(FakeClient(self.pages), self.cats)
        deep = [i for i in items if i.image.id == 12][0]
        self.assertEqual(str(deep.rel_dir), str(Path("A") / "B"))


# --------------------------------------------------------------------------- #
# 探测与登录
# --------------------------------------------------------------------------- #
class FakeApiClient(pd.PiwigoClient):
    """不联网：只替换网络相关方法。"""

    def __init__(self, status="guest", categories=None):
        super().__init__("https://example.invalid")
        self._state = {"status": status, "username": status}
        self._categories = categories or []
        self.form_login_called = False

    def get_status(self, refresh=True):
        return dict(self._state)

    def list_categories(self, recursive=True):
        return list(self._categories)

    def api(self, method, *, http_method="GET", **params):
        return False

    def _form_login(self, username, password):
        self.form_login_called = True
        self._state = {"status": "normal", "username": username}


class ProbeTest(unittest.TestCase):
    def test_public_site_does_not_need_auth(self):
        cats = [{"id": 1, "name": "A", "nb_images": 5, "nb_categories": 0}]
        info = FakeApiClient(categories=cats).probe()
        self.assertFalse(info["needs_auth"])
        self.assertEqual(info["total_images"], 5)

    def test_empty_site_needs_auth(self):
        info = FakeApiClient().probe()
        self.assertTrue(info["needs_auth"])
        self.assertIn("分类", info["reason"])


class LoginTest(unittest.TestCase):
    def test_ws_login_failure_falls_back_to_form(self):
        client = FakeApiClient()
        self.assertTrue(client.login("alice", "secret"))
        self.assertTrue(client.form_login_called)
        self.assertTrue(client.logged_in)

    def test_login_failure_returns_false(self):
        client = FakeApiClient()

        def not_logged_in(username, password):
            client.form_login_called = True

        client._form_login = not_logged_in
        self.assertFalse(client.login("alice", "bad"))
        self.assertFalse(client.logged_in)


# --------------------------------------------------------------------------- #
# 下载器（本地 HTTP 服务，测 Range 续传与跳过）
# --------------------------------------------------------------------------- #
PAYLOAD = bytes(range(256)) * 40  # 10240 字节


class RangeHandler(BaseHTTPRequestHandler):
    seen_ranges: list[str] = []
    fail_once = {"done": False}

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/broken"):
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng:
            RangeHandler.seen_ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0])
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


class DummyClient:
    def __init__(self, base_url):
        import requests

        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "test"})
        self.verify_tls = True
        self.username = None


class DownloaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def make_item(self, image_id=1, name="a.jpg"):
        img = pd.ImageRef(id=image_id, file=name, element_url=f"{self.base}/img/{name}")
        return pd.CatalogItem(image=img, rel_dir=Path("cat"))

    def test_download_then_skip(self):
        with TemporaryDirectory() as tmp:
            dl = pd.Downloader(DummyClient(self.base), Path(tmp), threads=2, progress=False)
            item = self.make_item()
            stats = dl.run([item])
            self.assertEqual((stats.done, stats.skipped, stats.failed), (1, 0, 0))
            target = Path(tmp) / "cat" / "a.jpg"
            self.assertEqual(target.read_bytes(), PAYLOAD)
            stats2 = dl.run([item])
            self.assertEqual((stats2.done, stats2.skipped), (0, 1))

    def test_resume_from_part_file(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "cat"
            out.mkdir(parents=True)
            part = out / "b.jpg.part"
            part.write_bytes(PAYLOAD[:1000])
            RangeHandler.seen_ranges.clear()
            dl = pd.Downloader(DummyClient(self.base), Path(tmp), threads=1, progress=False)
            stats = dl.run([self.make_item(2, "b.jpg")])
            self.assertEqual(stats.done, 1)
            self.assertEqual((out / "b.jpg").read_bytes(), PAYLOAD)
            self.assertTrue(any(r.startswith("bytes=1000-") for r in RangeHandler.seen_ranges))

    def test_same_name_different_id_does_not_overwrite(self):
        with TemporaryDirectory() as tmp:
            dl = pd.Downloader(DummyClient(self.base), Path(tmp), threads=1, progress=False)
            items = [self.make_item(10, "same.jpg"), self.make_item(11, "same.jpg")]
            stats = dl.run(items)
            self.assertEqual(stats.done, 2)
            files = sorted(p.name for p in (Path(tmp) / "cat").iterdir())
            self.assertEqual(files, ["same.jpg", "same~11.jpg"])

    def test_404_is_counted_as_failure(self):
        with TemporaryDirectory() as tmp:
            img = pd.ImageRef(id=99, file="x.jpg", element_url=f"{self.base}/broken/x.jpg")
            dl = pd.Downloader(
                DummyClient(self.base), Path(tmp), threads=1, retries=0, progress=False
            )
            stats = dl.run([pd.CatalogItem(image=img, rel_dir=Path("cat"))])
            self.assertEqual(stats.failed, 1)
            self.assertFalse((Path(tmp) / "cat" / "x.jpg").exists())

    def test_manifest_written(self):
        with TemporaryDirectory() as tmp:
            dl = pd.Downloader(DummyClient(self.base), Path(tmp), threads=1, progress=False)
            items = [self.make_item(1, "m.jpg")]
            stats = dl.run(items)
            path = dl.write_manifest(items, stats)
            self.assertTrue(path.exists())
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["file"], "m.jpg")


if __name__ == "__main__":
    unittest.main(verbosity=2)

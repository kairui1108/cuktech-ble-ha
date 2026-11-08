"""静态资源缓存契约（前端更新的"最后一公里"）。

背景 bug：`/static/**` 以前对所有非 HTML 文件回
`Cache-Control: public, max-age=604800, immutable`，而语言包就是普通 JS。前端新增
了一个 i18n key（`config.clearPrimary` 等）、却没有同步 bump HTML 里的 `?v=` 计数器，
老用户的浏览器就会拿 **7 天前的语言包**配新页面——按钮上直接显示 `config.clearPrimary`。

两层修法，这里各锁一条：

1. **ETag 条件请求**：JS/CSS/HTML 一律 `no-cache`，带 `If-None-Match` 时回 304。
   忘 bump 也只是多一次 304，页面不会再"新 HTML + 旧 JS"。
2. **`?v=` 一致性**：立即生效的 cache busting 仍然保留（老缓存里是 `?v=27`，
   新页面换成 `?v=28` 才会真的绕过它），但必须所有页面一起改——只改一半照样复发。
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import ha_server  # noqa: E402
from golden_harness import mock_request  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"

#: 页面里对语言包的引用（config.html / index.html / phone.html / static/*.html 都有）
LOCALE_REF = re.compile(
    r'(?:src|href)="(/static/locales/(?:zh-CN|en)\.js)\?v=(\d+)"')


@pytest.fixture()
def static_cache():
    """用真实 web 目录把内存静态缓存建起来，测试结束后恢复原缓存。"""
    old = ha_server._static_cache
    ha_server._static_cache = {}
    try:
        ha_server._cache_static_files()
        yield ha_server._static_cache
    finally:
        ha_server._static_cache = old


async def _get(path, **headers):
    return await ha_server.handle_cached_static(mock_request("GET", path, headers=headers))


class TestStaticCacheHeaders:
    async def test_js_revalidates_with_etag(self, static_cache):
        """语言包必须可条件请求：新增 key 后老用户下一次加载就能拿到。"""
        resp = await _get("/static/locales/zh-CN.js")
        assert resp.status == 200
        etag = resp.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"'), etag
        assert resp.headers["Cache-Control"] == "no-cache", \
            "JS 不能再是 immutable：否则忘 bump ?v= 时用户会拿着一周前的语言包"

        again = await _get("/static/locales/zh-CN.js", **{"If-None-Match": etag})
        assert again.status == 304, "没改过的语言包应该 304，而不是每次重下 21KB"
        assert again.headers["ETag"] == etag
        assert again.headers["Cache-Control"] == "no-cache"

    async def test_html_page_revalidates_with_etag(self, static_cache):
        resp = await _get("/config.html")
        assert resp.status == 200
        etag = resp.headers["ETag"]
        assert resp.headers["Cache-Control"] == "no-cache"
        again = await _get("/config.html", **{"If-None-Match": etag})
        assert again.status == 304

    async def test_gzip_variant_keeps_the_same_etag_and_advertises_vary(self, static_cache):
        plain = await _get("/static/app.js")
        gz = await _get("/static/app.js", **{"Accept-Encoding": "gzip, deflate"})
        assert gz.headers.get("Content-Encoding") == "gzip"
        assert gz.headers["ETag"] == plain.headers["ETag"], \
            "压缩与未压缩是同一份内容，ETag 必须一样（否则缓存会互相打架）"
        # 同一个 URL 两种表示（gzip/原文）：必须 Vary，否则共享缓存会把 gzip 体
        # 发给不支持 gzip 的客户端（304 也要带，RFC 9110 要求与 200 一致）。
        assert "Accept-Encoding" in plain.headers.get("Vary", "")
        assert "Accept-Encoding" in gz.headers.get("Vary", "")
        # 带 ETag 的 gzip 请求同样 304
        again = await _get("/static/app.js",
                           **{"Accept-Encoding": "gzip", "If-None-Match": gz.headers["ETag"]})
        assert again.status == 304
        assert "Accept-Encoding" in again.headers.get("Vary", ""), "304 也要带 Vary"

    async def test_index_varies_by_user_agent(self, static_cache):
        """首页按 UA 分发 index/phone：不 Vary 的话缓存会把手机版发给桌面端。"""
        server = ha_server.Server.__new__(ha_server.Server)
        resp = await server.handle_index(mock_request(
            "GET", "/", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"}))
        assert resp.headers["Content-Type"] == "text/html"
        assert "User-Agent" in resp.headers.get("Vary", "")

    async def test_images_still_long_cached(self, static_cache):
        """图片/字体不承载页面逻辑：继续 immutable，省掉条件请求。"""
        image = next((k for k in static_cache
                      if k.startswith("/static/")
                      and k.endswith((".png", ".webp", ".ico", ".woff2"))), None)
        assert image, "web/static 下应当至少有一张图片/字体"
        resp = await _get(image)
        assert "immutable" in resp.headers["Cache-Control"], resp.headers["Cache-Control"]


class TestLazyStaticCache:
    """大图片/字体不常驻内存，但请求语义必须和常驻条目完全一致。

    背景：`_cache_static_files` 以前把所有文件（含 2MB+ 的 PNG）都 `read_bytes()`
    留在内存里，而图片是长缓存、实际请求很少。改成惰性条目后，最容易回归的是：
    ETag 条件请求仍要 304、body 仍要完整、缓存头仍要 immutable。
    """

    def test_large_images_are_not_resident(self, static_cache):
        lazy = {k: v for k, v in static_cache.items() if "path" in v}
        assert lazy, "web/static 下应当有超过阈值的图片，走惰性路径"
        for key, entry in lazy.items():
            assert "raw" not in entry, f"{key} 不该常驻内存"
            assert "gzipped" not in entry
            assert entry["etag"] and entry["content_type"] and entry["path"]

    def test_small_icons_stay_resident(self, static_cache):
        resident_images = [
            v for k, v in static_cache.items()
            if k.endswith((".png", ".ico", ".webp"))
            and "raw" in v
        ]
        assert resident_images, "小图标应继续常驻（避免每次页面加载都碰磁盘）"
        assert all(len(v["raw"]) < ha_server._LAZY_MIN_BYTES
                   for v in resident_images)

    async def test_lazy_image_body_comes_from_disk(self, static_cache):
        path, entry = next((k, v) for k, v in static_cache.items() if "path" in v)
        on_disk = Path(entry["path"]).read_bytes()
        resp = await _get(path)
        assert resp.status == 200
        assert resp.body == on_disk, "惰性条目的 body 必须与磁盘内容逐字节一致"
        assert resp.headers["Content-Type"].startswith("image/")
        assert resp.headers["Content-Length"] == str(len(on_disk))
        assert "immutable" in resp.headers["Cache-Control"]

    async def test_lazy_conditional_request_does_not_touch_disk(
            self, static_cache, monkeypatch):
        path, entry = next((k, v) for k, v in static_cache.items() if "path" in v)

        def boom(self):
            raise AssertionError("304 命中 ETag 时不该读磁盘")

        monkeypatch.setattr(Path, "read_bytes", boom)
        resp = await _get(path, **{"If-None-Match": entry["etag"]})
        assert resp.status == 304
        assert resp.headers["ETag"] == entry["etag"]

    async def test_lazy_image_missing_on_disk_is_404(self, static_cache, tmp_path):
        path, entry = next((k, v) for k, v in static_cache.items() if "path" in v)
        entry["path"] = str(tmp_path / "gone.png")   # 启动后被删/被替换
        with pytest.raises(ha_server.web.HTTPNotFound):
            await _get(path)


def test_locale_versions_consistent_across_pages():
    """所有页面引用同一个语言包时，`?v=` 必须一致（改一半等于没改）。

    `?v=` 是 ETag 之外**立即生效**的那一层：浏览器里已经躺着的旧 URL 只有换号才
    会重新请求。`config.html` 换成 28、别处还是 27，老用户照样踩同一个坑。
    """
    pages = sorted(WEB.glob("*.html")) + sorted((WEB / "static").glob("*.html"))
    seen: dict[str, set] = {}
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="replace")
        for path, version in LOCALE_REF.findall(text):
            seen.setdefault(path, set()).add((version, page.name))

    assert seen, "没有任何页面引用语言包？i18n 接线断了吗"
    problems = {path: sorted(hits) for path, hits in seen.items()
                if len({v for v, _ in hits}) > 1}
    assert not problems, f"语言包 ?v= 在各页面不一致：{problems}"

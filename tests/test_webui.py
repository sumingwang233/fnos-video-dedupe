import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import webui


class WebuiAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        media = base / "media"
        media.mkdir()
        (media / "a.mp4").write_bytes(b"x" * 16)
        config = base / "config.json"
        config.write_text(json.dumps({"state_dir": str(base / "state"), "roots": [{"id": "m", "path": str(media), "role": "cleanup", "exclude": []}]}), encoding="utf-8")
        webui.Handler.config = str(config)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        self.server.gateway = True  # 模拟统一网关：身份头由 fnOS 注入
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base_url + path, data=b"" if method == "POST" else None, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_read_pages_need_no_admin(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("视频查重助手", body)
        # 回归防护：属性引号不闭合会让浏览器吞掉整个文档（白屏）
        self.assertEqual(body.count('"') % 2, 0, "引号数量为奇数，存在未闭合属性")
        self.assertIn("</title>", body.split("<style>")[0])

    def test_action_without_admin_header_is_403(self):
        status, body = self.request("/cancel", method="POST")
        self.assertEqual(status, 403)
        self.assertIn("管理员", body)

    def test_action_with_admin_header_passes(self):
        status, body = self.request("/cancel", method="POST", headers={"X-Trim-Isadmin": "true"})
        self.assertEqual(status, 200)
        self.assertIn("已请求取消", body)

    def test_gateway_prefix_routes_and_renders(self):
        status, body = self.request("/app/video-dedupe")
        self.assertEqual(status, 200)
        self.assertIn("href='/app/video-dedupe/'", body)

    def test_status_pages_keep_polling(self):
        # 回归防护：0.2.17/0.2.21 曾两次因页面重构丢掉轮询脚本，状态页渲染后即静止。
        webui.PLAN_STATE["running"] = True
        try:
            status, body = self.request("/plan-status")
            self.assertEqual(status, 200)
            self.assertIn("vdPoll", body)
        finally:
            webui.PLAN_STATE["running"] = False
        webui.APPLY_STATE["running"] = True
        try:
            status, body = self.request("/apply-status")
            self.assertEqual(status, 200)
            self.assertIn("vdPoll", body)
        finally:
            webui.APPLY_STATE["running"] = False

    def test_ui_baseline_guards(self):
        # 0.2.26 UI 精修基线：焦点可达、动效可关、窄屏适配不回退。
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(":focus-visible", body)
        self.assertIn("prefers-reduced-motion", body)
        self.assertIn("@media", body)

    def test_strata_theme_and_shell(self):
        # 0.2.27 Strata 换装基线：默认浅色（显式 dark 才深）、顶栏 tab 导航、
        # 全局状态胶囊常驻、自托管字体、概览指标卡骨架。
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("localStorage.vdTheme==='dark'", body)   # 默认浅色迁移语义
        self.assertIn("html.dark{", body)                       # 深色令牌组
        self.assertIn("class=tabs", body)                       # 顶栏分段导航
        self.assertIn("id=st-pill", body)                       # 全局状态胶囊
        self.assertIn("@font-face", body)                       # 自托管 Outfit
        self.assertIn("/font?f=latin", body)
        self.assertIn("class=chain", body)                      # 概览阶段链
        self.assertIn("class=metrics", body)                    # 概览指标卡

    def test_font_route(self):
        # /font 服务随包 woff2（dev 布局回退 fpk/app/fonts），未知名 404。
        status, body = self.request("/font?f=latin")
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 10000)  # 真 woff2 约 32KB；404 页不会这么大
        status, _ = self.request("/font?f=unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

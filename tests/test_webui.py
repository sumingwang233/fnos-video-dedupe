import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import webui
import videodedupe as v
import tasks

ADMIN_HDR = {"X-Trim-Isadmin": "true"}  # 模拟网关注入的管理员身份头


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跟随：让用例直接断言 303 状态码与 Location（§四B PRG 契约）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(base_url, path, method="GET", headers=None, data=None):
    if isinstance(data, str):
        data = data.encode("utf-8")
    body = data if data is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_noredirect(base_url, path, method="GET", headers=None, data=None):
    """同 fetch，但不跟随重定向；返回 (status, location, body)。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    body = data if data is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.headers.get("Location") or "", r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers or {}).get("Location") or "", e.read().decode("utf-8", "replace")


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

    def request(self, path, method="GET", headers=None, data=None):
        return fetch(self.base_url, path, method=method, headers=headers, data=data)

    def request_noredirect(self, path, method="GET", headers=None, data=None):
        return fetch_noredirect(self.base_url, path, method=method, headers=headers, data=data)

    def _csrf(self):
        """从首页扫描表单提取服务端注入的 CSRF 令牌（顺带验证注入生效）。"""
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        m = re.search(r"name=csrf value='([0-9a-f]{16,})'", body)
        self.assertIsNotNone(m, "POST 表单未注入 CSRF 令牌")
        return m.group(1)

    def test_scan_post_rejects_scope_not_bound_to_authorized_root(self):
        # 扫描范围必须绑定授权根身份：未知根、绝对路径相对段一律拒绝，且不启动扫描
        token = self._csrf()
        for payload in ("scope=ghost%3Asub", "scope=m%3A%2Fetc%2Fpasswd"):
            with self.subTest(payload=payload):
                status, body = self.request("/scan", method="POST", headers=ADMIN_HDR,
                                            data="csrf=" + token + "&" + payload)
                self.assertEqual(status, 400)
                self.assertIn("安全检查", body)

    def test_scan_post_pick_mode_without_selection_rejected(self):
        # §三.1：自选模式未勾选任何子文件夹时禁止启动——服务端在占用任务前拒绝，
        # 绝不静默回退全盘扫描；未知模式值同样 400，且都不留任务行。
        cfg = v.read_config(webui.Handler.config)
        token = self._csrf()
        status, body = self.request("/scan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scope_mode=pick")
        self.assertEqual(status, 400)
        self.assertIn("还没有选择要扫描的子文件夹", body)
        status, body = self.request("/scan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scope_mode=both")
        self.assertEqual(status, 400)
        self.assertIn("未知的扫描范围模式", body)
        self.assertIsNone(tasks.active(cfg))  # 拒绝发生在占用任务之前，不留任务行

    def test_home_renders_explicit_scope_modes(self):
        # §三.1：首页扫描范围显式双模式（全部/自选单选），旧的「未勾选=全部」隐式文案移除
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("name=vd-scan-mode", body)
        self.assertIn("value=all", body)
        self.assertIn("value=pick", body)
        self.assertIn("扫描全部已授权文件夹", body)
        self.assertIn("只扫描勾选的子文件夹", body)
        self.assertNotIn("未勾选时扫描全部", body)
        self.assertNotIn("不勾选则扫描全部", body)

    def test_home_shows_draft_ownership_note(self):
        # §三.1：已有审阅草稿时，扫描范围区说明旧草稿归属（绑旧扫描号，新扫描不沿用）
        cfg = v.read_config(webui.Handler.config)
        with v.state(cfg) as db:
            db.execute("INSERT INTO scans(id, created) VALUES('scan-draft-own', '2026-10-13T10:00:00Z')")
            db.execute("INSERT INTO drafts(scan, rev, data, updated) VALUES('scan-draft-own', 1, '{}', '2026-10-13T11:00:00Z')")
            db.commit()  # state() 不自动提交，未 commit 的插入会被回滚
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("不会沿用旧选择", body)
        self.assertIn("scan-draft-o", body)  # 编号前缀（页面 12 位截断展示）

    def test_read_pages_require_admin(self):
        # Review 26.10.12 P1/§四E：业务 GET（含缩略图）一律管理员，匿名被拒；
        # 匿名可读面只剩 PUBLIC_GET_PATHS（静态字体，见 test_font_route）。
        for path in ("/", "/thumb?scan=s&f=x"):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 403)
                self.assertIn("管理员", body)
        status, body = self.request("/", headers=ADMIN_HDR)
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
        # §四B：POST 成功一律 303（PRG）；管理员头缺失才是 403。无运行任务时
        # 取消幂等成功，跳回概览。
        token = self._csrf()
        status, loc, _ = self.request_noredirect("/cancel", method="POST", headers=ADMIN_HDR, data="csrf=" + token)
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/")

    def test_csrf_required_for_post(self):
        # Review 26.10.12 §四E：POST 缺失/错误令牌一律 403，管理员头也不能豁免。
        status, body = self.request("/cancel", method="POST", headers=ADMIN_HDR)
        self.assertEqual(status, 403)
        self.assertIn("安全校验", body)
        status, body = self.request("/cancel", method="POST", headers=ADMIN_HDR, data="csrf=deadbeef")
        self.assertEqual(status, 403)
        self.assertIn("安全校验", body)

    def test_post_body_size_limit(self):
        # 请求体超上限直接 413，不读入内存（测试里临时调小上限）
        token = self._csrf()
        old = webui.MAX_BODY_BYTES
        webui.MAX_BODY_BYTES = 32
        try:
            status, body = self.request("/cancel", method="POST", headers=ADMIN_HDR,
                                        data="csrf=" + token + "&pad=" + "x" * 200)
            self.assertEqual(status, 413)
            self.assertIn("请求体过大", body)
        finally:
            webui.MAX_BODY_BYTES = old

    def test_post_plan_missing_scan_param_is_400(self):
        # 明确的参数错误响应：缺 scan 参数直接 400，不进后台任务
        token = self._csrf()
        status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&sel=m%3Ax")
        self.assertEqual(status, 400)
        self.assertIn("缺少", body)

    def test_report_post_is_explicit_and_prG(self):
        # §四F 报告导出独立操作：POST 303 回概览并提示导出位置（PRG，刷新不重提）；
        # 导出不进任务表、不改业务状态；非法扫描号按安全检查拒绝；无扫描可导出时明确 400。
        cfg = v.read_config(webui.Handler.config)
        scan = v.inventory(cfg)
        token = self._csrf()
        status, loc, _ = self.request_noredirect("/report", method="POST", headers=ADMIN_HDR,
                                                 data="csrf=" + token)
        self.assertEqual(status, 303)  # 不带 scan → 取最近一次扫描
        self.assertIn("/?report=done&scan=" + scan["scan_id"], loc)
        status, body = self.request(loc, headers=ADMIN_HDR)  # 跟随 303 后的概览页
        self.assertIn("审计报告已导出", body)
        self.assertIsNone(tasks.active(cfg))  # 导出不占用任务行
        self.assertEqual(tasks.latest(cfg, "scan")["status"], tasks.SUCCESS)  # 业务状态不被报告操作改写
        status, body = self.request("/report", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scan=ghost")
        self.assertEqual(status, 400)
        self.assertIn("安全检查", body)

    def test_report_post_without_any_scan_is_400(self):
        token = self._csrf()
        status, body = self.request("/report", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token)
        self.assertEqual(status, 400)
        self.assertIn("没有可导出的扫描", body)

    def test_tcp_mode_ignores_forged_admin_header(self):
        # Review 26.10.12 §四E：X-Trim-* 注入头只在网关 socket 模式可信；
        # --tcp 开发模式下客户端伪造管理员头不得提权。VD_ALLOW_NO_ADMIN=1 是
        # 本机（127.0.0.1 监听）开发逃生阀，显式开启才放行。
        server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        server.gateway = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with mock.patch.dict(os.environ):
                os.environ.pop("VD_ALLOW_NO_ADMIN", None)
                status, _ = fetch(base, "/", headers=ADMIN_HDR)
                self.assertEqual(status, 403)
                status, _ = fetch(base, "/cancel", method="POST", headers=ADMIN_HDR, data=b"csrf=x")
                self.assertEqual(status, 403)
            with mock.patch.dict(os.environ, {"VD_ALLOW_NO_ADMIN": "1"}):
                status, _ = fetch(base, "/", headers=ADMIN_HDR)
                self.assertEqual(status, 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_thumb_scoped_to_scan_and_authorized_files(self):
        # 缩略图不接受任意路径：标识只用于查库，查不到记录或含路径成分一律 404
        for query in ("?scan=nope&f=whatever", "?scan=nope&f=..%2F..%2Fetc%2Fpasswd", "?scan=&f=x", "?scan=x&f="):
            with self.subTest(query=query):
                status, _ = self.request("/thumb" + query, headers=ADMIN_HDR)
                self.assertEqual(status, 404)

    def test_gateway_prefix_routes_and_renders(self):
        status, body = self.request("/app/video-dedupe", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("href='/app/video-dedupe/'", body)

    def test_status_pages_keep_polling(self):
        # 回归防护：0.2.17/0.2.21 曾两次因页面重构丢掉轮询脚本，状态页渲染后即静止。
        # 状态源是任务记录（§四B）：用真实占用驱动，而不是进程内布尔。
        cfg = v.read_config(webui.Handler.config)
        tasks.claim(cfg, "plan")
        try:
            status, body = self.request("/plan-status", headers=ADMIN_HDR)
            self.assertEqual(status, 200)
            self.assertIn("vdPoll", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")
        tasks.claim(cfg, "apply")
        try:
            status, body = self.request("/apply-status", headers=ADMIN_HDR)
            self.assertEqual(status, 200)
            self.assertIn("vdPoll", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_ui_baseline_guards(self):
        # 0.2.26 UI 精修基线：焦点可达、动效可关、窄屏适配不回退。
        # §四E 抽取后样式在 /static/app.css，基线断言跟着走（页面只留外链）。
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("href='/static/app.css'", body)
        status, css = self.request("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media", css)

    def test_strata_theme_and_shell(self):
        # 0.2.27 Strata 换装基线：默认浅色（显式 dark 才深）、顶栏 tab 导航、
        # 全局状态胶囊常驻、自托管字体、概览指标卡骨架。
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("localStorage.vdTheme==='dark'", body)   # 默认浅色迁移语义（防闪烁引导须内联）
        self.assertIn("class=tabs", body)                       # 顶栏分段导航
        self.assertIn("id=st-pill", body)                       # 全局状态胶囊
        self.assertIn("class=chain", body)                      # 概览阶段链
        self.assertIn("class=metrics", body)                    # 概览指标卡
        status, css = self.request("/static/app.css")           # 令牌组与字体在样式表（§四E）
        self.assertEqual(status, 200)
        self.assertIn("html.dark{", css)                        # 深色令牌组
        self.assertIn("@font-face", css)                        # 自托管 Outfit
        self.assertIn("../font?f=latin", css)                   # 相对引用：{px}/static/ 下 ../font 即 {px}/font

    def test_font_route(self):
        # /font 服务随包 woff2（dev 布局回退 fpk/app/fonts），未知名 404。
        status, body = self.request("/font?f=latin")
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 10000)  # 真 woff2 约 32KB；404 页不会这么大
        status, _ = self.request("/font?f=unknown")
        self.assertEqual(status, 404)

    def test_static_route_public_css_admin_js_whitelist(self):
        # Review 26.10.12 §四E：CSS/JS 抽为随包静态资源经 /static/ 提供。
        # 公开匿名面 = 字体 + 样式表；app.js 属业务页面资源，与管理员页面同权限；
        # 文件名白名单精确匹配，白名单外（含任何路径成分）一律 404。
        status, css = self.request("/static/app.css")           # 匿名可读
        self.assertEqual(status, 200)
        self.assertGreater(len(css), 1000)
        self.assertIn(":root{", css)
        req = urllib.request.Request(self.base_url + "/static/app.css")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers.get("Content-Type", "").startswith("text/css"))
        status, _ = self.request("/static/app.js")              # 脚本不在公开面
        self.assertEqual(status, 403)
        status, js = self.request("/static/app.js", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("function vdPoll", js)
        self.assertIn("VD_PX+'/progress'", js)                  # 接口 URL 经页面前缀拼接，无硬编码路径
        for name in ("nope.css", "../webui.py", "sub/app.css", "app.js%00"):
            with self.subTest(name=name):
                status, _ = self.request("/static/" + name, headers=ADMIN_HDR)
                self.assertEqual(status, 404)
        # 资源引用必须经 self.px 生成：网关前缀下渲染为带完整前缀的 URL
        status, body = self.request("/app/video-dedupe/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("href='/app/video-dedupe/static/app.css'", body)
        self.assertIn("src='/app/video-dedupe/static/app.js'", body)
        self.assertIn("var VD_PX='/app/video-dedupe'", body)

    def test_version_display_sourced_from_manifest(self):
        # Review 26.10.12 §四F：运行时显示版本以 fpk/manifest 为唯一来源
        # （videodedupe._manifest_version 解析），顶栏与设置页不再手抄版本号。
        manifest = Path(__file__).resolve().parent.parent / "fpk" / "manifest"
        version = next(line.split("=", 1)[1].strip() for line in
                       manifest.read_text(encoding="utf-8").splitlines()
                       if line.startswith("version="))
        self.assertEqual(v.VERSION, version)
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn(f"<span class=ver>v{version}</span>", body)
        status, body = self.request("/config", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn(f"应用版本 <code>{version}</code>", body)

    def _write_progress(self, data):
        state = Path(self.tmp.name) / "state"
        state.mkdir(exist_ok=True)
        (state / "progress.json").write_text(json.dumps(data), encoding="utf-8")

    def test_submit_guards_never_rely_on_onsubmit_return_value(self):
        # Review 26.10.12 P1：内联 onsubmit='return …' 依赖同步布尔；async 处理器返回
        # Promise 恒真值，用户取消确认仍会提交。所有确认表单改为显式 submit 监听 +
        # preventDefault，页面不得再出现挂 onsubmit 的表单标签。
        bodies = []
        # 空闲首页：开始/重新扫描表单
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        bodies.append(body)
        self.assertNotRegex(body, r"<form[^>]*\bonsubmit")          # 不依赖内联返回值
        self.assertNotIn("async function startScopedScan", body)    # 根因：async 必返 Promise
        self.assertIn("form[data-scan-scope]", body)                # 扫描表单显式监听挂接
        self.assertIn("preventDefault", body)                       # 取消路径阻断提交
        status, js = self.request("/static/app.js", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("getAttribute('data-confirm')", js)           # 静态确认守卫随 §四E 移入 app.js
        # 扫描中首页：取消扫描表单（§四B：运行态来自任务记录，取消表单绑定任务 ID）
        cfg = v.read_config(webui.Handler.config)
        self._write_progress({"phase": "fingerprint", "done": 1, "total": 2, "current": ""})
        task = tasks.claim(cfg, "scan")
        try:
            status, body = self.request("/", headers=ADMIN_HDR)
            self.assertEqual(status, 200)
            bodies.append(body)
            self.assertIn("data-confirm='确定要取消这次扫描吗？", body)
            self.assertIn(f"name=task value='{task['id']}'", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")
        # 计划就绪的确认页（无任务行 → 旧 progress 兼容路径）：执行入口在平台
        # 验证通过前禁用（§四A/§五）——只有禁用卡，没有提交表单。
        self._write_progress({"phase": "plan_ready", "plan_id": "t0", "delete_files": 1, "logical_bytes": 8})
        status, body = self.request("/plan-review", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        bodies.append(body)
        self.assertIn("待平台验证，暂不可用", body)
        self.assertNotIn("method=post action='/apply'", body)
        for body in bodies:
            self.assertNotRegex(body, r"<form[^>]*\bonsubmit")

    def test_apply_result_empty_states_are_honest(self):
        # Review 26.10.12 P1：没有结果时结果页不得渲染「清理完成/已删除 0 个」的
        # 成功页，须按 运行中/被中断/无结果 如实分流。状态源是任务记录（§四B）。
        # 外壳状态胶囊 JS 含「清理完成」字样（apply_done 标签），成功页判定用
        # 结果卡特有文案，不用裸词。
        no_success = (" 清理完成</h1>", "已删除 <b>", "释放约")
        cfg = v.read_config(webui.Handler.config)
        # 全新状态：如实说明没有结果，而不是「成功清理 0 个」
        status, body = self.request("/apply-result", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("当前没有删除结果", body)
        for marker in no_success:
            self.assertNotIn(marker, body)
        # 磁盘停在 applying 且无任务记录：CLI 崩溃/旧版残留 → 如实按中断呈现
        self._write_progress({"phase": "applying", "done": 1, "total": 2, "current": "", "freed": 8})
        status, body = self.request("/apply-result", headers=ADMIN_HDR)
        self.assertEqual(status, 503)
        self.assertIn("上次删除执行被中断", body)
        self.assertNotIn("已删除 <b>", body)
        # 执行中（任务行 running）：处理中提示并指向进度页
        tasks.claim(cfg, "apply", plan_id="t" * 64)
        try:
            status, body = self.request("/apply-result", headers=ADMIN_HDR)
            self.assertEqual(status, 503)
            self.assertIn("删除正在执行中", body)
            self.assertIn("/apply-status", body)
            self.assertNotIn("已删除 <b>", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_apply_result_renders_persisted_task_result(self):
        # §四B「完成结果可能丢失」的修复：apply 的完整结果持久化在任务行里，
        # 页面从任务记录渲染（本进程之外/重启后仍可读）。
        cfg = v.read_config(webui.Handler.config)
        tasks.claim(cfg, "apply", plan_id="t" * 64)
        tasks.finish(cfg, tasks.SUCCESS, result={
            "dry_run": False, "plan_id": "t" * 64, "deleted_files": 2, "logical_bytes": 4 * 2**20,
            "free_bytes_before": {"m": 2**30}, "free_bytes_after": {"m": 2 * 2**30},
            "journal": str(Path(self.tmp.name) / "state" / "t.journal.json"), "note": "x"})
        status, body = self.request("/apply-result", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("清理完成", body)
        self.assertIn("已删除 <b>2</b> 个文件", body)

    def test_apply_result_aggregates_per_root_space_dicts(self):
        # Review 26.10.12 P1：引擎 free_bytes_before/after 是 {root_id: 剩余字节}
        # （videodedupe.free_space 按授权目录组织），结果页必须遍历汇总而不是
        # 当作单个数字相除——旧实现在这里 TypeError 变 500。结果来自任务行。
        base = Path(self.tmp.name)
        for name in ("mediaA", "mediaB"):
            (base / name).mkdir(exist_ok=True)
            (base / name / "v.mp4").write_bytes(b"x" * 16)
        (base / "config.json").write_text(json.dumps({
            "state_dir": str(base / "state"),
            "roots": [{"id": "a", "path": str(base / "mediaA"), "role": "cleanup", "exclude": []},
                      {"id": "b", "path": str(base / "mediaB"), "role": "cleanup", "exclude": []}]}), encoding="utf-8")
        cfg = v.read_config(base / "config.json")
        gib = 2**30
        tasks.claim(cfg, "apply", plan_id="t" * 64)
        tasks.finish(cfg, tasks.SUCCESS, result={
            "dry_run": False, "plan_id": "t" * 64, "deleted_files": 3, "logical_bytes": 5 * 2**20,
            "free_bytes_before": {"a": 2 * gib, "b": 2 * gib, "gone": 10 * gib},
            "free_bytes_after": {"a": 6 * gib, "b": 6 * gib, "gone": 10 * gib},
            "journal": str(base / "state" / "t.journal.json"),
            "note": "空闲容量变化仅供参考"})
        status, body = self.request("/apply-result", headers=ADMIN_HDR)
        self.assertEqual(status, 200)                      # 旧实现在 dict/2**30 处 TypeError→500
        self.assertIn("清理完成", body)
        self.assertIn("已删除 <b>3</b> 个文件", body)
        self.assertIn("释放约 5 MB", body)
        # 每个授权目录一行；不在当前配置里的根如实标注，不参与文件系统去重
        self.assertIn("mediaA", body)
        self.assertIn("mediaB", body)
        self.assertIn("已移除的目录 gone", body)
        self.assertIn("6.0 GB", body)
        # a/b 同处一个文件系统（同一临时盘）合计只计一次，gone 独立计入（+0）：
        self.assertIn("合计（同一文件系统只计一次）", body)
        self.assertIn("+4,096 MB", body)

    def test_post_scan_busy_returns_current_task(self):
        # §四B：重复提交返回明确忙碌状态并指向当前任务；互斥靠任务占用+操作锁。
        token = self._csrf()
        cfg = v.read_config(webui.Handler.config)
        tasks.claim(cfg, "scan")
        try:
            status, body = self.request("/scan", method="POST", headers=ADMIN_HDR, data="csrf=" + token)
            self.assertEqual(status, 503)
            self.assertIn("正在进行中", body)
            # 其他类型入口共用同一互斥：计划/删除同样被拒
            status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                        data="csrf=" + token + "&scan=0123456789abcdef0123456789abcdef&sel=m%3Ax")
            self.assertEqual(status, 503)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")
        self.assertIsNotNone(tasks.claim(cfg, "scan"))  # 任务终结后工作区恢复可用
        tasks.finish(cfg, tasks.FAILED, error="test")

    def test_cancel_binds_specific_task(self):
        # §四B：停止绑定具体任务 ID，不能取消其他任务；非扫描任务不支持在线停止。
        token = self._csrf()
        cfg = v.read_config(webui.Handler.config)
        task = tasks.claim(cfg, "scan")
        try:
            status, body = self.request("/cancel", method="POST", headers=ADMIN_HDR,
                                        data="csrf=" + token + "&task=00000000000000000000000000000000")
            self.assertEqual(status, 400)
            self.assertIn("没有匹配的运行中任务", body)
            status, loc, _ = self.request_noredirect("/cancel", method="POST", headers=ADMIN_HDR,
                                                     data="csrf=" + token + "&task=" + task["id"])
            self.assertEqual(status, 303)                                    # §四B：POST 成功 → 303
            self.assertTrue(loc.startswith("/?task=" + task["id"]), loc)     # 跳到带该任务 ID 的 GET 页
            self.assertEqual(tasks.get(cfg, task["id"])["status"], tasks.STOPPING)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")
        tasks.claim(cfg, "apply", plan_id="t" * 64)
        try:
            status, body = self.request("/cancel", method="POST", headers=ADMIN_HDR, data="csrf=" + token)
            self.assertEqual(status, 400)
            self.assertIn("不支持在线停止", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_startup_reconcile_settles_phantom_running_state(self):
        # Review 26.10.12 P1：清理状态原同时依赖内存和 progress.json——重启后磁盘
        # 仍是 applying、页面长期显示执行中。启动核对把任务行终结为待核实、把
        # 残留相位落定为 interrupted，页面不再假运行，journal 汇总可查。
        cfg = v.read_config(webui.Handler.config)
        tasks.claim(cfg, "apply", plan_id="t" * 64)
        self._write_progress({"phase": "applying", "done": 1, "total": 2, "current": "", "freed": 8})
        webui._reconcile_on_start(webui.Handler.config)
        self.assertEqual((v.read_progress(cfg) or {}).get("phase"), "interrupted")
        row = tasks.latest(cfg, "apply")
        self.assertEqual(row["status"], tasks.VERIFY)
        status, body = self.request("/apply-status", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("待核实", body)
        self.assertNotIn("正在执行删除", body)
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertNotIn("正在执行删除", body)


    def test_post_forms_redirect_303_to_task_bound_get(self):
        # §四B 请求行为表：表单提交成功一律 303 → 带任务 ID 的 GET 页面；POST
        # 响应体为空且不含任何脚本——浏览器刷新/回退不会重提 POST
        # （Review 26.10.12 P1：原 post_plan 响应页 setTimeout(location.reload)）。
        token = self._csrf()
        cfg = v.read_config(webui.Handler.config)
        with mock.patch.object(webui, "scan_worker", lambda cfg: None):
            status, loc, body = self.request_noredirect("/scan", method="POST",
                                                         headers=ADMIN_HDR, data="csrf=" + token)
            self.assertEqual(status, 303)
            m = re.fullmatch(r"/\?task=([0-9a-f]{32})", loc)
            self.assertIsNotNone(m, loc)
            self.assertEqual(body, "")                       # 无脚本、无正文
            self.assertEqual(tasks.active(cfg)["id"], m.group(1))  # 303 指向刚占用的任务
            tasks.finish(cfg, tasks.FAILED, error="test", task_id=m.group(1))
        # §四D：计划输入只来自服务端草稿——为固定扫描号预置 scans+drafts 行
        # （本用例断言 PRG 与任务绑定不变；表单 sel 参数已不直达计划）
        cfg = v.read_config(webui.Handler.config)
        sid = "0123456789abcdef0123456789abcdef"
        with v.state(cfg, exclusive=False) as db:
            db.execute("INSERT INTO scans(id,config,roots,created,scope,summary) VALUES(?,?,?,?,NULL,NULL)",
                       (sid, v.config_identity(cfg), v.canonical(v.root_identities(cfg)), "2026-10-12T00:00:00Z"))
            db.execute("INSERT INTO drafts(scan,rev,data,updated) VALUES(?,?,?,?)",
                       (sid, 1, v.canonical({"exact:t": {"clean": ["m:a.mp4"], "confirmed": [], "skip": False}}), "t"))
            db.commit()
        with mock.patch.object(webui, "plan_worker", lambda *a: None):
            status, loc, body = self.request_noredirect(
                "/plan", method="POST", headers=ADMIN_HDR,
                data="csrf=" + token + "&scan=" + sid)
            self.assertEqual(status, 303)
            m = re.fullmatch(r"/plan-status\?task=([0-9a-f]{32})", loc)
            self.assertIsNotNone(m, loc)
            self.assertEqual(body, "")
            self.assertEqual(tasks.active(cfg)["id"], m.group(1))
            tasks.finish(cfg, tasks.FAILED, error="test", task_id=m.group(1))
        # 网关前缀模式下 Location 必须带 self.px 前缀（链接前缀纪律同样适用 303）
        with mock.patch.object(webui, "scan_worker", lambda cfg: None):
            status, loc, _ = self.request_noredirect("/app/video-dedupe/scan", method="POST",
                                                     headers=ADMIN_HDR, data="csrf=" + token)
            self.assertEqual(status, 303)
            self.assertTrue(loc.startswith("/app/video-dedupe/?task="), loc)
            self.assertEqual(tasks.active(cfg)["id"], loc.rsplit("=", 1)[1])
            tasks.finish(cfg, tasks.FAILED, error="test", task_id=loc.rsplit("=", 1)[1])

    def test_busy_submission_returns_current_task_not_new_one(self):
        # §四B：重复提交不产生第二个任务——忙碌应答携带当前任务信息（503），
        # 与 test_post_scan_busy_returns_current_task 互补：这里验证任务表里
        # 自始至终只有同一个 running 行。
        token = self._csrf()
        cfg = v.read_config(webui.Handler.config)
        first = tasks.claim(cfg, "plan", scan_id="0123456789abcdef0123456789abcdef")
        try:
            for _ in range(3):
                status, _, body = self.request_noredirect(
                    "/plan", method="POST", headers=ADMIN_HDR,
                    data="csrf=" + token + "&scan=0123456789abcdef0123456789abcdef&sel=m%3Ax")
                self.assertEqual(status, 503)
                self.assertIn("正在进行中", body)
            self.assertEqual(tasks.active(cfg)["id"], first["id"])  # 没有排队/覆盖
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_get_pages_have_no_side_effects(self):
        # §四B：GET 只读——不创建任务行、不写 progress.json。旧 GET /apply 同步
        # 跑核验预演的副作用已移除（Review 26.10.12 §四B 末条），只剩引导页。
        cfg = v.read_config(webui.Handler.config)
        state = Path(self.tmp.name) / "state"
        for path in ("/apply", "/apply?plan=whatever&confirm=whatever", "/plan-status",
                     "/plan-review", "/apply-status", "/apply-result", "/progress", "/"):
            with self.subTest(path=path):
                status, _ = self.request(path, headers=ADMIN_HDR)
                self.assertIn(status, (200, 404))
        self.assertIsNone(tasks.active(cfg))
        for ttype in ("scan", "plan", "apply"):
            self.assertIsNone(tasks.latest(cfg, ttype))
        self.assertFalse((state / "progress.json").exists())

    def test_cleanup_execution_disabled_until_platform_verified(self):
        # §四A/§五：回收站接入未经 fnOS 真机验证前，清理执行入口整体关闭：
        # 确认页显示「待平台验证，暂不可用」与具体原因（不宣称可恢复）、没有
        # 执行表单；直接 POST /apply 一律拒绝且不占用任务。
        self._write_progress({"phase": "plan_ready", "plan_id": "t0", "delete_files": 1, "logical_bytes": 8})
        status, body = self.request("/plan-review", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("待平台验证，暂不可用", body)
        self.assertIn("不进入系统回收站", body)
        self.assertNotIn("method=post action='/apply'", body)
        token = self._csrf()
        cfg = v.read_config(webui.Handler.config)
        status, body = self.request("/apply", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&plan=t0&confirm=t0")
        self.assertEqual(status, 503)
        self.assertIn("待平台验证，暂不可用", body)
        self.assertIsNone(tasks.active(cfg))  # 拒绝发生在占用任务之前，不留任务行

    def test_scan_progress_three_phases_no_fake_eta(self):
        # §三.2 三阶段进度：查找视频（总数未知只报已发现数、无进度条）→ 识别内容
        # → 整理结果；旧 ETA 按页面加载时刻估算属伪造数字，整体移除。
        cfg = v.read_config(webui.Handler.config)
        task = tasks.claim(cfg, "scan")
        try:
            cases = (
                # 断言锚定初始渲染的标签内容（id=p-label / p-num 内文）：轮询 JS
                # 静态包含全部阶段切换字符串，裸词断言会误伤。
                ({"phase": "discover", "found": 7, "done": 0, "total": 0, "current": "/vol/media"},
                 ["id=p-label>正在查找视频<", ">7 个视频已发现</span>", "id=p-bar-wrap style='display:none'"],
                 ["id=p-label>正在识别内容", "id=p-label>正在整理结果", ">已识别 ", ">已比较 ",
                  "估计剩余时间"]),
                ({"phase": "fingerprint", "done": 3, "total": 9, "current": "a.mp4"},
                 ["id=p-label>正在识别内容<", ">已识别 3 / 9 个视频</span>", "id=p-bar"],
                 ["id=p-label>正在查找视频", "id=p-label>正在整理结果", "估计剩余时间"]),
                ({"phase": "grouping", "done": 2, "total": 5, "current": ""},
                 ["id=p-label>正在整理结果<", ">已比较 2 / 5 个候选</span>", "id=p-bar"],
                 ["id=p-label>正在查找视频", "id=p-label>正在识别内容", "估计剩余时间"]),
            )
            for progress, present, absent in cases:
                with self.subTest(phase=progress["phase"]):
                    self._write_progress(progress)
                    status, body = self.request("/", headers=ADMIN_HDR)
                    self.assertEqual(status, 200)
                    for marker in present:
                        self.assertIn(marker, body)
                    for marker in absent:
                        self.assertNotIn(marker, body)
            # 停止表单：绑定任务 ID；确认文案如实说复用，不再说「接着进行」
            self._write_progress({"phase": "fingerprint", "done": 3, "total": 9, "current": ""})
            status, body = self.request("/", headers=ADMIN_HDR)
            self.assertIn(f"name=task value='{task['id']}'", body)
            self.assertIn("再次扫描会复用已有识别结果", body)
            self.assertNotIn("接着进行", body)
            self.assertIn("vdPoll", body)  # 三阶段轮询仍在（状态页静止回归防护）
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_scan_stopping_state_waits_for_checkpoint(self):
        # §三.2：停止请求后任务行 stopping——按钮让位给「已请求停止，等待当前
        # 步骤结束」，不再显示可重复点击的停止按钮。
        cfg = v.read_config(webui.Handler.config)
        task = tasks.claim(cfg, "scan")
        try:
            tasks.request_stop(cfg, task["id"])
            self._write_progress({"phase": "fingerprint", "done": 1, "total": 4, "current": ""})
            status, body = self.request("/", headers=ADMIN_HDR)
            self.assertEqual(status, 200)
            self.assertIn("已请求停止", body)
            self.assertIn("正在等待当前步骤结束", body)
            self.assertNotIn("停止扫描</button>", body)
        finally:
            tasks.finish(cfg, tasks.FAILED, error="test")

    def test_scan_cancelled_page_shows_reuse_note(self):
        # §三.2 已停止：独立结束状态卡——停止位置、数量、可执行的下一步，以及
        # 「再次扫描会复用已有识别结果」（不称断点续跑）。
        for stopped_at, progress, marker in (
            ("discover", {"found": 5, "done": 0, "total": 0}, "已发现 5 个视频"),
            ("fingerprint", {"found": 5, "done": 2, "total": 6}, "已识别 2 / 6 个"),
            ("grouping", {"found": 6, "done": 0, "total": 0}, "6 个视频已识别完成"),
        ):
            with self.subTest(stopped_at=stopped_at):
                self._write_progress({"phase": "cancelled", "stopped_at": stopped_at,
                                      "current": "", **progress})
                status, body = self.request("/", headers=ADMIN_HDR)
                self.assertEqual(status, 200)
                self.assertIn("扫描已停止</h3>", body)
                self.assertIn(marker, body)
                self.assertIn("再次扫描会复用已有识别结果", body)
                self.assertIn("再次扫描", body)      # 可执行的下一步
                self.assertNotIn("接着进行", body)   # 旧的断点续跑话术
                self.assertNotIn("正在扫描", body)   # 已停止≠仍在扫描

    def test_scan_done_page_distinguishes_skips(self):
        # §三.2 结束状态：完成（无跳过徽章）vs 完成但有跳过（数量+原因样本+重试说明）。
        clean = {"phase": "done", "scan_id": "a" * 32, "done": 2, "total": 2, "current": "",
                 "summary": {"files": 2, "exact_groups": 1, "visual_groups": 0,
                             "skipped_total": 0, "scan_warning_samples": [], "fingerprint_error_samples": []}}
        self._write_progress(clean)
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("扫描完成</h3>", body)
        self.assertIn("共识别 2 个视频", body)
        self.assertNotIn("处跳过", body)
        skipped = {"phase": "done", "scan_id": "b" * 32, "done": 3, "total": 3, "current": "",
                   "summary": {"files": 3, "exact_groups": 1, "visual_groups": 1, "skipped_total": 2,
                               "scan_warning_samples": ["跳过指纹阶段变化的文件: movies:x.mp4: 文件在复核时发生变化"],
                               "fingerprint_error_samples": ["movies:broken.mp4"]}}
        self._write_progress(skipped)
        status, body = self.request("/", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("扫描完成（部分内容被跳过）", body)
        self.assertIn("有 2 处跳过", body)
        self.assertIn("movies:x.mp4", body)       # 原因样本如实展示
        self.assertIn("movies:broken.mp4", body)
        self.assertIn("下次扫描会自动重试", body)

    def test_status_and_result_pages_bind_requested_task(self):
        # §四B 请求行为表：状态/结果读取返回固定任务的持久化状态，不按
        # 「最近一条」猜测；不存在的任务 ID 明确 404；成功但结果字段缺失时
        # 如实说明，不伪造成功页。
        cfg = v.read_config(webui.Handler.config)
        t1 = tasks.claim(cfg, "plan", scan_id="0123456789abcdef0123456789abcdef")
        tasks.finish(cfg, tasks.SUCCESS, result={"plan_id": "a" * 64, "delete_files": 2,
                                                 "logical_bytes": 4 * 2**20})
        t2 = tasks.claim(cfg, "plan")
        tasks.finish(cfg, tasks.FAILED, error="boom")
        hdrs = {**ADMIN_HDR, "Accept": "application/json"}
        status, body = self.request("/plan-status?task=" + t1["id"], headers=hdrs)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["plan_id"], "a" * 64)   # 固定读 t1，而非 latest(t2)
        status, body = self.request("/plan-status?task=" + t2["id"], headers=hdrs)
        self.assertEqual(json.loads(body)["error"], "boom")
        status, body = self.request("/plan-status?task=" + "f" * 32, headers=hdrs)
        self.assertEqual(status, 404)
        self.assertIn("任务不存在", body)
        # 确认页同样绑定任务：t1 成功 → 清单 + 禁用卡；t2 失败 → 失败页
        status, body = self.request("/plan-review?task=" + t1["id"], headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("清理计划已生成", body)
        status, body = self.request("/plan-review?task=" + t2["id"], headers=ADMIN_HDR)
        self.assertIn("生成清理计划失败", body)
        # 结果读取：任务成功但完整结果未落盘 → 如实「结果记录缺失」
        a1 = tasks.claim(cfg, "apply", plan_id="b" * 64)
        tasks.finish(cfg, tasks.SUCCESS)  # 无 result
        status, body = self.request("/apply-result?task=" + a1["id"], headers=ADMIN_HDR)
        self.assertEqual(status, 404)
        self.assertIn("结果记录缺失", body)
        a2 = tasks.claim(cfg, "apply", plan_id="c" * 64)
        tasks.finish(cfg, tasks.SUCCESS, result={
            "dry_run": False, "plan_id": "c" * 64, "deleted_files": 1, "logical_bytes": 2 * 2**20,
            "free_bytes_before": {"m": 2**30}, "free_bytes_after": {"m": 2**30}, "journal": "x"})
        status, body = self.request("/apply-result?task=" + a2["id"], headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("已删除 <b>1</b> 个文件", body)
        status, body = self.request("/apply-result?task=" + a1["id"], headers=ADMIN_HDR)  # latest=a2 也不影响 a1
        self.assertEqual(status, 404)
        self.assertIn("结果记录缺失", body)


    def test_review_page_no_preselection_and_suggestion_semantics(self):
        # Review 26.10.12 §三.3 / P0（webui 旧「最小文件=建议保留」并默认预勾选）：
        # 初次进入不自动选中任何清理项；批量建议只在用户点「应用建议」后生效，
        # 受保护副本优先保留；外挂字幕差异的组跳过批量处理并列出原因；
        # 每个文件明确标注 受保护/保留（点击后由脚本翻为 待清理）。
        base = Path(self.tmp.name)
        ref = base / "ref"
        ref.mkdir()
        (base / "media" / "b.mp4").write_bytes(b"x" * 16)      # 与 a.mp4 字节一致
        (ref / "r.mp4").write_bytes(b"x" * 16)                  # 同内容、只读目录 → 受保护副本
        (base / "media" / "s.mp4").write_bytes(b"y" * 16)
        (base / "media" / "s2.mp4").write_bytes(b"y" * 16)
        (base / "media" / "s.srt").write_text("1", encoding="utf-8")  # 只有 s.mp4 带字幕
        (base / "config.json").write_text(json.dumps({
            "state_dir": str(base / "state"),
            "roots": [{"id": "m", "path": str(base / "media"), "role": "cleanup", "exclude": []},
                      {"id": "r", "path": str(ref), "role": "reference", "exclude": []}]}), encoding="utf-8")
        with mock.patch.object(v, "probe_media", lambda cfg, path: {
                "duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                "fps": "24/1", "bitrate": "1000000", "tracks": []}), \
             mock.patch.object(v, "visual_fingerprint", lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)), \
             mock.patch.object(v, "thumbnail", return_value="t.jpg"):
            v.inventory(v.read_config(webui.Handler.config))
        status, body = self.request("/review", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        # 1) 默认无选中：没有勾选输入、没有选中态，旧「最小=保留」规则痕迹全清
        self.assertIn("name=sel", body)
        self.assertNotIn("aria-checked='true'", body)
        self.assertNotIn(" checked", body)
        self.assertNotIn("data-smallest", body)
        self.assertNotIn(">建议保留<", body)   # 旧的「建议保留」徽章（最小文件规则）
        self.assertNotIn("保留最小版本", body)
        self.assertNotIn("pickAll", body)
        self.assertNotIn("pickGroup", body)
        # 2) 每个文件标注状态：默认「保留」，只读目录成员标「受保护」
        self.assertIn("data-st>保留<", body)
        self.assertIn("受保护</span>", body)
        # 3) 建议展示但不预选：按钮 + 嵌入的建议载荷（clean=待清理，reset=建议保留）
        self.assertIn("应用建议", body)
        self.assertIn("应用本组建议", body)
        self.assertIn("受保护副本", body)
        payload = body.split("<script>VD_SUGGEST=")[1].split(";</script>")[0]
        self.assertIn("m:b.mp4", payload)   # 与受保护副本字节一致 → 建议清理
        self.assertIn("r:r.mp4", payload)   # 受保护副本 → 建议保留（reset）
        # 4) 外挂字幕差异的子组：跳过批量处理并在页面上列出原因
        self.assertIn("外挂字幕不一致", body)
        self.assertIn("需人工判断", body)
        # 5) 画面相似成员不参与批量建议的说明存在（本组为混合组，s/s2 属被跳过子组）
        self.assertIn("画面相似", body)


class ReviewPaginationDraftTests(unittest.TestCase):
    """审阅页服务端分页/筛选/超大分组预览 + 服务端草稿（Review 26.10.12
    §四D / P2：每次审阅不再加载整个结果集；选择持久化绑定扫描，修订冲突
    不静默覆盖；计划输入只来自草稿）。

    造库：media 21 对 + media2 21 对字节完全重复（互不相同、时长错开不成
    视觉组）+ 1 个 30 成员超大组，共 43 组；指纹管线全部 mock。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.media, self.media2 = base / "media", base / "media2"
        self.media.mkdir()
        self.media2.mkdir()
        self.spec = {}  # 绝对路径 -> duration
        for i in range(1, 22):
            for root, prefix, mk in ((self.media, "f", lambda i: 100.0 + i * 7),
                                     (self.media2, "g", lambda i: 1000.0 + i * 60)):
                data = bytes([i + (0 if prefix == "f" else 100)]) * 160
                duration = mk(i)  # 对间隔须大于时长容差窗口（约 2%），否则 vhash 全 0 的 mock 会串组
                for half in "ab":
                    p = root / f"{prefix}{i:02d}{half}.mp4"
                    p.write_bytes(data)
                    self.spec[str(p)] = duration
        big = b"BIG" * 53
        for i in range(30):  # 超大分组：30 个字节一致的成员
            p = self.media / f"big{i:02d}.mp4"
            p.write_bytes(big)
            self.spec[str(p)] = 400.0
        # big 组时长 400 落在 f 段（107~247）与 g 段（1007~1147）之外，独立成组
        config = base / "config.json"
        config.write_text(json.dumps({"state_dir": str(base / "state"),
                                      "roots": [{"id": "m", "path": str(self.media), "role": "cleanup", "exclude": []},
                                                {"id": "m2", "path": str(self.media2), "role": "cleanup", "exclude": []}]}),
                          encoding="utf-8")
        webui.Handler.config = str(config)
        cfg = v.read_config(config)
        with mock.patch.object(v, "probe_media", lambda cfg, path: {
                "duration": self.spec[str(path)], "width": 640, "height": 360, "codec": "h264",
                "fps": "24/1", "bitrate": "1000000", "tracks": []}), \
             mock.patch.object(v, "visual_fingerprint", lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)), \
             mock.patch.object(v, "thumbnail", return_value="t.jpg"):
            self.scan = v.inventory(cfg)["scan_id"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        self.server.gateway = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", headers=None, data=None):
        return fetch(self.base_url, path, method=method, headers=headers, data=data)

    def csrf(self):
        status, body = self.request("/", headers=ADMIN_HDR)
        return re.search(r"name=csrf value='([0-9a-f]{16,})'", body).group(1)

    @staticmethod
    def gids(body):
        return re.findall(r"data-gid='([^']+)'", body)

    def test_review_server_side_pagination_and_links(self):
        # 每页 20 组：页 1/2 各 20、页 3 余 2；越界与非法页号钳制；翻页链接保留
        # 筛选参数与网关前缀；两页组集合不相交（真的在分页，不是重复渲染）
        status, body = self.request("/review?scan=" + self.scan, headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.gids(body)), webui.Handler.REVIEW_PAGE_SIZE)
        self.assertIn("第 1 / 3 页", body)
        self.assertIn("当前筛选 43 组", body)
        self.assertIn(f"/review?scan={self.scan}&page=2&kind=exact", body)
        page1 = set(self.gids(body))
        status, body = self.request(f"/review?scan={self.scan}&page=2", headers=ADMIN_HDR)
        self.assertEqual(len(self.gids(body)), webui.Handler.REVIEW_PAGE_SIZE)
        self.assertFalse(page1 & set(self.gids(body)), "两页出现相同分组")
        status, body = self.request(f"/review?scan={self.scan}&page=3", headers=ADMIN_HDR)
        self.assertEqual(len(self.gids(body)), 43 - 2 * webui.Handler.REVIEW_PAGE_SIZE)
        status, body = self.request(f"/review?scan={self.scan}&page=99", headers=ADMIN_HDR)  # 越界钳到末页
        self.assertIn("第 3 / 3 页", body)
        status, body = self.request(f"/review?scan={self.scan}&page=abc", headers=ADMIN_HDR)  # 非法页号回落第 1 页
        self.assertIn("第 1 / 3 页", body)
        # 网关前缀模式下分页链接必须带 self.px（链接前缀纪律）
        status, body = self.request(f"/app/video-dedupe/review?scan={self.scan}&page=2", headers=ADMIN_HDR)
        self.assertIn("href='/app/video-dedupe/review?", body)

    def test_review_filters_kind_name_root_and_state(self):
        # 名称子串 / 文件夹（绑定根身份）/ 类型 / 待处理-已处理
        status, body = self.request(f"/review?scan={self.scan}&q=f01", headers=ADMIN_HDR)
        self.assertEqual(len(self.gids(body)), 1)
        self.assertIn("f01a.mp4", body)
        status, body = self.request(f"/review?scan={self.scan}&root=m2", headers=ADMIN_HDR)
        self.assertIn("当前筛选 21 组", body)
        self.assertIn("g01a.mp4", body)
        self.assertNotIn("f01a.mp4", body)
        status, body = self.request(f"/review?scan={self.scan}&kind=visual", headers=ADMIN_HDR)
        self.assertIn("当前筛选 0 组", body)
        self.assertEqual(self.gids(body), [])
        # 草稿决定已处理/待处理：保存一组选择后 state=done 只剩该组、pending 排除它
        token = self.csrf()
        gid, key = self.first_gid_and_key()
        status, out = self.post_draft(token, 0, {gid: {"clean": [key], "confirmed": [key], "skip": False}})
        self.assertEqual(status, 200)
        status, body = self.request(f"/review?scan={self.scan}&state=done", headers=ADMIN_HDR)
        self.assertEqual(self.gids(body), [gid])
        status, body = self.request(f"/review?scan={self.scan}&state=pending", headers=ADMIN_HDR)
        self.assertIn("当前筛选 42 组", body)
        self.assertNotIn(f"data-gid='{gid}'", body)

    def test_review_large_group_member_preview(self):
        # 超大分组默认只渲染前 12 个成员；?full=<gid> 展开全部（§四D/P2）
        status, body = self.request(f"/review?scan={self.scan}&q=big", headers=ADMIN_HDR)
        self.assertEqual(len(self.gids(body)), 1)
        self.assertEqual(body.count("class='tile"), webui.Handler.GROUP_MEMBER_PREVIEW)
        self.assertIn("本组共 30 个文件，先显示前 12 个", body)
        self.assertIn("显示全部 30 个", body)
        m = re.search(r"href='([^']*full=[^']+)'", body)
        self.assertTrue(m, "缺少展开全部成员的链接")
        status, body = self.request(m.group(1), headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertEqual(body.count("class='tile"), 30)
        self.assertIn("合计", body)  # 全部成员加载后才显示合计大小

    def first_gid_and_key(self):
        status, body = self.request("/review?scan=" + self.scan, headers=ADMIN_HDR)
        gid = self.gids(body)[0]
        seg = body.split(f"data-gid='{gid}'", 1)[1]
        key = re.search(r"name=sel value='([^']+)'", seg).group(1)
        return gid, key

    def post_draft(self, token, rev, draft_obj):
        payload = "csrf=%s&scan=%s&rev=%d&draft=%s" % (token, self.scan, rev,
                                                       quote(json.dumps(draft_obj, ensure_ascii=False)))
        return self.request("/draft", method="POST", headers=ADMIN_HDR, data=payload)

    def test_draft_save_restore_and_revision_conflict(self):
        # 保存 → 页面恢复勾选；旧修订再保存 409 提示刷新、不静默覆盖；伪造条目 400
        gid, key = self.first_gid_and_key()
        token = self.csrf()
        status, body = self.request("/review?scan=" + self.scan, headers=ADMIN_HDR)
        self.assertIn("VD_REV=0", body)  # 无草稿：修订 0
        status, out = self.post_draft(token, 0, {gid: {"clean": [key], "confirmed": [key], "skip": False}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(out), {"rev": 1})
        status, body = self.request("/review?scan=" + self.scan, headers=ADMIN_HDR)
        self.assertIn("VD_REV=1", body)                       # 修订号回填页面
        self.assertIn("aria-pressed='true'", body)            # 选择从草稿恢复（勾选按钮态）
        self.assertIn(" checked", body)
        self.assertIn("data-st>待清理<", body)
        self.assertIn("草稿已保存（修订 1）", body)
        # 另一标签页基于修订 0 保存 → 409 冲突（携带当前修订），先保存内容保留
        other = {gid: {"clean": [], "confirmed": [], "skip": True}}
        status, out = self.post_draft(token, 0, other)
        self.assertEqual(status, 409)
        data = json.loads(out)
        self.assertTrue(data["conflict"])
        self.assertEqual(data["rev"], 1)
        self.assertIn("刷新页面重新加载", data["error"])
        cfg = v.read_config(webui.Handler.config)
        with v.state(cfg, exclusive=False) as db:
            self.assertEqual(v.load_draft(db, cfg, self.scan)["groups"][gid]["clean"], [key])
        # 伪造分组 / 组外成员 / 非法 rev：明确 400，不产生草稿变更
        for bad in ({"nope:x": {"clean": [], "confirmed": [], "skip": False}},
                    {gid: {"clean": ["m:not-in-group.mp4"], "confirmed": [], "skip": False}}):
            status, out = self.post_draft(token, 1, bad)
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(out))
        status, out = self.request("/draft", method="POST", headers=ADMIN_HDR,
                                   data="csrf=%s&scan=%s&rev=zzz&draft={}" % (token, self.scan))
        self.assertEqual(status, 400)

    def test_plan_input_comes_from_draft_only(self):
        # §四D：计划输入只来自服务端草稿——无草稿提交被拒；草稿勾选直达计划
        token = self.csrf()
        status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scan=" + self.scan + "&sel=m:f01a.mp4")
        self.assertEqual(status, 400)
        self.assertIn("草稿中没有勾选任何文件", body)
        cfg = v.read_config(webui.Handler.config)
        self.assertEqual(tasks.latest(cfg, "plan")["error"], "草稿中没有待清理选择")
        gid, key = self.first_gid_and_key()
        status, out = self.post_draft(token, 0, {gid: {"clean": [key], "confirmed": [key], "skip": False}})
        self.assertEqual(status, 200)
        with mock.patch.object(webui, "plan_worker") as worker:
            status, loc, body = self.request_noredirect("/plan", method="POST", headers=ADMIN_HDR,
                                                        data="csrf=" + token + "&scan=" + self.scan)
            self.assertEqual(status, 303)
            self.assertRegex(loc, r"/plan-status\?task=[0-9a-f]{32}")
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(worker.call_args[0][2], [key])  # 后台计划拿到草稿选择
        tasks.finish(v.read_config(webui.Handler.config), tasks.FAILED, error="test",
                     task_id=loc.rsplit("=", 1)[1])  # 清理 mock 吞掉的收尾

    def request_noredirect(self, path, method="GET", headers=None, data=None):
        return fetch_noredirect(self.base_url, path, method=method, headers=headers, data=data)

    def test_draft_post_requires_admin_and_csrf(self):
        # 草稿写入同样过统一前置校验：匿名 403、令牌不符 403
        gid, key = self.first_gid_and_key()
        payload = "csrf=x&scan=%s&rev=0&draft=%s" % (self.scan, quote(json.dumps({gid: {"clean": [key], "confirmed": [], "skip": False}})))
        status, body = self.request("/draft", method="POST", data=payload)
        self.assertEqual(status, 403)
        self.assertIn("管理员", body)
        status, body = self.request("/draft", method="POST", headers=ADMIN_HDR, data=payload)
        self.assertEqual(status, 403)
        self.assertIn("安全校验", body)


class CompareAndConfirmTests(unittest.TestCase):
    """C2（Review 26.10.12 §三.3 / P1）：相似组比较界面（并排差异 + 五帧实际
    时间 + 画面不兼任勾选）、提交前校验（直接匹配 / 差异确认 / 跳过原因）、
    确认页保留/清理清单与差异注意点。

    造库：va/vb 视觉相似（指纹全同、时长 100/102 落在容差窗口，参数逐项不同，
    va 带外挂字幕）；ea/eb 字节完全一致。指纹管线三件套 mock，sha 真算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.media = base / "media"
        self.media.mkdir()
        (self.media / "va.mp4").write_bytes(b"A" * 3000)
        (self.media / "vb.mp4").write_bytes(b"B" * 5000)
        (self.media / "va.srt").write_text("1", encoding="utf-8")   # 只有 va 带外挂字幕
        (self.media / "ea.mp4").write_bytes(b"C" * 4000)
        (self.media / "eb.mp4").write_bytes(b"C" * 4000)
        (self.media / "eb.srt").write_text("1", encoding="utf-8")   # 只有 eb 带外挂字幕 → exact 组也有字幕差异
        config = base / "config.json"
        config.write_text(json.dumps({"state_dir": str(base / "state"),
                                      "roots": [{"id": "m", "path": str(self.media), "role": "cleanup", "exclude": []}]}),
                          encoding="utf-8")
        webui.Handler.config = str(config)
        cfg = v.read_config(config)
        metas = {
            "va.mp4": {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                       "fps": "24/1", "bitrate": "1000000",
                       "tracks": [{"type": "audio", "codec": "aac", "language": "chi",
                                   "title": "", "channels": 2, "disposition": {}}]},
            "vb.mp4": {"duration": 102.0, "width": 1280, "height": 720, "codec": "h264",
                       "fps": "30000/1001", "bitrate": "2000000",
                       "tracks": [{"type": "audio", "codec": "eac3", "language": "eng",
                                   "title": "", "channels": 6, "disposition": {}},
                                  {"type": "subtitle", "codec": "subrip", "language": "chi",
                                   "title": "", "channels": None, "disposition": {}}]},
            "ea.mp4": {"duration": 200.0, "width": 1280, "height": 720, "codec": "h264",
                       "fps": "24/1", "bitrate": "1000000", "tracks": []},
            "eb.mp4": {"duration": 200.0, "width": 1280, "height": 720, "codec": "h264",
                       "fps": "24/1", "bitrate": "1000000", "tracks": []},
        }
        with mock.patch.object(v, "probe_media", lambda cfg, path: metas[Path(path).name]), \
             mock.patch.object(v, "visual_fingerprint", lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)), \
             mock.patch.object(v, "thumbnail", return_value="t.jpg"):
            self.scan = v.inventory(cfg)["scan_id"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        self.server.gateway = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", headers=None, data=None):
        return fetch(self.base_url, path, method=method, headers=headers, data=data)

    def csrf(self):
        status, body = self.request("/", headers=ADMIN_HDR)
        return re.search(r"name=csrf value='([0-9a-f]{16,})'", body).group(1)

    def post_draft(self, token, rev, draft_obj):
        payload = "csrf=%s&scan=%s&rev=%d&draft=%s" % (token, self.scan, rev,
                                                       quote(json.dumps(draft_obj, ensure_ascii=False)))
        return self.request("/draft", method="POST", headers=ADMIN_HDR, data=payload)

    def gid_of_kind(self, kind):
        status, body = self.request(f"/review?scan={self.scan}&kind={kind}", headers=ADMIN_HDR)
        found = re.findall(r"data-gid='(%s:[^']+)'" % kind, body)
        self.assertTrue(found, f"审阅页没有 {kind} 组")
        return found[0]

    def captured_members(self, gid):
        cfg = v.read_config(webui.Handler.config)
        with v.state(cfg, exclusive=False) as db:
            groups = v.load_groups(db, self.scan)
            g = next(g for g in groups if g["gid"] == gid)
            return [m for m in v.load_files(db, cfg, self.scan, set(g["members"])).values()]

    # --- 比较数据组装 -------------------------------------------------------

    def test_compare_rows_assembly_highlights_and_neutral(self):
        # 纯函数层：字段齐全、差异标志精确（编码相同不算差异）、notes 口径正确、
        # 不产生任何相似百分比/质量评分式输出
        members = self.captured_members(self.gid_of_kind("visual"))
        data = v.compare_rows(v.read_config(webui.Handler.config), sorted(members, key=lambda m: m["rel"]))
        by_label = {r["label"]: r for r in data["rows"]}
        self.assertEqual([r["label"] for r in data["rows"]],
                         ["分辨率", "时长", "编码", "帧率", "大小", "音轨语言", "声道", "内嵌字幕", "外挂字幕"])
        self.assertEqual(by_label["分辨率"]["cells"], ["640×360", "1280×720"])
        self.assertTrue(by_label["分辨率"]["diff"])
        self.assertEqual(by_label["编码"]["cells"], ["h264", "h264"])
        self.assertFalse(by_label["编码"]["diff"])           # 相同字段不高亮
        self.assertEqual(by_label["外挂字幕"]["cells"], ["va.srt", "无"])
        self.assertEqual(data["notes"], ["音轨", "字幕", "时长"])
        exact = self.captured_members(self.gid_of_kind("exact"))
        self.assertEqual(v.compare_rows(v.read_config(webui.Handler.config), exact)["notes"], ["字幕"])
        # media_diff_notes 与 compare_rows 同口径；无外挂字幕键时只比内嵌轨
        self.assertEqual(v.media_diff_notes([{"duration": 1.0, "tracks": []},
                                             {"duration": 1.0, "tracks": []}]), [])

    def test_compare_page_fields_frames_and_no_picking(self):
        gid = self.gid_of_kind("visual")
        # 组内有待清理选择时差异卡才出现确认入口；未确认前亮「差异未确认」
        token = self.csrf()
        status, out = self.post_draft(token, 0, {gid: {"clean": ["m:vb.mp4"], "confirmed": [], "skip": False}})
        self.assertEqual(status, 200)
        status, body = self.request(f"/compare?scan={self.scan}&gid={gid}", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("差异未确认", body)
        self.assertIn("确认待清理选择", body)
        # 并排九字段全部呈现；差异行高亮、相同行（编码）不高亮
        for label in ("分辨率", "时长", "编码", "帧率", "大小", "音轨语言", "声道", "内嵌字幕", "外挂字幕"):
            self.assertIn(label, body)
        self.assertIn("<tr class=rd><th>分辨率", body)
        self.assertIn("<tr><th>编码</th>", body)
        self.assertIn("差异</span>", body)
        # 五帧切换 + 各自实际时间（va 100s：10%→0:10、50%→0:50；vb 102s：50%→0:51）
        for pct in ("10%", "25%", "50%", "75%", "90%"):
            self.assertIn(f">{pct}</button>", body)
        self.assertIn("实际时间 0:10", body)
        self.assertIn("实际时间 0:50", body)
        self.assertIn("实际时间 0:51", body)
        # 帧图指向 /preview 且带网关前缀；差异卡有确认按钮
        status2, body2 = self.request(f"/app/video-dedupe/compare?scan={self.scan}&gid={gid}", headers=ADMIN_HDR)
        self.assertIn("src='/app/video-dedupe/preview?scan=", body2)
        # 比较页不承担勾选（§三.3 画面/比较不兼任删除勾选）
        self.assertNotIn("name=sel", body)
        # 不包装成相似百分比或质量评分：页面明确免责，且不出现「相似度」话术
        self.assertIn("不提供质量评分或相似百分比", body)
        self.assertNotIn("相似度", body)
        self.assertNotIn("推荐清理画质", body)

    def test_preview_route_serves_frames_and_guards(self):
        gid = self.gid_of_kind("visual")
        frame_bytes = b"\xff\xd8" + b"frame" * 10
        previews = Path(self.tmp.name) / "state" / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        (previews / "f.jpg").write_bytes(frame_bytes)
        with mock.patch.object(v, "preview_frames",
                               lambda cfg, item, duration: [{"pos": 0.5, "t": 50.0, "img": "f.jpg"}]):
            status, body = self.request(f"/preview?scan={self.scan}&f=m:va.mp4&p=0.5", headers=ADMIN_HDR)
            self.assertEqual(status, 200)
            self.assertEqual(body[:2], "\ufffd\ufffd")   # fetch 以 utf-8/replace 解码：JPEG 魔数成为两个替换符
            for bad in (f"/preview?scan={self.scan}&f=m:va.mp4&p=0.3",      # 非法位置
                        f"/preview?scan={self.scan}&f=m:ghost.mp4&p=0.5"):   # 不属于本扫描
                status, _ = self.request(bad, headers=ADMIN_HDR)
                self.assertEqual(status, 404)
        # 扫描后文件被替换（stat 不符）：拒绝，不把别的画面冒充扫描内容
        target = self.media / "va.mp4"
        original = target.read_bytes()
        target.write_bytes(original + b"changed")
        try:
            with mock.patch.object(v, "preview_frames", lambda cfg, item, duration: []):
                status, body = self.request(f"/preview?scan={self.scan}&f=m:va.mp4&p=0.5", headers=ADMIN_HDR)
                self.assertEqual(status, 404)
                self.assertIn("已变化", body)
        finally:
            target.write_bytes(original)

    def test_review_tiles_pick_by_button_picture_opens_compare(self):
        # §三.3 交互契约：勾选由显式按钮承担；画面/名称是进比较的链接；
        # tile 容器不再整体可点；指纹距离中性呈现
        status, body = self.request(f"/review?scan={self.scan}&kind=visual", headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("onclick='togglePick(this)'", body)
        self.assertIn("标记待清理", body)
        self.assertIn("class=thumblnk", body)
        self.assertIn(f"href='/compare?scan={self.scan}&gid=visual:", body)
        self.assertIn("并排比较", body)
        self.assertNotIn("role=checkbox", body)
        self.assertNotIn("toggleTile", body)
        self.assertIn("画面指纹距离", body)          # 中性：算法距离，非相似百分比
        self.assertNotIn("相似度差异", body)

    # --- 提交前校验（直接匹配 / 差异确认 / 跳过原因）--------------------------

    def test_plan_precheck_rejects_and_accepts(self):
        token = self.csrf()
        vgid = self.gid_of_kind("visual")
        # 1) 差异未确认 → 拒绝并指明组
        status, out = self.post_draft(token, 0, {vgid: {"clean": ["m:vb.mp4"], "confirmed": [], "skip": False}})
        self.assertEqual(status, 200)
        rev = json.loads(out)["rev"]
        status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scan=" + self.scan)
        self.assertEqual(status, 400)
        self.assertIn("未通过提交前校验", body)
        self.assertIn("并排比较页核对并确认", body)
        # 2) 全组清理 → 没有仍被保留的直接匹配对象
        status, out = self.post_draft(token, rev, {vgid: {"clean": ["m:va.mp4", "m:vb.mp4"], "confirmed": ["m:va.mp4", "m:vb.mp4"], "skip": False}})
        rev = json.loads(out)["rev"]
        status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scan=" + self.scan)
        self.assertEqual(status, 400)
        self.assertIn("没有仍被保留的直接匹配对象", body)
        # 3) 跳过未说明原因 → 拒绝；说明后放行
        status, out = self.post_draft(token, rev, {vgid: {"clean": ["m:vb.mp4"], "confirmed": ["m:vb.mp4"], "skip": False},
                                                   self.gid_of_kind("exact"): {"clean": [], "confirmed": [], "skip": True}})
        rev = json.loads(out)["rev"]
        status, body = self.request("/plan", method="POST", headers=ADMIN_HDR,
                                    data="csrf=" + token + "&scan=" + self.scan)
        self.assertEqual(status, 400)
        self.assertIn("说明原因", body)
        egid = self.gid_of_kind("exact")
        status, out = self.post_draft(token, rev, {egid: {"clean": [], "confirmed": [], "skip": True, "skip_reason": "无法判断，先保留"}})
        rev = json.loads(out)["rev"]
        with mock.patch.object(webui, "plan_worker") as worker:
            status, loc, body = fetch_noredirect(self.base_url, "/plan", method="POST", headers=ADMIN_HDR,
                                                 data="csrf=" + token + "&scan=" + self.scan)
            self.assertEqual(status, 303)                          # 确认+原因齐备 → 放行（PRG）
            self.assertRegex(loc, r"/plan-status\?task=[0-9a-f]{32}")
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(worker.call_args[0][2], ["m:vb.mp4"])
        tasks.finish(v.read_config(webui.Handler.config), tasks.FAILED, error="test",
                     task_id=loc.rsplit("=", 1)[1])

    def test_plan_review_lists_keep_delete_folders_and_notes(self):
        # P1/§三.4 确认页清单：走真实计划生成（缓存命中，秒级），确认页直接展示
        # 保留/清理文件、所在文件夹、匹配依据、差异注意点与返回修改入口。
        token = self.csrf()
        vgid = self.gid_of_kind("visual")
        status, out = self.post_draft(token, 0, {vgid: {"clean": ["m:vb.mp4"], "confirmed": ["m:vb.mp4"], "skip": False}})
        self.assertEqual(status, 200)
        status, loc, body = fetch_noredirect(self.base_url, "/plan", method="POST", headers=ADMIN_HDR,
                                             data="csrf=" + token + "&scan=" + self.scan)
        self.assertEqual(status, 303)
        task_id = loc.rsplit("=", 1)[1]
        hdrs = {**ADMIN_HDR, "Accept": "application/json"}
        for _ in range(80):  # 等后台计划 + 预演完成（小库缓存命中，通常一两轮）
            status, body = self.request("/plan-status?task=" + task_id, headers=hdrs)
            data = json.loads(body)
            if not data["running"] and (data.get("plan_id") or data.get("error")):
                break
            time.sleep(0.2)
        self.assertTrue(data.get("plan_id"), f"计划未生成: {data.get('error')}")
        status, body = self.request("/plan-review?task=" + task_id, headers=ADMIN_HDR)
        self.assertEqual(status, 200)
        self.assertIn("保留与清理清单", body)
        self.assertIn("vb.mp4", body)                              # 清理文件具名
        self.assertIn("va.mp4", body)                              # 保留文件具名
        self.assertIn(str(self.media), body)                       # 所在文件夹（授权目录真实位置）
        self.assertIn("画面指纹直接匹配", body)                     # 匹配依据
        self.assertIn("音轨", body)                                # 差异注意点
        self.assertIn("返回审阅修改（全部选择保留）", body)          # 返回修改入口且承诺保留选择
        self.assertIn("待平台验证，暂不可用", body)                 # 清理入口保持禁用
        self.assertNotIn("method=post action='/apply'", body)
        self.assertNotIn("可恢复", body.replace("不可恢复", ""))     # 文案不宣称可恢复


if __name__ == "__main__":
    unittest.main()

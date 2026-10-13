#!/usr/bin/env python3
"""fnOS unified-gateway web UI for the video dedupe core.

Serves on an AF_UNIX socket (fnOS desktop iframe via gatewayPrefix); a TCP
fallback exists for Windows development and tests. Plain stdlib forms, no
client framework. Visual language: Strata / Gainsty design tokens (light
default, emerald accent, Outfit variable font self-hosted, no CDN); 0.2.27
replaces the former Steam-dark identity while keeping markup contracts,
polling behaviour and safety copy untouched.
Only fnOS-forwarded admin requests may read pages or act (X-Trim-Isadmin);
the anonymous surface is static assets only (PUBLIC_GET_PATHS).
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import secrets
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import videodedupe as v
import tasks

GATEWAY_PREFIX = "/app/video-dedupe"  # 统一网关前缀：路由时剥离，渲染时回加

# 任务协调（Review 26.10.12 §四B）：页面不再各自用进程内布尔猜测忙碌/进度，
# 统一读 tasks 表的持久化任务记录；互斥靠 tasks.claim 的跨进程操作锁
# （Web 与 CLI 共用）。_PLAN_PREVIEW 是本进程内计划确认页的预演明细缓存
# （折叠展示用技术信息；跨进程持久的事实来源是任务行与计划文档）。
_PLAN_PREVIEW = {}

# 多帧预览生成串行锁（§三.3/§四C）：preview_frames 一次生成一个文件的五帧，
# 一页 N 个成员的帧图请求并发到达时靠它排队——并发多路 ffmpeg 违反单路串行
# 约定；缓存命中时锁内开销可忽略。仅约束本进程，与扫描任务的互斥无关。
_PREVIEW_LOCK = threading.Lock()

# --- 访问边界（Review 26.10.12 §四E/P1）-------------------------------------
# 匿名可读的 GET 白名单：随包静态资源中的字体（/font）与样式表（/static/app.css）。
# 脚本 app.js 属业务页面资源，与管理员页面同权限（§四E 公开面只列「静态字体和
# 样式」）；业务页面、JSON 接口、缩略图一律要求管理员。
PUBLIC_GET_PATHS = {"/font", "/static/app.css"}
# /static/ 只放随包分发的静态文件（§四E）：文件名白名单精确匹配，无任何路径
# 拼接，业务静态资源不得混入本目录。
_STATIC_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
# POST 表单体上限：勾选清单按万级文件预留的余量，超出即 413（防异常客户端）。
MAX_BODY_BYTES = 4 * 1024 * 1024
# CSRF：进程级随机令牌，渲染时注入所有 POST 表单，提交时统一比对。
# 仅管理员能读到页面（即令牌），跨站请求拿不到它，故无法伪造表单提交。
_CSRF_TOKEN = secrets.token_hex(32)
_POST_FORM_RE = re.compile(r"(<form\b[^>]*\bmethod=post\b[^>]*>)")

# --- 清理执行入口禁用（Review 26.10.12 §四A/§五）--------------------------
# 系统回收站接入尚未在 fnOS 真机验证，而现有执行路径是永久删除（unlink），
# 与「清理必须可恢复」的产品边界不符。结果可恢复性得到平台验证之前（阶段 5
# 翻转 CLEANUP_VERIFIED；中间阶段不得顺手打开）：
# - 计划确认页只读展示清单，不渲染执行表单；
# - POST /apply 一律拒绝且不占用任务；
# - 不启用替代删除路径，文案不宣称可恢复。
# ponytail: 单一布尔开关 + 一组文案，验证通过后翻转即恢复；不做多后端配置体系。
CLEANUP_VERIFIED = False
CLEANUP_DISABLED_TITLE = "清理执行待平台验证，暂不可用"
CLEANUP_DISABLED_REASON = ("<li>当前的删除执行是<strong>直接永久删除</strong>，不进入系统回收站；</li>"
                           "<li>系统回收站接入（恢复语义与文件身份保护）尚未在飞牛真机完成验证；</li>"
                           "<li>验证通过前不开放清理执行，也不提供替代删除路径。</li>")


def _icon(inner, cls="ic"):
    """内联线条图标：统一 viewBox 24 / stroke 1.8 / currentColor，全单引号（守住引号配对测试）。"""
    return ("<svg class='" + cls + "' viewBox='0 0 24 24' fill='none' stroke='currentColor'"
            " stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            + inner + "</svg>")


_PATHS = {
    "logo": "<rect x='8.4' y='5.2' width='10.9' height='10.9' rx='2.5' fill='currentColor' stroke='none' opacity='.4'/><path fill='currentColor' stroke='none' fill-rule='evenodd' d='M7.2 8H13.1A2.5 2.5 0 0 1 15.6 10.5V16.4A2.5 2.5 0 0 1 13.1 18.9H7.2A2.5 2.5 0 0 1 4.7 16.4V10.5A2.5 2.5 0 0 1 7.2 8ZM9.2 11V15.9L13.4 13.4Z'/>",
    "home": "<path d='M3 11.5 12 4l9 7.5'/><path d='M5.5 10.5V20h13v-9.5'/><path d='M10 20v-5.5h4V20'/>",
    "review": "<path d='M16.5 3.5H6.5a3 3 0 0 0-3 3v10'/><rect x='7' y='7' width='13.5' height='13.5' rx='2.2'/><circle cx='11.2' cy='11.2' r='1.4' fill='currentColor' stroke='none'/><path d='M9.6 17.3l2.7-2.7 1.9 1.9 2.3-2.3 2.4 2.4'/>",
    "cog": "<path d='M4 7.5h9.5M18.5 7.5H20M4 16.5h1.5M10.5 16.5H20'/><circle cx='16' cy='7.5' r='2.3'/><circle cx='8' cy='16.5' r='2.3'/>",
    "moon": "<path d='M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z'/>",
    "sun": "<circle cx='12' cy='12' r='4'/><path d='M12 2.8v1.9M12 19.3v1.9M4.8 4.8l1.3 1.3M17.9 17.9l1.3 1.3M2.8 12h1.9M19.3 12h1.9M4.8 19.2l1.3-1.3M17.9 6.1l1.3-1.3'/>",
    "lock": "<rect x='5' y='10.5' width='14' height='10' rx='2.2'/><path d='M8 10.5V8a4 4 0 0 1 8 0v2.5'/>",
    "bulb": "<path d='M12 3a6 6 0 0 0-3.4 10.9c.8.6 1.4 1.5 1.4 2.4v.7h4v-.7c0-.9.6-1.8 1.4-2.4A6 6 0 0 0 12 3Z'/><path d='M9.8 20h4.4'/>",
    "clock": "<circle cx='12' cy='12' r='8.5'/><path d='M12 7.5V12l3.2 2'/>",
    "folder": "<path d='M3.5 7a2 2 0 0 1 2-2h4.2l2.2 2.5h6.6a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z'/>",
    "check": "<path d='M5 12.5l4.5 4.5L19 7.5'/>",
    "done": "<circle cx='12' cy='12' r='8.5'/><path d='M8.5 12.5l2.4 2.4 4.6-5'/>",
    "sparkle": "<path d='M12 3.5l1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8L5.5 10l4.8-1.7Z'/><path d='M18.6 15.9l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7Z'/>",
}
ICONS = {k: _icon(p) for k, p in _PATHS.items()}      # 18px：导航、tile 锁与勾选
ICONS_S = {k: _icon(p, "ic ic-s") for k, p in _PATHS.items()}  # 15px：正文行内


def scan_scope_label(cfg, items):
    """Human label for a stored scan scope (read from the scan result, never
    re-derived from current config). None = all authorized roots in full."""
    if items is None:
        return "全部授权文件夹"
    if not items:
        return "自选（勾选的目录当时不存在，实际未扫描）"
    roots = {r["id"]: r for r in cfg["roots"]}
    names = [f"{(roots.get(i['root']) or {'path': '已移除的目录 ' + i['root']})['path']}/{i['rel']}" for i in items]
    text = "、".join(names[:3]) + (f" 等 {len(names)} 处" if len(names) > 3 else "")
    return f"自选 {len(names)} 处：{text}"

PAGE = """<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>视频查重助手</title>
<script>try{if(localStorage.vdTheme==='dark')document.documentElement.classList.add('dark')}catch(e){}</script>
<link rel=icon href='data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 512 512%27%3E%3Crect width=%27512%27 height=%27512%27 rx=%27112%27 fill=%27%231D4ED8%27/%3E%3Crect x=%27180%27 y=%27110%27 width=%27232%27 height=%27232%27 rx=%2754%27 fill=%27%23FFFFFF%27 fill-opacity=%270.3%27/%3E%3Crect x=%27100%27 y=%27170%27 width=%27232%27 height=%27232%27 rx=%2754%27 fill=%27%23FFFFFF%27/%3E%3Cpath d=%27M196 232 196 340 286 286Z%27 fill=%27%231D4ED8%27/%3E%3C/svg%3E'>
<link rel=stylesheet href='{px}/static/app.css'></head>
<body>
<header class=topbar><div class=in>
<a class=brand href='{px}/'><span class=logo>{ic_logo}</span>视频查重助手</a>
<nav class=tabs>
<a href='{px}/' class='{on_home}'>{ic_home} 概览</a>
<a href='{px}/review' class='{on_review}'>{ic_review} 审阅</a>
<a href='{px}/config' class='{on_config}'>{ic_cog} 设置</a>
</nav>
<span class=grow></span>
<a class=pill id=st-pill href='{px}/' title=任务状态><i></i><span id=st-txt>状态…</span></a>
<span class=ver>{version}</span>
<button class=iconbtn onclick=toggleTheme() title=切换深浅色 aria-label=切换深浅色><span class=only-light>{ic_moon}</span><span class=only-dark>{ic_sun}</span></button>
</div></header>
<main class=wrap>
<script>var VD_PX='{px}';</script>
<script src='{px}/static/app.js'></script>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "videodedupe-web/" + v.VERSION
    protocol_version = "HTTP/1.1"  # 统一网关是 1.1 反向代理；所有响应必须带 Content-Length
    config = None  # set on server

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def note_exception(self, e):
        """原始异常先落 webui.log；错误页发送本身失败（客户端已断开）不再刷屏。"""
        import traceback
        sys.stderr.write("--- request exception ---\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()

    def _safe_write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器/网关已断开，属正常现象

    def address_string(self):
        # AF_UNIX 连接没有 peer 地址（client_address 为空元组）。
        return "gateway" if self.server.gateway else (self.client_address[0] if self.client_address else "-")

    # --- helpers -----------------------------------------------------------
    def page(self, body, status=200, active=""):
        px = getattr(self, "px", "")
        # CSRF（Review 26.10.12 §四E）：所有 POST 表单统一注入隐藏令牌，
        # do_POST 侧统一比对；令牌只出现在管理员可见的页面里。
        body = _POST_FORM_RE.sub(
            lambda m: m.group(1) + "<input type=hidden name=csrf value='" + _CSRF_TOKEN + "'>", body)
        head = (PAGE.replace("{px}", px)
                    .replace("{on_home}", "on" if active == "home" else "")
                    .replace("{on_review}", "on" if active == "review" else "")
                    .replace("{on_config}", "on" if active == "config" else "")
                    .replace("{version}", "v" + v.VERSION))
        for name in ("logo", "home", "review", "cog", "moon", "sun"):
            head = head.replace("{ic_%s}" % name, ICONS[name])
        data = (head + body + "</main></body></html>").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head", False):
            self._safe_write(data)

    def json_out(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self._safe_write(data)

    def redirect(self, path):
        """表单提交成功的统一应答（§四B 请求行为表）：303 → GET 页。
        响应体为空且不含任何脚本——浏览器刷新、回退再前进都不会重提 POST。"""
        self.send_response(303)
        self.send_header("Location", self.px + path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def is_admin(self):
        # 身份注入头（X-Trim-Isadmin / X-Trim-Userid）只在统一网关 AF_UNIX socket
        # 模式可信：socket 由 fnOS 网关独占连接，头部是网关校验会话后重写的，
        # 外部客户端无法直达。--tcp 开发模式下客户端可任意伪造这些头，一律不认；
        # VD_ALLOW_NO_ADMIN=1 是开发逃生阀（服务只监听 127.0.0.1），风险自负。
        if self.server.gateway:
            return self.headers.get("X-Trim-Isadmin", "").lower() == "true"
        return os.environ.get("VD_ALLOW_NO_ADMIN", "") == "1"

    def _drain_body(self):
        """决定不处理该 POST 时排空请求体（有界），保持 keep-alive 连接干净；
        长度不可信则直接断开，避免残留字节污染网关转发的下一个请求。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if 0 <= length <= MAX_BODY_BYTES:
            if length:
                self.rfile.read(length)
        else:
            self.close_connection = True

    def _post_guard(self):
        """POST 统一前置校验（Review 26.10.12 §四E）：管理员 → 请求体上限 → CSRF。
        返回解析后的表单 dict；已自行应答（403/400/413）时返回 None。"""
        if not self.is_admin():
            self._drain_body()
            self.page("<h1>需要管理员权限</h1>"
                      "<p class=lead>此操作仅限管理员执行；请以管理员账号登录飞牛后重试。</p>",
                      status=403, active="home")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True  # 长度不可信，无法安全排空/解析
            self.page(self.error_body("请求无法解析", "Content-Length 不是合法整数。"), status=400, active="home")
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # body 未读取，断开连接避免残留字节错位
            self.page(self.error_body("请求体过大", f"表单数据超过 {MAX_BODY_BYTES // 1048576} MB 上限。"),
                      status=413, active="home")
            return None
        try:
            qs = parse_qs(self.rfile.read(length).decode("utf-8"))
        except UnicodeDecodeError as e:
            raise v.Unsafe("请求体不是有效的 UTF-8 表单数据") from e
        if (qs.get("csrf") or [""])[0] != _CSRF_TOKEN:
            self.page(self.error_body("安全校验未通过",
                                      "表单令牌缺失或不匹配（CSRF 防护）；请从本应用页面重新操作。"),
                      status=403, active="home")
            return None
        return qs

    def cfg(self):
        return v.read_config(self.config)

    def cfg_or_none(self):
        try:
            return self.cfg()
        except (OSError, ValueError):
            return None

    # --- 任务状态（§四B：页面统一读任务记录，不用进程内布尔猜测）-------------
    def active_task(self):
        cfg = self.cfg_or_none()
        return tasks.active(cfg) if cfg else None

    def task_of(self, ttype):
        """某类型最近一条任务记录（含终态）；无配置/库不可读时返回 None。"""
        cfg = self.cfg_or_none()
        return tasks.latest(cfg, ttype) if cfg else None

    def _bound_task(self, qs, ttype):
        """状态/结果页固定任务读取（§四B 请求行为表）：?task= 绑定具体任务行，
        不靠「最近一条」猜测当前状态；不存在或类型不符返回 "missing"，缺省回落
        latest（兼容无任务 ID 的旧链接）。"""
        tid = (qs.get("task") or [""])[0]
        if not tid:
            return "latest", self.task_of(ttype)
        cfg = self.cfg_or_none()
        task = tasks.get(cfg, tid) if cfg else None
        if not task or task["type"] != ttype:
            return "missing", None
        return "bound", task

    def _task_not_found(self):
        self.page("<h1>任务不存在</h1>"
                  "<p class=hint>没有对应编号的任务记录；链接可能已过期，或任务属于其他类型。</p>"
                  f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", status=404, active="home")

    @staticmethod
    def task_result(task):
        if not task or not task.get("result"):
            return None
        try:
            return json.loads(task["result"])
        except ValueError:
            return None

    def busy_page(self, busy):
        """统一忙碌应答（§四B）：重复提交返回当前任务信息，不覆盖、不排队。"""
        task = getattr(busy, "task", None) or {}
        label = tasks.TYPE_LABELS.get(task.get("type"), "后台任务")
        self.page(f"<h1>{label}正在进行中</h1>"
                  "<p>同一工作区同时只运行一个后台任务；当前任务完成后即可开始新的操作。</p>"
                  f"<p><a class='btn big' href='{self.px}/'>查看当前任务进度</a></p>",
                  status=503, active="home")

    def route(self):
        """Strip the gateway prefix for routing (keeping the query); remember it for rendering."""
        path = urlparse(self.path).path
        if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
            self.px = GATEWAY_PREFIX
            stripped = self.path[len(GATEWAY_PREFIX):]
            return stripped if stripped.startswith("/") else "/" + stripped
        self.px = ""
        return self.path

    def do_HEAD(self):
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_GET(self):
        url = urlparse(self.route())
        try:
            # Review 26.10.12 P1/§四E：业务 GET/HEAD 一律管理员；匿名面仅 PUBLIC_GET_PATHS。
            # 403 页不展示日志尾部等内部信息（读取边界从陌生人侧收起）。
            if url.path not in PUBLIC_GET_PATHS and not self.is_admin():
                self.page("<h1>需要管理员权限</h1>"
                          "<p class=lead>此页面仅限管理员查看；请以管理员账号登录飞牛后重试。</p>",
                          status=403, active="home")
                return
            if url.path == "/":
                self.get_home(parse_qs(url.query), active="home")
            elif url.path == "/config":
                self.get_config(active="config")
            elif url.path == "/progress":
                self.get_progress()
            elif url.path == "/plan-status":
                self.get_plan_status(parse_qs(url.query))
            elif url.path == "/plan-review":
                self.get_plan_review(parse_qs(url.query))
            elif url.path == "/apply-status":
                self.get_apply_status(parse_qs(url.query))
            elif url.path == "/apply-result":
                self.get_apply_result(parse_qs(url.query))
            elif url.path == "/browse":
                self.get_browse(parse_qs(url.query))
            elif url.path == "/thumb":
                self.get_thumb(parse_qs(url.query))
            elif url.path == "/preview":
                self.get_preview(parse_qs(url.query))
            elif url.path == "/compare":
                self.get_compare(parse_qs(url.query))
            elif url.path == "/font":
                self.get_font(parse_qs(url.query))
            elif url.path.startswith("/static/"):
                self.get_static(url.path[len("/static/"):])
            elif url.path == "/review":
                self.get_review(parse_qs(url.query))
            elif url.path == "/apply":
                self.get_apply(parse_qs(url.query))
            else:
                self.page("<h1>页面不存在</h1><p class=hint>请从概览页进入。</p>", status=404, active="home")
        except v.Unsafe as e:
            self.page(self.error_body("无法完成该操作", str(e)), status=400, active="home")
        except BlockingIOError:
            self.page(self.error_body("扫描正在进行中", "扫描会占用一段时间，期间此页面暂停使用；请稍后刷新查看进度。"), status=503, active="home")
        except (OSError, ValueError, KeyError) as e:
            self.note_exception(e)
            if "temporarily unavailable" in str(e) or "Errno 11" in str(e):
                self.page(self.error_body("扫描正在进行中", "扫描会占用一段时间，期间此页面暂停使用；请稍后刷新查看进度。"), status=503, active="home")
            else:
                self.page(self.error_body("出错了", str(e)), status=500, active="home")

    def do_POST(self):
        url = urlparse(self.route())
        try:
            qs = self._post_guard()
            if qs is None:
                return
            if url.path == "/scan":
                self.post_scan(qs)
            elif url.path == "/cancel":
                self.post_cancel(qs)
            elif url.path == "/draft":
                self.post_draft(qs)
            elif url.path == "/plan":
                self.post_plan(qs)
            elif url.path == "/apply":
                self.post_apply(qs)
            elif url.path == "/report":
                self.post_report(qs)
            else:
                self.page("<h1>页面不存在</h1>", status=404, active="home")
        except v.Unsafe as e:
            self.note_exception(e)
            self.page(self.error_body("操作被安全检查拦下", str(e)), status=400, active="home")
        except BlockingIOError as e:
            self.note_exception(e)
            self.page(self.error_body("扫描正在进行中", "请等待扫描完成后再执行该操作。"), status=503, active="home")
        except (OSError, ValueError, KeyError) as e:
            self.note_exception(e)
            self.page(self.error_body("出错了", str(e)), status=500, active="home")

    @staticmethod
    def tail_log(cfg, lines=40):
        """webui.log 就在应用数据目录；错误页直接展示最近日志，免去 SSH。"""
        try:
            with open(Path(cfg["state_dir"]) / "webui.log", "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 16384))
                text = f.read().decode("utf-8", "replace")
            tail = "\n".join(text.splitlines()[-lines:])
            return tail or "（日志暂无内容）"
        except OSError:
            return "（日志文件暂不可读）"

    def error_body(self, title, detail):
        cfg = self.cfg_or_none()
        log_tail = self.tail_log(cfg) if cfg else "（尚未生成配置）"
        return (f"<h1>{html.escape(title)}</h1><div class=card><p><b>{html.escape(detail)}</b></p>"
                f"<p class=hint>下面的运行日志包含技术细节，反馈问题时请连同截图：</p>"
                f"<pre>{html.escape(log_tail)}</pre></div>"
                "<p><a class='btn ghost' href='javascript:history.back()'>返回上一步</a></p>")

    # --- overview ------------------------------------------------------------
    def get_home(self, qs=None, active=""):
        cfg = self.cfg_or_none()
        if cfg is None or not cfg["roots"]:
            self.page(
                "<h1>欢迎使用视频查重助手</h1><p class=lead>帮您找出重复和画面相似的视频，确认后再安全删除，释放存储空间。</p>"
                "<div class=card><h2 style=margin-top:0>开始之前：选择要扫描的文件夹</h2>"
                "<p>本应用<strong>不会自动访问</strong>您的文件。请先授权：</p>"
                "<ul class=plain>"
                "<li>打开飞牛的 <b>系统设置 → 应用 → 视频查重助手 → 访问权限</b></li>"
                "<li>点「添加」，选择存放视频的文件夹</li>"
                "<li>权限选择：<span class='badge b-ok'>读写</span> 表示「确认后可以删除这里的重复文件」；"
                "<span class='badge b-ref'>只读</span> 表示「只用来对比参考，绝不删除」</li></ul>"
                "<p>添加完成后，回到应用中心<strong>停止本应用再启动</strong>，然后回到本页开始第一次扫描。</p></div>")
            return
        progress = v.read_progress(cfg) or {"phase": "idle"}
        phase = progress.get("phase", "idle")
        # §四B：运行态以任务记录为准（重启后核对已终结陈旧行，页面不再长期
        # 显示执行中）；progress.json 只提供实时数字与终态相位。
        active = tasks.active(cfg)
        plan_running = active is not None and active["type"] == "plan"
        applying = active is not None and active["type"] == "apply"
        scanning = active is not None and active["type"] == "scan"
        # 阶段链（Strata Monitor 式状态条）：done=已过、act=进行中、err=出错、默认灰=未到
        if applying:
            chain = ("done", "done", "done", "act")
        elif plan_running:
            chain = ("done", "done", "act", "next")
        elif scanning:
            chain = ("act", "next", "next", "next")
        elif phase in {"error", "interrupted", "cancelled"}:
            chain = ("err", "next", "next", "next")
        elif phase == "plan_ready":
            chain = ("done", "done", "act", "next")
        elif phase == "apply_done":
            chain = ("done", "done", "done", "done")
        elif phase == "done":
            chain = ("done", "next", "next", "next")
        else:
            chain = ("next", "next", "next", "next")
        # §四F 报告导出独立操作：POST /report 成功后 303 回本页，提示导出位置
        report_banner = ""
        if (qs or {}).get("report") and cfg:
            report_banner = (f"<div class='card banner'><p class=m0>{ICONS_S['done']} 审计报告已导出到"
                             f" <code>{html.escape(cfg.get('export_dir', ''))}</code>"
                             "（CSV 明细与 HTML 报告各一份，文件名以扫描编号开头）。</p></div>")
        parts = [report_banner, "<h1>概览</h1>",
                 "<div class=chain>"
                 f"<span class='ph {chain[0]}'><i></i>扫描</span>"
                 f"<span class='ph {chain[1]}'><i></i>生成计划</span>"
                 f"<span class='ph {chain[2]}'><i></i>确认删除</span>"
                 f"<span class='ph {chain[3]}'><i></i>清理完成</span>"
                 "</div>"]
        if applying:
            done, total = progress.get("done", 0), progress.get("total", 0)
            freed = progress.get("freed", 0)
            pct = min(100, int(done * 100 / total)) if total else 0
            parts.append("<div class=card id=applybox>"
                         "<h3><span class='status-dot d-run'></span>正在执行删除（<span id=a-done>"
                         f"{done:,}</span> / <span id=a-total>{total:,}</span> 个文件）</h3>"
                         "<div class=bar><div id=a-bar style='width:" + str(pct) + "%'></div></div>"
                         f"<p class=hint>已释放约 <span id=a-freed>{freed / 2**20:.0f}</span> MB；"
                         "每个文件删除前会做一次内容校验（耗时与文件大小成正比）。</p>"
                         f"<p><a class='btn ghost' href='{self.px}/apply-status?task={active['id']}'>查看详细状态</a></p></div>")
            parts.append("""<script>
vdPoll('""" + self.px + """/apply-status',d=>{
  document.getElementById('a-done').textContent=(d.done||0).toLocaleString();
  document.getElementById('a-total').textContent=(d.total||0).toLocaleString();
  const t=d.total||0,p=t?Math.min(100,Math.round((d.done||0)*100/t)):0;
  document.getElementById('a-bar').style.width=p+'%';
  document.getElementById('a-freed').textContent=((d.freed||0)/1048576).toFixed(0);
},d=>{
  if(d.phase!=='applying'){location.reload();return true;}
  return false;
});
</script>""")
        elif plan_running and phase == "planning":
            done, total = progress.get("done", 0), progress.get("total", 0)
            pct = min(99, int(done * 100 / total)) if total else 0
            parts.append("<div class=card id=planbox>"
                         "<h3><span class='status-dot d-run'></span>正在生成清理计划（已核对 <span id=p-done>"
                         f"{done:,}</span> / <span id=p-total>{total:,}</span> 个文件）</h3>"
                         "<div class=bar><div id=p-bar style='width:" + str(pct) + "%'></div></div>"
                         f"<p class=hint>正在核对：<span id=p-cur>{html.escape(str(progress.get('current', '')))}</span>。"
                         "这一步只读文件、不做任何修改；完成后可在「审阅」页继续。</p>"
                         f"<p><a class='btn ghost' href='{self.px}/plan-status?task={active['id']}'>查看详细状态</a></p></div>")
            parts.append("""<script>
vdPoll('""" + self.px + """/progress',d=>{
  document.getElementById('p-done').textContent=(d.done||0).toLocaleString();
  document.getElementById('p-total').textContent=(d.total||0).toLocaleString();
  const t=d.total||0,p=t?Math.min(99,Math.round((d.done||0)*100/t)):0;
  document.getElementById('p-bar').style.width=p+'%';
  document.getElementById('p-cur').textContent=d.current||'';
},d=>{
  if(d.phase!=='planning'){location.reload();return true;}
  return false;
});
</script>""")
        elif scanning:
            # §三.2 三阶段进度：查找视频（总数未知→只报已发现数，不伪造百分比/
            # 剩余时间）、识别内容（done/total 真实进度）、整理结果（两两比对）。
            stopped = active.get("status") == tasks.STOPPING
            phase_p = progress.get("phase", "fingerprint")
            if phase_p == "discover":
                label, num, pct, bar_vis = "正在查找视频", f"{progress.get('found', 0):,} 个视频已发现", 0, "display:none"
            elif phase_p == "grouping":
                done, total = progress.get("done", 0), progress.get("total", 0)
                pct = min(99, int(done * 100 / total)) if total else 0
                label, num, bar_vis = "正在整理结果", f"已比较 {done:,} / {total:,} 个候选", ""
            else:
                done, total = progress.get("done", 0), progress.get("total", 0)
                pct = min(99, int(done * 100 / total)) if total else 0
                label, num, bar_vis = "正在识别内容", f"已识别 {done:,} / {total:,} 个视频", ""
            if stopped:
                stop_html = ("<p style='margin-top:10px'><span class='badge b-warn'>已请求停止</span> "
                             "<span class=hint>正在等待当前步骤结束，请稍候…</span></p>")
            else:
                stop_html = (f"<form method=post style='margin-top:10px' action='{self.px}/cancel' "
                             "data-confirm='确定要取消这次扫描吗？已成功识别的结果会保留；再次扫描会复用已有识别结果。'>"
                             f"<input type=hidden name=task value='{active['id']}'><button class='btn danger'>停止扫描</button></form>")
            parts.append("<div class=card id=scanbox>"
                         f"<h3><span class='status-dot d-run'></span><span id=p-label>{label}</span> · "
                         f"<span id=p-num style='font-size:14px;font-weight:500;color:var(--muted)'>{num}</span></h3>"
                         f"<div class=bar id=p-bar-wrap style='{bar_vis}'><div id=p-bar style='width:{pct}%'></div></div>"
                         f"<p class=hint>正在处理：<span id=p-cur>{html.escape(str(progress.get('current', '')))}</span></p>"
                         + stop_html + "</div>")
            # 三阶段静默轮询：阶段切换只换文案与数字，不整页刷新（白屏闪烁）；
            # 结束（完成/失败/已停止）时刷新一次呈现对应结束状态卡。
            parts.append("""<script>
vdPoll('""" + self.px + """/progress',d=>{
  const label=document.getElementById('p-label');
  const num=document.getElementById('p-num');
  const wrap=document.getElementById('p-bar-wrap');
  const bar=document.getElementById('p-bar');
  const cur=document.getElementById('p-cur');
  if(!label)return;
  if(d.phase==='discover'){
    label.textContent='正在查找视频';
    num.textContent=(d.found||0).toLocaleString()+' 个视频已发现';
    if(wrap)wrap.style.display='none';
    cur.textContent=d.current||'';
  }else if(d.phase==='fingerprint'){
    label.textContent='正在识别内容';
    num.textContent='已识别 '+(d.done||0).toLocaleString()+' / '+(d.total||0).toLocaleString()+' 个视频';
    const t=d.total||0;
    if(wrap){wrap.style.display='';}
    if(bar){bar.style.width=(t?Math.min(99,Math.round((d.done||0)*100/t)):0)+'%';}
    cur.textContent=d.current||'';
  }else if(d.phase==='grouping'){
    label.textContent='正在整理结果';
    num.textContent='已比较 '+(d.done||0).toLocaleString()+' / '+(d.total||0).toLocaleString()+' 个候选';
    const t=d.total||0;
    if(wrap){wrap.style.display='';}
    if(bar){bar.style.width=(t?Math.min(99,Math.round((d.done||0)*100/t)):0)+'%';}
    cur.textContent='';
  }
},d=>{
  if(d.phase==='done'||d.phase==='error'||d.phase==='cancelled'){location.reload();return true;}
  return false;
});
</script>""")
        elif phase == "error":
            parts.append("<div class=card><h3><span class='status-dot d-err'></span>上次扫描遇到问题</h3>"
                         f"<p><b>{html.escape(str(progress.get('error', '')))}</b></p>"
                         f"<p class=hint>运行日志（最近部分）：</p>"
                         f"<pre>{html.escape(self.tail_log(cfg, 25))}</pre>"
                         f"<form method=post action='{self.px}/scan' data-scan-scope><button class=btn>重新尝试扫描</button></form></div>")
        elif phase == "cancelled":
            # §三.2 已停止：独立结束状态——给停止位置、数量与「复用已有识别结果」
            # 的明确说法（缓存键 (path,stat)，不称精确断点续跑）。
            where = progress.get("stopped_at", "")
            stage = {"discover": "查找视频", "fingerprint": "识别内容", "grouping": "整理结果"}.get(where, "扫描")
            found = progress.get("found", 0)
            if where == "fingerprint" and progress.get("total"):
                detail = f"停止时正在「{stage}」：已发现 {found:,} 个视频，已识别 {progress.get('done', 0):,} / {progress.get('total', 0):,} 个。"
            elif where == "grouping":
                detail = f"停止时正在「{stage}」：{found:,} 个视频已识别完成。"
            else:
                detail = f"停止时正在「{stage}」：已发现 {found:,} 个视频。"
            parts.append("<div class=card><h3><span class='status-dot d-warn'></span>扫描已停止</h3>"
                         f"<p>{detail}</p>"
                         "<p class=hint>已成功计算的识别结果（画面指纹、缩略图等）已保留；"
                         "<b>再次扫描会复用已有识别结果</b>，只对没识别过或发生变化的文件重新计算。</p>"
                         f"<form method=post action='{self.px}/scan' data-scan-scope><button class='btn big'>再次扫描</button></form></div>")
        else:
            dot = "d-ok" if phase == "done" else "d-idle"
            if phase == "done":
                scan_id = progress.get("scan_id", "")
                summary = progress.get("summary") or {}
                skipped_total = int(summary.get("skipped_total", 0))
                with v.state(cfg, exclusive=False) as db:
                    scope_used = v.load_scope(db, scan_id)
                # §三.2 结束状态：完成 vs 完成但有跳过——同一 summary 给出数量、
                # 原因样本与下一步，页面不再自行猜测。
                stats = ""
                if summary:
                    stats = (f"<p class=hint>共识别 {summary.get('files', 0):,} 个视频："
                             f"完全重复 {summary.get('exact_groups', 0):,} 组、画面相似 {summary.get('visual_groups', 0):,} 组。</p>")
                if skipped_total:
                    samples = [*(summary.get("scan_warning_samples") or []), *(summary.get("fingerprint_error_samples") or [])]
                    sample_html = "".join(f"<li>{html.escape(str(s))}</li>" for s in samples[:5])
                    skip_html = (f"<p style='margin-top:8px'><span class='badge b-warn'>完成，但有 {skipped_total} 处跳过</span> "
                                 "本轮未能识别以下文件或目录（消失、不可读或视频损坏），不影响其余结果：</p>"
                                 f"<ul class=plain>{sample_html}</ul>"
                                 "<p class=hint>下次扫描会自动重试被跳过的部分。</p>")
                    title = "扫描完成（部分内容被跳过）"
                else:
                    skip_html = ""
                    title = "扫描完成"
                parts.append(f"<div class=card><h3><span class='status-dot {dot}'></span>{title}</h3>"
                             f"<p class=hint>本次范围：{html.escape(scan_scope_label(cfg, scope_used))}。</p>{stats}{skip_html}"
                             "<p class=hint>扫描会逐个读取视频画面并计算指纹；之后再次扫描会自动跳过没变化的文件，速度很快。</p>"
                             f"<a class='btn big' href='{self.px}/review?scan={scan_id}'>查看发现的重复视频</a> "
                             f"<form method=post style=display:inline action='{self.px}/report'>"
                             f"<input type=hidden name=scan value='{html.escape(scan_id)}'>"
                             "<button class='btn ghost'>导出审计报告</button></form> "
                             f"<form method=post style=display:inline action='{self.px}/scan' data-scan-scope>"
                             "<button class='btn ghost'>重新扫描</button></form></div>")
            else:
                parts.append("<div class=card><h3><span class='status-dot d-idle'></span>空闲，尚未扫描</h3>"
                             "<p class=hint>扫描会逐个读取视频画面并计算指纹，大文件夹首次可能需要数小时；期间建议暂停下载和文件整理。"
                             "之后再次扫描会自动跳过没变化的文件，速度很快。</p>"
                             f"<form method=post action='{self.px}/scan' data-scan-scope>"
                             "<button class='btn big'>开始扫描</button></form></div>")
        # 指标大数字卡：单一来源 = scans.summary（Review 26.10.12 §四D/P2「首页
        # 重复计算建议」收口：扫描完成时随结果持久化的聚合指标，候选口径与审阅
        # 页「应用建议」同源）；旧库行缺摘要时由 scan_metrics 按需现算，只读。
        metrics = None
        if not (scanning or applying or plan_running):
            with v.state(cfg, exclusive=False) as db:
                row = db.execute("SELECT id FROM scans ORDER BY created DESC LIMIT 1").fetchone()
                if row:
                    m = v.scan_metrics(db, cfg, row[0])
                    if m:
                        metrics = (m["groups"], m["candidates"], m["reclaim"], m["files"])

        def _metric(num, lab, sub):
            return (f"<div class=metric><div class=num>{num}</div><div class=lab>{lab}</div>"
                    f"<div class=sub>{sub}</div></div>")

        if metrics:
            g_n, c_n, reclaim, files_n = metrics
            human = (f"{reclaim / 2**30:.1f} GB" if reclaim >= 2**30
                     else f"{reclaim / 2**20:.1f} MB" if reclaim >= 2**20
                     else f"{reclaim / 2**10:.0f} KB")
            m_html = (_metric(f"{g_n:,}", "重复组", "疑似重复或相似的视频分组")
                      + _metric(f"{c_n:,}", "建议可清理", "点「应用建议」才会勾选的文件数")
                      + _metric(human, "可释放空间", "清理全部建议项后预计释放")
                      + _metric(f"{files_n:,}", "视频总数", "最近一次扫描覆盖的文件"))
        else:
            m_html = (_metric("—", "重复组", "完成扫描后显示")
                      + _metric("—", "建议可清理", "完成扫描后显示")
                      + _metric("—", "可释放空间", "完成扫描后显示")
                      + _metric("—", "视频总数", "完成扫描后显示"))
        parts.append("<div class=metrics>" + m_html + "</div>")
        dirs = "".join(
            f"<div class=card><h3>{ICONS['folder']} <code>{html.escape(r['path'])}</code></h3>"
            f"<p class=role>{'<span class=\"badge b-ok\">读写授权：确认后可删除此处重复视频</span>' if r['role'] == 'cleanup' else '<span class=\"badge b-ref\">只读授权：仅作对比参考，绝不删除</span>'}</p>"
            f"<p><button type=button class='btn ghost sm' onclick=\"browseTree(this,'{html.escape(r['id'], True)}')\">选择要扫描的子文件夹</button></p>"
            f"<div id='tree-{html.escape(r['id'], True)}' style='display:none'></div></div>"
            for r in cfg["roots"])
        # §三.1：扫描范围显式双模式——「全部授权文件夹」与「只扫描勾选的子文件夹」
        # 由用户明确选择；自选模式未勾选任何子文件夹时禁止启动（前端不提交 +
        # 服务端 400 双保险），不再是「未勾选=扫描全部」的隐式回退。
        # 已有审阅草稿时说明旧草稿归属：草稿绑定扫描编号（scan_guard），新扫描
        # 生成新编号，绝不自动沿用旧选择。
        draft_note = ""
        with v.state(cfg, exclusive=False) as db:
            drow = db.execute("SELECT d.scan FROM drafts d JOIN scans s ON s.id=d.scan "
                              "ORDER BY s.created DESC LIMIT 1").fetchone()
        if drow:
            draft_note = ("<p class=hint>上次审阅的选择草稿保存在扫描 <code>"
                          + html.escape(str(drow[0])[:12]) + "…</code> 名下（可在「历次扫描」回到该页继续）；"
                          "<b>新扫描会生成新编号，不会沿用旧选择</b>。</p>")
        parts.append("<h2>扫描范围</h2>"
                     "<p class=hint>在「系统设置 → 应用 → 访问权限」中增删文件夹；改动后重启本应用生效。</p>"
                     + draft_note
                     + "<p class=mode-picker>"
                     "<label><input type=radio name=vd-scan-mode value=all checked onchange=updateScopeSummary()> 扫描全部已授权文件夹</label>"
                     "<label><input type=radio name=vd-scan-mode value=pick onchange=updateScopeSummary()> 只扫描勾选的子文件夹</label>"
                     "</p>"
                     f"<div class=grid>{dirs}</div>"
                     "<p id=scope-summary class=hint>当前选择：扫描全部已授权文件夹</p>")
        # 子文件夹树：懒加载浏览 + 勾选；勾选绑定根身份（rootid:rel），服务端只认该根下的该子目录
        parts.append("""<script>
const VD_ROOTS = """ + json.dumps({r["id"]: {"path": r["path"], "role": r["role"]} for r in cfg["roots"]}, ensure_ascii=False).replace("</", "<\\/") + """;
const treeCache = {};
async function browseTree(btn, rootId){
  const box = document.getElementById('tree-' + rootId);
  if (box.style.display === 'none') {
    box.style.display = 'block';
    if (!box.dataset.loaded) { await loadDir(rootId, '', box); box.dataset.loaded = '1'; }
  } else { box.style.display = 'none'; }
}
async function loadDir(rootId, rel, container){
  const key = rootId + '|' + rel;
  const spinner = document.createElement('div');
  spinner.className = 'hint'; spinner.textContent = '加载中…';
  container.appendChild(spinner);
  const qs = new URLSearchParams({root: rootId, rel: rel});
  const res = await fetch('""" + self.px + """/browse?' + qs);
  spinner.remove();
  if (!res.ok) { container.textContent += '（此文件夹无法读取）'; return; }
  const dirs = await res.json();
  if (!dirs.length) { container.innerHTML += '<div class=hint>（没有子文件夹了）</div>'; return; }
  const ul = document.createElement('ul');
  ul.className = 'tree';
  for (const d of dirs) {
    const li = document.createElement('li');
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = rootId + ':' + d.rel;
    cb.dataset.root = rootId; cb.dataset.rel = d.rel;
    cb.addEventListener('change', updateScopeSummary);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(' ' + d.name));
    li.appendChild(label);
    const childBox = document.createElement('div');
    childBox.className = 'subtree'; childBox.style.display = 'none';
    const expander = document.createElement('span');
    expander.textContent = ' ▸'; expander.className = 'hint'; expander.style.cursor = 'pointer';
    expander.onclick = async () => {
      if (childBox.style.display === 'none') {
        childBox.style.display = 'block';
        if (!childBox.dataset.loaded) { await loadDir(rootId, d.rel, childBox); childBox.dataset.loaded = '1'; }
        expander.textContent = ' ▾';
      } else { childBox.style.display = 'none'; expander.textContent = ' ▸'; }
    };
    label.appendChild(expander);
    li.appendChild(childBox);
    ul.appendChild(li);
  }
  container.appendChild(ul);
}
function scanMode(){
  const r = document.querySelector('input[name="vd-scan-mode"]:checked');
  return r ? r.value : 'all';
}
function pickedScope(){
  const picked = [];
  document.querySelectorAll('.tree input:checked').forEach(cb => picked.push(cb.value));
  return picked;
}
function updateScopeSummary(){
  const el = document.getElementById('scope-summary');
  if (scanMode() === 'all') { el.textContent = '当前选择：扫描全部已授权文件夹'; return; }
  const picked = [];
  document.querySelectorAll('.tree input:checked').forEach(cb => {
    const info = VD_ROOTS[cb.dataset.root] || {};
    const loc = (info.path || cb.dataset.root) + '/' + (cb.dataset.rel || '');
    picked.push(loc + (info.role === 'reference' ? '（仅参考）' : '（可清理）'));
  });
  el.textContent = picked.length ? '已选择 ' + picked.length + ' 处：' + picked.join('、')
                                 : '尚未勾选任何子文件夹——请先在上方勾选，或改选「扫描全部已授权文件夹」';
}
// Review 26.10.12 P1：显式 submit 监听 + preventDefault，取消确认绝不提交。
// 旧内联 onsubmit='return startScopedScan(this)' 因 async 返回 Promise 恒真值，
// confirm 取消后表单照样提交；函数体内本无 await，改回同步布尔即可被监听器使用。
// §三.1：模式随表单显式提交（scope_mode）；自选模式未勾选任何子文件夹时
// 禁止启动（alert 后不提交，服务端同样拒绝），绝不悄悄变成全盘扫描。
function startScopedScan(form){
  const mode = scanMode();
  const picked = pickedScope();
  if (mode === 'pick') {
    if (!picked.length) {
      alert('「只扫描勾选的子文件夹」需要先勾选至少一个子文件夹；如要扫描全部，请选择「扫描全部已授权文件夹」。');
      return false;
    }
    if (!confirm('只扫描勾选的 ' + picked.length + ' 处子文件夹，现在开始吗？')) return false;
  } else if (!confirm('将扫描全部已授权文件夹，现在开始吗？')) {
    return false;
  }
  const modeInput = document.createElement('input');
  modeInput.type = 'hidden'; modeInput.name = 'scope_mode'; modeInput.value = mode;
  form.appendChild(modeInput);
  for (const v of picked) {
    const input = document.createElement('input');
    input.type = 'hidden'; input.name = 'scope'; input.value = v;
    form.appendChild(input);
  }
  return true;
}
document.querySelectorAll('form[data-scan-scope]').forEach(f => f.addEventListener('submit', e => {
  if (!startScopedScan(f)) e.preventDefault();
}));
</script>""")
        with v.state(cfg, exclusive=False) as db:
            scans = db.execute("SELECT id,created,scope FROM scans ORDER BY created DESC LIMIT 10").fetchall()
        if scans:
            rows = "".join(
                f"<tr><td><code>{s[:12]}…</code></td><td>{created}</td>"
                f"<td class=hint>{html.escape(scan_scope_label(cfg, json.loads(scope) if scope else None))}</td>"
                f"<td><a class='btn ghost sm' href='{self.px}/review?scan={s}'>查看重复情况</a></td></tr>"
                for s, created, scope in scans)
            parts.append("<h2>历次扫描</h2><table><tr><th>编号</th><th>时间</th><th>范围</th><th></th></tr>" + rows + "</table>")
        else:
            parts.append("<h2>历次扫描</h2><p class=hint>还没有扫描记录。完成第一次扫描后，这里可以查看每组重复视频。</p>")
        self.page("".join(parts), active=active or "home")

    # --- config ---------------------------------------------------------------
    def get_config(self, active=""):
        cfg = self.cfg_or_none()
        if cfg is None or not cfg["roots"]:
            self.get_home()
            return
        dirs = "".join(
            f"<tr><td><code>{html.escape(r['path'])}</code></td>"
            f"<td>{'<span class=\"badge b-ok\">读写：可清理</span>' if r['role'] == 'cleanup' else '<span class=\"badge b-ref\">只读：仅参考</span>'}</td></tr>"
            for r in cfg["roots"])
        banner = ""
        active = self.active_task()
        if active is not None and active["type"] == "plan":
            banner = f"<div class='card banner'><p class=m0>{ICONS_S['clock']} 清理计划正在后台生成。<a href='{self.px}/plan-status?task={active['id']}'>查看进度</a></p></div>"
        self.page(
            banner +
            "<h1>设置</h1><p class=lead>这里展示当前设置；日常使用只需关心「扫描范围」。</p>"
            "<div class=card><h2>扫描范围</h2>"
            "<p>来自「系统设置 → 应用 → 视频查重助手 → 访问权限」：</p>"
            "<table><tr><th>文件夹</th><th>授权与含义</th></tr>" + dirs + "</table>"
            "<p class=hint>想增删文件夹或改权限？在上述设置页操作，然后到应用中心停止并启动本应用。"
            "改动后旧的扫描结果作废，需要重新扫描（已识别过的文件不会重复耗时）。</p></div>"
            "<div class=card><h2>删除记录存档</h2>"
            f"<p>每次执行删除，都会在共享文件夹 <code>{html.escape(cfg.get('export_dir', ''))}</code> 留下一份"
            "<strong>删除明细记录</strong>（删了哪些文件、何时、多大），您在文件管理器里随时能查，作为事后核对依据。</p>"
            "<p class=hint>视频预览图和扫描数据库属于应用内部数据，不在共享目录中。</p></div>"
            "<h2>诊断信息</h2><div class=card><p>应用版本 <code>"
            + html.escape(v.VERSION) + "</code> · 引擎 " + html.escape(tool_versions(cfg))
            + "</p><p class=hint>运行日志（最近部分，反馈问题时请截图）：</p><pre>"
            + html.escape(self.tail_log(cfg, 25)) + "</pre></div>"
            "<details><summary>原始配置（JSON）</summary><pre>"
            + html.escape(json.dumps({k: val for k, val in sorted(cfg.items()) if not k.startswith('_')}, ensure_ascii=False, indent=2, default=str))
            + "</pre></details>", active=active or "config")

    # --- scan / cancel ----------------------------------------------------------
    def post_scan(self, qs):
        cfg = self.cfg()
        # 范围条目形如 rootid:相对路径（root id 不含冒号，首个冒号分隔），
        # 服务端按配置校验绑定与路径合法性。
        # §三.1：范围模式显式化——scope_mode=pick 必须携带范围条目，绝不静默
        # 回退全盘（前端未勾选不提交，此处兜底拒绝）；scope_mode=all 只扫全部，
        # 忽略树上残留的勾选；缺省（旧入口/直接 POST）维持「无 scope = 全部授权目录」。
        mode = (qs.get("scope_mode") or [""])[0]
        if mode not in ("", "all", "pick"):
            self.page(self.error_body("未知的扫描范围模式", "范围模式参数无效，请从页面重新提交。"), status=400, active="home")
            return
        scope_values = qs.get("scope", [])
        if mode == "pick" and not scope_values:
            self.page(self.error_body("还没有选择要扫描的子文件夹",
                                      "「只扫描勾选的子文件夹」需要先勾选至少一个子文件夹；"
                                      "如要扫描全部，请改选「扫描全部已授权文件夹」。"), status=400, active="home")
            return
        if mode == "all":
            scope_values = []
        if scope_values:
            cfg["scan_scope"] = sorted(set(scope_values))
            v.scan_scope(cfg)  # Unsafe -> 400：未知根/绝对路径/../符号链接，验证在前不占任务
        # §四B：配置与参数验证完成后才占用任务；检查与占用在操作锁内原子完成，
        # 忙碌判断不再依赖进程内布尔（Web 与 CLI 共用同一把锁）。
        try:
            task = tasks.claim(cfg, "scan")
        except tasks.Busy as busy:
            self.busy_page(busy)
            return
        threading.Thread(target=scan_worker, args=(cfg,), daemon=True).start()
        # §四B：POST 成功一律 303 → 带任务 ID 的 GET 页（概览即扫描进度页），
        # 响应体不含脚本，刷新/回退不会重提 POST。
        self.redirect("/?task=" + task["id"])

    def post_cancel(self, qs):
        cfg = self.cfg()
        active = tasks.active(cfg)
        # §四B：停止绑定具体任务 ID，不能取消其他任务；不匹配/已终结即明确拒绝。
        task_id = (qs.get("task") or [""])[0]
        if task_id and (not active or active["id"] != task_id):
            self.page(self.error_body("没有匹配的运行中任务",
                                      "要停止的任务不存在或已经结束；请刷新页面后从当前任务卡操作。"), status=400, active="home")
            return
        if active and active["type"] != "scan":
            self.page(self.error_body("该任务不支持在线停止",
                                      "当前运行的是「" + tasks.TYPE_LABELS.get(active["type"], "后台任务") +
                                      "」，为只读核对或删除安全流程，不能中途打断；请等待其完成。"), status=400, active="home")
            return
        if active:
            tasks.request_stop(cfg, active["id"])  # running→stopping，页面与任务行一致
        v.cancel_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        v.cancel_path(cfg).touch()
        # §四B：POST 成功一律 303 → 概览（带被停止任务的 ID；无任务行时纯概览）
        self.redirect(("/?task=" + active["id"]) if active else "/")

    def post_report(self, qs):
        """审计报告导出（Review 26.10.12 §四F 独立操作）：显式触发、秒级完成，
        只读数据库 + 写 export_dir，不占用任务行——报告成败只影响本次应答，
        绝不改写扫描/清理的业务状态；失败经统一异常分支如实呈现错误页。"""
        cfg = self.cfg()
        scan_id = (qs.get("scan") or [""])[0]
        if not scan_id:
            with v.state(cfg, exclusive=False) as db:
                row = db.execute("SELECT id FROM scans ORDER BY created DESC LIMIT 1").fetchone()
            if not row:
                self.page(self.error_body("没有可导出的扫描", "还没有完成过扫描；请先完成扫描，再导出审计报告。"),
                          status=400, active="home")
                return
            scan_id = row[0]
        v.export_audit(cfg, scan_id)  # Unsafe(越界/配置变化)/OSError(export_dir 不可写) → 统一错误页
        self.redirect("/?report=done&scan=" + scan_id)

    # --- review -----------------------------------------------------------------
    # §四D/P2：审阅页服务端分页（每页 20 组）+ 超大分组成员预览（默认前 12 个，
    # ?full=<gid> 展开）——万级结果不再每次审阅全量加载文件记录与缩略图。
    REVIEW_PAGE_SIZE = 20
    GROUP_MEMBER_PREVIEW = 12

    @staticmethod
    def _qp(qs, name, default=""):
        value = (qs.get(name) or [""])[0].strip()
        return value or default

    def _review_filters(self, qs, groups, draft):
        """解析审阅筛选（§三.3/§四D）：类型 / 待处理-已处理 / 文件夹 / 名称。
        默认先显示完全相同结果（无完全相同组时回落全部）；已处理 = 草稿中该组
        有明确决定（待清理选择、跳过或已确认差异），未触碰即待处理。"""
        kind = self._qp(qs, "kind")
        if kind not in {"exact", "visual", "all"}:
            kind = "exact" if any(g["kind"] == "exact" for g in groups) else "all"
        state_f = self._qp(qs, "state")
        if state_f not in {"pending", "done", "all"}:
            state_f = "all"

        def decided(g):
            ent = draft["groups"].get(g["gid"]) or {}
            return bool(ent.get("clean") or ent.get("confirmed") or ent.get("skip"))

        shown, root_f, q_raw = [], self._qp(qs, "root"), self._qp(qs, "q")
        needle = q_raw.lower()
        for g in groups:
            if kind != "all" and g["kind"] != kind:
                continue
            if root_f and not any(k.startswith(root_f + ":") for k in g["members"]):
                continue
            if needle and not any(needle in k.rsplit("/", 1)[-1].lower() for k in g["members"]):
                continue
            if state_f == "pending" and decided(g):
                continue
            if state_f == "done" and not decided(g):
                continue
            shown.append(g)
        return {"kind": kind, "state": state_f, "root": root_f, "q": q_raw}, shown

    def _review_url(self, scan_id, page, f, full=""):
        """审阅页链接（翻页/展开）：保留全部当前筛选参数，避免翻页后回落默认。"""
        params = {"scan": scan_id, "page": str(page)}
        for name in ("kind", "state", "root"):
            if f[name]:
                params[name] = f[name]
        if f["q"]:
            params["q"] = f["q"]
        if full:
            params["full"] = full
        return f"{self.px}/review?{urlencode(params)}"

    def get_review(self, qs, active=""):
        scan_id = self._qp(qs, "scan")
        if not scan_id:
            cfg = self.cfg()
            with v.state(cfg, exclusive=False) as db:
                row = db.execute("SELECT id FROM scans ORDER BY created DESC LIMIT 1").fetchone()
            if not row:
                self.page("<h1>还没有可审阅的结果</h1><p class=lead>先到概览页完成一次扫描，这里就会显示发现的重复视频。</p>"
                          f"<p><a class=btn href='{self.px}/'>去概览</a></p>", active="review")
                return
            scan_id = row[0]
        active = self.active_task()
        if active is not None and active["type"] == "scan":
            self.page("<h1>扫描正在进行中</h1><p>请等扫描完成后再来审阅；<a href='" + self.px + "/'>查看进度</a>。</p>", status=503, active="review")
            return
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            # 组行轻量（成员 key 列表 + 持久化建议）；文件记录只按本页渲染需要加载
            groups = v.load_groups(db, scan_id)   # Unsafe（扫描不存在/配置变化）→ 统一 400 页
            draft = v.load_draft(db, cfg, scan_id)  # 草稿绑定本次扫描：新扫描/配置变化不继承旧选择
            filt, shown = self._review_filters(qs, groups, draft)
            total = len(shown)
            pages = max(1, -(-total // self.REVIEW_PAGE_SIZE))
            try:
                page = max(1, min(pages, int(self._qp(qs, "page") or "1")))
            except ValueError:
                page = 1
            page_groups = shown[(page - 1) * self.REVIEW_PAGE_SIZE:page * self.REVIEW_PAGE_SIZE]
            # 超大分组不全量加载（§四D/P2）：默认每组只取前 N 个成员；?full=<gid>
            # 的组展开全部。旧库行没有持久化建议时需全组成员现算建议（一次性）。
            wanted, full_param = set(), self._qp(qs, "full")
            for g in page_groups:
                if g.get("suggest") is None:
                    wanted.update(g["members"])
                elif full_param == g["gid"]:
                    wanted.update(g["members"])
                else:
                    wanted.update(g["members"][:self.GROUP_MEMBER_PREVIEW])
            captured = v.load_files(db, cfg, scan_id, wanted)
        if not groups:
            self.page(f"<h1>审阅</h1><div class=card><h3>{ICONS_S['sparkle']} 没有发现重复或相似的视频</h3>"
                      f"<p class=hint>这批文件夹里没有可清理的内容。</p></div>"
                      f"<p><a class='btn ghost' href='{self.px}/'>返回概览</a></p>", active="review")
            return
        banner = ""
        if active is not None and active["type"] == "plan":
            progress = v.read_progress(cfg) or {}
            banner = (f"<div class='card banner'><p class=m0>{ICONS_S['clock']} 清理计划正在后台生成"
                      f"（已核对 {progress.get('done', 0)}/{progress.get('total', 0)} 个文件）；生成期间本页仍可浏览，"
                      f"但请勿重复提交。<a href='{self.px}/plan-status?task={active['id']}'>查看进度</a></p></div>")
        parts = [banner, f"<h1>审阅重复视频</h1><p class=lead>共 {len(groups)} 组疑似重复（当前筛选 {total} 组）。"
                 "点文件卡上的「标记待清理」按钮勾选；<b>点画面进入并排比较</b>（画面不兼任勾选）。"
                 f"<span class='badge b-ref'>{ICONS_S['lock']} 受保护</span>"
                 "的文件（只读文件夹或被多处引用的硬链接）不会进入清理计划。<b>每组至少保留一个</b>；"
                 "保留多份、整组不清理都是正常决定。选择会自动保存为审阅草稿（只属于本次扫描）。</p>"]
        parts.append("<div class='card note'><p class=m0>" + ICONS_S['bulb'] +
                     " 初次进入<b>不预选任何清理项</b>。「内容完全一致」的组提供批量建议，点「应用建议」后才会勾选："
                     "优先保留受保护副本，否则按文件夹路径与文件名的稳定顺序保留第一份——不用文件大小判断质量。"
                     "「画面相似」只是候选线索：语言、音轨、字幕、剪辑版本可能不同，请看预览图确认是同一内容再标记；"
                     "外挂字幕不一致或读取异常的组不做批量建议，会列出原因，需人工判断。</p></div>")
        # --- 筛选表单（§三.3：类型 / 待处理-已处理 / 文件夹 / 名称）---------------
        def option(value, label, current):
            return f"<option value='{value}'{' selected' if value == current else ''}>{label}</option>"
        root_opts = "".join(f"<option value='{r['id']}'{' selected' if r['id'] == filt['root'] else ''}>"
                            f"{html.escape(r['path'])}</option>" for r in cfg["roots"])
        parts.append(
            f"<form method=get action='{self.px}/review' class=card style='padding:14px 18px'>"
            f"<input type=hidden name=scan value='{scan_id}'>"
            "<div class=chain>"
            f"<label>类型 <select name=kind>{''.join([option('exact', '完全相同', filt['kind']), option('visual', '画面相似待比较', filt['kind']), option('all', '全部', filt['kind'])])}</select></label> "
            f"<label>状态 <select name=state>{''.join([option('all', '全部组', filt['state']), option('pending', '待处理', filt['state']), option('done', '已处理', filt['state'])])}</select></label> "
            f"<label>文件夹 <select name=root><option value=''>全部</option>{root_opts}</select></label> "
            f"<label>名称 <input name=q value='{html.escape(filt['q'])}' placeholder='文件名包含…' style='width:150px'></label> "
            "<button class='btn sm'>筛选</button> "
            f"<a class='btn ghost sm' href='{self.px}/review?scan={scan_id}'>重置</a>"
            "</div></form>")

        def suggest_of(g):
            # 建议单一来源：扫描时持久化的 groups.suggest；旧库行 NULL 才现算
            return g["suggest"] if g.get("suggest") is not None else \
                v.suggest_members(cfg, [captured[k] for k in g["members"]], g.get("exacts"))

        def names_of(keys):
            labels = [f"<span title='{html.escape(k)}'>{html.escape(k.split(':', 1)[1].rsplit('/', 1)[-1])}</span>"
                      for k in keys[:3]]
            return "、".join(labels) + (f" 等 {len(keys)} 个" if len(keys) > 3 else "")

        page_sug = {g["gid"]: suggest_of(g) for g in page_groups}
        parts.append("<script>VD_SUGGEST=" +
                     json.dumps({gid: {"clean": s["clean"], "reset": sorted(s["keep"])}
                                 for gid, s in page_sug.items() if s["clean"] or s["keep"]},
                                ensure_ascii=False).replace("</", "<\\/") + ";</script>")
        for g in page_groups:
            gid = g["gid"]
            ent = draft["groups"].get(gid) or {}
            clean = set(ent.get("clean") or [])
            skip = bool(ent.get("skip"))
            expanded = full_param == gid
            keys_shown = g["members"] if expanded else g["members"][:self.GROUP_MEMBER_PREVIEW]
            hidden_n = len(g["members"]) - len(keys_shown)
            members = [captured[k] for k in keys_shown]
            s = page_sug[gid]
            kind_badge = ("<span class='badge b-ok'>内容完全一致</span>" if g["kind"] == "exact"
                          else "<span class='badge b-warn'>画面相似 · 请确认是同一内容</span>")
            head_extra = f"{len(g['members'])} 个文件"
            if not hidden_n:  # 成员全部加载时才给合计大小（截断预览不误导）
                head_extra += f" · 合计 {sum(captured[k]['stat']['size'] for k in g['members']) / 2**20:.0f} MB"
            compare_url = f"{self.px}/compare?scan={scan_id}&gid={html.escape(gid)}"
            group_btns = f"<a class='btn ghost sm' href='{compare_url}'>并排比较</a> "
            if s["clean"]:
                group_btns += (f"<button type=button class='btn ghost sm' onclick='applySuggest(\"{gid}\")'>"
                               "应用本组建议</button>")
            group_btns += ("<button type=button class='btn ghost sm' onclick='clearPicks(this.closest(\".card\"))'>"
                           "清空勾选</button> ")
            group_btns += (f"<button type=button class='btn ghost sm' onclick='toggleSkip(this)'>"
                           f"{'取消跳过' if skip else '跳过本组'}</button>")
            skip_badge = (f"<span class='badge b-warn skip-badge' style='{'display:none' if not skip else ''}'>"
                          "已跳过 · 本组不参与本次清理</span>")
            # §三.3 组内差异确认状态：clean ⊆ confirmed 才算已确认（改动选择需再确认）。
            # 差异提示用已加载成员的元数据零 IO 现算（超大组截断预览时可能不全，
            # post_plan 的提交前校验会按全组兜底）。
            confirmed_ok = bool(clean) and clean <= set(ent.get("confirmed") or [])
            g_notes = v.media_diff_notes([m.get("meta") or {} for m in members])
            if clean and g_notes:
                if confirmed_ok:
                    diff_html = (f"<div class=diffnote>{ICONS_S['check']} 本组存在 {'、'.join(g_notes)} 差异，"
                                 "你已确认过当前待清理选择。<a href='" + compare_url + "'>重新核对</a></div>")
                else:
                    diff_html = (f"<div class=diffnote'>{ICONS_S['bulb']} 本组存在 {'、'.join(g_notes)} 差异："
                                 "提交清理清单前需<a href='" + compare_url + "'>并排比较</a>核对差异，"
                                 "并<button type=button class='btn sm' onclick='confirmDiff(this)'>确认待清理选择</button>"
                                 "（确认前无法提交）。</div>")
            else:
                diff_html = ""
            advice = []
            if s["keep"]:
                keeps = "、".join(
                    f"<b title='{html.escape(k)}'>{html.escape(k.split(':', 1)[1].rsplit('/', 1)[-1])}</b>"
                    f"（{html.escape(reason)}）" for k, reason in s["keep"].items())
                advice.append(f"批量建议——保留：{keeps}。")
            if s["clean"]:
                advice.append(f"建议清理：{names_of(s['clean'])}（与保留副本字节完全一致）。")
            for skipinfo in s["skipped"]:
                advice.append(f"{ICONS_S['bulb']} 不做批量建议：{html.escape(skipinfo['reason'])}"
                              f"（涉及 {names_of(skipinfo['members'])}）。")
            if s["manual"]:
                advice.append(f"其余 {len(s['manual'])} 个画面相似但内容不同的版本不参与批量建议，请逐项比较后手动标记。")
            if not advice:
                advice.append("画面相似候选：分辨率、音轨、字幕或剪辑版本可能不同，请逐项比较后手动标记；本组不做批量建议。")
            parts.append(f"<div class=card data-gid='{html.escape(gid)}' data-skip='{1 if skip else 0}'"
                         f" data-confirmed='{1 if confirmed_ok else 0}'"
                         f" data-skip-reason='{html.escape(str(ent.get('skip_reason') or ''), True)}'>"
                         f"<div class=g-head><h3>{kind_badge} {head_extra}</h3>{skip_badge}<span class=grow></span>"
                         + group_btns +
                         f"</div><div class=hint style='margin:6px 0 0'>{' '.join(advice)}</div>{diff_html}<div class=tiles>")
            for item in members:
                k = v.key(item)
                root = next(r for r in cfg["roots"] if r["id"] == item["root"])
                protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
                picked = k in clean and not skip
                meta = item.get("meta", {})
                distance = ""
                if g["kind"] == "visual" and item.get("vhash") and len(members) > 1:
                    other = next(m for m in members if m is not item and m.get("vhash"))
                    distance = round(sum(v.hamming(x, y) for x, y in zip(item["vhash"], other["vhash"])) / len(v.FRAME_POSITIONS), 1)
                compare_url = f"{self.px}/compare?scan={scan_id}&gid={html.escape(gid)}"
                thumb = (f"<img src='{self.px}/thumb?scan={scan_id}&f={html.escape(k)}' loading=lazy alt='点击进入并排比较'>"
                         if item.get("thumb") else "<div class=thumb-blank></div>")
                # §三.3/P0：默认全部保留（不预选）；状态从服务端草稿恢复（§四D）。
                # 画面/名称是进入比较页的链接；勾选由 pickbtn 显式承担，不兼任。
                badge = (f"<span class='badge b-ref corner' title='只读文件夹或硬链接，不会进入清理计划'>"
                         f"{ICONS_S['lock']} 受保护</span>" if protected
                         else f"<span class='badge corner {'b-err' if picked else 'b-ok'}' data-st>"
                         f"{'待清理' if picked else '保留'}</span>")
                cls = "tile prot" if protected else ("tile sel" if picked else "tile")
                # 中性呈现：算法指纹距离是原始差异度量（汉明距离均值/64），不是相似百分比或质量评分
                sim = (f"<br>画面指纹距离 {distance}/64" if distance != "" else "")
                button = "" if protected else (
                    f"<button type=button class=pickbtn onclick='togglePick(this)' aria-pressed='{'true' if picked else 'false'}'>"
                    f"{'改为保留' if picked else '标记待清理'}</button>")
                parts.append(
                    f"<div class='{cls}'>{badge}"
                    f"<a class=thumblnk href='{compare_url}' title='并排比较本组全部版本'>{thumb}</a>"
                    f"<a class=name style='text-decoration:none;color:inherit' href='{compare_url}' title='{html.escape(item['rel'])}'>{html.escape(Path(item['rel']).name)}</a>"
                    f"<div class=meta>{item['stat']['size'] / 2**20:.1f} MB · {meta.get('duration', 0):.0f} 秒 · "
                    f"{meta.get('width') or '?'}×{meta.get('height') or '?'}{sim}"
                    f"<span class=path title='{html.escape(item['rel'])}'>{ICONS_S['folder']} {html.escape(root['path'] + '/' + str(Path(item['rel']).parent))}</span></div>"
                    f"{button}"
                    f"<input type=checkbox name=sel value='{html.escape(k)}'{' checked' if picked else ''}"
                    f" {'disabled' if protected else ''} style='display:none'></div>")
            parts.append("</div>")
            if hidden_n > 0:
                parts.append(f"<div class=hint style='margin-top:10px'>本组共 {len(g['members'])} 个文件，先显示前 {len(keys_shown)} 个。"
                             f"<a href='{self._review_url(scan_id, page, filt, full=gid)}'>显示全部 {len(g['members'])} 个</a></div>")
            parts.append("</div>")
        # --- 分页导航（服务端分页，§四D/P2）---------------------------------------
        if pages > 1:
            nav = ["<div class=card style='padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap'>"]
            if page > 1:
                nav.append(f"<a class='btn ghost sm' href='{self._review_url(scan_id, page - 1, filt, full=full_param)}'>上一页</a>")
            nav.append(f"<span class=hint>第 {page} / {pages} 页 · 当前筛选 {total} 组 · 每页 {self.REVIEW_PAGE_SIZE} 组</span>")
            if page < pages:
                nav.append(f"<a class='btn ghost sm' href='{self._review_url(scan_id, page + 1, filt, full=full_param)}'>下一页</a>")
            nav.append("</div>")
            parts.append("".join(nav))
        parts.append("<div class=sticky>"
                     f"<form method=post id=plan-form action='{self.px}/plan'>"
                     f"<input type=hidden name=scan value='{scan_id}'>"
                     "<button class='btn big' id=plan-btn>下一步：生成清理计划（先预演，不会删除）</button></form> "
                     "<button type=button class='btn ghost' onclick='applySuggest()'>应用建议</button> "
                     "<button type=button class='btn ghost' onclick='clearPicks()'>全部取消勾选</button> "
                     f"<span id=draft-st class='badge b-ok'>草稿已保存（修订 {draft['rev']}）</span>"
                     "<span class=hint>选择自动保存为本次扫描的草稿；<b>草稿未保存或保存失败时无法提交</b>。"
                     "已生成的计划不受后续草稿修改影响；改了选择就重新生成计划。</span></div>")
        # 草稿自动保存（§四D）：变更防抖保存；失败保留页面选择并禁提交；两个标签页
        # 修订冲突提示刷新重新加载，不静默覆盖。
        parts.append("<script>\n"
                     f"var VD_SCAN='{scan_id}',VD_REV={draft['rev']},VD_CSRF='{_CSRF_TOKEN}';\n"
                     "var vdDirty=false,vdTimer=null,vdErr=null;\n"
                     """function vdPaint(msg){
  var b=document.getElementById('draft-st'),btn=document.getElementById('plan-btn');
  if(!b)return;
  if(vdErr==='conflict'){b.className='badge b-err';b.textContent='草稿冲突：另一标签页已保存更新——请刷新页面重新加载；本页选择未提交';}
  else if(vdErr){b.className='badge b-err';b.textContent='草稿保存失败：'+(msg||'网络错误')+'；本页选择已保留，恢复保存前无法提交';}
  else if(vdDirty){b.className='badge b-warn';b.textContent='正在保存草稿…';}
  else{b.className='badge b-ok';b.textContent='草稿已保存（修订 '+VD_REV+'）';}
  if(btn)btn.disabled=!!(vdDirty||vdErr);
}
function vdCollect(){
  var out={};
  document.querySelectorAll('.card[data-gid]').forEach(function(card){
    var clean=[],confirmed=[];
    card.querySelectorAll('.tile input[type=checkbox]').forEach(function(cb){
      if(cb.checked){clean.push(cb.value);}
    });
    // §三.3：confirmed 只在用户显式确认差异（confirmDiff/比较页按钮）后记录，
    // 不随勾选自动生成——存在差异的组必须单独确认才能提交。
    if(card.dataset.confirmed==='1'){confirmed=clean.slice();}
    out[card.dataset.gid]={clean:clean,confirmed:confirmed,skip:card.dataset.skip==='1',
      skip_reason:card.dataset.skipReason||''};
  });
  return out;
}
function vdSave(then){
  var body=new URLSearchParams({csrf:VD_CSRF,scan:VD_SCAN,rev:String(VD_REV),draft:JSON.stringify(vdCollect())});
  fetch('""" + self.px + """/draft',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body.toString()})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(res){
      if(res.ok){VD_REV=res.j.rev;vdDirty=false;vdErr=null;vdPaint();if(then)then();return;}
      vdErr=res.j.conflict?'conflict':'error';vdPaint(res.j.error);
    })
    .catch(function(){vdErr='network';vdPaint();});
}
function vdMarkDirty(){
  if(!VD_SCAN||vdErr==='conflict')return;  // 冲突后必须刷新重载，不再自动保存
  vdDirty=true;vdErr=null;vdPaint();
  if(vdTimer)clearTimeout(vdTimer);
  vdTimer=setTimeout(function(){vdSave();},800);
}
function toggleSkip(btn){
  var card=btn.closest('.card');
  var on=card.dataset.skip!=='1';
  if(on){
    // §三.3：不支持判断的组允许跳过，但提交清理清单前必须说明原因——跳过时
    // 就地收集（可留空，留空的组在提交时被服务端拒绝并点名）。
    var reason=prompt('说明跳过本组的原因（提交清理清单前需要说明，例如：无法判断是否同一内容）：',
                      card.dataset.skipReason||'');
    if(reason===null)return;          // 取消 = 不跳过
    card.dataset.skipReason=reason.trim();
  }
  card.dataset.skip=on?'1':'0';
  if(on)clearPicks(card);  // 跳过 = 整组不清理，勾选清空
  btn.textContent=on?'取消跳过':'跳过本组';
  var b=card.querySelector('.skip-badge');
  if(b)b.style.display=on?'':'none';
  vdMarkDirty();
}
(function(){
  var f=document.getElementById('plan-form');
  if(f)f.addEventListener('submit',function(e){  // 保存失败禁止提交（§四D）
    if(vdDirty||vdErr){e.preventDefault();if(!vdErr)vdSave();}
  });
  document.addEventListener('click',function(e){  // 未保存先保存再离开（翻页/筛选不丢选择）
    var a=e.target.closest?e.target.closest('a[href]'):null;
    if(a&&VD_SCAN&&vdDirty&&!vdErr){e.preventDefault();vdSave(function(){location.href=a.href;});}
  });
})();
vdPaint();
</script>""")
        self.page("".join(parts), active="review")

    def get_progress(self):
        try:
            self.json_out(v.read_progress(self.cfg()) or {"phase": "idle"})
        except v.Unsafe:
            self.json_out({"phase": "idle"})

    def get_browse(self, qs):
        root_id = (qs.get("root") or [""])[0]
        rel = (qs.get("rel") or [""])[0]
        try:
            self.json_out(v.browse_dirs(self.cfg(), root_id, rel))
        except v.Unsafe as e:
            self.note_exception(e)
            self.json_out({"error": str(e)}, status=400)
        except OSError as e:
            self.note_exception(e)
            self.json_out({"error": str(e)}, status=404)

    def get_thumb(self, qs):
        # Review 26.10.12 §四E：缩略图只按 (scan, key) 查应用私有库定位，不接受
        # 任意路径；记录必须归属该扫描，且其根仍在当前授权目录内；文件名收口为
        # 纯文件名（拒绝任何路径成分），落点固定在 state_dir/thumbs 下。
        scan_id = (qs.get("scan") or [""])[0]
        ident = (qs.get("f") or [""])[0]
        if not scan_id or not ident:
            self.page("<p>参数不完整</p>", status=404)
            return
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            row = db.execute("SELECT record FROM files WHERE scan=? AND key=?", (scan_id, ident)).fetchone()
        rec = json.loads(row[0]) if row else None
        if not rec or rec.get("root") not in {r["id"] for r in cfg["roots"]}:
            self.page("<p>无缩略图</p>", status=404)
            return
        name = rec.get("thumb") or ""
        if Path(name).name != name:
            self.page("<p>无缩略图</p>", status=404)
            return
        try:
            data = (Path(cfg["state_dir"]) / "thumbs" / name).read_bytes()
        except OSError:
            data = b""
        if not data:
            self.page("<p>无缩略图</p>", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not getattr(self, "_head", False):
            self._safe_write(data)

    def get_preview(self, qs):
        """比较页五帧图片（Review 26.10.12 §三.3/§四C）：按 (scan, key, pos)
        定位扫描记录后按需生成（preview_frames 私有缓存于 state_dir/previews）。
        与缩略图同一读取边界：不接受任意路径；扫描后文件被替换（stat 不符）
        即拒绝——不把别的视频画面冒充扫描时的内容。"""
        scan_id = (qs.get("scan") or [""])[0]
        ident = (qs.get("f") or [""])[0]
        try:
            pos = float((qs.get("p") or [""])[0])
        except ValueError:
            pos = -1.0
        if not scan_id or not ident or pos not in v.FRAME_POSITIONS:
            self.page("<p>参数不完整</p>", status=404)
            return
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            row = db.execute("SELECT record FROM files WHERE scan=? AND key=?", (scan_id, ident)).fetchone()
        rec = json.loads(row[0]) if row else None
        meta = rec.get("meta") or {} if rec else {}
        if not rec or rec.get("root") not in {r["id"] for r in cfg["roots"]} or meta.get("error") or not meta.get("duration"):
            self.page("<p>无预览帧</p>", status=404)
            return
        try:
            if v.snapshot(v.file_path(cfg, rec).lstat()) != rec.get("stat"):
                self.page("<p>文件在扫描后已变化，请重新扫描</p>", status=404)
                return
        except (OSError, v.Unsafe):
            self.page("<p>无预览帧</p>", status=404)
            return
        try:
            with _PREVIEW_LOCK:
                frames = v.preview_frames(cfg, rec, meta["duration"])
        except (v.Unsafe, OSError, ValueError, subprocess.SubprocessError):
            frames = []
        frame = next((f for f in frames if f.get("pos") == pos and f.get("img")), None)
        if not frame:
            self.page("<p>此帧画面生成失败</p>", status=404)
            return
        try:
            data = (Path(cfg["state_dir"]) / "previews" / frame["img"]).read_bytes()
        except OSError:
            data = b""
        if not data:
            self.page("<p>此帧画面生成失败</p>", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not getattr(self, "_head", False):
            self._safe_write(data)

    # --- compare（§三.3 相似组比较界面）--------------------------------------
    COMPARE_MAX_MEMBERS = 12  # 并排上限；超大组回审阅页 ?full= 逐一查看

    def get_compare(self, qs):
        scan_id = self._qp(qs, "scan")
        gid = self._qp(qs, "gid")
        if not scan_id or not gid:
            self.page("<h1>参数不完整</h1><p class=hint>请从审阅页点击画面进入比较。</p>", status=404, active="review")
            return
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            groups = v.load_groups(db, scan_id)
            group = next((g for g in groups if g["gid"] == gid), None)
            draft = v.load_draft(db, cfg, scan_id)
            truncated = len(group["members"]) > self.COMPARE_MAX_MEMBERS if group else False
            keys = group["members"][:self.COMPARE_MAX_MEMBERS] if group else []
            captured = v.load_files(db, cfg, scan_id, set(keys)) if keys else {}
        if group is None:
            self.page("<h1>分组不存在</h1><p class=hint>该组不在本次扫描结果中；请回审阅页重新进入。</p>", status=404, active="review")
            return
        members = [captured[k] for k in keys]
        cmp_data = v.compare_rows(cfg, members)
        ent = draft["groups"].get(gid) or {}
        clean = sorted(ent.get("clean") or [])
        confirmed = set(ent.get("confirmed") or [])
        notes = cmp_data["notes"]
        kind_badge = ("<span class='badge b-ok'>内容完全一致</span>" if group["kind"] == "exact"
                      else "<span class='badge b-warn'>画面相似 · 请确认是同一内容</span>")
        parts = [f"<h1>并排比较</h1><p class=lead>{kind_badge} 本组 {len(group['members'])} 个文件——"
                 "这里逐项查看差异，<b>不做勾选</b>；标记待清理请回审阅页。</p>"]
        # 差异提示 + 组内确认（§三.3：存在差异时在该组内明确确认）
        if notes:
            confirmed_now = bool(clean) and set(clean) <= confirmed
            badge = ("<span class='badge b-ok'>差异已确认</span>" if confirmed_now
                     else "<span class='badge b-warn'>差异未确认</span>")
            detail = " 本组当前没有待清理项。" if not clean else (
                f" 当前待清理 {len(clean)} 项" + ("。" if confirmed_now else "——确认后才可提交清理清单。") + badge)
            btn = "" if (confirmed_now or not clean) else \
                " <button type=button class='btn sm' onclick='vdConfirmCompare(this)'>我已核对差异，确认待清理选择</button>"
            parts.append(f"<div class='card' data-gid='{html.escape(gid)}' data-confirmed='{1 if confirmed_now else 0}'>"
                         f"<h3>本组存在差异：{'、'.join(notes)}</h3>"
                         "<p class=hint>下方高亮行是各版本不一致的地方，请逐帧、逐项核对后再决定清理；"
                         f"差异只是事实呈现，不代表哪个版本更好。{detail}{btn}</p></div>")
        else:
            parts.append("<div class=card><h3>本组各版本参数一致</h3>"
                         "<p class=hint>分辨率、时长、编码、帧率、大小、音轨、字幕均相同。</p></div>")
        # 五帧同步切换（§三.3：10%~90% 五个相对位置 + 各自实际时间）
        parts.append("<div class=card><h3>画面帧对比</h3>"
                     "<p class=hint>同一相对时间位置并排取样；每个视频时长不同，标注的是各自的"
                     "<b>实际时间</b>。首次查看时逐个生成画面，稍等片刻。</p><div class=frametabs>")
        tabs = [f"<button type=button class='ftab{' on' if pos == 0.5 else ''}' data-pos='{pos}'"
                f" onclick='vdFrameTab(this)'>{int(pos * 100)}%</button>" for pos in v.FRAME_POSITIONS]
        parts.append("".join(tabs) + "</div>")
        for pos in v.FRAME_POSITIONS:
            cells = []
            for item in members:
                k = v.key(item)
                meta = item.get("meta") or {}
                who = html.escape(Path(item["rel"]).name)
                if meta.get("error") or not meta.get("duration"):
                    cells.append(f"<div class=framecell><div class=who title='{html.escape(item['rel'])}'>{who}</div>"
                                 "<div class=framefail>元数据读取失败，无法定位帧位置</div></div>")
                    continue
                t = v.fmt_dur(meta["duration"] * pos)
                cells.append(f"<div class=framecell><div class=who title='{html.escape(item['rel'])}'>{who}</div>"
                             f"<img src='{self.px}/preview?scan={scan_id}&f={html.escape(k)}&p={pos}' loading=lazy "
                             f"alt='{who} 在 {t} 的画面' width=640 height=360>"
                             f"<div class=t>{int(pos * 100)}% 位置 · 实际时间 {t}</div></div>")
            cls = "framerow on" if pos == 0.5 else "framerow"
            parts.append(f"<div class='{cls}' data-row='{pos}'>" + "".join(cells) + "</div>")
        parts.append("</div>")
        # 并排参数表：差异行高亮（rd）
        parts.append("<div class=card><h3>参数对比</h3><div class=cmp-wrap><table class=cmp><tr><th></th>")
        roots = {r["id"]: r for r in cfg["roots"]}
        for item in members:
            root = roots.get(item["root"]) or {"path": "已移除的目录 " + item["root"]}
            folder = str(Path(root["path"]) / Path(item["rel"]).parent)
            parts.append(f"<th>{html.escape(Path(item['rel']).name)}<span class=dir title='{html.escape(folder)}'>"
                         f"{ICONS_S['folder']} {html.escape(folder)}</span></th>")
        parts.append("</tr>")
        for row in cmp_data["rows"]:
            trcls = " class=rd" if row["diff"] else ""
            parts.append(f"<tr{trcls}><th>{row['label']}{' <span class=rd-flag>差异</span>' if row['diff'] else ''}</th>")
            for cell in row["cells"]:
                parts.append(f"<td class='{'rdcell' if row['diff'] else ''}'>{html.escape(cell)}</td>")
            parts.append("</tr>")
        parts.append("</table></div>"
                     "<p class=hint>高亮行表示各版本在该项上不一致。本页不提供质量评分或相似百分比——"
                     "分辨率、码率、算法指纹距离都不能代表哪个版本更好。</p></div>")
        if truncated:
            parts.append(f"<p class=hint>本组共 {len(group['members'])} 个文件，比较视图最多并排 "
                         f"{self.COMPARE_MAX_MEMBERS} 个；其余成员请回审阅页展开查看。</p>")
        parts.append(f"<p><a class='btn big' href='{self.px}/review?scan={scan_id}'>返回审阅（勾选与已确认的差异都保留）</a> "
                     f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>")
        # 比较页脚本：帧 tab 切换 + 差异确认写回草稿（与审阅页同一修订协议）
        parts.append("<script>\n"
                     f"var VD_SCAN='{scan_id}',VD_REV={draft['rev']},VD_CSRF='{_CSRF_TOKEN}';\n"
                     "function vdFrameTab(btn){\n"
                     "  document.querySelectorAll('.frametabs .ftab').forEach(b=>b.classList.toggle('on',b===btn));\n"
                     "  document.querySelectorAll('.framerow').forEach(r=>r.classList.toggle('on',r.dataset.row===btn.dataset.pos));\n"
                     "}\n"
                     "function vdConfirmCompare(btn){\n"
                     f"  var clean={json.dumps(clean, ensure_ascii=False).replace('</', '<\\\\/')};\n"
                     "  if(!clean.length)return;\n"
                     "  var gid=btn.closest('[data-gid]').dataset.gid;\n"
                     "  var ent={};ent[gid]={clean:clean,confirmed:clean,skip:false};\n"
                     "  var body=new URLSearchParams({csrf:VD_CSRF,scan:VD_SCAN,rev:String(VD_REV),draft:JSON.stringify(ent)});\n"
                     "  fetch('" + self.px + "/draft',{method:'POST',"
                     "headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body.toString()})\n"
                     "    .then(r=>r.json().then(j=>({ok:r.ok,j:j})))\n"
                     "    .then(res=>{\n"
                     "      if(res.ok){VD_REV=res.j.rev;btn.textContent='已确认差异，可提交';"
                     "btn.closest('[data-gid]').dataset.confirmed='1';return;}\n"
                     "      alert('保存失败：'+(res.j&&res.j.error||'请刷新页面后重试'));\n"
                     "    })\n"
                     "    .catch(()=>alert('网络错误，未能保存确认'));\n"
                     "}\n"
                     "</script>")
        self.page("".join(parts), active="review")

    _FONTS = {"latin": "outfit-latin-wght.woff2", "latin-ext": "outfit-latin-ext-wght.woff2"}

    def get_font(self, qs):
        """自托管 Outfit 字体（SIL OFL，随包分发）。缺文件时 404，页面回退系统字体。"""
        name = self._FONTS.get((qs.get("f") or [""])[0])
        if not name:
            self.page("<p>无此字体</p>", status=404)
            return
        here = Path(__file__).resolve().parent
        for cand in (here / "fonts" / name, here / "fpk" / "app" / "fonts" / name):
            if cand.is_file():
                data = cand.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.end_headers()
                if not getattr(self, "_head", False):
                    self._safe_write(data)
                return
        self.page("<p>字体文件缺失</p>", status=404)

    def get_static(self, name):
        """随包静态资源（§四E）：CSS/JS 抽出为本地文件经 /static/ 提供。文件名
        白名单精确匹配（无路径拼接，天然拒绝 ../ 与任意路径）；开发布局与安装
        布局同为 webui.py 同级 static/。缺文件 404，页面功能降级不崩。"""
        ctype = _STATIC_TYPES.get(name)
        if not ctype:
            self.page("<p>无此静态资源</p>", status=404)
            return
        path = Path(__file__).resolve().parent / "static" / name
        try:
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b""
        if not data:
            self.page("<p>静态资源缺失</p>", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 页面同策略：无版本化文件名，升级后不得残留旧脚本/样式
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head", False):
            self._safe_write(data)

    # --- plan / apply ---------------------------------------------------------
    def post_draft(self, qs):
        """审阅草稿保存（Review 26.10.12 §四D）：JSON 应答供页面 fetch。修订冲突
        409 并携带当前修订号——页面提示刷新重新加载，不静默覆盖；保存失败时页面
        选择原样保留、提交按钮保持禁用，直到成功保存。"""
        cfg = self.cfg()
        scan_id = (qs.get("scan") or [""])[0]
        try:
            expect_rev = int((qs.get("rev") or [""])[0])
        except ValueError:
            self.json_out({"error": "rev 参数无效"}, status=400)
            return
        try:
            entries = json.loads((qs.get("draft") or [""])[0])
        except ValueError:
            self.json_out({"error": "draft 不是有效的 JSON"}, status=400)
            return
        try:
            with v.state(cfg, exclusive=False) as db:
                new_rev = v.save_draft(db, cfg, scan_id, entries, expect_rev)
        except v.DraftConflict as e:
            self.json_out({"error": str(e), "conflict": True, "rev": e.current_rev}, status=409)
            return
        except v.Unsafe as e:
            self.json_out({"error": str(e)}, status=400)
            return
        self.json_out({"rev": new_rev})

    def post_plan(self, qs):
        scan_id = (qs.get("scan") or [""])[0]
        if not scan_id:
            self.page(self.error_body("缺少参数", "请求缺少 scan 参数；请从审阅页重新操作。"), status=400, active="home")
            return
        cfg = self.cfg()
        # §四B：参数验证完成后原子占用任务；重复提交/其他任务运行中 → 明确忙碌。
        # 忙碌判断先于草稿读取（与既有 503 契约一致）；草稿问题在占用后如实记为
        # 失败任务行，不进后台线程。
        try:
            task = tasks.claim(cfg, "plan", scan_id=scan_id)
        except tasks.Busy as busy:
            self.busy_page(busy)
            return
        # §四D：计划输入只来自服务端草稿（表单勾选不再直达计划——保存失败或
        # 未保存的页面选择绝无机会进入计划）；草稿不可读或无待清理选择时拒绝。
        try:
            with v.state(cfg, exclusive=False) as db:
                draft = v.load_draft(db, cfg, scan_id)
        except v.Unsafe as e:
            tasks.finish(cfg, tasks.FAILED, error=str(e)[:500], task_id=task["id"])
            self.page(self.error_body("无法读取审阅草稿", str(e) + "；请回到审阅页重新操作。"), status=400, active="home")
            return
        selections = sorted({k for ent in draft["groups"].values()
                             if not ent.get("skip") for k in (ent.get("clean") or [])})
        if not selections:
            tasks.finish(cfg, tasks.FAILED, error="草稿中没有待清理选择", task_id=task["id"])
            self.page(self.error_body("草稿中没有勾选任何文件",
                                      "请先在审阅页标记要清理的文件，等状态条显示「草稿已保存」后再生成计划。"),
                      status=400, active="home")
            return
        # §三.3 提交前校验（同步、用扫描记录快速核对；v.plan 逐文件复核仍是权威）：
        # 1) 每个待清理文件必须有仍被保留的直接匹配对象（pair_match 与扫描同口径，
        #    连通组 A≈B≈C 不代表 A≈C）；2) 组内存在差异时必须已明确确认；
        # 3) 跳过的组必须已说明原因。不满足即拒绝，提前给出可处理的修正指引。
        problems = [gid for gid, ent in draft["groups"].items()
                    if ent.get("skip") and not (ent.get("skip_reason") or "").strip()]
        problems = ["跳过的组需要先说明原因（审阅页「跳过本组」时可填写）: " + "、".join(problems[:5])] if problems else []
        try:
            with v.state(cfg, exclusive=False) as db:
                groups = v.load_groups(db, scan_id)
                touched = [g for g in groups if (draft["groups"].get(g["gid"]) or {}).get("clean")]
                wanted = {k for g in touched for k in g["members"]}
                captured = v.load_files(db, cfg, scan_id, wanted) if wanted else {}
            for g in touched:
                ent = draft["groups"][g["gid"]]
                clean = set(ent.get("clean") or [])
                members = [captured[k] for k in g["members"]]
                keepers = [m for m in members if v.key(m) not in clean]
                for k in sorted(clean):
                    if not any(v.pair_match(captured[k], kept) for kept in keepers):
                        problems.append(f"{g['gid']} 的 {k}: 没有仍被保留的直接匹配对象"
                                        "（画面相似组按链式归组，不保证两两相似），请把它改为保留或另选保留对象")
                if v.compare_rows(cfg, members)["notes"] and not clean <= set(ent.get("confirmed") or []):
                    problems.append(f"{g['gid']}: 组内存在音轨/字幕/时长差异，请先在该组的并排比较页核对并确认待清理选择")
        except (v.Unsafe, OSError, ValueError, KeyError) as e:
            # 任务已占用：校验途中失败必须终结任务行，不能留下悬挂的 running
            tasks.finish(cfg, tasks.FAILED, error=str(e)[:500], task_id=task["id"])
            self.page(self.error_body("提交前校验无法完成", str(e)), status=400, active="review")
            return
        if problems:
            detail = "<br>".join(html.escape(p) for p in problems[:8])
            if len(problems) > 8:
                detail += f"<br>…等共 {len(problems)} 项"
            tasks.finish(cfg, tasks.FAILED, error="；".join(problems)[:500], task_id=task["id"])
            self.page(self.error_body("清理清单未通过提交前校验", detail), status=400, active="review")
            return
        threading.Thread(target=plan_worker, args=(cfg, scan_id, selections), daemon=True).start()
        # §四B（Review 26.10.12 P1）：原先此处返回 200 页面并在 3 秒后
        # location.reload()——刷新会重提 POST、状态跳转不稳定；统一 303 到
        # 绑定任务的计划状态页，进度轮询由该 GET 页承担。
        self.redirect("/plan-status?task=" + task["id"])

    def persisted_plan(self, cfg):
        """应用重启后从 progress.json 恢复已生成的计划。"""
        try:
            progress = v.read_progress(cfg) or {}
            if progress.get("phase") == "plan_ready" and progress.get("plan_id"):
                return progress
        except (OSError, ValueError):
            pass
        return None

    def _plan_listing_html(self, cfg, plan_id):
        """确认页的保留/清理清单（Review 26.10.12 P1/§三.4）：从已签名的计划
        文档直接读回每组 delete/keep，逐组列出文件、所在文件夹、直接匹配依据
        与差异注意点（音轨/字幕/时长）。文档不可读时如实说明，不伪造清单。"""
        try:
            doc = json.loads((Path(cfg["state_dir"]) / f"{plan_id}.plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ("<div class=card><h3>计划文档不可读</h3>"
                    "<p class=hint>签名的计划文件无法读取，分组清单展示不了；上方的数量与大小来自任务记录。"
                    "如需完整核对，请回审阅页重新生成计划。</p></div>")
        roots = {r["id"]: r for r in cfg["roots"]}

        def where(m):
            root = roots.get(m.get("root")) or {"path": "已移除的目录 " + str(m.get("root"))}
            return html.escape(str(Path(root["path"]) / Path(m.get("rel", "")).parent))

        out = []
        for i, g in enumerate(doc.get("groups", []), 1):
            dels, keeps = g.get("delete", []), g.get("keep", [])
            metas = [m.get("metadata") or {} for m in dels + keeps]
            notes = v.media_diff_notes(metas)
            kind_badge = ("<span class='badge b-ok'>内容完全一致</span>" if g.get("kind") == "exact"
                          else "<span class='badge b-warn'>画面相似</span>")
            note_html = (f"<div class=diffnote>{ICONS_S['bulb']} 注意：本组各版本存在 "
                         f"<b>{ '、'.join(notes) }</b>差异，清理前请再核对一遍。"
                         f"<a href='{self.px}/compare?scan={doc.get('scan_id', '')}&gid={html.escape(g.get('group', ''))}'>并排比较</a></div>"
                         if notes else "")
            rows = []
            for m in dels:
                match = m.get("match") or {}
                if match.get("basis") == "sha256":
                    basis = "与保留副本字节一致"
                elif match.get("basis") == "phash":
                    basis = f"画面指纹直接匹配（距离 {match.get('distance')}）"
                else:
                    basis = "—"
                keep_name = Path((match.get("keep") or ":").split(":", 1)[1]).name if match.get("keep") else "—"
                rows.append(f"<tr><td><span class='badge b-err'>清理</span></td>"
                            f"<td><b>{html.escape(Path(m.get('rel', '')).name)}</b>"
                            f"<span class=dir title='{html.escape(m.get('rel', ''))}'>{html.escape(m.get('rel', ''))}</span></td>"
                            f"<td>{where(m)}</td><td>{m.get('stat', {}).get('size', 0) / 2**20:.1f} MB</td>"
                            f"<td>{basis}（保留：{html.escape(keep_name)}）</td></tr>")
            for m in keeps:
                rows.append(f"<tr><td><span class='badge b-ok'>保留</span></td>"
                            f"<td>{html.escape(Path(m.get('rel', '')).name)}"
                            f"<span class=dir title='{html.escape(m.get('rel', ''))}'>{html.escape(m.get('rel', ''))}</span></td>"
                            f"<td>{where(m)}</td><td>{m.get('stat', {}).get('size', 0) / 2**20:.1f} MB</td><td>—</td></tr>")
            out.append(f"<div class=card><h3>第 {i} 组 · {kind_badge} 清理 {len(dels)} 个 / 保留 {len(keeps)} 个</h3>"
                       + note_html +
                       "<div class=cmp-wrap><table><tr><th>处理</th><th>文件</th><th>所在文件夹</th><th>大小</th><th>匹配依据</th></tr>"
                       + "".join(rows) + "</table></div></div>")
        return "".join(out) or "<div class=card><p class=hint>计划里没有分组数据。</p></div>"

    def get_plan_review(self, qs=None):
        qs = qs or {}
        mode, task = self._bound_task(qs, "plan")
        if mode == "missing":
            self._task_not_found()
            return
        cfg = self.cfg_or_none()
        if task and task["status"] in tasks.ACTIVE:
            self.page("<h1>清理计划正在生成中</h1><p>完成后本页会自动可用；"
                      f"<a class=btn href='{self.px}/plan-status?task={task['id']}'>查看进度</a></p>", status=503, active="home")
            return
        if task and task["status"] == tasks.INTERRUPTED:
            self.page("<h1>上次计划生成被中断</h1><p>应用重启时计划没有生成完成，需要回到审阅页重新勾选并生成"
                      "（扫描结果和指纹缓存不受影响）。</p>"
                      f"<p><a class='btn big' href='{self.px}/review'>返回审阅重新生成</a> "
                      f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", status=503, active="review")
            return
        if task and task["status"] == tasks.FAILED:
            self.page(self.error_body("生成清理计划失败", task.get("error") or "未知错误") +
                      f"<p><a class='btn ghost' href='{self.px}/review?scan={task.get('scan_id') or ''}'>返回审阅</a></p>", active="home")
            return
        result = self.task_result(task) if task and task["status"] == tasks.SUCCESS else None
        persisted = self.persisted_plan(cfg) if cfg and not result else None
        if not result and not persisted:
            phase = ((v.read_progress(cfg) or {}) if cfg else {}).get("phase")
            if phase == "planning":  # CLI 崩溃等无任务记录的残留：如实按中断呈现
                self.page("<h1>上次计划生成被中断</h1><p>计划生成没有完成，需要回到审阅页重新勾选并生成"
                          "（扫描结果和指纹缓存不受影响）。</p>"
                          f"<p><a class='btn big' href='{self.px}/review'>返回审阅重新生成</a> "
                          f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", status=503, active="review")
            else:
                self.page("<h1>当前没有待确认的清理计划</h1><p class=hint>到「审阅」页勾选要删除的文件，点「生成清理计划」。</p>"
                          f"<p><a class='btn big' href='{self.px}/review'>去审阅</a> "
                          f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", active="review")
            return
        # 任务行是跨重启的事实来源（§四B）；预演明细仅本进程展示，重启后不重放。
        if result:
            plan_id = result.get("plan_id", "")
            delete_files = result.get("delete_files", 0)
            logical_bytes = result.get("logical_bytes", 0)
            preview = _PLAN_PREVIEW.get(plan_id, {})
            scan_id = task.get("scan_id") or ""
        else:
            plan_id = persisted["plan_id"]
            delete_files = persisted.get("delete_files", 0)
            logical_bytes = persisted.get("logical_bytes", 0)
            preview = {}  # 旧版 progress 恢复：预演明细不再重放，执行时会做完整校验
            scan_id = ""
        mb = logical_bytes / 2**20
        # §三.4/P1 确认页清单：保留/清理文件与所在文件夹、差异注意点直接展示，
        # 不再只给数量与技术预演；用户在最重要的确认环节能看到具体决定。
        listing = self._plan_listing_html(cfg, plan_id) if cfg else ""
        # §四A/§五：执行入口在回收站接入通过真机验证前保持禁用；清单展示照常。
        if CLEANUP_VERIFIED:
            action_html = ("<p><form method=post action='" + self.px + "/apply' data-confirm='再次确认：永久删除这些文件，不可恢复？'>"
                           f"<input type=hidden name=plan value='{plan_id}'>"
                           f"<input type=hidden name=confirm value='{plan_id}'>"
                           "<button class='btn danger big'>确认执行删除</button></form> ")
        else:
            action_html = (f"<div class=card><h3>{ICONS_S['clock']} {CLEANUP_DISABLED_TITLE}</h3>"
                           f"<ul class=plain>{CLEANUP_DISABLED_REASON}</ul>"
                           "<p class=hint>清单已生成并核验完毕；平台验证通过后这里会提供执行入口。</p></div>")
        self.page(f"<h1>清理计划已生成（尚未删除）</h1>"
                  f"<div class=card><h3>本次计划涉及 <b class=danger-text>{delete_files}</b> 个文件，"
                  f"合计约 {mb:.0f} MB</h3>"
                  "<p class=hint>按现有执行路径，删除是<strong>永久性的</strong>、不进回收站；正因如此，"
                  "在系统回收站接入通过真机验证之前，执行入口保持关闭。</p></div>"
                  "<h2>保留与清理清单</h2>"
                  + listing +
                  f"<details><summary>查看预演结果（技术信息）</summary><pre>{html.escape(json.dumps(preview, ensure_ascii=False, indent=2))}</pre></details>"
                  + action_html +
                  "<p class=hint>已生成的计划文档不再变化；回到审阅修改草稿后需重新生成计划（§四D）。</p>"
                  f"<p><a class='btn big' href='{self.px}/review?scan={scan_id}'>返回审阅修改（全部选择保留）</a> "
                  f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", active="home")

    def get_plan_status(self, qs=None):
        qs = qs or {}
        mode, task = self._bound_task(qs, "plan")
        if mode == "missing":
            self._task_not_found()
            return
        progress = v.read_progress(self.cfg_or_none()) or {}
        result = self.task_result(task) if task and task["status"] == tasks.SUCCESS else None
        out = {"running": bool(task and task["status"] in tasks.ACTIVE),
               "plan_id": (result or {}).get("plan_id") or (task or {}).get("plan_id"),
               "error": (task or {}).get("error"),
               "phase": progress.get("phase"), "done": progress.get("done", 0), "total": progress.get("total", 0)}
        # 给前端轮询的是 JSON；人用浏览器直接打开时显示状态页
        if "application/json" not in self.headers.get("Accept", ""):
            phase = out["phase"]
            cfg = self.cfg_or_none()
            persisted = self.persisted_plan(cfg) if cfg else None
            # §四B：轮询与完成后的前进都绑定同一任务 ID，不再回落「最近一条」
            task_q = "?task=" + task["id"] if task else ""
            if out["running"]:
                done, total = out["done"], out["total"]
                init_pct = min(99, int(done * 100 / total)) if total else 0
                status_html = ("<div class=card id=planbox><h3><span class='status-dot d-run'></span>"
                               "正在生成清理计划</h3>"
                               f"<div class=bar><div id=pl-bar style='width:{init_pct}%'></div></div>"
                               f"<p class=hint>已核对 <span id=pl-done>{done}</span> / <span id=pl-total>{total}</span> 个文件。"
                               "完成后本页自动跳到确认页。</p></div>"
                               """<script>
vdPoll('""" + self.px + """/plan-status""" + task_q + """',st=>{
  const done=st.done||0,total=st.total||0;
  document.getElementById('pl-done').textContent=done.toLocaleString();
  document.getElementById('pl-total').textContent=total.toLocaleString();
  document.getElementById('pl-bar').style.width=(total?Math.min(99,Math.round(done*100/total)):0)+'%';
},st=>{
  if(st.error){location.reload();return true;}
  if(!st.running){
    if(st.plan_id){location.href='""" + self.px + """/plan-review""" + task_q + """';}else{location.reload();}
    return true;
  }
  return false;
});
</script>""")
            elif (task and task["status"] == tasks.INTERRUPTED) or (phase == "planning" and not out["running"]):
                status_html = ("<div class=card><h3><span class='status-dot d-warn'></span>上次生成被中断</h3>"
                               "<p>应用重启时计划生成没有完成（指纹缓存都在，重新生成会很快跳过已核对的部分语义，需重新核对）。</p></div>"
                               f"<p><a class='btn big' href='{self.px}/review'>返回审阅重新生成</a></p>")
            elif result or phase == "plan_ready" or persisted:
                status_html = ("<div class=card><h3><span class='status-dot d-ok'></span>计划已生成，等待确认</h3>"
                               "<p>核对已完成。请查看计划内容，确认后再执行。</p></div>"
                               f"<p><a class='btn big' href='{self.px}/plan-review{task_q}'>查看清理清单</a> "
                               f"<a class='btn ghost' href='{self.px}/review'>返回审阅重新勾选</a></p>")
            elif (task and task["status"] == tasks.FAILED) or phase == "plan_error" or out.get("error"):
                err = (task or {}).get("error") or (persisted or {}).get("error") or ""
                status_html = ("<div class=card><h3><span class='status-dot d-err'></span>生成失败</h3>"
                               f"<pre>{html.escape(str(err))}</pre></div>"
                               f"<p><a class='btn' href='{self.px}/review'>返回审阅重新生成</a></p>")
            else:
                status_html = ("<div class=card><h3><span class='status-dot d-idle'></span>当前没有进行中的任务</h3>"
                               "<p class=hint>请到「审阅」页勾选要删除的文件，然后点「生成清理计划」。</p></div>"
                               f"<p><a class='btn big' href='{self.px}/review'>去审阅</a> "
                               f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>")
            self.page("<h1>清理计划生成状态</h1>" + status_html, active="home")
            return
        self.json_out(out)

    def get_apply(self, qs):
        # §四B：GET 只读取，不触发核验工作。旧实现在此同步跑 v.apply 预演——
        # 长耗时只读校验会占住请求线程，且与任务互斥口径不一致；预演明细由
        # 计划生成流程在后台算好、确认页展示。
        self.page("<h1>请从计划确认页操作</h1>"
                  "<p class=hint>计划的预演信息与执行入口都在计划确认页；本页不再直接执行核对。</p>"
                  f"<p><a class='btn big' href='{self.px}/plan-review'>打开计划确认页</a> "
                  f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", active="home")

    def _cleanup_disabled_body(self):
        """清理入口禁用卡（§四A/§五）：如实说明不可用的原因，不宣称可恢复。"""
        return (f"<h1>{CLEANUP_DISABLED_TITLE}</h1>"
                f"<div class=card><ul class=plain>{CLEANUP_DISABLED_REASON}</ul>"
                "<p class=hint>扫描、审阅与清理清单的生成不受影响；平台验证通过后此入口会重新开放。</p></div>"
                f"<p><a class='btn ghost' href='{self.px}/plan-review'>返回计划确认页</a> "
                f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>")

    def post_apply(self, qs):
        # §四A/§五：结果可恢复性未在 fnOS 真机验证前，执行入口整体关闭。
        # 拒绝发生在占用任务之前——不产生任务行，也不触碰删除执行器本身。
        if not CLEANUP_VERIFIED:
            self.page(self._cleanup_disabled_body(), status=503, active="home")
            return
        plan_id = (qs.get("plan") or [""])[0]
        confirm = (qs.get("confirm") or [""])[0]
        if plan_id != confirm or not plan_id:
            self.page(self.error_body("确认信息不匹配", "确认值必须与计划编号完全一致；请从计划确认页操作。"), status=400, active="home")
            return
        cfg = self.cfg()
        # §四B：确认校验通过后原子占用任务；删除执行与扫描/计划共用互斥入口。
        try:
            task = tasks.claim(cfg, "apply", plan_id=plan_id)
        except tasks.Busy as busy:
            self.busy_page(busy)
            return
        threading.Thread(target=apply_worker, args=(cfg, plan_id, confirm), daemon=True).start()
        # §四B：POST 成功一律 303 → 绑定任务的删除状态页；进度轮询与完成后的
        # 结果页跳转都由该 GET 页承担，POST 响应体不含任何脚本。
        self.redirect("/apply-status?task=" + task["id"])

    def get_apply_status(self, qs=None):
        qs = qs or {}
        mode, task = self._bound_task(qs, "apply")
        if mode == "missing":
            self._task_not_found()
            return
        progress = v.read_progress(self.cfg_or_none()) or {}
        result = self.task_result(task)
        out = {"running": bool(task and task["status"] in tasks.ACTIVE),
               "plan_id": (task or {}).get("plan_id"),
               "result": result if task and task["status"] in (tasks.SUCCESS, tasks.PARTIAL) else None,
               "error": (task or {}).get("error"),
               "phase": progress.get("phase"), "done": progress.get("done", 0),
               "total": progress.get("total", 0), "freed": progress.get("freed", 0)}
        # 前端轮询取 JSON；浏览器直访显示人话状态页。
        # running 只认任务记录（§四B）：progress.json 残留 applying 不再让页面
        # 无限期显示执行中——那正是重启丢状态的老症状。
        if "application/json" not in self.headers.get("Accept", ""):
            task_q = "?task=" + task["id"] if task else ""
            if out["running"]:
                done, total = out["done"], out["total"]
                init_pct = min(100, int(done * 100 / total)) if total else 0
                body = ("<div class=card><h3><span class='status-dot d-run'></span>"
                        "正在执行删除（<span id=a-done>" + str(done) + "</span> / <span id=a-total>" + str(total) + "</span> 个文件）</h3>"
                        "<div class=bar><div id=a-bar style='width:" + str(init_pct) + "%'></div></div>"
                        "<p class=hint>每个文件删除前会做一次完整校验。完成后本页自动显示结果。</p></div>"
                        """<script>
vdPoll('""" + self.px + """/apply-status""" + task_q + """',st=>{
  const done=st.done||0,total=st.total||0;
  document.getElementById('a-done').textContent=done.toLocaleString();
  document.getElementById('a-total').textContent=total.toLocaleString();
  document.getElementById('a-bar').style.width=(total?Math.round(done*100/total):0)+'%';
},st=>{
  if(st.error){location.reload();return true;}
  if(!st.running&&st.result){location.href='""" + self.px + """/apply-result""" + task_q + """';return true;}
  return false;
});
</script>""")
            elif task and task["status"] == tasks.VERIFY:
                summary = result or {}
                pending = "日志中存在未决的暂存项，请先按删除日志核对并人工处理对应文件。" if summary.get("pending") else ""
                body = ("<div class=card><h3><span class='status-dot d-warn'></span>上次清理待核实</h3>"
                        f"<p>应用重启时清理没有执行完成。删除日志确认已完成 <b>{summary.get('deleted', 0)}</b> 项"
                        f"（日志：<code>{html.escape(str(summary.get('journal', '')))}</code>）。{pending}</p>"
                        "<p class=hint>已删除的部分不会重复删除；对同一计划重新执行时已删项自动跳过，"
                        "结果不确定的操作绝不会自动重放。</p></div>"
                        f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>")
            elif task and task["status"] == tasks.PARTIAL:
                body = self.error_body("清理部分完成后中止", task.get("error") or "") + \
                    "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；处理问题后可对同一计划重新执行，已删项自动跳过。</p>" + \
                    f"<p><a class='btn big' href='{self.px}/apply-result{task_q}'>查看已完成的部分</a></p>"
            elif task and task["status"] == tasks.FAILED:
                body = self.error_body("删除执行失败", task.get("error") or "") + \
                    "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；处理问题后可重新执行同一计划，已删项自动跳过。</p>" + \
                    f"<p><a class=btn href='{self.px}/'>返回概览</a></p>"
            elif out.get("result"):
                body = ("<div class=card><h3>" + ICONS_S['done'] + " 删除已成功执行</h3></div>"
                        f"<p><a class='btn' href='{self.px}/apply-result{task_q}'>查看结果</a></p>")
            elif progress.get("phase") == "applying":
                # 无任务记录但 progress 停在 applying：CLI 崩溃/旧版本残留，如实按中断呈现
                body = ("<div class=card><h3><span class='status-dot d-warn'></span>上次删除执行被中断</h3>"
                        "<p>删除没有执行完成。已完成的部分记录在删除日志中，不会重复删除；"
                        "处理问题后可对同一计划重新执行，已删项自动跳过。</p></div>"
                        f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>")
            else:
                body = ("<div class=card><h3>当前没有进行中的删除</h3>"
                        "<p class=hint>删除结果在执行完成后会在此显示。</p></div>"
                        f"<p><a class=btn href='{self.px}/'>返回概览</a></p>")
            self.page("<h1>删除执行状态</h1>" + body, active="home")
            return
        self.json_out(out)

    def get_apply_result(self, qs=None):
        qs = qs or {}
        mode, task = self._bound_task(qs, "apply")
        if mode == "missing":
            self._task_not_found()
            return
        cfg = self.cfg_or_none()
        if task and task["status"] in tasks.ACTIVE:
            self.page("<h1>删除正在执行中</h1><p class=hint>完成后本页会自动显示结果。</p>"
                      f"<p><a class='btn big' href='{self.px}/apply-status?task={task['id']}'>查看进度</a></p>", status=503, active="home")
            return
        if task and task["status"] == tasks.VERIFY:
            summary = self.task_result(task) or {}
            pending = "<p class=hint>日志中存在未决的暂存项：请先按删除日志核对并人工处理对应文件，再考虑重新执行。</p>" if summary.get("pending") else ""
            self.page("<h1>上次清理待核实</h1>"
                      f"<div class=card><h3>删除日志确认已完成 <b>{summary.get('deleted', 0)}</b> 项</h3>"
                      f"<p>应用重启时清理没有执行完成。日志：<code>{html.escape(str(summary.get('journal', '')))}</code></p>"
                      "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；对同一计划重新执行时已删项自动跳过，"
                      "结果不确定的操作绝不会自动重放。</p></div>" + pending +
                      f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", status=503, active="home")
            return
        if task and task["status"] == tasks.PARTIAL:
            summary = self.task_result(task) or {}
            self.page(self.error_body("清理部分完成后中止", task.get("error") or "") +
                      f"<p>删除日志确认已完成 <b>{summary.get('deleted', 0)}</b> 项"
                      f"（日志：<code>{html.escape(str(summary.get('journal', '')))}</code>）；"
                      "不会重复删除，处理后可对同一计划重新执行。</p>"
                      f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", status=503, active="home")
            return
        if task and task["status"] == tasks.FAILED:
            self.page(self.error_body("删除执行失败", task.get("error") or "") +
                      "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；处理问题后可对同一计划重新执行，已删项自动跳过。</p>" +
                      f"<p><a class=btn href='{self.px}/'>返回概览</a></p>", active="home")
            return
        # 引擎的 free_bytes_before/after 是 {root_id: 剩余字节}（free_space 按授权目录组织）；
        # 没有结果时先如实分流（处理中/被中断/无结果），绝不渲染「已删除 0 个」的成功页。
        r = self.task_result(task) if task and task["status"] == tasks.SUCCESS else None
        if task and task["status"] == tasks.SUCCESS and not r:
            # §四B「结果读取必须存在对应结果」：任务行在、终态成功，但完整结果
            # 没有落盘——如实说结果记录缺失，指向删除日志，不伪造成功页。
            self.page("<h1>该任务的结果记录缺失</h1>"
                      "<p class=hint>任务已结束，但完整结果没有保存下来；删除明细仍可按删除日志核对。</p>"
                      f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", status=404, active="home")
            return
        if not r:
            progress = (v.read_progress(cfg) or {}) if cfg else {}
            if progress.get("phase") == "applying":
                self.page("<h1>上次删除执行被中断</h1>"
                          "<p>应用重启时删除没有执行完成。已完成的部分记录在删除日志中，不会重复删除；"
                          "处理问题后可对同一计划重新执行，已删项自动跳过。</p>"
                          f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", status=503, active="home")
            else:
                self.page("<h1>当前没有删除结果</h1>"
                          "<p class=hint>尚未执行过清理，或执行记录已被清理；完成的清理会保留在任务记录中可随时回看。</p>"
                          f"<p><a class='btn big' href='{self.px}/'>返回概览</a></p>", active="home")
            return
        freed = r.get("logical_bytes", 0) / 2**20
        before = r.get("free_bytes_before") or {}
        after = r.get("free_bytes_after") or {}
        roots = {rt["id"]: rt for rt in (cfg or {}).get("roots", [])}
        devs = {}
        for rid, rt in roots.items():
            try:
                devs[rid] = os.stat(rt["path"]).st_dev
            except OSError:
                devs[rid] = rid  # 所在文件系统识别失败时按目录独立计入合计
        rows, delta, seen = [], 0, set()
        for rid in sorted(set(before) | set(after)):
            b, a = before.get(rid, 0), after.get(rid, 0)
            dev = devs.get(rid, rid)
            if dev not in seen:  # 同一文件系统的多个授权目录在合计里只计一次
                seen.add(dev)
                delta += a - b
            name = (roots.get(rid) or {"path": "已移除的目录 " + rid})["path"]
            rows.append(f"<tr><td>{html.escape(name)}</td><td>{b / 2**30:.1f} GB</td><td>{a / 2**30:.1f} GB</td>"
                        f"<td>{a - b:+,.0f} MB</td></tr>")
        self.page("<h1>" + ICONS_S['done'] + " 清理完成</h1>"
                  f"<div class=card><h3>已删除 <b>{r.get('deleted_files', 0)}</b> 个文件，释放约 {freed:.0f} MB</h3>"
                  "<table><thead><tr><th>授权目录</th><th>删除前剩余</th><th>删除后剩余</th><th>变化</th></tr></thead><tbody>"
                  + "".join(rows) +
                  f"<tr><td colspan=3>合计（同一文件系统只计一次）</td><td>{delta / 2**20:+,.0f} MB</td></tr></tbody></table>"
                  "<p>删除日志（每一步的详细记录）：<code>" + html.escape(str(r.get("journal", ""))) + "</code></p>"
                  "<p class=hint>释放量按文件大小累计估算；剩余空间以各文件系统回报为准（快照等机制可能延迟释放）。"
                  "之后再次扫描，这些文件不会再出现。</p></div>"
                  f"<p><a class=btn href='{self.px}/'>返回概览</a></p>", active="home")


_TOOL_VERSIONS = None


def tool_versions(cfg):
    global _TOOL_VERSIONS
    if _TOOL_VERSIONS is not None:
        return _TOOL_VERSIONS
    try:
        ffmpeg, ffprobe = v.resolve_tools(cfg)
        out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
        first = (out.stdout or "").splitlines()[0]
        _TOOL_VERSIONS = first.split("version")[0].strip() or "ffmpeg"
        return _TOOL_VERSIONS
    except Exception as e:
        _TOOL_VERSIONS = "ffmpeg 不可用（" + str(e)[:80] + "）"
        return _TOOL_VERSIONS


def plan_worker(cfg, scan_id, selections):
    """计划复核（逐文件 SHA+ffprobe）对大库可能持续数十分钟，放后台跑。

    任务占用在 post_plan（请求线程）完成；v.plan 内部收养并终结任务行
    （成功/失败/完整结果持久化）。progress.json 的 plan_ready/plan_error
    继续服务顶栏胶囊与旧状态兼容。"""
    try:
        result = v.plan(cfg, scan_id, selections)
        _PLAN_PREVIEW[result["plan_id"]] = v.apply(cfg, result["plan_id"])  # 预演明细仅本进程确认页展示
        v.write_progress(cfg, {"phase": "plan_ready", "plan_id": result["plan_id"],
                               "delete_files": result["delete_files"], "logical_bytes": result["logical_bytes"],
                               "done": 1, "total": 1, "current": "", "started": ""})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        v.write_progress(cfg, {"phase": "plan_error", "error": str(e)[:500],
                               "done": 0, "total": 0, "current": "", "started": ""})


def apply_worker(cfg, plan_id, confirm):
    """执行删除可能持续较久（逐文件全量终检+原子暂存），放后台跑并上报进度。

    任务占用在 post_apply 完成；v.apply 内部收养并终结任务行：成功/完整结果、
    失败结合 journal 判定 partial（已删过）或 failed。"""
    try:
        result = v.apply(cfg, plan_id, execute=True, confirm=confirm)
        v.write_progress(cfg, {"phase": "apply_done", "done": result.get("deleted_files", 0),
                               "total": result.get("deleted_files", 0), "current": "",
                               "freed": result.get("logical_bytes", 0), "started": ""})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        v.write_progress(cfg, {"phase": "apply_error", "error": str(e)[:500],
                               "done": 0, "total": 0, "current": "", "started": ""})


def scan_worker(cfg):
    """扫描在后台线程跑；任务占用在 post_scan 完成，v.inventory 收养并终结
    （成功/用户停止=部分完成/失败），此处仅兜底记录异常供诊断页展示。"""
    try:
        v.inventory(cfg)
    except Exception as e:  # surface into progress + stderr (webui.log) for diagnosis
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        v.write_progress(cfg, {"phase": "error", "error": str(e)[:500], "scan_id": "", "done": 0, "total": 0, "current": "", "started": ""})


def _reconcile_on_start(config_path):
    """webui 启动时核对未完成任务（§四B）。

    tasks.reconcile 内部先探测操作锁：仍有活进程在干活（如 CLI 引擎）时整体
    跳过，绝不误判。随后把 progress.json 残留的运行中相位落定为 interrupted，
    消灭「重启后页面长期显示执行中」的假运行。"""
    try:
        cfg = v.read_config(config_path)
    except (OSError, ValueError):
        return  # 配置不可读：首页呈现引导，不阻断启动
    settled = tasks.reconcile(cfg)
    if settled:
        progress = v.read_progress(cfg) or {}
        if progress.get("phase") in {"fingerprint", "planning", "applying"} and tasks.active(cfg) is None:
            progress["phase"] = "interrupted"
            v.write_progress(cfg, progress)


if hasattr(socket, "AF_UNIX"):
    class UnixHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_UNIX

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "videodedupe"
            self.server_port = 0
else:  # Windows 开发机：走 --tcp
    UnixHTTPServer = None


def build_server(cfg_path, socket_path=None, tcp_port=None):
    Handler.config = cfg_path
    if socket_path:
        if UnixHTTPServer is None:
            raise v.Unsafe("此平台不支持 AF_UNIX socket；请使用 --tcp 开发模式")
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.unlink()
        server = UnixHTTPServer(str(path), Handler)
        server.gateway = True
        os.chmod(path, 0o660)
        return server
    server = ThreadingHTTPServer(("127.0.0.1", tcp_port or 18099), Handler)
    server.gateway = False
    return server


def main():
    parser = argparse.ArgumentParser(description="视频查重 Web UI（飞牛统一网关）")
    parser.add_argument("--config", required=True)
    parser.add_argument("--socket", help="AF_UNIX socket 路径（fnOS 网关模式）")
    parser.add_argument("--tcp", type=int, help="开发/测试用 TCP 端口")
    args = parser.parse_args()
    # 空授权目录（roots=[]）是合法启动状态：首页呈现引导；这里不 fail fast。
    _reconcile_on_start(args.config)  # 重启核对（§四B）：终结陈旧任务行、落定假运行相位
    server = build_server(args.config, socket_path=args.socket, tcp_port=args.tcp)
    mode = args.socket or f"tcp://{server.server_address[0]}:{server.server_address[1]}"
    print(f"videodedupe webui listening on {mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

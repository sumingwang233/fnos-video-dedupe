#!/usr/bin/env python3
"""fnOS unified-gateway web UI for the video dedupe core.

Serves on an AF_UNIX socket (fnOS desktop iframe via gatewayPrefix); a TCP
fallback exists for Windows development and tests. Plain stdlib forms, no
client framework. Visual language: Strata / Gainsty design tokens (light
default, emerald accent, Outfit variable font self-hosted, no CDN); 0.2.27
replaces the former Steam-dark identity while keeping markup contracts,
polling behaviour and safety copy untouched.
Only fnOS-forwarded admin requests may act (X-Trim-Isadmin).
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import videodedupe as v

SCAN_LOCK = threading.Lock()   # one job at a time inside this process
SCAN_THREAD = {"running": False}
GATEWAY_PREFIX = "/app/video-dedupe"  # 统一网关前缀：路由时剥离，渲染时回加


def _icon(inner, cls="ic"):
    """内联线条图标：统一 viewBox 24 / stroke 1.8 / currentColor，全单引号（守住引号配对测试）。"""
    return ("<svg class='" + cls + "' viewBox='0 0 24 24' fill='none' stroke='currentColor'"
            " stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            + inner + "</svg>")


_PATHS = {
    "logo": "<rect x='3.5' y='3.5' width='12' height='12' rx='3.2'/><rect x='8.5' y='8.5' width='12' height='12' rx='3.2'/>",
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

PAGE = """<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>视频查重助手</title>
<script>try{if(localStorage.vdTheme==='dark')document.documentElement.classList.add('dark')}catch(e){}</script>
<style>
/* 设计令牌：Strata / Gainsty（浅色默认，html.dark 切深）。0.2.27 全面换装，类名契约不变。 */
@font-face{font-family:'Outfit';font-style:normal;font-weight:100 900;font-display:swap;
src:url('{px}/font?f=latin') format('woff2');
unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'Outfit';font-style:normal;font-weight:100 900;font-display:swap;
src:url('{px}/font?f=latin-ext') format('woff2');
unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}
:root{--bg:#f9fafb;--surface:#ffffff;--surface2:#f7f7f7;--line:#dee5ee;--line2:#eeeeee;
--ink:#221e1f;--ink-soft:#303030;--muted:#6f6f6f;
--accent:#10b981;--accent-hover:#0c8a60;--accent-ink:#ffffff;--tint:#ecfdf5;--accent-text:#0c8a60;--focus:#405de6;
--ok:#10b981;--ok-tint:#ecfdf5;--ok-text:#0c8a60;--warn:#e39b0b;--warn-tint:#fff6e0;--warn-text:#8a5a00;
--info:#0f97ff;--info-tint:#e8f4ff;--info-text:#0668c2;--danger:#e0283f;--danger-tint:#fdecee;--danger-text:#b01a2e;
--code-bg:#1b1f23;--code-ink:#e6e9ec;
--r-s:8px;--r-m:14px;--ease:cubic-bezier(.2,.7,.2,1);--dur:.16s;
--shadow-card:0 1px 1px rgba(15,23,42,.04),0 3px 6px -2px rgba(15,23,42,.06),0 7px 14px -6px rgba(15,23,42,.09)}
html.dark{--bg:#0e1113;--surface:#161a1d;--surface2:#1d2226;--line:#2a3036;--line2:#232830;
--ink:#eef1f3;--ink-soft:#d5dade;--muted:#8e979f;
--accent:#34d399;--accent-hover:#3fdfa8;--accent-ink:#052e20;--tint:#0f2a22;--accent-text:#4fe0ac;--focus:#7b93ff;
--ok:#34d399;--ok-tint:#0f2a22;--ok-text:#4fe0ac;--warn:#f5b642;--warn-tint:#2e2410;--warn-text:#ffd479;
--info:#57b6ff;--info-tint:#10293c;--info-text:#8fd0ff;--danger:#ff5a6e;--danger-tint:#33161c;--danger-text:#ff8d9b;
--shadow-card:0 1px 0 rgba(0,0,0,.25),0 6px 16px -8px rgba(0,0,0,.5)}
*{box-sizing:border-box}
body{font:15px/1.65 'Outfit',system-ui,-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei UI','Noto Sans CJK SC',sans-serif;margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
/* 顶栏：字标 + tab 胶囊 + 状态胶囊 + 主题切换（Strata 式分段导航） */
.topbar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--surface) 92%,transparent);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:1080px;margin:0 auto;display:flex;align-items:center;gap:14px;padding:10px 24px}
.brand{font-size:17px;font-weight:800;color:var(--ink);display:flex;align-items:center;gap:9px;text-decoration:none;letter-spacing:-.01em}
.brand .logo{color:var(--accent);display:inline-flex}
.tabs{display:flex;gap:2px;background:var(--surface2);border:1px solid var(--line);border-radius:999px;padding:3px}
.tabs a{padding:6px 16px;border-radius:999px;color:var(--muted);text-decoration:none;font-size:13.5px;font-weight:500;display:inline-flex;align-items:center;gap:7px;transition:background var(--dur) var(--ease),color var(--dur) var(--ease)}
.tabs a:hover{color:var(--ink)}
.tabs a.on{background:var(--surface);color:var(--ink);font-weight:600;box-shadow:var(--shadow-card)}
.tabs .ic{width:16px;height:16px}
.topbar .grow{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--muted);font-size:12.5px;text-decoration:none;white-space:nowrap;transition:border-color var(--dur) var(--ease),color var(--dur) var(--ease)}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--muted);display:inline-block;flex:none}
.pill:hover{border-color:var(--accent)}
.pill.st-run{border-color:color-mix(in srgb,var(--accent) 40%,var(--line));color:var(--accent-text)}
.pill.st-run i{background:var(--accent);animation:pulse 1.4s var(--ease) infinite}
.pill.st-ok{color:var(--ok-text)} .pill.st-ok i{background:var(--ok)}
.pill.st-err{color:var(--danger-text)} .pill.st-err i{background:var(--danger)}
.ver{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.iconbtn{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;border:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer;transition:border-color var(--dur) var(--ease),background var(--dur) var(--ease)}
.iconbtn:hover{border-color:var(--accent);background:var(--surface2)}
.only-light,.only-dark{display:inline-flex}
html.dark .only-light{display:none}
html:not(.dark) .only-dark{display:none}
.wrap{max-width:1080px;margin:0 auto;padding:26px 24px 96px}
h1{font-size:24px;margin:0 0 8px;color:var(--ink);letter-spacing:-.02em;font-weight:800}
h2{font-size:16px;margin:28px 0 10px;color:var(--ink);font-weight:700}
h3{font-size:14.5px;margin:0 0 4px;color:var(--ink);font-weight:600}
.lead{color:var(--muted);margin:0 0 20px;max-width:72ch}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-m);padding:20px 22px;margin:14px 0;box-shadow:var(--shadow-card)}
.card h2{margin-top:2px}
.card.note{background:var(--tint);border-color:color-mix(in srgb,var(--accent) 25%,var(--line));color:var(--ink-soft)}
.card.banner{background:var(--info-tint);border-color:color-mix(in srgb,var(--info) 25%,var(--line));color:var(--ink-soft)}
.m0{margin:0}
.role{margin:8px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
/* 概览：阶段链 + 指标大数字卡（Strata Monitor 式） */
.chain{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:2px 0 4px}
.ph{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:12.5px;background:var(--surface2);color:var(--muted)}
.ph i{width:6px;height:6px;border-radius:50%;background:currentColor;display:inline-block;flex:none}
.ph.done{background:var(--ok-tint);color:var(--ok-text)}
.ph.act{background:var(--tint);color:var(--accent-text);font-weight:600}
.ph.act i{animation:pulse 1.4s var(--ease) infinite}
.ph.err{background:var(--danger-tint);color:var(--danger-text)}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:14px 0}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-m);padding:16px 18px;box-shadow:var(--shadow-card)}
.metric .num{font-size:30px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:var(--ink);line-height:1.15}
.metric .lab{font-size:13px;color:var(--ink-soft);margin-top:2px;font-weight:600}
.metric .sub{font-size:12px;color:var(--muted);margin-top:6px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2.5px 10px;border-radius:999px;font-size:12.5px;font-weight:500;vertical-align:1px}
.b-ok{background:var(--ok-tint);color:var(--ok-text)} .b-ref{background:var(--info-tint);color:var(--info-text)}
.b-warn{background:var(--warn-tint);color:var(--warn-text)} .b-run{background:var(--tint);color:var(--accent-text)}
.b-err{background:var(--danger-tint);color:var(--danger-text)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:9px 20px;border:none;border-radius:var(--r-s);font-size:14.5px;font-family:inherit;cursor:pointer;background:var(--accent);color:var(--accent-ink);font-weight:600;text-decoration:none;transition:background var(--dur) var(--ease),border-color var(--dur) var(--ease),transform var(--dur) var(--ease)}
.btn:hover{background:var(--accent-hover)}
.btn:active{transform:translateY(1px)}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.btn.ghost:hover{background:var(--surface2);border-color:var(--accent)}
.btn.danger{background:var(--danger);color:#ffffff}
.btn.danger:hover{background:color-mix(in srgb,var(--danger) 85%,#000)}
.btn.big{padding:12px 28px;font-size:15.5px}
.btn.sm{padding:6px 14px;font-size:13px;font-weight:500}
.bar{background:var(--surface2);border-radius:999px;height:6px;overflow:hidden;margin:12px 0}
.bar>div{background:linear-gradient(90deg,var(--accent),color-mix(in srgb,var(--accent) 70%,var(--accent-hover)));height:100%;transition:width .5s var(--ease);position:relative;overflow:hidden}
.bar>div::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);transform:translateX(-100%);animation:sheen 1.9s var(--ease) infinite}
@keyframes sheen{to{transform:translateX(100%)}}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;font-variant-numeric:tabular-nums}
td,th{border-bottom:1px solid var(--line2);padding:9px 10px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12.5px;letter-spacing:.03em}
tr:hover td{background:var(--surface2)}
code{background:var(--surface2);border-radius:6px;padding:1px 7px;font-size:13px;color:var(--ink-soft);font-family:ui-monospace,Consolas,'Cascadia Mono',Menlo,monospace}
pre{background:var(--code-bg);color:var(--code-ink);border-radius:var(--r-s);padding:14px;overflow:auto;font-size:13px;line-height:1.55;font-family:ui-monospace,Consolas,'Cascadia Mono',Menlo,monospace}
.hint{font-size:13px;color:var(--muted)}
.eta-badge{margin-left:12px}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:9px;vertical-align:1px}
.d-ok{background:var(--ok)} .d-run{background:var(--accent);animation:pulse 1.4s var(--ease) infinite} .d-err{background:var(--danger)} .d-idle{background:var(--muted)} .d-warn{background:var(--warn)}
@keyframes pulse{50%{opacity:.35}}
details{margin:8px 0} summary{cursor:pointer;color:var(--muted)}
details[open] summary{color:var(--ink);margin-bottom:6px}
ul.plain{margin:6px 0;padding-left:20px} ul.plain li{margin:5px 0}
ul.tree{list-style:none;padding-left:8px;margin:6px 0}
ul.tree li{margin:5px 0}
ul.tree label{cursor:pointer}
.subtree{padding-left:26px;border-left:1px dashed var(--line);margin-left:6px}
/* 审阅页：以图为主的卡片流（结构不变，Strata 皮） */
.g-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.g-head .grow{flex:1}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(212px,1fr));gap:16px;margin-top:14px}
.tile{position:relative;background:var(--surface);border:2px solid var(--line);border-radius:var(--r-s);padding:10px;cursor:pointer;transition:transform var(--dur) var(--ease),border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease)}
.tile:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}
.tile.prot{opacity:.6;cursor:not-allowed}
.tile.sel{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 25%,transparent);background:color-mix(in srgb,var(--tint) 45%,var(--surface))}
.tile img{width:100%;height:126px;object-fit:cover;border-radius:6px;background:var(--surface2);display:block}
.thumb-blank{width:100%;height:126px;border-radius:6px;background:var(--surface2);display:flex;align-items:center;justify-content:center;color:var(--muted)}
.tile .name{font-size:13px;margin:9px 0 3px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile .meta{font-size:12px;color:var(--muted);line-height:1.55;font-variant-numeric:tabular-nums}
.tile .meta .path{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile .check{position:absolute;top:10px;left:10px;width:22px;height:22px;border-radius:50%;background:var(--accent);color:var(--accent-ink);display:none;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(15,23,42,.25)}
.tile .check svg{width:13px;height:13px}
.tile.sel .check{display:flex}
.tile .lock{position:absolute;top:10px;left:10px;color:var(--muted);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:3px;line-height:0}
.corner{position:absolute;top:10px;right:10px}
.sticky{position:sticky;bottom:0;background:color-mix(in srgb,var(--surface) 90%,transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);padding:14px 0;border-top:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:5}
.danger-text{color:var(--danger)}
.ic{width:18px;height:18px;flex:none;vertical-align:-3.5px}
.ic-s{width:15px;height:15px;vertical-align:-2.5px}
.badge svg{width:13px;height:13px;flex:none}
@media (max-width:880px){
.topbar .in{flex-wrap:wrap;padding:8px 14px;gap:8px}
.brand{font-size:15.5px}
.tabs{order:3;width:100%;overflow-x:auto}
.ver{display:none}
.wrap{padding:18px 14px 84px}
.metrics{grid-template-columns:repeat(2,1fr)}
.grid{grid-template-columns:1fr}
.tiles{grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
}
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation:none!important;transition:none!important}
}
</style></head>
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
<script>
// 统一轮询助手：正常 1.5s、失败 3s 重试；done(d) 返回 true 即停止（收尾跳转/刷新自便）。
// 0.2.17/0.2.21 曾两次因重构页面丢掉轮询脚本导致状态页静止，收口到这一处。
function vdPoll(url,tick,done){
  function step(){
    fetch(url,{headers:{'Accept':'application/json'}}).then(r=>r.json()).then(d=>{
      if(done&&done(d))return;
      if(tick)tick(d);
      setTimeout(step,1500);
    }).catch(()=>setTimeout(step,3000));
  }
  setTimeout(step,1500);
}
// 0.2.27 起默认浅色：显式存 'dark' 才深色（旧 'light' 存量自然落浅色）。
function toggleTheme(){
  const el=document.documentElement;
  const dark=el.classList.toggle('dark');
  try{localStorage.vdTheme=dark?'dark':'light'}catch(e){}
}
// 顶栏全局状态胶囊：全站可见任务态（空闲/扫描/计划/删除/出错），点击回概览。
// 前台：运行态 2.5s、空闲 10s；后台隐藏 15s 慢轮询；回到前台立即刷新。
(function(){
  const pill=document.getElementById('st-pill'),txt=document.getElementById('st-txt');
  if(!pill||!txt)return;
  let delay=10000,timer=null;
  function schedule(ms){clearTimeout(timer);timer=setTimeout(step,ms);}
  function draw(p){
    const ph=p.phase||'idle';
    const t=p.total||0,d=p.done||0;
    const pct=t?Math.min(99,Math.round(d*100/t)):null;
    let s='空闲',c='';
    if(ph==='fingerprint'){s='扫描中'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='planning'){s='生成计划'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='applying'){s='删除中'+(pct!==null?' '+pct+'%':'');c='st-run';}
    else if(ph==='error'){s='出错';c='st-err';}
    else if(ph==='apply_done'){s='清理完成';c='st-ok';}
    else if(ph==='plan_ready'){s='待确认删除';c='st-ok';}
    else if(ph==='done'){s='扫描完成';c='st-ok';}
    txt.textContent=s;
    pill.className='pill '+c;
    delay=(c==='st-run')?2500:10000;
  }
  function step(){
    fetch('{px}/progress',{headers:{'Accept':'application/json'}}).then(r=>r.json())
      .then(d=>{draw(d);schedule(document.hidden?15000:delay);})
      .catch(()=>schedule(document.hidden?30000:5000));
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)schedule(0);});
  schedule(400);
})();
function toggleTile(tile){
  if(tile.classList.contains('prot'))return;
  const cb=tile.querySelector('input');
  cb.checked=!cb.checked;
  tile.classList.toggle('sel',cb.checked);
  tile.setAttribute('aria-checked',cb.checked?'true':'false');
}
function tileKey(e,tile){
  if(e.key==='Enter'||e.key===' '){e.preventDefault();toggleTile(tile);}
}
function pickAll(keepRecommended){
  document.querySelectorAll('.tile').forEach(t=>{
    if(t.classList.contains('prot'))return;
    const cb=t.querySelector('input');
    cb.checked=keepRecommended&&t.dataset.smallest!=='1';
    t.classList.toggle('sel',cb.checked);
    t.setAttribute('aria-checked',cb.checked?'true':'false');
  });
}
function pickGroup(btn,keepSmallest){
  const card=btn.closest('.card');
  card.querySelectorAll('.tile').forEach(t=>{
    if(t.classList.contains('prot'))return;
    const cb=t.querySelector('input');
    const isSmallest=t.dataset.smallest==='1';
    cb.checked=keepSmallest&&!isSmallest;
    t.classList.toggle('sel',cb.checked);
    t.setAttribute('aria-checked',cb.checked?'true':'false');
  });
}
</script>"""


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

    def is_admin(self):
        # Gateway mode: fnOS validates the session and forwards identity
        # headers. TCP dev mode (no gateway) is trusted only when enabled.
        if self.server.gateway:
            return self.headers.get("X-Trim-Isadmin", "").lower() == "true"
        return os.environ.get("VD_ALLOW_NO_ADMIN", "") == "1"

    def body_qs(self):
        length = int(self.headers.get("Content-Length") or 0)
        return parse_qs(self.rfile.read(length).decode("utf-8"))

    def cfg(self):
        return v.read_config(self.config)

    def cfg_or_none(self):
        try:
            return self.cfg()
        except v.Unsafe:
            return None

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
            if url.path == "/":
                self.get_home(active="home")
            elif url.path == "/config":
                self.get_config(active="config")
            elif url.path == "/progress":
                self.get_progress()
            elif url.path == "/plan-status":
                self.get_plan_status()
            elif url.path == "/plan-review":
                self.get_plan_review()
            elif url.path == "/apply-status":
                self.get_apply_status()
            elif url.path == "/apply-result":
                self.get_apply_result()
            elif url.path == "/browse":
                self.get_browse(parse_qs(url.query))
            elif url.path == "/thumb":
                self.get_thumb(parse_qs(url.query))
            elif url.path == "/font":
                self.get_font(parse_qs(url.query))
            elif url.path == "/review":
                self.get_review(parse_qs(url.query))
            elif url.path == "/apply":
                self.get_apply(parse_qs(url.query))
            else:
                self.page("<h1>页面不存在</h1><p class=hint>请从概览页进入。</p>", status=404, active="home")
        except v.Unsafe as e:
            self.page(self.error_body("无法完成该操作", str(e)), active="home")
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
            if not self.is_admin():
                self.page(self.error_body("需要管理员权限", "此操作仅限管理员执行；请确认你以管理员账号登录飞牛。"), status=403, active="home")
                return
            qs = self.body_qs()
            if url.path == "/scan":
                self.post_scan(qs)
            elif url.path == "/cancel":
                self.post_cancel()
            elif url.path == "/plan":
                self.post_plan(qs)
            elif url.path == "/apply":
                self.post_apply(qs)
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
    def get_home(self, active=""):
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
        plan_running = PLAN_STATE["running"] or phase == "planning"
        applying = APPLY_STATE["running"] or phase == "applying"
        scanning = SCAN_THREAD["running"] and phase in {"fingerprint", "idle", ""}
        # 阶段链（Strata Monitor 式状态条）：done=已过、act=进行中、err=出错、默认灰=未到
        if applying:
            chain = ("done", "done", "done", "act")
        elif plan_running:
            chain = ("done", "done", "act", "next")
        elif scanning:
            chain = ("act", "next", "next", "next")
        elif phase == "error":
            chain = ("err", "next", "next", "next")
        elif phase == "plan_ready":
            chain = ("done", "done", "act", "next")
        elif phase == "apply_done":
            chain = ("done", "done", "done", "done")
        elif phase == "done":
            chain = ("done", "next", "next", "next")
        else:
            chain = ("next", "next", "next", "next")
        parts = ["<h1>概览</h1>",
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
                         f"<p><a class='btn ghost' href='{self.px}/apply-status'>查看详细状态</a></p></div>")
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
                         f"<p><a class='btn ghost' href='{self.px}/plan-status'>查看详细状态</a></p></div>")
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
            done, total = progress.get("done", 0), progress.get("total", 0)
            pct = min(99, int(done * 100 / total)) if total else 0
            started = progress.get("started", "")
            parts.append("<div class=card id=scanbox>"
                         "<h3><span class='status-dot d-run'></span>正在扫描（<span id=p-done>"
                         f"{done:,}</span> / <span id=p-total>{total:,}</span> 个视频）"
                         "<span id=p-eta class='badge b-run eta-badge'>估计剩余时间：计算中…</span></h3>"
                         "<div class=bar><div id=p-bar style='width:" + str(pct) + "%'></div></div>"
                         f"<p class=hint>正在读取：<span id=p-cur>{html.escape(str(progress.get('current', '')))}</span></p>"
                         f"<form method=post action='{self.px}/cancel' onsubmit='return confirm(\"确定要取消这次扫描吗？已完成的识别会保留，下次扫描会接着进行。\")'><button class='btn danger'>取消扫描</button></form></div>")
            # 静默轮询：只更新数字，不整页刷新（整页刷新会白屏闪烁）；
            # ETA 按已用时间/完成比例推算，前 2% 不显示（误差过大）。
            parts.append("""<script>
let startMs=Date.now();
vdPoll('""" + self.px + """/progress',d=>{
  if(d.phase!=='fingerprint')return;
  const done=d.done||0,total=d.total||0;
  document.getElementById('p-done').textContent=done.toLocaleString();
  document.getElementById('p-total').textContent=total.toLocaleString();
  const p=total?Math.min(99,Math.round(done*100/total)):0;
  document.getElementById('p-bar').style.width=p+'%';
  document.getElementById('p-cur').textContent=d.current||'';
  const etaEl=document.getElementById('p-eta');
  if(p>=2){
    const elapsed=(Date.now()-startMs)/1000;
    const remain=Math.max(0,Math.round(elapsed*(100-p)/p));
    const h=Math.floor(remain/3600),m=Math.floor(remain%3600/60),s=remain%60;
    etaEl.textContent='估计剩余时间：'+(h?h+' 小时 ':'')+(m?m+' 分 ':(s?s+' 秒':''))+(h||m?'':s+' 秒');
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
                         f"<form method=post action='{self.px}/scan'><button class=btn>重新尝试扫描</button></form></div>")
        else:
            dot = "d-ok" if phase == "done" else "d-idle"
            if phase == "done":
                scan_id = progress.get("scan_id", "")
                parts.append(f"<div class=card><h3><span class='status-dot {dot}'></span>扫描完成</h3>"
                             "<p class=hint>扫描会逐个读取视频画面并计算指纹；之后再次扫描会自动跳过没变化的文件，速度很快。</p>"
                             f"<a class='btn big' href='{self.px}/review?scan={scan_id}'>查看发现的重复视频</a> "
                             f"<form method=post style=display:inline action='{self.px}/scan' onsubmit='return startScopedScan(this)'>"
                             "<button class='btn ghost'>重新扫描</button></form></div>")
            else:
                parts.append("<div class=card><h3><span class='status-dot d-idle'></span>空闲，尚未扫描</h3>"
                             "<p class=hint>扫描会逐个读取视频画面并计算指纹，大文件夹首次可能需要数小时；期间建议暂停下载和文件整理。"
                             "之后再次扫描会自动跳过没变化的文件，速度很快。</p>"
                             f"<form method=post action='{self.px}/scan' onsubmit='return startScopedScan(this)'>"
                             "<button class='btn big'>开始扫描</button></form></div>")
        # 指标大数字卡：取最近一次扫描（候选口径与审阅页预勾选完全一致）
        metrics = None
        if not (scanning or applying or plan_running):
            with v.state(cfg, exclusive=False) as db:
                row = db.execute("SELECT id FROM scans ORDER BY created DESC LIMIT 1").fetchone()
            if row:
                try:
                    with v.state(cfg, exclusive=False) as db:
                        captured = v.load_scan(db, cfg, row[0])
                        groups = v.load_groups(db, row[0])
                    reference_ids = {r["id"] for r in cfg["roots"] if r["role"] == "reference"}
                    candidates = reclaim = 0
                    for g in groups:
                        members = [captured[k] for k in g["members"]]
                        smallest = min(members, key=lambda m: (m["stat"]["size"], m["root"] not in reference_ids, v.key(m)))
                        for m in members:
                            if m["root"] not in reference_ids and m["stat"]["nlink"] <= 1 and m is not smallest:
                                candidates += 1
                                reclaim += m["stat"]["size"]
                    metrics = (len(groups), candidates, reclaim, len(captured))
                except (KeyError, OSError, json.JSONDecodeError):
                    metrics = None

        def _metric(num, lab, sub):
            return (f"<div class=metric><div class=num>{num}</div><div class=lab>{lab}</div>"
                    f"<div class=sub>{sub}</div></div>")

        if metrics:
            g_n, c_n, reclaim, files_n = metrics
            human = (f"{reclaim / 2**30:.1f} GB" if reclaim >= 2**30
                     else f"{reclaim / 2**20:.1f} MB" if reclaim >= 2**20
                     else f"{reclaim / 2**10:.0f} KB")
            m_html = (_metric(f"{g_n:,}", "重复组", "疑似重复或相似的视频分组")
                      + _metric(f"{c_n:,}", "删除候选", "按建议预勾选的文件数")
                      + _metric(human, "可释放空间", "删除全部候选后预计释放")
                      + _metric(f"{files_n:,}", "视频总数", "最近一次扫描覆盖的文件"))
        else:
            m_html = (_metric("—", "重复组", "完成扫描后显示")
                      + _metric("—", "删除候选", "完成扫描后显示")
                      + _metric("—", "可释放空间", "完成扫描后显示")
                      + _metric("—", "视频总数", "完成扫描后显示"))
        parts.append("<div class=metrics>" + m_html + "</div>")
        dirs = "".join(
            f"<div class=card><h3>{ICONS['folder']} <code>{html.escape(r['path'])}</code></h3>"
            f"<p class=role>{'<span class=\"badge b-ok\">读写授权：确认后可删除此处重复视频</span>' if r['role'] == 'cleanup' else '<span class=\"badge b-ref\">只读授权：仅作对比参考，绝不删除</span>'}</p>"
            f"<p><button type=button class='btn ghost sm' onclick=\"browseTree(this,'{html.escape(r['id'], True)}')\">选择要扫描的子文件夹</button></p>"
            f"<div id='tree-{html.escape(r['id'], True)}' style='display:none'></div></div>"
            for r in cfg["roots"])
        parts.append("<h2>扫描范围</h2>"
                     "<p class=hint>在「系统设置 → 应用 → 访问权限」中增删文件夹；改动后重启本应用生效。"
                     "点「选择要扫描的子文件夹」可只扫描部分子目录（不勾选则扫描全部）。</p>"
                     "<p id=scope-summary class=hint>未勾选时扫描全部</p>"
                     f"<div class=grid>{dirs}</div>")
        # 子文件夹树：懒加载浏览 + 勾选；勾选后按子目录范围扫描
        parts.append("""<script>
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
    cb.type = 'checkbox'; cb.value = d.rel; cb.dataset.name = d.name;
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
function updateScopeSummary(){
  const picked = [];
  document.querySelectorAll('.tree input:checked').forEach(cb => picked.push(cb.dataset.name || cb.value));
  const el = document.getElementById('scope-summary');
  el.textContent = picked.length ? '已选择 ' + picked.length + ' 个子文件夹：' + picked.join('、') : '未勾选时扫描全部';
}
async function startScopedScan(form){
  const picked = [];
  document.querySelectorAll('.tree input:checked').forEach(cb => picked.push(cb.value));
  if (!confirm(picked.length ? '只扫描勾选的 ' + picked.length + ' 个子文件夹，现在开始吗？' : '未勾选任何子文件夹，将扫描全部，现在开始吗？')) return false;
  for (const v of picked) {
    const input = document.createElement('input');
    input.type = 'hidden'; input.name = 'subdir'; input.value = v;
    form.appendChild(input);
  }
  return true;
}
</script>""")
        with v.state(cfg, exclusive=False) as db:
            scans = db.execute("SELECT id,created FROM scans ORDER BY created DESC LIMIT 10").fetchall()
        if scans:
            rows = "".join(
                f"<tr><td><code>{s[:12]}…</code></td><td>{created}</td>"
                f"<td><a class='btn ghost sm' href='{self.px}/review?scan={s}'>查看重复情况</a></td></tr>"
                for s, created in scans)
            parts.append("<h2>历次扫描</h2><table><tr><th>编号</th><th>时间</th><th></th></tr>" + rows + "</table>")
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
        if PLAN_STATE["running"]:
            banner = f"<div class='card banner'><p class=m0>{ICONS_S['clock']} 清理计划正在后台生成。<a href='{self.px}/plan-status'>查看进度</a></p></div>"
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
        if PLAN_STATE["running"]:
            self.page("<h1>清理计划正在生成中</h1><p>请等计划生成完成后再开始扫描。</p>"
                      f"<p><a class=btn href='{self.px}/plan-status'>查看进度</a></p>", status=503, active="home")
            return
        if SCAN_THREAD["running"] or SCAN_LOCK.locked():
            self.page("<h1>扫描已在进行中</h1><p><a class=btn href='" + self.px + "/'>查看进度</a></p>", status=503, active="home")
            return
        cfg = self.cfg()
        subdirs = qs.get("subdir", [])
        if subdirs:
            cfg["scan_subdirs"] = sorted(set(subdirs))
        with SCAN_LOCK:
            SCAN_THREAD["running"] = True
            threading.Thread(target=scan_worker, args=(cfg,), daemon=True).start()
        self.page("<h1>扫描已开始</h1><p>正在逐个读取视频画面。您可以离开本页面，随时回来看进度。</p>"
                  f"<p><a class=btn href='{self.px}/'>查看扫描进度</a></p>", active="home")

    def post_cancel(self):
        cfg = self.cfg()
        v.cancel_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        v.cancel_path(cfg).touch()
        self.page("<h1>已请求取消</h1><p>当前视频处理完就停止；已识别的指纹会保留，下次扫描接着进行。</p>"
                  f"<p><a class=btn href='{self.px}/'>返回概览</a></p>", active="home")

    # --- review -----------------------------------------------------------------
    def get_review(self, qs, active=""):
        scan_id = (qs.get("scan") or [""])[0]
        if not scan_id:
            cfg = self.cfg()
            with v.state(cfg, exclusive=False) as db:
                row = db.execute("SELECT id FROM scans ORDER BY created DESC LIMIT 1").fetchone()
            if not row:
                self.page("<h1>还没有可审阅的结果</h1><p class=lead>先到概览页完成一次扫描，这里就会显示发现的重复视频。</p>"
                          f"<p><a class=btn href='{self.px}/'>去概览</a></p>", active="review")
                return
            scan_id = row[0]
        if SCAN_THREAD["running"]:
            self.page("<h1>扫描正在进行中</h1><p>请等扫描完成后再来审阅；<a href='" + self.px + "/'>查看进度</a>。</p>", status=503, active="review")
            return
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            captured = v.load_scan(db, cfg, scan_id)
            groups = v.load_groups(db, scan_id)
        if not groups:
            self.page(f"<h1>审阅</h1><div class=card><h3>{ICONS_S['sparkle']} 没有发现重复或相似的视频</h3>"
                      f"<p class=hint>这批文件夹里没有可清理的内容。</p></div>"
                      f"<p><a class='btn ghost' href='{self.px}/'>返回概览</a></p>", active="review")
            return
        banner = ""
        if PLAN_STATE["running"]:
            progress = v.read_progress(cfg) or {}
            banner = (f"<div class='card banner'><p class=m0>{ICONS_S['clock']} 清理计划正在后台生成"
                      f"（已核对 {progress.get('done', 0)}/{progress.get('total', 0)} 个文件）；生成期间本页仍可浏览，"
                      f"但请勿重复提交。<a href='{self.px}/plan-status'>查看进度</a></p></div>")
        parts = [banner, f"<h1>审阅重复视频</h1><p class=lead>共 {len(groups)} 组疑似重复。"
                 f"点画面卡片勾选要删除的文件；<span class='badge b-err'>{ICONS_S['lock']} 不可删除</span>"
                 "指参考文件夹或被多处引用的文件。<b>每组至少保留一个</b>。</p>",
                 f"<form method=post action='{self.px}/plan'><input type=hidden name=scan value='" + scan_id + "'>"]
        parts.append("<div class='card note'><p class=m0>" + ICONS_S['bulb'] +
                     " 每组都标注了「建议保留」的最小文件；「画面相似」只是候选线索：语言、音轨、字幕、剪辑版本可能不同，"
                     "请看预览图确认是同一内容再删。</p></div>")
        for g in groups:
            members = [captured[k] for k in g["members"]]
            reference_ids = {r["id"] for r in cfg["roots"] if r["role"] == "reference"}
            smallest = min(members, key=lambda m: (m["stat"]["size"], m["root"] not in reference_ids, v.key(m)))
            total_mb = sum(m["stat"]["size"] for m in members) / 2**20
            kind_badge = ("<span class='badge b-ok'>内容完全一致</span>" if g["kind"] == "exact"
                          else "<span class='badge b-warn'>画面相似 · 请确认是同一内容</span>")
            parts.append("<div class=card><div class=g-head>"
                         f"<h3>{kind_badge} {len(members)} 个文件 · 合计 {total_mb:.0f} MB</h3><span class=grow></span>"
                         f"<button type=button class='btn ghost sm' onclick='pickGroup(this,true)'>保留最小版本，勾选其余</button>"
                         f"<button type=button class='btn ghost sm' onclick='pickGroup(this,false)'>清空勾选</button>"
                         "</div><div class=tiles>")
            for item in members:
                root = next(r for r in cfg["roots"] if r["id"] == item["root"])
                protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
                meta = item.get("meta", {})
                distance = ""
                if g["kind"] == "visual" and item.get("vhash") and len(members) > 1:
                    other = next(m for m in members if m is not item and m.get("vhash"))
                    distance = round(sum(v.hamming(x, y) for x, y in zip(item["vhash"], other["vhash"])) / len(v.FRAME_POSITIONS), 1)
                thumb = (f"<img src='{self.px}/thumb?scan={scan_id}&f={html.escape(v.key(item))}' loading=lazy alt=''>"
                         if item.get("thumb") else "<div class=thumb-blank></div>")
                # 默认勾选推荐删除项：非最小（建议保留）且未受保护的文件
                recommended = not protected and item is not smallest
                cls = ("tile prot" if protected else "tile") + (" sel" if recommended else "")
                badge = ""
                if protected:
                    badge = "<span class=lock title='不可删除：只读文件夹或被多处引用'>" + ICONS["lock"] + "</span>"
                elif item is smallest:
                    badge = "<span class='badge b-ok corner'>建议保留</span>"
                sim = f"<br>相似度差异 {distance}" if distance != "" else ""
                parts.append(
                    f"<div class='{cls}' data-smallest='{'1' if item is smallest else '0'}' role=checkbox"
                    f" aria-checked='{'true' if recommended else 'false'}' aria-disabled='{'true' if protected else 'false'}'"
                    f" tabindex='{'-1' if protected else '0'}' onclick='toggleTile(this)' onkeydown='tileKey(event,this)'>"
                    f"{badge}{thumb}<span class=check aria-hidden='true'>{ICONS['check']}</span>"
                    f"<div class=name title='{html.escape(item['rel'])}'>{html.escape(Path(item['rel']).name)}</div>"
                    f"<div class=meta>{item['stat']['size'] / 2**20:.1f} MB · {meta.get('duration', 0):.0f} 秒 · "
                    f"{meta.get('width') or '?'}×{meta.get('height') or '?'}{sim}"
                    f"<span class=path title='{html.escape(item['rel'])}'>{ICONS_S['folder']} {html.escape(root['path'] + '/' + str(Path(item['rel']).parent))}</span></div>"
                    f"<input type=checkbox name=sel value='{html.escape(v.key(item))}' {'checked' if recommended else ''} {'disabled' if protected else ''} style='display:none'></div>")
            parts.append("</div></div>")
        parts.append("<div class=sticky>"
                     "<button class='btn big'>下一步：生成清理计划（先预演，不会删除）</button>"
                     "<button type=button class='btn ghost' onclick='pickAll(true)'>全部按建议勾选</button>"
                     "<button type=button class='btn ghost' onclick='pickAll(false)'>全部取消勾选</button>"
                     "<span class=hint>已按建议预勾选（每组保留最小的，其余为删除候选）；勾选的文件将进入计划，生成后还会再让你确认一次。</span></div></form>")
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
        scan_id = (qs.get("scan") or [""])[0]
        ident = (qs.get("f") or [""])[0]
        cfg = self.cfg()
        with v.state(cfg, exclusive=False) as db:
            row = db.execute("SELECT record FROM files WHERE scan=? AND key=?", (scan_id, ident)).fetchone()
        name = json.loads(row[0]).get("thumb") if row else None
        data = (Path(cfg["state_dir"]) / "thumbs" / name).read_bytes() if name else b""
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

    # --- plan / apply ---------------------------------------------------------
    def post_plan(self, qs):
        scan_id = (qs.get("scan") or [""])[0]
        selections = qs.get("sel", [])
        sys.stderr.write("post_plan scan=%r n_sel=%d sel0=%r\n" % (scan_id, len(selections), selections[0] if selections else None))
        sys.stderr.flush()
        if SCAN_THREAD["running"]:
            self.page("<h1>扫描正在进行中</h1>", status=503, active="home")
            return
        if PLAN_STATE["running"]:
            self.page("<h1>清理计划正在生成中</h1><p>上一个计划还没算完；完成后可再次操作。</p>"
                      f"<p><a class=btn href='{self.px}/plan-status'>查看进度</a></p>", status=503, active="home")
            return
        if not selections:
            self.page(self.error_body("没有勾选任何文件", "请至少勾选一个要删除的文件，再生成清理计划。"), status=400, active="home")
            return
        PLAN_STATE.update(running=True, scan_id=scan_id, plan_id=None, error=None,
                          logical_bytes=0, delete_files=0)
        threading.Thread(target=plan_worker, args=(self.cfg(), scan_id, selections), daemon=True).start()
        self.page("<h1>正在生成清理计划…</h1>"
                  "<div class=card id=planbox><h3><span class='status-dot d-run'></span>"
                  "<span id=pl-msg>正在逐个核对文件（完整性校验 + 读取音轨字幕信息）</span></h3>"
                  "<div class=bar><div id=pl-bar style='width:0%'></div></div>"
                  "<p class=hint>这一步只读文件、不做任何修改；大文件夹可能需要较长时间，请保持本页打开。</p></div>"
                  "<script>setTimeout(()=>location.reload(),3000)</script>", active="home")

    def persisted_plan(self, cfg):
        """应用重启后从 progress.json 恢复已生成的计划。"""
        try:
            progress = v.read_progress(cfg) or {}
            if progress.get("phase") == "plan_ready" and progress.get("plan_id"):
                return progress
        except (OSError, ValueError):
            pass
        return None

    def get_plan_review(self):
        cfg = self.cfg_or_none()
        persisted = self.persisted_plan(cfg) if cfg else None
        if PLAN_STATE["running"] or (not PLAN_STATE.get("plan_id") and not persisted):
            interrupted = (v.read_progress(cfg) or {}).get("phase") == "planning" and not PLAN_STATE["running"]
            if interrupted:
                self.page("<h1>上次计划生成被中断</h1><p>应用重启时计划没有生成完成，需要回到审阅页重新勾选并生成"
                          "（扫描结果和指纹缓存不受影响）。</p>"
                          f"<p><a class='btn big' href='{self.px}/review'>返回审阅重新生成</a> "
                          f"<a class='btn ghost' href='{self.px}/'>返回概览</a></p>", status=503, active="review")
            else:
                self.page("<h1>清理计划正在生成中</h1><p>完成后本页会自动可用；"
                          f"<a class=btn href='{self.px}/plan-status'>查看进度</a></p>", status=503, active="home")
            return
        if PLAN_STATE.get("error") and not persisted:
            cfg = self.cfg_or_none()
            self.page(self.error_body("生成清理计划失败", PLAN_STATE["error"]) +
                      f"<p><a class='btn ghost' href='{self.px}/review?scan={PLAN_STATE.get('scan_id', '')}'>返回审阅</a></p>", active="home")
            return
        # 内存状态（本进程生成）优先；否则用持久化状态（应用重启后恢复）。
        if PLAN_STATE.get("plan_id"):
            plan_id = PLAN_STATE["plan_id"]
            delete_files = PLAN_STATE.get("delete_files", 0)
            logical_bytes = PLAN_STATE.get("logical_bytes", 0)
            preview = PLAN_STATE.get("preview", {})
            scan_id = PLAN_STATE.get("scan_id", "")
        else:
            plan_id = persisted["plan_id"]
            delete_files = persisted.get("delete_files", 0)
            logical_bytes = persisted.get("logical_bytes", 0)
            preview = {}  # 重启恢复：预演明细不再重放，执行时会做完整校验
            scan_id = ""
        mb = logical_bytes / 2**20
        self.page(f"<h1>清理计划已生成（尚未删除）</h1>"
                  f"<div class=card><h3>本次将删除 <b class=danger-text>{delete_files}</b> 个文件，"
                  f"预计释放约 {mb:.0f} MB 空间</h3>"
                  "<p class=hint>删除是<strong>永久性的</strong>，不进回收站、无法恢复。执行时会再次核对每个文件，"
                  "任何异常都会整批停止；删除明细会自动存档备查。</p></div>"
                  f"<details><summary>查看预演结果（技术信息）</summary><pre>{html.escape(json.dumps(preview, ensure_ascii=False, indent=2))}</pre></details>"
                  f"<p><form method=post action='{self.px}/apply' onsubmit='return confirm(\"再次确认：永久删除这些文件，不可恢复？\")'>"
                  f"<input type=hidden name=plan value='{plan_id}'>"
                  f"<input type=hidden name=confirm value='{plan_id}'>"
                  f"<button class='btn danger big'>确认执行删除</button></form> "
                  f"<a class='btn ghost' href='{self.px}/review?scan={scan_id}'>返回审阅，再想想</a></p>", active="home")

    def get_plan_status(self):
        progress = v.read_progress(self.cfg_or_none()) or {}
        out = {"running": PLAN_STATE["running"], "plan_id": PLAN_STATE.get("plan_id"),
               "error": PLAN_STATE.get("error"),
               "phase": progress.get("phase"), "done": progress.get("done", 0), "total": progress.get("total", 0)}
        # 给前端轮询的是 JSON；人用浏览器直接打开时显示状态页
        if "application/json" not in self.headers.get("Accept", ""):
            phase = out["phase"]
            cfg = self.cfg_or_none()
            persisted = self.persisted_plan(cfg) if cfg else None
            if out["running"] or (phase == "planning" and PLAN_STATE["running"]):
                done, total = out["done"], out["total"]
                init_pct = min(99, int(done * 100 / total)) if total else 0
                status_html = ("<div class=card id=planbox><h3><span class='status-dot d-run'></span>"
                               "正在生成清理计划</h3>"
                               f"<div class=bar><div id=pl-bar style='width:{init_pct}%'></div></div>"
                               f"<p class=hint>已核对 <span id=pl-done>{done}</span> / <span id=pl-total>{total}</span> 个文件。"
                               "完成后本页自动跳到确认页。</p></div>"
                               """<script>
vdPoll('""" + self.px + """/plan-status',st=>{
  const done=st.done||0,total=st.total||0;
  document.getElementById('pl-done').textContent=done.toLocaleString();
  document.getElementById('pl-total').textContent=total.toLocaleString();
  document.getElementById('pl-bar').style.width=(total?Math.min(99,Math.round(done*100/total)):0)+'%';
},st=>{
  if(st.error){location.reload();return true;}
  if(!st.running){
    if(st.plan_id){location.href='""" + self.px + """/plan-review';}else{location.reload();}
    return true;
  }
  return false;
});
</script>""")
            elif phase == "planning":
                status_html = ("<div class=card><h3><span class='status-dot d-warn'></span>上次生成被中断</h3>"
                               "<p>应用重启时计划生成没有完成（指纹缓存都在，重新生成会很快跳过已核对的部分语义，需重新核对）。</p></div>"
                               f"<p><a class='btn big' href='{self.px}/review'>返回审阅重新生成</a></p>")
            elif phase == "plan_ready" or (persisted and not PLAN_STATE.get("error")):
                pid = (persisted or {}).get("plan_id") or PLAN_STATE.get("plan_id") or ""
                status_html = ("<div class=card><h3><span class='status-dot d-ok'></span>计划已生成，等待你确认</h3>"
                               "<p>核对已完成。请查看计划内容，确认后执行删除。</p></div>"
                               f"<p><a class='btn big' href='{self.px}/plan-review'>查看计划并确认删除</a> "
                               f"<a class='btn ghost' href='{self.px}/review'>返回审阅重新勾选</a></p>")
            elif phase == "plan_error" or out.get("error"):
                err = (persisted or {}).get("error") or out.get("error") or ""
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
        plan_id = (qs.get("plan") or [""])[0]
        if SCAN_THREAD["running"]:
            self.page("<h1>扫描正在进行中</h1>", status=503, active="home")
            return
        preview = v.apply(self.cfg(), plan_id)
        self.page(f"<h1>计划 {plan_id[:8]} 预演</h1><pre>{html.escape(json.dumps(preview, ensure_ascii=False, indent=2))}</pre>"
                  f"<p class=hint>执行删除请从计划确认页操作。</p>", active="home")

    def post_apply(self, qs):
        plan_id = (qs.get("plan") or [""])[0]
        confirm = (qs.get("confirm") or [""])[0]
        if APPLY_STATE["running"]:
            self.page("<h1>删除正在执行中</h1><p><a class=btn href='" + self.px + "/apply-status'>查看进度</a></p>", status=503, active="home")
            return
        if SCAN_THREAD["running"] or PLAN_STATE["running"]:
            self.page("<h1>其他任务正在进行中</h1><p>请等其完成后再执行删除。</p>", status=503, active="home")
            return
        if plan_id != confirm or not plan_id:
            self.page(self.error_body("确认信息不匹配", "确认值必须与计划编号完全一致；请从计划确认页操作。"), status=400, active="home")
            return
        APPLY_STATE.update(running=True, plan_id=plan_id, result=None, error=None)
        threading.Thread(target=apply_worker, args=(self.cfg(), plan_id, confirm), daemon=True).start()
        self.page("<h1>正在执行删除…</h1>"
                  "<div class=card><h3><span class='status-dot d-run'></span>"
                  "正在逐个核对并删除（<span id=a-done>0</span> / <span id=a-total>?</span> 个文件）</h3>"
                  "<div class=bar><div id=a-bar style='width:0%'></div></div>"
                  "<p class=hint>每个文件删除前都会做一次完整校验（安全步骤，耗时与文件总大小成正比）。"
                  "已释放约 <span id=a-freed>0</span> MB。</p></div>"
                  """<script>
vdPoll('""" + self.px + """/apply-status',st=>{
  if(st.phase!=='applying')return;
  const done=st.done||0,total=st.total||0;
  document.getElementById('a-done').textContent=done.toLocaleString();
  document.getElementById('a-total').textContent=total.toLocaleString();
  document.getElementById('a-bar').style.width=(total?Math.round(done*100/total):0)+'%';
  document.getElementById('a-freed').textContent=((st.freed||0)/1048576).toFixed(0);
},st=>{
  if(st.error){location.reload();return true;}
  if(!st.running&&st.result){location.href='""" + self.px + """/apply-result';return true;}
  return false;
});
</script>""", active="home")

    def get_apply_status(self):
        progress = v.read_progress(self.cfg_or_none()) or {}
        out = {"running": APPLY_STATE["running"], "plan_id": APPLY_STATE.get("plan_id"),
               "result": APPLY_STATE.get("result"), "error": APPLY_STATE.get("error"),
               "phase": progress.get("phase"), "done": progress.get("done", 0),
               "total": progress.get("total", 0), "freed": progress.get("freed", 0)}
        # 前端轮询取 JSON；浏览器直访显示人话状态页
        if "application/json" not in self.headers.get("Accept", ""):
            if out["running"] or progress.get("phase") == "applying":
                done, total = out["done"], out["total"]
                init_pct = min(100, int(done * 100 / total)) if total else 0
                body = ("<div class=card><h3><span class='status-dot d-run'></span>"
                        "正在执行删除（<span id=a-done>" + str(done) + "</span> / <span id=a-total>" + str(total) + "</span> 个文件）</h3>"
                        "<div class=bar><div id=a-bar style='width:" + str(init_pct) + "%'></div></div>"
                        "<p class=hint>每个文件删除前都会做一次完整校验。完成后本页自动显示结果。</p></div>"
                        """<script>
vdPoll('""" + self.px + """/apply-status',st=>{
  const done=st.done||0,total=st.total||0;
  document.getElementById('a-done').textContent=done.toLocaleString();
  document.getElementById('a-total').textContent=total.toLocaleString();
  document.getElementById('a-bar').style.width=(total?Math.round(done*100/total):0)+'%';
},st=>{
  if(st.error){location.reload();return true;}
  if(!st.running&&st.result){location.href='""" + self.px + """/apply-result';return true;}
  return false;
});
</script>""")
            elif APPLY_STATE.get("error"):
                body = self.error_body("删除执行失败", APPLY_STATE["error"]) + \
                    "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；处理问题后可重新执行同一计划，已删项自动跳过。</p>" + \
                    f"<p><a class=btn href='{self.px}/'>返回概览</a></p>"
            elif out.get("result"):
                body = ("<div class=card><h3>" + ICONS_S['done'] + " 删除已成功执行</h3></div>"
                        f"<p><a class=btn href='{self.px}/apply-result'>查看结果</a></p>")
            else:
                body = ("<div class=card><h3>当前没有进行中的删除</h3>"
                        "<p class=hint>删除结果在执行完成后会在此显示。</p></div>"
                        f"<p><a class=btn href='{self.px}/'>返回概览</a></p>")
            self.page("<h1>删除执行状态</h1>" + body, active="home")
            return
        self.json_out(out)

    def get_apply_result(self):
        if APPLY_STATE.get("error"):
            cfg = self.cfg_or_none()
            self.page(self.error_body("删除执行失败", APPLY_STATE["error"]) +
                      "<p class=hint>已完成的部分记录在删除日志中，不会重复删除；处理问题后可对同一计划重新执行，已删项自动跳过。</p>" +
                      f"<p><a class=btn href='{self.px}/'>返回概览</a></p>", active="home")
            return
        r = APPLY_STATE.get("result") or {}
        freed = r.get("logical_bytes", 0) / 2**20
        before = r.get("free_bytes_before", 0)
        after = r.get("free_bytes_after", 0)
        self.page("<h1>" + ICONS_S['done'] + " 清理完成</h1>"
                  f"<div class=card><h3>已删除 <b>{r.get('deleted_files', 0)}</b> 个文件，释放约 {freed:.0f} MB</h3>"
                  f"<table><tr><td>删除前剩余空间</td><td>{before / 2**30:.1f} GB</td></tr>"
                  f"<tr><td>删除后剩余空间</td><td>{after / 2**30:.1f} GB</td></tr>"
                  f"<tr><td>删除日志（每一步的详细记录）</td><td><code>{html.escape(str(r.get('journal', '')))}</code></td></tr></table>"
                  "<p class=hint>空间数字以文件系统回报为准（快照等机制可能延迟释放）。之后再次扫描，这些文件不会再出现。</p></div>"
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


PLAN_STATE = {"running": False, "scan_id": None, "plan_id": None, "error": None}


def plan_worker(cfg, scan_id, selections):
    """计划复核（逐文件 SHA+ffprobe）对大库可能持续数十分钟，放后台跑。
    结果同时持久化到 progress.json——应用重启后状态页/审阅入口仍可用。"""
    try:
        result = v.plan(cfg, scan_id, selections)
        PLAN_STATE["plan_id"] = result["plan_id"]
        PLAN_STATE["logical_bytes"] = result["logical_bytes"]
        PLAN_STATE["delete_files"] = result["delete_files"]
        PLAN_STATE["preview"] = v.apply(cfg, result["plan_id"])
        v.write_progress(cfg, {"phase": "plan_ready", "plan_id": result["plan_id"],
                               "delete_files": result["delete_files"], "logical_bytes": result["logical_bytes"],
                               "done": 1, "total": 1, "current": "", "started": ""})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        PLAN_STATE["error"] = str(e)[:500]
        v.write_progress(cfg, {"phase": "plan_error", "error": str(e)[:500],
                               "done": 0, "total": 0, "current": "", "started": ""})
    finally:
        PLAN_STATE["running"] = False


APPLY_STATE = {"running": False, "plan_id": None, "result": None, "error": None}


def apply_worker(cfg, plan_id, confirm):
    """执行删除可能持续较久（逐文件全量终检+原子暂存），放后台跑并上报进度。"""
    try:
        result = v.apply(cfg, plan_id, execute=True, confirm=confirm)
        APPLY_STATE["result"] = result
        APPLY_STATE["plan_id"] = plan_id
        v.write_progress(cfg, {"phase": "apply_done", "done": result.get("deleted_files", 0),
                               "total": result.get("deleted_files", 0), "current": "",
                               "freed": result.get("logical_bytes", 0), "started": ""})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        APPLY_STATE["error"] = str(e)[:500]
        v.write_progress(cfg, {"phase": "apply_error", "error": str(e)[:500],
                               "done": 0, "total": 0, "current": "", "started": ""})
    finally:
        APPLY_STATE["running"] = False


def scan_worker(cfg):
    try:
        v.inventory(cfg)
    except Exception as e:  # surface into progress + stderr (webui.log) for diagnosis
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        v.write_progress(cfg, {"phase": "error", "error": str(e)[:500], "scan_id": "", "done": 0, "total": 0, "current": "", "started": ""})
    finally:
        SCAN_THREAD["running"] = False


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
    server = build_server(args.config, socket_path=args.socket, tcp_port=args.tcp)
    mode = args.socket or f"tcp://{server.server_address[0]}:{server.server_address[1]}"
    print(f"videodedupe webui listening on {mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Video fingerprint scan, review, and explicitly confirmed Linux cleanup.

Native fnOS app core (fpk, python312 runtime + venv numpy/static-ffmpeg).
Python 3.12 standard library plus optional numpy for pHash. No network
requests of its own; ffmpeg/ffprobe run as local subprocesses.

Module map (see AGENTS.md): scan/fingerprint -> inventory & helpers here;
review/plan -> plan(); deletion safety -> apply() (change only with full
test suite green); install config -> fpk_setup(); web pages -> webui.py.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import fnmatch
import hashlib
import html
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

import tasks  # 任务协调（Review 26.10.12 §四B）：单表 + 跨进程操作锁

def _manifest_version():
    """单一版本来源 = fpk/manifest（Review 26.10.12 §四F），运行时解析。

    开发/源码归档布局读 fpk/manifest（本文件同级）；安装布局读随包复制到
    app/ 的 manifest（build_fpk.py 打包时写入，包内不再有 fpk/ 目录）。
    两处都没有说明布局已损坏——如实返回 0.0.0 暴露问题，不手抄版本号。
    """
    here = Path(__file__).resolve().parent
    for cand in (here / "fpk" / "manifest", here / "manifest"):
        try:
            for line in cand.read_text(encoding="utf-8").splitlines():
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return "0.0.0"


VERSION = _manifest_version()
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".vob", ".3gp", ".ogv"}
SIDECARS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
FRAME_POSITIONS = (0.1, 0.25, 0.5, 0.75, 0.9)
SIMILAR_MEAN_DISTANCE = 10  # of 64 pHash bits; report shows the real value
# 指纹缓存版本（Review 26.10.12 §四C）：pHash/缩略图算法或缓存 JSON 结构
# 变化时递增——版本进入缓存键，旧缓存自然失效并整体重算。
HASH_CACHE_VERSION = "2"
# 多帧预览缓存版本（Review 26.10.12 §四C）：预览尺寸/JSON 结构变化时递增。
PREVIEW_VERSION = "1"
EXPORT_FIELDS = ["GroupId", "Kind", "Path", "SizeBytes", "Duration", "Resolution", "BitrateKbs", "Distance", "Protected", "Checked"]

# fpk bundles numpy as unpacked wheels in app/site-packages/ — no pip, no venv.
_BUNDLED_SITE = Path(__file__).resolve().parent / "site-packages"
if _BUNDLED_SITE.is_dir() and str(_BUNDLED_SITE) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_SITE))


class Unsafe(ValueError):
    pass


class DraftConflict(Unsafe):
    """审阅草稿修订冲突（Review 26.10.12 §四D）：另一页面已保存更高修订，
    拒绝静默覆盖；current_rev 供页面提示刷新后重新加载。"""

    def __init__(self, current_rev, expect_rev):
        super().__init__(f"草稿已在其他页面被修改（当前修订 {current_rev}，本页基于 {expect_rev}）；"
                         "请刷新页面重新加载，本页选择不会被自动覆盖")
        self.current_rev = current_rev
        self.expect_rev = expect_rev


class Cancelled(Exception):
    pass


def canonical(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def inside(path: Path, root: Path):
    return path == root or root in path.parents


def no_links(path: Path):
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise Unsafe(f"拒绝符号链接路径: {part}")


def read_config(filename):
    source = Path(filename).absolute()
    cfg = json.loads(source.read_text(encoding="utf-8-sig"))
    state_dir = Path(cfg["state_dir"])
    if not state_dir.is_absolute():
        raise Unsafe("state_dir 必须是绝对路径")
    roots, seen = [], set()
    for item in cfg.get("roots", []):
        rid, raw = item.get("id", ""), item.get("path", "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", rid) or rid in seen:
            raise Unsafe("目录 id 必须唯一，由小写字母、数字、下划线或连字符组成")
        if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
            raise Unsafe("目录路径无效")
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
            raise Unsafe(f"必须使用具体的绝对目录路径: {raw}")
        no_links(path)
        role = item.get("role")
        if role not in {"reference", "cleanup"}:
            raise Unsafe("目录 role 只能是 reference 或 cleanup")
        excludes = item.get("exclude", [])
        if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
            raise Unsafe("exclude 必须是字符串数组")
        if any(r["path"] == str(path) for r in roots):
            raise Unsafe("同一个目录不能配置两次")
        roots.append({"id": rid, "path": str(path), "role": role, "exclude": excludes})
        seen.add(rid)
    if not roots:
        raise Unsafe("至少配置一个目录")
    cfg["roots"] = roots
    cfg["state_dir"] = str(state_dir.resolve())
    export_dir = cfg.get("export_dir")
    cfg["export_dir"] = str(Path(export_dir).resolve()) if export_dir else cfg["state_dir"]
    cfg.setdefault("probe", {})
    if cfg["probe"].get("mode", "local") not in {"local", None}:
        raise Unsafe("probe.mode 仅支持 local（原生应用无容器）")
    return cfg


def config_identity(cfg):
    # Host paths differ between contexts; stable IDs and roles do not.
    return digest([{k: r[k] for k in ("id", "role", "exclude")} for r in cfg["roots"]])


def owner(cfg, path):
    candidates = [r for r in cfg["roots"] if inside(path, Path(r["path"]))]
    if not candidates:
        raise Unsafe(f"路径不在授权目录内: {path}")
    return max(candidates, key=lambda r: len(Path(r["path"]).parts))


def excluded(cfg, root, path):
    if any(inside(path, Path(p)) for p in [cfg["state_dir"], *cfg.get("protected_paths", [])]):
        return True
    rel = path.relative_to(Path(root["path"])).as_posix()
    parts = [rel, *[p.as_posix() for p in PurePosixPath(rel).parents if p.as_posix() != "."]]
    return any(fnmatch.fnmatchcase(p, pattern.rstrip("/")) for p in parts for pattern in root["exclude"])


def relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Unsafe("相对路径无效")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(x in {"..", "."} for x in value.split("/")) or "\\" in value:
        raise Unsafe(f"拒绝越界或含糊路径: {value!r}")
    return rel


def file_path(cfg, item):
    root = next((r for r in cfg["roots"] if r["id"] == item["root"]), None)
    if root is None:
        raise Unsafe("计划中的目录已经移除")
    rel = relative_path(item["rel"])
    path = Path(root["path"]).joinpath(*rel.parts)
    no_links(path)
    if owner(cfg, path)["id"] != root["id"] or excluded(cfg, root, path):
        raise Unsafe("路径归属或排除规则发生变化")
    if path.suffix.lower() not in VIDEO_EXT:
        raise Unsafe("清理对象不是支持的视频文件")
    return path


def selection(value):
    """Parse 'root:rel' selection strings (first colon splits). Empty rel
    ('root:') selects the whole authorized root（真机验收 26.10.13 反馈2：
    母文件夹本身可勾选，不必逐个勾子目录）."""
    if not isinstance(value, str) or ":" not in value:
        raise Unsafe(f"选择格式必须是 目录id:相对路径: {value!r}")
    root, rel = value.split(":", 1)
    if rel:
        relative_path(rel)
    return {"root": root, "rel": rel}


def key(item):
    return item["root"] + ":" + item["rel"]


def snapshot(s):
    if not stat.S_ISREG(s.st_mode):
        raise Unsafe("对象不是普通文件")
    change_time = s.st_ctime_ns if os.name == "posix" else s.st_birthtime_ns
    return {"dev": s.st_dev, "ino": s.st_ino, "size": s.st_size, "mtime_ns": s.st_mtime_ns, "ctime_ns": change_time, "nlink": s.st_nlink}


@contextlib.contextmanager
def anchored(cfg, item):
    """Open the file with NOFOLLOW, walking down from its authorized root.

    Intermediate directories are opened O_PATH|O_NOFOLLOW: fnOS ACL grants
    give the package user traverse (x) on ancestor volumes but not list (r),
    so O_RDONLY|O_DIRECTORY on e.g. /vol7 fails with EACCES. O_PATH fds are
    valid dir_fd references; the root's identity is checked separately by
    root_identities (dev,ino)."""
    path = file_path(cfg, item)
    if os.name != "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            yield fd, None, path.name
        finally:
            os.close(fd)
        return
    flags = os.O_PATH | os.O_NOFOLLOW
    root_path = Path(next(r for r in cfg["roots"] if r["id"] == item["root"])["path"])
    parent = os.open(root_path, flags)
    fd = None
    try:
        for part in path.relative_to(root_path).parts[:-1]:
            nxt = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = nxt
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        yield fd, parent, path.name
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def fingerprint(cfg, item, expected=None):
    with anchored(cfg, item) as (fd, _, __):
        before = snapshot(os.fstat(fd))
        if expected is not None and before != expected:
            raise Unsafe(f"文件在扫描后发生变化: {key(item)}")
        sha = hashlib.sha256()
        while chunk := os.read(fd, 4 * 1024 * 1024):
            sha.update(chunk)
        if snapshot(os.fstat(fd)) != before:
            raise Unsafe(f"文件在读取过程中发生变化: {key(item)}")
    if snapshot(file_path(cfg, item).lstat()) != before:
        raise Unsafe("文件在读取后被替换")
    return before, sha.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def state(cfg, exclusive=True):
    """DB + operation lock（锁实现在 tasks.oplock，Web 与 CLI 共用同一把）。
    exclusive=True 是单写者门（scan/plan/apply）；读者传 exclusive=False——
    SQLite WAL 服务他们并发，长扫描期间绝不能 EAGAIN。"""
    with tasks.oplock(cfg, exclusive) as lock:
        db = sqlite3.connect(Path(cfg["state_dir"]) / "inventory.sqlite3")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, config TEXT, roots TEXT, created TEXT, scope TEXT, summary TEXT)")
            if "scope" not in {row[1] for row in db.execute("PRAGMA table_info(scans)")}:
                db.execute("ALTER TABLE scans ADD COLUMN scope TEXT")  # 旧库迁移：历史行 scope=NULL 即全部授权目录
            if "summary" not in {row[1] for row in db.execute("PRAGMA table_info(scans)")}:
                db.execute("ALTER TABLE scans ADD COLUMN summary TEXT")  # §四D/P2：聚合摘要持久化（首页单一来源）；NULL=旧库行，读取时按需现算
            db.execute("CREATE TABLE IF NOT EXISTS files(scan TEXT, key TEXT, record TEXT, PRIMARY KEY(scan,key))")
            db.execute("CREATE TABLE IF NOT EXISTS hashes(cache_key TEXT PRIMARY KEY, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS groups(scan TEXT, gid TEXT, kind TEXT, members TEXT, exacts TEXT, suggest TEXT, PRIMARY KEY(scan,gid))")
            if "exacts" not in {row[1] for row in db.execute("PRAGMA table_info(groups)")}:
                db.execute("ALTER TABLE groups ADD COLUMN exacts TEXT")  # 旧库迁移：NULL=读取时按 sha 现场重算（§四C）
            if "suggest" not in {row[1] for row in db.execute("PRAGMA table_info(groups)")}:
                db.execute("ALTER TABLE groups ADD COLUMN suggest TEXT")  # §四D/P2：批量建议随扫描持久化；NULL=旧库行，读取时现算
            db.execute("CREATE TABLE IF NOT EXISTS drafts(scan TEXT PRIMARY KEY, rev INTEGER NOT NULL, data TEXT NOT NULL, updated TEXT)")  # §四D：审阅草稿绑定扫描
            yield db
        finally:
            db.close()


def root_identities(cfg):
    result = {}
    for root in cfg["roots"]:
        path = Path(root["path"])
        no_links(path)
        s = path.stat()
        if not stat.S_ISDIR(s.st_mode):
            raise Unsafe(f"目录未挂载或不可访问（请在飞牛应用设置中授权目录）: {path}")
        result[root["id"]] = {"dev": s.st_dev, "ino": s.st_ino}
    return result


# --- ffmpeg / ffprobe / pHash ------------------------------------------------


def resolve_tools(cfg):
    if "_tools" not in cfg:
        override = cfg.get("probe", {})
        bundled = Path(__file__).resolve().parent / "bin"
        ffmpeg = override.get("ffmpeg") or (str(bundled / "ffmpeg") if (bundled / "ffmpeg").exists() else None) or shutil.which("ffmpeg")
        ffprobe = override.get("ffprobe") or (str(bundled / "ffprobe") if (bundled / "ffprobe").exists() else None) or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            try:
                from static_ffmpeg import add_paths
                add_paths()
                ffmpeg = ffmpeg or shutil.which("ffmpeg")
                ffprobe = ffprobe or shutil.which("ffprobe")
            except Exception:
                pass
        if not ffmpeg or not ffprobe:
            raise Unsafe("找不到 ffmpeg/ffprobe：应用包应自带 bin/ 目录，或在配置 probe 中指定路径")
        cfg["_tools"] = (ffmpeg, ffprobe)
    return cfg["_tools"]


def run_tool(args, timeout=120):
    # ponytail: single serial ffmpeg, one decode thread, low priority.
    prefix = ["nice", "-n", "19"] if os.name == "posix" and shutil.which("nice") else []
    p = subprocess.run([*prefix, *args], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise Unsafe(args[0].rsplit("/", 1)[-1] + " 失败: " + p.stderr.decode("utf-8", "replace").split("\n")[0][:200])
    return p.stdout


def probe_media(cfg, path):
    """ffprobe metadata for any file path; returns dict or raises Unsafe."""
    _, ffprobe = resolve_tools(cfg)
    out = run_tool([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    data = json.loads(out.decode("utf-8", "replace"))
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise Unsafe("没有可读取的视频流")
    duration = float(data.get("format", {}).get("duration", video.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise Unsafe("视频时长无效")
    tracks = [{"type": s["codec_type"], "codec": s.get("codec_name"), "language": s.get("tags", {}).get("language", "unknown"), "title": s.get("tags", {}).get("title", ""), "channels": s.get("channels"), "disposition": s.get("disposition", {})} for s in streams if s.get("codec_type") in {"audio", "subtitle"}]
    return {"duration": duration, "width": video.get("width"), "height": video.get("height"), "codec": video.get("codec_name"), "fps": video.get("avg_frame_rate"), "bitrate": data.get("format", {}).get("bit_rate"), "tracks": tracks}


def phash(gray):
    """64-bit perceptual hash of a 32x32 grayscale frame (1024 bytes)."""
    import numpy as np
    a = np.frombuffer(gray, dtype=np.uint8).astype(np.float64).reshape(32, 32)
    n = np.arange(32)
    k = np.arange(32).reshape(-1, 1)
    c = np.cos(np.pi * (2 * n + 1) * k / 64) * np.sqrt(np.where(k == 0, 1.0, 2.0) / 32)
    d = c @ a @ c.T
    block = d[:8, :8]
    bits = block > np.median(block)
    value = 0
    for b in bits.ravel():
        value = (value << 1) | int(b)
    return value


def hamming(a, b):
    return (a ^ b).bit_count()


def duration_window(shorter):
    """候选时长容差（Review 26.10.12 P1 统一口径）：取扫描候选侧原有的
    max(5.0, 较短时长*0.02)，不调参；计划阶段原先的 min 口径会拒绝扫描
    已发现并展示的相似组。"""
    return max(5.0, shorter * 0.02)


def pair_match(a, b):
    """扫描与计划共用的两两直接匹配判断（Review 26.10.12 P0/P1/§四C）。

    输入是 files 表里的两条成员记录（sha256/vhash/meta），返回 None 或
    匹配依据：{"basis": "sha256"}（字节完全一致）或
    {"basis": "phash", "distance": d}（时长窗口内帧均值距离达标）。
    候选容差与 pHash 阈值保持扫描侧原值；连通分组（A≈B≈C）不代表组内
    两两 pair_match 成立——删除项必须与某个保留对象直接匹配。"""
    sa, sb = a.get("sha256"), b.get("sha256")
    if sa and sb and sa == sb:
        return {"basis": "sha256"}
    va, vb = a.get("vhash"), b.get("vhash")
    if not va or not vb or a.get("meta", {}).get("error") or b.get("meta", {}).get("error"):
        return None
    da, db = a["meta"]["duration"], b["meta"]["duration"]
    if abs(da - db) > duration_window(min(da, db)):
        return None
    distance = sum(hamming(x, y) for x, y in zip(va, vb)) / len(FRAME_POSITIONS)
    return {"basis": "phash", "distance": round(distance, 1)} if distance <= SIMILAR_MEAN_DISTANCE else None


def exact_subgroups(members, stored=None):
    """完全重复（SHA）子组：连通候选组内字节一致(≥2)的成员 key 列表。

    stored 是分组入库时的值（旧库行为 NULL，按同一分桶规则现场重算）；
    exact 组全员同 sha，分桶结果自然是含全组的单个子组。"""
    if stored is not None:
        return stored
    buckets = {}
    for m in sorted(members, key=key):
        if m.get("sha256"):
            buckets.setdefault(m["sha256"], []).append(key(m))
    return sorted((ks for ks in buckets.values() if len(ks) > 1), key=lambda ks: ks[0])


def frame_gray(cfg, path, position):
    ffmpeg, _ = resolve_tools(cfg)
    out = run_tool([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{position:.3f}", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-",
                    "-threads", "1"], timeout=180)
    if len(out) != 1024:
        raise Unsafe("帧提取失败（输出尺寸异常）")
    return out


def thumbnail(cfg, path, position, name, folder="thumbs", width=320):
    ffmpeg, _ = resolve_tools(cfg)
    out = run_tool([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{position:.3f}", "-i", str(path),
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg",
                    "-", "-threads", "1"], timeout=180)
    if not out.startswith(b"\xff\xd8"):
        raise Unsafe("缩略图生成失败")
    target = Path(cfg["state_dir"]) / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(out)
    return name


def preview_cache_stem(cfg, item):
    """预览缓存键（§四C）：文件身份（path + stat）与 PREVIEW_VERSION 共同决定，
    文件替换后自然失效。GET /preview 的只读缓存查询与 preview_frames 的生成
    落盘共用同一身份，读到的与生成的永远是同一份。"""
    path = file_path(cfg, item)
    return digest({"v": PREVIEW_VERSION, "path": str(path), "stat": snapshot(path.lstat())})[:32]


def cached_preview_frames(cfg, item):
    """只读预览缓存（§四C）：返回已生成的帧清单（[{pos,t,img}] 子集），
    未生成或缓存损坏返回空列表。绝不触发 ffmpeg——生成只经 preview_frames
    （Web 侧 POST /preview/generate）。"""
    meta = Path(cfg["state_dir"]) / "previews" / (preview_cache_stem(cfg, item) + ".json")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return [f for f in data if isinstance(f, dict) and "pos" in f and "img" in f]
    except (OSError, ValueError, TypeError):
        return []  # 缓存损坏按未生成处理，由显式生成入口重建


def preview_frames(cfg, item, duration):
    """按需多帧预览（Review 26.10.12 §三.3/§四C）：生成入口（Web 侧仅
    POST /preview/generate；GET /preview 只读 cached_preview_frames）。

    在 FRAME_POSITIONS 五个相对位置各抽一帧（640px JPEG），每帧实际时间
    t=duration*pos 随图返回供界面显示。私有缓存在 state_dir/previews/，
    键 = preview_cache_stem（PREVIEW_VERSION + path + stat）——文件替换后
    自然失效重生成。单帧失败降级为该帧的 error 项并保留其余帧（§四C 能力
    分级）；缓存只收成功帧，失败帧下次调用按未命中重试。duration 由调用方
    取扫描记录的 meta.duration；t 是 -ss 请求位置换算的绝对秒数，不回读帧
    真实 PTS。
    # ponytail: 五帧各自独立串行调用 run_tool（nice + -threads 1），不合并
    filter_complex 多路输出——复用既有低优先级单路约定优先于省四次进程。"""
    if not math.isfinite(duration) or duration <= 0:
        raise Unsafe("视频时长无效")
    path = file_path(cfg, item)
    stem = preview_cache_stem(cfg, item)
    meta = Path(cfg["state_dir"]) / "previews" / (stem + ".json")  # 帧清单缓存
    cached = {f["pos"]: f for f in cached_preview_frames(cfg, item)}
    frames = []
    for i, pos in enumerate(FRAME_POSITIONS):
        if pos in cached:
            frames.append(cached[pos])
            continue
        try:
            frames.append({"pos": pos, "t": round(duration * pos, 3),
                           "img": thumbnail(cfg, path, duration * pos, f"{stem}-{i}.jpg",
                                            folder="previews", width=640)})
        except (Unsafe, OSError, ValueError, subprocess.SubprocessError) as e:
            frames.append({"pos": pos, "error": str(e)[:240]})
    meta.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(meta, frames)
    return frames


def visual_fingerprint(cfg, path, duration):
    try:
        return [phash(frame_gray(cfg, path, duration * p)) for p in FRAME_POSITIONS]
    except ImportError:
        raise Unsafe("numpy 未安装，无法计算视觉指纹（应用依赖安装不完整）")


def fingerprint_cached(cfg, db, item, want_sha):
    """Probe + pHash (+ optional sha256) with (path,stat) keyed cache.

    Review 26.10.12 P1/§四C 能力分级：元数据/视觉指纹/缩略图各自独立降级，
    抽帧或缩略图失败不抹掉已读到的元数据。失败（*_error）只留在本次返回
    值供展示，不写入持久缓存——重扫时按未命中重试失败能力；成功且版本
    一致（HASH_CACHE_VERSION 进缓存键）的部分继续复用。The cache key
    uses the file's *current* stat — if it differs from the recorded one
    (replaced / edited since scan), refuse instead of serving a stale hash."""
    path = file_path(cfg, item)
    current = snapshot(path.lstat())
    expected = item.get("stat")
    if expected is not None and current != expected:
        raise Unsafe(f"文件在复核时发生变化: {key(item)}")
    cache_key = digest({"v": HASH_CACHE_VERSION, "path": str(path), "stat": current})
    cached = db.execute("SELECT result FROM hashes WHERE cache_key=?", (cache_key,)).fetchone()
    result = json.loads(cached[0]) if cached else {}
    changed = cached is None
    duration = result["meta"].get("duration") if "meta" in result else None
    if "meta" not in result:
        try:
            result["meta"] = probe_media(cfg, path)
            duration = result["meta"]["duration"]
        except (Unsafe, OSError, ValueError, subprocess.SubprocessError) as e:
            result["meta_error"] = str(e)[:240]
        changed = True
    # 抽帧/缩略图依赖 probe 得到的时长；元数据失败时无从取帧，不空转重试。
    if "vhash" not in result and duration is not None:
        try:
            result["vhash"] = visual_fingerprint(cfg, path, duration)
        except (Unsafe, OSError, ValueError, subprocess.SubprocessError) as e:
            result["vhash_error"] = str(e)[:240]
        changed = True
    if "thumb" not in result and duration is not None:
        try:
            result["thumb"] = thumbnail(cfg, path, duration * FRAME_POSITIONS[2],
                                        cache_key[:32] + ".jpg")
        except (Unsafe, OSError, ValueError, subprocess.SubprocessError) as e:
            result["thumb_error"] = str(e)[:240]
        changed = True
    if want_sha and "sha" not in result:
        _, result["sha"] = fingerprint(cfg, item, item["stat"])
        changed = True
    if changed:
        stored = {k: value for k, value in result.items() if not k.endswith("_error")}
        db.execute("INSERT OR REPLACE INTO hashes VALUES(?,?)", (cache_key, canonical(stored)))
        db.commit()
    return result


# --- progress / cancel (lock-free files for the webui) ------------------------


def progress_path(cfg):
    return Path(cfg["state_dir"]) / "progress.json"


def cancel_path(cfg):
    return Path(cfg["state_dir"]) / "cancel.flag"


def write_progress(cfg, data):
    with contextlib.suppress(OSError):
        atomic_json(progress_path(cfg), data)
    # 任务侧镜像（Review 26.10.12 §四B）：cfg 带 _task_id（任务已被占用）时把
    # 阶段/进度同步进任务行；CLI 直跑（无任务上下文）行为不变。
    tasks.reflect(cfg, data)


def read_progress(cfg):
    try:
        return json.loads(progress_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_cancel(cfg):
    if cancel_path(cfg).exists():
        raise Cancelled()


# --- scan ---------------------------------------------------------------------


def scan_scope(cfg):
    """Explicit scan scope bound to root identity: a list of {root, rel} items.

    Accepts cfg['scan_scope'] as 'root:rel' strings (selection format) or
    {root, rel} dicts; missing/empty -> [] meaning every authorized root in
    full. rel='' selects the whole root（真机验收 26.10.13 反馈2：母文件夹
    本身可勾选——选中根后该根的其余子选择并入根）. Rejects unknown root
    ids, absolute/'..'/'\\' rels and symlinked targets; drops child
    selections already covered by an ancestor under the same root.
    Directories that do not exist yet are allowed here and skipped
    (with a warning) by inventory()."""
    scope = cfg.get("scan_scope")
    if not scope:
        return []
    by_root, whole = {}, set()
    for value in scope:
        item = value if isinstance(value, dict) else selection(value)
        root = next((r for r in cfg["roots"] if r["id"] == item["root"]), None)
        if root is None:
            raise Unsafe("扫描范围引用了未授权的目录: " + item["root"])
        if item["rel"]:
            by_root.setdefault(root["id"], set()).add(relative_path(item["rel"]))
        else:  # 空 rel = 整个授权根
            whole.add(root["id"])
    result = []
    for root in cfg["roots"]:  # 按 cfg 顺序输出，结果稳定可复现
        if root["id"] in whole:
            path = Path(root["path"])
            no_links(path)
            result.append({"root": root["id"], "rel": "", "path": path})
            continue  # 根已选，该根的子选择全部并入
        kept = []
        for rel in sorted(by_root.get(root["id"], ()), key=lambda p: p.parts):
            if any(k in rel.parents for k in kept):
                continue  # 同一根下父目录已选，子选择并入父
            path = Path(root["path"]).joinpath(*rel.parts)
            no_links(path)  # 拒绝符号链接（含中间目录链）
            kept.append(rel)
            result.append({"root": root["id"], "rel": rel.as_posix(), "path": path})
    return result


def inventory(cfg):
    """扫描入口接入任务协调（§四B）：收养 webui 已占用的任务或由 CLI 现场占用，
    结束后把完整结果/错误落进任务行；被占用时抛 tasks.Busy。"""
    with tasks.run(cfg, "scan") as task:
        task["result"] = _inventory(cfg)
        return task["result"]


def _inventory(cfg):
    roots = root_identities(cfg)
    scope = scan_scope(cfg)
    scan_id = uuid.uuid4().hex
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cancel_path(cfg).unlink(missing_ok=True)
    with state(cfg) as db:
        files, seen, warnings = [], set(), []
        effective = []  # 实际生效的扫描范围（存在且可遍历的起点），随结果入库
        fingerprint_errors = []
        skipped = set()
        stopped_at = "discover"  # 取消时所在阶段（§三.2：结束状态给位置与数量）
        processed = 0            # 取消时指纹阶段已处理数
        last_tick = [0.0]

        def tick(data):
            # discover/grouping 进度节流写盘（0.5s）：atomic_json 带 fsync，大目录
            # 逐条写会拖慢遍历。指纹阶段仍逐文件写（行为不变）。
            now = time.monotonic()
            if now - last_tick[0] >= 0.5:
                last_tick[0] = now
                write_progress(cfg, data)

        # 真机验收 26.10.13 反馈1：扫描中的实时指标与审阅预览。live_sha 只收
        # 已算出 SHA 的文件（只有同尺寸文件才会算，字节重复必然同尺寸）；
        # 初步建议与 suggest_members 同口径（受保护优先保留，否则路径序第一
        # 份），但不做外挂字幕/元数据异常检查——数字是已识别部分的初步值，
        # 终值以扫描完成为准（页面按「初步/扫描结束后可能调整」如实标注；
        # progress 字段名与数值口径不变，措辞去承诺化只在呈现层）。
        reference_ids = {r["id"] for r in cfg["roots"] if r["role"] == "reference"}
        root_paths = {r["id"]: r["path"] for r in cfg["roots"]}
        live_sha = {}
        live_cache = [0.0, (0, 0, 0)]

        def live_metrics(force=False):
            now = time.monotonic()
            if force or now - live_cache[0] >= 0.5:
                groups_n = clean_n = freed_n = 0
                for members in live_sha.values():
                    if len(members) < 2:
                        continue
                    groups_n += 1
                    protected = {id(m) for m in members
                                 if m["root"] in reference_ids or m["stat"]["nlink"] > 1}
                    if protected:
                        drop = [m for m in members if id(m) not in protected]
                    else:
                        ordered = sorted(members, key=lambda m: (root_paths[m["root"]], m["rel"]))
                        drop = ordered[1:]
                    clean_n += len(drop)
                    freed_n += sum(m["stat"]["size"] for m in drop)
                live_cache[0] = now
                live_cache[1] = (groups_n, clean_n, freed_n)
            return live_cache[1]

        flushed = [0]
        last_flush = [0.0]

        def flush_live(force=False):
            # 节流落库（≥2s，或收尾强制）：files 行按序增量补插（指纹循环按
            # files 顺序处理，切片即新完成的部分），groups 全量重建为当前已知
            # 的完全重复组并附初步建议，供审阅页扫描期间只读预览（live_groups/
            # live_files 专用通道读取；无 scans 行，不与完成态混淆）。
            now = time.monotonic()
            if not force and now - last_flush[0] < 2.0:
                return
            last_flush[0] = now
            db.executemany("INSERT OR IGNORE INTO files VALUES(?,?,?)",
                           [(scan_id, key(f), canonical(f)) for f in files[flushed[0]:processed]])
            flushed[0] = processed
            rows = []
            for sha, members in live_sha.items():
                if len(members) < 2:
                    continue
                members = sorted(members, key=key)
                protected = {id(m) for m in members
                             if m["root"] in reference_ids or m["stat"]["nlink"] > 1}
                if protected:
                    keep_map = {key(m): "受保护副本（初步建议，扫描完成后复核）" for m in members if id(m) in protected}
                    drop = [m for m in members if id(m) not in protected]
                else:
                    keep_map = {key(members[0]): "无受保护副本，按路径顺序保留第一份（初步建议）"}
                    drop = members[1:]
                keys = [key(m) for m in members]
                rows.append((scan_id, "exact:" + sha, "exact", canonical(keys), canonical([keys]),
                             canonical({"clean": sorted(key(m) for m in drop), "keep": keep_map,
                                        "skipped": [], "manual": []})))
            db.execute("DELETE FROM groups WHERE scan=?", (scan_id,))
            db.executemany("INSERT INTO groups(scan,gid,kind,members,exacts,suggest) VALUES(?,?,?,?,?,?)", rows)
            db.commit()

        def _stopped():
            # 统一的停止收尾：walk/指纹/分组三处检查点共用（§三.2 已停止≠失败）。
            write_progress(cfg, {"scan_id": scan_id, "phase": "cancelled", "stopped_at": stopped_at,
                                 "done": processed, "total": len(files) if stopped_at != "discover" else 0,
                                 "found": len(files), "current": "", "started": started})
            cancel_path(cfg).unlink(missing_ok=True)
            # §三.2 措辞纪律：缓存键是 (path, stat)，未变文件命中缓存、变化文件
            # 重算——如实写「复用已有识别结果」，不称精确断点续跑。
            summary = {"scan_id": scan_id, "cancelled": True, "stopped_at": stopped_at,
                       "found": len(files), "processed": processed,
                       "note": "扫描已停止；已成功计算的识别结果保留在缓存中，再次扫描会复用已有识别结果"}
            # 实时预览的临时行随取消清理（无 scans 行的孤儿数据不留在库里；
            # 识别缓存 hashes 表不受影响，重扫照常复用）
            db.execute("DELETE FROM files WHERE scan=?", (scan_id,))
            db.execute("DELETE FROM groups WHERE scan=?", (scan_id,))
            db.commit()
            tasks.finish(cfg, tasks.PARTIAL, result=summary, error="用户请求停止扫描")  # 已停止≠成功：任务记部分完成
            return summary

        try:
            # 阶段起点强制写一次（tick 此后节流），保证 progress.json 相位连贯
            write_progress(cfg, {"scan_id": scan_id, "phase": "discover", "found": 0,
                                 "done": 0, "total": 0, "current": "", "started": started})
            for root in cfg["roots"]:
                start_points = []
                if scope:
                    # 只扫绑定到该根的勾选子目录；未出现的根本轮不扫
                    for entry in scope:
                        if entry["root"] != root["id"]:
                            continue
                        if entry["path"].is_dir() and not entry["path"].is_symlink():
                            start_points.append(entry["path"])
                            effective.append({"root": entry["root"], "rel": entry["rel"]})
                        else:
                            warnings.append("扫描范围目录不存在，已跳过: " + entry["rel"])
                else:
                    start_points.append(Path(root["path"]))
                for start in start_points:
                    def walk_error(error):
                        # 读不了的子目录（如系统缩略图目录）：跳过并记录，不中止扫描。
                        warnings.append("跳过不可读目录: " + str(error))
                    for folder, dirs, names in os.walk(start, followlinks=False, onerror=walk_error):
                        check_cancel(cfg)  # 目录遍历阶段响应停止（§三.2）
                        # 阶段一「查找视频」：总数未知只报已发现数，不伪造百分比
                        tick({"scan_id": scan_id, "phase": "discover", "found": len(files),
                              "done": 0, "total": 0, "current": str(folder), "started": started})
                        base = Path(folder)
                        dirs[:] = [n for n in dirs if not (base / n).is_symlink() and not excluded(cfg, owner(cfg, base / n), base / n)]
                        for name in names:
                            check_cancel(cfg)  # 单目录海量文件时停止也不迟缓
                            path = base / name
                            if path.name.startswith(".vd-delete-") or path.suffix.lower() not in VIDEO_EXT or path.is_symlink() or str(path) in seen:
                                continue
                            actual = owner(cfg, path)
                            if excluded(cfg, actual, path):
                                continue
                            item = {"root": actual["id"], "rel": path.relative_to(Path(actual["path"])).as_posix()}
                            try:
                                file_path(cfg, item)
                                item["stat"] = snapshot(path.lstat())
                                files.append(item)
                                seen.add(str(path))
                            except Unsafe:
                                raise  # 越界/符号链接属于安全信号，仍拒绝
                            except OSError as e:
                                warnings.append(f"跳过不可读文件: {e}")
            # 阶段二「识别内容」：total 至此已知，done/total 是真实进度
            write_progress(cfg, {"scan_id": scan_id, "phase": "fingerprint", "done": 0, "total": len(files), "current": "", "started": started})
            buckets = {}
            for item in files:
                buckets.setdefault(item["stat"]["size"], []).append(item)
            dup_sizes = {size for size, bucket in buckets.items() if len(bucket) > 1}
            stopped_at = "fingerprint"
            for done, item in enumerate(files, 1):
                check_cancel(cfg)
                processed = done
                try:
                    result = fingerprint_cached(cfg, db, item, item["stat"]["size"] in dup_sizes)
                except (Unsafe, OSError) as e:
                    # §四C 单文件降级：扫描中消失/被替换的文件在处理当口就被发现
                    # （fingerprint_cached 逐文件 stat 复核），只从本轮剔除并记录，
                    # 不整轮失败——原先末尾的全量批量 re-stat 已由这一步取代（P2）。
                    # 越界/挂载类安全信号不在本路径——walk 阶段已拒，循环后的
                    # root_identities 复查仍立即停止。
                    warnings.append(f"跳过指纹阶段变化的文件: {key(item)}: {str(e)[:120]}")
                    skipped.add(key(item))
                    continue
                item["sha256"] = result.get("sha")
                item["meta"] = result.get("meta") or {"error": result.get("meta_error", "元数据读取失败")}
                item["vhash"] = result.get("vhash")
                item["thumb"] = result.get("thumb")
                if item["sha256"]:
                    live_sha.setdefault(item["sha256"], []).append(item)
                if item["meta"].get("error"):
                    fingerprint_errors.append(key(item))
                g_n, c_n, b_n = live_metrics(force=done == len(files))  # 末文件强制重算，小库不被节流卡旧值
                write_progress(cfg, {"scan_id": scan_id, "phase": "fingerprint", "done": done, "total": len(files),
                                     "current": key(item), "started": started,
                                     "found": len(files), "dup_groups": g_n, "dup_clean": c_n, "dup_bytes": b_n})
                flush_live()
            # 被跳过的文件缺指纹数据，无从参与分组，从本轮剔除（下轮按新 stat 重扫）。
            if skipped:
                files = [item for item in files if key(item) not in skipped]
            if root_identities(cfg) != roots:
                raise Unsafe("扫描期间挂载目录发生变化")
            stopped_at = "grouping"
        except Cancelled:
            return _stopped()
        # One union-find over all files: SHA equality is hard evidence, pHash
        # similarity (duration-compatible pairs) adds soft links. A cluster of
        # identical SHAs is "exact"; anything mixed (e.g. original + copy +
        # transcode) is "visual" so the transcode can join its original. 连通
        # 只是候选集合（§四C）：字节一致的成员另存完全重复子组，删除决策以
        # pair_match 的直接匹配为准，不依赖组内两两相似。
        parent = list(range(len(files)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by_sha = {}
        for i, item in enumerate(files):
            if item["sha256"]:
                by_sha.setdefault(item["sha256"], []).append(i)
        for indexes in by_sha.values():
            for j in indexes[1:]:
                union(indexes[0], j)
        visual_ids = [i for i, item in enumerate(files) if item.get("vhash") and not item["meta"].get("error")]
        # ponytail: O(k^2) inside the duration window per bucket; fine for
        # personal libraries, revisit if a window ever exceeds ~20k files.
        # 匹配判断统一走 pair_match（Review 26.10.12 P0/P1）：扫描与计划同一
        # 口径，扫描展示过的组不会再在计划阶段被另一套时长容差拒绝。
        ordered = sorted(visual_ids, key=lambda i: files[i]["meta"]["duration"])
        g_n, c_n, b_n = live_metrics()  # 分组阶段 SHA 已定，携带缓存值避免轮询清零
        # 阶段起点强制写一次（此后 tick 节流），保证相位序列在 progress.json 连贯
        write_progress(cfg, {"scan_id": scan_id, "phase": "grouping", "done": 0, "total": len(ordered),
                             "current": "", "started": started, "found": len(files),
                             "dup_groups": g_n, "dup_clean": c_n, "dup_bytes": b_n})
        try:
            for ai in range(len(ordered)):
                check_cancel(cfg)  # 分组循环同样响应停止（§三.2）
                # 阶段三「整理结果」：两两比对有序窗口，进度为主指针位置
                tick({"scan_id": scan_id, "phase": "grouping", "done": ai + 1, "total": len(ordered),
                      "current": "", "started": started, "found": len(files),
                      "dup_groups": g_n, "dup_clean": c_n, "dup_bytes": b_n})
                for bi in range(ai + 1, len(ordered)):
                    a, b = files[ordered[ai]], files[ordered[bi]]
                    if b["meta"]["duration"] - a["meta"]["duration"] > duration_window(a["meta"]["duration"]):
                        break  # 有序窗口剪枝，与 pair_match 的时长条件同一公式
                    if find(ordered[ai]) == find(ordered[bi]):
                        continue
                    if pair_match(a, b):
                        union(ordered[ai], ordered[bi])
        except Cancelled:
            return _stopped()
        clusters = {}
        for i in range(len(files)):
            clusters.setdefault(find(i), []).append(files[i])
        groups = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            shas = {m["sha256"] for m in members}
            kind = "exact" if None not in shas and len(shas) == 1 else "visual"
            gid = "exact:" + next(iter(shas)) if kind == "exact" else "visual:" + digest(sorted(key(m) for m in members))[:16]
            members = sorted(members, key=key)
            groups.append({"gid": gid, "kind": kind, "members": members, "exacts": exact_subgroups(members)})
        # 批量建议随扫描结果持久化（Review 26.10.12 §四D/P2「首页重复计算建议」
        # 收口）：建议含外挂字幕目录检查的 IO，扫描时算一次入库，首页指标与审阅
        # 页建议读同一份持久数据，不再每次页面访问现算。
        suggest_clean_total = reclaim_total = 0
        for g in groups:
            g["suggest"] = suggest_members(cfg, g["members"], g["exacts"])
            by_key = {key(m): m for m in g["members"]}
            suggest_clean_total += len(g["suggest"]["clean"])
            reclaim_total += sum(by_key[k]["stat"]["size"] for k in g["suggest"]["clean"])
        # 实时预览的临时行先清再写完整结果（含视觉相似组与正式建议），
        # 完成态与预览态在库里永不混存。
        db.execute("DELETE FROM files WHERE scan=?", (scan_id,))
        db.execute("DELETE FROM groups WHERE scan=?", (scan_id,))
        db.executemany("INSERT INTO files VALUES(?,?,?)", [(scan_id, key(f), canonical(f)) for f in files])
        db.executemany("INSERT INTO groups(scan,gid,kind,members,exacts,suggest) VALUES(?,?,?,?,?,?)",
                       [(scan_id, g["gid"], g["kind"], canonical([key(m) for m in g["members"]]),
                         canonical(g["exacts"]), canonical(g["suggest"])) for g in groups])
        # §三.2 结束状态：完成 / 完成但有跳过（skipped_total>0）由同一 summary
        # 携带数量、样本与原因，供概览页区分呈现；§四D/P2 聚合指标（groups/
        # candidates/reclaim）随 scans 行持久化——首页摘要的单一来源。
        summary = {"scan_id": scan_id, "files": len(files),
                   "groups": len(groups),
                   "exact_groups": sum(g["kind"] == "exact" for g in groups),
                   "visual_groups": sum(g["kind"] == "visual" for g in groups),
                   "candidates": suggest_clean_total, "reclaim": reclaim_total,
                   "scan_warnings": len(warnings), "scan_warning_samples": warnings[:5],
                   "fingerprint_errors": len(fingerprint_errors), "fingerprint_error_samples": fingerprint_errors[:5],
                   "skipped_files": len(skipped), "skipped_samples": sorted(skipped)[:5],
                   "skipped_total": len(warnings) + len(fingerprint_errors)}
        db.execute("INSERT INTO scans(id,config,roots,created,scope,summary) VALUES(?,?,?,?,?,?)",
                   (scan_id, config_identity(cfg), canonical(roots), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    canonical(effective) if scope else None, canonical(summary)))
        db.commit()
        write_progress(cfg, {"scan_id": scan_id, "phase": "done", "done": len(files), "total": len(files),
                             "current": "", "started": started, "summary": summary})
    # §四F 报告导出独立操作：结果入库即业务成功，审计报告改由 CLI export /
    # Web POST /report 显式生成——报告写入失败不再把已完成的扫描记为失败。
    return summary


def scan_guard(db, cfg, scan_id):
    """扫描行身份校验（load_scan/load_files/load_draft/scan_metrics 共用）：
    编号形状必须合法；扫描时的每个授权根现在仍须存在且挂载身份一致——
    真机验收 26.10.13：新增授权目录不作废旧扫描的查看（用户加了文件夹，
    原有审阅记录仍应可读）；移除/替换任一扫描时的根才拒绝（旧选择与旧
    结果都不可再用，§四D「不自动继承」由这同一道门守住）。scans.config
    仍写入全量身份留档，不再参与判定。计划与执行之间的一致性由 plan 文档
    签名 + apply 的 config/挂载/stat/SHA 终检独立保证，不依赖本函数。"""
    if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise Unsafe("扫描编号格式不正确")
    row = db.execute("SELECT roots FROM scans WHERE id=?", (scan_id,)).fetchone()
    current = root_identities(cfg)
    if not row or any(current.get(rid) != ident for rid, ident in json.loads(row[0]).items()):
        raise Unsafe("扫描不存在，或扫描时的授权目录已被移除/替换（新增授权不影响查看），必须重新扫描")


def load_scan(db, cfg, scan_id):
    scan_guard(db, cfg, scan_id)
    return {k: json.loads(record) for k, record in db.execute("SELECT key,record FROM files WHERE scan=?", (scan_id,))}


def load_files(db, cfg, scan_id, keys):
    """按需加载扫描文件记录子集（Review 26.10.12 §四D/P2 审阅分页）：万级结果
    只取本页组成员，不再每次审阅全量载入；身份校验与 load_scan 同源。"""
    scan_guard(db, cfg, scan_id)
    return _fetch_files(db, scan_id, keys)


def _fetch_files(db, scan_id, keys):
    out, ordered = {}, sorted(set(keys))
    for i in range(0, len(ordered), 200):  # IN 子句分块，防超 SQLite 变量上限
        chunk = ordered[i:i + 200]
        sql = "SELECT key,record FROM files WHERE scan=? AND key IN (%s)" % ",".join("?" * len(chunk))
        out.update({k: json.loads(r) for k, r in db.execute(sql, (scan_id, *chunk))})
    return out


def live_groups(db, scan_id):
    """进行中扫描的初步分组（真机验收 26.10.13 反馈1：审阅页扫描期间实时
    可看）。只含完全重复组——SHA 字节一致是硬证据，画面相似组要等分组阶段
    完成才作数；建议为扫描中节流落库的初步口径。无 scans 行（完成才写），
    不走 scan_guard；调用方只对 progress.json 指出的运行中 scan_id 使用，
    并按只读预览呈现。"""
    if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise Unsafe("扫描编号格式不正确")
    return load_groups(db, scan_id)


def live_files(db, scan_id, keys):
    """进行中扫描的文件记录按需加载（live 通道，同 live_groups 的使用约束；
    与 load_files 的差别只是不做 scans 行身份校验）。"""
    if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise Unsafe("扫描编号格式不正确")
    return _fetch_files(db, scan_id, keys)


def load_groups(db, scan_id):
    return [{"gid": gid, "kind": kind, "members": json.loads(members),
             "exacts": json.loads(exacts) if exacts else None,   # 旧库行 NULL：消费侧 exact_subgroups 重算
             "suggest": json.loads(suggest) if suggest else None}  # 旧库行 NULL：消费侧 suggest_members 现算
            for gid, kind, members, exacts, suggest in
            db.execute("SELECT gid,kind,members,exacts,suggest FROM groups WHERE scan=? ORDER BY kind,gid", (scan_id,))]


def load_scope(db, scan_id):
    """Scope actually scanned for a stored result: None = all authorized roots
    in full (also true for rows written before the scope column existed);
    a list of {root, rel} items means exactly those directories, rel='' being
    the whole authorized root."""
    row = db.execute("SELECT scope FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


# --- review / plan / export ----------------------------------------------------


def display_path(cfg, root_id, rel):
    """报告里的用户可见路径（Review 26.10.12 P2）：授权目录真实位置拼相对
    路径。替代容器时代的 /media/<root_id>/ 前缀——root id 是路径哈希，用户
    在 NAS 文件管理器中无从对应。"""
    root = next(r for r in cfg["roots"] if r["id"] == root_id)
    return f"{root['path']}/{rel}"


def bitrate_kbs(meta):
    """ffprobe 的 bit_rate 原值单位是 bit/s（字符串）；BitrateKbs 列名承诺
    Kb/s——÷1000 换算。缺失或异常值返回 None，由导出层写空，不伪装成 0。"""
    try:
        return round(int(meta.get("bitrate")) / 1000, 1)
    except (TypeError, ValueError):
        return None


def export_audit(cfg, scan_id):
    """Write the human-readable audit report (CSV + HTML) into export_dir."""
    with state(cfg, exclusive=False) as db:
        captured = load_scan(db, cfg, scan_id)
        groups = load_groups(db, scan_id)
    rows, details = [], {}
    for g in groups:
        members = [captured[k] for k in g["members"]]
        exacts = exact_subgroups(members, g.get("exacts"))
        # ponytail: 组内两两核对 O(k^2)；审计导出非交互路径，个人库组规模可接受。
        fully_pairwise = all(pair_match(x, y) for i, x in enumerate(members) for y in members[i + 1:])
        relation = "组内两两直接匹配" if fully_pairwise else "连通候选集合：存在不直接匹配的成员对（A≈B≈C 不代表 A≈C）"
        for item in members:
            root = next(r for r in cfg["roots"] if r["id"] == item["root"])
            # 直接匹配（§四C）：取组内匹配依据最强的一份（sha 优先、距离最近）
            # 并注明对象；清理项必须与其保留对象直接匹配，审计在扫描阶段就
            # 展示谁与谁匹配，用户可提前判断哪些清理选择不可处理。
            best = None
            for other in members:
                if other is item:
                    continue
                basis = pair_match(item, other)
                if basis and (best is None or basis.get("distance", 0) < best[0].get("distance", 0)):
                    best = (basis, other)
            distance = best[0].get("distance", 0) if best else "-"
            protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
            meta = item["meta"]
            path = display_path(cfg, item["root"], item["rel"])
            # §四F 混合组成员可能缺视觉指纹（抽帧/元数据失败，仅凭字节一致或
            # 传递关系入组）：导出不崩溃，逐成员如实标注缺失，不伪装成可比对。
            visual = item.get("vhash")
            rows.append({"GroupId": g["gid"], "Kind": g["kind"], "Path": path,
                         "SizeBytes": item["stat"]["size"],
                         "Duration": round(meta["duration"], 3) if meta.get("duration") else None,
                         "Resolution": f"{meta.get('width') or '?'}x{meta.get('height') or '?'}",
                         "BitrateKbs": bitrate_kbs(meta), "Distance": distance,
                         "Protected": protected, "Checked": "False"})
            details[path] = {"类型": "字节完全重复" if g["kind"] == "exact" else "视觉相似候选，需人工判断",
                             "组内关系": relation,
                             "pHash 距离(直接匹配)": distance,
                             "视觉指纹": "已计算" if visual else "缺失：该文件未能计算视觉指纹，不参与画面比对（如需比对请重新扫描）",
                             "完全重复子组": "是" if any(key(item) in sub for sub in exacts) else "否",
                             "直接匹配说明": (f"与 {key(best[1])} 直接匹配（{'字节一致' if best[0]['basis'] == 'sha256' else '画面达标'}）"
                                              if best else "仅经传递关系入组，与组内成员均无直接匹配"),
                             "硬链接数量": item["stat"]["nlink"],
                             "音轨字幕": [t for t in meta.get("tracks", [])] if not meta.get("error") else meta.get("error"),
                             "protected": protected}
    directory = Path(cfg["export_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{scan_id}-audit.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    html_path = directory / f"{scan_id}-audit.html"
    write_review(html_path, f"扫描 {scan_id[:8]} 审计报告（{len(groups)} 组）", rows, details)
    return {"report": str(html_path), "csv": str(csv_path)}


def write_review(path, title, rows, details=None):
    details = details or {}
    cells = []
    for i, row in enumerate(rows):
        info = details.get(row["Path"], {})
        fields = [row.get(k, "") for k in ("GroupId", "Path", "SizeBytes", "Duration", "Resolution", "Distance")]
        # 元数据缺失（None）显示为 "-"，不把 Python 字面量漏到页面上
        shown = ["-" if x is None else x for x in fields]
        cells.append("<tr>" + "".join("<td>" + html.escape(str(x)) + "</td>" for x in shown) + "<td><pre>" + html.escape(json.dumps(info, ensure_ascii=False)) + "</pre></td></tr>")
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>视频查重审计</title>
<style>body{font:15px system-ui;margin:24px;color:#17202a}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:8px;text-align:left;word-break:break-all}pre{white-space:pre-wrap;font-size:12px}</style>
<h1>""" + html.escape(title) + """</h1><p>审计导出：字节完全重复组与视觉相似候选。视觉相似不是字节一致性的证明；本报告不执行任何删除。</p>
<table><thead><tr><th>分组</th><th>NAS 路径</th><th>大小（字节）</th><th>时长（秒）</th><th>分辨率</th><th>pHash 距离</th><th>核对信息</th></tr></thead><tbody>""" + "".join(cells) + """</tbody></table>"""
    Path(path).write_text(page, encoding="utf-8")


def browse_dirs(cfg, root_id, rel=""):
    """List subdirectories under an authorized root for the UI tree picker.

    Returns [{'name','rel'}] sorted; unreadable dirs surface as OSError to
    the caller (UI shows '无法读取'). Only directory names are exposed."""
    root = next((r for r in cfg["roots"] if r["id"] == root_id), None)
    if root is None:
        raise Unsafe("未知的目录 id")
    rel_path = relative_path(rel) if rel else PurePosixPath(".")
    base = Path(root["path"]).joinpath(*rel_path.parts) if rel else Path(root["path"])
    no_links(base)
    result = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        child_rel = entry.relative_to(Path(root["path"])).as_posix()
        if excluded(cfg, root, entry):
            continue
        result.append({"name": entry.name, "rel": child_rel})
    return result


def external_subtitles(cfg, item):
    """List same-stem sidecar subtitle filenames next to the item.

    计划复核与批量建议（suggestions）共用同一口径；目录读取失败抛 OSError，
    由调用方决定降级方式。"""
    fp = file_path(cfg, item)
    return sorted(p.name for p in fp.parent.iterdir() if p.suffix.lower() in SIDECARS and p.stem == fp.stem)


def fmt_dur(seconds):
    """时长显示 1:23:45 / 12:34；异常值返回 "?"。比较页与帧时间标注共用。"""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_fps(rate):
    """ffprobe avg_frame_rate（如 '24000/1001'）→ '23.98 fps'；异常原样返回。"""
    try:
        num, den = str(rate).split("/", 1)
        value = int(num) / int(den)
        return f"{value:.2f}".rstrip("0").rstrip(".") + " fps"
    except (ValueError, ZeroDivisionError):
        return str(rate) if rate else "?"


def compare_rows(cfg, members):
    """比较视图数据组装（Review 26.10.12 §三.3）：组内成员并排展示
    分辨率/时长/编码/帧率/大小/音轨语言/声道/内嵌字幕/外挂字幕，逐字段给出
    差异标志供界面高亮。只呈现事实差异，不把分辨率、码率或 pHash 距离包装成
    质量评分或相似百分比。外挂字幕按 external_subtitles 现查（目录读取失败
    如实标「读取失败」并视为差异）；元数据读取失败的成员各字段显示错误。
    返回 {"cols": [文件名...], "rows": [{"label","cells","diff"}],
    "notes": 差异维度标签（["音轨","字幕","时长"] 子集，与 media_diff_notes
    同口径——供审阅/比较/确认页提示「存在差异需确认」）。"""
    def per(item, getter, missing="?"):
        meta = item.get("meta") or {}
        if meta.get("error"):
            return "读取失败"
        return getter(meta) or missing

    def audio_tracks(meta):
        return [t for t in meta.get("tracks", []) if t.get("type") == "audio"]

    def sub_tracks(meta):
        return [t for t in meta.get("tracks", []) if t.get("type") == "subtitle"]

    exts, ext_failed = [], False
    for item in members:
        try:
            exts.append("、".join(external_subtitles(cfg, item)) or "无")
        except OSError:
            exts.append("读取失败")
            ext_failed = True
    rows_def = [
        ("分辨率", lambda i, it: per(it, lambda x: f"{x.get('width') or '?'}×{x.get('height') or '?'}", "")),
        ("时长", lambda i, it: per(it, lambda x: fmt_dur(x.get("duration")))),
        ("编码", lambda i, it: per(it, lambda x: x.get("codec") or "?")),
        ("帧率", lambda i, it: per(it, lambda x: fmt_fps(x.get("fps")))),
        ("大小", lambda i, it: f"{it['stat']['size'] / 2**20:.1f} MB"),
        ("音轨语言", lambda i, it: per(it, lambda x: "+".join(str(t.get("language") or "未知") for t in audio_tracks(x)) or "无音轨", "")),
        ("声道", lambda i, it: per(it, lambda x: "+".join((str(t.get("channels") or "?") + "声道") for t in audio_tracks(x)) or "无音轨", "")),
        ("内嵌字幕", lambda i, it: per(it, lambda x: "+".join(str(t.get("language") or "未知") for t in sub_tracks(x)) or "无", "")),
        ("外挂字幕", lambda i, it: exts[i] if not (it.get("meta") or {}).get("error") else "读取失败"),
    ]
    cols = [Path(it["rel"]).name for it in members]
    rows = []
    for label, getter in rows_def:
        cells = [getter(i, it) for i, it in enumerate(members)]
        rows.append({"label": label, "cells": cells, "diff": len(set(cells)) > 1})
    by_label = {r["label"]: r["diff"] for r in rows}
    notes = []
    if by_label.get("音轨语言") or by_label.get("声道"):
        notes.append("音轨")
    if by_label.get("内嵌字幕") or by_label.get("外挂字幕"):
        notes.append("字幕")
    if by_label.get("时长"):
        notes.append("时长")
    if ext_failed and "字幕" not in notes:
        notes.append("字幕")  # 外挂字幕读取失败：无法核实是否一致，按需人工判断
    return {"cols": cols, "rows": rows, "notes": notes}


def media_diff_notes(metas):
    """成员媒体元数据的差异维度（§三.3/§三.4 与 compare_rows 同口径）：音轨、
    字幕、时长。metas 是 [{duration, tracks, external_subtitles?}]——计划文档
    成员的 metadata 带探测过的外挂字幕；缺 external_subtitles 键时只比较内嵌
    字幕轨（审阅页零 IO 提示用）。返回差异维度标签列表。"""
    def audios(m):
        return canonical([(t.get("language"), t.get("channels")) for t in m.get("tracks", []) if t.get("type") == "audio"])

    def subs(m):
        inner = canonical([t.get("language") for t in m.get("tracks", []) if t.get("type") == "subtitle"])
        ext = canonical(m.get("external_subtitles")) if m.get("external_subtitles") is not None else None
        return inner if ext is None else inner + "|" + ext

    notes = []
    if len({audios(m) for m in metas}) > 1:
        notes.append("音轨")
    if len({subs(m) for m in metas}) > 1:
        notes.append("字幕")
    if len({round(m.get("duration") or 0, 1) for m in metas}) > 1:
        notes.append("时长")
    return notes


def suggest_members(cfg, members, exacts=None, stored=None):
    """单组批量建议（Review 26.10.12 §三.3；P0 移除旧「最小文件=建议保留」规则
    ——完全一致的文件大小本就相同，大小不构成保留依据，也不用它推断质量）。

    members 是该组成员的 item dict 列表（files 表记录）；exacts 传分组入库时的
    完全重复子组（None 按同一分桶规则现算）；stored 传入库时的建议 JSON
    （groups.suggest；旧库行 NULL → 现算）。返回：
      {"clean":   [key...]  应用建议后标记待清理（全部属于某个 SHA 子组）
       "keep":    {key: 理由}  建议保留：受保护副本或子组按顺序保留的第一份
       "skipped": [{"members": [...], "reason": ...}]  需人工判断的子组及原因
       "manual":  [key...]  画面相似等不参与批量建议的成员，由用户逐项标记}
    规则：优先保留已有受保护副本（reference 目录、硬链接）；没有受保护副本时按
    文件夹路径+文件名 的稳定顺序保留第一份。外挂字幕不一致、元数据读取异常或
    字幕检查失败的子组整体跳过并列出原因。混合相似组只处理各自的 SHA 子组，
    绝不顺带标记其他转码版本；保留多份与整组跳过都是正常决定。"""
    if stored is not None:
        return stored
    root_path = {r["id"]: r["path"] for r in cfg["roots"]}
    reference_ids = {r["id"] for r in cfg["roots"] if r["role"] == "reference"}
    by_key = {key(m): m for m in members}
    clean, keep, skipped, covered = [], {}, [], set()
    for sub in exact_subgroups(members, exacts):
        covered.update(sub)
        bad = [k for k in sub if by_key[k].get("meta", {}).get("error")]
        reason = ""
        if bad:
            reason = "成员元数据读取异常（" + "、".join(bad[:2]) + (" 等" if len(bad) > 2 else "") + "），需人工判断"
        else:
            try:
                # 比较各成员的外挂字幕后缀序列（stem 各自对应文件名，比文件名本身）
                sidecars = {tuple(sorted(Path(n).suffix.lower() for n in external_subtitles(cfg, by_key[k])))
                            for k in sub}
            except (OSError, Unsafe) as e:
                reason = "外挂字幕检查失败（" + str(e).split("\n")[0][:120] + "），需人工判断"
            else:
                if len(sidecars) > 1:
                    reason = "成员外挂字幕不一致（有的带字幕文件、有的没有），删除可能丢失字幕，需人工判断"
        if reason:
            skipped.append({"members": list(sub), "reason": reason})
            continue
        protected = {k for k in sub if by_key[k]["root"] in reference_ids or by_key[k]["stat"]["nlink"] > 1}
        if protected:
            for k in sorted(protected):
                m = by_key[k]
                keep[k] = ("受保护副本（只读目录，仅参与对比）" if m["root"] in reference_ids
                           else "受保护副本（硬链接，多处引用同一文件）")
            clean.extend(k for k in sub if k not in protected)
        else:
            ordered = sorted(sub, key=lambda k: (root_path[by_key[k]["root"]], by_key[k]["rel"]))
            keep[ordered[0]] = "无受保护副本，按文件夹路径与文件名的稳定顺序保留的第一份"
            clean.extend(ordered[1:])
    return {"clean": sorted(clean), "keep": keep, "skipped": skipped,
            "manual": [k for k in map(key, members) if k not in covered]}


def suggestions(cfg, captured, groups):
    """建议列表（与 groups 同序）；单组核心规则见 suggest_members。
    # ponytail: 新扫描的建议已随 groups.suggest 持久化（§四D/P2），此处的现算
    # 只服务旧库行与测试直调；首页摘要走 scan_metrics，不再经过这里。"""
    return [{"gid": g["gid"], "kind": g["kind"],
             **suggest_members(cfg, [captured[k] for k in g["members"]], g.get("exacts"), g.get("suggest"))}
            for g in groups]


def scan_metrics(db, cfg, scan_id):
    """扫描摘要的统一读取口（Review 26.10.12 §四D/P2「首页重复计算建议」）：
    优先读 scans.summary 列——扫描完成时随结果持久化的聚合指标（含批量建议
    口径的候选数与可释放空间），首页/审阅共用同一来源。旧库行没有摘要时按需
    现算一次并只读返回（不写回：重新扫描后自然物化，不在 GET 页面里写库）。
    扫描不存在、或扫描时的授权目录被移除/替换时返回 None（该结果不可用，
    如实显示占位；新增授权不影响）。"""
    try:
        scan_guard(db, cfg, scan_id)
        row = db.execute("SELECT summary FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if all(k in data for k in ("groups", "candidates", "reclaim", "files")):
                return data
        captured = load_scan(db, cfg, scan_id)
        groups = load_groups(db, scan_id)
        sug = suggestions(cfg, captured, groups)
        return {"groups": len(groups), "candidates": sum(len(s["clean"]) for s in sug),
                "reclaim": sum(captured[k]["stat"]["size"] for s in sug for k in s["clean"]),
                "files": len(captured)}
    except (Unsafe, OSError, ValueError):
        return None


# --- review draft (Review 26.10.12 §四D) --------------------------------------


def load_draft(db, cfg, scan_id):
    """读取绑定某次扫描的审阅草稿。无草稿返回 {"rev": 0, "groups": {}}。

    身份校验与 load_scan 同源（scan_guard）：新扫描即新 scan_id，草稿天然
    不跨扫描继承；扫描时的授权目录被移除/替换直接拒绝（旧选择绝不自动套到
    新结果；新增授权不影响）。"""
    scan_guard(db, cfg, scan_id)
    row = db.execute("SELECT rev,data FROM drafts WHERE scan=?", (scan_id,)).fetchone()
    if not row:
        return {"rev": 0, "groups": {}}
    return {"rev": row[0], "groups": json.loads(row[1])}


def save_draft(db, cfg, scan_id, entries, expect_rev):
    """合并保存审阅草稿（乐观修订号，§四D）。

    entries 只携带本页处理过的组 {gid: {"clean": [key...], "confirmed": [key...],
    "skip": bool, "skip_reason": str?（§三.3 跳过的组提交前需说明原因，≤240 字）}}：请求中出现的 gid 整体替换、未出现的保留——同一标签页跨页
    选择累积；两个标签页基于同一旧修订先后保存时后者被 DraftConflict 明确
    拒绝，不静默覆盖。已生成的执行计划不受草稿后续修改影响（计划文档内容
    签名）；修改草稿后需重新生成计划。
    成功返回新修订号；条目/文件越界抛 Unsafe。"""
    scan_guard(db, cfg, scan_id)
    if not isinstance(entries, dict) or not entries:
        raise Unsafe("草稿内容无效")
    clean_keys = set()
    for gid, ent in entries.items():
        if not isinstance(gid, str) or not isinstance(ent, dict):
            raise Unsafe("草稿条目无效")
        row = db.execute("SELECT members FROM groups WHERE scan=? AND gid=?", (scan_id, gid)).fetchone()
        if not row:
            raise Unsafe("草稿引用了扫描结果之外的分组: " + str(gid)[:60])
        members = set(json.loads(row[0]))
        clean = ent.get("clean") or []
        confirmed = ent.get("confirmed") or []
        skip_reason = ent.get("skip_reason")
        if (not isinstance(clean, list) or not isinstance(confirmed, list)
                or not all(isinstance(k, str) for k in clean + confirmed)
                or ent.get("skip") not in (None, True, False)
                or (skip_reason is not None and (not isinstance(skip_reason, str) or len(skip_reason) > 240))):
            raise Unsafe("草稿条目字段无效: " + str(gid)[:60])
        unknown = (set(clean) | set(confirmed)) - members
        if unknown:
            raise Unsafe("草稿选择了分组之外的文件: " + ", ".join(sorted(unknown)[:3]))
        clean_keys.update(clean)
    if clean_keys:  # clean 还必须是该扫描里的真实文件（计划输入只来自草稿）
        marks = ",".join("?" * len(clean_keys))
        known = {r[0] for r in db.execute(
            "SELECT key FROM files WHERE scan=? AND key IN (%s)" % marks, (scan_id, *sorted(clean_keys)))}
        unknown = clean_keys - known
        if unknown:
            raise Unsafe("草稿选择了扫描之外的文件: " + ", ".join(sorted(unknown)[:3]))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row = db.execute("SELECT rev,data FROM drafts WHERE scan=?", (scan_id,)).fetchone()
    if row is None:
        if expect_rev != 0:
            raise DraftConflict(1, expect_rev)  # 本页从空白开始，别处已先保存
        try:
            db.execute("INSERT INTO drafts(scan,rev,data,updated) VALUES(?,1,?,?)",
                       (scan_id, canonical(entries), now))
            db.commit()
        except sqlite3.IntegrityError as e:  # 并发首存：唯一键竞争
            raise DraftConflict(1, expect_rev) from e
        return 1
    if row[0] != expect_rev:
        raise DraftConflict(row[0], expect_rev)
    merged = dict(json.loads(row[1]))
    merged.update(entries)
    cur = db.execute("UPDATE drafts SET data=?,rev=rev+1,updated=? WHERE scan=? AND rev=?",
                     (canonical(merged), now, scan_id, expect_rev))
    db.commit()
    if cur.rowcount == 0:  # 等锁期间被并发保存抢先：条件更新兜底，绝不双重覆盖
        raise DraftConflict(expect_rev + 1, expect_rev)
    return expect_rev + 1


def plan(cfg, scan_id, selections):
    """Build a signed cleanup plan from explicit selection items ({root, rel}).

    入口接入任务协调（§四B）：参数形状校验后占用/收养任务，结束把完整结果与
    错误落任务行；被占用时抛 tasks.Busy。"""
    if not isinstance(selections, list) or not selections:
        raise Unsafe("必须提供至少一个选择")
    selected = {}
    for value in selections:
        item = value if isinstance(value, dict) else selection(value)
        relative_path(item["rel"])
        selected[key(item)] = item
    # Review P1 修复：计划复核会经 fingerprint_cached 更新 hashes 缓存，必须持
    # 独占连接（state() 默认 exclusive=True）；原先 exclusive=False 与写缓存
    # 并存，和其它写者存在竞态。读者（审阅页/导出）不受影响——WAL 并发服务。
    with tasks.run(cfg, "plan", scan_id=scan_id) as task, state(cfg) as db:
        captured = load_scan(db, cfg, scan_id)
        groups = load_groups(db, scan_id)
        unknown = set(selected) - set(captured)
        if unknown:
            raise Unsafe("选择包含扫描之外的文件: " + ", ".join(sorted(unknown)[:5]))
        output, details, logical, rows = [], {}, 0, []
        touched = [g for g in groups if any(k in selected for k in g["members"])]
        plan_total = sum(len(g["members"]) for g in touched)
        plan_done = 0
        write_progress(cfg, {"scan_id": scan_id, "phase": "planning", "done": 0, "total": plan_total,
                             "current": "", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        for group in groups:
            if not any(k in selected for k in group["members"]):
                continue
            members = [dict(captured[k], **({"selected": True} if k in selected else {})) for k in group["members"]]
            for item in members:
                plan_done += 1
                write_progress(cfg, {"scan_id": scan_id, "phase": "planning", "done": plan_done, "total": plan_total,
                                     "current": key(item), "started": ""})
                # 复核走 (path,stat) 缓存：stat 未变即命中扫描时的 SHA，不再全量重读
                # （内容被替换会改变 stat 而未命中；执行删除前 apply 仍会全量终检）。
                result = fingerprint_cached(cfg, db, item, want_sha=True)
                sha = result["sha"]
                if item.get("sha256") and item["sha256"] != sha:
                    raise Unsafe("内容哈希与扫描结果不一致")
                item["sha256"] = sha
                # 元数据用扫描缓存（stat 未变即有效）；外挂字幕便宜，重查一次。
                metadata = dict(item["meta"])
                metadata["external_subtitles"] = external_subtitles(cfg, item)
                item["metadata"] = metadata
                root = next(r for r in cfg["roots"] if r["id"] == item["root"])
                protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
                if item.get("selected") and protected:
                    raise Unsafe("参考目录或硬链接文件不可删除")
                details[key(item)] = {"metadata": metadata, "硬链接数量": item["stat"]["nlink"], "protected": protected}
                shown = metadata if not metadata.get("error") else item["meta"]
                rows.append({"GroupId": group["gid"], "Kind": group["kind"], "Path": display_path(cfg, item["root"], item["rel"]),
                             "SizeBytes": item["stat"]["size"],
                             "Duration": round(shown["duration"], 3) if shown.get("duration") else None,
                             "Resolution": f"{shown.get('width') or '?'}x{shown.get('height') or '?'}",
                             "BitrateKbs": bitrate_kbs(shown), "Distance": "", "Protected": protected,
                             "Checked": "True" if item.get("selected") else "False"})
            exact = group["kind"] == "exact"
            if exact and len({m["sha256"] for m in members}) != 1:
                raise Unsafe("完全重复分组的内容校验失败")
            # §四C/P0/P1：连通分组≠两两相似，"每组保留一份"不足以保证留下与
            # 每个待清理版本直接匹配的文件。逐项用 pair_match（与扫描同一
            # 口径）核对直接匹配的保留对象并记录依据，替代原先组级的 min 口径
            # 时长检查——无直接匹配的清理项在生成计划前拒绝并列出原因。
            keepers = [m for m in members if not m.get("selected")]
            unmatched = []
            for item in members:
                if not item.get("selected"):
                    continue
                hits = [(pair_match(item, kept), kept) for kept in keepers]
                hits = [(b, k) for b, k in hits if b]
                if not hits:
                    why = ("该文件指纹信息不完整" if not item.get("vhash") or item["meta"].get("error")
                           else "保留文件指纹信息不完整" if any(not k.get("vhash") or k["meta"].get("error") for k in keepers)
                           else "与各保留文件的时长或画面差异均超出候选口径（A≈B≈C 不代表 A≈C）")
                    unmatched.append(f"{key(item)}: {why}")
                    continue
                basis, kept = min(hits, key=lambda bk: (bk[0]["basis"] != "sha256", bk[0].get("distance", 0), key(bk[1])))
                item["match"] = {"keep": key(kept), **basis}
                details[key(item)]["直接匹配保留"] = key(kept) + ("（字节一致）" if basis["basis"] == "sha256" else f"（画面距离 {basis['distance']}）")
            if unmatched:
                raise Unsafe("相似分组按连通关系组成，不保证两两相似；以下清理项没有与之直接匹配的保留文件，不生成删除计划（请把它们改为保留，或另选保留对象）: " + "; ".join(unmatched[:5]))
            tracks = {canonical({"tracks": m["metadata"].get("tracks"), "external_subtitles": m["metadata"].get("external_subtitles")}) for m in members}
            for item in members:
                details[key(item)].update({"类型": "字节完全重复" if exact else "视觉相似候选，需人工判断", "音轨或字幕差异": len(tracks) > 1})
            output.append({"group": group["gid"], "kind": "exact" if exact else "visual",
                           "delete": [m for m in members if m.get("selected")],
                           "keep": [m for m in members if not m.get("selected")]})
            logical += sum(m["stat"]["size"] for m in members if m.get("selected"))
        if not output:
            raise Unsafe("选择未命中任何扫描分组")
        for group in output:
            if not group["keep"] or not group["delete"]:
                raise Unsafe("每组必须保留至少一份文件")
        # Recheck after probing, which can be slow on large collections.
        for group in output:
            for item in group["delete"] + group["keep"]:
                if snapshot(file_path(cfg, item).lstat()) != item["stat"]:
                    raise Unsafe("审核期间文件发生变化，请重新扫描")
        payload = {"version": 1, "scan_id": scan_id, "config": config_identity(cfg), "groups": output, "logical_bytes": logical}
        plan_id = digest(payload)
        document = {"id": plan_id, **payload}
        directory = Path(cfg["state_dir"])
        atomic_json(directory / f"{plan_id}.plan.json", document)
        # §四F 业务状态与报告状态分离：签名计划已落盘即业务成功；审核报告写
        # export_dir 失败（不可写等）只降级说明，不把已成立的计划记为失败。
        detail_map = {display_path(cfg, *k.split(":", 1)): dv for k, dv in details.items()}
        report_path = Path(cfg["export_dir"]) / f"{plan_id}-review.html"
        report_error = ""
        try:
            write_review(report_path, "清理计划审核：请核对保留与清理清单、音轨与字幕", rows, detail_map)
        except OSError as e:
            report_error = f"审核报告写入共享文件夹失败: {e}；清理计划本身已生成，修复共享目录后重新提交选择即可"
        result = {"plan_id": plan_id, "delete_files": sum(len(g["delete"]) for g in output), "logical_bytes": logical,
                  "report": str(report_path), "plan": str(directory / f"{plan_id}.plan.json"),
                  "report_error": report_error or None,
                  "note": "默认只预演；请在报告中核对每份保留文件后再显式执行"}
        task["result"] = result  # 完整结果随任务行持久化（§四B），重启后确认页仍可读
        return result


def free_space(cfg):
    return {r["id"]: shutil.disk_usage(r["path"]).free for r in cfg["roots"]}


def require_cleanup_available():
    """真实清理的唯一引擎层门禁（Review 26.10.12 §四A）。

    系统回收站执行器尚未在 fnOS 真机验证，真实删除在引擎层无条件封死——
    Web 层的 CLEANUP_VERIFIED 只是体验层提示，不是安全边界。将来只有接入
    「经真机验证的系统回收站执行器」后才允许放行，且放行的必须是新的回收站
    执行器，不是旧的 unlink 永久删除路径。"""
    raise Unsafe("系统回收站能力尚未验证，真实清理不可用")


def apply(cfg, plan_id, execute=False, confirm=None):
    """预演/执行计划。真执行接入任务协调（§四B）：占用/收养任务，结束把完整
    结果落任务行；中途结束（异常/中断）结合 journal 经 tasks.apply_outcome
    定级 verify（不可证未执行）/partial/failed。预演是
    只读校验，不占任务也不持锁（与计划任务同进程先后调用，不能嵌套占用）。
    删除语义本身（原子暂存/journal/终检）不变。"""
    if not re.fullmatch(r"[a-f0-9]{64}", plan_id):
        raise Unsafe("计划编号格式不正确")
    if execute:
        # 引擎层封口先于 Linux/confirm 校验与任务占用：禁用状态不产生 apply 任务行。
        require_cleanup_available()
        if confirm != plan_id or os.name != "posix":
            raise Unsafe("执行删除需要 Linux，以及与计划编号完全一致的 --confirm")
    if not execute:
        return _apply(cfg, plan_id, False)
    with tasks.run(cfg, "apply", plan_id=plan_id) as task:
        result = _apply(cfg, plan_id, True)
        task["result"] = result
        return result


def _apply(cfg, plan_id, execute):
    # 真实执行体同样过门禁：绕过 apply 的内部调用也拒绝（预演不受影响）。
    if execute:
        require_cleanup_available()
    # 仅真正删除时持排它锁（互斥删除）；预演(dry_run)是长耗时的只读校验，
    # 无锁运行——否则大库预演期间整个应用被阻塞（真机 Errno 11）。
    with state(cfg, exclusive=execute) as db:
        directory = Path(cfg["state_dir"])
        document = json.loads((directory / f"{plan_id}.plan.json").read_text(encoding="utf-8"))
        payload = {k: v for k, v in document.items() if k != "id"}
        if document.get("id") != plan_id or digest(payload) != plan_id or document["config"] != config_identity(cfg):
            raise Unsafe("计划文件或目录配置已被修改")
        captured = load_scan(db, cfg, document["scan_id"])
        journal_path = directory / f"{plan_id}.journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {"plan_id": plan_id, "events": [], "pending": None}
        delete_keys = {key(m) for g in document["groups"] for m in g["delete"]}
        if journal.get("plan_id") != plan_id or any(e.get("key") not in delete_keys for e in journal.get("events", [])):
            raise Unsafe("清理日志与计划不一致")
        done = {e["key"] for e in journal["events"] if e["status"] == "deleted"}
        pending = journal.get("pending")
        if pending:
            if not isinstance(pending, dict) or not re.fullmatch(r"\.vd-delete-[a-f0-9]{32}\.[a-zA-Z0-9]{1,10}", pending.get("hold", "")):
                raise Unsafe("清理日志中的暂存路径无效")
            pending_key = pending["key"]
            item = next((m for g in document["groups"] for m in g["delete"] if key(m) == pending_key), None)
            if item is None:
                raise Unsafe("清理日志无效")
            path = file_path(cfg, item)
            held = path.parent / pending["hold"]
            if held.exists() or held.is_symlink():
                raise Unsafe(f"上次删除中断，未删除暂存文件。请先核对并恢复 {held} 到 {path}，然后重新 inventory/plan；不会自动删除暂存文件")
            if not path.exists():
                done.add(pending_key)
                if execute:
                    journal["events"].append({"key": pending_key, "status": "deleted", "bytes": item["stat"]["size"], "recovered": True})
                    journal["pending"] = None
                    atomic_json(journal_path, journal)
            elif snapshot(path.lstat()) != item["stat"]:
                raise Unsafe("中断后目标发生变化，不再执行删除")
        targets = []
        for group in document["groups"]:
            if not group["keep"] or not group["delete"]:
                raise Unsafe("计划分组必须有保留文件和删除文件")
            for item in group["keep"] + group["delete"]:
                ident = key(item)
                original = captured.get(ident)
                if not original or original["stat"] != item["stat"]:
                    raise Unsafe("计划与扫描快照不一致")
                file_path(cfg, item)
                if ident in done:
                    continue
                # 预检统一走 (path,stat) 缓存：扫描/计划时已算过 SHA，stat 未变
                # 即命中（内容被替换会因 stat 变化未命中并触发全量读+拒绝）。
                # 真正删除前的内容级终检在下方改名锁定后进行（保留）。
                sha = fingerprint_cached(cfg, db, item, want_sha=True)["sha"]
                if sha != item["sha256"]:
                    raise Unsafe("文件内容发生变化，整批预检失败")
            for item in group["delete"]:
                root = next(r for r in cfg["roots"] if r["id"] == item["root"])
                if root["role"] != "cleanup" or item["stat"]["nlink"] != 1:
                    raise Unsafe("目标不可清理")
                if key(item) not in done:
                    path = file_path(cfg, item)
                    if execute and not os.access(path.parent, os.W_OK | os.X_OK):
                        raise Unsafe("目录没有删除权限，不会自动提权")
                    targets.append((item, group["keep"]))
        before = free_space(cfg)
        if not execute:
            return {"dry_run": True, "plan_id": plan_id, "remaining_files": len(targets), "logical_bytes": sum(i["stat"]["size"] for i, _ in targets), "already_deleted": len(done)}
        # ponytail: one state-directory lock; use per-job locks only if parallel cleanup is needed.
        # 进度用全量口径（含本轮前已幂等删除的项），避免续跑计划显示 1/1 的误导。
        delete_all = [item for group in document["groups"] for item in group["delete"]]
        apply_total = len(delete_all)
        apply_done = len(done)
        apply_freed = sum(e.get("bytes", 0) for e in journal["events"] if e.get("status") == "deleted")
        write_progress(cfg, {"phase": "applying", "done": apply_done, "total": apply_total,
                             "current": "", "freed": apply_freed, "started": ""})
        for item, keepers in targets:
            apply_done += 1
            write_progress(cfg, {"phase": "applying", "done": apply_done, "total": apply_total,
                                 "current": key(item), "freed": apply_freed, "started": ""})
            try:
                if root_identities(cfg) != json.loads(db.execute("SELECT roots FROM scans WHERE id=?", (document["scan_id"],)).fetchone()[0]):
                    raise Unsafe("挂载目录发生变化")
                for kept in keepers:
                    if snapshot(file_path(cfg, kept).lstat()) != kept["stat"]:
                        raise Unsafe("保留文件在预检后发生变化")
                with anchored(cfg, item) as (fd, parent, name):
                    if snapshot(os.fstat(fd)) != item["stat"] or snapshot(os.stat(name, dir_fd=parent, follow_symlinks=False)) != item["stat"]:
                        raise Unsafe("目标在预检后被替换或修改")
                    hold = ".vd-delete-" + uuid.uuid4().hex + Path(name).suffix
                    journal["pending"] = {"key": key(item), "hold": hold}
                    atomic_json(journal_path, journal)
                    # Claim the directory entry atomically, then verify the claimed
                    # inode. A replacement between stat and rename is preserved.
                    reserved = os.open(hold, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    os.close(reserved)
                    claimed = False
                    try:
                        os.rename(name, hold, src_dir_fd=parent, dst_dir_fd=parent)
                        claimed = True
                        actual = snapshot(os.stat(hold, dir_fd=parent, follow_symlinks=False))
                        if any(actual[k] != item["stat"][k] for k in ("dev", "ino", "size", "mtime_ns", "nlink")):
                            raise Unsafe("原子暂存时目标被替换，保留该文件并停止")
                        os.lseek(fd, 0, os.SEEK_SET)
                        sha = hashlib.sha256()
                        stable = snapshot(os.fstat(fd))
                        while chunk := os.read(fd, 4 * 1024 * 1024):
                            sha.update(chunk)
                        if snapshot(os.fstat(fd)) != stable or sha.hexdigest() != item["sha256"]:
                            raise Unsafe("暂存文件内容变化，保留文件并停止")
                        for kept in keepers:
                            if snapshot(file_path(cfg, kept).lstat()) != kept["stat"]:
                                raise Unsafe("删除前保留文件发生变化")
                        if snapshot(os.fstat(fd)) != stable or snapshot(os.stat(hold, dir_fd=parent, follow_symlinks=False)) != stable:
                            raise Unsafe("删除前暂存文件发生变化，保留文件并停止")
                        os.unlink(hold, dir_fd=parent)
                    except Exception:
                        if claimed:
                            # link() refuses to overwrite any newly-created original
                            # path. A conflict leaves the held file for manual recovery.
                            try:
                                os.link(hold, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                                os.unlink(hold, dir_fd=parent)
                                journal["pending"] = None
                            except OSError:
                                pass
                        else:
                            os.unlink(hold, dir_fd=parent)
                            journal["pending"] = None
                        raise
                journal["events"].append({"key": key(item), "status": "deleted", "bytes": item["stat"]["size"], "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                journal["pending"] = None
                atomic_json(journal_path, journal)
                apply_freed += item["stat"]["size"]
                write_progress(cfg, {"phase": "applying", "done": apply_done, "total": apply_total,
                                     "current": "", "freed": apply_freed, "started": ""})
            except Exception as e:
                journal["events"].append({"key": key(item), "status": "error", "message": str(e), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                atomic_json(journal_path, journal)
                raise Unsafe(f"清理中止，已完成项目见日志 {journal_path}: {e}") from e
        return {"dry_run": False, "plan_id": plan_id, "deleted_files": len(targets), "logical_bytes": sum(i["stat"]["size"] for i, _ in targets), "free_bytes_before": before, "free_bytes_after": free_space(cfg), "journal": str(journal_path), "note": "空闲容量变化受快照、其他进程与延迟回收影响，不代表本次清理的独立物理收益"}


# --- fpk install/config hook ----------------------------------------------------


def accessible_roots():
    raw = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
    parts = raw.split(":") if os.name == "posix" else ([raw] if raw else [])
    return [PurePosixPath(p) for p in parts if p]


def root_authorized(path, accessible):
    p = PurePosixPath(path)
    return any(p == a or a in p.parents for a in accessible) if accessible else True


def derive_roots():
    """Config roots come from fnOS 'access permission' grants (system settings UI).

    Read-write grant -> cleanup; read-only grant -> reference (compare only).
    IDs derive from the path so they survive reordering of the grant list."""
    roots = []
    for path in accessible_roots():
        try:
            writable = os.access(str(path), os.W_OK | os.X_OK)
        except OSError:
            writable = False
        roots.append({"id": "d" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12],
                      "path": str(path), "role": "cleanup" if writable else "reference", "exclude": []})
    return roots


def fpk_setup():
    missing = [k for k in ("TRIM_PKGETC", "TRIM_PKGVAR") if not os.environ.get(k)]
    if missing:
        raise Unsafe("缺少应用环境变量: " + ", ".join(missing))
    etc = Path(os.environ["TRIM_PKGETC"])
    var = Path(os.environ["TRIM_PKGVAR"])
    shared = os.environ.get("TRIM_DATA_SHARE_PATHS", "")
    if os.name == "posix":
        shared = shared.split(":")[0]  # 冒号分隔的多路径；Windows 仅为开发测试（盘符含冒号）
    config_file = etc / "config.json"
    etc.mkdir(parents=True, exist_ok=True)
    roots = derive_roots()
    cfg = {"state_dir": str(var), "export_dir": shared or str(var / "exports"), "roots": roots, "probe": {}}
    temp = etc / "config.new.json"
    atomic_json(temp, cfg)
    try:
        if roots:
            # Validate before replacing a working configuration. An empty grant
            # list is written unvalidated so the UI can show onboarding.
            read_config(temp)
        os.replace(temp, config_file)
    finally:
        if temp.exists():
            temp.unlink()
    return {"config": str(config_file), "state": str(var), "roots": len(roots),
            "note": "请在 系统设置→应用→访问权限 中添加要扫描的文件夹" if not roots else "目录配置已从访问权限派生"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="飞牛原生视频查重核心引擎")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    commands.add_parser("fpk-setup")
    export = commands.add_parser("export")
    export.add_argument("--scan", required=True)
    review = commands.add_parser("plan")
    review.add_argument("--scan", required=True)
    review.add_argument("--select", action="append", required=True, help="root:相对路径，可重复")
    cleanup = commands.add_parser("apply")
    cleanup.add_argument("--plan", required=True)
    cleanup.add_argument("--execute", action="store_true")
    cleanup.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        if args.command == "fpk-setup":
            result = fpk_setup()
        else:
            if not args.config:
                parser.error("--config is required for this command")
            cfg = read_config(args.config)
            if args.command == "inventory":
                result = inventory(cfg)
            elif args.command == "export":
                result = export_audit(cfg, args.scan)
            elif args.command == "plan":
                result = plan(cfg, args.scan, args.select)
            else:
                result = apply(cfg, args.plan, args.execute, args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, csv.Error, sqlite3.Error) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

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

VERSION = "0.2.27"
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".vob", ".3gp", ".ogv"}
SIDECARS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
FRAME_POSITIONS = (0.1, 0.25, 0.5, 0.75, 0.9)
SIMILAR_MEAN_DISTANCE = 10  # of 64 pHash bits; report shows the real value
EXPORT_FIELDS = ["GroupId", "Kind", "Path", "SizeBytes", "Duration", "Resolution", "BitrateKbs", "Distance", "Protected", "Checked"]

# fpk bundles numpy as unpacked wheels in app/site-packages/ — no pip, no venv.
_BUNDLED_SITE = Path(__file__).resolve().parent / "site-packages"
if _BUNDLED_SITE.is_dir() and str(_BUNDLED_SITE) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_SITE))


class Unsafe(ValueError):
    pass


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
    """Parse 'root:rel' selection strings (first colon splits)."""
    if not isinstance(value, str) or ":" not in value:
        raise Unsafe(f"选择格式必须是 目录id:相对路径: {value!r}")
    root, rel = value.split(":", 1)
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
    """DB + operation lock. exclusive=True is the single-writer gate (scan/
    plan/apply); readers pass exclusive=False — SQLite WAL serves them
    concurrently and they must never fail with EAGAIN during a long scan."""
    directory = Path(cfg["state_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "operation.lock").open("a+b")
    try:
        if exclusive:
            if os.name == "posix":
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        db = sqlite3.connect(directory / "inventory.sqlite3")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, config TEXT, roots TEXT, created TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS files(scan TEXT, key TEXT, record TEXT, PRIMARY KEY(scan,key))")
            db.execute("CREATE TABLE IF NOT EXISTS hashes(cache_key TEXT PRIMARY KEY, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS groups(scan TEXT, gid TEXT, kind TEXT, members TEXT, PRIMARY KEY(scan,gid))")
            yield db
        finally:
            db.close()
    finally:
        lock.close()


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


def frame_gray(cfg, path, position):
    ffmpeg, _ = resolve_tools(cfg)
    out = run_tool([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{position:.3f}", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-",
                    "-threads", "1"], timeout=180)
    if len(out) != 1024:
        raise Unsafe("帧提取失败（输出尺寸异常）")
    return out


def thumbnail(cfg, path, position, name):
    ffmpeg, _ = resolve_tools(cfg)
    out = run_tool([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{position:.3f}", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg",
                    "-", "-threads", "1"], timeout=180)
    if not out.startswith(b"\xff\xd8"):
        raise Unsafe("缩略图生成失败")
    target = Path(cfg["state_dir"]) / "thumbs" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(out)
    return name


def visual_fingerprint(cfg, path, duration):
    try:
        return [phash(frame_gray(cfg, path, duration * p)) for p in FRAME_POSITIONS]
    except ImportError:
        raise Unsafe("numpy 未安装，无法计算视觉指纹（应用依赖安装不完整）")


def fingerprint_cached(cfg, db, item, want_sha):
    """Probe + pHash (+ optional sha256) with (path,stat) keyed cache.

    Unreadable videos degrade to a {'error': ...} meta without vhash; exact
    duplicate detection via sha256 still covers them. The cache key uses the
    file's *current* stat — if it differs from the recorded one (replaced /
    edited since scan), refuse instead of serving a stale hash."""
    path = file_path(cfg, item)
    current = snapshot(path.lstat())
    expected = item.get("stat")
    if expected is not None and current != expected:
        raise Unsafe(f"文件在复核时发生变化: {key(item)}")
    cache_key = digest({"path": str(path), "stat": current})
    cached = db.execute("SELECT result FROM hashes WHERE cache_key=?", (cache_key,)).fetchone()
    result = json.loads(cached[0]) if cached else {}
    changed = cached is None
    if "meta" not in result:
        try:
            result["meta"] = probe_media(cfg, path)
            result["vhash"] = visual_fingerprint(cfg, path, result["meta"]["duration"])
            result["thumb"] = thumbnail(cfg, path, result["meta"]["duration"] * FRAME_POSITIONS[2],
                                        cache_key[:32] + ".jpg")
        except (Unsafe, OSError, ValueError, subprocess.SubprocessError) as e:
            result["meta"] = {"error": str(e)[:240]}
        changed = True
    if want_sha and "sha" not in result:
        _, result["sha"] = fingerprint(cfg, item, item["stat"])
        changed = True
    if changed:
        db.execute("INSERT OR REPLACE INTO hashes VALUES(?,?)", (cache_key, canonical(result)))
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


def read_progress(cfg):
    try:
        return json.loads(progress_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_cancel(cfg):
    if cancel_path(cfg).exists():
        raise Cancelled()


# --- scan ---------------------------------------------------------------------


def inventory(cfg):
    roots = root_identities(cfg)
    scan_id = uuid.uuid4().hex
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cancel_path(cfg).unlink(missing_ok=True)
    with state(cfg) as db:
        files, seen, warnings = [], set(), []
        subdirs = cfg.get("scan_subdirs") or []
        for root in cfg["roots"]:
            start_points = []
            if subdirs:
                # 只扫用户勾选的子目录（相对根路径）；不存在的忽略
                for rel in subdirs:
                    candidate = Path(root["path"]) / rel
                    if candidate.is_dir() and not candidate.is_symlink():
                        start_points.append(candidate)
            else:
                start_points.append(Path(root["path"]))
            for start in start_points:
                def walk_error(error):
                    # 读不了的子目录（如系统缩略图目录）：跳过并记录，不中止扫描。
                    warnings.append("跳过不可读目录: " + str(error))
                for folder, dirs, names in os.walk(start, followlinks=False, onerror=walk_error):
                    base = Path(folder)
                    dirs[:] = [n for n in dirs if not (base / n).is_symlink() and not excluded(cfg, owner(cfg, base / n), base / n)]
                    for name in names:
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
        write_progress(cfg, {"scan_id": scan_id, "phase": "fingerprint", "done": 0, "total": len(files), "current": "", "started": started})
        buckets = {}
        for item in files:
            buckets.setdefault(item["stat"]["size"], []).append(item)
        dup_sizes = {size for size, bucket in buckets.items() if len(bucket) > 1}
        fingerprint_errors = []
        try:
            for done, item in enumerate(files, 1):
                check_cancel(cfg)
                result = fingerprint_cached(cfg, db, item, item["stat"]["size"] in dup_sizes)
                item["sha256"] = result.get("sha")
                item["meta"] = result["meta"]
                item["vhash"] = result.get("vhash")
                item["thumb"] = result.get("thumb")
                if item["meta"].get("error"):
                    fingerprint_errors.append(key(item))
                write_progress(cfg, {"scan_id": scan_id, "phase": "fingerprint", "done": done, "total": len(files),
                                     "current": key(item), "started": started})
            # 扫描期间变化的文件只从本轮剔除（下次扫描按新 stat 重新识别），不整批作废。
            changed = [key(item) for item in files if snapshot(file_path(cfg, item).lstat()) != item["stat"]]
            if changed:
                changed_set = set(changed)
                files = [item for item in files if key(item) not in changed_set]
            if root_identities(cfg) != roots:
                raise Unsafe("扫描期间挂载目录发生变化")
        except Cancelled:
            write_progress(cfg, {"scan_id": scan_id, "phase": "cancelled", "done": 0, "total": len(files), "current": "", "started": started})
            cancel_path(cfg).unlink(missing_ok=True)
            return {"scan_id": scan_id, "cancelled": True, "note": "扫描已取消；指纹缓存保留，再次扫描将增量续跑"}
        # One union-find over all files: SHA equality is hard evidence, pHash
        # similarity (duration-compatible pairs) adds soft links. A cluster of
        # identical SHAs is "exact"; anything mixed (e.g. original + copy +
        # transcode) is "visual" so the transcode can join its original.
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
        ordered = sorted(visual_ids, key=lambda i: files[i]["meta"]["duration"])
        for ai in range(len(ordered)):
            for bi in range(ai + 1, len(ordered)):
                a, b = files[ordered[ai]], files[ordered[bi]]
                if b["meta"]["duration"] - a["meta"]["duration"] > max(5.0, a["meta"]["duration"] * 0.02):
                    break
                if find(ordered[ai]) == find(ordered[bi]):
                    continue
                mean = sum(hamming(x, y) for x, y in zip(a["vhash"], b["vhash"])) / len(FRAME_POSITIONS)
                if mean <= SIMILAR_MEAN_DISTANCE:
                    union(ordered[ai], ordered[bi])
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
            groups.append({"gid": gid, "kind": kind, "members": sorted(members, key=key)})
        db.execute("INSERT INTO scans VALUES(?,?,?,?)", (scan_id, config_identity(cfg), canonical(roots), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        db.executemany("INSERT INTO files VALUES(?,?,?)", [(scan_id, key(f), canonical(f)) for f in files])
        db.executemany("INSERT INTO groups VALUES(?,?,?,?)", [(scan_id, g["gid"], g["kind"], canonical([key(m) for m in g["members"]])) for g in groups])
        db.commit()
        write_progress(cfg, {"scan_id": scan_id, "phase": "done", "done": len(files), "total": len(files), "current": "", "started": started})
    exports = export_audit(cfg, scan_id)
    return {"scan_id": scan_id, "files": len(files), "exact_groups": sum(g["kind"] == "exact" for g in groups),
            "scan_warnings": len(warnings), "scan_warning_samples": warnings[:5],
            "visual_groups": sum(g["kind"] == "visual" for g in groups),
            "fingerprint_errors": len(fingerprint_errors), "fingerprint_error_samples": fingerprint_errors[:5],
            "changed_during_scan": len(changed), "changed_samples": changed[:5],
            **exports}


def load_scan(db, cfg, scan_id):
    if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise Unsafe("扫描编号格式不正确")
    row = db.execute("SELECT config,roots FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not row or row[0] != config_identity(cfg):
        raise Unsafe("扫描不存在或目录配置已经变化，必须重新扫描")
    if json.loads(row[1]) != root_identities(cfg):
        raise Unsafe("挂载目录已被替换，必须重新扫描")
    return {k: json.loads(record) for k, record in db.execute("SELECT key,record FROM files WHERE scan=?", (scan_id,))}


def load_groups(db, scan_id):
    return [{"gid": gid, "kind": kind, "members": json.loads(members)}
            for gid, kind, members in db.execute("SELECT gid,kind,members FROM groups WHERE scan=? ORDER BY kind,gid", (scan_id,))]


# --- review / plan / export ----------------------------------------------------


def export_audit(cfg, scan_id):
    """Write the human-readable audit report (CSV + HTML) into export_dir."""
    with state(cfg, exclusive=False) as db:
        captured = load_scan(db, cfg, scan_id)
        groups = load_groups(db, scan_id)
    rows, details = [], {}
    for g in groups:
        members = [captured[k] for k in g["members"]]
        for i, item in enumerate(members):
            root = next(r for r in cfg["roots"] if r["id"] == item["root"])
            if g["kind"] == "visual":
                other = members[(i + 1) % len(members)]
                distance = round(sum(hamming(x, y) for x, y in zip(item["vhash"], other["vhash"])) / len(FRAME_POSITIONS), 1)
            else:
                distance = 0
            protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
            meta = item["meta"]
            path = f"/media/{item['root']}/{item['rel']}"
            rows.append({"GroupId": g["gid"], "Kind": g["kind"], "Path": path,
                         "SizeBytes": item["stat"]["size"], "Duration": round(meta.get("duration") or 0, 3),
                         "Resolution": f"{meta.get('width') or '?'}x{meta.get('height') or '?'}",
                         "BitrateKbs": meta.get("bitrate"), "Distance": distance,
                         "Protected": protected, "Checked": "False"})
            details[path] = {"类型": "字节完全重复" if g["kind"] == "exact" else "视觉相似候选，需人工判断",
                             "pHash 距离(相邻成员)": distance, "硬链接数量": item["stat"]["nlink"],
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
        cells.append("<tr>" + "".join("<td>" + html.escape(str(x)) + "</td>" for x in fields) + "<td><pre>" + html.escape(json.dumps(info, ensure_ascii=False)) + "</pre></td></tr>")
    headers = json.dumps(EXPORT_FIELDS)
    page = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1><title>视频查重审计</title>
<style>body{font:15px system-ui;margin:24px;color:#17202a}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:8px;text-align:left;word-break:break-all}pre{white-space:pre-wrap;font-size:12px}</style>
<h1>""" + html.escape(title) + """</h1><p>审计导出：字节完全重复组与视觉相似候选。视觉相似不是字节一致性的证明；本报告不执行任何删除。</p>
<table><thead><tr><th>分组</th><th>容器路径</th><th>大小（字节）</th><th>时长</th><th>分辨率</th><th>pHash 距离</th><th>核对信息</th></tr></thead><tbody>""" + "".join(cells) + """</tbody></table>"""
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


def probe(cfg, item):
    """Metadata for a scanned item, refreshed at plan time; sidecars included."""
    try:
        meta = probe_media(cfg, file_path(cfg, item))
    except (OSError, ValueError, Unsafe) as e:
        return {"error": "FFprobe 无法读取视频信息: " + str(e).split("\n")[0][:240]}
    sidecars = sorted(p.name for p in file_path(cfg, item).parent.iterdir() if p.suffix.lower() in SIDECARS and p.stem == file_path(cfg, item).stem)
    meta["external_subtitles"] = sidecars
    return meta


def plan(cfg, scan_id, selections):
    """Build a signed cleanup plan from explicit selection items ({root, rel})."""
    if not isinstance(selections, list) or not selections:
        raise Unsafe("必须提供至少一个选择")
    selected = {}
    for value in selections:
        item = value if isinstance(value, dict) else selection(value)
        relative_path(item["rel"])
        selected[key(item)] = item
    with state(cfg, exclusive=False) as db:
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
                fp = file_path(cfg, item)
                metadata["external_subtitles"] = sorted(p.name for p in fp.parent.iterdir() if p.suffix.lower() in SIDECARS and p.stem == fp.stem)
                item["metadata"] = metadata
                root = next(r for r in cfg["roots"] if r["id"] == item["root"])
                protected = root["role"] == "reference" or item["stat"]["nlink"] > 1
                if item.get("selected") and protected:
                    raise Unsafe("参考目录或硬链接文件不可删除")
                details[key(item)] = {"metadata": metadata, "硬链接数量": item["stat"]["nlink"], "protected": protected}
                shown = metadata if not metadata.get("error") else item["meta"]
                rows.append({"GroupId": group["gid"], "Kind": group["kind"], "Path": f"/media/{item['root']}/{item['rel']}",
                             "SizeBytes": item["stat"]["size"], "Duration": round(shown.get("duration", 0), 3),
                             "Resolution": f"{shown.get('width')}x{shown.get('height')}",
                             "BitrateKbs": shown.get("bitrate"), "Distance": "", "Protected": protected,
                             "Checked": "True" if item.get("selected") else "False"})
            exact = group["kind"] == "exact"
            if exact and len({m["sha256"] for m in members}) != 1:
                raise Unsafe("完全重复分组的内容校验失败")
            readable = [m for m in members if not m["metadata"].get("error")]
            durations = [m["metadata"]["duration"] for m in readable]
            complete = len(readable) == len(members) and max(durations) - min(durations) <= min(5, max(durations) * .02)
            if not exact and not complete:
                raise Unsafe("相似视频信息不完整或时长差异过大，本组不生成删除计划")
            tracks = {canonical({"tracks": m["metadata"].get("tracks"), "external_subtitles": m["metadata"].get("external_subtitles")}) for m in members}
            reference_ids = {r["id"] for r in cfg["roots"] if r["role"] == "reference"}
            suggested = min(members, key=lambda m: (m["stat"]["size"], m["root"] not in reference_ids, key(m)))
            for item in members:
                details[key(item)].update({"类型": "字节完全重复" if exact else "视觉相似候选，需人工判断", "建议保留": suggested is item, "音轨或字幕差异": len(tracks) > 1})
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
        detail_map = {f"/media/{k.split(':', 1)[0]}/{k.split(':', 1)[1]}": v for k, v in details.items()}
        write_review(Path(cfg["export_dir"]) / f"{plan_id}-review.html", "清理计划审核：请核对小体积版本、音轨与字幕", rows, detail_map)
    return {"plan_id": plan_id, "delete_files": sum(len(g["delete"]) for g in output), "logical_bytes": logical,
            "report": str(Path(cfg["export_dir"]) / f"{plan_id}-review.html"), "plan": str(directory / f"{plan_id}.plan.json"),
            "note": "默认只预演；请在报告中核对每份保留文件后再显式执行"}


def free_space(cfg):
    return {r["id"]: shutil.disk_usage(r["path"]).free for r in cfg["roots"]}


def apply(cfg, plan_id, execute=False, confirm=None):
    if not re.fullmatch(r"[a-f0-9]{64}", plan_id):
        raise Unsafe("计划编号格式不正确")
    if execute and (confirm != plan_id or os.name != "posix"):
        raise Unsafe("执行删除需要 Linux，以及与计划编号完全一致的 --confirm")
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

#!/usr/bin/env python3
"""Build a real .fpk using the official portable fnpack, without installing it.

Bundled at build time (cached in .tools/): ffmpeg/ffprobe static binaries
(GPL, johnvansickle) and numpy wheels — installs on the NAS are fully
offline; the wizard pip index is only a fallback for numpy.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
FFMPEG_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
VERSION = next(line.split("=", 1)[1].strip() for line in
               (ROOT / "fpk" / "manifest").read_text(encoding="utf-8").splitlines()
               if line.startswith("version="))  # 唯一版本来源：fpk/manifest


def normalize_package(package):
    """fnpack on Windows emits 0666 scripts; repair tar modes and app MD5."""
    with tarfile.open(package, "r:gz") as archive:
        members = [(m, archive.extractfile(m).read() if m.isfile() else None) for m in archive.getmembers()]
    app = next(data for member, data in members if member.name == "app.tgz")
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(app), mode="r:gz") as src, tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as dst:
        for member in src.getmembers():
            member.mode = 0o755 if member.isdir() or member.name.lstrip("./").startswith("bin/") else 0o644
            data = src.extractfile(member).read() if member.isfile() else None
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    app = output.getvalue()
    app_checksum = hashlib.md5(app, usedforsecurity=False).hexdigest()
    temp = package.with_suffix(".normalized.fpk")
    with tarfile.open(temp, "w:gz", format=tarfile.PAX_FORMAT) as dst:
        for member, data in members:
            member.mode = 0o755 if member.isdir() or member.name.startswith("cmd/") else 0o644
            if member.name == "app.tgz":
                data = app
            elif member.name == "manifest":
                data = re.sub(r"(?m)^(checksum\s*=\s*)[a-f0-9]{32}", lambda m: m[1] + app_checksum, data.decode("utf-8")).encode("utf-8")
            if data is not None:
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    for attempt in range(5):
        try:
            package.unlink(missing_ok=True)
            os.replace(temp, package)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(1)  # Windows Defender 等外部进程对新文件的临时锁


def bundled_ffmpeg():
    """Return ffmpeg/ffprobe pair, downloading the static build once."""
    cache = ROOT / ".tools" / "ffmpeg-linux64"
    pair = (cache / "ffmpeg", cache / "ffprobe")
    if pair[0].exists() and pair[1].exists():
        return pair
    archive_path = ROOT / ".tools" / "ffmpeg-release-amd64-static.tar.xz"
    if not archive_path.exists():
        archive_path.parent.mkdir(exist_ok=True)
        print("downloading static ffmpeg (build-time, ~40MB)…", flush=True)
        urllib.request.urlretrieve(FFMPEG_URL, archive_path)
    cache.parent.mkdir(exist_ok=True)
    with tarfile.open(archive_path, "r:xz") as archive:
        for member in archive.getmembers():
            if member.name.split("/")[-1] in {"ffmpeg", "ffprobe"} and member.isfile():
                member.name = member.name.split("/")[-1]
                archive.extract(member, cache)
                (cache / member.name).chmod(0o755)
    if not (pair[0].exists() and pair[1].exists()):
        raise RuntimeError("static ffmpeg archive did not contain ffmpeg/ffprobe")
    return pair


def bundled_wheels():
    """Unpack the numpy wheel into the app's site-packages (offline, no pip on the NAS)."""
    cache = ROOT / ".tools" / "wheels"
    cache.mkdir(exist_ok=True)
    if not any(cache.glob("numpy-*.whl")):
        print("downloading numpy wheel (build-time, ~17MB)…", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
                        "--platform", "manylinux_2_27_x86_64", "--platform", "manylinux2014_x86_64",
                        "--platform", "manylinux_2_17_x86_64", "--platform", "manylinux_2_28_x86_64",
                        "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312",
                        "--no-deps", "--dest", str(cache), "numpy"], check=True)
    return next(cache.glob("numpy-*.whl"))


def build(tool=None):
    if tool:
        binary = Path(tool).resolve()
    else:
        system = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}[platform.system()]
        arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
        binary = ROOT / ".tools" / ("fnpack.exe" if system == "windows" else "fnpack")
        if not binary.exists():
            binary.parent.mkdir(exist_ok=True)
            urllib.request.urlretrieve(f"https://static2.fnnas.com/fnpack/fnpack-1.2.3-{system}-{arch}", binary)
            binary.chmod(0o755)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    stage = dist / "builds" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    shutil.copytree(ROOT / "fpk", stage)
    for name in ("videodedupe.py", "tasks.py", "webui.py", "README.md", "VALIDATION.md", "LICENSE", "THIRD_PARTY.md", "config.example.json"):
        shutil.copy2(ROOT / name, stage / "app" / name)
    # §四E 静态资源（CSS/JS）随包分发；manifest 一并复制到 app/ 供运行时
    # 显示版本解析（§四F：唯一版本来源仍是 fpk/manifest，此为打包产物副本）。
    shutil.copytree(ROOT / "static", stage / "app" / "static")
    shutil.copy2(ROOT / "fpk" / "manifest", stage / "app" / "manifest")
    ffmpeg_dir = stage / "app" / "bin"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = bundled_ffmpeg()
    shutil.copy2(ffmpeg, ffmpeg_dir / "ffmpeg")
    shutil.copy2(ffprobe, ffmpeg_dir / "ffprobe")
    site = stage / "app" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    import zipfile
    with zipfile.ZipFile(bundled_wheels()) as wheel:
        wheel.extractall(site)
    for path in (stage / "cmd").iterdir():
        path.chmod(0o755)
    (ffmpeg_dir / "ffmpeg").chmod(0o755)
    (ffmpeg_dir / "ffprobe").chmod(0o755)
    result = subprocess.run([str(binary), "build", "--directory", str(stage)], cwd=dist, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    candidates = list(dist.glob("*.fpk")) + list(stage.glob("*.fpk"))
    package = next((p for p in candidates if p.name == "video-dedupe.fpk"), None)
    if package is None:
        raise RuntimeError("fnpack 未生成预期的 video-dedupe.fpk: " + result.stdout)
    normalize_package(package)
    destination = dist / f"video-dedupe-{VERSION}.fpk"
    if package != destination:
        shutil.copy2(package, destination)
    package_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    (dist / "SHA256SUMS").write_text(f"{package_hash}  {destination.name}\n", encoding="ascii")
    metadata = {"package": destination.name, "sha256": package_hash, "fnpack": "1.2.3", "fnpack_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "runtime": "python312", "dependencies": "bundled static ffmpeg/ffprobe (GPL, johnvansickle) + numpy unpacked into app/site-packages; no pip/venv/network at install", "ui": "fnOS unified gateway (app.sock, os_min_version 1.1.3100)", "architecture": "x86 (bundled linux-x64 binaries)", "postprocess": "Unix executable modes normalized; app.tgz MD5 recalculated", "verification": "fnpack static validation only; NAS runtime validation pending"}
    (dist / "build-info.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = dist / f"video-dedupe-{VERSION}-source.tar.gz"
    with tarfile.open(bundle, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        sources = [ROOT / name for name in ("videodedupe.py", "tasks.py", "webui.py", "build_fpk.py", ".gitignore", ".gitattributes", ".code-review-graphignore", "config.example.json", "README.md", "VALIDATION.md", "LICENSE", "THIRD_PARTY.md", "AGENTS.md", "Log.md")]
        sources += list((ROOT / "tests").glob("*.py"))
        sources += list((ROOT / "static").glob("*.css")) + list((ROOT / "static").glob("*.js"))
        sources += list((ROOT / "assets/icon").glob("*.svg"))
        sources += list((ROOT / ".github/workflows").glob("*.yml"))
        sources += [path for path in (ROOT / "fpk").rglob("*") if path.is_file() and not path.is_symlink() and not path.name.startswith(".")]
        for path in sources:
            rel = path.relative_to(ROOT)
            if path.is_file() and not path.is_symlink():
                info = archive.gettarinfo(str(path), arcname="video-dedupe/" + rel.as_posix())
                info.mode = 0o755 if rel.parts[0] == "fpk" and ("cmd" in rel.parts or "bin" in rel.parts) else 0o644
                with path.open("rb") as f:
                    archive.addfile(info, f)
    return {"fpk": str(destination), "sha256": package_hash, "source": str(bundle), "stage": str(stage)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fnpack")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.fnpack), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

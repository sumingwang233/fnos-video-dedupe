# video-dedupe

[简体中文](README.md) | English

Scans authorized folders on your NAS for videos that are **byte-identical** (SHA-256) or **visually similar** (pHash perceptual fingerprints), walks you through a manual review, and then executes an **explicitly confirmed, permanent deletion** backed by a signed plan — everything is re-verified before deletion, and an audit trail is archived afterwards.

[Download](#download) · [Screenshots](#screenshots) · [Getting started](#getting-started-in-three-steps) · [Everyday use](#everyday-use) · [FAQ](#data-and-faq)

## Download

Current release: **v0.2.27** (2026-10-08). Requires fnOS **1.1.3100 or newer** (unified gateway) on **x86** hardware; the UI is Simplified Chinese and admin-only. Every dependency ships inside the package — no Internet access needed to install or run.

| File | For whom |
|---|---|
| `video-dedupe-0.2.27.fpk` (~75 MB) | fnOS users: side-load the fpk in the App Center. |
| `video-dedupe-0.2.27-source.tar.gz` | Developers who want to read or build the source. |
| `SHA256SUMS.txt` | SHA-256 checksums for the files above. |

Get them from [Releases](https://github.com/sumingwang233/fnos-video-dedupe/releases). Verify before installing:

```sh
sha256sum video-dedupe-0.2.27.fpk   # compare against SHA256SUMS.txt
```

## Screenshots

The overview dashboard: pipeline stage, duplicate groups / delete candidates / reclaimable space / total videos at a glance (light theme is the default):

![Overview](assets/readme/overview-light.png)

The review page: thumbnails, sizes and visual distance per group; the smallest file in each group is kept by default, read-only reference folders are locked:

![Review](assets/readme/review-light.png)

The final confirmation before deletion — red warning, permanence spelled out, and every file is re-verified at execution time:

![Plan confirmation](assets/readme/plan-review-light.png)

One click switches to the dark theme:

![Dark theme](assets/readme/overview-dark.png)

## Getting started in three steps

1. **Authorize folders**: System Settings → Apps → video-dedupe → Access Permissions, add the folders to scan. **Read-write** = duplicates inside may be cleaned; **read-only** = reference only, never deleted.
2. **Scan**: open the app and press "开始扫描" (Start scan). The first pass reads every video and computes fingerprints (hours for large libraries; best done while the library is idle). Later scans skip unchanged files.
3. **Review and clean**: inspect each group, accept the suggested selection or adjust it → generate the cleanup plan (dry-run by default) → explicitly confirm the permanent deletion. An audit report is archived to a shared folder automatically.

## Everyday use

| Task | How |
|---|---|
| Deduplicate newly added videos | Overview → start scan → review |
| Scan only some subfolders | Overview → scan scope → pick subfolders |
| Two byte-identical copies of the same movie | "完全重复" groups, accept the suggestion |
| Keep one of several re-encoded variants | Inspect "视觉相似" group thumbnails and distances, pick manually |
| Check what was deleted before | Audit records (CSV / HTML) in the shared folder `video-dedupe/data` |
| Changed authorized folders | Stop and restart the app after changing permissions; rescan (fingerprint cache is kept, so it is fast) |

## Data and FAQ

**Is deletion safe? Could it delete the wrong file?**
Dry-run by default: nothing is deleted without explicit confirmation. Every group keeps at least one copy — selecting everything is rejected. Read-only folders and hard links never enter the deletion list. Plans are signed by content hash; at execution time the folder config, mount identity, file stats and SHA-256 are all re-verified, and any mismatch aborts the whole batch. On Linux, deletion first stages the file atomically in the same directory and re-verifies before the final unlink; a journal is written before each step so interruptions are recoverable. **The final deletion is permanent** (no recycle bin) — read the confirmation page carefully.

**Where is my data?**
The scan database and thumbnails live in the app's private directory; audit records are exported to the shared folder `video-dedupe/data`.

**Does it need Internet, Python or Docker?**
No. Since v0.2.0 the app is fully native (no containers); ffmpeg / ffprobe and numpy ship inside the package, with zero network dependencies at install time.

**Can non-admin users use it?**
No. The UI is served through the fnOS unified gateway with NAS account authentication, admin only.

**What about the v0.1.x Docker version?**
v0.1.0 was built on the VDF container; v0.2.0 is a native fpk rewrite. Data does not carry over — uninstall the old version and install this one.

**Which formats are supported?**
Common containers and codecs are decoded by the bundled ffmpeg (mp4 / mkv / mov / avi / wmv / flv / m2ts, …). Corrupt or unreadable files are recorded as warnings without aborting the scan.

### Current version

**v0.2.27 (2026-10-08)**: UI realigned with the Strata design language — light theme by default with one-click dark mode, top-bar single-column layout, dashboard-style overview (four metric cards + stage chain), a live status pill on every page, and a self-hosted Outfit font (offline, no CDN). See [Releases](https://github.com/sumingwang233/fnos-video-dedupe/releases) for all versions and [Log.md](Log.md) for the per-version changelog.

__Development and project structure__

Developed on Windows + WSL Ubuntu; mostly Python 3.12 standard library (numpy is the only third-party import), with unittest green on both platforms before every commit:

```sh
py -3.12 -X utf8 -m unittest discover -s tests -v        # tests (Windows)
py -3.12 -X utf8 build_fpk.py                            # build the fpk (fnpack 1.2.3)
VD_ALLOW_NO_ADMIN=1 py -3.12 -X utf8 webui.py --config config.local.json --tcp 18099  # local UI smoke test
```

In one sentence: `videodedupe.py` (scan / fingerprint / grouping / plan / delete engine) + `webui.py` (stdlib http.server UI over an AF_UNIX socket behind the unified gateway) + `fpk/` (fnOS app skeleton and lifecycle scripts) + `build_fpk.py` (packaging). Development discipline and module boundaries are in [AGENTS.md](AGENTS.md); validation records in [VALIDATION.md](VALIDATION.md).

## Contributing and license

- Repository code is licensed under [MIT](LICENSE).
- The bundled ffmpeg / ffprobe are GPL static binaries (built by johnvansickle); the Outfit font is SIL OFL 1.1. See [THIRD_PARTY.md](THIRD_PARTY.md) for declarations and source pointers.
- The visual-similarity approach was inspired by VDF (AGPL); no VDF code was copied.

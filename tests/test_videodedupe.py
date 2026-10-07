import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import videodedupe as v


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "视频 空间"
        self.ref = self.base / "archive"
        self.media.mkdir()
        self.ref.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"), "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": ["ignored/**"]}, {"id": "archive", "path": str(self.ref), "role": "reference", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def copies(self):
        filename = '片名 "逗号, 空格.mp4' if os.name == "posix" else '片名 逗号, 空格.mp4'
        a, b = self.media / filename, self.ref / "备份.mp4"
        a.write_bytes(b"video-a" * 20)
        b.write_bytes(a.read_bytes())
        return a, b

    def members(self, scan):
        with v.state(self.cfg) as db:
            return v.load_groups(db, scan["scan_id"])

    def selections_for(self, scan, selected="movies"):
        idents = [k for g in self.members(scan) for k in g["members"]]
        if selected == "all":
            picked = idents
        else:
            picked = [k for k in idents if k.startswith(selected + ":")]
        return [{"root": k.split(":", 1)[0], "rel": k.split(":", 1)[1]} for k in picked]

    def make_plan(self):
        a, b = self.copies()
        scan = v.inventory(self.cfg)
        with patch.object(v, "probe", return_value={"duration": 10, "tracks": [], "external_subtitles": []}):
            plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))
        return a, b, scan, plan

    def test_exact_and_unicode_and_dry_run(self):
        a, b, scan, plan = self.make_plan()
        self.assertEqual(scan["exact_groups"], 1)
        result = v.apply(self.cfg, plan["plan_id"])
        self.assertEqual(result["remaining_files"], 1)
        self.assertTrue(a.exists() and b.exists())
        self.assertIn("字节完全重复", Path(scan["report"]).read_text(encoding="utf-8"))

    def test_same_size_different_content(self):
        self.copies()
        (self.ref / "备份.mp4").write_bytes(b"video-b" * 20)
        self.assertEqual(v.inventory(self.cfg)["exact_groups"], 0)

    def test_replaced_same_size_file_aborts(self):
        a, _, _, plan = self.make_plan()
        stamp = a.stat()
        a.write_bytes(b"different".ljust(stamp.st_size, b"!"))
        os.utime(a, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        if os.name == "posix":
            # Linux ctime 会因内容改写而变化 → stat 复核检出
            with self.assertRaises(v.Unsafe):
                v.apply(self.cfg, plan["plan_id"])
        else:
            # Windows st_ctime 是创建时间，原地改写无法用 stat 检出；
            # 防线是「只在 Linux 执行删除」+ 执行时全量 SHA 终检。
            v.apply(self.cfg, plan["plan_id"])
        self.assertTrue(a.exists())

    def test_missing_keeper_aborts(self):
        a, b, _, plan = self.make_plan()
        b.unlink()
        with self.assertRaises((v.Unsafe, OSError)):
            v.apply(self.cfg, plan["plan_id"])
        self.assertTrue(a.exists())

    def test_all_selected_rejected(self):
        self.copies()
        scan = v.inventory(self.cfg)
        with self.assertRaises(v.Unsafe):
            v.plan(self.cfg, scan["scan_id"], self.selections_for(scan, "all"))

    def test_reference_not_deletable(self):
        self.copies()
        scan = v.inventory(self.cfg)
        with self.assertRaises(v.Unsafe):
            v.plan(self.cfg, scan["scan_id"], self.selections_for(scan, "archive"))

    def test_selection_traversal_rejected(self):
        for value in ["../archive/x.mp4", "/etc/passwd", "a/b/../c.mp4"]:
            with self.subTest(value=value), self.assertRaises(v.Unsafe):
                v.selection("movies:" + value)
        with self.assertRaises(v.Unsafe):
            v.selection("no-colon-slash")

    def test_unknown_selection_rejected(self):
        self.copies()
        scan = v.inventory(self.cfg)
        with self.assertRaises(v.Unsafe):
            v.plan(self.cfg, scan["scan_id"], [{"root": "movies", "rel": "不存在.mp4"}])

    @unittest.skipUnless(os.name == "posix", "O_PATH / directory modes are POSIX-only")
    def test_anchored_survives_traverse_only_ancestor(self):
        # 真机场景：飞牛 ACL 对祖先卷只给 traverse(x) 不给 list(r)，
        # anchored 的中间目录必须用 O_PATH 打开（Errno 13 回归）。
        vol = self.base / "vol7"
        media = vol / "1000" / "media"
        media.mkdir(parents=True)
        target = media / "traverse-only.mp4"
        keeper = media / "keeper.mp4"
        keeper.write_bytes(b"same-bytes" * 4)
        target.write_bytes(keeper.read_bytes())  # 字节相同 → exact 组
        vol.chmod(0o111)  # 祖先卷 traverse-only：可穿过、不可列目录
        try:
            cfg = {"state_dir": str(self.base / "state2"), "roots": [{"id": "m", "path": str(media), "role": "cleanup", "exclude": []}]}
            cpath = self.base / "c2.json"
            v.atomic_json(cpath, cfg)
            cfg = v.read_config(cpath)
            scan = v.inventory(cfg)
            with v.state(cfg, exclusive=False) as db:
                idents = [k for g in v.load_groups(db, scan["scan_id"]) for k in g["members"]]
            self.assertEqual(len(idents), 2, "walk 应能穿过 traverse-only 祖先读到文件")
            selections = [{"root": k.split(":", 1)[0], "rel": k.split(":", 1)[1]}
                          for k in idents if "traverse-only" in k]
            with patch.object(v, "probe", return_value={"duration": 10, "tracks": [], "external_subtitles": []}):
                plan = v.plan(cfg, scan["scan_id"], selections)
            result = v.apply(cfg, plan["plan_id"], True, plan["plan_id"])
            self.assertEqual(result["deleted_files"], 1)
            self.assertFalse(target.exists())
            self.assertTrue(keeper.exists())
        finally:
            vol.chmod(0o755)  # 让 TemporaryDirectory 能清理

    def test_symlink_rejected(self):
        self.copies()
        path = self.media / "link.mp4"
        try:
            path.symlink_to(self.ref / "备份.mp4")
        except OSError:
            self.skipTest("OS does not permit symlink creation")
        self.assertEqual(v.inventory(self.cfg)["files"], 2)
        with self.assertRaises(v.Unsafe):
            v.file_path(self.cfg, {"root": "movies", "rel": "link.mp4"})

    def test_hardlink_protected(self):
        a, _ = self.copies()
        os.link(a, self.media / "alias.mp4")
        scan = v.inventory(self.cfg)
        with self.assertRaises(v.Unsafe):
            v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))

    def test_overlapping_roots_deduplicated_and_child_role_wins(self):
        child = self.media / "child"
        child.mkdir()
        (child / "a.mp4").write_bytes(b"a")
        self.cfg["roots"].append({"id": "child", "path": str(child), "role": "reference", "exclude": []})
        scan = v.inventory(self.cfg)
        self.assertEqual(scan["files"], 1)
        self.assertEqual(v.owner(self.cfg, child / "a.mp4")["id"], "child")

    def test_excluded_and_sidecars_not_in_inventory(self):
        self.copies()
        ignored = self.media / "ignored"
        ignored.mkdir()
        (ignored / "a.mp4").write_bytes(b"ignored")
        (self.media / "subtitle.srt").write_text("caption")
        self.assertEqual(v.inventory(self.cfg)["files"], 2)

    def test_changed_config_and_mount_rejected(self):
        _, _, _, plan = self.make_plan()
        self.cfg["roots"][0]["role"] = "reference"
        with self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"])

    def test_plan_tampering_rejected(self):
        _, _, _, plan = self.make_plan()
        path = Path(plan["plan"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["logical_bytes"] += 1
        v.atomic_json(path, data)
        with self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"])

    def test_replaced_root_directory_rejected(self):
        _, _, _, plan = self.make_plan()
        self.media.rename(self.base / "previous-media")
        self.media.mkdir()
        with self.assertRaisesRegex(v.Unsafe, "挂载目录"):
            v.apply(self.cfg, plan["plan_id"])

    def test_html_escapes_paths(self):
        path = self.base / "report.html"
        v.write_review(path, "<bad>", [{"Path": "</script><img src=x onerror=alert(1)>", "GroupId": "<group>"}])
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("<img src=x", text)
        self.assertNotIn("<script", text)

    def test_export_audit_written(self):
        self.copies()
        scan = v.inventory(self.cfg)
        self.assertTrue(Path(scan["report"]).exists())
        self.assertTrue(Path(scan["csv"]).exists())
        self.assertIn("GroupId", Path(scan["csv"]).read_text(encoding="utf-8-sig"))

    def test_unreadable_video_degrades_not_aborts(self):
        self.copies()
        (self.media / "broken.mp4").write_bytes(b"not a video at all")
        scan = v.inventory(self.cfg)
        self.assertEqual(scan["files"], 3)
        self.assertGreaterEqual(scan["fingerprint_errors"], 3)  # fake bytes: ffmpeg 无法解析
        self.assertEqual(scan["exact_groups"], 1)

    def test_cancel_stops_scan(self):
        self.copies()
        real_progress = v.write_progress
        def cancel_after_start(cfg, data):
            real_progress(cfg, data)
            v.cancel_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            v.cancel_path(cfg).touch()
        with patch.object(v, "write_progress", side_effect=cancel_after_start):
            result = v.inventory(self.cfg)
        self.assertTrue(result.get("cancelled"))
        self.assertFalse(v.cancel_path(self.cfg).exists())

    def test_fpk_setup_derives_roots_from_grants(self):
        env = {"TRIM_PKGETC": str(self.base / "etc"), "TRIM_PKGVAR": str(self.base / "var"),
               "TRIM_DATA_SHARE_PATHS": str(self.base / "share") if os.name != "posix" else str(self.base / "share"),
               "TRIM_DATA_ACCESSIBLE_PATHS": str(self.media)}
        with patch.dict(os.environ, env), patch.object(v.os, "access", return_value=True):
            result = v.fpk_setup()
        written = json.loads((self.base / "etc" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(written["state_dir"], str(self.base / "var"))
        self.assertEqual(written["export_dir"], str(self.base / "share"))
        self.assertEqual(len(written["roots"]), 1)
        self.assertEqual(written["roots"][0]["role"], "cleanup")
        self.assertRegex(written["roots"][0]["id"], r"^d[0-9a-f]{12}$")

    def test_fpk_setup_readonly_grant_is_reference(self):
        env = {"TRIM_PKGETC": str(self.base / "etc"), "TRIM_PKGVAR": str(self.base / "var"),
               "TRIM_DATA_SHARE_PATHS": "", "TRIM_DATA_ACCESSIBLE_PATHS": str(self.media)}
        with patch.dict(os.environ, env), patch.object(v.os, "access", return_value=False):
            result = v.fpk_setup()
        written = json.loads((self.base / "etc" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(written["roots"][0]["role"], "reference")

    def test_fpk_setup_empty_grants_writes_onboarding_config(self):
        env = {"TRIM_PKGETC": str(self.base / "etc"), "TRIM_PKGVAR": str(self.base / "var"),
               "TRIM_DATA_SHARE_PATHS": "", "TRIM_DATA_ACCESSIBLE_PATHS": ""}
        with patch.dict(os.environ, env):
            result = v.fpk_setup()
        written = json.loads((self.base / "etc" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(written["roots"], [])
        self.assertIn("访问权限", result["note"])

    def test_derive_roots_id_stable_across_order(self):
        env = {"TRIM_DATA_ACCESSIBLE_PATHS": f"{self.ref}:{self.media}" if os.name == "posix" else str(self.media)}
        with patch.dict(os.environ, env):
            first = {r["path"]: r["id"] for r in v.derive_roots()}
        env2 = dict(env, TRIM_DATA_ACCESSIBLE_PATHS=f"{self.media}:{self.ref}" if os.name == "posix" else str(self.media))
        with patch.dict(os.environ, env2):
            second = {r["path"]: r["id"] for r in v.derive_roots()}
        for path, rid in first.items():
            self.assertEqual(second[path], rid)

    def test_read_state_during_exclusive_lock(self):
        # 扫描线程持有 EX 锁时，UI 的读请求绝不能 EAGAIN（真机 Errno 11 回归）
        with v.state(self.cfg) as writer:
            with v.state(self.cfg, exclusive=False) as reader:
                self.assertEqual(reader.execute("SELECT count(*) FROM scans").fetchone()[0], 0)

    def test_dry_run_uses_cache(self):
        # 预演是长耗时只读校验，必须走 (path,stat) 缓存，不全量重读（大库 Errno 11/慢回访）。
        a, b = self.copies()
        scan = v.inventory(self.cfg)
        with patch.object(v, "probe", return_value={"duration": 10, "tracks": [], "external_subtitles": []}):
            plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))  # 首次 plan 填充 SHA 缓存
        with patch.object(v, "fingerprint", side_effect=AssertionError("dry-run 不应全量重读")):
            result = v.apply(self.cfg, plan["plan_id"])  # dry-run 预演
        self.assertEqual(result["remaining_files"], 1)

    @unittest.skipUnless(os.name == "posix", "permanent deletion is Linux-only")
    def test_execute_and_idempotent_retry(self):
        a, b, _, plan = self.make_plan()
        result = v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertEqual(result["deleted_files"], 1)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertEqual(v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])["deleted_files"], 0)

    def test_execute_requires_confirmation(self):
        a, _, _, plan = self.make_plan()
        with self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"], True, "wrong")
        self.assertTrue(a.exists())

    @unittest.skipUnless(os.name == "posix", "atomic claim is Linux-only")
    def test_atomic_claim_preserves_replacement(self):
        a, b, _, plan = self.make_plan()
        old = a.read_bytes()
        saved = a.with_name("saved-original.mp4")
        real_rename = os.rename
        changed = False
        def racing_rename(src, dst, **kwargs):
            nonlocal changed
            if src == a.name and not changed:
                changed = True
                real_rename(a, saved)
                a.write_bytes(b"replacement".ljust(len(old), b"!"))
            return real_rename(src, dst, **kwargs)
        with patch.object(os, "rename", side_effect=racing_rename), self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertEqual(saved.read_bytes(), old)
        self.assertTrue(a.read_bytes().startswith(b"replacement"))
        self.assertTrue(b.exists())

    @unittest.skipUnless(os.name == "posix", "permanent deletion is Linux-only")
    def test_crash_after_unlink_recovers_journal(self):
        a, b, _, plan = self.make_plan()
        real_write = v.atomic_json
        def crash(path, data):
            if str(path).endswith(".journal.json") and data["events"]:
                raise KeyboardInterrupt()
            return real_write(path, data)
        with patch.object(v, "atomic_json", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertEqual(v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])["deleted_files"], 0)

    @unittest.skipUnless(os.name == "posix", "permanent deletion is Linux-only")
    def test_mid_batch_error_preserves_remaining_and_stops(self):
        a, b = self.copies()
        second = self.media / "second.mp4"
        second.write_bytes(a.read_bytes())
        scan = v.inventory(self.cfg)
        with patch.object(v, "probe", return_value={"duration": 10, "tracks": [], "external_subtitles": []}):
            plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))
        document = json.loads(Path(plan["plan"]).read_text(encoding="utf-8"))
        targets = document["groups"][0]["delete"]
        failed_name = Path(targets[1]["rel"]).name
        real_rename = os.rename
        def fail_second(src, dst, **kwargs):
            if src == failed_name:
                raise PermissionError("simulated denial")
            return real_rename(src, dst, **kwargs)
        with patch.object(os, "rename", side_effect=fail_second), self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertTrue(b.exists())
        self.assertEqual(sum(p.exists() for p in (a, second)), 1)
        self.assertTrue((self.media / failed_name).exists())


class PhashTests(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("numpy"), "numpy required")
    def test_phash_similarity(self):
        # 合成 32x32 灰度帧：平滑渐变图 vs 微扰图 vs 反相图
        base = bytearray()
        for y in range(32):
            for x in range(32):
                base.append((x * 8 + y * 4) % 256)
        similar = bytearray(base)
        for i in range(0, 1024, 37):
            similar[i] = (similar[i] + 12) % 256
        inverse = bytes(255 - b for b in base)
        h1, h2, h3 = v.phash(bytes(base)), v.phash(bytes(similar)), v.phash(inverse)
        self.assertEqual(v.hamming(h1, h2) <= 6, True, "微扰图距离应很小")
        self.assertGreater(v.hamming(h1, h3), 20, "反相图距离应很大")

    def test_hamming(self):
        self.assertEqual(v.hamming(0, 0), 0)
        self.assertEqual(v.hamming(0, (1 << 64) - 1), 64)
        self.assertEqual(v.hamming(0b1010, 0b0110), 2)


if __name__ == "__main__":
    unittest.main()

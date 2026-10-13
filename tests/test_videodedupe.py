import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import videodedupe as v
import tasks


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
        plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))
        return a, b, scan, plan

    def test_exact_and_unicode_and_dry_run(self):
        a, b, scan, plan = self.make_plan()
        self.assertEqual(scan["exact_groups"], 1)
        result = v.apply(self.cfg, plan["plan_id"])
        self.assertEqual(result["remaining_files"], 1)
        self.assertTrue(a.exists() and b.exists())
        exports = v.export_audit(self.cfg, scan["scan_id"])  # §四F 报告是显式导出操作
        self.assertIn("字节完全重复", Path(exports["report"]).read_text(encoding="utf-8"))

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
            plan = v.plan(cfg, scan["scan_id"], selections)
            # 默认封口（§四A）：删除机制用例显式开门，只测 anchored 行为本身
            with patch.object(v, "require_cleanup_available"):
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

    def test_scan_scope_rejects_bad_entries(self):
        self.copies()
        for scope in (["movies:/etc/passwd"],                          # 绝对路径
                      [{"root": "movies", "rel": "/etc/passwd"}],      # dict 形式同样拒绝
                      ["movies:../escape"],                            # .. 越界
                      ["movies:a\\b"],                                 # 反斜杠含糊路径
                      ["ghost:sub"]):                                  # 未授权根 id
            with self.subTest(scope=scope), self.assertRaises(v.Unsafe):
                v.scan_scope(dict(self.cfg, scan_scope=scope))
        link = self.media / "linkdir"
        try:
            link.symlink_to(self.ref)
        except OSError:
            self.skipTest("OS does not permit symlink creation")
        with self.assertRaises(v.Unsafe):
            v.scan_scope(dict(self.cfg, scan_scope=["movies:linkdir"]))

    def test_scan_scope_parent_child_dedup_and_stored(self):
        sub = self.media / "A"
        (sub / "B").mkdir(parents=True)
        (sub / "y.mp4").write_bytes(b"y")
        (sub / "B" / "x.mp4").write_bytes(b"x")
        cfg = dict(self.cfg, scan_scope=["movies:A/B", "movies:A"])  # 子先父后，输出仍应只剩父
        self.assertEqual(v.scan_scope(cfg), [{"root": "movies", "rel": "A", "path": sub}])
        scan = v.inventory(cfg)
        self.assertEqual(scan["files"], 2)  # A 与 A/B 各一个视频，父起点只走一遍
        with v.state(self.cfg, exclusive=False) as db:
            self.assertEqual(v.load_scope(db, scan["scan_id"]), [{"root": "movies", "rel": "A"}])

    def test_scan_scope_whole_root_selectable(self):
        # 真机验收 26.10.13 反馈2：母文件夹（授权根本身）可勾选——空 rel 表示
        # 整个根；与子选择混用时根覆盖子；dict 形式与字符串形式同义。
        sub = self.media / "A"
        (sub / "B").mkdir(parents=True)
        (sub / "y.mp4").write_bytes(b"y")
        (sub / "B" / "x.mp4").write_bytes(b"x")
        (self.media / "top.mp4").write_bytes(b"t")
        cfg = dict(self.cfg, scan_scope=["movies:", "movies:A/B"])
        self.assertEqual(v.scan_scope(cfg), [{"root": "movies", "rel": "", "path": self.media}])
        self.assertEqual(v.scan_scope(dict(self.cfg, scan_scope=[{"root": "movies", "rel": ""}])),
                         [{"root": "movies", "rel": "", "path": self.media}])
        scan = v.inventory(cfg)
        self.assertEqual(scan["files"], 3)  # 整根 = 根下全部视频
        with v.state(self.cfg, exclusive=False) as db:
            self.assertEqual(v.load_scope(db, scan["scan_id"]), [{"root": "movies", "rel": ""}])
        # 只勾另一根的整根：不影响未勾选的根
        scan2 = v.inventory(dict(self.cfg, scan_scope=["archive:"]))
        self.assertEqual(scan2["files"], 0)
        with v.state(self.cfg, exclusive=False) as db:
            self.assertEqual(v.load_scope(db, scan2["scan_id"]), [{"root": "archive", "rel": ""}])

    def test_scan_scope_same_named_subdir_scans_only_selected_root(self):
        for root in (self.media, self.ref):
            (root / "dup").mkdir()
            (root / "dup" / "v.mp4").write_bytes(b"same")
        cfg = dict(self.cfg, scan_scope=["movies:dup"])
        scan = v.inventory(cfg)
        self.assertEqual(scan["files"], 1)  # archive/dup 同名目录不得被顺带扫描
        with v.state(self.cfg, exclusive=False) as db:
            captured = v.load_scan(db, self.cfg, scan["scan_id"])
            self.assertEqual(list(captured), ["movies:dup/v.mp4"])
            self.assertEqual(v.load_scope(db, scan["scan_id"]), [{"root": "movies", "rel": "dup"}])
        full = v.inventory(self.cfg)  # 不带范围 = 全部授权目录，结果存 None
        self.assertEqual(full["files"], 2)
        with v.state(self.cfg, exclusive=False) as db:
            self.assertIsNone(v.load_scope(db, full["scan_id"]))

    def test_live_metrics_in_progress_payload(self):
        # 真机验收 26.10.13 反馈1：指纹阶段 progress.json 携带实时重复指标
        # （已识别部分的下界），供概览四卡与轮询直接取用
        a, _ = self.copies()
        captured, original = [], v.write_progress

        def record(cfg, data):
            captured.append(data)
            original(cfg, data)

        with patch.object(v, "write_progress", record):
            v.inventory(self.cfg)
        fps = [d for d in captured if d.get("phase") == "fingerprint" and d.get("done") == 2]
        self.assertTrue(fps)
        payload = fps[-1]
        self.assertEqual(payload["dup_groups"], 1)
        self.assertEqual(payload["dup_clean"], 1)  # archive 副本受保护，media 副本建议清理
        self.assertEqual(payload["dup_bytes"], a.stat().st_size)
        self.assertEqual(payload["found"], 2)

    def test_live_groups_and_files_readers(self):
        # live 通道（进行中扫描只读预览）：编号形状校验 + 与完成态同一行结构
        self.copies()
        scan = v.inventory(self.cfg)
        with self.assertRaises(v.Unsafe):
            v.live_groups(None, "not-a-scan-id")
        with v.state(self.cfg, exclusive=False) as db:
            groups = v.live_groups(db, scan["scan_id"])
            self.assertEqual(len(groups), 1)
            keys = groups[0]["members"]
            captured = v.live_files(db, scan["scan_id"], keys)
            self.assertEqual(set(captured), set(keys))

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

    def test_added_root_keeps_old_scan_viewable(self):
        # 真机验收 26.10.13 反馈3：新增授权文件夹后，旧扫描的审阅记录必须
        # 仍可查看（逐根比对，不做全量身份比对）；移除/替换旧根才拒绝。
        self.copies()
        scan = v.inventory(self.cfg)
        extra = self.base / "extra"
        extra.mkdir()
        grown = json.loads(self.config.read_text(encoding="utf-8"))
        grown["roots"].append({"id": "extra", "path": str(extra), "role": "cleanup", "exclude": []})
        v.atomic_json(self.config, grown)
        grown = v.read_config(self.config)
        with v.state(grown, exclusive=False) as db:
            self.assertTrue(v.load_scan(db, grown, scan["scan_id"]))  # 查看不再被拒
            draft = v.load_draft(db, grown, scan["scan_id"])
            self.assertEqual(draft, {"rev": 0, "groups": {}})
        # 新根目录下放进旧扫描同款文件，也不得污染旧结果（新扫描才有新组）
        (extra / "again.mp4").write_bytes(b"video-a" * 20)
        with v.state(grown, exclusive=False) as db:
            self.assertEqual(len(v.load_groups(db, scan["scan_id"])), 1)
        # 移除任一扫描时的根 → 拒绝
        shrunk = json.loads(self.config.read_text(encoding="utf-8"))
        shrunk["roots"] = [r for r in shrunk["roots"] if r["id"] != "archive"]
        v.atomic_json(self.config, shrunk)
        shrunk = v.read_config(self.config)
        with v.state(shrunk, exclusive=False) as db:
            with self.assertRaisesRegex(v.Unsafe, "移除/替换"):
                v.load_scan(db, shrunk, scan["scan_id"])

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
        with self.assertRaisesRegex(v.Unsafe, "移除/替换"):
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
        # §四F 解耦契约：扫描结果只承载业务摘要，报告路径由显式导出返回
        self.assertNotIn("report", scan)
        self.assertNotIn("csv", scan)
        exports = v.export_audit(self.cfg, scan["scan_id"])
        self.assertTrue(Path(exports["report"]).exists())
        self.assertTrue(Path(exports["csv"]).exists())
        self.assertIn("GroupId", Path(exports["csv"]).read_text(encoding="utf-8-sig"))

    def test_report_unit_conversion_and_real_paths(self):
        # Review 26.10.12 P2：ffprobe bit_rate 单位是 bit/s，BitrateKbs 列承诺
        # Kb/s——2456891 bit/s 必须换算为 2456.9，缺失/异常写空不伪装。
        self.assertEqual(v.bitrate_kbs({"bitrate": "2456891"}), 2456.9)
        self.assertIsNone(v.bitrate_kbs({}))
        self.assertIsNone(v.bitrate_kbs({"bitrate": "n/a"}))
        # 报告路径是用户可见的授权目录真实位置，不再是 /media/<root_id>/
        self.assertEqual(v.display_path(self.cfg, "movies", "a.mp4"), str(self.media) + "/a.mp4")

    def test_export_audit_real_path_and_empty_meta(self):
        self.copies()  # 假字节：ffmpeg 无法解析 → 元数据缺失而非 0/None 字面量
        scan = v.inventory(self.cfg)
        exports = v.export_audit(self.cfg, scan["scan_id"])
        csv_text = Path(exports["csv"]).read_text(encoding="utf-8-sig")
        self.assertNotIn("/media/", csv_text)
        self.assertIn(str(self.media), csv_text)
        self.assertIn("?x?", csv_text)  # 分辨率缺失：?x?，而不是 NonexNone
        html_text = Path(exports["report"]).read_text(encoding="utf-8")
        self.assertNotIn("None", html_text)  # 空值显示为 -，时长缺失不伪装成 0
        self.assertIn("NAS 路径", html_text)
        self.assertIn("时长（秒）", html_text)  # 数值列标注单位

    def test_report_viewport_attribute_closed(self):
        path = self.base / "report.html"
        v.write_review(path, "t", [{"Path": "a.mp4", "Duration": None}])
        text = path.read_text(encoding="utf-8")
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', text)
        self.assertNotIn("None", text)  # 缺失显示 -，不暴露 Python 字面量

    def test_plan_report_real_paths(self):
        _, _, _, plan = self.make_plan()
        text = Path(plan["report"]).read_text(encoding="utf-8")
        self.assertNotIn("/media/", text)
        self.assertIn(str(self.media) + "/", text)
        self.assertNotIn("None", text)

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

    def test_fpk_setup_missing_env_signals_failure(self):
        # cmd/main 靠进程退出码熔断：缺环境变量必须是明确非零退出（Review P1）
        env = {"TRIM_PKGETC": "", "TRIM_PKGVAR": ""}
        with patch.dict(os.environ, env):
            self.assertEqual(v.main(["fpk-setup"]), 1)

    def test_fpk_setup_refresh_failure_keeps_old_config_intact(self):
        # 刷新失败熔断后旧配置必须原样保留，修复后重启可继续派生（Review §四F）
        etc = self.base / "etc"
        etc.mkdir(parents=True, exist_ok=True)
        old = etc / "config.json"
        old.write_text('{"legacy": true}', encoding="utf-8")
        env = {"TRIM_PKGETC": str(etc), "TRIM_PKGVAR": str(self.base / "var"),
               "TRIM_DATA_SHARE_PATHS": "", "TRIM_DATA_ACCESSIBLE_PATHS": str(self.media)}
        with patch.dict(os.environ, env), patch.object(v, "atomic_json", side_effect=OSError("disk full")):
            self.assertEqual(v.main(["fpk-setup"]), 1)
        self.assertEqual(old.read_text(encoding="utf-8"), '{"legacy": true}')

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
        plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))  # 首次 plan 填充 SHA 缓存
        with patch.object(v, "fingerprint", side_effect=AssertionError("dry-run 不应全量重读")):
            result = v.apply(self.cfg, plan["plan_id"])  # dry-run 预演
        self.assertEqual(result["remaining_files"], 1)

    @unittest.skipUnless(os.name == "posix", "permanent deletion is Linux-only")
    def test_execute_and_idempotent_retry(self):
        a, b, _, plan = self.make_plan()
        # 默认封口（§四A）：真实清理在引擎层被 require_cleanup_available 拒绝；
        # 删除机制用例（原子暂存/journal/幂等）在此显式开门，不删用例不加 skip。
        with patch.object(v, "require_cleanup_available"):
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

    def test_execute_gate_blocks_at_engine_level(self):
        # §四A 引擎层封口：execute=True 无条件拒绝（双平台一致，先于 Linux/
        # confirm 校验与任务占用）；真实执行体不被触碰。预演不受影响（由
        # test_exact_and_unicode_and_dry_run / test_dry_run_uses_cache 覆盖）。
        with patch.object(v, "_apply") as executor:
            with self.assertRaises(v.Unsafe) as ctx:
                v.apply(self.cfg, "a" * 64, True, "a" * 64)
            executor.assert_not_called()
        self.assertIn("回收站", str(ctx.exception))

    def test_execute_gate_creates_no_apply_task_row(self):
        # 拒绝发生在占用任务之前——tasks 表不出现 apply 任务行
        with self.assertRaises(v.Unsafe):
            v.apply(self.cfg, "a" * 64, True, "a" * 64)
        self.assertIsNone(tasks.latest(self.cfg, "apply"))

    def test_apply_body_gated_too(self):
        # 绕过 apply 直接调真实执行体同样被拒：门禁先于计划文件读取
        #（'a'*64 无计划文件，若无门禁此处是 FileNotFoundError 而非 Unsafe）。
        with self.assertRaises(v.Unsafe) as ctx:
            v._apply(self.cfg, "a" * 64, True)
        self.assertIn("回收站", str(ctx.exception))

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
        with patch.object(v, "require_cleanup_available"), \
             patch.object(os, "rename", side_effect=racing_rename), self.assertRaises(v.Unsafe):
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
        with patch.object(v, "require_cleanup_available"), \
             patch.object(v, "atomic_json", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        with patch.object(v, "require_cleanup_available"):
            self.assertEqual(v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])["deleted_files"], 0)

    @unittest.skipUnless(os.name == "posix", "permanent deletion is Linux-only")
    def test_mid_batch_error_preserves_remaining_and_stops(self):
        a, b = self.copies()
        second = self.media / "second.mp4"
        second.write_bytes(a.read_bytes())
        scan = v.inventory(self.cfg)
        plan = v.plan(self.cfg, scan["scan_id"], self.selections_for(scan))
        document = json.loads(Path(plan["plan"]).read_text(encoding="utf-8"))
        targets = document["groups"][0]["delete"]
        failed_name = Path(targets[1]["rel"]).name
        real_rename = os.rename
        def fail_second(src, dst, **kwargs):
            if src == failed_name:
                raise PermissionError("simulated denial")
            return real_rename(src, dst, **kwargs)
        with patch.object(v, "require_cleanup_available"), \
             patch.object(os, "rename", side_effect=fail_second), self.assertRaises(v.Unsafe):
            v.apply(self.cfg, plan["plan_id"], True, plan["plan_id"])
        self.assertTrue(b.exists())
        self.assertEqual(sum(p.exists() for p in (a, second)), 1)
        self.assertTrue((self.media / failed_name).exists())


class MatchRuleTests(unittest.TestCase):
    """匹配规则统一与子组保留（Review 26.10.12 P0/P1/§四C）。

    指纹管线（probe_media/visual_fingerprint/thumbnail）全部 patch，不依赖
    ffmpeg：扫描、审计导出与计划复核走同一条 pair_match 口径。
    连通链哈希：d(X,Y)=6、d(Y,Z)=7、d(X,Z)=11——A≈B≈C 而 A≉C。"""

    CHAIN_X = 0
    CHAIN_Y = (1 << 10) | 0b11111          # bits {0..4, 10}
    CHAIN_Z = 0b11111 | sum(1 << b for b in (20, 21, 22, 23, 24, 30))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)
        self.spec = {}  # 文件名 -> (duration, vhash)

    def tearDown(self):
        self.tmp.cleanup()

    def add_file(self, name, data, duration, vhash):
        (self.media / name).write_bytes(data)
        self.spec[name] = (duration, vhash)

    def patched(self):
        def probe_media(cfg, path):
            if path.name not in self.spec:
                raise v.Unsafe("没有可读取的视频流")
            duration, _ = self.spec[path.name]
            return {"duration": duration, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        def visual_fingerprint(cfg, path, duration):
            vh = self.spec[path.name][1]
            if vh is None:  # 模拟抽帧失败：vhash 缺失、vhash_error 记录原因（§四C 能力分级）
                raise ValueError("抽帧失败")
            return [vh] * len(v.FRAME_POSITIONS)

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(v, "probe_media", probe_media))
        stack.enter_context(patch.object(v, "visual_fingerprint", visual_fingerprint))
        stack.enter_context(patch.object(v, "thumbnail", return_value="t.jpg"))
        return stack

    def select(self, *names):
        return [{"root": "movies", "rel": n} for n in names]

    def groups_of(self, scan):
        with v.state(self.cfg, exclusive=False) as db:
            return v.load_groups(db, scan["scan_id"])

    def plan_document(self, plan):
        return json.loads(Path(plan["plan"]).read_text(encoding="utf-8"))

    def test_pair_match_pure_function(self):
        # sha 优先：字节一致即匹配，时长/画面不参与
        a = {"sha256": "s1", "vhash": [0] * 5, "meta": {"duration": 100.0}}
        b = {"sha256": "s1", "vhash": [self.CHAIN_Y] * 5, "meta": {"duration": 999.0}}
        self.assertEqual(v.pair_match(a, b), {"basis": "sha256"})
        # 时长窗口内画面达标 → phash 依据；窗口/阈值外 → None
        c = {"sha256": "s2", "vhash": [0] * 5, "meta": {"duration": 100.0}}
        d = {"sha256": None, "vhash": [self.CHAIN_Y] * 5, "meta": {"duration": 104.5}}
        self.assertEqual(v.pair_match(c, d), {"basis": "phash", "distance": 6.0})
        e = {"sha256": None, "vhash": [self.CHAIN_Y] * 5, "meta": {"duration": 106.0}}
        self.assertIsNone(v.pair_match(c, e))  # Δ6 > max(5.0, 100*0.02)
        f = {"sha256": None, "vhash": None, "meta": {"error": "decode failed"}}
        self.assertIsNone(v.pair_match(c, f))  # 降级文件无视觉指纹
        g = {"sha256": None, "vhash": [self.CHAIN_Z] * 5, "meta": {"duration": 100.0}}
        self.assertIsNone(v.pair_match(c, g))  # d=11 > SIMILAR_MEAN_DISTANCE

    def test_chain_group_candidate_and_direct_match_keeper(self):
        # A≈B≈C 而 A≉C：连通成一个 visual 候选组；删 B（保留 A、C）时 B 与
        # A/C 都直接匹配 → 计划成立并记录 phash 依据
        self.add_file("a.mp4", b"a" * 160, 100.0, self.CHAIN_X)
        self.add_file("b.mp4", b"b" * 200, 100.0, self.CHAIN_Y)
        self.add_file("c.mp4", b"c" * 240, 100.0, self.CHAIN_Z)
        with self.patched():
            scan = v.inventory(self.cfg)
            plan = v.plan(self.cfg, scan["scan_id"], self.select("b.mp4"))
        groups = self.groups_of(scan)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "visual")
        self.assertEqual(groups[0]["exacts"], [])  # 无字节一致子组
        delete = self.plan_document(plan)["groups"][0]["delete"]
        self.assertEqual(len(delete), 1)
        self.assertEqual(delete[0]["match"]["basis"], "phash")
        self.assertIn(delete[0]["match"]["keep"], ("movies:a.mp4", "movies:c.mp4"))

    def test_chain_group_without_direct_match_rejected_before_plan(self):
        # 删 A、B 只保留 C：A 与 C 不直接匹配（仅连通）→ 生成计划前拒绝并点名
        self.add_file("a.mp4", b"a" * 160, 100.0, self.CHAIN_X)
        self.add_file("b.mp4", b"b" * 200, 100.0, self.CHAIN_Y)
        self.add_file("c.mp4", b"c" * 240, 100.0, self.CHAIN_Z)
        with self.patched():
            scan = v.inventory(self.cfg)
            with self.assertRaises(v.Unsafe) as caught:
                v.plan(self.cfg, scan["scan_id"], self.select("a.mp4", "b.mp4"))
        self.assertIn("movies:a.mp4", str(caught.exception))
        self.assertIn("直接匹配", str(caught.exception))
        self.assertFalse(list((Path(self.cfg["state_dir"])).glob("*.plan.json")))

    def test_chain_group_audit_flags_indirect_member(self):
        # 审计报告在扫描阶段就展示直接匹配情况（提前暴露不可处理的清理项）：
        # 链组不是两两相似，A 的直接匹配对象是 B——删 A 就必须保留 B 而非 C
        self.add_file("a.mp4", b"a" * 160, 100.0, self.CHAIN_X)
        self.add_file("b.mp4", b"b" * 200, 100.0, self.CHAIN_Y)
        self.add_file("c.mp4", b"c" * 240, 100.0, self.CHAIN_Z)
        with self.patched():
            scan = v.inventory(self.cfg)
            exports = v.export_audit(self.cfg, scan["scan_id"])
        report = Path(exports["report"]).read_text(encoding="utf-8")
        self.assertIn("存在不直接匹配的成员对", report)
        self.assertIn("pHash 距离(直接匹配)", report)
        self.assertIn("与 movies:b.mp4 直接匹配", report)  # A、C 的直接匹配对象都是 B

    def test_export_audit_marks_missing_vhash_member(self):
        # §四F：混合组成员缺视觉指纹（与 a1 字节一致但抽帧失败，仅凭 SHA 证据
        # 入组）——导出不崩溃，缺 vhash 成员如实标注，直接匹配按字节一致说明。
        self.add_file("a1.mp4", b"A" * 160, 100.0, self.CHAIN_X)
        self.add_file("a2.mp4", b"A" * 160, 100.0, None)
        self.add_file("b.mp4", b"B" * 200, 100.0, self.CHAIN_Y)
        with self.patched():
            scan = v.inventory(self.cfg)
            exports = v.export_audit(self.cfg, scan["scan_id"])  # 不抛异常
        groups = self.groups_of(scan)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "visual")  # {sha_A, None} 两种 → 视觉候选组
        csv_text = Path(exports["csv"]).read_text(encoding="utf-8-sig")
        self.assertEqual(csv_text.count("\n"), 4)  # 表头 + 3 名成员都在
        report = Path(exports["report"]).read_text(encoding="utf-8")
        self.assertIn("缺失：该文件未能计算视觉指纹，不参与画面比对", report)
        self.assertIn("与 movies:a1.mp4 直接匹配（字节一致）", report)  # a2 的直接匹配对象与依据

    def test_mixed_group_keeps_exact_subgroups(self):
        # 混合组：两对字节完全重复（A1=A2、B1=B2）经画面相似连通成一个
        # visual 候选组；子组入库存档，删除项以 sha 依据对准同子组保留对象
        self.add_file("a1.mp4", b"A" * 160, 100.0, self.CHAIN_X)
        self.add_file("a2.mp4", b"A" * 160, 100.0, self.CHAIN_X)
        self.add_file("b1.mp4", b"B" * 160, 100.0, self.CHAIN_Y)
        self.add_file("b2.mp4", b"B" * 160, 100.0, self.CHAIN_Y)
        with self.patched():
            scan = v.inventory(self.cfg)
            plan = v.plan(self.cfg, scan["scan_id"], self.select("a1.mp4", "b1.mp4"))
        groups = self.groups_of(scan)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "visual")  # 两种 sha 经视觉相似连通
        self.assertEqual(sorted(groups[0]["exacts"]),
                         sorted([["movies:a1.mp4", "movies:a2.mp4"], ["movies:b1.mp4", "movies:b2.mp4"]]))
        matches = {d["rel"]: d["match"] for d in self.plan_document(plan)["groups"][0]["delete"]}
        self.assertEqual(matches["a1.mp4"], {"keep": "movies:a2.mp4", "basis": "sha256"})
        self.assertEqual(matches["b1.mp4"], {"keep": "movies:b2.mp4", "basis": "sha256"})

    def test_duration_tolerance_identical_on_both_sides(self):
        # P1 回归：Δ4.5s 在扫描窗口 max(5.0, 100*2%) 内成组；计划原先的
        # min(5, 2%)=2.09 口径会拒绝，统一后两侧一致、计划成立
        self.add_file("short.mp4", b"s" * 160, 100.0, self.CHAIN_X)
        self.add_file("long.mp4", b"l" * 200, 104.5, self.CHAIN_Y)
        with self.patched():
            scan = v.inventory(self.cfg)
            self.assertEqual(scan["visual_groups"], 1)
            plan = v.plan(self.cfg, scan["scan_id"], self.select("long.mp4"))
        delete = self.plan_document(plan)["groups"][0]["delete"]
        self.assertEqual(delete[0]["match"]["basis"], "phash")

    def test_duration_window_boundary_not_grouped(self):
        # Δ5.5s 超出窗口：扫描不成组，计划无从选择（口径一致的反向边界）
        self.add_file("short.mp4", b"s" * 160, 100.0, self.CHAIN_X)
        self.add_file("long.mp4", b"l" * 200, 105.5, self.CHAIN_Y)
        with self.patched():
            scan = v.inventory(self.cfg)
        self.assertEqual(scan["visual_groups"], 0)
        self.assertEqual(self.groups_of(scan), [])

    def test_duration_window_scales_with_length(self):
        # 长视频走百分比分支：Δ7s ≤ max(5.0, 400*2%)=8s → 成组且计划成立
        self.add_file("u.mp4", b"u" * 160, 400.0, self.CHAIN_X)
        self.add_file("w.mp4", b"w" * 200, 407.0, self.CHAIN_Y)
        with self.patched():
            scan = v.inventory(self.cfg)
            self.assertEqual(scan["visual_groups"], 1)
            plan = v.plan(self.cfg, scan["scan_id"], self.select("w.mp4"))
        self.assertEqual(self.plan_document(plan)["groups"][0]["delete"][0]["match"]["basis"], "phash")

    def test_legacy_group_rows_recompute_exacts(self):
        # 旧库迁移：groups 行缺 exacts（NULL）时按 sha 现场重算同一分桶
        self.add_file("a1.mp4", b"A" * 160, 100.0, self.CHAIN_X)
        self.add_file("a2.mp4", b"A" * 160, 100.0, self.CHAIN_X)
        with self.patched():
            scan = v.inventory(self.cfg)
        with v.state(self.cfg) as db:
            db.execute("UPDATE groups SET exacts=NULL WHERE scan=?", (scan["scan_id"],))
            db.commit()
            reloaded = v.load_groups(db, scan["scan_id"])
        self.assertIsNone(reloaded[0]["exacts"])  # 存储值 NULL
        with v.state(self.cfg, exclusive=False) as db:
            captured = v.load_scan(db, self.cfg, scan["scan_id"])
        recomputed = v.exact_subgroups([captured[k] for k in reloaded[0]["members"]], reloaded[0]["exacts"])
        self.assertEqual(recomputed, [["movies:a1.mp4", "movies:a2.mp4"]])


class BatchSuggestionTests(unittest.TestCase):
    """完全相同文件的批量建议（Review 26.10.12 §三.3 / P0：移除「最小文件=建议保留」）。

    指纹管线 patch 同 MatchRuleTests（不依赖 ffmpeg）。建议规则：
    优先保留受保护副本（只读目录/硬链接）；否则按 文件夹路径+文件名 的稳定顺序
    保留第一份（不用大小推断质量——完全一致的文件大小本就相同）；外挂字幕不一致
    或读取异常的子组跳过并列出原因；混合相似组只处理各自 SHA 子组。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.ref = self.base / "ref"
        self.media.mkdir()
        self.ref.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []},
                              {"id": "vault", "path": str(self.ref), "role": "reference", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)
        self.spec = {}  # 绝对路径 -> (duration, vhash)

    def tearDown(self):
        self.tmp.cleanup()

    def add_file(self, root, name, data, duration=100.0, vhash=0):
        path = (self.media if root == "movies" else self.ref) / name
        path.write_bytes(data)
        self.spec[str(path)] = (duration, vhash)

    def patched(self):
        def probe_media(cfg, path):
            if str(path) not in self.spec:
                raise v.Unsafe("没有可读取的视频流")
            duration, _ = self.spec[str(path)]
            return {"duration": duration, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        def visual_fingerprint(cfg, path, duration):
            vh = self.spec[str(path)][1]
            if vh is None:
                raise ValueError("抽帧失败")
            return [vh] * len(v.FRAME_POSITIONS)

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(v, "probe_media", probe_media))
        stack.enter_context(patch.object(v, "visual_fingerprint", visual_fingerprint))
        stack.enter_context(patch.object(v, "thumbnail", return_value="t.jpg"))
        return stack

    def suggest(self, scan):
        with v.state(self.cfg, exclusive=False) as db:
            captured = v.load_scan(db, self.cfg, scan["scan_id"])
            groups = v.load_groups(db, scan["scan_id"])
        return v.suggestions(self.cfg, captured, groups)

    def one(self, scan):
        result = self.suggest(scan)
        self.assertEqual(len(result), 1)
        return result[0]

    def test_stable_order_keeps_first_by_path_and_name(self):
        # 无受保护副本：按文件夹路径+文件名稳定顺序保留第一份，其余为建议清理项；
        # 理由如实展示，重复计算结果一致（默认是否勾选由审阅页决定，建议只给名单）
        self.add_file("movies", "zeta.mp4", b"A" * 160)
        self.add_file("movies", "alpha.mp4", b"A" * 160)
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["kind"], "exact")
        self.assertEqual(s["clean"], ["movies:zeta.mp4"])
        self.assertEqual(list(s["keep"]), ["movies:alpha.mp4"])
        self.assertIn("文件夹路径与文件名", s["keep"]["movies:alpha.mp4"])
        self.assertEqual(s["skipped"], [])
        self.assertEqual(s["manual"], [])
        self.assertEqual(self.one(scan), s)  # 顺序稳定：重复计算结果一致

    def test_protected_copy_preferred_over_order(self):
        # 优先保留已有受保护副本：只读目录里的副本成为保留对象并给出理由，
        # 可清理目录的副本才是建议清理项——不受路径顺序影响
        self.add_file("movies", "alpha.mp4", b"A" * 160)
        self.add_file("vault", "zzz.mp4", b"A" * 160)
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["clean"], ["movies:alpha.mp4"])
        self.assertEqual(list(s["keep"]), ["vault:zzz.mp4"])
        self.assertIn("受保护", s["keep"]["vault:zzz.mp4"])

    def test_hardlinked_members_stay_protected(self):
        # 硬链接双方（nlink>1）都不可删：整组保留、不产生清理建议是正常决定
        a = self.media / "a.mp4"
        a.write_bytes(b"A" * 160)
        os.link(a, self.media / "b.mp4")
        self.spec[str(a)] = self.spec[str(self.media / "b.mp4")] = (100.0, 0)
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["clean"], [])
        self.assertEqual(sorted(s["keep"]), ["movies:a.mp4", "movies:b.mp4"])
        for reason in s["keep"].values():
            self.assertIn("受保护", reason)

    def test_mixed_group_suggestions_stay_inside_sha_subgroups(self):
        # 混合相似组（A1=A2、B1=B2 两对字节一致 + 与 B 画面相似但内容不同的 c）：
        # 建议只落在各自 SHA 子组内（各组保留第一份、清理重复份），转码版本归
        # manual，绝不顺带标记
        chain_y = (1 << 10) | 0b11111
        self.add_file("movies", "a1.mp4", b"A" * 160, vhash=0)
        self.add_file("movies", "a2.mp4", b"A" * 160, vhash=0)
        self.add_file("movies", "b1.mp4", b"B" * 160, vhash=chain_y)
        self.add_file("movies", "b2.mp4", b"B" * 160, vhash=chain_y)
        self.add_file("movies", "c.mp4", b"C" * 160, vhash=chain_y)
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["kind"], "visual")
        self.assertEqual(s["clean"], ["movies:a2.mp4", "movies:b2.mp4"])  # 不含 c.mp4
        self.assertEqual(sorted(s["keep"]), ["movies:a1.mp4", "movies:b1.mp4"])
        self.assertEqual(s["manual"], ["movies:c.mp4"])

    def test_subtitle_difference_skips_subgroup_with_reason(self):
        # 一个副本带外挂字幕、另一个没有：删除可能丢失字幕 → 整个子组跳过批量
        # 处理并列出原因；双方各有一份同后缀字幕（stem 各自对应）不算差异
        self.add_file("movies", "alpha.mp4", b"A" * 160)
        self.add_file("movies", "zeta.mp4", b"A" * 160)
        (self.media / "alpha.srt").write_text("1", encoding="utf-8")
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["clean"], [])
        self.assertEqual(s["keep"], {})
        self.assertEqual(s["skipped"], [{"members": ["movies:alpha.mp4", "movies:zeta.mp4"],
                                         "reason": s["skipped"][0]["reason"]}])
        self.assertIn("外挂字幕不一致", s["skipped"][0]["reason"])
        self.assertIn("需人工判断", s["skipped"][0]["reason"])
        (self.media / "zeta.srt").write_text("1", encoding="utf-8")
        with self.patched():
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["skipped"], [])
        self.assertEqual(s["clean"], ["movies:zeta.mp4"])

    def test_metadata_error_skips_subgroup(self):
        # 元数据读取异常的成员：不足以放心批量处理 → 子组跳过并列出成员与原因
        self.add_file("movies", "ok.mp4", b"A" * 160)
        (self.media / "bad.mp4").write_bytes(b"A" * 160)  # 不进 spec → probe 抛错、meta 降级

        def probe_media(cfg, path):
            if str(path) not in self.spec:
                raise v.Unsafe("没有可读取的视频流")
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with patch.object(v, "probe_media", probe_media), \
             patch.object(v, "visual_fingerprint", lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)), \
             patch.object(v, "thumbnail", return_value="t.jpg"):
            scan = v.inventory(self.cfg)
        s = self.one(scan)
        self.assertEqual(s["clean"], [])
        self.assertEqual(len(s["skipped"]), 1)
        self.assertIn("movies:bad.mp4", s["skipped"][0]["reason"])
        self.assertIn("读取异常", s["skipped"][0]["reason"])


class ReviewDraftTests(unittest.TestCase):
    """服务端审阅草稿与扫描摘要持久化（Review 26.10.12 §四D / P2「首页重复
    计算建议」收口）。指纹管线 patch 同 BatchSuggestionTests。

    - 草稿绑定 scan_id：乐观修订号，跨页合并，旧修订保存被明确拒绝不静默覆盖
    - 组外/扫描外/未知分组的选择一律拒绝；新扫描与配置变化不继承旧草稿
    - 批量建议随 groups.suggest 入库、聚合摘要随 scans.summary 入库；
      scan_metrics 是首页摘要单一来源（新扫描不再现算建议）
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.config = self.base / "config.json"
        v.atomic_json(self.config, {"state_dir": str(self.base / "state"),
                                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]})
        self.cfg = v.read_config(self.config)
        self.spec = {}

    def tearDown(self):
        self.tmp.cleanup()

    def add_file(self, name, data, duration=100.0):
        path = self.media / name
        path.write_bytes(data)
        self.spec[str(path)] = duration  # 不同重复对用不同时长：避开视觉候选的时长窗口，各自成组

    def scan(self):
        def probe_media(cfg, path):
            if str(path) not in self.spec:
                raise v.Unsafe("没有可读取的视频流")
            return {"duration": self.spec[str(path)], "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with patch.object(v, "probe_media", probe_media), \
             patch.object(v, "visual_fingerprint", lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)), \
             patch.object(v, "thumbnail", return_value="t.jpg"):
            return v.inventory(self.cfg)

    def groups_of(self, scan_id):
        with v.state(self.cfg, exclusive=False) as db:
            return v.load_groups(db, scan_id)

    def test_draft_roundtrip_merge_and_revision_conflict(self):
        # 保存/恢复/跨页合并/修订冲突：两个标签页基于同一旧修订先后保存，后者
        # 被明确拒绝（DraftConflict），先保存的内容不被覆盖
        self.add_file("a1.mp4", b"A" * 160)
        self.add_file("a2.mp4", b"A" * 160)
        self.add_file("b1.mp4", b"B" * 160, duration=200.0)
        self.add_file("b2.mp4", b"B" * 160, duration=200.0)
        scan = self.scan()
        g1, g2 = self.groups_of(scan["scan_id"])
        a1, a2 = "movies:a1.mp4", "movies:a2.mp4"
        with v.state(self.cfg, exclusive=False) as db:
            self.assertEqual(v.load_draft(db, self.cfg, scan["scan_id"]), {"rev": 0, "groups": {}})
            # 第一页保存组1：rev 0 → 1
            rev = v.save_draft(db, self.cfg, scan["scan_id"], {g1["gid"]: {"clean": [a2], "confirmed": [a2], "skip": False}}, 0)
            self.assertEqual(rev, 1)
            # 第二页（同标签页翻页）保存组2：请求只带组2，组1 选择保留
            rev = v.save_draft(db, self.cfg, scan["scan_id"], {g2["gid"]: {"clean": [], "confirmed": [], "skip": True}}, 1)
            self.assertEqual(rev, 2)
            draft = v.load_draft(db, self.cfg, scan["scan_id"])
            self.assertEqual(draft["rev"], 2)
            self.assertEqual(draft["groups"][g1["gid"]]["clean"], [a2])
            self.assertTrue(draft["groups"][g2["gid"]]["skip"])
            # 另一标签页仍基于修订 1 保存 → 冲突，先保存内容原样保留
            with self.assertRaises(v.DraftConflict) as ctx:
                v.save_draft(db, self.cfg, scan["scan_id"], {g1["gid"]: {"clean": [a1], "confirmed": [], "skip": False}}, 1)
            self.assertEqual(ctx.exception.current_rev, 2)
            self.assertEqual(v.load_draft(db, self.cfg, scan["scan_id"])["groups"][g1["gid"]]["clean"], [a2])
            # 冲突后基于最新修订可继续保存（刷新页面后的正常路径）
            self.assertEqual(v.save_draft(db, self.cfg, scan["scan_id"], {g1["gid"]: {"clean": [], "confirmed": [], "skip": False}}, 2), 3)

    def test_draft_rejects_entries_outside_group_or_scan(self):
        # 组外成员、未知分组、扫描外 clean、不存在的扫描一律拒绝
        self.add_file("a1.mp4", b"A" * 160)
        self.add_file("a2.mp4", b"A" * 160)
        self.add_file("solo.mp4", b"S" * 160, duration=300.0)  # 时长窗口外：独立文件，不进任何组
        scan = self.scan()
        gid = self.groups_of(scan["scan_id"])[0]["gid"]
        with v.state(self.cfg, exclusive=False) as db:
            for entries in (
                {gid: {"clean": ["movies:solo.mp4"], "confirmed": [], "skip": False}},   # 不在该组
                {"exact:deadbeef": {"clean": ["movies:a1.mp4"], "confirmed": [], "skip": False}},  # 未知分组
                {gid: {"clean": ["ghost:na.mp4"], "confirmed": [], "skip": False}},       # 扫描外 key
                {gid: {"clean": "movies:a1.mp4", "confirmed": [], "skip": False}},        # 字段形状
            ):
                with self.subTest(entries=entries):
                    with self.assertRaises(v.Unsafe):
                        v.save_draft(db, self.cfg, scan["scan_id"], entries, 0)
            with self.assertRaises(v.Unsafe):
                v.save_draft(db, self.cfg, "f" * 32, {}, 0)  # 扫描不存在
            self.assertEqual(v.load_draft(db, self.cfg, scan["scan_id"]), {"rev": 0, "groups": {}})  # 拒绝后草稿未产生

    def test_draft_bound_to_scan_no_inheritance(self):
        # 新扫描即新 scan_id：旧草稿不自动继承；配置变化后旧草稿直接不可读
        self.add_file("a1.mp4", b"A" * 160)
        self.add_file("a2.mp4", b"A" * 160)
        scan1 = self.scan()
        gid = self.groups_of(scan1["scan_id"])[0]["gid"]
        with v.state(self.cfg, exclusive=False) as db:
            v.save_draft(db, self.cfg, scan1["scan_id"], {gid: {"clean": ["movies:a2.mp4"], "confirmed": [], "skip": False}}, 0)
        self.add_file("a3.mp4", b"A" * 160)  # 重扫：同组多一份，scan_id 全新
        scan2 = self.scan()
        self.assertNotEqual(scan1["scan_id"], scan2["scan_id"])
        with v.state(self.cfg, exclusive=False) as db:
            self.assertEqual(v.load_draft(db, self.cfg, scan2["scan_id"]), {"rev": 0, "groups": {}})
        cfg2 = json.loads(json.dumps(self.cfg))
        cfg2["roots"][0]["exclude"] = ["nope/"]  # 仅目录配置变化（排除规则）
        v.atomic_json(self.base / "config2.json", cfg2)
        cfg2 = v.read_config(self.base / "config2.json")
        with v.state(cfg2, exclusive=False) as db:
            # 真机验收 26.10.13 反馈3：排除规则变化不废旧扫描——草稿仍可读
            # （执行仍由 plan 文档签名 + apply 的 config 终检独立拦截）
            draft = v.load_draft(db, cfg2, scan1["scan_id"])
            self.assertEqual(draft["rev"], 1)
            self.assertIn(gid, draft["groups"])
            # 扫描时的根被替换 → 仍拒绝（见 test_added_root_keeps_old_scan_viewable）
            cfg3 = json.loads(json.dumps(self.cfg))
            other = self.base / "elsewhere"
            other.mkdir()
            cfg3["roots"][0]["path"] = str(other)
            v.atomic_json(self.base / "config3.json", cfg3)
            cfg3 = v.read_config(self.base / "config3.json")
        with v.state(cfg3, exclusive=False) as db:
            with self.assertRaises(v.Unsafe):
                v.load_draft(db, cfg3, scan1["scan_id"])

    def test_summary_and_suggest_persisted_metrics_single_source(self):
        # 建议随 groups.suggest 入库、聚合摘要随 scans.summary 入库；scan_metrics
        # 读列值即返回——不再每次页面访问现算建议（suggestions 被调即失败）
        self.add_file("a1.mp4", b"A" * 160)
        self.add_file("a2.mp4", b"A" * 160)
        scan = self.scan()
        with v.state(self.cfg, exclusive=False) as db:
            row = db.execute("SELECT summary FROM scans WHERE id=?", (scan["scan_id"],)).fetchone()
            summary = json.loads(row[0])
            self.assertEqual(summary["groups"], 1)
            self.assertEqual(summary["candidates"], 1)                       # a2 建议清理
            self.assertEqual(summary["reclaim"], 160)                         # 建议可释放字节
            self.assertEqual(summary["files"], 2)
            stored = db.execute("SELECT suggest FROM groups WHERE scan=?", (scan["scan_id"],)).fetchone()[0]
            self.assertIn("movies:a2.mp4", stored)
            with patch.object(v, "suggestions", side_effect=AssertionError("不应现算建议")):
                m = v.scan_metrics(db, self.cfg, scan["scan_id"])             # 单一来源：直接读列
            self.assertEqual((m["groups"], m["candidates"], m["reclaim"], m["files"]), (1, 1, 160, 2))
            self.assertIsNone(v.scan_metrics(db, self.cfg, "f" * 32))         # 扫描不存在 → None
            # 旧库行（summary NULL）回落现算：scan_metrics 不写回、结果一致
            db.execute("UPDATE scans SET summary=NULL WHERE id=?", (scan["scan_id"],))
            db.commit()
            m = v.scan_metrics(db, self.cfg, scan["scan_id"])
            self.assertEqual((m["groups"], m["candidates"]), (1, 1))
            self.assertIsNone(db.execute("SELECT summary FROM scans WHERE id=?", (scan["scan_id"],)).fetchone()[0])
        g = self.groups_of(scan["scan_id"])[0]
        self.assertIn("movies:a2.mp4", g["suggest"]["clean"])                  # load_groups 带出持久化建议

    def test_load_files_returns_requested_subset_only(self):
        # 审阅分页按需加载：只含请求的 key；身份校验与 load_scan 同源
        self.add_file("a1.mp4", b"A" * 160)
        self.add_file("a2.mp4", b"A" * 160)
        self.add_file("b1.mp4", b"B" * 160)
        scan = self.scan()
        with v.state(self.cfg, exclusive=False) as db:
            subset = v.load_files(db, self.cfg, scan["scan_id"], ["movies:a1.mp4"])
            self.assertEqual(sorted(subset), ["movies:a1.mp4"])
            self.assertEqual(subset["movies:a1.mp4"]["stat"]["size"], 160)
            with self.assertRaises(v.Unsafe):
                v.load_files(db, self.cfg, "f" * 32, ["movies:a1.mp4"])


class CapabilityCacheTests(unittest.TestCase):
    """能力分级降级与缓存版本（Review 26.10.12 P1/§四C）。

    元数据/视觉指纹/缩略图分别记录成功与失败：仅缩略图失败不覆盖已读到的
    元数据；失败不进持久缓存（重扫重试失败能力，成功部分继续复用）；缓存
    键含算法/格式版本（版本变化整体重算）；单文件指纹阶段消失只跳过该文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def patched(self, probe_media=None, thumbnail=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(v, "probe_media", probe_media or (lambda cfg, path: {
            "duration": 100.0, "width": 640, "height": 360, "codec": "h264",
            "fps": "24/1", "bitrate": "1000000", "tracks": []})))
        stack.enter_context(patch.object(v, "visual_fingerprint",
                                         lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)))
        stack.enter_context(patch.object(v, "thumbnail", thumbnail or (lambda cfg, path, position, name: name)))
        return stack

    def records(self, scan):
        with v.state(self.cfg, exclusive=False) as db:
            return v.load_scan(db, self.cfg, scan["scan_id"])

    def test_thumb_failure_keeps_meta_and_retries_next_scan(self):
        # P1 回归：原先缩略图失败经共用异常处理把已读到的元数据覆盖成 error。
        # 现在仅缩略图失败时元数据/视觉指纹照常入库；重扫重试缩略图并补齐，
        # 已成功的 probe 不再重算（成功且版本一致的部分继续复用）。
        (self.media / "only.mp4").write_bytes(b"x" * 160)
        calls = {"probe": 0, "thumb": 0}

        def probe(cfg, path):
            calls["probe"] += 1
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        def thumb(cfg, path, position, name):
            calls["thumb"] += 1
            if calls["thumb"] == 1:
                raise v.Unsafe("缩略图生成失败")
            return "fixed.jpg"

        with self.patched(probe_media=probe, thumbnail=thumb):
            rec1 = self.records(v.inventory(self.cfg))["movies:only.mp4"]
            self.assertFalse(rec1["meta"].get("error"))  # 元数据未被缩略图失败覆盖
            self.assertEqual(rec1["meta"]["duration"], 100.0)
            self.assertEqual(rec1["vhash"], [0] * len(v.FRAME_POSITIONS))  # 视觉指纹保留
            self.assertIsNone(rec1.get("thumb"))  # 缩略图本轮失败缺席
            rec2 = self.records(v.inventory(self.cfg))["movies:only.mp4"]
        self.assertEqual(calls["probe"], 1)  # 成功能力复用：重扫未重算元数据
        self.assertEqual(calls["thumb"], 2)  # 失败能力重扫重试并补齐
        self.assertEqual(rec2["thumb"], "fixed.jpg")
        self.assertEqual(rec2["meta"]["duration"], 100.0)

    def test_failed_meta_not_cached_and_retried(self):
        # P1 回归：原先失败结果写入缓存会一直复用。现在失败不进持久缓存——
        # 首轮降级为 meta.error（扫描摘要计入 fingerprint_errors），重扫时
        # 重新 probe，恢复后正常参与业务。
        (self.media / "broken.mp4").write_bytes(b"x" * 160)
        calls = {"probe": 0}

        def probe(cfg, path):
            calls["probe"] += 1
            if calls["probe"] == 1:
                raise v.Unsafe("没有可读取的视频流")
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe):
            scan1 = v.inventory(self.cfg)
            rec1 = self.records(scan1)["movies:broken.mp4"]
            self.assertIn("没有可读取的视频流", rec1["meta"].get("error", ""))
            self.assertEqual(scan1["fingerprint_errors"], 1)
            scan2 = v.inventory(self.cfg)
            rec2 = self.records(scan2)["movies:broken.mp4"]
        self.assertEqual(calls["probe"], 2)  # 失败未被缓存，重扫重试
        self.assertFalse(rec2["meta"].get("error"))
        self.assertEqual(rec2["meta"]["duration"], 100.0)
        self.assertEqual(scan2["fingerprint_errors"], 0)

    def test_cache_version_change_recomputes(self):
        # §四C：缓存键含算法/格式版本——(path,stat) 未变但版本变化时旧缓存
        # 失效，整体重算（probe 重新执行）。
        (self.media / "stable.mp4").write_bytes(b"x" * 160)
        calls = {"probe": 0}

        def probe(cfg, path):
            calls["probe"] += 1
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe):
            v.inventory(self.cfg)
            self.assertEqual(calls["probe"], 1)  # 第二次前先确认首次已算
            with patch.object(v, "HASH_CACHE_VERSION", "999-test"):
                v.inventory(self.cfg)
        self.assertEqual(calls["probe"], 2)  # 版本变化触发重算

    def test_vanishing_file_skipped_not_fatal(self):
        # §四C：指纹阶段单文件消失只跳过并记录警告，其余文件正常完成本轮；
        # 处理首个文件时删掉另一个（不依赖遍历顺序，两个方向都成立）。
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        (self.media / "z.mp4").write_bytes(b"z" * 200)

        def probe(cfg, path):
            for other in self.media.iterdir():
                if other != path:
                    other.unlink()
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe):
            scan = v.inventory(self.cfg)
        self.assertEqual(scan["files"], 1)  # 消失文件被剔除，扫描没有整轮失败
        self.assertEqual(scan["scan_warnings"], 1)
        self.assertIn("跳过指纹阶段", scan["scan_warning_samples"][0])
        self.assertEqual(scan["fingerprint_errors"], 0)  # 存活文件业务结果准确


class ScanProgressTests(unittest.TestCase):
    """扫描三阶段进度、停止响应点与结束状态分类（Review 26.10.12 P2/§三.2）。

    指纹管线全部 patch（同 MatchRuleTests），不依赖 ffmpeg：
    - discover/fingerprint/grouping 三阶段 progress 相位——总数未知时只报已发现
      数（total=0），不伪造百分比；
    - 停止在目录遍历、文件处理、分组循环三处检查点均可响应；已成功计算的
      指纹缓存保留，重扫直接复用；
    - 结束 summary 区分 完成 / 完成但有跳过；扫描中消失的单个文件只跳过，
      不让整轮失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)
        self.progress_log = []

    def tearDown(self):
        self.tmp.cleanup()

    def patched(self, probe_media=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(v, "probe_media", probe_media or (lambda cfg, path: {
            "duration": 100.0, "width": 640, "height": 360, "codec": "h264",
            "fps": "24/1", "bitrate": "1000000", "tracks": []})))
        stack.enter_context(patch.object(v, "visual_fingerprint",
                                         lambda cfg, path, duration: [0] * len(v.FRAME_POSITIONS)))
        stack.enter_context(patch.object(v, "thumbnail", lambda cfg, path, position, name: name))
        return stack

    def watch(self, trigger=lambda data: False):
        """记录每次进度写入；trigger(data) 为真时置停止标志（模拟用户点停止）。"""
        real = v.write_progress

        def wrapper(cfg, data):
            self.progress_log.append(dict(data))
            real(cfg, data)
            if trigger(data):
                v.cancel_path(cfg).parent.mkdir(parents=True, exist_ok=True)
                v.cancel_path(cfg).touch()
        return patch.object(v, "write_progress", side_effect=wrapper)

    def cached_rows(self):
        with v.state(self.cfg, exclusive=False) as db:
            return db.execute("SELECT count(*) FROM hashes").fetchone()[0]

    def test_three_phase_progress_and_clean_summary(self):
        # 无跳过扫描：discover(总数未知)→fingerprint(total 已知)→grouping→done，
        # done 携带结束 summary（skipped_total=0 → 纯完成）。
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        (self.media / "z.mp4").write_bytes(b"z" * 200)
        with self.patched(), self.watch():
            result = v.inventory(self.cfg)
        phases = [p["phase"] for p in self.progress_log]
        self.assertLess(phases.index("discover"), phases.index("fingerprint"))
        self.assertLess(phases.index("fingerprint"), phases.index("grouping"))
        self.assertEqual(phases[-1], "done")
        discover = next(p for p in self.progress_log if p["phase"] == "discover")
        self.assertEqual(discover["total"], 0)  # 总数未知：不伪造总数/百分比
        self.assertIn("found", discover)
        fingerprint = next(p for p in self.progress_log if p["phase"] == "fingerprint" and p["total"])
        self.assertEqual(fingerprint["total"], 2)
        final = v.read_progress(self.cfg)
        self.assertEqual(final["phase"], "done")
        self.assertEqual(final["summary"]["files"], 2)
        self.assertEqual(final["summary"]["skipped_total"], 0)
        self.assertFalse(result.get("cancelled"))
        self.assertEqual(result["skipped_total"], 0)

    def test_cancel_during_walk_is_immediate(self):
        # 停止响应点①目录遍历：discover 首次写入即停止，停在 walk 而非等指纹阶段
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        with self.patched(), self.watch(lambda d: d.get("phase") == "discover"):
            result = v.inventory(self.cfg)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["stopped_at"], "discover")
        self.assertFalse(v.cancel_path(self.cfg).exists())
        self.assertEqual((v.read_progress(self.cfg) or {}).get("phase"), "cancelled")
        self.assertEqual(tasks.latest(self.cfg, "scan")["status"], tasks.PARTIAL)  # 已停止≠成功/失败

    def test_cancel_during_fingerprint_keeps_cache_for_reuse(self):
        # 停止响应点②文件处理：首个文件识别完成后停止——缓存保留（hashes 表），
        # 重扫只计算未识别过的文件（成功部分复用，不重算）。
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        (self.media / "z.mp4").write_bytes(b"z" * 200)
        calls = {"probe": 0}

        def probe(cfg, path):
            calls["probe"] += 1
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe), \
                self.watch(lambda d: d.get("phase") == "fingerprint" and d.get("done", 0) >= 1):
            first = v.inventory(self.cfg)
        self.assertTrue(first["cancelled"])
        self.assertEqual(first["stopped_at"], "fingerprint")
        self.assertEqual(first["processed"], 1)
        self.assertEqual(self.cached_rows(), 1)  # 已成功计算的缓存保留
        with self.patched(probe_media=probe):
            second = v.inventory(self.cfg)  # 重扫：无停止标志
        self.assertFalse(second.get("cancelled"))
        self.assertEqual(second["files"], 2)
        self.assertEqual(calls["probe"], 2)  # 只补算未识别的 1 个，缓存部分直接复用
        with tasks._db(self.cfg) as db:  # 停止任务的持久化结果：如实写复用措辞
            stopped = db.execute("SELECT result FROM tasks WHERE status='partial'").fetchall()
        self.assertIn("复用已有识别结果", stopped[0]["result"])
        self.assertNotIn("断点续跑", stopped[0]["result"])

    def test_cancel_during_grouping_responsive(self):
        # 停止响应点③分组循环：指纹全部完成后、两两比对前停止——已识别缓存全保留
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        (self.media / "z.mp4").write_bytes(b"z" * 200)
        with self.patched(), self.watch(lambda d: d.get("phase") == "grouping"):
            result = v.inventory(self.cfg)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["stopped_at"], "grouping")
        self.assertEqual(self.cached_rows(), 2)  # 指纹阶段成果完整保留
        self.assertEqual(result["found"], 2)

    def test_done_summary_counts_skips(self):
        # 结束状态：完成但有跳过——fingerprint_errors（媒体损坏）计入
        # skipped_total 并携带样本；扫描本身成功不整轮失败。
        (self.media / "ok.mp4").write_bytes(b"x" * 160)
        (self.media / "broken.mp4").write_bytes(b"y" * 160)

        def probe(cfg, path):
            if path.name == "broken.mp4":
                raise v.Unsafe("没有可读取的视频流")
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe), self.watch():
            result = v.inventory(self.cfg)
        self.assertFalse(result.get("cancelled"))
        self.assertEqual(result["files"], 2)  # 损坏文件保留记录（meta.error），不剔除
        self.assertEqual(result["fingerprint_errors"], 1)
        self.assertEqual(result["skipped_total"], 1)
        self.assertIn("movies:broken.mp4", result["fingerprint_error_samples"])
        final = v.read_progress(self.cfg)
        self.assertEqual(final["summary"]["skipped_total"], 1)

    def test_vanished_file_during_fingerprint_skips_not_fails(self):
        # P2 回归：末尾批量 re-stat 已移除——扫描中消失的单个文件由指纹阶段的
        # 逐文件 stat 复核在处理当口发现，降级为跳过，其余文件正常入库。
        (self.media / "a.mp4").write_bytes(b"a" * 160)
        (self.media / "z.mp4").write_bytes(b"z" * 200)

        def probe(cfg, path):
            for other in self.media.iterdir():  # 处理任一文件时删掉另一个
                if other != path:
                    other.unlink()
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}

        with self.patched(probe_media=probe), self.watch():
            result = v.inventory(self.cfg)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["skipped_files"], 1)
        self.assertEqual(result["skipped_total"], 1)
        self.assertIn("跳过指纹阶段", result["scan_warning_samples"][0])


class TaskCoordinationTests(unittest.TestCase):
    """任务协调模块（Review 26.10.12 §四B/实施阶段2）：状态机、互斥占用、重启核对。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def copies(self):
        a, b = self.media / "a.mp4", self.media / "b.mp4"
        a.write_bytes(b"video-a" * 20)
        b.write_bytes(a.read_bytes())
        return a, b

    def test_claim_occupies_atomically_and_rejects_concurrent(self):
        first = tasks.claim(self.cfg, "scan")
        self.assertEqual(first["status"], tasks.RUNNING)
        with self.assertRaises(tasks.Busy) as ctx:  # 不同类型同样互斥（一个工作区一个有状态操作）
            tasks.claim(self.cfg, "plan")
        self.assertEqual(ctx.exception.task["id"], first["id"])
        with v.state(self.cfg) as db:  # 操作锁被引擎持有（Web 与 CLI 共用同一把）
            with self.assertRaises(tasks.Busy):
                tasks.claim(self.cfg, "scan")
        self.assertEqual(tasks.finish(self.cfg, tasks.SUCCESS, result={"ok": 1}), 1)
        second = tasks.claim(self.cfg, "plan")  # 终结后可重新占用
        self.assertNotEqual(second["id"], first["id"])

    def test_state_machine_stop_then_terminal_idempotent(self):
        task = tasks.claim(self.cfg, "plan", scan_id="s1")
        self.assertTrue(tasks.request_stop(self.cfg, task["id"]))  # running → stopping
        self.assertEqual(tasks.get(self.cfg, task["id"])["status"], tasks.STOPPING)
        self.assertFalse(tasks.request_stop(self.cfg, task["id"]))  # 重复请求无效
        self.assertEqual(tasks.finish(self.cfg, tasks.PARTIAL, error="stopped"), 1)  # stopping → partial
        row = tasks.get(self.cfg, task["id"])
        self.assertEqual(row["status"], tasks.PARTIAL)
        self.assertIsNotNone(row["finished_ts"])
        self.assertEqual(tasks.finish(self.cfg, tasks.FAILED), 0)  # 终态不可再改（幂等）
        self.assertEqual(tasks.get(self.cfg, task["id"])["status"], tasks.PARTIAL)
        with self.assertRaises(ValueError):
            tasks.finish(self.cfg, "bogus-status")

    def test_stale_running_row_is_reclaimed_after_grace(self):
        first = tasks.claim(self.cfg, "scan")
        with tasks._db(self.cfg) as db:  # 模拟属主进程死亡：心跳停在宽限期之前
            db.execute("UPDATE tasks SET updated_ts=? WHERE id=?", (tasks._now() - tasks.STALE_AFTER - 10, first["id"]))
            db.commit()
        second = tasks.claim(self.cfg, "scan")  # 锁空闲 + 超宽限 → 回收旧行、允许重新占用
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(tasks.get(self.cfg, first["id"])["status"], tasks.INTERRUPTED)
        tasks.finish(self.cfg, tasks.SUCCESS)
        tasks.claim(self.cfg, "scan")  # 宽限期内的新鲜占用 → 忙碌而非误回收
        with self.assertRaises(tasks.Busy):
            tasks.claim(self.cfg, "scan")

    def test_scan_task_lifecycle_via_engine(self):
        self.copies()
        scan = v.inventory(self.cfg)
        row = tasks.latest(self.cfg, "scan")
        self.assertEqual(row["status"], tasks.SUCCESS)
        self.assertEqual(row["scan_id"], scan["scan_id"])
        self.assertEqual(json.loads(row["result"])["files"], scan["files"])
        self.assertEqual(row["phase"], "done")
        with v.state(self.cfg) as db:  # 引擎持锁时入口直接忙碌（CLI 同一把锁）
            with self.assertRaises(tasks.Busy):
                v.inventory(self.cfg)
        task = tasks.claim(self.cfg, "scan")  # webui 请求线程先占用，引擎入口收养、不二次占用
        v.inventory(self.cfg)
        self.assertEqual(tasks.get(self.cfg, task["id"])["status"], tasks.SUCCESS)

    def test_scan_cancelled_marks_partial_and_failure_marks_failed(self):
        self.copies()
        real_progress = v.write_progress

        def cancel_after_start(cfg, data):
            real_progress(cfg, data)
            v.cancel_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            v.cancel_path(cfg).touch()

        with patch.object(v, "write_progress", side_effect=cancel_after_start):
            v.inventory(self.cfg)
        self.assertEqual(tasks.latest(self.cfg, "scan")["status"], tasks.PARTIAL)  # 用户停止≠成功
        # §四F 业务状态与报告状态分离：export_dir 不可写（指向普通文件）时，
        # 扫描照常成功入库；显式导出如实报错，但绝不把已完成的扫描改写为失败。
        blocker = self.base / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        self.cfg["export_dir"] = str(blocker)
        scan = v.inventory(self.cfg)
        row = tasks.latest(self.cfg, "scan")
        self.assertEqual(row["status"], tasks.SUCCESS)
        self.assertEqual(json.loads(row["result"])["scan_id"], scan["scan_id"])
        with self.assertRaises(OSError):
            v.export_audit(self.cfg, scan["scan_id"])  # 报告失败如实抛出
        self.assertEqual(tasks.latest(self.cfg, "scan")["status"], tasks.SUCCESS)  # 扫描状态不被报告失败改写

    def test_plan_report_write_failure_degrades_not_fails(self):
        # §四F 业务/报告状态分离：签名计划已落盘即业务成功；审核报告写共享
        # 目录失败只降级为 report_error 说明，不把已成立的计划任务记为失败。
        self.copies()
        scan = v.inventory(self.cfg)
        blocker = self.base / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        self.cfg["export_dir"] = str(blocker)
        plan = v.plan(self.cfg, scan["scan_id"], [{"root": "movies", "rel": "a.mp4"}])
        row = tasks.latest(self.cfg, "plan")
        self.assertEqual(row["status"], tasks.SUCCESS)  # 报告失败不改业务结果
        result = json.loads(row["result"])
        self.assertIn("审核报告写入共享文件夹失败", result["report_error"])
        self.assertTrue(Path(plan["plan"]).exists())  # 签名计划照常生效

    def test_plan_task_lifecycle_and_exclusive_connection(self):
        self.copies()
        scan = v.inventory(self.cfg)
        sel = [{"root": "movies", "rel": "a.mp4"}]
        plan = v.plan(self.cfg, scan["scan_id"], sel)
        row = tasks.latest(self.cfg, "plan")
        self.assertEqual(row["status"], tasks.SUCCESS)
        self.assertEqual(row["scan_id"], scan["scan_id"])
        self.assertEqual(json.loads(row["result"])["plan_id"], plan["plan_id"])
        # Review P1：计划复核经 fingerprint_cached 更新缓存，必须持独占连接——
        # 他方持锁时入口忙碌，而不是带非独占连接继续写。
        with v.state(self.cfg) as db:
            with self.assertRaises(tasks.Busy):
                v.plan(self.cfg, scan["scan_id"], sel)
        with self.assertRaises(v.Unsafe):  # 失败路径：错误落任务行
            v.plan(self.cfg, scan["scan_id"], [{"root": "movies", "rel": "ghost.mp4"}])
        row = tasks.latest(self.cfg, "plan")
        self.assertEqual(row["status"], tasks.FAILED)
        self.assertIn("扫描之外", row["error"])

    def test_reconcile_marks_scan_interrupted(self):
        tasks.claim(self.cfg, "scan")
        v.write_progress(self.cfg, {"phase": "fingerprint", "done": 1, "total": 3, "current": "x.mp4"})
        out = tasks.reconcile(self.cfg)
        self.assertEqual(len(out), 1)
        row = tasks.latest(self.cfg, "scan")
        self.assertEqual(row["status"], tasks.INTERRUPTED)
        self.assertIsNone(tasks.active(self.cfg))
        second = tasks.claim(self.cfg, "scan")  # 核对后工作区可用
        self.assertNotEqual(second["id"], row["id"])

    def test_reconcile_apply_uses_journal_summary_never_replays(self):
        plan_id = "a" * 64
        tasks.claim(self.cfg, "apply", plan_id=plan_id)
        v.atomic_json(Path(self.cfg["state_dir"]) / f"{plan_id}.journal.json",
                      {"plan_id": plan_id,
                       "events": [{"key": "movies:x.mp4", "status": "deleted", "bytes": 8}],
                       "pending": {"key": "movies:y.mp4", "hold": ".vd-delete-" + "f" * 32 + ".mp4"}})
        out = tasks.reconcile(self.cfg)
        self.assertEqual(len(out), 1)
        row = tasks.latest(self.cfg, "apply")
        self.assertEqual(row["status"], tasks.VERIFY)  # 待核实：结合 journal 汇总
        summary = json.loads(row["result"])
        self.assertEqual(summary["deleted"], 1)
        self.assertTrue(summary["pending"])
        self.assertEqual(summary["pending_key"], "movies:y.mp4")
        self.assertIsNone(tasks.active(self.cfg))  # 只核实，绝不自动重放成新任务

    def test_reconcile_skips_while_operation_lock_held(self):
        tasks.claim(self.cfg, "scan")
        with v.state(self.cfg) as db:  # 活进程持锁（如 CLI 引擎在跑）：整体跳过，绝不误判
            self.assertEqual(tasks.reconcile(self.cfg), [])
            self.assertEqual(tasks.latest(self.cfg, "scan")["status"], tasks.RUNNING)
        tasks.reconcile(self.cfg)  # 锁释放后才核对
        self.assertEqual(tasks.latest(self.cfg, "scan")["status"], tasks.INTERRUPTED)

    def test_reflect_mirrors_progress_only_while_active(self):
        task = tasks.claim(self.cfg, "scan")
        v.write_progress(self.cfg, {"scan_id": "s1", "phase": "fingerprint", "done": 2, "total": 5, "current": "a.mp4"})
        row = tasks.get(self.cfg, task["id"])
        self.assertEqual((row["phase"], row["done"], row["total"], row["scan_id"]), ("fingerprint", 2, 5, "s1"))
        tasks.finish(self.cfg, tasks.SUCCESS, result={"files": 5})
        v.write_progress(self.cfg, {"phase": "done", "done": 5, "total": 5})  # 迟到的进度不改写终态
        row = tasks.get(self.cfg, task["id"])
        self.assertEqual(row["status"], tasks.SUCCESS)
        self.assertEqual(row["phase"], "fingerprint")
        self.assertEqual(json.loads(row["result"])["files"], 5)

    def test_journal_summary_counts_and_defaults(self):
        # §5B：读不到 journal ≠ 确认没执行——缺失/损坏不可降级为零计数。
        missing = tasks.journal_summary(self.cfg, "b" * 64)
        self.assertEqual(missing, {"journal": str(Path(self.cfg["state_dir"]) / ("b" * 64 + ".journal.json")),
                                   "readable": False, "deleted": None, "errors": None,
                                   "pending": None, "pending_key": ""})
        state = Path(self.cfg["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        (state / ("c" * 64 + ".journal.json")).write_text("{not json", encoding="utf-8")
        self.assertIs(tasks.journal_summary(self.cfg, "c" * 64)["readable"], False)  # 损坏 JSON
        v.atomic_json(state / ("d" * 64 + ".journal.json"), {"events": 5})  # 合法 JSON 但结构损坏
        self.assertIs(tasks.journal_summary(self.cfg, "d" * 64)["readable"], False)

    def test_apply_outcome_grading_by_journal(self):
        # §5B 定级规则：pending 为真或为 None（不可证未执行）→ VERIFY；
        # deleted 非零 → PARTIAL；可证全零 → FAILED。
        pid = "e" * 64
        state = Path(self.cfg["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        self.assertEqual(tasks.apply_outcome(self.cfg, pid)[0], tasks.VERIFY)  # journal 缺失
        (state / f"{pid}.journal.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(tasks.apply_outcome(self.cfg, pid)[0], tasks.VERIFY)  # journal 损坏
        v.atomic_json(state / f"{pid}.journal.json", {"plan_id": pid, "events": [], "pending": None})
        self.assertEqual(tasks.apply_outcome(self.cfg, pid)[0], tasks.FAILED)  # 可证零执行
        v.atomic_json(state / f"{pid}.journal.json",
                      {"plan_id": pid,
                       "events": [{"key": "movies:x.mp4", "status": "deleted", "bytes": 8}],
                       "pending": None})
        self.assertEqual(tasks.apply_outcome(self.cfg, pid)[0], tasks.PARTIAL)  # 仅 deleted
        v.atomic_json(state / f"{pid}.journal.json",
                      {"plan_id": pid,
                       "events": [{"key": "movies:x.mp4", "status": "deleted", "bytes": 8}],
                       "pending": {"key": "movies:y.mp4", "hold": ".vd-delete-" + "f" * 32 + ".mp4"}})
        status, summary = tasks.apply_outcome(self.cfg, pid)
        self.assertEqual(status, tasks.VERIFY)  # 未决暂存优先于 partial
        self.assertTrue(summary["pending"])
        self.assertTrue(summary["readable"])

    def test_apply_uncertain_finalization_shared_across_entries(self):
        # §5B 四入口同一定级：执行器异常/键盘中断/stale 回收（reconcile 由
        # test_reconcile_apply_uses_journal_summary_never_replays 覆盖）。
        pid = "a" * 64
        state = Path(self.cfg["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(RuntimeError):  # 执行器异常 + journal 缺失 → VERIFY
            with tasks.run(self.cfg, "apply", plan_id=pid):
                raise RuntimeError("boom")
        self.assertEqual(tasks.latest(self.cfg, "apply")["status"], tasks.VERIFY)
        with self.assertRaises(KeyboardInterrupt):  # 键盘中断 + journal 缺失 → VERIFY（不记 interrupted）
            with tasks.run(self.cfg, "apply", plan_id=pid):
                raise KeyboardInterrupt()
        self.assertEqual(tasks.latest(self.cfg, "apply")["status"], tasks.VERIFY)
        v.atomic_json(state / f"{pid}.journal.json",  # 键盘中断 + 可证仅删除过 → PARTIAL
                      {"plan_id": pid,
                       "events": [{"key": "movies:x.mp4", "status": "deleted", "bytes": 8}],
                       "pending": None})
        with self.assertRaises(KeyboardInterrupt):
            with tasks.run(self.cfg, "apply", plan_id=pid):
                raise KeyboardInterrupt()
        self.assertEqual(tasks.latest(self.cfg, "apply")["status"], tasks.PARTIAL)
        # 可证零执行（结构完整的空 journal）→ FAILED
        v.atomic_json(state / f"{pid}.journal.json", {"plan_id": pid, "events": [], "pending": None})
        with self.assertRaises(RuntimeError):
            with tasks.run(self.cfg, "apply", plan_id=pid):
                raise RuntimeError("boom")
        self.assertEqual(tasks.latest(self.cfg, "apply")["status"], tasks.FAILED)
        # stale 回收：apply 行按 journal 定级（此处 PARTIAL），scan 行仍 INTERRUPTED
        # （claim 每次只回收最近一条 stale 行，故顺序回收）。
        v.atomic_json(state / f"{pid}.journal.json",
                      {"plan_id": pid,
                       "events": [{"key": "movies:x.mp4", "status": "deleted", "bytes": 8}],
                       "pending": None})
        stale_apply = tasks.claim(self.cfg, "apply", plan_id=pid)
        with tasks._db(self.cfg) as db:
            db.execute("UPDATE tasks SET updated_ts=? WHERE id=?",
                       (tasks._now() - tasks.STALE_AFTER - 10, stale_apply["id"]))
            db.commit()
        tasks.claim(self.cfg, "scan")  # 回收 apply 行并占用
        self.assertEqual(tasks.get(self.cfg, stale_apply["id"])["status"], tasks.PARTIAL)
        tasks.finish(self.cfg, tasks.SUCCESS)
        stale_scan = tasks.claim(self.cfg, "scan")
        with tasks._db(self.cfg) as db:
            db.execute("UPDATE tasks SET updated_ts=? WHERE id=?",
                       (tasks._now() - tasks.STALE_AFTER - 10, stale_scan["id"]))
            db.commit()
        tasks.claim(self.cfg, "plan")  # scan 行回收仍走 interrupted
        self.assertEqual(tasks.get(self.cfg, stale_scan["id"])["status"], tasks.INTERRUPTED)
        tasks.finish(self.cfg, tasks.SUCCESS)

    def test_apply_grading_survives_journal_summary_fault(self):
        # §5B 故障注入：journal_summary 本身抛错时任务终态仍为 VERIFY 而非 FAILED。
        with patch.object(tasks, "journal_summary", side_effect=RuntimeError("fs gone")):
            with self.assertRaises(RuntimeError):
                with tasks.run(self.cfg, "apply", plan_id="9" * 64):
                    raise RuntimeError("boom")
        row = tasks.latest(self.cfg, "apply")
        self.assertEqual(row["status"], tasks.VERIFY)
        self.assertIs(json.loads(row["result"])["readable"], False)


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


class FpkMainStartGateTests(unittest.TestCase):
    """fpk/cmd/main start 的授权刷新熔断（Review P1）。

    脚本内 PY 硬编码为 fnOS 运行时路径，真机之外不可得；
    因此执行注入 mock 解释器的脚本副本，只测启动门禁逻辑本身。
    WSL 全量环境不拷贝 fpk/ 时自动跳过。
    # ponytail: 成功路径（socket 就绪轮询）未集成测试——Windows Python 无
    AF_UNIX，无法伪造 [ -S ] 可识别的套接字；该路径为原有代码且未被本改动触碰。"""

    @staticmethod
    def _bash_for_windows_paths():
        # which("bash") 在 Windows 可能命中 System32\bash.exe（WSL 启动器）：
        # 它活在 /mnt 命名空间，无法执行 C:/ 路径脚本（退出码 127），须探测
        # 真正能处理 Windows 路径的 bash（Git Bash）；找不到则放弃并跳过本测试。
        if os.name != "nt":
            return shutil.which("bash")
        candidates = [shutil.which("bash")] + [shutil.which(p) for p in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe")]
        for bash in dict.fromkeys(candidates):
            if not bash:
                continue
            try:
                probe = subprocess.run([bash, "-c", "test -d 'C:/'"],
                                       capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return bash
        return None

    def test_start_aborts_when_setup_fails(self):
        bash = self._bash_for_windows_paths()
        if not bash:
            self.skipTest("no bash that handles Windows paths (Git Bash not found)")
        src = Path(__file__).resolve().parent.parent / "fpk" / "cmd" / "main"
        if not src.exists():
            self.skipTest(f"script not present: {src}")
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).as_posix()
            mock_py = Path(raw) / "python-mock"
            mock_py.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8", newline="\n")
            mock_py.chmod(0o755)  # tempfile 落盘无执行位：WSL/Linux 上 [ -x $PY ] 会走「缺运行时」分支，熔断断言失配
            script = Path(raw) / "main"
            script.write_text(src.read_text(encoding="utf-8").replace(
                "PY=/var/apps/python312/target/bin/python3", "PY=" + mock_py.as_posix()),
                encoding="utf-8", newline="\n")
            (Path(raw) / "var").mkdir()
            log = Path(raw) / "trim.log"
            env = {**os.environ,
                   "TRIM_APPDEST": base + "/app", "TRIM_PKGETC": base + "/etc",
                   "TRIM_PKGVAR": base + "/var", "TRIM_TEMP_LOGFILE": log.as_posix()}
            proc = subprocess.run([bash, script.as_posix(), "start"], env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("配置派生失败", log.read_text(encoding="utf-8"))
            self.assertFalse((Path(raw) / "var" / "webui.pid").exists(), "熔断后不得拉起 webui")


class PreviewFramesTests(unittest.TestCase):
    """按需多帧预览（Review 26.10.12 §三.3/§四C）。

    ffmpeg 全桩替（thumbnail/run_tool 均 patch）：验证抽帧参数构造、相对
    时间换算、缓存命中/未命中与文件替换失效、失败帧降级重试，以及与扫描
    流程的解耦（inventory 绝不触发多帧预览）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.cfg = {"state_dir": str(self.base / "state"),
                    "roots": [{"id": "movies", "path": str(self.media), "role": "cleanup", "exclude": []}]}
        self.config = self.base / "config.json"
        v.atomic_json(self.config, self.cfg)
        self.cfg = v.read_config(self.config)
        # 本类 ffmpeg 全桩替：显式 probe override 让 resolve_tools 不查宿主
        # 文件系统——WSL/干净环境无 ffmpeg 时 thumbnail 仍可达 run_tool 桩。
        self.cfg["probe"] = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
        self.media.joinpath("a.mp4").write_bytes(b"video-a" * 20)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_thumbnail(self, calls, fail_positions=()):
        def thumbnail(cfg, path, position, name, folder="thumbs", width=320):
            calls.append({"position": round(position, 3), "name": name, "folder": folder, "width": width})
            if round(position, 3) in fail_positions:
                raise v.Unsafe("抽帧失败")
            target = Path(cfg["state_dir"]) / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\xff\xd8fake-jpeg")
            return name
        return thumbnail

    def test_positions_realtime_and_cache_layout(self):
        # 五个相对位置换算实际秒数（§三.3），640px 帧图落盘私有 previews/ 目录
        calls = []
        with patch.object(v, "thumbnail", self.fake_thumbnail(calls)):
            frames = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual([f["pos"] for f in frames], list(v.FRAME_POSITIONS))
        self.assertEqual([f["t"] for f in frames], [10.0, 25.0, 50.0, 75.0, 90.0])
        self.assertEqual([c["position"] for c in calls], [10.0, 25.0, 50.0, 75.0, 90.0])
        self.assertTrue(all(c["folder"] == "previews" and c["width"] == 640 for c in calls))
        previews = Path(self.cfg["state_dir"]) / "previews"
        self.assertEqual(len(list(previews.glob("*.jpg"))), 5)
        self.assertEqual(len(list(previews.glob("*.json"))), 1)  # 缓存清单随帧生成
        for f in frames:
            self.assertTrue((previews / f["img"]).read_bytes().startswith(b"\xff\xd8"))

    def test_realtime_rounding(self):
        # 非整时长按毫秒记录实际时间（0.1×66.6=6.659…→6.66）
        with patch.object(v, "thumbnail", self.fake_thumbnail([])):
            frames = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 66.6)
        self.assertEqual([f["t"] for f in frames], [6.66, 16.65, 33.3, 49.95, 59.94])

    def test_cache_hit_skips_ffmpeg(self):
        # 第二次调用全量命中：不再触发抽帧，返回值与首次一致（§四C 私有缓存）
        calls = []
        with patch.object(v, "thumbnail", self.fake_thumbnail(calls)):
            first = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
            second = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual(len(calls), 5)
        self.assertEqual(first, second)

    def test_cache_miss_after_file_replaced(self):
        # stat 进缓存键：文件替换后旧缓存不复用，五帧全部重新生成
        calls = []
        with patch.object(v, "thumbnail", self.fake_thumbnail(calls)):
            v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
            self.media.joinpath("a.mp4").write_bytes(b"replaced-new-bytes" * 30)
            v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(list((Path(self.cfg["state_dir"]) / "previews").glob("*.json"))), 2)

    def test_partial_failure_degrades_and_retries(self):
        # 单帧失败只降级该帧（其余四帧可用）；重调时成功帧走缓存、失败帧重试（§四C 能力分级）
        calls = []
        with patch.object(v, "thumbnail", self.fake_thumbnail(calls, fail_positions={25.0})):
            frames = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual(len(frames), 5)
        self.assertEqual([f["pos"] for f in frames if "img" in f], [0.1, 0.5, 0.75, 0.9])
        self.assertIn("error", frames[1])
        with patch.object(v, "thumbnail", self.fake_thumbnail(calls)):
            frames = v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual(len(calls), 6)  # 首轮 5 次 + 仅补失败帧 1 次
        self.assertTrue(all("img" in f and "error" not in f for f in frames))

    def test_ffmpeg_args_serial_low_priority_shape(self):
        # 抽帧参数构造（ffmpeg 本体桩替）：-ss 前置定位 + 单帧 + 640 宽 + -threads 1
        args_log = []

        def run_tool(args, timeout=120):
            args_log.append(list(args))
            return b"\xff\xd8fake-jpeg"
        with patch.object(v, "run_tool", run_tool):
            v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, 100.0)
        self.assertEqual(len(args_log), 5)
        target = str(self.media / "a.mp4")
        for args, t in zip(args_log, ["10.000", "25.000", "50.000", "75.000", "90.000"]):
            self.assertEqual(args[1:], ["-v", "error", "-nostdin", "-ss", t, "-i", target,
                                        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5",
                                        "-f", "image2", "-c:v", "mjpeg", "-", "-threads", "1"])

    def test_invalid_duration_refused(self):
        with patch.object(v, "thumbnail", self.fake_thumbnail([])):
            for bad in (0, -5, float("nan")):
                with self.assertRaises(v.Unsafe):
                    v.preview_frames(self.cfg, {"root": "movies", "rel": "a.mp4"}, bad)

    def test_scan_never_generates_previews(self):
        # 与扫描解耦（§四C）：inventory 只产单张缩略图，绝不触发多帧预览
        def probe_media(cfg, path):
            return {"duration": 100.0, "width": 640, "height": 360, "codec": "h264",
                    "fps": "24/1", "bitrate": "1000000", "tracks": []}
        with patch.object(v, "probe_media", probe_media), \
             patch.object(v, "visual_fingerprint", return_value=[0] * len(v.FRAME_POSITIONS)), \
             patch.object(v, "thumbnail", return_value="t.jpg"), \
             patch.object(v, "preview_frames") as preview:
            v.inventory(self.cfg)
        preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()

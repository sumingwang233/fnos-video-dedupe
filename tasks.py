"""轻量任务协调模块（Review 26.10.12 §四B / 实施阶段 2）。

复用 state_dir/inventory.sqlite3 里的一张 tasks 表 + state_dir/operation.lock
跨进程操作锁（Web 与 CLI 共用同一把锁，锁实现自 videodedupe.state 原样提取），
不引入队列服务。约定：

- 配置与参数验证完成后才调用 claim()；「查活跃任务 + 落 running 行」在独占
  操作锁内原子完成，占用成功前不产生任何副作用。
- 一个工作区同时至多一个活跃（running/stopping）任务；CLI 直接跑引擎同样
  先过这把锁（引擎入口 tasks.run 收养或占用）。
- 进度镜像 reflect() 由 videodedupe.write_progress 顺带调用；progress.json
  仍是逐文件进度的主源，任务行承载身份、阶段、结果与错误。
- 重启用 reconcile() 核对：扫描/计划标记 interrupted；清理结合 journal 经
  apply_outcome 定级 verify/partial/failed（§5B：不可证未执行一律 verify
  待核实），绝不自动重放不确定操作。

状态机：running →(request_stop)→ stopping →(finish)→ success / partial /
failed；进程消失后由 reconcile/stale 回收 → interrupted（扫描、计划）或
journal 定级 verify/partial/failed（清理）。
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

TYPES = ("scan", "plan", "apply")
RUNNING, STOPPING = "running", "stopping"
SUCCESS, PARTIAL, FAILED, INTERRUPTED, VERIFY = "success", "partial", "failed", "interrupted", "verify"
ACTIVE = (RUNNING, STOPPING)
TERMINAL = (SUCCESS, PARTIAL, FAILED, INTERRUPTED, VERIFY)
TYPE_LABELS = {"scan": "扫描", "plan": "清理计划生成", "apply": "清理执行"}
STATUS_LABELS = {RUNNING: "运行中", STOPPING: "已请求停止", SUCCESS: "已完成", PARTIAL: "部分完成",
                 FAILED: "失败", INTERRUPTED: "已中断", VERIFY: "待核实"}
# 占用（请求线程）与后台线程接管之间有短暂的无锁窗口：宽限期内不把活跃行判死。
STALE_AFTER = 120.0

_SCHEMA = """CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT DEFAULT '',
    scan_id TEXT DEFAULT '',
    plan_id TEXT DEFAULT '',
    created_ts REAL,
    updated_ts REAL,
    finished_ts REAL,
    done INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    current TEXT DEFAULT '',
    result TEXT,
    error TEXT)"""


class Busy(OSError):
    """工作区已被占用。task 属性携带当前活跃任务行（可能为 None：只有
    跨进程操作锁被持有、尚无任务行，例如 CLI 引擎正在干活）。"""

    def __init__(self, task=None):
        label = TYPE_LABELS.get((task or {}).get("type"), "后台任务")
        super().__init__(f"同一工作区同时只运行一个后台任务：{label}正在进行中")
        self.task = task


def _now():
    return time.time()


def _db_path(cfg):
    return Path(cfg["state_dir"]) / "inventory.sqlite3"


@contextlib.contextmanager
def _db(cfg):
    db = sqlite3.connect(_db_path(cfg), timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(_SCHEMA)
        db.commit()
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def oplock(cfg, exclusive=True):
    """state_dir 跨进程操作锁（自 videodedupe.state 原样提取，行为不变）。

    exclusive=True 非阻塞独占，争用抛 BlockingIOError/OSError——这是 Web 与
    CLI 共用的单写者门；exclusive=False 仅确保锁文件存在（读者不锁，SQLite
    WAL 并发服务读请求）。关闭句柄即释放。"""
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
        yield lock
    finally:
        lock.close()


def lock_free(cfg):
    """探测操作锁是否空闲：试探性独占后立即释放（供诊断/测试使用）。"""
    try:
        with oplock(cfg):
            return True
    except OSError:
        return False


def _row(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None


def get(cfg, task_id):
    with _db(cfg) as db:
        return _row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))


def active(cfg):
    """当前活跃任务（running/stopping）；状态库不可读时按无任务呈现。"""
    try:
        with _db(cfg) as db:
            return _row(db, "SELECT * FROM tasks WHERE status IN ('running','stopping')"
                            " ORDER BY updated_ts DESC, rowid DESC LIMIT 1")
    except (OSError, sqlite3.Error):
        return None


def latest(cfg, ttype):
    """某类型最近一条任务记录（含终态），供状态/结果页读取。"""
    try:
        with _db(cfg) as db:
            return _row(db, "SELECT * FROM tasks WHERE type=? ORDER BY created_ts DESC, rowid DESC LIMIT 1", (ttype,))
    except (OSError, sqlite3.Error):
        return None


def claim(cfg, ttype, scan_id="", plan_id=""):
    """检查与占用原子完成：独占操作锁期间查活跃任务并落 running 行。

    活跃行超过宽限期无心跳且我们拿到了锁（活任务必然持锁干活）→ 判定属主
    进程已退出，标记 interrupted 后允许重新占用。占用成功后 cfg['_task_id']
    指向该行，引擎入口 tasks.run 据此收养，不重复占用。"""
    if ttype not in TYPES:
        raise ValueError(f"未知任务类型: {ttype!r}")
    task_id = uuid.uuid4().hex
    try:
        with oplock(cfg):
            with _db(cfg) as db:
                current = _row(db, "SELECT * FROM tasks WHERE status IN ('running','stopping')"
                                   " ORDER BY updated_ts DESC, rowid DESC LIMIT 1")
                if current and _now() - (current["updated_ts"] or 0) > STALE_AFTER:
                    # 锁此刻由本调用持有：活跃行属主若还活着必然持有锁，故超时即死。
                    if current["type"] == "apply":
                        # §5B：stale 清理行的执行结果不确定，与其余入口共用 apply_outcome 定级。
                        status, summary = apply_outcome(cfg, current["plan_id"])
                        _finish(db, current["id"], status, result=summary,
                                error="任务长时间无进展且未持有操作锁，判定属主进程已退出；清理结果按删除日志定级，需人工核实")
                    else:
                        _finish(db, current["id"], INTERRUPTED, error="任务长时间无进展且未持有操作锁，判定属主进程已退出")
                    db.commit()
                    current = _row(db, "SELECT * FROM tasks WHERE status IN ('running','stopping') LIMIT 1")
                if current:
                    raise Busy(current)
                db.execute("INSERT INTO tasks(id,type,status,scan_id,plan_id,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?)",
                           (task_id, ttype, RUNNING, scan_id or "", plan_id or "", _now(), _now()))
                db.commit()
    except Busy:
        raise
    except OSError as e:  # oplock 争用：另一进程（CLI 引擎/旧 webui）正持锁干活
        with contextlib.suppress(sqlite3.Error, OSError):
            with _db(cfg) as db:
                current = _row(db, "SELECT * FROM tasks WHERE status IN ('running','stopping') LIMIT 1")
        raise Busy(current) from e
    cfg["_task_id"] = task_id
    return get(cfg, task_id)


def _finish(db, task_id, status, result=None, error=None):
    return db.execute(
        "UPDATE tasks SET status=?, result=?, error=?, finished_ts=?, updated_ts=? "
        "WHERE id=? AND status IN ('running','stopping')",
        (status,
         json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
         (error or "")[:500] or None, _now(), _now(), task_id)).rowcount


def finish(cfg, status, result=None, error=None, task_id=None):
    """任务收尾（幂等：只有活跃行可被终结；重复调用不改已终结状态）。"""
    if status not in TERMINAL:
        raise ValueError(f"未知终态: {status!r}")
    tid = task_id or cfg.get("_task_id")
    if not tid:
        return 0
    try:
        with _db(cfg) as db:
            changed = _finish(db, tid, status, result=result, error=error)
            db.commit()
    finally:
        if cfg.get("_task_id") in (tid, None):
            cfg.pop("_task_id", None)
    return changed


def reflect(cfg, data):
    """write_progress 的任务侧镜像：只更新活跃行，失败不阻断引擎主流程。"""
    tid = cfg.get("_task_id")
    if not tid or not isinstance(data, dict):
        return
    sets, args = [], []
    for column, k in (("phase", "phase"), ("done", "done"), ("total", "total"), ("current", "current")):
        if k in data:
            sets.append(f"{column}=?")
            args.append(data[k])
    if data.get("scan_id"):
        sets.append("scan_id=?")
        args.append(data["scan_id"])
    if not sets:
        return
    sets.append("updated_ts=?")
    args.append(_now())
    try:
        with _db(cfg) as db:
            db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND status IN ('running','stopping')",
                       (*args, tid))
            db.commit()
    except sqlite3.Error:
        pass  # 进度镜像尽力而为；progress.json 仍是逐文件进度的主源


def request_stop(cfg, task_id):
    """记录停止请求（running→stopping）。实际停止仍由引擎的取消检查完成。"""
    try:
        with _db(cfg) as db:
            changed = db.execute("UPDATE tasks SET status='stopping', updated_ts=? WHERE id=? AND status='running'",
                                 (_now(), task_id)).rowcount
            db.commit()
            return bool(changed)
    except sqlite3.Error:
        return False


def journal_summary(cfg, plan_id):
    """从计划 journal 汇总确定性结果：已删除/出错条数与未决暂存项。

    重启核对与清理失败分类共用；只读不写，绝不据此自动重放任何操作。
    §5B：读不到 journal ≠ 确认没执行——文件缺失/损坏（非法 JSON 或结构
    不是预期的 dict/列表）时 readable=False 且 pending/deleted 置 None
    （不可证未执行），绝不把读取失败降级为零计数。"""
    path = Path(cfg["state_dir"]) / f"{plan_id}.journal.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events") or []
        pending = data.get("pending") or None
        return {"journal": str(path), "readable": True,
                "deleted": sum(1 for e in events if e.get("status") == "deleted"),
                "errors": sum(1 for e in events if e.get("status") == "error"),
                "pending": bool(pending), "pending_key": (pending or {}).get("key", "")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"journal": str(path), "readable": False, "deleted": None, "errors": None,
                "pending": None, "pending_key": ""}


def apply_outcome(cfg, plan_id):
    """apply 收尾定级（§5B）：读 journal 按可证事实分级，全部「不确定执行」
    入口（执行器异常、键盘中断、stale 回收、重启 reconcile）共用同一语义。

    pending 为真或为 None（journal 不可读，不可证未执行）→ VERIFY；
    deleted 非零 → PARTIAL；否则 FAILED。返回 (status, summary)，
    绝不据此自动重放任何操作。"""
    try:
        summary = journal_summary(cfg, plan_id)
    except Exception:  # 汇总自身出错同样不可证未执行，绝不降级为 failed
        summary = {"journal": "", "readable": False, "deleted": None, "errors": None,
                   "pending": None, "pending_key": ""}
    if summary["pending"] is None or summary["pending"]:
        status = VERIFY
    elif summary["deleted"]:
        status = PARTIAL
    else:
        status = FAILED
    return status, summary


@contextlib.contextmanager
def run(cfg, ttype, scan_id="", plan_id=""):
    """引擎任务入口：收养（webui 请求线程已 claim，cfg 带 _task_id）或占用
    （CLI 直跑引擎），运行结束后按结果收尾。异常分类：清理中途结束（异常/
    键盘中断）结合 journal 经 apply_outcome 定级 verify/partial/failed
    （§5B：不可证未执行一律 verify）；其余类型键盘中断记 interrupted。"""
    if cfg.get("_task_id"):
        task = get(cfg, cfg["_task_id"])
        if not task or task["status"] not in ACTIVE or task["type"] != ttype:
            raise ValueError(f"任务 {cfg['_task_id']} 已终结或类型不符，拒绝收养")
    else:
        task = claim(cfg, ttype, scan_id, plan_id)
    try:
        yield task
    except KeyboardInterrupt:
        if ttype == "apply":
            # §5B：清理被中断＝执行结果不确定，与其余入口同一定级，不记 interrupted。
            status, summary = apply_outcome(cfg, plan_id or (task or {}).get("plan_id", ""))
            finish(cfg, status, result=summary,
                   error="任务被中断信号终止；清理结果不确定，需按删除日志核实")
        else:
            finish(cfg, INTERRUPTED, error="任务被中断信号终止")
        raise
    except Exception as e:
        if ttype == "apply":
            status, summary = apply_outcome(cfg, plan_id or (task or {}).get("plan_id", ""))
            finish(cfg, status, result=summary, error=str(e)[:500])
        else:
            finish(cfg, FAILED, error=str(e)[:500])
        raise
    else:
        finish(cfg, SUCCESS, result=(task or {}).get("result"))


def reconcile(cfg):
    """重启核对未完成任务（webui 启动时调用）。

    先探测操作锁：被占用说明仍有活进程在干活（如 CLI 引擎在跑），整体跳过，
    绝不误判。锁空闲时：扫描/计划 → interrupted；清理 → apply_outcome 定级
    （§5B：不可证未执行一律 verify）并把 journal 汇总写进结果（用户按日志
    核实，重跑同计划时已删项由 journal 幂等跳过，不确定操作绝不自动重放）。
    返回被核对的行列表。"""
    out = []
    try:
        with oplock(cfg):
            with _db(cfg) as db:
                rows = db.execute("SELECT * FROM tasks WHERE status IN ('running','stopping')").fetchall()
                for row in rows:
                    row = dict(row)
                    if row["type"] == "apply":
                        status, summary = apply_outcome(cfg, row["plan_id"])
                        _finish(db, row["id"], status, result=summary,
                                error="应用重启时清理未完成，需按删除日志核实；已删项重跑时自动跳过，不确定操作绝不自动重放")
                    else:
                        _finish(db, row["id"], INTERRUPTED, error="应用重启时任务未完成")
                    out.append(row)
                db.commit()
    except OSError:
        return out  # 锁被占用：仍有活任务，跳过本次核对
    except sqlite3.Error:
        return out  # 状态库暂不可读：尽力而为，不阻断启动
    return out

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regency.py — 監國「看守隔離區」（2026-09-06）

這是什麼：CC 額度用完、Codex／Grok 暫代那幾小時，把代理者的寫入圈進一個隔離資料夾，
        並在交還時自動列出「它到底改了哪些檔」。

為什麼：2026-08 那次代理，Codex 直接寫公司共用帳本（DECISIONS.md、記憶檔、handoff），
        CC 回來後 DEC 撞號三次、同一個 run 被兩邊各做一遍，還得自己找哪裡被改。
        兩顆腦寫同一本帳是所有麻煩的根；本模組把「做事」和「收拾」分開。

怎麼用：
    python regency.py start --leader CODEX   # 開一期：建隔離區、拍快照、禁區設唯讀（冪等）
    python regency.py end                    # 結束：解鎖、比對快照、寫 RECLAIM_REPORT.md、歸檔到 _archive/
    python regency.py status                 # 現在有沒有看守期、路徑、鎖了幾個
    python regency.py --selftest             # 自檢（unittest，全走暫存資料夾）

設計要點：
  - 禁區靠 Windows 唯讀屬性擋，PermissionError 是設計不是故障；本來就唯讀的檔記下來，end 時不去解。
  - 快照只算 < 2MB 的檔的 sha1，大檔只記 size/mtime；排除 log／sqlite／pyc／__pycache__／.git 等。
  - 全部路徑可用環境變數覆寫（REGENCY_ROOT／REGENCY_PROTECTED／REGENCY_WATCH），測試靠這個隔離。
  - 只讀 WATCH 範圍、不刪任何東西、不碰 pending_for_cc.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = "C:/Users/Charles/.agents/shared/regency"
DEFAULT_PROTECTED = [
    "D:/ai-memory/core/DECISIONS.md",
    "C:/Users/Charles/CLAUDE.md",
    "C:/Users/Charles/.claude/settings.json",
    "C:/Users/Charles/.claude/rules",
    "C:/Users/Charles/.claude/skills",
    "C:/Users/Charles/.agents/memory",
    "C:/Users/Charles/claude/session-handoff-active.md",
]
DEFAULT_WATCH = [
    "C:/Users/Charles/scripts",
    "C:/Users/Charles/.claude/hooks",
    "C:/Users/Charles/.agents/shared",
]
PENDING = Path("C:/Users/Charles/.agents/shared/pending_for_cc.md")
BIG = 2 * 1024 * 1024
SKIP_EXT = {".log", ".sqlite", ".sqlite-shm", ".sqlite-wal", ".pyc"}
# 2026-09-06 實測：~/scripts 5.7 萬檔裡 4 萬是 venv／site-packages／node_modules，快照 4 分 20 秒被裁判掛鉤逾時砍掉；排掉這些
SKIP_DIR = {"__pycache__", ".git", "node_modules", "_archive", "site-packages", ".venv", "venv", "env", ".tox",
            "dist", "build", ".cache", ".pytest_cache", ".mypy_cache", "logs", "log"}
# 監國自己每輪會動的檔，比對時當噪音排掉（否則每份報告都有它們）
SKIP_NAMES = {"leadership.json", "quota_evidence.jsonl", ".lead_referee_last_processed.json", "broadcast.md",
              "takeover_briefing.md", "pending_for_cc.md"}
EXIT_NO_CURRENT = 3


def _root() -> Path:
    return Path(os.environ.get("REGENCY_ROOT", DEFAULT_ROOT))


def _list_env(name: str, default: list[str]) -> list[Path]:
    v = os.environ.get(name)
    items = [x for x in v.split(";") if x.strip()] if v else default
    return [Path(x) for x in items]


def _protected() -> list[Path]:
    return _list_env("REGENCY_PROTECTED", DEFAULT_PROTECTED)


def _watch() -> list[Path]:
    return _list_env("REGENCY_WATCH", DEFAULT_WATCH)


def current() -> Path | None:
    f = _root() / "current"
    try:
        p = Path(f.read_text(encoding="utf-8").strip())
        return p if p.exists() else None
    except OSError:
        return None


# ---------------------------------------------------------------- 快照
def _iter_files(roots: list[Path]):
    for r in roots:
        if r.is_file():
            yield r
            continue
        if not r.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(r):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in SKIP_EXT or fn in SKIP_NAMES or "regency" in p.parts:
                    continue
                yield p


def snapshot(roots: list[Path]) -> dict:
    out: dict[str, dict] = {}
    for p in _iter_files(roots):
        try:
            st = p.stat()
        except OSError:
            continue
        rec = {"size": st.st_size, "mtime": st.st_mtime, "sha1": ""}
        if st.st_size < BIG:
            try:
                h = hashlib.sha1()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 16), b""):
                        h.update(chunk)
                rec["sha1"] = h.hexdigest()
            except OSError:
                pass
        out[str(p)] = rec
    return out


def diff_snapshots(a: dict, b: dict) -> dict:
    added = sorted(k for k in b if k not in a)
    removed = sorted(k for k in a if k not in b)
    changed = sorted(k for k in a if k in b and (a[k]["sha1"], a[k]["size"]) != (b[k]["sha1"], b[k]["size"]))
    return {"added": added, "changed": changed, "removed": removed}


# ---------------------------------------------------------------- 禁區
def _is_readonly(p: Path) -> bool:
    return not os.access(p, os.W_OK)


def lock_protected(paths: list[Path]) -> tuple[list[str], list[str], list[str]]:
    """回 (鎖上的, 本來就唯讀的, 不存在的)。"""
    locked, already, missing = [], [], []
    for root in paths:
        if not root.exists():
            missing.append(str(root))
            continue
        for p in _iter_files([root]) if root.is_dir() else [root]:
            if _is_readonly(p):
                already.append(str(p))
                continue
            try:
                os.chmod(p, stat.S_IREAD)
                locked.append(str(p))
            except OSError:
                missing.append(str(p))
    return locked, already, missing


def unlock(paths: list[str]) -> int:
    n = 0
    for s in paths:
        p = Path(s)
        if p.exists():
            try:
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------- start / end
LEDGER_TMPL = """# 看守帳（LEDGER）— {started} leader={leader}

> 代理期間做的每一件事記一行。CC 回來只看這份與 RECLAIM_REPORT.md。

| 時間 | 做了什麼 | 產物路徑 | 需要 CC 決定？ |
|---|---|---|---|
"""


def start(leader: str = "CODEX") -> Path:
    root = _root()
    cur = current()
    if cur:
        print(f"看守期已在進行：{cur}（冪等，不重開）")
        return cur
    started = datetime.now().astimezone()
    d = root / started.strftime("%Y%m%d-%H%M")
    (d / "work").mkdir(parents=True, exist_ok=True)
    (d / "LEDGER.md").write_text(LEDGER_TMPL.format(started=started.isoformat(timespec="minutes"), leader=leader), encoding="utf-8")
    snap = snapshot(_watch())
    (d / "snapshot_start.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    locked, already, missing = lock_protected(_protected())
    meta = {"leader": leader, "started_at": started.isoformat(), "protected": [str(p) for p in _protected()],
            "locked": locked, "already_readonly": already, "missing": missing, "watch": [str(p) for p in _watch()]}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "current").write_text(str(d), encoding="utf-8")
    print(f"看守期已開：{d}｜禁區 {len(locked)} 檔已鎖（本來就唯讀 {len(already)}，不存在 {len(missing)}）｜快照 {len(snap)} 檔")
    return d


def _report(d: Path, meta: dict, diff: dict, work_files: list[str], ended: datetime) -> str:
    def lst(items):
        return "\n".join(f"- `{x}`" for x in items) if items else "- （無）"
    pend = ""
    try:
        if PENDING.exists() and datetime.fromtimestamp(PENDING.stat().st_mtime).astimezone() > datetime.fromisoformat(meta["started_at"]):
            pend = PENDING.read_text(encoding="utf-8")
    except (OSError, ValueError):
        pass
    ledger = (d / "LEDGER.md").read_text(encoding="utf-8") if (d / "LEDGER.md").exists() else "（無）"
    return f"""# 交還報告（RECLAIM_REPORT）

## 期間
- leader={meta.get('leader')}｜開始 {meta.get('started_at')}｜結束 {ended.isoformat(timespec='minutes')}

## 禁區鎖定結果
- 鎖上 {len(meta.get('locked', []))} 檔，已解鎖；本來就唯讀 {len(meta.get('already_readonly', []))} 檔未動；不存在 {len(meta.get('missing', []))}。

## 看守期間改動的檔案（快照比對，範圍：{'、'.join(meta.get('watch', []))}）
### 修改 {len(diff['changed'])}
{lst(diff['changed'])}
### 新增 {len(diff['added'])}
{lst(diff['added'])}
### 刪除 {len(diff['removed'])}
{lst(diff['removed'])}

## 隔離區產物（work/）{len(work_files)}
{lst(work_files)}

## LEDGER 原文
{ledger}

## 排隊等 CC 的事項（pending_for_cc.md，若看守期間有更新）
{pend or '（無更新）'}
"""


def end() -> Path:
    d = current()
    if not d:
        print("沒有進行中的看守期（regency/current 不存在或路徑已失效）。")
        sys.exit(EXIT_NO_CURRENT)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    n = unlock(meta.get("locked", []))
    snap_start = json.loads((d / "snapshot_start.json").read_text(encoding="utf-8"))
    snap_end = snapshot([Path(p) for p in meta.get("watch", [])] or _watch())
    # 隔離區自己在 WATCH（.agents/shared）底下會被算成新增，排掉
    snap_end = {k: v for k, v in snap_end.items() if not k.startswith(str(d))}
    (d / "snapshot_end.json").write_text(json.dumps(snap_end, ensure_ascii=False), encoding="utf-8")
    diff = diff_snapshots(snap_start, snap_end)
    work_files = sorted(str(p) for p in (d / "work").rglob("*") if p.is_file())
    ended = datetime.now().astimezone()
    (d / "RECLAIM_REPORT.md").write_text(_report(d, meta, diff, work_files, ended), encoding="utf-8")
    arch = _root() / "_archive" / d.name
    arch.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(arch))
    try:
        (_root() / "current").unlink()
    except OSError:
        pass
    print(f"看守期已結束｜解鎖 {n}｜改動 新增{len(diff['added'])} 修改{len(diff['changed'])} 刪除{len(diff['removed'])}｜報告：{arch / 'RECLAIM_REPORT.md'}")
    return arch / "RECLAIM_REPORT.md"


def status() -> int:
    d = current()
    if not d:
        print("看守期：無（CC 當家或尚未開）")
        return 0
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    print(f"看守期：進行中｜leader={meta.get('leader')}｜開始 {meta.get('started_at')}｜隔離區 {d}｜鎖 {len(meta.get('locked', []))} 檔")
    return 0


# ---------------------------------------------------------------- 自檢
def _selftest() -> int:
    import tempfile
    import unittest

    class T(unittest.TestCase):
        def setUp(self):
            self.td = Path(tempfile.mkdtemp())
            self.root = self.td / "root"
            self.watch = self.td / "watch"; self.watch.mkdir()
            self.prot = self.td / "prot"; self.prot.mkdir()
            (self.watch / "keep.py").write_text("a", encoding="utf-8")
            (self.watch / "mod.py").write_text("b", encoding="utf-8")
            (self.watch / "del.py").write_text("c", encoding="utf-8")
            (self.watch / "skip.log").write_text("x", encoding="utf-8")
            self.p1 = self.prot / "DECISIONS.md"; self.p1.write_text("d", encoding="utf-8")
            self.p2 = self.prot / "was_ro.md"; self.p2.write_text("e", encoding="utf-8"); os.chmod(self.p2, stat.S_IREAD)
            os.environ["REGENCY_ROOT"] = str(self.root)
            os.environ["REGENCY_WATCH"] = str(self.watch)
            os.environ["REGENCY_PROTECTED"] = f"{self.prot};{self.td / 'missing.md'}"

        def tearDown(self):
            for p in self.td.rglob("*"):
                try:
                    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
            shutil.rmtree(self.td, ignore_errors=True)

        def test_1_start_builds(self):
            d = start("CODEX")
            self.assertTrue((self.root / "current").exists())
            self.assertTrue((d / "LEDGER.md").exists())
            snap = json.loads((d / "snapshot_start.json").read_text(encoding="utf-8"))
            self.assertEqual(len(snap), 3)  # .log 被排除

        def test_2_protected_locked_then_unlocked(self):
            start("CODEX")
            with self.assertRaises(PermissionError):
                open(self.p1, "a", encoding="utf-8").write("x")
            end()
            open(self.p1, "a", encoding="utf-8").write("x")

        def test_3_already_readonly_stays(self):
            start("CODEX"); end()
            self.assertFalse(os.access(self.p2, os.W_OK))

        def test_4_diff_three_kinds(self):
            start("CODEX")
            (self.watch / "new.py").write_text("n", encoding="utf-8")
            (self.watch / "mod.py").write_text("changed", encoding="utf-8")
            (self.watch / "del.py").unlink()
            rp = end()
            t = rp.read_text(encoding="utf-8")
            self.assertIn("new.py", t.split("### 新增")[1].split("### 刪除")[0])
            self.assertIn("mod.py", t.split("### 修改")[1].split("### 新增")[0])
            self.assertIn("del.py", t.split("### 刪除")[1])
            self.assertNotIn("keep.py", t)

        def test_5_idempotent_start(self):
            d1 = start("CODEX"); d2 = start("CODEX")
            self.assertEqual(d1, d2)
            self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]), 1)

        def test_6_end_without_current(self):
            with self.assertRaises(SystemExit) as cm:
                end()
            self.assertEqual(cm.exception.code, EXIT_NO_CURRENT)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    ap = argparse.ArgumentParser(description="監國看守隔離區")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start"); s.add_argument("--leader", default="CODEX")
    s.add_argument("--force", action="store_true", help="CC 當家時仍強制開（演習用）")
    sub.add_parser("end"); sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        # 看守期只在 CC 交出主導權時才有意義。CC 當家還開，等於把 3411 個檔鎖給不存在的看守者
        # （2026-09-07 06:30 孤兒看守期實證）。狀態檔讀不到就放行（fail-open，照舊）。
        if not a.force:
            try:
                st = json.loads(Path(r"C:/Users/Charles/.agents/shared/leadership.json").read_text(encoding="utf-8"))
                if st.get("leader") == "CC":
                    print("拒開：leadership.json 說 CC 當家，沒有人要看守。演習請加 --force。")
                    return 3
            except (OSError, ValueError):
                pass
        start(a.leader.upper()); return 0
    if a.cmd == "end":
        end(); return 0
    return status()


if __name__ == "__main__":
    sys.exit(main())

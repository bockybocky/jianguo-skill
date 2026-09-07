# -*- coding: utf-8 -*-
"""
lead_referee.py — SOLE writer of leadership.json for the multi-LLM regency protocol.

Tracks which engine (CC / CODEX / GROK / NONE) currently leads automation based on
hard quota-limit evidence, and flips leadership forward/back via probes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# UTF-8 stdout/stderr (guarded for unittest capture / already-wrapped streams)
try:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Module-level path & timing constants (monkeypatchable by tests)
# ---------------------------------------------------------------------------
SCRIPTS_DIR = r"C:\Users\Charles\scripts"
SHARED_DIR = r"C:\Users\Charles\.agents\shared"

STATE_PATH = os.path.join(SHARED_DIR, "leadership.json")
EVIDENCE_PATH = os.path.join(SHARED_DIR, "quota_evidence.jsonl")
SIDECAR_PATH = os.path.join(SHARED_DIR, ".lead_referee_last_processed.json")
BROADCAST_PATH = os.path.join(SHARED_DIR, "broadcast.md")
LOCK_PATH = os.path.join(os.environ.get("TEMP") or r"C:\Windows\Temp", "lead_referee.lock")

CLAUDE_CMD = r"C:\Users\Charles\AppData\Roaming\npm\claude.cmd"
CLAUDE_SETTINGS = r"C:\Users\Charles\scripts\line\claude_clean_settings.json"
CLAUDE_SANDBOX = os.path.join(
    os.environ.get("TEMP") or r"C:\Windows\Temp", "claude_sandbox"
)
CODEX_CMD = r"D:\codex-bin\codex.cmd"
GROK_CALL_SH = r"C:\Users\Charles\scripts\grok_call.sh"
DISCORD_NOTIFY = os.path.join(SCRIPTS_DIR, "discord_notify.py")

EVIDENCE_RECENCY_MINUTES = 30
PROBE_FAIL_BUMP_MINUTES = 30
DEFAULT_RESET_HOURS = 5
LOCK_STALE_MINUTES = 10

PROBE_CC_TIMEOUT = 90
PROBE_CODEX_TIMEOUT = 120
PROBE_GROK_TIMEOUT = 90
DISCORD_TIMEOUT = 30

VALID_LEADERS = frozenset({"CC", "CODEX", "GROK", "NONE"})
VALID_MODES = frozenset({"AUTO", "LOCKED"})
VALID_REASONS = frozenset({
    "startup", "cc_quota_hit", "codex_quota_hit", "grok_quota_hit",
    "probe_restored", "manual",
})

LEADER_LABELS = {
    "CC": "CC",
    "CODEX": "Codex",
    "GROK": "Grok",
    "NONE": "全員",
}
REASON_ZH = {
    "cc_quota_hit": "CC額度用盡",
    "codex_quota_hit": "Codex額度用盡",
    "grok_quota_hit": "Grok額度用盡（三路全掛）",
}

# Lock handle held while process owns the lock
_lock_fh: Any = None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def is_past_or_null(estimate: Optional[str], now: datetime) -> bool:
    """True if estimate is None/unparseable or now > estimate (already due)."""
    if estimate is None:
        return True
    dt = parse_iso(estimate)
    if dt is None:
        return True
    # Compare timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return now > dt


# ---------------------------------------------------------------------------
# Lockfile (single-instance)
# ---------------------------------------------------------------------------

def acquire_lock(lock_path: Optional[str] = None) -> bool:
    """
    Acquire exclusive non-blocking lock.
    Returns True on success, False if another instance holds the lock.
    """
    global _lock_fh
    path = lock_path or LOCK_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except Exception:
        pass

    # Stale lock cleanup: if mtime older than LOCK_STALE_MINUTES, remove
    if os.path.exists(path):
        try:
            mtime = os.path.getmtime(path)
            age_sec = time.time() - mtime
            if age_sec > LOCK_STALE_MINUTES * 60:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    # Prefer msvcrt.locking on Windows
    try:
        import msvcrt  # type: ignore

        fh = open(path, "a+b")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            _lock_fh = fh
            return True
        except OSError:
            fh.close()
            return False
    except ImportError:
        pass

    # Fallback: O_EXCL create
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        _lock_fh = os.fdopen(fd, "wb")
        return True
    except FileExistsError:
        # Stale retry already done above; if still exists, fail
        return False
    except OSError:
        return False


def release_lock(lock_path: Optional[str] = None) -> None:
    global _lock_fh
    path = lock_path or LOCK_PATH
    fh = _lock_fh
    _lock_fh = None
    if fh is not None:
        try:
            try:
                import msvcrt  # type: ignore
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            fh.close()
        except Exception:
            pass
    # For O_EXCL style, remove the lock file so next acquire can create
    try:
        if os.path.exists(path):
            # Only remove if we used excl style (no msvcrt hold); safe best-effort
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# State read / write (atomic + optimistic concurrency)
# ---------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "leader": "CC",
        "mode": "AUTO",
        "since": now_iso(),
        "reason": "startup",
        "cc_reset_estimate": None,
        "codex_reset_estimate": None,
        "epoch": 0,
    }


def read_state(path: Optional[str] = None) -> tuple[dict, int]:
    """
    Read state file. Returns (state_dict, epoch_at_read).
    If missing/corrupt, returns a fresh default (epoch 0) WITHOUT writing.
    Caller should use ensure_state / write_state to persist rebuilds.
    """
    p = path or STATE_PATH
    if not os.path.exists(p):
        return default_state(), -1  # -1 signals "did not exist"
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        epoch = int(data.get("epoch", 0))
        return data, epoch
    except Exception:
        return default_state(), -2  # -2 signals "corrupt"


def ensure_state(path: Optional[str] = None) -> dict:
    """
    Ensure state file exists and is valid JSON. Rebuilds if missing/corrupt.
    Writes the rebuilt state. Returns the state dict.
    """
    p = path or STATE_PATH
    state, epoch_flag = read_state(p)
    if epoch_flag < 0:
        msg = "leadership.json missing" if epoch_flag == -1 else "leadership.json corrupted"
        print(f"WARNING: {msg}; rebuilding default state (leader=CC).", file=sys.stderr)
        state = default_state()
        # Direct write for rebuild (no concurrency recheck needed for fresh create)
        _atomic_write_json(p, state)
        return state
    return state


def _atomic_write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".leadership_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def write_state(
    new_state: dict,
    expected_epoch: int,
    path: Optional[str] = None,
) -> bool:
    """
    Atomic write with optimistic concurrency.
    Re-reads file right before write; if epoch != expected_epoch, ABANDON write.
    expected_epoch of -1 or -2 means file was missing/corrupt at read time:
      - if file still missing, write is allowed (fresh create)
      - if file now exists with any epoch, abandon (someone else wrote)
    On success, sets new_state['epoch'] = expected_epoch + 1 (or 0 for rebuilds
    that already have epoch 0 and expected_epoch < 0).
    Returns True if write succeeded, False if abandoned.
    """
    p = path or STATE_PATH
    # Re-read just before write
    current, current_epoch = read_state(p)

    if expected_epoch < 0:
        # We thought file was missing/corrupt
        if current_epoch >= 0:
            # Someone else created/fixed it — abandon
            return False
        # Still missing/corrupt — write as-is (caller should set epoch appropriately)
        to_write = dict(new_state)
        if "epoch" not in to_write or to_write.get("epoch") is None:
            to_write["epoch"] = 0
        _atomic_write_json(p, to_write)
        return True

    if current_epoch != expected_epoch:
        # Concurrent modification — abandon
        return False

    to_write = dict(new_state)
    to_write["epoch"] = expected_epoch + 1
    _atomic_write_json(p, to_write)
    return True


# ---------------------------------------------------------------------------
# Sidecar (last processed evidence ts)
# ---------------------------------------------------------------------------

def read_sidecar(path: Optional[str] = None) -> Optional[str]:
    p = path or SIDECAR_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_processed_ts")
    except Exception:
        return None


def write_sidecar(ts: str, path: Optional[str] = None) -> None:
    p = path or SIDECAR_PATH
    directory = os.path.dirname(p) or "."
    os.makedirs(directory, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"last_processed_ts": ts}, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def read_evidence(
    path: Optional[str] = None,
    now: Optional[datetime] = None,
    last_processed_ts: Optional[str] = None,
) -> list[dict]:
    """
    Read recent (last EVIDENCE_RECENCY_MINUTES), not-yet-processed evidence lines.
    Malformed lines skipped. Missing file => empty list.
    """
    p = path or EVIDENCE_PATH
    if not os.path.exists(p):
        return []
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(minutes=EVIDENCE_RECENCY_MINUTES)
    last_dt = parse_iso(last_processed_ts) if last_processed_ts else None
    if last_dt is not None and last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=now.tzinfo)

    results: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts_str = obj.get("ts")
                if not ts_str:
                    continue
                ts_dt = parse_iso(ts_str)
                if ts_dt is None:
                    continue
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=now.tzinfo)
                if ts_dt < cutoff:
                    continue
                if last_dt is not None and ts_dt <= last_dt:
                    continue
                engine = obj.get("engine")
                if engine not in ("claude", "codex", "grok"):
                    continue
                results.append(obj)
    except OSError:
        return []
    # Sort by ts ascending so we process oldest-first if multiple
    results.sort(key=lambda x: x.get("ts") or "")
    return results


def latest_evidence_for(evidence: list[dict], engine: str) -> Optional[dict]:
    matches = [e for e in evidence if e.get("engine") == engine]
    if not matches:
        return None
    return matches[-1]


# ---------------------------------------------------------------------------
# Reset estimate parsing
# ---------------------------------------------------------------------------

def parse_reset_estimate(signature: str, now: Optional[datetime] = None) -> str:
    """
    Best-effort parse of reset time from evidence signature.
    Fallback: now + DEFAULT_RESET_HOURS.
    """
    now = now or datetime.now().astimezone()
    fallback = (now + timedelta(hours=DEFAULT_RESET_HOURS)).isoformat()
    if not signature:
        return fallback
    try:
        # Keywords: reset, resets at, 重置 (U+91CD U+7F6E)
        kw_pat = re.compile(
            r"(?:resets?\s+at|reset|重置)",
            re.IGNORECASE,
        )
        m = kw_pat.search(signature)
        if not m:
            return fallback

        after = signature[m.start():]
        # Look for time tokens near/after keyword
        # Patterns: 5:30 PM, 17:30, 2026-07-12T21:00:00, 2026-07-12 21:00
        time_patterns = [
            # ISO-ish datetime
            r"(\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?)",
            # 12-hour clock with AM/PM
            r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",
            # 24-hour clock
            r"(\d{1,2}:\d{2}(?::\d{2})?)",
        ]
        for pat in time_patterns:
            tm = re.search(pat, after)
            if not tm:
                continue
            token = tm.group(1).strip()
            parsed = _parse_time_token(token, now)
            if parsed is not None:
                return parsed.isoformat()
        return fallback
    except Exception:
        return fallback


def _parse_time_token(token: str, now: datetime) -> Optional[datetime]:
    token = token.strip()
    # ISO-ish
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", token):
            s = token.replace(" ", "T") if " " in token and "T" not in token else token
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)
            return dt
    except Exception:
        pass

    # 12-hour AM/PM
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)", token)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt = dt + timedelta(days=1)
        return dt

    # 24-hour
    m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?$", token)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        second = int(m.group(3) or 0)
        if hour > 23 or minute > 59:
            return None
        dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if dt <= now:
            dt = dt + timedelta(days=1)
        return dt

    return None


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def probe_cc() -> bool:
    try:
        os.makedirs(CLAUDE_SANDBOX, exist_ok=True)
        cmd = [
            CLAUDE_CMD,
            "-p",
            "reply exactly: ok",
            "--model",
            "claude-haiku-4-5-20251001",
            "--settings",
            CLAUDE_SETTINGS,
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_CC_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            cwd=CLAUDE_SANDBOX,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0 and "ok" in out.lower()
    except Exception:
        return False


def probe_codex() -> bool:
    try:
        # Windows .cmd needs shell=True
        cmd = f'{CODEX_CMD} exec --sandbox read-only "reply exactly: ok"'
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_CODEX_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            shell=True,
        )
        # Primary: rc==0; secondary sanity: stdout non-empty preferred but not required
        return r.returncode == 0
    except Exception:
        return False


def probe_grok() -> bool:
    try:
        cmd = ["bash", GROK_CALL_SH, "reply ok", "60"]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_GROK_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def build_flip_message(old_leader: str, new_leader: str, reason: str) -> str:
    if reason == "probe_restored" and new_leader == "CC":
        return "CC 復權，監國結束。"
    if reason == "probe_restored":
        x = LEADER_LABELS.get(old_leader, old_leader)
        y = LEADER_LABELS.get(new_leader, new_leader)
        return f"公司主導權切換：{x}交棒{y}。原因：接棒恢復。"
    # quota-hit styles
    x = LEADER_LABELS.get(old_leader, old_leader)
    y = LEADER_LABELS.get(new_leader, new_leader)
    reason_zh = REASON_ZH.get(reason, reason)
    return f"公司主導權切換：{x}交棒{y}。原因：{reason_zh}。"


def notify_discord(text: str) -> None:
    try:
        now = now_iso()
        text_with_ts = f"[{now}] {text}"
        subprocess.run(
            [sys.executable, DISCORD_NOTIFY, text_with_ts],
            capture_output=True,
            text=True,
            timeout=DISCORD_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        print(f"notify_discord failed: {e}", file=sys.stderr)


def notify_broadcast(text: str) -> None:
    try:
        now = now_iso()
        block = f"## [{now}] Leadership Relay\n\n{text}\n\n"
        directory = os.path.dirname(BROADCAST_PATH) or "."
        os.makedirs(directory, exist_ok=True)
        with open(BROADCAST_PATH, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as e:
        print(f"notify_broadcast failed: {e}", file=sys.stderr)


def notify_all(text: str) -> None:
    """Fire all channels fail-quiet."""
    try:
        notify_discord(text)
    except Exception as e:
        print(f"notify_discord outer: {e}", file=sys.stderr)
    try:
        notify_broadcast(text)
    except Exception as e:
        print(f"notify_broadcast outer: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Evaluation cycle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 監國 v2 掛鉤（2026-09-06）：①CodexBar 餵證據 ②翻牌時開／關看守隔離區
# 兩個都 fail-open：任何例外只記 messages，不影響裁判本體。測試用 LEAD_V2_HOOKS=0 關掉。
# ---------------------------------------------------------------------------

# 正式狀態檔路徑在 import 時凍住。測試會把 STATE_PATH 換成暫存檔模擬翻牌；
# 那時掛鉤若照跑，會對「真」系統開看守期（2026-09-07 06:30 實證：自動掃測試的排程沒帶
# LEAD_V2_HOOKS=0，test_lead_referee.py 一翻牌就鎖了 3411 個真檔，直到 10:06 才被發現）。
# 所以不只看環境變數：狀態檔不是正式那一份，掛鉤一律不動真系統。
_PROD_STATE_PATH = os.path.normcase(os.path.abspath(STATE_PATH))


def _v2_hooks_enabled() -> bool:
    if os.environ.get("LEAD_V2_HOOKS", "1") == "0":
        return False
    return os.path.normcase(os.path.abspath(STATE_PATH)) == _PROD_STATE_PATH


def ingest_external_evidence(summary: dict) -> None:
    """裁判每輪先把 CodexBar 讀數翻成證據（quota_evidence_feed.py），補上 17 支不走 llm_call 的排程盲區。"""
    if not _v2_hooks_enabled():
        return
    try:
        import quota_evidence_feed  # 同目錄
        written = quota_evidence_feed.run()
        if written:
            summary["messages"].append(f"codexbar feed: 寫入 {len(written)} 筆證據")
    except Exception as e:  # noqa: BLE001
        summary["messages"].append(f"codexbar feed 失敗（忽略）: {e}")


def on_leader_change(old_leader: str, new_leader: str, summary: Optional[dict] = None) -> None:
    """CC 交出主導權 → 開看守期（隔離區＋禁區唯讀＋接棒簡報）；CC 收回 → 結束看守期（解鎖＋比對＋報告）。"""
    if not _v2_hooks_enabled() or old_leader == new_leader:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    py = sys.executable
    cmds: list[list[str]] = []
    if old_leader == "CC" and new_leader != "CC":
        cmds.append([py, os.path.join(here, "regency.py"), "start", "--leader", new_leader])
        cmds.append([py, os.path.join(here, "lead.py"), "handoff"])
    elif new_leader == "CC" and old_leader != "CC":
        cmds.append([py, os.path.join(here, "regency.py"), "end"])
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            line = (r.stdout or r.stderr or "").strip().splitlines()
            msg = f"v2 hook {os.path.basename(c[1])} {c[2]}: exit={r.returncode} {line[-1] if line else ''}"
        except Exception as e:  # noqa: BLE001
            msg = f"v2 hook {os.path.basename(c[1])} 失敗（忽略）: {e}"
        if summary is not None:
            summary["messages"].append(msg)
        else:
            print(msg)


def evaluate_cycle(dry_run: bool = False) -> dict:
    """
    One evaluation cycle. Returns a summary dict for dry-run / testing.
    """
    summary: dict[str, Any] = {
        "action": "none",
        "old_leader": None,
        "new_leader": None,
        "reason": None,
        "dry_run": dry_run,
        "messages": [],
    }

    state = ensure_state() if not dry_run else (
        read_state()[0] if os.path.exists(STATE_PATH) else default_state()
    )
    # For dry-run without file: do not create
    if dry_run and not os.path.exists(STATE_PATH):
        state = default_state()
        summary["messages"].append("dry-run: no state file; would use default leader=CC")
        summary["old_leader"] = state.get("leader")
        summary["new_leader"] = state.get("leader")
        return summary

    # Re-read with epoch for concurrency
    state, expected_epoch = read_state()
    if expected_epoch < 0 and not dry_run:
        # ensure_state already wrote; re-read
        state, expected_epoch = read_state()

    old_leader = state.get("leader", "CC")
    summary["old_leader"] = old_leader
    summary["new_leader"] = old_leader
    mode = state.get("mode", "AUTO")
    now = datetime.now().astimezone()

    if not dry_run:
        ingest_external_evidence(summary)
    last_ts = read_sidecar()
    evidence = read_evidence(now=now, last_processed_ts=last_ts)

    # Track newest evidence ts for sidecar update
    newest_ts: Optional[str] = None
    for e in evidence:
        ts = e.get("ts")
        if ts and (newest_ts is None or ts > newest_ts):
            newest_ts = ts

    # 1. LOCKED: no flip, may update sidecar
    if mode == "LOCKED":
        summary["action"] = "locked_noop"
        summary["messages"].append("mode=LOCKED; no flip")
        if not dry_run and newest_ts:
            write_sidecar(newest_ts)
        return summary

    flipped_this_run = False
    new_state = dict(state)
    notify_text: Optional[str] = None

    # 2. Quota-hit cascade (priority order)
    if old_leader == "CC":
        ev = latest_evidence_for(evidence, "claude")
        if ev:
            est = parse_reset_estimate(ev.get("signature") or "", now)
            new_state["leader"] = "CODEX"
            new_state["reason"] = "cc_quota_hit"
            new_state["since"] = now_iso()
            new_state["cc_reset_estimate"] = est
            flipped_this_run = True
            summary["action"] = "quota_flip"
            summary["reason"] = "cc_quota_hit"
            summary["new_leader"] = "CODEX"
            summary["messages"].append("CC -> CODEX (cc_quota_hit)")
            notify_text = build_flip_message("CC", "CODEX", "cc_quota_hit")

    elif old_leader == "CODEX":
        ev = latest_evidence_for(evidence, "codex")
        if ev:
            est = parse_reset_estimate(ev.get("signature") or "", now)
            new_state["leader"] = "GROK"
            new_state["reason"] = "codex_quota_hit"
            new_state["since"] = now_iso()
            new_state["codex_reset_estimate"] = est
            flipped_this_run = True
            summary["action"] = "quota_flip"
            summary["reason"] = "codex_quota_hit"
            summary["new_leader"] = "GROK"
            summary["messages"].append("CODEX -> GROK (codex_quota_hit)")
            notify_text = build_flip_message("CODEX", "GROK", "codex_quota_hit")

    elif old_leader == "GROK":
        ev = latest_evidence_for(evidence, "grok")
        if ev:
            new_state["leader"] = "NONE"
            new_state["reason"] = "grok_quota_hit"
            new_state["since"] = now_iso()
            flipped_this_run = True
            summary["action"] = "quota_flip"
            summary["reason"] = "grok_quota_hit"
            summary["new_leader"] = "NONE"
            summary["messages"].append("GROK -> NONE (grok_quota_hit)")
            notify_text = build_flip_message("GROK", "NONE", "grok_quota_hit")

    # 3. Regency probe (only if rule 2 did NOT flip this run)
    # NONE is handled first (case 3c exception: up to 3 probes/run) so it is not
    # swallowed by the single-target 3a "leader != CC" branch.
    if not flipped_this_run:
        leader = new_state.get("leader", old_leader)

        if leader == "NONE":
            # Case 3c: probe CC → CODEX → GROK, stop at first success (up to 3 probes/run).
            summary["messages"].append("probe due: NONE cascade (CC, CODEX, GROK)")
            if dry_run:
                summary["action"] = "would_probe_none_cascade"
                summary["messages"].append(
                    "dry-run: would probe CC then CODEX then GROK until success (not executed)"
                )
            else:
                restored = None
                if probe_cc():
                    restored = "CC"
                elif probe_codex():
                    restored = "CODEX"
                elif probe_grok():
                    restored = "GROK"

                if restored:
                    new_state["leader"] = restored
                    new_state["reason"] = "probe_restored"
                    new_state["since"] = now_iso()
                    flipped_this_run = True
                    summary["action"] = "probe_restore"
                    summary["reason"] = "probe_restored"
                    summary["new_leader"] = restored
                    summary["messages"].append(f"NONE cascade: {restored} alive")
                    notify_text = build_flip_message("NONE", restored, "probe_restored")
                else:
                    summary["action"] = "none_all_failed"
                    summary["messages"].append("NONE cascade: all probes failed; no state change")
                    if newest_ts:
                        write_sidecar(newest_ts)
                    return summary

        elif leader != "CC" and is_past_or_null(new_state.get("cc_reset_estimate"), now):
            summary["messages"].append("probe due: CC")
            if dry_run:
                summary["action"] = "would_probe_cc"
                summary["messages"].append("dry-run: would probe CC (not executed)")
            else:
                ok = probe_cc()
                if ok:
                    new_state["leader"] = "CC"
                    new_state["reason"] = "probe_restored"
                    new_state["since"] = now_iso()
                    new_state["cc_reset_estimate"] = None  # 復權即清冷卻，防調度層誤跳 claude
                    flipped_this_run = True
                    summary["action"] = "probe_restore"
                    summary["reason"] = "probe_restored"
                    summary["new_leader"] = "CC"
                    summary["messages"].append("probe CC ok -> leader=CC")
                    notify_text = build_flip_message(old_leader, "CC", "probe_restored")
                else:
                    bump = (now + timedelta(minutes=PROBE_FAIL_BUMP_MINUTES)).isoformat()
                    new_state["cc_reset_estimate"] = bump
                    # keep leader, reason, mode; partial write
                    summary["action"] = "probe_fail_bump"
                    summary["messages"].append(f"probe CC failed; bump cc_reset_estimate to {bump}")
                    if write_state(new_state, expected_epoch):
                        summary["messages"].append("state written (estimate bump)")
                    else:
                        summary["messages"].append("write abandoned (epoch mismatch)")
                    if newest_ts:
                        write_sidecar(newest_ts)
                    return summary

        elif leader == "GROK" and is_past_or_null(new_state.get("codex_reset_estimate"), now):
            summary["messages"].append("probe due: CODEX")
            if dry_run:
                summary["action"] = "would_probe_codex"
                summary["messages"].append("dry-run: would probe CODEX (not executed)")
            else:
                ok = probe_codex()
                if ok:
                    new_state["leader"] = "CODEX"
                    new_state["reason"] = "probe_restored"
                    new_state["since"] = now_iso()
                    new_state["codex_reset_estimate"] = None  # 復權即清冷卻
                    flipped_this_run = True
                    summary["action"] = "probe_restore"
                    summary["reason"] = "probe_restored"
                    summary["new_leader"] = "CODEX"
                    summary["messages"].append("probe CODEX ok -> leader=CODEX")
                    notify_text = build_flip_message(old_leader, "CODEX", "probe_restored")
                else:
                    bump = (now + timedelta(minutes=PROBE_FAIL_BUMP_MINUTES)).isoformat()
                    new_state["codex_reset_estimate"] = bump
                    summary["action"] = "probe_fail_bump"
                    summary["messages"].append(f"probe CODEX failed; bump codex_reset_estimate to {bump}")
                    if write_state(new_state, expected_epoch):
                        summary["messages"].append("state written (estimate bump)")
                    else:
                        summary["messages"].append("write abandoned (epoch mismatch)")
                    if newest_ts:
                        write_sidecar(newest_ts)
                    return summary

    # Persist flip / quota change
    if flipped_this_run or (
        new_state.get("leader") != old_leader
    ):
        if dry_run:
            summary["messages"].append(
                f"dry-run: would write leader={new_state.get('leader')} "
                f"reason={new_state.get('reason')}"
            )
            summary["new_leader"] = new_state.get("leader")
            return summary

        if write_state(new_state, expected_epoch):
            summary["messages"].append(
                f"wrote leader={new_state.get('leader')} reason={new_state.get('reason')}"
            )
            if notify_text:
                notify_all(notify_text)
                summary["messages"].append(f"notified: {notify_text}")
            on_leader_change(old_leader, new_state.get("leader", old_leader), summary)
        else:
            summary["messages"].append("write abandoned (epoch mismatch)")
            summary["action"] = "write_abandoned"
    else:
        summary["messages"].append("no flip needed")

    if not dry_run and newest_ts:
        write_sidecar(newest_ts)

    return summary


# ---------------------------------------------------------------------------
# CLI: --probe force
# ---------------------------------------------------------------------------

def force_probe(engine: str) -> int:
    """
    Force probe of named engine; on success flip leadership (even if LOCKED).
    Returns exit code 0 always (CLI success); prints status to stdout.
    """
    engine = engine.lower().strip()
    mapping = {"cc": "CC", "codex": "CODEX", "grok": "GROK"}
    if engine not in mapping:
        print(f"Unknown engine: {engine}. Use cc|codex|grok.", file=sys.stderr)
        return 1

    target = mapping[engine]
    state = ensure_state()
    state, expected_epoch = read_state()
    if expected_epoch < 0:
        state, expected_epoch = read_state()

    old_leader = state.get("leader", "CC")
    probes = {"CC": probe_cc, "CODEX": probe_codex, "GROK": probe_grok}
    ok = probes[target]()
    if not ok:
        print(f"probe {engine}: FAILED")
        return 0

    if old_leader == target:
        print(f"probe {engine}: OK (already leader={target})")
        return 0

    new_state = dict(state)
    new_state["leader"] = target
    new_state["reason"] = "probe_restored" if target == "CC" else "manual"
    # Spec: --probe success sets leader to that engine. Use probe_restored for restore feel,
    # but reason for non-CC force can be manual. Prefer probe_restored when recovering hierarchy;
    # for explicit CLI always use probe_restored when it is a restore-style flip, manual otherwise.
    # Spec says: "if it succeeds, flip leadership to that engine" and "Sends a notification only
    # if a flip occurred". Use probe_restored for consistency with restore messages when new=CC,
    # and for non-CC use the template with 接棒恢復 only for probe_restored. Spec:
    # "probing cc and succeeding sets leader=CC" — reason not strictly forced for --probe.
    # We'll use probe_restored for all successful --probe flips so notify message is correct.
    new_state["reason"] = "probe_restored"
    new_state["since"] = now_iso()

    if write_state(new_state, expected_epoch):
        on_leader_change(old_leader, new_state.get("leader", old_leader))
        msg = build_flip_message(old_leader, target, "probe_restored")
        notify_all(msg)
        print(f"probe {engine}: OK -> leader={target}")
        print(msg)
    else:
        print(f"probe {engine}: OK but write abandoned (epoch mismatch)")
    return 0


def force_take(engine: str) -> int:
    """
    Charles 手動翻牌（不探測、直接翻）＋自動 LOCKED，蓋過自動裁決。
    lead CLI 專用；解鎖回自動用 --unlock。
    """
    engine = engine.lower().strip()
    mapping = {"cc": "CC", "codex": "CODEX", "grok": "GROK"}
    if engine not in mapping:
        print(f"Unknown engine: {engine}. Use cc|codex|grok.", file=sys.stderr)
        return 1

    target = mapping[engine]
    ensure_state()
    state, expected_epoch = read_state()
    old_leader = state.get("leader", "CC")

    new_state = dict(state)
    new_state["leader"] = target
    new_state["mode"] = "LOCKED"
    new_state["reason"] = "manual"
    new_state["since"] = now_iso()

    if write_state(new_state, expected_epoch):
        if old_leader != target:
            notify_all(build_flip_message(old_leader, target, "manual"))
            on_leader_change(old_leader, target)
        print(f"take: leader={target} mode=LOCKED（自動裁決已鎖，解鎖用 --unlock）")
    else:
        print("take: write abandoned (epoch mismatch)，請重試")
    return 0


def force_unlock() -> int:
    """解除 LOCKED 回 AUTO，裁判恢復自動裁決。"""
    ensure_state()
    state, expected_epoch = read_state()
    if state.get("mode") == "AUTO":
        print("已是 AUTO，不用解鎖")
        return 0
    new_state = dict(state)
    new_state["mode"] = "AUTO"
    if write_state(new_state, expected_epoch):
        print(f"unlock: mode=AUTO（leader={new_state.get('leader')}，下輪起自動裁決）")
    else:
        print("unlock: write abandoned (epoch mismatch)，請重試")
    return 0


def cmd_status() -> int:
    state = ensure_state()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_dry_run() -> int:
    summary = evaluate_cycle(dry_run=True)
    print("=== lead_referee dry-run ===")
    print(f"action: {summary.get('action')}")
    print(f"old_leader: {summary.get('old_leader')}")
    print(f"new_leader: {summary.get('new_leader')}")
    print(f"reason: {summary.get('reason')}")
    for m in summary.get("messages") or []:
        print(f"  - {m}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description="Leadership regency referee")
    parser.add_argument("--status", action="store_true", help="Print leadership.json")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without side effects")
    parser.add_argument(
        "--probe",
        choices=["cc", "codex", "grok"],
        help="Force probe an engine and flip if successful",
    )
    parser.add_argument(
        "--take",
        choices=["cc", "codex", "grok"],
        help="Manually flip leadership to engine and LOCK (Charles override)",
    )
    parser.add_argument("--unlock", action="store_true", help="Clear LOCKED, resume AUTO")
    args = parser.parse_args(argv)

    # --status is read-only (except ensure/rebuild); still take lock to be safe? Spec says
    # lockfile for runs; status does write on rebuild. Acquire lock.
    if not acquire_lock():
        # Silent exit 0 when lock held
        return 0

    try:
        if args.status:
            return cmd_status()
        if args.dry_run:
            return cmd_dry_run()
        if args.probe:
            return force_probe(args.probe)
        if args.take:
            return force_take(args.take)
        if args.unlock:
            return force_unlock()
        # Normal cycle
        evaluate_cycle(dry_run=False)
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())

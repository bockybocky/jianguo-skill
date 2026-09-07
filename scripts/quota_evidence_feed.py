#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota_evidence_feed.py — 用 CodexBar 的額度讀數餵監國裁判「撞牆證據」

為什麼要有這支（2026-09-06）：
  監國裁判（lead_referee.py）只吃 quota_evidence.jsonl，而會寫這個檔的只有走 llm_call.py 的 3 支腳本；
  17 支排程直接呼叫 claude -p。結果上線兩個月，證據檔裡只有一筆 7/12 演習，自動翻牌從沒真的發生過。
  這支把 dispatch_router.py 已經在讀的 CodexBar 快取翻成證據：Claude／Codex 的 5 小時窗用滿就寫一筆。

判準：5 小時窗 used_percent ≥ FULL_PCT（預設 99）→ 該引擎撞牆。
去重：同一個 resets_at 只寫一次（狀態存 ~/.cache/dispatch_router/feed_state.json）。
簽名格式配合裁判的 parse_reset_estimate：含「resets at <ISO 本地時間>」，裁判就能算出何時探測歸政。

用法：
  python quota_evidence_feed.py            # 讀快取（過期就重抓），該寫就寫，印一行
  python quota_evidence_feed.py --dry-run  # 只印判定不寫
  python quota_evidence_feed.py --selftest

原則：儀表壞掉什麼都不寫、不報錯（fail-quiet 但印一行讓人看見）；不寫入等於「沒證據」，裁判照舊。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_router as dr  # noqa: E402

EVIDENCE = Path(os.environ.get("QUOTA_EVIDENCE_PATH", "C:/Users/Charles/.agents/shared/quota_evidence.jsonl"))
FEED_STATE = Path(os.environ.get("QUOTA_FEED_STATE", str(dr.CACHE_FILE.parent / "feed_state.json")))
FULL_PCT = float(os.environ.get("QUOTA_FEED_FULL_PCT", "99"))
ENGINES = {"claude": "claude", "codex": "codex"}  # provider → 裁判用的 engine 名


def _session_window(raw: dict) -> tuple[float | None, str | None]:
    """回 (used_percent, resets_at) 取 5 小時窗（primary）。"""
    u = raw.get("usage") or {}
    w = u.get("primary") or {}
    return w.get("used_percent"), w.get("resets_at")


def _to_local_iso(resets_at: str | None) -> str:
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="minutes")
    except ValueError:
        return ""


def decide(snap: dict) -> list[dict]:
    """從快照算出該寫的證據列（尚未去重）。"""
    out = []
    for prov, engine in ENGINES.items():
        raw = snap.get("providers", {}).get(prov, {})
        if "usage" not in raw:
            continue
        used, resets = _session_window(raw)
        if used is None or float(used) < FULL_PCT:
            continue
        local = _to_local_iso(resets)
        sig = f"[codexbar] {engine} 5h window {float(used):.0f}% used. resets at {local or 'unknown'}"
        out.append({"ts": datetime.now().astimezone().isoformat(), "engine": engine,
                    "signature": sig, "source": "codexbar", "resets_at": resets or ""})
    return out


def _load_state() -> dict:
    try:
        return json.loads(FEED_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(dry_run: bool = False, snap: dict | None = None) -> list[dict]:
    """回傳實際寫入（或 dry-run 會寫入）的列。"""
    snap = snap or dr.snapshot()
    rows = decide(snap)
    state = _load_state()
    written = []
    for r in rows:
        key = f"{r['engine']}@{r['resets_at']}"
        if state.get(r["engine"]) == key:
            continue  # 同一個重置窗已經寫過
        written.append(r)
        state[r["engine"]] = key
    if not dry_run and written:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        with EVIDENCE.open("a", encoding="utf-8") as f:
            for r in written:
                f.write(json.dumps({k: v for k, v in r.items() if k != "resets_at"}, ensure_ascii=False) + "\n")
        FEED_STATE.parent.mkdir(parents=True, exist_ok=True)
        FEED_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return written


def _selftest() -> int:
    import tempfile
    global EVIDENCE, FEED_STATE
    fails = 0
    def check(name, cond):
        nonlocal fails
        print(f"{'PASS' if cond else 'FAIL'}  {name}"); fails += 0 if cond else 1
    with tempfile.TemporaryDirectory() as td:
        EVIDENCE = Path(td) / "ev.jsonl"; FEED_STATE = Path(td) / "st.json"
        now = datetime.now(timezone.utc)
        def snap(claude_used, codex_used):
            iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"providers": {
                "claude": {"usage": {"primary": {"used_percent": claude_used, "resets_at": iso}}},
                "codex": {"usage": {"primary": {"used_percent": codex_used, "resets_at": iso}}},
                "grok": {"error": "x"}}}
        w = run(snap=snap(10, 20))
        check("沒撞牆不寫", w == [] and not EVIDENCE.exists())
        w = run(snap=snap(100, 20))
        check("Claude 5h 100% → 寫一筆 claude", len(w) == 1 and w[0]["engine"] == "claude")
        check("簽名含 resets at 讓裁判能解析", "resets at" in w[0]["signature"] and "unknown" not in w[0]["signature"])
        w = run(snap=snap(100, 20))
        check("同一重置窗第二次不重寫", w == [])
        w = run(snap=snap(100, 99))
        check("Codex 也滿 → 只多寫 codex", len(w) == 1 and w[0]["engine"] == "codex")
        lines = EVIDENCE.read_text(encoding="utf-8").strip().splitlines()
        check("證據檔共 2 行且是合法 JSON", len(lines) == 2 and all(json.loads(l)["source"] == "codexbar" for l in lines))
        # 裁判能不能從這簽名算出重置時間
        try:
            import lead_referee as lr
            est = lr.parse_reset_estimate(w[0]["signature"])
            dt = datetime.fromisoformat(est)
            check("裁判 parse_reset_estimate 解得出時間（誤差 < 2 分）", abs((dt - now).total_seconds()) < 120)
        except Exception as e:  # noqa: BLE001
            check(f"裁判解析（例外 {e}）", False)
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    dry = "--dry-run" in argv
    written = run(dry_run=dry)
    if written:
        print(("[dry-run] 會寫入：" if dry else "已寫入證據：") + "；".join(r["signature"] for r in written))
    else:
        print("quota_feed：無撞牆（或已寫過）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

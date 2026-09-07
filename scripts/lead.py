#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lead — 監國協議（ADR-0026）Charles 專用指令。

用法：
    python lead.py                    # = status
    python lead.py status             # 誰當家、多久、翻牌史、pending 佇列
    python lead.py take codex         # 手動翻給 codex 並鎖定（cc|codex|grok）
    python lead.py auto               # 解鎖回自動裁決
    python lead.py probe cc           # 立即探測（成功即翻牌；花一次最小額度）
    python lead.py handoff            # 產接棒包 v2（隔離區＋守則＋額度行＋handoff）
    python lead.py reclaim            # 歸政一鍵：探測→翻回 CC→結束看守期→印交還報告與待決清單

所有狀態寫入都轉發 lead_referee.py（單一寫者鐵律），本檔只讀。
"""
from __future__ import annotations
import io
import os
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRIPTS = Path(r"C:/Users/Charles/scripts")
REFEREE = SCRIPTS / "lead_referee.py"
STATE = Path(r"C:/Users/Charles/.agents/shared/leadership.json")
EVIDENCE = Path(r"C:/Users/Charles/.agents/shared/quota_evidence.jsonl")
PENDING = Path(r"C:/Users/Charles/.agents/shared/pending_for_cc.md")
HANDOFF_ACTIVE = Path(r"C:/Users/Charles/claude/session-handoff-active.md")
BRIEFING = Path(r"C:/Users/Charles/.agents/shared/takeover_briefing.md")
AGENTS_MD = Path(r"C:/Users/Charles/AGENTS.md")

CARETAKER_RULES = """【看守內閣模式】你是臨時代理（CC 額度撞牆期間）。
禁止：修改 charter/CLAUDE.md/settings/記憶檔/skill、對外發布（blog/方格子/email/社群）、治理級決策。
遇到這類需求 → 一行寫入 C:/Users/Charles/.agents/shared/pending_for_cc.md 排隊等 CC 復權。
你可以：跑管線、寫程式、做研究、回報。公司說明書見 C:/Users/Charles/AGENTS.md。"""


def referee(*args: str) -> int:
    return subprocess.call([sys.executable, str(REFEREE), *args])


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"leader": "CC", "mode": "AUTO", "reason": "state_missing"}


def cmd_status() -> int:
    st = read_state()
    lead_icon = {"CC": "👑", "CODEX": "🏛️", "GROK": "🏛️", "NONE": "🚨"}.get(st.get("leader"), "?")
    print(f"{lead_icon} 當家：{st.get('leader')}（mode={st.get('mode')}，自 {st.get('since','?')[:16]}，原因 {st.get('reason')}）")
    if st.get("cc_reset_estimate"):
        print(f"   CC 額度預估重置：{st['cc_reset_estimate'][:16]}")
    if st.get("codex_reset_estimate"):
        print(f"   Codex 額度預估重置：{st['codex_reset_estimate'][:16]}")
    # 最近證據
    try:
        lines = EVIDENCE.read_text(encoding="utf-8").strip().splitlines()[-3:]
        if lines:
            print("── 最近撞牆證據 ──")
            for ln in lines:
                e = json.loads(ln)
                print(f"   {e['ts'][:16]} {e['engine']}: {e['signature'][:60]}")
    except Exception:
        pass
    # pending 佇列
    try:
        n = len([l for l in PENDING.read_text(encoding="utf-8").splitlines() if l.strip().startswith("-")])
        if n:
            print(f"── 待 CC 復權事項：{n} 筆（{PENDING}）──")
    except Exception:
        pass
    return 0


REGENCY_RULES = Path(r"C:/Users/Charles/.agents/shared/regency_rules.md")
REGENCY_CURRENT = Path(r"C:/Users/Charles/.agents/shared/regency/current")
REGENCY_ARCHIVE = Path(r"C:/Users/Charles/.agents/shared/regency/_archive")


def _regency_path() -> str:
    try:
        return REGENCY_CURRENT.read_text(encoding="utf-8").strip()
    except OSError:
        return "（尚未開看守期——先跑 lead take codex 或由裁判自動翻牌）"


def _dispatch_brief() -> str:
    try:
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("dispatch_router.py")), "brief"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           env={**os.environ, "PYTHONUTF8": "1"})
        return r.stdout.strip() or "（額度行讀不到）"
    except Exception as e:  # noqa: BLE001
        return f"（額度行讀不到：{e}）"


def cmd_handoff() -> int:
    """接棒簡報 v2（2026-09-06）：隔離區路徑＋看守守則全文＋額度行＋handoff＋排隊事項。"""
    st = read_state()
    rules = REGENCY_RULES.read_text(encoding="utf-8") if REGENCY_RULES.exists() else CARETAKER_RULES
    parts = [
        f"# 接棒簡報（takeover briefing）— {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## ★ 你的隔離區（產物只准寫這裡的 work/，LEDGER.md 在同一層）",
        f"`{_regency_path()}`",
        "",
        "## 這一刻的額度與派工建議",
        _dispatch_brief(),
        "",
        rules,
        "",
        f"## 主導權現況\n- leader={st.get('leader')} mode={st.get('mode')} 原因={st.get('reason')}",
        "",
        "## Session Handoff（CC 最後狀態）",
        HANDOFF_ACTIVE.read_text(encoding="utf-8") if HANDOFF_ACTIVE.exists() else "（無）",
    ]
    try:
        pend = PENDING.read_text(encoding="utf-8").strip()
        if pend:
            parts += ["", "## 已排隊等 CC 的事項", pend]
    except Exception:
        pass
    BRIEFING.write_text("\n".join(parts), encoding="utf-8")
    print(f"✅ 接棒包已產：{BRIEFING}")
    print("")
    print("下一步（開 Codex 接棒）：")
    print("  1. 開新終端機，cd 到工作目錄")
    print(r"  2. 跑：D:\codex-bin\codex.cmd")
    print(f"  3. 第一句貼：請先完整讀 {BRIEFING} 再接手工作，遵守其中看守守則；產物只寫隔離區 work/，每件事記進 LEDGER.md。")
    print("")
    print("（Codex 也掛了就把步驟 2 換成 grok，同一份簡報。）")
    return 0


def cmd_reclaim() -> int:
    """歸政一鍵（2026-09-06）：探測 CC → 翻回 CC（裁判掛鉤自動結束看守期：解鎖、比對、報告）→ 解 LOCKED → 印報告與待決清單。"""
    st = read_state()
    if st.get("leader") == "CC":
        print("已是 CC 當家。若看守期資料夾還開著，直接跑：python ~/scripts/regency.py end")
        return 0
    print("① 探測 CC 是否復活（花一次最小呼叫）…")
    referee("--probe", "cc")
    st2 = read_state()
    if st2.get("leader") != "CC":
        print(f"✗ CC 尚未復活（leader 仍={st2.get('leader')}），額度還沒回來。稍後再跑 lead reclaim。")
        return 1
    print("② 解除 LOCKED 回自動裁決…")
    referee("--unlock")
    print("③ 交還報告：")
    reports = sorted(REGENCY_ARCHIVE.glob("*/RECLAIM_REPORT.md")) if REGENCY_ARCHIVE.exists() else []
    if reports:
        rp = reports[-1]
        print(f"   {rp}")
        head = rp.read_text(encoding="utf-8").split("## LEDGER", 1)[0]
        print("   " + "\n   ".join(head.strip().splitlines()[:40]))
    else:
        print("   （沒有 RECLAIM_REPORT——看守期可能沒開過，或 regency.py 不在）")
    print("④ 排隊等你決定的事：")
    subprocess.run([sys.executable, str(Path(__file__).with_name("memory_steward") / "steward.py"), "pending-list"],
                   env={**os.environ, "PYTHONUTF8": "1"})
    print("\n✅ 歸政完成。逐條看報告的『修改／新增／刪除』清單決定收或退（退＝用 .bak 或 git 還原該檔）。")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "reclaim":
        return cmd_reclaim()
    if cmd == "status":
        return cmd_status()
    if cmd == "take" and len(args) >= 2:
        return referee("--take", args[1])
    if cmd == "auto":
        return referee("--unlock")
    if cmd == "probe" and len(args) >= 2:
        return referee("--probe", args[1])
    if cmd == "handoff":
        return cmd_handoff()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())

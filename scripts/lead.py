#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lead — 監國協議（ADR-0026）Charles 專用指令。

用法：
    python lead.py                    # = status
    python lead.py status             # 誰當家、多久、翻牌史、pending 佇列
    python lead.py take codex         # 手動翻給 codex 並鎖定（cc|codex|grok）
    python lead.py auto               # 解鎖回自動裁決
    python lead.py probe cc           # 立即探測（成功即翻牌；花一次最小額度）
    python lead.py handoff            # 產接棒包＋印 codex 接棒指令

所有狀態寫入都轉發 lead_referee.py（單一寫者鐵律），本檔只讀。
"""
from __future__ import annotations
import io
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


def cmd_handoff() -> int:
    st = read_state()
    parts = [
        f"# 接棒簡報（takeover briefing）— {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        CARETAKER_RULES,
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
    print(f"  3. 第一句貼：請先完整讀 {BRIEFING} 再接手工作，遵守其中看守內閣約束。")
    print("")
    print("（Codex 也掛了就把步驟 2 換成 grok，同一份簡報。）")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
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

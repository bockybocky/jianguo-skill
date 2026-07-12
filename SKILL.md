---
name: jianguo
description: 監國協議（ADR-0026）操作入口——CC 額度撞牆時公司主導權自動接力 CC→Codex→Grok 的查狀態、接棒、蓋牌、歸政、演習、故障排除一站式 SOP。當 Charles 說「監國」「lead」「誰當家」「主導權」「額度撞牆」「CC 掛了」「usage limit 到了」「切換 codex」「接棒」「復權」「歸政」，或任何終端機出現 Claude usage/rate limit 錯誤、管線疑似因額度停擺、想演習接棒流程、想查為什麼派工跑去 codex/grok 時，都用本 skill。不確定現在誰當家時第一反應就是進來跑 lead status。
---

# 監國協議（LEADERSHIP RELAY）操作手冊

一句話：CC 五小時額度撞牆 → 裁判自動翻牌給 Codex 看守內閣（可派 Grok）→ Codex 也竭 → Grok → 額度回來探測確認才歸政 CC。全程語音＋Discord＋broadcast 通知。

設計 SoT（原理、情境矩陣、被拒方案）：`D:/ai-memory/Projects/leadership_relay/JIANGUO_MASTER_v1.md`。本 skill 只管「怎麼操作」。

## 第一反應：先看誰當家

```bash
python C:/Users/Charles/scripts/lead.py status   # 終端機直接打 lead status 也行
```

輸出讀法：👑 CC 當家=正常；🏛️ CODEX/GROK=監國中；🚨 NONE=三家全掛。
mode=LOCKED 表示 Charles 手動鎖定，裁判不會自動翻。

## 五大情境 SOP

### 1. 終端機報 usage limit（你在場，互動中）

```bash
lead handoff        # 產接棒包（handoff Active＋看守約束＋pending 佇列）
```

照它印出的三步開 Codex 續作。Codex 也掛就把指令換 grok，同一份接棒包。
**不要**乾等額度回來——監國就是為了不停擺。

### 2. 管線半夜撞牆（無人在場）

不用做任何事——調度層呼叫內自動降級完成當次任務，裁判 5 分鐘內翻牌＋三路通知。
早上開工 SessionStart hook 會端出翻牌史＋`pending_for_cc.md` 排隊事項，逐筆處理即可。

### 3. 手動蓋牌（測試、或你不信任自動判定）

```bash
lead take codex     # 強制翻給 codex 並 LOCKED（語音會播切換通知）
lead auto           # 解鎖回自動裁決
```

### 4. 歸政（額度應該回來了）

```bash
lead probe cc       # 花一次最小 haiku 呼叫實測；成功即翻回＋語音「CC 復權」
```

自動歸政也會發生（裁判每輪檢查 reset 估計到期就探測），probe 只是你想立刻確認時用。

### 5. 三家全掛（leader=NONE）

deterministic 管線照跑，LLM 步驟 fail-loud 排隊（保 raw 不編造）。裁判持續輪流探測，
誰先復活誰監國（CC 優先）。此時人工介入選項：等、或開 gemini 互動頂急件（gemini 不在監國序列，屬工具引擎）。

## 元件地圖（改東西前先認路）

| 元件 | 路徑 | 職責 |
|---|---|---|
| 狀態 SoT | `~/.agents/shared/leadership.json` | **唯一寫者=裁判**，其他一律唯讀 |
| 裁判 | `~/scripts/lead_referee.py`（schtasks `LeadReferee` 每 5 分） | 翻牌/探測/通知 |
| 調度層 | `~/scripts/llm_call.py` v2 | 鏈序跟 leader、看守前綴、證據回報 |
| 你的指令 | `~/scripts/lead.py`（`lead` 全域可打） | status/take/auto/probe/handoff |
| 證據 | `~/.agents/shared/quota_evidence.jsonl` | llm_call append、裁判消費 |
| 排隊 | `~/.agents/shared/pending_for_cc.md` | 看守期間制度性動作凍結處 |
| hook | session_start.py＋fetch-handoff.py | 開場狀態＋每次輸入警告 |

鐵律：**任何人（含 Iris、agent、腳本）不得直寫 leadership.json**——要翻牌走 `lead take` 或裁判 API。
看守內閣紅線：代理期間禁改 charter/CLAUDE.md/settings/記憶/skill、禁對外發布、禁治理決策——需求進 pending 佇列。

## 演習（每季或改完元件後跑）

```bash
# 1. 注入模擬撞牆證據（source=manual，簽名帶 [DRILL]）
python -c "import json,datetime; open(r'C:/Users/Charles/.agents/shared/quota_evidence.jsonl','a',encoding='utf-8').write(json.dumps({'ts':datetime.datetime.now().astimezone().isoformat(),'engine':'claude','signature':'[DRILL] usage limit reached','source':'manual'},ensure_ascii=False)+'\n')"
python ~/scripts/lead_referee.py && lead status          # 2. 應翻 CODEX＋語音響
LLM_CALL_SKIP=claude python ~/scripts/llm_call.py --prompt "reply ok"   # 3. 應走 codex＋看守前綴
lead probe cc                                             # 4. 歸政＋語音「CC 復權」
```

演習判準：四步全過＋語音真的有響（沒響見故障排除第 1 條）。

## 故障排除

1. **翻牌了但沒聽到語音**：Voicebox 後端 sidecar 會無聲死（GUI 活著、`curl -m 3 127.0.0.1:17493/health` 不通）。
   修法：`taskkill //F //IM voicebox.exe` 後重開 `D:/apps/Voicebox/voicebox.exe`。Discord/broadcast 不受影響。
2. **CC 復權後派工還跑 codex**：查 `lead status` 的 reset 估計欄——歷史 bug（復權殘留冷卻）已雙保險修掉（裁判清估計＋leader 冷卻豁免）；若重現，跑一次 `lead take cc && lead auto` 強制重置狀態。
3. **狀態檔損壞/被誤刪**：所有讀者 fail-open 當 CC；裁判下一輪自動重建。不用手救。
4. **翻牌太頻繁/誤判**：`lead take cc`（LOCKED 鎖定）止血，再查 `quota_evidence.jsonl` 尾巴看誰在報假證據。
5. **測試**：`python ~/scripts/test_lead_referee.py`（28）＋`python ~/scripts/test_llm_call.py`（9）。改任一元件後必跑。

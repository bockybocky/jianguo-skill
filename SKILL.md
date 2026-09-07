---
name: jianguo
description: 監國協議（ADR-0026）操作入口——CC 額度撞牆時公司主導權自動接力 CC→Codex→Grok 的查狀態、接棒、蓋牌、歸政、演習、故障排除一站式 SOP。當 Charles 說「監國」「lead」「誰當家」「主導權」「額度撞牆」「CC 掛了」「usage limit 到了」「切換 codex」「接棒」「復權」「歸政」，或任何終端機出現 Claude usage/rate limit 錯誤、管線疑似因額度停擺、想演習接棒流程、想查為什麼派工跑去 codex/grok 時，都用本 skill。不確定現在誰當家時第一反應就是進來跑 lead status。
---

# 監國協議（LEADERSHIP RELAY）操作手冊 v2

一句話：CC 額度撞牆 → 裁判自動翻牌給 Codex 看守（可派 Grok）→ **Codex 只在隔離區做事、禁區作業系統層上鎖** → 額度回來 `lead reclaim` 一鍵歸政，機器列出「它改了什麼」給 CC 逐條收。

設計 SoT：`D:/ai-memory/Projects/leadership_relay/JIANGUO_MASTER_v1.md`（v1 原理）＋同檔 §7「v2 看守隔離區」（2026-09-06）。本 skill 只管「怎麼操作」。

## v2 改了什麼、為什麼（2026-09-06 Charles 拍板「先做幾小時、接著做但寫在隔離區」）

| 8 月接手時的痛 | 紀錄裡的真因 | v2 的修法 |
|---|---|---|
| 自動翻牌沒發生，全靠手動 `lead take` | 證據檔兩個月只有一筆演習：只有走 `llm_call.py` 的 3 支腳本會回報，17 支排程直接呼叫 `claude -p`，裁判是瞎的 | 裁判每輪先跑 `quota_evidence_feed.py`，把 CodexBar 讀到的「5 小時窗用滿」翻成證據；重置時間一起餵，歸政探測就會準時 |
| Codex 難用 | 接棒包只有 2 KB：一份 handoff 加一句約束，沒有守則、沒有額度、沒有工作區 | 接棒包 v2：隔離區路徑＋守則全文（`regency_rules.md`）＋這一刻額度行＋handoff＋排隊事項 |
| 歸政很麻煩 | 兩顆腦寫同一本帳：DEC 撞號三次、automl 重工；回來要找哪裡被改 | 隔離區 `regency/<日期>/work/`＋禁區唯讀（DECISIONS／CLAUDE.md／settings／rules／skills／memory／handoff）＋快照比對 → `RECLAIM_REPORT.md` 三類清單（新增／修改／刪除） |

**沒做（Charles 選「先幾小時」）**：整套搬到 Codex 的永久遷移，另案規劃；v2 的隔離區與快照是那件事的地基。

## 第一反應：先看誰當家、額度剩多少

```bash
lead status                                              # 👑 CC 當家=正常；🏛️ CODEX/GROK=看守中；🚨 NONE=三家全掛
PYTHONUTF8=1 python ~/scripts/dispatch_router.py brief   # 四家額度一行＋這一刻該派誰（Claude 🔴＝黃旗）
python ~/scripts/regency.py status                       # 看守期開著沒、隔離區在哪、鎖了幾個禁區
```

**額度那一行怎麼讀（2026-09-06 Charles 拍板：分配工作不只看剩幾成，還要看多久後重置）**

每格長這樣：`Codex週 🟡剩25%·17h後重置🟢餘裕`。三個欄位各管一件事：

| 欄位 | 意思 | 派工時怎麼用 |
|---|---|---|
| 燈（🟢🟡🔴⚪） | 剩幾成：≥40 綠／15～40 黃／<15 紅／讀不到灰 | 紅的不派（X 爬文除外）、灰的照預設派但要吼 |
| `·Nh後重置` | 這個窗還有幾小時重置（不到 90 分鐘印分鐘） | 紅燈但很快重置＝非急件可以等，不必硬轉別家；沒印＝讀不到重置時間 |
| `🟢餘裕`／`🔥超前` | 剩餘量比「按時間比例還該留著的量」多 15 個百分點＝餘裕；少 15 個百分點＝超前（撐不到重置） | 同燈號的兩家（Codex 對 Grok）比餘裕不比裸的剩幾成；黃燈但有餘裕視同綠燈可分擔 |

- **agy 那一格看的是「我們真正用的模型」的池**（2026-09-06 查證）：Antigravity 一個模型一個池，Pro 方案是 5 小時窗上面罩一個 7 天週池，週池用光就鎖到週期結束。CodexBar 的 primary 是 Gemini Pro 池，而派工線用的 Flash 沒有自己的池。儀表現在先找 Flash 池，找不到就在快取刷新時發一次最小探針，格式 `agy Flash🟢實測可用(Pro池剩11%·98h後重置)`：前半是探針結果（🔴＝探針失敗），括號是 Pro 池附註，只給參考。探針失敗先看 `table` 的 note 欄錯誤原因，再手動 `agy --model gemini-3.8-flash-high --print="只回一個字：好"` 對照。
- **窗長未知**：Grok 的窗長是假設值。若重置時間落在假設窗之外，儀表會把窗標清空、不標超前也不標餘裕，`table` 的 note 欄寫「窗長未知」——寧可少講，不要把假設當事實。
- 固定規則額度不改：X 爬文一定 Grok；agy 與 Sonnet 永不寫程式。
- 要看四家逐欄（距重置、餘裕、時間進度該用多少）：`PYTHONUTF8=1 python ~/scripts/dispatch_router.py`（印表）；改規則＝改 `route()` 並加 `--selftest` 案例（現 35 條）。

## 五大情境 SOP

### 1. 終端機報 usage limit（你在場）
```bash
lead take codex      # 翻給 Codex 並 LOCKED；掛鉤自動：開看守期（隔離區＋禁區上鎖）＋產接棒包 v2
```
照印出的三步開 Codex，第一句貼「請先完整讀 takeover_briefing.md…」。Codex 也掛就把指令換 grok，同一份簡報。
**不要**乾等額度回來。

### 2. 管線半夜撞牆（無人在場）
不用做事。裁判每 5 分鐘：CodexBar 讀到 Claude 5 小時窗用滿 → 寫證據 → 翻牌 CODEX → 開看守期 → Discord＋broadcast 通知。
早上開工 SessionStart hook 端出翻牌史，然後跑 `lead reclaim`。

### 2.5 翻牌那一刻，靠 Claude 的排程線會**靜默降級**，不會報錯（2026-09-07 實證，Charles 拍板記入）

翻牌只管「誰當家」，**管不到正在跑的排程**。當日兩次撞 Claude 5 小時小窗（10:24、19:53），
兩次都剛好撞上排程線發文的英文配圖步驟：翻譯呼叫 `claude -p` 回空字串 → 每張圖判「缺翻譯，跳過」
→ 文章照發、方格子照發、Substack 照寄 → **英文版零張圖上線兩篇**（serenity 週報、macromusings），
沒有任何警報，是 Charles 自己看到才知道。第二次備援已經接上 codex，卻撞到 Windows 裸 `codex` 找不到 `.cmd` 的洞，一樣回空。

**翻牌／歸政之後第一件事，多做一項**：列出翻牌時段內跑過的、步驟裡有呼叫 Claude 的排程，逐一驗**產物**不驗 log：
- 排程線發文（10:30／19:30／週一 10:00）：`cd C:/Users/Charles/Projects/realpha-blog && for f in $(ls -t src/content/blog/*.en.mdx | head -4); do s=$(basename $f .en.mdx); echo "$s zh圖=$(grep -c '/figures/' src/content/blog/$s.zh-TW.mdx) en圖=$(grep -c '/figures/' $f)"; done`
  英文少於中文＝補跑 `python ~/scripts/blog_auto/figures/en_figures.py auto <slug>`，再 build、push、Substack 用 `publish_draft(send=False)` 重發（不寄第二封信）。
- 其他 `claude -p` 依賴的排程（`grep -l "claude" ~/scripts/*.bat` 自己列），同樣看產物。
**BlogStuckWatch 不管這個**：它只看「跑超過 45 分鐘」，不看「跑完但少東西」。
**根本規矩**：靠 Claude 的機械步驟一律要有跨家備援（codex）＋**產物數量閘**（做完數一數，少了就通知）；只有備援沒有閘＝備援壞了一樣沒人知道（09-07 第二次就是這樣）。

### 3. 歸政（額度回來了）— **一個指令**
```bash
lead reclaim
```
它做四件事：① 探測 CC（花一次最小呼叫）② 翻回 CC 並解 LOCKED ③ 結束看守期：解鎖禁區、比對快照、印 `RECLAIM_REPORT.md` ④ 印排隊等你決定的事。
然後你只做一件事：看報告的「修改／新增／刪除」三張清單，**逐條收或退**（退＝用該檔 `.bak` 或 `git -C D:/ai-memory checkout` 還原）。
CC 還沒復活會印「尚未復活，稍後再跑」，不會亂翻。

### 4. 手動蓋牌／解鎖
```bash
lead take cc        # 強制翻回 CC（掛鉤自動結束看守期）
lead auto           # 解 LOCKED 回自動裁決
```

### 5. 三家全掛（leader=NONE）
確定性管線照跑，LLM 步驟 fail-loud 排隊。裁判輪流探測，誰先活誰當家（CC 優先）。手機上仍有異廠 bot 通道（見備援地圖）。

## 看守期間，Codex 能做什麼、不能做什麼（守則全文在 `~/.agents/shared/regency_rules.md`）

- **能**：接著做 handoff 的「回來先做」；改 `~/scripts`、`~/.claude/hooks` 的程式；產物寫隔離區 `work/`；用 `steward.py pending-add` 排隊要 CC 決定的事。
- **不能（作業系統層唯讀，寫入會 PermissionError，那是設計）**：`DECISIONS.md`／`CLAUDE.md`／`settings.json`／`.claude/rules`／`.claude/skills`／`.agents/memory`／`session-handoff-active.md`。
- **每做一件事記一列 `LEDGER.md`**。CC 回來只看 LEDGER 與 RECLAIM_REPORT。
- 不發 DEC 編號、不寫記憶、不改 skill、不對外發布、不動排程、不走另計費 API。

## 元件地圖（改東西前先認路）

| 元件 | 路徑 | 職責 |
|---|---|---|
| 狀態 SoT | `~/.agents/shared/leadership.json` | **唯一寫者=裁判** |
| 裁判 | `~/scripts/lead_referee.py`（schtasks `LeadReferee` 每 5 分） | 翻牌／探測／通知；**v2 掛鉤**：每輪先餵證據、翻牌時開／關看守期（`LEAD_V2_HOOKS=0` 可關） |
| 撞牆偵測 | `~/scripts/quota_evidence_feed.py` | CodexBar 5 小時窗 ≥99% → 寫 `quota_evidence.jsonl`（同重置窗只寫一次） |
| 看守隔離區 | `~/scripts/regency.py` | `start`：建 `regency/<日期>/`＋快照＋禁區唯讀；`end`：解鎖＋比對＋`RECLAIM_REPORT.md`＋歸檔 `_archive/` |
| 你的指令 | `~/scripts/lead.py` | status／take／auto／probe／handoff（v2 簡報）／**reclaim（一鍵歸政）** |
| 守則 | `~/.agents/shared/regency_rules.md` | 接棒包內嵌全文 |
| 額度儀表 | `~/scripts/dispatch_router.py`＋hook `quota_brief_hook.py` | 撞牆前哨；只建議不翻牌。每格＝燈號＋剩幾成＋距重置時間＋餘裕／超前（2026-09-06 起）；同燈號比餘裕決勝 |
| 調度層 | `~/scripts/llm_call.py` v2 | 鏈序跟 leader（只 3 支腳本在用，覆蓋率是已知洞） |
| 證據 | `~/.agents/shared/quota_evidence.jsonl` | llm_call＋feed 寫、裁判讀 |
| 排隊 | `~/.agents/shared/pending_for_cc.md`＋steward pending queue | 看守期間要 CC 決定的事 |

鐵律：**任何人不得直寫 leadership.json**。看守期間禁區靠唯讀屬性擋，不靠自覺。

## 演習（改完任一元件必跑）

```bash
LEAD_V2_HOOKS=0 python ~/scripts/test_lead_referee.py      # 28 項
python ~/scripts/test_llm_call.py                          # 9 項
PYTHONUTF8=1 python ~/scripts/quota_evidence_feed.py --selftest
PYTHONUTF8=1 python ~/scripts/regency.py --selftest
PYTHONUTF8=1 python ~/scripts/test_jianguo_v2_drill.py     # 全鏈路：假證據→翻牌→看守期開→禁區寫入被擋→改一檔→reclaim→報告列出那一檔
```
判準：全過＋演習報告真的列出被改的檔＋Discord／broadcast 通知有到。

## 故障排除

1. **`lead reclaim` 說 CC 尚未復活**：額度真的沒回來，看 `dispatch_router.py brief` 的 Claude 5h 格；或 CodexBar 讀的不是同一個帳號（`codexbar-cli usage -p claude`）。
2. **看守期沒自動開**：`lead status` 是 CODEX 但 `regency.py status` 說沒開 → 掛鉤失敗，看 referee 那輪的 messages；手動 `python ~/scripts/regency.py start --leader CODEX && lead handoff`。
3. **禁區解不開**：`regency.py end` 只解它鎖上的；本來就唯讀的檔記在 `meta.json` 的 `already_readonly`。
4. **兩個儀表打架**（監國說 CODEX、額度行說 Claude 🟢）：先信 `quota_evidence.jsonl` 原始證據，再查帳號。
5. **狀態檔損壞**：所有讀者 fail-open 當 CC，裁判下一輪重建。
6. **CC 當家卻有看守期在跑（記憶檔／skills 寫入被拒 PermissionError）**：`regency.py status` 說進行中、`lead status` 說 CC、`quota_evidence.jsonl` 沒有新撞牆、LEDGER 空白＝孤兒看守期。
   跑 `python ~/scripts/regency.py end` 解鎖（`lead reclaim` 看到 CC 當家不會替你關）。
   2026-09-07 06:30 實證根因：自動掃測試排程 `Realpha_AutomlTestScan` 跑 `test_lead_referee.py`，測試模擬翻牌，v2 掛鉤對真系統開了看守期、鎖 3411 檔到 10:06。
   已修三層：裁判掛鉤只在狀態檔是正式那份時才動（`_PROD_STATE_PATH`）；`regency.py start` 在 CC 當家時拒開（回傳碼 3，演習加 `--force`）；掃測試排程與測試檔都設 `LEAD_V2_HOOKS=0`。
   重現法：`LEAD_V2_HOOKS=1 python test_lead_referee.py` 跑完 `regency/current` 必須不存在。

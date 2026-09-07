# 監國協議 (Jianguo Protocol) — Leadership Relay for Multi-CLI AI Agent Fleets

**When your primary coding agent hits its usage limit, the company shouldn't stop.**

Jianguo (監國, "regency" — the crown prince governs while the king is away, and returns power when the king is back) is a Claude Code skill + reference implementation for **automatic leadership failover between AI CLI agents**:

```
        quota hit (hard evidence)      codex quota hit too
  Claude Code ──────────────→ Codex ──────────────→ Grok
  ↑   caretaker mode: pipelines keep running,          │
  │   governance/publishing actions frozen & queued    │
  └────── probe confirms quota restored ←──────────────┘
              (voice announces the restoration)
```

## Design highlights

- **State machine with a single writer.** `leadership.json` is mutated only by the referee script (atomic write + epoch guard), so any number of concurrent terminals/crons read a consistent answer to "who is in charge".
- **Hard evidence only.** No burn-rate prediction. A flip happens only when a real `429 / usage limit` error is captured by the dispatch layer. Restoration requires a successful minimal probe call — an estimate alone never flips leadership back (estimates lie; probes don't).
- **Caretaker cabinet, not full regency.** The acting agent runs pipelines, writes code, does research — but institutional actions (config/memory/publishing/governance) are frozen into a `pending_for_cc.md` queue for the primary agent to review after restoration.
- **Leader-aware dispatch.** One wrapper (`llm_call.py`) reorders its fallback chain per the current leader and reports quota evidence back to the referee. Existing callers gain failover with zero changes.
- **Multi-terminal aware.** Session-start and per-prompt hooks surface "regency in progress" warnings in every terminal, because parallel terminals share one quota pool and die together.
- **Drill-tested.** The included drill (inject fake evidence → flip → caretaker dispatch → probe → restore) caught a real bug on day one: a stale cooldown estimate after restoration made the dispatcher skip the restored leader. Fixed with a double guard (referee clears estimates on restore; dispatcher never cooldown-skips the current leader).

## v2 (2026-09): caretaker sandbox + quota evidence feed

Two months of v1 in production showed the automatic flip almost never fired, and handing work back was painful. Root causes and fixes:

| v1 pain | Real cause (from the logs) | v2 fix |
|---|---|---|
| Flip never triggered; every takeover was manual `lead take` | Only scripts routed through `llm_call.py` reported quota evidence; most scheduled jobs called `claude -p` directly, so the referee was blind | `quota_evidence_feed.py`: each referee tick reads the local quota dashboard (CodexBar) and converts "5-hour window exhausted" into hard evidence, including the reset time, so the restore probe fires on time |
| The caretaker agent was hard to work with | The handoff bundle was 2 KB: one handoff note and one constraint line | Handoff bundle v2: sandbox path + full caretaker rules (`regency_rules.md`) + the current quota line + handoff + queued items |
| Restoring was messy | Two agents wrote the same ledgers (decision IDs collided three times); the primary had to hunt for what changed | `regency.py`: a per-regency **sandbox** (`regency/<date>/work/`), OS-level read-only locks on protected zones (decision ledger / config / rules / skills / memory / handoff), a snapshot before and a diff after → `RECLAIM_REPORT.md` listing added / modified / deleted files. `lead reclaim` does probe → flip back → end regency → print the report in one command |

Also new: the referee refuses to open a regency while the primary is in charge unless `--force` (a scheduled test run once triggered a real lockdown), and hooks only fire on the production state file.

## Contents

| File | Role |
|---|---|
| `SKILL.md` | The Claude Code skill: operator runbook v2 (status / takeover / manual override / reclaim / drill / troubleshooting), in Traditional Chinese |
| `scripts/lead_referee.py` | The referee — sole writer of the leadership state machine; v2 adds the evidence feed tick and the regency hooks |
| `scripts/regency.py` | v2 caretaker sandbox: `start / status / end`, protected-zone locks, snapshot + reclaim report |
| `scripts/quota_evidence_feed.py` | v2 quota dashboard → evidence adapter (with reset-time carry-over) |
| `scripts/llm_call.py` | Leader-aware LLM dispatch wrapper with caretaker prefix injection |
| `scripts/lead.py` | Human CLI: `lead status / take / auto / probe / handoff / reclaim` |
| `regency_rules.md` | The caretaker rules handed to the acting agent (what it may do, where it may write, what is frozen) |
| `scripts/test_*.py` | Test suites (stdlib-only, fully mocked) |

## Adapting to your environment

This is a **reference implementation** extracted from a real single-operator, multi-agent Windows setup. Paths (`C:\Users\...`), notification channels (a local TTS server + Discord webhook), and the probe commands are environment-specific — grep for the constants block at the top of each script and swap in your own. The state machine, evidence protocol, and caretaker pattern port anywhere.

## License

MIT

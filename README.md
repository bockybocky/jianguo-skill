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

## Contents

| File | Role |
|---|---|
| `SKILL.md` | The Claude Code skill: operator runbook (status / takeover / manual override / restore / drill / troubleshooting), in Traditional Chinese |
| `scripts/lead_referee.py` | The referee — sole writer of the leadership state machine (28 tests) |
| `scripts/llm_call.py` | Leader-aware LLM dispatch wrapper with caretaker prefix injection (9 tests) |
| `scripts/lead.py` | Human CLI: `lead status / take / auto / probe / handoff` |
| `scripts/test_*.py` | Test suites (stdlib-only, fully mocked) |

## Adapting to your environment

This is a **reference implementation** extracted from a real single-operator, multi-agent Windows setup. Paths (`C:\Users\...`), notification channels (a local TTS server + Discord webhook), and the probe commands are environment-specific — grep for the constants block at the top of each script and swap in your own. The state machine, evidence protocol, and caretaker pattern port anywhere.

## License

MIT

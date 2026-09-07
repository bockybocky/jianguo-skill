#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Leadership-aware LLM fallback router for cron LLM steps (agent redundancy P0-2).

Base fallback chain (leadership may move one lane first, each timeout 120s):
  1. claude -p --model sonnet
  2. codex exec
  3. ~/scripts/gemini_call.sh
  4. ~/scripts/grok_call.sh

CLI:
  python llm_call.py --prompt-file X [--task-type summarize|classify] [--max-chars N]
  python llm_call.py --prompt "..."   # inline (short prompts)

Library:
  import llm_call
  result = llm_call.run(prompt)   # -> dict

Stdout JSON on success:
  {"engine":"claude|codex|gemini|grok","ok":true,"text":"...","leader":"CC"}
  optional "skipped" / "skip_reasons" when environment or cooldown skips apply

Stdout JSON when all lanes fail:
  {"ok":false,"engine":null,"text":""}
  exit code 3

**Caller contract (fail-loud / data-never-lost):**
  If exit code is 3 (or result["ok"] is False), the caller MUST keep raw input
  and MUST NOT invent a summary. Prefer writing the unsummarized artifact and
  alerting. This convention is also documented in
  ~/scripts/redundancy/cron_criticality.md.

Test hook:
  LLM_CALL_SKIP=claude,gemini   # comma-separated engines to force-skip

Leadership files:
  ~/.agents/shared/leadership.json (read fresh per call, fail-open)
  ~/.agents/shared/quota_evidence.jsonl (append-only quota evidence)
  ~/.agents/shared/pending_for_cc.md (append-only caretaker queue)

Logging:
  Appends one line per call to ~/scripts/redundancy/llm_call.log
  (timestamp / engine / elapsed_ms / prompt_len). Never logs prompt content.

stdlib only (subprocess / json / argparse / pathlib / os / time / sys / re /
tempfile / datetime).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# --- paths -----------------------------------------------------------------
HOME = Path.home()
SCRIPTS_DIR = HOME / "scripts"
LOG_PATH = SCRIPTS_DIR / "redundancy" / "llm_call.log"
GEMINI_SH = SCRIPTS_DIR / "gemini_call.sh"
GROK_SH = SCRIPTS_DIR / "grok_call.sh"
SHARED_DIR = HOME / ".agents" / "shared"
LEADERSHIP_PATH = SHARED_DIR / "leadership.json"
QUOTA_EVIDENCE_PATH = SHARED_DIR / "quota_evidence.jsonl"
PENDING_FOR_CC_PATH = SHARED_DIR / "pending_for_cc.md"

TIMEOUT_S = 120
# 2026-08-09：長提示的每車道預算要按提示長度放大。
# 病理：lunjian 抽卡的提示是 7.5K–20K 字，四條車道都在 120s 被砍 → ok=false
# 「全鏈死」，2026-08-04..08-08 連 6 天沒摘要。實測（重放 08-08 那份 11,536 字
# 的提示）claude 車道要 263.8s 才回完整 JSON array——不是金鑰過期也不是模型下架，
# 四條車道當下用短提示都秒回 PONG，純粹是預算不夠。
# 短提示維持 120s（別讓死掉的車道拖久才 failover）。
TIMEOUT_MAX_S = 600
TIMEOUT_CHARS_PER_S = 40
EXIT_ALL_DEAD = 3
BASE_ORDER = ["claude", "codex", "gemini", "grok"]
VALID_LEADERS = {"CC", "CODEX", "GROK", "NONE"}
LEADER_ENGINES = {"CC": "claude", "CODEX": "codex", "GROK": "grok"}
QUOTA_SIGNATURE_RE = re.compile(
    r"usage limit|rate limit|429|quota|limit reached|out of credits|insufficient|resets at",
    re.IGNORECASE,
)
CARETAKER_PREFIX = (
    "【看守內閣模式】你是臨時代理。禁止：修改 charter/CLAUDE.md/settings/記憶檔/skill、"
    "對外發布、治理級決策；遇到這類需求輸出一行『PENDING_FOR_CC: <摘要>』而不是執行。"
    "你可以：執行管線任務、寫程式、研究、回報。\n\n"
)

# Windows: prefer Git Bash (not WSL) for *.sh wrappers
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
)


def _claude_bin() -> str:
    if sys.platform == "win32":
        cand = HOME / "AppData" / "Roaming" / "npm" / "claude.cmd"
        if cand.exists():
            return str(cand)
        return "claude.cmd"
    return "claude"


def _codex_bin() -> str:
    if sys.platform == "win32":
        cand = Path("D:/codex-bin/codex.cmd")
        if cand.exists():
            return str(cand)
        return "codex.cmd"
    return "codex"


def _bash_bin() -> str:
    if sys.platform == "win32":
        for p in _GIT_BASH_CANDIDATES:
            if Path(p).exists():
                return p
        return "bash"
    return "bash"


def _lane_timeout(prompt: str) -> int:
    """Per-lane budget in seconds, scaled by prompt size (see TIMEOUT_MAX_S note)."""
    scaled = len(prompt) // TIMEOUT_CHARS_PER_S + 60
    return max(TIMEOUT_S, min(TIMEOUT_MAX_S, scaled))


def _skip_set() -> set[str]:
    raw = os.environ.get("LLM_CALL_SKIP", "") or ""
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _read_leadership_state() -> dict[str, Any]:
    """Read leadership state afresh, failing open to CC on any error."""
    fallback: dict[str, Any] = {"leader": "CC", "mode": "AUTO"}
    try:
        state = json.loads(LEADERSHIP_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return fallback
        if not isinstance(state.get("leader"), str):
            return fallback
        if not isinstance(state.get("mode"), str):
            return fallback
        if state["leader"] not in VALID_LEADERS:
            return fallback
        return state
    except Exception:
        return fallback


def _is_cooling(state: dict[str, Any], engine: str) -> bool:
    """Return whether claude/codex has a reset estimate still in the future."""
    field = {
        "claude": "cc_reset_estimate",
        "codex": "codex_reset_estimate",
    }.get(engine)
    if field is None:
        return False
    try:
        estimate = state.get(field)
        if not isinstance(estimate, str):
            return False
        reset_at = datetime.fromisoformat(estimate)
        return reset_at > datetime.now(reset_at.tzinfo)
    except Exception:
        return False


def _append_log(
    engine: str | None,
    ok: bool,
    elapsed_ms: int,
    prompt_len: int,
    skipped: list[str],
) -> None:
    """Append one audit line. Never write prompt/text content."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        skip_s = ",".join(skipped) if skipped else "-"
        line = (
            f"{ts}\tengine={engine or 'none'}\tok={int(ok)}\t"
            f"elapsed_ms={elapsed_ms}\tprompt_len={prompt_len}\tskipped={skip_s}\n"
        )
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        # Logging must never break the caller path.
        pass


def _run_captured(cmd: list[str], *, input_text: str | None, timeout: int) -> tuple[bool, str]:
    """Run subprocess with shell=False. Returns (ok, stdout_or_err)."""
    try:
        r = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return True, out
        err = (r.stderr or r.stdout or "")[:400]
        return False, f"rc={r.returncode}: {err}"
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout}s"
    except FileNotFoundError as e:
        return False, f"not found: {e}"
    except OSError as e:
        return False, f"os error: {e}"


def _try_claude(prompt: str) -> tuple[bool, str]:
    timeout_s = _lane_timeout(prompt)
    env = os.environ.copy()
    env["CLAUDE_HOOK_BYPASS"] = "1"
    cmd = [
        _claude_bin(),
        "-p",
        "--model",
        "sonnet",
        "--system-prompt",
        "You are a focused tool. Output only the requested answer, no preamble.",
        "--settings",
        "{}",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]
    try:
        r = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
            env=env,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return True, out
        err = (r.stderr or r.stdout or "")[:400]
        return False, f"rc={r.returncode}: {err}"
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout_s}s"
    except FileNotFoundError as e:
        return False, f"not found: {e}"
    except OSError as e:
        return False, f"os error: {e}"


def _try_codex(prompt: str) -> tuple[bool, str]:
    timeout_s = _lane_timeout(prompt)
    fd, out_path = tempfile.mkstemp(prefix="llm_call_codex_", suffix=".txt")
    os.close(fd)
    try:
        cmd = [_codex_bin(), "exec", "--sandbox", "read-only",
               "--skip-git-repo-check", "-o", out_path, "-"]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout_s,
                            shell=False, cwd=str(SCRIPTS_DIR))
        if r.returncode == 0:
            try:
                out = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                out = ""
            if out:
                return True, out
            return False, "empty output"
        err = (r.stderr or r.stdout or "")[:400]
        return False, "rc=" + str(r.returncode) + ": " + err
    except subprocess.TimeoutExpired:
        return False, "timeout " + str(timeout_s) + "s"
    except FileNotFoundError as e:
        return False, "not found: " + str(e)
    except OSError as e:
        return False, "os error: " + str(e)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _append_pending_for_cc(text: str) -> None:
    """Append caretaker escalation lines without affecting router success."""
    try:
        pending_lines = [
            line for line in text.splitlines() if line.startswith("PENDING_FOR_CC:")
        ]
        if not pending_lines:
            return
        PENDING_FOR_CC_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_FOR_CC_PATH.open("a", encoding="utf-8") as f:
            for line in pending_lines:
                f.write("[" + datetime.now().isoformat() + "] " + line + "\n")
    except Exception:
        pass


def _append_quota_evidence(engine: str, signature: str) -> None:
    """Append one quota signal without disrupting fallback routing."""
    try:
        QUOTA_EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        evidence = {
            "ts": datetime.now().isoformat(),
            "engine": engine,
            "signature": signature[:300],
            "source": "llm_call",
        }
        with QUOTA_EVIDENCE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _start_lead_referee() -> None:
    """Ask the referee to reassess leadership without blocking this call."""
    try:
        subprocess.Popen(
            [sys.executable, "C:/Users/Charles/scripts/lead_referee.py"],
            cwd=str(SCRIPTS_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass


def _try_sh_wrapper(script: Path, prompt: str) -> tuple[bool, str]:
    """Call gemini_call.sh / grok_call.sh via Git Bash (prompt as argv)."""
    if not script.exists():
        return False, f"missing {script}"
    # Posix path for bash on Windows
    sh_path = str(script).replace("\\", "/")
    timeout_s = _lane_timeout(prompt)
    cmd = [_bash_bin(), sh_path, prompt, str(timeout_s)]
    return _run_captured(cmd, input_text=None, timeout=timeout_s + 15)


def run(
    prompt: str,
    task_type: str | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Run the leadership-aware fallback chain; engine failures never raise.

    task_type is accepted for future routing hints (summarize|classify); currently
    does not change engine order.
    """
    del task_type
    if prompt is None:
        prompt = ""
    if not isinstance(prompt, str):
        prompt = str(prompt)
    if max_chars is not None and max_chars > 0 and len(prompt) > max_chars:
        prompt = prompt[:max_chars]

    prompt_len = len(prompt)
    state = _read_leadership_state()
    leader = state["leader"]
    leader_engine = LEADER_ENGINES.get(leader)
    if leader_engine is None:
        dispatch_order = list(BASE_ORDER)
    else:
        dispatch_order = [leader_engine] + [
            engine for engine in BASE_ORDER if engine != leader_engine
        ]

    routed_prompt = prompt if leader == "CC" else CARETAKER_PREFIX + prompt
    env_skips = _skip_set()
    skipped: list[str] = []
    skip_reasons: dict[str, str] = {}
    t0 = time.time()

    lanes: dict[str, Any] = {
        "claude": _try_claude,
        "codex": _try_codex,
        "gemini": lambda p: _try_sh_wrapper(GEMINI_SH, p),
        "grok": lambda p: _try_sh_wrapper(GROK_SH, p),
    }

    last_err = ""
    for name in dispatch_order:
        if name in env_skips:
            skipped.append(name)
            skip_reasons[name] = "env"
            continue
        # leader 引擎永不因冷卻跳過（復權後殘留估計不得凌駕 leader；演習 7/12 抓到的 bug）
        if name in {"claude", "codex"} and name != leader_engine and _is_cooling(state, name):
            skipped.append(name)
            skip_reasons[name] = "cooling"
            continue

        ok, text = lanes[name](routed_prompt)
        if ok:
            _append_pending_for_cc(text)
            elapsed_ms = int((time.time() - t0) * 1000)
            result: dict[str, Any] = {
                "engine": name,
                "ok": True,
                "text": text,
                "leader": leader,
            }
            if skipped:
                result["skipped"] = skipped
                result["skip_reasons"] = skip_reasons
            _append_log(name, True, elapsed_ms, prompt_len, skipped)
            return result

        if QUOTA_SIGNATURE_RE.search(text):
            _append_quota_evidence(name, text)
            _start_lead_referee()
        last_err = text

    elapsed_ms = int((time.time() - t0) * 1000)
    result = {
        "ok": False,
        "engine": None,
        "text": "",
        "error": last_err or "all engines failed",
        "leader": leader,
    }
    if skipped:
        result["skipped"] = skipped
        result["skip_reasons"] = skip_reasons
    _append_log(None, False, elapsed_ms, prompt_len, skipped)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unified LLM fallback router (claude → gemini → grok)"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt-file", help="Read prompt from file (UTF-8)")
    src.add_argument("--prompt", help="Inline prompt string")
    parser.add_argument(
        "--task-type",
        choices=("summarize", "classify"),
        default=None,
        help="Optional hint (does not change chain order yet)",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="Truncate prompt to N characters",
    )
    args = parser.parse_args(argv)

    if args.prompt_file:
        path = Path(args.prompt_file)
        if not path.is_file():
            print(
                json.dumps(
                    {"ok": False, "engine": None, "text": "", "error": f"missing file: {path}"},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return EXIT_ALL_DEAD
        prompt = path.read_text(encoding="utf-8", errors="replace")
    else:
        prompt = args.prompt or ""

    # Ensure UTF-8 stdout on Windows
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    result = run(prompt, task_type=args.task_type, max_chars=args.max_chars)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("ok"):
        return 0
    return EXIT_ALL_DEAD


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())

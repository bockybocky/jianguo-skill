from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import llm_call


class LlmCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.leadership_path = self.root / "shared" / "leadership.json"
        self.quota_path = self.root / "shared" / "quota_evidence.jsonl"
        self.pending_path = self.root / "shared" / "pending_for_cc.md"
        self.log_path = self.root / "logs" / "llm_call.log"
        self.path_patcher = patch.multiple(
            llm_call,
            LEADERSHIP_PATH=self.leadership_path,
            QUOTA_EVIDENCE_PATH=self.quota_path,
            PENDING_FOR_CC_PATH=self.pending_path,
            LOG_PATH=self.log_path,
        )
        self.path_patcher.start()
        self.env_patcher = patch.dict(os.environ, {"LLM_CALL_SKIP": ""}, clear=False)
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.path_patcher.stop()
        self.temp_dir.cleanup()

    def write_state(self, **state: object) -> None:
        self.leadership_path.parent.mkdir(parents=True, exist_ok=True)
        self.leadership_path.write_text(
            json.dumps(state), encoding="utf-8"
        )

    def test_codex_leader_attempts_codex_first(self) -> None:
        self.write_state(leader="CODEX", mode="AUTO")
        order: list[str] = []

        codex = Mock(side_effect=lambda prompt: (order.append("codex") or (True, "ok")))
        claude = Mock(side_effect=lambda prompt: (order.append("claude") or (False, "dead")))
        wrappers = Mock(
            side_effect=lambda script, prompt: (
                order.append("gemini" if script == llm_call.GEMINI_SH else "grok")
                or (False, "dead")
            )
        )
        with patch.object(llm_call, "_try_codex", codex), patch.object(
            llm_call, "_try_claude", claude
        ), patch.object(llm_call, "_try_sh_wrapper", wrappers):
            result = llm_call.run("hello")

        self.assertEqual(["codex"], order)
        codex.assert_called_once()
        claude.assert_not_called()
        wrappers.assert_not_called()
        self.assertEqual("codex", result["engine"])
        self.assertEqual("CODEX", result["leader"])

    def test_leader_engine_exempt_from_cooling_skip(self) -> None:
        # 7/12 演習抓到的 bug：復權後殘留冷卻估計不得凌駕 leader
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.write_state(
            leader="CC",
            mode="AUTO",
            cc_reset_estimate=future,
        )
        claude = Mock(return_value=(True, "claude ok"))
        codex = Mock(return_value=(True, "should not run"))

        with patch.object(llm_call, "_try_claude", claude), patch.object(
            llm_call, "_try_codex", codex
        ):
            result = llm_call.run("hello")

        claude.assert_called_once_with("hello")
        codex.assert_not_called()
        self.assertEqual([], result.get("skipped", []))
        self.assertEqual("claude", result["engine"])

    def test_quota_failure_appends_evidence_and_starts_referee(self) -> None:
        self.write_state(leader="CC", mode="AUTO")
        claude = Mock(return_value=(False, "RATE LIMIT reached; retry later"))
        codex = Mock(return_value=(True, "fallback ok"))

        with patch.object(llm_call, "_try_claude", claude), patch.object(
            llm_call, "_try_codex", codex
        ), patch.object(llm_call.subprocess, "Popen") as popen:
            result = llm_call.run("hello")

        self.assertTrue(result["ok"])
        evidence = [
            json.loads(line)
            for line in self.quota_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len(evidence))
        self.assertEqual("claude", evidence[0]["engine"])
        self.assertIn("RATE LIMIT", evidence[0]["signature"])
        self.assertEqual("llm_call", evidence[0]["source"])
        popen.assert_called_once()
        self.assertEqual(
            [llm_call.sys.executable, "C:/Users/Charles/scripts/lead_referee.py"],
            popen.call_args.args[0],
        )

    def test_caretaker_prefix_only_when_leader_is_not_cc(self) -> None:
        self.write_state(leader="GROK", mode="AUTO")
        wrappers = Mock(return_value=(True, "grok ok"))
        with patch.object(llm_call, "_try_sh_wrapper", wrappers):
            result = llm_call.run("do work")

        self.assertEqual("grok", result["engine"])
        self.assertEqual(llm_call.GROK_SH, wrappers.call_args.args[0])
        self.assertEqual(
            llm_call.CARETAKER_PREFIX + "do work",
            wrappers.call_args.args[1],
        )

        self.write_state(leader="CC", mode="AUTO")
        claude = Mock(return_value=(True, "claude ok"))
        with patch.object(llm_call, "_try_claude", claude):
            result = llm_call.run("do work")

        self.assertEqual("claude", result["engine"])
        claude.assert_called_once_with("do work")
        self.assertFalse(claude.call_args.args[0].startswith(llm_call.CARETAKER_PREFIX))

    def test_pending_for_cc_line_is_appended_without_changing_text(self) -> None:
        self.write_state(leader="CC", mode="AUTO")
        original = "answer\nPENDING_FOR_CC: review charter\ntrailer"
        with patch.object(llm_call, "_try_claude", return_value=(True, original)):
            result = llm_call.run("hello")

        self.assertEqual(original, result["text"])
        pending = self.pending_path.read_text(encoding="utf-8")
        self.assertIn("] PENDING_FOR_CC: review charter\n", pending)
        self.assertNotIn("answer", pending)
        self.assertNotIn("trailer", pending)

    def test_bad_leadership_states_fail_open_and_keep_base_order(self) -> None:
        cases = {
            "missing": None,
            "corrupt": "{not json",
            "wrong_types": json.dumps({"leader": "CODEX", "mode": 7}),
        }
        for label, contents in cases.items():
            with self.subTest(label=label):
                if self.leadership_path.exists():
                    self.leadership_path.unlink()
                if contents is not None:
                    self.leadership_path.parent.mkdir(parents=True, exist_ok=True)
                    self.leadership_path.write_text(contents, encoding="utf-8")

                order: list[str] = []
                claude = Mock(
                    side_effect=lambda prompt: (
                        order.append("claude") or (False, "dead")
                    )
                )
                codex = Mock(
                    side_effect=lambda prompt: (
                        order.append("codex") or (False, "dead")
                    )
                )

                def wrapper(script: Path, prompt: str) -> tuple[bool, str]:
                    order.append(
                        "gemini" if script == llm_call.GEMINI_SH else "grok"
                    )
                    return False, "dead"

                with patch.object(llm_call, "_try_claude", claude), patch.object(
                    llm_call, "_try_codex", codex
                ), patch.object(llm_call, "_try_sh_wrapper", side_effect=wrapper):
                    result = llm_call.run("hello")

                self.assertFalse(result["ok"])
                self.assertEqual("CC", result["leader"])
                self.assertEqual(llm_call.BASE_ORDER, order)

    def test_cli_prompt_and_prompt_file_keep_surface_and_exit_three(self) -> None:
        claude = Mock(return_value=(False, "dead"))
        codex = Mock(return_value=(False, "dead"))
        wrappers = Mock(return_value=(False, "dead"))

        with patch.object(llm_call, "_try_claude", claude), patch.object(
            llm_call, "_try_codex", codex
        ), patch.object(llm_call, "_try_sh_wrapper", wrappers):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = llm_call.main(
                    [
                        "--prompt",
                        "abcdefgh",
                        "--task-type",
                        "summarize",
                        "--max-chars",
                        "4",
                    ]
                )
            inline_result = json.loads(stdout.getvalue())

            self.assertEqual(3, exit_code)
            self.assertFalse(inline_result["ok"])
            claude.assert_called_once_with("abcd")
            codex.assert_called_once_with("abcd")
            self.assertEqual(2, wrappers.call_count)
            self.assertTrue(
                all(call.args[1] == "abcd" for call in wrappers.call_args_list)
            )

            claude.reset_mock()
            codex.reset_mock()
            wrappers.reset_mock()
            prompt_file = self.root / "prompt.txt"
            prompt_file.write_text("file prompt", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = llm_call.main(
                    [
                        "--prompt-file",
                        str(prompt_file),
                        "--task-type",
                        "classify",
                        "--max-chars",
                        "4",
                    ]
                )
            file_result = json.loads(stdout.getvalue())

        self.assertEqual(3, exit_code)
        self.assertFalse(file_result["ok"])
        claude.assert_called_once_with("file")
        codex.assert_called_once_with("file")
        self.assertEqual(2, wrappers.call_count)
        self.assertTrue(all(call.args[1] == "file" for call in wrappers.call_args_list))

    def test_env_and_cooling_skips_combine_with_leader_order(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.write_state(
            leader="CODEX",
            mode="AUTO",
            codex_reset_estimate=future,
        )
        codex = Mock(return_value=(True, "should not run"))
        claude = Mock(return_value=(True, "should not run"))

        def wrapper(script: Path, prompt: str) -> tuple[bool, str]:
            self.assertEqual(llm_call.GROK_SH, script)
            self.assertEqual(llm_call.CARETAKER_PREFIX + "hello", prompt)
            return True, "grok ok"

        with patch.dict(
            os.environ, {"LLM_CALL_SKIP": "claude,gemini"}, clear=False
        ), patch.object(llm_call, "_try_codex", codex), patch.object(
            llm_call, "_try_claude", claude
        ), patch.object(llm_call, "_try_sh_wrapper", side_effect=wrapper):
            result = llm_call.run("hello")

        # 新規則：codex 是 leader，冷卻豁免 → 直接被呼叫成功，不會落到 grok
        codex.assert_called_once()
        claude.assert_not_called()
        self.assertEqual("codex", result["engine"])
        # codex 為首且成功 → 其後引擎不會被評估，skipped 不產生
        self.assertEqual([], result.get("skipped", []))

    def test_try_codex_uses_verified_cli_contract(self) -> None:
        captured_output_path: list[Path] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            out_path = Path(cmd[cmd.index("-o") + 1])
            captured_output_path.append(out_path)
            out_path.write_text("  codex output\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(llm_call, "_codex_bin", return_value="codex-test"), patch.object(
            llm_call.subprocess, "run", side_effect=fake_run
        ) as run_mock:
            ok, text = llm_call._try_codex("prompt")

        self.assertTrue(ok)
        self.assertEqual("codex output", text)
        cmd = run_mock.call_args.args[0]
        self.assertEqual(
            [
                "codex-test",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-o",
                str(captured_output_path[0]),
                "-",
            ],
            cmd,
        )
        self.assertEqual("prompt", run_mock.call_args.kwargs["input"])
        self.assertEqual(str(llm_call.SCRIPTS_DIR), run_mock.call_args.kwargs["cwd"])
        self.assertFalse(captured_output_path[0].exists())


if __name__ == "__main__":
    unittest.main()

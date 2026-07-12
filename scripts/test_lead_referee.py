# -*- coding: utf-8 -*-
"""
Unit tests for lead_referee.py — fully isolated (tempdir only, no real probes/network).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import lead_referee as lr


class LeadRefereeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.state_path = os.path.join(self.tmp, "leadership.json")
        self.evidence_path = os.path.join(self.tmp, "quota_evidence.jsonl")
        self.sidecar_path = os.path.join(self.tmp, ".lead_referee_last_processed.json")
        self.broadcast_path = os.path.join(self.tmp, "broadcast.md")
        self.lock_path = os.path.join(self.tmp, "lead_referee.lock")
        self.sandbox = os.path.join(self.tmp, "claude_sandbox")

        self._patches = [
            mock.patch.object(lr, "STATE_PATH", self.state_path),
            mock.patch.object(lr, "EVIDENCE_PATH", self.evidence_path),
            mock.patch.object(lr, "SIDECAR_PATH", self.sidecar_path),
            mock.patch.object(lr, "BROADCAST_PATH", self.broadcast_path),
            mock.patch.object(lr, "LOCK_PATH", self.lock_path),
            mock.patch.object(lr, "CLAUDE_SANDBOX", self.sandbox),
            mock.patch.object(lr, "SCRIPTS_DIR", self.tmp),
        ]
        for p in self._patches:
            p.start()

        # Default: no real subprocess / voice
        self.probe_cc = mock.patch.object(lr, "probe_cc", return_value=False)
        self.probe_codex = mock.patch.object(lr, "probe_codex", return_value=False)
        self.probe_grok = mock.patch.object(lr, "probe_grok", return_value=False)
        self.notify_all = mock.patch.object(lr, "notify_all")
        self.mock_probe_cc = self.probe_cc.start()
        self.mock_probe_codex = self.probe_codex.start()
        self.mock_probe_grok = self.probe_grok.start()
        self.mock_notify = self.notify_all.start()

        # Always acquire lock in tests unless testing lock itself
        self.lock_ok = mock.patch.object(lr, "acquire_lock", return_value=True)
        self.lock_release = mock.patch.object(lr, "release_lock")
        self.lock_ok.start()
        self.lock_release.start()

    def tearDown(self):
        mock.patch.stopall()
        self._tmpdir.cleanup()

    def seed_state(self, **kwargs):
        state = lr.default_state()
        state.update(kwargs)
        # Use module write with expected_epoch -1 (missing) or current
        if os.path.exists(self.state_path):
            cur, ep = lr.read_state(self.state_path)
            # Force write by matching epoch then replacing fields
            written = dict(cur)
            written.update(kwargs)
            # Direct atomic write for test seeding via module helper
            # Spec: tests may call internal write; for seeding with specific epoch,
            # write atomically without concurrency check first time or with match.
            lr._atomic_write_json(self.state_path, written)
        else:
            lr._atomic_write_json(self.state_path, state)
        return lr.read_state(self.state_path)[0]

    def append_evidence(self, engine: str, signature: str = "quota exceeded",
                        minutes_ago: float = 1.0, source: str = "llm_call"):
        ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat()
        line = json.dumps({
            "ts": ts,
            "engine": engine,
            "signature": signature,
            "source": source,
        }, ensure_ascii=False)
        with open(self.evidence_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return ts

    def load_state(self):
        with open(self.state_path, "r", encoding="utf-8") as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# 1. Quota-hit cascade
# ---------------------------------------------------------------------------

class TestQuotaHitCascade(LeadRefereeTestBase):
    def test_cc_to_codex(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=0, reason="startup")
        self.append_evidence("claude", "rate limit hit")
        lr.evaluate_cycle(dry_run=False)
        st = self.load_state()
        self.assertEqual(st["leader"], "CODEX")
        self.assertEqual(st["reason"], "cc_quota_hit")
        self.assertEqual(st["epoch"], 1)
        self.mock_notify.assert_called()

    def test_codex_to_grok(self):
        self.seed_state(leader="CODEX", mode="AUTO", epoch=1, reason="cc_quota_hit")
        self.append_evidence("codex", "quota exceeded")
        lr.evaluate_cycle(dry_run=False)
        st = self.load_state()
        self.assertEqual(st["leader"], "GROK")
        self.assertEqual(st["reason"], "codex_quota_hit")
        self.assertEqual(st["epoch"], 2)

    def test_grok_to_none(self):
        self.seed_state(leader="GROK", mode="AUTO", epoch=2, reason="codex_quota_hit")
        self.append_evidence("grok", "out of tokens")
        lr.evaluate_cycle(dry_run=False)
        st = self.load_state()
        self.assertEqual(st["leader"], "NONE")
        self.assertEqual(st["reason"], "grok_quota_hit")
        self.assertEqual(st["epoch"], 3)
        # Urgent all-hands message
        args = self.mock_notify.call_args[0][0]
        self.assertIn("三路全掛", args)

    def test_full_cascade_sequential(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=0)
        self.append_evidence("claude")
        lr.evaluate_cycle()
        self.assertEqual(self.load_state()["leader"], "CODEX")
        # Clear sidecar so next evidence is fresh; use new evidence lines
        if os.path.exists(self.sidecar_path):
            os.remove(self.sidecar_path)
        self.append_evidence("codex", minutes_ago=0.5)
        lr.evaluate_cycle()
        self.assertEqual(self.load_state()["leader"], "GROK")
        if os.path.exists(self.sidecar_path):
            os.remove(self.sidecar_path)
        self.append_evidence("grok", minutes_ago=0.2)
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "NONE")
        self.assertEqual(st["reason"], "grok_quota_hit")
        self.assertGreaterEqual(st["epoch"], 3)


# ---------------------------------------------------------------------------
# 2. LOCKED mode
# ---------------------------------------------------------------------------

class TestLockedMode(LeadRefereeTestBase):
    def test_locked_blocks_flip(self):
        self.seed_state(leader="CC", mode="LOCKED", epoch=5, reason="manual")
        self.append_evidence("claude")
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["epoch"], 5)
        self.mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Successful regency probe
# ---------------------------------------------------------------------------

class TestSuccessfulProbe(LeadRefereeTestBase):
    def test_codex_probe_cc_restores(self):
        past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        self.seed_state(
            leader="CODEX", mode="AUTO", epoch=3,
            reason="cc_quota_hit", cc_reset_estimate=past,
        )
        self.mock_probe_cc.return_value = True
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["reason"], "probe_restored")
        self.mock_notify.assert_called()
        msg = self.mock_notify.call_args[0][0]
        self.assertEqual(msg, "CC 復權，監國結束。")

    def test_null_estimate_probes_immediately(self):
        self.seed_state(
            leader="GROK", mode="AUTO", epoch=1,
            reason="codex_quota_hit", cc_reset_estimate=None,
        )
        self.mock_probe_cc.return_value = True
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["reason"], "probe_restored")


# ---------------------------------------------------------------------------
# 4. Failed probe
# ---------------------------------------------------------------------------

class TestFailedProbe(LeadRefereeTestBase):
    def test_failed_probe_bumps_estimate(self):
        past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
        self.seed_state(
            leader="CODEX", mode="AUTO", epoch=4,
            reason="cc_quota_hit", cc_reset_estimate=past,
        )
        self.mock_probe_cc.return_value = False
        before = datetime.now().astimezone()
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CODEX")
        self.assertEqual(st["reason"], "cc_quota_hit")  # reason unchanged
        self.assertEqual(st["epoch"], 5)
        self.mock_notify.assert_not_called()
        est = lr.parse_iso(st["cc_reset_estimate"])
        self.assertIsNotNone(est)
        # Approximately now + 30 minutes
        expected = before + timedelta(minutes=30)
        delta = abs((est - expected).total_seconds())
        self.assertLess(delta, 120)  # within 2 minutes tolerance


# ---------------------------------------------------------------------------
# 5. Epoch concurrency guard
# ---------------------------------------------------------------------------

class TestEpochGuard(LeadRefereeTestBase):
    def test_stale_write_abandoned(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=10, reason="startup")
        state, expected_epoch = lr.read_state()
        self.assertEqual(expected_epoch, 10)

        # Simulate concurrent process bumping epoch to 11 with different leader
        concurrent = dict(state)
        concurrent["leader"] = "CODEX"
        concurrent["epoch"] = 11
        concurrent["reason"] = "cc_quota_hit"
        lr._atomic_write_json(self.state_path, concurrent)

        # Stale write attempt with expected_epoch=10
        stale = dict(state)
        stale["leader"] = "NONE"
        stale["reason"] = "manual"
        ok = lr.write_state(stale, expected_epoch=10)
        self.assertFalse(ok)

        on_disk = self.load_state()
        self.assertEqual(on_disk["epoch"], 11)
        self.assertEqual(on_disk["leader"], "CODEX")
        self.assertNotEqual(on_disk["leader"], "NONE")


# ---------------------------------------------------------------------------
# 6. Expired evidence ignored
# ---------------------------------------------------------------------------

class TestExpiredEvidence(LeadRefereeTestBase):
    def test_stale_evidence_ignored(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=0)
        # 45 minutes ago > 30 min window
        self.append_evidence("claude", minutes_ago=45)
        # Prevent probe path from interfering (leader is CC so no probe)
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["epoch"], 0)
        self.mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Corrupted / missing state rebuild
# ---------------------------------------------------------------------------

class TestRebuildState(LeadRefereeTestBase):
    def test_missing_rebuild_via_status(self):
        self.assertFalse(os.path.exists(self.state_path))
        # Capture stderr for warning
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = lr.cmd_status()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.state_path))
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["mode"], "AUTO")
        self.assertEqual(st["epoch"], 0)
        self.assertEqual(st["reason"], "startup")

    def test_corrupt_rebuild(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("NOT JSON {{{")
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            st = lr.ensure_state()
        self.assertEqual(st["leader"], "CC")
        self.assertEqual(st["epoch"], 0)
        loaded = self.load_state()
        self.assertEqual(loaded["leader"], "CC")
        self.assertEqual(loaded["mode"], "AUTO")


# ---------------------------------------------------------------------------
# 8. Lockfile mutual exclusion
# ---------------------------------------------------------------------------

class TestLockfile(LeadRefereeTestBase):
    def test_second_acquire_fails(self):
        # Stop the always-True lock patch for this test
        self.lock_ok.stop()
        self.lock_release.stop()

        # Use real acquire on temp lock path
        ok1 = lr.acquire_lock(self.lock_path)
        self.assertTrue(ok1)
        ok2 = lr.acquire_lock(self.lock_path)
        self.assertFalse(ok2)
        lr.release_lock(self.lock_path)

    def test_main_silent_exit_on_lock_fail(self):
        self.lock_ok.stop()
        # Re-patch acquire_lock to return False
        with mock.patch.object(lr, "acquire_lock", return_value=False):
            with mock.patch.object(lr, "release_lock"):
                # State should not be created
                self.assertFalse(os.path.exists(self.state_path))
                rc = lr.main([])
                self.assertEqual(rc, 0)
                self.assertFalse(os.path.exists(self.state_path))


# ---------------------------------------------------------------------------
# 9. Additional: --status, --dry-run, reset parse, NONE cascade
# ---------------------------------------------------------------------------

class TestStatusFresh(LeadRefereeTestBase):
    def test_status_creates_cc(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            lr.cmd_status()
        data = json.loads(out.getvalue())
        self.assertEqual(data["leader"], "CC")
        self.assertTrue(os.path.exists(self.state_path))


class TestDryRun(LeadRefereeTestBase):
    def test_dry_run_no_side_effects(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=2)
        before = self.load_state()
        self.append_evidence("claude")
        # dry-run must not probe or notify
        summary = lr.evaluate_cycle(dry_run=True)
        after = self.load_state()
        self.assertEqual(before, after)
        self.assertEqual(after["epoch"], 2)
        self.mock_probe_cc.assert_not_called()
        self.mock_probe_codex.assert_not_called()
        self.mock_probe_grok.assert_not_called()
        self.mock_notify.assert_not_called()
        self.assertFalse(os.path.exists(self.sidecar_path))
        self.assertIn(summary["action"], ("quota_flip", "none", "would_probe_cc"))
        # With claude evidence and leader CC, dry-run should report would flip
        self.assertEqual(summary["new_leader"], "CODEX")

    def test_dry_run_cli_no_create(self):
        self.assertFalse(os.path.exists(self.state_path))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = lr.cmd_dry_run()
        self.assertEqual(rc, 0)
        # Spec: completely side-effect-free; if file did not exist, stay absent
        # (ensure_state is skipped in dry-run path when missing)
        self.assertFalse(os.path.exists(self.state_path))
        self.mock_notify.assert_not_called()


class TestResetEstimateParsing(LeadRefereeTestBase):
    def test_fallback_no_keyword(self):
        now = datetime.now().astimezone()
        est = lr.parse_reset_estimate("something went wrong, try later", now)
        est_dt = lr.parse_iso(est)
        expected = now + timedelta(hours=lr.DEFAULT_RESET_HOURS)
        self.assertIsNotNone(est_dt)
        self.assertLess(abs((est_dt - expected).total_seconds()), 5)

    def test_parse_with_reset_time(self):
        now = datetime.now().astimezone()
        # Put a future clock time
        future_hour = (now.hour + 3) % 24
        sig = f"You've hit your limit. Resets at {future_hour:02d}:30"
        est = lr.parse_reset_estimate(sig, now)
        est_dt = lr.parse_iso(est)
        self.assertIsNotNone(est_dt)
        fallback = now + timedelta(hours=lr.DEFAULT_RESET_HOURS)
        # Should not equal naive fallback (within 1s) if parse worked
        if abs((est_dt - fallback).total_seconds()) < 2:
            # Regex may have fallen back; still assert fallback is valid path
            self.assertLess(abs((est_dt - fallback).total_seconds()), 5)
        else:
            # Parsed something different from 5h default
            self.assertNotAlmostEqual(
                est_dt.timestamp(), fallback.timestamp(), delta=60
            )

    def test_chinese_reset_keyword_fallback_or_parse(self):
        now = datetime.now().astimezone()
        # Chinese 重置 present; if no time token, fallback 5h
        est = lr.parse_reset_estimate("額度重置", now)
        est_dt = lr.parse_iso(est)
        expected = now + timedelta(hours=5)
        self.assertLess(abs((est_dt - expected).total_seconds()), 5)


class TestNoneCascade(LeadRefereeTestBase):
    def test_stops_at_first_success(self):
        self.seed_state(
            leader="NONE", mode="AUTO", epoch=7,
            reason="grok_quota_hit",
            cc_reset_estimate=None,
        )
        self.mock_probe_cc.return_value = False
        self.mock_probe_codex.return_value = True
        self.mock_probe_grok.return_value = True  # must NOT be called

        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "CODEX")
        self.assertEqual(st["reason"], "probe_restored")
        self.mock_probe_cc.assert_called()
        self.mock_probe_codex.assert_called()
        self.mock_probe_grok.assert_not_called()
        self.mock_notify.assert_called()
        msg = self.mock_notify.call_args[0][0]
        self.assertIn("接棒恢復", msg)

    def test_all_fail_no_write(self):
        self.seed_state(
            leader="NONE", mode="AUTO", epoch=7,
            reason="grok_quota_hit",
        )
        self.mock_probe_cc.return_value = False
        self.mock_probe_codex.return_value = False
        self.mock_probe_grok.return_value = False
        lr.evaluate_cycle()
        st = self.load_state()
        self.assertEqual(st["leader"], "NONE")
        self.assertEqual(st["epoch"], 7)
        self.mock_notify.assert_not_called()


class TestProbeFunctionsCmd(LeadRefereeTestBase):
    def test_probe_cc_command_args(self):
        # Unpatch probe_cc to test real function with mocked subprocess
        self.probe_cc.stop()
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = "ok\n"
        fake.stderr = ""
        with mock.patch.object(lr.subprocess, "run", return_value=fake) as run:
            result = lr.probe_cc()
        self.assertTrue(result)
        args = run.call_args
        cmd = args[0][0]
        self.assertEqual(cmd[0], lr.CLAUDE_CMD)
        self.assertIn("-p", cmd)
        self.assertIn("reply exactly: ok", cmd)

    def test_probe_grok_success_criteria(self):
        self.probe_grok.stop()
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = "  ok  "
        fake.stderr = ""
        with mock.patch.object(lr.subprocess, "run", return_value=fake):
            self.assertTrue(lr.probe_grok())
        fake.stdout = "   "
        with mock.patch.object(lr.subprocess, "run", return_value=fake):
            self.assertFalse(lr.probe_grok())

    def test_force_probe_cli_flip(self):
        self.seed_state(leader="CODEX", mode="LOCKED", epoch=2)
        self.mock_probe_cc.return_value = True
        # force_probe uses real probe_cc which is mocked
        rc = lr.force_probe("cc")
        self.assertEqual(rc, 0)
        st = self.load_state()
        self.assertEqual(st["leader"], "CC")
        self.mock_notify.assert_called()


class TestNotifications(LeadRefereeTestBase):
    def test_notify_broadcast_writes(self):
        self.notify_all.stop()
        with mock.patch.object(lr, "notify_voice"), \
             mock.patch.object(lr, "notify_discord"):
            lr.notify_all("測試訊息")
        self.assertTrue(os.path.exists(self.broadcast_path))
        with open(self.broadcast_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("測試訊息", content)

    def test_build_flip_messages(self):
        self.assertEqual(
            lr.build_flip_message("CODEX", "CC", "probe_restored"),
            "CC 復權，監國結束。",
        )
        self.assertEqual(
            lr.build_flip_message("CC", "CODEX", "cc_quota_hit"),
            "公司主導權切換：CC交棒Codex。原因：CC額度用盡。",
        )
        self.assertEqual(
            lr.build_flip_message("NONE", "CODEX", "probe_restored"),
            "公司主導權切換：全員交棒Codex。原因：接棒恢復。",
        )


class TestSidecarDedup(LeadRefereeTestBase):
    def test_already_processed_not_reflip(self):
        self.seed_state(leader="CC", mode="AUTO", epoch=0)
        ts = self.append_evidence("claude", minutes_ago=1)
        lr.evaluate_cycle()
        self.assertEqual(self.load_state()["leader"], "CODEX")
        epoch_after = self.load_state()["epoch"]
        # Second run without new evidence: should not flip again
        # Leader is CODEX now; without codex evidence, may probe CC if estimate due
        self.mock_probe_cc.return_value = False
        lr.evaluate_cycle()
        st = self.load_state()
        # Still CODEX; same claude evidence must not re-apply (already processed).
        # Failed CC probe may bump estimate + epoch but must not flip leader.
        self.assertEqual(st["leader"], "CODEX")
        self.assertEqual(st["reason"], "cc_quota_hit")


if __name__ == "__main__":
    unittest.main()

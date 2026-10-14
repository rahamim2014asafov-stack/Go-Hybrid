"""Offline CLI regressions for checker corpus verification (stdlib only)."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import corpus_harness


HARNESS = Path(__file__).with_name("corpus_harness.py")
ACCEPT = {
    "summary": {"verdict": "accept", "errors": 0, "warnings": 0},
    "diagnostics": [],
}
REJECT = {
    "summary": {"verdict": "reject", "errors": 1, "warnings": 0},
    "diagnostics": [
        {"severity": "error", "code": "HYB-E3001", "tier0_rule": "A1c"}
    ],
}


class CorpusHarnessTests(unittest.TestCase):
    def run_harness(self, payload, exit_code=0, expected="ACCEPT", kill=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            header = f"// EXPECTED-VERDICT: {expected}\n" if expected else ""
            (corpus / "01-fixture.hy").write_text(
                header + "fn main() {}\n", encoding="utf-8"
            )
            checker = root / "hyb"
            source = (
                f"#!{sys.executable}\n"
                "import os, signal\n"
                f"print({json.dumps(payload)!r}, flush=True)\n"
            )
            if kill:
                source += "os.kill(os.getpid(), signal.SIGKILL)\n"
            else:
                source += f"raise SystemExit({exit_code})\n"
            checker.write_text(source, encoding="utf-8")
            checker.chmod(0o700)
            return subprocess.run(
                [sys.executable, str(HARNESS), "--bin", str(checker),
                 "--corpus", str(corpus)],
                capture_output=True, text=True, timeout=10,
            )

    def assert_harness_error(self, payload, exit_code=0, **kwargs):
        proc = self.run_harness(payload, exit_code, **kwargs)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("harness-error", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_clean_accept(self):
        proc = self.run_harness(ACCEPT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict agreement: 1/1", proc.stdout)

    def test_registered_reject_is_successful_verification(self):
        proc = self.run_harness(REJECT, 1, expected="REJECT A1c")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_managed_accept_preserves_warning(self):
        payload = copy.deepcopy(ACCEPT)
        payload["summary"]["warnings"] = 1
        payload["diagnostics"] = [
            {"severity": "warning", "code": "HYB-E8001", "tier0_rule": None}
        ]
        proc = self.run_harness(payload, expected="ACCEPT-MANAGED")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_reject_with_wrong_rule_remains_a_mismatch(self):
        proc = self.run_harness(REJECT, 1, expected="REJECT A1b")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("got reject A1c", proc.stdout)

    def test_accept_cannot_contain_an_error_without_tier0_rule(self):
        payload = copy.deepcopy(ACCEPT)
        payload["summary"]["errors"] = 1
        payload["diagnostics"] = [
            {"severity": "error", "code": "HYB-E0002", "tier0_rule": None}
        ]
        self.assert_harness_error(payload)

    def test_accept_with_nonzero_exit_fails(self):
        self.assert_harness_error(ACCEPT, 1)

    def test_reject_with_zero_exit_fails(self):
        self.assert_harness_error(REJECT, 0, expected="REJECT A1c")

    def test_tool_failure_cannot_pass_with_valid_json(self):
        self.assert_harness_error(ACCEPT, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX signal exit status")
    def test_signal_after_valid_json_cannot_pass(self):
        self.assert_harness_error(ACCEPT, kill=True)

    def test_error_count_must_match_diagnostics(self):
        payload = copy.deepcopy(ACCEPT)
        payload["summary"]["errors"] = 1
        self.assert_harness_error(payload)

    def test_warning_count_must_match_diagnostics(self):
        payload = copy.deepcopy(ACCEPT)
        payload["summary"]["warnings"] = 1
        self.assert_harness_error(payload)

    def test_reject_requires_an_error(self):
        payload = copy.deepcopy(ACCEPT)
        payload["summary"]["verdict"] = "reject"
        self.assert_harness_error(payload, 1, expected="REJECT A1c")

    def test_invalid_envelopes_are_harness_errors(self):
        for payload in (None, [], {"summary": None, "diagnostics": []},
                        {"summary": ACCEPT["summary"], "diagnostics": [None]}):
            with self.subTest(payload=payload):
                self.assert_harness_error(payload)

    def test_checker_timeout_is_a_harness_error(self):
        with mock.patch.object(corpus_harness.subprocess, "run", side_effect=
                               subprocess.TimeoutExpired(["hyb"], 60)):
            with self.assertRaisesRegex(RuntimeError, "checker failed"):
                corpus_harness.run_checker("hyb", "fixture.hy")

    def test_checker_launch_failure_is_a_harness_error(self):
        with mock.patch.object(corpus_harness.subprocess, "run", side_effect=
                               PermissionError("not executable")):
            with self.assertRaisesRegex(RuntimeError, "checker failed"):
                corpus_harness.run_checker("hyb", "fixture.hy")

    def test_unregistered_corpus_is_misuse_not_success(self):
        proc = self.run_harness(ACCEPT, expected=None)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("no registered", proc.stderr)

    def test_unregistered_extra_file_is_still_skipped(self):
        # Keeping a helper .hy file without a header is allowed when at
        # least one actual registered case is verified.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "01-case.hy").write_text(
                "// EXPECTED-VERDICT: ACCEPT\nfn main() {}\n", encoding="utf-8"
            )
            (root / "helper.hy").write_text("fn helper() {}\n", encoding="utf-8")
            checker = root / "checker"
            checker.write_text(
                f"#!{sys.executable}\nprint({json.dumps(ACCEPT)!r})\n",
                encoding="utf-8",
            )
            checker.chmod(0o700)
            proc = subprocess.run(
                [sys.executable, str(HARNESS), "--bin", str(checker),
                 "--corpus", str(root)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("SKIP (no EXPECTED-VERDICT): helper.hy", proc.stdout)
            self.assertIn("verdict agreement: 1/1", proc.stdout)


if __name__ == "__main__":
    unittest.main()

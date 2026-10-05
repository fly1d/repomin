import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repomin.model import FailureSpec, ProcessFailureSignature, RunResult, PYTHON_FRAME_POLICY
from repomin.oracle import (
    BASELINE_RATE_EVIDENCE_FIELDS,
    CommandRunner,
    FailureOracle,
    OracleError,
    clopper_pearson_lower_bound,
    exact_binomial_upper_tail,
)
from repomin.signature import (
    collect_surefire_diagnostics,
    extract_process_failure,
    extract_java_exception,
    extract_python_exception,
    extract_run_python_exception,
    format_process_failure_signature,
)

ROOT_CAUSE = """\
[ERROR] java.lang.RuntimeException: wrapper
[ERROR]     at demo.Wrapper.call(Wrapper.java:10)
[ERROR] Caused by: java.lang.NoSuchMethodError: demo.Target.missing()
[ERROR]     at dev.repomin.TriggerTest.run(TriggerTest.java:27)
[ERROR]     at java.base/java.lang.reflect.Method.invoke(Method.java:569)
"""


def _stack(message: str, frame: str, line: int) -> str:
    return "java.lang.NoSuchMethodError: %s\n\tat %s(Source.java:%d)\n" % (
        message,
        frame,
        line,
    )


def _python_stack(
    message: str,
    function: str = "checkout",
    line: int = 42,
    root: str = "/tmp/session-1",
) -> str:
    return """\
Traceback (most recent call last):
  File "%s/reproduce.py", line 9, in <module>
    checkout()
  File "%s/service.py", line %d, in %s
    raise ValueError(%r)
ValueError: %s
""" % (root, root, line, function, message, message)


class _SequenceRunner:
    def __init__(self, outputs) -> None:
        self.outputs = iter(outputs)

    def run(self, _cwd: Path) -> RunResult:
        return RunResult(1, next(self.outputs), "", 0.01)


class _ResultSequenceRunner:
    def __init__(self, results) -> None:
        self.results = iter(results)

    def run(self, _cwd: Path) -> RunResult:
        return next(self.results)


class ProcessFailureSignatureTest(unittest.TestCase):
    def test_normalizes_signal_windows_status_and_plain_exit_code(self) -> None:
        signal_number = int(signal.SIGTERM)
        self.assertEqual(
            ProcessFailureSignature("posix_signal", signal_number),
            extract_process_failure(
                RunResult(-signal_number, "", "", 0.01)
            ),
        )
        expected_windows = ProcessFailureSignature(
            "windows_status",
            0xC0000005,
        )
        self.assertEqual(
            expected_windows,
            extract_process_failure(RunResult(0xC0000005, "", "", 0.01)),
        )
        self.assertEqual(
            expected_windows,
            extract_process_failure(RunResult(-1073741819, "", "", 0.01)),
        )
        self.assertEqual(
            ProcessFailureSignature("exit_code", 139),
            extract_process_failure(RunResult(139, "", "", 0.01)),
        )
        self.assertEqual(
            "Windows status 0xC0000005 (EXCEPTION_ACCESS_VIOLATION)",
            format_process_failure_signature(expected_windows),
        )

    def test_does_not_sign_success_timeout_or_resource_exhaustion(self) -> None:
        self.assertIsNone(extract_process_failure(RunResult(0, "", "", 0.01)))
        self.assertIsNone(
            extract_process_failure(
                RunResult(124, "", "", 0.01, timed_out=True)
            )
        )
        self.assertIsNone(
            extract_process_failure(
                RunResult(
                    137,
                    "",
                    "",
                    0.01,
                    resource_exhausted=True,
                    resource_reason="memory",
                )
            )
        )

    def test_oracle_learns_exact_process_termination(self) -> None:
        abort = int(signal.SIGABRT)
        term = int(signal.SIGTERM)
        oracle = FailureOracle(
            _ResultSequenceRunner(
                [
                    RunResult(-abort, "", "", 0.01),
                    RunResult(-abort, "", "", 0.01),
                ]
            ),
            FailureSpec(None, process_failure=True),
        )

        oracle.verify_baseline(Path("."), repeat=2)

        self.assertEqual(
            ProcessFailureSignature("posix_signal", abort),
            oracle.process_failure_signature,
        )
        self.assertTrue(oracle.accepts(RunResult(-abort, "", "", 0.01)))
        self.assertFalse(oracle.accepts(RunResult(-term, "", "", 0.01)))
        self.assertFalse(oracle.accepts(RunResult(128 + abort, "", "", 0.01)))

    def test_baseline_rejects_process_signature_drift(self) -> None:
        oracle = FailureOracle(
            _ResultSequenceRunner(
                [
                    RunResult(-int(signal.SIGABRT), "", "", 0.01),
                    RunResult(-int(signal.SIGTERM), "", "", 0.01),
                ]
            ),
            FailureSpec(None, process_failure=True),
        )

        with self.assertRaisesRegex(OracleError, "changed process failure signature"):
            oracle.verify_baseline(Path("."), repeat=2)

    def test_rate_evidence_excludes_process_signature_discovery(self) -> None:
        abort = int(signal.SIGABRT)
        oracle = FailureOracle(
            _ResultSequenceRunner(
                [
                    RunResult(0, "", "", 0.01),
                    RunResult(-abort, "", "", 0.01),
                    RunResult(-abort, "", "", 0.01),
                ]
            ),
            FailureSpec(None, process_failure=True),
            min_baseline_rate=0.2,
            confidence=0.5,
        )

        oracle.verify_baseline(Path("."), repeat=3, minimum_passes=1)

        self.assertEqual(2, oracle.baseline_passes)
        self.assertEqual(1, oracle.baseline_rate_evidence_runs)
        self.assertEqual(1, oracle.baseline_rate_evidence_passes)

    def test_process_signature_checkpoint_round_trip_fails_closed(self) -> None:
        abort = int(signal.SIGABRT)
        oracle = FailureOracle(
            _ResultSequenceRunner([RunResult(-abort, "", "", 0.01)]),
            FailureSpec(None, process_failure=True),
        )
        oracle.verify_baseline(Path("."), repeat=1)
        state = oracle.checkpoint_state()

        restored = FailureOracle(
            _ResultSequenceRunner([]),
            FailureSpec(None, process_failure=True),
        )
        self.assertFalse(restored.restore_checkpoint_state(state))
        self.assertEqual(oracle.process_failure_signature, restored.process_failure_signature)

        missing = dict(state)
        missing.pop("process_failure_signature")
        with self.assertRaisesRegex(OracleError, "missing its learned failure signature"):
            restored.restore_checkpoint_state(missing)

        tampered = dict(state)
        tampered["process_failure_signature"] = {
            "kind": "posix_signal",
            "code": 999,
        }
        with self.assertRaisesRegex(OracleError, "invalid process failure signature"):
            restored.restore_checkpoint_state(tampered)

    def test_process_signature_state_is_rejected_when_mode_is_disabled(self) -> None:
        state = {
            "process_failure_signature": {"kind": "exit_code", "code": 7}
        }
        oracle = FailureOracle(_ResultSequenceRunner([]), FailureSpec("failure"))
        with self.assertRaisesRegex(OracleError, "--process-failure is disabled"):
            oracle.restore_checkpoint_state(state)


class JavaExceptionSignatureTest(unittest.TestCase):
    def test_extracts_root_cause_and_normalizes_frames(self) -> None:
        signature = extract_java_exception(ROOT_CAUSE)

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("java.lang.NoSuchMethodError", signature.class_name)
        self.assertEqual("demo.Target.missing()", signature.message)
        self.assertEqual(
            (
                "dev.repomin.TriggerTest.run",
                "java.lang.reflect.Method.invoke",
            ),
            signature.frames,
        )

    def test_match_selects_the_relevant_independent_java_failure(self) -> None:
        output = (
            _stack("unrelated", "demo.Unrelated.run", 10)
            + _stack("checkout target", "demo.Checkout.run", 20)
        )

        signature = extract_java_exception(output, re.compile("checkout target"))

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("checkout target", signature.message)
        self.assertEqual(("demo.Checkout.run",), signature.frames)

    def test_match_outside_multiple_java_failures_is_ambiguous(self) -> None:
        output = (
            "TARGET_TEST\n"
            + _stack("unrelated", "demo.Unrelated.run", 10)
            + _stack("changed target", "demo.Checkout.run", 20)
        )

        self.assertIsNone(extract_java_exception(output, re.compile("TARGET_TEST")))

    def test_oracle_does_not_accept_stable_unrelated_java_failure(self) -> None:
        baseline = (
            _stack("unrelated", "demo.Unrelated.run", 10)
            + _stack("checkout target", "demo.Checkout.run", 20)
        )
        oracle = FailureOracle(
            _SequenceRunner([baseline]),
            FailureSpec("checkout target", java_exception=True),
        )
        oracle.verify_baseline(Path("."), repeat=1)

        changed = RunResult(
            1,
            _stack("unrelated", "demo.Unrelated.run", 30)
            + "checkout target summary\n"
            + _stack("different failure", "demo.Checkout.run", 40),
            "",
            0.01,
        )

        self.assertFalse(oracle.accepts(changed))

    def test_oracle_learns_stable_signature_and_rejects_similar_errors(self) -> None:
        runner = _SequenceRunner(
            [
                _stack("demo.Target.missing()", "demo.Trigger.run", 10),
                _stack("demo.Target.missing()", "demo.Trigger.run", 99),
            ]
        )
        oracle = FailureOracle(
            runner,
            FailureSpec("NoSuchMethodError", java_exception=True),
        )

        oracle.verify_baseline(Path("."), repeat=2)

        same_error = RunResult(
            1,
            _stack("demo.Target.missing()", "demo.Trigger.run", 250),
            "",
            0.01,
        )
        different_message = RunResult(
            1,
            _stack("demo.Other.missing()", "demo.Trigger.run", 10),
            "",
            0.01,
        )
        different_frame = RunResult(
            1,
            _stack("demo.Target.missing()", "demo.Other.run", 10),
            "",
            0.01,
        )
        self.assertTrue(oracle.accepts(same_error))
        self.assertFalse(oracle.accepts(different_message))
        self.assertFalse(oracle.accepts(different_frame))

    def test_baseline_requires_a_java_stack_frame(self) -> None:
        runner = _SequenceRunner(["NoSuchMethodError without a stack trace"])
        oracle = FailureOracle(
            runner,
            FailureSpec("NoSuchMethodError", java_exception=True),
        )

        with self.assertRaisesRegex(OracleError, "Java exception"):
            oracle.verify_baseline(Path("."), repeat=1)

    def test_repeated_baseline_rejects_signature_drift(self) -> None:
        runner = _SequenceRunner(
            [
                _stack("demo.Target.missing()", "demo.Trigger.run", 10),
                _stack("demo.Other.missing()", "demo.Other.run", 10),
            ]
        )
        oracle = FailureOracle(
            runner,
            FailureSpec("NoSuchMethodError", java_exception=True),
        )

        with self.assertRaisesRegex(OracleError, "changed Java exception signature"):
            oracle.verify_baseline(Path("."), repeat=2)

    def test_count_only_baseline_still_selects_the_stable_mode(self) -> None:
        first = _stack("ORIGINAL_FAILURE", "demo.First.run", 10)
        second = _stack("ORIGINAL_FAILURE", "demo.Second.run", 10)
        oracle = FailureOracle(
            _SequenceRunner([first, second, second]),
            FailureSpec("ORIGINAL_FAILURE", java_exception=True),
        )

        oracle.verify_baseline(Path("."), repeat=3, minimum_passes=2)

        self.assertTrue(oracle.accepts(RunResult(1, second, "", 0.01)))
        self.assertFalse(oracle.accepts(RunResult(1, first, "", 0.01)))

    def test_rate_gated_signature_uses_post_discovery_evidence(self) -> None:
        signature_families = (
            (
                "Java",
                lambda index: _stack(
                    "ORIGINAL_FAILURE",
                    "demo.Variant%d.run" % index,
                    10,
                ),
                lambda: FailureSpec("ORIGINAL_FAILURE", java_exception=True),
            ),
            (
                "Python",
                lambda index: _python_stack(
                    "ORIGINAL_FAILURE",
                    "variant_%d" % index,
                ),
                lambda: FailureSpec("ORIGINAL_FAILURE", python_exception=True),
            ),
        )

        for language, render, spec in signature_families:
            accepted_variants = []
            for second_variant in range(100):
                oracle = FailureOracle(
                    _SequenceRunner([render(0), render(second_variant)]),
                    spec(),
                    min_baseline_rate=0.02,
                )
                try:
                    oracle.verify_baseline(
                        Path("."),
                        repeat=2,
                        minimum_passes=1,
                    )
                except OracleError:
                    continue
                accepted_variants.append(second_variant)

            with self.subTest(language=language):
                self.assertEqual([0], accepted_variants)

    def test_rate_evidence_excludes_late_signature_discovery_and_prior_runs(
        self,
    ) -> None:
        signature_families = (
            (
                "Java",
                "ORIGINAL_FAILURE without a stack frame",
                lambda: _stack("ORIGINAL_FAILURE", "demo.Target.run", 10),
                lambda: FailureSpec("ORIGINAL_FAILURE", java_exception=True),
            ),
            (
                "Python",
                "ORIGINAL_FAILURE without a traceback",
                lambda: _python_stack("ORIGINAL_FAILURE", "target"),
                lambda: FailureSpec("ORIGINAL_FAILURE", python_exception=True),
            ),
        )

        for language, unsigned, signed, spec in signature_families:
            outputs = [
                "DIFFERENT_FAILURE",
                unsigned,
                signed(),
                signed(),
                signed(),
            ]
            oracle = FailureOracle(
                _SequenceRunner(outputs),
                spec(),
                min_baseline_rate=0.2,
                confidence=0.5,
            )

            oracle.verify_baseline(Path("."), repeat=5, minimum_passes=1)

            with self.subTest(language=language):
                self.assertEqual(5, oracle.baseline_runs)
                self.assertEqual(3, oracle.baseline_passes)
                self.assertEqual(2, oracle.baseline_rate_evidence_runs)
                self.assertEqual(2, oracle.baseline_rate_evidence_passes)
                self.assertEqual(
                    clopper_pearson_lower_bound(2, 2, 0.5),
                    oracle.baseline_exact_lower_bound,
                )
                self.assertEqual(
                    float(exact_binomial_upper_tail(2, 2, 0.2)),
                    oracle.baseline_exact_p_value,
                )
                self.assertTrue(oracle.baseline_exact_rate_gate_passed)

    def test_last_sample_signature_discovery_has_zero_rate_evidence(self) -> None:
        signature_families = (
            (
                "Java",
                "ORIGINAL_FAILURE without a stack frame",
                lambda: _stack("ORIGINAL_FAILURE", "demo.Target.run", 10),
                lambda: FailureSpec("ORIGINAL_FAILURE", java_exception=True),
            ),
            (
                "Python",
                "ORIGINAL_FAILURE without a traceback",
                lambda: _python_stack("ORIGINAL_FAILURE", "target"),
                lambda: FailureSpec("ORIGINAL_FAILURE", python_exception=True),
            ),
        )

        for language, unsigned, signed, spec in signature_families:
            oracle = FailureOracle(
                _SequenceRunner(["DIFFERENT_FAILURE", unsigned, signed()]),
                spec(),
                min_baseline_rate=0.04,
            )

            with self.subTest(language=language), self.assertRaisesRegex(
                OracleError,
                "0/0 rate-evidence passes",
            ):
                oracle.verify_baseline(Path("."), repeat=3, minimum_passes=1)
            self.assertEqual(0, oracle.baseline_rate_evidence_runs)
            self.assertEqual(0, oracle.baseline_rate_evidence_passes)
            self.assertEqual(0.0, oracle.baseline_exact_lower_bound)
            self.assertEqual(1.0, oracle.baseline_exact_p_value)
            self.assertFalse(oracle.baseline_exact_rate_gate_passed)

    def test_legacy_signature_checkpoint_without_rate_evidence_is_rejected(
        self,
    ) -> None:
        signature_families = (
            (
                "Java",
                lambda: _stack("ORIGINAL_FAILURE", "demo.Target.run", 10),
                lambda: FailureSpec("ORIGINAL_FAILURE", java_exception=True),
            ),
            (
                "Python",
                lambda: _python_stack("ORIGINAL_FAILURE", "target"),
                lambda: FailureSpec("ORIGINAL_FAILURE", python_exception=True),
            ),
        )

        for language, signed, spec in signature_families:
            oracle = FailureOracle(
                _SequenceRunner([signed(), signed()]),
                spec(),
                min_baseline_rate=0.2,
                confidence=0.5,
            )
            oracle.verify_baseline(Path("."), repeat=2, minimum_passes=1)
            state = oracle.checkpoint_state()
            for key in BASELINE_RATE_EVIDENCE_FIELDS:
                state.pop(key)

            restored = FailureOracle(
                _SequenceRunner([]),
                spec(),
                min_baseline_rate=0.2,
                confidence=0.5,
            )
            with self.subTest(language=language), self.assertRaisesRegex(
                OracleError,
                "lacks post-discovery baseline rate evidence",
            ):
                restored.restore_checkpoint_state(state)

    def test_collects_surefire_failure_when_console_has_no_stack(self) -> None:
        report = """\
<testsuite name="demo">
  <testcase name="fails" classname="demo.Trigger">
    <error type="java.lang.NoSuchMethodError" message="demo.Target.missing()"><![CDATA[
java.lang.NoSuchMethodError: demo.Target.missing()
    at demo.Trigger.run(Trigger.java:42)
]]></error>
  </testcase>
</testsuite>
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "target" / "surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-demo.Trigger.xml").write_text(report, encoding="utf-8")

            diagnostics = collect_surefire_diagnostics(root)
            runner = CommandRunner(
                "false",
                timeout_seconds=5,
                collect_java_diagnostics=True,
            )
            result = runner.run(root)
            oracle = FailureOracle(
                runner,
                FailureSpec("NoSuchMethodError", java_exception=True),
            )

        self.assertIn("demo.Target.missing()", diagnostics)
        self.assertEqual(diagnostics, result.diagnostics)
        self.assertTrue(oracle.accepts(result))


class PythonExceptionSignatureTest(unittest.TestCase):
    def test_known_roots_preserve_module_identity_across_fresh_copies(self) -> None:
        roots = (
            "/workspace/demo/.repomin-session-a/baseline-0001",
            "/workspace/demo/.repomin-session-b/baseline-0002",
            "/tmp/fresh-copy",
            "/workspace",
            "C:/work/fresh-copy",
        )
        signatures = []
        for root in roots:
            for separator in ("/", "\\"):
                path = (root + "/app/payments/service.py").replace("/", separator)
                text = ('Traceback (most recent call last):\n'
                        '  File "%s", line 42, in checkout\n'
                        'ValueError: payment failed\n' % path)
                signatures.append(extract_python_exception(text, execution_root=root))
        self.assertTrue(all(s == signatures[0] for s in signatures))
        self.assertEqual(("app/payments/service.py:checkout",), signatures[0].frames)
        other = text.replace("payments", "refunds")
        self.assertNotEqual(signatures[0], extract_python_exception(other, execution_root=root))

    def test_root_matching_requires_a_component_boundary_and_normalizes_dot_segments(self) -> None:
        def signature(path, root):
            return extract_python_exception(
                'Traceback (most recent call last):\n'
                '  File "%s", line 1, in work\nValueError: failure\n' % path,
                execution_root=root,
            )
        self.assertEqual(("app/service.py:work",), signature(
            "/workspace/copy/app/../app/service.py", "/workspace/copy").frames)
        self.assertEqual(("service.py:work",), signature(
            "/workspace/copy-other/app/service.py", "/workspace/copy").frames)
        self.assertEqual(("service.py:work",), signature(
            "/workspace/copy/../external/app/service.py", "/workspace/copy").frames)
        self.assertEqual(("app/service.py:work",), signature(
            "c:/WORK/COPY/app/service.py", "C:/work/copy").frames)

    def test_root_context_handles_absolute_and_relative_pytest_locations(self) -> None:
        root = "/workspace/copy"
        signatures = [extract_python_exception(
            "E   ValueError: failure\n%s:10: ValueError\n" % path,
            execution_root=root,
        ) for path in (root + "/app/service.py", "app/service.py")]
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(("app/service.py:<module>",), signatures[0].frames)

    def test_fresh_copy_oracle_and_checkpoint_keep_context_and_reject_near_matches(self) -> None:
        class RootRunner:
            def run(self, cwd):
                return RunResult(1, _python_stack("payment failed", root=str(cwd / "payments")), "", 0.01,
                                 python_execution_root=str(cwd))
        runner = RootRunner()
        oracle = FailureOracle(runner, FailureSpec("ValueError", python_exception=True))
        roots = iter([Path("/workspace/demo/session-a"), Path("/workspace/demo/session-b")])
        oracle.verify_baseline(Path("."), repeat=2, prepare=lambda: next(roots))
        candidate = runner.run(Path("/workspace/demo/candidate"))
        self.assertTrue(oracle.accepts(candidate))
        from dataclasses import replace
        self.assertFalse(oracle.accepts(replace(candidate, stdout=candidate.stdout.replace("payment failed", "refund failed"))))
        self.assertFalse(oracle.accepts(replace(candidate, stdout=candidate.stdout.replace("payments", "refunds"))))
        from repomin.session import _run_result_to_dict, _run_result_from_dict
        saved = _run_result_to_dict(candidate)
        self.assertEqual(candidate, _run_result_from_dict(saved))
        state = oracle.checkpoint_state()
        self.assertEqual(PYTHON_FRAME_POLICY, state["python_frame_policy"])
        restored = FailureOracle(runner, oracle.spec)
        restored.restore_checkpoint_state(state)
        self.assertTrue(restored.accepts(_run_result_from_dict(saved)))
        for policy in (None, "unsupported-policy"):
            broken = dict(state, python_frame_policy=policy)
            with self.assertRaisesRegex(OracleError, "normalization policy changed"):
                restored.restore_checkpoint_state(broken)
        broken_signature = dict(state, python_exception_signature=dict(
            state["python_exception_signature"], normalization_policy="unsupported-policy",
        ))
        with self.assertRaisesRegex(OracleError, "invalid Python frame normalization policy"):
            restored.restore_checkpoint_state(broken_signature)
        old = dict(state)
        del old["python_frame_policy"]
        with self.assertRaisesRegex(OracleError, "start a new session"):
            restored.restore_checkpoint_state(old)

    def test_legacy_oracle_retains_old_frame_contract_for_report_replay(self) -> None:
        result = RunResult(1, _python_stack("payment failed", root="/tmp/copy/payments"), "", 0.01,
                           python_execution_root="/tmp/copy")
        legacy = FailureOracle(_SequenceRunner([]), FailureSpec("ValueError", python_exception=True),
                               python_frame_policy=None)
        legacy.pin_failure_signature(extract_run_python_exception(result, use_execution_root=False))
        self.assertTrue(legacy.accepts(result))
        modern = FailureOracle(_SequenceRunner([]), legacy.spec)
        modern.pin_failure_signature(legacy.python_exception_signature)
        self.assertFalse(modern.accepts(result))

    def test_real_runner_matches_fresh_copies_and_rejects_same_basename_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ("baseline-a", "baseline-b", "candidate", "other")]
            for root in roots:
                folder = root / ("refunds" if root.name == "other" else "payments")
                folder.mkdir(parents=True)
                (folder / "service.py").write_text(
                    "def checkout():\n    raise ValueError('payment failed')\ncheckout()\n",
                    encoding="utf-8",
                )
            runner = CommandRunner(subprocess.list2cmdline([sys.executable, "payments/service.py"]), 5)
            oracle = FailureOracle(runner, FailureSpec("ValueError", python_exception=True))
            baselines = iter(roots[:2])
            oracle.verify_baseline(roots[0], repeat=2, prepare=lambda: next(baselines))
            self.assertTrue(oracle.accepts(runner.run(roots[2])))
            other = CommandRunner(subprocess.list2cmdline([sys.executable, "refunds/service.py"]), 5)
            other_result = other.run(roots[3])
            self.assertFalse(oracle.accepts(other_result),
                             "baseline=%r; candidate=%r; root=%r; output=%r" % (
                                 oracle.python_exception_signature,
                                 extract_run_python_exception(other_result),
                                 other_result.python_execution_root,
                                 other_result.output,
                             ))
            self.assertEqual(PYTHON_FRAME_POLICY, oracle.python_exception_signature.normalization_policy)

    def test_extracts_exception_and_normalizes_paths_lines_and_whitespace(self) -> None:
        first = extract_python_exception(
            _python_stack("payment   failed", line=42, root="/tmp/session-1")
        )
        second = extract_python_exception(
            _python_stack("payment failed", line=999, root="/var/trial-200")
        )

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual("ValueError", first.class_name)
        self.assertEqual("payment failed", first.message)
        self.assertEqual(
            ("service.py:checkout", "reproduce.py:<module>"),
            first.frames,
        )

    def test_selects_root_cause_from_a_chained_traceback(self) -> None:
        chained = (
            _python_stack("database unavailable")
            + "\nThe above exception was the direct cause of the following exception:\n\n"
            + "Traceback (most recent call last):\n"
            + '  File "/tmp/reproduce.py", line 20, in <module>\n'
            + "    raise RuntimeError('request failed')\n"
            + "RuntimeError: request failed\n"
        )

        signature = extract_python_exception(chained)

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("ValueError", signature.class_name)
        self.assertEqual("database unavailable", signature.message)

        wrapper = extract_python_exception(chained, re.compile("request failed"))
        self.assertIsNotNone(wrapper)
        assert wrapper is not None
        self.assertEqual("RuntimeError", wrapper.class_name)
        self.assertEqual("request failed", wrapper.message)

    def test_selects_leaf_from_python_exception_group(self) -> None:
        grouped = """\
  + Exception Group Traceback (most recent call last):
  |   File "/workspace/group.py", line 3, in run
  |     raise ExceptionGroup("group", [ValueError("leaf")])
  | ExceptionGroup: group (1 sub-exception)
  +-+---------------- 1 ----------------
    | Traceback (most recent call last):
    |   File "/workspace/worker.py", line 7, in work
    |     raise ValueError("leaf")
    | ValueError: leaf
"""

        signature = extract_python_exception(grouped)

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("ValueError", signature.class_name)
        self.assertEqual("leaf", signature.message)
        self.assertEqual(("worker.py:work",), signature.frames)

    def test_extracts_pytest_rendered_exception(self) -> None:
        output = """\
>       checkout(42)
E       RuntimeError: FastAPI route regression

app/main.py:10: RuntimeError
"""

        signature = extract_python_exception(output)

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("RuntimeError", signature.class_name)
        self.assertEqual("FastAPI route regression", signature.message)
        self.assertEqual(("app/main.py:<module>",), signature.frames)

    def test_match_selects_the_relevant_pytest_failure(self) -> None:
        output = """\
E       AssertionError: unrelated failure
tests/test_other.py:7: AssertionError
E       RuntimeError: checkout target
app/main.py:10: RuntimeError
"""

        signature = extract_python_exception(output, re.compile("checkout target"))

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual("RuntimeError", signature.class_name)
        self.assertEqual("checkout target", signature.message)

    def test_match_outside_multiple_python_failures_is_ambiguous(self) -> None:
        output = (
            "tests/test_checkout.py::test_target\n"
            + _python_stack("unrelated", function="background")
            + _python_stack("changed target", function="checkout")
        )

        signature = extract_python_exception(
            output,
            re.compile(r"tests/test_checkout\.py::test_target"),
        )

        self.assertIsNone(signature)

    def test_oracle_does_not_accept_stable_unrelated_python_failure(self) -> None:
        baseline = (
            _python_stack("unrelated", function="background")
            + _python_stack("checkout target", function="checkout")
        )
        oracle = FailureOracle(
            _SequenceRunner([baseline]),
            FailureSpec("checkout target", python_exception=True),
        )
        oracle.verify_baseline(Path("."), repeat=1)

        changed = RunResult(
            1,
            _python_stack("unrelated", function="background")
            + "checkout target summary\n"
            + _python_stack("different failure", function="checkout"),
            "",
            0.01,
        )

        self.assertFalse(oracle.accepts(changed))

    def test_oracle_rejects_same_class_and_message_from_a_different_frame(self) -> None:
        runner = _SequenceRunner(
            [
                _python_stack("payment failed", "checkout", 10, "/tmp/one"),
                _python_stack("payment failed", "checkout", 99, "/tmp/two"),
            ]
        )
        oracle = FailureOracle(
            runner,
            FailureSpec("ValueError", python_exception=True),
        )
        oracle.verify_baseline(Path("."), repeat=2)

        same = RunResult(
            1,
            _python_stack("payment failed", "checkout", 300, "/tmp/three"),
            "",
            0.01,
        )
        different_frame = RunResult(
            1,
            _python_stack("payment failed", "fallback", 10, "/tmp/four"),
            "",
            0.01,
        )
        self.assertTrue(oracle.accepts(same))
        self.assertFalse(oracle.accepts(different_frame))

    def test_baseline_requires_a_python_traceback_frame(self) -> None:
        runner = _SequenceRunner(
            ["Traceback (most recent call last):\nValueError: no frame\n"]
        )
        oracle = FailureOracle(
            runner,
            FailureSpec("ValueError", python_exception=True),
        )

        with self.assertRaisesRegex(OracleError, "Python exception"):
            oracle.verify_baseline(Path("."), repeat=1)

    def test_repeated_baseline_rejects_python_signature_drift(self) -> None:
        runner = _SequenceRunner(
            [
                _python_stack("payment failed", "checkout"),
                _python_stack("other failure", "fallback"),
            ]
        )
        oracle = FailureOracle(
            runner,
            FailureSpec("ValueError", python_exception=True),
        )

        with self.assertRaisesRegex(OracleError, "changed Python exception signature"):
            oracle.verify_baseline(Path("."), repeat=2)


if __name__ == "__main__":
    unittest.main()

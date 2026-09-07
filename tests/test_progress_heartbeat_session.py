from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repomin.model import FailureSpec, ReductionStats, RunResult
from repomin.oracle import FailureOracle
from repomin.session import HeartbeatSnapshot, MutationCandidate, ReductionSession


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Runner:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, _cwd: Path) -> RunResult:
        return RunResult(1, self.output, "", 0.01)


def _candidate(index: int) -> MutationCandidate:
    def mutation(root: Path) -> bool:
        (root / ("candidate-%d" % index)).write_text(
            "candidate\n",
            encoding="utf-8",
        )
        return True

    return MutationCandidate("private/path/candidate-%d" % index, mutation)


class ProgressHeartbeatSessionTests(unittest.TestCase):
    def _session(
        self,
        root: Path,
        output: str,
        **kwargs,
    ) -> ReductionSession:
        source = root / "source"
        source.mkdir()
        (source / "seed.txt").write_text("seed\n", encoding="utf-8")
        oracle = FailureOracle(_Runner(output), FailureSpec("ORIGINAL_FAILURE"))
        return ReductionSession(
            source,
            oracle,
            ReductionStats(source_files=1, source_bytes=5),
            **kwargs,
        )

    def test_fake_clock_emits_only_when_time_threshold_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = _FakeClock()
            snapshots = []
            with patch("repomin.session.time.monotonic", clock):
                session = self._session(
                    Path(directory),
                    "DIFFERENT_FAILURE",
                    heartbeat=snapshots.append,
                    heartbeat_interval_seconds=30,
                    heartbeat_attempt_interval=None,
                )
                try:
                    self.assertFalse(
                        session.try_mutation("files", "private one", _candidate(1).mutation)
                    )
                    clock.advance(29)
                    self.assertFalse(
                        session.try_mutation("files", "private two", _candidate(2).mutation)
                    )
                    self.assertEqual([], snapshots)

                    clock.advance(1)
                    self.assertFalse(
                        session.try_mutation(
                            "files", "private three", _candidate(3).mutation
                        )
                    )
                finally:
                    session.close()

            self.assertEqual(1, len(snapshots))
            self.assertEqual("interval", snapshots[0].reason)
            self.assertEqual(30.0, snapshots[0].elapsed_seconds)
            self.assertEqual(3, snapshots[0].completed_attempts)

    def test_begin_reduction_resets_process_local_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = _FakeClock()
            snapshots = []
            with patch("repomin.session.time.monotonic", clock):
                session = self._session(
                    Path(directory),
                    "DIFFERENT_FAILURE",
                    heartbeat=snapshots.append,
                    heartbeat_interval_seconds=30,
                    heartbeat_attempt_interval=None,
                )
                try:
                    clock.advance(20)
                    session.begin_reduction()
                    clock.advance(30)
                    self.assertFalse(
                        session.try_mutation("files", "private", _candidate(1).mutation)
                    )
                finally:
                    session.close()

            self.assertEqual(1, len(snapshots))
            self.assertEqual(30.0, snapshots[0].elapsed_seconds)

    def test_oracle_samples_exclude_cache_hits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            session = self._session(
                Path(directory),
                "DIFFERENT_FAILURE",
                heartbeat=snapshots.append,
                heartbeat_interval_seconds=None,
                heartbeat_attempt_interval=2,
            )
            candidate = _candidate(1)
            try:
                self.assertFalse(
                    session.try_mutation("files", "private one", candidate.mutation)
                )
                self.assertFalse(
                    session.try_mutation("files", "private two", candidate.mutation)
                )
            finally:
                session.close()

            self.assertEqual(1, len(snapshots))
            self.assertEqual(2, snapshots[0].candidate_samples)
            self.assertEqual(1, snapshots[0].oracle_samples)
            self.assertEqual(1, snapshots[0].cache_hits)

    def test_all_rejected_attempts_trigger_aggregate_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            session = self._session(
                Path(directory),
                "DIFFERENT_FAILURE",
                jobs=2,
                cache_enabled=False,
                heartbeat=snapshots.append,
                heartbeat_interval_seconds=None,
                heartbeat_attempt_interval=3,
            )
            try:
                self.assertIsNone(
                    session.try_mutations(
                        "files",
                        [_candidate(index) for index in range(4)],
                    )
                )
            finally:
                session.close()

            self.assertEqual(1, len(snapshots))
            snapshot = snapshots[0]
            self.assertEqual(4, snapshot.attempts)
            self.assertEqual(4, snapshot.completed_attempts)
            self.assertEqual(4, snapshot.rejected)
            self.assertEqual(0, snapshot.accepted)

    def test_parallel_windows_publish_monotonic_complete_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            callback_threads = []
            caller_thread = threading.get_ident()

            def heartbeat(snapshot: HeartbeatSnapshot) -> None:
                callback_threads.append(threading.get_ident())
                snapshots.append(snapshot)

            session = self._session(
                Path(directory),
                "DIFFERENT_FAILURE",
                jobs=2,
                cache_enabled=False,
                heartbeat=heartbeat,
                heartbeat_interval_seconds=None,
                heartbeat_attempt_interval=1,
            )
            try:
                session.try_mutations(
                    "files",
                    [_candidate(index) for index in range(4)],
                )
            finally:
                session.close()

            self.assertEqual([2, 4], [item.completed_attempts for item in snapshots])
            self.assertEqual([2, 4], [item.rejected for item in snapshots])
            self.assertEqual([caller_thread, caller_thread], callback_threads)

    def test_accepted_window_notifies_once_after_every_attempt_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            session = self._session(
                Path(directory),
                "ORIGINAL_FAILURE",
                jobs=3,
                cache_enabled=False,
                heartbeat=snapshots.append,
                heartbeat_interval_seconds=None,
                heartbeat_attempt_interval=1,
            )
            try:
                selected = session.try_mutations(
                    "files",
                    [_candidate(index) for index in range(3)],
                )
            finally:
                session.close()

            self.assertEqual(0, selected)
            self.assertEqual(1, len(snapshots))
            snapshot = snapshots[0]
            self.assertEqual(3, snapshot.completed_attempts)
            self.assertEqual(1, snapshot.accepted)
            self.assertEqual(2, snapshot.superseded)
            self.assertEqual(0, snapshot.rejected)
            self.assertNotIn("private", repr(snapshot))
            self.assertTrue(
                all(
                    value is None or isinstance(value, (str, int, float))
                    for value in vars(snapshot).values()
                )
            )

    def test_force_emits_phase_snapshot_without_waiting_for_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            session = self._session(
                Path(directory),
                "DIFFERENT_FAILURE",
                heartbeat=snapshots.append,
                heartbeat_interval_seconds=3600,
                heartbeat_attempt_interval=1000,
            )
            try:
                self.assertTrue(session.notify_heartbeat("baseline", force=True))
            finally:
                session.close()

            self.assertEqual(1, len(snapshots))
            self.assertEqual("baseline", snapshots[0].phase)
            self.assertEqual("phase", snapshots[0].reason)
            self.assertEqual(0, snapshots[0].completed_attempts)

    def test_no_callback_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "DIFFERENT_FAILURE")
            try:
                self.assertFalse(session.notify_heartbeat("files", force=True))
                self.assertFalse(
                    session.try_mutation("files", "private", _candidate(1).mutation)
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from repomin.execution import RunnerError
from repomin.gitignore import GitignoreMatcher
from repomin.model import (
    CANDIDATE_FAMILY_CONTROL_POLICY,
    HOLDOUT_CERTIFICATION_POLICY,
    TREE_CONTENT_FINGERPRINT_POLICY,
    TREE_FINGERPRINT_POLICY,
    HoldoutCertification,
    HoldoutSample,
    ReductionEvent,
    ReductionPhaseStats,
    ReductionStats,
    RunResult,
)
from repomin.oracle import (
    BASELINE_RATE_EVIDENCE_FIELDS,
    FailureOracle,
    anytime_lower_bound,
    candidate_family_alpha_upper_bound,
    candidate_family_confidence,
    clopper_pearson_lower_bound,
    exact_binomial_rate_gate,
    exact_binomial_upper_tail,
)


DEFAULT_IGNORES = {
    ".git",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".repomin",
    ".tox",
    ".venv",
    "__pycache__",
    ".vs",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
    "vendor",
}


class IgnoreSet(set):
    """Exact basename and repository-relative subtree exclusions.

    An optional :class:`~repomin.gitignore.GitignoreMatcher` adds file-local
    gitignore-style exclusions after the exact basename and path checks.
    Negated gitignore rules can only re-include paths excluded by earlier
    gitignore rules; they can never re-include a default or exact exclusion.
    """

    def __init__(
        self,
        names: Iterable[str] = (),
        paths: Iterable[str] = (),
        gitignore: Optional[GitignoreMatcher] = None,
        keep_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(names)
        self.paths = tuple(paths)
        self._path_parts = tuple(PurePosixPath(path).parts for path in self.paths)
        self.gitignore = gitignore
        self.keep_paths = tuple(keep_paths)
        self._keep_path_parts = tuple(
            PurePosixPath(path).parts for path in self.keep_paths
        )

    def matches(self, relative: Path, is_directory: Optional[bool] = None) -> bool:
        parts = relative.parts
        # An explicit keep is a stronger user-level declaration than an
        # exclusion rule.  Preserve the target and every ancestor needed to
        # reach it so initial copies and ignored-entry cleanup cannot erase it.
        if any(
            parts[: len(kept)] == kept or kept[: len(parts)] == parts
            for kept in self._keep_path_parts
        ):
            return False
        if any(part in self for part in parts):
            return True
        if any(
            parts[: len(path_parts)] == path_parts
            for path_parts in self._path_parts
        ):
            return True
        if self.gitignore is not None:
            posix = PurePosixPath(relative.as_posix())
            matched = self.gitignore.matches(posix, is_directory=is_directory)
            if (
                matched
                and is_directory is True
                and self.gitignore.may_reinclude_descendant(posix)
            ):
                return False
            return matched
        return False

_MUTATION_BLOCKING_FILESYSTEM_FLAG_NAMES = (
    "UF_IMMUTABLE",
    "UF_APPEND",
    "SF_IMMUTABLE",
    "SF_APPEND",
) + (
    ("UF_NOUNLINK", "SF_NOUNLINK")
    if sys.platform.startswith(("freebsd", "dragonfly"))
    else ()
)

Mutation = Callable[[Path], bool]
Progress = Callable[[str], None]


@dataclass(frozen=True)
class HeartbeatSnapshot:
    """Privacy-safe aggregate progress from one running reduction process."""

    phase: Optional[str]
    reason: str
    elapsed_seconds: float
    attempts: int
    completed_attempts: int
    accepted: int
    rejected: int
    superseded: int
    no_op: int
    aborted: int
    candidate_samples: int
    candidate_passes: int
    oracle_samples: int
    cache_hits: int


Heartbeat = Callable[[HeartbeatSnapshot], None]


@dataclass(frozen=True)
class MutationCandidate:
    description: str
    mutation: Mutation


CombineCandidates = Callable[
    [Sequence[MutationCandidate]],
    Optional[MutationCandidate],
]


@dataclass(frozen=True)
class _PreparedTrial:
    candidate_index: int
    attempt: int
    candidate: MutationCandidate
    path: Path
    cleanup_path: Path
    digest: str
    candidate_family_index: Optional[int]
    candidate_confidence: float
    candidate_alpha: Optional[float]


@dataclass(frozen=True)
class _RepeatedTrialOutcome:
    samples: Tuple[RunResult, ...]
    stop_reason: str
    anytime_lower_bound: Optional[float] = None
    cache_hit: bool = False


class SessionError(ValueError):
    """The requested persistent session cannot be created or resumed."""


class HoldoutCertificationError(RuntimeError):
    """A completed holdout attempt did not meet its certification contract."""


class ReductionSession:
    def __init__(
        self,
        source: Path,
        oracle: FailureOracle,
        stats: ReductionStats,
        progress: Optional[Progress] = None,
        ignores: Optional[Iterable[str]] = None,
        ignore_paths: Optional[Iterable[str]] = None,
        gitignore_matcher: Optional[GitignoreMatcher] = None,
        gitignore_files: Optional[Iterable[str]] = None,
        gitignore_recursive: bool = False,
        keep_paths: Optional[Iterable[str]] = None,
        max_attempts: Optional[int] = None,
        max_duration_seconds: Optional[float] = None,
        jobs: int = 1,
        cache_enabled: bool = True,
        temporary_parent: Optional[Path] = None,
        execution_working_directory_basename: Optional[str] = None,
        session_path: Optional[Path] = None,
        resume: bool = False,
        identity: Optional[dict] = None,
        candidate_runs: int = 1,
        candidate_min_passes: Optional[int] = None,
        candidate_min_rate: Optional[float] = None,
        run_confidence: Optional[float] = None,
        holdout_runs: Optional[int] = None,
        holdout_minimum_rate: Optional[float] = None,
        holdout_confidence: float = 0.95,
        heartbeat: Optional[Heartbeat] = None,
        heartbeat_interval_seconds: Optional[float] = 30.0,
        heartbeat_attempt_interval: Optional[int] = 25,
    ) -> None:
        if jobs < 1:
            raise ValueError("jobs must be at least 1")
        if candidate_runs < 1:
            raise ValueError("candidate runs must be at least 1")
        required_candidate_passes = (
            candidate_runs if candidate_min_passes is None else candidate_min_passes
        )
        if required_candidate_passes < 1 or required_candidate_passes > candidate_runs:
            raise ValueError(
                "minimum candidate passes must be between 1 and candidate runs"
            )
        if (holdout_runs is None) != (holdout_minimum_rate is None):
            raise ValueError(
                "holdout runs and minimum holdout rate must be configured together"
            )
        if holdout_runs is not None and (
            isinstance(holdout_runs, bool)
            or not isinstance(holdout_runs, int)
            or holdout_runs < 1
        ):
            raise ValueError("holdout runs must be at least 1")
        for value, label in (
            (holdout_minimum_rate, "minimum holdout rate"),
            (holdout_confidence, "holdout confidence"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) <= 0.0
                or float(value) >= 1.0
            ):
                raise ValueError("%s must be in (0, 1)" % label)
        self.source = source
        self.oracle = oracle
        self.stats = stats
        self.jobs = jobs
        self.candidate_runs = candidate_runs
        self.candidate_min_passes = required_candidate_passes
        self.candidate_min_rate = (
            oracle.min_candidate_rate
            if candidate_min_rate is None
            else candidate_min_rate
        )
        if run_confidence is not None and self.candidate_min_rate is None:
            raise ValueError("run confidence requires a minimum candidate rate")
        if run_confidence is not None:
            candidate_family_confidence(
                oracle.confidence,
                run_confidence,
                1,
            )
        self.run_confidence = run_confidence
        self.holdout_runs = holdout_runs
        self.holdout_minimum_rate = holdout_minimum_rate
        self.holdout_confidence = holdout_confidence
        self.execution_working_directory_basename = (
            _validate_execution_working_directory_basename(
                execution_working_directory_basename
            )
        )
        # Reusing one sampled result would defeat the purpose of repeated trials.
        self.cache_enabled = cache_enabled and candidate_runs == 1
        self.stats.jobs = jobs
        self.stats.cache_enabled = self.cache_enabled
        self.stats.candidate_runs = candidate_runs
        self.stats.candidate_min_passes = required_candidate_passes
        self.stats.min_baseline_rate = oracle.min_baseline_rate
        self.stats.min_candidate_rate = self.candidate_min_rate
        self.stats.confidence = oracle.confidence
        self.stats.run_confidence = run_confidence
        self.stats.candidate_family_control_policy = (
            CANDIDATE_FAMILY_CONTROL_POLICY
            if run_confidence is not None
            else None
        )
        self.progress = progress or (lambda message: None)
        if heartbeat is not None and not callable(heartbeat):
            raise ValueError("heartbeat must be callable")
        if heartbeat_interval_seconds is not None:
            if isinstance(heartbeat_interval_seconds, bool):
                raise ValueError(
                    "heartbeat interval must be a positive number of seconds"
                )
            try:
                heartbeat_interval_seconds = float(heartbeat_interval_seconds)
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError(
                    "heartbeat interval must be a positive number of seconds"
                ) from exc
            if (
                not math.isfinite(heartbeat_interval_seconds)
                or heartbeat_interval_seconds <= 0.0
            ):
                raise ValueError(
                    "heartbeat interval must be a positive number of seconds"
                )
        if heartbeat_attempt_interval is not None and (
            isinstance(heartbeat_attempt_interval, bool)
            or not isinstance(heartbeat_attempt_interval, int)
            or heartbeat_attempt_interval < 1
        ):
            raise ValueError(
                "heartbeat attempt interval must be a positive integer"
            )
        self.heartbeat = heartbeat
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.heartbeat_attempt_interval = heartbeat_attempt_interval
        self._heartbeat_started_at: Optional[float] = None
        self._heartbeat_last_emitted_at: Optional[float] = None
        self._heartbeat_last_completed_attempts = 0
        self._heartbeat_reduction_started = False
        self.ignore_paths = tuple(sorted(set(ignore_paths or ())))
        self.keep_paths = tuple(sorted(set(keep_paths or ())))
        # Rule-file order is meaningful because later negations can override
        # earlier entries.  Preserve the loader's deterministic order while
        # collapsing duplicate labels.
        self.gitignore_files = tuple(dict.fromkeys(gitignore_files or ()))
        self.gitignore_recursive = bool(gitignore_recursive)
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or max_attempts < 1
        ):
            raise ValueError("maximum attempts must be at least 1")
        if max_duration_seconds is not None and (
            isinstance(max_duration_seconds, bool)
            or not math.isfinite(float(max_duration_seconds))
            or float(max_duration_seconds) <= 0.0
        ):
            raise ValueError("maximum duration must be a positive number of seconds")
        self.max_attempts = max_attempts
        self.max_duration_seconds = (
            None if max_duration_seconds is None else float(max_duration_seconds)
        )
        self.ignores: IgnoreSet = IgnoreSet(
            DEFAULT_IGNORES,
            self.ignore_paths,
            gitignore_matcher,
            self.keep_paths,
        )
        self.ignores.update(ignores or ())
        self.stats.ignored_names = sorted(self.ignores)
        self.stats.ignored_paths = list(self.ignore_paths)
        self.stats.keep_paths = list(self.keep_paths)
        self.stats.gitignore_files = list(self.gitignore_files)
        self.stats.gitignore_recursive = self.gitignore_recursive
        self.stats.max_attempts = max_attempts
        self.stats.max_duration_seconds = self.max_duration_seconds
        self._temporary: Optional[tempfile.TemporaryDirectory] = None
        self._session_lock = None
        self.persistent_path = (
            Path(session_path).expanduser().resolve() if session_path is not None else None
        )
        self.resumed = resume
        self.identity = dict(identity or {})
        initial_source_fingerprint = (
            None
            if resume
            else _tree_digest(
                source,
                self.ignores,
                normalize_atimes=False,
            )
        )
        self._completed_phases: Set[str] = set()
        self.current_phase: Optional[str] = None
        self._active_phase_name: Optional[str] = None
        self._active_phase_started: Optional[float] = None
        self._active_phase_input_bytes: Optional[int] = None
        self.baseline: Optional[RunResult] = None
        self.final_validation_run: Optional[RunResult] = None
        self.holdout_certification = HoldoutCertification(
            status="not_started" if holdout_runs is not None else "not_requested",
            planned_runs=holdout_runs or 0,
            minimum_rate=holdout_minimum_rate,
            confidence=holdout_confidence if holdout_runs is not None else None,
        )
        self._holdout_recovery_pending = False
        self._keep_path_parts = tuple(
            PurePosixPath(path).parts for path in self.keep_paths
        )
        if self.persistent_path is not None:
            _prepare_persistent_path(self.persistent_path, resume)
            self._session_lock = _acquire_session_lock(self.persistent_path)
            try:
                self.root = self.persistent_path / "workspace"
                self.current = self.root / "current"
                if resume:
                    self._restore_persistent(oracle)
                else:
                    self._initialize_persistent(source)
            except BaseException:
                if not resume:
                    _cleanup_failed_persistent_initialization(self.persistent_path)
                _release_session_lock(self._session_lock)
                self._session_lock = None
                raise
        else:
            if resume:
                raise SessionError("--resume requires --session PATH")
            self._temporary = tempfile.TemporaryDirectory(
                prefix=".repomin-session-",
                dir=str(temporary_parent) if temporary_parent is not None else None,
            )
            self.root = Path(self._temporary.name)
            self.current = self.root / "current"
            try:
                _copy_repository(source, self.current, self.ignores)
            except BaseException:
                self.close()
                raise
        try:
            self._result_cache: Dict[str, RunResult] = {}
            self.last_candidate_decisions: Dict[int, bool] = {}
            if not resume:
                final_source_fingerprint = _tree_digest(
                    source,
                    self.ignores,
                    normalize_atimes=False,
                )
                copied_fingerprint = _tree_digest(self.current, self.ignores)
                if (
                    initial_source_fingerprint != final_source_fingerprint
                    or initial_source_fingerprint != copied_fingerprint
                ):
                    _remove_path_without_following(self.current, ignore_errors=True)
                    raise SessionError(
                        "source repository changed while its initial snapshot was copied"
                    )
                self._source_fingerprint = initial_source_fingerprint
            if self.persistent_path is not None:
                self.stats.session_path = str(self.persistent_path)
                self.stats.resumed = resume
            if self.persistent_path is not None and not resume:
                self._checkpoint(oracle)
            elif self.persistent_path is not None and self._holdout_recovery_pending:
                self._checkpoint(oracle)
        except BaseException:
            if self.persistent_path is not None and not resume:
                _cleanup_failed_persistent_initialization(self.persistent_path)
            self.close()
            raise
        self._reset_heartbeat_clock()

    def keeps(self, relative: Path) -> bool:
        """Return whether the file reducer must preserve ``relative``."""
        parts = relative.parts
        return any(
            # Keep the ancestor directories needed to reach a kept file, as
            # well as the kept path itself and descendants of kept directories.
            parts[: len(kept)] == kept or kept[: len(parts)] == parts
            for kept in self._keep_path_parts
        )

    def begin_reduction(self) -> None:
        """Start the wall-clock reduction budget if it is not already running."""
        if not self._heartbeat_reduction_started:
            self._reset_heartbeat_clock()
            self._heartbeat_reduction_started = True
        if self.max_duration_seconds is None:
            return
        if self.stats.reduction_started_at is None:
            self.stats.reduction_started_at = time.time()
            self._checkpoint()

    def reduction_budget_exhausted(self) -> bool:
        if self.max_duration_seconds is None:
            return False
        if self.stats.reduction_started_at is None:
            return False
        return time.time() - self.stats.reduction_started_at >= self.max_duration_seconds

    def close(self) -> None:
        try:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
        finally:
            _release_session_lock(self._session_lock)
            self._session_lock = None

    @property
    def completed_phases(self) -> Set[str]:
        return set(self._completed_phases)

    def phase_completed(self, phase: str) -> bool:
        return phase in self._completed_phases

    @property
    def final_candidate_confidence(self) -> float:
        if self.stats.events:
            confidence = self.stats.events[-1].candidate_confidence
            if confidence is not None:
                return confidence
        return self.oracle.confidence

    def mark_phase_completed(self, phase: str) -> None:
        self.current_phase = phase
        self._completed_phases.add(phase)
        self._checkpoint()

    def mark_completed(self) -> None:
        if self.persistent_path is None:
            return
        self._checkpoint(status="completed")

    def notify_heartbeat(
        self,
        phase: Optional[str] = None,
        *,
        force: bool = False,
    ) -> bool:
        """Emit one aggregate heartbeat when a configured threshold is due."""
        if self.heartbeat is None:
            return False
        if phase is not None and not isinstance(phase, str):
            raise ValueError("heartbeat phase must be text")
        now = time.monotonic()
        completed_attempts = self._completed_candidate_attempts()
        attempts_due = (
            self.heartbeat_attempt_interval is not None
            and completed_attempts - self._heartbeat_last_completed_attempts
            >= self.heartbeat_attempt_interval
        )
        interval_due = (
            self.heartbeat_interval_seconds is not None
            and self._heartbeat_last_emitted_at is not None
            and now - self._heartbeat_last_emitted_at
            >= self.heartbeat_interval_seconds
        )
        if not force and not attempts_due and not interval_due:
            return False

        phase_stats = tuple(self.stats.phase_stats.values())
        if force:
            reason = "phase"
        elif attempts_due:
            reason = "attempts"
        else:
            reason = "interval"
        started_at = self._heartbeat_started_at
        snapshot = HeartbeatSnapshot(
            phase=self.current_phase if phase is None else phase,
            reason=reason,
            elapsed_seconds=(
                0.0 if started_at is None else max(0.0, now - started_at)
            ),
            attempts=self.stats.attempts,
            completed_attempts=completed_attempts,
            accepted=self.stats.accepted,
            rejected=sum(item.rejected for item in phase_stats),
            superseded=sum(item.superseded for item in phase_stats),
            no_op=sum(item.no_op for item in phase_stats),
            aborted=sum(item.aborted for item in phase_stats),
            candidate_samples=self.stats.candidate_samples,
            candidate_passes=self.stats.candidate_passes,
            oracle_samples=sum(item.oracle_samples for item in phase_stats),
            cache_hits=self.stats.cache_hits,
        )
        self._heartbeat_last_emitted_at = now
        self._heartbeat_last_completed_attempts = completed_attempts
        self.heartbeat(snapshot)
        return True

    def _reset_heartbeat_clock(self) -> None:
        if self.heartbeat is None:
            return
        heartbeat_started_at = time.monotonic()
        self._heartbeat_started_at = heartbeat_started_at
        self._heartbeat_last_emitted_at = heartbeat_started_at
        self._heartbeat_last_completed_attempts = self._completed_candidate_attempts()

    def _completed_candidate_attempts(self) -> int:
        return sum(
            phase.accepted
            + phase.rejected
            + phase.superseded
            + phase.no_op
            + phase.aborted
            for phase in self.stats.phase_stats.values()
        )

    @contextmanager
    def measure_phase(self, phase: str) -> Iterator[None]:
        """Measure one active reducer pass without counting process downtime."""
        if self._active_phase_name is not None:
            raise RuntimeError("reduction phases must not be nested")
        phase_stats = self._phase_stats(phase)
        phase_stats.passes += 1
        self._active_phase_name = phase
        self._active_phase_started = time.monotonic()
        self._active_phase_input_bytes = _tree_file_bytes(self.current, self.ignores)
        self._checkpoint()
        try:
            yield
        except BaseException:
            phase_stats.aborted_passes += 1
            self.stats.phase_statistics_complete = False
            raise
        else:
            phase_stats.completed_passes += 1
        finally:
            assert self._active_phase_started is not None
            assert self._active_phase_input_bytes is not None
            phase_stats.wall_seconds += time.monotonic() - self._active_phase_started
            output_bytes = _tree_file_bytes(self.current, self.ignores)
            difference = self._active_phase_input_bytes - output_bytes
            if difference >= 0:
                phase_stats.bytes_removed += difference
            else:
                phase_stats.bytes_added += -difference
            self._active_phase_name = None
            self._active_phase_started = None
            self._active_phase_input_bytes = None
            self._checkpoint()

    def _phase_stats(self, phase: str) -> ReductionPhaseStats:
        phase_stats = self.stats.phase_stats.get(phase)
        if phase_stats is None:
            phase_stats = ReductionPhaseStats(phase)
            self.stats.phase_stats[phase] = phase_stats
        return phase_stats

    def try_mutation(self, phase: str, description: str, mutation: Mutation) -> bool:
        accepted = self.try_mutations(
            phase,
            [MutationCandidate(description=description, mutation=mutation)],
        )
        return accepted == 0

    def try_mutations(
        self,
        phase: str,
        candidates: Sequence[MutationCandidate],
        combine_accepted: Optional[CombineCandidates] = None,
    ) -> Optional[int]:
        self.current_phase = phase
        self.last_candidate_decisions = {}
        phase_stats = self._phase_stats(phase)
        if (
            self.max_attempts is not None
            and self.stats.attempts >= self.max_attempts
        ) or self.reduction_budget_exhausted():
            self.stats.budget_exhausted = True
            return None
        for window_start in range(0, len(candidates), self.jobs):
            if (
                self.max_attempts is not None
                and self.stats.attempts >= self.max_attempts
            ) or self.reduction_budget_exhausted():
                self.stats.budget_exhausted = True
                break
            current_digest = _tree_digest(self.current, self.ignores)
            window = candidates[window_start : window_start + self.jobs]
            prepared: List[_PreparedTrial] = []
            trial_paths: List[Path] = []
            pending_classification = set()
            try:
                for offset, candidate in enumerate(window):
                    trial = self._prepare_candidate(
                        window_start + offset,
                        candidate,
                        phase_stats,
                        current_digest,
                    )
                    if trial is None:
                        continue
                    prepared.append(trial)
                    trial_paths.append(trial.cleanup_path)
                    pending_classification.add(trial.attempt)

                if prepared and self.persistent_path is not None:
                    # A crash after this checkpoint burns every allocated family
                    # budget in the window and records its unfinished attempt as
                    # aborted. A later checkpoint replaces this conservative view.
                    phase_stats.aborted += len(prepared)
                    try:
                        self._checkpoint()
                    finally:
                        phase_stats.aborted -= len(prepared)
                results = self._run_prepared(prepared)
                selected = None
                selected_decision = None
                decisions: Dict[int, bool] = {}
                for trial in prepared:
                    outcome = results[trial.candidate_index]
                    accepted, decision = self._record_outcome(
                        trial,
                        outcome,
                        phase_stats,
                    )
                    decisions[trial.candidate_index] = accepted
                    self.last_candidate_decisions[trial.candidate_index] = accepted
                    if accepted and selected is None:
                        selected = trial
                        selected_decision = decision
                if selected is None:
                    phase_stats.rejected += len(prepared)
                    pending_classification.clear()
                    self.notify_heartbeat(phase)
                    continue
                assert selected_decision is not None
                return_index = selected.candidate_index

                passing_trials = [
                    trial
                    for trial in prepared
                    if decisions[trial.candidate_index]
                ]
                selected_outcome = results[selected.candidate_index]
                combined_selected = False
                if combine_accepted is not None and len(passing_trials) > 1:
                    combined = combine_accepted(
                        [trial.candidate for trial in passing_trials]
                    )
                    if combined is not None:
                        combined_trial = self._prepare_candidate(
                            -1,
                            combined,
                            phase_stats,
                            current_digest,
                        )
                        if combined_trial is not None:
                            trial_paths.append(combined_trial.cleanup_path)
                            pending_classification.add(combined_trial.attempt)
                            if self.persistent_path is not None:
                                phase_stats.aborted += len(pending_classification)
                                try:
                                    self._checkpoint()
                                finally:
                                    phase_stats.aborted -= len(pending_classification)
                            combined_outcome = self._run_prepared([combined_trial])[-1]
                            combined_accepted, combined_decision = self._record_outcome(
                                combined_trial,
                                combined_outcome,
                                phase_stats,
                            )
                            if combined_accepted:
                                for trial in prepared:
                                    if decisions[trial.candidate_index]:
                                        phase_stats.superseded += 1
                                    else:
                                        phase_stats.rejected += 1
                                    pending_classification.remove(trial.attempt)
                                selected = combined_trial
                                selected_decision = combined_decision
                                selected_outcome = combined_outcome
                                combined_selected = True
                            else:
                                phase_stats.rejected += 1
                                pending_classification.remove(combined_trial.attempt)

                if not combined_selected:
                    for trial in prepared:
                        if trial is selected:
                            continue
                        if decisions[trial.candidate_index]:
                            phase_stats.superseded += 1
                        else:
                            phase_stats.rejected += 1
                        pending_classification.remove(trial.attempt)

                accepted_samples = selected_outcome.samples
                next(
                    sample
                    for sample in accepted_samples
                    if self.oracle.accepts(sample)
                )
                started = time.monotonic()
                promoted = self.root / ("promoted-%06d" % selected.attempt)
                _copy_repository(self.current, promoted, self.ignores)
                if not _apply_candidate_mutation(
                    promoted,
                    selected.candidate,
                    self.ignores,
                ):
                    shutil.rmtree(promoted)
                    raise RuntimeError("accepted mutation could not be reapplied")
                if _tree_digest(promoted) != selected.digest:
                    shutil.rmtree(promoted)
                    raise RuntimeError(
                        "accepted mutation produced a different tree when reapplied"
                    )
                previous = self.root / ("previous-%06d" % selected.attempt)
                self.current.rename(previous)
                try:
                    promoted.rename(self.current)
                except BaseException:
                    if self.current.exists():
                        shutil.rmtree(self.current)
                    previous.rename(self.current)
                    raise
                if self.persistent_path is None:
                    shutil.rmtree(previous)
                duration = sum(
                    sample.duration_seconds for sample in accepted_samples
                ) + (time.monotonic() - started)
                self.stats.accepted += 1
                phase_stats.accepted += 1
                pending_classification.remove(selected.attempt)
                self.stats.events.append(
                    ReductionEvent(
                        phase,
                        selected.candidate.description,
                        duration,
                        oracle_runs=selected_decision[0],
                        oracle_passes=selected_decision[1],
                        oracle_rate=selected_decision[2],
                        oracle_lower_bound=selected_decision[3],
                        oracle_anytime_lower_bound=selected_decision[4],
                        oracle_early_acceptance=selected_decision[5],
                        candidate_family_index=selected.candidate_family_index,
                        candidate_confidence=(
                            selected.candidate_confidence
                            if selected.candidate_family_index is not None
                            else None
                        ),
                        candidate_alpha=selected.candidate_alpha,
                    )
                )
                self.progress("accepted: %s" % selected.candidate.description)
                self._checkpoint()
                self.notify_heartbeat(phase)
                if self.persistent_path is not None and previous.exists():
                    shutil.rmtree(previous)
                return return_index
            except BaseException:
                phase_stats.aborted += len(pending_classification)
                raise
            finally:
                _cleanup_tool_owned_paths(
                    trial_paths,
                    "candidate command working directories",
                )
                if self.persistent_path is not None:
                    self._checkpoint()
        return None

    def _prepare_candidate(
        self,
        candidate_index: int,
        candidate: MutationCandidate,
        phase_stats: ReductionPhaseStats,
        current_digest: str,
    ) -> Optional[_PreparedTrial]:
        self.stats.attempts += 1
        phase_stats.attempts += 1
        attempt = self.stats.attempts
        cleanup_path = self.root / ("trial-%06d" % attempt)
        path = (
            cleanup_path / self.execution_working_directory_basename
            if self.execution_working_directory_basename is not None
            else cleanup_path
        )
        try:
            _copy_repository(self.current, path, self.ignores)
            changed = _apply_candidate_mutation(
                path,
                candidate,
                self.ignores,
            )
            digest = _tree_digest(path) if changed else None
        except BaseException:
            phase_stats.aborted += 1
            _cleanup_tool_owned_paths(
                [cleanup_path],
                "candidate preparation directory",
            )
            raise
        if not changed:
            phase_stats.no_op += 1
            _cleanup_tool_owned_paths(
                [cleanup_path],
                "candidate preparation directory",
            )
            return None
        assert digest is not None
        if digest == current_digest:
            phase_stats.no_op += 1
            _cleanup_tool_owned_paths(
                [cleanup_path],
                "candidate preparation directory",
            )
            return None
        family_index: Optional[int] = None
        candidate_confidence = self.oracle.confidence
        candidate_alpha: Optional[float] = None
        if self.run_confidence is not None:
            family_index = self.stats.candidate_family_count + 1
            try:
                allocated, exact_alpha = candidate_family_confidence(
                    self.oracle.confidence,
                    self.run_confidence,
                    family_index,
                )
            except ValueError as exc:
                phase_stats.aborted += 1
                _cleanup_tool_owned_paths(
                    [cleanup_path],
                    "candidate preparation directory",
                )
                raise SessionError(
                    "candidate family %d cannot receive a representable confidence "
                    "budget; lower --run-confidence" % family_index
                ) from exc
            candidate_confidence = allocated
            candidate_alpha = float(exact_alpha)
            self.stats.candidate_family_count = family_index
            self.stats.candidate_family_alpha_upper_bound = float(
                candidate_family_alpha_upper_bound(
                    self.run_confidence,
                    family_index,
                )
            )
            if not exact_binomial_rate_gate(
                self.candidate_runs,
                self.candidate_runs,
                self.candidate_min_rate,
                candidate_confidence,
            ):
                phase_stats.aborted += 1
                _cleanup_tool_owned_paths(
                    [cleanup_path],
                    "candidate preparation directory",
                )
                raise SessionError(
                    "candidate family %d is unattainable with %d candidate runs at "
                    "%.17g confidence; increase --candidate-runs, lower "
                    "--min-candidate-rate, or lower --run-confidence"
                    % (family_index, self.candidate_runs, candidate_confidence)
                )
        return _PreparedTrial(
            candidate_index=candidate_index,
            attempt=attempt,
            candidate=candidate,
            path=path,
            cleanup_path=cleanup_path,
            digest=digest,
            candidate_family_index=family_index,
            candidate_confidence=candidate_confidence,
            candidate_alpha=candidate_alpha,
        )

    def _record_outcome(
        self,
        trial: _PreparedTrial,
        outcome: _RepeatedTrialOutcome,
        phase_stats: ReductionPhaseStats,
    ) -> Tuple[bool, tuple]:
        samples = outcome.samples
        terminal_accepted, passing = self.oracle.accepts_repeated(
            samples,
            self.candidate_min_passes,
            minimum_rate=self.candidate_min_rate,
            confidence=trial.candidate_confidence,
        )
        if outcome.stop_reason == "early_accept":
            accepted = True
        elif outcome.stop_reason == "early_reject":
            accepted = False
        else:
            accepted = terminal_accepted
        self.stats.candidate_samples += len(samples)
        self.stats.candidate_passes += passing
        phase_stats.oracle_sample_uses += len(samples)
        phase_stats.oracle_passing_sample_uses += passing
        if outcome.cache_hit:
            phase_stats.cache_hits += 1
        else:
            phase_stats.oracle_samples += len(samples)
            phase_stats.oracle_seconds += sum(
                sample.duration_seconds for sample in samples
            )
        if outcome.stop_reason == "early_reject":
            if accepted:
                raise RuntimeError("internal error: early rejection was accepted")
            self.stats.candidate_early_rejections += 1
            self.stats.candidate_samples_saved += self.candidate_runs - len(samples)
            phase_stats.samples_saved += self.candidate_runs - len(samples)
        elif outcome.stop_reason == "early_accept":
            if not terminal_accepted:
                raise RuntimeError(
                    "internal error: early acceptance lacks prefix evidence"
                )
            self.stats.candidate_early_acceptances += 1
            self.stats.candidate_samples_saved += self.candidate_runs - len(samples)
            phase_stats.samples_saved += self.candidate_runs - len(samples)
        decision = (
            len(samples),
            passing,
            self.oracle.candidate_rate,
            self.oracle.candidate_lower_bound,
            outcome.anytime_lower_bound,
            outcome.stop_reason == "early_accept",
        )
        return accepted, decision

    def _run_prepared(
        self, prepared: Sequence[_PreparedTrial]
    ) -> Dict[int, _RepeatedTrialOutcome]:
        results: Dict[int, _RepeatedTrialOutcome] = {}
        pending: List[_PreparedTrial] = []
        pending_by_digest: Dict[str, _PreparedTrial] = {}
        duplicate_trials: List[Tuple[_PreparedTrial, _PreparedTrial]] = []
        for trial in prepared:
            if self.cache_enabled and trial.digest in self._result_cache:
                results[trial.candidate_index] = _RepeatedTrialOutcome(
                    (self._result_cache[trial.digest],),
                    "full",
                    cache_hit=True,
                )
                self.stats.cache_hits += 1
            else:
                leader = (
                    pending_by_digest.get(trial.digest)
                    if self.cache_enabled
                    else None
                )
                if leader is not None:
                    duplicate_trials.append((trial, leader))
                    continue
                pending.append(trial)
                if self.cache_enabled:
                    pending_by_digest[trial.digest] = trial

        if len(pending) == 1:
            run_results = [self._run_trial_repeated(pending[0])]
        elif pending:
            executor = ThreadPoolExecutor(max_workers=min(self.jobs, len(pending)))
            try:
                run_results = list(
                    executor.map(
                        self._run_trial_repeated,
                        pending,
                    )
                )
            except BaseException:
                executor.shutdown(wait=False, cancel_futures=True)
                cancel = getattr(self.oracle.runner, "cancel", None)
                try:
                    if callable(cancel):
                        cancel()
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
        else:
            run_results = []

        for trial, outcome in zip(pending, run_results):
            results[trial.candidate_index] = outcome
            if self.cache_enabled:
                self._result_cache[trial.digest] = outcome.samples[0]
        for trial, leader in duplicate_trials:
            outcome = results[leader.candidate_index]
            results[trial.candidate_index] = _RepeatedTrialOutcome(
                outcome.samples,
                outcome.stop_reason,
                outcome.anytime_lower_bound,
                cache_hit=True,
            )
            self.stats.cache_hits += 1
        return results

    def _run_trial_repeated(self, trial: _PreparedTrial) -> _RepeatedTrialOutcome:
        if self.candidate_runs == 1:
            return _RepeatedTrialOutcome(
                (self.oracle.runner.run(trial.path),),
                "full",
            )
        results: List[RunResult] = []
        repeat_paths: List[Path] = []
        stop_reason = "full"
        lower_bound: Optional[float] = None
        try:
            for index in range(self.candidate_runs):
                prefix = "repeat-%06d-%03d" % (trial.attempt, index + 1)
                if self.execution_working_directory_basename is None:
                    repeat = self.root / prefix
                    repeat_paths.append(repeat)
                    _copy_repository(trial.path, repeat, self.ignores)
                else:
                    repeat_root, repeat = self._prepare_execution_copy(
                        trial.path,
                        prefix + "-",
                        self.execution_working_directory_basename,
                    )
                    repeat_paths.append(repeat_root)
                results.append(self.oracle.runner.run(repeat))
                if len(results) < self.candidate_runs:
                    if self._candidate_cannot_recover(
                        results,
                        trial.candidate_confidence,
                    ):
                        stop_reason = "early_reject"
                        break
                    can_accept, lower_bound = self._candidate_can_accept(
                        results,
                        trial.candidate_confidence,
                    )
                    if can_accept:
                        stop_reason = "early_accept"
                        break
        finally:
            _cleanup_tool_owned_paths(
                repeat_paths,
                "repeated candidate command working directories",
            )
        return _RepeatedTrialOutcome(
            tuple(results),
            stop_reason,
            lower_bound if stop_reason == "early_accept" else None,
        )

    def _prepare_execution_copy(
        self,
        source: Path,
        prefix: str,
        basename: str,
    ) -> Tuple[Path, Path]:
        execution_root = Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.root)))
        destination = execution_root / basename
        try:
            _copy_repository(source, destination, self.ignores)
        except BaseException:
            _cleanup_tool_owned_paths(
                [execution_root],
                "command working directory",
            )
            raise
        return execution_root, destination

    def _candidate_can_accept(
        self,
        results: Sequence[RunResult],
        confidence: Optional[float] = None,
    ) -> Tuple[bool, Optional[float]]:
        """Return whether this prefix safely establishes candidate acceptance."""
        level = self.oracle.confidence if confidence is None else confidence
        passing = sum(1 for result in results if self.oracle.accepts(result))
        if passing < self.candidate_min_passes:
            return False, None
        if self.candidate_min_rate is None:
            return True, None

        lower = anytime_lower_bound(
            passing,
            len(results),
            level,
        )
        if lower < self.candidate_min_rate:
            return False, lower

        # Treat every unobserved suffix sample as a failure. If that fixed-N
        # exact gate passes, any ordinary pass/fail completion also passes, so
        # early acceptance cannot enlarge the terminal acceptance set.
        worst_terminal_passes = exact_binomial_rate_gate(
            passing,
            self.candidate_runs,
            self.candidate_min_rate,
            level,
        )
        return worst_terminal_passes, lower

    def _candidate_cannot_recover(
        self,
        results: Sequence[RunResult],
        confidence: Optional[float] = None,
    ) -> bool:
        """Return true when remaining planned samples cannot change rejection."""
        level = self.oracle.confidence if confidence is None else confidence
        if any(result.timed_out or result.resource_exhausted for result in results):
            return True
        passing = sum(1 for result in results if self.oracle.accepts(result))
        remaining = self.candidate_runs - len(results)
        if passing + remaining < self.candidate_min_passes:
            return True
        if self.candidate_min_rate is None:
            return False
        best_possible = exact_binomial_rate_gate(
            passing + remaining,
            self.candidate_runs,
            self.candidate_min_rate,
            level,
        )
        return not best_possible

    def verify_baseline(
        self,
        repeat: int,
        minimum_passes: Optional[int] = None,
        minimum_rate: Optional[float] = None,
    ) -> RunResult:
        validation_root = (
            Path(tempfile.mkdtemp(prefix="baseline-", dir=str(self.root)))
            if self.execution_working_directory_basename is None
            else None
        )
        validation_roots: List[Path] = []
        attempt = 0

        def prepare() -> Path:
            nonlocal attempt
            attempt += 1
            if self.execution_working_directory_basename is None:
                assert validation_root is not None
                validation = validation_root / ("repository-%04d" % attempt)
                _copy_repository(self.current, validation, self.ignores)
            else:
                execution_root, validation = self._prepare_execution_copy(
                    self.current,
                    "baseline-%04d-" % attempt,
                    self.execution_working_directory_basename,
                )
                validation_roots.append(execution_root)
            return validation

        try:
            result = self.oracle.verify_baseline(
                self.current,
                repeat,
                minimum_passes=minimum_passes,
                minimum_rate=minimum_rate,
                prepare=prepare,
            )
            self.baseline = result
            self.stats.baseline_runs = self.oracle.baseline_runs
            self.stats.baseline_passes = self.oracle.baseline_passes
            self.stats.baseline_rate = self.oracle.baseline_rate
            self.stats.baseline_lower_bound = self.oracle.baseline_lower_bound
            self.stats.baseline_rate_evidence_runs = (
                self.oracle.baseline_rate_evidence_runs
            )
            self.stats.baseline_rate_evidence_passes = (
                self.oracle.baseline_rate_evidence_passes
            )
            self.stats.baseline_exact_lower_bound = (
                self.oracle.baseline_exact_lower_bound
            )
            self.stats.baseline_exact_p_value = self.oracle.baseline_exact_p_value
            self.stats.baseline_exact_rate_gate_passed = (
                self.oracle.baseline_exact_rate_gate_passed
            )
            self._checkpoint()
            return result
        finally:
            cleanup_paths = []
            if validation_root is not None:
                cleanup_paths.append(validation_root)
            cleanup_paths.extend(validation_roots)
            _cleanup_tool_owned_paths(
                cleanup_paths,
                "baseline command working directories",
            )

    def run_current(self) -> RunResult:
        return self.run_current_repeated()[0]

    def run_current_repeated(self) -> Tuple[RunResult, ...]:
        self.clean_generated()
        validation_root = (
            Path(tempfile.mkdtemp(prefix="validation-", dir=str(self.root)))
            if self.execution_working_directory_basename is None
            else None
        )
        validation_roots: List[Path] = []
        results: List[RunResult] = []
        try:
            for index in range(self.candidate_runs):
                if self.execution_working_directory_basename is None:
                    assert validation_root is not None
                    validation = validation_root / (
                        "repository-%04d" % (index + 1)
                    )
                    _copy_repository(self.current, validation, self.ignores)
                else:
                    execution_root, validation = self._prepare_execution_copy(
                        self.current,
                        "validation-%04d-" % (index + 1),
                        self.execution_working_directory_basename,
                    )
                    validation_roots.append(execution_root)
                results.append(self.oracle.runner.run(validation))
            return tuple(results)
        finally:
            cleanup_paths = []
            if validation_root is not None:
                cleanup_paths.append(validation_root)
            cleanup_paths.extend(validation_roots)
            _cleanup_tool_owned_paths(
                cleanup_paths,
                "validation command working directories",
            )

    def record_final_validation(self, result: RunResult) -> None:
        """Persist the consistency sample that precedes a requested holdout."""
        if self.holdout_certification.status == "not_requested":
            return
        if self.holdout_certification.status != "not_started":
            raise SessionError("holdout certification has already started")
        self.final_validation_run = result
        self._checkpoint()

    def run_holdout_certification(self) -> HoldoutCertification:
        """Run one fixed-size, cache-free holdout attempt on the frozen tree."""
        certification = self.holdout_certification
        if certification.status == "not_requested":
            return certification
        if certification.status == "certified":
            self._verify_holdout_artifact()
            return certification
        if certification.status == "not_certified":
            raise HoldoutCertificationError(_holdout_failure_message(certification))
        if certification.status == "aborted":
            raise SessionError(
                "holdout certification attempt is aborted and cannot be retried%s"
                % (": " + certification.error if certification.error else "")
            )

        if certification.status == "not_started":
            if self.final_validation_run is None:
                raise SessionError(
                    "holdout certification requires a saved final consistency validation"
                )
            self.clean_generated()
            planned_runs = certification.planned_runs
            minimum_rate = certification.minimum_rate
            confidence = certification.confidence
            assert minimum_rate is not None
            assert confidence is not None
            required_passes = _minimum_holdout_passes(
                planned_runs,
                minimum_rate,
                confidence,
            )
            certification.attempt_id = str(uuid.uuid4())
            certification.required_passes = required_passes
            certification.artifact_fingerprint = _tree_digest(
                self.current, self.ignores
            )
            certification.oracle_identity_sha256 = _oracle_identity_digest(
                self.identity, self.oracle
            )
            certification.status = "planned"
            self._checkpoint()

        self._verify_holdout_artifact()
        if certification.status == "planned":
            certification.status = "running"
            self._checkpoint()
        if certification.status != "running":
            raise SessionError("session contains invalid holdout certification state")
        if certification.in_flight_index is not None:
            self._record_interrupted_holdout_sample(certification.in_flight_index)

        while certification.completed_runs < certification.planned_runs:
            index = certification.completed_runs + 1
            certification.in_flight_index = index
            self._checkpoint()
            validation_root: Optional[Path] = None
            try:
                validation_root = Path(
                    tempfile.mkdtemp(
                        prefix="holdout-%04d-" % index,
                        dir=str(self.root),
                    )
                )
                validation = validation_root / (
                    self.execution_working_directory_basename or "repository"
                )
                _copy_repository(self.current, validation, self.ignores)
                result = self.oracle.runner.run(validation)
            except KeyboardInterrupt:
                self._record_interrupted_holdout_sample(index)
                raise
            except RunnerError as exc:
                self._abort_holdout("runner error: %s" % exc)
                raise
            except OSError as exc:
                self._abort_holdout("could not prepare a fresh sample: %s" % exc)
                raise SessionError(
                    "holdout certification could not prepare a fresh sample: %s" % exc
                ) from exc
            finally:
                if validation_root is not None:
                    _cleanup_tool_owned_paths(
                        [validation_root],
                        "holdout command working directory",
                    )

            self._record_holdout_result(index, result)

        self._verify_holdout_artifact()
        minimum_rate = certification.minimum_rate
        confidence = certification.confidence
        assert minimum_rate is not None
        assert confidence is not None
        certification.observed_rate = (
            float(certification.passes) / certification.planned_runs
        )
        certification.exact_lower_bound = clopper_pearson_lower_bound(
            certification.passes,
            certification.planned_runs,
            confidence,
        )
        certification.exact_p_value = float(
            exact_binomial_upper_tail(
                certification.passes,
                certification.planned_runs,
                minimum_rate,
            )
        )
        certification.exact_rate_gate_passed = exact_binomial_rate_gate(
            certification.passes,
            certification.planned_runs,
            minimum_rate,
            confidence,
        )
        resource_veto = (
            certification.timed_out_runs > 0
            or certification.resource_exhausted_runs > 0
        )
        certification.status = (
            "certified"
            if certification.exact_rate_gate_passed and not resource_veto
            else "not_certified"
        )
        self._checkpoint()
        if certification.status != "certified":
            raise HoldoutCertificationError(_holdout_failure_message(certification))
        return certification

    def _record_holdout_result(self, index: int, result: RunResult) -> None:
        certification = self.holdout_certification
        if certification.in_flight_index != index:
            raise SessionError("session contains an inconsistent holdout sample index")
        accepted = self.oracle.accepts(result)
        if result.timed_out:
            outcome = "timed_out"
        elif result.resource_exhausted:
            outcome = "resource_exhausted"
        elif accepted:
            outcome = "passed"
        else:
            outcome = "failed"
        certification.samples.append(
            HoldoutSample(
                index=index,
                outcome=outcome,
                accepted=accepted,
                returncode=result.returncode,
                duration_seconds=result.duration_seconds,
                timed_out=result.timed_out,
                resource_exhausted=result.resource_exhausted,
                resource_reason=result.resource_reason,
                output_sha256=_run_observation_digest(result),
            )
        )
        certification.completed_runs += 1
        certification.passes += int(accepted)
        certification.timed_out_runs += int(result.timed_out)
        certification.resource_exhausted_runs += int(result.resource_exhausted)
        certification.in_flight_index = None
        if accepted and certification.representative_run is None:
            certification.representative_run = result
        self._checkpoint()

    def _record_interrupted_holdout_sample(self, index: int) -> None:
        certification = self.holdout_certification
        _append_interrupted_holdout_sample(certification, index)
        self._checkpoint()

    def _abort_holdout(self, reason: str) -> None:
        certification = self.holdout_certification
        certification.status = "aborted"
        certification.error = reason
        certification.in_flight_index = None
        self._checkpoint()

    def _verify_holdout_artifact(self) -> None:
        expected = self.holdout_certification.artifact_fingerprint
        if expected is None or _tree_digest(self.current, self.ignores) != expected:
            self._abort_holdout("frozen artifact fingerprint changed")
            raise SessionError(
                "holdout certification artifact changed after the attempt was planned"
            )

    def clean_generated(self) -> None:
        _remove_ignored(self.current, self.ignores)

    def export(self, output: Path) -> None:
        certification = self.holdout_certification
        if certification.status not in {"not_requested", "certified"}:
            raise SessionError(
                "cannot export before the requested holdout certification succeeds"
            )
        expected_fingerprint = _tree_digest(self.current, self.ignores)
        if certification.status == "certified":
            certified_fingerprint = certification.artifact_fingerprint
            if (
                certified_fingerprint is None
                or expected_fingerprint != certified_fingerprint
            ):
                self._abort_holdout("frozen artifact fingerprint changed")
                raise SessionError(
                    "holdout certification artifact changed after the attempt was planned"
                )

        if _path_exists_without_following(output):
            if certification.status == "certified":
                if _output_matches_fingerprint(output, expected_fingerprint):
                    return
                raise SessionError(
                    "existing output differs from the certified artifact: %s" % output
                )
            raise FileExistsError("output already exists: %s" % output)

        staging_root: Optional[Path] = None
        try:
            staging_root = Path(
                tempfile.mkdtemp(prefix=".repomin-export-", dir=str(output.parent))
            )
            staged_output = staging_root / "payload"
            _copy_repository(self.current, staged_output, set())
            if _tree_digest(staged_output, set()) != expected_fingerprint:
                if certification.status == "certified":
                    self._abort_holdout("exported payload fingerprint changed")
                    detail = "certified artifact"
                else:
                    detail = "oracle-validated artifact"
                raise SessionError(
                    "staged export differs from the %s" % detail
                )

            try:
                _rename_directory_no_replace(staged_output, output)
            except OSError as exc:
                if _path_exists_without_following(output):
                    if (
                        certification.status == "certified"
                        and _output_matches_fingerprint(
                            output, expected_fingerprint
                        )
                    ):
                        return
                    raise FileExistsError(
                        errno.EEXIST,
                        "output already exists: %s" % output,
                        str(output),
                    ) from exc
                raise
        finally:
            if staging_root is not None:
                export_error = sys.exc_info()[1]
                try:
                    _remove_tool_owned_path_without_following(staging_root)
                except Exception as cleanup_error:
                    message = (
                        "could not remove export staging directory: %s"
                        % staging_root
                    )
                    if export_error is not None:
                        message += " (the export also failed with %s: %s)" % (
                            type(export_error).__name__,
                            export_error,
                        )
                    raise SessionError(message) from cleanup_error

    def _initialize_persistent(self, source: Path) -> None:
        assert self.persistent_path is not None
        if self.persistent_path.exists():
            if not self.persistent_path.is_dir():
                raise SessionError("session path is not a directory: %s" % self.persistent_path)
            if any(
                path.name != ".lock" for path in self.persistent_path.iterdir()
            ):
                raise SessionError(
                    "session path already exists; use --resume to continue it: %s"
                    % self.persistent_path
                )
        else:
            self.persistent_path.mkdir(parents=True)
        self.root.mkdir(parents=True, exist_ok=True)
        _copy_repository(source, self.current, self.ignores)

    def _restore_persistent(self, oracle: FailureOracle) -> None:
        assert self.persistent_path is not None
        state_path = self.persistent_path / "state.json"
        if not state_path.is_file():
            raise SessionError("session state is missing: %s" % state_path)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SessionError("could not read session state: %s" % state_path) from exc
        schema_version = state.get("schema_version")
        if schema_version in {1, 2}:
            raise SessionError(
                "legacy session state uses an incompatible tree fingerprint "
                "policy; start a new session instead"
            )
        if schema_version != 3:
            raise SessionError("unsupported session state schema")
        if state.get("tree_fingerprint_policy") != TREE_FINGERPRINT_POLICY:
            raise SessionError(
                "session tree fingerprint policy is missing or incompatible; "
                "start a new session instead"
            )
        _recover_promotion(
            self.root,
            state.get("current_fingerprint"),
            self.ignores,
        )
        if not self.current.is_dir():
            raise SessionError("session current state is missing: %s" % self.current)
        _cleanup_transient_workspace(self.root)
        expected_source = state.get("source_fingerprint")
        actual_source = _tree_digest(
            self.source,
            self.ignores,
            normalize_atimes=False,
        )
        if expected_source != actual_source:
            raise SessionError(
                "session source fingerprint changed; start a new session instead"
            )
        expected_identity = state.get("identity", {})
        if not _session_identities_match(expected_identity, self.identity):
            raise SessionError(
                "session configuration changed; command, failure matching, "
                "or reducer settings differ"
            )
        expected_current = state.get("current_fingerprint")
        if (
            expected_current is not None
            and _tree_digest(self.current, self.ignores) != expected_current
        ):
            raise SessionError(
                "session current state fingerprint changed; restore the "
                "checkpoint or start a new session"
            )
        try:
            stats_state = state["stats"]
            self.stats = _stats_from_dict(stats_state)
            self.stats.session_path = str(self.persistent_path)
            self.stats.resumed = True
            self._completed_phases = set(state.get("completed_phases", []))
            self.current_phase = state.get("current_phase")
            baseline = state.get("baseline")
            self.baseline = _run_result_from_dict(baseline) if baseline else None
            reconstructed_rate_evidence = oracle.restore_checkpoint_state(
                state.get("oracle", {})
            )
            if reconstructed_rate_evidence:
                if any(
                    key in stats_state for key in BASELINE_RATE_EVIDENCE_FIELDS
                ):
                    raise SessionError(
                        "session contains inconsistent baseline rate evidence"
                    )
                for key in BASELINE_RATE_EVIDENCE_FIELDS:
                    setattr(self.stats, key, getattr(oracle, key))
            _validate_restored_baseline_statistics(self.stats, oracle)
            _validate_restored_candidate_family_control(
                self.stats,
                oracle,
                self.run_confidence,
            )
            self.holdout_certification = _holdout_from_dict(
                state.get("holdout_certification")
            )
            final_validation = state.get("final_validation_run")
            self.final_validation_run = (
                _run_result_from_dict(final_validation)
                if final_validation is not None
                else None
            )
            _validate_restored_holdout_configuration(
                self.holdout_certification,
                self.holdout_runs,
                self.holdout_minimum_rate,
                self.holdout_confidence,
            )
            _validate_restored_holdout_evidence(
                self.holdout_certification,
                expected_identity,
                oracle,
                self.final_validation_run,
                self.current,
                self.ignores,
            )
            if self.holdout_certification.status not in {
                "not_requested",
                "not_started",
            }:
                self.holdout_certification.resumed = True
            in_flight = self.holdout_certification.in_flight_index
            if in_flight is not None:
                _append_interrupted_holdout_sample(
                    self.holdout_certification,
                    in_flight,
                )
                self._holdout_recovery_pending = True
            active_phase = state.get("active_phase")
            if active_phase is not None:
                self.stats.phase_statistics_complete = False
                phase_stats = self.stats.phase_stats.get(str(active_phase))
                if (
                    phase_stats is not None
                    and phase_stats.completed_passes + phase_stats.aborted_passes
                    < phase_stats.passes
                ):
                    phase_stats.aborted_passes += 1
        except SessionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError("session state is invalid: %s" % state_path) from exc
        self._source_fingerprint = expected_source

    def _checkpoint(self, oracle: Optional[FailureOracle] = None, status: str = "running") -> None:
        if self.persistent_path is None:
            return
        if oracle is None:
            # The oracle is only needed when the checkpoint carries a learned signature.
            oracle = getattr(self, "oracle", None)
        if oracle is None:
            return
        self.oracle = oracle
        state = {
            "schema_version": 3,
            "status": status,
            "tree_fingerprint_policy": TREE_FINGERPRINT_POLICY,
            "source_fingerprint": self._source_fingerprint,
            "current_fingerprint": _tree_digest(self.current, self.ignores),
            "identity": self.identity,
            "current_phase": self.current_phase,
            "active_phase": self._active_phase_name,
            "completed_phases": sorted(self._completed_phases),
            "stats": _stats_to_dict(self.stats),
            "baseline": _run_result_to_dict(self.baseline) if self.baseline else None,
            "final_validation_run": (
                _run_result_to_dict(self.final_validation_run)
                if self.final_validation_run is not None
                else None
            ),
            "holdout_certification": _holdout_to_dict(
                self.holdout_certification
            ),
            "oracle": oracle.checkpoint_state(),
        }
        state_path = self.persistent_path / "state.json"
        temporary = self.persistent_path / (".state-%d.tmp" % os.getpid())
        serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
        _fsync_directory(self.persistent_path)


def _prepare_persistent_path(path: Path, resume: bool) -> None:
    if path.exists() and not path.is_dir():
        raise SessionError("session path is not a directory: %s" % path)
    if resume and not path.is_dir():
        raise SessionError("session path does not exist: %s" % path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SessionError("could not create session path: %s" % path) from exc


def _acquire_session_lock(path: Path):
    lock_path = path / ".lock"
    try:
        stream = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError as exc:
        try:
            stream.close()
        except (OSError, UnboundLocalError):
            pass
        raise SessionError(
            "session is already in use by another ReproMin process: %s" % path
        ) from exc


def _release_session_lock(stream) -> None:
    if stream is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        stream.close()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_repository(source: Path, destination: Path, ignores: Set[str]) -> None:
    source_root = source.resolve()

    def ignore_names(directory: str, names: list) -> Set[str]:
        directory_path = Path(directory).resolve()
        ignored = set()
        for name in names:
            candidate = directory_path / name
            relative = candidate.relative_to(source_root)
            try:
                is_directory = stat.S_ISDIR(candidate.lstat().st_mode)
            except OSError:
                is_directory = None
            if _is_ignored(relative, ignores, is_directory=is_directory):
                ignored.add(name)
        return ignored

    _validate_repository_entries(source, ignores)
    try:
        shutil.copytree(source, destination, symlinks=True, ignore=ignore_names)
        _validate_repository_entries(destination, set())
        _normalize_tree_atimes(destination)
    except FileExistsError:
        # A caller- or race-created root is not tool-owned and must not be removed.
        raise
    except BaseException:
        _cleanup_tool_owned_paths(
            [destination],
            "incomplete repository copy",
        )
        raise


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _output_matches_fingerprint(output: Path, expected: str) -> bool:
    try:
        mode = output.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode) and _tree_digest(output, set()) == expected


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a directory, failing if the destination is claimed."""
    if os.name == "nt":
        # Unlike POSIX rename(), Windows os.rename() fails when dst exists.
        os.rename(source, destination)
        return

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    if sys.platform == "linux":
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory publication is unavailable",
                str(destination),
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,  # AT_FDCWD
            encoded_source,
            -100,
            encoded_destination,
            1,  # RENAME_NOREPLACE
        )
    elif sys.platform == "darwin":
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(
            encoded_source,
            encoded_destination,
            0x00000004,  # RENAME_EXCL
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory publication is unsupported on %s"
            % sys.platform,
            str(destination),
        )

    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _is_ignored(
    relative: Path,
    ignores: Set[str],
    is_directory: Optional[bool] = None,
) -> bool:
    matcher = getattr(ignores, "matches", None)
    if callable(matcher):
        if is_directory is None:
            # The legacy protocol only accepted ``matches(path)``; retaining
            # that call shape also avoids probing it with an unsupported
            # keyword when no entry type is available.
            return bool(matcher(relative))
        try:
            parameters = inspect.signature(matcher).parameters.values()
        except (TypeError, ValueError):
            parameters = None
        if parameters is not None and not any(
            parameter.name == "is_directory"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ):
            # Legacy matcher protocol: do not probe it with a keyword that it
            # cannot accept, and therefore do not run it twice on failure.
            return bool(matcher(relative))
        try:
            return bool(matcher(relative, is_directory=is_directory))
        except TypeError as keyword_error:
            # A callable without an inspectable signature may still be a
            # legacy matcher. Retry only in that unknown-signature case; for a
            # known new-style matcher, preserve an implementation TypeError.
            if parameters is not None:
                raise
            try:
                return bool(matcher(relative))
            except TypeError:
                raise keyword_error
    return any(part in ignores for part in relative.parts)


def _validate_repository_entries(root: Path, ignores: Set[str]) -> None:
    try:
        root_status = root.lstat()
    except OSError as exc:
        raise SessionError("repository could not be inspected safely: %s" % root) from exc
    if stat.S_ISLNK(root_status.st_mode):
        raise SessionError("repository root must not be a symbolic link: %s" % root)
    if _is_reparse_point(root_status):
        raise SessionError("repository root must not be a reparse point: %s" % root)
    if not stat.S_ISDIR(root_status.st_mode):
        raise SessionError("repository root is not a directory: %s" % root)
    _reject_mutation_blocking_filesystem_flags(root_status, Path("."))

    canonical_root = root.resolve()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise SessionError(
                "repository directory could not be inspected safely: %s" % directory
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            try:
                is_directory = stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
            except OSError:
                is_directory = None
            if _is_ignored(relative, ignores, is_directory=is_directory):
                continue
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SessionError(
                    "repository entry changed while being inspected: %s" % path
                ) from exc
            mode = status.st_mode
            _reject_mutation_blocking_filesystem_flags(
                status,
                relative,
            )
            if stat.S_ISLNK(mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise SessionError(
                        "repository symbolic link could not be inspected: %s" % path
                    ) from exc
                if Path(target).is_absolute():
                    raise SessionError(
                        "repository contains an absolute symbolic link: %s -> %s"
                        % (path.relative_to(root), target)
                    )
                try:
                    resolved_target = (path.parent / target).resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    raise SessionError(
                        "repository symbolic link target is not safely resolvable: %s"
                        % path.relative_to(root)
                    ) from exc
                if not _path_is_within(resolved_target, canonical_root):
                    raise SessionError(
                        "repository symbolic link escapes the repository: %s -> %s"
                        % (path.relative_to(root), target)
                    )
            elif _is_reparse_point(status):
                raise SessionError(
                    "repository contains an unsupported reparse point: %s"
                    % path.relative_to(root)
                )
            elif stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                if status.st_nlink > 1:
                    raise SessionError(
                        "repository contains a hard-linked regular file "
                        "(link count %d): %s"
                        % (status.st_nlink, path.relative_to(root))
                    )
            elif not stat.S_ISREG(mode):
                raise SessionError(
                    "repository contains an unsupported special file (%s): %s"
                    % (_special_file_kind(mode), path.relative_to(root))
                )


def _reject_mutation_blocking_filesystem_flags(status, relative_path: Path) -> None:
    flags = getattr(status, "st_flags", 0)
    if not flags:
        return
    names = [
        name
        for name in _MUTATION_BLOCKING_FILESYSTEM_FLAG_NAMES
        if getattr(stat, name, 0) and flags & getattr(stat, name)
    ]
    if names:
        raise SessionError(
            "repository contains mutation-blocking filesystem flags (%s): %s"
            % (", ".join(names), relative_path)
        )


def _is_reparse_point(status) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes and reparse_flag and attributes & reparse_flag)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _special_file_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    return "unknown"


def _remove_path_without_following(path: Path, ignore_errors: bool = False) -> None:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        if not ignore_errors:
            raise


def _remove_tool_owned_path_without_following(path: Path) -> None:
    """Remove a private work tree, recovering owner permissions as needed."""
    pending = [path]
    while pending:
        entry = pending.pop()
        try:
            status = entry.lstat()
        except FileNotFoundError:
            continue

        mode = status.st_mode
        is_symlink = stat.S_ISLNK(mode)
        is_reparse_point = _is_reparse_point(status)
        is_directory = stat.S_ISDIR(mode) and not is_reparse_point
        entry_flags = getattr(status, "st_flags", 0)
        may_change_inode_metadata = is_directory or (
            not is_reparse_point and getattr(status, "st_nlink", 0) == 1
        )
        if entry_flags and may_change_inode_metadata:
            chflags = getattr(os, "chflags", None)
            lchflags = getattr(os, "lchflags", None)
            try:
                if callable(chflags) and chflags in os.supports_follow_symlinks:
                    chflags(entry, 0, follow_symlinks=False)
                elif callable(lchflags):
                    lchflags(entry, 0)
            except FileNotFoundError:
                continue

        if is_symlink or is_reparse_point:
            continue

        if is_directory:
            required_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        elif os.name == "nt" and stat.S_ISREG(mode) and status.st_nlink == 1:
            # Windows maps the read-only file attribute to owner-write mode.
            # Refuse to chmod hard-linked files because their other names may
            # live outside this private staging tree.
            required_mode = stat.S_IWUSR
        else:
            required_mode = 0
        permissions = stat.S_IMODE(mode)
        if required_mode and permissions & required_mode != required_mode:
            try:
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(
                        entry,
                        permissions | required_mode,
                        follow_symlinks=False,
                    )
                else:
                    # The entry was just checked with lstat. Tool-owned staging
                    # trees are not exposed to command execution or mutation.
                    os.chmod(entry, permissions | required_mode)
            except FileNotFoundError:
                continue

        if is_directory:
            try:
                with os.scandir(entry) as children:
                    pending.extend(Path(child.path) for child in children)
            except FileNotFoundError:
                continue

    _remove_path_without_following(path)


def _cleanup_tool_owned_paths(paths: Iterable[Path], description: str) -> None:
    operation_error = sys.exc_info()[1]
    failures = []
    for path in paths:
        try:
            _remove_tool_owned_path_without_following(path)
        except OSError as exc:
            failures.append((path, exc))
    if not failures:
        return

    failed_paths = ", ".join(str(path) for path, _error in failures)
    message = "could not safely remove %s: %s" % (description, failed_paths)
    if operation_error is not None:
        message += " (the operation also failed with %s: %s)" % (
            type(operation_error).__name__,
            operation_error,
        )
    raise SessionError(message) from failures[0][1]


def _validate_execution_working_directory_basename(
    basename: Optional[str],
) -> Optional[str]:
    if basename is None:
        return None
    parsed = Path(basename)
    if (
        not basename
        or basename in {".", ".."}
        or "\x00" in basename
        or parsed.is_absolute()
        or len(parsed.parts) != 1
        or parsed.name != basename
    ):
        raise ValueError(
            "execution working directory basename must be one ordinary path segment"
        )
    return basename


def _apply_candidate_mutation(
    root: Path,
    candidate: MutationCandidate,
    ignores: Set[str],
) -> bool:
    saved_mtimes = _snapshot_tree_mtimes(root)
    changed = candidate.mutation(root)
    _validate_repository_entries(root, set())
    if not changed:
        return False
    _remove_ignored(root, ignores)
    _restore_tree_mtimes(root, saved_mtimes)
    _validate_repository_entries(root, set())
    return True


def _snapshot_tree_mtimes(root: Path) -> Dict[str, int]:
    return {
        _tree_relative_name(root, path): path.lstat().st_mtime_ns
        for path in [root, *root.rglob("*")]
    }


def _restore_tree_mtimes(root: Path, saved_mtimes: Dict[str, int]) -> None:
    default_mtime = saved_mtimes[""]
    entries = [root, *root.rglob("*")]
    entries.sort(
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for path in entries:
        mtime_ns = saved_mtimes.get(
            _tree_relative_name(root, path),
            default_mtime,
        )
        _set_tree_entry_times(path, mtime_ns)


def _normalize_tree_atimes(root: Path) -> None:
    entries = [root, *root.rglob("*")]
    entries.sort(
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for path in entries:
        _set_tree_entry_times(path, path.lstat().st_mtime_ns)


def _set_tree_entry_times(path: Path, mtime_ns: int) -> None:
    if path.is_symlink():
        if os.utime not in os.supports_follow_symlinks:
            return
        os.utime(
            path,
            ns=(mtime_ns, mtime_ns),
            follow_symlinks=False,
        )
        return
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _tree_relative_name(root: Path, path: Path) -> str:
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def _minimum_holdout_passes(
    runs: int,
    minimum_rate: float,
    confidence: float,
) -> int:
    if not exact_binomial_rate_gate(runs, runs, minimum_rate, confidence):
        best = clopper_pearson_lower_bound(runs, runs, confidence)
        raise ValueError(
            "minimum holdout rate %.4g is unattainable with %d holdout runs at "
            "%.4g confidence (best possible exact lower bound: %.4g); increase "
            "--holdout-runs or lower the rate/confidence"
            % (minimum_rate, runs, confidence, best)
        )
    low = 1
    high = runs
    while low < high:
        midpoint = low + (high - low) // 2
        if exact_binomial_rate_gate(
            midpoint,
            runs,
            minimum_rate,
            confidence,
        ):
            high = midpoint
        else:
            low = midpoint + 1
    return low


def _oracle_identity_digest(identity: dict, oracle: FailureOracle) -> str:
    oracle_state = oracle.checkpoint_state()
    payload = {
        "session_identity": identity,
        "failure_spec": {
            "match": oracle.spec.match,
            "exit_code": oracle.spec.exit_code,
            "java_exception": oracle.spec.java_exception,
            "python_exception": oracle.spec.python_exception,
            "process_failure": oracle.spec.process_failure,
        },
        "java_exception_signature": oracle_state.get("java_exception_signature"),
        "python_exception_signature": oracle_state.get("python_exception_signature"),
        "process_failure_signature": oracle_state.get("process_failure_signature"),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_observation_digest(result: RunResult) -> str:
    payload = json.dumps(
        _run_result_to_dict(result),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(payload).hexdigest()


def _append_interrupted_holdout_sample(
    certification: HoldoutCertification,
    index: int,
) -> None:
    if certification.status != "running" or certification.in_flight_index != index:
        raise SessionError("session contains an inconsistent in-flight holdout sample")
    if index != certification.completed_runs + 1:
        raise SessionError("session contains a non-sequential holdout sample")
    certification.samples.append(
        HoldoutSample(index=index, outcome="interrupted", accepted=False)
    )
    certification.completed_runs += 1
    certification.interrupted_runs += 1
    certification.in_flight_index = None


def _holdout_failure_message(certification: HoldoutCertification) -> str:
    return (
        "holdout certification failed: %d/%d samples passed; exact lower bound "
        "%.4f is below the %.4f target or a timeout/resource veto occurred"
        % (
            certification.passes,
            certification.planned_runs,
            certification.exact_lower_bound or 0.0,
            certification.minimum_rate or 0.0,
        )
    )


def _session_identities_match(saved: object, current: object) -> bool:
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return False
    normalized_saved = dict(saved)
    # Older sessions remain compatible with later options whose defaults do
    # not change the original reduction semantics.
    normalized_saved.setdefault("min_baseline_rate", None)
    normalized_saved.setdefault("min_candidate_rate", None)
    normalized_saved.setdefault("confidence", 0.95)
    normalized_saved.setdefault("run_confidence", None)
    normalized_saved.setdefault("candidate_family_control_policy", None)
    normalized_saved.setdefault("docker_image_id", None)
    normalized_saved.setdefault("java_analysis_classpath", [])
    normalized_saved.setdefault("holdout_runs", None)
    normalized_saved.setdefault("min_holdout_rate", None)
    normalized_saved.setdefault("holdout_confidence", 0.95)
    normalized_saved.setdefault("process_failure", False)
    normalized_saved.setdefault("ignored_names", sorted(DEFAULT_IGNORES))
    normalized_saved.setdefault("ignored_paths", [])
    normalized_saved.setdefault("gitignore_files", [])
    normalized_saved.setdefault("gitignore_sha256", None)
    normalized_saved.setdefault("gitignore_recursive", False)
    normalized_saved.setdefault("keep_paths", [])
    normalized_saved.setdefault("max_attempts", None)
    normalized_saved.setdefault("max_duration_seconds", None)
    normalized_saved.setdefault("semantic_reducer", None)
    normalized_saved.setdefault("semantic_endpoint", None)
    normalized_saved.setdefault("semantic_model", None)
    normalized_saved.setdefault("text_files", [])
    normalized_saved.setdefault(
        "environment_names", []
    )
    normalized_saved.setdefault(
        "environment_sha256",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    normalized_saved.setdefault(
        "holdout_certification_policy", HOLDOUT_CERTIFICATION_POLICY
    )
    normalized_current = dict(current)
    normalized_current.setdefault("min_baseline_rate", None)
    normalized_current.setdefault("min_candidate_rate", None)
    normalized_current.setdefault("confidence", 0.95)
    normalized_current.setdefault("run_confidence", None)
    normalized_current.setdefault("candidate_family_control_policy", None)
    normalized_current.setdefault("docker_image_id", None)
    normalized_current.setdefault("java_analysis_classpath", [])
    normalized_current.setdefault("holdout_runs", None)
    normalized_current.setdefault("min_holdout_rate", None)
    normalized_current.setdefault("holdout_confidence", 0.95)
    normalized_current.setdefault("process_failure", False)
    normalized_current.setdefault("ignored_names", sorted(DEFAULT_IGNORES))
    normalized_current.setdefault("ignored_paths", [])
    normalized_current.setdefault("gitignore_files", [])
    normalized_current.setdefault("gitignore_sha256", None)
    normalized_current.setdefault("gitignore_recursive", False)
    normalized_current.setdefault("keep_paths", [])
    normalized_current.setdefault("max_attempts", None)
    normalized_current.setdefault("max_duration_seconds", None)
    normalized_current.setdefault("semantic_reducer", None)
    normalized_current.setdefault("semantic_endpoint", None)
    normalized_current.setdefault("semantic_model", None)
    normalized_current.setdefault("text_files", [])
    normalized_current.setdefault("environment_names", [])
    normalized_current.setdefault(
        "environment_sha256",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    normalized_current.setdefault(
        "holdout_certification_policy", HOLDOUT_CERTIFICATION_POLICY
    )
    return normalized_saved == normalized_current


def _remove_ignored(root: Path, ignores: Set[str]) -> None:
    def ignored_path(path: Path) -> bool:
        try:
            status = path.lstat()
        except FileNotFoundError:
            # A command may have removed an ignored generated entry between
            # the directory walk and this cleanup pass.
            return False
        return _is_ignored(
            path.relative_to(root),
            ignores,
            is_directory=stat.S_ISDIR(status.st_mode),
        )

    candidates = sorted(
        (
            path
            for path in root.rglob("*")
            if ignored_path(path)
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in candidates:
        _remove_path_without_following(path)


def _cleanup_failed_persistent_initialization(session_path: Path) -> None:
    """Leave a failed new session with no resumable workspace or state."""
    _cleanup_tool_owned_paths(
        [session_path / "workspace"],
        "failed session workspace",
    )
    _remove_path_without_following(
        session_path / "state.json",
        ignore_errors=True,
    )
    try:
        temporary_states = list(session_path.glob(".state-*.tmp"))
    except OSError:
        return
    for temporary in temporary_states:
        _remove_path_without_following(temporary, ignore_errors=True)


def _cleanup_transient_workspace(root: Path) -> None:
    """Remove abandoned trial copies left by a hard process termination."""
    if not root.is_dir():
        return
    prefixes = (
        "trial-",
        "repeat-",
        "promoted-",
        "previous-",
        "baseline-",
        "validation-",
        "holdout-",
    )
    transient_paths = [
        path for path in root.iterdir() if path.name.startswith(prefixes)
    ]
    _cleanup_tool_owned_paths(
        transient_paths,
        "abandoned command working directories",
    )


def _recover_promotion(
    root: Path,
    expected_current: Optional[str] = None,
    ignores: Optional[Set[str]] = None,
) -> None:
    """Finish or roll back a directory swap interrupted by process death."""
    current = root / "current"
    previous = sorted(root.glob("previous-*"))
    promoted = sorted(root.glob("promoted-*"))
    candidates = previous + promoted
    matching = [
        path
        for path in candidates
        if expected_current is not None
        and path.is_dir()
        and _tree_digest(path, ignores) == expected_current
    ]
    if not current.exists():
        candidate = (
            matching[-1]
            if matching
            else (promoted[-1] if promoted else (previous[-1] if previous else None))
        )
        if candidate is not None:
            candidate.rename(current)
            promoted = [path for path in promoted if path != candidate]
            previous = [path for path in previous if path != candidate]
    elif expected_current is not None and _tree_digest(current, ignores) != expected_current:
        candidate = matching[-1] if matching else None
        if candidate is not None:
            shutil.rmtree(current)
            candidate.rename(current)
            promoted = [path for path in promoted if path != candidate]
            previous = [path for path in previous if path != candidate]
    for path in previous + promoted:
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _holdout_to_dict(certification: HoldoutCertification) -> dict:
    return {
        "schema_version": 1,
        "status": certification.status,
        "policy": certification.policy,
        "attempt_id": certification.attempt_id,
        "planned_runs": certification.planned_runs,
        "completed_runs": certification.completed_runs,
        "passes": certification.passes,
        "minimum_rate": certification.minimum_rate,
        "confidence": certification.confidence,
        "required_passes": certification.required_passes,
        "observed_rate": certification.observed_rate,
        "exact_lower_bound": certification.exact_lower_bound,
        "exact_p_value": certification.exact_p_value,
        "exact_rate_gate_passed": certification.exact_rate_gate_passed,
        "artifact_fingerprint": certification.artifact_fingerprint,
        "oracle_identity_sha256": certification.oracle_identity_sha256,
        "timed_out_runs": certification.timed_out_runs,
        "resource_exhausted_runs": certification.resource_exhausted_runs,
        "interrupted_runs": certification.interrupted_runs,
        "in_flight_index": certification.in_flight_index,
        "resumed": certification.resumed,
        "fresh_repository_copy_per_run": (
            certification.fresh_repository_copy_per_run
        ),
        "cache_used": certification.cache_used,
        "early_stopping": certification.early_stopping,
        "samples": [
            {
                "index": sample.index,
                "outcome": sample.outcome,
                "accepted": sample.accepted,
                "returncode": sample.returncode,
                "duration_seconds": sample.duration_seconds,
                "timed_out": sample.timed_out,
                "resource_exhausted": sample.resource_exhausted,
                "resource_reason": sample.resource_reason,
                "output_sha256": sample.output_sha256,
            }
            for sample in certification.samples
        ],
        "representative_run": (
            _run_result_to_dict(certification.representative_run)
            if certification.representative_run is not None
            else None
        ),
        "error": certification.error,
    }


def _holdout_from_dict(data: object) -> HoldoutCertification:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SessionError("session contains invalid holdout certification state")
    sample_data = data.get("samples", [])
    if not isinstance(sample_data, list):
        raise SessionError("session contains invalid holdout samples")
    samples = []
    for item in sample_data:
        if not isinstance(item, dict):
            raise SessionError("session contains invalid holdout samples")
        samples.append(
            HoldoutSample(
                index=int(item["index"]),
                outcome=str(item["outcome"]),
                accepted=bool(item["accepted"]),
                returncode=(
                    None if item.get("returncode") is None else int(item["returncode"])
                ),
                duration_seconds=(
                    None
                    if item.get("duration_seconds") is None
                    else float(item["duration_seconds"])
                ),
                timed_out=bool(item.get("timed_out", False)),
                resource_exhausted=bool(item.get("resource_exhausted", False)),
                resource_reason=item.get("resource_reason"),
                output_sha256=item.get("output_sha256"),
            )
        )
    representative = data.get("representative_run")
    certification = HoldoutCertification(
        status=str(data["status"]),
        policy=str(data["policy"]),
        attempt_id=data.get("attempt_id"),
        planned_runs=int(data.get("planned_runs", 0)),
        completed_runs=int(data.get("completed_runs", 0)),
        passes=int(data.get("passes", 0)),
        minimum_rate=_optional_float(data.get("minimum_rate")),
        confidence=_optional_float(data.get("confidence")),
        required_passes=(
            None
            if data.get("required_passes") is None
            else int(data["required_passes"])
        ),
        observed_rate=_optional_float(data.get("observed_rate")),
        exact_lower_bound=_optional_float(data.get("exact_lower_bound")),
        exact_p_value=_optional_float(data.get("exact_p_value")),
        exact_rate_gate_passed=(
            None
            if data.get("exact_rate_gate_passed") is None
            else bool(data["exact_rate_gate_passed"])
        ),
        artifact_fingerprint=data.get("artifact_fingerprint"),
        oracle_identity_sha256=data.get("oracle_identity_sha256"),
        timed_out_runs=int(data.get("timed_out_runs", 0)),
        resource_exhausted_runs=int(data.get("resource_exhausted_runs", 0)),
        interrupted_runs=int(data.get("interrupted_runs", 0)),
        in_flight_index=(
            None
            if data.get("in_flight_index") is None
            else int(data["in_flight_index"])
        ),
        resumed=bool(data.get("resumed", False)),
        fresh_repository_copy_per_run=bool(
            data.get("fresh_repository_copy_per_run", True)
        ),
        cache_used=bool(data.get("cache_used", False)),
        early_stopping=bool(data.get("early_stopping", False)),
        samples=samples,
        representative_run=(
            _run_result_from_dict(representative)
            if representative is not None
            else None
        ),
        error=data.get("error"),
    )
    _validate_holdout_state(certification)
    return certification


def _validate_holdout_state(certification: HoldoutCertification) -> None:
    allowed_statuses = {
        "not_requested",
        "not_started",
        "planned",
        "running",
        "certified",
        "not_certified",
        "aborted",
    }
    allowed_outcomes = {
        "passed",
        "failed",
        "timed_out",
        "resource_exhausted",
        "interrupted",
    }
    if (
        certification.status not in allowed_statuses
        or certification.policy != HOLDOUT_CERTIFICATION_POLICY
        or certification.planned_runs < 0
        or certification.completed_runs < 0
        or certification.completed_runs > certification.planned_runs
        or certification.passes < 0
        or certification.passes > certification.completed_runs
        or certification.timed_out_runs < 0
        or certification.resource_exhausted_runs < 0
        or certification.interrupted_runs < 0
        or not certification.fresh_repository_copy_per_run
        or certification.cache_used
        or certification.early_stopping
    ):
        raise SessionError("session contains invalid holdout certification state")
    if certification.status == "aborted":
        if not certification.error:
            raise SessionError("session contains an incomplete aborted holdout state")
    elif certification.error is not None:
        raise SessionError("session contains unexpected holdout error state")
    if [sample.index for sample in certification.samples] != list(
        range(1, len(certification.samples) + 1)
    ):
        raise SessionError("session contains non-sequential holdout samples")
    for sample in certification.samples:
        if sample.outcome not in allowed_outcomes:
            raise SessionError("session contains invalid holdout sample outcomes")
        _validate_holdout_sample(sample)
    if (
        certification.completed_runs != len(certification.samples)
        or certification.passes
        != sum(int(sample.accepted) for sample in certification.samples)
        or certification.timed_out_runs
        != sum(int(sample.timed_out) for sample in certification.samples)
        or certification.resource_exhausted_runs
        != sum(int(sample.resource_exhausted) for sample in certification.samples)
        or certification.interrupted_runs
        != sum(sample.outcome == "interrupted" for sample in certification.samples)
    ):
        raise SessionError("session contains inconsistent holdout sample totals")
    if certification.in_flight_index is not None and (
        certification.status != "running"
        or certification.in_flight_index != certification.completed_runs + 1
        or certification.in_flight_index > certification.planned_runs
    ):
        raise SessionError("session contains invalid in-flight holdout state")
    terminal = certification.status in {"certified", "not_certified"}
    if terminal and (
        certification.completed_runs != certification.planned_runs
        or certification.in_flight_index is not None
        or certification.exact_lower_bound is None
        or certification.exact_p_value is None
        or certification.exact_rate_gate_passed is None
    ):
        raise SessionError("session contains incomplete terminal holdout state")
    if certification.status in {
        "planned",
        "running",
        "certified",
        "not_certified",
        "aborted",
    }:
        if (
            certification.attempt_id is None
            or certification.artifact_fingerprint is None
            or certification.oracle_identity_sha256 is None
            or certification.required_passes is None
        ):
            raise SessionError("session contains an incomplete holdout plan")
    if certification.status == "certified" and (
        not certification.exact_rate_gate_passed
        or certification.timed_out_runs
        or certification.resource_exhausted_runs
        or certification.representative_run is None
    ):
        raise SessionError("session contains an invalid successful holdout state")


def _validate_holdout_sample(sample: HoldoutSample) -> None:
    if sample.outcome == "interrupted":
        if (
            sample.accepted
            or sample.returncode is not None
            or sample.duration_seconds is not None
            or sample.timed_out
            or sample.resource_exhausted
            or sample.resource_reason is not None
            or sample.output_sha256 is not None
        ):
            raise SessionError("session contains an invalid interrupted holdout sample")
        return

    if sample.timed_out:
        expected_outcome = "timed_out"
    elif sample.resource_exhausted:
        expected_outcome = "resource_exhausted"
    elif sample.accepted:
        expected_outcome = "passed"
    else:
        expected_outcome = "failed"
    if (
        sample.outcome != expected_outcome
        or ((sample.timed_out or sample.resource_exhausted) and sample.accepted)
    ):
        raise SessionError("session contains an inconsistent holdout sample outcome")
    if (
        sample.returncode is None
        or sample.duration_seconds is None
        or not math.isfinite(sample.duration_seconds)
        or sample.duration_seconds < 0.0
        or not _is_sha256_digest(sample.output_sha256)
    ):
        raise SessionError("session contains invalid holdout sample evidence")


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_restored_holdout_evidence(
    certification: HoldoutCertification,
    identity: dict,
    oracle: FailureOracle,
    final_validation_run: Optional[RunResult],
    current: Path,
    ignores: Set[str],
) -> None:
    terminal_values = (
        certification.observed_rate,
        certification.exact_lower_bound,
        certification.exact_p_value,
        certification.exact_rate_gate_passed,
    )
    if certification.status in {"not_requested", "not_started"}:
        unexpected_plan = (
            certification.attempt_id is not None
            or certification.required_passes is not None
            or certification.artifact_fingerprint is not None
            or certification.oracle_identity_sha256 is not None
        )
        invalid_disabled_plan = certification.status == "not_requested" and (
            certification.planned_runs != 0
            or certification.minimum_rate is not None
            or certification.confidence is not None
        )
        if (
            certification.completed_runs != 0
            or certification.samples
            or certification.representative_run is not None
            or any(value is not None for value in terminal_values)
            or unexpected_plan
            or invalid_disabled_plan
            or (
                final_validation_run is not None
                and (
                    certification.status != "not_started"
                    or not oracle.accepts(final_validation_run)
                )
            )
        ):
            raise SessionError("session contains premature holdout evidence")
        return

    minimum_rate = certification.minimum_rate
    confidence = certification.confidence
    if minimum_rate is None or confidence is None:
        raise SessionError("session contains an incomplete holdout plan")
    expected_required_passes = _minimum_holdout_passes(
        certification.planned_runs,
        minimum_rate,
        confidence,
    )
    if certification.required_passes != expected_required_passes:
        raise SessionError("session contains an inconsistent holdout pass threshold")
    if certification.oracle_identity_sha256 != _oracle_identity_digest(
        identity, oracle
    ):
        raise SessionError("session holdout oracle identity changed")
    if certification.artifact_fingerprint != _tree_digest(current, ignores):
        raise SessionError("session holdout artifact fingerprint changed")
    if final_validation_run is None or not oracle.accepts(final_validation_run):
        raise SessionError("session contains invalid final holdout validation evidence")

    representative = certification.representative_run
    if certification.passes == 0:
        if representative is not None:
            raise SessionError(
                "session contains unexpected representative holdout evidence"
            )
    else:
        accepted_digests = {
            sample.output_sha256
            for sample in certification.samples
            if sample.accepted
        }
        if (
            representative is None
            or not oracle.accepts(representative)
            or _run_observation_digest(representative) not in accepted_digests
        ):
            raise SessionError(
                "session contains invalid representative holdout evidence"
            )

    if certification.status == "planned" and certification.completed_runs != 0:
        raise SessionError("session contains completed samples in a planned holdout")
    if certification.status in {"planned", "running"}:
        if any(value is not None for value in terminal_values):
            raise SessionError("session contains premature terminal holdout statistics")
        return

    if certification.status == "aborted":
        if not any(value is not None for value in terminal_values):
            return
        if not all(value is not None for value in terminal_values):
            raise SessionError(
                "session contains incomplete terminal holdout statistics"
            )

    _validate_terminal_holdout_statistics(certification, minimum_rate, confidence)


def _validate_terminal_holdout_statistics(
    certification: HoldoutCertification,
    minimum_rate: float,
    confidence: float,
) -> None:
    if certification.completed_runs != certification.planned_runs:
        raise SessionError("session contains incomplete terminal holdout statistics")
    expected_observed_rate = float(certification.passes) / certification.planned_runs
    expected_lower_bound = clopper_pearson_lower_bound(
        certification.passes,
        certification.planned_runs,
        confidence,
    )
    expected_p_value = float(
        exact_binomial_upper_tail(
            certification.passes,
            certification.planned_runs,
            minimum_rate,
        )
    )
    expected_rate_gate = exact_binomial_rate_gate(
        certification.passes,
        certification.planned_runs,
        minimum_rate,
        confidence,
    )
    if (
        certification.observed_rate != expected_observed_rate
        or certification.exact_lower_bound != expected_lower_bound
        or certification.exact_p_value != expected_p_value
        or certification.exact_rate_gate_passed != expected_rate_gate
    ):
        raise SessionError("session contains inconsistent terminal holdout statistics")

    if certification.status in {"certified", "not_certified"}:
        resource_veto = (
            certification.timed_out_runs > 0
            or certification.resource_exhausted_runs > 0
        )
        expected_status = (
            "certified" if expected_rate_gate and not resource_veto else "not_certified"
        )
        if certification.status != expected_status:
            raise SessionError(
                "session contains an inconsistent terminal holdout status"
            )


def _validate_restored_holdout_configuration(
    certification: HoldoutCertification,
    runs: Optional[int],
    minimum_rate: Optional[float],
    confidence: float,
) -> None:
    if runs is None:
        if certification.status != "not_requested":
            raise SessionError("session holdout configuration changed")
        return
    if (
        certification.status == "not_requested"
        or certification.planned_runs != runs
        or certification.minimum_rate != minimum_rate
        or certification.confidence != confidence
    ):
        raise SessionError("session holdout configuration changed")


def _stats_to_dict(stats: ReductionStats) -> dict:
    return {
        "source_files": stats.source_files,
        "source_bytes": stats.source_bytes,
        "ignored_names": list(stats.ignored_names),
        "ignored_paths": list(stats.ignored_paths),
        "gitignore_files": list(stats.gitignore_files),
        "gitignore_sha256": stats.gitignore_sha256,
        "gitignore_recursive": stats.gitignore_recursive,
        "keep_paths": list(stats.keep_paths),
        "text_files": list(stats.text_files),
        "max_attempts": stats.max_attempts,
        "budget_exhausted": stats.budget_exhausted,
        "max_duration_seconds": stats.max_duration_seconds,
        "reduction_started_at": stats.reduction_started_at,
        "semantic_reducer": stats.semantic_reducer,
        "semantic_model": stats.semantic_model,
        "semantic_endpoint": stats.semantic_endpoint,
        "semantic_calls": stats.semantic_calls,
        "semantic_accepted": stats.semantic_accepted,
        "environment_names": list(stats.environment_names),
        "environment_sha256": stats.environment_sha256,
        "jobs": stats.jobs,
        "cache_enabled": stats.cache_enabled,
        "backend": stats.backend,
        "working_directory_policy": stats.working_directory_policy,
        "working_directory_basename": stats.working_directory_basename,
        "container_image": stats.container_image,
        "container_image_id": stats.container_image_id,
        "container_network": stats.container_network,
        "container_cpus": stats.container_cpus,
        "container_memory_bytes": stats.container_memory_bytes,
        "container_pids_limit": stats.container_pids_limit,
        "container_tmpfs_bytes": stats.container_tmpfs_bytes,
        "container_workspace_limit_bytes": stats.container_workspace_limit_bytes,
        "attempts": stats.attempts,
        "accepted": stats.accepted,
        "cache_hits": stats.cache_hits,
        "reduction_strategy": stats.reduction_strategy,
        "phase_statistics_complete": stats.phase_statistics_complete,
        "phase_stats": [
            _phase_stats_to_dict(phase_stats)
            for phase_stats in stats.phase_stats.values()
        ],
        "events": [
            {
                "phase": event.phase,
                "description": event.description,
                "duration_seconds": event.duration_seconds,
                "oracle_runs": event.oracle_runs,
                "oracle_passes": event.oracle_passes,
                "oracle_rate": event.oracle_rate,
                "oracle_lower_bound": event.oracle_lower_bound,
                "oracle_anytime_lower_bound": event.oracle_anytime_lower_bound,
                "oracle_early_acceptance": event.oracle_early_acceptance,
                "candidate_family_index": event.candidate_family_index,
                "candidate_confidence": event.candidate_confidence,
                "candidate_alpha": event.candidate_alpha,
            }
            for event in stats.events
        ],
        "output_files": stats.output_files,
        "output_bytes": stats.output_bytes,
        "session_path": stats.session_path,
        "resumed": stats.resumed,
        "baseline_runs": stats.baseline_runs,
        "baseline_passes": stats.baseline_passes,
        "min_baseline_rate": stats.min_baseline_rate,
        "min_candidate_rate": stats.min_candidate_rate,
        "confidence": stats.confidence,
        "candidate_sampling_policy": stats.candidate_sampling_policy,
        "run_confidence": stats.run_confidence,
        "candidate_family_control_policy": stats.candidate_family_control_policy,
        "candidate_family_count": stats.candidate_family_count,
        "candidate_family_alpha_upper_bound": (
            stats.candidate_family_alpha_upper_bound
        ),
        "baseline_rate": stats.baseline_rate,
        "baseline_lower_bound": stats.baseline_lower_bound,
        "baseline_rate_evidence_runs": stats.baseline_rate_evidence_runs,
        "baseline_rate_evidence_passes": stats.baseline_rate_evidence_passes,
        "baseline_exact_lower_bound": stats.baseline_exact_lower_bound,
        "baseline_exact_p_value": stats.baseline_exact_p_value,
        "baseline_exact_rate_gate_passed": stats.baseline_exact_rate_gate_passed,
        "candidate_runs": stats.candidate_runs,
        "candidate_min_passes": stats.candidate_min_passes,
        "candidate_samples": stats.candidate_samples,
        "candidate_passes": stats.candidate_passes,
        "candidate_early_rejections": stats.candidate_early_rejections,
        "candidate_early_acceptances": stats.candidate_early_acceptances,
        "candidate_samples_saved": stats.candidate_samples_saved,
        "final_runs": stats.final_runs,
        "final_passes": stats.final_passes,
        "final_rate": stats.final_rate,
        "final_lower_bound": stats.final_lower_bound,
    }


def _stats_from_dict(data: dict) -> ReductionStats:
    events = [
        ReductionEvent(
            str(event["phase"]),
            str(event["description"]),
            float(event["duration_seconds"]),
            int(event.get("oracle_runs", 0)),
            int(event.get("oracle_passes", 0)),
            _optional_float(event.get("oracle_rate")),
            _optional_float(event.get("oracle_lower_bound")),
            _optional_float(event.get("oracle_anytime_lower_bound")),
            bool(event.get("oracle_early_acceptance", False)),
            _optional_int(event.get("candidate_family_index")),
            _optional_float(event.get("candidate_confidence")),
            _optional_float(event.get("candidate_alpha")),
        )
        for event in data.get("events", [])
    ]
    values = {
        field.name: data[field.name]
        for field in fields(ReductionStats)
        if field.name not in {"events", "phase_stats", "session_path", "resumed"}
        and field.name in data
    }
    values.setdefault("ignored_names", sorted(DEFAULT_IGNORES))
    values.setdefault("ignored_paths", [])
    values.setdefault("gitignore_files", [])
    values.setdefault("gitignore_sha256", None)
    values.setdefault("gitignore_recursive", False)
    values.setdefault("keep_paths", [])
    values.setdefault("text_files", [])
    values.setdefault("max_attempts", None)
    values.setdefault("budget_exhausted", False)
    values.setdefault("max_duration_seconds", None)
    values.setdefault("reduction_started_at", None)
    values.setdefault("semantic_reducer", None)
    values.setdefault("semantic_model", None)
    values.setdefault("semantic_endpoint", None)
    values.setdefault("semantic_calls", 0)
    values.setdefault("semantic_accepted", 0)
    values.setdefault("environment_names", [])
    values.setdefault(
        "environment_sha256",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    values["events"] = events
    phase_data = data.get("phase_stats")
    if phase_data is None:
        values["phase_stats"] = {}
        values["phase_statistics_complete"] = bool(
            data.get("phase_statistics_complete", int(data.get("attempts", 0)) == 0)
        )
    else:
        values["phase_stats"] = _phase_stats_from_list(phase_data)
    values["session_path"] = data.get("session_path")
    values["resumed"] = bool(data.get("resumed", False))
    return ReductionStats(**values)


def _validate_restored_baseline_statistics(
    stats: ReductionStats,
    oracle: FailureOracle,
) -> None:
    if (
        stats.min_baseline_rate != oracle.min_baseline_rate
        or stats.confidence != oracle.confidence
    ):
        raise SessionError("session contains inconsistent baseline configuration")

    stats_values = (
        stats.baseline_runs,
        stats.baseline_passes,
        stats.baseline_rate,
        stats.baseline_lower_bound,
        stats.baseline_rate_evidence_runs,
        stats.baseline_rate_evidence_passes,
        stats.baseline_exact_lower_bound,
        stats.baseline_exact_p_value,
        stats.baseline_exact_rate_gate_passed,
    )
    oracle_values = (
        oracle.baseline_runs,
        oracle.baseline_passes,
        oracle.baseline_rate,
        oracle.baseline_lower_bound,
        oracle.baseline_rate_evidence_runs,
        oracle.baseline_rate_evidence_passes,
        oracle.baseline_exact_lower_bound,
        oracle.baseline_exact_p_value,
        oracle.baseline_exact_rate_gate_passed,
    )
    if stats_values != oracle_values:
        raise SessionError("session contains inconsistent baseline statistics")
    for count in (
        stats.baseline_rate_evidence_runs,
        stats.baseline_rate_evidence_passes,
    ):
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            raise SessionError("session contains invalid baseline rate evidence")
    gate = stats.baseline_exact_rate_gate_passed
    if gate is not None and not isinstance(gate, bool):
        raise SessionError("session contains invalid baseline rate evidence")


def _validate_restored_candidate_family_control(
    stats: ReductionStats,
    oracle: FailureOracle,
    run_confidence: Optional[float],
) -> None:
    if stats.run_confidence != run_confidence:
        raise SessionError("session contains inconsistent run confidence")
    expected_policy = (
        CANDIDATE_FAMILY_CONTROL_POLICY if run_confidence is not None else None
    )
    if stats.candidate_family_control_policy != expected_policy:
        raise SessionError("session contains inconsistent candidate family policy")
    count = stats.candidate_family_count
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count > stats.attempts
    ):
        raise SessionError("session contains invalid candidate family count")

    if run_confidence is None:
        if count != 0 or stats.candidate_family_alpha_upper_bound != 0.0:
            raise SessionError("session contains unexpected candidate family evidence")
        if any(
            event.candidate_family_index is not None
            or event.candidate_confidence is not None
            or event.candidate_alpha is not None
            for event in stats.events
        ):
            raise SessionError("session contains unexpected candidate family event")
        return

    expected_upper_bound = float(
        candidate_family_alpha_upper_bound(run_confidence, count)
    )
    if stats.candidate_family_alpha_upper_bound != expected_upper_bound:
        raise SessionError("session contains inconsistent candidate alpha spending")

    previous_index = 0
    for event in stats.events:
        index = event.candidate_family_index
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index <= previous_index
            or index > count
        ):
            raise SessionError("session contains invalid candidate family event index")
        confidence, exact_alpha = candidate_family_confidence(
            oracle.confidence,
            run_confidence,
            index,
        )
        if (
            event.candidate_confidence != confidence
            or event.candidate_alpha != float(exact_alpha)
        ):
            raise SessionError("session contains inconsistent candidate family event")
        previous_index = index


def _phase_stats_to_dict(stats: ReductionPhaseStats) -> dict:
    return {
        "phase": stats.phase,
        "passes": stats.passes,
        "completed_passes": stats.completed_passes,
        "aborted_passes": stats.aborted_passes,
        "wall_seconds": stats.wall_seconds,
        "bytes_removed": stats.bytes_removed,
        "bytes_added": stats.bytes_added,
        "attempts": stats.attempts,
        "no_op": stats.no_op,
        "rejected": stats.rejected,
        "accepted": stats.accepted,
        "superseded": stats.superseded,
        "aborted": stats.aborted,
        "oracle_sample_uses": stats.oracle_sample_uses,
        "oracle_samples": stats.oracle_samples,
        "oracle_passing_sample_uses": stats.oracle_passing_sample_uses,
        "oracle_seconds": stats.oracle_seconds,
        "cache_hits": stats.cache_hits,
        "samples_saved": stats.samples_saved,
    }


def _phase_stats_from_list(data: object) -> Dict[str, ReductionPhaseStats]:
    if not isinstance(data, list):
        raise SessionError("session contains invalid phase statistics")
    restored: Dict[str, ReductionPhaseStats] = {}
    integer_fields = (
        "passes",
        "completed_passes",
        "aborted_passes",
        "bytes_removed",
        "bytes_added",
        "attempts",
        "no_op",
        "rejected",
        "accepted",
        "superseded",
        "aborted",
        "oracle_sample_uses",
        "oracle_samples",
        "oracle_passing_sample_uses",
        "cache_hits",
        "samples_saved",
    )
    for item in data:
        if not isinstance(item, dict) or not item.get("phase"):
            raise SessionError("session contains invalid phase statistics")
        phase = str(item["phase"])
        if phase in restored:
            raise SessionError("session contains duplicate phase statistics")
        values = {name: int(item.get(name, 0)) for name in integer_fields}
        if any(value < 0 for value in values.values()):
            raise SessionError("session contains negative phase statistics")
        wall_seconds = float(item.get("wall_seconds", 0.0))
        oracle_seconds = float(item.get("oracle_seconds", 0.0))
        if (
            not math.isfinite(wall_seconds)
            or wall_seconds < 0.0
            or not math.isfinite(oracle_seconds)
            or oracle_seconds < 0.0
        ):
            raise SessionError("session contains invalid phase timing statistics")
        if values["completed_passes"] + values["aborted_passes"] > values["passes"]:
            raise SessionError("session contains inconsistent phase pass statistics")
        classified = sum(
            values[name]
            for name in ("no_op", "rejected", "accepted", "superseded", "aborted")
        )
        if classified != values["attempts"]:
            raise SessionError("session contains inconsistent phase attempt statistics")
        if (
            values["oracle_sample_uses"]
            != values["oracle_samples"] + values["cache_hits"]
            or values["oracle_passing_sample_uses"]
            > values["oracle_sample_uses"]
        ):
            raise SessionError("session contains inconsistent phase oracle statistics")
        restored[phase] = ReductionPhaseStats(
            phase=phase,
            wall_seconds=wall_seconds,
            oracle_seconds=oracle_seconds,
            **values,
        )
    return restored


def _optional_float(value: object) -> Optional[float]:
    return None if value is None else float(value)


def _optional_int(value: object) -> Optional[int]:
    return None if value is None else int(value)


def _run_result_to_dict(result: RunResult) -> dict:
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
        "diagnostics": result.diagnostics,
        "resource_exhausted": result.resource_exhausted,
        "resource_reason": result.resource_reason,
    }


def _run_result_from_dict(data: dict) -> RunResult:
    return RunResult(
        int(data["returncode"]),
        str(data["stdout"]),
        str(data["stderr"]),
        float(data["duration_seconds"]),
        bool(data.get("timed_out", False)),
        str(data.get("diagnostics", "")),
        bool(data.get("resource_exhausted", False)),
        data.get("resource_reason"),
    )


def _tree_file_bytes(root: Path, ignores: Set[str]) -> int:
    size = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if _is_ignored(
            relative,
            ignores,
            is_directory=stat.S_ISDIR(path.lstat().st_mode),
        ):
            continue
        if path.is_file() and not path.is_symlink():
            try:
                size += path.stat().st_size
            except OSError:
                continue
    return size


_TREE_DIGEST_DOMAIN = (
    b"repomin-tree-fingerprint\0"
    + TREE_FINGERPRINT_POLICY.encode("ascii")
    + b"\0"
)
_TREE_CONTENT_DIGEST_DOMAIN = (
    b"repomin-tree-content-fingerprint\0"
    + TREE_CONTENT_FINGERPRINT_POLICY.encode("ascii")
    + b"\0"
)


def _tree_digest(
    root: Path,
    ignores: Optional[Set[str]] = None,
    *,
    normalize_atimes: bool = True,
) -> str:
    primary_error: Optional[BaseException] = None
    try:
        _validate_repository_entries(root, ignores if ignores is not None else set())
        return _compute_tree_digest(root, ignores)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if normalize_atimes:
            try:
                _normalize_tree_atimes(root)
            except BaseException:
                if primary_error is None:
                    raise


def _tree_content_digest(
    root: Path,
    ignores: Optional[Set[str]] = None,
    *,
    normalize_atimes: bool = True,
) -> str:
    """Hash portable tree content while omitting transport-volatile metadata.

    The complete ``tree-sha256-v2`` fingerprint remains the authoritative
    local/session identity. This companion digest is intentionally limited to
    paths, entry kinds, file contents, and symlink targets so common archive
    transports that rewrite mtimes can still verify what was transferred.
    """
    primary_error: Optional[BaseException] = None
    try:
        _validate_repository_entries(root, ignores if ignores is not None else set())
        return _compute_tree_content_digest(root, ignores)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if normalize_atimes:
            try:
                _normalize_tree_atimes(root)
            except BaseException:
                if primary_error is None:
                    raise


def _compute_tree_digest(
    root: Path,
    ignores: Optional[Set[str]] = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(_TREE_DIGEST_DOMAIN)
    ignored = ignores if ignores is not None else set()
    paths = [
        (
            path,
            path.relative_to(root)
            .as_posix()
            .encode("utf-8", errors="surrogateescape"),
        )
        for path in root.rglob("*")
        if not _is_ignored(
            path.relative_to(root),
            ignored,
            is_directory=stat.S_ISDIR(path.lstat().st_mode),
        )
    ]
    paths.sort(key=lambda item: item[1])
    entries = [(root, b"")] + paths
    _update_tree_digest_field(
        digest,
        b"N",
        len(entries).to_bytes(8, byteorder="big", signed=False),
    )
    for path, relative in entries:
        stat_result = path.lstat()
        digest.update(b"E")
        _update_tree_digest_field(digest, b"P", relative)
        _update_tree_digest_field(
            digest,
            b"M",
            (stat_result.st_mode & 0o7777).to_bytes(
                2, byteorder="big", signed=False
            ),
        )
        _update_tree_digest_field(
            digest,
            b"Y",
            int(stat_result.st_mtime_ns).to_bytes(
                8, byteorder="big", signed=True
            ),
        )
        flags = getattr(stat_result, "st_flags", None)
        _update_tree_digest_field(
            digest,
            b"G",
            (
                b""
                if flags is None
                else int(flags).to_bytes(8, byteorder="big", signed=False)
            ),
        )
        file_attributes = getattr(stat_result, "st_file_attributes", None)
        _update_tree_digest_field(
            digest,
            b"W",
            (
                b""
                if file_attributes is None
                else int(file_attributes).to_bytes(
                    8, byteorder="big", signed=False
                )
            ),
        )
        kind = _tree_entry_kind(stat_result.st_mode)
        _update_tree_digest_field(digest, b"T", kind)
        xattrs = _tree_entry_xattrs(path)
        _update_tree_digest_field(
            digest,
            b"A",
            len(xattrs).to_bytes(8, byteorder="big", signed=False),
        )
        for name, value in xattrs:
            _update_tree_digest_field(digest, b"K", name)
            _update_tree_digest_field(digest, b"V", value)
        if kind == b"symlink":
            target = os.readlink(path).encode("utf-8", errors="surrogateescape")
            _update_tree_digest_field(digest, b"C", target)
        elif kind == b"regular-file":
            _update_tree_digest_file_content(digest, path, stat_result)
        elif kind in {b"block-device", b"character-device"}:
            _update_tree_digest_field(
                digest,
                b"C",
                int(stat_result.st_rdev).to_bytes(8, byteorder="big", signed=False),
            )
        else:
            _update_tree_digest_field(digest, b"C", b"")
        digest.update(b"Z")
    digest.update(b"X")
    return digest.hexdigest()


def _compute_tree_content_digest(
    root: Path,
    ignores: Optional[Set[str]] = None,
) -> str:
    """Compute the transport-friendly content fingerprint for one safe tree."""
    digest = hashlib.sha256()
    digest.update(_TREE_CONTENT_DIGEST_DOMAIN)
    ignored = ignores if ignores is not None else set()
    paths = [
        (
            path,
            path.relative_to(root)
            .as_posix()
            .encode("utf-8", errors="surrogateescape"),
        )
        for path in root.rglob("*")
        if not _is_ignored(
            path.relative_to(root),
            ignored,
            is_directory=stat.S_ISDIR(path.lstat().st_mode),
        )
    ]
    paths.sort(key=lambda item: item[1])
    entries = [(root, b"")] + paths
    _update_tree_digest_field(
        digest,
        b"N",
        len(entries).to_bytes(8, byteorder="big", signed=False),
    )
    for path, relative in entries:
        stat_result = path.lstat()
        digest.update(b"E")
        _update_tree_digest_field(digest, b"P", relative)
        kind = _tree_entry_kind(stat_result.st_mode)
        _update_tree_digest_field(digest, b"T", kind)
        if kind == b"symlink":
            target = os.readlink(path).encode("utf-8", errors="surrogateescape")
            _update_tree_digest_field(digest, b"C", target)
        elif kind == b"regular-file":
            _update_tree_digest_file_content(digest, path, stat_result)
        else:
            _update_tree_digest_field(digest, b"C", b"")
        digest.update(b"Z")
    digest.update(b"X")
    return digest.hexdigest()


def _update_tree_digest_field(digest, field_type: bytes, value: bytes) -> None:
    digest.update(field_type)
    digest.update(len(value).to_bytes(8, byteorder="big", signed=False))
    digest.update(value)


def _tree_entry_xattrs(path: Path) -> List[Tuple[bytes, bytes]]:
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    if not callable(listxattr) or not callable(getxattr):
        return []
    try:
        names = listxattr(path, follow_symlinks=False)
    except TypeError:
        if path.is_symlink():
            return []
        names = listxattr(path)
    except OSError as exc:
        unsupported = {
            value
            for value in (
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
                getattr(errno, "ENOSYS", None),
            )
            if value is not None
        }
        if exc.errno in unsupported:
            return []
        raise
    encoded_names = sorted(
        (
            name
            if isinstance(name, bytes)
            else name.encode("utf-8", errors="surrogateescape"),
            name,
        )
        for name in names
    )
    values = []
    for encoded_name, original_name in encoded_names:
        try:
            value = getxattr(
                path,
                original_name,
                follow_symlinks=False,
            )
        except TypeError:
            if path.is_symlink():
                return []
            value = getxattr(path, original_name)
        values.append((encoded_name, value))
    return values


def _update_tree_digest_file_content(
    digest,
    path: Path,
    expected_status,
) -> None:
    expected_size = int(expected_status.st_size)
    digest.update(b"C")
    digest.update(expected_size.to_bytes(8, byteorder="big", signed=False))
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    open_flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(str(path), open_flags)
    actual_size = 0
    try:
        opened_status = os.fstat(descriptor)
        if not _regular_file_snapshot_matches(opened_status, expected_status):
            raise OSError("regular file changed while fingerprinting: %s" % path)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            actual_size += len(chunk)
            digest.update(chunk)
        final_open_status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        final_path_status = path.lstat()
    except OSError as exc:
        raise OSError("regular file changed while fingerprinting: %s" % path) from exc
    if (
        actual_size != expected_size
        or not _regular_file_snapshot_matches(final_open_status, expected_status)
        or not _regular_file_snapshot_matches(final_path_status, expected_status)
    ):
        raise OSError("regular file changed while fingerprinting: %s" % path)


def _regular_file_snapshot_matches(actual, expected) -> bool:
    return (
        stat.S_ISREG(actual.st_mode)
        and actual.st_size == expected.st_size
        and actual.st_mtime_ns == expected.st_mtime_ns
        and actual.st_dev == expected.st_dev
        and actual.st_ino == expected.st_ino
        and actual.st_nlink == expected.st_nlink == 1
    )


def _tree_entry_kind(mode: int) -> bytes:
    if stat.S_ISLNK(mode):
        return b"symlink"
    if stat.S_ISDIR(mode):
        return b"directory"
    if stat.S_ISREG(mode):
        return b"regular-file"
    if stat.S_ISFIFO(mode):
        return b"fifo"
    if stat.S_ISSOCK(mode):
        return b"socket"
    if stat.S_ISBLK(mode):
        return b"block-device"
    if stat.S_ISCHR(mode):
        return b"character-device"
    return b"unknown-" + stat.S_IFMT(mode).to_bytes(
        4, byteorder="big", signed=False
    )

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict
from fractions import Fraction
from math import comb, isfinite, lgamma, log, log1p, nextafter, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Optional, Sequence, Tuple

from repomin.execution import (
    CommandRunner as CommandRunner,
    DockerRunner as DockerRunner,
    Runner,
    RunnerError as RunnerError,
)
from repomin.model import (
    FailureSpec,
    PYTHON_FRAME_POLICY,
    JavaExceptionSignature,
    ProcessFailureSignature,
    PythonExceptionSignature,
    RunResult,
)
from repomin.signature import (
    extract_process_failure,
    extract_run_java_exception,
    extract_run_python_exception,
    valid_recorded_process_failure_signature,
    valid_process_failure_signature,
)


class OracleError(RuntimeError):
    pass


BASELINE_RATE_EVIDENCE_FIELDS = (
    "baseline_rate_evidence_runs",
    "baseline_rate_evidence_passes",
    "baseline_exact_lower_bound",
    "baseline_exact_p_value",
    "baseline_exact_rate_gate_passed",
)


def _probability(value: Optional[float], name: str) -> Optional[float]:
    """Validate a user-facing probability and return it as a finite float."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("%s must be a number" % name)
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % name) from exc
    if not isfinite(normalized) or normalized <= 0.0 or normalized >= 1.0:
        raise ValueError("%s must be in (0, 1)" % name)
    return normalized


def candidate_family_confidence(
    confidence: float,
    run_confidence: float,
    family_index: int,
) -> Tuple[float, Fraction]:
    """Allocate a conservative binary confidence for one candidate family.

    Family ``j`` receives at most ``(1 - run_confidence) / (j * (j + 1))``
    alpha, additionally capped by the ordinary per-candidate alpha. The
    returned exact fraction is the alpha of the returned binary float.
    """
    base = _probability(confidence, "confidence")
    run_level = _probability(run_confidence, "run confidence")
    assert base is not None
    assert run_level is not None
    if isinstance(family_index, bool) or not isinstance(family_index, int):
        raise ValueError("candidate family index must be an integer")
    if family_index < 1:
        raise ValueError("candidate family index must be at least 1")

    base_fraction = Fraction.from_float(base)
    run_fraction = Fraction.from_float(run_level)
    nominal_alpha = (1 - run_fraction) / (
        family_index * (family_index + 1)
    )
    target_alpha = min(1 - base_fraction, nominal_alpha)
    target_confidence = 1 - target_alpha
    allocated = float(target_confidence)
    if Fraction.from_float(allocated) < target_confidence:
        allocated = nextafter(allocated, 1.0)
    if allocated >= 1.0:
        raise ValueError(
            "candidate family alpha is too small to represent as a binary float"
        )
    actual_alpha = 1 - Fraction.from_float(allocated)
    if actual_alpha <= 0 or actual_alpha > target_alpha:
        raise ArithmeticError("candidate family confidence rounded unsafely")
    return allocated, actual_alpha


def candidate_family_alpha_upper_bound(
    run_confidence: float,
    family_count: int,
) -> Fraction:
    """Return the harmonic policy's cumulative nominal alpha bound."""
    run_level = _probability(run_confidence, "run confidence")
    assert run_level is not None
    if isinstance(family_count, bool) or not isinstance(family_count, int):
        raise ValueError("candidate family count must be an integer")
    if family_count < 0:
        raise ValueError("candidate family count must be non-negative")
    return (1 - Fraction.from_float(run_level)) * Fraction(
        family_count,
        family_count + 1,
    )


def wilson_lower_bound(successes: int, total: int, confidence: float = 0.95) -> float:
    """Return the two-sided Wilson score lower bound for a success rate.

    ``confidence`` is the coverage probability (for example, ``0.95`` for a
    95% interval).  An empty sample set has no evidence of success and returns
    ``0.0``; this makes the helper useful while a sampler is still collecting
    observations.  Invalid counts and confidence values raise ``ValueError``.
    """
    if isinstance(successes, bool) or not isinstance(successes, int):
        raise ValueError("successes must be an integer")
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("total must be an integer")
    if total < 0:
        raise ValueError("total must be non-negative")
    if successes < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")
    level = _probability(confidence, "confidence")
    assert level is not None
    if total == 0:
        return 0.0

    # NormalDist is part of the Python standard library (3.8+) and avoids a
    # runtime dependency solely for an inverse normal CDF.
    # Use the lower tail so confidence values very close to one do not round
    # ``0.5 + level / 2`` to exactly 1.0 before the inverse CDF call.
    z = -NormalDist().inv_cdf((1.0 - level) / 2.0)
    n = float(total)
    p = float(successes) / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = z * sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n)))
    lower = (centre - spread) / denominator
    # Guard against tiny floating point excursions at the endpoints.
    return max(0.0, min(1.0, lower))


def exact_binomial_upper_tail(
    successes: int,
    total: int,
    null_rate: float,
) -> Fraction:
    """Return the exact binomial probability of at least ``successes``.

    The configured null rate is interpreted as its stored binary floating-point
    value. All probability arithmetic after that conversion uses integers, so
    callers can compare the result with a threshold without a rounding-induced
    false positive.
    """
    _validate_binomial_counts(successes, total)
    rate = _probability(null_rate, "null rate")
    assert rate is not None
    numerator, denominator = _binomial_upper_tail_ratio(successes, total, rate)
    return Fraction(numerator, denominator)


def exact_binomial_rate_gate(
    successes: int,
    total: int,
    minimum_rate: float,
    confidence: float = 0.95,
) -> bool:
    """Test a minimum rate with an exact one-sided binomial upper tail.

    This is equivalent to checking whether the one-sided Clopper-Pearson lower
    confidence bound is at least ``minimum_rate``. The comparison itself is
    performed by integer cross multiplication rather than rounded p-values.
    """
    _validate_binomial_counts(successes, total)
    rate = _probability(minimum_rate, "minimum rate")
    level = _probability(confidence, "confidence")
    assert rate is not None
    assert level is not None
    return _exact_binomial_rate_gate(successes, total, rate, level)


def clopper_pearson_lower_bound(
    successes: int,
    total: int,
    confidence: float = 0.95,
) -> float:
    """Return a one-sided Clopper-Pearson lower confidence bound.

    The statistical interval is exact rather than asymptotic. Bisection keeps
    its lower endpoint on the accepted side of an exact rational binomial-tail
    comparison, so the returned binary floating-point value is conservative.
    An empty sample or zero successes returns ``0.0``.
    """
    _validate_binomial_counts(successes, total)
    level = _probability(confidence, "confidence")
    assert level is not None
    if total == 0 or successes == 0:
        return 0.0

    low = 0.0
    high = 1.0
    while True:
        midpoint = low + (high - low) / 2.0
        if midpoint == low or midpoint == high:
            return low
        if _exact_binomial_rate_gate(successes, total, midpoint, level):
            low = midpoint
        else:
            high = midpoint


def _validate_binomial_counts(successes: int, total: int) -> None:
    if isinstance(successes, bool) or not isinstance(successes, int):
        raise ValueError("successes must be an integer")
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("total must be an integer")
    if total < 0:
        raise ValueError("total must be non-negative")
    if successes < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")


def _exact_binomial_rate_gate(
    successes: int,
    total: int,
    minimum_rate: float,
    confidence: float,
) -> bool:
    tail_numerator, tail_denominator = _binomial_upper_tail_ratio(
        successes,
        total,
        minimum_rate,
    )
    confidence_numerator, confidence_denominator = confidence.as_integer_ratio()
    alpha_numerator = confidence_denominator - confidence_numerator
    return (
        tail_numerator * confidence_denominator
        <= alpha_numerator * tail_denominator
    )


def _binomial_upper_tail_ratio(
    successes: int,
    total: int,
    null_rate: float,
) -> Tuple[int, int]:
    probability_numerator, probability_denominator = null_rate.as_integer_ratio()
    failure_numerator = probability_denominator - probability_numerator
    denominator = probability_denominator**total

    # Sum the shorter side of the distribution. Integer subtraction makes the
    # lower-tail complement exact, without catastrophic cancellation.
    upper_terms = total - successes + 1
    lower_terms = successes
    if upper_terms <= lower_terms:
        term = (
            comb(total, successes)
            * probability_numerator**successes
            * failure_numerator ** (total - successes)
        )
        upper_tail = term
        for count in range(successes, total):
            term, remainder = divmod(
                term * (total - count) * probability_numerator,
                (count + 1) * failure_numerator,
            )
            if remainder:
                raise ArithmeticError("inexact binomial probability recurrence")
            upper_tail += term
        return upper_tail, denominator

    term = failure_numerator**total
    lower_tail = 0
    for count in range(successes):
        lower_tail += term
        if count + 1 < successes:
            term, remainder = divmod(
                term * (total - count) * probability_numerator,
                (count + 1) * failure_numerator,
            )
            if remainder:
                raise ArithmeticError("inexact binomial probability recurrence")
    return denominator - lower_tail, denominator


def anytime_lower_bound(
    successes: int,
    total: int,
    confidence: float = 0.95,
) -> float:
    """Return a Jeffreys-mixture anytime-valid lower confidence bound.

    The bound inverts a beta-binomial mixture e-process with a fixed
    ``Beta(1/2, 1/2)`` mixing distribution.  Unlike a fixed-sample interval,
    it may be inspected after every observation without inflating its
    ``1 - confidence`` crossing probability.  An empty sample, or a sample
    with no successes, has no positive lower-bound evidence and returns 0.0.
    """
    if isinstance(successes, bool) or not isinstance(successes, int):
        raise ValueError("successes must be an integer")
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("total must be an integer")
    if total < 0:
        raise ValueError("total must be non-negative")
    if successes < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")
    level = _probability(confidence, "confidence")
    assert level is not None
    if total == 0 or successes == 0:
        return 0.0

    failures = total - successes
    log_threshold = -log(1.0 - level)
    log_mixture_constant = (
        lgamma(successes + 0.5)
        + lgamma(failures + 0.5)
        - lgamma(total + 1.0)
        - 2.0 * lgamma(0.5)
    )

    def log_mixture(null_rate: float) -> float:
        value = log_mixture_constant - successes * log(null_rate)
        if failures:
            value -= failures * log1p(-null_rate)
        return value

    # On (0, successes / total), the mixture e-value decreases continuously
    # from infinity to at most one. Keep ``low`` on the rejected side of the
    # confidence set so floating-point error can only delay early acceptance.
    low = 0.0
    high = float(successes) / float(total)
    for _ in range(80):
        midpoint = low + (high - low) / 2.0
        if log_mixture(midpoint) >= log_threshold:
            low = midpoint
        else:
            high = midpoint
    # Transcendental rounding can place the computed root a few ULPs above the
    # mathematical endpoint. Move inward so the published bound remains the
    # conservative side of a threshold comparison.
    for _ in range(32):
        low = nextafter(low, 0.0)
    return max(0.0, min(1.0, low))


class FailureOracle:
    def __init__(
        self,
        runner: Runner,
        spec: FailureSpec,
        min_baseline_rate: Optional[float] = None,
        min_candidate_rate: Optional[float] = None,
        confidence: float = 0.95,
        python_frame_policy: Optional[str] = PYTHON_FRAME_POLICY,
    ) -> None:
        signature_modes = sum(
            (spec.java_exception, spec.python_exception, spec.process_failure)
        )
        if signature_modes > 1:
            raise OracleError("only one learned failure signature mode may be enabled")
        if python_frame_policy not in (None, PYTHON_FRAME_POLICY):
            raise OracleError("unsupported Python frame normalization policy")
        self.python_frame_policy = python_frame_policy
        self.runner = runner
        self.spec = spec
        self.min_baseline_rate = _probability(
            min_baseline_rate, "minimum baseline rate"
        )
        self.min_candidate_rate = _probability(
            min_candidate_rate, "minimum candidate rate"
        )
        validated_confidence = _probability(confidence, "confidence")
        assert validated_confidence is not None
        self.confidence = validated_confidence
        self._java_exception_signature: Optional[JavaExceptionSignature] = None
        self._python_exception_signature: Optional[PythonExceptionSignature] = None
        self._process_failure_signature: Optional[ProcessFailureSignature] = None
        self.baseline_runs = 0
        self.baseline_passes = 0
        self.baseline_rate: Optional[float] = None
        self.baseline_lower_bound: Optional[float] = None
        self.baseline_rate_evidence_runs: Optional[int] = None
        self.baseline_rate_evidence_passes: Optional[int] = None
        self.baseline_exact_lower_bound: Optional[float] = None
        self.baseline_exact_p_value: Optional[float] = None
        self.baseline_exact_rate_gate_passed: Optional[bool] = None
        self.candidate_runs = 0
        self.candidate_passes = 0
        self.candidate_rate: Optional[float] = None
        self.candidate_lower_bound: Optional[float] = None
        try:
            self._pattern = re.compile(spec.match) if spec.match else None
        except re.error as exc:
            raise OracleError("invalid --match regular expression: %s" % exc) from exc

    def _extract_python_signature(self, result: RunResult) -> Optional[PythonExceptionSignature]:
        return extract_run_python_exception(
            result, self._pattern, self.python_frame_policy is not None,
        )

    def accepts(self, result: RunResult) -> bool:
        if not self._accepts_basic(result):
            return False
        if self.spec.java_exception:
            signature = extract_run_java_exception(result, self._pattern)
            if signature is None or (
                self._java_exception_signature is not None
                and signature != self._java_exception_signature
            ):
                return False
        if self.spec.python_exception:
            python_signature = self._extract_python_signature(result)
            if python_signature is None or (
                self._python_exception_signature is not None
                and python_signature != self._python_exception_signature
            ):
                return False
        if self.spec.process_failure:
            process_signature = extract_process_failure(result)
            if process_signature is None or (
                self._process_failure_signature is not None
                and process_signature != self._process_failure_signature
            ):
                return False
        return True

    def accepts_repeated(
        self,
        results: Sequence[RunResult],
        minimum_passes: int = 1,
        minimum_rate: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> Tuple[bool, int]:
        """Evaluate independent samples and return (accepted, passing_count)."""
        if minimum_passes < 1:
            raise ValueError("minimum candidate passes must be at least 1")
        rate = self.min_candidate_rate if minimum_rate is None else _probability(
            minimum_rate, "minimum candidate rate"
        )
        level = self.confidence if confidence is None else _probability(
            confidence, "confidence"
        )
        assert level is not None
        passing = sum(1 for result in results if self.accepts(result))
        total = len(results)
        lower = wilson_lower_bound(passing, total, level)
        self.candidate_runs = total
        self.candidate_passes = passing
        self.candidate_rate = (float(passing) / total) if total else 0.0
        self.candidate_lower_bound = lower
        if any(result.timed_out or result.resource_exhausted for result in results):
            return False, passing
        count_ok = passing >= minimum_passes
        rate_ok = rate is None or exact_binomial_rate_gate(
            passing,
            total,
            rate,
            level,
        )
        return count_ok and rate_ok, passing

    @property
    def java_exception_signature(self) -> Optional[JavaExceptionSignature]:
        return self._java_exception_signature

    @property
    def python_exception_signature(self) -> Optional[PythonExceptionSignature]:
        return self._python_exception_signature

    @property
    def process_failure_signature(self) -> Optional[ProcessFailureSignature]:
        return self._process_failure_signature

    def pin_failure_signature(self, signature: object) -> None:
        """Pin a previously recorded signature without learning a replacement."""
        if self.baseline_runs or any(
            value is not None
            for value in (
                self._java_exception_signature,
                self._python_exception_signature,
                self._process_failure_signature,
            )
        ):
            raise OracleError("failure signature can only be pinned on a fresh oracle")
        if self.spec.java_exception:
            if not isinstance(signature, JavaExceptionSignature):
                raise OracleError("recorded Java exception signature is invalid")
            self._java_exception_signature = signature
        elif self.spec.python_exception:
            if not isinstance(signature, PythonExceptionSignature):
                raise OracleError("recorded Python exception signature is invalid")
            self._python_exception_signature = signature
        elif self.spec.process_failure:
            if not isinstance(signature, ProcessFailureSignature) or not (
                valid_recorded_process_failure_signature(signature)
            ):
                raise OracleError("recorded process failure signature is invalid")
            self._process_failure_signature = signature
        else:
            raise OracleError("failure signature mode is not enabled")

    def _accepts_basic(self, result: RunResult) -> bool:
        if result.timed_out or result.resource_exhausted:
            return False
        expected_exit = self.spec.exit_code
        if expected_exit is None:
            if result.returncode == 0:
                return False
        elif result.returncode != expected_exit:
            return False
        match_text = result.output
        if self.spec.java_exception and result.diagnostics:
            match_text += "\n" + result.diagnostics
        return self._pattern is None or self._pattern.search(match_text) is not None

    def verify_baseline(
        self,
        cwd: Path,
        repeat: int,
        reset: Optional[Callable[[], None]] = None,
        prepare: Optional[Callable[[], Path]] = None,
        minimum_passes: Optional[int] = None,
        minimum_rate: Optional[float] = None,
    ) -> RunResult:
        if repeat < 1:
            raise OracleError("baseline repeat must be at least 1")
        required = repeat if minimum_passes is None else minimum_passes
        if required < 1 or required > repeat:
            raise OracleError(
                "minimum baseline passes must be between 1 and baseline runs"
            )
        if minimum_rate is None:
            rate = self.min_baseline_rate
        else:
            rate = _probability(minimum_rate, "minimum baseline rate")
            if rate != self.min_baseline_rate:
                raise OracleError(
                    "minimum baseline rate override does not match oracle configuration"
                )
        level = self.confidence
        self.baseline_rate_evidence_runs = None
        self.baseline_rate_evidence_passes = None
        self.baseline_exact_lower_bound = None
        self.baseline_exact_p_value = None
        self.baseline_exact_rate_gate_passed = None
        if reset is not None and prepare is not None:
            raise ValueError("baseline reset and prepare callbacks are mutually exclusive")
        baseline = None
        all_results: list[RunResult] = []
        successful: list[RunResult] = []
        java_signatures: list[JavaExceptionSignature] = []
        python_signatures: list[PythonExceptionSignature] = []
        process_signatures: list[ProcessFailureSignature] = []
        java_discovery_attempt: Optional[int] = None
        python_discovery_attempt: Optional[int] = None
        process_discovery_attempt: Optional[int] = None
        resource_failure: Optional[RunResult] = None
        for attempt in range(1, repeat + 1):
            run_cwd = cwd
            if prepare is not None:
                run_cwd = prepare()
            if reset is not None:
                reset()
            result = self.runner.run(run_cwd)
            all_results.append(result)
            if reset is not None:
                reset()
            if result.timed_out or result.resource_exhausted:
                resource_failure = result
            if not self._accepts_basic(result):
                continue
            successful.append(result)
            if self.spec.java_exception:
                signature = extract_run_java_exception(result, self._pattern)
                if signature is not None:
                    java_signatures.append(signature)
                    if java_discovery_attempt is None:
                        java_discovery_attempt = attempt
            if self.spec.python_exception:
                python_signature = self._extract_python_signature(result)
                if python_signature is not None:
                    python_signatures.append(python_signature)
                    if python_discovery_attempt is None:
                        python_discovery_attempt = attempt
            if self.spec.process_failure:
                process_signature = extract_process_failure(result)
                if process_signature is not None:
                    process_signatures.append(process_signature)
                    if process_discovery_attempt is None:
                        process_discovery_attempt = attempt
        self.baseline_runs = repeat
        if resource_failure is not None:
            failure = resource_failure
            raise OracleError(
                "baseline encountered a timeout or resource exhaustion "
                "(timed_out=%s, resource_exhausted=%s%s)"
                % (
                    failure.timed_out,
                    failure.resource_exhausted,
                    ", reason=" + failure.resource_reason
                    if failure.resource_reason
                    else "",
                )
            )
        if self.spec.java_exception:
            if rate is None:
                self._java_exception_signature = _stable_signature(
                    java_signatures,
                    required,
                    "Java",
                )
            else:
                if not java_signatures:
                    _stable_signature(java_signatures, 1, "Java")
                self._java_exception_signature = java_signatures[0]
            passing = [
                result
                for result in successful
                if extract_run_java_exception(result, self._pattern)
                == self._java_exception_signature
            ]
        elif self.spec.python_exception:
            if rate is None:
                self._python_exception_signature = _stable_signature(
                    python_signatures,
                    required,
                    "Python",
                )
            else:
                if not python_signatures:
                    _stable_signature(python_signatures, 1, "Python")
                self._python_exception_signature = python_signatures[0]
            passing = [
                result
                for result in successful
                if self._extract_python_signature(result)
                == self._python_exception_signature
            ]
        elif self.spec.process_failure:
            if rate is None:
                self._process_failure_signature = _stable_process_failure(
                    process_signatures,
                    required,
                )
            else:
                if not process_signatures:
                    _stable_process_failure(process_signatures, 1)
                self._process_failure_signature = process_signatures[0]
            passing = [
                result
                for result in successful
                if extract_process_failure(result) == self._process_failure_signature
            ]
        else:
            passing = successful
        self.baseline_passes = len(passing)
        self.baseline_rate = float(len(passing)) / repeat
        self.baseline_lower_bound = wilson_lower_bound(
            len(passing), repeat, level
        )
        count_ok = len(passing) >= required
        rate_evidence_passes = len(passing)
        rate_evidence_runs = repeat
        if rate is not None and self.spec.java_exception:
            assert java_discovery_attempt is not None
            rate_results = all_results[java_discovery_attempt:]
            rate_evidence_runs = len(rate_results)
            rate_evidence_passes = sum(
                1 for result in rate_results if self.accepts(result)
            )
        elif rate is not None and self.spec.python_exception:
            assert python_discovery_attempt is not None
            rate_results = all_results[python_discovery_attempt:]
            rate_evidence_runs = len(rate_results)
            rate_evidence_passes = sum(
                1 for result in rate_results if self.accepts(result)
            )
        elif rate is not None and self.spec.process_failure:
            assert process_discovery_attempt is not None
            rate_results = all_results[process_discovery_attempt:]
            rate_evidence_runs = len(rate_results)
            rate_evidence_passes = sum(
                1 for result in rate_results if self.accepts(result)
            )
        if rate is None:
            rate_ok = True
        else:
            self.baseline_rate_evidence_runs = rate_evidence_runs
            self.baseline_rate_evidence_passes = rate_evidence_passes
            self.baseline_exact_lower_bound = clopper_pearson_lower_bound(
                rate_evidence_passes,
                rate_evidence_runs,
                level,
            )
            self.baseline_exact_p_value = float(
                exact_binomial_upper_tail(
                    rate_evidence_passes,
                    rate_evidence_runs,
                    rate,
                )
            )
            self.baseline_exact_rate_gate_passed = exact_binomial_rate_gate(
                rate_evidence_passes,
                rate_evidence_runs,
                rate,
                level,
            )
            rate_ok = self.baseline_exact_rate_gate_passed
        if not count_ok or not rate_ok:
            last = result
            detail = ""
            if not count_ok:
                detail += "; at least %d passes are required" % required
            if not rate_ok:
                detail += (
                    "; exact one-sided rate gate failed for minimum rate %.4f "
                    "on %d/%d rate-evidence passes "
                    "(descriptive full-sample Wilson lower bound %.4f)"
                ) % (
                    rate,
                    rate_evidence_passes,
                    rate_evidence_runs,
                    self.baseline_lower_bound,
                )
            raise OracleError(
                "baseline reproduced the failure only %d/%d times%s "
                "(last exit=%d, timed_out=%s, resource_exhausted=%s%s)"
                % (
                    len(passing),
                    repeat,
                    detail,
                    last.returncode,
                    last.timed_out,
                    last.resource_exhausted,
                    ", reason=" + last.resource_reason if last.resource_reason else "",
                )
            )
        baseline = passing[-1]
        return baseline

    def checkpoint_state(self) -> dict:
        """Return the learned signature state needed by a resumable session."""
        state = {
            "min_baseline_rate": self.min_baseline_rate,
            "min_candidate_rate": self.min_candidate_rate,
            "confidence": self.confidence,
            "baseline_runs": self.baseline_runs,
            "baseline_passes": self.baseline_passes,
            "baseline_rate": self.baseline_rate,
            "baseline_lower_bound": self.baseline_lower_bound,
            "baseline_rate_evidence_runs": self.baseline_rate_evidence_runs,
            "baseline_rate_evidence_passes": self.baseline_rate_evidence_passes,
            "baseline_exact_lower_bound": self.baseline_exact_lower_bound,
            "baseline_exact_p_value": self.baseline_exact_p_value,
            "baseline_exact_rate_gate_passed": (
                self.baseline_exact_rate_gate_passed
            ),
            "candidate_runs": self.candidate_runs,
            "candidate_passes": self.candidate_passes,
            "candidate_rate": self.candidate_rate,
            "candidate_lower_bound": self.candidate_lower_bound,
        }
        if self._java_exception_signature is not None:
            state["java_exception_signature"] = asdict(self._java_exception_signature)
        if self.spec.python_exception:
            state["python_frame_policy"] = self.python_frame_policy
        if self._python_exception_signature is not None:
            state["python_exception_signature"] = asdict(
                self._python_exception_signature
            )
        if self._process_failure_signature is not None:
            state["process_failure_signature"] = asdict(
                self._process_failure_signature
            )
        return state

    def restore_checkpoint_state(self, state: dict) -> bool:
        """Restore learned state and report whether legacy evidence was rebuilt."""
        if not isinstance(state, dict):
            raise OracleError("session contains invalid oracle state")
        if self.spec.python_exception and (
            "python_frame_policy" not in state
            or state["python_frame_policy"] != self.python_frame_policy
        ):
            raise OracleError("Python frame normalization policy changed; start a new session")
        self._java_exception_signature = None
        self._python_exception_signature = None
        self._process_failure_signature = None
        for key, expected in (
            ("min_baseline_rate", self.min_baseline_rate),
            ("min_candidate_rate", self.min_candidate_rate),
            ("confidence", self.confidence),
        ):
            if key not in state:
                continue
            saved = state[key]
            if saved is None:
                normalized = None
            else:
                normalized = float(saved)
            if normalized != expected:
                raise OracleError("session oracle %s configuration changed" % key)
        self.baseline_runs = int(state.get("baseline_runs", 0))
        self.baseline_passes = int(state.get("baseline_passes", 0))
        self.baseline_rate = _optional_observation(state.get("baseline_rate"))
        self.baseline_lower_bound = _optional_observation(
            state.get("baseline_lower_bound")
        )
        has_rate_evidence_state = any(
            key in state for key in BASELINE_RATE_EVIDENCE_FIELDS
        )
        self.baseline_rate_evidence_runs = _optional_count(
            state.get("baseline_rate_evidence_runs")
        )
        self.baseline_rate_evidence_passes = _optional_count(
            state.get("baseline_rate_evidence_passes")
        )
        self.baseline_exact_lower_bound = _optional_observation(
            state.get("baseline_exact_lower_bound")
        )
        self.baseline_exact_p_value = _optional_observation(
            state.get("baseline_exact_p_value")
        )
        gate_state = state.get("baseline_exact_rate_gate_passed")
        if gate_state is not None and not isinstance(gate_state, bool):
            raise OracleError("session contains an invalid baseline rate gate result")
        self.baseline_exact_rate_gate_passed = gate_state
        self.candidate_runs = int(state.get("candidate_runs", 0))
        self.candidate_passes = int(state.get("candidate_passes", 0))
        self.candidate_rate = _optional_observation(state.get("candidate_rate"))
        self.candidate_lower_bound = _optional_observation(
            state.get("candidate_lower_bound")
        )
        java_state = state.get("java_exception_signature")
        python_state = state.get("python_exception_signature")
        process_state = state.get("process_failure_signature")
        if java_state is not None:
            if not self.spec.java_exception:
                raise OracleError(
                    "session contains a Java exception signature but --java-exception is disabled"
                )
            try:
                self._java_exception_signature = JavaExceptionSignature(
                    str(java_state["class_name"]),
                    str(java_state["message"]),
                    tuple(str(frame) for frame in java_state["frames"]),
                )
            except (KeyError, TypeError) as exc:
                raise OracleError("session contains an invalid Java exception signature") from exc
        if python_state is not None:
            if not self.spec.python_exception:
                raise OracleError(
                    "session contains a Python exception signature but "
                    "--python-exception is disabled"
                )
            if not isinstance(python_state, dict) or python_state.get("normalization_policy") not in (
                None, self.python_frame_policy,
            ):
                raise OracleError("session contains an invalid Python frame normalization policy")
            try:
                self._python_exception_signature = PythonExceptionSignature(
                    str(python_state["class_name"]),
                    str(python_state["message"]),
                    tuple(str(frame) for frame in python_state["frames"]),
                    python_state.get("normalization_policy"),
                )
            except (KeyError, TypeError) as exc:
                raise OracleError(
                    "session contains an invalid Python exception signature"
                ) from exc
        if process_state is not None:
            if not self.spec.process_failure:
                raise OracleError(
                    "session contains a process failure signature but "
                    "--process-failure is disabled"
                )
            try:
                kind = process_state["kind"]
                code = process_state["code"]
                if not isinstance(kind, str):
                    raise TypeError
                if isinstance(code, bool) or not isinstance(code, int):
                    raise TypeError
                signature = ProcessFailureSignature(kind, code)
                if not valid_process_failure_signature(signature):
                    raise ValueError
                self._process_failure_signature = signature
            except (KeyError, TypeError, ValueError) as exc:
                raise OracleError(
                    "session contains an invalid process failure signature"
                ) from exc
        learned_signatures = sum(
            signature is not None
            for signature in (
                self._java_exception_signature,
                self._python_exception_signature,
                self._process_failure_signature,
            )
        )
        signature_mode = (
            self.spec.java_exception
            or self.spec.python_exception
            or self.spec.process_failure
        )
        if self.baseline_runs > 0 and signature_mode and learned_signatures != 1:
            raise OracleError("session is missing its learned failure signature")
        if self.baseline_runs == 0 and learned_signatures:
            raise OracleError("session contains a signature before baseline discovery")
        return self._validate_restored_baseline_rate_evidence(
            has_rate_evidence_state
        )

    def _validate_restored_baseline_rate_evidence(self, present: bool) -> bool:
        values = (
            self.baseline_rate_evidence_runs,
            self.baseline_rate_evidence_passes,
            self.baseline_exact_lower_bound,
            self.baseline_exact_p_value,
            self.baseline_exact_rate_gate_passed,
        )
        signature_mode = (
            self.spec.java_exception
            or self.spec.python_exception
            or self.spec.process_failure
        )
        reconstructed = False
        if not present:
            if self.min_baseline_rate is None or self.baseline_runs == 0:
                return False
            if signature_mode:
                raise OracleError(
                    "legacy session lacks post-discovery baseline rate evidence; "
                    "start a new session instead"
                )
            self.baseline_rate_evidence_runs = self.baseline_runs
            self.baseline_rate_evidence_passes = self.baseline_passes
            reconstructed = True
        else:
            if all(value is None for value in values):
                if self.min_baseline_rate is None or self.baseline_runs == 0:
                    return False
                raise OracleError("session contains incomplete baseline rate evidence")
            if any(value is None for value in values):
                raise OracleError("session contains incomplete baseline rate evidence")
        if self.min_baseline_rate is None:
            raise OracleError(
                "session contains baseline rate evidence but no baseline rate gate"
            )

        runs = self.baseline_rate_evidence_runs
        passes = self.baseline_rate_evidence_passes
        assert runs is not None
        assert passes is not None
        if (
            self.baseline_runs < 1
            or self.baseline_passes < 0
            or self.baseline_passes > self.baseline_runs
            or self.baseline_rate
            != float(self.baseline_passes) / self.baseline_runs
            or self.baseline_lower_bound
            != wilson_lower_bound(
                self.baseline_passes,
                self.baseline_runs,
                self.confidence,
            )
            or passes > runs
            or runs > self.baseline_runs
            or passes > self.baseline_passes
        ):
            raise OracleError("session contains inconsistent baseline rate evidence")
        has_signature = (
            self._java_exception_signature is not None
            or self._python_exception_signature is not None
            or self._process_failure_signature is not None
        )
        if signature_mode and not has_signature:
            raise OracleError("session contains inconsistent baseline rate evidence")
        if signature_mode:
            if runs >= self.baseline_runs or passes >= self.baseline_passes:
                raise OracleError("session contains inconsistent baseline rate evidence")
        elif runs != self.baseline_runs or passes != self.baseline_passes:
            raise OracleError("session contains inconsistent baseline rate evidence")

        expected_lower_bound = clopper_pearson_lower_bound(
            passes,
            runs,
            self.confidence,
        )
        expected_p_value = float(
            exact_binomial_upper_tail(
                passes,
                runs,
                self.min_baseline_rate,
            )
        )
        expected_gate = exact_binomial_rate_gate(
            passes,
            runs,
            self.min_baseline_rate,
            self.confidence,
        )
        if not expected_gate:
            raise OracleError("session contains inconsistent baseline rate evidence")
        if reconstructed:
            self.baseline_exact_lower_bound = expected_lower_bound
            self.baseline_exact_p_value = expected_p_value
            self.baseline_exact_rate_gate_passed = expected_gate
        elif (
            self.baseline_exact_lower_bound != expected_lower_bound
            or self.baseline_exact_p_value != expected_p_value
            or self.baseline_exact_rate_gate_passed != expected_gate
        ):
            raise OracleError("session contains inconsistent baseline rate evidence")
        return reconstructed


def _optional_observation(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OracleError("session contains an invalid oracle observation") from exc
    if not isfinite(number) or number < 0.0 or number > 1.0:
        raise OracleError("session contains an invalid oracle observation")
    return number


def _optional_count(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OracleError("session contains an invalid baseline rate evidence count")
    return value


def _stable_signature(values: Sequence[object], required: int, language: str):
    if not values:
        raise OracleError(
            "baseline did not contain a %s exception with a stack frame" % language
        )
    signature, count = Counter(values).most_common(1)[0]
    if count < required:
        raise OracleError("changed %s exception signature" % language)
    return signature


def _stable_process_failure(
    values: Sequence[ProcessFailureSignature],
    required: int,
) -> ProcessFailureSignature:
    if not values:
        raise OracleError("baseline did not contain a non-zero process failure")
    signature, count = Counter(values).most_common(1)[0]
    if count < required:
        raise OracleError("changed process failure signature")
    return signature

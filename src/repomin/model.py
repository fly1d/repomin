from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


CANDIDATE_SAMPLING_POLICY = (
    "jeffreys-mixture-cs-exact-terminal-signature-split-v3"
)
CANDIDATE_FAMILY_CONTROL_POLICY = "harmonic-alpha-spending-v1"
REDUCTION_STRATEGY = "hierarchical-fixed-point-v2"
HOLDOUT_CERTIFICATION_POLICY = "fixed-n-clopper-pearson-one-sided-v1"
TREE_FINGERPRINT_POLICY = "tree-sha256-v2"
DEFAULT_SEMANTIC_TIMEOUT_SECONDS = 60.0
# A transport-friendly digest used when artifact stores rewrite filesystem
# metadata such as modification times. It intentionally covers content,
# paths, and entry kinds, but not mutable filesystem metadata.
TREE_CONTENT_FINGERPRINT_POLICY = "tree-content-sha256-v1"


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    diagnostics: str = ""
    resource_exhausted: bool = False
    resource_reason: Optional[str] = None

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


@dataclass(frozen=True)
class JavaExceptionSignature:
    class_name: str
    message: str
    frames: Tuple[str, ...]


@dataclass(frozen=True)
class PythonExceptionSignature:
    class_name: str
    message: str
    frames: Tuple[str, ...]


@dataclass(frozen=True)
class ProcessFailureSignature:
    kind: str
    code: int


@dataclass(frozen=True)
class FailureSpec:
    match: Optional[str]
    exit_code: Optional[int] = None
    java_exception: bool = False
    python_exception: bool = False
    process_failure: bool = False


@dataclass(frozen=True)
class HoldoutSample:
    index: int
    outcome: str
    accepted: bool
    returncode: Optional[int] = None
    duration_seconds: Optional[float] = None
    timed_out: bool = False
    resource_exhausted: bool = False
    resource_reason: Optional[str] = None
    output_sha256: Optional[str] = None


@dataclass
class HoldoutCertification:
    status: str = "not_requested"
    policy: str = HOLDOUT_CERTIFICATION_POLICY
    attempt_id: Optional[str] = None
    planned_runs: int = 0
    completed_runs: int = 0
    passes: int = 0
    minimum_rate: Optional[float] = None
    confidence: Optional[float] = None
    required_passes: Optional[int] = None
    observed_rate: Optional[float] = None
    exact_lower_bound: Optional[float] = None
    exact_p_value: Optional[float] = None
    exact_rate_gate_passed: Optional[bool] = None
    artifact_fingerprint: Optional[str] = None
    oracle_identity_sha256: Optional[str] = None
    timed_out_runs: int = 0
    resource_exhausted_runs: int = 0
    interrupted_runs: int = 0
    in_flight_index: Optional[int] = None
    resumed: bool = False
    fresh_repository_copy_per_run: bool = True
    cache_used: bool = False
    early_stopping: bool = False
    samples: List[HoldoutSample] = field(default_factory=list)
    representative_run: Optional[RunResult] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ReductionEvent:
    phase: str
    description: str
    duration_seconds: float
    oracle_runs: int = 0
    oracle_passes: int = 0
    oracle_rate: Optional[float] = None
    oracle_lower_bound: Optional[float] = None
    oracle_anytime_lower_bound: Optional[float] = None
    oracle_early_acceptance: bool = False
    candidate_family_index: Optional[int] = None
    candidate_confidence: Optional[float] = None
    candidate_alpha: Optional[float] = None


@dataclass
class ReductionPhaseStats:
    phase: str
    passes: int = 0
    completed_passes: int = 0
    aborted_passes: int = 0
    wall_seconds: float = 0.0
    bytes_removed: int = 0
    bytes_added: int = 0
    attempts: int = 0
    no_op: int = 0
    rejected: int = 0
    accepted: int = 0
    superseded: int = 0
    aborted: int = 0
    oracle_sample_uses: int = 0
    oracle_samples: int = 0
    oracle_passing_sample_uses: int = 0
    oracle_seconds: float = 0.0
    cache_hits: int = 0
    samples_saved: int = 0


@dataclass
class ReductionStats:
    source_files: int
    source_bytes: int
    jobs: int = 1
    cache_enabled: bool = True
    backend: str = "host"
    working_directory_policy: Optional[str] = None
    working_directory_basename: Optional[str] = None
    container_image: Optional[str] = None
    container_image_id: Optional[str] = None
    container_network: Optional[str] = None
    container_cpus: Optional[float] = None
    container_memory_bytes: Optional[int] = None
    container_pids_limit: Optional[int] = None
    container_tmpfs_bytes: Optional[int] = None
    container_workspace_limit_bytes: Optional[int] = None
    attempts: int = 0
    accepted: int = 0
    cache_hits: int = 0
    events: List[ReductionEvent] = field(default_factory=list)
    reduction_strategy: str = REDUCTION_STRATEGY
    phase_statistics_complete: bool = True
    phase_stats: Dict[str, ReductionPhaseStats] = field(default_factory=dict)
    output_files: int = 0
    output_bytes: int = 0
    session_path: Optional[str] = None
    resumed: bool = False
    baseline_runs: int = 0
    baseline_passes: int = 0
    # Optional statistical acceptance criteria and baseline/final observations.
    # Per-candidate evidence is retained on accepted ReductionEvent entries.
    min_baseline_rate: Optional[float] = None
    min_candidate_rate: Optional[float] = None
    confidence: float = 0.95
    candidate_sampling_policy: str = CANDIDATE_SAMPLING_POLICY
    run_confidence: Optional[float] = None
    candidate_family_control_policy: Optional[str] = None
    candidate_family_count: int = 0
    candidate_family_alpha_upper_bound: float = 0.0
    baseline_rate: Optional[float] = None
    baseline_lower_bound: Optional[float] = None
    baseline_rate_evidence_runs: Optional[int] = None
    baseline_rate_evidence_passes: Optional[int] = None
    baseline_exact_lower_bound: Optional[float] = None
    baseline_exact_p_value: Optional[float] = None
    baseline_exact_rate_gate_passed: Optional[bool] = None
    candidate_runs: int = 1
    candidate_min_passes: int = 1
    candidate_samples: int = 0
    candidate_passes: int = 0
    candidate_early_rejections: int = 0
    candidate_early_acceptances: int = 0
    candidate_samples_saved: int = 0
    final_runs: int = 0
    final_passes: int = 0
    final_rate: Optional[float] = None
    final_lower_bound: Optional[float] = None
    ignored_names: List[str] = field(default_factory=list)
    ignored_paths: List[str] = field(default_factory=list)
    environment_names: List[str] = field(default_factory=list)
    environment_sha256: Optional[str] = None
    gitignore_files: List[str] = field(default_factory=list)
    gitignore_sha256: Optional[str] = None
    gitignore_recursive: bool = False
    keep_paths: List[str] = field(default_factory=list)
    text_files: List[str] = field(default_factory=list)
    max_attempts: Optional[int] = None
    budget_exhausted: bool = False
    max_duration_seconds: Optional[float] = None
    reduction_started_at: Optional[float] = None
    semantic_reducer: Optional[str] = None
    semantic_model: Optional[str] = None
    semantic_endpoint: Optional[str] = None
    semantic_calls: int = 0
    semantic_accepted: int = 0
    semantic_timeout: Optional[float] = None


@dataclass(frozen=True)
class ReductionResult:
    output: Path
    stats: ReductionStats
    baseline: RunResult
    final_run: RunResult
    java_exception_signature: Optional[JavaExceptionSignature] = None
    python_exception_signature: Optional[PythonExceptionSignature] = None
    holdout_certification: HoldoutCertification = field(
        default_factory=HoldoutCertification
    )
    process_failure_signature: Optional[ProcessFailureSignature] = None

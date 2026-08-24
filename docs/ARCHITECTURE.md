# Architecture

ReproMin treats reduction as a sequence of transactions guarded by a failure
oracle.

```text
source repository
      |
      v
isolated working copy -> repeated baseline checks
      |
      v
manifest mutations -> initial file and directory mutations
      |
      v
Java/Python source mutations -> global fixed point -> final consistency check
      |
      v
optional frozen holdout certification -> new payload directory
                                  + sibling OUTPUT.repomin metadata
```

## Core invariants

1. The source repository is never used as a command working directory and is
   never mutated.
2. A mutation is committed only when the command still has the configured exit
   behavior, its combined output matches the configured regular expression, and
   any enabled failure signature matches.
3. Every mutation runs in a fresh copy of the last accepted state.
4. Validation commands never run in the accepted state. An accepted mutation
   is reapplied to a clean copy, so arbitrary command writes cannot be promoted.
5. Generated build artifacts are removed between attempts so stale outputs
   cannot replace deleted source files.
6. An existing output path is never overwritten.
7. When a persistent session is enabled, accepted state and progress are
   checkpointed atomically; a resumed session must match the original source
   fingerprint and reduction configuration.
8. Reduction terminates only after every enabled reducer is locally stable on a
   tree that includes the last accepted change made by every other reducer.
9. A requested holdout is planned once against a frozen tree and fixed oracle,
   runner, sample count, target rate, and confidence. Its samples are never used
   to select or modify the artifact.
10. The exported repository is exactly the accepted payload. Tool-owned
    `report.json` and `REPOMIN.md` live in the sibling `OUTPUT.repomin`
    directory, so they do not change the payload tree or its certified
    fingerprint. A host command that reads parent-directory state can still
    observe this sibling; such dependencies are part of the documented host
    backend boundary.

## Failure oracle

The base oracle evaluates these signals:

- the command completed before its timeout;
- the exit code is non-zero, or equals an explicitly requested code;
- when configured, combined stdout and stderr match a user-provided regular
  expression.

An output expression is required by the CLI unless `--process-failure` or
`--exit-code` is enabled. A standalone `--exit-code` preserves only the exact
requested exit status, so captured output may change freely. Process mode learns
one exact normalized termination from the baseline:
a directly observed POSIX signal, a Windows unsigned 32-bit status, or an
ordinary non-zero exit code. It then adds equality with that learned signature
to the complete oracle. Timeout and resource-exhaustion results have no process
signature. Positive shell/container codes such as 139 remain exit codes rather
than being inferred as signals, because the same number can be returned
deliberately.

`--baseline-runs N --min-baseline-passes K` evaluates the baseline as a sample
set. At least `K` samples must satisfy all base signals; the default `K=N`
preserves the strict deterministic behavior. A candidate can be sampled with
`--candidate-runs N --min-candidate-passes K`. Each sample runs in a fresh copy
of that candidate, and acceptance requires at least `K` complete oracle passes.
Observed timeouts and resource exhaustion are explicit negative samples, can
never be counted as passes, and reject the candidate sample set even when other
samples pass. Candidate result caching is disabled for repeated sampling so old
output cannot substitute for a new trial.

Candidate sampling can stop before `N` in either direction. Rejection is already
inevitable when the remaining samples cannot satisfy the count threshold, even
an all-pass suffix cannot pass the planned-size exact rate gate, or an observed
timeout/resource failure has made the set
invalid. A count-only candidate accepts as soon as it accumulates `K` passes;
additional ordinary failures cannot reduce that count.

With `--min-baseline-rate R` or `--min-candidate-rate R`, a complete sample set
must also pass the exact one-sided binomial upper-tail test at `R`, equivalently
have a Clopper-Pearson lower confidence bound at least `R`, where `0 < R < 1`.
`--confidence` controls the coverage level and defaults to `0.95`. A rate
criterion is combined with the count criterion; when no count minimum is
provided, the CLI uses a minimum of one so that the rate remains the useful
gate. Wilson lower bounds are retained as descriptive report metrics only.

Rate-gated learned-signature mode uses a conditional sample split. The first
basic-passing result with an extractable Java exception, Python exception, or
process termination fixes the signature and is excluded from the rate test.
Conditional on that discovery time and signature, only the remaining planned
samples are Bernoulli evidence for the now-fixed oracle, preventing a
data-selected modal signature from receiving an unadjusted single-category
p-value. The count threshold still uses all `N` samples matching the discovered
signature. CLI attainability reserves one of the `N` slots for discovery.
Count-only signature learning retains its stable-mode behavior.

Candidate early acceptance uses a Jeffreys beta-binomial mixture confidence
sequence. For `S_t` passes in `t` Bernoulli oracle samples, let
`alpha = 1 - confidence` and

```text
E_t(p) = B(S_t + 1/2, t - S_t + 1/2)
         / (B(1/2, 1/2) * p^S_t * (1 - p)^(t - S_t))
C_t = {p: E_t(p) < 1 / alpha}
L_t = inf C_t
```

For each fixed true pass probability `p`, `E_t(p)` is a nonnegative likelihood-
ratio martingale. Ville's inequality therefore gives simultaneous coverage
`P(p in C_t for every t) >= 1 - alpha`, so `L_t` may be inspected after every
sample without fixed-time repeated-peeking inflation. The `Beta(1/2, 1/2)`
mixing distribution is fixed before observing the candidate.

At a prefix `t < N`, a rate-gated candidate accepts only when `S_t >= K`,
`L_t >= R`, and `exact_binomial_rate_gate(S_t, N, R, confidence)` passes. The
last check treats every unobserved suffix result as a failure. It makes every
ordinary pass/fail completion satisfy the planned-size rule, so optional
stopping does not enlarge the fixed-`N` acceptance set. At `t = N`, acceptance
uses the count and exact gates; baseline and final validation also always
collect their complete configured sample counts.

After each observed candidate result, timeout or resource exhaustion takes
priority and rejects the prefix before an early-acceptance check. Once a prefix
stops, its remaining runs are not executed, so hypothetical resource failures
in that suffix are neither observed nor used to reverse the decision. The
confidence-sequence guarantee is per candidate unless run-wide control is
enabled.

With `--run-confidence C`, changed candidate family `j >= 1` is assigned

```text
alpha_j = min(1 - confidence, (1 - C) / (j * (j + 1)))
candidate_confidence_j = 1 - alpha_j
```

Both its confidence sequence and terminal exact gate use
`candidate_confidence_j`. Conditional on the reduction history, suppose the
fresh samples for every tested null candidate are iid Bernoulli oracle outcomes
with pass probability at most `R`. The worst-terminal guard makes early
acceptance a subset of planned-`N` exact acceptance, so family `j` has conditional
Type I error at most `alpha_j`. The union bound and
`sum_j 1/(j(j+1)) = 1` then bound the probability of any false candidate
acceptance in the session by `1-C`. Candidate families need not be independent;
the conditional per-family sampling model is the required assumption.

Family allocation occurs only after a mutation produces a changed tree and
before any candidate command runs. Parallel windows allocate in deterministic
candidate order. A separately tested combination and changed cache/duplicate
candidates receive new indices; no-ops do not. A persistent checkpoint records
the whole allocated window before sampling, conservatively classifying it as
aborted if the process never writes a later outcome checkpoint. Thus resume
never reuses alpha exposed to a command.

All allocation comparisons use exact fractions derived from configured binary
floats. The published candidate confidence is rounded toward one, so its actual
binary alpha never exceeds the nominal allocation. If alpha cannot be represented
below one, or the all-pass planned sample cannot clear the exact gate, reduction
fails closed. This is an inherent consequence of infinite-family control with a
fixed candidate sample count, not evidence that the reducer reached a fixed
point.

The report and persistent checkpoint retain the configured values and observed
rates/bounds. Baseline exact-gate evidence is stored separately as
`baseline_rate_evidence_runs`, `baseline_rate_evidence_passes`,
`baseline_exact_lower_bound`, `baseline_exact_p_value`, and
`baseline_exact_rate_gate_passed`. In exception-signature mode those counts
cover only samples after signature discovery; otherwise they cover the full
baseline sample. The pre-existing baseline counts, rate, and Wilson bound remain
full-sample descriptive metrics. All five exact-evidence fields are absent in
meaning (serialized as `null`) when no baseline rate gate is active. Restoring
a checkpoint recomputes the exact outputs from the persisted counts, configured
rate, and confidence before accepting them. Legacy non-signature checkpoints
can rebuild the evidence from their full-sample counts. A legacy signature
checkpoint without post-discovery counts fails closed because the discovery
position is not recoverable.

The CLI session identity and report carry a versioned
`candidate_sampling_policy`, preventing a resume from silently changing this
stopping contract. `candidate_early_acceptances` and `candidate_early_rejections`
separate the stopping directions, while `candidate_samples_saved` counts all
unexecuted candidate samples. Accepted mutation events retain their sample count
and Wilson evidence and add `oracle_anytime_lower_bound` plus the boolean
`oracle_early_acceptance`; no evidence is pooled across candidates. The separate
`candidate_family_control_policy` versions harmonic allocation. Checkpoints and
reports retain the run confidence, family count, cumulative nominal alpha upper
bound, and accepted events' family index, confidence, and actual binary alpha.
Resume recomputes these values and rejects inconsistent evidence. Ordinary final
validation uses the last accepted family's confidence, or base `--confidence`
when no mutation was accepted.

### Final holdout certification

The ordinary final validation reuses candidate thresholds and is a consistency
check, not independent evidence. With `--holdout-runs N` and
`--min-holdout-rate R`, ReproMin instead adds a distinct certification stage
after selection is complete. `--holdout-confidence C` controls this stage only
and defaults to `0.95`.

Let `S` be the number of complete oracle passes among the predeclared `N` fresh
samples and `alpha = 1 - C`. The one-sided Clopper-Pearson lower bound is

```text
L(0, N, C) = 0
P[Binomial(N, L(S,N,C)) >= S] = alpha,  S > 0
```

The published bound is found by conservative bisection. The actual gate avoids
a rounded-root comparison and evaluates

```text
P[Binomial(N, R) >= S] <= alpha
```

with integer-rational arithmetic relative to the configured binary floating-
point probabilities. The CLI rejects a plan when even `S=N` cannot pass. It
also records the smallest passing `S` as `required_passes`.

Before the first holdout command, the session removes ignored generated files
and freezes a tree fingerprint plus a digest of the failure specification,
learned failure signature, runner configuration, and session identity. All
`N` commands run sequentially in distinct copies of that same tree, bypassing
the candidate cache. There is no early acceptance or rejection. Timeout and
resource exhaustion count as non-passes and impose a hard certification veto.
Earlier baseline, candidate, combination, and final-validation results never
enter `S` or `N` and holdout commands never enter reducer phase counters.

The finite-sample coverage statement is conditional on the holdout observations
being iid after the artifact and protocol are frozen. Filesystem copies alone do
not establish this assumption: host caches, services, ports, time, load, and
network dependencies can correlate runs. The report therefore marks the iid
assumption as required but not verified. The certificate is an oracle pass-rate
statement for the recorded runner environment, not a probability that the code
is correct or a production failure-rate estimate.

Export copies the frozen payload without metadata and recomputes its complete
tree fingerprint before publication. The report and reproduction note are then
written to `OUTPUT.repomin`, outside the payload. This avoids the impossible
self-reference that would arise if certification evidence itself changed the
tree being certified.

A certified export is idempotent across a crash: an existing payload may be
reused only if its recomputed fingerprint equals the frozen artifact. A missing
sidecar is rebuilt, while an existing sidecar must be complete and equal to the
expected report and reproduction note (apart from resume provenance flags).
Partial or mismatched payload/metadata is rejected without resampling. An
uncertified session never treats an existing output as resumable state.

Both certified and non-holdout exports first freeze the accepted tree's
complete fingerprint. The payload is copied to a unique staging path in the
output's parent and that staged tree, including filesystem metadata, must match
the frozen fingerprint. ReproMin then publishes it with an OS no-replace rename
(`renameat2(RENAME_NOREPLACE)` on Linux, `renamex_np(RENAME_EXCL)` on macOS, or
the no-replace Windows `rename` behavior). This keeps publication on one
filesystem, prevents a race-created destination from being overwritten, and
ensures copy, fingerprint, or rename failures expose no partial output. Other
platforms fail safely when they cannot provide an equivalent primitive.

With `--java-exception`, passing baseline samples must also yield a stable
normalized root exception. The most frequent signature must meet the minimum
baseline threshold. The signature contains the exception class,
whitespace-normalized message, and up to three method frames. Source line
numbers and Java module prefixes are excluded. Candidates must match the
learned signature exactly. When independent Java failures are present, the
configured regular expression must identify a unique exception chain; an
ambiguous match fails closed instead of selecting an unrelated stack trace.

With `--python-exception`, passing baseline samples must also yield a stable
selected exception class, whitespace-normalized message, and up to three
innermost frames. Absolute working directories and line numbers are excluded.
The parser handles standard and chained tracebacks, exception-group leaves, and
pytest-rendered failures.
When output contains multiple exceptions, the configured regular expression
must identify a unique normalized block. A match found only in an unrelated
pytest node ID or summary is ambiguous and fails closed instead of falling back
to the first traceback. Baseline repetitions must agree before any candidate is
attempted.

The command runner extracts failure bodies from bounded Maven Surefire
`TEST-*.xml` files when this mode is enabled. Structured report data takes
precedence over console parsing and can also satisfy the configured regular
expression. Surefire environment properties are never collected.

Future oracles can add native crash signatures and run-wide confidence control
across adaptively selected candidate families. They must continue to distinguish
the requested failure from setup, compilation, timeout, and resource failures.

## Reduction session

`ReductionSession` owns a temporary current state. A reducer supplies a
mutation function, the session copies the current state, applies the mutation,
runs the oracle, and either accepts or discards the trial. On acceptance, the
session reconstructs the state from the clean parent and reapplies only the
mutation. Reducers never write directly to the source or final output.

Repeated baseline checks also receive distinct copy paths. Besides preventing
command writes from crossing runs, unique paths avoid stale bind-mount inode
caches when Docker Desktop observes a directory being deleted and recreated.

Candidate batches may be evaluated concurrently with `--jobs N`. Results are
consumed in candidate order, not completion order, so concurrency does not
change the deterministic base selection. Each command gets a separate repository
copy; external resources such as ports and databases remain shared.

If a reducer supplies a compatibility combiner and more than one candidate in a
parallel window passes, the session materializes their union as a new logical
candidate and runs the oracle again. A passing union is committed atomically. If
the union fails, the original lowest-index passing candidate is committed. An
oracle-positive candidate that is not committed is classified as `superseded`,
not rejected.

Before a command runs, the session hashes the candidate tree with the
domain-separated `tree-sha256-v2` policy. The canonical byte stream uses typed,
64-bit length-prefixed fields for paths, permission modes, nanosecond
modification times, entry types, regular-file contents, symlink targets,
filesystem flags exposed by `stat_result.st_flags`, and enumerable extended
attributes. It also includes the payload root's own metadata. Access time is
excluded because reading a tree
can change it; every copied or mutated command tree instead normalizes access
time to its preserved modification time. The resulting command result is
cached only for the current session. `--no-cache` disables reuse for failures
affected by state outside that tree.

The supported repository representation is deliberately closed: directories,
regular files, and relative symlinks resolving within the repository. Every
regular file must have `st_nlink == 1`; this rejects both in-tree hardlinks and
an otherwise invisible alias outside the tree. FIFOs, sockets, block devices,
character devices, and unknown entry types are rejected during the source
preflight and again before critical fingerprints, so a copy or command cannot
silently observe a tree with semantics the session cannot reproduce. Windows
non-symlink reparse points, including junctions, are rejected before recursion
for the same reason. Mutation-blocking filesystem flags are also outside the
representation: immutable, append-only, and host-enforced no-unlink BSD flags
exposed through `stat_result.st_flags` are rejected before copying or sampling.
Other copied BSD filesystem flags remain supported and contribute to the
canonical fingerprint. Linux inode flags managed through ioctls such as
`chattr +i` are not exposed by Python's `stat_result` and remain outside this
representation.

Command copies are tool-owned but can be mutated after their pre-command
fingerprint. Cleanup therefore removes flags without following symbolic links,
and only changes regular-file or symlink inode metadata when `st_nlink == 1`.
A protected multiply-linked inode is not modified because an alias may be
outside the command copy; cleanup fails explicitly instead of silently leaking
the private directory or changing external state.

Every changed candidate is canonicalized before its command: ignored generated
entries are removed, pre-existing paths recover their original modification
times, and new paths receive the copied root's modification time. A candidate
whose canonical fingerprint equals the current tree is a no-op. The
pre-command fingerprint is retained even when cache reuse is disabled. If the
oracle accepts it, ReproMin reapplies the mutation to a clean copy, repeats the
same canonicalization, and requires the fingerprint to match before the
directory swap. This prevents nondeterministic mutations or ignored artifacts
from promoting a tree that the oracle never observed.

The host execution layout gives each command a unique wrapper directory and
places the repository at `wrapper/OUTPUT.name`. This applies uniformly to
baseline samples, single and repeated candidate samples, final validation, and
holdout samples. `host-output-basename-v1` and the stabilized basename are
recorded in the session identity and report. Wrapper parents, absolute paths,
inodes, and devices are intentionally not stable, so commands that depend on
them must use Docker's fixed `/workspace` working directory or provide their
own controlled environment.

### Input exclusions

The session starts from a closed repository representation. Built-in generated
and dependency directory basenames are excluded during the initial copy and
every later canonicalization. The CLI may add exact basenames with repeatable
`--ignore NAME` options. It may also add exact repository-relative paths with
repeatable `--ignore-path RELATIVE_PATH`; the selected path and all descendants
are excluded without affecting same-named paths elsewhere. Both forms apply to
files and directories; values are ordinary path segments, not glob expressions.
The CLI may also add explicit gitignore-style rule files with `--gitignore` and
repeatable `--gitignore-file PATH`. Rule files are parsed into an ordered
matcher that supports comments, blank lines, negation, trailing-slash directory
rules, leading-slash anchoring, `*`, `**`, `?`, and character classes. Their
rules run after exact exclusions, so negation can only restore an entry that an
earlier rule-file entry removed. Ignored entries are absent from source
fingerprints, candidate workspaces, phase byte accounting, and exports. The
sorted effective basename and path sets and the rule-file digest are persisted
in the report and session identity. A resume with a different set or changed
rule-file content is rejected before any oracle command runs.

With `--gitignore-recursive`, ReproMin also reads nested `.gitignore` files in
top-down directory order. Each rule is scoped to the directory that contains
its file: the relative path supplied to the matcher is made relative to that
scope before anchoring. Negation in a nested file therefore applies only within
that subtree. Directories excluded by the built-in or exact ignore sets are not
descended into and cannot contribute a nested rule file. The
the collected rules. Directories excluded by the built-in, exact ignore, or
already-applied gitignore rules are not descended into and cannot contribute a
nested rule file. The `gitignore_recursive` boolean, sorted file list, and
content digest are part of the persistent session identity, so resuming with a
changed nested rule set is rejected before any oracle command runs.

Repeatable `--keep RELATIVE_PATH` protects an exact file or directory (and all
descendants) from the file reducer. It uses the same path grammar as
`--ignore-path`, accepts no glob syntax, and is recorded in the report and
session identity. Keeping a path prevents deletion but does not prevent source
or manifest reducers from editing files inside a kept directory.

### Reproduction environment

Repeatable CLI `--env NAME=VALUE` entries are parsed into a unique environment
mapping before the runner is built. Host commands receive the overrides on top
of the inherited environment; Docker commands receive only the explicit
overrides in addition to ReproMin's fixed `REPOMIN` and `HOME` variables. Names
must use the portable shell/container form `[A-Za-z_][A-Za-z0-9_]*`, duplicate
names are rejected, and `REPOMIN` is reserved for ReproMin's internal marker.
The mapping's sorted names and SHA-256 digest are recorded
in the report and persistent identity, while values are deliberately absent
from reports and checkpoints. Resume therefore detects value drift without
turning a session checkpoint into a secret store.

### Persistent checkpoints

`--session PATH` changes the disposable in-memory session into a durable
working directory containing `state.json` and `workspace/current`. The state
file records:

- the source tree fingerprint and a canonical copy of all CLI settings that
  affect the oracle or reducers;
- the ordered canonical Java attribution classpath and a content fingerprint
  for every classpath file or directory;
- the last accepted tree, baseline result, learned Java/Python signature,
  counters, events, and completed reduction phases;
- the phase that was active when the checkpoint was written;
- the saved final-consistency result and versioned holdout plan, in-flight slot,
  append-only sample summaries, aggregate counts, and terminal outcome.

Checkpoint writes use a temporary file followed by an atomic rename. They are
performed after baseline verification, every accepted mutation, and each
completed phase. Candidate copies are discarded before the next checkpoint.
Promotion of an accepted tree uses a directory swap; if the process dies in
the middle of that swap, resume repairs or rolls it back before validating the
checkpoint. A source or configuration mismatch is an explicit error, because
reusing a tree under a different failure command can produce a misleading
reproduction.

Checkpoint schema 3 records `tree-sha256-v2` explicitly and writes a holdout
slot as in-flight before creating its
sample copy or starting its command, then records the result before advancing.
If termination occurs inside that window, resume permanently records the slot as
an interrupted non-pass and continues with the next index; it never silently
resamples the same slot. `certified`, `not_certified`, and `aborted` are terminal
for that session. A terminal resume is idempotent and cannot implement
repeat-until-pass. Schema-1 and schema-2 sessions used the ambiguous v1 tree
encoding and are rejected before workspace recovery; their saved fingerprints
and historical samples are never reclassified under v2.

Classpath entries are validated and fingerprinted inputs, not session-owned
snapshots. A relative `--java-classpath PATH` is fixed against the original
source directory before any repository copy is made. Resume recomputes the
fingerprints and rejects a changed entry, entry order, or content; external
entries are not preserved or restored by the session. A top-level symlink is
strict-resolved to its canonical target. Directory fingerprints
recursively include relative names, entry types, permission bits, and file
contents; nested symlinks and special files are rejected. The analyzer also
recomputes every entry fingerprint before each compiler pass and aborts if the
external state changed after initial validation.

`--resume` requires an existing `--session PATH`. It restores the saved oracle
signature and skips phases already marked complete; an interrupted phase is
replayed from its last accepted tree. Persistent sessions are intentionally
outside the exported output and should be retained only as long as needed.

Both the sampling policy and `reduction_strategy` are versioned session inputs.
A checkpoint with a missing or different current strategy is deliberately
incompatible: reductions performed under a weaker fixed-point contract cannot
be relabeled as completed work under a newer one.

### Fixed points and accounting

Structured reducers propose deterministic hierarchical batches. They begin with
the broadest compatible target set, split rejected batches, and rediscover the
tree after every acceptance. Every proposed batch remains a normal transaction
with its own oracle evaluation; no target is accepted merely because another
member of its hierarchy passed.

The Java reducer uses rejection epochs rather than immediately retrying every
stable rejected target after each accepted edit. A target's semantic key is
suppressed only for the remainder of that epoch. If any Java edit is accepted,
a new epoch reanalyzes the tree and reconsiders all surviving keys. Java is
locally stable only when a complete epoch accepts nothing. The file reducer
similarly closes the non-monotonic dependency between directory and file
deletions before returning.

The CLI runs locally stable reducers through a dirty worklist. An acceptance by
one component marks every other component dirty. A component need not be queued
for its own changes because local stability is its reducer contract. The queue
is empty only after all components have run since the most recent external
change, which is the global fixed point.

`--max-attempts N` changes the terminal condition from global fixed point to
logical-attempt budget. Candidate preparation checks the accumulated logical
attempt count before each window and stops once `N` is reached. The final
validation and optional holdout still run against the latest accepted tree.
The report exposes `max_attempts` and `budget_exhausted`, and the value is part
of session identity so resume cannot silently change the bound.

`--max-duration SECONDS` applies the same candidate-only budget in wall-clock
time. The reduction start timestamp is stored in the checkpoint, so a resumed
session continues with the remaining budget measured from the original start.

The versioned `phase_statistics` block is additive to report schema 1. For each
phase it records pass counts and timing, net regular-file byte changes, logical
candidate classifications, and oracle use. The accounting identities are:

```text
attempts = no_op + rejected + accepted + superseded + aborted
oracle_sample_uses = oracle_samples + cache_hits
```

`oracle_sample_uses` is logical demand for evidence. `oracle_samples` is actual
command execution, excluding cache reuse; baseline, final validation, and
holdout certification are not charged to reducer phases. `samples_saved` is the unexecuted suffix from repeated
candidate early stopping. `oracle_seconds` sums the durations reported by actual
command samples and may exceed wall time when samples run concurrently.

Wall time covers active reducer passes, not downtime between resume operations.
Byte accounting records net regular-file bytes removed or added within each
pass. `coverage=partial` means some phase history was unavailable or a pass was
interrupted; it does not weaken candidate oracle acceptance. On restore, a saved
active pass is closed as aborted, while its incomplete timing/byte history keeps
coverage partial. Missing legacy phase counters are never synthesized.

## Execution backends

The host runner does not release a command until it is registered for
cancellation. On POSIX, a gate pipe holds a new process group before `exec`; on
Windows, the process is created suspended, assigned to a fail-closed Job Object
with `KILL_ON_JOB_CLOSE`, registered, and then resumed. Timeout, resource
failure, interruption, or a parallel-worker exception first cancels every
active command in the window. POSIX sends SIGTERM to each group and follows
with SIGKILL after a grace period; Windows terminates the Job. A per-process
completion event gives cancellation one owner and prevents trial cleanup from
racing command cleanup. Ordinary background children are also terminated when
the command leader returns.

Stdout and stderr are drained continuously from pipes into a shared bounded
memory buffer. POSIX uses non-blocking reads and Windows uses reader threads.
Their combined size is hard-gated at 64 MiB; overflow immediately becomes a
resource failure and closes the read ends. Closing those ends prevents a POSIX
descendant that deliberately escapes with `setsid()` from retaining or filling
ReproMin's output storage. Process groups cannot contain that re-sessioned
process, so it may still survive and affect host state. This is an explicit
host-backend boundary, not a sandbox guarantee.

The Docker runner uses the Docker CLI without an SDK dependency. It validates
the daemon and local image before reduction, resolves the user reference to a
canonical `sha256:...` image ID, disables automatic pulls, and starts every
hardened container by that ID. The reference and resolved ID are both part of
the checkpoint identity and report; resume resolves the reference again and
rejects drift before executing a sample. A name derived from the per-run cidfile
path is fixed before `docker run`, so cleanup does not depend on the cidfile
having been written. After terminating the Docker client, ReproMin issues
`docker rm -f` by ID when available or by the known name and retries for a
bounded daemon-settling period.

Optional CPU and memory budgets map to Docker cgroup limits, with memory and
swap capped together. PID count and `/tmp` tmpfs size have configurable bounded
defaults. A host-side monitor samples the logical size of the writable bind
mount and destroys the container when `--docker-workspace-limit` is crossed.
Timeouts, workspace overruns, and status 137 under a memory limit are marked as
non-oracle resource failures even when their output matches the requested text.

Docker Desktop cannot mount the macOS system temporary directory reliably, so
Docker sessions are created beside the source repository. They remain siblings
of the input and never become part of the copied repository.

Java structure analysis remains a host-side operation for both backends.
`--java-classpath` is therefore neither added to the oracle command nor given a
separate Docker mount. Only host-readable paths are accepted, even when the
reproduction command runs in Docker. An entry already below `SOURCE` may still
appear through the container's ordinary candidate-repository mount.

## Reducers

The generic file reducer uses hierarchical delta debugging. It attempts larger
directory and file groups first, then increases granularity when a group
contains required content. Directory depths are revisited after nested or file
deletions when those changes can unlock an earlier rejected directory.

The Maven reducer parses `pom.xml` and currently exposes modules, dependencies,
plugins, and properties as removable targets.

The Gradle reducer uses a purpose-built lexer for Groovy and Kotlin DSL files.
It balances strings, comments, parentheses, brackets, and closures before
identifying statements in known blocks. It reduces project includes,
dependencies, plugins, repositories, configurations, empty blocks, and logical
lines in `gradle.properties`. Every text range carries a content hash so a
position discovered before another accepted edit cannot modify shifted text.

The Python manifest reducer lexes TOML strings, comments, tables, arrays, and
inline tables without requiring Python 3.11's `tomllib`. It exposes PEP 621,
Poetry, PDM, dependency-group, uv, and build-system dependency declarations as
hashed text ranges. It also reduces complete logical lines in requirements
files, follows local requirement and constraint includes within the repository,
and treats backslash continuations atomically. Symlinked manifests and include
targets are not followed.

The Pipenv manifest reducer scans non-symlinked files named exactly `Pipfile`
and reuses the same strict TOML lexer. It exposes direct assignments in
`[packages]`, `[dev-packages]`, and `[requires]` as whole-statement,
content-hashed targets. `[[source]]` settings, arbitrary tables, and
`Pipfile.lock` remain untouched; malformed or stale ranges fail closed.

The Node manifest reducer parses each valid npm-compatible `package.json` with
its own strict JSON structure parser. It exposes dependency, development,
optional/peer dependency, script, workspace, file/bundle, `resolutions`, and
`overrides` members as comma-aware hashed text ranges. It rejects duplicate
keys and non-standard JSON constants, never follows symlinked manifests, and
deliberately leaves lockfiles, `exports`, `imports`, and `engines` untouched.
Package-manager resolution remains an explicit user command and the complete
oracle decides whether each structural removal is accepted.

The Composer manifest reducer reuses the strict JSON parser for each valid
`composer.json`. It exposes top-level `require`, `require-dev`, `replace`,
`conflict`, `provide`, and `scripts` object members plus `repositories` array
entries as content-hashed ranges. It rejects duplicate keys and non-standard
JSON constants, never follows symlinked manifests, and deliberately leaves
autoload maps, arbitrary `extra` metadata, and `composer.lock` untouched.
Composer dependency resolution remains an explicit user command and the
complete oracle decides whether each structural removal is accepted.

The MSBuild manifest reducer scans non-symlinked `.csproj`, `.fsproj`, and
`.vbproj` project files plus non-symlinked `Directory.Build.props` files with
the hardened XML parser. It exposes `PackageReference`, `ProjectReference`,
`FrameworkReference`, `Compile`, `EmbeddedResource`, `Content`, and `None` items
with an `Include` attribute as identity- and content-hashed targets. It leaves
property groups, imports, conditions, arbitrary metadata, and lockfiles
untouched. A target is removed only after its current XML subtree hash and
ordinal match the discovered identity; XML parse or stale-identity failures are
fail-closed. Documents containing `DOCTYPE` or `ENTITY` declarations are
rejected before parsing to avoid expanding untrusted XML entities.

The Ruby manifest reducer scans non-symlinked `Gemfile`, `gems.rb`, and
`Gemfile.*` files except `Gemfile.lock`. It exposes only complete, single-line
`gem` calls whose strings and brackets balance and whose line has no block or
continuation. Comments, strings containing `gem`, multiline calls, arbitrary
Ruby code, and lockfiles remain untouched. Targets are full-line,
content-hashed ranges; unbalanced or dynamically named calls are ignored rather
than guessed.

The Cargo manifest reducer reuses the strict TOML lexer and exposes dependency,
development-dependency, build-dependency, target-specific dependency, and
workspace `members`/`exclude` entries. It only scans `Cargo.toml`, rejects
symlinked manifests, and leaves `Cargo.lock`, features, patch metadata, and
other arbitrary tables untouched. Dependency table entries are removed as
whole TOML statements so inline tables and target predicates remain intact.

The Go manifest reducer scans `go.mod` and `go.work` line structure. It exposes
balanced `require`, `replace`, `exclude`, and `retract` entries from `go.mod`,
and `use` plus workspace-level `replace` entries from `go.work`, as full-line,
content-hashed targets. Module declarations, `go`/`toolchain` directives,
`go.sum`, and other workspace metadata remain untouched. An unclosed block
disables targets for that file rather than guessing at directive boundaries.

Additional manifest adapters should use a structured parser and stable target
identity; regular-expression text replacement is not an acceptable manifest
mutation strategy.

The native Java reducer compiles its analysis helper with `javac --release 11`
and runs it with the installed JDK 11+ compiler API. The release setting fixes
the helper's bytecode and API compatibility floor; it does not select the
source release of the project being reduced.
The helper reports UTF-8 byte ranges for imports, type members, statements,
annotations, parameters, and invocation/constructor/array arguments. It also
reports AST-backed replacement ranges for binary operands, conditional
branches, cast/unary operands, and bounded synthetic replacements for literals.
All remaining Java source paths are passed to one compiler task through a
NUL-delimited file list, avoiding command-line size limits and preserving
cross-file symbol identity. After parsing, the helper requests compiler
attribution and links eligible source-local `ExecutableElement` declarations to
resolved method and constructor calls. Eligible declarations are constructors,
non-native `static` or `private` methods, and closed-dispatch instance methods.
The latter must belong to an ordinary top-level or member class and either the
method or its declaring class must be `final`. A source-local override family
may also be coordinated when it has one package-visible root, no external
override or interface contract, and a final leaf method or owner. Enum, record,
local, and anonymous owners are excluded from instance-method support. External
overrides and interface implementations remain rejected even when dispatch is
otherwise final.
For each prospective parameter removal, the helper also rejects a method when
the reduced signature would newly override or implement an inherited contract,
or would clash after generic substitution or type erasure and require or
conflict with a bridge method.

For an unused parameter the helper emits records containing the comma-aware
declaration range and every corresponding call argument range. Removing a
varargs parameter removes the complete trailing argument range at each call.
Parameters referenced in the executable body and executables used through a
direct method or constructor reference emit blockers. A `new T(...) { ... }`
anonymous-class expression emits blockers for every source constructor of `T`
instead of a linked argument record, because its resolved executable is the
synthetic anonymous-class constructor. Record constructors are also excluded.
Python assembles a `JavaChangeSet` when a symbol group has one or more source
declarations, at least one resolved call edit, and no blocker. This allows all
declarations in a closed source-local override family to be changed atomically.
Native methods,
open virtual methods, unsafe hierarchy cases, unresolved calls, and external
executables are not linked.

Each `--java-classpath PATH` occurrence is one atomic compiler classpath entry;
it is never split on the platform path separator. Entries are canonicalized
relative to the original source directory, validated as readable existing
regular files or directories, checked for aliases of the same physical target,
and kept in CLI order. The helper supplies these paths directly to the compiler file
manager. They are not appended to the classpath that launches the helper, and
ReproMin never invokes a build tool to discover them. ReproMin does not impose
an archive extension or ZIP-format check on regular files; javac remains the
authority on whether an entry is a usable compiler classpath artifact.

Only compilation units from the explicit NUL-delimited source set are scanned
for declaration records, and Python discards every coordinated group that does
not contain at least one such declaration. A binary `ExecutableElement` resolved
from the external classpath can improve type and overload attribution, but it
cannot supply that source declaration role or form a coordinated source group.
The canonical host entry is never a mutation target. If it is below `SOURCE`,
its copied candidate counterpart remains subject to normal file reduction; the
analyzer continues to read the fixed original host entry.

It never edits source. ReproMin hashes each selected range and includes the
replacement bytes in target identity, applies one candidate change in a trial
repository, and commits it only through `ReductionSession`. Adjacent AST
positions, rather than token regular expressions, define comma-aware list
removals. A coordinated group is materialized in a trial only after every path,
range, hash, and non-overlap invariant is validated; edits are then applied in
descending offset order per file. Fully contained empty replacements from
nested recursive calls are deduplicated. Any stale or partially overlapping
range rejects the whole group before a file is written.

Symbol grouping has an analysis-wide attribution safety gate. An I/O or compiler
runtime exception during attribution, any compiler error other than the
explicitly recoverable missing-package and unresolved-symbol diagnostics, or an
`ERROR` type in a source type hierarchy or anywhere on a method-invocation,
constructor-expression, or member-reference path disables every coordinated
candidate for that analysis pass. Syntax-only targets are still emitted, and an
accepted syntax mutation causes the next pass to analyze the new tree again.
Recoverable unresolved symbols are never linked; other local groups may be
emitted only when no examined hierarchy or call path contains an `ERROR` type.
Within a passing analysis, each coordinated instance-method candidate also
requires resolved source symbol identity, an available complete hierarchy,
eligible owner and dispatch kinds, and successful current and prospective
override, implementation, generic-substitution, erasure, and bridge checks.

Direct Java method and constructor references are AST-visible compiler
blockers. Reflection such as `Class.getDeclaredMethod`, string- or
`MethodType`-based `MethodHandles.Lookup` operations such as `findVirtual` and
`findSpecial`, generated sources, JNI and framework calls, and precompiled
external callers that retain the old descriptor are not statically closed. They
remain the reproduction oracle's responsibility. The oracle alone decides
whether to accept a materialized mutation, but it preserves only the configured
command, exit behavior, output match, and optional failure signature. It does
not establish source compatibility, ABI compatibility, compilation, or
unexercised behavior. Reproduction commands should compile and test the
affected code and use `--java-exception` when exception identity matters.

The Python source reducer parses each `.py` file with the standard-library
`ast` module. It exposes imports, decorated definitions, and nested statements
as candidates. Python AST columns are UTF-8 byte offsets, so the reducer maps
them back to character offsets before hashing and editing; a syntax error or
stale range is skipped. It never performs token or regular-expression source
replacement.

The opt-in text reducer targets only the exact repository-relative paths given
with repeatable `--text-file RELATIVE_PATH`. It splits each selected UTF-8 text
file into newline-preserving line ranges and reuses the same interval-batch
scheduler plus `remove_text_targets` validation as the structured manifest
reducers. Binary files and files that fail UTF-8 decoding are skipped. Because
line offsets are content-hashed and the tree is re-scanned after each accepted
batch, a stale range is fail-closed rather than guessing at shifted text.

Maven batches locate every selected XML node before deleting any node and
serialize each affected POM once. Gradle and Python text batches validate every
path, range, hash, and overlap before the first write, and roll back all files on
a recoverable write failure. These atomicity rules precede oracle validation;
they do not replace it.

### Semantic reducer seam

The semantic reducer is an opt-in, provider-agnostic extension point rather than
a built-in language adapter. `SemanticBackend` exposes only `name` and
`propose(session) -> Sequence[MutationCandidate]`; `SemanticReducer` feeds those
candidates through the same `ReductionSession.try_mutations` pipeline as every
deterministic reducer. The oracle remains the single acceptance authority, so a
backend can never promote an edit that stops reproducing the configured failure.

The default backend is `NoopSemanticBackend`, which returns no candidates and
keeps a default run byte-for-byte equivalent to a run without the seam.
`HttpSemanticBackend` is the built-in OpenAI-compatible adapter. It uses only
the standard library, reads its bearer token from `REPOMIN_SEMANTIC_TOKEN`
(never from `argv` or reports), and requires an explicit endpoint and model via
`--semantic-endpoint` / `--semantic-model`. Its response contract is a JSON
object containing `choices[0].message.content`; that content must parse as an
`edits` array of either `{path, replace}` or `{path, delete}` edits. Paths are
validated as safe repository-relative paths before any mutation is materialized.

Because the global scheduler is a dirty worklist, an accepted semantic edit
requeues the deterministic reducers. This forms the same
syntax-then-semantic-then-syntax alternation described by LPR without importing
an LLM runtime. Checkpoints and reports record `semantic_reducer`,
`semantic_model`, `semantic_endpoint`, `semantic_calls`, and
`semantic_accepted`; the session identity includes the semantic configuration,
so a resumed run rejects a changed provider or model.

## Report and checkpoint fields

The sibling `OUTPUT.repomin/report.json` is the user-visible accounting surface.
Its top level records the original `command`, `failure_match`, baseline and
final exit codes, source/output file and byte counts, `attempts`,
`accepted_mutations`, and `cache_hits`. The `execution` block records the
reduction configuration and provenance, including the input-control knobs
(`ignored_names`, `ignored_paths`, `gitignore_files`, `gitignore_sha256`,
`gitignore_recursive`, `keep_paths`, `max_attempts`, `max_duration_seconds`,
`budget_exhausted`) and the opt-in semantic reducer fields (`semantic_reducer`,
`semantic_model`, `semantic_endpoint`, `semantic_calls`, `semantic_accepted`).
Secrets are never written: explicit environment variables appear only as sorted
names plus a SHA-256 digest, and the semantic bearer token is never stored.

`phase_statistics` carries one entry per reducer phase with additive counters
`attempts`, `no_op`, `rejected`, `accepted`, `superseded`, and `aborted`, plus
wall-clock and byte accounting and oracle sample/cache counters. The identity

```text
attempts = no_op + rejected + accepted + superseded + aborted
oracle_sample_uses = oracle_samples + cache_hits
```

holds for complete phases. `events` preserves one record per accepted mutation
with its oracle evidence and, when `--run-confidence` is enabled, its candidate
family index, confidence, and spent alpha.

Persistent `state.json` uses `schema_version: 3` and stores the tree-fingerprint
policy, source and current fingerprints, the reduction identity, the serialized
`ReductionStats`, baseline/final/holdout state, and the oracle checkpoint. A
resume fails closed when the source fingerprint, current fingerprint, identity,
or any validated statistical evidence is inconsistent. The identity includes
the command, matching configuration, backend and Docker settings, environment
digest, ignore/keep/gitignore rules, budget knobs, and the semantic provider
configuration, so changing any of them on `--resume` is rejected.

## Next technical milestones

1. Coordinated Java reductions for true override families, anonymous-class
   construction, record constructors, and direct method and constructor
   references.

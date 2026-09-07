# Technical pilot: Gradle composite plugin-build cycle

This case study records a maintainer-led local pilot completed on 2026-09-07
for [gradle/gradle#38843](https://github.com/gradle/gradle/issues/38843). The
issue already contained a valid public reproduction, but recreating it required
two complete repositories and a manual settings change. The goal was to test
whether ReproMin could turn that workflow into smaller, replayable evidence.
It was not a root-cause investigation, an upstream submission, or evidence of
non-maintainer adoption.

## Source and policy boundary

The prepared source combined these Apache-2.0 snapshots:

- `autonomousapps/dedebug` commit
  `d26e30980489a316fb824c52c217ef7aaf5d942b` from branch
  `trobalik.gradle-issue-38843`; and
- `autonomousapps/dependency-analysis-gradle-plugin` commit
  `93b52302ef0f48ad7aa0776ae2a1e479a9055f32`.

The only upstream-source edit enabled the composite build in
`dedebug/settings.gradle.kts`:

```kotlin
includeBuild("../dependency-analysis-gradle-plugin")
```

The pilot root added an oracle script, a provenance record, and generated-file
ignore rules. Both upstream licenses were protected and retained. Gradle's
contribution policy requires a human to understand an AI-assisted submission
and sign its DCO. No comment or pull request was sent upstream; the project
owner must review the artifact and disclosure before any delivery.

## Oracle contract

The `reproduce.sh` oracle runs `dedebug/gradlew :help`, unsets `CI`, and emits
`REPOMIN_GRADLE_38843_TARGET` with exit code 23 only when Gradle reports all of
these signals:

- failure to resolve `com.gradle.develocity` version `4.2.2`;
- the exact
  `:dependency-analysis-gradle-plugin -> :dependency-analysis-gradle-plugin`
  plugin-build cycle; and
- the statement that this cycle is unsupported with Isolated Projects.

The script also requires active, non-commented settings for the outer
`includeBuild`, `org.gradle.isolated-projects=true`, the included build name,
and its `includeBuild(".")` self-include. Negative controls confirmed that a
disabled outer include, a disabled self-include, and an unrelated plugin
resolution failure cannot emit the target marker.

The default command permits dependency downloads so a new user can start from
an empty Gradle cache. `REPOMIN_GRADLE_OFFLINE=1` is available for repeated
runs after warming the same checkout, but the pilot does not claim that a
single warm-up makes newly copied paths work offline.

## Reduction workflow

The source contained 854 reduction-visible files and 3,817,947 bytes. The run
used the `v0.1.0.dev10` GitHub Release wheel, macOS ARM64, OpenJDK 17.0.20, the
Gradle 9.7.0 wrapper, and the host backend. The oracle, provenance record, both
licenses, and the outer Gradle wrapper were protected. Source reduction was
disabled.

A global 80-attempt budget was consumed by the Gradle structured reducer
before file reduction could start. Because the budget is part of session
identity and cannot be raised on resume, the accepted tree became the input to
a separate file-only run. A third file-only run reached a fixed point, and a
short final run re-certified the portable oracle change.

| Stage | Files before | Files after | Bytes before | Bytes after | Attempts | Accepted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Gradle model | 854 | 854 | 3,817,947 | 3,806,016 | 80 | 9 |
| File reduction | 854 | 20 | 3,806,016 | 121,130 | 80 | 13 |
| File fixed point | 20 | 11 | 121,130 | 87,521 | 66 | 6 |
| Portable re-certification | 11 | 11 | 87,862 | 87,862 | 19 | 0 |

The final presentation is 341 bytes larger than the preceding fixed-point
payload because the protected oracle and provenance text were updated to
support and explain first-run online resolution. Across the prepared source
and final presentation, 843 files and 3,730,085 bytes were removed, leaving
about 2.3% of the original bytes.

The final 11 files are the oracle and provenance record, two license files,
the outer wrapper script/JAR/properties, and the two builds' settings and
properties files. ReproMin could not remove another eligible file while
preserving the configured failure and protected-file boundary.

## Validation and portability

`report validate` confirmed the exact final payload fingerprint. With a cache
populated by the cold-start run, `report replay` reproduced the recorded
match-and-exit-code oracle in 5 of 5 online-enabled fresh copies; each took
between 3.86 and 4.17 seconds. A separate first run with an empty Gradle user
home downloaded its requirements and reproduced the target in 96 seconds.

The trial also exposed a cache boundary worth preserving in the record. Twice,
a Doctor run started immediately after a reduction or export reported zero
passing fresh copies; subsequent direct or replay runs succeeded, followed by
Doctor results of 3/3 and 5/5. A cache warmed in one checkout also did not make
freshly copied paths reliable with `--offline`. Final evidence therefore uses
online-enabled fresh-copy replay, and the artifact does not claim a
network-free cold start.

## Product lessons

- A strict marker plus exact exit code rejected ordinary Gradle failures; the
  active-line preconditions prevented commented settings from passing setup.
- Build-tool caches are part of the reproducibility boundary. A successful
  warm run is not evidence that a copied checkout will work offline.
- A global budget can be consumed by an early structured reducer. Since a
  completed bounded session cannot accept a larger resume budget, practical
  pilots may need staged runs and separate reports.
- Aggregate Doctor output reports pass counts but not per-sample mismatch
  reasons, which made transient baseline failures harder to diagnose.
- `--gitignore-recursive` required a root `.gitignore` for this directory that
  combined two repositories, even though both nested repositories had their
  own files.
- The release wheel installed correctly from GitHub Releases, while neither
  the configured package mirror nor official PyPI exposed this project. That
  extra installation URL is adoption friction.

These are observations from one maintainer-run workflow, not sufficient reason
on their own to add new budget, cache, or diagnostic features. The roadmap's
two-independent-trial threshold still applies.

OpenAI Codex performed the local reproduction, oracle construction, reduction,
and documentation under delegated repository maintenance. Any future upstream
message must disclose that material involvement and be reviewed and sent by a
human who can make the required authorship and DCO attestations.

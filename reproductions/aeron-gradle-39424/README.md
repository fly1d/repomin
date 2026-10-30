# Replay a reduced public Gradle failure

This kit contains **27 original Aeron files / 166,731 bytes** that reproduce
[Gradle #39424](https://github.com/gradle/gradle/issues/39424). You can inspect
the [payload](reference-result/payload) and [source provenance](provenance.json)
without downloading the original 1,733-file repository. 中文说明见
[README.zh-CN.md](README.zh-CN.md).

A maintainer-run ReproMin dev15 reduction removed files from **1,733 files /
21,010,709 bytes to 27 files / 166,731 bytes** in four accepted attempts.
The remaining 27 files were protected in advance; this is not a minimality
claim. The included native report is a later **27 → 27, zero-edit** check of
that result. It records a command starting with `gradle`, so it does not depend
on the original runner's temporary installation path.

## Run the checked example

The measured environment is **Ubuntu Linux, Bash, Temurin JDK 25.0.5,
Gradle 9.8.1-bin and the published ReproMin 0.1.0.dev15**. Set `JAVA_HOME` and
put Gradle and `repomin` on `PATH`; Python 3.9+ and standard Unix utilities
are needed by the commands below. Check `gradle --version`,
`"$JAVA_HOME/bin/java" -version` and `repomin --version` first. Install ReproMin
using the existing [quick start](../../docs/QUICKSTART.md). JDK, Gradle and
dependency caches are not bundled. The first Gradle run needs network access.
Other operating systems and runtime versions have not been checked here.

From the **ReproMin repository root**, run this block. It creates a new directory
under `/tmp`, keeps a clean payload for report validation, and runs Gradle in a
separate `warmup` copy. The ancestor check matters: Aeron's build helper searches
for a parent `.git` directory, so executing directly inside this checkout could
use ReproMin's Git history instead of the intended source context.

```bash
bash <<'CASE'
set -eu
: "${JAVA_HOME:?Set JAVA_HOME to Temurin JDK 25.0.5}"
case_kit="$PWD/reproductions/aeron-gradle-39424/reference-result"
case_java="$JAVA_HOME"
case_gradle_bin="$(dirname "$(command -v gradle)")"
case_repomin="$(command -v repomin)"
case_work="$(mktemp -d /tmp/aeron39424.XXXXXX)"
printf 'Evidence directory: %s\n' "$case_work"
python3 - "$case_work" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
if any((p / ".git").exists() or (p / ".git").is_symlink()
       for p in [root, *root.parents]):
    raise SystemExit("Choose a temporary location with no .git ancestor")
PY
cp -Rp "$case_kit/payload" "$case_work/payload"
cp "$case_kit/report.json" "$case_work/report.json"
cp -Rp "$case_work/payload" "$case_work/warmup"
mkdir -p "$case_work/home" "$case_work/gradle-home" "$case_work/maven-local" "$case_work/tmp"
case_env() {
  env -i HOME="$case_work/home" JAVA_HOME="$case_java" BUILD_JAVA_HOME="$case_java" \
    JAVA_TOOL_OPTIONS="-Duser.home=$case_work/home -Dmaven.repo.local=$case_work/maven-local" \
    GRADLE_USER_HOME="$case_work/gradle-home" TMPDIR="$case_work/tmp" \
    PATH="$case_gradle_bin:$case_java/bin:/usr/bin:/bin" LANG=C.UTF-8 TERM=dumb "$@"
}
case_status=0
(cd "$case_work/warmup" && case_env gradle copyTestLogs --rerun-tasks \
  --console=plain --warning-mode=fail --no-scan --no-daemon \
  --max-workers=2 --no-build-cache) > "$case_work/warmup.log" 2>&1 || case_status=$?
if [ "$case_status" -ne 1 ]; then
  printf 'Expected Gradle exit 1; inspect %s/warmup.log\n' "$case_work" >&2
  exit 1
fi
grep -Fq "Task ':copyTestLogs' uses this output of task ':buildSrc:compileJava' without declaring an explicit or implicit dependency." "$case_work/warmup.log"
grep -Fq 'BUILD FAILED' "$case_work/warmup.log"
case_env "$case_repomin" report validate "$case_work/report.json" \
  --payload "$case_work/payload" --json > "$case_work/validation.json"
case_env "$case_repomin" report replay "$case_work/report.json" \
  --payload "$case_work/payload" --runs 2 --yes --json > "$case_work/replay.json"
cat "$case_work/replay.json"
printf 'Replay verified in two fresh copies.\n'
CASE
```

Success means the block exits **0**, and the replay JSON contains
`"reproduced": true` and `"passes": 2`. Each replay uses a fresh copy and the
recorded strict oracle. The initial Gradle failure must exit **1** and include
both the complete implicit-dependency diagnostic and `BUILD FAILED`; a network
or setup failure does not count. Inspect `warmup.log` if the block stops.

Do not build in the clean `payload` directory: generated `.gradle` and `build`
entries would change its fingerprint. Keep the workspace to inspect the evidence;
remove it when finished. These commands execute the trusted fixture and Gradle
on the host. `--yes` acknowledges executing the report's reviewed command.
Offline replay and private caches are not a network sandbox. Local result files
can contain paths and command output; inspect them before sharing.

## Evidence and boundaries

The [selected receipt](reference-result/receipt.json) records the
[cold-start and relocation check](https://github.com/fly1d/repomin/actions/runs/40995100565):
an online first run with empty private Gradle/Maven/JVM homes reproduced the
target in 36.518 seconds, and a fresh offline copy did so in 7.400 seconds.
Gradle, its cache, the payload and the native report were then moved to another
root; the old root no longer existed. Two fresh recorded-command replays passed,
as did two negative controls: disabling warning failure and running `help` both
exited 0 without the target diagnostic. Timings are observations, not guarantees.

Those hosted checks used exact fingerprints. Git checkout or archive transport
can instead validate through the recorded content fingerprint; all 27 original
file bytes and modes were checked independently. JDK and ReproMin stayed outside
the relocated root. This establishes one maintained example on one Ubuntu
runner, not independent adoption, upstream endorsement, another reducer's
disadvantage, or portability across all machines.

[provenance.json](provenance.json) separates the original 1,733 → 27 deletion
history from the included 27 → 27 report, with the original report hash and
[reduction run](https://github.com/fly1d/repomin/actions/runs/40845091649).
The old report's obsolete absolute command and raw logs are not committed here.
For file-deletion revalidation, the source package named
`buildSrc/src/main/java/io/aeron/build` requires a `--keep` override; the native
report records all 27 files as protected, with content reducers disabled.

All payload files match original Git blobs from
[`aeron-io/aeron@1bae7dd`](https://github.com/aeron-io/aeron/tree/1bae7dd012eb98415cd86aa67d61b8fdaf339200).
The original Apache-2.0 [LICENSE](reference-result/payload/LICENSE) and file
headers are unchanged. The retained wrapper points to Gradle 9.8.1-all; the
checked runtime was 9.8.1-bin, not that wrapper distribution. Documentation and
receipt preparation used Codex assistance. Nothing posts feedback or contacts
the upstream project automatically.

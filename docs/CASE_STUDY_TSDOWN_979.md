# Public pilot: tsdown CSS module composition duplication

This case study records a maintainer-led public pilot completed on 2026-09-02
for [rolldown/tsdown#979](https://github.com/rolldown/tsdown/issues/979). The
goal was to test whether ReproMin could turn an existing public reproduction
into smaller, executable evidence that was useful to an upstream maintainer.
It was not a root-cause investigation or a performance benchmark.

## Why this issue qualified

- The upstream issue was open and explicitly requested a minimal repository.
- The original reproduction was public and MIT-licensed.
- The workflow needed no credentials, private service, or interactive UI.
- Its pnpm dependency graph could be pinned and reused offline during
  reduction.
- The generated CSS provided a stable, locally observable failure contract.

The prepared fixture used `tsdown` 0.22.14, `@tsdown/css` 0.22.14, Rolldown
1.2.6, and TypeScript 6.0.3. The reduction ran with ReproMin 0.1.0.dev9,
Node.js 26.5.0, pnpm 11.19.0, and the host backend on macOS.

## Oracle contract

The fixture defined a separate `reproduce.mjs` oracle that:

1. removed the previous `dist` directory;
2. ran the ordinary package build;
3. imported the generated JavaScript and required at least two usable CSS
   module exports;
4. counted the common `cursor: pointer` rule in the generated CSS; and
5. emitted `DUPLICATED_CSS_MODULE_COMPOSITION` with exit code 23 only when the
   count was at least two.

The oracle script, MIT license, package manifest, and lock file were protected
from whole-file removal with `--keep`. The manifest and source reducers were
disabled, and only `src/index.ts` was selected for text reduction. CSS files
were eligible for whole-file removal but not line reduction, so the public
result retained their original valid syntax.

An earlier symptom-only oracle checked generated CSS without loading the
generated JavaScript. It allowed a technically reproducing entry file with
unbound variables. Strengthening the oracle rejected that low-quality result.
This was the main product lesson: ReproMin preserves the configured contract,
so artifact usability must be part of that contract when it matters.

## Result

| Evidence | Prepared fixture | Reduced payload |
| --- | ---: | ---: |
| Files | 14 | 8 |
| Bytes | 32,788 | 31,342 |
| CSS module consumers | 4 | 2 |

The protected `pnpm-lock.yaml` accounts for 28,148 bytes, so file and consumer
counts describe this reduction more usefully than byte retention alone.

The configured file and text reducers reached a fixed point after 60 attempts,
with four accepted mutations and 14 cache uses. They removed:

- two unrelated configuration files;
- the generic README and `.gitignore` from the reduction payload;
- two CSS module export lines; and
- the corresponding two component CSS files.

The run did not exhaust its attempt or duration budget. `report validate`
confirmed the exact payload fingerprint, and `report replay` reproduced the
recorded match-and-exit-code oracle in 3 of 3 fresh copies.

## Current-release revalidation

On 2026-10-28, the published dev14 wheel completed a new, transparently prepared
version of this case. The original historical 14-file preparation directory
was not recovered. The [reproduction materials](../reproductions/tsdown-979/README.md)
include all 12 upstream files from commit
`094c1471678386c4851fae87922cd7b4d4fac461`, an explicit four-export entry-point
patch, and only the lockfile and strict oracle from public result commit
`69801202853e56c5e4e970145cbef86aab14007d`. No reduced source file was used to
seed the input. Matching historical counts do not prove snapshot identity.

| Measured host | Python | Node | pnpm | Maintainer runs | Fresh replays per run |
| --- | --- | --- | --- | ---: | ---: |
| macOS arm64 | 3.13.9 | 26.5.0 | 11.8.0 | 2 | 3/3 |
| macOS arm64 | 3.9.6 | 24.19.0 | 11.8.0 | 1 | 3/3 |

Each run reduced 14 files / 32,788 bytes to 8 files / 31,342 bytes, with
56 attempts, four accepted mutations, 14 cache uses and no exhausted budget.
Six fewer files means only 1,446 fewer bytes (4.41%); the lockfile dominates.
The newly measured pnpm is 11.8.0, as declared by the public fixture, rather
than the historical 11.19.0 recorded above.

Both negative controls built successfully and reached the intended checks:
removing the target declaration exited 0 with zero matches; exporting only one
CSS object exited 2 with the insufficient-exports diagnostic. Neither emitted
the target marker. The original prepared inputs remained byte-identical, and
each exported payload passed exact fingerprint validation before and after
controls. The [selected receipt](../reproductions/tsdown-979/reference-result/receipt.json)
and [reference payload](../reproductions/tsdown-979/reference-result/payload)
cover the Node 24 / Python 3.9 run. Full reports, raw logs, paths and runtimes
remain private.

These are tests on one maintainer-controlled host, with fresh repository copies
and task-local dependency stores. They do not establish independent users,
separate-machine or Linux portability, statistical certification, comparative
advantage or confirmed usefulness. The [five-field feedback form](../reproductions/tsdown-979/FEEDBACK.md)
separates replication from real-workflow value. The recipe is an opt-in online
setup and host execution example, not part of the network-free CI benchmark
suite.

## Public delivery

The validated result is available on the
[public reproduction branch](https://github.com/fly1d/tsdown-css-modules-bug-report/tree/codex/tsdown-979-minimal).
That branch restores a short README and `.gitignore` for user ergonomics, so it
contains two more files than the validated reduction payload. The evidence was
shared in the
[upstream issue comment](https://github.com/rolldown/tsdown/issues/979#issuecomment-5505213611).

Only scalar validation and replay results were published. The private report,
command logs, temporary paths, and local environment details outside the
declared tool versions were not uploaded. The delivery claims only that the
same output symptom survives in a smaller fixture; it does not assign a root
cause or claim upstream adoption.

## Reusable lessons

- Clean generated output before every oracle check so stale files cannot pass.
- Exercise the generated artifact, not only the symptom text, when usability
  matters to the recipient.
- Protect the oracle, license, and reproducibility inputs from whole-file
  removal, and disable content reducers that are outside the intended scope.
- Pin dependencies and warm an offline cache before a many-attempt pilot.
- Apply line reduction only where the oracle rejects malformed or unusable
  results.
- Restore concise human documentation after validating the exact payload, and
  state clearly when the public presentation differs from that payload.

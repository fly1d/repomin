# Good first issues

The following GitHub issues are intentionally scoped for a first contribution.
Comment on an issue before starting, keep the change inside its stated scope,
and follow [CONTRIBUTING.md](../CONTRIBUTING.md) for tests and documentation.

## Ready to claim

There is no unassigned implementation issue at the moment. Completed tasks are
removed from this section so contributors do not start work against a closed
issue. New scoped tasks are added after their user workflow and acceptance
criteria are ready.

## Recently completed

- [Add an end-to-end report replay benchmark](https://github.com/fly1d/repomin/issues/17)
  now ships as the `report-replay` offline check. Run
  `python3 benchmarks/run_offline.py --only report-replay` when adding or
  reviewing report workflows.

The [real CI failure pilot](https://github.com/fly1d/repomin/issues/11) is still
open for users who have a sanitized workflow to share. This is a feedback and
fixture-discovery contribution rather than a reserved code task. Check the
repository's [open issues](https://github.com/fly1d/repomin/issues) for newly
proposed work, or use the [issue template chooser](https://github.com/fly1d/repomin/issues/new/choose)
to suggest a focused contribution.

## Claim and submit

Use this short loop for a starter issue:

1. Check that the issue is still open and does not already have an assignee,
   then comment with the part you plan to change. Wait for the maintainer to
   confirm the scope before doing substantial work.
2. Keep the change on a focused branch and follow the issue's acceptance
   criteria. For documentation or fixture work, run the exact command shown in
   the issue and capture the result (including the expected payload and report
   validation when applicable).
3. Before opening a pull request, run
   `python3 scripts/check_contribution.py`; add `--with-benchmarks` for fixture
   changes. Open the PR against `main`, include `Closes #<issue-number>`, and
   paste the checks and observed output into the PR template.
4. Leave the issue linked until review and CI are complete. If the task is no
   longer available, choose another issue from the [open issue
   list](https://github.com/fly1d/repomin/issues) instead of starting parallel
   work.

## Proposing another starter task

A good starter issue should describe one user workflow, name the likely files,
define observable acceptance criteria, and avoid changing reduction semantics.
Suitable areas include documentation examples, completion ergonomics, strict
manifest extensions that reuse an existing parser, and deterministic benchmark
assertions. Open a feature request before implementing a new reducer or backend
whose trust boundary is not already documented.

The complete project direction remains in [ROADMAP.md](ROADMAP.md).

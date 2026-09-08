# Good first issues

The live GitHub issue list is the source of truth for available starter work:

- [Open `good first issue` tasks](https://github.com/fly1d/repomin/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22)
- [All open issues](https://github.com/fly1d/repomin/issues)
- [Suggest a focused change](https://github.com/fly1d/repomin/issues/new/choose)

Closed work stays in GitHub's issue history rather than being copied into this
document. That prevents contributors from claiming a task whose status changed
after the documentation was published.

## Claim and submit

1. Check that the issue is open and unassigned, then comment with the part you
   plan to change. Wait for the maintainer to confirm the scope before doing
   substantial work.
2. Follow the issue's acceptance criteria and keep the pull request focused.
   Fixture or documentation work should include the exact command run and its
   observed result.
3. Run `python3 scripts/check_contribution.py` before opening the pull request.
   Add `--with-benchmarks` when changing a fixture.
4. Include `Closes #<issue-number>` and the checks you ran in the pull request
   description.

Read [CONTRIBUTING.md](../CONTRIBUTING.md) for the complete development and
review contracts.

## Contribute workflow evidence

You do not need a publishable failure to help. A successful, inconclusive, or
blocked trial is useful when it records the workflow goal, ReproMin version,
runner, attempted command, and resulting value or friction.

- Use the [workflow feedback template](https://github.com/fly1d/repomin/issues/new?template=adoption_feedback.md)
  for a sanitized trial.
- Use the [real-failure pilot](https://github.com/fly1d/repomin/issues/11) when
  you can share a suitable Maven, Gradle, or Python reproduction.

## Propose another starter task

A good starter issue names one user workflow, the likely files, and observable
acceptance criteria. It should avoid changing reduction semantics. Suitable
areas include focused documentation, completion ergonomics, deterministic
benchmark assertions, and strict extensions that reuse an existing manifest
parser.

Open a feature request before implementing a reducer or backend whose trust
boundary is not already documented. Project priorities and explicit non-goals
are maintained in [ROADMAP.md](ROADMAP.md).

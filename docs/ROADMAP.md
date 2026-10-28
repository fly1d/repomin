# Roadmap

ReproMin is pre-alpha. This plan prioritizes user outcomes, not release dates
or a feature count. The [README](../README.md) describes the current scope;
[CHANGELOG.md](../CHANGELOG.md) records delivered changes.

## Current: one useful independent workflow

The immediate question is whether someone with an existing repeatable failure
can get a smaller reproduction that helps their bug report, debugging, or
regression test. A confirmed useful workflow can guide the next change; it does
not establish broad demand or product maturity.

1. Start with a [three-item request](https://github.com/fly1d/repomin/issues/11):
   repository and revision, failing command, and a short error excerpt. No
   installation or completed report is needed to ask for help.
2. First establish whether the failure reproduces, or identify the one missing
   detail needed to try it. Then define the strict failure contract and run a
   bounded reduction.
3. Ask what the result helped the participant do, or where it blocked them.
   Reuse the reviewed Markdown summary for technical fields. If there is no
   report, a short redacted stage/error is enough.
4. Fix a verified blocker before adding product breadth. Defer new reducers,
   adapters, exporters and integrations until a real workflow needs them.
5. Automate a repeated, useful workflow after the manual path is understood.
   The existing Doctor handoff, validation summary and CI already cover their
   established jobs; no new benchmark controller or reporting funnel is needed.

Maintainer runs, demos, CI and AI-agent reviews do not count as independent
usefulness. The original 2026-10-01 to 2026-10-29 validation window and its
5/3/2/1 targets remain recorded in the existing thread; they have not been
extended or converted into evidence of adoption.

Use the [tsdown](CASE_STUDY_TSDOWN_979.md) and
[Gradle](CASE_STUDY_GRADLE_38843.md) examples as technical references. Respect the
[pydoctor outreach postmortem](CASE_STUDY_PYDOCTOR_728.md): contact another
project only after its prior opt-in, a human policy check and disclosure of
material agent involvement. Stop if the recipient declines.

## Release readiness

Resolve observed user blockers before choosing another version or publication
channel. Use the existing [release checklist](RELEASING.md) when an actual
release is needed; another release is not a substitute for useful feedback.

## Explicit non-goals

- Replacing a build system, test runner, or dependency resolver.
- Claiming code correctness from a passing oracle or holdout result.
- Running untrusted commands as a security sandbox on the host backend.
- Adding network-dependent tests to the offline benchmark suite.

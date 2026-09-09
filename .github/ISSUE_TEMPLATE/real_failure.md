---
name: Real CI or dependency failure
about: Offer a public failure or reviewed run evidence for a bounded pilot
title: "[Pilot] "
labels: documentation,help wanted
assignees: ""
---

<!--
Read docs/REAL_FAILURE_PILOT.md first. Write N/A and why when a detail is not
available. Supply either (a) a public, licensed repository/fixture that a
maintainer can run, or (b) enough reviewed evidence from your own ReproMin run.
No installation is required for path (a).
-->

## Requested outcome

<!-- Ask for a fit check, a bounded first reduction, or review of existing evidence. -->

## Context and impact

- Failure's effect on the real workflow:
- Public repository/fixture, revision, and license; or `unavailable` and why:
- Language and build/test system:
- Runner OS, architecture, and relevant tool versions:

## Evidence and validation

<!-- The first three failure-contract fields are required for either evidence path. -->

- Sanitized command:
- Target exit code or failure-signature shape:
- Different failure that must be rejected:
- ReproMin version and install source (or `not run`):
- Backend, adapter/reducer, and relevant options (or `not run`):
- Baseline, reduction result, and approximate duration (or `not run`):
- Payload before/after and what was retained or removed (or `not run`):
- `report validate` result or reviewed artifact link (or `not run`):

## Done when

<!-- State the observable result that would make this pilot useful. -->

## Authorship and automation

- Who selected and prepared this workflow:
- Who ran ReproMin and reviewed the result:
- Material LLM or agent involvement (or `none`):
- Semantic reducer (`none` or provider/type, without credentials):

## Safety and redistribution

- [ ] I removed secrets, credentials, tokens, private URLs, production/customer
      data, proprietary source, and confidential logs.
- [ ] Public material has an open-source license, and the command needs no
      private service, special hardware, or credentials.
- [ ] The remaining description and artifacts may be shared for compatibility
      and regression analysis.
- [ ] I understand the host backend runs the command directly and is not a
      security sandbox.
- [ ] I will contact another project only after it opts into the pilot. I will
      check its contribution and AI-assistance policies and disclose material
      agent involvement in the first message.

## Other decision-relevant context (optional)

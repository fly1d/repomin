---
name: Real CI or dependency failure
about: Share a sanitized workflow that ReproMin could reduce
title: "[Pilot] "
labels: documentation,help wanted
assignees: ""
---

Before posting, read the [real failure pilot
guide](https://github.com/fly1d/repomin/blob/main/docs/REAL_FAILURE_PILOT.md).
Never include credentials, proprietary source, or confidential logs.
Fill in what you can and write `N/A` for details that are unavailable.

## Workflow

- Public repository or fixture (optional):
- Language and build/test system:
- Runner OS and architecture:
- Relevant tool versions:

## Failure contract

- Sanitized command:
- Exit code or failure signature shape:
- What should count as a different failure:

## ReproMin run

- ReproMin version and install source:
- Backend (`host` or `docker`):
- Adapter and source reducer:
- Relevant options (sampling, limits, or signature mode):
- Baseline and reduction result:
- Approximate duration:

## Artifact evidence

- Payload size before and after (files/bytes):
- What the minimized payload retained or removed:
- `report validate` result:
- Sanitized report or public artifact link (optional):

## Authorship and automation

- Who selected and prepared this workflow:
- Who ran ReproMin and reviewed the result:
- Material LLM or agent involvement (or `none`):
- Semantic reducer (`none` or provider/type, without credentials):

## Privacy and redistribution

- [ ] Secrets, credentials, tokens, and private URLs were removed.
- [ ] Proprietary source and confidential logs were removed.
- [ ] The remaining description and artifacts may be shared for compatibility
      and regression analysis.
- [ ] I understand that the host backend executes the supplied command directly
      and is not a security sandbox.
- [ ] If this artifact will be shared with another project, I checked its
      contribution and AI-assistance policies and will disclose material agent
      involvement in the first message.

## Additional context

# Recorded result

The eight-file payload and selected receipt come from the current handoff
runner's successful macOS arm64 / Python 3.9.6 / Node 24.19.0 run. It passed
3/3 fresh-copy replays and both strict negative controls.

Two earlier runs of the original kit used Python 3.13.9 / Node 26.5.0 on
the same host. These are maintainer tests, not independent users or machines.
The current runner also passes the local Node26 preflight.

Only selected scalar data, hashes, payload and the allowlisted summary are
exported. Raw run-record, full report, logs, caches and runtimes are private.
New runs generate their own report. This reference payload is never used as
the initial reduction input.

The receipt's runner hash identifies the locally verified handoff runner. This
checkout helper adds only an early, readable missing-wheel error. Its explicit
download instructions use the same published wheel and checksum; the reduction,
oracle and replay procedure are unchanged.

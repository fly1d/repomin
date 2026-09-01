# Report replay benchmark

This small, network-free fixture exercises the complete artifact workflow:

1. reduce the `required.txt` line set while preserving `ORIGINAL_FAILURE`;
2. validate the generated report against the exported payload;
3. replay the report in two fresh copies; and
4. run the same payload with `--different` to prove a changed failure is
   reported as a mismatch.

Run it from the repository root:

```sh
python3 benchmarks/run_offline.py --only report-replay
```

The expected reduced payload contains exactly:

```text
reproduce.py
required.txt
```

and `required.txt` contains only `REPLAY_NEEDLE`. The command writes a marker
file in its working copy. Replay succeeds only when every run starts from a
fresh copy; the marker never appears in the exported payload.

The deliberate `--different` command prints `DIFFERENT_FAILURE` and exits with
code 9. The benchmark expects replay to return exit code 1 with
`reproduced: false`, which distinguishes an oracle mismatch from an invalid
report or replay setup (exit code 2).

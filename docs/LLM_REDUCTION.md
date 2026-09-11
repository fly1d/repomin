# Semantic reduction

ReproMin includes an experimental, opt-in HTTP semantic reducer. It asks an
OpenAI-compatible chat-completions endpoint for one small repository edit, then
passes that edit through the same failure oracle as every deterministic
reducer. The endpoint never decides whether an edit is accepted.

The feature is disabled by default, adds no third-party runtime dependency, and
is not required for ordinary repository reduction.

## Use an endpoint

Set an endpoint and model explicitly:

```sh
export REPOMIN_SEMANTIC_TOKEN=optional-bearer-token

repomin reduce . \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --semantic-reducer http \
  --semantic-endpoint http://localhost:8000/v1/chat/completions \
  --semantic-model local-model \
  --semantic-timeout 60
```

`REPOMIN_SEMANTIC_REDUCER`, `REPOMIN_SEMANTIC_ENDPOINT`,
`REPOMIN_SEMANTIC_MODEL`, and `REPOMIN_SEMANTIC_TIMEOUT` provide equivalent
defaults. The bearer token is accepted only through
`REPOMIN_SEMANTIC_TOKEN`, so it does not appear in command-line arguments or
reports.

The endpoint must implement the common `/v1/chat/completions` request and
response shape. ReproMin asks for JSON containing one or more file replacements
or deletions:

```json
{
  "edits": [
    {"path": "relative/path.py", "replace": "new file content\n"},
    {"path": "unused.txt", "delete": true}
  ]
}
```

Paths must be repository-relative. Replacements and deletions apply only to
existing regular files, protected paths cannot be deleted, and every resulting
candidate is validated in a fresh copy. Invalid responses and rejected edits
do not enter the accepted repository state.

Run the deterministic local-stub benchmark when changing this integration:

```sh
python3 benchmarks/run_offline.py --only semantic-stub
```

## Privacy and trust boundary

The semantic endpoint receives the configured command, failure signal, and a
bounded selection of readable UTF-8 files from the current reduced tree. Do not
enable it for credentials, proprietary code, private URLs, customer data, or
repositories whose contents the selected provider is not authorized to
receive.

The host execution backend remains unsandboxed. Oracle validation proves only
that the configured failure still matches in the sampled runs; it does not
prove correctness, explain the root cause, or make a model-proposed edit safe.

Reports record the semantic backend, endpoint, model, request timeout, call
count, and accepted-candidate count; the request timeout uses
`execution.semantic_timeout`. They omit the bearer token. Treat the full report
as sensitive because endpoint URLs and the reproduction command can still
contain private information.

## Design background

This boundary was informed by agent harnesses and program-reduction research,
but ReproMin does not depend on or embed those systems.

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) treats
  models, tools, storage, and execution as replaceable capabilities. ReproMin
  borrows the narrow capability-boundary idea rather than its runtime.
- [LPR: LLM-Aided Program Reduction](https://arxiv.org/abs/2312.13064)
  alternates syntax-guided and semantic reduction. ReproMin's fixed-point
  scheduler similarly re-runs deterministic reducers after an accepted
  semantic edit.
- Perses, Vulcan, and hierarchical delta debugging motivate parser-backed and
  hierarchical candidate generation. They do not replace ReproMin's ordinary
  failure-oracle acceptance contract.

The current HTTP adapter is intentionally small. New providers, agent loops, or
model-specific behavior should remain outside the core until independent user
workflows demonstrate that a shared integration contract is needed.

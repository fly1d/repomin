# FastAPI dependency regression fixture

This fixture models a small FastAPI route regression whose failure only keeps
occurring when the same dependency is declared in the project metadata and in
the nested runtime requirements file. The root requirements file also contains
an include, a constraint, an extra package index, and unrelated dependencies.
The test suite contains one unrelated failing test so the reducer must retain
the regression's failure identity rather than merely any non-zero exit.

The fixture is Docker-only because its oracle needs pinned FastAPI and pytest
versions. Build the local image from this directory; ReproMin never pulls an
image or runs a package resolver for you:

```sh
docker build -t repomin-fastapi-fixture benchmarks/python-fastapi

PYTHONPATH=src python3 -m repomin benchmarks/python-fastapi \
  --command 'python -m pytest -q' \
  --match 'FastAPI route regression: dependency override leaked' \
  --backend docker \
  --docker-image repomin-fastapi-fixture \
  --adapter python \
  --source-reducer none \
  --output /tmp/repomin-fastapi-result
```

The output directory must be outside this source fixture. The exported payload
should contain exactly:

```text
app/main.py
pyproject.toml
requirements.txt
requirements/runtime.txt
tests/test_regression.py
```

Validate the report and payload fingerprint without executing the recorded
command again:

```sh
PYTHONPATH=src python3 -m repomin report validate \
  /tmp/repomin-fastapi-result.repomin/report.json \
  --payload /tmp/repomin-fastapi-result --json
```

The image must be built locally first; ReproMin never pulls it. The workflow is
network-free after the image build, requires a local Docker daemon, and uses
Docker networking `none` by default. Docker is defense in depth rather than a
complete security boundary. The report is evidence for the configured failure
oracle, not a correctness or production-reliability guarantee. Review
[SECURITY.md](../../SECURITY.md) before using the host backend or an image built
from untrusted input.

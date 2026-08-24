# Directory.Build.props manifest benchmark

This fixture exercises the MSBuild XML adapter for shared `Directory.Build.props`
files. The offline Python oracle keeps one shared package reference, one shared
project reference, and the target framework while allowing unrelated shared item
entries to disappear. It does not require the .NET SDK; a real `dotnet build`
command can be substituted when the SDK and project dependencies are available.

Run from the repository root:

```sh
out_parent="$(mktemp -d /tmp/repomin-dotnet-dbp.XXXXXX)"
PYTHONPATH=src python3 -m repomin benchmarks/dotnet-directory-build-props \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --adapter dotnet \
  --source-reducer none \
  --output "$out_parent/result"
```

The independent oracle must still exit `1` with `ORIGINAL_FAILURE`, while the
exported directory retains `Directory.Build.props` with the required shared
package/project references and framework.

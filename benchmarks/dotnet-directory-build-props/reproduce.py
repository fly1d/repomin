from pathlib import Path

text = Path("Directory.Build.props").read_text(encoding="utf-8")
required = (
    'Include="Repomin.Shared.Required"',
    'Include="../required/Required.csproj"',
    "<TargetFramework>net8.0</TargetFramework>",
)
if not all(value in text for value in required):
    print("DIFFERENT_FAILURE")
    raise SystemExit(2)
print("ORIGINAL_FAILURE")
raise SystemExit(1)

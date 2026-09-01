from pathlib import Path
import sys


marker = Path(".replay-marker")
if marker.exists():
    print("STALE_COPY", file=sys.stderr)
    raise SystemExit(3)
marker.write_text("created by the replay command\n", encoding="utf-8")

if "--different" in sys.argv[1:]:
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(9)

if "REPLAY_NEEDLE" not in Path("required.txt").read_text(encoding="utf-8"):
    print("DIFFERENT_FAILURE", file=sys.stderr)
    raise SystemExit(8)

print("ORIGINAL_FAILURE", file=sys.stderr)
raise SystemExit(7)

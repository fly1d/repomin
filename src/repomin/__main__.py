import sys
from pathlib import Path

if not __package__:
    # Direct isolated execution must use this source checkout or installation.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repomin.cli import main


if __name__ == "__main__":
    raise SystemExit(main())


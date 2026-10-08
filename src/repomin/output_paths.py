"""Read-only checks for the directory used by atomic payload export."""

from pathlib import Path


def validate_output_parent(output: Path) -> None:
    """Reject an unavailable export parent before any oracle command runs."""
    parent = output.parent
    if not parent.is_dir():
        raise ValueError(
            "output parent must be an existing directory: %s "
            "(create it with mkdir before running Doctor or reduction)" % parent
        )

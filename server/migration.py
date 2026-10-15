# server/migration.py
"""Move a legacy shared storage root into a single account's directory.

Kept separate from the CLI so the destructive part can be unit tested without a
database.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MigrationPlan", "apply_migration", "plan_migration"]


@dataclass
class MigrationPlan:
    """What :func:`apply_migration` is going to do."""

    source: Path
    destination: Path
    entries: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    copy: bool = False

    @property
    def action(self) -> str:
        return "copy" if self.copy else "move"

    def describe(self):
        """Yield one human readable line per entry."""
        for entry in self.entries:
            marker = " (overwrite)" if entry.name in self.conflicts else ""
            yield f"{self.action} {entry.name}{marker}"


def plan_migration(source, destination, *, copy: bool = False) -> MigrationPlan:
    """Inspect a migration without changing anything.

    The destination is created if necessary, so an empty plan can be reported
    even for a fresh account.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()

    if not source.is_dir():
        raise ValueError(f"{source} is not a directory")
    if source == destination:
        raise ValueError("source and destination are the same directory")

    destination.mkdir(parents=True, exist_ok=True)
    # The destination normally lives inside the source, so it must not be moved
    # into itself.
    entries = [
        entry for entry in sorted(source.iterdir()) if entry.resolve() != destination
    ]
    conflicts = [
        entry.name for entry in entries if (destination / entry.name).exists()
    ]
    return MigrationPlan(source, destination, entries, conflicts, copy)


def apply_migration(plan: MigrationPlan) -> None:
    """Execute a plan produced by :func:`plan_migration`."""
    for entry in plan.entries:
        target = plan.destination / entry.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if plan.copy:
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
        else:
            shutil.move(str(entry), str(target))

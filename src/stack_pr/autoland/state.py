"""Checkpoint persistence (for ``--resume``) and the per-branch run lock."""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from stack_pr import git
from stack_pr.autoland.model import (
    LandingContext,
    _deserialize_entry,
    _deserialize_step,
    _serialize_step,
)
from stack_pr.autoland.options import AutolandOptions

# ---------------------------------------------------------------------------
# State persistence (checkpoint / resume)
# ---------------------------------------------------------------------------

STATE_VERSION = 1


@dataclass
class AutolandCheckpointer:
    """Persists and restores landing state for crash-safe ``--resume``."""

    path: Path
    branch: str
    base: str
    # The plan file the run was started from, so `--replan` can re-read it.
    plan_file: Path | None = None

    @staticmethod
    def default_path(branch: str) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", branch)
        return Path.home() / ".stack-pr" / "autoland" / f"{slug}.json"

    def save(self, ctx: LandingContext) -> None:
        """Atomically write a checkpoint of *ctx* to the state file."""
        data = {
            "version": STATE_VERSION,
            "branch": self.branch,
            "base": self.base,
            "plan_file": str(self.plan_file) if self.plan_file else None,
            "current_step": ctx.current_step,
            "last_landed_sha": ctx.last_landed_sha,
            # Kept so `autoland --status` can say why a stopped run stopped.
            "abort_reason": ctx.abort_reason,
            "stack": [asdict(e) for e in ctx.stack],
            "plan": [_serialize_step(s) for s in ctx.plan],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.rename(self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> tuple[AutolandCheckpointer, LandingContext]:
        """Load a checkpoint; returns the checkpointer and restored context."""
        data = json.loads(path.read_text())
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"Unsupported state file version: {data.get('version')}")
        ctx = LandingContext(
            stack=[_deserialize_entry(e) for e in data["stack"]],
            plan=[_deserialize_step(s) for s in data["plan"]],
            current_step=data.get("current_step", 0),
            aborted=bool(data.get("abort_reason")),
            abort_reason=data.get("abort_reason", ""),
            last_landed_sha=data.get("last_landed_sha", ""),
        )
        plan_file = data.get("plan_file")
        return cls(
            path=path,
            branch=data["branch"],
            base=data["base"],
            plan_file=Path(plan_file) if plan_file else None,
        ), ctx


@dataclass
class AutolandLock:
    """Advisory filesystem lock preventing concurrent autolands on a branch.

    The lock is an ``flock`` held for the lifetime of the process, so the OS
    releases it automatically on exit — including crashes. A failed or
    interrupted run therefore frees the lock (so it can later be resumed) while
    its state file persists. The lock *file* is only a handle: a leftover file
    from a crashed run does not block a future run, because acquisition depends
    on the flock, not on the file's existence.
    """

    path: Path
    _fd: int | None = field(default=None, repr=False)

    @staticmethod
    def for_state(state_path: Path) -> AutolandLock:
        """Return the lock sitting next to *state_path* (``<name>.lock``)."""
        return AutolandLock(state_path.with_name(state_path.name + ".lock"))

    def acquire(self) -> bool:
        """Try to take the lock; return False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            # The previous holder unlinks the file as it releases, so the flock
            # may have landed on a file that no longer has this name — a lock
            # nobody else can see. Only a lock on the file at the path counts.
            if self._names_locked_file(fd):
                break
            os.close(fd)
        self._fd = fd
        # Record the holder so `autoland --status` can name the running process.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return True

    def _names_locked_file(self, fd: int) -> bool:
        try:
            at_path, locked = self.path.stat(), os.fstat(fd)
        except FileNotFoundError:
            return False
        return (at_path.st_dev, at_path.st_ino) == (locked.st_dev, locked.st_ino)

    def is_held(self) -> bool:
        """Whether some process holds the lock right now.

        Probes without creating or removing the lock file, so it is safe to call
        while a run is live. The probe holds a shared lock for an instant; a run
        starting in exactly that instant would see the branch as busy.
        """
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            # Closing the descriptor also drops the probe's own lock.
            os.close(fd)
        return False

    def holder_pid(self) -> int | None:
        """The PID the holder recorded, or None if unknown."""
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        """Release the lock and remove the lock file (no-op if not held)."""
        if self._fd is None:
            return
        # Unlink while still holding the flock: a waiter that takes it next
        # then finds the name gone or reused, and retries (see acquire).
        # Unlocking first would let it lock a file about to lose its name.
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self._fd)  # closing the descriptor releases the flock
            self._fd = None


def _state_path(opts: AutolandOptions) -> Path:
    """The checkpoint an existing run for *opts* would have written."""
    if opts.state_file:
        return opts.state_file
    return AutolandCheckpointer.default_path(
        opts.branch or git.get_current_branch_name()
    )

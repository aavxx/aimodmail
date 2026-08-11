"""Refuse to start when another copy of this bot is already running.

Two bot processes against one token both receive every gateway event, so every
DM gets answered twice and every ticket is opened twice. Nothing in Discord's
API prevents it and nothing in the logs of either process looks wrong, which is
what makes it hard to notice: the only symptom is users seeing doubles.

The lock is an `flock` on a file rather than a PID file. A PID file has to be
cleaned up by the process that wrote it, so a bot that was killed, OOMed or
lost with its container leaves a stale lock behind and the next start refuses
for no reason. An `flock` is released by the kernel when the holding process
ends, however it ends, so there is no stale state to reason about and no
cleanup path that can be missed.

The PID is still written into the file, but only so the refusal can name the
process that is in the way — never as the thing being locked on.
"""

import atexit
import hashlib
import os
import tempfile
import typing
from pathlib import Path

from core.models import getLogger

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

logger = getLogger(__name__)

# Set to skip the check. For deployments that genuinely run more than one
# process against one token, which Modmail does not do, but which is the
# user's call rather than something to make impossible.
OVERRIDE_ENV = "MODMAIL_ALLOW_MULTIPLE"

# Overrides where the lock lives, for read-only or unusual temp setups.
LOCK_PATH_ENV = "MODMAIL_LOCK_FILE"

# Held for the lifetime of the process. Module level because closing the file
# releases the lock, and a local would be garbage collected the moment
# acquire() returned.
_handle: typing.Optional[typing.IO] = None


class AlreadyRunning(RuntimeError):
    """Another instance holds the lock."""

    def __init__(self, path: Path, pid: typing.Optional[str]):
        self.path = path
        self.pid = pid
        super().__init__(f"another bot process is already running (pid {pid or 'unknown'}, lock {path})")


def default_lock_path() -> Path:
    """A lock file specific to this checkout.

    Keyed on the installation directory, not a fixed name, so two different
    bots deployed on one host do not lock each other out — the thing worth
    preventing is two processes of *this* bot, not two bots.
    """
    override = (os.getenv(LOCK_PATH_ENV) or "").strip()
    if override:
        return Path(override)

    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"modmail-{digest}.lock"


def _read_holder(path: Path) -> typing.Optional[str]:
    """The PID recorded in the lock file, for the error message only.

    flock is advisory, so reading a locked file is fine.
    """
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def acquire(path: typing.Optional[Path] = None) -> Path:
    """Take the run lock. Raises AlreadyRunning if another process holds it.

    Returns the lock path on success, including when the check was skipped, so
    callers can log where it lives.
    """
    path = path or default_lock_path()

    if os.getenv(OVERRIDE_ENV):
        logger.warning(
            "%s is set, so the duplicate-process check is disabled. Two processes on one "
            "token will answer every message twice.",
            OVERRIDE_ENV,
        )
        return path

    if fcntl is None:
        # Windows. Rather than a second implementation on the platform this is
        # least likely to be deployed on, say plainly that the guard is not
        # active instead of implying a protection that is not there.
        logger.warning("No fcntl on this platform, so the duplicate-process check is not active.")
        return path

    global _handle

    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        logger.warning("Could not open the lock file %s, so the duplicate-process check is not active.", path)
        return path

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = _read_holder(path)
        handle.close()
        raise AlreadyRunning(path, holder) from None

    # Only now that the lock is ours: the PID in the file must describe the
    # process that actually holds it.
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        # The lock is what matters; the PID is a convenience for the next
        # process's error message.
        logger.debug("Could not record the pid in %s.", path, exc_info=True)

    _handle = handle
    atexit.register(release)
    logger.debug("Holding the single-instance lock at %s.", path)
    return path


def release() -> None:
    """Drop the lock. The kernel does this anyway; this is for tests."""
    global _handle
    if _handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(_handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            _handle.close()
        finally:
            _handle = None

"""Process-level safety for `radar run`: one bot per data directory, and a heartbeat for health checks."""
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def lock_path() -> Path:
    from radar import config
    return config.DB_PATH.with_suffix(".lock")


def heartbeat_path() -> Path:
    from radar import config
    return config.DB_PATH.with_suffix(".heartbeat")


def healthy_within(poll_minutes: int) -> float:
    """Max heartbeat age for a healthy bot: two missed checks plus slack for a slow sweep."""
    return poll_minutes * 60 * 2 + 120


class AlreadyRunning(RuntimeError):
    pass


def _try_lock(f) -> bool:
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


@contextmanager
def single_instance(lock_path: Path):
    """Held for the bot's lifetime. A second `radar run` on the same data directory fails fast instead of
    splitting Slack clicks between two bots. The OS releases the lock if the process dies, so it never goes stale."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    if not _try_lock(f):
        f.seek(0)
        owner = f.read().strip() or "unknown"
        f.close()
        raise AlreadyRunning(f"Another Radar bot is already running (pid {owner}, lock {lock_path}).")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    try:
        yield
    finally:
        f.close()


def beat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())))


def heartbeat_age(path: Path) -> float | None:
    """Seconds since the last beat, or None if the bot never wrote one."""
    try:
        return time.time() - int(path.read_text().strip())
    except (OSError, ValueError):
        return None

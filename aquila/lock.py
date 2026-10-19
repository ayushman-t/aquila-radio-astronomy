# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""One observer per data folder. An OS file lock held open for the life of the
process: the kernel drops it on any exit or reboot, so a stale lock cannot
happen and pid reuse does not matter. The pid inside is only for the message."""
import contextlib
import os
import sys
import time

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


LOCK_BYTE = 64          # lock a byte past the pid so other handles can still read the pid


def _try_lock(fd):
    try:
        if sys.platform == "win32":
            os.lseek(fd, LOCK_BYTE, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            os.lseek(fd, LOCK_BYTE, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


class Lock:
    def __init__(self, path, fd):
        self.path = path
        self.fd = fd

    def release(self):
        if self.fd is None:
            return
        _unlock(self.fd)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = None


def holder(state_dir):
    """pid of a live process holding this folder, else 0."""
    p = os.path.join(state_dir, "lock")
    if not os.path.exists(p):
        return 0
    try:
        fd = os.open(p, os.O_RDWR)
    except OSError:
        return 0
    try:
        if _try_lock(fd):
            _unlock(fd)
            return 0
        with contextlib.suppress(OSError, ValueError):
            os.lseek(fd, 0, 0)
            return int(os.read(fd, 32).decode().strip() or -1)
        return -1
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def acquire_lock(state_dir, log=print):
    p = os.path.join(state_dir, "lock")
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    got = False
    for _ in range(10):                  # a GUI polling holder() holds it for microseconds
        if _try_lock(fd):
            got = True
            break
        time.sleep(0.05)
    if not got:
        other = holder(state_dir)
        os.close(fd)
        log(f"another observer (pid {other}) is already using {state_dir}; not starting")
        raise SystemExit(3)
    with contextlib.suppress(OSError):
        os.lseek(fd, 0, 0)
        os.write(fd, str(os.getpid()).encode().ljust(32))
    return Lock(p, fd)


def release_lock(lk):
    if lk is not None:
        lk.release()


def watch_stop(state_dir, stop, every_s=1.0):
    """Call stop() as soon as a 'stop' file appears in the data folder, mid-block too.
    The app asks for a stop this way on every OS (Windows has no SIGTERM to send).
    The observer's own loop removes the file when it sees it."""
    import threading
    path = os.path.join(state_dir, "stop")

    def loop():
        while True:
            time.sleep(every_s)
            if os.path.exists(path):
                stop()
                return
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

"""A windowed build has no stdout or stderr; closing the app must not trip over that."""
import io
import sys

import pytest

pytest.importorskip("PySide6")
from aquila import gui  # noqa: E402


def test_flush_with_no_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    gui.flush_std()


def test_flush_with_closed_stream(monkeypatch):
    s = io.StringIO()
    s.close()
    monkeypatch.setattr(sys, "stdout", s)
    gui.flush_std()


@pytest.mark.parametrize("rc", [0, 3])
def test_hard_exit_ends_the_process_with_its_code(rc):
    """Run in a child: the exit code arrives, and no clean-up code gets to run."""
    import subprocess
    code = ("import atexit, sys\n"
            "from aquila import gui\n"
            "atexit.register(lambda: print('cleanup ran'))\n"
            "print('before'); sys.stdout.flush()\n"
            f"gui.hard_exit({rc})\n"
            "print('after')\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == rc, r.stderr
    assert r.stdout.split() == ["before"]

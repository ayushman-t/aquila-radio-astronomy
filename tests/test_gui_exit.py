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

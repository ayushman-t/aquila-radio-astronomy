"""The observer's log must survive a dead stdout: when the desktop app that
started it goes away, Windows raises EINVAL on the next print."""
import builtins

from aquila import store


def test_log_keeps_going_when_stdout_is_gone(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path))
    calls = []

    def broken_print(*a, **k):
        calls.append(a)
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(builtins, "print", broken_print)
    st.log("first")
    st.log("second")
    assert len(calls) == 1                      # stops trying after the first failure
    text = (tmp_path / "survey.log").read_text()
    assert "first" in text and "second" in text

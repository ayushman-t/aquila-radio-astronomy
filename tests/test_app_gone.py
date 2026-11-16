"""An observer started by the desktop app must not outlive it: with the app killed there
is no window left to stop it from, and it keeps the receiver and the data folder."""
import os
import subprocess
import sys
import time

from aquila import lock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_process_alive():
    assert lock.process_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not lock.process_alive(p.pid)


def test_no_app_no_watch(monkeypatch):
    monkeypatch.delenv(lock.APP_PID_ENV, raising=False)
    assert lock.watch_app(lambda: None) is None
    monkeypatch.setenv(lock.APP_PID_ENV, "not a number")
    assert lock.watch_app(lambda: None) is None


def test_watch_calls_stop_when_the_app_is_gone(monkeypatch):
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setenv(lock.APP_PID_ENV, str(app.pid))
    stopped = []
    lock.watch_app(lambda: stopped.append(1), every_s=0.1)
    time.sleep(0.5)
    assert not stopped                      # the app is there: observing goes on
    app.kill()
    app.wait()
    end = time.time() + 5
    while not stopped and time.time() < end:
        time.sleep(0.05)
    assert stopped


def test_observer_ends_after_its_app_is_killed(tmp_path):
    """The real thing: a hydrogen line observer on the test source, its app killed."""
    cfg = tmp_path / "c.json"
    import json
    cfg.write_text(json.dumps({"sdr": "synthetic", "state_dir": str(tmp_path / "s"),
                               "advanced": {"api_port": 18471},
                               "save": {"plots": False}}))
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    env = dict(os.environ, **{lock.APP_PID_ENV: str(app.pid)})
    # this source tree, also for a Python that does not read PYTHONPATH
    boot = (f"import sys; sys.path.insert(0, {ROOT!r}); from aquila import cli, lock; "
            "assert hasattr(lock, 'watch_app'); sys.exit(cli.main())")
    obs = subprocess.Popen([sys.executable, "-c", boot, "-c", str(cfg), "hline"],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.time() + 30
        while not (tmp_path / "s" / "lock").exists() and time.time() < end:
            time.sleep(0.2)
        assert obs.poll() is None
        time.sleep(3)
        assert obs.poll() is None           # still observing while the app lives
        app.kill()
        app.wait()
        assert obs.wait(timeout=30) == 0
    finally:
        for p in (app, obs):
            if p.poll() is None:
                p.kill()

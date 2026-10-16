# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Open the app when the user logs in. One entry per user, nothing system-wide:
an XDG autostart file on Linux, a Run key on Windows, a LaunchAgent on macOS.
With "observe when the app opens" also set, a node comes back by itself after
a power cut, as long as the computer logs in on its own."""
import contextlib
import os
import plistlib
import shlex
import sys

NAME = "aquila-radio-astronomy"
LABEL = "in.radioastronomy.aquila"
# entries written by versions that were still called "Aquila SETI": removed whenever
# the setting is changed, so an upgraded install never starts twice at login
OLD_NAME, OLD_LABEL, OLD_RUN = "aquila-seti", "in.radioastronomy.aquila-seti", "Aquila SETI"


def _command(cfg_path):
    """The interpreter running now, so the entry starts this same install."""
    exe = sys.executable
    if sys.platform == "win32":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe       # no console window at login
    cmd = [exe, "-m", "aquila.cli"]
    if cfg_path:
        cmd += ["-c", os.path.abspath(cfg_path)]
    return cmd + ["gui"]


def _linux_path(name=NAME):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", name + ".desktop")


def _mac_path(label=LABEL):
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")


def _drop_old():
    """Remove a login entry left by the old app name."""
    if sys.platform == "win32":
        import winreg
        with contextlib.suppress(OSError), winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, OLD_RUN)
        return
    old = _mac_path(OLD_LABEL) if sys.platform == "darwin" else _linux_path(OLD_NAME)
    with contextlib.suppress(OSError):
        os.remove(old)


def _desktop_quote(arg):
    """Exec= quoting from the desktop entry spec: double quotes, \\ " ` $ escaped."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg.replace("%", "%%")
    for c in ('\\', '"', '`', '$'):
        arg = arg.replace(c, '\\' + c)
    return '"' + arg.replace("%", "%%") + '"'


def enabled():
    """True when an entry for this app is in place."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, "Aquila Radio Astronomy")
            return True
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, OLD_RUN)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return os.path.exists(_mac_path()) or os.path.exists(_mac_path(OLD_LABEL))
    return os.path.exists(_linux_path()) or os.path.exists(_linux_path(OLD_NAME))


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_enabled(on, cfg_path=None):
    """Add or remove the login entry. Raises OSError when it cannot be written."""
    cmd = _command(cfg_path)
    _drop_old()
    if sys.platform == "win32":
        import subprocess
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, "Aquila Radio Astronomy", 0, winreg.REG_SZ,
                                  subprocess.list2cmdline(cmd))
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(k, "Aquila Radio Astronomy")
        return
    if sys.platform == "darwin":
        path = _mac_path()
        if not on:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                           "ProcessType": "Interactive"}, f)
        return
    path = _linux_path()
    if not on:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon-256.png")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Aquila Radio Astronomy\n"
                "Comment=SETI and hydrogen line observer\n"
                f"Exec={' '.join(_desktop_quote(a) for a in cmd)}\n"
                f"Icon={icon}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                # let the network and USB settle before the observer opens the SDR
                "X-GNOME-Autostart-Delay=15\n")
    os.replace(tmp, path)


def describe(cfg_path=None):
    """The command an entry runs, for display."""
    return shlex.join(_command(cfg_path))

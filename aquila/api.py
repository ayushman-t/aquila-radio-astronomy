# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""HTTP view of a node's state directory. Standard library only, so a Pi or a
phone needs nothing extra. The desktop app talks to this, locally or over the
network. GET serves small files and never changes anything. The one write is
POST /cal, which asks the observer for a receiver calibration; it needs the
node's control token (advanced.api_token) and is off when none is set."""
import json
import mimetypes
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

MAX_BYTES = 20_000_000
# never served, wherever the data folder points: the token, the spend, half-written files
PRIVATE = ("config.json", "budget.json")


class Budget:
    """Bytes served per UTC day. Past the cap only status.json and index.json are served,
    and those only up to twice the cap, so no client can run a metered link dry. The
    spend is kept in budget.json so a service restart does not reset it."""

    def __init__(self, daily_mb, path=None):
        self.cap = float(daily_mb or 0) * 1e6
        self.path = path
        self.lock = threading.Lock()
        self.day, self.used = "", 0
        if path and os.path.exists(path):
            try:
                d = json.load(open(path))
                self.day, self.used = d.get("day", ""), int(d.get("used", 0))
            except Exception:
                pass

    def _roll(self):
        import time
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.used = day, 0

    def charge(self, n):
        with self.lock:
            self._roll()
            self.used += n
            if self.path and self.cap:
                try:
                    tmp = self.path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump({"day": self.day, "used": self.used}, f)
                    os.replace(tmp, self.path)
                except OSError:
                    pass

    def allows(self, n, essential=False):
        """May n more bytes go out? Essential files get a second helping, nothing more."""
        if not self.cap:
            return True
        with self.lock:
            self._roll()
            limit = self.cap * (2 if essential else 1)
            return self.used + n <= limit


CAL_REQUEST = "cal_request"


def _handler(state_dir, budget, token=""):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=state_dir, **k)

        timeout = 20                   # an idle connection must not hold a thread forever

        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_error(405)

        def _private(self, full):
            """Outside the data folder (symlinks resolved), or a file no client needs."""
            root = os.path.realpath(state_dir)
            real = os.path.realpath(full)
            name = os.path.basename(real)
            return (not real.startswith(root + os.sep) or name in PRIVATE
                    or name.startswith(".") or name.endswith((".tmp", ".lock")))

        def do_POST(self):
            """POST /cal with header X-Aquila-Token: writes the request file the observer
            picks up between blocks. Constant-time token compare; no body is read."""
            import hmac
            path = self.path.partition("?")[0]
            if path != "/cal":
                return self.send_error(404)
            if not token:
                return self.send_error(403, "this node has no control token set")
            sent = self.headers.get("X-Aquila-Token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return self.send_error(403, "wrong control token")
            # only an observer that is running right now may be asked: a request left
            # for a stopped node would fire whenever it next starts, dish wherever it is
            st = index(state_dir).get("status") or {}
            try:
                fresh = (time.time() - float(st.get("ts") or 0)
                         < 3 * float(st.get("dwell_s") or 150) + 60)
            except (TypeError, ValueError):
                fresh = False
            if st.get("state") in ("stopped", "", None) or not fresh:
                return self.send_error(409, "node is not observing; nothing to calibrate")
            try:
                p = os.path.join(state_dir, CAL_REQUEST)
                with open(p + ".tmp", "w") as f:
                    f.write(str(int(time.time())))
                os.replace(p + ".tmp", p)
            except OSError as e:
                return self.send_error(500, str(e))
            return self._json_body(json.dumps({"ok": True, "queued": True}).encode())

        def do_GET(self):
            path, _, query = self.path.partition("?")
            essential = path in ("/", "/index.json", "/status.json")
            if path == "/survey.log" and query.startswith("tail="):
                return self._tail(query[5:])
            if path in ("/hits.csv", "/rfi.csv") and query.startswith("utc="):
                return self._rows(path[1:], query[4:])
            if path in ("/", "/index.json"):
                body = json.dumps(index(state_dir, control=bool(token))).encode()
                if not budget.allows(len(body), essential=True):
                    return self.send_error(429, "daily data budget spent")
                return self._json_body(body)
            if path.endswith("/") and path != "/":
                d = os.path.normpath(os.path.join(state_dir, path.strip("/")))
                inside = d == state_dir or d.startswith(state_dir + os.sep)
                if not inside or not os.path.isdir(d):
                    return self.send_error(404)
                return self._json(sorted(os.listdir(d)))
            full = self.translate_path(path)
            if (self._private(full) or not os.path.isfile(full)
                    or os.path.getsize(full) > MAX_BYTES):
                return self.send_error(404)
            if not budget.allows(os.path.getsize(full), essential=essential):
                return self.send_error(429, "daily data budget spent")
            ims = self.headers.get("If-Modified-Since")
            if ims:
                import email.utils
                try:
                    t = email.utils.parsedate_to_datetime(ims).timestamp()
                    if int(os.path.getmtime(full)) <= int(t):
                        self.send_response(304)
                        self.end_headers()
                        return None
                except (TypeError, ValueError):
                    pass
            budget.charge(os.path.getsize(full))
            return super().do_GET()

        def _tail(self, n):
            """Last n bytes of the log (n capped at 64 KB), for the app's Log tab."""
            try:
                n = max(1, min(int(n), 65536))
            except ValueError:
                return self.send_error(400)
            full = os.path.join(state_dir, "survey.log")
            if not os.path.isfile(full):
                return self.send_error(404)
            ims = self.headers.get("If-Modified-Since")
            if ims:
                import email.utils
                try:
                    t = email.utils.parsedate_to_datetime(ims).timestamp()
                    if int(os.path.getmtime(full)) <= int(t):
                        self.send_response(304)
                        self.end_headers()
                        return None
                except (TypeError, ValueError):
                    pass
            if not budget.allows(n):
                return self.send_error(429, "daily data budget spent")
            with open(full, "rb") as f:
                f.seek(max(0, os.path.getsize(full) - n))
                body = f.read()
            budget.charge(len(body))
            self._text(body, full)

        def _rows(self, name, utc):
            """Header plus the rows of one block (its UTC stamp), so the app can save a
            block's hits without pulling the whole table over a metered link."""
            import re
            if not re.fullmatch(r"\d{8}T\d{6}Z", utc):
                return self.send_error(400)
            full = os.path.join(state_dir, name)
            if not os.path.isfile(full):
                return self.send_error(404)
            out = []
            with open(full, "rb") as f:
                out.append(f.readline())
                pre = utc.encode() + b","
                for line in f:
                    if line.startswith(pre):
                        out.append(line)
            body = b"".join(out)
            if not budget.allows(len(body)):
                return self.send_error(429, "daily data budget spent")
            budget.charge(len(body))
            self._text(body, full, "text/csv")

        def _text(self, body, full, ctype="text/plain; charset=utf-8"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Last-Modified", self.date_time_string(int(os.path.getmtime(full))))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            body = json.dumps(obj).encode()
            if not budget.allows(len(body)):
                return self.send_error(429, "daily data budget spent")
            return self._json_body(body)

        def _json_body(self, body):
            budget.charge(len(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    return H


def index(state_dir, control=False):
    def rj(name, default):
        try:
            return json.load(open(os.path.join(state_dir, name)))
        except Exception:
            return default
    hl = os.path.join(state_dir, "hline")
    return {"status": rj("status.json", {}), "totals": rj("totals.json", {}), "tail": True,
            "control": control,
            "cal_pending": os.path.exists(os.path.join(state_dir, CAL_REQUEST)),
            "curate": rj("curate_summary.json", {}),
            "files": {k: os.path.exists(os.path.join(state_dir, k)) for k in
                      ("latest.png", "latest.npz", "hits.csv", "rfi.csv", "candidates.csv",
                       "candidates_oncomb.csv", "blocks.csv")},
            "hline": {"latest": os.path.exists(os.path.join(hl, "latest.png")),
                      "latest_raw": os.path.exists(os.path.join(hl, "latest_raw.png")),
                      "cal": os.path.exists(os.path.join(hl, "cal.dat"))}}


def serve(state_dir, bind="127.0.0.1", port=8090, background=True, daily_mb=0, token=""):
    mimetypes.add_type("application/octet-stream", ".npz")
    mimetypes.add_type("text/csv", ".csv")
    budget = Budget(daily_mb, os.path.join(state_dir, "budget.json") if daily_mb else None)
    srv = ThreadingHTTPServer((bind, int(port)),
                              _handler(os.path.abspath(state_dir), budget, str(token or "")))
    srv.daemon_threads = True
    if background:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv
    srv.serve_forever()

# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Desktop app. Plain native widgets, no theme. Shows what a node is doing,
starts and stops it locally, and reads remote nodes over their API."""
import calendar
import concurrent.futures
import contextlib
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from PySide6.QtCore import QEvent, QProcess, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QImage,
    QPainter,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, autostart, config, lock, sdr, skymap
from .config import MIN_SETI_CHANNELS

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon-256.png")


class Poller(QThread):
    """Collects everything one refresh needs, off the GUI thread."""
    done = Signal(object)
    part = Signal(object)       # the index alone, before the files: status paints at once

    def __init__(self, node, want_scan, running=False, cfg=None, want_block=None,
                 want_log=False):
        super().__init__()
        self.node = node
        self.cfg = cfg
        self.want_scan = want_scan
        self.want_block = want_block
        self.want_log = want_log
        self.running = running
        self.cancel = False                 # set when the user moves to another node

    def _stopped(self):
        return self.cancel

    def run(self):
        try:
            d = self._collect()
        except Exception as e:              # a bad file must not freeze every panel
            d = {"index": None, "error": str(e)}
        if not self.cancel:
            self.done.emit(d)

    def _collect(self):
        n = self.node
        idx = n.index()
        if idx is None and n.remote:
            return {"index": None}          # unreachable: no point waiting on every file
        if n.remote and not self.cancel:
            self.part.emit({"index": idx, "partial": True})
        # tables grow or get rewritten every block, so on a remote node they are refreshed
        # every 10 minutes, not every poll; the cached copy is reused in between
        slow_ok = (not n.remote) or (time.time() - getattr(n, "slow_at", 0) > 600)
        # fetches within a stage run side by side; a slow link is paid once per stage
        hl = (idx or {}).get("hline") if n.remote else None
        st = self._stopped
        slow = getattr(n, "slow", {"scans": ([], []), "cands": ([], []), "blocks": ([], [])})
        d = {"index": idx, "scans": slow["scans"], "cands": slow["cands"],
             "blocks": slow.get("blocks", ([], [])), "log": None,
             "dish": None if n.remote else config.pointing(self.cfg)["dish"]}

        def stage(jobs):
            """Fetch one group of files side by side, then hand the GUI what is in so far.
            Stages run in order of what the user looks at first: a far node on a slow link
            shows its waterfall before its pictures, and its pictures before its tables."""
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                futs = {k: ex.submit(fn) for k, fn in jobs.items()}
                d.update({k: f.result() for k, f in futs.items()})
            if n.remote and not self.cancel:
                self.part.emit(dict(d, partial=True))

        stage({"npz": lambda: n.get("latest.npz", st)})
        jobs = {"hl_cal": (lambda: n.get("hline/latest.png", st)) if hl is None or hl.get("latest")
                else (lambda: None),
                "hl_raw": (lambda: n.get("hline/latest_raw.png", st))
                if hl is None or hl.get("latest_raw") else (lambda: None)}
        if self.want_scan:
            f = self.want_scan
            jobs["sel_cal"] = lambda: n.get(f"hline/{f}.png", st)
            jobs["sel_raw"] = lambda: n.get(f"hline/{f}_raw.png", st)
            jobs["sel_hdr"] = lambda: n.get(f"hline/{f}.header", st)
        if self.want_block:
            jobs["sel_block"] = lambda: n.get(f"waterfalls/{self.want_block}.png", st)
        stage(jobs)
        jobs = {}
        if slow_ok:
            jobs["_scans"] = lambda: n.csv_rows("hline/scans.csv", st)
            jobs["_cands"] = lambda: n.csv_rows("candidates.csv", st)
            jobs["_blocks"] = lambda: self._blocks(n, idx, st)
        if self.running:
            pass                            # the local child appends its own output live
        elif not n.remote:
            jobs["log"] = lambda: n.tail("survey.log", 20000)
        elif self.want_log and idx and idx.get("tail"):
            # only while the Log tab is open: metered links
            jobs["log"] = lambda: n.get("survey.log?tail=8000", st)
        if jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                futs = {k: ex.submit(fn) for k, fn in jobs.items()}
                d.update({k: f.result() for k, f in futs.items()})
        if slow_ok and self.cancel:
            return d                        # abandoned: the cached tables stay as they were
        if slow_ok:
            n.slow_at = time.time()
            blocks = d.pop("_blocks")
            n.slow = {"scans": d.pop("_scans"), "cands": d.pop("_cands"),
                      "blocks": blocks if blocks is not None else slow.get("blocks", ([], []))}
            d.update({"scans": n.slow["scans"], "cands": n.slow["cands"],
                      "blocks": n.slow["blocks"]})
        return d

    @staticmethod
    def _blocks(n, idx, stop=None):
        """Block list: blocks.csv, topped up from the waterfall folder while the csv is
        still short (a node updated today has thousands of older pictures and no rows
        for them). Once the csv has 200 rows the folder is not listed any more."""
        files = (idx or {}).get("files") or {}
        has_csv = files.get("blocks.csv")
        head, rows = n.csv_rows("blocks.csv", stop) if has_csv or not n.remote else ([], [])
        if has_csv and not head:
            return None               # the node has the file but the fetch failed: keep the table
        if len(rows) >= 200:
            return head, rows
        names = []
        if n.remote:
            if not idx:
                return None
            b = n.get("waterfalls/", stop)
            if b is None:
                return None
            try:
                names = json.loads(b)
            except ValueError:
                names = []
        else:
            with contextlib.suppress(OSError):
                names = os.listdir(os.path.join(n.base, "waterfalls"))
        have = {r[0] for r in rows if r}
        extra = [[f[:-4], "", f, "", "", "", ""] for f in sorted(names)
                 if f.endswith(".png") and f[:-4] not in have]
        head = head or ["utc", "local", "thumb", "hits", "carriers", "top_snr", "center_hz"]
        return head, sorted(extra + rows, key=lambda r: r[0])


class Call(QThread):
    """Runs one blocking call (a node fetch, a request) off the GUI thread."""
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.done.emit(e)


class Probe(QThread):
    done = Signal(object)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        self.done.emit(sdr.probe(self.cfg))


class Node:
    """A state directory, local (path) or remote (http://host:port)."""

    def __init__(self, name, base, token=""):
        self.name = name
        self.base = base
        self.token = token or ""
        self.remote = base.startswith("http")

    def request_cal(self):
        """Ask a remote node for a calibration. Returns (ok, message)."""
        req = urllib.request.Request(f"{self.base}/cal", data=b"", method="POST",
                                     headers={"X-Aquila-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status == 200, "queued"
        except urllib.error.HTTPError as e:
            return False, {403: "wrong control token", 404: "this node's software is too old "
                           "to accept calibration requests",
                           409: "the node is not observing right now"}.get(
                               e.code, f"node said {e.code}")
        except Exception as e:
            return False, f"node unreachable: {e}"

    def get(self, rel, stop=None):
        """Remote files are fetched with If-Modified-Since: an unchanged file costs a
        few hundred bytes, which is what keeps a metered node inside its daily budget.
        stop() true while reading abandons the transfer (the user moved to another node)."""
        try:
            if self.remote:
                cache = self.__dict__.setdefault("_cache", {})
                # a cached copy older than this is refetched in full even if the node
                # says "not modified": a clock step on the node could otherwise answer
                # 304 forever and freeze every pane on a stale spectrum
                c = cache.get(rel)
                if c and time.time() - c[2] > 900:
                    cache.pop(rel, None)
                    c = None
                req = urllib.request.Request(f"{self.base}/{rel}")
                if c:
                    req.add_header("If-Modified-Since", c[0])
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        chunks = []
                        while True:
                            if stop is not None and stop():
                                return None
                            b = r.read(16384)
                            if not b:
                                break
                            chunks.append(b)
                        data = b"".join(chunks)
                        self.over_budget = False
                        lm = r.headers.get("Last-Modified")
                        if lm:
                            cache[rel] = (lm, data, time.time())
                        return data
                except urllib.error.HTTPError as e:
                    if e.code == 304 and c:
                        return c[1]
                    if e.code == 429:
                        self.over_budget = True
                        return c[1] if c else None
                    return None
            with open(os.path.join(self.base, rel), "rb") as f:
                return f.read()
        except Exception:
            return None

    def tail(self, rel, nbytes):
        try:
            p = os.path.join(self.base, rel)
            with open(p, "rb") as f:
                f.seek(max(0, os.path.getsize(p) - nbytes))
                return f.read()
        except Exception:
            return None

    def index(self):
        if self.remote:
            b = self.get("index.json")
            try:
                return json.loads(b) if b else None
            except ValueError:
                return None
        from .api import index
        return index(self.base) if os.path.isdir(self.base) else {}

    def csv_rows(self, rel, stop=None):
        b = self.get(rel, stop)
        if not b:
            return [], []
        try:
            rows = list(csv.reader(io.StringIO(b.replace(b"\0", b"").decode(errors="replace"))))
        except csv.Error:
            return [], []
        rows = [r for r in rows if r]
        return (rows[0], rows[1:]) if rows else ([], [])


class ButtonDelegate(QStyledItemDelegate):
    """Draws a table cell as a coloured button and reports clicks. One delegate serves
    thousands of rows; real QPushButtons per row would not."""
    clicked = Signal(int, int)

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)

    def paint(self, painter, option, index):
        text = index.data()
        if not text:
            return
        r = option.rect.adjusted(4, 3, -4, -3)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(r, 4, 4)
        painter.setPen(Qt.white)
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton \
                and index.data():
            self.clicked.emit(index.row(), index.column())
            return True
        return False


class Downloader(QThread):
    """Copies one scan or block out of a node into a folder of the user's choice:
    the plot(s), the raw files, and a plain CSV anyone can open."""
    done = Signal(str)

    def __init__(self, node, kind, key, dest, filters=False):
        super().__init__()
        self.node, self.kind, self.key, self.dest = node, kind, key, dest
        self.filters = filters            # node serves hits.csv?utc=... (api "tail" flag)

    def run(self):
        try:
            got = self._hline() if self.kind == "hline" else self._block()
        except Exception as e:
            self.done.emit(f"Download failed: {e}")
            return
        if not got:
            self.done.emit("Nothing could be fetched for that entry (files already rotated "
                           "out on the node, or the node is unreachable).")
        else:
            self.done.emit("Saved to {}:\n\n{}".format(self.dest, "\n".join(got)))

    def _save(self, rel, name=None):
        b = self.node.get(rel)
        if not b:
            return None
        name = name or os.path.basename(rel)
        with open(os.path.join(self.dest, name), "wb") as f:
            f.write(b)
        return name

    def _hline(self):
        f = self.key
        got = [x for x in (self._save(f"hline/{f}_raw.png"), self._save(f"hline/{f}.png"),
                           self._save(f"hline/{f}.dat"), self._save(f"hline/{f}.header")) if x]
        dat, hdr = f"{f}.dat", f"{f}.header"
        if dat in got and hdr in got:
            obs = {}
            for line in open(os.path.join(self.dest, hdr)):
                k, _, v = line.strip().partition("=")
                obs[k] = v
            chans = int(obs["channels"])
            w = np.fromfile(os.path.join(self.dest, dat), dtype=np.float32)
            w = w[:w.size // chans * chans].reshape(-1, chans)
            f0 = float(obs["frequency"])
            freq = f0 + (np.arange(chans) - chans / 2.0) * float(obs["bandwidth"]) / chans
            vel = 299792.458 * (1420405751.768 - freq) / 1420405751.768
            name = f"{f}.csv"
            with open(os.path.join(self.dest, name), "w", newline="") as fh:
                wr = csv.writer(fh)
                wr.writerow(["channel", "freq_mhz", "velocity_kms", "power_mean"] +
                            [f"row{i}" for i in range(w.shape[0])])
                mean = w.mean(axis=0)
                for i in range(chans):
                    wr.writerow([i, f"{freq[i] / 1e6:.6f}", f"{vel[i]:.2f}", f"{mean[i]:.6g}"] +
                                [f"{v:.6g}" for v in w[:, i]])
            got.append(name)
        return got

    def _block(self):
        utc = self.key
        got = [x for x in (self._save(f"waterfalls/{utc}.png"),) if x]
        rows = []
        if self.node.remote and not self.filters:
            got.append("(hits list skipped: this node's software is too old to filter "
                       "hits.csv by block, and the whole table is too big for its link)")
            return got
        for rel in ("hits.csv", "rfi.csv"):
            b = self.node.get(f"{rel}?utc={utc}" if self.node.remote else rel)
            if not b:
                continue
            r = list(csv.reader(io.StringIO(b.decode(errors="replace"))))
            if r and not rows:
                rows.append(r[0] + ["kind"])
            kind = "candidate" if rel == "hits.csv" else "carrier"
            rows += [x + [kind] for x in r[1:] if x and x[0] == utc]
        if len(rows) > 1:
            name = f"{utc}_hits.csv"
            with open(os.path.join(self.dest, name), "w", newline="") as fh:
                csv.writer(fh).writerows(rows)
            got.append(name)
        return got


def pixmap_from(data):
    if not data:
        return None
    img = QImage.fromData(data)
    return QPixmap.fromImage(img) if not img.isNull() else None


def autocrop(pm, tol=12, pad=6):
    """Trim the plain margin around a figure (virgo's PNGs are mostly margin)."""
    if pm is None:
        return None
    img = pm.toImage().convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    a = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :3]
    bg = a[0, 0].astype(int)
    diff = np.abs(a.astype(int) - bg).max(axis=2) > tol
    rows, cols = np.where(diff.any(axis=1))[0], np.where(diff.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return pm
    y0, y1 = max(0, rows[0] - pad), min(h, rows[-1] + pad + 1)
    x0, x1 = max(0, cols[0] - pad), min(w, cols[-1] + pad + 1)
    return pm.copy(int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def stamp(utc, node_local=""):
    """One way of writing a time everywhere: the node's own clock, then UTC.
    '20260929T131944Z', '2026-09-29 18:49:44' -> '2026-09-29 18:49:44 node time (13:19:44 UTC)'."""
    u = ""
    if utc and len(utc) >= 15:
        u = f"{utc[9:11]}:{utc[11:13]}:{utc[13:15]} UTC"
    if node_local and u:
        return f"{node_local} node time ({u})"
    if node_local:
        return f"{node_local} node time"
    if u:
        return f"{utc[0:4]}-{utc[4:6]}-{utc[6:8]} {u}"
    return ""


def scan_time(fname):
    """'obs_20260929_180926.dat' -> '2026-09-29 18:09:26' (the node's clock)."""
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", fname or "")
    return "{}-{}-{} {}:{}:{}".format(*m.groups()) if m else ""


class ImagePane(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self._placeholder = text
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(200, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pm = None

    def set_data(self, data):
        if data is not None and data == getattr(self, "_data", None):
            return
        self._data = data
        self._pm = autocrop(pixmap_from(data))
        if self._pm is None:
            self.setPixmap(QPixmap())
            self.setText(self._placeholder)
        self._fit()

    def resizeEvent(self, e):
        self._fit()
        super().resizeEvent(e)

    def _fit(self):
        if self._pm:
            pm = self._pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.setPixmap(pm)


class SkyMap(QWidget):
    """Equatorial map, RA increasing to the left like the sky. Galactic plane band,
    the beam to scale, its drift track, and the pointing numbers underneath."""

    def __init__(self):
        super().__init__()
        import pyqtgraph as pg
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        self.plot.getViewBox().disableAutoRange()
        self._full_sky()
        ax = self.plot.getAxis("bottom")
        ax.setTicks([[(-180 + 15 * h, f"{(12 - h) % 24}h") for h in range(0, 25, 3)]])
        self.plot.getAxis("left").setTicks([[(d, f"{d:+d}") for d in range(-60, 61, 30)]])
        self.img = pg.ImageItem()
        self.plot.addItem(self.img)
        for b in (-10, 0, 10):
            ra, dec = skymap.plane_track(b)
            pen = pg.mkPen("#e08a3c", width=2 if b == 0 else 1,
                           style=Qt.SolidLine if b == 0 else Qt.DotLine)
            for xs, ys in skymap.split_wrap(self._x(ra), dec):
                self.plot.plot(xs, ys, pen=pen)
        gra, gdec = skymap.galactic_to_radec([0.0], [0.0])
        self.plot.plot([float(self._x(gra[0]))], [float(gdec[0])], pen=None, symbol="star",
                       symbolSize=12, symbolBrush="#e08a3c", symbolPen=None)
        self.beam = pg.QtWidgets.QGraphicsEllipseItem()
        self.beam.setPen(pg.mkPen("#3070c0", width=2))
        self.plot.addItem(self.beam)
        self.cross = self.plot.plot([], [], pen=None, symbol="+", symbolSize=12,
                                    symbolPen=pg.mkPen("#3070c0", width=2))
        self.label = pg.TextItem("", color="#3070c0", anchor=(0.5, 1.2))
        self.plot.addItem(self.label)
        lay.addWidget(self.plot)
        self.info = QLabel("Set the site location in Settings to see where the dish points.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.credit = QLabel("")
        self.credit.setStyleSheet("color: #777;")
        lay.addWidget(self.credit)
        lay.addStretch(1)
        self._bg_done = False

    @staticmethod
    def _x(ra_deg):
        return -(((np.asarray(ra_deg, float) + 180.0) % 360.0) - 180.0)

    def _full_sky(self):
        self.plot.getViewBox().setRange(xRange=(-180, 180), yRange=(-90, 90), padding=0)

    def resizeEvent(self, e):
        QTimer.singleShot(0, self._full_sky)
        want = self.width() // 2 + 44
        room = self.height() - self.info.sizeHint().height() - self.credit.sizeHint().height() - 12
        self.plot.setFixedHeight(max(120, min(want, room)))
        super().resizeEvent(e)

    def load_background(self):
        if self._bg_done:
            return
        self._bg_done = True
        self.set_background(skymap.background())

    def set_background(self, p):
        if p:
            im = QImage(p).convertToFormat(QImage.Format_RGBA8888)
            w, h = im.width(), im.height()
            arr = np.frombuffer(im.constBits(), np.uint8).reshape(h, w, 4).copy()
            self.img.setImage(np.transpose(arr[::-1], (1, 0, 2)), autoLevels=False,
                              levels=(0, 255))
            self.img.setRect(QRectF(-180, -90, 360, 180))
            self.credit.setText(skymap.CREDIT)
            self._full_sky()

    def update_pointing(self, cfg, name="", t_unix=None):
        """Dish marker for cfg's pointing, at t_unix (a selected scan's time) or now."""
        pt = skymap.pointing(cfg, t_unix if t_unix else time.time())
        if not pt:
            self.cross.setData([], [])
            self.beam.setRect(0, 0, 0, 0)
            self.label.setText("")
            self.info.setText("Set the site location in Settings to see where the dish points.")
            return
        x, y = float(self._x(pt["ra_deg"])), pt["dec_deg"]
        bw = max(pt["beam_deg"], 2.0)
        wid = bw / max(math.cos(math.radians(y)), 0.2)
        self.beam.setRect(x - wid / 2, y - bw / 2, wid, bw)
        self.cross.setData([x], [y])
        self.label.setText(name)
        self.label.setPos(x, y + bw / 2)
        when = time.strftime("scan %Y-%m-%d %H:%M UTC     ", time.gmtime(t_unix)) if t_unix else ""
        self.info.setText(
            when + f"Az {pt['az']:.1f}  El {pt['el']:.1f}     RA {pt['ra_deg'] / 15:.2f} h  "
            f"Dec {pt['dec_deg']:+.1f}     Galactic l {pt['l']:.1f}  b {pt['b']:+.1f}     "
            f"LST {pt['lst_h']:.2f} h     beam {pt['beam_deg']:.1f} deg")


class Settings(QDialog):
    """The few things a user sets. Everything else stays in the config file."""

    def __init__(self, cfg, parent=None, current_sdr=None):
        """current_sdr: the SDR this computer's node is using right now (None = not observing)."""
        super().__init__(parent)
        self.setWindowTitle("Settings (this computer's node)")
        self.cfg = json.loads(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}))
        self.cfg["_path"] = cfg.get("_path")
        self.cfg["_state_dir_set"] = cfg.get("_state_dir_set")
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry().height() if scr else 720
        self.resize(860, min(720, avail - 80))
        self.sdr = QComboBox()
        cur = (sdr.SOURCES[current_sdr].label if current_sdr in sdr.SOURCES
               else (current_sdr or "none"))
        self.sdr.addItem(f"Auto (first SDR found; current: {cur})", "auto")
        for name, label, ok, install in sdr.choices():
            self.sdr.addItem(label if ok else f"{label}  (not installed: {install})", name)
            if not ok:
                self.sdr.model().item(self.sdr.count() - 1).setEnabled(False)
        i = self.sdr.findData(cfg["sdr"])
        self.sdr.setCurrentIndex(i if i >= 0 else 0)
        row = QHBoxLayout()
        row.addWidget(self.sdr, 1)
        self.check = QPushButton("Check")
        self.check.clicked.connect(self.check_sdr)
        row.addWidget(self.check)
        form.addRow("Receiver (SDR):", row)
        self.sdr_result = QLabel("")
        self.sdr_result.setWordWrap(True)
        if current_sdr:
            self.sdr_result.setText(f"In use now by this computer's node: {cur}.")
        else:
            self.sdr_result.setText("Not observing. Looking for a connected receiver...")
            self._scan = Call(sdr.attached)
            self._scan.done.connect(lambda names: self.sdr_result.setText(
                "Not observing. Connected: " + ", ".join(sdr.SOURCES[n].label for n in names)
                if names and not isinstance(names, Exception) else
                "Not observing. No receiver connected."))
            self._scan.start()
        self.sdr_result.setStyleSheet("color: gray")
        form.addRow(self.sdr_result)
        self.freq = self._spin(0, 30000, cfg["center_mhz"], 4, " MHz")
        form.addRow("Centre frequency:", self.freq)
        fnote = QLabel("Hydrogen line is 1420.4058 MHz. Keep it off the centre; the default "
                       "1420.0000 puts it 400 kHz up, clear of the DC spike.")
        fnote.setWordWrap(True)
        form.addRow(fnote)
        self.rate = QComboBox()
        form.addRow("Bandwidth (sample rate):", self.rate)
        self.sdr.currentIndexChanged.connect(self._fill_rates)
        self._fill_rates()
        self.fft = QComboBox()
        for p in (10, 12, 14, 16, 18, 19, 20, 21, 22):
            tag = "  (recommended)" if p == 20 else ("  (hydrogen line only)" if p < 18 else "")
            self.fft.addItem(f"2^{p}{tag}", 1 << p)
        i = self.fft.findData(int(cfg["advanced"]["n_chan"]))
        self.fft.setCurrentIndex(i if i >= 0 else self.fft.findData(1 << 20))
        row = QHBoxLayout()
        row.addWidget(self.fft, 1)
        self.fft_info = QLabel("")
        self.fft_info.setWordWrap(True)
        row.addWidget(self.fft_info, 1)
        form.addRow("Narrowband resolution (FFT size):", row)
        self.fft.currentIndexChanged.connect(self._fft_info)
        self.rate.currentIndexChanged.connect(self._fft_info)
        self._fft_info()
        d = cfg["dish"]
        self.az = self._spin(0, 360, d["az"], 1, " deg")
        self.el = self._spin(0, 90, d["el"], 1, " deg")
        self.dia = self._spin(0.1, 100, d.get("diameter_m", 1.0), 2, " m")
        self.az.setToolTip("Compass bearing the dish faces: 0 north, 90 east, 180 south, 270 west")
        self.el.setToolTip("0 is the horizon, 90 is straight up")
        form.addRow("Dish azimuth (0 = north):", self.az)
        form.addRow("Dish elevation (90 = straight up):", self.el)
        form.addRow("Dish diameter:", self.dia)
        self.rot = QLineEdit(d.get("rotator", ""))
        self.rot.setPlaceholderText("blank = fixed dish")
        self.rot.setToolTip("Hamlib rotctld address, host:port (port 4533 if omitted). Works with "
                            "rotctld in front of any supported rotator, or the Aquila dish bridge. "
                            "Azimuth and elevation above are then read live and ignored here.")
        form.addRow("Rotator (rotctld host:port):", self.rot)
        rnote = QLabel("With a rotator set, the dish pointing comes from it live.")
        rnote.setStyleSheet("color: gray")
        form.addRow(rnote)

        def _rot_toggle():
            fixed = not self.rot.text().strip()
            self.az.setEnabled(fixed)
            self.el.setEnabled(fixed)
        self.rot.textChanged.connect(_rot_toggle)
        _rot_toggle()
        site = cfg.get("site") or {}
        self.lat = self._spin(-90, 90, site.get("lat") or 0.0, 4, " deg")
        self.lon = self._spin(-180, 180, site.get("lon") or 0.0, 4, " deg")
        self.alt = self._spin(-500, 9000, site.get("alt") or 0.0, 0, " m")
        self.lat.setToolTip("Decimal degrees, north positive, e.g. 30.6400")
        self.lon.setToolTip("Decimal degrees, east positive, e.g. 76.8200")
        form.addRow("Site latitude (north +):", self.lat)
        form.addRow("Site longitude (east +):", self.lon)
        form.addRow("Site altitude:", self.alt)
        note = QLabel("Latitude and longitude in decimal degrees, as a phone map app shows them. "
                      "Leave both at 0 to skip. Used only for the sky map and velocity axes; "
                      "kept in the config file on this computer, never sent anywhere.")
        note.setWordWrap(True)
        form.addRow(note)
        row = QHBoxLayout()
        self.folder = QLineEdit(cfg["state_dir"])
        self.folder.setReadOnly(True)
        row.addWidget(self.folder, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self.pick_folder)
        row.addWidget(b)
        form.addRow("Data folder:", row)
        self.token = QLineEdit(cfg["advanced"].get("api_token", ""))
        self.token.setPlaceholderText("blank = other computers can only view this node")
        self.token.setToolTip("A word other copies of the app must give to ask this node for a "
                              "receiver calibration over the network. Takes effect when the "
                              "node is next started.")
        form.addRow("Control token:", self.token)
        self.auto_login = QCheckBox("Open this app when I log in to the computer")
        self.auto_login.setChecked(autostart.enabled())
        self.auto_obs = QCheckBox("Start observing as soon as the app opens")
        self.auto_obs.setChecked(bool((cfg.get("app") or {}).get("observe_on_open")))
        self.narrow = QCheckBox("Narrowband search as well as the hydrogen line")
        self.narrow.setChecked((cfg.get("app") or {}).get("narrowband", True))
        self.narrow.setToolTip("Off: the app observes the hydrogen line only and the search "
                               "tabs are hidden. Change it any time.")
        form.addRow("Observe:", self.narrow)
        form.addRow("Startup:", self.auto_login)
        form.addRow("", self.auto_obs)
        snote = QLabel("Tick both and a node comes back by itself after a power cut, once "
                       "the computer is set to power on with mains and to log in "
                       "automatically.")
        snote.setWordWrap(True)
        snote.setStyleSheet("color: gray")
        form.addRow(snote)
        sv = cfg.get("save") or {}
        self.s_csv = QCheckBox("Hit and scan tables (CSV)")
        self.s_csv.setChecked(sv.get("csv", True))
        self.s_plots = QCheckBox("Plots (block images and virgo hydrogen line plots)")
        self.s_plots.setChecked(sv.get("plots", True))
        self.s_spectra = QCheckBox("Spectra (virgo scan files and hit snippets)")
        self.s_spectra.setChecked(sv.get("spectra", True))
        self.s_raw = QCheckBox("Raw IQ samples for every block")
        self.s_raw.setChecked(sv.get("raw_iq", False))
        form.addRow("Save:", self.s_csv)
        form.addRow("", self.s_plots)
        form.addRow("", self.s_spectra)
        form.addRow("", self.s_raw)
        warn = QLabel("Raw IQ is 1.5 GB per block at 2.5 Msps and 6 GB at 10 Msps, so 35 to "
                      "140 GB an hour. Unless the data folder is on a drive with terabytes "
                      "free, it will fill in hours. Raw files are never deleted by the app; "
                      "a block is skipped when the drive cannot hold it.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #a00000;")
        form.addRow(warn)
        scope = QLabel("Everything on this page is for this computer's own node only. "
                       "A remote node is set up on its own computer; nothing here is sent "
                       "to it or changes it.")
        scope.setWordWrap(True)
        scope.setStyleSheet("color: gray")
        form.addRow(scope)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def check_sdr(self):
        self.check.setEnabled(False)
        self.sdr_result.setText("Opening the SDR...")
        cfg = {k: v for k, v in self.apply().items() if not k.startswith("_")}
        self._probe = Probe(cfg)
        self._probe.done.connect(self._probe_done)
        self._probe.start()

    def _probe_done(self, res):
        ok, msg = res
        self.sdr_result.setText(msg)
        self.sdr_result.setStyleSheet("" if ok else "color: #a00000;")
        self.check.setEnabled(True)
        pass

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self._folder_set = True

    def _sdr_class(self):
        name = self.sdr.currentData()
        return sdr.SOURCES.get(name) or sdr.SOURCES[(sdr.detect() or ["airspy"])[0]]

    def _fill_rates(self):
        cls = self._sdr_class()
        cur = int(self.cfg.get("sample_rate") or 0)
        self.rate.blockSignals(True)
        self.rate.clear()
        self.rate.addItem(f"Device default ({cls.default_rate / 1e6:g} Msps)", 0)
        for r in cls.rates:
            self.rate.addItem(f"{r / 1e6:g} Msps", r)
        i = self.rate.findData(cur)
        self.rate.setCurrentIndex(i if i >= 0 else 0)
        self.rate.blockSignals(False)
        if hasattr(self, "fft_info"):
            self._fft_info()

    def _fft_info(self):
        n = int(self.fft.currentData())
        cls = self._sdr_class()
        rate = float(self.rate.currentData() or cls.default_rate)
        adv = self.cfg["advanced"]
        chan = rate / n
        block = float(adv["dwell_s"]) - float(adv["settle_skip_s"])
        if n < int(adv.get("seti_min_chan") or MIN_SETI_CHANNELS):
            self.fft_info.setText(f"{chan:.0f} Hz channels. Too coarse for the narrowband search, "
                                  "so only the hydrogen line runs.")
        else:
            self.fft_info.setText(f"{chan:.2f} Hz channels, drift step {chan / block:.3f} Hz/s. "
                                  "Narrowband search and hydrogen line both run.")

    @staticmethod
    def _spin(lo, hi, val, dec, suffix):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(float(val))
        s.setSuffix(suffix)
        return s

    def apply(self):
        c = json.loads(json.dumps({k: v for k, v in self.cfg.items() if not k.startswith("_")}))
        c["_path"] = self.cfg.get("_path")
        c["_state_dir_set"] = self.cfg.get("_state_dir_set")
        c["sdr"] = self.sdr.currentData()
        if getattr(self, "_folder_set", False):
            c["state_dir"] = self.folder.text()
            c["_state_dir_set"] = True
        c["save"] = {"csv": self.s_csv.isChecked(), "plots": self.s_plots.isChecked(),
                     "spectra": self.s_spectra.isChecked(), "raw_iq": self.s_raw.isChecked()}
        c["app"] = dict(c.get("app") or {}, observe_on_open=self.auto_obs.isChecked(),
                        narrowband=self.narrow.isChecked())
        c["_autostart"] = self.auto_login.isChecked()
        c["center_mhz"] = self.freq.value()
        c["sample_rate"] = int(self.rate.currentData() or 0)
        c["advanced"]["n_chan"] = int(self.fft.currentData())
        c["advanced"]["api_token"] = self.token.text().strip()
        c["dish"].update({"rotator": self.rot.text().strip(),
                          "az": self.az.value(), "el": self.el.value(),
                          "diameter_m": self.dia.value()})
        if self.lat.value() == 0.0 and self.lon.value() == 0.0:
            c["site"] = {"lat": None, "lon": None, "alt": 0}
        else:
            c["site"] = {"lat": self.lat.value(), "lon": self.lon.value(),
                         "alt": self.alt.value()}
        return c


class QuickSetup(QDialog):
    """The one page a new user needs: what this computer observes with and where the dish
    points, every field already filled with what the app will use. Settings has the rest."""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick setup (this computer's node)")
        self.cfg = cfg
        outer = QVBoxLayout(self)
        intro = QLabel("Receiver and dish for this computer's node.")
        outer.addWidget(intro)
        form = QFormLayout()
        outer.addLayout(form)
        self.name = QLineEdit(cfg.get("name") or "my-dish")
        self.name.setToolTip("Shown as the node's name in this app and to other computers")
        form.addRow("Node name:", self.name)
        self.sdr = QComboBox()
        found = sdr.detect()
        self.sdr.addItem("Auto detect  (detecting...)", "auto")
        for name, label, ok, install in sdr.choices():
            self.sdr.addItem(label if ok else f"{label}  (not installed: {install})", name)
            if not ok:
                self.sdr.model().item(self.sdr.count() - 1).setEnabled(False)
        want = cfg.get("sdr") if cfg.get("sdr") in found else "auto"
        self.sdr.setCurrentIndex(max(self.sdr.findData(want), 0))
        row = QHBoxLayout()
        row.addWidget(self.sdr, 1)
        self.check = QPushButton("Check")
        self.check.clicked.connect(self.check_sdr)
        row.addWidget(self.check)
        form.addRow("Receiver (SDR):", row)
        self.sdr_result = QLabel("Looking for a connected receiver...")
        self.sdr_result.setWordWrap(True)
        self.sdr_result.setStyleSheet("color: gray")
        form.addRow(self.sdr_result)
        self._scan = Call(sdr.attached)
        self._scan.done.connect(lambda names, found=found: self._scanned(names, found))
        self._scan.start()
        self.mode = QComboBox()
        self.mode.addItem("Hydrogen line only", False)
        self.mode.addItem("Hydrogen line and narrowband search", True)
        self.mode.setCurrentIndex(1 if (cfg.get("app") or {}).get("narrowband", True) else 0)
        self.mode.setToolTip("The narrowband search looks for drifting signals across the "
                             "band in the same observation. Can be turned on or off later "
                             "in Settings.")
        form.addRow("Observe:", self.mode)
        self.freq = Settings._spin(0, 30000, cfg["center_mhz"], 4, " MHz")
        form.addRow("Centre frequency:", self.freq)
        d = cfg["dish"]
        self.az = Settings._spin(0, 360, d["az"], 1, " deg")
        self.el = Settings._spin(0, 90, d["el"], 1, " deg")
        self.dia = Settings._spin(0.1, 100, d.get("diameter_m", 1.0), 2, " m")
        self.az.setToolTip("Compass bearing the dish faces: 0 north, 90 east, 180 south, 270 west")
        self.el.setToolTip("0 is the horizon, 90 is straight up")
        form.addRow("Dish azimuth (0 = north):", self.az)
        form.addRow("Dish elevation (90 = straight up):", self.el)
        form.addRow("Dish diameter:", self.dia)
        site = cfg.get("site") or {}
        self.lat = Settings._spin(-90, 90, site.get("lat") or 0.0, 4, " deg")
        self.lon = Settings._spin(-180, 180, site.get("lon") or 0.0, 4, " deg")
        form.addRow("Site latitude (north +):", self.lat)
        form.addRow("Site longitude (east +):", self.lon)
        snote = QLabel("Decimal degrees. Leave at 0 to skip the sky map.")
        snote.setStyleSheet("color: gray")
        form.addRow(snote)
        folder = QLabel(cfg["state_dir"])
        folder.setWordWrap(True)
        form.addRow("Data folder:", folder)
        adv = cfg["advanced"]
        rest = QLabel(f"Sample rate: receiver default.   Channels: "
                      f"2^{int(adv['n_chan']).bit_length() - 1}.   Block: {adv['dwell_s']} s.   "
                      "Bias tee: on.   Change these in Settings.")
        rest.setWordWrap(True)
        rest.setStyleSheet("color: gray")
        outer.addWidget(rest)
        row = QHBoxLayout()
        more = QPushButton("All settings...")
        more.clicked.connect(lambda: self.done(2))
        row.addWidget(more)
        row.addStretch(1)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(save)
        outer.addLayout(row)
        self.resize(640, self.sizeHint().height())

    def _scanned(self, names, found):
        if isinstance(names, Exception):
            names = []
        labels = [sdr.SOURCES[n].label for n in names]
        self.sdr.setItemText(0, "Auto detect  (now: "
                             + (labels[0] if labels else "none detected") + ")")
        if labels:
            self.sdr_result.setText("Connected: " + ", ".join(labels))
        elif found:
            self.sdr_result.setText("No receiver connected. Software installed for: "
                                    + ", ".join(sdr.SOURCES[n].label for n in found))
        else:
            self.sdr_result.setText("No receiver connected and no SDR software installed. "
                                    "The synthetic source runs without hardware.")

    def check_sdr(self):
        self.check.setEnabled(False)
        self.sdr_result.setText("Opening the SDR...")
        self.sdr_result.setStyleSheet("")
        self._probe = Probe({k: v for k, v in self.apply().items() if not k.startswith("_")})
        self._probe.done.connect(self._probe_done)
        self._probe.start()

    def _probe_done(self, res):
        ok, msg = res
        self.sdr_result.setText(msg)
        self.sdr_result.setStyleSheet("" if ok else "color: #a00000;")
        self.check.setEnabled(True)

    def apply(self):
        c = json.loads(json.dumps({k: v for k, v in self.cfg.items() if not k.startswith("_")}))
        c["_path"] = self.cfg.get("_path")
        c["_state_dir_set"] = self.cfg.get("_state_dir_set")
        c["name"] = self.name.text().strip() or "my-dish"
        c["sdr"] = self.sdr.currentData()
        c["app"] = dict(c.get("app") or {}, narrowband=bool(self.mode.currentData()))
        c["center_mhz"] = self.freq.value()
        c["dish"].update({"az": self.az.value(), "el": self.el.value(),
                          "diameter_m": self.dia.value()})
        if self.lat.value() == 0.0 and self.lon.value() == 0.0:
            c["site"] = {"lat": None, "lon": None, "alt": 0}
        else:
            c["site"] = {"lat": self.lat.value(), "lon": self.lon.value(),
                         "alt": (c.get("site") or {}).get("alt") or 0}
        return c


class Main(QMainWindow):
    def __init__(self, cfg_path=None):
        super().__init__()
        self.cfg_path = cfg_path
        self.cfg = config.load(cfg_path)
        self.setWindowTitle("Aquila Radio Astronomy")
        self.resize(1200, 820)
        self.proc = None
        self.nodes = [Node(self.cfg["name"] + " (this computer)", self.cfg["state_dir"])]
        for r in self.cfg.get("remote_nodes") or []:          # remembered from earlier sessions
            if isinstance(r, dict) and r.get("url"):
                self.nodes.append(Node(r.get("name") or r["url"], r["url"].rstrip("/"),
                                       r.get("token", "")))
        self._menus()
        self._body()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.refresh()
        if not os.path.exists(self.cfg.get("_path") or ""):
            QTimer.singleShot(300, self.first_run)
        elif (self.cfg.get("app") or {}).get("observe_on_open"):
            QTimer.singleShot(3000, self._observe_on_open)

    def _observe_on_open(self):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        other = lock.holder(self.cfg["state_dir"])
        if other:
            # a service or an earlier copy of the app is observing already: leave it
            self.sb.showMessage(f"Not started: the data folder is in use (pid {other})", 15000)
            return
        self.start("run" if self.narrowband() else "hline")

    def first_run(self):
        self.quick_setup()

    def quick_setup(self):
        self.cfg = config.load(self.cfg_path)
        dlg = QuickSetup(self.cfg, self)
        r = dlg.exec()
        probe = getattr(dlg, "_probe", None)
        if probe is not None and probe.isRunning():
            probe.done.connect(lambda *_: dlg.deleteLater())
        else:
            dlg.deleteLater()
        if r == QDialog.Accepted:
            self._save_cfg(dlg.apply())
        elif r == 2:
            self.settings()

    def _save_cfg(self, cfg):
        """Write cfg and bring the window in line with it (name, folder, running child)."""
        config.save(cfg, self.cfg_path)
        self.cfg = config.load(self.cfg_path)     # state_dir follows the name until set by hand
        self.nodes[0] = Node(self.cfg["name"] + " (this computer)", self.cfg["state_dir"])
        self._fill_nodes()
        self._apply_mode()
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            QMessageBox.information(
                self, "Settings", "Dish pointing and site apply from the next block. "
                "SDR, frequency, sample rate and FFT apply at the next Start.")
        self.refresh(force=True)

    def _menus(self):
        m = self.menuBar()
        q = m.addMenu("&Quick setup")
        q.addAction(self._act("Quick setup...", self.quick_setup))
        f = m.addMenu("&File")
        f.addAction(self._act("Connect to node...", self.connect_node))
        self.recent_menu = f.addMenu("Recent nodes")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        f.addAction(self._act("Set control token for this node...", self.set_token))
        f.addAction(self._act("Forget this node", self.forget_node))
        f.addAction(self._act("Open data folder", self.open_folder))
        f.addSeparator()
        f.addAction(self._act("Quit", self.close, "Ctrl+Q"))
        o = m.addMenu("&Observe")
        self.a_seti = self._act("Start observing", lambda: self.start("run"))
        self.a_hline = self._act("Start hydrogen line only", lambda: self.start("hline"))
        self.a_cal = self._act("Take receiver calibration...", self.take_cal)
        self.a_delcal = self._act("Delete receiver calibration...", self.delete_cal)
        self.a_stop = self._act("Stop", lambda: self.stop())
        for a in (self.a_seti, self.a_hline, self.a_cal, self.a_delcal, self.a_stop):
            o.addAction(a)
        st = m.addMenu("&Settings")
        st.addAction(self._act("Settings...", self.settings, "Ctrl+,"))
        h = m.addMenu("&Help")
        h.addAction(self._act("About", self.about))
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addWidget(QLabel(" Node: "))
        self.node_box = QComboBox()
        self.node_box.currentIndexChanged.connect(self._node_changed)
        tb.addWidget(self.node_box)
        tb.addSeparator()
        for a in (self.a_seti, self.a_hline, self.a_stop):
            tb.addAction(a)
        self._fill_nodes()
        last = self.cfg.get("last_node") or ""
        for k, n in enumerate(self.nodes):
            if n.remote and n.base == last:
                self.node_box.blockSignals(True)
                self.node_box.setCurrentIndex(k)
                self.node_box.blockSignals(False)

    def _act(self, text, fn, shortcut=None):
        a = QAction(text, self)
        a.triggered.connect(fn)
        if shortcut:
            a.setShortcut(shortcut)
        return a

    def _node_changed(self):
        self._sv_selected = None
        self._sv_rows = None
        self._last_idx = None
        n = self.node()
        self._force = True
        self._hl_selected = None
        self._hl_rows = None
        self._c_rows = None
        self._wf_ts = None
        if hasattr(self, "sb_label"):
            self.sb_label.setText(f"{n.name}    loading")
            self._clear_panels()
        want = n.base if n.remote else ""
        if self.cfg.get("last_node", "") != want:
            with contextlib.suppress(Exception):
                cfg = config.load(self.cfg_path)
                cfg["last_node"] = want
                config.save(cfg, self.cfg_path)
                self.cfg["last_node"] = want
        self.refresh()

    def _clear_panels(self):
        """Blank every pane so the previous node's data is not mistaken for this one's."""
        self._wf_ts = None
        self.wf_img.clear()
        self.sp_curve.setData([], [])
        self.wf_plot.setTitle("Waterfall (loading)")
        self.sp_plot.setTitle("Spectrum (loading)")
        for pane in (self.hl_cal, self.hl_raw, self.sv_block_pane, self.c_thumb):
            pane.set_data(None)
            pane.setText("Loading...")
        for m in (self.hl_model, self.sv_model, self.c_model):
            m.removeRows(0, m.rowCount())
        for k in self.st:
            self.st[k].setText("-")

    def _fill_nodes(self):
        keep = self.node_box.currentText() if self.node_box.count() else ""
        self.node_box.blockSignals(True)
        self.node_box.clear()
        for n in self.nodes:
            self.node_box.addItem(n.name)
        for k, n in enumerate(self.nodes):
            if n.remote and n.name == keep:
                self.node_box.setCurrentIndex(k)
        self.node_box.blockSignals(False)

    def _body(self):
        import pyqtgraph as pg
        pg.setConfigOptions(background="w", foreground="k", antialias=False)
        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        # Survey
        outer = QSplitter(Qt.Vertical)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        self.sv_banner, self.sv_live, row = self._banner(lambda: self.view_block(None))
        left.addLayout(row)
        self.sv_stack = QStackedWidget()
        split = QSplitter(Qt.Vertical)
        self.wf_plot = pg.PlotWidget(labels={"left": "time (s)", "bottom": "MHz"})
        self.wf_img = pg.ImageItem()
        self.wf_img.setColorMap(pg.colormap.get("inferno"))
        self.wf_plot.addItem(self.wf_img)
        self.sp_plot = pg.PlotWidget(labels={"left": "dB", "bottom": "MHz"})
        self.sp_plot.setXLink(self.wf_plot)
        self.sp_curve = self.sp_plot.plot(pen=pg.mkPen("#b06000", width=1))
        split.addWidget(self.wf_plot)
        split.addWidget(self.sp_plot)
        split.setSizes([450, 220])
        self.sv_stack.addWidget(split)
        self.sv_block_pane = ImagePane("Loading block...")
        self.sv_stack.addWidget(self.sv_block_pane)
        left.addWidget(self.sv_stack, 1)
        lay.addLayout(left, 1)
        box = QGroupBox("Status")
        form = QFormLayout(box)
        self.st = {}
        for key, label in (("state", "State"), ("sdr", "Receiver"), ("center", "Centre"),
                           ("chan", "Channel width"), ("blocks", "Blocks done"),
                           ("hits", "Hits logged"), ("cands", "Candidates A / B / C"),
                           ("disk", "Disk used"), ("up", "Running for")):
            self.st[key] = QLabel("-")
            form.addRow(label + ":", self.st[key])
        box.setFixedWidth(270)
        self.st["state"].setWordWrap(True)
        lay.addWidget(box)
        outer.addWidget(w)
        self.sv_table, self.sv_model = self._scan_table(self.view_block, self.download_block)
        gb = QGroupBox("Blocks (one per dwell)")
        QVBoxLayout(gb).addWidget(self.sv_table)
        outer.addWidget(gb)
        outer.setSizes([600, 220])
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 1)
        self.sv_tab = tabs.addTab(outer, "Narrowband search")

        # Hydrogen line
        w = QSplitter(Qt.Vertical)
        topw = QWidget()
        topv = QVBoxLayout(topw)
        topv.setContentsMargins(0, 0, 0, 0)
        self.hl_banner, self.hl_live, row = self._banner(lambda: self.view_scan(None))
        topv.addLayout(row)
        # one plot at a time: the calibrated plot already contains everything the raw
        # plot shows. The calibrate button sits above whichever plot is visible.
        hdr = QHBoxLayout()
        self.lb_raw = QLabel("Uncalibrated (virgo, raw)")
        self.lb_raw.setStyleSheet("font-weight: bold")
        hdr.addWidget(self.lb_raw)
        self.b_cal = QPushButton("Calibrate now")
        self.b_cal.setStyleSheet("QPushButton { background: #2e9e4f; color: white; "
                                 "font-weight: bold; padding: 3px 12px; border-radius: 4px; }"
                                 "QPushButton:disabled { background: #9ccfae; }")
        self.b_cal.clicked.connect(self.take_cal)
        hdr.addWidget(self.b_cal)
        hdr.addStretch(1)
        topv.addLayout(hdr)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        topv.addLayout(top, 1)
        self.hl_cal = ImagePane("No calibrated plot yet. Take a receiver calibration first.")
        self.hl_raw = ImagePane("No scan yet.")
        self.gb_cal = QGroupBox()
        lay_c = QVBoxLayout(self.gb_cal)
        lay_c.setContentsMargins(2, 2, 2, 2)
        lay_c.addWidget(self.hl_cal)
        top.addWidget(self.gb_cal, 1)
        self.gb_raw = QGroupBox()
        lay_r = QVBoxLayout(self.gb_raw)
        lay_r.setContentsMargins(2, 2, 2, 2)
        lay_r.addWidget(self.hl_raw, 1)
        top.addWidget(self.gb_raw, 1)
        w.addWidget(topw)
        botw = QWidget()
        bottom = QHBoxLayout(botw)
        bottom.setContentsMargins(0, 0, 0, 0)
        self.sky = SkyMap()
        self.gb_sky = QGroupBox("Sky")
        QVBoxLayout(self.gb_sky).addWidget(self.sky)
        bottom.addWidget(self.gb_sky, 1)
        self.hl_top, self.hl_bottom = top, bottom
        self.hl_table, self.hl_model = self._scan_table(self.view_scan, self.download_scan)
        gb = QGroupBox("Scans")
        QVBoxLayout(gb).addWidget(self.hl_table)
        bottom.addWidget(gb, 1)
        w.addWidget(botw)
        w.setSizes([560, 260])
        w.setStretchFactor(0, 3)
        w.setStretchFactor(1, 1)
        self.hl_tab = tabs.addTab(w, "Hydrogen line")

        # Candidates
        w = QWidget()
        lay = QHBoxLayout(w)
        self.c_table = QTableView()
        self.c_model = QStandardItemModel()
        self.c_table.setModel(self.c_model)
        self.c_table.setEditTriggers(QTableView.NoEditTriggers)
        self.c_table.setSelectionBehavior(QTableView.SelectRows)
        self.c_table.setSortingEnabled(True)
        self.c_table.selectionModel().selectionChanged.connect(self.show_cand)
        lay.addWidget(self.c_table, 3)
        right = QVBoxLayout()
        self.c_when = QLabel("")
        right.addWidget(self.c_when)
        self.c_thumb = ImagePane("Select a candidate to see its block.")
        right.addWidget(self.c_thumb, 1)
        lay.addLayout(right, 2)
        self.c_tab = tabs.addTab(w, "Candidates")

        # Log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("Nothing logged yet. A node writes here as it observes; "
                                    "remote nodes are read while this tab is open.")
        self.log_tab = tabs.addTab(self.log, "Log")
        self.tabs = tabs
        tabs.currentChanged.connect(lambda i: i == self.log_tab and self.refresh(force=True))
        self._apply_mode()

    def narrowband(self):
        return bool((self.cfg.get("app") or {}).get("narrowband", True))

    def _apply_mode(self):
        """Hydrogen line only: the search tabs go away and Start runs the line alone."""
        nb = self.narrowband()
        self.tabs.setTabVisible(self.sv_tab, nb)
        self.tabs.setTabVisible(self.c_tab, nb)
        self.a_seti.setVisible(nb)
        self.a_hline.setText("Start hydrogen line only" if nb else "Start observing")
        if not nb and self.tabs.currentIndex() in (self.sv_tab, self.c_tab):
            self.tabs.setCurrentIndex(self.hl_tab)

        self.sb = self.statusBar()
        self.sb_label = QLabel("")
        self.sb.addPermanentWidget(self.sb_label)
        self.sb.addPermanentWidget(QLabel("   Ayushman Tripathi, www.radioastronomy.in   "))

    def _banner(self, go_live):
        """LIVE / NOT LIVE strip above a plot, with the button that returns to live.
        Returns (label, button, row layout)."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        lb = QLabel("LIVE")
        lb.setStyleSheet(self.LIVE_CSS)
        btn = QPushButton("SEE LIVE")
        btn.setToolTip("Back to the live plot")
        btn.setStyleSheet("QPushButton { background: #2e9e4f; color: white; font-weight: bold; "
                          "padding: 3px 14px; border-radius: 4px; }"
                          "QPushButton:disabled { background: #bbb; }")
        btn.setEnabled(False)
        btn.clicked.connect(go_live)
        row.addWidget(lb, 1)
        row.addWidget(btn)
        return lb, btn, row

    LIVE_CSS = ("QLabel { background: #2e9e4f; color: white; font-weight: bold; "
                "padding: 3px 8px; border-radius: 4px; }")
    NOTLIVE_CSS = ("QLabel { background: #d9822b; color: white; font-weight: bold; "
                   "padding: 3px 8px; border-radius: 4px; }")

    def _set_live(self, label, btn, live, what=""):
        if live:
            label.setText("LIVE" + (f"    {what}" if what else ""))
            label.setStyleSheet(self.LIVE_CSS)
        else:
            label.setText(f"NOT LIVE    {what}    Press SEE LIVE to go back to live")
            label.setStyleSheet(self.NOTLIVE_CSS)
        btn.setEnabled(not live)

    def _scan_table(self, on_view, on_download):
        """A read-only table whose last two columns are View (blue) and Download buttons."""
        view = QTableView()
        model = QStandardItemModel()
        view.setModel(model)
        view.setEditTriggers(QTableView.NoEditTriggers)
        view.setSelectionBehavior(QTableView.SelectRows)
        view.setSelectionMode(QTableView.SingleSelection)
        view.verticalHeader().setVisible(False)
        d_view = ButtonDelegate("#1f6fd0", view)
        d_dl = ButtonDelegate("#5a6b7d", view)
        d_view.clicked.connect(lambda r, c: on_view(model.item(r, 1).text()))
        d_dl.clicked.connect(lambda r, c: on_download(model.item(r, 1).text()))
        view.setProperty("delegates", (d_view, d_dl))      # keep them alive
        return view, model

    def _fill_scan_table(self, view, model, headers, rows):
        """Rows get View and Download cells appended; the key is always column 1."""
        self._fill(model, view, headers + ["", ""], [r + ["View", "Download"] for r in rows])
        d_view, d_dl = view.property("delegates")
        nv, nd = len(headers), len(headers) + 1
        view.setItemDelegateForColumn(nv, d_view)
        view.setItemDelegateForColumn(nd, d_dl)
        hh = view.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(nv, QHeaderView.Fixed)
        hh.setSectionResizeMode(nd, QHeaderView.Fixed)
        view.setColumnWidth(nv, 72)
        view.setColumnWidth(nd, 96)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)

    # ---- data
    def node(self):
        i = max(0, self.node_box.currentIndex())
        return self.nodes[i] if i < len(self.nodes) else self.nodes[0]

    REMOTE_EVERY_S = 30     # a remote node may sit on a metered link: poll it gently

    def refresh(self, force=False):
        n = self.node()
        if force:
            self._force = True
        cur = getattr(self, "_poll", None)
        if cur is not None and cur.isRunning():
            if cur.node is n:
                # a click must not be dropped: rerun as soon as this poll ends
                if force or getattr(self, "_force", False):
                    self._pending = True
                return
            # the user moved to another node: let go of this one's files mid-transfer
            cur.cancel = True
            self._pending = False           # the node change forces a fresh poll anyway
            self.__dict__.setdefault("_old_polls", []).append(cur)
            cur.finished.connect(self._old_poll_done)
        if n.remote:
            last = getattr(self, "_last_remote", {}).get(n.base, 0)
            if time.time() - last < self.REMOTE_EVERY_S and not getattr(self, "_force", False):
                return
            self.__dict__.setdefault("_last_remote", {})[n.base] = time.time()
        self._force = False
        running = self.proc is not None and self.proc.state() != QProcess.NotRunning
        self._poll = Poller(n, getattr(self, "_hl_selected", None), running, self.cfg,
                            want_block=getattr(self, "_sv_selected", None),
                            want_log=self.tabs.currentIndex() == self.log_tab)
        self._poll.done.connect(lambda d, n=n: self.apply(n, d))
        self._poll.part.connect(lambda d, n=n: self.apply(n, d))
        # bound methods: a lambda capturing the thread would keep it alive forever
        self._poll.finished.connect(self._poll_finished)
        self._poll.start()

    def _poll_finished(self):
        p = self.sender()
        if p is getattr(self, "_poll", None) and getattr(self, "_pending", False):
            self._pending = False
            p.wait(200)
            self.refresh(force=True)

    def _old_poll_done(self):
        old = getattr(self, "_old_polls", [])
        p = self.sender()
        if p in old:
            old.remove(p)

    def apply(self, n, d):
        if n is not self.node():
            return
        idx = d["index"]
        if idx is None:
            why = "daily data budget spent" if getattr(n, "over_budget", False) else "unreachable"
            if d.get("error"):
                why = f"could not read the node's files: {d['error']}"
            self.st["state"].setText(why)
            self.sb_label.setText(f"{n.name}    {why} at {n.base}")
            if self.wf_plot.plotItem.titleLabel.text == "Waterfall (loading)":
                self.wf_plot.setTitle(f"Waterfall ({why})")
                self.sp_plot.setTitle(f"Spectrum ({why})")
                for pane in (self.hl_cal, self.hl_raw, self.sv_block_pane, self.c_thumb):
                    if pane.text() == "Loading...":
                        pane.setText(f"Node {why}.")
            return
        self._last_idx = idx
        s = idx.get("status", {})
        cu = idx.get("curate", {})
        raw_state = state = str(s.get("state", "not running"))
        # our own child observes the local node only; a remote node is judged by its status
        running = (not n.remote and self.proc is not None
                   and self.proc.state() != QProcess.NotRunning)
        try:
            age = time.time() - float(s.get("ts") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        # a calibration writes no status while it records (one hydrogen-line scan long)
        allow = max(3 * float(s.get("dwell_s") or 150) + 60,
                    1.5 * float(s.get("cal_s") or 0) + 60 if raw_state == "calibrating" else 0)
        dead = (s and not n.remote and not running and raw_state != "stopped"
                and not lock.holder(self.cfg["state_dir"]))
        if dead:
            # nothing holds the folder: the observer was killed or the power went
            state = f"not running (last status: {raw_state})"
        elif s and age > allow and not running:
            state = f"stale, last seen {int(age // 60)} min ago"
        elif s.get("mode") == "hline":
            state = f"hydrogen line: {state}"
        elif s.get("seti") is False:
            state = f"{state} (hydrogen line only, FFT too small for the narrowband search)"
        if getattr(n, "over_budget", False):
            state += "  (daily data budget spent, panels frozen)"
        self.st["state"].setText(state)
        self.st["sdr"].setText(str(s.get("sdr", "-")))
        # every field is set each time: a node without a key must not show the last node's
        self.st["center"].setText(f"{s['center_hz'] / 1e6:.4f} MHz" if s.get("center_hz") else "-")
        self.st["chan"].setText(f"{s['chan_bw_hz']:.2f} Hz" if s.get("chan_bw_hz") else "-")
        self.st["blocks"].setText(str(s.get("blocks", "-")))
        self.st["hits"].setText(str(s.get("hits_total", "-")))
        if cu:
            a, b, c = cu.get("tierA", 0), cu.get("tierB", 0), cu.get("tierC", 0)
            oc = cu.get("oncomb_review", 0)
            self.st["cands"].setText(f"{a} / {b} / {c}" + (f"   ({oc} on comb)" if oc else ""))
        else:
            self.st["cands"].setText("-")
        self.st["disk"].setText(f"{s['footprint_mb'] / 1000:.2f} GB"
                                if s.get("footprint_mb") is not None else "-")
        up = s.get("uptime_s")
        self.st["up"].setText(f"{up // 3600} h {(up % 3600) // 60} min"
                              if isinstance(up, int) else "-")
        held = (not running) and not n.remote and bool(lock.holder(self.cfg["state_dir"]))
        self.a_stop.setEnabled(running)
        if held:
            state = f"in use by another process (pid {lock.holder(self.cfg['state_dir'])})"
            self.st["state"].setText(state)
        self.a_seti.setEnabled(not running and not n.remote and not held)
        self.a_hline.setEnabled(not running and not n.remote and not held)
        self.a_cal.setEnabled(not running and not n.remote and not held)
        has_cal = bool((idx.get("hline") or {}).get("cal"))
        self.a_delcal.setEnabled(not n.remote and has_cal)
        self.a_delcal.setToolTip("" if not n.remote else
                                 "A remote node's calibration is deleted on its own computer.")
        pending = bool(idx.get("cal_pending")) or raw_state == "calibrating"
        if not n.remote and not (running or lock.holder(self.cfg["state_dir"])):
            pending = False         # left by an observer that died: void at the next start
        if pending:
            self.b_cal.setText("Calibrating..." if raw_state == "calibrating"
                               else "Calibration queued")
            self.b_cal.setEnabled(False)
            self.b_cal.setToolTip("Runs after the current block.")
        elif n.remote:
            can = bool(idx.get("control"))
            self.b_cal.setText(("Recalibrate" if has_cal else "Calibrate now") + " (remote)")
            self.b_cal.setEnabled(can)
            self.b_cal.setToolTip(
                "Ask this node for a reference scan. It runs after the current block; "
                "observing continues by itself." if can else
                "This node has no control token set. On the node: Settings, Control token "
                "(advanced.api_token in its config), then restart it.")
        else:
            self.b_cal.setText("Recalibrate" if has_cal else "Calibrate now")
            self.b_cal.setEnabled(True)       # any aquila observer here picks up the request
            self.b_cal.setToolTip(
                "Record the receiver reference (50 ohm terminator or cold sky). "
                "If observing is running here, it pauses for one scan and resumes.")
        api_s = f"    api {s['api']}" if s.get("api") else ""
        self.sb_label.setText(f"{n.name}    {state}    {'remote' if n.remote else n.base}{api_s}")
        if "npz" not in d:
            return                          # status only: the files are still on their way
        self._refresh_waterfall(d["npz"], s.get("ts"))
        if "hl_raw" in d:
            self._refresh_hline(d)
        if d.get("partial"):
            return                          # the tables and the log come with the full result
        self._refresh_blocks(d)
        self._refresh_cands(d["cands"])
        self.sky.load_background()
        if n.remote:
            view = {"dish": s.get("dish") or {"az": 0, "el": 90},
                    "site": s.get("site") or {}, "center_mhz": (s.get("center_hz") or 1420e6) / 1e6}
        else:
            view = dict(self.cfg)
            if d.get("dish"):
                view["dish"] = d["dish"]
        # a selected H-line scan shows the sky as it was then, from the scan's own header
        t_scan, dish_scan = self._scan_pointing(d)
        if dish_scan:
            view["dish"] = dict(view["dish"], **dish_scan)
        self.sky.update_pointing(view, n.name if n.remote else self.cfg["name"], t_scan)
        # 7: the log tab mirrors survey.log (local file, or the node's tail endpoint while
        # the tab is open); a child started from here appends its own output live instead
        if not running and d["log"]:
            txt = d["log"].decode(errors="replace")[-20000:]
            if n.remote:
                txt = txt.split("\n", 1)[-1] if "\n" in txt else txt   # drop the cut first line
            if txt != self.log.toPlainText():
                self.log.setPlainText(txt)
                self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _refresh_waterfall(self, b, ts):
        if getattr(self, "_wf_ts", None) == ts and ts is not None:
            return
        if not b:
            self._wf_ts = None
            self.wf_img.clear()
            self.sp_curve.setData([], [])
            self.wf_plot.setTitle("Waterfall (no block recorded yet)")
            self.sp_plot.setTitle("Spectrum")
            return
        try:
            z = np.load(io.BytesIO(b))
            wf = z["waterfall"].astype(np.float32)
            sp = z["spectrum"].astype(np.float32)
            cb, c0 = float(z["chan_bw"]), float(z["center_hz"])
            nch, rs = int(z["n_chan"]), float(z["row_s"])
            when = stamp(str(z["utc"]) if "utc" in z.files else "",
                         str(z["local"]) if "local" in z.files else "")
        except Exception:
            return
        self.wf_plot.setTitle(f"Waterfall, block recorded {when}" if when else "Waterfall")
        self.sp_plot.setTitle(f"Spectrum, block recorded {when}" if when else "Spectrum")
        self._wf_ts = ts
        f_lo = (c0 - nch * cb / 2) / 1e6
        f_hi = (c0 + nch * cb / 2) / 1e6
        lg = 10 * wf   # stored as log10
        self.wf_img.setImage(lg.T, autoLevels=False,
                             levels=(np.percentile(lg, 5), np.percentile(lg, 99.5)))
        self.wf_img.setRect(f_lo, 0, f_hi - f_lo, wf.shape[0] * rs)
        xs = f_lo + (np.arange(sp.size) + 0.5) * (f_hi - f_lo) / sp.size
        self.sp_curve.setData(xs, 10 * np.log10(sp + 1e-6))
        if getattr(self, "_wf_span", None) != (f_lo, f_hi):
            self._wf_span = (f_lo, f_hi)      # only a new tuning resets a zoom
            self.wf_plot.setXRange(f_lo, f_hi, padding=0)

    def _refresh_hline(self, d):
        head, rows = d["scans"]
        rows = [r for r in rows if len(r) > 1]       # a torn last line must not stop the GUI
        sel = getattr(self, "_hl_selected", None)
        by_file = {r[1].replace(".dat", ""): r for r in rows if len(r) > 1}
        if sel and "sel_raw" in d:
            cal, raw, row = d["sel_cal"], d["sel_raw"], by_file.get(sel)
        else:
            cal, raw = d["hl_cal"], d["hl_raw"]
            row = rows[-1] if rows else None
        when = ""
        if row:
            when = stamp(row[5] if len(row) > 5 else "", scan_time(row[1]))
        elif sel:
            when = scan_time(sel)
        self.hl_cal.set_data(cal)
        self.hl_raw.set_data(raw)
        suffix = f"   scan {when}" if when else ""
        if sel and "sel_raw" in d:
            self._set_live(self.hl_banner, self.hl_live, False,
                           f"scan {when}{self._hdr_summary(d.get('sel_hdr'))}")
        else:
            self._set_live(self.hl_banner, self.hl_live, True,
                           f"latest scan {when}" if when else "")
        self.gb_cal.setVisible(bool(cal))
        self.gb_raw.setVisible(not cal)
        # the sky map always sits beside the plot; the scan list has the full width below
        if not getattr(self, "_sky_top", False):
            self._sky_top = True
            self.hl_top.addWidget(self.gb_sky, 1)
        if cal:
            self.lb_raw.setText("Calibrated (virgo, receiver reference)" + suffix)
        else:
            self.lb_raw.setText("Uncalibrated (virgo, raw)" + suffix
                                + "   (no receiver calibration yet)")
        if head and rows != getattr(self, "_hl_rows", None):
            self._hl_rows = rows
            self._fill_scan_table(
                self.hl_table, self.hl_model,
                ["Time", "Scan", "Line peak (%)", "Velocity (km/s)", "Calibrated"],
                [(r + [""] * 5)[:4] + ["yes" if (len(r) > 4 and r[4] == "1") else "no"]
                 for r in rows[::-1] if len(r) > 1])

    @staticmethod
    def _hdr_dict(b):
        """key=value lines of a scan's .header as a dict."""
        h = {}
        for line in (b or b"").decode(errors="replace").splitlines():
            k, _, v = line.partition("=")
            h[k.strip()] = v.strip()
        return h

    def _scan_pointing(self, d):
        """(t_unix, {'az','el'}) of the selected H-line scan, each None when unknown or live."""
        sel = getattr(self, "_hl_selected", None)
        if not sel or "sel_raw" not in d:
            return None, None
        t = None
        for r in d["scans"][1]:
            if len(r) > 5 and r[1].replace(".dat", "") == sel:
                m = re.search(r"(\d{4})(\d{2})(\d{2})[T_](\d{2})(\d{2})(\d{2})", r[5])
                if m:
                    t = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
                break
        h = self._hdr_dict(d.get("sel_hdr"))
        if t is None and h.get("mjd"):                      # scans from before the utc column
            with contextlib.suppress(ValueError):
                t = (float(h["mjd"]) - 40587.0) * 86400.0
        dish = None
        aa = (h.get("az_alt") or "").split()
        if len(aa) == 2:
            with contextlib.suppress(ValueError):
                dish = {"az": float(aa[0]), "el": float(aa[1])}
        return t, dish

    @staticmethod
    def _hdr_summary(b):
        """'  az 28.4 el 55.1, gains 14/15/15, 144 s' from a scan's .header, or ''."""
        if not b:
            return ""
        h = Main._hdr_dict(b)
        parts = []
        if h.get("az_alt"):
            aa = h["az_alt"].split()
            if len(aa) == 2:
                parts.append(f"az {aa[0]} el {aa[1]}")
        g = [h.get(k) for k in ("rf_gain", "if_gain", "bb_gain")]
        if all(g):
            parts.append("gains " + "/".join(g))
        if h.get("duration"):
            parts.append(f"{h['duration']} s")
        return ("   " + ", ".join(parts)) if parts else ""

    def _refresh_blocks(self, d):
        head, rows = d.get("blocks") or ([], [])
        sel = getattr(self, "_sv_selected", None)
        if sel:
            self.sv_stack.setCurrentIndex(1)
            png = d.get("sel_block")
            if png:
                self.sv_block_pane.set_data(png)
            by_utc = {r[0]: r for r in rows if r}
            r = by_utc.get(sel)
            if not png and "sel_block" in d:
                self.sv_block_pane.set_data(None)
                if r and len(r) > 2 and r[2] == "":
                    self.sv_block_pane.setText("Plots are switched off on this node, so this "
                                               "block has no picture. Its hits can still be "
                                               "downloaded.")
                else:
                    self.sv_block_pane.setText("That block's picture is no longer on the node "
                                               "(old thumbnails are rotated out).")
            when = stamp(sel, r[1] if r and len(r) > 1 else "")
            extra = ""
            if r and len(r) > 5 and r[3] != "":
                extra = f"   {r[3]} candidate(s), {r[4]} carrier(s), top SNR {r[5]}"
            self._set_live(self.sv_banner, self.sv_live, False, f"block {when}{extra}")
        else:
            self.sv_stack.setCurrentIndex(0)
            self._set_live(self.sv_banner, self.sv_live, True)
        if head and rows != getattr(self, "_sv_rows", None):
            self._sv_rows = rows
            out = []
            for r in rows[::-1]:
                if not r:
                    continue
                r = r + [""] * (7 - len(r))
                out.append([r[1] or stamp(r[0]), r[0], r[3], r[4], r[5]])
            self._fill_scan_table(self.sv_table, self.sv_model,
                                  ["Time", "Block (UTC)", "Candidates", "Carriers", "Top SNR"], out)

    def _refresh_cands(self, cands):
        head, rows = cands
        if head and rows != getattr(self, "_c_rows", None):
            self._c_rows = rows
            self._fill(self.c_model, self.c_table,
                       ["Tier", "UTC", "Local time", "Frequency (Hz)", "Drift (Hz/s)", "SNR",
                        "Width (Hz)", "Rows lit", "Image"],
                       rows)

    def _fill(self, model, view, headers, rows):
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        model.setSortRole(Qt.UserRole)
        for r in rows:
            items = []
            for v in r:
                it = QStandardItem(v)
                try:
                    it.setData(float(v), Qt.UserRole)
                except ValueError:
                    it.setData(v, Qt.UserRole)
                items.append(it)
            model.appendRow(items)
        view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        view.horizontalHeader().setStretchLastSection(True)

    def show_cand(self):
        idx = self.c_table.selectionModel().selectedRows()
        if not idx:
            return
        r = idx[0].row()
        thumb = self.c_model.item(r, 8).text()
        when = stamp(self.c_model.item(r, 1).text(), self.c_model.item(r, 2).text())
        self.c_when.setText(f"Block recorded {when}" if when else "")
        self._c_sel = thumb
        if thumb:
            n = self.node()
            self.c_thumb.set_data(None)
            self.c_thumb.setText("Loading...")
            self._c_load = self._call(lambda: n.get(f"waterfalls/{thumb}"),
                                      lambda b, t=thumb, n=n: self._c_sel == t and n is self.node()
                                      and self.c_thumb.set_data(
                                          None if isinstance(b, Exception) else b))

    def view_scan(self, key):
        """View one hydrogen-line scan (key = 'obs_..._...' or None for live)."""
        self._hl_selected = key.replace(".dat", "") if key else None
        self.refresh(force=True)

    def view_block(self, key):
        """View one SETI block by its UTC stamp, or None for live."""
        self._sv_selected = key or None
        if key:
            self.sv_block_pane.set_data(None)
            self.sv_block_pane.setText("Loading block...")
        self.refresh(force=True)

    def download_scan(self, key):
        self._download("hline", key.replace(".dat", ""), f"scan {scan_time(key)}")

    def download_block(self, key):
        self._download("block", key, f"block {stamp(key)}")

    def _download(self, kind, key, what):
        if getattr(self, "_dl", None) is not None and self._dl.isRunning():
            QMessageBox.information(self, "Download", "A download is still running.")
            return
        dest = QFileDialog.getExistingDirectory(
            self, f"Save {what} to folder",
            self.cfg.get("last_download_dir") or os.path.expanduser("~"))
        if not dest:
            return
        self.cfg["last_download_dir"] = dest
        with contextlib.suppress(Exception):
            config.save(self.cfg, self.cfg_path)
        self._dl = Downloader(self.node(), kind, key, dest,
                              filters=bool((getattr(self, "_last_idx", None) or {}).get("tail")))
        self._dl.done.connect(lambda msg: QMessageBox.information(self, "Download", msg))
        self._dl.start()
        self.sb.showMessage(f"Downloading {what}...", 5000)

    # ---- actions
    def start(self, verb):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._proc_out)
        self.proc.finished.connect(self._proc_finished)
        self.proc.errorOccurred.connect(self._proc_error)
        self._child_out = []
        self._proc_dir = self.cfg["state_dir"]     # a later Settings change must not lose it
        self._verb = verb
        # a frozen app (PyInstaller) is its own interpreter: the executable takes the verb
        args = [] if getattr(sys, "frozen", False) else ["-m", "aquila.cli"]
        if self.cfg_path:
            args += ["-c", self.cfg_path]
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.cfg["state_dir"], "stop"))
        self.proc.start(sys.executable, args + [verb])
        self.refresh()

    def _proc_out(self):
        txt = bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip()
        if txt:
            self.log.appendPlainText(txt)
            self._child_out = (getattr(self, "_child_out", []) + txt.splitlines())[-40:]

    def _proc_finished(self, code, status):
        if code != 0 and status == QProcess.NormalExit and not getattr(self, "_closing", False):
            # startup errors go to the child's output, which the log tab replaces with
            # survey.log on the next poll: show them where they stay
            tail = "\n".join(getattr(self, "_child_out", [])[-12:]) or f"exit code {code}"
            self.sb.showMessage(f"Observer stopped with an error (exit {code})", 15000)
            QMessageBox.warning(self, "Aquila Radio Astronomy", f"The observer stopped:\n\n{tail}")
        self.refresh()

    def _proc_error(self, err):
        if err == QProcess.FailedToStart:
            QMessageBox.warning(self, "Aquila Radio Astronomy",
                                f"Could not start the observer: {self.proc.errorString()}")

    def _ask_stop(self):
        """The observer acts on the stop file within a second, on every OS; SIGTERM too."""
        with contextlib.suppress(OSError):
            d = getattr(self, "_proc_dir", None) or self.cfg["state_dir"]
            open(os.path.join(d, "stop"), "w").close()
        if sys.platform != "win32":
            self.proc.terminate()

    def stop(self, refresh=True):
        p = self.proc
        if p is not None and p.state() != QProcess.NotRunning:
            self._ask_stop()
            self.sb.showMessage("Stopping...", 5000)

            def force():                       # not gone in 30 s: end it
                if p.state() != QProcess.NotRunning:
                    p.kill()
            QTimer.singleShot(30000, force)
        if refresh:
            self.refresh()

    def closeEvent(self, e):
        p = self.proc
        running = p is not None and p.state() != QProcess.NotRunning
        if running and e.spontaneous() and not getattr(self, "_closing", False):
            # a node is often left running for weeks: one stray click must not end it
            r = QMessageBox.question(self, "Aquila Radio Astronomy",
                                     "Closing the app stops observing. Close anyway?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self._closing = True
        self.timer.stop()
        if running:
            self._ask_stop()
            if not p.waitForFinished(30000):
                p.kill()
                p.waitForFinished(5000)
        for t in getattr(self, "_old_polls", []):
            t.cancel = True
        if getattr(self, "_poll", None) is not None:
            self._poll.cancel = True
        for t in ([getattr(self, "_poll", None), getattr(self, "_dl", None)]
                  + list(getattr(self, "_old_polls", [])) + list(getattr(self, "_calls", []))):
            if t is not None and t.isRunning():
                t.wait(15000)
        e.accept()

    CAL_TEXT = ("Take one reference scan with either:\n\n"
                "  a 50 ohm terminator on the LNA input in place of the antenna, or\n"
                "  the dish on a cold patch of sky well away from the galactic plane.\n\n"
                "The reference must not contain hydrogen, or it cancels the line you want "
                "to see.")

    def take_cal(self):
        n = self.node()
        if n.remote:
            return self._take_cal_remote(n)
        running = self.proc is not None and self.proc.state() != QProcess.NotRunning
        verb = getattr(self, "_verb", None)
        if running and verb == "cal":
            return
        other = None if running else lock.holder(self.cfg["state_dir"])
        if running or other:
            # an observer is running here (ours or another process): it takes the reference
            # itself between blocks, so nothing is stopped or restarted
            r = QMessageBox.question(
                self, "Calibration", self.CAL_TEXT + "\n\nObserving is running: the reference "
                "is taken after the current block and observing carries on. Queue it now?")
            if r != QMessageBox.Yes:
                return
            try:
                d = (getattr(self, "_proc_dir", None) if running else None) or self.cfg["state_dir"]
                p = os.path.join(d, "cal_request")
                with open(p + ".tmp", "w") as f:
                    f.write(str(int(time.time())))
                os.replace(p + ".tmp", p)
            except OSError as e:
                QMessageBox.warning(self, "Calibration", f"Could not queue it: {e}")
                return
            self.sb.showMessage("Calibration queued; it runs after the current block.", 8000)
            self.refresh(force=True)
            return
        r = QMessageBox.question(self, "Calibration", self.CAL_TEXT + "\n\nReady to record it now?")
        if r != QMessageBox.Yes:
            return
        self.start("cal")

    def _take_cal_remote(self, n):
        if not n.token:
            tok, ok = QInputDialog.getText(self, "Control token",
                                           f"Control token for {n.name}:", QLineEdit.Password)
            if not ok or not tok.strip():
                return
            n.token = tok.strip()
            self._remember(n)
        r = QMessageBox.question(
            self, "Calibration", self.CAL_TEXT + f"\n\nThe node {n.name} takes the reference "
            "after its current block and keeps observing. Make sure the dish is on cold sky, "
            "or someone at the dish has the terminator in place. Queue it now?")
        if r != QMessageBox.Yes:
            return
        self.sb.showMessage(f"Asking {n.name} for a calibration...")
        self._cal_call = self._call(n.request_cal, lambda r, n=n: self._cal_answer(n, r))

    def _cal_answer(self, n, r):
        ok, msg = (False, f"node unreachable: {r}") if isinstance(r, Exception) else r
        if ok:
            self.sb.showMessage(f"Calibration queued on {n.name}; it runs after the current "
                                "block.", 8000)
        else:
            self.sb.clearMessage()
            QMessageBox.warning(self, "Calibration", f"Not queued: {msg}.")
            if "token" in msg:
                n.token = ""
                self._remember(n)
        self.refresh(force=True)

    def _call(self, fn, on_done):
        """Run fn off the GUI thread, then on_done(result or exception) on it."""
        t = Call(fn)
        t.done.connect(on_done)
        self.__dict__.setdefault("_calls", []).append(t)
        t.finished.connect(self._call_done)
        t.start()
        return t

    def _call_done(self):
        calls = getattr(self, "_calls", [])
        t = self.sender()
        if t in calls:
            calls.remove(t)

    CAL_FILES = ("cal.dat", "cal.header", "cal_fine.dat", "cal_fine.header")

    def delete_cal(self):
        """Remove this computer's receiver reference; plots go back to raw from the next
        scan. The previous reference kept as cal.prev.* is left alone."""
        n = self.node()
        if n.remote:
            return
        d = os.path.join(self.cfg["state_dir"], "hline")
        have = [f for f in self.CAL_FILES if os.path.exists(os.path.join(d, f))]
        if not have:
            QMessageBox.information(self, "Calibration", "This node has no calibration.")
            return
        r = QMessageBox.question(
            self, "Calibration", "Delete this node's receiver calibration? Scans show the "
            "raw plot only until a new one is taken.")
        if r != QMessageBox.Yes:
            return
        for f in have:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, f))
        self.sb.showMessage("Receiver calibration deleted.", 8000)
        self.refresh(force=True)

    def _fill_recent(self):
        self.recent_menu.clear()
        remote = [n for n in self.nodes if n.remote]
        if not remote:
            a = self.recent_menu.addAction("(none yet)")
            a.setEnabled(False)
            return
        for n in remote:
            a = self.recent_menu.addAction(f"{n.name}    {n.base}")
            a.triggered.connect(lambda _=False, n=n: self._select_node(n))

    def _select_node(self, n):
        self.node_box.setCurrentIndex(self.nodes.index(n))

    def connect_node(self):
        remembered = self.cfg.get("remote_nodes") or []
        start = remembered[-1]["url"] if remembered else "http://"
        url, ok = QInputDialog.getText(self, "Connect to node",
                                       "Node address (http://host:8090):", text=start)
        if ok and url.strip():
            base = url.strip().rstrip("/")
            for k, existing in enumerate(self.nodes):
                if existing.remote and existing.base == base:
                    self.node_box.setCurrentIndex(k)
                    return
            n = Node(base, base)
            self.sb.showMessage(f"Connecting to {base}...")
            self._connect_call = self._call(n.index, lambda idx, n=n, url=url:
                                            self._connect_answer(n, url, idx))

    def _connect_answer(self, n, url, idx):
        self.sb.clearMessage()
        if not idx or isinstance(idx, Exception):
            QMessageBox.warning(self, "Connect to node", "No node answered at that address.")
            return
        n.name = idx.get("status", {}).get("name") or url
        if idx.get("control"):
            tok, ok2 = QInputDialog.getText(
                self, "Control token",
                f"{n.name} accepts calibration requests from the app.\n"
                "Enter its control token (the api_token in the node's config),\n"
                "or leave blank to view only:", QLineEdit.Password)
            if ok2:
                n.token = tok.strip()
        self.nodes.append(n)
        self._fill_nodes()
        self.node_box.setCurrentIndex(len(self.nodes) - 1)
        self._remember(n)

    def _remember(self, n):
        cfg = config.load(self.cfg_path)
        saved = [r for r in cfg.get("remote_nodes") or [] if r.get("url") != n.base]
        cfg["remote_nodes"] = saved + [{"name": n.name, "url": n.base, "token": n.token}]
        config.save(cfg, self.cfg_path)
        self.cfg["remote_nodes"] = cfg["remote_nodes"]

    def set_token(self):
        n = self.node()
        if not n.remote:
            QMessageBox.information(self, "Control token", "This computer's own node needs no "
                                    "token. Set one in Settings so other computers can ask it "
                                    "for a calibration.")
            return
        tok, ok = QInputDialog.getText(self, "Control token",
                                       f"Control token for {n.name} (blank = view only):",
                                       QLineEdit.Password, n.token)
        if ok:
            n.token = tok.strip()
            self._remember(n)
            self.refresh(force=True)

    def forget_node(self):
        n = self.node()
        if not n.remote:
            return
        cfg = config.load(self.cfg_path)
        cfg["remote_nodes"] = [r for r in cfg.get("remote_nodes") or [] if r.get("url") != n.base]
        config.save(cfg, self.cfg_path)
        self.cfg["remote_nodes"] = cfg["remote_nodes"]
        self.nodes.remove(n)
        self._fill_nodes()
        self.node_box.setCurrentIndex(0)

    def settings(self):
        self.cfg = config.load(self.cfg_path)
        dlg = Settings(self.cfg, self, self._local_sdr())
        try:
            self._settings_dialog(dlg)
        finally:
            # one dialog per open would pile up otherwise; its SDR check must end first
            probe = getattr(dlg, "_probe", None)
            if probe is not None and probe.isRunning():
                probe.done.connect(lambda *_: dlg.deleteLater())
            else:
                dlg.deleteLater()

    def _local_sdr(self):
        """Name of the SDR this computer's node is observing with now, or None."""
        try:
            st = (self.nodes[0].index() or {}).get("status") or {}
            running = self.proc is not None and self.proc.state() != QProcess.NotRunning
            allow = 3 * float(st.get("dwell_s") or 150) + 60
            fresh = time.time() - float(st.get("ts") or 0) < allow
            if st.get("sdr") and st.get("state") != "stopped" and (running or
                    (fresh and lock.holder(self.cfg["state_dir"]))):
                return st["sdr"]
        except Exception:
            pass
        return None

    def _settings_dialog(self, dlg):
        if dlg.exec() == QDialog.Accepted:
            self.cfg = dlg.apply()
            want = self.cfg.pop("_autostart", None)
            if want is not None and want != autostart.enabled():
                try:
                    autostart.set_enabled(want, self.cfg.get("_path") or self.cfg_path)
                except OSError as ex:
                    QMessageBox.warning(self, "Settings",
                                        f"Could not change the login setting: {ex}")
            self._save_cfg(self.cfg)

    def open_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.cfg["state_dir"]))

    def about(self):
        box = QMessageBox(self)
        box.setWindowTitle("Aquila Radio Astronomy")
        box.setIconPixmap(QPixmap(ICON).scaled(96, 96, Qt.KeepAspectRatio,
                                               Qt.SmoothTransformation))
        box.setText(
            f"Aquila Radio Astronomy {__version__}\n\nNarrowband search and hydrogen line "
            "observing with a dish and an SDR.\n\nAyushman Tripathi\n"
            "www.radioastronomy.in\n\n"
            "MIT licence: free for anyone to use and modify.\n\n"
            "Hydrogen line plots by Virgo (astro-virgo, GPL-3), "
            "github.com/0xCoto/Virgo.")
        box.exec()


def main(cfg_path=None):
    app = QApplication(sys.argv)
    app.setApplicationName("Aquila Radio Astronomy")
    app.setDesktopFileName("aquila-radio-astronomy")
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(ICON))
    try:
        w = Main(cfg_path)
    except RuntimeError as e:
        QMessageBox.critical(None, "Aquila Radio Astronomy", str(e))
        sys.exit(1)
    # logging out or shutting down closes the window too: never stop that with a question
    app.commitDataRequest.connect(lambda *_: setattr(w, "_closing", True))
    w.show()
    rc = app.exec()
    # a worker thread still waiting on a dead node must not abort the exit
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    main()

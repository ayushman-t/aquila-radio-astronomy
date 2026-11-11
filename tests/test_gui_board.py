"""The Hydrogen line tab's layout: its boxes fill the tab at any window size."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QGroupBox, QVBoxLayout, QWidget  # noqa: E402

from aquila import gui  # noqa: E402


def test_scan_times_from_a_scans_row():
    row = ["2026-11-10 21:16:35", "obs_20261110_211134.dat", "9.7", "8.0", "1",
           "20261110T154635Z", "0.404", "0.0100"]
    assert gui.scan_unix(row) == 1794325595
    assert gui.scan_unix(row[:5]) is None and gui.scan_unix([]) is None
    assert gui.node_tz([row]) == 19800                  # the node's clock, UTC + 5:30
    assert gui.node_tz([row[:5]]) is None and gui.node_tz([]) is None


def test_a_scan_has_one_time_the_one_in_the_list():
    row = ["2026-11-11 07:11:53", "obs_20261111_070640.dat", "6.45", "-17.6", "1",
           "20261111T014153Z", "0.271", "0.0313"]
    assert gui.scan_when(row) == "2026-11-11 07:11:53"
    # the banner's two clocks are the same moment
    assert (gui.stamp(row[5], gui.scan_when(row))
            == "2026-11-11 07:11:53 node time (01:41:53 UTC)")
    # a row with no time of its own falls back to the file's
    assert gui.scan_when(["", "obs_20261111_070640.dat"]) == "2026-11-11 07:06:40"
    assert gui.scan_when(None) == ""


class _Sky(QWidget):
    def height_for(self, w):
        return w // 2 + 44 + 60


class _Raw(QWidget):
    def aspect(self, default):
        return default


@pytest.mark.parametrize("size", [(960, 480), (996, 563), (1196, 683), (1853, 873),
                                  (2556, 1243)])
@pytest.mark.parametrize("missing", [None, "line", "raw"])
def test_board_fills_the_tab(size, missing):
    app = QApplication.instance() or QApplication([])
    sky, raw = _Sky(), _Raw()
    boxes = {}
    for name, inner in (("line", QWidget()), ("sky", sky), ("day", QWidget()),
                        ("raw", raw), ("scans", QWidget())):
        boxes[name] = QGroupBox(name)
        QVBoxLayout(boxes[name]).addWidget(inner)
    b = gui.LineBoard(boxes["line"], boxes["sky"], boxes["day"], boxes["raw"],
                      boxes["scans"], sky, raw, lambda w: 440 if w < 1300 else 650)
    if missing:
        boxes[missing].setVisible(False)
    b.resize(*size)
    b.show()
    app.processEvents()
    b.place()
    W, H = size
    seen = [x.geometry() for x in boxes.values() if not x.isHidden()]
    assert len(seen) == (4 if missing else 5)
    for g in seen:
        assert g.left() >= 0 and g.top() >= 0 and g.right() < W and g.bottom() < H
        assert g.width() >= 150 and g.height() >= 150
    for i, g in enumerate(seen):
        assert not any(g.intersects(o) for o in seen[i + 1:])
    # the boxes and the gaps between them are the whole tab: nothing is left empty
    area = sum(g.width() * g.height() for g in seen)
    gaps = b.GAP * (W + 2 * H)                          # one between the rows, two in a row
    assert W * H - gaps <= area <= W * H
    top = boxes["raw" if missing == "line" else "line"].geometry()
    assert top.left() == 0 and top.top() == 0           # the one plot there has the large box
    b.close()


def test_sky_text_wraps_and_hours_thin_out_in_a_small_box():
    app = QApplication.instance() or QApplication([])
    sky = gui.SkyMap()
    sky.credit.setText(gui.skymap.CREDIT)
    sky.update_pointing({"site": {"lat": 24.67, "lon": 81.87, "alt": 300},
                         "dish": {"az": 255.0, "el": 73.0, "diameter_m": 2.4},
                         "center_mhz": 1420.4}, "node", 1_794_325_595.0)
    for w in (290, 456, 850):
        sky.resize(w, sky.height_for(w))
        sky.show()
        app.processEvents()
        for lb in (sky.info, sky.credit):
            assert lb.width() <= w and lb.heightForWidth(lb.width()) <= lb.height(), (w, lb.text())
            assert lb.geometry().bottom() < sky.height()
        assert sky._hours == (6 if w < 400 else 3)


def test_status_line_shortens_instead_of_being_cut():
    app = QApplication.instance() or QApplication([])
    lb = gui.ElideLabel()
    lb.setText("node    observing    /a/long/folder/name/that/does/not/fit/in/the/bar")
    assert lb.minimumSizeHint().width() < lb.sizeHint().width()
    lb.resize(120, lb.sizeHint().height())
    lb.show()
    app.processEvents()
    assert not lb.grab().isNull()                        # paints at a width below its text


def test_sky_name_stays_on_the_map():
    app = QApplication.instance() or QApplication([])
    sky = gui.SkyMap()
    cfg = {"site": {"lat": 24.67, "lon": 81.87, "alt": 300},
           "dish": {"az": 255.0, "el": 73.0, "diameter_m": 2.4}, "center_mhz": 1420.4}
    sky.resize(300, sky.height_for(300))
    sky.show()
    for t in (1_794_325_595.0, 1_794_325_595.0 + 6 * 3600, 1_794_325_595.0 + 12 * 3600):
        sky.update_pointing(cfg, "A node with quite a long name for a small map", t)
        app.processEvents()
        box = sky.plot.getViewBox().sceneBoundingRect()
        name = sky.label.sceneBoundingRect()
        assert box.left() <= name.left() and name.right() <= box.right() + 1, t

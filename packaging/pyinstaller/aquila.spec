# PyInstaller spec for the Aquila Radio Astronomy desktop app. One folder build (fast start,
# easy to inspect), used for the Windows MSIX, the macOS .app/.dmg and the plain
# Linux tarball. Run from the repository root:
#   pyinstaller packaging/pyinstaller/aquila.spec
# A `tools` folder in the repository root (vendor SDR binaries) is bundled if present.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), "..", ".."))
sys.path.insert(0, ROOT)
from aquila import __version__  # noqa: E402

NAME = "Aquila Radio Astronomy"
ICON = {"win32": os.path.join(ROOT, "build", "icons", "aquila.ico"),
        "darwin": os.path.join(ROOT, "build", "icons", "aquila.icns")}.get(sys.platform)
if ICON and not os.path.exists(ICON):
    ICON = None

datas = [(os.path.join(ROOT, "aquila", "assets"), os.path.join("aquila", "assets"))]
datas += collect_data_files("pyqtgraph", includes=["**/*.ui", "**/*.png", "**/*.svg"])
datas += collect_data_files("matplotlib", subdir="mpl-data")
datas += collect_data_files("astropy", includes=["**/*.cfg", "**/*.dat", "**/*.fits",
                                                  "**/*.json", "**/*.csv"])
tools = os.path.join(ROOT, "tools")
if os.path.isdir(tools):
    datas.append((tools, "tools"))

hidden = ["aquila", "virgo", "scipy._lib.array_api_compat.numpy.fft",
          "matplotlib.backends.backend_agg", "PySide6.QtSvg"]
hidden += collect_submodules("astropy.io") + collect_submodules("astropy.units")

a = Analysis(
    [os.path.join(ROOT, "packaging", "pyinstaller", "entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
              "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
              "PySide6.QtRemoteObjects", "PySide6.QtSensors", "PySide6.QtSerialPort",
              "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtLocation",
              "IPython", "pytest"],
    noarchive=False,
)
# Qt modules the app never loads but the PySide6 hooks drag in (about 40 MB)
UNUSED_QT = ("Quick", "Qml", "Pdf", "WebEngine", "WebChannel", "WebView", "Designer",
             "Multimedia", "3D", "Charts", "DataVisualization", "Bluetooth", "Nfc",
             "Positioning", "Sensors", "SerialPort", "TextToSpeech", "WebSockets",
             "Location", "RemoteObjects", "Scxml", "StateMachine", "Help", "Sql", "Test",
             "VirtualKeyboard", "SpatialAudio", "Graphs", "HttpServer", "Labs", "Nfc",
             "UiTools", "ShaderTools")


def _unused(name):
    base = os.path.basename(name)
    return base.startswith(("libQt6", "Qt6")) and any(f"Qt6{m}" in base for m in UNUSED_QT)


a.binaries = [b for b in a.binaries if not _unused(b[0])]
a.datas = [d for d in a.datas if not _unused(d[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="aquila-radio-astronomy" if sys.platform != "darwin" else NAME,
    debug=False, strip=False, upx=False,
    console=False,                      # the GUI; `aquila` CLI users install with pip
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="aquila-radio-astronomy")
if sys.platform == "darwin":
    app = BUNDLE(
        coll, name=f"{NAME}.app", icon=ICON, bundle_identifier="in.radioastronomy.aquila",
        version=__version__,
        info_plist={"CFBundleShortVersionString": __version__,
                    "NSHighResolutionCapable": True,
                    "LSMinimumSystemVersion": "12.0"},
    )

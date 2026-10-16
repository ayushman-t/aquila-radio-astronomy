#!/bin/sh
# macOS build: PyInstaller .app with the SDR tools inside -> plain .dmg (unsigned,
# not notarized). Testers open it with right-click > Open the first time, or:
#   xattr -dr com.apple.quarantine "/Applications/Aquila Radio Astronomy.app"
# Run from the repository root with the app installed in the current Python:
#   pip install ".[gui,hline]" pyinstaller pillow && sh packaging/macos/build.sh
# The vendor tools (airspy_rx, rtl_sdr, hackrf_transfer and their dylibs) are taken
# from Homebrew at build time and relinked into a self-contained tools/ folder, so the
# user installs nothing else. aquila.sdr.which looks in tools/ next to the app first.
set -e
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"", open("aquila/__init__.py").read()).group(1))')

# 1. vendor tools, made relocatable (dylibbundler rewrites the install names)
brew list airspy >/dev/null 2>&1 || brew install airspy
brew list librtlsdr >/dev/null 2>&1 || brew install librtlsdr
brew list hackrf >/dev/null 2>&1 || brew install hackrf
brew list dylibbundler >/dev/null 2>&1 || brew install dylibbundler
rm -rf tools && mkdir -p tools/libs
for t in airspy_rx airspy_info rtl_sdr rtl_test rtl_biast hackrf_transfer hackrf_info; do
    src=$(command -v "$t" || true)
    [ -n "$src" ] && cp "$src" tools/
done
for t in tools/airspy_rx tools/airspy_info tools/rtl_sdr tools/rtl_test tools/rtl_biast \
         tools/hackrf_transfer tools/hackrf_info; do
    [ -f "$t" ] && dylibbundler -od -b -x "$t" -d tools/libs -p @executable_path/libs >/dev/null
done
ls tools

# 2. icons, the frozen app, the dmg
python3 packaging/make_icons.py
pyinstaller --noconfirm --clean packaging/pyinstaller/aquila.spec
rm -rf build/dmg && mkdir -p build/dmg
cp -R "dist/Aquila Radio Astronomy.app" build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -volname "Aquila Radio Astronomy" -srcfolder build/dmg -ov -format UDZO \
    "dist/aquila-radio-astronomy-$VERSION-macos-$(uname -m).dmg"
echo "built dist/aquila-radio-astronomy-$VERSION-macos-$(uname -m).dmg"

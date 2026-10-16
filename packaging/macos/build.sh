#!/bin/sh
# macOS build: PyInstaller .app with the SDR tools inside -> plain .dmg (unsigned,
# not notarized). Testers open it with right-click > Open the first time, or:
#   xattr -dr com.apple.quarantine "/Applications/Aquila Radio Astronomy.app"
# Run from the repository root with the app installed in the current Python:
#   pip install ".[gui,hline]" pyinstaller pillow && sh packaging/macos/build.sh
# The vendor tools (airspy_rx, rtl_sdr, hackrf_transfer and their dylibs) are taken
# from Homebrew at build time and relinked into a self-contained tools/ folder, so the
# user installs nothing else. aquila.sdr.which looks in tools/ next to the app first.
set -ex
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
# dylibbundler asks on the terminal for any library it cannot find, which would hang a
# CI job for ever, so it gets the Homebrew lib folder and no stdin
BREW=$(brew --prefix)
for t in tools/airspy_rx tools/airspy_info tools/rtl_sdr tools/rtl_test tools/rtl_biast \
         tools/hackrf_transfer tools/hackrf_info; do
    [ -f "$t" ] && dylibbundler -od -b -x "$t" -d tools/libs -p @executable_path/libs \
        -s "$BREW/lib" -s "$BREW/opt/libusb/lib" </dev/null
done
ls tools

# 2. icons, the frozen app, the dmg
python3 packaging/make_icons.py
pyinstaller --noconfirm --clean packaging/pyinstaller/aquila.spec
rm -rf build/dmg && mkdir -p build/dmg
cp -R "dist/Aquila Radio Astronomy.app" build/dmg/
ln -s /Applications build/dmg/Applications
# hdiutil on a busy runner sometimes fails with "resource busy": try a few times
DMG="dist/aquila-radio-astronomy-$VERSION-macos-$(uname -m).dmg"
for i in 1 2 3 4 5; do
    hdiutil create -volname "Aquila Radio Astronomy" -srcfolder build/dmg -ov -format UDZO \
        "$DMG" && break
    echo "hdiutil failed, attempt $i"; sleep 10
done
test -f "$DMG"
echo "built dist/aquila-radio-astronomy-$VERSION-macos-$(uname -m).dmg"

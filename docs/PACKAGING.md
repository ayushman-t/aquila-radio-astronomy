# Packaging and release

What ships, how it is built, and the one-time accounts each channel needs.

## Channels

| Channel | Package | Built by | Install for the user |
|---|---|---|---|
| Ubuntu / Debian | `.deb` + apt repo on radioastronomy.in | `release.yml` job `deb`, repo published by `packaging/apt/publish.sh` | one `apt install aquila-radio-astronomy` after adding the repo line once |
| Snap Store | `aquila-radio-astronomy` snap, strict, raw-usb | `release.yml` job `snap` (snapcraft) | `snap install aquila-radio-astronomy` then `snap connect aquila-radio-astronomy:raw-usb` |
| Microsoft Store | MSIX (full trust, x64) | `release.yml` job `windows` (`packaging/windows/build.ps1`) | Store page, updates automatic |
| Windows portable | zip of the PyInstaller folder | same job | unzip, run `aquila-radio-astronomy.exe` |
| macOS | plain `.dmg`, unsigned | `release.yml` job `macos` (`packaging/macos/build.sh`) | right-click > Open the first time |
| Linux portable | tar.gz of the PyInstaller folder | job `linux-portable` | untar, run `aquila-radio-astronomy` |
| Developers | `pip install ".[gui,hline]"` | | |

Every channel is produced from one git tag: `git tag v0.2.0 && git push --tags`
runs `release.yml`, which attaches everything to a GitHub release. The Store
uploads are manual (MSIX to Partner Center) or automatic (snap, once the store
credential secret is set).

## One-time setup

1. Public GitHub repository for the app (the release workflow needs it; the
   current working copy has no remote).
2. **Snap Store**: account at snapcraft.io, then `snapcraft register aquila-radio-astronomy`.
   `snapcraft export-login creds.txt` and put the file's content in the repo
   secret `SNAPCRAFT_STORE_CREDENTIALS`. The workflow then publishes every tag to
   the `edge` channel; promote to `stable` from the web dashboard.
3. **Microsoft Store**: Partner Center developer account (one-time fee, individual
   tier). Reserve the name "Aquila Radio Astronomy", copy the Identity Name and Publisher it
   assigns into `packaging/windows/AppxManifest.xml`. Upload the MSIX from the
   release. The Store signs it. Reviewers only check that it installs and opens;
   the app opens fine with no SDR attached (Quick setup, synthetic source).
4. **apt repo**: a GPG signing key kept off CI (`gpg --quick-gen-key
   "Aquila Radio Astronomy apt <apt@radioastronomy.in>"`), the public key exported to
   `https://radioastronomy.in/apt/aquila.gpg`. `packaging/apt/publish.sh` takes a
   `.deb` and regenerates the `dists/` and `pool/` tree with `reprepro`, then the
   tree is uploaded to the web host under `/apt`. Users add one line:
   `deb [signed-by=/usr/share/keyrings/aquila.gpg] https://radioastronomy.in/apt stable main`.
5. **macOS signing and notarization**: skipped for now (Apple developer fee).
   Testers get the plain dmg and the right-click > Open step. Add later without
   changing anything else: sign the `.app` in `build.sh` and notarize the dmg.

## What each package contains

Every channel is one install with nothing else to fetch:

- deb: `apt install aquila-radio-astronomy` pulls Qt, numpy, scipy, matplotlib, astropy, the
  SDR tools (Ubuntu's airspy, rtl-sdr, hackrf) and python3-astro-virgo as hard
  dependencies. Debian does not ship Virgo, so `packaging/apt/virgo-deb.sh` builds it
  from PyPI and `publish.sh` puts it in the same repo; apt resolves it by itself.
- snap: Qt and the Python stack inside, Ubuntu's airspy/rtl-sdr/hackrf staged in.
- MSIX and Windows zip: everything inside, plus the vendors' own x64 tools
  (`airspy_rx` also drives the HydraSDR, which has no Windows build).
- dmg: everything inside, plus the Homebrew tools relinked into `tools/` at build
  time (dylibbundler), so the user installs nothing else.
- Linux tar.gz: everything but the SDR tools (distro packages, `apt install airspy
  rtl-sdr hackrf`); the deb and the snap are the all-in-one Linux routes.

The frozen builds look for SDR tools in a `tools` folder next to the executable
first (`aquila.sdr.which`), then PATH and the Homebrew paths.

The one thing no package can install is the Windows USB driver: Airspy, HydraSDR,
HackRF and RTL-SDR need WinUSB via Zadig once per device. The Check button in
Settings says so when the driver is missing.

## Licence note

The app is MIT. The hydrogen-line plots are drawn by Virgo (astro-virgo), which
is **GPL-3**. Every package includes it (the deb as a hard dependency on
python3-astro-virgo from our repo, the others inside the bundle), so each
package is a combined work distributed under GPL-3 terms, which MIT allows. The
app's own source stays MIT; the About box credits Virgo and links its source.

## Build Python

The frozen builds use Python 3.12 in CI. Python 3.13.3 crashes inside
PyInstaller's import analysis for this app ("Fatal Python error:
_PyEval_EvalFrameDefault: Executing a cache"), repeatably; 3.10 to 3.12 work.

## Versioning

`aquila/__init__.py` holds the version; `pyproject.toml`, `debian/changelog` and
the MSIX manifest must agree with it (the Windows and snap builds read it from
`__init__.py`). Bump all three, commit, tag.

## Drivers

- Windows: Airspy, HydraSDR and HackRF need the WinUSB driver (Zadig) once;
  RTL-SDR too. The Settings page's Check button reports a clear message when the
  driver is missing.
- Linux deb and snap: the vendor packages install the udev rules; the snap needs
  `snap connect aquila-radio-astronomy:raw-usb`.
- macOS: Homebrew tools need no driver.

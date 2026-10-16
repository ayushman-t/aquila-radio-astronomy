#!/bin/sh
# Build python3-astro-virgo_<ver>-1_all.deb from the PyPI sdist, since Debian does not
# ship Virgo. Needs debhelper, dh-python, pybuild-plugin-pyproject, python3-all,
# python3-setuptools. The .deb lands in build/virgo/.
#   sh packaging/apt/virgo-deb.sh 3.8.2
set -e
VER=${1:-3.8.2}
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf build/virgo && mkdir -p build/virgo && cd build/virgo
pip download --no-deps --no-binary :all: "astro-virgo==$VER"
tar xf astro-virgo-*.tar.gz
cd astro-virgo-*/
mkdir debian
cat > debian/control <<EOT
Source: astro-virgo
Section: python
Priority: optional
Maintainer: Ayushman Tripathi <ayushmantripathi7724@gmail.com>
Build-Depends: debhelper-compat (= 13), dh-python, python3-all, python3-setuptools
Standards-Version: 4.6.2
Homepage: https://github.com/0xCoto/Virgo

Package: python3-astro-virgo
Architecture: all
Depends: \${python3:Depends}, \${misc:Depends}, python3-numpy, python3-matplotlib, python3-astropy
Description: Virgo, a spectrometer and hydrogen line toolkit for radio astronomy
 Python package and virgo command for hydrogen line observation, calibration and
 plotting. Packaged for Aquila Radio Astronomy.
EOT
cat > debian/changelog <<EOT
astro-virgo ($VER-1) unstable; urgency=medium

  * Packaged from the PyPI sdist for Aquila Radio Astronomy.

 -- Ayushman Tripathi <ayushmantripathi7724@gmail.com>  $(date -R)
EOT
printf '#!/usr/bin/make -f\nexport PYBUILD_NAME=astro-virgo\n\n%%:\n\tdh $@ --with python3 --buildsystem=pybuild\n\noverride_dh_auto_test:\n' > debian/rules
chmod +x debian/rules
cp "$HERE/virgo-copyright" debian/copyright
dpkg-buildpackage -us -uc -b
ls ../*.deb

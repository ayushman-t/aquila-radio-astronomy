#!/bin/sh
# Build python3-astro-virgo_<ver>_all.deb from the PyPI sdist (Debian does not ship
# Virgo). Needs: python3-stdeb, dh-python, python3-all. Output lands in deb_dist/.
#   sh packaging/apt/virgo-deb.sh 3.8.2
set -e
VER=${1:-3.8.2}
rm -rf build/virgo && mkdir -p build/virgo && cd build/virgo
pip download --no-deps --no-binary :all: "astro-virgo==$VER"
tar xf astro-virgo-*.tar.gz
cd astro-virgo-*/
python3 setup.py --command-packages=stdeb.command sdist_dsc \
    --package3 python3-astro-virgo --depends3 "python3-astropy, python3-matplotlib, python3-numpy" \
    --copyright-file ../../../packaging/apt/virgo-copyright bdist_deb
ls deb_dist/*.deb

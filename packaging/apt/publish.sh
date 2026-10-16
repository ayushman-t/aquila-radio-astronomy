#!/bin/sh
# Add .deb files to the apt repository tree and sign it. Run on the machine that
# holds the signing key, then upload $REPO to https://radioastronomy.in/apt/.
#   packaging/apt/publish.sh dist/aquila-radio-astronomy_0.2.0-1_all.deb [more.deb ...]
# Needs: reprepro, the GPG key named in KEY. First run creates the tree.
set -e
REPO=${REPO:-$HOME/aquila-apt}
KEY=${KEY:-apt@radioastronomy.in}
mkdir -p "$REPO/conf"
[ -f "$REPO/conf/distributions" ] || cat > "$REPO/conf/distributions" <<EOF
Origin: radioastronomy.in
Label: Aquila Radio Astronomy
Codename: stable
Architectures: all amd64 arm64
Components: main
Description: Aquila Radio Astronomy and its dependencies
SignWith: $KEY
EOF
for deb in "$@"; do
    reprepro -b "$REPO" includedeb stable "$deb"
done
gpg --export "$KEY" > "$REPO/aquila.gpg"
echo "repository at $REPO; upload dists/ pool/ and aquila.gpg to https://radioastronomy.in/apt/"
echo "users: curl -fsSL https://radioastronomy.in/apt/aquila.gpg | sudo tee /usr/share/keyrings/aquila.gpg >/dev/null"
echo "       echo 'deb [signed-by=/usr/share/keyrings/aquila.gpg] https://radioastronomy.in/apt stable main' | sudo tee /etc/apt/sources.list.d/aquila.list"

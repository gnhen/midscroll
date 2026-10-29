#!/usr/bin/env bash
# Build the RPM into dist/. Needs:
#   sudo dnf install rpm-build systemd-rpm-macros libappstream-glib \
#                    desktop-file-utils
set -euo pipefail
cd "$(dirname "$0")"
repo=$(cd ../.. && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# rpmbuild wants flat sources
cp "$repo"/midscroll.py "$repo"/midscroll-overlay.py "$repo"/midscroll.conf \
   "$repo"/midscroll-settings.py "$repo"/midscroll-apply.py \
   "$repo"/io.github.gnhen.midscroll.Settings.desktop \
   "$repo"/io.github.gnhen.midscroll.policy \
   "$repo"/io.github.gnhen.midscroll.Settings.metainfo.xml \
   "$repo"/README.md "$repo"/SECURITY.md "$repo"/LICENSE \
   "$repo"/systemd/midscroll.service "$repo"/systemd/midscroll-overlay.service \
   "$repo"/icons/move-vertical.svg \
   "$repo"/gnome-extension/extension.js "$repo"/gnome-extension/metadata.json \
   "$repo"/gnome-extension/stylesheet.css "$work"/

mkdir -p "$repo/dist"
rpmbuild -bb \
    --define "_sourcedir $work" \
    --define "_rpmdir $work/out" \
    midscroll.spec
cp "$work"/out/noarch/*.rpm "$repo/dist/"
echo
echo "Built: $(ls "$repo"/dist/*.rpm)"

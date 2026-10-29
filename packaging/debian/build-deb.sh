#!/usr/bin/env bash
# Build a binary .deb into dist/. Uses dpkg-deb when available, otherwise
# assembles the archive with ar/tar so the package can be built on
# non-Debian systems too.
#
# Set DEB_MAINTAINER to override the maintainer field:
#   DEB_MAINTAINER="Name <email>" ./build-deb.sh
set -euo pipefail
cd "$(dirname "$0")"
repo=$(cd ../.. && pwd)

version=1.15-1
maintainer=${DEB_MAINTAINER:-"midscroll maintainers <noreply@example.com>"}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
stage="$work/stage"

install -Dm755 "$repo/midscroll.py" "$stage/usr/bin/midscroll"
install -Dm755 "$repo/midscroll-overlay.py" "$stage/usr/bin/midscroll-overlay"
install -Dm755 "$repo/midscroll-settings.py" "$stage/usr/bin/midscroll-settings"
install -Dm755 "$repo/midscroll-apply.py" "$stage/usr/bin/midscroll-apply"
install -Dm644 "$repo/io.github.gnhen.midscroll.Settings.desktop" \
    "$stage/usr/share/applications/io.github.gnhen.midscroll.Settings.desktop"
install -Dm644 "$repo/io.github.gnhen.midscroll.policy" \
    "$stage/usr/share/polkit-1/actions/io.github.gnhen.midscroll.policy"
install -Dm644 "$repo/systemd/midscroll.service" \
    "$stage/usr/lib/systemd/system/midscroll.service"
install -Dm644 "$repo/systemd/midscroll-overlay.service" \
    "$stage/usr/lib/systemd/user/midscroll-overlay.service"
install -Dm644 "$repo/icons/move-vertical.svg" \
    "$stage/usr/share/midscroll/move-vertical.svg"
install -Dm644 "$repo/icons/move-vertical.svg" \
    "$stage/usr/share/icons/hicolor/scalable/apps/midscroll.svg"
for f in extension.js metadata.json stylesheet.css; do
    install -Dm644 "$repo/gnome-extension/$f" \
        "$stage/usr/share/gnome-shell/extensions/midscroll-indicator@midscroll/$f"
done
install -Dm644 "$repo/midscroll.conf" "$stage/etc/midscroll.conf"
install -Dm644 "$repo/README.md" "$stage/usr/share/doc/midscroll/README.md"
install -Dm644 "$repo/SECURITY.md" "$stage/usr/share/doc/midscroll/SECURITY.md"
install -Dm644 "$repo/LICENSE" "$stage/usr/share/doc/midscroll/copyright"

mkdir -p "$stage/DEBIAN"
cat > "$stage/DEBIAN/control" <<EOF
Package: midscroll
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-evdev, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, librsvg2-common, polkitd | policykit-1
Recommends: libgtk4-layer-shell0, gir1.2-gtk4layershell-1.0, kdotool, x11-utils
Maintainer: $maintainer
Description: Windows-style middle-button drag autoscroll
 Hold the middle mouse button and drag to scroll, with speed proportional
 to drag distance, like Windows 10/11. Works on Wayland and X11 in every
 application by operating at the kernel input layer (evdev/uinput).
 .
 Includes a GNOME Shell extension that does the scrolling on GNOME with a
 free-moving cursor, and a session overlay that shows a scroll badge at the
 anchored cursor while a drag-scroll is active (KDE Plasma; needs kdotool
 and gtk4-layer-shell).
EOF

printf '/etc/midscroll.conf\n' > "$stage/DEBIAN/conffiles"

cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    systemctl daemon-reload || true
    systemctl enable --now midscroll.service || true
    systemctl --global enable midscroll-overlay.service || true
    if [ -z "$2" ]; then
        echo "GNOME: log out and back in, then turn on the Shell extension:"
        echo "  gnome-extensions enable midscroll-indicator@midscroll"
    fi
fi
EOF

cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ]; then
    systemctl disable --now midscroll.service || true
    systemctl --global disable midscroll-overlay.service || true
fi
EOF

cat > "$stage/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
systemctl daemon-reload || true
EOF

chmod 755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm" "$stage/DEBIAN/postrm"

mkdir -p "$repo/dist"
out="$repo/dist/midscroll_${version}_all.deb"

if command -v dpkg-deb >/dev/null; then
    dpkg-deb --build --root-owner-group "$stage" "$out"
else
    # Assemble by hand: a .deb is an ar archive of debian-binary +
    # control.tar.gz + data.tar.gz, in that order.
    echo "2.0" > "$work/debian-binary"
    tar -C "$stage/DEBIAN" --owner=0 --group=0 --numeric-owner \
        -czf "$work/control.tar.gz" .
    rm -rf "$stage/DEBIAN"
    tar -C "$stage" --owner=0 --group=0 --numeric-owner \
        -czf "$work/data.tar.gz" .
    rm -f "$out"
    ar rc "$out" "$work/debian-binary" "$work/control.tar.gz" \
        "$work/data.tar.gz"
fi

echo
echo "Built: $out"

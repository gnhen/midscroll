#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

if [ -n "${SUDO_USER:-}" ]; then
    uid=$(id -u "$SUDO_USER")
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user stop midscroll-overlay.service || true
    sudo -u "$SUDO_USER" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        python3 - <<'PY' || true
from gi.repository import Gio
try:
    s = Gio.Settings.new("org.gnome.shell")
except Exception:
    raise SystemExit  # not a GNOME system
on = s.get_strv("enabled-extensions")
if "midscroll-indicator@midscroll" in on:
    on.remove("midscroll-indicator@midscroll")
    s.set_strv("enabled-extensions", on)
    Gio.Settings.sync()
PY
fi
systemctl --global disable midscroll-overlay.service || true
systemctl disable --now midscroll.service || true
rm -f /etc/systemd/system/midscroll.service /usr/bin/midscroll /etc/midscroll.conf
rm -f /usr/lib/systemd/user/midscroll-overlay.service /usr/bin/midscroll-overlay
rm -f /usr/bin/midscroll-settings /usr/bin/midscroll-apply
rm -f /usr/share/applications/io.github.gnhen.midscroll.Settings.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/midscroll.svg
rm -f /usr/share/polkit-1/actions/io.github.gnhen.midscroll.policy
rm -rf /usr/share/midscroll
rm -rf /usr/share/gnome-shell/extensions/midscroll-indicator@midscroll
systemctl daemon-reload

echo "midscroll removed."

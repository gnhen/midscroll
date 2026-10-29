Name:           midscroll
Version:        1.15
Release:        1%{?dist}
Summary:        Windows-style middle-button drag autoscroll
License:        Unlicense
BuildArch:      noarch

Source0:        midscroll.py
Source1:        midscroll.service
Source2:        midscroll.conf
Source3:        README.md
Source4:        midscroll-overlay.py
Source5:        midscroll-overlay.service
Source6:        move-vertical.svg
Source7:        LICENSE
Source8:        midscroll-settings.py
Source9:        midscroll-apply.py
Source10:       io.github.gnhen.midscroll.Settings.desktop
Source11:       io.github.gnhen.midscroll.policy
Source12:       SECURITY.md
Source13:       io.github.gnhen.midscroll.Settings.metainfo.xml

Requires:       python3
Requires:       python3-evdev
# Overlay and settings GUI (GTK)
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       gtk4-layer-shell
Requires:       librsvg2
Requires:       kdotool
# Settings GUI applies changes as root through pkexec
Requires:       polkit
# Focus detection on X11 sessions (app blacklist)
Recommends:     xprop
%{?systemd_requires}
BuildRequires:  systemd-rpm-macros
# Validating the software-center metadata (%%check)
BuildRequires:  libappstream-glib
BuildRequires:  desktop-file-utils

%description
Hold the middle mouse button and drag to scroll, with speed proportional
to drag distance, like Windows 10/11. Works on Wayland and X11 in every
application by operating at the kernel input layer (evdev/uinput).
Includes a session overlay that draws the autoscroll badge at the
anchored pointer and a ghost cursor that follows your hand while a
drag-scroll is active, and a GTK settings GUI that also picks which
devices count as a mouse.

%install
install -Dm755 %{SOURCE0} %{buildroot}%{_bindir}/midscroll
install -Dm644 %{SOURCE1} %{buildroot}%{_unitdir}/midscroll.service
install -Dm644 %{SOURCE2} %{buildroot}%{_sysconfdir}/midscroll.conf
install -Dm644 %{SOURCE3} %{buildroot}%{_docdir}/midscroll/README.md
install -Dm644 %{SOURCE12} %{buildroot}%{_docdir}/midscroll/SECURITY.md
install -Dm755 %{SOURCE4} %{buildroot}%{_bindir}/midscroll-overlay
install -Dm644 %{SOURCE5} %{buildroot}%{_userunitdir}/midscroll-overlay.service
install -Dm644 %{SOURCE6} %{buildroot}%{_datadir}/midscroll/move-vertical.svg
# Same artwork as the scroll badge, as the app's themed icon.
install -Dm644 %{SOURCE6} \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/midscroll.svg
install -Dm644 %{SOURCE7} %{buildroot}%{_licensedir}/midscroll/LICENSE
install -Dm755 %{SOURCE8} %{buildroot}%{_bindir}/midscroll-settings
install -Dm755 %{SOURCE9} %{buildroot}%{_bindir}/midscroll-apply
install -Dm644 %{SOURCE10} \
    %{buildroot}%{_datadir}/applications/io.github.gnhen.midscroll.Settings.desktop
install -Dm644 %{SOURCE11} \
    %{buildroot}%{_datadir}/polkit-1/actions/io.github.gnhen.midscroll.policy
# Software-center metadata, so the settings app shows up in Discover/GNOME Software
install -Dm644 %{SOURCE13} \
    %{buildroot}%{_metainfodir}/io.github.gnhen.midscroll.Settings.metainfo.xml
install -d %{buildroot}%{_userpresetdir}
echo "enable midscroll-overlay.service" \
    > %{buildroot}%{_userpresetdir}/90-midscroll.preset

%check
appstream-util validate-relax --nonet \
    %{buildroot}%{_metainfodir}/io.github.gnhen.midscroll.Settings.metainfo.xml
desktop-file-validate \
    %{buildroot}%{_datadir}/applications/io.github.gnhen.midscroll.Settings.desktop

%post
%systemd_post midscroll.service
%systemd_user_post midscroll-overlay.service
# Enable and start immediately on first install
if [ $1 -eq 1 ]; then
    systemctl enable --now midscroll.service >/dev/null 2>&1 || :
fi

%preun
%systemd_preun midscroll.service
%systemd_user_preun midscroll-overlay.service

%postun
%systemd_postun_with_restart midscroll.service
%systemd_user_postun_with_restart midscroll-overlay.service

%files
%license %{_licensedir}/midscroll/LICENSE
%doc %{_docdir}/midscroll/README.md
%doc %{_docdir}/midscroll/SECURITY.md
%{_bindir}/midscroll
%{_bindir}/midscroll-overlay
%{_bindir}/midscroll-settings
%{_bindir}/midscroll-apply
%{_unitdir}/midscroll.service
%{_userunitdir}/midscroll-overlay.service
%{_userpresetdir}/90-midscroll.preset
%{_datadir}/midscroll/move-vertical.svg
%{_datadir}/icons/hicolor/scalable/apps/midscroll.svg
%{_datadir}/applications/io.github.gnhen.midscroll.Settings.desktop
%{_datadir}/polkit-1/actions/io.github.gnhen.midscroll.policy
%{_metainfodir}/io.github.gnhen.midscroll.Settings.metainfo.xml
%config(noreplace) %{_sysconfdir}/midscroll.conf

%changelog
* Tue Aug 26 2026 midscroll - 1.15-1
- SNAP_CURSOR_ON_RELEASE: snap the anchored cursor to the ghost position
  once a drag-scroll ends, matching Windows autoscroll behavior
  - Enabled by default, toggleable in the settings GUI, /etc/midscroll.conf,
    or via --snap-cursor / --no-snap-cursor
- Sub-deadzone motion no longer discarded: small drags within DEADZONE_PX
  now replay their accumulated offset on release instead of silently
  vanishing
- SNAP_CURSOR_ON_RELEASE help text now reflects "default: on"
- Snap uses GHOST_SCALE so the cursor always lands exactly where the
  ghost cursor was drawn

* Tue Jul 28 2026 midscroll - 1.14-1
- Ship AppStream metadata, so the settings app has a proper listing in
  Discover and GNOME Software instead of appearing as a bare package
- Validate the metadata and the desktop entry as part of the build

* Mon Jul 27 2026 midscroll - 1.13-1
- Choose which devices count as a mouse: EXTRA_DEVICES / IGNORE_DEVICES in
  the config, --extra-device / --ignore-device / --list-devices on the
  command line, and a device list with a switch each in the settings GUI
- Draw a ghost cursor that follows your hand while dragging, with the badge
  locked at the anchor, the way Windows autoscroll looks (GHOST_CURSOR,
  GHOST_SCALE)
- Security: the config is only read when root owns it and nobody else can
  write it; keyboard-class devices are refused unless ALLOW_KEYBOARDS is
  set by a root edit, which midscroll-apply cannot do; state-socket clients
  are capped and cursor positions go only to the active session; the daemon
  now runs with no capabilities; every Apply authenticates. See SECURITY.md

* Mon Jul 20 2026 midscroll - 1.12-1
- Add an "Enable on desktop & panels" toggle (DESKTOP_SCROLL, off by
  default): while a desktop shell is focused (plasmashell, nemo-desktop,
  xfdesktop, waybar, xfce4-panel, GNOME Shell/gjs) midscroll pauses, so a
  middle-drag no longer hijacks the desktop or a panel. Exposed in the
  settings GUI and as --desktop / --no-desktop

* Mon Jul 20 2026 midscroll - 1.11-1
- Fix the scroll badge and app blacklist still missing under the overlay
  sandbox: use ProtectSystem=full instead of strict (strict makes /tmp
  read-only, so kdotool cannot write the KWin script it hands the compositor)
- Open the settings window tall enough to show every control, capped to the
  monitor height so it never runs off a small screen

* Mon Jul 20 2026 midscroll - 1.10-1
- Fix the scroll badge and app blacklist regressing under 1.8's overlay
  sandbox: drop PrivateTmp, which broke kdotool (it hands KWin a script via
  a /tmp path the compositor must read)
- Use the scroll-arrows badge artwork as the settings app icon (themed
  hicolor icon; desktop file renamed to the app ID so the window icon maps)

* Mon Jul 20 2026 midscroll - 1.9-1
- Bound and sanitize the BLACKLIST value in midscroll-apply (the only
  free-text field crossing the pkexec boundary): strip non-window-class
  characters and cap length, so it cannot mangle the config or carry a
  huge/malformed payload
- Ship a polkit policy for midscroll-apply (auth_admin_keep), so applying
  settings from the GUI uses a scoped, briefly-cached authorization instead
  of generic full-admin auth

* Mon Jul 20 2026 midscroll - 1.8-1
- Sandbox the session helper (midscroll-overlay): NoNewPrivileges,
  ProtectSystem=strict, PrivateTmp, kernel/clock/namespace protections and a
  restricted address-family set, matching the daemon's hardening
- Add a syscall filter (@system-service, EPERM) and MemoryDenyWriteExecute to
  the daemon
- Refuse to grab our own uinput mirrors even if the phys marker fails, and
  warn at startup if the phys marker does not round-trip

* Mon Jul 20 2026 midscroll - 1.7-1
- Settings GUI (midscroll-settings): a GTK window to change every tunable -
  speed, dead zone, event rate, natural scrolling, the app blacklist and
  the new toggle mode - applied via pkexec with an automatic daemon restart
- Toggle mode (TOGGLE_MODE): click the middle button once to start
  autoscroll and again, or any click, to stop it (Windows-Explorer /
  Firefox style) instead of holding and dragging (issue #1 feature request)

* Mon Jul 20 2026 midscroll - 1.6-1
- Preserve per-mouse pointer settings: mirror each mouse through its own
  uinput device that copies the source name/vendor/product, so libinput and
  KDE no longer reset pointer speed and acceleration to defaults
- Stop grabbing keyboards: require both REL_X and REL_Y so a device exposing
  a stray pointer capability via media keys is no longer captured

* Mon Jul 20 2026 midscroll - 1.5-1
- Only trust focus reports from a logged-in user's helper (peer-credential
  check on the state socket), so a stray local process can't pause the
  daemon
- Track focus per helper: a second session or a stale helper exiting no
  longer wipes the live one's focus
- Keep enforcing the app blacklist during a drag, not just at press time,
  so switching into a blacklisted app stops an in-progress scroll
- Resync held buttons after dropped kernel events so a button can't stick
- Time the scroll ticks by the monotonic clock, so speed holds under load
- is_mouse only excludes real pointing-absolute devices, not any device
  with a stray absolute axis

* Mon Jul 20 2026 midscroll - 1.4-1
- Per-device scroll state (multiple mice can no longer corrupt each other)
- App blacklist: pause automatically over apps that use middle-drag
  themselves (default: FreeCAD, OrcaSlicer, Minecraft)
- Command-line interface for overriding any tunable per run
- Strict config validation, logging with --debug, systemd sandboxing

* Mon Jul 20 2026 midscroll - 1.3-1
- Initial public release

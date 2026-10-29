#!/usr/bin/env python3
"""midscroll-overlay - session helper for midscroll's middle-drag autoscroll.

Two jobs, both over the daemon's socket at /run/midscroll/state.sock:

1. While a drag-scroll is active, draws the autoscroll cursor: a badge
   with a vertical-arrows icon locked to the point where midscroll has
   anchored the pointer, and a ghost cursor that follows your hand from
   there, the way Windows autoscroll looks (Wayland only; both are drawn
   with wlr-layer-shell).
2. Reports the focused window's class back to the daemon so it can pause
   itself over blacklisted apps (CAD, slicers, games that use middle-drag
   natively). Polled once a second via kdotool on Wayland KDE, or xprop
   on X11 - both tiny, ubiquitous tools, so no new library dependencies.

The drawing surface sits on the compositor's overlay layer with an empty
input region and no keyboard interactivity, so clicks, scrolling and focus
pass straight through it. It covers the monitor (the ghost has to be able
to go anywhere), so that empty input region is the thing that must never
be wrong: it is re-applied every time the surface is mapped, the surface
is only mapped while a drag is actually running, and it is taken down if
the daemon goes quiet for STALE_SEC or the socket drops.

The anchor position is read once per drag via kdotool (KWin scripting);
midscroll pins the real pointer for the whole drag, so one query is
enough, and the ghost's offsets come from the daemon. Without kdotool
nothing is drawn and scrolling is unaffected. On X11 sessions there is no
drawing, but focus reporting still runs.
"""

import array
import ctypes.util
import logging
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

log = logging.getLogger("midscroll-overlay")

WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))

if WAYLAND:
    # gtk4-layer-shell must be loaded before libwayland-client, which a
    # Python process can only guarantee via LD_PRELOAD; re-exec once with
    # it set.
    _LS = ctypes.util.find_library("gtk4-layer-shell")
    if _LS and _LS not in os.environ.get("LD_PRELOAD", ""):
        os.environ["LD_PRELOAD"] = (_LS + " "
                                    + os.environ.get("LD_PRELOAD", "")).strip()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    import cairo
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    from gi.repository import Gtk4LayerShell as LayerShell

SOCK_PATH = "/run/midscroll/state.sock"
ICON_PATH = "/usr/share/midscroll/move-vertical.svg"
BADGE_PX = 42       # badge diameter
ICON_PX = 24        # icon size inside the badge
GHOST_W = 22        # fallback cursor size, when the theme can't be read
GHOST_H = 34
GHOST_ALPHA = 0.85  # so the ghost reads as a copy, not the real pointer
FOCUS_POLL_SEC = 1.0
STALE_SEC = 5.0     # take the overlay down if the daemon stops talking
MAX_OFFSET = 100000  # ignore an implausible position line

# Where to look for the pointer image, and what it might be called. The
# ghost is meant to be your cursor, so it is loaded from the same theme
# the compositor draws with rather than invented here.
CURSOR_DIRS = ("~/.local/share/icons", "~/.icons", "/usr/share/icons",
               "/usr/local/share/icons", "/usr/share/pixmaps")
CURSOR_NAMES = ("default", "left_ptr", "arrow", "top_left_arrow")
DEFAULT_CURSOR_SIZE = 24
XCURSOR_IMAGE = 0xfffd0002  # chunk type for an image, in an Xcursor file
MAX_CURSOR_PX = 256         # sanity bound on an image we will map

FOCUS_TOOL = shutil.which("kdotool") if WAYLAND else shutil.which("xprop")

CSS = """
window { background: transparent; }
.badge {
    background-color: rgba(30, 30, 32, 0.82);
    border: 1px solid rgba(255, 255, 255, 0.28);
    border-radius: 9999px;
}
"""


def kde_input_setting(key, fallback=None):
    """A value from kcminputrc, where KDE keeps the cursor settings."""
    path = os.path.expanduser("~/.config/kcminputrc")
    try:
        with open(path) as f:
            for line in f:
                name, _, value = line.partition("=")
                if name.strip() == key and value.strip():
                    return value.strip()
    except OSError:
        pass
    return fallback


def cursor_theme_name():
    return (os.environ.get("XCURSOR_THEME")
            or kde_input_setting("cursorTheme", "default"))


def cursor_size():
    for value in (os.environ.get("XCURSOR_SIZE"),
                  kde_input_setting("cursorSize")):
        try:
            size = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < size <= MAX_CURSOR_PX:
            return size
    return DEFAULT_CURSOR_SIZE


def find_cursor_file(theme, seen=None):
    """Path to a theme's pointer cursor, following Inherits= if needed."""
    seen = seen if seen is not None else set()
    if not theme or theme in seen or len(seen) > 8:
        return None
    seen.add(theme)
    inherits = []
    for directory in CURSOR_DIRS:
        base = os.path.join(os.path.expanduser(directory), theme)
        for name in CURSOR_NAMES:
            path = os.path.join(base, "cursors", name)
            if os.path.isfile(path):
                return path
        try:
            with open(os.path.join(base, "index.theme")) as f:
                for line in f:
                    if line.startswith("Inherits"):
                        inherits += [p.strip() for p in
                                     line.partition("=")[2].split(",")
                                     if p.strip()]
        except OSError:
            pass
    for parent in inherits:
        path = find_cursor_file(parent, seen)
        if path:
            return path
    return None


def read_xcursor(path, want):
    """(pixels, width, height, xhot, yhot) for one image in an Xcursor file.

    Xcursor is a small container: a header, a table of contents, then
    chunks. We take the image chunk whose nominal size is closest to the
    one the compositor would have picked, and the first frame if the
    cursor happens to be animated. Only sizes we would actually draw are
    accepted, so a corrupt or hostile file can't make us map something
    enormous.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(4 << 20)
    except OSError:
        return None
    if len(data) < 16 or data[:4] != b"Xcur":
        return None
    _magic, header, _version, ntoc = struct.unpack_from("<4sIII", data)
    best = None
    for i in range(min(ntoc, 1024)):
        try:
            ctype, nominal, pos = struct.unpack_from("<III", data,
                                                     header + i * 12)
        except struct.error:
            break
        if ctype != XCURSOR_IMAGE:
            continue
        if best is None or abs(nominal - want) < abs(best[0] - want):
            best = (nominal, pos)
    if best is None:
        return None
    try:
        (_size, _type, _subtype, _version, width, height, xhot, yhot,
         _delay) = struct.unpack_from("<9I", data, best[1])
    except struct.error:
        return None
    if not (0 < width <= MAX_CURSOR_PX and 0 < height <= MAX_CURSOR_PX):
        return None
    if not (xhot <= width and yhot <= height):
        return None
    off = best[1] + 36
    count = width * height
    try:
        pixels = struct.unpack_from(f"<{count}I", data, off)
    except struct.error:
        return None
    # Xcursor stores premultiplied ARGB little-endian, which is what a
    # cairo ARGB32 surface wants once it is in this machine's word order.
    return array.array("I", pixels).tobytes(), width, height, xhot, yhot


def active_window_class():
    """Class of the focused window, or "" if it can't be determined."""
    try:
        if WAYLAND:
            # KDE only: kdotool drives KWin's scripting API.
            out = subprocess.run(
                ["kdotool", "getactivewindow", "getwindowclassname"],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else ""
        out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                             capture_output=True, text=True, timeout=5)
        m = re.search(r"window id # (0x[0-9a-fA-F]+)", out.stdout)
        if not m:
            return ""
        out = subprocess.run(["xprop", "-id", m.group(1), "WM_CLASS"],
                             capture_output=True, text=True, timeout=5)
        # WM_CLASS(STRING) = "instance", "Class"; report both so the
        # daemon's substring match sees whichever casing the app uses.
        m = re.search(r'=\s*"([^"]*)",\s*"([^"]*)"', out.stdout)
        return f"{m.group(1)} {m.group(2)}" if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def report_focus(sock, stop):
    """Push focus changes to the daemon until the connection dies."""
    if not FOCUS_TOOL:
        log.warning("%s not found; app blacklist will be inactive",
                    "kdotool" if WAYLAND else "xprop")
        return
    last = None
    while not stop.is_set():
        cls = active_window_class()
        if cls != last:
            last = cls
            try:
                sock.sendall(b"focus " + cls.encode("utf-8", "replace")
                             + b"\n")
            except OSError:
                return
        stop.wait(FOCUS_POLL_SEC)


if WAYLAND:
    def load_theme_cursor():
        """Your own pointer image as (surface, xhot, yhot), or None.

        The ghost should look like the cursor you already have, so it is
        read straight out of the active Xcursor theme - the same file the
        compositor draws from - rather than approximated.
        """
        path = find_cursor_file(cursor_theme_name())
        if not path:
            return None
        image = read_xcursor(path, cursor_size())
        if not image:
            log.warning("could not read the cursor theme at %s; drawing a "
                        "plain arrow instead", path)
            return None
        pixels, width, height, xhot, yhot = image
        stride = cairo.ImageSurface.format_stride_for_width(
            cairo.FORMAT_ARGB32, width)
        surface = cairo.ImageSurface.create_for_data(
            bytearray(pixels), cairo.FORMAT_ARGB32, width, height, stride)
        log.info("ghost cursor: %s (%dx%d, hotspot %d,%d)",
                 path, width, height, xhot, yhot)
        return surface, xhot, yhot

    def draw_fallback_ghost(cr, width, height):
        """A plain arrow, for when the theme can't be read."""
        s = min(width / GHOST_W, height / GHOST_H)
        cr.scale(s, s)
        cr.move_to(1, 1)
        cr.line_to(1, 25)
        cr.line_to(6.5, 20)
        cr.line_to(10, 28.5)
        cr.line_to(13.5, 27)
        cr.line_to(10, 19)
        cr.line_to(17, 18.5)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.72)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.85)
        cr.set_line_width(1.5)
        cr.stroke()

    def make_ghost_drawer(cursor):
        """Draw function for the ghost: your own pointer if we have it."""
        def draw(_area, cr, width, height, *_data):
            if cursor is None:
                draw_fallback_ghost(cr, width, height)
                return
            cr.set_source_surface(cursor[0], 0, 0)
            # Slightly see-through: it is a copy of your cursor, and it
            # should be readable as one rather than mistaken for the real
            # pointer, which is parked back at the badge.
            cr.paint_with_alpha(GHOST_ALPHA)
        return draw

    class Overlay:
        """The badge and ghost cursor, on one click-through surface."""

        def __init__(self, app):
            self.win = Gtk.Window(application=app)
            LayerShell.init_for_window(self.win)
            LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
            LayerShell.set_namespace(self.win, "midscroll")
            LayerShell.set_keyboard_mode(self.win,
                                         LayerShell.KeyboardMode.NONE)
            LayerShell.set_exclusive_zone(self.win, -1)
            # All four edges: the surface covers the monitor, so the ghost
            # can be drawn anywhere on it. Nothing on it takes input.
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                         LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self.win, edge, True)

            icon = Gtk.Image.new_from_file(ICON_PATH)
            icon.set_pixel_size(ICON_PX)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            icon.set_hexpand(True)
            icon.set_vexpand(True)
            self.badge = Gtk.Box()
            self.badge.add_css_class("badge")
            self.badge.set_size_request(BADGE_PX, BADGE_PX)
            self.badge.append(icon)
            self.ghost = Gtk.DrawingArea()
            self.cursor = None
            self.cursor_signature = None
            self.fixed = Gtk.Fixed()
            self.fixed.put(self.badge, 0, 0)
            self.fixed.put(self.ghost, 0, 0)
            self.win.set_child(self.fixed)

            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            # The surface is recreated whenever the window is shown again,
            # and a full-screen surface that took input would swallow every
            # click in the session, so this is re-applied on every map -
            # never assumed to have survived from last time.
            self.win.connect("realize", self._make_click_through)
            self.win.connect("map", self._make_click_through)
            self.active = False
            self.seq = 0        # discards stale position queries
            self.anchor = None  # where the pointer is pinned, monitor-local
            self.offset = (0, 0)
            self.geo = None     # geometry of the monitor we are drawn on
            self.last_line = time.monotonic()
            GLib.timeout_add_seconds(1, self._check_stale)
            self._refresh_cursor()

        def _cursor_signature(self):
            return (cursor_theme_name(), cursor_size())

        def _refresh_cursor(self):
            """Reload the ghost when the compositor's cursor settings change."""
            signature = self._cursor_signature()
            if signature == self.cursor_signature:
                return
            cursor = load_theme_cursor()
            self.cursor_signature = signature
            self.cursor = cursor
            if cursor is None:
                self.hotspot = (1, 1)
                self.ghost.set_size_request(GHOST_W, GHOST_H)
            else:
                self.hotspot = (cursor[1], cursor[2])
                self.ghost.set_size_request(cursor[0].get_width(),
                                            cursor[0].get_height())
            self.ghost.set_draw_func(make_ghost_drawer(cursor))

        def _make_click_through(self, *_):
            surface = self.win.get_surface()
            if surface is not None:
                surface.set_input_region(cairo.Region())

        def _check_stale(self):
            """Watchdog for the two ways this could go wrong.

            Never leave the overlay up if the daemon stops talking, and
            re-assert the empty input region while it is up, so even a
            missed map signal can only cost a fraction of a second of
            clicks rather than the rest of the session.
            """
            if self.active:
                self._make_click_through()
                self._refresh_cursor()
                if time.monotonic() - self.last_line > STALE_SEC:
                    log.warning("no state from midscroll for %.0fs; hiding",
                                STALE_SEC)
                    self.set_active(False)
            return True

        def note_line(self):
            self.last_line = time.monotonic()

        def set_active(self, active):
            self.note_line()
            if active == self.active:
                return
            self.active = active
            self.seq += 1
            if not active:
                self.win.set_visible(False)
                self.anchor = None
                self.offset = (0, 0)
                return
            self._refresh_cursor()
            # The pointer is pinned for the whole drag, so one query does.
            try:
                proc = Gio.Subprocess.new(
                    ["kdotool", "getmouselocation", "--shell"],
                    Gio.SubprocessFlags.STDOUT_PIPE
                    | Gio.SubprocessFlags.STDERR_SILENCE)
            except GLib.Error:
                log.warning("kdotool not available; nothing will be drawn")
                return
            proc.communicate_utf8_async(None, None, self._got_pos, self.seq)

        def set_offset(self, dx, dy):
            """Where the ghost is, relative to the anchor."""
            self.note_line()
            self.offset = (dx, dy)
            if self.active and self.anchor is not None:
                self._layout()

        def _got_pos(self, proc, res, seq):
            try:
                _ok, out, _err = proc.communicate_utf8_finish(res)
            except GLib.Error:
                return
            if seq != self.seq or not self.active:
                return  # the drag already ended
            mx = re.search(r"x[=:](-?\d+)", out, re.I)
            my = re.search(r"y[=:](-?\d+)", out, re.I)
            if not (mx and my):
                log.warning("bad kdotool output: %r", out)
                return
            self._anchor_at(int(mx.group(1)), int(my.group(1)))
            self._layout()
            self.win.set_visible(True)
            self._make_click_through()

        def _anchor_at(self, x, y):
            # Layer-shell surfaces belong to one output; find the monitor
            # holding the (global) anchor and work in its coordinates.
            monitors = Gdk.Display.get_default().get_monitors()
            self.geo = None
            for i in range(monitors.get_n_items()):
                mon = monitors.get_item(i)
                geo = mon.get_geometry()
                if (geo.x <= x < geo.x + geo.width
                        and geo.y <= y < geo.y + geo.height):
                    LayerShell.set_monitor(self.win, mon)
                    self.geo = geo
                    break
            if self.geo is None:
                self.anchor = (x, y)
            else:
                self.anchor = (x - self.geo.x, y - self.geo.y)

        def _layout(self):
            if self.anchor is None:
                return
            ax, ay = self.anchor
            self.fixed.move(self.badge, ax - BADGE_PX // 2,
                            ay - BADGE_PX // 2)
            gx, gy = ax + self.offset[0], ay + self.offset[1]
            if self.geo is not None:
                # Windows stops the cursor at the screen edge; so do we.
                gx = max(0, min(gx, self.geo.width - 1))
                gy = max(0, min(gy, self.geo.height - 1))
            # Place the image by its hotspot, the way the compositor
            # places the real one, so the ghost points at the same pixel.
            self.fixed.move(self.ghost, gx - self.hotspot[0],
                            gy - self.hotspot[1])


def overflow_uid():
    """The uid the kernel shows for an owner outside our user namespace."""
    try:
        with open("/proc/sys/kernel/overflowuid") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 65534


def socket_is_trustworthy(path=SOCK_PATH):
    """True if the state socket is the daemon's, not one someone planted.

    /run/midscroll is root-owned, so a socket there being anything else
    means something is badly wrong - which is exactly when it is worth
    not connecting, and worth not reporting our focused window to.

    The owner test has to allow for this being a sandboxed *user*
    service: systemd puts one in a user namespace where our own uid is
    the only one mapped, so root - like every other owner - reads back as
    the overflow uid. Root or an owner we cannot name is as good as it
    gets from in here; an owner we *can* name (ourselves, another logged
    in user) is precisely the case worth refusing.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (stat.S_ISSOCK(st.st_mode)
            and st.st_uid in (0, overflow_uid()))


def handle_line(overlay, text):
    """One line from the daemon: "1", "0" or "pos <dx> <dy>"."""
    if text == "1":
        overlay.set_active(True)
    elif text == "0":
        overlay.set_active(False)
    elif text.startswith("pos "):
        parts = text.split()
        if len(parts) != 3:
            return
        try:
            dx, dy = int(parts[1]), int(parts[2])
        except ValueError:
            return
        if abs(dx) <= MAX_OFFSET and abs(dy) <= MAX_OFFSET:
            overlay.set_offset(dx, dy)


def watch_socket(overlay):
    """Follow the daemon's state socket, reconnecting if it goes away.

    Each connection also gets a thread pushing focus reports back up it.
    """
    complained = False
    while True:
        stop = threading.Event()
        try:
            if not socket_is_trustworthy():
                if not complained:
                    complained = True
                    log.warning("%s is not a root-owned socket; not "
                                "connecting", SOCK_PATH)
                raise OSError
            complained = False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(SOCK_PATH)
                threading.Thread(target=report_focus, args=(s, stop),
                                 daemon=True).start()
                for line in s.makefile("r"):
                    if overlay:
                        GLib.idle_add(handle_line, overlay, line.strip())
        except OSError:
            pass
        finally:
            stop.set()
        if overlay:
            GLib.idle_add(overlay.set_active, False)
        time.sleep(2)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    if not WAYLAND:
        # X11 has no supported click-through overlay implementation here.
        # Keep focus reporting, but do not claim that ghost mode is active.
        log.info("X11 session: ghost cursor and badge unavailable; "
                 "focus reporting only")
        watch_socket(None)
        return

    app = Gtk.Application(application_id="org.midscroll.overlay",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        overlay = Overlay(app)
        threading.Thread(target=watch_socket, args=(overlay,),
                         daemon=True).start()

    app.connect("activate", activate)
    app.hold()  # stay alive while the badge is hidden
    app.run()


if __name__ == "__main__":
    main()

#!/usr/bin/python3
"""midscroll-settings - a small GTK4 GUI for midscroll's tunables.

Reads the current /etc/midscroll.conf (world-readable), lets you change
every setting - including toggle mode - with sliders, switches and a text
box, and writes it back through pkexec (midscroll-apply), restarting the
daemon so the change takes effect immediately. No terminal or config
editing required.
"""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

CONFIG_PATH = "/etc/midscroll.conf"

# key, label, lower, upper, step, digits
FLOATS = [
    ("DEADZONE_PX", "Dead zone (px)", 0, 200, 1, 0),
    ("SPEED_MULT", "Speed multiplier", 0.001, 1.0, 0.001, 3),
    ("SPEED_EXP", "Speed curve exponent", 0.5, 4.0, 0.1, 1),
    ("MAX_PX_PER_SEC", "Max speed (px/s)", 1000, 200000, 500, 0),
    ("PX_PER_NOTCH", "Pixels per wheel notch", 1, 500, 1, 0),
    ("MAX_DRAG_PX", "Max drag distance (px)", 100, 10000, 10, 0),
    ("TICK_HZ", "Event rate (Hz)", 10, 360, 5, 0),
]
# key, label, subtitle
BOOLS = [
    ("TOGGLE_MODE", "Toggle mode",
     "Click the middle button to start autoscroll and again to stop, "
     "instead of holding and dragging."),
    ("NATURAL", "Natural scrolling", "Invert the scroll direction."),
    ("DESKTOP_SCROLL", "Enable on desktop & panels",
     "Allow autoscroll while the desktop or a panel/taskbar is focused. "
     "Off by default so a middle-drag doesn't hijack the desktop."),
    ("FREE_CURSOR", "Free cursor during drag",
     "Let the cursor move freely while drag-scrolling instead of anchoring "
     "it. Note: once the cursor leaves the window you started in, the "
     "scroll jumps to whatever is under it."),
]
DEFAULTS = {
    "DEADZONE_PX": 15.0, "SPEED_MULT": 0.008, "SPEED_EXP": 2.2,
    "MAX_PX_PER_SEC": 30000.0, "PX_PER_NOTCH": 55.0, "MAX_DRAG_PX": 1200.0,
    "TICK_HZ": 90.0, "NATURAL": False, "TOGGLE_MODE": False,
    "DESKTOP_SCROLL": False, "FREE_CURSOR": False,
    "BLACKLIST": "freecad, orcaslicer, minecraft",
}
FLOAT_KEYS = [f[0] for f in FLOATS]
BOOL_KEYS = [b[0] for b in BOOLS]


def read_config(path=CONFIG_PATH):
    """Current values from the config file, defaults filling any gaps."""
    values = dict(DEFAULTS)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k in FLOAT_KEYS:
            try:
                values[k] = float(v)
            except ValueError:
                pass
        elif k in BOOL_KEYS:
            values[k] = v.lower() in ("1", "true", "yes", "on")
        elif k == "BLACKLIST":
            values[k] = v
    return values


def find_apply():
    """Locate the privileged writer (installed, or alongside this file).

    The installed /usr/bin/midscroll-apply is checked first, deliberately: it
    is root-owned, so it is preferred over a sibling copy that might sit in a
    user-writable checkout. The checkout paths are only a dev fallback.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in ("/usr/bin/midscroll-apply",
                 os.path.join(here, "midscroll-apply"),
                 os.path.join(here, "midscroll-apply.py")):
        if os.path.exists(cand):
            return cand
    return "/usr/bin/midscroll-apply"


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="midscroll")
        self.set_default_size(460, -1)
        self.floats = {}
        self.bools = {}

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        reset = Gtk.Button(label="Reset to defaults")
        reset.connect("clicked", self.on_reset)
        header.pack_start(reset)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        header.pack_end(self.apply_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Open tall enough to show every control instead of a stub window;
        # _fit_to_screen() then caps the height to the monitor so a small
        # screen scrolls the overflow rather than running off-screen.
        scroller.set_propagate_natural_height(True)
        self.connect("realize", self._fit_to_screen)
        outer.append(scroller)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        scroller.set_child(box)

        box.append(self._heading("Behavior"))
        for key, label, subtitle in BOOLS:
            box.append(self._bool_row(key, label, subtitle))

        box.append(self._heading("Speed and feel"))
        for key, label, lo, hi, step, digits in FLOATS:
            box.append(self._float_row(key, label, lo, hi, step, digits))

        box.append(self._heading("App blacklist"))
        hint = Gtk.Label(
            label="Comma-separated window-class substrings midscroll pauses "
                  "over (apps with their own middle-drag). Leave empty to "
                  "disable.",
            wrap=True, xalign=0)
        hint.add_css_class("dim-label")
        box.append(hint)
        self.blacklist = Gtk.Entry(hexpand=True)
        box.append(self.blacklist)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.set_margin_start(16)
        self.status.set_margin_end(16)
        self.status.set_margin_bottom(10)
        self.status.set_wrap(True)
        outer.append(self.status)

        self.load()

    def _fit_to_screen(self, *_):
        """Size to the content, capped at the monitor's height."""
        try:
            display = self.get_display()
            surface = self.get_surface()
            monitor = (display.get_monitor_at_surface(surface)
                       if surface else None)
            if monitor is None:
                monitors = display.get_monitors()
                monitor = (monitors.get_item(0)
                           if monitors.get_n_items() else None)
            if monitor is None:
                return
            avail = int(monitor.get_geometry().height * 0.92)
            _min, nat, _mb, _nb = self.measure(Gtk.Orientation.VERTICAL, 460)
            self.set_default_size(460, min(nat, avail))
        except Exception:
            pass  # sizing is best-effort; never block the window from opening

    # ---- widget builders ----
    def _heading(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.add_css_class("heading")
        lbl.set_margin_top(8)
        return lbl

    def _row(self, label):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Label(label=label, xalign=0, hexpand=True)
        row.append(text)
        return row

    def _float_row(self, key, label, lo, hi, step, digits):
        row = self._row(label)
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                             page_increment=step * 10)
        spin = Gtk.SpinButton(adjustment=adj, digits=digits)
        spin.set_valign(Gtk.Align.CENTER)
        self.floats[key] = spin
        row.append(spin)
        return row

    def _bool_row(self, key, label, subtitle):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(Gtk.Label(label=label, xalign=0))
        sub = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        col.append(sub)
        row.append(col)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.bools[key] = switch
        row.append(switch)
        return row

    # ---- data ----
    def load(self, values=None):
        vals = values if values is not None else read_config()
        for key, spin in self.floats.items():
            spin.set_value(float(vals[key]))
        for key, switch in self.bools.items():
            switch.set_active(bool(vals[key]))
        self.blacklist.set_text(str(vals["BLACKLIST"]))

    def on_reset(self, _btn):
        self.load(DEFAULTS)
        self.set_status("Defaults loaded - press Apply to save.")

    def collect(self):
        args = []
        for key, spin in self.floats.items():
            args.append(f"{key}={spin.get_value():g}")
        for key, switch in self.bools.items():
            args.append(f"{key}={'true' if switch.get_active() else 'false'}")
        args.append(f"BLACKLIST={self.blacklist.get_text().strip()}")
        return args

    def set_status(self, text):
        self.status.set_text(text)

    # ---- apply ----
    def on_apply(self, _btn):
        argv = ["pkexec"]
        apply = find_apply()
        if apply.endswith(".py"):
            argv += [sys.executable, apply]
        else:
            argv.append(apply)
        argv += self.collect()
        self.apply_btn.set_sensitive(False)
        self.set_status("Applying...")
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as err:
            self.apply_btn.set_sensitive(True)
            self.set_status(f"Could not run pkexec: {err.message}")
            return
        proc.communicate_utf8_async(None, None, self._applied, None)

    def _applied(self, proc, result, _data):
        self.apply_btn.set_sensitive(True)
        try:
            _ok, _out, err = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            self.set_status(f"Apply failed: {exc.message}")
            return
        if proc.get_exit_status() == 0:
            self.set_status("Applied and restarted midscroll.")
        else:
            detail = (err or "").strip().splitlines()
            msg = detail[-1] if detail else "authorization dismissed"
            self.set_status(f"Not applied: {msg}")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.gnhen.midscroll.Settings")

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

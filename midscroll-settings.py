#!/usr/bin/python3
"""midscroll-settings - a small GTK4 GUI for midscroll's tunables.

Reads the current /etc/midscroll.conf (world-readable), lets you change
every setting - including toggle mode and which devices count as a mouse -
with sliders, switches and a text box, and writes it back through pkexec
(midscroll-apply), restarting the daemon so the change takes effect
immediately. No terminal or config editing required.

This process is unprivileged and stays that way: it cannot open
/dev/input (those nodes are root's), so the device list is built from
/proc/bus/input/devices, which is world-readable, plus the by-id and
by-path symlink names. Nothing here talks to the daemon; the only
privileged step is handing validated KEY=VALUE arguments to
midscroll-apply through polkit.
"""

import os
import re
import struct
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

CONFIG_PATH = "/etc/midscroll.conf"
PROC_DEVICES = "/proc/bus/input/devices"
DEV_DIR = "/dev/input"
PHYS_MARKER = "midscroll"

# Kernel input codes (linux/input-event-codes.h), fixed ABI.
BTN_MIDDLE = 0x112
REL_X, REL_Y = 0x00, 0x01
ABS_X, ABS_MT_POSITION_X = 0x00, 0x35
KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER = 30, 44, 57, 28
KEYBOARD_KEYS = (KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER)
BITS_PER_WORD = struct.calcsize("l") * 8

# key, label, lower, upper, step, digits
FLOATS = [
    ("DEADZONE_PX", "Dead zone (px)", 0, 200, 1, 0),
    ("SPEED_MULT", "Speed multiplier", 0.001, 1.0, 0.001, 3),
    ("SPEED_EXP", "Speed curve exponent", 0.5, 4.0, 0.1, 1),
    ("MAX_PX_PER_SEC", "Max speed (px/s)", 1000, 200000, 500, 0),
    ("PX_PER_NOTCH", "Pixels per wheel notch", 1, 500, 1, 0),
    ("MAX_DRAG_PX", "Max drag distance (px)", 100, 10000, 10, 0),
    ("TICK_HZ", "Event rate (Hz)", 10, 360, 5, 0),
    ("GHOST_SCALE", "Ghost cursor travel", 0.1, 5.0, 0.1, 1),
    ("TOGGLE_HOLD_MS", "Toggle hold threshold (ms)", 0, 1000, 10, 0),
]
# key, label, subtitle
BOOLS = [
    ("TOGGLE_MODE", "Toggle mode",
     "Start autoscroll without holding throughout; any click stops it. "
     "Set a hold threshold below to preserve quick middle-clicks."),
    ("NATURAL", "Natural scrolling", "Invert the scroll direction."),
    ("GHOST_CURSOR", "Ghost cursor",
     "While you drag, draw a cursor that follows your hand from the "
     "anchor point. It only draws - the real pointer stays put so the "
     "scroll can't leak into another window."),
    ("DESKTOP_SCROLL", "Enable on desktop & panels",
     "Allow autoscroll while the desktop or a panel/taskbar is focused. "
     "Off by default so a middle-drag doesn't hijack the desktop."),
]
DEFAULTS = {
    "DEADZONE_PX": 15.0, "SPEED_MULT": 0.008, "SPEED_EXP": 2.2,
    "MAX_PX_PER_SEC": 30000.0, "PX_PER_NOTCH": 55.0, "MAX_DRAG_PX": 1200.0,
    "TICK_HZ": 90.0, "GHOST_SCALE": 1.0, "TOGGLE_HOLD_MS": 0.0,
    "NATURAL": False,
    "TOGGLE_MODE": False, "DESKTOP_SCROLL": False, "GHOST_CURSOR": True,
    "BLACKLIST": "freecad, orcaslicer, minecraft",
    "EXTRA_DEVICES": "", "IGNORE_DEVICES": "",
    # Not settable here: only a root edit of the config turns it on.
    "ALLOW_KEYBOARDS": False,
}
FLOAT_KEYS = [f[0] for f in FLOATS]
BOOL_KEYS = [b[0] for b in BOOLS]
DEVICE_KEYS = ["EXTRA_DEVICES", "IGNORE_DEVICES"]


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
        elif k in BOOL_KEYS or k == "ALLOW_KEYBOARDS":
            values[k] = v.lower() in ("1", "true", "yes", "on")
        elif k == "BLACKLIST" or k in DEVICE_KEYS:
            values[k] = v
    return values


def split_specs(text):
    """A config list value as a list of specs."""
    return [p.strip() for p in str(text).split(",") if p.strip()]


def _mask(value):
    """A /proc capability bitmap as one integer.

    The kernel prints these most-significant word first, one word per
    machine long, so pad each word out before joining them.
    """
    words = value.split()
    if not words:
        return 0
    return int("".join(w.zfill(BITS_PER_WORD // 4) for w in words), 16)


def _has(mask, bit):
    return bool(mask >> bit & 1)


def read_input_devices(path=PROC_DEVICES):
    """Every input device the kernel knows about, from /proc.

    One dict per device with the fields the daemon decides on: name,
    vendor/product, phys, its /dev/input/eventN node and the capability
    bits. midscroll's own uinput mirrors carry a known phys string and
    are left out - they aren't hardware and must never be selected.
    """
    try:
        with open(path) as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return []
    devices = []
    for block in blocks:
        rec = {"name": "", "vendor": 0, "product": 0, "phys": "",
               "event": "", "keys": 0, "rels": 0, "abss": 0}
        for line in block.splitlines():
            if line.startswith("I: "):
                ids = dict(p.split("=", 1) for p in line[3:].split()
                           if "=" in p)
                try:
                    rec["vendor"] = int(ids.get("Vendor", "0"), 16)
                    rec["product"] = int(ids.get("Product", "0"), 16)
                except ValueError:
                    pass
            elif line.startswith("N: Name="):
                rec["name"] = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("P: Phys="):
                rec["phys"] = line[len("P: Phys="):].strip()
            elif line.startswith("H: Handlers="):
                for handler in line[len("H: Handlers="):].split():
                    if handler.startswith("event"):
                        rec["event"] = f"{DEV_DIR}/{handler}"
            elif line.startswith("B: KEY="):
                rec["keys"] = _mask(line[len("B: KEY="):])
            elif line.startswith("B: REL="):
                rec["rels"] = _mask(line[len("B: REL="):])
            elif line.startswith("B: ABS="):
                rec["abss"] = _mask(line[len("B: ABS="):])
        if rec["event"] and PHYS_MARKER not in rec["phys"]:
            devices.append(rec)
    return devices


def is_mouse_record(rec):
    """The daemon's is_mouse() rule, applied to a /proc record."""
    return (_has(rec["keys"], BTN_MIDDLE)
            and _has(rec["rels"], REL_X)
            and _has(rec["rels"], REL_Y)
            and not _has(rec["abss"], ABS_X)
            and not _has(rec["abss"], ABS_MT_POSITION_X))


def is_keyboard_record(rec):
    """The daemon's is_keyboard() rule, applied to a /proc record."""
    return all(_has(rec["keys"], k) for k in KEYBOARD_KEYS)


def has_axes(rec):
    return bool(rec["rels"] or rec["abss"])


def stable_specs():
    """Map each /dev/input/eventN to its most stable identifier.

    Same preference as the daemon's --list-devices: the by-id name if
    there is one, else by-path, both of which survive reboots and
    renumbering where a bare eventN does not.
    """
    specs = {}
    for directory in (f"{DEV_DIR}/by-path", f"{DEV_DIR}/by-id"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in sorted(names):
            link = os.path.join(directory, name)
            target = os.path.realpath(link)
            if target.startswith(f"{DEV_DIR}/event"):
                specs[target] = link  # by-id listed last, so it wins
    return specs


def spec_for(rec, specs, devices):
    """The identifier to write for one device: stable forms first.

    A by-id or by-path link if the device has one, else its name if that
    is unambiguous and survives the config's character set (no commas or
    other separators), else the bare event node - which works but moves
    around between reboots.
    """
    if rec["event"] in specs:
        return specs[rec["event"]]
    name = rec["name"].strip()
    same = [d for d in devices if d["name"].strip() == name]
    if len(name) >= 3 and len(same) == 1 and re.match(r"^[\w./:\- ]+$", name):
        return name
    return rec["event"]


def spec_matches(rec, spec):
    """The daemon's device_matches() rule, applied to a /proc record."""
    if spec.startswith("/"):
        return os.path.realpath(spec) == rec["event"]
    if re.match(r"^[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}$", spec):
        vendor, product = spec.split(":")
        return (rec["vendor"] == int(vendor, 16)
                and rec["product"] == int(product, 16))
    return spec.lower() in rec["name"].strip().lower()


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
        self.set_default_size(520, -1)
        self.floats = {}
        self.bools = {}
        self.device_rows = []   # (record, spec, auto-detected, switch)
        self.extra = []         # EXTRA_DEVICES as loaded
        self.ignore = []        # IGNORE_DEVICES as loaded
        self.allow_keyboards = False

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

        box.append(self._devices_section())

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
            _min, nat, _mb, _nb = self.measure(Gtk.Orientation.VERTICAL, 520)
            self.set_default_size(520, min(nat, avail))
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

    def _devices_section(self):
        """The list of input devices, with a switch each."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = self._heading("Mice and devices")
        title.set_hexpand(True)
        head.append(title)
        rescan = Gtk.Button(label="Rescan")
        rescan.set_valign(Gtk.Align.CENTER)
        rescan.connect("clicked", lambda _b: self.rebuild_devices())
        head.append(rescan)
        box.append(head)

        hint = Gtk.Label(
            label="midscroll picks up ordinary mice by itself. Turn a "
                  "device on to use it anyway, or off to leave it alone. "
                  "Only a device with a middle button and relative axes "
                  "can actually autoscroll; anything you turn on here is "
                  "grabbed exclusively and re-emitted through a virtual "
                  "mirror.",
            wrap=True, xalign=0)
        hint.add_css_class("dim-label")
        box.append(hint)

        # The list lives in its own bounded, scrollable frame: a machine
        # with thirty input nodes shouldn't stretch the window to match.
        self.devices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=6)
        self.devices_box.set_margin_top(8)
        self.devices_box.set_margin_bottom(8)
        self.devices_box.set_margin_start(8)
        self.devices_box.set_margin_end(8)
        device_scroller = Gtk.ScrolledWindow()
        device_scroller.set_policy(Gtk.PolicyType.NEVER,
                                   Gtk.PolicyType.AUTOMATIC)
        device_scroller.set_min_content_height(240)
        device_scroller.set_max_content_height(240)
        device_scroller.set_propagate_natural_height(False)
        device_scroller.set_child(self.devices_box)
        frame = Gtk.Frame()
        frame.set_child(device_scroller)
        box.append(frame)

        self.show_all_devices = Gtk.CheckButton(
            label="Show all input devices")
        self.show_all_devices.connect("toggled",
                                      lambda _c: self.rebuild_devices())
        box.append(self.show_all_devices)
        return box

    def _device_row(self, rec, spec, auto, on, blocked):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(Gtk.Label(label=rec["name"].strip() or "(unnamed)",
                             xalign=0, wrap=True))
        if blocked:
            state = "keyboard - only a root edit of the config can allow it"
        elif auto:
            state = "detected as a mouse"
        else:
            state = "not detected as a mouse"
        sub = Gtk.Label(label=f"{rec['event']} - {state}\n{spec}",
                        xalign=0, wrap=True, selectable=True)
        sub.add_css_class("dim-label")
        col.append(sub)
        row.append(col)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.set_active(on)
        if blocked:
            switch.set_sensitive(False)
            switch.set_tooltip_text(
                "midscroll never grabs a keyboard unless ALLOW_KEYBOARDS "
                f"is set in {CONFIG_PATH}, which only root can do.")
        row.append(switch)
        self.device_rows.append((rec, spec, auto, switch))
        return row

    def _orphan_row(self, spec, in_extra):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(
            label=f"{spec}\n{'use as a mouse' if in_extra else 'never use'} "
                  "- not connected",
            xalign=0, hexpand=True, wrap=True)
        label.add_css_class("dim-label")
        row.append(label)
        drop = Gtk.Button(icon_name="list-remove-symbolic")
        drop.set_valign(Gtk.Align.CENTER)
        drop.set_tooltip_text("Forget this device")
        drop.connect("clicked", self.on_forget, spec, in_extra)
        row.append(drop)
        return row

    def on_forget(self, _btn, spec, in_extra):
        specs = self.extra if in_extra else self.ignore
        if spec in specs:
            specs.remove(spec)
        self.rebuild_devices()
        self.set_status(f"Removed {spec} - press Apply to save.")

    def rebuild_devices(self):
        """Redraw the device list from /proc and the current lists."""
        child = self.devices_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.devices_box.remove(child)
            child = nxt
        self.device_rows = []
        devices = read_input_devices()
        specs = stable_specs()
        show_all = self.show_all_devices.get_active()
        listed = set()
        for rec in devices:
            spec = spec_for(rec, specs, devices)
            auto = is_mouse_record(rec)
            named = [s for s in self.extra + self.ignore
                     if spec_matches(rec, s)]
            listed.update(named)
            if not (show_all or auto or has_axes(rec) or named):
                continue
            blocked = is_keyboard_record(rec) and not self.allow_keyboards
            on = (auto or any(spec_matches(rec, s) for s in self.extra))
            if any(spec_matches(rec, s) for s in self.ignore):
                on = False
            self.devices_box.append(
                self._device_row(rec, spec, auto, on and not blocked,
                                 blocked))
        # Specs for hardware that isn't plugged in stay in the config
        # until they are removed here, rather than vanishing silently.
        orphans = [(s, True) for s in self.extra if s not in listed]
        orphans += [(s, False) for s in self.ignore if s not in listed]
        if orphans:
            note = Gtk.Label(label="Set by hand, not connected right now:",
                             xalign=0)
            note.add_css_class("dim-label")
            self.devices_box.append(note)
            for spec, in_extra in orphans:
                self.devices_box.append(self._orphan_row(spec, in_extra))

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
        self.extra = split_specs(vals["EXTRA_DEVICES"])
        self.ignore = split_specs(vals["IGNORE_DEVICES"])
        # Read from the config, never written back from here.
        self.allow_keyboards = bool(vals["ALLOW_KEYBOARDS"])
        self.rebuild_devices()

    def on_reset(self, _btn):
        # Keep ALLOW_KEYBOARDS as it is: this window doesn't own it.
        values = dict(DEFAULTS)
        values["ALLOW_KEYBOARDS"] = self.allow_keyboards
        self.load(values)
        self.set_status("Defaults loaded - press Apply to save.")

    def device_lists(self):
        """(EXTRA_DEVICES, IGNORE_DEVICES) from the switches on screen.

        A switch only produces an entry when it disagrees with what
        midscroll would do on its own, so the lists stay as short as the
        change actually is. Specs whose device isn't connected are kept.
        """
        extra, ignore = [], []
        seen = set()
        for rec, spec, auto, switch in self.device_rows:
            on = switch.get_active()
            if on and not auto:
                extra.append(spec)
            elif auto and not on:
                ignore.append(spec)
            for s in self.extra + self.ignore:
                if spec_matches(rec, s):
                    seen.add(s)
        extra += [s for s in self.extra if s not in seen]
        ignore += [s for s in self.ignore if s not in seen]
        return extra, ignore

    def collect(self):
        args = []
        for key, spin in self.floats.items():
            args.append(f"{key}={spin.get_value():g}")
        for key, switch in self.bools.items():
            args.append(f"{key}={'true' if switch.get_active() else 'false'}")
        args.append(f"BLACKLIST={self.blacklist.get_text().strip()}")
        extra, ignore = self.device_lists()
        args.append("EXTRA_DEVICES=" + ", ".join(extra))
        args.append("IGNORE_DEVICES=" + ", ".join(ignore))
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

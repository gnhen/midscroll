#!/usr/bin/python3
"""midscroll-apply - write /etc/midscroll.conf and restart the daemon.

Run as root, normally through pkexec from the settings GUI. Takes the
tunables as KEY=VALUE arguments, validates every one (so an untrusted
caller can only ever produce a well-formed midscroll config, never write
arbitrary text to /etc), regenerates a clean commented config file and
restarts midscroll.service so the change takes effect.

    midscroll-apply DEADZONE_PX=15 SPEED_MULT=0.008 ... TOGGLE_MODE=false

Pass --no-restart to only write the file.

One key is deliberately out of reach here: ALLOW_KEYBOARDS, which lets the
daemon grab keyboard-class devices. This program is reachable by anyone
who can pass its polkit check, so it refuses to set that key and only
carries an existing setting through - turning it on takes a root edit of
the config file. See SECURITY.md.
"""

import math
import os
import re
import subprocess
import sys
import tempfile

CONFIG_PATH = "/etc/midscroll.conf"

# Bounds on our own argv: it comes from an unprivileged caller.
MAX_ARGS = 64
MAX_ARG_LEN = 4096
# Bounds on device specs, matching the daemon's.
MAX_SPECS = 32
MAX_SPEC_LEN = 128
MAX_SPECS_LEN = 1024
# Never settable from here, only preserved.
ROOT_ONLY_KEYS = ("ALLOW_KEYBOARDS",)

# Float tunables and whether they must be strictly positive (a zero dead
# zone is fine; a zero rate or multiplier would break the daemon).
FLOATS = {
    "DEADZONE_PX": False,
    "SPEED_MULT": True,
    "SPEED_EXP": True,
    "MAX_PX_PER_SEC": True,
    "PX_PER_NOTCH": True,
    "MAX_DRAG_PX": True,
    "TICK_HZ": True,
    "GHOST_SCALE": True,
    "TOGGLE_HOLD_MS": False,
}
BOOLS = ("NATURAL", "TOGGLE_MODE", "DESKTOP_SCROLL", "GHOST_CURSOR")
DEVICES = ("EXTRA_DEVICES", "IGNORE_DEVICES")
KEYS = tuple(FLOATS) + BOOLS + DEVICES + ("BLACKLIST",)

# Defaults for any key the caller omits.
DEFAULTS = {
    "DEADZONE_PX": 15.0, "SPEED_MULT": 0.008, "SPEED_EXP": 2.2,
    "MAX_PX_PER_SEC": 30000.0, "PX_PER_NOTCH": 55.0, "MAX_DRAG_PX": 1200.0,
    "TICK_HZ": 90.0, "GHOST_SCALE": 1.0, "TOGGLE_HOLD_MS": 0.0,
    "NATURAL": False,
    "TOGGLE_MODE": False, "DESKTOP_SCROLL": False, "GHOST_CURSOR": True,
    "BLACKLIST": "freecad, orcaslicer, minecraft",
    "EXTRA_DEVICES": "", "IGNORE_DEVICES": "",
}

TEMPLATE = """\
# midscroll tuning - edit here or use the midscroll-settings GUI, then:
#   sudo systemctl restart midscroll
#
# Speed curve is Chromium/Edge's Windows autoscroll formula:
#   speed (px/sec) = SPEED_MULT * (pixels dragged) ^ SPEED_EXP   per axis

DEADZONE_PX = {DEADZONE_PX:g}          # per-axis dead zone in pixels
SPEED_MULT = {SPEED_MULT:g}        # overall speed knob (bigger = faster)
SPEED_EXP = {SPEED_EXP:g}           # curve shape (bigger = more extreme far out)
MAX_PX_PER_SEC = {MAX_PX_PER_SEC:g}    # safety cap
PX_PER_NOTCH = {PX_PER_NOTCH:g}         # px one wheel notch scrolls in your apps
MAX_DRAG_PX = {MAX_DRAG_PX:g}        # max effective drag distance
TICK_HZ = {TICK_HZ:g}              # scroll smoothness (event rate)
NATURAL = {NATURAL}           # true = inverted / touchscreen-style direction

# Click the middle button once to start autoscroll and again (or any click)
# to stop, Windows-Explorer style, instead of holding it and dragging.
# Set TOGGLE_HOLD_MS above zero to keep quick presses as native middle clicks.
TOGGLE_MODE = {TOGGLE_MODE}
TOGGLE_HOLD_MS = {TOGGLE_HOLD_MS:g}

# Autoscroll over the desktop and panels too. Off by default, so a middle-drag
# on the desktop/taskbar (plasmashell, xfdesktop, waybar, ...) is left alone.
DESKTOP_SCROLL = {DESKTOP_SCROLL}

# While you drag, the real pointer stays anchored so the scroll can't leak to
# another window, and the session helper draws a cursor that follows your hand.
# GHOST_SCALE is how far that drawn cursor travels per unit of mouse motion.
GHOST_CURSOR = {GHOST_CURSOR}
GHOST_SCALE = {GHOST_SCALE:g}

# Apps that use middle-drag themselves: while one of these is the focused
# window, midscroll pauses. Comma-separated, case-insensitive window-class
# substrings; leave empty to disable.
BLACKLIST = {BLACKLIST}

# Devices to use as a mouse even though midscroll doesn't detect one, and
# devices never to use even if it does. Comma-separated; each entry is a
# /dev/input path (the by-id and by-path links are the stable ones), a hex
# vendor:product, or part of the device name. `midscroll --list-devices'
# prints all three for every device.
EXTRA_DEVICES = {EXTRA_DEVICES}
IGNORE_DEVICES = {IGNORE_DEVICES}
{ALLOW_KEYBOARDS}"""

# Appended only when it is already on: midscroll-apply never sets it.
KEYBOARDS_BLOCK = """
# Lets midscroll grab keyboard-class devices listed in EXTRA_DEVICES. This
# means a root daemon reads every key on them, and they lose key repeat and
# LEDs on the way through its virtual mirror. The settings GUI cannot set
# this; it is only ever changed by editing this file as root.
ALLOW_KEYBOARDS = true
"""


def die(msg):
    sys.stderr.write(f"midscroll-apply: {msg}\n")
    sys.exit(2)


def parse_bool(text):
    t = text.strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {text!r}")


def clean_list(val, charset, max_len, max_parts, total):
    """A comma list normalised to a bounded, well-formed config value.

    Anything that isn't a plausible character is dropped - including '#',
    '=' and newlines, which the daemon's parser would read as structure -
    so no argument can turn into extra config lines, and the length caps
    stop a caller pushing something huge into a root-written file.
    """
    parts = [re.sub(charset, "", p.strip(), flags=re.ASCII)[:max_len]
             for p in val.replace("\n", ",").split(",")]
    kept = [p for p in parts if p][:max_parts]
    return ", ".join(kept)[:total]


def keyboards_enabled(path=CONFIG_PATH):
    """Whether ALLOW_KEYBOARDS is already on in a trustworthy config.

    Read the way the daemon reads it (no symlink, root-owned, not writable
    by anyone else); anything else is not carried forward, because the
    daemon wouldn't honour it either.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    with os.fdopen(fd, "r", errors="replace") as f:
        st = os.fstat(f.fileno())
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False
        for line in f.read(65536).splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, val = (p.strip() for p in line.split("=", 1))
            if key == "ALLOW_KEYBOARDS":
                return val.lower() in ("1", "true", "yes", "on")
    return False


def main(argv):
    os.umask(0o077)
    if len(argv) > MAX_ARGS:
        die(f"too many arguments (limit {MAX_ARGS})")
    for arg in argv:
        if len(arg) > MAX_ARG_LEN:
            die(f"argument longer than {MAX_ARG_LEN} characters")
    restart = True
    values = dict(DEFAULTS)
    for arg in argv:
        if arg == "--no-restart":
            restart = False
            continue
        if "=" not in arg:
            die(f"expected KEY=VALUE, got {arg!r}")
        key, val = arg.split("=", 1)
        key = key.strip()
        if key in ROOT_ONLY_KEYS:
            die(f"{key} can only be changed by editing {CONFIG_PATH} as root")
        if key not in KEYS:
            die(f"unknown key {key!r}")
        if key in FLOATS:
            try:
                num = float(val)
            except ValueError:
                die(f"{key}: not a number: {val!r}")
            if not math.isfinite(num):
                die(f"{key}: must be finite")
            if FLOATS[key] and num <= 0:
                die(f"{key}: must be strictly greater than zero")
            if num < 0:
                die(f"{key}: must not be negative")
            values[key] = num
        elif key in BOOLS:
            try:
                values[key] = parse_bool(val)
            except ValueError as err:
                die(f"{key}: {err}")
        elif key in DEVICES:
            # Device specs need '/' and ':' as well, for /dev/input paths
            # and vendor:product; the daemon re-validates every entry.
            values[key] = clean_list(val, r"[^\w./:\- ]", MAX_SPEC_LEN,
                                     MAX_SPECS, MAX_SPECS_LEN)
        else:  # BLACKLIST
            values[key] = clean_list(val, r"[^\w.\- ]", 64, 32, 512)

    text = TEMPLATE.format(
        NATURAL="true" if values["NATURAL"] else "false",
        TOGGLE_MODE="true" if values["TOGGLE_MODE"] else "false",
        DESKTOP_SCROLL="true" if values["DESKTOP_SCROLL"] else "false",
        GHOST_CURSOR="true" if values["GHOST_CURSOR"] else "false",
        BLACKLIST=values["BLACKLIST"],
        EXTRA_DEVICES=values["EXTRA_DEVICES"],
        IGNORE_DEVICES=values["IGNORE_DEVICES"],
        # Carried through untouched: this program can't turn it on or off.
        ALLOW_KEYBOARDS=KEYBOARDS_BLOCK if keyboards_enabled() else "",
        **{k: values[k] for k in FLOATS},
    )

    directory = os.path.dirname(CONFIG_PATH) or "/"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".midscroll.conf.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, CONFIG_PATH)
    except OSError as err:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die(f"cannot write {CONFIG_PATH}: {err}")

    print(f"wrote {CONFIG_PATH}")
    if restart:
        try:
            subprocess.run(["systemctl", "restart", "midscroll.service"],
                           check=True)
        except (OSError, subprocess.CalledProcessError) as err:
            die(f"restart failed: {err}")
        print("restarted midscroll.service")


if __name__ == "__main__":
    main(sys.argv[1:])

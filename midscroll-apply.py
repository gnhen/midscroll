#!/usr/bin/python3
"""midscroll-apply - write /etc/midscroll.conf and restart the daemon.

Run as root, normally through pkexec from the settings GUI. Takes the
tunables as KEY=VALUE arguments, validates every one (so an untrusted
caller can only ever produce a well-formed midscroll config, never write
arbitrary text to /etc), regenerates a clean commented config file and
restarts midscroll.service so the change takes effect.

    midscroll-apply DEADZONE_PX=15 SPEED_MULT=0.008 ... TOGGLE_MODE=false

Pass --no-restart to only write the file.
"""

import math
import os
import re
import subprocess
import sys
import tempfile

CONFIG_PATH = "/etc/midscroll.conf"

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
}
BOOLS = ("NATURAL", "TOGGLE_MODE", "DESKTOP_SCROLL", "FREE_CURSOR")
KEYS = tuple(FLOATS) + BOOLS + ("BLACKLIST",)

# Defaults for any key the caller omits.
DEFAULTS = {
    "DEADZONE_PX": 15.0, "SPEED_MULT": 0.008, "SPEED_EXP": 2.2,
    "MAX_PX_PER_SEC": 30000.0, "PX_PER_NOTCH": 55.0, "MAX_DRAG_PX": 1200.0,
    "TICK_HZ": 90.0, "NATURAL": False, "TOGGLE_MODE": False,
    "DESKTOP_SCROLL": False, "FREE_CURSOR": False,
    "BLACKLIST": "freecad, orcaslicer, minecraft",
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
TOGGLE_MODE = {TOGGLE_MODE}

# Autoscroll over the desktop and panels too. Off by default, so a middle-drag
# on the desktop/taskbar (plasmashell, xfdesktop, waybar, ...) is left alone.
DESKTOP_SCROLL = {DESKTOP_SCROLL}

# Let the cursor move freely during a drag-scroll. Off by default: the cursor
# is anchored so the scroll stays on the window you started in. With this on
# the cursor follows the hand, but once it leaves the original window the
# scroll jumps to whatever is under it.
FREE_CURSOR = {FREE_CURSOR}

# Apps that use middle-drag themselves: while one of these is the focused
# window, midscroll pauses. Comma-separated, case-insensitive window-class
# substrings; leave empty to disable.
BLACKLIST = {BLACKLIST}
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


def main(argv):
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
        else:  # BLACKLIST
            # Normalise to a clean, bounded comma list. Strip anything that
            # isn't a plausible window-class character (drops '#', which the
            # daemon's parser would treat as a comment, plus control chars),
            # and cap per-part and total length so a caller can't push a
            # huge or malformed value into the root-written config.
            parts = [re.sub(r"[^\w.\- ]", "", p.strip(), flags=re.ASCII)[:64]
                     for p in val.replace("\n", ",").split(",")]
            values[key] = ", ".join(p for p in parts if p)[:512]

    text = TEMPLATE.format(
        NATURAL="true" if values["NATURAL"] else "false",
        TOGGLE_MODE="true" if values["TOGGLE_MODE"] else "false",
        DESKTOP_SCROLL="true" if values["DESKTOP_SCROLL"] else "false",
        FREE_CURSOR="true" if values["FREE_CURSOR"] else "false",
        BLACKLIST=values["BLACKLIST"],
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

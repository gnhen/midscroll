#!/usr/bin/python3
"""midscroll - Windows-style middle-button drag autoscroll for Linux.

Hold the middle mouse button and drag: the page scrolls in that direction,
and the farther you drag from the point where you pressed, the faster it
scrolls. Release to stop. A quick middle click without dragging passes
through as a normal middle click (paste / open link in new tab).

With TOGGLE_MODE enabled the interaction is instead the Windows-Explorer /
Firefox style: a single middle click starts autoscroll, the cursor then
moves freely and the page scrolls by its distance from that origin, and any
mouse click stops it. (In that mode the middle button no longer pastes.)

Works on Wayland and X11 in any application, because it sits at the kernel
input layer: it grabs the real mouse and re-emits its events through a
per-mouse uinput mirror, injecting high-resolution wheel events while a
middle-drag is active. Each mirror copies its source mouse's name and
vendor/product IDs, so libinput/KDE keep applying that mouse's own
pointer-speed and acceleration settings instead of reverting to defaults.

Apps that use middle-drag themselves (CAD, slicers, games) can be
blacklisted by window class; while one of them is focused, the middle
button passes straight through. The focused window's class is reported by
the session helper (midscroll-overlay) over the state socket.

Tunables can be overridden in /etc/midscroll.conf (KEY = value lines) or
per run on the command line: midscroll --help.
"""

import argparse
import asyncio
import logging
import math
import os
import socket
import struct
import time

from evdev import InputDevice, UInput, ecodes as e, list_devices

VERSION = "1.12"

log = logging.getLogger("midscroll")

# ---- Tuning (override in /etc/midscroll.conf or via CLI) ------------------
# Speed curve from Chromium/Edge's autoscroll:
#   velocity_px_per_sec = SPEED_MULT * |offset_px| ^ SPEED_EXP   (per axis)
# with a 15 px per-axis dead zone. Chromium uses 0.000008 * d^2.2 in px/ms;
# SPEED_MULT is that times 1000.
DEADZONE_PX = 15.0        # per-axis dead zone, as in Chromium
SPEED_MULT = 0.008        # px/sec multiplier, overall speed
SPEED_EXP = 2.2           # exponent: slow near the press point, fast far
MAX_PX_PER_SEC = 30000.0  # safety cap on scroll speed
PX_PER_NOTCH = 55.0       # how many px one wheel notch scrolls in your apps
MAX_DRAG_PX = 1200.0      # cap on effective drag distance (~screen height)
TICK_HZ = 90.0            # scroll event rate (higher = smoother)
NATURAL = False           # True inverts scroll direction
TOGGLE_MODE = False       # True: click to start/stop instead of hold-and-drag
DESKTOP_SCROLL = False    # True: also autoscroll over the desktop and panels
FREE_CURSOR = False       # True lets the cursor move freely during a drag-scroll

# Window-class substrings (case-insensitive) over which midscroll pauses
# and the middle button behaves natively.
BLACKLIST = ["freecad", "orcaslicer", "minecraft"]

# Desktop-environment shells (desktop, panels, taskbars) — autoscroll over
# these is almost never wanted (on KDE it switches virtual desktops; panels
# have nothing to scroll). Blocked exactly like BLACKLIST, but only while
# DESKTOP_SCROLL is off (the default). Lowercase, to match parse_blacklist.
DESKTOP_SHELLS = ["plasmashell", "nemo-desktop", "xfdesktop", "waybar",
                  "xfce4-panel", "org.gnome.shell", "gjs"]

HIRES_PER_LINE = 120      # kernel convention: 120 hi-res units per notch
MAX_TICK_DT = 0.25        # cap the per-tick time step (see ticker())
PHYS_MARKER = "midscroll"  # phys string on our mirrors, so we skip our own
CONFIG_PATH = "/etc/midscroll.conf"
SOCK_DIR = "/run/midscroll"
SOCK_PATH = SOCK_DIR + "/state.sock"

FLOAT_KEYS = {"DEADZONE_PX", "TICK_HZ", "SPEED_MULT", "SPEED_EXP",
              "MAX_PX_PER_SEC", "PX_PER_NOTCH", "MAX_DRAG_PX"}
BOOL_KEYS = {"NATURAL", "TOGGLE_MODE", "DESKTOP_SCROLL", "FREE_CURSOR"}
# Zero would divide by zero (TICK_HZ, PX_PER_NOTCH) or make the daemon
# silently never scroll; only the dead zone may be zero.
POSITIVE_KEYS = FLOAT_KEYS - {"DEADZONE_PX"}


def parse_bool(text):
    return text.strip().lower() in ("1", "true", "yes", "on")


def validate(key, val):
    """Return an error string if val is out of bounds for key, else None."""
    if not math.isfinite(val):
        return "must be a finite number"
    if key in POSITIVE_KEYS and val <= 0:
        return "must be strictly greater than zero"
    if val < 0:
        return "must not be negative"
    return None


def parse_blacklist(text):
    return [p.strip().lower() for p in text.split(",") if p.strip()]


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k in FLOAT_KEYS:
            try:
                val = float(v)
            except ValueError:
                log.error("config: bad value for %s: %r (keeping %g)",
                          k, v, globals()[k])
                continue
            err = validate(k, val)
            if err:
                log.error("config: %s = %g rejected: %s (keeping %g)",
                          k, val, err, globals()[k])
                continue
            globals()[k] = val
        elif k in BOOL_KEYS:
            globals()[k] = parse_bool(v)
        elif k == "BLACKLIST":
            globals()["BLACKLIST"] = parse_blacklist(v)
        else:
            log.warning("config: unknown key %s", k)


def _peer_uid(sock):
    """UID of the process on the other end of a unix socket, or None."""
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except OSError:
        return None


def _uid_has_active_seat(uid):
    """True if uid is a user logged in on a seat (not a service account).

    logind writes /run/systemd/users/<uid> with a STATE line for each
    logged-in user. We only trust focus reports from such a user, so a
    random local process (sandboxed app, service account) can't feed the
    daemon and pause it. Without logind nobody is trusted and the app
    blacklist is simply inactive.
    """
    try:
        with open(f"/run/systemd/users/{uid}") as f:
            for line in f:
                if line.startswith("STATE="):
                    return line.strip()[len("STATE="):] in ("active", "online")
    except OSError:
        pass
    return False


class FocusFilter:
    """The focused window class reported by each connected helper.

    The root daemon can't see session windows itself, so every
    midscroll-overlay reports its focused window's class. We keep one
    entry per helper and pause midscroll when any of them has a
    blacklisted app focused. Keeping them separate means one helper
    disconnecting can't wipe another's report.
    """

    def __init__(self):
        self.by_client = {}

    def update(self, client, wclass):
        self.by_client[client] = wclass
        log.debug("focus: %r (blocked=%s)", wclass, self.blocked)

    def remove(self, client):
        self.by_client.pop(client, None)

    @property
    def blocked(self):
        blockers = BLACKLIST if DESKTOP_SCROLL else BLACKLIST + DESKTOP_SHELLS
        for wclass in self.by_client.values():
            c = wclass.lower()
            if any(b in c for b in blockers):
                return True
        return False


class Notifier:
    """State socket shared with session helpers (midscroll-overlay).

    Sends b"1\\n" when a drag-scroll starts and b"0\\n" when it stops (and
    the current state on connect) so the helper can draw the badge; reads
    "focus <window class>" lines back to drive the blacklist. Only helpers
    running as a logged-in user are accepted (see _uid_has_active_seat);
    everything else is dropped, so a stray local process can't pause the
    daemon. Purely advisory otherwise: scrolling works with no listeners,
    and failing to bind the socket is non-fatal.
    """

    def __init__(self, focus):
        self.focus = focus
        self.writers = set()
        self.msg = b"0\n"
        self.server = None

    async def start(self):
        try:
            os.makedirs(SOCK_DIR, exist_ok=True)
            try:
                os.unlink(SOCK_PATH)
            except FileNotFoundError:
                pass
            self.server = await asyncio.start_unix_server(
                self._client, SOCK_PATH)
            # World-accessible so any session's helper can connect; each
            # connection is then checked by peer UID before we trust it.
            os.chmod(SOCK_PATH, 0o666)
        except OSError as err:
            log.warning("overlay socket unavailable: %s", err)

    async def _client(self, reader, writer):
        uid = _peer_uid(writer.get_extra_info("socket"))
        if uid is None or not _uid_has_active_seat(uid):
            log.debug("rejected socket from uid %s", uid)
            writer.close()
            return
        client = object()  # identity key for this connection's focus report
        self.writers.add(writer)
        try:
            writer.write(self.msg)
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("focus "):
                    self.focus.update(client, text[len("focus "):])
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.writers.discard(writer)
            self.focus.remove(client)
            writer.close()

    def set(self, active):
        msg = b"1\n" if active else b"0\n"
        if msg == self.msg:
            return
        self.msg = msg
        for w in list(self.writers):
            try:
                w.write(msg)
            except (OSError, ConnectionError):
                self.writers.discard(w)


class State:
    """Scroll session for one grabbed mouse.

    ``ui`` is that mouse's own uinput mirror: scroll events for this session
    are injected through it, and it carries the source mouse's identity so
    the compositor keeps the mouse's per-device pointer settings.
    """

    def __init__(self, ui):
        self.ui = ui
        self.reset()

    def reset(self):
        self.pending = False      # middle held, deadzone not yet exceeded
        self.active = False       # hold-drag scrolling
        self.toggled = False      # toggle-mode scrolling (no button held)
        self.passthrough = False  # middle held over a blacklisted app
        self.eat_release = None   # button whose release to swallow (toggle)
        self.dx = 0.0             # cursor offset from the origin
        self.dy = 0.0
        self.acc_v = 0.0          # fractional hi-res units carried over
        self.acc_h = 0.0
        self.notch_v = 0.0        # hi-res units accumulated toward a notch
        self.notch_h = 0.0

    @property
    def scrolling(self):
        """True whenever scroll events should be emitted this tick."""
        return self.active or self.toggled

    def press(self):
        self.reset()
        self.pending = True

    def begin_toggle(self):
        """Start toggle-mode autoscroll, anchored at the current point."""
        self.reset()
        self.toggled = True

    def release(self):
        """Return True if this was a plain click (no drag)."""
        was_click = self.pending
        self.reset()
        return was_click


def _clamp(v):
    # The cursor is anchored during a drag, so nothing bounds the drag
    # distance on its own; cap it the way Windows caps the cursor at the
    # screen edge, so slowing down never needs a huge reverse drag.
    return math.copysign(min(abs(v), MAX_DRAG_PX), v)


def _is_button(code):
    """True for a mouse button code (BTN_LEFT/RIGHT/MIDDLE/SIDE/...)."""
    return e.BTN_MOUSE <= code < e.BTN_JOYSTICK


def _accumulate(st, ev):
    """Fold a REL_X/REL_Y delta into the offset from the scroll origin."""
    if ev.code == e.REL_X:
        st.dx = _clamp(st.dx + ev.value)
    else:
        st.dy = _clamp(st.dy + ev.value)


def speed_px(offset):
    """Pixels/second for a per-axis offset from the press point.

    Inside the dead zone the speed is zero; outside it grows as
    offset^2.2, so small drags crawl and full-screen drags fly.
    """
    if abs(offset) <= DEADZONE_PX:
        return 0.0
    v = SPEED_MULT * abs(offset) ** SPEED_EXP
    return math.copysign(min(v, MAX_PX_PER_SEC), offset)


async def ticker(states, notifier, focus):
    # Measure the real time between ticks instead of assuming 1/TICK_HZ:
    # under load asyncio.sleep overshoots, and using the nominal step
    # would quietly make scrolling slower than configured.
    last = time.monotonic()
    while True:
        await asyncio.sleep(1.0 / TICK_HZ)
        now = time.monotonic()
        dt = min(now - last, MAX_TICK_DT)  # cap so a stall can't lurch
        last = now
        for st in list(states.values()):
            try:
                tick(st, dt, focus)
            except Exception as err:  # one bad tick must not kill the loop
                log.error("tick error: %r", err)
        notifier.set(any(st.scrolling for st in states.values()))


def tick(st, dt, focus):
    if focus.blocked:
        # A blacklisted app is focused. Never scroll, and stop anything
        # already running (focus changed mid-scroll) so it can't keep
        # scrolling under the blacklisted app.
        if st.scrolling or st.pending:
            st.reset()
        return
    if (not TOGGLE_MODE and st.pending
            and (abs(st.dx) > DEADZONE_PX or abs(st.dy) > DEADZONE_PX)):
        st.pending = False
        st.active = True
        log.debug("scroll started")
    if not st.scrolling:
        return
    ui = st.ui
    s = 1 if NATURAL else -1
    to_hires = HIRES_PER_LINE / PX_PER_NOTCH
    # Drag down => wheel-down (negative REL_WHEEL); drag right => positive
    # REL_HWHEEL. Both axes at once allows diagonal panning.
    st.acc_v += s * speed_px(st.dy) * to_hires * dt
    st.acc_h += -s * speed_px(st.dx) * to_hires * dt
    wrote = False
    iv = int(st.acc_v)
    if iv:
        st.acc_v -= iv
        st.notch_v += iv
        ui.write(e.EV_REL, e.REL_WHEEL_HI_RES, iv)
        n = int(st.notch_v / HIRES_PER_LINE)
        if n:
            st.notch_v -= n * HIRES_PER_LINE
            ui.write(e.EV_REL, e.REL_WHEEL, n)
        wrote = True
    ih = int(st.acc_h)
    if ih:
        st.acc_h -= ih
        st.notch_h += ih
        ui.write(e.EV_REL, e.REL_HWHEEL_HI_RES, ih)
        n = int(st.notch_h / HIRES_PER_LINE)
        if n:
            st.notch_h -= n * HIRES_PER_LINE
            ui.write(e.EV_REL, e.REL_HWHEEL, n)
        wrote = True
    if wrote:
        ui.syn()


def _resync(ui, dev, held, st):
    """Release any virtual button the real device no longer holds down.

    Called after SYN_DROPPED, where the kernel dropped events on us and a
    button release may be among them. We compare the buttons we're holding
    on the virtual device against the real device's current state and let
    go of the difference, so a dropped release can't leave a stuck button.
    """
    active = dev.active_keys()
    changed = False
    for code in list(held):
        if code not in active:
            ui.write(e.EV_KEY, code, 0)
            held.discard(code)
            changed = True
    if e.BTN_MIDDLE not in active:
        if st.passthrough:
            st.passthrough = False
            ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)  # we did press it through
            changed = True
        elif st.pending or st.active:
            st.release()  # drag button never went to the virtual device
    if changed:
        ui.syn()


def make_uinput(dev):
    """A uinput mirror that carries the source mouse's identity.

    We grab the physical mouse and re-emit its events through this virtual
    device, so the compositor sees the mirror - not the real mouse - as the
    pointer. Copying the source's name and vendor/product/version lets
    libinput and KDE match the user's existing per-device settings (pointer
    speed, acceleration profile) to the mirror, rather than treating it as a
    brand-new device and falling back to defaults. A distinctive phys string
    lets us recognise and skip our own mirrors during hotplug.
    """
    caps = dev.capabilities(absinfo=False)
    keys = set(caps.get(e.EV_KEY, ()))
    keys |= set(range(e.BTN_MOUSE, e.BTN_JOYSTICK))  # all mouse button codes
    rels = set(caps.get(e.EV_REL, ()))
    rels |= {e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL,
             e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES}  # codes we inject
    info = dev.info
    return UInput(
        {e.EV_KEY: sorted(keys), e.EV_REL: sorted(rels)},
        name=dev.name,
        vendor=info.vendor,
        product=info.product,
        version=info.version,
        bustype=info.bustype,
        phys=PHYS_MARKER,
    )


def phys_roundtrips():
    """True if a uinput device reports back the phys string we set on it.

    is_mouse() skips our own mirrors by their PHYS_MARKER, with the tracked
    device paths (our_paths) as backup. On a kernel where the phys string
    doesn't survive, is_mouse can't tell a mirror apart, so the operator
    should know the path tracking is doing all the work.
    """
    try:
        probe = UInput({e.EV_REL: [e.REL_X, e.REL_Y]},
                       name="midscroll self-check", phys=PHYS_MARKER)
    except OSError as err:
        log.warning("phys self-check: cannot open uinput: %s", err)
        return False
    try:
        dev = probe.device
        phys = (getattr(dev, "phys", "") or "") if dev else ""
        return PHYS_MARKER in phys
    finally:
        probe.close()


def _toggle_key(ev, st, ui, focus):
    """Handle a mouse-button event in toggle mode.

    Windows-Explorer style: a middle click starts autoscroll, then any
    click stops it. Returns True if the event was consumed (swallowed),
    False if it should be forwarded like a normal button press.
    """
    code = ev.code
    # Finish swallowing the click that stopped autoscroll: eat its release
    # so the app underneath never sees the stopping click.
    if ev.value == 0 and st.eat_release == code:
        st.eat_release = None
        return True
    if st.toggled:
        # Autoscroll is running: any button press stops it, consumed so the
        # click doesn't also land in whatever is under the cursor.
        if ev.value == 1:
            log.debug("toggle scroll stopped")
            st.reset()
            st.eat_release = code
        return True
    # Idle: only the middle button starts autoscroll.
    if code == e.BTN_MIDDLE:
        if ev.value == 1:
            if focus.blocked:
                st.passthrough = True
                ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                ui.syn()
            else:
                st.pending = True
        elif ev.value == 0:
            if st.passthrough:
                st.passthrough = False
                ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                ui.syn()
            elif st.pending:
                st.pending = False
                st.begin_toggle()
                log.debug("toggle scroll started")
        return True
    return False  # other buttons while idle pass straight through


async def pump(path, dev, states, tasks, focus, our_paths):
    """Grab one mouse and forward its events, intercepting middle-drags."""
    # Belt to is_mouse's phys check and main's our_paths skip: never pump one
    # of our own mirrors. Guards the brief hotplug window between a mirror
    # appearing and our_paths learning its path, so a phys hiccup can't drop
    # a root process into a 90 Hz input feedback loop.
    if PHYS_MARKER in (dev.phys or "") or path in our_paths:
        log.warning("refusing to grab our own mirror %s (%s)", dev.name, path)
        dev.close()
        tasks.pop(path, None)
        return
    try:
        dev.grab()
    except OSError as err:
        log.warning("cannot grab %s: %s", path, err)
        dev.close()
        tasks.pop(path, None)
        return
    try:
        ui = make_uinput(dev)
    except OSError as err:
        log.warning("cannot mirror %s: %s", dev.name, err)
        dev.close()
        tasks.pop(path, None)
        return
    mirror_path = ui.device.path if ui.device else None
    if mirror_path:
        our_paths.add(mirror_path)
    st = states[path] = State(ui)
    held = set()  # non-middle buttons we're currently holding down virtually
    log.info("grabbed %s (%s)", dev.name, path)
    try:
        async for ev in dev.async_read_loop():
            if TOGGLE_MODE and ev.type == e.EV_KEY and _is_button(ev.code):
                if _toggle_key(ev, st, ui, focus):
                    continue
                # An unrelated button while idle: fall through and forward it.
            elif ev.type == e.EV_KEY and ev.code == e.BTN_MIDDLE:
                if ev.value == 1:
                    if focus.blocked:
                        # A blacklisted app owns middle-drag; pass the
                        # button straight through, held state and all.
                        st.passthrough = True
                        log.debug("middle press passed through (%r focused)",
                                  next(iter(focus.by_client.values()), ""))
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                    else:
                        st.press()
                elif ev.value == 0:
                    if st.passthrough:
                        st.passthrough = False
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                    elif st.release():
                        # No drag happened: replay as a normal middle click.
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                        ui.syn()
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                        ui.syn()
                continue
            if ev.type == e.EV_REL and ev.code in (e.REL_X, e.REL_Y):
                if st.toggled:
                    # Toggle mode: track distance from the origin but let the
                    # motion through, so the cursor follows the hand like
                    # Windows autoscroll.
                    _accumulate(st, ev)
                    ui.write(ev.type, ev.code, ev.value)
                    continue
                if st.pending or st.active:
                    # Hold-drag: swallow cursor motion so the pointer stays
                    # anchored at the press point. Scroll events then keep
                    # hitting the original window instead of whatever the
                    # cursor would have drifted over.
                    _accumulate(st, ev)
                    if not FREE_CURSOR:
                        continue
            if ev.type == e.EV_SYN:
                if ev.code == e.SYN_DROPPED:
                    _resync(ui, dev, held, st)
                else:
                    ui.syn()
            elif ev.type in (e.EV_KEY, e.EV_REL):
                if ev.type == e.EV_KEY:
                    if ev.value == 1:
                        held.add(ev.code)
                    elif ev.value == 0:
                        held.discard(ev.code)
                ui.write(ev.type, ev.code, ev.value)
    except OSError:
        pass  # device unplugged
    finally:
        # Don't leave a button stuck down on the virtual device if the real
        # one vanished mid-press.
        if held or st.passthrough:
            for code in held:
                ui.write(e.EV_KEY, code, 0)
            if st.passthrough:
                ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
            ui.syn()
        st.reset()
        if mirror_path:
            our_paths.discard(mirror_path)
        try:
            ui.close()
        except OSError:
            pass
        try:
            dev.close()
        except OSError:
            pass
        states.pop(path, None)
        tasks.pop(path, None)
        log.info("released %s", path)


def is_mouse(dev):
    if PHYS_MARKER in (dev.phys or ""):
        return False  # one of our own uinput mirrors
    caps = dev.capabilities()
    keys = caps.get(e.EV_KEY, ())
    rels = caps.get(e.EV_REL, ())
    # EV_ABS capabilities are (code, AbsInfo) pairs; pull out the codes.
    abs_codes = {a[0] if isinstance(a, tuple) else a
                 for a in caps.get(e.EV_ABS, ())}
    # A plain relative mouse: middle button + both relative axes. Requiring
    # REL_X *and* REL_Y is what excludes keyboards that expose a stray
    # BTN_*/REL_X capability through a media or consumer-control collection
    # (e.g. the Razer BlackWidow), which we were wrongly grabbing before.
    # Exclude only devices with a pointing absolute axis (touchpads,
    # touchscreens, tablets); a stray unrelated ABS axis on a gaming mouse or
    # receiver is fine.
    return (e.BTN_MIDDLE in keys
            and e.REL_X in rels
            and e.REL_Y in rels
            and e.ABS_X not in abs_codes
            and e.ABS_MT_POSITION_X not in abs_codes)


async def main():
    focus = FocusFilter()
    notifier = Notifier(focus)
    await notifier.start()
    states = {}
    tasks = {}
    seen = set()
    our_paths = set()  # event nodes of our own uinput mirrors, never grabbed
    if not phys_roundtrips():
        log.warning("uinput phys marker did not round-trip; relying on "
                    "device-path tracking to skip our own mirrors")
    tick_task = asyncio.create_task(ticker(states, notifier, focus))
    log.info("running")
    try:
        while True:
            # Hotplug: probe only paths we have never examined. Non-mouse
            # devices are remembered and never reopened (repeatedly opening
            # every input device caused visible input hiccups); a path is
            # forgotten when it disappears, so replugging re-probes it. Our
            # own mirror nodes are skipped so we never grab what we emit.
            paths = set(list_devices())
            seen &= paths
            our_paths &= paths
            for path in sorted(paths - seen - our_paths):
                seen.add(path)
                try:
                    dev = InputDevice(path)
                except OSError:
                    continue
                if is_mouse(dev):
                    tasks[path] = asyncio.create_task(
                        pump(path, dev, states, tasks, focus, our_paths))
                else:
                    log.debug("ignoring %s (%s)", dev.name, path)
                    dev.close()
            await asyncio.sleep(2)
    finally:
        tick_task.cancel()
        for t in list(tasks.values()):
            t.cancel()


def _float_arg(key):
    def parse(text):
        try:
            val = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number")
        err = validate(key, val)
        if err:
            raise argparse.ArgumentTypeError(f"{key} {err}")
        return val
    return parse


# CLI flag -> (config key, help text); dest is the key lowercased.
CLI_FLOATS = {
    "--deadzone-px": ("DEADZONE_PX", "per-axis dead zone in pixels"),
    "--speed-mult": ("SPEED_MULT", "overall speed multiplier"),
    "--speed-exp": ("SPEED_EXP", "speed curve exponent"),
    "--max-px-per-sec": ("MAX_PX_PER_SEC", "scroll speed safety cap"),
    "--px-per-notch": ("PX_PER_NOTCH",
                       "pixels one wheel notch scrolls in your apps"),
    "--max-drag-px": ("MAX_DRAG_PX", "cap on effective drag distance"),
    "--tick-hz": ("TICK_HZ", "scroll event rate"),
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="midscroll",
        description="Windows-style middle-button drag autoscroll daemon.",
        epilog=f"Defaults come from {CONFIG_PATH}; command-line options "
               "override it for this run.")
    p.add_argument("--version", action="version",
                   version=f"midscroll {VERSION}")
    p.add_argument("--config", default=CONFIG_PATH, metavar="PATH",
                   help=f"config file to read (default: {CONFIG_PATH})")
    p.add_argument("--debug", action="store_true",
                   help="log debug detail (device probing, focus changes, "
                        "scroll starts)")
    p.add_argument("--natural", action=argparse.BooleanOptionalAction,
                   default=None, help="invert the scroll direction")
    p.add_argument("--toggle-mode", action=argparse.BooleanOptionalAction,
                   default=None, dest="toggle_mode",
                   help="click to start/stop autoscroll (Windows-Explorer "
                        "style) instead of hold-and-drag")
    p.add_argument("--desktop", action=argparse.BooleanOptionalAction,
                   default=None, dest="desktop_scroll",
                   help="also autoscroll over the desktop and panels "
                        "(default: off, so they are left alone)")
    p.add_argument("--free-cursor", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="let the cursor move freely during drag-scroll "
                        "instead of anchoring it")
    p.add_argument("--blacklist", metavar="APPS", default=None,
                   help="comma-separated window-class substrings over which "
                        "midscroll pauses (default: "
                        f"\"{', '.join(BLACKLIST)}\"; pass '' to disable)")
    for flag, (key, help_text) in CLI_FLOATS.items():
        p.add_argument(flag, dest=key.lower(), type=_float_arg(key),
                       default=None, metavar="N",
                       help=f"{help_text} (default: {globals()[key]:g})")
    return p.parse_args(argv)


def cli():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s")
    load_config(args.config)
    for key in FLOAT_KEYS:
        val = getattr(args, key.lower())
        if val is not None:
            globals()[key] = val
    if args.natural is not None:
        globals()["NATURAL"] = args.natural
    if args.toggle_mode is not None:
        globals()["TOGGLE_MODE"] = args.toggle_mode
    if args.desktop_scroll is not None:
        globals()["DESKTOP_SCROLL"] = args.desktop_scroll
    if args.free_cursor is not None:
        globals()["FREE_CURSOR"] = args.free_cursor
    if args.blacklist is not None:
        globals()["BLACKLIST"] = parse_blacklist(args.blacklist)
    log.debug("tunables: %s NATURAL=%s TOGGLE_MODE=%s DESKTOP_SCROLL=%s FREE_CURSOR=%s "
              "BLACKLIST=%s",
              " ".join(f"{k}={globals()[k]:g}" for k in sorted(FLOAT_KEYS)),
              NATURAL, TOGGLE_MODE, DESKTOP_SCROLL, FREE_CURSOR, BLACKLIST)
    asyncio.run(main())


if __name__ == "__main__":
    cli()

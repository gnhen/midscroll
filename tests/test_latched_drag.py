"""Exercise the real event pump with fake mouse/uinput; no input devices opened."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('midscroll', ROOT / 'midscroll.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
e = m.e


def event(kind, code, value):
    return SimpleNamespace(type=kind, code=code, value=value)


def button(code=e.BTN_MIDDLE, value=1):
    return event(e.EV_KEY, code, value)


def move(value=40, axis=e.REL_Y):
    return event(e.EV_REL, axis, value)


SYN = event(e.EV_SYN, e.SYN_REPORT, 0)


class Mirror:
    device = SimpleNamespace(path='/fake/mirror')

    def __init__(self):
        self.events = []
        self.closed = False

    def write(self, *args):
        self.events.append(args)

    def syn(self):
        self.write(e.EV_SYN, e.SYN_REPORT, 0)

    def close(self):
        self.closed = True


class Notice:
    def __init__(self):
        self.visible = False
        self.ghost_visible = False

    def ghost(self, value):
        self.ghost_visible = value

    def pos(self, *args):
        pass

    def set(self, value):
        self.visible = value


class DragTests(unittest.TestCase):
    def run_events(self, steps, latched=True, toggle=False, blocked=False):
        ui, notice = Mirror(), Notice()
        states, tasks, paths = {}, {'/fake/mouse': None}, set()
        focus = SimpleNamespace(blocked=blocked, by_client={})

        class Mouse:
            phys, name = '', 'test mouse'
            closed = False

            def grab(self):
                pass

            def close(self):
                self.closed = True

            def active_keys(self):
                return []

            async def async_read_loop(self):
                for step in steps:
                    if callable(step):
                        step(states['/fake/mouse'], ui, notice, focus)
                    else:
                        yield step
                raise OSError('unplugged')

        dev = Mouse()
        with patch.multiple(m, LATCHED_DRAG_MODE=latched, TOGGLE_MODE=toggle), \
                patch.object(m, 'make_uinput', return_value=ui):
            asyncio.run(m.pump('/fake/mouse', dev, states, tasks, focus,
                               paths, notice))
        self.assertEqual((states, tasks, paths), ({}, {}, set()))
        self.assertTrue(ui.closed and dev.closed)
        return ui

    def assert_active(self, st, ui, notice, focus):
        self.assertTrue(st.active)
        self.assertTrue(st.latched)
        self.assertFalse(st.release_pending)
        m.tick(st, .1, focus)
        notice.set(st.scrolling)
        self.assertTrue(notice.visible)
        self.assertTrue(notice.ghost_visible)

    def keys(self, ui):
        return [ev for ev in ui.events if ev[0] == e.EV_KEY]

    def test_quick_click_is_native_in_both_drag_modes(self):
        for latched in (False, True):
            with self.subTest(latched=latched):
                ui = self.run_events([button(), SYN, button(value=0), SYN], latched)
                self.assertEqual(self.keys(ui), [(e.EV_KEY, e.BTN_MIDDLE, 1),
                                                (e.EV_KEY, e.BTN_MIDDLE, 0)])

    def test_sub_deadzone_motion_replays_with_click(self):
        ui = self.run_events([button(), move(15), button(value=0), SYN])
        self.assertEqual(len(self.keys(ui)), 2)
        self.assertIn((e.EV_REL, e.REL_Y, 15), ui.events)

    def test_fast_drag_release_latches_without_waiting_for_tick(self):
        def movement(st, ui, notice, focus):
            self.assertEqual(st.dy, 70)
            self.assert_active(st, ui, notice, focus)
            self.assertTrue(any(ev[1] == e.REL_WHEEL_HI_RES for ev in ui.events))
        ui = self.run_events([button(), move(), button(value=0), SYN,
                              self.assert_active, move(30), SYN, movement])
        self.assertEqual(self.keys(ui), [])
        self.assertFalse(any(ev[:2] == (e.EV_REL, e.REL_Y) for ev in ui.events))

    def test_cancelling_button_press_and_release_are_consumed(self):
        for code in (e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE):
            def stopped(st, ui, notice, focus):
                self.assertFalse(st.scrolling)
                self.assertFalse(notice.visible)
                self.assertFalse(notice.ghost_visible)
            with self.subTest(code=code):
                ui = self.run_events([button(), move(), button(value=0), SYN,
                                      button(code), SYN, stopped,
                                      button(code, 0), SYN,
                                      button(e.BTN_LEFT), button(e.BTN_LEFT, 0), SYN])
                self.assertEqual(self.keys(ui), [(e.EV_KEY, e.BTN_LEFT, 1),
                                                (e.EV_KEY, e.BTN_LEFT, 0)])
                self.assertIn((e.EV_REL, e.REL_Y, 40), ui.events)  # snap at stop

    def test_cancellation_while_middle_held_consumes_both_releases(self):
        ui = self.run_events([button(), move(), button(e.BTN_LEFT), SYN,
                              button(e.BTN_LEFT, 0), button(value=0), SYN])
        self.assertEqual(self.keys(ui), [])

    def test_disabled_mode_stops_on_release(self):
        def stopped(st, *args):
            self.assertFalse(st.scrolling)
        def tick(st, ui, notice, focus):
            m.tick(st, .01, focus)
        self.run_events([button(), move(), tick, button(value=0), SYN, stopped],
                        latched=False)

    def test_toggle_mode_takes_precedence(self):
        for latched in (False, True):
            def toggled(st, *args):
                self.assertTrue(st.toggled)
                self.assertFalse(st.active)
            ui = self.run_events([button(), button(value=0), SYN, toggled,
                                  move(), button(e.BTN_LEFT), button(e.BTN_LEFT, 0), SYN],
                                 latched=latched, toggle=True)
            self.assertEqual(self.keys(ui), [])
            self.assertIn((e.EV_REL, e.REL_Y, 40), ui.events)

    def test_excluded_app_gets_native_middle_drag(self):
        ui = self.run_events([button(), move(), button(value=0), SYN], blocked=True)
        self.assertEqual(len(self.keys(ui)), 2)
        self.assertIn((e.EV_REL, e.REL_Y, 40), ui.events)

    def test_focus_change_stops_latch(self):
        def block(st, ui, notice, focus):
            focus.blocked = True
            m.tick(st, .1, focus)
            self.assertFalse(st.scrolling)
        self.run_events([button(), move(), button(value=0), SYN, block])

    def test_wheel_passthrough_idle_and_latched(self):
        wheel = event(e.EV_REL, e.REL_WHEEL, -1)
        ui = self.run_events([wheel, SYN, button(), move(), button(value=0), SYN,
                              wheel, SYN])
        self.assertEqual(ui.events.count((e.EV_REL, e.REL_WHEEL, -1)), 2)

    def test_dropped_events_cancel_latch_and_allow_next_click(self):
        def stopped(st, *args):
            self.assertFalse(st.scrolling)
        ui = self.run_events([button(), move(), button(value=0), SYN,
                              event(e.EV_SYN, e.SYN_DROPPED, 0), stopped,
                              button(), button(value=0), SYN])
        self.assertEqual(len(self.keys(ui)), 2)

    def test_unplugged_latch_can_be_replugged_and_clicked(self):
        self.run_events([button(), move(), button(value=0), SYN, self.assert_active])
        ui = self.run_events([button(), button(value=0), SYN])
        self.assertEqual(len(self.keys(ui)), 2)

    def test_button_on_second_mouse_cancels_latch(self):
        def second_mouse(st, ui, notice, focus):
            other = m.State(Mirror())
            other.active = other.latched = True
            other.dy = 80
            # The pump sees every grabbed mouse through the shared states map.
            # Include it only for this check; normal teardown owns its own state.
            second_mouse.other = other
            states_ref = next_states[0]
            states_ref['/other'] = other

        # Capture the states argument without changing pump behavior.
        next_states = []
        original_pump = m.pump
        async def wrapped(path, dev, states, *args):
            next_states.append(states)
            try:
                return await original_pump(path, dev, states, *args)
            finally:
                states.pop('/other', None)
        def stopped(st, ui, notice, focus):
            self.assertFalse(second_mouse.other.scrolling)
            self.assertFalse(notice.visible)
        with patch.object(m, 'pump', wrapped):
            ui = self.run_events([second_mouse, button(e.BTN_RIGHT),
                                  button(e.BTN_RIGHT, 0), SYN, stopped])
        self.assertEqual(self.keys(ui), [])

    def test_deadzone_is_per_axis_and_strict(self):
        for axis in (e.REL_X, e.REL_Y):
            for distance in (-16, -15, 15, 16):
                with self.subTest(axis=axis, distance=distance):
                    def check(st, *args):
                        self.assertEqual(st.latched, abs(distance) > m.DEADZONE_PX)
                    ui = self.run_events([button(), move(distance, axis),
                                          button(value=0), SYN, check])
                    self.assertEqual(len(self.keys(ui)),
                                     0 if abs(distance) > m.DEADZONE_PX else 2)

    def test_return_to_anchor_still_latches_after_crossing_deadzone(self):
        def paused(st, ui, notice, focus):
            self.assertTrue(st.active and st.latched)
            self.assertEqual(st.dy, 0)
            before = list(ui.events)
            m.tick(st, .1, focus)
            self.assertEqual(ui.events, before)
        ui = self.run_events([button(), move(40), move(-40),
                              button(value=0), SYN, paused,
                              button(e.BTN_LEFT), button(e.BTN_LEFT, 0), SYN])
        self.assertEqual(self.keys(ui), [])

    def test_cancellation_in_middle_release_report_does_not_eat_next_click(self):
        ui = self.run_events([button(), move(), SYN, button(value=0),
                              button(e.BTN_LEFT), SYN,
                              button(e.BTN_LEFT, 0), SYN,
                              button(), button(value=0), SYN])
        self.assertEqual(self.keys(ui), [(e.EV_KEY, e.BTN_MIDDLE, 1),
                                        (e.EV_KEY, e.BTN_MIDDLE, 0)])

    def test_cancellation_without_cursor_snap(self):
        with patch.object(m, 'SNAP_CURSOR_ON_RELEASE', False):
            ui = self.run_events([button(), move(), button(value=0), SYN,
                                  button(e.BTN_RIGHT), button(e.BTN_RIGHT, 0), SYN])
        self.assertEqual(self.keys(ui), [])
        self.assertFalse(any(ev[0] == e.EV_REL and ev[1] in (e.REL_X, e.REL_Y)
                             for ev in ui.events))

    def test_cancellation_release_survives_another_middle_press(self):
        ui = self.run_events([button(), move(), button(value=0), SYN,
                              button(e.BTN_LEFT), SYN,
                              button(), SYN, button(e.BTN_LEFT, 0), SYN,
                              button(value=0), SYN])
        self.assertEqual(self.keys(ui), [(e.EV_KEY, e.BTN_MIDDLE, 1),
                                        (e.EV_KEY, e.BTN_MIDDLE, 0)])

    def test_release_report_motion_does_not_move_anchor(self):
        def check(st, *args):
            self.assertTrue(st.latched)
            self.assertEqual(st.dy, 40)
        ui = self.run_events([button(), move(), SYN, button(value=0),
                              move(30), SYN, check])
        self.assertFalse(any(ev[:2] == (e.EV_REL, e.REL_Y) for ev in ui.events))

    def test_idle_buttons_and_keyboard_events_are_not_cancellation(self):
        key = event(e.EV_KEY, e.KEY_A, 1)
        def check(st, *args):
            self.assertTrue(st.latched)
        ui = self.run_events([button(e.BTN_LEFT), button(e.BTN_LEFT, 0), SYN,
                              button(), move(), button(value=0), SYN, key, check])
        self.assertEqual(self.keys(ui), [(e.EV_KEY, e.BTN_LEFT, 1),
                                        (e.EV_KEY, e.BTN_LEFT, 0),
                                        (e.EV_KEY, e.KEY_A, 1),
                                        (e.EV_KEY, e.KEY_A, 0)])  # teardown

    def test_apply_helper_accepts_and_renders_setting(self):
        spec = importlib.util.spec_from_file_location('midscroll_apply',
                                                      ROOT / 'midscroll-apply.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.assertIn('LATCHED_DRAG_MODE', helper.BOOLS)
        self.assertFalse(helper.DEFAULTS['LATCHED_DRAG_MODE'])
        values = dict(helper.DEFAULTS)
        values.update({key: 'false' for key in helper.BOOLS})
        values['LATCHED_DRAG_MODE'] = 'true'
        values['ALLOW_KEYBOARDS'] = ''
        self.assertIn('LATCHED_DRAG_MODE = true', helper.TEMPLATE.format(**values))

    def test_keyboard_class_stays_excluded_even_when_forced(self):
        dev = SimpleNamespace(phys='', name='test keyboard', capabilities=lambda: {
            e.EV_KEY: [*m.KEYBOARD_KEYS, e.BTN_MIDDLE],
            e.EV_REL: [e.REL_X, e.REL_Y]})
        with patch.multiple(m, EXTRA_DEVICES=['test keyboard'], ALLOW_KEYBOARDS=False):
            self.assertFalse(m.decide_device(dev, '/dev/input/event9')[0])

    def test_config_and_cli_setting(self):
        with patch.object(m, 'read_config_text', return_value='LATCHED_DRAG_MODE=true'), \
                patch.object(m, 'LATCHED_DRAG_MODE', False):
            m.load_config('/unused')
            self.assertTrue(m.LATCHED_DRAG_MODE)
        self.assertTrue(m.parse_args(['--latched-drag-mode']).latched_drag_mode)
        self.assertFalse(m.parse_args(['--no-latched-drag-mode']).latched_drag_mode)


if __name__ == '__main__':
    unittest.main()

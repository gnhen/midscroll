#!/usr/bin/env python3
"""Behavior checks for toggle mode's optional hold threshold."""

import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def load_midscroll():
    codes = {
        name: value
        for value, name in enumerate(
            (
                "ABS_MT_POSITION_X", "ABS_X", "BTN_JOYSTICK", "BTN_MIDDLE",
                "BTN_MOUSE", "EV_ABS", "EV_KEY", "EV_REL", "EV_SYN",
                "KEY_A", "KEY_ENTER", "KEY_SPACE", "KEY_Z", "REL_HWHEEL",
                "REL_HWHEEL_HI_RES", "REL_WHEEL", "REL_WHEEL_HI_RES",
                "REL_X", "REL_Y", "SYN_DROPPED",
            ),
            start=1,
        )
    }
    fake_evdev = types.ModuleType("evdev")
    fake_evdev.InputDevice = object
    fake_evdev.UInput = object
    fake_evdev.ecodes = SimpleNamespace(**codes)
    fake_evdev.list_devices = lambda: []
    sys.modules["evdev"] = fake_evdev

    path = pathlib.Path(__file__).parents[1] / "midscroll.py"
    spec = importlib.util.spec_from_file_location("midscroll_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeUInput:
    def __init__(self):
        self.events = []

    def write(self, event_type, code, value):
        self.events.append(("write", event_type, code, value))

    def syn(self):
        self.events.append(("syn",))


class ToggleBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.midscroll = load_midscroll()

    def event(self, value):
        return SimpleNamespace(code=self.midscroll.e.BTN_MIDDLE, value=value)

    def run_press(self, threshold, press_time, release_time):
        m = self.midscroll
        m.TOGGLE_HOLD_MS = threshold
        ui = FakeUInput()
        state = m.State(ui)
        focus = SimpleNamespace(blocked=False)
        with patch.object(
            m.time, "monotonic", side_effect=(press_time, release_time)
        ):
            m._toggle_key(self.event(1), state, ui, focus)
            m._toggle_key(self.event(0), state, ui, focus)
        return state, ui

    def test_quick_middle_click_is_replayed_natively(self):
        m = self.midscroll
        state, ui = self.run_press(180, 10.0, 10.1)
        self.assertFalse(state.toggled)
        self.assertEqual(
            ui.events,
            [
                ("write", m.e.EV_KEY, m.e.BTN_MIDDLE, 1),
                ("syn",),
                ("write", m.e.EV_KEY, m.e.BTN_MIDDLE, 0),
                ("syn",),
            ],
        )

    def test_long_middle_press_starts_toggle_scroll(self):
        state, ui = self.run_press(180, 20.0, 20.25)
        self.assertTrue(state.toggled)
        self.assertEqual(ui.events, [])

    def test_zero_threshold_retains_click_to_toggle(self):
        state, ui = self.run_press(0, 30.0, 30.0)
        self.assertTrue(state.toggled)
        self.assertEqual(ui.events, [])


if __name__ == "__main__":
    unittest.main()

import ctypes
import threading

import comtypes.client

# UI Automation control type ids: Button, Hyperlink, ListItem, MenuItem
SNAP_TYPES = {50000, 50005, 50007, 50011}
MAX_SNAP_SIZE = 200  # px, width and height must both be smaller


class MagneticSnapper:
    """Returns the center of a small clickable UI element under a point, else None."""

    def __init__(self) -> None:
        ctypes.windll.ole32.CoInitialize(None)
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        self._UIA = UIA
        self._init_thread = threading.get_ident()
        self._local = threading.local()
        self.uia = self._create()

    def _create(self):
        return comtypes.client.CreateObject(
            self._UIA.CUIAutomation, interface=self._UIA.IUIAutomation)

    def _uia_for_this_thread(self):
        # COM objects must be created on the thread that uses them
        if threading.get_ident() == self._init_thread:
            return self.uia
        uia = getattr(self._local, "uia", None)
        if uia is None:
            ctypes.windll.ole32.CoInitialize(None)
            uia = self._create()
            self._local.uia = uia
        return uia

    def get_snap_target(self, x, y):
        try:
            uia = self._uia_for_this_thread()
            el = uia.ElementFromPoint(self._UIA.tagPOINT(int(x), int(y)))
            if el is None or el.CurrentControlType not in SNAP_TYPES:
                return None
            r = el.CurrentBoundingRectangle
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0 or w >= MAX_SNAP_SIZE or h >= MAX_SNAP_SIZE:
                return None
            return (r.left + r.right) / 2.0, (r.top + r.bottom) / 2.0
        except Exception:
            return None
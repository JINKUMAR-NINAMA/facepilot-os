import argparse
import math
import sys
import time
from collections import deque

import keyboard
from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication

from config import FacePilotConfig
from core import win32_input
from core.expression_engine import ExpressionController
from core.vision_thread import FrameTelemetry, VisionThread


class UiClickRouter:
    """Decides whether a left press belongs to a notch button or an OSK key (called from the vision thread).

    Plain Python + pure-math hit tests only. Swallowed presses queue an action for the main-thread timer.
    A press anywhere on the visible OSK panel is swallowed (even between keys) so it can never reach, and
    steal focus from, the window underneath.
    """

    DEDUPE_S = 0.12

    def __init__(self, notch=None, osk=None) -> None:
        self.notch = notch
        self.osk = osk
        self.pending = deque()
        self._swallow = False
        self._last = (None, 0.0)

    def hit(self, x, y):
        osk = self.osk
        if osk is not None and osk.is_active and osk.contains_point(x, y):
            return ("OSK", osk.hit_test(x, y))
        notch = self.notch
        if notch is not None:
            name = notch.hit_test(x, y)
            if name is not None:
                return ("NOTCH", name)
        return None

    def gate(self, kind: str, x, y) -> bool:
        """True = swallow this left-button event. 'up' always pairs with a swallowed 'down'."""
        if kind == "up":
            swallowed, self._swallow = self._swallow, False
            return swallowed
        hit = None if x is None else self.hit(x, y)
        if hit is None:
            self._swallow = False
            return False
        self._swallow = True
        if hit[1] is not None:
            now = time.perf_counter()
            if not (hit == self._last[0] and (now - self._last[1]) < self.DEDUPE_S):
                self.pending.append(hit)       # a double-click gesture on one key queues it once
            self._last = (hit, now)
        return True


class CursorController(QObject):
    """on_telemetry is connected with DirectConnection: it runs in the vision thread for minimum latency."""

    def __init__(self, enabled: bool, cfg: FacePilotConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.tracking_enabled = enabled
        self.snapper = None
        if cfg.magnetic_snap_enabled:
            try:
                from core.magnetic_snap import MagneticSnapper
                self.snapper = MagneticSnapper()
            except Exception as e:
                print(f"Magnetic snap disabled: {e}", flush=True)
        self.expr = ExpressionController(cfg.swap_eyes)
        # Phase 4 overlay HUDs (click-through, never take focus). Optional: a failure must not stop head control.
        self.notch = None
        self.ring = None
        self._resolve_state = None
        self.last_face = False               # written by on_telemetry (vision thread), read by the HUD timer
        self.last_fps = 0.0
        # Phase 4.3: click router (gate runs in the vision thread, actions run on the main-thread timer),
        # built-in GlassOSK and the Voice AI hotkey bridge (Wispr Flow / Win+H / custom chord)
        self.osk = None
        self.router = UiClickRouter()
        self._ui_last_ts = 0.0
        self.voice_active: bool = False
        try:
            from ui.glass_hud import CursorRingHUD, DynamicIslandNotch, resolve_state
            self._resolve_state = resolve_state
            self.notch = DynamicIslandNotch()
            self.ring = CursorRingHUD()
            self.notch.show()
        except Exception as e:
            print(f"HUD disabled: {e}", flush=True)
            self.notch = self.ring = None
        if self.notch is not None:
            try:
                from ui.glass_hud import GlassOSK
                self.osk = GlassOSK()
            except Exception as e:
                print(f"Built-in keyboard disabled: {e}", flush=True)
                self.osk = None
            self.router.notch = self.notch
            self.router.osk = self.osk
            win32_input.set_left_gate(self._left_gate)
        self.vision = None   # set in main(); used to request a head re-center
        self.screen_w, self.screen_h = win32_input.screen_size()
        self.freeze_cursor = False
        self.target_x = None
        self.target_y = None
        self.curr_x = None
        self.curr_y = None
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(8)  # 125 Hz
        self.timer.timeout.connect(self.update_cursor)
        self.timer.start()

    def _left_gate(self, kind: str) -> bool:
        """Called by win32_input.left_down/left_up (vision thread). True = swallow: a notch button / OSK key was hit."""
        return self.router.gate(kind, self.curr_x, self.curr_y)

    def _service_ui_actions(self) -> None:
        q = self.router.pending
        while q:
            try:
                src, name = q.popleft()
            except IndexError:
                break
            try:
                if src == "OSK":
                    self._osk_key(name)
                else:
                    self._notch_action(name)
            except Exception as ex:
                print(f"UI action {src}:{name} failed: {ex}", flush=True)

    def _notch_action(self, name: str) -> None:
        now = time.perf_counter()
        if now - self._ui_last_ts < 0.6:         # a double-wink must not toggle twice
            return
        self._ui_last_ts = now
        if self.notch is not None:
            self.notch.flash(name)
        if name == "KEYBOARD":
            self._toggle_osk()
        elif name == "VOICE":
            self._toggle_voice()

    def _toggle_osk(self) -> None:
        if self.osk is None:
            win32_input.toggle_osk()             # built-in keyboard unavailable: fall back to the Windows one
            return
        self.osk.setVisible(not self.osk.is_active)
        for w in (self.ring, self.notch):        # keep the cursor ring and notch above the keyboard
            if w is not None:
                w.raise_()

    def _quit_app(self) -> None:
        """On-screen exit: reset the voice indicator, hide the keyboard and quit (aboutToQuit does the rest)."""
        self.stop_voice()
        if self.osk is not None:
            self.osk.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _osk_key(self, token) -> None:
        osk = self.osk
        if osk is None:
            return
        osk.flash_key(token)
        if token == "CTRL":
            osk.ctrl_active = not osk.ctrl_active
            osk.update()
            return
        if token == "ALT":
            osk.alt_active = not osk.alt_active
            osk.update()
            return
        if (osk.ctrl_active or osk.alt_active) and token not in ("SHIFT", "SYM", "CLEAR", "HIDE"):
            mods = []
            if osk.ctrl_active:
                mods.append(0x11)                # VK_CONTROL
            if osk.alt_active:
                mods.append(0x12)                # VK_MENU
            if osk.shift_active:
                mods.append(0x10)                # VK_SHIFT
            vk = win32_input.char_to_vk(token)
            osk.reset_sticky_modifiers()         # one-shot latches: always unlatch after the key
            if vk is not None:
                win32_input.send_hotkey_chord(mods + [vk])
                return
        if token == "ESC":                       # reached only when no CTRL / ALT chord fired
            self._quit_app()
            return
        if token == "BACKSPACE":
            win32_input.send_vk(win32_input.VK_BACK)
        elif token == "ENTER":
            win32_input.send_vk(win32_input.VK_RETURN)
        elif token == "SPACE":
            win32_input.type_unicode_text(" ")
        elif token == "CLEAR":
            win32_input.send_hotkey_chord([0x11, 0x41])
            win32_input.send_vk(win32_input.VK_BACK)
        elif token == "SHIFT":
            osk.toggle_shift()
        elif token == "SYM":
            osk.toggle_sym()
        elif token == "HIDE":
            osk.hide()
        elif len(token) == 1:
            win32_input.type_unicode_text(token)

    def _toggle_voice(self) -> None:
        """Flip the Voice AI state and fire the configured dictation hotkey (Wispr Flow by default)."""
        self.voice_active = not self.voice_active
        try:
            win32_input.trigger_voice_ai(self.cfg.voice_mode, self.cfg.voice_custom_vks)
        except Exception as ex:
            print(f"Voice AI hotkey failed: {ex}", flush=True)
            self.voice_active = False

    def stop_voice(self) -> None:
        self.voice_active = False

    def _center_action(self, action: str) -> None:
        cx, cy = self.screen_w // 2, self.screen_h // 2
        if action == "CENTER_UNLOCK" and self.vision is not None:
            self.vision.request_recenter()   # same as F8: current head pose becomes neutral
        self.curr_x = self.target_x = float(cx)
        self.curr_y = self.target_y = float(cy)
        win32_input.set_cursor_pos(cx, cy)
        self.freeze_cursor = True            # keep this frame's stale head position from overwriting the center

    def toggle(self) -> None:
        self.tracking_enabled = not self.tracking_enabled
        if not self.tracking_enabled:
            self.expr.release_all()
            self.voice_active = False            # F10 pause resets the Voice AI indicator
        print(f"Tracking {'ENABLED' if self.tracking_enabled else 'PAUSED'}", flush=True)

    @pyqtSlot(object)
    def on_telemetry(self, t: FrameTelemetry) -> None:
        self.last_face = t.face_detected     # plain attributes only: no Qt widget is touched from this thread
        self.last_fps = t.fps
        if not t.face_detected:
            self.expr.release_all()
            self.freeze_cursor = False
        elif self.tracking_enabled:
            self.freeze_cursor = self.expr.update(t.left_ear, t.right_ear)
            action = self.expr.consume_action()
            if action:
                self._center_action(action)
        if t.face_detected and not self.freeze_cursor:
            self.target_x = t.screen_x
            self.target_y = t.screen_y
            if self.snapper is not None and t.velocity < 20.0:
                snap = self.snapper.get_snap_target(self.target_x, self.target_y)
                if snap is not None:
                    self.target_x, self.target_y = snap
            if self.curr_x is None:
                self.curr_x = self.target_x
                self.curr_y = self.target_y

    def update_cursor(self) -> None:
        self._move_cursor()
        self._service_ui_actions()
        self._update_hud()

    def _update_hud(self) -> None:
        if self.notch is None or self.ring is None:
            return
        try:
            e = self.expr
            key, label = self._resolve_state(self.tracking_enabled, self.last_face, e.center_locked,
                                             e.drag_active, e.hold_progress, e.hold_kind,
                                             e.last_action, time.perf_counter())
            if self.curr_x is not None:
                self.notch.update_hover(self.curr_x, self.curr_y)
            self.notch.set_status(key, label, e.last_l_ratio, e.last_r_ratio, self.last_fps,
                                  e.hold_progress, e.hold_kind, "WISPR" if self.voice_active else "")
            if self.osk is not None and self.osk.is_active and self.curr_x is not None:
                self.osk.update_hover(self.curr_x, self.curr_y)
            self.ring.set_status(key, e.hold_progress, e.hold_kind)
            if self.curr_x is not None:
                self.ring.move_to(self.curr_x, self.curr_y)
                if not self.ring.isVisible():
                    self.ring.show()
        except Exception as ex:
            print(f"HUD error, overlays disabled: {ex}", flush=True)
            win32_input.set_left_gate(None)
            for w in (self.notch, self.ring, self.osk):
                if w is not None:
                    w.hide()
            self.notch = self.ring = None

    def _move_cursor(self) -> None:
        if (not self.tracking_enabled or self.expr.is_frozen()
                or self.target_x is None or self.curr_x is None):
            return
        dx = self.target_x - self.curr_x
        dy = self.target_y - self.curr_y
        if dx * dx + dy * dy < self.cfg.micro_aim_deadzone ** 2:
            return  # inside micro-aim deadzone: hold the cursor still
        dist = math.hypot(dx, dy)
        alpha = 0.80 if dist >= 35.0 else max(0.30, min(0.75, dist / 50.0))
        self.curr_x += dx * alpha
        self.curr_y += dy * alpha
        win32_input.set_cursor_pos(int(self.curr_x), int(self.curr_y))


class KillBridge(QObject):
    kill = pyqtSignal()   # emitted from the keyboard hook thread, delivered queued


def run_verify() -> int:
    from core.one_euro_filter import OneEuroFilter2D
    from core.pose_mapper import PoseMapper

    results = []

    def check(name, ok, info=""):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")

    cfg = FacePilotConfig()
    check("config", (cfg.frame_width, cfg.frame_height, cfg.fps) == (640, 480, 60)
          and cfg.kill_switch == "esc" and cfg.recenter_key == "f8")

    # 1-Euro: jitter around a still point must shrink; a fast sweep must still move
    import random
    random.seed(1)
    f = OneEuroFilter2D(cfg.min_cutoff, cfg.beta, cfg.d_cutoff, 1920, 1080)
    t, outs = 0.0, []
    for _ in range(120):
        t += 1 / 60
        outs.append(f(0.5 + random.uniform(-0.003, 0.003), 0.5 + random.uniform(-0.003, 0.003), t))
    spread = max(o[0] for o in outs[30:]) - min(o[0] for o in outs[30:])
    check("1euro jitter reduction", spread < 0.006 * 1920, f"(spread {spread:.2f}px)")
    for i in range(30):
        t += 1 / 60
        o = f(0.5 + i * 0.01, 0.5, t)
    check("1euro velocity", o[2] > 100, f"({o[2]:.0f}px/s)")

    # Pose mapper with synthetic landmarks
    class LM:
        def __init__(self, x=0.5, y=0.5, z=0.0):
            self.x, self.y, self.z = x, y, z

    def face(shift=0.0, fz=0.0):
        lm = [LM() for _ in range(478)]
        lm[234], lm[454] = LM(0.4, 0.5), LM(0.6, 0.5)
        lm[10], lm[152] = LM(0.5, 0.35, fz), LM(0.5, 0.65, 0.0)
        lm[1] = LM(0.5 + shift, 0.5)
        return lm

    pm = PoseMapper(cfg, 1920, 1080)
    c = pm.map(face())
    check("mapper center", abs(c.screen_x - 959.5) < 2 and c.in_deadzone)
    r = pm.map(face(shift=-0.05))   # nose to image-left = user turns right (invert_x)
    check("mapper direction", r.screen_x > c.screen_x)
    ext = pm.map(face(shift=-0.5))
    check("mapper clamp", 0 <= ext.screen_x <= 1919)
    check("pitch factor", pm.map(face(fz=-0.05)).pitch_compensation_factor > 1.0)

    # 1:1 isotropy: equal normalized head offsets (0.05 yaw, 0.05 pitch) -> equal pixel displacement
    iso = face()
    iso[1] = LM(0.49, 0.515)         # yaw = -0.01/0.2 = -0.05, pitch = +0.015/0.3 = +0.05
    p_iso = pm.map(iso)
    dpx, dpy = p_iso.screen_x - 960.0, p_iso.screen_y - 540.0
    check("mapper isotropic 1:1 (equal X/Y offsets -> equal px)",
          dpx > 50 and abs(dpx - dpy) < 1.0, f"(dx={dpx:.1f}px, dy={dpy:.1f}px)")

    # vertical extremes still reach the top and bottom edges and never leave the screen
    ext_dn = face()
    ext_dn[1] = LM(0.5, 0.95)
    ext_up = face()
    ext_up[1] = LM(0.5, 0.05)
    p_dn, p_up = pm.map(ext_dn), pm.map(ext_up)
    check("mapper Y reaches edges, stays in bounds",
          0 <= p_up.screen_y < 80 and 1000 < p_dn.screen_y <= 1079,
          f"(up={p_up.screen_y:.0f}, down={p_dn.screen_y:.0f})")

    # Win32 (no-op move: set cursor to where it already is)
    if win32_input.IS_WINDOWS:
        x, y = win32_input.get_cursor_pos()
        check("win32 SetCursorPos", win32_input.set_cursor_pos(x, y), f"{win32_input.screen_size()}")
    else:
        print("[SKIP] win32 (not Windows)")

    # ExpressionController transitions (win32 button calls are recorded, never sent)
    from core.expression_engine import ExpressionController
    calls = []
    names = ("left_click", "right_click", "left_down", "left_up")
    saved = {n: getattr(win32_input, n) for n in names}
    for n in names:
        setattr(win32_input, n, (lambda name: lambda: calls.append(name))(n))
    try:
        def run_seq(segs, dt=1 / 30):
            ec = ExpressionController(verbose=False)
            calls.clear()
            t, first_frozen, max_frozen = 100.0, None, 0.0
            for le, re, secs in [(0.30, 0.30, 0.5)] + segs + [(0.30, 0.30, 0.6)]:
                for _ in range(max(1, round(secs / dt))):
                    if ec.update(le, re, now=t):
                        first_frozen = t if first_frozen is None else first_frozen
                        max_frozen = max(max_frozen, t - first_frozen)
                    else:
                        first_frozen = None
                    t += dt
            return list(calls), max_frozen

        got, fz = run_seq([(0.05, 0.05, 0.25)])
        check("expr: natural blink ignored", got == [] and fz == 0.0, f"({got})")
        got, fz = run_seq([(0.08, 0.30, 0.25)])
        check("expr: quick left wink -> left_down at 80ms, left_up on reopen (LEFT_CLICK)", got == ["left_down", "left_up"] and fz == 0.0,
              f"({got}, freeze {fz * 1000:.0f}ms)")
        got, fz = run_seq([(0.08, 0.30, 1.0)])
        check("expr: left hold 1s -> one left_down, one left_up", got == ["left_down", "left_up"] and fz == 0.0,
              f"({got}, freeze {fz * 1000:.0f}ms)")
        got, fz = run_seq([(0.30, 0.08, 0.25)])
        check("expr: right wink -> right click", got == ["right_click"] and fz == 0.0,
              f"({got}, freeze {fz * 1000:.0f}ms)")
        got, fz = run_seq([(0.30, 0.08, 0.2), (0.30, 0.30, 0.1), (0.30, 0.08, 0.1)])
        check("expr: right flutter inside 250ms cooldown -> one right click, no left clicks", got == ["right_click"],
              f"({got})")

        # 0 ms freeze: never frozen on the wink-onset frame or later
        ec = ExpressionController(verbose=False)
        tt = 100.0
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += 1 / 30
        f_on = ec.update(0.08, 0.30, now=tt)
        f_late = ec.update(0.08, 0.30, now=tt + 0.030)
        check("expr: 0ms freeze, never frozen by a wink", (not f_on) and (not f_late), f"(onset={f_on}, +30ms={f_late})")

        # two quick left winks -> two instant left clicks (native OS double-click)
        got, fz = run_seq([(0.08, 0.30, 0.2), (0.30, 0.30, 0.1), (0.08, 0.30, 0.2)])
        check("expr: two quick left winks -> two left clicks", got == ["left_down", "left_up", "left_down", "left_up"],
              f"({got})")

        # 0 ms firing: RIGHT_CLICK lands on the exact reopen frame (no wait buffer)
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt, dt = 300.0, 1 / 30
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += dt
        for _ in range(8):                       # right wink ~267 ms
            ec.update(0.30, 0.08, now=tt)
            tt += dt
        before = list(calls)
        ec.update(0.30, 0.30, now=tt)            # first reopened frame
        check("expr: right click fires on the reopen frame (0ms)",
              before == [] and calls == ["right_click"],
              f"(before={before}, on_reopen={calls})")

        # drag: left_down held from 80 ms, DRAG_START at 450 ms, 1-frame flicker tolerated, left_up after 2 reopened frames (>= 0.76)
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt = 400.0
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += dt
        for _ in range(30):                      # left wink held ~1 s -> DRAG_START at 750 ms
            ec.update(0.08, 0.30, now=tt)
            tt += dt
        held = list(calls)
        drag_up = ec.drag_active                 # DRAG_START reached at 450 ms
        for _ in range(3):                       # squint flicker: ratio 0.72 (< 0.76)
            ec.update(0.216, 0.30, now=tt)
            tt += dt
        flicker = list(calls)
        ec.update(0.30, 0.30, now=tt)            # ONE reopened frame (1-frame camera flicker)
        tt += dt
        one_flick = list(calls)
        still_drag = ec.drag_active
        ec.update(0.08, 0.30, now=tt)            # eye closed again: flicker forgotten
        tt += dt
        ec.update(0.30, 0.30, now=tt)            # reopened frame 1 (no drop yet)
        tt += dt
        ec.update(0.30, 0.30, now=tt)            # reopened frame 2 -> left_up
        check("expr: left hold -> DRAG_START, survives flicker, left_up on reopen",
              held == ["left_down"] and drag_up and flicker == ["left_down"] and one_flick == ["left_down"] and still_drag
              and calls == ["left_down", "left_up"] and not ec.drag_active,
              f"(held={held}, flicker={flicker}, 1frame={one_flick}, final={calls})")

        # right hold >= 450 ms: DOUBLE_CLICK at the 450 ms mark, and NO right click on reopen
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt, dt = 500.0, 1 / 30
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += dt
        for _ in range(20):                      # right eye closed ~667 ms
            ec.update(0.30, 0.08, now=tt)
            tt += dt
        during = list(calls)
        ec.update(0.30, 0.30, now=tt)            # right eye reopens
        check("expr: right hold >= 450ms -> DOUBLE_CLICK, no right click on reopen",
              during == ["left_click", "left_click"] and calls == ["left_click", "left_click"],
              f"(during={during}, after_reopen={calls})")

        # safety: left wink pressed (80 ms), then both eyes close -> left_up, button never stuck
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt = 600.0
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += dt
        for _ in range(5):                       # left wink ~167 ms: pressed at ~100 ms
            ec.update(0.08, 0.30, now=tt)
            tt += dt
        pressed = list(calls)
        ec.update(0.05, 0.05, now=tt)            # both eyes close
        check("expr: both eyes close mid left-press -> left_up, never stuck",
              pressed == ["left_down"] and calls == ["left_down", "left_up"] and not ec.left_button_down,
              f"(pressed={pressed}, after_close={calls})")

        # 2 s both-eyes hold: first hold locks, second hold unlocks; no clicks may fire
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt, dt = 200.0, 1 / 30
        acts = []

        def feed(le, re, secs):
            nonlocal tt
            for _ in range(round(secs / dt)):
                ec.update(le, re, now=tt)
                a = ec.consume_action()
                if a:
                    acts.append(a)
                tt += dt

        feed(0.30, 0.30, 0.5)
        feed(0.05, 0.05, 1.9)            # just under 2 s: nothing yet
        early = list(acts)
        feed(0.05, 0.05, 0.4)            # crosses 2 s: lock
        locked = ec.center_locked
        feed(0.30, 0.30, 0.5)            # eyes reopen: still locked, still frozen
        stays_frozen = ec.update(0.30, 0.30, now=tt)
        feed(0.05, 0.05, 2.1)            # second 2 s hold: unlock
        check("expr: 2s both-eyes hold -> lock/unlock",
              early == [] and acts == ["CENTER_LOCK", "CENTER_UNLOCK"]
              and locked and stays_frozen and not ec.center_locked and calls == [],
              f"(acts={acts}, clicks={calls})")

        # Phase 4: hold_progress / hold_kind / ratio read-outs the HUD reads (win32 calls still recorded)
        ec = ExpressionController(verbose=False)
        calls.clear()
        tt, dt = 700.0, 1 / 30
        for _ in range(15):
            ec.update(0.30, 0.30, now=tt)
            tt += dt
        idle_hp = ec.hold_progress
        hp_left = []
        for _ in range(9):                       # left wink: the 9th frame is ~267 ms in
            ec.update(0.08, 0.30, now=tt)
            hp_left.append(ec.hold_progress)
            tt += dt
        kind_l = ec.hold_kind
        ratios_ok = abs(ec.last_l_ratio - 0.08 / 0.30) < 0.02 and abs(ec.last_r_ratio - 1.0) < 0.02
        ec.update(0.30, 0.30, now=tt)            # reopen -> LEFT_CLICK recorded
        tt += dt
        check("expr: hold_progress scales over a left wink (80-450 ms)",
              idle_hp == 0.0 and hp_left[2] == 0.0 and 0.40 < hp_left[8] < 0.60
              and hp_left == sorted(hp_left) and kind_l == "L" and ratios_ok
              and ec.hold_progress == 0.0 and calls == ["left_down", "left_up"],
              f"(p@67ms={hp_left[2]:.2f}, p@267ms={hp_left[8]:.2f}, kind={kind_l}, after_reopen={ec.hold_progress:.2f})")

        hp_right = []
        for _ in range(9):                       # right wink, ~267 ms in on the 9th frame
            ec.update(0.30, 0.08, now=tt)
            hp_right.append(ec.hold_progress)
            tt += dt
        kind_r = ec.hold_kind
        ec.update(0.30, 0.30, now=tt)            # reopen -> RIGHT_CLICK recorded
        tt += dt
        check("expr: hold_progress scales over a right wink (80-450 ms)",
              hp_right[2] == 0.0 and 0.40 < hp_right[8] < 0.60 and kind_r == "R"
              and ec.hold_progress == 0.0 and calls[-1] == "right_click",
              f"(p@67ms={hp_right[2]:.2f}, p@267ms={hp_right[8]:.2f}, kind={kind_r}, after_reopen={ec.hold_progress:.2f})")

        for _ in range(31):                      # both eyes closed, the 31st frame is ~1.0 s in
            ec.update(0.05, 0.05, now=tt)
            tt += dt
        both_mid, kind_b = ec.hold_progress, ec.hold_kind
        locked_early = ec.center_locked
        ec.update(0.30, 0.30, now=tt)            # eyes reopen before 2 s: no lock, progress resets
        tt += dt
        check("expr: hold_progress scales over a both-eyes hold (0-2000 ms)",
              0.45 < both_mid < 0.55 and kind_b == "BOTH" and not locked_early
              and ec.hold_progress == 0.0 and not ec.center_locked,
              f"(p@1.0s={both_mid:.2f}, kind={kind_b}, after_reopen={ec.hold_progress:.2f})")

        ec.update(0.60, 0.60, now=tt)            # very wide eyes: ratio ~1.9 -> clamped to 1.5
        check("expr: eye ratios exposed and clamped to 0.0..1.5",
              ec.last_l_ratio == 1.5 and ec.last_r_ratio == 1.5 and 0.0 <= ec.hold_progress <= 1.0,
              f"(l={ec.last_l_ratio:.2f}, r={ec.last_r_ratio:.2f})")
    finally:
        for n, f in saved.items():
            setattr(win32_input, n, f)

    # Phase 4.1: left-click gate (notch button interception) swallows or passes real left events
    sent = []
    orig_mouse = win32_input._mouse
    win32_input._mouse = lambda flags, dx=0, dy=0: sent.append(flags)
    try:
        win32_input.set_left_gate(lambda kind: True)
        win32_input.left_down()
        win32_input.left_up()
        win32_input.left_click()
        swallowed = list(sent)
        win32_input.set_left_gate(lambda kind: False)
        win32_input.left_click()
        passed = list(sent)
    finally:
        win32_input.set_left_gate(None)
        win32_input._mouse = orig_mouse
    check("win32: left gate swallows when True, passes down+up when False",
          swallowed == [] and passed == [win32_input.MOUSEEVENTF_LEFTDOWN, win32_input.MOUSEEVENTF_LEFTUP],
          f"(swallowed={swallowed}, passed={passed})")

    # Phase 4.1: type_unicode_text structure (built, never sent)
    evs = win32_input.build_unicode_inputs("Hi\U0001F600")
    uni_ok = (len(evs) == 8
              and all(e.type == win32_input.INPUT_KEYBOARD for e in evs)
              and evs[0].u.ki.wVk == 0 and evs[0].u.ki.wScan == ord("H")
              and evs[0].u.ki.dwFlags == win32_input.KEYEVENTF_UNICODE
              and evs[1].u.ki.dwFlags == (win32_input.KEYEVENTF_UNICODE | win32_input.KEYEVENTF_KEYUP)
              and evs[4].u.ki.wScan == 0xD83D and evs[6].u.ki.wScan == 0xDE00)
    check("win32: type_unicode_text builds KEYEVENTF_UNICODE down/up pairs (surrogates split)", uni_ok,
          f"({len(evs)} events)")

    # Phase 4.2: send_vk / send_hotkey_chord event order (built, never sent)
    rec = []
    orig_sm = win32_input._send_many
    win32_input._send_many = lambda inputs: (rec.extend(inputs), len(inputs))[1]
    try:
        win32_input.send_vk(win32_input.VK_BACK)
        one = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in rec]
        rec.clear()
        win32_input.send_hotkey_chord([0x11, 0x41])
        two = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in rec]
    finally:
        win32_input._send_many = orig_sm
    UP = win32_input.KEYEVENTF_KEYUP
    check("win32: send_vk = down+up, send_hotkey_chord presses in order and releases in reverse",
          one == [(0x08, 0), (0x08, UP)] and two == [(0x11, 0), (0x41, 0), (0x41, UP), (0x11, UP)],
          f"({one}, {two})")

    # Phase 4.3: Voice AI hotkey bridge (events are built and recorded, never sent)
    import types
    vrec = []
    orig_sm2 = win32_input._send_many
    win32_input._send_many = lambda inputs: (vrec.extend(inputs), len(inputs))[1]
    try:
        win32_input.trigger_voice_ai("wispr_flow")
        wispr_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in vrec]
        vrec.clear()
        win32_input.trigger_voice_ai("win_h")
        winh_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in vrec]
        vrec.clear()
        win32_input.trigger_voice_ai("custom_chord", (0x11, 0x41))
        custom_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in vrec]
        vrec.clear()
        fake = types.SimpleNamespace(voice_active=False, cfg=cfg)
        CursorController._toggle_voice(fake)
        v_on = fake.voice_active
        CursorController._toggle_voice(fake)
        v_off = fake.voice_active
        CursorController._toggle_voice(fake)
        paused = types.SimpleNamespace(tracking_enabled=True, voice_active=fake.voice_active,
                                       expr=types.SimpleNamespace(release_all=lambda: None))
        CursorController.toggle(paused)          # F10 pause must reset the Voice AI indicator
    finally:
        win32_input._send_many = orig_sm2
    UPV = win32_input.KEYEVENTF_KEYUP
    check("voice: trigger_voice_ai wispr_flow = Ctrl+Win+Space, win_h = Win+H, custom chord",
          wispr_ev == [(0x11, 0), (0x5B, 0), (0x20, 0), (0x20, UPV), (0x5B, UPV), (0x11, UPV)]
          and winh_ev == [(0x5B, 0), (0x48, 0), (0x48, UPV), (0x5B, UPV)]
          and custom_ev == [(0x11, 0), (0x41, 0), (0x41, UPV), (0x11, UPV)],
          f"(wispr={wispr_ev}, win_h={winh_ev})")
    check("voice: cfg defaults (wispr_flow, Ctrl+Win+Space vks) and toggle flips idle <-> active, F10 pause resets",
          cfg.voice_mode == "wispr_flow" and tuple(cfg.voice_custom_vks) == (0x11, 0x5B, 0x20)
          and v_on is True and v_off is False and paused.voice_active is False,
          f"(on={v_on}, off={v_off}, after_pause={paused.voice_active})")

    # Phase 4 HUD: overlays build and paint, carry the no-focus + click-through styles, resolve states
    if win32_input.IS_WINDOWS:
        qapp = QApplication.instance() or QApplication(sys.argv)
        try:
            from ui.glass_hud import (WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
                                      WS_EX_TRANSPARENT, CursorRingHUD, DynamicIslandNotch,
                                      resolve_state, verify_win32_styles)
            notch, ring = DynamicIslandNotch(), CursorRingHUD()
            sized = (notch.width(), notch.height(), ring.width(), ring.height()) == (136, 28, 64, 64)
            paint_ok = True
            for key in ("TRACKING", "ACTION", "DRAG", "CHARGING", "LOCK", "PAUSED", "NOFACE"):
                notch.set_status(key, key, 0.4, 0.9, 30.0, 0.5, "L")
                ring.set_status(key, 0.5, "L")
                paint_ok = paint_ok and (not notch.grab().isNull()) and (not ring.grab().isNull())
            check("ui: notch 136x28 compact + ring 64x64 instantiate and paint all 7 states", sized and paint_ok,
                  f"(sized={sized}, painted={paint_ok})")

            need = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
            sn = verify_win32_styles(int(notch.winId()))
            sr = verify_win32_styles(int(ring.winId()))
            check("ui: WS_EX_NOACTIVATE + WS_EX_TRANSPARENT set on notch and ring",
                  (sn & need) == need and (sr & need) == need,
                  f"(notch=0x{sn:08X}, ring=0x{sr:08X})")

            # Phase 4.1: hover expands to 340x48, hit_test finds both buttons, leaving collapses back
            g = notch._geo
            cxp = (g.x() + g.width() / 2.0) * notch._dpr
            notch.update_hover(cxp, 5.0)
            for _ in range(80):
                notch.set_status("TRACKING", "TRACKING", 0.4, 0.9, 30.0, 0.0, "")
            expanded = (notch.width(), notch.height()) == (340, 48)
            kb_xy, st_xy = notch.button_center("KEYBOARD"), notch.button_center("VOICE")
            hit_kb = notch.hit_test(*kb_xy) == "KEYBOARD"
            hit_st = notch.hit_test(*st_xy) == "VOICE"
            hit_mid = notch.hit_test(cxp, kb_xy[1]) is None
            paint_exp = True
            for hot_xy, listening in ((kb_xy, ""), (st_xy, ""), (st_xy, "WISPR")):
                notch.update_hover(*hot_xy)
                notch.set_status("TRACKING", "TRACKING", 0.4, 0.9, 30.0, 0.5, "L", listening)
                paint_exp = paint_exp and (not notch.grab().isNull())
            notch.update_hover(cxp, 600.0)
            for _ in range(80):
                notch.set_status("TRACKING", "TRACKING", 0.4, 0.9, 30.0, 0.0, "")
            collapsed = (notch.width(), notch.height()) == (136, 28) and notch.hit_test(*kb_xy) is None
            check("ui: hover -> 340x48, hit_test KEYBOARD/VOICE, paints hot+listening, collapses to 136x28",
                  expanded and hit_kb and hit_st and hit_mid and paint_exp and collapsed,
                  f"(expanded={expanded}, kb={hit_kb}, voice={hit_st}, mid_none={hit_mid}, "
                  f"painted={paint_exp}, collapsed={collapsed})")

            # Phase 4.2: GlassOSK styles, key hit-testing, shift/sym toggles, click gate over OSK keys
            from ui.glass_hud import GlassOSK
            osk = GlassOSK()
            osk.show()
            so = verify_win32_styles(int(osk.winId()))
            hits = [osk.hit_test(*osk.key_center(t)) for t in ("SPACE", "BACKSPACE", "HIDE", "q")]
            keys_ok = hits == ["SPACE", "BACKSPACE", "HIDE", "q"]
            osk.toggle_shift()
            shift_ok = (osk.shift_active and osk.hit_test(*osk.key_center("Q")) == "Q"
                        and osk.key_center("q") is None)
            osk.toggle_shift()
            osk.toggle_sym()
            sym_ok = (osk.sym_active and osk.hit_test(*osk.key_center("!")) == "!"
                      and osk.key_center("q") is None)
            osk.toggle_sym()
            osk.flash_key("q")
            osk.update_hover(*osk.key_center("w"))
            osk_paint = not osk.grab().isNull()
            osk_ok = ((osk.width(), osk.height()) == (780, 250) and (so & need) == need
                      and keys_ok and shift_ok and sym_ok and osk_paint)
            check("ui: GlassOSK 780x250, NOACTIVATE|TRANSPARENT, hit_test SPACE/BACKSPACE/HIDE/letters, shift + sym toggles",
                  osk_ok, f"(styles=0x{so:08X}, hits={hits}, shift={shift_ok}, sym={sym_ok}, painted={osk_paint})")

            router = UiClickRouter(notch, osk)
            kx, ky = osk.key_center("q")
            d1 = router.gate("down", kx, ky)
            q1 = list(router.pending)
            u1 = router.gate("up", kx, ky)
            d2 = router.gate("down", kx, ky)             # double-click gesture on one key: swallowed, queued once
            q2 = list(router.pending)
            u2 = router.gate("up", kx, ky)
            router.pending.clear()
            gx, gy = (osk._ox + 3.0) * osk._dpr, (osk._oy + 3.0) * osk._dpr   # panel margin, no key there
            d3 = router.gate("down", gx, gy)
            q3 = list(router.pending)
            u3 = router.gate("up", gx, gy)
            d4 = router.gate("down", 5.0, 500.0)         # open desktop: the real click must pass through
            u4 = router.gate("up", 5.0, 500.0)
            osk.hide()
            d5 = router.gate("down", kx, ky)             # hidden keyboard: no interception
            u5 = router.gate("up", kx, ky)
            gate_ok = (d1 and u1 and d2 and u2 and d3 and u3 and not d4 and not u4 and not d5 and not u5
                       and q1 == [("OSK", "q")] and q2 == [("OSK", "q")] and q3 == [])
            osk.close()
            check("ui: click gate swallows OS clicks over visible OSK keys + panel, passes elsewhere, ignores hidden OSK",
                  gate_ok, f"(q1={q1}, q2={q2}, q3={q3}, d4={d4}, d5={d5})")

            # Phase 5: CTRL / ALT keys on row 4 and sticky one-shot shortcut chords (events recorded, never sent)
            osk2 = GlassOSK()
            osk2.show()                                  # hide() only fires hideEvent (unlatch) on a visible widget
            hits2 = [osk2.hit_test(*osk2.key_center(t)) for t in ("CTRL", "ALT")]
            vk_ok = (win32_input.char_to_vk("c") == 0x43 and win32_input.char_to_vk("A") == 0x41
                     and win32_input.char_to_vk("5") == 0x35 and win32_input.char_to_vk("-") == 0xBD
                     and win32_input.char_to_vk(".") == 0xBE and win32_input.char_to_vk("/") == 0xBF
                     and win32_input.char_to_vk(",") == 0xBC and win32_input.char_to_vk("SPACE") == 0x20
                     and win32_input.char_to_vk("ENTER") == 0x0D and win32_input.char_to_vk("BACKSPACE") == 0x08
                     and win32_input.char_to_vk("TAB") == 0x09 and win32_input.char_to_vk("~") is None)
            check("ui: GlassOSK row 4 exposes CTRL and ALT (hit_test) + char_to_vk mapping",
                  hits2 == ["CTRL", "ALT"] and vk_ok, f"(hits={hits2}, vk_ok={vk_ok})")

            prec = []
            orig_sm3 = win32_input._send_many
            win32_input._send_many = lambda inputs: (prec.extend(inputs), len(inputs))[1]
            try:
                fake_c = types.SimpleNamespace(osk=osk2)
                UPK = win32_input.KEYEVENTF_KEYUP
                CursorController._osk_key(fake_c, "CTRL")
                latched_c = osk2.ctrl_active
                CursorController._osk_key(fake_c, "c")
                ctrl_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in prec]
                ctrl_off = not osk2.ctrl_active
                prec.clear()
                CursorController._osk_key(fake_c, "ALT")
                latched_a = osk2.alt_active
                CursorController._osk_key(fake_c, "a")
                alt_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in prec]
                alt_off = not osk2.alt_active
                prec.clear()
                CursorController._osk_key(fake_c, "CTRL")
                CursorController._osk_key(fake_c, "ALT")
                CursorController._osk_key(fake_c, "a")
                both_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in prec]
                both_off = not osk2.ctrl_active and not osk2.alt_active
                prec.clear()
                CursorController._osk_key(fake_c, "CTRL")
                osk2.hide()
                hide_off = not osk2.ctrl_active and not osk2.alt_active
            finally:
                win32_input._send_many = orig_sm3
            check("ui: sticky CTRL + c -> Ctrl+C chord and auto-unlatch",
                  latched_c and ctrl_off
                  and ctrl_ev == [(0x11, 0), (0x43, 0), (0x43, UPK), (0x11, UPK)],
                  f"({ctrl_ev})")
            check("ui: sticky ALT + a -> Alt+A chord, CTRL+ALT+a -> Ctrl+Alt+A, auto-unlatch, hide unlatches",
                  latched_a and alt_off and both_off and hide_off
                  and alt_ev == [(0x12, 0), (0x41, 0), (0x41, UPK), (0x12, UPK)]
                  and both_ev == [(0x11, 0), (0x12, 0), (0x41, 0), (0x41, UPK), (0x12, UPK), (0x11, UPK)],
                  f"(alt={alt_ev}, both={both_ev})")
            # Phase 5.1: ESC key on row 1, ESC -> VK_ESCAPE, plain ESC quits, latched CTRL + ESC is a chord
            osk2.show()
            esc_xy = osk2.key_center("ESC")
            hit_esc = esc_xy is not None and osk2.hit_test(*esc_xy) == "ESC"
            check("ui: GlassOSK row 1 exposes ESC (hit_test) + char_to_vk('ESC') == 0x1B",
                  hit_esc and win32_input.char_to_vk("ESC") == 0x1B, f"(hit={hit_esc})")
            quits, qrec = [], []
            orig_sm4 = win32_input._send_many
            win32_input._send_many = lambda inputs: (qrec.extend(inputs), len(inputs))[1]
            try:
                fake_q = types.SimpleNamespace(osk=osk2, _quit_app=lambda: quits.append(1))
                CursorController._osk_key(fake_q, "ESC")
                plain_quit = quits == [1] and qrec == []
                CursorController._osk_key(fake_q, "CTRL")
                CursorController._osk_key(fake_q, "ESC")
                esc_ev = [(e.u.ki.wVk, e.u.ki.dwFlags) for e in qrec]
                chord_ok = quits == [1] and not osk2.ctrl_active
            finally:
                win32_input._send_many = orig_sm4
            UPE = win32_input.KEYEVENTF_KEYUP
            check("ui: ESC quits when unlatched, latched CTRL + ESC fires the chord and unlatches",
                  plain_quit and chord_ok and esc_ev == [(0x11, 0), (0x1B, 0), (0x1B, UPE), (0x11, UPE)],
                  f"(quits={quits}, chord={esc_ev})")
            osk2.close()

            t0 = 1000.0
            rs = [
                resolve_state(True, True, False, False, 0.0, "", None, t0)[0],
                resolve_state(True, True, False, False, 0.0, "", ("LEFT_CLICK", t0 - 0.10), t0)[0],
                resolve_state(True, True, False, False, 0.0, "", ("LEFT_CLICK", t0 - 0.50), t0)[0],
                resolve_state(True, True, False, False, 0.0, "", ("DRAG_DROP", t0 - 0.10), t0)[0],
                resolve_state(True, True, False, True, 0.0, "", None, t0)[0],
                resolve_state(True, True, False, False, 0.5, "L", None, t0)[0],
                resolve_state(True, True, False, False, 0.1, "BOTH", None, t0)[0],
                resolve_state(True, True, True, False, 0.0, "", None, t0)[0],
                resolve_state(False, True, False, False, 0.0, "", None, t0)[0],
                resolve_state(True, False, False, False, 0.0, "", None, t0)[0],
            ]
            want = ["TRACKING", "ACTION", "TRACKING", "TRACKING", "DRAG", "CHARGING",
                    "TRACKING", "LOCK", "PAUSED", "NOFACE"]
            check("ui: state resolution (tracking, action flash, drag, charging, lock, paused, no face)",
                  rs == want, f"({rs})")
            notch.close()
            ring.close()
        except Exception as ex:
            check("ui: HUD checks", False, "\n" + repr(ex))

    # Model file present (required by FaceLandmarker)
    import os
    from core.vision_thread import MODEL_PATH
    check("face model file", os.path.exists(MODEL_PATH), f"({MODEL_PATH})")

    # Vision pipeline headless (never touches the cursor)
    if win32_input.IS_WINDOWS:
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        vt, got, errs = VisionThread(cfg), [], []
        vt.telemetry_signal.connect(got.append)
        vt.error_signal.connect(errs.append)
        vt.start()
        loop = QEventLoop()
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        vt.stop()
        if errs:
            check("vision pipeline", False, "\n" + errs[0][:600])
        else:
            fps = got[-1].fps if got else 0.0
            check("vision pipeline", len(got) > 10, f"({len(got)} frames, ~{fps:.0f} fps)")

    ok = all(results)
    print("VERIFY:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="headless self-test")
    args = ap.parse_args()
    if args.verify:
        return run_verify()

    cfg = FacePilotConfig()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    vt = VisionThread(cfg)
    cursor = CursorController(cfg.tracking_enabled, cfg)
    cursor.vision = vt
    vt.telemetry_signal.connect(cursor.on_telemetry, Qt.ConnectionType.DirectConnection)
    vt.error_signal.connect(lambda m: (print("ERROR:", m), app.quit()))

    bridge = KillBridge()
    bridge.kill.connect(app.quit)
    keyboard.add_hotkey(cfg.kill_switch, bridge.kill.emit)
    keyboard.add_hotkey(cfg.recenter_key, vt.request_recenter)
    keyboard.add_hotkey(cfg.recenter_key, cursor.expr.calibrate)
    keyboard.add_hotkey(cfg.toggle_key, cursor.toggle)

    app.aboutToQuit.connect(vt.stop)
    app.aboutToQuit.connect(cursor.expr.release_all)
    app.aboutToQuit.connect(cursor.stop_voice)
    app.aboutToQuit.connect(keyboard.unhook_all)

    vt.start()
    print(f"FacePilot running. [{cfg.kill_switch.upper()}] = exit, [{cfg.recenter_key.upper()}] = recenter, [{cfg.toggle_key.upper()}] = toggle")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
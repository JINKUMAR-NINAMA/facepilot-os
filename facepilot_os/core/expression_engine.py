import time

from core import win32_input


class ExpressionController:
    """
    Classic-trackpad wink engine with live diagnostics. Call update() once per telemetry frame.
    Returns True ONLY while center-locked. 0 ms freeze: winks and drags never pause the cursor.

    Symmetric blinks            -> ignored (no click, no freeze)
    Left wink   80-450 ms       -> left_down at 80 ms, left_up on reopen (= LEFT_CLICK)
    Left hold  >= 450 ms        -> DRAG_START at 450 ms (button stays down); left_up after 2 reopened frames (l_ratio >= 0.76)
    Right wink  80-450 ms       -> RIGHT_CLICK on the reopen frame (250 ms cooldown)
    Right hold >= 450 ms        -> DOUBLE_CLICK at the 450 ms mark (no RIGHT_CLICK on reopen)
    """

    def __init__(self, swap_eyes: bool = False, verbose: bool = True) -> None:
        self.swap_eyes = swap_eyes
        self.verbose = verbose

        # ratio thresholds (ear / personal open-eye baseline)
        self.blink_ratio = 0.65      # both below this (and similar) = natural blink
        self.blink_gap = 0.12
        self.wink_ratio = 0.68       # winking eye below this
        self.wink_gap = 0.12         # other eye must be at least this much higher
        self.reopen_ratio = 0.76     # drag ends after drag_reopen_frames consecutive frames with left ratio >= this

        # baseline capture gates (absolute EAR)
        self.open_min = 0.22
        self.open_stable = 0.05
        self.recal_min = 0.15

        # timing (seconds)
        self.wink_min = 0.080
        self.hold_threshold = 0.450         # held this long: left wink -> DRAG, right wink -> DOUBLE_CLICK
        self.drag_reopen_frames = 2          # consecutive reopened frames (~35 ms) that end a drag
        # right_click_max removed: a right wink held >= hold_threshold is a DOUBLE_CLICK
        self.right_cooldown = 0.250         # min gap between two RIGHT_CLICKs
        # right_wait removed: no right-pending buffer
        self.reopen_debounce = 0.0
        self.freeze_ms = 0.0               # strictly 0 ms: only the center lock ever freezes the cursor
        self.status_period = 0.250
        self.both_hold_recenter_ms: float = 2000.0   # both eyes closed this long -> toggle center lock
        self.both_gap = 0.18                         # max |l_ratio - r_ratio| for a symmetric closure
        self.both_reopen_ratio = 0.70                # an eye above this ratio ends the closure

        # baselines: updated ONLY while both eyes are wide open and stable, or on F8
        self.left_base = None
        self.right_base = None
        self._recal = False

        # state
        self.drag_active = False
        self.freeze_until = 0.0
        self._side = None            # 'L' / 'R' while a wink is in progress
        self._start = 0.0
        self._last_true = 0.0
        self._second = False         # legacy latch, always False (right double-click override removed)
        self._last_right_click = -1e9        # time of the last RIGHT_CLICK (cooldown)
        self._drag_open_since = None
        self._drag_start_time = 0.0
        self.left_button_down = False        # physical left button held (from 80 ms of a left wink until reopen)
        self.right_hold_fired = False        # DOUBLE_CLICK already fired during this right wink
        self._drag_open_frames = 0           # consecutive reopened frames while dragging
        self._last_print = 0.0
        self.both_closed_start: float | None = None
        self.both_hold_fired: bool = False
        self.center_locked: bool = False
        self.pending_action: str | None = None
        # HUD read-outs (Phase 4), refreshed every update()
        self.last_l_ratio: float = 0.0       # clamped 0.0..1.5
        self.last_r_ratio: float = 0.0
        self.hold_progress: float = 0.0      # 0.0..1.0
        self.hold_kind: str = ""             # "BOTH" / "L" / "R" while hold_progress > 0
        self.state_name: str = "IDLE"
        self.last_action = None              # (name, perf_counter time) of the last click-type action
        # HUD read-outs (Phase 4), refreshed every update()
        self.last_l_ratio: float = 0.0       # clamped 0.0..1.5
        self.last_r_ratio: float = 0.0
        self.hold_progress: float = 0.0      # 0.0..1.0
        self.hold_kind: str = ""             # "BOTH" / "L" / "R" while hold_progress > 0
        self.state_name: str = "IDLE"
        self.last_action = None              # (name, perf_counter time) of the last click-type action

    # ------------------------------------------------------------------ public
    def consume_action(self):
        """Return and clear the last high-level action ("CENTER_LOCK" / "CENTER_UNLOCK"), or None."""
        action, self.pending_action = self.pending_action, None
        return action

    def calibrate(self) -> None:
        """Re-capture open-eye baselines on the next frame (keep both eyes open)."""
        self._recal = True

    def is_frozen(self, now: float = None) -> bool:
        if now is None:
            now = time.perf_counter()
        if self.center_locked:
            return True
        return False                     # 0 ms: only center_locked ever freezes the cursor

    def update(self, left_ear: float, right_ear: float, now: float = None) -> bool:
        if now is None:
            now = time.perf_counter()
        if self.swap_eyes:
            left_ear, right_ear = right_ear, left_ear

        self._update_baseline(left_ear, right_ear)

        if not self.left_base or not self.right_base:
            self._status(now, f"[EYES] waiting for baseline: open both eyes wide or press F8 "
                              f"| L_ear: {left_ear:.2f} | R_ear: {right_ear:.2f}")
            return False

        lr = left_ear / self.left_base
        rr = right_ear / self.right_base
        self.last_l_ratio = min(1.5, max(0.0, lr))
        self.last_r_ratio = min(1.5, max(0.0, rr))
        self.last_l_ratio = min(1.5, max(0.0, lr))
        self.last_r_ratio = min(1.5, max(0.0, rr))

        natural = (lr < self.blink_ratio and rr < self.blink_ratio
                   and abs(lr - rr) < self.blink_gap)
        l_wink = (not natural and lr < self.wink_ratio and (rr - lr) >= self.wink_gap)
        r_wink = (not natural and rr < self.wink_ratio and (lr - rr) >= self.wink_gap)

        both_closed = (lr < self.blink_ratio and rr < self.blink_ratio
                       and abs(lr - rr) < self.both_gap)
        eye_reopened = lr >= self.both_reopen_ratio or rr >= self.both_reopen_ratio
        self._update_both_hold(both_closed, eye_reopened, now)
        self._update_progress(now)       # also covers the center-locked early return below
        self._update_progress(now)       # also covers the center-locked early return below

        if self.center_locked:
            state = "CENTER_LOCKED"
        elif self.drag_active:
            state = "DRAG"
        elif self._side is not None:
            state = "LEFT_WINK" if self._side == "L" else "RIGHT_WINK"
        elif natural:
            state = "BLINK_IGNORED"
        else:
            state = "IDLE"
        self.state_name = state
        self.state_name = state
        self._status(now, f"[EYES] L_ratio: {lr:.2f} | R_ratio: {rr:.2f} "
                          f"| diff: {lr - rr:+.2f} | state: {state}")

        # no right-pending buffer: RIGHT_CLICK fires on the reopen frame

        if self.center_locked:
            return True                  # locked: no clicks, no cursor movement

        if self.drag_active:
            self._update_drag(lr, now)
        elif both_closed:
            self._abort_left(now)        # symmetric closure: never leave a pressed left button stuck
            self._side = None            # symmetric closure: cancel any wink in progress, no clicks
            self._second = False
        else:
            self._update_wink(l_wink, r_wink, now)
        self._update_progress(now)
        self._update_progress(now)
        return self.is_frozen(now)

    def release_all(self) -> None:
        """Safety: never leave the mouse button held or a latch active."""
        if self.drag_active or self.left_button_down:
            win32_input.left_up()
        self.left_button_down = False
        self.right_hold_fired = False
        self._drag_open_frames = 0
        self.drag_active = False
        self._side = None
        self._second = False
        self._drag_open_since = None
        self.freeze_until = 0.0
        self.both_closed_start = None    # center_locked itself is deliberately kept
        self.hold_progress = 0.0
        self.hold_kind = ""
        self.hold_progress = 0.0
        self.hold_kind = ""
        self.both_hold_fired = False

    # ----------------------------------------------------------------- internal
    def _status(self, now: float, text: str) -> None:
        if not self.verbose or now - self._last_print < self.status_period:
            return
        self._last_print = now
        print("\r" + text.ljust(95), end="", flush=True)

    def _event(self, text: str) -> None:
        if self.verbose:
            print("\n" + text, flush=True)

    def _update_progress(self, now: float) -> None:
        """Publish hold progress (0..1) and its kind ("BOTH" / "L" / "R" / "") for the HUD."""
        p, kind = 0.0, ""
        if self.both_closed_start is not None and not self.both_hold_fired:
            p = min(1.0, (now - self.both_closed_start) * 1000.0 / self.both_hold_recenter_ms)
            kind = "BOTH"
        elif (self._side is not None and not self.drag_active
              and not (self._side == "R" and self.right_hold_fired)):
            lo, hi = self.wink_min * 1000.0, self.hold_threshold * 1000.0
            p = min(1.0, max(0.0, ((now - self._start) * 1000.0 - lo) / (hi - lo)))
            kind = self._side
        self.hold_progress = p
        self.hold_kind = kind if p > 0.0 else ""

    def _update_progress(self, now: float) -> None:
        """Publish hold progress (0..1) and its kind ("BOTH" / "L" / "R" / "") for the HUD."""
        p, kind = 0.0, ""
        if self.both_closed_start is not None and not self.both_hold_fired:
            p = min(1.0, (now - self.both_closed_start) * 1000.0 / self.both_hold_recenter_ms)
            kind = "BOTH"
        elif (self._side is not None and not self.drag_active
              and not (self._side == "R" and self.right_hold_fired)):
            lo, hi = self.wink_min * 1000.0, self.hold_threshold * 1000.0
            p = min(1.0, max(0.0, ((now - self._start) * 1000.0 - lo) / (hi - lo)))
            kind = self._side
        self.hold_progress = p
        self.hold_kind = kind if p > 0.0 else ""

    def _abort_left(self, now: float) -> None:
        """Both eyes closed while a pre-drag left press is held: release it so the button never sticks."""
        if self.left_button_down and not self.drag_active:
            self.left_button_down = False
            self._fire("LEFT_RELEASE_ON_BLINK", win32_input.left_up, int((now - self._start) * 1000))
        self.right_hold_fired = False

    def _fire(self, name: str, fn, dur_ms: int) -> None:
        self.last_action = (name, time.perf_counter())
        self.last_action = (name, time.perf_counter())
        self._event(f">>> [ACTION FIRED] {name} (duration={dur_ms}ms)")
        fn()

    def _update_both_hold(self, both_closed: bool, eye_reopened: bool, now: float) -> None:
        if both_closed:
            if self.both_closed_start is None:
                self.both_closed_start = now
                self.both_hold_fired = False
            held_ms = (now - self.both_closed_start) * 1000.0
            if not self.both_hold_fired and held_ms >= self.both_hold_recenter_ms:
                self.both_hold_fired = True
                self.center_locked = not self.center_locked
                self._side = None
                self._second = False
                if self.drag_active:     # never leave the mouse button held across a lock toggle
                    self.drag_active = False
                    self._drag_open_since = None
                    self._fire("DRAG_DROP", win32_input.left_up,
                               int((now - self._drag_start_time) * 1000))
                    self.left_button_down = False
                if self.center_locked:
                    self.pending_action = "CENTER_LOCK"
                    self._event(">>> [ACTION FIRED] CENTER_LOCK_ENGAGED (cursor frozen at screen center)")
                else:
                    self.pending_action = "CENTER_UNLOCK"
                    self._event(">>> [ACTION FIRED] CENTER_LOCK_RELEASED (head neutral pose re-centered at screen center)")
        elif eye_reopened:
            self.both_closed_start = None
            self.both_hold_fired = False

    def _update_baseline(self, le: float, re: float) -> None:
        if self._recal:
            self._recal = False
            if le >= self.recal_min and re >= self.recal_min:
                self.left_base, self.right_base = le, re
                self._event(f">>> [BASELINE SET] L={le:.3f} R={re:.3f}")
                return
        if le > self.open_min and re > self.open_min and abs(le - re) < self.open_stable:
            if self.left_base is None or self.right_base is None:
                self.left_base, self.right_base = le, re
            else:
                self.left_base += 0.05 * (le - self.left_base)
                self.right_base += 0.05 * (re - self.right_base)

    def _update_drag(self, lr: float, now: float) -> None:
        if lr >= self.reopen_ratio:           # reopened (>= 0.76): needs drag_reopen_frames consecutive frames
            self._drag_open_frames += 1
            if self._drag_open_frames >= self.drag_reopen_frames:
                self._drag_open_frames = 0
                self.drag_active = False
                self.left_button_down = False
                self._drag_open_since = None
                held = int((now - self._drag_start_time) * 1000)
                self._fire("DRAG_DROP", win32_input.left_up, held)
        else:
            self._drag_open_frames = 0      # flicker: a lone reopened frame is forgotten

    def _update_wink(self, l_wink: bool, r_wink: bool, now: float) -> None:
        if self._side is None:
            side = "L" if l_wink else ("R" if r_wink else None)
            if side is None:
                return
            self._side = side
            self._start = now
            self._last_true = now
            self._second = False     # legacy latch, never True
            self.right_hold_fired = False
            # 0 ms freeze: nothing is frozen on wink onset
            self._event(">>> [LEFT WINK] started" if side == "L" else ">>> [RIGHT WINK] started")
            return

        still = l_wink if self._side == "L" else r_wink
        if still:
            self._last_true = now
            held_s = now - self._start
            if self._side == "L":
                if not self.left_button_down and held_s >= self.wink_min:
                    self.left_button_down = True        # physical press at 80 ms
                    self._event(">>> [LEFT WINK] 80 ms reached: left_down")
                    win32_input.left_down()
                if held_s >= self.hold_threshold:
                    self._side = None
                    self.drag_active = True
                    self._drag_open_since = None
                    self._drag_open_frames = 0
                    self._drag_start_time = now
                    self._event(f">>> [ACTION FIRED] DRAG_START (duration={int(held_s * 1000)}ms, button already held)")
            elif not self.right_hold_fired and held_s >= self.hold_threshold:
                self.right_hold_fired = True
                self.last_action = ("DOUBLE_CLICK", time.perf_counter())
                self.last_action = ("DOUBLE_CLICK", time.perf_counter())
                self._event(f">>> [ACTION FIRED] DOUBLE_CLICK (duration={int(held_s * 1000)}ms)")
                win32_input.left_click()
                win32_input.left_click()
            return

        if now - self._last_true < self.reopen_debounce:
            return                               # inert (reopen_debounce = 0.0): act on the first reopened frame

        # wink ended (eye reopened)
        dur = now - self._start
        dur_ms = int(dur * 1000)
        side, second = self._side, self._second
        self._side = None
        self._second = False

        if side == "L":
            if self.left_button_down:
                # left_down was sent at 80 ms; left_up now completes a native click
                self.left_button_down = False
                self._fire("LEFT_CLICK", win32_input.left_up, dur_ms)
            else:
                self._event(f">>> [WINK IGNORED] LEFT duration={dur_ms}ms (needs >= 80ms)")
        elif self.right_hold_fired:
            self.right_hold_fired = False       # DOUBLE_CLICK already fired at 450 ms: no RIGHT_CLICK on reopen
            self._event(f">>> [RIGHT WINK] hold released after DOUBLE_CLICK (duration={dur_ms}ms)")
        elif self.wink_min <= dur < self.hold_threshold:
            if now - self._last_right_click >= self.right_cooldown:
                self._last_right_click = now
                self._fire("RIGHT_CLICK", win32_input.right_click, dur_ms)
            else:
                gap_ms = int((now - self._last_right_click) * 1000)
                self._event(f">>> [WINK IGNORED] RIGHT cooldown ({gap_ms}ms < 250ms)")
        else:
            self._event(f">>> [WINK IGNORED] RIGHT duration={dur_ms}ms (needs 80-450ms)")
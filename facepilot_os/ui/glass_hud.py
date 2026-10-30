"""FacePilot OS Phase 4.1: click-through glass overlays (compact Dynamic Island notch + cursor ring).

Both windows are frameless, always on top, never take focus and never receive mouse input.
The notch buttons are purely visual: CursorController intercepts the left click via hit_test().
Call set_status() / update_hover() / move_to() from the Qt main thread only (hit_test is pure math).
"""
import ctypes
import sys
import time
from ctypes import wintypes

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QLinearGradient, QPainter,
                         QPainterPath, QPen, QRadialGradient, QRegion)
from PyQt6.QtWidgets import QWidget

IS_WINDOWS = sys.platform == "win32"
ENABLE_BLUR = False  # DWM blur disabled: Windows 11 draws a black box behind the rounded glass; plain translucent glass

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
_SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED

ACCENT = {
    "TRACKING": "#F5F5F7",   # pearl white
    "ACTION": "#32D74B",     # mint
    "DRAG": "#FFD60A",       # champagne gold
    "CHARGING": "#FFD60A",   # champagne gold
    "LOCK": "#FF453A",       # system coral
    "PAUSED": "#8E8E93",     # titanium gray
    "NOFACE": "#8E8E93",     # titanium gray
}
CYAN = "#F5F5F7"       # pearl (name kept: tracking / hover accent)
BLUE = "#E5E5EA"       # silver (end of the telemetry bar gradient)
EMERALD = "#32D74B"
AMBER = "#FF9F0A"
ROSE = "#FF453A"
FLASH_ACTIONS = ("LEFT_CLICK", "RIGHT_CLICK", "DOUBLE_CLICK")
FLASH_SECONDS = 0.35
BUTTON_FLASH_SECONDS = 0.30

_ALIGN_C = Qt.AlignmentFlag.AlignCenter.value

_U32 = None


def _user32():
    global _U32
    if not IS_WINDOWS:
        return None
    if _U32 is None:
        u = ctypes.windll.user32
        get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        put = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        put.restype = ctypes.c_ssize_t
        pos = u.SetWindowPos
        pos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        pos.restype = wintypes.BOOL
        _U32 = (get, put, pos)
    return _U32


def verify_win32_styles(hwnd: int) -> int:
    """Return the current GWL_EXSTYLE bitmask of hwnd (0 when not on Windows)."""
    fns = _user32()
    if fns is None or not hwnd:
        return 0
    return int(fns[0](hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF


def apply_win32_overlay_styles(hwnd: int, click_through: bool) -> int:
    """LAYERED | TOOLWINDOW | NOACTIVATE always; TRANSPARENT only when click_through. Returns the new bitmask."""
    fns = _user32()
    if fns is None or not hwnd:
        return 0
    get, put, pos = fns
    style = int(get(hwnd, GWL_EXSTYLE))
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    put(hwnd, GWL_EXSTYLE, style)
    pos(hwnd, None, 0, 0, 0, 0, _SWP_FLAGS)
    return verify_win32_styles(hwnd)


class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCA_DATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def _enable_win32_acrylic_blur(hwnd: int, tint: int = 0x55121216) -> bool:
    """Best effort: DWM acrylic (state 4) then blur-behind (state 3) with a subtle tint (AABBGGRR). Never raises."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(_WCA_DATA)]
        fn.restype = wintypes.BOOL
        for state in (4, 3):                     # ACCENT_ENABLE_ACRYLICBLURBEHIND, ACCENT_ENABLE_BLURBEHIND
            pol = _ACCENT_POLICY(state, 0, tint, 0)
            data = _WCA_DATA(19, ctypes.cast(ctypes.byref(pol), ctypes.c_void_p), ctypes.sizeof(pol))
            if fn(hwnd, ctypes.byref(data)):     # WCA_ACCENT_POLICY = 19
                return True
    except Exception:
        pass
    return False


def _round_mask(widget, w: float, h: float, radius: float) -> None:
    """Clip the window (and so the DWM blur) to a rounded rectangle instead of a blurred box."""
    if not ENABLE_BLUR:
        return
    try:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, float(w), float(h)), radius, radius)
        widget.setMask(QRegion(path.toFillPolygon().toPolygon()))
    except Exception:
        pass


def progress_visible(progress: float, kind: str, arc: bool = False) -> bool:
    """A natural blink briefly charges the both-eyes timer, so BOTH needs >= 0.20 (400 ms) before it shows."""
    floor = 0.20 if kind == "BOTH" else (0.02 if arc else 0.05)
    return progress > floor


def arc_color(state: str, kind: str) -> str:
    return ROSE if (kind == "BOTH" or state == "LOCK") else AMBER


def resolve_state(tracking_enabled, face_detected, center_locked, drag_active,
                  hold_progress, hold_kind, last_action, now):
    """Pure function: engine read-outs -> (state key, notch label)."""
    if not tracking_enabled:
        return "PAUSED", "PAUSED"
    if not face_detected:
        return "NOFACE", "NO FACE"
    charging = progress_visible(hold_progress, hold_kind)
    if center_locked:
        return "LOCK", ("UNLOCKING" if (charging and hold_kind == "BOTH") else "CENTER LOCKED")
    if drag_active:
        return "DRAG", "DRAG ACTIVE"
    if last_action is not None and last_action[0] in FLASH_ACTIONS and (now - last_action[1]) < FLASH_SECONDS:
        return "ACTION", last_action[0].replace("_", " ")
    if charging:
        label = {"BOTH": "LOCKING", "L": "DRAG ARMING", "R": "DOUBLE ARMING"}.get(hold_kind, "HOLDING")
        return "CHARGING", label
    return "TRACKING", "TRACKING"


class _OverlayBase(QWidget):
    BLUR = False                 # subclasses that are glass panels (notch, keyboard) set True

    def __init__(self) -> None:
        super().__init__(None, (Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool
                                | Qt.WindowType.WindowDoesNotAcceptFocus))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        apply_win32_overlay_styles(int(self.winId()), True)
        if self.BLUR and ENABLE_BLUR:
            _enable_win32_acrylic_blur(int(self.winId()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_win32_overlay_styles(int(self.winId()), True)   # Qt may rewrite styles while showing
        if self.BLUR and ENABLE_BLUR:
            _enable_win32_acrylic_blur(int(self.winId()))


class DynamicIslandNotch(_OverlayBase):
    """Compact 136x28 pill (state dot + L/R bars). Cursor hover expands it to 340x48 with two buttons.

    The window itself stays 100% click-through; hit_test() tells CursorController which button the cursor is on.
    """

    COMPACT = (136, 28)
    EXPANDED = (340, 48)
    TOP_Y = 8
    BLUR = True

    def __init__(self) -> None:
        super().__init__()
        scr = QGuiApplication.primaryScreen()
        self._geo = scr.geometry() if scr is not None else QRectF(0, 0, 1920, 1080).toRect()
        self._dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
        self.btn_kb_rect = QRectF(8.0, 10.0, 96.0, 28.0)      # widget-local, expanded layout
        self.btn_stt_rect = QRectF(236.0, 10.0, 96.0, 28.0)
        self._state = "TRACKING"
        self._label = "TRACKING"
        self._l = 0.0
        self._r = 0.0
        self._fps = 0.0
        self._progress = 0.0
        self._kind = ""
        self._hover = False
        self._hot = None
        self._listening = False
        self._pulse = 0
        self._flash = ("", 0.0)
        self._w = float(self.COMPACT[0])
        self._h = float(self.COMPACT[1])
        self._iw, self._ih = self.COMPACT
        self._key = None
        self._f_small = QFont("Segoe UI")
        self._f_small.setPointSizeF(6.0)
        self._f_small.setBold(True)
        self._f_btn = QFont("Segoe UI")
        self._f_btn.setPointSizeF(7.5)
        self._f_btn.setBold(True)
        self._place(*self.COMPACT)

    def _place(self, w: int, h: int) -> None:
        g = self._geo
        self._iw, self._ih = w, h
        self.setGeometry(g.x() + (g.width() - w) // 2, g.y() + self.TOP_Y, w, h)
        _round_mask(self, w, h, h / 2.0)

    def update_hover(self, x_px: float, y_px: float) -> None:
        """Main thread: expand while the cursor is near the top-center notch (hysteresis: wider zone once open)."""
        g = self._geo
        lx, ly = x_px / self._dpr, y_px / self._dpr
        dxa = abs(lx - (g.x() + g.width() / 2.0))
        dya = ly - g.y()
        if self._hover:
            inside = dxa <= 180.0 and dya <= 64.0
        else:
            inside = dxa <= 90.0 and dya <= 40.0
        self._hover = inside
        self._hot = self.hit_test(x_px, y_px) if inside else None

    def hit_test(self, x_px: float, y_px: float):
        """'KEYBOARD' / 'VOICE' when the cursor is over a button of the fully expanded notch, else None."""
        if not self._hover or self._iw < self.EXPANDED[0] - 4:
            return None
        g = self._geo
        lx = x_px / self._dpr - (g.x() + (g.width() - self._iw) // 2)
        ly = y_px / self._dpr - (g.y() + self.TOP_Y)
        pt = QPointF(lx, ly)
        if self.btn_kb_rect.contains(pt):
            return "KEYBOARD"
        if self.btn_stt_rect.contains(pt):
            return "VOICE"
        return None

    def button_center(self, which: str):
        """Physical-pixel screen center of a button in the expanded layout (used by --verify)."""
        g = self._geo
        r = self.btn_kb_rect if which == "KEYBOARD" else self.btn_stt_rect
        ox = g.x() + (g.width() - self.EXPANDED[0]) // 2
        oy = g.y() + self.TOP_Y
        return (ox + r.center().x()) * self._dpr, (oy + r.center().y()) * self._dpr

    def flash(self, which: str) -> None:
        self._flash = (which, time.perf_counter())

    def _flash_name(self, now: float) -> str:
        return self._flash[0] if (now - self._flash[1]) < BUTTON_FLASH_SECONDS else ""

    def set_status(self, state, label, l_ratio, r_ratio, fps, progress=0.0, kind="", listening=False) -> None:
        tw, th = self.EXPANDED if self._hover else self.COMPACT
        self._w += (tw - self._w) * 0.3
        self._h += (th - self._h) * 0.3
        if abs(tw - self._w) < 0.6:
            self._w = float(tw)
        if abs(th - self._h) < 0.6:
            self._h = float(th)
        iw, ih = int(round(self._w)), int(round(self._h))
        now = time.perf_counter()
        pulse = (int(now * 5.0) % 2) if listening else 0
        key = (state, round(l_ratio, 2), round(r_ratio, 2), round(progress, 2), kind, iw, ih,
               self._hot, listening or "", pulse, self._flash_name(now))
        if key == self._key:
            return
        self._key = key
        self._state, self._label = state, label
        self._l, self._r, self._fps = l_ratio, r_ratio, fps
        self._progress, self._kind = progress, kind
        self._listening, self._pulse = (listening or ""), pulse
        if (iw, ih) != (self.width(), self.height()):
            self._place(iw, ih)
        self.update()

    def _draw_meters(self, p, dot_x: float, bars_x: float, bars_end: float, cy: float, accent: QColor) -> None:
        halo = QRadialGradient(QPointF(dot_x, cy), 9.0)
        c0, c1 = QColor(accent), QColor(accent)
        c0.setAlpha(90)
        c1.setAlpha(0)
        halo.setColorAt(0.0, c0)
        halo.setColorAt(1.0, c1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(dot_x, cy), 9.0, 9.0)
        p.setBrush(accent)
        p.drawEllipse(QPointF(dot_x, cy), 3.5, 3.5)      # 7 px state dot

        track_x = bars_x + 12.0
        bar_w = max(20.0, bars_end - track_x)
        for name, ratio, y in (("L", self._l, cy - 7.0), ("R", self._r, cy + 3.0)):
            p.setFont(self._f_small)
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(QRectF(bars_x, y - 4.0, 10.0, 12.0), _ALIGN_C, name)
            track = QRectF(track_x, y, bar_w, 4.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawRoundedRect(track, 2.0, 2.0)
            v = min(1.0, max(0.0, ratio))
            if v > 0.01:
                grad = QLinearGradient(track.x(), 0.0, track.x() + bar_w, 0.0)
                if v >= 0.68:
                    grad.setColorAt(0.0, QColor(EMERALD).lighter(120))
                    grad.setColorAt(1.0, QColor(EMERALD))
                else:
                    grad.setColorAt(0.0, QColor(CYAN))
                    grad.setColorAt(1.0, QColor(BLUE))
                p.setBrush(QBrush(grad))
                p.drawRoundedRect(QRectF(track.x(), track.y(), bar_w * v, 4.0), 2.0, 2.0)
            tx = track.x() + bar_w * 0.68
            p.setPen(QPen(QColor(255, 255, 255, 235), 1.0))
            p.drawLine(QPointF(tx, y - 1.5), QPointF(tx, y + 5.5))

    def _draw_button(self, p, rect: QRectF, text: str, base: str, hot: bool, flashing: bool,
                     pulse: int = 0, listening: bool = False, small: bool = False) -> None:
        col = QColor(base)
        if listening:
            fill, edge, txt = QColor(255, 69, 58, 100 + 30 * pulse), QColor(col), QColor(255, 255, 255)
            edge.setAlpha(235)
        elif flashing:
            fill, edge, txt = QColor(EMERALD), QColor(EMERALD), QColor(255, 255, 255)
            fill.setAlpha(170)
            edge.setAlpha(255)
        elif hot:
            fill, edge, txt = QColor(255, 255, 255, 78), QColor(255, 255, 255, 190), QColor(255, 255, 255)
        else:
            fill, edge, txt = QColor(255, 255, 255, 20), QColor(255, 255, 255, 45), QColor(235, 238, 245)
        top = QColor(fill)
        top.setAlpha(min(255, fill.alpha() + 18))
        pill = QLinearGradient(0.0, rect.top(), 0.0, rect.bottom())
        pill.setColorAt(0.0, top)
        pill.setColorAt(1.0, fill)
        p.setPen(QPen(edge, 1.0))
        p.setBrush(QBrush(pill))
        p.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)
        p.setFont(self._f_small if small else self._f_btn)
        p.setPen(txt)
        p.drawText(rect, _ALIGN_C, text)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        w, h = float(self.width()), float(self.height())
        cy = h / 2.0
        accent = QColor(ACCENT.get(self._state, ACCENT["TRACKING"]))

        # visionOS liquid glass capsule: dark vertical gradient, top sheen, specular rim with accent glow
        rad = h / 2.0 - 1.0
        body = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        fill = QLinearGradient(0.0, 0.0, 0.0, h)
        fill.setColorAt(0.0, QColor(34, 34, 40, 118))
        fill.setColorAt(1.0, QColor(24, 24, 28, 140))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(body, rad, rad)
        sheen = QLinearGradient(0.0, 0.0, 0.0, h / 2.0)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 38))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(sheen))
        p.drawRoundedRect(QRectF(3.0, 2.0, w - 6.0, h / 2.0 - 1.0), rad - 2.0, rad - 2.0)
        glow = QColor(ROSE) if self._listening else QColor(accent)
        glow.setAlpha(35 if (not self._listening and self._state == "TRACKING") else 190)
        rim = QLinearGradient(0.0, 0.0, 0.0, h)
        rim.setColorAt(0.0, QColor(255, 255, 255, 145))
        rim.setColorAt(1.0, glow)
        p.setPen(QPen(QBrush(rim), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body, rad, rad)

        if w >= self.EXPANDED[0] - 4.0:
            self._draw_meters(p, 124.0, 138.0, 220.0, cy, accent)
            flash = self._flash_name(time.perf_counter())
            self._draw_button(p, self.btn_kb_rect, "\u2328 Keyboard", CYAN,
                              self._hot == "KEYBOARD", flash == "KEYBOARD")
            if self._listening:
                self._draw_button(p, self.btn_stt_rect, "\u25CF Wispr Active", ROSE, True, False,
                                  pulse=self._pulse, listening=True, small=True)
            else:
                self._draw_button(p, self.btn_stt_rect, "\U0001F399 Voice AI", EMERALD,
                                  self._hot == "VOICE", flash == "VOICE")
        else:
            ox = (w - float(self.COMPACT[0])) / 2.0
            self._draw_meters(p, ox + 16.0, ox + 32.0, ox + 122.0, cy, accent)

        # thin charge line along the bottom while a hold is charging
        if progress_visible(self._progress, self._kind, arc=True):
            pen = QPen(QColor(arc_color(self._state, self._kind)), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            span = (w - 28.0) * min(1.0, self._progress)
            p.drawLine(QPointF(14.0, h - 3.5), QPointF(14.0 + span, h - 3.5))
        p.end()


class CursorRingHUD(_OverlayBase):
    """64x64 click-through reticle that follows the cursor."""

    SIZE = 64

    def __init__(self) -> None:
        super().__init__()
        scr = QGuiApplication.primaryScreen()
        self._dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
        self._state = "TRACKING"
        self._progress = 0.0
        self._kind = ""
        self._key = None
        self._last_pos = None
        self.resize(self.SIZE, self.SIZE)

    def set_status(self, state, progress=0.0, kind="") -> None:
        key = (state, round(progress, 2), kind)
        if key == self._key:
            return
        self._key = key
        self._state, self._progress, self._kind = state, progress, kind
        self.update()

    def move_to(self, x: float, y: float) -> None:
        nx = int(x / self._dpr) - self.SIZE // 2
        ny = int(y / self._dpr) - self.SIZE // 2
        if (nx, ny) != self._last_pos:
            self._last_pos = (nx, ny)
            self.move(nx, ny)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPointF(self.SIZE / 2.0, self.SIZE / 2.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._state == "LOCK":
            rose = QColor(ACCENT["LOCK"])
            rose.setAlpha(230)
            p.setPen(QPen(rose, 2.2))
            p.drawEllipse(c, 18.0, 18.0)
            for dx, dy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
                p.drawLine(QPointF(c.x() + dx * 10.0, c.y() + dy * 10.0),
                           QPointF(c.x() + dx * 27.0, c.y() + dy * 27.0))
        else:
            ring = QColor(ACCENT.get(self._state, ACCENT["TRACKING"]))
            ring.setAlpha(210)
            p.setPen(QPen(ring, 2.2))
            p.drawEllipse(c, 18.0, 18.0)
        if self._state == "DRAG":
            fill = QColor(AMBER)
            fill.setAlpha(80)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawEllipse(c, 12.0, 12.0)
            p.setBrush(Qt.BrushStyle.NoBrush)
        if progress_visible(self._progress, self._kind, arc=True):
            pen = QPen(QColor(arc_color(self._state, self._kind)), 3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(c.x() - 22.0, c.y() - 22.0, 44.0, 44.0),
                      90 * 16, -int(min(1.0, self._progress) * 360 * 16))
        p.end()


class GlassOSK(_OverlayBase):
    """Built-in obsidian glass on-screen keyboard (780x250, bottom-center), hidden until toggled.

    100% click-through + non-activating, exactly like the notch. Nothing ever clicks this window: the
    CursorController asks hit_test() which key the cursor is on and swallows the OS click, so the user's
    textbox keeps focus and Windows UIPI (which blocks SendInput clicks on osk.exe) never matters.
    hit_test/contains_point are pure math and safe to call from the vision thread; paint state is main thread.
    """

    WIDTH, HEIGHT = 780, 250
    BLUR = True
    BOTTOM_MARGIN = 310            # window top = screen height - 310
    PAD_X, PAD_Y = 14.0, 17.0
    GAP, KEY_H, ROW_GAP = 6.0, 48.0, 8.0
    FLASH_SECONDS = 0.20

    def __init__(self) -> None:
        super().__init__()
        scr = QGuiApplication.primaryScreen()
        self._geo = scr.geometry() if scr is not None else QRectF(0, 0, 1920, 1080).toRect()
        self._dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
        g = self._geo
        self._ox = g.x() + (g.width() - self.WIDTH) // 2
        self._oy = max(g.y(), g.y() + g.height() - self.BOTTOM_MARGIN)
        self.shift_active = False
        self.sym_active = False
        self.ctrl_active: bool = False
        self.alt_active: bool = False
        self._shown = False
        self._hot = None
        self._flash = ("", 0.0)
        self._key = None
        self._keys = []
        self._f_char = QFont("Segoe UI")
        self._f_char.setPointSizeF(13.0)
        self._f_char.setBold(True)
        self._f_spec = QFont("Segoe UI")
        self._f_spec.setPointSizeF(8.5)
        self._f_spec.setBold(True)
        self._rebuild()
        self.setGeometry(self._ox, self._oy, self.WIDTH, self.HEIGHT)
        _round_mask(self, self.WIDTH, self.HEIGHT, 24.0)

    @property
    def is_active(self) -> bool:
        return self._shown

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._shown = True
        self._key = None
        self.update()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._shown = False
        self._hot = None
        self.reset_sticky_modifiers()            # a hidden keyboard never keeps a latched CTRL / ALT / SHIFT

    def reset_sticky_modifiers(self) -> None:
        """Unlatch SHIFT, CTRL and ALT (one-shot shortcut latches) and redraw."""
        self.shift_active = False
        self.ctrl_active = False
        self.alt_active = False
        self._rebuild()
        self._key = None
        self.update()

    def _row_defs(self):
        shift, sym = self.shift_active, self.sym_active

        def ch(c):
            return (c, c, 1.0)

        conv = str.upper if shift else str.lower
        if sym:
            r2c, r3c, r4c = "!@#$%^&*()", "_+=[]{};:", "'\"<>?~|"
        else:
            r2c, r3c, r4c = conv("QWERTYUIOP"), conv("ASDFGHJKL"), conv("ZXCVBNM")
        return [
            [("ESC", "ESC", 1.3)] + [ch(c) for c in "1234567890-"] + [("BACKSPACE", "\u232B BKSP", 2.0)],
            [ch(c) for c in r2c] + [("CLEAR", "CLEAR", 2.0)],
            [("SHIFT", "\u21E7 SHIFT", 1.5)] + [ch(c) for c in r3c] + [("ENTER", "\u21B5 ENTER", 2.0)],
            [("CTRL", "CTRL", 1.5), ("ALT", "ALT", 1.5), ("SYM", "ABC" if sym else "?123", 1.5)]
            + [ch(c) for c in r4c] + [ch(".")]
            + [("SPACE", "SPACE", 2.5), ("HIDE", "\u2715 HIDE", 1.5)],
        ]

    def _rebuild(self) -> None:
        keys = []
        for ri, row in enumerate(self._row_defs()):
            n = len(row)
            total_w = sum(w for _t, _l, w in row)
            unit = (self.WIDTH - 2.0 * self.PAD_X - self.GAP * (n - 1)) / total_w
            x = self.PAD_X
            y = self.PAD_Y + ri * (self.KEY_H + self.ROW_GAP)
            for token, label, w in row:
                kw = unit * w
                keys.append((token, label, QRectF(x, y, kw, self.KEY_H)))
                x += kw + self.GAP
        self._keys = keys

    def toggle_shift(self) -> None:
        self.shift_active = not self.shift_active
        self._rebuild()
        self.refresh()

    def toggle_sym(self) -> None:
        self.sym_active = not self.sym_active
        self._rebuild()
        self.refresh()

    def _local(self, x_px: float, y_px: float):
        return x_px / self._dpr - self._ox, y_px / self._dpr - self._oy

    def contains_point(self, x_px: float, y_px: float) -> bool:
        lx, ly = self._local(x_px, y_px)
        return 0.0 <= lx <= self.WIDTH and 0.0 <= ly <= self.HEIGHT

    def hit_test(self, x_px: float, y_px: float):
        """Token of the key under the cursor ('q', 'SPACE', 'BACKSPACE', 'HIDE', ...) or None."""
        lx, ly = self._local(x_px, y_px)
        pt = QPointF(lx, ly)
        for token, _label, rect in self._keys:
            if rect.contains(pt):
                return token
        return None

    def key_center(self, token: str):
        """Physical-pixel screen center of a key (None when that token is not in the current layer)."""
        for t, _label, r in self._keys:
            if t == token:
                return (self._ox + r.center().x()) * self._dpr, (self._oy + r.center().y()) * self._dpr
        return None

    def update_hover(self, x_px: float, y_px: float) -> None:
        self._hot = self.hit_test(x_px, y_px)
        self.refresh()

    def flash_key(self, token: str) -> None:
        self._flash = (token, time.perf_counter())
        self.refresh()

    def _flash_name(self, now: float) -> str:
        return self._flash[0] if (now - self._flash[1]) < self.FLASH_SECONDS else ""

    def refresh(self) -> None:
        key = (self._hot, self.shift_active, self.sym_active, self.ctrl_active, self.alt_active,
               self._flash_name(time.perf_counter()), self._shown)
        if key == self._key:
            return
        self._key = key
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        w, h = float(self.width()), float(self.height())
        # visionOS floating glass chassis: frosted gradient, top sheen, diagonal specular rim
        body = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        chassis = QLinearGradient(0.0, 0.0, 0.0, h)
        chassis.setColorAt(0.0, QColor(34, 34, 40, 120))
        chassis.setColorAt(1.0, QColor(24, 24, 28, 140))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(chassis))
        p.drawRoundedRect(body, 24.0, 24.0)
        sheen = QLinearGradient(0.0, 0.0, 0.0, h * 0.35)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 38))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(sheen))
        p.drawRoundedRect(QRectF(3.0, 2.0, w - 6.0, h * 0.35), 22.0, 22.0)
        rim = QLinearGradient(0.0, 0.0, w, h)
        rim.setColorAt(0.0, QColor(255, 255, 255, 145))
        rim.setColorAt(1.0, QColor(255, 255, 255, 35))
        p.setPen(QPen(QBrush(rim), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body, 24.0, 24.0)

        flash = self._flash_name(time.perf_counter())
        for token, label, r in self._keys:
            lit = ((token == "SHIFT" and self.shift_active) or (token == "SYM" and self.sym_active)
                   or (token == "CTRL" and self.ctrl_active) or (token == "ALT" and self.alt_active))
            special = len(token) > 1
            solid = False
            if token == flash:
                fill, edge, txt = QColor(50, 215, 75, 190), QColor(50, 215, 75, 255), QColor(255, 255, 255)
                ew, solid = 1.2, True
            elif lit:
                fill, edge, txt = QColor(255, 255, 255, 185), QColor(255, 255, 255, 230), QColor(28, 28, 30)
                ew, solid = 1.4, True
            elif token == self._hot:
                fill, edge, txt = QColor(255, 255, 255, 78), QColor(255, 255, 255, 190), QColor(255, 255, 255)
                ew = 1.4
            else:
                fill = QColor(255, 255, 255, 28 if special else 20)
                edge = QColor(255, 69, 58, 130) if token == "ESC" else QColor(255, 255, 255, 35)
                txt, ew = QColor(235, 238, 245), 1.0
            bottom = QColor(fill)
            if not solid:
                bottom.setAlpha(max(10, fill.alpha() // 2))
            cap = QLinearGradient(0.0, r.top(), 0.0, r.bottom())
            cap.setColorAt(0.0, fill)
            cap.setColorAt(1.0, bottom)
            p.setPen(QPen(edge, ew))
            p.setBrush(QBrush(cap))
            p.drawRoundedRect(r, 10.0, 10.0)
            p.setFont(self._f_char if len(token) == 1 else self._f_spec)
            p.setPen(txt)
            p.drawText(r, _ALIGN_C, label)
        p.end()
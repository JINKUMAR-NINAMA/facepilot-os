import ctypes
import sys
from ctypes import wintypes
from typing import Optional, Sequence, Tuple

IS_WINDOWS = sys.platform == "win32"
user32 = ctypes.windll.user32 if IS_WINDOWS else None

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
SM_CXSCREEN, SM_CYSCREEN = 0, 1


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if IS_WINDOWS:
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise OSError("win32_input requires Windows")


def screen_size() -> Tuple[int, int]:
    _require_windows()
    return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


def get_cursor_pos() -> Tuple[int, int]:
    _require_windows()
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> bool:
    _require_windows()
    return bool(user32.SetCursorPos(int(x), int(y)))


def _send(inp: INPUT) -> None:
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0) -> None:
    _require_windows()
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
    _send(inp)


def move_abs(x: int, y: int) -> None:
    """SendInput absolute move (normalized 0..65535 across the primary screen)."""
    w, h = screen_size()
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
           int(x * 65535 / max(w - 1, 1)), int(y * 65535 / max(h - 1, 1)))


_left_gate = None


def set_left_gate(fn) -> None:
    """Install fn(kind) with kind 'down' or 'up'; returning True swallows that left-button event."""
    global _left_gate
    _left_gate = fn


def _gated(kind: str) -> bool:
    gate = _left_gate
    if gate is None:
        return False
    try:
        return bool(gate(kind))
    except Exception:
        return False


def left_down() -> None:
    if _gated("down"):
        return
    _mouse(MOUSEEVENTF_LEFTDOWN)


def left_up() -> None:
    if _gated("up"):
        return
    _mouse(MOUSEEVENTF_LEFTUP)


def right_down() -> None: _mouse(MOUSEEVENTF_RIGHTDOWN)
def right_up() -> None: _mouse(MOUSEEVENTF_RIGHTUP)


def left_click() -> None:
    left_down()
    left_up()


def right_click() -> None:
    right_down()
    right_up()


def key_event(vk: int, down: bool = True) -> None:
    _require_windows()
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, 0)
    _send(inp)


def key_tap(vk: int) -> None:
    key_event(vk, True)
    key_event(vk, False)


MOUSEEVENTF_WHEEL = 0x0800


def scroll(dy: int) -> None:
    """Vertical wheel. Positive = up, negative = down. 120 units = one notch."""
    _require_windows()
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, int(dy) & 0xFFFFFFFF, MOUSEEVENTF_WHEEL, 0, 0)
    _send(inp)


# ---- Phase 4.1: unicode typing, On-Screen Keyboard, Windows voice typing ----
KEYEVENTF_UNICODE = 0x0004
VK_LWIN, VK_CONTROL, VK_O, VK_H = 0x5B, 0x11, 0x4F, 0x48


def _unicode_input(unit: int, up: bool) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return inp


def build_unicode_inputs(text: str) -> list:
    """One down + one up INPUT per UTF-16 code unit (surrogate pairs become two units)."""
    data = text.replace("\r", " ").replace("\n", " ").encode("utf-16-le")
    out = []
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        out.append(_unicode_input(unit, False))
        out.append(_unicode_input(unit, True))
    return out


def _send_many(inputs) -> int:
    _require_windows()
    sent = 0
    for i in range(0, len(inputs), 128):
        chunk = inputs[i:i + 128]
        arr = (INPUT * len(chunk))(*chunk)
        sent += int(user32.SendInput(len(chunk), arr, ctypes.sizeof(INPUT)))
    return sent


def type_unicode_text(text: str) -> int:
    """Type text into whatever window has keyboard focus (KEYEVENTF_UNICODE). Returns events sent."""
    return _send_many(build_unicode_inputs(text))


def _chord(*vks: int) -> int:
    inputs = []
    for vk in vks:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(vk, 0, 0, 0, 0)
        inputs.append(inp)
    for vk in reversed(vks):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
        inputs.append(inp)
    return _send_many(inputs)


def send_hotkey_win_ctrl_o() -> bool:
    """Win+Ctrl+O toggles the Windows accessibility On-Screen Keyboard without taking focus."""
    return _chord(VK_LWIN, VK_CONTROL, VK_O) == 6


def toggle_osk() -> bool:
    """Win+Ctrl+O; falls back to launching osk.exe. Returns True when the hotkey path was used."""
    try:
        if send_hotkey_win_ctrl_o():
            return True
    except Exception:
        pass
    import subprocess
    subprocess.Popen(["osk.exe"], shell=True)
    return False


def trigger_win_voice_typing() -> bool:
    """Win+H opens Windows voice typing for the focused textbox."""
    return _chord(VK_LWIN, VK_H) == 4


# ---- Phase 4.2: single virtual keys + chords (built-in GlassOSK, Ctrl+A clear, Win+H fallback) ----
VK_BACK, VK_TAB, VK_RETURN, VK_SPACE = 0x08, 0x09, 0x0D, 0x20
VK_LEFT, VK_RIGHT = 0x25, 0x27


def send_vk(vk: int) -> None:
    """Press and release one virtual key (down + up in a single SendInput call)."""
    _chord(int(vk))


def send_hotkey_chord(vks: Sequence[int]) -> None:
    """Press all vks in order, then release them in reverse order (KEYEVENTF_KEYUP)."""
    _chord(*[int(v) for v in vks])



# ---- Phase 4.3: Voice AI hotkey bridge (Wispr Flow / Win+H / custom chord) ----
def trigger_voice_ai(mode: str = "wispr_flow", custom_vks: Sequence[int] = (0x11, 0x5B, 0x20)) -> None:
    """Fire the configured dictation hotkey. wispr_flow = Ctrl+Win+Space, win_h = Win+H, else custom chord."""
    if mode == "wispr_flow":
        send_hotkey_chord([0x11, 0x5B, 0x20])
    elif mode == "win_h":
        send_hotkey_chord([0x5B, 0x48])
    else:
        send_hotkey_chord(list(custom_vks))



# ---- Phase 5: token -> virtual-key mapping for sticky CTRL / ALT shortcuts ----
_PUNCT_VK = {"-": 0xBD, ".": 0xBE, "/": 0xBF, ",": 0xBC}
_TOKEN_VK = {"SPACE": 0x20, "ENTER": 0x0D, "BACKSPACE": 0x08, "TAB": 0x09, "ESC": 0x1B}


def char_to_vk(key_token: str) -> Optional[int]:
    """Map a single ASCII letter/digit/punctuation char or a special token to a Windows virtual-key code."""
    if not key_token:
        return None
    if key_token in _TOKEN_VK:
        return _TOKEN_VK[key_token]
    if len(key_token) != 1:
        return None
    ch = key_token
    if "a" <= ch <= "z" or "A" <= ch <= "Z":
        return ord(ch.upper())
    if "0" <= ch <= "9":
        return ord(ch)
    return _PUNCT_VK.get(ch)
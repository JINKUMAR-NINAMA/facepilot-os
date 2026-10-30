import math
from dataclasses import dataclass
from typing import Optional, Sequence

from config import FacePilotConfig

NOSE, FOREHEAD, CHIN, L_CHEEK, R_CHEEK = 1, 10, 152, 234, 454


@dataclass
class PoseResult:
    norm_x: float                  # 0..1 after deadzone + soft clamp
    norm_y: float
    screen_x: float                # unfiltered px
    screen_y: float
    yaw: float
    pitch: float
    pitch_angle: float             # radians relative to neutral, + = looking down
    pitch_compensation_factor: float
    in_deadzone: bool
    bar: float                     # brow raise relative to neutral (1.0 = neutral)
    left_ear: float                # pitch-compensated, subject's left eye
    right_ear: float               # pitch-compensated, subject's right eye


def compute_ear(lm, idx: Sequence[int], w: int, h: int) -> float:
    p = [(lm[i].x * w, lm[i].y * h) for i in idx]
    d = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])
    horiz = d(p[0], p[3])
    return (d(p[1], p[5]) + d(p[2], p[4])) / (2.0 * horiz) if horiz > 1e-6 else 0.0


# Iris centers (refine_landmarks model), upper-brow points, eye corners
R_IRIS, L_IRIS, R_BROW, L_BROW = 468, 473, 105, 334
R_EYE_CORNERS, L_EYE_CORNERS = (33, 133), (362, 263)


def compute_bar(lm, w: int, h: int) -> float:
    """Raw Brow Aspect Ratio: eye-center -> upper-brow distance / eye width, both eyes averaged."""
    def d(a, b):
        return math.hypot((lm[a].x - lm[b].x) * w, (lm[a].y - lm[b].y) * h)

    total, n = 0.0, 0
    for iris, brow, (c0, c1) in ((R_IRIS, R_BROW, R_EYE_CORNERS),
                                 (L_IRIS, L_BROW, L_EYE_CORNERS)):
        width = d(c0, c1)
        if width > 1e-6:
            total += d(iris, brow) / width
            n += 1
    return total / n if n else 0.0


def soft_clamp(v: float, knee: float) -> float:
    """Linear up to `knee`, then asymptotically approaches +/-1."""
    a = abs(v)
    if a <= knee:
        return v
    out = knee + (1.0 - knee) * math.tanh((a - knee) / (1.0 - knee))
    return math.copysign(out, v)


class PoseMapper:
    def __init__(self, cfg: FacePilotConfig, screen_w: int, screen_h: int) -> None:
        self.cfg = cfg
        self.sw, self.sh = screen_w, screen_h
        self._aspect = cfg.frame_height / cfg.frame_width
        self._neutral: Optional[tuple] = None
        self._bar_neutral: Optional[float] = None

    def _measure(self, lm):
        n, f, c, l, r = lm[NOSE], lm[FOREHEAD], lm[CHIN], lm[L_CHEEK], lm[R_CHEEK]
        face_w = max(abs(r.x - l.x), 1e-6)
        face_h = max(abs(c.y - f.y), 1e-6)
        yaw = (n.x - (l.x + r.x) / 2.0) / face_w
        pitch = (n.y - (f.y + c.y) / 2.0) / face_h
        # 3D chin->forehead vector; forehead nearer camera (z smaller) = looking down
        dy_w = (c.y - f.y) * self._aspect
        angle = math.atan2(-(f.z - c.z), max(dy_w, 1e-6))
        return yaw, pitch, angle

    def recenter(self, lm) -> None:
        self._neutral = self._measure(lm)
        self._bar_neutral = compute_bar(lm, self.cfg.frame_width, self.cfg.frame_height) or None

    def map(self, lm) -> PoseResult:
        cfg = self.cfg
        yaw, pitch, angle = self._measure(lm)
        if self._neutral is None:
            self._neutral = (yaw, pitch, angle)
        y0, p0, a0 = self._neutral

        dx = (yaw - y0) * cfg.sensitivity_x * (-1.0 if cfg.invert_x else 1.0)
        dy = (pitch - p0) * cfg.sensitivity_y

        # radial deadzone (continuous, direction-preserving)
        r = math.hypot(dx, dy)
        in_dz = r <= cfg.radial_deadzone
        if in_dz:
            dx = dy = 0.0
        else:
            s = (r - cfg.radial_deadzone) / r
            dx, dy = dx * s, dy * s
            # non-linear response: r -> r ** exponent (direction preserved)
            r2 = math.hypot(dx, dy)
            if r2 > 1e-9:
                k = r2 ** (cfg.response_exponent - 1.0)
                dx, dy = dx * k, dy * k

        # isotropic projection: BOTH axes use the same pixel factor (half the screen WIDTH) around
        # the screen center, so equal head offsets give equal pixel displacement on X and Y.
        half_w = self.sw / 2.0
        ey = self.sh / self.sw                         # vertical half-extent in width units (0.5625 on 16:9)
        dx = soft_clamp(dx, cfg.edge_knee)
        dy = soft_clamp(dy / ey, cfg.edge_knee) * ey   # 1:1 linear up to the knee, soft at the edge
        px = min(max(self.sw / 2.0 + dx * half_w, 0.0), self.sw - 1.0)
        py = min(max(self.sh / 2.0 + dy * half_w, 0.0), self.sh - 1.0)
        nx, ny = px / (self.sw - 1.0), py / (self.sh - 1.0)

        rel = angle - a0
        factor = min(cfg.ear_pitch_max_factor, max(1.0, 1.0 + cfg.ear_pitch_gain * rel))

        w, h = cfg.frame_width, cfg.frame_height
        l_ear = compute_ear(lm, cfg.left_eye, w, h) * factor
        r_ear = compute_ear(lm, cfg.right_eye, w, h) * factor
        bar_raw = compute_bar(lm, w, h)
        if self._bar_neutral is None and bar_raw > 1e-6:
            self._bar_neutral = bar_raw
        bar = bar_raw / self._bar_neutral if self._bar_neutral else 1.0

        return PoseResult(nx, ny, nx * (self.sw - 1), ny * (self.sh - 1),
                          yaw, pitch, rel, factor, in_dz, bar, l_ear, r_ear)
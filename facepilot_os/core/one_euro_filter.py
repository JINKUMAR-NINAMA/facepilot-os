import math
from collections import deque
from typing import Tuple


def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class _LowPass:
    def __init__(self) -> None:
        self.y = None

    def __call__(self, x: float, alpha: float) -> float:
        self.y = x if self.y is None else alpha * x + (1.0 - alpha) * self.y
        return self.y


class OneEuroFilter:
    """1D 1-Euro Filter (Casiez et al., 2012)."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self) -> None:
        self._t = None
        self._x = None
        self._x_lp = _LowPass()
        self._dx_lp = _LowPass()

    def __call__(self, x: float, t: float) -> float:
        if self._t is None:
            self._t, self._x = t, x
            self._x_lp(x, 1.0)
            self._dx_lp(0.0, 1.0)
            return x
        dt = max(t - self._t, 1e-6)
        dx = (x - self._x) / dt
        edx = self._dx_lp(dx, _alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        out = self._x_lp(x, _alpha(cutoff, dt))
        self._t, self._x = t, x
        return out


class OneEuroFilter2D:
    """
    Filters normalized (x, y) and returns (screen_x, screen_y, velocity_px_s).
    Velocity is the path length over a rolling window of filtered screen points.
    """

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0,
                 scale_x: float = 1.0, scale_y: float = 1.0, window: int = 8) -> None:
        self._fx = OneEuroFilter(min_cutoff, beta, d_cutoff)
        self._fy = OneEuroFilter(min_cutoff, beta, d_cutoff)
        self.scale_x = scale_x
        self.scale_y = scale_y
        self._hist = deque(maxlen=max(2, window))

    def reset(self) -> None:
        self._fx.reset()
        self._fy.reset()
        self._hist.clear()

    def __call__(self, nx: float, ny: float, t: float) -> Tuple[float, float, float]:
        sx = self._fx(nx, t) * self.scale_x
        sy = self._fy(ny, t) * self.scale_y
        self._hist.append((t, sx, sy))
        return sx, sy, self._velocity()

    def _velocity(self) -> float:
        if len(self._hist) < 2:
            return 0.0
        path = 0.0
        pts = list(self._hist)
        for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:]):
            path += math.hypot(x1 - x0, y1 - y0)
        elapsed = pts[-1][0] - pts[0][0]
        return path / elapsed if elapsed > 1e-6 else 0.0
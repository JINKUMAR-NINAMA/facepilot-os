from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class FacePilotConfig:
    # Camera (cv2.CAP_DSHOW, 640x480 @ 60fps, 1-frame buffer)
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    fps: int = 60
    buffer_size: int = 1

    # Head -> cursor mapping
    micro_aim_deadzone: float = 1.5   # px; cursor holds still inside this radius
    magnetic_snap_enabled: bool = True
    response_exponent: float = 1.15   # >1: compress small head moves (precision), accelerate large ones
    sensitivity_x: float = 4.5          # X == Y: isotropic, same px per head-offset unit on both axes
    sensitivity_y: float = 4.5
    swap_eyes: bool = False          # True if [LEFT WINK]/[RIGHT WINK] log shows the wrong eye
    invert_x: bool = True            # un-mirrored webcam: turning right must move cursor right
    radial_deadzone: float = 0.03    # in normalized [-1, 1] units
    edge_knee: float = 0.80          # soft-clamp starts compressing beyond this

    # 1-Euro Filter
    min_cutoff: float = 0.85
    beta: float = 3.5
    d_cutoff: float = 1.0
    velocity_window: int = 15        # samples in rolling velocity window

    # Pitch compensation for EAR
    ear_pitch_gain: float = 0.6      # EAR boost per radian of downward pitch
    ear_pitch_max_factor: float = 1.6

    # Landmark indices
    left_eye: List[int] = field(default_factory=lambda: [362, 385, 387, 263, 373, 380])
    right_eye: List[int] = field(default_factory=lambda: [33, 160, 158, 133, 153, 144])

    # Hotkeys
    kill_switch: str = "esc"
    recenter_key: str = "f8"
    toggle_key: str = "f10"

    tracking_enabled: bool = True

    # Voice AI hotkey bridge (no local model, zero extra dependencies)
    voice_mode: str = "wispr_flow"   # "wispr_flow" (Ctrl+Win+Space hands-free toggle), "win_h" (Win+H), or "custom_chord"
    voice_custom_vks: Tuple[int, ...] = (0x11, 0x5B, 0x20)   # VK_CONTROL, VK_LWIN, VK_SPACE
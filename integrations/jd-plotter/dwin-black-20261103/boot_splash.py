"""Native, PC-independent DWIN T5L DGUS II boot splash control.

Source: DWIN T5L_DGUSII Application Development Guide V2.8, section 7.6,
printed pages 135-136 (references/DGUSII-guide.pdf). A current official
mirror describes the same native 0x5A04 Image Animation control:
https://forums.dwin-global.com/wp-content/uploads/2021/12/T5L_DGUSII-Application-Development-Guide-V2.7.pdf

The screen boots into picture/page 0 by default. A native Image Animation
on page 0 plays ascending full-screen background pictures and stops on its
last page, provided that last page has no Image Animation control. Thus a
two-picture sequence, splash=0 and home=1, uses one 250 * 8 ms interval.
This revision selects ICL 32 explicitly rather than the implicit background
alias 0. Pic_End_Exp points to Home for the expanded-library path as well.
No host, user VP initialization, hardware CFG, kernel, or OS binary is added.

This is NOT the Animation Icon (0x5A01) control, which only animates icons.
Pic_End_Exp applies to expanded-library playback; it is not a general
arbitrary return-page feature. A descending 8 -> 0 sequence is invalid.

The precise visible power-up interval still requires a physical display
test: JPEG decoding, display refresh, and existing external firmware can
affect timing. A host must not force a different page during the splash.
"""
from dataclasses import dataclass
import struct


BOOT_PAGE = 0
HOME_PAGE = 1
BOOT_DURATION_MS = 2000


@dataclass(frozen=True)
class ImageAnimation:
    """Native 32-byte 5A04 instruction; compatible with build_display_file.

    For our two-second boot use ``ImageAnimation()`` and put only the logo
    on page 0; put normal Home controls on page 1. The containing 14.BIN
    compiler/parser must admit type 5A04 with five description words and a
    fixed zero word at offset 0x06 instead of an application VP.
    """

    page: int = BOOT_PAGE
    first_page: int = BOOT_PAGE
    last_page: int = HOME_PAGE
    frame_ticks: int = BOOT_DURATION_MS // 8
    library_id: int = 32

    @property
    def nominal_transition_ms(self) -> int:
        return (self.last_page - self.first_page) * self.frame_ticks * 8

    def encode(self, width: int = 800, height: int = 480) -> bytes:
        # Deliberately support only our documented background-library path;
        # do not accidentally enable unknown expanded NAND hardware modes.
        for name, value in (("page", self.page), ("first_page", self.first_page), ("last_page", self.last_page)):
            if type(value) is not int or not 0 <= value < 128:
                raise ValueError(f"{name} must be an integer in 0..127")
        if self.first_page >= self.last_page:
            raise ValueError("Image Animation requires first_page < last_page")
        if self.page != self.first_page:
            raise ValueError("Place this animation control on its first page")
        if type(self.frame_ticks) is not int or not 1 <= self.frame_ticks <= 255:
            raise ValueError("frame_ticks must be an integer in 1..255 (8 ms units)")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError("Invalid LCD dimensions")
        record = bytearray(32)
        struct.pack_into(">6H", record, 0, 0x5A04, 0xFFFF, 0x0005, 0x0000,
                         self.first_page, self.last_page)
        record[0x0C] = self.frame_ticks
        if self.library_id != 32:
            raise ValueError("Boot animation must use our deployed library 32 explicitly")
        record[0x0D] = self.library_id
        # Explicit source avoids the old implicit CFG-selected background alias.
        # End_Exp is relevant to expanded-library playback; point it to Home
        # as well, so this path cannot return to the splash.
        struct.pack_into(">H", record, 0x0E, self.last_page)
        return bytes(record)


def audit_boot_pages(displays, touches, *, page_count: int) -> dict:
    """Reject common integration errors before deployment; no file writes.

    This audit is intentionally specific to the chosen two-page boot flow.
    Navigation must never lead back to splash. One explicit local tap to skip
    is permitted, but no machine-action hotspots from the previous Home.
    """
    displays, touches = tuple(displays), tuple(touches)
    animations = [control for control in displays if isinstance(control, ImageAnimation)]
    if len(animations) != 1:
        raise ValueError("Exactly one native boot animation is required")
    animation = animations[0]
    animation.encode()
    if (animation.page, animation.first_page, animation.last_page, animation.frame_ticks) != (0, 0, 1, 250):
        raise ValueError("Boot flow must be page 0 -> 1 at 250 * 8 ms")
    if page_count < 2:
        raise ValueError("Boot splash needs separate splash and home pages")
    if any(control is not animation and control.page == BOOT_PAGE for control in displays):
        raise ValueError("Splash page must not have dynamic status overlays")
    splash_touches = [control for control in touches if control.page == BOOT_PAGE]
    if len(splash_touches) != 1 or any(
            getattr(control, "target_page", None) != HOME_PAGE
            or hasattr(control, "key")
            or tuple(control.rect) != (0, 0, 799, 479)
            for control in splash_touches):
        raise ValueError("Splash permits exactly one local skip-to-Home, no machine actions")
    if any(getattr(control, "target_page", None) == BOOT_PAGE for control in touches):
        raise ValueError("Navigation must lead to Home 1, not splash 0")
    if any(not 0 <= control.page < page_count for control in displays + touches):
        raise ValueError("A control has no background page")
    return {
        "native_type": "0x5A04 Image Animation",
        "splash_page": BOOT_PAGE,
        "home_page": HOME_PAGE,
        "frame_time_ticks": 250,
        "tick_ms": 8,
        "nominal_splash_ms": BOOT_DURATION_MS,
        "explicit_library_id": 32,
        "expanded_return_page": HOME_PAGE,
        "local_tap_skip": True,
        "host_required": False,
        "cfg_changed": False,
        "physical_timing_verified": False,
        "source": "DWIN T5L DGUS II Guide V2.8, 7.6, printed pages 135-136",
    }

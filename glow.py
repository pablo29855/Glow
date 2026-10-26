"""Siri-style glowing screen-edge overlay for Windows.

Four thin layered windows hug the screen edges, each drawn with real
per-pixel alpha (UpdateLayeredWindow), so the glow fades into the desktop
with no black band. Only the strips are composited by the DWM, never the
whole screen, which is what keeps the desktop responsive.

Two things make the corners seamless:

* alpha comes from the distance to the *nearest* screen edge, so inside a
  corner square the vertical and horizontal falloffs meet at equal values;
* hue comes from a position that runs once around the perimeter and folds
  back on itself, so the colour at the end of the loop equals the colour at
  the start instead of jumping.

Usage:
    pythonw glow.py          run the overlay daemon
    python  glow.py on       turn the glow on (starts the daemon if needed)
    python  glow.py off      turn the glow off
    python  glow.py quit     stop the daemon
    python  glow.py check    run the self-check
    python  glow.py icon     redraw glow.ico
"""

import collections
import ctypes
import ctypes.wintypes as w
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

try:
    import soundcard
except Exception:  # no loopback support available; the glow still works
    soundcard = None

# Everything below marked in LIMITS can be changed from the settings window
# (glow_ui.py); it is stored in glow.json and picked up live by the daemon.

# ------------------------------------------------------------------- triggers
ENABLED = True      # master switch
AGENT = True        # glow while the agent is working (hooks run `glow.py on`)
MUSIC = True        # glow with whatever the PC is playing

# ---------------------------------------------------------------- look & feel
SPEED = 1.0         # multiplier on every animation's own speed
COLOUR_MODE = "spectrum"  # "spectrum" (each animation's own hues), "single", "palette", "screen"
PALETTE = ["#00e5ff", "#7c4dff", "#ff4081"]  # "single" uses only the first
THICKNESS = 90     # glow depth in pixels while the agent works
SURGE_THICKNESS = 150  # how deep a bass hit pushes the glow into the screen
FALLOFF = 2.4       # higher = tighter glow hugging the edge
PEAK_ALPHA = 0.85   # opacity at the very edge
SATURATION = 0.80
FPS = 60
FADE_SECONDS = 0.35
RING_STEPS = 512    # resolution of the precomputed pattern table
MONITORS = "all"    # "all" frames every display, "primary" only the main one

# ------------------------------------------------------------------- music
# Only the PC's own output is analysed (WASAPI loopback of the default
# speaker). A real microphone is never opened.
MUSIC_RMS_ON = 0.008     # loopback level that counts as "something is playing"
MUSIC_HOLD = 3.0         # seconds of silence before the glow lets go
BANDS = ((30, 150), (150, 2000), (4000, 14000))  # bass, mid, treble in Hz
AGC_SECONDS = 4.0        # auto-gain memory: quiet and loud songs both fill the range
AGC_FLOOR = 2e-3         # below this a band is treated as silence, not boosted
ATTACK = 0.012           # seconds for the glow to jump up to a hit
RELEASE = 0.16           # seconds for it to fall back
BEAT_SENSITIVITY = 1.8   # bass flux must beat this multiple of the running mean
BEAT_MIN_LEVEL = 0.45    # and the bass must be this close to its recent peak
BEAT_GAP = 0.22          # refractory: no second beat sooner than this (~270 BPM)
BEAT_DECAY = 5.0         # how fast a beat pulse falls back to zero
BEAT_FLOOR = 0.30        # dimmest the glow gets between beats
BEAT_SPEED = 2.5         # extra pattern speed at the peak of a beat
TREBLE_SPEED = 1.5       # hi-hats and cymbals make the pattern shimmer faster
KICK_JUMP = 0.035        # the pattern whips this far around the loop on each beat
WHITE_HOT = 0.55         # how white the rim burns at full bass
SURGE_LEVELS = 10        # precomputed glow depths between quiet and full bass
STEREO = 0.35            # left monitors follow the left channel, right the right

# ------------------------------------------------------------------- screen
# COLOUR_MODE "screen" takes its colours from what you are watching: a small
# snapshot of one monitor or of the window in front, ten times a second.
SCREEN_SOURCE = 0        # 0 = the window you are watching, N = monitor N (left to right)
SCREEN_FLASH = 0.8       # how hard a strobe or pyro flash in the picture lights the edge
SCREEN_FPS = 10          # snapshots per second; each costs ~8 ms of CPU
SCREEN_SIZE = (128, 72)  # snapshot resolution: plenty for colour statistics
SCREEN_SLOTS = 4         # colours taken from the picture, shared out by how much each shows
SCREEN_BLEND = 0.3       # per snapshot: how far the colours move towards the new picture
FLASH_RISE = 0.12        # jump in the share of near-white pixels that counts as a flash
FLASH_GAP = 0.2          # seconds before another flash can fire
SCREEN_COLOURS = None    # live (SCREEN_SLOTS, 3) BGR from the snapshots; None = none yet

VERSION = "1.0.0"   # single source: installer/build.py and the release tag read it

SENTINEL = os.path.join(tempfile.gettempdir(), "openclaw-glow.on")
# while the settings window previews, this file exists; its text pins an animation
PREVIEW = os.path.join(tempfile.gettempdir(), "openclaw-glow.preview")
FROZEN = getattr(sys, "frozen", False)  # built into GLOW.exe by PyInstaller
# user data lives next to the exe (portable) or next to this script
DATA_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG = os.path.join(DATA_DIR, "glow.json")
# bundled into the exe; drawn next to the script on first use otherwise
ICON = os.path.join(getattr(sys, "_MEIPASS", DATA_DIR), "glow.ico")
LOCK_PORT = 50573   # single-instance guard: only one process can bind this
CLASS_NAME = "OpenClawGlowOverlay"

# ---------------------------------------------------------------------- win32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    user32.SetProcessDPIAware()

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
PM_REMOVE = 0x0001
WM_DESTROY = 0x0002
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = 0x8001  # WM_APP + 1: what the notification icon sends us
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
TPM_RETURNCMD, TPM_NONOTIFY = 0x0100, 0x0080
MENU_OPEN, MENU_QUIT = 1, 2
shell32 = ctypes.windll.shell32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
        ("uFlags", w.UINT), ("uCallbackMessage", w.UINT), ("hIcon", w.HICON),
        ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

for _fn, _restype in (
    (user32.CreateWindowExW, w.HWND), (user32.DefWindowProcW, ctypes.c_ssize_t),
    (user32.GetDC, w.HDC), (gdi32.CreateCompatibleDC, w.HDC),
    (gdi32.CreateDIBSection, w.HANDLE), (gdi32.SelectObject, w.HANDLE),
    (user32.CreatePopupMenu, w.HMENU), (user32.LoadIconW, w.HICON),
    (shell32.ExtractIconW, w.HICON), (user32.LoadImageW, w.HANDLE),
):
    _fn.restype = _restype

# 64-bit handles overflow ctypes' default int argument, so spell the types out
user32.GetDC.argtypes = [w.HWND]
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [
    w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT,
]
user32.UpdateLayeredWindow.argtypes = [
    w.HWND, w.HDC, ctypes.c_void_p, ctypes.c_void_p, w.HDC, ctypes.c_void_p,
    w.DWORD, ctypes.c_void_p, w.DWORD,
]
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [w.HMENU, w.UINT, w.UINT]
user32.TrackPopupMenu.argtypes = [
    w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p,
]
user32.DestroyMenu.argtypes = [w.HMENU]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
shell32.ExtractIconW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT]
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
gdi32.CreateCompatibleBitmap.restype = w.HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = [w.HDC] + [ctypes.c_int] * 4 + [w.HDC] + [ctypes.c_int] * 4 + [w.DWORD]
gdi32.GetDIBits.argtypes = [w.HDC, w.HANDLE, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
gdi32.DeleteObject.argtypes = [w.HANDLE]
gdi32.DeleteDC.argtypes = [w.HDC]
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindow.restype = w.HWND
user32.GetWindow.argtypes = [w.HWND, w.UINT]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsIconic.argtypes = [w.HWND]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.DestroyWindow.argtypes = [w.HWND]
user32.DrawTextW.argtypes = [w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.RECT), w.UINT]
gdi32.CreateFontW.restype = w.HANDLE
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR]
gdi32.SetBkMode.argtypes = [w.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [w.HDC, w.DWORD]
shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.c_void_p]
gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.SelectObject.argtypes = [w.HDC, w.HANDLE]
gdi32.CreateDIBSection.argtypes = [
    w.HDC, ctypes.c_void_p, w.UINT, ctypes.POINTER(ctypes.c_void_p),
    w.HANDLE, w.DWORD,
]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM
)


def monitors():
    """The displays that get a frame, as (x, y, width, height).

    Each screen gets its own frame. Using the virtual-desktop bounding box
    instead would draw the glow around the outside of a multi-monitor setup,
    leaving the inner screens with no visible edges at all.
    """
    found = displays()
    if MONITORS == "primary":
        found = [r for r in found if r[0] == 0 and r[1] == 0] or found[:1]
    return found


def displays():
    """Every display as (x, y, width, height), left to right."""
    found = []

    def collect(hmon, hdc, rect, param):
        r = rect.contents
        found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0)
    if not found:  # no enumeration (rare); fall back to the primary display
        found.append((0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))
    return sorted(found, key=lambda r: (r[0], r[1]))


def hsv_to_bgr(hue, sat, val):
    """Vectorised HSV -> (B, G, R) float arrays in 0..1."""
    i = np.floor(hue * 6.0)
    f = hue * 6.0 - i
    p = np.full_like(f, val * (1.0 - sat))
    q = val * (1.0 - f * sat)
    t = val * (1.0 - (1.0 - f) * sat)
    v = np.full_like(f, float(val))
    conds = [i.astype(np.int32) % 6 == k for k in range(5)]
    r = np.select(conds, [v, q, p, p, t], v)
    g = np.select(conds, [t, v, v, q, p], p)
    b = np.select(conds, [p, p, t, v, v], q)
    return b, g, r


# ------------------------------------------------------------------ patterns
# Every animation is a function of one cyclic parameter u in [0, 1) that runs
# once around the perimeter. Because u is cyclic, the pattern meets itself
# where the loop closes, and animating is just sliding u, which keeps the
# whole thing a single table lookup per frame.

def ring_distance(u, centre=0.0):
    """Shortest distance from u to centre going either way around the loop."""
    d = np.abs((u - centre) % 1.0)
    return np.minimum(d, 1.0 - d)


# A pattern returns (tone, intensity): tone in 0..1 says where along the
# colour source a point sits, intensity how bright it is. Colour is applied
# afterwards, so every animation works with the spectrum, one colour or a palette.

def pat_rainbow(u):
    """Full spectrum laid once around the perimeter, travelling."""
    return u, np.ones_like(u)


def pat_comet(u):
    """One bright head chasing its tail around the loop."""
    tail = (-u) % 1.0                       # 0 at the head, grows behind it
    glow = np.exp(-(tail / 0.55) ** 1.4) * 0.9 + 0.10
    glow = np.maximum(glow, np.exp(-(ring_distance(u) / 0.035) ** 2))
    return tail, glow


def pat_twin(u):
    """Two heads opposite each other, sweeping in step."""
    d = np.minimum(ring_distance(u, 0.0), ring_distance(u, 0.5))
    glow = np.exp(-(d / 0.11) ** 2) * 0.88 + 0.12
    return d * 4.0, glow                    # d tops out at 0.25, halfway between heads


def pat_breathe(u):
    """Three slow swells drifting around, no hard head."""
    swell = 0.5 + 0.5 * np.cos(2.0 * np.pi * 3.0 * u)
    return swell, 0.30 + 0.70 * swell


DROP_BARS = 12  # even, so the alternating colours still alternate across the seam
DROP_LEVELS = np.array([1.0, 0.55, 0.9, 0.4, 1.0, 0.7, 0.5, 0.95, 0.6, 1.0, 0.45, 0.8])


def pat_drop(u):
    """Festival lasers: sharp beams of uneven height, every third one stutters in two."""
    pos = u * DROP_BARS
    bar = np.minimum(pos.astype(np.intp), DROP_BARS - 1)
    f = pos - bar                             # 0..1 inside the bar
    stutter = bar % 3 == 2
    beam = np.where(stutter,
                    np.maximum(np.exp(-((f - 0.3) / 0.05) ** 2),
                               np.exp(-((f - 0.7) / 0.05) ** 2)),
                    np.exp(-((f - 0.5) / 0.07) ** 2))
    glow = 0.08 + 0.92 * beam * DROP_LEVELS[bar]
    tone = np.where(stutter, 0.5, (bar % 2).astype(np.float32))
    return tone, glow


def pat_stage(u):
    """Still frame of the live "stage" mode (see Stage), for previews and tables."""
    d = ring_distance(u, 0.5)
    return d * 2.0, 0.2 + 0.8 * np.exp(-(d / 0.1) ** 2)


def pat_arena(u):
    """Still frame of the live "arena" mode (see Arena): three speakers, three colours."""
    return np.floor(u * 3.0) / 2.0, 0.05 + 0.95 * np.sin(np.pi * (u * 3.0 % 1.0)) ** 2


# Still frames of the other live modes, for previews and tables.

def pat_scanner(u):
    """One head with a short trail (see Scanner)."""
    d = ring_distance(u, 0.5)
    return np.clip(d * 4.0, 0.0, 1.0), 0.05 + 0.95 * np.exp(-(d / 0.06) ** 2)


def pat_eq(u):
    """Bars tallest in the middle, tone = height (see Equalizer)."""
    lit = np.abs(u * 24.0 % 1.0 - 0.5) < 0.38
    edge = np.abs(u - 0.5) * 2.0
    return edge, 0.05 + 0.95 * lit * (1.0 - 0.8 * edge)


def pat_rain(u):
    """Evenly spaced drops in two alternating colours (see Rain)."""
    return np.floor(u * 16.0) % 2.0, 0.1 + 0.9 * np.exp(-((u * 16.0 % 1.0 - 0.5) / 0.08) ** 2)


def pat_fire(u):
    """Uneven flames (see Fire)."""
    f = 0.5 + 0.5 * np.cos(2.0 * np.pi * 5.0 * u) * np.cos(2.0 * np.pi * 3.0 * u)
    return f, 0.3 + 0.7 * f


def pat_storm(u):
    """A dark sky with one bolt (see Storm)."""
    bolt = np.exp(-(ring_distance(u, 0.5) / 0.02) ** 2)
    return 0.3 + 0.7 * bolt, 0.08 + 0.92 * bolt


def pat_siren(u):
    """Four lamps, two colours (see Siren)."""
    return np.floor(u * 4.0 + 0.5) % 2.0, 0.15 + 0.85 * np.sin(np.pi * ((u * 4.0 + 0.5) % 1.0)) ** 2


def pat_aurora(u):
    """Soft curtains, colour drifting along them (see Aurora)."""
    c = 0.5 + 0.5 * np.sin(2.0 * np.pi * 2.0 * u) * np.sin(2.0 * np.pi * 5.0 * u + 1.0)
    return 0.5 + 0.5 * np.sin(2.0 * np.pi * u), 0.2 + 0.8 * c * c


# hue/span/sat: the animation's own colours in "spectrum" mode.
# cyclic: tone wraps around the loop, so a palette must close on itself too.
Mode = collections.namedtuple("Mode", "name pattern speed hue span sat cyclic")
MODES = [
    Mode("rainbow", pat_rainbow, 0.07, 0.00, 1.000, 1.000, True),
    Mode("comet", pat_comet, 0.30, 0.56, 0.300, 1.000, False),
    Mode("twin", pat_twin, -0.22, 0.80, 0.225, 1.000, False),
    Mode("breathe", pat_breathe, 0.05, 0.62, 0.100, 0.875, False),
    Mode("drop", pat_drop, 0.40, 0.50, 0.330, 1.000, False),
    Mode("stage", pat_stage, 0.00, 0.50, 0.400, 1.000, False),
    Mode("arena", pat_arena, 0.00, 0.55, 0.450, 1.000, False),
    Mode("scanner", pat_scanner, 0.00, 0.98, 0.060, 1.000, False),
    Mode("eq", pat_eq, 0.00, 0.33, -0.330, 1.000, False),
    Mode("rain", pat_rain, 0.00, 0.55, 0.120, 1.000, False),
    Mode("fire", pat_fire, 0.00, 0.00, 0.130, 1.000, False),
    Mode("storm", pat_storm, 0.00, 0.62, 0.120, 0.800, False),
    Mode("siren", pat_siren, 0.00, 0.00, 0.640, 1.000, False),
    Mode("aurora", pat_aurora, 0.00, 0.38, 0.450, 0.900, False),
]
# drawn fresh every frame instead of a sliding table (see SHOWS)
LIVE = ("stage", "arena", "scanner", "eq", "rain", "fire", "storm", "siren", "aurora")
# animations drawn together; the festival ones are opt-in so the default stays calm
MODES_ON = [m.name for m in MODES if m.name not in ("drop",) + LIVE]


def hex_to_bgr(code):
    v = int(code[1:], 16)
    return (v & 255) / 255.0, (v >> 8 & 255) / 255.0, (v >> 16) / 255.0


def tone_colours(mode, tone):
    """(N, 3) BGR in 0..1 for a pattern's tones, from the chosen colour source."""
    if COLOUR_MODE == "spectrum":
        hue = (mode.hue + mode.span * tone) % 1.0
        return np.stack(hsv_to_bgr(hue, SATURATION * mode.sat, 1.0), axis=-1)
    live = SCREEN_COLOURS if COLOUR_MODE == "screen" else None
    if live is not None:
        stops = live.astype(np.float32)
    else:  # nothing captured yet: the saved palette stands in
        chosen = PALETTE[:1] if COLOUR_MODE == "single" else PALETTE
        stops = np.array([hex_to_bgr(c) for c in chosen], dtype=np.float32)
    if mode.cyclic:
        stops = np.vstack([stops, stops[:1]])
    if len(stops) == 1:
        return np.repeat(stops, len(tone), axis=0)
    pos = np.clip(tone, 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(pos.astype(np.intp), len(stops) - 2)
    f = (pos - i)[:, None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


def ring(mode, steps=RING_STEPS):
    """Colour (N, 3 BGR) and intensity (N,) at `steps` points around the loop."""
    u = np.linspace(0.0, 1.0, steps, endpoint=False, dtype=np.float32)
    tone, intensity = mode.pattern(u)
    return tone_colours(mode, tone), intensity


# -------------------------------------------------------------------- screen
def watched_rect(source=None):
    """Screen rectangle of what you are watching, or None when there is nothing.

    Source 0 is the window in front. When that is Glow's own settings window
    the one right behind it is taken instead: that is what you were watching
    while you tune the look.
    """
    source = SCREEN_SOURCE if source is None else source
    if source:
        screens = displays()
        return screens[min(source, len(screens)) - 1]
    hwnd = user32.GetForegroundWindow()
    title = ctypes.create_unicode_buffer(64)
    rect = w.RECT()
    while hwnd:
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            user32.GetWindowTextW(hwnd, title, 64)
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if title.value and title.value != "Glow" and width >= 200 and height >= 150:
                return rect.left, rect.top, width, height
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT: next window down the z-order
    return None


def grab(rect, size=SCREEN_SIZE):
    """Small BGRA snapshot (h, w, 4) of a screen rectangle.

    Without CAPTUREBLT the blit leaves out layered windows, so the glow
    itself never ends up in the picture it takes its colours from.
    """
    x, y, width, height = rect
    sw, sh = size
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, sw, sh)
    old = gdi32.SelectObject(dc, bitmap)
    try:
        gdi32.SetStretchBltMode(dc, 3)  # COLORONCOLOR: point samples, cheapest
        gdi32.StretchBlt(dc, 0, 0, sw, sh, screen, x, y, width, height, 0x00CC0020)  # SRCCOPY
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight, hdr.biPlanes, hdr.biBitCount = sw, -sh, 1, 32
        pixels = np.zeros((sh, sw, 4), np.uint8)
        gdi32.GetDIBits(dc, bitmap, 0, sh, pixels.ctypes.data, ctypes.byref(bmi), 0)
        return pixels
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)


_identify_turn = 0  # a newer frame cuts the one on show short
_identify_proc = WNDPROC(lambda hwnd, msg, wp, lp: user32.DefWindowProcW(hwnd, msg, wp, lp))


def identify(rect, label, seconds=1.8):
    """Frame a screen rectangle for a moment, labelled, then fade it out.

    Shows which monitor or window the screen colours come from. Reuses the
    overlay's click-through layered window, so it never takes the focus.
    """
    global _identify_turn
    _identify_turn += 1
    turn = _identify_turn
    cls = WNDCLASSEX()
    cls.cbSize = ctypes.sizeof(WNDCLASSEX)
    cls.lpfnWndProc = ctypes.cast(_identify_proc, ctypes.c_void_p)
    cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    cls.lpszClassName = CLASS_NAME
    user32.RegisterClassExW(ctypes.byref(cls))  # fails harmlessly once registered
    frame = Edge.__new__(Edge)  # the window and surface only, no glow maps
    frame.x, frame.y, frame.w, frame.h = rect
    frame._create_window(cls.hInstance)
    frame._create_surface()
    px, edge = frame.pixels, 8
    cyan = (255, 229, 0, 255)  # premultiplied BGRA of #00e5ff
    px[:edge], px[-edge:], px[:, :edge], px[:, -edge:] = cyan, cyan, cyan, cyan
    half = min(260, frame.w // 2 - edge)  # a small window gets a narrower label
    box = w.RECT(frame.w // 2 - half, frame.h // 2 - 40, frame.w // 2 + half, frame.h // 2 + 40)
    px[box.top:box.bottom, box.left:box.right] = (24, 18, 14, 255)
    font = gdi32.CreateFontW(34, 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 5, 0, "Segoe UI")
    old = gdi32.SelectObject(frame.dc, font)
    gdi32.SetBkMode(frame.dc, 1)                # TRANSPARENT: keep the box colour
    gdi32.SetTextColor(frame.dc, 0x00FFFFFF)
    user32.DrawTextW(frame.dc, f"GLOW toma los colores de aquí · {label}", -1,
                     ctypes.byref(box), 0x25)   # DT_CENTER | DT_VCENTER | DT_SINGLELINE
    px[box.top:box.bottom, box.left:box.right, 3] = 255  # GDI text leaves alpha at 0
    gdi32.SelectObject(frame.dc, old)
    gdi32.DeleteObject(font)
    msg = w.MSG()
    start = time.perf_counter()
    try:
        frame.push(1.0)
        frame.show(True)
        while turn == _identify_turn:
            left = seconds - (time.perf_counter() - start)
            if left <= 0:
                break
            frame.push(min(1.0, left / 0.5))  # fade out over the last half second
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(1 / 60)
    finally:
        user32.DestroyWindow(frame.hwnd)
        gdi32.DeleteObject(frame.bitmap)
        gdi32.DeleteDC(frame.dc)
        user32.ReleaseDC(None, frame.screen_dc)


def screen_colours(pixels, slots=SCREEN_SLOTS):
    """The picture's vivid colours as (slots, 3) BGR at full brightness, or None.

    Lasers, LED walls and stage lights are what count, so every pixel votes
    for its hue weighted by how saturated and bright it is; the dark crowd
    and grey haze barely count. The strongest hues share the slots by how
    much of the picture they cover, ordered by hue so a slot keeps roughly
    the same colour from one snapshot to the next. A grey or dark picture
    returns None: the last colours are kept rather than fading to grey.
    """
    bgr = pixels[..., :3].reshape(-1, 3).astype(np.float32) / 255.0
    hi, lo = bgr.max(axis=1), bgr.min(axis=1)
    chroma = hi - lo
    weight = (chroma / np.maximum(hi, 1e-6)) ** 2 * hi * (hi > 0.12)
    if weight.sum() < 0.004 * len(bgr):
        return None
    b, g, r = bgr.T
    c = np.maximum(chroma, 1e-6)
    hue = np.select([hi == r, hi == g], [((g - b) / c) % 6.0, (b - r) / c + 2.0], (r - g) / c + 4.0) / 6.0
    bins = (hue * 12).astype(np.intp) % 12        # 30 degrees of hue each
    votes = np.bincount(bins, weight, 12)
    # a peak is a bin at least as strong as both neighbours, with a real share
    peaks = [i for i in range(12) if votes[i] >= votes[i - 1] and votes[i] >= votes[(i + 1) % 12]
             and votes[i] > 0.08 * votes.sum()]
    peaks = sorted(peaks, key=lambda i: -votes[i])[:slots]
    # slots handed out by share, largest remainder first
    share = votes[peaks] / votes[peaks].sum() * slots
    count = np.floor(share).astype(int)
    for i in np.argsort(count - share)[:slots - count.sum()]:
        count[i] += 1
    out = []
    for i in sorted(range(len(peaks)), key=lambda i: peaks[i]):
        mine = bins == peaks[i]
        colour = (bgr[mine] * weight[mine, None]).sum(axis=0) / weight[mine].sum()
        out += [colour / max(colour.max(), 1e-6)] * count[i]
    return np.array(out, np.float32)


class Eye:
    """Watches the screen: live colours for COLOUR_MODE "screen", and flashes.

    Runs on its own thread (a snapshot waits ~25 ms on the compositor) and
    only while `active` is set, so it costs nothing when the glow is off or
    uses other colours. Like Listener, the render loop reads plain values.
    """

    def __init__(self, capture=True):
        self.active = False
        self.version = 0      # bumped whenever SCREEN_COLOURS moves
        self.flashes = 0      # flash counter: the render loop fires on each new one
        self._bright = None   # running share of near-white pixels
        self._struck = -1e9
        if capture:
            threading.Thread(target=self._run, daemon=True).start()

    def look(self, pixels, now):
        """Take one snapshot into account."""
        global SCREEN_COLOURS
        new = screen_colours(pixels)
        if new is not None:
            old = SCREEN_COLOURS
            SCREEN_COLOURS = new if old is None else old + (new - old) * SCREEN_BLEND
            self.version += 1
        # strobes and pyro turn a big part of the picture near white at once
        bright = float(np.mean(pixels[..., :3].min(axis=-1) > 200))
        if (self._bright is not None and bright - self._bright > FLASH_RISE
                and now - self._struck >= FLASH_GAP):
            self.flashes += 1
            self._struck = now
        self._bright = bright if self._bright is None else self._bright + (bright - self._bright) * 0.3

    def _run(self):
        while True:
            started = time.perf_counter()
            if self.active:
                try:
                    rect = watched_rect()
                    if rect:
                        self.look(grab(rect), started)
                except Exception:
                    pass  # a window closing mid-snapshot; try again next time
            else:
                self._bright = None  # a stale level would fake a flash on return
            time.sleep(max(0.05, 1.0 / SCREEN_FPS - (time.perf_counter() - started)))


# ------------------------------------------------------------------- settings
# name: (min, max) for numbers, allowed values for text, None for switches/lists
LIMITS = {
    "ENABLED": None, "AGENT": None, "MUSIC": None, 
    "MODES_ON": None, "PALETTE": None,
    "COLOUR_MODE": ("spectrum", "single", "palette", "screen"),
    "MONITORS": ("all", "primary"),
    "SPEED": (0.0, 3.0),
    "PEAK_ALPHA": (0.1, 1.0), "THICKNESS": (20, 200), "FALLOFF": (0.8, 5.0),
    "SATURATION": (0.0, 1.0), "FADE_SECONDS": (0.05, 2.0), "FPS": (24, 144),
    "SURGE_THICKNESS": (40, 300), "WHITE_HOT": (0.0, 1.0), "BEAT_FLOOR": (0.0, 1.0),
    "KICK_JUMP": (0.0, 0.15), "BEAT_SPEED": (0.0, 6.0), "TREBLE_SPEED": (0.0, 4.0),
    "STEREO": (0.0, 1.0), "BEAT_SENSITIVITY": (1.1, 4.0), "BEAT_DECAY": (1.0, 15.0),
    "ATTACK": (0.002, 0.1), "RELEASE": (0.03, 1.0), "MUSIC_RMS_ON": (0.001, 0.05),
    "MUSIC_HOLD": (0.5, 10.0), "SCREEN_SOURCE": (0, 8), "SCREEN_FLASH": (0.0, 1.0),
}
DEFAULTS = {k: (list(v) if isinstance(v, list) else v)
            for k, v in globals().items() if k in LIMITS}
# changing these needs new windows, so the daemon restarts itself
GEOMETRY = ("SURGE_THICKNESS", "MONITORS")
HEX = re.compile(r"#[0-9a-fA-F]{6}")


def apply_config(data):
    """Validate untrusted settings and make them live. Bad entries are skipped."""
    names = [m.name for m in MODES]
    for key, value in data.items():
        if key not in LIMITS:
            continue
        default, rule = DEFAULTS[key], LIMITS[key]
        try:
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    continue
            elif isinstance(default, (int, float)):
                lo, hi = rule
                value = type(default)(min(hi, max(lo, float(value))))
            elif isinstance(default, str):
                if value not in rule:
                    continue
            elif not isinstance(value, list):
                continue
            elif key == "PALETTE":
                value = [c.lower() for c in value if isinstance(c, str) and HEX.fullmatch(c)][:6]
            elif key == "MODES_ON":
                value = [n for n in names if n in value]
            if value == []:
                continue  # at least one colour and one animation, always
        except (TypeError, ValueError):
            continue
        globals()[key] = value
    # a bass hit must reach at least as deep as the resting glow
    globals()["THICKNESS"] = min(THICKNESS, SURGE_THICKNESS)


def current_config():
    return {k: globals()[k] for k in LIMITS}


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            apply_config(json.load(f))
    except (OSError, ValueError, AttributeError):
        pass  # missing or broken file: keep what we have


def save_config():
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current_config(), f, indent=2)
    os.replace(tmp, CONFIG)  # atomic: the daemon never reads half a file


def config_stamp():
    try:
        return os.path.getmtime(CONFIG)
    except OSError:
        return None


def preview_mode():
    """None when not previewing, else the pinned animation name ('' = all on)."""
    try:
        with open(PREVIEW, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def colour_table(depth, mode, reach=1.0, peak=None, hot=0.0):
    """Flat premultiplied-BGRA table laid out as [u * depth + d].

    Stored flat and doubled so a frame is a single `np.take`: advancing the
    animation by one step is just adding `depth` to every index, and the
    duplicate half absorbs the wraparound so no modulo is needed.
    """
    flat = paint_table(*ring(mode), depth, reach, peak, hot).reshape(-1, 4)
    return np.concatenate([flat, flat], axis=0)


def paint_table(colour, intensity, depth, reach=1.0, peak=None, hot=0.0):
    """(RING_STEPS, depth, 4) premultiplied BGRA from ring colours and intensity.

    `reach` is the fraction of `depth` the glow covers, and `hot` blends the
    rim right at the edge towards white; the music surge levels vary both.
    """
    peak = PEAK_ALPHA if peak is None else peak
    # reach, peak and hot: one number, or one per ring sample (the arena's speakers)
    reach, peak, hot = (np.asarray(v, np.float32)[..., None] for v in (reach, peak, hot))
    d = np.minimum(np.arange(depth, dtype=np.float32) / (depth * reach), 1.0)
    profile = ((1.0 - d) ** FALLOFF) * peak * 255.0             # (depth,) or (N, depth)
    core = hot * (1.0 - d) ** 6                                 # white-hot rim
    # channel = lit * (colour * profile * (1 - core) + profile * core) and
    # alpha = lit * profile: one small matrix product builds the whole table.
    # White is scaled by intensity too, so every channel stays <= alpha.
    shades = np.stack([profile * (1.0 - core), profile * core], axis=-2).astype(np.float32)
    lit = intensity.astype(np.float32)
    rows = np.empty((len(lit), 4, 2), np.float32)               # B, G, R, A
    rows[:, :3, 0] = colour * lit[:, None]
    rows[:, :, 1] = lit[:, None]
    rows[:, 3, 0] = lit
    if shades.ndim == 2:
        out = (rows.reshape(-1, 2) @ shades).reshape(len(lit), 4, depth)
    else:  # one shade per sample: ~5x slower, only the arena pays it
        out = rows @ shades
    return out.transpose(0, 2, 1).astype(np.uint8)


def blend(frames, depth):
    """Several animations at once, as (words, offset, brightness) frames.

    Their light adds up like stage lights do, clipped at full: premultiplied
    colour and alpha both just sum, so the result stays valid premultiplied.
    """
    n = RING_STEPS * depth
    out = np.zeros((n, 4), np.float32)
    for words, offset, pulse in frames:
        out += words[offset:offset + n].view(np.uint8).reshape(-1, 4) * np.float32(pulse)
    return as_words(np.minimum(out, 255.0).astype(np.uint8))


def as_words(lut):
    """BGRA rows as one uint32 per pixel: `np.take` on words is much faster."""
    return np.ascontiguousarray(lut).view(np.uint32).ravel()


def surge_tables(depth, mode):
    """Glow tables from quiet (thin, coloured) to full bass (deep, white-hot)."""
    calm = min(1.0, THICKNESS / depth)
    tables = []
    for level in range(SURGE_LEVELS):
        s = level / (SURGE_LEVELS - 1)
        tables.append(as_words(colour_table(
            depth, mode,
            reach=calm * 0.6 + (1.0 - calm * 0.6) * s,
            peak=PEAK_ALPHA + (1.0 - PEAK_ALPHA) * s,
            hot=WHITE_HOT * s * s,
        )))
    return tables


def ring_index(perimeter_pos):
    """Pattern table index for a normalised perimeter position in 0..1.

    One full turn of the loop maps to one full turn of the pattern, so
    position 0 and position 1 land on the same entry and the seam closes.
    """
    return (perimeter_pos * RING_STEPS).astype(np.intp) % RING_STEPS


def is_beat(flux, mean, energy):
    """A beat is a jump in energy well above the recent average jump."""
    return (
        energy > MUSIC_RMS_ON
        and mean > 1e-6
        and flux > mean * BEAT_SENSITIVITY
    )


class Listener:
    """WASAPI loopback analyser: what the PC plays, split into bass/mid/treble.

    Only the loopback of the default speaker is opened, never a microphone.
    Runs on its own thread because `record` blocks. It reads whatever the
    backend has (~10 ms chunks) so there is no backlog and no added latency,
    and analyses a sliding 2048-sample window so bass stays resolvable.

    The render loop only reads single floats and ints, so no lock is needed:
    a value one frame late is invisible.
    """

    RATE = 48000
    WINDOW = 2048   # ~43 ms of audio, 23 Hz FFT bins: kick and bass line separate
    DEVICE_CHECK = 1.0  # seconds between checks for a new default output

    def __init__(self, capture=True):
        self.beat = 0.0       # 1.0 right on a beat, decayed by the render loop
        self.beats = 0        # onset counter: the render loop kicks on each new one
        self.last_sound = -1e9
        self.bass = self.mid = self.treble = 0.0  # 0..1, auto-gained
        self.left_share = 0.5  # 0 = all right channel, 1 = all left
        self._ring = np.zeros((self.WINDOW, 2), dtype=np.float32)
        self._taper = (np.hanning(self.WINDOW) / (self.WINDOW / 4)).astype(np.float32)
        hz = np.fft.rfftfreq(self.WINDOW, 1.0 / self.RATE)
        self._bands = [(hz >= lo) & (hz < hi) for lo, hi in BANDS]
        self._peaks = np.full(len(BANDS), AGC_FLOOR)
        self._previous = 0.0
        self._flux = collections.deque(maxlen=100)  # ~1 s of bass jumps
        self._struck = -1e9
        self._fed = None
        if capture and soundcard is not None:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def playing(self):
        return time.perf_counter() - self.last_sound < MUSIC_HOLD

    def feed(self, data, now):
        """Analyse one chunk of frames x channels float32 audio."""
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        data = data[:, :2]
        n = min(len(data), self.WINDOW)
        if n == 0:
            return
        ring = self._ring
        ring[:-n] = ring[n:]
        ring[-n:] = data[-n:]
        dt = 0.0 if self._fed is None else now - self._fed
        self._fed = now

        energy = float(np.sqrt(np.mean(np.square(data, dtype=np.float32))))
        if energy > MUSIC_RMS_ON:
            self.last_sound = now

        spectrum = np.abs(np.fft.rfft(ring.mean(axis=1) * self._taper))
        raw = np.array([np.sqrt(np.mean(spectrum[m] ** 2)) for m in self._bands])
        decayed = np.maximum(self._peaks * np.exp(-dt / AGC_SECONDS), AGC_FLOOR)
        self._peaks = np.maximum(raw, decayed)
        # squared so the gap between a hit and the bed under it gets wider
        self.bass, self.mid, self.treble = (raw / self._peaks) ** 2

        left, right = np.sqrt(np.mean(np.square(ring), axis=0))
        self.left_share = float(left / (left + right)) if left + right > 1e-6 else 0.5

        flux = max(0.0, float(raw[0]) - self._previous)
        self._previous = float(raw[0])
        mean = sum(self._flux) / len(self._flux) if self._flux else 0.0
        if (is_beat(flux, mean, energy) and self.bass > BEAT_MIN_LEVEL
                and now - self._struck >= BEAT_GAP):
            self.beat = 1.0
            self.beats += 1
            self._struck = now
        self._flux.append(flux)

    @staticmethod
    def wanted():
        return ENABLED and MUSIC

    def _run(self):
        while True:
            if not self.wanted():
                time.sleep(0.5)  # music reaction off: capture nothing at all
                continue
            try:
                speaker = soundcard.default_speaker()
                source = soundcard.get_microphone(speaker.id, include_loopback=True)
                if not source.isloopback:
                    # matched by exact device id, so this should never happen;
                    # refuse rather than risk recording a real microphone
                    raise RuntimeError("not a loopback device")
                checked = time.perf_counter()
                with source.recorder(samplerate=self.RATE, blocksize=480) as rec:
                    while True:
                        self.feed(rec.record(numframes=None), time.perf_counter())
                        now = time.perf_counter()
                        if not self.wanted():
                            self.bass = self.mid = self.treble = 0.0
                            break
                        if now - checked > self.DEVICE_CHECK:
                            checked = now
                            if soundcard.default_speaker().id != speaker.id:
                                break  # output switched (headphones, HDMI...)
            except Exception:
                # device vanished, or nothing to capture yet; retry
                time.sleep(2.0)


class Edge:
    """One thin layered window along a single screen edge."""

    def __init__(self, hinstance, rect, depth_map, perimeter_map, depth, pan=0.5):
        self.x, self.y, self.w, self.h = rect
        self.pan = pan  # 0 = leftmost monitor, 1 = rightmost: picks the channel
        self._create_window(hinstance)
        self._create_surface()

        # every pixel is one entry in the flat colour table; the animation
        # only ever adds a multiple of `depth` to these indices
        self.index = ring_index(perimeter_map) * depth + depth_map
        self.scratch = np.empty_like(self.index)

    def _create_window(self, hinstance):
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            CLASS_NAME, "OpenClaw Glow", WS_POPUP,
            self.x, self.y, self.w, self.h, None, None, hinstance, None,
        )
        self._pos = w.POINT(self.x, self.y)
        self._size = w.SIZE(self.w, self.h)
        self._src = w.POINT(0, 0)

    def _create_surface(self):
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = self.w
        hdr.biHeight = -self.h  # negative => rows run top to bottom
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = 0  # BI_RGB

        self.screen_dc = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(
            self.screen_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
        gdi32.SelectObject(self.dc, self.bitmap)
        buf = ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8 * (self.w * self.h * 4)))
        self.pixels = np.ctypeslib.as_array(buf.contents).reshape(self.h, self.w, 4)
        self.pixels[:] = 0
        self.words = self.pixels.view(np.uint32).reshape(self.h, self.w)

    def paint(self, words, offset):
        np.add(self.index, offset, out=self.scratch)
        # indices are always in range; 'clip' skips the buffered bounds check
        np.take(words, self.scratch, out=self.words, mode="clip")

    def push(self, opacity):
        opacity = min(1.0, max(0.0, opacity))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, int(opacity * 255), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd, self.screen_dc, ctypes.byref(self._pos),
            ctypes.byref(self._size), self.dc, ctypes.byref(self._src), 0,
            ctypes.byref(blend), ULW_ALPHA,
        )

    def show(self, visible):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)
        if visible:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def build_maps(sw, sh, t):
    """Per-window (rect, depth_map, arc_map) for one screen, plus its perimeter.

    `arc_map` is the distance travelled along that screen's outline, in
    pixels, not yet normalised: the caller chains several screens into one
    longer loop before turning arc length into a pattern position.

    Corners belong to the top and bottom windows, so no two windows overlap
    and nothing double-blends. Inside a corner the depth is the distance to
    the nearest edge, which is what makes the falloff continuous across the
    seam with the side windows.
    """
    side_h = max(1, sh - 2 * t)
    perimeter = float(2 * sw + 2 * side_h)

    rows = np.arange(t, dtype=np.intp)[:, None]
    cols = np.arange(sw, dtype=np.intp)[None, :]
    edge_dist = np.minimum(cols, sw - 1 - cols)   # distance to the nearest side

    # top: depth grows downward, arc runs left to right
    top_depth = np.minimum(rows, edge_dist)
    top_arc = np.broadcast_to(cols, (t, sw)).astype(np.float32)

    # bottom: depth grows upward, arc runs right to left
    bot_depth = np.minimum(t - 1 - rows, edge_dist)
    bot_arc = np.broadcast_to((sw + side_h + (sw - 1 - cols)).astype(np.float32), (t, sw))

    srows = np.arange(side_h, dtype=np.intp)[:, None]
    scols = np.arange(t, dtype=np.intp)[None, :]

    # right: depth grows leftward, arc continues down the right side
    right_depth = np.broadcast_to(t - 1 - scols, (side_h, t))
    right_arc = np.broadcast_to((sw + srows).astype(np.float32), (side_h, t))

    # left: depth grows rightward, arc closes the loop going up
    left_depth = np.broadcast_to(scols, (side_h, t))
    left_arc = (2 * sw + side_h + (side_h - 1 - srows)).astype(np.float32)
    left_arc = np.broadcast_to(left_arc, (side_h, t))

    windows = [
        ((0, 0, sw, t), top_depth, top_arc),
        ((0, sh - t, sw, t), bot_depth, bot_arc),
        ((sw - t, t, t, side_h), right_depth, right_arc),
        ((0, t, t, side_h), left_depth, left_arc),
    ]
    return windows, perimeter


def ring_coords(screens, t):
    """Where every ring sample sits on the desk.

    X runs across all monitors (0 = far left, 1 = far right), Y down its own
    screen (0 = top, 1 = bottom), and the screen index says which monitor.
    `screens` must be in the order Glow chains them, and the outline walk
    mirrors build_maps.
    """
    sides = [max(1, sh - 2 * t) for _, _, _, sh in screens]
    perimeters = [2 * sw + 2 * side for (_, _, sw, _), side in zip(screens, sides)]
    arc = (np.arange(RING_STEPS) + 0.5) / RING_STEPS * sum(perimeters)
    left = min(s[0] for s in screens)
    right = max(s[0] + s[2] for s in screens)
    x = np.zeros(RING_STEPS, np.float32)
    y = np.zeros(RING_STEPS, np.float32)
    screen = np.zeros(RING_STEPS, np.intp)
    start = 0.0
    for i, ((ox, _, sw, sh), side, perimeter) in enumerate(zip(screens, sides, perimeters)):
        here = (arc >= start) & (arc < start + perimeter)
        a = arc[here] - start
        edge = [a < sw, a < sw + side, a < 2 * sw + side]  # top, right, bottom, left
        px = np.select(edge, [a, sw, 2 * sw + side - a], 0)
        py = np.select(edge, [0, t + a - sw, sh], t + side - (a - 2 * sw - side))
        x[here] = (ox + px - left) / (right - left)
        y[here] = py / sh
        screen[here] = i
        start += perimeter
    return x, y, screen


# ---------------------------------------------------------------- live shows
# The modes above are a fixed pattern slid around the loop. A live show is
# drawn fresh every frame instead: every ring sample knows where it sits on the
# desk (ring_coords), so a show can treat the monitors as one wall. Shows only
# ever paint the ring, so like every mode they stay on the edge strips and
# never cover the screen.

Sound = collections.namedtuple("Sound", "bass mid treble left_share kicked music")


def drift(x, t, scale=1.0, seed=0.0):
    """Smooth noise in 0..1 along x that moves with t: a few sines out of step."""
    k = np.array([1.0, 2.3, 4.1, 7.7])
    s = np.sin(2.0 * np.pi * (np.outer(x, k * scale) + t * np.array([0.13, -0.21, 0.37, -0.55]))
               + np.array([0.0, 1.7, 4.2, 2.9]) + seed) @ (1.0 / k)
    return np.clip(0.5 + 0.8 * s / (1.0 / k).sum(), 0.0, 1.0)


class Live:
    """Base of the live shows.

    A subclass implements `draw(mode, now, dt, sound)`, which returns per ring
    sample the summed colour (N, 3), the summed weight (N,), the surge (how
    deep and white the glow pushes in) and its own reach, or None to let the
    surge set it. A flash (a drop, a strobe in the video) is added on top of
    every show. Without music (agent glow, preview) a show runs on a demo
    beat of its own.

    With PUMP set, music drives a show the way it drives the sliding modes:
    every kick and the bass push the glow deep and white-hot, and the
    brightness dips to BEAT_FLOOR between beats. `self.hit` is that kick
    envelope (real music only), for a show's own reaction to the beat.
    """

    DEMO_BPM = 124
    FLASH_DECAY = 3.0
    PUMP = True

    def __init__(self, x, y, screen):
        if screen.max() == 0:  # one monitor: its thirds stand in for three
            screen = np.minimum((x * 3.0).astype(np.intp), 2)
        self.x, self.y, self.screen = x, y, screen
        self.count = int(screen.max()) + 1
        self.pan = screen / max(1, self.count - 1)   # 0 = leftmost monitor, 1 = rightmost
        self.lo = np.array([x[screen == m].min() for m in range(self.count)])
        self.hi = np.array([x[screen == m].max() for m in range(self.count)])
        self.centres = (self.lo + self.hi) / 2.0
        # 0..1 across its own monitor
        self.local = (x - self.lo[screen]) / np.maximum(self.hi - self.lo, 1e-6)[screen]
        self.floor = 1.0 - y            # 0 on the bottom edges, 1 on the top ones
        self.top = (1.0 - y) ** 4       # sparks live near the top edges
        self.side = (y > 0.0) & (y < 1.0)
        self.bottom = y >= 1.0
        self.sparks = np.zeros_like(x)
        self.kicks = 0
        self.last_kick = -1e9
        self.flash = self.demo = self.hit = 0.0
        self.calm = 1.0
        self.intensity = np.zeros_like(x)
        self.rng = np.random.default_rng()

    def demo_sound(self, now, dt):
        """A four-on-the-floor kick for when nothing is playing."""
        self.demo += dt * self.DEMO_BPM / 60.0
        kicked = self.demo >= 1.0
        self.demo %= 1.0
        return Sound(0.5 * np.exp(-5.0 * self.demo), 0.4, 0.2, 0.5 + 0.35 * np.sin(now * 1.7), kicked, False)

    def spark(self, dt, rate, fade):
        """Random sparkles: `rate` per sample per second, each fading at `fade`."""
        self.sparks[self.rng.random(len(self.x)) < dt * rate] = 1.0
        self.sparks *= np.exp(-fade * dt)

    def frame(self, mode, depth, now, dt, sound):
        """This frame's glow table, laid out like colour_table's first half."""
        music = sound.music
        if not music:
            sound = self.demo_sound(now, dt)
        self.hit = 1.0 if music and sound.kicked else self.hit * np.exp(-BEAT_DECAY * dt)
        self.calm = min(1.0, THICKNESS / depth)
        num, tot, surge, reach = self.draw(mode, now, dt, sound)
        self.flash *= np.exp(-self.FLASH_DECAY * dt)
        num = num + self.flash
        tot = tot + self.flash
        self.intensity = np.minimum(tot, 1.0)
        if self.PUMP and music:  # the same pulse the sliding modes get
            punch = max(sound.bass, self.hit)
            surge = np.maximum(surge, punch)
            self.intensity = self.intensity * (BEAT_FLOOR + (1.0 - BEAT_FLOOR) * max(punch, sound.mid))
        surge = np.clip(np.maximum(surge, self.flash), 0.0, 1.0)
        return as_words(paint_table(
            num / np.maximum(tot, 1e-6)[:, None], self.intensity, depth,
            reach=self.calm + (1.0 - self.calm) * surge if reach is None else np.maximum(reach, 0.05),
            peak=PEAK_ALPHA + (1.0 - PEAK_ALPHA) * surge,
            hot=np.minimum(1.0, WHITE_HOT * surge * surge + self.flash),
        ))


class Stage(Live):
    """The monitors as one festival stage.

    * each kick fires a shockwave from the centre of the monitor wall out to
      both sides, a new palette colour every time, the first of each bar wider;
    * the bass fills every screen from the floor up, like VU meters;
    * hi-hats throw sparks along the top edges;
    * when the kicks stop (breakdown) sparks build up everywhere, and the
      first kick after it is the drop: every edge flashes white.
    """

    PUMP = False       # its waves and meters already are the beat
    WAVE_SPEED = 1.2   # desk widths per second: centre to far edge in ~0.4 s
    WAVE_WIDTH = 0.05
    BREAKDOWN = 3.0    # seconds without a kick that make the next kick a drop
    FLASH_DECAY = 2.5

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.mid_x = np.abs(x - 0.5)   # distance from the centre of the wall
        self.waves = []                # (born, tone, width)

    def draw(self, mode, now, dt, s):
        if s.kicked:
            if s.music and now - self.last_kick > self.BREAKDOWN:
                self.flash = 1.0
            self.kicks += 1
            self.last_kick = now
            wide = 2.0 if self.kicks % 4 == 1 else 1.0
            # golden-ratio steps: every kick a colour far from the last one
            self.waves = self.waves[-5:] + [(now, self.kicks * 0.382 % 1.0, wide)]
        riser = float(np.clip((now - self.last_kick - 1.0) / 6.0, 0.0, 1.0)) if s.music else 0.0

        cols = tone_colours(mode, np.array([w[1] for w in self.waves] + [0.0, 1.0], np.float32))
        # a faint bed in the first colour, so the stage never goes fully dark
        tot = np.full_like(self.x, 0.06)
        num = tot[:, None] * cols[-2]
        for (born, _, wide), col in zip(self.waves, cols):
            age = now - born
            front = np.exp(-((self.mid_x - age * self.WAVE_SPEED) / (self.WAVE_WIDTH * wide)) ** 2)
            front *= np.exp(-3.0 * age)
            num += front[:, None] * col
            tot += front
        meter = 0.6 * np.clip((0.95 * s.bass - self.floor) / 0.1, 0.0, 1.0)
        num += meter[:, None] * tone_colours(mode, 0.5 * self.floor)
        tot += meter
        self.spark(dt, 1.5 * s.treble * self.top + 3.0 * riser, 12.0)
        num += self.sparks[:, None] * (0.5 + 0.5 * cols[-1])
        return num, tot + self.sparks, s.bass, None


class Arena(Live):
    """Every monitor is a speaker on a festival main stage.

    * left monitors play the left channel, right ones the right: each screen
      is a VU meter of its own speaker, rising from the floor with the bass;
    * kicks walk the stereo field: the downbeat thumps every speaker, then
      left, right, centre. A hit speaker's whole frame pumps deep into the
      screen and fires a shockwave across the other monitors;
    * laser beams sweep along the edges of the whole wall, faster with the mids;
    * a breakdown (no kicks) turns into a build: the lights go down, sparks
      pile up and a strobe chases from speaker to speaker, faster and faster;
    * the first kick after it is the drop: white flash, every speaker on
      every kick, strobes, full lasers and sparks for DROP_SECONDS.

    With one monitor its thirds play left, centre and right.
    """

    PUMP = False        # builds and drops need their own dark and light
    WAVE_SPEED = 1.6    # desk widths per second
    BREAKDOWN = 2.5     # seconds without a kick that make the next kick a drop
    BUILD_SECONDS = 8.0  # how long a build takes to reach full tension
    DROP_SECONDS = 10.0
    LASERS = 6
    DEMO_BPM = 128
    DEMO_GROOVE, DEMO_BUILD = 32, 16  # demo beats with kicks, then without

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        # the build strobe ping-pongs: left, centre, right, centre...
        self.chase = list(range(self.count)) + list(range(self.count - 2, 0, -1))
        self.pump = np.zeros(self.count, np.float32)
        self.waves = []                 # (born, origin x, tone, width)
        self.drop = self.lasers = self.roll = 0.0

    def demo_sound(self, now, dt):
        """Its own show: a groove, then a build without kicks, then the drop."""
        was = int(self.demo)
        self.demo += dt * self.DEMO_BPM / 60.0
        grooving = int(self.demo) % (self.DEMO_GROOVE + self.DEMO_BUILD) < self.DEMO_GROOVE
        bass = 0.85 * np.exp(-5.0 * (self.demo % 1.0)) if grooving else 0.0
        return Sound(bass, 0.4, 0.3, 0.5 + 0.35 * np.sin(now * 1.7),
                     int(self.demo) != was and grooving, False)

    def draw(self, mode, now, dt, s):
        self.pump *= np.exp(-9.0 * dt)
        self.drop = max(0.0, self.drop - dt / self.DROP_SECONDS)
        if s.kicked:
            if now - self.last_kick > self.BREAKDOWN:
                self.flash = self.drop = 1.0
                self.kicks = 0
            self.kicks += 1
            self.last_kick = now
            step = self.kicks % 4
            if step == 1 or self.drop > 0.3:
                hit = list(range(self.count))
            else:
                hit = [{2: 0, 3: self.count - 1, 0: self.count // 2}[step]]
            self.pump[hit] = 1.0
            if self.drop > 0.3:  # strobe on every kick of the drop
                self.flash = max(self.flash, 0.6 * self.drop)
            tone = self.kicks * 0.382 % 1.0  # golden-ratio steps: far from the last colour
            wide = 2.0 if step == 1 else 1.0
            self.waves = self.waves[-8:] + [(now, self.centres[h], tone, wide) for h in hit]
        riser = float(np.clip((now - self.last_kick - 0.8) / self.BUILD_SECONDS, 0.0, 1.0))

        cols = tone_colours(mode, np.array([w[2] for w in self.waves] + [0.0, 1.0], np.float32))
        tip = cols[-1]
        # a faint bed that dims as the build pulls the lights down
        tot = np.full_like(self.x, 0.05 * (1.0 - 0.8 * riser))
        num = tot[:, None] * cols[-2]
        for (born, origin, _, wide), col in zip(self.waves, cols):
            age = now - born
            front = np.exp(-((np.abs(self.x - origin) - age * self.WAVE_SPEED) / (0.045 * wide)) ** 2)
            front *= np.exp(-3.0 * age)
            num += front[:, None] * col
            tot += front

        # the speakers: each monitor's own channel, its own colour (new every bar)
        gain = np.clip(1.0 + STEREO * (2.0 * s.left_share - 1.0) * (1.0 - 2.0 * self.pan), 0.0, 2.0)
        level = np.clip(s.bass * gain, 0.0, 1.0)
        cone = self.pump[self.screen]
        meter = 0.7 * np.clip((0.95 * level - self.floor) / 0.1, 0.0, 1.0)
        own = tone_colours(mode, ((self.kicks // 4) * 0.382 + 0.5 * self.pan) % 1.0)
        num += (meter + cone)[:, None] * own
        tot += meter + cone

        self.lasers += dt * (0.12 + 0.5 * s.mid + 1.2 * self.drop + 0.8 * riser)
        k = np.arange(self.LASERS)
        beams = 0.5 + 0.5 * np.sin(2.0 * np.pi * self.lasers * (1.0 + 0.37 * k) + 2.4 * k)
        strength = min(1.0, 0.15 + 0.4 * s.treble + self.drop + 0.6 * riser)
        rays = np.exp(-((self.x[:, None] - beams) / 0.012) ** 2) * strength
        num += rays @ tone_colours(mode, (0.25 * k + 0.1 * self.lasers) % 1.0)
        tot += rays.sum(axis=1)

        if riser > 0.0:  # snare roll: the strobe runs from speaker to speaker
            self.roll += dt * (2.0 + 14.0 * riser ** 2)
            lit = self.chase[int(self.roll) % len(self.chase)]
            strobe = riser * (self.screen == lit) * (self.roll % 1.0 < 0.5)
            num += strobe[:, None] * (0.6 + 0.4 * tip)
            tot += strobe

        self.spark(dt, 2.0 * s.treble * self.top + 4.0 * riser + 3.0 * self.drop, 14.0)
        num += self.sparks[:, None] * (0.5 + 0.5 * tip)
        # per speaker: a thump or its bass pushes that monitor's glow deep and white
        return num, tot + self.sparks, np.maximum(cone, level), None


class Scanner(Live):
    """KITT's scanner across the whole monitor wall.

    One head sweeps the top and bottom edges from the far left to the far
    right and back with a fading trail; the side edges flare as it passes.
    The mids speed it up, and a kick thumps the monitor under the head.
    """

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.trail = np.zeros_like(x)
        self.thump = np.zeros(self.count, np.float32)
        self.sweep = 0.0

    def draw(self, mode, now, dt, s):
        self.sweep += dt * (1.1 + 2.5 * s.mid + 1.5 * s.bass + 4.0 * self.hit)
        head = 0.5 - 0.5 * np.cos(self.sweep)          # 0 = far left, 1 = far right
        beam = np.exp(-((self.x - head) / (0.025 * (1.0 + 1.5 * self.hit))) ** 2)
        self.trail = np.maximum(self.trail * np.exp(-(3.0 - 1.5 * self.hit) * dt), beam)
        self.thump *= np.exp(-6.0 * dt)
        if s.kicked:
            self.thump[np.abs(self.centres - head).argmin()] = 1.0
        thump = 0.9 * self.thump[self.screen]
        flare = beam * self.hit
        glow = 0.04 + self.trail
        num = glow[:, None] * tone_colours(mode, 1.0 - self.trail)   # head tone 0, tail tone 1
        num += thump[:, None] * tone_colours(mode, np.full(1, 0.5, np.float32)) + flare[:, None]
        return num, glow + thump + flare, np.maximum(beam * (0.3 + 0.7 * max(s.bass, self.hit)), thump), None


class Equalizer(Live):
    """The bottom edges are one wide spectrum analyser across the monitor wall.

    Bars rise into the screen with the music: bass in the middle of the
    wall, treble out at both ends, mirrored like a festival LED floor. Each
    bar has its own wobble that a kick reshuffles, and the bars fall back
    slowly. The side edges are VU meters of the whole mix, and hi-hats
    twinkle along the top.
    """

    BARS = 30

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        pos = x * self.BARS
        self.bar = np.minimum(pos.astype(np.intp), self.BARS - 1)
        self.lit = np.abs(pos % 1.0 - 0.5) < 0.38       # a dark gap between bars
        self.band = np.abs(np.arange(self.BARS) + 0.5 - self.BARS / 2) / (self.BARS / 2)  # 0 bass .. 1 treble
        self.shape = self.rng.random(self.BARS)
        self.height = np.zeros(self.BARS)

    def draw(self, mode, now, dt, s):
        if s.kicked:
            self.shape = 0.5 * self.shape + 0.5 * self.rng.random(self.BARS)
        level = np.interp(self.band, (0.0, 0.5, 1.0), (s.bass, s.mid, s.treble))
        target = np.clip(level * (0.55 + 0.6 * self.shape) + 0.5 * self.hit * (1.0 - self.band), 0.0, 1.0)
        self.height = np.maximum(target, self.height - 1.8 * dt)
        h = self.height[self.bar]
        loud = max(s.bass, s.mid)
        vu = np.clip((0.9 * loud - self.floor) / 0.08, 0.0, 1.0) * self.side
        floor = self.bottom & self.lit
        glow = 0.03 + np.where(floor, 0.15 + 0.85 * h, 0.7 * vu)
        num = glow[:, None] * tone_colours(mode, np.where(self.bottom, h, self.floor))  # tone = height
        self.spark(dt, 3.0 * s.treble * self.top, 10.0)
        num += self.sparks[:, None] * tone_colours(mode, np.ones(1, np.float32))
        reach = np.where(floor, 0.08 + 0.92 * h, self.calm)
        return num, glow + self.sparks, np.where(floor, h, 0.5 * vu * loud), reach


class Rain(Live):
    """Rain running down the side edges of every monitor.

    Drops slide down the left and right edges with a short streak and
    splash along the bottom edge when they land. Hi-hats and mids make it
    pour, every kick throws a burst of drops, and a soft cloud drifts along
    the top edges.
    """

    MAX_DROPS = 48

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.lane = np.where(self.side, screen * 2 + (self.local > 0.5), -1)  # one per side edge
        self.lanes = np.unique(self.lane[self.lane >= 0])
        self.drops = np.zeros((0, 4))     # lane, y, speed, tone
        self.splashes = []                # (born, x where it landed, screen, tone)

    def draw(self, mode, now, dt, s):
        n = self.rng.poisson(dt * (1.5 + 10.0 * s.treble + 4.0 * s.mid)) + (12 if s.kicked else 0)
        if n and len(self.lanes):
            new = np.column_stack([self.rng.choice(self.lanes, n), self.rng.uniform(0.0, 0.3, n),
                                   self.rng.uniform(0.5, 1.1, n) * (1.0 + s.bass + self.hit), self.rng.random(n)])
            self.drops = np.vstack([self.drops, new])[-self.MAX_DROPS:]
        self.drops[:, 1] += self.drops[:, 2] * dt
        landed = self.drops[:, 1] >= 1.0
        for lane, _, _, tone in self.drops[landed]:
            m = int(lane) // 2
            self.splashes.append((now, self.hi[m] if int(lane) % 2 else self.lo[m], m, tone))
        self.drops = self.drops[~landed]
        self.splashes = [p for p in self.splashes if now - p[0] < 0.8]

        behind = self.drops[:, 1:2] - self.y[None, :]          # > 0 above the head
        streak = np.where(behind >= 0.0, 0.6 * np.exp(-behind / 0.1), 0.0) + np.exp(-(behind / 0.02) ** 2)
        streak *= self.lane[None, :] == self.drops[:, 0:1]
        tones = list(self.drops[:, 3])
        rows = [streak]
        for born, at, m, tone in self.splashes:
            age = now - born
            front = age * 0.6 * (self.hi[m] - self.lo[m])          # crosses the monitor in ~1.6 s
            ripple = np.exp(-((np.abs(self.x - at) - front) / 0.01) ** 2) * np.exp(-5.0 * age)
            rows.append((ripple * self.bottom * (self.screen == m))[None, :])
            tones.append(tone)
        weights = np.vstack(rows)
        cloud = 0.03 + (0.15 * drift(self.x, now * 0.3, 2.0) + 0.5 * self.hit) * (self.y <= 0.0)
        num = cloud[:, None] * tone_colours(mode, np.full(1, 0.5, np.float32))
        num += weights.T @ tone_colours(mode, np.array(tones, np.float32))
        drops = weights.sum(axis=0)
        return num, cloud + drops, 0.5 * np.minimum(drops, 1.0), None


class Fire(Live):
    """Flames climb from the bottom edges of every monitor.

    Tongues of uneven height lick up from the bottom edge and flicker, the
    side edges burn from the floor up and cool towards the top, and embers
    glow along the sides. The bass feeds the fire; a kick makes it roar.
    """

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.roar = self.t = 0.0

    def draw(self, mode, now, dt, s):
        self.roar = max(self.roar * np.exp(-2.5 * dt), 1.0 if s.kicked else 0.0)
        heat = min(1.3, 0.4 + 0.5 * s.bass + 0.5 * self.roar)
        self.t += dt * (1.0 + 2.0 * heat)
        tongue = drift(self.x, 3.0 * self.t, 12.0) ** 1.5
        lick = drift(self.y + 5.0 * self.x, 4.0 * self.t, 3.0)
        flame = np.clip((heat * (0.3 + 0.5 * lick) - self.floor) / 0.2, 0.0, 1.0) * self.side
        glow = 0.03 + np.where(self.bottom, 0.5 + 0.5 * tongue, flame)
        # tone 1 is the hottest colour: the base of the flames
        burn = min(1.0, heat) * tongue
        num = glow[:, None] * tone_colours(mode, np.where(self.bottom, 0.4 + 0.6 * burn, 0.8 * flame))
        self.spark(dt, (4.0 * heat + 30.0 * self.hit) * self.side * self.y ** 2, 6.0)
        num += self.sparks[:, None] * tone_colours(mode, np.ones(1, np.float32))
        reach = np.where(self.bottom, 0.15 + 0.85 * burn, self.calm)
        return num, glow + self.sparks, np.where(self.bottom, burn, 0.5 * heat * flame), reach


class Storm(Live):
    """A thunderstorm along the edges.

    A dim bed drifts like clouds. Lightning strikes a random stretch of the
    loop, jagged and white-hot, flickering two or three times before it
    dies, and lights its whole monitor like sheet lightning. Kicks call the
    strikes; loud bass makes them likelier.
    """

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.u = (np.arange(len(x)) + 0.5) / len(x)   # position along the loop
        self.bolts = []                               # (born, shape over the ring)
        self.sheet = np.zeros(self.count, np.float32)

    def draw(self, mode, now, dt, s):
        chance = (1.0 if s.music else 0.45 + 0.5 * s.bass) if s.kicked else dt * 0.25
        if self.rng.random() < chance:
            centre = self.rng.random()
            near = ring_distance(self.u, centre) < self.rng.uniform(0.01, 0.05) * (1.0 + 1.5 * s.bass)
            self.bolts = self.bolts[-3:] + [(now, near * self.rng.uniform(0.35, 1.0, len(self.x)))]
            self.sheet[self.screen[int(centre * len(self.x))]] = 1.0
        self.bolts = [b for b in self.bolts if now - b[0] < 1.2]
        bolt = np.zeros_like(self.x)
        for born, shape in self.bolts:
            age = now - born   # strike, a second flicker, then the afterglow
            flicker = (age < 0.05) + 0.6 * (0.09 < age < 0.14) + 0.8 * (age > 0.2) * np.exp(-8.0 * (age - 0.2))
            bolt = np.maximum(bolt, shape * flicker)
        self.sheet *= np.exp(-5.0 * dt)
        sheet = 0.5 * self.sheet[self.screen]
        cloud = 0.05 + 0.1 * drift(self.x, now * 0.4, 3.0)
        num = cloud[:, None] * tone_colours(mode, 0.3 * cloud)
        num += bolt[:, None] * (0.6 + 0.4 * tone_colours(mode, np.ones(1, np.float32)))
        num += sheet[:, None] * tone_colours(mode, np.full(1, 0.5, np.float32))
        return num, cloud + bolt + sheet, bolt, None


class Siren(Live):
    """Police lights: each monitor's left half one colour, its right half the other.

    The halves take turns in the classic double blink, and neighbouring
    monitors run opposite, so the wall wig-wags from screen to screen. The
    mids speed it up; a kick fires both halves at once.
    """

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.half = ((self.local > 0.5) ^ (self.screen % 2 == 1)).astype(np.float32)
        self.clock = self.burst = 0.0

    def draw(self, mode, now, dt, s):
        self.clock += dt * (1.6 + 1.5 * s.mid)   # blink cycles per second
        if s.kicked and s.music:  # on the beat the other lamp's double blink starts
            self.clock = np.floor(self.clock * 2.0 + 1.0) / 2.0
        self.burst = max(self.burst * np.exp(-10.0 * dt), 1.0 if s.kicked else 0.0)
        p = self.clock % 1.0
        first = float(p < 0.12 or 0.2 < p < 0.32)
        second = float(0.5 < p < 0.62 or 0.7 < p < 0.82)
        on = np.maximum(np.where(self.half > 0.5, second, first), 0.6 * self.burst)
        glow = 0.15 + on  # the lamps never go fully dark
        num = glow[:, None] * tone_colours(mode, self.half)
        return num, glow, on * (0.5 + 0.5 * s.bass), None


class Aurora(Live):
    """Northern lights hanging from the top edges.

    Soft curtains of uneven length ripple slowly sideways across the whole
    wall and fade down the side edges, the colour drifting along them; the
    bottom stays dark. Calm on purpose, a mode to work with: music only makes
    the curtains sway and shine a little more.
    """

    def __init__(self, x, y, screen):
        super().__init__(x, y, screen)
        self.t = 0.0

    def draw(self, mode, now, dt, s):
        self.t += dt * (0.25 + 0.6 * s.mid * s.music + 1.5 * self.hit)
        curtain = drift(self.x, self.t, 4.0) ** 2
        hang = np.where(self.side, np.exp(-4.0 * self.y), 1.0) * (self.y < 1.0)
        glow = 0.02 + (0.25 + 0.75 * curtain) * hang * (0.8 + 0.3 * s.bass * s.music + 0.6 * self.hit)
        num = glow[:, None] * tone_colours(mode, drift(self.x, 0.5 * self.t, 1.5, 2.0))
        reach = np.where(self.y <= 0.0, 0.2 + 0.8 * curtain, self.calm)
        return num, glow, 0.3 * curtain * hang, reach


SHOWS = {"stage": Stage, "arena": Arena, "scanner": Scanner, "eq": Equalizer, "rain": Rain,
         "fire": Fire, "storm": Storm, "siren": Siren, "aurora": Aurora}


class Tray:
    """Notification-area icon: click to open the settings, right-click to quit.

    It lives in the daemon, the one process that keeps running after the
    settings window closes. Its window shares the render thread, so the
    render loop's PeekMessage pump delivers its messages.
    """

    def __init__(self, hinstance):
        self.quit = False
        self.wndproc = WNDPROC(self._proc)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = ctypes.cast(self.wndproc, ctypes.c_void_p)
        cls.hInstance = hinstance
        cls.lpszClassName = "GlowTray"
        user32.RegisterClassExW(ctypes.byref(cls))
        # Explorer restarts (and a sign-in race) wipe the icon; it tells us so.
        # Set before the window exists: creating it already calls _proc.
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.hwnd = user32.CreateWindowExW(0, "GlowTray", "GLOW", 0, 0, 0, 0, 0,
                                           None, None, hinstance, None)
        small = user32.GetSystemMetrics(49)  # SM_CXSMICON
        icon = user32.LoadImageW(None, icon_file(), 1, small, small, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if not icon:
            icon = shell32.ExtractIconW(hinstance, sys.executable, 0)
        if not icon or icon == 1:  # no icon anywhere
            icon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION
        self.data = NOTIFYICONDATA(
            cbSize=ctypes.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1,
            uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
            hIcon=icon, szTip="GLOW",
        )
        self.add()

    def add(self):
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))

    def remove(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))

    def _proc(self, hwnd, msg, wp, lp):
        if msg == WM_TRAY:
            if lp & 0xFFFF == WM_LBUTTONUP:
                spawn(app_argv())
            elif lp & 0xFFFF == WM_RBUTTONUP:
                self.menu()
            return 0
        if msg == self.taskbar_created:
            self.add()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, MENU_OPEN, "Abrir GLOW")
        user32.AppendMenuW(menu, 0, MENU_QUIT, "Salir")
        user32.SetMenuDefaultItem(menu, MENU_OPEN, 0)
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # or the menu never closes
        choice = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY,
                                       pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        if choice == MENU_OPEN:
            spawn(app_argv())
        elif choice == MENU_QUIT:
            self.quit = True


class Glow:
    def __init__(self):
        screens = monitors()
        smallest = min(min(sw, sh) for _, _, sw, sh in screens)
        self.depth = max(1, min(SURGE_THICKNESS, smallest // 2))
        self.geometry = tuple(globals()[k] for k in GEOMETRY)
        self.ear = Listener()
        self.eye = Eye()
        self.build_tables()
        self.phase = np.zeros(len(MODES))   # each animation keeps its own place

        self.wndproc = WNDPROC(
            lambda hwnd, msg, wp, lp: user32.DefWindowProcW(hwnd, msg, wp, lp)
        )
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = ctypes.cast(self.wndproc, ctypes.c_void_p)
        cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(cls))
        self.tray = Tray(cls.hInstance)

        # Chain every screen's outline into ONE loop so the pattern crosses
        # from monitor to monitor instead of each screen animating alone.
        screens = sorted(screens, key=lambda r: (r[0], r[1]))
        layout = [(s, build_maps(s[2], s[3], self.depth)) for s in screens]
        total = sum(perimeter for _, (_, perimeter) in layout)
        centres = [x + sw / 2 for x, _, sw, _ in screens]
        span = max(centres) - min(centres)

        self.edges = []
        travelled = 0.0
        for (ox, oy, sw, _), (windows, perimeter) in layout:
            pan = (ox + sw / 2 - min(centres)) / span if span else 0.5
            for (rx, ry, rw, rh), depth, arc in windows:
                position = (travelled + arc) / total
                self.edges.append(Edge(
                    cls.hInstance, (ox + rx, oy + ry, rw, rh),
                    np.ascontiguousarray(depth),
                    np.ascontiguousarray(position), self.depth, pan,
                ))
            travelled += perimeter
        x, y, screen = ring_coords(screens, self.depth)
        self.shows = {name: show(x, y, screen) for name, show in SHOWS.items()}

    def build_tables(self, only=None):
        """Per mode: the calm agent look, plus the music surge levels.

        Built up front (and again when settings change) so switching
        animation or bass level never stalls a frame. With screen colours
        only the animation on show is rebuilt as the picture changes.
        """
        calm = min(1.0, THICKNESS / self.depth)
        if only is None:
            # live modes paint their own tables every frame
            self.calm = [None if m.name in LIVE else as_words(colour_table(self.depth, m, reach=calm))
                         for m in MODES]
            self.surge = [None if m.name in LIVE else surge_tables(self.depth, m) for m in MODES]
            self.tinted = [self.eye.version] * len(MODES)
        else:
            self.calm[only] = as_words(colour_table(self.depth, MODES[only], reach=calm))
            self.surge[only] = surge_tables(self.depth, MODES[only])
            self.tinted[only] = self.eye.version

    def layer(self, i, now, dt, sound, glare, flashed):
        """One animation's frame as (words, table offset, brightness)."""
        mode = MODES[i]
        show = self.shows.get(mode.name)
        if show:
            if flashed:  # a strobe in the video: the whole show flashes with it
                show.flash = max(show.flash, glare)
            return show.frame(mode, self.depth, now, dt, sound), 0, 1.0
        base = mode.speed * SPEED
        if sound.music:
            # bass pushes the glow deeper and whiter, every band pumps the
            # brightness, beats whip the pattern round, hi-hats shimmer it
            surge = max(sound.bass, self.ear.beat, glare)
            words = self.surge[i][round(surge * (SURGE_LEVELS - 1))]
            pulse = BEAT_FLOOR + (1.0 - BEAT_FLOOR) * max(surge, sound.mid)
            speed = base * (1.0 + BEAT_SPEED * self.ear.beat + TREBLE_SPEED * sound.treble)
            if sound.kicked:
                self.phase[i] += KICK_JUMP * np.sign(base)
        else:
            words, pulse, speed = self.calm[i], 1.0, base
        self.phase[i] = (self.phase[i] + speed * dt) % 1.0
        return words, int(self.phase[i] * RING_STEPS) % RING_STEPS * self.depth, pulse

    def run(self):
        """Render until told to quit. Returns "restart" if windows must change."""
        msg = w.MSG()
        opacity = 0.0
        shown = False
        last = polled = time.perf_counter()
        painted = None
        bass = mid = treble = 0.0   # smoothed band levels
        left_share = 0.5
        kicks_seen = self.ear.beats
        flashes_seen = self.eye.flashes
        glare = 0.0                 # a flash in the picture, decaying like a beat
        retinted = last
        stamp = config_stamp()
        pinned = preview_mode()
        names = [m.name for m in MODES]

        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_DESTROY:
                    return None
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if self.tray.quit:
                return None

            now = time.perf_counter()
            dt = now - last
            last = now

            # settings window: new settings and the preview are polled twice a second
            if now - polled >= 0.5:
                polled = now
                pinned = preview_mode()
                if config_stamp() != stamp:
                    stamp = config_stamp()
                    load_config()
                    if tuple(globals()[k] for k in GEOMETRY) != self.geometry:
                        return "restart"
                    self.build_tables()
                    painted = None

            music = ENABLED and MUSIC and self.ear.playing
            agent = ENABLED and AGENT and os.path.exists(SENTINEL)
            self.ear.beat = max(0.0, self.ear.beat - dt * BEAT_DECAY)
            target = 1.0 if (agent or music or pinned is not None) else 0.0
            step = dt / FADE_SECONDS
            if opacity < target:
                opacity = min(target, opacity + step)
            else:
                opacity = max(target, opacity - step)

            self.eye.active = ENABLED and COLOUR_MODE == "screen" and opacity > 0.002
            if opacity <= 0.002:
                if shown:
                    for edge in self.edges:
                        edge.show(False)
                    shown = False
                    painted = None
                time.sleep(0.2)  # idle: poll the sentinel, burn nothing
                last = time.perf_counter()
                continue

            # fast attack, slower release: the glow hits with the music and
            # then breathes out instead of snapping off
            up = 1.0 - np.exp(-dt / ATTACK)
            down = 1.0 - np.exp(-dt / RELEASE)

            def follow(env, raw):
                return env + (raw - env) * (up if raw > env else down)

            bass = follow(bass, self.ear.bass)
            mid = follow(mid, self.ear.mid)
            treble = follow(treble, self.ear.treble)
            left_share += (self.ear.left_share - left_share) * down
            kicked = self.ear.beats != kicks_seen
            kicks_seen = self.ear.beats
            flashed = self.eye.flashes != flashes_seen and COLOUR_MODE == "screen"
            flashes_seen = self.eye.flashes
            glare = SCREEN_FLASH if flashed else max(0.0, glare - dt * BEAT_DECAY)

            # every animation that is on plays at once, light added together;
            # the settings window can pin one to look at it alone
            if pinned in names:
                layers = [names.index(pinned)]
            else:
                layers = [i for i, m in enumerate(MODES) if m.name in MODES_ON] or [0]
            # live shows read the screen colours every frame; the others get
            # their tables repainted a few times a second as the picture moves
            stale = [i for i in layers if MODES[i].name not in LIVE and self.tinted[i] != self.eye.version]
            if COLOUR_MODE == "screen" and stale and now - retinted > 0.25:
                self.build_tables(stale[0])
                retinted = now
                painted = None
            sound = Sound(bass, mid, treble, left_share, kicked, music)
            frames = [self.layer(i, now, dt, sound, glare, flashed) for i in layers]
            if len(frames) == 1:
                words, offset, pulse = frames[0]
            else:
                words, offset, pulse = blend(frames, self.depth), 0, 1.0
            # redraw the bitmaps only when the picture actually changed
            key = (offset, id(words))
            repaint = len(frames) > 1 or MODES[layers[0]].name in LIVE or key != painted
            for edge in self.edges:
                if repaint:
                    edge.paint(words, offset)
                # left monitors follow the left channel, right ones the right
                share = left_share + (1.0 - 2.0 * left_share) * edge.pan
                balance = 1.0 + STEREO * (2.0 * share - 1.0) if music else 1.0
                edge.push(opacity * pulse * balance)
            painted = key

            if not shown:
                for edge in self.edges:
                    edge.show(True)
                shown = True

            time.sleep(max(0.0, 1.0 / FPS - (time.perf_counter() - now)))


# -------------------------------------------------------------------- control
def is_running():
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return True
    probe.close()
    return False


def app_argv(*args):
    """Command line that runs GLOW with `args`; no args opens the settings."""
    if FROZEN:
        return [sys.executable, *args]
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    script = "glow.py" if args else "glow_ui.py"
    return [exe, os.path.join(DATA_DIR, script), *args]


def spawn(argv):
    subprocess.Popen(
        argv,
        # a onefile exe must unpack its own copy, not share this process's one
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
        close_fds=True,
    )


def spawn_daemon():
    if not is_running():
        spawn(app_argv("daemon"))


def daemon():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return  # another daemon already owns the overlay
    lock.listen(1)
    load_config()
    glow = Glow()
    result = glow.run()
    glow.tray.remove()  # or a dead icon lingers until the mouse passes over it
    if result == "restart":
        lock.close()     # free the guard first, or the new daemon would bow out
        spawn_daemon()   # a fresh process builds windows for the new geometry
    # Leave without interpreter teardown: soundcard releases COM from a
    # destructor while the capture thread may still be inside a COM call, and
    # that can hang the exit forever, leaving no glow and no live daemon.
    os._exit(0)


def clear_sentinel():
    try:
        os.remove(SENTINEL)
    except FileNotFoundError:
        pass


def icon_pixels(size, ss=4):
    """(size, size, 4) RGBA uint8: a glowing ring on a dark rounded square."""
    n = size * ss
    c = (np.arange(n) + 0.5) / n * 2.0 - 1.0
    x, y = np.meshgrid(c, -c)
    q = np.maximum(np.abs(np.stack([x, y])) - 0.94 + 0.36, 0.0)
    inside = np.hypot(q[0], q[1]) <= 0.36
    d = np.abs(np.hypot(x, y) - 0.55)
    core = np.clip(1.0 - d / (0.07 + 1.2 / size), 0.0, 1.0) ** 1.5
    light = core + 0.75 * np.exp(-(d / 0.2) ** 2)
    stops = np.array([hex_to_bgr(h)[::-1] for h in ("#00e5ff", "#7c4dff", "#ff4081", "#00e5ff")])
    pos = (np.arctan2(y, x) / (2.0 * np.pi) + 0.25) % 1.0 * 3.0
    i = np.minimum(pos.astype(np.intp), 2)
    col = stops[i] + (stops[i + 1] - stops[i]) * (pos - i)[..., None]
    rgb = np.array([0.05, 0.06, 0.10]) + col * light[..., None] + 0.5 * core[..., None] ** 4
    rgba = np.concatenate([np.clip(rgb, 0.0, 1.0) * inside[..., None], inside[..., None]], axis=-1)
    rgba = rgba.reshape(size, ss, size, ss, 4).mean(axis=(1, 3))
    rgba[..., :3] /= np.maximum(rgba[..., 3:], 1e-6)  # ICO alpha is straight, not premultiplied
    return np.round(rgba * 255).astype(np.uint8)


def write_icon(path, sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)):
    """A multi-size .ico of 32-bit DIBs (read by Windows, Tk and PyInstaller)."""
    images = []
    for s in sizes:
        bgra = icon_pixels(s)[::-1, :, [2, 1, 0, 3]]  # DIBs are bottom-up BGRA
        header = np.array([40, s, 2 * s, 1 | 32 << 16, 0, 0, 0, 0, 0, 0], np.uint32).tobytes()
        mask = bytes((s + 31) // 32 * 4 * s)        # all zero: alpha decides
        images.append(header + bgra.tobytes() + mask)
    out = np.array([0, 1, len(sizes)], np.uint16).tobytes()
    offset = 6 + 16 * len(sizes)
    for s, data in zip(sizes, images):
        out += bytes([s % 256, s % 256, 0, 0]) + np.array([1, 32], np.uint16).tobytes()
        out += np.array([len(data), offset], np.uint32).tobytes()
        offset += len(data)
    with open(path, "wb") as f:
        f.write(out + b"".join(images))


def icon_file():
    """Path to the app icon, drawing it first if it is missing."""
    if not os.path.exists(ICON):
        try:
            write_icon(ICON)
        except OSError:
            pass  # read-only folder: callers fall back to a stock icon
    return ICON


def selfcheck():
    """Assert the four seams are continuous in both depth and hue."""
    sw, sh, t = 1920, 1080, THICKNESS
    maps, perimeter = build_maps(sw, sh, t)
    (_, top_d, top_p), (_, bot_d, bot_p), (_, right_d, right_p), (_, left_d, left_p) = maps

    def hue(p):
        return ring_index(np.asarray(p, dtype=np.float32) / perimeter)

    def near(a, b, label, tol=2):
        a = np.asarray(a, np.int64)
        b = np.asarray(b, np.int64)
        gap = np.abs(a - b)
        # the pattern table is cyclic, so 0 and RING_STEPS-1 are neighbours
        gap = np.minimum(gap, RING_STEPS - gap) if np.max(gap) > RING_STEPS // 2 else gap
        assert int(np.max(gap)) <= tol, f"{label}: discontinuidad de {int(np.max(gap))}"

    # top window's last row meets the side windows' first row
    near(top_d[t - 1, :t], left_d[0, :], "depth costura izquierda")
    near(top_d[t - 1, -t:], right_d[0, :], "depth costura derecha")
    near(bot_d[0, :t], left_d[-1, :], "depth costura inferior izquierda")
    near(bot_d[0, -t:], right_d[-1, :], "depth costura inferior derecha")

    near(hue(top_p[0, 0]), hue(left_p[0, 0]), "hue esquina superior izquierda")
    near(hue(top_p[0, -1]), hue(right_p[0, 0]), "hue esquina superior derecha")
    near(hue(bot_p[0, 0]), hue(left_p[-1, 0]), "hue esquina inferior izquierda")
    near(hue(bot_p[0, -1]), hue(right_p[-1, 0]), "hue esquina inferior derecha")

    # a corner pixel must fade the same way whichever edge you approach it from
    assert top_d[0, 0] == 0 and top_d[t - 1, t - 1] == t - 1, "esquina mal graduada"

    # two chained screens must hand the pattern over without repeating a stretch
    fake = [(0, 0, sw, sh), (sw, 0, sw, sh)]
    spans = []
    walked = 0.0
    grand = 2.0 * perimeter
    for _ in fake:
        spans.append((walked / grand, (walked + perimeter) / grand))
        walked += perimeter
    assert abs(spans[0][1] - spans[1][0]) < 1e-6, "los monitores no se encadenan"
    assert abs(spans[-1][1] - 1.0) < 1e-6, "el bucle global no cierra"

    # every mode, with every colour source, must close on itself and keep
    # valid premultiplied alpha
    for source in LIMITS["COLOUR_MODE"]:
        apply_config({"COLOUR_MODE": source})
        for mode in MODES:
            lut = colour_table(t, mode)
            half = len(lut) // 2
            first, last_ = lut[:t].astype(np.int16), lut[half - t:half].astype(np.int16)
            assert np.max(np.abs(first - last_)) <= 26, f"{mode.name}/{source}: salto al cerrar"
            assert np.all(lut[:, :3] <= lut[:, 3:4]), f"{mode.name}/{source}: alfa invalido"
    # several animations at once: light adds up, clipped, still premultiplied
    words = [as_words(colour_table(t, m)) for m in MODES[:3]]
    mixed = blend([(wd, 7 * t, 1.0) for wd in words], t).view(np.uint8).reshape(-1, 4)
    one = words[0][7 * t:7 * t + RING_STEPS * t].view(np.uint8).reshape(-1, 4)
    assert len(mixed) == RING_STEPS * t and np.all(mixed >= one), "la mezcla pierde luz"
    assert np.all(mixed[:, :3] <= mixed[:, 3:4]), "la mezcla rompe el alfa"
    # the spectrum must be smooth all the way round, not only where it closes
    apply_config({"COLOUR_MODE": "spectrum"})
    colour, _ = ring(MODES[0])
    assert np.abs(np.diff(colour, axis=0)).max() < 0.05, "salto de color en el espectro"
    apply_config({"COLOUR_MODE": "single", "PALETTE": ["#ff0000"]})
    colour, _ = ring(MODES[0], 8)
    assert np.allclose(colour, [0.0, 0.0, 1.0]), "un solo color no es rojo puro (BGR)"

    # settings from the window are untrusted: clamp, drop junk, never empty
    apply_config({"PEAK_ALPHA": 9, "FPS": "60", "MUSIC": "yes", "MONITORS": "x",
                  "MODES_ON": [], "PALETTE": ["red", "#00FF00"], "EVIL": 1})
    assert PEAK_ALPHA == 1.0 and FPS == 60 and MUSIC is True and MONITORS == "all"
    assert MODES_ON == DEFAULTS["MODES_ON"] and PALETTE == ["#00ff00"]
    apply_config({"THICKNESS": 130, "SURGE_THICKNESS": 45})
    assert THICKNESS == 45, "el reposo no puede ser mas grueso que el golpe"
    apply_config(DEFAULTS)
    assert current_config() == DEFAULTS, "no vuelve a los valores por defecto"

    # screen colours: a blue stage with a magenta laser, over a dark crowd
    global SCREEN_COLOURS
    shot = np.zeros((72, 128, 4), np.uint8)
    shot[:30] = (255, 60, 0, 255)          # BGRA blue LED wall
    shot[30:40] = (230, 0, 255, 255)       # magenta lasers
    shot[40:] = (20, 25, 30, 255)          # crowd
    found = screen_colours(shot)
    assert found.shape == (SCREEN_SLOTS, 3), "no reparte todos los colores"
    blue = int(np.sum(found[:, 2] < 0.2))  # no red in it: blue, not magenta
    assert blue == 3, f"el azul cubre 3/4 de lo vivo, tiene {blue} huecos"
    assert screen_colours(np.full((72, 128, 4), 128, np.uint8)) is None, "una imagen gris no debe dar colores"
    eye = Eye(capture=False)
    for i in range(5):
        eye.look(shot, i * 0.1)
    assert eye.flashes == 0, "destello sin estrobo"
    eye.look(np.full((72, 128, 4), 255, np.uint8), 0.6)
    assert eye.flashes == 1, "no detecta el estrobo"
    apply_config({"COLOUR_MODE": "screen"})
    assert np.allclose(tone_colours(MODES[1], np.array([0.0])), SCREEN_COLOURS[0]), "no usa los colores de la pantalla"
    SCREEN_COLOURS = None
    apply_config(DEFAULTS)

    # the beat detector must fire on a real jump and stay quiet otherwise
    assert is_beat(0.02, 0.005, 0.10), "no detecta un golpe claro"
    assert not is_beat(0.006, 0.005, 0.10), "dispara con ruido de fondo"
    assert not is_beat(0.02, 0.005, 0.0), "dispara en silencio"

    # surge tables must stay valid premultiplied alpha (no channel above alpha)
    for words in surge_tables(t, MODES[0])[::SURGE_LEVELS - 1]:
        px = words.view(np.uint8).reshape(-1, 4)
        assert np.all(px[:, :3] <= px[:, 3:4]), "surge rompe el alfa premultiplicado"

    # synthetic song fed in 10 ms chunks: a 55 Hz kick every 0.5 s over a
    # quiet 440 Hz pad, left channel only
    rate, hop = Listener.RATE, 480
    t_s = np.arange(4 * rate) / rate
    since = t_s % 0.5
    kick = 0.8 * np.sin(2 * np.pi * 55 * since) * np.exp(-since / 0.08)
    song = (kick + 0.1 * np.sin(2 * np.pi * 440 * t_s)).astype(np.float32)
    ear = Listener(capture=False)
    loudest_bass = 0.0
    for i in range(0, len(song), hop):
        chunk = np.stack([song[i:i + hop], np.zeros(hop, np.float32)], axis=1)
        ear.feed(chunk, i / rate)
        loudest_bass = max(loudest_bass, ear.bass)
    assert 7 <= ear.beats <= 9, f"cuenta {ear.beats} golpes, esperaba 8"
    assert loudest_bass > 0.9, "el bajo no llena el rango"
    assert ear.left_share > 0.9, "no detecta audio solo en el canal izquierdo"

    for i in range(0, rate, hop):  # one second of silence
        ear.feed(np.zeros((hop, 2), np.float32), 4 + i / rate)
    assert ear.beats <= 9 and ear.bass < 0.05, "reacciona al silencio"

    # the live stage on three monitors side by side: the middle third of the
    # loop is the centre screen, a kick starts there and a long gap is a drop
    x, y, screen = ring_coords([(0, 0, sw, sh), (sw, 0, sw, sh), (2 * sw, 0, sw, sh)], t)
    assert 0 <= x.min() and x.max() <= 1 and 0 <= y.min() and y.max() <= 1, "fuera del escritorio"
    middle = slice(RING_STEPS // 3 + 1, 2 * RING_STEPS // 3)
    assert np.all(np.abs(x[middle] - 0.5) <= 1 / 6 + 1e-3), "el monitor central no esta en medio"
    names = [m.name for m in MODES]

    def snd(bass=0.0, mid=0.0, treble=0.0, left=0.5, kick=False):
        return Sound(bass, mid, treble, left, kick, True)

    stage, live = Stage(x, y, screen), MODES[names.index("stage")]
    stage.frame(live, t, 10.0, 0.01, snd(0.9, kick=True))
    assert stage.flash > 0.9, "el primer kick tras un silencio no es un drop"
    stage.flash = 0.0
    stage.frame(live, t, 11.0, 0.01, snd(kick=True))
    assert stage.flash == 0.0, "un kick normal no debe ser drop"
    words = stage.frame(live, t, 11.1, 0.01, snd())
    outer = np.abs(x - 0.5) > 0.4
    assert stage.intensity[middle].max() > 3 * stage.intensity[outer].max(), "la onda no nace en el centro"
    px = words.view(np.uint8).reshape(-1, 4)
    assert np.all(px[:, :3] <= px[:, 3:4]), "stage rompe el alfa premultiplicado"

    # the arena: each monitor a speaker. First kick is the drop (all speakers),
    # in the groove the bar walks downbeat-all, left, right, centre
    assert list(np.unique(screen)) == [0, 1, 2] and np.all(screen[middle] == 1), "no sabe en que monitor esta"
    arena = Arena(x, y, screen)
    show = MODES[names.index("arena")]
    arena.frame(show, t, 10.0, 0.01, snd(0.9, 0.5, kick=True))
    assert arena.flash > 0.9 and arena.drop > 0.9 and np.all(arena.pump > 0.9), "el drop no enciende todo"
    arena.drop = arena.flash = 0.0
    for beat, lit in ((1, 0), (2, 2), (3, 1)):  # left, right, centre
        arena.frame(show, t, 10.0 + 0.5 * beat, 0.5, snd(mid=0.5, kick=True))
        assert arena.pump.argmax() == lit and np.sum(arena.pump > 0.5) == 1, f"beat {beat + 1} no va al parlante {lit}"
    # bass only in the left channel lights the left monitor, not the right one
    apply_config({"STEREO": 1.0})
    arena.pump[:] = 0.0
    arena.frame(show, t, 12.5, 0.001, snd(0.9, left=1.0))
    assert arena.intensity[screen == 0].mean() > 3 * arena.intensity[screen == 2].mean(), "el estereo no separa parlantes"
    apply_config(DEFAULTS)
    # a long gap is a build: the strobe chases across the monitors
    seen = set()
    for i in range(400):
        arena.frame(show, t, 18.0 + i * 0.01, 0.01, snd(0.0, 0.5, 0.2))
        seen.add(int(np.argmax([arena.intensity[screen == m].mean() for m in range(3)])))
    assert seen == {0, 1, 2}, "el build no recorre los tres monitores"
    words = arena.frame(show, t, 22.0, 0.01, snd(0.0, 0.5, 0.2, kick=True))
    assert arena.drop > 0.9, "tras el build no hay drop"
    px = words.view(np.uint8).reshape(-1, 4)
    assert np.all(px[:, :3] <= px[:, 3:4]), "arena rompe el alfa premultiplicado"

    # every live show on the edges: valid alpha, music and demo, and its own trick
    assert set(SHOWS) == set(LIVE), "un modo en vivo sin show"
    shows = {name: cls(x, y, screen) for name, cls in SHOWS.items()}
    heads, splashed, struck = [], False, False
    for i in range(240):
        sound = Sound(0.9 * (i % 25 < 5), 0.5, 0.5, 0.5, i % 25 == 0, i < 200)  # then the demo
        for name, show in shows.items():
            words = show.frame(MODES[names.index(name)], t, 50.0 + i / 60, 1 / 60, sound)
            px = words.view(np.uint8).reshape(-1, 4)
            assert np.all(px[:, :3] <= px[:, 3:4]), f"{name} rompe el alfa premultiplicado"
            assert np.isfinite(show.intensity).all() and show.intensity.max() > 0.1, f"{name} se apaga"
        heads.append(x[shows["scanner"].intensity.argmax()])
        splashed |= bool(shows["rain"].splashes)
        struck |= bool(shows["storm"].bolts)
    top, bottom = y <= 0.0, y >= 1.0

    def lit(name, where):
        return shows[name].intensity[where].mean()

    assert np.ptp(heads) > 0.6, "el escaner no recorre los monitores"
    assert splashed and struck, "sin gotas que caen o sin rayos"
    assert lit("fire", bottom) > 2 * lit("fire", top), "el fuego no sube desde abajo"
    assert lit("aurora", top) > 2 * lit("aurora", bottom), "la aurora no cuelga de arriba"
    eq = shows["eq"]
    eq.height[:] = 0.0
    eq.frame(MODES[names.index("eq")], t, 60.0, 0.01, snd(1.0))
    assert eq.height[eq.BARS // 2] > 0.4 > eq.height[0], "los graves no van al centro"
    siren = shows["siren"]
    siren.clock = siren.burst = 0.0
    siren.frame(MODES[names.index("siren")], t, 60.0, 0.001, snd())
    half = siren.half > 0.5
    assert siren.intensity[~half].mean() > 3 * siren.intensity[half].mean(), "la sirena no alterna"

    print(f"selfcheck OK: costuras continuas, {len(MODES)} modos cierran el bucle, "
          f"{ear.beats} golpes detectados")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if cmd == "check":
        selfcheck()
        return
    if cmd == "icon":
        write_icon(ICON)
        return
    if cmd == "on":
        open(SENTINEL, "w").close()
        spawn_daemon()
    elif cmd == "off":
        clear_sentinel()
    elif cmd == "quit":
        clear_sentinel()
        while True:
            hwnd = user32.FindWindowW(CLASS_NAME, None)
            if not hwnd:
                break
            user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
            time.sleep(0.3)
            if user32.FindWindowW(CLASS_NAME, None) == hwnd:
                break
    else:
        daemon()


if __name__ == "__main__":
    main()

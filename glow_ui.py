"""Settings window for glow.py: a native Tk window, no browser.

Every change is validated by glow.apply_config, previewed at once with the
overlay's own tables and shows, saved to glow.json and picked up live by the
running overlay daemon.

Usage:
    pythonw glow_ui.py
"""

import colorsys
import ctypes
import json
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import colorchooser, messagebox, ttk

import numpy as np

import glow

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "OpenClawGlow"
UI_PORT = glow.LOCK_PORT + 1  # fixed, so a second launch finds this window
PROFILES = os.path.join(glow.DATA_DIR, "glow_profiles.json")
# what a style or profile covers: everything except when the glow switches on
# and which screen it watches
STYLE_KEYS = [k for k in glow.LIMITS if k not in ("ENABLED", "AGENT", "MUSIC", "SCREEN_SOURCE")]
MAX_PROFILES = 30
SAVE_DELAY = 150   # ms of quiet before a burst of changes is written to disk

# ------------------------------------------------------------------- theme
BG, CARD, RAISED, LINE = "#0b0d13", "#131722", "#1b2030", "#262d3d"
TEXT, DIM, ACCENT, ON_ACCENT = "#e8ecf4", "#8b93a7", "#00e5ff", "#001018"
DESK, BEZEL, SCREEN = (7, 8, 12), (38, 44, 58), (17, 20, 28)
F_SMALL = ("Segoe UI", 9)
F_TITLE = ("Segoe UI Semibold", 10)
F_H2 = ("Segoe UI Semibold", 12)
F_H1 = ("Segoe UI Semibold", 20)
SCALE = 1.0  # screen DPI / 96, set once the window exists


def px(n):
    return int(round(n * SCALE))


# ----------------------------------------------------------------- content
FMT = {
    "pct": lambda v: f"{round(v * 100)} %",
    "px": lambda v: f"{round(v)} px",
    "x": lambda v: f"×{v:.2f}",
    "s": lambda v: f"{v:.1f} s" if v < 10 else f"{round(v)} s",
    "ms": lambda v: f"{round(v * 1000)} ms",
    "fps": lambda v: f"{round(v)} fps",
    "num": lambda v: f"{v:.2f}",
    "rate": lambda v: f"{v:.1f} /s",
    "vol": lambda v: f"{v:.3f}",
    "loop": lambda v: f"{v * 100:.1f} % del borde",
}
FIELDS = {
    "motion": [
        ("SPEED", "Velocidad", "Qué tan rápido recorre el borde", "x"),
    ],
    "spectrum": [("SATURATION", "Saturación", "De tonos pastel a colores intensos", "pct")],
    "screen": [("SCREEN_FLASH", "Destellos del video",
                "Estrobos y pirotecnia en pantalla encienden el borde; 0 los ignora", "pct")],
    "look": [
        ("PEAK_ALPHA", "Intensidad", "Opacidad del brillo junto al borde", "pct"),
        ("THICKNESS", "Grosor", "Cuánto entra el brillo en la pantalla", "px"),
        ("FALLOFF", "Concentración", "Alto: pegado al borde · Bajo: más difuso", "num"),
        ("FADE_SECONDS", "Fundido", "Tiempo para aparecer y desaparecer", "s"),
        ("FPS", "Fluidez", "Cuadros por segundo; más fluidez usa más CPU", "fps"),
    ],
    "music": [
        ("SURGE_THICKNESS", "Profundidad del golpe", "Hasta dónde empujan los graves. Reinicia el efecto", "px"),
        ("WHITE_HOT", "Núcleo blanco", "Cuánto se blanquea el borde con los graves", "pct"),
        ("BEAT_FLOOR", "Brillo entre golpes", "Qué tan tenue queda entre beat y beat", "pct"),
        ("KICK_JUMP", "Latigazo", "Salto del patrón en cada beat", "loop"),
        ("BEAT_SPEED", "Aceleración en el beat", "Impulso extra de velocidad en cada golpe", "x"),
        ("TREBLE_SPEED", "Brillo de agudos", "Platillos y hi-hats aceleran el patrón", "x"),
        ("STEREO", "Estéreo entre monitores", "Pantallas izquierda y derecha siguen su canal", "pct"),
    ],
    "advanced": [
        ("BEAT_SENSITIVITY", "Exigencia de beat", "Alto: solo cuentan los golpes fuertes", "x"),
        ("BEAT_DECAY", "Caída del golpe", "Qué tan rápido se apaga cada pulso", "rate"),
        ("ATTACK", "Ataque", "Tiempo para subir con un golpe", "ms"),
        ("RELEASE", "Liberación", "Tiempo para volver a bajar", "ms"),
        ("MUSIC_RMS_ON", "Umbral de sonido", "Volumen mínimo que cuenta como música", "vol"),
        ("MUSIC_HOLD", "Espera tras silencio", "Cuánto sigue encendido cuando la música para", "s"),
    ],
}
MODE_TEXT = {
    "rainbow": ("Arcoíris", "El espectro completo recorre el borde"),
    "comet": ("Cometa", "Una cabeza brillante persigue su estela"),
    "twin": ("Gemelos", "Dos destellos opuestos barren a la vez"),
    "breathe": ("Respiración", "Tres ondas suaves que respiran"),
    "drop": ("Drop", "Láseres de festival con cortes glitch"),
    "stage": ("Escenario", "En vivo: cada kick estalla desde el monitor central, los graves "
                           "suben desde el piso y el drop enciende todo"),
    "arena": ("Arena", "En vivo: cada monitor es un parlante. Ondas de choque, láseres, "
                       "build-up que recorre los monitores y drop que enciende todo"),
    "scanner": ("Escáner", "Una cabeza que barre de monitor a monitor, como KITT"),
    "eq": ("Ecualizador", "Barras en el borde inferior: graves al centro, agudos a los lados"),
    "rain": ("Lluvia", "Gotas que caen por los costados y salpican abajo"),
    "fire": ("Fuego", "Llamas que suben desde el borde inferior"),
    "storm": ("Tormenta", "Relámpagos que cruzan los bordes en cada golpe"),
    "siren": ("Sirena", "Luces de patrulla que alternan entre las mitades"),
    "aurora": ("Aurora", "Cortinas tranquilas desde el borde superior; ideal para trabajar"),
}
COLOUR_NAMES = {"spectrum": "Espectro", "single": "Un color", "palette": "Combinación", "screen": "Pantalla"}
PALETTES = {
    "Neón": ["#ff2bd6", "#00f0ff", "#7a2bff"],
    "Aurora": ["#00ffa3", "#00b3ff", "#8a5cff"],
    "Atardecer": ["#ff5f6d", "#ffc371", "#ff2e97"],
    "Océano": ["#00c6ff", "#0072ff", "#00ffd5"],
    "Fuego": ["#ff2a00", "#ff8a00", "#ffd000"],
    "Hielo": ["#e0f7ff", "#7dd3fc", "#a78bfa"],
    "Rave": ["#00f0ff", "#b000ff", "#ff0080"],
    "Main Stage": ["#00e5ff", "#2962ff", "#d500f9", "#ff1744", "#ffd600"],
    "Arena": ["#00e5ff", "#ff00aa", "#7c00ff", "#ffb300"],
    "Policía": ["#ff1744", "#2962ff"],
    "Tormenta": ["#1a237e", "#7c4dff", "#e0f7ff"],
    "Boreal": ["#00ff9d", "#00e5ff", "#b388ff"],
}
QUICK = ["#ffffff", "#00e5ff", "#2979ff", "#7c4dff", "#ff4081",
         "#ff1744", "#ff9100", "#ffea00", "#00e676", "#1de9b6"]


def pal(name):
    return {"COLOUR_MODE": "palette", "PALETTE": PALETTES[name]}


PRESETS = [
    ("Siri", "El original", {}),
    ("Neón", "Vivo y nítido", {
        **pal("Neón"), "MODES_ON": ["comet", "twin"], "SPEED": 1.4,
        "PEAK_ALPHA": 1.0, "THICKNESS": 70, "FALLOFF": 3.2}),
    ("Aurora", "Lento y envolvente", {
        **pal("Aurora"), "MODES_ON": ["rainbow", "breathe"], "SPEED": 0.6,
        "PEAK_ALPHA": 0.8, "THICKNESS": 130, "FALLOFF": 1.8, "SURGE_THICKNESS": 180,
        "KICK_JUMP": 0.01, "BEAT_SPEED": 1.0}),
    ("Fuego", "Cálido e intenso", {
        **pal("Fuego"), "MODES_ON": ["comet", "twin"], "SPEED": 1.2,
        "PEAK_ALPHA": 0.9, "THICKNESS": 100, "FALLOFF": 2.2, "WHITE_HOT": 0.7}),
    ("Minimal", "Blanco y discreto", {
        "COLOUR_MODE": "single", "PALETTE": ["#ffffff"], "MODES_ON": ["breathe"], "SPEED": 0.8,
        "PEAK_ALPHA": 0.45, "THICKNESS": 50, "FALLOFF": 3.0, "WHITE_HOT": 0.2, "KICK_JUMP": 0.0,
        "BEAT_SPEED": 0.8, "TREBLE_SPEED": 0.5}),
    ("Fiesta", "Máxima reacción a la música", {
        "SATURATION": 1.0, "SPEED": 1.6, "PEAK_ALPHA": 1.0,
        "THICKNESS": 80, "FALLOFF": 2.2, "SURGE_THICKNESS": 200, "WHITE_HOT": 0.8,
        "BEAT_FLOOR": 0.15, "KICK_JUMP": 0.08, "BEAT_SPEED": 4.0, "TREBLE_SPEED": 2.5, "STEREO": 0.6}),
    ("Drop", "Láseres de festival que saltan en cada kick", {
        **pal("Rave"), "MODES_ON": ["drop"], "SPEED": 1.3, "PEAK_ALPHA": 1.0, "THICKNESS": 60,
        "FALLOFF": 3.5, "SURGE_THICKNESS": 220, "WHITE_HOT": 0.9, "BEAT_FLOOR": 0.08,
        "KICK_JUMP": 1 / 12, "BEAT_SPEED": 3.0, "TREBLE_SPEED": 2.5, "STEREO": 0.7, "BEAT_DECAY": 7.0}),
    ("Festival en vivo", "Colores del show que ves, efectos al ritmo del DJ", {
        "COLOUR_MODE": "screen", "MODES_ON": ["stage", "drop"], "SPEED": 1.3,
        "PEAK_ALPHA": 1.0, "THICKNESS": 70, "FALLOFF": 2.8, "SURGE_THICKNESS": 220,
        "WHITE_HOT": 0.75, "BEAT_FLOOR": 0.1, "KICK_JUMP": 1 / 12, "BEAT_SPEED": 3.0,
        "TREBLE_SPEED": 2.5, "STEREO": 0.5, "BEAT_DECAY": 6.0, "SCREEN_FLASH": 1.0}),
    ("Main Stage", "Tus monitores como escenario de festival", {
        **pal("Main Stage"), "MODES_ON": ["stage"], "PEAK_ALPHA": 1.0, "THICKNESS": 70,
        "FALLOFF": 2.8, "SURGE_THICKNESS": 200, "WHITE_HOT": 0.6, "STEREO": 0.5}),
    ("Arena", "Cada monitor es un parlante: build-up, drop y láseres en estéreo", {
        **pal("Arena"), "MODES_ON": ["arena"], "SATURATION": 1.0, "PEAK_ALPHA": 1.0,
        "THICKNESS": 60, "FALLOFF": 2.4, "SURGE_THICKNESS": 240, "WHITE_HOT": 0.85,
        "STEREO": 1.0, "ATTACK": 0.008, "RELEASE": 0.12}),
    ("KITT", "Un escáner rojo que barre tus monitores", {
        "MODES_ON": ["scanner"], "SATURATION": 1.0, "PEAK_ALPHA": 1.0, "THICKNESS": 60, "FALLOFF": 3.0}),
    ("Ecualizador", "Barras al ritmo en el borde inferior", {
        "MODES_ON": ["eq"], "SATURATION": 1.0, "PEAK_ALPHA": 1.0, "THICKNESS": 80, "FALLOFF": 2.4,
        "SURGE_THICKNESS": 200, "STEREO": 0.6}),
    ("Lluvia", "Gotas por los costados que salpican al caer", {
        **pal("Océano"), "MODES_ON": ["rain"], "PEAK_ALPHA": 0.9, "THICKNESS": 60, "FALLOFF": 2.8}),
    ("Fogata", "Llamas que suben desde abajo", {
        **pal("Fuego"), "MODES_ON": ["fire"], "PEAK_ALPHA": 1.0, "THICKNESS": 90, "FALLOFF": 2.0,
        "WHITE_HOT": 0.5}),
    ("Tormenta", "Relámpagos en cada golpe", {
        **pal("Tormenta"), "MODES_ON": ["storm"], "PEAK_ALPHA": 1.0, "THICKNESS": 70, "FALLOFF": 2.6,
        "WHITE_HOT": 0.9, "SURGE_THICKNESS": 220}),
    ("Patrulla", "Luces de policía que alternan", {
        **pal("Policía"), "MODES_ON": ["siren"], "PEAK_ALPHA": 1.0, "THICKNESS": 60, "FALLOFF": 3.0}),
    ("Boreal", "Cortinas suaves desde arriba, ideal para trabajar", {
        **pal("Boreal"), "MODES_ON": ["aurora"], "SPEED": 0.8, "PEAK_ALPHA": 0.7, "THICKNESS": 120,
        "FALLOFF": 1.8}),
]


def look(values):
    """A full style from a preset or profile: the defaults, overlaid."""
    out = {k: glow.DEFAULTS[k] for k in STYLE_KEYS}
    out.update((k, v) for k, v in values.items() if k in out)
    out["MODES_ON"] = [m.name for m in glow.MODES if m.name in out["MODES_ON"]]
    out["PALETTE"] = [c.lower() for c in out["PALETTE"]]
    return out


def describe(values):
    v = look(values)
    n = len(v["MODES_ON"])
    return f"{COLOUR_NAMES[v['COLOUR_MODE']]} · {n} animación{'' if n == 1 else 'es'}"


RAINBOW = ["#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h / 6, 0.9, 1)) for h in range(7)]


def swatch(values):
    v = look(values)
    return {"spectrum": RAINBOW, "screen": ["#0b0d13", "#00e5ff", "#d500f9", "#ffd600"],
            "single": [v["PALETTE"][0], "#333333"]}.get(v["COLOUR_MODE"], v["PALETTE"])


def hex_rgb(code):
    return [int(code[i:i + 2], 16) for i in (1, 3, 5)]


def stops(colours, n=48):
    """(n, 3) RGB blended evenly through the colours."""
    rgb = np.array([hex_rgb(c) for c in colours], float)
    at = np.linspace(0.0, 1.0, len(rgb))
    t = np.linspace(0.0, 1.0, n)
    return np.stack([np.interp(t, at, rgb[:, k]) for k in range(3)], axis=1)


def ink(code):
    """Black or white, whichever reads on this colour."""
    r, g, b = hex_rgb(code)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


# ----------------------------------------------------------------- backend
def daemon_command():
    return subprocess.list2cmdline(glow.app_argv("daemon"))


def autostart(enable=None):
    """Read, or set, the HKCU Run entry that starts the overlay at sign-in."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        if enable is True:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, daemon_command())
        elif enable is False:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
        try:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == daemon_command()
        except FileNotFoundError:
            return False


def set_preview(on, mode=""):
    if on:
        with open(glow.PREVIEW, "w", encoding="utf-8") as f:
            f.write(mode)
        glow.spawn_daemon()
    else:
        try:
            os.remove(glow.PREVIEW)
        except FileNotFoundError:
            pass


def profiles():
    try:
        with open(PROFILES, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def edit_profiles(save=None, delete=None):
    """Save the current look under a name, or delete one. Returns all profiles."""
    data = profiles()
    if isinstance(save, str) and save.strip():
        name = save.strip()[:40]
        if name in data or len(data) < MAX_PROFILES:
            config = glow.current_config()
            data[name] = {k: config[k] for k in STYLE_KEYS}
    if isinstance(delete, str):
        data.pop(delete, None)
    tmp = PROFILES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PROFILES)
    return data


def identify(source):
    rect = glow.watched_rect(source)
    if rect:
        glow.identify(rect, f"Monitor {source}" if source else "Ventana activa")


def look_at_screen():
    """One snapshot of the watched screen, so the preview shows its colours."""
    rect = glow.watched_rect()
    found = glow.screen_colours(glow.grab(rect)) if rect else None
    if found is not None:
        glow.SCREEN_COLOURS = found
    return found is not None


def claim():
    """Own UI_PORT, or wake the window that already does (then None)."""
    for _ in range(10):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", UI_PORT))
            sock.listen(4)
            return sock
        except OSError:
            sock.close()
        glow.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it take the front
        try:
            socket.create_connection(("127.0.0.1", UI_PORT), timeout=3).close()
            return None
        except OSError:
            time.sleep(0.5)  # it is closing; take over once it is gone
    return None


def calm_table(depth, mode):
    return glow.as_words(glow.colour_table(depth, mode, reach=min(1.0, glow.THICKNESS / depth)))


# ------------------------------------------------------------------ preview
class Preview:
    """The monitors in miniature, lit with the overlay's own tables and shows.

    Every preview pixel looks up the same table entry as the real edge pixel
    it stands for, so what you see here is what the daemon draws.
    """

    def __init__(self, canvas, width, height):
        self.canvas, self.w, self.h = canvas, width, height
        self.image = tk.PhotoImage(width=width, height=height)
        canvas.create_image(0, 0, image=self.image, anchor="nw")
        self.tag = canvas.create_text(px(12), height - px(8), anchor="sw", fill=DIM, font=F_SMALL)
        self.veil = canvas.create_text(width // 2, height // 2, text="Efecto desactivado",
                                       fill=TEXT, font=F_H2, state="hidden")
        self.key = None
        self.cache = {}
        self.beat = self.clock = 0.0
        self.phase = {}   # each animation keeps its own place
        self.last = time.perf_counter()

    def invalidate(self):
        self.cache.clear()

    def layout(self):
        self.key = (glow.SURGE_THICKNESS, glow.MONITORS)
        screens = sorted(glow.monitors(), key=lambda r: (r[0], r[1]))  # chained like the daemon
        smallest = min(min(sw, sh) for _, _, sw, sh in screens)
        self.depth = depth = max(1, min(glow.SURGE_THICKNESS, smallest // 2))
        left, top = min(s[0] for s in screens), min(s[1] for s in screens)
        right, bottom = max(s[0] + s[2] for s in screens), max(s[1] + s[3] for s in screens)
        margin, gap = px(16), px(6)
        scale = min((self.w - 2 * margin) / (right - left), (self.h - 2 * margin) / (bottom - top))
        x0 = (self.w - (right - left) * scale) / 2
        y0 = (self.h - (bottom - top) * scale) / 2
        base = np.empty((self.h, self.w, 3), np.uint8)
        base[:] = DESK
        boxes = []
        for sx, sy, sw, sh in screens:
            X, Y = round(x0 + (sx - left) * scale) + gap // 2, round(y0 + (sy - top) * scale) + gap // 2
            W, H = max(12, round(sw * scale) - gap), max(12, round(sh * scale) - gap)
            base[Y - 1:Y + H + 1, X - 1:X + W + 1] = BEZEL
            base[Y:Y + H, X:X + W] = SCREEN
            # twice as deep as to scale, or the glow would be a hairline
            t = max(3, min(round(2 * depth * scale), (H - 1) // 2, (W - 1) // 2))
            boxes.append((X, Y, t, glow.build_maps(W, H, t)))
        # chain the screens into one loop, exactly like the daemon does
        total = sum(perimeter for *_, (_, perimeter) in boxes)
        index, dest = [], []
        travelled = 0.0
        for X, Y, t, (windows, perimeter) in boxes:
            for (rx, ry, rw, rh), dmap, arc in windows:
                d = np.minimum(depth - 1, dmap * depth // t)  # back to real pixel depth
                index.append((glow.ring_index((travelled + arc) / total) * depth + d).ravel())
                yy, xx = np.mgrid[Y + ry:Y + ry + rh, X + rx:X + rx + rw]
                dest.append((yy * self.w + xx).ravel())
            travelled += perimeter
        self.index, self.dest = np.concatenate(index), np.concatenate(dest)
        self.base = base.reshape(-1, 3)
        self.under = self.base[self.dest].astype(np.float32)
        x, y, screen = glow.ring_coords(screens, depth)
        self.shows = {name: show(x, y, screen) for name, show in glow.SHOWS.items()}
        self.cache.clear()

    def table(self, mode, build):
        key = (mode.name, build)
        if key not in self.cache:
            self.cache[key] = build(self.depth, mode)
        return self.cache[key]

    def layer(self, mode, now, dt, sound):
        """One animation's frame as (words, table offset, brightness), as in the daemon."""
        show = self.shows.get(mode.name)
        if show:
            return show.frame(mode, self.depth, now, dt, sound), 0, 1.0
        speed = mode.speed * glow.SPEED
        pulse = 1.0
        phase = self.phase.get(mode.name, 0.0)
        if sound.music:
            surge = max(sound.bass, self.beat)
            words = self.table(mode, glow.surge_tables)[round(surge * (glow.SURGE_LEVELS - 1))]
            pulse = glow.BEAT_FLOOR + (1.0 - glow.BEAT_FLOOR) * max(surge, sound.mid)
            if sound.kicked:
                phase += glow.KICK_JUMP * np.sign(speed)
            speed *= 1.0 + glow.BEAT_SPEED * self.beat + glow.TREBLE_SPEED * sound.treble
        else:
            words = self.table(mode, calm_table)
        phase = self.phase[mode.name] = (phase + speed * dt) % 1.0
        return words, int(phase * glow.RING_STEPS) % glow.RING_STEPS * self.depth, pulse

    def frame(self, music, focus):
        if self.key != (glow.SURGE_THICKNESS, glow.MONITORS):
            self.layout()
        now = time.perf_counter()
        dt = min(0.1, now - self.last)
        self.last = now
        kicked, bass, treble = False, 0.0, 0.0
        if music:  # a steady 120 BPM kick stands in for the song
            self.clock += dt
            kicked = self.clock >= 0.5
            self.clock %= 0.5
            self.beat = 1.0 if kicked else max(0.0, self.beat - dt * glow.BEAT_DECAY)
            bass = 0.95 * math.exp(-self.clock / 0.12)
            treble = 0.25 + 0.25 * abs(math.sin(9.7 * now) * math.sin(3.1 * now))
        sound = glow.Sound(bass, 0.5, treble, 0.5 + 0.3 * math.sin(0.8 * now), kicked, music)
        # every animation that is on, at once, like the daemon; a tapped card plays alone
        modes = [m for m in glow.MODES if m.name == focus or not focus and m.name in glow.MODES_ON]
        frames = [self.layer(m, now, dt, sound) for m in modes]
        words, offset, pulse = frames[0] if len(frames) == 1 else (glow.blend(frames, self.depth), 0, 1.0)
        k = min(1.0, pulse) * (1.0 if glow.ENABLED else 0.3)
        lit = words[self.index + offset].view(np.uint8).reshape(-1, 4).astype(np.float32) * (k / 255.0)
        out = self.base.copy()
        out[self.dest] = self.under * (1.0 - lit[:, 3:]) + lit[:, 2::-1] * 255.0  # premultiplied BGRA
        self.image.configure(data=b"P6 %d %d 255\n" % (self.w, self.h) + out.tobytes(), format="ppm")
        self.canvas.itemconfigure(self.tag, text=" + ".join(MODE_TEXT[m.name][0] for m in modes))
        self.canvas.itemconfigure(self.veil, state="hidden" if glow.ENABLED else "normal")


# ------------------------------------------------------------------ widgets
def on_click(widget, command, skip=()):
    """Click anywhere on a widget and its children, except the `skip` ones."""
    if widget not in skip:
        widget.bind("<Button-1>", lambda e: command())
        for child in widget.winfo_children():
            on_click(child, command, skip)


def label(parent, text, font=F_SMALL, fg=TEXT, wrap=0, **pack):
    widget = tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"],
                      wraplength=px(wrap), justify="left", anchor="w")
    widget.pack(fill="x", **pack)
    return widget


def button(parent, text, command, primary=False):
    bg, fg = (ACCENT, ON_ACCENT) if primary else (LINE, TEXT)
    return tk.Button(parent, text=text, command=command, font=F_TITLE, relief="flat", bd=0,
                     cursor="hand2", padx=px(14), pady=px(4), bg=bg, fg=fg,
                     activebackground=TEXT, activeforeground=BG, disabledforeground=DIM)


def card(parent, title, lead=None):
    frame = tk.Frame(parent, bg=CARD, padx=px(16), pady=px(14),
                     highlightthickness=1, highlightbackground=LINE)
    frame.pack(fill="x", pady=(0, px(12)))
    label(frame, title, F_H2)
    frame.lead = label(frame, lead, fg=DIM, wrap=520, pady=(px(2), px(6))) if lead is not None else None
    return frame


def row(parent, title, hint):
    """A line with a title and hint on the left; pack the control into it."""
    line = tk.Frame(parent, bg=parent["bg"])
    line.pack(fill="x", pady=px(5))
    text = tk.Frame(line, bg=parent["bg"])
    text.pack(side="left", fill="x", expand=True)
    label(text, title, F_TITLE)
    label(text, hint, fg=DIM, wrap=330)
    return line


class Switch(tk.Canvas):
    """A pill switch; clicking calls `command(new_state)`."""

    def __init__(self, parent, command):
        super().__init__(parent, width=px(40), height=px(22), bg=parent["bg"],
                         highlightthickness=0, cursor="hand2")
        self.on = False
        self.bind("<Button-1>", lambda e: command(not self.on))
        self.set(False)

    def set(self, on):
        self.on = bool(on)
        self.delete("all")
        w, h = px(40) - 1, px(22) - 1
        fill = ACCENT if self.on else LINE
        self.create_oval(0, 0, h, h, fill=fill, width=0)
        self.create_oval(w - h, 0, w, h, fill=fill, width=0)
        self.create_rectangle(h / 2, 0, w - h / 2, h + 1, fill=fill, width=0)
        x, r = (w - h / 2 if self.on else h / 2), h / 2 - px(3)
        self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r, fill=ON_ACCENT if self.on else TEXT, width=0)


class Seg(tk.Frame):
    """Segmented buttons; clicking one calls `command(value)`."""

    def __init__(self, parent, options, command):
        super().__init__(parent, bg=LINE, padx=2, pady=2)
        self.buttons = {}
        for value, text in options:
            b = tk.Label(self, text=text, font=F_SMALL, bg=LINE, fg=DIM,
                         padx=px(10), pady=px(4), cursor="hand2")
            b.pack(side="left", fill="x", expand=True)
            b.bind("<Button-1>", lambda e, v=value: command(v))
            self.buttons[value] = b

    def set(self, value):
        for v, b in self.buttons.items():
            b.configure(bg=ACCENT if v == value else LINE, fg=ON_ACCENT if v == value else DIM)


class Bar(tk.Canvas):
    """A strip of colours stretched to its width."""

    def __init__(self, parent, height):
        super().__init__(parent, height=height, width=px(60), bg=parent["bg"], highlightthickness=0)
        self.rgb = np.zeros((1, 3))
        self.bind("<Configure>", lambda e: self.draw())

    def show(self, rgb):
        self.rgb = np.clip(np.asarray(rgb, float), 0, 255).round().astype(int)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h, n = max(2, self.winfo_width()), self.winfo_height(), len(self.rgb)
        for i, c in enumerate(self.rgb):
            self.create_rectangle(i * w / n, 0, (i + 1) * w / n + 1, h, fill="#%02x%02x%02x" % tuple(c), width=0)


class Tile(tk.Frame):
    """A clickable card: colour strip, name, optional text and delete cross."""

    def __init__(self, parent, name, text, colours, command, delete=None):
        super().__init__(parent, bg=RAISED, padx=px(10), pady=px(8), cursor="hand2",
                         highlightthickness=2, highlightbackground=RAISED)
        Bar(self, px(6)).pack(fill="x")
        self.winfo_children()[0].show(stops(colours))
        head = tk.Frame(self, bg=RAISED)
        head.pack(fill="x", pady=(px(6), 0))
        tk.Label(head, text=name, font=F_TITLE, bg=RAISED, fg=TEXT).pack(side="left")
        skip = []
        if delete:
            cross = tk.Label(head, text="✕", font=F_SMALL, bg=RAISED, fg=DIM, cursor="hand2")
            cross.pack(side="right")
            cross.bind("<Button-1>", lambda e: delete())
            skip.append(cross)
        if text:
            label(self, text, fg=DIM, wrap=150)
        on_click(self, command, skip)

    def mark(self, on):
        self.configure(highlightbackground=ACCENT if on else RAISED)


class ModeCard(tk.Frame):
    """One animation: its colours, name, text, and a switch to turn it on."""

    def __init__(self, parent, app, mode):
        super().__init__(parent, bg=RAISED, padx=px(10), pady=px(8), cursor="hand2",
                         highlightthickness=2, highlightbackground=RAISED)
        self.mode = mode
        self.strip = Bar(self, px(8))
        self.strip.pack(fill="x")
        head = tk.Frame(self, bg=RAISED)
        head.pack(fill="x", pady=(px(6), 0))
        name, text = MODE_TEXT[mode.name]
        tk.Label(head, text=name, font=F_TITLE, bg=RAISED, fg=TEXT).pack(side="left")
        self.switch = Switch(head, lambda on: app.toggle_mode(mode.name, on))
        self.switch.pack(side="right")
        label(self, text, fg=DIM, wrap=230)
        on_click(self, lambda: app.focus_mode(mode.name), [self.switch])

    def paint(self):
        colour, intensity = glow.ring(self.mode, 48)
        lit = np.clip(intensity, 0.0, 1.0)[:, None]
        self.strip.show(colour[:, ::-1] * 255 * lit + np.array(hex_rgb(RAISED)) * (1 - lit))

    def mark(self, on):
        self.configure(highlightbackground=ACCENT if on else RAISED)


class Field:
    """A labelled slider for one numeric setting."""

    def __init__(self, app, parent, key, title, hint, kind):
        self.app, self.key, self.kind = app, key, kind
        self.lo, hi = glow.LIMITS[key]
        self.integer = isinstance(glow.DEFAULTS[key], int)
        self.step = 1 if self.integer else (hi - self.lo) / 200
        frame = tk.Frame(parent, bg=parent["bg"])
        frame.pack(fill="x", pady=px(5))
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text=title, font=F_TITLE, bg=frame["bg"], fg=TEXT).grid(row=0, column=0, sticky="w")
        self.value = tk.Label(frame, font=F_SMALL, bg=frame["bg"], fg=ACCENT)
        self.value.grid(row=0, column=1, sticky="e")
        tk.Label(frame, text=hint, font=F_SMALL, bg=frame["bg"], fg=DIM, wraplength=px(500),
                 justify="left").grid(row=1, column=0, columnspan=2, sticky="w")
        self.scale = ttk.Scale(frame, from_=self.lo, to=hi, command=self.moved)
        self.scale.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(4), 0))

    def moved(self, raw):
        if self.app.syncing:
            return
        v = self.lo + round((float(raw) - self.lo) / self.step) * self.step
        self.app.change({self.key: int(round(v)) if self.integer else round(v, 6)})

    def sync(self, value):
        self.scale.set(value)
        self.value.configure(text=FMT[self.kind](value))


# ---------------------------------------------------------------------- app
class App:
    def __init__(self, root, sock):
        global SCALE
        SCALE = root.winfo_fpixels("1i") / 96.0
        self.root = root
        self.syncing = False   # widgets being set from the config, not by you
        self.pending = None    # scheduled save
        self.focus = None      # animation pinned in the preview
        self.music = False     # preview simulates music instead of the agent
        self.running = glow.is_running()
        self.switches, self.segs, self.fields = {}, {}, []
        self.preset_tiles, self.profile_tiles, self.palette_tiles = [], [], []
        self.painted = None    # colours the strips and chips were drawn with
        self.screen_version = 0
        self.wake = threading.Event()
        threading.Thread(target=self.listen, args=(sock,), daemon=True).start()

        root.title("Glow")
        try:
            root.iconbitmap(default=glow.icon_file())
        except tk.TclError:
            pass  # no icon file: Tk's own feather stays
        root.configure(bg=BG)
        root.geometry(f"{px(1240)}x{min(px(860), root.winfo_screenheight() - px(150))}+{px(40)}+{px(20)}")
        root.minsize(px(1040), px(600))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Horizontal.TScale", background=ACCENT, troughcolor=LINE, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, gripcount=0, sliderlength=px(12))
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG, bordercolor=BG,
                        arrowcolor=DIM, lightcolor=LINE, darkcolor=LINE, gripcount=0)
        self.build_header()
        body = tk.Frame(root, bg=BG, padx=px(20))
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y")
        self.build_left(left)
        self.build_right(self.scroller(body))
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()
        self.ping()
        self.sample()

    # ------------------------------------------------------------ layout
    def build_header(self):
        bar = tk.Frame(self.root, bg=BG, padx=px(20), pady=px(14))
        bar.pack(fill="x")
        size = px(40)
        rgba = glow.icon_pixels(size).astype(np.float32) / 255.0
        rgb = rgba[..., :3] * rgba[..., 3:] + np.array(hex_rgb(BG)) / 255.0 * (1 - rgba[..., 3:])
        self.logo = tk.PhotoImage(data=b"P6 %d %d 255\n" % (size, size)
                                  + (rgb * 255).round().astype(np.uint8).tobytes(), format="ppm")
        tk.Label(bar, image=self.logo, bg=BG).pack(side="left", padx=(0, px(12)))
        brand = tk.Frame(bar, bg=BG)
        brand.pack(side="left")
        label(brand, "Glow", F_H1)
        label(brand, "Borde luminoso para tus pantallas", fg=DIM)
        self.switches["ENABLED"] = Switch(bar, lambda on: self.change({"ENABLED": on}))
        self.switches["ENABLED"].pack(side="right")
        tk.Label(bar, text="Efecto", font=F_TITLE, bg=BG, fg=TEXT).pack(side="right", padx=px(8))
        self.status = tk.Label(bar, font=F_SMALL, bg=BG, fg=DIM)
        self.status.pack(side="right", padx=px(20))

    def build_left(self, parent):
        w, h = px(560), px(210)
        frame = tk.Frame(parent, bg=LINE, padx=1, pady=1)
        frame.pack(pady=(0, px(12)))
        stage = tk.Canvas(frame, width=w, height=h, bg="#07080c", highlightthickness=0)
        stage.pack()
        self.preview = Preview(stage, w, h)

        sim = card(parent, "Vista previa")
        line = row(sim, "Simular", "Cómo se ve en cada situación")
        self.sim = Seg(line, [(False, "Agente"), (True, "Música")], self.simulate)
        self.sim.pack(side="right")
        self.sim.set(False)
        line = row(sim, "Ver en mis pantallas", "Enciende el borde real mientras ajustas")
        self.screen_preview = Switch(line, self.show_on_screens)
        self.screen_preview.pack(side="right")

        when = card(parent, "Cuándo se enciende", "El brillo aparece solo en estas situaciones.")
        for key, title, hint in (
                ("AGENT", "Mientras el agente trabaja", "Desde que envías un mensaje hasta que Claude responde"),
                ("MUSIC", "Con la música", "Solo el audio que sale del PC, nunca el micrófono")):
            self.switches[key] = Switch(row(when, title, hint), lambda on, k=key: self.change({k: on}))
            self.switches[key].pack(side="right")

    def scroller(self, parent):
        """A scrolling column; returns the frame to fill."""
        outer = tk.Frame(parent, bg=BG)
        outer.pack(side="left", fill="both", expand=True, padx=(px(16), 0))
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        window = canvas.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(0, px(8)))
        self.root.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))
        return inner

    def grid(self, parent, tiles, columns):
        for c in range(columns):
            parent.columnconfigure(c, weight=1, uniform="tile")
        for i, tile in enumerate(tiles):
            tile.grid(row=i // columns, column=i % columns, sticky="nsew", padx=px(3), pady=px(3))

    def fields_in(self, parent, group):
        for spec in FIELDS[group]:
            self.fields.append(Field(self, parent, *spec))

    def build_right(self, parent):
        styles = card(parent, "Estilos", "Un punto de partida. Luego ajusta lo que quieras.")
        grid = tk.Frame(styles, bg=CARD)
        grid.pack(fill="x")
        for name, text, values in PRESETS:
            tile = Tile(grid, name, text, swatch(values), lambda v=values: self.change(look(v)))
            self.preset_tiles.append((tile, look(values)))
        self.grid(grid, [t for t, _ in self.preset_tiles], 3)
        label(styles, "Mis perfiles", F_TITLE, pady=(px(14), 0))
        label(styles, "Guarda cómo se ve ahora (colores, animaciones, apariencia y música) "
                      "y vuelve a él con un toque.", fg=DIM, wrap=520)
        form = tk.Frame(styles, bg=CARD)
        form.pack(fill="x", pady=px(6))
        self.profile_name = tk.StringVar()
        entry = tk.Entry(form, textvariable=self.profile_name, font=F_SMALL, bg=RAISED, fg=TEXT,
                         insertbackground=TEXT, relief="flat", highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=px(4))
        entry.bind("<Return>", lambda e: self.save_profile())
        self.save_button = button(form, "Guardar", self.save_profile, primary=True)
        self.save_button.pack(side="left", padx=(px(8), 0))
        self.profile_name.trace_add("write", lambda *a: self.name_changed())
        self.profile_grid = tk.Frame(styles, bg=CARD)
        self.profile_grid.pack(fill="x")
        self.no_profiles = label(styles, "Aún no tienes perfiles guardados.", fg=DIM)
        self.build_profiles()
        self.name_changed()

        modes = card(parent, "Animaciones", "Toca una tarjeta para verla en la vista previa. "
                                            "Las encendidas se ven todas a la vez.")
        grid = tk.Frame(modes, bg=CARD)
        grid.pack(fill="x")
        self.cards = [ModeCard(grid, self, m) for m in glow.MODES]
        self.grid(grid, self.cards, 2)
        self.fields_in(modes, "motion")

        colour = card(parent, "Color", "Colores propios de cada animación, uno solo, tu propia "
                                       "combinación o los de lo que estás viendo.")
        self.segs["COLOUR_MODE"] = Seg(colour, list(COLOUR_NAMES.items()),
                                       lambda v: self.change({"COLOUR_MODE": v}))
        self.segs["COLOUR_MODE"].pack(fill="x", pady=(0, px(8)))
        self.panels = {name: tk.Frame(colour, bg=CARD) for name in COLOUR_NAMES}
        self.fields_in(self.panels["spectrum"], "spectrum")

        single = tk.Frame(self.panels["single"], bg=CARD)
        single.pack(fill="x")
        self.single_chip = tk.Frame(single, width=px(56), height=px(56), cursor="hand2")
        self.single_chip.pack(side="left", padx=(0, px(14)))
        self.single_chip.bind("<Button-1>", lambda e: self.edit_colour(0))
        quick = tk.Frame(single, bg=CARD)
        quick.pack(side="left", fill="x")
        label(quick, "Rápidos", fg=DIM, pady=(0, px(6)))
        dots = tk.Frame(quick, bg=CARD)
        dots.pack(anchor="w")
        for c in QUICK:
            dot = tk.Frame(dots, bg=c, width=px(22), height=px(22), cursor="hand2")
            dot.pack(side="left", padx=(0, px(6)))
            dot.bind("<Button-1>", lambda e, c=c: self.change(
                {"PALETTE": [c] + glow.PALETTE[1:], "COLOUR_MODE": "single"}))

        self.chips = tk.Frame(self.panels["palette"], bg=CARD)
        self.chips.pack(fill="x", pady=(0, px(8)))
        grid = tk.Frame(self.panels["palette"], bg=CARD)
        grid.pack(fill="x")
        for name, colours in PALETTES.items():
            tile = Tile(grid, name, "", colours, lambda c=colours: self.change(
                {"PALETTE": c, "COLOUR_MODE": "palette"}))
            self.palette_tiles.append((tile, colours))
        self.grid(grid, [t for t, _ in self.palette_tiles], 4)
        label(self.panels["palette"], "Hasta 6 colores. Se funden en orden a lo largo del borde.",
              fg=DIM, pady=(px(6), 0))

        screen = self.panels["screen"]
        row(screen, "Qué mirar", "Monitores numerados de izquierda a derecha")
        sources = [(0, "Ventana activa")] + [(i, f"Monitor {i}") for i in range(1, len(glow.displays()) + 1)]
        self.segs["SCREEN_SOURCE"] = Seg(screen, sources, self.watch)
        self.segs["SCREEN_SOURCE"].pack(fill="x", pady=(0, px(8)))
        self.live = Bar(screen, px(28))
        self.live.pack(fill="x")
        self.fields_in(screen, "screen")
        label(screen, "Toma los colores vivos de la imagen (luces, láseres, pantallas LED) varias "
                      "veces por segundo. Con “Ventana activa” sigue la ventana que tienes al frente; "
                      "si es esta, usa la que está detrás.", fg=DIM, wrap=520, pady=(px(6), 0))

        appearance = card(parent, "Apariencia", "Brillo, tamaño y suavidad del borde.")
        self.fields_in(appearance, "look")
        line = row(appearance, "Pantallas", "Al cambiarlo, el efecto se reinicia")
        self.segs["MONITORS"] = Seg(line, [("all", "Todas"), ("primary", "Solo principal")],
                                    lambda v: self.change({"MONITORS": v}))
        self.segs["MONITORS"].pack(side="right")

        self.music_card = card(parent, "Reacción a la música", "")
        self.fields_in(self.music_card, "music")
        toggle = label(self.music_card, "Avanzado  ▸", F_TITLE, fg=ACCENT, pady=(px(8), 0))
        toggle.configure(cursor="hand2")
        advanced = tk.Frame(self.music_card, bg=CARD)
        self.fields_in(advanced, "advanced")

        def flip(e):
            if advanced.winfo_ismapped():
                advanced.pack_forget()
                toggle.configure(text="Avanzado  ▸")
            else:
                advanced.pack(fill="x")
                toggle.configure(text="Avanzado  ▾")
        toggle.bind("<Button-1>", flip)

        system = card(parent, "Sistema")
        self.autostart = Switch(row(system, "Iniciar con Windows", "El efecto arranca solo al iniciar sesión"),
                                self.set_autostart)
        self.autostart.pack(side="right")
        self.autostart.set(autostart())
        button(row(system, "Valores de fábrica", "Vuelve al efecto original"), "Restablecer",
               lambda: self.change(glow.DEFAULTS)).pack(side="right")

    def build_profiles(self):
        for child in self.profile_grid.winfo_children():
            child.destroy()
        saved = sorted(profiles().items(), key=lambda p: p[0].lower())
        self.profile_tiles = [
            (Tile(self.profile_grid, name, describe(values), swatch(values),
                  lambda v=values: self.change(look(v)), lambda n=name: self.delete_profile(n)), look(values))
            for name, values in saved if isinstance(values, dict)]
        self.grid(self.profile_grid, [t for t, _ in self.profile_tiles], 3)
        if self.profile_tiles:
            self.no_profiles.pack_forget()
        else:
            self.no_profiles.pack(fill="x")

    # ------------------------------------------------------------ state
    def change(self, values):
        """Make settings live now; write them to disk once you pause."""
        glow.apply_config(values)
        self.preview.invalidate()
        self.refresh()
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(SAVE_DELAY, self.flush)

    def flush(self):
        self.pending = None
        glow.save_config()
        glow.spawn_daemon()

    def refresh(self):
        """Put every control in line with the live settings."""
        cfg = glow.current_config()
        self.syncing = True
        for key, switch in self.switches.items():
            switch.set(cfg[key])
        for key, seg in self.segs.items():
            seg.set(cfg[key])
        for field in self.fields:
            field.sync(cfg[field.key])
        self.syncing = False
        style = {k: cfg[k] for k in STYLE_KEYS}
        for tile, values in self.preset_tiles + self.profile_tiles:
            tile.mark(values == style)
        for tile, colours in self.palette_tiles:
            tile.mark(cfg["COLOUR_MODE"] == "palette" and colours == cfg["PALETTE"])
        for c in self.cards:
            c.switch.set(c.mode.name in cfg["MODES_ON"])
            c.mark(c.mode.name == self.focus)
        for name, panel in self.panels.items():
            if name == cfg["COLOUR_MODE"]:
                panel.pack(fill="x", after=self.segs["COLOUR_MODE"])
            else:
                panel.pack_forget()
        painted = (cfg["COLOUR_MODE"], tuple(cfg["PALETTE"]), cfg["SATURATION"], self.screen_version)
        if painted != self.painted:
            self.painted = painted
            self.paint_colours()
        self.music_card.lead.configure(
            text="Cómo responde el borde a lo que suena. Usa “Simular → Música” para verlo." if cfg["MUSIC"]
            else "La música está apagada en «Cuándo se enciende»: estos ajustes no se usan.")
        self.show_status()

    def paint_colours(self):
        for c in self.cards:
            c.paint()
        self.single_chip.configure(bg=glow.PALETTE[0])
        for child in self.chips.winfo_children():
            child.destroy()
        palette = glow.PALETTE
        for i, c in enumerate(palette):
            chip = tk.Frame(self.chips, bg=c, width=px(36), height=px(36), cursor="hand2")
            chip.pack(side="left", padx=(0, px(6)))
            chip.bind("<Button-1>", lambda e, i=i: self.edit_colour(i))
            if len(palette) > 1:
                cross = tk.Label(chip, text="✕", font=F_SMALL, bg=c, fg=ink(c), cursor="hand2")
                cross.place(relx=1.0, rely=0.0, anchor="ne")
                cross.bind("<Button-1>", lambda e, i=i: self.change(
                    {"PALETTE": palette[:i] + palette[i + 1:]}))
        if len(palette) < 6:
            add = tk.Label(self.chips, text="+", font=F_H2, bg=LINE, fg=TEXT, width=2, cursor="hand2")
            add.pack(side="left", ipady=px(2))
            add.bind("<Button-1>", lambda e: self.change({"PALETTE": palette + [
                "#%02x%02x%02x" % tuple(round(v * 255) for v in colorsys.hsv_to_rgb(random.random(), 0.85, 1))]}))
        live = glow.SCREEN_COLOURS
        self.live.show(np.repeat(np.asarray(live)[:, ::-1] * 255, 12, axis=0) if live is not None
                       else stops([LINE, CARD]))

    def show_status(self):
        text, colour = ("● Iniciando…", DIM) if not self.running else \
            ("● Activo", "#00e676") if glow.ENABLED else ("● En pausa", "#ffb300")
        self.status.configure(text=text, fg=colour)

    # ------------------------------------------------------------ actions
    def toggle_mode(self, name, on):
        chosen = [n for n in glow.MODES_ON if n != name] + ([name] if on else [])
        if chosen:  # at least one animation stays on
            self.change({"MODES_ON": chosen})

    def focus_mode(self, name):
        self.focus = None if self.focus == name else name
        if self.screen_preview.on:
            set_preview(True, self.focus or "")
        self.refresh()

    def simulate(self, music):
        self.music = music
        self.sim.set(music)

    def show_on_screens(self, on):
        set_preview(on, self.focus or "")
        self.screen_preview.set(on)

    def set_autostart(self, on):
        try:
            self.autostart.set(autostart(on))
        except OSError:  # refused by Windows or an antivirus: show it unchanged
            self.autostart.set(autostart())

    def edit_colour(self, i):
        _, chosen = colorchooser.askcolor(glow.PALETTE[i], parent=self.root, title="Color")
        if chosen:
            palette = list(glow.PALETTE)
            palette[i] = chosen
            self.change({"PALETTE": palette})

    def watch(self, source):
        self.change({"SCREEN_SOURCE": source})
        threading.Thread(target=identify, args=(source,), daemon=True).start()

    def name_changed(self):
        name = self.profile_name.get()
        if len(name) > 40:
            self.profile_name.set(name[:40])
            return
        exists = name.strip() in profiles()
        self.save_button.configure(text="Actualizar" if exists else "Guardar",
                                   state="normal" if name.strip() else "disabled")

    def save_profile(self):
        name = self.profile_name.get().strip()
        if not name:
            return
        if name in profiles() and not messagebox.askyesno(
                "Glow", f"¿Reemplazar el perfil “{name}” con los ajustes actuales?", parent=self.root):
            return
        edit_profiles(save=name)
        self.profile_name.set("")
        self.build_profiles()
        self.refresh()

    def delete_profile(self, name):
        if messagebox.askyesno("Glow", f"¿Eliminar el perfil “{name}”?", parent=self.root):
            edit_profiles(delete=name)
            self.build_profiles()
            self.name_changed()

    # ------------------------------------------------------------ loops
    def listen(self, sock):
        """Another launch connects here to bring this window forward."""
        while True:
            conn, _ = sock.accept()
            conn.close()
            self.wake.set()  # Tk is only touched from its own thread (tick)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def tick(self):
        try:
            if self.wake.is_set():
                self.wake.clear()
                self.show()
            self.preview.frame(self.music, self.focus)
        finally:
            self.root.after(33, self.tick)

    def ping(self):
        self.running = glow.is_running()
        if not self.running:
            glow.spawn_daemon()
        self.show_status()
        self.root.after(3000, self.ping)

    def sample(self):
        """While the colours come from the screen, look at it once a second."""
        try:
            if glow.COLOUR_MODE == "screen" and look_at_screen():
                self.screen_version += 1
                self.preview.invalidate()
                self.refresh()
        finally:
            self.root.after(1000, self.sample)

    def close(self):
        if self.pending:
            self.root.after_cancel(self.pending)
            self.flush()
        set_preview(False)
        self.root.destroy()


def main():
    # Started with a console (double-click, `python glow_ui.py`): hand over to
    # a windowless pythonw copy so closing the console leaves the window be.
    argv = glow.app_argv()
    if os.path.normcase(argv[0]) != os.path.normcase(sys.executable):
        glow.spawn(argv)
        return
    sock = claim()
    if sock is None:
        return  # already open: it has been brought forward
    glow.load_config()
    glow.spawn_daemon()  # the daemon owns the tray icon, so it always runs
    set_preview(False)   # a preview left over from a crash
    # its own taskbar button and icon instead of Python's
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenClaw.Glow")
    root = tk.Tk()
    App(root, sock)
    root.mainloop()


if __name__ == "__main__":
    main()

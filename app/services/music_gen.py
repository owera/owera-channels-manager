"""Generate focus/hyperfocus background music via local procedural synthesis.

Each track is unique across: root key, musical scale, bass waveform, melody
pattern, BPM, drum pattern, and dynamic arc (intro → build → peak → drop).
30 style presets covering lo-fi focus, ambient, deep work, and driving techno
ensure a diverse pool — no two tracks sound the same.

No API key or GPU required. Generates a 30-second WAV in ~0.2s using numpy +
scipy (reverb, filter sweep) + a tape-style delay.
"""

import logging
import random
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from app.config import settings
from app.db import session_scope
from app.services import quota

logger = logging.getLogger("manager.music_gen")

SR = 44100
_AUDIO_EXTS = {".mp3", ".m4a", ".wav"}

# ── Musical building blocks ────────────────────────────────────────────────────

# Root note frequencies (C2 octave, 65 Hz range — good for bass)
ROOTS: dict[str, float] = {
    "C": 65.41, "C#": 69.30, "D": 73.42, "Eb": 77.78,
    "E": 82.41, "F": 87.31, "F#": 92.50, "G": 98.00,
    "Ab": 103.83, "A": 110.00, "Bb": 116.54, "B": 123.47,
}

# Scales as semitone offsets (extended two octaves for melodic range)
SCALES: dict[str, list[int]] = {
    "minor_pentatonic": [0, 3, 5, 7, 10, 12, 15, 17, 19, 22],
    "dorian":           [0, 2, 3, 5, 7, 9, 10, 12, 14, 15],
    "phrygian":         [0, 1, 3, 5, 7, 8, 10, 12, 13, 15],
    "natural_minor":    [0, 2, 3, 5, 7, 8, 10, 12, 14, 15],
    "major_pentatonic": [0, 2, 4, 7, 9, 12, 14, 16, 19, 21],
    "lydian":           [0, 2, 4, 6, 7, 9, 11, 12, 14, 16],
    "mixolydian":       [0, 2, 4, 5, 7, 9, 10, 12, 14, 16],
}


def _hz_scale(root_hz: float, semitones: int) -> float:
    return root_hz * (2 ** (semitones / 12))


# ── Oscillators ───────────────────────────────────────────────────────────────

def _osc(freq: float, t: np.ndarray, wave: str = "sine") -> np.ndarray:
    phase = (freq * t) % 1.0
    if wave == "saw":
        return (2 * phase - 1).astype(np.float32)
    if wave == "square":
        return np.where(phase < 0.5, 1.0, -1.0).astype(np.float32)
    if wave == "triangle":
        return (2 * np.abs(2 * phase - 1) - 1).astype(np.float32)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)   # sine default


# ── Sound primitives ──────────────────────────────────────────────────────────

def _kick(punch: float = 1.0) -> np.ndarray:
    """Kick drum — frequency-swept sine, punchy transient."""
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    freq = 50 + 130 * np.exp(-t / 0.016)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    amp = np.exp(-t / (0.10 * punch))
    click = np.exp(-t / 0.003) * 0.35
    return ((amp * np.sin(phase) + click) * punch).astype(np.float32)


def _hihat(open_: bool = False, bright: float = 1.0) -> np.ndarray:
    """Hi-hat — high-passed noise burst."""
    dur = 0.18 if open_ else 0.035
    n = int(dur * SR)
    t = np.arange(n) / SR
    amp = np.exp(-t / (dur * (0.55 if open_ else 0.4)))
    noise = np.random.uniform(-1.0, 1.0, n).astype(np.float32)
    hp = noise - np.concatenate([[0], noise[:-1]]) * 0.92
    return (amp * hp * 0.32 * bright).astype(np.float32)


def _clap() -> np.ndarray:
    """Snare/clap — layered noise bursts + tonal resonance."""
    n = int(0.09 * SR)
    t = np.arange(n) / SR
    bursts = sum(
        np.exp(-((t - o) ** 2) / (2 * 0.0025 ** 2)) * np.random.uniform(-1, 1, n)
        for o in [0.0, 0.005, 0.011]
    )
    tone = np.exp(-t / 0.038) * np.sin(2 * np.pi * 210 * t) * 0.25
    amp = np.exp(-t / 0.055)
    return (amp * (bursts + tone) * 0.48).astype(np.float32)


def _bass_note(freq: float, duration: float, wave: str = "sine",
               distort: float = 1.6) -> np.ndarray:
    """Bass note — chosen waveform with soft-clip warmth."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    amp = 0.65 * np.exp(-t / (duration * 0.72)) + 0.18
    s = amp * _osc(freq, t, wave)
    return (np.tanh(s * distort) / distort).astype(np.float32)


def _pad_chord(root_hz: float, scale: list[int], duration: float,
               degrees: list[int], wave: str = "sine") -> np.ndarray:
    """Stacked chord pad — multiple scale degrees voiced together."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    attack = min(0.12, duration * 0.25)
    release = min(0.15, duration * 0.3)
    env = np.ones(n, dtype=np.float32)
    a_n, r_n = int(attack * SR), int(release * SR)
    if a_n > 0:
        env[:a_n] = np.linspace(0, 1, a_n)
    if r_n > 0:
        env[-r_n:] = np.linspace(1, 0, r_n)
    chord = np.zeros(n, dtype=np.float32)
    for deg in degrees:
        semi = scale[deg % len(scale)]
        freq = _hz_scale(root_hz * 2, semi)   # one octave up for pad register
        # Detune slightly for richness
        chord += _osc(freq, t, wave) * 0.4
        chord += _osc(freq * 1.004, t, "sine") * 0.2
    return (env * chord * 0.22).astype(np.float32)


def _lead_note(freq: float, duration: float, wave: str = "saw") -> np.ndarray:
    """Melodic lead note — bright with fast attack, medium release."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    attack = 0.008
    env = np.ones(n, dtype=np.float32)
    a_n, r_n = int(attack * SR), int(min(0.08, duration * 0.35) * SR)
    if a_n > 0:
        env[:a_n] = np.linspace(0, 1, a_n)
    if r_n > 0:
        env[-r_n:] = np.linspace(1, 0, r_n)
    s = _osc(freq, t, wave) + _osc(freq * 0.998, t, "sine") * 0.4
    return (env * s * 0.28).astype(np.float32)


def _add_at(mix: np.ndarray, signal: np.ndarray, offset: int) -> None:
    end = min(len(mix), offset + len(signal))
    mix[offset:end] += signal[:end - offset]


# ── Effects ───────────────────────────────────────────────────────────────────

def _apply_reverb(signal: np.ndarray, room: float = 0.4, wet: float = 0.18) -> np.ndarray:
    ir_len = int(SR * room)
    t_ir = np.arange(ir_len) / SR
    ir = np.exp(-t_ir / (room * 0.5)) * np.random.randn(ir_len).astype(np.float32)
    ir[0] = 1.0
    ir /= np.abs(ir).max() + 1e-9
    wet_sig = fftconvolve(signal, ir)[:len(signal)].astype(np.float32)
    return (signal * (1.0 - wet) + wet_sig * wet).astype(np.float32)


def _apply_delay(signal: np.ndarray, delay_s: float = 0.25,
                 feedback: float = 0.38, wet: float = 0.22) -> np.ndarray:
    """Tape-style delay: up to 5 decaying echoes."""
    echoes = np.zeros_like(signal)
    delay_n = int(delay_s * SR)
    atten = 1.0
    for _ in range(5):
        atten *= feedback
        if delay_n >= len(signal):
            break
        echoes[delay_n:] += signal[:len(signal) - delay_n] * atten
        delay_n += int(delay_s * SR)
    return (signal * (1.0 - wet) + echoes * wet).astype(np.float32)


def _apply_filter_sweep(signal: np.ndarray, f_low: float = 280.0,
                        f_high: float = 7000.0, cycles: int = 2,
                        n_blocks: int = 32) -> np.ndarray:
    nyq = SR / 2.0
    block = len(signal) // n_blocks
    result = np.empty_like(signal)
    for i in range(n_blocks):
        phase = (i / n_blocks) * cycles * 2 * np.pi
        t = (1.0 - np.cos(phase)) / 2.0
        cutoff = f_low + (f_high - f_low) * t
        cutoff_norm = min(cutoff / nyq * 0.98, 0.98)
        sos = butter(4, cutoff_norm, btype="low", output="sos")
        start = i * block
        end = start + block if i < n_blocks - 1 else len(signal)
        result[start:end] = sosfilt(sos, signal[start:end])
    return result.astype(np.float32)


def _section_envelope(total: int, sections: list[tuple[float, float, float]]) -> np.ndarray:
    """Build a gain envelope from section specs: [(start_frac, end_frac, gain), ...]
    with linear crossfade between adjacent sections."""
    env = np.zeros(total, dtype=np.float32)
    for start_f, end_f, gain in sections:
        s, e = int(start_f * total), int(end_f * total)
        fade = min(int(0.05 * total), (e - s) // 4)
        seg = np.ones(e - s, dtype=np.float32) * gain
        if fade > 0 and len(seg) >= 2 * fade:
            seg[:fade] *= np.linspace(0, 1, fade)
            seg[-fade:] *= np.linspace(1, 0, fade)
        env[s:e] += seg
    return env


# ── 30 Style presets ──────────────────────────────────────────────────────────
#
# Each preset defines:
#   desc        human-readable name
#   bpm         beats per minute
#   root        root note key (see ROOTS)
#   scale       scale name (see SCALES)
#   bass_wave   oscillator waveform for bass ("sine","saw","square","triangle")
#   bass_pat    bass melody: list of scale degree indices (8 notes)
#   rhythm      drum pattern ("4on4", "halfstep", "dotted", "none")
#   hh          hi-hat density ("8th", "16th", "sparse")
#   pad         use chord pad layer
#   pad_wave    pad waveform
#   lead        use melodic lead layer
#   lead_wave   lead waveform
#   lead_pat    lead melody: list of scale degree indices (8 notes)
#   reverb_wet  reverb wet mix
#   delay       apply tape delay to lead/pad
#   sweep       apply filter sweep to tonal layer
#   genre       tag for logging

TECHNO_STYLES = [
    # ── Hyperfocus / Lo-fi / Focus ──────────────────────────────────────────
    {"desc": "deep focus A dorian 90",
     "bpm": 90,  "root": "A",  "scale": "dorian",
     "bass_wave": "triangle", "bass_pat": [0, 0, 2, 0, 1, 3, 2, 0],
     "rhythm": "halfstep", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "sine",  "lead_pat": [2, 4, 3, 2, 1, 0, 2, 4],
     "reverb_wet": 0.28, "delay": True,  "sweep": False, "genre": "focus"},

    {"desc": "lo-fi groove F major 82",
     "bpm": 82,  "root": "F",  "scale": "major_pentatonic",
     "bass_wave": "sine", "bass_pat": [0, 0, 1, 0, 2, 1, 0, 3],
     "rhythm": "halfstep", "hh": "sparse",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "triangle", "lead_pat": [0, 2, 4, 2, 3, 1, 0, 2],
     "reverb_wet": 0.30, "delay": True,  "sweep": False, "genre": "lofi"},

    {"desc": "study flow G dorian 88",
     "bpm": 88,  "root": "G",  "scale": "dorian",
     "bass_wave": "sine", "bass_pat": [0, 0, 0, 2, 1, 0, 3, 2],
     "rhythm": "halfstep", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.25, "delay": False, "sweep": False, "genre": "focus"},

    {"desc": "binaural drift D lydian 75",
     "bpm": 75,  "root": "D",  "scale": "lydian",
     "bass_wave": "sine", "bass_pat": [0, 1, 2, 1, 0, 3, 1, 0],
     "rhythm": "none", "hh": "sparse",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "sine",  "lead_pat": [0, 2, 4, 6, 4, 2, 1, 0],
     "reverb_wet": 0.40, "delay": True,  "sweep": False, "genre": "ambient"},

    {"desc": "morning focus E major 95",
     "bpm": 95,  "root": "E",  "scale": "major_pentatonic",
     "bass_wave": "triangle", "bass_pat": [0, 0, 2, 1, 0, 3, 2, 0],
     "rhythm": "halfstep", "hh": "8th",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "triangle", "lead_pat": [0, 1, 3, 2, 4, 3, 1, 0],
     "reverb_wet": 0.22, "delay": False, "sweep": False, "genre": "focus"},

    {"desc": "deep work Bb phrygian 85",
     "bpm": 85,  "root": "Bb", "scale": "phrygian",
     "bass_wave": "triangle", "bass_pat": [0, 0, 1, 0, 2, 1, 3, 0],
     "rhythm": "halfstep", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "sine",  "lead_pat": [0, 1, 3, 1, 2, 0, 1, 3],
     "reverb_wet": 0.32, "delay": True,  "sweep": False, "genre": "focus"},

    {"desc": "flow state C dorian 100",
     "bpm": 100, "root": "C",  "scale": "dorian",
     "bass_wave": "sine", "bass_pat": [0, 0, 3, 0, 2, 0, 1, 3],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "triangle", "lead_pat": [2, 4, 3, 5, 4, 2, 0, 1],
     "reverb_wet": 0.22, "delay": True,  "sweep": False, "genre": "focus"},

    # ── Ambient / Atmospheric ────────────────────────────────────────────────
    {"desc": "space drift F lydian 110",
     "bpm": 110, "root": "F",  "scale": "lydian",
     "bass_wave": "sine", "bass_pat": [0, 0, 2, 1, 0, 3, 2, 1],
     "rhythm": "4on4", "hh": "sparse",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "sine",  "lead_pat": [0, 3, 5, 4, 2, 1, 3, 5],
     "reverb_wet": 0.38, "delay": True,  "sweep": True,  "genre": "ambient"},

    {"desc": "dub space A natural minor 116",
     "bpm": 116, "root": "A",  "scale": "natural_minor",
     "bass_wave": "sine", "bass_pat": [0, 0, 0, 2, 0, 1, 3, 0],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.38, "delay": True,  "sweep": True,  "genre": "dub"},

    {"desc": "cloud nine G lydian 104",
     "bpm": 104, "root": "G",  "scale": "lydian",
     "bass_wave": "triangle", "bass_pat": [0, 2, 1, 3, 0, 2, 4, 1],
     "rhythm": "4on4", "hh": "sparse",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "sine",  "lead_pat": [0, 2, 4, 5, 4, 2, 3, 1],
     "reverb_wet": 0.35, "delay": True,  "sweep": False, "genre": "ambient"},

    {"desc": "night drive Eb mixolydian 108",
     "bpm": 108, "root": "Eb", "scale": "mixolydian",
     "bass_wave": "saw", "bass_pat": [0, 0, 3, 0, 2, 4, 1, 0],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "saw",   "lead_pat": [0, 2, 4, 3, 5, 4, 2, 0],
     "reverb_wet": 0.25, "delay": True,  "sweep": True,  "genre": "ambient"},

    # ── Deep / Hypnotic Electronic ───────────────────────────────────────────
    {"desc": "Detroit deep F dorian 122",
     "bpm": 122, "root": "F",  "scale": "dorian",
     "bass_wave": "triangle", "bass_pat": [0, 0, 2, 0, 3, 0, 1, 2],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "triangle", "lead_pat": [0, 2, 3, 5, 4, 3, 1, 0],
     "reverb_wet": 0.28, "delay": False, "sweep": True,  "genre": "deep"},

    {"desc": "hypnotic C dorian 126",
     "bpm": 126, "root": "C",  "scale": "dorian",
     "bass_wave": "sine", "bass_pat": [0, 0, 0, 3, 0, 0, 2, 0],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.20, "delay": False, "sweep": True,  "genre": "minimal"},

    {"desc": "dub techno D dorian 124",
     "bpm": 124, "root": "D",  "scale": "dorian",
     "bass_wave": "sine", "bass_pat": [0, 0, 1, 0, 0, 2, 0, 3],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.40, "delay": True,  "sweep": True,  "genre": "dub"},

    {"desc": "progressive E lydian 128",
     "bpm": 128, "root": "E",  "scale": "lydian",
     "bass_wave": "saw", "bass_pat": [0, 0, 2, 3, 0, 1, 4, 2],
     "rhythm": "4on4", "hh": "16th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "saw",   "lead_pat": [0, 2, 5, 4, 6, 4, 2, 1],
     "reverb_wet": 0.22, "delay": False, "sweep": True,  "genre": "progressive"},

    # ── Driving Techno ───────────────────────────────────────────────────────
    {"desc": "driving underground C minor 128",
     "bpm": 128, "root": "C",  "scale": "minor_pentatonic",
     "bass_wave": "saw", "bass_pat": [0, 0, 0, 3, 0, 0, 2, 5],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.16, "delay": False, "sweep": True,  "genre": "techno"},

    {"desc": "Berlin minimal D dorian 130",
     "bpm": 130, "root": "D",  "scale": "dorian",
     "bass_wave": "triangle", "bass_pat": [0, 0, 2, 0, 0, 3, 0, 1],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.15, "delay": False, "sweep": True,  "genre": "minimal"},

    {"desc": "acid techno E phrygian 135",
     "bpm": 135, "root": "E",  "scale": "phrygian",
     "bass_wave": "square", "bass_pat": [0, 3, 0, 1, 0, 2, 3, 1],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": True, "lead_wave": "square","lead_pat": [0, 1, 3, 0, 2, 1, 4, 3],
     "reverb_wet": 0.18, "delay": False, "sweep": True,  "genre": "acid"},

    {"desc": "rolling groove A dorian 133",
     "bpm": 133, "root": "A",  "scale": "dorian",
     "bass_wave": "saw", "bass_pat": [0, 2, 0, 3, 1, 0, 2, 4],
     "rhythm": "4on4", "hh": "8th",
     "pad": False, "pad_wave": "sine",
     "lead": True, "lead_wave": "triangle", "lead_pat": [2, 3, 5, 4, 3, 1, 0, 2],
     "reverb_wet": 0.18, "delay": False, "sweep": True,  "genre": "techno"},

    {"desc": "melodic rise G dorian 128",
     "bpm": 128, "root": "G",  "scale": "dorian",
     "bass_wave": "saw", "bass_pat": [0, 0, 3, 0, 2, 5, 0, 4],
     "rhythm": "4on4", "hh": "16th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "saw",   "lead_pat": [0, 2, 4, 5, 4, 3, 2, 0],
     "reverb_wet": 0.20, "delay": False, "sweep": True,  "genre": "melodic"},

    {"desc": "dark industrial F# minor 132",
     "bpm": 132, "root": "F#", "scale": "natural_minor",
     "bass_wave": "square", "bass_pat": [0, 0, 2, 0, 3, 0, 0, 4],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.16, "delay": False, "sweep": True,  "genre": "industrial"},

    {"desc": "warehouse Eb minor 130",
     "bpm": 130, "root": "Eb", "scale": "natural_minor",
     "bass_wave": "saw", "bass_pat": [0, 0, 0, 2, 0, 3, 5, 0],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.18, "delay": False, "sweep": True,  "genre": "industrial"},

    # ── High Energy ──────────────────────────────────────────────────────────
    {"desc": "peak time G minor 138",
     "bpm": 138, "root": "G",  "scale": "minor_pentatonic",
     "bass_wave": "saw", "bass_pat": [0, 0, 3, 0, 0, 5, 0, 2],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.14, "delay": False, "sweep": True,  "genre": "techno"},

    {"desc": "hard rave B phrygian 140",
     "bpm": 140, "root": "B",  "scale": "phrygian",
     "bass_wave": "square", "bass_pat": [0, 0, 1, 0, 3, 0, 2, 0],
     "rhythm": "4on4", "hh": "16th",
     "pad": False, "pad_wave": "sine",
     "lead": True, "lead_wave": "square","lead_pat": [0, 1, 0, 3, 1, 2, 0, 4],
     "reverb_wet": 0.14, "delay": False, "sweep": True,  "genre": "rave"},

    {"desc": "EBM Bb minor 136",
     "bpm": 136, "root": "Bb", "scale": "natural_minor",
     "bass_wave": "square", "bass_pat": [0, 0, 2, 0, 4, 0, 3, 0],
     "rhythm": "4on4", "hh": "8th",
     "pad": False, "pad_wave": "sine",
     "lead": True, "lead_wave": "square","lead_pat": [0, 2, 1, 3, 2, 4, 1, 0],
     "reverb_wet": 0.16, "delay": False, "sweep": True,  "genre": "EBM"},

    {"desc": "trance-influenced Ab major 138",
     "bpm": 138, "root": "Ab", "scale": "major_pentatonic",
     "bass_wave": "saw", "bass_pat": [0, 0, 2, 0, 1, 3, 0, 4],
     "rhythm": "4on4", "hh": "16th",
     "pad": True,  "pad_wave": "sine",
     "lead": True, "lead_wave": "saw",   "lead_pat": [0, 2, 4, 3, 5, 4, 3, 2],
     "reverb_wet": 0.20, "delay": False, "sweep": True,  "genre": "trance"},

    # ── Groove / Crossover ───────────────────────────────────────────────────
    {"desc": "chill groove D mixolydian 115",
     "bpm": 115, "root": "D",  "scale": "mixolydian",
     "bass_wave": "triangle", "bass_pat": [0, 2, 0, 3, 0, 1, 4, 2],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "triangle","lead_pat": [0, 2, 4, 3, 5, 3, 1, 2],
     "reverb_wet": 0.24, "delay": True,  "sweep": False, "genre": "groove"},

    {"desc": "late night C# dorian 125",
     "bpm": 125, "root": "C#", "scale": "dorian",
     "bass_wave": "sine", "bass_pat": [0, 0, 2, 4, 0, 3, 1, 0],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.26, "delay": True,  "sweep": True,  "genre": "minimal"},

    {"desc": "hypnotic Berlin A dorian 130",
     "bpm": 130, "root": "A",  "scale": "dorian",
     "bass_wave": "triangle", "bass_pat": [0, 0, 0, 3, 2, 0, 0, 4],
     "rhythm": "4on4", "hh": "16th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.22, "delay": True,  "sweep": True,  "genre": "minimal"},

    {"desc": "drone pulse F minor 120",
     "bpm": 120, "root": "F",  "scale": "minor_pentatonic",
     "bass_wave": "sine", "bass_pat": [0, 0, 0, 0, 0, 2, 0, 3],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "sine",
     "lead": False,"lead_wave": "sine",  "lead_pat": [],
     "reverb_wet": 0.30, "delay": True,  "sweep": True,  "genre": "minimal"},

    {"desc": "deep lounge G# dorian 112",
     "bpm": 112, "root": "Ab", "scale": "dorian",
     "bass_wave": "triangle", "bass_pat": [0, 2, 0, 1, 3, 0, 2, 4],
     "rhythm": "4on4", "hh": "8th",
     "pad": True,  "pad_wave": "triangle",
     "lead": True, "lead_wave": "sine",  "lead_pat": [2, 4, 3, 5, 4, 2, 1, 3],
     "reverb_wet": 0.28, "delay": True,  "sweep": True,  "genre": "groove"},
]


# ── Main generator ─────────────────────────────────────────────────────────────

def generate_techno(duration_s: int = 30, style: dict | None = None,
                    seed: int | None = None) -> np.ndarray:
    """Synthesise a track. Returns float32 mono samples at SR."""
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)
    if style is None:
        style = random.choice(TECHNO_STYLES)

    bpm      = style["bpm"]
    root_hz  = ROOTS[style["root"]]
    scale    = SCALES[style["scale"]]
    beat     = 60.0 / bpm
    bar      = beat * 4
    total    = int(duration_s * SR)

    bass_pat  = style["bass_pat"]
    hh_dens   = style["hh"]
    rhythm    = style["rhythm"]

    drums  = np.zeros(total, dtype=np.float32)
    tonal  = np.zeros(total, dtype=np.float32)

    # ── Dynamic arc: intro → build → peak → breakdown → drop ──────────────
    # Expressed as (start_frac, end_frac, gain) for drums and tonal separately.
    if duration_s >= 28:
        drum_env  = _section_envelope(total, [(0.0, 0.25, 0.0),   # silent intro
                                              (0.25, 0.85, 1.0),  # in
                                              (0.70, 0.82, 0.0),  # breakdown
                                              (0.82, 1.0, 1.0)])  # drop
        tonal_env = _section_envelope(total, [(0.0, 0.10, 0.4),   # quiet intro
                                              (0.10, 0.70, 1.0),  # rise
                                              (0.70, 0.82, 0.6),  # breakdown
                                              (0.82, 1.0, 1.0)])  # full
    else:
        drum_env  = np.ones(total, dtype=np.float32)
        tonal_env = np.ones(total, dtype=np.float32)

    # ── Kick drum ──────────────────────────────────────────────────────────
    if rhythm == "4on4":
        t = 0.0
        while t < duration_s:
            _add_at(drums, _kick(), int(t * SR))
            t += beat
    elif rhythm == "halfstep":
        t = 0.0
        beat_n = 0
        while t < duration_s:
            if beat_n % 4 in (0, 2):   # beats 1 and 3 only
                _add_at(drums, _kick(punch=1.2), int(t * SR))
            t += beat
            beat_n += 1
    elif rhythm == "dotted":
        t = 0.0
        while t < duration_s:
            _add_at(drums, _kick(), int(t * SR))
            t2 = t + beat * 0.75
            if t2 < duration_s:
                _add_at(drums, _kick(punch=0.6), int(t2 * SR))
            t += beat * 1.5

    # ── Clap/snare ────────────────────────────────────────────────────────
    if rhythm != "none":
        t = beat
        while t < duration_s:
            _add_at(drums, _clap(), int(t * SR))
            t += beat * 2

    # ── Hi-hats ───────────────────────────────────────────────────────────
    if hh_dens == "16th":
        step, t = beat / 4, beat / 4
    elif hh_dens == "8th":
        step, t = beat / 2, beat / 2
    else:  # sparse
        step, t = beat, beat
    while t < duration_s:
        _add_at(drums, _hihat(), int(t * SR))
        t += step

    # Open hi-hat every bar on offbeat of beat 4
    if rhythm != "none":
        t = bar * 0.875
        while t < duration_s:
            _add_at(drums, _hihat(open_=True), int(t * SR))
            t += bar

    drums *= drum_env

    # ── Bass line ─────────────────────────────────────────────────────────
    beat_n = 0
    t = 0.0
    while t < duration_s:
        idx = bass_pat[beat_n % len(bass_pat)]
        semi = scale[idx % len(scale)]
        freq = _hz_scale(root_hz, semi)
        _add_at(tonal, _bass_note(freq, beat * 0.88, style["bass_wave"]), int(t * SR))
        t += beat
        beat_n += 1

    # ── Chord pad ─────────────────────────────────────────────────────────
    if style["pad"]:
        # Voice chords every bar (4 beats): root + third + fifth of scale
        t = 0.0
        while t < duration_s:
            _add_at(tonal, _pad_chord(root_hz, scale, bar * 0.95, [0, 2, 4],
                                      style["pad_wave"]), int(t * SR))
            t += bar

    # ── Lead melody ───────────────────────────────────────────────────────
    if style["lead"] and style["lead_pat"]:
        lead_pat = style["lead_pat"]
        step = beat / 2
        t = 0.0
        note_i = 0
        while t < duration_s:
            idx = lead_pat[note_i % len(lead_pat)]
            semi = scale[(idx + 5) % len(scale)]  # shift up a few scale degrees
            freq = _hz_scale(root_hz * 4, semi)   # two octaves up
            dur = step * random.choice([0.75, 0.85, 0.6])
            _add_at(tonal, _lead_note(freq, dur, style["lead_wave"]), int(t * SR))
            t += step
            note_i += 1

    tonal *= tonal_env

    # ── Filter sweep (tonal layer only) ───────────────────────────────────
    if style["sweep"]:
        sweep_cycles = 1 if duration_s <= 20 else 2
        tonal = _apply_filter_sweep(tonal, f_low=260.0, f_high=7200.0,
                                    cycles=sweep_cycles)

    # ── Tape delay (tonal layer — pad / lead styles only) ─────────────────
    if style["delay"]:
        delay_s = round(beat * random.choice([0.5, 0.75, 1.0]), 4)
        tonal = _apply_delay(tonal, delay_s=delay_s, feedback=0.35, wet=0.20)

    mix = drums + tonal

    # ── Room reverb (full mix) ────────────────────────────────────────────
    mix = _apply_reverb(mix, room=0.38, wet=style["reverb_wet"])

    # ── Normalise ─────────────────────────────────────────────────────────
    peak = np.abs(mix).max()
    if peak > 0:
        mix *= 0.80 / peak
    return mix


# ── IO helpers ────────────────────────────────────────────────────────────────

def _write_wav(samples: np.ndarray, path: Path) -> None:
    pcm = (samples * 32767).clip(-32767, 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def generate_and_save(prompt: str, bgm_dir: Path, duration_s: int = 30) -> Path:
    bgm_dir.mkdir(parents=True, exist_ok=True)
    style = random.choice(TECHNO_STYLES)
    samples = generate_techno(duration_s=duration_s, style=style)
    slug = str(int(time.time() * 1000))
    out = bgm_dir / f"techno_{slug}.wav"
    _write_wav(samples, out)
    logger.info("generated %s — %s %s %d BPM (%dKB)",
                out.name, style["genre"], style["desc"], style["bpm"],
                out.stat().st_size // 1024)
    return out


def pool_count(bgm_dir: Path) -> int:
    if not bgm_dir.exists():
        return 0
    return sum(1 for p in bgm_dir.iterdir()
               if p.suffix.lower() == ".wav" and p.stem.startswith("techno_"))


def list_tracks(bgm_dir: Path) -> list[dict]:
    if not bgm_dir.exists():
        return []
    result = []
    for p in sorted(bgm_dir.iterdir()):
        if p.suffix.lower() not in _AUDIO_EXTS:
            continue
        stat = p.stat()
        result.append({"name": p.name, "size_kb": round(stat.st_size / 1024, 1),
                        "created": stat.st_ctime})
    return result


def replenish(target: int | None = None) -> int:
    bgm_dir = Path(settings.bgm_dir)
    target = target or settings.bgm_pool_target
    current = pool_count(bgm_dir)
    if current >= target:
        return 0
    need = target - current
    logger.info("BGM pool %d/%d — generating %d tracks", current, target, need)
    generated = 0
    for _ in range(need):
        style = random.choice(TECHNO_STYLES)
        try:
            out = generate_and_save(style["desc"], bgm_dir)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="success",
                          detail=f"{out.name}: {style['desc']} {style['bpm']}bpm")
            generated += 1
        except Exception as e:
            logger.error("music_gen error: %s", e)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="error", detail=str(e)[:200])
    return generated

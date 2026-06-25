"""Generate techno background music via local procedural synthesis.

Uses numpy + scipy to synthesise kick drums, hi-hats, bass lines, synth pads,
room reverb, and a cycling low-pass filter sweep — the defining sound of techno.
Generates 30-second WAV files in < 2s with no API key or GPU required.

Variety comes from randomised BPM, bass patterns, synth arpeggios, and
hi-hat density across 20 defined style presets.
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

SR = 44100  # sample rate
_AUDIO_EXTS = {".mp3", ".m4a", ".wav"}

# 20 style presets — each defines the feel of one generated track.
# Fields: bpm, bass_pattern (note indices in minor pentatonic), hihat_density,
# synth (True/False), description (for logging).
TECHNO_STYLES = [
    {"bpm": 128, "bass": [0, 0, 0, 5], "hh": "8th",  "synth": False, "desc": "driving underground 128"},
    {"bpm": 130, "bass": [0, 0, 3, 0], "hh": "16th", "synth": False, "desc": "minimal Berlin 130"},
    {"bpm": 135, "bass": [0, 3, 5, 3], "hh": "16th", "synth": True,  "desc": "acid techno 135"},
    {"bpm": 126, "bass": [0, 0, 0, 7], "hh": "8th",  "synth": True,  "desc": "deep Detroit 126"},
    {"bpm": 140, "bass": [0, 0, 5, 0], "hh": "16th", "synth": False, "desc": "hard techno 140"},
    {"bpm": 132, "bass": [0, 3, 0, 5], "hh": "8th",  "synth": False, "desc": "dark industrial 132"},
    {"bpm": 128, "bass": [0, 0, 7, 5], "hh": "16th", "synth": True,  "desc": "melodic techno 128"},
    {"bpm": 136, "bass": [0, 5, 3, 7], "hh": "16th", "synth": False, "desc": "EBM 136"},
    {"bpm": 124, "bass": [0, 0, 0, 3], "hh": "8th",  "synth": True,  "desc": "dub techno 124"},
    {"bpm": 138, "bass": [0, 3, 3, 5], "hh": "16th", "synth": False, "desc": "peak-time 138"},
    {"bpm": 125, "bass": [0, 0, 5, 7], "hh": "8th",  "synth": False, "desc": "minimal late-night 125"},
    {"bpm": 130, "bass": [0, 7, 0, 5], "hh": "8th",  "synth": True,  "desc": "analogue 130"},
    {"bpm": 140, "bass": [0, 0, 3, 5], "hh": "16th", "synth": False, "desc": "raw rave 140"},
    {"bpm": 128, "bass": [0, 5, 0, 3], "hh": "16th", "synth": True,  "desc": "progressive 128"},
    {"bpm": 126, "bass": [0, 0, 7, 0], "hh": "8th",  "synth": True,  "desc": "atmospheric 126"},
    {"bpm": 133, "bass": [0, 3, 5, 0], "hh": "16th", "synth": False, "desc": "rolling groove 133"},
    {"bpm": 138, "bass": [0, 5, 5, 3], "hh": "16th", "synth": True,  "desc": "trance-influenced 138"},
    {"bpm": 120, "bass": [0, 0, 0, 0], "hh": "8th",  "synth": True,  "desc": "drone meditative 120"},
    {"bpm": 132, "bass": [0, 7, 3, 5], "hh": "16th", "synth": False, "desc": "industrial 132"},
    {"bpm": 130, "bass": [0, 0, 5, 3], "hh": "8th",  "synth": True,  "desc": "hypnotic Berlin 130"},
]

# Minor pentatonic intervals in semitones from root C1 (32.70 Hz)
_ROOT_HZ = 32.70
_PENTATONIC = [0, 3, 5, 7, 10, 12, 15, 17]  # minor pentatonic, two octaves


def _hz(semitones: int) -> float:
    return _ROOT_HZ * (2 ** (semitones / 12))


def _kick(duration: float = 0.5) -> np.ndarray:
    """Kick drum: frequency-swept sine with exponential amplitude decay."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    freq = 50 + 120 * np.exp(-t / 0.018)       # pitch: 170Hz → 50Hz
    phase = 2 * np.pi * np.cumsum(freq) / SR
    amp = np.exp(-t / 0.12)                      # punchy decay
    click = np.exp(-t / 0.003) * 0.3            # transient click
    return (amp * np.sin(phase) + click).astype(np.float32)


def _hihat(duration: float = 0.035, open_: bool = False) -> np.ndarray:
    """Hi-hat: shaped white noise burst."""
    dur = duration * (5 if open_ else 1)
    n = int(dur * SR)
    t = np.arange(n) / SR
    tau = dur * (0.6 if open_ else 0.4)
    amp = np.exp(-t / tau)
    noise = np.random.uniform(-1.0, 1.0, n).astype(np.float32)
    # Simple high-pass: subtract low-frequency component
    hp = noise - np.concatenate([[0], noise[:-1]]) * 0.95
    return (amp * hp * 0.35).astype(np.float32)


def _clap(duration: float = 0.08) -> np.ndarray:
    """Snare/clap: three short noise bursts + resonance."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    bursts = sum(
        np.exp(-((t - o) ** 2) / (2 * 0.003 ** 2)) * np.random.uniform(-1, 1, n)
        for o in [0.0, 0.006, 0.012]
    )
    tone = np.exp(-t / 0.04) * np.sin(2 * np.pi * 220 * t) * 0.3
    amp = np.exp(-t / (duration * 0.6))
    return (amp * (bursts + tone) * 0.5).astype(np.float32)


def _bass_note(freq: float, duration: float, distort: float = 1.8) -> np.ndarray:
    """Bass note: sine + soft clip for warmth."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    amp = 0.7 * np.exp(-t / (duration * 0.75)) + 0.15
    s = amp * np.sin(2 * np.pi * freq * t)
    return (np.tanh(s * distort) / distort).astype(np.float32)


def _synth_note(freq: float, duration: float) -> np.ndarray:
    """Synth pad: detuned oscillators with slow attack."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    attack = 0.05
    release = min(0.1, duration * 0.3)
    env = np.ones(n, dtype=np.float32)
    a_n = int(attack * SR)
    r_n = int(release * SR)
    if a_n > 0:
        env[:a_n] = np.linspace(0, 1, a_n)
    if r_n > 0:
        env[-r_n:] = np.linspace(1, 0, r_n)
    # Two slightly detuned oscillators
    s = (np.sin(2 * np.pi * freq * t) +
         np.sin(2 * np.pi * freq * 1.003 * t) * 0.7)
    return (env * s * 0.25).astype(np.float32)


def _add_at(mix: np.ndarray, signal: np.ndarray, offset: int) -> None:
    end = min(len(mix), offset + len(signal))
    mix[offset:end] += signal[:end - offset]


def _apply_reverb(signal: np.ndarray, room: float = 0.4, wet: float = 0.18) -> np.ndarray:
    """Convolve with a synthetic room impulse response.

    room  — decay time in seconds (longer = bigger room).
    wet   — mix ratio: 0 = dry, 1 = fully wet.
    """
    ir_len = int(SR * room)
    t_ir = np.arange(ir_len) / SR
    # Exponentially decaying noise burst as impulse response
    ir = np.exp(-t_ir / (room * 0.5)) * np.random.randn(ir_len).astype(np.float32)
    ir[0] = 1.0   # identity at t=0 preserves the dry signal's transient
    ir = ir / (np.abs(ir).max() + 1e-9)
    wet_sig = fftconvolve(signal, ir)[:len(signal)].astype(np.float32)
    return (signal * (1.0 - wet) + wet_sig * wet).astype(np.float32)


def _apply_filter_sweep(signal: np.ndarray, f_low: float = 300.0,
                        f_high: float = 7000.0, cycles: int = 2,
                        n_blocks: int = 32) -> np.ndarray:
    """Cycling low-pass filter sweep: opens from f_low → f_high and back, `cycles` times.

    This is the defining sound of techno — muffled at the start of each cycle,
    fully open at the peak, then closing again for the next drop.
    """
    nyq = SR / 2.0
    block = len(signal) // n_blocks
    result = np.empty_like(signal)

    for i in range(n_blocks):
        # Sinusoidal sweep position: 0 → 1 → 0 per cycle
        phase = (i / n_blocks) * cycles * 2 * np.pi
        t = (1.0 - np.cos(phase)) / 2.0          # 0..1 smooth cosine envelope
        cutoff = f_low + (f_high - f_low) * t
        cutoff_norm = min(cutoff / nyq * 0.98, 0.98)

        sos = butter(4, cutoff_norm, btype="low", output="sos")
        start = i * block
        end = start + block if i < n_blocks - 1 else len(signal)
        result[start:end] = sosfilt(sos, signal[start:end])

    return result.astype(np.float32)


def generate_techno(duration_s: int = 30, style: dict | None = None,
                    seed: int | None = None) -> np.ndarray:
    """Synthesise a techno track. Returns float32 mono samples at SR."""
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)
    if style is None:
        style = random.choice(TECHNO_STYLES)

    bpm = style["bpm"]
    beat = 60.0 / bpm
    total = int(duration_s * SR)

    # Separate layers so effects can be applied selectively
    drums = np.zeros(total, dtype=np.float32)   # kick + clap + hi-hats
    tonal = np.zeros(total, dtype=np.float32)   # bass + synth (gets filter sweep)

    bass_pattern = style["bass"]
    hh_density = style["hh"]
    use_synth = style["synth"]

    # --- Kick: four-on-the-floor ---
    t = 0.0
    while t < duration_s:
        _add_at(drums, _kick(), int(t * SR))
        t += beat

    # --- Clap/snare: beats 2 and 4 ---
    t = beat
    while t < duration_s:
        _add_at(drums, _clap(), int(t * SR))
        t += beat * 2

    # --- Closed hi-hat ---
    step = beat / 2 if hh_density == "8th" else beat / 4
    t = step
    while t < duration_s:
        _add_at(drums, _hihat(), int(t * SR))
        t += step

    # --- Open hi-hat: every bar (4 beats), on the "and" of beat 4 ---
    t = beat * 3.5
    while t < duration_s:
        _add_at(drums, _hihat(open_=True), int(t * SR))
        t += beat * 4

    # --- Bass line: one note per beat ---
    beat_n = 0
    t = 0.0
    while t < duration_s:
        semi = _PENTATONIC[bass_pattern[beat_n % len(bass_pattern)]]
        freq = _hz(semi)
        _add_at(tonal, _bass_note(freq, beat * 0.88), int(t * SR))
        t += beat
        beat_n += 1

    # --- Synth arpeggio (optional): eighth notes, higher octave ---
    if use_synth:
        arp_notes = [_PENTATONIC[i] + 12 for i in [0, 2, 3, 4]]
        t = 0.0
        note_i = 0
        while t < duration_s:
            semi = arp_notes[note_i % len(arp_notes)]
            freq = _hz(semi) * 4
            _add_at(tonal, _synth_note(freq, beat / 2 * 0.8), int(t * SR))
            t += beat / 2
            note_i += 1

    # --- Filter sweep on tonal layer (bass + synth) ---
    # Cycles and sweep range vary by style for variety
    sweep_cycles = 1 if duration_s <= 15 else 2
    tonal = _apply_filter_sweep(tonal, f_low=250.0, f_high=6500.0, cycles=sweep_cycles)

    # --- Mix layers ---
    mix = drums + tonal

    # --- Room reverb on the full mix (subtle — keeps drums punchy) ---
    mix = _apply_reverb(mix, room=0.35, wet=0.15)

    # --- Normalise to 80% of full scale ---
    peak = np.abs(mix).max()
    if peak > 0:
        mix *= 0.8 / peak
    return mix


def _write_wav(samples: np.ndarray, path: Path) -> None:
    """Write float32 mono samples as 16-bit PCM WAV."""
    pcm = (samples * 32767).clip(-32767, 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def generate_and_save(prompt: str, bgm_dir: Path, duration_s: int = 30) -> Path:
    """Generate one techno track and save it to bgm_dir. Returns the path.

    `prompt` is used only for logging; style is picked randomly.
    """
    bgm_dir.mkdir(parents=True, exist_ok=True)
    style = random.choice(TECHNO_STYLES)
    samples = generate_techno(duration_s=duration_s, style=style)
    slug = str(int(time.time() * 1000))
    out = bgm_dir / f"techno_{slug}.wav"
    _write_wav(samples, out)
    size_kb = out.stat().st_size // 1024
    logger.info("generated track: %s (%dKB, %s, %d BPM)",
                out.name, size_kb, style["desc"], style["bpm"])
    return out


def pool_count(bgm_dir: Path) -> int:
    """Count audio files in bgm_dir (same extensions as worker._pick_bgm)."""
    if not bgm_dir.exists():
        return 0
    return sum(1 for p in bgm_dir.iterdir() if p.suffix.lower() in _AUDIO_EXTS)


def list_tracks(bgm_dir: Path) -> list[dict]:
    """Return metadata for each audio file in bgm_dir, sorted by name."""
    if not bgm_dir.exists():
        return []
    result = []
    for p in sorted(bgm_dir.iterdir()):
        if p.suffix.lower() not in _AUDIO_EXTS:
            continue
        stat = p.stat()
        result.append({
            "name": p.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "created": stat.st_ctime,
        })
    return result


def replenish(target: int | None = None) -> int:
    """Generate tracks until bgm_dir has `target` files.

    Returns the number of tracks generated.
    """
    bgm_dir = Path(settings.bgm_dir)
    target = target or settings.bgm_pool_target
    current = pool_count(bgm_dir)
    if current >= target:
        return 0

    need = target - current
    logger.info("BGM pool has %d tracks, target %d — generating %d", current, target, need)

    generated = 0
    for _ in range(need):
        style = random.choice(TECHNO_STYLES)
        try:
            out = generate_and_save(style["desc"], bgm_dir)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="success",
                          detail=f"generated {out.name}: {style['desc']} {style['bpm']}bpm")
            generated += 1
        except Exception as e:
            logger.error("music_gen: track generation failed: %s", e)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="error", detail=str(e)[:200])

    return generated

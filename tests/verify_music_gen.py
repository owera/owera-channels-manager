"""Dependency-free regression checks for app/services/music_gen.py.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_music_gen.py

music_gen is the local numpy techno synth + BGM pool manager that keeps the
render pipeline from going silent: the scheduler's replenish tick, issues.py's
"BGM pool low" signal, and every render that pulls a random track all depend
on generate_techno / pool_count / replenish. pool_count gained incidental
coverage via verify_scheduler; the synthesis, presets, WAV IO, list_tracks,
and replenish gate had zero direct tests. Covers:

  - pure helpers: _hz_scale octave math, _osc shapes (sine/saw/square/triangle
    + unknown→sine), _add_at boundary (clips past end of mix)
  - sound primitives: kick/hihat/clap/bass/pad/lead return float32 of expected
    length, finite, non-zero energy
  - effects: reverb/delay/filter preserve length + dtype; delay at delay_s past
    signal length is a no-op on echoes; section envelope builds the arc
  - TECHNO_STYLES: exactly 31 presets (docstring says 30 — reality is 31);
    every preset carries the required keys; root/scale/rhythm/hh/wave values
    resolve against the live tables
  - generate_techno: sample count = duration*SR, float32, peak ≤ 0.80+eps,
    seed reproducibility, short (<28s) flat envelopes vs long sectional arc,
    rhythm="none" still produces tonal energy, all 31 styles generate without
    error at a short duration (each style is a real synthesis path)
  - pool_count: missing dir → 0; only techno_*.wav counts (mp3 / foreign stem /
    uppercase .WAV ok; nested dirs ignored)
  - list_tracks: missing dir → []; all three audio extensions; non-audio
    ignored; sorted by name; size_kb / created present
  - _write_wav + generate_and_save: real mono 16-bit 44100 WAV named
    techno_<ms>.wav lands under bgm_dir; prompt is ignored for style pick
  - replenish: at-or-above target returns 0 and writes nothing; below target
    generates exactly the deficit; a raising generate_and_save still logs an
    error JobRun and continues the loop (partial success)

Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import contextlib
import logging
import struct
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import JobRun
from app.services import music_gen, quota
from app.services.music_gen import (
    ROOTS,
    SCALES,
    SR,
    TECHNO_STYLES,
    _add_at,
    _apply_delay,
    _apply_filter_sweep,
    _apply_reverb,
    _bass_note,
    _clap,
    _hihat,
    _hz_scale,
    _kick,
    _lead_note,
    _osc,
    _pad_chord,
    _section_envelope,
    _write_wav,
    generate_and_save,
    generate_techno,
    list_tracks,
    pool_count,
    replenish,
)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# --------------------------------------------------------------------------- helpers
REQUIRED_STYLE_KEYS = {
    "desc", "bpm", "root", "scale", "bass_wave", "bass_pat", "rhythm", "hh",
    "pad", "pad_wave", "lead", "lead_wave", "lead_pat", "reverb_wet", "delay",
    "sweep", "genre",
}
VALID_RHYTHMS = {"4on4", "halfstep", "dotted", "none"}
VALID_HH = {"8th", "16th", "sparse"}
VALID_WAVES = {"sine", "saw", "square", "triangle"}


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


@contextlib.contextmanager
def _scoped(session: Session):
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise


# --------------------------------------------------------------------------- pure helpers
print("pure helpers (_hz_scale / _osc / _add_at)")

ok(abs(_hz_scale(110.0, 12) - 220.0) < 1e-9, "_hz_scale(+12) is exactly one octave up")
ok(abs(_hz_scale(110.0, -12) - 55.0) < 1e-9, "_hz_scale(-12) is exactly one octave down")
ok(abs(_hz_scale(110.0, 0) - 110.0) < 1e-9, "_hz_scale(0) is identity")

t = np.arange(int(0.02 * SR), dtype=np.float64) / SR
for wave_name in ("sine", "saw", "square", "triangle"):
    s = _osc(440.0, t, wave_name)
    ok(s.dtype == np.float32, f"_osc({wave_name}) returns float32")
    ok(len(s) == len(t), f"_osc({wave_name}) length matches t")
    ok(np.isfinite(s).all(), f"_osc({wave_name}) is finite")
    ok(float(np.abs(s).max()) > 0.5, f"_osc({wave_name}) has non-trivial amplitude")

# unknown wave falls through to sine (and sine is itself non-silent — a
# "return zeros for both" mutant would pass allclose alone)
unknown = _osc(440.0, t, "not-a-wave")
sine = _osc(440.0, t, "sine")
ok(np.allclose(unknown, sine) and float(np.abs(sine).max()) > 0.5,
   "unknown wave shape falls through to non-silent sine")

mix = np.zeros(100, dtype=np.float32)
_add_at(mix, np.ones(50, dtype=np.float32), 80)
ok(np.allclose(mix[80:], 1.0) and mix[79] == 0.0,
   "_add_at clips the signal past the end of mix (no overflow)")
_add_at(mix, np.ones(10, dtype=np.float32), 0)
ok(float(mix[0]) == 1.0 and float(mix[9]) == 1.0 and float(mix[10]) == 0.0,
   "_add_at at offset 0 writes the full signal when it fits")


# --------------------------------------------------------------------------- sound primitives
print("sound primitives (kick / hihat / clap / bass / pad / lead)")

kick = _kick()
ok(kick.dtype == np.float32 and len(kick) == int(0.5 * SR),
   "_kick is float32 of 0.5s")
ok(np.isfinite(kick).all() and float(np.abs(kick).max()) > 0.1,
   "_kick has finite non-trivial energy")

hh_closed = _hihat(open_=False)
hh_open = _hihat(open_=True)
ok(len(hh_open) > len(hh_closed), "open hi-hat is longer than closed")
ok(hh_closed.dtype == np.float32 and np.isfinite(hh_closed).all(),
   "closed hi-hat is float32 + finite")

clap = _clap()
ok(clap.dtype == np.float32 and len(clap) == int(0.09 * SR),
   "_clap is float32 of 0.09s")
ok(float(np.abs(clap).max()) > 0.0, "_clap has energy")

for w in VALID_WAVES:
    bn = _bass_note(110.0, 0.2, w)
    ok(bn.dtype == np.float32 and len(bn) == int(0.2 * SR),
       f"_bass_note({w}) length/dtype")
    ok(np.isfinite(bn).all(), f"_bass_note({w}) finite")

pad = _pad_chord(110.0, SCALES["dorian"], 0.5, [0, 2, 4], "sine")
ok(pad.dtype == np.float32 and len(pad) == int(0.5 * SR),
   "_pad_chord length/dtype")
ok(float(np.abs(pad).max()) > 0.0, "_pad_chord has energy")

lead = _lead_note(440.0, 0.15, "saw")
ok(lead.dtype == np.float32 and len(lead) == int(0.15 * SR),
   "_lead_note length/dtype")
ok(float(np.abs(lead).max()) > 0.0, "_lead_note has energy")


# --------------------------------------------------------------------------- effects
print("effects (reverb / delay / filter / section envelope)")

sig = np.sin(2 * np.pi * 220 * np.arange(int(0.5 * SR)) / SR).astype(np.float32)
rev = _apply_reverb(sig, room=0.2, wet=0.3)
ok(len(rev) == len(sig) and rev.dtype == np.float32,
   "reverb preserves length + float32")
ok(np.isfinite(rev).all(), "reverb output is finite")
# wet>0 must change the signal (not a pure pass-through)
ok(not np.allclose(rev, sig), "reverb with wet>0 alters the signal")

# delay_s past the signal: echoes never land → dry residual only (wet blend)
dly_noop = _apply_delay(sig, delay_s=2.0, feedback=0.5, wet=0.5)
ok(len(dly_noop) == len(sig), "delay past signal length still returns same length")
# dry * (1-wet) path: peak drops to ~half when no echoes land
ok(float(np.abs(dly_noop).max()) < float(np.abs(sig).max()),
   "delay with delay_s past length attenuates dry path (no echoes)")

dly = _apply_delay(sig, delay_s=0.05, feedback=0.4, wet=0.3)
ok(len(dly) == len(sig) and np.isfinite(dly).all(),
   "delay with in-range delay_s preserves length + finite")
# Discriminate three failure modes: pure pass-through (wet=0), pure dry
# attenuation (echoes stripped), and a totally-silent wet path.
ok(not np.allclose(dly, sig), "delay with wet>0 is not a pure pass-through")
ok(not np.allclose(dly, sig * (1.0 - 0.3)),
   "delay with in-range delay_s actually adds echoes (not dry-only)")

swept = _apply_filter_sweep(sig, f_low=300.0, f_high=4000.0, cycles=1, n_blocks=8)
ok(len(swept) == len(sig) and swept.dtype == np.float32,
   "filter sweep preserves length + float32")
ok(np.isfinite(swept).all(), "filter sweep output is finite")

env = _section_envelope(1000, [(0.0, 0.5, 1.0), (0.5, 1.0, 0.5)])
ok(len(env) == 1000 and env.dtype == np.float32, "section envelope length/dtype")
# middle of first section ≈ 1.0, middle of second ≈ 0.5 (away from crossfade edges)
ok(abs(float(env[250]) - 1.0) < 1e-5, "section envelope mid-first-section gain is 1.0")
ok(abs(float(env[750]) - 0.5) < 1e-5, "section envelope mid-second-section gain is 0.5")
# empty sections → zeros
ok(float(_section_envelope(100, []).sum()) == 0.0,
   "section envelope with no sections is all zeros")


# --------------------------------------------------------------------------- TECHNO_STYLES contract
print("TECHNO_STYLES contract (31 presets, keys, table refs)")

# Docstring / module comment still say "30" but the list has grown to 31 —
# pin the live count so a silent drop (or accidental wipe) fails loudly.
ok(len(TECHNO_STYLES) == 31, f"exactly 31 style presets (got {len(TECHNO_STYLES)})")
descs = [s["desc"] for s in TECHNO_STYLES]
ok(len(descs) == len(set(descs)), "every style desc is unique")

for i, style in enumerate(TECHNO_STYLES):
    missing = REQUIRED_STYLE_KEYS - set(style.keys())
    ok(not missing, f"style[{i}] has all required keys (missing {missing})")
    ok(style["root"] in ROOTS, f"style[{i}] root '{style['root']}' is in ROOTS")
    ok(style["scale"] in SCALES, f"style[{i}] scale '{style['scale']}' is in SCALES")
    ok(style["rhythm"] in VALID_RHYTHMS, f"style[{i}] rhythm '{style['rhythm']}' is valid")
    ok(style["hh"] in VALID_HH, f"style[{i}] hh '{style['hh']}' is valid")
    ok(style["bass_wave"] in VALID_WAVES, f"style[{i}] bass_wave is valid")
    ok(style["pad_wave"] in VALID_WAVES, f"style[{i}] pad_wave is valid")
    ok(style["lead_wave"] in VALID_WAVES, f"style[{i}] lead_wave is valid")
    ok(isinstance(style["bpm"], (int, float)) and 60 <= style["bpm"] <= 200,
       f"style[{i}] bpm is a sane 60-200 value")
    ok(isinstance(style["bass_pat"], list) and len(style["bass_pat"]) >= 1,
       f"style[{i}] bass_pat is a non-empty list")
    ok(isinstance(style["pad"], bool) and isinstance(style["lead"], bool),
       f"style[{i}] pad/lead are bools")
    ok(isinstance(style["delay"], bool) and isinstance(style["sweep"], bool),
       f"style[{i}] delay/sweep are bools")
    if style["lead"]:
        ok(isinstance(style["lead_pat"], list) and len(style["lead_pat"]) >= 1,
           f"style[{i}] lead=True implies non-empty lead_pat")


# --------------------------------------------------------------------------- generate_techno
print("generate_techno (length, peak, seed, envelopes, styles)")

# short path: duration < 28 uses flat envelopes (cheaper + discriminates branch)
short = generate_techno(duration_s=4, style=TECHNO_STYLES[0], seed=42)
ok(short.dtype == np.float32, "generate_techno returns float32")
ok(len(short) == 4 * SR, "generate_techno sample count = duration * SR")
ok(np.isfinite(short).all(), "generate_techno output is finite")
peak = float(np.abs(short).max())
ok(peak > 0.0, "generate_techno produces non-silent audio")
ok(peak <= 0.80 + 1e-5, f"generate_techno peak is normalised to ≤0.80 (got {peak:.4f})")

# seed reproducibility
a = generate_techno(duration_s=3, style=TECHNO_STYLES[1], seed=7)
b = generate_techno(duration_s=3, style=TECHNO_STYLES[1], seed=7)
c = generate_techno(duration_s=3, style=TECHNO_STYLES[1], seed=8)
ok(np.allclose(a, b), "same seed + same style → identical samples")
ok(not np.allclose(a, c), "different seed → different samples (noise/delay vary)")

# long path (≥28s) exercises the sectional drum/tonal envelopes; use a no-lead
# no-delay style to keep runtime low, and seed for determinism
long_style = next(s for s in TECHNO_STYLES
                  if s["rhythm"] == "4on4" and not s["lead"] and not s["delay"])
long = generate_techno(duration_s=28, style=long_style, seed=1)
ok(len(long) == 28 * SR, "long (≥28s) path sample count = duration * SR")
ok(np.isfinite(long).all() and float(np.abs(long).max()) > 0.0,
   "long path produces finite non-silent audio")
# intro is drum-silent for the first 25% of the arc — first ~1s of drums should
# be near-zero relative to the peak of the drop. Use a slice of pure tonal+reverb
# residual: absolute energy in the first 0.5s should be lower than the peak slice.
intro_e = float(np.mean(long[: int(0.5 * SR)] ** 2))
peak_e = float(np.mean(long[int(0.85 * 28 * SR): int(0.90 * 28 * SR)] ** 2))
ok(intro_e < peak_e, "long path: intro energy is below drop energy (sectional arc)")

# rhythm="none": no kicks/claps, but tonal (bass/pad/lead) still writes
none_style = next(s for s in TECHNO_STYLES if s["rhythm"] == "none")
none_out = generate_techno(duration_s=3, style=none_style, seed=3)
ok(float(np.abs(none_out).max()) > 0.0,
   "rhythm='none' still produces tonal energy (not a silent track)")

# halfstep + dotted paths (the other two rhythm branches)
half = next(s for s in TECHNO_STYLES if s["rhythm"] == "halfstep")
ok(float(np.abs(generate_techno(duration_s=3, style=half, seed=2)).max()) > 0.0,
   "rhythm='halfstep' generates non-silent audio")
# dotted is in the valid set but no preset currently uses it — synthesise one
dotted = dict(TECHNO_STYLES[0])
dotted["rhythm"] = "dotted"
dotted["lead"] = False  # speed
ok(float(np.abs(generate_techno(duration_s=3, style=dotted, seed=2)).max()) > 0.0,
   "rhythm='dotted' (synthetic preset) generates non-silent audio")

# every real preset synthesises at a short duration without raising
print("  generating all 31 styles at 2s …")
for i, style in enumerate(TECHNO_STYLES):
    try:
        out = generate_techno(duration_s=2, style=style, seed=i)
    except Exception as e:  # pragma: no cover — fail with the style name
        ok(False, f"style[{i}] '{style['desc']}' raised: {e}")
        continue
    ok(len(out) == 2 * SR and np.isfinite(out).all()
       and float(np.abs(out).max()) > 0.0,
       f"style[{i}] '{style['desc']}' synthesises clean 2s audio")


# --------------------------------------------------------------------------- pool_count
print("pool_count (only techno_*.wav)")

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    ok(pool_count(d / "missing") == 0, "pool_count missing dir → 0")
    ok(pool_count(d) == 0, "pool_count empty dir → 0")

    (d / "techno_1.wav").write_bytes(b"RIFF")
    (d / "techno_2.WAV").write_bytes(b"RIFF")  # case-insensitive suffix
    (d / "techno_3.mp3").write_bytes(b"ID3")   # wrong ext — ignore
    (d / "ambient_1.wav").write_bytes(b"RIFF")  # wrong stem — ignore
    (d / "notes.txt").write_text("nope")
    sub = d / "nested"
    sub.mkdir()
    (sub / "techno_nested.wav").write_bytes(b"RIFF")  # not iterated (iterdir)

    ok(pool_count(d) == 2,
       "pool_count counts only techno_*.wav (case-insensitive .wav; ignores mp3/"
       "foreign-stem/nested)")


# --------------------------------------------------------------------------- list_tracks
print("list_tracks (audio exts, sorted, shape)")

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    ok(list_tracks(d / "missing") == [], "list_tracks missing dir → []")
    ok(list_tracks(d) == [], "list_tracks empty dir → []")

    (d / "b.wav").write_bytes(b"x" * 2048)
    (d / "a.mp3").write_bytes(b"x" * 1024)
    (d / "c.m4a").write_bytes(b"x" * 512)
    (d / "z.txt").write_text("nope")
    (d / "d.flac").write_bytes(b"x")  # not in _AUDIO_EXTS

    tracks = list_tracks(d)
    names = [t["name"] for t in tracks]
    ok(names == ["a.mp3", "b.wav", "c.m4a"],
       f"list_tracks returns the three audio exts sorted by name (got {names})")
    ok(all(set(t.keys()) >= {"name", "size_kb", "created"} for t in tracks),
       "list_tracks entries carry name/size_kb/created")
    ok(tracks[0]["size_kb"] == round(1024 / 1024, 1),
       "list_tracks size_kb is round(bytes/1024, 1)")
    ok(all(isinstance(t["created"], float) for t in tracks),
       "list_tracks created is a float (st_ctime)")


# --------------------------------------------------------------------------- WAV IO
print("_write_wav + generate_and_save")

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    samples = generate_techno(duration_s=1, style=TECHNO_STYLES[0], seed=99)
    path = d / "unit.wav"
    _write_wav(samples, path)
    ok(path.is_file() and path.stat().st_size > 44, "_write_wav creates a non-trivial file")
    with wave.open(str(path), "r") as wf:
        ok(wf.getnchannels() == 1, "_write_wav is mono")
        ok(wf.getsampwidth() == 2, "_write_wav is 16-bit")
        ok(wf.getframerate() == SR, "_write_wav sample rate is SR (44100)")
        ok(wf.getnframes() == len(samples), "_write_wav frame count matches samples")
        raw = wf.readframes(wf.getnframes())
    # first frame is a signed int16 — just prove it's decodable PCM
    first = struct.unpack_from("<h", raw, 0)[0]
    ok(isinstance(first, int), "_write_wav payload is little-endian int16 PCM")

    # generate_and_save: creates techno_<ms>.wav under bgm_dir, mkdir -p
    nested = d / "pool" / "sub"
    out = generate_and_save("ignored-prompt", nested, duration_s=1)
    ok(out.parent == nested, "generate_and_save writes under the given bgm_dir")
    ok(out.name.startswith("techno_") and out.suffix == ".wav",
       f"generate_and_save names the file techno_<slug>.wav (got {out.name})")
    ok(out.is_file() and out.stat().st_size > 44,
       "generate_and_save produces a non-trivial WAV")
    with wave.open(str(out), "r") as wf:
        ok(wf.getnchannels() == 1 and wf.getframerate() == SR,
           "generate_and_save WAV is mono @ SR")


# --------------------------------------------------------------------------- replenish
print("replenish (at-target no-op / below-target generates deficit / error path)")

_orig_bgm = settings.bgm_dir
_orig_target = settings.bgm_pool_target
_orig_scope = music_gen.session_scope
_orig_gen = music_gen.generate_and_save

try:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        settings.bgm_dir = str(d)
        settings.bgm_pool_target = 3

        # at-or-above target → 0, no files written
        for i in range(3):
            (d / f"techno_{i}.wav").write_bytes(b"RIFF....")
        session = _fresh_session()
        music_gen.session_scope = lambda: _scoped(session)
        n = replenish()
        ok(n == 0, "replenish at target returns 0")
        ok(pool_count(d) == 3, "replenish at target writes no new tracks")
        runs = session.exec(select(JobRun)).all()
        ok(len(runs) == 0, "replenish at target writes no JobRun")

        # below target by 2 → generates exactly 2 (stub generate_and_save so
        # we don't wait on real synthesis, but prove the deficit arithmetic +
        # the success log). Filenames use a monotonic counter so later calls
        # never clobber earlier ones (pool_count would otherwise under-count).
        (d / "techno_0.wav").unlink()
        (d / "techno_1.wav").unlink()
        # one real techno_ left → need 2 to hit target 3
        ok(pool_count(d) == 1, "precondition: pool is 1 against target 3")

        gen_seq = {"n": 0}
        calls = []

        def _fake_gen(prompt, bgm_dir, duration_s=30):
            calls.append(prompt)
            gen_seq["n"] += 1
            p = Path(bgm_dir) / f"techno_gen_{gen_seq['n']}.wav"
            p.write_bytes(b"RIFF")
            return p

        music_gen.generate_and_save = _fake_gen
        session2 = _fresh_session()
        music_gen.session_scope = lambda: _scoped(session2)
        n = replenish()
        ok(n == 2, f"replenish below target generates exactly the deficit (got {n})")
        ok(len(calls) == 2, "generate_and_save called once per deficit track")
        ok(pool_count(d) == 3, "after replenish, pool_count reaches target")
        runs2 = session2.exec(select(JobRun)).all()
        ok(len(runs2) == 2, "one success JobRun per generated track")
        ok(all(r.kind == "music_gen" and r.status == "success" for r in runs2),
           "replenish success rows are kind=music_gen status=success")
        ok(all(r.detail and "bpm" in r.detail for r in runs2),
           "replenish success detail carries the style desc + bpm")

        # explicit target override ignores settings.bgm_pool_target
        calls.clear()
        n = replenish(target=5)  # pool is 3, need 2 more
        ok(n == 2 and len(calls) == 2,
           "replenish(target=N) uses the override, not settings.bgm_pool_target")
        ok(pool_count(d) == 5, "after override replenish, pool_count is 5")

        # error path: generate_and_save raises mid-loop → error JobRun, loop continues
        boom_n = {"i": 0}

        def _boom(prompt, bgm_dir, duration_s=30):
            boom_n["i"] += 1
            if boom_n["i"] == 1:
                raise RuntimeError("synth exploded")
            gen_seq["n"] += 1
            p = Path(bgm_dir) / f"techno_ok_{gen_seq['n']}.wav"
            p.write_bytes(b"RIFF")
            return p

        music_gen.generate_and_save = _boom
        # pool is 5 after the override above; target 7 → need 2 (1 boom + 1 ok)
        ok(pool_count(d) == 5, "precondition: pool is 5 before error-path replenish")
        session3 = _fresh_session()
        music_gen.session_scope = lambda: _scoped(session3)
        # silence the expected ERROR log so the suite output stays clean
        log = logging.getLogger("manager.music_gen")
        prev_level = log.level
        log.setLevel(logging.CRITICAL)
        try:
            n = replenish(target=7)
        finally:
            log.setLevel(prev_level)
        ok(n == 1, f"replenish counts only successful gens when one raises (got {n})")
        runs3 = session3.exec(select(JobRun)).all()
        statuses = sorted(r.status for r in runs3)
        ok(statuses == ["error", "success"],
           f"replenish error path logs one error + one success JobRun (got {statuses})")
        err = next(r for r in runs3 if r.status == "error")
        ok(err.kind == "music_gen" and "synth exploded" in (err.detail or ""),
           "error JobRun carries the exception text")
finally:
    settings.bgm_dir = _orig_bgm
    settings.bgm_pool_target = _orig_target
    music_gen.session_scope = _orig_scope
    music_gen.generate_and_save = _orig_gen


print()
print(f"ALL {_checks} CHECKS PASSED")

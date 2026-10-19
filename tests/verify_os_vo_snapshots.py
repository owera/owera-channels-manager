"""Before/after Gate B proof on snapshots #1452, #1453, #1465.

Run: PYTHONPATH=. .venv/bin/python tests/verify_os_vo_snapshots.py

AFTER boards use the snapshot script plus whisper/`card_sync` pins of
the recorded audio (``tests/os_gate_b_real_vo.py``). No rewritten
S1453/S1465, no uniform synthetic track.

Canonical before/after (VM_FAIL live boards + whisper anchors) is
``docs/gate-b-os-before-after.md`` (written by verify_os_literal_cards).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.services import craft
from app.services.engines import storyboard
from tests.os_gate_b_real_vo import artifact_dir, snapshot_description, whisper_words

_checks = 0
ART = artifact_dir()
UPLOADS = Path("/home/ubuntu/.cursor/projects/workspace/uploads")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "os-gate-b"


def _snap_path(name: str) -> Path:
    for p in (FIXTURES / name, UPLOADS / name):
        if p.is_file():
            return p
    return FIXTURES / name


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


SNAPS = {
    1452: _snap_path("snapshot-2026-10-19_07c2.json"),
    1453: _snap_path("snapshot-2026-10-19_64c7.json"),
    1465: _snap_path("snapshot-2026-10-19_706f.json"),
}


def load_snap(path: Path) -> dict:
    raw = json.loads(path.read_text())
    cc = raw.get("creation_config")
    if isinstance(cc, str):
        raw["creation_config"] = json.loads(cc)
    return raw


def synth_words(script: str, duration: float, card_sync=None) -> list[dict]:
    """Uniform word timings. Snapshots have no WordBoundary list / VM_FAIL.md."""
    toks = [w for w in (script or "").split() if w.strip()]
    usable = max(1.0, float(duration) - craft.END_TAIL_S)
    n = max(len(toks), 1)
    step = usable / n
    words, t = [], 0.08
    for w in toks:
        words.append({"text": w.strip(".,!?:;"), "start": round(t, 3),
                      "dur": round(min(0.32, step * 0.65), 3)})
        t += step
    # Nudge cue tokens toward card_sync speech_start when present.
    notes = (card_sync or {}).get("notes") or []
    wtok = [craft._sync_toks(w.get("text")) for w in words]
    flat = []
    for i, ts in enumerate(wtok):
        for _ in ts:
            flat.append(i)
    for n in notes:
        cue = n.get("cue") or ""
        speech = n.get("speech_start")
        need = craft._sync_toks(cue)
        if speech is None or not need:
            continue
        # find first run
        pos = -1
        toks_flat = [t for ts in wtok for t in ts]
        for i in range(0, max(0, len(toks_flat) - len(need) + 1)):
            if toks_flat[i:i + len(need)] == need:
                pos = i
                break
        if pos < 0 or pos >= len(flat):
            continue
        wi = flat[pos]
        delta = float(speech) - float(words[wi]["start"])
        if abs(delta) < 0.01:
            continue
        for w in words[wi:wi + max(len(need), 1)]:
            w["start"] = round(max(0.0, float(w["start"]) + delta), 3)
    return words


def gate_of(beats, *, duration, script, words, brand, topic_id, sources=None,
            description=None):
    ctext = craft.card_text_marker(beats, script, words, brand=brand, sources=sources)
    cvo = craft.card_vo_marker(beats, script, brand=brand)
    ctime = craft.card_timing_marker(beats, words, script, duration=duration,
                                    sources=sources)
    al = craft.allowlist_required_marker(topic_id, sources)
    return craft.video_maker_gate(
        beats, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
        card_text=ctext, card_vo=cvo, duration=duration, card_timing=ctime,
        allowlist_check=al, brand=brand, script=script, words=words,
        sources=sources, description=description)


def kinds_of(g: dict) -> list[str]:
    out = []
    for k in g.get("kinds") or []:
        if isinstance(k, dict) and k.get("check"):
            out.append(k["check"])
    for r in g.get("reasons") or []:
        for name in ("blank_gap", "stale_card", "quote_before_speech",
                     "vs_card_not_allowed", "single_term_card", "card_text_not_literal",
                     "echo_card", "allowlist_required", "card_vo", "OS cards",
                     "sentence_too_long", "card_exits_early", "card_enters_early",
                     "card_overlap", "cta_after_video_end", "repeated_vo_sentence",
                     "code_read_too_short", "description_missing_source",
                     "enumeration_needs_rewrite", "beat_hold",
                     "missing_word_timings", "unspoken_card"):
            if name in r and name not in out:
                out.append(name)
    return out


def md_table(vid, topic, duration, before_beats, after_beats, g_before, g_after, words_note):
    def rows(beats, g, label):
        lines = [f"### {label}", "",
                 "| # | type | start | dur | end | text |",
                 "|---|------|------:|----:|----:|------|"]
        for i, b in enumerate(beats):
            st = float(b.get("start") or 0)
            du = float(b.get("dur") or 0)
            text = (craft.beat_screen_text(b) or b.get("text") or b.get("cue") or "")[:80]
            text = text.replace("|", "\\|")
            lines.append(f"| {i} | {b.get('type')} | {st:.2f} | {du:.2f} | {st+du:.2f} | {text} |")
        hits = g.get("reasons") or ["(none)"]
        kinds = kinds_of(g)
        lines += ["", f"**Gate B:** `{g['checks'].get('B')}` / result `{g.get('result')}`",
                  "", f"**kinds:** {', '.join(kinds) or '(none)'}", "",
                  "**reasons:**", ""]
        for h in hits:
            lines.append(f"- {h}")
        lines.append("")
        return lines

    lines = [f"# #{vid} (topic {topic}, duration {duration:.2f}s)", "",
             words_note, ""]
    lines += rows(before_beats, g_before, "BEFORE (snapshot / current main)")
    lines += rows(after_beats, g_after, "AFTER (this branch, OS VO-sentence cards)")
    return "\n".join(lines)


print("snapshots have no word timings")
ok(not any((UPLOADS / n).exists() for n in ("VM_FAIL.md", "vm_fail.md")),
   "no VM_FAIL.md in the upload set")

summaries = []
for vid, path in SNAPS.items():
    raw = load_snap(path)
    cc = raw["creation_config"]
    script = raw["script"]
    subject = raw.get("title") or raw.get("subject")
    topic_id = raw["topic_id"]
    duration = float(cc["duration"])
    before = cc["beats"]
    words = whisper_words(vid)
    has_wb = any(isinstance(w, dict) and "start" in w
                 for w in (cc.get("words") or []))
    ok(not has_wb, f"#{vid}: snapshot creation_config has no TTS words")
    note = ("Word timings from VM_FAIL whisper phrase spans + snapshot "
            "`card_sync.speech_start` of the recorded audio. Script is the "
            "snapshot VO (not a rewritten copy).")

    g_before = gate_of(before, duration=duration, script=script, words=words,
                       brand="os", topic_id=topic_id, sources=None,
                       description=snapshot_description(vid))

    html = storyboard.compose(
        subject=subject, script=script, words=words, duration=duration,
        resolution="portrait", width=1080, height=1920, topic_id=topic_id,
        content_format="short", language="English",
        llm=lambda *a, **k: (_ for _ in ()).throw(AssertionError("AFTER must not call LLM")),
        brand="os", topic_name="Agent traps" if topic_id == 47 else "CrewAI")
    ok(html, f"#{vid} AFTER compose returns HTML")
    after = craft.beats_from_html(html)
    types = [b.get("type") for b in after]
    ok(not ({"compare", "term_define", "list"} & set(types)),
       f"#{vid} AFTER has no banned templates ({types})")
    g_after = gate_of(after, duration=duration, script=script, words=words,
                      brand="os", topic_id=topic_id, sources=None,
                      description=snapshot_description(vid))

    md = md_table(vid, topic_id, duration, before, after, g_before, g_after, note)
    out = ART / f"gate-b-{vid}-before-after.md"
    out.write_text(md)
    print(f"  wrote {out}")

    summaries.append({
        "id": vid, "topic": topic_id, "duration": duration,
        "before_types": [b.get("type") for b in before],
        "after_types": types,
        "before_B": g_before["checks"]["B"], "after_B": g_after["checks"]["B"],
        "before_kinds": kinds_of(g_before), "after_kinds": kinds_of(g_after),
        "before_n": len(before), "after_n": len(after),
        "hook_before": (before[0].get("dur") if before else None),
        "hook_after": (after[0].get("dur") if after else None),
    })

# Pins that the three videos actually move the way CMO asked
s1452 = next(s for s in summaries if s["id"] == 1452)
s1453 = next(s for s in summaries if s["id"] == 1453)
s1465 = next(s for s in summaries if s["id"] == 1465)
ok("vs_card_not_allowed" in s1452["before_kinds"] or "os_cards" in s1452["before_kinds"]
   or s1452["before_B"] == "FAIL",
   f"#1452 BEFORE fails OS templates ({s1452['before_kinds']})")
ok("allowlist_required" in s1452["before_kinds"] or s1452["before_B"] == "FAIL",
   "#1452 topic 47 with no on_screen_allow → BEFORE Gate B FAIL")
ok("compare" not in s1452["after_types"] and "term_define" not in s1452["after_types"],
   "#1452 AFTER dropped compare/term_define")
ok("stale_card" in s1465["before_kinds"] or (s1465["hook_before"] or 0) > 5,
   f"#1465 BEFORE hook frozen ({s1465['hook_before']}s) / stale_card")
ok((s1465["hook_after"] or 99) <= 4.1,
   f"#1465 AFTER hook ≤ ~4s ({s1465['hook_after']})")

index = ART / "gate-b-before-after.md"
index.write_text(
    "# Gate B before/after — #1452, #1453, #1465\n\n"
    "AFTER uses the snapshot script + whisper/`card_sync` pins of the "
    "recorded audio (no rewritten S1453/S1465, no uniform synthetic track).\n\n"
    "| video | topic | BEFORE B | AFTER B | BEFORE kinds | AFTER kinds | hook BEFORE | hook AFTER |\n"
    "|------:|------:|:--------:|:-------:|--------------|-------------|------------:|-----------:|\n"
    + "\n".join(
        f"| #{s['id']} | {s['topic']} | {s['before_B']} | {s['after_B']} | "
        f"{', '.join(s['before_kinds']) or '—'} | {', '.join(s['after_kinds']) or '—'} | "
        f"{s['hook_before']:.2f}s | {s['hook_after']:.2f}s |"
        for s in summaries)
    + "\n\nPer-video tables: `gate-b-1452-before-after.md`, "
      "`gate-b-1453-before-after.md`, `gate-b-1465-before-after.md`.\n"
)
print(f"  wrote {index}")

print()
print(f"ALL {_checks} CHECKS PASSED")

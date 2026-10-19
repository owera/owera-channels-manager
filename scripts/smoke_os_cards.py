#!/usr/bin/env python3
"""Read-only OS mid-card smoke for a live host (no render, spend, LLM, or TTS).

With craft.OS_VO_SENTENCE_CARDS on, load one existing OS video by id, generate
the deterministic OS mid cards from its stored script, print the cards and the
Gate B result. Exit non-zero on error. Gate B FAIL is a printed result, not a
script error.

  PYTHONPATH=. uv run python scripts/smoke_os_cards.py 1452
  PYTHONPATH=. .venv/bin/python scripts/smoke_os_cards.py 1452

SQLite is opened mode=ro. The session never commits. The LLM callable raises
if the kill switch is off (so a rollback cannot spend). Word timings come
from the stored HyperFrames narration_words.json when present, otherwise
from the video's creation_config duration / card_sync (no TTS).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("video_id", type=int, help="existing OS video id")
    p.add_argument("--db", default=None,
                   help="SQLite path (default: settings.db_path). Read-only.")
    return p.parse_args(argv)


def _loads(raw):
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            die(f"invalid JSON: {e}")
    return {}


def _ro_engine(db_path: Path):
    from sqlalchemy import create_engine

    if not db_path.is_file():
        die(f"DB not found: {db_path}")
    uri = f"sqlite:///file:{db_path.resolve().as_posix()}?mode=ro&uri=true"
    return create_engine(uri, connect_args={"check_same_thread": False, "uri": True})


def _load_row(session, video_id: int):
    from app.models import Channel, Topic, Video
    from app.services.craft import brand_of

    video = session.get(Video, video_id)
    if video is None:
        die(f"video {video_id} not found")
    script = (video.script or "").strip()
    if not script:
        die(f"video {video_id} has no stored script")
    channel = session.get(Channel, video.channel_id)
    if channel is None:
        die(f"video {video_id} channel {video.channel_id} not found")
    brand = brand_of(channel.slug, channel.name, channel_id=channel.id)
    if brand != "os":
        die(f"video {video_id} is brand={brand!r} (need os)")
    topic = session.get(Topic, video.topic_id)
    return video, channel, topic, script, brand


def _words_from_job(handle: str | None):
    if not handle:
        return None
    from app.config import settings

    path = Path(settings.hyperframes_storage_dir) / handle / "narration_words.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"narration_words.json unreadable: {e}")
    if not isinstance(raw, list) or not raw:
        return None
    return raw


def _words_from_cc(script: str, cc: dict) -> list[dict]:
    """Deterministic timings from stored duration + card_sync. No TTS."""
    from app.services import craft

    toks = [w for w in script.split() if w.strip()]
    if not toks:
        die("script has no words")
    duration = float(cc.get("duration") or 0) or None
    if duration is None:
        last = None
        for n in (cc.get("card_sync") or {}).get("notes") or []:
            if isinstance(n, dict) and n.get("speech_start") is not None:
                last = float(n["speech_start"])
        if last is None:
            die("no stored word timings, duration, or card_sync — cannot time cards without TTS")
        duration = last + float(getattr(craft, "END_TAIL_S", 1.5))
    usable = max(1.0, duration - float(getattr(craft, "END_TAIL_S", 1.5)))
    step = usable / len(toks)
    words, t = [], 0.08
    for w in toks:
        words.append({"text": w.strip(".,!?:;"), "start": round(t, 3),
                      "dur": round(min(0.32, step * 0.65), 3)})
        t += step
    notes = (cc.get("card_sync") or {}).get("notes") or []
    wtok = [craft._sync_toks(w.get("text")) for w in words]
    flat = []
    for i, ts in enumerate(wtok):
        for _ in ts:
            flat.append(i)
    for n in notes:
        if not isinstance(n, dict):
            continue
        cue = n.get("cue") or ""
        speech = n.get("speech_start")
        need = craft._sync_toks(cue)
        if speech is None or not need:
            continue
        toks_flat = [t for ts in wtok for t in ts]
        pos = -1
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


def _boom(*_a, **_k):
    raise RuntimeError("smoke_os_cards: LLM called (OS_VO_SENTENCE_CARDS off or not OS)")


def _print_cards(beats) -> None:
    from app.services import craft

    print("CARDS")
    print(f"{'#':>3} {'type':<12} {'start':>7} {'dur':>6} {'end':>7}  text")
    for i, b in enumerate(beats or []):
        st = float(b.get("start") or 0)
        du = float(b.get("dur") or 0)
        text = (craft.beat_screen_text(b) or b.get("text") or b.get("cue") or "")
        text = " ".join(str(text).split())
        print(f"{i:3d} {str(b.get('type') or '?'):<12} {st:7.2f} {du:6.2f} {st + du:7.2f}  {text}")
    print()


def _print_gate(gate: dict) -> None:
    checks = gate.get("checks") or {}
    print("GATE B")
    print(f"  result: {gate.get('result')}")
    print(f"  checks: A={checks.get('A')} B={checks.get('B')} C={checks.get('C')}")
    kinds = []
    for k in gate.get("kinds") or []:
        if isinstance(k, dict) and k.get("check"):
            kinds.append(k["check"])
        elif isinstance(k, str):
            kinds.append(k)
    print(f"  kinds: {', '.join(kinds) or '(none)'}")
    print("  reasons:")
    reasons = gate.get("reasons") or []
    if not reasons:
        print("    (none)")
    for r in reasons:
        print(f"    - {r}")
    print()


def run(video_id: int, db_path: Path) -> int:
    from sqlmodel import Session

    from app.config import settings
    from app.services import craft
    from app.services.engines import storyboard

    if not craft.OS_VO_SENTENCE_CARDS:
        die("craft.OS_VO_SENTENCE_CARDS is False — kill switch is off; "
            "set it True in app/services/craft.py and restart uvicorn")
    if settings.composition_version != "storyboard":
        die(f"composition_version={settings.composition_version!r} (need storyboard)")

    engine = _ro_engine(db_path)
    with Session(engine) as session:
        video, channel, topic, script, brand = _load_row(session, video_id)
        cc = _loads(video.creation_config)
        ov = _loads(video.overrides_json)
        allow = ov.get("on_screen_allow") or cc.get("on_screen_allow")
        on_screen = ov.get("on_screen") or cc.get("on_screen")
        fmt = "long" if topic and topic.content_format == "long" else "short"
        if fmt == "long":
            die(f"video {video_id} is long-form (OS VO-sentence cards are shorts-only)")
        stored = _words_from_job(video.mpt_task_id)
        if stored:
            words, words_src = stored, f"hyperframes/{video.mpt_task_id}/narration_words.json"
        else:
            words, words_src = _words_from_cc(script, cc), "creation_config duration/card_sync"
        duration = float(cc.get("duration") or 0)
        if duration <= 0:
            last = max((float(w.get("start") or 0) + float(w.get("dur") or 0))
                       for w in words)
            duration = last + float(getattr(craft, "END_TAIL_S", 1.5))
        resolution = cc.get("resolution") or "portrait"
        width, height = (1080, 1920) if resolution == "portrait" else (1920, 1080)

        print(f"# smoke_os_cards video={video_id} brand={brand} "
              f"topic={video.topic_id} flag=OS_VO_SENTENCE_CARDS")
        print(f"# db={db_path} (sqlite mode=ro, no writes)")
        print(f"# words={words_src}  duration={duration:.2f}s  "
              f"script_words={len(script.split())}")
        print(f"# subject={video.subject}")
        print()

        html = storyboard.compose(
            subject=video.subject, script=script, words=words, duration=duration,
            resolution=resolution, width=width, height=height,
            topic_id=video.topic_id, content_format=fmt,
            allowed_types=list(settings.composition_beat_types),
            language="English", llm=_boom, brand=brand,
            channel_id=channel.id, channel_slug=channel.slug,
            topic_name=(topic.name if topic else None),
            on_screen_allow=allow, on_screen=on_screen,
        )
        if not html:
            die("storyboard.compose returned empty (OS VO-sentence path failed)")
        beats = craft.beats_from_html(html)
        if not beats:
            die("no beats in composed HTML")

        sources = craft.on_screen_allow(allow)
        ctext = craft.card_text_marker(beats, script, words, brand=brand,
                                       sources=sources or None)
        cvo = craft.card_vo_marker(beats, script, brand=brand)
        ctime = craft.card_timing_marker(beats, words, script, duration=duration,
                                         sources=sources or None)
        al = craft.allowlist_required_marker(video.topic_id, allow)
        sync = craft.card_sync_marker(beats, words, fmt, script=script)
        cli = craft.cli_check_marker(beats, script, title=video.subject,
                                     topic_name=(topic.name if topic else None),
                                     content_format=fmt, topic_id=video.topic_id,
                                     sources=allow)
        gate = craft.video_maker_gate(
            beats, content_format=fmt, beat_timing=craft.BEAT_TIMING_CURRENT,
            card_sync=sync, cli_check=cli, card_text=ctext, card_vo=cvo,
            duration=duration, card_timing=ctime, allowlist_check=al,
            brand=brand, script=script, words=words, sources=allow,
            description=video.description,
        )
        _print_cards(beats)
        _print_gate(gate)
        print(json.dumps({
            "video_id": video_id,
            "brand": brand,
            "topic_id": video.topic_id,
            "os_vo_sentence_cards": True,
            "words_source": words_src,
            "duration": duration,
            "cards": [
                {"i": i, "type": b.get("type"), "start": b.get("start"),
                 "dur": b.get("dur"),
                 "text": craft.beat_screen_text(b) or b.get("text") or b.get("cue")}
                for i, b in enumerate(beats)
            ],
            "gate": {
                "result": gate.get("result"),
                "checks": gate.get("checks"),
                "kinds": [k.get("check") if isinstance(k, dict) else k
                          for k in (gate.get("kinds") or [])],
                "reasons": gate.get("reasons") or [],
            },
        }, ensure_ascii=False))
        return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.db:
        os.environ["MANAGER_DB_PATH"] = str(Path(args.db).resolve())
    from app.config import settings

    db = Path(args.db).resolve() if args.db else Path(settings.db_path)
    try:
        return run(args.video_id, db)
    except SystemExit:
        raise
    except Exception as e:
        die(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())

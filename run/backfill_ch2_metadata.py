"""Backfill PT-BR metadata onto ch2's pre-Phase-1 back catalog (backlog #14).

The ~115 rodrigo-recio videos published before the Subscriber Offensive language
fix (9c9a8f7, live 2026-07-09..11) went up with no `defaultLanguage` /
`defaultAudioLanguage` tags and no localized subscribe-CTA/links block — the
channel's best-performing assets are the least discoverable in PT. A few also
carry genuinely EN titles/captions from the hardcoded en-US era.

Two-step, operator-in-the-loop (the real run touches live published videos, so
it is deliberately NOT autonomous):

  1. Plan (default, READ-ONLY — no DB writes, no YouTube mutations):
       PYTHONPATH=. uv run python run/backfill_ch2_metadata.py [--top 20]
     Ranks candidates by latest measured views, fetches their LIVE snippets
     (videos.list, 1 quota unit per 50), decides per video:
       - retag_only   (default): KEEP the proven live title/tags — only add the
                      pt-BR language tags and the localized CTA/links block
                      (+ chapters for long-form, when derivable).
       - regenerate:  live title+description show no PT signal at all — propose
                      fresh PT-BR title/description/tags from the stored script.
     Writes the full proposal to a plan file the operator reviews (edit
     `proposed` fields freely; set "skip": true to drop an entry).

  2. Apply (operator-gated):
       PYTHONPATH=. uv run python run/backfill_ch2_metadata.py --apply [--limit N]
     Replays the REVIEWED plan file: re-fetches each live snippet, skips on
     title/description drift, repairs the records (no second update) when the
     live video is already localized (crash resume / manual fix), merges the
     proposal into the live snippet (categoryId etc. preserved) and calls
     videos().update (50 units each). Hard daily cap of
     10 updates (Pacific quota day), JobRun-logged, DB rows synced, each entry
     stamped applied_at in the plan file as it lands.

The plan pre-filters on the DB description missing the sub_confirmation marker
(descriptions are written at publish time, so DB == what was uploaded); the
live snippet is still re-checked and already-localized videos drop out.
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlmodel import Session, func, select  # noqa: E402

from app.models import (Channel, JobRun, OAuthStatus, Playlist, Topic, Video,  # noqa: E402
                        VideoMetric, VideoStatus, utcnow)
from app.services import chapters, metadata, quota, video_gen  # noqa: E402
from app.services.metadata import _SUB_CONFIRM_MARKER  # noqa: E402

DEFAULT_CHANNEL = "rodrigo-recio"
DEFAULT_TOP = 20
DAILY_CAP = 10                      # videos().update calls per Pacific quota day
QUOTA_VIDEO_UPDATE = 50             # videos.update cost (units)
DETAIL_PREFIX = "backfill-metadata"  # JobRun detail prefix: cap counting + forensics
DEFAULT_PLAN_FILE = REPO / "run" / "agent-reports" / "backfill-ch2-metadata-plan.json"

# Minimal PT signal: common Portuguese function words (minus EN homographs like
# "do"/"no"/"so"/"era") + diacritics. Used only to decide retag_only vs
# regenerate; the operator reviews the plan either way.
_PT_STOPWORDS = {
    "de", "da", "dos", "das", "que", "para", "pra", "voce", "nao", "com",
    "sem", "seu", "sua", "seus", "suas", "mais", "como", "por", "uma", "um",
    "na", "nos", "nas", "em", "se", "ao", "aos", "pelo", "pela", "esse", "essa",
    "isso", "esta", "ja", "tambem", "tudo", "todo", "toda", "quando", "onde",
    "porque", "muito", "bem", "vai", "ver", "fazer", "tem", "ser", "foi",
    "antes", "depois", "hoje", "cada", "entre", "mesmo", "assim",
    "entao", "ainda", "veja", "olha", "fica", "deixa", "salve",
}
_PT_DIACRITICS = set("áàâãéêíóôõúüç")


def looks_en(text: str) -> bool:
    """True when the text shows no Portuguese signal: no PT diacritics and fewer
    than two PT-stopword hits (two, so a stray EN/PT homograph can't flip it).
    Empty text is NOT flagged (conservative: keep, don't rewrite)."""
    t = (text or "").lower()
    if any(c in _PT_DIACRITICS for c in t):
        return False
    words = re.findall(r"[a-z]+", unicodedata.normalize("NFKD", t)
                       .encode("ascii", "ignore").decode())
    if not words:
        return False
    return sum(1 for w in words if w in _PT_STOPWORDS) < 2


def merge_snippet(live_snippet: dict, proposed: dict) -> dict:
    """The videos().update body snippet: the LIVE snippet (categoryId and every
    other field preserved) with only our fields overwritten, upload-body caps
    applied (title 100 / description 5000 / tags 30)."""
    snippet = dict(live_snippet)
    snippet["title"] = (proposed["title"] or "")[:100]
    snippet["description"] = (proposed["description"] or "")[:5000]
    snippet["tags"] = (proposed.get("tags") or [])[:30]
    snippet["defaultLanguage"] = proposed["defaultLanguage"]
    snippet["defaultAudioLanguage"] = proposed["defaultAudioLanguage"]
    return snippet


def select_candidates(session: Session, channel: Channel, top: int) -> list[dict]:
    """Published videos still missing the CTA/links block, ranked by latest
    measured views desc. Returns [{video, content_format, views}]."""
    rows = session.exec(
        select(Video, Topic.content_format)
        .join(Topic, Topic.id == Video.topic_id)
        .where(Video.channel_id == channel.id,
               Video.status == VideoStatus.PUBLISHED,
               Video.yt_video_id.is_not(None))  # type: ignore[union-attr]
    ).all()
    rows = [(v, fmt) for v, fmt in rows
            if _SUB_CONFIRM_MARKER not in (v.description or "")]
    latest_views: dict[int, tuple] = {}
    for m in session.exec(select(VideoMetric).where(
            VideoMetric.video_id.in_([v.id for v, _ in rows]))):  # type: ignore[union-attr]
        cur = latest_views.get(m.video_id)
        if cur is None or m.captured_at > cur[0]:
            latest_views[m.video_id] = (m.captured_at, m.views)
    out = [{"video": v, "content_format": fmt,
            "views": latest_views.get(v.id, (None, 0))[1]}
           for v, fmt in rows]
    out.sort(key=lambda c: c["views"], reverse=True)
    return out[:top]


def fetch_live_snippets(service, yt_ids: list[str]) -> dict[str, dict]:
    """Live snippets keyed by yt video id (videos.list, 1 unit per 50 ids).
    Missing ids (deleted on YouTube) are simply absent from the result."""
    out: dict[str, dict] = {}
    for i in range(0, len(yt_ids), 50):
        chunk = yt_ids[i:i + 50]
        resp = service.videos().list(part="snippet", id=",".join(chunk),
                                     maxResults=50).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item["snippet"]
    return out


def _playlist_yt_id(session: Session, video: Video) -> str | None:
    topic = session.get(Topic, video.topic_id)
    if not topic or not topic.playlist_id:
        return None
    pl = session.get(Playlist, topic.playlist_id)
    return pl.yt_playlist_id if pl else None


def backfill_reasons(live_snippet: dict, language_code: str) -> list[str]:
    reasons = []
    want = language_code.lower()
    if (live_snippet.get("defaultAudioLanguage") or "").lower() != want:
        reasons.append("missing-audio-language")
    if (live_snippet.get("defaultLanguage") or "").lower() != want:
        reasons.append("missing-language")
    if _SUB_CONFIRM_MARKER not in (live_snippet.get("description") or ""):
        reasons.append("no-cta-block")
    if looks_en((live_snippet.get("title") or "") + " "
                + (live_snippet.get("description") or "")):
        reasons.append("en-metadata")
    return reasons


def build_plan(session: Session, channel: Channel, service, top: int,
               language_code: str, language_name: str,
               generate_fn=metadata.generate,
               chapters_fn=chapters.chapter_lines_for_video) -> dict:
    """READ-ONLY: rank candidates, fetch live snippets, and propose per-video
    changes. Everything the operator needs to review is in the returned plan."""
    candidates = select_candidates(session, channel, top)
    live = fetch_live_snippets(service, [c["video"].yt_video_id for c in candidates])
    entries = []
    for c in candidates:
        v: Video = c["video"]
        snippet = live.get(v.yt_video_id)
        if snippet is None:
            print(f"  ! #{v.id} {v.yt_video_id} not found live (deleted?) — skipping")
            continue
        reasons = backfill_reasons(snippet, language_code)
        if not reasons:
            continue  # already localized live; DB just lagged behind
        action = "regenerate" if "en-metadata" in reasons else "retag_only"
        if action == "regenerate":
            meta = generate_fn(v.subject, v.script or "",
                               content_format=c["content_format"],
                               language=language_name)
            title, base_desc, tags = meta["title"], meta["description"], meta["tags"]
        else:
            title = snippet.get("title") or ""
            base_desc = snippet.get("description") or ""
            tags = snippet.get("tags") or []
        ch_lines = chapters_fn(v) if c["content_format"] == "long" else None
        description = metadata.finalize_description(
            base_desc, language_code, channel.yt_channel_id,
            _playlist_yt_id(session, v), chapter_lines=ch_lines)
        entries.append({
            "video_id": v.id, "yt_video_id": v.yt_video_id,
            "views": c["views"], "content_format": c["content_format"],
            "action": action, "reasons": reasons,
            "current": {"title": snippet.get("title"),
                        "description": snippet.get("description"),
                        "tags": snippet.get("tags") or [],
                        "defaultLanguage": snippet.get("defaultLanguage"),
                        "defaultAudioLanguage": snippet.get("defaultAudioLanguage")},
            "proposed": {"title": title, "description": description, "tags": tags,
                         "defaultLanguage": language_code,
                         "defaultAudioLanguage": language_code},
            "skip": False, "applied_at": None,
        })
    return {"channel": channel.slug, "language_code": language_code,
            "generated_at": utcnow().isoformat(), "entries": entries}


def write_plan(plan: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    tmp.replace(path)


def applied_today(session: Session) -> int:
    """Backfill updates already applied this Pacific quota day (the 10/day cap)."""
    return session.exec(
        select(func.count(JobRun.id)).where(
            JobRun.kind == "metadata", JobRun.status == "success",
            # "': '" so repair records (DETAIL_PREFIX-repair:) never count — a
            # repair performs no videos().update, so it must not burn a cap slot
            JobRun.detail.like(f"{DETAIL_PREFIX}: %"),  # type: ignore[union-attr]
            JobRun.created_at >= quota._quota_day_start())
    ).one()


def apply_plan(session: Session, channel: Channel, service, plan: dict,
               limit: int, plan_path: Path | None = None) -> int:
    """Apply reviewed entries: re-fetch live, skip on drift, merge + update,
    sync the DB row, JobRun-log, stamp applied_at (persisted to plan_path after
    every entry, so a crash mid-run never loses track of what already went
    live). Returns how many were applied."""

    def _persist():
        if plan_path is not None:
            write_plan(plan, plan_path)

    cap_left = DAILY_CAP - applied_today(session)
    if cap_left <= 0:
        print(f"daily cap reached ({DAILY_CAP}/{DAILY_CAP} this quota day) — "
              "run again after Pacific midnight")
        return 0
    budget = max(0, min(limit, cap_left))
    pending = [e for e in plan["entries"] if not e.get("applied_at") and not e.get("skip")]
    print(f"{len(pending)} pending entries; applying up to {budget} "
          f"(cap leaves {cap_left} today)")

    def _sync_db(e, title, description, tags):
        v = session.get(Video, e["video_id"])
        if v is not None:
            v.title = title
            v.description = description
            v.tags_json = json.dumps(tags)
            v.updated_at = utcnow()
            session.add(v)

    applied = 0
    for e in pending[:budget]:
        live = fetch_live_snippets(service, [e["yt_video_id"]]).get(e["yt_video_id"])
        if live is None:
            print(f"  ! #{e['video_id']} {e['yt_video_id']} gone live — skipping")
            e["skip"] = True
            _persist()
            continue
        if not backfill_reasons(live, e["proposed"]["defaultAudioLanguage"]):
            # Live is already fully localized: a prior run applied this entry but
            # crashed before recording it (or someone fixed the video by hand).
            # Repair the records from the live state — no second update call, and
            # the "-repair" detail keeps it off the daily-cap counter.
            _sync_db(e, live.get("title"), live.get("description"),
                     live.get("tags") or [])
            quota.log(session, kind="metadata", status="success",
                      video_id=e["video_id"], channel_id=channel.id,
                      detail=f"{DETAIL_PREFIX}-repair: already live "
                             f"'{(live.get('title') or '')[:60]}'",
                      quota_cost=1)
            session.commit()
            e["applied_at"] = utcnow().isoformat()
            _persist()
            print(f"  = #{e['video_id']} {e['yt_video_id']} already localized live "
                  "— records repaired, no update spent")
            continue
        if ((live.get("title") or "") != (e["current"]["title"] or "")
                or (live.get("description") or "") != (e["current"]["description"] or "")):
            print(f"  ! #{e['video_id']} live title/description drifted since plan "
                  "— skipping (re-run plan mode to refresh)")
            e["skip"] = True
            _persist()
            continue
        body = {"id": e["yt_video_id"], "snippet": merge_snippet(live, e["proposed"])}
        service.videos().update(part="snippet", body=body).execute()
        _sync_db(e, e["proposed"]["title"], e["proposed"]["description"],
                 e["proposed"]["tags"])
        quota.log(session, kind="metadata", status="success",
                  video_id=e["video_id"], channel_id=channel.id,
                  detail=f"{DETAIL_PREFIX}: {e['action']} "
                         f"'{(e['proposed']['title'] or '')[:60]}'",
                  quota_cost=QUOTA_VIDEO_UPDATE + 1)  # +1 for the drift-check list
        session.commit()
        e["applied_at"] = utcnow().isoformat()
        _persist()
        applied += 1
        print(f"  ✓ #{e['video_id']} {e['yt_video_id']} {e['action']} "
              f"({e['views']}v) '{e['proposed']['title'][:60]}'")
    return applied


def _print_plan_summary(plan: dict) -> None:
    print(f"\nplan: {len(plan['entries'])} videos → language "
          f"{plan['language_code']} (channel {plan['channel']})")
    for e in plan["entries"]:
        print(f"  #{e['video_id']:>4} {e['yt_video_id']} {e['views']:>5}v "
              f"{e['action']:<11} {','.join(e['reasons'])}")
        if e["action"] == "regenerate":
            print(f"        title: {e['current']['title']!r}")
            print(f"           -> {e['proposed']['title']!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="plan mode: how many top-viewed candidates to consider")
    ap.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_FILE)
    ap.add_argument("--apply", action="store_true",
                    help="apply the reviewed plan file (mutates LIVE videos)")
    ap.add_argument("--limit", type=int, default=DAILY_CAP,
                    help=f"apply mode: max updates this run (cap {DAILY_CAP}/day)")
    args = ap.parse_args(argv)

    from app.db import engine
    from app.services import youtube
    with Session(engine) as session:
        channel = session.exec(select(Channel).where(Channel.slug == args.channel)).first()
        if not channel:
            print(f"no channel with slug {args.channel!r}")
            return 2
        if channel.oauth_status != OAuthStatus.CONNECTED:
            print(f"channel {args.channel} is {channel.oauth_status}, not connected — "
                  "reconnect first")
            return 2
        language_code = video_gen.channel_language_code(session, channel.id)
        language_name = video_gen.channel_language(session, channel.id)
        if not language_code or language_code.lower().startswith("en"):
            print(f"channel voice language is {language_code!r} — nothing to localize; "
                  "refusing")
            return 2
        service = youtube.get_service(args.channel)

        if args.apply:
            if not args.plan_file.is_file():
                print(f"plan file {args.plan_file} not found — run plan mode first")
                return 2
            plan = json.loads(args.plan_file.read_text())
            if plan.get("channel") != channel.slug:
                print(f"plan file is for channel {plan.get('channel')!r}, "
                      f"not {channel.slug!r}")
                return 2
            applied = apply_plan(session, channel, service, plan, args.limit,
                                 plan_path=args.plan_file)
            write_plan(plan, args.plan_file)
            remaining = [e for e in plan["entries"]
                         if not e.get("applied_at") and not e.get("skip")]
            print(f"applied {applied}; {len(remaining)} still pending in {args.plan_file}")
            return 0

        plan = build_plan(session, channel, service, args.top,
                          language_code, language_name)
        write_plan(plan, args.plan_file)
        _print_plan_summary(plan)
        print(f"\nwrote {args.plan_file}\nreview it (edit `proposed`, set skip:true "
              f"to drop), then: PYTHONPATH=. uv run python "
              f"run/backfill_ch2_metadata.py --apply")
        return 0


if __name__ == "__main__":
    sys.exit(main())

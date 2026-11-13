"""Auto-produce draft-policy dry-run: which drafts would be skipped / kept now.

Usage (on a COPY of manager.db; nothing is written, the session rolls back):
    PYTHONPATH=. uv run python scripts/dryrun_autoproduce_policy.py /path/to/copy.db
No TTS: the spoken-by measurement is disabled (word count + age only).
"""
import sys

from sqlmodel import Session, create_engine, select

from app.models import Channel, Topic, Video, VideoStatus
from app.services import autoproduce_policy as ap
from app.services.render_loop import _hold_invalid_subject


def no_tts(head, voice):
    raise RuntimeError("dry-run: no TTS (word count only)")


eng = create_engine(f"sqlite:///file:{sys.argv[1]}?mode=ro&uri=true")
with Session(eng) as s:
    for ch in s.exec(select(Channel)).all():
        rows = s.exec(select(Video, Topic).join(Topic, Topic.id == Video.topic_id)
                      .where(Video.channel_id == ch.id, Video.status == VideoStatus.DRAFT,
                             Topic.active == True, Topic.weight > 0)  # noqa: E712
                      .order_by(Video.position, Video.id)).all()
        rows = [(v, t) for v, t in rows if not _hold_invalid_subject(s, v, ch)]
        pol = ap.policy_for(ch)
        kept, skipped = ap.filter_rows(s, ch, rows, measure=no_tts, apply=False)
        print(f"channel {ch.id} {ch.slug} paused={ch.paused} policy={pol and pol['key']} "
              f"drafts={len(rows)} skip={len(skipped)} keep={len(kept)}")
        for v, r in skipped:
            print(f"   SKIP #{v.id} {v.created_at:%Y-%m-%d} {ap.claim_of(v)!r}: {r}")
        for v, _t in kept:
            print(f"   KEEP #{v.id} {v.created_at:%Y-%m-%d} {ap.claim_of(v)!r}")
        s.rollback()

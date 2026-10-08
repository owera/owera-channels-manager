"""SQLite engine + session helpers."""

from contextlib import contextmanager
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app import models  # noqa: F401  (ensure tables are registered)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


# The default look/voice, as a concrete editable profile (mirrors the engine
# fallbacks so behavior is unchanged, but now visible & editable in the UI).
DEFAULT_PROFILE_NAME = "Default"
DEFAULT_PROFILE_PARAMS = {
    "video_aspect": "9:16", "video_language": "en-US", "video_source": "pexels",
    "voice_name": "en-US-AndrewNeural-Male", "paragraph_number": 2,
    "subtitle_enabled": True, "subtitle_position": "bottom",
    "font_size": 60, "text_fore_color": "#FFFFFF", "stroke_color": "#000000",
    "stroke_width": 1.5, "bgm_type": "random", "bgm_volume": 0.2,
}


def ensure_default_profile(session: Session) -> models.RenderProfile:
    """A shared 'Default' render profile that exists as a real, editable row."""
    import json
    p = session.exec(
        select(models.RenderProfile).where(
            models.RenderProfile.name == DEFAULT_PROFILE_NAME,
            models.RenderProfile.channel_id == None,  # noqa: E711
        )
    ).first()
    if p is None:
        p = models.RenderProfile(name=DEFAULT_PROFILE_NAME, channel_id=None,
                                 params_json=json.dumps(DEFAULT_PROFILE_PARAMS))
        session.add(p)
        session.commit()
        session.refresh(p)
    return p


def _add_missing_columns() -> None:
    """SQLModel.create_all never ALTERs existing tables, so add columns introduced
    after a table was first created. Idempotent; safe to run on every startup."""
    from sqlalchemy import text

    wanted = {
        "renderprofile": [("engine", "VARCHAR DEFAULT 'mpt'")],
        "video": [("engine", "VARCHAR"), ("creation_config", "VARCHAR"),
                  ("craft_review", "VARCHAR DEFAULT 'pending'"),
                  ("held", "BOOLEAN DEFAULT 0"), ("held_at", "DATETIME"),
                  ("measurement_streak", "INTEGER DEFAULT 0")],
        "videometric": [("average_view_duration", "FLOAT DEFAULT 0"),
                        ("traffic_json", "VARCHAR")],
        "channel": [("cooldown_until", "DATETIME"),
                    ("publish_windows", "VARCHAR"),
                    ("publish_tz", "VARCHAR")],
        "topic": [("content_format", "VARCHAR DEFAULT 'short'"),
                  ("weight", "INTEGER DEFAULT 1")],
        "settings": [("topic_autogen_enabled", "BOOLEAN DEFAULT 0"),
                     ("topic_autogen_min_pending", "INTEGER DEFAULT 3"),
                     ("topic_autogen_target", "INTEGER DEFAULT 6")],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))


# VideoMetric columns that may hold NULL ("not reported yet", P1 2026-09-29).
# Tables created before that have them NOT NULL; SQLite cannot drop NOT NULL
# with ALTER, so the table is rebuilt once (same rows, same ids, same indexes).
_VIDEOMETRIC_NULLABLE = ("views", "impressions", "ctr", "avg_view_pct",
                         "watch_time_minutes", "average_view_duration", "likes",
                         "comments", "subscribers_gained")


def _relax_videometric_notnull() -> bool:
    """Rebuild ``videometric`` if any metric column is still NOT NULL.
    Idempotent (no-op once relaxed); one transaction. Returns True if rebuilt."""
    from sqlalchemy import text

    with engine.begin() as conn:
        info = list(conn.execute(text("PRAGMA table_info(videometric)")))
        if not info or not any(r[1] in _VIDEOMETRIC_NULLABLE and r[3] for r in info):
            return False
        old_cols = [r[1] for r in info]
        for idx in list(conn.execute(text("PRAGMA index_list(videometric)"))):
            if idx[3] == "c":                      # explicit CREATE INDEX (ix_…)
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx[1]}"'))
        conn.execute(text("ALTER TABLE videometric RENAME TO _videometric_pre_null"))
        table = SQLModel.metadata.tables["videometric"]
        table.create(conn)                         # new schema + its indexes
        cols = ", ".join(c for c in old_cols if c in table.c)
        conn.execute(text(f"INSERT INTO videometric ({cols}) "
                          f"SELECT {cols} FROM _videometric_pre_null"))
        conn.execute(text("DROP TABLE _videometric_pre_null"))
    return True


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _relax_videometric_notnull()
    with Session(engine) as s:
        if s.get(models.Settings, 1) is None:
            s.add(models.Settings(id=1))
            s.commit()
        ensure_default_profile(s)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """For use outside request handlers (scheduler loops)."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def app_settings(session: Session) -> models.Settings:
    obj = session.get(models.Settings, 1)
    if obj is None:
        obj = models.Settings(id=1)
        session.add(obj)
        session.commit()
        session.refresh(obj)
    return obj

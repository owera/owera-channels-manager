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
                  ("measurement_streak", "INTEGER DEFAULT 0"),
                  ("approved_by", "VARCHAR")],
        "videometric": [("average_view_duration", "FLOAT DEFAULT 0"),
                        ("traffic_json", "VARCHAR")],
        "channel": [("cooldown_until", "DATETIME"),
                    ("publish_windows", "VARCHAR"),
                    ("publish_tz", "VARCHAR"),
                    ("workspace_id", "INTEGER DEFAULT 1"),
                    ("credential_source", "VARCHAR DEFAULT 'legacy_file'"),
                    ("niche", "VARCHAR"),
                    ("is_test", "BOOLEAN DEFAULT 0"),
                    ("access_lost_at", "DATETIME"),
                    ("access_lost_reason", "VARCHAR"),
                    ("youtube_data_purged_at", "DATETIME"),
                    ("oauth_status_changed_at", "DATETIME"),
                    ("last_refresh_ok_at", "DATETIME"),
                    ("token_refresh_stale_at", "DATETIME")],
        "oauthpending": [("workspace_id", "INTEGER DEFAULT 1")],
        "topic": [("content_format", "VARCHAR DEFAULT 'short'"),
                  ("weight", "INTEGER DEFAULT 1")],
        "settings": [("topic_autogen_enabled", "BOOLEAN DEFAULT 0"),
                     ("topic_autogen_min_pending", "INTEGER DEFAULT 3"),
                     ("topic_autogen_target", "INTEGER DEFAULT 6")],
        "workspace": [("owner_email", "VARCHAR"),
                      ("stripe_customer_id", "VARCHAR"),
                      ("stripe_subscription_id", "VARCHAR"),
                      ("welcome_email_sent", "BOOLEAN DEFAULT 0"),
                      ("closed_at", "DATETIME"),
                      ("stripe_state_event_created", "INTEGER"),
                      ("stripe_raw_status", "VARCHAR"),
                      ("subscription_current_period_end", "DATETIME"),
                      ("subscription_cancel_at_period_end", "BOOLEAN DEFAULT 0"),
                      ("last_invoice_paid_at", "DATETIME"),
                      ("scheduler_paused", "BOOLEAN DEFAULT 0"),
                      ("is_test", "BOOLEAN DEFAULT 0"),
                      ("login_ambiguous_owner_count", "INTEGER"),
                      ("login_ambiguous_owner_last_at", "DATETIME"),
                      ("paid_at", "DATETIME"),
                      ("first_draft_at", "DATETIME")],
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


def _ensure_default_workspace(session: Session) -> None:
    ws = session.get(models.Workspace, 1)
    if ws is None:
        session.add(models.Workspace(
            id=1, slug="owera", name="Owera (legacy default workspace)"))
        session.commit()
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("UPDATE channel SET workspace_id = 1 WHERE workspace_id IS NULL"))


def _ensure_unique_stripe_subscription() -> None:
    """One workspace per Stripe subscription (partial unique index; NULLs allowed).
    Skipped with a warning if existing rows already collide, so startup never fails."""
    import logging
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError, OperationalError

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_stripe_subscription "
                "ON workspace (stripe_subscription_id) "
                "WHERE stripe_subscription_id IS NOT NULL"))
    except (IntegrityError, OperationalError) as e:
        logging.getLogger("manager.db").warning(
            "ux_workspace_stripe_subscription not created: %s", type(e).__name__)


def _null_fabricated_impressions_ctr() -> int:
    """S8a / LegalOps L4: impressions / ctr were never measured (the Analytics
    API v2 rejects them, 2026-07-09 audit), so every stored 0 is a fabricated
    default. Turn those zeros into NULL ("no data"). Only exact zeros are
    touched; idempotent (a no-op once applied). Returns rows changed."""
    from sqlalchemy import text

    with engine.begin() as conn:
        info = list(conn.execute(text("PRAGMA table_info(videometric)")))
        cols = {r[1] for r in info}
        n = 0
        if "impressions" in cols:
            n += conn.execute(text(
                "UPDATE videometric SET impressions = NULL WHERE impressions = 0")).rowcount or 0
        if "ctr" in cols:
            n += conn.execute(text(
                "UPDATE videometric SET ctr = NULL WHERE ctr = 0")).rowcount or 0
        return n


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _ensure_unique_stripe_subscription()
    _relax_videometric_notnull()
    _null_fabricated_impressions_ctr()
    with Session(engine) as s:
        _ensure_default_workspace(s)
        if s.get(models.Settings, 1) is None:
            s.add(models.Settings(id=1))
            s.commit()
        ensure_default_profile(s)


# Workspace id that owns the truly global settings (scheduler kill switch,
# render_concurrency, drip, autogen knobs). S4: only this workspace may read
# the full Settings row or PATCH it; every other workspace gets its own pause.
ADMIN_WORKSPACE_ID = 1


def paused_workspace_ids(session: Session) -> set[int]:
    """Ids of workspaces whose own automation is paused (S4 per-workspace pause).

    ``init_db`` adds the
    column before any loop starts."""
    rows = session.exec(
        select(models.Workspace.id).where(
            models.Workspace.scheduler_paused == True)  # noqa: E712
    ).all()
    return {int(r) for r in rows if r is not None}


def channel_workspace_paused(session: Session, channel, paused: set[int] | None = None) -> bool:
    """True if ``channel`` belongs to a workspace that paused its automation."""
    if channel is None:
        return False
    ids = paused if paused is not None else paused_workspace_ids(session)
    return (getattr(channel, "workspace_id", None) or ADMIN_WORKSPACE_ID) in ids


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

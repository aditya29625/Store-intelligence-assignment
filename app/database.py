"""
database.py — SQLAlchemy setup with SQLite.

Uses SQLite for zero-infra local/container deployment.
The schema is event-centric: raw events are stored as-is, and all metrics
are computed on-the-fly via SQL aggregation queries.
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/store_intelligence.db")

# SQLite needs connect_args for thread safety in FastAPI
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create all tables. Idempotent."""
    from app import models  # import ensures models are registered with Base
    Base.metadata.create_all(bind=engine)

    # Create indexes for query performance
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_events_store_ts
            ON events (store_id, timestamp)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_events_visitor
            ON events (visitor_id, store_id)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events (event_type, store_id)
        """))
        conn.commit()

"""
Database configuration and SQLAlchemy initialization.

This module defines the database connection URL, sets up the SQLAlchemy
engine for a local SQLite file, and creates the SessionLocal factory
and the declarative Base class for ORM models.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./pslocks.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def ensure_db_schema() -> None:
    """Ensures that all tables and missing columns exist in the database."""
    Base.metadata.create_all(bind=engine)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            res = conn.execute(text("PRAGMA table_info(event_logs)")).fetchall()
            cols = [r[1] for r in res]
            if cols:
                for col, col_type, default in [
                    ("direction", "VARCHAR", "'RX'"),
                    ("can_id", "VARCHAR", "NULL"),
                    ("payload", "VARCHAR", "NULL"),
                    ("corr_id", "INTEGER", "NULL"),
                ]:
                    if col not in cols:
                        conn.execute(text(f"ALTER TABLE event_logs ADD COLUMN {col} {col_type} DEFAULT {default}"))
                conn.commit()
    except Exception:
        pass

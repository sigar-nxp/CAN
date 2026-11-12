"""
Database configuration and SQLAlchemy initialization.

This module defines the database connection URL, sets up the SQLAlchemy
engine for a local SQLite file, and creates the SessionLocal factory
and the declarative Base class for ORM models.
"""

import os
from typing import Optional
from sqlalchemy import Column, String, create_engine, text
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


class SystemSetting(Base):
    """Key-value store for persistent global gateway settings."""

    __tablename__ = "system_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=False)


def ensure_db_schema(bind=None) -> None:
    """
    Ensures that all tables and missing columns exist in the database.

    Args:
        bind: Optional SQLAlchemy engine to migrate instead of the default one.
    """
    target_engine = bind if bind is not None else engine
    Base.metadata.create_all(bind=target_engine)
    try:
        with target_engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS system_settings ("
                "key VARCHAR PRIMARY KEY, "
                "value VARCHAR NOT NULL"
                ")"
            ))
            conn.commit()

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
            
            res_dev = conn.execute(text("PRAGMA table_info(devices)")).fetchall()
            cols_dev = [r[1] for r in res_dev]
            if cols_dev:
                if "hardware_uid" not in cols_dev:
                    conn.execute(text("ALTER TABLE devices ADD COLUMN hardware_uid VARCHAR NULL"))

                # Gym mode columns (offline fitness-gym free locker selection)
                for col, ddl in [
                    ("custom_name", "VARCHAR NULL"),
                    ("locker_state", "VARCHAR NOT NULL DEFAULT 'vacant'"),
                    ("assigned_card_uid", "VARCHAR NULL"),
                    ("occupied_at", "DATETIME NULL"),
                ]:
                    if col not in cols_dev:
                        conn.execute(text(f"ALTER TABLE devices ADD COLUMN {col} {ddl}"))

                # Backfill (idempotent): keep operator-chosen legacy names, replace
                # auto-generated ones with the default "Locker <id>" label.
                conn.execute(text(
                    "UPDATE devices SET custom_name = CASE "
                    "WHEN name IS NULL OR name = 'Lock' OR name = 'Lock ' || device_id "
                    "THEN 'Locker ' || device_id ELSE name END "
                    "WHERE custom_name IS NULL"
                ))
                conn.commit()
    except Exception:
        pass


def get_system_setting(key: str, default: Optional[str] = None, bind=None) -> Optional[str]:
    """Reads a persistent setting from the system_settings table."""
    target_engine = bind if bind is not None else engine
    try:
        with target_engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS system_settings ("
                "key VARCHAR PRIMARY KEY, "
                "value VARCHAR NOT NULL"
                ")"
            ))
            row = conn.execute(
                text("SELECT value FROM system_settings WHERE key = :key"),
                {"key": key},
            ).fetchone()
            if row is not None and row[0] is not None:
                return str(row[0])
    except Exception:
        pass
    return default


def set_system_setting(key: str, value: str, bind=None) -> None:
    """Writes a persistent setting to the system_settings table."""
    target_engine = bind if bind is not None else engine
    try:
        with target_engine.connect() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS system_settings ("
                "key VARCHAR PRIMARY KEY, "
                "value VARCHAR NOT NULL"
                ")"
            ))
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value) VALUES (:key, :val) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"key": key, "val": str(value)},
            )
            conn.commit()
    except Exception:
        pass



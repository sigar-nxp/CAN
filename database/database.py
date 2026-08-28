"""
Database configuration and SQLAlchemy initialization.

This module defines the database connection URL, sets up the SQLAlchemy
engine for a local SQLite file, and creates the SessionLocal factory
and the declarative Base class for ORM models.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./pslocks.db"

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
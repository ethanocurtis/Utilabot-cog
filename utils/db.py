from __future__ import annotations
import os, datetime as dt
from typing import Optional, Dict, List, Set
from sqlalchemy import create_engine, Integer, String, DateTime, ForeignKey, Float, Text, Boolean, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

# ---------------- Base & Models ----------------

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # discord user id
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())

class Balance(Base):
    __tablename__ = "balances"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    credits: Mapped[int] = mapped_column(Integer, default=0)

class Inventory(Base):
    __tablename__ = "inventory"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    item: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer, default=1)

class ShopItem(Base):
    __tablename__ = "shop_items"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    price: Mapped[int] = mapped_column(Integer)

class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    cost: Mapped[int] = mapped_column(Integer)
    hourly_yield: Mapped[int] = mapped_column(Integer)

class Ownership(Base):
    __tablename__ = "ownership"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    acquired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())
    last_payout_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())

class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[dt.datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

class Note(Base):
    __tablename__ = "notes"  # long-form notes
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())
    # NOTE: 'note_no' is added via migration below to avoid changing your ORM right now.

# ---------------- Engine Helpers ----------------

def init_engine_and_session(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, SessionLocal

# ---------------- Migrations ----------------

def _add_notes_note_no_and_index(conn) -> None:
    """Add notes.note_no and unique index (user_id, note_no) if missing."""
    # Add column if not present
    try:
        conn.exec_driver_sql("ALTER TABLE notes ADD COLUMN note_no INTEGER;")
    except Exception:
        # Column likely exists already
        pass
    # Create unique index on (user_id, note_no)
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_user_note_no ON notes(user_id, note_no);"
    )

def _backfill_note_no_compact(conn) -> None:
    """
    Assign compact per-user numbers to NULL note_no rows, filling the smallest
    available positive integers (1..N without gaps) in creation order.
    Safe to run repeatedly.
    """
    # Distinct users with at least one NULL note_no
    user_rows = conn.exec_driver_sql(
        "SELECT DISTINCT user_id FROM notes WHERE note_no IS NULL"
    ).fetchall()
    if not user_rows:
        return

    for (user_id,) in user_rows:
        # Fetch used numbers
        used_rows = conn.exec_driver_sql(
            "SELECT note_no FROM notes WHERE user_id = :u AND note_no IS NOT NULL ORDER BY note_no ASC",
            {"u": user_id},
        ).fetchall()
        used: Set[int] = {r[0] for r in used_rows if isinstance(r[0], int)}

        # Fetch rows needing assignment in a stable order (created_at then id)
        null_rows = conn.exec_driver_sql(
            "SELECT id FROM notes WHERE user_id = :u AND note_no IS NULL ORDER BY created_at ASC, id ASC",
            {"u": user_id},
        ).fetchall()

        # Assign smallest available positive integers
        next_no = 1
        for (note_pk,) in null_rows:
            while next_no in used:
                next_no += 1
            conn.exec_driver_sql(
                "UPDATE notes SET note_no = :nn WHERE id = :id",
                {"nn": next_no, "id": note_pk},
            )
            used.add(next_no)
            next_no += 1

def _create_weather_tables_and_kv(conn) -> None:
    """Ensure weather-related tables and generic user KV exist."""
    conn.exec_driver_sql("""
    CREATE TABLE IF NOT EXISTS weather_zips (
        user_id INTEGER PRIMARY KEY,
        zip TEXT NOT NULL
    );
    """)
    conn.exec_driver_sql("""
    CREATE TABLE IF NOT EXISTS weather_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        zip TEXT NOT NULL,
        cadence TEXT NOT NULL CHECK (cadence IN ('daily','weekly')),
        hh INTEGER NOT NULL,
        mi INTEGER NOT NULL,
        weekly_days INTEGER NOT NULL DEFAULT 7,
        next_run_utc TEXT NOT NULL
    );
    """)
    # small user settings KV (used by weather alerts etc.)
    conn.exec_driver_sql("""
    CREATE TABLE IF NOT EXISTS user_notes_kv (
        user_id INTEGER NOT NULL,
        k TEXT NOT NULL,
        v TEXT NOT NULL,
        PRIMARY KEY (user_id, k)
    );
    """)

def run_migrations(engine):
    # 1) Create ORM-declared tables
    Base.metadata.create_all(engine)

    # 2) Weather + KV tables
    with engine.begin() as conn:
        _create_weather_tables_and_kv(conn)

    # 3) Notes: add note_no + unique index, then backfill
    with engine.begin() as conn:
        _add_notes_note_no_and_index(conn)
        _backfill_note_no_compact(conn)

    # 4) Seed shop items & businesses (idempotent: add missing by name only)
from sqlalchemy.orm import Session
with Session(engine) as s:
    # --- Shop items ---
    shop_defaults = [
        ("Fishing Rod", 100),
        ("Bait", 5),
        ("Pickaxe", 250),
    ]
    existing_shop = {n for (n,) in s.query(ShopItem.name).all()}
    for name, price in shop_defaults:
        if name not in existing_shop:
            s.add(ShopItem(name=name, price=price))

    # --- Businesses ---
    biz_defaults = [
        # low
        ("Lemonade Stand",   5_000,        42),
        ("Food Truck",      25_000,       250),

        # medium
        ("Car Wash",        50_000,       400),
        ("Gas Station",    120_000,       900),
        ("Mini-Mart",      250_000,     1_800),

        # high
        ("Arcade",       1_000_000,     6_000),
        ("Nightclub",    2_000_000,    12_000),
        ("Solar Farm",   5_000_000,    30_000),
        ("Logistics Fleet", 8_000_000, 48_000),
        ("Data Center", 12_000_000,    90_000),
        ("Stadium Rights", 20_000_000, 150_000),
        ("Spaceport",   50_000_000,   400_000),
    ]
    # compare case-insensitively so “Car Wash” vs “car wash” counts as existing
    existing_biz = {n.lower() for (n,) in s.query(Business.name).all()}
    for name, cost, hourly in biz_defaults:
        if name.lower() not in existing_biz:
            s.add(Business(name=name, cost=cost, hourly_yield=hourly))

    s.commit()

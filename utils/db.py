from __future__ import annotations
import os, datetime as dt
from typing import Optional
from sqlalchemy import create_engine, Integer, String, DateTime, ForeignKey, Float, Text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)           # discord user id
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
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())

def init_engine_and_session(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, SessionLocal

def run_migrations(engine):
    Base.metadata.create_all(engine)
    # Seed some default shop items & businesses if empty
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        if not s.query(ShopItem).first():
            s.add_all([
                ShopItem(name="Fishing Rod", price=100),
                ShopItem(name="Bait", price=5),
                ShopItem(name="Pickaxe", price=250),
            ])
        if not s.query(Business).first():
            s.add_all([
                Business(name="Lemonade Stand", cost=500, hourly_yield=20),
                Business(name="Food Truck", cost=2500, hourly_yield=120),
                Business(name="Arcade", cost=10000, hourly_yield=600),
            ])
        s.commit()

from __future__ import annotations
"""
utils/economy_helpers.py

Drop-in helpers for cogs that need to charge or pay users using your central
`Balance` model. These functions:
  - auto-detect the numeric balance column name (amount, balance, credits, coins, value)
  - ensure a Balance row exists when needed
  - provide convenience wrappers for common economy ops

Usage in a cog:
    from utils.economy_helpers import (
        get_balance, can_afford, add_balance, charge, payout, transfer, with_session
    )

    @app_commands.command(...)
    async def somegame(self, interaction: discord.Interaction, bet: int):
        with with_session(self.bot.SessionLocal) as session:
            ensure_user(session, interaction.user.id)  # from utils.common
            if not can_afford(session, interaction.user.id, bet):
                return await interaction.response.send_message("Not enough credits.", ephemeral=True)
            charge(session, interaction.user.id, bet)  # debit
            # ...do game logic...
            payout(session, interaction.user.id, bet * 2)  # credit

If you want a dedicated "house" account, reserve user_id=0 (or any constant).
"""
from contextlib import contextmanager
from typing import Callable, Optional, Tuple

from utils.db import Balance  # type: ignore
from utils.common import ensure_user  # type: ignore


# ----------------------------
# Internal column detection
# ----------------------------

_BALANCE_FIELDS = ("amount", "balance", "credits", "coins", "value")


def _detect_field(bal: Balance) -> Optional[str]:
    for f in _BALANCE_FIELDS:
        if hasattr(bal, f):
            return f
    return None


def _ensure_balance_row(session, user_id: int) -> Tuple[Balance, str]:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        bal = Balance(user_id=user_id)
        session.add(bal)
        session.flush()
    field = _detect_field(bal)
    if not field:
        raise RuntimeError("Could not detect numeric balance column on Balance model.")
    return bal, field


# ----------------------------
# Session helper
# ----------------------------

@contextmanager
def with_session(SessionLocal: Callable[[], object]):
    """Context manager to get/close a SQLAlchemy session cleanly."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ----------------------------
# Public economy ops
# ----------------------------

def get_balance(session, user_id: int) -> int:
    bal, field = _ensure_balance_row(session, user_id)
    return int(getattr(bal, field) or 0)


def set_balance(session, user_id: int, new_amount: int) -> int:
    bal, field = _ensure_balance_row(session, user_id)
    setattr(bal, field, int(new_amount))
    session.flush()
    return int(getattr(bal, field) or 0)


def add_balance(session, user_id: int, delta: int) -> int:
    bal, field = _ensure_balance_row(session, user_id)
    current = int(getattr(bal, field) or 0)
    setattr(bal, field, current + int(delta))
    session.flush()
    return int(getattr(bal, field) or 0)


def can_afford(session, user_id: int, amount: int) -> bool:
    return get_balance(session, user_id) >= int(amount)


def charge(session, user_id: int, amount: int) -> int:
    """Debit user by amount. Raises if insufficient funds."""
    amount = int(amount)
    if amount < 0:
        raise ValueError("charge amount must be positive")
    if not can_afford(session, user_id, amount):
        raise RuntimeError("Insufficient funds")
    return add_balance(session, user_id, -amount)


def payout(session, user_id: int, amount: int) -> int:
    """Credit user by amount (no checks)."""
    amount = int(amount)
    if amount < 0:
        raise ValueError("payout amount must be positive")
    return add_balance(session, user_id, amount)


def transfer(session, sender_id: int, receiver_id: int, amount: int) -> None:
    """Move credits from sender to receiver atomically (within this session)."""
    charge(session, sender_id, amount)
    payout(session, receiver_id, amount)
    # session commit handled by caller/with_session

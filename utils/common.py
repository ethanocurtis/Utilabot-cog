import datetime as dt
from sqlalchemy.orm import Session
from .db import User, Balance

def ensure_user(session: Session, user_id: int):
    u = session.get(User, user_id)
    if not u:
        u = User(id=user_id)
        session.add(u)
    b = session.get(Balance, user_id)
    if not b:
        b = Balance(user_id=user_id, credits=0)
        session.add(b)
    return u, b

def add_credits(session: Session, user_id: int, amount: int):
    ensure_user(session, user_id)
    bal = session.get(Balance, user_id)
    bal.credits += amount
    return bal.credits

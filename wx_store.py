
from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy import text

class WxStore:
from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy import text

class WxStore:
    """
    Minimal storage adapter for the weather cog.

    Tables expected in DB:
      - weather_zips(user_id INTEGER PRIMARY KEY, zip TEXT)
      - weather_subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,
                              user_id INTEGER, zip TEXT, cadence TEXT,
                              hh INTEGER, mi INTEGER, weekly_days INTEGER,
                              next_run_utc TEXT)
      - notes(user_id INTEGER, k TEXT, v TEXT, PRIMARY KEY(user_id, k))
    """
    def __init__(self, engine):
        self.engine = engine
        # Some code in the cog calls self.store.db.execute(...)
        self.db = engine

    # ---- ZIP ----
    def get_user_zip(self, user_id: int) -> Optional[str]:
        with self.engine.connect() as c:
            row = c.execute(
                text("SELECT zip FROM weather_zips WHERE user_id=:u"),
                {"u": user_id},
            ).fetchone()
            return row[0] if row else None

    def set_user_zip(self, user_id: int, zip_code: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    """
                    INSERT INTO weather_zips(user_id, zip)
                    VALUES (:u, :z)
                    ON CONFLICT(user_id) DO UPDATE SET zip=excluded.zip
                    """
                ),
                {"u": user_id, "z": zip_code},
            )

    # ---- Subscriptions ----
    def add_weather_sub(self, sub: Dict[str, Any]) -> int:
        with self.engine.begin() as c:
            res = c.execute(
                text(
                    """
                    INSERT INTO weather_subscriptions
                        (user_id, zip, cadence, hh, mi, weekly_days, next_run_utc)
                    VALUES (:user_id, :zip, :cadence, :hh, :mi, :weekly_days, :next_run_utc)
                    """
                ),
                sub,
            )
            # SQLite specific
            return res.lastrowid  # type: ignore[attr-defined]

    def list_weather_subs(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT id, user_id, zip, cadence, hh, mi, weekly_days, next_run_utc "
            "FROM weather_subscriptions"
        )
        params: Dict[str, Any] = {}
        if user_id is not None:
            sql += " WHERE user_id=:u"
            params["u"] = user_id
        sql += " ORDER BY id ASC"

        with self.engine.connect() as c:
            rows = c.execute(text(sql), params).fetchall()

        return [
            {
                "id": r[0],
                "user_id": r[1],
                "zip": r[2],
                "cadence": r[3],
                "hh": r[4],
                "mi": r[5],
                "weekly_days": r[6],
                "next_run_utc": r[7],
            }
            for r in rows
        ]

    def update_weather_sub(self, sub_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=:{k}" for k in fields.keys())
        fields["id"] = sub_id
        with self.engine.begin() as c:
            c.execute(
                text(f"UPDATE weather_subscriptions SET {sets} WHERE id=:id"),
                fields,
            )

    def remove_weather_sub(self, sub_id: int, requester_id: int) -> bool:
        with self.engine.begin() as c:
            res = c.execute(
                text(
                    "DELETE FROM weather_subscriptions "
                    "WHERE id=:i AND user_id=:u"
                ),
                {"i": sub_id, "u": requester_id},
            )
            return res.rowcount > 0  # type: ignore[attr-defined]

    # ---- Notes (generic key/value per *user*) ----
    def get_note(self, user_id: int, key: str) -> Optional[str]:
        with self.engine.connect() as c:
            row = c.execute(
                text("SELECT v FROM notes WHERE user_id=:u AND k=:k"),
                {"u": user_id, "k": key},
            ).fetchone()
            return row[0] if row else None

    def set_note(self, user_id: int, key: str, value: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    """
                    INSERT INTO notes(user_id, k, v)
                    VALUES (:u, :k, :v)
                    ON CONFLICT(user_id, k) DO UPDATE SET v=excluded.v
                    """
                ),
                {"u": user_id, "k": key, "v": value},
            )

    # ---- Global config (stored in notes with user_id=0) ----
    CONFIG_USER: int = 0  # reserve user_id=0 for global KV

    def set_config(self, key: str, value) -> None:
        with self.engine.begin() as c:
            c.execute(
                text(
                    """
                    INSERT INTO notes(user_id, k, v)
                    VALUES (:u, :k, :v)
                    ON CONFLICT(user_id, k) DO UPDATE SET v=excluded.v
                    """
                ),
                {"u": self.CONFIG_USER, "k": str(key), "v": str(value)},
            )

    def get_config(self, key: str) -> Optional[str]:
        with self.engine.connect() as c:
            row = c.execute(
                text("SELECT v FROM notes WHERE user_id=:u AND k=:k"),
                {"u": self.CONFIG_USER, "k": str(key)},
            ).fetchone()
            return row[0] if row else None

    def delete_config(self, key: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                text("DELETE FROM notes WHERE user_id=:u AND k=:k"),
                {"u": self.CONFIG_USER, "k": str(key)},
            )

    def get_config_all(self) -> Dict[str, str]:
        with self.engine.connect() as c:
            rows = c.execute(
                text("SELECT k, v FROM notes WHERE user_id=:u"),
                {"u": self.CONFIG_USER},
            ).fetchall()
        return {str(k): str(v) for (k, v) in rows}

    # ---- Autodelete wrappers (used by the moderation cog) ----
    def set_autodelete(self, channel_id: int, seconds: int) -> None:
        self.set_config(f"autodelete:{int(channel_id)}", int(seconds))

    def remove_autodelete(self, channel_id: int) -> None:
        self.delete_config(f"autodelete:{int(channel_id)}")

    def get_autodelete(self) -> Dict[str, int]:
        raw = self.get_config_all()
        out: Dict[str, int] = {}
        for k, v in raw.items():
            if k.startswith("autodelete:"):
                cid = k.split(":", 1)[1]
                try:
                    out[str(int(cid))] = int(float(v))
                except Exception:
                    continue
        return out

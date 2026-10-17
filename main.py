
import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", "standpoint.db")
RESULTS_ADMIN_TOKEN = os.getenv("RESULTS_ADMIN_TOKEN", "")

app = FastAPI(title="STANDPOINT Matchmaking API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                game_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                telegram_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                joined_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                players_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Миграции: безопасно добавляем поля в существующую базу.
        player_columns = {
            r["name"] for r in con.execute("PRAGMA table_info(players)")
        }
        additions = {
            "elo": "INTEGER NOT NULL DEFAULT 1000",
            "wins": "INTEGER NOT NULL DEFAULT 0",
            "losses": "INTEGER NOT NULL DEFAULT 0",
            "draws": "INTEGER NOT NULL DEFAULT 0",
            "premium_until": "TEXT",
        }
        for name, definition in additions.items():
            if name not in player_columns:
                con.execute(
                    f"ALTER TABLE players ADD COLUMN {name} {definition}"
                )

        match_columns = {
            r["name"] for r in con.execute("PRAGMA table_info(matches)")
        }
        if "rated" not in match_columns:
            con.execute(
                "ALTER TABLE matches ADD COLUMN rated INTEGER NOT NULL DEFAULT 0"
            )


init_db()


class RegisterPlayer(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    nickname: str = Field(default="Игрок", min_length=1, max_length=64)
    game_id: str = Field(min_length=1, max_length=64)


class JoinQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="5v5", min_length=1, max_length=16)


class LeaveQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)


class MatchResult(BaseModel):
    # winner: "team1", "team2" или "draw"
    winner: Optional[str] = None
    result: Optional[Any] = None
    telegram_id: Optional[str] = None


def get_division(elo: int) -> str:
    if elo >= 2400:
        return "Элита"
    if elo >= 2100:
        return "Мастер"
    if elo >= 1800:
        return "Алмаз"
    if elo >= 1500:
        return "Платина"
    if elo >= 1200:
        return "Золото"
    return "Бронза"


def public_player(row):
    p = dict(row)
    p["division"] = get_division(p.get("elo", 1000))
    p["premium_active"] = bool(
        p.get("premium_until")
        and p["premium_until"] > now()
    )
    return p


@app.get("/")
def root():
    return {
        "service": "STANDPOINT Matchmaking API",
        "status": "ok",
        "version": "2.0.0",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/players/register")
def register_player(payload: RegisterPlayer):
    telegram_id = payload.telegram_id.strip()
    nickname = payload.nickname.strip() or "Игрок"
    game_id = payload.game_id.strip()

    if not telegram_id or not game_id:
        raise HTTPException(400, "Укажи Telegram ID и игровой ID")

    with db() as con:
        con.execute("""
            INSERT INTO players
                (telegram_id, nickname, game_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                nickname=excluded.nickname,
                game_id=excluded.game_id,
                updated_at=excluded.updated_at
        """, (telegram_id, nickname, game_id, now(), now()))

        row = con.execute(
            "SELECT * FROM players WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()

    return {"status": "registered", "player": public_player(row)}


@app.get("/players/{telegram_id}")
def get_player(telegram_id: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM players WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "Игрок не зарегистрирован")

    return public_player(row)


@app.get("/players/{telegram_id}/history")
def player_history(telegram_id: str, limit: int = 20):
    limit = max(1, min(limit, 100))

    with db() as con:
        player = con.execute(
            "SELECT telegram_id FROM players WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()

        if not player:
            raise HTTPException(404, "Игрок не зарегистрирован")

        rows = con.execute("""
            SELECT match_id, mode, status, players_json,
                   result_json, created_at, rated
            FROM matches
            WHERE status='finished'
              AND players_json LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (f'%"{telegram_id}"%', limit)).fetchall()

    history = []
    for row in rows:
        item = dict(row)
        item["players"] = json.loads(item.pop("players_json"))
        item["result"] = (
            json.loads(item["result_json"])
            if item.get("result_json") else None
        )
        item.pop("result_json", None)
        history.append(item)

    return {"telegram_id": telegram_id, "matches": history}


@app.get("/leaderboard")
def leaderboard(limit: int = 50):
    limit = max(1, min(limit, 100))

    with db() as con:
        rows = con.execute("""
            SELECT telegram_id, nickname, game_id, elo, wins, losses, draws
            FROM players
            ORDER BY elo DESC, wins DESC
            LIMIT ?
        """, (limit,)).fetchall()

    players = []
    for index, row in enumerate(rows, start=1):
        item = dict(row)
        item["rank"] = index
        item["division"] = get_division(item["elo"])
        players.append(item)

    return {"players": players, "count": len(players)}


@app.post("/queue/join")
def join_queue(payload: JoinQueue):
    mode = payload.mode.strip().lower()

    if mode not in {"1v1", "2v2", "3v3", "5v5"}:
        raise HTTPException(400, "Неподдерживаемый режим")

    with db() as con:
        player = con.execute(
            "SELECT telegram_id FROM players WHERE telegram_id=?",
            (payload.telegram_id,),
        ).fetchone()

        if not player:
            raise HTTPException(
                400, "Сначала зарегистрируй игровой ID в профиле"
            )

        con.execute("""
            INSERT INTO queue (telegram_id, mode, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                mode=excluded.mode, joined_at=excluded.joined_at
        """, (payload.telegram_id, mode, now()))

        players = [
            r["telegram_id"]
            for r in con.execute(
                "SELECT telegram_id FROM queue WHERE mode=? ORDER BY joined_at",
                (mode,),
            ).fetchall()
        ]

        needed = int(mode.split("v")[0]) * 2

        if len(players) >= needed:
            chosen = players[:needed]
            match_id = str(uuid.uuid4())[:8]

            con.execute("""
                INSERT INTO matches
                    (match_id, mode, status, players_json, created_at, rated)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (
                match_id, mode, "ready",
                json.dumps(chosen), now()
            ))

            con.executemany(
                "DELETE FROM queue WHERE telegram_id=?",
                [(pid,) for pid in chosen],
            )

            return {
                "status": "matched",
                "mode": mode,
                "match_id": match_id,
                "players_found": needed,
                "players_needed": needed,
                "players": chosen,
            }

        return {
            "status": "queued",
            "mode": mode,
            "players_found": len(players),
            "players_needed": needed,
            "players_remaining": needed - len(players),
        }


@app.post("/queue/leave")
def leave_queue(payload: LeaveQueue):
    with db() as con:
        cur = con.execute(
            "DELETE FROM queue WHERE telegram_id=?",
            (payload.telegram_id,),
        )

    return {"status": "left_queue", "removed": cur.rowcount > 0}


@app.get("/queue/{mode}")
def queue_status(mode: str):
    mode = mode.lower()

    if mode not in {"1v1", "2v2", "3v3", "5v5"}:
        raise HTTPException(400, "Неподдерживаемый режим")

    with db() as con:
        rows = con.execute("""
            SELECT telegram_id, joined_at FROM queue
            WHERE mode=? ORDER BY joined_at
        """, (mode,)).fetchall()

    needed = int(mode.split("v")[0]) * 2

    return {
        "mode": mode,
        "players_found": len(rows),
        "players_needed": needed,
        "players": [dict(r) for r in rows],
    }


@app.get("/matches/{match_id}")
def get_match(match_id: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM matches WHERE match_id=?",
            (match_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "Матч не найден")

    data = dict(row)
    data["players"] = json.loads(data.pop("players_json"))
    data["result"] = (
        json.loads(data["result_json"])
        if data.get("result_json") else None
    )
    data.pop("result_json", None)
    return data


@app.post("/matches/{match_id}/result")
def submit_result(
    match_id: str,
    payload: MatchResult,
    x_admin_token: Optional[str] = Header(default=None),
):
    # Не включай рейтинг в продакшене, пока не настроен секрет.
    if not RESULTS_ADMIN_TOKEN:
        raise HTTPException(
            503,
            "Результаты временно отключены: настрой RESULTS_ADMIN_TOKEN в Render",
        )

    if x_admin_token != RESULTS_ADMIN_TOKEN:
        raise HTTPException(403, "Нет разрешения подтверждать результаты")

    if payload.winner not in {"team1", "team2", "draw"}:
        raise HTTPException(
            400,
            "winner должен быть team1, team2 или draw",
        )

    with db() as con:
        match = con.execute(
            "SELECT * FROM matches WHERE match_id=?",
            (match_id,),
        ).fetchone()

        if not match:
            raise HTTPException(404, "Матч не найден")

        if match["status"] == "finished" or match["rated"]:
            raise HTTPException(409, "Результат этого матча уже сохранён")

        players = json.loads(match["players_json"])
        if len(players) < 2:
            raise HTTPException(400, "В матче недостаточно игроков")

        # Данный базовый вариант делит список поровну на две команды.
        split = len(players) // 2
        team1, team2 = players[:split], players[split:]

        if payload.winner == "draw":
            elo_delta = 0
        else:
            elo_delta = 25

        for pid in players:
            p = con.execute(
                "SELECT elo FROM players WHERE telegram_id=?",
                (pid,),
            ).fetchone()
            if not p:
                continue

            is_winner = (
                payload.winner == "team1" and pid in team1
            ) or (
                payload.winner == "team2" and pid in team2
            )

            if payload.winner == "draw":
                con.execute(
                    "UPDATE players SET draws=draws+1 WHERE telegram_id=?",
                    (pid,),
                )
            elif is_winner:
                con.execute("""
                    UPDATE players
                    SET wins=wins+1, elo=elo+?
                    WHERE telegram_id=?
                """, (elo_delta, pid))
            else:
                con.execute("""
                    UPDATE players
                    SET losses=losses+1, elo=MAX(0, elo-?)
                    WHERE telegram_id=?
                """, (elo_delta, pid))

        result = {
            "winner": payload.winner,
            "team1": team1,
            "team2": team2,
            "submitted_at": now(),
        }

        con.execute("""
            UPDATE matches
            SET status='finished', result_json=?, rated=1
            WHERE match_id=?
        """, (json.dumps(result, ensure_ascii=False), match_id))

    return {
        "status": "result_saved",
        "match_id": match_id,
        "result": result,
    }

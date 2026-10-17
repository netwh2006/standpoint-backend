
import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", "standpoint.db")

app = FastAPI(title="STANDPOINT Matchmaking API", version="1.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
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


init_db()


class RegisterPlayer(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    nickname: str = Field(default="Игрок", max_length=64)
    game_id: str = Field(min_length=1, max_length=64)


class JoinQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="5v5", max_length=16)


class LeaveQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)


class MatchResult(BaseModel):
    winner: Optional[str] = None
    result: Optional[Any] = None
    telegram_id: Optional[str] = None


@app.get("/")
def root():
    return {
        "service": "STANDPOINT Matchmaking API",
        "status": "ok",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/players/register")
def register_player(payload: RegisterPlayer):
    now = datetime.now(timezone.utc).isoformat()

    with db() as con:
        con.execute("""
            INSERT INTO players
            (telegram_id, nickname, game_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
            nickname=excluded.nickname,
            game_id=excluded.game_id,
            updated_at=excluded.updated_at
        """, (
            payload.telegram_id,
            payload.nickname.strip() or "Игрок",
            payload.game_id.strip(),
            now,
            now
        ))

        row = con.execute(
            "SELECT * FROM players WHERE telegram_id=?",
            (payload.telegram_id,)
        ).fetchone()

    return {"status": "registered", "player": dict(row)}


@app.get("/players/{telegram_id}")
def get_player(telegram_id: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM players WHERE telegram_id=?",
            (telegram_id,)
        ).fetchone()

    if not row:
        raise HTTPException(404, "Игрок не зарегистрирован")

    return dict(row)


@app.post("/queue/join")
def join_queue(payload: JoinQueue):
    mode = payload.mode.strip().lower()

    if mode not in {"1v1", "2v2", "3v3", "5v5"}:
        raise HTTPException(400, "Неподдерживаемый режим")

    with db() as con:
        player = con.execute(
            "SELECT telegram_id FROM players WHERE telegram_id=?",
            (payload.telegram_id,)
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
        """, (
            payload.telegram_id,
            mode,
            datetime.now(timezone.utc).isoformat()
        ))

        players = [
            row["telegram_id"]
            for row in con.execute(
                "SELECT telegram_id FROM queue WHERE mode=? ORDER BY joined_at",
                (mode,)
            ).fetchall()
        ]

        needed = int(mode.split("v")[0]) * 2

        if len(players) >= needed:
            chosen = players[:needed]
            match_id = str(uuid.uuid4())[:8]

            con.execute("""
                INSERT INTO matches
                (match_id, mode, status, players_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                match_id,
                mode,
                "ready",
                json.dumps(chosen),
                datetime.now(timezone.utc).isoformat()
            ))

            con.executemany(
                "DELETE FROM queue WHERE telegram_id=?",
                [(pid,) for pid in chosen]
            )

            return {
                "status": "matched",
                "mode": mode,
                "match_id": match_id,
                "players_found": needed,
                "players_needed": needed,
                "players": chosen
            }

        return {
            "status": "queued",
            "mode": mode,
            "players_found": len(players),
            "players_needed": needed,
            "players_remaining": needed - len(players)
        }


@app.post("/queue/leave")
def leave_queue(payload: LeaveQueue):
    with db() as con:
        cur = con.execute(
            "DELETE FROM queue WHERE telegram_id=?",
            (payload.telegram_id,)
        )

    return {
        "status": "left_queue",
        "removed": cur.rowcount > 0
    }


@app.get("/queue/{mode}")
def queue_status(mode: str):
    mode = mode.lower()

    with db() as con:
        rows = con.execute(
            "SELECT telegram_id, joined_at FROM queue WHERE mode=? ORDER BY joined_at",
            (mode,)
        ).fetchall()

    needed = (
        int(mode.split("v")[0]) * 2
        if "v" in mode and mode.split("v")[0].isdigit()
        else 0
    )

    return {
        "mode": mode,
        "players_found": len(rows),
        "players_needed": needed,
        "players": [dict(row) for row in rows]
    }


@app.get("/matches/{match_id}")
def get_match(match_id: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM matches WHERE match_id=?",
            (match_id,)
        ).fetchone()

    if not row:
        raise HTTPException(404, "Матч не найден")

    data = dict(row)
    data["players"] = json.loads(data.pop("players_json"))
    data["result"] = (
        json.loads(data["result_json"])
        if data.get("result_json")
        else None
    )
    data.pop("result_json", None)

    return data


@app.post("/matches/{match_id}/result")
def submit_result(match_id: str, payload: MatchResult):
    result = payload.result if payload.result is not None else {
        "winner": payload.winner,
        "telegram_id": payload.telegram_id
    }

    with db() as con:
        row = con.execute(
            "SELECT match_id FROM matches WHERE match_id=?",
            (match_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Матч не найден")

        con.execute(
            "UPDATE matches SET status=?, result_json=? WHERE match_id=?",
            (
                "finished",
                json.dumps(result, ensure_ascii=False),
                match_id
            )
        )

    return {
        "status": "result_saved",
        "match_id": match_id,
        "result": result
    }

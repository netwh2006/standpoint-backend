
import os
import json
import sqlite3
import uuid
import secrets
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal

from fastapi import (
    FastAPI, HTTPException, Header, UploadFile, File, Form
)
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict


DB_PATH = os.getenv("DATABASE_PATH", "standpoint.db")
ADMIN_TOKEN = os.getenv("RESULTS_ADMIN_TOKEN", "").strip()
MODERATOR_USERNAME = os.getenv(
    "MODERATOR_USERNAME", "yukzen_bro"
).lstrip("@")

INITIAL_ELO = int(os.getenv("INITIAL_ELO", os.getenv("INITIAL_MMR", "1000")))
# Dynamic ELO uses the classic expected-score formula; K-factor is configurable.
ELO_K_FACTOR = max(1, min(int(os.getenv("ELO_K_FACTOR", "32")), 64))

MODES = {"1v1": 1, "2v2": 2, "3v3": 3, "5v5": 5}
MAPS = {
    "Sandstone", "Province", "Rust", "Breeze",
    "Zone 7", "Sakura", "Dune", "Calypso"
}

app = FastAPI(
    title="STANDPOINT Competitive Match API",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                game_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                draws INTEGER NOT NULL DEFAULT 0,
                kills INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                telegram_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                FOREIGN KEY(telegram_id)
                    REFERENCES players(telegram_id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                players_json TEXT NOT NULL,
                captain_id TEXT,
                map_name TEXT,
                map_actions_json TEXT NOT NULL DEFAULT '[]',
                score_a INTEGER,
                score_b INTEGER,
                winner TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                stats_json TEXT NOT NULL DEFAULT '{}',
                review_note TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                elo_applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS evidence_files (
                file_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                data BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(match_id)
                    REFERENCES matches(match_id) ON DELETE CASCADE
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_evidence_match
            ON evidence_files(match_id)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                elo_before INTEGER NOT NULL,
                elo_delta INTEGER NOT NULL,
                kills INTEGER,
                deaths INTEGER,
                kd REAL,
                created_at TEXT NOT NULL,
                UNIQUE(match_id, telegram_id)
            )
        """)

        # Feature tables added in v3.0.0. Kept in the same SQLite database
        # to preserve the existing deployment and database path.
        con.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                telegram_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user
                ON notifications(telegram_id, is_read, created_at);

            CREATE TABLE IF NOT EXISTS match_invites (
                invite_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                inviter_id TEXT NOT NULL,
                invitee_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(match_id) REFERENCES matches(match_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS friendships (
                requester_id TEXT NOT NULL,
                addressee_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(requester_id, addressee_id),
                CHECK(requester_id <> addressee_id)
            );

            CREATE TABLE IF NOT EXISTS clans (
                clan_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                tag TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                owner_id TEXT NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clan_members (
                clan_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL,
                PRIMARY KEY(clan_id, telegram_id),
                FOREIGN KEY(clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_clan_members_user
                ON clan_members(telegram_id);
            CREATE TABLE IF NOT EXISTS clan_invites (
                invite_id TEXT PRIMARY KEY,
                clan_id TEXT NOT NULL,
                inviter_id TEXT NOT NULL,
                invitee_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(clan_id) REFERENCES clans(clan_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS clan_wars (
                war_id TEXT PRIMARY KEY,
                clan_a TEXT NOT NULL,
                clan_b TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                score_a INTEGER,
                score_b INTEGER,
                winner_clan_id TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(clan_a) REFERENCES clans(clan_id),
                FOREIGN KEY(clan_b) REFERENCES clans(clan_id)
            );

            CREATE TABLE IF NOT EXISTS pass_seasons (
                season_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS player_pass (
                season_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(season_id, telegram_id),
                FOREIGN KEY(season_id) REFERENCES pass_seasons(season_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS rewards (
                reward_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                reward_type TEXT NOT NULL DEFAULT 'badge',
                season_id TEXT,
                required_level INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS player_rewards (
                telegram_id TEXT NOT NULL,
                reward_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                earned_at TEXT NOT NULL,
                PRIMARY KEY(telegram_id, reward_id),
                FOREIGN KEY(reward_id) REFERENCES rewards(reward_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                moderator_note TEXT,
                reviewed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(match_id) REFERENCES matches(match_id) ON DELETE CASCADE
            );
        """)

        migrations = [
            ("players", "elo",
             f"INTEGER NOT NULL DEFAULT {INITIAL_ELO}"),
            ("players", "wins", "INTEGER NOT NULL DEFAULT 0"),
            ("players", "losses", "INTEGER NOT NULL DEFAULT 0"),
            ("players", "draws", "INTEGER NOT NULL DEFAULT 0"),
            ("players", "kills", "INTEGER NOT NULL DEFAULT 0"),
            ("players", "deaths", "INTEGER NOT NULL DEFAULT 0"),
            ("matches", "captain_id", "TEXT"),
            ("matches", "captain_b_id", "TEXT"),
            ("matches", "map_name", "TEXT"),
            ("matches", "map_actions_json",
             "TEXT NOT NULL DEFAULT '[]'"),
            ("matches", "score_a", "INTEGER"),
            ("matches", "score_b", "INTEGER"),
            ("matches", "winner", "TEXT"),
            ("matches", "evidence_json",
             "TEXT NOT NULL DEFAULT '{}'"),
            ("matches", "stats_json",
             "TEXT NOT NULL DEFAULT '{}'"),
            ("matches", "review_note", "TEXT"),
            ("matches", "reviewed_by", "TEXT"),
            ("matches", "reviewed_at", "TEXT"),
            ("matches", "elo_applied",
             "INTEGER NOT NULL DEFAULT 0"),
            ("matches", "updated_at", "TEXT"),
            ("match_history", "elo_before", "INTEGER NOT NULL DEFAULT 0"),
            ("match_history", "elo_delta", "INTEGER NOT NULL DEFAULT 0"),
        ]

        for table, column, definition in migrations:
            columns = {
                row["name"]
                for row in con.execute(
                    f"PRAGMA table_info({table})"
                )
            }
            if column not in columns:
                con.execute(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {column} {definition}"
                )

        # Upgrade older databases created before the MMR -> ELO rename.
        player_cols = {r["name"] for r in con.execute("PRAGMA table_info(players)")}
        if "mmr" in player_cols and "elo" in player_cols:
            con.execute("UPDATE players SET elo=mmr WHERE (elo IS NULL OR elo=1000) AND mmr IS NOT NULL")
        history_cols = {r["name"] for r in con.execute("PRAGMA table_info(match_history)")}
        if "mmr_before" in history_cols and "elo_before" in history_cols:
            con.execute("UPDATE match_history SET elo_before=mmr_before WHERE elo_before=0")
        if "mmr_delta" in history_cols and "elo_delta" in history_cols:
            con.execute("UPDATE match_history SET elo_delta=mmr_delta WHERE elo_delta=0")
        match_cols = {r["name"] for r in con.execute("PRAGMA table_info(matches)")}
        if "mmr_applied" in match_cols and "elo_applied" in match_cols:
            con.execute("UPDATE matches SET elo_applied=mmr_applied")

        con.execute(
            "UPDATE players SET elo=? WHERE elo=0",
            (INITIAL_ELO,)
        )
        con.execute(
            "UPDATE matches SET updated_at=created_at "
            "WHERE updated_at IS NULL"
        )


init_db()


class RegisterPlayer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    telegram_id: str = Field(min_length=1, max_length=64)
    nickname: str = Field(
        default="ÐÐ³ÑÐ¾Ðº", min_length=1, max_length=64
    )
    game_id: str = Field(min_length=1, max_length=64)


class JoinQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="5v5", min_length=1, max_length=16)


class LeaveQueue(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)


class CaptainSelect(BaseModel):
    captain_id: str = Field(min_length=1, max_length=64)
    team: Literal["A", "B"] = "A"


class MapAction(BaseModel):
    telegram_id: str
    action: Literal["ban", "pick"]
    map_name: str


class ReviewDecision(BaseModel):
    decision: Literal["approve", "dispute", "reject"]
    score_a: Optional[int] = Field(
        default=None, ge=0, le=100
    )
    score_b: Optional[int] = Field(
        default=None, ge=0, le=100
    )
    stats: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    note: str = Field(default="", max_length=1000)


def require_admin(x_admin_token: Optional[str]):
    if not ADMIN_TOKEN:
        raise HTTPException(
            503,
            "ÐÐ¾Ð´ÐµÑÐ°ÑÐ¸Ñ Ð½Ðµ Ð½Ð°ÑÑÑÐ¾ÐµÐ½Ð°: Ð´Ð¾Ð±Ð°Ð²ÑÑÐµ "
            "RESULTS_ADMIN_TOKEN Ð² Render Environment"
        )

    if (
        not x_admin_token
        or not secrets.compare_digest(
            x_admin_token, ADMIN_TOKEN
        )
    ):
        raise HTTPException(403, "ÐÐµÑ Ð¿ÑÐ°Ð² Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¾ÑÐ°")


def load_match(con, match_id):
    row = con.execute(
        "SELECT * FROM matches WHERE match_id=?",
        (match_id,)
    ).fetchone()

    if not row:
        raise HTTPException(404, "ÐÐ°ÑÑ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")

    data = dict(row)

    fields = [
        ("players_json", "players"),
        ("map_actions_json", "map_actions"),
        ("evidence_json", "evidence"),
        ("stats_json", "stats"),
    ]

    for field, key in fields:
        try:
            default = "[]" if key == "map_actions" else "{}"
            data[key] = json.loads(data.pop(field) or default)
        except json.JSONDecodeError:
            data[key] = [] if key == "map_actions" else {}

    return data


def require_match_player(match, telegram_id):
    if telegram_id not in match["players"]:
        raise HTTPException(
            403, "ÐÑ Ð½Ðµ ÑÑÐ°ÑÑÐ½Ð¸Ðº ÑÑÐ¾Ð³Ð¾ Ð¼Ð°ÑÑÐ°"
        )


@app.get("/")
def root():
    return {
        "service": "STANDPOINT Competitive Match API",
        "status": "ok",
        "version": "3.0.0",
        "docs": "/docs",
        "moderator_username": "@" + MODERATOR_USERNAME,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    return {
        "modes": list(MODES.keys()),
        "maps": sorted(MAPS),
        "moderator_username": "@" + MODERATOR_USERNAME,
        "manual_review_required": True,
        "elo_changes_before_approval": False,
    }


@app.post("/players/register")
def register_player(payload: RegisterPlayer):
    stamp = now()

    with db() as con:
        con.execute("""
            INSERT INTO players(
                telegram_id, nickname, game_id,
                created_at, updated_at, elo
            )
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                nickname=excluded.nickname,
                game_id=excluded.game_id,
                updated_at=excluded.updated_at
        """, (
            payload.telegram_id,
            payload.nickname or "ÐÐ³ÑÐ¾Ðº",
            payload.game_id,
            stamp,
            stamp,
            INITIAL_ELO,
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

        history = con.execute("""
            SELECT h.*, m.mode, m.map_name,
                   m.score_a, m.score_b
            FROM match_history h
            JOIN matches m ON m.match_id=h.match_id
            WHERE h.telegram_id=?
            ORDER BY h.created_at DESC LIMIT 50
        """, (telegram_id,)).fetchall()

    if not row:
        raise HTTPException(404, "ÐÐ³ÑÐ¾Ðº Ð½Ðµ Ð·Ð°ÑÐµÐ³Ð¸ÑÑÑÐ¸ÑÐ¾Ð²Ð°Ð½")

    player = dict(row)
    player["kd"] = (
        round(player["kills"] / player["deaths"], 2)
        if player["deaths"]
        else float(player["kills"])
    )

    return {
        "player": player,
        "match_history": [dict(item) for item in history],
    }


@app.get("/leaderboard")
def leaderboard(limit: int = 50):
    limit = max(1, min(limit, 100))

    with db() as con:
        rows = con.execute("""
            SELECT telegram_id, nickname, game_id,
                   elo, wins, losses, draws, kills, deaths
            FROM players
            ORDER BY elo DESC, wins DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return {
        "players": [dict(row) for row in rows],
        "count": len(rows),
    }


@app.post("/queue/join")
def join_queue(payload: JoinQueue):
    mode = payload.mode.strip().lower()

    if mode not in MODES:
        raise HTTPException(
            400, "ÐÐ¾ÑÑÑÐ¿Ð½Ð¾: 1v1, 2v2, 3v3, 5v5"
        )

    stamp = now()

    with db() as con:
        player = con.execute(
            "SELECT 1 FROM players WHERE telegram_id=?",
            (payload.telegram_id,)
        ).fetchone()

        if not player:
            raise HTTPException(
                400, "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð·Ð°ÑÐµÐ³Ð¸ÑÑÑÐ¸ÑÑÐ¹ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ"
            )

        active = con.execute("""
            SELECT match_id FROM matches
            WHERE status IN (
                'ready', 'map_pick', 'playing',
                'awaiting_evidence', 'under_review',
                'disputed'
            )
            AND players_json LIKE ?
        """, ("%" + payload.telegram_id + "%",)).fetchone()

        if active:
            raise HTTPException(
                409, "Ð£ ÑÐµÐ±Ñ ÑÐ¶Ðµ ÐµÑÑÑ Ð°ÐºÑÐ¸Ð²Ð½ÑÐ¹ Ð¼Ð°ÑÑ"
            )

        con.execute("""
            INSERT INTO queue(telegram_id, mode, joined_at)
            VALUES(?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                mode=excluded.mode,
                joined_at=excluded.joined_at
        """, (payload.telegram_id, mode, stamp))

        players = [
            row["telegram_id"]
            for row in con.execute("""
                SELECT telegram_id FROM queue
                WHERE mode=? ORDER BY joined_at
            """, (mode,)).fetchall()
        ]

        needed = MODES[mode] * 2

        if len(players) < needed:
            return {
                "status": "queued",
                "mode": mode,
                "players_found": len(players),
                "players_needed": needed,
                "players_remaining": needed - len(players),
            }

        chosen = players[:needed]
        size = MODES[mode]
        team_a = chosen[:size]
        team_b = chosen[size:]
        captain = team_a[0]
        match_id = uuid.uuid4().hex[:10]

        con.execute("""
            INSERT INTO matches(
                match_id, mode, status, players_json,
                captain_id, created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?)
        """, (
            match_id, mode, "map_pick",
            json.dumps(chosen), captain, stamp, stamp
        ))

        con.executemany(
            "DELETE FROM queue WHERE telegram_id=?",
            [(pid,) for pid in chosen]
        )
        for pid in chosen:
            create_notification(con, pid, "match_found", "ÐÐ°ÑÑ Ð½Ð°Ð¹Ð´ÐµÐ½",
                f"Ð ÐµÐ¶Ð¸Ð¼ {mode}: Ð¼Ð°ÑÑ {match_id}",
                {"match_id": match_id, "mode": mode})

    return {
        "status": "matched",
        "match_id": match_id,
        "mode": mode,
        "players": chosen,
        "teams": {"A": team_a, "B": team_b},
        "captain_id": captain,
        "next_step": "ban_pick",
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
        "removed": cur.rowcount > 0,
    }


@app.get("/queue/{mode}")
def queue_status(mode: str):
    mode = mode.lower()

    if mode not in MODES:
        raise HTTPException(400, "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹ ÑÐµÐ¶Ð¸Ð¼")

    with db() as con:
        rows = con.execute("""
            SELECT telegram_id, joined_at FROM queue
            WHERE mode=? ORDER BY joined_at
        """, (mode,)).fetchall()

    needed = MODES[mode] * 2

    return {
        "mode": mode,
        "players_found": len(rows),
        "players_needed": needed,
        "players": [dict(row) for row in rows],
    }


@app.get("/matches/{match_id}")
def get_match(match_id: str):
    with db() as con:
        return load_match(con, match_id)


@app.post("/matches/{match_id}/captain")
def select_captain(match_id: str, payload: CaptainSelect):
    with db() as con:
        match = load_match(con, match_id)

        if match["status"] != "map_pick":
            raise HTTPException(409, "ÐÑÐ±Ð¾Ñ ÐºÐ°Ð¿Ð¸ÑÐ°Ð½Ð° Ð·Ð°ÐºÑÑÑ")

        team_size = MODES[match["mode"]]
        team_a, team_b = match["players"][:team_size], match["players"][team_size:]
        allowed = team_a if payload.team == "A" else team_b
        if payload.captain_id not in allowed:
            raise HTTPException(400, "ÐÐ°Ð¿Ð¸ÑÐ°Ð½ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±ÑÑÑ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ¾Ð¼ Ð²ÑÐ±ÑÐ°Ð½Ð½Ð¾Ð¹ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ")

        column = "captain_id" if payload.team == "A" else "captain_b_id"
        con.execute(f"UPDATE matches SET {column}=?, updated_at=? WHERE match_id=?",
                    (payload.captain_id, now(), match_id))

    return {"status": "captain_set", "team": payload.team,
            "captain_id": payload.captain_id}


@app.post("/matches/{match_id}/map-action")
def map_action(match_id: str, payload: MapAction):
    name = payload.map_name.strip()

    if name not in MAPS:
        raise HTTPException(
            400, "ÐÐ°ÑÑÐ° Ð¾ÑÑÑÑÑÑÐ²ÑÐµÑ Ð² ÑÐ°Ð·ÑÐµÑÑÐ½Ð½Ð¾Ð¼ ÑÐ¿Ð¸ÑÐºÐµ"
        )

    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, payload.telegram_id)

        if match["status"] != "map_pick":
            raise HTTPException(409, "ÐÐ°Ð½-Ð¿Ð¸Ðº ÑÐ¶Ðµ Ð·Ð°Ð²ÐµÑÑÑÐ½")

        actions = match["map_actions"]
        team_size = MODES[match["mode"]]
        team_a, team_b = match["players"][:team_size], match["players"][team_size:]
        captain_a = match.get("captain_id")
        captain_b = match.get("captain_b_id") or team_b[0]
        if not captain_a:
            raise HTTPException(409, "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð½Ð°Ð·Ð½Ð°ÑÑÑÐµ ÐºÐ°Ð¿Ð¸ÑÐ°Ð½Ð° ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ A")

        ban_limit = min(4, max(0, len(MAPS) - 1))
        ban_count = sum(1 for action in actions if action.get("action") == "ban")
        if ban_count < ban_limit:
            expected_captain = captain_a if ban_count % 2 == 0 else captain_b
            if payload.action != "ban":
                raise HTTPException(409, "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð·Ð°Ð²ÐµÑÑÐ¸ÑÐµ Ð²ÑÐµ Ð±Ð°Ð½Ñ Ð¿Ð¾ Ð¾ÑÐµÑÐµÐ´Ð¸")
            if payload.telegram_id != expected_captain:
                raise HTTPException(403, "Ð¡ÐµÐ¹ÑÐ°Ñ ÑÐ¾Ð´ Ð´ÑÑÐ³Ð¾Ð³Ð¾ ÐºÐ°Ð¿Ð¸ÑÐ°Ð½Ð°")
        else:
            if payload.action != "pick":
                raise HTTPException(409, "ÐÐ°Ð½Ñ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ñ: Ð½ÐµÐ¾Ð±ÑÐ¾Ð´Ð¸Ð¼Ð¾ Ð²ÑÐ±ÑÐ°ÑÑ Ð¾ÑÑÐ°Ð²ÑÑÑÑÑ ÐºÐ°ÑÑÑ")
            if payload.telegram_id != captain_a:
                raise HTTPException(403, "ÐÐ°ÑÑÑ Ð²ÑÐ±Ð¸ÑÐ°ÐµÑ ÐºÐ°Ð¿Ð¸ÑÐ°Ð½ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ A")

        if any(a["map_name"] == name for a in actions):
            raise HTTPException(
                409, "Ð­ÑÐ° ÐºÐ°ÑÑÐ° ÑÐ¶Ðµ Ð²ÑÐ±ÑÐ°Ð½Ð° Ð¸Ð»Ð¸ Ð·Ð°Ð±Ð°Ð½ÐµÐ½Ð°"
            )

        if payload.action == "pick":
            actions.append({"action": "pick", "map_name": name,
                            "by": payload.telegram_id, "at": now()})
            con.execute("""
                UPDATE matches SET map_name=?, map_actions_json=?, status='ready',
                updated_at=? WHERE match_id=?
            """, (name, json.dumps(actions), now(), match_id))
        else:
            actions.append({
                "action": "ban",
                "map_name": name,
                "by": payload.telegram_id,
                "at": now(),
            })
            con.execute("""
                UPDATE matches SET map_actions_json=?,
                updated_at=? WHERE match_id=?
            """, (json.dumps(actions), now(), match_id))

    return {
        "status": (
            "map_picked" if payload.action == "pick"
            else "map_banned"
        ),
        "map_name": name,
        "next_status": (
            "ready" if payload.action == "pick"
            else "map_pick"
        ),
    }


@app.post("/matches/{match_id}/start")
def start_match(match_id: str, telegram_id: str):
    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, telegram_id)

        if telegram_id != match["captain_id"]:
            raise HTTPException(
                403, "ÐÐ°Ð¿ÑÑÑÐ¸ÑÑ Ð¼Ð°ÑÑ Ð¼Ð¾Ð¶ÐµÑ ÑÐ¾Ð»ÑÐºÐ¾ ÐºÐ°Ð¿Ð¸ÑÐ°Ð½"
            )

        if match["status"] != "ready" or not match["map_name"]:
            raise HTTPException(
                409, "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð·Ð°Ð²ÐµÑÑÐ¸ÑÐµ Ð±Ð°Ð½-Ð¿Ð¸Ðº Ð¸ Ð²ÑÐ±ÐµÑÐ¸ÑÐµ ÐºÐ°ÑÑÑ"
            )

        con.execute("""
            UPDATE matches SET status='playing', updated_at=?
            WHERE match_id=?
        """, (now(), match_id))

    return {"status": "playing", "match_id": match_id}


@app.post("/matches/{match_id}/evidence")
async def submit_evidence(
    match_id: str,
    telegram_id: str = Form(...),
    claimed_score_a: int = Form(..., ge=0, le=100),
    claimed_score_b: int = Form(..., ge=0, le=100),
    stats_json: str = Form(default="{}"),
    screenshots: list[UploadFile] = File(...),
):
    """ÐÐ°Ð³ÑÑÐ·ÐºÐ° ÑÐºÑÐ¸Ð½ÑÐ¾ÑÐ¾Ð² Ð½ÐµÐ¿Ð¾ÑÑÐµÐ´ÑÑÐ²ÐµÐ½Ð½Ð¾ Ð¸Ð· Mini App."""

    if not screenshots or len(screenshots) > 6:
        raise HTTPException(
            400, "ÐÑÐ¸ÐºÑÐµÐ¿Ð¸ÑÐµ Ð¾Ñ 1 Ð´Ð¾ 6 ÑÐºÑÐ¸Ð½ÑÐ¾ÑÐ¾Ð²"
        )

    try:
        submitted_stats = json.loads(stats_json or "{}")
    except json.JSONDecodeError:
        raise HTTPException(
            400, "stats_json Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±ÑÑÑ ÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¼ JSON"
        )

    if not isinstance(submitted_stats, dict):
        raise HTTPException(
            400, "stats_json Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±ÑÑÑ Ð¾Ð±ÑÐµÐºÑÐ¾Ð¼"
        )

    prepared = []
    allowed_types = {
        "image/jpeg", "image/png", "image/webp"
    }
    max_size = 5 * 1024 * 1024

    for upload in screenshots:
        content_type = (upload.content_type or "").lower()

        if content_type not in allowed_types:
            raise HTTPException(
                400, "ÐÐ¾Ð´Ð´ÐµÑÐ¶Ð¸Ð²Ð°ÑÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ JPG, PNG Ð¸ WEBP"
            )

        data = await upload.read(max_size + 1)

        if not data:
            raise HTTPException(400, "ÐÐ´Ð¸Ð½ Ð¸Ð· ÑÐ°Ð¹Ð»Ð¾Ð² Ð¿ÑÑÑÐ¾Ð¹")

        if len(data) > max_size:
            raise HTTPException(
                413, "ÐÐ°ÐºÑÐ¸Ð¼Ð°Ð»ÑÐ½ÑÐ¹ ÑÐ°Ð·Ð¼ÐµÑ ÑÐºÑÐ¸Ð½ÑÐ¾ÑÐ° â 5 ÐÐ"
            )

        valid = (
            content_type == "image/jpeg"
            and data.startswith(b"\xff\xd8\xff")
        ) or (
            content_type == "image/png"
            and data.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            content_type == "image/webp"
            and len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        )

        if not valid:
            raise HTTPException(
                400, "Ð¤Ð°Ð¹Ð» Ð½Ðµ ÑÐ¾Ð¾ÑÐ²ÐµÑÑÑÐ²ÑÐµÑ ÑÐ¸Ð¿Ñ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ñ"
            )

        prepared.append((
            uuid.uuid4().hex,
            upload.filename or "screenshot",
            content_type,
            data,
        ))

    stamp = now()

    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, telegram_id)

        if match["status"] not in {
            "playing", "awaiting_evidence",
            "disputed", "under_review"
        }:
            raise HTTPException(
                409, "Ð¡ÐµÐ¹ÑÐ°Ñ ÑÐºÑÐ¸Ð½ÑÐ¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð½ÐµÐ»ÑÐ·Ñ"
            )

        con.execute("""
            DELETE FROM evidence_files
            WHERE match_id=? AND telegram_id=?
        """, (match_id, telegram_id))

        for file_id, filename, content_type, data in prepared:
            con.execute("""
                INSERT INTO evidence_files(
                    file_id, match_id, telegram_id,
                    filename, content_type, data, created_at
                ) VALUES(?,?,?,?,?,?,?)
            """, (
                file_id, match_id, telegram_id,
                filename, content_type, data, stamp
            ))

        evidence = match["evidence"]
        evidence[telegram_id] = {
            "files": [
                {
                    "file_id": file_id,
                    "filename": filename,
                    "content_type": content_type,
                }
                for file_id, filename, content_type, _ in prepared
            ],
            "claimed_score_a": claimed_score_a,
            "claimed_score_b": claimed_score_b,
            "submitted_at": stamp,
        }

        stats = match["stats"]
        stats.update(submitted_stats)

        con.execute("""
            UPDATE matches SET evidence_json=?, stats_json=?,
            status='under_review', updated_at=?
            WHERE match_id=?
        """, (
            json.dumps(evidence),
            json.dumps(stats),
            stamp,
            match_id,
        ))

        submissions_received = len(evidence)

    return {
        "status": "evidence_received",
        "match_id": match_id,
        "submissions_received": submissions_received,
        "players_in_match": len(match["players"]),
        "next_status": "under_review",
        "elo_changed": False,
        "uploaded_files": len(prepared),
        "storage": "temporary_database_blob",
    }


@app.get("/admin/matches/{match_id}/evidence/{file_id}")
def get_evidence_file(
    match_id: str,
    file_id: str,
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)

    with db() as con:
        row = con.execute("""
            SELECT content_type, data, filename
            FROM evidence_files
            WHERE match_id=? AND file_id=?
        """, (match_id, file_id)).fetchone()

    if not row:
        raise HTTPException(
            404, "Ð¤Ð¾ÑÐ¾ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾ Ð¸Ð»Ð¸ ÑÐ¶Ðµ ÑÐ´Ð°Ð»ÐµÐ½Ð¾"
        )

    filename = row["filename"].replace('"', "")

    return Response(
        content=row["data"],
        media_type=row["content_type"],
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition":
                f'inline; filename="{filename}"',
        },
    )


@app.get("/admin/moderation/queue")
def moderation_queue(
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)

    with db() as con:
        rows = con.execute("""
            SELECT match_id, mode, status, players_json,
                   captain_id, map_name, score_a, score_b,
                   created_at, updated_at
            FROM matches
            WHERE status IN ('under_review', 'disputed')
            ORDER BY created_at ASC
        """).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["players"] = json.loads(item.pop("players_json"))
        result.append(item)

    return {
        "moderator_username": "@" + MODERATOR_USERNAME,
        "matches": result,
        "count": len(result),
    }


@app.get("/admin/matches/{match_id}")
def admin_match_detail(
    match_id: str,
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)

    with db() as con:
        return load_match(con, match_id)


@app.post("/admin/matches/{match_id}/review")
def review_match(
    match_id: str,
    payload: ReviewDecision,
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)
    stamp = now()
    elo_changes = {}

    with db() as con:
        match = load_match(con, match_id)

        if match["status"] not in {"under_review", "disputed"}:
            raise HTTPException(
                409, "ÐÐ°ÑÑ Ð½Ðµ Ð¾Ð¶Ð¸Ð´Ð°ÐµÑ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ¸"
            )

        if payload.decision == "dispute":
            con.execute("""
                UPDATE matches SET status='disputed',
                review_note=?, reviewed_by=?, reviewed_at=?,
                updated_at=? WHERE match_id=?
            """, (
                payload.note,
                "@" + MODERATOR_USERNAME,
                stamp,
                stamp,
                match_id,
            ))

            return {
                "status": "disputed",
                "match_id": match_id,
                "elo_changed": False,
            }

        if payload.decision == "reject":
            con.execute("""
                UPDATE matches SET status='rejected',
                review_note=?, reviewed_by=?, reviewed_at=?,
                updated_at=? WHERE match_id=?
            """, (
                payload.note,
                "@" + MODERATOR_USERNAME,
                stamp,
                stamp,
                match_id,
            ))

            con.execute(
                "DELETE FROM evidence_files WHERE match_id=?",
                (match_id,)
            )

            return {
                "status": "rejected",
                "match_id": match_id,
                "elo_changed": False,
                "photos_deleted": True,
            }

        if payload.score_a is None or payload.score_b is None:
            raise HTTPException(
                400, "ÐÐ»Ñ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð¸Ñ ÑÐºÐ°Ð¶Ð¸ÑÐµ score_a Ð¸ score_b"
            )

        if match["elo_applied"]:
            raise HTTPException(
                409, "ELO Ð·Ð° ÑÑÐ¾Ñ Ð¼Ð°ÑÑ ÑÐ¶Ðµ Ð¿ÑÐ¸Ð¼ÐµÐ½ÑÐ½"
            )

        team_size = MODES[match["mode"]]
        players = match["players"]
        team_a = players[:team_size]
        team_b = players[team_size:]

        winner = (
            "draw" if payload.score_a == payload.score_b
            else "A" if payload.score_a > payload.score_b
            else "B"
        )

        stats = {**match["stats"], **payload.stats}

        for pid, values in stats.items():
            if pid not in players:
                raise HTTPException(
                    400,
                    "Ð¡ÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ° ÑÐ¾Ð´ÐµÑÐ¶Ð¸Ñ Ð¸Ð³ÑÐ¾ÐºÐ° Ð½Ðµ Ð¸Ð· ÑÑÐ¾Ð³Ð¾ Ð¼Ð°ÑÑÐ°"
                )

            if not isinstance(values, dict):
                raise HTTPException(
                    400, "Ð¡ÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ° Ð¸Ð³ÑÐ¾ÐºÐ° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ Ð¾Ð±ÑÐµÐºÑÐ¾Ð¼"
                )

            for key in ("kills", "deaths"):
                value = values.get(key)

                if value is not None and (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > 1000
                ):
                    raise HTTPException(
                        400,
                        "Kills/deaths Ð´Ð¾Ð»Ð¶Ð½Ñ Ð±ÑÑÑ ÑÐµÐ»ÑÐ¼Ð¸ ÑÐ¸ÑÐ»Ð°Ð¼Ð¸ "
                        "Ð¾Ñ 0 Ð´Ð¾ 1000"
                    )

        # ELO is calculated from each player's rating and the opposing
        # team's average rating. K-factor is configurable via ELO_K_FACTOR.
        k_factor = max(1, min(int(os.getenv("ELO_K_FACTOR", "32")), 64))
        ratings = {}
        for pid in players:
            row = con.execute(
                "SELECT elo FROM players WHERE telegram_id=?", (pid,)
            ).fetchone()
            if not row:
                raise HTTPException(400, "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº Ð¼Ð°ÑÑÐ° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð² Ð±Ð°Ð·Ðµ")
            ratings[pid] = int(row["elo"])
        avg_a = sum(ratings[pid] for pid in team_a) / len(team_a)
        avg_b = sum(ratings[pid] for pid in team_b) / len(team_b)

        expected_a = 1.0 / (1.0 + 10.0 ** ((avg_b - avg_a) / 400.0))
        expected_b = 1.0 - expected_a

        for pid in players:
            player = con.execute("""
                SELECT elo, wins, losses, draws
                FROM players WHERE telegram_id=?
            """, (pid,)).fetchone()

            if not player:
                raise HTTPException(
                    400, "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº Ð¼Ð°ÑÑÐ° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð² Ð±Ð°Ð·Ðµ"
                )

            team = "A" if pid in team_a else "B"

            outcome = (
                "draw" if winner == "draw"
                else "win" if team == winner
                else "loss"
            )

            expected = expected_a if team == "A" else expected_b
            actual = 0.5 if outcome == "draw" else 1.0 if outcome == "win" else 0.0
            # Every player on a team receives the same team-level delta. This
            # keeps equal-sized teams zero-sum while accounting for opponent strength.
            delta = int(round(k_factor * (actual - expected)))

            values = stats.get(pid, {})
            kills = values.get("kills")
            deaths = values.get("deaths")

            kd = (
                round(kills / deaths, 2)
                if kills is not None and deaths not in (None, 0)
                else float(kills)
                if kills is not None and deaths == 0
                else None
            )

            elo_changes[pid] = {"elo_before": player["elo"], "elo_delta": delta, "elo_after": player["elo"] + delta}
            con.execute("""
                UPDATE players SET
                    elo=elo+?,
                    wins=wins+?,
                    losses=losses+?,
                    draws=draws+?,
                    kills=kills+?,
                    deaths=deaths+?,
                    updated_at=?
                WHERE telegram_id=?
            """, (
                delta,
                1 if outcome == "win" else 0,
                1 if outcome == "loss" else 0,
                1 if outcome == "draw" else 0,
                kills or 0,
                deaths or 0,
                stamp,
                pid,
            ))

            history_columns = {r["name"] for r in con.execute("PRAGMA table_info(match_history)")}
            if "mmr_before" in history_columns and "mmr_delta" in history_columns:
                con.execute("""INSERT INTO match_history(
                    match_id,telegram_id,outcome,mmr_before,mmr_delta,
                    elo_before,elo_delta,kills,deaths,kd,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (match_id,pid,outcome,player["elo"],delta,player["elo"],delta,kills,deaths,kd,stamp))
            else:
                con.execute("""INSERT INTO match_history(
                    match_id,telegram_id,outcome,elo_before,elo_delta,
                    kills,deaths,kd,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (match_id,pid,outcome,player["elo"],delta,kills,deaths,kd,stamp))

        con.execute("""
            UPDATE matches SET
                status='confirmed',
                score_a=?,
                score_b=?,
                winner=?,
                stats_json=?,
                review_note=?,
                reviewed_by=?,
                reviewed_at=?,
                elo_applied=1,
                updated_at=?
            WHERE match_id=? AND elo_applied=0
        """, (
            payload.score_a,
            payload.score_b,
            winner,
            json.dumps(stats),
            payload.note,
            "@" + MODERATOR_USERNAME,
            stamp,
            stamp,
            match_id,
        ))

        # Award 50 seasonal XP for a moderator-confirmed match. The pass is
        # optional: matches still work normally when no season is active.
        active_season = con.execute("SELECT season_id FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if active_season:
            for pid in players:
                con.execute("""INSERT INTO player_pass(season_id,telegram_id,xp,level,updated_at)
                    VALUES(?,?,50,1,?) ON CONFLICT(season_id,telegram_id) DO UPDATE SET
                    xp=xp+50, level=CAST((xp+50)/100 AS INTEGER)+1, updated_at=excluded.updated_at""",
                    (active_season["season_id"], pid, stamp))
                pass_row = con.execute("SELECT level FROM player_pass WHERE season_id=? AND telegram_id=?",
                                       (active_season["season_id"], pid)).fetchone()
                unlocked = con.execute("SELECT reward_id,name FROM rewards WHERE season_id=? AND required_level<=?",
                                       (active_season["season_id"], pass_row["level"])).fetchall()
                for reward in unlocked:
                    inserted = con.execute("INSERT OR IGNORE INTO player_rewards(telegram_id,reward_id,source,earned_at) VALUES(?,?,'pass',?)",
                                           (pid, reward["reward_id"], stamp))
                    if inserted.rowcount:
                        create_notification(con, pid, "pass_reward_unlocked", "ÐÐ°Ð³ÑÐ°Ð´Ð° STANDPOINT Pass Ð¾ÑÐºÑÑÑÐ°",
                                            reward["name"], {"reward_id": reward["reward_id"], "season_id": active_season["season_id"]})

        con.execute(
            "DELETE FROM evidence_files WHERE match_id=?",
            (match_id,)
        )

    return {
        "status": "confirmed",
        "match_id": match_id,
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "winner": winner,
        "map_name": match["map_name"],
        "elo_system": {
            "type": "dynamic_team_elo",
            "k_factor": max(1, min(int(os.getenv("ELO_K_FACTOR", "32")), 64)),
            "changes": {pid: item["elo_delta"] for pid, item in elo_changes.items()},
        },
        "reviewed_by": "@" + MODERATOR_USERNAME,
        "elo_applied": True,
        "photos_deleted": True,
    }


@app.get("/matches/{match_id}/history")
def match_history(match_id: str):
    with db() as con:
        match = load_match(con, match_id)

        if match["status"] != "confirmed":
            raise HTTPException(
                409, "Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ Ð¼Ð°ÑÑÐ° ÐµÑÑ Ð½Ðµ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½"
            )

        rows = con.execute("""
            SELECT * FROM match_history
            WHERE match_id=?
            ORDER BY telegram_id
        """, (match_id,)).fetchall()

    return {
        "match_id": match_id,
        "mode": match["mode"],
        "map_name": match["map_name"],
        "score_a": match["score_a"],
        "score_b": match["score_b"],
        "winner": match["winner"],
        "players": [dict(row) for row in rows],
    }

# ---------------------------------------------------------------------------
# STANDPOINT v3.0.0 feature endpoints
# ---------------------------------------------------------------------------

class FriendRequest(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=64)

class FriendDecision(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]

class MatchInviteCreate(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=64)

class MatchInviteDecision(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]

class NotificationRead(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)

class ClanCreate(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=3, max_length=40)
    tag: str = Field(min_length=2, max_length=8, pattern=r"^[A-Za-z0-9]+$")
    description: str = Field(default="", max_length=500)

class ClanInvite(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=64)

class ClanInviteDecision(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]

class ClanRoleChange(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=64)
    role: Literal["member", "officer"]

class ClanWarCreate(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    opponent_clan_id: str = Field(min_length=1, max_length=64)

class ClanWarDecision(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]

class ClanWarResult(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    score_a: int = Field(ge=0, le=100)
    score_b: int = Field(ge=0, le=100)

class AppealCreate(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=80)
    details: str = Field(default="", max_length=2000)

class AppealReview(BaseModel):
    decision: Literal["uphold", "overturn", "need_info"]
    note: str = Field(default="", max_length=1000)

class PassSeasonCreate(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    starts_at: str
    ends_at: str
    active: bool = True

class RewardCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    reward_type: Literal["badge", "title", "cosmetic"] = "badge"
    season_id: Optional[str] = None
    required_level: int = Field(default=1, ge=1, le=100)


def create_notification(con, telegram_id: str, kind: str, title: str,
                        body: str = "", payload: Optional[dict] = None):
    con.execute("""INSERT INTO notifications(
        notification_id, telegram_id, kind, title, body, payload_json,
        is_read, created_at) VALUES(?,?,?,?,?,?,0,?)""",
        (uuid.uuid4().hex, telegram_id, kind, title, body,
         json.dumps(payload or {}), now()))


def require_registered_player(con, telegram_id: str):
    row = con.execute("SELECT telegram_id FROM players WHERE telegram_id=?",
                      (telegram_id,)).fetchone()
    if not row:
        raise HTTPException(404, "ÐÐ³ÑÐ¾Ðº Ð½Ðµ Ð·Ð°ÑÐµÐ³Ð¸ÑÑÑÐ¸ÑÐ¾Ð²Ð°Ð½")
    return row


def require_clan_role(con, clan_id: str, telegram_id: str, roles: tuple[str, ...]):
    row = con.execute("SELECT role FROM clan_members WHERE clan_id=? AND telegram_id=?",
                      (clan_id, telegram_id)).fetchone()
    if not row or row["role"] not in roles:
        raise HTTPException(403, "ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ Ð¿ÑÐ°Ð² Ð² ÐºÐ»Ð°Ð½Ðµ")
    return row


@app.get("/elo/config")
def elo_config():
    return {"system": "ELO", "initial_elo": INITIAL_ELO,
            "k_factor": ELO_K_FACTOR,
            "formula": "team_expected=1/(1+10^((opponent_team_average-own_team_average)/400))"}


@app.get("/elo/history/{telegram_id}")
def elo_history(telegram_id: str, limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        require_registered_player(con, telegram_id)
        rows = con.execute("""SELECT match_id, outcome, elo_before, elo_delta,
            kills, deaths, kd, created_at FROM match_history
            WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?""",
            (telegram_id, limit)).fetchall()
    return {"telegram_id": telegram_id, "history": [dict(r) for r in rows]}


@app.post("/friends/request")
def friend_request(payload: FriendRequest):
    if payload.telegram_id == payload.target_id:
        raise HTTPException(400, "ÐÐµÐ»ÑÐ·Ñ Ð´Ð¾Ð±Ð°Ð²Ð¸ÑÑ ÑÐ°Ð¼Ð¾Ð³Ð¾ ÑÐµÐ±Ñ")
    stamp = now()
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        require_registered_player(con, payload.target_id)
        existing = con.execute("""SELECT status FROM friendships WHERE
            (requester_id=? AND addressee_id=?) OR
            (requester_id=? AND addressee_id=?)""",
            (payload.telegram_id, payload.target_id,
             payload.target_id, payload.telegram_id)).fetchone()
        if existing and existing["status"] == "accepted":
            raise HTTPException(409, "ÐÑ ÑÐ¶Ðµ Ð´ÑÑÐ·ÑÑ")
        if existing and existing["status"] == "pending":
            raise HTTPException(409, "ÐÐ°ÑÐ²ÐºÐ° ÑÐ¶Ðµ ÑÑÑÐµÑÑÐ²ÑÐµÑ")
        con.execute("""INSERT INTO friendships(requester_id, addressee_id,
            status, created_at, updated_at) VALUES(?,?,'pending',?,?)
            ON CONFLICT(requester_id, addressee_id) DO UPDATE SET
            status='pending', updated_at=excluded.updated_at""",
            (payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "friend_request",
            "ÐÐ¾Ð²Ð°Ñ Ð·Ð°ÑÐ²ÐºÐ° Ð² Ð´ÑÑÐ·ÑÑ", payload.telegram_id,
            {"from": payload.telegram_id})
    return {"status": "pending", "target_id": payload.target_id}


@app.get("/friends/{telegram_id}")
def friends_list(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        friends = con.execute("""SELECT p.telegram_id, p.nickname, p.game_id,
            p.elo FROM friendships f JOIN players p ON
            p.telegram_id=CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END
            WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted'
            ORDER BY p.nickname COLLATE NOCASE""",
            (telegram_id, telegram_id, telegram_id)).fetchall()
        incoming = con.execute("""SELECT f.requester_id, p.nickname, f.created_at
            FROM friendships f JOIN players p ON p.telegram_id=f.requester_id
            WHERE f.addressee_id=? AND f.status='pending' ORDER BY f.created_at""",
            (telegram_id,)).fetchall()
        outgoing = con.execute("""SELECT f.addressee_id, p.nickname, f.created_at
            FROM friendships f JOIN players p ON p.telegram_id=f.addressee_id
            WHERE f.requester_id=? AND f.status='pending' ORDER BY f.created_at""",
            (telegram_id,)).fetchall()
    return {"friends": [dict(r) for r in friends],
            "incoming_requests": [dict(r) for r in incoming],
            "outgoing_requests": [dict(r) for r in outgoing]}


@app.post("/friends/{requester_id}/decision")
def friend_decision(requester_id: str, payload: FriendDecision):
    stamp = now()
    with db() as con:
        cur = con.execute("""UPDATE friendships SET status=?, updated_at=?
            WHERE requester_id=? AND addressee_id=? AND status='pending'""",
            ("accepted" if payload.decision == "accept" else "rejected",
             stamp, requester_id, payload.telegram_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "ÐÐ°ÑÐ²ÐºÐ° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°")
        if payload.decision == "accept":
            create_notification(con, requester_id, "friend_accepted",
                "ÐÐ°ÑÐ²ÐºÐ° Ð² Ð´ÑÑÐ·ÑÑ Ð¿ÑÐ¸Ð½ÑÑÐ°", payload.telegram_id,
                {"friend_id": payload.telegram_id})
    return {"status": payload.decision}


@app.delete("/friends/{telegram_id}/{target_id}")
def remove_friend(telegram_id: str, target_id: str):
    with db() as con:
        cur = con.execute("""DELETE FROM friendships WHERE status='accepted' AND
            ((requester_id=? AND addressee_id=?) OR
             (requester_id=? AND addressee_id=?))""",
            (telegram_id, target_id, target_id, telegram_id))
    return {"status": "removed", "removed": cur.rowcount > 0}


@app.post("/matches/{match_id}/invites")
def create_match_invite(match_id: str, payload: MatchInviteCreate):
    stamp, invite_id = now(), uuid.uuid4().hex
    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, payload.telegram_id)
        if match["status"] not in {"map_pick", "ready"}:
            raise HTTPException(409, "ÐÑÐ¸Ð³Ð»Ð°ÑÐ°ÑÑ Ð¸Ð³ÑÐ¾ÐºÐ¾Ð² Ð¼Ð¾Ð¶Ð½Ð¾ ÑÐ¾Ð»ÑÐºÐ¾ Ð´Ð¾ Ð½Ð°ÑÐ°Ð»Ð° Ð¼Ð°ÑÑÐ°")
        if payload.target_id in match["players"]:
            raise HTTPException(409, "ÐÐ³ÑÐ¾Ðº ÑÐ¶Ðµ ÑÑÐ°ÑÑÐ²ÑÐµÑ Ð² Ð¼Ð°ÑÑÐµ")
        require_registered_player(con, payload.target_id)
        con.execute("INSERT INTO match_invites(invite_id,match_id,inviter_id,invitee_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (invite_id, match_id, payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "match_invite", "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð² Ð¼Ð°ÑÑ",
            f"ÐÐ³ÑÐ¾Ðº {payload.telegram_id} Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐ°ÐµÑ Ð²Ð°Ñ Ð² Ð¼Ð°ÑÑ {match_id}",
            {"invite_id": invite_id, "match_id": match_id})
    return {"status": "invited", "invite_id": invite_id,
            "note": "ÐÑÐ¸Ð½ÑÑÐ¸Ðµ Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ñ Ð¿Ð¾ÐºÐ° Ð½Ðµ Ð¼ÐµÐ½ÑÐµÑ ÑÐ¾ÑÑÐ°Ð² ÑÐ¶Ðµ ÑÐ¾Ð·Ð´Ð°Ð½Ð½Ð¾Ð³Ð¾ Ð¼Ð°ÑÑÐ°; Ð¾Ð½Ð¾ ÑÐ¸ÐºÑÐ¸ÑÑÐµÑ Ð¾ÑÐºÐ»Ð¸Ðº Ð¸Ð³ÑÐ¾ÐºÐ°."}


@app.get("/matches/invites/{telegram_id}")
def list_match_invites(telegram_id: str):
    with db() as con:
        rows = con.execute("""SELECT i.*, m.mode, m.status AS match_status,
            m.map_name FROM match_invites i JOIN matches m ON m.match_id=i.match_id
            WHERE i.invitee_id=? AND i.status='pending' ORDER BY i.created_at DESC""",
            (telegram_id,)).fetchall()
    return {"invites": [dict(r) for r in rows]}


@app.post("/matches/invites/{invite_id}/decision")
def decide_match_invite(invite_id: str, payload: MatchInviteDecision):
    stamp = now()
    with db() as con:
        invite = con.execute("SELECT * FROM match_invites WHERE invite_id=?", (invite_id,)).fetchone()
        if not invite or invite["invitee_id"] != payload.telegram_id or invite["status"] != "pending":
            raise HTTPException(404, "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾")
        status = "accepted" if payload.decision == "accept" else "rejected"
        con.execute("UPDATE match_invites SET status=?,updated_at=? WHERE invite_id=?",
                    (status, stamp, invite_id))
        create_notification(con, invite["inviter_id"], "match_invite_response",
            "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð¿ÑÐ¸Ð½ÑÑÐ¾" if status == "accepted" else "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð¾ÑÐºÐ»Ð¾Ð½ÐµÐ½Ð¾",
            payload.telegram_id, {"invite_id": invite_id, "status": status})
    return {"status": status, "match_id": invite["match_id"]}


@app.get("/notifications/{telegram_id}")
def list_notifications(telegram_id: str, unread_only: bool = False, limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        require_registered_player(con, telegram_id)
        query = "SELECT * FROM notifications WHERE telegram_id=?"
        args: list = [telegram_id]
        if unread_only:
            query += " AND is_read=0"
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = con.execute(query, args).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        result.append(item)
    return {"notifications": result, "count": len(result)}


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, payload: NotificationRead):
    with db() as con:
        cur = con.execute("UPDATE notifications SET is_read=1 WHERE notification_id=? AND telegram_id=?",
                          (notification_id, payload.telegram_id))
        if not cur.rowcount:
            raise HTTPException(404, "Ð£Ð²ÐµÐ´Ð¾Ð¼Ð»ÐµÐ½Ð¸Ðµ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾")
    return {"status": "read"}


@app.post("/notifications/{telegram_id}/read-all")
def mark_all_notifications_read(telegram_id: str):
    with db() as con:
        cur = con.execute("UPDATE notifications SET is_read=1 WHERE telegram_id=? AND is_read=0",
                          (telegram_id,))
    return {"status": "ok", "updated": cur.rowcount}


@app.post("/clans")
def create_clan(payload: ClanCreate):
    stamp, clan_id = now(), uuid.uuid4().hex[:12]
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        if con.execute("SELECT 1 FROM clan_members WHERE telegram_id=?", (payload.telegram_id,)).fetchone():
            raise HTTPException(409, "ÐÐ³ÑÐ¾Ðº ÑÐ¶Ðµ ÑÐ¾ÑÑÐ¾Ð¸Ñ Ð² ÐºÐ»Ð°Ð½Ðµ")
        try:
            con.execute("INSERT INTO clans(clan_id,name,tag,description,owner_id,created_at) VALUES(?,?,?,?,?,?)",
                (clan_id, payload.name.strip(), payload.tag.upper(), payload.description.strip(), payload.telegram_id, stamp))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ Ð¸Ð»Ð¸ ÑÐµÐ³ ÐºÐ»Ð°Ð½Ð° ÑÐ¶Ðµ Ð·Ð°Ð½ÑÑÑ")
        con.execute("INSERT INTO clan_members(clan_id,telegram_id,role,joined_at) VALUES(?,?,'owner',?)",
                     (clan_id, payload.telegram_id, stamp))
        create_notification(con, payload.telegram_id, "clan_created", "ÐÐ»Ð°Ð½ ÑÐ¾Ð·Ð´Ð°Ð½", payload.name,
                            {"clan_id": clan_id})
    return {"status": "created", "clan_id": clan_id, "name": payload.name, "tag": payload.tag.upper()}


@app.get("/clans")
def list_clans(limit: int = 50, offset: int = 0):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    with db() as con:
        rows = con.execute("""SELECT c.*, COUNT(cm.telegram_id) AS member_count
            FROM clans c LEFT JOIN clan_members cm ON cm.clan_id=c.clan_id
            GROUP BY c.clan_id ORDER BY c.elo DESC, c.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)).fetchall()
    return {"clans": [dict(r) for r in rows], "limit": limit, "offset": offset}


@app.get("/clans/{clan_id}")
def clan_detail(clan_id: str):
    with db() as con:
        clan = con.execute("SELECT * FROM clans WHERE clan_id=?", (clan_id,)).fetchone()
        if not clan:
            raise HTTPException(404, "ÐÐ»Ð°Ð½ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        members = con.execute("""SELECT cm.telegram_id, cm.role, cm.joined_at,
            p.nickname, p.elo, p.wins, p.losses FROM clan_members cm
            JOIN players p ON p.telegram_id=cm.telegram_id WHERE cm.clan_id=?
            ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'officer' THEN 1 ELSE 2 END,
            cm.joined_at""", (clan_id,)).fetchall()
    return {"clan": dict(clan), "members": [dict(r) for r in members]}


@app.post("/clans/{clan_id}/invites")
def invite_to_clan(clan_id: str, payload: ClanInvite):
    stamp, invite_id = now(), uuid.uuid4().hex
    with db() as con:
        require_clan_role(con, clan_id, payload.telegram_id, ("owner", "officer"))
        require_registered_player(con, payload.target_id)
        if con.execute("SELECT 1 FROM clan_members WHERE telegram_id=?", (payload.target_id,)).fetchone():
            raise HTTPException(409, "ÐÐ³ÑÐ¾Ðº ÑÐ¶Ðµ ÑÐ¾ÑÑÐ¾Ð¸Ñ Ð² ÐºÐ»Ð°Ð½Ðµ")
        con.execute("INSERT INTO clan_invites(invite_id,clan_id,inviter_id,invitee_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (invite_id, clan_id, payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "clan_invite", "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð² ÐºÐ»Ð°Ð½", "",
                            {"invite_id": invite_id, "clan_id": clan_id})
    return {"status": "invited", "invite_id": invite_id}


@app.get("/clans/invites/{telegram_id}")
def clan_invites(telegram_id: str):
    with db() as con:
        rows = con.execute("""SELECT i.*, c.name AS clan_name, c.tag AS clan_tag
            FROM clan_invites i JOIN clans c ON c.clan_id=i.clan_id
            WHERE i.invitee_id=? AND i.status='pending' ORDER BY i.created_at DESC""",
            (telegram_id,)).fetchall()
    return {"invites": [dict(r) for r in rows]}


@app.post("/clans/invites/{invite_id}/decision")
def decide_clan_invite(invite_id: str, payload: ClanInviteDecision):
    stamp = now()
    with db() as con:
        invite = con.execute("SELECT * FROM clan_invites WHERE invite_id=?", (invite_id,)).fetchone()
        if not invite or invite["invitee_id"] != payload.telegram_id or invite["status"] != "pending":
            raise HTTPException(404, "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾")
        if payload.decision == "accept":
            require_registered_player(con, payload.telegram_id)
            if con.execute("SELECT 1 FROM clan_members WHERE telegram_id=?", (payload.telegram_id,)).fetchone():
                raise HTTPException(409, "ÐÐ³ÑÐ¾Ðº ÑÐ¶Ðµ ÑÐ¾ÑÑÐ¾Ð¸Ñ Ð² ÐºÐ»Ð°Ð½Ðµ")
            con.execute("INSERT INTO clan_members(clan_id,telegram_id,role,joined_at) VALUES(?,?,'member',?)",
                         (invite["clan_id"], payload.telegram_id, stamp))
        con.execute("UPDATE clan_invites SET status=?,updated_at=? WHERE invite_id=?",
                     ("accepted" if payload.decision == "accept" else "rejected", stamp, invite_id))
        create_notification(con, invite["inviter_id"], "clan_invite_response",
            "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð¿ÑÐ¸Ð½ÑÑÐ¾" if payload.decision == "accept" else "ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ðµ Ð¾ÑÐºÐ»Ð¾Ð½ÐµÐ½Ð¾",
            payload.telegram_id, {"clan_id": invite["clan_id"]})
    return {"status": payload.decision}


@app.post("/clans/{clan_id}/roles")
def change_clan_role(clan_id: str, payload: ClanRoleChange):
    with db() as con:
        require_clan_role(con, clan_id, payload.telegram_id, ("owner",))
        cur = con.execute("UPDATE clan_members SET role=? WHERE clan_id=? AND telegram_id=? AND role<>'owner'",
                          (payload.role, clan_id, payload.target_id))
        if not cur.rowcount:
            raise HTTPException(404, "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð¸Ð»Ð¸ ÑÐ¾Ð»Ñ Ð½ÐµÐ»ÑÐ·Ñ Ð¸Ð·Ð¼ÐµÐ½Ð¸ÑÑ")
    return {"status": "updated", "role": payload.role}


@app.delete("/clans/{clan_id}/members/{target_id}")
def remove_clan_member(clan_id: str, target_id: str, telegram_id: str):
    with db() as con:
        require_clan_role(con, clan_id, telegram_id, ("owner", "officer"))
        target = con.execute("SELECT role FROM clan_members WHERE clan_id=? AND telegram_id=?",
                             (clan_id, target_id)).fetchone()
        if not target:
            raise HTTPException(404, "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        if target["role"] == "owner" or (target["role"] == "officer" and telegram_id != target_id):
            raise HTTPException(403, "ÐÐµÐ»ÑÐ·Ñ Ð¸ÑÐºÐ»ÑÑÐ¸ÑÑ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ° Ð¸Ð»Ð¸ Ð´ÑÑÐ³Ð¾Ð³Ð¾ Ð¾ÑÐ¸ÑÐµÑÐ°")
        con.execute("DELETE FROM clan_members WHERE clan_id=? AND telegram_id=?", (clan_id, target_id))
    return {"status": "removed"}


@app.post("/clan-wars")
def create_clan_war(payload: ClanWarCreate):
    stamp, war_id = now(), uuid.uuid4().hex[:12]
    with db() as con:
        own = con.execute("SELECT clan_id FROM clan_members WHERE telegram_id=? AND role IN ('owner','officer')",
                          (payload.telegram_id,)).fetchone()
        if not own:
            raise HTTPException(403, "Ð¡Ð¾Ð·Ð´Ð°ÑÑ Clan War Ð¼Ð¾Ð¶ÐµÑ Ð²Ð»Ð°Ð´ÐµÐ»ÐµÑ Ð¸Ð»Ð¸ Ð¾ÑÐ¸ÑÐµÑ ÐºÐ»Ð°Ð½Ð°")
        if own["clan_id"] == payload.opponent_clan_id:
            raise HTTPException(400, "ÐÐµÐ»ÑÐ·Ñ Ð²ÑÐ·Ð²Ð°ÑÑ ÑÐ¾Ð±ÑÑÐ²ÐµÐ½Ð½ÑÐ¹ ÐºÐ»Ð°Ð½")
        opponent = con.execute("SELECT owner_id,name FROM clans WHERE clan_id=?", (payload.opponent_clan_id,)).fetchone()
        if not opponent:
            raise HTTPException(404, "ÐÐ»Ð°Ð½ ÑÐ¾Ð¿ÐµÑÐ½Ð¸ÐºÐ° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        con.execute("INSERT INTO clan_wars(war_id,clan_a,clan_b,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (war_id, own["clan_id"], payload.opponent_clan_id, payload.telegram_id, stamp, stamp))
        create_notification(con, opponent["owner_id"], "clan_war_challenge", "ÐÐ¾Ð²ÑÐ¹ Ð²ÑÐ·Ð¾Ð² Clan Wars", "",
                            {"war_id": war_id, "clan_id": own["clan_id"]})
    return {"status": "pending", "war_id": war_id}


@app.post("/clan-wars/{war_id}/decision")
def decide_clan_war(war_id: str, payload: ClanWarDecision):
    stamp = now()
    with db() as con:
        war = con.execute("SELECT * FROM clan_wars WHERE war_id=?", (war_id,)).fetchone()
        if not war or war["status"] != "pending":
            raise HTTPException(404, "ÐÑÐ·Ð¾Ð² Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð¸Ð»Ð¸ ÑÐ¶Ðµ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐ°Ð½")
        require_clan_role(con, war["clan_b"], payload.telegram_id, ("owner", "officer"))
        con.execute("UPDATE clan_wars SET status=?,updated_at=? WHERE war_id=?",
                    ("accepted" if payload.decision == "accept" else "rejected", stamp, war_id))
    return {"status": payload.decision, "war_id": war_id}


@app.post("/clan-wars/{war_id}/result")
def report_clan_war_result(war_id: str, payload: ClanWarResult):
    stamp = now()
    with db() as con:
        war = con.execute("SELECT * FROM clan_wars WHERE war_id=?", (war_id,)).fetchone()
        if not war or war["status"] != "accepted":
            raise HTTPException(409, "Clan War Ð½Ðµ Ð³Ð¾ÑÐ¾Ð² Ðº Ð¾ÑÐ¿ÑÐ°Ð²ÐºÐµ ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÐ°")
        require_clan_role(con, war["clan_a"], payload.telegram_id, ("owner", "officer"))
        winner = None if payload.score_a == payload.score_b else war["clan_a"] if payload.score_a > payload.score_b else war["clan_b"]
        con.execute("UPDATE clan_wars SET status='under_review',score_a=?,score_b=?,winner_clan_id=?,updated_at=? WHERE war_id=?",
                    (payload.score_a, payload.score_b, winner, stamp, war_id))
        create_notification(con, payload.telegram_id, "clan_war_result_submitted", "Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ Clan War Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ Ð½Ð° Ð¿ÑÐ¾Ð²ÐµÑÐºÑ", "",
                            {"war_id": war_id})
    return {"status": "under_review", "winner_clan_id": winner}


@app.get("/clan-wars/{clan_id}")
def clan_war_history(clan_id: str, limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        rows = con.execute("""SELECT w.*, a.name AS clan_a_name, b.name AS clan_b_name
            FROM clan_wars w JOIN clans a ON a.clan_id=w.clan_a
            JOIN clans b ON b.clan_id=w.clan_b
            WHERE w.clan_a=? OR w.clan_b=? ORDER BY w.created_at DESC LIMIT ?""",
            (clan_id, clan_id, limit)).fetchall()
    return {"wars": [dict(r) for r in rows]}


@app.post("/admin/clan-wars/{war_id}/review")
def review_clan_war(war_id: str, decision: Literal["approve", "reject"],
                    x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    stamp = now()
    with db() as con:
        war = con.execute("SELECT * FROM clan_wars WHERE war_id=? AND status='under_review'", (war_id,)).fetchone()
        if not war:
            raise HTTPException(404, "Clan War Ð½Ðµ Ð¾Ð¶Ð¸Ð´Ð°ÐµÑ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ¸")
        if decision == "approve":
            clan_a = con.execute("SELECT elo FROM clans WHERE clan_id=?", (war["clan_a"],)).fetchone()
            clan_b = con.execute("SELECT elo FROM clans WHERE clan_id=?", (war["clan_b"],)).fetchone()
            rating_a, rating_b = int(clan_a["elo"]), int(clan_b["elo"])
            expected_a = 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))
            actual_a = 0.5 if war["score_a"] == war["score_b"] else 1.0 if war["score_a"] > war["score_b"] else 0.0
            delta_a = int(round(ELO_K_FACTOR * (actual_a - expected_a)))
            delta_b = -delta_a
            con.execute("UPDATE clans SET elo=MAX(0,elo+?),wins=wins+?,losses=losses+? WHERE clan_id=?",
                        (delta_a, 1 if actual_a == 1 else 0, 1 if actual_a == 0 else 0, war["clan_a"]))
            con.execute("UPDATE clans SET elo=MAX(0,elo+?),wins=wins+?,losses=losses+? WHERE clan_id=?",
                        (delta_b, 1 if actual_a == 0 else 0, 1 if actual_a == 1 else 0, war["clan_b"]))
            winner = None if actual_a == 0.5 else war["clan_a"] if actual_a == 1 else war["clan_b"]
            con.execute("UPDATE clan_wars SET status='confirmed',winner_clan_id=?,updated_at=? WHERE war_id=?",
                        (winner, stamp, war_id))
        else:
            con.execute("UPDATE clan_wars SET status='rejected',updated_at=? WHERE war_id=?", (stamp, war_id))
        owners = con.execute("SELECT owner_id FROM clans WHERE clan_id IN (?,?)", (war["clan_a"], war["clan_b"])).fetchall()
        for owner in owners:
            create_notification(con, owner["owner_id"], "clan_war_reviewed",
                "Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ Clan War Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½" if decision == "approve" else "Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ Clan War Ð¾ÑÐºÐ»Ð¾Ð½ÑÐ½",
                "", {"war_id": war_id, "status": "confirmed" if decision == "approve" else "rejected"})
    return {"status": "confirmed" if decision == "approve" else "rejected", "war_id": war_id}


@app.post("/matches/{match_id}/appeals")
def create_match_appeal(match_id: str, payload: AppealCreate):
    stamp, appeal_id = now(), uuid.uuid4().hex
    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, payload.telegram_id)
        if match["status"] not in {"confirmed", "rejected", "disputed"}:
            raise HTTPException(409, "ÐÐ»Ñ ÑÑÐ¾Ð³Ð¾ Ð¼Ð°ÑÑÐ° Ð°Ð¿ÐµÐ»Ð»ÑÑÐ¸Ñ ÑÐµÐ¹ÑÐ°Ñ Ð½ÐµÐ´Ð¾ÑÑÑÐ¿Ð½Ð°")
        existing = con.execute("SELECT 1 FROM appeals WHERE match_id=? AND telegram_id=? AND status IN ('open','need_info')",
                               (match_id, payload.telegram_id)).fetchone()
        if existing:
            raise HTTPException(409, "Ð£ Ð²Ð°Ñ ÑÐ¶Ðµ ÐµÑÑÑ Ð¾ÑÐºÑÑÑÐ°Ñ Ð°Ð¿ÐµÐ»Ð»ÑÑÐ¸Ñ Ð¿Ð¾ ÑÑÐ¾Ð¼Ñ Ð¼Ð°ÑÑÑ")
        con.execute("INSERT INTO appeals(appeal_id,match_id,telegram_id,reason,details,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (appeal_id, match_id, payload.telegram_id, payload.reason, payload.details, stamp, stamp))
        create_notification(con, payload.telegram_id, "appeal_created", "ÐÐ¿ÐµÐ»Ð»ÑÑÐ¸Ñ ÑÐ¾Ð·Ð´Ð°Ð½Ð°", "",
                            {"appeal_id": appeal_id, "match_id": match_id})
    return {"status": "open", "appeal_id": appeal_id}


@app.get("/appeals/{telegram_id}")
def list_appeals(telegram_id: str, limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        rows = con.execute("SELECT * FROM appeals WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?",
                           (telegram_id, limit)).fetchall()
    return {"appeals": [dict(r) for r in rows]}


@app.get("/admin/appeals")
def admin_appeals(x_admin_token: Optional[str] = Header(default=None), status: str = "open"):
    require_admin(x_admin_token)
    if status not in {"open", "need_info", "upheld", "overturned"}:
        raise HTTPException(400, "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹ ÑÑÐ°ÑÑÑ Ð°Ð¿ÐµÐ»Ð»ÑÑÐ¸Ð¸")
    with db() as con:
        rows = con.execute("SELECT * FROM appeals WHERE status=? ORDER BY created_at ASC", (status,)).fetchall()
    return {"appeals": [dict(r) for r in rows], "count": len(rows)}


@app.post("/admin/appeals/{appeal_id}/review")
def review_appeal(appeal_id: str, payload: AppealReview,
                  x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    stamp = now()
    new_status = {"uphold": "upheld", "overturn": "overturned", "need_info": "need_info"}[payload.decision]
    with db() as con:
        appeal = con.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
        if not appeal or appeal["status"] not in {"open", "need_info"}:
            raise HTTPException(404, "ÐÑÐºÑÑÑÐ°Ñ Ð°Ð¿ÐµÐ»Ð»ÑÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°")
        if payload.decision == "overturn":
            match = load_match(con, appeal["match_id"])
            if match["status"] == "confirmed" and match.get("elo_applied"):
                history_rows = con.execute("SELECT * FROM match_history WHERE match_id=?",
                                           (appeal["match_id"],)).fetchall()
                for history in history_rows:
                    pid = history["telegram_id"]
                    win_delta = 1 if history["outcome"] == "win" else 0
                    loss_delta = 1 if history["outcome"] == "loss" else 0
                    draw_delta = 1 if history["outcome"] == "draw" else 0
                    con.execute("""UPDATE players SET elo=elo-?,wins=wins-?,losses=losses-?,
                        draws=draws-?,kills=MAX(0,kills-?),deaths=MAX(0,deaths-?),updated_at=?
                        WHERE telegram_id=?""",
                        (history["elo_delta"], win_delta, loss_delta, draw_delta,
                         history["kills"] or 0, history["deaths"] or 0, stamp, pid))
                con.execute("DELETE FROM match_history WHERE match_id=?", (appeal["match_id"],))
                active_season = con.execute("SELECT season_id FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
                if active_season:
                    for pid in match["players"]:
                        con.execute("""UPDATE player_pass SET xp=MAX(0,xp-50),
                            level=CAST(MAX(0,xp-50)/100 AS INTEGER)+1,updated_at=?
                            WHERE season_id=? AND telegram_id=?""",
                            (stamp, active_season["season_id"], pid))
                con.execute("""UPDATE matches SET status='disputed',score_a=NULL,score_b=NULL,
                    winner=NULL,stats_json='{}',elo_applied=0,review_note=?,updated_at=?
                    WHERE match_id=?""",
                    ("Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ Ð¾ÑÐ¼ÐµÐ½ÑÐ½ Ð¿Ð¾ Ð°Ð¿ÐµÐ»Ð»ÑÑÐ¸Ð¸: " + payload.note, stamp, appeal["match_id"]))
        con.execute("UPDATE appeals SET status=?,moderator_note=?,reviewed_by=?,updated_at=? WHERE appeal_id=?",
                    (new_status, payload.note, "@" + MODERATOR_USERNAME, stamp, appeal_id))
        create_notification(con, appeal["telegram_id"], "appeal_reviewed", "ÐÐ¿ÐµÐ»Ð»ÑÑÐ¸Ñ ÑÐ°ÑÑÐ¼Ð¾ÑÑÐµÐ½Ð°", payload.note,
                            {"appeal_id": appeal_id, "status": new_status})
    return {"status": new_status, "appeal_id": appeal_id}


@app.post("/admin/pass/seasons")
def create_pass_season(payload: PassSeasonCreate,
                       x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    try:
        start = datetime.fromisoformat(payload.starts_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(payload.ends_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "ÐÐ°ÑÑ Ð´Ð¾Ð»Ð¶Ð½Ñ Ð±ÑÑÑ ISO-8601")
    if end <= start:
        raise HTTPException(400, "ÐÐ°ÑÐ° Ð¾ÐºÐ¾Ð½ÑÐ°Ð½Ð¸Ñ Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ Ð¿Ð¾Ð·Ð¶Ðµ Ð½Ð°ÑÐ°Ð»Ð°")
    season_id, stamp = uuid.uuid4().hex[:12], now()
    with db() as con:
        if payload.active:
            con.execute("UPDATE pass_seasons SET active=0")
        con.execute("INSERT INTO pass_seasons(season_id,name,starts_at,ends_at,active,created_at) VALUES(?,?,?,?,?,?)",
                     (season_id, payload.name, start.isoformat(), end.isoformat(), int(payload.active), stamp))
    return {"status": "created", "season_id": season_id}


@app.get("/pass/active")
def active_pass():
    with db() as con:
        season = con.execute("SELECT * FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not season:
            return {"active": False, "season": None}
        rewards = con.execute("SELECT * FROM rewards WHERE season_id=? ORDER BY required_level,reward_id",
                              (season["season_id"],)).fetchall()
    return {"active": True, "season": dict(season), "rewards": [dict(r) for r in rewards]}


@app.get("/pass/{telegram_id}")
def player_pass(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        season = con.execute("SELECT * FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not season:
            return {"active": False, "message": "Ð¡ÐµÐ¹ÑÐ°Ñ Ð½ÐµÑ Ð°ÐºÑÐ¸Ð²Ð½Ð¾Ð³Ð¾ STANDPOINT Pass"}
        row = con.execute("SELECT * FROM player_pass WHERE season_id=? AND telegram_id=?",
                          (season["season_id"], telegram_id)).fetchone()
        xp, level = (row["xp"], row["level"]) if row else (0, 1)
        rewards = con.execute("""SELECT r.*, CASE WHEN pr.reward_id IS NULL THEN 0 ELSE 1 END AS claimed
            FROM rewards r LEFT JOIN player_rewards pr ON pr.reward_id=r.reward_id AND pr.telegram_id=?
            WHERE r.season_id=? ORDER BY r.required_level,r.reward_id""",
            (telegram_id, season["season_id"])).fetchall()
    return {"season": dict(season), "xp": xp, "level": level,
            "xp_to_next_level": max(0, level * 100 - xp), "rewards": [dict(r) for r in rewards]}


@app.post("/admin/pass/rewards")
def create_pass_reward(payload: RewardCreate,
                       x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    stamp, reward_id = now(), uuid.uuid4().hex[:12]
    with db() as con:
        if payload.season_id and not con.execute("SELECT 1 FROM pass_seasons WHERE season_id=?", (payload.season_id,)).fetchone():
            raise HTTPException(404, "Ð¡ÐµÐ·Ð¾Ð½ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        con.execute("INSERT INTO rewards(reward_id,name,description,reward_type,season_id,required_level,created_at) VALUES(?,?,?,?,?,?,?)",
                     (reward_id, payload.name, payload.description, payload.reward_type, payload.season_id, payload.required_level, stamp))
    return {"status": "created", "reward_id": reward_id}


@app.get("/rewards/{telegram_id}")
def player_rewards(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        rows = con.execute("""SELECT r.*, pr.source, pr.earned_at FROM player_rewards pr
            JOIN rewards r ON r.reward_id=pr.reward_id WHERE pr.telegram_id=? ORDER BY pr.earned_at DESC""",
            (telegram_id,)).fetchall()
    return {"rewards": [dict(r) for r in rows], "count": len(rows)}


@app.post("/admin/rewards/{reward_id}/grant/{telegram_id}")
def grant_reward(reward_id: str, telegram_id: str,
                 x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with db() as con:
        require_registered_player(con, telegram_id)
        if not con.execute("SELECT 1 FROM rewards WHERE reward_id=?", (reward_id,)).fetchone():
            raise HTTPException(404, "ÐÐ°Ð³ÑÐ°Ð´Ð° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°")
        con.execute("INSERT OR IGNORE INTO player_rewards(telegram_id,reward_id,source,earned_at) VALUES(?,?,'admin',?)",
                    (telegram_id, reward_id, now()))
        create_notification(con, telegram_id, "reward_granted", "ÐÐ¾Ð»ÑÑÐµÐ½Ð° Ð½Ð¾Ð²Ð°Ñ Ð½Ð°Ð³ÑÐ°Ð´Ð°", "",
                            {"reward_id": reward_id})
    return {"status": "granted", "reward_id": reward_id}


@app.get("/leaderboard/clans")
def clan_leaderboard(limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        rows = con.execute("""SELECT c.clan_id,c.name,c.tag,c.elo,c.wins,c.losses,
            COUNT(cm.telegram_id) AS member_count FROM clans c
            LEFT JOIN clan_members cm ON cm.clan_id=c.clan_id
            GROUP BY c.clan_id ORDER BY c.elo DESC,c.wins DESC LIMIT ?""", (limit,)).fetchall()
    return {"clans": [dict(r) for r in rows]}

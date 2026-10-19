
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
from fastapi.responses import Response, HTMLResponse
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
    version="3.1.0"
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

            CREATE TABLE IF NOT EXISTS cosmetic_items (
                item_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('frame','title','profile_card')),
                description TEXT NOT NULL DEFAULT '',
                price_points INTEGER NOT NULL DEFAULT 0 CHECK(price_points >= 0),
                rarity TEXT NOT NULL DEFAULT 'common',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS player_cosmetics (
                telegram_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'shop',
                PRIMARY KEY(telegram_id,item_id),
                FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY(item_id) REFERENCES cosmetic_items(item_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS player_cosmetic_settings (
                telegram_id TEXT PRIMARY KEY,
                active_frame TEXT,
                active_title TEXT,
                active_profile_card TEXT,
                points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS qualification_awards (
                award_id TEXT PRIMARY KEY,
                qualification_key TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                place INTEGER NOT NULL CHECK(place BETWEEN 1 AND 10),
                trophy_name TEXT NOT NULL,
                elo_snapshot INTEGER NOT NULL,
                awarded_at TEXT NOT NULL,
                UNIQUE(qualification_key,telegram_id),
                FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE
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
        default="ÃÂÃÂ³ÃÂÃÂ¾ÃÂº", min_length=1, max_length=64
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
            "ÃÂÃÂ¾ÃÂ´ÃÂµÃÂÃÂ°ÃÂÃÂ¸ÃÂ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂÃÂÃÂÃÂ¾ÃÂµÃÂ½ÃÂ°: ÃÂ´ÃÂ¾ÃÂ±ÃÂ°ÃÂ²ÃÂÃÂÃÂµ "
            "RESULTS_ADMIN_TOKEN ÃÂ² Render Environment"
        )

    if (
        not x_admin_token
        or not secrets.compare_digest(
            x_admin_token, ADMIN_TOKEN
        )
    ):
        raise HTTPException(403, "ÃÂÃÂµÃÂ ÃÂ¿ÃÂÃÂ°ÃÂ² ÃÂ¼ÃÂ¾ÃÂ´ÃÂµÃÂÃÂ°ÃÂÃÂ¾ÃÂÃÂ°")


def load_match(con, match_id):
    row = con.execute(
        "SELECT * FROM matches WHERE match_id=?",
        (match_id,)
    ).fetchone()

    if not row:
        raise HTTPException(404, "ÃÂÃÂ°ÃÂÃÂ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½")

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
            403, "ÃÂÃÂ ÃÂ½ÃÂµ ÃÂÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂº ÃÂÃÂÃÂ¾ÃÂ³ÃÂ¾ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ°"
        )


# ÃÂÃÂ¾ÃÂ»ÃÂ½ÃÂÃÂ¹ ÃÂ¸ÃÂ½ÃÂÃÂµÃÂÃÂÃÂµÃÂ¹ÃÂ STANDPOINT ÃÂ²ÃÂÃÂÃÂÃÂ¾ÃÂµÃÂ½ ÃÂ² main.py, ÃÂ¿ÃÂ¾ÃÂÃÂÃÂ¾ÃÂ¼ÃÂ ÃÂ¾ÃÂÃÂ´ÃÂµÃÂ»ÃÂÃÂ½ÃÂÃÂ¹ HTML-ÃÂÃÂ°ÃÂ¹ÃÂ» ÃÂ½ÃÂµ ÃÂ½ÃÂÃÂ¶ÃÂµÃÂ½.
# API_BASE ÃÂ² HTML ÃÂ¿ÃÂÃÂÃÂÃÂ¾ÃÂ¹: ÃÂ·ÃÂ°ÃÂ¿ÃÂÃÂ¾ÃÂÃÂ ÃÂ¸ÃÂ´ÃÂÃÂ ÃÂ½ÃÂ° ÃÂÃÂÃÂ¾ÃÂ ÃÂ¶ÃÂµ ÃÂ´ÃÂ¾ÃÂ¼ÃÂµÃÂ½, ÃÂ³ÃÂ´ÃÂµ ÃÂ·ÃÂ°ÃÂ¿ÃÂÃÂÃÂµÃÂ½ backend.
_FRONTEND_HTML_B64 = """PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzA4MGEwZCI+Cjx0aXRsZT5TVEFORFBPSU5UIOKAlCBTdGFuZG9mZiAyIENvbXBldGl0aXZlPC90aXRsZT4KPHN0eWxlPgo6cm9vdHtjb2xvci1zY2hlbWU6ZGFyazstLWJnOiMwODBhMGQ7LS1zdXJmYWNlOiMxMTE2MWM7LS1zdXJmYWNlMjojMTcxZTI2Oy0tc3VyZmFjZTM6IzIwMmEzNTstLXN0cm9rZTojM2E0NjUzOy0tdGV4dDojZmZmZmZmOy0tbXV0ZWQ6I2MwY2FkNDstLW9yYW5nZTojZmY1YTEyOy0tb3JhbmdlMjojZmY4YTIyOy0tZ3JlZW46IzUwZGRhMDstLXJlZDojZmY1OTY0Oy0tZ29sZDojZmZjYTY4Oy0tcjoxN3B4fQoqe2JveC1zaXppbmc6Ym9yZGVyLWJveH1odG1se2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yLXNjaGVtZTpkYXJrfWJvZHl7bWFyZ2luOjA7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAscmdiYSgyNTUsODAsMTUsLjEyKSx0cmFuc3BhcmVudCAyNyUpLHZhcigtLWJnKTtjb2xvcjp2YXIoLS10ZXh0KTtmb250OjE0cHgvMS40NSBJbnRlcix1aS1zYW5zLXNlcmlmLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLEJsaW5rTWFjU3lzdGVtRm9udCwiU2Vnb2UgVUkiLHNhbnMtc2VyaWY7bWluLWhlaWdodDoxMDB2aH1idXR0b24saW5wdXQsc2VsZWN0LHRleHRhcmVhe2ZvbnQ6aW5oZXJpdH1idXR0b257Y3Vyc29yOnBvaW50ZXI7Y29sb3I6aW5oZXJpdH1idXR0b246Zm9jdXMtdmlzaWJsZSxpbnB1dDpmb2N1cy12aXNpYmxle291dGxpbmU6MnB4IHNvbGlkIHZhcigtLW9yYW5nZTIpO291dGxpbmUtb2Zmc2V0OjJweH1he2NvbG9yOmluaGVyaXQ7dGV4dC1kZWNvcmF0aW9uOm5vbmV9LnNoZWxse21pbi1oZWlnaHQ6MTAwdmg7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMzhweCBtaW5tYXgoMCwxZnIpfS5zaWRlYmFye2hlaWdodDoxMDB2aDtwb3NpdGlvbjpzdGlja3k7dG9wOjA7YmFja2dyb3VuZDojMGIwZTEyO2JvcmRlci1yaWdodDoxcHggc29saWQgIzIwMjczMDtwYWRkaW5nOjIzcHggMTRweCAxNXB4O2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjI4cHg7ei1pbmRleDoxMH0uYnJhbmR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtwYWRkaW5nOjAgOHB4fS5icmFuZG1hcmt7d2lkdGg6MzdweDtoZWlnaHQ6MzdweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY0YzBhLCNmZjliMjgpO2NvbG9yOiMxNDBkMDk7Zm9udC1zaXplOjIzcHg7Zm9udC13ZWlnaHQ6MTAwMDtjbGlwLXBhdGg6cG9seWdvbigwIDAsMTAwJSAwLDEwMCUgNzAlLDY0JSAxMDAlLDAgMTAwJSk7Ym94LXNoYWRvdzowIDAgMjJweCAjZmY2NDFjMzN9LmJyYW5kbmFtZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo5NTA7bGV0dGVyLXNwYWNpbmc6LjVweH0uYnJhbmRuYW1lIHNwYW57Y29sb3I6dmFyKC0tb3JhbmdlKX0uYnJhbmR0YWd7Zm9udC1zaXplOjhweDtjb2xvcjojYmFjNGNlO2xldHRlci1zcGFjaW5nOjEuOHB4O2ZvbnQtd2VpZ2h0OjgwMH0ubmF2Z3JvdXAgbGFiZWx7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMTJweDttYXJnaW4tYm90dG9tOjhweDtmb250LXNpemU6OXB4O2xldHRlci1zcGFjaW5nOjEuN3B4O2NvbG9yOiM5YWE3YjQ7Zm9udC13ZWlnaHQ6OTAwfS5uYXZ7ZGlzcGxheTpncmlkO2dhcDo0cHh9Lm5hdiBidXR0b257d2lkdGg6MTAwJTtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O2JhY2tncm91bmQ6bm9uZTt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTFweCAxMnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo2NTA7Zm9udC1zaXplOjEzcHh9Lm5hdiBidXR0b24gLm5pe3dpZHRoOjE5cHg7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjE3cHh9Lm5hdiBidXR0b246aG92ZXJ7YmFja2dyb3VuZDojMTkxZjI3O2NvbG9yOndoaXRlfS5uYXYgYnV0dG9uLmFjdGl2ZXtjb2xvcjojZmY5YTRkO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCMzYTFiMTAsIzIwMTUwZik7Ym9yZGVyLWNvbG9yOiM0OTI4MTk7Ym94LXNoYWRvdzppbnNldCAzcHggMCB2YXIoLS1vcmFuZ2UpfS5uYXYgLm5hdmNvdW50e21hcmdpbi1sZWZ0OmF1dG87YmFja2dyb3VuZDojNDgyNDE2O2NvbG9yOiNmZjliNWM7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzoycHggNnB4O2ZvbnQtc2l6ZToxMHB4fS5zaWRlYmFyLWZvb3R7bWFyZ2luLXRvcDphdXRvfS51c2VyY2FyZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6MTFweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjMTcxZDI0LCMxMDE0MTkpfS5hdmF0YXJ7d2lkdGg6MzlweDtoZWlnaHQ6MzlweDtmbGV4OjAgMCBhdXRvO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0MGRlZywjZmY4YzI3LCM1YjI0MTIpO2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjp3aGl0ZX0udXNlcmNhcmQgYntkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMnB4O292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzO21heC13aWR0aDoxMjBweH0udXNlcmNhcmQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5tYWlue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjE1MzBweDttaW4td2lkdGg6MDttYXJnaW46MCBhdXRvO3BhZGRpbmc6MjhweCBjbGFtcCgxN3B4LDMuMnZ3LDQ2cHgpIDU1cHh9LnRvcGJhcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxNnB4O3BhZGRpbmctYm90dG9tOjIxcHg7bWFyZ2luLWJvdHRvbToyM3B4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyNDJiMzN9LmtpY2tlcntmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoycHg7Zm9udC13ZWlnaHQ6OTAwO2NvbG9yOnZhcigtLW9yYW5nZTIpfS50b3BiYXIgaDF7Zm9udC1zaXplOmNsYW1wKDI0cHgsM3Z3LDM1cHgpO2xldHRlci1zcGFjaW5nOi0xLjFweDtsaW5lLWhlaWdodDoxLjE7bWFyZ2luOjdweCAwIDA7Zm9udC13ZWlnaHQ6OTUwfS50b3BhY3Rpb25ze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0uaWNvbmJ0biwuYnRuLXNlY29uZGFyeXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDQ7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6MTFweDtjb2xvcjojZjRmNmY4O3BhZGRpbmc6MTFweCAxNHB4fS5pY29uYnRue3dpZHRoOjQzcHg7aGVpZ2h0OjQzcHg7cGFkZGluZzowO3Bvc2l0aW9uOnJlbGF0aXZlO2ZvbnQtc2l6ZToxOHB4fS5waW5ne3Bvc2l0aW9uOmFic29sdXRlO3dpZHRoOjdweDtoZWlnaHQ6N3B4O2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtib3JkZXItcmFkaXVzOjUwJTtyaWdodDo4cHg7dG9wOjhweH0uYnRuLXByaW1hcnl7Ym9yZGVyOjA7Ym9yZGVyLXJhZGl1czoxMXB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjZmY1MDBiLCNmZjkwMjQpO2NvbG9yOiMxNjBlMDk7Zm9udC13ZWlnaHQ6OTAwO3BhZGRpbmc6MTNweCAxOHB4O2JveC1zaGFkb3c6MCA3cHggMjJweCAjZmY2NDFjMjY7dHJhbnNpdGlvbjpmaWx0ZXIgLjE1cyx0cmFuc2Zvcm0gLjE1c30uYnRuLXByaW1hcnk6aG92ZXJ7ZmlsdGVyOmJyaWdodG5lc3MoMS4wOCk7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoLTFweCl9LmJ0bi1zZWNvbmRhcnk6aG92ZXIsLmljb25idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiM4YzRhMjR9LmxheW91dHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDEuNTVmcikgbWlubWF4KDI3NXB4LDFmcik7Z2FwOjE3cHh9LnN0YWNre2Rpc3BsYXk6Z3JpZDthbGlnbi1jb250ZW50OnN0YXJ0O2dhcDoxNnB4fS5oZXJve3Bvc2l0aW9uOnJlbGF0aXZlO2lzb2xhdGlvbjppc29sYXRlO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0OjI3MHB4O2JvcmRlci1yYWRpdXM6MjBweDtwYWRkaW5nOmNsYW1wKDI0cHgsMy41dncsNDBweCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtib3JkZXI6MXB4IHNvbGlkICM2MzMzMWM7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTA1ZGVnLHJnYmEoNDcsMjMsMTMsLjk4KSxyZ2JhKDIyLDE5LDIyLC45OCkgNTglLHJnYmEoMTIsMTcsMjIsLjk4KSl9Lmhlcm86YmVmb3Jle2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7ei1pbmRleDotMTt3aWR0aDoyODBweDtoZWlnaHQ6MjgwcHg7cmlnaHQ6NnB4O3RvcDotNTNweDtib3JkZXI6MXB4IHNvbGlkICNmZjY0MWMyODt0cmFuc2Zvcm06cm90YXRlKDM4ZGVnKTtib3gtc2hhZG93OjAgMCAwIDIycHggI2ZmNjQxYzA3LDAgMCAwIDQ3cHggI2ZmNjQxYzA1fS5oZXJvOmFmdGVye2NvbnRlbnQ6IlMiO3Bvc2l0aW9uOmFic29sdXRlO3otaW5kZXg6LTE7cmlnaHQ6MjBweDt0b3A6LTU2cHg7Zm9udC1zaXplOjMwMHB4O2xpbmUtaGVpZ2h0OjE7Zm9udC13ZWlnaHQ6MTAwMDtjb2xvcjojZmY2NDFjMTg7dHJhbnNmb3JtOnNrZXcoLTEwZGVnKX0uaGVyby1jb3B5e21heC13aWR0aDo1MTVweH0uZXllYnJvd3tjb2xvcjojZmY5ODRjO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoyLjRweH0uaGVybyBoMntmb250LXNpemU6Y2xhbXAoMzJweCw0LjF2dyw1MHB4KTtsZXR0ZXItc3BhY2luZzotMS44cHg7bGluZS1oZWlnaHQ6MS4wMTttYXJnaW46MTRweCAwIDE0cHg7Zm9udC13ZWlnaHQ6MTAwMH0uaGVybyBoMiBzcGFue2NvbG9yOnZhcigtLW9yYW5nZSl9Lmhlcm8gcHtjb2xvcjojYzFjN2NlO2ZvbnQtc2l6ZToxNHB4O2xpbmUtaGVpZ2h0OjEuNjU7bWF4LXdpZHRoOjQ0MHB4O21hcmdpbjowIDAgMjFweH0uaGVyby1hY3Rpb25ze2Rpc3BsYXk6ZmxleDtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0uc2VjdGlvbntkaXNwbGF5Om5vbmU7YW5pbWF0aW9uOmFwcGVhciAuMThzIGVhc2V9LnNlY3Rpb24uYWN0aXZle2Rpc3BsYXk6YmxvY2t9QGtleWZyYW1lcyBhcHBlYXJ7ZnJvbXtvcGFjaXR5Oi40O3RyYW5zZm9ybTp0cmFuc2xhdGVZKDNweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTpub25lfX0uc3RhdHN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMyxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0uc3RhdHtwYWRkaW5nOjE2cHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1zdHJva2UpO2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE1MWIyMiwjMTAxNDE5KTttaW4td2lkdGg6MH0uc3RhdCBsYWJlbHtmb250LXNpemU6MTBweDtjb2xvcjojZDBkN2RmO2xldHRlci1zcGFjaW5nOi43cHg7Zm9udC13ZWlnaHQ6NzUwO2Rpc3BsYXk6YmxvY2t9LnN0YXQgc3Ryb25ne2ZvbnQtc2l6ZTpjbGFtcCgyMXB4LDJ2dywyOHB4KTtkaXNwbGF5OmJsb2NrO21hcmdpbjo1cHggMCAycHg7bGV0dGVyLXNwYWNpbmc6LS43cHg7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo5NTB9LnN0YXQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5jYXJke3BhZGRpbmc6MThweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czp2YXIoLS1yKTtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMCwjMTAxNDE5KTttaW4td2lkdGg6MH0uY2FyZGhlYWR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtnYXA6MTBweDttYXJnaW4tYm90dG9tOjE1cHh9LmNhcmRoZWFkIGgze2ZvbnQtc2l6ZToxNHB4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojg1MDtjb2xvcjojZjVmN2Y5fS50ZXh0bGlua3tib3JkZXI6MDtiYWNrZ3JvdW5kOm5vbmU7Y29sb3I6I2ZmOWE0ZDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3NTA7cGFkZGluZzo1cHh9LnRleHRsaW5rOmhvdmVye2NvbG9yOiNmZmMwOGF9Lm1hdGNocm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4O2JvcmRlcjoxcHggc29saWQgIzI5MzIzYjtib3JkZXItcmFkaXVzOjEzcHg7YmFja2dyb3VuZDojMDkwZDEyfS5tYXRjaGxvZ297d2lkdGg6NDZweDtoZWlnaHQ6NDZweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JvcmRlci1yYWRpdXM6MTJweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzQ0MjAwZSwjMjExNTBmKTtjb2xvcjojZmY4YTIyO2ZvbnQtc2l6ZToyMnB4O2ZsZXgtc2hyaW5rOjB9Lm1hdGNoY29weXtmbGV4OjE7bWluLXdpZHRoOjB9Lm1hdGNoY29weSBie2ZvbnQtc2l6ZToxMnB4O2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2Y1ZjdmOX0ubWF0Y2hjb3B5IHNtYWxse2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTBweDttYXJnaW4tdG9wOjRweH0ucGlsbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0Ojg1MDtsZXR0ZXItc3BhY2luZzouM3B4O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtiYWNrZ3JvdW5kOiMxNTMzMjY7Ym9yZGVyOjFweCBzb2xpZCAjMjQ0YTM4O2NvbG9yOiM2MGU1YTU7d2hpdGUtc3BhY2U6bm93cmFwfS5wYXNzY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTVkZWcsIzJjMTkwZiwjMTkxNTFhIDU4JSwjMTExNzFkKTtib3JkZXItY29sb3I6IzYwMzUxZjtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59LnBhc3NjYXJkOmFmdGVye2NvbnRlbnQ6IuKcpiI7cG9zaXRpb246YWJzb2x1dGU7cmlnaHQ6MTNweDt0b3A6LTE1cHg7Zm9udC1zaXplOjEwMHB4O2NvbG9yOiNmZjhhMjIxMDtwb2ludGVyLWV2ZW50czpub25lfS5wYXNzLXRvcHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjEycHg7YWxpZ24taXRlbXM6Y2VudGVyfS5wYXNzLWxldmVse2ZvbnQtc2l6ZTozN3B4O2ZvbnQtd2VpZ2h0OjEwMDA7Y29sb3I6dmFyKC0tb3JhbmdlMik7bGluZS1oZWlnaHQ6MX0ucGFzc2NhcmQgaDN7bWFyZ2luOjdweCAwIDZweDtmb250LXNpemU6MTdweH0ubXV0ZWR7Y29sb3I6dmFyKC0tbXV0ZWQpfS5zbWFsbHtmb250LXNpemU6MTFweH0ucGFzc2NhcmQgcHttYXJnaW46MDtjb2xvcjojYzhkMGQ4O2ZvbnQtc2l6ZToxMXB4fS5wcm9ncmVzc3toZWlnaHQ6OHB4O2JhY2tncm91bmQ6IzMwMzYzZTtib3JkZXItcmFkaXVzOjIwcHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbjoxN3B4IDAgOHB4fS5wcm9ncmVzcyBzcGFue2hlaWdodDoxMDAlO3dpZHRoOjQwJTtkaXNwbGF5OmJsb2NrO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCNmZjRlMGIsI2ZmOWEyOCk7Ym94LXNoYWRvdzowIDAgMTRweCAjZmY2NDFjNTU7Ym9yZGVyLXJhZGl1czoyMHB4fS5wYXNzLWZvb3R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtjb2xvcjojYzRjZGQ2O2ZvbnQtc2l6ZToxMXB4fS5sZWFkZXJsaXN0e2Rpc3BsYXk6Z3JpZH0ubGVhZGVye2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MjNweCBtaW5tYXgoMCwxZnIpIGF1dG87YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzoxMHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgIzI1MmQzNX0ubGVhZGVyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTowfS5yYW5re2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjojYzBjYWQ0fS5sZWFkZXI6bnRoLWNoaWxkKDEpIC5yYW5re2NvbG9yOnZhcigtLWdvbGQpfS5sZWFkZXI6bnRoLWNoaWxkKDIpIC5yYW5re2NvbG9yOiNkMWQ4ZGZ9LmxlYWRlcjpudGgtY2hpbGQoMykgLnJhbmt7Y29sb3I6I2Q5OTg2OX0ucGxheWVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweDttaW4td2lkdGg6MH0ubWluaWF2YXt3aWR0aDozM3B4O2hlaWdodDozM3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Ym9yZGVyLXJhZGl1czoxMHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMzAzYjQ3LCMxYTIwMjcpO2JvcmRlcjoxcHggc29saWQgIzNhNDY1Mjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4NTA7ZmxleC1zaHJpbms6MH0ucGxheWVyIGJ7ZGlzcGxheTpibG9jaztmb250LXNpemU6MTFweDt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9LnBsYXllciBzbWFsbHtkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNjMGNhZDQ7bWFyZ2luLXRvcDoycHh9LmVsb3tmb250LXdlaWdodDo5NTA7Zm9udC1zaXplOjEycHh9LnNlY3Rpb25oZWFke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7bWFyZ2luOjVweCAwIDE5cHh9LnNlY3Rpb25oZWFkIGgye2ZvbnQtc2l6ZToyNnB4O2xldHRlci1zcGFjaW5nOi0uN3B4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojk1MH0uc2VjdGlvbmhlYWQgcHtmb250LXNpemU6MTJweDtjb2xvcjojYzBjYWQ0O21hcmdpbjo1cHggMCAwfS5iYWRnZS1vcmFuZ2V7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGQ7Ym9yZGVyOjFweCBzb2xpZCAjNjkzNzFkO2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjkwMH0ubWFwZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg0LG1pbm1heCgwLDFmcikpO2dhcDoxMHB4fS5tYXB0aWxle3BhZGRpbmc6MDt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzE0MWIyMztib3JkZXItcmFkaXVzOjEzcHg7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfS5tYXBhcnR7aGVpZ2h0OjEwNXB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVuO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjNTQ0MDJkLCMyYTMwMzQgNTAlLCMxMTE2MWMpfS5tYXBhcnQ6YmVmb3JlLC5tYXBhcnQ6YWZ0ZXJ7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoxNDBweDtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6I2ZmZmZmZjEyO3RyYW5zZm9ybTpyb3RhdGUoLTM1ZGVnKX0ubWFwYXJ0OmFmdGVye3RyYW5zZm9ybTpyb3RhdGUoNDVkZWcpfS5tYXBhcnQgc3Bhbntmb250LXNpemU6MzNweDt6LWluZGV4OjE7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDVweCAjMDAwKX0ubWFwdGlsZSBie2Rpc3BsYXk6YmxvY2s7cGFkZGluZzoxMHB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNmMWY0ZjZ9Lm1hcHRpbGUuc2VsZWN0ZWR7Ym9yZGVyLWNvbG9yOnZhcigtLW9yYW5nZSk7Ym94LXNoYWRvdzowIDAgMCAxcHggdmFyKC0tb3JhbmdlKSwwIDhweCAyNHB4ICNmZjY0MWMxNX0ubWFwdGlsZS5zZWxlY3RlZCBie2NvbG9yOiNmZjliNGR9Lm1hcHRpbGUuc2VsZWN0ZWQ6YWZ0ZXJ7Y29udGVudDoi4pyTIjtwb3NpdGlvbjphYnNvbHV0ZTtyaWdodDo3cHg7dG9wOjdweDt3aWR0aDoyMnB4O2hlaWdodDoyMnB4O2JvcmRlci1yYWRpdXM6NTAlO2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7YmFja2dyb3VuZDp2YXIoLS1vcmFuZ2UpO2NvbG9yOiMxMzBkMDg7Zm9udC13ZWlnaHQ6MTAwMH0ubW9kZWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDo4cHg7bWFyZ2luOjIwcHggMH0ubW9kZXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTIxNzFkO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHggNXB4O2ZvbnQtd2VpZ2h0OjkwMDtjb2xvcjojZDNkOWRmfS5tb2RlLmFjdGl2ZXtib3JkZXItY29sb3I6Izk4NDkxZjtiYWNrZ3JvdW5kOiMzYjIwMTQ7Y29sb3I6I2ZmOWE0ZH0ucXVldWVib3h7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjNTkzMDFiO2JvcmRlci1yYWRpdXM6MTlweDtwYWRkaW5nOjMwcHggMjJweDtiYWNrZ3JvdW5kOnJhZGlhbC1ncmFkaWVudChlbGxpcHNlIGF0IDUwJSAwLCMzYTFjMTAsdHJhbnNwYXJlbnQgNTglKSxsaW5lYXItZ3JhZGllbnQoMTQ1ZGVnLCMxNTFhMjAsIzBlMTIxNil9LnF1ZXVlaWNvbnt3aWR0aDoxMDBweDtoZWlnaHQ6MTAwcHg7bWFyZ2luOjAgYXV0byAxOHB4O2JvcmRlcjoxcHggc29saWQgIzc1M2ExYztib3JkZXItcmFkaXVzOjUwJTtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2NvbG9yOiNmZjhjMjU7Zm9udC1zaXplOjM2cHg7Ym94LXNoYWRvdzowIDAgMCA5cHggI2ZmNjQxYzA4LDAgMCA0NXB4ICNmZjY0MWMxNX0ucXVldWVib3ggaDN7Zm9udC1zaXplOjIzcHg7bWFyZ2luOjhweCAwfS5xdWV1ZWJveCBwe2NvbG9yOiNjNGNkZDY7Zm9udC1zaXplOjEycHh9LnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo3cHg7ZmxleC13cmFwOndyYXA7bWFyZ2luLWJvdHRvbToxNXB4fS50YWJzIGJ1dHRvbntib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6OXB4O2NvbG9yOiNhZGI3YzE7cGFkZGluZzo5cHggMTNweDtmb250LXNpemU6MTFweH0udGFicyBidXR0b24uYWN0aXZle2JhY2tncm91bmQ6IzNhMjAxNDtib3JkZXItY29sb3I6IzgwNDAxZjtjb2xvcjojZmY5YTRkfS50YWJsZXdyYXB7b3ZlcmZsb3c6YXV0bztib3JkZXI6MXB4IHNvbGlkICMyYzM1NDA7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0udGFibGV7Ym9yZGVyLWNvbGxhcHNlOmNvbGxhcHNlO3dpZHRoOjEwMCU7bWluLXdpZHRoOjUyMHB4fS50YWJsZSB0aCwudGFibGUgdGR7dGV4dC1hbGlnbjpsZWZ0O3BhZGRpbmc6MTRweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjUyZDM2O2ZvbnQtc2l6ZToxMXB4fS50YWJsZSB0aHtiYWNrZ3JvdW5kOiMyMDI4MzM7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo4MDB9LnRhYmxlIHRke2NvbG9yOiNlOGVkZjJ9LnRhYmxlIHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTowfS50YWJsZSB0ci55b3V7YmFja2dyb3VuZDojMmIxYTExfS5wcm9maWxlaGVyb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MjBweDtib3JkZXI6MXB4IHNvbGlkICM2MDM1MWY7Ym9yZGVyLXJhZGl1czoxN3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjMmIxOTBmLCMxNTE5MWUpO21hcmdpbi1ib3R0b206MTVweH0ucHJvZmlsZWF2YXRhcnt3aWR0aDo3MnB4O2hlaWdodDo3MnB4O2JvcmRlci1yYWRpdXM6MTlweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY4ZDI2LCM1YjIxMTEpO2ZvbnQtc2l6ZToyOHB4O2JveC1zaGFkb3c6MCAwIDAgNHB4ICNmZjY0MWMxOH0ucHJvZmlsZWhlcm8gaDN7bWFyZ2luOjAgMCA1cHg7Zm9udC1zaXplOjIwcHh9LnByb2ZpbGVoZXJvIHB7bWFyZ2luOjAgMCA4cHg7Zm9udC1zaXplOjExcHg7Y29sb3I6I2FlYjhjMn0ucHJvZmlsZWVsb3ttYXJnaW4tbGVmdDphdXRvO3RleHQtYWxpZ246cmlnaHR9LnByb2ZpbGVlbG8gc21hbGx7Zm9udC1zaXplOjlweDtjb2xvcjojYWViOGMyO2xldHRlci1zcGFjaW5nOjFweH0ucHJvZmlsZWVsbyBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MzBweDtjb2xvcjojZmY5YTRkfS5mb3JtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9LmZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6N3B4O21hcmdpbjoxMHB4IDB9LmZpZWxkIGxhYmVse2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiMWJiYzV9LmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe3dpZHRoOjEwMCU7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzA4MGIxMDtjb2xvcjojZjRmNmY4O2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHg7b3V0bGluZTpub25lfS5maWVsZCBpbnB1dDpmb2N1c3tib3JkZXItY29sb3I6dmFyKC0tb3JhbmdlKX0uY2xhbmhlcm97ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTRweDtwYWRkaW5nOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCAjMmUzYjQ5O2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTBkZWcsIzE4MjQzMSwjMTExNjFjKX0uY2xhbmxvZ297d2lkdGg6NjRweDtoZWlnaHQ6NjRweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6IzFkMzA0Mjtib3JkZXItcmFkaXVzOjE3cHg7Zm9udC1zaXplOjMwcHh9LmNsYW5oZXJvIGgze21hcmdpbjo0cHggMDtmb250LXNpemU6MTlweH0uY2xhbmhlcm8gcHttYXJnaW46MDtjb2xvcjojYWFiNWMwO2ZvbnQtc2l6ZToxMXB4fS5jbGFuaGVybyAuZ3Jvd3tmbGV4OjF9Lm5vdGljZXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1zdGFydDtnYXA6MTFweDtwYWRkaW5nOjE0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjgzMDNhfS5ub3RpY2U6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOjB9Lm5vdGljZWljb3t3aWR0aDozN3B4O2hlaWdodDozN3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXItcmFkaXVzOjExcHg7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGR9Lm5vdGljZSBie2ZvbnQtc2l6ZToxMnB4fS5ub3RpY2UgcHttYXJnaW46NHB4IDA7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTFweH0uYm90dG9tbmF2e2Rpc3BsYXk6bm9uZX0udG9hc3R7cG9zaXRpb246Zml4ZWQ7ei1pbmRleDoxMDA7cmlnaHQ6MThweDtib3R0b206MThweDttYXgtd2lkdGg6Y2FsYygxMDB2dyAtIDM2cHgpO2JhY2tncm91bmQ6IzE3MWYyNjtib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTE7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHZhcigtLW9yYW5nZSk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTNweCAxNnB4O2JveC1zaGFkb3c6MCAxMnB4IDQwcHggIzAwMDk7Y29sb3I6I2Y1ZjdmODtmb250LXNpemU6MTJweDthbmltYXRpb246YXBwZWFyIC4yc30uZm9vdG5vdGV7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNDttYXJnaW4tdG9wOjEycHg7bGluZS1oZWlnaHQ6MS41fQpAbWVkaWEobWF4LXdpZHRoOjEwNTBweCl7LnNoZWxse2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMDVweCBtaW5tYXgoMCwxZnIpfS5sYXlvdXR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uc2lkZWhpZGV7ZGlzcGxheTpub25lfX0KQG1lZGlhKG1heC13aWR0aDo3MDBweCl7LnNoZWxse2Rpc3BsYXk6YmxvY2t9LnNpZGViYXJ7ZGlzcGxheTpub25lfS5tYWlue3BhZGRpbmc6MjBweCAxNXB4IGNhbGMoOTRweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSl9LnRvcGJhcntwYWRkaW5nLWJvdHRvbToxNnB4O21hcmdpbi1ib3R0b206MThweH0udG9wYmFyIGgxe2ZvbnQtc2l6ZToyNXB4fS50b3BhY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2Rpc3BsYXk6bm9uZX0ubGF5b3V0e2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnI7Z2FwOjEzcHh9LnN0YWNre2dhcDoxM3B4fS5oZXJve21pbi1oZWlnaHQ6MzAwcHg7cGFkZGluZzoyNHB4IDIxcHg7Ym9yZGVyLXJhZGl1czoyMHB4fS5oZXJvOmJlZm9yZXtyaWdodDotOTBweDt0b3A6LThweDt3aWR0aDoyMzBweDtoZWlnaHQ6MjMwcHh9Lmhlcm86YWZ0ZXJ7cmlnaHQ6LTlweDt0b3A6MTJweDtmb250LXNpemU6MjIwcHh9Lmhlcm8gaDJ7Zm9udC1zaXplOjM3cHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvIHB7Zm9udC1zaXplOjEzcHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvLWFjdGlvbnN7Z2FwOjhweH0uaGVyby1hY3Rpb25zIGJ1dHRvbntwYWRkaW5nOjEycHggMTNweDtmb250LXNpemU6MTJweH0uc3RhdHN7Z2FwOjhweH0uc3RhdHtwYWRkaW5nOjEzcHggMTBweDtib3JkZXItcmFkaXVzOjE1cHh9LnN0YXQgbGFiZWx7Zm9udC1zaXplOjlweDtsZXR0ZXItc3BhY2luZzouM3B4fS5zdGF0IHN0cm9uZ3tmb250LXNpemU6MjFweH0uc3RhdCBzbWFsbHtmb250LXNpemU6OXB4O2xpbmUtaGVpZ2h0OjEuMztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6NHB4fS5jYXJke3BhZGRpbmc6MTVweDtib3JkZXItcmFkaXVzOjE2cHh9LnBhc3NjYXJke3BhZGRpbmc6MThweH0ubWFwZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDIsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9Lm1hcGFydHtoZWlnaHQ6OTNweH0uc2VjdGlvbmhlYWQgaDJ7Zm9udC1zaXplOjI0cHh9LnNlY3Rpb25oZWFkIHB7bWF4LXdpZHRoOjI1MHB4O2xpbmUtaGVpZ2h0OjEuNX0ucXVldWVib3h7cGFkZGluZzoyNXB4IDE1cHh9Lm1vZGVncmlke2dhcDo2cHh9Lm1vZGV7cGFkZGluZzoxMnB4IDJweDtmb250LXNpemU6MTJweH0ucHJvZmlsZWhlcm97cGFkZGluZzoxNXB4O2dhcDoxMXB4fS5wcm9maWxlYXZhdGFye3dpZHRoOjU3cHg7aGVpZ2h0OjU3cHg7Zm9udC1zaXplOjIzcHh9LnByb2ZpbGVoZXJvIGgze2ZvbnQtc2l6ZToxN3B4fS5wcm9maWxlZWxvIHN0cm9uZ3tmb250LXNpemU6MjRweH0uZm9ybWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uYm90dG9tbmF2e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsMWZyKTtwb3NpdGlvbjpmaXhlZDt6LWluZGV4OjUwO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3BhZGRpbmc6OHB4IDRweCBjYWxjKDhweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7YmFja2dyb3VuZDpyZ2JhKDgsMTAsMTMsLjk3KTtiYWNrZHJvcC1maWx0ZXI6Ymx1cigxNnB4KTtib3JkZXItdG9wOjFweCBzb2xpZCAjMjkzMjNkO2JveC1zaGFkb3c6MCAtOXB4IDI1cHggIzAwMDV9LmJvdHRvbW5hdiBidXR0b257ZGlzcGxheTpncmlkO2p1c3RpZnktaXRlbXM6Y2VudGVyO2dhcDozcHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjowO2NvbG9yOiNiYWM0Y2U7Zm9udC1zaXplOjlweDtwYWRkaW5nOjRweCAwfS5ib3R0b21uYXYgYnV0dG9uIHNwYW57Zm9udC1zaXplOjE5cHh9LmJvdHRvbW5hdiBidXR0b24uYWN0aXZle2NvbG9yOiNmZjlhNGR9LnRhYmxlIHRoLC50YWJsZSB0ZHtwYWRkaW5nOjEycHh9LnBhc3MtZm9vdHtmb250LXNpemU6MTBweH19CgovKiBDb250cmFzdCBoYXJkZW5pbmcgZm9yIGVtYmVkZGVkIFRlbGVncmFtL1B1enpsZS5ib3Qgd2Vidmlld3MgKi8KOnJvb3R7LS10ZXh0OiNmZmY7LS1tdXRlZDojYzBjYWQ0Oy0tc3Ryb2tlOiMzYTQ2NTN9CmJvZHl7Y29sb3I6I2ZmZn0KaDEsaDIsaDMsaDQsYixzdHJvbmcsLmJyYW5kbmFtZSwubWF0Y2hjb3B5IGIsLnBsYXllciBiLC5jYXJkaGVhZCBoMywuc2VjdGlvbmhlYWQgaDJ7Y29sb3I6I2ZmZn0KcHtjb2xvcjojZDBkN2RmfQpsYWJlbHtjb2xvcjojZDBkN2RmfQouY2FyZCwuc3RhdCwudGFibGV3cmFwLC5xdWV1ZWJveCwubWF0Y2hyb3d7Y29sb3I6I2ZmZn0KLnN0YXQgbGFiZWwsLm1hdGNoY29weSBzbWFsbCwucGxheWVyIHNtYWxsLC5zZWN0aW9uaGVhZCBwLC5tdXRlZCwuZm9vdG5vdGV7Y29sb3I6I2I5YzVkMH0KLnRhYmxlIHRoe2NvbG9yOiNlMGU2ZWN9Ci50YWJsZSB0ZHtjb2xvcjojZjdmOWZifQoubmF2IGJ1dHRvbntjb2xvcjojZDJkOWUxfQoubmF2IGJ1dHRvbi5hY3RpdmV7Y29sb3I6I2ZmYjA2Zn0KLmJ0bi1zZWNvbmRhcnl7Y29sb3I6I2ZmZjtiYWNrZ3JvdW5kOiMxYjIyMmI7Ym9yZGVyLWNvbG9yOiM0NjUzNjF9Ci50ZXh0bGlua3tjb2xvcjojZmZhZDcwfQouZmllbGQgbGFiZWx7Y29sb3I6I2Q1ZGNlNH0KLmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe2NvbG9yOiNmZmY7YmFja2dyb3VuZDojMDgwYzExO2JvcmRlci1jb2xvcjojNDY1MzYxfQoucGlsbHtjb2xvcjojNzVmMGI2fQouc3RhdCBzbWFsbHtjb2xvcjojNzVlYWIwfQoubm90aWNlIHB7Y29sb3I6I2MyY2NkNn0KLnBhc3NjYXJkIHB7Y29sb3I6I2QwZDdkZn0KCgovKiBGb3JjZSBkYXJrIGNhbnZhcyBpbiBlbWJlZGRlZCBidWlsZGVycy93ZWJ2aWV3cyAoaW5jbHVkaW5nIHNhZmUgYXJlYXMpICovCmh0bWwsYm9keXtiYWNrZ3JvdW5kLWNvbG9yOiMwODBhMGQhaW1wb3J0YW50O2NvbG9yOiNmZmYhaW1wb3J0YW50fQpib2R5e2JhY2tncm91bmQtaW1hZ2U6cmFkaWFsLWdyYWRpZW50KGVsbGlwc2UgYXQgMTAwJSAwLHJnYmEoMjU1LDgwLDE1LC4xMiksdHJhbnNwYXJlbnQgMjclKSFpbXBvcnRhbnR9CiNhcHAsLmFwcCwuY29udGFpbmVyLC5zaGVsbCwubWFpbntiYWNrZ3JvdW5kLWNvbG9yOnRyYW5zcGFyZW50fQppbnB1dCxzZWxlY3QsdGV4dGFyZWF7Y29sb3Itc2NoZW1lOmRhcmt9Cjo6c2VsZWN0aW9ue2JhY2tncm91bmQ6I2ZmNjQxYztjb2xvcjojMDgwYTBkfQoKCi8qIEhBUkQgT1ZFUlJJREVTOiBQdXp6bGUuYm90IC8gVGVsZWdyYW0gV2ViVmlldyBob3N0IHN0eWxlcyAqLwo6cm9vdCwgaHRtbCwgYm9keSB7CiAgYmFja2dyb3VuZDogIzA4MGEwZCAhaW1wb3J0YW50OwogIGJhY2tncm91bmQtY29sb3I6ICMwODBhMGQgIWltcG9ydGFudDsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50OwogIGNvbG9yLXNjaGVtZTogZGFyayAhaW1wb3J0YW50Owp9Cmh0bWwsIGJvZHkgewogIG1pbi1oZWlnaHQ6IDEwMCUgIWltcG9ydGFudDsKICBtYXJnaW46IDAgIWltcG9ydGFudDsKfQpib2R5IHsKICBiYWNrZ3JvdW5kLWltYWdlOiByYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAsIHJnYmEoMjU1LDgwLDE1LC4xMCksIHRyYW5zcGFyZW50IDMwJSkgIWltcG9ydGFudDsKfQouc2hlbGwsIC5tYWluLCBtYWluLCBzZWN0aW9uLCBhcnRpY2xlLCAucGFnZSwgLmNvbnRlbnQsIC5jb250YWluZXIsICNhcHAsICNyb290IHsKICBjb2xvcjogI2Y3ZjlmYzsKfQpoMSwgaDIsIGgzLCBoNCwgaDUsIGg2LCBwLCBsYWJlbCwgc3Bhbiwgc21hbGwsIHN0cm9uZywgYiwgbGksIHRkLCB0aCB7CiAgdGV4dC1zaGFkb3c6IG5vbmU7Cn0KLnRvcGJhciBoMSwgLmNhcmQgaDIsIC5jYXJkIGgzLCAuc2VjdGlvbmhlYWQgaDIsIC5oZXJvIGgyLAouc3RhdCBzdHJvbmcsIC5tYXRjaGNvcHkgYiwgLnBsYXllciBiLCAuYnJhbmRuYW1lIHsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50Owp9Ci5raWNrZXIsIC50ZXh0bGluaywgLnNlY3Rpb25oZWFkIGEgeyBjb2xvcjogI2ZmYWI3MCAhaW1wb3J0YW50OyB9Ci5tdXRlZCwgLmZvb3Rub3RlLCAuc2VjdGlvbmhlYWQgcCwgLm1hdGNoY29weSBzbWFsbCwgLnBsYXllciBzbWFsbCwKLnN0YXQgbGFiZWwsIC5zdGF0IHNtYWxsLCAuY2FyZCBwLCAuY2FyZCBzbWFsbCB7CiAgY29sb3I6ICNjMmNjZDYgIWltcG9ydGFudDsKfQouY2FyZCwgLnN0YXQsIC5xdWV1ZWJveCwgLnRhYmxld3JhcCwgLm1hdGNocm93LCAubm90aWNlLCAucGFzc2NhcmQgewogIGNvbG9yOiAjZjdmOWZjOwp9CkBtZWRpYSAobWF4LXdpZHRoOiA3NjBweCkgewogIGh0bWwsIGJvZHksIC5zaGVsbCwgLm1haW4geyBiYWNrZ3JvdW5kLWNvbG9yOiAjMDgwYTBkICFpbXBvcnRhbnQ7IH0KfQoKCi8qIEZJTkFMIFJFU1BPTlNJVkUgKyBDTEFOIFJPU1RFUiBGSVggKi8KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIHsgd2lkdGg6MTAwJTsgbWluLXdpZHRoOjA7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBkaXNwbGF5OmdyaWQgIWltcG9ydGFudDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDFmcikgbWF4LWNvbnRlbnQgIWltcG9ydGFudDsgYWxpZ24taXRlbXM6Y2VudGVyICFpbXBvcnRhbnQ7IGNvbHVtbi1nYXA6MTZweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MDsgcGFkZGluZzoxM3B4IDJweCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHsgZGlzcGxheTpmbGV4ICFpbXBvcnRhbnQ7IGFsaWduLWl0ZW1zOmNlbnRlciAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MCAhaW1wb3J0YW50OyBvdmVyZmxvdzpoaWRkZW47IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgPiBkaXY6bGFzdC1jaGlsZCB7IG1pbi13aWR0aDowICFpbXBvcnRhbnQ7IGZsZXg6MSAxIGF1dG8gIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAjY2xhbk1lbWJlcnMgLnBsYXllciBiIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgZm9udC13ZWlnaHQ6ODAwICFpbXBvcnRhbnQ7IHdoaXRlLXNwYWNlOm5vcm1hbCAhaW1wb3J0YW50OyBvdmVyZmxvdy13cmFwOmFueXdoZXJlICFpbXBvcnRhbnQ7IHdvcmQtYnJlYWs6YnJlYWstd29yZCAhaW1wb3J0YW50OyBvdmVyZmxvdzp2aXNpYmxlICFpbXBvcnRhbnQ7IHRleHQtb3ZlcmZsb3c6Y2xpcCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHNtYWxsIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBtYXJnaW4tdG9wOjRweCAhaW1wb3J0YW50OyBjb2xvcjojYzJjY2Q2ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxMXB4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgd2hpdGUtc3BhY2U6bm9ybWFsICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5taW5pYXZhIHsgd2lkdGg6NDBweCAhaW1wb3J0YW50OyBoZWlnaHQ6NDBweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA0MHB4ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5lbG8geyBkaXNwbGF5OmJsb2NrICFpbXBvcnRhbnQ7IGp1c3RpZnktc2VsZjplbmQgIWltcG9ydGFudDsgYWxpZ24tc2VsZjpjZW50ZXIgIWltcG9ydGFudDsgbWluLXdpZHRoOjQ0cHg7IHBhZGRpbmc6N3B4IDlweCAhaW1wb3J0YW50OyBib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTM7IGJvcmRlci1yYWRpdXM6OXB4OyBiYWNrZ3JvdW5kOiMyMDI4MzM7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgdGV4dC1hbGlnbjpyaWdodCAhaW1wb3J0YW50OyB3aGl0ZS1zcGFjZTpub3dyYXAgIWltcG9ydGFudDsgZm9udC1zaXplOjEzcHggIWltcG9ydGFudDsgbGluZS1oZWlnaHQ6MS4yICFpbXBvcnRhbnQ7IGZvbnQtd2VpZ2h0OjkwMCAhaW1wb3J0YW50OyBmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7IH0KI3BhZ2UtY2xhbnMgLmNhcmQgeyBtaW4td2lkdGg6MDsgb3ZlcmZsb3c6aGlkZGVuOyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCB7IGdhcDoxMnB4OyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCBoMyB7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8geyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gLmdyb3cgeyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gaDMgeyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IG92ZXJmbG93LXdyYXA6YW55d2hlcmU7IH0KI3BhZ2UtY2xhbnMgLmNsYW5oZXJvIHAsICNwYWdlLWNsYW5zIC5zZWN0aW9uaGVhZCBwIHsgY29sb3I6I2MyY2NkNiAhaW1wb3J0YW50OyB9Cmh0bWwsIGJvZHkgeyB3aWR0aDoxMDAlOyBtYXgtd2lkdGg6MTAwJTsgb3ZlcmZsb3cteDpoaWRkZW47IH0KaW1nLCBzdmcsIHZpZGVvLCBjYW52YXMsIGJ1dHRvbiwgaW5wdXQsIHNlbGVjdCwgdGV4dGFyZWEgeyBtYXgtd2lkdGg6MTAwJTsgfQpAbWVkaWEgKG1heC13aWR0aDo2MDBweCkgewogLnNlY3Rpb24geyBwYWRkaW5nLWxlZnQ6MTRweDsgcGFkZGluZy1yaWdodDoxNHB4OyB9CiAuc2VjdGlvbmhlYWQgeyBhbGlnbi1pdGVtczpmbGV4LXN0YXJ0OyBmbGV4LXdyYXA6d3JhcDsgZ2FwOjEwcHg7IH0KIC5zZWN0aW9uaGVhZCBoMiB7IGZvbnQtc2l6ZToyM3B4OyBsaW5lLWhlaWdodDoxLjI7IH0KIC5zZWN0aW9uaGVhZCBwIHsgZm9udC1zaXplOjEzcHg7IGxpbmUtaGVpZ2h0OjEuNTsgfQogLmxheW91dCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczptaW5tYXgoMCwxZnIpICFpbXBvcnRhbnQ7IGdhcDoxNHB4OyB9CiAuY2FyZCB7IG1pbi13aWR0aDowOyBwYWRkaW5nOjE1cHg7IH0KIC5jbGFuaGVybyB7IHBhZGRpbmc6MTRweCAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBmbGV4LXdyYXA6d3JhcDsgfQogLmNsYW5sb2dvIHsgd2lkdGg6NTJweCAhaW1wb3J0YW50OyBoZWlnaHQ6NTJweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA1MnB4OyBmb250LXNpemU6MjVweCAhaW1wb3J0YW50OyB9CiAuY2xhbmhlcm8gLmdyb3cgeyBmbGV4OjEgMSBjYWxjKDEwMCUgLSA3NXB4KTsgfQogLmNsYW5oZXJvIGgzIHsgZm9udC1zaXplOjE3cHggIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBjb2x1bW4tZ2FwOjEwcHggIWltcG9ydGFudDsgcGFkZGluZzoxMnB4IDAgIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgYiB7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IH0KICNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAuZWxvIHsgbWluLXdpZHRoOjQycHg7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IHBhZGRpbmc6N3B4ICFpbXBvcnRhbnQ7IH0KIC5tYXBncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpOyB9Cn0KCgovKiBNYXRjaCBDZW50ZXIg4oCUIGRlZGljYXRlZCBjb21wZXRpdGl2ZSBmbG93ICovCi5tYXRjaC1jZW50ZXJ7ZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXI6MXB4IHNvbGlkICMzYTQxNGI7Ym9yZGVyLXJhZGl1czoxOHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMSwjMGUxMjE3KTtib3gtc2hhZG93OjAgMThweCA0NXB4ICMwMDA1fQoubWF0Y2gtY2VudGVyLnZpc2libGV7ZGlzcGxheTpibG9ja30ubWMtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjE0cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTdweCAxOXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyOTMxM2E7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTAwZGVnLCMyNDE1MGYsIzE0MTkxZil9Lm1jLXRpdGxle2ZvbnQtd2VpZ2h0Ojk1MDtmb250LXNpemU6MThweH0ubWMtc3Vie2ZvbnQtc2l6ZToxMnB4O2NvbG9yOiNhZWI4YzM7bWFyZ2luLXRvcDo0cHh9Lm1jLXN0YXR1c3tib3JkZXI6MXB4IHNvbGlkICM3MDQwMWY7Y29sb3I6I2ZmYWQ2OTtiYWNrZ3JvdW5kOiMzNTFlMTM7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo2cHggOXB4O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjkwMDtsZXR0ZXItc3BhY2luZzouN3B4O3doaXRlLXNwYWNlOm5vd3JhcH0ubWMtYm9keXtwYWRkaW5nOjE4cHg7ZGlzcGxheTpncmlkO2dhcDoxNnB4fS5tYy1wcm9ncmVzc3toZWlnaHQ6N3B4O2JvcmRlci1yYWRpdXM6OTlweDtiYWNrZ3JvdW5kOiMyNTJjMzQ7b3ZlcmZsb3c6aGlkZGVufS5tYy1wcm9ncmVzcyBzcGFue2Rpc3BsYXk6YmxvY2s7aGVpZ2h0OjEwMCU7d2lkdGg6MDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjZmY0ZTBiLCNmZjlhMjcpO3RyYW5zaXRpb246d2lkdGggLjJzfS5tYy1wbGF5ZXJncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtcGxheWVye21pbi13aWR0aDowO2JvcmRlcjoxcHggc29saWQgIzMwMzk0NDtiYWNrZ3JvdW5kOiMxNzFkMjQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7dGV4dC1hbGlnbjpjZW50ZXJ9Lm1jLXBsYXllci5hY2NlcHRlZHtib3JkZXItY29sb3I6IzIzNzk1NztiYWNrZ3JvdW5kOiMxMTI3MWZ9Lm1jLXBsYXllciBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfS5tYy1wbGF5ZXIgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojOThhNWIxO2ZvbnQtc2l6ZTo5cHg7bWFyZ2luLXRvcDo0cHh9Lm1jLXBsYXllciAubWMtYXZhdGFye3dpZHRoOjI4cHg7aGVpZ2h0OjI4cHg7Ym9yZGVyLXJhZGl1czo5cHg7bWFyZ2luOjAgYXV0byA2cHg7ZGlzcGxheTpncmlkO3BsYWNlLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMzMDIwMTY7Y29sb3I6I2ZmOWI1MDtmb250LXdlaWdodDo5NTB9Lm1jLWFjdGlvbnN7ZGlzcGxheTpmbGV4O2dhcDo5cHg7ZmxleC13cmFwOndyYXB9Lm1jLWFjdGlvbnMgLmJ0bi1wcmltYXJ5LC5tYy1hY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2ZsZXg6MTttaW4td2lkdGg6MTMwcHh9Lm1jLXR3b3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9Lm1jLXBhbmVse2JvcmRlcjoxcHggc29saWQgIzJiMzMzZDtib3JkZXItcmFkaXVzOjEzcHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6IzExMTYxY30ubWMtcGFuZWwgaDR7bWFyZ2luOjAgMCA5cHg7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjhweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmOWE0ZH0ubWMtbXV0ZWR7Y29sb3I6IzlkYTliNTtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6NnB4O21hcmdpbjo5cHggMH0ubWMtZmllbGQgbGFiZWx7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNTtsZXR0ZXItc3BhY2luZzouN3B4O2ZvbnQtd2VpZ2h0OjgwMH0ubWMtZmllbGQgc2VsZWN0LC5tYy1maWVsZCBpbnB1dCwubWMtZmllbGQgdGV4dGFyZWF7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMwYjBmMTM7Ym9yZGVyOjFweCBzb2xpZCAjMzQzZDQ3O2JvcmRlci1yYWRpdXM6OHB4O2NvbG9yOiNmNGY2Zjg7cGFkZGluZzoxMHB4O21pbi13aWR0aDowfS5tYy1tYXBze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtbWFwe2JvcmRlcjoxcHggc29saWQgIzMzM2Q0ODtiYWNrZ3JvdW5kOiMxODFmMjc7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMXB4fS5tYy1tYXA6aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZjc4MmF9Lm1jLW1hcC51c2Vke29wYWNpdHk6LjM1O3BvaW50ZXItZXZlbnRzOm5vbmU7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaH0ubWMtbG9ne2Rpc3BsYXk6Z3JpZDtnYXA6NnB4fS5tYy1sb2dyb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7Zm9udC1zaXplOjExcHg7cGFkZGluZzo4cHggOXB4O2JvcmRlci1yYWRpdXM6OHB4O2JhY2tncm91bmQ6IzFhMjAyOH0ubWMtbG9ncm93IHNwYW46bGFzdC1jaGlsZHtjb2xvcjojZmZhZDY5fS5tYy10ZWFte2JvcmRlcjoxcHggc29saWQgIzJlMzk0NDtiYWNrZ3JvdW5kOiMxNTFiMjI7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTJweH0ubWMtdGVhbSBoNHttYXJnaW46MCAwIDhweDtmb250LXNpemU6MTJweH0ubWMtdGVhbSB1bHttYXJnaW46MDtwYWRkaW5nLWxlZnQ6MThweDtjb2xvcjojYzNjYmQzO2ZvbnQtc2l6ZToxMnB4O2xpbmUtaGVpZ2h0OjEuOX0ubWMtZm9ybWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo5cHh9Lm1jLW5vdGljZXtib3JkZXI6MXB4IHNvbGlkICMzYzMwMjM7YmFja2dyb3VuZDojMjQxYjE0O2NvbG9yOiNlNmM5YWQ7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6MTBweDtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWhpZGRlbntkaXNwbGF5Om5vbmUhaW1wb3J0YW50fQpAbWVkaWEobWF4LXdpZHRoOjYwMHB4KXsubWMtYm9keXtwYWRkaW5nOjEzcHh9Lm1jLXBsYXllcmdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg1LG1pbm1heCgwLDFmcikpO2dhcDo0cHh9Lm1jLXBsYXllcntwYWRkaW5nOjhweCAycHh9Lm1jLXBsYXllciBie2ZvbnQtc2l6ZTo5cHh9Lm1jLW1hcHN7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5tYy10d297Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0ubWMtaGVhZHtwYWRkaW5nOjE0cHh9Lm1jLXRpdGxle2ZvbnQtc2l6ZToxNnB4fX0KCjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9InNoZWxsIj4KPGFzaWRlIGNsYXNzPSJzaWRlYmFyIj4KICA8ZGl2IGNsYXNzPSJicmFuZCI+PGRpdiBjbGFzcz0iYnJhbmRtYXJrIj5TPC9kaXY+PGRpdj48ZGl2IGNsYXNzPSJicmFuZG5hbWUiPlNUQU5EPHNwYW4+UE9JTlQ8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0iYnJhbmR0YWciPlNUQU5ET0ZGIDIg4oCiIENPTVBFVElUSVZFPC9kaXY+PC9kaXY+PC9kaXY+CiAgPGRpdiBjbGFzcz0ibmF2Z3JvdXAiPjxsYWJlbD7Qn9Cb0JDQotCk0J7QoNCc0JA8L2xhYmVsPjxuYXYgY2xhc3M9Im5hdiIgaWQ9ImRlc2t0b3BOYXYiPgogICAgPGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLXBhZ2U9ImhvbWUiPjxzcGFuIGNsYXNzPSJuaSI+4oyCPC9zcGFuPtCT0LvQsNCy0L3QsNGPPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0ibWF0Y2hlcyI+PHNwYW4gY2xhc3M9Im5pIj7impQ8L3NwYW4+0JzQsNGC0YfQuDwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InJhdGluZyI+PHNwYW4gY2xhc3M9Im5pIj7imZw8L3NwYW4+0KDQtdC50YLQuNC90LMgRUxPPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0iY2xhbnMiPjxzcGFuIGNsYXNzPSJuaSI+4pmiPC9zcGFuPtCa0LvQsNC90YsgPHNwYW4gY2xhc3M9Im5hdmNvdW50Ij5ORVc8L3NwYW4+PC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0icGFzcyI+PHNwYW4gY2xhc3M9Im5pIj7inKY8L3NwYW4+U1RBTkRQT0lOVCBQYXNzPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0iZnJpZW5kcyI+PHNwYW4gY2xhc3M9Im5pIj7imac8L3NwYW4+0JTRgNGD0LfRjNGPPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0ibm90aWZpY2F0aW9ucyI+PHNwYW4gY2xhc3M9Im5pIj7imac8L3NwYW4+0KPQstC10LTQvtC80LvQtdC90LjRjyA8c3BhbiBjbGFzcz0ibmF2Y291bnQiPjI8L3NwYW4+PC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0icHJvZmlsZSI+PHNwYW4gY2xhc3M9Im5pIj7il4k8L3NwYW4+0J/RgNC+0YTQuNC70Yw8L2J1dHRvbj4KICA8L25hdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJzaWRlYmFyLWZvb3QiPjxkaXYgY2xhc3M9InVzZXJjYXJkIj48ZGl2IGNsYXNzPSJhdmF0YXIiPlM8L2Rpdj48ZGl2PjxiIGlkPSJzaWRlTmFtZSI+c3RhbmRwb2ludF9wbGF5ZXI8L2I+PHNtYWxsPuKXjyDQlNCV0JzQni3Qn9Cg0J7QpNCY0JvQrDwvc21hbGw+PC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7QotCS0J7QryDQmNCT0KDQkC4g0KLQktCe0K8g0J/QntCX0JjQptCY0K8uPC9wPjwvZGl2Pgo8L2FzaWRlPgo8bWFpbiBjbGFzcz0ibWFpbiI+CjxoZWFkZXIgY2xhc3M9InRvcGJhciI+PGRpdj48ZGl2IGNsYXNzPSJraWNrZXIiPlNUQU5ET0ZGIDIg4oCiIEVTUE9SVFMgUExBVEZPUk08L2Rpdj48aDEgaWQ9InBhZ2VUaXRsZSI+0KLQstC+0Y8g0LjQs9GA0LAuINCi0LLQvtGPINC/0L7Qt9C40YbQuNGPLjwvaDE+PC9kaXY+PGRpdiBjbGFzcz0idG9wYWN0aW9ucyI+PHNwYW4gaWQ9ImFwaVN0YXR1cyIgY2xhc3M9ImJhZGdlLW9yYW5nZSI+QVBJOiDQv9C+0LTQutC70Y7Rh9C10L3QuNC14oCmPC9zcGFuPjxidXR0b24gY2xhc3M9Imljb25idG4iIGRhdGEtcGFnZT0ibm90aWZpY2F0aW9ucyIgYXJpYS1sYWJlbD0i0KPQstC10LTQvtC80LvQtdC90LjRjyI+4pmnPGkgY2xhc3M9InBpbmciPjwvaT48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBkYXRhLXBhZ2U9InByb2ZpbGUiPtCc0L7QuSDQv9GA0L7RhNC40LvRjCDihpc8L2J1dHRvbj48L2Rpdj48L2hlYWRlcj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIGFjdGl2ZSIgaWQ9InBhZ2UtaG9tZSI+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiPgogIDxkaXYgY2xhc3M9InN0YWNrIj4KICAgPGRpdiBjbGFzcz0iaGVybyI+PGRpdiBjbGFzcz0iaGVyby1jb3B5Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQktCV0KDQqNCY0J3QlTwvZGl2PjxoMj7QmNCz0YDQsNC5INGD0LzQvdC10LUuPGJyPjxzcGFuPtCf0L7QtNC90LjQvNCw0LkgRUxPLjwvc3Bhbj48L2gyPjxwPtCd0LDRhdC+0LTQuCDQutC+0LzQsNC90LTRgywg0L/QvtCx0LXQttC00LDQuSDQsiDRgNC10LnRgtC40L3Qs9C+0LLRi9GFINC80LDRgtGH0LDRhSDQuCDQtNC+0LrQsNC30YvQstCw0LksINC60YLQviDQt9C00LXRgdGMINGB0LjQu9GM0L3QtdC50YjQuNC5LjwvcD48ZGl2IGNsYXNzPSJoZXJvLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBkYXRhLXBhZ2U9Im1hdGNoZXMiPuKalCDQndCw0LnRgtC4INC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgZGF0YS1wYWdlPSJyYXRpbmciPtCi0LDQsdC70LjRhtCwINC70LjQtNC10YDQvtCyPC9idXR0b24+PC9kaXY+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0KLQktCe0JkgRUxPPC9sYWJlbD48c3Ryb25nIGlkPSJob21lRWxvIj4xMDAwPC9zdHJvbmc+PHNtYWxsPuKXjyDQodGC0LDRgNGC0L7QstGL0Lkg0YDQtdC50YLQuNC90LM8L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxsYWJlbD7Qn9Ce0JHQldCU0Ks8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXaW5zIj4wPC9zdHJvbmc+PHNtYWxsPtCc0LDRgtGH0Lgg0YHQtdC30L7QvdCwPC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+V0lOIFJBVEU8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXUiI+MCU8L3N0cm9uZz48c21hbGw+0J7QsdGJ0LjQuSDQv9C+0LrQsNC30LDRgtC10LvRjDwvc21hbGw+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JHQu9C40LbQsNC50YjQuNC5INC80LDRgtGHPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJtYXRjaGVzIj7QktGB0LUg0LzQsNGC0YfQuCDihpI8L2J1dHRvbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaHJvdyI+PGRpdiBjbGFzcz0ibWF0Y2hsb2dvIj7impQ8L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaGNvcHkiPjxiPtCg0LXQudGC0LjQvdCz0L7QstGL0Lkg0LzQsNGC0YcgwrcgNXY1PC9iPjxzbWFsbD5TYW5kc3RvbmUgwrcgQ29tcGV0aXRpdmUgwrcg0J7QttC40LTQsNC90LjQtSDQuNCz0YDQvtC60L7Qsjwvc21hbGw+PC9kaXY+PHNwYW4gY2xhc3M9InBpbGwiPkxJVkUgUVVFVUU8L3NwYW4+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QtdC00LDQstC90LjQtSDRgNC10LfRg9C70YzRgtCw0YLRizwvaDM+PHNwYW4gY2xhc3M9Im11dGVkIHNtYWxsIj7QodCV0JfQntCdIDAzPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7inKY8L2Rpdj48ZGl2PjxiPtCd0L7QstGL0Lkg0YHQtdC30L7QvSDQvdCw0YfQsNC70YHRjzwvYj48cD7Qn9C+0LTQvdC40LzQsNC5INGA0LXQudGC0LjQvdCzINC4INC+0YLQutGA0YvQstCw0Lkg0L3QsNCz0YDQsNC00YsgU1RBTkRQT0lOVCBQYXNzLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pqUPC9kaXY+PGRpdj48Yj7QoNC10LnRgtC40L3Qs9C+0LLQsNGPINC+0YfQtdGA0LXQtNGMINC+0YLQutGA0YvRgtCwPC9iPjxwPtCh0L7QsdC40YDQsNC5INC60L7QvNCw0L3QtNGDINC4INCy0YHRgtGD0L/QsNC5INCyINCx0L7QuS48L3A+PC9kaXY+PC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ic3RhY2siPgogICA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QodCV0JfQntCdIDAzPC9kaXY+PGgzPlNUQU5EUE9JTlQgUEFTUzwvaDM+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1sZXZlbCIgaWQ9InBhc3NMZXZlbCI+MDE8L2Rpdj48L2Rpdj48cD7QmNCz0YDQsNC5INC80LDRgtGH0LgsINC30LDRgNCw0LHQsNGC0YvQstCw0LkgWFAg0Lgg0L7RgtC60YDRi9Cy0LDQuSDQvdCw0LPRgNCw0LTRiyDRgdC10LfQvtC90LAuPC9wPjxkaXYgY2xhc3M9InByb2dyZXNzIj48c3BhbiBpZD0icGFzc1Byb2dyZXNzIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1mb290Ij48c3BhbiBpZD0icGFzc1hQIj4xMjAgLyAzMDAgWFA8L3NwYW4+PGJ1dHRvbiBjbGFzcz0idGV4dGxpbmsiIGRhdGEtcGFnZT0icGFzcyI+0J7RgtC60YDRi9GC0YwgUGFzcyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotC+0L8g0LjQs9GA0L7QutC+0LI8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBkYXRhLXBhZ2U9InJhdGluZyI+0JLRgdC1INGA0LXQudGC0LjQvdCz0Lgg4oaSPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImhvbWVMZWFkZXJzIj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotCy0L7QuSDQv9GA0L7Qs9GA0LXRgdGBPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5TRUFTT04gMDM8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hyb3ciPjxkaXYgY2xhc3M9Im1hdGNobG9nbyI+4pmcPC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hjb3B5Ij48Yj7Qn9C+0LTQvdC40LzQsNC50YHRjyDQsiDRgNC10LnRgtC40L3Qs9C1PC9iPjxzbWFsbD7QmtCw0LbQtNCw0Y8g0L/QvtCx0LXQtNCwINC/0YDQuNCx0LvQuNC20LDQtdGCINC6INGN0LvQuNGC0LUuPC9zbWFsbD48L2Rpdj48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJyYXRpbmciPkVMTyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KIDwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtbWF0Y2hlcyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0JzQsNGC0YfQvNC10LnQutC40L3QszwvaDI+PHA+0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0LrQsNGA0YLRgy4g0J3QsNC50LTQuCDRgdC+0L/QtdGA0L3QuNC60L7QsiDRgdCy0L7QtdCz0L4g0YPRgNC+0LLQvdGPLjwvcD48L2Rpdj48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5DT01QRVRJVElWRTwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9ImxheW91dCI+CiAgPGRpdiBjbGFzcz0ic3RhY2siPjxkaXYgY2xhc3M9InF1ZXVlYm94Ij48ZGl2IGNsYXNzPSJxdWV1ZWljb24iIGlkPSJxdWV1ZUljb24iPuKalDwvZGl2PjxkaXYgY2xhc3M9ImV5ZWJyb3ciPtCf0J7QmNCh0Jog0JzQkNCi0KfQkDwvZGl2PjxoMyBpZD0icXVldWVUaXRsZSI+0JPQvtGC0L7QsiDQuiDQuNCz0YDQtT88L2gzPjxwIGlkPSJxdWV1ZURlc2NyaXB0aW9uIj7QktGL0LHQtdGA0Lgg0YDQtdC20LjQvCDQuCDQvdCw0YfQvdC4INC/0L7QuNGB0Log0YDQtdC50YLQuNC90LPQvtCy0L7Qs9C+INC80LDRgtGH0LAuPC9wPjxkaXYgY2xhc3M9Im1vZGVncmlkIiBpZD0ibW9kZUdyaWQiPjxidXR0b24gY2xhc3M9Im1vZGUiIGRhdGEtbW9kZT0iMXYxIj4xdjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtb2RlIiBkYXRhLW1vZGU9IjJ2MiI+MnYyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibW9kZSIgZGF0YS1tb2RlPSIzdjMiPjN2MzwvYnV0dG9uPjxidXR0b24gY2xhc3M9Im1vZGUgYWN0aXZlIiBkYXRhLW1vZGU9IjV2NSI+NXY1PC9idXR0b24+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJxdWV1ZUJ1dHRvbiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQudGC0Lgg0LzQsNGC0Yc8L2J1dHRvbj48cCBjbGFzcz0iZm9vdG5vdGUiIGlkPSJxdWV1ZUFwaU5vdGUiPtCf0L7QuNGB0Log0Lgg0YHRgtCw0YLRg9GBINC+0YfQtdGA0LXQtNC4INGB0LjQvdGF0YDQvtC90LjQt9C40YDRg9GO0YLRgdGPINGBINGB0LXRgNCy0LXRgNC+0LwgU1RBTkRQT0lOVC48L3A+PC9kaXY+CiAgPGRpdiBjbGFzcz0ibWF0Y2gtY2VudGVyIiBpZD0ibWF0Y2hDZW50ZXIiIGFyaWEtbGl2ZT0icG9saXRlIj4KICAgPGRpdiBjbGFzcz0ibWMtaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5TVEFORFBPSU5UIENPTVBFVElUSVZFPC9kaXY+PGRpdiBjbGFzcz0ibWMtdGl0bGUiIGlkPSJtY1RpdGxlIj7QnNCw0YLRhyDQvdCw0LnQtNC10L08L2Rpdj48ZGl2IGNsYXNzPSJtYy1zdWIiIGlkPSJtY1N1YnRpdGxlIj7Qn9C+0LTRgtCy0LXRgNC00Lgg0YPRh9Cw0YHRgtC40LUsINGH0YLQvtCx0Ysg0L/QtdGA0LXQudGC0Lgg0LIg0LvQvtCx0LHQuC48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0ibWMtc3RhdHVzIiBpZD0ibWNTdGF0dXMiPk1BVENIIEZPVU5EPC9zcGFuPjwvZGl2PgogICA8ZGl2IGNsYXNzPSJtYy1ib2R5Ij4KICAgIDxkaXYgaWQ9Im1jQWNjZXB0UGFuZWwiPjxkaXYgY2xhc3M9Im1jLXByb2dyZXNzIj48c3BhbiBpZD0ibWNBY2NlcHRQcm9ncmVzcyI+PC9zcGFuPjwvZGl2PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQWNjZXB0VGV4dCI+0J7QttC40LTQsNC10Lwg0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8g0LjQs9GA0L7QutC+0LLigKY8L3A+PGRpdiBjbGFzcz0ibWMtcGxheWVyZ3JpZCIgaWQ9Im1jQWNjZXB0UGxheWVycyI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyIgc3R5bGU9Im1hcmdpbi10b3A6MTJweCI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJtY0FjY2VwdEJ0biI+4pyTINCf0YDQuNC90Y/RgtGMINC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9Im1jRGVjbGluZUJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJtY0JhblBhbmVsIiBjbGFzcz0ibWMtaGlkZGVuIj4KICAgICA8ZGl2IGNsYXNzPSJtYy10d28iPjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQTwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5BIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQSIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEE8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5BSW5mbyI+PC9wPjwvZGl2PjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQjwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5CIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEI8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5CSW5mbyI+PC9wPjwvZGl2PjwvZGl2PgogICAgIDxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQgaWQ9Im1jQmFuVGl0bGUiPkJhbiAvIFBpY2sg0LrQsNGA0YI8L2g0PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQmFuSGludCI+0JrQsNC/0LjRgtCw0L3RiyDQv9C+INC+0YfQtdGA0LXQtNC4INCx0LvQvtC60LjRgNGD0Y7RgiDQutCw0YDRgtGLLCDQt9Cw0YLQtdC8INCy0YvQsdC40YDQsNGO0YIg0LrQsNGA0YLRgyDQvNCw0YLRh9CwLjwvcD48ZGl2IGNsYXNzPSJtYy1tYXBzIiBpZD0ibWNNYXBCdXR0b25zIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1sb2ciIGlkPSJtY01hcExvZyIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9Im1jTG9iYnlQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCDQmtC+0LzQsNC90LTQsCBBPC9oND48dWwgaWQ9Im1jVGVhbUEiPjwvdWw+PC9kaXY+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCflLUg0JrQvtC80LDQvdC00LAgQjwvaDQ+PHVsIGlkPSJtY1RlYW1CIj48L3VsPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLW5vdGljZSIgaWQ9Im1jTG9iYnlJbmZvIj7QmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwLiDQntC20LjQtNCw0LXQvCDQt9Cw0L/Rg9GB0Log0LzQsNGC0YfQsCDQutCw0L/QuNGC0LDQvdC+0Lwg0LrQvtC80LDQvdC00YsgQS48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9Im1jU3RhcnRCdG4iIHN0eWxlPSJ3aWR0aDoxMDAlO21hcmdpbi10b3A6MTBweCI+4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNQbGF5aW5nUGFuZWwiIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLW5vdGljZSI+0JzQsNGC0Ycg0L/RgNC+0YXQvtC00LjRgiDQsiBTdGFuZG9mZiAyLiDQn9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINCy0L3QtdGB0Lgg0YHRh9GR0YIg0L7QsdC10LjRhSDQutC+0LzQsNC90LQg4oCUINGA0LXQt9GD0LvRjNGC0LDRgiDRg9C50LTRkdGCINC80L7QtNC10YDQsNGC0L7RgNGDINC90LAg0L/RgNC+0LLQtdGA0LrRgywgRUxPINC90LUg0LjQt9C80LXQvdC40YLRgdGPINC00L4g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPC9kaXY+PGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQTwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQjwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QotCS0J7QmCDQo9CR0JjQmdCh0KLQktCQIChLSUxMUyk8L2xhYmVsPjxpbnB1dCBpZD0ibWNLaWxscyIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwMCIgdmFsdWU9IjAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KLQktCe0Jgg0KHQnNCV0KDQotCYIChERUFUSFMpPC9sYWJlbD48aW5wdXQgaWQ9Im1jRGVhdGhzIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAwIiB2YWx1ZT0iMCI+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QmtCe0JzQnNCV0J3QotCQ0KDQmNCZINCaINCg0JXQl9Cj0JvQrNCi0JDQotCjPC9sYWJlbD48dGV4dGFyZWEgaWQ9Im1jUmVzdWx0Tm90ZSIgcm93cz0iMiIgcGxhY2Vob2xkZXI9ItCd0LDQv9GA0LjQvNC10YA6INGB0LrRgNC40L3RiNC+0YIg0YDQtdC30YPQu9GM0YLQsNGC0LAg0LHRg9C00LXRgiDQv9GA0LjQu9C+0LbQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDIj48L3RleHRhcmVhPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0ibWNTdWJtaXRSZXN1bHQiIHN0eWxlPSJ3aWR0aDoxMDAlIj7QntGC0L/RgNCw0LLQuNGC0Ywg0YDQtdC30YPQu9GM0YLQsNGCINC90LAg0L/RgNC+0LLQtdGA0LrRgzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNSZXZpZXdQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtbm90aWNlIiBpZD0ibWNSZXZpZXdJbmZvIj7QoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgyDQvNC+0LTQtdGA0LDRgtC+0YDRgy4g0KDQtdC50YLQuNC90LMg0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS48L2Rpdj48ZGl2IGlkPSJtY0FwcGVhbEZvcm0iIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0J/QoNCY0KfQmNCd0JAg0JDQn9CV0JvQm9Cv0KbQmNCYPC9sYWJlbD48c2VsZWN0IGlkPSJtY0FwcGVhbFJlYXNvbiI+PG9wdGlvbiB2YWx1ZT0id3Jvbmdfc2NvcmUiPtCd0LXQstC10YDQvdGL0Lkg0YHRh9GR0YI8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJjaGVhdGluZyI+0J/QvtC00L7Qt9GA0LXQvdC40LUg0L3QsCDQvdC10YfQtdGB0YLQvdGD0Y4g0LjQs9GA0YM8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJ0ZWNobmljYWxfaXNzdWUiPtCi0LXRhdC90LjRh9C10YHQutCw0Y8g0L/RgNC+0LHQu9C10LzQsDwvb3B0aW9uPjxvcHRpb24gdmFsdWU9Im90aGVyIj7QlNGA0YPQs9C+0LU8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCf0J7QlNCg0J7QkdCd0J7QodCi0Jg8L2xhYmVsPjx0ZXh0YXJlYSBpZD0ibWNBcHBlYWxEZXRhaWxzIiByb3dzPSIzIiBwbGFjZWhvbGRlcj0i0J7Qv9C40YjQuCDQv9GA0L7QsdC70LXQvNGDINC00LvRjyDQvNC+0LTQtdGA0LDRgtC+0YDQsCI+PC90ZXh0YXJlYT48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTdWJtaXRBcHBlYWwiIHN0eWxlPSJ3aWR0aDoxMDAlIj7Qn9C+0LTQsNGC0Ywg0LDQv9C10LvQu9GP0YbQuNGOPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J/RgNCw0LLQuNC70LAg0YDQtdC50YLQuNC90LPQsDwvaDM+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+RUxPPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7ihpc8L2Rpdj48ZGl2PjxiPtCf0L7QsdC10LbQtNCw0Lkg4oCUINC/0L7Qu9GD0YfQsNC5INGA0LXQudGC0LjQvdCzPC9iPjxwPtCg0LXQt9GD0LvRjNGC0LDRgiDQvNCw0YLRh9CwINCy0LvQuNGP0LXRgiDQvdCwINGC0LLQvtC5IEVMTy48L3A+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm90aWNlIj48ZGl2IGNsYXNzPSJub3RpY2VpY28iPuKcpjwvZGl2PjxkaXY+PGI+0JjQs9GA0LDQuSDRgNC10LPRg9C70Y/RgNC90L48L2I+PHA+0JfQsNGA0LDQsdCw0YLRi9Cy0LDQuSDQvtC/0YvRgiDQtNC70Y8g0YHQtdC30L7QvdC90L7Qs9C+IFBhc3MuPC9wPjwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JLRi9Cx0L7RgCDQutCw0YDRgtGLPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCf0KDQldCU0J/QntCn0KLQldCd0JjQlTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXBncmlkIiBpZD0ibWFwR3JpZCI+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSBzZWxlY3RlZCIgZGF0YS1tYXA9IlNhbmRzdG9uZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+c77iPPC9zcGFuPjwvZGl2PjxiPlNhbmRzdG9uZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUHJvdmluY2UiPjxkaXYgY2xhc3M9Im1hcGFydCI+PHNwYW4+8J+Pme+4jzwvc3Bhbj48L2Rpdj48Yj5Qcm92aW5jZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUnVzdCI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+tPC9zcGFuPjwvZGl2PjxiPlJ1c3Q8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IkJyZWV6ZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4yKPC9zcGFuPjwvZGl2PjxiPkJyZWV6ZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iWm9uZSA3Ij48ZGl2IGNsYXNzPSJtYXBhcnQiPjxzcGFuPuKYou+4jzwvc3Bhbj48L2Rpdj48Yj5ab25lIDc8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IlNha3VyYSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y4PC9zcGFuPjwvZGl2PjxiPlNha3VyYTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iRHVuZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7im7DvuI88L3NwYW4+PC9kaXY+PGI+RHVuZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iQ2FseXBzbyI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y0PC9zcGFuPjwvZGl2PjxiPkNhbHlwc288L2I+PC9idXR0b24+PC9kaXY+PC9kaXY+CiA8L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXJhdGluZyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0KDQtdC50YLQuNC90LMg0LjQs9GA0L7QutC+0LI8L2gyPjxwPtCU0L7QutCw0LbQuCDRgdCy0L7QuSDRg9GA0L7QstC10L3RjC4g0J/QvtC00L3QuNC80LDQudGB0Y8g0LIg0YLQsNCx0LvQuNGG0LUg0LvQuNC00LXRgNC+0LIuPC9wPjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNFQVNPTiAwMzwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYnMiIGlkPSJyYXRpbmdUYWJzIj48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdGFiPSJzZWFzb24iPtCh0LXQt9C+0L0gMDM8L2J1dHRvbj48YnV0dG9uIGRhdGEtdGFiPSJhbGwiPtCX0LAg0LLRgdGRINCy0YDQtdC80Y88L2J1dHRvbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYmxld3JhcCI+PHRhYmxlIGNsYXNzPSJ0YWJsZSI+PHRoZWFkPjx0cj48dGg+IzwvdGg+PHRoPtCY0JPQoNCe0Jo8L3RoPjx0aD5FTE88L3RoPjx0aD7Qn9Ce0JHQldCU0Ks8L3RoPjx0aD5XSU4gUkFURTwvdGg+PC90cj48L3RoZWFkPjx0Ym9keSBpZD0icmF0aW5nUm93cyI+PC90Ym9keT48L3RhYmxlPjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSI+0KLQsNCx0LvQuNGG0LAg0YHQvtC00LXRgNC20LjRgiDQtNC10LzQvtC90YHRgtGA0LDRhtC40L7QvdC90YvQtSDQtNCw0L3QvdGL0LUuINCf0L7QtNC60LvRjtGH0LggYmFja2VuZCwg0YfRgtC+0LHRiyDQvtGC0L7QsdGA0LDQttCw0YLRjCDRgNC10LDQu9GM0L3Ri9C5INGA0LXQudGC0LjQvdCzINC40LPRgNC+0LrQvtCyLjwvcD4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXBhc3MiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPlNUQU5EUE9JTlQgUGFzczwvaDI+PHA+0KHQtdC30L7QvSAwMyDCtyDQmNCz0YDQsNC5LCDQt9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQLCDQt9Cw0LHQuNGA0LDQuSDQvdCw0LPRgNCw0LTRiy48L3A+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+0JDQmtCi0JjQktCV0J08L3NwYW4+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQrdCb0JjQotCVPC9kaXY+PGgyIHN0eWxlPSJtYXJnaW46OHB4IDA7Zm9udC1zaXplOjI4cHgiPtCh0LXQt9C+0L3QvdGL0LUg0L3QsNCz0YDQsNC00Ys8L2gyPjxwPtCX0LDRgNCw0LHQsNGC0YvQstCw0Lkg0L7Qv9GL0YIg0LfQsCDQv9C+0LTRgtCy0LXRgNC20LTRkdC90L3Ri9C1INC80LDRgtGH0LguPC9wPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtbGV2ZWwiIHN0eWxlPSJmb250LXNpemU6NDZweCIgaWQ9InBhc3NMZXZlbEJpZyI+MDE8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJwcm9ncmVzcyI+PHNwYW4gaWQ9InBhc3NQcm9ncmVzc0JpZyI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtZm9vdCI+PHNwYW4gaWQ9InBhc3NYUEJpZyI+MTIwIC8gMzAwIFhQPC9zcGFuPjxzcGFuPtCj0YDQvtCy0LXQvdGMIDEg0LjQtyAzMDwvc3Bhbj48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QsNCz0YDQsNC00Ysg0YHQtdC30L7QvdCwPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPjMg0J/QoNCV0JTQnNCV0KLQkDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0cyI+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfjpbvuI88L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0J3QvtCy0L7QsdGA0LDQvdC10YY8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMSDCtyDQn9C+0LvRg9GH0LXQvdC+PC9zbWFsbD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGNsYWltIiBzdHlsZT0ibWFyZ2luLXRvcDoxMnB4O3dpZHRoOjEwMCU7Zm9udC1zaXplOjEwcHgiIGRhdGEtcmV3YXJkPSLQndC+0LLQvtCx0YDQsNC90LXRhiI+0J/QvtC70YPRh9C10L3QvjwvYnV0dG9uPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyN3B4Ij7wn5SlPC9kaXY+PHN0cm9uZyBzdHlsZT0iZm9udC1zaXplOjE1cHgiPtCd0LAg0L/QvtC00YrRkdC80LU8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgNTwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0J3QsCDQv9C+0LTRitGR0LzQtSI+0J/RgNC+0LLQtdGA0LjRgtGMPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfkZE8L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0K3Qu9C40YLQsDwvc3Ryb25nPjxzbWFsbD7Qo9GA0L7QstC10L3RjCAxMDwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0K3Qu9C40YLQsCI+0JfQsNCx0LvQvtC60LjRgNC+0LLQsNC90L48L2J1dHRvbj48L2Rpdj48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLWNsYW5zIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QmtC70LDQvdGLPC9oMj48cD7QodC+0LHQtdGA0Lgg0LrQvtC80LDQvdC00YMuINCX0LDRidC40YnQsNC5INGB0LLQvtC5INGC0LXQsy4g0J/QvtCx0LXQttC00LDQuSDQsiBDbGFuIFdhcnMuPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY3JlYXRlQ2xhbkJ0biI+77yLINCh0L7Qt9C00LDRgtGMINC60LvQsNC9PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjbGFuaGVybyI+PGRpdiBjbGFzcz0iY2xhbmxvZ28iPvCfkLo8L2Rpdj48ZGl2IGNsYXNzPSJncm93Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQmtCb0JDQnTwvZGl2PjxoMyBpZD0iY2xhbk5hbWUiPkZyb3N0IFdvbHZlcyA8c3BhbiBpZD0iY2xhblRhZyIgY2xhc3M9Im11dGVkIHNtYWxsIj4jRlc8L3NwYW4+PC9oMz48cD4xMiDRg9GH0LDRgdGC0L3QuNC60L7QsiDCtyAxOTgwIENsYW4gRUxPPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkiIGlkPSJjbGFuRGV0YWlscyI+0J/QvtC00YDQvtCx0L3QtdC1PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0KHQvtGB0YLQsNCyINC60LvQsNC90LA8L2gzPjxzcGFuIGNsYXNzPSJtdXRlZCBzbWFsbCI+MTIgLyAyMDwvc3Bhbj48L2Rpdj48ZGl2IGlkPSJjbGFuTWVtYmVycyIgY2xhc3M9ImxlYWRlcmxpc3QiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+Q2xhbiBXYXJzPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5MSVZFIExPQkJZPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWFyb3VuZDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE4cHggMCI+PGRpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MzVweCI+8J+boe+4jzwvZGl2PjxiIGlkPSJ3YXJPd25OYW1lIj7QotCS0J7QmSDQmtCb0JDQnTwvYj48L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MjFweDtjb2xvcjojZmY5ODRkIj5WUzwvc3Ryb25nPjxkaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjM1cHgiPuKalO+4jzwvZGl2PjxiPtCh0J7Qn9CV0KDQndCY0Jo8L2I+PC9kaXY+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIiBpZD0id2FyU3RhdHVzVGV4dCI+0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LrQu9Cw0L3RgyDQv9C+INC10LPQviBJRCDQuNC70Lgg0L7RgtC60YDQvtC5INC40YHRgtC+0YDQuNGOLjwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0id2FyQnV0dG9uIiBzdHlsZT0id2lkdGg6MTAwJSI+0J7RgtC60YDRi9GC0Ywg0LjRgdGC0L7RgNC40Y4gQ2xhbiBXYXJzPC9idXR0b24+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHdhci1sb2JieS1jYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4O2JvcmRlci1jb2xvcjojNTEzMjFmO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzMGRlZywjMWQxNzE0LCMxMTE2MWMpIj48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5DTEFOIFdBUiBPUEVSQVRJT05TPC9kaXY+PGgzIHN0eWxlPSJtYXJnaW4tdG9wOjVweCI+0JvQvtCx0LHQuCDQutC70LDQvdC+0LLQvtC5INCy0L7QudC90Ys8L2gzPjwvZGl2PjxzcGFuIGNsYXNzPSJtYy1zdGF0dXMiPjV2NSDCtyBCTzE8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntGC0L/RgNCw0LLRjCDQstGL0LfQvtCyINC00YDRg9Cz0L7QvNGDINC60LvQsNC90YMuINCf0L7RgdC70LUg0L/RgNC40L3Rj9GC0LjRjyDQv9C+0Y/QstC40YLRgdGPINC90LDRgdGC0L7Rj9GJ0LXQtSDQu9C+0LHQsdC4INGBINGB0L7RgdGC0LDQstCw0LzQuCDQuCDQvtGC0L/RgNCw0LLQutC+0Lkg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L3QsCDQvNC+0LTQtdGA0LDRhtC40Y4uPC9wPjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCa0JvQkNCdLdCh0J7Qn9CV0KDQndCY0Jo8L2xhYmVsPjxzZWxlY3QgaWQ9Indhck9wcG9uZW50SWQiPjxvcHRpb24gdmFsdWU9IiI+0JfQsNCz0YDRg9C20LDQtdC8INGB0L/QuNGB0L7QuiDQutC70LDQvdC+0LLigKY8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPklEINCi0JXQmtCj0KnQldCZINCS0J7QmdCd0Ks8L2xhYmVsPjxpbnB1dCBpZD0id2FySWRJbnB1dCIgcGxhY2Vob2xkZXI9ItCf0L7Rj9Cy0LjRgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LfQvtCy0LAiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY2hhbGxlbmdlV2FyQnRuIj7impQg0J7RgtC/0YDQsNCy0LjRgtGMINCy0YvQt9C+0LI8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibG9hZFdhckxvYmJ5QnRuIj7QntGC0LrRgNGL0YLRjCDQu9C+0LHQsdC4PC9idXR0b24+PC9kaXY+PGRpdiBpZD0id2FyTG9iYnlDb250ZW50IiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibWMtbXV0ZWQiPtCf0L7QutCwINC90LXRgiDQsNC60YLQuNCy0L3QvtCz0L4g0LvQvtCx0LHQuC4g0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LjQu9C4INCy0YHRgtCw0LLRjCBJRCDRgdGD0YnQtdGB0YLQstGD0Y7RidC10Lkg0LLQvtC50L3Riy48L3A+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIiBpZD0icGFnZS1mcmllbmRzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QlNGA0YPQt9GM0Y88L2gyPjxwPtCf0YDQuNCz0LvQsNGI0LDQuSDQuNCz0YDQvtC60L7QsiDQsiDQutC+0LzQsNC90LTRgyDQuCDQstC80LXRgdGC0LUg0L/QvtC00L3QuNC80LDQuSBFTE8uPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWRkRnJpZW5kQnRuIj7vvIsg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7Qn9Ce0JjQodCaINCf0J4g0J3QmNCa0J3QldCZ0JzQozwvbGFiZWw+PGlucHV0IGlkPSJmcmllbmRTZWFyY2giIHBsYWNlaG9sZGVyPSLQndCw0L/RgNC40LzQtdGALCBzaDF6byI+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImZyaWVuZHNMaXN0Ij48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLW5vdGlmaWNhdGlvbnMiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCj0LLQtdC00L7QvNC70LXQvdC40Y88L2gyPjxwPtCc0LDRgtGH0LgsINC/0YDQuNCz0LvQsNGI0LXQvdC40Y8g0Lgg0YHQvtCx0YvRgtC40Y8g0L/Qu9Cw0YLRhNC+0YDQvNGLLjwvcD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0icmVhZEFsbCI+0J/RgNC+0YfQuNGC0LDRgtGMINCy0YHQtTwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCIgaWQ9Im5vdGljZUxpc3QiPjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtcHJvZmlsZSI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwPC9oMj48cD7QotCy0L7RjyDRgdGC0LDRgtC40YHRgtC40LrQsCwg0YDQtdC50YLQuNC90LMg0Lgg0LjQs9GA0L7QstC+0Lkg0L/RgNC+0LPRgNC10YHRgS48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJwcm9maWxlaGVybyI+PGRpdiBjbGFzcz0icHJvZmlsZWF2YXRhciI+4pmfPC9kaXY+PGRpdj48aDMgaWQ9InByb2ZpbGVOYW1lIj5zdGFuZHBvaW50X3BsYXllcjwvaDM+PHAgaWQ9InByb2ZpbGVJZCI+SUQ6IGRlbW9fMDAxPC9wPjxzcGFuIGNsYXNzPSJwaWxsIiBpZD0icHJvZmlsZVN0YXR1cyI+4pePINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InByb2ZpbGVlbG8iPjxzbWFsbD7QotCV0JrQo9Cp0JjQmSBFTE88L3NtYWxsPjxzdHJvbmcgaWQ9InByb2ZpbGVFbG8iPjEwMDA8L3N0cm9uZz48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0JzQkNCi0KfQmDwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZU1hdGNoZXMiPjA8L3N0cm9uZz48c21hbGw+0JLRgdC10LPQviDRgdGL0LPRgNCw0L3Qvjwvc21hbGw+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGxhYmVsPtCf0J7QkdCV0JTQqzwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZVdpbnMiPjA8L3N0cm9uZz48c21hbGw+0J/QvtC00YLQstC10YDQttC00LXQvdC+PC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+Sy9EPC9sYWJlbD48c3Ryb25nIGlkPSJwcm9maWxlS0QiPuKAlDwvc3Ryb25nPjxzbWFsbD7Qo9Cx0LjQudGB0YLQstCwIC8g0YHQvNC10YDRgtC4PC9zbWFsbD48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JTQsNC90L3Ri9C1INC/0YDQvtGE0LjQu9GPPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCh0JXQoNCS0JXQoCAvINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCd0JjQmtCd0JXQmdCcPC9sYWJlbD48aW5wdXQgaWQ9ImVkaXROYW1lIiBtYXhsZW5ndGg9IjI0IiB2YWx1ZT0ic3RhbmRwb2ludF9wbGF5ZXIiPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JjQk9Cg0J7QktCe0JkgSUQ8L2xhYmVsPjxpbnB1dCBpZD0iZWRpdEdhbWVJZCIgbWF4bGVuZ3RoPSIzMiIgdmFsdWU9ImRlbW9fMDAxIj48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9InNhdmVQcm9maWxlIj7QodC+0YXRgNCw0L3QuNGC0Ywg0LjQt9C80LXQvdC10L3QuNGPPC9idXR0b24+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9GA0L7RhNC40LvRjCDRgdC+0YXRgNCw0L3Rj9C10YLRgdGPINC90LAg0YHQtdGA0LLQtdGA0LUsINC10YHQu9C4IEFQSSDQv9C+0LTQutC70Y7Rh9GR0L0g0LggVGVsZWdyYW0gSUQg0LTQvtGB0YLRg9C/0LXQvS48L3A+PC9kaXY+Cjwvc2VjdGlvbj4KPC9tYWluPgo8L2Rpdj4KPG5hdiBjbGFzcz0iYm90dG9tbmF2IiBpZD0iYm90dG9tTmF2Ij48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtcGFnZT0iaG9tZSI+PHNwYW4+4oyCPC9zcGFuPtCT0LvQsNCy0L3QsNGPPC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9Im1hdGNoZXMiPjxzcGFuPuKalDwvc3Bhbj7QnNCw0YLRh9C4PC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9InJhdGluZyI+PHNwYW4+4pmcPC9zcGFuPtCg0LXQudGC0LjQvdCzPC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9ImNsYW5zIj48c3Bhbj7imaI8L3NwYW4+0JrQu9Cw0L3RizwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJwcm9maWxlIj48c3Bhbj7il4k8L3NwYW4+0J/RgNC+0YTQuNC70Yw8L2J1dHRvbj48L25hdj4KPGRpdiBpZD0idG9hc3RSb290IiBhcmlhLWxpdmU9InBvbGl0ZSI+PC9kaXY+CjxzY3JpcHQ+CmNvbnN0ICQ9KHMscj1kb2N1bWVudCk9PnIucXVlcnlTZWxlY3RvcihzKSwkJD0ocyxyPWRvY3VtZW50KT0+Wy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CmNvbnN0IGRlZmF1bHRzPXtuYW1lOiJzdGFuZHBvaW50X3BsYXllciIsZ2FtZUlkOiJkZW1vXzAwMSIsZWxvOjEwMDAsd2luczowLGxvc3NlczowLGRyYXdzOjAsa2lsbHM6MCxkZWF0aHM6MCx4cDoxMjAsZnJpZW5kczpbe25hbWU6InNoMXpvIixzdGF0dXM6ItCb0LXQs9C10L3QtNCwIixlbG86MjQxMH0se25hbWU6Incxbm5lciIsc3RhdHVzOiLQkiDQuNCz0YDQtSIsZWxvOjIzODd9LHtuYW1lOiJ6eGNydXNlZCIsc3RhdHVzOiLQndC1INCyINGB0LXRgtC4IixlbG86MjMzMn1dLG5vdGljZXM6W3tpY29uOiLinKYiLHRpdGxlOiLQlNC+0LHRgNC+INC/0L7QttCw0LvQvtCy0LDRgtGMINCyIFNUQU5EUE9JTlQiLGJvZHk6ItCi0LLQvtC5INGB0L7RgNC10LLQvdC+0LLQsNGC0LXQu9GM0L3Ri9C5INC/0YPRgtGMINC90LDRh9C40L3QsNC10YLRgdGPINC30LTQtdGB0YwuIixyZWFkOmZhbHNlfSx7aWNvbjoi4pqUIix0aXRsZToi0KDQtdC50YLQuNC90LPQvtCy0LDRjyDQvtGH0LXRgNC10LTRjCDQvtGC0LrRgNGL0YLQsCIsYm9keToi0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0L3QsNC50LTQuCDQvNCw0YLRhy4iLHJlYWQ6ZmFsc2V9LHtpY29uOiLwn46WIix0aXRsZToiU1RBTkRQT0lOVCBQYXNzINCw0LrRgtC40LLQtdC9Iixib2R5OiLQl9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQINC30LAg0LzQsNGC0YfQuCDQuCDQvtGC0LrRgNGL0LLQsNC5INC90LDQs9GA0LDQtNGLLiIscmVhZDp0cnVlfV19OwpsZXQgc3RhdGU7dHJ5e3N0YXRlPXsuLi5kZWZhdWx0cywuLi5KU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCJzdGFuZHBvaW50RGVtb1YyIil8fCJ7fSIpfTt9Y2F0Y2h7c3RhdGU9ey4uLmRlZmF1bHRzfTt9CmxldCBzZWxlY3RlZE1vZGU9IjV2NSIsc2VsZWN0ZWRNYXA9IlNhbmRzdG9uZSIscXVldWVUaW1lcj1udWxsLHF1ZXVlU2Vjb25kcz0wLHF1ZXVlQWN0aXZlPWZhbHNlOwpjb25zdCBsZWFkZXJzPVt7bmFtZToic2gxem8iLGVsbzoyNDEwLHdpbnM6MTUyLHdyOjc0fSx7bmFtZToidzFubmVyIixlbG86MjM4Nyx3aW5zOjEzOSx3cjo3MX0se25hbWU6Inp4Y3J1c2VkIixlbG86MjMzMix3aW5zOjEyOCx3cjo2OX0se25hbWU6ImRvbms2NjYiLGVsbzoyMjkxLHdpbnM6MTE3LHdyOjY4fSx7bmFtZToieW91MW5nIixlbG86MjI1Nix3aW5zOjExMSx3cjo2Nn1dOwpmdW5jdGlvbiBlc2Mocyl7cmV0dXJuIFN0cmluZyhzKS5yZXBsYWNlKC9bJjw+IiddL2csYz0+KHsiJiI6IiZhbXA7IiwiPCI6IiZsdDsiLCI+IjoiJmd0OyIsJyInOiImcXVvdDsiLCInIjoiJiMzOTsifVtjXSkpO30KZnVuY3Rpb24gc2F2ZSgpe2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJzdGFuZHBvaW50RGVtb1YyIixKU09OLnN0cmluZ2lmeShzdGF0ZSkpO30KZnVuY3Rpb24gdG9hc3Qocyl7Y29uc3Qgcm9vdD0kKCIjdG9hc3RSb290Iik7cm9vdC5pbm5lckhUTUw9IiI7Y29uc3QgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCJkaXYiKTtkLmNsYXNzTmFtZT0idG9hc3QiO2QudGV4dENvbnRlbnQ9cztyb290LmFwcGVuZChkKTtzZXRUaW1lb3V0KCgpPT5kLnJlbW92ZSgpLDMyMDApO30KZnVuY3Rpb24gbmF2KHBhZ2UpeyQkKCIuc2VjdGlvbiIpLmZvckVhY2gocz0+cy5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHMuaWQ9PT0icGFnZS0iK3BhZ2UpKTskJCgiW2RhdGEtcGFnZV0iKS5mb3JFYWNoKGI9PmIuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIixiLmRhdGFzZXQucGFnZT09PXBhZ2UpKTtjb25zdCB0PXtob21lOiLQotCy0L7RjyDQuNCz0YDQsC4g0KLQstC+0Y8g0L/QvtC30LjRhtC40Y8uIixtYXRjaGVzOiLQnNCw0YLRh9C80LXQudC60LjQvdCzIixyYXRpbmc6ItCg0LXQudGC0LjQvdCzIEVMTyIsY2xhbnM6ItCa0LvQsNC90Ysg0LggQ2xhbiBXYXJzIixwYXNzOiJTVEFORFBPSU5UIFBhc3MiLGZyaWVuZHM6ItCU0YDRg9C30YzRjyIsbm90aWZpY2F0aW9uczoi0KPQstC10LTQvtC80LvQtdC90LjRjyIscHJvZmlsZToi0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwIn07JCgiI3BhZ2VUaXRsZSIpLnRleHRDb250ZW50PXRbcGFnZV18fCJTVEFORFBPSU5UIjt3aW5kb3cuc2Nyb2xsVG8oe3RvcDowLGJlaGF2aW9yOiJzbW9vdGgifSk7fQokJCgiW2RhdGEtcGFnZV0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5uYXYoYi5kYXRhc2V0LnBhZ2UpKSk7CmZ1bmN0aW9uIHJlbmRlckxlYWRlcnMoKXtjb25zdCBhcnI9Wy4uLmxlYWRlcnMse25hbWU6c3RhdGUubmFtZSxlbG86c3RhdGUuZWxvLHdpbnM6c3RhdGUud2lucyx3cjpzdGF0ZS53aW5zK3N0YXRlLmxvc3Nlcz9NYXRoLnJvdW5kKHN0YXRlLndpbnMvKHN0YXRlLndpbnMrc3RhdGUubG9zc2VzKSoxMDApOjB9XS5zb3J0KChhLGIpPT5iLmVsby1hLmVsbyk7JCgiI2hvbWVMZWFkZXJzIikuaW5uZXJIVE1MPWFyci5zbGljZSgwLDQpLm1hcCgocCxpKT0+YDxkaXYgY2xhc3M9ImxlYWRlciI+PHNwYW4gY2xhc3M9InJhbmsiPiR7aSsxfTwvc3Bhbj48ZGl2IGNsYXNzPSJwbGF5ZXIiPjxkaXYgY2xhc3M9Im1pbmlhdmEiPiR7ZXNjKHAubmFtZVswXS50b1VwcGVyQ2FzZSgpKX08L2Rpdj48ZGl2PjxiPiR7ZXNjKHAubmFtZSl9PC9iPjxzbWFsbD4ke2k9PT0wPyLQm9C10LPQtdC90LTQsCI6aT09PTE/ItCc0LDRgdGC0LXRgCI6ItCY0LPRgNC+0LoifTwvc21hbGw+PC9kaXY+PC9kaXY+PHNwYW4gY2xhc3M9ImVsbyI+JHtwLmVsb308L3NwYW4+PC9kaXY+YCkuam9pbigiIik7JCgiI3JhdGluZ1Jvd3MiKS5pbm5lckhUTUw9YXJyLm1hcCgocCxpKT0+YDx0ciBjbGFzcz0iJHtwLm5hbWU9PT1zdGF0ZS5uYW1lPyJ5b3UiOiIifSI+PHRkPiR7aSsxfTwvdGQ+PHRkPjxiPiR7ZXNjKHAubmFtZSl9PC9iPjwvdGQ+PHRkPjxiPiR7cC5lbG99PC9iPjwvdGQ+PHRkPiR7cC53aW5zfTwvdGQ+PHRkPiR7cC53cn0lPC90ZD48L3RyPmApLmpvaW4oIiIpO30KZnVuY3Rpb24gcmVuZGVyRnJpZW5kcygpe2NvbnN0IHE9KCQoIiNmcmllbmRTZWFyY2giKT8udmFsdWV8fCIiKS50b0xvd2VyQ2FzZSgpOyQoIiNmcmllbmRzTGlzdCIpLmlubmVySFRNTD1zdGF0ZS5mcmllbmRzLmZpbHRlcihmPT5mLm5hbWUudG9Mb3dlckNhc2UoKS5pbmNsdWRlcyhxKSkubWFwKGY9PmA8ZGl2IGNsYXNzPSJsZWFkZXIiPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MoZi5uYW1lWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MoZi5uYW1lKX08L2I+PHNtYWxsPiR7ZXNjKGYuc3RhdHVzKX0gwrcgRUxPICR7Zi5lbG99PC9zbWFsbD48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGludml0ZSIgZGF0YS1uYW1lPSIke2VzYyhmLm5hbWUpfSI+0J/RgNC40LPQu9Cw0YHQuNGC0Yw8L2J1dHRvbj48L2Rpdj5gKS5qb2luKCIiKXx8JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0JjQs9GA0L7QutC4INC90LUg0L3QsNC50LTQtdC90YsuPC9wPic7JCQoIi5pbnZpdGUiKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT50b2FzdCgi0JTQtdC80L46INC/0YDQuNCz0LvQsNGI0LXQvdC40LUg0LTQu9GPICIrYi5kYXRhc2V0Lm5hbWUrIiDQvdC1INC+0YLQv9GA0LDQstC70LXQvdC+INC90LAg0YHQtdGA0LLQtdGALiIpKSk7fQpmdW5jdGlvbiByZW5kZXJOb3RpY2VzKCl7JCgiI25vdGljZUxpc3QiKS5pbm5lckhUTUw9c3RhdGUubm90aWNlcy5tYXAoKG4saSk9PmA8ZGl2IGNsYXNzPSJub3RpY2UiIHN0eWxlPSIke24ucmVhZD8ib3BhY2l0eTouODIiOiIifSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj4ke2VzYyhuLmljb24pfTwvZGl2PjxkaXYgc3R5bGU9ImZsZXg6MSI+PGI+JHtlc2Mobi50aXRsZSl9PC9iPjxwPiR7ZXNjKG4uYm9keSl9PC9wPjxzbWFsbCBjbGFzcz0ibXV0ZWQiPiR7bi5yZWFkPyLQn9GA0L7Rh9C40YLQsNC90L4iOiLQndC+0LLQvtC1In08L3NtYWxsPjwvZGl2PiR7bi5yZWFkPyIiOmA8YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayByZWFkTm90aWNlIiBkYXRhLWluZGV4PSIke2l9Ij7Qn9GA0L7Rh9C40YLQsNC90L48L2J1dHRvbj5gfTwvZGl2PmApLmpvaW4oIiIpOyQkKCIucmVhZE5vdGljZSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2NvbnN0IG49c3RhdGUubm90aWNlc1srYi5kYXRhc2V0LmluZGV4XTt0cnl7aWYobi5pZCYmYXBpVXNlcklkKWF3YWl0IGFwaShgL25vdGlmaWNhdGlvbnMvJHtlbmNvZGVVUklDb21wb25lbnQobi5pZCl9L3JlYWRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZH19KTtuLnJlYWQ9dHJ1ZTtyZW5kZXJOb3RpY2VzKCk7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC+0YLQvNC10YLQuNGC0Yw6ICIrZS5tZXNzYWdlKTt9fSkpO30KZnVuY3Rpb24gcmVuZGVyKCl7Y29uc3QgdG90YWw9c3RhdGUud2lucytzdGF0ZS5sb3NzZXMrc3RhdGUuZHJhd3Msd3I9dG90YWw/TWF0aC5yb3VuZChzdGF0ZS53aW5zL3RvdGFsKjEwMCk6MCxsZXZlbD1NYXRoLmZsb29yKHN0YXRlLnhwLzMwMCkrMSxwY3Q9KHN0YXRlLnhwJTMwMCkvMzskKCIjc2lkZU5hbWUiKS50ZXh0Q29udGVudD1zdGF0ZS5uYW1lOyQoIiNob21lRWxvIikudGV4dENvbnRlbnQ9c3RhdGUuZWxvOyQoIiNob21lV2lucyIpLnRleHRDb250ZW50PXN0YXRlLndpbnM7JCgiI2hvbWVXUiIpLnRleHRDb250ZW50PXdyKyIlIjskKCIjcHJvZmlsZU5hbWUiKS50ZXh0Q29udGVudD1zdGF0ZS5uYW1lOyQoIiNwcm9maWxlSWQiKS50ZXh0Q29udGVudD0iSUQ6ICIrc3RhdGUuZ2FtZUlkKyIiOyQoIiNwcm9maWxlRWxvIikudGV4dENvbnRlbnQ9c3RhdGUuZWxvOyQoIiNwcm9maWxlTWF0Y2hlcyIpLnRleHRDb250ZW50PXRvdGFsOyQoIiNwcm9maWxlV2lucyIpLnRleHRDb250ZW50PXN0YXRlLndpbnM7JCgiI3Byb2ZpbGVLRCIpLnRleHRDb250ZW50PXN0YXRlLmRlYXRocz8oc3RhdGUua2lsbHMvc3RhdGUuZGVhdGhzKS50b0ZpeGVkKDIpOnN0YXRlLmtpbGxzPyLiiJ4iOiLigJQiOyQoIiNlZGl0TmFtZSIpLnZhbHVlPXN0YXRlLm5hbWU7JCgiI2VkaXRHYW1lSWQiKS52YWx1ZT1zdGF0ZS5nYW1lSWQ7JCgiI3Bhc3NMZXZlbCIpLnRleHRDb250ZW50PVN0cmluZyhsZXZlbCkucGFkU3RhcnQoMiwiMCIpOyQoIiNwYXNzTGV2ZWxCaWciKS50ZXh0Q29udGVudD1TdHJpbmcobGV2ZWwpLnBhZFN0YXJ0KDIsIjAiKTskKCIjcGFzc1hQIikudGV4dENvbnRlbnQ9KHN0YXRlLnhwJTMwMCkrIiAvIDMwMCBYUCI7JCgiI3Bhc3NYUEJpZyIpLnRleHRDb250ZW50PShzdGF0ZS54cCUzMDApKyIgLyAzMDAgWFAiOyQoIiNwYXNzUHJvZ3Jlc3MiKS5zdHlsZS53aWR0aD1wY3QrIiUiOyQoIiNwYXNzUHJvZ3Jlc3NCaWciKS5zdHlsZS53aWR0aD1wY3QrIiUiO3JlbmRlckxlYWRlcnMoKTtyZW5kZXJGcmllbmRzKCk7cmVuZGVyTm90aWNlcygpO3NhdmUoKTt9CiQkKCJbZGF0YS1tb2RlXSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PntpZihxdWV1ZUFjdGl2ZSl7dG9hc3QoItCh0L3QsNGH0LDQu9CwINC+0YLQvNC10L3QuCDRgtC10LrRg9GJ0LjQuSDQv9C+0LjRgdC6LCDRh9GC0L7QsdGLINGB0LzQtdC90LjRgtGMINGA0LXQttC40LwuIik7cmV0dXJuO31zZWxlY3RlZE1vZGU9Yi5kYXRhc2V0Lm1vZGU7JCQoIltkYXRhLW1vZGVdIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIseD09PWIpKTt9KSk7CiQkKCJbZGF0YS1tYXBdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+ewogc2VsZWN0ZWRNYXA9Yi5kYXRhc2V0Lm1hcDskJCgiW2RhdGEtbWFwXSIpLmZvckVhY2goeD0+eC5jbGFzc0xpc3QudG9nZ2xlKCJzZWxlY3RlZCIseD09PWIpKTsKIHRvYXN0KCLQmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwOiAiK3NlbGVjdGVkTWFwKyhhY3RpdmVNYXRjaElkPyIgwrcg0L7RgtC60YDQvtC5INC80LDRgtGHINC00LvRjyBiYW4tcGljayI6IiIpKTsKfSkpOwokKCIjcXVldWVCdXR0b24iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGlmKCFhcGlVc2VySWQpe3RvYXN0KCLQntGC0LrRgNC+0LkgU1RBTkRQT0lOVCDRh9C10YDQtdC3IFRlbGVncmFtINC4INGB0L7RhdGA0LDQvdC4INC/0YDQvtGE0LjQu9GMLiIpO25hdigicHJvZmlsZSIpO3JldHVybjt9CiB0cnl7CiAgaWYoIXF1ZXVlQWN0aXZlKXsKICAgY29uc3QgZD1hd2FpdCBhcGkoIi9xdWV1ZS9qb2luIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbW9kZTpzZWxlY3RlZE1vZGV9fSk7CiAgIGlmKGQubWF0Y2hfaWQpe2FjdGl2ZU1hdGNoSWQ9ZC5tYXRjaF9pZDtxdWV1ZUFjdGl2ZT1mYWxzZTtzdG9wUXVldWVQb2xsaW5nKCk7YXdhaXQgc2hvd01hdGNoKGFjdGl2ZU1hdGNoSWQpO3JldHVybjt9CiAgIHF1ZXVlQWN0aXZlPWQuc3RhdHVzPT09InF1ZXVlZCI7JCgiI3F1ZXVlQnV0dG9uIikudGV4dENvbnRlbnQ9cXVldWVBY3RpdmU/ItCe0YLQvNC10L3QuNGC0Ywg0L/QvtC40YHQuiI6ItCd0LDQudGC0Lgg0LzQsNGC0YciOwogICAkKCIjcXVldWVUaXRsZSIpLnRleHRDb250ZW50PSLQmNGJ0LXQvCDRgdC+0L/QtdGA0L3QuNC60L7QsuKApiI7CiAgICQoIiNxdWV1ZURlc2NyaXB0aW9uIikudGV4dENvbnRlbnQ9YNCSINC+0YfQtdGA0LXQtNC4ICR7ZC5wbGF5ZXJzX2ZvdW5kfS8ke2QucGxheWVyc19uZWVkZWR9OyDQvtGB0YLQsNC70L7RgdGMICR7ZC5wbGF5ZXJzX3JlbWFpbmluZ31gOwogICB0b2FzdCgi0J/QvtC40YHQuiDQt9Cw0L/Rg9GJ0LXQvS4g0KHRh9GR0YLRh9C40Log0YHQuNC90YXRgNC+0L3QuNC30LjRgNGD0LXRgtGB0Y8g0YEg0YHQtdGA0LLQtdGA0L7QvC4iKTtzdGFydFF1ZXVlUG9sbGluZygpOwogIH1lbHNlewogICBhd2FpdCBhcGkoIi9xdWV1ZS9sZWF2ZSIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkfX0pOwogICBxdWV1ZUFjdGl2ZT1mYWxzZTtzdG9wUXVldWVQb2xsaW5nKCk7JCgiI3F1ZXVlQnV0dG9uIikudGV4dENvbnRlbnQ9ItCd0LDQudGC0Lgg0LzQsNGC0YciOyQoIiNxdWV1ZVRpdGxlIikudGV4dENvbnRlbnQ9ItCf0L7QuNGB0Log0L7RgtC80LXQvdGR0L0iOyQoIiNxdWV1ZURlc2NyaXB0aW9uIikudGV4dENvbnRlbnQ9ItCi0Ysg0LLRi9GI0LXQuyDQuNC3INC+0YfQtdGA0LXQtNC4LiI7dG9hc3QoItCf0L7QuNGB0Log0L7RgtC80LXQvdGR0L0uIik7CiAgfQogfWNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0L7Rh9C10YDQtdC00Lg6ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjcmF0aW5nVGFicyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixlPT57Y29uc3QgYj1lLnRhcmdldC5jbG9zZXN0KCJbZGF0YS10YWJdIik7aWYoIWIpcmV0dXJuOyQkKCJbZGF0YS10YWJdIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIseD09PWIpKTt0b2FzdChiLmRhdGFzZXQudGFiPT09InNlYXNvbiI/ItCg0LXQudGC0LjQvdCzINGB0LXQt9C+0L3QsCAo0LTQtdC80L4pLiI6ItCe0LHRidC40Lkg0YDQtdC50YLQuNC90LMgKNC00LXQvNC+KS4iKTt9KTsKJCQoIi5jbGFpbSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PntpZihiLmRhdGFzZXQucmV3YXJkPT09ItCd0L7QstC+0LHRgNCw0L3QtdGGIil7dG9hc3QoItCt0YLQsCDQvdCw0LPRgNCw0LTQsCDRg9C20LUg0L/QvtC70YPRh9C10L3QsC4iKTtyZXR1cm47fWNvbnN0IG5lZWQ9Yi5kYXRhc2V0LnJld2FyZD09PSLQndCwINC/0L7QtNGK0ZHQvNC1Ij81OjEwLGxldmVsPU1hdGguZmxvb3Ioc3RhdGUueHAvMzAwKSsxO3RvYXN0KGxldmVsPG5lZWQ/ItCd0YPQttC10L0g0YPRgNC+0LLQtdC90YwgIituZWVkKyIuINCi0LLQvtC5INGD0YDQvtCy0LXQvdGMOiAiK2xldmVsOiLQndCw0LPRgNCw0LTRiyDQsdGD0LTRg9GCINC00L7RgdGC0YPQv9C90Ysg0L/QvtGB0LvQtSDQv9C+0LTQutC70Y7Rh9C10L3QuNGPIFBhc3MgQVBJLiIpO30pKTsKJCgiI3NhdmVQcm9maWxlIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBjb25zdCBuaWNrbmFtZT0kKCIjZWRpdE5hbWUiKS52YWx1ZS50cmltKCksZ2FtZV9pZD0kKCIjZWRpdEdhbWVJZCIpLnZhbHVlLnRyaW0oKTsKIGlmKCFuaWNrbmFtZXx8IWdhbWVfaWQpcmV0dXJuIHRvYXN0KCLQl9Cw0L/QvtC70L3QuCDQvdC40LrQvdC10LnQvCDQuCDQuNCz0YDQvtCy0L7QuSBJRC4iKTsKIGxldCBpZD1hcGlVc2VySWR8fHByb21wdCgi0JLQstC10LTQuCDRgdCy0L7QuSBUZWxlZ3JhbSBJRCDQtNC70Y8g0L/RgNC40LLRj9C30LrQuCDQsNC60LrQsNGD0L3RgtCwOiIpOwogaWYoIWlkKXJldHVybjthcGlVc2VySWQ9U3RyaW5nKGlkKS50cmltKCk7bG9jYWxTdG9yYWdlLnNldEl0ZW0oInN0YW5kcG9pbnRUZWxlZ3JhbUlkIixhcGlVc2VySWQpOwogdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKCIvcGxheWVycy9yZWdpc3RlciIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG5pY2tuYW1lLGdhbWVfaWR9fSk7YXBwbHlQbGF5ZXIoZC5wbGF5ZXIpO2F3YWl0IHJlZnJlc2hBcGlEYXRhKCk7dG9hc3QoItCf0YDQvtGE0LjQu9GMINGB0L7RhdGA0LDQvdGR0L0g0L3QsCDRgdC10YDQstC10YDQtS4iKTt9CiBjYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINC/0YDQvtGE0LjQu9GPOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2ZyaWVuZFNlYXJjaCIpLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IixyZW5kZXJGcmllbmRzKTsKJCgiI2FkZEZyaWVuZEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogY29uc3QgdGFyZ2V0X2lkPXByb21wdCgiVGVsZWdyYW0gSUQg0LjQs9GA0L7QutCwLCDQutC+0YLQvtGA0L7Qs9C+INGF0L7Rh9C10YjRjCDQtNC+0LHQsNCy0LjRgtGMOiIpOwogaWYoIXRhcmdldF9pZHx8IWFwaVVzZXJJZClyZXR1cm4gdG9hc3QoItCd0YPQttC10L0g0YLQstC+0LkgVGVsZWdyYW0gSUQg0LggSUQg0LTRgNGD0LPQsC4iKTsKIHRyeXthd2FpdCBhcGkoIi9mcmllbmRzL3JlcXVlc3QiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCx0YXJnZXRfaWQ6dGFyZ2V0X2lkLnRyaW0oKX19KTt0b2FzdCgi0JfQsNGP0LLQutCwINC+0YLQv9GA0LDQstC70LXQvdCwLiIpO2F3YWl0IHJlZnJlc2hBcGlEYXRhKCk7fQogY2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDQt9Cw0Y/QstC60Lg6ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjcmVhZEFsbCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFwaVVzZXJJZClyZXR1cm4gdG9hc3QoItCh0L3QsNGH0LDQu9CwINC/0L7QtNC60LvRjtGH0Lgg0L/RgNC+0YTQuNC70YwuIik7CiB0cnl7YXdhaXQgYXBpKGAvbm90aWZpY2F0aW9ucy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfS9yZWFkLWFsbGAse21ldGhvZDoiUE9TVCJ9KTthd2FpdCByZWZyZXNoQXBpRGF0YSgpO3RvYXN0KCLQo9Cy0LXQtNC+0LzQu9C10L3QuNGPINC+0YLQvNC10YfQtdC90Ysg0L/RgNC+0YfQuNGC0LDQvdC90YvQvNC4LiIpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LA6ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjY3JlYXRlQ2xhbkJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFwaVVzZXJJZClyZXR1cm4gdG9hc3QoItCh0L3QsNGH0LDQu9CwINC/0L7QtNC60LvRjtGH0Lgg0L/RgNC+0YTQuNC70Ywg0LIgVGVsZWdyYW0uIik7CiBjb25zdCBuYW1lPXByb21wdCgi0J3QsNC30LLQsNC90LjQtSDQutC70LDQvdCwICgz4oCTNDAg0YHQuNC80LLQvtC70L7Qsik6Iik7aWYoIW5hbWU/LnRyaW0oKSlyZXR1cm47CiBjb25zdCB0YWc9cHJvbXB0KCLQotC10LMg0LrQu9Cw0L3QsCAoMuKAkzgg0LvQsNGC0LjQvdGB0LrQuNGFINCx0YPQutCyL9GG0LjRhNGAKToiKTtpZighdGFnPy50cmltKCkpcmV0dXJuOwogY29uc3QgZGVzY3JpcHRpb249cHJvbXB0KCLQntC/0LjRgdCw0L3QuNC1INC60LvQsNC90LAgKNC90LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+KToiKXx8IiI7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoIi9jbGFucyIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG5hbWU6bmFtZS50cmltKCksdGFnOnRhZy50cmltKCksZGVzY3JpcHRpb259fSk7YWN0aXZlQ2xhbklkPWQuY2xhbl9pZDt0b2FzdCgi0JrQu9Cw0L0g0YHQvtC30LTQsNC9INC90LAg0YHQtdGA0LLQtdGA0LUuIik7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt9CiBjYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINGB0L7Qt9C00LDQvdC40Y8g0LrQu9Cw0L3QsDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNjbGFuRGV0YWlscyIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFjdGl2ZUNsYW5JZClyZXR1cm4gdG9hc3QoItCd0LXRgiDQstGL0LHRgNCw0L3QvdC+0LPQviDQutC70LDQvdCwLiDQodC+0LfQtNCw0Lkg0LrQu9Cw0L0g0LjQu9C4INC+0LHQvdC+0LLQuCDQtNCw0L3QvdGL0LUuIik7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jbGFucy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVDbGFuSWQpfWApO2FsZXJ0KGAke2QuY2xhbi5uYW1lfSBbJHtkLmNsYW4udGFnfV0gwrcgRUxPICR7ZC5jbGFuLmVsb31cblxuYCtkLm1lbWJlcnMubWFwKG09PmAke20ubmlja25hbWV9IOKAlCAke20ucm9sZX0g4oCUICR7bS5lbG99IEVMT2ApLmpvaW4oIlxuIikpO30KIGNhdGNoKGUpe3RvYXN0KGUubWVzc2FnZSk7fQp9KTsKJCgiI3dhckJ1dHRvbiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFjdGl2ZUNsYW5JZClyZXR1cm4gdG9hc3QoItCh0L3QsNGH0LDQu9CwINCy0YvQsdC10YDQuCDQuNC70Lgg0YHQvtC30LTQsNC5INC60LvQsNC9LiIpOwogdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvY2xhbi13YXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZUNsYW5JZCl9YCk7YWxlcnQoKGQud2Fyc3x8W10pLm1hcCh3PT5gJHt3Lndhcl9pZH0gwrcgJHt3LnN0YXR1c30gwrcgJHt3LnNjb3JlX2E/PyItIn06JHt3LnNjb3JlX2I/PyItIn1gKS5qb2luKCJcbiIpfHwi0JjRgdGC0L7RgNC40Y8gQ2xhbiBXYXJzINC/0YPRgdGC0LAuIik7fQogY2F0Y2goZSl7dG9hc3QoIkNsYW4gV2FyczogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNjbGFuTWVtYmVycyIpLmlubmVySFRNTD0nPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7Ql9Cw0LPRgNGD0LfQutCwINGB0L7RgdGC0LDQstCwINC60LvQsNC90LAg0YEg0YHQtdGA0LLQtdGA0LDigKY8L3A+JzsKCi8vID09PT09IFNUQU5EUE9JTlQgQVBJID09PT09CmNvbnN0IEFQSV9CQVNFPSIiOwpsZXQgYXBpVXNlcklkPSIiLGFjdGl2ZUNsYW5JZD0iIixhY3RpdmVNYXRjaElkPSIiLHF1ZXVlUG9sbFRpbWVyPW51bGw7CmZ1bmN0aW9uIGdldFRlbGVncmFtVXNlcigpe3RyeXtyZXR1cm4gd2luZG93LlRlbGVncmFtPy5XZWJBcHA/LmluaXREYXRhVW5zYWZlPy51c2VyfHxudWxsfWNhdGNoe3JldHVybiBudWxsfX0KZnVuY3Rpb24gc2V0QXBpU3RhdHVzKHRleHQsb2s9ZmFsc2Upe2NvbnN0IGU9JCgiI2FwaVN0YXR1cyIpO2lmKGUpe2UudGV4dENvbnRlbnQ9dGV4dDtlLnN0eWxlLmNvbG9yPW9rPyIjNzVmMGI2IjoiI2ZmYjRhOCI7fX0KZnVuY3Rpb24gcmVhZGFibGVBcGlFcnJvcihkLHN0YXR1cyl7CiBjb25zdCBkZXRhaWw9ZD8uZGV0YWlsfHxkPy5tZXNzYWdlOwogLy8g0J3QtSDQstGL0LLQvtC00LjQvCDQsiDQuNC90YLQtdGA0YTQtdC50YEg0L/QvtGC0LXQvdGG0LjQsNC70YzQvdC+INGB0LvQvtC80LDQvdC90YPRjiDQutC+0LTQuNGA0L7QstC60YMg0L7RgtCy0LXRgtCwINGB0LXRgNCy0LXRgNCwLgogaWYoc3RhdHVzPT09NDA0KXJldHVybiAi0L/RgNC+0YTQuNC70Ywg0LjQu9C4INGA0LXRgdGD0YDRgSDQvdC1INC90LDQudC00LXQvSAoNDA0KSI7CiBpZihzdGF0dXM9PT00MjIpcmV0dXJuICLRgdC10YDQstC10YAg0L7RgtC60LvQvtC90LjQuyDQtNCw0L3QvdGL0LUg0LfQsNC/0YDQvtGB0LAgKDQyMikiOwogaWYoc3RhdHVzPT09NDAxfHxzdGF0dXM9PT00MDMpcmV0dXJuICLRgdC10YDQstC10YAg0L3QtSDRgNCw0LfRgNC10YjQuNC7INC30LDQv9GA0L7RgSAoIitzdGF0dXMrIikiOwogaWYoc3RhdHVzPj01MDApcmV0dXJuICLQstC90YPRgtGA0LXQvdC90Y/RjyDQvtGI0LjQsdC60LAg0YHQtdGA0LLQtdGA0LAgKCIrc3RhdHVzKyIpIjsKIGlmKHR5cGVvZiBkZXRhaWw9PT0ic3RyaW5nIiAmJiAhL1vDkMORw4PDouKCrO+/vV0vLnRlc3QoZGV0YWlsKSlyZXR1cm4gZGV0YWlsOwogcmV0dXJuICLQvtGI0LjQsdC60LAg0LfQsNC/0YDQvtGB0LAgKEhUVFAgIitzdGF0dXMrIikiOwp9CmFzeW5jIGZ1bmN0aW9uIGFwaShwYXRoLHttZXRob2Q9IkdFVCIsYm9keT1udWxsfT17fSl7CiBjb25zdCBvcHRzPXttZXRob2QsaGVhZGVyczp7IkFjY2VwdCI6ImFwcGxpY2F0aW9uL2pzb24ifX07CiBpZihib2R5IT09bnVsbCl7b3B0cy5oZWFkZXJzWyJDb250ZW50LVR5cGUiXT0iYXBwbGljYXRpb24vanNvbiI7b3B0cy5ib2R5PUpTT04uc3RyaW5naWZ5KGJvZHkpO30KIGxldCByO3RyeXtyPWF3YWl0IGZldGNoKEFQSV9CQVNFK3BhdGgsb3B0cyl9Y2F0Y2goZSl7dGhyb3cgbmV3IEVycm9yKCLRgdC10YDQstC10YAg0L3QtdC00L7RgdGC0YPQv9C10L0g0LjQu9C4INCx0YDQsNGD0LfQtdGAINC30LDQsdC70L7QutC40YDQvtCy0LDQuyDQt9Cw0L/RgNC+0YEgKENPUlMv0YHQtdGC0YwpIik7fQogbGV0IGQ9e307dHJ5e2Q9YXdhaXQgci5qc29uKCl9Y2F0Y2h7fQogaWYoIXIub2spe2NvbnN0IGVycj1uZXcgRXJyb3IocmVhZGFibGVBcGlFcnJvcihkLHIuc3RhdHVzKSk7ZXJyLnN0YXR1cz1yLnN0YXR1cztlcnIuYXBpUGF0aD1wYXRoO3Rocm93IGVycjt9CiByZXR1cm4gZDsKfQpmdW5jdGlvbiBhcHBseVBsYXllcihwKXtpZighcClyZXR1cm47Y29uc3QgcHM9JCgiI3Byb2ZpbGVTdGF0dXMiKTtpZihwcylwcy50ZXh0Q29udGVudD0i4pePINCf0KDQntCk0JjQm9CsINChINCh0JXQoNCS0JXQoNCQIjtzZXRBcGlTdGF0dXMoIkFQSSDQvtC90LvQsNC50L0gwrcg0L/RgNC+0YTQuNC70Ywg0LfQsNCz0YDRg9C20LXQvSIsdHJ1ZSk7c3RhdGUubmFtZT1wLm5pY2tuYW1lfHxzdGF0ZS5uYW1lO3N0YXRlLmdhbWVJZD1wLmdhbWVfaWR8fHN0YXRlLmdhbWVJZDtzdGF0ZS5lbG89TnVtYmVyKHAuZWxvPz8xMDAwKTtzdGF0ZS53aW5zPU51bWJlcihwLndpbnM/PzApO3N0YXRlLmxvc3Nlcz1OdW1iZXIocC5sb3NzZXM/PzApO3N0YXRlLmRyYXdzPU51bWJlcihwLmRyYXdzPz8wKTtzdGF0ZS5raWxscz1OdW1iZXIocC5raWxscz8/MCk7c3RhdGUuZGVhdGhzPU51bWJlcihwLmRlYXRocz8/MCk7cmVuZGVyKCk7fQphc3luYyBmdW5jdGlvbiByZWZyZXNoQXBpRGF0YSgpewogaWYoIWFwaVVzZXJJZClyZXR1cm47CiBjb25zdCBqb2JzPVsKICBhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT5hcHBseVBsYXllcihkLnBsYXllcikpLAogIGFwaSgiL2xlYWRlcmJvYXJkP2xpbWl0PTUwIikudGhlbihkPT57CiAgIGNvbnN0IGE9KGQucGxheWVyc3x8W10pLm1hcCh4PT4oe25hbWU6eC5uaWNrbmFtZSxlbG86TnVtYmVyKHguZWxvfHwxMDAwKSx3aW5zOk51bWJlcih4LndpbnN8fDApLHdyOihOdW1iZXIoeC53aW5zfHwwKStOdW1iZXIoeC5sb3NzZXN8fDApKT9NYXRoLnJvdW5kKE51bWJlcih4LndpbnN8fDApLyhOdW1iZXIoeC53aW5zfHwwKStOdW1iZXIoeC5sb3NzZXN8fDApKSoxMDApOjB9KSk7CiAgIGlmKGEubGVuZ3RoKXskKCIjaG9tZUxlYWRlcnMiKS5pbm5lckhUTUw9YS5zbGljZSgwLDUpLm1hcCgoeCxpKT0+YDxkaXYgY2xhc3M9ImxlYWRlciI+PHNwYW4gY2xhc3M9InJhbmsiPiR7aSsxfTwvc3Bhbj48ZGl2IGNsYXNzPSJwbGF5ZXIiPjxkaXYgY2xhc3M9Im1pbmlhdmEiPiR7ZXNjKCh4Lm5hbWV8fCI/IilbMF0udG9VcHBlckNhc2UoKSl9PC9kaXY+PGRpdj48Yj4ke2VzYyh4Lm5hbWUpfTwvYj48c21hbGw+JHt4LndpbnN9INC/0L7QsdC10LQ8L3NtYWxsPjwvZGl2PjwvZGl2PjxzcGFuIGNsYXNzPSJlbG8iPiR7eC5lbG99PC9zcGFuPjwvZGl2PmApLmpvaW4oIiIpOyQoIiNyYXRpbmdSb3dzIikuaW5uZXJIVE1MPWEubWFwKCh4LGkpPT5gPHRyPjx0ZD4ke2krMX08L3RkPjx0ZD48Yj4ke2VzYyh4Lm5hbWUpfTwvYj48L3RkPjx0ZD48Yj4ke3guZWxvfTwvYj48L3RkPjx0ZD4ke3gud2luc308L3RkPjx0ZD4ke3gud3J9JTwvdGQ+PC90cj5gKS5qb2luKCIiKTt9CiAgfSksCiAgYXBpKGAvZnJpZW5kcy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApLnRoZW4oZD0+e3N0YXRlLmZyaWVuZHM9KGQuZnJpZW5kc3x8W10pLm1hcChmPT4oe25hbWU6Zi5uaWNrbmFtZSx0ZWxlZ3JhbV9pZDpmLnRlbGVncmFtX2lkLHN0YXR1czoi0JTRgNGD0LMiLGVsbzpOdW1iZXIoZi5lbG98fDEwMDApfSkpO3JlbmRlckZyaWVuZHMoKTt9KSwKICBhcGkoYC9ub3RpZmljYXRpb25zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57c3RhdGUubm90aWNlcz0oZC5ub3RpZmljYXRpb25zfHxbXSkubWFwKG49Pih7aWQ6bi5ub3RpZmljYXRpb25faWQsaWNvbjpuLmtpbmQ9PT0ibWF0Y2hfZm91bmQiPyLimpQiOiLinKYiLHRpdGxlOm4udGl0bGUsYm9keTpuLmJvZHkscmVhZDohIW4uaXNfcmVhZH0pKTtyZW5kZXJOb3RpY2VzKCk7fSksCiAgYXBpKGAvcGFzcy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApLnRoZW4oZD0+e2lmKGQuYWN0aXZlIT09ZmFsc2Upe3N0YXRlLnhwPU51bWJlcihkLnhwfHwwKTtyZW5kZXIoKTt9fSksCiAgYXBpKGAvcmV3YXJkcy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApLnRoZW4oZD0+ewogICBjb25zdCBlYXJuZWQ9bmV3IFNldCgoZC5yZXdhcmRzfHxbXSkubWFwKHI9PnIubmFtZSkpOwogICAkJCgiLmNsYWltIikuZm9yRWFjaChiPT57Y29uc3QgbmFtZT1iLmRhdGFzZXQucmV3YXJkO2lmKGVhcm5lZC5oYXMobmFtZSkpe2IudGV4dENvbnRlbnQ9ItCf0L7Qu9GD0YfQtdC90L4iO2IuZGlzYWJsZWQ9dHJ1ZTt9fSk7CiAgfSksCiAgYXBpKCIvY2xhbnM/bGltaXQ9NTAiKS50aGVuKGFzeW5jIGQ9PnsKICAgY29uc3QgY2xhbnM9ZC5jbGFuc3x8W107CiAgIGNvbnN0IG9wcG9uZW50U2VsZWN0PSQoIiN3YXJPcHBvbmVudElkIik7aWYob3Bwb25lbnRTZWxlY3Qpe2NvbnN0IG9wcG9uZW50cz1jbGFucy5maWx0ZXIoeD0+eC5jbGFuX2lkIT09YWN0aXZlQ2xhbklkKTtvcHBvbmVudFNlbGVjdC5pbm5lckhUTUw9JzxvcHRpb24gdmFsdWU9IiI+0JLRi9Cx0LXRgNC4INC60LvQsNC9LdGB0L7Qv9C10YDQvdC40LrigKY8L29wdGlvbj4nK29wcG9uZW50cy5tYXAoeD0+YDxvcHRpb24gdmFsdWU9IiR7ZXNjKHguY2xhbl9pZCl9Ij4ke2VzYyh4Lm5hbWUpfSBbJHtlc2MoeC50YWcpfV0gwrcgJHtOdW1iZXIoeC5lbG98fDEwMDApfSBFTE88L29wdGlvbj5gKS5qb2luKCIiKTt9CiAgIGlmKCFjbGFucy5sZW5ndGgpeyQoIiNjbGFuTWVtYmVycyIpLmlubmVySFRNTD0nPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QmtC70LDQvdC+0LIg0L/QvtC60LAg0L3QtdGCLiDQodC+0LfQtNCw0Lkg0L/QtdGA0LLRi9C5INC60LvQsNC9LjwvcD4nO3JldHVybjt9CiAgIGNvbnN0IG1pbmU9Y2xhbnMuZmluZChjPT5jLm93bmVyX2lkPT09YXBpVXNlcklkKTtjb25zdCBjPW1pbmV8fGNsYW5zWzBdO2FjdGl2ZUNsYW5JZD1jLmNsYW5faWQ7CiAgIGlmKG9wcG9uZW50U2VsZWN0KXtjb25zdCBvcHBvbmVudHM9Y2xhbnMuZmlsdGVyKHg9PnguY2xhbl9pZCE9PWFjdGl2ZUNsYW5JZCk7b3Bwb25lbnRTZWxlY3QuaW5uZXJIVE1MPSc8b3B0aW9uIHZhbHVlPSIiPtCS0YvQsdC10YDQuCDQutC70LDQvS3RgdC+0L/QtdGA0L3QuNC64oCmPC9vcHRpb24+JytvcHBvbmVudHMubWFwKHg9PmA8b3B0aW9uIHZhbHVlPSIke2VzYyh4LmNsYW5faWQpfSI+JHtlc2MoeC5uYW1lKX0gWyR7ZXNjKHgudGFnKX1dIMK3ICR7TnVtYmVyKHguZWxvfHwxMDAwKX0gRUxPPC9vcHRpb24+YCkuam9pbigiIik7fQogICBjb25zdCBvd25XYXJOYW1lPSQoIiN3YXJPd25OYW1lIik7aWYob3duV2FyTmFtZSlvd25XYXJOYW1lLnRleHRDb250ZW50PVN0cmluZyhjLnRhZ3x8Yy5uYW1lfHwi0KLQktCe0Jkg0JrQm9CQ0J0iKS50b1VwcGVyQ2FzZSgpOwogICAkKCIjY2xhbk5hbWUiKS5jaGlsZE5vZGVzWzBdLnRleHRDb250ZW50PWMubmFtZSsiICI7JCgiI2NsYW5UYWciKS50ZXh0Q29udGVudD0iIyIrYy50YWc7CiAgIGNvbnN0IGRldGFpbD1hd2FpdCBhcGkoYC9jbGFucy8ke2VuY29kZVVSSUNvbXBvbmVudChjLmNsYW5faWQpfWApLG1lbWJlcnM9ZGV0YWlsLm1lbWJlcnN8fFtdOwogICAkKCIjY2xhbk1lbWJlcnMiKS5pbm5lckhUTUw9bWVtYmVycy5tYXAobT0+YDxkaXYgY2xhc3M9ImxlYWRlciI+PGRpdiBjbGFzcz0icGxheWVyIj48ZGl2IGNsYXNzPSJtaW5pYXZhIj4ke2VzYygobS5uaWNrbmFtZXx8Ij8iKVswXS50b1VwcGVyQ2FzZSgpKX08L2Rpdj48ZGl2PjxiPiR7ZXNjKG0ubmlja25hbWV8fG0udGVsZWdyYW1faWQpfTwvYj48c21hbGw+JHtlc2MobS5yb2xlKX08L3NtYWxsPjwvZGl2PjwvZGl2PjxzcGFuIGNsYXNzPSJlbG8iPiR7TnVtYmVyKG0uZWxvfHwxMDAwKX08L3NwYW4+PC9kaXY+YCkuam9pbigiIil8fCc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCSINC60LvQsNC90LUg0L/QvtC60LAg0L3QtdGCINGD0YfQsNGB0YLQvdC40LrQvtCyLjwvcD4nOwogICBjb25zdCBjb3VudD0kKCIuY2FyZGhlYWQgLm11dGVkLnNtYWxsIiwkKCIjcGFnZS1jbGFucyIpKTtpZihjb3VudCljb3VudC50ZXh0Q29udGVudD1gJHttZW1iZXJzLmxlbmd0aH0gLyAyMGA7CiAgfSkKIF07CiBjb25zdCByZXN1bHRzPWF3YWl0IFByb21pc2UuYWxsU2V0dGxlZChqb2JzKTsKIGNvbnN0IGVycm9ycz1yZXN1bHRzLmZpbHRlcih4PT54LnN0YXR1cz09PSJyZWplY3RlZCIpOwogaWYoZXJyb3JzLmxlbmd0aCljb25zb2xlLndhcm4oItCd0LXQutC+0YLQvtGA0YvQtSDRgNCw0LfQtNC10LvRiyBBUEkg0L3QtSDQt9Cw0LPRgNGD0LfQuNC70LjRgdGMOiIsZXJyb3JzLm1hcCh4PT54LnJlYXNvbj8ubWVzc2FnZSkpOwogcmVuZGVyKCk7Cn0KYXN5bmMgZnVuY3Rpb24gaW5pdEFwaSgpewogY29uc3QgdGc9Z2V0VGVsZWdyYW1Vc2VyKCk7YXBpVXNlcklkPVN0cmluZyh0Zz8uaWR8fGxvY2FsU3RvcmFnZS5nZXRJdGVtKCJzdGFuZHBvaW50VGVsZWdyYW1JZCIpfHwiIikudHJpbSgpOwogaWYodGc/LmlkKXtsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgic3RhbmRwb2ludFRlbGVncmFtSWQiLGFwaVVzZXJJZCk7aWYodGcudXNlcm5hbWUpc3RhdGUubmFtZT10Zy51c2VybmFtZTt9CiAvLyDQn9GA0L7QstC10YDRj9C10Lwg0LTQvtGB0YLRg9C/0L3QvtGB0YLRjCDRgdC10YDQstC10YDQsCDQvtGC0LTQtdC70YzQvdC+INC+0YIg0YDQtdCz0LjRgdGC0YDQsNGG0LjQuCDQv9GA0L7RhNC40LvRjy4KIHRyeXsKICBhd2FpdCBhcGkoIi9jb25maWciKTsKICBzZXRBcGlTdGF0dXMoIkFQSTog0YHQtdGA0LLQtdGAINC+0L3Qu9Cw0LnQvSIsdHJ1ZSk7CiAgY29uc3QgcW49JCgiI3F1ZXVlQXBpTm90ZSIpO2lmKHFuKXFuLnRleHRDb250ZW50PSLQodC10YDQstC10YAg0LTQvtGB0YLRg9C/0LXQvS4g0JTQu9GPINC/0L7QuNGB0LrQsCDQvNCw0YLRh9CwINC90YPQttC10L0g0L/RgNC+0YTQuNC70YwgVGVsZWdyYW0g0Lgg0YPRgdC/0LXRiNC90LDRjyDRgNC10LPQuNGB0YLRgNCw0YbQuNGPLiI7CiB9Y2F0Y2goZSl7c2V0QXBpU3RhdHVzKCJBUEk6INC+0YjQuNCx0LrQsCDRgdC+0LXQtNC40L3QtdC90LjRjyIsZmFsc2UpO2NvbnNvbGUuZXJyb3IoIkFQSSBjb25maWc6IixlKTt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQv9C+0LTQutC70Y7Rh9C40YLRjNGB0Y8g0Log0YHQtdGA0LLQtdGA0YM6ICIrZS5tZXNzYWdlKTtyZXR1cm47fQogaWYoIWFwaVVzZXJJZCl7c2V0QXBpU3RhdHVzKCJBUEk6INC+0YLQutGA0L7QuSDQsiBUZWxlZ3JhbSIsZmFsc2UpO2NvbnN0IHFuPSQoIiNxdWV1ZUFwaU5vdGUiKTtpZihxbilxbi50ZXh0Q29udGVudD0i0J7RgtC60YDQvtC5INC80LjQvdC4LdC/0YDQuNC70L7QttC10L3QuNC1INGH0LXRgNC10LcgVGVsZWdyYW0sINGH0YLQvtCx0Ysg0L/QvtC70YPRh9C40YLRjCBUZWxlZ3JhbSBJRCDQuCDQuNGB0LrQsNGC0Ywg0LzQsNGC0YcuIjtyZXR1cm47fQogdHJ5ewogIHRyeXtjb25zdCBkPWF3YWl0IGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKTthcHBseVBsYXllcihkLnBsYXllcnx8ZCk7fQogIGNhdGNoKGUpewogICBpZihlLnN0YXR1cyE9PTQwNCAmJiAhL0hUVFAgNDA0fNC90LUg0L3QsNC50LTQtdC9L2kudGVzdChlLm1lc3NhZ2UpKXRocm93IGU7CiAgIGNvbnN0IG5pY2tuYW1lPSh0Zz8udXNlcm5hbWV8fHN0YXRlLm5hbWV8fCLQmNCz0YDQvtC6Iikuc2xpY2UoMCwyNCksZ2FtZV9pZD1TdHJpbmcoc3RhdGUuZ2FtZUlkfHwoInNwXyIrYXBpVXNlcklkLnNsaWNlKC04KSkpLnNsaWNlKDAsMzIpOwogICBjb25zdCBkPWF3YWl0IGFwaSgiL3BsYXllcnMvcmVnaXN0ZXIiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxuaWNrbmFtZSxnYW1lX2lkfX0pO2FwcGx5UGxheWVyKGQucGxheWVyfHxkKTsKICB9CiAgYXdhaXQgcmVmcmVzaEFwaURhdGEoKTsKIH1jYXRjaChlKXsKICAvLyDQodC10YDQstC10YAg0YPQttC1INC/0L7QtNGC0LLQtdGA0LTQuNC7INC00L7RgdGC0YPQv9C90L7RgdGC0Ywg0YfQtdGA0LXQtyAvY29uZmlnIOKAlCDQvdC1INC90LDQt9GL0LLQsNC10Lwg0Y3RgtC+INC+0LHRidC10Lkg0L7RiNC40LHQutC+0LkgQVBJLgogIHNldEFwaVN0YXR1cygiQVBJINC+0L3Qu9Cw0LnQvSDCtyDQvtGI0LjQsdC60LAg0L/RgNC+0YTQuNC70Y8iLGZhbHNlKTtjb25zb2xlLmVycm9yKCJQcm9maWxlIGluaXQ6IixlKTsKICBjb25zdCBxbj0kKCIjcXVldWVBcGlOb3RlIik7aWYocW4pcW4udGV4dENvbnRlbnQ9ItCh0LXRgNCy0LXRgCDQtNC+0YHRgtGD0L/QtdC9LCDQvdC+INC90LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNCz0YDRg9C30LjRgtGMINC/0YDQvtGE0LjQu9GMLiDQntGC0LrRgNC+0LkgwqvQn9GA0L7RhNC40LvRjMK7LCDQv9GA0L7QstC10YDRjCDQvdC40LrQvdC10LnQvCDQuCDQuNCz0YDQvtCy0L7QuSBJRCDQuCDRgdC+0YXRgNCw0L3QuCDQtdGJ0ZEg0YDQsNC3LiI7CiAgdG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNCz0YDRg9C30LjRgtGMINC/0YDQvtGE0LjQu9GMOiAiK2UubWVzc2FnZSk7CiB9Cn0KCgovLyA9PT09PSBDbGFuIFdhcnM6IEFQSS1iYWNrZWQgY2hhbGxlbmdlIGFuZCBsb2JieSA9PT09PQpsZXQgYWN0aXZlV2FySWQ9IiI7CmFzeW5jIGZ1bmN0aW9uIGxvYWRXYXJMb2JieShpZCl7aWYoIWlkfHwhYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0J3Rg9C20LXQvSBJRCBDbGFuIFdhciDQuCDQv9C+0LTQutC70Y7Rh9GR0L3QvdGL0Lkg0L/RgNC+0YTQuNC70YwuIik7dHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvY2xhbi13YXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGlkKX0vbG9iYnlgKTthY3RpdmVXYXJJZD1pZDskKCIjd2FySWRJbnB1dCIpLnZhbHVlPWlkO2NvbnN0IHc9ZC53YXIsdGVhbUE9ZC50ZWFtcz8uQXx8W10sdGVhbUI9ZC50ZWFtcz8uQnx8W107JCgiI3dhckxvYmJ5Q29udGVudCIpLmlubmVySFRNTD1gPGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCAke2VzYyh3LmNsYW5fYV9uYW1lKX0gIyR7ZXNjKHcuY2xhbl9hX3RhZyl9PC9oND48dWw+JHt0ZWFtQS5tYXAocD0+YDxsaT4ke2VzYyhwLm5pY2tuYW1lKX0gwrcgJHtOdW1iZXIocC5lbG8pfSBFTE88L2xpPmApLmpvaW4oIiIpfHwiPGxpPtCh0L7RgdGC0LDQsiDQvdC1INC30LDQs9GA0YPQttC10L08L2xpPiJ9PC91bD48L2Rpdj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+UtSAke2VzYyh3LmNsYW5fYl9uYW1lKX0gIyR7ZXNjKHcuY2xhbl9iX3RhZyl9PC9oND48dWw+JHt0ZWFtQi5tYXAocD0+YDxsaT4ke2VzYyhwLm5pY2tuYW1lKX0gwrcgJHtOdW1iZXIocC5lbG8pfSBFTE88L2xpPmApLmpvaW4oIiIpfHwiPGxpPtCh0L7RgdGC0LDQsiDQvdC1INC30LDQs9GA0YPQttC10L08L2xpPiJ9PC91bD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1ub3RpY2UiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPtCh0YLQsNGC0YPRgTogPGI+JHtlc2Mody5zdGF0dXMpfTwvYj4gwrcgSUQgJHtlc2Mody53YXJfaWQpfSR7dy5zdGF0dXM9PT0iYWNjZXB0ZWQiPyIgwrcg0JLRi9C30L7QsiDQv9GA0LjQvdGP0YIsINC80L7QttC90L4g0L/RgNC+0LLQtdGB0YLQuCDRgdC10YDQuNGOINC4INC+0YLQv9GA0LDQstC40YLRjCDRgdGH0ZHRgi4iOiIifTwvZGl2PiR7dy5zdGF0dXM9PT0icGVuZGluZyI/YDxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWNjZXB0V2FyQnRuIj7Qn9GA0LjQvdGP0YLRjCDQstGL0LfQvtCyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9InJlamVjdFdhckJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+YDoiIn0ke3cuc3RhdHVzPT09ImFjY2VwdGVkIj9gPGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQm9CQ0J3QkCBBPC9sYWJlbD48aW5wdXQgaWQ9IndhclNjb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTMiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQm9CQ0J3QkCBCPC9sYWJlbD48aW5wdXQgaWQ9IndhclNjb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOSI+PC9kaXY+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJzdWJtaXRXYXJTY29yZUJ0biIgc3R5bGU9IndpZHRoOjEwMCUiPtCe0YLQv9GA0LDQstC40YLRjCDRgNC10LfRg9C70YzRgtCw0YIg0LzQvtC00LXRgNCw0YLQvtGA0YM8L2J1dHRvbj5gOiIifWA7Y29uc3QgYWNjZXB0PSQoIiNhY2NlcHRXYXJCdG4iKTtpZihhY2NlcHQpYWNjZXB0Lm9uY2xpY2s9KCk9PmRlY2lkZVdhcigiYWNjZXB0Iik7Y29uc3QgcmVqZWN0PSQoIiNyZWplY3RXYXJCdG4iKTtpZihyZWplY3QpcmVqZWN0Lm9uY2xpY2s9KCk9PmRlY2lkZVdhcigicmVqZWN0Iik7Y29uc3Qgc3VibWl0PSQoIiNzdWJtaXRXYXJTY29yZUJ0biIpO2lmKHN1Ym1pdClzdWJtaXQub25jbGljaz1hc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL2NsYW4td2Fycy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVXYXJJZCl9L3Jlc3VsdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHNjb3JlX2E6TnVtYmVyKCQoIiN3YXJTY29yZUEiKS52YWx1ZSksc2NvcmVfYjpOdW1iZXIoJCgiI3dhclNjb3JlQiIpLnZhbHVlKX19KTt0b2FzdCgi0KDQtdC30YPQu9GM0YLQsNGCIENsYW4gV2FyINC+0YLQv9GA0LDQstC70LXQvSDQvdCwINC/0YDQvtCy0LXRgNC60YMuIik7YXdhaXQgbG9hZFdhckxvYmJ5KGFjdGl2ZVdhcklkKTt9Y2F0Y2goZSl7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgiBDbGFuIFdhcjogIitlLm1lc3NhZ2UpO319OyQoIiN3YXJTdGF0dXNUZXh0IikudGV4dENvbnRlbnQ9YENsYW4gV2FyICR7dy53YXJfaWR9OiAke3cuc3RhdHVzfWA7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC+0YLQutGA0YvRgtGMINC70L7QsdCx0LggQ2xhbiBXYXI6ICIrZS5tZXNzYWdlKTt9fQphc3luYyBmdW5jdGlvbiBkZWNpZGVXYXIoZGVjaXNpb24pe3RyeXthd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlV2FySWQpfS9kZWNpc2lvbmAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGRlY2lzaW9ufX0pO3RvYXN0KGRlY2lzaW9uPT09ImFjY2VwdCI/ItCS0YvQt9C+0LIg0L/RgNC40L3Rj9GCLiI6ItCS0YvQt9C+0LIg0L7RgtC60LvQvtC90ZHQvS4iKTthd2FpdCBsb2FkV2FyTG9iYnkoYWN0aXZlV2FySWQpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtCx0YDQsNCx0L7RgtCw0YLRjCDQstGL0LfQvtCyOiAiK2UubWVzc2FnZSk7fX0KJCgiI2NoYWxsZW5nZVdhckJ0biIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2NvbnN0IG9wcG9uZW50X2NsYW5faWQ9JCgiI3dhck9wcG9uZW50SWQiKS52YWx1ZS50cmltKCk7aWYoIWFwaVVzZXJJZHx8IW9wcG9uZW50X2NsYW5faWQpcmV0dXJuIHRvYXN0KCLQo9C60LDQttC4IElEINC60LvQsNC90LAg0YHQvtC/0LXRgNC90LjQutCwINC4INC/0L7QtNC60LvRjtGH0Lgg0L/RgNC+0YTQuNC70YwuIik7dHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKCIvY2xhbi13YXJzIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsb3Bwb25lbnRfY2xhbl9pZH19KTthY3RpdmVXYXJJZD1kLndhcl9pZDskKCIjd2FySWRJbnB1dCIpLnZhbHVlPWQud2FyX2lkO3RvYXN0KCLQktGL0LfQvtCyIENsYW4gV2FyINC+0YLQv9GA0LDQstC70LXQvS4iKTthd2FpdCBsb2FkV2FyTG9iYnkoZC53YXJfaWQpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDRgdC+0LfQtNCw0YLRjCBDbGFuIFdhcjogIitlLm1lc3NhZ2UpO319KTsKJCgiI2xvYWRXYXJMb2JieUJ0biIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PmxvYWRXYXJMb2JieSgkKCIjd2FySWRJbnB1dCIpLnZhbHVlLnRyaW0oKSkpOwoKLy8gPT09PT0gTWF0Y2ggQ2VudGVyOiBhY2NlcHQgLT4gY2FwdGFpbiBkcmFmdCAtPiBiYW4vcGljayAtPiBsb2JieSAtPiByZXN1bHQgLT4gYXBwZWFsID09PT09CmxldCBtYXRjaFBvbGxUaW1lcj1udWxsLGN1cnJlbnRNYXRjaD1udWxsOwpjb25zdCBNQVRDSF9NQVBTPVsiQnJlZXplIiwiQ2FseXBzbyIsIkR1bmUiLCJQcm92aW5jZSIsIlJ1c3QiLCJTYWt1cmEiLCJTYW5kc3RvbmUiLCJab25lIDciXTsKZnVuY3Rpb24gc3RvcFF1ZXVlUG9sbGluZygpe2lmKHF1ZXVlUG9sbFRpbWVyKXtjbGVhckludGVydmFsKHF1ZXVlUG9sbFRpbWVyKTtxdWV1ZVBvbGxUaW1lcj1udWxsO319CmZ1bmN0aW9uIHN0b3BNYXRjaFBvbGxpbmcoKXtpZihtYXRjaFBvbGxUaW1lcil7Y2xlYXJJbnRlcnZhbChtYXRjaFBvbGxUaW1lcik7bWF0Y2hQb2xsVGltZXI9bnVsbDt9fQpmdW5jdGlvbiBtY1Nob3coaWQpe1sibWNBY2NlcHRQYW5lbCIsIm1jQmFuUGFuZWwiLCJtY0xvYmJ5UGFuZWwiLCJtY1BsYXlpbmdQYW5lbCIsIm1jUmV2aWV3UGFuZWwiXS5mb3JFYWNoKHg9PiQoIiMiK3gpLmNsYXNzTGlzdC50b2dnbGUoIm1jLWhpZGRlbiIseCE9PWlkKSk7JCgiI21hdGNoQ2VudGVyIikuY2xhc3NMaXN0LmFkZCgidmlzaWJsZSIpO30KZnVuY3Rpb24gbWF0Y2hQbGF5ZXJOYW1lKGlkKXtyZXR1cm4gKGN1cnJlbnRNYXRjaD8ucGxheWVyX3Byb2ZpbGVzPy5baWRdPy5uaWNrbmFtZSl8fCgi0JjQs9GA0L7QuiAiK1N0cmluZyhpZHx8IiIpLnNsaWNlKC00KSk7fQpmdW5jdGlvbiByZW5kZXJNY1BsYXllcnMoKXtjb25zdCBwbGF5ZXJzPWN1cnJlbnRNYXRjaD8ucGxheWVyc3x8W10sYWNjZXB0YW5jZT1jdXJyZW50TWF0Y2g/LmFjY2VwdGFuY2V8fHt9OyQoIiNtY0FjY2VwdFBsYXllcnMiKS5pbm5lckhUTUw9cGxheWVycy5tYXAoKGlkLGkpPT57Y29uc3QgYWNjZXB0ZWQ9ISFhY2NlcHRhbmNlW2lkXTtyZXR1cm4gYDxkaXYgY2xhc3M9Im1jLXBsYXllciAke2FjY2VwdGVkPyJhY2NlcHRlZCI6IiJ9Ij48ZGl2IGNsYXNzPSJtYy1hdmF0YXIiPiR7YWNjZXB0ZWQ/IuKckyI6aSsxfTwvZGl2PjxiPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfTwvYj48c21hbGw+JHthY2NlcHRlZD8i0J/QoNCY0J3Qr9CbIjoi0J7QltCY0JTQkNCd0JjQlSJ9PC9zbWFsbD48L2Rpdj5gfSkuam9pbigiIik7Y29uc3Qgbj1OdW1iZXIoY3VycmVudE1hdGNoPy5hY2NlcHRlZF9jb3VudHx8MCksdG90YWw9TnVtYmVyKGN1cnJlbnRNYXRjaD8ucGxheWVyc19uZWVkZWR8fHBsYXllcnMubGVuZ3RofHwwKTskKCIjbWNBY2NlcHRUZXh0IikudGV4dENvbnRlbnQ9YNCf0YDQuNC90Y/Qu9C4ICR7bn0g0LjQtyAke3RvdGFsfS4g0JTQu9GPINGB0YLQsNGA0YLQsCDQstGB0LUg0YPRh9Cw0YHRgtC90LjQutC4INC00L7Qu9C20L3RiyDQv9C+0LTRgtCy0LXRgNC00LjRgtGMINC80LDRgtGHLmA7JCgiI21jQWNjZXB0UHJvZ3Jlc3MiKS5zdHlsZS53aWR0aD0odG90YWw/TWF0aC5taW4oMTAwLG4vdG90YWwqMTAwKTowKSsiJSI7JCgiI21jQWNjZXB0QnRuIikuZGlzYWJsZWQ9ISFhY2NlcHRhbmNlW2FwaVVzZXJJZF07JCgiI21jQWNjZXB0QnRuIikudGV4dENvbnRlbnQ9YWNjZXB0YW5jZVthcGlVc2VySWRdPyLinJMg0KLRiyDQv9GA0LjQvdGP0Lsg0LzQsNGC0YciOiLinJMg0J/RgNC40L3Rj9GC0Ywg0LzQsNGC0YciO30KZnVuY3Rpb24gZmlsbENhcHRhaW5TZWxlY3RzKCl7Y29uc3QgcGxheWVycz1jdXJyZW50TWF0Y2g/LnBsYXllcnN8fFtdLHNpemU9KHsiMXYxIjoxLCIydjIiOjIsIjN2MyI6MywiNXY1Ijo1fSlbY3VycmVudE1hdGNoPy5tb2RlXXx8NSx0ZWFtcz17QTpwbGF5ZXJzLnNsaWNlKDAsc2l6ZSksQjpwbGF5ZXJzLnNsaWNlKHNpemUpfTtbIkEiLCJCIl0uZm9yRWFjaCh0PT57Y29uc3Qgc2VsPSQoIiNtY0NhcHRhaW4iK3QpLGlkcz10ZWFtc1t0XTtzZWwuaW5uZXJIVE1MPWlkcy5tYXAoaWQ9PmA8b3B0aW9uIHZhbHVlPSIke2VzYyhpZCl9Ij4ke2VzYyhtYXRjaFBsYXllck5hbWUoaWQpKX08L29wdGlvbj5gKS5qb2luKCIiKTtjb25zdCBjYXB0YWluPWN1cnJlbnRNYXRjaD8uW3Q9PT0iQSI/ImNhcHRhaW5faWQiOiJjYXB0YWluX2JfaWQiXTtpZihjYXB0YWluKXNlbC52YWx1ZT1jYXB0YWluOyQoIiNtY0NhcHRhaW4iK3QrIkluZm8iKS50ZXh0Q29udGVudD1jYXB0YWluPyLQmtCw0L/QuNGC0LDQvSDQvdCw0LfQvdCw0YfQtdC9OiAiK21hdGNoUGxheWVyTmFtZShjYXB0YWluKToi0JrQsNC/0LjRgtCw0L0g0LXRidGRINC90LUg0LLRi9Cx0YDQsNC9Ijt9KTt9CmZ1bmN0aW9uIHJlbmRlck1hcFBpY2soKXtjb25zdCBhY3Rpb25zPWN1cnJlbnRNYXRjaD8ubWFwX2FjdGlvbnN8fFtdLGJhbnM9YWN0aW9ucy5maWx0ZXIoYT0+YS5hY3Rpb249PT0iYmFuIikubGVuZ3RoLGZpbmlzaGVkPSEhY3VycmVudE1hdGNoPy5tYXBfbmFtZTskKCIjbWNCYW5UaXRsZSIpLnRleHRDb250ZW50PWZpbmlzaGVkPyLQmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwOiAiK2N1cnJlbnRNYXRjaC5tYXBfbmFtZTpgQmFuIC8gUGljayDCtyAke01hdGgubWluKGJhbnMsNCl9LzQg0LHQsNC90L7QsmA7Y29uc3QgZXhwZWN0ZWQ9YmFucyUyPT09MD9jdXJyZW50TWF0Y2g/LmNhcHRhaW5faWQ6KGN1cnJlbnRNYXRjaD8uY2FwdGFpbl9iX2lkfHxjdXJyZW50TWF0Y2g/LnBsYXllcnM/LlsoeyIxdjEiOjEsIjJ2MiI6MiwiM3YzIjozLCI1djUiOjV9KVtjdXJyZW50TWF0Y2g/Lm1vZGVdfHw1XSk7Y29uc3QgY2FuQWN0PSFmaW5pc2hlZCYmYXBpVXNlcklkPT09ZXhwZWN0ZWQ7JCgiI21jQmFuSGludCIpLnRleHRDb250ZW50PWZpbmlzaGVkPyJCYW4tcGljayDQt9Cw0LLQtdGA0YjRkdC9LiDQn9C10YDQtdGF0L7QtNC4INCyINC70L7QsdCx0Lgg0LzQsNGC0YfQsC4iOmJhbnM8ND9g0KHQu9C10LTRg9GO0YnQuNC5INCx0LDQvTogJHttYXRjaFBsYXllck5hbWUoZXhwZWN0ZWQpfSR7Y2FuQWN0PyIg4oCUINGC0LLQvtC5INGF0L7QtCI6IiDigJQg0L7QttC40LTQsNC10Lwg0LrQsNC/0LjRgtCw0L3QsCJ9LmA6YNCR0LDQvdGLINC30LDQstC10YDRiNC10L3Riy4g0JrQsNC/0LjRgtCw0L0gQSDQstGL0LHQuNGA0LDQtdGCINC60LDRgNGC0YMke2FwaVVzZXJJZD09PWN1cnJlbnRNYXRjaD8uY2FwdGFpbl9pZD8iIOKAlCDQstGL0LHQtdGA0Lgg0LrQsNGA0YLRgyDQvdC40LbQtSI6Ii4ifWA7Y29uc3QgdXNlZD1uZXcgU2V0KGFjdGlvbnMubWFwKGE9PmEubWFwX25hbWUpKTskKCIjbWNNYXBCdXR0b25zIikuaW5uZXJIVE1MPU1BVENIX01BUFMubWFwKG5hbWU9PntsZXQgYWN0aW9uPSJiYW4iO2lmKGJhbnM+PTQpYWN0aW9uPSJwaWNrIjtyZXR1cm4gYDxidXR0b24gY2xhc3M9Im1jLW1hcCAke3VzZWQuaGFzKG5hbWUpPyJ1c2VkIjoiIn0iIGRhdGEtbWMtbWFwPSIke2VzYyhuYW1lKX0iIGRhdGEtbWMtYWN0aW9uPSIke2FjdGlvbn0iICR7KCFjYW5BY3R8fGZpbmlzaGVkfHx1c2VkLmhhcyhuYW1lKSk/ImRpc2FibGVkIjoiIn0+JHtlc2MobmFtZSl9PHNtYWxsIHN0eWxlPSJkaXNwbGF5OmJsb2NrO2NvbG9yOiM5ZGE5YjU7bWFyZ2luLXRvcDo0cHgiPiR7dXNlZC5oYXMobmFtZSk/KGFjdGlvbnMuZmluZChhPT5hLm1hcF9uYW1lPT09bmFtZSk/LmFjdGlvbj09PSJiYW4iPyLQl9CQ0JHQkNCd0JXQndCQIjoi0JLQq9CR0KDQkNCd0JAiKTphY3Rpb249PT0iYmFuIj8iQkFOIjoiUElDSyJ9PC9zbWFsbD48L2J1dHRvbj5gfSkuam9pbigiIik7JCgiI21jTWFwTG9nIikuaW5uZXJIVE1MPWFjdGlvbnMubWFwKChhLGkpPT5gPGRpdiBjbGFzcz0ibWMtbG9ncm93Ij48c3Bhbj4ke2krMX0uICR7ZXNjKGEuYWN0aW9uPT09ImJhbiI/IkJBTiI6IlBJQ0siKX0gwrcgJHtlc2MoYS5tYXBfbmFtZSl9PC9zcGFuPjxzcGFuPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShhLmJ5KSl9PC9zcGFuPjwvZGl2PmApLmpvaW4oIiIpfHwnPHAgY2xhc3M9Im1jLW11dGVkIj7Qn9C+0LrQsCDQvdC10YIg0LTQtdC50YHRgtCy0LjQuSDQvdCwINC60LDRgNGC0LUuPC9wPic7JCQoIltkYXRhLW1jLW1hcF0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vbWFwLWFjdGlvbmAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGFjdGlvbjpiLmRhdGFzZXQubWNBY3Rpb24sbWFwX25hbWU6Yi5kYXRhc2V0Lm1jTWFwfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO31jYXRjaChlKXt0b2FzdCgiQmFuL1BpY2s6ICIrZS5tZXNzYWdlKTt9fSkpO30KZnVuY3Rpb24gcmVuZGVyTWF0Y2goKXtpZighY3VycmVudE1hdGNoKXJldHVybjtjb25zdCBtPWN1cnJlbnRNYXRjaDskKCIjbWF0Y2hDZW50ZXIiKS5jbGFzc0xpc3QuYWRkKCJ2aXNpYmxlIik7JCgiI21jU3VidGl0bGUiKS50ZXh0Q29udGVudD1g0JzQsNGC0YcgJHttLm1hdGNoX2lkfSDCtyAke20ubW9kZX0gwrcgJHttLnBsYXllcnM/Lmxlbmd0aHx8MH0g0LjQs9GA0L7QutC+0LJgOyQoIiNtY1N0YXR1cyIpLnRleHRDb250ZW50PVN0cmluZyhtLnN0YXR1c3x8Ik1BVENIIikucmVwbGFjZUFsbCgiXyIsIiAiKS50b1VwcGVyQ2FzZSgpOyQoIiNtY1RpdGxlIikudGV4dENvbnRlbnQ9KHthY2NlcHRpbmc6ItCc0LDRgtGHINC90LDQudC00LXQvSDigJQg0L/QvtC00YLQstC10YDQtNC4IixtYXBfcGljazoiQmFuIC8gUGljayDQutCw0YDRgiIscmVhZHk6ItCb0L7QsdCx0Lgg0LPQvtGC0L7QstC+IixwbGF5aW5nOiLQnNCw0YLRhyDQsiDQuNCz0YDQtSIsdW5kZXJfcmV2aWV3OiLQoNC10LfRg9C70YzRgtCw0YIg0L3QsCDQv9GA0L7QstC10YDQutC1Iixjb25maXJtZWQ6ItCc0LDRgtGHINC/0L7QtNGC0LLQtdGA0LbQtNGR0L0iLHJlamVjdGVkOiLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC60LvQvtC90ZHQvSIsZGlzcHV0ZWQ6ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGB0L/QsNGA0LjQstCw0LXRgtGB0Y8iLGRlY2xpbmVkOiLQnNCw0YLRhyDQvtGC0LrQu9C+0L3RkdC9IixleHBpcmVkOiLQktGA0LXQvNGPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPINC40YHRgtC10LrQu9C+In0pW20uc3RhdHVzXXx8ItCc0LDRgtGHIjsKIGlmKG0uc3RhdHVzPT09ImFjY2VwdGluZyIpe21jU2hvdygibWNBY2NlcHRQYW5lbCIpO3JlbmRlck1jUGxheWVycygpO3JldHVybjt9CiBpZihtLnN0YXR1cz09PSJtYXBfcGljayIpe21jU2hvdygibWNCYW5QYW5lbCIpO2ZpbGxDYXB0YWluU2VsZWN0cygpO3JlbmRlck1hcFBpY2soKTtyZXR1cm47fQogaWYobS5zdGF0dXM9PT0icmVhZHkiKXttY1Nob3coIm1jTG9iYnlQYW5lbCIpO2NvbnN0IHNpemU9KHsiMXYxIjoxLCIydjIiOjIsIjN2MyI6MywiNXY1Ijo1fSlbbS5tb2RlXXx8NSx0YT0obS5wbGF5ZXJzfHxbXSkuc2xpY2UoMCxzaXplKSx0Yj0obS5wbGF5ZXJzfHxbXSkuc2xpY2Uoc2l6ZSk7JCgiI21jVGVhbUEiKS5pbm5lckhUTUw9dGEubWFwKGlkPT5gPGxpPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfSR7aWQ9PT1tLmNhcHRhaW5faWQ/IiDCtyDQmtCQ0J/QmNCi0JDQnSI6IiJ9PC9saT5gKS5qb2luKCIiKTskKCIjbWNUZWFtQiIpLmlubmVySFRNTD10Yi5tYXAoaWQ9PmA8bGk+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGlkKSl9JHtpZD09PW0uY2FwdGFpbl9iX2lkPyIgwrcg0JrQkNCf0JjQotCQ0J0iOiIifTwvbGk+YCkuam9pbigiIik7JCgiI21jTG9iYnlJbmZvIikudGV4dENvbnRlbnQ9YNCa0LDRgNGC0LA6ICR7bS5tYXBfbmFtZXx8ItC90LUg0LLRi9Cx0YDQsNC90LAifS4g0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQSDQt9Cw0L/Rg9GB0LrQsNC10YIg0LzQsNGC0YcuYDskKCIjbWNTdGFydEJ0biIpLmRpc2FibGVkPWFwaVVzZXJJZCE9PW0uY2FwdGFpbl9pZDskKCIjbWNTdGFydEJ0biIpLnRleHRDb250ZW50PWFwaVVzZXJJZD09PW0uY2FwdGFpbl9pZD8i4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhyI6ItCe0LbQuNC00LDQtdC8INC60LDQv9C40YLQsNC90LAg0LrQvtC80LDQvdC00YsgQSI7cmV0dXJuO30KIGlmKG0uc3RhdHVzPT09InBsYXlpbmciKXttY1Nob3coIm1jUGxheWluZ1BhbmVsIik7cmV0dXJuO30KIGlmKFsidW5kZXJfcmV2aWV3IiwiY29uZmlybWVkIiwicmVqZWN0ZWQiLCJkaXNwdXRlZCJdLmluY2x1ZGVzKG0uc3RhdHVzKSl7bWNTaG93KCJtY1Jldmlld1BhbmVsIik7JCgiI21jUmV2aWV3SW5mbyIpLnRleHRDb250ZW50PW0uc3RhdHVzPT09ImNvbmZpcm1lZCI/YNCg0LXQt9GD0LvRjNGC0LDRgiDQv9C+0LTRgtCy0LXRgNC20LTRkdC9OiAke20uc2NvcmVfYT8/IuKAlCJ9OiR7bS5zY29yZV9iPz8i4oCUIn0uINCg0LXQudGC0LjQvdCzINC80L7QttC10YIg0LHRi9GC0Ywg0L7QsdC90L7QstC70ZHQvSDQvNC+0LTQtdGA0LDRhtC40LXQuS5gOm0uc3RhdHVzPT09InJlamVjdGVkIj8i0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQutC70L7QvdGR0L0g0LzQvtC00LXRgNCw0YLQvtGA0L7QvC4g0J/RgNC+0LLQtdGA0Ywg0YPQstC10LTQvtC80LvQtdC90LjRjy4iOm0uc3RhdHVzPT09ImRpc3B1dGVkIj8i0KDQtdC30YPQu9GM0YLQsNGCINC+0YHQv9Cw0YDQuNCy0LDQtdGC0YHRjy4g0JzQvtC00LXRgNCw0YLQvtGA0YMg0YLRgNC10LHRg9C10YLRgdGPINC/0YDQvtCy0LXRgNC60LAuIjoi0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQv9GA0LDQstC70LXQvSDQvdCwINC/0YDQvtCy0LXRgNC60YMg0LzQvtC00LXRgNCw0YLQvtGA0YMuIEVMTyDQvdC1INC80LXQvdGP0LXRgtGB0Y8g0LTQviDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy4iOyQoIiNtY0FwcGVhbEZvcm0iKS5jbGFzc0xpc3QudG9nZ2xlKCJtYy1oaWRkZW4iLCFbImNvbmZpcm1lZCIsInJlamVjdGVkIiwiZGlzcHV0ZWQiXS5pbmNsdWRlcyhtLnN0YXR1cykpO3JldHVybjt9CiBtY1Nob3coIm1jUmV2aWV3UGFuZWwiKTskKCIjbWNSZXZpZXdJbmZvIikudGV4dENvbnRlbnQ9ItCc0LDRgtGHINC30LDQstC10YDRiNC40Lsg0L/RgNC+0YbQtdGB0YEg0L/RgNC40L3Rj9GC0LjRjzogIittLnN0YXR1czsKfQphc3luYyBmdW5jdGlvbiBwb2xsQWN0aXZlTWF0Y2goKXtpZighYWN0aXZlTWF0Y2hJZClyZXR1cm47dHJ5e2NvbnN0IG09YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX1gKTtjdXJyZW50TWF0Y2g9bTtyZW5kZXJNYXRjaCgpO2lmKFsiY29uZmlybWVkIiwicmVqZWN0ZWQiLCJkZWNsaW5lZCIsImV4cGlyZWQiXS5pbmNsdWRlcyhtLnN0YXR1cykpc3RvcE1hdGNoUG9sbGluZygpO31jYXRjaChlKXtjb25zb2xlLndhcm4oIk1hdGNoIHBvbGwiLGUubWVzc2FnZSk7fX0KYXN5bmMgZnVuY3Rpb24gc2hvd01hdGNoKGlkKXthY3RpdmVNYXRjaElkPWlkO3F1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD0i0J3QsNC50YLQuCDQvNCw0YLRhyI7bmF2KCJtYXRjaGVzIik7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7c3RvcE1hdGNoUG9sbGluZygpO21hdGNoUG9sbFRpbWVyPXNldEludGVydmFsKCgpPT57aWYoIWRvY3VtZW50LmhpZGRlbilwb2xsQWN0aXZlTWF0Y2goKX0sMzAwMCk7fQphc3luYyBmdW5jdGlvbiBzdGFydFF1ZXVlUG9sbGluZygpe3N0b3BRdWV1ZVBvbGxpbmcoKTtjb25zdCB1cGRhdGU9YXN5bmMoKT0+e2lmKCFxdWV1ZUFjdGl2ZXx8ZG9jdW1lbnQuaGlkZGVuKXJldHVybjt0cnl7Y29uc3QgcT1hd2FpdCBhcGkoYC9xdWV1ZS8ke2VuY29kZVVSSUNvbXBvbmVudChzZWxlY3RlZE1vZGUpfWApO2NvbnN0IGZvdW5kPU51bWJlcihxLnBsYXllcnNfZm91bmR8fDApLG5lZWRlZD1OdW1iZXIocS5wbGF5ZXJzX25lZWRlZHx8MCk7JCgiI3F1ZXVlVGl0bGUiKS50ZXh0Q29udGVudD0i0JjRidC10Lwg0YHQvtC/0LXRgNC90LjQutC+0LLigKYiOyQoIiNxdWV1ZURlc2NyaXB0aW9uIikudGV4dENvbnRlbnQ9YNCSINC+0YfQtdGA0LXQtNC4ICR7Zm91bmR9LyR7bmVlZGVkfTsg0L7RgdGC0LDQu9C+0YHRjCAke01hdGgubWF4KDAsbmVlZGVkLWZvdW5kKX1gO2NvbnN0IGFjdGl2ZT1hd2FpdCBhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L2FjdGl2ZS1tYXRjaGApO2lmKGFjdGl2ZS5hY3RpdmUmJmFjdGl2ZS5tYXRjaCl7YWN0aXZlTWF0Y2hJZD1hY3RpdmUubWF0Y2gubWF0Y2hfaWQ7YXdhaXQgc2hvd01hdGNoKGFjdGl2ZU1hdGNoSWQpO3RvYXN0KCLQnNCw0YLRhyDQvdCw0LnQtNC10L0hINCf0L7QtNGC0LLQtdGA0LTQuCDRg9GH0LDRgdGC0LjQtS4iKTt9fWNhdGNoKGUpe2NvbnNvbGUud2FybigiUXVldWUgc3RhdHVzIixlLm1lc3NhZ2UpO319O2F3YWl0IHVwZGF0ZSgpO3F1ZXVlUG9sbFRpbWVyPXNldEludGVydmFsKHVwZGF0ZSw0MDAwKTt9CiQoIiNtY0FjY2VwdEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L2FjY2VwdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGFjY2VwdGVkOnRydWV9fSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7dG9hc3QoItCi0Ysg0L/QvtC00YLQstC10YDQtNC40Lsg0LzQsNGC0YcuIik7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC/0YDQuNC90Y/RgtGMINC80LDRgtGHOiAiK2UubWVzc2FnZSk7fX0pOwokKCIjbWNEZWNsaW5lQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9PntpZighY29uZmlybSgi0J7RgtC60LvQvtC90LjRgtGMINC90LDQudC00LXQvdC90YvQuSDQvNCw0YLRhz8g0J7RgdGC0LDQu9GM0L3Ri9C1INC40LPRgNC+0LrQuCDQstC10YDQvdGD0YLRgdGPINCyINC/0L7QuNGB0LouIikpcmV0dXJuO3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9hY2NlcHRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxhY2NlcHRlZDpmYWxzZX19KTtzdG9wTWF0Y2hQb2xsaW5nKCk7JCgiI21hdGNoQ2VudGVyIikuY2xhc3NMaXN0LnJlbW92ZSgidmlzaWJsZSIpO3RvYXN0KCLQnNCw0YLRhyDQvtGC0LrQu9C+0L3RkdC9LiIpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtGC0LrQu9C+0L3QuNGC0Yw6ICIrZS5tZXNzYWdlKTt9fSk7CmFzeW5jIGZ1bmN0aW9uIHNldENhcHRhaW4odGVhbSl7Y29uc3QgaWQ9JCgiI21jQ2FwdGFpbiIrdGVhbSkudmFsdWU7aWYoIWlkKXJldHVybjt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vY2FwdGFpbmAse21ldGhvZDoiUE9TVCIsYm9keTp7Y2FwdGFpbl9pZDppZCx0ZWFtfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQmtCw0L/QuNGC0LDQvSDQutC+0LzQsNC90LTRiyAiK3RlYW0rIiDQvdCw0LfQvdCw0YfQtdC9LiIpO31jYXRjaChlKXt0b2FzdCgi0JrQsNC/0LjRgtCw0L06ICIrZS5tZXNzYWdlKTt9fQokKCIjbWNTZXRDYXB0YWluQSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+c2V0Q2FwdGFpbigiQSIpKTskKCIjbWNTZXRDYXB0YWluQiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+c2V0Q2FwdGFpbigiQiIpKTsKJCgiI21jU3RhcnRCdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9zdGFydD90ZWxlZ3JhbV9pZD0ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWAse21ldGhvZDoiUE9TVCJ9KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt0b2FzdCgi0JzQsNGC0Ycg0L7RgtC80LXRh9C10L0g0LrQsNC6INC90LDRh9Cw0YLRi9C5LiIpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0L/Rg9GB0YLQuNGC0Ywg0LzQsNGC0Yc6ICIrZS5tZXNzYWdlKTt9fSk7CiQoIiNtY1N1Ym1pdFJlc3VsdCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57Y29uc3Qgc2NvcmVfYT1OdW1iZXIoJCgiI21jU2NvcmVBIikudmFsdWUpLHNjb3JlX2I9TnVtYmVyKCQoIiNtY1Njb3JlQiIpLnZhbHVlKTtpZighTnVtYmVyLmlzSW50ZWdlcihzY29yZV9hKXx8IU51bWJlci5pc0ludGVnZXIoc2NvcmVfYil8fHNjb3JlX2E8MHx8c2NvcmVfYjwwKXJldHVybiB0b2FzdCgi0KPQutCw0LbQuCDQutC+0YDRgNC10LrRgtC90YvQuSDRgdGH0ZHRgiDQvtCx0LXQuNGFINC60L7QvNCw0L3QtC4iKTt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vcmVzdWx0YCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsc2NvcmVfYSxzY29yZV9iLHN0YXRzOntbYXBpVXNlcklkXTp7a2lsbHM6TnVtYmVyKCQoIiNtY0tpbGxzIikudmFsdWV8fDApLGRlYXRoczpOdW1iZXIoJCgiI21jRGVhdGhzIikudmFsdWV8fDApfX0sbm90ZTokKCIjbWNSZXN1bHROb3RlIikudmFsdWUudHJpbSgpfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDLiBFTE8g0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS4iKTt9Y2F0Y2goZSl7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgjogIitlLm1lc3NhZ2UpO319KTsKJCgiI21jU3VibWl0QXBwZWFsIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vYXBwZWFsc2Ase21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHJlYXNvbjokKCIjbWNBcHBlYWxSZWFzb24iKS52YWx1ZSxkZXRhaWxzOiQoIiNtY0FwcGVhbERldGFpbHMiKS52YWx1ZS50cmltKCl9fSk7dG9hc3QoItCQ0L/QtdC70LvRj9GG0LjRjyDQvtGC0L/RgNCw0LLQu9C10L3QsCDQvNC+0LTQtdGA0LDRgtC+0YDRgy4iKTskKCIjbWNTdWJtaXRBcHBlYWwiKS5kaXNhYmxlZD10cnVlO31jYXRjaChlKXt0b2FzdCgi0JDQv9C10LvQu9GP0YbQuNGPOiAiK2UubWVzc2FnZSk7fX0pOwovLyBLZWVwIHJlYWwgcHJvZmlsZSBzdGF0ZSBhbmQgZGlzY292ZXIgYSBtYXRjaCBpZiBpdCB3YXMgZm9ybWVkIGJ5IGFub3RoZXIgcXVldWUgcGFydGljaXBhbnQuCmNvbnN0IF9vbGRBcHBseVBsYXllcj1hcHBseVBsYXllcjthcHBseVBsYXllcj1mdW5jdGlvbihwKXtfb2xkQXBwbHlQbGF5ZXIocCk7Y29uc3QgcHM9JCgiI3Byb2ZpbGVTdGF0dXMiKTtpZihwcylwcy50ZXh0Q29udGVudD0i4pePINCf0KDQntCk0JjQm9CsINChINCh0JXQoNCS0JXQoNCQIjtzZXRBcGlTdGF0dXMoIkFQSSDQvtC90LvQsNC50L0gwrcg0L/RgNC+0YTQuNC70Ywg0LfQsNCz0YDRg9C20LXQvSIsdHJ1ZSk7fTsKY29uc3QgX29sZEluaXRBcGk9aW5pdEFwaTtpbml0QXBpPWFzeW5jIGZ1bmN0aW9uKCl7YXdhaXQgX29sZEluaXRBcGkoKTtpZihhcGlVc2VySWQpe3RyeXtjb25zdCBhPWF3YWl0IGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX0vYWN0aXZlLW1hdGNoYCk7aWYoYS5hY3RpdmUmJmEubWF0Y2gpe2FjdGl2ZU1hdGNoSWQ9YS5tYXRjaC5tYXRjaF9pZDthd2FpdCBzaG93TWF0Y2goYWN0aXZlTWF0Y2hJZCk7fX1jYXRjaChlKXtjb25zb2xlLndhcm4oIkFjdGl2ZSBtYXRjaCIsZS5tZXNzYWdlKTt9fX07CgpyZW5kZXIoKTsKaW5pdEFwaSgpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+"""

@app.get("/", response_class=HTMLResponse)
def root():
    import base64
    return HTMLResponse(
        content=base64.b64decode(_FRONTEND_HTML_B64).decode("utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/admin-panel", response_class=HTMLResponse)
def admin_panel():
    # Browser-based moderator panel; privileged API calls still require the server token.
    return HTMLResponse(r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>STANDPOINT â ÐÐ°Ð½ÐµÐ»Ñ Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¾ÑÐ°</title><style>
:root{color-scheme:dark;--bg:#080a0d;--panel:#11161c;--panel2:#171e26;--line:#303b46;--text:#f4f6f8;--muted:#9da9b5;--orange:#ff681b;--green:#39c879;--red:#ff5964}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 100% 0,rgba(255,104,27,.13),transparent 32%),var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,sans-serif}.wrap{max-width:1200px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}.brand{font-size:20px;font-weight:900;letter-spacing:.08em}.brand span{color:var(--orange)}.sub{color:var(--muted);font-size:13px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px}.login{display:flex;gap:10px;flex-wrap:wrap}.input,textarea{background:#090d12;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:12px;font:inherit;min-width:0}.input{flex:1}button{border:0;border-radius:10px;padding:11px 15px;font:inherit;font-weight:750;cursor:pointer;background:var(--orange);color:#fff}button.secondary{background:#252e38;border:1px solid var(--line)}button.danger{background:#7e2029}button:disabled{opacity:.5;cursor:wait}.layout{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,1.4fr);gap:16px}.match{width:100%;display:block;text-align:left;background:var(--panel2);border:1px solid var(--line);margin:8px 0;padding:13px;border-radius:12px;color:var(--text)}.match.active{border-color:var(--orange)}.pill{display:inline-block;border-radius:6px;padding:3px 7px;background:#2a333e;color:#cbd5df;font-size:12px}.muted{color:var(--muted)}.row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.score{display:flex;gap:10px;align-items:center}.score input{width:90px;text-align:center;font-size:22px;font-weight:800}.players{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.player{background:#0c1117;border:1px solid var(--line);border-radius:10px;padding:12px}.player input{width:80px;margin:5px 5px 0 0}.evidence{display:flex;gap:10px;flex-wrap:wrap}.evidence img{width:130px;height:100px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.status{padding:10px 12px;border-radius:9px;background:#17212b;color:#d9e3eb;margin:10px 0}.status.error{background:#401b20;color:#ffc2c6}.status.ok{background:#123324;color:#aaf0c8}.empty{color:var(--muted);padding:22px;text-align:center}@media(max-width:800px){.wrap{padding:14px}.layout{grid-template-columns:1fr}.players{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="wrap"><header class="top"><div><div class="brand">STAND<span>POINT</span> / MODERATION</div><div class="sub">ÐÑÐ¾Ð²ÐµÑÐºÐ° ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÐ¾Ð² â¢ MMR Ð¿ÑÐ¸Ð¼ÐµÐ½ÑÐµÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð¿Ð¾ÑÐ»Ðµ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð¸Ñ</div></div><button class="secondary" id="refresh">ÐÐ±Ð½Ð¾Ð²Ð¸ÑÑ Ð¾ÑÐµÑÐµÐ´Ñ</button></header>
<section class="panel"><div class="row"><div><b>ÐÐ¾ÑÑÑÐ¿ Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¾ÑÐ°</b><div class="sub">Ð¢Ð¾ÐºÐµÐ½ Ð¿ÑÐ¾Ð²ÐµÑÑÐµÑÑÑ ÑÐµÑÐ²ÐµÑÐ¾Ð¼ Ð¸ ÑÑÐ°Ð½Ð¸ÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð² ÑÑÐ¾Ð¹ Ð²ÐºÐ»Ð°Ð´ÐºÐµ.</div></div><span class="pill">SECURE API</span></div><div class="login" style="margin-top:12px"><input class="input" id="token" type="password" placeholder="RESULTS_ADMIN_TOKEN" autocomplete="current-password"><button id="connect">ÐÐ¾Ð¹ÑÐ¸</button><button class="secondary" id="logout">ÐÑÐ¹ÑÐ¸</button></div><div id="message" class="status" hidden></div></section>
<div class="layout"><section class="panel"><div class="row"><h3 style="margin:0">ÐÐ°ÑÑÐ¸ Ð½Ð° Ð¿ÑÐ¾Ð²ÐµÑÐºÐµ</h3><span class="pill" id="count">0</span></div><div class="sub">ÐÐ¶Ð¸Ð´Ð°ÑÑÐ¸Ðµ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ¸ Ð¸ ÑÐ¿Ð¾ÑÐ½ÑÐµ ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÑ</div><div id="queue" class="empty">ÐÐ¾Ð¹Ð´Ð¸ÑÐµ, ÑÑÐ¾Ð±Ñ Ð·Ð°Ð³ÑÑÐ·Ð¸ÑÑ Ð¼Ð°ÑÑÐ¸.</div></section>
<section class="panel" id="detail"><div class="empty">ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¼Ð°ÑÑ Ð² Ð¾ÑÐµÑÐµÐ´Ð¸.</div></section></div></main>
<script>
const $=id=>document.getElementById(id);let token='',selected=null,details=null,queue=[];const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function msg(text,type=''){const el=$('message');el.hidden=!text;el.className='status '+type;el.textContent=text}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json','X-Admin-Token':token,...(opts.headers||{})}});const body=await r.text();let data;try{data=body?JSON.parse(body):{}}catch{data={detail:body||('HTTP '+r.status)}}if(!r.ok)throw Error(data.detail||('HTTP '+r.status));return data}
function setToken(){token=$('token').value.trim();if(!token)throw Error('ÐÐ²ÐµÐ´Ð¸ÑÐµ ÑÐ¾ÐºÐµÐ½ Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¾ÑÐ°')}
async function loadQueue(){const d=await api('/admin/moderation/queue');queue=d.matches||[];$('count').textContent=queue.length;if(!queue.length){$('queue').innerHTML='<div class="empty">ÐÑÐµÑÐµÐ´Ñ Ð¿ÑÑÑÐ° â Ð½Ð¾Ð²ÑÑ Ð¼Ð°ÑÑÐµÐ¹ Ð½Ð° Ð¿ÑÐ¾Ð²ÐµÑÐºÐµ Ð½ÐµÑ.</div>';return} $('queue').innerHTML=queue.map(m=>`<button class="match ${selected===m.match_id?'active':''}" data-id="${esc(m.match_id)}"><div class="row"><b>${esc(m.mode)} Â· ${esc(m.map_name||'ÐÐ°ÑÑÐ° Ð½Ðµ ÑÐºÐ°Ð·Ð°Ð½Ð°')}</b><span class="pill">${esc(m.status)}</span></div><div class="muted">ID: ${esc(m.match_id)}</div><div>Ð¡ÑÑÑ: ${m.score_a??'â'} : ${m.score_b??'â'}</div><div class="muted">${esc((m.players||[]).length)} Ð¸Ð³ÑÐ¾ÐºÐ¾Ð² Â· ${esc(m.created_at||'')}</div></button>`).join('');document.querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>openMatch(b.dataset.id))}
async function openMatch(id){selected=id;details=await api('/admin/matches/'+encodeURIComponent(id));renderDetail();await loadQueue()}
function renderDetail(){const m=details||{},players=m.players||[],stats=m.stats||{},ev=m.evidence||{};let evidence=[];for(const [pid,sub] of Object.entries(ev)){for(const f of (sub.files||[]))evidence.push({pid,file:f})}
$('detail').innerHTML=`<div class="row"><div><h3 style="margin:0">ÐÐ°ÑÑ ${esc(m.match_id)}</h3><div class="sub">${esc(m.mode)} Â· ${esc(m.map_name||'ÐÐ°ÑÑÐ° Ð½Ðµ ÑÐºÐ°Ð·Ð°Ð½Ð°')} Â· ${esc(m.status)}</div></div><span class="pill">${players.length} Ð¸Ð³ÑÐ¾ÐºÐ¾Ð²</span></div><div class="panel" style="padding:12px;margin-top:14px"><div class="sub">ÐÑÐ¾Ð³Ð¾Ð²ÑÐ¹ ÑÑÑÑ (Ð¿Ð¾ Ð½ÐµÐ¼Ñ Ð¾Ð¿ÑÐµÐ´ÐµÐ»ÑÐµÑÑÑ Ð¿Ð¾Ð±ÐµÐ´Ð¸ÑÐµÐ»Ñ)</div><div class="score" style="margin-top:8px"><label>ÐÐ¾Ð¼Ð°Ð½Ð´Ð° A <input class="input" id="scoreA" type="number" min="0" max="100" value="${m.score_a??(Object.values(ev)[0]?.claimed_score_a??0)}"></label><b>:</b><label>ÐÐ¾Ð¼Ð°Ð½Ð´Ð° B <input class="input" id="scoreB" type="number" min="0" max="100" value="${m.score_b??(Object.values(ev)[0]?.claimed_score_b??0)}"></label></div><div class="sub" style="margin-top:8px">Ð¡Ð¾ÑÑÐ°Ð² Ð´ÐµÐ»Ð¸ÑÑÑ Ð½Ð° ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ A/B ÑÐ¾Ð³Ð»Ð°ÑÐ½Ð¾ ÑÐµÐ¶Ð¸Ð¼Ñ Ð¼Ð°ÑÑÐ°.</div></div><h4>ÐÐ³ÑÐ¾ÐºÐ¸ Ð¸ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ°</h4><div class="players">${players.map((pid,i)=>{const st=stats[pid]||{};return `<div class="player"><b>${i<players.length/2?'ÐÐ¾Ð¼Ð°Ð½Ð´Ð° A':'ÐÐ¾Ð¼Ð°Ð½Ð´Ð° B'}</b><div class="muted">ID: ${esc(pid)}</div><div>Ð£Ð±Ð¸Ð¹ÑÑÐ²Ð° <input class="input" type="number" min="0" max="1000" id="k_${esc(pid)}" value="${st.kills??''}" placeholder="â"> Ð¡Ð¼ÐµÑÑÐ¸ <input class="input" type="number" min="0" max="1000" id="d_${esc(pid)}" value="${st.deaths??''}" placeholder="â"></div></div>`}).join('')}</div><h4>ÐÐ¾ÐºÐ°Ð·Ð°ÑÐµÐ»ÑÑÑÐ²Ð°</h4><div class="evidence">${evidence.length?evidence.map(e=>`<div><div class="sub">ÐÐ³ÑÐ¾Ðº ${esc(e.pid)}</div><button class="secondary" data-evidence="${esc(e.file.file_id)}" data-mid="${esc(m.match_id)}">ÐÑÐºÑÑÑÑ ${esc(e.file.filename||'ÑÐºÑÐ¸Ð½ÑÐ¾Ñ')}</button></div>`).join(''):'<span class="muted">ÐÐ»Ð¾Ð¶ÐµÐ½Ð¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ñ.</span>'}</div><h4>ÐÐ¾Ð¼Ð¼ÐµÐ½ÑÐ°ÑÐ¸Ð¹ Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¾ÑÐ°</h4><textarea class="input" id="note" rows="3" style="width:100%;resize:vertical" placeholder="ÐÑÐ¸ÑÐ¸Ð½Ð° ÑÐµÑÐµÐ½Ð¸Ñ (Ð½ÐµÐ¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð¾)"></textarea><div class="actions"><button id="approve">â ÐÐ´Ð¾Ð±ÑÐ¸ÑÑ Ð¼Ð°ÑÑ Ð¸ Ð¿ÑÐ¸Ð¼ÐµÐ½Ð¸ÑÑ MMR</button><button class="danger" id="reject">â ÐÑÐºÐ»Ð¾Ð½Ð¸ÑÑ Ð±ÐµÐ· Ð½Ð°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ñ</button><button class="secondary" id="dispute">ÐÑÐ¼ÐµÑÐ¸ÑÑ ÑÐ¿Ð¾ÑÐ½ÑÐ¼</button></div><div class="sub" style="margin-top:12px">ÐÐ´Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ð´Ð¾Ð±Ð°Ð²Ð¸Ñ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÑ Ð¸ Ð¿ÑÐ¸Ð¼ÐµÐ½Ð¸Ñ MMR Ð¾Ð´Ð¸Ð½ ÑÐ°Ð·. ÐÑÐºÐ»Ð¾Ð½ÐµÐ½Ð¸Ðµ Ð½Ðµ Ð¼ÐµÐ½ÑÐµÑ ÑÐµÐ¹ÑÐ¸Ð½Ð³ Ð¸ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÑ.</div>`;
$('approve').onclick=()=>review('approve');$('reject').onclick=()=>review('reject');$('dispute').onclick=()=>review('dispute');document.querySelectorAll('[data-evidence]').forEach(b=>b.onclick=()=>openEvidence(b.dataset.mid,b.dataset.evidence));}
async function openEvidence(mid,fid){try{const r=await fetch(`/admin/matches/${encodeURIComponent(mid)}/evidence/${encodeURIComponent(fid)}`,{headers:{'X-Admin-Token':token}});if(!r.ok)throw Error('ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð³ÑÑÐ·Ð¸ÑÑ ÑÐ°Ð¹Ð»');const url=URL.createObjectURL(await r.blob());const w=window.open(url,'_blank');if(!w)msg('ÐÑÐ°ÑÐ·ÐµÑ Ð·Ð°Ð±Ð»Ð¾ÐºÐ¸ÑÐ¾Ð²Ð°Ð» Ð¾ÐºÐ½Ð¾ Ñ Ð´Ð¾ÐºÐ°Ð·Ð°ÑÐµÐ»ÑÑÑÐ²Ð¾Ð¼. Ð Ð°Ð·ÑÐµÑÐ¸ÑÐµ Ð²ÑÐ¿Ð»ÑÐ²Ð°ÑÑÐ¸Ðµ Ð¾ÐºÐ½Ð°.','error')}catch(e){msg(e.message,'error')}}
async function review(decision){try{const a=Number($('scoreA').value),b=Number($('scoreB').value);if(decision==='approve'&&(!Number.isInteger(a)||!Number.isInteger(b)||a<0||b<0))throw Error('Ð£ÐºÐ°Ð¶Ð¸ÑÐµ ÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¹ ÑÑÑÑ Ð¾Ð±ÐµÐ¸Ñ ÐºÐ¾Ð¼Ð°Ð½Ð´');const stats={};for(const pid of details.players||[]){const k=$('k_'+pid)?.value,d=$('d_'+pid)?.value;if(k!==''||d!==''){stats[pid]={};if(k!=='')stats[pid].kills=Number(k);if(d!=='')stats[pid].deaths=Number(d)}}const body={decision,score_a:decision==='approve'?a:null,score_b:decision==='approve'?b:null,stats,note:$('note').value};const result=await api('/admin/matches/'+encodeURIComponent(selected)+'/review',{method:'POST',body:JSON.stringify(body)});msg(decision==='approve'?'ÐÐ°ÑÑ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½. MMR Ð¸ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ° Ð¿ÑÐ¸Ð¼ÐµÐ½ÐµÐ½Ñ.':decision==='reject'?'ÐÐ°ÑÑ Ð¾ÑÐºÐ»Ð¾Ð½ÑÐ½. MMR Ð¸ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ° Ð½Ðµ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ñ.':'ÐÐ°ÑÑ Ð¾ÑÐ¼ÐµÑÐµÐ½ ÐºÐ°Ðº ÑÐ¿Ð¾ÑÐ½ÑÐ¹.', 'ok');await loadQueue();if(decision!=='dispute'&&queue.length){await openMatch(queue[0].match_id)}else if(!queue.length){$('detail').innerHTML='<div class="empty">ÐÑÐµÑÐµÐ´Ñ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐ°Ð½Ð°.</div>'}}catch(e){msg(e.message,'error')}}
$('connect').onclick=async()=>{try{setToken();await loadQueue();msg('ÐÐ¾ÑÑÑÐ¿ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½.','ok')}catch(e){msg(e.message,'error')}};$('refresh').onclick=async()=>{try{if(!token)setToken();await loadQueue();msg('ÐÑÐµÑÐµÐ´Ñ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð°.','ok')}catch(e){msg(e.message,'error')}};$('logout').onclick=()=>{token='';selected=null;details=null;$('token').value='';$('queue').innerHTML='<div class="empty">ÐÐ¾Ð¹Ð´Ð¸ÑÐµ, ÑÑÐ¾Ð±Ñ Ð·Ð°Ð³ÑÑÐ·Ð¸ÑÑ Ð¼Ð°ÑÑÐ¸.</div>';$('detail').innerHTML='<div class="empty">ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¼Ð°ÑÑ Ð² Ð¾ÑÐµÑÐµÐ´Ð¸.</div>';$('count').textContent='0';msg('ÐÑ Ð²ÑÑÐ»Ð¸ Ð¸Ð· Ð¿Ð°Ð½ÐµÐ»Ð¸.','')};
</script></body></html>''')



@app.get("/api-info")
def api_info():
    return {
        "service": "STANDPOINT Competitive Match API",
        "status": "ok",
        "version": "3.1.0",
        "frontend": "/",
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
            payload.nickname or "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº",
            payload.game_id,
            stamp,
            stamp,
            INITIAL_ELO,
        ))

        row = con.execute(
            "SELECT * FROM players WHERE telegram_id=?",
            (payload.telegram_id,)
        ).fetchone()
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id, updated_at) VALUES(?,?)",
                    (payload.telegram_id, stamp))

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
        raise HTTPException(404, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂ½ÃÂµ ÃÂ·ÃÂ°ÃÂÃÂµÃÂ³ÃÂ¸ÃÂÃÂÃÂÃÂ¸ÃÂÃÂ¾ÃÂ²ÃÂ°ÃÂ½")

    player = dict(row)
    player["kd"] = (
        round(player["kills"] / player["deaths"], 2)
        if player["deaths"]
        else float(player["kills"])
    )

    with db() as con:
        settings = con.execute("SELECT points,active_frame,active_title,active_profile_card FROM player_cosmetic_settings WHERE telegram_id=?", (telegram_id,)).fetchone()
        awards = con.execute("SELECT qualification_key,place,trophy_name,elo_snapshot,awarded_at FROM qualification_awards WHERE telegram_id=? ORDER BY awarded_at DESC", (telegram_id,)).fetchall()
        totals = con.execute("SELECT COUNT(*) AS matches, COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),0) AS wins, COALESCE(AVG(kd),0) AS avg_kd FROM match_history WHERE telegram_id=?", (telegram_id,)).fetchone()
        recent = con.execute("SELECT outcome, elo_delta, kills, deaths, kd, created_at FROM match_history WHERE telegram_id=? ORDER BY created_at DESC LIMIT 20", (telegram_id,)).fetchall()
    return {
        "player": player,
        "match_history": [dict(item) for item in history],
        "cosmetics": dict(settings) if settings else {"points": 0, "active_frame": None, "active_title": None, "active_profile_card": None},
        "trophies": [dict(item) for item in awards],
        "extended_stats": {"matches": totals["matches"], "wins": totals["wins"], "win_rate": round(100 * totals["wins"] / totals["matches"], 1) if totals["matches"] else 0, "average_kd": round(float(totals["avg_kd"] or 0), 2), "recent_form": [dict(item) for item in recent]},
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
            400, "ÃÂÃÂ¾ÃÂÃÂÃÂÃÂ¿ÃÂ½ÃÂ¾: 1v1, 2v2, 3v3, 5v5"
        )

    stamp = now()

    with db() as con:
        player = con.execute(
            "SELECT 1 FROM players WHERE telegram_id=?",
            (payload.telegram_id,)
        ).fetchone()

        if not player:
            raise HTTPException(
                400, "ÃÂ¡ÃÂ½ÃÂ°ÃÂÃÂ°ÃÂ»ÃÂ° ÃÂ·ÃÂ°ÃÂÃÂµÃÂ³ÃÂ¸ÃÂÃÂÃÂÃÂ¸ÃÂÃÂÃÂ¹ ÃÂ¿ÃÂÃÂ¾ÃÂÃÂ¸ÃÂ»ÃÂ"
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
                409, "ÃÂ£ ÃÂÃÂµÃÂ±ÃÂ ÃÂÃÂ¶ÃÂµ ÃÂµÃÂÃÂÃÂ ÃÂ°ÃÂºÃÂÃÂ¸ÃÂ²ÃÂ½ÃÂÃÂ¹ ÃÂ¼ÃÂ°ÃÂÃÂ"
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
            create_notification(con, pid, "match_found", "ÃÂÃÂ°ÃÂÃÂ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½",
                f"ÃÂ ÃÂµÃÂ¶ÃÂ¸ÃÂ¼ {mode}: ÃÂ¼ÃÂ°ÃÂÃÂ {match_id}",
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
        raise HTTPException(400, "ÃÂÃÂµÃÂ¸ÃÂ·ÃÂ²ÃÂµÃÂÃÂÃÂ½ÃÂÃÂ¹ ÃÂÃÂµÃÂ¶ÃÂ¸ÃÂ¼")

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
            raise HTTPException(409, "ÃÂÃÂÃÂ±ÃÂ¾ÃÂ ÃÂºÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½ÃÂ° ÃÂ·ÃÂ°ÃÂºÃÂÃÂÃÂ")

        team_size = MODES[match["mode"]]
        team_a, team_b = match["players"][:team_size], match["players"][team_size:]
        allowed = team_a if payload.team == "A" else team_b
        if payload.captain_id not in allowed:
            raise HTTPException(400, "ÃÂÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½ ÃÂ´ÃÂ¾ÃÂ»ÃÂ¶ÃÂµÃÂ½ ÃÂ±ÃÂÃÂÃÂ ÃÂÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂºÃÂ¾ÃÂ¼ ÃÂ²ÃÂÃÂ±ÃÂÃÂ°ÃÂ½ÃÂ½ÃÂ¾ÃÂ¹ ÃÂºÃÂ¾ÃÂ¼ÃÂ°ÃÂ½ÃÂ´ÃÂ")

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
            400, "ÃÂÃÂ°ÃÂÃÂÃÂ° ÃÂ¾ÃÂÃÂÃÂÃÂÃÂÃÂÃÂ²ÃÂÃÂµÃÂ ÃÂ² ÃÂÃÂ°ÃÂ·ÃÂÃÂµÃÂÃÂÃÂ½ÃÂ½ÃÂ¾ÃÂ¼ ÃÂÃÂ¿ÃÂ¸ÃÂÃÂºÃÂµ"
        )

    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, payload.telegram_id)

        if match["status"] != "map_pick":
            raise HTTPException(409, "ÃÂÃÂ°ÃÂ½-ÃÂ¿ÃÂ¸ÃÂº ÃÂÃÂ¶ÃÂµ ÃÂ·ÃÂ°ÃÂ²ÃÂµÃÂÃÂÃÂÃÂ½")

        actions = match["map_actions"]
        team_size = MODES[match["mode"]]
        team_a, team_b = match["players"][:team_size], match["players"][team_size:]
        captain_a = match.get("captain_id")
        captain_b = match.get("captain_b_id") or team_b[0]
        if not captain_a:
            raise HTTPException(409, "ÃÂ¡ÃÂ½ÃÂ°ÃÂÃÂ°ÃÂ»ÃÂ° ÃÂ½ÃÂ°ÃÂ·ÃÂ½ÃÂ°ÃÂÃÂÃÂÃÂµ ÃÂºÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½ÃÂ° ÃÂºÃÂ¾ÃÂ¼ÃÂ°ÃÂ½ÃÂ´ÃÂ A")

        ban_limit = min(4, max(0, len(MAPS) - 1))
        ban_count = sum(1 for action in actions if action.get("action") == "ban")
        if ban_count < ban_limit:
            expected_captain = captain_a if ban_count % 2 == 0 else captain_b
            if payload.action != "ban":
                raise HTTPException(409, "ÃÂ¡ÃÂ½ÃÂ°ÃÂÃÂ°ÃÂ»ÃÂ° ÃÂ·ÃÂ°ÃÂ²ÃÂµÃÂÃÂÃÂ¸ÃÂÃÂµ ÃÂ²ÃÂÃÂµ ÃÂ±ÃÂ°ÃÂ½ÃÂ ÃÂ¿ÃÂ¾ ÃÂ¾ÃÂÃÂµÃÂÃÂµÃÂ´ÃÂ¸")
            if payload.telegram_id != expected_captain:
                raise HTTPException(403, "ÃÂ¡ÃÂµÃÂ¹ÃÂÃÂ°ÃÂ ÃÂÃÂ¾ÃÂ´ ÃÂ´ÃÂÃÂÃÂ³ÃÂ¾ÃÂ³ÃÂ¾ ÃÂºÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½ÃÂ°")
        else:
            if payload.action != "pick":
                raise HTTPException(409, "ÃÂÃÂ°ÃÂ½ÃÂ ÃÂ·ÃÂ°ÃÂ²ÃÂµÃÂÃÂÃÂµÃÂ½ÃÂ: ÃÂ½ÃÂµÃÂ¾ÃÂ±ÃÂÃÂ¾ÃÂ´ÃÂ¸ÃÂ¼ÃÂ¾ ÃÂ²ÃÂÃÂ±ÃÂÃÂ°ÃÂÃÂ ÃÂ¾ÃÂÃÂÃÂ°ÃÂ²ÃÂÃÂÃÂÃÂÃÂ ÃÂºÃÂ°ÃÂÃÂÃÂ")
            if payload.telegram_id != captain_a:
                raise HTTPException(403, "ÃÂÃÂ°ÃÂÃÂÃÂ ÃÂ²ÃÂÃÂ±ÃÂ¸ÃÂÃÂ°ÃÂµÃÂ ÃÂºÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½ ÃÂºÃÂ¾ÃÂ¼ÃÂ°ÃÂ½ÃÂ´ÃÂ A")

        if any(a["map_name"] == name for a in actions):
            raise HTTPException(
                409, "ÃÂ­ÃÂÃÂ° ÃÂºÃÂ°ÃÂÃÂÃÂ° ÃÂÃÂ¶ÃÂµ ÃÂ²ÃÂÃÂ±ÃÂÃÂ°ÃÂ½ÃÂ° ÃÂ¸ÃÂ»ÃÂ¸ ÃÂ·ÃÂ°ÃÂ±ÃÂ°ÃÂ½ÃÂµÃÂ½ÃÂ°"
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
                403, "ÃÂÃÂ°ÃÂ¿ÃÂÃÂÃÂÃÂ¸ÃÂÃÂ ÃÂ¼ÃÂ°ÃÂÃÂ ÃÂ¼ÃÂ¾ÃÂ¶ÃÂµÃÂ ÃÂÃÂ¾ÃÂ»ÃÂÃÂºÃÂ¾ ÃÂºÃÂ°ÃÂ¿ÃÂ¸ÃÂÃÂ°ÃÂ½"
            )

        if match["status"] != "ready" or not match["map_name"]:
            raise HTTPException(
                409, "ÃÂ¡ÃÂ½ÃÂ°ÃÂÃÂ°ÃÂ»ÃÂ° ÃÂ·ÃÂ°ÃÂ²ÃÂµÃÂÃÂÃÂ¸ÃÂÃÂµ ÃÂ±ÃÂ°ÃÂ½-ÃÂ¿ÃÂ¸ÃÂº ÃÂ¸ ÃÂ²ÃÂÃÂ±ÃÂµÃÂÃÂ¸ÃÂÃÂµ ÃÂºÃÂ°ÃÂÃÂÃÂ"
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
    """ÃÂÃÂ°ÃÂ³ÃÂÃÂÃÂ·ÃÂºÃÂ° ÃÂÃÂºÃÂÃÂ¸ÃÂ½ÃÂÃÂ¾ÃÂÃÂ¾ÃÂ² ÃÂ½ÃÂµÃÂ¿ÃÂ¾ÃÂÃÂÃÂµÃÂ´ÃÂÃÂÃÂ²ÃÂµÃÂ½ÃÂ½ÃÂ¾ ÃÂ¸ÃÂ· Mini App."""

    if not screenshots or len(screenshots) > 6:
        raise HTTPException(
            400, "ÃÂÃÂÃÂ¸ÃÂºÃÂÃÂµÃÂ¿ÃÂ¸ÃÂÃÂµ ÃÂ¾ÃÂ 1 ÃÂ´ÃÂ¾ 6 ÃÂÃÂºÃÂÃÂ¸ÃÂ½ÃÂÃÂ¾ÃÂÃÂ¾ÃÂ²"
        )

    try:
        submitted_stats = json.loads(stats_json or "{}")
    except json.JSONDecodeError:
        raise HTTPException(
            400, "stats_json ÃÂ´ÃÂ¾ÃÂ»ÃÂ¶ÃÂµÃÂ½ ÃÂ±ÃÂÃÂÃÂ ÃÂºÃÂ¾ÃÂÃÂÃÂµÃÂºÃÂÃÂ½ÃÂÃÂ¼ JSON"
        )

    if not isinstance(submitted_stats, dict):
        raise HTTPException(
            400, "stats_json ÃÂ´ÃÂ¾ÃÂ»ÃÂ¶ÃÂµÃÂ½ ÃÂ±ÃÂÃÂÃÂ ÃÂ¾ÃÂ±ÃÂÃÂµÃÂºÃÂÃÂ¾ÃÂ¼"
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
                400, "ÃÂÃÂ¾ÃÂ´ÃÂ´ÃÂµÃÂÃÂ¶ÃÂ¸ÃÂ²ÃÂ°ÃÂÃÂÃÂÃÂ ÃÂÃÂ¾ÃÂ»ÃÂÃÂºÃÂ¾ JPG, PNG ÃÂ¸ WEBP"
            )

        data = await upload.read(max_size + 1)

        if not data:
            raise HTTPException(400, "ÃÂÃÂ´ÃÂ¸ÃÂ½ ÃÂ¸ÃÂ· ÃÂÃÂ°ÃÂ¹ÃÂ»ÃÂ¾ÃÂ² ÃÂ¿ÃÂÃÂÃÂÃÂ¾ÃÂ¹")

        if len(data) > max_size:
            raise HTTPException(
                413, "ÃÂÃÂ°ÃÂºÃÂÃÂ¸ÃÂ¼ÃÂ°ÃÂ»ÃÂÃÂ½ÃÂÃÂ¹ ÃÂÃÂ°ÃÂ·ÃÂ¼ÃÂµÃÂ ÃÂÃÂºÃÂÃÂ¸ÃÂ½ÃÂÃÂ¾ÃÂÃÂ° Ã¢ÂÂ 5 ÃÂÃÂ"
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
                400, "ÃÂ¤ÃÂ°ÃÂ¹ÃÂ» ÃÂ½ÃÂµ ÃÂÃÂ¾ÃÂ¾ÃÂÃÂ²ÃÂµÃÂÃÂÃÂÃÂ²ÃÂÃÂµÃÂ ÃÂÃÂ¸ÃÂ¿ÃÂ ÃÂ¸ÃÂ·ÃÂ¾ÃÂ±ÃÂÃÂ°ÃÂ¶ÃÂµÃÂ½ÃÂ¸ÃÂ"
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
                409, "ÃÂ¡ÃÂµÃÂ¹ÃÂÃÂ°ÃÂ ÃÂÃÂºÃÂÃÂ¸ÃÂ½ÃÂÃÂ¾ÃÂÃÂ ÃÂ¾ÃÂÃÂ¿ÃÂÃÂ°ÃÂ²ÃÂ¸ÃÂÃÂ ÃÂ½ÃÂµÃÂ»ÃÂÃÂ·ÃÂ"
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
            404, "ÃÂ¤ÃÂ¾ÃÂÃÂ¾ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ÃÂ¾ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂÃÂ¶ÃÂµ ÃÂÃÂ´ÃÂ°ÃÂ»ÃÂµÃÂ½ÃÂ¾"
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
                409, "ÃÂÃÂ°ÃÂÃÂ ÃÂ½ÃÂµ ÃÂ¾ÃÂ¶ÃÂ¸ÃÂ´ÃÂ°ÃÂµÃÂ ÃÂ¿ÃÂÃÂ¾ÃÂ²ÃÂµÃÂÃÂºÃÂ¸"
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
                400, "ÃÂÃÂ»ÃÂ ÃÂ¿ÃÂ¾ÃÂ´ÃÂÃÂ²ÃÂµÃÂÃÂ¶ÃÂ´ÃÂµÃÂ½ÃÂ¸ÃÂ ÃÂÃÂºÃÂ°ÃÂ¶ÃÂ¸ÃÂÃÂµ score_a ÃÂ¸ score_b"
            )

        if match["elo_applied"]:
            raise HTTPException(
                409, "ELO ÃÂ·ÃÂ° ÃÂÃÂÃÂ¾ÃÂ ÃÂ¼ÃÂ°ÃÂÃÂ ÃÂÃÂ¶ÃÂµ ÃÂ¿ÃÂÃÂ¸ÃÂ¼ÃÂµÃÂ½ÃÂÃÂ½"
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
                    "ÃÂ¡ÃÂÃÂ°ÃÂÃÂ¸ÃÂÃÂÃÂ¸ÃÂºÃÂ° ÃÂÃÂ¾ÃÂ´ÃÂµÃÂÃÂ¶ÃÂ¸ÃÂ ÃÂ¸ÃÂ³ÃÂÃÂ¾ÃÂºÃÂ° ÃÂ½ÃÂµ ÃÂ¸ÃÂ· ÃÂÃÂÃÂ¾ÃÂ³ÃÂ¾ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ°"
                )

            if not isinstance(values, dict):
                raise HTTPException(
                    400, "ÃÂ¡ÃÂÃÂ°ÃÂÃÂ¸ÃÂÃÂÃÂ¸ÃÂºÃÂ° ÃÂ¸ÃÂ³ÃÂÃÂ¾ÃÂºÃÂ° ÃÂ´ÃÂ¾ÃÂ»ÃÂ¶ÃÂ½ÃÂ° ÃÂ±ÃÂÃÂÃÂ ÃÂ¾ÃÂ±ÃÂÃÂµÃÂºÃÂÃÂ¾ÃÂ¼"
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
                        "Kills/deaths ÃÂ´ÃÂ¾ÃÂ»ÃÂ¶ÃÂ½ÃÂ ÃÂ±ÃÂÃÂÃÂ ÃÂÃÂµÃÂ»ÃÂÃÂ¼ÃÂ¸ ÃÂÃÂ¸ÃÂÃÂ»ÃÂ°ÃÂ¼ÃÂ¸ "
                        "ÃÂ¾ÃÂ 0 ÃÂ´ÃÂ¾ 1000"
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
                raise HTTPException(400, "ÃÂ£ÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂº ÃÂ¼ÃÂ°ÃÂÃÂÃÂ° ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ² ÃÂ±ÃÂ°ÃÂ·ÃÂµ")
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
                    400, "ÃÂ£ÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂº ÃÂ¼ÃÂ°ÃÂÃÂÃÂ° ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ² ÃÂ±ÃÂ°ÃÂ·ÃÂµ"
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
                        create_notification(con, pid, "pass_reward_unlocked", "ÃÂÃÂ°ÃÂ³ÃÂÃÂ°ÃÂ´ÃÂ° STANDPOINT Pass ÃÂ¾ÃÂÃÂºÃÂÃÂÃÂÃÂ°",
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
                409, "ÃÂ ÃÂµÃÂ·ÃÂÃÂ»ÃÂÃÂÃÂ°ÃÂ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ° ÃÂµÃÂÃÂ ÃÂ½ÃÂµ ÃÂ¿ÃÂ¾ÃÂ´ÃÂÃÂ²ÃÂµÃÂÃÂ¶ÃÂ´ÃÂÃÂ½"
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
        raise HTTPException(404, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂ½ÃÂµ ÃÂ·ÃÂ°ÃÂÃÂµÃÂ³ÃÂ¸ÃÂÃÂÃÂÃÂ¸ÃÂÃÂ¾ÃÂ²ÃÂ°ÃÂ½")
    return row


def require_clan_role(con, clan_id: str, telegram_id: str, roles: tuple[str, ...]):
    row = con.execute("SELECT role FROM clan_members WHERE clan_id=? AND telegram_id=?",
                      (clan_id, telegram_id)).fetchone()
    if not row or row["role"] not in roles:
        raise HTTPException(403, "ÃÂÃÂµÃÂ´ÃÂ¾ÃÂÃÂÃÂ°ÃÂÃÂ¾ÃÂÃÂ½ÃÂ¾ ÃÂ¿ÃÂÃÂ°ÃÂ² ÃÂ² ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂµ")
    return row


class CosmeticPurchase(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)

class CosmeticEquip(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=80)

class CosmeticPointsAdjust(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    delta: int = Field(ge=-1000000, le=1000000)
    reason: str = Field(default="admin_adjustment", max_length=100)

class QualificationAwardCreate(BaseModel):
    qualification_key: str = Field(min_length=2, max_length=100)
    placements: list[str] = Field(min_length=1, max_length=10)


@app.get("/cosmetics/shop")
def cosmetics_shop(item_type: Optional[Literal["frame", "title", "profile_card"]] = None):
    with db() as con:
        query = "SELECT item_id,name,item_type,description,price_points,rarity FROM cosmetic_items WHERE active=1"
        args = []
        if item_type:
            query += " AND item_type=?"
            args.append(item_type)
        query += " ORDER BY price_points, name"
        rows = con.execute(query, args).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows), "currency": "SP Points"}


@app.get("/cosmetics/{telegram_id}")
def player_cosmetics(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        settings = con.execute("SELECT * FROM player_cosmetic_settings WHERE telegram_id=?", (telegram_id,)).fetchone()
        items = con.execute("SELECT i.*, pc.acquired_at, pc.source FROM player_cosmetics pc JOIN cosmetic_items i ON i.item_id=pc.item_id WHERE pc.telegram_id=? ORDER BY pc.acquired_at DESC", (telegram_id,)).fetchall()
    return {"settings": dict(settings) if settings else {"telegram_id": telegram_id, "points": 0}, "items": [dict(r) for r in items]}


@app.post("/cosmetics/{item_id}/purchase")
def purchase_cosmetic(item_id: str, payload: CosmeticPurchase):
    stamp = now()
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        item = con.execute("SELECT * FROM cosmetic_items WHERE item_id=? AND active=1", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404, "ÃÂÃÂÃÂµÃÂ´ÃÂ¼ÃÂµÃÂ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂÃÂ½ÃÂÃÂ ÃÂ ÃÂ¿ÃÂÃÂ¾ÃÂ´ÃÂ°ÃÂ¶ÃÂ¸")
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id,updated_at) VALUES(?,?)", (payload.telegram_id, stamp))
        if con.execute("SELECT 1 FROM player_cosmetics WHERE telegram_id=? AND item_id=?", (payload.telegram_id,item_id)).fetchone():
            raise HTTPException(409, "ÃÂÃÂÃÂµÃÂ´ÃÂ¼ÃÂµÃÂ ÃÂÃÂ¶ÃÂµ ÃÂµÃÂÃÂÃÂ ÃÂ² ÃÂºÃÂ¾ÃÂ»ÃÂ»ÃÂµÃÂºÃÂÃÂ¸ÃÂ¸")
        settings = con.execute("SELECT points FROM player_cosmetic_settings WHERE telegram_id=?", (payload.telegram_id,)).fetchone()
        if settings["points"] < item["price_points"]:
            raise HTTPException(409, "ÃÂÃÂµÃÂ´ÃÂ¾ÃÂÃÂÃÂ°ÃÂÃÂ¾ÃÂÃÂ½ÃÂ¾ SP Points")
        con.execute("UPDATE player_cosmetic_settings SET points=points-?,updated_at=? WHERE telegram_id=?", (item["price_points"],stamp,payload.telegram_id))
        con.execute("INSERT INTO player_cosmetics(telegram_id,item_id,acquired_at,source) VALUES(?,?,?,'shop')", (payload.telegram_id,item_id,stamp))
    return {"status":"purchased","item_id":item_id,"spent":item["price_points"]}


@app.post("/cosmetics/equip")
def equip_cosmetic(payload: CosmeticEquip):
    stamp = now()
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        row = con.execute("SELECT i.item_type FROM cosmetic_items i JOIN player_cosmetics p ON p.item_id=i.item_id WHERE i.item_id=? AND p.telegram_id=?", (payload.item_id,payload.telegram_id)).fetchone()
        if not row:
            raise HTTPException(404, "ÃÂÃÂÃÂµÃÂ´ÃÂ¼ÃÂµÃÂ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ² ÃÂ²ÃÂ°ÃÂÃÂµÃÂ¹ ÃÂºÃÂ¾ÃÂ»ÃÂ»ÃÂµÃÂºÃÂÃÂ¸ÃÂ¸")
        column = {"frame":"active_frame","title":"active_title","profile_card":"active_profile_card"}[row["item_type"]]
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id,updated_at) VALUES(?,?)", (payload.telegram_id,stamp))
        con.execute(f"UPDATE player_cosmetic_settings SET {column}=?,updated_at=? WHERE telegram_id=?", (payload.item_id,stamp,payload.telegram_id))
    return {"status":"equipped","item_id":payload.item_id,"item_type":row["item_type"]}


@app.get("/players/{telegram_id}/statistics")
def extended_player_statistics(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        overall = con.execute("SELECT COUNT(*) matches, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins, SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) losses, SUM(CASE WHEN outcome='draw' THEN 1 ELSE 0 END) draws, SUM(kills) kills, SUM(deaths) deaths, AVG(kd) average_kd FROM match_history WHERE telegram_id=?", (telegram_id,)).fetchone()
        by_mode = con.execute("SELECT m.mode, COUNT(*) matches, SUM(CASE WHEN h.outcome='win' THEN 1 ELSE 0 END) wins, AVG(h.kd) average_kd FROM match_history h JOIN matches m ON m.match_id=h.match_id WHERE h.telegram_id=? GROUP BY m.mode ORDER BY matches DESC", (telegram_id,)).fetchall()
        timeline = con.execute("SELECT match_id,outcome,elo_before,elo_delta,kills,deaths,kd,created_at FROM match_history WHERE telegram_id=? ORDER BY created_at DESC LIMIT 50", (telegram_id,)).fetchall()
    d=dict(overall)
    for k in ("matches","wins","losses","draws","kills","deaths"): d[k]=int(d[k] or 0)
    d["win_rate"]=round(d["wins"]*100/d["matches"],1) if d["matches"] else 0
    d["kd"]=round(d["kills"]/d["deaths"],2) if d["deaths"] else float(d["kills"])
    d["average_kd"]=round(float(d["average_kd"] or 0),2)
    return {"telegram_id":telegram_id,"overall":d,"by_mode":[dict(r) for r in by_mode],"timeline":[dict(r) for r in timeline]}


@app.get("/players/{telegram_id}/trophies")
def player_trophies(telegram_id: str):
    with db() as con:
        require_registered_player(con, telegram_id)
        rows=con.execute("SELECT qualification_key,place,trophy_name,elo_snapshot,awarded_at FROM qualification_awards WHERE telegram_id=? ORDER BY awarded_at DESC",(telegram_id,)).fetchall()
    return {"trophies":[dict(r) for r in rows],"count":len(rows)}


@app.post("/admin/qualifications/{qualification_key}/award")
def award_qualification(qualification_key: str, payload: QualificationAwardCreate, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    if qualification_key != payload.qualification_key:
        raise HTTPException(400,"ÃÂÃÂ»ÃÂÃÂ ÃÂºÃÂ²ÃÂ°ÃÂ»ÃÂ¸ÃÂÃÂ¸ÃÂºÃÂ°ÃÂÃÂ¸ÃÂ¸ ÃÂ² URL ÃÂ¸ ÃÂÃÂµÃÂ»ÃÂµ ÃÂ½ÃÂµ ÃÂÃÂ¾ÃÂ²ÃÂ¿ÃÂ°ÃÂ´ÃÂ°ÃÂµÃÂ")
    stamp=now(); awarded=[]
    with db() as con:
        for place, telegram_id in enumerate(payload.placements, start=1):
            if not con.execute("SELECT 1 FROM players WHERE telegram_id=?",(telegram_id,)).fetchone():
                raise HTTPException(404,f"ÃÂÃÂ³ÃÂÃÂ¾ÃÂº {telegram_id} ÃÂ½ÃÂµ ÃÂ·ÃÂ°ÃÂÃÂµÃÂ³ÃÂ¸ÃÂÃÂÃÂÃÂ¸ÃÂÃÂ¾ÃÂ²ÃÂ°ÃÂ½")
            elo=con.execute("SELECT elo FROM players WHERE telegram_id=?",(telegram_id,)).fetchone()["elo"]
            trophy_name={1:"STANDPOINT Ã¢ÂÂ ÃÂ§ÃÂµÃÂ¼ÃÂ¿ÃÂ¸ÃÂ¾ÃÂ½ ÃÂºÃÂ²ÃÂ°ÃÂ»ÃÂ¸ÃÂÃÂ¸ÃÂºÃÂ°ÃÂÃÂ¸ÃÂ¸",2:"STANDPOINT Ã¢ÂÂ ÃÂ¡ÃÂµÃÂÃÂµÃÂ±ÃÂÃÂÃÂ½ÃÂÃÂ¹ ÃÂºÃÂÃÂ±ÃÂ¾ÃÂº",3:"STANDPOINT Ã¢ÂÂ ÃÂÃÂÃÂ¾ÃÂ½ÃÂ·ÃÂ¾ÃÂ²ÃÂÃÂ¹ ÃÂºÃÂÃÂ±ÃÂ¾ÃÂº"}.get(place,f"STANDPOINT Ã¢ÂÂ ÃÂ¢ÃÂ¾ÃÂ¿-{place} ÃÂºÃÂ²ÃÂ°ÃÂ»ÃÂ¸ÃÂÃÂ¸ÃÂºÃÂ°ÃÂÃÂ¸ÃÂ¸")
            award_id=uuid.uuid4().hex
            con.execute("INSERT INTO qualification_awards(award_id,qualification_key,telegram_id,place,trophy_name,elo_snapshot,awarded_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(qualification_key,telegram_id) DO UPDATE SET place=excluded.place,trophy_name=excluded.trophy_name,elo_snapshot=excluded.elo_snapshot,awarded_at=excluded.awarded_at",(award_id,qualification_key,telegram_id,place,trophy_name,elo,stamp))
            create_notification(con,telegram_id,"qualification_trophy","ÃÂÃÂ¾ÃÂ»ÃÂÃÂÃÂµÃÂ½ ÃÂºÃÂÃÂ±ÃÂ¾ÃÂº STANDPOINT",trophy_name,{"qualification_key":qualification_key,"place":place})
            awarded.append({"telegram_id":telegram_id,"place":place,"trophy_name":trophy_name,"elo_snapshot":elo})
    return {"status":"awarded","qualification_key":qualification_key,"awards":awarded}


@app.post("/admin/cosmetics")
def admin_create_cosmetic(item_id: str, name: str, item_type: Literal["frame","title","profile_card"], price_points: int = 0, description: str = "", rarity: str = "common", x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    if not item_id.strip() or not name.strip() or price_points < 0 or price_points > 1000000:
        raise HTTPException(400,"ÃÂÃÂÃÂ¾ÃÂ²ÃÂµÃÂÃÂÃÂÃÂµ ID, ÃÂ½ÃÂ°ÃÂ·ÃÂ²ÃÂ°ÃÂ½ÃÂ¸ÃÂµ ÃÂ¸ ÃÂÃÂµÃÂ½ÃÂ")
    with db() as con:
        con.execute("INSERT INTO cosmetic_items(item_id,name,item_type,description,price_points,rarity,active,created_at) VALUES(?,?,?,?,?,?,1,?)",(item_id.strip(),name.strip(),item_type,description[:500],price_points,rarity[:30],now()))
    return {"status":"created","item_id":item_id,"item_type":item_type}


@app.post("/admin/cosmetics/points")
def admin_adjust_cosmetic_points(payload: CosmeticPointsAdjust, x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    with db() as con:
        require_registered_player(con,payload.telegram_id)
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id,updated_at) VALUES(?,?)",(payload.telegram_id,now()))
        row=con.execute("SELECT points FROM player_cosmetic_settings WHERE telegram_id=?",(payload.telegram_id,)).fetchone()
        new_points=row["points"]+payload.delta
        if new_points < 0: raise HTTPException(409,"ÃÂÃÂ°ÃÂ»ÃÂ°ÃÂ½ÃÂ ÃÂ½ÃÂµ ÃÂ¼ÃÂ¾ÃÂ¶ÃÂµÃÂ ÃÂ±ÃÂÃÂÃÂ ÃÂ¾ÃÂÃÂÃÂ¸ÃÂÃÂ°ÃÂÃÂµÃÂ»ÃÂÃÂ½ÃÂÃÂ¼")
        con.execute("UPDATE player_cosmetic_settings SET points=?,updated_at=? WHERE telegram_id=?",(new_points,now(),payload.telegram_id))
    return {"status":"updated","points":new_points,"reason":payload.reason}


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
        raise HTTPException(400, "ÃÂÃÂµÃÂ»ÃÂÃÂ·ÃÂ ÃÂ´ÃÂ¾ÃÂ±ÃÂ°ÃÂ²ÃÂ¸ÃÂÃÂ ÃÂÃÂ°ÃÂ¼ÃÂ¾ÃÂ³ÃÂ¾ ÃÂÃÂµÃÂ±ÃÂ")
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
            raise HTTPException(409, "ÃÂÃÂ ÃÂÃÂ¶ÃÂµ ÃÂ´ÃÂÃÂÃÂ·ÃÂÃÂ")
        if existing and existing["status"] == "pending":
            raise HTTPException(409, "ÃÂÃÂ°ÃÂÃÂ²ÃÂºÃÂ° ÃÂÃÂ¶ÃÂµ ÃÂÃÂÃÂÃÂµÃÂÃÂÃÂ²ÃÂÃÂµÃÂ")
        con.execute("""INSERT INTO friendships(requester_id, addressee_id,
            status, created_at, updated_at) VALUES(?,?,'pending',?,?)
            ON CONFLICT(requester_id, addressee_id) DO UPDATE SET
            status='pending', updated_at=excluded.updated_at""",
            (payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "friend_request",
            "ÃÂÃÂ¾ÃÂ²ÃÂ°ÃÂ ÃÂ·ÃÂ°ÃÂÃÂ²ÃÂºÃÂ° ÃÂ² ÃÂ´ÃÂÃÂÃÂ·ÃÂÃÂ", payload.telegram_id,
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
            raise HTTPException(404, "ÃÂÃÂ°ÃÂÃÂ²ÃÂºÃÂ° ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ÃÂ°")
        if payload.decision == "accept":
            create_notification(con, requester_id, "friend_accepted",
                "ÃÂÃÂ°ÃÂÃÂ²ÃÂºÃÂ° ÃÂ² ÃÂ´ÃÂÃÂÃÂ·ÃÂÃÂ ÃÂ¿ÃÂÃÂ¸ÃÂ½ÃÂÃÂÃÂ°", payload.telegram_id,
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
            raise HTTPException(409, "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂ°ÃÂÃÂ ÃÂ¸ÃÂ³ÃÂÃÂ¾ÃÂºÃÂ¾ÃÂ² ÃÂ¼ÃÂ¾ÃÂ¶ÃÂ½ÃÂ¾ ÃÂÃÂ¾ÃÂ»ÃÂÃÂºÃÂ¾ ÃÂ´ÃÂ¾ ÃÂ½ÃÂ°ÃÂÃÂ°ÃÂ»ÃÂ° ÃÂ¼ÃÂ°ÃÂÃÂÃÂ°")
        if payload.target_id in match["players"]:
            raise HTTPException(409, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂÃÂ¶ÃÂµ ÃÂÃÂÃÂ°ÃÂÃÂÃÂ²ÃÂÃÂµÃÂ ÃÂ² ÃÂ¼ÃÂ°ÃÂÃÂÃÂµ")
        require_registered_player(con, payload.target_id)
        con.execute("INSERT INTO match_invites(invite_id,match_id,inviter_id,invitee_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (invite_id, match_id, payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "match_invite", "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ² ÃÂ¼ÃÂ°ÃÂÃÂ",
            f"ÃÂÃÂ³ÃÂÃÂ¾ÃÂº {payload.telegram_id} ÃÂ¿ÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂ°ÃÂµÃÂ ÃÂ²ÃÂ°ÃÂ ÃÂ² ÃÂ¼ÃÂ°ÃÂÃÂ {match_id}",
            {"invite_id": invite_id, "match_id": match_id})
    return {"status": "invited", "invite_id": invite_id,
            "note": "ÃÂÃÂÃÂ¸ÃÂ½ÃÂÃÂÃÂ¸ÃÂµ ÃÂ¿ÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂ ÃÂ¿ÃÂ¾ÃÂºÃÂ° ÃÂ½ÃÂµ ÃÂ¼ÃÂµÃÂ½ÃÂÃÂµÃÂ ÃÂÃÂ¾ÃÂÃÂÃÂ°ÃÂ² ÃÂÃÂ¶ÃÂµ ÃÂÃÂ¾ÃÂ·ÃÂ´ÃÂ°ÃÂ½ÃÂ½ÃÂ¾ÃÂ³ÃÂ¾ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ°; ÃÂ¾ÃÂ½ÃÂ¾ ÃÂÃÂ¸ÃÂºÃÂÃÂ¸ÃÂÃÂÃÂµÃÂ ÃÂ¾ÃÂÃÂºÃÂ»ÃÂ¸ÃÂº ÃÂ¸ÃÂ³ÃÂÃÂ¾ÃÂºÃÂ°."}


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
            raise HTTPException(404, "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ÃÂ¾")
        status = "accepted" if payload.decision == "accept" else "rejected"
        con.execute("UPDATE match_invites SET status=?,updated_at=? WHERE invite_id=?",
                    (status, stamp, invite_id))
        create_notification(con, invite["inviter_id"], "match_invite_response",
            "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ¿ÃÂÃÂ¸ÃÂ½ÃÂÃÂÃÂ¾" if status == "accepted" else "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ¾ÃÂÃÂºÃÂ»ÃÂ¾ÃÂ½ÃÂµÃÂ½ÃÂ¾",
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
            raise HTTPException(404, "ÃÂ£ÃÂ²ÃÂµÃÂ´ÃÂ¾ÃÂ¼ÃÂ»ÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ÃÂ¾")
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
            raise HTTPException(409, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂÃÂ¶ÃÂµ ÃÂÃÂ¾ÃÂÃÂÃÂ¾ÃÂ¸ÃÂ ÃÂ² ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂµ")
        try:
            con.execute("INSERT INTO clans(clan_id,name,tag,description,owner_id,created_at) VALUES(?,?,?,?,?,?)",
                (clan_id, payload.name.strip(), payload.tag.upper(), payload.description.strip(), payload.telegram_id, stamp))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "ÃÂÃÂ°ÃÂ·ÃÂ²ÃÂ°ÃÂ½ÃÂ¸ÃÂµ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂÃÂµÃÂ³ ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂ° ÃÂÃÂ¶ÃÂµ ÃÂ·ÃÂ°ÃÂ½ÃÂÃÂÃÂ")
        con.execute("INSERT INTO clan_members(clan_id,telegram_id,role,joined_at) VALUES(?,?,'owner',?)",
                     (clan_id, payload.telegram_id, stamp))
        create_notification(con, payload.telegram_id, "clan_created", "ÃÂÃÂ»ÃÂ°ÃÂ½ ÃÂÃÂ¾ÃÂ·ÃÂ´ÃÂ°ÃÂ½", payload.name,
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
            raise HTTPException(404, "ÃÂÃÂ»ÃÂ°ÃÂ½ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½")
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
            raise HTTPException(409, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂÃÂ¶ÃÂµ ÃÂÃÂ¾ÃÂÃÂÃÂ¾ÃÂ¸ÃÂ ÃÂ² ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂµ")
        con.execute("INSERT INTO clan_invites(invite_id,clan_id,inviter_id,invitee_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (invite_id, clan_id, payload.telegram_id, payload.target_id, stamp, stamp))
        create_notification(con, payload.target_id, "clan_invite", "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ² ÃÂºÃÂ»ÃÂ°ÃÂ½", "",
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
            raise HTTPException(404, "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ÃÂ¾")
        if payload.decision == "accept":
            require_registered_player(con, payload.telegram_id)
            if con.execute("SELECT 1 FROM clan_members WHERE telegram_id=?", (payload.telegram_id,)).fetchone():
                raise HTTPException(409, "ÃÂÃÂ³ÃÂÃÂ¾ÃÂº ÃÂÃÂ¶ÃÂµ ÃÂÃÂ¾ÃÂÃÂÃÂ¾ÃÂ¸ÃÂ ÃÂ² ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂµ")
            con.execute("INSERT INTO clan_members(clan_id,telegram_id,role,joined_at) VALUES(?,?,'member',?)",
                         (invite["clan_id"], payload.telegram_id, stamp))
        con.execute("UPDATE clan_invites SET status=?,updated_at=? WHERE invite_id=?",
                     ("accepted" if payload.decision == "accept" else "rejected", stamp, invite_id))
        create_notification(con, invite["inviter_id"], "clan_invite_response",
            "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ¿ÃÂÃÂ¸ÃÂ½ÃÂÃÂÃÂ¾" if payload.decision == "accept" else "ÃÂÃÂÃÂ¸ÃÂ³ÃÂ»ÃÂ°ÃÂÃÂµÃÂ½ÃÂ¸ÃÂµ ÃÂ¾ÃÂÃÂºÃÂ»ÃÂ¾ÃÂ½ÃÂµÃÂ½ÃÂ¾",
            payload.telegram_id, {"clan_id": invite["clan_id"]})
    return {"status": payload.decision}


@app.post("/clans/{clan_id}/roles")
def change_clan_role(clan_id: str, payload: ClanRoleChange):
    with db() as con:
        require_clan_role(con, clan_id, payload.telegram_id, ("owner",))
        cur = con.execute("UPDATE clan_members SET role=? WHERE clan_id=? AND telegram_id=? AND role<>'owner'",
                          (payload.role, clan_id, payload.target_id))
        if not cur.rowcount:
            raise HTTPException(404, "ÃÂ£ÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂº ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂÃÂ¾ÃÂ»ÃÂ ÃÂ½ÃÂµÃÂ»ÃÂÃÂ·ÃÂ ÃÂ¸ÃÂ·ÃÂ¼ÃÂµÃÂ½ÃÂ¸ÃÂÃÂ")
    return {"status": "updated", "role": payload.role}


@app.delete("/clans/{clan_id}/members/{target_id}")
def remove_clan_member(clan_id: str, target_id: str, telegram_id: str):
    with db() as con:
        require_clan_role(con, clan_id, telegram_id, ("owner", "officer"))
        target = con.execute("SELECT role FROM clan_members WHERE clan_id=? AND telegram_id=?",
                             (clan_id, target_id)).fetchone()
        if not target:
            raise HTTPException(404, "ÃÂ£ÃÂÃÂ°ÃÂÃÂÃÂ½ÃÂ¸ÃÂº ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½")
        if target["role"] == "owner" or (target["role"] == "officer" and telegram_id != target_id):
            raise HTTPException(403, "ÃÂÃÂµÃÂ»ÃÂÃÂ·ÃÂ ÃÂ¸ÃÂÃÂºÃÂ»ÃÂÃÂÃÂ¸ÃÂÃÂ ÃÂ²ÃÂ»ÃÂ°ÃÂ´ÃÂµÃÂ»ÃÂÃÂÃÂ° ÃÂ¸ÃÂ»ÃÂ¸ ÃÂ´ÃÂÃÂÃÂ³ÃÂ¾ÃÂ³ÃÂ¾ ÃÂ¾ÃÂÃÂ¸ÃÂÃÂµÃÂÃÂ°")
        con.execute("DELETE FROM clan_members WHERE clan_id=? AND telegram_id=?", (clan_id, target_id))
    return {"status": "removed"}


@app.post("/clan-wars")
def create_clan_war(payload: ClanWarCreate):
    stamp, war_id = now(), uuid.uuid4().hex[:12]
    with db() as con:
        own = con.execute("SELECT clan_id FROM clan_members WHERE telegram_id=? AND role IN ('owner','officer')",
                          (payload.telegram_id,)).fetchone()
        if not own:
            raise HTTPException(403, "ÃÂ¡ÃÂ¾ÃÂ·ÃÂ´ÃÂ°ÃÂÃÂ Clan War ÃÂ¼ÃÂ¾ÃÂ¶ÃÂµÃÂ ÃÂ²ÃÂ»ÃÂ°ÃÂ´ÃÂµÃÂ»ÃÂµÃÂ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂ¾ÃÂÃÂ¸ÃÂÃÂµÃÂ ÃÂºÃÂ»ÃÂ°ÃÂ½ÃÂ°")
        if own["clan_id"] == payload.opponent_clan_id:
            raise HTTPException(400, "ÃÂÃÂµÃÂ»ÃÂÃÂ·ÃÂ ÃÂ²ÃÂÃÂ·ÃÂ²ÃÂ°ÃÂÃÂ ÃÂÃÂ¾ÃÂ±ÃÂÃÂÃÂ²ÃÂµÃÂ½ÃÂ½ÃÂÃÂ¹ ÃÂºÃÂ»ÃÂ°ÃÂ½")
        opponent = con.execute("SELECT owner_id,name FROM clans WHERE clan_id=?", (payload.opponent_clan_id,)).fetchone()
        if not opponent:
            raise HTTPException(404, "ÃÂÃÂ»ÃÂ°ÃÂ½ ÃÂÃÂ¾ÃÂ¿ÃÂµÃÂÃÂ½ÃÂ¸ÃÂºÃÂ° ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½")
        con.execute("INSERT INTO clan_wars(war_id,clan_a,clan_b,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (war_id, own["clan_id"], payload.opponent_clan_id, payload.telegram_id, stamp, stamp))
        create_notification(con, opponent["owner_id"], "clan_war_challenge", "ÃÂÃÂ¾ÃÂ²ÃÂÃÂ¹ ÃÂ²ÃÂÃÂ·ÃÂ¾ÃÂ² Clan Wars", "",
                            {"war_id": war_id, "clan_id": own["clan_id"]})
    return {"status": "pending", "war_id": war_id}


@app.post("/clan-wars/{war_id}/decision")
def decide_clan_war(war_id: str, payload: ClanWarDecision):
    stamp = now()
    with db() as con:
        war = con.execute("SELECT * FROM clan_wars WHERE war_id=?", (war_id,)).fetchone()
        if not war or war["status"] != "pending":
            raise HTTPException(404, "ÃÂÃÂÃÂ·ÃÂ¾ÃÂ² ÃÂ½ÃÂµ ÃÂ½ÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ ÃÂ¸ÃÂ»ÃÂ¸ ÃÂÃÂ¶ÃÂµ ÃÂ¾ÃÂ±ÃÂÃÂ°ÃÂ±ÃÂ¾ÃÂÃÂ°ÃÂ½")
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
            raise HTTPException(409, "Clan War ÃÂ½ÃÂµ ÃÂ³ÃÂ¾ÃÂÃÂ¾ÃÂ² ÃÂº ÃÂ¾ÃÂÃÂ¿ÃÂÃÂ°ÃÂ²ÃÂºÃÂµ ÃÂÃÂµÃÂ·ÃÂÃÂ»ÃÂÃÂÃÂ°ÃÂÃÂ°")
        require_clan_role(con, war["clan_a"], payload.telegram_id, ("owner", "officer"))
        winner = None if payload.score_a == payload.score_b else war["clan_a"] if payload.score_a > payload.score_b else war["clan_b"]
        con.execute("UPDATE clan_wars SET status='under_review',score_a=?,score_b=?,winner_clan_id=?,updated_at=? WHERE war_id=?",
                    (payload.score_a, payload.score_b, winner, stamp, war_id))
        create_notification(con, payload.telegram_id, "clan_war_result_submitted", "ÃÂ ÃÂµÃÂ·ÃÂÃÂ»ÃÂÃÂÃÂ°ÃÂ Clan War ÃÂ¾ÃÂÃÂ¿ÃÂÃÂ°ÃÂ²ÃÂ»ÃÂµÃÂ½ ÃÂ½ÃÂ° ÃÂ¿ÃÂÃÂ¾ÃÂ²ÃÂµÃÂÃÂºÃÂ", "",
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
            raise HTTPException(404, "Clan War ÃÂ½ÃÂµ ÃÂ¾ÃÂ¶ÃÂ¸ÃÂ´ÃÂ°ÃÂµÃÂ ÃÂ¿ÃÂÃÂ¾ÃÂ²ÃÂµÃÂÃÂºÃÂ¸")
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
                "ÃÂ ÃÂµÃÂ·ÃÂÃÂ»ÃÂÃÂÃÂ°ÃÂ Clan War ÃÂ¿ÃÂ¾ÃÂ´ÃÂÃÂ²ÃÂµÃÂÃÂ¶ÃÂ´ÃÂÃÂ½" if decision == "approve" else "ÃÂ ÃÂµÃÂ·ÃÂÃÂ»ÃÂÃÂÃÂ°ÃÂ Clan War ÃÂ¾ÃÂÃÂºÃÂ»ÃÂ¾ÃÂ½ÃÂÃÂ½",
                "", {"war_id": war_id, "status": "confirmed" if decision == "approve" else "rejected"})
    return {"status": "confirmed" if decision == "approve" else "rejected", "war_id": war_id}


@app.post("/matches/{match_id}/appeals")
def create_match_appeal(match_id: str, payload: AppealCreate):
    stamp, appeal_id = now(), uuid.uuid4().hex
    with db() as con:
        match = load_match(con, match_id)
        require_match_player(match, payload.telegram_id)
        if match["status"] not in {"confirmed", "rejected", "disputed"}:
            raise HTTPException(409, "ÃÂÃÂ»ÃÂ ÃÂÃÂÃÂ¾ÃÂ³ÃÂ¾ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ° ÃÂ°ÃÂ¿ÃÂµÃÂ»ÃÂ»ÃÂÃÂÃÂ¸ÃÂ ÃÂÃÂµÃÂ¹ÃÂÃÂ°ÃÂ ÃÂ½ÃÂµÃÂ´ÃÂ¾ÃÂÃÂÃÂÃÂ¿ÃÂ½ÃÂ°")
        existing = con.execute("SELECT 1 FROM appeals WHERE match_id=? AND telegram_id=? AND status IN ('open','need_info')",
                               (match_id, payload.telegram_id)).fetchone()
        if existing:
            raise HTTPException(409, "ÃÂ£ ÃÂ²ÃÂ°ÃÂ ÃÂÃÂ¶ÃÂµ ÃÂµÃÂÃÂÃÂ ÃÂ¾ÃÂÃÂºÃÂÃÂÃÂÃÂ°ÃÂ ÃÂ°ÃÂ¿ÃÂµÃÂ»ÃÂ»ÃÂÃÂÃÂ¸ÃÂ ÃÂ¿ÃÂ¾ ÃÂÃÂÃÂ¾ÃÂ¼ÃÂ ÃÂ¼ÃÂ°ÃÂÃÂÃÂ")
        con.execute("INSERT INTO appeals(appeal_id,match_id,telegram_id,reason,details,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (appeal_id, match_id, payload.telegram_id, payload.reason, payload.details, stamp, stamp))
        create_notification(con, payload.telegram_id, "appeal_created", "ÃÂÃÂ¿ÃÂµÃÂ»ÃÂ»ÃÂÃÂÃÂ¸ÃÂ ÃÂÃÂ¾ÃÂ·ÃÂ´ÃÂ°ÃÂ½ÃÂ°", "",
                            {"appeal_id": appeal_id, "match_id": match_id})
    return {"status": "open", "appeal_id": appeal_id}


@app.get("/appeals/{telegram_id}")
def list_appeals(telegram_id: str, limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as con:
        rows = con.execute("SELECT * FROM appeals WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?",
                           (telegram_id, limit)).fetchall()

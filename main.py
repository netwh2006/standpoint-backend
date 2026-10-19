
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


# ÐÐ¾Ð»Ð½ÑÐ¹ Ð¸Ð½ÑÐµÑÑÐµÐ¹Ñ STANDPOINT Ð²ÑÑÑÐ¾ÐµÐ½ Ð² main.py, Ð¿Ð¾ÑÑÐ¾Ð¼Ñ Ð¾ÑÐ´ÐµÐ»ÑÐ½ÑÐ¹ HTML-ÑÐ°Ð¹Ð» Ð½Ðµ Ð½ÑÐ¶ÐµÐ½.
# API_BASE Ð² HTML Ð¿ÑÑÑÐ¾Ð¹: Ð·Ð°Ð¿ÑÐ¾ÑÑ Ð¸Ð´ÑÑ Ð½Ð° ÑÑÐ¾Ñ Ð¶Ðµ Ð´Ð¾Ð¼ÐµÐ½, Ð³Ð´Ðµ Ð·Ð°Ð¿ÑÑÐµÐ½ backend.
_FRONTEND_HTML_B64 = """PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzA4MGEwZCI+Cjx0aXRsZT5TVEFORFBPSU5UIOKAlCBTdGFuZG9mZiAyIENvbXBldGl0aXZlPC90aXRsZT4KPHN0eWxlPgo6cm9vdHtjb2xvci1zY2hlbWU6ZGFyazstLWJnOiMwODBhMGQ7LS1zdXJmYWNlOiMxMTE2MWM7LS1zdXJmYWNlMjojMTcxZTI2Oy0tc3VyZmFjZTM6IzIwMmEzNTstLXN0cm9rZTojM2E0NjUzOy0tdGV4dDojZmZmZmZmOy0tbXV0ZWQ6I2MwY2FkNDstLW9yYW5nZTojZmY1YTEyOy0tb3JhbmdlMjojZmY4YTIyOy0tZ3JlZW46IzUwZGRhMDstLXJlZDojZmY1OTY0Oy0tZ29sZDojZmZjYTY4Oy0tcjoxN3B4fQoqe2JveC1zaXppbmc6Ym9yZGVyLWJveH1odG1se2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yLXNjaGVtZTpkYXJrfWJvZHl7bWFyZ2luOjA7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAscmdiYSgyNTUsODAsMTUsLjEyKSx0cmFuc3BhcmVudCAyNyUpLHZhcigtLWJnKTtjb2xvcjp2YXIoLS10ZXh0KTtmb250OjE0cHgvMS40NSBJbnRlcix1aS1zYW5zLXNlcmlmLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLEJsaW5rTWFjU3lzdGVtRm9udCwiU2Vnb2UgVUkiLHNhbnMtc2VyaWY7bWluLWhlaWdodDoxMDB2aH1idXR0b24saW5wdXQsc2VsZWN0LHRleHRhcmVhe2ZvbnQ6aW5oZXJpdH1idXR0b257Y3Vyc29yOnBvaW50ZXI7Y29sb3I6aW5oZXJpdH1idXR0b246Zm9jdXMtdmlzaWJsZSxpbnB1dDpmb2N1cy12aXNpYmxle291dGxpbmU6MnB4IHNvbGlkIHZhcigtLW9yYW5nZTIpO291dGxpbmUtb2Zmc2V0OjJweH1he2NvbG9yOmluaGVyaXQ7dGV4dC1kZWNvcmF0aW9uOm5vbmV9LnNoZWxse21pbi1oZWlnaHQ6MTAwdmg7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMzhweCBtaW5tYXgoMCwxZnIpfS5zaWRlYmFye2hlaWdodDoxMDB2aDtwb3NpdGlvbjpzdGlja3k7dG9wOjA7YmFja2dyb3VuZDojMGIwZTEyO2JvcmRlci1yaWdodDoxcHggc29saWQgIzIwMjczMDtwYWRkaW5nOjIzcHggMTRweCAxNXB4O2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjI4cHg7ei1pbmRleDoxMH0uYnJhbmR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtwYWRkaW5nOjAgOHB4fS5icmFuZG1hcmt7d2lkdGg6MzdweDtoZWlnaHQ6MzdweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY0YzBhLCNmZjliMjgpO2NvbG9yOiMxNDBkMDk7Zm9udC1zaXplOjIzcHg7Zm9udC13ZWlnaHQ6MTAwMDtjbGlwLXBhdGg6cG9seWdvbigwIDAsMTAwJSAwLDEwMCUgNzAlLDY0JSAxMDAlLDAgMTAwJSk7Ym94LXNoYWRvdzowIDAgMjJweCAjZmY2NDFjMzN9LmJyYW5kbmFtZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo5NTA7bGV0dGVyLXNwYWNpbmc6LjVweH0uYnJhbmRuYW1lIHNwYW57Y29sb3I6dmFyKC0tb3JhbmdlKX0uYnJhbmR0YWd7Zm9udC1zaXplOjhweDtjb2xvcjojYmFjNGNlO2xldHRlci1zcGFjaW5nOjEuOHB4O2ZvbnQtd2VpZ2h0OjgwMH0ubmF2Z3JvdXAgbGFiZWx7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMTJweDttYXJnaW4tYm90dG9tOjhweDtmb250LXNpemU6OXB4O2xldHRlci1zcGFjaW5nOjEuN3B4O2NvbG9yOiM5YWE3YjQ7Zm9udC13ZWlnaHQ6OTAwfS5uYXZ7ZGlzcGxheTpncmlkO2dhcDo0cHh9Lm5hdiBidXR0b257d2lkdGg6MTAwJTtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O2JhY2tncm91bmQ6bm9uZTt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTFweCAxMnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo2NTA7Zm9udC1zaXplOjEzcHh9Lm5hdiBidXR0b24gLm5pe3dpZHRoOjE5cHg7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjE3cHh9Lm5hdiBidXR0b246aG92ZXJ7YmFja2dyb3VuZDojMTkxZjI3O2NvbG9yOndoaXRlfS5uYXYgYnV0dG9uLmFjdGl2ZXtjb2xvcjojZmY5YTRkO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCMzYTFiMTAsIzIwMTUwZik7Ym9yZGVyLWNvbG9yOiM0OTI4MTk7Ym94LXNoYWRvdzppbnNldCAzcHggMCB2YXIoLS1vcmFuZ2UpfS5uYXYgLm5hdmNvdW50e21hcmdpbi1sZWZ0OmF1dG87YmFja2dyb3VuZDojNDgyNDE2O2NvbG9yOiNmZjliNWM7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzoycHggNnB4O2ZvbnQtc2l6ZToxMHB4fS5zaWRlYmFyLWZvb3R7bWFyZ2luLXRvcDphdXRvfS51c2VyY2FyZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6MTFweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjMTcxZDI0LCMxMDE0MTkpfS5hdmF0YXJ7d2lkdGg6MzlweDtoZWlnaHQ6MzlweDtmbGV4OjAgMCBhdXRvO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0MGRlZywjZmY4YzI3LCM1YjI0MTIpO2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjp3aGl0ZX0udXNlcmNhcmQgYntkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMnB4O292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzO21heC13aWR0aDoxMjBweH0udXNlcmNhcmQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5tYWlue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjE1MzBweDttaW4td2lkdGg6MDttYXJnaW46MCBhdXRvO3BhZGRpbmc6MjhweCBjbGFtcCgxN3B4LDMuMnZ3LDQ2cHgpIDU1cHh9LnRvcGJhcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxNnB4O3BhZGRpbmctYm90dG9tOjIxcHg7bWFyZ2luLWJvdHRvbToyM3B4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyNDJiMzN9LmtpY2tlcntmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoycHg7Zm9udC13ZWlnaHQ6OTAwO2NvbG9yOnZhcigtLW9yYW5nZTIpfS50b3BiYXIgaDF7Zm9udC1zaXplOmNsYW1wKDI0cHgsM3Z3LDM1cHgpO2xldHRlci1zcGFjaW5nOi0xLjFweDtsaW5lLWhlaWdodDoxLjE7bWFyZ2luOjdweCAwIDA7Zm9udC13ZWlnaHQ6OTUwfS50b3BhY3Rpb25ze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0uaWNvbmJ0biwuYnRuLXNlY29uZGFyeXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDQ7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6MTFweDtjb2xvcjojZjRmNmY4O3BhZGRpbmc6MTFweCAxNHB4fS5pY29uYnRue3dpZHRoOjQzcHg7aGVpZ2h0OjQzcHg7cGFkZGluZzowO3Bvc2l0aW9uOnJlbGF0aXZlO2ZvbnQtc2l6ZToxOHB4fS5waW5ne3Bvc2l0aW9uOmFic29sdXRlO3dpZHRoOjdweDtoZWlnaHQ6N3B4O2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtib3JkZXItcmFkaXVzOjUwJTtyaWdodDo4cHg7dG9wOjhweH0uYnRuLXByaW1hcnl7Ym9yZGVyOjA7Ym9yZGVyLXJhZGl1czoxMXB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjZmY1MDBiLCNmZjkwMjQpO2NvbG9yOiMxNjBlMDk7Zm9udC13ZWlnaHQ6OTAwO3BhZGRpbmc6MTNweCAxOHB4O2JveC1zaGFkb3c6MCA3cHggMjJweCAjZmY2NDFjMjY7dHJhbnNpdGlvbjpmaWx0ZXIgLjE1cyx0cmFuc2Zvcm0gLjE1c30uYnRuLXByaW1hcnk6aG92ZXJ7ZmlsdGVyOmJyaWdodG5lc3MoMS4wOCk7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoLTFweCl9LmJ0bi1zZWNvbmRhcnk6aG92ZXIsLmljb25idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiM4YzRhMjR9LmxheW91dHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDEuNTVmcikgbWlubWF4KDI3NXB4LDFmcik7Z2FwOjE3cHh9LnN0YWNre2Rpc3BsYXk6Z3JpZDthbGlnbi1jb250ZW50OnN0YXJ0O2dhcDoxNnB4fS5oZXJve3Bvc2l0aW9uOnJlbGF0aXZlO2lzb2xhdGlvbjppc29sYXRlO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0OjI3MHB4O2JvcmRlci1yYWRpdXM6MjBweDtwYWRkaW5nOmNsYW1wKDI0cHgsMy41dncsNDBweCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtib3JkZXI6MXB4IHNvbGlkICM2MzMzMWM7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTA1ZGVnLHJnYmEoNDcsMjMsMTMsLjk4KSxyZ2JhKDIyLDE5LDIyLC45OCkgNTglLHJnYmEoMTIsMTcsMjIsLjk4KSl9Lmhlcm86YmVmb3Jle2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7ei1pbmRleDotMTt3aWR0aDoyODBweDtoZWlnaHQ6MjgwcHg7cmlnaHQ6NnB4O3RvcDotNTNweDtib3JkZXI6MXB4IHNvbGlkICNmZjY0MWMyODt0cmFuc2Zvcm06cm90YXRlKDM4ZGVnKTtib3gtc2hhZG93OjAgMCAwIDIycHggI2ZmNjQxYzA3LDAgMCAwIDQ3cHggI2ZmNjQxYzA1fS5oZXJvOmFmdGVye2NvbnRlbnQ6IlMiO3Bvc2l0aW9uOmFic29sdXRlO3otaW5kZXg6LTE7cmlnaHQ6MjBweDt0b3A6LTU2cHg7Zm9udC1zaXplOjMwMHB4O2xpbmUtaGVpZ2h0OjE7Zm9udC13ZWlnaHQ6MTAwMDtjb2xvcjojZmY2NDFjMTg7dHJhbnNmb3JtOnNrZXcoLTEwZGVnKX0uaGVyby1jb3B5e21heC13aWR0aDo1MTVweH0uZXllYnJvd3tjb2xvcjojZmY5ODRjO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoyLjRweH0uaGVybyBoMntmb250LXNpemU6Y2xhbXAoMzJweCw0LjF2dyw1MHB4KTtsZXR0ZXItc3BhY2luZzotMS44cHg7bGluZS1oZWlnaHQ6MS4wMTttYXJnaW46MTRweCAwIDE0cHg7Zm9udC13ZWlnaHQ6MTAwMH0uaGVybyBoMiBzcGFue2NvbG9yOnZhcigtLW9yYW5nZSl9Lmhlcm8gcHtjb2xvcjojYzFjN2NlO2ZvbnQtc2l6ZToxNHB4O2xpbmUtaGVpZ2h0OjEuNjU7bWF4LXdpZHRoOjQ0MHB4O21hcmdpbjowIDAgMjFweH0uaGVyby1hY3Rpb25ze2Rpc3BsYXk6ZmxleDtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0uc2VjdGlvbntkaXNwbGF5Om5vbmU7YW5pbWF0aW9uOmFwcGVhciAuMThzIGVhc2V9LnNlY3Rpb24uYWN0aXZle2Rpc3BsYXk6YmxvY2t9QGtleWZyYW1lcyBhcHBlYXJ7ZnJvbXtvcGFjaXR5Oi40O3RyYW5zZm9ybTp0cmFuc2xhdGVZKDNweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTpub25lfX0uc3RhdHN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMyxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0uc3RhdHtwYWRkaW5nOjE2cHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1zdHJva2UpO2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE1MWIyMiwjMTAxNDE5KTttaW4td2lkdGg6MH0uc3RhdCBsYWJlbHtmb250LXNpemU6MTBweDtjb2xvcjojZDBkN2RmO2xldHRlci1zcGFjaW5nOi43cHg7Zm9udC13ZWlnaHQ6NzUwO2Rpc3BsYXk6YmxvY2t9LnN0YXQgc3Ryb25ne2ZvbnQtc2l6ZTpjbGFtcCgyMXB4LDJ2dywyOHB4KTtkaXNwbGF5OmJsb2NrO21hcmdpbjo1cHggMCAycHg7bGV0dGVyLXNwYWNpbmc6LS43cHg7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo5NTB9LnN0YXQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5jYXJke3BhZGRpbmc6MThweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czp2YXIoLS1yKTtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMCwjMTAxNDE5KTttaW4td2lkdGg6MH0uY2FyZGhlYWR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtnYXA6MTBweDttYXJnaW4tYm90dG9tOjE1cHh9LmNhcmRoZWFkIGgze2ZvbnQtc2l6ZToxNHB4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojg1MDtjb2xvcjojZjVmN2Y5fS50ZXh0bGlua3tib3JkZXI6MDtiYWNrZ3JvdW5kOm5vbmU7Y29sb3I6I2ZmOWE0ZDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3NTA7cGFkZGluZzo1cHh9LnRleHRsaW5rOmhvdmVye2NvbG9yOiNmZmMwOGF9Lm1hdGNocm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4O2JvcmRlcjoxcHggc29saWQgIzI5MzIzYjtib3JkZXItcmFkaXVzOjEzcHg7YmFja2dyb3VuZDojMDkwZDEyfS5tYXRjaGxvZ297d2lkdGg6NDZweDtoZWlnaHQ6NDZweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JvcmRlci1yYWRpdXM6MTJweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzQ0MjAwZSwjMjExNTBmKTtjb2xvcjojZmY4YTIyO2ZvbnQtc2l6ZToyMnB4O2ZsZXgtc2hyaW5rOjB9Lm1hdGNoY29weXtmbGV4OjE7bWluLXdpZHRoOjB9Lm1hdGNoY29weSBie2ZvbnQtc2l6ZToxMnB4O2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2Y1ZjdmOX0ubWF0Y2hjb3B5IHNtYWxse2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTBweDttYXJnaW4tdG9wOjRweH0ucGlsbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0Ojg1MDtsZXR0ZXItc3BhY2luZzouM3B4O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtiYWNrZ3JvdW5kOiMxNTMzMjY7Ym9yZGVyOjFweCBzb2xpZCAjMjQ0YTM4O2NvbG9yOiM2MGU1YTU7d2hpdGUtc3BhY2U6bm93cmFwfS5wYXNzY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTVkZWcsIzJjMTkwZiwjMTkxNTFhIDU4JSwjMTExNzFkKTtib3JkZXItY29sb3I6IzYwMzUxZjtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59LnBhc3NjYXJkOmFmdGVye2NvbnRlbnQ6IuKcpiI7cG9zaXRpb246YWJzb2x1dGU7cmlnaHQ6MTNweDt0b3A6LTE1cHg7Zm9udC1zaXplOjEwMHB4O2NvbG9yOiNmZjhhMjIxMDtwb2ludGVyLWV2ZW50czpub25lfS5wYXNzLXRvcHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjEycHg7YWxpZ24taXRlbXM6Y2VudGVyfS5wYXNzLWxldmVse2ZvbnQtc2l6ZTozN3B4O2ZvbnQtd2VpZ2h0OjEwMDA7Y29sb3I6dmFyKC0tb3JhbmdlMik7bGluZS1oZWlnaHQ6MX0ucGFzc2NhcmQgaDN7bWFyZ2luOjdweCAwIDZweDtmb250LXNpemU6MTdweH0ubXV0ZWR7Y29sb3I6dmFyKC0tbXV0ZWQpfS5zbWFsbHtmb250LXNpemU6MTFweH0ucGFzc2NhcmQgcHttYXJnaW46MDtjb2xvcjojYzhkMGQ4O2ZvbnQtc2l6ZToxMXB4fS5wcm9ncmVzc3toZWlnaHQ6OHB4O2JhY2tncm91bmQ6IzMwMzYzZTtib3JkZXItcmFkaXVzOjIwcHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbjoxN3B4IDAgOHB4fS5wcm9ncmVzcyBzcGFue2hlaWdodDoxMDAlO3dpZHRoOjQwJTtkaXNwbGF5OmJsb2NrO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCNmZjRlMGIsI2ZmOWEyOCk7Ym94LXNoYWRvdzowIDAgMTRweCAjZmY2NDFjNTU7Ym9yZGVyLXJhZGl1czoyMHB4fS5wYXNzLWZvb3R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtjb2xvcjojYzRjZGQ2O2ZvbnQtc2l6ZToxMXB4fS5sZWFkZXJsaXN0e2Rpc3BsYXk6Z3JpZH0ubGVhZGVye2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MjNweCBtaW5tYXgoMCwxZnIpIGF1dG87YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzoxMHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgIzI1MmQzNX0ubGVhZGVyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTowfS5yYW5re2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjojYzBjYWQ0fS5sZWFkZXI6bnRoLWNoaWxkKDEpIC5yYW5re2NvbG9yOnZhcigtLWdvbGQpfS5sZWFkZXI6bnRoLWNoaWxkKDIpIC5yYW5re2NvbG9yOiNkMWQ4ZGZ9LmxlYWRlcjpudGgtY2hpbGQoMykgLnJhbmt7Y29sb3I6I2Q5OTg2OX0ucGxheWVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweDttaW4td2lkdGg6MH0ubWluaWF2YXt3aWR0aDozM3B4O2hlaWdodDozM3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Ym9yZGVyLXJhZGl1czoxMHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMzAzYjQ3LCMxYTIwMjcpO2JvcmRlcjoxcHggc29saWQgIzNhNDY1Mjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4NTA7ZmxleC1zaHJpbms6MH0ucGxheWVyIGJ7ZGlzcGxheTpibG9jaztmb250LXNpemU6MTFweDt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9LnBsYXllciBzbWFsbHtkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNjMGNhZDQ7bWFyZ2luLXRvcDoycHh9LmVsb3tmb250LXdlaWdodDo5NTA7Zm9udC1zaXplOjEycHh9LnNlY3Rpb25oZWFke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7bWFyZ2luOjVweCAwIDE5cHh9LnNlY3Rpb25oZWFkIGgye2ZvbnQtc2l6ZToyNnB4O2xldHRlci1zcGFjaW5nOi0uN3B4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojk1MH0uc2VjdGlvbmhlYWQgcHtmb250LXNpemU6MTJweDtjb2xvcjojYzBjYWQ0O21hcmdpbjo1cHggMCAwfS5iYWRnZS1vcmFuZ2V7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGQ7Ym9yZGVyOjFweCBzb2xpZCAjNjkzNzFkO2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjkwMH0ubWFwZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg0LG1pbm1heCgwLDFmcikpO2dhcDoxMHB4fS5tYXB0aWxle3BhZGRpbmc6MDt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzE0MWIyMztib3JkZXItcmFkaXVzOjEzcHg7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfS5tYXBhcnR7aGVpZ2h0OjEwNXB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVuO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjNTQ0MDJkLCMyYTMwMzQgNTAlLCMxMTE2MWMpfS5tYXBhcnQ6YmVmb3JlLC5tYXBhcnQ6YWZ0ZXJ7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoxNDBweDtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6I2ZmZmZmZjEyO3RyYW5zZm9ybTpyb3RhdGUoLTM1ZGVnKX0ubWFwYXJ0OmFmdGVye3RyYW5zZm9ybTpyb3RhdGUoNDVkZWcpfS5tYXBhcnQgc3Bhbntmb250LXNpemU6MzNweDt6LWluZGV4OjE7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDVweCAjMDAwKX0ubWFwdGlsZSBie2Rpc3BsYXk6YmxvY2s7cGFkZGluZzoxMHB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNmMWY0ZjZ9Lm1hcHRpbGUuc2VsZWN0ZWR7Ym9yZGVyLWNvbG9yOnZhcigtLW9yYW5nZSk7Ym94LXNoYWRvdzowIDAgMCAxcHggdmFyKC0tb3JhbmdlKSwwIDhweCAyNHB4ICNmZjY0MWMxNX0ubWFwdGlsZS5zZWxlY3RlZCBie2NvbG9yOiNmZjliNGR9Lm1hcHRpbGUuc2VsZWN0ZWQ6YWZ0ZXJ7Y29udGVudDoi4pyTIjtwb3NpdGlvbjphYnNvbHV0ZTtyaWdodDo3cHg7dG9wOjdweDt3aWR0aDoyMnB4O2hlaWdodDoyMnB4O2JvcmRlci1yYWRpdXM6NTAlO2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7YmFja2dyb3VuZDp2YXIoLS1vcmFuZ2UpO2NvbG9yOiMxMzBkMDg7Zm9udC13ZWlnaHQ6MTAwMH0ubW9kZWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDo4cHg7bWFyZ2luOjIwcHggMH0ubW9kZXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTIxNzFkO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHggNXB4O2ZvbnQtd2VpZ2h0OjkwMDtjb2xvcjojZDNkOWRmfS5tb2RlLmFjdGl2ZXtib3JkZXItY29sb3I6Izk4NDkxZjtiYWNrZ3JvdW5kOiMzYjIwMTQ7Y29sb3I6I2ZmOWE0ZH0ucXVldWVib3h7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjNTkzMDFiO2JvcmRlci1yYWRpdXM6MTlweDtwYWRkaW5nOjMwcHggMjJweDtiYWNrZ3JvdW5kOnJhZGlhbC1ncmFkaWVudChlbGxpcHNlIGF0IDUwJSAwLCMzYTFjMTAsdHJhbnNwYXJlbnQgNTglKSxsaW5lYXItZ3JhZGllbnQoMTQ1ZGVnLCMxNTFhMjAsIzBlMTIxNil9LnF1ZXVlaWNvbnt3aWR0aDoxMDBweDtoZWlnaHQ6MTAwcHg7bWFyZ2luOjAgYXV0byAxOHB4O2JvcmRlcjoxcHggc29saWQgIzc1M2ExYztib3JkZXItcmFkaXVzOjUwJTtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2NvbG9yOiNmZjhjMjU7Zm9udC1zaXplOjM2cHg7Ym94LXNoYWRvdzowIDAgMCA5cHggI2ZmNjQxYzA4LDAgMCA0NXB4ICNmZjY0MWMxNX0ucXVldWVib3ggaDN7Zm9udC1zaXplOjIzcHg7bWFyZ2luOjhweCAwfS5xdWV1ZWJveCBwe2NvbG9yOiNjNGNkZDY7Zm9udC1zaXplOjEycHh9LnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo3cHg7ZmxleC13cmFwOndyYXA7bWFyZ2luLWJvdHRvbToxNXB4fS50YWJzIGJ1dHRvbntib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6OXB4O2NvbG9yOiNhZGI3YzE7cGFkZGluZzo5cHggMTNweDtmb250LXNpemU6MTFweH0udGFicyBidXR0b24uYWN0aXZle2JhY2tncm91bmQ6IzNhMjAxNDtib3JkZXItY29sb3I6IzgwNDAxZjtjb2xvcjojZmY5YTRkfS50YWJsZXdyYXB7b3ZlcmZsb3c6YXV0bztib3JkZXI6MXB4IHNvbGlkICMyYzM1NDA7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0udGFibGV7Ym9yZGVyLWNvbGxhcHNlOmNvbGxhcHNlO3dpZHRoOjEwMCU7bWluLXdpZHRoOjUyMHB4fS50YWJsZSB0aCwudGFibGUgdGR7dGV4dC1hbGlnbjpsZWZ0O3BhZGRpbmc6MTRweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjUyZDM2O2ZvbnQtc2l6ZToxMXB4fS50YWJsZSB0aHtiYWNrZ3JvdW5kOiMyMDI4MzM7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo4MDB9LnRhYmxlIHRke2NvbG9yOiNlOGVkZjJ9LnRhYmxlIHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTowfS50YWJsZSB0ci55b3V7YmFja2dyb3VuZDojMmIxYTExfS5wcm9maWxlaGVyb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MjBweDtib3JkZXI6MXB4IHNvbGlkICM2MDM1MWY7Ym9yZGVyLXJhZGl1czoxN3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjMmIxOTBmLCMxNTE5MWUpO21hcmdpbi1ib3R0b206MTVweH0ucHJvZmlsZWF2YXRhcnt3aWR0aDo3MnB4O2hlaWdodDo3MnB4O2JvcmRlci1yYWRpdXM6MTlweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY4ZDI2LCM1YjIxMTEpO2ZvbnQtc2l6ZToyOHB4O2JveC1zaGFkb3c6MCAwIDAgNHB4ICNmZjY0MWMxOH0ucHJvZmlsZWhlcm8gaDN7bWFyZ2luOjAgMCA1cHg7Zm9udC1zaXplOjIwcHh9LnByb2ZpbGVoZXJvIHB7bWFyZ2luOjAgMCA4cHg7Zm9udC1zaXplOjExcHg7Y29sb3I6I2FlYjhjMn0ucHJvZmlsZWVsb3ttYXJnaW4tbGVmdDphdXRvO3RleHQtYWxpZ246cmlnaHR9LnByb2ZpbGVlbG8gc21hbGx7Zm9udC1zaXplOjlweDtjb2xvcjojYWViOGMyO2xldHRlci1zcGFjaW5nOjFweH0ucHJvZmlsZWVsbyBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MzBweDtjb2xvcjojZmY5YTRkfS5mb3JtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9LmZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6N3B4O21hcmdpbjoxMHB4IDB9LmZpZWxkIGxhYmVse2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiMWJiYzV9LmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe3dpZHRoOjEwMCU7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzA4MGIxMDtjb2xvcjojZjRmNmY4O2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHg7b3V0bGluZTpub25lfS5maWVsZCBpbnB1dDpmb2N1c3tib3JkZXItY29sb3I6dmFyKC0tb3JhbmdlKX0uY2xhbmhlcm97ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTRweDtwYWRkaW5nOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCAjMmUzYjQ5O2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTBkZWcsIzE4MjQzMSwjMTExNjFjKX0uY2xhbmxvZ297d2lkdGg6NjRweDtoZWlnaHQ6NjRweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6IzFkMzA0Mjtib3JkZXItcmFkaXVzOjE3cHg7Zm9udC1zaXplOjMwcHh9LmNsYW5oZXJvIGgze21hcmdpbjo0cHggMDtmb250LXNpemU6MTlweH0uY2xhbmhlcm8gcHttYXJnaW46MDtjb2xvcjojYWFiNWMwO2ZvbnQtc2l6ZToxMXB4fS5jbGFuaGVybyAuZ3Jvd3tmbGV4OjF9Lm5vdGljZXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1zdGFydDtnYXA6MTFweDtwYWRkaW5nOjE0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjgzMDNhfS5ub3RpY2U6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOjB9Lm5vdGljZWljb3t3aWR0aDozN3B4O2hlaWdodDozN3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXItcmFkaXVzOjExcHg7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGR9Lm5vdGljZSBie2ZvbnQtc2l6ZToxMnB4fS5ub3RpY2UgcHttYXJnaW46NHB4IDA7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTFweH0uYm90dG9tbmF2e2Rpc3BsYXk6bm9uZX0udG9hc3R7cG9zaXRpb246Zml4ZWQ7ei1pbmRleDoxMDA7cmlnaHQ6MThweDtib3R0b206MThweDttYXgtd2lkdGg6Y2FsYygxMDB2dyAtIDM2cHgpO2JhY2tncm91bmQ6IzE3MWYyNjtib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTE7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHZhcigtLW9yYW5nZSk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTNweCAxNnB4O2JveC1zaGFkb3c6MCAxMnB4IDQwcHggIzAwMDk7Y29sb3I6I2Y1ZjdmODtmb250LXNpemU6MTJweDthbmltYXRpb246YXBwZWFyIC4yc30uZm9vdG5vdGV7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNDttYXJnaW4tdG9wOjEycHg7bGluZS1oZWlnaHQ6MS41fQpAbWVkaWEobWF4LXdpZHRoOjEwNTBweCl7LnNoZWxse2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMDVweCBtaW5tYXgoMCwxZnIpfS5sYXlvdXR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uc2lkZWhpZGV7ZGlzcGxheTpub25lfX0KQG1lZGlhKG1heC13aWR0aDo3MDBweCl7LnNoZWxse2Rpc3BsYXk6YmxvY2t9LnNpZGViYXJ7ZGlzcGxheTpub25lfS5tYWlue3BhZGRpbmc6MjBweCAxNXB4IGNhbGMoOTRweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSl9LnRvcGJhcntwYWRkaW5nLWJvdHRvbToxNnB4O21hcmdpbi1ib3R0b206MThweH0udG9wYmFyIGgxe2ZvbnQtc2l6ZToyNXB4fS50b3BhY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2Rpc3BsYXk6bm9uZX0ubGF5b3V0e2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnI7Z2FwOjEzcHh9LnN0YWNre2dhcDoxM3B4fS5oZXJve21pbi1oZWlnaHQ6MzAwcHg7cGFkZGluZzoyNHB4IDIxcHg7Ym9yZGVyLXJhZGl1czoyMHB4fS5oZXJvOmJlZm9yZXtyaWdodDotOTBweDt0b3A6LThweDt3aWR0aDoyMzBweDtoZWlnaHQ6MjMwcHh9Lmhlcm86YWZ0ZXJ7cmlnaHQ6LTlweDt0b3A6MTJweDtmb250LXNpemU6MjIwcHh9Lmhlcm8gaDJ7Zm9udC1zaXplOjM3cHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvIHB7Zm9udC1zaXplOjEzcHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvLWFjdGlvbnN7Z2FwOjhweH0uaGVyby1hY3Rpb25zIGJ1dHRvbntwYWRkaW5nOjEycHggMTNweDtmb250LXNpemU6MTJweH0uc3RhdHN7Z2FwOjhweH0uc3RhdHtwYWRkaW5nOjEzcHggMTBweDtib3JkZXItcmFkaXVzOjE1cHh9LnN0YXQgbGFiZWx7Zm9udC1zaXplOjlweDtsZXR0ZXItc3BhY2luZzouM3B4fS5zdGF0IHN0cm9uZ3tmb250LXNpemU6MjFweH0uc3RhdCBzbWFsbHtmb250LXNpemU6OXB4O2xpbmUtaGVpZ2h0OjEuMztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6NHB4fS5jYXJke3BhZGRpbmc6MTVweDtib3JkZXItcmFkaXVzOjE2cHh9LnBhc3NjYXJke3BhZGRpbmc6MThweH0ubWFwZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDIsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9Lm1hcGFydHtoZWlnaHQ6OTNweH0uc2VjdGlvbmhlYWQgaDJ7Zm9udC1zaXplOjI0cHh9LnNlY3Rpb25oZWFkIHB7bWF4LXdpZHRoOjI1MHB4O2xpbmUtaGVpZ2h0OjEuNX0ucXVldWVib3h7cGFkZGluZzoyNXB4IDE1cHh9Lm1vZGVncmlke2dhcDo2cHh9Lm1vZGV7cGFkZGluZzoxMnB4IDJweDtmb250LXNpemU6MTJweH0ucHJvZmlsZWhlcm97cGFkZGluZzoxNXB4O2dhcDoxMXB4fS5wcm9maWxlYXZhdGFye3dpZHRoOjU3cHg7aGVpZ2h0OjU3cHg7Zm9udC1zaXplOjIzcHh9LnByb2ZpbGVoZXJvIGgze2ZvbnQtc2l6ZToxN3B4fS5wcm9maWxlZWxvIHN0cm9uZ3tmb250LXNpemU6MjRweH0uZm9ybWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uYm90dG9tbmF2e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsMWZyKTtwb3NpdGlvbjpmaXhlZDt6LWluZGV4OjUwO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3BhZGRpbmc6OHB4IDRweCBjYWxjKDhweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7YmFja2dyb3VuZDpyZ2JhKDgsMTAsMTMsLjk3KTtiYWNrZHJvcC1maWx0ZXI6Ymx1cigxNnB4KTtib3JkZXItdG9wOjFweCBzb2xpZCAjMjkzMjNkO2JveC1zaGFkb3c6MCAtOXB4IDI1cHggIzAwMDV9LmJvdHRvbW5hdiBidXR0b257ZGlzcGxheTpncmlkO2p1c3RpZnktaXRlbXM6Y2VudGVyO2dhcDozcHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjowO2NvbG9yOiNiYWM0Y2U7Zm9udC1zaXplOjlweDtwYWRkaW5nOjRweCAwfS5ib3R0b21uYXYgYnV0dG9uIHNwYW57Zm9udC1zaXplOjE5cHh9LmJvdHRvbW5hdiBidXR0b24uYWN0aXZle2NvbG9yOiNmZjlhNGR9LnRhYmxlIHRoLC50YWJsZSB0ZHtwYWRkaW5nOjEycHh9LnBhc3MtZm9vdHtmb250LXNpemU6MTBweH19CgovKiBDb250cmFzdCBoYXJkZW5pbmcgZm9yIGVtYmVkZGVkIFRlbGVncmFtL1B1enpsZS5ib3Qgd2Vidmlld3MgKi8KOnJvb3R7LS10ZXh0OiNmZmY7LS1tdXRlZDojYzBjYWQ0Oy0tc3Ryb2tlOiMzYTQ2NTN9CmJvZHl7Y29sb3I6I2ZmZn0KaDEsaDIsaDMsaDQsYixzdHJvbmcsLmJyYW5kbmFtZSwubWF0Y2hjb3B5IGIsLnBsYXllciBiLC5jYXJkaGVhZCBoMywuc2VjdGlvbmhlYWQgaDJ7Y29sb3I6I2ZmZn0KcHtjb2xvcjojZDBkN2RmfQpsYWJlbHtjb2xvcjojZDBkN2RmfQouY2FyZCwuc3RhdCwudGFibGV3cmFwLC5xdWV1ZWJveCwubWF0Y2hyb3d7Y29sb3I6I2ZmZn0KLnN0YXQgbGFiZWwsLm1hdGNoY29weSBzbWFsbCwucGxheWVyIHNtYWxsLC5zZWN0aW9uaGVhZCBwLC5tdXRlZCwuZm9vdG5vdGV7Y29sb3I6I2I5YzVkMH0KLnRhYmxlIHRoe2NvbG9yOiNlMGU2ZWN9Ci50YWJsZSB0ZHtjb2xvcjojZjdmOWZifQoubmF2IGJ1dHRvbntjb2xvcjojZDJkOWUxfQoubmF2IGJ1dHRvbi5hY3RpdmV7Y29sb3I6I2ZmYjA2Zn0KLmJ0bi1zZWNvbmRhcnl7Y29sb3I6I2ZmZjtiYWNrZ3JvdW5kOiMxYjIyMmI7Ym9yZGVyLWNvbG9yOiM0NjUzNjF9Ci50ZXh0bGlua3tjb2xvcjojZmZhZDcwfQouZmllbGQgbGFiZWx7Y29sb3I6I2Q1ZGNlNH0KLmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe2NvbG9yOiNmZmY7YmFja2dyb3VuZDojMDgwYzExO2JvcmRlci1jb2xvcjojNDY1MzYxfQoucGlsbHtjb2xvcjojNzVmMGI2fQouc3RhdCBzbWFsbHtjb2xvcjojNzVlYWIwfQoubm90aWNlIHB7Y29sb3I6I2MyY2NkNn0KLnBhc3NjYXJkIHB7Y29sb3I6I2QwZDdkZn0KCgovKiBGb3JjZSBkYXJrIGNhbnZhcyBpbiBlbWJlZGRlZCBidWlsZGVycy93ZWJ2aWV3cyAoaW5jbHVkaW5nIHNhZmUgYXJlYXMpICovCmh0bWwsYm9keXtiYWNrZ3JvdW5kLWNvbG9yOiMwODBhMGQhaW1wb3J0YW50O2NvbG9yOiNmZmYhaW1wb3J0YW50fQpib2R5e2JhY2tncm91bmQtaW1hZ2U6cmFkaWFsLWdyYWRpZW50KGVsbGlwc2UgYXQgMTAwJSAwLHJnYmEoMjU1LDgwLDE1LC4xMiksdHJhbnNwYXJlbnQgMjclKSFpbXBvcnRhbnR9CiNhcHAsLmFwcCwuY29udGFpbmVyLC5zaGVsbCwubWFpbntiYWNrZ3JvdW5kLWNvbG9yOnRyYW5zcGFyZW50fQppbnB1dCxzZWxlY3QsdGV4dGFyZWF7Y29sb3Itc2NoZW1lOmRhcmt9Cjo6c2VsZWN0aW9ue2JhY2tncm91bmQ6I2ZmNjQxYztjb2xvcjojMDgwYTBkfQoKCi8qIEhBUkQgT1ZFUlJJREVTOiBQdXp6bGUuYm90IC8gVGVsZWdyYW0gV2ViVmlldyBob3N0IHN0eWxlcyAqLwo6cm9vdCwgaHRtbCwgYm9keSB7CiAgYmFja2dyb3VuZDogIzA4MGEwZCAhaW1wb3J0YW50OwogIGJhY2tncm91bmQtY29sb3I6ICMwODBhMGQgIWltcG9ydGFudDsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50OwogIGNvbG9yLXNjaGVtZTogZGFyayAhaW1wb3J0YW50Owp9Cmh0bWwsIGJvZHkgewogIG1pbi1oZWlnaHQ6IDEwMCUgIWltcG9ydGFudDsKICBtYXJnaW46IDAgIWltcG9ydGFudDsKfQpib2R5IHsKICBiYWNrZ3JvdW5kLWltYWdlOiByYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAsIHJnYmEoMjU1LDgwLDE1LC4xMCksIHRyYW5zcGFyZW50IDMwJSkgIWltcG9ydGFudDsKfQouc2hlbGwsIC5tYWluLCBtYWluLCBzZWN0aW9uLCBhcnRpY2xlLCAucGFnZSwgLmNvbnRlbnQsIC5jb250YWluZXIsICNhcHAsICNyb290IHsKICBjb2xvcjogI2Y3ZjlmYzsKfQpoMSwgaDIsIGgzLCBoNCwgaDUsIGg2LCBwLCBsYWJlbCwgc3Bhbiwgc21hbGwsIHN0cm9uZywgYiwgbGksIHRkLCB0aCB7CiAgdGV4dC1zaGFkb3c6IG5vbmU7Cn0KLnRvcGJhciBoMSwgLmNhcmQgaDIsIC5jYXJkIGgzLCAuc2VjdGlvbmhlYWQgaDIsIC5oZXJvIGgyLAouc3RhdCBzdHJvbmcsIC5tYXRjaGNvcHkgYiwgLnBsYXllciBiLCAuYnJhbmRuYW1lIHsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50Owp9Ci5raWNrZXIsIC50ZXh0bGluaywgLnNlY3Rpb25oZWFkIGEgeyBjb2xvcjogI2ZmYWI3MCAhaW1wb3J0YW50OyB9Ci5tdXRlZCwgLmZvb3Rub3RlLCAuc2VjdGlvbmhlYWQgcCwgLm1hdGNoY29weSBzbWFsbCwgLnBsYXllciBzbWFsbCwKLnN0YXQgbGFiZWwsIC5zdGF0IHNtYWxsLCAuY2FyZCBwLCAuY2FyZCBzbWFsbCB7CiAgY29sb3I6ICNjMmNjZDYgIWltcG9ydGFudDsKfQouY2FyZCwgLnN0YXQsIC5xdWV1ZWJveCwgLnRhYmxld3JhcCwgLm1hdGNocm93LCAubm90aWNlLCAucGFzc2NhcmQgewogIGNvbG9yOiAjZjdmOWZjOwp9CkBtZWRpYSAobWF4LXdpZHRoOiA3NjBweCkgewogIGh0bWwsIGJvZHksIC5zaGVsbCwgLm1haW4geyBiYWNrZ3JvdW5kLWNvbG9yOiAjMDgwYTBkICFpbXBvcnRhbnQ7IH0KfQoKCi8qIEZJTkFMIFJFU1BPTlNJVkUgKyBDTEFOIFJPU1RFUiBGSVggKi8KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIHsgd2lkdGg6MTAwJTsgbWluLXdpZHRoOjA7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBkaXNwbGF5OmdyaWQgIWltcG9ydGFudDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDFmcikgbWF4LWNvbnRlbnQgIWltcG9ydGFudDsgYWxpZ24taXRlbXM6Y2VudGVyICFpbXBvcnRhbnQ7IGNvbHVtbi1nYXA6MTZweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MDsgcGFkZGluZzoxM3B4IDJweCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHsgZGlzcGxheTpmbGV4ICFpbXBvcnRhbnQ7IGFsaWduLWl0ZW1zOmNlbnRlciAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MCAhaW1wb3J0YW50OyBvdmVyZmxvdzpoaWRkZW47IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgPiBkaXY6bGFzdC1jaGlsZCB7IG1pbi13aWR0aDowICFpbXBvcnRhbnQ7IGZsZXg6MSAxIGF1dG8gIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAjY2xhbk1lbWJlcnMgLnBsYXllciBiIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgZm9udC13ZWlnaHQ6ODAwICFpbXBvcnRhbnQ7IHdoaXRlLXNwYWNlOm5vcm1hbCAhaW1wb3J0YW50OyBvdmVyZmxvdy13cmFwOmFueXdoZXJlICFpbXBvcnRhbnQ7IHdvcmQtYnJlYWs6YnJlYWstd29yZCAhaW1wb3J0YW50OyBvdmVyZmxvdzp2aXNpYmxlICFpbXBvcnRhbnQ7IHRleHQtb3ZlcmZsb3c6Y2xpcCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHNtYWxsIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBtYXJnaW4tdG9wOjRweCAhaW1wb3J0YW50OyBjb2xvcjojYzJjY2Q2ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxMXB4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgd2hpdGUtc3BhY2U6bm9ybWFsICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5taW5pYXZhIHsgd2lkdGg6NDBweCAhaW1wb3J0YW50OyBoZWlnaHQ6NDBweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA0MHB4ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5lbG8geyBkaXNwbGF5OmJsb2NrICFpbXBvcnRhbnQ7IGp1c3RpZnktc2VsZjplbmQgIWltcG9ydGFudDsgYWxpZ24tc2VsZjpjZW50ZXIgIWltcG9ydGFudDsgbWluLXdpZHRoOjQ0cHg7IHBhZGRpbmc6N3B4IDlweCAhaW1wb3J0YW50OyBib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTM7IGJvcmRlci1yYWRpdXM6OXB4OyBiYWNrZ3JvdW5kOiMyMDI4MzM7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgdGV4dC1hbGlnbjpyaWdodCAhaW1wb3J0YW50OyB3aGl0ZS1zcGFjZTpub3dyYXAgIWltcG9ydGFudDsgZm9udC1zaXplOjEzcHggIWltcG9ydGFudDsgbGluZS1oZWlnaHQ6MS4yICFpbXBvcnRhbnQ7IGZvbnQtd2VpZ2h0OjkwMCAhaW1wb3J0YW50OyBmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7IH0KI3BhZ2UtY2xhbnMgLmNhcmQgeyBtaW4td2lkdGg6MDsgb3ZlcmZsb3c6aGlkZGVuOyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCB7IGdhcDoxMnB4OyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCBoMyB7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8geyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gLmdyb3cgeyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gaDMgeyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IG92ZXJmbG93LXdyYXA6YW55d2hlcmU7IH0KI3BhZ2UtY2xhbnMgLmNsYW5oZXJvIHAsICNwYWdlLWNsYW5zIC5zZWN0aW9uaGVhZCBwIHsgY29sb3I6I2MyY2NkNiAhaW1wb3J0YW50OyB9Cmh0bWwsIGJvZHkgeyB3aWR0aDoxMDAlOyBtYXgtd2lkdGg6MTAwJTsgb3ZlcmZsb3cteDpoaWRkZW47IH0KaW1nLCBzdmcsIHZpZGVvLCBjYW52YXMsIGJ1dHRvbiwgaW5wdXQsIHNlbGVjdCwgdGV4dGFyZWEgeyBtYXgtd2lkdGg6MTAwJTsgfQpAbWVkaWEgKG1heC13aWR0aDo2MDBweCkgewogLnNlY3Rpb24geyBwYWRkaW5nLWxlZnQ6MTRweDsgcGFkZGluZy1yaWdodDoxNHB4OyB9CiAuc2VjdGlvbmhlYWQgeyBhbGlnbi1pdGVtczpmbGV4LXN0YXJ0OyBmbGV4LXdyYXA6d3JhcDsgZ2FwOjEwcHg7IH0KIC5zZWN0aW9uaGVhZCBoMiB7IGZvbnQtc2l6ZToyM3B4OyBsaW5lLWhlaWdodDoxLjI7IH0KIC5zZWN0aW9uaGVhZCBwIHsgZm9udC1zaXplOjEzcHg7IGxpbmUtaGVpZ2h0OjEuNTsgfQogLmxheW91dCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczptaW5tYXgoMCwxZnIpICFpbXBvcnRhbnQ7IGdhcDoxNHB4OyB9CiAuY2FyZCB7IG1pbi13aWR0aDowOyBwYWRkaW5nOjE1cHg7IH0KIC5jbGFuaGVybyB7IHBhZGRpbmc6MTRweCAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBmbGV4LXdyYXA6d3JhcDsgfQogLmNsYW5sb2dvIHsgd2lkdGg6NTJweCAhaW1wb3J0YW50OyBoZWlnaHQ6NTJweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA1MnB4OyBmb250LXNpemU6MjVweCAhaW1wb3J0YW50OyB9CiAuY2xhbmhlcm8gLmdyb3cgeyBmbGV4OjEgMSBjYWxjKDEwMCUgLSA3NXB4KTsgfQogLmNsYW5oZXJvIGgzIHsgZm9udC1zaXplOjE3cHggIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBjb2x1bW4tZ2FwOjEwcHggIWltcG9ydGFudDsgcGFkZGluZzoxMnB4IDAgIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgYiB7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IH0KICNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAuZWxvIHsgbWluLXdpZHRoOjQycHg7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IHBhZGRpbmc6N3B4ICFpbXBvcnRhbnQ7IH0KIC5tYXBncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpOyB9Cn0KCgovKiBNYXRjaCBDZW50ZXIg4oCUIGRlZGljYXRlZCBjb21wZXRpdGl2ZSBmbG93ICovCi5tYXRjaC1jZW50ZXJ7ZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXI6MXB4IHNvbGlkICMzYTQxNGI7Ym9yZGVyLXJhZGl1czoxOHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMSwjMGUxMjE3KTtib3gtc2hhZG93OjAgMThweCA0NXB4ICMwMDA1fQoubWF0Y2gtY2VudGVyLnZpc2libGV7ZGlzcGxheTpibG9ja30ubWMtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjE0cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTdweCAxOXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyOTMxM2E7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTAwZGVnLCMyNDE1MGYsIzE0MTkxZil9Lm1jLXRpdGxle2ZvbnQtd2VpZ2h0Ojk1MDtmb250LXNpemU6MThweH0ubWMtc3Vie2ZvbnQtc2l6ZToxMnB4O2NvbG9yOiNhZWI4YzM7bWFyZ2luLXRvcDo0cHh9Lm1jLXN0YXR1c3tib3JkZXI6MXB4IHNvbGlkICM3MDQwMWY7Y29sb3I6I2ZmYWQ2OTtiYWNrZ3JvdW5kOiMzNTFlMTM7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo2cHggOXB4O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjkwMDtsZXR0ZXItc3BhY2luZzouN3B4O3doaXRlLXNwYWNlOm5vd3JhcH0ubWMtYm9keXtwYWRkaW5nOjE4cHg7ZGlzcGxheTpncmlkO2dhcDoxNnB4fS5tYy1wcm9ncmVzc3toZWlnaHQ6N3B4O2JvcmRlci1yYWRpdXM6OTlweDtiYWNrZ3JvdW5kOiMyNTJjMzQ7b3ZlcmZsb3c6aGlkZGVufS5tYy1wcm9ncmVzcyBzcGFue2Rpc3BsYXk6YmxvY2s7aGVpZ2h0OjEwMCU7d2lkdGg6MDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjZmY0ZTBiLCNmZjlhMjcpO3RyYW5zaXRpb246d2lkdGggLjJzfS5tYy1wbGF5ZXJncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtcGxheWVye21pbi13aWR0aDowO2JvcmRlcjoxcHggc29saWQgIzMwMzk0NDtiYWNrZ3JvdW5kOiMxNzFkMjQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7dGV4dC1hbGlnbjpjZW50ZXJ9Lm1jLXBsYXllci5hY2NlcHRlZHtib3JkZXItY29sb3I6IzIzNzk1NztiYWNrZ3JvdW5kOiMxMTI3MWZ9Lm1jLXBsYXllciBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfS5tYy1wbGF5ZXIgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojOThhNWIxO2ZvbnQtc2l6ZTo5cHg7bWFyZ2luLXRvcDo0cHh9Lm1jLXBsYXllciAubWMtYXZhdGFye3dpZHRoOjI4cHg7aGVpZ2h0OjI4cHg7Ym9yZGVyLXJhZGl1czo5cHg7bWFyZ2luOjAgYXV0byA2cHg7ZGlzcGxheTpncmlkO3BsYWNlLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMzMDIwMTY7Y29sb3I6I2ZmOWI1MDtmb250LXdlaWdodDo5NTB9Lm1jLWFjdGlvbnN7ZGlzcGxheTpmbGV4O2dhcDo5cHg7ZmxleC13cmFwOndyYXB9Lm1jLWFjdGlvbnMgLmJ0bi1wcmltYXJ5LC5tYy1hY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2ZsZXg6MTttaW4td2lkdGg6MTMwcHh9Lm1jLXR3b3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9Lm1jLXBhbmVse2JvcmRlcjoxcHggc29saWQgIzJiMzMzZDtib3JkZXItcmFkaXVzOjEzcHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6IzExMTYxY30ubWMtcGFuZWwgaDR7bWFyZ2luOjAgMCA5cHg7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjhweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmOWE0ZH0ubWMtbXV0ZWR7Y29sb3I6IzlkYTliNTtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6NnB4O21hcmdpbjo5cHggMH0ubWMtZmllbGQgbGFiZWx7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNTtsZXR0ZXItc3BhY2luZzouN3B4O2ZvbnQtd2VpZ2h0OjgwMH0ubWMtZmllbGQgc2VsZWN0LC5tYy1maWVsZCBpbnB1dCwubWMtZmllbGQgdGV4dGFyZWF7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMwYjBmMTM7Ym9yZGVyOjFweCBzb2xpZCAjMzQzZDQ3O2JvcmRlci1yYWRpdXM6OHB4O2NvbG9yOiNmNGY2Zjg7cGFkZGluZzoxMHB4O21pbi13aWR0aDowfS5tYy1tYXBze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtbWFwe2JvcmRlcjoxcHggc29saWQgIzMzM2Q0ODtiYWNrZ3JvdW5kOiMxODFmMjc7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMXB4fS5tYy1tYXA6aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZjc4MmF9Lm1jLW1hcC51c2Vke29wYWNpdHk6LjM1O3BvaW50ZXItZXZlbnRzOm5vbmU7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaH0ubWMtbG9ne2Rpc3BsYXk6Z3JpZDtnYXA6NnB4fS5tYy1sb2dyb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7Zm9udC1zaXplOjExcHg7cGFkZGluZzo4cHggOXB4O2JvcmRlci1yYWRpdXM6OHB4O2JhY2tncm91bmQ6IzFhMjAyOH0ubWMtbG9ncm93IHNwYW46bGFzdC1jaGlsZHtjb2xvcjojZmZhZDY5fS5tYy10ZWFte2JvcmRlcjoxcHggc29saWQgIzJlMzk0NDtiYWNrZ3JvdW5kOiMxNTFiMjI7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTJweH0ubWMtdGVhbSBoNHttYXJnaW46MCAwIDhweDtmb250LXNpemU6MTJweH0ubWMtdGVhbSB1bHttYXJnaW46MDtwYWRkaW5nLWxlZnQ6MThweDtjb2xvcjojYzNjYmQzO2ZvbnQtc2l6ZToxMnB4O2xpbmUtaGVpZ2h0OjEuOX0ubWMtZm9ybWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo5cHh9Lm1jLW5vdGljZXtib3JkZXI6MXB4IHNvbGlkICMzYzMwMjM7YmFja2dyb3VuZDojMjQxYjE0O2NvbG9yOiNlNmM5YWQ7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6MTBweDtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWhpZGRlbntkaXNwbGF5Om5vbmUhaW1wb3J0YW50fQpAbWVkaWEobWF4LXdpZHRoOjYwMHB4KXsubWMtYm9keXtwYWRkaW5nOjEzcHh9Lm1jLXBsYXllcmdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg1LG1pbm1heCgwLDFmcikpO2dhcDo0cHh9Lm1jLXBsYXllcntwYWRkaW5nOjhweCAycHh9Lm1jLXBsYXllciBie2ZvbnQtc2l6ZTo5cHh9Lm1jLW1hcHN7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5tYy10d297Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0ubWMtaGVhZHtwYWRkaW5nOjE0cHh9Lm1jLXRpdGxle2ZvbnQtc2l6ZToxNnB4fX0KCgouY29zbWV0aWMtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLG1pbm1heCgwLDFmcikpO2dhcDoxMnB4fS5jb3NtZXRpYy1pdGVte2JvcmRlcjoxcHggc29saWQgIzM1NDE0ZDtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxNHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMTcxZTI2LCMwZTEyMTcpO21pbi13aWR0aDowfS5jb3NtZXRpYy1hcnR7aGVpZ2h0Ojk1cHg7Ym9yZGVyLXJhZGl1czoxMnB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Zm9udC1zaXplOjM2cHg7bWFyZ2luLWJvdHRvbToxMnB4O2JhY2tncm91bmQ6cmFkaWFsLWdyYWRpZW50KGNpcmNsZSBhdCA1MCUgMjAlLCM2MTM1MWUsIzFhMWQyMiA3MiUpO2JvcmRlcjoxcHggc29saWQgIzZhM2EyMH0uY29zbWV0aWMtaXRlbSBoNHttYXJnaW46MCAwIDVweDtmb250LXNpemU6MTNweH0uY29zbWV0aWMtaXRlbSBwe2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiZGM4ZDI7bWluLWhlaWdodDozMnB4O21hcmdpbjowIDAgMTBweH0uY29zbWV0aWMtbWV0YXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNmZmFkNzB9LmNvc21ldGljLWl0ZW0gYnV0dG9ue3dpZHRoOjEwMCU7Zm9udC1zaXplOjExcHg7cGFkZGluZzoxMHB4IDhweH0ucmFyaXR5e2ZvbnQtc2l6ZTo5cHg7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi43cHg7Y29sb3I6I2ZmY2E2OH0udHJvcGh5LWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCxtaW5tYXgoMCwxZnIpKTtnYXA6MTJweH0udHJvcGh5e3Bvc2l0aW9uOnJlbGF0aXZlO292ZXJmbG93OmhpZGRlbjtwYWRkaW5nOjA7dGV4dC1hbGlnbjpsZWZ0O2JvcmRlcjoxcHggc29saWQgIzM5NDY1Mztib3JkZXItcmFkaXVzOjE1cHg7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTU1ZGVnLCMxMTE4MjAsIzA4MGMxMSA3OCUpO2JveC1zaGFkb3c6aW5zZXQgMCAxcHggMCAjZmZmZmZmMGQsMCA5cHggMjRweCAjMDAwNTttaW4td2lkdGg6MH0udHJvcGh5OmJlZm9yZXtjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCA1MCUgMjIlLHZhcigtLXRyb3BoeS1nbG93LCNmZjliMzAyMCksdHJhbnNwYXJlbnQgNjclKTtwb2ludGVyLWV2ZW50czpub25lfS50cm9waHkudHJvcGh5LWdvbGR7LS10cm9waHktZ2xvdzojZmZhZTM1MzA7Ym9yZGVyLWNvbG9yOiNhNjZjMjB9LnRyb3BoeS50cm9waHktc2lsdmVyey0tdHJvcGh5LWdsb3c6I2I5ZDVmNTIyO2JvcmRlci1jb2xvcjojNjU3MTdlfS50cm9waHkudHJvcGh5LWJyb256ZXstLXRyb3BoeS1nbG93OiNmZjc4MzUyYjtib3JkZXItY29sb3I6I2E5NTAyNX0udHJvcGh5LnRyb3BoeS1vdGhlcnstLXRyb3BoeS1nbG93OiM4YWE4ZDUxYTtib3JkZXItY29sb3I6IzUzNjc3OX0udHJvcGh5IC5jdXB7aGVpZ2h0OjE5MHB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7cG9zaXRpb246cmVsYXRpdmU7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTQ1ZGVnLCMxNjFjMjMsIzA4MGMxMCk7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgI2ZmZmZmZjEyfS50cm9waHkgLmN1cDphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO2JvdHRvbTo4cHg7bGVmdDoxMiU7cmlnaHQ6MTIlO2hlaWdodDoxOHB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tdHJvcGh5LWdsb3csI2ZmOWIzMDIwKTtmaWx0ZXI6Ymx1cigxMHB4KX0udHJvcGh5IC5jdXAgc3Zne3dpZHRoOjEwMCU7bWF4LXdpZHRoOjE5MHB4O2hlaWdodDoxODBweDtvdmVyZmxvdzp2aXNpYmxlO3Bvc2l0aW9uOnJlbGF0aXZlO3otaW5kZXg6MTtmaWx0ZXI6ZHJvcC1zaGFkb3coMCA5cHggN3B4ICMwMDBhKX0udHJvcGh5LWluZm97cG9zaXRpb246cmVsYXRpdmU7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtwYWRkaW5nOjEycHh9LnRyb3BoeS1yYW5re2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7bWluLXdpZHRoOjQycHg7aGVpZ2h0OjM2cHg7cGFkZGluZzowIDVweDtib3JkZXItcmFkaXVzOjVweDt0cmFuc2Zvcm06c2tldygtMTJkZWcpO2ZvbnQtd2VpZ2h0Ojk1MDtmb250LXNpemU6MTRweDtjb2xvcjojMTExO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmZkYzgzLCNlYWEzMmIpfS50cm9waHktc2lsdmVyIC50cm9waHktcmFua3tiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMzVkZWcsI2Y0ZjdmYiwjOWNhOWI3KX0udHJvcGh5LWJyb256ZSAudHJvcGh5LXJhbmt7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCNmZmIxN2QsI2Q2NWQyNil9LnRyb3BoeS1vdGhlciAudHJvcGh5LXJhbmt7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCNjMGQyZTUsIzYxNzY4ZSk7Y29sb3I6IzEwMTgyMH0udHJvcGh5LWluZm8gYntkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMXB4O21hcmdpbjowIDAgNHB4O2xpbmUtaGVpZ2h0OjEuM30udHJvcGh5LWluZm8gc21hbGx7Zm9udC1zaXplOjlweDtjb2xvcjojOWVhY2JhO2Rpc3BsYXk6YmxvY2s7b3ZlcmZsb3ctd3JhcDphbnl3aGVyZTtsaW5lLWhlaWdodDoxLjR9LnRyb3BoeSAudHJvcGh5LXBsYWNle3Bvc2l0aW9uOmFic29sdXRlO3RvcDo4cHg7cmlnaHQ6OHB4O3otaW5kZXg6Mjtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjkwMDtsZXR0ZXItc3BhY2luZzouNHB4O2NvbG9yOiNmZmJkNjE7Ym9yZGVyOjFweCBzb2xpZCAjZmZiZDYxNDU7Ym9yZGVyLXJhZGl1czo1cHg7cGFkZGluZzo0cHggNnB4O2JhY2tncm91bmQ6IzA4MGIxMGM5fUBtZWRpYShtYXgtd2lkdGg6NzAwcHgpey50cm9waHktZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDIsbWlubWF4KDAsMWZyKSl9LnRyb3BoeSAuY3Vwe2hlaWdodDoxNTBweH0udHJvcGh5IC5jdXAgc3Zne2hlaWdodDoxNDJweDttYXgtd2lkdGg6MTUwcHh9LnRyb3BoeS1pbmZve3BhZGRpbmc6MTBweDtnYXA6OHB4fS50cm9waHktcmFua3ttaW4td2lkdGg6MzRweDtoZWlnaHQ6MzFweDtmb250LXNpemU6MTJweH19fS5zdGF0LWRldGFpbC1ncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9LnN0YXQtZGV0YWlse3BhZGRpbmc6MTRweDtib3JkZXI6MXB4IHNvbGlkICMzNDQwNGM7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0uc3RhdC1kZXRhaWwgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojYjdjMmNjO2ZvbnQtc2l6ZToxMHB4fS5zdGF0LWRldGFpbCBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MjJweDttYXJnaW4tdG9wOjdweDtjb2xvcjojZmY5YTRkfS5jb3NtZXRpYy1lbXB0eXtncmlkLWNvbHVtbjoxLy0xO3BhZGRpbmc6MjBweDt0ZXh0LWFsaWduOmNlbnRlcjtjb2xvcjojYjljNWQwO2JvcmRlcjoxcHggZGFzaGVkICMzOTQ2NTM7Ym9yZGVyLXJhZGl1czoxMnB4fUBtZWRpYShtYXgtd2lkdGg6NzAwcHgpey5jb3NtZXRpYy1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX0udHJvcGh5LWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5zdGF0LWRldGFpbC1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX19Cgo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJzaGVsbCI+Cjxhc2lkZSBjbGFzcz0ic2lkZWJhciI+CiAgPGRpdiBjbGFzcz0iYnJhbmQiPjxkaXYgY2xhc3M9ImJyYW5kbWFyayI+UzwvZGl2PjxkaXY+PGRpdiBjbGFzcz0iYnJhbmRuYW1lIj5TVEFORDxzcGFuPlBPSU5UPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImJyYW5kdGFnIj5TVEFORE9GRiAyIOKAoiBDT01QRVRJVElWRTwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9Im5hdmdyb3VwIj48bGFiZWw+0J/Qm9CQ0KLQpNCe0KDQnNCQPC9sYWJlbD48bmF2IGNsYXNzPSJuYXYiIGlkPSJkZXNrdG9wTmF2Ij4KICAgIDxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS1wYWdlPSJob21lIj48c3BhbiBjbGFzcz0ibmkiPuKMgjwvc3Bhbj7Qk9C70LDQstC90LDRjzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9Im1hdGNoZXMiPjxzcGFuIGNsYXNzPSJuaSI+4pqUPC9zcGFuPtCc0LDRgtGH0Lg8L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJyYXRpbmciPjxzcGFuIGNsYXNzPSJuaSI+4pmcPC9zcGFuPtCg0LXQudGC0LjQvdCzIEVMTzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9ImNsYW5zIj48c3BhbiBjbGFzcz0ibmkiPuKZojwvc3Bhbj7QmtC70LDQvdGLIDxzcGFuIGNsYXNzPSJuYXZjb3VudCI+TkVXPC9zcGFuPjwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InBhc3MiPjxzcGFuIGNsYXNzPSJuaSI+4pymPC9zcGFuPlNUQU5EUE9JTlQgUGFzczwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9ImZyaWVuZHMiPjxzcGFuIGNsYXNzPSJuaSI+4pmnPC9zcGFuPtCU0YDRg9C30YzRjzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9Im5vdGlmaWNhdGlvbnMiPjxzcGFuIGNsYXNzPSJuaSI+4pmnPC9zcGFuPtCj0LLQtdC00L7QvNC70LXQvdC40Y8gPHNwYW4gY2xhc3M9Im5hdmNvdW50Ij4yPC9zcGFuPjwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InByb2ZpbGUiPjxzcGFuIGNsYXNzPSJuaSI+4peJPC9zcGFuPtCf0YDQvtGE0LjQu9GMPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0iY29zbWV0aWNzIj48c3BhbiBjbGFzcz0ibmkiPuKcpzwvc3Bhbj7QnNCw0LPQsNC30LjQvSDQutC+0YHQvNC10YLQuNC60Lg8L2J1dHRvbj4KICA8L25hdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJzaWRlYmFyLWZvb3QiPjxkaXYgY2xhc3M9InVzZXJjYXJkIj48ZGl2IGNsYXNzPSJhdmF0YXIiPlM8L2Rpdj48ZGl2PjxiIGlkPSJzaWRlTmFtZSI+c3RhbmRwb2ludF9wbGF5ZXI8L2I+PHNtYWxsPuKXjyDQlNCV0JzQni3Qn9Cg0J7QpNCY0JvQrDwvc21hbGw+PC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7QotCS0J7QryDQmNCT0KDQkC4g0KLQktCe0K8g0J/QntCX0JjQptCY0K8uPC9wPjwvZGl2Pgo8L2FzaWRlPgo8bWFpbiBjbGFzcz0ibWFpbiI+CjxoZWFkZXIgY2xhc3M9InRvcGJhciI+PGRpdj48ZGl2IGNsYXNzPSJraWNrZXIiPlNUQU5ET0ZGIDIg4oCiIEVTUE9SVFMgUExBVEZPUk08L2Rpdj48aDEgaWQ9InBhZ2VUaXRsZSI+0KLQstC+0Y8g0LjQs9GA0LAuINCi0LLQvtGPINC/0L7Qt9C40YbQuNGPLjwvaDE+PC9kaXY+PGRpdiBjbGFzcz0idG9wYWN0aW9ucyI+PHNwYW4gaWQ9ImFwaVN0YXR1cyIgY2xhc3M9ImJhZGdlLW9yYW5nZSI+QVBJOiDQv9C+0LTQutC70Y7Rh9C10L3QuNC14oCmPC9zcGFuPjxidXR0b24gY2xhc3M9Imljb25idG4iIGRhdGEtcGFnZT0ibm90aWZpY2F0aW9ucyIgYXJpYS1sYWJlbD0i0KPQstC10LTQvtC80LvQtdC90LjRjyI+4pmnPGkgY2xhc3M9InBpbmciPjwvaT48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBkYXRhLXBhZ2U9InByb2ZpbGUiPtCc0L7QuSDQv9GA0L7RhNC40LvRjCDihpc8L2J1dHRvbj48L2Rpdj48L2hlYWRlcj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIGFjdGl2ZSIgaWQ9InBhZ2UtaG9tZSI+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiPgogIDxkaXYgY2xhc3M9InN0YWNrIj4KICAgPGRpdiBjbGFzcz0iaGVybyI+PGRpdiBjbGFzcz0iaGVyby1jb3B5Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQktCV0KDQqNCY0J3QlTwvZGl2PjxoMj7QmNCz0YDQsNC5INGD0LzQvdC10LUuPGJyPjxzcGFuPtCf0L7QtNC90LjQvNCw0LkgRUxPLjwvc3Bhbj48L2gyPjxwPtCd0LDRhdC+0LTQuCDQutC+0LzQsNC90LTRgywg0L/QvtCx0LXQttC00LDQuSDQsiDRgNC10LnRgtC40L3Qs9C+0LLRi9GFINC80LDRgtGH0LDRhSDQuCDQtNC+0LrQsNC30YvQstCw0LksINC60YLQviDQt9C00LXRgdGMINGB0LjQu9GM0L3QtdC50YjQuNC5LjwvcD48ZGl2IGNsYXNzPSJoZXJvLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBkYXRhLXBhZ2U9Im1hdGNoZXMiPuKalCDQndCw0LnRgtC4INC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgZGF0YS1wYWdlPSJyYXRpbmciPtCi0LDQsdC70LjRhtCwINC70LjQtNC10YDQvtCyPC9idXR0b24+PC9kaXY+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0KLQktCe0JkgRUxPPC9sYWJlbD48c3Ryb25nIGlkPSJob21lRWxvIj4xMDAwPC9zdHJvbmc+PHNtYWxsPuKXjyDQodGC0LDRgNGC0L7QstGL0Lkg0YDQtdC50YLQuNC90LM8L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxsYWJlbD7Qn9Ce0JHQldCU0Ks8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXaW5zIj4wPC9zdHJvbmc+PHNtYWxsPtCc0LDRgtGH0Lgg0YHQtdC30L7QvdCwPC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+V0lOIFJBVEU8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXUiI+MCU8L3N0cm9uZz48c21hbGw+0J7QsdGJ0LjQuSDQv9C+0LrQsNC30LDRgtC10LvRjDwvc21hbGw+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JHQu9C40LbQsNC50YjQuNC5INC80LDRgtGHPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJtYXRjaGVzIj7QktGB0LUg0LzQsNGC0YfQuCDihpI8L2J1dHRvbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaHJvdyI+PGRpdiBjbGFzcz0ibWF0Y2hsb2dvIj7impQ8L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaGNvcHkiPjxiPtCg0LXQudGC0LjQvdCz0L7QstGL0Lkg0LzQsNGC0YcgwrcgNXY1PC9iPjxzbWFsbD5TYW5kc3RvbmUgwrcgQ29tcGV0aXRpdmUgwrcg0J7QttC40LTQsNC90LjQtSDQuNCz0YDQvtC60L7Qsjwvc21hbGw+PC9kaXY+PHNwYW4gY2xhc3M9InBpbGwiPkxJVkUgUVVFVUU8L3NwYW4+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QtdC00LDQstC90LjQtSDRgNC10LfRg9C70YzRgtCw0YLRizwvaDM+PHNwYW4gY2xhc3M9Im11dGVkIHNtYWxsIj7QodCV0JfQntCdIDAzPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7inKY8L2Rpdj48ZGl2PjxiPtCd0L7QstGL0Lkg0YHQtdC30L7QvSDQvdCw0YfQsNC70YHRjzwvYj48cD7Qn9C+0LTQvdC40LzQsNC5INGA0LXQudGC0LjQvdCzINC4INC+0YLQutGA0YvQstCw0Lkg0L3QsNCz0YDQsNC00YsgU1RBTkRQT0lOVCBQYXNzLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pqUPC9kaXY+PGRpdj48Yj7QoNC10LnRgtC40L3Qs9C+0LLQsNGPINC+0YfQtdGA0LXQtNGMINC+0YLQutGA0YvRgtCwPC9iPjxwPtCh0L7QsdC40YDQsNC5INC60L7QvNCw0L3QtNGDINC4INCy0YHRgtGD0L/QsNC5INCyINCx0L7QuS48L3A+PC9kaXY+PC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ic3RhY2siPgogICA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QodCV0JfQntCdIDAzPC9kaXY+PGgzPlNUQU5EUE9JTlQgUEFTUzwvaDM+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1sZXZlbCIgaWQ9InBhc3NMZXZlbCI+MDE8L2Rpdj48L2Rpdj48cD7QmNCz0YDQsNC5INC80LDRgtGH0LgsINC30LDRgNCw0LHQsNGC0YvQstCw0LkgWFAg0Lgg0L7RgtC60YDRi9Cy0LDQuSDQvdCw0LPRgNCw0LTRiyDRgdC10LfQvtC90LAuPC9wPjxkaXYgY2xhc3M9InByb2dyZXNzIj48c3BhbiBpZD0icGFzc1Byb2dyZXNzIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1mb290Ij48c3BhbiBpZD0icGFzc1hQIj4xMjAgLyAzMDAgWFA8L3NwYW4+PGJ1dHRvbiBjbGFzcz0idGV4dGxpbmsiIGRhdGEtcGFnZT0icGFzcyI+0J7RgtC60YDRi9GC0YwgUGFzcyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotC+0L8g0LjQs9GA0L7QutC+0LI8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBkYXRhLXBhZ2U9InJhdGluZyI+0JLRgdC1INGA0LXQudGC0LjQvdCz0Lgg4oaSPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImhvbWVMZWFkZXJzIj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotCy0L7QuSDQv9GA0L7Qs9GA0LXRgdGBPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5TRUFTT04gMDM8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hyb3ciPjxkaXYgY2xhc3M9Im1hdGNobG9nbyI+4pmcPC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hjb3B5Ij48Yj7Qn9C+0LTQvdC40LzQsNC50YHRjyDQsiDRgNC10LnRgtC40L3Qs9C1PC9iPjxzbWFsbD7QmtCw0LbQtNCw0Y8g0L/QvtCx0LXQtNCwINC/0YDQuNCx0LvQuNC20LDQtdGCINC6INGN0LvQuNGC0LUuPC9zbWFsbD48L2Rpdj48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJyYXRpbmciPkVMTyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KIDwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtbWF0Y2hlcyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0JzQsNGC0YfQvNC10LnQutC40L3QszwvaDI+PHA+0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0LrQsNGA0YLRgy4g0J3QsNC50LTQuCDRgdC+0L/QtdGA0L3QuNC60L7QsiDRgdCy0L7QtdCz0L4g0YPRgNC+0LLQvdGPLjwvcD48L2Rpdj48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5DT01QRVRJVElWRTwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9ImxheW91dCI+CiAgPGRpdiBjbGFzcz0ic3RhY2siPjxkaXYgY2xhc3M9InF1ZXVlYm94Ij48ZGl2IGNsYXNzPSJxdWV1ZWljb24iIGlkPSJxdWV1ZUljb24iPuKalDwvZGl2PjxkaXYgY2xhc3M9ImV5ZWJyb3ciPtCf0J7QmNCh0Jog0JzQkNCi0KfQkDwvZGl2PjxoMyBpZD0icXVldWVUaXRsZSI+0JPQvtGC0L7QsiDQuiDQuNCz0YDQtT88L2gzPjxwIGlkPSJxdWV1ZURlc2NyaXB0aW9uIj7QktGL0LHQtdGA0Lgg0YDQtdC20LjQvCDQuCDQvdCw0YfQvdC4INC/0L7QuNGB0Log0YDQtdC50YLQuNC90LPQvtCy0L7Qs9C+INC80LDRgtGH0LAuPC9wPjxkaXYgY2xhc3M9Im1vZGVncmlkIiBpZD0ibW9kZUdyaWQiPjxidXR0b24gY2xhc3M9Im1vZGUiIGRhdGEtbW9kZT0iMXYxIj4xdjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtb2RlIiBkYXRhLW1vZGU9IjJ2MiI+MnYyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibW9kZSIgZGF0YS1tb2RlPSIzdjMiPjN2MzwvYnV0dG9uPjxidXR0b24gY2xhc3M9Im1vZGUgYWN0aXZlIiBkYXRhLW1vZGU9IjV2NSI+NXY1PC9idXR0b24+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJxdWV1ZUJ1dHRvbiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQudGC0Lgg0LzQsNGC0Yc8L2J1dHRvbj48cCBjbGFzcz0iZm9vdG5vdGUiIGlkPSJxdWV1ZUFwaU5vdGUiPtCf0L7QuNGB0Log0Lgg0YHRgtCw0YLRg9GBINC+0YfQtdGA0LXQtNC4INGB0LjQvdGF0YDQvtC90LjQt9C40YDRg9GO0YLRgdGPINGBINGB0LXRgNCy0LXRgNC+0LwgU1RBTkRQT0lOVC48L3A+PC9kaXY+CiAgPGRpdiBjbGFzcz0ibWF0Y2gtY2VudGVyIiBpZD0ibWF0Y2hDZW50ZXIiIGFyaWEtbGl2ZT0icG9saXRlIj4KICAgPGRpdiBjbGFzcz0ibWMtaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5TVEFORFBPSU5UIENPTVBFVElUSVZFPC9kaXY+PGRpdiBjbGFzcz0ibWMtdGl0bGUiIGlkPSJtY1RpdGxlIj7QnNCw0YLRhyDQvdCw0LnQtNC10L08L2Rpdj48ZGl2IGNsYXNzPSJtYy1zdWIiIGlkPSJtY1N1YnRpdGxlIj7Qn9C+0LTRgtCy0LXRgNC00Lgg0YPRh9Cw0YHRgtC40LUsINGH0YLQvtCx0Ysg0L/QtdGA0LXQudGC0Lgg0LIg0LvQvtCx0LHQuC48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0ibWMtc3RhdHVzIiBpZD0ibWNTdGF0dXMiPk1BVENIIEZPVU5EPC9zcGFuPjwvZGl2PgogICA8ZGl2IGNsYXNzPSJtYy1ib2R5Ij4KICAgIDxkaXYgaWQ9Im1jQWNjZXB0UGFuZWwiPjxkaXYgY2xhc3M9Im1jLXByb2dyZXNzIj48c3BhbiBpZD0ibWNBY2NlcHRQcm9ncmVzcyI+PC9zcGFuPjwvZGl2PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQWNjZXB0VGV4dCI+0J7QttC40LTQsNC10Lwg0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8g0LjQs9GA0L7QutC+0LLigKY8L3A+PGRpdiBjbGFzcz0ibWMtcGxheWVyZ3JpZCIgaWQ9Im1jQWNjZXB0UGxheWVycyI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyIgc3R5bGU9Im1hcmdpbi10b3A6MTJweCI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJtY0FjY2VwdEJ0biI+4pyTINCf0YDQuNC90Y/RgtGMINC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9Im1jRGVjbGluZUJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJtY0JhblBhbmVsIiBjbGFzcz0ibWMtaGlkZGVuIj4KICAgICA8ZGl2IGNsYXNzPSJtYy10d28iPjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQTwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5BIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQSIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEE8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5BSW5mbyI+PC9wPjwvZGl2PjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQjwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5CIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEI8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5CSW5mbyI+PC9wPjwvZGl2PjwvZGl2PgogICAgIDxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQgaWQ9Im1jQmFuVGl0bGUiPkJhbiAvIFBpY2sg0LrQsNGA0YI8L2g0PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQmFuSGludCI+0JrQsNC/0LjRgtCw0L3RiyDQv9C+INC+0YfQtdGA0LXQtNC4INCx0LvQvtC60LjRgNGD0Y7RgiDQutCw0YDRgtGLLCDQt9Cw0YLQtdC8INCy0YvQsdC40YDQsNGO0YIg0LrQsNGA0YLRgyDQvNCw0YLRh9CwLjwvcD48ZGl2IGNsYXNzPSJtYy1tYXBzIiBpZD0ibWNNYXBCdXR0b25zIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1sb2ciIGlkPSJtY01hcExvZyIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9Im1jTG9iYnlQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCDQmtC+0LzQsNC90LTQsCBBPC9oND48dWwgaWQ9Im1jVGVhbUEiPjwvdWw+PC9kaXY+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCflLUg0JrQvtC80LDQvdC00LAgQjwvaDQ+PHVsIGlkPSJtY1RlYW1CIj48L3VsPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLW5vdGljZSIgaWQ9Im1jTG9iYnlJbmZvIj7QmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwLiDQntC20LjQtNCw0LXQvCDQt9Cw0L/Rg9GB0Log0LzQsNGC0YfQsCDQutCw0L/QuNGC0LDQvdC+0Lwg0LrQvtC80LDQvdC00YsgQS48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9Im1jU3RhcnRCdG4iIHN0eWxlPSJ3aWR0aDoxMDAlO21hcmdpbi10b3A6MTBweCI+4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNQbGF5aW5nUGFuZWwiIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLW5vdGljZSI+0JzQsNGC0Ycg0L/RgNC+0YXQvtC00LjRgiDQsiBTdGFuZG9mZiAyLiDQn9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINCy0L3QtdGB0Lgg0YHRh9GR0YIg0L7QsdC10LjRhSDQutC+0LzQsNC90LQg4oCUINGA0LXQt9GD0LvRjNGC0LDRgiDRg9C50LTRkdGCINC80L7QtNC10YDQsNGC0L7RgNGDINC90LAg0L/RgNC+0LLQtdGA0LrRgywgRUxPINC90LUg0LjQt9C80LXQvdC40YLRgdGPINC00L4g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPC9kaXY+PGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQTwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQjwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QotCS0J7QmCDQo9CR0JjQmdCh0KLQktCQIChLSUxMUyk8L2xhYmVsPjxpbnB1dCBpZD0ibWNLaWxscyIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwMCIgdmFsdWU9IjAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KLQktCe0Jgg0KHQnNCV0KDQotCYIChERUFUSFMpPC9sYWJlbD48aW5wdXQgaWQ9Im1jRGVhdGhzIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAwIiB2YWx1ZT0iMCI+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QmtCe0JzQnNCV0J3QotCQ0KDQmNCZINCaINCg0JXQl9Cj0JvQrNCi0JDQotCjPC9sYWJlbD48dGV4dGFyZWEgaWQ9Im1jUmVzdWx0Tm90ZSIgcm93cz0iMiIgcGxhY2Vob2xkZXI9ItCd0LDQv9GA0LjQvNC10YA6INGB0LrRgNC40L3RiNC+0YIg0YDQtdC30YPQu9GM0YLQsNGC0LAg0LHRg9C00LXRgiDQv9GA0LjQu9C+0LbQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDIj48L3RleHRhcmVhPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0ibWNTdWJtaXRSZXN1bHQiIHN0eWxlPSJ3aWR0aDoxMDAlIj7QntGC0L/RgNCw0LLQuNGC0Ywg0YDQtdC30YPQu9GM0YLQsNGCINC90LAg0L/RgNC+0LLQtdGA0LrRgzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNSZXZpZXdQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtbm90aWNlIiBpZD0ibWNSZXZpZXdJbmZvIj7QoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgyDQvNC+0LTQtdGA0LDRgtC+0YDRgy4g0KDQtdC50YLQuNC90LMg0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS48L2Rpdj48ZGl2IGlkPSJtY0FwcGVhbEZvcm0iIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0J/QoNCY0KfQmNCd0JAg0JDQn9CV0JvQm9Cv0KbQmNCYPC9sYWJlbD48c2VsZWN0IGlkPSJtY0FwcGVhbFJlYXNvbiI+PG9wdGlvbiB2YWx1ZT0id3Jvbmdfc2NvcmUiPtCd0LXQstC10YDQvdGL0Lkg0YHRh9GR0YI8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJjaGVhdGluZyI+0J/QvtC00L7Qt9GA0LXQvdC40LUg0L3QsCDQvdC10YfQtdGB0YLQvdGD0Y4g0LjQs9GA0YM8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJ0ZWNobmljYWxfaXNzdWUiPtCi0LXRhdC90LjRh9C10YHQutCw0Y8g0L/RgNC+0LHQu9C10LzQsDwvb3B0aW9uPjxvcHRpb24gdmFsdWU9Im90aGVyIj7QlNGA0YPQs9C+0LU8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCf0J7QlNCg0J7QkdCd0J7QodCi0Jg8L2xhYmVsPjx0ZXh0YXJlYSBpZD0ibWNBcHBlYWxEZXRhaWxzIiByb3dzPSIzIiBwbGFjZWhvbGRlcj0i0J7Qv9C40YjQuCDQv9GA0L7QsdC70LXQvNGDINC00LvRjyDQvNC+0LTQtdGA0LDRgtC+0YDQsCI+PC90ZXh0YXJlYT48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTdWJtaXRBcHBlYWwiIHN0eWxlPSJ3aWR0aDoxMDAlIj7Qn9C+0LTQsNGC0Ywg0LDQv9C10LvQu9GP0YbQuNGOPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J/RgNCw0LLQuNC70LAg0YDQtdC50YLQuNC90LPQsDwvaDM+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+RUxPPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7ihpc8L2Rpdj48ZGl2PjxiPtCf0L7QsdC10LbQtNCw0Lkg4oCUINC/0L7Qu9GD0YfQsNC5INGA0LXQudGC0LjQvdCzPC9iPjxwPtCg0LXQt9GD0LvRjNGC0LDRgiDQvNCw0YLRh9CwINCy0LvQuNGP0LXRgiDQvdCwINGC0LLQvtC5IEVMTy48L3A+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm90aWNlIj48ZGl2IGNsYXNzPSJub3RpY2VpY28iPuKcpjwvZGl2PjxkaXY+PGI+0JjQs9GA0LDQuSDRgNC10LPRg9C70Y/RgNC90L48L2I+PHA+0JfQsNGA0LDQsdCw0YLRi9Cy0LDQuSDQvtC/0YvRgiDQtNC70Y8g0YHQtdC30L7QvdC90L7Qs9C+IFBhc3MuPC9wPjwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JLRi9Cx0L7RgCDQutCw0YDRgtGLPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCf0KDQldCU0J/QntCn0KLQldCd0JjQlTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXBncmlkIiBpZD0ibWFwR3JpZCI+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSBzZWxlY3RlZCIgZGF0YS1tYXA9IlNhbmRzdG9uZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+c77iPPC9zcGFuPjwvZGl2PjxiPlNhbmRzdG9uZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUHJvdmluY2UiPjxkaXYgY2xhc3M9Im1hcGFydCI+PHNwYW4+8J+Pme+4jzwvc3Bhbj48L2Rpdj48Yj5Qcm92aW5jZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUnVzdCI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+tPC9zcGFuPjwvZGl2PjxiPlJ1c3Q8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IkJyZWV6ZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4yKPC9zcGFuPjwvZGl2PjxiPkJyZWV6ZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iWm9uZSA3Ij48ZGl2IGNsYXNzPSJtYXBhcnQiPjxzcGFuPuKYou+4jzwvc3Bhbj48L2Rpdj48Yj5ab25lIDc8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IlNha3VyYSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y4PC9zcGFuPjwvZGl2PjxiPlNha3VyYTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iRHVuZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7im7DvuI88L3NwYW4+PC9kaXY+PGI+RHVuZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iQ2FseXBzbyI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y0PC9zcGFuPjwvZGl2PjxiPkNhbHlwc288L2I+PC9idXR0b24+PC9kaXY+PC9kaXY+CiA8L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXJhdGluZyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0KDQtdC50YLQuNC90LMg0LjQs9GA0L7QutC+0LI8L2gyPjxwPtCU0L7QutCw0LbQuCDRgdCy0L7QuSDRg9GA0L7QstC10L3RjC4g0J/QvtC00L3QuNC80LDQudGB0Y8g0LIg0YLQsNCx0LvQuNGG0LUg0LvQuNC00LXRgNC+0LIuPC9wPjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNFQVNPTiAwMzwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYnMiIGlkPSJyYXRpbmdUYWJzIj48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdGFiPSJzZWFzb24iPtCh0LXQt9C+0L0gMDM8L2J1dHRvbj48YnV0dG9uIGRhdGEtdGFiPSJhbGwiPtCX0LAg0LLRgdGRINCy0YDQtdC80Y88L2J1dHRvbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYmxld3JhcCI+PHRhYmxlIGNsYXNzPSJ0YWJsZSI+PHRoZWFkPjx0cj48dGg+IzwvdGg+PHRoPtCY0JPQoNCe0Jo8L3RoPjx0aD5FTE88L3RoPjx0aD7Qn9Ce0JHQldCU0Ks8L3RoPjx0aD5XSU4gUkFURTwvdGg+PC90cj48L3RoZWFkPjx0Ym9keSBpZD0icmF0aW5nUm93cyI+PC90Ym9keT48L3RhYmxlPjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSI+0KLQsNCx0LvQuNGG0LAg0YHQvtC00LXRgNC20LjRgiDQtNC10LzQvtC90YHRgtGA0LDRhtC40L7QvdC90YvQtSDQtNCw0L3QvdGL0LUuINCf0L7QtNC60LvRjtGH0LggYmFja2VuZCwg0YfRgtC+0LHRiyDQvtGC0L7QsdGA0LDQttCw0YLRjCDRgNC10LDQu9GM0L3Ri9C5INGA0LXQudGC0LjQvdCzINC40LPRgNC+0LrQvtCyLjwvcD4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXBhc3MiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPlNUQU5EUE9JTlQgUGFzczwvaDI+PHA+0KHQtdC30L7QvSAwMyDCtyDQmNCz0YDQsNC5LCDQt9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQLCDQt9Cw0LHQuNGA0LDQuSDQvdCw0LPRgNCw0LTRiy48L3A+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+0JDQmtCi0JjQktCV0J08L3NwYW4+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQrdCb0JjQotCVPC9kaXY+PGgyIHN0eWxlPSJtYXJnaW46OHB4IDA7Zm9udC1zaXplOjI4cHgiPtCh0LXQt9C+0L3QvdGL0LUg0L3QsNCz0YDQsNC00Ys8L2gyPjxwPtCX0LDRgNCw0LHQsNGC0YvQstCw0Lkg0L7Qv9GL0YIg0LfQsCDQv9C+0LTRgtCy0LXRgNC20LTRkdC90L3Ri9C1INC80LDRgtGH0LguPC9wPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtbGV2ZWwiIHN0eWxlPSJmb250LXNpemU6NDZweCIgaWQ9InBhc3NMZXZlbEJpZyI+MDE8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJwcm9ncmVzcyI+PHNwYW4gaWQ9InBhc3NQcm9ncmVzc0JpZyI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtZm9vdCI+PHNwYW4gaWQ9InBhc3NYUEJpZyI+MTIwIC8gMzAwIFhQPC9zcGFuPjxzcGFuPtCj0YDQvtCy0LXQvdGMIDEg0LjQtyAzMDwvc3Bhbj48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QsNCz0YDQsNC00Ysg0YHQtdC30L7QvdCwPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPjMg0J/QoNCV0JTQnNCV0KLQkDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0cyI+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfjpbvuI88L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0J3QvtCy0L7QsdGA0LDQvdC10YY8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMSDCtyDQn9C+0LvRg9GH0LXQvdC+PC9zbWFsbD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGNsYWltIiBzdHlsZT0ibWFyZ2luLXRvcDoxMnB4O3dpZHRoOjEwMCU7Zm9udC1zaXplOjEwcHgiIGRhdGEtcmV3YXJkPSLQndC+0LLQvtCx0YDQsNC90LXRhiI+0J/QvtC70YPRh9C10L3QvjwvYnV0dG9uPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyN3B4Ij7wn5SlPC9kaXY+PHN0cm9uZyBzdHlsZT0iZm9udC1zaXplOjE1cHgiPtCd0LAg0L/QvtC00YrRkdC80LU8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgNTwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0J3QsCDQv9C+0LTRitGR0LzQtSI+0J/RgNC+0LLQtdGA0LjRgtGMPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfkZE8L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0K3Qu9C40YLQsDwvc3Ryb25nPjxzbWFsbD7Qo9GA0L7QstC10L3RjCAxMDwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0K3Qu9C40YLQsCI+0JfQsNCx0LvQvtC60LjRgNC+0LLQsNC90L48L2J1dHRvbj48L2Rpdj48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLWNsYW5zIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QmtC70LDQvdGLPC9oMj48cD7QodC+0LHQtdGA0Lgg0LrQvtC80LDQvdC00YMuINCX0LDRidC40YnQsNC5INGB0LLQvtC5INGC0LXQsy4g0J/QvtCx0LXQttC00LDQuSDQsiBDbGFuIFdhcnMuPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY3JlYXRlQ2xhbkJ0biI+77yLINCh0L7Qt9C00LDRgtGMINC60LvQsNC9PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjbGFuaGVybyI+PGRpdiBjbGFzcz0iY2xhbmxvZ28iPvCfkLo8L2Rpdj48ZGl2IGNsYXNzPSJncm93Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQmtCb0JDQnTwvZGl2PjxoMyBpZD0iY2xhbk5hbWUiPkZyb3N0IFdvbHZlcyA8c3BhbiBpZD0iY2xhblRhZyIgY2xhc3M9Im11dGVkIHNtYWxsIj4jRlc8L3NwYW4+PC9oMz48cD4xMiDRg9GH0LDRgdGC0L3QuNC60L7QsiDCtyAxOTgwIENsYW4gRUxPPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkiIGlkPSJjbGFuRGV0YWlscyI+0J/QvtC00YDQvtCx0L3QtdC1PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0KHQvtGB0YLQsNCyINC60LvQsNC90LA8L2gzPjxzcGFuIGNsYXNzPSJtdXRlZCBzbWFsbCI+MTIgLyAyMDwvc3Bhbj48L2Rpdj48ZGl2IGlkPSJjbGFuTWVtYmVycyIgY2xhc3M9ImxlYWRlcmxpc3QiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+Q2xhbiBXYXJzPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5MSVZFIExPQkJZPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWFyb3VuZDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE4cHggMCI+PGRpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MzVweCI+8J+boe+4jzwvZGl2PjxiIGlkPSJ3YXJPd25OYW1lIj7QotCS0J7QmSDQmtCb0JDQnTwvYj48L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MjFweDtjb2xvcjojZmY5ODRkIj5WUzwvc3Ryb25nPjxkaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjM1cHgiPuKalO+4jzwvZGl2PjxiPtCh0J7Qn9CV0KDQndCY0Jo8L2I+PC9kaXY+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIiBpZD0id2FyU3RhdHVzVGV4dCI+0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LrQu9Cw0L3RgyDQv9C+INC10LPQviBJRCDQuNC70Lgg0L7RgtC60YDQvtC5INC40YHRgtC+0YDQuNGOLjwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0id2FyQnV0dG9uIiBzdHlsZT0id2lkdGg6MTAwJSI+0J7RgtC60YDRi9GC0Ywg0LjRgdGC0L7RgNC40Y4gQ2xhbiBXYXJzPC9idXR0b24+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHdhci1sb2JieS1jYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4O2JvcmRlci1jb2xvcjojNTEzMjFmO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzMGRlZywjMWQxNzE0LCMxMTE2MWMpIj48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5DTEFOIFdBUiBPUEVSQVRJT05TPC9kaXY+PGgzIHN0eWxlPSJtYXJnaW4tdG9wOjVweCI+0JvQvtCx0LHQuCDQutC70LDQvdC+0LLQvtC5INCy0L7QudC90Ys8L2gzPjwvZGl2PjxzcGFuIGNsYXNzPSJtYy1zdGF0dXMiPjV2NSDCtyBCTzE8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntGC0L/RgNCw0LLRjCDQstGL0LfQvtCyINC00YDRg9Cz0L7QvNGDINC60LvQsNC90YMuINCf0L7RgdC70LUg0L/RgNC40L3Rj9GC0LjRjyDQv9C+0Y/QstC40YLRgdGPINC90LDRgdGC0L7Rj9GJ0LXQtSDQu9C+0LHQsdC4INGBINGB0L7RgdGC0LDQstCw0LzQuCDQuCDQvtGC0L/RgNCw0LLQutC+0Lkg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L3QsCDQvNC+0LTQtdGA0LDRhtC40Y4uPC9wPjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCa0JvQkNCdLdCh0J7Qn9CV0KDQndCY0Jo8L2xhYmVsPjxzZWxlY3QgaWQ9Indhck9wcG9uZW50SWQiPjxvcHRpb24gdmFsdWU9IiI+0JfQsNCz0YDRg9C20LDQtdC8INGB0L/QuNGB0L7QuiDQutC70LDQvdC+0LLigKY8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPklEINCi0JXQmtCj0KnQldCZINCS0J7QmdCd0Ks8L2xhYmVsPjxpbnB1dCBpZD0id2FySWRJbnB1dCIgcGxhY2Vob2xkZXI9ItCf0L7Rj9Cy0LjRgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LfQvtCy0LAiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY2hhbGxlbmdlV2FyQnRuIj7impQg0J7RgtC/0YDQsNCy0LjRgtGMINCy0YvQt9C+0LI8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibG9hZFdhckxvYmJ5QnRuIj7QntGC0LrRgNGL0YLRjCDQu9C+0LHQsdC4PC9idXR0b24+PC9kaXY+PGRpdiBpZD0id2FyTG9iYnlDb250ZW50IiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibWMtbXV0ZWQiPtCf0L7QutCwINC90LXRgiDQsNC60YLQuNCy0L3QvtCz0L4g0LvQvtCx0LHQuC4g0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LjQu9C4INCy0YHRgtCw0LLRjCBJRCDRgdGD0YnQtdGB0YLQstGD0Y7RidC10Lkg0LLQvtC50L3Riy48L3A+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIiBpZD0icGFnZS1mcmllbmRzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QlNGA0YPQt9GM0Y88L2gyPjxwPtCf0YDQuNCz0LvQsNGI0LDQuSDQuNCz0YDQvtC60L7QsiDQsiDQutC+0LzQsNC90LTRgyDQuCDQstC80LXRgdGC0LUg0L/QvtC00L3QuNC80LDQuSBFTE8uPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWRkRnJpZW5kQnRuIj7vvIsg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7Qn9Ce0JjQodCaINCf0J4g0J3QmNCa0J3QldCZ0JzQozwvbGFiZWw+PGlucHV0IGlkPSJmcmllbmRTZWFyY2giIHBsYWNlaG9sZGVyPSLQndCw0L/RgNC40LzQtdGALCBzaDF6byI+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImZyaWVuZHNMaXN0Ij48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLW5vdGlmaWNhdGlvbnMiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCj0LLQtdC00L7QvNC70LXQvdC40Y88L2gyPjxwPtCc0LDRgtGH0LgsINC/0YDQuNCz0LvQsNGI0LXQvdC40Y8g0Lgg0YHQvtCx0YvRgtC40Y8g0L/Qu9Cw0YLRhNC+0YDQvNGLLjwvcD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0icmVhZEFsbCI+0J/RgNC+0YfQuNGC0LDRgtGMINCy0YHQtTwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCIgaWQ9Im5vdGljZUxpc3QiPjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtcHJvZmlsZSI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwPC9oMj48cD7QotCy0L7RjyDRgdGC0LDRgtC40YHRgtC40LrQsCwg0YDQtdC50YLQuNC90LMg0Lgg0LjQs9GA0L7QstC+0Lkg0L/RgNC+0LPRgNC10YHRgS48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJwcm9maWxlaGVybyI+PGRpdiBjbGFzcz0icHJvZmlsZWF2YXRhciI+4pmfPC9kaXY+PGRpdj48aDMgaWQ9InByb2ZpbGVOYW1lIj5zdGFuZHBvaW50X3BsYXllcjwvaDM+PHAgaWQ9InByb2ZpbGVJZCI+SUQ6IGRlbW9fMDAxPC9wPjxzcGFuIGNsYXNzPSJwaWxsIiBpZD0icHJvZmlsZVN0YXR1cyI+4pePINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InByb2ZpbGVlbG8iPjxzbWFsbD7QotCV0JrQo9Cp0JjQmSBFTE88L3NtYWxsPjxzdHJvbmcgaWQ9InByb2ZpbGVFbG8iPjEwMDA8L3N0cm9uZz48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0JzQkNCi0KfQmDwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZU1hdGNoZXMiPjA8L3N0cm9uZz48c21hbGw+0JLRgdC10LPQviDRgdGL0LPRgNCw0L3Qvjwvc21hbGw+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGxhYmVsPtCf0J7QkdCV0JTQqzwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZVdpbnMiPjA8L3N0cm9uZz48c21hbGw+0J/QvtC00YLQstC10YDQttC00LXQvdC+PC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+Sy9EPC9sYWJlbD48c3Ryb25nIGlkPSJwcm9maWxlS0QiPuKAlDwvc3Ryb25nPjxzbWFsbD7Qo9Cx0LjQudGB0YLQstCwIC8g0YHQvNC10YDRgtC4PC9zbWFsbD48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JTQsNC90L3Ri9C1INC/0YDQvtGE0LjQu9GPPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCh0JXQoNCS0JXQoCAvINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCd0JjQmtCd0JXQmdCcPC9sYWJlbD48aW5wdXQgaWQ9ImVkaXROYW1lIiBtYXhsZW5ndGg9IjI0IiB2YWx1ZT0ic3RhbmRwb2ludF9wbGF5ZXIiPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JjQk9Cg0J7QktCe0JkgSUQ8L2xhYmVsPjxpbnB1dCBpZD0iZWRpdEdhbWVJZCIgbWF4bGVuZ3RoPSIzMiIgdmFsdWU9ImRlbW9fMDAxIj48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9InNhdmVQcm9maWxlIj7QodC+0YXRgNCw0L3QuNGC0Ywg0LjQt9C80LXQvdC10L3QuNGPPC9idXR0b24+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9GA0L7RhNC40LvRjCDRgdC+0YXRgNCw0L3Rj9C10YLRgdGPINC90LAg0YHQtdGA0LLQtdGA0LUsINC10YHQu9C4IEFQSSDQv9C+0LTQutC70Y7Rh9GR0L0g0LggVGVsZWdyYW0gSUQg0LTQvtGB0YLRg9C/0LXQvS48L3A+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCg0LDRgdGI0LjRgNC10L3QvdCw0Y8g0YHRgtCw0YLQuNGB0YLQuNC60LA8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBpZD0icmVmcmVzaEV4dHJhU3RhdHMiPtCe0LHQvdC+0LLQuNGC0Ywg4oa7PC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwtZ3JpZCI+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7Qn9Ce0JHQldCU0KsgJTwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFXaW5yYXRlIj7igJQ8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCf0J7QoNCQ0JbQldCd0JjQrzwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFMb3NzZXMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCd0JjQp9Cs0Jg8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhRHJhd3MiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCj0JHQmNCZ0KHQotCS0JA8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhS2lsbHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0JzQldCg0KLQmDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFEZWF0aHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0KDQldCU0J3QmNCZIEsvRDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFLRCI+4oCUPC9zdHJvbmc+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7QodCr0JPQoNCQ0J3QniDQnNCQ0KLQp9CV0Jk8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhTWF0Y2hlcyI+MDwvc3Ryb25nPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQtZGV0YWlsIj48c21hbGw+0KLQldCa0KPQqdCY0JkgRUxPPC9zbWFsbD48c3Ryb25nIGlkPSJleHRyYUVsbyI+MTAwMDwvc3Ryb25nPjwvZGl2PjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSIgaWQ9ImV4dHJhU3RhdHNOb3RlIj7QlNCw0L3QvdGL0LUg0LHRg9C00YPRgiDQt9Cw0LPRgNGD0LbQtdC90Ysg0LjQtyBBUEkg0L/QvtGB0LvQtSDQv9C+0LTQutC70Y7Rh9C10L3QuNGPINC/0YDQvtGE0LjQu9GPLjwvcD48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JrRg9Cx0LrQuCDQuCDQvdCw0LPRgNCw0LTRizwvaDM+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSIgaWQ9InRyb3BoeUNvdW50Ij4wINCd0JDQk9Cg0JDQlDwvc3Bhbj48L2Rpdj48cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCd0LDQs9GA0LDQtNGLINC30LAg0LrQstCw0LvQuNGE0LjQutCw0YbQuNC4INC4INC40YLQvtCz0L7QstGL0LUg0LzQtdGB0YLQsCDQsiDRgNC10LnRgtC40L3Qs9C1LiDQmtGD0LHQutC4INC+0YLQvtCx0YDQsNC20LDRjtGC0YHRjyDQv9C+0YHQu9C1INCy0YvQtNCw0YfQuCDRgNC10LfRg9C70YzRgtCw0YLQsCDQvtGA0LPQsNC90LjQt9Cw0YLQvtGA0L7QvC48L3A+PGRpdiBjbGFzcz0idHJvcGh5LWdyaWQiIGlkPSJwcm9maWxlVHJvcGhpZXMiPjxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7Qn9C+0LrQsCDQvdC10YIg0LrRg9Cx0LrQvtCyLiDQo9GH0LDRgdGC0LLRg9C5INCyINC60LLQsNC70LjRhNC40LrQsNGG0LjRj9GFLCDRh9GC0L7QsdGLINC/0L7Qu9GD0YfQuNGC0Ywg0L3QsNCz0YDQsNC00YMuPC9kaXY+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIiBpZD0icGFnZS1jb3NtZXRpY3MiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCc0LDQs9Cw0LfQuNC9INC60L7RgdC80LXRgtC40LrQuDwvaDI+PHA+0J7RhNC+0YDQvNC70LXQvdC40LUgU1RBTkRQT0lOVCBJRDog0YDQsNC80LrQuCwg0YLQuNGC0YPQu9GLINC4INC60LDRgNGC0L7Rh9C60Lgg0L/RgNC+0YTQuNC70Y8uPC9wPjwvZGl2PjxkaXYgY2xhc3M9ImJhZGdlLW9yYW5nZSI+U1AgUE9JTlRTOiA8c3BhbiBpZD0iY29zbWV0aWNQb2ludHMiPuKAlDwvc3Bhbj48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JrQsNGC0LDQu9C+0LM8L2gzPjxkaXYgY2xhc3M9InRhYnMiIHN0eWxlPSJtYXJnaW46MCI+PGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLWNvc21ldGljLWZpbHRlcj0iYWxsIj7QktGB0LU8L2J1dHRvbj48YnV0dG9uIGRhdGEtY29zbWV0aWMtZmlsdGVyPSJmcmFtZSI+0KDQsNC80LrQuDwvYnV0dG9uPjxidXR0b24gZGF0YS1jb3NtZXRpYy1maWx0ZXI9InRpdGxlIj7QotC40YLRg9C70Ys8L2J1dHRvbj48YnV0dG9uIGRhdGEtY29zbWV0aWMtZmlsdGVyPSJwcm9maWxlX2NhcmQiPtCa0LDRgNGC0L7Rh9C60Lg8L2J1dHRvbj48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1ncmlkIiBpZD0iY29zbWV0aWNTaG9wR3JpZCI+PGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCX0LDQs9GA0YPQttCw0LXQvCDQutCw0YLQsNC70L7QsyDQutC+0YHQvNC10YLQuNC60LjigKY8L2Rpdj48L2Rpdj48cCBjbGFzcz0iZm9vdG5vdGUiPtCf0L7QutGD0L/QutCwINC00L7RgdGC0YPQv9C90LAg0L/QvtGB0LvQtSDQv9C+0LTQutC70Y7Rh9C10L3QuNGPINCw0LLRgtC+0YDQuNC30L7QstCw0L3QvdC+0LPQviDQv9GA0L7RhNC40LvRjy4g0JLQsNC70Y7RgtCwINC4INC/0YDQtdC00LzQtdGC0Ysg0LHQtdGA0YPRgtGB0Y8g0LjQtyBiYWNrZW5kLCDQsCDQvdC1INC40Lcg0LTQtdC80L7QvdGB0YLRgNCw0YbQuNC+0L3QvdGL0YUg0LfQvdCw0YfQtdC90LjQuS48L3A+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCc0L7RjyDQutC+0LvQu9C10LrRhtC40Y88L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBpZD0icmVmcmVzaENvc21ldGljcyI+0J7QsdC90L7QstC40YLRjCDihrs8L2J1dHRvbj48L2Rpdj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1ncmlkIiBpZD0ibXlDb3NtZXRpY3NHcmlkIj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0JLQvtC50LTQuCDRh9C10YDQtdC3IFRlbGVncmFtLCDRh9GC0L7QsdGLINC30LDQs9GA0YPQt9C40YLRjCDQutC+0LvQu9C10LrRhtC40Y4uPC9kaXY+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KPC9tYWluPgo8L2Rpdj4KPG5hdiBjbGFzcz0iYm90dG9tbmF2IiBpZD0iYm90dG9tTmF2Ij48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtcGFnZT0iaG9tZSI+PHNwYW4+4oyCPC9zcGFuPtCT0LvQsNCy0L3QsNGPPC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9Im1hdGNoZXMiPjxzcGFuPuKalDwvc3Bhbj7QnNCw0YLRh9C4PC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9InJhdGluZyI+PHNwYW4+4pmcPC9zcGFuPtCg0LXQudGC0LjQvdCzPC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9ImNsYW5zIj48c3Bhbj7imaI8L3NwYW4+0JrQu9Cw0L3RizwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJwcm9maWxlIj48c3Bhbj7il4k8L3NwYW4+0J/RgNC+0YTQuNC70Yw8L2J1dHRvbj48L25hdj4KPGRpdiBpZD0idG9hc3RSb290IiBhcmlhLWxpdmU9InBvbGl0ZSI+PC9kaXY+CjxzY3JpcHQ+CmNvbnN0ICQ9KHMscj1kb2N1bWVudCk9PnIucXVlcnlTZWxlY3RvcihzKSwkJD0ocyxyPWRvY3VtZW50KT0+Wy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CmNvbnN0IGRlZmF1bHRzPXtuYW1lOiJzdGFuZHBvaW50X3BsYXllciIsZ2FtZUlkOiJkZW1vXzAwMSIsZWxvOjEwMDAsd2luczowLGxvc3NlczowLGRyYXdzOjAsa2lsbHM6MCxkZWF0aHM6MCx4cDoxMjAsZnJpZW5kczpbe25hbWU6InNoMXpvIixzdGF0dXM6ItCb0LXQs9C10L3QtNCwIixlbG86MjQxMH0se25hbWU6Incxbm5lciIsc3RhdHVzOiLQkiDQuNCz0YDQtSIsZWxvOjIzODd9LHtuYW1lOiJ6eGNydXNlZCIsc3RhdHVzOiLQndC1INCyINGB0LXRgtC4IixlbG86MjMzMn1dLG5vdGljZXM6W3tpY29uOiLinKYiLHRpdGxlOiLQlNC+0LHRgNC+INC/0L7QttCw0LvQvtCy0LDRgtGMINCyIFNUQU5EUE9JTlQiLGJvZHk6ItCi0LLQvtC5INGB0L7RgNC10LLQvdC+0LLQsNGC0LXQu9GM0L3Ri9C5INC/0YPRgtGMINC90LDRh9C40L3QsNC10YLRgdGPINC30LTQtdGB0YwuIixyZWFkOmZhbHNlfSx7aWNvbjoi4pqUIix0aXRsZToi0KDQtdC50YLQuNC90LPQvtCy0LDRjyDQvtGH0LXRgNC10LTRjCDQvtGC0LrRgNGL0YLQsCIsYm9keToi0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0L3QsNC50LTQuCDQvNCw0YLRhy4iLHJlYWQ6ZmFsc2V9LHtpY29uOiLwn46WIix0aXRsZToiU1RBTkRQT0lOVCBQYXNzINCw0LrRgtC40LLQtdC9Iixib2R5OiLQl9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQINC30LAg0LzQsNGC0YfQuCDQuCDQvtGC0LrRgNGL0LLQsNC5INC90LDQs9GA0LDQtNGLLiIscmVhZDp0cnVlfV19OwpsZXQgc3RhdGU7dHJ5e3N0YXRlPXsuLi5kZWZhdWx0cywuLi5KU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCJzdGFuZHBvaW50RGVtb1YyIil8fCJ7fSIpfTt9Y2F0Y2h7c3RhdGU9ey4uLmRlZmF1bHRzfTt9CmxldCBzZWxlY3RlZE1vZGU9IjV2NSIsc2VsZWN0ZWRNYXA9IlNhbmRzdG9uZSIscXVldWVUaW1lcj1udWxsLHF1ZXVlU2Vjb25kcz0wLHF1ZXVlQWN0aXZlPWZhbHNlOwpjb25zdCBsZWFkZXJzPVt7bmFtZToic2gxem8iLGVsbzoyNDEwLHdpbnM6MTUyLHdyOjc0fSx7bmFtZToidzFubmVyIixlbG86MjM4Nyx3aW5zOjEzOSx3cjo3MX0se25hbWU6Inp4Y3J1c2VkIixlbG86MjMzMix3aW5zOjEyOCx3cjo2OX0se25hbWU6ImRvbms2NjYiLGVsbzoyMjkxLHdpbnM6MTE3LHdyOjY4fSx7bmFtZToieW91MW5nIixlbG86MjI1Nix3aW5zOjExMSx3cjo2Nn1dOwpmdW5jdGlvbiBlc2Mocyl7cmV0dXJuIFN0cmluZyhzKS5yZXBsYWNlKC9bJjw+IiddL2csYz0+KHsiJiI6IiZhbXA7IiwiPCI6IiZsdDsiLCI+IjoiJmd0OyIsJyInOiImcXVvdDsiLCInIjoiJiMzOTsifVtjXSkpO30KZnVuY3Rpb24gc2F2ZSgpe2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJzdGFuZHBvaW50RGVtb1YyIixKU09OLnN0cmluZ2lmeShzdGF0ZSkpO30KZnVuY3Rpb24gdG9hc3Qocyl7Y29uc3Qgcm9vdD0kKCIjdG9hc3RSb290Iik7cm9vdC5pbm5lckhUTUw9IiI7Y29uc3QgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCJkaXYiKTtkLmNsYXNzTmFtZT0idG9hc3QiO2QudGV4dENvbnRlbnQ9cztyb290LmFwcGVuZChkKTtzZXRUaW1lb3V0KCgpPT5kLnJlbW92ZSgpLDMyMDApO30KZnVuY3Rpb24gbmF2KHBhZ2UpeyQkKCIuc2VjdGlvbiIpLmZvckVhY2gocz0+cy5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHMuaWQ9PT0icGFnZS0iK3BhZ2UpKTskJCgiW2RhdGEtcGFnZV0iKS5mb3JFYWNoKGI9PmIuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIixiLmRhdGFzZXQucGFnZT09PXBhZ2UpKTtjb25zdCB0PXtob21lOiLQotCy0L7RjyDQuNCz0YDQsC4g0KLQstC+0Y8g0L/QvtC30LjRhtC40Y8uIixtYXRjaGVzOiLQnNCw0YLRh9C80LXQudC60LjQvdCzIixyYXRpbmc6ItCg0LXQudGC0LjQvdCzIEVMTyIsY2xhbnM6ItCa0LvQsNC90Ysg0LggQ2xhbiBXYXJzIixwYXNzOiJTVEFORFBPSU5UIFBhc3MiLGZyaWVuZHM6ItCU0YDRg9C30YzRjyIsbm90aWZpY2F0aW9uczoi0KPQstC10LTQvtC80LvQtdC90LjRjyIscHJvZmlsZToi0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwIixjb3NtZXRpY3M6ItCc0LDQs9Cw0LfQuNC9INC60L7RgdC80LXRgtC40LrQuCJ9OyQoIiNwYWdlVGl0bGUiKS50ZXh0Q29udGVudD10W3BhZ2VdfHwiU1RBTkRQT0lOVCI7d2luZG93LnNjcm9sbFRvKHt0b3A6MCxiZWhhdmlvcjoic21vb3RoIn0pO30KJCQoIltkYXRhLXBhZ2VdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+bmF2KGIuZGF0YXNldC5wYWdlKSkpOwpmdW5jdGlvbiByZW5kZXJMZWFkZXJzKCl7Y29uc3QgYXJyPVsuLi5sZWFkZXJzLHtuYW1lOnN0YXRlLm5hbWUsZWxvOnN0YXRlLmVsbyx3aW5zOnN0YXRlLndpbnMsd3I6c3RhdGUud2lucytzdGF0ZS5sb3NzZXM/TWF0aC5yb3VuZChzdGF0ZS53aW5zLyhzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcykqMTAwKTowfV0uc29ydCgoYSxiKT0+Yi5lbG8tYS5lbG8pOyQoIiNob21lTGVhZGVycyIpLmlubmVySFRNTD1hcnIuc2xpY2UoMCw0KS5tYXAoKHAsaSk9PmA8ZGl2IGNsYXNzPSJsZWFkZXIiPjxzcGFuIGNsYXNzPSJyYW5rIj4ke2krMX08L3NwYW4+PGRpdiBjbGFzcz0icGxheWVyIj48ZGl2IGNsYXNzPSJtaW5pYXZhIj4ke2VzYyhwLm5hbWVbMF0udG9VcHBlckNhc2UoKSl9PC9kaXY+PGRpdj48Yj4ke2VzYyhwLm5hbWUpfTwvYj48c21hbGw+JHtpPT09MD8i0JvQtdCz0LXQvdC00LAiOmk9PT0xPyLQnNCw0YHRgtC10YAiOiLQmNCz0YDQvtC6In08L3NtYWxsPjwvZGl2PjwvZGl2PjxzcGFuIGNsYXNzPSJlbG8iPiR7cC5lbG99PC9zcGFuPjwvZGl2PmApLmpvaW4oIiIpOyQoIiNyYXRpbmdSb3dzIikuaW5uZXJIVE1MPWFyci5tYXAoKHAsaSk9PmA8dHIgY2xhc3M9IiR7cC5uYW1lPT09c3RhdGUubmFtZT8ieW91IjoiIn0iPjx0ZD4ke2krMX08L3RkPjx0ZD48Yj4ke2VzYyhwLm5hbWUpfTwvYj48L3RkPjx0ZD48Yj4ke3AuZWxvfTwvYj48L3RkPjx0ZD4ke3Aud2luc308L3RkPjx0ZD4ke3Aud3J9JTwvdGQ+PC90cj5gKS5qb2luKCIiKTt9CmZ1bmN0aW9uIHJlbmRlckZyaWVuZHMoKXtjb25zdCBxPSgkKCIjZnJpZW5kU2VhcmNoIik/LnZhbHVlfHwiIikudG9Mb3dlckNhc2UoKTskKCIjZnJpZW5kc0xpc3QiKS5pbm5lckhUTUw9c3RhdGUuZnJpZW5kcy5maWx0ZXIoZj0+Zi5uYW1lLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMocSkpLm1hcChmPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48ZGl2IGNsYXNzPSJwbGF5ZXIiPjxkaXYgY2xhc3M9Im1pbmlhdmEiPiR7ZXNjKGYubmFtZVswXS50b1VwcGVyQ2FzZSgpKX08L2Rpdj48ZGl2PjxiPiR7ZXNjKGYubmFtZSl9PC9iPjxzbWFsbD4ke2VzYyhmLnN0YXR1cyl9IMK3IEVMTyAke2YuZWxvfTwvc21hbGw+PC9kaXY+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBpbnZpdGUiIGRhdGEtbmFtZT0iJHtlc2MoZi5uYW1lKX0iPtCf0YDQuNCz0LvQsNGB0LjRgtGMPC9idXR0b24+PC9kaXY+YCkuam9pbigiIil8fCc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCY0LPRgNC+0LrQuCDQvdC1INC90LDQudC00LXQvdGLLjwvcD4nOyQkKCIuaW52aXRlIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+dG9hc3QoItCU0LXQvNC+OiDQv9GA0LjQs9C70LDRiNC10L3QuNC1INC00LvRjyAiK2IuZGF0YXNldC5uYW1lKyIg0L3QtSDQvtGC0L/RgNCw0LLQu9C10L3QviDQvdCwINGB0LXRgNCy0LXRgC4iKSkpO30KZnVuY3Rpb24gcmVuZGVyTm90aWNlcygpeyQoIiNub3RpY2VMaXN0IikuaW5uZXJIVE1MPXN0YXRlLm5vdGljZXMubWFwKChuLGkpPT5gPGRpdiBjbGFzcz0ibm90aWNlIiBzdHlsZT0iJHtuLnJlYWQ/Im9wYWNpdHk6LjgyIjoiIn0iPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+JHtlc2Mobi5pY29uKX08L2Rpdj48ZGl2IHN0eWxlPSJmbGV4OjEiPjxiPiR7ZXNjKG4udGl0bGUpfTwvYj48cD4ke2VzYyhuLmJvZHkpfTwvcD48c21hbGwgY2xhc3M9Im11dGVkIj4ke24ucmVhZD8i0J/RgNC+0YfQuNGC0LDQvdC+Ijoi0J3QvtCy0L7QtSJ9PC9zbWFsbD48L2Rpdj4ke24ucmVhZD8iIjpgPGJ1dHRvbiBjbGFzcz0idGV4dGxpbmsgcmVhZE5vdGljZSIgZGF0YS1pbmRleD0iJHtpfSI+0J/RgNC+0YfQuNGC0LDQvdC+PC9idXR0b24+YH08L2Rpdj5gKS5qb2luKCIiKTskJCgiLnJlYWROb3RpY2UiKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pntjb25zdCBuPXN0YXRlLm5vdGljZXNbK2IuZGF0YXNldC5pbmRleF07dHJ5e2lmKG4uaWQmJmFwaVVzZXJJZClhd2FpdCBhcGkoYC9ub3RpZmljYXRpb25zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KG4uaWQpfS9yZWFkYCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWR9fSk7bi5yZWFkPXRydWU7cmVuZGVyTm90aWNlcygpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtGC0LzQtdGC0LjRgtGMOiAiK2UubWVzc2FnZSk7fX0pKTt9CmZ1bmN0aW9uIHJlbmRlcigpe2NvbnN0IHRvdGFsPXN0YXRlLndpbnMrc3RhdGUubG9zc2VzK3N0YXRlLmRyYXdzLHdyPXRvdGFsP01hdGgucm91bmQoc3RhdGUud2lucy90b3RhbCoxMDApOjAsbGV2ZWw9TWF0aC5mbG9vcihzdGF0ZS54cC8zMDApKzEscGN0PShzdGF0ZS54cCUzMDApLzM7JCgiI3NpZGVOYW1lIikudGV4dENvbnRlbnQ9c3RhdGUubmFtZTskKCIjaG9tZUVsbyIpLnRleHRDb250ZW50PXN0YXRlLmVsbzskKCIjaG9tZVdpbnMiKS50ZXh0Q29udGVudD1zdGF0ZS53aW5zOyQoIiNob21lV1IiKS50ZXh0Q29udGVudD13cisiJSI7JCgiI3Byb2ZpbGVOYW1lIikudGV4dENvbnRlbnQ9c3RhdGUubmFtZTskKCIjcHJvZmlsZUlkIikudGV4dENvbnRlbnQ9IklEOiAiK3N0YXRlLmdhbWVJZCsiIjskKCIjcHJvZmlsZUVsbyIpLnRleHRDb250ZW50PXN0YXRlLmVsbzskKCIjcHJvZmlsZU1hdGNoZXMiKS50ZXh0Q29udGVudD10b3RhbDskKCIjcHJvZmlsZVdpbnMiKS50ZXh0Q29udGVudD1zdGF0ZS53aW5zOyQoIiNwcm9maWxlS0QiKS50ZXh0Q29udGVudD1zdGF0ZS5kZWF0aHM/KHN0YXRlLmtpbGxzL3N0YXRlLmRlYXRocykudG9GaXhlZCgyKTpzdGF0ZS5raWxscz8i4oieIjoi4oCUIjskKCIjZWRpdE5hbWUiKS52YWx1ZT1zdGF0ZS5uYW1lOyQoIiNlZGl0R2FtZUlkIikudmFsdWU9c3RhdGUuZ2FtZUlkOyQoIiNwYXNzTGV2ZWwiKS50ZXh0Q29udGVudD1TdHJpbmcobGV2ZWwpLnBhZFN0YXJ0KDIsIjAiKTskKCIjcGFzc0xldmVsQmlnIikudGV4dENvbnRlbnQ9U3RyaW5nKGxldmVsKS5wYWRTdGFydCgyLCIwIik7JCgiI3Bhc3NYUCIpLnRleHRDb250ZW50PShzdGF0ZS54cCUzMDApKyIgLyAzMDAgWFAiOyQoIiNwYXNzWFBCaWciKS50ZXh0Q29udGVudD0oc3RhdGUueHAlMzAwKSsiIC8gMzAwIFhQIjskKCIjcGFzc1Byb2dyZXNzIikuc3R5bGUud2lkdGg9cGN0KyIlIjskKCIjcGFzc1Byb2dyZXNzQmlnIikuc3R5bGUud2lkdGg9cGN0KyIlIjtyZW5kZXJMZWFkZXJzKCk7cmVuZGVyRnJpZW5kcygpO3JlbmRlck5vdGljZXMoKTtzYXZlKCk7fQokJCgiW2RhdGEtbW9kZV0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57aWYocXVldWVBY3RpdmUpe3RvYXN0KCLQodC90LDRh9Cw0LvQsCDQvtGC0LzQtdC90Lgg0YLQtdC60YPRidC40Lkg0L/QvtC40YHQuiwg0YfRgtC+0LHRiyDRgdC80LXQvdC40YLRjCDRgNC10LbQuNC8LiIpO3JldHVybjt9c2VsZWN0ZWRNb2RlPWIuZGF0YXNldC5tb2RlOyQkKCJbZGF0YS1tb2RlXSIpLmZvckVhY2goeD0+eC5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHg9PT1iKSk7fSkpOwokJCgiW2RhdGEtbWFwXSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnsKIHNlbGVjdGVkTWFwPWIuZGF0YXNldC5tYXA7JCQoIltkYXRhLW1hcF0iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgic2VsZWN0ZWQiLHg9PT1iKSk7CiB0b2FzdCgi0JrQsNGA0YLQsCDQstGL0LHRgNCw0L3QsDogIitzZWxlY3RlZE1hcCsoYWN0aXZlTWF0Y2hJZD8iIMK3INC+0YLQutGA0L7QuSDQvNCw0YLRhyDQtNC70Y8gYmFuLXBpY2siOiIiKSk7Cn0pKTsKJCgiI3F1ZXVlQnV0dG9uIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYXBpVXNlcklkKXt0b2FzdCgi0J7RgtC60YDQvtC5IFNUQU5EUE9JTlQg0YfQtdGA0LXQtyBUZWxlZ3JhbSDQuCDRgdC+0YXRgNCw0L3QuCDQv9GA0L7RhNC40LvRjC4iKTtuYXYoInByb2ZpbGUiKTtyZXR1cm47fQogdHJ5ewogIGlmKCFxdWV1ZUFjdGl2ZSl7CiAgIGNvbnN0IGQ9YXdhaXQgYXBpKCIvcXVldWUvam9pbiIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG1vZGU6c2VsZWN0ZWRNb2RlfX0pOwogICBpZihkLm1hdGNoX2lkKXthY3RpdmVNYXRjaElkPWQubWF0Y2hfaWQ7cXVldWVBY3RpdmU9ZmFsc2U7c3RvcFF1ZXVlUG9sbGluZygpO2F3YWl0IHNob3dNYXRjaChhY3RpdmVNYXRjaElkKTtyZXR1cm47fQogICBxdWV1ZUFjdGl2ZT1kLnN0YXR1cz09PSJxdWV1ZWQiOyQoIiNxdWV1ZUJ1dHRvbiIpLnRleHRDb250ZW50PXF1ZXVlQWN0aXZlPyLQntGC0LzQtdC90LjRgtGMINC/0L7QuNGB0LoiOiLQndCw0LnRgtC4INC80LDRgtGHIjsKICAgJCgiI3F1ZXVlVGl0bGUiKS50ZXh0Q29udGVudD0i0JjRidC10Lwg0YHQvtC/0LXRgNC90LjQutC+0LLigKYiOwogICAkKCIjcXVldWVEZXNjcmlwdGlvbiIpLnRleHRDb250ZW50PWDQkiDQvtGH0LXRgNC10LTQuCAke2QucGxheWVyc19mb3VuZH0vJHtkLnBsYXllcnNfbmVlZGVkfTsg0L7RgdGC0LDQu9C+0YHRjCAke2QucGxheWVyc19yZW1haW5pbmd9YDsKICAgdG9hc3QoItCf0L7QuNGB0Log0LfQsNC/0YPRidC10L0uINCh0YfRkdGC0YfQuNC6INGB0LjQvdGF0YDQvtC90LjQt9C40YDRg9C10YLRgdGPINGBINGB0LXRgNCy0LXRgNC+0LwuIik7c3RhcnRRdWV1ZVBvbGxpbmcoKTsKICB9ZWxzZXsKICAgYXdhaXQgYXBpKCIvcXVldWUvbGVhdmUiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZH19KTsKICAgcXVldWVBY3RpdmU9ZmFsc2U7c3RvcFF1ZXVlUG9sbGluZygpOyQoIiNxdWV1ZUJ1dHRvbiIpLnRleHRDb250ZW50PSLQndCw0LnRgtC4INC80LDRgtGHIjskKCIjcXVldWVUaXRsZSIpLnRleHRDb250ZW50PSLQn9C+0LjRgdC6INC+0YLQvNC10L3RkdC9IjskKCIjcXVldWVEZXNjcmlwdGlvbiIpLnRleHRDb250ZW50PSLQotGLINCy0YvRiNC10Lsg0LjQtyDQvtGH0LXRgNC10LTQuC4iO3RvYXN0KCLQn9C+0LjRgdC6INC+0YLQvNC10L3RkdC9LiIpOwogIH0KIH1jYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINC+0YfQtdGA0LXQtNC4OiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI3JhdGluZ1RhYnMiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsZT0+e2NvbnN0IGI9ZS50YXJnZXQuY2xvc2VzdCgiW2RhdGEtdGFiXSIpO2lmKCFiKXJldHVybjskJCgiW2RhdGEtdGFiXSIpLmZvckVhY2goeD0+eC5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHg9PT1iKSk7dG9hc3QoYi5kYXRhc2V0LnRhYj09PSJzZWFzb24iPyLQoNC10LnRgtC40L3QsyDRgdC10LfQvtC90LAgKNC00LXQvNC+KS4iOiLQntCx0YnQuNC5INGA0LXQudGC0LjQvdCzICjQtNC10LzQvikuIik7fSk7CiQkKCIuY2xhaW0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57aWYoYi5kYXRhc2V0LnJld2FyZD09PSLQndC+0LLQvtCx0YDQsNC90LXRhiIpe3RvYXN0KCLQrdGC0LAg0L3QsNCz0YDQsNC00LAg0YPQttC1INC/0L7Qu9GD0YfQtdC90LAuIik7cmV0dXJuO31jb25zdCBuZWVkPWIuZGF0YXNldC5yZXdhcmQ9PT0i0J3QsCDQv9C+0LTRitGR0LzQtSI/NToxMCxsZXZlbD1NYXRoLmZsb29yKHN0YXRlLnhwLzMwMCkrMTt0b2FzdChsZXZlbDxuZWVkPyLQndGD0LbQtdC9INGD0YDQvtCy0LXQvdGMICIrbmVlZCsiLiDQotCy0L7QuSDRg9GA0L7QstC10L3RjDogIitsZXZlbDoi0J3QsNCz0YDQsNC00Ysg0LHRg9C00YPRgiDQtNC+0YHRgtGD0L/QvdGLINC/0L7RgdC70LUg0L/QvtC00LrQu9GO0YfQtdC90LjRjyBQYXNzIEFQSS4iKTt9KSk7CiQoIiNzYXZlUHJvZmlsZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogY29uc3Qgbmlja25hbWU9JCgiI2VkaXROYW1lIikudmFsdWUudHJpbSgpLGdhbWVfaWQ9JCgiI2VkaXRHYW1lSWQiKS52YWx1ZS50cmltKCk7CiBpZighbmlja25hbWV8fCFnYW1lX2lkKXJldHVybiB0b2FzdCgi0JfQsNC/0L7Qu9C90Lgg0L3QuNC60L3QtdC50Lwg0Lgg0LjQs9GA0L7QstC+0LkgSUQuIik7CiBsZXQgaWQ9YXBpVXNlcklkfHxwcm9tcHQoItCS0LLQtdC00Lgg0YHQstC+0LkgVGVsZWdyYW0gSUQg0LTQu9GPINC/0YDQuNCy0Y/Qt9C60Lgg0LDQutC60LDRg9C90YLQsDoiKTsKIGlmKCFpZClyZXR1cm47YXBpVXNlcklkPVN0cmluZyhpZCkudHJpbSgpO2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJzdGFuZHBvaW50VGVsZWdyYW1JZCIsYXBpVXNlcklkKTsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaSgiL3BsYXllcnMvcmVnaXN0ZXIiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxuaWNrbmFtZSxnYW1lX2lkfX0pO2FwcGx5UGxheWVyKGQucGxheWVyKTthd2FpdCByZWZyZXNoQXBpRGF0YSgpO3RvYXN0KCLQn9GA0L7RhNC40LvRjCDRgdC+0YXRgNCw0L3RkdC9INC90LAg0YHQtdGA0LLQtdGA0LUuIik7fQogY2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDQv9GA0L7RhNC40LvRjzogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNmcmllbmRTZWFyY2giKS5hZGRFdmVudExpc3RlbmVyKCJpbnB1dCIscmVuZGVyRnJpZW5kcyk7CiQoIiNhZGRGcmllbmRCdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGNvbnN0IHRhcmdldF9pZD1wcm9tcHQoIlRlbGVncmFtIElEINC40LPRgNC+0LrQsCwg0LrQvtGC0L7RgNC+0LPQviDRhdC+0YfQtdGI0Ywg0LTQvtCx0LDQstC40YLRjDoiKTsKIGlmKCF0YXJnZXRfaWR8fCFhcGlVc2VySWQpcmV0dXJuIHRvYXN0KCLQndGD0LbQtdC9INGC0LLQvtC5IFRlbGVncmFtIElEINC4IElEINC00YDRg9Cz0LAuIik7CiB0cnl7YXdhaXQgYXBpKCIvZnJpZW5kcy9yZXF1ZXN0Iix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsdGFyZ2V0X2lkOnRhcmdldF9pZC50cmltKCl9fSk7dG9hc3QoItCX0LDRj9Cy0LrQsCDQvtGC0L/RgNCw0LLQu9C10L3QsC4iKTthd2FpdCByZWZyZXNoQXBpRGF0YSgpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0LfQsNGP0LLQutC4OiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI3JlYWRBbGwiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGlmKCFhcGlVc2VySWQpcmV0dXJuIHRvYXN0KCLQodC90LDRh9Cw0LvQsCDQv9C+0LTQutC70Y7Rh9C4INC/0YDQvtGE0LjQu9GMLiIpOwogdHJ5e2F3YWl0IGFwaShgL25vdGlmaWNhdGlvbnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX0vcmVhZC1hbGxgLHttZXRob2Q6IlBPU1QifSk7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt0b2FzdCgi0KPQstC10LTQvtC80LvQtdC90LjRjyDQvtGC0LzQtdGH0LXQvdGLINC/0YDQvtGH0LjRgtCw0L3QvdGL0LzQuC4iKTt9CiBjYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2NyZWF0ZUNsYW5CdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGlmKCFhcGlVc2VySWQpcmV0dXJuIHRvYXN0KCLQodC90LDRh9Cw0LvQsCDQv9C+0LTQutC70Y7Rh9C4INC/0YDQvtGE0LjQu9GMINCyIFRlbGVncmFtLiIpOwogY29uc3QgbmFtZT1wcm9tcHQoItCd0LDQt9Cy0LDQvdC40LUg0LrQu9Cw0L3QsCAoM+KAkzQwINGB0LjQvNCy0L7Qu9C+0LIpOiIpO2lmKCFuYW1lPy50cmltKCkpcmV0dXJuOwogY29uc3QgdGFnPXByb21wdCgi0KLQtdCzINC60LvQsNC90LAgKDLigJM4INC70LDRgtC40L3RgdC60LjRhSDQsdGD0LrQsi/RhtC40YTRgCk6Iik7aWYoIXRhZz8udHJpbSgpKXJldHVybjsKIGNvbnN0IGRlc2NyaXB0aW9uPXByb21wdCgi0J7Qv9C40YHQsNC90LjQtSDQutC70LDQvdCwICjQvdC10L7QsdGP0LfQsNGC0LXQu9GM0L3Qvik6Iil8fCIiOwogdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKCIvY2xhbnMiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxuYW1lOm5hbWUudHJpbSgpLHRhZzp0YWcudHJpbSgpLGRlc2NyaXB0aW9ufX0pO2FjdGl2ZUNsYW5JZD1kLmNsYW5faWQ7dG9hc3QoItCa0LvQsNC9INGB0L7Qt9C00LDQvSDQvdCwINGB0LXRgNCy0LXRgNC1LiIpO2F3YWl0IHJlZnJlc2hBcGlEYXRhKCk7fQogY2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDRgdC+0LfQtNCw0L3QuNGPINC60LvQsNC90LA6ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjY2xhbkRldGFpbHMiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGlmKCFhY3RpdmVDbGFuSWQpcmV0dXJuIHRvYXN0KCLQndC10YIg0LLRi9Cx0YDQsNC90L3QvtCz0L4g0LrQu9Cw0L3QsC4g0KHQvtC30LTQsNC5INC60LvQsNC9INC40LvQuCDQvtCx0L3QvtCy0Lgg0LTQsNC90L3Ri9C1LiIpOwogdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvY2xhbnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlQ2xhbklkKX1gKTthbGVydChgJHtkLmNsYW4ubmFtZX0gWyR7ZC5jbGFuLnRhZ31dIMK3IEVMTyAke2QuY2xhbi5lbG99XG5cbmArZC5tZW1iZXJzLm1hcChtPT5gJHttLm5pY2tuYW1lfSDigJQgJHttLnJvbGV9IOKAlCAke20uZWxvfSBFTE9gKS5qb2luKCJcbiIpKTt9CiBjYXRjaChlKXt0b2FzdChlLm1lc3NhZ2UpO30KfSk7CiQoIiN3YXJCdXR0b24iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGlmKCFhY3RpdmVDbGFuSWQpcmV0dXJuIHRvYXN0KCLQodC90LDRh9Cw0LvQsCDQstGL0LHQtdGA0Lgg0LjQu9C4INGB0L7Qt9C00LDQuSDQutC70LDQvS4iKTsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaShgL2NsYW4td2Fycy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVDbGFuSWQpfWApO2FsZXJ0KChkLndhcnN8fFtdKS5tYXAodz0+YCR7dy53YXJfaWR9IMK3ICR7dy5zdGF0dXN9IMK3ICR7dy5zY29yZV9hPz8iLSJ9OiR7dy5zY29yZV9iPz8iLSJ9YCkuam9pbigiXG4iKXx8ItCY0YHRgtC+0YDQuNGPIENsYW4gV2FycyDQv9GD0YHRgtCwLiIpO30KIGNhdGNoKGUpe3RvYXN0KCJDbGFuIFdhcnM6ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjY2xhbk1lbWJlcnMiKS5pbm5lckhUTUw9JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0JfQsNCz0YDRg9C30LrQsCDRgdC+0YHRgtCw0LLQsCDQutC70LDQvdCwINGBINGB0LXRgNCy0LXRgNCw4oCmPC9wPic7CgovLyA9PT09PSBTVEFORFBPSU5UIEFQSSA9PT09PQpjb25zdCBBUElfQkFTRT0iaHR0cHM6Ly9zdGFuZHBvaW50LWJhY2tlbmQub25yZW5kZXIuY29tIjsKbGV0IGFwaVVzZXJJZD0iIixhY3RpdmVDbGFuSWQ9IiIsYWN0aXZlTWF0Y2hJZD0iIixxdWV1ZVBvbGxUaW1lcj1udWxsOwpmdW5jdGlvbiBnZXRUZWxlZ3JhbVVzZXIoKXt0cnl7cmV0dXJuIHdpbmRvdy5UZWxlZ3JhbT8uV2ViQXBwPy5pbml0RGF0YVVuc2FmZT8udXNlcnx8bnVsbH1jYXRjaHtyZXR1cm4gbnVsbH19CmZ1bmN0aW9uIHNldEFwaVN0YXR1cyh0ZXh0LG9rPWZhbHNlKXtjb25zdCBlPSQoIiNhcGlTdGF0dXMiKTtpZihlKXtlLnRleHRDb250ZW50PXRleHQ7ZS5zdHlsZS5jb2xvcj1vaz8iIzc1ZjBiNiI6IiNmZmI0YTgiO319CmZ1bmN0aW9uIHJlYWRhYmxlQXBpRXJyb3IoZCxzdGF0dXMpewogY29uc3QgZGV0YWlsPWQ/LmRldGFpbHx8ZD8ubWVzc2FnZTsKIC8vINCd0LUg0LLRi9Cy0L7QtNC40Lwg0LIg0LjQvdGC0LXRgNGE0LXQudGBINC/0L7RgtC10L3RhtC40LDQu9GM0L3QviDRgdC70L7QvNCw0L3QvdGD0Y4g0LrQvtC00LjRgNC+0LLQutGDINC+0YLQstC10YLQsCDRgdC10YDQstC10YDQsC4KIGlmKHN0YXR1cz09PTQwNClyZXR1cm4gItC/0YDQvtGE0LjQu9GMINC40LvQuCDRgNC10YHRg9GA0YEg0L3QtSDQvdCw0LnQtNC10L0gKDQwNCkiOwogaWYoc3RhdHVzPT09NDIyKXJldHVybiAi0YHQtdGA0LLQtdGAINC+0YLQutC70L7QvdC40Lsg0LTQsNC90L3Ri9C1INC30LDQv9GA0L7RgdCwICg0MjIpIjsKIGlmKHN0YXR1cz09PTQwMXx8c3RhdHVzPT09NDAzKXJldHVybiAi0YHQtdGA0LLQtdGAINC90LUg0YDQsNC30YDQtdGI0LjQuyDQt9Cw0L/RgNC+0YEgKCIrc3RhdHVzKyIpIjsKIGlmKHN0YXR1cz49NTAwKXJldHVybiAi0LLQvdGD0YLRgNC10L3QvdGP0Y8g0L7RiNC40LHQutCwINGB0LXRgNCy0LXRgNCwICgiK3N0YXR1cysiKSI7CiBpZih0eXBlb2YgZGV0YWlsPT09InN0cmluZyIgJiYgIS9bw5DDkcODw6Ligqzvv71dLy50ZXN0KGRldGFpbCkpcmV0dXJuIGRldGFpbDsKIHJldHVybiAi0L7RiNC40LHQutCwINC30LDQv9GA0L7RgdCwIChIVFRQICIrc3RhdHVzKyIpIjsKfQphc3luYyBmdW5jdGlvbiBhcGkocGF0aCx7bWV0aG9kPSJHRVQiLGJvZHk9bnVsbH09e30pewogY29uc3Qgb3B0cz17bWV0aG9kLGhlYWRlcnM6eyJBY2NlcHQiOiJhcHBsaWNhdGlvbi9qc29uIn19OwogaWYoYm9keSE9PW51bGwpe29wdHMuaGVhZGVyc1siQ29udGVudC1UeXBlIl09ImFwcGxpY2F0aW9uL2pzb24iO29wdHMuYm9keT1KU09OLnN0cmluZ2lmeShib2R5KTt9CiBsZXQgcjt0cnl7cj1hd2FpdCBmZXRjaChBUElfQkFTRStwYXRoLG9wdHMpfWNhdGNoKGUpe3Rocm93IG5ldyBFcnJvcigi0YHQtdGA0LLQtdGAINC90LXQtNC+0YHRgtGD0L/QtdC9INC40LvQuCDQsdGA0LDRg9C30LXRgCDQt9Cw0LHQu9C+0LrQuNGA0L7QstCw0Lsg0LfQsNC/0YDQvtGBIChDT1JTL9GB0LXRgtGMKSIpO30KIGxldCBkPXt9O3RyeXtkPWF3YWl0IHIuanNvbigpfWNhdGNoe30KIGlmKCFyLm9rKXtjb25zdCBlcnI9bmV3IEVycm9yKHJlYWRhYmxlQXBpRXJyb3IoZCxyLnN0YXR1cykpO2Vyci5zdGF0dXM9ci5zdGF0dXM7ZXJyLmFwaVBhdGg9cGF0aDt0aHJvdyBlcnI7fQogcmV0dXJuIGQ7Cn0KZnVuY3Rpb24gYXBwbHlQbGF5ZXIocCl7aWYoIXApcmV0dXJuO2NvbnN0IHBzPSQoIiNwcm9maWxlU3RhdHVzIik7aWYocHMpcHMudGV4dENvbnRlbnQ9IuKXjyDQn9Cg0J7QpNCY0JvQrCDQoSDQodCV0KDQktCV0KDQkCI7c2V0QXBpU3RhdHVzKCJBUEkg0L7QvdC70LDQudC9IMK3INC/0YDQvtGE0LjQu9GMINC30LDQs9GA0YPQttC10L0iLHRydWUpO3N0YXRlLm5hbWU9cC5uaWNrbmFtZXx8c3RhdGUubmFtZTtzdGF0ZS5nYW1lSWQ9cC5nYW1lX2lkfHxzdGF0ZS5nYW1lSWQ7c3RhdGUuZWxvPU51bWJlcihwLmVsbz8/MTAwMCk7c3RhdGUud2lucz1OdW1iZXIocC53aW5zPz8wKTtzdGF0ZS5sb3NzZXM9TnVtYmVyKHAubG9zc2VzPz8wKTtzdGF0ZS5kcmF3cz1OdW1iZXIocC5kcmF3cz8/MCk7c3RhdGUua2lsbHM9TnVtYmVyKHAua2lsbHM/PzApO3N0YXRlLmRlYXRocz1OdW1iZXIocC5kZWF0aHM/PzApO3JlbmRlcigpO30KYXN5bmMgZnVuY3Rpb24gcmVmcmVzaEFwaURhdGEoKXsKIGlmKCFhcGlVc2VySWQpcmV0dXJuOwogY29uc3Qgam9icz1bCiAgYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApLnRoZW4oZD0+YXBwbHlQbGF5ZXIoZC5wbGF5ZXIpKSwKICBhcGkoIi9sZWFkZXJib2FyZD9saW1pdD01MCIpLnRoZW4oZD0+ewogICBjb25zdCBhPShkLnBsYXllcnN8fFtdKS5tYXAoeD0+KHtuYW1lOngubmlja25hbWUsZWxvOk51bWJlcih4LmVsb3x8MTAwMCksd2luczpOdW1iZXIoeC53aW5zfHwwKSx3cjooTnVtYmVyKHgud2luc3x8MCkrTnVtYmVyKHgubG9zc2VzfHwwKSk/TWF0aC5yb3VuZChOdW1iZXIoeC53aW5zfHwwKS8oTnVtYmVyKHgud2luc3x8MCkrTnVtYmVyKHgubG9zc2VzfHwwKSkqMTAwKTowfSkpOwogICBpZihhLmxlbmd0aCl7JCgiI2hvbWVMZWFkZXJzIikuaW5uZXJIVE1MPWEuc2xpY2UoMCw1KS5tYXAoKHgsaSk9PmA8ZGl2IGNsYXNzPSJsZWFkZXIiPjxzcGFuIGNsYXNzPSJyYW5rIj4ke2krMX08L3NwYW4+PGRpdiBjbGFzcz0icGxheWVyIj48ZGl2IGNsYXNzPSJtaW5pYXZhIj4ke2VzYygoeC5uYW1lfHwiPyIpWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MoeC5uYW1lKX08L2I+PHNtYWxsPiR7eC53aW5zfSDQv9C+0LHQtdC0PC9zbWFsbD48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0iZWxvIj4ke3guZWxvfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKTskKCIjcmF0aW5nUm93cyIpLmlubmVySFRNTD1hLm1hcCgoeCxpKT0+YDx0cj48dGQ+JHtpKzF9PC90ZD48dGQ+PGI+JHtlc2MoeC5uYW1lKX08L2I+PC90ZD48dGQ+PGI+JHt4LmVsb308L2I+PC90ZD48dGQ+JHt4LndpbnN9PC90ZD48dGQ+JHt4LndyfSU8L3RkPjwvdHI+YCkuam9pbigiIik7fQogIH0pLAogIGFwaShgL2ZyaWVuZHMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PntzdGF0ZS5mcmllbmRzPShkLmZyaWVuZHN8fFtdKS5tYXAoZj0+KHtuYW1lOmYubmlja25hbWUsdGVsZWdyYW1faWQ6Zi50ZWxlZ3JhbV9pZCxzdGF0dXM6ItCU0YDRg9CzIixlbG86TnVtYmVyKGYuZWxvfHwxMDAwKX0pKTtyZW5kZXJGcmllbmRzKCk7fSksCiAgYXBpKGAvbm90aWZpY2F0aW9ucy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApLnRoZW4oZD0+e3N0YXRlLm5vdGljZXM9KGQubm90aWZpY2F0aW9uc3x8W10pLm1hcChuPT4oe2lkOm4ubm90aWZpY2F0aW9uX2lkLGljb246bi5raW5kPT09Im1hdGNoX2ZvdW5kIj8i4pqUIjoi4pymIix0aXRsZTpuLnRpdGxlLGJvZHk6bi5ib2R5LHJlYWQ6ISFuLmlzX3JlYWR9KSk7cmVuZGVyTm90aWNlcygpO30pLAogIGFwaShgL3Bhc3MvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PntpZihkLmFjdGl2ZSE9PWZhbHNlKXtzdGF0ZS54cD1OdW1iZXIoZC54cHx8MCk7cmVuZGVyKCk7fX0pLAogIGFwaShgL3Jld2FyZHMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PnsKICAgY29uc3QgZWFybmVkPW5ldyBTZXQoKGQucmV3YXJkc3x8W10pLm1hcChyPT5yLm5hbWUpKTsKICAgJCQoIi5jbGFpbSIpLmZvckVhY2goYj0+e2NvbnN0IG5hbWU9Yi5kYXRhc2V0LnJld2FyZDtpZihlYXJuZWQuaGFzKG5hbWUpKXtiLnRleHRDb250ZW50PSLQn9C+0LvRg9GH0LXQvdC+IjtiLmRpc2FibGVkPXRydWU7fX0pOwogIH0pLAogIGFwaSgiL2NsYW5zP2xpbWl0PTUwIikudGhlbihhc3luYyBkPT57CiAgIGNvbnN0IGNsYW5zPWQuY2xhbnN8fFtdOwogICBjb25zdCBvcHBvbmVudFNlbGVjdD0kKCIjd2FyT3Bwb25lbnRJZCIpO2lmKG9wcG9uZW50U2VsZWN0KXtjb25zdCBvcHBvbmVudHM9Y2xhbnMuZmlsdGVyKHg9PnguY2xhbl9pZCE9PWFjdGl2ZUNsYW5JZCk7b3Bwb25lbnRTZWxlY3QuaW5uZXJIVE1MPSc8b3B0aW9uIHZhbHVlPSIiPtCS0YvQsdC10YDQuCDQutC70LDQvS3RgdC+0L/QtdGA0L3QuNC64oCmPC9vcHRpb24+JytvcHBvbmVudHMubWFwKHg9PmA8b3B0aW9uIHZhbHVlPSIke2VzYyh4LmNsYW5faWQpfSI+JHtlc2MoeC5uYW1lKX0gWyR7ZXNjKHgudGFnKX1dIMK3ICR7TnVtYmVyKHguZWxvfHwxMDAwKX0gRUxPPC9vcHRpb24+YCkuam9pbigiIik7fQogICBpZighY2xhbnMubGVuZ3RoKXskKCIjY2xhbk1lbWJlcnMiKS5pbm5lckhUTUw9JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0JrQu9Cw0L3QvtCyINC/0L7QutCwINC90LXRgi4g0KHQvtC30LTQsNC5INC/0LXRgNCy0YvQuSDQutC70LDQvS48L3A+JztyZXR1cm47fQogICBjb25zdCBtaW5lPWNsYW5zLmZpbmQoYz0+Yy5vd25lcl9pZD09PWFwaVVzZXJJZCk7Y29uc3QgYz1taW5lfHxjbGFuc1swXTthY3RpdmVDbGFuSWQ9Yy5jbGFuX2lkOwogICBpZihvcHBvbmVudFNlbGVjdCl7Y29uc3Qgb3Bwb25lbnRzPWNsYW5zLmZpbHRlcih4PT54LmNsYW5faWQhPT1hY3RpdmVDbGFuSWQpO29wcG9uZW50U2VsZWN0LmlubmVySFRNTD0nPG9wdGlvbiB2YWx1ZT0iIj7QktGL0LHQtdGA0Lgg0LrQu9Cw0L0t0YHQvtC/0LXRgNC90LjQuuKApjwvb3B0aW9uPicrb3Bwb25lbnRzLm1hcCh4PT5gPG9wdGlvbiB2YWx1ZT0iJHtlc2MoeC5jbGFuX2lkKX0iPiR7ZXNjKHgubmFtZSl9IFske2VzYyh4LnRhZyl9XSDCtyAke051bWJlcih4LmVsb3x8MTAwMCl9IEVMTzwvb3B0aW9uPmApLmpvaW4oIiIpO30KICAgY29uc3Qgb3duV2FyTmFtZT0kKCIjd2FyT3duTmFtZSIpO2lmKG93bldhck5hbWUpb3duV2FyTmFtZS50ZXh0Q29udGVudD1TdHJpbmcoYy50YWd8fGMubmFtZXx8ItCi0JLQntCZINCa0JvQkNCdIikudG9VcHBlckNhc2UoKTsKICAgJCgiI2NsYW5OYW1lIikuY2hpbGROb2Rlc1swXS50ZXh0Q29udGVudD1jLm5hbWUrIiAiOyQoIiNjbGFuVGFnIikudGV4dENvbnRlbnQ9IiMiK2MudGFnOwogICBjb25zdCBkZXRhaWw9YXdhaXQgYXBpKGAvY2xhbnMvJHtlbmNvZGVVUklDb21wb25lbnQoYy5jbGFuX2lkKX1gKSxtZW1iZXJzPWRldGFpbC5tZW1iZXJzfHxbXTsKICAgJCgiI2NsYW5NZW1iZXJzIikuaW5uZXJIVE1MPW1lbWJlcnMubWFwKG09PmA8ZGl2IGNsYXNzPSJsZWFkZXIiPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MoKG0ubmlja25hbWV8fCI/IilbMF0udG9VcHBlckNhc2UoKSl9PC9kaXY+PGRpdj48Yj4ke2VzYyhtLm5pY2tuYW1lfHxtLnRlbGVncmFtX2lkKX08L2I+PHNtYWxsPiR7ZXNjKG0ucm9sZSl9PC9zbWFsbD48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0iZWxvIj4ke051bWJlcihtLmVsb3x8MTAwMCl9PC9zcGFuPjwvZGl2PmApLmpvaW4oIiIpfHwnPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QkiDQutC70LDQvdC1INC/0L7QutCwINC90LXRgiDRg9GH0LDRgdGC0L3QuNC60L7Qsi48L3A+JzsKICAgY29uc3QgY291bnQ9JCgiLmNhcmRoZWFkIC5tdXRlZC5zbWFsbCIsJCgiI3BhZ2UtY2xhbnMiKSk7aWYoY291bnQpY291bnQudGV4dENvbnRlbnQ9YCR7bWVtYmVycy5sZW5ndGh9IC8gMjBgOwogIH0pCiBdOwogY29uc3QgcmVzdWx0cz1hd2FpdCBQcm9taXNlLmFsbFNldHRsZWQoam9icyk7CiBjb25zdCBlcnJvcnM9cmVzdWx0cy5maWx0ZXIoeD0+eC5zdGF0dXM9PT0icmVqZWN0ZWQiKTsKIGlmKGVycm9ycy5sZW5ndGgpY29uc29sZS53YXJuKCLQndC10LrQvtGC0L7RgNGL0LUg0YDQsNC30LTQtdC70YsgQVBJINC90LUg0LfQsNCz0YDRg9C30LjQu9C40YHRjDoiLGVycm9ycy5tYXAoeD0+eC5yZWFzb24/Lm1lc3NhZ2UpKTsKIHJlbmRlcigpOwp9CmFzeW5jIGZ1bmN0aW9uIGluaXRBcGkoKXsKIGNvbnN0IHRnPWdldFRlbGVncmFtVXNlcigpO2FwaVVzZXJJZD1TdHJpbmcodGc/LmlkfHxsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgic3RhbmRwb2ludFRlbGVncmFtSWQiKXx8IiIpLnRyaW0oKTsKIGlmKHRnPy5pZCl7bG9jYWxTdG9yYWdlLnNldEl0ZW0oInN0YW5kcG9pbnRUZWxlZ3JhbUlkIixhcGlVc2VySWQpO2lmKHRnLnVzZXJuYW1lKXN0YXRlLm5hbWU9dGcudXNlcm5hbWU7fQogLy8g0J/RgNC+0LLQtdGA0Y/QtdC8INC00L7RgdGC0YPQv9C90L7RgdGC0Ywg0YHQtdGA0LLQtdGA0LAg0L7RgtC00LXQu9GM0L3QviDQvtGCINGA0LXQs9C40YHRgtGA0LDRhtC40Lgg0L/RgNC+0YTQuNC70Y8uCiB0cnl7CiAgYXdhaXQgYXBpKCIvY29uZmlnIik7CiAgc2V0QXBpU3RhdHVzKCJBUEk6INGB0LXRgNCy0LXRgCDQvtC90LvQsNC50L0iLHRydWUpOwogIGNvbnN0IHFuPSQoIiNxdWV1ZUFwaU5vdGUiKTtpZihxbilxbi50ZXh0Q29udGVudD0i0KHQtdGA0LLQtdGAINC00L7RgdGC0YPQv9C10L0uINCU0LvRjyDQv9C+0LjRgdC60LAg0LzQsNGC0YfQsCDQvdGD0LbQtdC9INC/0YDQvtGE0LjQu9GMIFRlbGVncmFtINC4INGD0YHQv9C10YjQvdCw0Y8g0YDQtdCz0LjRgdGC0YDQsNGG0LjRjy4iOwogfWNhdGNoKGUpe3NldEFwaVN0YXR1cygiQVBJOiDQvtGI0LjQsdC60LAg0YHQvtC10LTQuNC90LXQvdC40Y8iLGZhbHNlKTtjb25zb2xlLmVycm9yKCJBUEkgY29uZmlnOiIsZSk7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L/QvtC00LrQu9GO0YfQuNGC0YzRgdGPINC6INGB0LXRgNCy0LXRgNGDOiAiK2UubWVzc2FnZSk7cmV0dXJuO30KIGlmKCFhcGlVc2VySWQpe3NldEFwaVN0YXR1cygiQVBJOiDQvtGC0LrRgNC+0Lkg0LIgVGVsZWdyYW0iLGZhbHNlKTtjb25zdCBxbj0kKCIjcXVldWVBcGlOb3RlIik7aWYocW4pcW4udGV4dENvbnRlbnQ9ItCe0YLQutGA0L7QuSDQvNC40L3QuC3Qv9GA0LjQu9C+0LbQtdC90LjQtSDRh9C10YDQtdC3IFRlbGVncmFtLCDRh9GC0L7QsdGLINC/0L7Qu9GD0YfQuNGC0YwgVGVsZWdyYW0gSUQg0Lgg0LjRgdC60LDRgtGMINC80LDRgtGHLiI7cmV0dXJuO30KIHRyeXsKICB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCk7YXBwbHlQbGF5ZXIoZC5wbGF5ZXJ8fGQpO30KICBjYXRjaChlKXsKICAgaWYoZS5zdGF0dXMhPT00MDQgJiYgIS9IVFRQIDQwNHzQvdC1INC90LDQudC00LXQvS9pLnRlc3QoZS5tZXNzYWdlKSl0aHJvdyBlOwogICBjb25zdCBuaWNrbmFtZT0odGc/LnVzZXJuYW1lfHxzdGF0ZS5uYW1lfHwi0JjQs9GA0L7QuiIpLnNsaWNlKDAsMjQpLGdhbWVfaWQ9U3RyaW5nKHN0YXRlLmdhbWVJZHx8KCJzcF8iK2FwaVVzZXJJZC5zbGljZSgtOCkpKS5zbGljZSgwLDMyKTsKICAgY29uc3QgZD1hd2FpdCBhcGkoIi9wbGF5ZXJzL3JlZ2lzdGVyIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbmlja25hbWUsZ2FtZV9pZH19KTthcHBseVBsYXllcihkLnBsYXllcnx8ZCk7CiAgfQogIGF3YWl0IHJlZnJlc2hBcGlEYXRhKCk7CiB9Y2F0Y2goZSl7CiAgLy8g0KHQtdGA0LLQtdGAINGD0LbQtSDQv9C+0LTRgtCy0LXRgNC00LjQuyDQtNC+0YHRgtGD0L/QvdC+0YHRgtGMINGH0LXRgNC10LcgL2NvbmZpZyDigJQg0L3QtSDQvdCw0LfRi9Cy0LDQtdC8INGN0YLQviDQvtCx0YnQtdC5INC+0YjQuNCx0LrQvtC5IEFQSS4KICBzZXRBcGlTdGF0dXMoIkFQSSDQvtC90LvQsNC50L0gwrcg0L7RiNC40LHQutCwINC/0YDQvtGE0LjQu9GPIixmYWxzZSk7Y29uc29sZS5lcnJvcigiUHJvZmlsZSBpbml0OiIsZSk7CiAgY29uc3QgcW49JCgiI3F1ZXVlQXBpTm90ZSIpO2lmKHFuKXFuLnRleHRDb250ZW50PSLQodC10YDQstC10YAg0LTQvtGB0YLRg9C/0LXQvSwg0L3QviDQvdC1INGD0LTQsNC70L7RgdGMINC30LDQs9GA0YPQt9C40YLRjCDQv9GA0L7RhNC40LvRjC4g0J7RgtC60YDQvtC5IMKr0J/RgNC+0YTQuNC70YzCuywg0L/RgNC+0LLQtdGA0Ywg0L3QuNC60L3QtdC50Lwg0Lgg0LjQs9GA0L7QstC+0LkgSUQg0Lgg0YHQvtGF0YDQsNC90Lgg0LXRidGRINGA0LDQty4iOwogIHRvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC30LDQs9GA0YPQt9C40YLRjCDQv9GA0L7RhNC40LvRjDogIitlLm1lc3NhZ2UpOwogfQp9CgoKLy8gPT09PT0gQ2xhbiBXYXJzOiBBUEktYmFja2VkIGNoYWxsZW5nZSBhbmQgbG9iYnkgPT09PT0KbGV0IGFjdGl2ZVdhcklkPSIiOwphc3luYyBmdW5jdGlvbiBsb2FkV2FyTG9iYnkoaWQpe2lmKCFpZHx8IWFwaVVzZXJJZClyZXR1cm4gdG9hc3QoItCd0YPQttC10L0gSUQgQ2xhbiBXYXIg0Lgg0L/QvtC00LrQu9GO0YfRkdC90L3Ri9C5INC/0YDQvtGE0LjQu9GMLiIpO3RyeXtjb25zdCBkPWF3YWl0IGFwaShgL2NsYW4td2Fycy8ke2VuY29kZVVSSUNvbXBvbmVudChpZCl9L2xvYmJ5YCk7YWN0aXZlV2FySWQ9aWQ7JCgiI3dhcklkSW5wdXQiKS52YWx1ZT1pZDtjb25zdCB3PWQud2FyLHRlYW1BPWQudGVhbXM/LkF8fFtdLHRlYW1CPWQudGVhbXM/LkJ8fFtdOyQoIiN3YXJMb2JieUNvbnRlbnQiKS5pbm5lckhUTUw9YDxkaXYgY2xhc3M9Im1jLXR3byI+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCfn6AgJHtlc2Mody5jbGFuX2FfbmFtZSl9ICMke2VzYyh3LmNsYW5fYV90YWcpfTwvaDQ+PHVsPiR7dGVhbUEubWFwKHA9PmA8bGk+JHtlc2MocC5uaWNrbmFtZSl9IMK3ICR7TnVtYmVyKHAuZWxvKX0gRUxPPC9saT5gKS5qb2luKCIiKXx8IjxsaT7QodC+0YHRgtCw0LIg0L3QtSDQt9Cw0LPRgNGD0LbQtdC9PC9saT4ifTwvdWw+PC9kaXY+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCflLUgJHtlc2Mody5jbGFuX2JfbmFtZSl9ICMke2VzYyh3LmNsYW5fYl90YWcpfTwvaDQ+PHVsPiR7dGVhbUIubWFwKHA9PmA8bGk+JHtlc2MocC5uaWNrbmFtZSl9IMK3ICR7TnVtYmVyKHAuZWxvKX0gRUxPPC9saT5gKS5qb2luKCIiKXx8IjxsaT7QodC+0YHRgtCw0LIg0L3QtSDQt9Cw0LPRgNGD0LbQtdC9PC9saT4ifTwvdWw+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibWMtbm90aWNlIiBzdHlsZT0ibWFyZ2luLXRvcDoxMHB4Ij7QodGC0LDRgtGD0YE6IDxiPiR7ZXNjKHcuc3RhdHVzKX08L2I+IMK3IElEICR7ZXNjKHcud2FyX2lkKX0ke3cuc3RhdHVzPT09ImFjY2VwdGVkIj8iIMK3INCS0YvQt9C+0LIg0L/RgNC40L3Rj9GCLCDQvNC+0LbQvdC+INC/0YDQvtCy0LXRgdGC0Lgg0YHQtdGA0LjRjiDQuCDQvtGC0L/RgNCw0LLQuNGC0Ywg0YHRh9GR0YIuIjoiIn08L2Rpdj4ke3cuc3RhdHVzPT09InBlbmRpbmciP2A8ZGl2IGNsYXNzPSJtYy1hY3Rpb25zIiBzdHlsZT0ibWFyZ2luLXRvcDoxMHB4Ij48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9ImFjY2VwdFdhckJ0biI+0J/RgNC40L3Rj9GC0Ywg0LLRi9C30L7QsjwvYnV0dG9uPjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkiIGlkPSJyZWplY3RXYXJCdG4iPtCe0YLQutC70L7QvdC40YLRjDwvYnV0dG9uPjwvZGl2PmA6IiJ9JHt3LnN0YXR1cz09PSJhY2NlcHRlZCI/YDxkaXYgY2xhc3M9Im1jLWZvcm1ncmlkIiBzdHlsZT0ibWFyZ2luLXRvcDoxMHB4Ij48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCh0KfQgdCiINCa0JvQkNCd0JAgQTwvbGFiZWw+PGlucHV0IGlkPSJ3YXJTY29yZUEiIHR5cGU9Im51bWJlciIgbWluPSIwIiBtYXg9IjEwMCIgdmFsdWU9IjEzIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCh0KfQgdCiINCa0JvQkNCd0JAgQjwvbGFiZWw+PGlucHV0IGlkPSJ3YXJTY29yZUIiIHR5cGU9Im51bWJlciIgbWluPSIwIiBtYXg9IjEwMCIgdmFsdWU9IjkiPjwvZGl2PjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0ic3VibWl0V2FyU2NvcmVCdG4iIHN0eWxlPSJ3aWR0aDoxMDAlIj7QntGC0L/RgNCw0LLQuNGC0Ywg0YDQtdC30YPQu9GM0YLQsNGCINC80L7QtNC10YDQsNGC0L7RgNGDPC9idXR0b24+YDoiIn1gO2NvbnN0IGFjY2VwdD0kKCIjYWNjZXB0V2FyQnRuIik7aWYoYWNjZXB0KWFjY2VwdC5vbmNsaWNrPSgpPT5kZWNpZGVXYXIoImFjY2VwdCIpO2NvbnN0IHJlamVjdD0kKCIjcmVqZWN0V2FyQnRuIik7aWYocmVqZWN0KXJlamVjdC5vbmNsaWNrPSgpPT5kZWNpZGVXYXIoInJlamVjdCIpO2NvbnN0IHN1Ym1pdD0kKCIjc3VibWl0V2FyU2NvcmVCdG4iKTtpZihzdWJtaXQpc3VibWl0Lm9uY2xpY2s9YXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlV2FySWQpfS9yZXN1bHRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxzY29yZV9hOk51bWJlcigkKCIjd2FyU2NvcmVBIikudmFsdWUpLHNjb3JlX2I6TnVtYmVyKCQoIiN3YXJTY29yZUIiKS52YWx1ZSl9fSk7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgiBDbGFuIFdhciDQvtGC0L/RgNCw0LLQu9C10L0g0L3QsCDQv9GA0L7QstC10YDQutGDLiIpO2F3YWl0IGxvYWRXYXJMb2JieShhY3RpdmVXYXJJZCk7fWNhdGNoKGUpe3RvYXN0KCLQoNC10LfRg9C70YzRgtCw0YIgQ2xhbiBXYXI6ICIrZS5tZXNzYWdlKTt9fTskKCIjd2FyU3RhdHVzVGV4dCIpLnRleHRDb250ZW50PWBDbGFuIFdhciAke3cud2FyX2lkfTogJHt3LnN0YXR1c31gO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtGC0LrRgNGL0YLRjCDQu9C+0LHQsdC4IENsYW4gV2FyOiAiK2UubWVzc2FnZSk7fX0KYXN5bmMgZnVuY3Rpb24gZGVjaWRlV2FyKGRlY2lzaW9uKXt0cnl7YXdhaXQgYXBpKGAvY2xhbi13YXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZVdhcklkKX0vZGVjaXNpb25gLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxkZWNpc2lvbn19KTt0b2FzdChkZWNpc2lvbj09PSJhY2NlcHQiPyLQktGL0LfQvtCyINC/0YDQuNC90Y/Rgi4iOiLQktGL0LfQvtCyINC+0YLQutC70L7QvdGR0L0uIik7YXdhaXQgbG9hZFdhckxvYmJ5KGFjdGl2ZVdhcklkKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L7QsdGA0LDQsdC+0YLQsNGC0Ywg0LLRi9C30L7QsjogIitlLm1lc3NhZ2UpO319CiQoIiNjaGFsbGVuZ2VXYXJCdG4iKT8uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pntjb25zdCBvcHBvbmVudF9jbGFuX2lkPSQoIiN3YXJPcHBvbmVudElkIikudmFsdWUudHJpbSgpO2lmKCFhcGlVc2VySWR8fCFvcHBvbmVudF9jbGFuX2lkKXJldHVybiB0b2FzdCgi0KPQutCw0LbQuCBJRCDQutC70LDQvdCwINGB0L7Qv9C10YDQvdC40LrQsCDQuCDQv9C+0LTQutC70Y7Rh9C4INC/0YDQvtGE0LjQu9GMLiIpO3RyeXtjb25zdCBkPWF3YWl0IGFwaSgiL2NsYW4td2FycyIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG9wcG9uZW50X2NsYW5faWR9fSk7YWN0aXZlV2FySWQ9ZC53YXJfaWQ7JCgiI3dhcklkSW5wdXQiKS52YWx1ZT1kLndhcl9pZDt0b2FzdCgi0JLRi9C30L7QsiBDbGFuIFdhciDQvtGC0L/RgNCw0LLQu9C10L0uIik7YXdhaXQgbG9hZFdhckxvYmJ5KGQud2FyX2lkKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0YHQvtC30LTQsNGC0YwgQ2xhbiBXYXI6ICIrZS5tZXNzYWdlKTt9fSk7CiQoIiNsb2FkV2FyTG9iYnlCdG4iKT8uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5sb2FkV2FyTG9iYnkoJCgiI3dhcklkSW5wdXQiKS52YWx1ZS50cmltKCkpKTsKCi8vID09PT09IE1hdGNoIENlbnRlcjogYWNjZXB0IC0+IGNhcHRhaW4gZHJhZnQgLT4gYmFuL3BpY2sgLT4gbG9iYnkgLT4gcmVzdWx0IC0+IGFwcGVhbCA9PT09PQpsZXQgbWF0Y2hQb2xsVGltZXI9bnVsbCxjdXJyZW50TWF0Y2g9bnVsbDsKY29uc3QgTUFUQ0hfTUFQUz1bIkJyZWV6ZSIsIkNhbHlwc28iLCJEdW5lIiwiUHJvdmluY2UiLCJSdXN0IiwiU2FrdXJhIiwiU2FuZHN0b25lIiwiWm9uZSA3Il07CmZ1bmN0aW9uIHN0b3BRdWV1ZVBvbGxpbmcoKXtpZihxdWV1ZVBvbGxUaW1lcil7Y2xlYXJJbnRlcnZhbChxdWV1ZVBvbGxUaW1lcik7cXVldWVQb2xsVGltZXI9bnVsbDt9fQpmdW5jdGlvbiBzdG9wTWF0Y2hQb2xsaW5nKCl7aWYobWF0Y2hQb2xsVGltZXIpe2NsZWFySW50ZXJ2YWwobWF0Y2hQb2xsVGltZXIpO21hdGNoUG9sbFRpbWVyPW51bGw7fX0KZnVuY3Rpb24gbWNTaG93KGlkKXtbIm1jQWNjZXB0UGFuZWwiLCJtY0JhblBhbmVsIiwibWNMb2JieVBhbmVsIiwibWNQbGF5aW5nUGFuZWwiLCJtY1Jldmlld1BhbmVsIl0uZm9yRWFjaCh4PT4kKCIjIit4KS5jbGFzc0xpc3QudG9nZ2xlKCJtYy1oaWRkZW4iLHghPT1pZCkpOyQoIiNtYXRjaENlbnRlciIpLmNsYXNzTGlzdC5hZGQoInZpc2libGUiKTt9CmZ1bmN0aW9uIG1hdGNoUGxheWVyTmFtZShpZCl7cmV0dXJuIChjdXJyZW50TWF0Y2g/LnBsYXllcl9wcm9maWxlcz8uW2lkXT8ubmlja25hbWUpfHwoItCY0LPRgNC+0LogIitTdHJpbmcoaWR8fCIiKS5zbGljZSgtNCkpO30KZnVuY3Rpb24gcmVuZGVyTWNQbGF5ZXJzKCl7Y29uc3QgcGxheWVycz1jdXJyZW50TWF0Y2g/LnBsYXllcnN8fFtdLGFjY2VwdGFuY2U9Y3VycmVudE1hdGNoPy5hY2NlcHRhbmNlfHx7fTskKCIjbWNBY2NlcHRQbGF5ZXJzIikuaW5uZXJIVE1MPXBsYXllcnMubWFwKChpZCxpKT0+e2NvbnN0IGFjY2VwdGVkPSEhYWNjZXB0YW5jZVtpZF07cmV0dXJuIGA8ZGl2IGNsYXNzPSJtYy1wbGF5ZXIgJHthY2NlcHRlZD8iYWNjZXB0ZWQiOiIifSI+PGRpdiBjbGFzcz0ibWMtYXZhdGFyIj4ke2FjY2VwdGVkPyLinJMiOmkrMX08L2Rpdj48Yj4ke2VzYyhtYXRjaFBsYXllck5hbWUoaWQpKX08L2I+PHNtYWxsPiR7YWNjZXB0ZWQ/ItCf0KDQmNCd0K/QmyI6ItCe0JbQmNCU0JDQndCY0JUifTwvc21hbGw+PC9kaXY+YH0pLmpvaW4oIiIpO2NvbnN0IG49TnVtYmVyKGN1cnJlbnRNYXRjaD8uYWNjZXB0ZWRfY291bnR8fDApLHRvdGFsPU51bWJlcihjdXJyZW50TWF0Y2g/LnBsYXllcnNfbmVlZGVkfHxwbGF5ZXJzLmxlbmd0aHx8MCk7JCgiI21jQWNjZXB0VGV4dCIpLnRleHRDb250ZW50PWDQn9GA0LjQvdGP0LvQuCAke259INC40LcgJHt0b3RhbH0uINCU0LvRjyDRgdGC0LDRgNGC0LAg0LLRgdC1INGD0YfQsNGB0YLQvdC40LrQuCDQtNC+0LvQttC90Ysg0L/QvtC00YLQstC10YDQtNC40YLRjCDQvNCw0YLRhy5gOyQoIiNtY0FjY2VwdFByb2dyZXNzIikuc3R5bGUud2lkdGg9KHRvdGFsP01hdGgubWluKDEwMCxuL3RvdGFsKjEwMCk6MCkrIiUiOyQoIiNtY0FjY2VwdEJ0biIpLmRpc2FibGVkPSEhYWNjZXB0YW5jZVthcGlVc2VySWRdOyQoIiNtY0FjY2VwdEJ0biIpLnRleHRDb250ZW50PWFjY2VwdGFuY2VbYXBpVXNlcklkXT8i4pyTINCi0Ysg0L/RgNC40L3Rj9C7INC80LDRgtGHIjoi4pyTINCf0YDQuNC90Y/RgtGMINC80LDRgtGHIjt9CmZ1bmN0aW9uIGZpbGxDYXB0YWluU2VsZWN0cygpe2NvbnN0IHBsYXllcnM9Y3VycmVudE1hdGNoPy5wbGF5ZXJzfHxbXSxzaXplPSh7IjF2MSI6MSwiMnYyIjoyLCIzdjMiOjMsIjV2NSI6NX0pW2N1cnJlbnRNYXRjaD8ubW9kZV18fDUsdGVhbXM9e0E6cGxheWVycy5zbGljZSgwLHNpemUpLEI6cGxheWVycy5zbGljZShzaXplKX07WyJBIiwiQiJdLmZvckVhY2godD0+e2NvbnN0IHNlbD0kKCIjbWNDYXB0YWluIit0KSxpZHM9dGVhbXNbdF07c2VsLmlubmVySFRNTD1pZHMubWFwKGlkPT5gPG9wdGlvbiB2YWx1ZT0iJHtlc2MoaWQpfSI+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGlkKSl9PC9vcHRpb24+YCkuam9pbigiIik7Y29uc3QgY2FwdGFpbj1jdXJyZW50TWF0Y2g/Llt0PT09IkEiPyJjYXB0YWluX2lkIjoiY2FwdGFpbl9iX2lkIl07aWYoY2FwdGFpbilzZWwudmFsdWU9Y2FwdGFpbjskKCIjbWNDYXB0YWluIit0KyJJbmZvIikudGV4dENvbnRlbnQ9Y2FwdGFpbj8i0JrQsNC/0LjRgtCw0L0g0L3QsNC30L3QsNGH0LXQvTogIittYXRjaFBsYXllck5hbWUoY2FwdGFpbik6ItCa0LDQv9C40YLQsNC9INC10YnRkSDQvdC1INCy0YvQsdGA0LDQvSI7fSk7fQpmdW5jdGlvbiByZW5kZXJNYXBQaWNrKCl7Y29uc3QgYWN0aW9ucz1jdXJyZW50TWF0Y2g/Lm1hcF9hY3Rpb25zfHxbXSxiYW5zPWFjdGlvbnMuZmlsdGVyKGE9PmEuYWN0aW9uPT09ImJhbiIpLmxlbmd0aCxmaW5pc2hlZD0hIWN1cnJlbnRNYXRjaD8ubWFwX25hbWU7JCgiI21jQmFuVGl0bGUiKS50ZXh0Q29udGVudD1maW5pc2hlZD8i0JrQsNGA0YLQsCDQstGL0LHRgNCw0L3QsDogIitjdXJyZW50TWF0Y2gubWFwX25hbWU6YEJhbiAvIFBpY2sgwrcgJHtNYXRoLm1pbihiYW5zLDQpfS80INCx0LDQvdC+0LJgO2NvbnN0IGV4cGVjdGVkPWJhbnMlMj09PTA/Y3VycmVudE1hdGNoPy5jYXB0YWluX2lkOihjdXJyZW50TWF0Y2g/LmNhcHRhaW5fYl9pZHx8Y3VycmVudE1hdGNoPy5wbGF5ZXJzPy5bKHsiMXYxIjoxLCIydjIiOjIsIjN2MyI6MywiNXY1Ijo1fSlbY3VycmVudE1hdGNoPy5tb2RlXXx8NV0pO2NvbnN0IGNhbkFjdD0hZmluaXNoZWQmJmFwaVVzZXJJZD09PWV4cGVjdGVkOyQoIiNtY0JhbkhpbnQiKS50ZXh0Q29udGVudD1maW5pc2hlZD8iQmFuLXBpY2sg0LfQsNCy0LXRgNGI0ZHQvS4g0J/QtdGA0LXRhdC+0LTQuCDQsiDQu9C+0LHQsdC4INC80LDRgtGH0LAuIjpiYW5zPDQ/YNCh0LvQtdC00YPRjtGJ0LjQuSDQsdCw0L06ICR7bWF0Y2hQbGF5ZXJOYW1lKGV4cGVjdGVkKX0ke2NhbkFjdD8iIOKAlCDRgtCy0L7QuSDRhdC+0LQiOiIg4oCUINC+0LbQuNC00LDQtdC8INC60LDQv9C40YLQsNC90LAifS5gOmDQkdCw0L3RiyDQt9Cw0LLQtdGA0YjQtdC90YsuINCa0LDQv9C40YLQsNC9IEEg0LLRi9Cx0LjRgNCw0LXRgiDQutCw0YDRgtGDJHthcGlVc2VySWQ9PT1jdXJyZW50TWF0Y2g/LmNhcHRhaW5faWQ/IiDigJQg0LLRi9Cx0LXRgNC4INC60LDRgNGC0YMg0L3QuNC20LUiOiIuIn1gO2NvbnN0IHVzZWQ9bmV3IFNldChhY3Rpb25zLm1hcChhPT5hLm1hcF9uYW1lKSk7JCgiI21jTWFwQnV0dG9ucyIpLmlubmVySFRNTD1NQVRDSF9NQVBTLm1hcChuYW1lPT57bGV0IGFjdGlvbj0iYmFuIjtpZihiYW5zPj00KWFjdGlvbj0icGljayI7cmV0dXJuIGA8YnV0dG9uIGNsYXNzPSJtYy1tYXAgJHt1c2VkLmhhcyhuYW1lKT8idXNlZCI6IiJ9IiBkYXRhLW1jLW1hcD0iJHtlc2MobmFtZSl9IiBkYXRhLW1jLWFjdGlvbj0iJHthY3Rpb259IiAkeyghY2FuQWN0fHxmaW5pc2hlZHx8dXNlZC5oYXMobmFtZSkpPyJkaXNhYmxlZCI6IiJ9PiR7ZXNjKG5hbWUpfTxzbWFsbCBzdHlsZT0iZGlzcGxheTpibG9jaztjb2xvcjojOWRhOWI1O21hcmdpbi10b3A6NHB4Ij4ke3VzZWQuaGFzKG5hbWUpPyhhY3Rpb25zLmZpbmQoYT0+YS5tYXBfbmFtZT09PW5hbWUpPy5hY3Rpb249PT0iYmFuIj8i0JfQkNCR0JDQndCV0J3QkCI6ItCS0KvQkdCg0JDQndCQIik6YWN0aW9uPT09ImJhbiI/IkJBTiI6IlBJQ0sifTwvc21hbGw+PC9idXR0b24+YH0pLmpvaW4oIiIpOyQoIiNtY01hcExvZyIpLmlubmVySFRNTD1hY3Rpb25zLm1hcCgoYSxpKT0+YDxkaXYgY2xhc3M9Im1jLWxvZ3JvdyI+PHNwYW4+JHtpKzF9LiAke2VzYyhhLmFjdGlvbj09PSJiYW4iPyJCQU4iOiJQSUNLIil9IMK3ICR7ZXNjKGEubWFwX25hbWUpfTwvc3Bhbj48c3Bhbj4ke2VzYyhtYXRjaFBsYXllck5hbWUoYS5ieSkpfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKXx8JzxwIGNsYXNzPSJtYy1tdXRlZCI+0J/QvtC60LAg0L3QtdGCINC00LXQudGB0YLQstC40Lkg0L3QsCDQutCw0YDRgtC1LjwvcD4nOyQkKCJbZGF0YS1tYy1tYXBdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L21hcC1hY3Rpb25gLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxhY3Rpb246Yi5kYXRhc2V0Lm1jQWN0aW9uLG1hcF9uYW1lOmIuZGF0YXNldC5tY01hcH19KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt9Y2F0Y2goZSl7dG9hc3QoIkJhbi9QaWNrOiAiK2UubWVzc2FnZSk7fX0pKTt9CmZ1bmN0aW9uIHJlbmRlck1hdGNoKCl7aWYoIWN1cnJlbnRNYXRjaClyZXR1cm47Y29uc3QgbT1jdXJyZW50TWF0Y2g7JCgiI21hdGNoQ2VudGVyIikuY2xhc3NMaXN0LmFkZCgidmlzaWJsZSIpOyQoIiNtY1N1YnRpdGxlIikudGV4dENvbnRlbnQ9YNCc0LDRgtGHICR7bS5tYXRjaF9pZH0gwrcgJHttLm1vZGV9IMK3ICR7bS5wbGF5ZXJzPy5sZW5ndGh8fDB9INC40LPRgNC+0LrQvtCyYDskKCIjbWNTdGF0dXMiKS50ZXh0Q29udGVudD1TdHJpbmcobS5zdGF0dXN8fCJNQVRDSCIpLnJlcGxhY2VBbGwoIl8iLCIgIikudG9VcHBlckNhc2UoKTskKCIjbWNUaXRsZSIpLnRleHRDb250ZW50PSh7YWNjZXB0aW5nOiLQnNCw0YLRhyDQvdCw0LnQtNC10L0g4oCUINC/0L7QtNGC0LLQtdGA0LTQuCIsbWFwX3BpY2s6IkJhbiAvIFBpY2sg0LrQsNGA0YIiLHJlYWR5OiLQm9C+0LHQsdC4INCz0L7RgtC+0LLQviIscGxheWluZzoi0JzQsNGC0Ycg0LIg0LjQs9GA0LUiLHVuZGVyX3Jldmlldzoi0KDQtdC30YPQu9GM0YLQsNGCINC90LAg0L/RgNC+0LLQtdGA0LrQtSIsY29uZmlybWVkOiLQnNCw0YLRhyDQv9C+0LTRgtCy0LXRgNC20LTRkdC9IixyZWplY3RlZDoi0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQutC70L7QvdGR0L0iLGRpc3B1dGVkOiLQoNC10LfRg9C70YzRgtCw0YIg0L7RgdC/0LDRgNC40LLQsNC10YLRgdGPIixkZWNsaW5lZDoi0JzQsNGC0Ycg0L7RgtC60LvQvtC90ZHQvSIsZXhwaXJlZDoi0JLRgNC10LzRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjyDQuNGB0YLQtdC60LvQviJ9KVttLnN0YXR1c118fCLQnNCw0YLRhyI7CiBpZihtLnN0YXR1cz09PSJhY2NlcHRpbmciKXttY1Nob3coIm1jQWNjZXB0UGFuZWwiKTtyZW5kZXJNY1BsYXllcnMoKTtyZXR1cm47fQogaWYobS5zdGF0dXM9PT0ibWFwX3BpY2siKXttY1Nob3coIm1jQmFuUGFuZWwiKTtmaWxsQ2FwdGFpblNlbGVjdHMoKTtyZW5kZXJNYXBQaWNrKCk7cmV0dXJuO30KIGlmKG0uc3RhdHVzPT09InJlYWR5Iil7bWNTaG93KCJtY0xvYmJ5UGFuZWwiKTtjb25zdCBzaXplPSh7IjF2MSI6MSwiMnYyIjoyLCIzdjMiOjMsIjV2NSI6NX0pW20ubW9kZV18fDUsdGE9KG0ucGxheWVyc3x8W10pLnNsaWNlKDAsc2l6ZSksdGI9KG0ucGxheWVyc3x8W10pLnNsaWNlKHNpemUpOyQoIiNtY1RlYW1BIikuaW5uZXJIVE1MPXRhLm1hcChpZD0+YDxsaT4ke2VzYyhtYXRjaFBsYXllck5hbWUoaWQpKX0ke2lkPT09bS5jYXB0YWluX2lkPyIgwrcg0JrQkNCf0JjQotCQ0J0iOiIifTwvbGk+YCkuam9pbigiIik7JCgiI21jVGVhbUIiKS5pbm5lckhUTUw9dGIubWFwKGlkPT5gPGxpPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfSR7aWQ9PT1tLmNhcHRhaW5fYl9pZD8iIMK3INCa0JDQn9CY0KLQkNCdIjoiIn08L2xpPmApLmpvaW4oIiIpOyQoIiNtY0xvYmJ5SW5mbyIpLnRleHRDb250ZW50PWDQmtCw0YDRgtCwOiAke20ubWFwX25hbWV8fCLQvdC1INCy0YvQsdGA0LDQvdCwIn0uINCa0LDQv9C40YLQsNC9INC60L7QvNCw0L3QtNGLIEEg0LfQsNC/0YPRgdC60LDQtdGCINC80LDRgtGHLmA7JCgiI21jU3RhcnRCdG4iKS5kaXNhYmxlZD1hcGlVc2VySWQhPT1tLmNhcHRhaW5faWQ7JCgiI21jU3RhcnRCdG4iKS50ZXh0Q29udGVudD1hcGlVc2VySWQ9PT1tLmNhcHRhaW5faWQ/IuKWtiDQndCw0YfQsNGC0Ywg0LzQsNGC0YciOiLQntC20LjQtNCw0LXQvCDQutCw0L/QuNGC0LDQvdCwINC60L7QvNCw0L3QtNGLIEEiO3JldHVybjt9CiBpZihtLnN0YXR1cz09PSJwbGF5aW5nIil7bWNTaG93KCJtY1BsYXlpbmdQYW5lbCIpO3JldHVybjt9CiBpZihbInVuZGVyX3JldmlldyIsImNvbmZpcm1lZCIsInJlamVjdGVkIiwiZGlzcHV0ZWQiXS5pbmNsdWRlcyhtLnN0YXR1cykpe21jU2hvdygibWNSZXZpZXdQYW5lbCIpOyQoIiNtY1Jldmlld0luZm8iKS50ZXh0Q29udGVudD1tLnN0YXR1cz09PSJjb25maXJtZWQiP2DQoNC10LfRg9C70YzRgtCw0YIg0L/QvtC00YLQstC10YDQttC00ZHQvTogJHttLnNjb3JlX2E/PyLigJQifToke20uc2NvcmVfYj8/IuKAlCJ9LiDQoNC10LnRgtC40L3QsyDQvNC+0LbQtdGCINCx0YvRgtGMINC+0LHQvdC+0LLQu9GR0L0g0LzQvtC00LXRgNCw0YbQuNC10LkuYDptLnN0YXR1cz09PSJyZWplY3RlZCI/ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGC0LrQu9C+0L3RkdC9INC80L7QtNC10YDQsNGC0L7RgNC+0LwuINCf0YDQvtCy0LXRgNGMINGD0LLQtdC00L7QvNC70LXQvdC40Y8uIjptLnN0YXR1cz09PSJkaXNwdXRlZCI/ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGB0L/QsNGA0LjQstCw0LXRgtGB0Y8uINCc0L7QtNC10YDQsNGC0L7RgNGDINGC0YDQtdCx0YPQtdGC0YHRjyDQv9GA0L7QstC10YDQutCwLiI6ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGC0L/RgNCw0LLQu9C10L0g0L3QsCDQv9GA0L7QstC10YDQutGDINC80L7QtNC10YDQsNGC0L7RgNGDLiBFTE8g0L3QtSDQvNC10L3Rj9C10YLRgdGPINC00L4g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uIjskKCIjbWNBcHBlYWxGb3JtIikuY2xhc3NMaXN0LnRvZ2dsZSgibWMtaGlkZGVuIiwhWyJjb25maXJtZWQiLCJyZWplY3RlZCIsImRpc3B1dGVkIl0uaW5jbHVkZXMobS5zdGF0dXMpKTtyZXR1cm47fQogbWNTaG93KCJtY1Jldmlld1BhbmVsIik7JCgiI21jUmV2aWV3SW5mbyIpLnRleHRDb250ZW50PSLQnNCw0YLRhyDQt9Cw0LLQtdGA0YjQuNC7INC/0YDQvtGG0LXRgdGBINC/0YDQuNC90Y/RgtC40Y86ICIrbS5zdGF0dXM7Cn0KYXN5bmMgZnVuY3Rpb24gcG9sbEFjdGl2ZU1hdGNoKCl7aWYoIWFjdGl2ZU1hdGNoSWQpcmV0dXJuO3RyeXtjb25zdCBtPWF3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9YCk7Y3VycmVudE1hdGNoPW07cmVuZGVyTWF0Y2goKTtpZihbImNvbmZpcm1lZCIsInJlamVjdGVkIiwiZGVjbGluZWQiLCJleHBpcmVkIl0uaW5jbHVkZXMobS5zdGF0dXMpKXN0b3BNYXRjaFBvbGxpbmcoKTt9Y2F0Y2goZSl7Y29uc29sZS53YXJuKCJNYXRjaCBwb2xsIixlLm1lc3NhZ2UpO319CmFzeW5jIGZ1bmN0aW9uIHNob3dNYXRjaChpZCl7YWN0aXZlTWF0Y2hJZD1pZDtxdWV1ZUFjdGl2ZT1mYWxzZTtzdG9wUXVldWVQb2xsaW5nKCk7JCgiI3F1ZXVlQnV0dG9uIikudGV4dENvbnRlbnQ9ItCd0LDQudGC0Lgg0LzQsNGC0YciO25hdigibWF0Y2hlcyIpO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3N0b3BNYXRjaFBvbGxpbmcoKTttYXRjaFBvbGxUaW1lcj1zZXRJbnRlcnZhbCgoKT0+e2lmKCFkb2N1bWVudC5oaWRkZW4pcG9sbEFjdGl2ZU1hdGNoKCl9LDMwMDApO30KYXN5bmMgZnVuY3Rpb24gc3RhcnRRdWV1ZVBvbGxpbmcoKXtzdG9wUXVldWVQb2xsaW5nKCk7Y29uc3QgdXBkYXRlPWFzeW5jKCk9PntpZighcXVldWVBY3RpdmV8fGRvY3VtZW50LmhpZGRlbilyZXR1cm47dHJ5e2NvbnN0IHE9YXdhaXQgYXBpKGAvcXVldWUvJHtlbmNvZGVVUklDb21wb25lbnQoc2VsZWN0ZWRNb2RlKX1gKTtjb25zdCBmb3VuZD1OdW1iZXIocS5wbGF5ZXJzX2ZvdW5kfHwwKSxuZWVkZWQ9TnVtYmVyKHEucGxheWVyc19uZWVkZWR8fDApOyQoIiNxdWV1ZVRpdGxlIikudGV4dENvbnRlbnQ9ItCY0YnQtdC8INGB0L7Qv9C10YDQvdC40LrQvtCy4oCmIjskKCIjcXVldWVEZXNjcmlwdGlvbiIpLnRleHRDb250ZW50PWDQkiDQvtGH0LXRgNC10LTQuCAke2ZvdW5kfS8ke25lZWRlZH07INC+0YHRgtCw0LvQvtGB0YwgJHtNYXRoLm1heCgwLG5lZWRlZC1mb3VuZCl9YDtjb25zdCBhY3RpdmU9YXdhaXQgYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfS9hY3RpdmUtbWF0Y2hgKTtpZihhY3RpdmUuYWN0aXZlJiZhY3RpdmUubWF0Y2gpe2FjdGl2ZU1hdGNoSWQ9YWN0aXZlLm1hdGNoLm1hdGNoX2lkO2F3YWl0IHNob3dNYXRjaChhY3RpdmVNYXRjaElkKTt0b2FzdCgi0JzQsNGC0Ycg0L3QsNC50LTQtdC9ISDQn9C+0LTRgtCy0LXRgNC00Lgg0YPRh9Cw0YHRgtC40LUuIik7fX1jYXRjaChlKXtjb25zb2xlLndhcm4oIlF1ZXVlIHN0YXR1cyIsZS5tZXNzYWdlKTt9fTthd2FpdCB1cGRhdGUoKTtxdWV1ZVBvbGxUaW1lcj1zZXRJbnRlcnZhbCh1cGRhdGUsNDAwMCk7fQokKCIjbWNBY2NlcHRCdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9hY2NlcHRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxhY2NlcHRlZDp0cnVlfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQotGLINC/0L7QtNGC0LLQtdGA0LTQuNC7INC80LDRgtGHLiIpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQv9GA0LjQvdGP0YLRjCDQvNCw0YLRhzogIitlLm1lc3NhZ2UpO319KTsKJCgiI21jRGVjbGluZUJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57aWYoIWNvbmZpcm0oItCe0YLQutC70L7QvdC40YLRjCDQvdCw0LnQtNC10L3QvdGL0Lkg0LzQsNGC0Yc/INCe0YHRgtCw0LvRjNC90YvQtSDQuNCz0YDQvtC60Lgg0LLQtdGA0L3Rg9GC0YHRjyDQsiDQv9C+0LjRgdC6LiIpKXJldHVybjt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vYWNjZXB0YCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsYWNjZXB0ZWQ6ZmFsc2V9fSk7c3RvcE1hdGNoUG9sbGluZygpOyQoIiNtYXRjaENlbnRlciIpLmNsYXNzTGlzdC5yZW1vdmUoInZpc2libGUiKTt0b2FzdCgi0JzQsNGC0Ycg0L7RgtC60LvQvtC90ZHQvS4iKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L7RgtC60LvQvtC90LjRgtGMOiAiK2UubWVzc2FnZSk7fX0pOwphc3luYyBmdW5jdGlvbiBzZXRDYXB0YWluKHRlYW0pe2NvbnN0IGlkPSQoIiNtY0NhcHRhaW4iK3RlYW0pLnZhbHVlO2lmKCFpZClyZXR1cm47dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L2NhcHRhaW5gLHttZXRob2Q6IlBPU1QiLGJvZHk6e2NhcHRhaW5faWQ6aWQsdGVhbX19KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt0b2FzdCgi0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgIit0ZWFtKyIg0L3QsNC30L3QsNGH0LXQvS4iKTt9Y2F0Y2goZSl7dG9hc3QoItCa0LDQv9C40YLQsNC9OiAiK2UubWVzc2FnZSk7fX0KJCgiI21jU2V0Q2FwdGFpbkEiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnNldENhcHRhaW4oIkEiKSk7JCgiI21jU2V0Q2FwdGFpbkIiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnNldENhcHRhaW4oIkIiKSk7CiQoIiNtY1N0YXJ0QnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vc3RhcnQ/dGVsZWdyYW1faWQ9JHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gLHttZXRob2Q6IlBPU1QifSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7dG9hc3QoItCc0LDRgtGHINC+0YLQvNC10YfQtdC9INC60LDQuiDQvdCw0YfQsNGC0YvQuS4iKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNC/0YPRgdGC0LjRgtGMINC80LDRgtGHOiAiK2UubWVzc2FnZSk7fX0pOwokKCIjbWNTdWJtaXRSZXN1bHQiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2NvbnN0IHNjb3JlX2E9TnVtYmVyKCQoIiNtY1Njb3JlQSIpLnZhbHVlKSxzY29yZV9iPU51bWJlcigkKCIjbWNTY29yZUIiKS52YWx1ZSk7aWYoIU51bWJlci5pc0ludGVnZXIoc2NvcmVfYSl8fCFOdW1iZXIuaXNJbnRlZ2VyKHNjb3JlX2IpfHxzY29yZV9hPDB8fHNjb3JlX2I8MClyZXR1cm4gdG9hc3QoItCj0LrQsNC20Lgg0LrQvtGA0YDQtdC60YLQvdGL0Lkg0YHRh9GR0YIg0L7QsdC10LjRhSDQutC+0LzQsNC90LQuIik7dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L3Jlc3VsdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHNjb3JlX2Esc2NvcmVfYixzdGF0czp7W2FwaVVzZXJJZF06e2tpbGxzOk51bWJlcigkKCIjbWNLaWxscyIpLnZhbHVlfHwwKSxkZWF0aHM6TnVtYmVyKCQoIiNtY0RlYXRocyIpLnZhbHVlfHwwKX19LG5vdGU6JCgiI21jUmVzdWx0Tm90ZSIpLnZhbHVlLnRyaW0oKX19KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt0b2FzdCgi0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQv9GA0LDQstC70LXQvSDQvNC+0LTQtdGA0LDRgtC+0YDRgy4gRUxPINC/0L7QutCwINC90LUg0LjQt9C80LXQvdGR0L0uIik7fWNhdGNoKGUpe3RvYXN0KCLQoNC10LfRg9C70YzRgtCw0YI6ICIrZS5tZXNzYWdlKTt9fSk7CiQoIiNtY1N1Ym1pdEFwcGVhbCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L2FwcGVhbHNgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxyZWFzb246JCgiI21jQXBwZWFsUmVhc29uIikudmFsdWUsZGV0YWlsczokKCIjbWNBcHBlYWxEZXRhaWxzIikudmFsdWUudHJpbSgpfX0pO3RvYXN0KCLQkNC/0LXQu9C70Y/RhtC40Y8g0L7RgtC/0YDQsNCy0LvQtdC90LAg0LzQvtC00LXRgNCw0YLQvtGA0YMuIik7JCgiI21jU3VibWl0QXBwZWFsIikuZGlzYWJsZWQ9dHJ1ZTt9Y2F0Y2goZSl7dG9hc3QoItCQ0L/QtdC70LvRj9GG0LjRjzogIitlLm1lc3NhZ2UpO319KTsKLy8gS2VlcCByZWFsIHByb2ZpbGUgc3RhdGUgYW5kIGRpc2NvdmVyIGEgbWF0Y2ggaWYgaXQgd2FzIGZvcm1lZCBieSBhbm90aGVyIHF1ZXVlIHBhcnRpY2lwYW50Lgpjb25zdCBfb2xkQXBwbHlQbGF5ZXI9YXBwbHlQbGF5ZXI7YXBwbHlQbGF5ZXI9ZnVuY3Rpb24ocCl7X29sZEFwcGx5UGxheWVyKHApO2NvbnN0IHBzPSQoIiNwcm9maWxlU3RhdHVzIik7aWYocHMpcHMudGV4dENvbnRlbnQ9IuKXjyDQn9Cg0J7QpNCY0JvQrCDQoSDQodCV0KDQktCV0KDQkCI7c2V0QXBpU3RhdHVzKCJBUEkg0L7QvdC70LDQudC9IMK3INC/0YDQvtGE0LjQu9GMINC30LDQs9GA0YPQttC10L0iLHRydWUpO307CmNvbnN0IF9vbGRJbml0QXBpPWluaXRBcGk7aW5pdEFwaT1hc3luYyBmdW5jdGlvbigpe2F3YWl0IF9vbGRJbml0QXBpKCk7aWYoYXBpVXNlcklkKXt0cnl7Y29uc3QgYT1hd2FpdCBhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L2FjdGl2ZS1tYXRjaGApO2lmKGEuYWN0aXZlJiZhLm1hdGNoKXthY3RpdmVNYXRjaElkPWEubWF0Y2gubWF0Y2hfaWQ7YXdhaXQgc2hvd01hdGNoKGFjdGl2ZU1hdGNoSWQpO319Y2F0Y2goZSl7Y29uc29sZS53YXJuKCJBY3RpdmUgbWF0Y2giLGUubWVzc2FnZSk7fX19OwoKCmZ1bmN0aW9uIHN0YW5kcG9pbnRUcm9waHlTdmcocGxhY2Upe2NvbnN0IHA9TnVtYmVyKHBsYWNlKXx8NDtjb25zdCBtZXRhbD1wPT09MT9bJyNmZmYwYTYnLCcjZDY5YjIwJywnI2ZmZjdjYyddOnA9PT0yP1snI2Y1ZjhmZicsJyM4Nzk2YTknLCcjZmZmZmZmJ106cD09PTM/WycjZmZkMmEwJywnIzllNGIyMCcsJyNmZmU0YmQnXTpbJyNjN2Q1ZTgnLCcjNTk2YzgzJywnI2VlZjVmZiddO2NvbnN0IFtoaSxtaWQsc2hpbmVdPW1ldGFsO2NvbnN0IGVtYmxlbT1wPT09MT8nSSc6cD09PTI/J0lJJzpwPT09Mz8nSUlJJzonU1AnO2NvbnN0IHVpZD0nc3BjdXAnK3A7cmV0dXJuIGA8c3ZnIHZpZXdCb3g9IjAgMCAxODAgMjAwIiByb2xlPSJpbWciIGFyaWEtbGFiZWw9ItCa0YPQsdC+0LogU1RBTkRQT0lOVCDigJQgJHtwfSDQvNC10YHRgtC+Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9IiR7dWlkfW1ldGFsIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMSI+PHN0b3Agc3RvcC1jb2xvcj0iJHtzaGluZX0iLz48c3RvcCBvZmZzZXQ9Ii4yMiIgc3RvcC1jb2xvcj0iJHtoaX0iLz48c3RvcCBvZmZzZXQ9Ii41MiIgc3RvcC1jb2xvcj0iJHttaWR9Ii8+PHN0b3Agb2Zmc2V0PSIuNzgiIHN0b3AtY29sb3I9IiR7c2hpbmV9Ii8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIke21pZH0iLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBpZD0iJHt1aWR9ZGFyayIgeDE9IjAiIHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIHN0b3AtY29sb3I9IiMzOTQyNGMiLz48c3RvcCBvZmZzZXQ9Ii41IiBzdG9wLWNvbG9yPSIjMDgwYjEwIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMjUyZDM2Ii8+PC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZGllbnQgaWQ9IiR7dWlkfWJhc2UiIHgxPSIwIiB5MT0iMCIgeDI9IjAiIHkyPSIxIj48c3RvcCBzdG9wLWNvbG9yPSIke2hpfSIvPjxzdG9wIG9mZnNldD0iLjQ4IiBzdG9wLWNvbG9yPSIke21pZH0iLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMxNTE5MWYiLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48ZWxsaXBzZSBjeD0iOTAiIGN5PSIxODciIHJ4PSI1OCIgcnk9IjYiIGZpbGw9IiR7aGl9IiBvcGFjaXR5PSIuMTgiLz48cGF0aCBkPSJNNDcgMjJMMjIgMzNMMzIgNjdRMzggODMgNjEgODJMNjcgNjZRNDcgNjIgNDQgNDdMNDcgMjJaTTEzMyAyMkwxNTggMzNMMTQ4IDY3UTE0MiA4MyAxMTkgODJMMTEzIDY2UTEzMyA2MiAxMzYgNDdMMTMzIDIyWiIgZmlsbD0idXJsKCMke3VpZH1tZXRhbCkiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMiIvPjxwYXRoIGQ9Ik00OCAxOEwxMzIgMThMMTI4IDU3UTEyMiA4NCA5MCAxMDNRNTggODQgNTIgNTdaIiBmaWxsPSJ1cmwoIyR7dWlkfW1ldGFsKSIgc3Ryb2tlPSIke3NoaW5lfSIgc3Ryb2tlLXdpZHRoPSIyLjIiLz48cGF0aCBkPSJNNTggMjVMNzggMjdMODggODdMNjkgNzBaIiBmaWxsPSIjZmZmIiBvcGFjaXR5PSIuMiIvPjxwYXRoIGQ9Ik0xMzIgMjFMMTE2IDI4TDEwMyA4OUwxMjAgNjVaIiBmaWxsPSIjZmZmIiBvcGFjaXR5PSIuMTMiLz48cGF0aCBkPSJNNTggNDNMOTAgMzJMMTIyIDQzTDExMiA2NUw5MCA4NEw2OCA2NVoiIGZpbGw9InVybCgjJHt1aWR9ZGFyaykiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMS40Ii8+PHBhdGggZD0iTTkwIDM4TDk5IDQ4TDkwIDU3TDgxIDQ4WiIgZmlsbD0iJHtoaX0iLz48cGF0aCBkPSJNODMgNDlMOTAgNDRMOTcgNDlMOTAgNTVaIiBmaWxsPSIke21pZH0iLz48dGV4dCB4PSI5MCIgeT0iNjkiIGZvbnQtc2l6ZT0iOSIgZm9udC13ZWlnaHQ9IjkwMCIgbGV0dGVyLXNwYWNpbmc9Ii41IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmaWxsPSIke3NoaW5lfSIgZm9udC1mYW1pbHk9IkFyaWFsLHNhbnMtc2VyaWYiPlNUQU5EUE9JTlQ8L3RleHQ+PHBhdGggZD0iTTc4IDk5SDEwMkwxMDcgMTE5TDczIDExOVoiIGZpbGw9InVybCgjJHt1aWR9bWV0YWwpIiBzdHJva2U9IiR7c2hpbmV9IiBzdHJva2Utd2lkdGg9IjEuMyIvPjxwYXRoIGQ9Ik02NiAxMTlMMTE0IDExOUwxMjYgMTM1TDU0IDEzNVoiIGZpbGw9InVybCgjJHt1aWR9YmFzZSkiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMS40Ii8+PHBhdGggZD0iTTUzIDEzN0gxMjdMMTM0IDE2M0g0NloiIGZpbGw9InVybCgjJHt1aWR9ZGFyaykiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMS41Ii8+PHBhdGggZD0iTTYxIDE0MUgxMTlMMTI0IDE1N0g1NloiIGZpbGw9IiMwODBiMTAiIHN0cm9rZT0iJHtoaX0iIHN0cm9rZS13aWR0aD0iMS4yIi8+PHRleHQgeD0iOTAiIHk9IjE1NCIgZm9udC1zaXplPSIke2VtYmxlbT09PSdTUCc/MTg6MjB9IiBmb250LXdlaWdodD0iOTAwIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmaWxsPSIke3NoaW5lfSIgZm9udC1mYW1pbHk9Ikdlb3JnaWEsc2VyaWYiPiR7ZW1ibGVtfTwvdGV4dD48cGF0aCBkPSJNNDMgMTY1SDEzN0wxNDUgMTc2SDM1WiIgZmlsbD0idXJsKCMke3VpZH1tZXRhbCkiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMS40Ii8+PHBhdGggZD0iTTMxIDE3N0gxNDlMMTU0IDE4NUgyNloiIGZpbGw9InVybCgjJHt1aWR9YmFzZSkiIHN0cm9rZT0iJHtzaGluZX0iIHN0cm9rZS13aWR0aD0iMS40Ii8+PHBhdGggZD0iTTM5IDE4MUgxNDEiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLW9wYWNpdHk9Ii41IiBzdHJva2Utd2lkdGg9IjEuMiIvPjwvc3ZnPmA7fQovLyBDb3NtZXRpYyBzaG9wLCBleHRlbmRlZCBwcm9maWxlIHN0YXRpc3RpY3MgYW5kIHF1YWxpZmljYXRpb24gdHJvcGhpZXMuCmxldCBjb3NtZXRpY0NhdGFsb2c9W10sY29zbWV0aWNGaWx0ZXI9ImFsbCIsbXlDb3NtZXRpY0l0ZW1zPVtdOwpmdW5jdGlvbiBjb3NtZXRpY0ljb24odHlwZSl7cmV0dXJuIHR5cGU9PT0iZnJhbWUiPyLilqMiOnR5cGU9PT0idGl0bGUiPyLinKYiOiLilqQiO30KZnVuY3Rpb24gY29zbWV0aWNUeXBlTmFtZSh0eXBlKXtyZXR1cm4gKHtmcmFtZToi0KDQsNC80LrQsCDQv9GA0L7RhNC40LvRjyIsdGl0bGU6ItCi0LjRgtGD0LsiLHByb2ZpbGVfY2FyZDoi0JrQsNGA0YLQvtGH0LrQsCDQv9GA0L7RhNC40LvRjyJ9KVt0eXBlXXx8ItCa0L7RgdC80LXRgtC40LrQsCI7fQpmdW5jdGlvbiByZW5kZXJDb3NtZXRpY1Nob3AoKXtjb25zdCBsaXN0PWNvc21ldGljQ2F0YWxvZy5maWx0ZXIoeD0+Y29zbWV0aWNGaWx0ZXI9PT0iYWxsInx8eC5pdGVtX3R5cGU9PT1jb3NtZXRpY0ZpbHRlcik7Y29uc3Qgcm9vdD0kKCIjY29zbWV0aWNTaG9wR3JpZCIpO2lmKCFyb290KXJldHVybjtyb290LmlubmVySFRNTD1saXN0Lmxlbmd0aD9saXN0Lm1hcCh4PT5gPGFydGljbGUgY2xhc3M9ImNvc21ldGljLWl0ZW0iPjxkaXYgY2xhc3M9ImNvc21ldGljLWFydCI+JHtjb3NtZXRpY0ljb24oeC5pdGVtX3R5cGUpfTwvZGl2PjxkaXYgY2xhc3M9InJhcml0eSI+JHtlc2MoeC5yYXJpdHl8fCJjb21tb24iKX0gwrcgJHtlc2MoY29zbWV0aWNUeXBlTmFtZSh4Lml0ZW1fdHlwZSkpfTwvZGl2PjxoND4ke2VzYyh4Lm5hbWUpfTwvaDQ+PHA+JHtlc2MoeC5kZXNjcmlwdGlvbnx8ItCa0L7RgdC80LXRgtC40YfQtdGB0LrQuNC5INC/0YDQtdC00LzQtdGCIFNUQU5EUE9JTlQiKX08L3A+PGRpdiBjbGFzcz0iY29zbWV0aWMtbWV0YSI+PGI+JHtOdW1iZXIoeC5wcmljZV9wb2ludHN8fDApLnRvTG9jYWxlU3RyaW5nKCJydS1SVSIpfSBTUDwvYj48c3Bhbj4ke2VzYyhjb3NtZXRpY1R5cGVOYW1lKHguaXRlbV90eXBlKSl9PC9zcGFuPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IGJ1eUNvc21ldGljIiBkYXRhLWl0ZW09IiR7ZXNjKHguaXRlbV9pZCl9Ij7QmtGD0L/QuNGC0Yw8L2J1dHRvbj48L2FydGljbGU+YCkuam9pbigiIik6JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7QkiDRjdGC0L7QuSDQutCw0YLQtdCz0L7RgNC40Lgg0L/QvtC60LAg0L3QtdGCINC/0YDQtdC00LzQtdGC0L7Qsi48L2Rpdj4nOwogJCQoIi5idXlDb3NtZXRpYyIscm9vdCkuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57aWYoIWFwaVVzZXJJZCl7dG9hc3QoItCe0YLQutGA0L7QuSDQv9GA0LjQu9C+0LbQtdC90LjQtSDRh9C10YDQtdC3IFRlbGVncmFtLCDRh9GC0L7QsdGLINC/0L7QutGD0L/QsNGC0Ywg0L/RgNC10LTQvNC10YLRiy4iKTtyZXR1cm47fWIuZGlzYWJsZWQ9dHJ1ZTt0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jb3NtZXRpY3MvJHtlbmNvZGVVUklDb21wb25lbnQoYi5kYXRhc2V0Lml0ZW0pfS9wdXJjaGFzZWAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkfX0pO3RvYXN0KGQubWVzc2FnZXx8ItCf0L7QutGD0L/QutCwINCy0YvQv9C+0LvQvdC10L3QsCIpO2F3YWl0IGxvYWRDb3NtZXRpY0RhdGEoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0LrRg9C/0LjRgtGMOiAiK2UubWVzc2FnZSk7fWZpbmFsbHl7Yi5kaXNhYmxlZD1mYWxzZTt9fSkpO30KZnVuY3Rpb24gcmVuZGVyTXlDb3NtZXRpY3MoKXtjb25zdCByb290PSQoIiNteUNvc21ldGljc0dyaWQiKTtpZighcm9vdClyZXR1cm47cm9vdC5pbm5lckhUTUw9bXlDb3NtZXRpY0l0ZW1zLmxlbmd0aD9teUNvc21ldGljSXRlbXMubWFwKHg9PmA8YXJ0aWNsZSBjbGFzcz0iY29zbWV0aWMtaXRlbSI+PGRpdiBjbGFzcz0iY29zbWV0aWMtYXJ0Ij4ke2Nvc21ldGljSWNvbih4Lml0ZW1fdHlwZSl9PC9kaXY+PGRpdiBjbGFzcz0icmFyaXR5Ij4ke2VzYyh4LnJhcml0eXx8ImNvbW1vbiIpfTwvZGl2PjxoND4ke2VzYyh4Lm5hbWUpfTwvaDQ+PHA+JHtlc2MoeC5kZXNjcmlwdGlvbnx8Y29zbWV0aWNUeXBlTmFtZSh4Lml0ZW1fdHlwZSkpfTwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGVxdWlwQ29zbWV0aWMiIGRhdGEtaXRlbT0iJHtlc2MoeC5pdGVtX2lkKX0iPtCd0LDQtNC10YLRjDwvYnV0dG9uPjwvYXJ0aWNsZT5gKS5qb2luKCIiKTonPGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCa0L7Qu9C70LXQutGG0LjRjyDQv9C+0LrQsCDQv9GD0YHRgtCwLjwvZGl2Pic7JCQoIi5lcXVpcENvc21ldGljIixyb290KS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9PntpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjCDRh9C10YDQtdC3IFRlbGVncmFtLiIpO3RyeXthd2FpdCBhcGkoIi9jb3NtZXRpY3MvZXF1aXAiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxpdGVtX2lkOmIuZGF0YXNldC5pdGVtfX0pO3RvYXN0KCLQn9GA0LXQtNC80LXRgiDRjdC60LjQv9C40YDQvtCy0LDQvSIpO2F3YWl0IGxvYWRDb3NtZXRpY0RhdGEoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0Y3QutC40L/QuNGA0L7QstCw0YLRjDogIitlLm1lc3NhZ2UpO319KSk7fQphc3luYyBmdW5jdGlvbiBsb2FkQ29zbWV0aWNEYXRhKCl7aWYoIWFwaVVzZXJJZCl7JCgiI2Nvc21ldGljU2hvcEdyaWQiKS5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7QmtCw0YLQsNC70L7QsyDQvNC+0LbQvdC+INC/0YDQvtGB0LzQvtGC0YDQtdGC0YwsINC90L4g0L/QvtC60YPQv9C60LAg0YLRgNC10LHRg9C10YIg0L/QvtC00LrQu9GO0YfQtdC90LjRjyDQv9GA0L7RhNC40LvRjy48L2Rpdj4nO3JldHVybjt9dHJ5e2NvbnN0IFtjYXRhbG9nLG93bmVkXT1hd2FpdCBQcm9taXNlLmFsbChbYXBpKCIvY29zbWV0aWNzL3Nob3AiKSxhcGkoYC9jb3NtZXRpY3MvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKV0pO2Nvc21ldGljQ2F0YWxvZz1jYXRhbG9nLml0ZW1zfHxjYXRhbG9nLmNvc21ldGljc3x8W107bXlDb3NtZXRpY0l0ZW1zPW93bmVkLml0ZW1zfHxvd25lZC5jb3NtZXRpY3N8fFtdOyQoIiNjb3NtZXRpY1BvaW50cyIpLnRleHRDb250ZW50PU51bWJlcihvd25lZC5zZXR0aW5ncz8ucG9pbnRzPz9vd25lZC5wb2ludHM/PzApLnRvTG9jYWxlU3RyaW5nKCJydS1SVSIpO3JlbmRlckNvc21ldGljU2hvcCgpO3JlbmRlck15Q29zbWV0aWNzKCk7fWNhdGNoKGUpeyQoIiNjb3NtZXRpY1Nob3BHcmlkIikuaW5uZXJIVE1MPWA8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0LrQsNGC0LDQu9C+0LM6ICR7ZXNjKGUubWVzc2FnZSl9PC9kaXY+YDt9fQphc3luYyBmdW5jdGlvbiBsb2FkRXh0cmFQcm9maWxlRGF0YSgpe2lmKCFhcGlVc2VySWQpcmV0dXJuO3RyeXtjb25zdCBbc3RhdHMsdHJvcGhpZXNdPWF3YWl0IFByb21pc2UuYWxsKFthcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3N0YXRpc3RpY3NgKSxhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3Ryb3BoaWVzYCldKTtjb25zdCBkPXN0YXRzLnN0YXRpc3RpY3N8fHN0YXRzO2NvbnN0IHZhbD0oaWQsdik9Pntjb25zdCBlbD0kKGlkKTtpZihlbCllbC50ZXh0Q29udGVudD12Pz8wO307dmFsKCIjZXh0cmFXaW5yYXRlIiwoZC53aW5fcmF0ZT8/ZC53aW5yYXRlKSE9bnVsbD8oTnVtYmVyKGQud2luX3JhdGU/P2Qud2lucmF0ZSkudG9GaXhlZCgxKSsiJSIpOihzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cz9NYXRoLnJvdW5kKHN0YXRlLndpbnMvKHN0YXRlLndpbnMrc3RhdGUubG9zc2VzK3N0YXRlLmRyYXdzKSoxMDApKyIlIjoiMCUiKSk7dmFsKCIjZXh0cmFMb3NzZXMiLGQubG9zc2VzPz9zdGF0ZS5sb3NzZXMpO3ZhbCgiI2V4dHJhRHJhd3MiLGQuZHJhd3M/P3N0YXRlLmRyYXdzKTt2YWwoIiNleHRyYUtpbGxzIixkLmtpbGxzPz9zdGF0ZS5raWxscyk7dmFsKCIjZXh0cmFEZWF0aHMiLGQuZGVhdGhzPz9zdGF0ZS5kZWF0aHMpO3ZhbCgiI2V4dHJhS0QiLGQua2Q/PyhzdGF0ZS5kZWF0aHM/KHN0YXRlLmtpbGxzL3N0YXRlLmRlYXRocykudG9GaXhlZCgyKToi4oCUIikpO3ZhbCgiI2V4dHJhTWF0Y2hlcyIsZC5tYXRjaGVzPz9kLnRvdGFsX21hdGNoZXM/PyhzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cykpO3ZhbCgiI2V4dHJhRWxvIixkLmVsbz8/c3RhdGUuZWxvKTskKCIjZXh0cmFTdGF0c05vdGUiKS50ZXh0Q29udGVudD0i0KHRgtCw0YLQuNGB0YLQuNC60LAg0L/QvtC70YPRh9C10L3QsCDQuNC3IEFQSS4iO2NvbnN0IGl0ZW1zPXRyb3BoaWVzLnRyb3BoaWVzfHxbXTskKCIjdHJvcGh5Q291bnQiKS50ZXh0Q29udGVudD1pdGVtcy5sZW5ndGgrIiDQndCQ0JPQoNCQ0JQiOyQoIiNwcm9maWxlVHJvcGhpZXMiKS5pbm5lckhUTUw9aXRlbXMubGVuZ3RoP2l0ZW1zLm1hcCh0PT57Y29uc3QgcGxhY2U9TnVtYmVyKHQucGxhY2UpfHw0O2NvbnN0IGNscz1wbGFjZT09PTE/InRyb3BoeS1nb2xkIjpwbGFjZT09PTI/InRyb3BoeS1zaWx2ZXIiOnBsYWNlPT09Mz8idHJvcGh5LWJyb256ZSI6InRyb3BoeS1vdGhlciI7Y29uc3QgcmFuaz1wbGFjZT09PTE/IjEiOnBsYWNlPT09Mj8iMiI6cGxhY2U9PT0zPyIzIjoiU1AiO3JldHVybiBgPGFydGljbGUgY2xhc3M9InRyb3BoeSAke2Nsc30iPjxzcGFuIGNsYXNzPSJ0cm9waHktcGxhY2UiPiR7cGxhY2U8PTM/IlRPUCAiK3BsYWNlOiJTVEFORFBPSU5UIn08L3NwYW4+PGRpdiBjbGFzcz0iY3VwIj4ke3N0YW5kcG9pbnRUcm9waHlTdmcocGxhY2UpfTwvZGl2PjxkaXYgY2xhc3M9InRyb3BoeS1pbmZvIj48c3BhbiBjbGFzcz0idHJvcGh5LXJhbmsiPiR7cmFua308L3NwYW4+PGRpdj48Yj4ke2VzYyh0LnRyb3BoeV9uYW1lfHwoItCi0L7Qvy0iK3QucGxhY2UpKX08L2I+PHNtYWxsPiR7ZXNjKHQucXVhbGlmaWNhdGlvbl9rZXl8fCLQmtCy0LDQu9C40YTQuNC60LDRhtC40Y8iKX08L3NtYWxsPjxzbWFsbD4ke3BsYWNlfSDQvNC10YHRgtC+PC9zbWFsbD48L2Rpdj48L2Rpdj48L2FydGljbGU+YH0pLmpvaW4oIiIpOic8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J/QvtC60LAg0L3QtdGCINC60YPQsdC60L7Qsi4g0J3QsNCz0YDQsNC00Ysg0L/QvtGP0LLRj9GC0YHRjyDQv9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINC60LLQsNC70LjRhNC40LrQsNGG0LjQuC48L2Rpdj4nO31jYXRjaChlKXskKCIjZXh0cmFTdGF0c05vdGUiKS50ZXh0Q29udGVudD0i0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0YHRgtCw0YLQuNGB0YLQuNC60YM6ICIrZS5tZXNzYWdlO319CiQkKCJbZGF0YS1jb3NtZXRpYy1maWx0ZXJdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2Nvc21ldGljRmlsdGVyPWIuZGF0YXNldC5jb3NtZXRpY0ZpbHRlcjskJCgiW2RhdGEtY29zbWV0aWMtZmlsdGVyXSIpLmZvckVhY2goeD0+eC5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHg9PT1iKSk7cmVuZGVyQ29zbWV0aWNTaG9wKCk7fSkpOwokKCIjcmVmcmVzaENvc21ldGljcyIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsbG9hZENvc21ldGljRGF0YSk7JCgiI3JlZnJlc2hFeHRyYVN0YXRzIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixsb2FkRXh0cmFQcm9maWxlRGF0YSk7CmNvbnN0IF9vbGROYXZGb3JDb3NtZXRpY3M9bmF2O25hdj1mdW5jdGlvbihwYWdlKXtfb2xkTmF2Rm9yQ29zbWV0aWNzKHBhZ2UpO2lmKHBhZ2U9PT0iY29zbWV0aWNzIilsb2FkQ29zbWV0aWNEYXRhKCk7aWYocGFnZT09PSJwcm9maWxlIilsb2FkRXh0cmFQcm9maWxlRGF0YSgpO307CmNvbnN0IF9vbGRBcHBseVBsYXllckZvckV4dHJhcz1hcHBseVBsYXllcjthcHBseVBsYXllcj1mdW5jdGlvbihwKXtfb2xkQXBwbHlQbGF5ZXJGb3JFeHRyYXMocCk7aWYoYXBpVXNlcklkKXtsb2FkRXh0cmFQcm9maWxlRGF0YSgpO2lmKGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoIiNwYWdlLWNvc21ldGljcy5hY3RpdmUiKSlsb2FkQ29zbWV0aWNEYXRhKCk7fX07CgpyZW5kZXIoKTsKaW5pdEFwaSgpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+"""

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
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json','X-Admin-Token':token,...(opts.headers||{})}});let data;try{data=await r.json()}catch{data={detail:await r.text()}}if(!r.ok)throw Error(data.detail||('HTTP '+r.status));return data}
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
        raise HTTPException(404, "ÐÐ³ÑÐ¾Ðº Ð½Ðµ Ð·Ð°ÑÐµÐ³Ð¸ÑÑÑÐ¸ÑÐ¾Ð²Ð°Ð½")

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
            raise HTTPException(404, "ÐÑÐµÐ´Ð¼ÐµÑ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð¸Ð»Ð¸ ÑÐ½ÑÑ Ñ Ð¿ÑÐ¾Ð´Ð°Ð¶Ð¸")
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id,updated_at) VALUES(?,?)", (payload.telegram_id, stamp))
        if con.execute("SELECT 1 FROM player_cosmetics WHERE telegram_id=? AND item_id=?", (payload.telegram_id,item_id)).fetchone():
            raise HTTPException(409, "ÐÑÐµÐ´Ð¼ÐµÑ ÑÐ¶Ðµ ÐµÑÑÑ Ð² ÐºÐ¾Ð»Ð»ÐµÐºÑÐ¸Ð¸")
        settings = con.execute("SELECT points FROM player_cosmetic_settings WHERE telegram_id=?", (payload.telegram_id,)).fetchone()
        if settings["points"] < item["price_points"]:
            raise HTTPException(409, "ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ SP Points")
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
            raise HTTPException(404, "ÐÑÐµÐ´Ð¼ÐµÑ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð² Ð²Ð°ÑÐµÐ¹ ÐºÐ¾Ð»Ð»ÐµÐºÑÐ¸Ð¸")
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
        raise HTTPException(400,"ÐÐ»ÑÑ ÐºÐ²Ð°Ð»Ð¸ÑÐ¸ÐºÐ°ÑÐ¸Ð¸ Ð² URL Ð¸ ÑÐµÐ»Ðµ Ð½Ðµ ÑÐ¾Ð²Ð¿Ð°Ð´Ð°ÐµÑ")
    stamp=now(); awarded=[]
    with db() as con:
        for place, telegram_id in enumerate(payload.placements, start=1):
            if not con.execute("SELECT 1 FROM players WHERE telegram_id=?",(telegram_id,)).fetchone():
                raise HTTPException(404,f"ÐÐ³ÑÐ¾Ðº {telegram_id} Ð½Ðµ Ð·Ð°ÑÐµÐ³Ð¸ÑÑÑÐ¸ÑÐ¾Ð²Ð°Ð½")
            elo=con.execute("SELECT elo FROM players WHERE telegram_id=?",(telegram_id,)).fetchone()["elo"]
            trophy_name={1:"STANDPOINT â Ð§ÐµÐ¼Ð¿Ð¸Ð¾Ð½ ÐºÐ²Ð°Ð»Ð¸ÑÐ¸ÐºÐ°ÑÐ¸Ð¸",2:"STANDPOINT â Ð¡ÐµÑÐµÐ±ÑÑÐ½ÑÐ¹ ÐºÑÐ±Ð¾Ðº",3:"STANDPOINT â ÐÑÐ¾Ð½Ð·Ð¾Ð²ÑÐ¹ ÐºÑÐ±Ð¾Ðº"}.get(place,f"STANDPOINT â Ð¢Ð¾Ð¿-{place} ÐºÐ²Ð°Ð»Ð¸ÑÐ¸ÐºÐ°ÑÐ¸Ð¸")
            award_id=uuid.uuid4().hex
            con.execute("INSERT INTO qualification_awards(award_id,qualification_key,telegram_id,place,trophy_name,elo_snapshot,awarded_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(qualification_key,telegram_id) DO UPDATE SET place=excluded.place,trophy_name=excluded.trophy_name,elo_snapshot=excluded.elo_snapshot,awarded_at=excluded.awarded_at",(award_id,qualification_key,telegram_id,place,trophy_name,elo,stamp))
            create_notification(con,telegram_id,"qualification_trophy","ÐÐ¾Ð»ÑÑÐµÐ½ ÐºÑÐ±Ð¾Ðº STANDPOINT",trophy_name,{"qualification_key":qualification_key,"place":place})
            awarded.append({"telegram_id":telegram_id,"place":place,"trophy_name":trophy_name,"elo_snapshot":elo})
    return {"status":"awarded","qualification_key":qualification_key,"awards":awarded}


@app.post("/admin/cosmetics")
def admin_create_cosmetic(item_id: str, name: str, item_type: Literal["frame","title","profile_card"], price_points: int = 0, description: str = "", rarity: str = "common", x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    if not item_id.strip() or not name.strip() or price_points < 0 or price_points > 1000000:
        raise HTTPException(400,"ÐÑÐ¾Ð²ÐµÑÑÑÐµ ID, Ð½Ð°Ð·Ð²Ð°Ð½Ð¸Ðµ Ð¸ ÑÐµÐ½Ñ")
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
        if new_points < 0: raise HTTPException(409,"ÐÐ°Ð»Ð°Ð½Ñ Ð½Ðµ Ð¼Ð¾Ð¶ÐµÑ Ð±ÑÑÑ Ð¾ÑÑÐ¸ÑÐ°ÑÐµÐ»ÑÐ½ÑÐ¼")
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


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
_FRONTEND_HTML_B64 = """PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzA4MGEwZCI+Cjx0aXRsZT5TVEFORFBPSU5UIOKAlCBTdGFuZG9mZiAyIENvbXBldGl0aXZlPC90aXRsZT4KPHN0eWxlPgo6cm9vdHtjb2xvci1zY2hlbWU6ZGFyazstLWJnOiMwODBhMGQ7LS1zdXJmYWNlOiMxMTE2MWM7LS1zdXJmYWNlMjojMTcxZTI2Oy0tc3VyZmFjZTM6IzIwMmEzNTstLXN0cm9rZTojM2E0NjUzOy0tdGV4dDojZmZmZmZmOy0tbXV0ZWQ6I2MwY2FkNDstLW9yYW5nZTojZmY1YTEyOy0tb3JhbmdlMjojZmY4YTIyOy0tZ3JlZW46IzUwZGRhMDstLXJlZDojZmY1OTY0Oy0tZ29sZDojZmZjYTY4Oy0tcjoxN3B4fQoqe2JveC1zaXppbmc6Ym9yZGVyLWJveH1odG1se2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yLXNjaGVtZTpkYXJrfWJvZHl7bWFyZ2luOjA7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAscmdiYSgyNTUsODAsMTUsLjEyKSx0cmFuc3BhcmVudCAyNyUpLHZhcigtLWJnKTtjb2xvcjp2YXIoLS10ZXh0KTtmb250OjE0cHgvMS40NSBJbnRlcix1aS1zYW5zLXNlcmlmLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLEJsaW5rTWFjU3lzdGVtRm9udCwiU2Vnb2UgVUkiLHNhbnMtc2VyaWY7bWluLWhlaWdodDoxMDB2aH1idXR0b24saW5wdXQsc2VsZWN0LHRleHRhcmVhe2ZvbnQ6aW5oZXJpdH1idXR0b257Y3Vyc29yOnBvaW50ZXI7Y29sb3I6aW5oZXJpdH1idXR0b246Zm9jdXMtdmlzaWJsZSxpbnB1dDpmb2N1cy12aXNpYmxle291dGxpbmU6MnB4IHNvbGlkIHZhcigtLW9yYW5nZTIpO291dGxpbmUtb2Zmc2V0OjJweH1he2NvbG9yOmluaGVyaXQ7dGV4dC1kZWNvcmF0aW9uOm5vbmV9LnNoZWxse21pbi1oZWlnaHQ6MTAwdmg7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMzhweCBtaW5tYXgoMCwxZnIpfS5zaWRlYmFye2hlaWdodDoxMDB2aDtwb3NpdGlvbjpzdGlja3k7dG9wOjA7YmFja2dyb3VuZDojMGIwZTEyO2JvcmRlci1yaWdodDoxcHggc29saWQgIzIwMjczMDtwYWRkaW5nOjIzcHggMTRweCAxNXB4O2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjI4cHg7ei1pbmRleDoxMH0uYnJhbmR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtwYWRkaW5nOjAgOHB4fS5icmFuZG1hcmt7d2lkdGg6MzdweDtoZWlnaHQ6MzdweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY0YzBhLCNmZjliMjgpO2NvbG9yOiMxNDBkMDk7Zm9udC1zaXplOjIzcHg7Zm9udC13ZWlnaHQ6MTAwMDtjbGlwLXBhdGg6cG9seWdvbigwIDAsMTAwJSAwLDEwMCUgNzAlLDY0JSAxMDAlLDAgMTAwJSk7Ym94LXNoYWRvdzowIDAgMjJweCAjZmY2NDFjMzN9LmJyYW5kbmFtZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo5NTA7bGV0dGVyLXNwYWNpbmc6LjVweH0uYnJhbmRuYW1lIHNwYW57Y29sb3I6dmFyKC0tb3JhbmdlKX0uYnJhbmR0YWd7Zm9udC1zaXplOjhweDtjb2xvcjojYmFjNGNlO2xldHRlci1zcGFjaW5nOjEuOHB4O2ZvbnQtd2VpZ2h0OjgwMH0ubmF2Z3JvdXAgbGFiZWx7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMTJweDttYXJnaW4tYm90dG9tOjhweDtmb250LXNpemU6OXB4O2xldHRlci1zcGFjaW5nOjEuN3B4O2NvbG9yOiM5YWE3YjQ7Zm9udC13ZWlnaHQ6OTAwfS5uYXZ7ZGlzcGxheTpncmlkO2dhcDo0cHh9Lm5hdiBidXR0b257d2lkdGg6MTAwJTtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O2JhY2tncm91bmQ6bm9uZTt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTFweCAxMnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo2NTA7Zm9udC1zaXplOjEzcHh9Lm5hdiBidXR0b24gLm5pe3dpZHRoOjE5cHg7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjE3cHh9Lm5hdiBidXR0b246aG92ZXJ7YmFja2dyb3VuZDojMTkxZjI3O2NvbG9yOndoaXRlfS5uYXYgYnV0dG9uLmFjdGl2ZXtjb2xvcjojZmY5YTRkO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCMzYTFiMTAsIzIwMTUwZik7Ym9yZGVyLWNvbG9yOiM0OTI4MTk7Ym94LXNoYWRvdzppbnNldCAzcHggMCB2YXIoLS1vcmFuZ2UpfS5uYXYgLm5hdmNvdW50e21hcmdpbi1sZWZ0OmF1dG87YmFja2dyb3VuZDojNDgyNDE2O2NvbG9yOiNmZjliNWM7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzoycHggNnB4O2ZvbnQtc2l6ZToxMHB4fS5zaWRlYmFyLWZvb3R7bWFyZ2luLXRvcDphdXRvfS51c2VyY2FyZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6MTFweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjMTcxZDI0LCMxMDE0MTkpfS5hdmF0YXJ7d2lkdGg6MzlweDtoZWlnaHQ6MzlweDtmbGV4OjAgMCBhdXRvO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0MGRlZywjZmY4YzI3LCM1YjI0MTIpO2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjp3aGl0ZX0udXNlcmNhcmQgYntkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMnB4O292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzO21heC13aWR0aDoxMjBweH0udXNlcmNhcmQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5tYWlue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjE1MzBweDttaW4td2lkdGg6MDttYXJnaW46MCBhdXRvO3BhZGRpbmc6MjhweCBjbGFtcCgxN3B4LDMuMnZ3LDQ2cHgpIDU1cHh9LnRvcGJhcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxNnB4O3BhZGRpbmctYm90dG9tOjIxcHg7bWFyZ2luLWJvdHRvbToyM3B4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyNDJiMzN9LmtpY2tlcntmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoycHg7Zm9udC13ZWlnaHQ6OTAwO2NvbG9yOnZhcigtLW9yYW5nZTIpfS50b3BiYXIgaDF7Zm9udC1zaXplOmNsYW1wKDI0cHgsM3Z3LDM1cHgpO2xldHRlci1zcGFjaW5nOi0xLjFweDtsaW5lLWhlaWdodDoxLjE7bWFyZ2luOjdweCAwIDA7Zm9udC13ZWlnaHQ6OTUwfS50b3BhY3Rpb25ze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0uaWNvbmJ0biwuYnRuLXNlY29uZGFyeXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDQ7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6MTFweDtjb2xvcjojZjRmNmY4O3BhZGRpbmc6MTFweCAxNHB4fS5pY29uYnRue3dpZHRoOjQzcHg7aGVpZ2h0OjQzcHg7cGFkZGluZzowO3Bvc2l0aW9uOnJlbGF0aXZlO2ZvbnQtc2l6ZToxOHB4fS5waW5ne3Bvc2l0aW9uOmFic29sdXRlO3dpZHRoOjdweDtoZWlnaHQ6N3B4O2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtib3JkZXItcmFkaXVzOjUwJTtyaWdodDo4cHg7dG9wOjhweH0uYnRuLXByaW1hcnl7Ym9yZGVyOjA7Ym9yZGVyLXJhZGl1czoxMXB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjZmY1MDBiLCNmZjkwMjQpO2NvbG9yOiMxNjBlMDk7Zm9udC13ZWlnaHQ6OTAwO3BhZGRpbmc6MTNweCAxOHB4O2JveC1zaGFkb3c6MCA3cHggMjJweCAjZmY2NDFjMjY7dHJhbnNpdGlvbjpmaWx0ZXIgLjE1cyx0cmFuc2Zvcm0gLjE1c30uYnRuLXByaW1hcnk6aG92ZXJ7ZmlsdGVyOmJyaWdodG5lc3MoMS4wOCk7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoLTFweCl9LmJ0bi1zZWNvbmRhcnk6aG92ZXIsLmljb25idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiM4YzRhMjR9LmxheW91dHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDEuNTVmcikgbWlubWF4KDI3NXB4LDFmcik7Z2FwOjE3cHh9LnN0YWNre2Rpc3BsYXk6Z3JpZDthbGlnbi1jb250ZW50OnN0YXJ0O2dhcDoxNnB4fS5oZXJve3Bvc2l0aW9uOnJlbGF0aXZlO2lzb2xhdGlvbjppc29sYXRlO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0OjI3MHB4O2JvcmRlci1yYWRpdXM6MjBweDtwYWRkaW5nOmNsYW1wKDI0cHgsMy41dncsNDBweCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtib3JkZXI6MXB4IHNvbGlkICM2MzMzMWM7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTA1ZGVnLHJnYmEoNDcsMjMsMTMsLjk4KSxyZ2JhKDIyLDE5LDIyLC45OCkgNTglLHJnYmEoMTIsMTcsMjIsLjk4KSl9Lmhlcm86YmVmb3Jle2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7ei1pbmRleDotMTt3aWR0aDoyODBweDtoZWlnaHQ6MjgwcHg7cmlnaHQ6NnB4O3RvcDotNTNweDtib3JkZXI6MXB4IHNvbGlkICNmZjY0MWMyODt0cmFuc2Zvcm06cm90YXRlKDM4ZGVnKTtib3gtc2hhZG93OjAgMCAwIDIycHggI2ZmNjQxYzA3LDAgMCAwIDQ3cHggI2ZmNjQxYzA1fS5oZXJvOmFmdGVye2NvbnRlbnQ6IlMiO3Bvc2l0aW9uOmFic29sdXRlO3otaW5kZXg6LTE7cmlnaHQ6MjBweDt0b3A6LTU2cHg7Zm9udC1zaXplOjMwMHB4O2xpbmUtaGVpZ2h0OjE7Zm9udC13ZWlnaHQ6MTAwMDtjb2xvcjojZmY2NDFjMTg7dHJhbnNmb3JtOnNrZXcoLTEwZGVnKX0uaGVyby1jb3B5e21heC13aWR0aDo1MTVweH0uZXllYnJvd3tjb2xvcjojZmY5ODRjO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoyLjRweH0uaGVybyBoMntmb250LXNpemU6Y2xhbXAoMzJweCw0LjF2dyw1MHB4KTtsZXR0ZXItc3BhY2luZzotMS44cHg7bGluZS1oZWlnaHQ6MS4wMTttYXJnaW46MTRweCAwIDE0cHg7Zm9udC13ZWlnaHQ6MTAwMH0uaGVybyBoMiBzcGFue2NvbG9yOnZhcigtLW9yYW5nZSl9Lmhlcm8gcHtjb2xvcjojYzFjN2NlO2ZvbnQtc2l6ZToxNHB4O2xpbmUtaGVpZ2h0OjEuNjU7bWF4LXdpZHRoOjQ0MHB4O21hcmdpbjowIDAgMjFweH0uaGVyby1hY3Rpb25ze2Rpc3BsYXk6ZmxleDtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0uc2VjdGlvbntkaXNwbGF5Om5vbmU7YW5pbWF0aW9uOmFwcGVhciAuMThzIGVhc2V9LnNlY3Rpb24uYWN0aXZle2Rpc3BsYXk6YmxvY2t9QGtleWZyYW1lcyBhcHBlYXJ7ZnJvbXtvcGFjaXR5Oi40O3RyYW5zZm9ybTp0cmFuc2xhdGVZKDNweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTpub25lfX0uc3RhdHN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMyxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0uc3RhdHtwYWRkaW5nOjE2cHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1zdHJva2UpO2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE1MWIyMiwjMTAxNDE5KTttaW4td2lkdGg6MH0uc3RhdCBsYWJlbHtmb250LXNpemU6MTBweDtjb2xvcjojZDBkN2RmO2xldHRlci1zcGFjaW5nOi43cHg7Zm9udC13ZWlnaHQ6NzUwO2Rpc3BsYXk6YmxvY2t9LnN0YXQgc3Ryb25ne2ZvbnQtc2l6ZTpjbGFtcCgyMXB4LDJ2dywyOHB4KTtkaXNwbGF5OmJsb2NrO21hcmdpbjo1cHggMCAycHg7bGV0dGVyLXNwYWNpbmc6LS43cHg7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo5NTB9LnN0YXQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5jYXJke3BhZGRpbmc6MThweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czp2YXIoLS1yKTtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMCwjMTAxNDE5KTttaW4td2lkdGg6MH0uY2FyZGhlYWR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtnYXA6MTBweDttYXJnaW4tYm90dG9tOjE1cHh9LmNhcmRoZWFkIGgze2ZvbnQtc2l6ZToxNHB4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojg1MDtjb2xvcjojZjVmN2Y5fS50ZXh0bGlua3tib3JkZXI6MDtiYWNrZ3JvdW5kOm5vbmU7Y29sb3I6I2ZmOWE0ZDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3NTA7cGFkZGluZzo1cHh9LnRleHRsaW5rOmhvdmVye2NvbG9yOiNmZmMwOGF9Lm1hdGNocm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4O2JvcmRlcjoxcHggc29saWQgIzI5MzIzYjtib3JkZXItcmFkaXVzOjEzcHg7YmFja2dyb3VuZDojMDkwZDEyfS5tYXRjaGxvZ297d2lkdGg6NDZweDtoZWlnaHQ6NDZweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JvcmRlci1yYWRpdXM6MTJweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzQ0MjAwZSwjMjExNTBmKTtjb2xvcjojZmY4YTIyO2ZvbnQtc2l6ZToyMnB4O2ZsZXgtc2hyaW5rOjB9Lm1hdGNoY29weXtmbGV4OjE7bWluLXdpZHRoOjB9Lm1hdGNoY29weSBie2ZvbnQtc2l6ZToxMnB4O2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2Y1ZjdmOX0ubWF0Y2hjb3B5IHNtYWxse2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTBweDttYXJnaW4tdG9wOjRweH0ucGlsbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0Ojg1MDtsZXR0ZXItc3BhY2luZzouM3B4O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtiYWNrZ3JvdW5kOiMxNTMzMjY7Ym9yZGVyOjFweCBzb2xpZCAjMjQ0YTM4O2NvbG9yOiM2MGU1YTU7d2hpdGUtc3BhY2U6bm93cmFwfS5wYXNzY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTVkZWcsIzJjMTkwZiwjMTkxNTFhIDU4JSwjMTExNzFkKTtib3JkZXItY29sb3I6IzYwMzUxZjtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59LnBhc3NjYXJkOmFmdGVye2NvbnRlbnQ6IuKcpiI7cG9zaXRpb246YWJzb2x1dGU7cmlnaHQ6MTNweDt0b3A6LTE1cHg7Zm9udC1zaXplOjEwMHB4O2NvbG9yOiNmZjhhMjIxMDtwb2ludGVyLWV2ZW50czpub25lfS5wYXNzLXRvcHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjEycHg7YWxpZ24taXRlbXM6Y2VudGVyfS5wYXNzLWxldmVse2ZvbnQtc2l6ZTozN3B4O2ZvbnQtd2VpZ2h0OjEwMDA7Y29sb3I6dmFyKC0tb3JhbmdlMik7bGluZS1oZWlnaHQ6MX0ucGFzc2NhcmQgaDN7bWFyZ2luOjdweCAwIDZweDtmb250LXNpemU6MTdweH0ubXV0ZWR7Y29sb3I6dmFyKC0tbXV0ZWQpfS5zbWFsbHtmb250LXNpemU6MTFweH0ucGFzc2NhcmQgcHttYXJnaW46MDtjb2xvcjojYzhkMGQ4O2ZvbnQtc2l6ZToxMXB4fS5wcm9ncmVzc3toZWlnaHQ6OHB4O2JhY2tncm91bmQ6IzMwMzYzZTtib3JkZXItcmFkaXVzOjIwcHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbjoxN3B4IDAgOHB4fS5wcm9ncmVzcyBzcGFue2hlaWdodDoxMDAlO3dpZHRoOjQwJTtkaXNwbGF5OmJsb2NrO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCNmZjRlMGIsI2ZmOWEyOCk7Ym94LXNoYWRvdzowIDAgMTRweCAjZmY2NDFjNTU7Ym9yZGVyLXJhZGl1czoyMHB4fS5wYXNzLWZvb3R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtjb2xvcjojYzRjZGQ2O2ZvbnQtc2l6ZToxMXB4fS5sZWFkZXJsaXN0e2Rpc3BsYXk6Z3JpZH0ubGVhZGVye2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MjNweCBtaW5tYXgoMCwxZnIpIGF1dG87YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzoxMHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgIzI1MmQzNX0ubGVhZGVyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTowfS5yYW5re2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjojYzBjYWQ0fS5sZWFkZXI6bnRoLWNoaWxkKDEpIC5yYW5re2NvbG9yOnZhcigtLWdvbGQpfS5sZWFkZXI6bnRoLWNoaWxkKDIpIC5yYW5re2NvbG9yOiNkMWQ4ZGZ9LmxlYWRlcjpudGgtY2hpbGQoMykgLnJhbmt7Y29sb3I6I2Q5OTg2OX0ucGxheWVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweDttaW4td2lkdGg6MH0ubWluaWF2YXt3aWR0aDozM3B4O2hlaWdodDozM3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Ym9yZGVyLXJhZGl1czoxMHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMzAzYjQ3LCMxYTIwMjcpO2JvcmRlcjoxcHggc29saWQgIzNhNDY1Mjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4NTA7ZmxleC1zaHJpbms6MH0ucGxheWVyIGJ7ZGlzcGxheTpibG9jaztmb250LXNpemU6MTFweDt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9LnBsYXllciBzbWFsbHtkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNjMGNhZDQ7bWFyZ2luLXRvcDoycHh9LmVsb3tmb250LXdlaWdodDo5NTA7Zm9udC1zaXplOjEycHh9LnNlY3Rpb25oZWFke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7bWFyZ2luOjVweCAwIDE5cHh9LnNlY3Rpb25oZWFkIGgye2ZvbnQtc2l6ZToyNnB4O2xldHRlci1zcGFjaW5nOi0uN3B4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojk1MH0uc2VjdGlvbmhlYWQgcHtmb250LXNpemU6MTJweDtjb2xvcjojYzBjYWQ0O21hcmdpbjo1cHggMCAwfS5iYWRnZS1vcmFuZ2V7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGQ7Ym9yZGVyOjFweCBzb2xpZCAjNjkzNzFkO2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjkwMH0ubWFwZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg0LG1pbm1heCgwLDFmcikpO2dhcDoxMHB4fS5tYXB0aWxle3BhZGRpbmc6MDt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzE0MWIyMztib3JkZXItcmFkaXVzOjEzcHg7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfS5tYXBhcnR7aGVpZ2h0OjEwNXB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVuO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjNTQ0MDJkLCMyYTMwMzQgNTAlLCMxMTE2MWMpfS5tYXBhcnQ6YmVmb3JlLC5tYXBhcnQ6YWZ0ZXJ7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoxNDBweDtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6I2ZmZmZmZjEyO3RyYW5zZm9ybTpyb3RhdGUoLTM1ZGVnKX0ubWFwYXJ0OmFmdGVye3RyYW5zZm9ybTpyb3RhdGUoNDVkZWcpfS5tYXBhcnQgc3Bhbntmb250LXNpemU6MzNweDt6LWluZGV4OjE7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDVweCAjMDAwKX0ubWFwdGlsZSBie2Rpc3BsYXk6YmxvY2s7cGFkZGluZzoxMHB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNmMWY0ZjZ9Lm1hcHRpbGUuc2VsZWN0ZWR7Ym9yZGVyLWNvbG9yOnZhcigtLW9yYW5nZSk7Ym94LXNoYWRvdzowIDAgMCAxcHggdmFyKC0tb3JhbmdlKSwwIDhweCAyNHB4ICNmZjY0MWMxNX0ubWFwdGlsZS5zZWxlY3RlZCBie2NvbG9yOiNmZjliNGR9Lm1hcHRpbGUuc2VsZWN0ZWQ6YWZ0ZXJ7Y29udGVudDoi4pyTIjtwb3NpdGlvbjphYnNvbHV0ZTtyaWdodDo3cHg7dG9wOjdweDt3aWR0aDoyMnB4O2hlaWdodDoyMnB4O2JvcmRlci1yYWRpdXM6NTAlO2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7YmFja2dyb3VuZDp2YXIoLS1vcmFuZ2UpO2NvbG9yOiMxMzBkMDg7Zm9udC13ZWlnaHQ6MTAwMH0ubW9kZWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDo4cHg7bWFyZ2luOjIwcHggMH0ubW9kZXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTIxNzFkO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHggNXB4O2ZvbnQtd2VpZ2h0OjkwMDtjb2xvcjojZDNkOWRmfS5tb2RlLmFjdGl2ZXtib3JkZXItY29sb3I6Izk4NDkxZjtiYWNrZ3JvdW5kOiMzYjIwMTQ7Y29sb3I6I2ZmOWE0ZH0ucXVldWVib3h7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjNTkzMDFiO2JvcmRlci1yYWRpdXM6MTlweDtwYWRkaW5nOjMwcHggMjJweDtiYWNrZ3JvdW5kOnJhZGlhbC1ncmFkaWVudChlbGxpcHNlIGF0IDUwJSAwLCMzYTFjMTAsdHJhbnNwYXJlbnQgNTglKSxsaW5lYXItZ3JhZGllbnQoMTQ1ZGVnLCMxNTFhMjAsIzBlMTIxNil9LnF1ZXVlaWNvbnt3aWR0aDoxMDBweDtoZWlnaHQ6MTAwcHg7bWFyZ2luOjAgYXV0byAxOHB4O2JvcmRlcjoxcHggc29saWQgIzc1M2ExYztib3JkZXItcmFkaXVzOjUwJTtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2NvbG9yOiNmZjhjMjU7Zm9udC1zaXplOjM2cHg7Ym94LXNoYWRvdzowIDAgMCA5cHggI2ZmNjQxYzA4LDAgMCA0NXB4ICNmZjY0MWMxNX0ucXVldWVib3ggaDN7Zm9udC1zaXplOjIzcHg7bWFyZ2luOjhweCAwfS5xdWV1ZWJveCBwe2NvbG9yOiNjNGNkZDY7Zm9udC1zaXplOjEycHh9LnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo3cHg7ZmxleC13cmFwOndyYXA7bWFyZ2luLWJvdHRvbToxNXB4fS50YWJzIGJ1dHRvbntib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6OXB4O2NvbG9yOiNhZGI3YzE7cGFkZGluZzo5cHggMTNweDtmb250LXNpemU6MTFweH0udGFicyBidXR0b24uYWN0aXZle2JhY2tncm91bmQ6IzNhMjAxNDtib3JkZXItY29sb3I6IzgwNDAxZjtjb2xvcjojZmY5YTRkfS50YWJsZXdyYXB7b3ZlcmZsb3c6YXV0bztib3JkZXI6MXB4IHNvbGlkICMyYzM1NDA7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0udGFibGV7Ym9yZGVyLWNvbGxhcHNlOmNvbGxhcHNlO3dpZHRoOjEwMCU7bWluLXdpZHRoOjUyMHB4fS50YWJsZSB0aCwudGFibGUgdGR7dGV4dC1hbGlnbjpsZWZ0O3BhZGRpbmc6MTRweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjUyZDM2O2ZvbnQtc2l6ZToxMXB4fS50YWJsZSB0aHtiYWNrZ3JvdW5kOiMyMDI4MzM7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo4MDB9LnRhYmxlIHRke2NvbG9yOiNlOGVkZjJ9LnRhYmxlIHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTowfS50YWJsZSB0ci55b3V7YmFja2dyb3VuZDojMmIxYTExfS5wcm9maWxlaGVyb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MjBweDtib3JkZXI6MXB4IHNvbGlkICM2MDM1MWY7Ym9yZGVyLXJhZGl1czoxN3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjMmIxOTBmLCMxNTE5MWUpO21hcmdpbi1ib3R0b206MTVweH0ucHJvZmlsZWF2YXRhcnt3aWR0aDo3MnB4O2hlaWdodDo3MnB4O2JvcmRlci1yYWRpdXM6MTlweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY4ZDI2LCM1YjIxMTEpO2ZvbnQtc2l6ZToyOHB4O2JveC1zaGFkb3c6MCAwIDAgNHB4ICNmZjY0MWMxOH0ucHJvZmlsZWhlcm8gaDN7bWFyZ2luOjAgMCA1cHg7Zm9udC1zaXplOjIwcHh9LnByb2ZpbGVoZXJvIHB7bWFyZ2luOjAgMCA4cHg7Zm9udC1zaXplOjExcHg7Y29sb3I6I2FlYjhjMn0ucHJvZmlsZWVsb3ttYXJnaW4tbGVmdDphdXRvO3RleHQtYWxpZ246cmlnaHR9LnByb2ZpbGVlbG8gc21hbGx7Zm9udC1zaXplOjlweDtjb2xvcjojYWViOGMyO2xldHRlci1zcGFjaW5nOjFweH0ucHJvZmlsZWVsbyBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MzBweDtjb2xvcjojZmY5YTRkfS5mb3JtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9LmZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6N3B4O21hcmdpbjoxMHB4IDB9LmZpZWxkIGxhYmVse2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiMWJiYzV9LmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe3dpZHRoOjEwMCU7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzA4MGIxMDtjb2xvcjojZjRmNmY4O2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHg7b3V0bGluZTpub25lfS5maWVsZCBpbnB1dDpmb2N1c3tib3JkZXItY29sb3I6dmFyKC0tb3JhbmdlKX0uY2xhbmhlcm97ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTRweDtwYWRkaW5nOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCAjMmUzYjQ5O2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTBkZWcsIzE4MjQzMSwjMTExNjFjKX0uY2xhbmxvZ297d2lkdGg6NjRweDtoZWlnaHQ6NjRweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6IzFkMzA0Mjtib3JkZXItcmFkaXVzOjE3cHg7Zm9udC1zaXplOjMwcHh9LmNsYW5oZXJvIGgze21hcmdpbjo0cHggMDtmb250LXNpemU6MTlweH0uY2xhbmhlcm8gcHttYXJnaW46MDtjb2xvcjojYWFiNWMwO2ZvbnQtc2l6ZToxMXB4fS5jbGFuaGVybyAuZ3Jvd3tmbGV4OjF9Lm5vdGljZXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1zdGFydDtnYXA6MTFweDtwYWRkaW5nOjE0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjgzMDNhfS5ub3RpY2U6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOjB9Lm5vdGljZWljb3t3aWR0aDozN3B4O2hlaWdodDozN3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXItcmFkaXVzOjExcHg7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGR9Lm5vdGljZSBie2ZvbnQtc2l6ZToxMnB4fS5ub3RpY2UgcHttYXJnaW46NHB4IDA7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTFweH0uYm90dG9tbmF2e2Rpc3BsYXk6bm9uZX0udG9hc3R7cG9zaXRpb246Zml4ZWQ7ei1pbmRleDoxMDA7cmlnaHQ6MThweDtib3R0b206MThweDttYXgtd2lkdGg6Y2FsYygxMDB2dyAtIDM2cHgpO2JhY2tncm91bmQ6IzE3MWYyNjtib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTE7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHZhcigtLW9yYW5nZSk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTNweCAxNnB4O2JveC1zaGFkb3c6MCAxMnB4IDQwcHggIzAwMDk7Y29sb3I6I2Y1ZjdmODtmb250LXNpemU6MTJweDthbmltYXRpb246YXBwZWFyIC4yc30uZm9vdG5vdGV7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNDttYXJnaW4tdG9wOjEycHg7bGluZS1oZWlnaHQ6MS41fQpAbWVkaWEobWF4LXdpZHRoOjEwNTBweCl7LnNoZWxse2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMDVweCBtaW5tYXgoMCwxZnIpfS5sYXlvdXR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uc2lkZWhpZGV7ZGlzcGxheTpub25lfX0KQG1lZGlhKG1heC13aWR0aDo3MDBweCl7LnNoZWxse2Rpc3BsYXk6YmxvY2t9LnNpZGViYXJ7ZGlzcGxheTpub25lfS5tYWlue3BhZGRpbmc6MjBweCAxNXB4IGNhbGMoOTRweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSl9LnRvcGJhcntwYWRkaW5nLWJvdHRvbToxNnB4O21hcmdpbi1ib3R0b206MThweH0udG9wYmFyIGgxe2ZvbnQtc2l6ZToyNXB4fS50b3BhY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2Rpc3BsYXk6bm9uZX0ubGF5b3V0e2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnI7Z2FwOjEzcHh9LnN0YWNre2dhcDoxM3B4fS5oZXJve21pbi1oZWlnaHQ6MzAwcHg7cGFkZGluZzoyNHB4IDIxcHg7Ym9yZGVyLXJhZGl1czoyMHB4fS5oZXJvOmJlZm9yZXtyaWdodDotOTBweDt0b3A6LThweDt3aWR0aDoyMzBweDtoZWlnaHQ6MjMwcHh9Lmhlcm86YWZ0ZXJ7cmlnaHQ6LTlweDt0b3A6MTJweDtmb250LXNpemU6MjIwcHh9Lmhlcm8gaDJ7Zm9udC1zaXplOjM3cHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvIHB7Zm9udC1zaXplOjEzcHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvLWFjdGlvbnN7Z2FwOjhweH0uaGVyby1hY3Rpb25zIGJ1dHRvbntwYWRkaW5nOjEycHggMTNweDtmb250LXNpemU6MTJweH0uc3RhdHN7Z2FwOjhweH0uc3RhdHtwYWRkaW5nOjEzcHggMTBweDtib3JkZXItcmFkaXVzOjE1cHh9LnN0YXQgbGFiZWx7Zm9udC1zaXplOjlweDtsZXR0ZXItc3BhY2luZzouM3B4fS5zdGF0IHN0cm9uZ3tmb250LXNpemU6MjFweH0uc3RhdCBzbWFsbHtmb250LXNpemU6OXB4O2xpbmUtaGVpZ2h0OjEuMztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6NHB4fS5jYXJke3BhZGRpbmc6MTVweDtib3JkZXItcmFkaXVzOjE2cHh9LnBhc3NjYXJke3BhZGRpbmc6MThweH0ubWFwZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDIsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9Lm1hcGFydHtoZWlnaHQ6OTNweH0uc2VjdGlvbmhlYWQgaDJ7Zm9udC1zaXplOjI0cHh9LnNlY3Rpb25oZWFkIHB7bWF4LXdpZHRoOjI1MHB4O2xpbmUtaGVpZ2h0OjEuNX0ucXVldWVib3h7cGFkZGluZzoyNXB4IDE1cHh9Lm1vZGVncmlke2dhcDo2cHh9Lm1vZGV7cGFkZGluZzoxMnB4IDJweDtmb250LXNpemU6MTJweH0ucHJvZmlsZWhlcm97cGFkZGluZzoxNXB4O2dhcDoxMXB4fS5wcm9maWxlYXZhdGFye3dpZHRoOjU3cHg7aGVpZ2h0OjU3cHg7Zm9udC1zaXplOjIzcHh9LnByb2ZpbGVoZXJvIGgze2ZvbnQtc2l6ZToxN3B4fS5wcm9maWxlZWxvIHN0cm9uZ3tmb250LXNpemU6MjRweH0uZm9ybWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uYm90dG9tbmF2e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsMWZyKTtwb3NpdGlvbjpmaXhlZDt6LWluZGV4OjUwO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3BhZGRpbmc6OHB4IDRweCBjYWxjKDhweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7YmFja2dyb3VuZDpyZ2JhKDgsMTAsMTMsLjk3KTtiYWNrZHJvcC1maWx0ZXI6Ymx1cigxNnB4KTtib3JkZXItdG9wOjFweCBzb2xpZCAjMjkzMjNkO2JveC1zaGFkb3c6MCAtOXB4IDI1cHggIzAwMDV9LmJvdHRvbW5hdiBidXR0b257ZGlzcGxheTpncmlkO2p1c3RpZnktaXRlbXM6Y2VudGVyO2dhcDozcHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjowO2NvbG9yOiNiYWM0Y2U7Zm9udC1zaXplOjlweDtwYWRkaW5nOjRweCAwfS5ib3R0b21uYXYgYnV0dG9uIHNwYW57Zm9udC1zaXplOjE5cHh9LmJvdHRvbW5hdiBidXR0b24uYWN0aXZle2NvbG9yOiNmZjlhNGR9LnRhYmxlIHRoLC50YWJsZSB0ZHtwYWRkaW5nOjEycHh9LnBhc3MtZm9vdHtmb250LXNpemU6MTBweH19CgovKiBDb250cmFzdCBoYXJkZW5pbmcgZm9yIGVtYmVkZGVkIFRlbGVncmFtL1B1enpsZS5ib3Qgd2Vidmlld3MgKi8KOnJvb3R7LS10ZXh0OiNmZmY7LS1tdXRlZDojYzBjYWQ0Oy0tc3Ryb2tlOiMzYTQ2NTN9CmJvZHl7Y29sb3I6I2ZmZn0KaDEsaDIsaDMsaDQsYixzdHJvbmcsLmJyYW5kbmFtZSwubWF0Y2hjb3B5IGIsLnBsYXllciBiLC5jYXJkaGVhZCBoMywuc2VjdGlvbmhlYWQgaDJ7Y29sb3I6I2ZmZn0KcHtjb2xvcjojZDBkN2RmfQpsYWJlbHtjb2xvcjojZDBkN2RmfQouY2FyZCwuc3RhdCwudGFibGV3cmFwLC5xdWV1ZWJveCwubWF0Y2hyb3d7Y29sb3I6I2ZmZn0KLnN0YXQgbGFiZWwsLm1hdGNoY29weSBzbWFsbCwucGxheWVyIHNtYWxsLC5zZWN0aW9uaGVhZCBwLC5tdXRlZCwuZm9vdG5vdGV7Y29sb3I6I2I5YzVkMH0KLnRhYmxlIHRoe2NvbG9yOiNlMGU2ZWN9Ci50YWJsZSB0ZHtjb2xvcjojZjdmOWZifQoubmF2IGJ1dHRvbntjb2xvcjojZDJkOWUxfQoubmF2IGJ1dHRvbi5hY3RpdmV7Y29sb3I6I2ZmYjA2Zn0KLmJ0bi1zZWNvbmRhcnl7Y29sb3I6I2ZmZjtiYWNrZ3JvdW5kOiMxYjIyMmI7Ym9yZGVyLWNvbG9yOiM0NjUzNjF9Ci50ZXh0bGlua3tjb2xvcjojZmZhZDcwfQouZmllbGQgbGFiZWx7Y29sb3I6I2Q1ZGNlNH0KLmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe2NvbG9yOiNmZmY7YmFja2dyb3VuZDojMDgwYzExO2JvcmRlci1jb2xvcjojNDY1MzYxfQoucGlsbHtjb2xvcjojNzVmMGI2fQouc3RhdCBzbWFsbHtjb2xvcjojNzVlYWIwfQoubm90aWNlIHB7Y29sb3I6I2MyY2NkNn0KLnBhc3NjYXJkIHB7Y29sb3I6I2QwZDdkZn0KCgovKiBGb3JjZSBkYXJrIGNhbnZhcyBpbiBlbWJlZGRlZCBidWlsZGVycy93ZWJ2aWV3cyAoaW5jbHVkaW5nIHNhZmUgYXJlYXMpICovCmh0bWwsYm9keXtiYWNrZ3JvdW5kLWNvbG9yOiMwODBhMGQhaW1wb3J0YW50O2NvbG9yOiNmZmYhaW1wb3J0YW50fQpib2R5e2JhY2tncm91bmQtaW1hZ2U6cmFkaWFsLWdyYWRpZW50KGVsbGlwc2UgYXQgMTAwJSAwLHJnYmEoMjU1LDgwLDE1LC4xMiksdHJhbnNwYXJlbnQgMjclKSFpbXBvcnRhbnR9CiNhcHAsLmFwcCwuY29udGFpbmVyLC5zaGVsbCwubWFpbntiYWNrZ3JvdW5kLWNvbG9yOnRyYW5zcGFyZW50fQppbnB1dCxzZWxlY3QsdGV4dGFyZWF7Y29sb3Itc2NoZW1lOmRhcmt9Cjo6c2VsZWN0aW9ue2JhY2tncm91bmQ6I2ZmNjQxYztjb2xvcjojMDgwYTBkfQoKCi8qIEhBUkQgT1ZFUlJJREVTOiBQdXp6bGUuYm90IC8gVGVsZWdyYW0gV2ViVmlldyBob3N0IHN0eWxlcyAqLwo6cm9vdCwgaHRtbCwgYm9keSB7CiAgYmFja2dyb3VuZDogIzA4MGEwZCAhaW1wb3J0YW50OwogIGJhY2tncm91bmQtY29sb3I6ICMwODBhMGQgIWltcG9ydGFudDsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50OwogIGNvbG9yLXNjaGVtZTogZGFyayAhaW1wb3J0YW50Owp9Cmh0bWwsIGJvZHkgewogIG1pbi1oZWlnaHQ6IDEwMCUgIWltcG9ydGFudDsKICBtYXJnaW46IDAgIWltcG9ydGFudDsKfQpib2R5IHsKICBiYWNrZ3JvdW5kLWltYWdlOiByYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAsIHJnYmEoMjU1LDgwLDE1LC4xMCksIHRyYW5zcGFyZW50IDMwJSkgIWltcG9ydGFudDsKfQouc2hlbGwsIC5tYWluLCBtYWluLCBzZWN0aW9uLCBhcnRpY2xlLCAucGFnZSwgLmNvbnRlbnQsIC5jb250YWluZXIsICNhcHAsICNyb290IHsKICBjb2xvcjogI2Y3ZjlmYzsKfQpoMSwgaDIsIGgzLCBoNCwgaDUsIGg2LCBwLCBsYWJlbCwgc3Bhbiwgc21hbGwsIHN0cm9uZywgYiwgbGksIHRkLCB0aCB7CiAgdGV4dC1zaGFkb3c6IG5vbmU7Cn0KLnRvcGJhciBoMSwgLmNhcmQgaDIsIC5jYXJkIGgzLCAuc2VjdGlvbmhlYWQgaDIsIC5oZXJvIGgyLAouc3RhdCBzdHJvbmcsIC5tYXRjaGNvcHkgYiwgLnBsYXllciBiLCAuYnJhbmRuYW1lIHsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50Owp9Ci5raWNrZXIsIC50ZXh0bGluaywgLnNlY3Rpb25oZWFkIGEgeyBjb2xvcjogI2ZmYWI3MCAhaW1wb3J0YW50OyB9Ci5tdXRlZCwgLmZvb3Rub3RlLCAuc2VjdGlvbmhlYWQgcCwgLm1hdGNoY29weSBzbWFsbCwgLnBsYXllciBzbWFsbCwKLnN0YXQgbGFiZWwsIC5zdGF0IHNtYWxsLCAuY2FyZCBwLCAuY2FyZCBzbWFsbCB7CiAgY29sb3I6ICNjMmNjZDYgIWltcG9ydGFudDsKfQouY2FyZCwgLnN0YXQsIC5xdWV1ZWJveCwgLnRhYmxld3JhcCwgLm1hdGNocm93LCAubm90aWNlLCAucGFzc2NhcmQgewogIGNvbG9yOiAjZjdmOWZjOwp9CkBtZWRpYSAobWF4LXdpZHRoOiA3NjBweCkgewogIGh0bWwsIGJvZHksIC5zaGVsbCwgLm1haW4geyBiYWNrZ3JvdW5kLWNvbG9yOiAjMDgwYTBkICFpbXBvcnRhbnQ7IH0KfQoKCi8qIEZJTkFMIFJFU1BPTlNJVkUgKyBDTEFOIFJPU1RFUiBGSVggKi8KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIHsgd2lkdGg6MTAwJTsgbWluLXdpZHRoOjA7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBkaXNwbGF5OmdyaWQgIWltcG9ydGFudDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDFmcikgbWF4LWNvbnRlbnQgIWltcG9ydGFudDsgYWxpZ24taXRlbXM6Y2VudGVyICFpbXBvcnRhbnQ7IGNvbHVtbi1nYXA6MTZweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MDsgcGFkZGluZzoxM3B4IDJweCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHsgZGlzcGxheTpmbGV4ICFpbXBvcnRhbnQ7IGFsaWduLWl0ZW1zOmNlbnRlciAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MCAhaW1wb3J0YW50OyBvdmVyZmxvdzpoaWRkZW47IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgPiBkaXY6bGFzdC1jaGlsZCB7IG1pbi13aWR0aDowICFpbXBvcnRhbnQ7IGZsZXg6MSAxIGF1dG8gIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAjY2xhbk1lbWJlcnMgLnBsYXllciBiIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgZm9udC13ZWlnaHQ6ODAwICFpbXBvcnRhbnQ7IHdoaXRlLXNwYWNlOm5vcm1hbCAhaW1wb3J0YW50OyBvdmVyZmxvdy13cmFwOmFueXdoZXJlICFpbXBvcnRhbnQ7IHdvcmQtYnJlYWs6YnJlYWstd29yZCAhaW1wb3J0YW50OyBvdmVyZmxvdzp2aXNpYmxlICFpbXBvcnRhbnQ7IHRleHQtb3ZlcmZsb3c6Y2xpcCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHNtYWxsIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBtYXJnaW4tdG9wOjRweCAhaW1wb3J0YW50OyBjb2xvcjojYzJjY2Q2ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxMXB4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgd2hpdGUtc3BhY2U6bm9ybWFsICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5taW5pYXZhIHsgd2lkdGg6NDBweCAhaW1wb3J0YW50OyBoZWlnaHQ6NDBweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA0MHB4ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5lbG8geyBkaXNwbGF5OmJsb2NrICFpbXBvcnRhbnQ7IGp1c3RpZnktc2VsZjplbmQgIWltcG9ydGFudDsgYWxpZ24tc2VsZjpjZW50ZXIgIWltcG9ydGFudDsgbWluLXdpZHRoOjQ0cHg7IHBhZGRpbmc6N3B4IDlweCAhaW1wb3J0YW50OyBib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTM7IGJvcmRlci1yYWRpdXM6OXB4OyBiYWNrZ3JvdW5kOiMyMDI4MzM7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgdGV4dC1hbGlnbjpyaWdodCAhaW1wb3J0YW50OyB3aGl0ZS1zcGFjZTpub3dyYXAgIWltcG9ydGFudDsgZm9udC1zaXplOjEzcHggIWltcG9ydGFudDsgbGluZS1oZWlnaHQ6MS4yICFpbXBvcnRhbnQ7IGZvbnQtd2VpZ2h0OjkwMCAhaW1wb3J0YW50OyBmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7IH0KI3BhZ2UtY2xhbnMgLmNhcmQgeyBtaW4td2lkdGg6MDsgb3ZlcmZsb3c6aGlkZGVuOyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCB7IGdhcDoxMnB4OyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCBoMyB7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8geyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gLmdyb3cgeyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gaDMgeyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IG92ZXJmbG93LXdyYXA6YW55d2hlcmU7IH0KI3BhZ2UtY2xhbnMgLmNsYW5oZXJvIHAsICNwYWdlLWNsYW5zIC5zZWN0aW9uaGVhZCBwIHsgY29sb3I6I2MyY2NkNiAhaW1wb3J0YW50OyB9Cmh0bWwsIGJvZHkgeyB3aWR0aDoxMDAlOyBtYXgtd2lkdGg6MTAwJTsgb3ZlcmZsb3cteDpoaWRkZW47IH0KaW1nLCBzdmcsIHZpZGVvLCBjYW52YXMsIGJ1dHRvbiwgaW5wdXQsIHNlbGVjdCwgdGV4dGFyZWEgeyBtYXgtd2lkdGg6MTAwJTsgfQpAbWVkaWEgKG1heC13aWR0aDo2MDBweCkgewogLnNlY3Rpb24geyBwYWRkaW5nLWxlZnQ6MTRweDsgcGFkZGluZy1yaWdodDoxNHB4OyB9CiAuc2VjdGlvbmhlYWQgeyBhbGlnbi1pdGVtczpmbGV4LXN0YXJ0OyBmbGV4LXdyYXA6d3JhcDsgZ2FwOjEwcHg7IH0KIC5zZWN0aW9uaGVhZCBoMiB7IGZvbnQtc2l6ZToyM3B4OyBsaW5lLWhlaWdodDoxLjI7IH0KIC5zZWN0aW9uaGVhZCBwIHsgZm9udC1zaXplOjEzcHg7IGxpbmUtaGVpZ2h0OjEuNTsgfQogLmxheW91dCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczptaW5tYXgoMCwxZnIpICFpbXBvcnRhbnQ7IGdhcDoxNHB4OyB9CiAuY2FyZCB7IG1pbi13aWR0aDowOyBwYWRkaW5nOjE1cHg7IH0KIC5jbGFuaGVybyB7IHBhZGRpbmc6MTRweCAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBmbGV4LXdyYXA6d3JhcDsgfQogLmNsYW5sb2dvIHsgd2lkdGg6NTJweCAhaW1wb3J0YW50OyBoZWlnaHQ6NTJweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA1MnB4OyBmb250LXNpemU6MjVweCAhaW1wb3J0YW50OyB9CiAuY2xhbmhlcm8gLmdyb3cgeyBmbGV4OjEgMSBjYWxjKDEwMCUgLSA3NXB4KTsgfQogLmNsYW5oZXJvIGgzIHsgZm9udC1zaXplOjE3cHggIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBjb2x1bW4tZ2FwOjEwcHggIWltcG9ydGFudDsgcGFkZGluZzoxMnB4IDAgIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgYiB7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IH0KICNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAuZWxvIHsgbWluLXdpZHRoOjQycHg7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IHBhZGRpbmc6N3B4ICFpbXBvcnRhbnQ7IH0KIC5tYXBncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpOyB9Cn0KCgovKiBNYXRjaCBDZW50ZXIg4oCUIGRlZGljYXRlZCBjb21wZXRpdGl2ZSBmbG93ICovCi5tYXRjaC1jZW50ZXJ7ZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXI6MXB4IHNvbGlkICMzYTQxNGI7Ym9yZGVyLXJhZGl1czoxOHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMSwjMGUxMjE3KTtib3gtc2hhZG93OjAgMThweCA0NXB4ICMwMDA1fQoubWF0Y2gtY2VudGVyLnZpc2libGV7ZGlzcGxheTpibG9ja30ubWMtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjE0cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTdweCAxOXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyOTMxM2E7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTAwZGVnLCMyNDE1MGYsIzE0MTkxZil9Lm1jLXRpdGxle2ZvbnQtd2VpZ2h0Ojk1MDtmb250LXNpemU6MThweH0ubWMtc3Vie2ZvbnQtc2l6ZToxMnB4O2NvbG9yOiNhZWI4YzM7bWFyZ2luLXRvcDo0cHh9Lm1jLXN0YXR1c3tib3JkZXI6MXB4IHNvbGlkICM3MDQwMWY7Y29sb3I6I2ZmYWQ2OTtiYWNrZ3JvdW5kOiMzNTFlMTM7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo2cHggOXB4O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjkwMDtsZXR0ZXItc3BhY2luZzouN3B4O3doaXRlLXNwYWNlOm5vd3JhcH0ubWMtYm9keXtwYWRkaW5nOjE4cHg7ZGlzcGxheTpncmlkO2dhcDoxNnB4fS5tYy1wcm9ncmVzc3toZWlnaHQ6N3B4O2JvcmRlci1yYWRpdXM6OTlweDtiYWNrZ3JvdW5kOiMyNTJjMzQ7b3ZlcmZsb3c6aGlkZGVufS5tYy1wcm9ncmVzcyBzcGFue2Rpc3BsYXk6YmxvY2s7aGVpZ2h0OjEwMCU7d2lkdGg6MDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjZmY0ZTBiLCNmZjlhMjcpO3RyYW5zaXRpb246d2lkdGggLjJzfS5tYy1wbGF5ZXJncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtcGxheWVye21pbi13aWR0aDowO2JvcmRlcjoxcHggc29saWQgIzMwMzk0NDtiYWNrZ3JvdW5kOiMxNzFkMjQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7dGV4dC1hbGlnbjpjZW50ZXJ9Lm1jLXBsYXllci5hY2NlcHRlZHtib3JkZXItY29sb3I6IzIzNzk1NztiYWNrZ3JvdW5kOiMxMTI3MWZ9Lm1jLXBsYXllciBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfS5tYy1wbGF5ZXIgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojOThhNWIxO2ZvbnQtc2l6ZTo5cHg7bWFyZ2luLXRvcDo0cHh9Lm1jLXBsYXllciAubWMtYXZhdGFye3dpZHRoOjI4cHg7aGVpZ2h0OjI4cHg7Ym9yZGVyLXJhZGl1czo5cHg7bWFyZ2luOjAgYXV0byA2cHg7ZGlzcGxheTpncmlkO3BsYWNlLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMzMDIwMTY7Y29sb3I6I2ZmOWI1MDtmb250LXdlaWdodDo5NTB9Lm1jLWFjdGlvbnN7ZGlzcGxheTpmbGV4O2dhcDo5cHg7ZmxleC13cmFwOndyYXB9Lm1jLWFjdGlvbnMgLmJ0bi1wcmltYXJ5LC5tYy1hY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2ZsZXg6MTttaW4td2lkdGg6MTMwcHh9Lm1jLXR3b3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9Lm1jLXBhbmVse2JvcmRlcjoxcHggc29saWQgIzJiMzMzZDtib3JkZXItcmFkaXVzOjEzcHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6IzExMTYxY30ubWMtcGFuZWwgaDR7bWFyZ2luOjAgMCA5cHg7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjhweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmOWE0ZH0ubWMtbXV0ZWR7Y29sb3I6IzlkYTliNTtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6NnB4O21hcmdpbjo5cHggMH0ubWMtZmllbGQgbGFiZWx7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNTtsZXR0ZXItc3BhY2luZzouN3B4O2ZvbnQtd2VpZ2h0OjgwMH0ubWMtZmllbGQgc2VsZWN0LC5tYy1maWVsZCBpbnB1dCwubWMtZmllbGQgdGV4dGFyZWF7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMwYjBmMTM7Ym9yZGVyOjFweCBzb2xpZCAjMzQzZDQ3O2JvcmRlci1yYWRpdXM6OHB4O2NvbG9yOiNmNGY2Zjg7cGFkZGluZzoxMHB4O21pbi13aWR0aDowfS5tYy1tYXBze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtbWFwe2JvcmRlcjoxcHggc29saWQgIzMzM2Q0ODtiYWNrZ3JvdW5kOiMxODFmMjc7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMXB4fS5tYy1tYXA6aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZjc4MmF9Lm1jLW1hcC51c2Vke29wYWNpdHk6LjM1O3BvaW50ZXItZXZlbnRzOm5vbmU7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaH0ubWMtbG9ne2Rpc3BsYXk6Z3JpZDtnYXA6NnB4fS5tYy1sb2dyb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7Zm9udC1zaXplOjExcHg7cGFkZGluZzo4cHggOXB4O2JvcmRlci1yYWRpdXM6OHB4O2JhY2tncm91bmQ6IzFhMjAyOH0ubWMtbG9ncm93IHNwYW46bGFzdC1jaGlsZHtjb2xvcjojZmZhZDY5fS5tYy10ZWFte2JvcmRlcjoxcHggc29saWQgIzJlMzk0NDtiYWNrZ3JvdW5kOiMxNTFiMjI7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTJweH0ubWMtdGVhbSBoNHttYXJnaW46MCAwIDhweDtmb250LXNpemU6MTJweH0ubWMtdGVhbSB1bHttYXJnaW46MDtwYWRkaW5nLWxlZnQ6MThweDtjb2xvcjojYzNjYmQzO2ZvbnQtc2l6ZToxMnB4O2xpbmUtaGVpZ2h0OjEuOX0ubWMtZm9ybWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo5cHh9Lm1jLW5vdGljZXtib3JkZXI6MXB4IHNvbGlkICMzYzMwMjM7YmFja2dyb3VuZDojMjQxYjE0O2NvbG9yOiNlNmM5YWQ7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6MTBweDtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWhpZGRlbntkaXNwbGF5Om5vbmUhaW1wb3J0YW50fQpAbWVkaWEobWF4LXdpZHRoOjYwMHB4KXsubWMtYm9keXtwYWRkaW5nOjEzcHh9Lm1jLXBsYXllcmdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg1LG1pbm1heCgwLDFmcikpO2dhcDo0cHh9Lm1jLXBsYXllcntwYWRkaW5nOjhweCAycHh9Lm1jLXBsYXllciBie2ZvbnQtc2l6ZTo5cHh9Lm1jLW1hcHN7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5tYy10d297Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0ubWMtaGVhZHtwYWRkaW5nOjE0cHh9Lm1jLXRpdGxle2ZvbnQtc2l6ZToxNnB4fX0KCgouY29zbWV0aWMtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLG1pbm1heCgwLDFmcikpO2dhcDoxMnB4fS5jb3NtZXRpYy1pdGVte2JvcmRlcjoxcHggc29saWQgIzM1NDE0ZDtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxNHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMTcxZTI2LCMwZTEyMTcpO21pbi13aWR0aDowfS5jb3NtZXRpYy1hcnR7aGVpZ2h0Ojk1cHg7Ym9yZGVyLXJhZGl1czoxMnB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Zm9udC1zaXplOjM2cHg7bWFyZ2luLWJvdHRvbToxMnB4O2JhY2tncm91bmQ6cmFkaWFsLWdyYWRpZW50KGNpcmNsZSBhdCA1MCUgMjAlLCM2MTM1MWUsIzFhMWQyMiA3MiUpO2JvcmRlcjoxcHggc29saWQgIzZhM2EyMH0uY29zbWV0aWMtaXRlbSBoNHttYXJnaW46MCAwIDVweDtmb250LXNpemU6MTNweH0uY29zbWV0aWMtaXRlbSBwe2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiZGM4ZDI7bWluLWhlaWdodDozMnB4O21hcmdpbjowIDAgMTBweH0uY29zbWV0aWMtbWV0YXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNmZmFkNzB9LmNvc21ldGljLWl0ZW0gYnV0dG9ue3dpZHRoOjEwMCU7Zm9udC1zaXplOjExcHg7cGFkZGluZzoxMHB4IDhweH0ucmFyaXR5e2ZvbnQtc2l6ZTo5cHg7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi43cHg7Y29sb3I6I2ZmY2E2OH0udHJvcGh5LWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNSxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0udHJvcGh5e3BhZGRpbmc6MTNweCA4cHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjMzk0NjUzO2JvcmRlci1yYWRpdXM6MTNweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzFiMjMyYywjMTAxNDE5KX0udHJvcGh5IC5jdXB7Zm9udC1zaXplOjMwcHg7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDdweCAjMDAwOCl9LnRyb3BoeSBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7bWFyZ2luOjZweCAwIDNweH0udHJvcGh5IHNtYWxse2ZvbnQtc2l6ZTo5cHg7Y29sb3I6I2JhYzVjZjtkaXNwbGF5OmJsb2NrfS5zdGF0LWRldGFpbC1ncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9LnN0YXQtZGV0YWlse3BhZGRpbmc6MTRweDtib3JkZXI6MXB4IHNvbGlkICMzNDQwNGM7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0uc3RhdC1kZXRhaWwgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojYjdjMmNjO2ZvbnQtc2l6ZToxMHB4fS5zdGF0LWRldGFpbCBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MjJweDttYXJnaW4tdG9wOjdweDtjb2xvcjojZmY5YTRkfS5jb3NtZXRpYy1lbXB0eXtncmlkLWNvbHVtbjoxLy0xO3BhZGRpbmc6MjBweDt0ZXh0LWFsaWduOmNlbnRlcjtjb2xvcjojYjljNWQwO2JvcmRlcjoxcHggZGFzaGVkICMzOTQ2NTM7Ym9yZGVyLXJhZGl1czoxMnB4fUBtZWRpYShtYXgtd2lkdGg6NzAwcHgpey5jb3NtZXRpYy1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX0udHJvcGh5LWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5zdGF0LWRldGFpbC1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX19Cgo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJzaGVsbCI+Cjxhc2lkZSBjbGFzcz0ic2lkZWJhciI+CiAgPGRpdiBjbGFzcz0iYnJhbmQiPjxkaXYgY2xhc3M9ImJyYW5kbWFyayI+UzwvZGl2PjxkaXY+PGRpdiBjbGFzcz0iYnJhbmRuYW1lIj5TVEFORDxzcGFuPlBPSU5UPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImJyYW5kdGFnIj5TVEFORE9GRiAyIOKAoiBDT01QRVRJVElWRTwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9Im5hdmdyb3VwIj48bGFiZWw+0J/Qm9CQ0KLQpNCe0KDQnNCQPC9sYWJlbD48bmF2IGNsYXNzPSJuYXYiIGlkPSJkZXNrdG9wTmF2Ij4KICAgIDxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS1wYWdlPSJob21lIj48c3BhbiBjbGFzcz0ibmkiPuKMgjwvc3Bhbj7Qk9C70LDQstC90LDRjzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9Im1hdGNoZXMiPjxzcGFuIGNsYXNzPSJuaSI+4pqUPC9zcGFuPtCc0LDRgtGH0Lg8L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJyYXRpbmciPjxzcGFuIGNsYXNzPSJuaSI+4pmcPC9zcGFuPtCg0LXQudGC0LjQvdCzIEVMTzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9ImNsYW5zIj48c3BhbiBjbGFzcz0ibmkiPuKZojwvc3Bhbj7QmtC70LDQvdGLIDxzcGFuIGNsYXNzPSJuYXZjb3VudCI+TkVXPC9zcGFuPjwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InBhc3MiPjxzcGFuIGNsYXNzPSJuaSI+4pymPC9zcGFuPlNUQU5EUE9JTlQgUGFzczwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9ImZyaWVuZHMiPjxzcGFuIGNsYXNzPSJuaSI+4pmnPC9zcGFuPtCU0YDRg9C30YzRjzwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9Im5vdGlmaWNhdGlvbnMiPjxzcGFuIGNsYXNzPSJuaSI+4pmnPC9zcGFuPtCj0LLQtdC00L7QvNC70LXQvdC40Y8gPHNwYW4gY2xhc3M9Im5hdmNvdW50Ij4yPC9zcGFuPjwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InByb2ZpbGUiPjxzcGFuIGNsYXNzPSJuaSI+4peJPC9zcGFuPtCf0YDQvtGE0LjQu9GMPC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0iY29zbWV0aWNzIj48c3BhbiBjbGFzcz0ibmkiPuKcpzwvc3Bhbj7QnNCw0LPQsNC30LjQvSDQutC+0YHQvNC10YLQuNC60Lg8L2J1dHRvbj4KICA8L25hdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJzaWRlYmFyLWZvb3QiPjxkaXYgY2xhc3M9InVzZXJjYXJkIj48ZGl2IGNsYXNzPSJhdmF0YXIiPlM8L2Rpdj48ZGl2PjxiIGlkPSJzaWRlTmFtZSI+c3RhbmRwb2ludF9wbGF5ZXI8L2I+PHNtYWxsPuKXjyDQlNCV0JzQni3Qn9Cg0J7QpNCY0JvQrDwvc21hbGw+PC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7QotCS0J7QryDQmNCT0KDQkC4g0KLQktCe0K8g0J/QntCX0JjQptCY0K8uPC9wPjwvZGl2Pgo8L2FzaWRlPgo8bWFpbiBjbGFzcz0ibWFpbiI+CjxoZWFkZXIgY2xhc3M9InRvcGJhciI+PGRpdj48ZGl2IGNsYXNzPSJraWNrZXIiPlNUQU5ET0ZGIDIg4oCiIEVTUE9SVFMgUExBVEZPUk08L2Rpdj48aDEgaWQ9InBhZ2VUaXRsZSI+0KLQstC+0Y8g0LjQs9GA0LAuINCi0LLQvtGPINC/0L7Qt9C40YbQuNGPLjwvaDE+PC9kaXY+PGRpdiBjbGFzcz0idG9wYWN0aW9ucyI+PHNwYW4gaWQ9ImFwaVN0YXR1cyIgY2xhc3M9ImJhZGdlLW9yYW5nZSI+QVBJOiDQv9C+0LTQutC70Y7Rh9C10L3QuNC14oCmPC9zcGFuPjxidXR0b24gY2xhc3M9Imljb25idG4iIGRhdGEtcGFnZT0ibm90aWZpY2F0aW9ucyIgYXJpYS1sYWJlbD0i0KPQstC10LTQvtC80LvQtdC90LjRjyI+4pmnPGkgY2xhc3M9InBpbmciPjwvaT48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBkYXRhLXBhZ2U9InByb2ZpbGUiPtCc0L7QuSDQv9GA0L7RhNC40LvRjCDihpc8L2J1dHRvbj48L2Rpdj48L2hlYWRlcj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIGFjdGl2ZSIgaWQ9InBhZ2UtaG9tZSI+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiPgogIDxkaXYgY2xhc3M9InN0YWNrIj4KICAgPGRpdiBjbGFzcz0iaGVybyI+PGRpdiBjbGFzcz0iaGVyby1jb3B5Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQktCV0KDQqNCY0J3QlTwvZGl2PjxoMj7QmNCz0YDQsNC5INGD0LzQvdC10LUuPGJyPjxzcGFuPtCf0L7QtNC90LjQvNCw0LkgRUxPLjwvc3Bhbj48L2gyPjxwPtCd0LDRhdC+0LTQuCDQutC+0LzQsNC90LTRgywg0L/QvtCx0LXQttC00LDQuSDQsiDRgNC10LnRgtC40L3Qs9C+0LLRi9GFINC80LDRgtGH0LDRhSDQuCDQtNC+0LrQsNC30YvQstCw0LksINC60YLQviDQt9C00LXRgdGMINGB0LjQu9GM0L3QtdC50YjQuNC5LjwvcD48ZGl2IGNsYXNzPSJoZXJvLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBkYXRhLXBhZ2U9Im1hdGNoZXMiPuKalCDQndCw0LnRgtC4INC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgZGF0YS1wYWdlPSJyYXRpbmciPtCi0LDQsdC70LjRhtCwINC70LjQtNC10YDQvtCyPC9idXR0b24+PC9kaXY+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0KLQktCe0JkgRUxPPC9sYWJlbD48c3Ryb25nIGlkPSJob21lRWxvIj4xMDAwPC9zdHJvbmc+PHNtYWxsPuKXjyDQodGC0LDRgNGC0L7QstGL0Lkg0YDQtdC50YLQuNC90LM8L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxsYWJlbD7Qn9Ce0JHQldCU0Ks8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXaW5zIj4wPC9zdHJvbmc+PHNtYWxsPtCc0LDRgtGH0Lgg0YHQtdC30L7QvdCwPC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+V0lOIFJBVEU8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXUiI+MCU8L3N0cm9uZz48c21hbGw+0J7QsdGJ0LjQuSDQv9C+0LrQsNC30LDRgtC10LvRjDwvc21hbGw+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JHQu9C40LbQsNC50YjQuNC5INC80LDRgtGHPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJtYXRjaGVzIj7QktGB0LUg0LzQsNGC0YfQuCDihpI8L2J1dHRvbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaHJvdyI+PGRpdiBjbGFzcz0ibWF0Y2hsb2dvIj7impQ8L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaGNvcHkiPjxiPtCg0LXQudGC0LjQvdCz0L7QstGL0Lkg0LzQsNGC0YcgwrcgNXY1PC9iPjxzbWFsbD5TYW5kc3RvbmUgwrcgQ29tcGV0aXRpdmUgwrcg0J7QttC40LTQsNC90LjQtSDQuNCz0YDQvtC60L7Qsjwvc21hbGw+PC9kaXY+PHNwYW4gY2xhc3M9InBpbGwiPkxJVkUgUVVFVUU8L3NwYW4+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QtdC00LDQstC90LjQtSDRgNC10LfRg9C70YzRgtCw0YLRizwvaDM+PHNwYW4gY2xhc3M9Im11dGVkIHNtYWxsIj7QodCV0JfQntCdIDAzPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7inKY8L2Rpdj48ZGl2PjxiPtCd0L7QstGL0Lkg0YHQtdC30L7QvSDQvdCw0YfQsNC70YHRjzwvYj48cD7Qn9C+0LTQvdC40LzQsNC5INGA0LXQudGC0LjQvdCzINC4INC+0YLQutGA0YvQstCw0Lkg0L3QsNCz0YDQsNC00YsgU1RBTkRQT0lOVCBQYXNzLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pqUPC9kaXY+PGRpdj48Yj7QoNC10LnRgtC40L3Qs9C+0LLQsNGPINC+0YfQtdGA0LXQtNGMINC+0YLQutGA0YvRgtCwPC9iPjxwPtCh0L7QsdC40YDQsNC5INC60L7QvNCw0L3QtNGDINC4INCy0YHRgtGD0L/QsNC5INCyINCx0L7QuS48L3A+PC9kaXY+PC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ic3RhY2siPgogICA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QodCV0JfQntCdIDAzPC9kaXY+PGgzPlNUQU5EUE9JTlQgUEFTUzwvaDM+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1sZXZlbCIgaWQ9InBhc3NMZXZlbCI+MDE8L2Rpdj48L2Rpdj48cD7QmNCz0YDQsNC5INC80LDRgtGH0LgsINC30LDRgNCw0LHQsNGC0YvQstCw0LkgWFAg0Lgg0L7RgtC60YDRi9Cy0LDQuSDQvdCw0LPRgNCw0LTRiyDRgdC10LfQvtC90LAuPC9wPjxkaXYgY2xhc3M9InByb2dyZXNzIj48c3BhbiBpZD0icGFzc1Byb2dyZXNzIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1mb290Ij48c3BhbiBpZD0icGFzc1hQIj4xMjAgLyAzMDAgWFA8L3NwYW4+PGJ1dHRvbiBjbGFzcz0idGV4dGxpbmsiIGRhdGEtcGFnZT0icGFzcyI+0J7RgtC60YDRi9GC0YwgUGFzcyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotC+0L8g0LjQs9GA0L7QutC+0LI8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBkYXRhLXBhZ2U9InJhdGluZyI+0JLRgdC1INGA0LXQudGC0LjQvdCz0Lgg4oaSPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImhvbWVMZWFkZXJzIj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotCy0L7QuSDQv9GA0L7Qs9GA0LXRgdGBPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5TRUFTT04gMDM8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hyb3ciPjxkaXYgY2xhc3M9Im1hdGNobG9nbyI+4pmcPC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hjb3B5Ij48Yj7Qn9C+0LTQvdC40LzQsNC50YHRjyDQsiDRgNC10LnRgtC40L3Qs9C1PC9iPjxzbWFsbD7QmtCw0LbQtNCw0Y8g0L/QvtCx0LXQtNCwINC/0YDQuNCx0LvQuNC20LDQtdGCINC6INGN0LvQuNGC0LUuPC9zbWFsbD48L2Rpdj48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJyYXRpbmciPkVMTyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KIDwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtbWF0Y2hlcyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0JzQsNGC0YfQvNC10LnQutC40L3QszwvaDI+PHA+0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0LrQsNGA0YLRgy4g0J3QsNC50LTQuCDRgdC+0L/QtdGA0L3QuNC60L7QsiDRgdCy0L7QtdCz0L4g0YPRgNC+0LLQvdGPLjwvcD48L2Rpdj48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5DT01QRVRJVElWRTwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9ImxheW91dCI+CiAgPGRpdiBjbGFzcz0ic3RhY2siPjxkaXYgY2xhc3M9InF1ZXVlYm94Ij48ZGl2IGNsYXNzPSJxdWV1ZWljb24iIGlkPSJxdWV1ZUljb24iPuKalDwvZGl2PjxkaXYgY2xhc3M9ImV5ZWJyb3ciPtCf0J7QmNCh0Jog0JzQkNCi0KfQkDwvZGl2PjxoMyBpZD0icXVldWVUaXRsZSI+0JPQvtGC0L7QsiDQuiDQuNCz0YDQtT88L2gzPjxwIGlkPSJxdWV1ZURlc2NyaXB0aW9uIj7QktGL0LHQtdGA0Lgg0YDQtdC20LjQvCDQuCDQvdCw0YfQvdC4INC/0L7QuNGB0Log0YDQtdC50YLQuNC90LPQvtCy0L7Qs9C+INC80LDRgtGH0LAuPC9wPjxkaXYgY2xhc3M9Im1vZGVncmlkIiBpZD0ibW9kZUdyaWQiPjxidXR0b24gY2xhc3M9Im1vZGUiIGRhdGEtbW9kZT0iMXYxIj4xdjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtb2RlIiBkYXRhLW1vZGU9IjJ2MiI+MnYyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibW9kZSIgZGF0YS1tb2RlPSIzdjMiPjN2MzwvYnV0dG9uPjxidXR0b24gY2xhc3M9Im1vZGUgYWN0aXZlIiBkYXRhLW1vZGU9IjV2NSI+NXY1PC9idXR0b24+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJxdWV1ZUJ1dHRvbiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQudGC0Lgg0LzQsNGC0Yc8L2J1dHRvbj48cCBjbGFzcz0iZm9vdG5vdGUiIGlkPSJxdWV1ZUFwaU5vdGUiPtCf0L7QuNGB0Log0Lgg0YHRgtCw0YLRg9GBINC+0YfQtdGA0LXQtNC4INGB0LjQvdGF0YDQvtC90LjQt9C40YDRg9GO0YLRgdGPINGBINGB0LXRgNCy0LXRgNC+0LwgU1RBTkRQT0lOVC48L3A+PC9kaXY+CiAgPGRpdiBjbGFzcz0ibWF0Y2gtY2VudGVyIiBpZD0ibWF0Y2hDZW50ZXIiIGFyaWEtbGl2ZT0icG9saXRlIj4KICAgPGRpdiBjbGFzcz0ibWMtaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5TVEFORFBPSU5UIENPTVBFVElUSVZFPC9kaXY+PGRpdiBjbGFzcz0ibWMtdGl0bGUiIGlkPSJtY1RpdGxlIj7QnNCw0YLRhyDQvdCw0LnQtNC10L08L2Rpdj48ZGl2IGNsYXNzPSJtYy1zdWIiIGlkPSJtY1N1YnRpdGxlIj7Qn9C+0LTRgtCy0LXRgNC00Lgg0YPRh9Cw0YHRgtC40LUsINGH0YLQvtCx0Ysg0L/QtdGA0LXQudGC0Lgg0LIg0LvQvtCx0LHQuC48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0ibWMtc3RhdHVzIiBpZD0ibWNTdGF0dXMiPk1BVENIIEZPVU5EPC9zcGFuPjwvZGl2PgogICA8ZGl2IGNsYXNzPSJtYy1ib2R5Ij4KICAgIDxkaXYgaWQ9Im1jQWNjZXB0UGFuZWwiPjxkaXYgY2xhc3M9Im1jLXByb2dyZXNzIj48c3BhbiBpZD0ibWNBY2NlcHRQcm9ncmVzcyI+PC9zcGFuPjwvZGl2PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQWNjZXB0VGV4dCI+0J7QttC40LTQsNC10Lwg0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8g0LjQs9GA0L7QutC+0LLigKY8L3A+PGRpdiBjbGFzcz0ibWMtcGxheWVyZ3JpZCIgaWQ9Im1jQWNjZXB0UGxheWVycyI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyIgc3R5bGU9Im1hcmdpbi10b3A6MTJweCI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJtY0FjY2VwdEJ0biI+4pyTINCf0YDQuNC90Y/RgtGMINC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9Im1jRGVjbGluZUJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJtY0JhblBhbmVsIiBjbGFzcz0ibWMtaGlkZGVuIj4KICAgICA8ZGl2IGNsYXNzPSJtYy10d28iPjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQTwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5BIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQSIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEE8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5BSW5mbyI+PC9wPjwvZGl2PjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQjwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5CIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEI8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5CSW5mbyI+PC9wPjwvZGl2PjwvZGl2PgogICAgIDxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQgaWQ9Im1jQmFuVGl0bGUiPkJhbiAvIFBpY2sg0LrQsNGA0YI8L2g0PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQmFuSGludCI+0JrQsNC/0LjRgtCw0L3RiyDQv9C+INC+0YfQtdGA0LXQtNC4INCx0LvQvtC60LjRgNGD0Y7RgiDQutCw0YDRgtGLLCDQt9Cw0YLQtdC8INCy0YvQsdC40YDQsNGO0YIg0LrQsNGA0YLRgyDQvNCw0YLRh9CwLjwvcD48ZGl2IGNsYXNzPSJtYy1tYXBzIiBpZD0ibWNNYXBCdXR0b25zIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1sb2ciIGlkPSJtY01hcExvZyIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9Im1jTG9iYnlQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCDQmtC+0LzQsNC90LTQsCBBPC9oND48dWwgaWQ9Im1jVGVhbUEiPjwvdWw+PC9kaXY+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCflLUg0JrQvtC80LDQvdC00LAgQjwvaDQ+PHVsIGlkPSJtY1RlYW1CIj48L3VsPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLW5vdGljZSIgaWQ9Im1jTG9iYnlJbmZvIj7QmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwLiDQntC20LjQtNCw0LXQvCDQt9Cw0L/Rg9GB0Log0LzQsNGC0YfQsCDQutCw0L/QuNGC0LDQvdC+0Lwg0LrQvtC80LDQvdC00YsgQS48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9Im1jU3RhcnRCdG4iIHN0eWxlPSJ3aWR0aDoxMDAlO21hcmdpbi10b3A6MTBweCI+4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNQbGF5aW5nUGFuZWwiIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLW5vdGljZSI+0JzQsNGC0Ycg0L/RgNC+0YXQvtC00LjRgiDQsiBTdGFuZG9mZiAyLiDQn9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINCy0L3QtdGB0Lgg0YHRh9GR0YIg0L7QsdC10LjRhSDQutC+0LzQsNC90LQg4oCUINGA0LXQt9GD0LvRjNGC0LDRgiDRg9C50LTRkdGCINC80L7QtNC10YDQsNGC0L7RgNGDINC90LAg0L/RgNC+0LLQtdGA0LrRgywgRUxPINC90LUg0LjQt9C80LXQvdC40YLRgdGPINC00L4g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPC9kaXY+PGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQTwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQjwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QotCS0J7QmCDQo9CR0JjQmdCh0KLQktCQIChLSUxMUyk8L2xhYmVsPjxpbnB1dCBpZD0ibWNLaWxscyIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwMCIgdmFsdWU9IjAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KLQktCe0Jgg0KHQnNCV0KDQotCYIChERUFUSFMpPC9sYWJlbD48aW5wdXQgaWQ9Im1jRGVhdGhzIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAwIiB2YWx1ZT0iMCI+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QmtCe0JzQnNCV0J3QotCQ0KDQmNCZINCaINCg0JXQl9Cj0JvQrNCi0JDQotCjPC9sYWJlbD48dGV4dGFyZWEgaWQ9Im1jUmVzdWx0Tm90ZSIgcm93cz0iMiIgcGxhY2Vob2xkZXI9ItCd0LDQv9GA0LjQvNC10YA6INGB0LrRgNC40L3RiNC+0YIg0YDQtdC30YPQu9GM0YLQsNGC0LAg0LHRg9C00LXRgiDQv9GA0LjQu9C+0LbQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDIj48L3RleHRhcmVhPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0ibWNTdWJtaXRSZXN1bHQiIHN0eWxlPSJ3aWR0aDoxMDAlIj7QntGC0L/RgNCw0LLQuNGC0Ywg0YDQtdC30YPQu9GM0YLQsNGCINC90LAg0L/RgNC+0LLQtdGA0LrRgzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNSZXZpZXdQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtbm90aWNlIiBpZD0ibWNSZXZpZXdJbmZvIj7QoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgyDQvNC+0LTQtdGA0LDRgtC+0YDRgy4g0KDQtdC50YLQuNC90LMg0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS48L2Rpdj48ZGl2IGlkPSJtY0FwcGVhbEZvcm0iIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0J/QoNCY0KfQmNCd0JAg0JDQn9CV0JvQm9Cv0KbQmNCYPC9sYWJlbD48c2VsZWN0IGlkPSJtY0FwcGVhbFJlYXNvbiI+PG9wdGlvbiB2YWx1ZT0id3Jvbmdfc2NvcmUiPtCd0LXQstC10YDQvdGL0Lkg0YHRh9GR0YI8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJjaGVhdGluZyI+0J/QvtC00L7Qt9GA0LXQvdC40LUg0L3QsCDQvdC10YfQtdGB0YLQvdGD0Y4g0LjQs9GA0YM8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJ0ZWNobmljYWxfaXNzdWUiPtCi0LXRhdC90LjRh9C10YHQutCw0Y8g0L/RgNC+0LHQu9C10LzQsDwvb3B0aW9uPjxvcHRpb24gdmFsdWU9Im90aGVyIj7QlNGA0YPQs9C+0LU8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCf0J7QlNCg0J7QkdCd0J7QodCi0Jg8L2xhYmVsPjx0ZXh0YXJlYSBpZD0ibWNBcHBlYWxEZXRhaWxzIiByb3dzPSIzIiBwbGFjZWhvbGRlcj0i0J7Qv9C40YjQuCDQv9GA0L7QsdC70LXQvNGDINC00LvRjyDQvNC+0LTQtdGA0LDRgtC+0YDQsCI+PC90ZXh0YXJlYT48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTdWJtaXRBcHBlYWwiIHN0eWxlPSJ3aWR0aDoxMDAlIj7Qn9C+0LTQsNGC0Ywg0LDQv9C10LvQu9GP0YbQuNGOPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J/RgNCw0LLQuNC70LAg0YDQtdC50YLQuNC90LPQsDwvaDM+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+RUxPPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7ihpc8L2Rpdj48ZGl2PjxiPtCf0L7QsdC10LbQtNCw0Lkg4oCUINC/0L7Qu9GD0YfQsNC5INGA0LXQudGC0LjQvdCzPC9iPjxwPtCg0LXQt9GD0LvRjNGC0LDRgiDQvNCw0YLRh9CwINCy0LvQuNGP0LXRgiDQvdCwINGC0LLQvtC5IEVMTy48L3A+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm90aWNlIj48ZGl2IGNsYXNzPSJub3RpY2VpY28iPuKcpjwvZGl2PjxkaXY+PGI+0JjQs9GA0LDQuSDRgNC10LPRg9C70Y/RgNC90L48L2I+PHA+0JfQsNGA0LDQsdCw0YLRi9Cy0LDQuSDQvtC/0YvRgiDQtNC70Y8g0YHQtdC30L7QvdC90L7Qs9C+IFBhc3MuPC9wPjwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JLRi9Cx0L7RgCDQutCw0YDRgtGLPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCf0KDQldCU0J/QntCn0KLQldCd0JjQlTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXBncmlkIiBpZD0ibWFwR3JpZCI+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSBzZWxlY3RlZCIgZGF0YS1tYXA9IlNhbmRzdG9uZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+c77iPPC9zcGFuPjwvZGl2PjxiPlNhbmRzdG9uZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUHJvdmluY2UiPjxkaXYgY2xhc3M9Im1hcGFydCI+PHNwYW4+8J+Pme+4jzwvc3Bhbj48L2Rpdj48Yj5Qcm92aW5jZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUnVzdCI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+tPC9zcGFuPjwvZGl2PjxiPlJ1c3Q8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IkJyZWV6ZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4yKPC9zcGFuPjwvZGl2PjxiPkJyZWV6ZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iWm9uZSA3Ij48ZGl2IGNsYXNzPSJtYXBhcnQiPjxzcGFuPuKYou+4jzwvc3Bhbj48L2Rpdj48Yj5ab25lIDc8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IlNha3VyYSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y4PC9zcGFuPjwvZGl2PjxiPlNha3VyYTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iRHVuZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7im7DvuI88L3NwYW4+PC9kaXY+PGI+RHVuZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iQ2FseXBzbyI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y0PC9zcGFuPjwvZGl2PjxiPkNhbHlwc288L2I+PC9idXR0b24+PC9kaXY+PC9kaXY+CiA8L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXJhdGluZyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0KDQtdC50YLQuNC90LMg0LjQs9GA0L7QutC+0LI8L2gyPjxwPtCU0L7QutCw0LbQuCDRgdCy0L7QuSDRg9GA0L7QstC10L3RjC4g0J/QvtC00L3QuNC80LDQudGB0Y8g0LIg0YLQsNCx0LvQuNGG0LUg0LvQuNC00LXRgNC+0LIuPC9wPjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNFQVNPTiAwMzwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYnMiIGlkPSJyYXRpbmdUYWJzIj48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdGFiPSJzZWFzb24iPtCh0LXQt9C+0L0gMDM8L2J1dHRvbj48YnV0dG9uIGRhdGEtdGFiPSJhbGwiPtCX0LAg0LLRgdGRINCy0YDQtdC80Y88L2J1dHRvbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYmxld3JhcCI+PHRhYmxlIGNsYXNzPSJ0YWJsZSI+PHRoZWFkPjx0cj48dGg+IzwvdGg+PHRoPtCY0JPQoNCe0Jo8L3RoPjx0aD5FTE88L3RoPjx0aD7Qn9Ce0JHQldCU0Ks8L3RoPjx0aD5XSU4gUkFURTwvdGg+PC90cj48L3RoZWFkPjx0Ym9keSBpZD0icmF0aW5nUm93cyI+PC90Ym9keT48L3RhYmxlPjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSI+0KLQsNCx0LvQuNGG0LAg0YHQvtC00LXRgNC20LjRgiDQtNC10LzQvtC90YHRgtGA0LDRhtC40L7QvdC90YvQtSDQtNCw0L3QvdGL0LUuINCf0L7QtNC60LvRjtGH0LggYmFja2VuZCwg0YfRgtC+0LHRiyDQvtGC0L7QsdGA0LDQttCw0YLRjCDRgNC10LDQu9GM0L3Ri9C5INGA0LXQudGC0LjQvdCzINC40LPRgNC+0LrQvtCyLjwvcD4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXBhc3MiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPlNUQU5EUE9JTlQgUGFzczwvaDI+PHA+0KHQtdC30L7QvSAwMyDCtyDQmNCz0YDQsNC5LCDQt9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQLCDQt9Cw0LHQuNGA0LDQuSDQvdCw0LPRgNCw0LTRiy48L3A+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+0JDQmtCi0JjQktCV0J08L3NwYW4+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQrdCb0JjQotCVPC9kaXY+PGgyIHN0eWxlPSJtYXJnaW46OHB4IDA7Zm9udC1zaXplOjI4cHgiPtCh0LXQt9C+0L3QvdGL0LUg0L3QsNCz0YDQsNC00Ys8L2gyPjxwPtCX0LDRgNCw0LHQsNGC0YvQstCw0Lkg0L7Qv9GL0YIg0LfQsCDQv9C+0LTRgtCy0LXRgNC20LTRkdC90L3Ri9C1INC80LDRgtGH0LguPC9wPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtbGV2ZWwiIHN0eWxlPSJmb250LXNpemU6NDZweCIgaWQ9InBhc3NMZXZlbEJpZyI+MDE8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJwcm9ncmVzcyI+PHNwYW4gaWQ9InBhc3NQcm9ncmVzc0JpZyI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtZm9vdCI+PHNwYW4gaWQ9InBhc3NYUEJpZyI+MTIwIC8gMzAwIFhQPC9zcGFuPjxzcGFuPtCj0YDQvtCy0LXQvdGMIDEg0LjQtyAzMDwvc3Bhbj48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QsNCz0YDQsNC00Ysg0YHQtdC30L7QvdCwPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPjMg0J/QoNCV0JTQnNCV0KLQkDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0cyI+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfjpbvuI88L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0J3QvtCy0L7QsdGA0LDQvdC10YY8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMSDCtyDQn9C+0LvRg9GH0LXQvdC+PC9zbWFsbD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGNsYWltIiBzdHlsZT0ibWFyZ2luLXRvcDoxMnB4O3dpZHRoOjEwMCU7Zm9udC1zaXplOjEwcHgiIGRhdGEtcmV3YXJkPSLQndC+0LLQvtCx0YDQsNC90LXRhiI+0J/QvtC70YPRh9C10L3QvjwvYnV0dG9uPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyN3B4Ij7wn5SlPC9kaXY+PHN0cm9uZyBzdHlsZT0iZm9udC1zaXplOjE1cHgiPtCd0LAg0L/QvtC00YrRkdC80LU8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgNTwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0J3QsCDQv9C+0LTRitGR0LzQtSI+0J/RgNC+0LLQtdGA0LjRgtGMPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfkZE8L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0K3Qu9C40YLQsDwvc3Ryb25nPjxzbWFsbD7Qo9GA0L7QstC10L3RjCAxMDwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0K3Qu9C40YLQsCI+0JfQsNCx0LvQvtC60LjRgNC+0LLQsNC90L48L2J1dHRvbj48L2Rpdj48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLWNsYW5zIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QmtC70LDQvdGLPC9oMj48cD7QodC+0LHQtdGA0Lgg0LrQvtC80LDQvdC00YMuINCX0LDRidC40YnQsNC5INGB0LLQvtC5INGC0LXQsy4g0J/QvtCx0LXQttC00LDQuSDQsiBDbGFuIFdhcnMuPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY3JlYXRlQ2xhbkJ0biI+77yLINCh0L7Qt9C00LDRgtGMINC60LvQsNC9PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjbGFuaGVybyI+PGRpdiBjbGFzcz0iY2xhbmxvZ28iPvCfkLo8L2Rpdj48ZGl2IGNsYXNzPSJncm93Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQmtCb0JDQnTwvZGl2PjxoMyBpZD0iY2xhbk5hbWUiPkZyb3N0IFdvbHZlcyA8c3BhbiBpZD0iY2xhblRhZyIgY2xhc3M9Im11dGVkIHNtYWxsIj4jRlc8L3NwYW4+PC9oMz48cD4xMiDRg9GH0LDRgdGC0L3QuNC60L7QsiDCtyAxOTgwIENsYW4gRUxPPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkiIGlkPSJjbGFuRGV0YWlscyI+0J/QvtC00YDQvtCx0L3QtdC1PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0KHQvtGB0YLQsNCyINC60LvQsNC90LA8L2gzPjxzcGFuIGNsYXNzPSJtdXRlZCBzbWFsbCI+MTIgLyAyMDwvc3Bhbj48L2Rpdj48ZGl2IGlkPSJjbGFuTWVtYmVycyIgY2xhc3M9ImxlYWRlcmxpc3QiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+Q2xhbiBXYXJzPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5MSVZFIExPQkJZPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWFyb3VuZDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE4cHggMCI+PGRpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MzVweCI+8J+boe+4jzwvZGl2PjxiIGlkPSJ3YXJPd25OYW1lIj7QotCS0J7QmSDQmtCb0JDQnTwvYj48L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MjFweDtjb2xvcjojZmY5ODRkIj5WUzwvc3Ryb25nPjxkaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjM1cHgiPuKalO+4jzwvZGl2PjxiPtCh0J7Qn9CV0KDQndCY0Jo8L2I+PC9kaXY+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIiBpZD0id2FyU3RhdHVzVGV4dCI+0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LrQu9Cw0L3RgyDQv9C+INC10LPQviBJRCDQuNC70Lgg0L7RgtC60YDQvtC5INC40YHRgtC+0YDQuNGOLjwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0id2FyQnV0dG9uIiBzdHlsZT0id2lkdGg6MTAwJSI+0J7RgtC60YDRi9GC0Ywg0LjRgdGC0L7RgNC40Y4gQ2xhbiBXYXJzPC9idXR0b24+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHdhci1sb2JieS1jYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4O2JvcmRlci1jb2xvcjojNTEzMjFmO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzMGRlZywjMWQxNzE0LCMxMTE2MWMpIj48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5DTEFOIFdBUiBPUEVSQVRJT05TPC9kaXY+PGgzIHN0eWxlPSJtYXJnaW4tdG9wOjVweCI+0JvQvtCx0LHQuCDQutC70LDQvdC+0LLQvtC5INCy0L7QudC90Ys8L2gzPjwvZGl2PjxzcGFuIGNsYXNzPSJtYy1zdGF0dXMiPjV2NSDCtyBCTzE8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntGC0L/RgNCw0LLRjCDQstGL0LfQvtCyINC00YDRg9Cz0L7QvNGDINC60LvQsNC90YMuINCf0L7RgdC70LUg0L/RgNC40L3Rj9GC0LjRjyDQv9C+0Y/QstC40YLRgdGPINC90LDRgdGC0L7Rj9GJ0LXQtSDQu9C+0LHQsdC4INGBINGB0L7RgdGC0LDQstCw0LzQuCDQuCDQvtGC0L/RgNCw0LLQutC+0Lkg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L3QsCDQvNC+0LTQtdGA0LDRhtC40Y4uPC9wPjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCa0JvQkNCdLdCh0J7Qn9CV0KDQndCY0Jo8L2xhYmVsPjxzZWxlY3QgaWQ9Indhck9wcG9uZW50SWQiPjxvcHRpb24gdmFsdWU9IiI+0JfQsNCz0YDRg9C20LDQtdC8INGB0L/QuNGB0L7QuiDQutC70LDQvdC+0LLigKY8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPklEINCi0JXQmtCj0KnQldCZINCS0J7QmdCd0Ks8L2xhYmVsPjxpbnB1dCBpZD0id2FySWRJbnB1dCIgcGxhY2Vob2xkZXI9ItCf0L7Rj9Cy0LjRgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LfQvtCy0LAiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY2hhbGxlbmdlV2FyQnRuIj7impQg0J7RgtC/0YDQsNCy0LjRgtGMINCy0YvQt9C+0LI8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibG9hZFdhckxvYmJ5QnRuIj7QntGC0LrRgNGL0YLRjCDQu9C+0LHQsdC4PC9idXR0b24+PC9kaXY+PGRpdiBpZD0id2FyTG9iYnlDb250ZW50IiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibWMtbXV0ZWQiPtCf0L7QutCwINC90LXRgiDQsNC60YLQuNCy0L3QvtCz0L4g0LvQvtCx0LHQuC4g0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LjQu9C4INCy0YHRgtCw0LLRjCBJRCDRgdGD0YnQtdGB0YLQstGD0Y7RidC10Lkg0LLQvtC50L3Riy48L3A+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIiBpZD0icGFnZS1mcmllbmRzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QlNGA0YPQt9GM0Y88L2gyPjxwPtCf0YDQuNCz0LvQsNGI0LDQuSDQuNCz0YDQvtC60L7QsiDQsiDQutC+0LzQsNC90LTRgyDQuCDQstC80LXRgdGC0LUg0L/QvtC00L3QuNC80LDQuSBFTE8uPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWRkRnJpZW5kQnRuIj7vvIsg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7Qn9Ce0JjQodCaINCf0J4g0J3QmNCa0J3QldCZ0JzQozwvbGFiZWw+PGlucHV0IGlkPSJmcmllbmRTZWFyY2giIHBsYWNlaG9sZGVyPSLQndCw0L/RgNC40LzQtdGALCBzaDF6byI+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImZyaWVuZHNMaXN0Ij48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLW5vdGlmaWNhdGlvbnMiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCj0LLQtdC00L7QvNC70LXQvdC40Y88L2gyPjxwPtCc0LDRgtGH0LgsINC/0YDQuNCz0LvQsNGI0LXQvdC40Y8g0Lgg0YHQvtCx0YvRgtC40Y8g0L/Qu9Cw0YLRhNC+0YDQvNGLLjwvcD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0icmVhZEFsbCI+0J/RgNC+0YfQuNGC0LDRgtGMINCy0YHQtTwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCIgaWQ9Im5vdGljZUxpc3QiPjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtcHJvZmlsZSI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwPC9oMj48cD7QotCy0L7RjyDRgdGC0LDRgtC40YHRgtC40LrQsCwg0YDQtdC50YLQuNC90LMg0Lgg0LjQs9GA0L7QstC+0Lkg0L/RgNC+0LPRgNC10YHRgS48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJwcm9maWxlaGVybyI+PGRpdiBjbGFzcz0icHJvZmlsZWF2YXRhciI+4pmfPC9kaXY+PGRpdj48aDMgaWQ9InByb2ZpbGVOYW1lIj5zdGFuZHBvaW50X3BsYXllcjwvaDM+PHAgaWQ9InByb2ZpbGVJZCI+SUQ6IGRlbW9fMDAxPC9wPjxzcGFuIGNsYXNzPSJwaWxsIiBpZD0icHJvZmlsZVN0YXR1cyI+4pePINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InByb2ZpbGVlbG8iPjxzbWFsbD7QotCV0JrQo9Cp0JjQmSBFTE88L3NtYWxsPjxzdHJvbmcgaWQ9InByb2ZpbGVFbG8iPjEwMDA8L3N0cm9uZz48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0JzQkNCi0KfQmDwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZU1hdGNoZXMiPjA8L3N0cm9uZz48c21hbGw+0JLRgdC10LPQviDRgdGL0LPRgNCw0L3Qvjwvc21hbGw+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGxhYmVsPtCf0J7QkdCV0JTQqzwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZVdpbnMiPjA8L3N0cm9uZz48c21hbGw+0J/QvtC00YLQstC10YDQttC00LXQvdC+PC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+Sy9EPC9sYWJlbD48c3Ryb25nIGlkPSJwcm9maWxlS0QiPuKAlDwvc3Ryb25nPjxzbWFsbD7Qo9Cx0LjQudGB0YLQstCwIC8g0YHQvNC10YDRgtC4PC9zbWFsbD48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JTQsNC90L3Ri9C1INC/0YDQvtGE0LjQu9GPPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCh0JXQoNCS0JXQoCAvINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCd0JjQmtCd0JXQmdCcPC9sYWJlbD48aW5wdXQgaWQ9ImVkaXROYW1lIiBtYXhsZW5ndGg9IjI0IiB2YWx1ZT0ic3RhbmRwb2ludF9wbGF5ZXIiPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JjQk9Cg0J7QktCe0JkgSUQ8L2xhYmVsPjxpbnB1dCBpZD0iZWRpdEdhbWVJZCIgbWF4bGVuZ3RoPSIzMiIgdmFsdWU9ImRlbW9fMDAxIj48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9InNhdmVQcm9maWxlIj7QodC+0YXRgNCw0L3QuNGC0Ywg0LjQt9C80LXQvdC10L3QuNGPPC9idXR0b24+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9GA0L7RhNC40LvRjCDRgdC+0YXRgNCw0L3Rj9C10YLRgdGPINC90LAg0YHQtdGA0LLQtdGA0LUsINC10YHQu9C4IEFQSSDQv9C+0LTQutC70Y7Rh9GR0L0g0LggVGVsZWdyYW0gSUQg0LTQvtGB0YLRg9C/0LXQvS48L3A+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCg0LDRgdGI0LjRgNC10L3QvdCw0Y8g0YHRgtCw0YLQuNGB0YLQuNC60LA8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBpZD0icmVmcmVzaEV4dHJhU3RhdHMiPtCe0LHQvdC+0LLQuNGC0Ywg4oa7PC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwtZ3JpZCI+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7Qn9Ce0JHQldCU0KsgJTwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFXaW5yYXRlIj7igJQ8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCf0J7QoNCQ0JbQldCd0JjQrzwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFMb3NzZXMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCd0JjQp9Cs0Jg8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhRHJhd3MiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCj0JHQmNCZ0KHQotCS0JA8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhS2lsbHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0JzQldCg0KLQmDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFEZWF0aHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0KDQldCU0J3QmNCZIEsvRDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFLRCI+4oCUPC9zdHJvbmc+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7QodCr0JPQoNCQ0J3QniDQnNCQ0KLQp9CV0Jk8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhTWF0Y2hlcyI+MDwvc3Ryb25nPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQtZGV0YWlsIj48c21hbGw+0KLQldCa0KPQqdCY0JkgRUxPPC9zbWFsbD48c3Ryb25nIGlkPSJleHRyYUVsbyI+MTAwMDwvc3Ryb25nPjwvZGl2PjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSIgaWQ9ImV4dHJhU3RhdHNOb3RlIj7QlNCw0L3QvdGL0LUg0LHRg9C00YPRgiDQt9Cw0LPRgNGD0LbQtdC90Ysg0LjQtyBBUEkg0L/QvtGB0LvQtSDQv9C+0LTQutC70Y7Rh9C10L3QuNGPINC/0YDQvtGE0LjQu9GPLjwvcD48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIGlkPSJtb2RlcmF0b3JDYXJkIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXItY29sb3I6IzcwNDAxZjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzIxMTcwZiwjMTExNjFjKSI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxkaXY+PGRpdiBjbGFzcz0iZXllYnJvdyI+TU9ERVJBVElPTiBDRU5URVI8L2Rpdj48aDMgc3R5bGU9Im1hcmdpbi10b3A6NXB4Ij7QkNC00LzQuNC9LdC/0LDQvdC10LvRjCDQvNC+0LTQtdGA0LDRgtC+0YDQsDwvaDM+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+QHl1a3plbl9icm88L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntC00L7QsdGA0LXQvdC40LUg0L3QsNGH0LjRgdC70Y/QtdGCIEVMTyDQuCDQt9Cw0L/QuNGB0YvQstCw0LXRgiDRgdGC0LDRgtC40YHRgtC40LrRgyDQsiDQv9GA0L7RhNC40LvQuCDRg9GH0LDRgdGC0L3QuNC60L7Qsi4g0J7RgtC60LDQtyDQvdC1INC80LXQvdGP0LXRgiBFTE8g0Lgg0YHRgtCw0YLQuNGB0YLQuNC60YMuPC9wPjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JrQm9Cu0Kcg0JzQntCU0JXQoNCQ0KbQmNCYIChSRVNVTFRTX0FETUlOX1RPS0VOKTwvbGFiZWw+PGlucHV0IGlkPSJtb2RlcmF0b3JUb2tlbiIgdHlwZT0icGFzc3dvcmQiIGF1dG9jb21wbGV0ZT0ib2ZmIiBwbGFjZWhvbGRlcj0i0JLRgdGC0LDQstGMINGB0LXQutGA0LXRgtC90YvQuSDQutC70Y7RhyDRgdC10YDQstC10YDQsCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJsb2FkTW9kZXJhdGlvbkJ0biI+0JfQsNCz0YDRg9C30LjRgtGMINC80LDRgtGH0Lgg0L3QsCDQv9GA0L7QstC10YDQutGDPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9ImNsZWFyTW9kZXJhdG9yVG9rZW4iPtCe0YfQuNGB0YLQuNGC0Ywg0LrQu9GO0Yc8L2J1dHRvbj48L2Rpdj48ZGl2IGlkPSJtb2RlcmF0aW9uUXVldWUiIHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z2FwOjEycHg7bWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCe0YfQtdGA0LXQtNGMINC80L7QtNC10YDQsNGG0LjQuCDQvdC1INC30LDQs9GA0YPQttC10L3QsC48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCa0YPQsdC60Lgg0Lgg0L3QsNCz0YDQsNC00Ys8L2gzPjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiIGlkPSJ0cm9waHlDb3VudCI+MCDQndCQ0JPQoNCQ0JQ8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QndCw0LPRgNCw0LTRiyDQt9CwINC60LLQsNC70LjRhNC40LrQsNGG0LjQuCDQuCDQuNGC0L7Qs9C+0LLRi9C1INC80LXRgdGC0LAg0LIg0YDQtdC50YLQuNC90LPQtS4g0JrRg9Cx0LrQuCDQvtGC0L7QsdGA0LDQttCw0Y7RgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LTQsNGH0Lgg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L7RgNCz0LDQvdC40LfQsNGC0L7RgNC+0LwuPC9wPjxkaXYgY2xhc3M9InRyb3BoeS1ncmlkIiBpZD0icHJvZmlsZVRyb3BoaWVzIj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J/QvtC60LAg0L3QtdGCINC60YPQsdC60L7Qsi4g0KPRh9Cw0YHRgtCy0YPQuSDQsiDQutCy0LDQu9C40YTQuNC60LDRhtC40Y/RhSwg0YfRgtC+0LHRiyDQv9C+0LvRg9GH0LjRgtGMINC90LDQs9GA0LDQtNGDLjwvZGl2PjwvZGl2PjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtY29zbWV0aWNzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QnNCw0LPQsNC30LjQvSDQutC+0YHQvNC10YLQuNC60Lg8L2gyPjxwPtCe0YTQvtGA0LzQu9C10L3QuNC1IFNUQU5EUE9JTlQgSUQ6INGA0LDQvNC60LgsINGC0LjRgtGD0LvRiyDQuCDQutCw0YDRgtC+0YfQutC4INC/0YDQvtGE0LjQu9GPLjwvcD48L2Rpdj48ZGl2IGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNQIFBPSU5UUzogPHNwYW4gaWQ9ImNvc21ldGljUG9pbnRzIj7igJQ8L3NwYW4+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxNHB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCa0LDRgtCw0LvQvtCzPC9oMz48ZGl2IGNsYXNzPSJ0YWJzIiBzdHlsZT0ibWFyZ2luOjAiPjxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS1jb3NtZXRpYy1maWx0ZXI9ImFsbCI+0JLRgdC1PC9idXR0b24+PGJ1dHRvbiBkYXRhLWNvc21ldGljLWZpbHRlcj0iZnJhbWUiPtCg0LDQvNC60Lg8L2J1dHRvbj48YnV0dG9uIGRhdGEtY29zbWV0aWMtZmlsdGVyPSJ0aXRsZSI+0KLQuNGC0YPQu9GLPC9idXR0b24+PGJ1dHRvbiBkYXRhLWNvc21ldGljLWZpbHRlcj0icHJvZmlsZV9jYXJkIj7QmtCw0YDRgtC+0YfQutC4PC9idXR0b24+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0iY29zbWV0aWMtZ3JpZCIgaWQ9ImNvc21ldGljU2hvcEdyaWQiPjxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7Ql9Cw0LPRgNGD0LbQsNC10Lwg0LrQsNGC0LDQu9C+0LMg0LrQvtGB0LzQtdGC0LjQutC44oCmPC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9C+0LrRg9C/0LrQsCDQtNC+0YHRgtGD0L/QvdCwINC/0L7RgdC70LUg0L/QvtC00LrQu9GO0YfQtdC90LjRjyDQsNCy0YLQvtGA0LjQt9C+0LLQsNC90L3QvtCz0L4g0L/RgNC+0YTQuNC70Y8uINCS0LDQu9GO0YLQsCDQuCDQv9GA0LXQtNC80LXRgtGLINCx0LXRgNGD0YLRgdGPINC40LcgYmFja2VuZCwg0LAg0L3QtSDQuNC3INC00LXQvNC+0L3RgdGC0YDQsNGG0LjQvtC90L3Ri9GFINC30L3QsNGH0LXQvdC40LkuPC9wPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QnNC+0Y8g0LrQvtC70LvQtdC60YbQuNGPPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgaWQ9InJlZnJlc2hDb3NtZXRpY3MiPtCe0LHQvdC+0LLQuNGC0Ywg4oa7PC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0iY29zbWV0aWMtZ3JpZCIgaWQ9Im15Q29zbWV0aWNzR3JpZCI+PGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCS0L7QudC00Lgg0YfQtdGA0LXQtyBUZWxlZ3JhbSwg0YfRgtC+0LHRiyDQt9Cw0LPRgNGD0LfQuNGC0Ywg0LrQvtC70LvQtdC60YbQuNGOLjwvZGl2PjwvZGl2PjwvZGl2Pgo8L3NlY3Rpb24+CjwvbWFpbj4KPC9kaXY+CjxuYXYgY2xhc3M9ImJvdHRvbW5hdiIgaWQ9ImJvdHRvbU5hdiI+PGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLXBhZ2U9ImhvbWUiPjxzcGFuPuKMgjwvc3Bhbj7Qk9C70LDQstC90LDRjzwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJtYXRjaGVzIj48c3Bhbj7impQ8L3NwYW4+0JzQsNGC0YfQuDwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJyYXRpbmciPjxzcGFuPuKZnDwvc3Bhbj7QoNC10LnRgtC40L3QszwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJjbGFucyI+PHNwYW4+4pmiPC9zcGFuPtCa0LvQsNC90Ys8L2J1dHRvbj48YnV0dG9uIGRhdGEtcGFnZT0icHJvZmlsZSI+PHNwYW4+4peJPC9zcGFuPtCf0YDQvtGE0LjQu9GMPC9idXR0b24+PC9uYXY+CjxkaXYgaWQ9InRvYXN0Um9vdCIgYXJpYS1saXZlPSJwb2xpdGUiPjwvZGl2Pgo8c2NyaXB0Pgpjb25zdCAkPShzLHI9ZG9jdW1lbnQpPT5yLnF1ZXJ5U2VsZWN0b3IocyksJCQ9KHMscj1kb2N1bWVudCk9PlsuLi5yLnF1ZXJ5U2VsZWN0b3JBbGwocyldOwpjb25zdCBkZWZhdWx0cz17bmFtZToic3RhbmRwb2ludF9wbGF5ZXIiLGdhbWVJZDoiZGVtb18wMDEiLGVsbzoxMDAwLHdpbnM6MCxsb3NzZXM6MCxkcmF3czowLGtpbGxzOjAsZGVhdGhzOjAseHA6MTIwLGZyaWVuZHM6W3tuYW1lOiJzaDF6byIsc3RhdHVzOiLQm9C10LPQtdC90LTQsCIsZWxvOjI0MTB9LHtuYW1lOiJ3MW5uZXIiLHN0YXR1czoi0JIg0LjQs9GA0LUiLGVsbzoyMzg3fSx7bmFtZToienhjcnVzZWQiLHN0YXR1czoi0J3QtSDQsiDRgdC10YLQuCIsZWxvOjIzMzJ9XSxub3RpY2VzOlt7aWNvbjoi4pymIix0aXRsZToi0JTQvtCx0YDQviDQv9C+0LbQsNC70L7QstCw0YLRjCDQsiBTVEFORFBPSU5UIixib2R5OiLQotCy0L7QuSDRgdC+0YDQtdCy0L3QvtCy0LDRgtC10LvRjNC90YvQuSDQv9GD0YLRjCDQvdCw0YfQuNC90LDQtdGC0YHRjyDQt9C00LXRgdGMLiIscmVhZDpmYWxzZX0se2ljb246IuKalCIsdGl0bGU6ItCg0LXQudGC0LjQvdCz0L7QstCw0Y8g0L7Rh9C10YDQtdC00Ywg0L7RgtC60YDRi9GC0LAiLGJvZHk6ItCS0YvQsdC10YDQuCDRgNC10LbQuNC8INC4INC90LDQudC00Lgg0LzQsNGC0YcuIixyZWFkOmZhbHNlfSx7aWNvbjoi8J+OliIsdGl0bGU6IlNUQU5EUE9JTlQgUGFzcyDQsNC60YLQuNCy0LXQvSIsYm9keToi0JfQsNGA0LDQsdCw0YLRi9Cy0LDQuSBYUCDQt9CwINC80LDRgtGH0Lgg0Lgg0L7RgtC60YDRi9Cy0LDQuSDQvdCw0LPRgNCw0LTRiy4iLHJlYWQ6dHJ1ZX1dfTsKbGV0IHN0YXRlO3RyeXtzdGF0ZT17Li4uZGVmYXVsdHMsLi4uSlNPTi5wYXJzZShsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgic3RhbmRwb2ludERlbW9WMiIpfHwie30iKX07fWNhdGNoe3N0YXRlPXsuLi5kZWZhdWx0c307fQpsZXQgc2VsZWN0ZWRNb2RlPSI1djUiLHNlbGVjdGVkTWFwPSJTYW5kc3RvbmUiLHF1ZXVlVGltZXI9bnVsbCxxdWV1ZVNlY29uZHM9MCxxdWV1ZUFjdGl2ZT1mYWxzZTsKY29uc3QgbGVhZGVycz1be25hbWU6InNoMXpvIixlbG86MjQxMCx3aW5zOjE1Mix3cjo3NH0se25hbWU6Incxbm5lciIsZWxvOjIzODcsd2luczoxMzksd3I6NzF9LHtuYW1lOiJ6eGNydXNlZCIsZWxvOjIzMzIsd2luczoxMjgsd3I6Njl9LHtuYW1lOiJkb25rNjY2IixlbG86MjI5MSx3aW5zOjExNyx3cjo2OH0se25hbWU6InlvdTFuZyIsZWxvOjIyNTYsd2luczoxMTEsd3I6NjZ9XTsKZnVuY3Rpb24gZXNjKHMpe3JldHVybiBTdHJpbmcocykucmVwbGFjZSgvWyY8PiInXS9nLGM9Pih7IiYiOiImYW1wOyIsIjwiOiImbHQ7IiwiPiI6IiZndDsiLCciJzoiJnF1b3Q7IiwiJyI6IiYjMzk7In1bY10pKTt9CmZ1bmN0aW9uIHNhdmUoKXtsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgic3RhbmRwb2ludERlbW9WMiIsSlNPTi5zdHJpbmdpZnkoc3RhdGUpKTt9CmZ1bmN0aW9uIHRvYXN0KHMpe2NvbnN0IHJvb3Q9JCgiI3RvYXN0Um9vdCIpO3Jvb3QuaW5uZXJIVE1MPSIiO2NvbnN0IGQ9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiZGl2Iik7ZC5jbGFzc05hbWU9InRvYXN0IjtkLnRleHRDb250ZW50PXM7cm9vdC5hcHBlbmQoZCk7c2V0VGltZW91dCgoKT0+ZC5yZW1vdmUoKSwzMjAwKTt9CmZ1bmN0aW9uIG5hdihwYWdlKXskJCgiLnNlY3Rpb24iKS5mb3JFYWNoKHM9PnMuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIixzLmlkPT09InBhZ2UtIitwYWdlKSk7JCQoIltkYXRhLXBhZ2VdIikuZm9yRWFjaChiPT5iLmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIsYi5kYXRhc2V0LnBhZ2U9PT1wYWdlKSk7Y29uc3QgdD17aG9tZToi0KLQstC+0Y8g0LjQs9GA0LAuINCi0LLQvtGPINC/0L7Qt9C40YbQuNGPLiIsbWF0Y2hlczoi0JzQsNGC0YfQvNC10LnQutC40L3QsyIscmF0aW5nOiLQoNC10LnRgtC40L3QsyBFTE8iLGNsYW5zOiLQmtC70LDQvdGLINC4IENsYW4gV2FycyIscGFzczoiU1RBTkRQT0lOVCBQYXNzIixmcmllbmRzOiLQlNGA0YPQt9GM0Y8iLG5vdGlmaWNhdGlvbnM6ItCj0LLQtdC00L7QvNC70LXQvdC40Y8iLHByb2ZpbGU6ItCf0YDQvtGE0LjQu9GMINC40LPRgNC+0LrQsCIsY29zbWV0aWNzOiLQnNCw0LPQsNC30LjQvSDQutC+0YHQvNC10YLQuNC60LgifTskKCIjcGFnZVRpdGxlIikudGV4dENvbnRlbnQ9dFtwYWdlXXx8IlNUQU5EUE9JTlQiO3dpbmRvdy5zY3JvbGxUbyh7dG9wOjAsYmVoYXZpb3I6InNtb290aCJ9KTt9CiQkKCJbZGF0YS1wYWdlXSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9Pm5hdihiLmRhdGFzZXQucGFnZSkpKTsKZnVuY3Rpb24gcmVuZGVyTGVhZGVycygpe2NvbnN0IGFycj1bLi4ubGVhZGVycyx7bmFtZTpzdGF0ZS5uYW1lLGVsbzpzdGF0ZS5lbG8sd2luczpzdGF0ZS53aW5zLHdyOnN0YXRlLndpbnMrc3RhdGUubG9zc2VzP01hdGgucm91bmQoc3RhdGUud2lucy8oc3RhdGUud2lucytzdGF0ZS5sb3NzZXMpKjEwMCk6MH1dLnNvcnQoKGEsYik9PmIuZWxvLWEuZWxvKTskKCIjaG9tZUxlYWRlcnMiKS5pbm5lckhUTUw9YXJyLnNsaWNlKDAsNCkubWFwKChwLGkpPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48c3BhbiBjbGFzcz0icmFuayI+JHtpKzF9PC9zcGFuPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MocC5uYW1lWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MocC5uYW1lKX08L2I+PHNtYWxsPiR7aT09PTA/ItCb0LXQs9C10L3QtNCwIjppPT09MT8i0JzQsNGB0YLQtdGAIjoi0JjQs9GA0L7QuiJ9PC9zbWFsbD48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0iZWxvIj4ke3AuZWxvfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKTskKCIjcmF0aW5nUm93cyIpLmlubmVySFRNTD1hcnIubWFwKChwLGkpPT5gPHRyIGNsYXNzPSIke3AubmFtZT09PXN0YXRlLm5hbWU/InlvdSI6IiJ9Ij48dGQ+JHtpKzF9PC90ZD48dGQ+PGI+JHtlc2MocC5uYW1lKX08L2I+PC90ZD48dGQ+PGI+JHtwLmVsb308L2I+PC90ZD48dGQ+JHtwLndpbnN9PC90ZD48dGQ+JHtwLndyfSU8L3RkPjwvdHI+YCkuam9pbigiIik7fQpmdW5jdGlvbiByZW5kZXJGcmllbmRzKCl7Y29uc3QgcT0oJCgiI2ZyaWVuZFNlYXJjaCIpPy52YWx1ZXx8IiIpLnRvTG93ZXJDYXNlKCk7JCgiI2ZyaWVuZHNMaXN0IikuaW5uZXJIVE1MPXN0YXRlLmZyaWVuZHMuZmlsdGVyKGY9PmYubmFtZS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHEpKS5tYXAoZj0+YDxkaXYgY2xhc3M9ImxlYWRlciI+PGRpdiBjbGFzcz0icGxheWVyIj48ZGl2IGNsYXNzPSJtaW5pYXZhIj4ke2VzYyhmLm5hbWVbMF0udG9VcHBlckNhc2UoKSl9PC9kaXY+PGRpdj48Yj4ke2VzYyhmLm5hbWUpfTwvYj48c21hbGw+JHtlc2MoZi5zdGF0dXMpfSDCtyBFTE8gJHtmLmVsb308L3NtYWxsPjwvZGl2PjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgaW52aXRlIiBkYXRhLW5hbWU9IiR7ZXNjKGYubmFtZSl9Ij7Qn9GA0LjQs9C70LDRgdC40YLRjDwvYnV0dG9uPjwvZGl2PmApLmpvaW4oIiIpfHwnPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QmNCz0YDQvtC60Lgg0L3QtSDQvdCw0LnQtNC10L3Riy48L3A+JzskJCgiLmludml0ZSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnRvYXN0KCLQlNC10LzQvjog0L/RgNC40LPQu9Cw0YjQtdC90LjQtSDQtNC70Y8gIitiLmRhdGFzZXQubmFtZSsiINC90LUg0L7RgtC/0YDQsNCy0LvQtdC90L4g0L3QsCDRgdC10YDQstC10YAuIikpKTt9CmZ1bmN0aW9uIHJlbmRlck5vdGljZXMoKXskKCIjbm90aWNlTGlzdCIpLmlubmVySFRNTD1zdGF0ZS5ub3RpY2VzLm1hcCgobixpKT0+YDxkaXYgY2xhc3M9Im5vdGljZSIgc3R5bGU9IiR7bi5yZWFkPyJvcGFjaXR5Oi44MiI6IiJ9Ij48ZGl2IGNsYXNzPSJub3RpY2VpY28iPiR7ZXNjKG4uaWNvbil9PC9kaXY+PGRpdiBzdHlsZT0iZmxleDoxIj48Yj4ke2VzYyhuLnRpdGxlKX08L2I+PHA+JHtlc2Mobi5ib2R5KX08L3A+PHNtYWxsIGNsYXNzPSJtdXRlZCI+JHtuLnJlYWQ/ItCf0YDQvtGH0LjRgtCw0L3QviI6ItCd0L7QstC+0LUifTwvc21hbGw+PC9kaXY+JHtuLnJlYWQ/IiI6YDxidXR0b24gY2xhc3M9InRleHRsaW5rIHJlYWROb3RpY2UiIGRhdGEtaW5kZXg9IiR7aX0iPtCf0YDQvtGH0LjRgtCw0L3QvjwvYnV0dG9uPmB9PC9kaXY+YCkuam9pbigiIik7JCQoIi5yZWFkTm90aWNlIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57Y29uc3Qgbj1zdGF0ZS5ub3RpY2VzWytiLmRhdGFzZXQuaW5kZXhdO3RyeXtpZihuLmlkJiZhcGlVc2VySWQpYXdhaXQgYXBpKGAvbm90aWZpY2F0aW9ucy8ke2VuY29kZVVSSUNvbXBvbmVudChuLmlkKX0vcmVhZGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkfX0pO24ucmVhZD10cnVlO3JlbmRlck5vdGljZXMoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L7RgtC80LXRgtC40YLRjDogIitlLm1lc3NhZ2UpO319KSk7fQpmdW5jdGlvbiByZW5kZXIoKXtjb25zdCB0b3RhbD1zdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cyx3cj10b3RhbD9NYXRoLnJvdW5kKHN0YXRlLndpbnMvdG90YWwqMTAwKTowLGxldmVsPU1hdGguZmxvb3Ioc3RhdGUueHAvMzAwKSsxLHBjdD0oc3RhdGUueHAlMzAwKS8zOyQoIiNzaWRlTmFtZSIpLnRleHRDb250ZW50PXN0YXRlLm5hbWU7JCgiI2hvbWVFbG8iKS50ZXh0Q29udGVudD1zdGF0ZS5lbG87JCgiI2hvbWVXaW5zIikudGV4dENvbnRlbnQ9c3RhdGUud2luczskKCIjaG9tZVdSIikudGV4dENvbnRlbnQ9d3IrIiUiOyQoIiNwcm9maWxlTmFtZSIpLnRleHRDb250ZW50PXN0YXRlLm5hbWU7JCgiI3Byb2ZpbGVJZCIpLnRleHRDb250ZW50PSJJRDogIitzdGF0ZS5nYW1lSWQrIiI7JCgiI3Byb2ZpbGVFbG8iKS50ZXh0Q29udGVudD1zdGF0ZS5lbG87JCgiI3Byb2ZpbGVNYXRjaGVzIikudGV4dENvbnRlbnQ9dG90YWw7JCgiI3Byb2ZpbGVXaW5zIikudGV4dENvbnRlbnQ9c3RhdGUud2luczskKCIjcHJvZmlsZUtEIikudGV4dENvbnRlbnQ9c3RhdGUuZGVhdGhzPyhzdGF0ZS5raWxscy9zdGF0ZS5kZWF0aHMpLnRvRml4ZWQoMik6c3RhdGUua2lsbHM/IuKIniI6IuKAlCI7JCgiI2VkaXROYW1lIikudmFsdWU9c3RhdGUubmFtZTskKCIjZWRpdEdhbWVJZCIpLnZhbHVlPXN0YXRlLmdhbWVJZDskKCIjcGFzc0xldmVsIikudGV4dENvbnRlbnQ9U3RyaW5nKGxldmVsKS5wYWRTdGFydCgyLCIwIik7JCgiI3Bhc3NMZXZlbEJpZyIpLnRleHRDb250ZW50PVN0cmluZyhsZXZlbCkucGFkU3RhcnQoMiwiMCIpOyQoIiNwYXNzWFAiKS50ZXh0Q29udGVudD0oc3RhdGUueHAlMzAwKSsiIC8gMzAwIFhQIjskKCIjcGFzc1hQQmlnIikudGV4dENvbnRlbnQ9KHN0YXRlLnhwJTMwMCkrIiAvIDMwMCBYUCI7JCgiI3Bhc3NQcm9ncmVzcyIpLnN0eWxlLndpZHRoPXBjdCsiJSI7JCgiI3Bhc3NQcm9ncmVzc0JpZyIpLnN0eWxlLndpZHRoPXBjdCsiJSI7cmVuZGVyTGVhZGVycygpO3JlbmRlckZyaWVuZHMoKTtyZW5kZXJOb3RpY2VzKCk7c2F2ZSgpO30KJCQoIltkYXRhLW1vZGVdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2lmKHF1ZXVlQWN0aXZlKXt0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L7RgtC80LXQvdC4INGC0LXQutGD0YnQuNC5INC/0L7QuNGB0LosINGH0YLQvtCx0Ysg0YHQvNC10L3QuNGC0Ywg0YDQtdC20LjQvC4iKTtyZXR1cm47fXNlbGVjdGVkTW9kZT1iLmRhdGFzZXQubW9kZTskJCgiW2RhdGEtbW9kZV0iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIix4PT09YikpO30pKTsKJCQoIltkYXRhLW1hcF0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57CiBzZWxlY3RlZE1hcD1iLmRhdGFzZXQubWFwOyQkKCJbZGF0YS1tYXBdIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoInNlbGVjdGVkIix4PT09YikpOwogdG9hc3QoItCa0LDRgNGC0LAg0LLRi9Cx0YDQsNC90LA6ICIrc2VsZWN0ZWRNYXArKGFjdGl2ZU1hdGNoSWQ/IiDCtyDQvtGC0LrRgNC+0Lkg0LzQsNGC0Ycg0LTQu9GPIGJhbi1waWNrIjoiIikpOwp9KSk7CiQoIiNxdWV1ZUJ1dHRvbiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFwaVVzZXJJZCl7dG9hc3QoItCe0YLQutGA0L7QuSBTVEFORFBPSU5UINGH0LXRgNC10LcgVGVsZWdyYW0g0Lgg0YHQvtGF0YDQsNC90Lgg0L/RgNC+0YTQuNC70YwuIik7bmF2KCJwcm9maWxlIik7cmV0dXJuO30KIHRyeXsKICBpZighcXVldWVBY3RpdmUpewogICBjb25zdCBkPWF3YWl0IGFwaSgiL3F1ZXVlL2pvaW4iLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxtb2RlOnNlbGVjdGVkTW9kZX19KTsKICAgaWYoZC5tYXRjaF9pZCl7YWN0aXZlTWF0Y2hJZD1kLm1hdGNoX2lkO3F1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTthd2FpdCBzaG93TWF0Y2goYWN0aXZlTWF0Y2hJZCk7cmV0dXJuO30KICAgcXVldWVBY3RpdmU9ZC5zdGF0dXM9PT0icXVldWVkIjskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD1xdWV1ZUFjdGl2ZT8i0J7RgtC80LXQvdC40YLRjCDQv9C+0LjRgdC6Ijoi0J3QsNC50YLQuCDQvNCw0YLRhyI7CiAgICQoIiNxdWV1ZVRpdGxlIikudGV4dENvbnRlbnQ9ItCY0YnQtdC8INGB0L7Qv9C10YDQvdC40LrQvtCy4oCmIjsKICAgJCgiI3F1ZXVlRGVzY3JpcHRpb24iKS50ZXh0Q29udGVudD1g0JIg0L7Rh9C10YDQtdC00LggJHtkLnBsYXllcnNfZm91bmR9LyR7ZC5wbGF5ZXJzX25lZWRlZH07INC+0YHRgtCw0LvQvtGB0YwgJHtkLnBsYXllcnNfcmVtYWluaW5nfWA7CiAgIHRvYXN0KCLQn9C+0LjRgdC6INC30LDQv9GD0YnQtdC9LiDQodGH0ZHRgtGH0LjQuiDRgdC40L3RhdGA0L7QvdC40LfQuNGA0YPQtdGC0YHRjyDRgSDRgdC10YDQstC10YDQvtC8LiIpO3N0YXJ0UXVldWVQb2xsaW5nKCk7CiAgfWVsc2V7CiAgIGF3YWl0IGFwaSgiL3F1ZXVlL2xlYXZlIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWR9fSk7CiAgIHF1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD0i0J3QsNC50YLQuCDQvNCw0YLRhyI7JCgiI3F1ZXVlVGl0bGUiKS50ZXh0Q29udGVudD0i0J/QvtC40YHQuiDQvtGC0LzQtdC90ZHQvSI7JCgiI3F1ZXVlRGVzY3JpcHRpb24iKS50ZXh0Q29udGVudD0i0KLRiyDQstGL0YjQtdC7INC40Lcg0L7Rh9C10YDQtdC00LguIjt0b2FzdCgi0J/QvtC40YHQuiDQvtGC0LzQtdC90ZHQvS4iKTsKICB9CiB9Y2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDQvtGH0LXRgNC10LTQuDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNyYXRpbmdUYWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLXRhYl0iKTtpZighYilyZXR1cm47JCQoIltkYXRhLXRhYl0iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIix4PT09YikpO3RvYXN0KGIuZGF0YXNldC50YWI9PT0ic2Vhc29uIj8i0KDQtdC50YLQuNC90LMg0YHQtdC30L7QvdCwICjQtNC10LzQvikuIjoi0J7QsdGJ0LjQuSDRgNC10LnRgtC40L3QsyAo0LTQtdC80L4pLiIpO30pOwokJCgiLmNsYWltIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2lmKGIuZGF0YXNldC5yZXdhcmQ9PT0i0J3QvtCy0L7QsdGA0LDQvdC10YYiKXt0b2FzdCgi0K3RgtCwINC90LDQs9GA0LDQtNCwINGD0LbQtSDQv9C+0LvRg9GH0LXQvdCwLiIpO3JldHVybjt9Y29uc3QgbmVlZD1iLmRhdGFzZXQucmV3YXJkPT09ItCd0LAg0L/QvtC00YrRkdC80LUiPzU6MTAsbGV2ZWw9TWF0aC5mbG9vcihzdGF0ZS54cC8zMDApKzE7dG9hc3QobGV2ZWw8bmVlZD8i0J3Rg9C20LXQvSDRg9GA0L7QstC10L3RjCAiK25lZWQrIi4g0KLQstC+0Lkg0YPRgNC+0LLQtdC90Yw6ICIrbGV2ZWw6ItCd0LDQs9GA0LDQtNGLINCx0YPQtNGD0YIg0LTQvtGB0YLRg9C/0L3RiyDQv9C+0YHQu9C1INC/0L7QtNC60LvRjtGH0LXQvdC40Y8gUGFzcyBBUEkuIik7fSkpOwokKCIjc2F2ZVByb2ZpbGUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGNvbnN0IG5pY2tuYW1lPSQoIiNlZGl0TmFtZSIpLnZhbHVlLnRyaW0oKSxnYW1lX2lkPSQoIiNlZGl0R2FtZUlkIikudmFsdWUudHJpbSgpOwogaWYoIW5pY2tuYW1lfHwhZ2FtZV9pZClyZXR1cm4gdG9hc3QoItCX0LDQv9C+0LvQvdC4INC90LjQutC90LXQudC8INC4INC40LPRgNC+0LLQvtC5IElELiIpOwogbGV0IGlkPWFwaVVzZXJJZHx8cHJvbXB0KCLQktCy0LXQtNC4INGB0LLQvtC5IFRlbGVncmFtIElEINC00LvRjyDQv9GA0LjQstGP0LfQutC4INCw0LrQutCw0YPQvdGC0LA6Iik7CiBpZighaWQpcmV0dXJuO2FwaVVzZXJJZD1TdHJpbmcoaWQpLnRyaW0oKTtsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgic3RhbmRwb2ludFRlbGVncmFtSWQiLGFwaVVzZXJJZCk7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoIi9wbGF5ZXJzL3JlZ2lzdGVyIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbmlja25hbWUsZ2FtZV9pZH19KTthcHBseVBsYXllcihkLnBsYXllcik7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt0b2FzdCgi0J/RgNC+0YTQuNC70Ywg0YHQvtGF0YDQsNC90ZHQvSDQvdCwINGB0LXRgNCy0LXRgNC1LiIpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0L/RgNC+0YTQuNC70Y86ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjZnJpZW5kU2VhcmNoIikuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLHJlbmRlckZyaWVuZHMpOwokKCIjYWRkRnJpZW5kQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBjb25zdCB0YXJnZXRfaWQ9cHJvbXB0KCJUZWxlZ3JhbSBJRCDQuNCz0YDQvtC60LAsINC60L7RgtC+0YDQvtCz0L4g0YXQvtGH0LXRiNGMINC00L7QsdCw0LLQuNGC0Yw6Iik7CiBpZighdGFyZ2V0X2lkfHwhYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0J3Rg9C20LXQvSDRgtCy0L7QuSBUZWxlZ3JhbSBJRCDQuCBJRCDQtNGA0YPQs9CwLiIpOwogdHJ5e2F3YWl0IGFwaSgiL2ZyaWVuZHMvcmVxdWVzdCIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHRhcmdldF9pZDp0YXJnZXRfaWQudHJpbSgpfX0pO3RvYXN0KCLQl9Cw0Y/QstC60LAg0L7RgtC/0YDQsNCy0LvQtdC90LAuIik7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt9CiBjYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINC30LDRj9Cy0LrQuDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNyZWFkQWxsIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjC4iKTsKIHRyeXthd2FpdCBhcGkoYC9ub3RpZmljYXRpb25zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3JlYWQtYWxsYCx7bWV0aG9kOiJQT1NUIn0pO2F3YWl0IHJlZnJlc2hBcGlEYXRhKCk7dG9hc3QoItCj0LLQtdC00L7QvNC70LXQvdC40Y8g0L7RgtC80LXRh9C10L3RiyDQv9GA0L7Rh9C40YLQsNC90L3Ri9C80LguIik7fQogY2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNjcmVhdGVDbGFuQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjCDQsiBUZWxlZ3JhbS4iKTsKIGNvbnN0IG5hbWU9cHJvbXB0KCLQndCw0LfQstCw0L3QuNC1INC60LvQsNC90LAgKDPigJM0MCDRgdC40LzQstC+0LvQvtCyKToiKTtpZighbmFtZT8udHJpbSgpKXJldHVybjsKIGNvbnN0IHRhZz1wcm9tcHQoItCi0LXQsyDQutC70LDQvdCwICgy4oCTOCDQu9Cw0YLQuNC90YHQutC40YUg0LHRg9C60LIv0YbQuNGE0YApOiIpO2lmKCF0YWc/LnRyaW0oKSlyZXR1cm47CiBjb25zdCBkZXNjcmlwdGlvbj1wcm9tcHQoItCe0L/QuNGB0LDQvdC40LUg0LrQu9Cw0L3QsCAo0L3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4pOiIpfHwiIjsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaSgiL2NsYW5zIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbmFtZTpuYW1lLnRyaW0oKSx0YWc6dGFnLnRyaW0oKSxkZXNjcmlwdGlvbn19KTthY3RpdmVDbGFuSWQ9ZC5jbGFuX2lkO3RvYXN0KCLQmtC70LDQvSDRgdC+0LfQtNCw0L0g0L3QsCDRgdC10YDQstC10YDQtS4iKTthd2FpdCByZWZyZXNoQXBpRGF0YSgpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0YHQvtC30LTQsNC90LjRjyDQutC70LDQvdCwOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2NsYW5EZXRhaWxzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYWN0aXZlQ2xhbklkKXJldHVybiB0b2FzdCgi0J3QtdGCINCy0YvQsdGA0LDQvdC90L7Qs9C+INC60LvQsNC90LAuINCh0L7Qt9C00LDQuSDQutC70LDQvSDQuNC70Lgg0L7QsdC90L7QstC4INC00LDQvdC90YvQtS4iKTsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaShgL2NsYW5zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZUNsYW5JZCl9YCk7YWxlcnQoYCR7ZC5jbGFuLm5hbWV9IFske2QuY2xhbi50YWd9XSDCtyBFTE8gJHtkLmNsYW4uZWxvfVxuXG5gK2QubWVtYmVycy5tYXAobT0+YCR7bS5uaWNrbmFtZX0g4oCUICR7bS5yb2xlfSDigJQgJHttLmVsb30gRUxPYCkuam9pbigiXG4iKSk7fQogY2F0Y2goZSl7dG9hc3QoZS5tZXNzYWdlKTt9Cn0pOwokKCIjd2FyQnV0dG9uIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYWN0aXZlQ2xhbklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0LLRi9Cx0LXRgNC4INC40LvQuCDRgdC+0LfQtNCw0Lkg0LrQu9Cw0L0uIik7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlQ2xhbklkKX1gKTthbGVydCgoZC53YXJzfHxbXSkubWFwKHc9PmAke3cud2FyX2lkfSDCtyAke3cuc3RhdHVzfSDCtyAke3cuc2NvcmVfYT8/Ii0ifToke3cuc2NvcmVfYj8/Ii0ifWApLmpvaW4oIlxuIil8fCLQmNGB0YLQvtGA0LjRjyBDbGFuIFdhcnMg0L/Rg9GB0YLQsC4iKTt9CiBjYXRjaChlKXt0b2FzdCgiQ2xhbiBXYXJzOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2NsYW5NZW1iZXJzIikuaW5uZXJIVE1MPSc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCX0LDQs9GA0YPQt9C60LAg0YHQvtGB0YLQsNCy0LAg0LrQu9Cw0L3QsCDRgSDRgdC10YDQstC10YDQsOKApjwvcD4nOwoKLy8gPT09PT0gU1RBTkRQT0lOVCBBUEkgPT09PT0KY29uc3QgQVBJX0JBU0U9Imh0dHBzOi8vc3RhbmRwb2ludC1iYWNrZW5kLm9ucmVuZGVyLmNvbSI7CmxldCBhcGlVc2VySWQ9IiIsYWN0aXZlQ2xhbklkPSIiLGFjdGl2ZU1hdGNoSWQ9IiIscXVldWVQb2xsVGltZXI9bnVsbDsKZnVuY3Rpb24gZ2V0VGVsZWdyYW1Vc2VyKCl7dHJ5e3JldHVybiB3aW5kb3cuVGVsZWdyYW0/LldlYkFwcD8uaW5pdERhdGFVbnNhZmU/LnVzZXJ8fG51bGx9Y2F0Y2h7cmV0dXJuIG51bGx9fQpmdW5jdGlvbiBzZXRBcGlTdGF0dXModGV4dCxvaz1mYWxzZSl7Y29uc3QgZT0kKCIjYXBpU3RhdHVzIik7aWYoZSl7ZS50ZXh0Q29udGVudD10ZXh0O2Uuc3R5bGUuY29sb3I9b2s/IiM3NWYwYjYiOiIjZmZiNGE4Ijt9fQpmdW5jdGlvbiByZWFkYWJsZUFwaUVycm9yKGQsc3RhdHVzKXsKIGNvbnN0IGRldGFpbD1kPy5kZXRhaWx8fGQ/Lm1lc3NhZ2U7CiAvLyDQndC1INCy0YvQstC+0LTQuNC8INCyINC40L3RgtC10YDRhNC10LnRgSDQv9C+0YLQtdC90YbQuNCw0LvRjNC90L4g0YHQu9C+0LzQsNC90L3Rg9GOINC60L7QtNC40YDQvtCy0LrRgyDQvtGC0LLQtdGC0LAg0YHQtdGA0LLQtdGA0LAuCiBpZihzdGF0dXM9PT00MDQpcmV0dXJuICLQv9GA0L7RhNC40LvRjCDQuNC70Lgg0YDQtdGB0YPRgNGBINC90LUg0L3QsNC50LTQtdC9ICg0MDQpIjsKIGlmKHN0YXR1cz09PTQyMilyZXR1cm4gItGB0LXRgNCy0LXRgCDQvtGC0LrQu9C+0L3QuNC7INC00LDQvdC90YvQtSDQt9Cw0L/RgNC+0YHQsCAoNDIyKSI7CiBpZihzdGF0dXM9PT00MDF8fHN0YXR1cz09PTQwMylyZXR1cm4gItGB0LXRgNCy0LXRgCDQvdC1INGA0LDQt9GA0LXRiNC40Lsg0LfQsNC/0YDQvtGBICgiK3N0YXR1cysiKSI7CiBpZihzdGF0dXM+PTUwMClyZXR1cm4gItCy0L3Rg9GC0YDQtdC90L3Rj9GPINC+0YjQuNCx0LrQsCDRgdC10YDQstC10YDQsCAoIitzdGF0dXMrIikiOwogaWYodHlwZW9mIGRldGFpbD09PSJzdHJpbmciICYmICEvW8OQw5HDg8Oi4oKs77+9XS8udGVzdChkZXRhaWwpKXJldHVybiBkZXRhaWw7CiByZXR1cm4gItC+0YjQuNCx0LrQsCDQt9Cw0L/RgNC+0YHQsCAoSFRUUCAiK3N0YXR1cysiKSI7Cn0KYXN5bmMgZnVuY3Rpb24gYXBpKHBhdGgse21ldGhvZD0iR0VUIixib2R5PW51bGx9PXt9KXsKIGNvbnN0IG9wdHM9e21ldGhvZCxoZWFkZXJzOnsiQWNjZXB0IjoiYXBwbGljYXRpb24vanNvbiJ9fTsKIGlmKGJvZHkhPT1udWxsKXtvcHRzLmhlYWRlcnNbIkNvbnRlbnQtVHlwZSJdPSJhcHBsaWNhdGlvbi9qc29uIjtvcHRzLmJvZHk9SlNPTi5zdHJpbmdpZnkoYm9keSk7fQogbGV0IHI7dHJ5e3I9YXdhaXQgZmV0Y2goQVBJX0JBU0UrcGF0aCxvcHRzKX1jYXRjaChlKXt0aHJvdyBuZXcgRXJyb3IoItGB0LXRgNCy0LXRgCDQvdC10LTQvtGB0YLRg9C/0LXQvSDQuNC70Lgg0LHRgNCw0YPQt9C10YAg0LfQsNCx0LvQvtC60LjRgNC+0LLQsNC7INC30LDQv9GA0L7RgSAoQ09SUy/RgdC10YLRjCkiKTt9CiBsZXQgZD17fTt0cnl7ZD1hd2FpdCByLmpzb24oKX1jYXRjaHt9CiBpZighci5vayl7Y29uc3QgZXJyPW5ldyBFcnJvcihyZWFkYWJsZUFwaUVycm9yKGQsci5zdGF0dXMpKTtlcnIuc3RhdHVzPXIuc3RhdHVzO2Vyci5hcGlQYXRoPXBhdGg7dGhyb3cgZXJyO30KIHJldHVybiBkOwp9CmZ1bmN0aW9uIGFwcGx5UGxheWVyKHApe2lmKCFwKXJldHVybjtjb25zdCBwcz0kKCIjcHJvZmlsZVN0YXR1cyIpO2lmKHBzKXBzLnRleHRDb250ZW50PSLil48g0J/QoNCe0KTQmNCb0Kwg0KEg0KHQldCg0JLQldCg0JAiO3NldEFwaVN0YXR1cygiQVBJINC+0L3Qu9Cw0LnQvSDCtyDQv9GA0L7RhNC40LvRjCDQt9Cw0LPRgNGD0LbQtdC9Iix0cnVlKTtzdGF0ZS5uYW1lPXAubmlja25hbWV8fHN0YXRlLm5hbWU7c3RhdGUuZ2FtZUlkPXAuZ2FtZV9pZHx8c3RhdGUuZ2FtZUlkO3N0YXRlLmVsbz1OdW1iZXIocC5lbG8/PzEwMDApO3N0YXRlLndpbnM9TnVtYmVyKHAud2lucz8/MCk7c3RhdGUubG9zc2VzPU51bWJlcihwLmxvc3Nlcz8/MCk7c3RhdGUuZHJhd3M9TnVtYmVyKHAuZHJhd3M/PzApO3N0YXRlLmtpbGxzPU51bWJlcihwLmtpbGxzPz8wKTtzdGF0ZS5kZWF0aHM9TnVtYmVyKHAuZGVhdGhzPz8wKTtyZW5kZXIoKTt9CmFzeW5jIGZ1bmN0aW9uIHJlZnJlc2hBcGlEYXRhKCl7CiBpZighYXBpVXNlcklkKXJldHVybjsKIGNvbnN0IGpvYnM9WwogIGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PmFwcGx5UGxheWVyKGQucGxheWVyKSksCiAgYXBpKCIvbGVhZGVyYm9hcmQ/bGltaXQ9NTAiKS50aGVuKGQ9PnsKICAgY29uc3QgYT0oZC5wbGF5ZXJzfHxbXSkubWFwKHg9Pih7bmFtZTp4Lm5pY2tuYW1lLGVsbzpOdW1iZXIoeC5lbG98fDEwMDApLHdpbnM6TnVtYmVyKHgud2luc3x8MCksd3I6KE51bWJlcih4LndpbnN8fDApK051bWJlcih4Lmxvc3Nlc3x8MCkpP01hdGgucm91bmQoTnVtYmVyKHgud2luc3x8MCkvKE51bWJlcih4LndpbnN8fDApK051bWJlcih4Lmxvc3Nlc3x8MCkpKjEwMCk6MH0pKTsKICAgaWYoYS5sZW5ndGgpeyQoIiNob21lTGVhZGVycyIpLmlubmVySFRNTD1hLnNsaWNlKDAsNSkubWFwKCh4LGkpPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48c3BhbiBjbGFzcz0icmFuayI+JHtpKzF9PC9zcGFuPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MoKHgubmFtZXx8Ij8iKVswXS50b1VwcGVyQ2FzZSgpKX08L2Rpdj48ZGl2PjxiPiR7ZXNjKHgubmFtZSl9PC9iPjxzbWFsbD4ke3gud2luc30g0L/QvtCx0LXQtDwvc21hbGw+PC9kaXY+PC9kaXY+PHNwYW4gY2xhc3M9ImVsbyI+JHt4LmVsb308L3NwYW4+PC9kaXY+YCkuam9pbigiIik7JCgiI3JhdGluZ1Jvd3MiKS5pbm5lckhUTUw9YS5tYXAoKHgsaSk9PmA8dHI+PHRkPiR7aSsxfTwvdGQ+PHRkPjxiPiR7ZXNjKHgubmFtZSl9PC9iPjwvdGQ+PHRkPjxiPiR7eC5lbG99PC9iPjwvdGQ+PHRkPiR7eC53aW5zfTwvdGQ+PHRkPiR7eC53cn0lPC90ZD48L3RyPmApLmpvaW4oIiIpO30KICB9KSwKICBhcGkoYC9mcmllbmRzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57c3RhdGUuZnJpZW5kcz0oZC5mcmllbmRzfHxbXSkubWFwKGY9Pih7bmFtZTpmLm5pY2tuYW1lLHRlbGVncmFtX2lkOmYudGVsZWdyYW1faWQsc3RhdHVzOiLQlNGA0YPQsyIsZWxvOk51bWJlcihmLmVsb3x8MTAwMCl9KSk7cmVuZGVyRnJpZW5kcygpO30pLAogIGFwaShgL25vdGlmaWNhdGlvbnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PntzdGF0ZS5ub3RpY2VzPShkLm5vdGlmaWNhdGlvbnN8fFtdKS5tYXAobj0+KHtpZDpuLm5vdGlmaWNhdGlvbl9pZCxpY29uOm4ua2luZD09PSJtYXRjaF9mb3VuZCI/IuKalCI6IuKcpiIsdGl0bGU6bi50aXRsZSxib2R5Om4uYm9keSxyZWFkOiEhbi5pc19yZWFkfSkpO3JlbmRlck5vdGljZXMoKTt9KSwKICBhcGkoYC9wYXNzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57aWYoZC5hY3RpdmUhPT1mYWxzZSl7c3RhdGUueHA9TnVtYmVyKGQueHB8fDApO3JlbmRlcigpO319KSwKICBhcGkoYC9yZXdhcmRzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57CiAgIGNvbnN0IGVhcm5lZD1uZXcgU2V0KChkLnJld2FyZHN8fFtdKS5tYXAocj0+ci5uYW1lKSk7CiAgICQkKCIuY2xhaW0iKS5mb3JFYWNoKGI9Pntjb25zdCBuYW1lPWIuZGF0YXNldC5yZXdhcmQ7aWYoZWFybmVkLmhhcyhuYW1lKSl7Yi50ZXh0Q29udGVudD0i0J/QvtC70YPRh9C10L3QviI7Yi5kaXNhYmxlZD10cnVlO319KTsKICB9KSwKICBhcGkoIi9jbGFucz9saW1pdD01MCIpLnRoZW4oYXN5bmMgZD0+ewogICBjb25zdCBjbGFucz1kLmNsYW5zfHxbXTsKICAgY29uc3Qgb3Bwb25lbnRTZWxlY3Q9JCgiI3dhck9wcG9uZW50SWQiKTtpZihvcHBvbmVudFNlbGVjdCl7Y29uc3Qgb3Bwb25lbnRzPWNsYW5zLmZpbHRlcih4PT54LmNsYW5faWQhPT1hY3RpdmVDbGFuSWQpO29wcG9uZW50U2VsZWN0LmlubmVySFRNTD0nPG9wdGlvbiB2YWx1ZT0iIj7QktGL0LHQtdGA0Lgg0LrQu9Cw0L0t0YHQvtC/0LXRgNC90LjQuuKApjwvb3B0aW9uPicrb3Bwb25lbnRzLm1hcCh4PT5gPG9wdGlvbiB2YWx1ZT0iJHtlc2MoeC5jbGFuX2lkKX0iPiR7ZXNjKHgubmFtZSl9IFske2VzYyh4LnRhZyl9XSDCtyAke051bWJlcih4LmVsb3x8MTAwMCl9IEVMTzwvb3B0aW9uPmApLmpvaW4oIiIpO30KICAgaWYoIWNsYW5zLmxlbmd0aCl7JCgiI2NsYW5NZW1iZXJzIikuaW5uZXJIVE1MPSc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCa0LvQsNC90L7QsiDQv9C+0LrQsCDQvdC10YIuINCh0L7Qt9C00LDQuSDQv9C10YDQstGL0Lkg0LrQu9Cw0L0uPC9wPic7cmV0dXJuO30KICAgY29uc3QgbWluZT1jbGFucy5maW5kKGM9PmMub3duZXJfaWQ9PT1hcGlVc2VySWQpO2NvbnN0IGM9bWluZXx8Y2xhbnNbMF07YWN0aXZlQ2xhbklkPWMuY2xhbl9pZDsKICAgaWYob3Bwb25lbnRTZWxlY3Qpe2NvbnN0IG9wcG9uZW50cz1jbGFucy5maWx0ZXIoeD0+eC5jbGFuX2lkIT09YWN0aXZlQ2xhbklkKTtvcHBvbmVudFNlbGVjdC5pbm5lckhUTUw9JzxvcHRpb24gdmFsdWU9IiI+0JLRi9Cx0LXRgNC4INC60LvQsNC9LdGB0L7Qv9C10YDQvdC40LrigKY8L29wdGlvbj4nK29wcG9uZW50cy5tYXAoeD0+YDxvcHRpb24gdmFsdWU9IiR7ZXNjKHguY2xhbl9pZCl9Ij4ke2VzYyh4Lm5hbWUpfSBbJHtlc2MoeC50YWcpfV0gwrcgJHtOdW1iZXIoeC5lbG98fDEwMDApfSBFTE88L29wdGlvbj5gKS5qb2luKCIiKTt9CiAgIGNvbnN0IG93bldhck5hbWU9JCgiI3dhck93bk5hbWUiKTtpZihvd25XYXJOYW1lKW93bldhck5hbWUudGV4dENvbnRlbnQ9U3RyaW5nKGMudGFnfHxjLm5hbWV8fCLQotCS0J7QmSDQmtCb0JDQnSIpLnRvVXBwZXJDYXNlKCk7CiAgICQoIiNjbGFuTmFtZSIpLmNoaWxkTm9kZXNbMF0udGV4dENvbnRlbnQ9Yy5uYW1lKyIgIjskKCIjY2xhblRhZyIpLnRleHRDb250ZW50PSIjIitjLnRhZzsKICAgY29uc3QgZGV0YWlsPWF3YWl0IGFwaShgL2NsYW5zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGMuY2xhbl9pZCl9YCksbWVtYmVycz1kZXRhaWwubWVtYmVyc3x8W107CiAgICQoIiNjbGFuTWVtYmVycyIpLmlubmVySFRNTD1tZW1iZXJzLm1hcChtPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48ZGl2IGNsYXNzPSJwbGF5ZXIiPjxkaXYgY2xhc3M9Im1pbmlhdmEiPiR7ZXNjKChtLm5pY2tuYW1lfHwiPyIpWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MobS5uaWNrbmFtZXx8bS50ZWxlZ3JhbV9pZCl9PC9iPjxzbWFsbD4ke2VzYyhtLnJvbGUpfTwvc21hbGw+PC9kaXY+PC9kaXY+PHNwYW4gY2xhc3M9ImVsbyI+JHtOdW1iZXIobS5lbG98fDEwMDApfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKXx8JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0JIg0LrQu9Cw0L3QtSDQv9C+0LrQsCDQvdC10YIg0YPRh9Cw0YHRgtC90LjQutC+0LIuPC9wPic7CiAgIGNvbnN0IGNvdW50PSQoIi5jYXJkaGVhZCAubXV0ZWQuc21hbGwiLCQoIiNwYWdlLWNsYW5zIikpO2lmKGNvdW50KWNvdW50LnRleHRDb250ZW50PWAke21lbWJlcnMubGVuZ3RofSAvIDIwYDsKICB9KQogXTsKIGNvbnN0IHJlc3VsdHM9YXdhaXQgUHJvbWlzZS5hbGxTZXR0bGVkKGpvYnMpOwogY29uc3QgZXJyb3JzPXJlc3VsdHMuZmlsdGVyKHg9Pnguc3RhdHVzPT09InJlamVjdGVkIik7CiBpZihlcnJvcnMubGVuZ3RoKWNvbnNvbGUud2Fybigi0J3QtdC60L7RgtC+0YDRi9C1INGA0LDQt9C00LXQu9GLIEFQSSDQvdC1INC30LDQs9GA0YPQt9C40LvQuNGB0Yw6IixlcnJvcnMubWFwKHg9PngucmVhc29uPy5tZXNzYWdlKSk7CiByZW5kZXIoKTsKfQphc3luYyBmdW5jdGlvbiBpbml0QXBpKCl7CiBjb25zdCB0Zz1nZXRUZWxlZ3JhbVVzZXIoKTthcGlVc2VySWQ9U3RyaW5nKHRnPy5pZHx8bG9jYWxTdG9yYWdlLmdldEl0ZW0oInN0YW5kcG9pbnRUZWxlZ3JhbUlkIil8fCIiKS50cmltKCk7CiBpZih0Zz8uaWQpe2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJzdGFuZHBvaW50VGVsZWdyYW1JZCIsYXBpVXNlcklkKTtpZih0Zy51c2VybmFtZSlzdGF0ZS5uYW1lPXRnLnVzZXJuYW1lO30KIC8vINCf0YDQvtCy0LXRgNGP0LXQvCDQtNC+0YHRgtGD0L/QvdC+0YHRgtGMINGB0LXRgNCy0LXRgNCwINC+0YLQtNC10LvRjNC90L4g0L7RgiDRgNC10LPQuNGB0YLRgNCw0YbQuNC4INC/0YDQvtGE0LjQu9GPLgogdHJ5ewogIGF3YWl0IGFwaSgiL2NvbmZpZyIpOwogIHNldEFwaVN0YXR1cygiQVBJOiDRgdC10YDQstC10YAg0L7QvdC70LDQudC9Iix0cnVlKTsKICBjb25zdCBxbj0kKCIjcXVldWVBcGlOb3RlIik7aWYocW4pcW4udGV4dENvbnRlbnQ9ItCh0LXRgNCy0LXRgCDQtNC+0YHRgtGD0L/QtdC9LiDQlNC70Y8g0L/QvtC40YHQutCwINC80LDRgtGH0LAg0L3Rg9C20LXQvSDQv9GA0L7RhNC40LvRjCBUZWxlZ3JhbSDQuCDRg9GB0L/QtdGI0L3QsNGPINGA0LXQs9C40YHRgtGA0LDRhtC40Y8uIjsKIH1jYXRjaChlKXtzZXRBcGlTdGF0dXMoIkFQSTog0L7RiNC40LHQutCwINGB0L7QtdC00LjQvdC10L3QuNGPIixmYWxzZSk7Y29uc29sZS5lcnJvcigiQVBJIGNvbmZpZzoiLGUpO3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC/0L7QtNC60LvRjtGH0LjRgtGM0YHRjyDQuiDRgdC10YDQstC10YDRgzogIitlLm1lc3NhZ2UpO3JldHVybjt9CiBpZighYXBpVXNlcklkKXtzZXRBcGlTdGF0dXMoIkFQSTog0L7RgtC60YDQvtC5INCyIFRlbGVncmFtIixmYWxzZSk7Y29uc3QgcW49JCgiI3F1ZXVlQXBpTm90ZSIpO2lmKHFuKXFuLnRleHRDb250ZW50PSLQntGC0LrRgNC+0Lkg0LzQuNC90Lgt0L/RgNC40LvQvtC20LXQvdC40LUg0YfQtdGA0LXQtyBUZWxlZ3JhbSwg0YfRgtC+0LHRiyDQv9C+0LvRg9GH0LjRgtGMIFRlbGVncmFtIElEINC4INC40YHQutCw0YLRjCDQvNCw0YLRhy4iO3JldHVybjt9CiB0cnl7CiAgdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApO2FwcGx5UGxheWVyKGQucGxheWVyfHxkKTt9CiAgY2F0Y2goZSl7CiAgIGlmKGUuc3RhdHVzIT09NDA0ICYmICEvSFRUUCA0MDR80L3QtSDQvdCw0LnQtNC10L0vaS50ZXN0KGUubWVzc2FnZSkpdGhyb3cgZTsKICAgY29uc3Qgbmlja25hbWU9KHRnPy51c2VybmFtZXx8c3RhdGUubmFtZXx8ItCY0LPRgNC+0LoiKS5zbGljZSgwLDI0KSxnYW1lX2lkPVN0cmluZyhzdGF0ZS5nYW1lSWR8fCgic3BfIithcGlVc2VySWQuc2xpY2UoLTgpKSkuc2xpY2UoMCwzMik7CiAgIGNvbnN0IGQ9YXdhaXQgYXBpKCIvcGxheWVycy9yZWdpc3RlciIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG5pY2tuYW1lLGdhbWVfaWR9fSk7YXBwbHlQbGF5ZXIoZC5wbGF5ZXJ8fGQpOwogIH0KICBhd2FpdCByZWZyZXNoQXBpRGF0YSgpOwogfWNhdGNoKGUpewogIC8vINCh0LXRgNCy0LXRgCDRg9C20LUg0L/QvtC00YLQstC10YDQtNC40Lsg0LTQvtGB0YLRg9C/0L3QvtGB0YLRjCDRh9C10YDQtdC3IC9jb25maWcg4oCUINC90LUg0L3QsNC30YvQstCw0LXQvCDRjdGC0L4g0L7QsdGJ0LXQuSDQvtGI0LjQsdC60L7QuSBBUEkuCiAgc2V0QXBpU3RhdHVzKCJBUEkg0L7QvdC70LDQudC9IMK3INC+0YjQuNCx0LrQsCDQv9GA0L7RhNC40LvRjyIsZmFsc2UpO2NvbnNvbGUuZXJyb3IoIlByb2ZpbGUgaW5pdDoiLGUpOwogIGNvbnN0IHFuPSQoIiNxdWV1ZUFwaU5vdGUiKTtpZihxbilxbi50ZXh0Q29udGVudD0i0KHQtdGA0LLQtdGAINC00L7RgdGC0YPQv9C10L0sINC90L4g0L3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0L/RgNC+0YTQuNC70YwuINCe0YLQutGA0L7QuSDCq9Cf0YDQvtGE0LjQu9GMwrssINC/0YDQvtCy0LXRgNGMINC90LjQutC90LXQudC8INC4INC40LPRgNC+0LLQvtC5IElEINC4INGB0L7RhdGA0LDQvdC4INC10YnRkSDRgNCw0LcuIjsKICB0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0L/RgNC+0YTQuNC70Yw6ICIrZS5tZXNzYWdlKTsKIH0KfQoKCi8vID09PT09IENsYW4gV2FyczogQVBJLWJhY2tlZCBjaGFsbGVuZ2UgYW5kIGxvYmJ5ID09PT09CmxldCBhY3RpdmVXYXJJZD0iIjsKYXN5bmMgZnVuY3Rpb24gbG9hZFdhckxvYmJ5KGlkKXtpZighaWR8fCFhcGlVc2VySWQpcmV0dXJuIHRvYXN0KCLQndGD0LbQtdC9IElEIENsYW4gV2FyINC4INC/0L7QtNC60LvRjtGH0ZHQvdC90YvQuSDQv9GA0L7RhNC40LvRjC4iKTt0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoaWQpfS9sb2JieWApO2FjdGl2ZVdhcklkPWlkOyQoIiN3YXJJZElucHV0IikudmFsdWU9aWQ7Y29uc3Qgdz1kLndhcix0ZWFtQT1kLnRlYW1zPy5BfHxbXSx0ZWFtQj1kLnRlYW1zPy5CfHxbXTskKCIjd2FyTG9iYnlDb250ZW50IikuaW5uZXJIVE1MPWA8ZGl2IGNsYXNzPSJtYy10d28iPjxkaXYgY2xhc3M9Im1jLXRlYW0iPjxoND7wn5+gICR7ZXNjKHcuY2xhbl9hX25hbWUpfSAjJHtlc2Mody5jbGFuX2FfdGFnKX08L2g0Pjx1bD4ke3RlYW1BLm1hcChwPT5gPGxpPiR7ZXNjKHAubmlja25hbWUpfSDCtyAke051bWJlcihwLmVsbyl9IEVMTzwvbGk+YCkuam9pbigiIil8fCI8bGk+0KHQvtGB0YLQsNCyINC90LUg0LfQsNCz0YDRg9C20LXQvTwvbGk+In08L3VsPjwvZGl2PjxkaXYgY2xhc3M9Im1jLXRlYW0iPjxoND7wn5S1ICR7ZXNjKHcuY2xhbl9iX25hbWUpfSAjJHtlc2Mody5jbGFuX2JfdGFnKX08L2g0Pjx1bD4ke3RlYW1CLm1hcChwPT5gPGxpPiR7ZXNjKHAubmlja25hbWUpfSDCtyAke051bWJlcihwLmVsbyl9IEVMTzwvbGk+YCkuam9pbigiIil8fCI8bGk+0KHQvtGB0YLQsNCyINC90LUg0LfQsNCz0YDRg9C20LXQvTwvbGk+In08L3VsPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLW5vdGljZSIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+0KHRgtCw0YLRg9GBOiA8Yj4ke2VzYyh3LnN0YXR1cyl9PC9iPiDCtyBJRCAke2VzYyh3Lndhcl9pZCl9JHt3LnN0YXR1cz09PSJhY2NlcHRlZCI/IiDCtyDQktGL0LfQvtCyINC/0YDQuNC90Y/Rgiwg0LzQvtC20L3QviDQv9GA0L7QstC10YHRgtC4INGB0LXRgNC40Y4g0Lgg0L7RgtC/0YDQsNCy0LjRgtGMINGB0YfRkdGCLiI6IiJ9PC9kaXY+JHt3LnN0YXR1cz09PSJwZW5kaW5nIj9gPGRpdiBjbGFzcz0ibWMtYWN0aW9ucyIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJhY2NlcHRXYXJCdG4iPtCf0YDQuNC90Y/RgtGMINCy0YvQt9C+0LI8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0icmVqZWN0V2FyQnRuIj7QntGC0LrQu9C+0L3QuNGC0Yw8L2J1dHRvbj48L2Rpdj5gOiIifSR7dy5zdGF0dXM9PT0iYWNjZXB0ZWQiP2A8ZGl2IGNsYXNzPSJtYy1mb3JtZ3JpZCIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QodCn0IHQoiDQmtCb0JDQndCQIEE8L2xhYmVsPjxpbnB1dCBpZD0id2FyU2NvcmVBIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAiIHZhbHVlPSIxMyI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QodCn0IHQoiDQmtCb0JDQndCQIEI8L2xhYmVsPjxpbnB1dCBpZD0id2FyU2NvcmVCIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAiIHZhbHVlPSI5Ij48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9InN1Ym1pdFdhclNjb3JlQnRuIiBzdHlsZT0id2lkdGg6MTAwJSI+0J7RgtC/0YDQsNCy0LjRgtGMINGA0LXQt9GD0LvRjNGC0LDRgiDQvNC+0LTQtdGA0LDRgtC+0YDRgzwvYnV0dG9uPmA6IiJ9YDtjb25zdCBhY2NlcHQ9JCgiI2FjY2VwdFdhckJ0biIpO2lmKGFjY2VwdClhY2NlcHQub25jbGljaz0oKT0+ZGVjaWRlV2FyKCJhY2NlcHQiKTtjb25zdCByZWplY3Q9JCgiI3JlamVjdFdhckJ0biIpO2lmKHJlamVjdClyZWplY3Qub25jbGljaz0oKT0+ZGVjaWRlV2FyKCJyZWplY3QiKTtjb25zdCBzdWJtaXQ9JCgiI3N1Ym1pdFdhclNjb3JlQnRuIik7aWYoc3VibWl0KXN1Ym1pdC5vbmNsaWNrPWFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvY2xhbi13YXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZVdhcklkKX0vcmVzdWx0YCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsc2NvcmVfYTpOdW1iZXIoJCgiI3dhclNjb3JlQSIpLnZhbHVlKSxzY29yZV9iOk51bWJlcigkKCIjd2FyU2NvcmVCIikudmFsdWUpfX0pO3RvYXN0KCLQoNC10LfRg9C70YzRgtCw0YIgQ2xhbiBXYXIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgy4iKTthd2FpdCBsb2FkV2FyTG9iYnkoYWN0aXZlV2FySWQpO31jYXRjaChlKXt0b2FzdCgi0KDQtdC30YPQu9GM0YLQsNGCIENsYW4gV2FyOiAiK2UubWVzc2FnZSk7fX07JCgiI3dhclN0YXR1c1RleHQiKS50ZXh0Q29udGVudD1gQ2xhbiBXYXIgJHt3Lndhcl9pZH06ICR7dy5zdGF0dXN9YDt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L7RgtC60YDRi9GC0Ywg0LvQvtCx0LHQuCBDbGFuIFdhcjogIitlLm1lc3NhZ2UpO319CmFzeW5jIGZ1bmN0aW9uIGRlY2lkZVdhcihkZWNpc2lvbil7dHJ5e2F3YWl0IGFwaShgL2NsYW4td2Fycy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVXYXJJZCl9L2RlY2lzaW9uYCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsZGVjaXNpb259fSk7dG9hc3QoZGVjaXNpb249PT0iYWNjZXB0Ij8i0JLRi9C30L7QsiDQv9GA0LjQvdGP0YIuIjoi0JLRi9C30L7QsiDQvtGC0LrQu9C+0L3RkdC9LiIpO2F3YWl0IGxvYWRXYXJMb2JieShhY3RpdmVXYXJJZCk7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC+0LHRgNCw0LHQvtGC0LDRgtGMINCy0YvQt9C+0LI6ICIrZS5tZXNzYWdlKTt9fQokKCIjY2hhbGxlbmdlV2FyQnRuIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57Y29uc3Qgb3Bwb25lbnRfY2xhbl9pZD0kKCIjd2FyT3Bwb25lbnRJZCIpLnZhbHVlLnRyaW0oKTtpZighYXBpVXNlcklkfHwhb3Bwb25lbnRfY2xhbl9pZClyZXR1cm4gdG9hc3QoItCj0LrQsNC20LggSUQg0LrQu9Cw0L3QsCDRgdC+0L/QtdGA0L3QuNC60LAg0Lgg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjC4iKTt0cnl7Y29uc3QgZD1hd2FpdCBhcGkoIi9jbGFuLXdhcnMiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxvcHBvbmVudF9jbGFuX2lkfX0pO2FjdGl2ZVdhcklkPWQud2FyX2lkOyQoIiN3YXJJZElucHV0IikudmFsdWU9ZC53YXJfaWQ7dG9hc3QoItCS0YvQt9C+0LIgQ2xhbiBXYXIg0L7RgtC/0YDQsNCy0LvQtdC9LiIpO2F3YWl0IGxvYWRXYXJMb2JieShkLndhcl9pZCk7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINGB0L7Qt9C00LDRgtGMIENsYW4gV2FyOiAiK2UubWVzc2FnZSk7fX0pOwokKCIjbG9hZFdhckxvYmJ5QnRuIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+bG9hZFdhckxvYmJ5KCQoIiN3YXJJZElucHV0IikudmFsdWUudHJpbSgpKSk7CgovLyA9PT09PSBNYXRjaCBDZW50ZXI6IGFjY2VwdCAtPiBjYXB0YWluIGRyYWZ0IC0+IGJhbi9waWNrIC0+IGxvYmJ5IC0+IHJlc3VsdCAtPiBhcHBlYWwgPT09PT0KbGV0IG1hdGNoUG9sbFRpbWVyPW51bGwsY3VycmVudE1hdGNoPW51bGw7CmNvbnN0IE1BVENIX01BUFM9WyJCcmVlemUiLCJDYWx5cHNvIiwiRHVuZSIsIlByb3ZpbmNlIiwiUnVzdCIsIlNha3VyYSIsIlNhbmRzdG9uZSIsIlpvbmUgNyJdOwpmdW5jdGlvbiBzdG9wUXVldWVQb2xsaW5nKCl7aWYocXVldWVQb2xsVGltZXIpe2NsZWFySW50ZXJ2YWwocXVldWVQb2xsVGltZXIpO3F1ZXVlUG9sbFRpbWVyPW51bGw7fX0KZnVuY3Rpb24gc3RvcE1hdGNoUG9sbGluZygpe2lmKG1hdGNoUG9sbFRpbWVyKXtjbGVhckludGVydmFsKG1hdGNoUG9sbFRpbWVyKTttYXRjaFBvbGxUaW1lcj1udWxsO319CmZ1bmN0aW9uIG1jU2hvdyhpZCl7WyJtY0FjY2VwdFBhbmVsIiwibWNCYW5QYW5lbCIsIm1jTG9iYnlQYW5lbCIsIm1jUGxheWluZ1BhbmVsIiwibWNSZXZpZXdQYW5lbCJdLmZvckVhY2goeD0+JCgiIyIreCkuY2xhc3NMaXN0LnRvZ2dsZSgibWMtaGlkZGVuIix4IT09aWQpKTskKCIjbWF0Y2hDZW50ZXIiKS5jbGFzc0xpc3QuYWRkKCJ2aXNpYmxlIik7fQpmdW5jdGlvbiBtYXRjaFBsYXllck5hbWUoaWQpe3JldHVybiAoY3VycmVudE1hdGNoPy5wbGF5ZXJfcHJvZmlsZXM/LltpZF0/Lm5pY2tuYW1lKXx8KCLQmNCz0YDQvtC6ICIrU3RyaW5nKGlkfHwiIikuc2xpY2UoLTQpKTt9CmZ1bmN0aW9uIHJlbmRlck1jUGxheWVycygpe2NvbnN0IHBsYXllcnM9Y3VycmVudE1hdGNoPy5wbGF5ZXJzfHxbXSxhY2NlcHRhbmNlPWN1cnJlbnRNYXRjaD8uYWNjZXB0YW5jZXx8e307JCgiI21jQWNjZXB0UGxheWVycyIpLmlubmVySFRNTD1wbGF5ZXJzLm1hcCgoaWQsaSk9Pntjb25zdCBhY2NlcHRlZD0hIWFjY2VwdGFuY2VbaWRdO3JldHVybiBgPGRpdiBjbGFzcz0ibWMtcGxheWVyICR7YWNjZXB0ZWQ/ImFjY2VwdGVkIjoiIn0iPjxkaXYgY2xhc3M9Im1jLWF2YXRhciI+JHthY2NlcHRlZD8i4pyTIjppKzF9PC9kaXY+PGI+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGlkKSl9PC9iPjxzbWFsbD4ke2FjY2VwdGVkPyLQn9Cg0JjQndCv0JsiOiLQntCW0JjQlNCQ0J3QmNCVIn08L3NtYWxsPjwvZGl2PmB9KS5qb2luKCIiKTtjb25zdCBuPU51bWJlcihjdXJyZW50TWF0Y2g/LmFjY2VwdGVkX2NvdW50fHwwKSx0b3RhbD1OdW1iZXIoY3VycmVudE1hdGNoPy5wbGF5ZXJzX25lZWRlZHx8cGxheWVycy5sZW5ndGh8fDApOyQoIiNtY0FjY2VwdFRleHQiKS50ZXh0Q29udGVudD1g0J/RgNC40L3Rj9C70LggJHtufSDQuNC3ICR7dG90YWx9LiDQlNC70Y8g0YHRgtCw0YDRgtCwINCy0YHQtSDRg9GH0LDRgdGC0L3QuNC60Lgg0LTQvtC70LbQvdGLINC/0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LzQsNGC0YcuYDskKCIjbWNBY2NlcHRQcm9ncmVzcyIpLnN0eWxlLndpZHRoPSh0b3RhbD9NYXRoLm1pbigxMDAsbi90b3RhbCoxMDApOjApKyIlIjskKCIjbWNBY2NlcHRCdG4iKS5kaXNhYmxlZD0hIWFjY2VwdGFuY2VbYXBpVXNlcklkXTskKCIjbWNBY2NlcHRCdG4iKS50ZXh0Q29udGVudD1hY2NlcHRhbmNlW2FwaVVzZXJJZF0/IuKckyDQotGLINC/0YDQuNC90Y/QuyDQvNCw0YLRhyI6IuKckyDQn9GA0LjQvdGP0YLRjCDQvNCw0YLRhyI7fQpmdW5jdGlvbiBmaWxsQ2FwdGFpblNlbGVjdHMoKXtjb25zdCBwbGF5ZXJzPWN1cnJlbnRNYXRjaD8ucGxheWVyc3x8W10sc2l6ZT0oeyIxdjEiOjEsIjJ2MiI6MiwiM3YzIjozLCI1djUiOjV9KVtjdXJyZW50TWF0Y2g/Lm1vZGVdfHw1LHRlYW1zPXtBOnBsYXllcnMuc2xpY2UoMCxzaXplKSxCOnBsYXllcnMuc2xpY2Uoc2l6ZSl9O1siQSIsIkIiXS5mb3JFYWNoKHQ9Pntjb25zdCBzZWw9JCgiI21jQ2FwdGFpbiIrdCksaWRzPXRlYW1zW3RdO3NlbC5pbm5lckhUTUw9aWRzLm1hcChpZD0+YDxvcHRpb24gdmFsdWU9IiR7ZXNjKGlkKX0iPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfTwvb3B0aW9uPmApLmpvaW4oIiIpO2NvbnN0IGNhcHRhaW49Y3VycmVudE1hdGNoPy5bdD09PSJBIj8iY2FwdGFpbl9pZCI6ImNhcHRhaW5fYl9pZCJdO2lmKGNhcHRhaW4pc2VsLnZhbHVlPWNhcHRhaW47JCgiI21jQ2FwdGFpbiIrdCsiSW5mbyIpLnRleHRDb250ZW50PWNhcHRhaW4/ItCa0LDQv9C40YLQsNC9INC90LDQt9C90LDRh9C10L06ICIrbWF0Y2hQbGF5ZXJOYW1lKGNhcHRhaW4pOiLQmtCw0L/QuNGC0LDQvSDQtdGJ0ZEg0L3QtSDQstGL0LHRgNCw0L0iO30pO30KZnVuY3Rpb24gcmVuZGVyTWFwUGljaygpe2NvbnN0IGFjdGlvbnM9Y3VycmVudE1hdGNoPy5tYXBfYWN0aW9uc3x8W10sYmFucz1hY3Rpb25zLmZpbHRlcihhPT5hLmFjdGlvbj09PSJiYW4iKS5sZW5ndGgsZmluaXNoZWQ9ISFjdXJyZW50TWF0Y2g/Lm1hcF9uYW1lOyQoIiNtY0JhblRpdGxlIikudGV4dENvbnRlbnQ9ZmluaXNoZWQ/ItCa0LDRgNGC0LAg0LLRi9Cx0YDQsNC90LA6ICIrY3VycmVudE1hdGNoLm1hcF9uYW1lOmBCYW4gLyBQaWNrIMK3ICR7TWF0aC5taW4oYmFucyw0KX0vNCDQsdCw0L3QvtCyYDtjb25zdCBleHBlY3RlZD1iYW5zJTI9PT0wP2N1cnJlbnRNYXRjaD8uY2FwdGFpbl9pZDooY3VycmVudE1hdGNoPy5jYXB0YWluX2JfaWR8fGN1cnJlbnRNYXRjaD8ucGxheWVycz8uWyh7IjF2MSI6MSwiMnYyIjoyLCIzdjMiOjMsIjV2NSI6NX0pW2N1cnJlbnRNYXRjaD8ubW9kZV18fDVdKTtjb25zdCBjYW5BY3Q9IWZpbmlzaGVkJiZhcGlVc2VySWQ9PT1leHBlY3RlZDskKCIjbWNCYW5IaW50IikudGV4dENvbnRlbnQ9ZmluaXNoZWQ/IkJhbi1waWNrINC30LDQstC10YDRiNGR0L0uINCf0LXRgNC10YXQvtC00Lgg0LIg0LvQvtCx0LHQuCDQvNCw0YLRh9CwLiI6YmFuczw0P2DQodC70LXQtNGD0Y7RidC40Lkg0LHQsNC9OiAke21hdGNoUGxheWVyTmFtZShleHBlY3RlZCl9JHtjYW5BY3Q/IiDigJQg0YLQstC+0Lkg0YXQvtC0IjoiIOKAlCDQvtC20LjQtNCw0LXQvCDQutCw0L/QuNGC0LDQvdCwIn0uYDpg0JHQsNC90Ysg0LfQsNCy0LXRgNGI0LXQvdGLLiDQmtCw0L/QuNGC0LDQvSBBINCy0YvQsdC40YDQsNC10YIg0LrQsNGA0YLRgyR7YXBpVXNlcklkPT09Y3VycmVudE1hdGNoPy5jYXB0YWluX2lkPyIg4oCUINCy0YvQsdC10YDQuCDQutCw0YDRgtGDINC90LjQttC1IjoiLiJ9YDtjb25zdCB1c2VkPW5ldyBTZXQoYWN0aW9ucy5tYXAoYT0+YS5tYXBfbmFtZSkpOyQoIiNtY01hcEJ1dHRvbnMiKS5pbm5lckhUTUw9TUFUQ0hfTUFQUy5tYXAobmFtZT0+e2xldCBhY3Rpb249ImJhbiI7aWYoYmFucz49NClhY3Rpb249InBpY2siO3JldHVybiBgPGJ1dHRvbiBjbGFzcz0ibWMtbWFwICR7dXNlZC5oYXMobmFtZSk/InVzZWQiOiIifSIgZGF0YS1tYy1tYXA9IiR7ZXNjKG5hbWUpfSIgZGF0YS1tYy1hY3Rpb249IiR7YWN0aW9ufSIgJHsoIWNhbkFjdHx8ZmluaXNoZWR8fHVzZWQuaGFzKG5hbWUpKT8iZGlzYWJsZWQiOiIifT4ke2VzYyhuYW1lKX08c21hbGwgc3R5bGU9ImRpc3BsYXk6YmxvY2s7Y29sb3I6IzlkYTliNTttYXJnaW4tdG9wOjRweCI+JHt1c2VkLmhhcyhuYW1lKT8oYWN0aW9ucy5maW5kKGE9PmEubWFwX25hbWU9PT1uYW1lKT8uYWN0aW9uPT09ImJhbiI/ItCX0JDQkdCQ0J3QldCd0JAiOiLQktCr0JHQoNCQ0J3QkCIpOmFjdGlvbj09PSJiYW4iPyJCQU4iOiJQSUNLIn08L3NtYWxsPjwvYnV0dG9uPmB9KS5qb2luKCIiKTskKCIjbWNNYXBMb2ciKS5pbm5lckhUTUw9YWN0aW9ucy5tYXAoKGEsaSk9PmA8ZGl2IGNsYXNzPSJtYy1sb2dyb3ciPjxzcGFuPiR7aSsxfS4gJHtlc2MoYS5hY3Rpb249PT0iYmFuIj8iQkFOIjoiUElDSyIpfSDCtyAke2VzYyhhLm1hcF9uYW1lKX08L3NwYW4+PHNwYW4+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGEuYnkpKX08L3NwYW4+PC9kaXY+YCkuam9pbigiIil8fCc8cCBjbGFzcz0ibWMtbXV0ZWQiPtCf0L7QutCwINC90LXRgiDQtNC10LnRgdGC0LLQuNC5INC90LAg0LrQsNGA0YLQtS48L3A+JzskJCgiW2RhdGEtbWMtbWFwXSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9tYXAtYWN0aW9uYCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsYWN0aW9uOmIuZGF0YXNldC5tY0FjdGlvbixtYXBfbmFtZTpiLmRhdGFzZXQubWNNYXB9fSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7fWNhdGNoKGUpe3RvYXN0KCJCYW4vUGljazogIitlLm1lc3NhZ2UpO319KSk7fQpmdW5jdGlvbiByZW5kZXJNYXRjaCgpe2lmKCFjdXJyZW50TWF0Y2gpcmV0dXJuO2NvbnN0IG09Y3VycmVudE1hdGNoOyQoIiNtYXRjaENlbnRlciIpLmNsYXNzTGlzdC5hZGQoInZpc2libGUiKTskKCIjbWNTdWJ0aXRsZSIpLnRleHRDb250ZW50PWDQnNCw0YLRhyAke20ubWF0Y2hfaWR9IMK3ICR7bS5tb2RlfSDCtyAke20ucGxheWVycz8ubGVuZ3RofHwwfSDQuNCz0YDQvtC60L7QsmA7JCgiI21jU3RhdHVzIikudGV4dENvbnRlbnQ9U3RyaW5nKG0uc3RhdHVzfHwiTUFUQ0giKS5yZXBsYWNlQWxsKCJfIiwiICIpLnRvVXBwZXJDYXNlKCk7JCgiI21jVGl0bGUiKS50ZXh0Q29udGVudD0oe2FjY2VwdGluZzoi0JzQsNGC0Ycg0L3QsNC50LTQtdC9IOKAlCDQv9C+0LTRgtCy0LXRgNC00LgiLG1hcF9waWNrOiJCYW4gLyBQaWNrINC60LDRgNGCIixyZWFkeToi0JvQvtCx0LHQuCDQs9C+0YLQvtCy0L4iLHBsYXlpbmc6ItCc0LDRgtGHINCyINC40LPRgNC1Iix1bmRlcl9yZXZpZXc6ItCg0LXQt9GD0LvRjNGC0LDRgiDQvdCwINC/0YDQvtCy0LXRgNC60LUiLGNvbmZpcm1lZDoi0JzQsNGC0Ycg0L/QvtC00YLQstC10YDQttC00ZHQvSIscmVqZWN0ZWQ6ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGC0LrQu9C+0L3RkdC9IixkaXNwdXRlZDoi0KDQtdC30YPQu9GM0YLQsNGCINC+0YHQv9Cw0YDQuNCy0LDQtdGC0YHRjyIsZGVjbGluZWQ6ItCc0LDRgtGHINC+0YLQutC70L7QvdGR0L0iLGV4cGlyZWQ6ItCS0YDQtdC80Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8g0LjRgdGC0LXQutC70L4ifSlbbS5zdGF0dXNdfHwi0JzQsNGC0YciOwogaWYobS5zdGF0dXM9PT0iYWNjZXB0aW5nIil7bWNTaG93KCJtY0FjY2VwdFBhbmVsIik7cmVuZGVyTWNQbGF5ZXJzKCk7cmV0dXJuO30KIGlmKG0uc3RhdHVzPT09Im1hcF9waWNrIil7bWNTaG93KCJtY0JhblBhbmVsIik7ZmlsbENhcHRhaW5TZWxlY3RzKCk7cmVuZGVyTWFwUGljaygpO3JldHVybjt9CiBpZihtLnN0YXR1cz09PSJyZWFkeSIpe21jU2hvdygibWNMb2JieVBhbmVsIik7Y29uc3Qgc2l6ZT0oeyIxdjEiOjEsIjJ2MiI6MiwiM3YzIjozLCI1djUiOjV9KVttLm1vZGVdfHw1LHRhPShtLnBsYXllcnN8fFtdKS5zbGljZSgwLHNpemUpLHRiPShtLnBsYXllcnN8fFtdKS5zbGljZShzaXplKTskKCIjbWNUZWFtQSIpLmlubmVySFRNTD10YS5tYXAoaWQ9PmA8bGk+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGlkKSl9JHtpZD09PW0uY2FwdGFpbl9pZD8iIMK3INCa0JDQn9CY0KLQkNCdIjoiIn08L2xpPmApLmpvaW4oIiIpOyQoIiNtY1RlYW1CIikuaW5uZXJIVE1MPXRiLm1hcChpZD0+YDxsaT4ke2VzYyhtYXRjaFBsYXllck5hbWUoaWQpKX0ke2lkPT09bS5jYXB0YWluX2JfaWQ/IiDCtyDQmtCQ0J/QmNCi0JDQnSI6IiJ9PC9saT5gKS5qb2luKCIiKTskKCIjbWNMb2JieUluZm8iKS50ZXh0Q29udGVudD1g0JrQsNGA0YLQsDogJHttLm1hcF9uYW1lfHwi0L3QtSDQstGL0LHRgNCw0L3QsCJ9LiDQmtCw0L/QuNGC0LDQvSDQutC+0LzQsNC90LTRiyBBINC30LDQv9GD0YHQutCw0LXRgiDQvNCw0YLRhy5gOyQoIiNtY1N0YXJ0QnRuIikuZGlzYWJsZWQ9YXBpVXNlcklkIT09bS5jYXB0YWluX2lkOyQoIiNtY1N0YXJ0QnRuIikudGV4dENvbnRlbnQ9YXBpVXNlcklkPT09bS5jYXB0YWluX2lkPyLilrYg0J3QsNGH0LDRgtGMINC80LDRgtGHIjoi0J7QttC40LTQsNC10Lwg0LrQsNC/0LjRgtCw0L3QsCDQutC+0LzQsNC90LTRiyBBIjtyZXR1cm47fQogaWYobS5zdGF0dXM9PT0icGxheWluZyIpe21jU2hvdygibWNQbGF5aW5nUGFuZWwiKTtyZXR1cm47fQogaWYoWyJ1bmRlcl9yZXZpZXciLCJjb25maXJtZWQiLCJyZWplY3RlZCIsImRpc3B1dGVkIl0uaW5jbHVkZXMobS5zdGF0dXMpKXttY1Nob3coIm1jUmV2aWV3UGFuZWwiKTskKCIjbWNSZXZpZXdJbmZvIikudGV4dENvbnRlbnQ9bS5zdGF0dXM9PT0iY29uZmlybWVkIj9g0KDQtdC30YPQu9GM0YLQsNGCINC/0L7QtNGC0LLQtdGA0LbQtNGR0L06ICR7bS5zY29yZV9hPz8i4oCUIn06JHttLnNjb3JlX2I/PyLigJQifS4g0KDQtdC50YLQuNC90LMg0LzQvtC20LXRgiDQsdGL0YLRjCDQvtCx0L3QvtCy0LvRkdC9INC80L7QtNC10YDQsNGG0LjQtdC5LmA6bS5zdGF0dXM9PT0icmVqZWN0ZWQiPyLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC60LvQvtC90ZHQvSDQvNC+0LTQtdGA0LDRgtC+0YDQvtC8LiDQn9GA0L7QstC10YDRjCDRg9Cy0LXQtNC+0LzQu9C10L3QuNGPLiI6bS5zdGF0dXM9PT0iZGlzcHV0ZWQiPyLQoNC10LfRg9C70YzRgtCw0YIg0L7RgdC/0LDRgNC40LLQsNC10YLRgdGPLiDQnNC+0LTQtdGA0LDRgtC+0YDRgyDRgtGA0LXQsdGD0LXRgtGB0Y8g0L/RgNC+0LLQtdGA0LrQsC4iOiLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgyDQvNC+0LTQtdGA0LDRgtC+0YDRgy4gRUxPINC90LUg0LzQtdC90Y/QtdGC0YHRjyDQtNC+INC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLiI7JCgiI21jQXBwZWFsRm9ybSIpLmNsYXNzTGlzdC50b2dnbGUoIm1jLWhpZGRlbiIsIVsiY29uZmlybWVkIiwicmVqZWN0ZWQiLCJkaXNwdXRlZCJdLmluY2x1ZGVzKG0uc3RhdHVzKSk7cmV0dXJuO30KIG1jU2hvdygibWNSZXZpZXdQYW5lbCIpOyQoIiNtY1Jldmlld0luZm8iKS50ZXh0Q29udGVudD0i0JzQsNGC0Ycg0LfQsNCy0LXRgNGI0LjQuyDQv9GA0L7RhtC10YHRgSDQv9GA0LjQvdGP0YLQuNGPOiAiK20uc3RhdHVzOwp9CmFzeW5jIGZ1bmN0aW9uIHBvbGxBY3RpdmVNYXRjaCgpe2lmKCFhY3RpdmVNYXRjaElkKXJldHVybjt0cnl7Y29uc3QgbT1hd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfWApO2N1cnJlbnRNYXRjaD1tO3JlbmRlck1hdGNoKCk7aWYoWyJjb25maXJtZWQiLCJyZWplY3RlZCIsImRlY2xpbmVkIiwiZXhwaXJlZCJdLmluY2x1ZGVzKG0uc3RhdHVzKSlzdG9wTWF0Y2hQb2xsaW5nKCk7fWNhdGNoKGUpe2NvbnNvbGUud2FybigiTWF0Y2ggcG9sbCIsZS5tZXNzYWdlKTt9fQphc3luYyBmdW5jdGlvbiBzaG93TWF0Y2goaWQpe2FjdGl2ZU1hdGNoSWQ9aWQ7cXVldWVBY3RpdmU9ZmFsc2U7c3RvcFF1ZXVlUG9sbGluZygpOyQoIiNxdWV1ZUJ1dHRvbiIpLnRleHRDb250ZW50PSLQndCw0LnRgtC4INC80LDRgtGHIjtuYXYoIm1hdGNoZXMiKTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTtzdG9wTWF0Y2hQb2xsaW5nKCk7bWF0Y2hQb2xsVGltZXI9c2V0SW50ZXJ2YWwoKCk9PntpZighZG9jdW1lbnQuaGlkZGVuKXBvbGxBY3RpdmVNYXRjaCgpfSwzMDAwKTt9CmFzeW5jIGZ1bmN0aW9uIHN0YXJ0UXVldWVQb2xsaW5nKCl7c3RvcFF1ZXVlUG9sbGluZygpO2NvbnN0IHVwZGF0ZT1hc3luYygpPT57aWYoIXF1ZXVlQWN0aXZlfHxkb2N1bWVudC5oaWRkZW4pcmV0dXJuO3RyeXtjb25zdCBxPWF3YWl0IGFwaShgL3F1ZXVlLyR7ZW5jb2RlVVJJQ29tcG9uZW50KHNlbGVjdGVkTW9kZSl9YCk7Y29uc3QgZm91bmQ9TnVtYmVyKHEucGxheWVyc19mb3VuZHx8MCksbmVlZGVkPU51bWJlcihxLnBsYXllcnNfbmVlZGVkfHwwKTskKCIjcXVldWVUaXRsZSIpLnRleHRDb250ZW50PSLQmNGJ0LXQvCDRgdC+0L/QtdGA0L3QuNC60L7QsuKApiI7JCgiI3F1ZXVlRGVzY3JpcHRpb24iKS50ZXh0Q29udGVudD1g0JIg0L7Rh9C10YDQtdC00LggJHtmb3VuZH0vJHtuZWVkZWR9OyDQvtGB0YLQsNC70L7RgdGMICR7TWF0aC5tYXgoMCxuZWVkZWQtZm91bmQpfWA7Y29uc3QgYWN0aXZlPWF3YWl0IGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX0vYWN0aXZlLW1hdGNoYCk7aWYoYWN0aXZlLmFjdGl2ZSYmYWN0aXZlLm1hdGNoKXthY3RpdmVNYXRjaElkPWFjdGl2ZS5tYXRjaC5tYXRjaF9pZDthd2FpdCBzaG93TWF0Y2goYWN0aXZlTWF0Y2hJZCk7dG9hc3QoItCc0LDRgtGHINC90LDQudC00LXQvSEg0J/QvtC00YLQstC10YDQtNC4INGD0YfQsNGB0YLQuNC1LiIpO319Y2F0Y2goZSl7Y29uc29sZS53YXJuKCJRdWV1ZSBzdGF0dXMiLGUubWVzc2FnZSk7fX07YXdhaXQgdXBkYXRlKCk7cXVldWVQb2xsVGltZXI9c2V0SW50ZXJ2YWwodXBkYXRlLDQwMDApO30KJCgiI21jQWNjZXB0QnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vYWNjZXB0YCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsYWNjZXB0ZWQ6dHJ1ZX19KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt0b2FzdCgi0KLRiyDQv9C+0LTRgtCy0LXRgNC00LjQuyDQvNCw0YLRhy4iKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L/RgNC40L3Rj9GC0Ywg0LzQsNGC0Yc6ICIrZS5tZXNzYWdlKTt9fSk7CiQoIiNtY0RlY2xpbmVCdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2lmKCFjb25maXJtKCLQntGC0LrQu9C+0L3QuNGC0Ywg0L3QsNC50LTQtdC90L3Ri9C5INC80LDRgtGHPyDQntGB0YLQsNC70YzQvdGL0LUg0LjQs9GA0L7QutC4INCy0LXRgNC90YPRgtGB0Y8g0LIg0L/QvtC40YHQui4iKSlyZXR1cm47dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L2FjY2VwdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGFjY2VwdGVkOmZhbHNlfX0pO3N0b3BNYXRjaFBvbGxpbmcoKTskKCIjbWF0Y2hDZW50ZXIiKS5jbGFzc0xpc3QucmVtb3ZlKCJ2aXNpYmxlIik7dG9hc3QoItCc0LDRgtGHINC+0YLQutC70L7QvdGR0L0uIik7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC+0YLQutC70L7QvdC40YLRjDogIitlLm1lc3NhZ2UpO319KTsKYXN5bmMgZnVuY3Rpb24gc2V0Q2FwdGFpbih0ZWFtKXtjb25zdCBpZD0kKCIjbWNDYXB0YWluIit0ZWFtKS52YWx1ZTtpZighaWQpcmV0dXJuO3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9jYXB0YWluYCx7bWV0aG9kOiJQT1NUIixib2R5OntjYXB0YWluX2lkOmlkLHRlYW19fSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7dG9hc3QoItCa0LDQv9C40YLQsNC9INC60L7QvNCw0L3QtNGLICIrdGVhbSsiINC90LDQt9C90LDRh9C10L0uIik7fWNhdGNoKGUpe3RvYXN0KCLQmtCw0L/QuNGC0LDQvTogIitlLm1lc3NhZ2UpO319CiQoIiNtY1NldENhcHRhaW5BIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5zZXRDYXB0YWluKCJBIikpOyQoIiNtY1NldENhcHRhaW5CIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5zZXRDYXB0YWluKCJCIikpOwokKCIjbWNTdGFydEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L3N0YXJ0P3RlbGVncmFtX2lkPSR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCx7bWV0aG9kOiJQT1NUIn0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQnNCw0YLRhyDQvtGC0LzQtdGH0LXQvSDQutCw0Log0L3QsNGH0LDRgtGL0LkuIik7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC30LDQv9GD0YHRgtC40YLRjCDQvNCw0YLRhzogIitlLm1lc3NhZ2UpO319KTsKJCgiI21jU3VibWl0UmVzdWx0IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pntjb25zdCBzY29yZV9hPU51bWJlcigkKCIjbWNTY29yZUEiKS52YWx1ZSksc2NvcmVfYj1OdW1iZXIoJCgiI21jU2NvcmVCIikudmFsdWUpO2lmKCFOdW1iZXIuaXNJbnRlZ2VyKHNjb3JlX2EpfHwhTnVtYmVyLmlzSW50ZWdlcihzY29yZV9iKXx8c2NvcmVfYTwwfHxzY29yZV9iPDApcmV0dXJuIHRvYXN0KCLQo9C60LDQttC4INC60L7RgNGA0LXQutGC0L3Ri9C5INGB0YfRkdGCINC+0LHQtdC40YUg0LrQvtC80LDQvdC0LiIpO3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9yZXN1bHRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxzY29yZV9hLHNjb3JlX2Isc3RhdHM6e1thcGlVc2VySWRdOntraWxsczpOdW1iZXIoJCgiI21jS2lsbHMiKS52YWx1ZXx8MCksZGVhdGhzOk51bWJlcigkKCIjbWNEZWF0aHMiKS52YWx1ZXx8MCl9fSxub3RlOiQoIiNtY1Jlc3VsdE5vdGUiKS52YWx1ZS50cmltKCl9fSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGC0L/RgNCw0LLQu9C10L0g0LzQvtC00LXRgNCw0YLQvtGA0YMuIEVMTyDQv9C+0LrQsCDQvdC1INC40LfQvNC10L3RkdC9LiIpO31jYXRjaChlKXt0b2FzdCgi0KDQtdC30YPQu9GM0YLQsNGCOiAiK2UubWVzc2FnZSk7fX0pOwokKCIjbWNTdWJtaXRBcHBlYWwiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9hcHBlYWxzYCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQscmVhc29uOiQoIiNtY0FwcGVhbFJlYXNvbiIpLnZhbHVlLGRldGFpbHM6JCgiI21jQXBwZWFsRGV0YWlscyIpLnZhbHVlLnRyaW0oKX19KTt0b2FzdCgi0JDQv9C10LvQu9GP0YbQuNGPINC+0YLQv9GA0LDQstC70LXQvdCwINC80L7QtNC10YDQsNGC0L7RgNGDLiIpOyQoIiNtY1N1Ym1pdEFwcGVhbCIpLmRpc2FibGVkPXRydWU7fWNhdGNoKGUpe3RvYXN0KCLQkNC/0LXQu9C70Y/RhtC40Y86ICIrZS5tZXNzYWdlKTt9fSk7Ci8vIEtlZXAgcmVhbCBwcm9maWxlIHN0YXRlIGFuZCBkaXNjb3ZlciBhIG1hdGNoIGlmIGl0IHdhcyBmb3JtZWQgYnkgYW5vdGhlciBxdWV1ZSBwYXJ0aWNpcGFudC4KY29uc3QgX29sZEFwcGx5UGxheWVyPWFwcGx5UGxheWVyO2FwcGx5UGxheWVyPWZ1bmN0aW9uKHApe19vbGRBcHBseVBsYXllcihwKTtjb25zdCBwcz0kKCIjcHJvZmlsZVN0YXR1cyIpO2lmKHBzKXBzLnRleHRDb250ZW50PSLil48g0J/QoNCe0KTQmNCb0Kwg0KEg0KHQldCg0JLQldCg0JAiO3NldEFwaVN0YXR1cygiQVBJINC+0L3Qu9Cw0LnQvSDCtyDQv9GA0L7RhNC40LvRjCDQt9Cw0LPRgNGD0LbQtdC9Iix0cnVlKTt9Owpjb25zdCBfb2xkSW5pdEFwaT1pbml0QXBpO2luaXRBcGk9YXN5bmMgZnVuY3Rpb24oKXthd2FpdCBfb2xkSW5pdEFwaSgpO2lmKGFwaVVzZXJJZCl7dHJ5e2NvbnN0IGE9YXdhaXQgYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfS9hY3RpdmUtbWF0Y2hgKTtpZihhLmFjdGl2ZSYmYS5tYXRjaCl7YWN0aXZlTWF0Y2hJZD1hLm1hdGNoLm1hdGNoX2lkO2F3YWl0IHNob3dNYXRjaChhY3RpdmVNYXRjaElkKTt9fWNhdGNoKGUpe2NvbnNvbGUud2FybigiQWN0aXZlIG1hdGNoIixlLm1lc3NhZ2UpO319fTsKCgovLyBDb3NtZXRpYyBzaG9wLCBleHRlbmRlZCBwcm9maWxlIHN0YXRpc3RpY3MgYW5kIHF1YWxpZmljYXRpb24gdHJvcGhpZXMuCmxldCBjb3NtZXRpY0NhdGFsb2c9W10sY29zbWV0aWNGaWx0ZXI9ImFsbCIsbXlDb3NtZXRpY0l0ZW1zPVtdOwpmdW5jdGlvbiBjb3NtZXRpY0ljb24odHlwZSl7cmV0dXJuIHR5cGU9PT0iZnJhbWUiPyLilqMiOnR5cGU9PT0idGl0bGUiPyLinKYiOiLilqQiO30KZnVuY3Rpb24gY29zbWV0aWNUeXBlTmFtZSh0eXBlKXtyZXR1cm4gKHtmcmFtZToi0KDQsNC80LrQsCDQv9GA0L7RhNC40LvRjyIsdGl0bGU6ItCi0LjRgtGD0LsiLHByb2ZpbGVfY2FyZDoi0JrQsNGA0YLQvtGH0LrQsCDQv9GA0L7RhNC40LvRjyJ9KVt0eXBlXXx8ItCa0L7RgdC80LXRgtC40LrQsCI7fQpmdW5jdGlvbiByZW5kZXJDb3NtZXRpY1Nob3AoKXtjb25zdCBsaXN0PWNvc21ldGljQ2F0YWxvZy5maWx0ZXIoeD0+Y29zbWV0aWNGaWx0ZXI9PT0iYWxsInx8eC5pdGVtX3R5cGU9PT1jb3NtZXRpY0ZpbHRlcik7Y29uc3Qgcm9vdD0kKCIjY29zbWV0aWNTaG9wR3JpZCIpO2lmKCFyb290KXJldHVybjtyb290LmlubmVySFRNTD1saXN0Lmxlbmd0aD9saXN0Lm1hcCh4PT5gPGFydGljbGUgY2xhc3M9ImNvc21ldGljLWl0ZW0iPjxkaXYgY2xhc3M9ImNvc21ldGljLWFydCI+JHtjb3NtZXRpY0ljb24oeC5pdGVtX3R5cGUpfTwvZGl2PjxkaXYgY2xhc3M9InJhcml0eSI+JHtlc2MoeC5yYXJpdHl8fCJjb21tb24iKX0gwrcgJHtlc2MoY29zbWV0aWNUeXBlTmFtZSh4Lml0ZW1fdHlwZSkpfTwvZGl2PjxoND4ke2VzYyh4Lm5hbWUpfTwvaDQ+PHA+JHtlc2MoeC5kZXNjcmlwdGlvbnx8ItCa0L7RgdC80LXRgtC40YfQtdGB0LrQuNC5INC/0YDQtdC00LzQtdGCIFNUQU5EUE9JTlQiKX08L3A+PGRpdiBjbGFzcz0iY29zbWV0aWMtbWV0YSI+PGI+JHtOdW1iZXIoeC5wcmljZV9wb2ludHN8fDApLnRvTG9jYWxlU3RyaW5nKCJydS1SVSIpfSBTUDwvYj48c3Bhbj4ke2VzYyhjb3NtZXRpY1R5cGVOYW1lKHguaXRlbV90eXBlKSl9PC9zcGFuPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IGJ1eUNvc21ldGljIiBkYXRhLWl0ZW09IiR7ZXNjKHguaXRlbV9pZCl9Ij7QmtGD0L/QuNGC0Yw8L2J1dHRvbj48L2FydGljbGU+YCkuam9pbigiIik6JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7QkiDRjdGC0L7QuSDQutCw0YLQtdCz0L7RgNC40Lgg0L/QvtC60LAg0L3QtdGCINC/0YDQtdC00LzQtdGC0L7Qsi48L2Rpdj4nOwogJCQoIi5idXlDb3NtZXRpYyIscm9vdCkuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57aWYoIWFwaVVzZXJJZCl7dG9hc3QoItCe0YLQutGA0L7QuSDQv9GA0LjQu9C+0LbQtdC90LjQtSDRh9C10YDQtdC3IFRlbGVncmFtLCDRh9GC0L7QsdGLINC/0L7QutGD0L/QsNGC0Ywg0L/RgNC10LTQvNC10YLRiy4iKTtyZXR1cm47fWIuZGlzYWJsZWQ9dHJ1ZTt0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jb3NtZXRpY3MvJHtlbmNvZGVVUklDb21wb25lbnQoYi5kYXRhc2V0Lml0ZW0pfS9wdXJjaGFzZWAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkfX0pO3RvYXN0KGQubWVzc2FnZXx8ItCf0L7QutGD0L/QutCwINCy0YvQv9C+0LvQvdC10L3QsCIpO2F3YWl0IGxvYWRDb3NtZXRpY0RhdGEoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0LrRg9C/0LjRgtGMOiAiK2UubWVzc2FnZSk7fWZpbmFsbHl7Yi5kaXNhYmxlZD1mYWxzZTt9fSkpO30KZnVuY3Rpb24gcmVuZGVyTXlDb3NtZXRpY3MoKXtjb25zdCByb290PSQoIiNteUNvc21ldGljc0dyaWQiKTtpZighcm9vdClyZXR1cm47cm9vdC5pbm5lckhUTUw9bXlDb3NtZXRpY0l0ZW1zLmxlbmd0aD9teUNvc21ldGljSXRlbXMubWFwKHg9PmA8YXJ0aWNsZSBjbGFzcz0iY29zbWV0aWMtaXRlbSI+PGRpdiBjbGFzcz0iY29zbWV0aWMtYXJ0Ij4ke2Nvc21ldGljSWNvbih4Lml0ZW1fdHlwZSl9PC9kaXY+PGRpdiBjbGFzcz0icmFyaXR5Ij4ke2VzYyh4LnJhcml0eXx8ImNvbW1vbiIpfTwvZGl2PjxoND4ke2VzYyh4Lm5hbWUpfTwvaDQ+PHA+JHtlc2MoeC5kZXNjcmlwdGlvbnx8Y29zbWV0aWNUeXBlTmFtZSh4Lml0ZW1fdHlwZSkpfTwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGVxdWlwQ29zbWV0aWMiIGRhdGEtaXRlbT0iJHtlc2MoeC5pdGVtX2lkKX0iPtCd0LDQtNC10YLRjDwvYnV0dG9uPjwvYXJ0aWNsZT5gKS5qb2luKCIiKTonPGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCa0L7Qu9C70LXQutGG0LjRjyDQv9C+0LrQsCDQv9GD0YHRgtCwLjwvZGl2Pic7JCQoIi5lcXVpcENvc21ldGljIixyb290KS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9PntpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjCDRh9C10YDQtdC3IFRlbGVncmFtLiIpO3RyeXthd2FpdCBhcGkoIi9jb3NtZXRpY3MvZXF1aXAiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxpdGVtX2lkOmIuZGF0YXNldC5pdGVtfX0pO3RvYXN0KCLQn9GA0LXQtNC80LXRgiDRjdC60LjQv9C40YDQvtCy0LDQvSIpO2F3YWl0IGxvYWRDb3NtZXRpY0RhdGEoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0Y3QutC40L/QuNGA0L7QstCw0YLRjDogIitlLm1lc3NhZ2UpO319KSk7fQphc3luYyBmdW5jdGlvbiBsb2FkQ29zbWV0aWNEYXRhKCl7aWYoIWFwaVVzZXJJZCl7JCgiI2Nvc21ldGljU2hvcEdyaWQiKS5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7QmtCw0YLQsNC70L7QsyDQvNC+0LbQvdC+INC/0YDQvtGB0LzQvtGC0YDQtdGC0YwsINC90L4g0L/QvtC60YPQv9C60LAg0YLRgNC10LHRg9C10YIg0L/QvtC00LrQu9GO0YfQtdC90LjRjyDQv9GA0L7RhNC40LvRjy48L2Rpdj4nO3JldHVybjt9dHJ5e2NvbnN0IFtjYXRhbG9nLG93bmVkXT1hd2FpdCBQcm9taXNlLmFsbChbYXBpKCIvY29zbWV0aWNzL3Nob3AiKSxhcGkoYC9jb3NtZXRpY3MvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKV0pO2Nvc21ldGljQ2F0YWxvZz1jYXRhbG9nLml0ZW1zfHxjYXRhbG9nLmNvc21ldGljc3x8W107bXlDb3NtZXRpY0l0ZW1zPW93bmVkLml0ZW1zfHxvd25lZC5jb3NtZXRpY3N8fFtdOyQoIiNjb3NtZXRpY1BvaW50cyIpLnRleHRDb250ZW50PU51bWJlcihvd25lZC5zZXR0aW5ncz8ucG9pbnRzPz9vd25lZC5wb2ludHM/PzApLnRvTG9jYWxlU3RyaW5nKCJydS1SVSIpO3JlbmRlckNvc21ldGljU2hvcCgpO3JlbmRlck15Q29zbWV0aWNzKCk7fWNhdGNoKGUpeyQoIiNjb3NtZXRpY1Nob3BHcmlkIikuaW5uZXJIVE1MPWA8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0LrQsNGC0LDQu9C+0LM6ICR7ZXNjKGUubWVzc2FnZSl9PC9kaXY+YDt9fQphc3luYyBmdW5jdGlvbiBsb2FkRXh0cmFQcm9maWxlRGF0YSgpe2lmKCFhcGlVc2VySWQpcmV0dXJuO3RyeXtjb25zdCBbc3RhdHMsdHJvcGhpZXNdPWF3YWl0IFByb21pc2UuYWxsKFthcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3N0YXRpc3RpY3NgKSxhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3Ryb3BoaWVzYCldKTtjb25zdCBkPXN0YXRzLnN0YXRpc3RpY3N8fHN0YXRzO2NvbnN0IHZhbD0oaWQsdik9Pntjb25zdCBlbD0kKGlkKTtpZihlbCllbC50ZXh0Q29udGVudD12Pz8wO307dmFsKCIjZXh0cmFXaW5yYXRlIiwoZC53aW5fcmF0ZT8/ZC53aW5yYXRlKSE9bnVsbD8oTnVtYmVyKGQud2luX3JhdGU/P2Qud2lucmF0ZSkudG9GaXhlZCgxKSsiJSIpOihzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cz9NYXRoLnJvdW5kKHN0YXRlLndpbnMvKHN0YXRlLndpbnMrc3RhdGUubG9zc2VzK3N0YXRlLmRyYXdzKSoxMDApKyIlIjoiMCUiKSk7dmFsKCIjZXh0cmFMb3NzZXMiLGQubG9zc2VzPz9zdGF0ZS5sb3NzZXMpO3ZhbCgiI2V4dHJhRHJhd3MiLGQuZHJhd3M/P3N0YXRlLmRyYXdzKTt2YWwoIiNleHRyYUtpbGxzIixkLmtpbGxzPz9zdGF0ZS5raWxscyk7dmFsKCIjZXh0cmFEZWF0aHMiLGQuZGVhdGhzPz9zdGF0ZS5kZWF0aHMpO3ZhbCgiI2V4dHJhS0QiLGQua2Q/PyhzdGF0ZS5kZWF0aHM/KHN0YXRlLmtpbGxzL3N0YXRlLmRlYXRocykudG9GaXhlZCgyKToi4oCUIikpO3ZhbCgiI2V4dHJhTWF0Y2hlcyIsZC5tYXRjaGVzPz9kLnRvdGFsX21hdGNoZXM/PyhzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cykpO3ZhbCgiI2V4dHJhRWxvIixkLmVsbz8/c3RhdGUuZWxvKTskKCIjZXh0cmFTdGF0c05vdGUiKS50ZXh0Q29udGVudD0i0KHRgtCw0YLQuNGB0YLQuNC60LAg0L/QvtC70YPRh9C10L3QsCDQuNC3IEFQSS4iO2NvbnN0IGl0ZW1zPXRyb3BoaWVzLnRyb3BoaWVzfHxbXTskKCIjdHJvcGh5Q291bnQiKS50ZXh0Q29udGVudD1pdGVtcy5sZW5ndGgrIiDQndCQ0JPQoNCQ0JQiOyQoIiNwcm9maWxlVHJvcGhpZXMiKS5pbm5lckhUTUw9aXRlbXMubGVuZ3RoP2l0ZW1zLm1hcCh0PT5gPGRpdiBjbGFzcz0idHJvcGh5Ij48ZGl2IGNsYXNzPSJjdXAiPiR7TnVtYmVyKHQucGxhY2UpPT09MT8i8J+PhiI6TnVtYmVyKHQucGxhY2UpPT09Mj8i8J+liCI6TnVtYmVyKHQucGxhY2UpPT09Mz8i8J+liSI6IvCfj4UifTwvZGl2PjxiPiR7ZXNjKHQudHJvcGh5X25hbWV8fCgi0KLQvtC/LSIrdC5wbGFjZSkpfTwvYj48c21hbGw+JHtlc2ModC5xdWFsaWZpY2F0aW9uX2tleXx8ItCa0LLQsNC70LjRhNC40LrQsNGG0LjRjyIpfTwvc21hbGw+PHNtYWxsPiR7dC5wbGFjZX0g0LzQtdGB0YLQvjwvc21hbGw+PC9kaXY+YCkuam9pbigiIik6JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7Qn9C+0LrQsCDQvdC10YIg0LrRg9Cx0LrQvtCyLiDQndCw0LPRgNCw0LTRiyDQv9C+0Y/QstGP0YLRgdGPINC/0L7RgdC70LUg0LfQsNCy0LXRgNGI0LXQvdC40Y8g0LrQstCw0LvQuNGE0LjQutCw0YbQuNC4LjwvZGl2Pic7fWNhdGNoKGUpeyQoIiNleHRyYVN0YXRzTm90ZSIpLnRleHRDb250ZW50PSLQndC1INGD0LTQsNC70L7RgdGMINC30LDQs9GA0YPQt9C40YLRjCDRgdGC0LDRgtC40YHRgtC40LrRgzogIitlLm1lc3NhZ2U7fX0KJCQoIltkYXRhLWNvc21ldGljLWZpbHRlcl0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57Y29zbWV0aWNGaWx0ZXI9Yi5kYXRhc2V0LmNvc21ldGljRmlsdGVyOyQkKCJbZGF0YS1jb3NtZXRpYy1maWx0ZXJdIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIseD09PWIpKTtyZW5kZXJDb3NtZXRpY1Nob3AoKTt9KSk7CiQoIiNyZWZyZXNoQ29zbWV0aWNzIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixsb2FkQ29zbWV0aWNEYXRhKTskKCIjcmVmcmVzaEV4dHJhU3RhdHMiKT8uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGxvYWRFeHRyYVByb2ZpbGVEYXRhKTsKY29uc3QgX29sZE5hdkZvckNvc21ldGljcz1uYXY7bmF2PWZ1bmN0aW9uKHBhZ2Upe19vbGROYXZGb3JDb3NtZXRpY3MocGFnZSk7aWYocGFnZT09PSJjb3NtZXRpY3MiKWxvYWRDb3NtZXRpY0RhdGEoKTtpZihwYWdlPT09InByb2ZpbGUiKWxvYWRFeHRyYVByb2ZpbGVEYXRhKCk7fTsKY29uc3QgX29sZEFwcGx5UGxheWVyRm9yRXh0cmFzPWFwcGx5UGxheWVyO2FwcGx5UGxheWVyPWZ1bmN0aW9uKHApe19vbGRBcHBseVBsYXllckZvckV4dHJhcyhwKTtpZihhcGlVc2VySWQpe2xvYWRFeHRyYVByb2ZpbGVEYXRhKCk7aWYoZG9jdW1lbnQucXVlcnlTZWxlY3RvcigiI3BhZ2UtY29zbWV0aWNzLmFjdGl2ZSIpKWxvYWRDb3NtZXRpY0RhdGEoKTt9fTsKCgovLyBNb2RlcmF0b3IgcGFuZWw6IHZpc2liaWxpdHkgaXMgdGllZCB0byBUZWxlZ3JhbSB1c2VybmFtZTsgQVBJIGFjdGlvbnMgc3RpbGwgcmVxdWlyZSBzZXJ2ZXIgc2VjcmV0Lgpjb25zdCBNT0RFUkFUT1JfVVNFUk5BTUU9Inl1a3plbl9icm8iOwpmdW5jdGlvbiBtb2RlcmF0b3JBbGxvd2VkKCl7Y29uc3QgdT1nZXRUZWxlZ3JhbVVzZXIoKTtyZXR1cm4gU3RyaW5nKHU/LnVzZXJuYW1lfHwiIikucmVwbGFjZSgvXkAvLCIiKS50b0xvd2VyQ2FzZSgpPT09TU9ERVJBVE9SX1VTRVJOQU1FO30KZnVuY3Rpb24gbW9kZXJhdG9ySGVhZGVycygpe2NvbnN0IHRva2VuPSQoIiNtb2RlcmF0b3JUb2tlbiIpPy52YWx1ZS50cmltKCk7aWYoIXRva2VuKXRocm93IG5ldyBFcnJvcigi0JLQstC10LTQuCBSRVNVTFRTX0FETUlOX1RPS0VOINC40Lcg0L3QsNGB0YLRgNC+0LXQuiDRgdC10YDQstC10YDQsC4iKTtyZXR1cm4geyJYLUFkbWluLVRva2VuIjp0b2tlbn07fQphc3luYyBmdW5jdGlvbiBtb2RlcmF0b3JBcGkocGF0aCx7bWV0aG9kPSJHRVQiLGJvZHk9bnVsbH09e30pe2NvbnN0IGhlYWRlcnM9ey4uLm1vZGVyYXRvckhlYWRlcnMoKSxBY2NlcHQ6ImFwcGxpY2F0aW9uL2pzb24ifTtjb25zdCBvcHRzPXttZXRob2QsaGVhZGVyc307aWYoYm9keSE9PW51bGwpe2hlYWRlcnNbIkNvbnRlbnQtVHlwZSJdPSJhcHBsaWNhdGlvbi9qc29uIjtvcHRzLmJvZHk9SlNPTi5zdHJpbmdpZnkoYm9keSk7fWxldCByO3RyeXtyPWF3YWl0IGZldGNoKEFQSV9CQVNFK3BhdGgsb3B0cyl9Y2F0Y2goZSl7dGhyb3cgbmV3IEVycm9yKCLQodC10YDQstC10YAg0L3QtdC00L7RgdGC0YPQv9C10L0g0LjQu9C4INC+0YjQuNCx0LrQsCDRgdC10YLQuC4iKTt9bGV0IGQ9e307dHJ5e2Q9YXdhaXQgci5qc29uKCl9Y2F0Y2h7fWlmKCFyLm9rKXRocm93IG5ldyBFcnJvcihkLmRldGFpbHx8KCJIVFRQICIrci5zdGF0dXMpKTtyZXR1cm4gZDt9CmZ1bmN0aW9uIHNob3dNb2RlcmF0b3JQYW5lbCgpe2NvbnN0IGNhcmQ9JCgiI21vZGVyYXRvckNhcmQiKTtpZihjYXJkKWNhcmQuc3R5bGUuZGlzcGxheT1tb2RlcmF0b3JBbGxvd2VkKCk/ImJsb2NrIjoibm9uZSI7fQphc3luYyBmdW5jdGlvbiBsb2FkTW9kZXJhdGlvblF1ZXVlKCl7Y29uc3Qgb3V0PSQoIiNtb2RlcmF0aW9uUXVldWUiKTtvdXQuaW5uZXJIVE1MPSc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCX0LDQs9GA0YPQt9C60LAg0L7Rh9C10YDQtdC00LjigKY8L3A+Jzt0cnl7Y29uc3QgZD1hd2FpdCBtb2RlcmF0b3JBcGkoIi9hZG1pbi9tb2RlcmF0aW9uL3F1ZXVlIik7Y29uc3QgbWF0Y2hlcz1kLm1hdGNoZXN8fFtdO2lmKCFtYXRjaGVzLmxlbmd0aCl7b3V0LmlubmVySFRNTD0nPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QndC10YIg0LzQsNGC0YfQtdC5LCDQvtC20LjQtNCw0Y7RidC40YUg0L/RgNC+0LLQtdGA0LrQuC48L3A+JztyZXR1cm47fW91dC5pbm5lckhUTUw9bWF0Y2hlcy5tYXAobT0+YDxkaXYgY2xhc3M9Im1jLXBhbmVsIiBkYXRhLW1vZC1tYXRjaD0iJHtlc2MobS5tYXRjaF9pZCl9Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGRpdj48Yj4ke2VzYyhtLm1vZGV8fCLQnNCw0YLRhyIpfSDCtyAke2VzYyhtLm1hdGNoX2lkKX08L2I+PGRpdiBjbGFzcz0ibWMtbXV0ZWQiPiR7ZXNjKG0uc3RhdHVzKX0gwrcgJHtlc2MobS5tYXBfbmFtZXx8ItCa0LDRgNGC0LAg0L3QtSDRg9C60LDQt9Cw0L3QsCIpfTwvZGl2PjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPiR7KG0ucGxheWVyc3x8W10pLmxlbmd0aH0g0LjQs9GA0L7QutC+0LI8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibWMtbXV0ZWQiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHgiPklEINC40LPRgNC+0LrQvtCyOiAkeyhtLnBsYXllcnN8fFtdKS5tYXAoZXNjKS5qb2luKCIsICIpfTwvZGl2PjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCh0KfQgdCiINCa0J7QnNCQ0J3QlNCrIEE8L2xhYmVsPjxpbnB1dCB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAiIGRhdGEtc2NvcmUtYSBwbGFjZWhvbGRlcj0iMCI+PC9kaXY+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7QodCn0IHQoiDQmtCe0JzQkNCd0JTQqyBCPC9sYWJlbD48aW5wdXQgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiBkYXRhLXNjb3JlLWIgcGxhY2Vob2xkZXI9IjAiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0KHQotCQ0KLQmNCh0KLQmNCa0JAg0J/QniDQmNCT0KDQntCa0JDQnCAoSlNPTiwg0L3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4pPC9sYWJlbD48dGV4dGFyZWEgZGF0YS1wbGF5ZXItc3RhdHMgcm93cz0iMyIgcGxhY2Vob2xkZXI9J3siVEVMRUdSQU1fSUQiOnsia2lsbHMiOjEwLCJkZWF0aHMiOjR9fSc+PC90ZXh0YXJlYT48c21hbGwgY2xhc3M9Im1jLW11dGVkIj7QlNC+0LHQsNCy0Ywga2lsbHMvZGVhdGhzINC00LvRjyDQutCw0LbQtNC+0LPQviDQuNCz0YDQvtC60LAg0L/QviBUZWxlZ3JhbSBJRC4g0JXRgdC70Lgg0L7RgdGC0LDQstC40YLRjCDQv9GD0YHRgtGL0LwsINGB0YLQsNGC0LjRgdGC0LjQutCwINC90LUg0LjQt9C80LXQvdC40YLRgdGPLjwvc21hbGw+PC9kaXY+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7QmtCe0JzQnNCV0J3QotCQ0KDQmNCZPC9sYWJlbD48aW5wdXQgZGF0YS1yZXZpZXctbm90ZSBtYXhsZW5ndGg9IjEwMDAiIHBsYWNlaG9sZGVyPSLQn9GA0LjRh9C40L3QsCDRgNC10YjQtdC90LjRjyAo0L3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4pIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1hY3Rpb25zIj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgZGF0YS1tb2QtYXBwcm92ZT7inJMg0J7QtNC+0LHRgNC40YLRjCArINC90LDRh9C40YHQu9C40YLRjCBFTE88L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBkYXRhLW1vZC1yZWplY3Q+4pyVINCe0YLQutCw0LfQsNGC0Ywg0LHQtdC3INC40LfQvNC10L3QtdC90LjQuTwvYnV0dG9uPjwvZGl2PjwvZGl2PmApLmpvaW4oIiIpO291dC5xdWVyeVNlbGVjdG9yQWxsKCJbZGF0YS1tb2QtYXBwcm92ZV0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5yZXZpZXdNb2RlcmF0aW9uKGIuY2xvc2VzdCgiW2RhdGEtbW9kLW1hdGNoXSIpLCJhcHByb3ZlIikpKTtvdXQucXVlcnlTZWxlY3RvckFsbCgiW2RhdGEtbW9kLXJlamVjdF0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT5yZXZpZXdNb2RlcmF0aW9uKGIuY2xvc2VzdCgiW2RhdGEtbW9kLW1hdGNoXSIpLCJyZWplY3QiKSkpO31jYXRjaChlKXtvdXQuaW5uZXJIVE1MPWA8cCBzdHlsZT0iY29sb3I6I2ZmOGU4ZSI+JHtlc2MoZS5tZXNzYWdlKX08L3A+YDt9fQphc3luYyBmdW5jdGlvbiByZXZpZXdNb2RlcmF0aW9uKGNhcmQsZGVjaXNpb24pe2NvbnN0IG1hdGNoSWQ9Y2FyZC5kYXRhc2V0Lm1vZE1hdGNoO2xldCBzdGF0cz17fTt0cnl7Y29uc3QgcmF3PWNhcmQucXVlcnlTZWxlY3RvcigiW2RhdGEtcGxheWVyLXN0YXRzXSIpLnZhbHVlLnRyaW0oKTtpZihyYXcpc3RhdHM9SlNPTi5wYXJzZShyYXcpO2lmKCFzdGF0c3x8dHlwZW9mIHN0YXRzIT09Im9iamVjdCJ8fEFycmF5LmlzQXJyYXkoc3RhdHMpKXRocm93IG5ldyBFcnJvcigi0KHRgtCw0YLQuNGB0YLQuNC60LAg0LTQvtC70LbQvdCwINCx0YvRgtGMIEpTT04t0L7QsdGK0LXQutGC0L7QvC4iKTt9Y2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDRgdGC0LDRgtC40YHRgtC40LrQuDogIitlLm1lc3NhZ2UpO3JldHVybjt9Y29uc3QgYm9keT17ZGVjaXNpb24sbm90ZTpjYXJkLnF1ZXJ5U2VsZWN0b3IoIltkYXRhLXJldmlldy1ub3RlXSIpLnZhbHVlLnRyaW0oKX07aWYoZGVjaXNpb249PT0iYXBwcm92ZSIpe2NvbnN0IGE9Y2FyZC5xdWVyeVNlbGVjdG9yKCJbZGF0YS1zY29yZS1hXSIpLnZhbHVlLGI9Y2FyZC5xdWVyeVNlbGVjdG9yKCJbZGF0YS1zY29yZS1iXSIpLnZhbHVlO2lmKGE9PT0iInx8Yj09PSIiKXJldHVybiB0b2FzdCgi0JTQu9GPINC+0LTQvtCx0YDQtdC90LjRjyDRg9C60LDQttC4INC+0LHQsCDRgdGH0ZHRgtCwLiIpO2JvZHkuc2NvcmVfYT1OdW1iZXIoYSk7Ym9keS5zY29yZV9iPU51bWJlcihiKTtib2R5LnN0YXRzPXN0YXRzO31pZighY29uZmlybShkZWNpc2lvbj09PSJhcHByb3ZlIj8i0J7QtNC+0LHRgNC40YLRjCDQvNCw0YLRhz8gRUxPINC4INGD0LrQsNC30LDQvdC90LDRjyDRgdGC0LDRgtC40YHRgtC40LrQsCDQsdGD0LTRg9GCINC30LDQv9C40YHQsNC90Ysg0LIg0L/RgNC+0YTQuNC70Lgg0LjQs9GA0L7QutC+0LIuIjoi0J7RgtC60LDQt9Cw0YLRjCDQsiDQvNCw0YLRh9C1PyBFTE8g0Lgg0YHRgtCw0YLQuNGB0YLQuNC60LAg0L7RgdGC0LDQvdGD0YLRgdGPINCx0LXQtyDQuNC30LzQtdC90LXQvdC40LkuIikpcmV0dXJuO3RyeXtjb25zdCBkPWF3YWl0IG1vZGVyYXRvckFwaShgL2FkbWluL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQobWF0Y2hJZCl9L3Jldmlld2Ase21ldGhvZDoiUE9TVCIsYm9keX0pO3RvYXN0KGRlY2lzaW9uPT09ImFwcHJvdmUiPyLQnNCw0YLRhyDQvtC00L7QsdGA0LXQvSwgRUxPINC4INGB0YLQsNGC0LjRgdGC0LjQutCwINC+0LHQvdC+0LLQu9C10L3Riy4iOiLQnNCw0YLRhyDQvtGC0LrQu9C+0L3RkdC9INCx0LXQtyDQvdCw0YfQuNGB0LvQtdC90LjRjyBFTE8uIik7YXdhaXQgbG9hZE1vZGVyYXRpb25RdWV1ZSgpO31jYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINC80L7QtNC10YDQsNGG0LjQuDogIitlLm1lc3NhZ2UpO319CiQoIiNsb2FkTW9kZXJhdGlvbkJ0biIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsbG9hZE1vZGVyYXRpb25RdWV1ZSk7JCgiI2NsZWFyTW9kZXJhdG9yVG9rZW4iKT8uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57JCgiI21vZGVyYXRvclRva2VuIikudmFsdWU9IiI7dG9hc3QoItCf0L7Qu9C1INC60LvRjtGH0LAg0L7Rh9C40YnQtdC90L4uIik7fSk7c2hvd01vZGVyYXRvclBhbmVsKCk7CnJlbmRlcigpOwppbml0QXBpKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4="""

@app.get("/", response_class=HTMLResponse)
def root():
    import base64
    return HTMLResponse(
        content=base64.b64decode(_FRONTEND_HTML_B64).decode("utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


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

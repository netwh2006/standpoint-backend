
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
            CREATE TABLE IF NOT EXISTS player_pass_premium (
                season_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'verified_payment',
                PRIMARY KEY(season_id, telegram_id),
                FOREIGN KEY(season_id) REFERENCES pass_seasons(season_id) ON DELETE CASCADE,
                FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pass_currency_claims (
                season_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                claimed_at TEXT NOT NULL,
                PRIMARY KEY(season_id, telegram_id, level),
                FOREIGN KEY(season_id) REFERENCES pass_seasons(season_id) ON DELETE CASCADE,
                FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE
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
_FRONTEND_HTML_B64 = """PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCB2aWV3cG9ydC1maXQ9Y292ZXIiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzA4MGEwZCI+Cjx0aXRsZT5TVEFORFBPSU5UIOKAlCBTdGFuZG9mZiAyIENvbXBldGl0aXZlPC90aXRsZT4KPHN0eWxlPgo6cm9vdHtjb2xvci1zY2hlbWU6ZGFyazstLWJnOiMwODBhMGQ7LS1zdXJmYWNlOiMxMTE2MWM7LS1zdXJmYWNlMjojMTcxZTI2Oy0tc3VyZmFjZTM6IzIwMmEzNTstLXN0cm9rZTojM2E0NjUzOy0tdGV4dDojZmZmZmZmOy0tbXV0ZWQ6I2MwY2FkNDstLW9yYW5nZTojZmY1YTEyOy0tb3JhbmdlMjojZmY4YTIyOy0tZ3JlZW46IzUwZGRhMDstLXJlZDojZmY1OTY0Oy0tZ29sZDojZmZjYTY4Oy0tcjoxN3B4fQoqe2JveC1zaXppbmc6Ym9yZGVyLWJveH1odG1se2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yLXNjaGVtZTpkYXJrfWJvZHl7bWFyZ2luOjA7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAscmdiYSgyNTUsODAsMTUsLjEyKSx0cmFuc3BhcmVudCAyNyUpLHZhcigtLWJnKTtjb2xvcjp2YXIoLS10ZXh0KTtmb250OjE0cHgvMS40NSBJbnRlcix1aS1zYW5zLXNlcmlmLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLEJsaW5rTWFjU3lzdGVtRm9udCwiU2Vnb2UgVUkiLHNhbnMtc2VyaWY7bWluLWhlaWdodDoxMDB2aH1idXR0b24saW5wdXQsc2VsZWN0LHRleHRhcmVhe2ZvbnQ6aW5oZXJpdH1idXR0b257Y3Vyc29yOnBvaW50ZXI7Y29sb3I6aW5oZXJpdH1idXR0b246Zm9jdXMtdmlzaWJsZSxpbnB1dDpmb2N1cy12aXNpYmxle291dGxpbmU6MnB4IHNvbGlkIHZhcigtLW9yYW5nZTIpO291dGxpbmUtb2Zmc2V0OjJweH1he2NvbG9yOmluaGVyaXQ7dGV4dC1kZWNvcmF0aW9uOm5vbmV9LnNoZWxse21pbi1oZWlnaHQ6MTAwdmg7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMzhweCBtaW5tYXgoMCwxZnIpfS5zaWRlYmFye2hlaWdodDoxMDB2aDtwb3NpdGlvbjpzdGlja3k7dG9wOjA7YmFja2dyb3VuZDojMGIwZTEyO2JvcmRlci1yaWdodDoxcHggc29saWQgIzIwMjczMDtwYWRkaW5nOjIzcHggMTRweCAxNXB4O2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjI4cHg7ei1pbmRleDoxMH0uYnJhbmR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtwYWRkaW5nOjAgOHB4fS5icmFuZG1hcmt7d2lkdGg6MzdweDtoZWlnaHQ6MzdweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY0YzBhLCNmZjliMjgpO2NvbG9yOiMxNDBkMDk7Zm9udC1zaXplOjIzcHg7Zm9udC13ZWlnaHQ6MTAwMDtjbGlwLXBhdGg6cG9seWdvbigwIDAsMTAwJSAwLDEwMCUgNzAlLDY0JSAxMDAlLDAgMTAwJSk7Ym94LXNoYWRvdzowIDAgMjJweCAjZmY2NDFjMzN9LmJyYW5kbmFtZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo5NTA7bGV0dGVyLXNwYWNpbmc6LjVweH0uYnJhbmRuYW1lIHNwYW57Y29sb3I6dmFyKC0tb3JhbmdlKX0uYnJhbmR0YWd7Zm9udC1zaXplOjhweDtjb2xvcjojYmFjNGNlO2xldHRlci1zcGFjaW5nOjEuOHB4O2ZvbnQtd2VpZ2h0OjgwMH0ubmF2Z3JvdXAgbGFiZWx7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMTJweDttYXJnaW4tYm90dG9tOjhweDtmb250LXNpemU6OXB4O2xldHRlci1zcGFjaW5nOjEuN3B4O2NvbG9yOiM5YWE3YjQ7Zm9udC13ZWlnaHQ6OTAwfS5uYXZ7ZGlzcGxheTpncmlkO2dhcDo0cHh9Lm5hdiBidXR0b257d2lkdGg6MTAwJTtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O2JhY2tncm91bmQ6bm9uZTt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTFweCAxMnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo2NTA7Zm9udC1zaXplOjEzcHh9Lm5hdiBidXR0b24gLm5pe3dpZHRoOjE5cHg7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjE3cHh9Lm5hdiBidXR0b246aG92ZXJ7YmFja2dyb3VuZDojMTkxZjI3O2NvbG9yOndoaXRlfS5uYXYgYnV0dG9uLmFjdGl2ZXtjb2xvcjojZmY5YTRkO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCMzYTFiMTAsIzIwMTUwZik7Ym9yZGVyLWNvbG9yOiM0OTI4MTk7Ym94LXNoYWRvdzppbnNldCAzcHggMCB2YXIoLS1vcmFuZ2UpfS5uYXYgLm5hdmNvdW50e21hcmdpbi1sZWZ0OmF1dG87YmFja2dyb3VuZDojNDgyNDE2O2NvbG9yOiNmZjliNWM7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzoycHggNnB4O2ZvbnQtc2l6ZToxMHB4fS5zaWRlYmFyLWZvb3R7bWFyZ2luLXRvcDphdXRvfS51c2VyY2FyZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6MTFweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjMTcxZDI0LCMxMDE0MTkpfS5hdmF0YXJ7d2lkdGg6MzlweDtoZWlnaHQ6MzlweDtmbGV4OjAgMCBhdXRvO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0MGRlZywjZmY4YzI3LCM1YjI0MTIpO2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjp3aGl0ZX0udXNlcmNhcmQgYntkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMnB4O292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzO21heC13aWR0aDoxMjBweH0udXNlcmNhcmQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5tYWlue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjE1MzBweDttaW4td2lkdGg6MDttYXJnaW46MCBhdXRvO3BhZGRpbmc6MjhweCBjbGFtcCgxN3B4LDMuMnZ3LDQ2cHgpIDU1cHh9LnRvcGJhcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxNnB4O3BhZGRpbmctYm90dG9tOjIxcHg7bWFyZ2luLWJvdHRvbToyM3B4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyNDJiMzN9LmtpY2tlcntmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoycHg7Zm9udC13ZWlnaHQ6OTAwO2NvbG9yOnZhcigtLW9yYW5nZTIpfS50b3BiYXIgaDF7Zm9udC1zaXplOmNsYW1wKDI0cHgsM3Z3LDM1cHgpO2xldHRlci1zcGFjaW5nOi0xLjFweDtsaW5lLWhlaWdodDoxLjE7bWFyZ2luOjdweCAwIDA7Zm9udC13ZWlnaHQ6OTUwfS50b3BhY3Rpb25ze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0uaWNvbmJ0biwuYnRuLXNlY29uZGFyeXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDQ7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6MTFweDtjb2xvcjojZjRmNmY4O3BhZGRpbmc6MTFweCAxNHB4fS5pY29uYnRue3dpZHRoOjQzcHg7aGVpZ2h0OjQzcHg7cGFkZGluZzowO3Bvc2l0aW9uOnJlbGF0aXZlO2ZvbnQtc2l6ZToxOHB4fS5waW5ne3Bvc2l0aW9uOmFic29sdXRlO3dpZHRoOjdweDtoZWlnaHQ6N3B4O2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtib3JkZXItcmFkaXVzOjUwJTtyaWdodDo4cHg7dG9wOjhweH0uYnRuLXByaW1hcnl7Ym9yZGVyOjA7Ym9yZGVyLXJhZGl1czoxMXB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjZmY1MDBiLCNmZjkwMjQpO2NvbG9yOiMxNjBlMDk7Zm9udC13ZWlnaHQ6OTAwO3BhZGRpbmc6MTNweCAxOHB4O2JveC1zaGFkb3c6MCA3cHggMjJweCAjZmY2NDFjMjY7dHJhbnNpdGlvbjpmaWx0ZXIgLjE1cyx0cmFuc2Zvcm0gLjE1c30uYnRuLXByaW1hcnk6aG92ZXJ7ZmlsdGVyOmJyaWdodG5lc3MoMS4wOCk7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoLTFweCl9LmJ0bi1zZWNvbmRhcnk6aG92ZXIsLmljb25idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiM4YzRhMjR9LmxheW91dHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDEuNTVmcikgbWlubWF4KDI3NXB4LDFmcik7Z2FwOjE3cHh9LnN0YWNre2Rpc3BsYXk6Z3JpZDthbGlnbi1jb250ZW50OnN0YXJ0O2dhcDoxNnB4fS5oZXJve3Bvc2l0aW9uOnJlbGF0aXZlO2lzb2xhdGlvbjppc29sYXRlO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0OjI3MHB4O2JvcmRlci1yYWRpdXM6MjBweDtwYWRkaW5nOmNsYW1wKDI0cHgsMy41dncsNDBweCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtib3JkZXI6MXB4IHNvbGlkICM2MzMzMWM7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTA1ZGVnLHJnYmEoNDcsMjMsMTMsLjk4KSxyZ2JhKDIyLDE5LDIyLC45OCkgNTglLHJnYmEoMTIsMTcsMjIsLjk4KSl9Lmhlcm86YmVmb3Jle2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7ei1pbmRleDotMTt3aWR0aDoyODBweDtoZWlnaHQ6MjgwcHg7cmlnaHQ6NnB4O3RvcDotNTNweDtib3JkZXI6MXB4IHNvbGlkICNmZjY0MWMyODt0cmFuc2Zvcm06cm90YXRlKDM4ZGVnKTtib3gtc2hhZG93OjAgMCAwIDIycHggI2ZmNjQxYzA3LDAgMCAwIDQ3cHggI2ZmNjQxYzA1fS5oZXJvOmFmdGVye2NvbnRlbnQ6IlMiO3Bvc2l0aW9uOmFic29sdXRlO3otaW5kZXg6LTE7cmlnaHQ6MjBweDt0b3A6LTU2cHg7Zm9udC1zaXplOjMwMHB4O2xpbmUtaGVpZ2h0OjE7Zm9udC13ZWlnaHQ6MTAwMDtjb2xvcjojZmY2NDFjMTg7dHJhbnNmb3JtOnNrZXcoLTEwZGVnKX0uaGVyby1jb3B5e21heC13aWR0aDo1MTVweH0uZXllYnJvd3tjb2xvcjojZmY5ODRjO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTBweDtsZXR0ZXItc3BhY2luZzoyLjRweH0uaGVybyBoMntmb250LXNpemU6Y2xhbXAoMzJweCw0LjF2dyw1MHB4KTtsZXR0ZXItc3BhY2luZzotMS44cHg7bGluZS1oZWlnaHQ6MS4wMTttYXJnaW46MTRweCAwIDE0cHg7Zm9udC13ZWlnaHQ6MTAwMH0uaGVybyBoMiBzcGFue2NvbG9yOnZhcigtLW9yYW5nZSl9Lmhlcm8gcHtjb2xvcjojYzFjN2NlO2ZvbnQtc2l6ZToxNHB4O2xpbmUtaGVpZ2h0OjEuNjU7bWF4LXdpZHRoOjQ0MHB4O21hcmdpbjowIDAgMjFweH0uaGVyby1hY3Rpb25ze2Rpc3BsYXk6ZmxleDtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0uc2VjdGlvbntkaXNwbGF5Om5vbmU7YW5pbWF0aW9uOmFwcGVhciAuMThzIGVhc2V9LnNlY3Rpb24uYWN0aXZle2Rpc3BsYXk6YmxvY2t9QGtleWZyYW1lcyBhcHBlYXJ7ZnJvbXtvcGFjaXR5Oi40O3RyYW5zZm9ybTp0cmFuc2xhdGVZKDNweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTpub25lfX0uc3RhdHN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMyxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0uc3RhdHtwYWRkaW5nOjE2cHg7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1zdHJva2UpO2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE1MWIyMiwjMTAxNDE5KTttaW4td2lkdGg6MH0uc3RhdCBsYWJlbHtmb250LXNpemU6MTBweDtjb2xvcjojZDBkN2RmO2xldHRlci1zcGFjaW5nOi43cHg7Zm9udC13ZWlnaHQ6NzUwO2Rpc3BsYXk6YmxvY2t9LnN0YXQgc3Ryb25ne2ZvbnQtc2l6ZTpjbGFtcCgyMXB4LDJ2dywyOHB4KTtkaXNwbGF5OmJsb2NrO21hcmdpbjo1cHggMCAycHg7bGV0dGVyLXNwYWNpbmc6LS43cHg7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo5NTB9LnN0YXQgc21hbGx7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tZ3JlZW4pfS5jYXJke3BhZGRpbmc6MThweDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXN0cm9rZSk7Ym9yZGVyLXJhZGl1czp2YXIoLS1yKTtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMCwjMTAxNDE5KTttaW4td2lkdGg6MH0uY2FyZGhlYWR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtnYXA6MTBweDttYXJnaW4tYm90dG9tOjE1cHh9LmNhcmRoZWFkIGgze2ZvbnQtc2l6ZToxNHB4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojg1MDtjb2xvcjojZjVmN2Y5fS50ZXh0bGlua3tib3JkZXI6MDtiYWNrZ3JvdW5kOm5vbmU7Y29sb3I6I2ZmOWE0ZDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3NTA7cGFkZGluZzo1cHh9LnRleHRsaW5rOmhvdmVye2NvbG9yOiNmZmMwOGF9Lm1hdGNocm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4O2JvcmRlcjoxcHggc29saWQgIzI5MzIzYjtib3JkZXItcmFkaXVzOjEzcHg7YmFja2dyb3VuZDojMDkwZDEyfS5tYXRjaGxvZ297d2lkdGg6NDZweDtoZWlnaHQ6NDZweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JvcmRlci1yYWRpdXM6MTJweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzQ0MjAwZSwjMjExNTBmKTtjb2xvcjojZmY4YTIyO2ZvbnQtc2l6ZToyMnB4O2ZsZXgtc2hyaW5rOjB9Lm1hdGNoY29weXtmbGV4OjE7bWluLXdpZHRoOjB9Lm1hdGNoY29weSBie2ZvbnQtc2l6ZToxMnB4O2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2Y1ZjdmOX0ubWF0Y2hjb3B5IHNtYWxse2Rpc3BsYXk6YmxvY2s7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTBweDttYXJnaW4tdG9wOjRweH0ucGlsbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0Ojg1MDtsZXR0ZXItc3BhY2luZzouM3B4O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtiYWNrZ3JvdW5kOiMxNTMzMjY7Ym9yZGVyOjFweCBzb2xpZCAjMjQ0YTM4O2NvbG9yOiM2MGU1YTU7d2hpdGUtc3BhY2U6bm93cmFwfS5wYXNzY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTVkZWcsIzJjMTkwZiwjMTkxNTFhIDU4JSwjMTExNzFkKTtib3JkZXItY29sb3I6IzYwMzUxZjtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59LnBhc3NjYXJkOmFmdGVye2NvbnRlbnQ6IuKcpiI7cG9zaXRpb246YWJzb2x1dGU7cmlnaHQ6MTNweDt0b3A6LTE1cHg7Zm9udC1zaXplOjEwMHB4O2NvbG9yOiNmZjhhMjIxMDtwb2ludGVyLWV2ZW50czpub25lfS5wYXNzLXRvcHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjEycHg7YWxpZ24taXRlbXM6Y2VudGVyfS5wYXNzLWxldmVse2ZvbnQtc2l6ZTozN3B4O2ZvbnQtd2VpZ2h0OjEwMDA7Y29sb3I6dmFyKC0tb3JhbmdlMik7bGluZS1oZWlnaHQ6MX0ucGFzc2NhcmQgaDN7bWFyZ2luOjdweCAwIDZweDtmb250LXNpemU6MTdweH0ubXV0ZWR7Y29sb3I6dmFyKC0tbXV0ZWQpfS5zbWFsbHtmb250LXNpemU6MTFweH0ucGFzc2NhcmQgcHttYXJnaW46MDtjb2xvcjojYzhkMGQ4O2ZvbnQtc2l6ZToxMXB4fS5wcm9ncmVzc3toZWlnaHQ6OHB4O2JhY2tncm91bmQ6IzMwMzYzZTtib3JkZXItcmFkaXVzOjIwcHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbjoxN3B4IDAgOHB4fS5wcm9ncmVzcyBzcGFue2hlaWdodDoxMDAlO3dpZHRoOjQwJTtkaXNwbGF5OmJsb2NrO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCNmZjRlMGIsI2ZmOWEyOCk7Ym94LXNoYWRvdzowIDAgMTRweCAjZmY2NDFjNTU7Ym9yZGVyLXJhZGl1czoyMHB4fS5wYXNzLWZvb3R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDoxMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtjb2xvcjojYzRjZGQ2O2ZvbnQtc2l6ZToxMXB4fS5sZWFkZXJsaXN0e2Rpc3BsYXk6Z3JpZH0ubGVhZGVye2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MjNweCBtaW5tYXgoMCwxZnIpIGF1dG87YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzoxMHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgIzI1MmQzNX0ubGVhZGVyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTowfS5yYW5re2ZvbnQtd2VpZ2h0Ojk1MDtjb2xvcjojYzBjYWQ0fS5sZWFkZXI6bnRoLWNoaWxkKDEpIC5yYW5re2NvbG9yOnZhcigtLWdvbGQpfS5sZWFkZXI6bnRoLWNoaWxkKDIpIC5yYW5re2NvbG9yOiNkMWQ4ZGZ9LmxlYWRlcjpudGgtY2hpbGQoMykgLnJhbmt7Y29sb3I6I2Q5OTg2OX0ucGxheWVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweDttaW4td2lkdGg6MH0ubWluaWF2YXt3aWR0aDozM3B4O2hlaWdodDozM3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Ym9yZGVyLXJhZGl1czoxMHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMzAzYjQ3LCMxYTIwMjcpO2JvcmRlcjoxcHggc29saWQgIzNhNDY1Mjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4NTA7ZmxleC1zaHJpbms6MH0ucGxheWVyIGJ7ZGlzcGxheTpibG9jaztmb250LXNpemU6MTFweDt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9LnBsYXllciBzbWFsbHtkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNjMGNhZDQ7bWFyZ2luLXRvcDoycHh9LmVsb3tmb250LXdlaWdodDo5NTA7Zm9udC1zaXplOjEycHh9LnNlY3Rpb25oZWFke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7bWFyZ2luOjVweCAwIDE5cHh9LnNlY3Rpb25oZWFkIGgye2ZvbnQtc2l6ZToyNnB4O2xldHRlci1zcGFjaW5nOi0uN3B4O21hcmdpbjowO2ZvbnQtd2VpZ2h0Ojk1MH0uc2VjdGlvbmhlYWQgcHtmb250LXNpemU6MTJweDtjb2xvcjojYzBjYWQ0O21hcmdpbjo1cHggMCAwfS5iYWRnZS1vcmFuZ2V7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGQ7Ym9yZGVyOjFweCBzb2xpZCAjNjkzNzFkO2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDhweDtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjkwMH0ubWFwZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg0LG1pbm1heCgwLDFmcikpO2dhcDoxMHB4fS5tYXB0aWxle3BhZGRpbmc6MDt0ZXh0LWFsaWduOmxlZnQ7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzE0MWIyMztib3JkZXItcmFkaXVzOjEzcHg7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfS5tYXBhcnR7aGVpZ2h0OjEwNXB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVuO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjNTQ0MDJkLCMyYTMwMzQgNTAlLCMxMTE2MWMpfS5tYXBhcnQ6YmVmb3JlLC5tYXBhcnQ6YWZ0ZXJ7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoxNDBweDtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6I2ZmZmZmZjEyO3RyYW5zZm9ybTpyb3RhdGUoLTM1ZGVnKX0ubWFwYXJ0OmFmdGVye3RyYW5zZm9ybTpyb3RhdGUoNDVkZWcpfS5tYXBhcnQgc3Bhbntmb250LXNpemU6MzNweDt6LWluZGV4OjE7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDVweCAjMDAwKX0ubWFwdGlsZSBie2Rpc3BsYXk6YmxvY2s7cGFkZGluZzoxMHB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNmMWY0ZjZ9Lm1hcHRpbGUuc2VsZWN0ZWR7Ym9yZGVyLWNvbG9yOnZhcigtLW9yYW5nZSk7Ym94LXNoYWRvdzowIDAgMCAxcHggdmFyKC0tb3JhbmdlKSwwIDhweCAyNHB4ICNmZjY0MWMxNX0ubWFwdGlsZS5zZWxlY3RlZCBie2NvbG9yOiNmZjliNGR9Lm1hcHRpbGUuc2VsZWN0ZWQ6YWZ0ZXJ7Y29udGVudDoi4pyTIjtwb3NpdGlvbjphYnNvbHV0ZTtyaWdodDo3cHg7dG9wOjdweDt3aWR0aDoyMnB4O2hlaWdodDoyMnB4O2JvcmRlci1yYWRpdXM6NTAlO2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7YmFja2dyb3VuZDp2YXIoLS1vcmFuZ2UpO2NvbG9yOiMxMzBkMDg7Zm9udC13ZWlnaHQ6MTAwMH0ubW9kZWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDo4cHg7bWFyZ2luOjIwcHggMH0ubW9kZXtib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTIxNzFkO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHggNXB4O2ZvbnQtd2VpZ2h0OjkwMDtjb2xvcjojZDNkOWRmfS5tb2RlLmFjdGl2ZXtib3JkZXItY29sb3I6Izk4NDkxZjtiYWNrZ3JvdW5kOiMzYjIwMTQ7Y29sb3I6I2ZmOWE0ZH0ucXVldWVib3h7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjNTkzMDFiO2JvcmRlci1yYWRpdXM6MTlweDtwYWRkaW5nOjMwcHggMjJweDtiYWNrZ3JvdW5kOnJhZGlhbC1ncmFkaWVudChlbGxpcHNlIGF0IDUwJSAwLCMzYTFjMTAsdHJhbnNwYXJlbnQgNTglKSxsaW5lYXItZ3JhZGllbnQoMTQ1ZGVnLCMxNTFhMjAsIzBlMTIxNil9LnF1ZXVlaWNvbnt3aWR0aDoxMDBweDtoZWlnaHQ6MTAwcHg7bWFyZ2luOjAgYXV0byAxOHB4O2JvcmRlcjoxcHggc29saWQgIzc1M2ExYztib3JkZXItcmFkaXVzOjUwJTtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2NvbG9yOiNmZjhjMjU7Zm9udC1zaXplOjM2cHg7Ym94LXNoYWRvdzowIDAgMCA5cHggI2ZmNjQxYzA4LDAgMCA0NXB4ICNmZjY0MWMxNX0ucXVldWVib3ggaDN7Zm9udC1zaXplOjIzcHg7bWFyZ2luOjhweCAwfS5xdWV1ZWJveCBwe2NvbG9yOiNjNGNkZDY7Zm9udC1zaXplOjEycHh9LnRhYnN7ZGlzcGxheTpmbGV4O2dhcDo3cHg7ZmxleC13cmFwOndyYXA7bWFyZ2luLWJvdHRvbToxNXB4fS50YWJzIGJ1dHRvbntib3JkZXI6MXB4IHNvbGlkICMzMDM5NDM7YmFja2dyb3VuZDojMTkxZjI3O2JvcmRlci1yYWRpdXM6OXB4O2NvbG9yOiNhZGI3YzE7cGFkZGluZzo5cHggMTNweDtmb250LXNpemU6MTFweH0udGFicyBidXR0b24uYWN0aXZle2JhY2tncm91bmQ6IzNhMjAxNDtib3JkZXItY29sb3I6IzgwNDAxZjtjb2xvcjojZmY5YTRkfS50YWJsZXdyYXB7b3ZlcmZsb3c6YXV0bztib3JkZXI6MXB4IHNvbGlkICMyYzM1NDA7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0udGFibGV7Ym9yZGVyLWNvbGxhcHNlOmNvbGxhcHNlO3dpZHRoOjEwMCU7bWluLXdpZHRoOjUyMHB4fS50YWJsZSB0aCwudGFibGUgdGR7dGV4dC1hbGlnbjpsZWZ0O3BhZGRpbmc6MTRweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjUyZDM2O2ZvbnQtc2l6ZToxMXB4fS50YWJsZSB0aHtiYWNrZ3JvdW5kOiMyMDI4MzM7Y29sb3I6I2QwZDdkZjtmb250LXdlaWdodDo4MDB9LnRhYmxlIHRke2NvbG9yOiNlOGVkZjJ9LnRhYmxlIHRyOmxhc3QtY2hpbGQgdGR7Ym9yZGVyLWJvdHRvbTowfS50YWJsZSB0ci55b3V7YmFja2dyb3VuZDojMmIxYTExfS5wcm9maWxlaGVyb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MjBweDtib3JkZXI6MXB4IHNvbGlkICM2MDM1MWY7Ym9yZGVyLXJhZGl1czoxN3B4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDExMGRlZywjMmIxOTBmLCMxNTE5MWUpO21hcmdpbi1ib3R0b206MTVweH0ucHJvZmlsZWF2YXRhcnt3aWR0aDo3MnB4O2hlaWdodDo3MnB4O2JvcmRlci1yYWRpdXM6MTlweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZmY4ZDI2LCM1YjIxMTEpO2ZvbnQtc2l6ZToyOHB4O2JveC1zaGFkb3c6MCAwIDAgNHB4ICNmZjY0MWMxOH0ucHJvZmlsZWhlcm8gaDN7bWFyZ2luOjAgMCA1cHg7Zm9udC1zaXplOjIwcHh9LnByb2ZpbGVoZXJvIHB7bWFyZ2luOjAgMCA4cHg7Zm9udC1zaXplOjExcHg7Y29sb3I6I2FlYjhjMn0ucHJvZmlsZWVsb3ttYXJnaW4tbGVmdDphdXRvO3RleHQtYWxpZ246cmlnaHR9LnByb2ZpbGVlbG8gc21hbGx7Zm9udC1zaXplOjlweDtjb2xvcjojYWViOGMyO2xldHRlci1zcGFjaW5nOjFweH0ucHJvZmlsZWVsbyBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MzBweDtjb2xvcjojZmY5YTRkfS5mb3JtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9LmZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6N3B4O21hcmdpbjoxMHB4IDB9LmZpZWxkIGxhYmVse2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiMWJiYzV9LmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe3dpZHRoOjEwMCU7Ym9yZGVyOjFweCBzb2xpZCAjMzAzOTQzO2JhY2tncm91bmQ6IzA4MGIxMDtjb2xvcjojZjRmNmY4O2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjEycHg7b3V0bGluZTpub25lfS5maWVsZCBpbnB1dDpmb2N1c3tib3JkZXItY29sb3I6dmFyKC0tb3JhbmdlKX0uY2xhbmhlcm97ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTRweDtwYWRkaW5nOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCAjMmUzYjQ5O2JvcmRlci1yYWRpdXM6MTZweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMTBkZWcsIzE4MjQzMSwjMTExNjFjKX0uY2xhbmxvZ297d2lkdGg6NjRweDtoZWlnaHQ6NjRweDtkaXNwbGF5OmdyaWQ7cGxhY2UtaXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6IzFkMzA0Mjtib3JkZXItcmFkaXVzOjE3cHg7Zm9udC1zaXplOjMwcHh9LmNsYW5oZXJvIGgze21hcmdpbjo0cHggMDtmb250LXNpemU6MTlweH0uY2xhbmhlcm8gcHttYXJnaW46MDtjb2xvcjojYWFiNWMwO2ZvbnQtc2l6ZToxMXB4fS5jbGFuaGVybyAuZ3Jvd3tmbGV4OjF9Lm5vdGljZXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1zdGFydDtnYXA6MTFweDtwYWRkaW5nOjE0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMjgzMDNhfS5ub3RpY2U6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOjB9Lm5vdGljZWljb3t3aWR0aDozN3B4O2hlaWdodDozN3B4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXItcmFkaXVzOjExcHg7YmFja2dyb3VuZDojM2IyMDE0O2NvbG9yOiNmZjlhNGR9Lm5vdGljZSBie2ZvbnQtc2l6ZToxMnB4fS5ub3RpY2UgcHttYXJnaW46NHB4IDA7Y29sb3I6I2MyY2NkNjtmb250LXNpemU6MTFweH0uYm90dG9tbmF2e2Rpc3BsYXk6bm9uZX0udG9hc3R7cG9zaXRpb246Zml4ZWQ7ei1pbmRleDoxMDA7cmlnaHQ6MThweDtib3R0b206MThweDttYXgtd2lkdGg6Y2FsYygxMDB2dyAtIDM2cHgpO2JhY2tncm91bmQ6IzE3MWYyNjtib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTE7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHZhcigtLW9yYW5nZSk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTNweCAxNnB4O2JveC1zaGFkb3c6MCAxMnB4IDQwcHggIzAwMDk7Y29sb3I6I2Y1ZjdmODtmb250LXNpemU6MTJweDthbmltYXRpb246YXBwZWFyIC4yc30uZm9vdG5vdGV7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNDttYXJnaW4tdG9wOjEycHg7bGluZS1oZWlnaHQ6MS41fQpAbWVkaWEobWF4LXdpZHRoOjEwNTBweCl7LnNoZWxse2dyaWQtdGVtcGxhdGUtY29sdW1uczoyMDVweCBtaW5tYXgoMCwxZnIpfS5sYXlvdXR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uc2lkZWhpZGV7ZGlzcGxheTpub25lfX0KQG1lZGlhKG1heC13aWR0aDo3MDBweCl7LnNoZWxse2Rpc3BsYXk6YmxvY2t9LnNpZGViYXJ7ZGlzcGxheTpub25lfS5tYWlue3BhZGRpbmc6MjBweCAxNXB4IGNhbGMoOTRweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSl9LnRvcGJhcntwYWRkaW5nLWJvdHRvbToxNnB4O21hcmdpbi1ib3R0b206MThweH0udG9wYmFyIGgxe2ZvbnQtc2l6ZToyNXB4fS50b3BhY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2Rpc3BsYXk6bm9uZX0ubGF5b3V0e2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnI7Z2FwOjEzcHh9LnN0YWNre2dhcDoxM3B4fS5oZXJve21pbi1oZWlnaHQ6MzAwcHg7cGFkZGluZzoyNHB4IDIxcHg7Ym9yZGVyLXJhZGl1czoyMHB4fS5oZXJvOmJlZm9yZXtyaWdodDotOTBweDt0b3A6LThweDt3aWR0aDoyMzBweDtoZWlnaHQ6MjMwcHh9Lmhlcm86YWZ0ZXJ7cmlnaHQ6LTlweDt0b3A6MTJweDtmb250LXNpemU6MjIwcHh9Lmhlcm8gaDJ7Zm9udC1zaXplOjM3cHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvIHB7Zm9udC1zaXplOjEzcHg7bWF4LXdpZHRoOjM0MHB4fS5oZXJvLWFjdGlvbnN7Z2FwOjhweH0uaGVyby1hY3Rpb25zIGJ1dHRvbntwYWRkaW5nOjEycHggMTNweDtmb250LXNpemU6MTJweH0uc3RhdHN7Z2FwOjhweH0uc3RhdHtwYWRkaW5nOjEzcHggMTBweDtib3JkZXItcmFkaXVzOjE1cHh9LnN0YXQgbGFiZWx7Zm9udC1zaXplOjlweDtsZXR0ZXItc3BhY2luZzouM3B4fS5zdGF0IHN0cm9uZ3tmb250LXNpemU6MjFweH0uc3RhdCBzbWFsbHtmb250LXNpemU6OXB4O2xpbmUtaGVpZ2h0OjEuMztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6NHB4fS5jYXJke3BhZGRpbmc6MTVweDtib3JkZXItcmFkaXVzOjE2cHh9LnBhc3NjYXJke3BhZGRpbmc6MThweH0ubWFwZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDIsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9Lm1hcGFydHtoZWlnaHQ6OTNweH0uc2VjdGlvbmhlYWQgaDJ7Zm9udC1zaXplOjI0cHh9LnNlY3Rpb25oZWFkIHB7bWF4LXdpZHRoOjI1MHB4O2xpbmUtaGVpZ2h0OjEuNX0ucXVldWVib3h7cGFkZGluZzoyNXB4IDE1cHh9Lm1vZGVncmlke2dhcDo2cHh9Lm1vZGV7cGFkZGluZzoxMnB4IDJweDtmb250LXNpemU6MTJweH0ucHJvZmlsZWhlcm97cGFkZGluZzoxNXB4O2dhcDoxMXB4fS5wcm9maWxlYXZhdGFye3dpZHRoOjU3cHg7aGVpZ2h0OjU3cHg7Zm9udC1zaXplOjIzcHh9LnByb2ZpbGVoZXJvIGgze2ZvbnQtc2l6ZToxN3B4fS5wcm9maWxlZWxvIHN0cm9uZ3tmb250LXNpemU6MjRweH0uZm9ybWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0uYm90dG9tbmF2e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsMWZyKTtwb3NpdGlvbjpmaXhlZDt6LWluZGV4OjUwO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3BhZGRpbmc6OHB4IDRweCBjYWxjKDhweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7YmFja2dyb3VuZDpyZ2JhKDgsMTAsMTMsLjk3KTtiYWNrZHJvcC1maWx0ZXI6Ymx1cigxNnB4KTtib3JkZXItdG9wOjFweCBzb2xpZCAjMjkzMjNkO2JveC1zaGFkb3c6MCAtOXB4IDI1cHggIzAwMDV9LmJvdHRvbW5hdiBidXR0b257ZGlzcGxheTpncmlkO2p1c3RpZnktaXRlbXM6Y2VudGVyO2dhcDozcHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjowO2NvbG9yOiNiYWM0Y2U7Zm9udC1zaXplOjlweDtwYWRkaW5nOjRweCAwfS5ib3R0b21uYXYgYnV0dG9uIHNwYW57Zm9udC1zaXplOjE5cHh9LmJvdHRvbW5hdiBidXR0b24uYWN0aXZle2NvbG9yOiNmZjlhNGR9LnRhYmxlIHRoLC50YWJsZSB0ZHtwYWRkaW5nOjEycHh9LnBhc3MtZm9vdHtmb250LXNpemU6MTBweH19CgovKiBDb250cmFzdCBoYXJkZW5pbmcgZm9yIGVtYmVkZGVkIFRlbGVncmFtL1B1enpsZS5ib3Qgd2Vidmlld3MgKi8KOnJvb3R7LS10ZXh0OiNmZmY7LS1tdXRlZDojYzBjYWQ0Oy0tc3Ryb2tlOiMzYTQ2NTN9CmJvZHl7Y29sb3I6I2ZmZn0KaDEsaDIsaDMsaDQsYixzdHJvbmcsLmJyYW5kbmFtZSwubWF0Y2hjb3B5IGIsLnBsYXllciBiLC5jYXJkaGVhZCBoMywuc2VjdGlvbmhlYWQgaDJ7Y29sb3I6I2ZmZn0KcHtjb2xvcjojZDBkN2RmfQpsYWJlbHtjb2xvcjojZDBkN2RmfQouY2FyZCwuc3RhdCwudGFibGV3cmFwLC5xdWV1ZWJveCwubWF0Y2hyb3d7Y29sb3I6I2ZmZn0KLnN0YXQgbGFiZWwsLm1hdGNoY29weSBzbWFsbCwucGxheWVyIHNtYWxsLC5zZWN0aW9uaGVhZCBwLC5tdXRlZCwuZm9vdG5vdGV7Y29sb3I6I2I5YzVkMH0KLnRhYmxlIHRoe2NvbG9yOiNlMGU2ZWN9Ci50YWJsZSB0ZHtjb2xvcjojZjdmOWZifQoubmF2IGJ1dHRvbntjb2xvcjojZDJkOWUxfQoubmF2IGJ1dHRvbi5hY3RpdmV7Y29sb3I6I2ZmYjA2Zn0KLmJ0bi1zZWNvbmRhcnl7Y29sb3I6I2ZmZjtiYWNrZ3JvdW5kOiMxYjIyMmI7Ym9yZGVyLWNvbG9yOiM0NjUzNjF9Ci50ZXh0bGlua3tjb2xvcjojZmZhZDcwfQouZmllbGQgbGFiZWx7Y29sb3I6I2Q1ZGNlNH0KLmZpZWxkIGlucHV0LC5maWVsZCBzZWxlY3QsLmZpZWxkIHRleHRhcmVhe2NvbG9yOiNmZmY7YmFja2dyb3VuZDojMDgwYzExO2JvcmRlci1jb2xvcjojNDY1MzYxfQoucGlsbHtjb2xvcjojNzVmMGI2fQouc3RhdCBzbWFsbHtjb2xvcjojNzVlYWIwfQoubm90aWNlIHB7Y29sb3I6I2MyY2NkNn0KLnBhc3NjYXJkIHB7Y29sb3I6I2QwZDdkZn0KCgovKiBGb3JjZSBkYXJrIGNhbnZhcyBpbiBlbWJlZGRlZCBidWlsZGVycy93ZWJ2aWV3cyAoaW5jbHVkaW5nIHNhZmUgYXJlYXMpICovCmh0bWwsYm9keXtiYWNrZ3JvdW5kLWNvbG9yOiMwODBhMGQhaW1wb3J0YW50O2NvbG9yOiNmZmYhaW1wb3J0YW50fQpib2R5e2JhY2tncm91bmQtaW1hZ2U6cmFkaWFsLWdyYWRpZW50KGVsbGlwc2UgYXQgMTAwJSAwLHJnYmEoMjU1LDgwLDE1LC4xMiksdHJhbnNwYXJlbnQgMjclKSFpbXBvcnRhbnR9CiNhcHAsLmFwcCwuY29udGFpbmVyLC5zaGVsbCwubWFpbntiYWNrZ3JvdW5kLWNvbG9yOnRyYW5zcGFyZW50fQppbnB1dCxzZWxlY3QsdGV4dGFyZWF7Y29sb3Itc2NoZW1lOmRhcmt9Cjo6c2VsZWN0aW9ue2JhY2tncm91bmQ6I2ZmNjQxYztjb2xvcjojMDgwYTBkfQoKCi8qIEhBUkQgT1ZFUlJJREVTOiBQdXp6bGUuYm90IC8gVGVsZWdyYW0gV2ViVmlldyBob3N0IHN0eWxlcyAqLwo6cm9vdCwgaHRtbCwgYm9keSB7CiAgYmFja2dyb3VuZDogIzA4MGEwZCAhaW1wb3J0YW50OwogIGJhY2tncm91bmQtY29sb3I6ICMwODBhMGQgIWltcG9ydGFudDsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50OwogIGNvbG9yLXNjaGVtZTogZGFyayAhaW1wb3J0YW50Owp9Cmh0bWwsIGJvZHkgewogIG1pbi1oZWlnaHQ6IDEwMCUgIWltcG9ydGFudDsKICBtYXJnaW46IDAgIWltcG9ydGFudDsKfQpib2R5IHsKICBiYWNrZ3JvdW5kLWltYWdlOiByYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSBhdCAxMDAlIDAsIHJnYmEoMjU1LDgwLDE1LC4xMCksIHRyYW5zcGFyZW50IDMwJSkgIWltcG9ydGFudDsKfQouc2hlbGwsIC5tYWluLCBtYWluLCBzZWN0aW9uLCBhcnRpY2xlLCAucGFnZSwgLmNvbnRlbnQsIC5jb250YWluZXIsICNhcHAsICNyb290IHsKICBjb2xvcjogI2Y3ZjlmYzsKfQpoMSwgaDIsIGgzLCBoNCwgaDUsIGg2LCBwLCBsYWJlbCwgc3Bhbiwgc21hbGwsIHN0cm9uZywgYiwgbGksIHRkLCB0aCB7CiAgdGV4dC1zaGFkb3c6IG5vbmU7Cn0KLnRvcGJhciBoMSwgLmNhcmQgaDIsIC5jYXJkIGgzLCAuc2VjdGlvbmhlYWQgaDIsIC5oZXJvIGgyLAouc3RhdCBzdHJvbmcsIC5tYXRjaGNvcHkgYiwgLnBsYXllciBiLCAuYnJhbmRuYW1lIHsKICBjb2xvcjogI2Y3ZjlmYyAhaW1wb3J0YW50Owp9Ci5raWNrZXIsIC50ZXh0bGluaywgLnNlY3Rpb25oZWFkIGEgeyBjb2xvcjogI2ZmYWI3MCAhaW1wb3J0YW50OyB9Ci5tdXRlZCwgLmZvb3Rub3RlLCAuc2VjdGlvbmhlYWQgcCwgLm1hdGNoY29weSBzbWFsbCwgLnBsYXllciBzbWFsbCwKLnN0YXQgbGFiZWwsIC5zdGF0IHNtYWxsLCAuY2FyZCBwLCAuY2FyZCBzbWFsbCB7CiAgY29sb3I6ICNjMmNjZDYgIWltcG9ydGFudDsKfQouY2FyZCwgLnN0YXQsIC5xdWV1ZWJveCwgLnRhYmxld3JhcCwgLm1hdGNocm93LCAubm90aWNlLCAucGFzc2NhcmQgewogIGNvbG9yOiAjZjdmOWZjOwp9CkBtZWRpYSAobWF4LXdpZHRoOiA3NjBweCkgewogIGh0bWwsIGJvZHksIC5zaGVsbCwgLm1haW4geyBiYWNrZ3JvdW5kLWNvbG9yOiAjMDgwYTBkICFpbXBvcnRhbnQ7IH0KfQoKCi8qIEZJTkFMIFJFU1BPTlNJVkUgKyBDTEFOIFJPU1RFUiBGSVggKi8KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIHsgd2lkdGg6MTAwJTsgbWluLXdpZHRoOjA7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBkaXNwbGF5OmdyaWQgIWltcG9ydGFudDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOm1pbm1heCgwLDFmcikgbWF4LWNvbnRlbnQgIWltcG9ydGFudDsgYWxpZ24taXRlbXM6Y2VudGVyICFpbXBvcnRhbnQ7IGNvbHVtbi1nYXA6MTZweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MDsgcGFkZGluZzoxM3B4IDJweCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHsgZGlzcGxheTpmbGV4ICFpbXBvcnRhbnQ7IGFsaWduLWl0ZW1zOmNlbnRlciAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBtaW4td2lkdGg6MCAhaW1wb3J0YW50OyBvdmVyZmxvdzpoaWRkZW47IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgPiBkaXY6bGFzdC1jaGlsZCB7IG1pbi13aWR0aDowICFpbXBvcnRhbnQ7IGZsZXg6MSAxIGF1dG8gIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAjY2xhbk1lbWJlcnMgLnBsYXllciBiIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgZm9udC13ZWlnaHQ6ODAwICFpbXBvcnRhbnQ7IHdoaXRlLXNwYWNlOm5vcm1hbCAhaW1wb3J0YW50OyBvdmVyZmxvdy13cmFwOmFueXdoZXJlICFpbXBvcnRhbnQ7IHdvcmQtYnJlYWs6YnJlYWstd29yZCAhaW1wb3J0YW50OyBvdmVyZmxvdzp2aXNpYmxlICFpbXBvcnRhbnQ7IHRleHQtb3ZlcmZsb3c6Y2xpcCAhaW1wb3J0YW50OyB9CiNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAucGxheWVyIHNtYWxsIHsgZGlzcGxheTpibG9jayAhaW1wb3J0YW50OyBtYXJnaW4tdG9wOjRweCAhaW1wb3J0YW50OyBjb2xvcjojYzJjY2Q2ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxMXB4ICFpbXBvcnRhbnQ7IGxpbmUtaGVpZ2h0OjEuMzUgIWltcG9ydGFudDsgd2hpdGUtc3BhY2U6bm9ybWFsICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5taW5pYXZhIHsgd2lkdGg6NDBweCAhaW1wb3J0YW50OyBoZWlnaHQ6NDBweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA0MHB4ICFpbXBvcnRhbnQ7IGZvbnQtc2l6ZToxM3B4ICFpbXBvcnRhbnQ7IH0KI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5lbG8geyBkaXNwbGF5OmJsb2NrICFpbXBvcnRhbnQ7IGp1c3RpZnktc2VsZjplbmQgIWltcG9ydGFudDsgYWxpZ24tc2VsZjpjZW50ZXIgIWltcG9ydGFudDsgbWluLXdpZHRoOjQ0cHg7IHBhZGRpbmc6N3B4IDlweCAhaW1wb3J0YW50OyBib3JkZXI6MXB4IHNvbGlkICMzYTQ2NTM7IGJvcmRlci1yYWRpdXM6OXB4OyBiYWNrZ3JvdW5kOiMyMDI4MzM7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgdGV4dC1hbGlnbjpyaWdodCAhaW1wb3J0YW50OyB3aGl0ZS1zcGFjZTpub3dyYXAgIWltcG9ydGFudDsgZm9udC1zaXplOjEzcHggIWltcG9ydGFudDsgbGluZS1oZWlnaHQ6MS4yICFpbXBvcnRhbnQ7IGZvbnQtd2VpZ2h0OjkwMCAhaW1wb3J0YW50OyBmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXM7IH0KI3BhZ2UtY2xhbnMgLmNhcmQgeyBtaW4td2lkdGg6MDsgb3ZlcmZsb3c6aGlkZGVuOyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCB7IGdhcDoxMnB4OyB9CiNwYWdlLWNsYW5zIC5jYXJkaGVhZCBoMyB7IGNvbG9yOiNmZmYgIWltcG9ydGFudDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8geyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gLmdyb3cgeyBtaW4td2lkdGg6MDsgfQojcGFnZS1jbGFucyAuY2xhbmhlcm8gaDMgeyBjb2xvcjojZmZmICFpbXBvcnRhbnQ7IG92ZXJmbG93LXdyYXA6YW55d2hlcmU7IH0KI3BhZ2UtY2xhbnMgLmNsYW5oZXJvIHAsICNwYWdlLWNsYW5zIC5zZWN0aW9uaGVhZCBwIHsgY29sb3I6I2MyY2NkNiAhaW1wb3J0YW50OyB9Cmh0bWwsIGJvZHkgeyB3aWR0aDoxMDAlOyBtYXgtd2lkdGg6MTAwJTsgb3ZlcmZsb3cteDpoaWRkZW47IH0KaW1nLCBzdmcsIHZpZGVvLCBjYW52YXMsIGJ1dHRvbiwgaW5wdXQsIHNlbGVjdCwgdGV4dGFyZWEgeyBtYXgtd2lkdGg6MTAwJTsgfQpAbWVkaWEgKG1heC13aWR0aDo2MDBweCkgewogLnNlY3Rpb24geyBwYWRkaW5nLWxlZnQ6MTRweDsgcGFkZGluZy1yaWdodDoxNHB4OyB9CiAuc2VjdGlvbmhlYWQgeyBhbGlnbi1pdGVtczpmbGV4LXN0YXJ0OyBmbGV4LXdyYXA6d3JhcDsgZ2FwOjEwcHg7IH0KIC5zZWN0aW9uaGVhZCBoMiB7IGZvbnQtc2l6ZToyM3B4OyBsaW5lLWhlaWdodDoxLjI7IH0KIC5zZWN0aW9uaGVhZCBwIHsgZm9udC1zaXplOjEzcHg7IGxpbmUtaGVpZ2h0OjEuNTsgfQogLmxheW91dCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczptaW5tYXgoMCwxZnIpICFpbXBvcnRhbnQ7IGdhcDoxNHB4OyB9CiAuY2FyZCB7IG1pbi13aWR0aDowOyBwYWRkaW5nOjE1cHg7IH0KIC5jbGFuaGVybyB7IHBhZGRpbmc6MTRweCAhaW1wb3J0YW50OyBnYXA6MTFweCAhaW1wb3J0YW50OyBmbGV4LXdyYXA6d3JhcDsgfQogLmNsYW5sb2dvIHsgd2lkdGg6NTJweCAhaW1wb3J0YW50OyBoZWlnaHQ6NTJweCAhaW1wb3J0YW50OyBmbGV4OjAgMCA1MnB4OyBmb250LXNpemU6MjVweCAhaW1wb3J0YW50OyB9CiAuY2xhbmhlcm8gLmdyb3cgeyBmbGV4OjEgMSBjYWxjKDEwMCUgLSA3NXB4KTsgfQogLmNsYW5oZXJvIGgzIHsgZm9udC1zaXplOjE3cHggIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5sZWFkZXIgeyBjb2x1bW4tZ2FwOjEwcHggIWltcG9ydGFudDsgcGFkZGluZzoxMnB4IDAgIWltcG9ydGFudDsgfQogI3BhZ2UtY2xhbnMgI2NsYW5NZW1iZXJzIC5wbGF5ZXIgYiB7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IH0KICNwYWdlLWNsYW5zICNjbGFuTWVtYmVycyAuZWxvIHsgbWluLXdpZHRoOjQycHg7IGZvbnQtc2l6ZToxMnB4ICFpbXBvcnRhbnQ7IHBhZGRpbmc6N3B4ICFpbXBvcnRhbnQ7IH0KIC5tYXBncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpOyB9Cn0KCgovKiBNYXRjaCBDZW50ZXIg4oCUIGRlZGljYXRlZCBjb21wZXRpdGl2ZSBmbG93ICovCi5tYXRjaC1jZW50ZXJ7ZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXI6MXB4IHNvbGlkICMzYTQxNGI7Ym9yZGVyLXJhZGl1czoxOHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzE0MWEyMSwjMGUxMjE3KTtib3gtc2hhZG93OjAgMThweCA0NXB4ICMwMDA1fQoubWF0Y2gtY2VudGVyLnZpc2libGV7ZGlzcGxheTpibG9ja30ubWMtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Z2FwOjE0cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTdweCAxOXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkICMyOTMxM2E7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTAwZGVnLCMyNDE1MGYsIzE0MTkxZil9Lm1jLXRpdGxle2ZvbnQtd2VpZ2h0Ojk1MDtmb250LXNpemU6MThweH0ubWMtc3Vie2ZvbnQtc2l6ZToxMnB4O2NvbG9yOiNhZWI4YzM7bWFyZ2luLXRvcDo0cHh9Lm1jLXN0YXR1c3tib3JkZXI6MXB4IHNvbGlkICM3MDQwMWY7Y29sb3I6I2ZmYWQ2OTtiYWNrZ3JvdW5kOiMzNTFlMTM7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo2cHggOXB4O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjkwMDtsZXR0ZXItc3BhY2luZzouN3B4O3doaXRlLXNwYWNlOm5vd3JhcH0ubWMtYm9keXtwYWRkaW5nOjE4cHg7ZGlzcGxheTpncmlkO2dhcDoxNnB4fS5tYy1wcm9ncmVzc3toZWlnaHQ6N3B4O2JvcmRlci1yYWRpdXM6OTlweDtiYWNrZ3JvdW5kOiMyNTJjMzQ7b3ZlcmZsb3c6aGlkZGVufS5tYy1wcm9ncmVzcyBzcGFue2Rpc3BsYXk6YmxvY2s7aGVpZ2h0OjEwMCU7d2lkdGg6MDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjZmY0ZTBiLCNmZjlhMjcpO3RyYW5zaXRpb246d2lkdGggLjJzfS5tYy1wbGF5ZXJncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDUsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtcGxheWVye21pbi13aWR0aDowO2JvcmRlcjoxcHggc29saWQgIzMwMzk0NDtiYWNrZ3JvdW5kOiMxNzFkMjQ7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7dGV4dC1hbGlnbjpjZW50ZXJ9Lm1jLXBsYXllci5hY2NlcHRlZHtib3JkZXItY29sb3I6IzIzNzk1NztiYWNrZ3JvdW5kOiMxMTI3MWZ9Lm1jLXBsYXllciBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfS5tYy1wbGF5ZXIgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojOThhNWIxO2ZvbnQtc2l6ZTo5cHg7bWFyZ2luLXRvcDo0cHh9Lm1jLXBsYXllciAubWMtYXZhdGFye3dpZHRoOjI4cHg7aGVpZ2h0OjI4cHg7Ym9yZGVyLXJhZGl1czo5cHg7bWFyZ2luOjAgYXV0byA2cHg7ZGlzcGxheTpncmlkO3BsYWNlLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMzMDIwMTY7Y29sb3I6I2ZmOWI1MDtmb250LXdlaWdodDo5NTB9Lm1jLWFjdGlvbnN7ZGlzcGxheTpmbGV4O2dhcDo5cHg7ZmxleC13cmFwOndyYXB9Lm1jLWFjdGlvbnMgLmJ0bi1wcmltYXJ5LC5tYy1hY3Rpb25zIC5idG4tc2Vjb25kYXJ5e2ZsZXg6MTttaW4td2lkdGg6MTMwcHh9Lm1jLXR3b3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEycHh9Lm1jLXBhbmVse2JvcmRlcjoxcHggc29saWQgIzJiMzMzZDtib3JkZXItcmFkaXVzOjEzcHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6IzExMTYxY30ubWMtcGFuZWwgaDR7bWFyZ2luOjAgMCA5cHg7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjhweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmOWE0ZH0ubWMtbXV0ZWR7Y29sb3I6IzlkYTliNTtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWZpZWxke2Rpc3BsYXk6Z3JpZDtnYXA6NnB4O21hcmdpbjo5cHggMH0ubWMtZmllbGQgbGFiZWx7Zm9udC1zaXplOjEwcHg7Y29sb3I6I2FlYjljNTtsZXR0ZXItc3BhY2luZzouN3B4O2ZvbnQtd2VpZ2h0OjgwMH0ubWMtZmllbGQgc2VsZWN0LC5tYy1maWVsZCBpbnB1dCwubWMtZmllbGQgdGV4dGFyZWF7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMwYjBmMTM7Ym9yZGVyOjFweCBzb2xpZCAjMzQzZDQ3O2JvcmRlci1yYWRpdXM6OHB4O2NvbG9yOiNmNGY2Zjg7cGFkZGluZzoxMHB4O21pbi13aWR0aDowfS5tYy1tYXBze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjdweH0ubWMtbWFwe2JvcmRlcjoxcHggc29saWQgIzMzM2Q0ODtiYWNrZ3JvdW5kOiMxODFmMjc7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTBweCA1cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMXB4fS5tYy1tYXA6aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZjc4MmF9Lm1jLW1hcC51c2Vke29wYWNpdHk6LjM1O3BvaW50ZXItZXZlbnRzOm5vbmU7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaH0ubWMtbG9ne2Rpc3BsYXk6Z3JpZDtnYXA6NnB4fS5tYy1sb2dyb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7Zm9udC1zaXplOjExcHg7cGFkZGluZzo4cHggOXB4O2JvcmRlci1yYWRpdXM6OHB4O2JhY2tncm91bmQ6IzFhMjAyOH0ubWMtbG9ncm93IHNwYW46bGFzdC1jaGlsZHtjb2xvcjojZmZhZDY5fS5tYy10ZWFte2JvcmRlcjoxcHggc29saWQgIzJlMzk0NDtiYWNrZ3JvdW5kOiMxNTFiMjI7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTJweH0ubWMtdGVhbSBoNHttYXJnaW46MCAwIDhweDtmb250LXNpemU6MTJweH0ubWMtdGVhbSB1bHttYXJnaW46MDtwYWRkaW5nLWxlZnQ6MThweDtjb2xvcjojYzNjYmQzO2ZvbnQtc2l6ZToxMnB4O2xpbmUtaGVpZ2h0OjEuOX0ubWMtZm9ybWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo5cHh9Lm1jLW5vdGljZXtib3JkZXI6MXB4IHNvbGlkICMzYzMwMjM7YmFja2dyb3VuZDojMjQxYjE0O2NvbG9yOiNlNmM5YWQ7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6MTBweDtmb250LXNpemU6MTJweDtsaW5lLWhlaWdodDoxLjV9Lm1jLWhpZGRlbntkaXNwbGF5Om5vbmUhaW1wb3J0YW50fQpAbWVkaWEobWF4LXdpZHRoOjYwMHB4KXsubWMtYm9keXtwYWRkaW5nOjEzcHh9Lm1jLXBsYXllcmdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg1LG1pbm1heCgwLDFmcikpO2dhcDo0cHh9Lm1jLXBsYXllcntwYWRkaW5nOjhweCAycHh9Lm1jLXBsYXllciBie2ZvbnQtc2l6ZTo5cHh9Lm1jLW1hcHN7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5tYy10d297Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0ubWMtaGVhZHtwYWRkaW5nOjE0cHh9Lm1jLXRpdGxle2ZvbnQtc2l6ZToxNnB4fX0KCgouY29zbWV0aWMtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLG1pbm1heCgwLDFmcikpO2dhcDoxMnB4fS5jb3NtZXRpYy1pdGVte2JvcmRlcjoxcHggc29saWQgIzM1NDE0ZDtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxNHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE0NWRlZywjMTcxZTI2LCMwZTEyMTcpO21pbi13aWR0aDowfS5jb3NtZXRpYy1hcnR7aGVpZ2h0Ojk1cHg7Ym9yZGVyLXJhZGl1czoxMnB4O2Rpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7Zm9udC1zaXplOjM2cHg7bWFyZ2luLWJvdHRvbToxMnB4O2JhY2tncm91bmQ6cmFkaWFsLWdyYWRpZW50KGNpcmNsZSBhdCA1MCUgMjAlLCM2MTM1MWUsIzFhMWQyMiA3MiUpO2JvcmRlcjoxcHggc29saWQgIzZhM2EyMH0uY29zbWV0aWMtaXRlbSBoNHttYXJnaW46MCAwIDVweDtmb250LXNpemU6MTNweH0uY29zbWV0aWMtaXRlbSBwe2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiNiZGM4ZDI7bWluLWhlaWdodDozMnB4O21hcmdpbjowIDAgMTBweH0uY29zbWV0aWMtbWV0YXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMHB4O2NvbG9yOiNmZmFkNzB9LmNvc21ldGljLWl0ZW0gYnV0dG9ue3dpZHRoOjEwMCU7Zm9udC1zaXplOjExcHg7cGFkZGluZzoxMHB4IDhweH0ucmFyaXR5e2ZvbnQtc2l6ZTo5cHg7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi43cHg7Y29sb3I6I2ZmY2E2OH0udHJvcGh5LWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNSxtaW5tYXgoMCwxZnIpKTtnYXA6MTBweH0udHJvcGh5e3BhZGRpbmc6MTNweCA4cHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOjFweCBzb2xpZCAjMzk0NjUzO2JvcmRlci1yYWRpdXM6MTNweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzFiMjMyYywjMTAxNDE5KX0udHJvcGh5IC5jdXB7Zm9udC1zaXplOjMwcHg7ZmlsdGVyOmRyb3Atc2hhZG93KDAgNHB4IDdweCAjMDAwOCl9LnRyb3BoeSBie2Rpc3BsYXk6YmxvY2s7Zm9udC1zaXplOjExcHg7bWFyZ2luOjZweCAwIDNweH0udHJvcGh5IHNtYWxse2ZvbnQtc2l6ZTo5cHg7Y29sb3I6I2JhYzVjZjtkaXNwbGF5OmJsb2NrfS5zdGF0LWRldGFpbC1ncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsbWlubWF4KDAsMWZyKSk7Z2FwOjEwcHh9LnN0YXQtZGV0YWlse3BhZGRpbmc6MTRweDtib3JkZXI6MXB4IHNvbGlkICMzNDQwNGM7Ym9yZGVyLXJhZGl1czoxM3B4O2JhY2tncm91bmQ6IzEyMTgyMH0uc3RhdC1kZXRhaWwgc21hbGx7ZGlzcGxheTpibG9jaztjb2xvcjojYjdjMmNjO2ZvbnQtc2l6ZToxMHB4fS5zdGF0LWRldGFpbCBzdHJvbmd7ZGlzcGxheTpibG9jaztmb250LXNpemU6MjJweDttYXJnaW4tdG9wOjdweDtjb2xvcjojZmY5YTRkfS5jb3NtZXRpYy1lbXB0eXtncmlkLWNvbHVtbjoxLy0xO3BhZGRpbmc6MjBweDt0ZXh0LWFsaWduOmNlbnRlcjtjb2xvcjojYjljNWQwO2JvcmRlcjoxcHggZGFzaGVkICMzOTQ2NTM7Ym9yZGVyLXJhZGl1czoxMnB4fUBtZWRpYShtYXgtd2lkdGg6NzAwcHgpey5jb3NtZXRpYy1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX0udHJvcGh5LWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgyLG1pbm1heCgwLDFmcikpfS5zdGF0LWRldGFpbC1ncmlke2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMixtaW5tYXgoMCwxZnIpKX19CgoKLndhbGxldC1oZXJvLC50b3VybmFtZW50LWhlcm97cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVuO2JvcmRlcjoxcHggc29saWQgIzZiMzUxYztib3JkZXItcmFkaXVzOjIycHg7cGFkZGluZzoyNXB4O2JhY2tncm91bmQ6cmFkaWFsLWdyYWRpZW50KGVsbGlwc2UgYXQgOTAlIDAsIzcxMzcxODY2LHRyYW5zcGFyZW50IDQ0JSksbGluZWFyLWdyYWRpZW50KDEyMGRlZywjMjkxNzBmLCMxNTE5MWYgNzAlKTt9Ci53YWxsZXQtaGVyb3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4fS53YWxsZXQtaGVybyBoMntmb250LXNpemU6MzhweDtsaW5lLWhlaWdodDoxO21hcmdpbjoxMnB4IDB9LndhbGxldC1oZXJvIGgyIHNtYWxse2ZvbnQtc2l6ZToxNXB4O2NvbG9yOiNmZjliNGR9LndhbGxldC1oZXJvIHAsLnRvdXJuYW1lbnQtaGVybyBwe2NvbG9yOiNjMmNjZDY7bWFyZ2luOjB9LndhbGxldC1tYXJre3dpZHRoOjc4cHg7aGVpZ2h0Ojc4cHg7ZGlzcGxheTpncmlkO3BsYWNlLWl0ZW1zOmNlbnRlcjtib3JkZXItcmFkaXVzOjIycHg7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTQwZGVnLCNmZjliMjgsI2ZmNGQwYSk7Y29sb3I6IzFhMTAwYTtmb250LXNpemU6MjVweDtmb250LXdlaWdodDoxMDAwO3RyYW5zZm9ybTpyb3RhdGUoOGRlZyk7Ym94LXNoYWRvdzowIDEycHggMzVweCAjZmY2NDFjMzB9LmdvbGQtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLG1pbm1heCgwLDFmcikpO2dhcDoxMnB4fS5nb2xkLWNhcmR7cG9zaXRpb246cmVsYXRpdmU7cGFkZGluZzoxOHB4O2JvcmRlcjoxcHggc29saWQgIzM1NDE0ZDtib3JkZXItcmFkaXVzOjE4cHg7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTUwZGVnLCMxNzFlMjYsIzBmMTMxOCk7b3ZlcmZsb3c6aGlkZGVufS5nb2xkLWNhcmQuZmVhdHVyZWR7Ym9yZGVyLWNvbG9yOiNhNDUxMjM7Ym94LXNoYWRvdzowIDAgMCAxcHggI2ZmNmIxYjI0LDAgMTZweCAzNnB4ICMwMDAzfS5nb2xkLXJpYmJvbntwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MDtyaWdodDowO3BhZGRpbmc6NnB4IDlweDtib3JkZXItcmFkaXVzOjAgMCAwIDEwcHg7YmFja2dyb3VuZDojZmY2ODFiO2NvbG9yOiMxYTEwMDk7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDoxMDAwfS5nb2xkLWljb257Zm9udC1zaXplOjI3cHg7Y29sb3I6I2ZmYjUzZjttYXJnaW4tYm90dG9tOjEzcHh9LmdvbGQtY2FyZCBoM3tmb250LXNpemU6MjBweDttYXJnaW46MTNweCAwIDVweH0uZ29sZC1jYXJkIHB7Zm9udC1zaXplOjExcHg7Y29sb3I6I2JhYzVjZjttaW4taGVpZ2h0OjMwcHh9LmdvbGQtcHJpY2V7Zm9udC1zaXplOjI2cHg7Zm9udC13ZWlnaHQ6OTUwO21hcmdpbjoxNnB4IDB9LmdvbGQtY2FyZCAuYnRuLXByaW1hcnl7d2lkdGg6MTAwJTtmb250LXNpemU6MTJweDtwYWRkaW5nOjEycHggOHB4fS50b3VybmFtZW50LWhlcm97cGFkZGluZzpjbGFtcCgyNHB4LDV2dyw0NXB4KX0udG91cm5hbWVudC1oZXJvIGgye2ZvbnQtc2l6ZTpjbGFtcCgzMXB4LDV2dyw0OHB4KTtsaW5lLWhlaWdodDoxLjA0O2xldHRlci1zcGFjaW5nOi0xLjNweDttYXJnaW46MTJweCAwfS50b3VybmFtZW50LWhlcm8gaDIgc3Bhbntjb2xvcjojZmY3MTFmfS50b3VybmFtZW50LWhlcm8gcHttYXgtd2lkdGg6NTcwcHg7bWFyZ2luLWJvdHRvbToyMHB4fS50b3VybmFtZW50LXN0YXRze21hcmdpbi10b3A6MTRweH0KQG1lZGlhKG1heC13aWR0aDo3MDBweCl7LmdvbGQtZ3JpZHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyfS5nb2xkLWNhcmR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczo1NHB4IDFmcjtjb2x1bW4tZ2FwOjEwcHg7YWxpZ24taXRlbXM6Y2VudGVyfS5nb2xkLWljb257Z3JpZC1yb3c6MS80O21hcmdpbjowfS5nb2xkLWNhcmQgLmJhZGdlLW9yYW5nZXtqdXN0aWZ5LXNlbGY6c3RhcnR9LmdvbGQtY2FyZCBoM3ttYXJnaW46NnB4IDB9LmdvbGQtY2FyZCBwe21pbi1oZWlnaHQ6MDttYXJnaW46MH0uZ29sZC1wcmljZXttYXJnaW46MTBweCAwfS5nb2xkLWNhcmQgLmJ0bi1wcmltYXJ5e2dyaWQtY29sdW1uOjEvLTE7bWFyZ2luLXRvcDo3cHh9LndhbGxldC1oZXJve3BhZGRpbmc6MjFweH0ud2FsbGV0LWhlcm8gaDJ7Zm9udC1zaXplOjMzcHh9LndhbGxldC1tYXJre3dpZHRoOjYwcHg7aGVpZ2h0OjYwcHg7ZmxleDowIDAgNjBweH0udG91cm5hbWVudC1oZXJve3BhZGRpbmc6MjRweCAyMHB4fX0KLnNwLWNvaW57ZGlzcGxheTppbmxpbmUtZ3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7d2lkdGg6MzZweDtoZWlnaHQ6MzZweDtib3JkZXItcmFkaXVzOjUwJTtmb250LXNpemU6MTFweDtmb250LXdlaWdodDoxMDAwO2NvbG9yOiMyMDEwMDY7YmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoY2lyY2xlIGF0IDMwJSAyNSUsI2ZmZjBhMSwjZmZiNTJkIDMwJSwjZGY2NTExIDcwJSwjOGIzMTBhKTtib3JkZXI6MnB4IHNvbGlkICNmZmNjNjI7Ym94LXNoYWRvdzowIDAgMCAzcHggI2ZmOGEyMjE4LDAgNHB4IDEycHggIzAwMDh9LnNwLWNvaW4ubGFyZ2V7d2lkdGg6NDhweDtoZWlnaHQ6NDhweDtmb250LXNpemU6MTRweH0uY2xhaW1TUDpkaXNhYmxlZHtvcGFjaXR5Oi41NTtjdXJzb3I6bm90LWFsbG93ZWR9PC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0ic2hlbGwiPgo8YXNpZGUgY2xhc3M9InNpZGViYXIiPgogIDxkaXYgY2xhc3M9ImJyYW5kIj48ZGl2IGNsYXNzPSJicmFuZG1hcmsiPlM8L2Rpdj48ZGl2PjxkaXYgY2xhc3M9ImJyYW5kbmFtZSI+U1RBTkQ8c3Bhbj5QT0lOVDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJicmFuZHRhZyI+U1RBTkRPRkYgMiDigKIgQ09NUEVUSVRJVkU8L2Rpdj48L2Rpdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJuYXZncm91cCI+PGxhYmVsPtCf0JvQkNCi0KTQntCg0JzQkDwvbGFiZWw+PG5hdiBjbGFzcz0ibmF2IiBpZD0iZGVza3RvcE5hdiI+CiAgICA8YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtcGFnZT0iaG9tZSI+PHNwYW4gY2xhc3M9Im5pIj7ijII8L3NwYW4+0JPQu9Cw0LLQvdCw0Y88L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJtYXRjaGVzIj48c3BhbiBjbGFzcz0ibmkiPuKalDwvc3Bhbj7QnNCw0YLRh9C4PC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0icmF0aW5nIj48c3BhbiBjbGFzcz0ibmkiPuKZnDwvc3Bhbj7QoNC10LnRgtC40L3QsyBFTE88L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJjbGFucyI+PHNwYW4gY2xhc3M9Im5pIj7imaI8L3NwYW4+0JrQu9Cw0L3RiyA8c3BhbiBjbGFzcz0ibmF2Y291bnQiPk5FVzwvc3Bhbj48L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJwYXNzIj48c3BhbiBjbGFzcz0ibmkiPuKcpjwvc3Bhbj5TVEFORFBPSU5UIFBhc3M8L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJmcmllbmRzIj48c3BhbiBjbGFzcz0ibmkiPuKZpzwvc3Bhbj7QlNGA0YPQt9GM0Y88L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJub3RpZmljYXRpb25zIj48c3BhbiBjbGFzcz0ibmkiPuKZpzwvc3Bhbj7Qo9Cy0LXQtNC+0LzQu9C10L3QuNGPIDxzcGFuIGNsYXNzPSJuYXZjb3VudCI+Mjwvc3Bhbj48L2J1dHRvbj4KICAgIDxidXR0b24gZGF0YS1wYWdlPSJwcm9maWxlIj48c3BhbiBjbGFzcz0ibmkiPuKXiTwvc3Bhbj7Qn9GA0L7RhNC40LvRjDwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9ImNvc21ldGljcyI+PHNwYW4gY2xhc3M9Im5pIj7inKc8L3NwYW4+0JzQsNCz0LDQt9C40L0g0LrQvtGB0LzQtdGC0LjQutC4PC9idXR0b24+CiAgICA8YnV0dG9uIGRhdGEtcGFnZT0ic3RvcmUiPjxzcGFuIGNsYXNzPSJuaSI+4peIPC9zcGFuPtCc0LDQs9Cw0LfQuNC9INCy0LDQu9GO0YLRizwvYnV0dG9uPgogICAgPGJ1dHRvbiBkYXRhLXBhZ2U9InRvdXJuYW1lbnRzIj48c3BhbiBjbGFzcz0ibmkiPvCfj4Y8L3NwYW4+0KLRg9GA0L3QuNGA0Ys8L2J1dHRvbj4KICA8L25hdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJzaWRlYmFyLWZvb3QiPjxkaXYgY2xhc3M9InVzZXJjYXJkIj48ZGl2IGNsYXNzPSJhdmF0YXIiPlM8L2Rpdj48ZGl2PjxiIGlkPSJzaWRlTmFtZSI+c3RhbmRwb2ludF9wbGF5ZXI8L2I+PHNtYWxsPuKXjyDQlNCV0JzQni3Qn9Cg0J7QpNCY0JvQrDwvc21hbGw+PC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7QotCS0J7QryDQmNCT0KDQkC4g0KLQktCe0K8g0J/QntCX0JjQptCY0K8uPC9wPjwvZGl2Pgo8L2FzaWRlPgo8bWFpbiBjbGFzcz0ibWFpbiI+CjxoZWFkZXIgY2xhc3M9InRvcGJhciI+PGRpdj48ZGl2IGNsYXNzPSJraWNrZXIiPlNUQU5ET0ZGIDIg4oCiIEVTUE9SVFMgUExBVEZPUk08L2Rpdj48aDEgaWQ9InBhZ2VUaXRsZSI+0KLQstC+0Y8g0LjQs9GA0LAuINCi0LLQvtGPINC/0L7Qt9C40YbQuNGPLjwvaDE+PC9kaXY+PGRpdiBjbGFzcz0idG9wYWN0aW9ucyI+PHNwYW4gaWQ9ImFwaVN0YXR1cyIgY2xhc3M9ImJhZGdlLW9yYW5nZSI+QVBJOiDQv9C+0LTQutC70Y7Rh9C10L3QuNC14oCmPC9zcGFuPjxidXR0b24gY2xhc3M9Imljb25idG4iIGRhdGEtcGFnZT0ibm90aWZpY2F0aW9ucyIgYXJpYS1sYWJlbD0i0KPQstC10LTQvtC80LvQtdC90LjRjyI+4pmnPGkgY2xhc3M9InBpbmciPjwvaT48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBkYXRhLXBhZ2U9InByb2ZpbGUiPtCc0L7QuSDQv9GA0L7RhNC40LvRjCDihpc8L2J1dHRvbj48L2Rpdj48L2hlYWRlcj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIGFjdGl2ZSIgaWQ9InBhZ2UtaG9tZSI+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiPgogIDxkaXYgY2xhc3M9InN0YWNrIj4KICAgPGRpdiBjbGFzcz0iaGVybyI+PGRpdiBjbGFzcz0iaGVyby1jb3B5Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQktCV0KDQqNCY0J3QlTwvZGl2PjxoMj7QmNCz0YDQsNC5INGD0LzQvdC10LUuPGJyPjxzcGFuPtCf0L7QtNC90LjQvNCw0LkgRUxPLjwvc3Bhbj48L2gyPjxwPtCd0LDRhdC+0LTQuCDQutC+0LzQsNC90LTRgywg0L/QvtCx0LXQttC00LDQuSDQsiDRgNC10LnRgtC40L3Qs9C+0LLRi9GFINC80LDRgtGH0LDRhSDQuCDQtNC+0LrQsNC30YvQstCw0LksINC60YLQviDQt9C00LXRgdGMINGB0LjQu9GM0L3QtdC50YjQuNC5LjwvcD48ZGl2IGNsYXNzPSJoZXJvLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBkYXRhLXBhZ2U9Im1hdGNoZXMiPuKalCDQndCw0LnRgtC4INC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgZGF0YS1wYWdlPSJyYXRpbmciPtCi0LDQsdC70LjRhtCwINC70LjQtNC10YDQvtCyPC9idXR0b24+PC9kaXY+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0KLQktCe0JkgRUxPPC9sYWJlbD48c3Ryb25nIGlkPSJob21lRWxvIj4xMDAwPC9zdHJvbmc+PHNtYWxsPuKXjyDQodGC0LDRgNGC0L7QstGL0Lkg0YDQtdC50YLQuNC90LM8L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxsYWJlbD7Qn9Ce0JHQldCU0Ks8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXaW5zIj4wPC9zdHJvbmc+PHNtYWxsPtCc0LDRgtGH0Lgg0YHQtdC30L7QvdCwPC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+V0lOIFJBVEU8L2xhYmVsPjxzdHJvbmcgaWQ9ImhvbWVXUiI+MCU8L3N0cm9uZz48c21hbGw+0J7QsdGJ0LjQuSDQv9C+0LrQsNC30LDRgtC10LvRjDwvc21hbGw+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JHQu9C40LbQsNC50YjQuNC5INC80LDRgtGHPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJtYXRjaGVzIj7QktGB0LUg0LzQsNGC0YfQuCDihpI8L2J1dHRvbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaHJvdyI+PGRpdiBjbGFzcz0ibWF0Y2hsb2dvIj7impQ8L2Rpdj48ZGl2IGNsYXNzPSJtYXRjaGNvcHkiPjxiPtCg0LXQudGC0LjQvdCz0L7QstGL0Lkg0LzQsNGC0YcgwrcgNXY1PC9iPjxzbWFsbD5TYW5kc3RvbmUgwrcgQ29tcGV0aXRpdmUgwrcg0J7QttC40LTQsNC90LjQtSDQuNCz0YDQvtC60L7Qsjwvc21hbGw+PC9kaXY+PHNwYW4gY2xhc3M9InBpbGwiPkxJVkUgUVVFVUU8L3NwYW4+PC9kaXY+PC9kaXY+CiAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QtdC00LDQstC90LjQtSDRgNC10LfRg9C70YzRgtCw0YLRizwvaDM+PHNwYW4gY2xhc3M9Im11dGVkIHNtYWxsIj7QodCV0JfQntCdIDAzPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7inKY8L2Rpdj48ZGl2PjxiPtCd0L7QstGL0Lkg0YHQtdC30L7QvSDQvdCw0YfQsNC70YHRjzwvYj48cD7Qn9C+0LTQvdC40LzQsNC5INGA0LXQudGC0LjQvdCzINC4INC+0YLQutGA0YvQstCw0Lkg0L3QsNCz0YDQsNC00YsgU1RBTkRQT0lOVCBQYXNzLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pqUPC9kaXY+PGRpdj48Yj7QoNC10LnRgtC40L3Qs9C+0LLQsNGPINC+0YfQtdGA0LXQtNGMINC+0YLQutGA0YvRgtCwPC9iPjxwPtCh0L7QsdC40YDQsNC5INC60L7QvNCw0L3QtNGDINC4INCy0YHRgtGD0L/QsNC5INCyINCx0L7QuS48L3A+PC9kaXY+PC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ic3RhY2siPgogICA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QodCV0JfQntCdIDAzPC9kaXY+PGgzPlNUQU5EUE9JTlQgUEFTUzwvaDM+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1sZXZlbCIgaWQ9InBhc3NMZXZlbCI+MDE8L2Rpdj48L2Rpdj48cD7QmNCz0YDQsNC5INC80LDRgtGH0LgsINC30LDRgNCw0LHQsNGC0YvQstCw0LkgWFAg0Lgg0L7RgtC60YDRi9Cy0LDQuSDQvdCw0LPRgNCw0LTRiyDRgdC10LfQvtC90LAuPC9wPjxkaXYgY2xhc3M9InByb2dyZXNzIj48c3BhbiBpZD0icGFzc1Byb2dyZXNzIj48L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0icGFzcy1mb290Ij48c3BhbiBpZD0icGFzc1hQIj4xMjAgLyAzMDAgWFA8L3NwYW4+PGJ1dHRvbiBjbGFzcz0idGV4dGxpbmsiIGRhdGEtcGFnZT0icGFzcyI+0J7RgtC60YDRi9GC0YwgUGFzcyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotC+0L8g0LjQs9GA0L7QutC+0LI8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBkYXRhLXBhZ2U9InJhdGluZyI+0JLRgdC1INGA0LXQudGC0LjQvdCz0Lgg4oaSPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImhvbWVMZWFkZXJzIj48L2Rpdj48L2Rpdj4KICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QotCy0L7QuSDQv9GA0L7Qs9GA0LXRgdGBPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5TRUFTT04gMDM8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hyb3ciPjxkaXYgY2xhc3M9Im1hdGNobG9nbyI+4pmcPC9kaXY+PGRpdiBjbGFzcz0ibWF0Y2hjb3B5Ij48Yj7Qn9C+0LTQvdC40LzQsNC50YHRjyDQsiDRgNC10LnRgtC40L3Qs9C1PC9iPjxzbWFsbD7QmtCw0LbQtNCw0Y8g0L/QvtCx0LXQtNCwINC/0YDQuNCx0LvQuNC20LDQtdGCINC6INGN0LvQuNGC0LUuPC9zbWFsbD48L2Rpdj48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgZGF0YS1wYWdlPSJyYXRpbmciPkVMTyDihpI8L2J1dHRvbj48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KIDwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtbWF0Y2hlcyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0JzQsNGC0YfQvNC10LnQutC40L3QszwvaDI+PHA+0JLRi9Cx0LXRgNC4INGA0LXQttC40Lwg0Lgg0LrQsNGA0YLRgy4g0J3QsNC50LTQuCDRgdC+0L/QtdGA0L3QuNC60L7QsiDRgdCy0L7QtdCz0L4g0YPRgNC+0LLQvdGPLjwvcD48L2Rpdj48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5DT01QRVRJVElWRTwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9ImxheW91dCI+CiAgPGRpdiBjbGFzcz0ic3RhY2siPjxkaXYgY2xhc3M9InF1ZXVlYm94Ij48ZGl2IGNsYXNzPSJxdWV1ZWljb24iIGlkPSJxdWV1ZUljb24iPuKalDwvZGl2PjxkaXYgY2xhc3M9ImV5ZWJyb3ciPtCf0J7QmNCh0Jog0JzQkNCi0KfQkDwvZGl2PjxoMyBpZD0icXVldWVUaXRsZSI+0JPQvtGC0L7QsiDQuiDQuNCz0YDQtT88L2gzPjxwIGlkPSJxdWV1ZURlc2NyaXB0aW9uIj7QktGL0LHQtdGA0Lgg0YDQtdC20LjQvCDQuCDQvdCw0YfQvdC4INC/0L7QuNGB0Log0YDQtdC50YLQuNC90LPQvtCy0L7Qs9C+INC80LDRgtGH0LAuPC9wPjxkaXYgY2xhc3M9Im1vZGVncmlkIiBpZD0ibW9kZUdyaWQiPjxidXR0b24gY2xhc3M9Im1vZGUiIGRhdGEtbW9kZT0iMXYxIj4xdjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtb2RlIiBkYXRhLW1vZGU9IjJ2MiI+MnYyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibW9kZSIgZGF0YS1tb2RlPSIzdjMiPjN2MzwvYnV0dG9uPjxidXR0b24gY2xhc3M9Im1vZGUgYWN0aXZlIiBkYXRhLW1vZGU9IjV2NSI+NXY1PC9idXR0b24+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJxdWV1ZUJ1dHRvbiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQudGC0Lgg0LzQsNGC0Yc8L2J1dHRvbj48cCBjbGFzcz0iZm9vdG5vdGUiIGlkPSJxdWV1ZUFwaU5vdGUiPtCf0L7QuNGB0Log0Lgg0YHRgtCw0YLRg9GBINC+0YfQtdGA0LXQtNC4INGB0LjQvdGF0YDQvtC90LjQt9C40YDRg9GO0YLRgdGPINGBINGB0LXRgNCy0LXRgNC+0LwgU1RBTkRQT0lOVC48L3A+PC9kaXY+CiAgPGRpdiBjbGFzcz0ibWF0Y2gtY2VudGVyIiBpZD0ibWF0Y2hDZW50ZXIiIGFyaWEtbGl2ZT0icG9saXRlIj4KICAgPGRpdiBjbGFzcz0ibWMtaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5TVEFORFBPSU5UIENPTVBFVElUSVZFPC9kaXY+PGRpdiBjbGFzcz0ibWMtdGl0bGUiIGlkPSJtY1RpdGxlIj7QnNCw0YLRhyDQvdCw0LnQtNC10L08L2Rpdj48ZGl2IGNsYXNzPSJtYy1zdWIiIGlkPSJtY1N1YnRpdGxlIj7Qn9C+0LTRgtCy0LXRgNC00Lgg0YPRh9Cw0YHRgtC40LUsINGH0YLQvtCx0Ysg0L/QtdGA0LXQudGC0Lgg0LIg0LvQvtCx0LHQuC48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0ibWMtc3RhdHVzIiBpZD0ibWNTdGF0dXMiPk1BVENIIEZPVU5EPC9zcGFuPjwvZGl2PgogICA8ZGl2IGNsYXNzPSJtYy1ib2R5Ij4KICAgIDxkaXYgaWQ9Im1jQWNjZXB0UGFuZWwiPjxkaXYgY2xhc3M9Im1jLXByb2dyZXNzIj48c3BhbiBpZD0ibWNBY2NlcHRQcm9ncmVzcyI+PC9zcGFuPjwvZGl2PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQWNjZXB0VGV4dCI+0J7QttC40LTQsNC10Lwg0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8g0LjQs9GA0L7QutC+0LLigKY8L3A+PGRpdiBjbGFzcz0ibWMtcGxheWVyZ3JpZCIgaWQ9Im1jQWNjZXB0UGxheWVycyI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyIgc3R5bGU9Im1hcmdpbi10b3A6MTJweCI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJtY0FjY2VwdEJ0biI+4pyTINCf0YDQuNC90Y/RgtGMINC80LDRgtGHPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9Im1jRGVjbGluZUJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJtY0JhblBhbmVsIiBjbGFzcz0ibWMtaGlkZGVuIj4KICAgICA8ZGl2IGNsYXNzPSJtYy10d28iPjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQTwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5BIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQSIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEE8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5BSW5mbyI+PC9wPjwvZGl2PjxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQ+0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQjwvaDQ+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QktCr0JHQoNCQ0KLQrCDQmtCQ0J/QmNCi0JDQndCQPC9sYWJlbD48c2VsZWN0IGlkPSJtY0NhcHRhaW5CIj48L3NlbGVjdD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTZXRDYXB0YWluQiIgc3R5bGU9IndpZHRoOjEwMCUiPtCd0LDQt9C90LDRh9C40YLRjCDQutCw0L/QuNGC0LDQvdCwIEI8L2J1dHRvbj48cCBjbGFzcz0ibWMtbXV0ZWQiIGlkPSJtY0NhcHRhaW5CSW5mbyI+PC9wPjwvZGl2PjwvZGl2PgogICAgIDxkaXYgY2xhc3M9Im1jLXBhbmVsIj48aDQgaWQ9Im1jQmFuVGl0bGUiPkJhbiAvIFBpY2sg0LrQsNGA0YI8L2g0PjxwIGNsYXNzPSJtYy1tdXRlZCIgaWQ9Im1jQmFuSGludCI+0JrQsNC/0LjRgtCw0L3RiyDQv9C+INC+0YfQtdGA0LXQtNC4INCx0LvQvtC60LjRgNGD0Y7RgiDQutCw0YDRgtGLLCDQt9Cw0YLQtdC8INCy0YvQsdC40YDQsNGO0YIg0LrQsNGA0YLRgyDQvNCw0YLRh9CwLjwvcD48ZGl2IGNsYXNzPSJtYy1tYXBzIiBpZD0ibWNNYXBCdXR0b25zIj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1sb2ciIGlkPSJtY01hcExvZyIgc3R5bGU9Im1hcmdpbi10b3A6MTBweCI+PC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9Im1jTG9iYnlQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCDQmtC+0LzQsNC90LTQsCBBPC9oND48dWwgaWQ9Im1jVGVhbUEiPjwvdWw+PC9kaXY+PGRpdiBjbGFzcz0ibWMtdGVhbSI+PGg0PvCflLUg0JrQvtC80LDQvdC00LAgQjwvaDQ+PHVsIGlkPSJtY1RlYW1CIj48L3VsPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLW5vdGljZSIgaWQ9Im1jTG9iYnlJbmZvIj7QmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwLiDQntC20LjQtNCw0LXQvCDQt9Cw0L/Rg9GB0Log0LzQsNGC0YfQsCDQutCw0L/QuNGC0LDQvdC+0Lwg0LrQvtC80LDQvdC00YsgQS48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9Im1jU3RhcnRCdG4iIHN0eWxlPSJ3aWR0aDoxMDAlO21hcmdpbi10b3A6MTBweCI+4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNQbGF5aW5nUGFuZWwiIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLW5vdGljZSI+0JzQsNGC0Ycg0L/RgNC+0YXQvtC00LjRgiDQsiBTdGFuZG9mZiAyLiDQn9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINCy0L3QtdGB0Lgg0YHRh9GR0YIg0L7QsdC10LjRhSDQutC+0LzQsNC90LQg4oCUINGA0LXQt9GD0LvRjNGC0LDRgiDRg9C50LTRkdGCINC80L7QtNC10YDQsNGC0L7RgNGDINC90LAg0L/RgNC+0LLQtdGA0LrRgywgRUxPINC90LUg0LjQt9C80LXQvdC40YLRgdGPINC00L4g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPC9kaXY+PGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQTwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQjwvbGFiZWw+PGlucHV0IGlkPSJtY1Njb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QotCS0J7QmCDQo9CR0JjQmdCh0KLQktCQIChLSUxMUyk8L2xhYmVsPjxpbnB1dCBpZD0ibWNLaWxscyIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwMCIgdmFsdWU9IjAiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KLQktCe0Jgg0KHQnNCV0KDQotCYIChERUFUSFMpPC9sYWJlbD48aW5wdXQgaWQ9Im1jRGVhdGhzIiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgbWF4PSIxMDAwIiB2YWx1ZT0iMCI+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibWMtZmllbGQiPjxsYWJlbD7QmtCe0JzQnNCV0J3QotCQ0KDQmNCZINCaINCg0JXQl9Cj0JvQrNCi0JDQotCjPC9sYWJlbD48dGV4dGFyZWEgaWQ9Im1jUmVzdWx0Tm90ZSIgcm93cz0iMiIgcGxhY2Vob2xkZXI9ItCd0LDQv9GA0LjQvNC10YA6INGB0LrRgNC40L3RiNC+0YIg0YDQtdC30YPQu9GM0YLQsNGC0LAg0LHRg9C00LXRgiDQv9GA0LjQu9C+0LbQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDIj48L3RleHRhcmVhPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0ibWNTdWJtaXRSZXN1bHQiIHN0eWxlPSJ3aWR0aDoxMDAlIj7QntGC0L/RgNCw0LLQuNGC0Ywg0YDQtdC30YPQu9GM0YLQsNGCINC90LAg0L/RgNC+0LLQtdGA0LrRgzwvYnV0dG9uPjwvZGl2PgogICAgPGRpdiBpZD0ibWNSZXZpZXdQYW5lbCIgY2xhc3M9Im1jLWhpZGRlbiI+PGRpdiBjbGFzcz0ibWMtbm90aWNlIiBpZD0ibWNSZXZpZXdJbmZvIj7QoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC90LAg0L/RgNC+0LLQtdGA0LrRgyDQvNC+0LTQtdGA0LDRgtC+0YDRgy4g0KDQtdC50YLQuNC90LMg0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS48L2Rpdj48ZGl2IGlkPSJtY0FwcGVhbEZvcm0iIGNsYXNzPSJtYy1oaWRkZW4iPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0J/QoNCY0KfQmNCd0JAg0JDQn9CV0JvQm9Cv0KbQmNCYPC9sYWJlbD48c2VsZWN0IGlkPSJtY0FwcGVhbFJlYXNvbiI+PG9wdGlvbiB2YWx1ZT0id3Jvbmdfc2NvcmUiPtCd0LXQstC10YDQvdGL0Lkg0YHRh9GR0YI8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJjaGVhdGluZyI+0J/QvtC00L7Qt9GA0LXQvdC40LUg0L3QsCDQvdC10YfQtdGB0YLQvdGD0Y4g0LjQs9GA0YM8L29wdGlvbj48b3B0aW9uIHZhbHVlPSJ0ZWNobmljYWxfaXNzdWUiPtCi0LXRhdC90LjRh9C10YHQutCw0Y8g0L/RgNC+0LHQu9C10LzQsDwvb3B0aW9uPjxvcHRpb24gdmFsdWU9Im90aGVyIj7QlNGA0YPQs9C+0LU8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJtYy1maWVsZCI+PGxhYmVsPtCf0J7QlNCg0J7QkdCd0J7QodCi0Jg8L2xhYmVsPjx0ZXh0YXJlYSBpZD0ibWNBcHBlYWxEZXRhaWxzIiByb3dzPSIzIiBwbGFjZWhvbGRlcj0i0J7Qv9C40YjQuCDQv9GA0L7QsdC70LXQvNGDINC00LvRjyDQvNC+0LTQtdGA0LDRgtC+0YDQsCI+PC90ZXh0YXJlYT48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibWNTdWJtaXRBcHBlYWwiIHN0eWxlPSJ3aWR0aDoxMDAlIj7Qn9C+0LTQsNGC0Ywg0LDQv9C10LvQu9GP0YbQuNGOPC9idXR0b24+PC9kaXY+PC9kaXY+CiAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J/RgNCw0LLQuNC70LAg0YDQtdC50YLQuNC90LPQsDwvaDM+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+RUxPPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im5vdGljZSI+PGRpdiBjbGFzcz0ibm90aWNlaWNvIj7ihpc8L2Rpdj48ZGl2PjxiPtCf0L7QsdC10LbQtNCw0Lkg4oCUINC/0L7Qu9GD0YfQsNC5INGA0LXQudGC0LjQvdCzPC9iPjxwPtCg0LXQt9GD0LvRjNGC0LDRgiDQvNCw0YLRh9CwINCy0LvQuNGP0LXRgiDQvdCwINGC0LLQvtC5IEVMTy48L3A+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm90aWNlIj48ZGl2IGNsYXNzPSJub3RpY2VpY28iPuKcpjwvZGl2PjxkaXY+PGI+0JjQs9GA0LDQuSDRgNC10LPRg9C70Y/RgNC90L48L2I+PHA+0JfQsNGA0LDQsdCw0YLRi9Cy0LDQuSDQvtC/0YvRgiDQtNC70Y8g0YHQtdC30L7QvdC90L7Qs9C+IFBhc3MuPC9wPjwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JLRi9Cx0L7RgCDQutCw0YDRgtGLPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCf0KDQldCU0J/QntCn0KLQldCd0JjQlTwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJtYXBncmlkIiBpZD0ibWFwR3JpZCI+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSBzZWxlY3RlZCIgZGF0YS1tYXA9IlNhbmRzdG9uZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+c77iPPC9zcGFuPjwvZGl2PjxiPlNhbmRzdG9uZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUHJvdmluY2UiPjxkaXYgY2xhc3M9Im1hcGFydCI+PHNwYW4+8J+Pme+4jzwvc3Bhbj48L2Rpdj48Yj5Qcm92aW5jZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iUnVzdCI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4+tPC9zcGFuPjwvZGl2PjxiPlJ1c3Q8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IkJyZWV6ZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4yKPC9zcGFuPjwvZGl2PjxiPkJyZWV6ZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iWm9uZSA3Ij48ZGl2IGNsYXNzPSJtYXBhcnQiPjxzcGFuPuKYou+4jzwvc3Bhbj48L2Rpdj48Yj5ab25lIDc8L2I+PC9idXR0b24+PGJ1dHRvbiBjbGFzcz0ibWFwdGlsZSIgZGF0YS1tYXA9IlNha3VyYSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y4PC9zcGFuPjwvZGl2PjxiPlNha3VyYTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iRHVuZSI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7im7DvuI88L3NwYW4+PC9kaXY+PGI+RHVuZTwvYj48L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJtYXB0aWxlIiBkYXRhLW1hcD0iQ2FseXBzbyI+PGRpdiBjbGFzcz0ibWFwYXJ0Ij48c3Bhbj7wn4y0PC9zcGFuPjwvZGl2PjxiPkNhbHlwc288L2I+PC9idXR0b24+PC9kaXY+PC9kaXY+CiA8L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXJhdGluZyI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0KDQtdC50YLQuNC90LMg0LjQs9GA0L7QutC+0LI8L2gyPjxwPtCU0L7QutCw0LbQuCDRgdCy0L7QuSDRg9GA0L7QstC10L3RjC4g0J/QvtC00L3QuNC80LDQudGB0Y8g0LIg0YLQsNCx0LvQuNGG0LUg0LvQuNC00LXRgNC+0LIuPC9wPjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNFQVNPTiAwMzwvc3Bhbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYnMiIGlkPSJyYXRpbmdUYWJzIj48YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdGFiPSJzZWFzb24iPtCh0LXQt9C+0L0gMDM8L2J1dHRvbj48YnV0dG9uIGRhdGEtdGFiPSJhbGwiPtCX0LAg0LLRgdGRINCy0YDQtdC80Y88L2J1dHRvbj48L2Rpdj4KIDxkaXYgY2xhc3M9InRhYmxld3JhcCI+PHRhYmxlIGNsYXNzPSJ0YWJsZSI+PHRoZWFkPjx0cj48dGg+IzwvdGg+PHRoPtCY0JPQoNCe0Jo8L3RoPjx0aD5FTE88L3RoPjx0aD7Qn9Ce0JHQldCU0Ks8L3RoPjx0aD5XSU4gUkFURTwvdGg+PC90cj48L3RoZWFkPjx0Ym9keSBpZD0icmF0aW5nUm93cyI+PC90Ym9keT48L3RhYmxlPjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSI+0KLQsNCx0LvQuNGG0LAg0YHQvtC00LXRgNC20LjRgiDQtNC10LzQvtC90YHRgtGA0LDRhtC40L7QvdC90YvQtSDQtNCw0L3QvdGL0LUuINCf0L7QtNC60LvRjtGH0LggYmFja2VuZCwg0YfRgtC+0LHRiyDQvtGC0L7QsdGA0LDQttCw0YLRjCDRgNC10LDQu9GM0L3Ri9C5INGA0LXQudGC0LjQvdCzINC40LPRgNC+0LrQvtCyLjwvcD4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXBhc3MiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPlNUQU5EUE9JTlQgUGFzczwvaDI+PHA+0KHQtdC30L7QvSAwMyDCtyDQmNCz0YDQsNC5LCDQt9Cw0YDQsNCx0LDRgtGL0LLQsNC5IFhQLCDQt9Cw0LHQuNGA0LDQuSDQvdCw0LPRgNCw0LTRiy48L3A+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+0JDQmtCi0JjQktCV0J08L3NwYW4+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHBhc3NjYXJkIj48ZGl2IGNsYXNzPSJwYXNzLXRvcCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQn9Cj0KLQrCDQmiDQrdCb0JjQotCVPC9kaXY+PGgyIHN0eWxlPSJtYXJnaW46OHB4IDA7Zm9udC1zaXplOjI4cHgiPtCh0LXQt9C+0L3QvdGL0LUg0L3QsNCz0YDQsNC00Ys8L2gyPjxwPtCX0LDRgNCw0LHQsNGC0YvQstCw0Lkg0L7Qv9GL0YIg0LfQsCDQv9C+0LTRgtCy0LXRgNC20LTRkdC90L3Ri9C1INC80LDRgtGH0LguPC9wPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtbGV2ZWwiIHN0eWxlPSJmb250LXNpemU6NDZweCIgaWQ9InBhc3NMZXZlbEJpZyI+MDE8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJwcm9ncmVzcyI+PHNwYW4gaWQ9InBhc3NQcm9ncmVzc0JpZyI+PC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InBhc3MtZm9vdCI+PHNwYW4gaWQ9InBhc3NYUEJpZyI+MTIwIC8gMzAwIFhQPC9zcGFuPjxzcGFuPtCj0YDQvtCy0LXQvdGMIDEg0LjQtyAzMDwvc3Bhbj48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0J3QsNCz0YDQsNC00Ysg0YHQtdC30L7QvdCwPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPjMg0J/QoNCV0JTQnNCV0KLQkDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0cyI+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfjpbvuI88L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0J3QvtCy0L7QsdGA0LDQvdC10YY8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMSDCtyDQn9C+0LvRg9GH0LXQvdC+PC9zbWFsbD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGNsYWltIiBzdHlsZT0ibWFyZ2luLXRvcDoxMnB4O3dpZHRoOjEwMCU7Zm9udC1zaXplOjEwcHgiIGRhdGEtcmV3YXJkPSLQndC+0LLQvtCx0YDQsNC90LXRhiI+0J/QvtC70YPRh9C10L3QvjwvYnV0dG9uPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyN3B4Ij7wn5SlPC9kaXY+PHN0cm9uZyBzdHlsZT0iZm9udC1zaXplOjE1cHgiPtCd0LAg0L/QvtC00YrRkdC80LU8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgNTwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0J3QsCDQv9C+0LTRitGR0LzQtSI+0J/RgNC+0LLQtdGA0LjRgtGMPC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBzdHlsZT0iZm9udC1zaXplOjI3cHgiPvCfkZE8L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MTVweCI+0K3Qu9C40YLQsDwvc3Ryb25nPjxzbWFsbD7Qo9GA0L7QstC10L3RjCAxMDwvc21hbGw+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBjbGFpbSIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlO2ZvbnQtc2l6ZToxMHB4IiBkYXRhLXJld2FyZD0i0K3Qu9C40YLQsCI+0JfQsNCx0LvQvtC60LjRgNC+0LLQsNC90L48L2J1dHRvbj48L2Rpdj48L2Rpdj48L2Rpdj4KPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9Im1hcmdpbi10b3A6MTZweCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QktCw0LvRjtGC0L3Ri9C1INC90LDQs9GA0LDQtNGLIFNUQU5EUE9JTlQ8L2gzPjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlBSRU1JVU08L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7Ql9Cw0LHQuNGA0LDQuSBTUCDQt9CwINC00L7RgdGC0LjQs9C90YPRgtGL0LUg0YPRgNC+0LLQvdC4LiDQmtCw0LbQtNCw0Y8g0L3QsNCz0YDQsNC00LAg0L3QsNGH0LjRgdC70Y/QtdGC0YHRjyDQvdCwINGB0LXRgNCy0LXRgNC90YvQuSDQsdCw0LvQsNC90YEg0YLQvtC70YzQutC+INC+0LTQuNC9INGA0LDQty48L3A+PGRpdiBjbGFzcz0ic3RhdHMiPgo8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzcC1jb2luIGxhcmdlIj5TUDwvZGl2PjxzdHJvbmc+KzUwIFNQPC9zdHJvbmc+PHNtYWxsPtCj0YDQvtCy0LXQvdGMIDI8L3NtYWxsPjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgY2xhaW1TUCIgZGF0YS1sZXZlbD0iMiIgc3R5bGU9Im1hcmdpbi10b3A6MTJweDt3aWR0aDoxMDAlIj7Ql9Cw0LHRgNCw0YLRjDwvYnV0dG9uPjwvZGl2Pgo8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzcC1jb2luIGxhcmdlIj5TUDwvZGl2PjxzdHJvbmc+KzEwMCBTUDwvc3Ryb25nPjxzbWFsbD7Qo9GA0L7QstC10L3RjCA3PC9zbWFsbD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IGNsYWltU1AiIGRhdGEtbGV2ZWw9IjciIHN0eWxlPSJtYXJnaW4tdG9wOjEycHg7d2lkdGg6MTAwJSI+0JfQsNCx0YDQsNGC0Yw8L2J1dHRvbj48L2Rpdj4KPGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBjbGFzcz0ic3AtY29pbiBsYXJnZSI+U1A8L2Rpdj48c3Ryb25nPisxNTAgU1A8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMTU8L3NtYWxsPjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgY2xhaW1TUCIgZGF0YS1sZXZlbD0iMTUiIHN0eWxlPSJtYXJnaW4tdG9wOjEycHg7d2lkdGg6MTAwJSI+0JfQsNCx0YDQsNGC0Yw8L2J1dHRvbj48L2Rpdj4KPGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBjbGFzcz0ic3AtY29pbiBsYXJnZSI+U1A8L2Rpdj48c3Ryb25nPisyNTAgU1A8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMjU8L3NtYWxsPjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgY2xhaW1TUCIgZGF0YS1sZXZlbD0iMjUiIHN0eWxlPSJtYXJnaW4tdG9wOjEycHg7d2lkdGg6MTAwJSI+0JfQsNCx0YDQsNGC0Yw8L2J1dHRvbj48L2Rpdj4KPGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBjbGFzcz0ic3AtY29pbiBsYXJnZSI+U1A8L2Rpdj48c3Ryb25nPiszMDAgU1A8L3N0cm9uZz48c21hbGw+0KPRgNC+0LLQtdC90YwgMzA8L3NtYWxsPjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgY2xhaW1TUCIgZGF0YS1sZXZlbD0iMzAiIHN0eWxlPSJtYXJnaW4tdG9wOjEycHg7d2lkdGg6MTAwJSI+0JfQsNCx0YDQsNGC0Yw8L2J1dHRvbj48L2Rpdj4KPC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7QotGA0LXQsdGD0LXRgtGB0Y8g0L/QvtC00YLQstC10YDQttC00ZHQvdC90YvQuSBQcmVtaXVtIFBhc3MuINCd0LDQs9GA0LDQtNCwINC90LUg0LzQvtC20LXRgiDQsdGL0YLRjCDQvdCw0YfQuNGB0LvQtdC90LAg0L/QvtCy0YLQvtGA0L3Qvi48L3A+PC9kaXY+XG48L3NlY3Rpb24+XG5cblxuPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLXN0b3JlIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QnNCw0LPQsNC30LjQvSBTVEFORFBPSU5UPC9oMj48cD7Qn9C+0L/QvtC70L3Rj9C5INCx0LDQu9Cw0L3RgSwg0L7RgtC60YDRi9Cy0LDQuSDQutC+0YHQvNC10YLQuNC60YMg0Lgg0YHQvtCx0LjRgNCw0Lkg0YHQstC+0Lkg0YHRgtC40LvRjC48L3A+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+U1AgV0FMTEVUPC9zcGFuPjwvZGl2PgogPGRpdiBjbGFzcz0id2FsbGV0LWhlcm8iPjxkaXY+PGRpdiBjbGFzcz0iZXllYnJvdyI+0KLQktCe0Jkg0JHQkNCb0JDQndChPC9kaXY+PGgyPjxzcGFuIGlkPSJ3YWxsZXRQb2ludHMiPuKAlDwvc3Bhbj4gPHNtYWxsPlNQPC9zbWFsbD48L2gyPjxwPlNUQU5EUE9JTlQgUG9pbnRzIMK3INCy0LDQu9GO0YLQsCDQv9C70LDRgtGE0L7RgNC80Ys8L3A+PC9kaXY+PGRpdiBjbGFzcz0id2FsbGV0LW1hcmsiPlNQPC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCIgc3R5bGU9Im1hcmdpbi10b3A6MjJweCI+PGRpdj48aDIgc3R5bGU9ImZvbnQtc2l6ZToyMXB4Ij5Hb2xkINC00LvRjyBTdGFuZG9mZiAyPC9oMj48cD7Qn9Cw0LrQtdGC0Ysg0YEg0LLRi9Cz0L7QtNC+0Lkg0LfQsCDQsdC+0LvRjNGI0LjQuSDQvtCx0YrRkdC8LjwvcD48L2Rpdj48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCm0LXQvdGLLdC/0YDQtdC00LvQvtC20LXQvdC40Y8gwrcgRVVSPC9zcGFuPjwvZGl2PgogPGRpdiBjbGFzcz0iZ29sZC1ncmlkIj4KICA8YXJ0aWNsZSBjbGFzcz0iZ29sZC1jYXJkIj48ZGl2IGNsYXNzPSJnb2xkLWljb24iPuKcpjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPtCh0KLQkNCg0KI8L3NwYW4+PGgzPjUwMCBHb2xkPC9oMz48cD7Qn9C10YDQstC+0LUg0L/QvtC/0L7Qu9C90LXQvdC40LU8L3A+PGRpdiBjbGFzcz0iZ29sZC1wcmljZSI+4oKsNCw5OTwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IGdvbGQtYnV5IiBkYXRhLXBhY2s9IjUwMCI+0JLRi9Cx0YDQsNGC0Ywg0L/QsNC60LXRgjwvYnV0dG9uPjwvYXJ0aWNsZT4KICA8YXJ0aWNsZSBjbGFzcz0iZ29sZC1jYXJkIGZlYXR1cmVkIj48ZGl2IGNsYXNzPSJnb2xkLXJpYmJvbiI+0J/QntCf0KPQm9Cv0KDQndCr0Jk8L2Rpdj48ZGl2IGNsYXNzPSJnb2xkLWljb24iPuKcpuKcpjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPtCS0KvQk9Ce0JTQndCV0JU8L3NwYW4+PGgzPjEgMDAwIEdvbGQ8L2gzPjxwPtCR0L7Qu9GM0YjQtSDQt9C+0LvQvtGC0LAg0LfQsCDQv9C+0LrRg9C/0LrRgzwvcD48ZGl2IGNsYXNzPSJnb2xkLXByaWNlIj7igqw4LDk5PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkgZ29sZC1idXkiIGRhdGEtcGFjaz0iMTAwMCI+0JLRi9Cx0YDQsNGC0Ywg0L/QsNC60LXRgjwvYnV0dG9uPjwvYXJ0aWNsZT4KICA8YXJ0aWNsZSBjbGFzcz0iZ29sZC1jYXJkIj48ZGl2IGNsYXNzPSJnb2xkLWljb24iPuKcpuKcpuKcpjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPtCc0JDQmtCh0JjQnNCj0Jw8L3NwYW4+PGgzPjMgMDAwIEdvbGQ8L2gzPjxwPtCU0LvRjyDQsdC+0LvRjNGI0LjRhSDQv9C70LDQvdC+0LI8L3A+PGRpdiBjbGFzcz0iZ29sZC1wcmljZSI+4oKsMjQsOTk8L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSBnb2xkLWJ1eSIgZGF0YS1wYWNrPSIzMDAwIj7QktGL0LHRgNCw0YLRjCDQv9Cw0LrQtdGCPC9idXR0b24+PC9hcnRpY2xlPgogPC9kaXY+CiA8cCBjbGFzcz0iZm9vdG5vdGUiPtCS0LDQttC90L46INGN0YLQviDQstC40YLRgNC40L3QsCDQuCDRgNC10LrQvtC80LXQvdC00L7QstCw0L3QvdGL0LUg0YbQtdC90YssINCwINC90LUg0L/QvtC00LrQu9GO0YfRkdC90L3QsNGPINC+0L/Qu9Cw0YLQsC4g0JfQvtC70L7RgtC+INC90LUg0L3QsNGH0LjRgdC70Y/QtdGC0YHRjyDQsNCy0YLQvtC80LDRgtC40YfQtdGB0LrQuDog0L/QtdGA0LXQtCDQt9Cw0L/Rg9GB0LrQvtC8INC/0YDQvtC00LDQtiDQvdGD0LbQvdC+INC/0L7QtNC60LvRjtGH0LjRgtGMINC/0LvQsNGC0ZHQttC90L7Qs9C+INC/0YDQvtCy0LDQudC00LXRgNCwINC4INC70LXQs9Cw0LvRjNC90YvQuSDRgdC/0L7RgdC+0LEg0L/QtdGA0LXQtNCw0YfQuCBHb2xkLiBTVEFORFBPSU5UIFBvaW50cyDQuCDQuNCz0YDQvtCy0L7QtSBHb2xkIOKAlCDRgNCw0LfQvdGL0LUg0LLQsNC70Y7RgtGLLjwvcD4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JrQvtGB0LzQtdGC0LjQutCwIFNUQU5EUE9JTlQ8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBkYXRhLXBhZ2U9ImNvc21ldGljcyI+0J7RgtC60YDRi9GC0Ywg0LrQsNGC0LDQu9C+0LMg4oaSPC9idXR0b24+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QoNCw0LzQutC4LCDRgtC40YLRg9C70Ysg0Lgg0LrQsNGA0YLQvtGH0LrQuCDQv9GA0L7RhNC40LvRjyDQt9CwIFNQLjwvcD48L2Rpdj4KPC9zZWN0aW9uPgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtdG91cm5hbWVudHMiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCi0YPRgNC90LjRgNGLPC9oMj48cD7QmtC70LDQvdC+0LLRi9C1INGB0LXRgtC60LgsINC+0YLQsdC+0YDQvtGH0L3Ri9C1INC80LDRgtGH0Lgg0Lgg0L/Rg9GC0Ywg0Log0YLRgNC+0YTQtdGP0LwuPC9wPjwvZGl2PjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiPkNMQU4gQ09NUEVUSVRJT048L3NwYW4+PC9kaXY+CiA8ZGl2IGNsYXNzPSJ0b3VybmFtZW50LWhlcm8iPjxkaXYgY2xhc3M9ImV5ZWJyb3ciPlNUQU5EUE9JTlQgQVJFTkE8L2Rpdj48aDI+0JjQs9GA0LDQuSDQt9CwINC60LvQsNC9Ljxicj48c3Bhbj7Ql9Cw0LHQuNGA0LDQuSDRgtC40YLRg9C7Ljwvc3Bhbj48L2gyPjxwPtCi0YPRgNC90LjRgNC90LDRjyDRgdC40YHRgtC10LzQsCDQuNGB0L/QvtC70YzQt9GD0LXRgiDQutC70LDQvdGLINC4IENsYW4gV2FycyDQuNC3INGB0LXRgNCy0LXRgNC90L7Qs9C+IEFQSS4g0J3QsNGH0L3QuCDRgSDQstGL0LfQvtCy0LAg0YHQvtC/0LXRgNC90LjQutGDINCyINGA0LDQt9C00LXQu9C1IMKr0JrQu9Cw0L3Ri8K7LjwvcD48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgZGF0YS1wYWdlPSJjbGFucyI+0J/QtdGA0LXQudGC0Lgg0LogQ2xhbiBXYXJzIOKGlzwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0ic3RhdHMgdG91cm5hbWVudC1zdGF0cyI+PGRpdiBjbGFzcz0ic3RhdCI+PGxhYmVsPtCk0J7QoNCc0JDQojwvbGFiZWw+PHN0cm9uZz41djU8L3N0cm9uZz48c21hbGw+0JrQvtC80LDQvdC00L3Ri9C5PC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0KHQldCg0JjQmDwvbGFiZWw+PHN0cm9uZz5CTzE8L3N0cm9uZz48c21hbGw+Q2xhbiBXYXIgbG9iYnk8L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQiPjxsYWJlbD7QoNCV0JfQo9Cb0KzQotCQ0KLQqzwvbGFiZWw+PHN0cm9uZz5SZXZpZXc8L3N0cm9uZz48c21hbGw+0J/RgNC+0LLQtdGA0LrQsCDQvNC+0LTQtdGA0LDRgtC+0YDQvtC8PC9zbWFsbD48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JrQsNC6INC90LDRh9Cw0YLRjCDRgtGD0YDQvdC40YDQvdGL0Lkg0L/Rg9GC0Yw8L2gzPjxzcGFuIGNsYXNzPSJtdXRlZCBzbWFsbCI+MyDRiNCw0LPQsDwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pmiPC9kaXY+PGRpdj48Yj4xLiDQodC+0LfQtNCw0Lkg0LjQu9C4INCy0YHRgtGD0L/QuCDQsiDQutC70LDQvTwvYj48cD7QodC+0LHQtdGA0Lgg0YHQvtGB0YLQsNCyINC4INC90LDQt9C90LDRh9GMINC+0YLQstC10YLRgdGC0LLQtdC90L3Ri9GFINC30LAg0LLRi9C30L7QstGLLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+4pqUPC9kaXY+PGRpdj48Yj4yLiDQntGC0L/RgNCw0LLRjCBDbGFuIFdhcjwvYj48cD7QktGL0LHQtdGA0Lgg0LrQu9Cw0L0g0YHQvtC/0LXRgNC90LjQutCwLCDQtNC+0LbQtNC40YHRjCDQv9GA0LjQvdGP0YLQuNGPINC4INGB0YvQs9GA0LDQuSDRgdC10YDQuNGOLjwvcD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJub3RpY2UiPjxkaXYgY2xhc3M9Im5vdGljZWljbyI+8J+PhjwvZGl2PjxkaXY+PGI+My4g0J7RgtC/0YDQsNCy0Ywg0YDQtdC30YPQu9GM0YLQsNGCPC9iPjxwPtCg0LXQt9GD0LvRjNGC0LDRgiDQv9GA0L7RhdC+0LTQuNGCINC80L7QtNC10YDQsNGG0LjRjjsg0L/QvtGB0LvQtSDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjyDQvNC+0LbQvdC+INGD0YfQuNGC0YvQstCw0YLRjCDQtdCz0L4g0LIg0YLRg9GA0L3QuNGA0L3QvtC5INGC0LDQsdC70LjRhtC1LjwvcD48L2Rpdj48L2Rpdj48L2Rpdj4KIDxwIGNsYXNzPSJmb290bm90ZSI+0J/QvtC70L3QvtGG0LXQvdC90LDRjyDQsNCy0YLQvtC80LDRgtC40YfQtdGB0LrQsNGPINGB0LXRgtC60LAg0YLRg9GA0L3QuNGA0L7Qsiwg0YDQtdCz0LjRgdGC0YDQsNGG0LjRjyDRgdC+0YHRgtCw0LLQvtCyINC4INC/0YDQuNC30L7QstGL0LUg0L3QsNGH0LjRgdC70LXQvdC40Y8g0YLRgNC10LHRg9GO0YIg0L7RgtC00LXQu9GM0L3QvtC5INGC0YPRgNC90LjRgNC90L7QuSDQvNC+0LTQtdC70Lgg0LIgYmFja2VuZDsg0YHQtdC50YfQsNGBINGN0YLQvtGCINGA0LDQt9C00LXQuyDRgdCy0Y/Qt9Cw0L0g0YEg0YHRg9GJ0LXRgdGC0LLRg9GO0YnQuNC80Lgg0LrQu9Cw0L3QsNC80Lgg0LggQ2xhbiBXYXJzINCx0LXQtyDQstGL0LTRg9C80LDQvdC90YvRhSDRgtGD0YDQvdC40YDQvtCyLjwvcD4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLWNsYW5zIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QmtC70LDQvdGLPC9oMj48cD7QodC+0LHQtdGA0Lgg0LrQvtC80LDQvdC00YMuINCX0LDRidC40YnQsNC5INGB0LLQvtC5INGC0LXQsy4g0J/QvtCx0LXQttC00LDQuSDQsiBDbGFuIFdhcnMuPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY3JlYXRlQ2xhbkJ0biI+77yLINCh0L7Qt9C00LDRgtGMINC60LvQsNC9PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjbGFuaGVybyI+PGRpdiBjbGFzcz0iY2xhbmxvZ28iPvCfkLo8L2Rpdj48ZGl2IGNsYXNzPSJncm93Ij48ZGl2IGNsYXNzPSJleWVicm93Ij7QotCS0J7QmSDQmtCb0JDQnTwvZGl2PjxoMyBpZD0iY2xhbk5hbWUiPkZyb3N0IFdvbHZlcyA8c3BhbiBpZD0iY2xhblRhZyIgY2xhc3M9Im11dGVkIHNtYWxsIj4jRlc8L3NwYW4+PC9oMz48cD4xMiDRg9GH0LDRgdGC0L3QuNC60L7QsiDCtyAxOTgwIENsYW4gRUxPPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkiIGlkPSJjbGFuRGV0YWlscyI+0J/QvtC00YDQvtCx0L3QtdC1PC9idXR0b24+PC9kaXY+CiA8ZGl2IGNsYXNzPSJsYXlvdXQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0KHQvtGB0YLQsNCyINC60LvQsNC90LA8L2gzPjxzcGFuIGNsYXNzPSJtdXRlZCBzbWFsbCI+MTIgLyAyMDwvc3Bhbj48L2Rpdj48ZGl2IGlkPSJjbGFuTWVtYmVycyIgY2xhc3M9ImxlYWRlcmxpc3QiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+Q2xhbiBXYXJzPC9oMz48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj5MSVZFIExPQkJZPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWFyb3VuZDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE4cHggMCI+PGRpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MzVweCI+8J+boe+4jzwvZGl2PjxiIGlkPSJ3YXJPd25OYW1lIj7QotCS0J7QmSDQmtCb0JDQnTwvYj48L2Rpdj48c3Ryb25nIHN0eWxlPSJmb250LXNpemU6MjFweDtjb2xvcjojZmY5ODRkIj5WUzwvc3Ryb25nPjxkaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjM1cHgiPuKalO+4jzwvZGl2PjxiPtCh0J7Qn9CV0KDQndCY0Jo8L2I+PC9kaXY+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIiBpZD0id2FyU3RhdHVzVGV4dCI+0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LrQu9Cw0L3RgyDQv9C+INC10LPQviBJRCDQuNC70Lgg0L7RgtC60YDQvtC5INC40YHRgtC+0YDQuNGOLjwvcD48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0id2FyQnV0dG9uIiBzdHlsZT0id2lkdGg6MTAwJSI+0J7RgtC60YDRi9GC0Ywg0LjRgdGC0L7RgNC40Y4gQ2xhbiBXYXJzPC9idXR0b24+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIHdhci1sb2JieS1jYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4O2JvcmRlci1jb2xvcjojNTEzMjFmO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzMGRlZywjMWQxNzE0LCMxMTE2MWMpIj48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGRpdj48ZGl2IGNsYXNzPSJleWVicm93Ij5DTEFOIFdBUiBPUEVSQVRJT05TPC9kaXY+PGgzIHN0eWxlPSJtYXJnaW4tdG9wOjVweCI+0JvQvtCx0LHQuCDQutC70LDQvdC+0LLQvtC5INCy0L7QudC90Ys8L2gzPjwvZGl2PjxzcGFuIGNsYXNzPSJtYy1zdGF0dXMiPjV2NSDCtyBCTzE8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntGC0L/RgNCw0LLRjCDQstGL0LfQvtCyINC00YDRg9Cz0L7QvNGDINC60LvQsNC90YMuINCf0L7RgdC70LUg0L/RgNC40L3Rj9GC0LjRjyDQv9C+0Y/QstC40YLRgdGPINC90LDRgdGC0L7Rj9GJ0LXQtSDQu9C+0LHQsdC4INGBINGB0L7RgdGC0LDQstCw0LzQuCDQuCDQvtGC0L/RgNCw0LLQutC+0Lkg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L3QsCDQvNC+0LTQtdGA0LDRhtC40Y4uPC9wPjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCa0JvQkNCdLdCh0J7Qn9CV0KDQndCY0Jo8L2xhYmVsPjxzZWxlY3QgaWQ9Indhck9wcG9uZW50SWQiPjxvcHRpb24gdmFsdWU9IiI+0JfQsNCz0YDRg9C20LDQtdC8INGB0L/QuNGB0L7QuiDQutC70LDQvdC+0LLigKY8L29wdGlvbj48L3NlbGVjdD48L2Rpdj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPklEINCi0JXQmtCj0KnQldCZINCS0J7QmdCd0Ks8L2xhYmVsPjxpbnB1dCBpZD0id2FySWRJbnB1dCIgcGxhY2Vob2xkZXI9ItCf0L7Rj9Cy0LjRgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LfQvtCy0LAiPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iY2hhbGxlbmdlV2FyQnRuIj7impQg0J7RgtC/0YDQsNCy0LjRgtGMINCy0YvQt9C+0LI8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0ibG9hZFdhckxvYmJ5QnRuIj7QntGC0LrRgNGL0YLRjCDQu9C+0LHQsdC4PC9idXR0b24+PC9kaXY+PGRpdiBpZD0id2FyTG9iYnlDb250ZW50IiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibWMtbXV0ZWQiPtCf0L7QutCwINC90LXRgiDQsNC60YLQuNCy0L3QvtCz0L4g0LvQvtCx0LHQuC4g0KHQvtC30LTQsNC5INCy0YvQt9C+0LIg0LjQu9C4INCy0YHRgtCw0LLRjCBJRCDRgdGD0YnQtdGB0YLQstGD0Y7RidC10Lkg0LLQvtC50L3Riy48L3A+PC9kaXY+PC9kaXY+Cjwvc2VjdGlvbj4KCjxzZWN0aW9uIGNsYXNzPSJzZWN0aW9uIiBpZD0icGFnZS1mcmllbmRzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QlNGA0YPQt9GM0Y88L2gyPjxwPtCf0YDQuNCz0LvQsNGI0LDQuSDQuNCz0YDQvtC60L7QsiDQsiDQutC+0LzQsNC90LTRgyDQuCDQstC80LXRgdGC0LUg0L/QvtC00L3QuNC80LDQuSBFTE8uPC9wPjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWRkRnJpZW5kQnRuIj7vvIsg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7Qn9Ce0JjQodCaINCf0J4g0J3QmNCa0J3QldCZ0JzQozwvbGFiZWw+PGlucHV0IGlkPSJmcmllbmRTZWFyY2giIHBsYWNlaG9sZGVyPSLQndCw0L/RgNC40LzQtdGALCBzaDF6byI+PC9kaXY+PGRpdiBjbGFzcz0ibGVhZGVybGlzdCIgaWQ9ImZyaWVuZHNMaXN0Ij48L2Rpdj48L2Rpdj4KPC9zZWN0aW9uPgoKPHNlY3Rpb24gY2xhc3M9InNlY3Rpb24iIGlkPSJwYWdlLW5vdGlmaWNhdGlvbnMiPgogPGRpdiBjbGFzcz0ic2VjdGlvbmhlYWQiPjxkaXY+PGgyPtCj0LLQtdC00L7QvNC70LXQvdC40Y88L2gyPjxwPtCc0LDRgtGH0LgsINC/0YDQuNCz0LvQsNGI0LXQvdC40Y8g0Lgg0YHQvtCx0YvRgtC40Y8g0L/Qu9Cw0YLRhNC+0YDQvNGLLjwvcD48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tc2Vjb25kYXJ5IiBpZD0icmVhZEFsbCI+0J/RgNC+0YfQuNGC0LDRgtGMINCy0YHQtTwvYnV0dG9uPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCIgaWQ9Im5vdGljZUxpc3QiPjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtcHJvZmlsZSI+CiA8ZGl2IGNsYXNzPSJzZWN0aW9uaGVhZCI+PGRpdj48aDI+0J/RgNC+0YTQuNC70Ywg0LjQs9GA0L7QutCwPC9oMj48cD7QotCy0L7RjyDRgdGC0LDRgtC40YHRgtC40LrQsCwg0YDQtdC50YLQuNC90LMg0Lgg0LjQs9GA0L7QstC+0Lkg0L/RgNC+0LPRgNC10YHRgS48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJwcm9maWxlaGVybyI+PGRpdiBjbGFzcz0icHJvZmlsZWF2YXRhciI+4pmfPC9kaXY+PGRpdj48aDMgaWQ9InByb2ZpbGVOYW1lIj5zdGFuZHBvaW50X3BsYXllcjwvaDM+PHAgaWQ9InByb2ZpbGVJZCI+SUQ6IGRlbW9fMDAxPC9wPjxzcGFuIGNsYXNzPSJwaWxsIiBpZD0icHJvZmlsZVN0YXR1cyI+4pePINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9InByb2ZpbGVlbG8iPjxzbWFsbD7QotCV0JrQo9Cp0JjQmSBFTE88L3NtYWxsPjxzdHJvbmcgaWQ9InByb2ZpbGVFbG8iPjEwMDA8L3N0cm9uZz48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9InN0YXRzIj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+0JzQkNCi0KfQmDwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZU1hdGNoZXMiPjA8L3N0cm9uZz48c21hbGw+0JLRgdC10LPQviDRgdGL0LPRgNCw0L3Qvjwvc21hbGw+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdCI+PGxhYmVsPtCf0J7QkdCV0JTQqzwvbGFiZWw+PHN0cm9uZyBpZD0icHJvZmlsZVdpbnMiPjA8L3N0cm9uZz48c21hbGw+0J/QvtC00YLQstC10YDQttC00LXQvdC+PC9zbWFsbD48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0Ij48bGFiZWw+Sy9EPC9sYWJlbD48c3Ryb25nIGlkPSJwcm9maWxlS0QiPuKAlDwvc3Ryb25nPjxzbWFsbD7Qo9Cx0LjQudGB0YLQstCwIC8g0YHQvNC10YDRgtC4PC9zbWFsbD48L2Rpdj48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJtYXJnaW4tdG9wOjE2cHgiPjxkaXYgY2xhc3M9ImNhcmRoZWFkIj48aDM+0JTQsNC90L3Ri9C1INC/0YDQvtGE0LjQu9GPPC9oMz48c3BhbiBjbGFzcz0ibXV0ZWQgc21hbGwiPtCh0JXQoNCS0JXQoCAvINCf0KDQntCk0JjQm9CsPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9ImZvcm1ncmlkIj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCd0JjQmtCd0JXQmdCcPC9sYWJlbD48aW5wdXQgaWQ9ImVkaXROYW1lIiBtYXhsZW5ndGg9IjI0IiB2YWx1ZT0ic3RhbmRwb2ludF9wbGF5ZXIiPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JjQk9Cg0J7QktCe0JkgSUQ8L2xhYmVsPjxpbnB1dCBpZD0iZWRpdEdhbWVJZCIgbWF4bGVuZ3RoPSIzMiIgdmFsdWU9ImRlbW9fMDAxIj48L2Rpdj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSIgaWQ9InNhdmVQcm9maWxlIj7QodC+0YXRgNCw0L3QuNGC0Ywg0LjQt9C80LXQvdC10L3QuNGPPC9idXR0b24+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9GA0L7RhNC40LvRjCDRgdC+0YXRgNCw0L3Rj9C10YLRgdGPINC90LAg0YHQtdGA0LLQtdGA0LUsINC10YHQu9C4IEFQSSDQv9C+0LTQutC70Y7Rh9GR0L0g0LggVGVsZWdyYW0gSUQg0LTQvtGB0YLRg9C/0LXQvS48L3A+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCg0LDRgdGI0LjRgNC10L3QvdCw0Y8g0YHRgtCw0YLQuNGB0YLQuNC60LA8L2gzPjxidXR0b24gY2xhc3M9InRleHRsaW5rIiBpZD0icmVmcmVzaEV4dHJhU3RhdHMiPtCe0LHQvdC+0LLQuNGC0Ywg4oa7PC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwtZ3JpZCI+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7Qn9Ce0JHQldCU0KsgJTwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFXaW5yYXRlIj7igJQ8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCf0J7QoNCQ0JbQldCd0JjQrzwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFMb3NzZXMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCd0JjQp9Cs0Jg8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhRHJhd3MiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCj0JHQmNCZ0KHQotCS0JA8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhS2lsbHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0JzQldCg0KLQmDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFEZWF0aHMiPjA8L3N0cm9uZz48L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LWRldGFpbCI+PHNtYWxsPtCh0KDQldCU0J3QmNCZIEsvRDwvc21hbGw+PHN0cm9uZyBpZD0iZXh0cmFLRCI+4oCUPC9zdHJvbmc+PC9kaXY+PGRpdiBjbGFzcz0ic3RhdC1kZXRhaWwiPjxzbWFsbD7QodCr0JPQoNCQ0J3QniDQnNCQ0KLQp9CV0Jk8L3NtYWxsPjxzdHJvbmcgaWQ9ImV4dHJhTWF0Y2hlcyI+MDwvc3Ryb25nPjwvZGl2PjxkaXYgY2xhc3M9InN0YXQtZGV0YWlsIj48c21hbGw+0KLQldCa0KPQqdCY0JkgRUxPPC9zbWFsbD48c3Ryb25nIGlkPSJleHRyYUVsbyI+MTAwMDwvc3Ryb25nPjwvZGl2PjwvZGl2PjxwIGNsYXNzPSJmb290bm90ZSIgaWQ9ImV4dHJhU3RhdHNOb3RlIj7QlNCw0L3QvdGL0LUg0LHRg9C00YPRgiDQt9Cw0LPRgNGD0LbQtdC90Ysg0LjQtyBBUEkg0L/QvtGB0LvQtSDQv9C+0LTQutC70Y7Rh9C10L3QuNGPINC/0YDQvtGE0LjQu9GPLjwvcD48L2Rpdj4KIDxkaXYgY2xhc3M9ImNhcmQiIGlkPSJtb2RlcmF0b3JDYXJkIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweDtib3JkZXItY29sb3I6IzcwNDAxZjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNDVkZWcsIzIxMTcwZiwjMTExNjFjKSI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxkaXY+PGRpdiBjbGFzcz0iZXllYnJvdyI+TU9ERVJBVElPTiBDRU5URVI8L2Rpdj48aDMgc3R5bGU9Im1hcmdpbi10b3A6NXB4Ij7QkNC00LzQuNC9LdC/0LDQvdC10LvRjCDQvNC+0LTQtdGA0LDRgtC+0YDQsDwvaDM+PC9kaXY+PHNwYW4gY2xhc3M9ImJhZGdlLW9yYW5nZSI+QHl1a3plbl9icm88L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QntC00L7QsdGA0LXQvdC40LUg0L3QsNGH0LjRgdC70Y/QtdGCIEVMTyDQuCDQt9Cw0L/QuNGB0YvQstCw0LXRgiDRgdGC0LDRgtC40YHRgtC40LrRgyDQsiDQv9GA0L7RhNC40LvQuCDRg9GH0LDRgdGC0L3QuNC60L7Qsi4g0J7RgtC60LDQtyDQvdC1INC80LXQvdGP0LXRgiBFTE8g0Lgg0YHRgtCw0YLQuNGB0YLQuNC60YMuPC9wPjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JrQm9Cu0Kcg0JzQntCU0JXQoNCQ0KbQmNCYIChSRVNVTFRTX0FETUlOX1RPS0VOKTwvbGFiZWw+PGlucHV0IGlkPSJtb2RlcmF0b3JUb2tlbiIgdHlwZT0icGFzc3dvcmQiIGF1dG9jb21wbGV0ZT0ib2ZmIiBwbGFjZWhvbGRlcj0i0JLRgdGC0LDQstGMINGB0LXQutGA0LXRgtC90YvQuSDQutC70Y7RhyDRgdC10YDQstC10YDQsCI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJsb2FkTW9kZXJhdGlvbkJ0biI+0JfQsNCz0YDRg9C30LjRgtGMINC80LDRgtGH0Lgg0L3QsCDQv9GA0L7QstC10YDQutGDPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9ImNsZWFyTW9kZXJhdG9yVG9rZW4iPtCe0YfQuNGB0YLQuNGC0Ywg0LrQu9GO0Yc8L2J1dHRvbj48L2Rpdj48ZGl2IGlkPSJtb2RlcmF0aW9uUXVldWUiIHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z2FwOjEycHg7bWFyZ2luLXRvcDoxNHB4Ij48cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCe0YfQtdGA0LXQtNGMINC80L7QtNC10YDQsNGG0LjQuCDQvdC1INC30LDQs9GA0YPQttC10L3QsC48L3A+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLXRvcDoxNnB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCa0YPQsdC60Lgg0Lgg0L3QsNCz0YDQsNC00Ys8L2gzPjxzcGFuIGNsYXNzPSJiYWRnZS1vcmFuZ2UiIGlkPSJ0cm9waHlDb3VudCI+MCDQndCQ0JPQoNCQ0JQ8L3NwYW4+PC9kaXY+PHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QndCw0LPRgNCw0LTRiyDQt9CwINC60LLQsNC70LjRhNC40LrQsNGG0LjQuCDQuCDQuNGC0L7Qs9C+0LLRi9C1INC80LXRgdGC0LAg0LIg0YDQtdC50YLQuNC90LPQtS4g0JrRg9Cx0LrQuCDQvtGC0L7QsdGA0LDQttCw0Y7RgtGB0Y8g0L/QvtGB0LvQtSDQstGL0LTQsNGH0Lgg0YDQtdC30YPQu9GM0YLQsNGC0LAg0L7RgNCz0LDQvdC40LfQsNGC0L7RgNC+0LwuPC9wPjxkaXYgY2xhc3M9InRyb3BoeS1ncmlkIiBpZD0icHJvZmlsZVRyb3BoaWVzIj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J/QvtC60LAg0L3QtdGCINC60YPQsdC60L7Qsi4g0KPRh9Cw0YHRgtCy0YPQuSDQsiDQutCy0LDQu9C40YTQuNC60LDRhtC40Y/RhSwg0YfRgtC+0LHRiyDQv9C+0LvRg9GH0LjRgtGMINC90LDQs9GA0LDQtNGDLjwvZGl2PjwvZGl2PjwvZGl2Pgo8L3NlY3Rpb24+Cgo8c2VjdGlvbiBjbGFzcz0ic2VjdGlvbiIgaWQ9InBhZ2UtY29zbWV0aWNzIj4KIDxkaXYgY2xhc3M9InNlY3Rpb25oZWFkIj48ZGl2PjxoMj7QnNCw0LPQsNC30LjQvSDQutC+0YHQvNC10YLQuNC60Lg8L2gyPjxwPtCe0YTQvtGA0LzQu9C10L3QuNC1IFNUQU5EUE9JTlQgSUQ6INGA0LDQvNC60LgsINGC0LjRgtGD0LvRiyDQuCDQutCw0YDRgtC+0YfQutC4INC/0YDQvtGE0LjQu9GPLjwvcD48L2Rpdj48ZGl2IGNsYXNzPSJiYWRnZS1vcmFuZ2UiPlNQIFBPSU5UUzogPHNwYW4gaWQ9ImNvc21ldGljUG9pbnRzIj7igJQ8L3NwYW4+PC9kaXY+PC9kaXY+CiA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxNHB4Ij48ZGl2IGNsYXNzPSJjYXJkaGVhZCI+PGgzPtCa0LDRgtCw0LvQvtCzPC9oMz48ZGl2IGNsYXNzPSJ0YWJzIiBzdHlsZT0ibWFyZ2luOjAiPjxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS1jb3NtZXRpYy1maWx0ZXI9ImFsbCI+0JLRgdC1PC9idXR0b24+PGJ1dHRvbiBkYXRhLWNvc21ldGljLWZpbHRlcj0iZnJhbWUiPtCg0LDQvNC60Lg8L2J1dHRvbj48YnV0dG9uIGRhdGEtY29zbWV0aWMtZmlsdGVyPSJ0aXRsZSI+0KLQuNGC0YPQu9GLPC9idXR0b24+PGJ1dHRvbiBkYXRhLWNvc21ldGljLWZpbHRlcj0icHJvZmlsZV9jYXJkIj7QmtCw0YDRgtC+0YfQutC4PC9idXR0b24+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0iY29zbWV0aWMtZ3JpZCIgaWQ9ImNvc21ldGljU2hvcEdyaWQiPjxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7Ql9Cw0LPRgNGD0LbQsNC10Lwg0LrQsNGC0LDQu9C+0LMg0LrQvtGB0LzQtdGC0LjQutC44oCmPC9kaXY+PC9kaXY+PHAgY2xhc3M9ImZvb3Rub3RlIj7Qn9C+0LrRg9C/0LrQsCDQtNC+0YHRgtGD0L/QvdCwINC/0L7RgdC70LUg0L/QvtC00LrQu9GO0YfQtdC90LjRjyDQsNCy0YLQvtGA0LjQt9C+0LLQsNC90L3QvtCz0L4g0L/RgNC+0YTQuNC70Y8uINCS0LDQu9GO0YLQsCDQuCDQv9GA0LXQtNC80LXRgtGLINCx0LXRgNGD0YLRgdGPINC40LcgYmFja2VuZCwg0LAg0L3QtSDQuNC3INC00LXQvNC+0L3RgdGC0YDQsNGG0LjQvtC90L3Ri9GFINC30L3QsNGH0LXQvdC40LkuPC9wPjwvZGl2PgogPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxoMz7QnNC+0Y8g0LrQvtC70LvQtdC60YbQuNGPPC9oMz48YnV0dG9uIGNsYXNzPSJ0ZXh0bGluayIgaWQ9InJlZnJlc2hDb3NtZXRpY3MiPtCe0LHQvdC+0LLQuNGC0Ywg4oa7PC9idXR0b24+PC9kaXY+PGRpdiBjbGFzcz0iY29zbWV0aWMtZ3JpZCIgaWQ9Im15Q29zbWV0aWNzR3JpZCI+PGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCS0L7QudC00Lgg0YfQtdGA0LXQtyBUZWxlZ3JhbSwg0YfRgtC+0LHRiyDQt9Cw0LPRgNGD0LfQuNGC0Ywg0LrQvtC70LvQtdC60YbQuNGOLjwvZGl2PjwvZGl2PjwvZGl2Pgo8L3NlY3Rpb24+CjwvbWFpbj4KPC9kaXY+CjxuYXYgY2xhc3M9ImJvdHRvbW5hdiIgaWQ9ImJvdHRvbU5hdiI+PGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLXBhZ2U9ImhvbWUiPjxzcGFuPuKMgjwvc3Bhbj7Qk9C70LDQstC90LDRjzwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJtYXRjaGVzIj48c3Bhbj7impQ8L3NwYW4+0JzQsNGC0YfQuDwvYnV0dG9uPjxidXR0b24gZGF0YS1wYWdlPSJzdG9yZSI+PHNwYW4+4peIPC9zcGFuPtCc0LDQs9Cw0LfQuNC9PC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9InRvdXJuYW1lbnRzIj48c3Bhbj7wn4+GPC9zcGFuPtCi0YPRgNC90LjRgNGLPC9idXR0b24+PGJ1dHRvbiBkYXRhLXBhZ2U9InByb2ZpbGUiPjxzcGFuPuKXiTwvc3Bhbj7Qn9GA0L7RhNC40LvRjDwvYnV0dG9uPjwvbmF2Pgo8ZGl2IGlkPSJ0b2FzdFJvb3QiIGFyaWEtbGl2ZT0icG9saXRlIj48L2Rpdj4KPHNjcmlwdD4KY29uc3QgJD0ocyxyPWRvY3VtZW50KT0+ci5xdWVyeVNlbGVjdG9yKHMpLCQkPShzLHI9ZG9jdW1lbnQpPT5bLi4uci5xdWVyeVNlbGVjdG9yQWxsKHMpXTsKY29uc3QgZGVmYXVsdHM9e25hbWU6InN0YW5kcG9pbnRfcGxheWVyIixnYW1lSWQ6ImRlbW9fMDAxIixlbG86MTAwMCx3aW5zOjAsbG9zc2VzOjAsZHJhd3M6MCxraWxsczowLGRlYXRoczowLHhwOjEyMCxmcmllbmRzOlt7bmFtZToic2gxem8iLHN0YXR1czoi0JvQtdCz0LXQvdC00LAiLGVsbzoyNDEwfSx7bmFtZToidzFubmVyIixzdGF0dXM6ItCSINC40LPRgNC1IixlbG86MjM4N30se25hbWU6Inp4Y3J1c2VkIixzdGF0dXM6ItCd0LUg0LIg0YHQtdGC0LgiLGVsbzoyMzMyfV0sbm90aWNlczpbe2ljb246IuKcpiIsdGl0bGU6ItCU0L7QsdGA0L4g0L/QvtC20LDQu9C+0LLQsNGC0Ywg0LIgU1RBTkRQT0lOVCIsYm9keToi0KLQstC+0Lkg0YHQvtGA0LXQstC90L7QstCw0YLQtdC70YzQvdGL0Lkg0L/Rg9GC0Ywg0L3QsNGH0LjQvdCw0LXRgtGB0Y8g0LfQtNC10YHRjC4iLHJlYWQ6ZmFsc2V9LHtpY29uOiLimpQiLHRpdGxlOiLQoNC10LnRgtC40L3Qs9C+0LLQsNGPINC+0YfQtdGA0LXQtNGMINC+0YLQutGA0YvRgtCwIixib2R5OiLQktGL0LHQtdGA0Lgg0YDQtdC20LjQvCDQuCDQvdCw0LnQtNC4INC80LDRgtGHLiIscmVhZDpmYWxzZX0se2ljb246IvCfjpYiLHRpdGxlOiJTVEFORFBPSU5UIFBhc3Mg0LDQutGC0LjQstC10L0iLGJvZHk6ItCX0LDRgNCw0LHQsNGC0YvQstCw0LkgWFAg0LfQsCDQvNCw0YLRh9C4INC4INC+0YLQutGA0YvQstCw0Lkg0L3QsNCz0YDQsNC00YsuIixyZWFkOnRydWV9XX07CmxldCBzdGF0ZTt0cnl7c3RhdGU9ey4uLmRlZmF1bHRzLC4uLkpTT04ucGFyc2UobG9jYWxTdG9yYWdlLmdldEl0ZW0oInN0YW5kcG9pbnREZW1vVjIiKXx8Int9Iil9O31jYXRjaHtzdGF0ZT17Li4uZGVmYXVsdHN9O30KbGV0IHNlbGVjdGVkTW9kZT0iNXY1IixzZWxlY3RlZE1hcD0iU2FuZHN0b25lIixxdWV1ZVRpbWVyPW51bGwscXVldWVTZWNvbmRzPTAscXVldWVBY3RpdmU9ZmFsc2U7CmNvbnN0IGxlYWRlcnM9W3tuYW1lOiJzaDF6byIsZWxvOjI0MTAsd2luczoxNTIsd3I6NzR9LHtuYW1lOiJ3MW5uZXIiLGVsbzoyMzg3LHdpbnM6MTM5LHdyOjcxfSx7bmFtZToienhjcnVzZWQiLGVsbzoyMzMyLHdpbnM6MTI4LHdyOjY5fSx7bmFtZToiZG9uazY2NiIsZWxvOjIyOTEsd2luczoxMTcsd3I6Njh9LHtuYW1lOiJ5b3UxbmciLGVsbzoyMjU2LHdpbnM6MTExLHdyOjY2fV07CmZ1bmN0aW9uIGVzYyhzKXtyZXR1cm4gU3RyaW5nKHMpLnJlcGxhY2UoL1smPD4iJ10vZyxjPT4oeyImIjoiJmFtcDsiLCI8IjoiJmx0OyIsIj4iOiImZ3Q7IiwnIic6IiZxdW90OyIsIiciOiImIzM5OyJ9W2NdKSk7fQpmdW5jdGlvbiBzYXZlKCl7bG9jYWxTdG9yYWdlLnNldEl0ZW0oInN0YW5kcG9pbnREZW1vVjIiLEpTT04uc3RyaW5naWZ5KHN0YXRlKSk7fQpmdW5jdGlvbiB0b2FzdChzKXtjb25zdCByb290PSQoIiN0b2FzdFJvb3QiKTtyb290LmlubmVySFRNTD0iIjtjb25zdCBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoImRpdiIpO2QuY2xhc3NOYW1lPSJ0b2FzdCI7ZC50ZXh0Q29udGVudD1zO3Jvb3QuYXBwZW5kKGQpO3NldFRpbWVvdXQoKCk9PmQucmVtb3ZlKCksMzIwMCk7fQpmdW5jdGlvbiBuYXYocGFnZSl7JCQoIi5zZWN0aW9uIikuZm9yRWFjaChzPT5zLmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIscy5pZD09PSJwYWdlLSIrcGFnZSkpOyQkKCJbZGF0YS1wYWdlXSIpLmZvckVhY2goYj0+Yi5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLGIuZGF0YXNldC5wYWdlPT09cGFnZSkpO2NvbnN0IHQ9e2hvbWU6ItCi0LLQvtGPINC40LPRgNCwLiDQotCy0L7RjyDQv9C+0LfQuNGG0LjRjy4iLG1hdGNoZXM6ItCc0LDRgtGH0LzQtdC50LrQuNC90LMiLHJhdGluZzoi0KDQtdC50YLQuNC90LMgRUxPIixjbGFuczoi0JrQu9Cw0L3RiyDQuCBDbGFuIFdhcnMiLHBhc3M6IlNUQU5EUE9JTlQgUGFzcyIsZnJpZW5kczoi0JTRgNGD0LfRjNGPIixub3RpZmljYXRpb25zOiLQo9Cy0LXQtNC+0LzQu9C10L3QuNGPIixwcm9maWxlOiLQn9GA0L7RhNC40LvRjCDQuNCz0YDQvtC60LAiLGNvc21ldGljczoi0JzQsNCz0LDQt9C40L0g0LrQvtGB0LzQtdGC0LjQutC4IixzdG9yZToi0JzQsNCz0LDQt9C40L0gU1RBTkRQT0lOVCIsdG91cm5hbWVudHM6ItCi0YPRgNC90LjRgNGLIn07JCgiI3BhZ2VUaXRsZSIpLnRleHRDb250ZW50PXRbcGFnZV18fCJTVEFORFBPSU5UIjt3aW5kb3cuc2Nyb2xsVG8oe3RvcDowLGJlaGF2aW9yOiJzbW9vdGgifSk7aWYocGFnZT09PSJzdG9yZSIpe3N5bmNXYWxsZXQoKTtyZWZyZXNoUGFzc0N1cnJlbmN5KCk7fWlmKHBhZ2U9PT0icGFzcyIpcmVmcmVzaFBhc3NDdXJyZW5jeSgpO30KJCQoIltkYXRhLXBhZ2VdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+bmF2KGIuZGF0YXNldC5wYWdlKSkpOwoKLy8gTmF0aXZlLWZlZWxpbmcgaG9yaXpvbnRhbCBuYXZpZ2F0aW9uIG9uIHRvdWNoIHNjcmVlbnMgYW5kIHRyYWNrcGFkcy4KY29uc3Qgc3dpcGVQYWdlcz1bImhvbWUiLCJtYXRjaGVzIiwicmF0aW5nIiwiY2xhbnMiLCJwYXNzIiwiZnJpZW5kcyIsIm5vdGlmaWNhdGlvbnMiLCJwcm9maWxlIiwiY29zbWV0aWNzIiwic3RvcmUiLCJ0b3VybmFtZW50cyJdOwpsZXQgc3dpcGVTdGFydFg9MCxzd2lwZVN0YXJ0WT0wOwpkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IixlPT57aWYoZS50b3VjaGVzLmxlbmd0aD09PTEpe3N3aXBlU3RhcnRYPWUudG91Y2hlc1swXS5jbGllbnRYO3N3aXBlU3RhcnRZPWUudG91Y2hlc1swXS5jbGllbnRZO319LHtwYXNzaXZlOnRydWV9KTsKZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLGU9PntpZighc3dpcGVTdGFydFgpcmV0dXJuO2NvbnN0IGR4PWUuY2hhbmdlZFRvdWNoZXNbMF0uY2xpZW50WC1zd2lwZVN0YXJ0WCxkeT1lLmNoYW5nZWRUb3VjaGVzWzBdLmNsaWVudFktc3dpcGVTdGFydFk7c3dpcGVTdGFydFg9MDtpZihNYXRoLmFicyhkeCk8NzV8fE1hdGguYWJzKGR4KTxNYXRoLmFicyhkeSkqMS4yNSlyZXR1cm47Y29uc3QgYWN0aXZlPSQoIi5zZWN0aW9uLmFjdGl2ZSIpLGN1cnJlbnQ9YWN0aXZlPy5pZC5yZXBsYWNlKCJwYWdlLSIsIiIpO2xldCBpPXN3aXBlUGFnZXMuaW5kZXhPZihjdXJyZW50KTtpZihpPDApcmV0dXJuO2k9ZHg8MD9NYXRoLm1pbihzd2lwZVBhZ2VzLmxlbmd0aC0xLGkrMSk6TWF0aC5tYXgoMCxpLTEpO25hdihzd2lwZVBhZ2VzW2ldKTt9LHtwYXNzaXZlOnRydWV9KTsKJCQoIi5nb2xkLWJ1eSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnRvYXN0KCLQn9Cw0LrQtdGCICIrTnVtYmVyKGIuZGF0YXNldC5wYWNrKS50b0xvY2FsZVN0cmluZygicnUtUlUiKSsiIEdvbGQgwrcg0L7RgNC40LXQvdGC0LjRgCDRhtC10L3RiyDQv9C+0LrQsNC30LDQvS4g0J7Qv9C70LDRgtCwINC4INCy0YvQtNCw0YfQsCBHb2xkINC10YnRkSDQvdC1INC/0L7QtNC60LvRjtGH0LXQvdGLLiIpKSk7CmZ1bmN0aW9uIHN5bmNXYWxsZXQoKXtjb25zdCBwPSQoIiNjb3NtZXRpY1BvaW50cyIpPy50ZXh0Q29udGVudHx8IuKAlCI7Y29uc3Qgdz0kKCIjd2FsbGV0UG9pbnRzIik7aWYodyl3LnRleHRDb250ZW50PXA7fQoKZnVuY3Rpb24gcmVuZGVyTGVhZGVycygpe2NvbnN0IGFycj1bLi4ubGVhZGVycyx7bmFtZTpzdGF0ZS5uYW1lLGVsbzpzdGF0ZS5lbG8sd2luczpzdGF0ZS53aW5zLHdyOnN0YXRlLndpbnMrc3RhdGUubG9zc2VzP01hdGgucm91bmQoc3RhdGUud2lucy8oc3RhdGUud2lucytzdGF0ZS5sb3NzZXMpKjEwMCk6MH1dLnNvcnQoKGEsYik9PmIuZWxvLWEuZWxvKTskKCIjaG9tZUxlYWRlcnMiKS5pbm5lckhUTUw9YXJyLnNsaWNlKDAsNCkubWFwKChwLGkpPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48c3BhbiBjbGFzcz0icmFuayI+JHtpKzF9PC9zcGFuPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MocC5uYW1lWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MocC5uYW1lKX08L2I+PHNtYWxsPiR7aT09PTA/ItCb0LXQs9C10L3QtNCwIjppPT09MT8i0JzQsNGB0YLQtdGAIjoi0JjQs9GA0L7QuiJ9PC9zbWFsbD48L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0iZWxvIj4ke3AuZWxvfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKTskKCIjcmF0aW5nUm93cyIpLmlubmVySFRNTD1hcnIubWFwKChwLGkpPT5gPHRyIGNsYXNzPSIke3AubmFtZT09PXN0YXRlLm5hbWU/InlvdSI6IiJ9Ij48dGQ+JHtpKzF9PC90ZD48dGQ+PGI+JHtlc2MocC5uYW1lKX08L2I+PC90ZD48dGQ+PGI+JHtwLmVsb308L2I+PC90ZD48dGQ+JHtwLndpbnN9PC90ZD48dGQ+JHtwLndyfSU8L3RkPjwvdHI+YCkuam9pbigiIik7fQpmdW5jdGlvbiByZW5kZXJGcmllbmRzKCl7Y29uc3QgcT0oJCgiI2ZyaWVuZFNlYXJjaCIpPy52YWx1ZXx8IiIpLnRvTG93ZXJDYXNlKCk7JCgiI2ZyaWVuZHNMaXN0IikuaW5uZXJIVE1MPXN0YXRlLmZyaWVuZHMuZmlsdGVyKGY9PmYubmFtZS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHEpKS5tYXAoZj0+YDxkaXYgY2xhc3M9ImxlYWRlciI+PGRpdiBjbGFzcz0icGxheWVyIj48ZGl2IGNsYXNzPSJtaW5pYXZhIj4ke2VzYyhmLm5hbWVbMF0udG9VcHBlckNhc2UoKSl9PC9kaXY+PGRpdj48Yj4ke2VzYyhmLm5hbWUpfTwvYj48c21hbGw+JHtlc2MoZi5zdGF0dXMpfSDCtyBFTE8gJHtmLmVsb308L3NtYWxsPjwvZGl2PjwvZGl2PjxidXR0b24gY2xhc3M9ImJ0bi1zZWNvbmRhcnkgaW52aXRlIiBkYXRhLW5hbWU9IiR7ZXNjKGYubmFtZSl9Ij7Qn9GA0LjQs9C70LDRgdC40YLRjDwvYnV0dG9uPjwvZGl2PmApLmpvaW4oIiIpfHwnPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7QmNCz0YDQvtC60Lgg0L3QtSDQvdCw0LnQtNC10L3Riy48L3A+JzskJCgiLmludml0ZSIpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PnRvYXN0KCLQlNC10LzQvjog0L/RgNC40LPQu9Cw0YjQtdC90LjQtSDQtNC70Y8gIitiLmRhdGFzZXQubmFtZSsiINC90LUg0L7RgtC/0YDQsNCy0LvQtdC90L4g0L3QsCDRgdC10YDQstC10YAuIikpKTt9CmZ1bmN0aW9uIHJlbmRlck5vdGljZXMoKXskKCIjbm90aWNlTGlzdCIpLmlubmVySFRNTD1zdGF0ZS5ub3RpY2VzLm1hcCgobixpKT0+YDxkaXYgY2xhc3M9Im5vdGljZSIgc3R5bGU9IiR7bi5yZWFkPyJvcGFjaXR5Oi44MiI6IiJ9Ij48ZGl2IGNsYXNzPSJub3RpY2VpY28iPiR7ZXNjKG4uaWNvbil9PC9kaXY+PGRpdiBzdHlsZT0iZmxleDoxIj48Yj4ke2VzYyhuLnRpdGxlKX08L2I+PHA+JHtlc2Mobi5ib2R5KX08L3A+PHNtYWxsIGNsYXNzPSJtdXRlZCI+JHtuLnJlYWQ/ItCf0YDQvtGH0LjRgtCw0L3QviI6ItCd0L7QstC+0LUifTwvc21hbGw+PC9kaXY+JHtuLnJlYWQ/IiI6YDxidXR0b24gY2xhc3M9InRleHRsaW5rIHJlYWROb3RpY2UiIGRhdGEtaW5kZXg9IiR7aX0iPtCf0YDQvtGH0LjRgtCw0L3QvjwvYnV0dG9uPmB9PC9kaXY+YCkuam9pbigiIik7JCQoIi5yZWFkTm90aWNlIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57Y29uc3Qgbj1zdGF0ZS5ub3RpY2VzWytiLmRhdGFzZXQuaW5kZXhdO3RyeXtpZihuLmlkJiZhcGlVc2VySWQpYXdhaXQgYXBpKGAvbm90aWZpY2F0aW9ucy8ke2VuY29kZVVSSUNvbXBvbmVudChuLmlkKX0vcmVhZGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkfX0pO24ucmVhZD10cnVlO3JlbmRlck5vdGljZXMoKTt9Y2F0Y2goZSl7dG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0L7RgtC80LXRgtC40YLRjDogIitlLm1lc3NhZ2UpO319KSk7fQpmdW5jdGlvbiByZW5kZXIoKXtjb25zdCB0b3RhbD1zdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cyx3cj10b3RhbD9NYXRoLnJvdW5kKHN0YXRlLndpbnMvdG90YWwqMTAwKTowLGxldmVsPU1hdGguZmxvb3Ioc3RhdGUueHAvMzAwKSsxLHBjdD0oc3RhdGUueHAlMzAwKS8zOyQoIiNzaWRlTmFtZSIpLnRleHRDb250ZW50PXN0YXRlLm5hbWU7JCgiI2hvbWVFbG8iKS50ZXh0Q29udGVudD1zdGF0ZS5lbG87JCgiI2hvbWVXaW5zIikudGV4dENvbnRlbnQ9c3RhdGUud2luczskKCIjaG9tZVdSIikudGV4dENvbnRlbnQ9d3IrIiUiOyQoIiNwcm9maWxlTmFtZSIpLnRleHRDb250ZW50PXN0YXRlLm5hbWU7JCgiI3Byb2ZpbGVJZCIpLnRleHRDb250ZW50PSJJRDogIitzdGF0ZS5nYW1lSWQrIiI7JCgiI3Byb2ZpbGVFbG8iKS50ZXh0Q29udGVudD1zdGF0ZS5lbG87JCgiI3Byb2ZpbGVNYXRjaGVzIikudGV4dENvbnRlbnQ9dG90YWw7JCgiI3Byb2ZpbGVXaW5zIikudGV4dENvbnRlbnQ9c3RhdGUud2luczskKCIjcHJvZmlsZUtEIikudGV4dENvbnRlbnQ9c3RhdGUuZGVhdGhzPyhzdGF0ZS5raWxscy9zdGF0ZS5kZWF0aHMpLnRvRml4ZWQoMik6c3RhdGUua2lsbHM/IuKIniI6IuKAlCI7JCgiI2VkaXROYW1lIikudmFsdWU9c3RhdGUubmFtZTskKCIjZWRpdEdhbWVJZCIpLnZhbHVlPXN0YXRlLmdhbWVJZDskKCIjcGFzc0xldmVsIikudGV4dENvbnRlbnQ9U3RyaW5nKGxldmVsKS5wYWRTdGFydCgyLCIwIik7JCgiI3Bhc3NMZXZlbEJpZyIpLnRleHRDb250ZW50PVN0cmluZyhsZXZlbCkucGFkU3RhcnQoMiwiMCIpOyQoIiNwYXNzWFAiKS50ZXh0Q29udGVudD0oc3RhdGUueHAlMzAwKSsiIC8gMzAwIFhQIjskKCIjcGFzc1hQQmlnIikudGV4dENvbnRlbnQ9KHN0YXRlLnhwJTMwMCkrIiAvIDMwMCBYUCI7JCgiI3Bhc3NQcm9ncmVzcyIpLnN0eWxlLndpZHRoPXBjdCsiJSI7JCgiI3Bhc3NQcm9ncmVzc0JpZyIpLnN0eWxlLndpZHRoPXBjdCsiJSI7cmVuZGVyTGVhZGVycygpO3JlbmRlckZyaWVuZHMoKTtyZW5kZXJOb3RpY2VzKCk7c2F2ZSgpO30KJCQoIltkYXRhLW1vZGVdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2lmKHF1ZXVlQWN0aXZlKXt0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L7RgtC80LXQvdC4INGC0LXQutGD0YnQuNC5INC/0L7QuNGB0LosINGH0YLQvtCx0Ysg0YHQvNC10L3QuNGC0Ywg0YDQtdC20LjQvC4iKTtyZXR1cm47fXNlbGVjdGVkTW9kZT1iLmRhdGFzZXQubW9kZTskJCgiW2RhdGEtbW9kZV0iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIix4PT09YikpO30pKTsKJCQoIltkYXRhLW1hcF0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCgpPT57CiBzZWxlY3RlZE1hcD1iLmRhdGFzZXQubWFwOyQkKCJbZGF0YS1tYXBdIikuZm9yRWFjaCh4PT54LmNsYXNzTGlzdC50b2dnbGUoInNlbGVjdGVkIix4PT09YikpOwogdG9hc3QoItCa0LDRgNGC0LAg0LLRi9Cx0YDQsNC90LA6ICIrc2VsZWN0ZWRNYXArKGFjdGl2ZU1hdGNoSWQ/IiDCtyDQvtGC0LrRgNC+0Lkg0LzQsNGC0Ycg0LTQu9GPIGJhbi1waWNrIjoiIikpOwp9KSk7CiQoIiNxdWV1ZUJ1dHRvbiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYyAoKT0+ewogaWYoIWFwaVVzZXJJZCl7dG9hc3QoItCe0YLQutGA0L7QuSBTVEFORFBPSU5UINGH0LXRgNC10LcgVGVsZWdyYW0g0Lgg0YHQvtGF0YDQsNC90Lgg0L/RgNC+0YTQuNC70YwuIik7bmF2KCJwcm9maWxlIik7cmV0dXJuO30KIHRyeXsKICBpZighcXVldWVBY3RpdmUpewogICBjb25zdCBkPWF3YWl0IGFwaSgiL3F1ZXVlL2pvaW4iLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxtb2RlOnNlbGVjdGVkTW9kZX19KTsKICAgaWYoZC5tYXRjaF9pZCl7YWN0aXZlTWF0Y2hJZD1kLm1hdGNoX2lkO3F1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTthd2FpdCBzaG93TWF0Y2goYWN0aXZlTWF0Y2hJZCk7cmV0dXJuO30KICAgcXVldWVBY3RpdmU9ZC5zdGF0dXM9PT0icXVldWVkIjskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD1xdWV1ZUFjdGl2ZT8i0J7RgtC80LXQvdC40YLRjCDQv9C+0LjRgdC6Ijoi0J3QsNC50YLQuCDQvNCw0YLRhyI7CiAgICQoIiNxdWV1ZVRpdGxlIikudGV4dENvbnRlbnQ9ItCY0YnQtdC8INGB0L7Qv9C10YDQvdC40LrQvtCy4oCmIjsKICAgJCgiI3F1ZXVlRGVzY3JpcHRpb24iKS50ZXh0Q29udGVudD1g0JIg0L7Rh9C10YDQtdC00LggJHtkLnBsYXllcnNfZm91bmR9LyR7ZC5wbGF5ZXJzX25lZWRlZH07INC+0YHRgtCw0LvQvtGB0YwgJHtkLnBsYXllcnNfcmVtYWluaW5nfWA7CiAgIHRvYXN0KCLQn9C+0LjRgdC6INC30LDQv9GD0YnQtdC9LiDQodGH0ZHRgtGH0LjQuiDRgdC40L3RhdGA0L7QvdC40LfQuNGA0YPQtdGC0YHRjyDRgSDRgdC10YDQstC10YDQvtC8LiIpO3N0YXJ0UXVldWVQb2xsaW5nKCk7CiAgfWVsc2V7CiAgIGF3YWl0IGFwaSgiL3F1ZXVlL2xlYXZlIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWR9fSk7CiAgIHF1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD0i0J3QsNC50YLQuCDQvNCw0YLRhyI7JCgiI3F1ZXVlVGl0bGUiKS50ZXh0Q29udGVudD0i0J/QvtC40YHQuiDQvtGC0LzQtdC90ZHQvSI7JCgiI3F1ZXVlRGVzY3JpcHRpb24iKS50ZXh0Q29udGVudD0i0KLRiyDQstGL0YjQtdC7INC40Lcg0L7Rh9C10YDQtdC00LguIjt0b2FzdCgi0J/QvtC40YHQuiDQvtGC0LzQtdC90ZHQvS4iKTsKICB9CiB9Y2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDQvtGH0LXRgNC10LTQuDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNyYXRpbmdUYWJzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGU9Pntjb25zdCBiPWUudGFyZ2V0LmNsb3Nlc3QoIltkYXRhLXRhYl0iKTtpZighYilyZXR1cm47JCQoIltkYXRhLXRhYl0iKS5mb3JFYWNoKHg9PnguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIix4PT09YikpO3RvYXN0KGIuZGF0YXNldC50YWI9PT0ic2Vhc29uIj8i0KDQtdC50YLQuNC90LMg0YHQtdC30L7QvdCwICjQtNC10LzQvikuIjoi0J7QsdGJ0LjQuSDRgNC10LnRgtC40L3QsyAo0LTQtdC80L4pLiIpO30pOwokJCgiLmNsYWltIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2lmKGIuZGF0YXNldC5yZXdhcmQ9PT0i0J3QvtCy0L7QsdGA0LDQvdC10YYiKXt0b2FzdCgi0K3RgtCwINC90LDQs9GA0LDQtNCwINGD0LbQtSDQv9C+0LvRg9GH0LXQvdCwLiIpO3JldHVybjt9Y29uc3QgbmVlZD1iLmRhdGFzZXQucmV3YXJkPT09ItCd0LAg0L/QvtC00YrRkdC80LUiPzU6MTAsbGV2ZWw9TWF0aC5mbG9vcihzdGF0ZS54cC8zMDApKzE7dG9hc3QobGV2ZWw8bmVlZD8i0J3Rg9C20LXQvSDRg9GA0L7QstC10L3RjCAiK25lZWQrIi4g0KLQstC+0Lkg0YPRgNC+0LLQtdC90Yw6ICIrbGV2ZWw6ItCd0LDQs9GA0LDQtNGLINCx0YPQtNGD0YIg0LTQvtGB0YLRg9C/0L3RiyDQv9C+0YHQu9C1INC/0L7QtNC60LvRjtGH0LXQvdC40Y8gUGFzcyBBUEkuIik7fSkpOwokKCIjc2F2ZVByb2ZpbGUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMgKCk9PnsKIGNvbnN0IG5pY2tuYW1lPSQoIiNlZGl0TmFtZSIpLnZhbHVlLnRyaW0oKSxnYW1lX2lkPSQoIiNlZGl0R2FtZUlkIikudmFsdWUudHJpbSgpOwogaWYoIW5pY2tuYW1lfHwhZ2FtZV9pZClyZXR1cm4gdG9hc3QoItCX0LDQv9C+0LvQvdC4INC90LjQutC90LXQudC8INC4INC40LPRgNC+0LLQvtC5IElELiIpOwogbGV0IGlkPWFwaVVzZXJJZHx8cHJvbXB0KCLQktCy0LXQtNC4INGB0LLQvtC5IFRlbGVncmFtIElEINC00LvRjyDQv9GA0LjQstGP0LfQutC4INCw0LrQutCw0YPQvdGC0LA6Iik7CiBpZighaWQpcmV0dXJuO2FwaVVzZXJJZD1TdHJpbmcoaWQpLnRyaW0oKTtsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgic3RhbmRwb2ludFRlbGVncmFtSWQiLGFwaVVzZXJJZCk7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoIi9wbGF5ZXJzL3JlZ2lzdGVyIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbmlja25hbWUsZ2FtZV9pZH19KTthcHBseVBsYXllcihkLnBsYXllcik7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt0b2FzdCgi0J/RgNC+0YTQuNC70Ywg0YHQvtGF0YDQsNC90ZHQvSDQvdCwINGB0LXRgNCy0LXRgNC1LiIpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0L/RgNC+0YTQuNC70Y86ICIrZS5tZXNzYWdlKTt9Cn0pOwokKCIjZnJpZW5kU2VhcmNoIikuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLHJlbmRlckZyaWVuZHMpOwokKCIjYWRkRnJpZW5kQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBjb25zdCB0YXJnZXRfaWQ9cHJvbXB0KCJUZWxlZ3JhbSBJRCDQuNCz0YDQvtC60LAsINC60L7RgtC+0YDQvtCz0L4g0YXQvtGH0LXRiNGMINC00L7QsdCw0LLQuNGC0Yw6Iik7CiBpZighdGFyZ2V0X2lkfHwhYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0J3Rg9C20LXQvSDRgtCy0L7QuSBUZWxlZ3JhbSBJRCDQuCBJRCDQtNGA0YPQs9CwLiIpOwogdHJ5e2F3YWl0IGFwaSgiL2ZyaWVuZHMvcmVxdWVzdCIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHRhcmdldF9pZDp0YXJnZXRfaWQudHJpbSgpfX0pO3RvYXN0KCLQl9Cw0Y/QstC60LAg0L7RgtC/0YDQsNCy0LvQtdC90LAuIik7YXdhaXQgcmVmcmVzaEFwaURhdGEoKTt9CiBjYXRjaChlKXt0b2FzdCgi0J7RiNC40LHQutCwINC30LDRj9Cy0LrQuDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNyZWFkQWxsIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjC4iKTsKIHRyeXthd2FpdCBhcGkoYC9ub3RpZmljYXRpb25zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L3JlYWQtYWxsYCx7bWV0aG9kOiJQT1NUIn0pO2F3YWl0IHJlZnJlc2hBcGlEYXRhKCk7dG9hc3QoItCj0LLQtdC00L7QvNC70LXQvdC40Y8g0L7RgtC80LXRh9C10L3RiyDQv9GA0L7Rh9C40YLQsNC90L3Ri9C80LguIik7fQogY2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsDogIitlLm1lc3NhZ2UpO30KfSk7CiQoIiNjcmVhdGVDbGFuQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjCDQsiBUZWxlZ3JhbS4iKTsKIGNvbnN0IG5hbWU9cHJvbXB0KCLQndCw0LfQstCw0L3QuNC1INC60LvQsNC90LAgKDPigJM0MCDRgdC40LzQstC+0LvQvtCyKToiKTtpZighbmFtZT8udHJpbSgpKXJldHVybjsKIGNvbnN0IHRhZz1wcm9tcHQoItCi0LXQsyDQutC70LDQvdCwICgy4oCTOCDQu9Cw0YLQuNC90YHQutC40YUg0LHRg9C60LIv0YbQuNGE0YApOiIpO2lmKCF0YWc/LnRyaW0oKSlyZXR1cm47CiBjb25zdCBkZXNjcmlwdGlvbj1wcm9tcHQoItCe0L/QuNGB0LDQvdC40LUg0LrQu9Cw0L3QsCAo0L3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4pOiIpfHwiIjsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaSgiL2NsYW5zIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsbmFtZTpuYW1lLnRyaW0oKSx0YWc6dGFnLnRyaW0oKSxkZXNjcmlwdGlvbn19KTthY3RpdmVDbGFuSWQ9ZC5jbGFuX2lkO3RvYXN0KCLQmtC70LDQvSDRgdC+0LfQtNCw0L0g0L3QsCDRgdC10YDQstC10YDQtS4iKTthd2FpdCByZWZyZXNoQXBpRGF0YSgpO30KIGNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0YHQvtC30LTQsNC90LjRjyDQutC70LDQvdCwOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2NsYW5EZXRhaWxzIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYWN0aXZlQ2xhbklkKXJldHVybiB0b2FzdCgi0J3QtdGCINCy0YvQsdGA0LDQvdC90L7Qs9C+INC60LvQsNC90LAuINCh0L7Qt9C00LDQuSDQutC70LDQvSDQuNC70Lgg0L7QsdC90L7QstC4INC00LDQvdC90YvQtS4iKTsKIHRyeXtjb25zdCBkPWF3YWl0IGFwaShgL2NsYW5zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZUNsYW5JZCl9YCk7YWxlcnQoYCR7ZC5jbGFuLm5hbWV9IFske2QuY2xhbi50YWd9XSDCtyBFTE8gJHtkLmNsYW4uZWxvfVxuXG5gK2QubWVtYmVycy5tYXAobT0+YCR7bS5uaWNrbmFtZX0g4oCUICR7bS5yb2xlfSDigJQgJHttLmVsb30gRUxPYCkuam9pbigiXG4iKSk7fQogY2F0Y2goZSl7dG9hc3QoZS5tZXNzYWdlKTt9Cn0pOwokKCIjd2FyQnV0dG9uIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jICgpPT57CiBpZighYWN0aXZlQ2xhbklkKXJldHVybiB0b2FzdCgi0KHQvdCw0YfQsNC70LAg0LLRi9Cx0LXRgNC4INC40LvQuCDRgdC+0LfQtNCw0Lkg0LrQu9Cw0L0uIik7CiB0cnl7Y29uc3QgZD1hd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlQ2xhbklkKX1gKTthbGVydCgoZC53YXJzfHxbXSkubWFwKHc9PmAke3cud2FyX2lkfSDCtyAke3cuc3RhdHVzfSDCtyAke3cuc2NvcmVfYT8/Ii0ifToke3cuc2NvcmVfYj8/Ii0ifWApLmpvaW4oIlxuIil8fCLQmNGB0YLQvtGA0LjRjyBDbGFuIFdhcnMg0L/Rg9GB0YLQsC4iKTt9CiBjYXRjaChlKXt0b2FzdCgiQ2xhbiBXYXJzOiAiK2UubWVzc2FnZSk7fQp9KTsKJCgiI2NsYW5NZW1iZXJzIikuaW5uZXJIVE1MPSc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCX0LDQs9GA0YPQt9C60LAg0YHQvtGB0YLQsNCy0LAg0LrQu9Cw0L3QsCDRgSDRgdC10YDQstC10YDQsOKApjwvcD4nOwoKLy8gPT09PT0gU1RBTkRQT0lOVCBBUEkgPT09PT0KY29uc3QgQVBJX0JBU0U9Imh0dHBzOi8vc3RhbmRwb2ludC1iYWNrZW5kLm9ucmVuZGVyLmNvbSI7CmxldCBhcGlVc2VySWQ9IiIsYWN0aXZlQ2xhbklkPSIiLGFjdGl2ZU1hdGNoSWQ9IiIscXVldWVQb2xsVGltZXI9bnVsbDsKZnVuY3Rpb24gZ2V0VGVsZWdyYW1Vc2VyKCl7dHJ5e3JldHVybiB3aW5kb3cuVGVsZWdyYW0/LldlYkFwcD8uaW5pdERhdGFVbnNhZmU/LnVzZXJ8fG51bGx9Y2F0Y2h7cmV0dXJuIG51bGx9fQpmdW5jdGlvbiBzZXRBcGlTdGF0dXModGV4dCxvaz1mYWxzZSl7Y29uc3QgZT0kKCIjYXBpU3RhdHVzIik7aWYoZSl7ZS50ZXh0Q29udGVudD10ZXh0O2Uuc3R5bGUuY29sb3I9b2s/IiM3NWYwYjYiOiIjZmZiNGE4Ijt9fQpmdW5jdGlvbiByZWFkYWJsZUFwaUVycm9yKGQsc3RhdHVzKXsKIGNvbnN0IGRldGFpbD1kPy5kZXRhaWx8fGQ/Lm1lc3NhZ2U7CiAvLyDQndC1INCy0YvQstC+0LTQuNC8INCyINC40L3RgtC10YDRhNC10LnRgSDQv9C+0YLQtdC90YbQuNCw0LvRjNC90L4g0YHQu9C+0LzQsNC90L3Rg9GOINC60L7QtNC40YDQvtCy0LrRgyDQvtGC0LLQtdGC0LAg0YHQtdGA0LLQtdGA0LAuCiBpZihzdGF0dXM9PT00MDQpcmV0dXJuICLQv9GA0L7RhNC40LvRjCDQuNC70Lgg0YDQtdGB0YPRgNGBINC90LUg0L3QsNC50LTQtdC9ICg0MDQpIjsKIGlmKHN0YXR1cz09PTQyMilyZXR1cm4gItGB0LXRgNCy0LXRgCDQvtGC0LrQu9C+0L3QuNC7INC00LDQvdC90YvQtSDQt9Cw0L/RgNC+0YHQsCAoNDIyKSI7CiBpZihzdGF0dXM9PT00MDF8fHN0YXR1cz09PTQwMylyZXR1cm4gItGB0LXRgNCy0LXRgCDQvdC1INGA0LDQt9GA0LXRiNC40Lsg0LfQsNC/0YDQvtGBICgiK3N0YXR1cysiKSI7CiBpZihzdGF0dXM+PTUwMClyZXR1cm4gItCy0L3Rg9GC0YDQtdC90L3Rj9GPINC+0YjQuNCx0LrQsCDRgdC10YDQstC10YDQsCAoIitzdGF0dXMrIikiOwogaWYodHlwZW9mIGRldGFpbD09PSJzdHJpbmciICYmICEvW8OQw5HDg8Oi4oKs77+9XS8udGVzdChkZXRhaWwpKXJldHVybiBkZXRhaWw7CiByZXR1cm4gItC+0YjQuNCx0LrQsCDQt9Cw0L/RgNC+0YHQsCAoSFRUUCAiK3N0YXR1cysiKSI7Cn0KYXN5bmMgZnVuY3Rpb24gYXBpKHBhdGgse21ldGhvZD0iR0VUIixib2R5PW51bGx9PXt9KXsKIGNvbnN0IG9wdHM9e21ldGhvZCxoZWFkZXJzOnsiQWNjZXB0IjoiYXBwbGljYXRpb24vanNvbiJ9fTsKIGlmKGJvZHkhPT1udWxsKXtvcHRzLmhlYWRlcnNbIkNvbnRlbnQtVHlwZSJdPSJhcHBsaWNhdGlvbi9qc29uIjtvcHRzLmJvZHk9SlNPTi5zdHJpbmdpZnkoYm9keSk7fQogbGV0IHI7dHJ5e3I9YXdhaXQgZmV0Y2goQVBJX0JBU0UrcGF0aCxvcHRzKX1jYXRjaChlKXt0aHJvdyBuZXcgRXJyb3IoItGB0LXRgNCy0LXRgCDQvdC10LTQvtGB0YLRg9C/0LXQvSDQuNC70Lgg0LHRgNCw0YPQt9C10YAg0LfQsNCx0LvQvtC60LjRgNC+0LLQsNC7INC30LDQv9GA0L7RgSAoQ09SUy/RgdC10YLRjCkiKTt9CiBsZXQgZD17fTt0cnl7ZD1hd2FpdCByLmpzb24oKX1jYXRjaHt9CiBpZighci5vayl7Y29uc3QgZXJyPW5ldyBFcnJvcihyZWFkYWJsZUFwaUVycm9yKGQsci5zdGF0dXMpKTtlcnIuc3RhdHVzPXIuc3RhdHVzO2Vyci5hcGlQYXRoPXBhdGg7dGhyb3cgZXJyO30KIHJldHVybiBkOwp9CmZ1bmN0aW9uIGFwcGx5UGxheWVyKHApe2lmKCFwKXJldHVybjtjb25zdCBwcz0kKCIjcHJvZmlsZVN0YXR1cyIpO2lmKHBzKXBzLnRleHRDb250ZW50PSLil48g0J/QoNCe0KTQmNCb0Kwg0KEg0KHQldCg0JLQldCg0JAiO3NldEFwaVN0YXR1cygiQVBJINC+0L3Qu9Cw0LnQvSDCtyDQv9GA0L7RhNC40LvRjCDQt9Cw0LPRgNGD0LbQtdC9Iix0cnVlKTtzdGF0ZS5uYW1lPXAubmlja25hbWV8fHN0YXRlLm5hbWU7c3RhdGUuZ2FtZUlkPXAuZ2FtZV9pZHx8c3RhdGUuZ2FtZUlkO3N0YXRlLmVsbz1OdW1iZXIocC5lbG8/PzEwMDApO3N0YXRlLndpbnM9TnVtYmVyKHAud2lucz8/MCk7c3RhdGUubG9zc2VzPU51bWJlcihwLmxvc3Nlcz8/MCk7c3RhdGUuZHJhd3M9TnVtYmVyKHAuZHJhd3M/PzApO3N0YXRlLmtpbGxzPU51bWJlcihwLmtpbGxzPz8wKTtzdGF0ZS5kZWF0aHM9TnVtYmVyKHAuZGVhdGhzPz8wKTtyZW5kZXIoKTt9CmFzeW5jIGZ1bmN0aW9uIHJlZnJlc2hQYXNzQ3VycmVuY3koKXsKIGlmKCFhcGlVc2VySWQpcmV0dXJuOwogdHJ5ewogIGNvbnN0IGQ9YXdhaXQgYXBpKGAvcGFzcy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApOwogIGlmKGQuYWN0aXZlPT09ZmFsc2UpcmV0dXJuOwogIHN0YXRlLnhwPU51bWJlcihkLnhwfHwwKTsKICBjb25zdCB3YWxsZXQ9JCgiI3dhbGxldFBvaW50cyIpO2lmKHdhbGxldCl3YWxsZXQudGV4dENvbnRlbnQ9TnVtYmVyKGQucG9pbnRzfHwwKS50b0xvY2FsZVN0cmluZygicnUtUlUiKTsKICBjb25zdCBjbGFpbWVkPW5ldyBTZXQoKGQuY2xhaW1lZF9jdXJyZW5jeV9sZXZlbHN8fFtdKS5tYXAoTnVtYmVyKSk7CiAgY29uc3QgYW1vdW50cz17Mjo1MCw3OjEwMCwxNToxNTAsMjU6MjUwLDMwOjMwMH07CiAgJCQoIi5jbGFpbVNQIikuZm9yRWFjaChiPT57CiAgIGNvbnN0IGxldmVsPU51bWJlcihiLmRhdGFzZXQubGV2ZWwpLGFscmVhZHk9Y2xhaW1lZC5oYXMobGV2ZWwpLGxvY2tlZD0hZC5wcmVtaXVtfHxOdW1iZXIoZC5sZXZlbHx8MSk8bGV2ZWw7CiAgIGIuZGlzYWJsZWQ9YWxyZWFkeXx8bG9ja2VkOwogICBiLnRleHRDb250ZW50PWFscmVhZHk/ItCf0L7Qu9GD0YfQtdC90L4iOighZC5wcmVtaXVtPyLQndGD0LbQtdC9IFByZW1pdW0gUGFzcyI6KE51bWJlcihkLmxldmVsfHwxKTxsZXZlbD9g0J7RgtC60YDQvtC10YLRgdGPINC90LAg0YPRgNC+0LLQvdC1ICR7bGV2ZWx9YDpg0JfQsNCx0YDQsNGC0YwgJHthbW91bnRzW2xldmVsXX0gU1BgKSk7CiAgfSk7CiB9Y2F0Y2goZSl7Y29uc29sZS53YXJuKCJQYXNzIGN1cnJlbmN5OiIsZS5tZXNzYWdlKTt9Cn0KJCQoIi5jbGFpbVNQIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57CiBpZighYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0J7RgtC60YDQvtC5INC/0YDQuNC70L7QttC10L3QuNC1INGH0LXRgNC10LcgVGVsZWdyYW0g0Lgg0L/QvtC00LrQu9GO0YfQuCDQv9GA0L7RhNC40LvRjC4iKTsKIGIuZGlzYWJsZWQ9dHJ1ZTsKIHRyeXsKICBjb25zdCBkPWF3YWl0IGFwaSgiL3Bhc3MvY2xhaW0tY3VycmVuY3kiLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxsZXZlbDpOdW1iZXIoYi5kYXRhc2V0LmxldmVsKX19KTsKICB0b2FzdChg0J3QsNGH0LjRgdC70LXQvdC+ICske2QuYW1vdW50fSBTUC4g0JHQsNC70LDQvdGBOiAke051bWJlcihkLnBvaW50cykudG9Mb2NhbGVTdHJpbmcoInJ1LVJVIil9IFNQYCk7CiAgYXdhaXQgcmVmcmVzaFBhc3NDdXJyZW5jeSgpOwogfWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC/0L7Qu9GD0YfQuNGC0YwgU1A6ICIrZS5tZXNzYWdlKTthd2FpdCByZWZyZXNoUGFzc0N1cnJlbmN5KCk7fQp9KSk7CmFzeW5jIGZ1bmN0aW9uIHJlZnJlc2hBcGlEYXRhKCl7CiBpZighYXBpVXNlcklkKXJldHVybjsKIGNvbnN0IGpvYnM9WwogIGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PmFwcGx5UGxheWVyKGQucGxheWVyKSksCiAgYXBpKCIvbGVhZGVyYm9hcmQ/bGltaXQ9NTAiKS50aGVuKGQ9PnsKICAgY29uc3QgYT0oZC5wbGF5ZXJzfHxbXSkubWFwKHg9Pih7bmFtZTp4Lm5pY2tuYW1lLGVsbzpOdW1iZXIoeC5lbG98fDEwMDApLHdpbnM6TnVtYmVyKHgud2luc3x8MCksd3I6KE51bWJlcih4LndpbnN8fDApK051bWJlcih4Lmxvc3Nlc3x8MCkpP01hdGgucm91bmQoTnVtYmVyKHgud2luc3x8MCkvKE51bWJlcih4LndpbnN8fDApK051bWJlcih4Lmxvc3Nlc3x8MCkpKjEwMCk6MH0pKTsKICAgaWYoYS5sZW5ndGgpeyQoIiNob21lTGVhZGVycyIpLmlubmVySFRNTD1hLnNsaWNlKDAsNSkubWFwKCh4LGkpPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48c3BhbiBjbGFzcz0icmFuayI+JHtpKzF9PC9zcGFuPjxkaXYgY2xhc3M9InBsYXllciI+PGRpdiBjbGFzcz0ibWluaWF2YSI+JHtlc2MoKHgubmFtZXx8Ij8iKVswXS50b1VwcGVyQ2FzZSgpKX08L2Rpdj48ZGl2PjxiPiR7ZXNjKHgubmFtZSl9PC9iPjxzbWFsbD4ke3gud2luc30g0L/QvtCx0LXQtDwvc21hbGw+PC9kaXY+PC9kaXY+PHNwYW4gY2xhc3M9ImVsbyI+JHt4LmVsb308L3NwYW4+PC9kaXY+YCkuam9pbigiIik7JCgiI3JhdGluZ1Jvd3MiKS5pbm5lckhUTUw9YS5tYXAoKHgsaSk9PmA8dHI+PHRkPiR7aSsxfTwvdGQ+PHRkPjxiPiR7ZXNjKHgubmFtZSl9PC9iPjwvdGQ+PHRkPjxiPiR7eC5lbG99PC9iPjwvdGQ+PHRkPiR7eC53aW5zfTwvdGQ+PHRkPiR7eC53cn0lPC90ZD48L3RyPmApLmpvaW4oIiIpO30KICB9KSwKICBhcGkoYC9mcmllbmRzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57c3RhdGUuZnJpZW5kcz0oZC5mcmllbmRzfHxbXSkubWFwKGY9Pih7bmFtZTpmLm5pY2tuYW1lLHRlbGVncmFtX2lkOmYudGVsZWdyYW1faWQsc3RhdHVzOiLQlNGA0YPQsyIsZWxvOk51bWJlcihmLmVsb3x8MTAwMCl9KSk7cmVuZGVyRnJpZW5kcygpO30pLAogIGFwaShgL25vdGlmaWNhdGlvbnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX1gKS50aGVuKGQ9PntzdGF0ZS5ub3RpY2VzPShkLm5vdGlmaWNhdGlvbnN8fFtdKS5tYXAobj0+KHtpZDpuLm5vdGlmaWNhdGlvbl9pZCxpY29uOm4ua2luZD09PSJtYXRjaF9mb3VuZCI/IuKalCI6IuKcpiIsdGl0bGU6bi50aXRsZSxib2R5Om4uYm9keSxyZWFkOiEhbi5pc19yZWFkfSkpO3JlbmRlck5vdGljZXMoKTt9KSwKICBhcGkoYC9wYXNzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57aWYoZC5hY3RpdmUhPT1mYWxzZSl7c3RhdGUueHA9TnVtYmVyKGQueHB8fDApO3JlbmRlcigpO319KSwKICBhcGkoYC9yZXdhcmRzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCkudGhlbihkPT57CiAgIGNvbnN0IGVhcm5lZD1uZXcgU2V0KChkLnJld2FyZHN8fFtdKS5tYXAocj0+ci5uYW1lKSk7CiAgICQkKCIuY2xhaW0iKS5mb3JFYWNoKGI9Pntjb25zdCBuYW1lPWIuZGF0YXNldC5yZXdhcmQ7aWYoZWFybmVkLmhhcyhuYW1lKSl7Yi50ZXh0Q29udGVudD0i0J/QvtC70YPRh9C10L3QviI7Yi5kaXNhYmxlZD10cnVlO319KTsKICB9KSwKICBhcGkoIi9jbGFucz9saW1pdD01MCIpLnRoZW4oYXN5bmMgZD0+ewogICBjb25zdCBjbGFucz1kLmNsYW5zfHxbXTsKICAgY29uc3Qgb3Bwb25lbnRTZWxlY3Q9JCgiI3dhck9wcG9uZW50SWQiKTtpZihvcHBvbmVudFNlbGVjdCl7Y29uc3Qgb3Bwb25lbnRzPWNsYW5zLmZpbHRlcih4PT54LmNsYW5faWQhPT1hY3RpdmVDbGFuSWQpO29wcG9uZW50U2VsZWN0LmlubmVySFRNTD0nPG9wdGlvbiB2YWx1ZT0iIj7QktGL0LHQtdGA0Lgg0LrQu9Cw0L0t0YHQvtC/0LXRgNC90LjQuuKApjwvb3B0aW9uPicrb3Bwb25lbnRzLm1hcCh4PT5gPG9wdGlvbiB2YWx1ZT0iJHtlc2MoeC5jbGFuX2lkKX0iPiR7ZXNjKHgubmFtZSl9IFske2VzYyh4LnRhZyl9XSDCtyAke051bWJlcih4LmVsb3x8MTAwMCl9IEVMTzwvb3B0aW9uPmApLmpvaW4oIiIpO30KICAgaWYoIWNsYW5zLmxlbmd0aCl7JCgiI2NsYW5NZW1iZXJzIikuaW5uZXJIVE1MPSc8cCBjbGFzcz0ibXV0ZWQgc21hbGwiPtCa0LvQsNC90L7QsiDQv9C+0LrQsCDQvdC10YIuINCh0L7Qt9C00LDQuSDQv9C10YDQstGL0Lkg0LrQu9Cw0L0uPC9wPic7cmV0dXJuO30KICAgY29uc3QgbWluZT1jbGFucy5maW5kKGM9PmMub3duZXJfaWQ9PT1hcGlVc2VySWQpO2NvbnN0IGM9bWluZXx8Y2xhbnNbMF07YWN0aXZlQ2xhbklkPWMuY2xhbl9pZDsKICAgaWYob3Bwb25lbnRTZWxlY3Qpe2NvbnN0IG9wcG9uZW50cz1jbGFucy5maWx0ZXIoeD0+eC5jbGFuX2lkIT09YWN0aXZlQ2xhbklkKTtvcHBvbmVudFNlbGVjdC5pbm5lckhUTUw9JzxvcHRpb24gdmFsdWU9IiI+0JLRi9Cx0LXRgNC4INC60LvQsNC9LdGB0L7Qv9C10YDQvdC40LrigKY8L29wdGlvbj4nK29wcG9uZW50cy5tYXAoeD0+YDxvcHRpb24gdmFsdWU9IiR7ZXNjKHguY2xhbl9pZCl9Ij4ke2VzYyh4Lm5hbWUpfSBbJHtlc2MoeC50YWcpfV0gwrcgJHtOdW1iZXIoeC5lbG98fDEwMDApfSBFTE88L29wdGlvbj5gKS5qb2luKCIiKTt9CiAgIGNvbnN0IG93bldhck5hbWU9JCgiI3dhck93bk5hbWUiKTtpZihvd25XYXJOYW1lKW93bldhck5hbWUudGV4dENvbnRlbnQ9U3RyaW5nKGMudGFnfHxjLm5hbWV8fCLQotCS0J7QmSDQmtCb0JDQnSIpLnRvVXBwZXJDYXNlKCk7CiAgICQoIiNjbGFuTmFtZSIpLmNoaWxkTm9kZXNbMF0udGV4dENvbnRlbnQ9Yy5uYW1lKyIgIjskKCIjY2xhblRhZyIpLnRleHRDb250ZW50PSIjIitjLnRhZzsKICAgY29uc3QgZGV0YWlsPWF3YWl0IGFwaShgL2NsYW5zLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGMuY2xhbl9pZCl9YCksbWVtYmVycz1kZXRhaWwubWVtYmVyc3x8W107CiAgICQoIiNjbGFuTWVtYmVycyIpLmlubmVySFRNTD1tZW1iZXJzLm1hcChtPT5gPGRpdiBjbGFzcz0ibGVhZGVyIj48ZGl2IGNsYXNzPSJwbGF5ZXIiPjxkaXYgY2xhc3M9Im1pbmlhdmEiPiR7ZXNjKChtLm5pY2tuYW1lfHwiPyIpWzBdLnRvVXBwZXJDYXNlKCkpfTwvZGl2PjxkaXY+PGI+JHtlc2MobS5uaWNrbmFtZXx8bS50ZWxlZ3JhbV9pZCl9PC9iPjxzbWFsbD4ke2VzYyhtLnJvbGUpfTwvc21hbGw+PC9kaXY+PC9kaXY+PHNwYW4gY2xhc3M9ImVsbyI+JHtOdW1iZXIobS5lbG98fDEwMDApfTwvc3Bhbj48L2Rpdj5gKS5qb2luKCIiKXx8JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0JIg0LrQu9Cw0L3QtSDQv9C+0LrQsCDQvdC10YIg0YPRh9Cw0YHRgtC90LjQutC+0LIuPC9wPic7CiAgIGNvbnN0IGNvdW50PSQoIi5jYXJkaGVhZCAubXV0ZWQuc21hbGwiLCQoIiNwYWdlLWNsYW5zIikpO2lmKGNvdW50KWNvdW50LnRleHRDb250ZW50PWAke21lbWJlcnMubGVuZ3RofSAvIDIwYDsKICB9KQogXTsKIGNvbnN0IHJlc3VsdHM9YXdhaXQgUHJvbWlzZS5hbGxTZXR0bGVkKGpvYnMpOwogY29uc3QgZXJyb3JzPXJlc3VsdHMuZmlsdGVyKHg9Pnguc3RhdHVzPT09InJlamVjdGVkIik7CiBpZihlcnJvcnMubGVuZ3RoKWNvbnNvbGUud2Fybigi0J3QtdC60L7RgtC+0YDRi9C1INGA0LDQt9C00LXQu9GLIEFQSSDQvdC1INC30LDQs9GA0YPQt9C40LvQuNGB0Yw6IixlcnJvcnMubWFwKHg9PngucmVhc29uPy5tZXNzYWdlKSk7CiByZW5kZXIoKTsKfQphc3luYyBmdW5jdGlvbiBpbml0QXBpKCl7CiBjb25zdCB0Zz1nZXRUZWxlZ3JhbVVzZXIoKTthcGlVc2VySWQ9U3RyaW5nKHRnPy5pZHx8bG9jYWxTdG9yYWdlLmdldEl0ZW0oInN0YW5kcG9pbnRUZWxlZ3JhbUlkIil8fCIiKS50cmltKCk7CiBpZih0Zz8uaWQpe2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJzdGFuZHBvaW50VGVsZWdyYW1JZCIsYXBpVXNlcklkKTtpZih0Zy51c2VybmFtZSlzdGF0ZS5uYW1lPXRnLnVzZXJuYW1lO30KIC8vINCf0YDQvtCy0LXRgNGP0LXQvCDQtNC+0YHRgtGD0L/QvdC+0YHRgtGMINGB0LXRgNCy0LXRgNCwINC+0YLQtNC10LvRjNC90L4g0L7RgiDRgNC10LPQuNGB0YLRgNCw0YbQuNC4INC/0YDQvtGE0LjQu9GPLgogdHJ5ewogIGF3YWl0IGFwaSgiL2NvbmZpZyIpOwogIHNldEFwaVN0YXR1cygiQVBJOiDRgdC10YDQstC10YAg0L7QvdC70LDQudC9Iix0cnVlKTsKICBjb25zdCBxbj0kKCIjcXVldWVBcGlOb3RlIik7aWYocW4pcW4udGV4dENvbnRlbnQ9ItCh0LXRgNCy0LXRgCDQtNC+0YHRgtGD0L/QtdC9LiDQlNC70Y8g0L/QvtC40YHQutCwINC80LDRgtGH0LAg0L3Rg9C20LXQvSDQv9GA0L7RhNC40LvRjCBUZWxlZ3JhbSDQuCDRg9GB0L/QtdGI0L3QsNGPINGA0LXQs9C40YHRgtGA0LDRhtC40Y8uIjsKIH1jYXRjaChlKXtzZXRBcGlTdGF0dXMoIkFQSTog0L7RiNC40LHQutCwINGB0L7QtdC00LjQvdC10L3QuNGPIixmYWxzZSk7Y29uc29sZS5lcnJvcigiQVBJIGNvbmZpZzoiLGUpO3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC/0L7QtNC60LvRjtGH0LjRgtGM0YHRjyDQuiDRgdC10YDQstC10YDRgzogIitlLm1lc3NhZ2UpO3JldHVybjt9CiBpZighYXBpVXNlcklkKXtzZXRBcGlTdGF0dXMoIkFQSTog0L7RgtC60YDQvtC5INCyIFRlbGVncmFtIixmYWxzZSk7Y29uc3QgcW49JCgiI3F1ZXVlQXBpTm90ZSIpO2lmKHFuKXFuLnRleHRDb250ZW50PSLQntGC0LrRgNC+0Lkg0LzQuNC90Lgt0L/RgNC40LvQvtC20LXQvdC40LUg0YfQtdGA0LXQtyBUZWxlZ3JhbSwg0YfRgtC+0LHRiyDQv9C+0LvRg9GH0LjRgtGMIFRlbGVncmFtIElEINC4INC40YHQutCw0YLRjCDQvNCw0YLRhy4iO3JldHVybjt9CiB0cnl7CiAgdHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWApO2FwcGx5UGxheWVyKGQucGxheWVyfHxkKTt9CiAgY2F0Y2goZSl7CiAgIGlmKGUuc3RhdHVzIT09NDA0ICYmICEvSFRUUCA0MDR80L3QtSDQvdCw0LnQtNC10L0vaS50ZXN0KGUubWVzc2FnZSkpdGhyb3cgZTsKICAgY29uc3Qgbmlja25hbWU9KHRnPy51c2VybmFtZXx8c3RhdGUubmFtZXx8ItCY0LPRgNC+0LoiKS5zbGljZSgwLDI0KSxnYW1lX2lkPVN0cmluZyhzdGF0ZS5nYW1lSWR8fCgic3BfIithcGlVc2VySWQuc2xpY2UoLTgpKSkuc2xpY2UoMCwzMik7CiAgIGNvbnN0IGQ9YXdhaXQgYXBpKCIvcGxheWVycy9yZWdpc3RlciIse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLG5pY2tuYW1lLGdhbWVfaWR9fSk7YXBwbHlQbGF5ZXIoZC5wbGF5ZXJ8fGQpOwogIH0KICBhd2FpdCByZWZyZXNoQXBpRGF0YSgpOwogIGF3YWl0IHJlZnJlc2hQYXNzQ3VycmVuY3koKTsKIH1jYXRjaChlKXsKICAvLyDQodC10YDQstC10YAg0YPQttC1INC/0L7QtNGC0LLQtdGA0LTQuNC7INC00L7RgdGC0YPQv9C90L7RgdGC0Ywg0YfQtdGA0LXQtyAvY29uZmlnIOKAlCDQvdC1INC90LDQt9GL0LLQsNC10Lwg0Y3RgtC+INC+0LHRidC10Lkg0L7RiNC40LHQutC+0LkgQVBJLgogIHNldEFwaVN0YXR1cygiQVBJINC+0L3Qu9Cw0LnQvSDCtyDQvtGI0LjQsdC60LAg0L/RgNC+0YTQuNC70Y8iLGZhbHNlKTtjb25zb2xlLmVycm9yKCJQcm9maWxlIGluaXQ6IixlKTsKICBjb25zdCBxbj0kKCIjcXVldWVBcGlOb3RlIik7aWYocW4pcW4udGV4dENvbnRlbnQ9ItCh0LXRgNCy0LXRgCDQtNC+0YHRgtGD0L/QtdC9LCDQvdC+INC90LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNCz0YDRg9C30LjRgtGMINC/0YDQvtGE0LjQu9GMLiDQntGC0LrRgNC+0LkgwqvQn9GA0L7RhNC40LvRjMK7LCDQv9GA0L7QstC10YDRjCDQvdC40LrQvdC10LnQvCDQuCDQuNCz0YDQvtCy0L7QuSBJRCDQuCDRgdC+0YXRgNCw0L3QuCDQtdGJ0ZEg0YDQsNC3LiI7CiAgdG9hc3QoItCd0LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNCz0YDRg9C30LjRgtGMINC/0YDQvtGE0LjQu9GMOiAiK2UubWVzc2FnZSk7CiB9Cn0KCgovLyA9PT09PSBDbGFuIFdhcnM6IEFQSS1iYWNrZWQgY2hhbGxlbmdlIGFuZCBsb2JieSA9PT09PQpsZXQgYWN0aXZlV2FySWQ9IiI7CmFzeW5jIGZ1bmN0aW9uIGxvYWRXYXJMb2JieShpZCl7aWYoIWlkfHwhYXBpVXNlcklkKXJldHVybiB0b2FzdCgi0J3Rg9C20LXQvSBJRCBDbGFuIFdhciDQuCDQv9C+0LTQutC70Y7Rh9GR0L3QvdGL0Lkg0L/RgNC+0YTQuNC70YwuIik7dHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvY2xhbi13YXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGlkKX0vbG9iYnlgKTthY3RpdmVXYXJJZD1pZDskKCIjd2FySWRJbnB1dCIpLnZhbHVlPWlkO2NvbnN0IHc9ZC53YXIsdGVhbUE9ZC50ZWFtcz8uQXx8W10sdGVhbUI9ZC50ZWFtcz8uQnx8W107JCgiI3dhckxvYmJ5Q29udGVudCIpLmlubmVySFRNTD1gPGRpdiBjbGFzcz0ibWMtdHdvIj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+foCAke2VzYyh3LmNsYW5fYV9uYW1lKX0gIyR7ZXNjKHcuY2xhbl9hX3RhZyl9PC9oND48dWw+JHt0ZWFtQS5tYXAocD0+YDxsaT4ke2VzYyhwLm5pY2tuYW1lKX0gwrcgJHtOdW1iZXIocC5lbG8pfSBFTE88L2xpPmApLmpvaW4oIiIpfHwiPGxpPtCh0L7RgdGC0LDQsiDQvdC1INC30LDQs9GA0YPQttC10L08L2xpPiJ9PC91bD48L2Rpdj48ZGl2IGNsYXNzPSJtYy10ZWFtIj48aDQ+8J+UtSAke2VzYyh3LmNsYW5fYl9uYW1lKX0gIyR7ZXNjKHcuY2xhbl9iX3RhZyl9PC9oND48dWw+JHt0ZWFtQi5tYXAocD0+YDxsaT4ke2VzYyhwLm5pY2tuYW1lKX0gwrcgJHtOdW1iZXIocC5lbG8pfSBFTE88L2xpPmApLmpvaW4oIiIpfHwiPGxpPtCh0L7RgdGC0LDQsiDQvdC1INC30LDQs9GA0YPQttC10L08L2xpPiJ9PC91bD48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJtYy1ub3RpY2UiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPtCh0YLQsNGC0YPRgTogPGI+JHtlc2Mody5zdGF0dXMpfTwvYj4gwrcgSUQgJHtlc2Mody53YXJfaWQpfSR7dy5zdGF0dXM9PT0iYWNjZXB0ZWQiPyIgwrcg0JLRi9C30L7QsiDQv9GA0LjQvdGP0YIsINC80L7QttC90L4g0L/RgNC+0LLQtdGB0YLQuCDRgdC10YDQuNGOINC4INC+0YLQv9GA0LDQstC40YLRjCDRgdGH0ZHRgi4iOiIifTwvZGl2PiR7dy5zdGF0dXM9PT0icGVuZGluZyI/YDxkaXYgY2xhc3M9Im1jLWFjdGlvbnMiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPjxidXR0b24gY2xhc3M9ImJ0bi1wcmltYXJ5IiBpZD0iYWNjZXB0V2FyQnRuIj7Qn9GA0LjQvdGP0YLRjCDQstGL0LfQvtCyPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgaWQ9InJlamVjdFdhckJ0biI+0J7RgtC60LvQvtC90LjRgtGMPC9idXR0b24+PC9kaXY+YDoiIn0ke3cuc3RhdHVzPT09ImFjY2VwdGVkIj9gPGRpdiBjbGFzcz0ibWMtZm9ybWdyaWQiIHN0eWxlPSJtYXJnaW4tdG9wOjEwcHgiPjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQm9CQ0J3QkCBBPC9sYWJlbD48aW5wdXQgaWQ9IndhclNjb3JlQSIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iMTMiPjwvZGl2PjxkaXYgY2xhc3M9Im1jLWZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQm9CQ0J3QkCBCPC9sYWJlbD48aW5wdXQgaWQ9IndhclNjb3JlQiIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iOSI+PC9kaXY+PC9kaXY+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGlkPSJzdWJtaXRXYXJTY29yZUJ0biIgc3R5bGU9IndpZHRoOjEwMCUiPtCe0YLQv9GA0LDQstC40YLRjCDRgNC10LfRg9C70YzRgtCw0YIg0LzQvtC00LXRgNCw0YLQvtGA0YM8L2J1dHRvbj5gOiIifWA7Y29uc3QgYWNjZXB0PSQoIiNhY2NlcHRXYXJCdG4iKTtpZihhY2NlcHQpYWNjZXB0Lm9uY2xpY2s9KCk9PmRlY2lkZVdhcigiYWNjZXB0Iik7Y29uc3QgcmVqZWN0PSQoIiNyZWplY3RXYXJCdG4iKTtpZihyZWplY3QpcmVqZWN0Lm9uY2xpY2s9KCk9PmRlY2lkZVdhcigicmVqZWN0Iik7Y29uc3Qgc3VibWl0PSQoIiNzdWJtaXRXYXJTY29yZUJ0biIpO2lmKHN1Ym1pdClzdWJtaXQub25jbGljaz1hc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL2NsYW4td2Fycy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVXYXJJZCl9L3Jlc3VsdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHNjb3JlX2E6TnVtYmVyKCQoIiN3YXJTY29yZUEiKS52YWx1ZSksc2NvcmVfYjpOdW1iZXIoJCgiI3dhclNjb3JlQiIpLnZhbHVlKX19KTt0b2FzdCgi0KDQtdC30YPQu9GM0YLQsNGCIENsYW4gV2FyINC+0YLQv9GA0LDQstC70LXQvSDQvdCwINC/0YDQvtCy0LXRgNC60YMuIik7YXdhaXQgbG9hZFdhckxvYmJ5KGFjdGl2ZVdhcklkKTt9Y2F0Y2goZSl7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgiBDbGFuIFdhcjogIitlLm1lc3NhZ2UpO319OyQoIiN3YXJTdGF0dXNUZXh0IikudGV4dENvbnRlbnQ9YENsYW4gV2FyICR7dy53YXJfaWR9OiAke3cuc3RhdHVzfWA7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC+0YLQutGA0YvRgtGMINC70L7QsdCx0LggQ2xhbiBXYXI6ICIrZS5tZXNzYWdlKTt9fQphc3luYyBmdW5jdGlvbiBkZWNpZGVXYXIoZGVjaXNpb24pe3RyeXthd2FpdCBhcGkoYC9jbGFuLXdhcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlV2FySWQpfS9kZWNpc2lvbmAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGRlY2lzaW9ufX0pO3RvYXN0KGRlY2lzaW9uPT09ImFjY2VwdCI/ItCS0YvQt9C+0LIg0L/RgNC40L3Rj9GCLiI6ItCS0YvQt9C+0LIg0L7RgtC60LvQvtC90ZHQvS4iKTthd2FpdCBsb2FkV2FyTG9iYnkoYWN0aXZlV2FySWQpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtCx0YDQsNCx0L7RgtCw0YLRjCDQstGL0LfQvtCyOiAiK2UubWVzc2FnZSk7fX0KJCgiI2NoYWxsZW5nZVdhckJ0biIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2NvbnN0IG9wcG9uZW50X2NsYW5faWQ9JCgiI3dhck9wcG9uZW50SWQiKS52YWx1ZS50cmltKCk7aWYoIWFwaVVzZXJJZHx8IW9wcG9uZW50X2NsYW5faWQpcmV0dXJuIHRvYXN0KCLQo9C60LDQttC4IElEINC60LvQsNC90LAg0YHQvtC/0LXRgNC90LjQutCwINC4INC/0L7QtNC60LvRjtGH0Lgg0L/RgNC+0YTQuNC70YwuIik7dHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKCIvY2xhbi13YXJzIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsb3Bwb25lbnRfY2xhbl9pZH19KTthY3RpdmVXYXJJZD1kLndhcl9pZDskKCIjd2FySWRJbnB1dCIpLnZhbHVlPWQud2FyX2lkO3RvYXN0KCLQktGL0LfQvtCyIENsYW4gV2FyINC+0YLQv9GA0LDQstC70LXQvS4iKTthd2FpdCBsb2FkV2FyTG9iYnkoZC53YXJfaWQpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDRgdC+0LfQtNCw0YLRjCBDbGFuIFdhcjogIitlLm1lc3NhZ2UpO319KTsKJCgiI2xvYWRXYXJMb2JieUJ0biIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsKCk9PmxvYWRXYXJMb2JieSgkKCIjd2FySWRJbnB1dCIpLnZhbHVlLnRyaW0oKSkpOwoKLy8gPT09PT0gTWF0Y2ggQ2VudGVyOiBhY2NlcHQgLT4gY2FwdGFpbiBkcmFmdCAtPiBiYW4vcGljayAtPiBsb2JieSAtPiByZXN1bHQgLT4gYXBwZWFsID09PT09CmxldCBtYXRjaFBvbGxUaW1lcj1udWxsLGN1cnJlbnRNYXRjaD1udWxsOwpjb25zdCBNQVRDSF9NQVBTPVsiQnJlZXplIiwiQ2FseXBzbyIsIkR1bmUiLCJQcm92aW5jZSIsIlJ1c3QiLCJTYWt1cmEiLCJTYW5kc3RvbmUiLCJab25lIDciXTsKZnVuY3Rpb24gc3RvcFF1ZXVlUG9sbGluZygpe2lmKHF1ZXVlUG9sbFRpbWVyKXtjbGVhckludGVydmFsKHF1ZXVlUG9sbFRpbWVyKTtxdWV1ZVBvbGxUaW1lcj1udWxsO319CmZ1bmN0aW9uIHN0b3BNYXRjaFBvbGxpbmcoKXtpZihtYXRjaFBvbGxUaW1lcil7Y2xlYXJJbnRlcnZhbChtYXRjaFBvbGxUaW1lcik7bWF0Y2hQb2xsVGltZXI9bnVsbDt9fQpmdW5jdGlvbiBtY1Nob3coaWQpe1sibWNBY2NlcHRQYW5lbCIsIm1jQmFuUGFuZWwiLCJtY0xvYmJ5UGFuZWwiLCJtY1BsYXlpbmdQYW5lbCIsIm1jUmV2aWV3UGFuZWwiXS5mb3JFYWNoKHg9PiQoIiMiK3gpLmNsYXNzTGlzdC50b2dnbGUoIm1jLWhpZGRlbiIseCE9PWlkKSk7JCgiI21hdGNoQ2VudGVyIikuY2xhc3NMaXN0LmFkZCgidmlzaWJsZSIpO30KZnVuY3Rpb24gbWF0Y2hQbGF5ZXJOYW1lKGlkKXtyZXR1cm4gKGN1cnJlbnRNYXRjaD8ucGxheWVyX3Byb2ZpbGVzPy5baWRdPy5uaWNrbmFtZSl8fCgi0JjQs9GA0L7QuiAiK1N0cmluZyhpZHx8IiIpLnNsaWNlKC00KSk7fQpmdW5jdGlvbiByZW5kZXJNY1BsYXllcnMoKXtjb25zdCBwbGF5ZXJzPWN1cnJlbnRNYXRjaD8ucGxheWVyc3x8W10sYWNjZXB0YW5jZT1jdXJyZW50TWF0Y2g/LmFjY2VwdGFuY2V8fHt9OyQoIiNtY0FjY2VwdFBsYXllcnMiKS5pbm5lckhUTUw9cGxheWVycy5tYXAoKGlkLGkpPT57Y29uc3QgYWNjZXB0ZWQ9ISFhY2NlcHRhbmNlW2lkXTtyZXR1cm4gYDxkaXYgY2xhc3M9Im1jLXBsYXllciAke2FjY2VwdGVkPyJhY2NlcHRlZCI6IiJ9Ij48ZGl2IGNsYXNzPSJtYy1hdmF0YXIiPiR7YWNjZXB0ZWQ/IuKckyI6aSsxfTwvZGl2PjxiPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfTwvYj48c21hbGw+JHthY2NlcHRlZD8i0J/QoNCY0J3Qr9CbIjoi0J7QltCY0JTQkNCd0JjQlSJ9PC9zbWFsbD48L2Rpdj5gfSkuam9pbigiIik7Y29uc3Qgbj1OdW1iZXIoY3VycmVudE1hdGNoPy5hY2NlcHRlZF9jb3VudHx8MCksdG90YWw9TnVtYmVyKGN1cnJlbnRNYXRjaD8ucGxheWVyc19uZWVkZWR8fHBsYXllcnMubGVuZ3RofHwwKTskKCIjbWNBY2NlcHRUZXh0IikudGV4dENvbnRlbnQ9YNCf0YDQuNC90Y/Qu9C4ICR7bn0g0LjQtyAke3RvdGFsfS4g0JTQu9GPINGB0YLQsNGA0YLQsCDQstGB0LUg0YPRh9Cw0YHRgtC90LjQutC4INC00L7Qu9C20L3RiyDQv9C+0LTRgtCy0LXRgNC00LjRgtGMINC80LDRgtGHLmA7JCgiI21jQWNjZXB0UHJvZ3Jlc3MiKS5zdHlsZS53aWR0aD0odG90YWw/TWF0aC5taW4oMTAwLG4vdG90YWwqMTAwKTowKSsiJSI7JCgiI21jQWNjZXB0QnRuIikuZGlzYWJsZWQ9ISFhY2NlcHRhbmNlW2FwaVVzZXJJZF07JCgiI21jQWNjZXB0QnRuIikudGV4dENvbnRlbnQ9YWNjZXB0YW5jZVthcGlVc2VySWRdPyLinJMg0KLRiyDQv9GA0LjQvdGP0Lsg0LzQsNGC0YciOiLinJMg0J/RgNC40L3Rj9GC0Ywg0LzQsNGC0YciO30KZnVuY3Rpb24gZmlsbENhcHRhaW5TZWxlY3RzKCl7Y29uc3QgcGxheWVycz1jdXJyZW50TWF0Y2g/LnBsYXllcnN8fFtdLHNpemU9KHsiMXYxIjoxLCIydjIiOjIsIjN2MyI6MywiNXY1Ijo1fSlbY3VycmVudE1hdGNoPy5tb2RlXXx8NSx0ZWFtcz17QTpwbGF5ZXJzLnNsaWNlKDAsc2l6ZSksQjpwbGF5ZXJzLnNsaWNlKHNpemUpfTtbIkEiLCJCIl0uZm9yRWFjaCh0PT57Y29uc3Qgc2VsPSQoIiNtY0NhcHRhaW4iK3QpLGlkcz10ZWFtc1t0XTtzZWwuaW5uZXJIVE1MPWlkcy5tYXAoaWQ9PmA8b3B0aW9uIHZhbHVlPSIke2VzYyhpZCl9Ij4ke2VzYyhtYXRjaFBsYXllck5hbWUoaWQpKX08L29wdGlvbj5gKS5qb2luKCIiKTtjb25zdCBjYXB0YWluPWN1cnJlbnRNYXRjaD8uW3Q9PT0iQSI/ImNhcHRhaW5faWQiOiJjYXB0YWluX2JfaWQiXTtpZihjYXB0YWluKXNlbC52YWx1ZT1jYXB0YWluOyQoIiNtY0NhcHRhaW4iK3QrIkluZm8iKS50ZXh0Q29udGVudD1jYXB0YWluPyLQmtCw0L/QuNGC0LDQvSDQvdCw0LfQvdCw0YfQtdC9OiAiK21hdGNoUGxheWVyTmFtZShjYXB0YWluKToi0JrQsNC/0LjRgtCw0L0g0LXRidGRINC90LUg0LLRi9Cx0YDQsNC9Ijt9KTt9CmZ1bmN0aW9uIHJlbmRlck1hcFBpY2soKXtjb25zdCBhY3Rpb25zPWN1cnJlbnRNYXRjaD8ubWFwX2FjdGlvbnN8fFtdLGJhbnM9YWN0aW9ucy5maWx0ZXIoYT0+YS5hY3Rpb249PT0iYmFuIikubGVuZ3RoLGZpbmlzaGVkPSEhY3VycmVudE1hdGNoPy5tYXBfbmFtZTskKCIjbWNCYW5UaXRsZSIpLnRleHRDb250ZW50PWZpbmlzaGVkPyLQmtCw0YDRgtCwINCy0YvQsdGA0LDQvdCwOiAiK2N1cnJlbnRNYXRjaC5tYXBfbmFtZTpgQmFuIC8gUGljayDCtyAke01hdGgubWluKGJhbnMsNCl9LzQg0LHQsNC90L7QsmA7Y29uc3QgZXhwZWN0ZWQ9YmFucyUyPT09MD9jdXJyZW50TWF0Y2g/LmNhcHRhaW5faWQ6KGN1cnJlbnRNYXRjaD8uY2FwdGFpbl9iX2lkfHxjdXJyZW50TWF0Y2g/LnBsYXllcnM/LlsoeyIxdjEiOjEsIjJ2MiI6MiwiM3YzIjozLCI1djUiOjV9KVtjdXJyZW50TWF0Y2g/Lm1vZGVdfHw1XSk7Y29uc3QgY2FuQWN0PSFmaW5pc2hlZCYmYXBpVXNlcklkPT09ZXhwZWN0ZWQ7JCgiI21jQmFuSGludCIpLnRleHRDb250ZW50PWZpbmlzaGVkPyJCYW4tcGljayDQt9Cw0LLQtdGA0YjRkdC9LiDQn9C10YDQtdGF0L7QtNC4INCyINC70L7QsdCx0Lgg0LzQsNGC0YfQsC4iOmJhbnM8ND9g0KHQu9C10LTRg9GO0YnQuNC5INCx0LDQvTogJHttYXRjaFBsYXllck5hbWUoZXhwZWN0ZWQpfSR7Y2FuQWN0PyIg4oCUINGC0LLQvtC5INGF0L7QtCI6IiDigJQg0L7QttC40LTQsNC10Lwg0LrQsNC/0LjRgtCw0L3QsCJ9LmA6YNCR0LDQvdGLINC30LDQstC10YDRiNC10L3Riy4g0JrQsNC/0LjRgtCw0L0gQSDQstGL0LHQuNGA0LDQtdGCINC60LDRgNGC0YMke2FwaVVzZXJJZD09PWN1cnJlbnRNYXRjaD8uY2FwdGFpbl9pZD8iIOKAlCDQstGL0LHQtdGA0Lgg0LrQsNGA0YLRgyDQvdC40LbQtSI6Ii4ifWA7Y29uc3QgdXNlZD1uZXcgU2V0KGFjdGlvbnMubWFwKGE9PmEubWFwX25hbWUpKTskKCIjbWNNYXBCdXR0b25zIikuaW5uZXJIVE1MPU1BVENIX01BUFMubWFwKG5hbWU9PntsZXQgYWN0aW9uPSJiYW4iO2lmKGJhbnM+PTQpYWN0aW9uPSJwaWNrIjtyZXR1cm4gYDxidXR0b24gY2xhc3M9Im1jLW1hcCAke3VzZWQuaGFzKG5hbWUpPyJ1c2VkIjoiIn0iIGRhdGEtbWMtbWFwPSIke2VzYyhuYW1lKX0iIGRhdGEtbWMtYWN0aW9uPSIke2FjdGlvbn0iICR7KCFjYW5BY3R8fGZpbmlzaGVkfHx1c2VkLmhhcyhuYW1lKSk/ImRpc2FibGVkIjoiIn0+JHtlc2MobmFtZSl9PHNtYWxsIHN0eWxlPSJkaXNwbGF5OmJsb2NrO2NvbG9yOiM5ZGE5YjU7bWFyZ2luLXRvcDo0cHgiPiR7dXNlZC5oYXMobmFtZSk/KGFjdGlvbnMuZmluZChhPT5hLm1hcF9uYW1lPT09bmFtZSk/LmFjdGlvbj09PSJiYW4iPyLQl9CQ0JHQkNCd0JXQndCQIjoi0JLQq9CR0KDQkNCd0JAiKTphY3Rpb249PT0iYmFuIj8iQkFOIjoiUElDSyJ9PC9zbWFsbD48L2J1dHRvbj5gfSkuam9pbigiIik7JCgiI21jTWFwTG9nIikuaW5uZXJIVE1MPWFjdGlvbnMubWFwKChhLGkpPT5gPGRpdiBjbGFzcz0ibWMtbG9ncm93Ij48c3Bhbj4ke2krMX0uICR7ZXNjKGEuYWN0aW9uPT09ImJhbiI/IkJBTiI6IlBJQ0siKX0gwrcgJHtlc2MoYS5tYXBfbmFtZSl9PC9zcGFuPjxzcGFuPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShhLmJ5KSl9PC9zcGFuPjwvZGl2PmApLmpvaW4oIiIpfHwnPHAgY2xhc3M9Im1jLW11dGVkIj7Qn9C+0LrQsCDQvdC10YIg0LTQtdC50YHRgtCy0LjQuSDQvdCwINC60LDRgNGC0LUuPC9wPic7JCQoIltkYXRhLW1jLW1hcF0iKS5mb3JFYWNoKGI9PmIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vbWFwLWFjdGlvbmAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGFjdGlvbjpiLmRhdGFzZXQubWNBY3Rpb24sbWFwX25hbWU6Yi5kYXRhc2V0Lm1jTWFwfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO31jYXRjaChlKXt0b2FzdCgiQmFuL1BpY2s6ICIrZS5tZXNzYWdlKTt9fSkpO30KZnVuY3Rpb24gcmVuZGVyTWF0Y2goKXtpZighY3VycmVudE1hdGNoKXJldHVybjtjb25zdCBtPWN1cnJlbnRNYXRjaDskKCIjbWF0Y2hDZW50ZXIiKS5jbGFzc0xpc3QuYWRkKCJ2aXNpYmxlIik7JCgiI21jU3VidGl0bGUiKS50ZXh0Q29udGVudD1g0JzQsNGC0YcgJHttLm1hdGNoX2lkfSDCtyAke20ubW9kZX0gwrcgJHttLnBsYXllcnM/Lmxlbmd0aHx8MH0g0LjQs9GA0L7QutC+0LJgOyQoIiNtY1N0YXR1cyIpLnRleHRDb250ZW50PVN0cmluZyhtLnN0YXR1c3x8Ik1BVENIIikucmVwbGFjZUFsbCgiXyIsIiAiKS50b1VwcGVyQ2FzZSgpOyQoIiNtY1RpdGxlIikudGV4dENvbnRlbnQ9KHthY2NlcHRpbmc6ItCc0LDRgtGHINC90LDQudC00LXQvSDigJQg0L/QvtC00YLQstC10YDQtNC4IixtYXBfcGljazoiQmFuIC8gUGljayDQutCw0YDRgiIscmVhZHk6ItCb0L7QsdCx0Lgg0LPQvtGC0L7QstC+IixwbGF5aW5nOiLQnNCw0YLRhyDQsiDQuNCz0YDQtSIsdW5kZXJfcmV2aWV3OiLQoNC10LfRg9C70YzRgtCw0YIg0L3QsCDQv9GA0L7QstC10YDQutC1Iixjb25maXJtZWQ6ItCc0LDRgtGHINC/0L7QtNGC0LLQtdGA0LbQtNGR0L0iLHJlamVjdGVkOiLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC60LvQvtC90ZHQvSIsZGlzcHV0ZWQ6ItCg0LXQt9GD0LvRjNGC0LDRgiDQvtGB0L/QsNGA0LjQstCw0LXRgtGB0Y8iLGRlY2xpbmVkOiLQnNCw0YLRhyDQvtGC0LrQu9C+0L3RkdC9IixleHBpcmVkOiLQktGA0LXQvNGPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPINC40YHRgtC10LrQu9C+In0pW20uc3RhdHVzXXx8ItCc0LDRgtGHIjsKIGlmKG0uc3RhdHVzPT09ImFjY2VwdGluZyIpe21jU2hvdygibWNBY2NlcHRQYW5lbCIpO3JlbmRlck1jUGxheWVycygpO3JldHVybjt9CiBpZihtLnN0YXR1cz09PSJtYXBfcGljayIpe21jU2hvdygibWNCYW5QYW5lbCIpO2ZpbGxDYXB0YWluU2VsZWN0cygpO3JlbmRlck1hcFBpY2soKTtyZXR1cm47fQogaWYobS5zdGF0dXM9PT0icmVhZHkiKXttY1Nob3coIm1jTG9iYnlQYW5lbCIpO2NvbnN0IHNpemU9KHsiMXYxIjoxLCIydjIiOjIsIjN2MyI6MywiNXY1Ijo1fSlbbS5tb2RlXXx8NSx0YT0obS5wbGF5ZXJzfHxbXSkuc2xpY2UoMCxzaXplKSx0Yj0obS5wbGF5ZXJzfHxbXSkuc2xpY2Uoc2l6ZSk7JCgiI21jVGVhbUEiKS5pbm5lckhUTUw9dGEubWFwKGlkPT5gPGxpPiR7ZXNjKG1hdGNoUGxheWVyTmFtZShpZCkpfSR7aWQ9PT1tLmNhcHRhaW5faWQ/IiDCtyDQmtCQ0J/QmNCi0JDQnSI6IiJ9PC9saT5gKS5qb2luKCIiKTskKCIjbWNUZWFtQiIpLmlubmVySFRNTD10Yi5tYXAoaWQ9PmA8bGk+JHtlc2MobWF0Y2hQbGF5ZXJOYW1lKGlkKSl9JHtpZD09PW0uY2FwdGFpbl9iX2lkPyIgwrcg0JrQkNCf0JjQotCQ0J0iOiIifTwvbGk+YCkuam9pbigiIik7JCgiI21jTG9iYnlJbmZvIikudGV4dENvbnRlbnQ9YNCa0LDRgNGC0LA6ICR7bS5tYXBfbmFtZXx8ItC90LUg0LLRi9Cx0YDQsNC90LAifS4g0JrQsNC/0LjRgtCw0L0g0LrQvtC80LDQvdC00YsgQSDQt9Cw0L/Rg9GB0LrQsNC10YIg0LzQsNGC0YcuYDskKCIjbWNTdGFydEJ0biIpLmRpc2FibGVkPWFwaVVzZXJJZCE9PW0uY2FwdGFpbl9pZDskKCIjbWNTdGFydEJ0biIpLnRleHRDb250ZW50PWFwaVVzZXJJZD09PW0uY2FwdGFpbl9pZD8i4pa2INCd0LDRh9Cw0YLRjCDQvNCw0YLRhyI6ItCe0LbQuNC00LDQtdC8INC60LDQv9C40YLQsNC90LAg0LrQvtC80LDQvdC00YsgQSI7cmV0dXJuO30KIGlmKG0uc3RhdHVzPT09InBsYXlpbmciKXttY1Nob3coIm1jUGxheWluZ1BhbmVsIik7cmV0dXJuO30KIGlmKFsidW5kZXJfcmV2aWV3IiwiY29uZmlybWVkIiwicmVqZWN0ZWQiLCJkaXNwdXRlZCJdLmluY2x1ZGVzKG0uc3RhdHVzKSl7bWNTaG93KCJtY1Jldmlld1BhbmVsIik7JCgiI21jUmV2aWV3SW5mbyIpLnRleHRDb250ZW50PW0uc3RhdHVzPT09ImNvbmZpcm1lZCI/YNCg0LXQt9GD0LvRjNGC0LDRgiDQv9C+0LTRgtCy0LXRgNC20LTRkdC9OiAke20uc2NvcmVfYT8/IuKAlCJ9OiR7bS5zY29yZV9iPz8i4oCUIn0uINCg0LXQudGC0LjQvdCzINC80L7QttC10YIg0LHRi9GC0Ywg0L7QsdC90L7QstC70ZHQvSDQvNC+0LTQtdGA0LDRhtC40LXQuS5gOm0uc3RhdHVzPT09InJlamVjdGVkIj8i0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQutC70L7QvdGR0L0g0LzQvtC00LXRgNCw0YLQvtGA0L7QvC4g0J/RgNC+0LLQtdGA0Ywg0YPQstC10LTQvtC80LvQtdC90LjRjy4iOm0uc3RhdHVzPT09ImRpc3B1dGVkIj8i0KDQtdC30YPQu9GM0YLQsNGCINC+0YHQv9Cw0YDQuNCy0LDQtdGC0YHRjy4g0JzQvtC00LXRgNCw0YLQvtGA0YMg0YLRgNC10LHRg9C10YLRgdGPINC/0YDQvtCy0LXRgNC60LAuIjoi0KDQtdC30YPQu9GM0YLQsNGCINC+0YLQv9GA0LDQstC70LXQvSDQvdCwINC/0YDQvtCy0LXRgNC60YMg0LzQvtC00LXRgNCw0YLQvtGA0YMuIEVMTyDQvdC1INC80LXQvdGP0LXRgtGB0Y8g0LTQviDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy4iOyQoIiNtY0FwcGVhbEZvcm0iKS5jbGFzc0xpc3QudG9nZ2xlKCJtYy1oaWRkZW4iLCFbImNvbmZpcm1lZCIsInJlamVjdGVkIiwiZGlzcHV0ZWQiXS5pbmNsdWRlcyhtLnN0YXR1cykpO3JldHVybjt9CiBtY1Nob3coIm1jUmV2aWV3UGFuZWwiKTskKCIjbWNSZXZpZXdJbmZvIikudGV4dENvbnRlbnQ9ItCc0LDRgtGHINC30LDQstC10YDRiNC40Lsg0L/RgNC+0YbQtdGB0YEg0L/RgNC40L3Rj9GC0LjRjzogIittLnN0YXR1czsKfQphc3luYyBmdW5jdGlvbiBwb2xsQWN0aXZlTWF0Y2goKXtpZighYWN0aXZlTWF0Y2hJZClyZXR1cm47dHJ5e2NvbnN0IG09YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX1gKTtjdXJyZW50TWF0Y2g9bTtyZW5kZXJNYXRjaCgpO2lmKFsiY29uZmlybWVkIiwicmVqZWN0ZWQiLCJkZWNsaW5lZCIsImV4cGlyZWQiXS5pbmNsdWRlcyhtLnN0YXR1cykpc3RvcE1hdGNoUG9sbGluZygpO31jYXRjaChlKXtjb25zb2xlLndhcm4oIk1hdGNoIHBvbGwiLGUubWVzc2FnZSk7fX0KYXN5bmMgZnVuY3Rpb24gc2hvd01hdGNoKGlkKXthY3RpdmVNYXRjaElkPWlkO3F1ZXVlQWN0aXZlPWZhbHNlO3N0b3BRdWV1ZVBvbGxpbmcoKTskKCIjcXVldWVCdXR0b24iKS50ZXh0Q29udGVudD0i0J3QsNC50YLQuCDQvNCw0YLRhyI7bmF2KCJtYXRjaGVzIik7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7c3RvcE1hdGNoUG9sbGluZygpO21hdGNoUG9sbFRpbWVyPXNldEludGVydmFsKCgpPT57aWYoIWRvY3VtZW50LmhpZGRlbilwb2xsQWN0aXZlTWF0Y2goKX0sMzAwMCk7fQphc3luYyBmdW5jdGlvbiBzdGFydFF1ZXVlUG9sbGluZygpe3N0b3BRdWV1ZVBvbGxpbmcoKTtjb25zdCB1cGRhdGU9YXN5bmMoKT0+e2lmKCFxdWV1ZUFjdGl2ZXx8ZG9jdW1lbnQuaGlkZGVuKXJldHVybjt0cnl7Y29uc3QgcT1hd2FpdCBhcGkoYC9xdWV1ZS8ke2VuY29kZVVSSUNvbXBvbmVudChzZWxlY3RlZE1vZGUpfWApO2NvbnN0IGZvdW5kPU51bWJlcihxLnBsYXllcnNfZm91bmR8fDApLG5lZWRlZD1OdW1iZXIocS5wbGF5ZXJzX25lZWRlZHx8MCk7JCgiI3F1ZXVlVGl0bGUiKS50ZXh0Q29udGVudD0i0JjRidC10Lwg0YHQvtC/0LXRgNC90LjQutC+0LLigKYiOyQoIiNxdWV1ZURlc2NyaXB0aW9uIikudGV4dENvbnRlbnQ9YNCSINC+0YfQtdGA0LXQtNC4ICR7Zm91bmR9LyR7bmVlZGVkfTsg0L7RgdGC0LDQu9C+0YHRjCAke01hdGgubWF4KDAsbmVlZGVkLWZvdW5kKX1gO2NvbnN0IGFjdGl2ZT1hd2FpdCBhcGkoYC9wbGF5ZXJzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9L2FjdGl2ZS1tYXRjaGApO2lmKGFjdGl2ZS5hY3RpdmUmJmFjdGl2ZS5tYXRjaCl7YWN0aXZlTWF0Y2hJZD1hY3RpdmUubWF0Y2gubWF0Y2hfaWQ7YXdhaXQgc2hvd01hdGNoKGFjdGl2ZU1hdGNoSWQpO3RvYXN0KCLQnNCw0YLRhyDQvdCw0LnQtNC10L0hINCf0L7QtNGC0LLQtdGA0LTQuCDRg9GH0LDRgdGC0LjQtS4iKTt9fWNhdGNoKGUpe2NvbnNvbGUud2FybigiUXVldWUgc3RhdHVzIixlLm1lc3NhZ2UpO319O2F3YWl0IHVwZGF0ZSgpO3F1ZXVlUG9sbFRpbWVyPXNldEludGVydmFsKHVwZGF0ZSw0MDAwKTt9CiQoIiNtY0FjY2VwdEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57dHJ5e2F3YWl0IGFwaShgL21hdGNoZXMvJHtlbmNvZGVVUklDb21wb25lbnQoYWN0aXZlTWF0Y2hJZCl9L2FjY2VwdGAse21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLGFjY2VwdGVkOnRydWV9fSk7YXdhaXQgcG9sbEFjdGl2ZU1hdGNoKCk7dG9hc3QoItCi0Ysg0L/QvtC00YLQstC10YDQtNC40Lsg0LzQsNGC0YcuIik7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC/0YDQuNC90Y/RgtGMINC80LDRgtGHOiAiK2UubWVzc2FnZSk7fX0pOwokKCIjbWNEZWNsaW5lQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9PntpZighY29uZmlybSgi0J7RgtC60LvQvtC90LjRgtGMINC90LDQudC00LXQvdC90YvQuSDQvNCw0YLRhz8g0J7RgdGC0LDQu9GM0L3Ri9C1INC40LPRgNC+0LrQuCDQstC10YDQvdGD0YLRgdGPINCyINC/0L7QuNGB0LouIikpcmV0dXJuO3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9hY2NlcHRgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZCxhY2NlcHRlZDpmYWxzZX19KTtzdG9wTWF0Y2hQb2xsaW5nKCk7JCgiI21hdGNoQ2VudGVyIikuY2xhc3NMaXN0LnJlbW92ZSgidmlzaWJsZSIpO3RvYXN0KCLQnNCw0YLRhyDQvtGC0LrQu9C+0L3RkdC9LiIpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQvtGC0LrQu9C+0L3QuNGC0Yw6ICIrZS5tZXNzYWdlKTt9fSk7CmFzeW5jIGZ1bmN0aW9uIHNldENhcHRhaW4odGVhbSl7Y29uc3QgaWQ9JCgiI21jQ2FwdGFpbiIrdGVhbSkudmFsdWU7aWYoIWlkKXJldHVybjt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vY2FwdGFpbmAse21ldGhvZDoiUE9TVCIsYm9keTp7Y2FwdGFpbl9pZDppZCx0ZWFtfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQmtCw0L/QuNGC0LDQvSDQutC+0LzQsNC90LTRiyAiK3RlYW0rIiDQvdCw0LfQvdCw0YfQtdC9LiIpO31jYXRjaChlKXt0b2FzdCgi0JrQsNC/0LjRgtCw0L06ICIrZS5tZXNzYWdlKTt9fQokKCIjbWNTZXRDYXB0YWluQSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+c2V0Q2FwdGFpbigiQSIpKTskKCIjbWNTZXRDYXB0YWluQiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+c2V0Q2FwdGFpbigiQiIpKTsKJCgiI21jU3RhcnRCdG4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e3RyeXthd2FpdCBhcGkoYC9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFjdGl2ZU1hdGNoSWQpfS9zdGFydD90ZWxlZ3JhbV9pZD0ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfWAse21ldGhvZDoiUE9TVCJ9KTthd2FpdCBwb2xsQWN0aXZlTWF0Y2goKTt0b2FzdCgi0JzQsNGC0Ycg0L7RgtC80LXRh9C10L0g0LrQsNC6INC90LDRh9Cw0YLRi9C5LiIpO31jYXRjaChlKXt0b2FzdCgi0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0L/Rg9GB0YLQuNGC0Ywg0LzQsNGC0Yc6ICIrZS5tZXNzYWdlKTt9fSk7CiQoIiNtY1N1Ym1pdFJlc3VsdCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57Y29uc3Qgc2NvcmVfYT1OdW1iZXIoJCgiI21jU2NvcmVBIikudmFsdWUpLHNjb3JlX2I9TnVtYmVyKCQoIiNtY1Njb3JlQiIpLnZhbHVlKTtpZighTnVtYmVyLmlzSW50ZWdlcihzY29yZV9hKXx8IU51bWJlci5pc0ludGVnZXIoc2NvcmVfYil8fHNjb3JlX2E8MHx8c2NvcmVfYjwwKXJldHVybiB0b2FzdCgi0KPQutCw0LbQuCDQutC+0YDRgNC10LrRgtC90YvQuSDRgdGH0ZHRgiDQvtCx0LXQuNGFINC60L7QvNCw0L3QtC4iKTt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vcmVzdWx0YCx7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsc2NvcmVfYSxzY29yZV9iLHN0YXRzOntbYXBpVXNlcklkXTp7a2lsbHM6TnVtYmVyKCQoIiNtY0tpbGxzIikudmFsdWV8fDApLGRlYXRoczpOdW1iZXIoJCgiI21jRGVhdGhzIikudmFsdWV8fDApfX0sbm90ZTokKCIjbWNSZXN1bHROb3RlIikudmFsdWUudHJpbSgpfX0pO2F3YWl0IHBvbGxBY3RpdmVNYXRjaCgpO3RvYXN0KCLQoNC10LfRg9C70YzRgtCw0YIg0L7RgtC/0YDQsNCy0LvQtdC9INC80L7QtNC10YDQsNGC0L7RgNGDLiBFTE8g0L/QvtC60LAg0L3QtSDQuNC30LzQtdC90ZHQvS4iKTt9Y2F0Y2goZSl7dG9hc3QoItCg0LXQt9GD0LvRjNGC0LDRgjogIitlLm1lc3NhZ2UpO319KTsKJCgiI21jU3VibWl0QXBwZWFsIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGFzeW5jKCk9Pnt0cnl7YXdhaXQgYXBpKGAvbWF0Y2hlcy8ke2VuY29kZVVSSUNvbXBvbmVudChhY3RpdmVNYXRjaElkKX0vYXBwZWFsc2Ase21ldGhvZDoiUE9TVCIsYm9keTp7dGVsZWdyYW1faWQ6YXBpVXNlcklkLHJlYXNvbjokKCIjbWNBcHBlYWxSZWFzb24iKS52YWx1ZSxkZXRhaWxzOiQoIiNtY0FwcGVhbERldGFpbHMiKS52YWx1ZS50cmltKCl9fSk7dG9hc3QoItCQ0L/QtdC70LvRj9GG0LjRjyDQvtGC0L/RgNCw0LLQu9C10L3QsCDQvNC+0LTQtdGA0LDRgtC+0YDRgy4iKTskKCIjbWNTdWJtaXRBcHBlYWwiKS5kaXNhYmxlZD10cnVlO31jYXRjaChlKXt0b2FzdCgi0JDQv9C10LvQu9GP0YbQuNGPOiAiK2UubWVzc2FnZSk7fX0pOwovLyBLZWVwIHJlYWwgcHJvZmlsZSBzdGF0ZSBhbmQgZGlzY292ZXIgYSBtYXRjaCBpZiBpdCB3YXMgZm9ybWVkIGJ5IGFub3RoZXIgcXVldWUgcGFydGljaXBhbnQuCmNvbnN0IF9vbGRBcHBseVBsYXllcj1hcHBseVBsYXllcjthcHBseVBsYXllcj1mdW5jdGlvbihwKXtfb2xkQXBwbHlQbGF5ZXIocCk7Y29uc3QgcHM9JCgiI3Byb2ZpbGVTdGF0dXMiKTtpZihwcylwcy50ZXh0Q29udGVudD0i4pePINCf0KDQntCk0JjQm9CsINChINCh0JXQoNCS0JXQoNCQIjtzZXRBcGlTdGF0dXMoIkFQSSDQvtC90LvQsNC50L0gwrcg0L/RgNC+0YTQuNC70Ywg0LfQsNCz0YDRg9C20LXQvSIsdHJ1ZSk7fTsKY29uc3QgX29sZEluaXRBcGk9aW5pdEFwaTtpbml0QXBpPWFzeW5jIGZ1bmN0aW9uKCl7YXdhaXQgX29sZEluaXRBcGkoKTtpZihhcGlVc2VySWQpe3RyeXtjb25zdCBhPWF3YWl0IGFwaShgL3BsYXllcnMvJHtlbmNvZGVVUklDb21wb25lbnQoYXBpVXNlcklkKX0vYWN0aXZlLW1hdGNoYCk7aWYoYS5hY3RpdmUmJmEubWF0Y2gpe2FjdGl2ZU1hdGNoSWQ9YS5tYXRjaC5tYXRjaF9pZDthd2FpdCBzaG93TWF0Y2goYWN0aXZlTWF0Y2hJZCk7fX1jYXRjaChlKXtjb25zb2xlLndhcm4oIkFjdGl2ZSBtYXRjaCIsZS5tZXNzYWdlKTt9fX07CgoKLy8gQ29zbWV0aWMgc2hvcCwgZXh0ZW5kZWQgcHJvZmlsZSBzdGF0aXN0aWNzIGFuZCBxdWFsaWZpY2F0aW9uIHRyb3BoaWVzLgpsZXQgY29zbWV0aWNDYXRhbG9nPVtdLGNvc21ldGljRmlsdGVyPSJhbGwiLG15Q29zbWV0aWNJdGVtcz1bXTsKZnVuY3Rpb24gY29zbWV0aWNJY29uKHR5cGUpe3JldHVybiB0eXBlPT09ImZyYW1lIj8i4pajIjp0eXBlPT09InRpdGxlIj8i4pymIjoi4pakIjt9CmZ1bmN0aW9uIGNvc21ldGljVHlwZU5hbWUodHlwZSl7cmV0dXJuICh7ZnJhbWU6ItCg0LDQvNC60LAg0L/RgNC+0YTQuNC70Y8iLHRpdGxlOiLQotC40YLRg9C7Iixwcm9maWxlX2NhcmQ6ItCa0LDRgNGC0L7Rh9C60LAg0L/RgNC+0YTQuNC70Y8ifSlbdHlwZV18fCLQmtC+0YHQvNC10YLQuNC60LAiO30KZnVuY3Rpb24gcmVuZGVyQ29zbWV0aWNTaG9wKCl7Y29uc3QgbGlzdD1jb3NtZXRpY0NhdGFsb2cuZmlsdGVyKHg9PmNvc21ldGljRmlsdGVyPT09ImFsbCJ8fHguaXRlbV90eXBlPT09Y29zbWV0aWNGaWx0ZXIpO2NvbnN0IHJvb3Q9JCgiI2Nvc21ldGljU2hvcEdyaWQiKTtpZighcm9vdClyZXR1cm47cm9vdC5pbm5lckhUTUw9bGlzdC5sZW5ndGg/bGlzdC5tYXAoeD0+YDxhcnRpY2xlIGNsYXNzPSJjb3NtZXRpYy1pdGVtIj48ZGl2IGNsYXNzPSJjb3NtZXRpYy1hcnQiPiR7Y29zbWV0aWNJY29uKHguaXRlbV90eXBlKX08L2Rpdj48ZGl2IGNsYXNzPSJyYXJpdHkiPiR7ZXNjKHgucmFyaXR5fHwiY29tbW9uIil9IMK3ICR7ZXNjKGNvc21ldGljVHlwZU5hbWUoeC5pdGVtX3R5cGUpKX08L2Rpdj48aDQ+JHtlc2MoeC5uYW1lKX08L2g0PjxwPiR7ZXNjKHguZGVzY3JpcHRpb258fCLQmtC+0YHQvNC10YLQuNGH0LXRgdC60LjQuSDQv9GA0LXQtNC80LXRgiBTVEFORFBPSU5UIil9PC9wPjxkaXYgY2xhc3M9ImNvc21ldGljLW1ldGEiPjxiPiR7TnVtYmVyKHgucHJpY2VfcG9pbnRzfHwwKS50b0xvY2FsZVN0cmluZygicnUtUlUiKX0gU1A8L2I+PHNwYW4+JHtlc2MoY29zbWV0aWNUeXBlTmFtZSh4Lml0ZW1fdHlwZSkpfTwvc3Bhbj48L2Rpdj48YnV0dG9uIGNsYXNzPSJidG4tcHJpbWFyeSBidXlDb3NtZXRpYyIgZGF0YS1pdGVtPSIke2VzYyh4Lml0ZW1faWQpfSI+0JrRg9C/0LjRgtGMPC9idXR0b24+PC9hcnRpY2xlPmApLmpvaW4oIiIpOic8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0JIg0Y3RgtC+0Lkg0LrQsNGC0LXQs9C+0YDQuNC4INC/0L7QutCwINC90LXRgiDQv9GA0LXQtNC80LXRgtC+0LIuPC9kaXY+JzsKICQkKCIuYnV5Q29zbWV0aWMiLHJvb3QpLmZvckVhY2goYj0+Yi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsYXN5bmMoKT0+e2lmKCFhcGlVc2VySWQpe3RvYXN0KCLQntGC0LrRgNC+0Lkg0L/RgNC40LvQvtC20LXQvdC40LUg0YfQtdGA0LXQtyBUZWxlZ3JhbSwg0YfRgtC+0LHRiyDQv9C+0LrRg9C/0LDRgtGMINC/0YDQtdC00LzQtdGC0YsuIik7cmV0dXJuO31iLmRpc2FibGVkPXRydWU7dHJ5e2NvbnN0IGQ9YXdhaXQgYXBpKGAvY29zbWV0aWNzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGIuZGF0YXNldC5pdGVtKX0vcHVyY2hhc2VgLHttZXRob2Q6IlBPU1QiLGJvZHk6e3RlbGVncmFtX2lkOmFwaVVzZXJJZH19KTt0b2FzdChkLm1lc3NhZ2V8fCLQn9C+0LrRg9C/0LrQsCDQstGL0L/QvtC70L3QtdC90LAiKTthd2FpdCBsb2FkQ29zbWV0aWNEYXRhKCk7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINC60YPQv9C40YLRjDogIitlLm1lc3NhZ2UpO31maW5hbGx5e2IuZGlzYWJsZWQ9ZmFsc2U7fX0pKTt9CmZ1bmN0aW9uIHJlbmRlck15Q29zbWV0aWNzKCl7Y29uc3Qgcm9vdD0kKCIjbXlDb3NtZXRpY3NHcmlkIik7aWYoIXJvb3QpcmV0dXJuO3Jvb3QuaW5uZXJIVE1MPW15Q29zbWV0aWNJdGVtcy5sZW5ndGg/bXlDb3NtZXRpY0l0ZW1zLm1hcCh4PT5gPGFydGljbGUgY2xhc3M9ImNvc21ldGljLWl0ZW0iPjxkaXYgY2xhc3M9ImNvc21ldGljLWFydCI+JHtjb3NtZXRpY0ljb24oeC5pdGVtX3R5cGUpfTwvZGl2PjxkaXYgY2xhc3M9InJhcml0eSI+JHtlc2MoeC5yYXJpdHl8fCJjb21tb24iKX08L2Rpdj48aDQ+JHtlc2MoeC5uYW1lKX08L2g0PjxwPiR7ZXNjKHguZGVzY3JpcHRpb258fGNvc21ldGljVHlwZU5hbWUoeC5pdGVtX3R5cGUpKX08L3A+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSBlcXVpcENvc21ldGljIiBkYXRhLWl0ZW09IiR7ZXNjKHguaXRlbV9pZCl9Ij7QndCw0LTQtdGC0Yw8L2J1dHRvbj48L2FydGljbGU+YCkuam9pbigiIik6JzxkaXYgY2xhc3M9ImNvc21ldGljLWVtcHR5Ij7QmtC+0LvQu9C10LrRhtC40Y8g0L/QvtC60LAg0L/Rg9GB0YLQsC48L2Rpdj4nOyQkKCIuZXF1aXBDb3NtZXRpYyIscm9vdCkuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixhc3luYygpPT57aWYoIWFwaVVzZXJJZClyZXR1cm4gdG9hc3QoItCh0L3QsNGH0LDQu9CwINC/0L7QtNC60LvRjtGH0Lgg0L/RgNC+0YTQuNC70Ywg0YfQtdGA0LXQtyBUZWxlZ3JhbS4iKTt0cnl7YXdhaXQgYXBpKCIvY29zbWV0aWNzL2VxdWlwIix7bWV0aG9kOiJQT1NUIixib2R5Ont0ZWxlZ3JhbV9pZDphcGlVc2VySWQsaXRlbV9pZDpiLmRhdGFzZXQuaXRlbX19KTt0b2FzdCgi0J/RgNC10LTQvNC10YIg0Y3QutC40L/QuNGA0L7QstCw0L0iKTthd2FpdCBsb2FkQ29zbWV0aWNEYXRhKCk7fWNhdGNoKGUpe3RvYXN0KCLQndC1INGD0LTQsNC70L7RgdGMINGN0LrQuNC/0LjRgNC+0LLQsNGC0Yw6ICIrZS5tZXNzYWdlKTt9fSkpO30KYXN5bmMgZnVuY3Rpb24gbG9hZENvc21ldGljRGF0YSgpe2lmKCFhcGlVc2VySWQpeyQoIiNjb3NtZXRpY1Nob3BHcmlkIikuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0JrQsNGC0LDQu9C+0LMg0LzQvtC20L3QviDQv9GA0L7RgdC80L7RgtGA0LXRgtGMLCDQvdC+INC/0L7QutGD0L/QutCwINGC0YDQtdCx0YPQtdGCINC/0L7QtNC60LvRjtGH0LXQvdC40Y8g0L/RgNC+0YTQuNC70Y8uPC9kaXY+JztyZXR1cm47fXRyeXtjb25zdCBbY2F0YWxvZyxvd25lZF09YXdhaXQgUHJvbWlzZS5hbGwoW2FwaSgiL2Nvc21ldGljcy9zaG9wIiksYXBpKGAvY29zbWV0aWNzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KGFwaVVzZXJJZCl9YCldKTtjb3NtZXRpY0NhdGFsb2c9Y2F0YWxvZy5pdGVtc3x8Y2F0YWxvZy5jb3NtZXRpY3N8fFtdO215Q29zbWV0aWNJdGVtcz1vd25lZC5pdGVtc3x8b3duZWQuY29zbWV0aWNzfHxbXTskKCIjY29zbWV0aWNQb2ludHMiKS50ZXh0Q29udGVudD1OdW1iZXIob3duZWQuc2V0dGluZ3M/LnBvaW50cz8/b3duZWQucG9pbnRzPz8wKS50b0xvY2FsZVN0cmluZygicnUtUlUiKTtyZW5kZXJDb3NtZXRpY1Nob3AoKTtyZW5kZXJNeUNvc21ldGljcygpO31jYXRjaChlKXskKCIjY29zbWV0aWNTaG9wR3JpZCIpLmlubmVySFRNTD1gPGRpdiBjbGFzcz0iY29zbWV0aWMtZW1wdHkiPtCd0LUg0YPQtNCw0LvQvtGB0Ywg0LfQsNCz0YDRg9C30LjRgtGMINC60LDRgtCw0LvQvtCzOiAke2VzYyhlLm1lc3NhZ2UpfTwvZGl2PmA7fX0KYXN5bmMgZnVuY3Rpb24gbG9hZEV4dHJhUHJvZmlsZURhdGEoKXtpZighYXBpVXNlcklkKXJldHVybjt0cnl7Y29uc3QgW3N0YXRzLHRyb3BoaWVzXT1hd2FpdCBQcm9taXNlLmFsbChbYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfS9zdGF0aXN0aWNzYCksYXBpKGAvcGxheWVycy8ke2VuY29kZVVSSUNvbXBvbmVudChhcGlVc2VySWQpfS90cm9waGllc2ApXSk7Y29uc3QgZD1zdGF0cy5zdGF0aXN0aWNzfHxzdGF0cztjb25zdCB2YWw9KGlkLHYpPT57Y29uc3QgZWw9JChpZCk7aWYoZWwpZWwudGV4dENvbnRlbnQ9dj8/MDt9O3ZhbCgiI2V4dHJhV2lucmF0ZSIsKGQud2luX3JhdGU/P2Qud2lucmF0ZSkhPW51bGw/KE51bWJlcihkLndpbl9yYXRlPz9kLndpbnJhdGUpLnRvRml4ZWQoMSkrIiUiKTooc3RhdGUud2lucytzdGF0ZS5sb3NzZXMrc3RhdGUuZHJhd3M/TWF0aC5yb3VuZChzdGF0ZS53aW5zLyhzdGF0ZS53aW5zK3N0YXRlLmxvc3NlcytzdGF0ZS5kcmF3cykqMTAwKSsiJSI6IjAlIikpO3ZhbCgiI2V4dHJhTG9zc2VzIixkLmxvc3Nlcz8/c3RhdGUubG9zc2VzKTt2YWwoIiNleHRyYURyYXdzIixkLmRyYXdzPz9zdGF0ZS5kcmF3cyk7dmFsKCIjZXh0cmFLaWxscyIsZC5raWxscz8/c3RhdGUua2lsbHMpO3ZhbCgiI2V4dHJhRGVhdGhzIixkLmRlYXRocz8/c3RhdGUuZGVhdGhzKTt2YWwoIiNleHRyYUtEIixkLmtkPz8oc3RhdGUuZGVhdGhzPyhzdGF0ZS5raWxscy9zdGF0ZS5kZWF0aHMpLnRvRml4ZWQoMik6IuKAlCIpKTt2YWwoIiNleHRyYU1hdGNoZXMiLGQubWF0Y2hlcz8/ZC50b3RhbF9tYXRjaGVzPz8oc3RhdGUud2lucytzdGF0ZS5sb3NzZXMrc3RhdGUuZHJhd3MpKTt2YWwoIiNleHRyYUVsbyIsZC5lbG8/P3N0YXRlLmVsbyk7JCgiI2V4dHJhU3RhdHNOb3RlIikudGV4dENvbnRlbnQ9ItCh0YLQsNGC0LjRgdGC0LjQutCwINC/0L7Qu9GD0YfQtdC90LAg0LjQtyBBUEkuIjtjb25zdCBpdGVtcz10cm9waGllcy50cm9waGllc3x8W107JCgiI3Ryb3BoeUNvdW50IikudGV4dENvbnRlbnQ9aXRlbXMubGVuZ3RoKyIg0J3QkNCT0KDQkNCUIjskKCIjcHJvZmlsZVRyb3BoaWVzIikuaW5uZXJIVE1MPWl0ZW1zLmxlbmd0aD9pdGVtcy5tYXAodD0+YDxkaXYgY2xhc3M9InRyb3BoeSI+PGRpdiBjbGFzcz0iY3VwIj4ke051bWJlcih0LnBsYWNlKT09PTE/IvCfj4YiOk51bWJlcih0LnBsYWNlKT09PTI/IvCfpYgiOk51bWJlcih0LnBsYWNlKT09PTM/IvCfpYkiOiLwn4+FIn08L2Rpdj48Yj4ke2VzYyh0LnRyb3BoeV9uYW1lfHwoItCi0L7Qvy0iK3QucGxhY2UpKX08L2I+PHNtYWxsPiR7ZXNjKHQucXVhbGlmaWNhdGlvbl9rZXl8fCLQmtCy0LDQu9C40YTQuNC60LDRhtC40Y8iKX08L3NtYWxsPjxzbWFsbD4ke3QucGxhY2V9INC80LXRgdGC0L48L3NtYWxsPjwvZGl2PmApLmpvaW4oIiIpOic8ZGl2IGNsYXNzPSJjb3NtZXRpYy1lbXB0eSI+0J/QvtC60LAg0L3QtdGCINC60YPQsdC60L7Qsi4g0J3QsNCz0YDQsNC00Ysg0L/QvtGP0LLRj9GC0YHRjyDQv9C+0YHQu9C1INC30LDQstC10YDRiNC10L3QuNGPINC60LLQsNC70LjRhNC40LrQsNGG0LjQuC48L2Rpdj4nO31jYXRjaChlKXskKCIjZXh0cmFTdGF0c05vdGUiKS50ZXh0Q29udGVudD0i0J3QtSDRg9C00LDQu9C+0YHRjCDQt9Cw0LPRgNGD0LfQuNGC0Ywg0YHRgtCw0YLQuNGB0YLQuNC60YM6ICIrZS5tZXNzYWdlO319CiQkKCJbZGF0YS1jb3NtZXRpYy1maWx0ZXJdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+e2Nvc21ldGljRmlsdGVyPWIuZGF0YXNldC5jb3NtZXRpY0ZpbHRlcjskJCgiW2RhdGEtY29zbWV0aWMtZmlsdGVyXSIpLmZvckVhY2goeD0+eC5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLHg9PT1iKSk7cmVuZGVyQ29zbWV0aWNTaG9wKCk7fSkpOwokKCIjcmVmcmVzaENvc21ldGljcyIpPy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsbG9hZENvc21ldGljRGF0YSk7JCgiI3JlZnJlc2hFeHRyYVN0YXRzIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIixsb2FkRXh0cmFQcm9maWxlRGF0YSk7CmNvbnN0IF9vbGROYXZGb3JDb3NtZXRpY3M9bmF2O25hdj1mdW5jdGlvbihwYWdlKXtfb2xkTmF2Rm9yQ29zbWV0aWNzKHBhZ2UpO2lmKHBhZ2U9PT0iY29zbWV0aWNzIilsb2FkQ29zbWV0aWNEYXRhKCk7aWYocGFnZT09PSJwcm9maWxlIilsb2FkRXh0cmFQcm9maWxlRGF0YSgpO307CmNvbnN0IF9vbGRBcHBseVBsYXllckZvckV4dHJhcz1hcHBseVBsYXllcjthcHBseVBsYXllcj1mdW5jdGlvbihwKXtfb2xkQXBwbHlQbGF5ZXJGb3JFeHRyYXMocCk7aWYoYXBpVXNlcklkKXtsb2FkRXh0cmFQcm9maWxlRGF0YSgpO2lmKGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoIiNwYWdlLWNvc21ldGljcy5hY3RpdmUiKSlsb2FkQ29zbWV0aWNEYXRhKCk7fX07CgoKLy8gTW9kZXJhdG9yIHBhbmVsOiB2aXNpYmlsaXR5IGlzIHRpZWQgdG8gVGVsZWdyYW0gdXNlcm5hbWU7IEFQSSBhY3Rpb25zIHN0aWxsIHJlcXVpcmUgc2VydmVyIHNlY3JldC4KY29uc3QgTU9ERVJBVE9SX1VTRVJOQU1FPSJ5dWt6ZW5fYnJvIjsKZnVuY3Rpb24gbW9kZXJhdG9yQWxsb3dlZCgpe2NvbnN0IHU9Z2V0VGVsZWdyYW1Vc2VyKCk7cmV0dXJuIFN0cmluZyh1Py51c2VybmFtZXx8IiIpLnJlcGxhY2UoL15ALywiIikudG9Mb3dlckNhc2UoKT09PU1PREVSQVRPUl9VU0VSTkFNRTt9CmZ1bmN0aW9uIG1vZGVyYXRvckhlYWRlcnMoKXtjb25zdCB0b2tlbj0kKCIjbW9kZXJhdG9yVG9rZW4iKT8udmFsdWUudHJpbSgpO2lmKCF0b2tlbil0aHJvdyBuZXcgRXJyb3IoItCS0LLQtdC00LggUkVTVUxUU19BRE1JTl9UT0tFTiDQuNC3INC90LDRgdGC0YDQvtC10Log0YHQtdGA0LLQtdGA0LAuIik7cmV0dXJuIHsiWC1BZG1pbi1Ub2tlbiI6dG9rZW59O30KYXN5bmMgZnVuY3Rpb24gbW9kZXJhdG9yQXBpKHBhdGgse21ldGhvZD0iR0VUIixib2R5PW51bGx9PXt9KXtjb25zdCBoZWFkZXJzPXsuLi5tb2RlcmF0b3JIZWFkZXJzKCksQWNjZXB0OiJhcHBsaWNhdGlvbi9qc29uIn07Y29uc3Qgb3B0cz17bWV0aG9kLGhlYWRlcnN9O2lmKGJvZHkhPT1udWxsKXtoZWFkZXJzWyJDb250ZW50LVR5cGUiXT0iYXBwbGljYXRpb24vanNvbiI7b3B0cy5ib2R5PUpTT04uc3RyaW5naWZ5KGJvZHkpO31sZXQgcjt0cnl7cj1hd2FpdCBmZXRjaChBUElfQkFTRStwYXRoLG9wdHMpfWNhdGNoKGUpe3Rocm93IG5ldyBFcnJvcigi0KHQtdGA0LLQtdGAINC90LXQtNC+0YHRgtGD0L/QtdC9INC40LvQuCDQvtGI0LjQsdC60LAg0YHQtdGC0LguIik7fWxldCBkPXt9O3RyeXtkPWF3YWl0IHIuanNvbigpfWNhdGNoe31pZighci5vayl0aHJvdyBuZXcgRXJyb3IoZC5kZXRhaWx8fCgiSFRUUCAiK3Iuc3RhdHVzKSk7cmV0dXJuIGQ7fQpmdW5jdGlvbiBzaG93TW9kZXJhdG9yUGFuZWwoKXtjb25zdCBjYXJkPSQoIiNtb2RlcmF0b3JDYXJkIik7aWYoY2FyZCljYXJkLnN0eWxlLmRpc3BsYXk9bW9kZXJhdG9yQWxsb3dlZCgpPyJibG9jayI6Im5vbmUiO30KYXN5bmMgZnVuY3Rpb24gbG9hZE1vZGVyYXRpb25RdWV1ZSgpe2NvbnN0IG91dD0kKCIjbW9kZXJhdGlvblF1ZXVlIik7b3V0LmlubmVySFRNTD0nPHAgY2xhc3M9Im11dGVkIHNtYWxsIj7Ql9Cw0LPRgNGD0LfQutCwINC+0YfQtdGA0LXQtNC44oCmPC9wPic7dHJ5e2NvbnN0IGQ9YXdhaXQgbW9kZXJhdG9yQXBpKCIvYWRtaW4vbW9kZXJhdGlvbi9xdWV1ZSIpO2NvbnN0IG1hdGNoZXM9ZC5tYXRjaGVzfHxbXTtpZighbWF0Y2hlcy5sZW5ndGgpe291dC5pbm5lckhUTUw9JzxwIGNsYXNzPSJtdXRlZCBzbWFsbCI+0J3QtdGCINC80LDRgtGH0LXQuSwg0L7QttC40LTQsNGO0YnQuNGFINC/0YDQvtCy0LXRgNC60LguPC9wPic7cmV0dXJuO31vdXQuaW5uZXJIVE1MPW1hdGNoZXMubWFwKG09PmA8ZGl2IGNsYXNzPSJtYy1wYW5lbCIgZGF0YS1tb2QtbWF0Y2g9IiR7ZXNjKG0ubWF0Y2hfaWQpfSI+PGRpdiBjbGFzcz0iY2FyZGhlYWQiPjxkaXY+PGI+JHtlc2MobS5tb2RlfHwi0JzQsNGC0YciKX0gwrcgJHtlc2MobS5tYXRjaF9pZCl9PC9iPjxkaXYgY2xhc3M9Im1jLW11dGVkIj4ke2VzYyhtLnN0YXR1cyl9IMK3ICR7ZXNjKG0ubWFwX25hbWV8fCLQmtCw0YDRgtCwINC90LUg0YPQutCw0LfQsNC90LAiKX08L2Rpdj48L2Rpdj48c3BhbiBjbGFzcz0iYmFkZ2Utb3JhbmdlIj4keyhtLnBsYXllcnN8fFtdKS5sZW5ndGh9INC40LPRgNC+0LrQvtCyPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im1jLW11dGVkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij5JRCDQuNCz0YDQvtC60L7QsjogJHsobS5wbGF5ZXJzfHxbXSkubWFwKGVzYykuam9pbigiLCAiKX08L2Rpdj48ZGl2IGNsYXNzPSJmb3JtZ3JpZCI+PGRpdiBjbGFzcz0iZmllbGQiPjxsYWJlbD7QodCn0IHQoiDQmtCe0JzQkNCd0JTQqyBBPC9sYWJlbD48aW5wdXQgdHlwZT0ibnVtYmVyIiBtaW49IjAiIG1heD0iMTAwIiBkYXRhLXNjb3JlLWEgcGxhY2Vob2xkZXI9IjAiPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0KHQp9CB0KIg0JrQntCc0JDQndCU0KsgQjwvbGFiZWw+PGlucHV0IHR5cGU9Im51bWJlciIgbWluPSIwIiBtYXg9IjEwMCIgZGF0YS1zY29yZS1iIHBsYWNlaG9sZGVyPSIwIj48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJmaWVsZCI+PGxhYmVsPtCh0KLQkNCi0JjQodCi0JjQmtCQINCf0J4g0JjQk9Cg0J7QmtCQ0JwgKEpTT04sINC90LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+KTwvbGFiZWw+PHRleHRhcmVhIGRhdGEtcGxheWVyLXN0YXRzIHJvd3M9IjMiIHBsYWNlaG9sZGVyPSd7IlRFTEVHUkFNX0lEIjp7ImtpbGxzIjoxMCwiZGVhdGhzIjo0fX0nPjwvdGV4dGFyZWE+PHNtYWxsIGNsYXNzPSJtYy1tdXRlZCI+0JTQvtCx0LDQstGMIGtpbGxzL2RlYXRocyDQtNC70Y8g0LrQsNC20LTQvtCz0L4g0LjQs9GA0L7QutCwINC/0L4gVGVsZWdyYW0gSUQuINCV0YHQu9C4INC+0YHRgtCw0LLQuNGC0Ywg0L/Rg9GB0YLRi9C8LCDRgdGC0LDRgtC40YHRgtC40LrQsCDQvdC1INC40LfQvNC10L3QuNGC0YHRjy48L3NtYWxsPjwvZGl2PjxkaXYgY2xhc3M9ImZpZWxkIj48bGFiZWw+0JrQntCc0JzQldCd0KLQkNCg0JjQmTwvbGFiZWw+PGlucHV0IGRhdGEtcmV2aWV3LW5vdGUgbWF4bGVuZ3RoPSIxMDAwIiBwbGFjZWhvbGRlcj0i0J/RgNC40YfQuNC90LAg0YDQtdGI0LXQvdC40Y8gKNC90LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+KSI+PC9kaXY+PGRpdiBjbGFzcz0ibWMtYWN0aW9ucyI+PGJ1dHRvbiBjbGFzcz0iYnRuLXByaW1hcnkiIGRhdGEtbW9kLWFwcHJvdmU+4pyTINCe0LTQvtCx0YDQuNGC0YwgKyDQvdCw0YfQuNGB0LvQuNGC0YwgRUxPPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iYnRuLXNlY29uZGFyeSIgZGF0YS1tb2QtcmVqZWN0PuKclSDQntGC0LrQsNC30LDRgtGMINCx0LXQtyDQuNC30LzQtdC90LXQvdC40Lk8L2J1dHRvbj48L2Rpdj48L2Rpdj5gKS5qb2luKCIiKTtvdXQucXVlcnlTZWxlY3RvckFsbCgiW2RhdGEtbW9kLWFwcHJvdmVdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+cmV2aWV3TW9kZXJhdGlvbihiLmNsb3Nlc3QoIltkYXRhLW1vZC1tYXRjaF0iKSwiYXBwcm92ZSIpKSk7b3V0LnF1ZXJ5U2VsZWN0b3JBbGwoIltkYXRhLW1vZC1yZWplY3RdIikuZm9yRWFjaChiPT5iLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+cmV2aWV3TW9kZXJhdGlvbihiLmNsb3Nlc3QoIltkYXRhLW1vZC1tYXRjaF0iKSwicmVqZWN0IikpKTt9Y2F0Y2goZSl7b3V0LmlubmVySFRNTD1gPHAgc3R5bGU9ImNvbG9yOiNmZjhlOGUiPiR7ZXNjKGUubWVzc2FnZSl9PC9wPmA7fX0KYXN5bmMgZnVuY3Rpb24gcmV2aWV3TW9kZXJhdGlvbihjYXJkLGRlY2lzaW9uKXtjb25zdCBtYXRjaElkPWNhcmQuZGF0YXNldC5tb2RNYXRjaDtsZXQgc3RhdHM9e307dHJ5e2NvbnN0IHJhdz1jYXJkLnF1ZXJ5U2VsZWN0b3IoIltkYXRhLXBsYXllci1zdGF0c10iKS52YWx1ZS50cmltKCk7aWYocmF3KXN0YXRzPUpTT04ucGFyc2UocmF3KTtpZighc3RhdHN8fHR5cGVvZiBzdGF0cyE9PSJvYmplY3QifHxBcnJheS5pc0FycmF5KHN0YXRzKSl0aHJvdyBuZXcgRXJyb3IoItCh0YLQsNGC0LjRgdGC0LjQutCwINC00L7Qu9C20L3QsCDQsdGL0YLRjCBKU09OLdC+0LHRitC10LrRgtC+0LwuIik7fWNhdGNoKGUpe3RvYXN0KCLQntGI0LjQsdC60LAg0YHRgtCw0YLQuNGB0YLQuNC60Lg6ICIrZS5tZXNzYWdlKTtyZXR1cm47fWNvbnN0IGJvZHk9e2RlY2lzaW9uLG5vdGU6Y2FyZC5xdWVyeVNlbGVjdG9yKCJbZGF0YS1yZXZpZXctbm90ZV0iKS52YWx1ZS50cmltKCl9O2lmKGRlY2lzaW9uPT09ImFwcHJvdmUiKXtjb25zdCBhPWNhcmQucXVlcnlTZWxlY3RvcigiW2RhdGEtc2NvcmUtYV0iKS52YWx1ZSxiPWNhcmQucXVlcnlTZWxlY3RvcigiW2RhdGEtc2NvcmUtYl0iKS52YWx1ZTtpZihhPT09IiJ8fGI9PT0iIilyZXR1cm4gdG9hc3QoItCU0LvRjyDQvtC00L7QsdGA0LXQvdC40Y8g0YPQutCw0LbQuCDQvtCx0LAg0YHRh9GR0YLQsC4iKTtib2R5LnNjb3JlX2E9TnVtYmVyKGEpO2JvZHkuc2NvcmVfYj1OdW1iZXIoYik7Ym9keS5zdGF0cz1zdGF0czt9aWYoIWNvbmZpcm0oZGVjaXNpb249PT0iYXBwcm92ZSI/ItCe0LTQvtCx0YDQuNGC0Ywg0LzQsNGC0Yc/IEVMTyDQuCDRg9C60LDQt9Cw0L3QvdCw0Y8g0YHRgtCw0YLQuNGB0YLQuNC60LAg0LHRg9C00YPRgiDQt9Cw0L/QuNGB0LDQvdGLINCyINC/0YDQvtGE0LjQu9C4INC40LPRgNC+0LrQvtCyLiI6ItCe0YLQutCw0LfQsNGC0Ywg0LIg0LzQsNGC0YfQtT8gRUxPINC4INGB0YLQsNGC0LjRgdGC0LjQutCwINC+0YHRgtCw0L3Rg9GC0YHRjyDQsdC10Lcg0LjQt9C80LXQvdC10L3QuNC5LiIpKXJldHVybjt0cnl7Y29uc3QgZD1hd2FpdCBtb2RlcmF0b3JBcGkoYC9hZG1pbi9tYXRjaGVzLyR7ZW5jb2RlVVJJQ29tcG9uZW50KG1hdGNoSWQpfS9yZXZpZXdgLHttZXRob2Q6IlBPU1QiLGJvZHl9KTt0b2FzdChkZWNpc2lvbj09PSJhcHByb3ZlIj8i0JzQsNGC0Ycg0L7QtNC+0LHRgNC10L0sIEVMTyDQuCDRgdGC0LDRgtC40YHRgtC40LrQsCDQvtCx0L3QvtCy0LvQtdC90YsuIjoi0JzQsNGC0Ycg0L7RgtC60LvQvtC90ZHQvSDQsdC10Lcg0L3QsNGH0LjRgdC70LXQvdC40Y8gRUxPLiIpO2F3YWl0IGxvYWRNb2RlcmF0aW9uUXVldWUoKTt9Y2F0Y2goZSl7dG9hc3QoItCe0YjQuNCx0LrQsCDQvNC+0LTQtdGA0LDRhtC40Lg6ICIrZS5tZXNzYWdlKTt9fQokKCIjbG9hZE1vZGVyYXRpb25CdG4iKT8uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLGxvYWRNb2RlcmF0aW9uUXVldWUpOyQoIiNjbGVhck1vZGVyYXRvclRva2VuIik/LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwoKT0+eyQoIiNtb2RlcmF0b3JUb2tlbiIpLnZhbHVlPSIiO3RvYXN0KCLQn9C+0LvQtSDQutC70Y7Rh9CwINC+0YfQuNGJ0LXQvdC+LiIpO30pO3Nob3dNb2RlcmF0b3JQYW5lbCgpOwpyZW5kZXIoKTsKaW5pdEFwaSgpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+"""

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

class PassPremiumGrant(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    season_id: Optional[str] = None
    source: str = Field(default="verified_payment", min_length=3, max_length=40)

class PassCurrencyClaim(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=64)
    level: int = Field(ge=1, le=30)


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
        premium = con.execute("SELECT 1 FROM player_pass_premium WHERE season_id=? AND telegram_id=?",
                              (season["season_id"], telegram_id)).fetchone() is not None
        wallet = con.execute("SELECT points FROM player_cosmetic_settings WHERE telegram_id=?",
                             (telegram_id,)).fetchone()
        claimed_rows = con.execute("SELECT level FROM pass_currency_claims WHERE season_id=? AND telegram_id=?",
                                   (season["season_id"], telegram_id)).fetchall()
        claimed_levels = [int(r["level"]) for r in claimed_rows]
        rewards = con.execute("""SELECT r.*, CASE WHEN pr.reward_id IS NULL THEN 0 ELSE 1 END AS claimed
            FROM rewards r LEFT JOIN player_rewards pr ON pr.reward_id=r.reward_id AND pr.telegram_id=?
            WHERE r.season_id=? ORDER BY r.required_level,r.reward_id""",
            (telegram_id, season["season_id"])).fetchall()
    return {"active": True, "season": dict(season), "xp": xp, "level": level,
            "premium": premium, "points": int(wallet["points"]) if wallet else 0,
            "claimed_currency_levels": claimed_levels,
            "xp_to_next_level": max(0, level * 100 - xp), "rewards": [dict(r) for r in rewards]}


@app.post("/admin/pass/grant-premium")
def grant_pass_premium(payload: PassPremiumGrant,
                       x_admin_token: Optional[str] = Header(default=None)):
    """Grant premium entitlement only after an external payment has been verified."""
    require_admin(x_admin_token)
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        season = None
        if payload.season_id:
            season = con.execute("SELECT season_id FROM pass_seasons WHERE season_id=?",
                                 (payload.season_id,)).fetchone()
        else:
            season = con.execute("SELECT season_id FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not season:
            raise HTTPException(404, "ÐÐºÑÐ¸Ð²Ð½ÑÐ¹ ÑÐµÐ·Ð¾Ð½ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        con.execute("""INSERT OR IGNORE INTO player_pass_premium(season_id,telegram_id,granted_at,source)
                       VALUES(?,?,?,?)""",
                    (season["season_id"], payload.telegram_id, now(), payload.source))
    return {"status": "granted", "season_id": season["season_id"], "telegram_id": payload.telegram_id}


@app.post("/pass/claim-currency")
def claim_pass_currency(payload: PassCurrencyClaim):
    amounts = {2: 50, 7: 100, 15: 150, 25: 250, 30: 300}
    amount = amounts.get(payload.level)
    if amount is None:
        raise HTTPException(400, "ÐÐ° ÑÑÐ¾Ð¼ ÑÑÐ¾Ð²Ð½Ðµ Ð½ÐµÑ Ð²Ð°Ð»ÑÑÐ½Ð¾Ð¹ Ð½Ð°Ð³ÑÐ°Ð´Ñ")
    with db() as con:
        require_registered_player(con, payload.telegram_id)
        season = con.execute("SELECT season_id FROM pass_seasons WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not season:
            raise HTTPException(409, "ÐÐµÑ Ð°ÐºÑÐ¸Ð²Ð½Ð¾Ð³Ð¾ ÑÐµÐ·Ð¾Ð½Ð°")
        premium = con.execute("SELECT 1 FROM player_pass_premium WHERE season_id=? AND telegram_id=?",
                               (season["season_id"], payload.telegram_id)).fetchone()
        if not premium:
            raise HTTPException(403, "ÐÐ»Ñ ÑÑÐ¾Ð¹ Ð½Ð°Ð³ÑÐ°Ð´Ñ Ð½ÑÐ¶ÐµÐ½ Ð°ÐºÑÐ¸Ð²Ð¸ÑÐ¾Ð²Ð°Ð½Ð½ÑÐ¹ Premium Pass")
        pass_row = con.execute("SELECT level FROM player_pass WHERE season_id=? AND telegram_id=?",
                               (season["season_id"], payload.telegram_id)).fetchone()
        current_level = int(pass_row["level"]) if pass_row else 1
        if current_level < payload.level:
            raise HTTPException(409, f"Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð´Ð¾ÑÑÐ¸Ð³Ð½Ð¸ ÑÑÐ¾Ð²Ð½Ñ {payload.level}")
        con.execute("INSERT OR IGNORE INTO player_cosmetic_settings(telegram_id,updated_at) VALUES(?,?)",
                    (payload.telegram_id, now()))
        try:
            con.execute("INSERT INTO pass_currency_claims(season_id,telegram_id,level,amount,claimed_at) VALUES(?,?,?,?,?)",
                        (season["season_id"], payload.telegram_id, payload.level, amount, now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ð­ÑÐ° Ð½Ð°Ð³ÑÐ°Ð´Ð° ÑÐ¶Ðµ Ð¿Ð¾Ð»ÑÑÐµÐ½Ð°")
        con.execute("UPDATE player_cosmetic_settings SET points=points+?,updated_at=? WHERE telegram_id=?",
                    (amount, now(), payload.telegram_id))
        wallet = con.execute("SELECT points FROM player_cosmetic_settings WHERE telegram_id=?",
                             (payload.telegram_id,)).fetchone()
        create_notification(con, payload.telegram_id, "pass_currency_reward", f"+{amount} SP",
                            f"ÐÐ¾Ð»ÑÑÐµÐ½Ð° Ð½Ð°Ð³ÑÐ°Ð´Ð° STANDPOINT Pass Ð·Ð° ÑÑÐ¾Ð²ÐµÐ½Ñ {payload.level}.",
                            {"season_id": season["season_id"], "level": payload.level, "amount": amount})
    return {"status": "claimed", "amount": amount, "points": int(wallet["points"]), "level": payload.level}


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


import os
import random
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime,
    ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./standpoint.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

app = FastAPI(title="STANDPOINT API", version="0.1.0")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, nullable=False, index=True)
    nickname = Column(String, nullable=False)
    game_id = Column(String, nullable=False)
    rating = Column(Integer, default=1000, nullable=False)


class QueueEntry(Base):
    __tablename__ = "queue_entries"
    __table_args__ = (
        UniqueConstraint("player_id", name="uq_queue_player"),
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    mode = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True)
    mode = Column(String, nullable=False)
    status = Column(String, default="waiting", nullable=False)
    captain_player_id = Column(Integer, ForeignKey("players.id"))
    map_name = Column(String, default="To be selected")
    winner_team = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MatchPlayer(Base):
    __tablename__ = "match_players"
    __table_args__ = (
        UniqueConstraint("match_id", "player_id", name="uq_match_player"),
    )

    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    team = Column(Integer, nullable=False)
    confirmed_result = Column(Integer, nullable=True)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class RegisterRequest(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=100)
    nickname: str = Field(min_length=1, max_length=50)
    game_id: str = Field(min_length=1, max_length=100)


class QueueRequest(BaseModel):
    telegram_id: str
    mode: str


class ResultRequest(BaseModel):
    telegram_id: str
    winner_team: int = Field(ge=1, le=2)


@app.get("/")
def root():
    return {
        "name": "STANDPOINT API",
        "version": "0.1.0",
        "docs": "/docs",
        "status": "online"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/players/register")
def register_player(data: RegisterRequest, db: Session = Depends(get_db)):
    player = db.query(Player).filter_by(
        telegram_id=data.telegram_id
    ).first()

    if player:
        player.nickname = data.nickname
        player.game_id = data.game_id
    else:
        player = Player(
            telegram_id=data.telegram_id,
            nickname=data.nickname,
            game_id=data.game_id,
            rating=1000,
        )
        db.add(player)

    db.commit()
    db.refresh(player)

    return {
        "message": "Player registered",
        "player": {
            "id": player.id,
            "telegram_id": player.telegram_id,
            "nickname": player.nickname,
            "game_id": player.game_id,
            "rating": player.rating,
        }
    }


@app.get("/players/{telegram_id}")
def get_player(telegram_id: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter_by(telegram_id=telegram_id).first()
    if not player:
        raise HTTPException(404, "Player not found")

    return {
        "id": player.id,
        "telegram_id": player.telegram_id,
        "nickname": player.nickname,
        "game_id": player.game_id,
        "rating": player.rating,
    }


@app.post("/queue/join")
def join_queue(data: QueueRequest, db: Session = Depends(get_db)):
    if data.mode not in ("2v2", "5v5"):
        raise HTTPException(400, "Mode must be 2v2 or 5v5")

    player = db.query(Player).filter_by(
        telegram_id=data.telegram_id
    ).first()
    if not player:
        raise HTTPException(404, "Register your player first")

    active_match = (
        db.query(MatchPlayer)
        .join(Match, Match.id == MatchPlayer.match_id)
        .filter(
            MatchPlayer.player_id == player.id,
            Match.status.in_(["waiting", "ready"])
        )
        .first()
    )
    if active_match:
        raise HTTPException(409, "Player already has an active match")

    existing = db.query(QueueEntry).filter_by(player_id=player.id).first()
    if existing:
        if existing.mode == data.mode:
            return {"status": "already_in_queue", "mode": existing.mode}
        db.delete(existing)
        db.flush()

    entry = QueueEntry(player_id=player.id, mode=data.mode)
    db.add(entry)
    db.flush()

    team_size = 2 if data.mode == "2v2" else 5
    needed = team_size * 2

    entries = (
        db.query(QueueEntry)
        .filter_by(mode=data.mode)
        .order_by(QueueEntry.created_at, QueueEntry.id)
        .limit(needed)
        .all()
    )

    if len(entries) < needed:
        db.commit()
        return {
            "status": "queued",
            "mode": data.mode,
            "players_found": len(entries),
            "players_needed": needed,
        }

    player_ids = [item.player_id for item in entries]
    random.shuffle(player_ids)

    match = Match(
        mode=data.mode,
        status="ready",
        captain_player_id=player_ids[0],
    )
    db.add(match)
    db.flush()

    for index, player_id in enumerate(player_ids):
        db.add(MatchPlayer(
            match_id=match.id,
            player_id=player_id,
            team=1 if index < team_size else 2,
        ))

    for item in entries:
        db.delete(item)

    db.commit()

    return {
        "status": "match_found",
        "match_id": match.id,
        "mode": match.mode,
        "players": needed,
        "captain_player_id": match.captain_player_id,
        "message": "Match created. Captain must create the game lobby manually."
    }


@app.post("/queue/leave")
def leave_queue(telegram_id: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter_by(telegram_id=telegram_id).first()
    if not player:
        raise HTTPException(404, "Player not found")

    entry = db.query(QueueEntry).filter_by(player_id=player.id).first()
    if not entry:
        return {"status": "not_in_queue"}

    db.delete(entry)
    db.commit()
    return {"status": "left_queue"}


@app.get("/queue/{mode}")
def queue_status(mode: str, db: Session = Depends(get_db)):
    if mode not in ("2v2", "5v5"):
        raise HTTPException(400, "Mode must be 2v2 or 5v5")

    count = db.query(QueueEntry).filter_by(mode=mode).count()
    needed = 4 if mode == "2v2" else 10

    return {
        "mode": mode,
        "players_found": count,
        "players_needed": needed,
    }


@app.get("/matches/{match_id}")
def get_match(match_id: int, db: Session = Depends(get_db)):
    match = db.query(Match).filter_by(id=match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")

    members = (
        db.query(MatchPlayer, Player)
        .join(Player, Player.id == MatchPlayer.player_id)
        .filter(MatchPlayer.match_id == match_id)
        .all()
    )

    return {
        "id": match.id,
        "mode": match.mode,
        "status": match.status,
        "map_name": match.map_name,
        "captain_player_id": match.captain_player_id,
        "winner_team": match.winner_team,
        "players": [
            {
                "telegram_id": player.telegram_id,
                "nickname": player.nickname,
                "game_id": player.game_id,
                "team": membership.team,
            }
            for membership, player in members
        ],
    }


@app.post("/matches/{match_id}/result")
def report_result(
    match_id: int,
    data: ResultRequest,
    db: Session = Depends(get_db),
):
    match = db.query(Match).filter_by(id=match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")
    if match.status != "ready":
        raise HTTPException(409, "Match is not accepting results")

    player = db.query(Player).filter_by(
        telegram_id=data.telegram_id
    ).first()
    if not player:
        raise HTTPException(404, "Player not found")

    membership = db.query(MatchPlayer).filter_by(
        match_id=match_id,
        player_id=player.id,
    ).first()
    if not membership:
        raise HTTPException(403, "You are not in this match")

    membership.confirmed_result = data.winner_team
    db.flush()

    reports = (
        db.query(MatchPlayer)
        .filter_by(match_id=match_id)
        .all()
    )
    teams = {report.team for report in reports}
    team_votes = {
        team: sum(
            1 for report in reports
            if report.team == team
            and report.confirmed_result == data.winner_team
        )
        for team in teams
    }

    # A result is finalized only when every player confirms the same winner.
    if reports and all(
        report.confirmed_result == data.winner_team
        for report in reports
    ):
        match.winner_team = data.winner_team
        match.status = "completed"

    db.commit()

    return {
        "status": match.status,
        "reported_winner_team": data.winner_team,
        "message": (
            "Result confirmed"
            if match.status == "completed"
            else "Waiting for all players to confirm the same result"
        ),
    }

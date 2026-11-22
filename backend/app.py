import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

import chess
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/chess.sqlite3")
INIT_DATA_MAX_AGE = int(os.environ.get("INIT_DATA_MAX_AGE", "86400"))
DEFAULT_ELO = 1000
ELO_K = 32

Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Telegram Chess API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://klogest.github.io"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


def connect():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def initialize_database():
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT COLLATE NOCASE UNIQUE,
                first_name TEXT NOT NULL,
                elo INTEGER NOT NULL DEFAULT 1000,
                games_played INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS friend_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL REFERENCES players(telegram_id) ON DELETE CASCADE,
                to_id INTEGER NOT NULL REFERENCES players(telegram_id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                UNIQUE(from_id, to_id),
                CHECK(from_id <> to_id)
            );

            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                white_id INTEGER NOT NULL REFERENCES players(telegram_id),
                black_id INTEGER NOT NULL REFERENCES players(telegram_id),
                status TEXT NOT NULL DEFAULT 'active',
                fen TEXT NOT NULL,
                moves_json TEXT NOT NULL DEFAULT '[]',
                result TEXT,
                winner_id INTEGER REFERENCES players(telegram_id),
                white_delta INTEGER,
                black_delta INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_friend_to_status
                ON friend_requests(to_id, status);
            CREATE INDEX IF NOT EXISTS idx_games_players
                ON games(white_id, black_id, updated_at);
            """
        )


initialize_database()


def validate_init_data(init_data: str):
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Backend BOT_TOKEN is not configured")
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram signature check failed")
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="Telegram session expired; reopen the Mini App")
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing")
    return user


def authenticated_user(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    if not init_data:
        raise HTTPException(status_code=401, detail="Open this page from Telegram to sign in")
    telegram_user = validate_init_data(init_data)
    username = telegram_user.get("username")
    first_name = str(telegram_user.get("first_name") or "Игрок")[:64]
    try:
        with connect() as db:
            db.execute(
                """
                INSERT INTO players (telegram_id, username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (int(telegram_user["id"]), username, first_name, int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="This Telegram username is already linked to another account")
    return int(telegram_user["id"])


def player_json(row):
    return {
        "id": row["telegram_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "elo": row["elo"],
        "games_played": row["games_played"],
        "wins": row["wins"],
    }


def require_friendship(db, user_id, friend_id):
    row = db.execute(
        """
        SELECT 1 FROM friend_requests
        WHERE status = 'accepted'
          AND ((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))
        LIMIT 1
        """,
        (user_id, friend_id, friend_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="You can only invite an accepted friend")


class FriendRequestBody(BaseModel):
    username: str = Field(min_length=1, max_length=33)


class CreateGameBody(BaseModel):
    friend_username: str = Field(min_length=1, max_length=33)


class MoveBody(BaseModel):
    uci: str = Field(min_length=4, max_length=5)


def game_json(db, row, viewer_id):
    white = db.execute("SELECT * FROM players WHERE telegram_id = ?", (row["white_id"],)).fetchone()
    black = db.execute("SELECT * FROM players WHERE telegram_id = ?", (row["black_id"],)).fetchone()
    board = chess.Board(row["fen"])
    moves = json.loads(row["moves_json"])
    return {
        "id": row["id"],
        "status": row["status"],
        "fen": row["fen"],
        "moves": moves,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "legal_moves": [move.uci() for move in board.legal_moves]
        if row["status"] == "active" and (board.turn == chess.WHITE) == (viewer_id == row["white_id"])
        else [],
        "white": player_json(white),
        "black": player_json(black),
        "result": row["result"],
        "winner_id": row["winner_id"],
        "rating_change": {
            "white": row["white_delta"],
            "black": row["black_delta"],
        }
        if row["status"] == "finished"
        else None,
        "updated_at": row["updated_at"],
    }


def apply_elo(db, white_id, black_id, score_white):
    white = db.execute("SELECT elo FROM players WHERE telegram_id = ?", (white_id,)).fetchone()
    black = db.execute("SELECT elo FROM players WHERE telegram_id = ?", (black_id,)).fetchone()
    white_elo, black_elo = white["elo"], black["elo"]
    expected_white = 1 / (1 + 10 ** ((black_elo - white_elo) / 400))
    expected_black = 1 - expected_white
    score_black = 1 - score_white
    white_new = max(0, round(white_elo + ELO_K * (score_white - expected_white)))
    black_new = max(0, round(black_elo + ELO_K * (score_black - expected_black)))
    white_delta, black_delta = white_new - white_elo, black_new - black_elo
    db.execute(
        "UPDATE players SET elo = ?, games_played = games_played + 1, wins = wins + ? WHERE telegram_id = ?",
        (white_new, int(score_white == 1), white_id),
    )
    db.execute(
        "UPDATE players SET elo = ?, games_played = games_played + 1, wins = wins + ? WHERE telegram_id = ?",
        (black_new, int(score_black == 1), black_id),
    )
    return white_delta, black_delta


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/auth")
def authenticate(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user_id = authenticated_user(init_data)
    with connect() as db:
        row = db.execute("SELECT * FROM players WHERE telegram_id = ?", (user_id,)).fetchone()
        return {"player": player_json(row), "elo_k": ELO_K}


@app.get("/api/players")
def search_players(q: str = "", user_id: int = Depends(authenticated_user)):
    query = q.strip().lstrip("@")[:33]
    if len(query) < 2:
        return {"players": []}
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM players
            WHERE telegram_id <> ? AND username IS NOT NULL
              AND username LIKE ? ESCAPE '\\'
            ORDER BY username COLLATE NOCASE LIMIT 20
            """,
            (user_id, query.replace("%", "\\%").replace("_", "\\_") + "%"),
        ).fetchall()
        return {"players": [player_json(row) for row in rows]}


@app.post("/api/friends/request")
def send_friend_request(body: FriendRequestBody, user_id: int = Depends(authenticated_user)):
    username = body.username.strip().lstrip("@")
    if not username or username.casefold() == "":
        raise HTTPException(status_code=400, detail="Enter a Telegram username")
    with connect() as db:
        target = db.execute(
            "SELECT * FROM players WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Player not found. Ask them to open the Mini App first.")
        target_id = target["telegram_id"]
        if target_id == user_id:
            raise HTTPException(status_code=400, detail="You cannot add yourself")
        existing = db.execute(
            "SELECT * FROM friend_requests WHERE from_id = ? AND to_id = ?",
            (user_id, target_id),
        ).fetchone()
        reverse = db.execute(
            "SELECT * FROM friend_requests WHERE from_id = ? AND to_id = ?",
            (target_id, user_id),
        ).fetchone()
        if existing and existing["status"] == "accepted":
            return {"status": "accepted", "player": player_json(target)}
        if reverse and reverse["status"] == "pending":
            db.execute("UPDATE friend_requests SET status = 'accepted' WHERE id = ?", (reverse["id"],))
            if existing:
                db.execute("UPDATE friend_requests SET status = 'accepted' WHERE id = ?", (existing["id"],))
            else:
                db.execute(
                    "INSERT INTO friend_requests(from_id, to_id, status, created_at) VALUES (?, ?, 'accepted', ?)",
                    (user_id, target_id, int(time.time())),
                )
            return {"status": "accepted", "player": player_json(target)}
        if existing and existing["status"] == "pending":
            return {"status": "pending", "player": player_json(target)}
        db.execute(
            """
            INSERT INTO friend_requests(from_id, to_id, status, created_at)
            VALUES (?, ?, 'pending', ?)
            ON CONFLICT(from_id, to_id) DO UPDATE SET status = 'pending', created_at = excluded.created_at
            """,
            (user_id, target_id, int(time.time())),
        )
        return {"status": "pending", "player": player_json(target)}


@app.get("/api/friends")
def list_friends(user_id: int = Depends(authenticated_user)):
    with connect() as db:
        friends = db.execute(
            """
            SELECT DISTINCT p.* FROM players p
            JOIN friend_requests f ON (
                (f.from_id = ? AND f.to_id = p.telegram_id) OR
                (f.to_id = ? AND f.from_id = p.telegram_id)
            )
            WHERE f.status = 'accepted'
            ORDER BY p.username COLLATE NOCASE
            """,
            (user_id, user_id),
        ).fetchall()
        incoming = db.execute(
            """
            SELECT f.id AS request_id, p.* FROM friend_requests f
            JOIN players p ON p.telegram_id = f.from_id
            WHERE f.to_id = ? AND f.status = 'pending'
            ORDER BY f.created_at
            """,
            (user_id,),
        ).fetchall()
        outgoing = db.execute(
            """
            SELECT f.id AS request_id, p.* FROM friend_requests f
            JOIN players p ON p.telegram_id = f.to_id
            WHERE f.from_id = ? AND f.status = 'pending'
            ORDER BY f.created_at
            """,
            (user_id,),
        ).fetchall()
        return {
            "friends": [player_json(row) for row in friends],
            "incoming": [{"request_id": row["request_id"], **player_json(row)} for row in incoming],
            "outgoing": [{"request_id": row["request_id"], **player_json(row)} for row in outgoing],
        }


@app.post("/api/friends/{request_id}/accept")
def accept_friend_request(request_id: int, user_id: int = Depends(authenticated_user)):
    with connect() as db:
        request = db.execute(
            "SELECT * FROM friend_requests WHERE id = ? AND to_id = ? AND status = 'pending'",
            (request_id, user_id),
        ).fetchone()
        if not request:
            raise HTTPException(status_code=404, detail="Friend request not found")
        now = int(time.time())
        db.execute("UPDATE friend_requests SET status = 'accepted' WHERE id = ?", (request_id,))
        db.execute(
            """
            INSERT INTO friend_requests(from_id, to_id, status, created_at)
            VALUES (?, ?, 'accepted', ?)
            ON CONFLICT(from_id, to_id) DO UPDATE SET status = 'accepted'
            """,
            (user_id, request["from_id"], now),
        )
        return {"status": "accepted"}


@app.post("/api/friends/{request_id}/decline")
def decline_friend_request(request_id: int, user_id: int = Depends(authenticated_user)):
    with connect() as db:
        result = db.execute(
            "UPDATE friend_requests SET status = 'declined' WHERE id = ? AND to_id = ? AND status = 'pending'",
            (request_id, user_id),
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Friend request not found")
        return {"status": "declined"}


@app.get("/api/leaderboard")
def leaderboard(user_id: int = Depends(authenticated_user)):
    with connect() as db:
        rows = db.execute(
            """
            SELECT telegram_id, username, first_name, elo, games_played, wins
            FROM players
            ORDER BY elo DESC, wins DESC, username COLLATE NOCASE
            LIMIT 50
            """
        ).fetchall()
        return {"players": [player_json(row) for row in rows]}


@app.get("/api/games")
def list_games(user_id: int = Depends(authenticated_user)):
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM games
            WHERE (white_id = ? OR black_id = ?)
            ORDER BY updated_at DESC LIMIT 30
            """,
            (user_id, user_id),
        ).fetchall()
        return {"games": [game_json(db, row, user_id) for row in rows]}


@app.post("/api/games")
def create_game(body: CreateGameBody, user_id: int = Depends(authenticated_user)):
    username = body.friend_username.strip().lstrip("@")
    now = int(time.time())
    with connect() as db:
        friend = db.execute(
            "SELECT * FROM players WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not friend:
            raise HTTPException(status_code=404, detail="Player not found. Ask them to open the Mini App first.")
        if friend["telegram_id"] == user_id:
            raise HTTPException(status_code=400, detail="You cannot play against yourself")
        require_friendship(db, user_id, friend["telegram_id"])
        game_id = uuid.uuid4().hex[:12]
        board = chess.Board()
        db.execute(
            """
            INSERT INTO games(id, white_id, black_id, status, fen, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (game_id, user_id, friend["telegram_id"], board.fen(), now, now),
        )
        row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return game_json(db, row, user_id)


@app.get("/api/games/{game_id}")
def get_game(game_id: str, user_id: int = Depends(authenticated_user)):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM games WHERE id = ? AND (white_id = ? OR black_id = ?)",
            (game_id, user_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Game not found")
        return game_json(db, row, user_id)


@app.post("/api/games/{game_id}/move")
def make_move(game_id: str, body: MoveBody, user_id: int = Depends(authenticated_user)):
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row or user_id not in (row["white_id"], row["black_id"]):
            raise HTTPException(status_code=404, detail="Game not found")
        if row["status"] != "active":
            raise HTTPException(status_code=409, detail="Game is already finished")

        board = chess.Board(row["fen"])
        expected_user = row["white_id"] if board.turn == chess.WHITE else row["black_id"]
        if user_id != expected_user:
            raise HTTPException(status_code=409, detail="Wait for your turn")
        try:
            move = chess.Move.from_uci(body.uci)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid chess move")
        if move not in board.legal_moves:
            raise HTTPException(status_code=400, detail="Illegal chess move")

        san = board.san(move)
        board.push(move)
        moves = json.loads(row["moves_json"])
        moves.append({"uci": move.uci(), "san": san})
        outcome = board.outcome(claim_draw=True)
        result = None
        winner_id = None
        white_delta = black_delta = None
        status = "active"
        if outcome:
            status = "finished"
            result = outcome.result()
            if result == "1-0":
                winner_id, score_white = row["white_id"], 1.0
            elif result == "0-1":
                winner_id, score_white = row["black_id"], 0.0
            else:
                score_white = 0.5
            white_delta, black_delta = apply_elo(db, row["white_id"], row["black_id"], score_white)

        db.execute(
            """
            UPDATE games
            SET fen = ?, moves_json = ?, status = ?, result = ?, winner_id = ?,
                white_delta = ?, black_delta = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                board.fen(), json.dumps(moves), status, result, winner_id,
                white_delta, black_delta, now, game_id,
            ),
        )
        updated = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return game_json(db, updated, user_id)


@app.post("/api/games/{game_id}/resign")
def resign_game(game_id: str, user_id: int = Depends(authenticated_user)):
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM games WHERE id = ? AND status = 'active' AND (white_id = ? OR black_id = ?)",
            (game_id, user_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active game not found")
        winner_id = row["black_id"] if user_id == row["white_id"] else row["white_id"]
        score_white = 0.0 if user_id == row["white_id"] else 1.0
        white_delta, black_delta = apply_elo(db, row["white_id"], row["black_id"], score_white)
        db.execute(
            """
            UPDATE games
            SET status = 'finished', result = ?, winner_id = ?,
                white_delta = ?, black_delta = ?, updated_at = ?
            WHERE id = ?
            """,
            ("0-1" if score_white == 0 else "1-0", winner_id, white_delta, black_delta, now, game_id),
        )
        updated = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return game_json(db, updated, user_id)

"""
Simple chat website (inspired by y99.in) with real accounts.

Users must register with a username (5+ characters) and a password
before they can chat. Accounts and rooms are stored in a local SQLite
database (chat.db, created automatically on first run); chat messages
themselves stay in memory only, like a guest chat room.

Rooms are stored as rows with a numeric primary key. A room is always
addressed by that numeric id in the URL: /r/<room_id>.

Run with:
    python app.py

Then open http://localhost:5000 in your browser.
"""

import sqlite3
import secrets
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, g, abort
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)
socketio = SocketIO(app, cors_allowed_origins="*")

DB_PATH = "chat.db"

USERNAME_MIN_LEN = 5   # usernames must be LONGER than 4 characters
PASSWORD_MIN_LEN = 6
ROOM_NAME_MAX_LEN = 32

# --- In-memory chat state -----------------------------------------------
# Messages and "who's online" are ephemeral, like a guest chat room —
# only accounts and the room directory itself are persisted.

MAX_HISTORY = 100
message_history = {}   # room_id (int) -> list of {username, message, time}
room_users = {}         # room_id (int) -> set of usernames currently online

# Seeded once on first run. (name, description)
FEATURED_ROOMS = [
    ("Lobby", "Say hi and see who's around."),
    ("Random", "Whatever's on your mind."),
    ("Tech", "Gadgets, code, and internet nonsense."),
    ("Music", "New releases and old favorites."),
    ("Sports", "Games, scores, hot takes."),
]


# --- Database helpers -----------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            featured INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    existing = {
        row["name"].lower()
        for row in db.execute("SELECT name FROM rooms").fetchall()
    }
    for name, description in FEATURED_ROOMS:
        if name.lower() not in existing:
            db.execute(
                "INSERT INTO rooms (name, description, featured, created_at) VALUES (?, ?, 1, ?)",
                (name, description, datetime.now().isoformat()),
            )
    db.commit()
    db.close()


def get_user_by_username(username):
    db = get_db()
    return db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def create_user(username, password):
    db = get_db()
    db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), datetime.now().isoformat()),
    )
    db.commit()


def get_room(room_id):
    db = get_db()
    return db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()


def get_room_by_name(name):
    db = get_db()
    return db.execute(
        "SELECT * FROM rooms WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def get_or_create_room(name):
    """Look up a room by name (case-insensitive), creating it if needed.
    Always returns a room row with a numeric id."""
    room = get_room_by_name(name)
    if room:
        return room

    db = get_db()
    db.execute(
        "INSERT INTO rooms (name, description, featured, created_at) VALUES (?, '', 0, ?)",
        (name, datetime.now().isoformat()),
    )
    db.commit()
    return get_room_by_name(name)


def list_featured_rooms():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM rooms WHERE featured = 1 ORDER BY id ASC"
    ).fetchall()
    return [with_online_count(r) for r in rows]


def list_active_rooms():
    """Rooms that currently have at least one person online."""
    db = get_db()
    active_ids = [rid for rid, users in room_users.items() if users]
    if not active_ids:
        return []
    placeholders = ",".join("?" for _ in active_ids)
    rows = db.execute(
        f"SELECT * FROM rooms WHERE id IN ({placeholders})", active_ids
    ).fetchall()
    rows = [with_online_count(r) for r in rows]
    rows.sort(key=lambda r: r["online_count"], reverse=True)
    return rows


def with_online_count(room_row):
    room = dict(room_row)
    room["online_count"] = len(room_users.get(room["id"], set()))
    return room


# --- Auth helpers -----------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def add_message(room_id, username, text):
    entry = {
        "username": username,
        "message": text,
        "time": datetime.now().strftime("%H:%M"),
    }
    history = message_history.setdefault(room_id, [])
    history.append(entry)
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]
    return entry


# --- Auth routes ------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        error = None
        if len(username) <= USERNAME_MIN_LEN - 1:
            error = f"Username must be longer than {USERNAME_MIN_LEN - 1} characters."
        elif not username.replace("_", "").isalnum():
            error = "Username can only contain letters, numbers, and underscores."
        elif len(password) < PASSWORD_MIN_LEN:
            error = f"Password must be at least {PASSWORD_MIN_LEN} characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif get_user_by_username(username):
            error = "That username is already taken."

        if error:
            return render_template("register.html", error=error, username=username)

        create_user(username, password)
        session["username"] = username
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_user_by_username(username)
        if user is None or not check_password_hash(user["password_hash"], password):
            return render_template(
                "login.html", error="Incorrect username or password.", username=username
            )

        session["username"] = user["username"]
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))


# --- Chat routes --------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        name = request.form.get("room", "").strip()[:ROOM_NAME_MAX_LEN] or "Lobby"
        room = get_or_create_room(name)
        return redirect(url_for("room", room_id=room["id"]))

    return render_template(
        "index.html",
        featured_rooms=list_featured_rooms(),
        active_rooms=list_active_rooms(),
        username=session["username"],
    )


@app.route("/r/<int:room_id>")
@login_required
def room(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        abort(404)

    username = session["username"]
    history = message_history.get(room_id, [])
    online = sorted(room_users.get(room_id, set()))
    return render_template(
        "room.html",
        room=room_row,
        username=username,
        history=history,
        online=online,
    )


# --- Socket.IO events -------------------------------------------------------
# Socket.IO rooms are keyed by the string form of the numeric room id.

@socketio.on("join")
def handle_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)

    join_room(str(room_id))
    room_users.setdefault(room_id, set()).add(username)

    emit(
        "system",
        {"msg": f"{username} joined the room.", "type": "join"},
        room=str(room_id),
    )
    emit(
        "roster",
        {"online": sorted(room_users.get(room_id, set()))},
        room=str(room_id),
    )


@socketio.on("leave")
def handle_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)

    leave_room(str(room_id))
    room_users.get(room_id, set()).discard(username)

    emit(
        "system",
        {"msg": f"{username} left the room.", "type": "leave"},
        room=str(room_id),
    )
    emit(
        "roster",
        {"online": sorted(room_users.get(room_id, set()))},
        room=str(room_id),
    )


@socketio.on("message")
def handle_message(data):
    room_id = data.get("room_id")
    username = session.get("username")
    text = (data.get("message") or "").strip()[:1000]

    if room_id is None or not username or not text:
        return
    room_id = int(room_id)

    entry = add_message(room_id, username, text)
    emit("message", entry, room=str(room_id))


@socketio.on("disconnect")
def handle_disconnect():
    # Best-effort cleanup; the client also sends an explicit "leave" event
    # when the page unloads, so this mostly covers dropped connections.
    pass


init_db()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)

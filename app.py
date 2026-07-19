"""
Simple chat website (inspired by y99.in) with real accounts + Voice Chat.
"""

import sqlite3
import secrets
import json
import os
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, g, abort
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)

# Use gevent instead of eventlet to avoid deprecation warning
socketio = SocketIO(app, 
                   cors_allowed_origins="*", 
                   async_mode='gevent')   # Changed from default/eventlet

DB_PATH = "chat.db"
USERS_JSON = "users.json"

USERNAME_MIN_LEN = 5
PASSWORD_MIN_LEN = 6
ROOM_NAME_MAX_LEN = 32

# In-memory state
MAX_HISTORY = 100
message_history = {}
room_users = {}
typing_users = {}

FEATURED_ROOMS = [
    ("Lobby", "Say hi and see who's around."),
    ("Suggestions", "Suggest changes to the site."),  
    ("Random", "Whatever's on your mind."),
    ("Tech", "Gadgets, code, and internet nonsense."),
    ("Music", "New releases and old favorites."),
    ("Sports", "Games, scores, hot takes."),  
]

# JSON User Storage
def load_users():
    if os.path.exists(USERS_JSON):
        try:
            with open(USERS_JSON, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_users(users_dict):
    with open(USERS_JSON, 'w') as f:
        json.dump(users_dict, f, indent=2)

users_db = load_users()

def get_user_by_username(username):
    return users_db.get(username)

def create_user(username, password):
    users_db[username] = {
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now().isoformat()
    }
    save_users(users_db)

# SQLite for Rooms
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
    db.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            featured INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    existing = {row[0].lower() for row in db.execute("SELECT name FROM rooms").fetchall()}
    for name, description in FEATURED_ROOMS:
        if name.lower() not in existing:
            db.execute(
                "INSERT INTO rooms (name, description, featured, created_at) VALUES (?, ?, 1, ?)",
                (name, description, datetime.now().isoformat())
            )
    db.commit()
    db.close()

# ... (rest of your routes and functions stay the same) ...

# Voice Chat Signaling
@socketio.on("voice_offer")
def handle_voice_offer(data):
    target = data.get("target")
    offer = data.get("offer")
    username = session.get("username")
    if target and offer and username:
        emit("voice_offer", {"from": username, "offer": offer}, room=target)

@socketio.on("voice_answer")
def handle_voice_answer(data):
    target = data.get("target")
    answer = data.get("answer")
    username = session.get("username")
    if target and answer and username:
        emit("voice_answer", {"from": username, "answer": answer}, room=target)

@socketio.on("voice_ice")
def handle_voice_ice(data):
    target = data.get("target")
    candidate = data.get("candidate")
    username = session.get("username")
    if target and candidate and username:
        emit("voice_ice", {"from": username, "candidate": candidate}, room=target)

@socketio.on("voice_join")
def handle_voice_join(data):
    username = session.get("username")
    if username:
        emit("voice_user_joined", {"username": username}, broadcast=True, include_self=False)

# Standard Socket Events (unchanged)
@socketio.on("join")
def handle_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    join_room(str(room_id))
    room_users.setdefault(room_id, set()).add(username)
    emit("roster", {"online": sorted(room_users.get(room_id, set()))}, room=str(room_id))

@socketio.on("leave")
def handle_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    leave_room(str(room_id))
    room_users.get(room_id, set()).discard(username)
    emit("roster", {"online": sorted(room_users.get(room_id, set()))}, room=str(room_id))

@socketio.on("message")
def handle_message(data):
    room_id = data.get("room_id")
    username = session.get("username")
    text = (data.get("message") or "").strip()[:1000]
    if room_id is None or not username or not text:
        return
    room_id = int(room_id)
    entry = {
        "username": username,
        "message": text,
        "time": datetime.now().strftime("%H:%M"),
    }
    message_history.setdefault(room_id, []).append(entry)
    emit("message", entry, room=str(room_id))

@socketio.on("disconnect")
def handle_disconnect():
    pass

# Initialize
init_db()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)

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

# Use gevent to avoid Eventlet deprecation
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

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

def get_room(room_id):
    db = get_db()
    return db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()

def get_room_by_name(name):
    db = get_db()
    return db.execute("SELECT * FROM rooms WHERE name = ? COLLATE NOCASE", (name,)).fetchone()

def get_or_create_room(name):
    room = get_room_by_name(name)
    if room:
        return room
    db = get_db()
    db.execute(
        "INSERT INTO rooms (name, description, featured, created_at) VALUES (?, '', 0, ?)",
        (name, datetime.now().isoformat())
    )
    db.commit()
    return get_room_by_name(name)

def list_featured_rooms():
    db = get_db()
    rows = db.execute("SELECT * FROM rooms WHERE featured = 1 ORDER BY id ASC").fetchall()
    return [with_online_count(r) for r in rows]

def list_active_rooms():
    db = get_db()
    active_ids = [rid for rid, users in room_users.items() if users]
    if not active_ids:
        return []
    placeholders = ",".join("?" for _ in active_ids)
    rows = db.execute(f"SELECT * FROM rooms WHERE id IN ({placeholders})", active_ids).fetchall()
    rows = [with_online_count(r) for r in rows]
    rows.sort(key=lambda r: r["online_count"], reverse=True)
    return rows

def with_online_count(room_row):
    room = dict(room_row)
    room["online_count"] = len(room_users.get(room["id"], set()))
    return room

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
        del history[:len(history) - MAX_HISTORY]
    return entry

# ====================== ROUTES ======================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        error = None
        if len(username) < USERNAME_MIN_LEN:
            error = f"Username must be at least {USERNAME_MIN_LEN} characters."
        elif not username.replace("_", "").isalnum():
            error = "Username can only contain letters, numbers, and underscores."
        elif len(password) < PASSWORD_MIN_LEN:
            error = f"Password must be at least {PASSWORD_MIN_LEN} characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif get_user_by_username(username):
            error = "Username already taken."

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
        if not user or not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Incorrect username or password.", username=username)

        session["username"] = username
        return redirect(url_for("index"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

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

    username = session.get("username")
    history = message_history.get(room_id, [])
    online = sorted(room_users.get(room_id, set()))

    return render_template(
        "room.html",
        room=room_row,
        username=username,
        history=history,
        online=online,
        active_rooms=list_active_rooms(),
    )
    
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    username = session["username"]
    error = None
    success = None

    if request.method == "POST":
        file = request.files.get("avatar")
        rel_path, err = save_uploaded_image(
            file, AVATAR_DIR, MAX_AVATAR_DIMENSION, filename_prefix=f"{username}_"
        )
        if err:
            error = err
        else:
            old = users_db.get(username, {}).get("avatar")
            users_db.setdefault(username, {})["avatar"] = rel_path
            save_users(users_db)
            if old:
                old_path = os.path.join("static", old)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            success = "Profile picture updated."

    return render_template(
        "profile.html",
        username=username,
        avatar_url=get_user_avatar(username),
        error=error,
        success=success,
    )
    
# Admin routes (kept simple)
@app.route("/admin")
@login_required
def admin_dashboard():
    if session.get("username") not in ["syphir", "admin"]:
        abort(403)
    return render_template("admin.html", username=session["username"])

# Voice Signaling
@socketio.on("voice_offer")
def handle_voice_offer(data):
    target = data.get("target")
    offer = data.get("offer")
    if target and offer:
        emit("voice_offer", {"from": session.get("username"), "offer": offer}, room=target)

@socketio.on("voice_answer")
def handle_voice_answer(data):
    target = data.get("target")
    answer = data.get("answer")
    if target and answer:
        emit("voice_answer", {"from": session.get("username"), "answer": answer}, room=target)

@socketio.on("voice_ice")
def handle_voice_ice(data):
    target = data.get("target")
    candidate = data.get("candidate")
    if target and candidate:
        emit("voice_ice", {"from": session.get("username"), "candidate": candidate}, room=target)

@socketio.on("voice_join")
def handle_voice_join(data):
    username = session.get("username")
    if username:
        emit("voice_user_joined", {"username": username}, broadcast=True, include_self=False)

# Standard Chat Events
@socketio.on("join")
def handle_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if not room_id or not username:
        return
    room_id = int(room_id)
    join_room(str(room_id))
    room_users.setdefault(room_id, set()).add(username)
    emit("roster", {"online": sorted(room_users[room_id])}, room=str(room_id))

@socketio.on("message")
def handle_message(data):
    room_id = data.get("room_id")
    username = session.get("username")
    text = (data.get("message") or "").strip()[:1000]
    if not room_id or not username or not text:
        return
    room_id = int(room_id)
    entry = add_message(room_id, username, text)
    emit("message", entry, room=str(room_id))

if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)

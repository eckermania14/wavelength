"""
Simple chat website (inspired by y99.in) with real accounts.
Users saved to users.json + admin broadcast support.
"""
import eventlet
eventlet.monkey_patch()

import sqlite3
import secrets
import json
import os
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, g, abort
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB per request (image uploads)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

DB_PATH = "chat.db"
USERS_JSON = "users.json"

USERNAME_MIN_LEN = 5
PASSWORD_MIN_LEN = 6
ROOM_NAME_MAX_LEN = 32

# ==================== Uploads (avatars + chat images) ====================
# NOTE: on Railway, local disk storage is EPHEMERAL. Files saved here (and
# users.json / chat.db) will be wiped on redeploy/restart unless you attach
# a Railway Volume mounted over this directory (and the db/json paths).
UPLOAD_ROOT = os.path.join("static", "uploads")
AVATAR_DIR = os.path.join(UPLOAD_ROOT, "avatars")
CHAT_IMAGE_DIR = os.path.join(UPLOAD_ROOT, "chat")
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(CHAT_IMAGE_DIR, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_CHAT_IMAGE_DIMENSION = 1600  # px, longest side
MAX_AVATAR_DIMENSION = 512

# In-memory chat state
MAX_HISTORY = 100
message_history = {}
room_users = {}
typing_users = {}  # room_id -> set of typing usernames

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
        "created_at": datetime.now().isoformat(),
        "avatar": None,
    }
    save_users(users_db)


def get_user_avatar(username):
    """Returns a URL for the user's avatar, or None if they haven't set one."""
    user = get_user_by_username(username)
    if user and user.get("avatar"):
        return url_for("static", filename=user["avatar"])
    return None


# ==================== Image upload helpers ====================

def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_image(file_storage, dest_dir, max_dimension, filename_prefix=""):
    """
    Validate, downscale, and save an uploaded image.
    Returns (relative_static_path, error_message). Exactly one will be None.
    """
    if not file_storage or file_storage.filename == "":
        return None, "No file selected."
    if not allowed_image(file_storage.filename):
        return None, "Unsupported file type. Use PNG, JPG, GIF, or WEBP."

    try:
        # Verify it's really an image (defends against renamed non-image files)
        image = Image.open(file_storage.stream)
        image.verify()
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image)  # respect camera rotation
        if "A" in image.getbands():
            image = image.convert("RGBA")
        else:
            image = image.convert("RGB")
    except Exception:
        return None, "That file doesn't look like a valid image."

    image.thumbnail((max_dimension, max_dimension))
    ext = "png" if image.mode == "RGBA" else "jpg"
    fname = f"{filename_prefix}{uuid.uuid4().hex}.{ext}"
    path = os.path.join(dest_dir, fname)
    save_kwargs = {"quality": 85, "optimize": True} if ext == "jpg" else {"optimize": True}
    image.save(path, **save_kwargs)

    rel = os.path.relpath(path, "static").replace(os.sep, "/")
    return rel, None


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
        row[0].lower()
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


def get_room(room_id):
    db = get_db()
    return db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()


def get_room_by_name(name):
    db = get_db()
    return db.execute(
        "SELECT * FROM rooms WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def get_or_create_room(name):
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


def online_users_with_avatars(room_id):
    names = sorted(room_users.get(room_id, set()))
    return [{"username": u, "avatar": get_user_avatar(u)} for u in names]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def add_message(room_id, username, text, image_url=None):
    entry = {
        "username": username,
        "message": text,
        "image_url": image_url,
        "avatar": get_user_avatar(username),
        "time": datetime.now().strftime("%H:%M"),
    }
    history = message_history.setdefault(room_id, [])
    history.append(entry)
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]
    return entry


# Routes
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
        if not user or not check_password_hash(user["password_hash"], password):
            return render_template(
                "login.html", error="Incorrect username or password.", username=username
            )
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
        avatar_url=get_user_avatar(session["username"]),
    )


@app.route("/r/<int:room_id>")
@login_required
def room(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        abort(404)
    username = session["username"]
    history = message_history.get(room_id, [])
    online = online_users_with_avatars(room_id)
    return render_template(
        "room.html",
        room=room_row,
        username=username,
        avatar_url=get_user_avatar(username),
        history=history,
        online=online,
        active_rooms=list_active_rooms(),
    )


# ==================== Profile / avatar upload ====================

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


# ==================== Chat image upload ====================

@app.route("/upload/chat-image", methods=["POST"])
@login_required
def upload_chat_image():
    file = request.files.get("image")
    rel_path, err = save_uploaded_image(file, CHAT_IMAGE_DIR, MAX_CHAT_IMAGE_DIMENSION)
    if err:
        return {"error": err}, 400
    return {"url": url_for("static", filename=rel_path)}


@app.errorhandler(413)
def too_large(e):
    if request.path.startswith("/upload/"):
        return {"error": "File is too large (max 8MB)."}, 413
    return "File is too large.", 413


# Admin Dashboard
@app.route("/admin")
@login_required
def admin_dashboard():
    if session.get("username") not in ["syphir", "admin"]:
        abort(403)
    online_users = []
    for room_id, users in room_users.items():
        room = get_room(room_id)
        room_name = room["name"] if room else "Unknown"
        for u in users:
            online_users.append({
                "username": u,
                "room_id": room_id,
                "room_name": room_name
            })
    return render_template("admin.html",
                           username=session["username"],
                           online_users=online_users,
                           total_online=len(online_users))


# Global notification
@app.route("/admin/notify", methods=["POST"])
@login_required
def admin_notify():
    if session.get("username") not in ["syphir", "admin"]:
        abort(403)
    title = request.form.get("title", "Server Notice")
    message = request.form.get("message", "Server maintenance in progress.")
    socketio.emit('admin_notice', {
        "title": title,
        "message": message
    }, broadcast=True)
    return redirect(url_for("admin_dashboard"))


# Kick user
@app.route("/admin/kick", methods=["POST"])
@login_required
def admin_kick():
    if session.get("username") not in ["syphir", "admin"]:
        abort(403)
    username = request.form.get("username")
    room_id = request.form.get("room_id")
    if username and room_id:
        room_id = int(room_id)
        if username in room_users.get(room_id, set()):
            room_users[room_id].discard(username)
            socketio.emit("system", {
                "msg": f"{username} was kicked by admin.",
                "type": "leave"
            }, room=str(room_id))
    return redirect(url_for("admin_dashboard"))


# ==================== VOICE CHAT SIGNALING ====================

# room_id -> {username: sid}
voice_room_users = {}


def _voice_leave_all(sid):
    """Remove a disconnecting socket from every voice room it was in and notify peers."""
    for room_id, users in list(voice_room_users.items()):
        username = next((u for u, s in users.items() if s == sid), None)
        if username:
            users.pop(username, None)
            emit("voice_user_left", {"username": username, "sid": sid},
                 room=str(room_id), include_self=False)
            if not users:
                voice_room_users.pop(room_id, None)


@socketio.on("voice_join")
def handle_voice_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    sid = request.sid

    existing = voice_room_users.get(room_id, {})
    emit("voice_peers", {
        "peers": [{"username": u, "sid": s} for u, s in existing.items()]
    })

    voice_room_users.setdefault(room_id, {})[username] = sid
    emit("voice_user_joined", {"username": username, "sid": sid},
         room=str(room_id), include_self=False)


@socketio.on("voice_leave")
def handle_voice_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    voice_room_users.get(room_id, {}).pop(username, None)
    if not voice_room_users.get(room_id):
        voice_room_users.pop(room_id, None)
    emit("voice_user_left", {"username": username, "sid": request.sid},
         room=str(room_id), include_self=False)


@socketio.on("voice_offer")
def handle_voice_offer(data):
    target = data.get("target")
    offer = data.get("offer")
    username = session.get("username")
    if target and offer and username:
        emit("voice_offer", {"from": request.sid, "username": username, "offer": offer},
             room=target)


@socketio.on("voice_answer")
def handle_voice_answer(data):
    target = data.get("target")
    answer = data.get("answer")
    username = session.get("username")
    if target and answer and username:
        emit("voice_answer", {"from": request.sid, "username": username, "answer": answer},
             room=target)


@socketio.on("voice_ice")
def handle_voice_ice(data):
    target = data.get("target")
    candidate = data.get("candidate")
    if target and candidate:
        emit("voice_ice", {"from": request.sid, "candidate": candidate}, room=target)


# Socket.IO Events
@socketio.on("join")
def handle_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    join_room(str(room_id))
    room_users.setdefault(room_id, set()).add(username)
    emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))


@socketio.on("leave")
def handle_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    leave_room(str(room_id))
    room_users.get(room_id, set()).discard(username)
    emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))


@socketio.on("message")
def handle_message(data):
    room_id = data.get("room_id")
    username = session.get("username")
    text = (data.get("message") or "").strip()[:1000]
    image_url = data.get("image_url")

    # Only accept image URLs that point at our own chat-upload directory
    if image_url and not image_url.startswith(url_for("static", filename="uploads/chat/")):
        image_url = None

    if room_id is None or not username or (not text and not image_url):
        return
    room_id = int(room_id)
    entry = add_message(room_id, username, text, image_url=image_url)
    emit("message", entry, room=str(room_id))


@socketio.on("typing")
def handle_typing(data):
    room_id = data.get("room_id")
    username = session.get("username")
    is_typing = data.get("isTyping", False)
    if room_id is None or not username:
        return
    room_id = int(room_id)
    if is_typing:
        typing_users.setdefault(room_id, set()).add(username)
    else:
        typing_users.get(room_id, set()).discard(username)
    emit("typing", {
        "typingUsers": list(typing_users.get(room_id, set()))
    }, room=str(room_id))


@socketio.on("disconnect")
def handle_disconnect():
    _voice_leave_all(request.sid)


# Initialize
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

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

# Use gevent to avoid Eventlet deprecation
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

DB_PATH = "chat.db"
USERS_JSON = "users.json"

USERNAME_MIN_LEN = 5
PASSWORD_MIN_LEN = 6
ROOM_NAME_MAX_LEN = 32

ADMIN_USERS = {"syphir", "admin"}

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

# ==================== Uploads (avatars + chat images) ====================
# NOTE: on most PaaS hosts (Railway, etc.) local disk storage is EPHEMERAL.
# Files saved here (and users.json / chat.db) will be wiped on redeploy or
# restart unless you attach a persistent volume mounted over this directory.
UPLOAD_ROOT = os.path.join("static", "uploads")
AVATAR_DIR = os.path.join(UPLOAD_ROOT, "avatars")
CHAT_IMAGE_DIR = os.path.join(UPLOAD_ROOT, "chat")
ROOM_BG_DIR = os.path.join(UPLOAD_ROOT, "room_backgrounds")
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(CHAT_IMAGE_DIR, exist_ok=True)
os.makedirs(ROOM_BG_DIR, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_CHAT_IMAGE_DIMENSION = 1600  # px, longest side
MAX_AVATAR_DIMENSION = 512
MAX_ROOM_BG_DIMENSION = 1920
BIO_MAX_LEN = 280


def load_users():
    if os.path.exists(USERS_JSON):
        try:
            with open(USERS_JSON, 'r') as f:
                return json.load(f)
        except Exception:
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
        "bio": "",
    }
    save_users(users_db)


def get_user_avatar(username):
    """Returns a static URL for the user's avatar, or None if they haven't set one."""
    user = get_user_by_username(username)
    if user and user.get("avatar"):
        return url_for("static", filename=user["avatar"])
    return None


def get_user_bio(username):
    user = get_user_by_username(username)
    return (user or {}).get("bio", "") or ""


def update_user_bio(username, bio):
    users_db.setdefault(username, {})["bio"] = bio
    save_users(users_db)


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

    # Migrate in any columns added after the original schema was shipped.
    existing_cols = {row[1] for row in db.execute("PRAGMA table_info(rooms)").fetchall()}
    for col, ddl in [
        ("owner", "ALTER TABLE rooms ADD COLUMN owner TEXT"),
        ("password_hash", "ALTER TABLE rooms ADD COLUMN password_hash TEXT"),
        ("background_image", "ALTER TABLE rooms ADD COLUMN background_image TEXT"),
    ]:
        if col not in existing_cols:
            db.execute(ddl)

    db.execute("""
        CREATE TABLE IF NOT EXISTS room_moderators (
            room_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            added_by TEXT,
            added_at TEXT NOT NULL,
            PRIMARY KEY (room_id, username)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS room_bans (
            room_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            banned_by TEXT,
            banned_at TEXT NOT NULL,
            PRIMARY KEY (room_id, username)
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


# ==================== Custom rooms: ownership / moderators / bans ====================

def list_custom_rooms(query=""):
    """Non-featured, user-created rooms (i.e. rooms with an owner), optionally
    filtered by a case-insensitive substring match on name or description."""
    db = get_db()
    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        rows = db.execute(
            "SELECT * FROM rooms WHERE owner IS NOT NULL AND (name LIKE ? OR description LIKE ?) "
            "ORDER BY name COLLATE NOCASE",
            (like, like),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM rooms WHERE owner IS NOT NULL ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [with_online_count(r) for r in rows]


def create_custom_room(name, description, password, background_rel, owner):
    """Returns (room_row, error). Exactly one will be None."""
    name = (name or "").strip()[:ROOM_NAME_MAX_LEN]
    if not name:
        return None, "Room name is required."
    if get_room_by_name(name):
        return None, "A room with that name already exists."

    db = get_db()
    password_hash = generate_password_hash(password) if password else None
    db.execute(
        "INSERT INTO rooms (name, description, featured, created_at, owner, password_hash, background_image) "
        "VALUES (?, ?, 0, ?, ?, ?, ?)",
        (name, (description or "").strip()[:280], datetime.now().isoformat(), owner, password_hash, background_rel),
    )
    db.commit()
    return get_room_by_name(name), None


def is_room_owner(room_row, username):
    return bool(username) and room_row["owner"] == username


def is_room_moderator(room_id, username):
    if not username:
        return False
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM room_moderators WHERE room_id = ? AND username = ?", (room_id, username)
    ).fetchone()
    return row is not None


def can_moderate_room(room_row, username):
    return is_room_owner(room_row, username) or is_room_moderator(room_row["id"], username)


def list_room_moderators(room_id):
    db = get_db()
    rows = db.execute(
        "SELECT username, added_by, added_at FROM room_moderators WHERE room_id = ? ORDER BY username COLLATE NOCASE",
        (room_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_room_moderator(room_id, username, added_by):
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO room_moderators (room_id, username, added_by, added_at) VALUES (?, ?, ?, ?)",
        (room_id, username, added_by, datetime.now().isoformat()),
    )
    db.commit()


def remove_room_moderator(room_id, username):
    db = get_db()
    db.execute("DELETE FROM room_moderators WHERE room_id = ? AND username = ?", (room_id, username))
    db.commit()


def is_user_banned(room_id, username):
    if not username:
        return False
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM room_bans WHERE room_id = ? AND username = ?", (room_id, username)
    ).fetchone()
    return row is not None


def list_room_bans(room_id):
    db = get_db()
    rows = db.execute(
        "SELECT username, banned_by, banned_at FROM room_bans WHERE room_id = ? ORDER BY username COLLATE NOCASE",
        (room_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def ban_user_from_room(room_id, username, banned_by):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO room_bans (room_id, username, banned_by, banned_at) VALUES (?, ?, ?, ?)",
        (room_id, username, banned_by, datetime.now().isoformat()),
    )
    db.commit()
    # Banned mods lose their moderator status too.
    db.execute("DELETE FROM room_moderators WHERE room_id = ? AND username = ?", (room_id, username))
    db.commit()


def unban_user_from_room(room_id, username):
    db = get_db()
    db.execute("DELETE FROM room_bans WHERE room_id = ? AND username = ?", (room_id, username))
    db.commit()


def room_is_unlocked_for_session(room_row, username):
    """True if the room has no password, the user owns/moderates it, or they've
    already entered the password this session."""
    if not room_row["password_hash"]:
        return True
    if can_moderate_room(room_row, username):
        return True
    return room_row["id"] in session.get("unlocked_rooms", [])


def online_users_with_avatars(room_id):
    """List of {username, avatar} dicts for everyone in a room, sorted by name."""
    names = sorted(room_users.get(room_id, set()))
    return [{"username": u, "avatar": get_user_avatar(u)} for u in names]


def serialize_room(room_dict):
    """room_dict is the output of with_online_count() (a plain dict)."""
    return {
        "id": room_dict["id"],
        "name": room_dict["name"],
        "description": room_dict.get("description", ""),
        "online_count": room_dict.get("online_count", 0),
        "owner": room_dict.get("owner"),
        "has_password": bool(room_dict.get("password_hash")),
        "background_image": (
            url_for("static", filename=room_dict["background_image"])
            if room_dict.get("background_image") else None
        ),
    }


def json_error(message, status=400):
    return {"error": message}, status


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view):
    """
    Like login_required, but for JSON/fetch endpoints. Returns a JSON 401
    instead of a 302 redirect to /login — fetch() follows redirects
    transparently, so a redirect here would hand the caller an HTML login
    page that res.json() then fails to parse.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return {"error": "Your session has expired. Please log in again."}, 401
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

    username = session["username"]
    q = request.args.get("q", "")
    return render_template(
        "index.html",
        featured_rooms=list_featured_rooms(),
        active_rooms=list_active_rooms(),
        custom_rooms=list_custom_rooms(q),
        room_query=q,
        username=username,
        avatar_url=get_user_avatar(username),
        room_error=request.args.get("room_error"),
        banned_room=request.args.get("banned_room"),
        open_create_room=request.args.get("open_create_room"),
    )


@app.route("/r/<int:room_id>")
@login_required
def room(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        abort(404)

    username = session.get("username")

    if is_user_banned(room_id, username) and not is_room_owner(room_row, username):
        return redirect(url_for("index", banned_room=room_row["name"]))

    if not room_is_unlocked_for_session(room_row, username):
        return redirect(url_for("room_join", room_id=room_id))

    history = message_history.get(room_id, [])
    online = online_users_with_avatars(room_id)
    is_owner = is_room_owner(room_row, username)
    is_moderator = can_moderate_room(room_row, username)

    return render_template(
        "room.html",
        room=room_row,
        room_background=(
            url_for("static", filename=room_row["background_image"])
            if room_row["background_image"] else None
        ),
        username=username,
        avatar_url=get_user_avatar(username),
        history=history,
        online=online,
        active_rooms=list_active_rooms(),
        is_owner=is_owner,
        is_moderator=is_moderator,
        moderators=list_room_moderators(room_id) if is_owner else [],
        bans=list_room_bans(room_id) if is_moderator else [],
        settings_error=request.args.get("settings_error"),
        settings_success=request.args.get("settings_success"),
        open_settings=request.args.get("open_settings"),
    )


@app.route("/r/<int:room_id>/join", methods=["GET", "POST"])
@login_required
def room_join(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        abort(404)
    username = session.get("username")

    if is_user_banned(room_id, username) and not is_room_owner(room_row, username):
        return redirect(url_for("index", banned_room=room_row["name"]))

    if room_is_unlocked_for_session(room_row, username):
        return redirect(url_for("room", room_id=room_id))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and check_password_hash(room_row["password_hash"], password):
            unlocked = session.get("unlocked_rooms", [])
            unlocked.append(room_id)
            session["unlocked_rooms"] = unlocked
            return redirect(url_for("room", room_id=room_id))
        error = "Incorrect password."

    return render_template("room_join.html", room=room_row, error=error)


# ==================== Custom room creation ====================

@app.route("/rooms/new", methods=["POST"])
@login_required
def create_room_route():
    username = session["username"]
    name = request.form.get("name", "")
    description = request.form.get("description", "")
    password = request.form.get("password", "")

    background_rel = None
    file = request.files.get("background_image")
    if file and file.filename:
        background_rel, err = save_uploaded_image(
            file, ROOM_BG_DIR, MAX_ROOM_BG_DIMENSION, filename_prefix="room_"
        )
        if err:
            return redirect(url_for("index", room_error=err, open_create_room=1))

    room_row, err = create_custom_room(name, description, password, background_rel, username)
    if err:
        return redirect(url_for("index", room_error=err, open_create_room=1))

    return redirect(url_for("room", room_id=room_row["id"]))


@app.route("/api/rooms/browse")
@api_login_required
def api_rooms_browse():
    q = request.args.get("q", "")
    return {"rooms": [serialize_room(r) for r in list_custom_rooms(q)]}


# ==================== Per-room settings, moderators, and bans ====================

def _room_or_404(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        abort(404)
    return room_row


@app.route("/r/<int:room_id>/settings", methods=["POST"])
@login_required
def room_settings(room_id):
    room_row = _room_or_404(room_id)
    username = session["username"]
    if not is_room_owner(room_row, username):
        abort(403)

    name = request.form.get("name", "").strip()[:ROOM_NAME_MAX_LEN]
    description = request.form.get("description", "").strip()[:280]
    new_password = request.form.get("password", "")
    remove_password = request.form.get("remove_password") == "1"
    remove_background = request.form.get("remove_background") == "1"

    if not name:
        return redirect(url_for("room", room_id=room_id, settings_error="Room name is required.", open_settings=1))

    existing = get_room_by_name(name)
    if existing and existing["id"] != room_id:
        return redirect(url_for("room", room_id=room_id, settings_error="Another room already has that name.", open_settings=1))

    db = get_db()

    background_rel = room_row["background_image"]
    if remove_background:
        if background_rel:
            old_path = os.path.join("static", background_rel)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        background_rel = None

    file = request.files.get("background_image")
    if file and file.filename:
        new_rel, err = save_uploaded_image(file, ROOM_BG_DIR, MAX_ROOM_BG_DIMENSION, filename_prefix="room_")
        if err:
            return redirect(url_for("room", room_id=room_id, settings_error=err, open_settings=1))
        if room_row["background_image"]:
            old_path = os.path.join("static", room_row["background_image"])
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        background_rel = new_rel

    if remove_password:
        password_hash = None
    elif new_password:
        password_hash = generate_password_hash(new_password)
    else:
        password_hash = room_row["password_hash"]

    db.execute(
        "UPDATE rooms SET name = ?, description = ?, password_hash = ?, background_image = ? WHERE id = ?",
        (name, description, password_hash, background_rel, room_id),
    )
    db.commit()

    return redirect(url_for("room", room_id=room_id, settings_success="Room settings updated.", open_settings=1))


@app.route("/r/<int:room_id>/moderators/add", methods=["POST"])
@login_required
def room_moderators_add(room_id):
    room_row = _room_or_404(room_id)
    username = session["username"]
    if not is_room_owner(room_row, username):
        abort(403)

    target = request.form.get("username", "").strip()
    if not target:
        return redirect(url_for("room", room_id=room_id, settings_error="Enter a username.", open_settings=1))
    if not get_user_by_username(target):
        return redirect(url_for("room", room_id=room_id, settings_error=f"No user named '{target}'.", open_settings=1))
    if target == room_row["owner"]:
        return redirect(url_for("room", room_id=room_id, settings_error="The owner is already in charge.", open_settings=1))

    add_room_moderator(room_id, target, username)
    return redirect(url_for("room", room_id=room_id, settings_success=f"{target} is now a moderator.", open_settings=1))


@app.route("/r/<int:room_id>/moderators/remove", methods=["POST"])
@login_required
def room_moderators_remove(room_id):
    room_row = _room_or_404(room_id)
    username = session["username"]
    if not is_room_owner(room_row, username):
        abort(403)

    target = request.form.get("username", "").strip()
    remove_room_moderator(room_id, target)
    return redirect(url_for("room", room_id=room_id, settings_success=f"{target} is no longer a moderator.", open_settings=1))


@app.route("/r/<int:room_id>/ban", methods=["POST"])
@login_required
def room_ban(room_id):
    room_row = _room_or_404(room_id)
    username = session["username"]
    if not can_moderate_room(room_row, username):
        abort(403)

    target = request.form.get("username", "").strip()
    if not target:
        return redirect(url_for("room", room_id=room_id, settings_error="Enter a username.", open_settings=1))
    if target == room_row["owner"]:
        return redirect(url_for("room", room_id=room_id, settings_error="You can't ban the room owner.", open_settings=1))
    if not get_user_by_username(target):
        return redirect(url_for("room", room_id=room_id, settings_error=f"No user named '{target}'.", open_settings=1))

    ban_user_from_room(room_id, target, username)

    # Boot them out immediately if they're currently in the room.
    if target in room_users.get(room_id, set()):
        room_users[room_id].discard(target)
        socketio.emit("system", {"msg": f"{target} was banned from this room.", "type": "leave"}, room=str(room_id))
        socketio.emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))
        socketio.emit("banned", {"username": target}, room=str(room_id))

    return redirect(url_for("room", room_id=room_id, settings_success=f"{target} has been banned.", open_settings=1))


@app.route("/r/<int:room_id>/unban", methods=["POST"])
@login_required
def room_unban(room_id):
    room_row = _room_or_404(room_id)
    username = session["username"]
    if not can_moderate_room(room_row, username):
        abort(403)

    target = request.form.get("username", "").strip()
    unban_user_from_room(room_id, target)
    return redirect(url_for("room", room_id=room_id, settings_success=f"{target} has been unbanned.", open_settings=1))


# ==================== Profile / avatar upload ====================

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    username = session["username"]
    error = None
    success = None

    if request.method == "POST":
        bio = request.form.get("bio")
        file = request.files.get("avatar")
        has_file = bool(file and file.filename)

        if has_file:
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

        if bio is not None and not error:
            update_user_bio(username, bio.strip()[:BIO_MAX_LEN])

        if not error:
            success = "Profile updated."

    return render_template(
        "profile.html",
        username=username,
        avatar_url=get_user_avatar(username),
        bio=get_user_bio(username),
        bio_max_len=BIO_MAX_LEN,
        error=error,
        success=success,
    )


@app.route("/u/<username>")
@login_required
def user_profile(username):
    user = get_user_by_username(username)
    if user is None:
        abort(404)
    return render_template(
        "user_profile.html",
        profile_username=username,
        avatar_url=get_user_avatar(username),
        bio=get_user_bio(username),
        created_at=user.get("created_at"),
        is_admin=username in ADMIN_USERS,
        is_me=(username == session.get("username")),
    )


# ==================== Chat image upload ====================

@app.route("/upload/chat-image", methods=["POST"])
@api_login_required
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


# ==================== JSON API (for the desktop client) ====================
# These sit alongside the existing server-rendered pages and change nothing
# about the web UI. They exist because the desktop client can't parse
# Jinja-rendered HTML the way a browser can.

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if len(username) < USERNAME_MIN_LEN:
        return json_error(f"Username must be at least {USERNAME_MIN_LEN} characters.")
    if not username.replace("_", "").isalnum():
        return json_error("Username can only contain letters, numbers, and underscores.")
    if len(password) < PASSWORD_MIN_LEN:
        return json_error(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    if get_user_by_username(username):
        return json_error("Username already taken.")

    create_user(username, password)
    session["username"] = username
    return {"username": username, "avatar_url": get_user_avatar(username)}


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return json_error("Incorrect username or password.", 401)

    session["username"] = username
    return {"username": username, "avatar_url": get_user_avatar(username), "is_admin": username in ADMIN_USERS}


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("username", None)
    return {"ok": True}


@app.route("/api/me")
@api_login_required
def api_me():
    username = session["username"]
    return {"username": username, "avatar_url": get_user_avatar(username), "is_admin": username in ADMIN_USERS}


@app.route("/api/rooms")
@api_login_required
def api_rooms():
    return {
        "featured": [serialize_room(r) for r in list_featured_rooms()],
        "active": [serialize_room(r) for r in list_active_rooms()],
    }


@app.route("/api/rooms", methods=["POST"])
@api_login_required
def api_create_room():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:ROOM_NAME_MAX_LEN] or "Lobby"
    room_row = get_or_create_room(name)
    return serialize_room(with_online_count(room_row))


@app.route("/api/rooms/<int:room_id>")
@api_login_required
def api_room_detail(room_id):
    room_row = get_room(room_id)
    if room_row is None:
        return json_error("Room not found.", 404)
    return {
        "room": serialize_room(with_online_count(room_row)),
        "history": message_history.get(room_id, []),
        "online": online_users_with_avatars(room_id),
    }


@app.route("/api/profile/avatar", methods=["POST"])
@api_login_required
def api_profile_avatar():
    username = session["username"]
    file = request.files.get("avatar")
    rel_path, err = save_uploaded_image(
        file, AVATAR_DIR, MAX_AVATAR_DIMENSION, filename_prefix=f"{username}_"
    )
    if err:
        return json_error(err)

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

    return {"avatar_url": get_user_avatar(username)}


@app.route("/api/admin/online")
@api_login_required
def api_admin_online():
    if session["username"] not in ADMIN_USERS:
        return json_error("Forbidden.", 403)
    online_users = []
    for rid, users in room_users.items():
        if not users:
            continue
        room_row = get_room(rid)
        room_name = room_row["name"] if room_row else "Unknown"
        for u in sorted(users):
            online_users.append({"username": u, "room_id": rid, "room_name": room_name})
    return {"online_users": online_users, "total_online": len(online_users)}


@app.route("/api/admin/notify", methods=["POST"])
@api_login_required
def api_admin_notify():
    if session["username"] not in ADMIN_USERS:
        return json_error("Forbidden.", 403)
    data = request.get_json(silent=True) or {}
    title = data.get("title") or "Server Notice"
    message = (data.get("message") or "").strip()
    if message:
        socketio.emit("admin_notice", {"title": title, "message": message})
    return {"ok": True}


@app.route("/api/admin/kick", methods=["POST"])
@api_login_required
def api_admin_kick():
    if session["username"] not in ADMIN_USERS:
        return json_error("Forbidden.", 403)
    data = request.get_json(silent=True) or {}
    target_username = data.get("username")
    room_id = data.get("room_id")
    if target_username and room_id is not None:
        room_id = int(room_id)
        if target_username in room_users.get(room_id, set()):
            room_users[room_id].discard(target_username)
            socketio.emit(
                "system",
                {"msg": f"{target_username} was kicked by an admin.", "type": "leave"},
                room=str(room_id),
            )
            socketio.emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))
    return {"ok": True}


# ==================== Admin (web pages) ====================

@app.route("/admin")
@login_required
def admin_dashboard():
    if session.get("username") not in ADMIN_USERS:
        abort(403)

    online_users = []
    for rid, users in room_users.items():
        if not users:
            continue
        room_row = get_room(rid)
        room_name = room_row["name"] if room_row else "Unknown"
        for u in sorted(users):
            online_users.append({"username": u, "room_id": rid, "room_name": room_name})

    return render_template(
        "admin.html",
        username=session["username"],
        online_users=online_users,
        total_online=len(online_users),
    )


@app.route("/admin/notify", methods=["POST"])
@login_required
def admin_notify():
    if session.get("username") not in ADMIN_USERS:
        abort(403)
    title = request.form.get("title", "Server Notice")
    message = request.form.get("message", "").strip()
    if message:
        socketio.emit("admin_notice", {"title": title, "message": message})
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/kick", methods=["POST"])
@login_required
def admin_kick():
    if session.get("username") not in ADMIN_USERS:
        abort(403)
    username = request.form.get("username")
    room_id = request.form.get("room_id")
    if username and room_id:
        room_id = int(room_id)
        if username in room_users.get(room_id, set()):
            room_users[room_id].discard(username)
            socketio.emit(
                "system",
                {"msg": f"{username} was kicked by an admin.", "type": "leave"},
                room=str(room_id),
            )
            socketio.emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))
    return redirect(url_for("admin_dashboard"))


# ==================== Voice Signaling ====================
# WebRTC mesh: every participant opens a direct RTCPeerConnection to every
# other participant in the same room. This server only relays signaling
# messages (SDP offers/answers, ICE candidates) between sids — it never
# touches the audio itself.
#
# Protocol (mirrors what room.html's client-side JS expects):
#   client -> server: voice_join  {room_id}
#   server -> joiner:  voice_peers {peers: [{username, sid}, ...]}   (existing participants)
#   server -> others:  voice_user_joined {username, sid}             (the new joiner)
#   client -> server: voice_offer/voice_answer/voice_ice {room_id, target: <sid>, ...}
#   server -> target:  voice_offer/voice_answer/voice_ice {from: <sid>, username, ...}
#   client -> server: voice_leave {room_id}
#   server -> others:  voice_user_left {username, sid}

# room_id -> {username: sid}
voice_room_users = {}


def _voice_leave(room_id, username, sid):
    users = voice_room_users.get(room_id)
    if not users or users.get(username) != sid:
        return
    users.pop(username, None)
    if not users:
        voice_room_users.pop(room_id, None)
    emit("voice_user_left", {"username": username, "sid": sid}, room=str(room_id), include_self=False)


@socketio.on("voice_join")
def handle_voice_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    room_id = int(room_id)
    sid = request.sid

    # Tell the joiner who's already here so *they* initiate offers to each peer.
    existing = voice_room_users.get(room_id, {})
    emit("voice_peers", {"peers": [{"username": u, "sid": s} for u, s in existing.items()]})

    voice_room_users.setdefault(room_id, {})[username] = sid
    emit("voice_user_joined", {"username": username, "sid": sid}, room=str(room_id), include_self=False)


@socketio.on("voice_leave")
def handle_voice_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if room_id is None or not username:
        return
    _voice_leave(int(room_id), username, request.sid)


@socketio.on("voice_offer")
def handle_voice_offer(data):
    target = data.get("target")
    offer = data.get("offer")
    username = session.get("username")
    if target and offer and username:
        emit("voice_offer", {"from": request.sid, "username": username, "offer": offer}, room=target)


@socketio.on("voice_answer")
def handle_voice_answer(data):
    target = data.get("target")
    answer = data.get("answer")
    username = session.get("username")
    if target and answer and username:
        emit("voice_answer", {"from": request.sid, "username": username, "answer": answer}, room=target)


@socketio.on("voice_ice")
def handle_voice_ice(data):
    target = data.get("target")
    candidate = data.get("candidate")
    if target and candidate:
        emit("voice_ice", {"from": request.sid, "candidate": candidate}, room=target)


# ==================== Standard Chat Events ====================

@socketio.on("join")
def handle_join(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if not room_id or not username:
        return
    room_id = int(room_id)
    room_row = get_room(room_id)
    if room_row is None:
        return
    if is_user_banned(room_id, username) and not is_room_owner(room_row, username):
        emit("banned", {"username": username})
        return
    join_room(str(room_id))
    room_users.setdefault(room_id, set()).add(username)
    emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))


@socketio.on("leave")
def handle_leave(data):
    room_id = data.get("room_id")
    username = session.get("username")
    if not room_id or not username:
        return
    room_id = int(room_id)
    leave_room(str(room_id))
    room_users.get(room_id, set()).discard(username)
    emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))


@socketio.on("disconnect")
def handle_disconnect():
    """Clean up both the chat roster and any voice session when a socket drops."""
    username = session.get("username")
    if not username:
        return
    sid = request.sid

    for room_id, users in list(room_users.items()):
        if username in users:
            users.discard(username)
            emit("roster", {"online": online_users_with_avatars(room_id)}, room=str(room_id))

    for room_id, users in list(voice_room_users.items()):
        if users.get(username) == sid:
            _voice_leave(room_id, username, sid)


@socketio.on("message")
def handle_message(data):
    room_id = data.get("room_id")
    username = session.get("username")
    text = (data.get("message") or "").strip()[:1000]
    image_url = data.get("image_url")

    # Only accept image URLs that point at our own chat-upload directory —
    # never trust an arbitrary client-supplied URL.
    if image_url and not image_url.startswith(url_for("static", filename="uploads/chat/")):
        image_url = None

    if not room_id or not username or (not text and not image_url):
        return
    room_id = int(room_id)
    entry = add_message(room_id, username, text, image_url=image_url)
    emit("message", entry, room=str(room_id))


if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)

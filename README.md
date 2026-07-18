# Chat

A small, no-frills chat website, similar in spirit to y99.in: register
an account, pick (or create) a room, start talking. Chat messages are
in-memory only — nothing is saved to disk, and history is cleared
whenever the server restarts. User accounts, however, are stored in a
local SQLite database (`chat.db`, created automatically on first run)
so you can log back in later.

## Features

- Accounts: register with a username and password
  - Usernames must be more than 4 characters (5+), letters/numbers/underscore only
  - Passwords must be at least 6 characters, hashed with Werkzeug's
    `generate_password_hash` (never stored in plain text)
- Real-time messaging (Flask-SocketIO / WebSockets)
- Multiple chat rooms — type any room name to create/join it, or use one
  of the suggested rooms on the home page
- Live "who's online" list per room
- Join/leave notifications
- Plain, functional UI — no frameworks, no clutter

## Setup

```bash
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open http://localhost:5000 in your browser. Open it in a couple of
tabs (or on a couple of devices on the same network) to see the chat
working in real time.

## Notes / things you may want to change before deploying anywhere public

- `SECRET_KEY` is regenerated on every restart — set a fixed one via an
  environment variable if you want sessions (and logged-in users) to
  survive restarts without re-logging in.
- There's no rate limiting, profanity filtering, abuse prevention, or
  "forgot password" flow — worth adding if you open this up to the
  public.
- Message history is capped at 100 messages per room and lives only in
  memory (a Python dict). Swap in Redis or a database if you need
  persistence or want to run more than one server process.
- `chat.db` (SQLite) holds accounts only — delete it to reset all users.
- For production, run behind a proper WSGI/ASGI server with
  eventlet/gevent (Flask-SocketIO's built-in dev server is fine for
  local use only).

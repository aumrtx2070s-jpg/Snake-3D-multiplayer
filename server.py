"""Snake 3D multiplayer server - pure Python standard library, no pip install.

It does two jobs:

  1. Serves the browser client (the files in ./public) over plain HTTP.
  2. Runs the authoritative game loop for every room and streams the world
     state to the players over a hand-rolled WebSocket (RFC 6455: text
     frames plus ping / pong / close - no third-party library).

Run locally:
    python server.py            then open  http://localhost:8000

On Render (free web service):
    Start Command = python server.py       (PORT is provided by the platform)

The 2D turtle game in the parent folder is untouched and unrelated.
"""

import base64
import hashlib
import json
import os
import random
import secrets
import struct
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------- tuning knobs
GRID_W, GRID_H = 32, 24          # play area, in cells
TICK_HZ = 15                     # game steps per second (snake speed)
START_LEN = 3                    # body length on every (re)spawn
RESPAWN_SEC = 3.0               # dead time before you pop back in
MAX_PLAYERS = 8                 # per room
APPLES_PER_ROOM = 4
ROOM_IDLE_REAP_SEC = 60         # drop a room with no live connection this long
CODE_LEN = 4
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I ambiguity

# A write that doesn't drain within this long marks the connection dead,
# instead of blocking the one shared game-loop thread that broadcasts to
# every room - one stalled player must never freeze everyone else's game.
SEND_TIMEOUT = 1.5

# One stable colour per seat, handed out in order as players join.
PLAYER_COLORS = [
    "#8BC34A", "#42A5F5", "#FFCA28", "#EF5350",
    "#AB47BC", "#26C6DA", "#FF7043", "#EC407A",
]

DIRS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(HERE, "public")
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"     # RFC 6455 handshake GUID


# =====================================================================
#  Game model
# =====================================================================
class Player:
    __slots__ = ("id", "name", "color", "conn", "body", "direction",
                 "moved", "pending", "alive", "respawn_at", "score")

    def __init__(self, pid, name, color):
        self.id = pid
        self.name = name
        self.color = color
        self.conn = None            # WSConn, wired up once the socket joins
        self.body = []              # list of (x, y); head is index 0
        self.direction = (1, 0)     # heading that will be applied next step
        self.moved = (1, 0)         # heading of the last *committed* step
        self.pending = None         # one buffered turn for the next step
        self.alive = False
        self.respawn_at = 0.0
        self.score = 0


class Room:
    def __init__(self, code):
        self.code = code
        self.players = {}           # pid -> Player
        self.apples = []            # list of (x, y)
        self.lock = threading.RLock()
        self.empty_since = None     # wall-clock time the last socket left

    # ------------------------------------------------------ membership
    def _free_color(self):
        used = {p.color for p in self.players.values()}
        for c in PLAYER_COLORS:
            if c not in used:
                return c
        return random.choice(PLAYER_COLORS)

    def add_player(self, name):
        with self.lock:
            if len(self.players) >= MAX_PLAYERS:
                return None
            pid = secrets.token_hex(4)
            p = Player(pid, name, self._free_color())
            self.players[pid] = p
            self._spawn(p)
            self._replenish_apples()
            self.empty_since = None
            return p

    def remove_player(self, pid):
        with self.lock:
            self.players.pop(pid, None)

    # ------------------------------------------------------ helpers
    def _occupied(self):
        cells = set()
        for p in self.players.values():
            cells.update(p.body)
        return cells

    def _free_cell(self):
        occ = self._occupied() | set(self.apples)
        free = [(x, y)
                for x in range(GRID_W)
                for y in range(GRID_H)
                if (x, y) not in occ]
        return random.choice(free) if free else None

    def _replenish_apples(self):
        while len(self.apples) < APPLES_PER_ROOM:
            cell = self._free_cell()
            if cell is None:
                break
            self.apples.append(cell)

    def _spawn(self, p):
        cell = self._free_cell() or (GRID_W // 2, GRID_H // 2)
        x, y = cell
        if x >= START_LEN:                       # lay the tail out to the left
            p.body = [(x - i, y) for i in range(START_LEN)]
            heading = (1, 0)
        else:                                    # ...unless we're near the wall
            p.body = [(x + i, y) for i in range(START_LEN)]
            heading = (-1, 0)
        p.direction = heading
        p.moved = heading
        p.pending = None
        p.alive = True
        p.respawn_at = 0.0

    # ------------------------------------------------------ input
    def set_dir(self, pid, name):
        d = DIRS.get(name)
        if d is None:
            return
        with self.lock:
            p = self.players.get(pid)
            if not p or not p.alive:
                return
            # refuse a 180 into the neck, checked against the *committed* step
            if d[0] == -p.moved[0] and d[1] == -p.moved[1]:
                return
            p.pending = d

    # ------------------------------------------------------ simulation
    def step(self, now):
        with self.lock:
            alive = [p for p in self.players.values() if p.alive]

            # 1. apply the buffered turn, then work out each new head cell
            new_heads = {}
            for p in alive:
                if p.pending is not None:
                    p.direction = p.pending
                    p.pending = None
                hx, hy = p.body[0]
                dx, dy = p.direction
                new_heads[p.id] = (hx + dx, hy + dy)

            # 2. which snakes are about to eat?
            eating = {p.id: new_heads[p.id] in self.apples for p in alive}

            # 3. cells that will still be solid *after* everyone moves
            #    (a non-eating snake's tail cell vacates, so it is not solid)
            blocked = set()
            for p in alive:
                blocked.update(p.body if eating[p.id] else p.body[:-1])

            # 4. deaths: wall, body hit, or two heads into the same cell
            dead = set()
            for pid, (nx, ny) in new_heads.items():
                if nx < 0 or nx >= GRID_W or ny < 0 or ny >= GRID_H:
                    dead.add(pid)
                elif (nx, ny) in blocked:
                    dead.add(pid)
            head_counts = Counter(new_heads.values())
            for pid, cell in new_heads.items():
                if head_counts[cell] > 1:
                    dead.add(pid)

            # 5. commit the survivors, kill the rest
            for p in alive:
                if p.id in dead:
                    p.alive = False
                    p.body = []
                    p.respawn_at = now + RESPAWN_SEC
                    continue
                head = new_heads[p.id]
                p.body.insert(0, head)
                if eating[p.id]:
                    if head in self.apples:
                        self.apples.remove(head)
                    p.score += 1
                else:
                    p.body.pop()
                p.moved = p.direction

            # 6. keep the board stocked and revive anyone whose timer is up
            self._replenish_apples()
            for p in self.players.values():
                if not p.alive and p.respawn_at and now >= p.respawn_at:
                    self._spawn(p)

    def snapshot(self, now):
        with self.lock:
            players = []
            for p in self.players.values():
                players.append({
                    "id": p.id,
                    "name": p.name,
                    "color": p.color,
                    "body": [[x, y] for (x, y) in p.body],
                    "score": p.score,
                    "alive": p.alive,
                    "respawn_in": (0 if p.alive
                                   else max(0.0, round(p.respawn_at - now, 1))),
                })
            return {
                "type": "state",
                "w": GRID_W,
                "h": GRID_H,
                "apples": [[x, y] for (x, y) in self.apples],
                "players": players,
            }


# =====================================================================
#  Room registry + the single shared game loop
# =====================================================================
rooms = {}
rooms_lock = threading.Lock()


def make_room():
    with rooms_lock:
        for _ in range(10000):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if code not in rooms:
                rooms[code] = Room(code)
                return rooms[code]
    raise RuntimeError("ran out of room codes")


def get_room(code):
    if not code:
        return None
    with rooms_lock:
        return rooms.get(code.strip().upper())


def game_loop():
    period = 1.0 / TICK_HZ
    next_tick = time.monotonic()
    while True:
        now = time.time()
        with rooms_lock:
            batch = list(rooms.values())

        for room in batch:
            try:
                room.step(now)
                payload = json.dumps(room.snapshot(now), separators=(",", ":"))
                with room.lock:
                    conns = [p.conn for p in room.players.values() if p.conn]
                live = sum(1 for c in conns if c.send(payload))

                if live == 0:
                    if room.empty_since is None:
                        room.empty_since = now
                    elif now - room.empty_since > ROOM_IDLE_REAP_SEC:
                        with rooms_lock:
                            rooms.pop(room.code, None)
                else:
                    room.empty_since = None
            except Exception as exc:                          # never die here
                sys.stderr.write("room %s: %r\n" % (room.code, exc))

        next_tick += period
        drift = next_tick - time.monotonic()
        if drift > 0:
            time.sleep(drift)
        else:
            next_tick = time.monotonic()                      # we fell behind


# =====================================================================
#  Minimal WebSocket connection (RFC 6455)
# =====================================================================
class WSConn:
    def __init__(self, rfile, wfile, sock):
        self.rfile = rfile
        self.wfile = wfile
        self.open = True
        self._wlock = threading.Lock()
        try:
            sock.settimeout(SEND_TIMEOUT)
        except OSError:
            pass

    # -------------------------------------------------- outbound
    def send(self, text):
        """Send one text frame. Returns False once the peer is gone."""
        if not self.open:
            return False
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])                            # FIN + opcode text
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        try:
            with self._wlock:
                self.wfile.write(bytes(header) + data)
                self.wfile.flush()
            return True
        except (OSError, ValueError):
            self.open = False
            return False

    def _control(self, opcode, payload=b""):
        if not self.open:
            return
        try:
            with self._wlock:
                self.wfile.write(bytes([0x80 | opcode, len(payload)]) + payload)
                self.wfile.flush()
        except (OSError, ValueError):
            self.open = False

    def close(self):
        self._control(0x8)
        self.open = False

    # -------------------------------------------------- inbound
    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.rfile.read(n - len(buf))
            except TimeoutError:
                continue           # nothing arrived within SEND_TIMEOUT - keep waiting,
                                    # this is normal for an idle player, not a dead one
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return buf

    def _one_frame(self):
        b0, b1 = self._exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._exact(8))[0]
        mask = self._exact(4) if masked else b"\x00\x00\x00\x00"
        payload = self._exact(length)
        if masked:
            payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        """Block until the next application message. str, or None on close."""
        while True:
            try:
                fin, opcode, payload = self._one_frame()
            except (ConnectionError, OSError, struct.error):
                self.open = False
                return None

            if opcode == 0x8:                    # close
                self.open = False
                return None
            if opcode == 0x9:                    # ping -> pong
                self._control(0xA, payload[:125])
                continue
            if opcode == 0xA:                    # pong -> ignore
                continue
            if opcode not in (0x0, 0x1, 0x2):   # unknown -> ignore
                continue

            message = payload
            while not fin:                       # stitch continuation frames
                try:
                    fin, _cont_op, more = self._one_frame()
                except (ConnectionError, OSError, struct.error):
                    self.open = False
                    return None
                message += more
            try:
                return message.decode("utf-8")
            except UnicodeDecodeError:
                continue


# =====================================================================
#  One player's socket session
# =====================================================================
def clean_name(name):
    if not isinstance(name, str):
        return "Player"
    name = "".join(ch for ch in name.strip() if ch.isprintable())[:16]
    return name or "Player"


class Session:
    def __init__(self, conn):
        self.conn = conn
        self.room = None
        self.player = None

    def _send(self, obj):
        self.conn.send(json.dumps(obj, separators=(",", ":")))

    def run(self):
        while True:
            raw = self.conn.recv()
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue

            kind = msg.get("type")
            if kind == "create":
                self._join(make_room(), msg.get("name"))
            elif kind == "join":
                room = get_room(msg.get("code"))
                if room is None:
                    self._send({"type": "error", "msg": "ไม่พบห้องนี้"})
                else:
                    self._join(room, msg.get("name"))
            elif kind == "dir":
                if self.room and self.player:
                    self.room.set_dir(self.player.id, msg.get("dir"))
            elif kind == "leave":
                self.cleanup()

    def _join(self, room, name):
        if self.room is not None:
            self.cleanup()
        player = room.add_player(clean_name(name))
        if player is None:
            self._send({"type": "error", "msg": "ห้องเต็มแล้ว"})
            return
        player.conn = self.conn
        self.room = room
        self.player = player
        self._send({
            "type": "joined",
            "code": room.code,
            "id": player.id,
            "color": player.color,
            "w": GRID_W,
            "h": GRID_H,
            "tick_ms": round(1000 / TICK_HZ),
        })

    def cleanup(self):
        if self.room and self.player:
            self.room.remove_player(self.player.id)
        self.room = None
        self.player = None


# =====================================================================
#  HTTP: static files + the /ws upgrade
# =====================================================================
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "snake3d"

    def log_message(self, *_a):
        pass                                     # keep the platform logs quiet

    # -------------------------------------------------- routing
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws()
            return
        if path in ("/healthz", "/health"):
            self._text(200, "ok")
            return
        self._serve_static(path)

    def do_HEAD(self):
        self._serve_static(self.path.split("?", 1)[0], head_only=True)

    # -------------------------------------------------- helpers
    def _text(self, code, body):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _serve_static(self, path, head_only=False):
        if path in ("/", ""):
            path = "/index.html"
        rel = os.path.normpath(path).lstrip("\\/")
        full = os.path.join(PUBLIC_DIR, rel)
        if (not os.path.abspath(full).startswith(os.path.abspath(PUBLIC_DIR))
                or not os.path.isfile(full)):
            self._text(404, "not found")
            return
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                                  "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only and self.command != "HEAD":
            self.wfile.write(body)

    def _handle_ws(self):
        self.close_connection = True              # we own the socket from here
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._text(400, "bad websocket request")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        conn = WSConn(self.rfile, self.wfile, self.connection)
        session = Session(conn)
        try:
            session.run()
        except Exception as exc:
            sys.stderr.write("ws session: %r\n" % (exc,))
        finally:
            session.cleanup()
            conn.close()


def main():
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=game_loop, name="game-loop", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("snake3d on http://0.0.0.0:%d   (open http://localhost:%d)"
          % (port, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

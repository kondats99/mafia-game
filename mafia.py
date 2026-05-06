#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║                    M A F I A                             ║
║         Terminal Social Deduction Game                   ║
╚══════════════════════════════════════════════════════════╝

HOW TO PLAY
───────────
HOST:   python mafia.py --host
        Share your IP + port with friends.
        For cross-internet play, use ngrok (free):
            ngrok tcp 55000
            Share the address shown (e.g. 0.tcp.ngrok.io:12345)

JOIN:   python mafia.py --join --server 192.168.1.5
   or   python mafia.py --join --server 0.tcp.ngrok.io --port 12345

REQUIREMENTS
────────────
  Python 3.7+
  Windows:  pip install windows-curses
  Mac/Linux: curses is built-in

CHAT LAYOUT (single screen, no tabs)
─────────────────────────────────────
  DAY   — all alive players chat freely
  NIGHT — town is silent; mafia chat appears in red (mafia only)
  DEAD  — ghost messages flow inline with live chat, grey + dimmed
          format: "Name (Role): message"
          visible only to other dead players and spectators
          dead players can also read the live town chat in real time
"""

import curses
import socket
import threading
import json
import sys
import time
import random
import argparse
import textwrap
from collections import defaultdict

# ── Constants ──────────────────────────────────────────────────────────────
DEFAULT_PORT = 55000

# Colour pair IDs
C_WHITE  = 1
C_GREEN  = 2
C_RED    = 3
C_YELLOW = 4
C_BLUE   = 5
C_GREY   = 6
C_GOLD   = 7
C_HEADER = 8
C_INPUT  = 9
C_DIM    = 10

# Roles
ROLE_VILLAGER = "Villager"
ROLE_MAFIA    = "Mafia"
ROLE_SHERIFF  = "Sheriff"
ROLE_DOCTOR   = "Doctor"

# Phases
PHASE_LOBBY = "lobby"
PHASE_DAY   = "day"
PHASE_NIGHT = "night"
PHASE_OVER  = "over"

# Chat channels
CH_TOWN  = "town"
CH_MAFIA = "mafia"
CH_GHOST = "ghost"
CH_SYS   = "system"


# ══════════════════════════════════════════════════════════════════════════
# GAME STATE
# ══════════════════════════════════════════════════════════════════════════
class GameState:
    def __init__(self):
        self.lock               = threading.Lock()
        self.phase              = PHASE_LOBBY
        self.day                = 0
        self.players            = {}   # pid -> {pid, name, role, alive}
        self.settings           = {"sheriff": True, "doctor": True}
        self.host_pid           = None
        self.messages           = []
        self.votes              = {}   # pid -> target_pid (can be None for skip)
        self.night_acts         = {}   # pid -> {type, target}
        self.mafia_pids         = []
        self.winner             = None
        self.last_killed        = None
        self.last_saved         = False
        self.sheriff_results    = {}   # sheriff_pid -> {target_pid: bool}
        self.doc_last_protected = None
        self.night_processed    = False  # Prevent double night resolution

    def to_dict(self, viewer_pid=None):
        viewer       = self.players.get(viewer_pid, {})
        viewer_role  = viewer.get("role")
        viewer_alive = viewer.get("alive", True)

        players_out = {}
        for pid, p in self.players.items():
            players_out[pid] = {
                "pid":   pid,
                "name":  p["name"],
                "alive": p["alive"],
                "role":  self._visible_role(pid, viewer_pid, viewer_role, viewer_alive),
            }

        sheriff_log = {}
        if viewer_pid and viewer_pid in self.sheriff_results:
            sheriff_log = self.sheriff_results[viewer_pid]

        return {
            "phase":         self.phase,
            "day":           self.day,
            "players":       players_out,
            "settings":      self.settings,
            "host_pid":      self.host_pid,
            "messages":      self._visible_messages(viewer_role, viewer_alive)[-300:],
            "votes":         self.votes,
            "night_acts":    self._visible_night_acts(viewer_pid),
            "mafia_pids":    self.mafia_pids if (viewer_role == ROLE_MAFIA or not viewer_alive) else [],
            "winner":        self.winner,
            "last_killed":   self.last_killed,
            "last_saved":    self.last_saved,
            "sheriff_log":   sheriff_log,
            "doc_last_prot": self.doc_last_protected,
        }

    def _visible_role(self, pid, viewer_pid, viewer_role, viewer_alive):
        p = self.players[pid]
        if pid == viewer_pid:
            return p["role"]                               # always see own role
        if not viewer_alive:
            return p["role"]                               # dead see all roles
        if viewer_role == ROLE_MAFIA and pid in self.mafia_pids:
            return p["role"]                               # mafia see allies
        return "Unknown"

    def _visible_night_acts(self, viewer_pid):
        """Mafia members see all mafia kill votes; others only see their own action."""
        viewer = self.players.get(viewer_pid, {})
        viewer_role  = viewer.get("role")
        viewer_alive = viewer.get("alive", True)
        out = {}
        if viewer_role == ROLE_MAFIA:
            # Show all kill votes from alive mafia to each other
            for pid in self.mafia_pids:
                if pid in self.night_acts:
                    out[pid] = self.night_acts[pid]
        elif viewer_pid and viewer_pid in self.night_acts:
            out[viewer_pid] = self.night_acts[viewer_pid]
        return out

    def _visible_messages(self, viewer_role, viewer_alive):
        """
        Channel rules:
          system → everyone
          town   → everyone (dead may read but not write)
          mafia  → mafia members (alive) + all dead players (ghosts spectate everything)
          ghost  → dead only
        """
        out = []
        for m in self.messages:
            ch = m.get("channel", CH_TOWN)
            if ch == CH_SYS:
                out.append(m)
            elif ch == CH_TOWN:
                out.append(m)
            elif ch == CH_MAFIA:
                # Alive mafia see it; dead players see everything as spectators
                if viewer_role == ROLE_MAFIA or not viewer_alive:
                    out.append(m)
            elif ch == CH_GHOST:
                if not viewer_alive:
                    out.append(m)
        return out


# ══════════════════════════════════════════════════════════════════════════
# SERVER
# ══════════════════════════════════════════════════════════════════════════
class MafiaServer:
    def __init__(self, port=DEFAULT_PORT):
        self.port    = port
        self.gs      = GameState()
        self.clients = {}
        self.running = True

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(12)
        srv.settimeout(1.0)
        while self.running:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
        srv.close()

    def _handle(self, conn):
        pid = None
        buf = ""
        try:
            while self.running:
                data = conn.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    break
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = self._process(msg, conn, pid)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            if pid:
                self._disconnect(pid)
            try:
                conn.close()
            except Exception:
                pass

    def _process(self, msg, conn, pid):
        t = msg.get("type")

        if t == "join":
            pid  = msg["pid"]
            name = msg["name"][:20]
            with self.gs.lock:
                if self.gs.phase != PHASE_LOBBY:
                    self._send(conn, {"type": "error", "text": "Game already started."})
                    return pid
                if len(self.gs.players) >= 12:
                    self._send(conn, {"type": "error", "text": "Session full (max 12)."})
                    return pid
                is_host = len(self.gs.players) == 0
                self.gs.players[pid] = {"pid": pid, "name": name, "role": None, "alive": True}
                if is_host:
                    self.gs.host_pid = pid
                self.clients[pid] = conn
            self._sys("  " + name + " joined." + (" [HOST]" if is_host else ""))
            self._broadcast_state()
            return pid

        if t == "settings" and pid == self.gs.host_pid:
            with self.gs.lock:
                self.gs.settings["sheriff"] = bool(msg.get("sheriff", True))
                self.gs.settings["doctor"]  = bool(msg.get("doctor",  True))
            self._broadcast_state()
            return pid

        if t == "start" and pid == self.gs.host_pid:
            with self.gs.lock:
                if len(self.gs.players) < 4:
                    self._send(conn, {"type": "error", "text": "Need at least 4 players."})
                    return pid
                self._assign_roles()
                self.gs.phase = PHASE_DAY
                self.gs.day   = 1
            self._sys("=" * 52)
            self._sys("  The game begins. It is Day 1.")
            self._sys("  Discuss and vote to eliminate a suspect.")
            self._sys("=" * 52)
            self._broadcast_state()
            return pid

        if t == "chat":
            channel = msg.get("channel", CH_TOWN)
            text    = msg.get("text", "").strip()[:300]
            if not text:
                return pid
            with self.gs.lock:
                player = self.gs.players.get(pid)
                if not player:
                    return pid
                alive = player["alive"]
                role  = player["role"]

                if channel == CH_GHOST:
                    if alive:
                        return pid
                    self.gs.messages.append({
                        "text": text, "author": player["name"],
                        "author_pid": pid, "author_role": role,
                        "channel": CH_GHOST, "ts": time.time(),
                    })
                    self._broadcast_state()
                    return pid

                if not alive:
                    return pid

                if channel == CH_TOWN:
                    if self.gs.phase == PHASE_NIGHT:
                        return pid
                    self.gs.messages.append({
                        "text": text, "author": player["name"],
                        "author_pid": pid, "author_role": None,
                        "channel": CH_TOWN, "ts": time.time(),
                    })

                elif channel == CH_MAFIA:
                    if role != ROLE_MAFIA or self.gs.phase != PHASE_NIGHT:
                        return pid
                    self.gs.messages.append({
                        "text": text, "author": player["name"],
                        "author_pid": pid, "author_role": ROLE_MAFIA,
                        "channel": CH_MAFIA, "ts": time.time(),
                    })

            self._broadcast_state()
            return pid

        if t == "vote" and self.gs.phase == PHASE_DAY:
            target = msg.get("target")
            with self.gs.lock:
                voter = self.gs.players.get(pid)
                if not voter or not voter["alive"]:
                    return pid
                # Allow None for skip vote, or a valid alive player
                if target is None:
                    self.gs.votes[pid] = None
                elif target in self.gs.players and self.gs.players[target]["alive"]:
                    self.gs.votes[pid] = target
                else:
                    return pid
            self._broadcast_state()
            self._check_day_resolution()
            return pid

        if t == "night_action" and self.gs.phase == PHASE_NIGHT:
            target = msg.get("target")
            action = msg.get("action")
            with self.gs.lock:
                actor = self.gs.players.get(pid)
                if not actor or not actor["alive"]:
                    return pid
                role  = actor["role"]
                valid = {ROLE_MAFIA: "kill", ROLE_SHERIFF: "investigate", ROLE_DOCTOR: "protect"}
                if action != valid.get(role):
                    return pid
                # Sheriff cannot change their investigation once submitted
                if role == ROLE_SHERIFF and pid in self.gs.night_acts:
                    self._send(conn, {"type": "error",
                        "text": "You have already investigated someone tonight."})
                    return pid
                if target and target in self.gs.players and self.gs.players[target]["alive"]:
                    if role == ROLE_DOCTOR and target == self.gs.doc_last_protected:
                        self._send(conn, {"type": "error",
                            "text": "Can't protect the same person two nights in a row."})
                        return pid
                    self.gs.night_acts[pid] = {"type": action, "target": target}
                elif target is None:
                    # Sheriff can't un-investigate; mafia/doctor can change their mind
                    if role == ROLE_SHERIFF:
                        return pid
                    self.gs.night_acts.pop(pid, None)
            self._broadcast_state()
            self._check_night_resolution()
            return pid

        return pid

    def _assign_roles(self):
        pids    = list(self.gs.players.keys())
        random.shuffle(pids)
        n       = len(pids)
        mafia_n = 1 if n <= 5 else (2 if n <= 8 else 3)
        i = 0
        self.gs.mafia_pids = []
        for _ in range(mafia_n):
            self.gs.players[pids[i]]["role"] = ROLE_MAFIA
            self.gs.mafia_pids.append(pids[i])
            i += 1
        if self.gs.settings["sheriff"] and i < n:
            self.gs.players[pids[i]]["role"] = ROLE_SHERIFF; i += 1
        if self.gs.settings["doctor"] and i < n:
            self.gs.players[pids[i]]["role"] = ROLE_DOCTOR; i += 1
        while i < n:
            self.gs.players[pids[i]]["role"] = ROLE_VILLAGER; i += 1

    def _check_day_resolution(self):
        with self.gs.lock:
            if self.gs.phase != PHASE_DAY:
                return
            alive  = [p for p in self.gs.players.values() if p["alive"]]
            voters = [p["pid"] for p in alive]
            # Check if all alive players have voted (including skip votes represented as None)
            if any(v not in self.gs.votes for v in voters):
                return

            tally = defaultdict(int)
            for v in voters:
                target = self.gs.votes[v]
                if target is not None:
                    tally[target] += 1

            if not tally:
                # Everyone skipped — no elimination
                self._sys("-" * 52)
                self._sys("  VOTE RESULT: No votes cast. Nobody is eliminated.")
                self._sys("  Night " + str(self.gs.day + 1) + " falls.")
                self._move_to_night()
                return

            max_v  = max(tally.values())
            top    = [p for p, c in tally.items() if c == max_v]
            elim   = random.choice(top)
            victim = self.gs.players[elim]
            victim["alive"]     = False

        self._sys("-" * 52)
        self._sys("  VOTE RESULT: " + victim["name"] + " has been eliminated.")
        self._sys("  Their role: " + victim["role"] + ".")
        self._sys("  Night " + str(self.gs.day + 1) + " falls. The town goes silent.")
        self._sys("-" * 52)

        with self.gs.lock:
            if not self._check_win():
                self._move_to_night()
            else:
                self._broadcast_state()

    def _move_to_night(self):
        """Transition from day to night, resetting night state."""
        with self.gs.lock:
            self.gs.phase = PHASE_NIGHT
            self.gs.day += 1
            self.gs.votes = {}
            self.gs.night_acts = {}
            self.gs.night_processed = False
            self.gs.last_killed = None
            self.gs.last_saved = False
            # Sheriff results persist; doc protection history persists
        self._broadcast_state()

    def _check_night_resolution(self):
        with self.gs.lock:
            if self.gs.phase != PHASE_NIGHT:
                return
            # Prevent double resolution
            if self.gs.night_processed:
                return

            alive       = {p["pid"]: p for p in self.gs.players.values() if p["alive"]}
            mafia_alive = [pid for pid in self.gs.mafia_pids if pid in alive]
            sheriff_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_SHERIFF and self.gs.settings["sheriff"]),
                None
            )
            doctor_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_DOCTOR and self.gs.settings["doctor"]),
                None
            )

            # Determine which roles have pending actions
            # Mafia: all alive mafia must have submitted a kill (or be waiting)
            mafia_ready = True
            if mafia_alive:
                mafia_ready = all(pid in self.gs.night_acts for pid in mafia_alive)

            # Sheriff: if exists and hasn't acted, they're not ready
            sheriff_ready = True
            if sheriff_pid:
                sheriff_ready = sheriff_pid in self.gs.night_acts

            # Doctor: if exists and hasn't acted, they're not ready
            doctor_ready = True
            if doctor_pid:
                doctor_ready = doctor_pid in self.gs.night_acts

            # If any role with required action hasn't submitted, wait
            if not (mafia_ready and sheriff_ready and doctor_ready):
                return

            # All actions are in — process night, but don't mark processed yet
            # so we can compute everything in one go
            self.gs.night_processed = True

            # ── Process sheriff investigation (if submitted) ──
            if sheriff_pid:
                act = self.gs.night_acts.get(sheriff_pid)
                if act and act["type"] == "investigate":
                    t    = act["target"]
                    is_m = t in self.gs.mafia_pids
                    self.gs.sheriff_results.setdefault(sheriff_pid, {})[t] = is_m

            # ── Process doctor protection (if submitted) ──
            protected = None
            if doctor_pid:
                act = self.gs.night_acts.get(doctor_pid)
                if act and act["type"] == "protect":
                    protected = act["target"]
                    self.gs.doc_last_protected = protected

            # ── Resolve mafia kill ──
            kill_tally = defaultdict(int)
            for pid in mafia_alive:
                act = self.gs.night_acts.get(pid)
                if act and act["type"] == "kill":
                    kill_tally[act["target"]] += 1
            kill_target = None
            if kill_tally:
                max_k = max(kill_tally.values())
                kill_target = random.choice([t for t, c in kill_tally.items() if c == max_k])

            saved = (kill_target is not None and kill_target == protected)

            killed_pid = None
            if kill_target and not saved:
                self.gs.players[kill_target]["alive"] = False
                killed_pid = kill_target

            self.gs.last_killed = killed_pid
            self.gs.last_saved  = saved
            self.gs.phase       = PHASE_DAY
            self.gs.night_acts  = {}
            self.gs.votes       = {}

        # Outside lock for system messages
        self._sys("-" * 52)
        if saved:
            self._sys("  MORNING: The Doctor saved someone -- no one died tonight.")
        elif killed_pid:
            v = self.gs.players[killed_pid]
            self._sys("  MORNING: " + v["name"] + " was found dead. The Mafia struck.")
            self._sys("  Their role: " + v["role"] + ".")
        else:
            self._sys("  MORNING: A quiet night -- no one was killed.")
        self._sys("  Day " + str(self.gs.day) + " begins. Discuss and vote.")
        self._sys("-" * 52)

        with self.gs.lock:
            if not self._check_win():
                self._broadcast_state()

    def _check_win(self):
        alive         = [p for p in self.gs.players.values() if p["alive"]]
        mafia_alive   = [p for p in alive if p["pid"] in self.gs.mafia_pids]
        village_alive = [p for p in alive if p["pid"] not in self.gs.mafia_pids]

        if not mafia_alive:
            self.gs.phase  = PHASE_OVER
            self.gs.winner = "village"
            self._sys("=" * 52)
            self._sys("  VILLAGE WINS! All Mafia eliminated.")
            self._reveal_all()
            self._sys("=" * 52)
            self._broadcast_state()
            return True
        if len(mafia_alive) >= len(village_alive):
            self.gs.phase  = PHASE_OVER
            self.gs.winner = "mafia"
            self._sys("=" * 52)
            self._sys("  MAFIA WINS! The killers control the town.")
            self._reveal_all()
            self._sys("=" * 52)
            self._broadcast_state()
            return True
        return False

    def _reveal_all(self):
        self._sys("  -- Final Role Reveal --")
        for p in sorted(self.gs.players.values(), key=lambda x: x["name"]):
            status = "alive" if p["alive"] else "dead"
            self._sys("    " + p["name"].ljust(20) + p["role"].ljust(12) + "[" + status + "]")

    def _sys(self, text):
        with self.gs.lock:
            self.gs.messages.append({
                "text": text, "author": "SYSTEM", "author_pid": None,
                "author_role": None, "channel": CH_SYS, "ts": time.time(),
            })

    def _send(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except Exception:
            pass

    def _broadcast_state(self):
        dead = []
        for pid, conn in list(self.clients.items()):
            snap = self.gs.to_dict(viewer_pid=pid)
            snap["type"] = "state"
            try:
                conn.sendall((json.dumps(snap) + "\n").encode())
            except Exception:
                dead.append(pid)
        for pid in dead:
            self._disconnect(pid)

    def _disconnect(self, pid):
        name = self.gs.players.get(pid, {}).get("name", "?")
        with self.gs.lock:
            self.clients.pop(pid, None)
            if self.gs.phase == PHASE_LOBBY:
                # In lobby just remove them entirely
                self.gs.players.pop(pid, None)
                if self.gs.host_pid == pid and self.gs.players:
                    self.gs.host_pid = next(iter(self.gs.players))
            else:
                # Mid-game: mark dead so win conditions can be evaluated
                if pid in self.gs.players:
                    self.gs.players[pid]["alive"] = False
                # Clean up any pending votes/actions from this player
                self.gs.votes.pop(pid, None)
                self.gs.night_acts.pop(pid, None)
                # Also remove any votes cast FOR this player
                self.gs.votes = {k: v for k, v in self.gs.votes.items() if v != pid}

        self._sys("  " + name + " disconnected and has been removed from the game.")

        if self.gs.phase not in (PHASE_LOBBY, PHASE_OVER):
            # Re-check night resolution in case we were waiting on this player
            if self.gs.phase == PHASE_NIGHT:
                self._check_night_resolution()
            # Check win — mafia quitting mid-game should end it
            if not self._check_win():
                self._broadcast_state()
        else:
            self._broadcast_state()


# ══════════════════════════════════════════════════════════════════════════
# CLIENT
# ══════════════════════════════════════════════════════════════════════════
class MafiaClient:
    def __init__(self, host, port, name):
        self.host       = host
        self.port       = port
        self.name       = name
        self.pid        = name[:8] + "_" + str(random.randint(1000, 9999))
        self.state      = {}
        self.buf        = ""
        self.conn       = None
        self.running    = True
        self.input_str  = ""
        self.msg_offset = 0
        self.error_msg  = ""
        self.error_ts   = 0

    def connect(self):
        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn.connect((self.host, self.port))
        self._send({"type": "join", "pid": self.pid, "name": self.name})
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        while self.running:
            try:
                data = self.conn.recv(8192).decode("utf-8", errors="replace")
                if not data:
                    break
                self.buf += data
                while "\n" in self.buf:
                    line, self.buf = self.buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "state":
                        self.state = obj
                    elif obj.get("type") == "error":
                        self.error_msg = obj.get("text", "Error")
                        self.error_ts  = time.time()
            except Exception:
                break
        self.running = False

    def _send(self, obj):
        try:
            self.conn.sendall((json.dumps(obj) + "\n").encode())
        except Exception:
            pass

    # ── Curses entry ───────────────────────────────────────────────────────
    def run(self):
        curses.wrapper(self._curses_main)

    def _curses_main(self, stdscr):
        self._init_colors()
        curses.cbreak()
        stdscr.keypad(True)
        stdscr.nodelay(True)
        curses.curs_set(0)
        while self.running:
            self._draw(stdscr)
            self._handle_input(stdscr)
            time.sleep(0.05)
        stdscr.nodelay(False)
        stdscr.clear()
        self._put(stdscr, 0, 0, "Disconnected. Press any key to exit.",
                  curses.color_pair(C_GOLD))
        stdscr.refresh()
        stdscr.getch()

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_WHITE,  curses.COLOR_WHITE,  -1)
        curses.init_pair(C_GREEN,  curses.COLOR_GREEN,  -1)
        curses.init_pair(C_RED,    curses.COLOR_RED,    -1)
        # Use YELLOW but we'll pair with A_BOLD for #F1C40F style gold
        curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_BLUE,   curses.COLOR_CYAN,   -1)
        curses.init_pair(C_GREY,   8,                   -1)
        # Gold color: yellow + bold gives a richer gold (#F1C40F style)
        curses.init_pair(C_GOLD,   curses.COLOR_YELLOW, -1)
        curses.init_pair(C_HEADER, curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(C_INPUT,  curses.COLOR_WHITE,  -1)
        curses.init_pair(C_DIM,    8,                   -1)

    # ══════════════════════════════════════════════════════════════════════
    # MAIN DRAW
    # ══════════════════════════════════════════════════════════════════════
    def _draw(self, stdscr):
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        s = self.state

        if not s:
            self._put(stdscr, 0, 0, "Connecting...", curses.color_pair(C_GOLD))
            stdscr.refresh()
            return

        phase    = s.get("phase", PHASE_LOBBY)
        players  = s.get("players", {})
        me       = players.get(self.pid, {})
        my_role  = me.get("role") or "Unknown"
        is_alive = me.get("alive", True)
        is_host  = s.get("host_pid") == self.pid

        # Layout constants — defined once, passed everywhere needed
        PWIDTH = 24          # left panel width (player list)
        CWIDTH = max(W - PWIDTH - 1, 20)  # chat width

        self._draw_header(stdscr, W, phase, s, my_role, is_alive)
        self._draw_players(stdscr, H, W, PWIDTH, players, s, my_role, is_alive)
        self._draw_divider(stdscr, H, PWIDTH)
        self._draw_chat(stdscr, H, W, PWIDTH, CWIDTH, s, my_role, phase, is_alive)
        self._draw_input(stdscr, H, W, PWIDTH, CWIDTH, phase, is_alive, my_role, is_host, s)

        if phase == PHASE_LOBBY:
            self._draw_lobby_hint(stdscr, H, W, PWIDTH, is_host, s)

        stdscr.refresh()

    # ── Header bar ─────────────────────────────────────────────────────────
    def _draw_header(self, stdscr, W, phase, s, my_role, is_alive):
        phase_lbl = {
            PHASE_LOBBY: "LOBBY",
            PHASE_DAY:   "DAY "   + str(s.get("day", 0)),
            PHASE_NIGHT: "NIGHT " + str(s.get("day", 0)),
            PHASE_OVER:  "GAME OVER",
        }.get(phase, phase.upper())
        dead_tag = "  [DEAD]" if not is_alive else ""
        title = " MAFIA  |  " + phase_lbl + "  |  " + self.name + " (" + my_role + ")" + dead_tag + " "
        self._put(stdscr, 0, 0, title[:W - 1].ljust(W - 1),
                  curses.color_pair(C_HEADER) | curses.A_BOLD)

    # ── Player list (left panel) ───────────────────────────────────────────
    def _draw_players(self, stdscr, H, W, PWIDTH, players, s, my_role, is_alive):
        row = 2
        self._put(stdscr, row, 1, "PLAYERS", curses.color_pair(C_GOLD) | curses.A_BOLD)
        row += 1
        self._put(stdscr, row, 1, "-" * (PWIDTH - 2), curses.color_pair(C_DIM))
        row += 1

        phase       = s.get("phase")
        mafia_pids  = s.get("mafia_pids", [])
        sheriff_log = s.get("sheriff_log", {})

        for pid, p in sorted(players.items(), key=lambda x: x[1]["name"]):
            if row >= H - 6:
                break
            is_me = pid == self.pid
            alive = p.get("alive", True)
            name  = p["name"][: PWIDTH - 4]

            if not alive:
                if is_alive:
                    # Alive viewer: dead players shown grey
                    name_attr = curses.color_pair(C_GREY) | curses.A_DIM
                else:
                    # Dead viewer: see true role colours for everyone
                    col, extra = self._role_colour_by_true_role(p.get("role"))
                    name_attr  = curses.color_pair(col) | extra | curses.A_DIM
            else:
                col, extra = self._player_colour(pid, p, my_role, mafia_pids, sheriff_log, is_me)
                name_attr  = curses.color_pair(col) | extra | (curses.A_BOLD if is_me else 0)

            marker = ">" if is_me else " "
            suffix = " X" if not alive else (" <" if is_me else "")
            self._put(stdscr, row, 0, (" " + marker + name + suffix)[: PWIDTH], name_attr)
            row += 1

            # Role tag on a second line (indented)
            role_tag = self._role_tag(pid, p, my_role, mafia_pids, sheriff_log, is_me, is_alive)
            if role_tag and row < H - 6:
                if not is_alive:
                    # Dead viewer: colour the role tag by true role
                    tag_col, tag_extra = self._role_colour_by_true_role(p.get("role"))
                    tag_attr = curses.color_pair(tag_col) | tag_extra | curses.A_DIM
                else:
                    tag_attr = curses.color_pair(C_DIM)
                self._put(stdscr, row, 3, role_tag[: PWIDTH - 4], tag_attr)
                row += 1

        # Vote tally (day only)
        if phase == PHASE_DAY:
            votes = s.get("votes", {})
            tally = defaultdict(int)
            for v in votes.values():
                if v is not None:
                    tally[v] += 1
            if tally and row < H - 6:
                row += 1
                self._put(stdscr, row, 1, "VOTES", curses.color_pair(C_GOLD) | curses.A_BOLD)
                row += 1
                for tpid, cnt in sorted(tally.items(), key=lambda x: -x[1]):
                    if row >= H - 6:
                        break
                    tname = players.get(tpid, {}).get("name", "?")[: PWIDTH - 5]
                    self._put(stdscr, row, 1, tname + ": " + str(cnt),
                              curses.color_pair(C_WHITE))
                    row += 1

        # Night action status + kill vote tally for mafia
        if phase == PHASE_NIGHT and is_alive and my_role in (ROLE_MAFIA, ROLE_SHERIFF, ROLE_DOCTOR):
            night_acts = s.get("night_acts", {})
            my_act     = night_acts.get(self.pid)
            if row < H - 6:
                row += 1
                status = "Action set" if my_act else "Pending..."
                col    = C_GREEN if my_act else C_GOLD
                self._put(stdscr, row, 1, status[: PWIDTH - 2], curses.color_pair(col))
                row += 1

            # Mafia: show kill vote tally like daytime votes
            if my_role == ROLE_MAFIA and night_acts and row < H - 6:
                self._put(stdscr, row, 1, "KILL VOTES", curses.color_pair(C_RED) | curses.A_BOLD)
                row += 1
                kill_tally = defaultdict(int)
                for act in night_acts.values():
                    if act.get("type") == "kill":
                        kill_tally[act["target"]] += 1
                for tpid, cnt in sorted(kill_tally.items(), key=lambda x: -x[1]):
                    if row >= H - 6:
                        break
                    tname = players.get(tpid, {}).get("name", "?")[: PWIDTH - 5]
                    self._put(stdscr, row, 1, tname + ": " + str(cnt),
                              curses.color_pair(C_WHITE))
                    row += 1

    def _player_colour(self, pid, p, my_role, mafia_pids, sheriff_log, is_me):
        """
        Returns (colour_pair_id, extra_attr) per role POV:
          Mafia   — self + allies = RED,        others = GREEN
          Sheriff — self = GOLD (YELLOW + BOLD), confirmed mafia = RED, others = GREEN
          Doctor  — self = BLUE(cyan),          others = GREEN
          Villager— everyone GREEN
        """
        if my_role == ROLE_MAFIA:
            return (C_RED if pid in mafia_pids else C_GREEN, 0)
        if my_role == ROLE_SHERIFF:
            if is_me:
                return (C_GOLD, curses.A_BOLD)   # Gold style for sheriff
            if pid in sheriff_log and sheriff_log[pid]:
                return (C_RED, 0)
            return (C_GREEN, 0)
        if my_role == ROLE_DOCTOR:
            return (C_BLUE if is_me else C_GREEN, 0)
        return (C_GREEN, 0)

    def _role_colour_by_true_role(self, role):
        """Colour pair for a player whose true role is known (used by dead viewers)."""
        if role == ROLE_MAFIA:
            return (C_RED, 0)
        if role == ROLE_SHERIFF:
            return (C_GOLD, curses.A_BOLD)  # Gold style for sheriff
        if role == ROLE_DOCTOR:
            return (C_BLUE, 0)
        return (C_GREEN, 0)   # Villager / Unknown

    def _role_tag(self, pid, p, my_role, mafia_pids, sheriff_log, is_me, viewer_alive):
        """Small label shown beneath a player's name."""
        if not viewer_alive:
            return "[" + (p.get("role") or "?") + "]"   # dead see all roles
        if is_me:
            return "[" + (p.get("role") or "?") + "]"
        if my_role == ROLE_MAFIA and pid in mafia_pids:
            return "[Mafia]"
        if my_role == ROLE_SHERIFF and pid in sheriff_log:
            return "[Mafia!]" if sheriff_log[pid] else "[Clean]"
        return ""

    # ── Divider ────────────────────────────────────────────────────────────
    def _draw_divider(self, stdscr, H, PWIDTH):
        for r in range(1, H):
            try:
                stdscr.addch(r, PWIDTH, "|", curses.color_pair(C_DIM))
            except curses.error:
                pass

    # ── Single chat area ───────────────────────────────────────────────────
    def _draw_chat(self, stdscr, H, W, PWIDTH, CWIDTH, s, my_role, phase, is_alive):
        messages = s.get("messages", [])
        players  = s.get("players", {})
        mafia_pids  = s.get("mafia_pids", [])
        sheriff_log = s.get("sheriff_log", {})
        col0     = PWIDTH + 1

        # Build wrapped lines. Each entry stores:
        #   line        — full text of this wrapped line
        #   ch          — channel
        #   apid        — author pid
        #   prefix_len  — how many chars is the "Name: " prefix (first line only, 0 on continuations)
        #   name_attr   — curses attr for the name prefix
        wrapped = []
        for m in messages:
            ch     = m.get("channel", CH_TOWN)
            author = m.get("author", "?")
            apid   = m.get("author_pid")
            role   = m.get("author_role")
            text   = m.get("text", "")

            if ch == CH_SYS:
                prefix     = ""
                full       = text
                name_attr  = 0
            elif ch == CH_GHOST:
                prefix     = author + " (" + (role or "?") + "): "
                full       = prefix + text
                name_attr  = curses.color_pair(C_GREY) | curses.A_DIM
            elif ch == CH_MAFIA:
                prefix     = "[MAFIA] " + author + ": "
                full       = prefix + text
                name_attr  = curses.color_pair(C_RED) | curses.A_BOLD
            else:
                # Town chat — colour the name by the author's role as seen by THIS viewer
                prefix     = author + ": "
                full       = prefix + text
                name_attr  = self._name_attr_for(apid, players, my_role, mafia_pids,
                                                  sheriff_log, is_alive)

            lines = textwrap.wrap(full, CWIDTH - 2) or [full]
            for i, line in enumerate(lines):
                wrapped.append({
                    "line":       line,
                    "ch":         ch,
                    "apid":       apid,
                    "prefix_len": len(prefix) if i == 0 else 0,
                    "name_attr":  name_attr,
                })

        # Scroll
        chat_rows       = H - 6
        total           = len(wrapped)
        self.msg_offset = min(self.msg_offset, max(0, total - chat_rows))
        if self.msg_offset > 0:
            start = max(0, total - chat_rows - self.msg_offset)
            end   = total - self.msg_offset
        else:
            start = max(0, total - chat_rows)
            end   = total

        r = 2
        for entry in wrapped[start:end]:
            if r >= H - 4:
                break
            line       = entry["line"][: CWIDTH - 1]
            ch         = entry["ch"]
            prefix_len = entry["prefix_len"]
            name_attr  = entry["name_attr"]
            body_attr  = self._body_attr(entry)

            if ch == CH_GHOST:
                # Entire ghost line — italic + grey dim
                ghost_attr = curses.color_pair(C_GREY) | curses.A_DIM
                try:
                    ghost_attr |= curses.A_ITALIC
                except AttributeError:
                    pass  # older curses builds may not have A_ITALIC
                self._put(stdscr, r, col0, line, ghost_attr)
            elif ch == CH_SYS or prefix_len == 0:
                # System messages or continuation lines — no prefix colouring
                self._put(stdscr, r, col0, line, body_attr)
            else:
                # Render name prefix in its colour, body in white
                name_part = line[:prefix_len]
                body_part = line[prefix_len:]
                self._put(stdscr, r, col0,              name_part, name_attr)
                self._put(stdscr, r, col0 + prefix_len, body_part, body_attr)
            r += 1

        if self.msg_offset > 0:
            self._put(stdscr, 2, W - 16,
                      "^ scroll +" + str(self.msg_offset) + " ",
                      curses.color_pair(C_DIM))

    def _name_attr_for(self, apid, players, my_role, mafia_pids, sheriff_log, viewer_alive):
        """
        Return the curses attr to use for a player's name in chat.
        Applies the same colour rules as the player list.
        """
        if apid is None:
            return curses.color_pair(C_WHITE)
        p     = players.get(apid, {})
        is_me = apid == self.pid

        if not viewer_alive:
            # Dead viewer sees true role colours
            col, extra = self._role_colour_by_true_role(p.get("role"))
            attr = curses.color_pair(col) | extra
            if is_me:
                attr |= curses.A_BOLD
            return attr

        col, extra = self._player_colour(apid, p, my_role, mafia_pids, sheriff_log, is_me)
        attr = curses.color_pair(col) | extra
        if is_me:
            attr |= curses.A_BOLD
        return attr

    def _body_attr(self, entry):
        """Curses attr for the message body (after the name prefix)."""
        ch   = entry["ch"]
        apid = entry["apid"]
        if ch == CH_SYS:
            return curses.color_pair(C_GOLD) | curses.A_DIM
        if ch == CH_GHOST:
            return curses.color_pair(C_GREY) | curses.A_DIM
        if ch == CH_MAFIA:
            return curses.color_pair(C_WHITE)
        # Town
        if apid == self.pid:
            return curses.color_pair(C_WHITE) | curses.A_BOLD
        return curses.color_pair(C_WHITE)

    # ── Input bar ──────────────────────────────────────────────────────────
    def _draw_input(self, stdscr, H, W, PWIDTH, CWIDTH, phase, is_alive, my_role, is_host, s):
        col0 = PWIDTH + 1

        # Separator
        self._put(stdscr, H - 4, col0, "-" * (CWIDTH - 1), curses.color_pair(C_DIM))

        # Error message (shown on separator row, overrides it)
        if self.error_msg and time.time() - self.error_ts < 4:
            self._put(stdscr, H - 4, col0 + 1,
                      "  " + self.error_msg + "  "[: CWIDTH - 2],
                      curses.color_pair(C_RED))

        # Channel indicator + input
        can_chat  = self._can_chat(phase, is_alive, my_role)
        chan_tag  = ""
        if not is_alive:
            chan_tag = "[GHOST] "
        elif my_role == ROLE_MAFIA and phase == PHASE_NIGHT:
            chan_tag = "[MAFIA] "

        if can_chat:
            prompt = chan_tag + "> " + self.input_str
            self._put(stdscr, H - 3, col0, prompt[: CWIDTH - 1],
                      curses.color_pair(C_INPUT) | curses.A_BOLD)
        else:
            self._put(stdscr, H - 3, col0,
                      "[ Silent -- you cannot speak right now ]",
                      curses.color_pair(C_DIM))

        # Hint line
        hints = self._hints(phase, is_alive, my_role, is_host)
        self._put(stdscr, H - 2, col0, hints[: CWIDTH - 1], curses.color_pair(C_DIM))

        # Second hint line (commands)
        hints2 = self._hints2(phase, is_alive, my_role, is_host)
        self._put(stdscr, H - 1, col0, hints2[: CWIDTH - 1], curses.color_pair(C_DIM))

    def _can_chat(self, phase, is_alive, my_role):
        if not is_alive:
            return True   # dead can always ghost-chat
        if phase == PHASE_NIGHT:
            return my_role == ROLE_MAFIA   # only mafia talk at night
        return phase in (PHASE_DAY, PHASE_LOBBY)

    def _hints(self, phase, is_alive, my_role, is_host):
        if not is_alive:
            return "You are a ghost. Your messages are visible only to other dead players."
        if phase == PHASE_LOBBY:
            return "Waiting for players...  Chat freely."
        if phase == PHASE_DAY:
            return "Discuss with the town. Vote to eliminate a suspect."
        if phase == PHASE_NIGHT:
            if my_role == ROLE_MAFIA:
                return "Night. Coordinate with your crew."
            if my_role == ROLE_SHERIFF:
                return "Night. Use /investigate NAME to check a player."
            if my_role == ROLE_DOCTOR:
                return "Night. Use /protect NAME to protect a player."
            return "Night. The town sleeps. Wait for morning..."
        return ""

    def _hints2(self, phase, is_alive, my_role, is_host):
        parts = []
        if phase == PHASE_LOBBY and is_host:
            parts += ["/start", "/sheriff on|off", "/doctor on|off"]
        if phase == PHASE_DAY and is_alive:
            parts += ["/vote NAME", "/unvote", "/skip"]
        if phase == PHASE_NIGHT and is_alive:
            if my_role == ROLE_MAFIA:
                parts.append("/kill NAME")
            elif my_role == ROLE_SHERIFF:
                parts.append("/investigate NAME")
            elif my_role == ROLE_DOCTOR:
                parts.append("/protect NAME")
        parts += ["PgUp/PgDn scroll", "/quit"]
        return "  |  ".join(parts)

    # ── Lobby hint block ───────────────────────────────────────────────────
    def _draw_lobby_hint(self, stdscr, H, W, PWIDTH, is_host, s):
        """PWIDTH is passed explicitly to avoid NameError."""
        settings = s.get("settings", {})
        n        = len(s.get("players", {}))
        mafia_n  = 1 if n <= 5 else (2 if n <= 8 else 3)
        col0     = PWIDTH + 2

        lines = [
            "Players: " + str(n) + "/12  |  "
            + "Sheriff: " + ("ON" if settings.get("sheriff") else "OFF") + "  |  "
            + "Doctor: "  + ("ON" if settings.get("doctor")  else "OFF"),
            "Mafia count: " + str(mafia_n) + "  |  Min 4 players to start.",
        ]
        if is_host:
            lines.append("You are the HOST -- type /start when ready.")

        base_row = H - 6 - len(lines)
        for i, line in enumerate(lines):
            self._put(stdscr, base_row + i, col0,
                      line[: W - col0 - 1],
                      curses.color_pair(C_GOLD))

    # ── Utility ────────────────────────────────────────────────────────────
    def _put(self, stdscr, row, col, text, attr=0):
        try:
            stdscr.addstr(row, col, text, attr)
        except curses.error:
            pass

    # ══════════════════════════════════════════════════════════════════════
    # INPUT
    # ══════════════════════════════════════════════════════════════════════
    def _handle_input(self, stdscr):
        try:
            key = stdscr.getch()
        except Exception:
            return
        if key == -1:
            return
        if key == curses.KEY_PPAGE:
            self.msg_offset += 5; return
        if key == curses.KEY_NPAGE:
            self.msg_offset = max(0, self.msg_offset - 5); return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.input_str = self.input_str[:-1]; return
        if key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            self._submit(); return
        if 32 <= key <= 126 and len(self.input_str) < 280:
            self.input_str += chr(key)

    def _submit(self):
        text = self.input_str.strip()
        self.input_str  = ""
        self.msg_offset = 0
        if not text:
            return

        s        = self.state
        phase    = s.get("phase", PHASE_LOBBY)
        players  = s.get("players", {})
        me       = players.get(self.pid, {})
        my_role  = me.get("role") or "Unknown"
        is_alive = me.get("alive", True)
        is_host  = s.get("host_pid") == self.pid

        # ── Commands ──────────────────────────────────────────────────────
        if text.startswith("/"):
            parts = text.split()
            cmd   = parts[0].lower()

            if cmd == "/quit":
                self.running = False; return

            if cmd == "/start" and is_host:
                self._send({"type": "start"}); return

            if cmd in ("/sheriff", "/doctor") and is_host:
                val = not (len(parts) > 1 and parts[1].lower() == "off")
                cur = s.get("settings", {})
                self._send({"type": "settings",
                            "sheriff": val if cmd == "/sheriff" else cur.get("sheriff", True),
                            "doctor":  val if cmd == "/doctor"  else cur.get("doctor",  True)})
                return

            if cmd == "/vote" and phase == PHASE_DAY and is_alive:
                if len(parts) < 2:
                    self._err("Usage: /vote <name>")
                    return
                tpid = self._find_player(" ".join(parts[1:]), players, alive_only=True)
                if tpid:
                    self._send({"type": "vote", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            if cmd == "/skip" and phase == PHASE_DAY and is_alive:
                self._send({"type": "vote", "target": None})
                return

            if cmd == "/unvote" and phase == PHASE_DAY:
                self._send({"type": "vote", "target": None}); return

            if cmd == "/kill" and phase == PHASE_NIGHT and my_role == ROLE_MAFIA and is_alive:
                if len(parts) < 2:
                    self._err("Usage: /kill <name>")
                    return
                tpid = self._find_player(" ".join(parts[1:]), players,
                                         alive_only=True, exclude=self.pid)
                if tpid:
                    self._send({"type": "night_action", "action": "kill", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            if cmd == "/investigate" and phase == PHASE_NIGHT and my_role == ROLE_SHERIFF and is_alive:
                if len(parts) < 2:
                    self._err("Usage: /investigate <name>")
                    return
                tpid = self._find_player(" ".join(parts[1:]), players,
                                         alive_only=True, exclude=self.pid)
                if tpid:
                    self._send({"type": "night_action", "action": "investigate", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            if cmd == "/protect" and phase == PHASE_NIGHT and my_role == ROLE_DOCTOR and is_alive:
                if len(parts) < 2:
                    self._err("Usage: /protect <name>")
                    return
                tpid = self._find_player(" ".join(parts[1:]), players, alive_only=True)
                if tpid:
                    self._send({"type": "night_action", "action": "protect", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            self._err("Unknown command: " + cmd); return

        # ── Regular chat ──────────────────────────────────────────────────
        if not self._can_chat(phase, is_alive, my_role):
            return

        if not is_alive:
            channel = CH_GHOST
        elif my_role == ROLE_MAFIA and phase == PHASE_NIGHT:
            channel = CH_MAFIA
        else:
            channel = CH_TOWN

        self._send({"type": "chat", "channel": channel, "text": text})

    def _err(self, msg):
        self.error_msg = msg
        self.error_ts  = time.time()

    def _find_player(self, query, players, alive_only=False, exclude=None):
        q = query.strip().lower()
        if not q:
            return None
        for pid, p in players.items():
            if exclude and pid == exclude:
                continue
            if alive_only and not p.get("alive", True):
                continue
            if p["name"].lower().startswith(q):
                return pid
        return None


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(
        description="MAFIA -- Terminal social deduction game",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
QUICK START
  Host:  python mafia.py --host
  Join:  python mafia.py --join --server 192.168.1.5

INTERNET PLAY (free, no signup)
  1. Download ngrok: https://ngrok.com/download
  2. Run:  ngrok tcp 55000
  3. Share the address shown, e.g.: 0.tcp.ngrok.io:12345
  4. Others join: python mafia.py --join --server 0.tcp.ngrok.io --port 12345
        """
    )
    parser.add_argument("--host",   action="store_true", help="Host a session")
    parser.add_argument("--join",   action="store_true", help="Join a session")
    parser.add_argument("--server", default="127.0.0.1", help="Host IP or hostname")
    parser.add_argument("--port",   type=int, default=DEFAULT_PORT, help="Port (default 55000)")
    parser.add_argument("--name",   default="", help="Your player name")
    args = parser.parse_args()

    if not args.host and not args.join:
        parser.print_help()
        print("\nUse --host to host, or --join --server IP to join.\n")
        sys.exit(0)

    name = args.name.strip()
    if not name:
        name = input("Enter your name: ").strip()
    if not name:
        print("Name cannot be empty.")
        sys.exit(1)
    name = name[:20]

    if args.host:
        ip = get_local_ip()
        print("""
╔══════════════════════════════════════════════════════════╗
║                    M A F I A                             ║
╠══════════════════════════════════════════════════════════╣
║  Port: """ + str(args.port) + """
║  Your IP: """ + ip + """
║
║  Same-network join command:
║    python mafia.py --join --server """ + ip + """
║
║  Internet play:  ngrok tcp """ + str(args.port) + """
╚══════════════════════════════════════════════════════════╝
""")
        server = MafiaServer(port=args.port)
        threading.Thread(target=server.start, daemon=True).start()
        time.sleep(0.4)

        client = MafiaClient("127.0.0.1", args.port, name)
        try:
            client.connect()
        except Exception as e:
            print("Could not connect to local server: " + str(e))
            sys.exit(1)
        time.sleep(0.2)
        client.run()
        server.running = False

    else:
        print("Connecting to " + args.server + ":" + str(args.port) + " as '" + name + "'...")
        client = MafiaClient(args.server, args.port, name)
        try:
            client.connect()
        except Exception as e:
            print("Connection failed: " + str(e))
            print("Check the IP/port and make sure the host is running.")
            sys.exit(1)
        time.sleep(0.2)
        client.run()


if __name__ == "__main__":
    main()
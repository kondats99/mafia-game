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
C_DIM         = 10
C_SHERIFF_GOLD = 11   # golden yellow for sheriff

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
        self.votes              = {}   # pid -> target_pid
        self.night_acts         = {}   # pid -> {type, target}
        self.mafia_pids         = []
        self.winner             = None
        self.last_killed        = None
        self.last_saved         = False
        self.sheriff_results    = {}   # sheriff_pid -> {target_pid: bool}
        self.doc_last_protected = None
        self.night_resolving    = False   # countdown in progress
        self.night_countdown    = 0       # seconds remaining (0 = not counting)

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
            "doc_last_prot":    self.doc_last_protected,
            "night_countdown":  getattr(self, "night_countdown", 0),
            "night_resolving":  getattr(self, "night_resolving", False),
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
        srv.listen(20)
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
                if len(self.gs.players) >= 20:
                    self._send(conn, {"type": "error", "text": "Session full (max 20)."})
                    return pid
                # Reject duplicate names
                existing_names = [p["name"].lower() for p in self.gs.players.values()]
                if name.lower() in existing_names:
                    self._send(conn, {"type": "error",
                        "text": "Name '" + name + "' is already taken. Please choose a different name."})
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
                if target == "SKIP":
                    self.gs.votes[pid] = "SKIP"
                elif target and target in self.gs.players and self.gs.players[target]["alive"]:
                    self.gs.votes[pid] = target
                elif target is None:
                    self.gs.votes.pop(pid, None)
            self._broadcast_state()
            self._check_day_resolution()
            return pid

        if t == "night_action" and self.gs.phase == PHASE_NIGHT:
            target     = msg.get("target")
            action     = msg.get("action")
            do_resolve = False
            with self.gs.lock:
                actor = self.gs.players.get(pid)
                if not actor or not actor["alive"]:
                    return pid
                role  = actor["role"]
                valid = {ROLE_MAFIA: "kill", ROLE_SHERIFF: "investigate", ROLE_DOCTOR: "protect"}
                if action != valid.get(role):
                    return pid
                # Sheriff cannot re-investigate once submitted
                if role == ROLE_SHERIFF and pid in self.gs.night_acts:
                    self._send(conn, {"type": "error",
                        "text": "You have already investigated someone tonight."})
                    return pid
                if target and target in self.gs.players and self.gs.players[target]["alive"]:
                    if role == ROLE_MAFIA and target in self.gs.mafia_pids:
                        self._send(conn, {"type": "error",
                            "text": "You can't kill a teammate."})
                        return pid
                    if role == ROLE_DOCTOR and target == self.gs.doc_last_protected:
                        self._send(conn, {"type": "error",
                            "text": "Can't protect the same person two nights in a row."})
                        return pid
                    self.gs.night_acts[pid] = {"type": action, "target": target}
                    do_resolve = True
                    # Sheriff: immediately record result and prepare personal notification
                    if role == ROLE_SHERIFF:
                        is_m = target in self.gs.mafia_pids
                        self.gs.sheriff_results.setdefault(pid, {})[target] = is_m
                elif target is None:
                    if role == ROLE_SHERIFF:
                        return pid  # sheriff can't un-investigate
                    self.gs.night_acts.pop(pid, None)
            # Broadcast and resolve OUTSIDE the lock to prevent deadlock
            self._broadcast_state()
            if do_resolve:
                self._check_night_resolution()
            return pid

        return pid

    def _assign_roles(self):
        pids    = list(self.gs.players.keys())
        random.shuffle(pids)
        n       = len(pids)
        mafia_n = 1 if n <= 5 else (2 if n <= 8 else (3 if n <= 14 else 4))
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
            if any(v not in self.gs.votes for v in voters):
                return  # wait for everyone to vote (including /skip)

            tally = defaultdict(int)
            for v in voters:
                tally[self.gs.votes[v]] += 1
            max_v = max(tally.values())
            top   = [t for t, c in tally.items() if c == max_v]
            elim  = random.choice(top)

            # If SKIP wins (or ties and is chosen), no elimination
            skip_vote = (elim == "SKIP")
            victim    = None
            if not skip_vote:
                victim = self.gs.players[elim]
                victim["alive"] = False

            self.gs.phase            = PHASE_NIGHT
            self.gs.day             += 1
            self.gs.votes            = {}
            self.gs.night_acts       = {}
            self.gs.last_killed      = None
            self.gs.last_saved       = False
            self.gs.night_resolving  = False
            self.gs.night_countdown  = 0

        self._sys("-" * 52)
        if skip_vote:
            self._sys("  VOTE RESULT: The town chose to skip. No one was eliminated.")
        else:
            self._sys("  VOTE RESULT: " + victim["name"] + " has been eliminated.")
            self._sys("  Their role: " + ("Town" if victim["role"] in (ROLE_VILLAGER, ROLE_SHERIFF, ROLE_DOCTOR) else victim["role"]) + ".")
        self._sys("  Night " + str(self.gs.day) + " falls. The town goes silent.")
        self._sys("-" * 52)
        if not self._check_win():
            self._broadcast_state()

    def _check_night_resolution(self):
        """Check if all special roles have acted; if so, start the 5-second countdown."""
        with self.gs.lock:
            if self.gs.phase != PHASE_NIGHT:
                return
            if getattr(self.gs, "night_resolving", False):
                return  # countdown already running

            alive       = {p["pid"]: p for p in self.gs.players.values() if p["alive"]}
            mafia_alive = [pid for pid in self.gs.mafia_pids if pid in alive]

            # Find alive special roles (only if that role is enabled in settings)
            sheriff_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_SHERIFF and self.gs.settings["sheriff"]), None
            )
            doctor_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_DOCTOR and self.gs.settings["doctor"]), None
            )

            # ALL special roles must act before night ends
            mafia_rdy   = all(pid in self.gs.night_acts for pid in mafia_alive) if mafia_alive else True
            sheriff_rdy = (sheriff_pid is None or sheriff_pid in self.gs.night_acts)
            doctor_rdy  = (doctor_pid  is None or doctor_pid  in self.gs.night_acts)

            if not (mafia_rdy and sheriff_rdy and doctor_rdy):
                return  # still waiting on someone

            # Mark countdown started so we don't double-trigger
            self.gs.night_resolving = True

        # ── Broadcast "all actions in" state so mafia can see final kill votes ──
        self._broadcast_state()

        # ── 5-second countdown then resolve ──
        def _do_countdown():
            for i in range(5, 0, -1):
                with self.gs.lock:
                    self.gs.night_countdown = i
                self._broadcast_state()
                time.sleep(1)
            with self.gs.lock:
                self.gs.night_countdown = 0
            self._resolve_night()

        threading.Thread(target=_do_countdown, daemon=True).start()

    def _resolve_night(self):
        """Apply night actions and transition to day."""
        with self.gs.lock:
            if self.gs.phase != PHASE_NIGHT:
                return
            alive       = {p["pid"]: p for p in self.gs.players.values() if p["alive"]}
            mafia_alive = [pid for pid in self.gs.mafia_pids if pid in alive]

            sheriff_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_SHERIFF and self.gs.settings["sheriff"]), None
            )
            doctor_pid = next(
                (pid for pid, p in alive.items()
                 if p["role"] == ROLE_DOCTOR and self.gs.settings["doctor"]), None
            )

            # Sheriff investigation already recorded in _process immediately on submit.
            # Doctor protection
            protected = None
            if doctor_pid:
                act = self.gs.night_acts.get(doctor_pid)
                if act and act["type"] == "protect":
                    protected = act["target"]
                    self.gs.doc_last_protected = protected

            # Mafia kill tally
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

            self.gs.last_killed     = killed_pid
            self.gs.last_saved      = saved
            self.gs.phase           = PHASE_DAY
            self.gs.night_acts      = {}
            self.gs.votes           = {}
            self.gs.night_resolving = False
            self.gs.night_countdown = 0

        self._sys("-" * 52)
        if saved:
            self._sys("  MORNING: The Doctor saved someone -- no one died tonight.")
        elif killed_pid:
            v = self.gs.players[killed_pid]
            self._sys("  MORNING: " + v["name"] + " was found dead. The Mafia struck.")
            self._sys("  Their role: " + ("Town" if v["role"] in (ROLE_VILLAGER, ROLE_SHERIFF, ROLE_DOCTOR) else v["role"]) + ".")
        else:
            self._sys("  MORNING: A quiet night -- no one was killed.")
        self._sys("  Day " + str(self.gs.day) + " begins. Discuss and vote.")
        self._sys("-" * 52)
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
        self.input_str      = ""
        self.msg_offset     = 0
        self.error_msg      = ""
        self.error_ts       = 0
        self.seen_sheriff   = set()   # pids already notified about
        self.local_msgs     = []      # injected client-side messages (sheriff reveals)

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
                        self._check_sheriff_notifications()
                    elif obj.get("type") == "error":
                        self.error_msg = obj.get("text", "Error")
                        self.error_ts  = time.time()
            except Exception:
                break
        self.running = False

    def _check_sheriff_notifications(self):
        """Inject a local message when the sheriff gets a new investigation result."""
        s = self.state
        players = s.get("players", {})
        me = players.get(self.pid, {})
        if me.get("role") != ROLE_SHERIFF:
            return
        sheriff_log = s.get("sheriff_log", {})
        for inv_pid, is_mafia in sheriff_log.items():
            if inv_pid not in self.seen_sheriff:
                self.seen_sheriff.add(inv_pid)
                inv_name = players.get(inv_pid, {}).get("name", "?")
                if is_mafia:
                    text = "  INVESTIGATION: " + inv_name + " is Mafia!"
                else:
                    text = "  INVESTIGATION: " + inv_name + " is Clean!"
                self.local_msgs.append({
                    "text": text, "author": "SYSTEM", "author_pid": None,
                    "author_role": None, "channel": CH_SYS,
                    "ts": time.time(), "local": True,
                    "inv_pid": inv_pid, "is_mafia": is_mafia,
                })

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
        curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_BLUE, curses.COLOR_CYAN, -1)   # doctor cyan — same for all viewers
        curses.init_pair(C_GREY,   8,                   -1)
        curses.init_pair(C_GOLD,   curses.COLOR_YELLOW, -1)
        curses.init_pair(C_HEADER, curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(C_INPUT,  curses.COLOR_WHITE,  -1)
        curses.init_pair(C_DIM,    8,                   -1)
        # Sheriff gold: try to use a true #F1C40F-like gold via init_color (colour 11)
        # Falls back gracefully on terminals that don't support colour editing
        # Sheriff gold colour. Uses colour slot 200 (NOT 11 — that's a pair id)
        # to avoid any collision with default palette or pair IDs.
        try:
            if curses.can_change_color() and curses.COLORS >= 256:
                curses.init_color(200, 945, 769, 59)   # #F1C40F
                curses.init_pair(C_SHERIFF_GOLD, 200, -1)
            else:
                # Fallback: bold yellow looks gold-ish on most terminals
                curses.init_pair(C_SHERIFF_GOLD, curses.COLOR_YELLOW, -1)
        except Exception:
            curses.init_pair(C_SHERIFF_GOLD, curses.COLOR_YELLOW, -1)

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
        self._put(stdscr, row, 1, "PLAYERS", curses.color_pair(C_WHITE) | curses.A_BOLD)
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

            # ── Determine name colour ──────────────────────────────────────
            if not alive:
                # Dead players: always grey regardless of who's viewing
                name_attr = curses.color_pair(C_GREY) | curses.A_DIM
            elif not is_alive:
                # Dead viewer looking at an alive player: use true role colour
                tag_col, tag_extra = self._role_colour_by_true_role(p.get("role"))
                name_attr = curses.color_pair(tag_col) | tag_extra
            else:
                # Alive viewer: use role-based colour (limited by what they can see)
                col, extra = self._player_colour(pid, p, my_role, mafia_pids, sheriff_log, is_me)
                name_attr  = curses.color_pair(col) | extra

            # ── Determine inline role tag and its colour ───────────────────
            role_tag = self._role_tag(pid, p, my_role, mafia_pids, sheriff_log, is_me, is_alive)
            if role_tag:
                if not is_alive:
                    # Dead viewer: tag in role colour
                    tag_col, tag_extra = self._role_colour_by_true_role(p.get("role"))
                    tag_attr = curses.color_pair(tag_col) | tag_extra
                    if not alive:
                        tag_attr |= curses.A_DIM
                elif not alive:
                    tag_attr = curses.color_pair(C_DIM)
                else:
                    tag_attr = curses.color_pair(C_DIM)
            else:
                tag_attr = curses.color_pair(C_DIM)

            # ── Render name + marker on one line, tag appended inline ──────
            marker = ">" if is_me else " "
            suffix = " X" if not alive else (" <" if is_me else "")
            name_str = (" " + marker + p["name"] + suffix)[: PWIDTH - 1]

            if role_tag:
                # Render name part, then tag immediately after on same row
                name_col_end = len(name_str)
                tag_str = " " + role_tag
                # Truncate if combined exceeds panel
                if name_col_end + len(tag_str) > PWIDTH:
                    # Shorten name to fit tag
                    name_str = name_str[: PWIDTH - len(tag_str) - 1]
                    name_col_end = len(name_str)
                self._put(stdscr, row, 0, name_str, name_attr)
                self._put(stdscr, row, name_col_end, tag_str[: PWIDTH - name_col_end], tag_attr)
            else:
                self._put(stdscr, row, 0, name_str[: PWIDTH], name_attr)
            row += 1

        # Vote tally (day only) — show target counts, skips, and who voted
        if phase == PHASE_DAY:
            votes = s.get("votes", {})
            alive_players = {pid: p for pid, p in players.items() if p.get("alive")}

            if row < H - 6:
                row += 1
                voted_count   = len(votes)
                total_alive   = len(alive_players)
                header = "VOTES (" + str(voted_count) + "/" + str(total_alive) + ")"
                self._put(stdscr, row, 1, header[: PWIDTH - 2],
                          curses.color_pair(C_WHITE) | curses.A_BOLD)
                row += 1

            # Target tally
            tally = defaultdict(int)
            for v in votes.values():
                tally[v] += 1

            for tpid, cnt in sorted(tally.items(), key=lambda x: -x[1]):
                if row >= H - 6:
                    break
                if tpid == "SKIP":
                    label = "Skip: " + str(cnt)
                    self._put(stdscr, row, 1, label[: PWIDTH - 2],
                              curses.color_pair(C_DIM))
                else:
                    tname = players.get(tpid, {}).get("name", "?")[: PWIDTH - 5]
                    self._put(stdscr, row, 1, tname + ": " + str(cnt),
                              curses.color_pair(C_WHITE))
                row += 1

            # Show each alive player's vote status
            if row < H - 6:
                row += 1
                self._put(stdscr, row, 1, "WHO VOTED", curses.color_pair(C_WHITE) | curses.A_BOLD)
                row += 1
            for vpid, p in sorted(alive_players.items(), key=lambda x: x[1]["name"]):
                if row >= H - 6:
                    break
                vname = p["name"][: PWIDTH - 6]
                if vpid in votes:
                    target = votes[vpid]
                    if target == "SKIP":
                        mark = " skip"
                    else:
                        tgt_name = players.get(target, {}).get("name", "?")[:6]
                        mark = "->" + tgt_name
                    self._put(stdscr, row, 1, (vname + " " + mark)[: PWIDTH - 2],
                              curses.color_pair(C_GREEN))
                else:
                    self._put(stdscr, row, 1, (vname + " ...")[: PWIDTH - 2],
                              curses.color_pair(C_DIM))
                row += 1

        # Night action status + kill vote tally for mafia
        if phase == PHASE_NIGHT:
            night_acts      = s.get("night_acts", {})
            countdown       = s.get("night_countdown", 0)
            night_resolving = s.get("night_resolving", False)

            # Countdown display (visible to everyone at night)
            if night_resolving and countdown > 0 and row < H - 6:
                row += 1
                self._put(stdscr, row, 1, ("Dawn in " + str(countdown) + "...")[: PWIDTH - 2],
                          curses.color_pair(C_GOLD) | curses.A_BOLD)
                row += 1

            if is_alive and my_role in (ROLE_MAFIA, ROLE_SHERIFF, ROLE_DOCTOR):
                my_act = night_acts.get(self.pid)
                if row < H - 6:
                    if not night_resolving:
                        status = "Action set" if my_act else "Pending..."
                        scol   = C_GREEN if my_act else C_GOLD
                        self._put(stdscr, row, 1, status[: PWIDTH - 2], curses.color_pair(scol))
                        row += 1

                # Sheriff: show investigation log in side panel immediately
                if my_role == ROLE_SHERIFF and row < H - 6:
                    sheriff_log = s.get("sheriff_log", {})
                    if sheriff_log:
                        if row < H - 6:
                            self._put(stdscr, row, 1, "FINDINGS",
                                      curses.color_pair(C_WHITE) | curses.A_BOLD)
                            row += 1
                        for inv_pid, is_m in sheriff_log.items():
                            if row >= H - 6:
                                break
                            inv_name = players.get(inv_pid, {}).get("name", "?")[:PWIDTH - 8]
                            result_col = C_RED if is_m else C_GREEN
                            result_lbl = "MAFIA" if is_m else "clean"
                            label = inv_name + ": " + result_lbl
                            self._put(stdscr, row, 1, label[: PWIDTH - 2],
                                      curses.color_pair(result_col))
                            row += 1

                # Mafia: show kill vote tally
                if my_role == ROLE_MAFIA and night_acts and row < H - 6:
                    self._put(stdscr, row, 1, "KILL VOTES",
                              curses.color_pair(C_RED) | curses.A_BOLD)
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
          Sheriff — self = YELLOW+BOLD (gold),  confirmed mafia = RED, others = GREEN
          Doctor  — self = BLUE(cyan),          others = GREEN
          Villager— everyone GREEN
        """
        if my_role == ROLE_MAFIA:
            # All mafia (self + allies) same dark red; others green
            return (C_RED, 0) if pid in mafia_pids else (C_GREEN, 0)
        if my_role == ROLE_SHERIFF:
            if is_me:
                return (C_SHERIFF_GOLD, curses.A_BOLD)  # gold
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
            return (C_SHERIFF_GOLD, curses.A_BOLD)
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
            return "[Mafia]" if sheriff_log[pid] else "[Clean]"
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
        # Merge server messages with any local-only messages (e.g. sheriff notifications)
        # Sort by timestamp so they appear in chronological order
        server_msgs = s.get("messages", [])
        all_msgs    = sorted(server_msgs + self.local_msgs, key=lambda m: m.get("ts", 0))
        messages    = all_msgs
        players     = s.get("players", {})
        mafia_pids  = s.get("mafia_pids", [])
        sheriff_log = s.get("sheriff_log", {})
        col0        = PWIDTH + 1

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
                prefix     = author + ": "
                full       = prefix + text
                name_attr  = curses.color_pair(C_RED)   # no A_BOLD — keeps dark red
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
                ghost_attr = curses.color_pair(C_GREY) | curses.A_DIM
                try:
                    ghost_attr |= curses.A_ITALIC
                except AttributeError:
                    pass
                self._put(stdscr, r, col0, line, ghost_attr)
            elif ch == CH_SYS:
                # Rich rendering: bold keywords, coloured roles, white numbers
                self._render_sys_line(stdscr, r, col0, line, CWIDTH, players)
            elif prefix_len == 0:
                # Continuation wrap line — body colour only
                self._put(stdscr, r, col0, line, body_attr)
            else:
                # Name prefix in role colour, body in white
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
            return curses.color_pair(col) | extra

        # No A_BOLD for is_me — bold on RED/BLUE renders as light variants on
        # most terminals which breaks the uniform colour scheme
        col, extra = self._player_colour(apid, p, my_role, mafia_pids, sheriff_log, is_me)
        return curses.color_pair(col) | extra

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

    # ── Rich system message renderer ──────────────────────────────────────
    def _render_sys_line(self, stdscr, row, col, line, CWIDTH, players=None):
        """
        Render a system message line with inline colour segments:
          - BOLD KEYWORDS (MORNING, VOTE RESULT, etc.)  → bold white
          - Role words (Villager, Mafia, Sheriff, Doctor) → role colour
          - Day/Night numbers                            → bold white
          - Separator lines (all dashes/equals)         → dim grey
          - Everything else                             → dim grey
        """
        # Pure separator lines
        stripped = line.strip()
        if all(c in '-= ' for c in stripped) and stripped:
            self._put(stdscr, row, col, line[:CWIDTH-1], curses.color_pair(C_DIM))
            return

        # Build a list of (text, attr) segments by scanning the line
        # We tokenise by known special words
        ROLE_COLOURS = {
            ROLE_VILLAGER: (C_GREEN,  0),
            ROLE_MAFIA:    (C_RED,    0),
            ROLE_SHERIFF:  (C_SHERIFF_GOLD, curses.A_BOLD),
            ROLE_DOCTOR:   (C_BLUE, 0),
            "Clean":       (C_GREEN,  0),
            "Innocent":    (C_GREEN,  0),
            "Town":        (C_GREEN,  0),
        }
        BOLD_KEYWORDS = {
            "MORNING:", "VOTE RESULT:", "VILLAGE WINS!", "MAFIA WINS!",
            "Final Role Reveal", "MORNING", "VOTE RESULT",
            "INVESTIGATION:", "INVESTIGATION",
        }

        # Collect known player names for white highlighting
        player_names = sorted(
            [p["name"] for p in (players or {}).values()],
            key=len, reverse=True  # longest first to avoid partial matches
        )

        base_attr    = curses.color_pair(C_GOLD)                        # brownish yellow body text
        keyword_attr = curses.color_pair(C_RED)  | curses.A_BOLD          # MORNING:, VOTE RESULT: etc
        number_attr  = curses.color_pair(C_WHITE) | curses.A_BOLD          # day/night numbers

        # Split line into tokens keeping spaces, then reassemble with attrs
        # Simple approach: scan character by character matching known words
        segments = []
        i = 0
        while i < len(line):
            # Check for bold keywords at this position
            matched_kw = None
            for kw in sorted(BOLD_KEYWORDS, key=len, reverse=True):
                if line[i:i+len(kw)] == kw:
                    matched_kw = kw
                    break
            if matched_kw:
                segments.append((matched_kw, keyword_attr))
                i += len(matched_kw)
                continue

            # Check for player names (render white)
            matched_name = None
            for pname in player_names:
                if line[i:i+len(pname)] == pname:
                    before = line[i-1] if i > 0 else " "
                    after  = line[i+len(pname)] if i+len(pname) < len(line) else " "
                    if not before.isalpha() and not after.isalpha():
                        matched_name = pname
                        break
            if matched_name:
                segments.append((matched_name, curses.color_pair(C_WHITE) | curses.A_BOLD))
                i += len(matched_name)
                continue

            # Check for role names
            matched_role = None
            for role, (cpair, extra) in ROLE_COLOURS.items():
                if line[i:i+len(role)] == role:
                    # Make sure it's not mid-word (preceded/followed by non-alpha)
                    before = line[i-1] if i > 0 else " "
                    after  = line[i+len(role)] if i+len(role) < len(line) else " "
                    if not before.isalpha() and not after.isalpha():
                        matched_role = (role, cpair, extra)
                        break
            if matched_role:
                role_text, cpair, extra = matched_role
                segments.append((role_text, curses.color_pair(cpair) | extra))
                i += len(role_text)
                continue

            # Check for standalone numbers (day numbers etc.)
            if line[i].isdigit():
                j = i
                while j < len(line) and line[j].isdigit():
                    j += 1
                before = line[i-1] if i > 0 else " "
                after  = line[j] if j < len(line) else " "
                if not before.isalpha() and not after.isalpha():
                    segments.append((line[i:j], number_attr))
                    i = j
                    continue

            # Regular character — accumulate into base
            if segments and segments[-1][1] == base_attr:
                segments[-1] = (segments[-1][0] + line[i], base_attr)
            else:
                segments.append((line[i], base_attr))
            i += 1

        # Render segments
        x = col
        for text, attr in segments:
            if x >= col + CWIDTH - 1:
                break
            avail = col + CWIDTH - 1 - x
            self._put(stdscr, row, x, text[:avail], attr)
            x += len(text)

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
        can_type  = self._can_chat(phase, is_alive, my_role)
        chan_tag  = ""
        if not is_alive:
            chan_tag = "[GHOST] "
        elif my_role == ROLE_MAFIA and phase == PHASE_NIGHT:
            chan_tag = "[MAFIA] "
        elif my_role in (ROLE_SHERIFF, ROLE_DOCTOR) and phase == PHASE_NIGHT:
            night_acts = s.get("night_acts", {})
            if self.pid in night_acts:
                # Action done — show waiting message instead of input
                self._put(stdscr, H - 3, col0,
                          "[ Action submitted. Waiting for morning... ]",
                          curses.color_pair(C_DIM))
                hints = self._hints(phase, is_alive, my_role, is_host)
                self._put(stdscr, H - 2, col0, hints[: CWIDTH - 1], curses.color_pair(C_DIM))
                hints2 = self._hints2(phase, is_alive, my_role, is_host)
                self._put(stdscr, H - 1, col0, hints2[: CWIDTH - 1], curses.color_pair(C_DIM))
                return
            chan_tag = "[SHERIFF] " if my_role == ROLE_SHERIFF else "[DOCTOR]  "

        if can_type:
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
        """Whether the player can type in the input box right now."""
        if not is_alive:
            return True   # dead can always ghost-chat
        if phase == PHASE_NIGHT:
            # Mafia chat; sheriff/doctor can type commands even though they can't chat
            return my_role in (ROLE_MAFIA, ROLE_SHERIFF, ROLE_DOCTOR)
        return phase in (PHASE_DAY, PHASE_LOBBY)

    def _hints(self, phase, is_alive, my_role, is_host):
        if not is_alive:
            return "You are a ghost. Your messages are visible only to other dead players."
        if phase == PHASE_LOBBY:
            return "Waiting for players...  Chat freely."
        if phase == PHASE_DAY:
            return "Discuss with the town. Vote to eliminate a suspect. Use /skip to abstain."
        if phase == PHASE_NIGHT:
            if my_role == ROLE_MAFIA:
                return "Night. Coordinate with your crew. Use /kill NAME to vote who to eliminate."
            if my_role == ROLE_SHERIFF:
                night_acts = self.state.get("night_acts", {})
                if self.pid in night_acts:
                    return "Night. Investigation submitted. Waiting for morning..."
                return "Night. Use /investigate NAME to check a player."
            if my_role == ROLE_DOCTOR:
                night_acts = self.state.get("night_acts", {})
                if self.pid in night_acts:
                    return "Night. Protection submitted. Waiting for morning..."
                return "Night. Use /protect NAME to protect someone (not same person twice)."
            return "Night. The town sleeps. Wait for morning..."
        return ""

    def _hints2(self, phase, is_alive, my_role, is_host):
        parts = []
        if phase == PHASE_LOBBY and is_host:
            parts += ["/start", "/sheriff on|off", "/doctor on|off"]
        if phase == PHASE_DAY and is_alive:
            parts += ["/vote NAME", "/skip", "/unvote"]
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
        mafia_n  = 1 if n <= 5 else (2 if n <= 8 else (3 if n <= 14 else 4))
        col0     = PWIDTH + 2

        lines = [
            "Players: " + str(n) + "/20  |  "
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
                tpid = self._find_player(" ".join(parts[1:]), players, alive_only=True)
                if tpid:
                    self._send({"type": "vote", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            if cmd == "/unvote" and phase == PHASE_DAY:
                self._send({"type": "vote", "target": None}); return

            if cmd == "/skip" and phase == PHASE_DAY and is_alive:
                self._send({"type": "vote", "target": "SKIP"}); return

            if cmd == "/kill" and phase == PHASE_NIGHT and my_role == ROLE_MAFIA and is_alive:
                mafia_pids = s.get("mafia_pids", [])
                tpid = self._find_player(" ".join(parts[1:]), players,
                                         alive_only=True, exclude_pids=mafia_pids)
                if tpid:
                    self._send({"type": "night_action", "action": "kill", "target": tpid})
                else:
                    self._err("Player not found (can't target teammates): " + " ".join(parts[1:]))
                return

            if cmd == "/investigate" and phase == PHASE_NIGHT and my_role == ROLE_SHERIFF and is_alive:
                tpid = self._find_player(" ".join(parts[1:]), players,
                                         alive_only=True, exclude=self.pid)
                if tpid:
                    self._send({"type": "night_action", "action": "investigate", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            if cmd == "/protect" and phase == PHASE_NIGHT and my_role == ROLE_DOCTOR and is_alive:
                tpid = self._find_player(" ".join(parts[1:]), players, alive_only=True)
                if tpid:
                    self._send({"type": "night_action", "action": "protect", "target": tpid})
                else:
                    self._err("Player not found: " + " ".join(parts[1:]))
                return

            self._err("Unknown command: " + cmd); return

        # ── Regular chat ──────────────────────────────────────────────────
        if not is_alive:
            self._send({"type": "chat", "channel": CH_GHOST, "text": text})
            return

        if phase == PHASE_NIGHT:
            if my_role == ROLE_MAFIA:
                self._send({"type": "chat", "channel": CH_MAFIA, "text": text})
            # Sheriff and doctor cannot free-chat at night (commands only)
            return

        if phase in (PHASE_DAY, PHASE_LOBBY):
            self._send({"type": "chat", "channel": CH_TOWN, "text": text})

    def _err(self, msg):
        self.error_msg = msg
        self.error_ts  = time.time()

    def _find_player(self, query, players, alive_only=False, exclude=None, exclude_pids=None):
        q = query.strip().lower()
        if not q:
            return None
        for pid, p in players.items():
            if exclude and pid == exclude:
                continue
            if exclude_pids and pid in exclude_pids:
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

        while True:
            client = MafiaClient("127.0.0.1", args.port, name)
            try:
                client.connect()
            except Exception as e:
                print("Could not connect to local server: " + str(e))
                sys.exit(1)
            time.sleep(0.3)
            # Check if the server rejected us (name taken)
            if client.error_msg and "already taken" in client.error_msg:
                print("Name '" + name + "' is already taken.")
                name = input("Enter a different name: ").strip()[:20]
                if not name:
                    sys.exit(1)
                continue
            client.run()
            break
        server.running = False

    else:
        while True:
            print("Connecting to " + args.server + ":" + str(args.port) + " as '" + name + "'...")
            client = MafiaClient(args.server, args.port, name)
            try:
                client.connect()
            except Exception as e:
                print("Connection failed: " + str(e))
                print("Check the IP/port and make sure the host is running.")
                sys.exit(1)
            time.sleep(0.3)
            if client.error_msg and "already taken" in client.error_msg:
                print("Name '" + name + "' is already taken.")
                name = input("Enter a different name: ").strip()[:20]
                if not name:
                    sys.exit(1)
                continue
            client.run()
            break


if __name__ == "__main__":
    main()

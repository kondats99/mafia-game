# 🕵️‍♂️ MAFIA: Terminal Social Deduction Game

A sleek, terminal-based implementation of the classic social deduction game **Mafia**. Up to 20 players. 

---

## 🎮 Game Overview

In **Mafia**, players are divided into two groups: the **Town** and the **Mafia**.

* **The Mafia** knows who their partners are and must work together to eliminate Town members one by one during the Night — coordinating in a private chat invisible to the rest.
* **The Town** (Villagers, Sheriff, Doctor, and Vigilante) must use their intuition and special abilities to identify and eliminate the Mafia during the Day phase through democratic voting.

The game ends when all Mafia members are eliminated (**Town Wins**) or when the Mafia equals or outnumbers the Town (**Mafia Wins**).

---

## ✨ Features

* **Dynamic Role System:** Includes classic and optional roles:
    * 🗡️ **Mafia:** Coordinate kills in a private night chat. See your teammates. Can't kill each other.
    * ⭐ **Sheriff:** Investigate one player per night. Results appear immediately on your screen.
    * 🩺 **Doctor:** Protect one player per night from being killed. Cannot protect the same person two nights in a row.
    * 🔫 **Vigilante:** One bullet. Use it wisely — or save it. Town-aligned but acts at night.
    * 🚜 **Villager:** Use logic and social deduction to root out the killers.
* **Color-coded UI:** Every role has its own colour. Players see the world differently based on their role.
* **Ghost Chat:** Dead players can chat among themselves and watch the game unfold in real time — like spectator mode.
* **Night Countdowns:** 5-second countdowns before Night and before Dawn, giving players time to register what happened.
* **Skip Voting:** Not enough evidence? The Town can vote to skip the day's elimination.
* **Tie Voting:** A tied town vote results in no elimination — no random picks.
* **Auto-lobby:** After a game ends, a countdown returns all players to the lobby automatically, preserving chat history.
* **Real-time Interaction:** Synchronized day/night cycles, live chat, and role-specific action panels.
* **Command Aliases:** Shortcuts for every command so you can act fast.

---

## 🚀 Quick Start

### Prerequisites

* **Python 3.7+**
* **Windows:** `pip install windows-curses`
* **Mac/Linux:** `curses` is built-in.

### Hosting a Game

1. Open your terminal.
2. Run:
    ```bash
    python mafia.py --host
    ```
3. Share your **Local IP** and **Port** (default `55000`) with your friends.

### Joining a Game

1. Get the host's IP address.
2. Run:
    ```bash
    python mafia.py --join --server <SERVER_IP>
    ```

### Cross-Internet Play (via ngrok)

1. Host runs: `ngrok tcp 55000`
2. Share the ngrok address (e.g. `0.tcp.ngrok.io:12345`)
3. Others join:
    ```bash
    python mafia.py --join --server 0.tcp.ngrok.io --port 12345
    ```

---

## 🎭 Roles In Detail

### 🗡️ Mafia
**Alignment:** Mafia &nbsp;|&nbsp; **Night Action:** `/kill <name>`

The Mafia are the hidden killers. At the start of the game, every Mafia member is told who their teammates are — but no one else knows. Each night, the Mafia coordinate privately in their own chat to vote on who to eliminate. The player with the most votes is killed; ties are broken randomly among the tied candidates. During the day, Mafia members must blend in with the Town, casting suspicion on innocents and voting alongside them to avoid detection. They cannot kill their own teammates.

---

### ⭐ Sheriff
**Alignment:** Town &nbsp;|&nbsp; **Night Action:** `/investigate <name>`

The Sheriff is the Town's detective. Each night, they secretly investigate one player and immediately learn whether that player is **Mafia** or **Innocent**. The result appears only on the Sheriff's screen — no one else sees it. The Sheriff must decide how to use this information: revealing themselves to share findings risks being targeted by the Mafia; staying silent means watching the Town vote blindly. One investigation per night, and it cannot be changed once submitted.

---

### 🩺 Doctor
**Alignment:** Town &nbsp;|&nbsp; **Night Action:** `/protect <name>`

The Doctor can save lives. Each night, they choose one player to protect — including themselves. If the Mafia (or Vigilante) targets that same player, the attack is blocked and the protected player survives. The Doctor's protection is secret; neither the target nor anyone else knows they were saved. The only public sign is a morning announcement that no one died. The Doctor cannot protect the same person two nights in a row, so they must plan carefully.

---

### 🔫 Vigilante
**Alignment:** Town &nbsp;|&nbsp; **Night Action:** `/shoot <name>` or `/skip`

The Vigilante is a lone gunman on the side of the Town — but with only **one bullet for the entire game**. Each night, they can choose to shoot a player or save the bullet with `/skip`. Skipping does not spend the bullet, so the Vigilante can wait until they're certain of a target. Once the bullet is fired, the Vigilante becomes an ordinary Villager for the rest of the game. Shooting the wrong person is costly — killing a Town member is a big loss. Choose wisely.

---

### 🚜 Villager
**Alignment:** Town &nbsp;|&nbsp; **Night Action:** None

The Villager has no special abilities. They cannot act at night and receive no hidden information. Their only weapon is observation: paying close attention to what people say during the day, who votes for whom, and who seems to be pushing too hard or deflecting suspicion. A good Villager reads the room, builds trust, and convinces the Town to vote out the right people. Most players will be Villagers — and the Town's success depends on them getting it right.

---

### Role Scaling (Mafia count by player count)

| Players | Mafia |
| :--- | :--- |
| 4 – 5 | 1 |
| 6 – 8 | 2 |
| 9 – 14 | 3 |
| 15 – 20 | 4 |

---

## 🛠️ Controls & Commands

### ⚙️ Lobby (Host Only)

| Command | Alias | Description |
| :--- | :--- | :--- |
| `/start` | — | Start the game (min. 4 players). |
| `/sheriff on\|off` | `/sh` | Toggle Sheriff role. |
| `/doctor on\|off` | `/doc`, `/d` | Toggle Doctor role. |
| `/vigilante on\|off` | `/vig`, `/v` | Toggle Vigilante role. |

Settings are visible to all players in the chat footer throughout the lobby and in the bottom-left panel throughout the game.

### ☀️ Day Phase

| Command | Alias | Description |
| :--- | :--- | :--- |
| `/vote <name>` | `/v` | Vote to eliminate a player. |
| `/skip` | — | Vote to skip today's elimination. |
| `/unvote` | `/uv`, `/u` | Clear your vote. |

### 🌙 Night Phase

| Command | Alias | Role | Description |
| :--- | :--- | :--- | :--- |
| `/kill <name>` | `/k` | Mafia | Vote to kill a player. |
| `/investigate <name>` | `/inv`, `/i`, `/search` | Sheriff | Reveal a player's alignment. |
| `/protect <name>` | `/p`, `/save` | Doctor | Shield a player from death. |
| `/shoot <name>` | `/sh` | Vigilante | Fire your one bullet. |
| `/skip` | — | Vigilante | Hold your fire and save the bullet. |

### ☑️ Always

| Command | Alias | Description |
| :--- | :--- | :--- |
| `/quit` | `/q` | Exit the game. |
| `PgUp / PgDn` | — | Scroll chat history. |
| `↑ / ↓` | — | Navigate input history (like a terminal). |

---

## 🎨 Colour Key

| Colour | Meaning |
| :--- | :--- |
| 🔴 Dark Red | Mafia |
| 🟢 Green | Town / Villager / Innocent |
| 🟡 Gold | Sheriff |
| 🔵 Cyan | Doctor |
| 🟣 Purple | Vigilante |
| ⚪ White | Neutral / Numbers |
| ⬛ Grey | Dead players |

---

## 💬 Chat Channels

| Channel | Who sees it | When |
| :--- | :--- | :--- |
| Town | All alive players | Day only |
| Mafia | Mafia members | Night only |
| Ghost | Dead players only | Always |

Dead players see all channels — town, mafia, and ghost — in real time.

---

## ⚖️ License

This project is open-source and free to use for personal enjoyment.

*Good luck, and remember... trust no one.*

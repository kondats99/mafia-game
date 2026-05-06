# 🕵️‍♂️ MAFIA: Terminal Social Deduction Game

A sleek, terminal-based implementation of the classic social deduction game **Mafia**.

---

## 🎮 Game Overview

In **Mafia**, players are divided into two groups: the **Town** and the **Mafia**.

* **The Mafia** knows who their partners are and must work together to eliminate the Town members one by one during the Night.
* **The Town** (Villagers, Sheriff, and Doctor) must use their intuition and special abilities to identify and eliminate the Mafia during the Day phase through democratic voting.

The game ends when all Mafia members are eliminated (**Town Wins**) or when the Mafia outnumbers the Town (**Mafia Wins**).

---

## ✨ Features

* **Dynamic Role System:** Includes classic roles:
    * 🗡️ **Mafia:** Coordinate kills in a private night chat.
    * ⭐ **Sheriff:** Investigate players at night to reveal their true alignment.
    * 🩺 **Doctor:** Choose one player to protect from a Mafia attack each night.
    * 🚜 **Villager:** Use logic and social cues to root out the killers.
* **Real-time Interaction:** Features synchronized day/night cycles and live chat.
* **Intuitive UI:** A clean, `curses`-based terminal interface with color-coded feedback.
* **Skip Voting:** Don't have enough evidence? The Town can collectively choose to skip a vote.

---

## 🚀 Quick Start

### Prerequisites
* **Python 3.7+**
* **Windows Users:** `pip install windows-curses`
* **Mac/Linux Users:** `curses` is built-in.

### Hosting a Game
1.  Open your terminal.
2.  Run the host command:
    ```bash
    python mafia.py --host
    ```
3.  Share your **Local IP** and **Port** (default 55000) with your friends.

### Joining a Game
1.  Get the server IP from the host.
2.  Run the join command:
    ```bash
    python mafia.py --join --server <SERVER_IP> --name <YOUR_NAME>
    ```

---

## 🛠️ Controls & Commands

Interact with the game by typing commands into the chat bar:

| Command | Phase | Description |
| :--- | :--- | :--- |
| `/start` | Lobby | (Host Only) Starts the game. |
| `/vote <name>` | Day | Casts your vote to eliminate a player. |
| `/skip` | Day | Casts a vote to skip the elimination for the day. |
| `/kill <name>` | Night | (Mafia Only) Votes for a target to eliminate. |
| `/investigate <name>` | Night | (Sheriff Only) Check if a player is Mafia. |
| `/protect <name>` | Night | (Doctor Only) Protect a player from being killed. |
| `/quit` | Any | Safely exit the game. |

---

## ⚖️ License
This project is open-source and free to use for personal enjoyment.

*Good luck, and remember... trust no one.* 

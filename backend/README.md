<div align="center">

# ⚔️ Solo Leveling Bot

**A Telegram bot that gamifies your daily habits — Solo Leveling style.**

Register → pick your daily challenge time → get pinged with a "System" quest → log your reps → post proof to your squad's group chat → level up.

[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0.svg)](https://docs.aiogram.dev/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](#quick-start)

</div>

---

## ✨ What it does

> `[System]` A new quest has been generated for Player #839201.

Every day, at a time slot you choose, the bot fires a "System"-style notification and challenges you to complete a task — push-ups, squats, abs, chess, reading, or anything else you configure. Log a number, and it's added to your permanent stats. Send a photo, and it gets posted straight to your group chat so your friends can watch you grind. Skip too many days, and your streak resets. Tap **Give Up**, and you eat a 48-hour penalty.

It's a minimal, self-hostable accountability system built to feel like a leveling-up RPG rather than another boring habit tracker.

## 🧩 Features

| | |
|---|---|
| 🪪 **Zero-friction onboarding** | `/start` grabs your `user_id` automatically — no forms, just a few taps |
| ⏰ **4 daily time slots** | Morning / Day / Evening / Night — quests fire on a per-user schedule |
| 🎯 **Configurable focus areas** | Physical (push-ups, squats, abs) or mental (chess, reading) — easy to extend |
| 👥 **Auto group binding** | Add the bot to a group and it links itself to whoever added it — no manual ID copying required |
| 📸 **Proof-of-work photos** | Completed challenges can be posted to your group with an auto-generated caption |
| 📈 **XP, levels & streaks** | 100 XP per challenge, 1000 XP per level, persistent daily streak counter |
| 🎯 **Per-focus daily targets** | Every chosen focus has a goal (e.g. 50 push-ups); the day only counts as done once all goals are hit |
| 🕵️ **Undocumented x2 bonus** | Push a single discipline to double its target and secretly earn +5 levels instantly — then it locks for the day |
| 🏳️ **Give-up penalties** | Bailing on a quest resets your streak and starts a 48h penalty timer |
| ⌛ **Auto-expiry** | Unfinished quests silently expire after a configurable timeout |
| 🗣️ **Non-repetitive System voice** | Messages are composed from a fragment bank, not hardcoded — thousands of unique combinations out of the box |
| 📌 **Self-cleaning onboarding** | Each registration step deletes the previous one — no chat clutter |
| 📍 **Live pinned profile card** | A single pinned message with your stats, auto-updated on every challenge completion or expiry |
| 🐳 **One-command deploy** | Docker Compose, SQLite, no external services required |

## 🛠️ Tech stack

- **[aiogram 3.x](https://docs.aiogram.dev/)** — fully async Telegram Bot API framework
- **[aiosqlite](https://github.com/omnilib/aiosqlite)** — async SQLite, zero external DB to manage
- **[APScheduler](https://apscheduler.readthedocs.io/)** — cron-style scheduling for daily quests and expiry checks
- **Docker / Docker Compose** — one command, persistent volume

## 🚀 Quick start

```bash
git clone https://github.com/<you>/solo-leveling-bot.git
cd solo-leveling-bot
cp .env.example .env
```

1. Grab a token from [@BotFather](https://t.me/BotFather) and put it in `.env` as `BOT_TOKEN`.
2. (Optional) adjust `TZ` and the four time slots in `.env`.
3. Run it:

```bash
docker compose up -d --build
```

Your SQLite database will live at `./data_storage/db.sql` on the host — it survives rebuilds.

## 📁 Project structure

```
solo_bot/
├── bot/
│   ├── main.py            # entrypoint
│   ├── config.py          # settings, focus options, time-of-day labels
│   ├── database.py        # aiosqlite layer + schema
│   ├── keyboards.py        # inline keyboards
│   ├── messages.py         # System-voice message generator
│   ├── scheduler.py        # APScheduler jobs
│   ├── states.py           # FSM states for onboarding
│   └── handlers/
│       ├── start.py        # /start, onboarding flow
│       ├── group.py        # group binding (auto + manual)
│       ├── challenge.py    # daily quest logic
│       └── misc.py         # /profile, /help
├── data/
│   └── messages.json       # message fragment bank
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 🔧 How it works

<details>
<summary><strong>Onboarding</strong></summary>

<br>

`/start` → the bot reads `message.from_user.id` automatically → pick a daily quest time (4 buttons) → multi-select focus areas (push-ups / squats / abs / chess / reading — extend the list in `bot/config.py::FOCUS_OPTIONS`) → optionally bind a group → done.

Each step **deletes the previous step's message** before sending the next one (`bot/handlers/start.py::_advance_step`), so the chat stays clean instead of filling up with a trail of old prompts. The final step deletes itself and replaces it with a single **pinned profile card** (see below).

</details>

<details>
<summary><strong>Group binding</strong></summary>

<br>

The bot can capture a group's `chat_id` automatically: aiogram delivers a `my_chat_member` update whenever the bot is added to a group, and that update contains both the group's `chat.id` and the `from_user` who added it — enough to link the group to that user's profile with zero manual steps.

As a fallback, `/bind_group` (run inside the group) and `/group_id` are available for manual binding and debugging.

> **Note:** to let the bot read `/bind_group` messages in a group, disable Privacy Mode for the bot via BotFather (`/setprivacy` → `Disable`), or make it a group admin. Auto-binding via `my_chat_member` doesn't require this.

</details>

<details>
<summary><strong>Daily challenges</strong></summary>

<br>

A cron job runs for each of the 4 time slots (`TIME_MORNING` / `TIME_DAY` / `TIME_EVENING` / `TIME_NIGHT`). At the scheduled time, every user in that slot gets **one message** listing all of their chosen focus areas, each with a daily target (e.g. `🥊 Push-ups 0/50`) plus **Give Up**. That single message is the whole UI for the day — it's never re-sent, only **edited in place** (`bot/challenge_render.py`) as the player makes progress.

- Tapping a focus button sets it as the active focus: a marker emoji appears to its **right** (💪 for push-ups, 🧠 for chess, etc. — configurable per focus in `bot/config.py::FOCUS_OPTIONS`), and the message footer changes to a short prompt for a number.
- Sending a number is treated as **one set** — it's added to that focus's running total, and the user's message is deleted immediately to keep the chat clean.
- A focus gets a **✅** the moment its target is reached — but it stays selectable, so the player can keep stacking sets on it (e.g. chasing the secret bonus below) while other focuses are still open.
- The **day's challenge only completes once every focus has hit its target** (or the player gives up) — reaching one goal doesn't end the challenge by itself.
- **🏳 Give Up** resets the streak, applies the 48h penalty, and closes the message (keyboard removed).
- A separate job runs every 15 minutes and expires (edits + closes) any quest older than `CHALLENGE_TIMEOUT_HOURS` (default: 18h) that was never finished, resetting the streak.

**Secret x2 bonus:** this isn't advertised anywhere in the UI. If a player pushes a single focus to `target × 2` (e.g. 100 push-ups when the goal was 50), the bot fires a one-off "hidden protocol detected" message and instantly grants `+5 levels`. That focus is then **sealed** (`✅🔒`) for the rest of the day — it can't be selected again, so the bonus can't be farmed twice on the same discipline.

</details>

<details>
<summary><strong>The "System" voice</strong></summary>

<br>

Instead of a flat file of thousands of hardcoded strings, messages are assembled at runtime from a fragment bank (`data/messages.json`): `system_prefix + opener + body + closer`. Even with the current, relatively small bank, the "quest start" category alone yields **38,400** unique combinations (20 × 30 × 8 × 8) — and the bank grows combinatorially, not linearly, as you add fragments.

</details>

<details>
<summary><strong>Pinned profile card</strong></summary>

<br>

Once registration finishes, the bot sends and **pins** a single summary message (`bot/profile.py`) with the player's level, XP, streak, chosen time slot, group-binding status, and per-focus totals.

That message is then **edited in place** — never re-sent — whenever:
- a challenge is completed (reps logged),
- a challenge is given up on,
- a challenge expires (18h timeout),
- the group gets (re)bound.

If the pinned message was deleted by the user, `sync_profile_message()` transparently falls back to sending and re-pinning a fresh one. `/profile` just triggers the same sync — it never spams a new message.

</details>

## ⚙️ Configuration

All configuration lives in `.env` (see `.env.example`):

| Variable | Description | Default |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather | — |
| `TZ` | IANA timezone used for scheduling | `Asia/Almaty` |
| `TIME_MORNING` / `TIME_DAY` / `TIME_EVENING` / `TIME_NIGHT` | Daily dispatch times (`HH:MM`) | `05:00` / `13:00` / `19:00` / `23:00` |
| `CHALLENGE_TIMEOUT_HOURS` | Hours before an unfinished quest expires | `18` |
| `PENALTY_HOURS` | Penalty duration after "Give Up" | `48` |
| `DB_PATH` | Path to the SQLite file | `/app/data/db.sql` |

Per-focus daily targets, active-focus marker emojis, and the secret bonus multiplier/reward live in `bot/config.py` (`FOCUS_OPTIONS`, `BONUS_MULTIPLIER`, `BONUS_LEVELS`) rather than `.env`, since they're gameplay tuning rather than deployment config.

## 🗺️ Roadmap / ideas

- [ ] Configurable per-submission upper bound on logged reps (sanity check)
- [ ] Move focus areas from code into the DB + an admin command to add new ones
- [ ] Weekly/monthly recap messages ("Rank up" summaries)
- [ ] Optional Redis-backed FSM storage for multi-instance deployments

## 📄 License

MIT — do whatever you want with it.

---

<div align="center">
<sub>Built for people who need a little artificial pressure to actually show up every day.</sub>
</div>

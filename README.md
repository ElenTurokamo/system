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
| 🏳️ **Give-up penalties** | Bailing on a quest resets your streak and starts a 48h penalty timer |
| ⌛ **Auto-expiry** | Unfinished quests silently expire after a configurable timeout |
| 🗣️ **Non-repetitive System voice** | Messages are composed from a fragment bank, not hardcoded — thousands of unique combinations out of the box |
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

A cron job runs for each of the 4 time slots (`TIME_MORNING` / `TIME_DAY` / `TIME_EVENING` / `TIME_NIGHT`). At the scheduled time, every user in that slot gets a quest message with buttons for their chosen focus areas plus **Give Up**.

- Pick a focus → the bot asks for a number.
- Send a number → it's added to `daily_<focus>` in the DB, **+100 XP** is awarded, streak **+1**, level recalculated (`level = xp // 1000 + 1`).
- You're then prompted for a photo. If a group is bound, the photo is forwarded there with an auto-generated caption like *"Day N. Player `user_id` completed the challenge."*
- **Give Up** → streak resets to 0 and `penalty_until = now + 48h` is stored (a soft, in-app penalty marker — the bot obviously can't enforce real device restrictions).
- A separate job runs every 15 minutes and expires any quest older than `CHALLENGE_TIMEOUT_HOURS` (default: 18h) that was never finished, resetting the streak.

</details>

<details>
<summary><strong>The "System" voice</strong></summary>

<br>

Instead of a flat file of thousands of hardcoded strings, messages are assembled at runtime from a fragment bank (`data/messages.json`): `system_prefix + opener + body + closer`. Even with the current, relatively small bank, the "quest start" category alone yields **38,400** unique combinations (20 × 30 × 8 × 8) — and the bank grows combinatorially, not linearly, as you add fragments.

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
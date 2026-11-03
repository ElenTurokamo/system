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

Every day, at a time slot you choose, the bot fires a "System"-style notification and challenges you to complete a task — push-ups, squats, abs, chess, reading, or anything else you configure. Log a number, and it's added to your permanent stats. Send a photo, a video or a video note (a "circle"), and once you finish the challenge it is posted to your group chat together with the day's report so your friends can watch you grind. Fail a quest and the System reacts step by step: first your streak is frozen, then you take a penalty, and after the third failure in a row the System freezes itself until `/start`.

It's a minimal, self-hostable accountability system built to feel like a leveling-up RPG rather than another boring habit tracker.

## 🧩 Features

| | |
|---|---|
| 🪪 **Zero-friction onboarding** | `/start` grabs your `user_id` automatically — no forms, just a few taps |
| ⏰ **4 daily time slots** | Morning / Day / Evening / Night — quests fire on a per-user schedule |
| 🎯 **Configurable focus areas** | Physical (push-ups, squats, abs) or mental (chess, reading) — easy to extend |
| 👥 **Auto group binding** | Add the bot to a group and it links itself to whoever added it — no manual ID copying required |
| 📸 **Proof-of-work media** | A photo, a video or a video note (circle) is kept until the challenge is finished (manually or automatically), then posted to your group with an auto-generated report caption (for a circle — as the next message, because video notes cannot have captions) |
| 📈 **XP, levels & streaks** | 100 XP per challenge, 1000 XP per level, persistent daily streak counter |
| 🎯 **Per-focus daily targets** | Every chosen focus has a goal (e.g. 50 push-ups); the day only counts as done once all goals are hit |
| 🕵️ **Undocumented x2 bonus** | Push a single discipline to double its target and secretly earn +5 levels instantly — then it locks for the day |
| 🧊 **Streak freeze** | The first failed quest in a row is forgiven: the streak is frozen (🧊 instead of 🔥), no XP loss, no restrictions |
| 🏳️ **Penalties** | The second failed quest in a row (timeout or Give Up) takes XP, resets your streak and pauses new quests for 48h (shown in the profile with a countdown) |
| ❄️ **System freeze** | The third failed quest in a row freezes the System **without a penalty**: no new quests until you send `/start`; the profile gets a note at the very end |
| 🧹 **Short failure notice** | A compact notice (what failed + what happened: streak frozen / XP taken and restriction / System frozen) that deletes itself after 5 minutes |
| ⌛ **Auto-expiry** | Unfinished quests silently expire after a configurable timeout |
| 🗣️ **Non-repetitive System voice** | Messages are composed from a fragment bank, not hardcoded — thousands of unique combinations out of the box |
| 📌 **Self-cleaning onboarding** | Each registration step deletes the previous one — no chat clutter |
| 📍 **Live pinned profile card** | A single pinned message with your stats, auto-updated on every challenge completion or expiry |
| 💾 **State store** | A second, independent SQLite file keeps message keys (profile, failure report), the guide `file_id` and the auto-delete queue — survives a restart and a `db.sql` rollback |
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

Your SQLite database will live at `./data_storage/db.sql` on the host — it survives rebuilds. This is mounted to a dedicated `/app/storage` path inside the container, separate from the bundled `/app/data` (message fragment bank), so the mount never shadows files baked into the image.

The second database — the **state store** (`bot_state.db`) — lives on its own volume, `./data_state` on the host (`/app/state` in the container), and is backed up to git as a separate file `bot_state.db` next to `db.sql` (see "State store" below).

## 📁 Project structure

```
solo_bot/
├── bot/
│   ├── main.py            # entrypoint
│   ├── config.py          # settings, focus options, time-of-day labels
│   ├── database.py        # aiosqlite layer + schema
│   ├── state_store.py     # independent state DB: message keys, guide file_id, auto-delete queue
│   ├── failure_policy.py  # failure handling: streak freeze (1st) / penalty (2nd in a row) / System freeze (3rd, no penalty)
│   ├── keyboards.py        # inline keyboards
│   ├── messages.py         # System-voice message generator
│   ├── dispatch.py         # fault-tolerant daily delivery: retries, catch-up, skip rules
│   ├── scheduler.py        # APScheduler jobs
│   ├── completion.py       # quest completion (button + auto-finish on timeout)
│   ├── guide.py            # PDF guide (/help + registration)
│   ├── states.py           # FSM states for onboarding
│   └── handlers/
│       ├── start.py        # /start, onboarding flow
│       ├── group.py        # group binding (auto + manual)
│       ├── challenge.py    # daily quest logic
│       └── misc.py         # /profile, /help
├── data/
│   ├── messages.json       # message fragment bank
│   └── guide.pdf           # player guide (sent with /help and at registration)
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
- **🏳 Give Up** counts as a failed quest (see **Streak freeze** below: streak freeze the first time in a row, penalty the second, System freeze the third) and closes the message (keyboard removed). The button is shown only while at least one goal is still open: once every focus is closed it disappears and only **🏁 Finish** is left (it comes back if the player rolls progress back below a target). A stale click on it is rejected, and the SQL guard in `Database.give_up_challenge` makes a completed-goals quest impossible to give up even in a race.
- **Group reports are sent only when a challenge ends.** A photo, video or video note (circle) sent after the physical goals are closed is only *stored* (`challenges.physical_photo_file_id` + `challenges.physical_media_type`, the message is deleted from the chat; only the first file is accepted, the rest of an album is ignored; only in the private chat) — nothing reaches the group at that moment. The report goes out from `bot/completion.py` when the challenge is completed, manually (**🏁 Finish**) or automatically (timeout): with a stored photo or video it is the file plus the report caption (`Отчёт дня N. Игрок … завершил испытание.`, plus the secret-quest / eased lines evaluated at completion time), with a video note the circle goes first and the same report follows as a separate message (Telegram video notes have no caption); without a photo it is the same text — but only on the automatic path (a manual finish after skipping the photo posts nothing, as before). If the media cannot be sent, the same report goes out as text. A failed or given-up quest never posts the media.
- **Defeat message to the group.** When a quest fails (timeout or Give Up) an additional message is posted to the bound group — the player was defeated, the penalty (−XP, access restriction hours) and the streak reset (with the number of lost days if the streak was non-zero), `messages.group_failure_report`, sent from `failure_policy.post_failure_report`. It is sent once per failure (the same conditional `UPDATE` that guards the penalty guards it), is independent of the personal failure card, and a failing group never blocks the penalty. If the failure is forgiven (first in a row), the group message says so instead (`messages.group_failure_frozen_report`): the player was defeated, the streak is frozen, **no** penalty — and warns that the next failure in a row costs the streak. On the failure that freezes the System (third in a row) the group is told so (`messages.group_failure_system_frozen_report`).
- A separate job runs every minute and expires (edits + closes) any quest older than `CHALLENGE_TIMEOUT_HOURS` (default: 18h) that was never finished, counting it as a failure (freeze or penalty, see below).
- **Forgot to press "Finish"?** When the timeout hits, the quest is *not* failed if every discipline is already closed: it is completed automatically (XP, streak, daily stats, reset of the failure counter - exactly like the button), and the report (`Отчёт дня N. Игрок … завершил испытание.`, plus the secret-quest line if earned) is posted to the bound group: with the stored photo if the player sent one, otherwise as text. Never twice. The profile shows the quest (and the secret quest) as done. Day numbering in the report starts from 1 (the streak including the day being completed). While every goal is closed, no "time is running out / penalty" warnings are sent, and one that is already hanging is deleted the moment the last goal is closed (they come back if the player rolls progress back below the target). Check and status change are one atomic SQL `UPDATE` (`Database.complete_challenge_if_ready`), so the button, the timeout and Give Up can never both apply. Code: `bot/completion.py`.
- **Failure warnings:** the player gets a push 60, 30 and 15 minutes before the quest fails (the previous warning is deleted when the next one arrives, and all of them are removed when the quest closes). In the last 15 minutes the card timer switches to `MM:SS` and refreshes every `CHALLENGE_COUNTDOWN_REFRESH_SECONDS` (default 1).
- **Penalty pause:** after a penalty (the second failure in a row; the forgiven first one and the System-freezing third impose no pause) no new quests are issued until the penalty timer shown in the profile runs out; skipped slots are simply not dispatched. The profile shows just two short lines: `🚫 Штраф: новые испытания приостановлены.` and the countdown.
- **Failure notice:** the personal failure message is intentionally short — the failed quest and its reason, then only the outcome (`🧊` streak frozen / XP taken + streak reset + restriction / `❄️` System frozen). It is deleted `FAILURE_MESSAGE_TTL_MINUTES` (default 5) minutes after sending by a job that runs every 30 seconds (state lives in the DB, so a restart loses nothing; a transient Telegram error is retried on the next tick), or earlier when the next daily quest is issued.
- **System freeze (auto-stop):** if the player fails three daily quests in a row (timeout or Give Up, no completed quest in between), the System freezes itself (same state as `/stop`) and stays off until `/start`. **No penalty** is applied on that third failure (no XP loss, no restriction); the profile gets `❄️ Система заморожена. Для активации отправьте /start.` as its very last line. A completed quest resets the counter; `/start` after an auto-freeze starts from a clean slate. Threshold: `AUTO_STOP_AFTER_FAILURES` (default `3`). Two failures in a row do **not** stop the System — they only cost a penalty.
- **Delivery fault tolerance:** a quest only counts as issued once its card actually reaches the player (`bot/dispatch.py`, journal in the `dispatch_log` table). If sending fails, the bot retries every `DISPATCH_RETRY_INTERVAL_MINUTES` (60) up to `DISPATCH_RETRY_ATTEMPTS` (3) times — slot, +1h, +2h, +3h — and then skips the player until the next day; the undelivered card is removed and never penalizes the player (the timeout is counted from the moment of real delivery). No retries are made if the player switched the System off: `/stop` or auto-stop (also between attempts), a blocked bot (marked unreachable immediately), or a changed quest time. A check runs every minute; if the slot's cron did not fire at all (bot was down or restarted), quests are sent late within `DISPATCH_CATCHUP_WINDOW_MINUTES` (180) of the slot, only to players registered before the slot.

**Secret x2 bonus:** this isn't advertised anywhere in the UI. If a player pushes a single focus to `target × 2` (e.g. 100 push-ups when the goal was 50), the bot fires a one-off "hidden protocol detected" message and instantly grants `+5 levels`. That focus is then **sealed** (`✅🔒`) for the rest of the day — it can't be selected again, so the bonus can't be farmed twice on the same discipline.

</details>

<details>
<summary><strong>Lowering the difficulty ("📉 Снизить сложность")</strong></summary>

<br>

Around 50 push-ups a day, finishing the daily quest gets really hard, and a skipped day costs the streak. So from a certain level a button appears under the quest card:

- **When:** from `EASE_UNLOCK_LEVEL` - the first level where the push-up target reaches `EASE_UNLOCK_PUSHUPS_TARGET` (50), currently **level 22** (computed in `bot/config.py` from the target curve, not hard-coded). The level is one for every discipline because all targets grow along the same curve, so "the equivalent of 50 push-ups" is simply each discipline's own target at that level: squats 50, abs 50, pull-ups 40, running 25 min, chess 3 games, reading 30 pages (`ease_equivalent_targets()`).
- **Flow:** the card turns into a confirmation (goals "was → will be", what changes, **Да / Нет**). **Нет** restores the normal card and the button stays. **Да** is irreversible and works once per quest.
- **Effects of "Да":** every discipline's target is halved (rounded up, minimum 1: 50→25, 55→28, 25→13, 3→2); progress already logged is kept and can close a discipline at once; the reward for the quest is **half the XP** (`eased_reward`, rounded down); the **secret quest is off** for the day - no 🔥 marks, no bonus, even at double the old norm; the **streak continues** as usual; the profile and the group report get the note "📉 Сложность в этот день была понижена" (if the photo report had already gone out before easing, a short separate note is sent at completion).
- **Hidden when:** every goal of the day is closed, difficulty is already lowered, or the quest is closed. If progress is rolled back below the target, the button comes back.
- **Atomic:** `Database.ease_challenge` is a conditional `UPDATE` (status, not eased, an open goal exists), so a double "Да" or "Да" together with finish/expiry can't halve the targets twice. Code: `handlers/challenge.py` (`on_ease_*`), `challenge_render.py` (`push_ease_confirm`, `ease_offered`), `completion.py` (reward, notes).

</details>

<details>
<summary><strong>Streak freeze (🧊)</strong></summary>

<br>

A failed quest (timeout or **Give Up**) is judged by how many quests the player has failed **in a row** (`users.consecutive_failures`, reset by every completed quest):

| Failure | What happens |
|---|---|
| **1st in a row** | **Forgiven.** The streak is kept and *frozen*: the profile (and the friend brief) show 🧊 instead of 🔥 next to the streak. **No** XP loss, **no** access restriction, the next quest arrives at the usual time. The player gets a failure card without penalties, the bound group gets a failure message (without penalties) saying the streak is frozen. |
| **2nd in a row** | **Penalty:** −`MISSED_DAY_XP_PENALTY` XP, streak reset to 0, new quests paused for `PENALTY_HOURS`. The freeze is cleared. The System keeps running. |
| **3rd in a row** (`AUTO_STOP_AFTER_FAILURES`) | **System freeze, no penalty:** no XP loss, no restriction; the System stops sending quests until `/start`. |

Completing a quest while frozen unfreezes the streak (🔥 again) and it keeps growing: the frozen day is simply "skipped", it neither counts nor breaks the streak. A quest completed between two failures restores the grace (the next failure is the "1st in a row" again). The 60/30/15-minute warnings tell the truth too: they announce a streak freeze, a penalty or a System freeze, depending on which failure in a row the next one would be.

Details worth knowing:
- Both failure paths (timeout in `scheduler._expire_one`, **Give Up** in `handlers/challenge._execute_give_up`) go through the single `failure_policy.apply_failure`; it runs only after the conditional `UPDATE` that closes the quest, so a double click or a click racing the timeout cannot apply it twice.
- With `AUTO_STOP_AFTER_FAILURES=1` the very first failure freezes the System (no streak freeze, no penalty); with `=2` the ladder is shorter (streak freeze, then System freeze — no penalty step). The number of forgiven failures is `config.FREEZE_GRACE_FAILURES` (1), the rules are `config.failure_is_forgiven` / `config.failure_stops_system`.
- Players upgraded from the old version keep their counter but have no freeze flag (their earlier failure was already penalized), so their next failure is treated as the 2nd in a row.
- A manual `/stop` + `/start` keeps the freeze and the counter (no loophole); `/start` after an *auto*-stop starts from a clean slate.
- The player guide (`data/guide.pdf`, slide "Не успел? Вот что будет") is a pre-rendered PDF and still describes the old rule — regenerate it from `frontend/Гайд по системе.pptx` when convenient.

</details>

<details>
<summary><strong>State store (second local database)</strong></summary>

<br>

`bot/state_store.py` is a small independent SQLite file (`STATE_DB_PATH`, default `/app/state/bot_state.db` on its own Docker volume `./data_state`). It is opened **before** the main DB. The git auto-backup uploads it as a separate file (`bot_state.db`) next to `db.sql`; both files are copied via SQLite's online-backup API so a snapshot is never taken mid-write. Restoring `db.sql` from a backup does not overwrite the live state file.

**Why.** The main DB already stores the profile message id, but `db.sql` can be rolled back (restore from the twice-a-week backup, recreated volume, replaced file). Then it holds *stale* ids: the pinned profile can no longer be edited, the bot sends a new one and the old copy stays in the chat as a duplicate (and cannot be deleted once it is older than 48 h). The state store keeps a write-through copy of the keys that is not rolled back.

| Table | Contents | Used by |
|---|---|---|
| `message_keys` | `(kind, user_id) → chat_id, message_id, created_at, updated_at` for the **profile** and the **failure report** message; an empty `message_id` is a tombstone ("cleared") | `Database.set_/clear_profile_message`, `set_/clear_failure_message` (write-through), `Database.reconcile_message_keys` (startup) |
| `kv` | the Telegram `file_id` of the PDF guide, with a fingerprint (bot id + sha256 of `guide.pdf`) | `bot/guide.py` — no re-upload after a restart; replacing the PDF or the token invalidates it |
| `pending_deletes` | auto-delete queue of ephemeral messages (command replies, `/help` with the guide, friend list…) | `bot/utils.py` — `resume_pending_deletes` runs at startup, so such messages no longer stay in the chat forever after a restart |

**Reconciliation at startup** (`Database.reconcile_message_keys`, runs in `db.connect()`): for every player and key the *fresher* record wins (main DB: send time of the message; state store: `updated_at`); an empty store is seeded from the main DB; a tombstone newer than a stale key clears it; a live key missing in the main DB comes back from the store. After the rollback scenario the profile is edited in place — no duplicate.

**Principles.** The store is auxiliary: any failure (disk, permissions, locked file) is only logged and never breaks the bot; while it is not connected every method is a no-op. Process shutdown keeps `pending_deletes` rows (only a finished delete attempt or an explicit cancel removes them). Intentionally **not** persisted: the "Сдаться? / Снизить сложность?" confirmation screens and flood-control timestamps (monotonic clocks, the next timer tick redraws the card), per-player locks, and the dispatcher's "slot in progress" set (the DB journal `dispatch_runs` already covers restarts).

</details>

<details>
<summary><strong>One profile message, always</strong></summary>

<br>

Telegram does not allow editing or deleting a message older than 48 hours, so a pinned profile that is never re-sent would eventually freeze and, once re-sent, leave a stale duplicate behind. The bot avoids both **without sending messages at random times**:

- **Active players** - the profile is re-created together with the daily quest (right above the new card, silently, the old one is deleted). The dispatcher looks one day ahead: if the profile would cross the `PROFILE_EDIT_WINDOW_HOURS - PROFILE_REFRESH_BUFFER_HOURS` (48 - 6 = 42 h) line before the *next* quest, it is re-created now. In practice that is once a day; a profile the player requested himself a moment ago (`/profile`) is left alone. It cannot be "every other day": on the second day the message would be exactly 48 h old and could no longer be deleted.
- **Frozen/stopped players** (`/stop`, System freeze) - nothing is sent to them. Their old profile is quietly deleted before it turns 48 h old, and `/start` creates a fresh one when they return (otherwise two profiles would be left in the chat).
- **Safety net** - a job every 10 minutes (`scheduler.refresh_stale_profiles`) handles the rare case where a living player's profile reached the line without a quest being dispatched (re-created as above). It retries an unreachable player at most once per 30 minutes.

What the bot cannot fix: if the player blocked the bot (or the bot was down for more than the 6-hour buffer) while the profile aged past 48 h, Telegram refuses to delete the old message and it stays above the new one.

</details>

<details>
<summary><strong>PDF guide</strong></summary>

<br>

`data/guide.pdf` (player guide) is sent (a) together with `/help` - one message: the PDF with the command list as its caption (auto-deleted after 5 minutes, falls back to plain text if the file is missing or Telegram fails), and (b) once at the very start of registration, above the first step (flag `users.guide_sent`, so a repeated `/start` during registration does not resend it). The first upload caches Telegram's `file_id` in memory. To update the guide, replace `data/guide.pdf` and restart the bot. Code: `bot/guide.py`.

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
| `PENALTY_HOURS` | Duration of the penalty (new quests paused) after the 2nd failure in a row | `48` |
| `AUTO_STOP_AFTER_FAILURES` | Consecutive failed quests before the System freezes itself (that failure carries no penalty) | `3` |
| `FAILURE_MESSAGE_TTL_MINUTES` | Minutes after which the failure notice is deleted from the chat | `5` |
| `DISPATCH_RETRY_ATTEMPTS` | Retries after a failed quest delivery (then the player is skipped until the next day) | `3` |
| `DISPATCH_RETRY_INTERVAL_MINUTES` | Step between delivery attempts | `60` |
| `DISPATCH_CATCHUP_WINDOW_MINUTES` | How long after a slot a missed (cron did not fire) delivery is still sent | `180` |
| `DB_PATH` | Path to the SQLite file | `/app/storage/db.sql` |
| `STATE_DB_PATH` | Path to the state-store SQLite file (message keys, guide `file_id`, auto-delete queue) | `/app/state/bot_state.db` (default without Docker: `bot_state.db` next to `DB_PATH`) |
| `MISSED_DAY_XP_PENALTY` | XP taken on a penalty (the 2nd failure in a row) | `300` |

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

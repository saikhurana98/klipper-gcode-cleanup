# Design Constraints

This document captures every design constraint provided during the development of `klipper-gcode-cleanup`. It is the authoritative reference for why the system is built the way it is. Any future change that conflicts with a constraint here requires an explicit decision to revise it.

---

## 1. Scheduling

| Constraint | Value | Reason |
|---|---|---|
| Cleanup schedule | 1st of every month, 05:00 IST | Monthly cadence keeps storage tidy without being aggressive |
| Purge schedule | 7th of every month, 05:00 IST | 6-day recovery window after cleanup before files are gone permanently |
| Timezone | Asia/Kolkata (IST, UTC+5:30) | Printer Pi is set to IST; schedule must match local perception of time |
| Schedule mechanism | Hourly systemd user timer + in-script date/hour check | Schedule is fully configurable from the web editor without regenerating systemd unit files |

The timer fires every hour. The Python script reads `cleanup_day`, `purge_day`, and `run_hour` from `gcode_cleanup.cfg` at runtime and exits in under a second if the current moment does not match. This makes **all schedule values hot-configurable** from the Fluidd / Mainsail config editor with no service restart.

---

## 2. File lifecycle

### 2.1 Cleanup (1st of month)
- Stale files are **moved to `printer_data/gcodes_trash/`**, never hard-deleted on the first pass.
- A `manifest_YYYY-MM-DD.json` is written to the trash root recording every file moved, kept, and errored — it is **preserved through purges** for audit purposes.
- Companion thumbnail files (`.thumbs/<stem>-*`) are moved alongside their G-code file.
- Name collisions in the trash are resolved by appending the Unix timestamp to the stem.

### 2.2 Purge (7th of month)
- Everything in `gcodes_trash/` is **permanently deleted**, except manifest JSON files.
- Empty subdirectories inside the trash are removed bottom-up.

### 2.3 Empty directory removal
- After files are moved to trash, any **subdirectory that is now empty** is removed from `printer_data/gcodes/`.
- Nested `.thumbs` directories inside emptied project folders are removed first (so the parent can be `rmdir`'d).
- The **top-level `gcodes/.thumbs`** is never removed — Moonraker manages it.
- The `gcodes/` root itself is never removed.

---

## 3. Retention rules

A file is **never moved to trash** if either of the following holds:

| Rule | Condition | Configurable key |
|---|---|---|
| Recently uploaded | `now − file_mtime < min_upload_age_days` | `min_upload_age_days` (default: 7) |
| Recently printed | `now − last_print_start < min_since_print_days` | `min_since_print_days` (default: 7) |

**Upload time** is sourced from the filesystem `mtime` of the G-code file, which Moonraker sets to the upload timestamp.

**Last print time** is sourced from **Moonraker's job history API** (`/server/history/list`). The query uses an `after` filter so only jobs within the retention window are fetched — the full 1 000+ job history is never scanned unnecessarily.

---

## 4. Printer safety

- The script queries `print_stats.state` via the Moonraker API before doing anything.
- If the printer is **actively printing**, the cleanup run is **aborted entirely** and a notification is sent.
- This prevents any interference with an in-progress print job.

---

## 5. System access — zero sudo

**No `sudo` is ever used by the script or installer.**

| What | How |
|---|---|
| Scheduler | `systemctl --user` — unit files live in `~/.config/systemd/user/` |
| Session persistence | `loginctl enable-linger` — supported without root on systemd ≥ 239 |
| Config file | `~/printer_data/config/gcode_cleanup.cfg` — user-owned |
| Logs | `~/printer_data/logs/gcode-cleanup.log` + `/dev/log` syslog socket (user-writable) |
| Trash | `~/printer_data/gcodes_trash/` — user-owned |

The **one historical exception**: the very first deployment accidentally wrote unit files to `/etc/systemd/system/`. Those were manually cleaned up and the constraint was tightened to zero-sudo going forward.

---

## 6. No system file modification

The installer (`install.sh`) must never write to:
- `/etc/` or any subdirectory
- `/usr/` or any system path
- Any path outside the user's home directory

This is enforced as a hard rule: the addon must be installable and updatable by the `pi` user with no elevated privileges whatsoever.

---

## 7. Configuration must be editable via the web UI

All user-facing variables — **schedule, retention thresholds, and notification settings** — must live in `~/printer_data/config/gcode_cleanup.cfg`.

Fluidd and Mainsail surface every file in `printer_data/config/` in their built-in config editor. Users must be able to change any setting from a web browser without SSH access.

The install script **never overwrites an existing config** on update, so user edits survive `git pull`.

---

## 8. Moonraker update manager integration

The addon must be packageable as a `git_repo` type entry in Moonraker's `update_manager`, so it:
- Appears in the Fluidd / Mainsail **Update Manager panel** alongside Klipper and Fluidd
- Can be updated with a single click
- Re-runs `install.sh` after each `git pull` (idempotently)

```ini
[update_manager klipper-gcode-cleanup]
type: git_repo
path: ~/klipper-gcode-cleanup
origin: https://github.com/saikhurana98/klipper-gcode-cleanup.git
primary_branch: main
install_script: install.sh
```

---

## 9. Notifications

### Priority order
1. **ntfy** (primary) — real browser and phone push notifications via `https://ntfy.sh` or a self-hosted instance. Chosen because Fluidd console messages are easily missed.
2. **Fluidd / Mainsail console** (secondary fallback) — `RESPOND TYPE=command MSG="..."` GCode script via Moonraker API.
3. **Home Assistant** (future) — REST API call to an HA notification service. Wired in code but **disabled by default** (`homeassistant_enabled: false`). Must not activate until explicitly configured.

### Why not console-only
The original implementation used `RESPOND` to send messages to the Fluidd console. This was rejected because console messages are easily missed — especially for a background job running at 05:00. A proper web/push notification is required as the primary channel.

---

## 10. Logging

| Sink | Format | Purpose |
|---|---|---|
| `~/printer_data/logs/gcode-cleanup.log` | Rotating, 5 × 1 MB, timestamped | Local persistent log visible in Fluidd log viewer |
| `/dev/log` (syslog socket) | `klipper-cleanup[PID]: LEVEL msg` | Picked up by journald; query with `journalctl --user -t klipper-cleanup` |
| `stdout` | Plain level + message | Captured by systemd as journal output |

**Every file decision is logged** — whether a file was kept, moved, or errored, and the specific reason (uploaded recently / printed recently).

Future intent: forward logs to a **central log server** via journald remote forwarding. No code changes will be needed in the addon — only `journald.conf` on the Pi needs updating.

---

## 11. Code quality

| Tool | Purpose | Enforcement |
|---|---|---|
| `ruff check` | Linting (E, F, W, I, UP, B, C4 rules) | CI blocks merge on failure |
| `ruff format` | Formatting | CI blocks merge on failure |
| `pytest` | 38 unit tests covering all major paths | CI blocks merge on failure |

### Branch protection
- **Direct pushes to `main` are blocked** at the GitHub repository level.
- All changes must arrive via a pull request.
- Both `lint` and `test` CI jobs must pass before a PR can be merged.

---

## 12. Metadata source

G-code file metadata is **not** read from the Moonraker `/server/files/metadata` API. That endpoint returns `404` for files without slicer-embedded headers (empty files, non-annotated uploads). Instead:

| Data point | Source |
|---|---|
| Upload time | `os.stat(file).st_mtime` — filesystem mtime, set by Moonraker on upload |
| Last print time | `/server/history/list` — Moonraker job history, filtered by `after=cutoff` |

---

## 13. Constraints not yet implemented

These constraints were stated as future intent and are **not active**:

| Constraint | Status | Notes |
|---|---|---|
| Home Assistant notifications | Wired, disabled | Set `homeassistant_enabled: true` and fill credentials to activate |
| Central log server | Not implemented | Will require only `journald.conf` changes on the Pi, no addon changes |

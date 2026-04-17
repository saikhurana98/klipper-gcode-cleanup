# klipper-gcode-cleanup

Automatic stale G-code file management for [Klipper](https://www.klipper3d.org/) printers running [Moonraker](https://github.com/Arksine/moonraker).

Files are moved to a trash folder first, then permanently deleted several days later — giving you a recovery window. All thresholds and the schedule are configurable directly from the **Fluidd / Mainsail config editor**, no SSH required.

## How it works

| When | What happens |
|---|---|
| 1st of month at 05:00 (configurable) | Stale G-code files are moved to `gcodes_trash/` |
| 7th of month at 05:00 (configurable) | Everything in `gcodes_trash/` is permanently deleted |

**A file is never trashed if:**
- It was uploaded within the last 7 days (configurable), or
- It was started for a print within the last 7 days (configurable).

The script aborts cleanly if the printer is actively printing.

Notifications appear in the Fluidd / Mainsail console after each run. Home Assistant notifications are supported but disabled by default.

## Installation

```bash
# 1. Clone the repo into your home directory
cd ~
git clone https://github.com/skhurana333/klipper-gcode-cleanup.git

# 2. Run the install script
bash ~/klipper-gcode-cleanup/install.sh
```

That's it. The installer:
- Copies the default config to `~/printer_data/config/gcode_cleanup.cfg`
- Registers an hourly cron job (no root / sudo needed, no system files touched)

## Enable automatic updates via Moonraker

Add the following block to `~/printer_data/config/moonraker.conf`:

```ini
[update_manager klipper-gcode-cleanup]
type: git_repo
path: ~/klipper-gcode-cleanup
origin: https://github.com/skhurana333/klipper-gcode-cleanup.git
primary_branch: main
install_script: install.sh
```

Then restart Moonraker. The addon appears in the Update Manager panel alongside Klipper and Fluidd.

## Configuration

Edit `gcode_cleanup.cfg` in the Fluidd / Mainsail **Config files** tab. Changes take effect on the next hourly check — no restart needed.

```ini
[gcode_cleanup]

# ── Schedule ────────────────────────────────────────────────────────────────
cleanup_day: 1          # Day of month to move stale files to trash  (1–28)
purge_day: 7            # Day of month to permanently delete trash    (1–28)
run_hour: 5             # Hour to run  (0–23, local Pi timezone)

# ── Retention rules ─────────────────────────────────────────────────────────
min_upload_age_days: 7  # Keep files uploaded within this many days
min_since_print_days: 7 # Keep files printed within this many days

# ── Notifications ────────────────────────────────────────────────────────────
fluidd_notifications: true

homeassistant_enabled: false
homeassistant_url: http://homeassistant.local:8123
homeassistant_token:
homeassistant_notify_service: notify.notify
```

## Manual usage

```bash
# Dry-run (log decisions, change nothing)
python3 ~/klipper-gcode-cleanup/cleanup.py --cleanup --dry-run
python3 ~/klipper-gcode-cleanup/cleanup.py --purge   --dry-run

# Force a real run regardless of today's date
python3 ~/klipper-gcode-cleanup/cleanup.py --cleanup
python3 ~/klipper-gcode-cleanup/cleanup.py --purge
```

## Logs

```bash
# Rotating local log
tail -f ~/printer_data/logs/gcode-cleanup.log

# System journal (Raspberry Pi)
journalctl -t klipper-cleanup -f
```

## How scheduling works

A **systemd user timer** (`~/.config/systemd/user/klipper-cleanup.timer`) fires every hour. The Python script reads `gcode_cleanup.cfg` and exits immediately if the current day / hour don't match `cleanup_day` / `purge_day` / `run_hour`. This means:

- Changing the schedule in the Fluidd config editor takes effect on the very next hourly tick.
- No timer reload or restart is needed after a config edit.
- No system files are touched — everything lives under `~/.config/` and `~/printer_data/`.

## Uninstall

```bash
# Disable the timer
systemctl --user disable --now klipper-cleanup.timer

# Remove unit files
rm ~/.config/systemd/user/klipper-cleanup.{service,timer}
systemctl --user daemon-reload

# Remove the config (optional — preserves your settings if you reinstall)
rm ~/printer_data/config/gcode_cleanup.cfg

# Remove the repo
rm -rf ~/klipper-gcode-cleanup
```

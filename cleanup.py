#!/usr/bin/env python3
"""
klipper-gcode-cleanup — Auto-stale G-code file management for Klipper/Moonraker.

Normal usage (called by systemd timer every hour):
    cleanup.py

The script reads ~/printer_data/config/gcode_cleanup.cfg and:
  • Exits silently if today / current hour don't match the configured schedule.
  • Runs --cleanup mode on cleanup_day at run_hour.
  • Runs --purge  mode on purge_day  at run_hour.

Manual overrides (bypass date/hour check):
    cleanup.py --cleanup [--dry-run]
    cleanup.py --purge   [--dry-run]

Config file location can be overridden:
    cleanup.py --config /path/to/gcode_cleanup.cfg
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"

DEFAULT_CONFIG_PATHS = [
    Path("/home/pi/printer_data/config/gcode_cleanup.cfg"),
    Path.home() / "printer_data" / "config" / "gcode_cleanup.cfg",
]

DEFAULT_GCODES_DIR = Path("/home/pi/printer_data/gcodes")
DEFAULT_TRASH_DIR  = Path("/home/pi/printer_data/gcodes_trash")
DEFAULT_LOG_FILE   = Path("/home/pi/printer_data/logs/gcode-cleanup.log")

# Directory names inside gcodes_dir that are never touched
SKIP_DIRS = {".thumbs", "gcodes_trash"}

GCODE_EXTENSIONS = {".gcode", ".g", ".gc", ".gco"}


# ─────────────────────────────────────────────────────────────────────────────
# Configuration loader
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    """Reads gcode_cleanup.cfg and exposes typed accessors."""

    def __init__(self, path: Path):
        self._path = path
        self._cfg = configparser.ConfigParser(
            default_section="__defaults__",
            inline_comment_prefixes=("#",),
        )
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        self._cfg.read(path)

    def _get(self, key: str, fallback: str) -> str:
        return self._cfg.get("gcode_cleanup", key, fallback=fallback).strip()

    def _getint(self, key: str, fallback: int) -> int:
        return self._cfg.getint("gcode_cleanup", key, fallback=fallback)

    def _getbool(self, key: str, fallback: bool) -> bool:
        return self._cfg.getboolean("gcode_cleanup", key, fallback=fallback)

    # ── Schedule ─────────────────────────────────────────────────────────────
    @property
    def cleanup_day(self) -> int:
        return self._getint("cleanup_day", 1)

    @property
    def purge_day(self) -> int:
        return self._getint("purge_day", 7)

    @property
    def run_hour(self) -> int:
        return self._getint("run_hour", 5)

    # ── Retention ────────────────────────────────────────────────────────────
    @property
    def min_upload_age_days(self) -> int:
        return self._getint("min_upload_age_days", 7)

    @property
    def min_since_print_days(self) -> int:
        return self._getint("min_since_print_days", 7)

    # ── Moonraker ────────────────────────────────────────────────────────────
    @property
    def moonraker_host(self) -> str:
        return self._get("moonraker_host", "localhost")

    @property
    def moonraker_port(self) -> int:
        return self._getint("moonraker_port", 7125)

    # ── Paths (optional overrides in config) ─────────────────────────────────
    @property
    def gcodes_dir(self) -> Path:
        return Path(self._get("gcodes_dir", str(DEFAULT_GCODES_DIR)))

    @property
    def trash_dir(self) -> Path:
        return Path(self._get("trash_dir", str(DEFAULT_TRASH_DIR)))

    @property
    def log_file(self) -> Path:
        return Path(self._get("log_file", str(DEFAULT_LOG_FILE)))

    # ── Notifications ────────────────────────────────────────────────────────
    @property
    def fluidd_notifications(self) -> bool:
        return self._getbool("fluidd_notifications", True)

    @property
    def homeassistant_enabled(self) -> bool:
        return self._getbool("homeassistant_enabled", False)

    @property
    def homeassistant_url(self) -> str:
        return self._get("homeassistant_url", "")

    @property
    def homeassistant_token(self) -> str:
        return self._get("homeassistant_token", "")

    @property
    def homeassistant_notify_service(self) -> str:
        return self._get("homeassistant_notify_service", "notify.notify")


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("klipper-cleanup")
    logger.setLevel(logging.DEBUG)

    syslog_fmt = logging.Formatter("klipper-cleanup[%(process)d]: %(levelname)s %(message)s")
    file_fmt   = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )

    # Syslog → picked up by journald; query: journalctl -t klipper-cleanup
    try:
        sh = logging.handlers.SysLogHandler(address="/dev/log")
        sh.setLevel(logging.INFO)
        sh.setFormatter(syslog_fmt)
        logger.addHandler(sh)
    except OSError:
        pass  # not on a Linux system with /dev/log

    # Rotating local log (5 × 1 MB) in printer_data/logs/
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_048_576, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(file_fmt)
    logger.addHandler(fh)

    # stdout — captured by systemd unit's StandardOutput=journal
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    logger.addHandler(ch)

    return logger


# ─────────────────────────────────────────────────────────────────────────────
# Moonraker API client
# ─────────────────────────────────────────────────────────────────────────────

class MoonrakerClient:
    def __init__(self, host: str, port: int, timeout: int = 10):
        self.base    = f"http://{host}:{port}"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> dict:
        r = self._session.post(f"{self.base}{path}", json=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def printer_state(self) -> str:
        """Return current print_stats state (printing | standby | idle | error | …)."""
        try:
            d = self._get("/printer/objects/query", {"print_stats": ""})
            return d["result"]["status"]["print_stats"]["state"]
        except Exception:
            return "unknown"

    def recent_print_jobs(self, after_ts: float) -> dict[str, float]:
        """
        Return {filename: most_recent_start_time} for all jobs whose
        start_time > after_ts.  Paginates descending until the batch
        falls entirely before the cutoff.
        """
        result: dict[str, float] = {}
        start = 0
        limit = 100

        while True:
            try:
                data = self._get(
                    "/server/history/list",
                    {"limit": limit, "start": start, "order": "desc"},
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"History API error: {exc}") from exc

            jobs = data.get("result", {}).get("jobs", [])
            if not jobs:
                break

            any_recent = False
            for job in jobs:
                st = job.get("start_time", 0.0)
                if st <= after_ts:
                    # Everything past this point is older — stop early
                    return result
                any_recent = True
                fname = job.get("filename", "")
                if fname and st > result.get(fname, 0.0):
                    result[fname] = st

            if not any_recent or len(jobs) < limit:
                break
            start += limit

        return result

    def send_gcode(self, script: str) -> None:
        """Execute a GCode script (RESPOND sends a message to the Fluidd console)."""
        self._post("/printer/gcode/script", {"script": script})

    def notify_homeassistant(self, url: str, token: str, service: str, message: str, title: str) -> None:
        endpoint = f"{url}/api/services/{service.replace('.', '/')}"
        r = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"title": title, "message": message},
            timeout=10,
        )
        r.raise_for_status()


# ─────────────────────────────────────────────────────────────────────────────
# Notifier
# ─────────────────────────────────────────────────────────────────────────────

class Notifier:
    def __init__(self, cfg: Config, client: MoonrakerClient, log: logging.Logger):
        self._cfg    = cfg
        self._client = client
        self._log    = log

    def send(self, message: str, title: str = "Klipper Cleanup") -> None:
        self._fluidd(message)
        self._homeassistant(message, title)

    def _fluidd(self, message: str) -> None:
        if not self._cfg.fluidd_notifications:
            return
        escaped = message.replace('"', '\\"')
        try:
            self._client.send_gcode(f'RESPOND TYPE=command MSG="{escaped}"')
        except Exception as exc:
            self._log.warning("Fluidd notification failed: %s", exc)

    def _homeassistant(self, message: str, title: str) -> None:
        if not self._cfg.homeassistant_enabled:
            return
        if not self._cfg.homeassistant_url or not self._cfg.homeassistant_token:
            self._log.warning("HA notifications enabled but URL/token missing in config")
            return
        try:
            self._client.notify_homeassistant(
                self._cfg.homeassistant_url,
                self._cfg.homeassistant_token,
                self._cfg.homeassistant_notify_service,
                message,
                title,
            )
        except Exception as exc:
            self._log.warning("Home Assistant notification failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup job  (1st of month by default)
# ─────────────────────────────────────────────────────────────────────────────

class CleanupJob:
    def __init__(
        self,
        cfg: Config,
        client: MoonrakerClient,
        notifier: Notifier,
        log: logging.Logger,
        dry_run: bool = False,
    ):
        self._cfg      = cfg
        self._client   = client
        self._notifier = notifier
        self._log      = log
        self._dry_run  = dry_run

    def run(self) -> None:
        now_ts   = time.time()
        now_dt   = datetime.fromtimestamp(now_ts)
        # A file is "recent" if it falls within the stricter of the two thresholds
        upload_cutoff = now_ts - (self._cfg.min_upload_age_days  * 86_400)
        print_cutoff  = now_ts - (self._cfg.min_since_print_days * 86_400)

        self._log.info(
            "=== Cleanup started | v%s | upload_cutoff=%s | print_cutoff=%s | dry_run=%s ===",
            VERSION,
            datetime.fromtimestamp(upload_cutoff).isoformat(timespec="seconds"),
            datetime.fromtimestamp(print_cutoff).isoformat(timespec="seconds"),
            self._dry_run,
        )

        # Safety: never run during an active print
        state = self._client.printer_state()
        if state == "printing":
            msg = "Printer is currently printing — cleanup aborted."
            self._log.warning(msg)
            self._notifier.send(f"[Cleanup] ABORTED: {msg}")
            return
        self._log.info("Printer state: %s — proceeding", state)

        # Fetch recent print history in one paginated sweep
        self._log.info("Querying print history ...")
        try:
            recent_prints = self._client.recent_print_jobs(print_cutoff)
            self._log.info(
                "History contains %d file(s) printed since %s",
                len(recent_prints),
                datetime.fromtimestamp(print_cutoff).isoformat(timespec="seconds"),
            )
        except Exception as exc:
            self._log.error("History API failed: %s — aborting", exc)
            self._notifier.send("[Cleanup] ERROR: history API unavailable. Aborted.")
            return

        files    = self._discover_files()
        moved:  list[str] = []
        kept:   list[str] = []
        errors: list[str] = []

        self._log.info("Discovered %d G-code file(s)", len(files))

        for path in files:
            rel = str(path.relative_to(self._cfg.gcodes_dir))
            reason = self._keep_reason(path, rel, upload_cutoff, print_cutoff, recent_prints)
            if reason:
                self._log.info("KEEP  %-60s  (%s)", rel, reason)
                kept.append(rel)
            else:
                try:
                    self._move_to_trash(path, rel, now_ts)
                    self._log.info("TRASH %s", rel)
                    moved.append(rel)
                except Exception as exc:
                    self._log.error("ERROR trashing %s: %s", rel, exc)
                    errors.append(rel)

        if not self._dry_run:
            self._write_manifest(now_ts, moved, kept, errors)

        summary = (
            f"[Cleanup] {len(moved)} moved to trash, {len(kept)} kept, {len(errors)} error(s)."
        )
        self._log.info(summary)
        self._notifier.send(summary)
        if errors:
            self._log.error("Files with errors: %s", ", ".join(errors))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _discover_files(self) -> list[Path]:
        found = []
        for root, dirs, files in os.walk(self._cfg.gcodes_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if Path(name).suffix.lower() in GCODE_EXTENSIONS:
                    found.append(Path(root) / name)
        return found

    def _keep_reason(
        self,
        path: Path,
        rel: str,
        upload_cutoff: float,
        print_cutoff: float,
        recent_prints: dict[str, float],
    ) -> Optional[str]:
        mtime = path.stat().st_mtime
        if mtime > upload_cutoff:
            return f"uploaded {datetime.fromtimestamp(mtime).isoformat(timespec='seconds')}"

        last_print = recent_prints.get(rel)
        if last_print and last_print > print_cutoff:
            return f"printed {datetime.fromtimestamp(last_print).isoformat(timespec='seconds')}"

        return None

    def _move_to_trash(self, src: Path, rel: str, run_ts: float) -> None:
        if self._dry_run:
            self._log.info("[DRY-RUN] would move: %s", rel)
            return

        dest = self._cfg.trash_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Avoid name collisions with a timestamp suffix
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}__{int(run_ts)}{dest.suffix}")

        shutil.move(str(src), str(dest))

        # Best-effort: move companion thumbnails
        thumbs_src = src.parent / ".thumbs"
        if thumbs_src.is_dir():
            for thumb in thumbs_src.glob(f"{src.stem}-*"):
                t_dest = self._cfg.trash_dir / ".thumbs" / thumb.name
                t_dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(thumb), str(t_dest))
                except Exception:
                    pass

    def _write_manifest(self, run_ts: float, moved: list, kept: list, errors: list) -> None:
        date_str = datetime.fromtimestamp(run_ts).strftime("%Y-%m-%d")
        manifest = self._cfg.trash_dir / f"manifest_{date_str}.json"
        with open(manifest, "w") as f:
            json.dump(
                {
                    "version":       VERSION,
                    "run_time":      datetime.fromtimestamp(run_ts).isoformat(timespec="seconds"),
                    "retention": {
                        "min_upload_age_days":  self._cfg.min_upload_age_days,
                        "min_since_print_days": self._cfg.min_since_print_days,
                    },
                    "moved":  moved,
                    "kept":   kept,
                    "errors": errors,
                },
                f,
                indent=2,
            )
        self._log.info("Manifest written → %s", manifest)


# ─────────────────────────────────────────────────────────────────────────────
# Purge job  (7th of month by default)
# ─────────────────────────────────────────────────────────────────────────────

class PurgeJob:
    def __init__(
        self,
        cfg: Config,
        notifier: Notifier,
        log: logging.Logger,
        dry_run: bool = False,
    ):
        self._cfg      = cfg
        self._notifier = notifier
        self._log      = log
        self._dry_run  = dry_run

    def run(self) -> None:
        trash = self._cfg.trash_dir
        self._log.info(
            "=== Purge started | v%s | trash=%s | dry_run=%s ===",
            VERSION, trash, self._dry_run,
        )

        if not trash.exists():
            self._log.info("Trash directory does not exist — nothing to purge.")
            return

        deleted_files = deleted_dirs = errors = preserved = 0

        # Deepest paths first so parent dirs are empty before we rmdir them
        all_items = sorted(trash.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for item in all_items:
            if not item.exists():
                continue
            # Keep manifests for audit (tiny JSON, negligible disk cost)
            if item.is_file() and item.name.startswith("manifest_") and item.suffix == ".json":
                preserved += 1
                continue
            try:
                if self._dry_run:
                    self._log.info("[DRY-RUN] would delete: %s", item)
                elif item.is_file() or item.is_symlink():
                    item.unlink()
                    deleted_files += 1
                elif item.is_dir():
                    try:
                        item.rmdir()  # only succeeds when empty
                        deleted_dirs += 1
                    except OSError:
                        pass
            except Exception as exc:
                self._log.error("ERROR deleting %s: %s", item, exc)
                errors += 1

        summary = (
            f"[Purge] {deleted_files} files, {deleted_dirs} dirs deleted; "
            f"{preserved} manifest(s) preserved; {errors} error(s)."
        )
        self._log.info(summary)
        self._notifier.send(summary)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def resolve_config(override: Optional[str]) -> Path:
    if override:
        return Path(override)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Could not find gcode_cleanup.cfg. "
        f"Tried: {[str(p) for p in DEFAULT_CONFIG_PATHS]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Klipper G-code stale file cleanup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config",  metavar="PATH", help="Path to gcode_cleanup.cfg")
    parser.add_argument("--cleanup", action="store_true", help="Force cleanup mode (ignore schedule)")
    parser.add_argument("--purge",   action="store_true", help="Force purge mode   (ignore schedule)")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions without making changes")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    if args.cleanup and args.purge:
        parser.error("--cleanup and --purge are mutually exclusive")

    try:
        config_path = resolve_config(args.config)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = Config(config_path)
    except Exception as exc:
        print(f"[ERROR] Failed to load config: {exc}", file=sys.stderr)
        sys.exit(1)

    log = setup_logging(cfg.log_file)
    log.debug("Config loaded from %s", config_path)

    client   = MoonrakerClient(cfg.moonraker_host, cfg.moonraker_port)
    notifier = Notifier(cfg, client, log)

    now = datetime.now()

    # ── Determine which mode to run ───────────────────────────────────────────
    if args.cleanup:
        mode = "cleanup"
    elif args.purge:
        mode = "purge"
    else:
        # Auto-detect from schedule — this is the normal hourly-timer path
        if now.hour != cfg.run_hour:
            log.debug(
                "Hour %02d ≠ configured run_hour %02d — nothing to do.",
                now.hour, cfg.run_hour,
            )
            return

        if now.day == cfg.cleanup_day:
            mode = "cleanup"
        elif now.day == cfg.purge_day:
            mode = "purge"
        else:
            log.debug(
                "Day %d matches neither cleanup_day=%d nor purge_day=%d — nothing to do.",
                now.day, cfg.cleanup_day, cfg.purge_day,
            )
            return

    log.info("Running in %s mode (config: %s)", mode, config_path)

    if mode == "cleanup":
        CleanupJob(cfg, client, notifier, log, dry_run=args.dry_run).run()
    else:
        PurgeJob(cfg, notifier, log, dry_run=args.dry_run).run()


if __name__ == "__main__":
    main()

"""
Tests for klipper-gcode-cleanup.

Run with:  pytest tests/ -v
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from cleanup import (
    GCODE_EXTENSIONS,
    SKIP_DIRS,
    CleanupJob,
    Config,
    Notifier,
    PurgeJob,
    resolve_config,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def make_config(tmp_path: Path, extra: str = "") -> Config:
    """Write a minimal config file and return a Config instance.

    Only path settings are fixed.  Schedule / retention values use Config
    class defaults unless overridden via ``extra``.
    """
    cfg_text = dedent(f"""
        [gcode_cleanup]
        gcodes_dir: {tmp_path / "gcodes"}
        trash_dir: {tmp_path / "trash"}
        log_file: {tmp_path / "cleanup.log"}
        {extra}
    """)
    cfg_file = tmp_path / "gcode_cleanup.cfg"
    cfg_file.write_text(cfg_text)
    return Config(cfg_file)


def make_gcode(path: Path, age_days: float) -> Path:
    """Create a gcode file with the given age (mtime set relative to now)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("; gcode")
    mtime = time.time() - age_days * 86_400
    import os

    os.utime(path, (mtime, mtime))
    return path


def null_notifier() -> Notifier:
    """A notifier that does nothing."""
    n = MagicMock(spec=Notifier)
    return n


def null_client() -> MagicMock:
    client = MagicMock()
    client.printer_state.return_value = "standby"
    client.recent_print_jobs.return_value = {}
    return client


def null_logger() -> MagicMock:
    import logging

    return logging.getLogger("test")


# ─────────────────────────────────────────────────────────────────────────────
# Config tests
# ─────────────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_defaults_parsed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.cleanup_day == 1
        assert cfg.purge_day == 7
        assert cfg.run_hour == 5
        assert cfg.min_upload_age_days == 7
        assert cfg.min_since_print_days == 7

    def test_custom_values(self, tmp_path: Path) -> None:
        cfg = make_config(
            tmp_path,
            extra="cleanup_day: 15\npurge_day: 22\nrun_hour: 3\nmin_upload_age_days: 14",
        )
        assert cfg.cleanup_day == 15
        assert cfg.purge_day == 22
        assert cfg.run_hour == 3
        assert cfg.min_upload_age_days == 14

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            Config(tmp_path / "nonexistent.cfg")

    def test_ntfy_disabled_by_default(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.ntfy_enabled is False

    def test_fluidd_enabled_by_default(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.fluidd_notifications is True


class TestResolveConfig:
    def test_override_path_used(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "custom.cfg"
        cfg_file.write_text("[gcode_cleanup]\n")
        result = resolve_config(str(cfg_file))
        assert result == cfg_file

    def test_missing_override_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_config(str(tmp_path / "missing.cfg"))


# ─────────────────────────────────────────────────────────────────────────────
# Retention logic tests
# ─────────────────────────────────────────────────────────────────────────────


class TestKeepReason:
    def setup_method(self) -> None:
        pass

    def _make_job(self, cfg: Config) -> CleanupJob:
        return CleanupJob(cfg, null_client(), null_notifier(), null_logger())

    def test_recent_upload_is_kept(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        f = make_gcode(tmp_path / "gcodes" / "fresh.gcode", age_days=3)
        job = self._make_job(cfg)
        now = time.time()
        cutoff = now - 7 * 86_400
        reason = job._keep_reason(f, "fresh.gcode", cutoff, cutoff, {})
        assert reason is not None
        assert "uploaded" in reason

    def test_old_upload_no_history_is_trashed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        f = make_gcode(tmp_path / "gcodes" / "old.gcode", age_days=30)
        job = self._make_job(cfg)
        now = time.time()
        cutoff = now - 7 * 86_400
        reason = job._keep_reason(f, "old.gcode", cutoff, cutoff, {})
        assert reason is None

    def test_old_upload_recent_print_is_kept(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        f = make_gcode(tmp_path / "gcodes" / "old_but_printed.gcode", age_days=30)
        job = self._make_job(cfg)
        now = time.time()
        cutoff = now - 7 * 86_400
        recent_prints = {"old_but_printed.gcode": now - 1 * 86_400}  # printed yesterday
        reason = job._keep_reason(f, "old_but_printed.gcode", cutoff, cutoff, recent_prints)
        assert reason is not None
        assert "printed" in reason

    def test_old_upload_old_print_is_trashed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        f = make_gcode(tmp_path / "gcodes" / "old_old.gcode", age_days=30)
        job = self._make_job(cfg)
        now = time.time()
        cutoff = now - 7 * 86_400
        recent_prints = {"old_old.gcode": now - 20 * 86_400}  # printed 20 days ago
        reason = job._keep_reason(f, "old_old.gcode", cutoff, cutoff, recent_prints)
        assert reason is None

    def test_boundary_exactly_at_cutoff_is_trashed(self, tmp_path: Path) -> None:
        """A file whose mtime equals the cutoff exactly is NOT within the window."""
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        f = make_gcode(tmp_path / "gcodes" / "boundary.gcode", age_days=7)
        job = self._make_job(cfg)
        now = time.time()
        cutoff = now - 7 * 86_400
        # mtime ≈ cutoff (within a second of test execution — mtime <= cutoff)
        reason = job._keep_reason(f, "boundary.gcode", cutoff, cutoff, {})
        # At exactly the boundary the file is stale — no reason to keep
        assert reason is None


# ─────────────────────────────────────────────────────────────────────────────
# File discovery tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDiscoverFiles:
    def test_finds_gcode_extensions(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        for ext in GCODE_EXTENSIONS:
            (gcodes / f"file{ext}").parent.mkdir(parents=True, exist_ok=True)
            (gcodes / f"file{ext}").write_text("; test")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        found = {f.suffix.lower() for f in job._discover_files()}
        assert found == GCODE_EXTENSIONS

    def test_skips_skip_dirs(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        for skip in SKIP_DIRS:
            d = gcodes / skip
            d.mkdir(parents=True, exist_ok=True)
            (d / "should_be_skipped.gcode").write_text("; skip")
        (gcodes / "included.gcode").write_text("; include")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        files = job._discover_files()
        names = [f.name for f in files]
        assert "should_be_skipped.gcode" not in names
        assert "included.gcode" in names

    def test_finds_files_in_subdirectories(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        sub = tmp_path / "gcodes" / "project"
        sub.mkdir(parents=True)
        (sub / "nested.gcode").write_text("; nested")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        files = job._discover_files()
        assert any(f.name == "nested.gcode" for f in files)


# ─────────────────────────────────────────────────────────────────────────────
# Move to trash tests
# ─────────────────────────────────────────────────────────────────────────────


class TestMoveToTrash:
    def test_file_moved_to_trash(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        gcodes.mkdir()
        trash = tmp_path / "trash"
        trash.mkdir()
        f = gcodes / "stale.gcode"
        f.write_text("; stale")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        job._move_to_trash(f, "stale.gcode", time.time())
        assert not f.exists()
        assert (trash / "stale.gcode").exists()

    def test_dry_run_does_not_move(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        gcodes.mkdir()
        f = gcodes / "stale.gcode"
        f.write_text("; stale")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger(), dry_run=True)
        job._move_to_trash(f, "stale.gcode", time.time())
        assert f.exists()  # not moved

    def test_collision_handled_with_timestamp_suffix(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        gcodes.mkdir()
        trash = tmp_path / "trash"
        trash.mkdir()
        # Pre-populate trash with a same-named file
        (trash / "stale.gcode").write_text("; existing")
        f = gcodes / "stale.gcode"
        f.write_text("; new")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        job._move_to_trash(f, "stale.gcode", 1234567890)
        assert (trash / "stale__1234567890.gcode").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Empty directory removal tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoveEmptyDirs:
    def test_empty_subdir_removed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        empty_sub = gcodes / "empty_project"
        empty_sub.mkdir(parents=True)
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        removed = job._remove_empty_dirs()
        assert removed == 1
        assert not empty_sub.exists()

    def test_non_empty_subdir_kept(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        sub = gcodes / "active_project"
        sub.mkdir(parents=True)
        (sub / "active.gcode").write_text("; still here")
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        removed = job._remove_empty_dirs()
        assert removed == 0
        assert sub.exists()

    def test_gcodes_root_never_removed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        job._remove_empty_dirs()
        assert (tmp_path / "gcodes").exists()

    def test_top_level_thumbs_not_removed(self, tmp_path: Path) -> None:
        """The top-level gcodes/.thumbs is always preserved (Moonraker owns it)."""
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        thumbs = gcodes / ".thumbs"
        thumbs.mkdir(parents=True, exist_ok=True)
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        job._remove_empty_dirs()
        assert thumbs.exists()

    def test_nested_thumbs_removed_when_empty(self, tmp_path: Path) -> None:
        """A .thumbs inside a project subdir is removed when empty, freeing the parent."""
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        nested_thumbs = gcodes / "project" / ".thumbs"
        nested_thumbs.mkdir(parents=True, exist_ok=True)
        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        removed = job._remove_empty_dirs()
        assert removed == 2  # .thumbs + project dir
        assert not (gcodes / "project").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup integration tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCleanupRun:
    def test_stale_files_moved_fresh_kept(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        (tmp_path / "trash").mkdir()
        stale = make_gcode(gcodes / "stale.gcode", age_days=30)
        fresh = make_gcode(gcodes / "fresh.gcode", age_days=2)

        client = null_client()
        job = CleanupJob(cfg, client, null_notifier(), null_logger())
        job.run()

        assert not stale.exists()
        assert fresh.exists()
        assert (tmp_path / "trash" / "stale.gcode").exists()

    def test_aborts_when_printing(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir()
        (tmp_path / "trash").mkdir()
        stale = make_gcode(tmp_path / "gcodes" / "stale.gcode", age_days=30)

        client = null_client()
        client.printer_state.return_value = "printing"
        notifier = null_notifier()
        job = CleanupJob(cfg, client, notifier, null_logger())
        job.run()

        assert stale.exists()  # untouched
        notifier.send.assert_called_once()
        assert "ABORTED" in notifier.send.call_args[0][0]

    def test_empty_subdir_cleaned_after_trash(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        gcodes = tmp_path / "gcodes"
        (tmp_path / "trash").mkdir()
        sub = gcodes / "project"
        make_gcode(sub / "only_file.gcode", age_days=30)

        job = CleanupJob(cfg, null_client(), null_notifier(), null_logger())
        job.run()

        assert not (sub / "only_file.gcode").exists()
        assert not sub.exists()  # empty dir removed


# ─────────────────────────────────────────────────────────────────────────────
# Purge tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPurgeRun:
    def test_files_deleted_manifests_preserved(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        trash = tmp_path / "trash"
        trash.mkdir()
        (trash / "old_file.gcode").write_text("; trash")
        (trash / "manifest_2026-01-01.json").write_text("{}")

        job = PurgeJob(cfg, null_notifier(), null_logger())
        job.run()

        assert not (trash / "old_file.gcode").exists()
        assert (trash / "manifest_2026-01-01.json").exists()

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        trash = tmp_path / "trash"
        trash.mkdir()
        f = trash / "old_file.gcode"
        f.write_text("; trash")

        job = PurgeJob(cfg, null_notifier(), null_logger(), dry_run=True)
        job.run()

        assert f.exists()

    def test_missing_trash_dir_is_noop(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        # trash dir does NOT exist
        job = PurgeJob(cfg, null_notifier(), null_logger())
        job.run()  # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# Auto-mode dispatch tests
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoMode:
    """Tests for the day/hour schedule dispatch in main()."""

    def _run_auto(self, tmp_path: Path, day: int, hour: int) -> tuple[bool, bool]:
        """
        Run main() in auto mode with the clock mocked to (day, hour).
        Returns (cleanup_ran, purge_ran).
        """
        from unittest.mock import patch

        cfg = make_config(tmp_path)
        (tmp_path / "gcodes").mkdir(exist_ok=True)
        (tmp_path / "trash").mkdir(exist_ok=True)

        cleanup_ran = False
        purge_ran = False

        class _FakeCleanup:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                nonlocal cleanup_ran
                cleanup_ran = True

        class _FakePurge:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                nonlocal purge_ran
                purge_ran = True

        fake_now = MagicMock()
        fake_now.hour = hour
        fake_now.day = day

        with (
            patch("cleanup.resolve_config", return_value=cfg._path),
            patch("cleanup.Config", return_value=cfg),
            patch("cleanup.setup_logging", return_value=null_logger()),
            patch("cleanup.MoonrakerClient"),
            patch("cleanup.Notifier"),
            patch("cleanup.CleanupJob", _FakeCleanup),
            patch("cleanup.PurgeJob", _FakePurge),
            patch("cleanup.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp

            import argparse

            import cleanup as mod

            with patch(
                "argparse.ArgumentParser.parse_args",
                return_value=argparse.Namespace(
                    config=str(cfg._path), cleanup=False, purge=False, dry_run=False
                ),
            ):
                mod.main()

        return cleanup_ran, purge_ran

    def test_cleanup_runs_on_correct_day_and_hour(self, tmp_path: Path) -> None:
        cleanup_ran, purge_ran = self._run_auto(tmp_path, day=1, hour=5)
        assert cleanup_ran
        assert not purge_ran

    def test_purge_runs_on_correct_day_and_hour(self, tmp_path: Path) -> None:
        cleanup_ran, purge_ran = self._run_auto(tmp_path, day=7, hour=5)
        assert not cleanup_ran
        assert purge_ran

    def test_nothing_runs_on_wrong_hour(self, tmp_path: Path) -> None:
        cleanup_ran, purge_ran = self._run_auto(tmp_path, day=1, hour=12)
        assert not cleanup_ran
        assert not purge_ran

    def test_nothing_runs_on_unscheduled_day(self, tmp_path: Path) -> None:
        cleanup_ran, purge_ran = self._run_auto(tmp_path, day=15, hour=5)
        assert not cleanup_ran
        assert not purge_ran


# ─────────────────────────────────────────────────────────────────────────────
# Notifier tests
# ─────────────────────────────────────────────────────────────────────────────


class TestNotifier:
    def _make_notifier(self, tmp_path: Path, extra_cfg: str = "") -> tuple[Notifier, MagicMock]:
        cfg = make_config(tmp_path, extra=extra_cfg)
        client = null_client()
        notifier = Notifier(cfg, client, null_logger())
        return notifier, client

    def test_ntfy_not_called_when_disabled(self, tmp_path: Path) -> None:
        notifier, _ = self._make_notifier(tmp_path, "ntfy_enabled: false")
        with patch("requests.post") as mock_post:
            notifier.send("test")
        mock_post.assert_not_called()

    def test_ntfy_called_when_enabled(self, tmp_path: Path) -> None:
        notifier, _ = self._make_notifier(tmp_path, "ntfy_enabled: true\nntfy_topic: klipper-test")
        with patch("requests.post") as mock_post:
            notifier.send("test message")
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "klipper-test" in url

    def test_ntfy_skips_when_topic_missing(self, tmp_path: Path) -> None:
        notifier, _ = self._make_notifier(tmp_path, "ntfy_enabled: true\nntfy_topic:")
        with patch("requests.post") as mock_post:
            notifier.send("test")
        mock_post.assert_not_called()

    def test_fluidd_sends_gcode(self, tmp_path: Path) -> None:
        notifier, client = self._make_notifier(tmp_path, "fluidd_notifications: true")
        notifier.send("hello")
        client.send_gcode.assert_called_once()
        script = client.send_gcode.call_args[0][0]
        assert "hello" in script
        assert "RESPOND" in script


# ── CI smoke test ─────────────────────────────────────────────────────────────


def test_version_string_is_semver() -> None:
    """VERSION follows semantic versioning (MAJOR.MINOR.PATCH)."""
    import re

    from cleanup import VERSION

    assert re.match(r"^\d+\.\d+\.\d+$", VERSION), f"Bad version format: {VERSION}"

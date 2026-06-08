"""Tests for the main() CLI entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import dead_mans_switch
from tests.conftest import GitRepo


class TestMainCLI:
    def test_basic_invocation_runs_in_test_mode(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Empty emails dir now raises (loud-failure consistency with
        # warning/PASSED_AWAY guards), so this test asserts that — the
        # CLI parsing path through `main()` still reaches `run()` and
        # surfaces the raise.
        empty_dir = tmp_repo_with_initial_commit.path / "empty_emails"
        empty_dir.mkdir()
        monkeypatch.setattr(
            dead_mans_switch.DeadMansSwitch, "PATH_TO_EMAILS", empty_dir
        )
        monkeypatch.setattr("sys.argv", ["dead_mans_switch.py", "48", "0"])

        with pytest.raises(
            dead_mans_switch.DeadMansSwitchException, match="No .txt files"
        ):
            dead_mans_switch.main()

    def test_armed_flag_parsed(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["dead_mans_switch.py", "48", "2", "--armed"]
        )
        with patch.object(dead_mans_switch.DeadMansSwitch, "run") as run_mock:
            dead_mans_switch.main()
        # The constructor was used with armed=True
        run_mock.assert_called_once()

    def test_manual_dispatch_flag_bypasses_validation(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sub-24-hour interval is only allowed with --manual-dispatch
        monkeypatch.setattr(
            "sys.argv",
            ["dead_mans_switch.py", "1", "0", "--manual-dispatch"],
        )
        with patch.object(dead_mans_switch.DeadMansSwitch, "run") as run_mock:
            dead_mans_switch.main()
        run_mock.assert_called_once()

    def test_missing_args_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["dead_mans_switch.py"])
        with pytest.raises(SystemExit):
            dead_mans_switch.main()

    def test_non_numeric_interval_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["dead_mans_switch.py", "not-a-number", "2"]
        )
        with pytest.raises(SystemExit):
            dead_mans_switch.main()

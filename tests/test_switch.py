"""Tests for the DeadMansSwitch state machine and run() dispatch."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dead_mans_switch import (
    Commit,
    DeadMansSwitch,
    DeadMansSwitchException,
    Email,
    State,
)
from tests.conftest import FakeSMTPState, GitRepo, hours_ago


def _disable_pushes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub `git push` so tests can run without a real remote."""
    pushed: list[list[str]] = []
    original_run = subprocess.run

    def spy(args, *a, **kw):
        if list(args[:2]) == ["git", "push"]:
            pushed.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    return pushed


class TestConstructorValidation:
    def test_valid_init(self, tmp_repo_with_initial_commit: GitRepo) -> None:
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._heartbeat_interval_hours == 48
        assert dms._number_of_warnings == 2
        assert dms._armed is False
        assert dms._manual_dispatch is False

    def test_float_interval_allowed(self, tmp_repo_with_initial_commit: GitRepo) -> None:
        dms = DeadMansSwitch(48.5, 2, armed=False, manual_dispatch=False)
        assert dms._heartbeat_interval_hours == 48.5

    def test_interval_below_minimum_raises(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(DeadMansSwitchException, match="must be at least"):
            DeadMansSwitch(12, 0, armed=False, manual_dispatch=False)

    def test_manual_dispatch_bypasses_minimum(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        # Should NOT raise even with interval < 24
        DeadMansSwitch(1, 0, armed=False, manual_dispatch=True)

    def test_negative_warnings_raises(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(DeadMansSwitchException, match="greater than or equal to 0"):
            DeadMansSwitch(48, -1, armed=False, manual_dispatch=False)

    def test_non_int_warnings_raises(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(DeadMansSwitchException, match="greater than or equal to 0"):
            DeadMansSwitch(48, 2.5, armed=False, manual_dispatch=False)  # type: ignore[arg-type]

    def test_non_bool_armed_raises(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(DeadMansSwitchException, match="Armed must be a boolean"):
            DeadMansSwitch(48, 0, armed="true", manual_dispatch=False)  # type: ignore[arg-type]

    def test_non_bool_manual_dispatch_raises(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(
            DeadMansSwitchException, match="Manual dispatch must be a boolean"
        ):
            DeadMansSwitch(48, 0, armed=False, manual_dispatch="false")  # type: ignore[arg-type]


class TestRemainingWarnings:
    def test_no_warnings_yet(self, tmp_repo_with_initial_commit: GitRepo) -> None:
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 2

    def test_one_prior_warning(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("initial")
        tmp_repo.commit("warning issued", author_name="dms_bot")
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 1

    def test_owner_commit_breaks_history_scan(self, tmp_repo: GitRepo) -> None:
        # Warnings before this owner commit should not count.
        tmp_repo.commit("warning issued", author_name="dms_bot")
        tmp_repo.commit("heartbeat", author_name="oedokumaci")
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 2

    def test_passed_away_commit_means_already_declared_dead(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit("initial")
        tmp_repo.commit("passed away", author_name="dms_bot")
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        # Sentinel: more than configured warnings means "already declared dead"
        assert dms._remaining_warnings == 3

    def test_non_bot_commit_treated_as_heartbeat(self, tmp_repo: GitRepo) -> None:
        """Anything not authored by dms_bot is treated as a heartbeat."""
        tmp_repo.commit("hi from collaborator", author_name="random_dev")
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 2  # heartbeat resets count

    def test_short_history_breaks_loop(self, tmp_repo: GitRepo) -> None:
        # Only one warning commit total, but configured for 2 warnings.
        # Loop should break gracefully on the second iteration.
        tmp_repo.commit("warning issued", author_name="dms_bot")
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 1


class TestGetState:
    def test_disarmed_when_not_armed(
        self, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._get_state() == State.DISARMED

    def test_alive_when_recent_commit(self, tmp_repo_with_initial_commit: GitRepo) -> None:
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() == State.ALIVE

    def test_issue_warning_when_inactive(self, tmp_repo: GitRepo) -> None:
        # Single old owner commit, no prior warnings.
        tmp_repo.commit("old", when=hours_ago(100))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() == State.ISSUE_WARNING

    def test_passed_away_when_no_warnings_left(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("ancient", when=hours_ago(1000))
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=False)
        # Latest commit is the owner's old one, no warnings configured, time exceeded.
        assert dms._get_state() == State.PASSED_AWAY

    def test_already_declared_dead(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("ancient", when=hours_ago(1000))
        tmp_repo.commit(
            "passed away",
            author_name="dms_bot",
            when=hours_ago(900),
        )
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() == State.ALREADY_DECLARED_DEAD


class TestGatherEmails:
    def test_only_txt_files_picked_up(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
    ) -> None:
        (emails_dir / "real.txt").write_text("To: a@b.com\nSubject: s\n\nbody")
        (emails_dir / "skip.txt.template").write_text("To: a@b.com\nSubject: s\n\nbody")
        (emails_dir / "skip.md").write_text("# notes")
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        emails = dms._gather_emails()
        assert len(emails) == 1

    def test_empty_directory_returns_empty_list(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
    ) -> None:
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert dms._gather_emails() == []


class TestRunDisarmed:
    def test_disarmed_sends_test_emails_to_owner(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        (emails_dir / "gf.txt").write_text(
            "To: gf@example.com\nSubject: hi\n\nLove you"
        )
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        dms.run()
        assert len(fake_smtp.sent) == 1
        assert fake_smtp.sent[0].to_addr == "me@gmail.com"
        assert "Test Email" in fake_smtp.sent[0].subject
        assert "test email" in fake_smtp.sent[0].body.lower()

    def test_disarmed_with_no_emails_raises(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
    ) -> None:
        """Disarmed empty-dir now raises to match the warning/PASSED_AWAY
        guards. README step 5b (manual armed=false dispatch to verify
        setup) otherwise shows a green workflow with no emails — exactly
        mimicking successful test mode while no recipients were actually
        configured."""
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        with pytest.raises(DeadMansSwitchException, match="No .txt files"):
            dms.run()


class TestRunAlive:
    def test_alive_does_nothing(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        dms.run()
        assert fake_smtp.sent == []
        assert "No action needed" in capsys.readouterr().out


class TestRunIssueWarning:
    def test_issue_warning_creates_commit(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The warning handler now pre-validates EmailServer credentials
        # AND parses templates before writing the warning commit.
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("old", when=hours_ago(100))
        # write_to_repo's env-var override forces the author to dms_bot
        # regardless of the local git config — the test below verifies both
        # the message AND the author.
        (emails_dir / "rcpt.txt").write_text("To: rcpt@example.com\nSubject: s\n\nbody")
        _disable_pushes(monkeypatch)
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        dms.run()
        # No emails sent
        assert fake_smtp.sent == []
        # A new "warning issued" commit was added by dms_bot.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s|%an"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        subject, author = log.stdout.split("|")
        assert subject == "warning issued"
        assert author == "dms_bot"


class TestRunPassedAway:
    def test_passed_away_sends_to_real_recipients(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "gf.txt").write_text(
            "To: gf@example.com\nSubject: hi\n\nBye"
        )
        _disable_pushes(monkeypatch)
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=False)
        dms.run()
        assert len(fake_smtp.sent) == 1
        assert fake_smtp.sent[0].to_addr == "gf@example.com"
        # Subject untouched (no "Manually Triggered" prefix)
        assert fake_smtp.sent[0].subject == "hi"

    def test_manual_dispatch_reroutes_to_owner(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "gf.txt").write_text(
            "To: gf@example.com\nSubject: hi\n\nBye"
        )
        _disable_pushes(monkeypatch)
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=True)
        dms.run()
        assert len(fake_smtp.sent) == 1
        assert fake_smtp.sent[0].to_addr == "me@gmail.com"
        assert "Manually Triggered" in fake_smtp.sent[0].subject


class TestRunAlreadyDeclaredDead:
    def test_already_dead_does_nothing(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp: FakeSMTPState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tmp_repo.commit("ancient", when=hours_ago(1000))
        tmp_repo.commit("passed away", author_name="dms_bot", when=hours_ago(900))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        dms.run()
        assert fake_smtp.sent == []
        assert "already declared dead" in capsys.readouterr().out


class TestExceptionType:
    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(DeadMansSwitchException, match="boom"):
            raise DeadMansSwitchException("boom")

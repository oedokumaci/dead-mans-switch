"""Tests for the Commit dataclass and its git operations."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dead_mans_switch import BOT_EMAIL, BOT_USERNAME, Commit, DeadMansSwitchException
from tests.conftest import GitRepo, hours_ago


class TestCommitDataclass:
    def test_fields(self) -> None:
        when = datetime.now(timezone.utc)
        c = Commit(message="hi", user="alice", email="alice@e.com", timestamp=when)
        assert c.message == "hi"
        assert c.user == "alice"
        assert c.email == "alice@e.com"
        assert c.timestamp is when

    def test_equality(self) -> None:
        when = datetime.now(timezone.utc)
        assert (
            Commit("m", "u", "u@e.com", when)
            == Commit("m", "u", "u@e.com", when)
        )


class TestHoursSince:
    def test_recent_commit_has_small_value(self) -> None:
        c = Commit("m", "u", "u@e.com", datetime.now(timezone.utc))
        assert c.hours_since < 0.1

    def test_2_5_hours_ago(self) -> None:
        c = Commit("m", "u", "u@e.com", hours_ago(2.5))
        assert 2.4 < c.hours_since < 2.6

    def test_negative_when_future(self) -> None:
        c = Commit("m", "u", "u@e.com", hours_ago(-1))
        assert c.hours_since < 0


class TestFromLastCommit:
    def test_returns_latest_commit(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("first commit")
        c = Commit.from_last_commit()
        assert c.message == "first commit"
        assert c.user == tmp_repo.owner_name
        assert c.timestamp.tzinfo is not None

    def test_returns_utc_timezone(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("a")
        c = Commit.from_last_commit()
        # %cI gives local timezone; from_last_commit converts to UTC.
        assert c.timestamp.utcoffset().total_seconds() == 0

    def test_skip_returns_older_commit(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("first")
        tmp_repo.commit("second")
        tmp_repo.commit("third")
        assert Commit.from_last_commit(0).message == "third"
        assert Commit.from_last_commit(1).message == "second"
        assert Commit.from_last_commit(2).message == "first"

    def test_raises_when_no_commits(self, tmp_repo: GitRepo) -> None:
        # Empty repo: git log exits non-zero. The exact error string depends
        # on the git version, so accept either "git log failed" (CalledProcessError
        # path) or "No commit at skip" (empty-stdout path).
        with pytest.raises(
            DeadMansSwitchException, match="git log failed|No commit at skip"
        ):
            Commit.from_last_commit()

    def test_raises_outside_git_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(DeadMansSwitchException):
            Commit.from_last_commit()


class TestIsByDmsBot:
    def test_true_for_bot_user_and_email(self) -> None:
        c = Commit("m", BOT_USERNAME, BOT_EMAIL, datetime.now(timezone.utc))
        assert c.is_by_dms_bot() is True

    def test_false_for_anyone_else(self) -> None:
        # Name matches but email differs (the round-7 spoofing-defense case)
        c = Commit("m", BOT_USERNAME, "evil@elsewhere.com", datetime.now(timezone.utc))
        assert c.is_by_dms_bot() is False
        # Email matches but name differs (also rejected)
        c = Commit("m", "impostor", BOT_EMAIL, datetime.now(timezone.utc))
        assert c.is_by_dms_bot() is False
        # Neither matches
        for name in ("Onurcan Edokumaci", "oedokumaci", "random", ""):
            c = Commit("m", name, "x@y.com", datetime.now(timezone.utc))
            assert c.is_by_dms_bot() is False


class TestWriteToRepo:
    def test_creates_empty_commit_with_bot_identity_and_pushes(
        self, tmp_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_repo.commit("initial")
        push_calls: list[list[str]] = []
        original_run = subprocess.run

        def spy_run(args, *a, **kw):
            if list(args[:2]) == ["git", "push"]:
                push_calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spy_run)

        Commit(
            message="warning issued",
            user=BOT_USERNAME,
            email=BOT_EMAIL,
            timestamp=datetime.now(timezone.utc),
        ).write_to_repo()

        # Bot identity is applied via env vars, not local git config, so the
        # actual commit author should be `dms_bot` even though the repo's
        # local user.name is the owner.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s|%an|%ae"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        subject, author, email = log.stdout.split("|")
        assert subject == "warning issued"
        assert author == BOT_USERNAME
        assert email == BOT_EMAIL
        assert push_calls == [["git", "push"]]

    def test_push_failure_rolls_back_local_commit(
        self, tmp_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_repo.commit("initial")
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        original_run = subprocess.run

        def spy_run(args, *a, **kw):
            if list(args[:2]) == ["git", "push"]:
                # Simulate the failure that subprocess.run(check=True) would
                # produce when push fails.
                raise subprocess.CalledProcessError(1, list(args), "", "fake push failure")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spy_run)

        with pytest.raises(DeadMansSwitchException, match="push failed"):
            Commit(
                message="warning issued",
                user=BOT_USERNAME,
                email=BOT_EMAIL,
                timestamp=datetime.now(timezone.utc),
            ).write_to_repo()

        # The local commit should have been rolled back.
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert before == after, "push failure must rollback the local commit"

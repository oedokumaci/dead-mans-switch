"""Targeted tests to cover error paths and edge branches that are awkward
to reach from natural usage. Each test cites the specific line(s) it
covers.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dead_mans_switch import (
    BOT_EMAIL,
    BOT_USERNAME,
    Commit,
    DeadMansSwitch,
    DeadMansSwitchException,
    Email,
    State,
)
from tests.conftest import GitRepo, hours_ago


class TestCommitParseErrors:
    """Cover the error-handling lines in `Commit.from_last_commit`."""

    def test_bad_timestamp_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Covers the ValueError-on-fromisoformat path."""
        def fake(args, *a, **kw):
            # Format: %s<SEP>%an<SEP>%ae<SEP>%cI
            return "msg\x1fauthor\x1fauthor@e.com\x1fnot-a-timestamp"

        monkeypatch.setattr(subprocess, "check_output", fake)
        with pytest.raises(DeadMansSwitchException, match="Bad timestamp"):
            Commit.from_last_commit(0)


class TestCommitFailure:
    """Cover the `git commit failed` error path in `write_to_repo`."""

    def test_commit_failure_raises(
        self, tmp_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers line 150-151."""
        original = subprocess.run

        def spy(args, *a, **kw):
            if list(args[:2]) == ["git", "commit"]:
                raise subprocess.CalledProcessError(1, list(args), "", "fake")
            return original(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spy)
        with pytest.raises(DeadMansSwitchException, match="commit failed"):
            Commit(
                "warning issued",
                BOT_USERNAME,
                BOT_EMAIL,
                datetime.now(timezone.utc),
            ).write_to_repo()


class TestEmailParseErrors:
    """Cover error branches in `Email.from_txt`."""

    def test_os_error_on_read_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers line 209-210 (OSError other than FileNotFoundError)."""
        p = tmp_path / "x.txt"
        p.write_text("To: a@b.com\nSubject: s\n\nbody")

        def boom(*a, **kw):
            raise PermissionError("simulated")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(DeadMansSwitchException, match="Cannot read"):
            Email.from_txt(p)

    def test_malformed_header_raises(self, tmp_path: Path) -> None:
        """Covers line 224-227 (no ':' in a header line)."""
        p = tmp_path / "x.txt"
        p.write_text("To: a@b.com\nthis-line-has-no-colon\nSubject: s\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="Malformed header"):
            Email.from_txt(p)

    def test_duplicate_header_raises(self, tmp_path: Path) -> None:
        """Covers line 230-233 (same header twice)."""
        p = tmp_path / "x.txt"
        p.write_text("To: a@b.com\nTo: c@d.com\nSubject: s\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="Duplicate"):
            Email.from_txt(p)

    def test_invalid_placeholder_syntax_is_left_literal(
        self, tmp_path: Path
    ) -> None:
        """The custom substitution recognises only `${IDENT}`. Anything that
        doesn't match (e.g. `${with spaces}`, bare `$5`, `$HOME`) is left as
        literal text — by design. Old behavior raised; new behavior is more
        forgiving so real email bodies don't break."""
        p = tmp_path / "x.txt"
        p.write_text(
            "To: a@b.com\nSubject: s\n\n"
            "body ${invalid syntax} costs $5 at $HOME"
        )
        email = Email.from_txt(p)
        assert email.body == "body ${invalid syntax} costs $5 at $HOME"


class TestRemainingWarningsBranches:
    """Cover the remaining branch in `_get_remaining_warnings`."""

    def test_partial_warning_history_then_owner(self, tmp_repo: GitRepo) -> None:
        """Two warnings + heartbeat: walks through both warnings, decrements
        twice, then breaks on the owner commit. Covers the loop-continues
        branch after `remaining -= 1` (466->450)."""
        tmp_repo.commit("heartbeat", when=hours_ago(200))
        tmp_repo.commit(
            "warning issued",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
            when=hours_ago(150),
        )
        tmp_repo.commit(
            "warning issued",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
            when=hours_ago(100),
        )
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 0

    def test_passed_away_check_handles_git_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If `git log` fails (no repo, etc.), `_passed_away_already_committed`
        returns False rather than crashing — the empty-history case is handled
        by the loop below."""

        def fake_check_output(args, *a, **kw):
            raise subprocess.CalledProcessError(128, list(args), "", "no repo")

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)
        assert DeadMansSwitch._passed_away_already_committed() is False

    def test_unknown_bot_message_is_ignored(self, tmp_repo: GitRepo) -> None:
        """Bot commit with neither warning nor passed-away message — loop
        continues to the next commit (covers 466->450)."""
        tmp_repo.commit("heartbeat")
        tmp_repo.commit(
            "some unknown bot action",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
        )
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 2  # no warnings counted, heartbeat reached

    def test_only_warnings_no_heartbeat_exits_loop_normally(
        self, tmp_repo: GitRepo
    ) -> None:
        """All N+1 commits are warnings (no PASSED_AWAY, no heartbeat).
        Loop runs all iterations, no break or return — covers 450->470."""
        for hours in (300, 200, 100):
            tmp_repo.commit(
                "warning issued",
                author_name=BOT_USERNAME,
                author_email="dms@bot.github.com",
                when=hours_ago(hours),
            )
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        # range(3): each iteration decrements remaining (2 → 1 → 0 → -1),
        # then max(-1, 0) clamps to 0.
        assert dms._remaining_warnings == 0


class TestRemoteHasCommit:
    """Real integration tests for `_remote_has_commit`. The previous
    `origin/HEAD`-based implementation passed monkey-patched tests but
    failed in actual GitHub Actions because `actions/checkout@v4` doesn't
    always set the symbolic ref. These tests use real bare + clone repos
    to make sure the function behaves correctly without monkey-patching
    its internals."""

    def test_returns_false_on_bad_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`git branch -r --contains <bad-sha>` exits non-zero. The helper
        must return False rather than treating it as success."""
        from dead_mans_switch import _remote_has_commit

        bare = tmp_path / "origin.bare"
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True, capture_output=True,
        )
        clone.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(clone)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare)],
            cwd=clone, check=True, capture_output=True,
        )
        monkeypatch.chdir(clone)
        # Nonexistent sha → git branch returns non-zero.
        assert _remote_has_commit("0" * 40) is False

    def test_returns_false_on_git_fetch_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If `git fetch` errors out (offline, bad creds), the helper
        falls back to False so the caller treats the push as truly failed
        rather than spuriously claiming success."""
        from dead_mans_switch import _remote_has_commit

        original_run = subprocess.run

        def fake(args, *a, **kw):
            if list(args[:2]) == ["git", "fetch"]:
                raise subprocess.CalledProcessError(1, list(args))
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", fake)
        assert _remote_has_commit("deadbeef") is False

    def test_returns_false_for_unpushed_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a commit that exists locally but hasn't been pushed
        to any remote-tracking ref must return False. No origin/HEAD
        dependency."""
        from dead_mans_switch import _remote_has_commit

        bare = tmp_path / "origin.bare"
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(bare), str(clone)],
            check=True, capture_output=True,
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "owner",
            "GIT_AUTHOR_EMAIL": "owner@example.com",
            "GIT_COMMITTER_NAME": "owner",
            "GIT_COMMITTER_EMAIL": "owner@example.com",
        }
        # An initial commit so HEAD exists.
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "initial"],
            cwd=clone, check=True, env=env, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=clone, check=True, capture_output=True,
        )
        # NEW unpushed local commit.
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "local-only"],
            cwd=clone, check=True, env=env, capture_output=True,
        )
        local_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=clone, text=True
        ).strip()

        monkeypatch.chdir(clone)
        assert _remote_has_commit(local_sha) is False

    def test_returns_true_after_successful_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: after a successful push, the helper must find the
        commit on the remote. The old implementation broke here when
        `origin/HEAD` was not a symbolic ref — this test exercises the
        same condition (`git clone` does set origin/HEAD, but the test
        below uses `init`+`remote add` which doesn't, mimicking
        actions/checkout)."""
        from dead_mans_switch import _remote_has_commit

        bare = tmp_path / "origin.bare"
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True, capture_output=True,
        )
        # Use init + remote add (NOT clone) — this matches the way
        # actions/checkout@v4 sets up the runner's working tree, where
        # `refs/remotes/origin/HEAD` is NOT created.
        clone.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(clone)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare)],
            cwd=clone, check=True, capture_output=True,
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "owner",
            "GIT_AUTHOR_EMAIL": "owner@example.com",
            "GIT_COMMITTER_NAME": "owner",
            "GIT_COMMITTER_EMAIL": "owner@example.com",
        }
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "first"],
            cwd=clone, check=True, env=env, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=clone, check=True, capture_output=True,
        )
        local_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=clone, text=True
        ).strip()

        # Confirm there's no symbolic origin/HEAD (the situation the old
        # implementation couldn't handle).
        ref_check = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/HEAD"],
            cwd=clone, capture_output=True,
        )
        assert ref_check.returncode != 0, (
            "Test invariant broken: this setup is supposed to NOT create "
            "origin/HEAD, but it did."
        )

        monkeypatch.chdir(clone)
        assert _remote_has_commit(local_sha) is True


class TestManualDispatchSuccessOutput:
    """Cover the `print(...)` branch in `_handle_issue_warning` after a
    successful pre-flight when manual_dispatch is True. The empty-dir
    guard + template parse run; then the function prints success and
    short-circuits."""

    def test_manual_warning_pre_flight_prints_success(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("old", when=hours_ago(100))
        (emails_dir / "x.txt").write_text(
            "To: rcpt@example.com\nSubject: s\n\nbody"
        )
        DeadMansSwitch(48, 2, armed=True, manual_dispatch=True).run()
        out = capsys.readouterr().out.lower()
        assert "would issue warning" in out
        assert "parsed successfully" in out


class TestUnhandledStateRaises:
    """Cover the `case _ as unhandled` arm — defends against a future State
    enum value being added without an explicit case."""

    def test_unknown_state_raises_assertion_error(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (emails_dir / "a.txt").write_text(
            "To: a@b.com\nSubject: s\n\nbody"
        )
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        monkeypatch.setattr(dms, "_get_state", lambda: "fake-future-state")
        with pytest.raises(AssertionError, match="unhandled state"):
            dms.run()


class TestEmailHeaderEdges:
    def test_no_blank_line_between_headers_and_eof(self, tmp_path: Path) -> None:
        """File has only headers and no body section — loop completes without
        hitting the blank-line break (covers 220->236)."""
        p = tmp_path / "x.txt"
        p.write_text("To: a@b.com\nSubject: just headers")
        email = Email.from_txt(p)
        assert email.to == "a@b.com"
        assert email.subject == "just headers"
        assert email.body == ""

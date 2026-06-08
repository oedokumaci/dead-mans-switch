"""Regression tests for the bugs we found and fixed.

These were originally bug reproducers (failing tests that documented broken
behavior). After the fixes, they're regression tests — they should stay
green. Marked `@pytest.mark.bug` so they can still be located easily.
"""

from __future__ import annotations

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


@pytest.mark.bug
class TestBug1ShallowCheckoutRaisesLoudly:
    """Original bug: `actions/checkout@v4` defaults to ``fetch-depth: 1``,
    truncating warning history. The old code swallowed the resulting
    ``ValueError`` from parsing empty `git log` output via
    ``except Exception: break``, silently undercounting warnings.

    The *workflow-level* fix is ``fetch-depth: 0`` in dms.yaml (covered by
    `test_workflow.py`). The *code-level* fix is that `from_last_commit`
    now raises ``DeadMansSwitchException`` on empty output instead of
    silently breaking — so if the workflow is ever misconfigured again, the
    cron will fail loudly rather than continue with bad state.
    """

    def test_from_last_commit_raises_on_empty_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct test: empty `git log` output → loud error, not silent break."""

        def fake_check_output(args, *a, **kw):
            return ""  # what a shallow checkout returns past its depth

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)
        with pytest.raises(DeadMansSwitchException, match="No commit at skip"):
            Commit.from_last_commit(0)

    def test_from_last_commit_raises_on_malformed_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Garbled output also fails loudly instead of silently breaking."""

        def fake_check_output(args, *a, **kw):
            return "no separators here"

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)
        with pytest.raises(DeadMansSwitchException, match="Could not parse"):
            Commit.from_last_commit(0)


@pytest.mark.bug
class TestBug2OwnerDetection:
    """Original bug: `is_by_repo_owner` compared `%an` (display name like
    "Onurcan Edokumaci") to the URL slug ("oedokumaci"). Fix: replaced
    owner-detection with `is_by_dms_bot` — the bot has a fixed, controlled
    identity, so checking it directly is robust regardless of how the
    owner's local git is configured.
    """

    def test_owner_with_typical_full_name_does_not_crash(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit(
            "Heartbeat",
            author_name="Onurcan Edokumaci",
            author_email="me@example.com",
        )
        # The owner's name differs from the URL slug — previously this
        # raised "Only the repo owner should commit to the repo". Now it
        # should construct cleanly.
        DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)

    def test_owner_committed_with_random_name(self, tmp_repo: GitRepo) -> None:
        """Anyone except dms_bot is treated as a heartbeat."""
        tmp_repo.commit("any message", author_name="A Different Person")
        # No exception — the commit just doesn't count as a bot warning.
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 2  # no warnings observed

    def test_bot_commit_is_recognized(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("initial")
        tmp_repo.commit(
            "warning issued",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
        )
        dms = DeadMansSwitch(48, 2, armed=False, manual_dispatch=False)
        assert dms._remaining_warnings == 1


@pytest.mark.bug
class TestBug3StrictSubstitution:
    """Original bug: `Email.from_txt` used `Template.safe_substitute` which
    silently left `${MISSING}` placeholders as literal text. Fix: use
    `Template.substitute` so missing env vars raise loudly with a useful
    error message.
    """

    def test_missing_env_var_in_body_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LAPTOP_PASSWORD", raising=False)
        template = tmp_path / "gf.txt"
        template.write_text(
            "To: gf@example.com\n"
            "Subject: oops\n"
            "\n"
            "My laptop password is ${LAPTOP_PASSWORD}.\n"
        )
        with pytest.raises(DeadMansSwitchException, match="Missing environment variable"):
            Email.from_txt(template)

    def test_missing_env_var_in_to_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RECIPIENT_EMAIL", raising=False)
        template = tmp_path / "x.txt"
        template.write_text("To: ${RECIPIENT_EMAIL}\nSubject: s\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="Missing environment variable"):
            Email.from_txt(template)

    def test_missing_env_var_in_subject_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUBJECT_VAR", raising=False)
        template = tmp_path / "x.txt"
        template.write_text("To: a@b.com\nSubject: ${SUBJECT_VAR}\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="Missing environment variable"):
            Email.from_txt(template)

    def test_present_env_var_substitutes_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VAR", "hello")
        template = tmp_path / "x.txt"
        template.write_text("To: a@b.com\nSubject: s\n\nThe value is ${VAR}.")
        email = Email.from_txt(template)
        assert email.body == "The value is hello."


@pytest.mark.bug
class TestBug4NEqualsZeroNoRetrigger:
    """New bug found by reviewer: with ``NUMBER_OF_WARNINGS=0`` the old
    `_get_remaining_warnings` loop ran ``range(0)`` (zero iterations), so the
    ``PASSED_AWAY`` sentinel was never produced. Every subsequent cron run
    re-derived PASSED_AWAY and re-sent the final emails. Fix: walk
    ``range(N + 1)`` so the latest commit is always inspected.
    """

    def test_n_zero_passed_away_is_recognized_as_already_dead(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit("ancient", when=hours_ago(1000))
        tmp_repo.commit(
            "passed away",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
            when=hours_ago(900),
        )
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=False)
        # Sentinel: N+1 = 1 means "already declared dead"
        assert dms._remaining_warnings == 1
        assert dms._get_state() == State.ALREADY_DECLARED_DEAD


@pytest.mark.bug
class TestBug5IdempotentPassedAway:
    """New bug: terminal commit used to be written *after* `send_all`. A
    partial failure mid-batch left no terminal commit, so the next cron
    re-sent every email — recipients got the "I'm dead" message twice.
    Fix: write the terminal commit *first*, then send. A partial send failure
    leaves the switch in ALREADY_DECLARED_DEAD and the next cron does nothing.
    """

    def test_terminal_commit_written_before_emails_sent(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "a.txt").write_text("To: a@b.com\nSubject: s\n\nbye")

        commit_at_send_time: list[str] = []
        from dead_mans_switch import EmailServer

        original_send_all = EmailServer.send_all

        def spy_send_all(self, emails):
            log = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%s"],
                cwd=tmp_repo.path,
                capture_output=True,
                text=True,
                check=True,
            )
            commit_at_send_time.append(log.stdout)
            return original_send_all(self, emails)

        monkeypatch.setattr(EmailServer, "send_all", spy_send_all)
        # Stub the actual git push since there's no remote.
        original_run = subprocess.run

        def push_stub(args, *a, **kw):
            if list(args[:2]) == ["git", "push"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", push_stub)

        DeadMansSwitch(48, 0, armed=True, manual_dispatch=False).run()

        # By the time send_all runs, PASSED_AWAY has already been committed.
        assert commit_at_send_time == ["passed away"]


@pytest.mark.bug
class TestEmptyEmailsGuard:
    """New finding: if the user forgets to rename ``.txt.template`` to
    ``.txt``, the PASSED_AWAY branch used to silently send zero emails and
    then write the terminal commit, permanently bricking the switch with no
    recipients notified. Fix: refuse to commit terminal state when the
    emails list is empty.
    """

    def test_empty_emails_directory_in_passed_away_raises(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,  # empty
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=False)
        with pytest.raises(DeadMansSwitchException, match="no \\.txt files"):
            dms.run()
        # No terminal commit should have been written.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout != "passed away"


@pytest.mark.bug
class TestManualDispatchDoesNotBrick:
    """New finding: ``workflow_dispatch`` unconditionally appends
    ``--manual-dispatch``. The old code still wrote the terminal commit in
    that path, so a single GUI "Run workflow" while the heartbeat was
    overdue would permanently arm the switch into ALREADY_DECLARED_DEAD.
    Fix: manual dispatch never advances state.
    """

    def test_manual_passed_away_does_not_write_terminal_commit(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "a.txt").write_text("To: a@b.com\nSubject: hi\n\nbody")

        DeadMansSwitch(48, 0, armed=True, manual_dispatch=True).run()

        # The email should have gone to the owner (redirected by manual mode).
        assert fake_smtp.sent[0].to_addr == "me@gmail.com"
        # No "passed away" commit should have landed.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout != "passed away"

    def test_manual_issue_warning_does_not_write_commit(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp,
    ) -> None:
        # As of round 7, ISSUE_WARNING validates creds AND opens an SMTP
        # session (to catch wrong/expired App Passwords) AND parses
        # templates, before short-circuiting on manual dispatch.
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        (emails_dir / "a.txt").write_text(
            "To: rcpt@example.com\nSubject: s\n\nbody"
        )
        tmp_repo.commit("old", when=hours_ago(100))
        DeadMansSwitch(48, 2, armed=True, manual_dispatch=True).run()
        # No new "warning issued" commit.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout != "warning issued"
        assert "manual dispatch" in capsys.readouterr().out.lower()


@pytest.mark.bug
class TestPipeInCommitMessage:
    """New finding: the old git log parser used ``|`` as a field separator
    and ``split("|", 2)``, which mis-parses commit messages containing
    ``|``. Fix: use the ASCII unit-separator (``\\x1f``) instead.
    """

    def test_pipe_in_commit_message_is_parsed_correctly(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit("refactor: split a|b|c")
        commit = Commit.from_last_commit(0)
        assert commit.message == "refactor: split a|b|c"


@pytest.mark.bug
class TestFirstTimeUserGetsClearError:
    """Reviewer finding H2: if MY_EMAIL was unset and the user's templates
    happened to reference some ${VAR} also unset, the user saw a confusing
    "Missing environment variable" error pointing at the template rather
    than the more actionable "MY_EMAIL not set". Fix: validate the
    EmailServer config before parsing templates.
    """

    def test_disarmed_with_no_credentials_complains_about_credentials(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Templates reference an unset env var, AND credentials are unset.
        # The user should see the credentials error, not the template error.
        monkeypatch.delenv("MY_EMAIL", raising=False)
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        monkeypatch.delenv("ANY_UNSET_VAR", raising=False)
        (emails_dir / "a.txt").write_text(
            "To: a@b.com\nSubject: s\n\nbody with ${ANY_UNSET_VAR}"
        )
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        with pytest.raises(DeadMansSwitchException, match="MY_EMAIL"):
            dms.run()

    def test_passed_away_with_no_credentials_complains_about_credentials(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MY_EMAIL", raising=False)
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "a.txt").write_text(
            "To: rcpt@example.com\nSubject: bye\n\nbody with ${UNSET}"
        )
        dms = DeadMansSwitch(48, 0, armed=True, manual_dispatch=False)
        with pytest.raises(DeadMansSwitchException, match="MY_EMAIL"):
            dms.run()


@pytest.mark.bug
class TestICloudAliasDomains:
    """README claimed support for ``icloud.com`` and ``me.com`` but the old
    config only included ``icloud.com``. Fix: added ``me.com`` and
    ``mac.com`` (the third Apple legacy domain).
    """

    @pytest.mark.parametrize("domain", ["icloud.com", "me.com", "mac.com"])
    def test_apple_domains_configure(
        self, domain: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dead_mans_switch import EmailServer

        monkeypatch.setenv("MY_EMAIL", f"user@{domain}")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        server = EmailServer()
        assert server._smtp_config["server"] == "smtp.mail.me.com"


# ---------------------------------------------------------------------------
# Round-3 regression tests (added after the third independent review found
# bugs that survived the first two rounds).
# ---------------------------------------------------------------------------


@pytest.mark.bug
class TestClockSkewDoesNotJamSwitch:
    """Round-3 finding (R1): a future-dated commit makes ``hours_since``
    negative, and ``negative > interval`` was always False — switch jammed in
    ALIVE forever, regardless of how stale every other commit was. Fix:
    treat ``hours_since < 0`` as ``interval_passed``.
    """

    def test_future_dated_owner_commit_advances_state(
        self, tmp_repo: GitRepo
    ) -> None:
        future = datetime.now(timezone.utc).replace(year=2099)
        tmp_repo.commit("future-dated", when=future)
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        # Without the fix this would be ALIVE; with the fix it advances.
        assert dms._get_state() == State.ISSUE_WARNING


@pytest.mark.bug
class TestEmailRegexRejectsInjection:
    """Round-3 finding (R1+R2+R3): ``re.match`` wasn't anchored at end and
    the character class didn't exclude whitespace/comma. An env-substituted
    To: containing CRLF could enable SMTP-header injection or accidentally
    Bcc multiple recipients.
    """

    @pytest.mark.parametrize(
        "addr",
        [
            "ok@example.com\r\nBcc: leak@evil.com",
            "ok@example.com\nBcc: leak@evil.com",
            "ok@example.com, other@example.com",
            "ok@example.com trailing",
            "ok@example.com\ttab",
        ],
    )
    def test_dangerous_to_address_rejected(self, addr: str) -> None:
        with pytest.raises(DeadMansSwitchException, match="Invalid email"):
            Email(addr, "s", "b")


@pytest.mark.bug
class TestPassedAwayIsPermanent:
    """Round-3 finding (R3): if the owner pushed a commit after a PASSED_AWAY
    (vacation false-positive scenario), the bot would walk past the
    PASSED_AWAY commit, treat the owner commit as a heartbeat, reset the
    warning counter, and eventually fire a *second* mortality storm. Fix:
    detect PASSED_AWAY anywhere in history and stay there permanently.
    """

    def test_owner_commit_after_passed_away_does_not_revive(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit("old", when=hours_ago(1000))
        tmp_repo.commit(
            State.PASSED_AWAY,
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
            when=hours_ago(900),
        )
        # The owner "comes back" with a new commit. Without the fix this
        # would reset to ALIVE. With the fix, the switch stays terminal.
        tmp_repo.commit("revival heartbeat", when=hours_ago(1))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() == State.ALREADY_DECLARED_DEAD


@pytest.mark.bug
class TestLiteralDollarInTemplate:
    """Round-3 finding (R3): ``Template.substitute`` raised ValueError on
    any unescaped ``$`` (``$5``, ``$HOME``, regex anchors, shell variables).
    Real email bodies contain those constantly. Fix: switched to a custom
    regex-based substitution that only recognises ``${IDENT}`` — bare ``$``
    is left as literal text.
    """

    def test_dollar_amounts_in_body_survive(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "x.txt"
        p.write_text(
            "To: a@b.com\nSubject: bill\n\nYou owe me $500 and $HOME pwd."
        )
        email = Email.from_txt(p)
        assert email.body == "You owe me $500 and $HOME pwd."

    def test_braced_var_still_substitutes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AMOUNT", "$500")
        p = tmp_path / "x.txt"
        p.write_text("To: a@b.com\nSubject: s\n\nYou owe me ${AMOUNT} this month.")
        assert Email.from_txt(p).body == "You owe me $500 this month."


@pytest.mark.bug
class TestEmptyEmailsGuardAtWarning:
    """Round-3 finding (R3): the empty-emails guard was only in the
    PASSED_AWAY branch. The warning branch happily wrote state-advancing
    commits with nothing to send, so the user could walk through N warning
    commits before the empty-dir error surfaced at the final stage —
    typically after they're gone.
    """

    def test_warning_with_empty_emails_dir_raises(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,  # empty
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tmp_repo.commit("old", when=hours_ago(100))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        with pytest.raises(DeadMansSwitchException, match="no \\.txt files"):
            dms.run()


@pytest.mark.bug
class TestSmtpConnectionBeforeTerminalCommit:
    """Round-3 finding (R2): in armed PASSED_AWAY, the SMTP connection was
    established AFTER ``write_to_repo``. If TLS/auth/DNS failed on connect,
    the terminal commit was already on the remote → next cron sees
    ALREADY_DECLARED_DEAD → recipients never notified. Fix: enter the
    ``with server`` block first; commit and send inside.
    """

    def test_smtp_connect_failure_does_not_write_terminal_commit(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        fake_smtp,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        tmp_repo.commit("ancient", when=hours_ago(1000))
        (emails_dir / "a.txt").write_text("To: a@b.com\nSubject: s\n\nbye")
        # Force the SMTP authentication to fail — simulating the transient
        # connection failure window.
        fake_smtp.auth_raises = True
        with pytest.raises(DeadMansSwitchException, match="authenticate"):
            DeadMansSwitch(48, 0, armed=True, manual_dispatch=False).run()
        # The terminal commit must NOT have been written — otherwise the
        # next cron would see ALREADY_DECLARED_DEAD and silently skip
        # forever.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout != State.PASSED_AWAY


@pytest.mark.bug
class TestHiddenFileAndDirectoryFilter:
    """Round-3 finding (R1): ``Path.glob('*.txt')`` includes dotfiles and
    can match directories named ``foo.txt/``. Both are now filtered."""

    def test_dotfile_is_skipped(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
    ) -> None:
        (emails_dir / "real.txt").write_text("To: a@b.com\nSubject: s\n\nbody")
        (emails_dir / ".hidden.txt").write_text(
            "To: a@b.com\nSubject: s\n\nbody"
        )
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert len(dms._email_template_paths()) == 1

    def test_directory_named_dot_txt_is_skipped(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        emails_dir: Path,
    ) -> None:
        (emails_dir / "real.txt").write_text("To: a@b.com\nSubject: s\n\nbody")
        (emails_dir / "draft.txt").mkdir()
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert len(dms._email_template_paths()) == 1


@pytest.mark.bug
class TestHeartbeatThresholdEquality:
    """Round-3 finding (R3): strict ``>`` meant a commit at exactly the
    threshold reported ALIVE. Loosened to ``>=`` so a perfectly-on-time
    cron doesn't miss by epsilon.
    """

    def test_at_exact_threshold_advances(self, tmp_repo: GitRepo) -> None:
        tmp_repo.commit("at-threshold", when=hours_ago(48))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() == State.ISSUE_WARNING


# ---------------------------------------------------------------------------
# Round-4 regression tests (added after a second wave of three independent
# Opus reviewers each surfaced bugs the prior eight reviewers missed).
# ---------------------------------------------------------------------------


@pytest.mark.bug
class TestOutOfHistoryDistinctFromParseError:
    """Round-4 finding (R5 C1 / "A4"): `_get_remaining_warnings` previously
    caught every `DeadMansSwitchException` from `from_last_commit` and
    treated it as "end of history". But the same exception was raised for
    parse failures (malformed format, bad timestamp), causing the function
    to silently undercount warnings. Fix: introduce `_OutOfHistoryError`
    subclass; only that one is caught by the loop.
    """

    def test_parse_failure_at_skip_one_propagates(
        self, tmp_repo: GitRepo
    ) -> None:
        """Garbled output at skip=1 must not be silently treated as end of
        history. Otherwise a corrupt commit could let the switch keep
        running with an undercounted warning state."""
        from dead_mans_switch import _OutOfHistoryError

        # Set up a real repo with one valid commit at skip=0...
        tmp_repo.commit(
            "warning issued",
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
        )

        # ...then patch Commit.from_last_commit so skip=1 returns garbage,
        # exercising the parse-error branch independently of the (real)
        # _passed_away_already_committed sweep.
        from dead_mans_switch import Commit

        original_from_last = Commit.from_last_commit

        def fake_from_last(i: int = 0):
            if i == 0:
                return original_from_last(0)
            raise DeadMansSwitchException(
                f"Could not parse git log output: 'no separators'"
            )

        import dead_mans_switch as dms_mod
        monkey = pytest.MonkeyPatch()
        monkey.setattr(dms_mod.Commit, "from_last_commit", staticmethod(fake_from_last))
        try:
            with pytest.raises(DeadMansSwitchException, match="Could not parse"):
                DeadMansSwitch._get_remaining_warnings(2)
        finally:
            monkey.undo()

        # And the dedicated subclass is what the loop is allowed to swallow.
        assert issubclass(_OutOfHistoryError, DeadMansSwitchException)


@pytest.mark.bug
class TestPassedAwayExactMatch:
    """Round-4 finding (R4 M5 / R5 C2 / R6 M1 / "A1"):
    ``git log --author=dms_bot`` is a regex substring filter, not exact
    equality. A real contributor named ``alice_dms_bot_x`` (or any commit
    whose author email contains ``dms_bot``) would have their commits
    scanned, and a subject of exactly ``passed away`` would brick the
    switch into ALREADY_DECLARED_DEAD. Fix: emit ``%an<sep>%s`` and check
    for exact equality of both fields.
    """

    def test_substring_match_author_does_not_trigger(
        self, tmp_repo: GitRepo
    ) -> None:
        # An attacker (or just a confused contributor) commits "passed away"
        # with an author whose name CONTAINS "dms_bot".
        tmp_repo.commit("ancient", when=hours_ago(1000))
        tmp_repo.commit(
            State.PASSED_AWAY,
            author_name="alice_dms_bot_x",
            author_email="alice@example.com",
            when=hours_ago(500),
        )
        # Recent owner heartbeat — switch should be ALIVE.
        tmp_repo.commit("recent heartbeat", when=hours_ago(1))
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        # With the old substring `--author=dms_bot`, this would match the
        # impostor commit and report ALREADY_DECLARED_DEAD. With the exact
        # match it correctly stays ALIVE.
        assert dms._get_state() == State.ALIVE


@pytest.mark.bug
class TestWarningPreFlightParse:
    """Round-4 finding (R6 H2 / "A5"): the warning handler used to write a
    state-advancing commit before parsing templates. A typo'd ``${VAR}``
    would let every warning land safely and only surface at PASSED_AWAY,
    by which time the owner may not be alive to fix it. Fix: parse all
    templates at the top of `_handle_issue_warning`.
    """

    def test_warning_with_missing_env_var_raises_before_commit(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp,
    ) -> None:
        # MY_EMAIL is set so we get past the credential check to the
        # template-parse check that this test is actually about.
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        monkeypatch.delenv("UNSET_VAR", raising=False)
        tmp_repo.commit("old", when=hours_ago(100))
        (emails_dir / "x.txt").write_text(
            "To: rcpt@example.com\nSubject: s\n\nbody ${UNSET_VAR}"
        )
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        with pytest.raises(
            DeadMansSwitchException, match="Missing environment variable"
        ):
            dms.run()
        # Most importantly: the warning commit was NOT written.
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout != State.ISSUE_WARNING


@pytest.mark.bug
class TestAlreadyDeadBeatsDisarmed:
    """Round-4 finding (R6 L4 / "R6 L4"): user toggling `ARMED=false`
    after PASSED_AWAY used to restart daily test emails (which would land
    in the inbox of whoever inherited the deceased's account). Fix:
    `ALREADY_DECLARED_DEAD` takes precedence over `DISARMED`.
    """

    def test_disarmed_after_passed_away_stays_terminal(
        self, tmp_repo: GitRepo
    ) -> None:
        tmp_repo.commit("ancient", when=hours_ago(1000))
        tmp_repo.commit(
            State.PASSED_AWAY,
            author_name=BOT_USERNAME,
            author_email="dms@bot.github.com",
            when=hours_ago(900),
        )
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        # Even though armed=False, the terminal state takes precedence.
        assert dms._get_state() == State.ALREADY_DECLARED_DEAD


# ---------------------------------------------------------------------------
# Round-5 regression tests.
# ---------------------------------------------------------------------------


@pytest.mark.bug
class TestPushSucceededServerSide:
    """Round-5 finding (R7 H1): a non-zero `git push` can mean either
    "didn't land on remote" or "landed but client lost the ack" (TCP RST
    after success, network timeout, SIGTERM). The old code rolled back +
    raised in both cases, so the latter silently bricked the switch via
    the next cron's `ALREADY_DECLARED_DEAD`. Fix: after non-zero push,
    `ls-remote` to check if the remote already has our commit.
    """

    def test_push_failed_but_remote_has_commit_proceeds(
        self,
        tmp_repo: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tmp_repo.commit("initial")

        import dead_mans_switch as dms_mod

        # Stub the post-push verification to claim the remote has it.
        monkeypatch.setattr(dms_mod, "_remote_has_commit", lambda sha: True)
        original_run = subprocess.run

        def spy_run(args, *a, **kw):
            if list(args[:2]) == ["git", "push"]:
                raise subprocess.CalledProcessError(1, list(args), "", "TCP RST")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spy_run)
        # Should NOT raise — the verification succeeded.
        Commit(
            message="warning issued",
            user=BOT_USERNAME,
            email=BOT_EMAIL,
            timestamp=datetime.now(timezone.utc),
        ).write_to_repo()
        assert "treating as success" in capsys.readouterr().out

    def test_push_failed_and_remote_does_not_have_commit_raises(
        self,
        tmp_repo: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tmp_repo.commit("initial")

        import dead_mans_switch as dms_mod

        # Verification reports the push really didn't land.
        monkeypatch.setattr(dms_mod, "_remote_has_commit", lambda sha: False)
        original_run = subprocess.run

        def spy_run(args, *a, **kw):
            if list(args[:2]) == ["git", "push"]:
                raise subprocess.CalledProcessError(1, list(args), "", "")
            return original_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spy_run)
        with pytest.raises(DeadMansSwitchException, match="push failed"):
            Commit(
                message="warning issued",
                user=BOT_USERNAME,
                email=BOT_EMAIL,
                timestamp=datetime.now(timezone.utc),
            ).write_to_repo()


@pytest.mark.bug
class TestManualDispatchPreflightsTemplates:
    """Round-5 finding (R9 H1): if the user was already past their
    heartbeat when they ran the manual verify step, `_handle_issue_warning`
    short-circuited on `manual_dispatch=True` *before* parsing templates
    — so a typo'd `${VAR}` looked fine in the verify step and only blew
    up at PASSED_AWAY weeks later when nobody could fix it. Fix: run the
    empty-dir guard + template parse before the manual-dispatch
    short-circuit.
    """

    def test_manual_warning_with_missing_var_raises(
        self,
        tmp_repo: GitRepo,
        emails_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        monkeypatch.delenv("UNSET_VAR", raising=False)
        tmp_repo.commit("old", when=hours_ago(100))
        (emails_dir / "x.txt").write_text(
            "To: rcpt@example.com\nSubject: s\n\nbody ${UNSET_VAR}"
        )
        # State is ISSUE_WARNING, manual_dispatch=True. Prior code would
        # have short-circuited on the manual_dispatch check. Now the
        # template parse runs first and surfaces the missing var.
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=True)
        with pytest.raises(
            DeadMansSwitchException, match="Missing environment variable"
        ):
            dms.run()


@pytest.mark.bug
class TestUtf8ErrorIsActionable:
    """Round-5 finding (R9 H2): `UnicodeDecodeError` subclasses
    `ValueError`, not `OSError`, so the prior `except OSError` clause
    didn't catch it. Non-UTF-8 templates gave a raw stack trace. Now
    wrapped with an actionable error message.
    """

    def test_non_utf8_template_gives_actionable_error(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "x.txt"
        # Write Latin-1 with a non-ASCII byte that's invalid as UTF-8 start.
        p.write_bytes(b"To: a@b.com\nSubject: caf\xe9\n\nbody")
        with pytest.raises(
            DeadMansSwitchException, match="not valid UTF-8"
        ):
            Email.from_txt(p)


@pytest.mark.bug
class TestRefuseOwnerEqualsBot:
    """Round-5 finding (R7 L2): if the runner's local `git config
    user.name` happens to be `dms_bot`, every heartbeat would be
    misclassified as a bot warning and the switch would silently jam.
    Now we refuse to run.
    """

    def test_runner_with_bot_username_aborts(
        self, tmp_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dead_mans_switch import _refuse_if_owner_is_bot

        # Patch the runner's git identity.
        subprocess.run(
            ["git", "config", "user.name", BOT_USERNAME],
            cwd=tmp_repo.path,
            check=True,
        )
        with pytest.raises(DeadMansSwitchException, match="reserved identity"):
            _refuse_if_owner_is_bot()

    def test_no_local_config_is_fine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If `git config user.name` errors out (no config), skip the check."""
        from dead_mans_switch import _refuse_if_owner_is_bot

        def fake(args, *a, **kw):
            raise subprocess.CalledProcessError(1, list(args))

        monkeypatch.setattr(subprocess, "check_output", fake)
        # Should not raise.
        _refuse_if_owner_is_bot()


@pytest.mark.bug
class TestMyEmailValidated:
    """Round-7 finding (R16 high): MY_EMAIL wasn't passed through the
    same injection-safety regex used for recipients. A secret pasted
    with a trailing CRLF (Windows clipboards) would otherwise be baked
    into ``msg["From"]`` and crash MIMEMultipart at send time — AFTER
    the terminal commit was pushed.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "me@gmail.com\r\n",
            "me@gmail.com\nBcc: leak@evil.com",
            "me@gmail.com\t",
            "not-an-email",
            "",
        ],
    )
    def test_invalid_my_email_rejected_at_init(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dead_mans_switch import EmailServer

        if value:
            monkeypatch.setenv("MY_EMAIL", value)
        else:
            monkeypatch.delenv("MY_EMAIL", raising=False)
        monkeypatch.setenv("MY_PASSWORD", "pw")
        with pytest.raises(DeadMansSwitchException):
            EmailServer()


@pytest.mark.bug
class TestSubjectControlChars:
    """Round-6 finding (R12 M, R13 M3, R14 M): subject substitution wasn't
    CRLF-validated like the To field was. A multi-line `${SUBJECT_VAR}`
    in PASSED_AWAY would crash `email.policy` mid-batch (after the terminal
    commit was already pushed). Now rejected at parse time."""

    @pytest.mark.parametrize(
        "subject",
        ["a\nb", "a\rb", "a\r\nb", "a\vb", "a\fb", "a\x00b"],
    )
    def test_control_char_in_subject_rejected(self, subject: str) -> None:
        with pytest.raises(DeadMansSwitchException, match="control character"):
            Email("ok@b.com", subject, "body")


@pytest.mark.bug
class TestNonFiniteIntervalRejected:
    """Round-6 finding (R13 L1): NaN and inf both bypass the
    < HEARTBEAT_CHECK_HOUR_FREQUENCY guard (NaN comparisons are always
    False; inf is not < any finite value). Both would silently jam the
    switch in ALIVE. Now rejected at construction."""

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejected(
        self, value: float, tmp_repo_with_initial_commit: GitRepo
    ) -> None:
        with pytest.raises(DeadMansSwitchException, match="finite"):
            DeadMansSwitch(value, 2, armed=True, manual_dispatch=False)


@pytest.mark.bug
class TestCommitIsFrozen:
    """Round-5 finding (R9 low): `Email` was frozen but `Commit` was not.
    Asymmetry without justification; now both are frozen.
    """

    def test_commit_cannot_be_mutated(self) -> None:
        c = Commit("m", "u", "u@e.com", datetime.now(timezone.utc))
        with pytest.raises(Exception):
            c.message = "different"


@pytest.mark.bug
class TestEmailIsFrozen:
    """Round-4 finding (R5 M4 / R6 M2 / "freeze Email"): mutable Email
    dataclass let `email.to = ...` bypass `__post_init__` validation. Fix:
    `frozen=True`, mutation routes through `dataclasses.replace`.
    """

    def test_direct_mutation_raises(self) -> None:
        e = Email("ok@example.com", "s", "b")
        with pytest.raises(Exception):
            # FrozenInstanceError is a subclass of AttributeError;
            # accept either name.
            e.to = "evil@example.com\r\nBcc: leak@evil.com"

    def test_replace_re_runs_validation(self) -> None:
        from dataclasses import replace

        e = Email("ok@example.com", "s", "b")
        with pytest.raises(DeadMansSwitchException, match="Invalid email"):
            replace(e, to="evil@example.com\r\nBcc: leak@evil.com")

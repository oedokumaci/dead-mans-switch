"""Shared pytest fixtures for the Dead Man's Switch test suite."""

from __future__ import annotations

import os
import smtplib
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove environment variables that could leak between tests."""
    for key in ("MY_EMAIL", "MY_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


@dataclass
class GitRepo:
    """Helper handle for a temporary git repository used in tests."""

    path: Path
    # Realistic display name that differs from the URL slug — mirrors typical
    # git setups where `user.name` is a person's name, not their GitHub login.
    owner_name: str = "Onurcan Edokumaci"
    owner_email: str = "owner@example.com"
    owner_slug: str = "oedokumaci"
    remote_url: str = "https://github.com/oedokumaci/dead-mans-switch.git"

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=check, capture_output=True, text=True
        )

    def commit(
        self,
        message: str,
        *,
        author_name: str | None = None,
        author_email: str | None = None,
        when: datetime | None = None,
        allow_empty: bool = True,
    ) -> None:
        # Local import to avoid hard import cycle with tests/__init__.py.
        from dead_mans_switch import BOT_EMAIL, BOT_USERNAME

        env = os.environ.copy()
        if author_name is None:
            author_name = self.owner_name
        if author_email is None:
            # Tests that pass `author_name="dms_bot"` almost always want the
            # commit to actually look like a bot commit (so `is_by_dms_bot`
            # returns True). Default the email to match so individual tests
            # don't have to keep both in sync. Tests that *want* a name-only
            # match (impersonation) can still pass `author_email` explicitly.
            if author_name == BOT_USERNAME:
                author_email = BOT_EMAIL
            else:
                author_email = self.owner_email
        if when is None:
            when = datetime.now(timezone.utc)
        iso = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_AUTHOR_DATE"] = iso
        env["GIT_COMMITTER_NAME"] = author_name
        env["GIT_COMMITTER_EMAIL"] = author_email
        env["GIT_COMMITTER_DATE"] = iso
        args = ["git", "commit", "-m", message]
        if allow_empty:
            args.insert(2, "--allow-empty")
        subprocess.run(args, cwd=self.path, env=env, check=True, capture_output=True)


@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitRepo:
    """Create an empty git repo with a remote and chdir into it."""
    repo = GitRepo(path=tmp_path)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", repo.owner_name],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", repo.owner_email],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", repo.remote_url],
        cwd=tmp_path,
        check=True,
    )
    # The Commit class assumes the script lives in this repo to find PATH_TO_EMAILS,
    # but the cwd is what `git` will operate on. Chdir keeps both consistent.
    monkeypatch.chdir(tmp_path)
    return repo


@pytest.fixture
def tmp_repo_with_initial_commit(tmp_repo: GitRepo) -> GitRepo:
    """Repo with one owner commit so `git log` has something to return."""
    tmp_repo.commit("initial commit")
    return tmp_repo


@dataclass
class SentMessage:
    """A message captured by the fake SMTP server."""

    from_addr: str
    to_addr: str
    subject: str
    body: str
    raw: Message


@dataclass
class FakeSMTPState:
    """Records what happened during an SMTP session."""

    server: str = ""
    port: int = 0
    timeout: float | None = None
    started_tls: bool = False
    login_email: str | None = None
    login_password: str | None = None
    quit_called: bool = False
    quit_raises: bool = False
    auth_raises: bool = False
    starttls_raises: bool = False
    connect_raises: type[BaseException] | None = None
    sent: list[SentMessage] = field(default_factory=list)
    used_as_context: bool = False


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> FakeSMTPState:
    """Replace smtplib.SMTP with a controllable fake. Returns the state object."""
    state = FakeSMTPState()

    class FakeSMTP:
        def __init__(self, server: str, port: int, timeout: float | None = None) -> None:
            if state.connect_raises is not None:
                raise state.connect_raises("simulated connection failure")
            state.server = server
            state.port = port
            state.timeout = timeout

        def starttls(self) -> None:
            if state.starttls_raises:
                raise smtplib.SMTPException("simulated starttls failure")
            state.started_tls = True

        def login(self, email: str, password: str) -> None:
            if state.auth_raises:
                raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
            state.login_email = email
            state.login_password = password

        def send_message(self, msg: Message) -> None:
            state.sent.append(
                SentMessage(
                    from_addr=str(msg["From"]),
                    to_addr=str(msg["To"]),
                    subject=str(msg["Subject"]),
                    body=msg.get_payload()[0].get_payload(),
                    raw=msg,
                )
            )

        def quit(self) -> None:
            if state.quit_raises:
                raise smtplib.SMTPException("simulated quit failure")
            state.quit_called = True

        def __enter__(self) -> "FakeSMTP":
            state.used_as_context = True
            return self

        def __exit__(self, *_: Any) -> None:
            self.quit()

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return state


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace time.sleep with a recorder so tests stay fast."""
    import time as time_mod

    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(time_mod, "sleep", fake_sleep)
    return calls


@pytest.fixture
def emails_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp emails directory, patched into DeadMansSwitch.PATH_TO_EMAILS."""
    import dead_mans_switch as dms

    directory = tmp_path / "emails"
    directory.mkdir()
    monkeypatch.setattr(dms.DeadMansSwitch, "PATH_TO_EMAILS", directory)
    return directory


def make_template(path: Path, *, to: str = "rcpt@example.com", subject: str = "S", body: str = "B") -> Path:
    """Write an email template file and return its path."""
    path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def template_factory(emails_dir: Path):
    """Factory for creating email templates inside the patched emails dir."""

    def _factory(name: str, **kwargs: str) -> Path:
        return make_template(emails_dir / name, **kwargs)

    return _factory


def utc(*args: int) -> datetime:
    """Convenience constructor for UTC datetimes in tests."""
    return datetime(*args, tzinfo=timezone.utc)


def hours_ago(n: float) -> datetime:
    """Return a UTC datetime n hours before now."""
    return datetime.now(timezone.utc) - timedelta(hours=n)

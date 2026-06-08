"""Tests for the EmailServer SMTP wrapper."""

from __future__ import annotations

import smtplib

import pytest

from dead_mans_switch import DeadMansSwitchException, Email, EmailServer
from tests.conftest import FakeSMTPState


SUPPORTED_PROVIDERS = [
    ("user@gmail.com", "smtp.gmail.com", 587),
    ("user@icloud.com", "smtp.mail.me.com", 587),
    ("user@outlook.com", "smtp-mail.outlook.com", 587),
    ("user@yahoo.com", "smtp.mail.yahoo.com", 587),
    ("user@hotmail.com", "smtp-mail.outlook.com", 587),
    ("user@protonmail.ch", "smtp.protonmail.ch", 587),
    ("user@protonmail.com", "smtp.protonmail.ch", 587),
    ("user@fastmail.com", "smtp.fastmail.com", 587),
    ("user@zoho.com", "smtp.zoho.com", 587),
    ("user@zohomail.com", "smtppro.zoho.com", 587),
    ("user@aol.com", "smtp.aol.com", 587),
    ("user@gmx.com", "mail.gmx.com", 587),
    ("user@gmx.net", "mail.gmx.com", 587),
    ("user@mail.com", "smtp.mail.com", 587),
    ("user@yandex.com", "smtp.yandex.com", 587),
    ("user@yandex.ru", "smtp.yandex.com", 587),
]


class TestInitialization:
    def test_missing_email_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_PASSWORD", "x")
        with pytest.raises(DeadMansSwitchException, match="MY_EMAIL"):
            EmailServer()

    def test_missing_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_EMAIL", "user@gmail.com")
        with pytest.raises(DeadMansSwitchException, match="MY_PASSWORD"):
            EmailServer()

    def test_unsupported_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_EMAIL", "user@nothing.test")
        monkeypatch.setenv("MY_PASSWORD", "x")
        with pytest.raises(DeadMansSwitchException, match="Unsupported email provider"):
            EmailServer()

    @pytest.mark.parametrize("email,server_host,port", SUPPORTED_PROVIDERS)
    def test_all_supported_providers_configure_correctly(
        self,
        email: str,
        server_host: str,
        port: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", email)
        monkeypatch.setenv("MY_PASSWORD", "pw")
        server = EmailServer()
        assert server._smtp_config["server"] == server_host
        assert server._smtp_config["port"] == port

    def test_domain_match_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "User@GMAIL.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        server = EmailServer()
        assert server._smtp_config["server"] == "smtp.gmail.com"

    def test_email_property(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "x")
        assert EmailServer().email == "me@gmail.com"


class TestContextManager:
    def test_enter_connects_and_logs_in(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        with EmailServer():
            assert fake_smtp.started_tls
            assert fake_smtp.login_email == "me@gmail.com"
            assert fake_smtp.login_password == "pw"

    def test_exit_quits(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        with EmailServer():
            pass
        assert fake_smtp.quit_called

    def test_exit_swallows_quit_errors(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        fake_smtp.quit_raises = True
        # Should not propagate
        with EmailServer():
            pass

    def test_exit_propagates_inner_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        with pytest.raises(RuntimeError, match="boom"):
            with EmailServer():
                raise RuntimeError("boom")

    def test_exit_without_prior_enter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct call to __exit__ when _smtp_server was never set."""
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        srv = EmailServer()
        # No __enter__ → _smtp_server stays None — exits cleanly without raising.
        srv.__exit__(None, None, None)
        # __exit__ returns None (implicitly) — Python only suppresses
        # exceptions when __exit__ returns truthy, and we never want that.
        assert srv.__exit__(None, None, None) is None


class TestConnectionErrors:
    def test_auth_failure_raises_dms_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        fake_smtp.auth_raises = True
        with pytest.raises(DeadMansSwitchException, match="authenticate"):
            with EmailServer():
                pass

    def test_smtp_exception_raises_dms_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        fake_smtp.starttls_raises = True
        with pytest.raises(DeadMansSwitchException, match="SMTP error"):
            with EmailServer():
                pass

    def test_generic_exception_raises_dms_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_smtp: FakeSMTPState
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        fake_smtp.connect_raises = ConnectionRefusedError
        with pytest.raises(DeadMansSwitchException, match="Failed to connect"):
            with EmailServer():
                pass


class TestSending:
    def test_send_in_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        with EmailServer() as srv:
            srv.send_all([Email("a@b.com", "S", "Body")])
        assert len(fake_smtp.sent) == 1
        msg = fake_smtp.sent[0]
        assert msg.from_addr == "me@gmail.com"
        assert msg.to_addr == "a@b.com"
        assert msg.subject == "S"
        assert "Body" in msg.body

    def test_send_outside_context_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
    ) -> None:
        """Forgetting the `with` block used to silently fall back to a fresh
        TLS+login per email, bypassing rate-limit pacing. Now raises."""
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        srv = EmailServer()
        with pytest.raises(DeadMansSwitchException, match="context manager"):
            srv.send_all([Email("a@b.com", "S", "Body")])

    def test_send_all_sleeps_between(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")
        emails = [
            Email("a@b.com", "S1", "B1"),
            Email("c@d.com", "S2", "B2"),
        ]
        with EmailServer() as srv:
            srv.send_all(emails)
        # N-1 sleeps for N emails — no wasted sleep after the last send.
        assert len(no_sleep) == 1
        assert no_sleep[0] == EmailServer.SECONDS_BETWEEN_EMAILS

    def test_send_failure_wraps_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_smtp: FakeSMTPState,
        no_sleep: list[float],
    ) -> None:
        monkeypatch.setenv("MY_EMAIL", "me@gmail.com")
        monkeypatch.setenv("MY_PASSWORD", "pw")

        with EmailServer() as srv:
            # Force send_message to blow up
            def bad_send(_msg):
                raise smtplib.SMTPException("boom")

            srv._smtp_server.send_message = bad_send  # type: ignore[assignment]
            with pytest.raises(DeadMansSwitchException, match="Failed to send"):
                srv.send_all([Email("a@b.com", "S", "B")])

"""Tests for the Email dataclass and template parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from dead_mans_switch import DeadMansSwitchException, Email


class TestEmailValidation:
    def test_valid_email_passes(self) -> None:
        e = Email("user@example.com", "subj", "body")
        assert e.to == "user@example.com"
        assert e.subject == "subj"
        assert e.body == "body"

    @pytest.mark.parametrize(
        "addr",
        [
            "no-at-sign",
            "@no-local.com",
            "no-domain@",
            "no-tld@example",
            "two@@signs.com",
            "",
        ],
    )
    def test_invalid_email_raises(self, addr: str) -> None:
        with pytest.raises(DeadMansSwitchException, match="Invalid email address"):
            Email(addr, "s", "b")


class TestFromTxt:
    def test_basic_parse(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\nSubject: hi\n\nhello there\n")
        e = Email.from_txt(p)
        assert e.to == "a@b.com"
        assert e.subject == "hi"
        assert e.body == "hello there"

    def test_multiline_body(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\nSubject: hi\n\nline1\nline2\nline3\n")
        e = Email.from_txt(p)
        assert e.body == "line1\nline2\nline3"

    def test_case_insensitive_headers(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("TO: a@b.com\nSUBJECT: hi\n\nbody")
        e = Email.from_txt(p)
        assert e.to == "a@b.com"
        assert e.subject == "hi"

    def test_extra_blank_lines_between_subject_and_body(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\nSubject: hi\n\n\n\nbody")
        e = Email.from_txt(p)
        assert e.body == "body"

    def test_env_substitution_in_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RCPT", "real@example.com")
        p = tmp_path / "msg.txt"
        p.write_text("To: ${RCPT}\nSubject: s\n\nb")
        assert Email.from_txt(p).to == "real@example.com"

    def test_env_substitution_in_subject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOPIC", "urgent")
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\nSubject: ${TOPIC}!\n\nbody")
        assert Email.from_txt(p).subject == "urgent!"

    def test_env_substitution_in_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECRET", "rosebud")
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\nSubject: s\n\nThe password is ${SECRET}.")
        assert Email.from_txt(p).body == "The password is rosebud."

    def test_missing_to_field_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("Subject: hi\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="No 'To:' field"):
            Email.from_txt(p)

    def test_missing_subject_field_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "msg.txt"
        p.write_text("To: a@b.com\n\nbody")
        with pytest.raises(DeadMansSwitchException, match="No 'Subject:' field"):
            Email.from_txt(p)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DeadMansSwitchException, match="Email file not found"):
            Email.from_txt(tmp_path / "nope.txt")

    def test_invalid_email_after_substitution_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BAD", "not-an-email")
        p = tmp_path / "msg.txt"
        p.write_text("To: ${BAD}\nSubject: s\n\nb")
        with pytest.raises(DeadMansSwitchException):
            Email.from_txt(p)

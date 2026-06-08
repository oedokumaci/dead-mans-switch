"""Tests for the public-activity liveness signal (v2.1.0 opt-in feature).

The test matrix here is intentionally exhaustive — ``--cov-fail-under=100``
plus branch coverage means every short-circuit, every per-event ``continue``,
every fail-closed guard needs a case on each side. See
``docs/PLAN_liveness.md`` §4.3 for the original enumeration.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import dead_mans_switch
from dead_mans_switch import (
    DeadMansSwitch,
    DeadMansSwitchException,
    EVENTS_API_MAX_DAYS,
    LIVENESS_EVENT_TYPES,
    MESSAGE_PATTERN_PAYLOAD_PATHS,
    State,
    _build_opener,
    _compile_pattern_list,
    _fetch_public_events,
    _has_recent_public_activity,
    _is_bot_event,
    _parse_check_public_activity,
    _truncate_for_regex,
)
from tests.conftest import (
    FakeGitHubEventsState,
    GitRepo,
    gh_event,
    hours_ago,
)


def _enable_feature(
    monkeypatch: pytest.MonkeyPatch,
    *,
    username: str = "alice",
    token: str | None = None,
    repo: str = "alice/dead-mans-switch",
) -> None:
    """Set the env flags so DeadMansSwitch(...) constructs with the feature on."""
    monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "true")
    monkeypatch.setenv("GH_USERNAME", username)
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", username)
    if token is not None:
        monkeypatch.setenv("GH_ACTIVITY_TOKEN", token)


# ---------------------------------------------------------------------------
# _truncate_for_regex
# ---------------------------------------------------------------------------


class TestTruncateForRegex:
    def test_short_string_unchanged(self) -> None:
        assert _truncate_for_regex("hello") == "hello"

    def test_long_string_truncated(self) -> None:
        long = "a" * 10_000
        out = _truncate_for_regex(long)
        assert len(out) == dead_mans_switch.REGEX_INPUT_MAX_BYTES

    def test_truncation_preserves_prefix(self) -> None:
        """`value[:N]` must keep the START of the string, not the end.
        Bot signatures (e.g. `chore(deps):`) appear at the beginning of
        PR titles/bodies — a mutation to `value[-N:]` would silently
        miss them. Use a distinctive prefix and a long all-`x` tail so
        the slice direction is observable."""
        marker = "PREFIX-MARKER:"
        long = marker + ("x" * 10_000)
        out = _truncate_for_regex(long)
        assert out.startswith(marker), (
            "expected truncation to keep the prefix; suggests value[-N:] mutation"
        )

    def test_none_returns_empty(self) -> None:
        assert _truncate_for_regex(None) == ""

    def test_non_string_returns_empty(self) -> None:
        assert _truncate_for_regex(123) == ""
        assert _truncate_for_regex({"x": 1}) == ""


# ---------------------------------------------------------------------------
# _build_opener
# ---------------------------------------------------------------------------


class TestBuildOpener:
    def test_does_not_have_redirect_handler(self) -> None:
        """Authorization-leak defense: a future maintainer must not "fix"
        the missing HTTPRedirectHandler back into place."""
        opener = _build_opener()
        for handler in opener.handlers:
            assert not isinstance(handler, urllib.request.HTTPRedirectHandler), (
                "_build_opener must NOT install HTTPRedirectHandler — "
                "redirects on api.github.com would leak the PAT."
            )

    def test_has_https_handler(self) -> None:
        opener = _build_opener()
        assert any(
            isinstance(h, urllib.request.HTTPSHandler) for h in opener.handlers
        )

    def test_has_http_error_processor(self) -> None:
        """Without HTTPErrorProcessor, a 4xx response is returned as
        a successful object — GitHub's `{"message": "..."}` error
        body would parse as a dict and the event loop would silently
        return False without emitting the fail-closed warning."""
        opener = _build_opener()
        assert any(
            isinstance(h, urllib.request.HTTPErrorProcessor)
            for h in opener.handlers
        ), (
            "_build_opener must install HTTPErrorProcessor — without "
            "it, 4xx/5xx responses bypass the fail-closed branch."
        )

    def test_has_http_default_error_handler(self) -> None:
        """HTTPErrorProcessor routes 4xx/5xx through the error chain;
        HTTPDefaultErrorHandler is the chain's terminal handler that
        actually raises HTTPError. Without it, OpenerDirector.error()
        raises KeyError because no handler claimed the proto."""
        opener = _build_opener()
        assert any(
            isinstance(h, urllib.request.HTTPDefaultErrorHandler)
            for h in opener.handlers
        ), (
            "_build_opener must install HTTPDefaultErrorHandler — "
            "without it, the error chain terminates in KeyError "
            "rather than HTTPError."
        )


class TestFetchAgainstFakeWire:
    """Exercises the REAL `_build_opener` and `_fetch_public_events`
    against a mocked HTTPS handler. The other test classes monkeypatch
    `_fetch_public_events` directly, which would miss any regression
    in the opener's handler stack itself."""

    def _install_fake_https(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status: int = 200,
        body: bytes = b"[]",
    ) -> dict[str, Any]:
        """Replace `_build_opener` with an opener whose only handler
        is a stub HTTPSHandler that synthesises a Response at the
        requested status + body. Returns a captured-request dict."""
        captured: dict[str, Any] = {}

        class _FakeResponse:
            def __init__(self) -> None:
                self.status = status
                self.code = status
                # urllib's HTTPErrorProcessor looks at .status to
                # decide whether to raise HTTPError.
                self.headers = {}
                self.msg = f"status {status}"
                self.url = ""
                self._body = body
                self.closed = False

            def read(self) -> bytes:
                return self._body

            def info(self) -> dict[str, str]:
                return {}

            def geturl(self) -> str:
                return self.url

            def getcode(self) -> int:
                return status

            def close(self) -> None:
                # HTTPError keeps a reference to .close so it can
                # release the underlying file. Without this attribute
                # the raised exception triggers an unraisable
                # AttributeError on cleanup.
                self.closed = True

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *_: Any) -> None:
                self.close()

        class _FakeHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, request: urllib.request.Request) -> Any:
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                resp = _FakeResponse()
                resp.url = request.full_url
                return resp

        def fake_build_opener() -> urllib.request.OpenerDirector:
            opener = urllib.request.OpenerDirector()
            opener.add_handler(_FakeHTTPSHandler())
            opener.add_handler(urllib.request.HTTPErrorProcessor())
            opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
            return opener

        monkeypatch.setattr(dead_mans_switch, "_build_opener", fake_build_opener)
        return captured

    def test_200_with_list_body_returns_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_fake_https(monkeypatch, status=200, body=b'[{"type":"PushEvent"}]')
        out = _fetch_public_events("alice", token=None)
        assert out == [{"type": "PushEvent"}]

    def test_4xx_raises_http_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of TestBuildOpener.test_has_http_error_processor:
        a 401 with a JSON error body must raise HTTPError, NOT silently
        parse as a dict and return."""
        self._install_fake_https(
            monkeypatch,
            status=401,
            body=b'{"message":"Bad credentials"}',
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _fetch_public_events("alice", token="expired")
        assert exc_info.value.code == 401

    def test_4xx_routes_to_fail_closed_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """End-to-end: a 4xx from the wire propagates through
        _fetch_public_events into _has_recent_public_activity's
        fail-closed branch and emits the warning. Without the
        HTTPErrorProcessor in _build_opener, this test would hang on
        the function returning False with NO warning text."""
        self._install_fake_https(
            monkeypatch,
            status=401,
            body=b'{"message":"Bad credentials"}',
        )
        result = _has_recent_public_activity(
            username="alice",
            token="expired",
            since=hours_ago(24),
            dms_repo_full_name="alice/dms",
            author_patterns=[],
            message_patterns=[],
        )
        assert result is False
        out = capsys.readouterr().out
        assert "::warning::Public activity check unavailable" in out

    def test_200_with_non_list_body_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Schema-drift guard: a successful 200 whose body is a dict
        or any non-list (future API change, surprising error path)
        must fail closed via the JSONDecodeError catch, NOT silently
        treat as empty events."""
        self._install_fake_https(
            monkeypatch,
            status=200,
            body=b'{"unexpected":"shape"}',
        )
        result = _has_recent_public_activity(
            username="alice",
            token=None,
            since=hours_ago(24),
            dms_repo_full_name="alice/dms",
            author_patterns=[],
            message_patterns=[],
        )
        assert result is False
        assert "::warning::Public activity check unavailable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _fetch_public_events  (the actual urllib seam — header verification)
# ---------------------------------------------------------------------------


class TestFetchPublicEvents:
    def test_request_includes_required_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub returns 403 without a User-Agent; the Accept header
        and X-GitHub-Api-Version are also mandatory. Auth header should
        only be present when a token is supplied. The timeout argument
        bounds a hung response so the 15-minute job timeout isn't the
        only backstop — a mutation that drops the timeout kwarg would
        survive without the explicit assertion below."""
        captured: dict[str, Any] = {}

        class _FakeResp:
            def read(self) -> bytes:
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _FakeOpener:
            handlers: list[Any] = []

            def open(self, request: urllib.request.Request, timeout: float) -> Any:
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                captured["timeout"] = timeout
                return _FakeResp()

        monkeypatch.setattr(dead_mans_switch, "_build_opener", lambda: _FakeOpener())

        out = _fetch_public_events("alice", token="abc123")
        assert out == []
        assert captured["url"] == "https://api.github.com/users/alice/events/public"
        # urllib title-cases header names
        assert captured["headers"]["User-agent"] == "dead-mans-switch"
        assert captured["headers"]["Accept"] == "application/vnd.github+json"
        assert captured["headers"]["X-github-api-version"] == "2022-11-28"
        assert captured["headers"]["Authorization"] == "Bearer abc123"
        assert captured["timeout"] == dead_mans_switch.EVENTS_API_TIMEOUT_SECONDS

    def test_no_auth_header_when_token_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeResp:
            def read(self) -> bytes:
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _FakeOpener:
            def open(self, request: urllib.request.Request, timeout: float) -> Any:
                captured["headers"] = dict(request.header_items())
                return _FakeResp()

        monkeypatch.setattr(dead_mans_switch, "_build_opener", lambda: _FakeOpener())

        _fetch_public_events("alice", token=None)
        assert "Authorization" not in captured["headers"]

    def test_no_auth_header_when_token_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeResp:
            def read(self) -> bytes:
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _FakeOpener:
            def open(self, request: urllib.request.Request, timeout: float) -> Any:
                captured["headers"] = dict(request.header_items())
                return _FakeResp()

        monkeypatch.setattr(dead_mans_switch, "_build_opener", lambda: _FakeOpener())
        _fetch_public_events("alice", token="")
        assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# _is_bot_event
# ---------------------------------------------------------------------------


class TestIsBotEvent:
    def test_missing_actor_is_bot(self) -> None:
        """Over-fire: missing actor field → classify as bot, not human."""
        event = {"type": "PushEvent"}
        assert _is_bot_event(event, [], []) is True

    def test_actor_not_a_dict_is_bot(self) -> None:
        event = {"type": "PushEvent", "actor": "alice"}
        assert _is_bot_event(event, [], []) is True

    def test_missing_login_is_bot(self) -> None:
        event = {"type": "PushEvent", "actor": {}}
        assert _is_bot_event(event, [], []) is True

    def test_empty_login_is_bot(self) -> None:
        event = {"type": "PushEvent", "actor": {"login": ""}}
        assert _is_bot_event(event, [], []) is True

    def test_login_not_a_string_is_bot(self) -> None:
        event = {"type": "PushEvent", "actor": {"login": 123}}
        assert _is_bot_event(event, [], []) is True

    def test_bracket_bot_suffix(self) -> None:
        event = gh_event(login="dependabot[bot]")
        assert _is_bot_event(event, [], []) is True

    def test_endswith_not_startswith(self) -> None:
        """`[bot]extra` is NOT a bot — guards against an `endswith → startswith`
        mutation that would let a malicious actor named `[bot]something`
        masquerade as a human."""
        event = gh_event(login="[bot]extra")
        assert _is_bot_event(event, [], []) is False

    def test_human_with_bot_in_name_is_not_bot(self) -> None:
        """`realuser-bot`, `robotron` should NOT be classified as bots —
        the suffix check must not be a substring search."""
        assert _is_bot_event(gh_event(login="realuser-bot"), [], []) is False
        assert _is_bot_event(gh_event(login="robotron"), [], []) is False

    def test_custom_author_pattern_matches(self) -> None:
        event = gh_event(login="renovate-app")
        patterns = [re.compile(r"renovate")]
        assert _is_bot_event(event, patterns, []) is True

    def test_no_message_patterns_short_circuits(self) -> None:
        """When `message_patterns` is empty, payload is never walked."""
        event = gh_event(
            login="alice",
            payload={"pull_request": {"title": "chore(deps): bump x"}},
        )
        assert _is_bot_event(event, [], []) is False

    def test_payload_not_a_dict_is_not_bot(self) -> None:
        event = {"type": "PushEvent", "actor": {"login": "alice"}, "payload": "junk"}
        patterns = [re.compile(r"chore")]
        assert _is_bot_event(event, [], patterns) is False

    @pytest.mark.parametrize("path", list(MESSAGE_PATTERN_PAYLOAD_PATHS))
    def test_message_pattern_matches_at_each_payload_path(
        self, path: tuple[str, ...]
    ) -> None:
        """Every documented payload path must be reachable by the
        message-pattern matcher. Catches a future refactor that
        accidentally drops one of the paths."""
        # Build a nested payload that puts "AUTOMATED" at exactly this path.
        leaf_value: Any = "this looks AUTOMATED to me"
        node: Any = leaf_value
        for key in reversed(path):
            node = {key: node}
        event = gh_event(login="alice", payload=node)
        patterns = [re.compile(r"AUTOMATED")]
        assert _is_bot_event(event, [], patterns) is True

    def test_message_pattern_unrelated_payload_does_not_match(self) -> None:
        """Sibling regression to `test_*_matches_at_each_payload_path`:
        if the event has a payload but none of the documented paths
        carry the matching text, no match. Single test (not
        parametrized) because the `any(p.search(...) for path in ...)`
        loop is short-circuit — a failure here covers all paths."""
        event = gh_event(
            login="alice",
            payload={"unrelated": {"key": "AUTOMATED"}},
        )
        patterns = [re.compile(r"AUTOMATED")]
        assert _is_bot_event(event, [], patterns) is False

    def test_two_patterns_only_one_matches(self) -> None:
        """`any` semantics — catches an `any → all` mutation."""
        event = gh_event(
            login="alice",
            payload={"issue": {"title": "matches only second"}},
        )
        patterns = [re.compile(r"never-matches"), re.compile(r"second")]
        assert _is_bot_event(event, [], patterns) is True

    def test_empty_payload_text_is_not_bot(self) -> None:
        """An empty string at the path shouldn't trigger the regex even if
        the regex would technically match an empty string."""
        event = gh_event(login="alice", payload={"issue": {"body": ""}})
        patterns = [re.compile(r".*")]
        # _truncate_for_regex returns "" for empty input; the `if text and ...`
        # guard short-circuits, so no match.
        assert _is_bot_event(event, [], patterns) is False

    def test_payload_path_node_not_a_dict_skips(self) -> None:
        """Mid-path a non-dict node aborts the walk for that path without
        crashing."""
        event = gh_event(
            login="alice",
            payload={"pull_request": "not-a-dict"},
        )
        patterns = [re.compile(r"anything")]
        # Should not raise, should return False (no match).
        assert _is_bot_event(event, [], patterns) is False


# ---------------------------------------------------------------------------
# _has_recent_public_activity
# ---------------------------------------------------------------------------


class TestHasRecentPublicActivity:
    def _call(
        self,
        *,
        since_hours_ago: float = 24,
        dms_repo: str = "alice/dead-mans-switch",
        author_patterns: list[re.Pattern[str]] | None = None,
        message_patterns: list[re.Pattern[str]] | None = None,
    ) -> bool:
        return _has_recent_public_activity(
            username="alice",
            token=None,
            since=hours_ago(since_hours_ago),
            dms_repo_full_name=dms_repo,
            author_patterns=author_patterns or [],
            message_patterns=message_patterns or [],
        )

    def test_returns_true_on_human_event(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = [
            gh_event(login="alice", created_at=hours_ago(1)),
        ]
        assert self._call() is True

    def test_override_emits_operator_notice(
        self,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A successful override must surface in the Actions log so the
        operator can see that the safety net fired today. A mutation
        that drops the `::notice::Public activity override: keeping
        ALIVE ...` print would otherwise survive (return value
        unchanged) — silent loss of observability."""
        fake_github_events.events = [
            gh_event(
                login="alice",
                event_type="PushEvent",
                repo_name="alice/elsewhere",
                created_at=hours_ago(1),
            ),
        ]
        assert self._call() is True
        out = capsys.readouterr().out
        assert "::notice::Public activity override: keeping ALIVE" in out
        assert "PushEvent" in out
        assert "alice/elsewhere" in out

    @pytest.mark.parametrize("event_type", sorted(LIVENESS_EVENT_TYPES))
    def test_every_liveness_event_type_counts(
        self,
        fake_github_events: FakeGitHubEventsState,
        event_type: str,
    ) -> None:
        """Every member of LIVENESS_EVENT_TYPES must reach the True
        path. A mutation that drops, say, ``DiscussionEvent`` from
        the frozenset would otherwise silently make discussions stop
        counting as liveness — and the rest of the integration tests
        only use ``PushEvent``."""
        fake_github_events.events = [
            gh_event(
                login="alice",
                event_type=event_type,
                repo_name="alice/elsewhere",
                created_at=hours_ago(1),
            ),
        ]
        assert self._call() is True

    def test_event_not_a_dict_is_skipped(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = [
            "junk-string",  # type: ignore[list-item]
            gh_event(login="alice", created_at=hours_ago(1)),
        ]
        assert self._call() is True

    def test_skips_non_liveness_event_type(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        # WatchEvent is not in LIVENESS_EVENT_TYPES.
        assert "WatchEvent" not in LIVENESS_EVENT_TYPES
        fake_github_events.events = [
            gh_event(event_type="WatchEvent", login="alice", created_at=hours_ago(1)),
        ]
        assert self._call() is False

    def test_skips_dms_repo_event(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = [
            gh_event(
                login="alice",
                repo_name="alice/dead-mans-switch",
                created_at=hours_ago(1),
            ),
        ]
        assert self._call(dms_repo="alice/dead-mans-switch") is False

    def test_repo_not_a_dict_still_processed(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """Missing repo info shouldn't crash — the event is still counted
        unless the bot-filter rejects it."""
        ev = gh_event(login="alice", created_at=hours_ago(1))
        ev["repo"] = "not-a-dict"
        fake_github_events.events = [ev]
        assert self._call() is True

    def test_skips_event_without_created_at(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        ev = gh_event(login="alice")
        del ev["created_at"]
        fake_github_events.events = [ev]
        assert self._call() is False

    def test_skips_event_with_non_string_created_at(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        ev = gh_event(login="alice")
        ev["created_at"] = 12345  # type: ignore[assignment]
        fake_github_events.events = [ev]
        assert self._call() is False

    def test_skips_event_with_unparseable_created_at(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """`datetime.fromisoformat('not-a-date')` raises ValueError; the
        per-event try/except must catch it, log, and continue."""
        ev = gh_event(login="alice", created_at="not-a-date")
        fake_github_events.events = [ev, gh_event(login="alice", created_at=hours_ago(1))]
        assert self._call() is True

    def test_event_older_than_since_is_skipped(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = [
            gh_event(login="alice", created_at=hours_ago(72)),
        ]
        # 24h lookback, event is 72h old.
        assert self._call(since_hours_ago=24) is False

    def test_event_at_exact_threshold_is_skipped(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """Boundary check: event_time strictly less than `since` is
        skipped. An event at exactly `since` passes (catches the
        `< → <=` mutation)."""
        now = datetime.now(timezone.utc)
        # Event at exactly `now - 24h`; lookback also 24h → boundary.
        ev = gh_event(login="alice", created_at=now - timedelta(hours=24))

        # since = now - 24h. event_time == since → event_time < since is False
        # → event survives the boundary. Expect True (kept alive).
        fake_github_events.events = [ev]
        assert _has_recent_public_activity(
            username="alice",
            token=None,
            since=now - timedelta(hours=24),
            dms_repo_full_name="alice/dms",
            author_patterns=[],
            message_patterns=[],
        ) is True

        # Now flip: event slightly older than since.
        ev2 = gh_event(login="alice", created_at=now - timedelta(hours=25))
        fake_github_events.events = [ev2]
        assert _has_recent_public_activity(
            username="alice",
            token=None,
            since=now - timedelta(hours=24),
            dms_repo_full_name="alice/dms",
            author_patterns=[],
            message_patterns=[],
        ) is False

    def test_skips_bot_events(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = [
            gh_event(login="dependabot[bot]", created_at=hours_ago(1)),
        ]
        assert self._call() is False

    def test_empty_response_returns_false(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.events = []
        assert self._call() is False

    def test_http_error_fails_closed(
        self,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_github_events.raise_exception = urllib.error.HTTPError(
            "https://api.github.com/users/alice/events/public",
            401,
            "Unauthorized",
            {},  # type: ignore[arg-type]
            None,
        )
        assert self._call() is False
        captured = capsys.readouterr()
        assert "::warning::" in captured.out
        assert "Public activity check unavailable" in captured.out
        # Don't leak the underlying error type/url — the warning is
        # generic by design.
        assert "401" not in captured.out
        assert "Unauthorized" not in captured.out

    def test_url_error_fails_closed(
        self,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_github_events.raise_exception = urllib.error.URLError("DNS fail")
        assert self._call() is False
        assert "::warning::" in capsys.readouterr().out

    def test_json_decode_error_fails_closed(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.raise_exception = json.JSONDecodeError(
            "Expecting value", "garbage", 0
        )
        assert self._call() is False

    def test_timeout_fails_closed(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        fake_github_events.raise_exception = TimeoutError("slow")
        assert self._call() is False

    def test_filter_bug_propagates(
        self, fake_github_events: FakeGitHubEventsState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filter-logic bug (RuntimeError) must NOT be silently
        swallowed by the per-event try/except — that catches only
        KeyError/AttributeError/TypeError/ValueError. Programmer
        errors propagate loudly."""
        fake_github_events.events = [gh_event(login="alice", created_at=hours_ago(1))]

        def boom(_event: Any, _ap: Any, _mp: Any) -> bool:
            raise RuntimeError("intentional filter bug")

        monkeypatch.setattr(dead_mans_switch, "_is_bot_event", boom)
        with pytest.raises(RuntimeError, match="intentional filter bug"):
            self._call()

    @pytest.mark.parametrize(
        "exc_type",
        [KeyError, AttributeError, TypeError, ValueError],
    )
    def test_filter_bug_with_caught_types_still_propagates(
        self,
        fake_github_events: FakeGitHubEventsState,
        monkeypatch: pytest.MonkeyPatch,
        exc_type: type[Exception],
    ) -> None:
        """Stronger version of test_filter_bug_propagates: even when
        `_is_bot_event` raises one of the four exception types the
        event-extraction try/except DOES catch, the filter call
        itself must NOT be inside that try (plan §2.5). A mutation
        that re-wrapped `_is_bot_event` inside the per-event try
        would survive `test_filter_bug_propagates` (which uses
        RuntimeError) but fail this parametrized version."""
        fake_github_events.events = [gh_event(login="alice", created_at=hours_ago(1))]

        def raises_caught_type(_event: Any, _ap: Any, _mp: Any) -> bool:
            raise exc_type(f"filter bug raising {exc_type.__name__}")

        monkeypatch.setattr(dead_mans_switch, "_is_bot_event", raises_caught_type)
        with pytest.raises(exc_type):
            self._call()

    def test_null_actor_event_skipped_not_crashed(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """A real-world malformed event where `actor` is null reaches
        `_has_recent_public_activity`. The over-fire isinstance guard
        in `_is_bot_event` returns True (skip), so the event doesn't
        count and the function returns False without crashing."""
        ev = gh_event(login="alice", created_at=hours_ago(1))
        ev["actor"] = None
        fake_github_events.events = [ev]
        assert self._call() is False

    def test_http_client_exception_fails_closed(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """`http.client.HTTPException` (BadStatusLine, IncompleteRead,
        etc.) is NOT a subclass of `URLError` — without an explicit
        catch, a wire-level glitch would crash the workflow instead of
        falling back to commit-only liveness."""
        import http.client

        fake_github_events.raise_exception = http.client.BadStatusLine("garbage")
        assert self._call() is False

    def test_iso_timestamp_with_z_suffix_parses(
        self, fake_github_events: FakeGitHubEventsState
    ) -> None:
        """GitHub uses `2024-01-15T12:34:56Z` (trailing Z) — Python 3.13+
        parses this natively, no `.replace('Z', '+00:00')` shim needed."""
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fake_github_events.events = [gh_event(login="alice", created_at=recent)]
        assert self._call() is True

    def test_malformed_event_logs_warning(
        self,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The per-event warning must surface the event type (when
        available) so operators can debug from the Actions log."""
        ev = gh_event(login="alice", event_type="PushEvent", created_at="not-a-date")
        fake_github_events.events = [ev]
        self._call()
        out = capsys.readouterr().out
        assert "::warning::Skipping malformed public event" in out
        assert "PushEvent" in out


# ---------------------------------------------------------------------------
# _consult_public_activity   (the DeadMansSwitch wrapper)
# ---------------------------------------------------------------------------


class TestConsultPublicActivity:
    def test_no_username_warns_and_returns_false(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "true")
        # Both GH_USERNAME and GITHUB_REPOSITORY_OWNER unset.
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert dms._consult_public_activity() is False
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "no GH_USERNAME" in out

    def test_clamps_lookback_to_30_days_with_notice(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _enable_feature(monkeypatch)
        # 60-day interval — should clamp to 30 days.
        dms = DeadMansSwitch(60 * 24, 0, armed=False, manual_dispatch=False)
        fake_github_events.events = []
        before = datetime.now(timezone.utc)
        dms._consult_public_activity()
        after = datetime.now(timezone.utc)
        out = capsys.readouterr().out
        assert "::notice::" in out
        assert f"{EVENTS_API_MAX_DAYS}d" in out
        assert f"{EVENTS_API_MAX_DAYS * 24}h" in out
        # Defends against a mutation to the cap arithmetic that
        # would survive the notice-text check (which only verifies
        # the rendered string, not the actual `since` cutoff).
        assert fake_github_events.last_since is not None
        expected_low = before - timedelta(hours=EVENTS_API_MAX_DAYS * 24)
        expected_high = after - timedelta(hours=EVENTS_API_MAX_DAYS * 24)
        assert expected_low <= fake_github_events.last_since <= expected_high, (
            f"since should be clamped to ~{EVENTS_API_MAX_DAYS}d ago; "
            f"got {fake_github_events.last_since}"
        )

    def test_does_not_clamp_under_30_days(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _enable_feature(monkeypatch)
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        fake_github_events.events = []
        before = datetime.now(timezone.utc)
        dms._consult_public_activity()
        after = datetime.now(timezone.utc)
        out = capsys.readouterr().out
        assert "exceeds GitHub events feed window" not in out
        # And confirm the lookback is the configured 48h, not clamped.
        assert fake_github_events.last_since is not None
        expected_low = before - timedelta(hours=48)
        expected_high = after - timedelta(hours=48)
        assert expected_low <= fake_github_events.last_since <= expected_high

    def test_passes_through_username_and_token(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch, username="bob", token="tok_abc")
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        dms._consult_public_activity()
        assert fake_github_events.last_username == "bob"
        assert fake_github_events.last_token == "tok_abc"

    def test_gh_username_wins_over_runner_owner(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        """When both GH_USERNAME and GITHUB_REPOSITORY_OWNER are set
        and differ, the explicit override wins. A mutation that
        reversed `gh_username or runner_owner` to
        `runner_owner or gh_username` would silently query the wrong
        account. The existing typo-notice test only asserts on the
        notice text — this catches the actual username queried."""
        monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "true")
        monkeypatch.setenv("GH_USERNAME", "charlie")
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "dave")
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        dms._consult_public_activity()
        assert fake_github_events.last_username == "charlie"


# ---------------------------------------------------------------------------
# _get_state integration — 2×2 truth table + ordering invariants
# ---------------------------------------------------------------------------


class TestGetStateIntegration:
    def _stale_repo(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        hours: float = 100,
    ) -> GitRepo:
        """Push the only commit back into the past so interval has passed."""
        # Re-author the initial commit at `hours` ago.
        repo = tmp_repo_with_initial_commit
        # Use the helper to add a fresh stale commit.
        repo.commit("stale heartbeat", when=hours_ago(hours))
        return repo

    def test_alive_when_override_passes_with_stale_commit(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        self._stale_repo(tmp_repo_with_initial_commit)
        fake_github_events.events = [gh_event(login="alice", created_at=hours_ago(1))]
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ALIVE
        assert fake_github_events.call_count == 1

    def test_advances_when_override_returns_false(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        self._stale_repo(tmp_repo_with_initial_commit)
        fake_github_events.events = []  # no activity
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ISSUE_WARNING
        assert fake_github_events.call_count == 1

    def test_skips_override_when_disabled(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        # CHECK_PUBLIC_ACTIVITY explicitly unset.
        self._stale_repo(tmp_repo_with_initial_commit)
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        dms._get_state()
        assert fake_github_events.call_count == 0

    def test_skips_override_when_interval_not_passed(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        # No stale commit — initial commit was just now.
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ALIVE
        assert fake_github_events.call_count == 0

    def test_skips_override_when_manual_dispatch(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        self._stale_repo(tmp_repo_with_initial_commit)
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=True)
        # Manual dispatch must not call the API even when interval has passed.
        dms._get_state()
        assert fake_github_events.call_count == 0

    def test_skips_override_when_already_dead(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        # Plant a PASSED_AWAY bot commit so the switch is in terminal state.
        repo = tmp_repo_with_initial_commit
        repo.commit(
            str(State.PASSED_AWAY),
            author_name=dead_mans_switch.BOT_USERNAME,
            when=hours_ago(1),
        )
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ALREADY_DECLARED_DEAD
        assert fake_github_events.call_count == 0

    def test_skips_override_when_disarmed(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        _enable_feature(monkeypatch)
        self._stale_repo(tmp_repo_with_initial_commit)
        dms = DeadMansSwitch(48, 1, armed=False, manual_dispatch=False)
        assert dms._get_state() is State.DISARMED
        assert fake_github_events.call_count == 0

    def test_dms_repo_self_exclusion(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        """The bot's own warning commit on this repo must not count as
        liveness — only events on OTHER repos."""
        _enable_feature(monkeypatch, repo="alice/dead-mans-switch")
        self._stale_repo(tmp_repo_with_initial_commit)
        # Only event is a push on the DMS repo itself.
        fake_github_events.events = [
            gh_event(
                login="alice",
                repo_name="alice/dead-mans-switch",
                created_at=hours_ago(1),
            ),
        ]
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        # Activity exists, but on the DMS repo → excluded → advances.
        assert dms._get_state() is State.ISSUE_WARNING

    def test_author_pattern_filters_via_integration_path(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        """Configure a BOT_AUTHOR_PATTERNS regex through the env var
        path (the production flow) and verify it filters an event
        via `_is_bot_event` at the integration site. A mutation that
        swapped the positional args in
        `_is_bot_event(event, author_patterns, message_patterns)` →
        `_is_bot_event(event, message_patterns, author_patterns)`
        would survive every direct unit test (they all build the
        lists positionally) but fail here: the only configured
        patterns are author-side, and the event would match them as
        bots only if they reach the author path."""
        _enable_feature(monkeypatch, repo="alice/dead-mans-switch")
        monkeypatch.setenv("BOT_AUTHOR_PATTERNS", "^renovate$")
        self._stale_repo(tmp_repo_with_initial_commit)
        # Only event is a `renovate` push elsewhere — the
        # BOT_AUTHOR_PATTERNS should classify it as a bot, so no
        # human activity is found and the switch advances.
        fake_github_events.events = [
            gh_event(
                login="renovate",
                repo_name="alice/elsewhere",
                created_at=hours_ago(1),
            ),
        ]
        dms = DeadMansSwitch(48, 1, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ISSUE_WARNING

    def test_dms_repo_self_exclusion_two_cron_sequence(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        fake_github_events: FakeGitHubEventsState,
    ) -> None:
        """Realistic two-cron scenario: a prior cron has issued a
        warning (so the bot's own PushEvent is in the events feed),
        but the owner has genuinely committed to a DIFFERENT repo.
        The override must keep them ALIVE — the bot's self-event is
        excluded by the DMS-repo filter, the elsewhere event counts."""
        _enable_feature(monkeypatch, repo="alice/dead-mans-switch")
        repo = self._stale_repo(tmp_repo_with_initial_commit)
        # Simulate that a prior cron has already issued one warning,
        # leaving a bot commit on the DMS repo. (Planted directly via
        # the test helper so we don't have to wire git push.)
        repo.commit(
            str(State.ISSUE_WARNING),
            author_name=dead_mans_switch.BOT_USERNAME,
            when=hours_ago(0.5),
        )
        fake_github_events.events = [
            # The bot's own warning push (would land in the public
            # events feed if the DMS repo is itself public).
            gh_event(
                login="alice",
                repo_name="alice/dead-mans-switch",
                created_at=hours_ago(0.5),
            ),
            # Real owner activity on a different repo.
            gh_event(
                login="alice",
                repo_name="alice/elsewhere",
                created_at=hours_ago(0.25),
            ),
        ]
        dms = DeadMansSwitch(48, 2, armed=True, manual_dispatch=False)
        assert dms._get_state() is State.ALIVE


# ---------------------------------------------------------------------------
# _compile_pattern_list & _parse_check_public_activity
# ---------------------------------------------------------------------------


class TestCompilePatternList:
    def test_empty_input_returns_empty_list(self) -> None:
        assert _compile_pattern_list("", "X") == []

    def test_blank_lines_skipped_among_valid(self) -> None:
        out = _compile_pattern_list("\nfoo\n\n  bar  \n", "X")
        assert len(out) == 2
        assert out[0].pattern == "foo"
        assert out[1].pattern == "bar"

    def test_bad_regex_raises_with_var_name(self) -> None:
        with pytest.raises(DeadMansSwitchException, match="BOT_AUTHOR_PATTERNS"):
            _compile_pattern_list("[unclosed", "BOT_AUTHOR_PATTERNS")

    def test_bad_regex_raises_at_dms_init(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critical: the raise must happen during DeadMansSwitch
        construction, NOT during the first _get_state call."""
        monkeypatch.setenv("BOT_AUTHOR_PATTERNS", "[unclosed")
        with pytest.raises(DeadMansSwitchException, match="BOT_AUTHOR_PATTERNS"):
            DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)

    def test_bad_regex_raises_before_git_work(
        self,
        tmp_repo: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plan §2.10: pattern compilation runs BEFORE
        `_get_remaining_warnings` (the git history walk). A mutation
        that re-ordered these would, on an empty repo (no commits),
        produce a `git log failed` DeadMansSwitchException from the
        history walk INSTEAD OF the actionable `Invalid regex in
        BOT_AUTHOR_PATTERNS` error. This test plants an empty repo +
        bad regex so the ordering is observable: the regex error must
        win."""
        monkeypatch.setenv("BOT_AUTHOR_PATTERNS", "[unclosed")
        with pytest.raises(
            DeadMansSwitchException, match="BOT_AUTHOR_PATTERNS"
        ):
            DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)


class TestParseCheckPublicActivity:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", False),
            ("false", False),
            ("true", True),
        ],
    )
    def test_accepts_canonical_values(self, value: str, expected: bool) -> None:
        assert _parse_check_public_activity(value) is expected

    @pytest.mark.parametrize(
        "bad",
        ["yes", "1", "0", "TRUE", "True", "FALSE", " true ", " true", "true ", "y", "n"],
    )
    def test_strict_rejects_truthy_lookalikes(self, bad: str) -> None:
        with pytest.raises(DeadMansSwitchException, match="CHECK_PUBLIC_ACTIVITY"):
            _parse_check_public_activity(bad)

    def test_raise_at_dms_init(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "yes")
        with pytest.raises(DeadMansSwitchException, match="CHECK_PUBLIC_ACTIVITY"):
            DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)


# ---------------------------------------------------------------------------
# Token-shape validation
# ---------------------------------------------------------------------------


class TestTokenShape:
    @pytest.mark.parametrize(
        "bad_token",
        ["abc\n", "abc\r\n", " abc", "abc ", "abc def", "abc-def", "abc.def", "abc/def"],
    )
    def test_rejects_whitespace_and_special_chars(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        bad_token: str,
    ) -> None:
        _enable_feature(monkeypatch, token=bad_token)
        with pytest.raises(DeadMansSwitchException, match="GH_ACTIVITY_TOKEN"):
            DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)

    def test_accepts_clean_token(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_feature(monkeypatch, token="github_pat_AbcDEF_123_xyz")
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert dms._activity_token == "github_pat_AbcDEF_123_xyz"

    def test_empty_token_is_allowed_and_normalised_to_none(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_feature(monkeypatch)
        # No GH_ACTIVITY_TOKEN set.
        dms = DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        assert dms._activity_token is None

    def test_token_not_validated_when_feature_disabled(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user who set a token but turned the feature off shouldn't
        be blocked at startup — they may be in the middle of disabling
        the feature without rotating the token."""
        monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "false")
        monkeypatch.setenv("GH_ACTIVITY_TOKEN", "anything with spaces")
        # Must not raise.
        DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)


# ---------------------------------------------------------------------------
# GH_USERNAME / GITHUB_REPOSITORY_OWNER typo notice
# ---------------------------------------------------------------------------


class TestUsernameNotice:
    def test_notice_when_username_differs_from_owner(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CHECK_PUBLIC_ACTIVITY", "true")
        monkeypatch.setenv("GH_USERNAME", "alice")
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "bob")
        DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        out = capsys.readouterr().out
        assert "::notice::" in out
        assert "GH_USERNAME" in out
        assert "'alice'" in out
        assert "'bob'" in out

    def test_no_notice_when_username_matches_owner(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _enable_feature(monkeypatch)
        DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        out = capsys.readouterr().out
        assert "::notice::GH_USERNAME" not in out

    def test_no_notice_when_feature_disabled(
        self,
        tmp_repo_with_initial_commit: GitRepo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GH_USERNAME", "alice")
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "bob")
        DeadMansSwitch(48, 0, armed=False, manual_dispatch=False)
        out = capsys.readouterr().out
        assert "::notice::GH_USERNAME" not in out


# ---------------------------------------------------------------------------
# Optional integration test (skipped by default; opt-in via env var)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("DMS_INTEGRATION_GITHUB"),
    reason="set DMS_INTEGRATION_GITHUB=1 to hit api.github.com",
)
def test_real_api_event_shape() -> None:
    """Sanity check that real api.github.com still returns the shape
    we expect. Skipped by default — octocat is heavily rate-limited
    worldwide so we prefer the runner's own username when available."""
    user = os.getenv("GITHUB_REPOSITORY_OWNER") or "torvalds"
    events = _fetch_public_events(user, os.getenv("GH_ACTIVITY_TOKEN"))
    assert isinstance(events, list)

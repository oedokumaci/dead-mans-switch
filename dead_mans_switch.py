"""Dead Man's Switch: emails recipients when git commits stop.

A single-file Python implementation designed to run from a GitHub Actions
cron. Monitors the current git repository for owner activity (any commit
not authored by ``dms_bot``); after ``heartbeat_interval_hours`` of silence
the bot writes a warning commit, then sends emails to the configured
recipients once ``number_of_warnings`` warnings have accrued.

Run modes:
  - disarmed (default): emails are redirected to ``MY_EMAIL`` for test viewing
  - armed: emails go to the recipients in ``emails/*.txt``
  - manual dispatch: bypasses the 24-hour minimum interval and never writes
    state-advancing commits — safe for testing from the Actions GUI

Usage:
    python dead_mans_switch.py <heartbeat_interval_hours> <number_of_warnings>
                               [--armed] [--manual-dispatch]
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import smtplib
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, TypedDict


# The bot's commit author identity. Any commit with this %an value is treated
# as a state-advancing action by the switch (warning/passed away). Commits
# with any other author are treated as owner heartbeats.
BOT_USERNAME = "dms_bot"
BOT_EMAIL = "dms@bot.github.com"

# --- Public-activity liveness signal (opt-in, v2.1.0) ---
# See docs/PLAN_liveness.md for the full design rationale.

GITHUB_API_BASE = "https://api.github.com"
EVENTS_API_TIMEOUT_SECONDS = 10
# GitHub's events feed is capped at 30 days / 300 events; longer
# heartbeat intervals are silently clamped to this floor.
EVENTS_API_MAX_DAYS = 30
# GitHub returns 403 to unset/empty User-Agent — set it explicitly.
EVENTS_API_USER_AGENT = "dead-mans-switch"

# Max bytes of any single string passed to user-regex `.search()`.
# Defends against ReDoS (catastrophic backtracking) on long PR/issue
# bodies. 4 KB fits any real bot signature (which appear in the first
# few hundred chars) without exposing the regex engine to a 1 MB body.
REGEX_INPUT_MAX_BYTES = 4096

# Token-shape sanity check — rejects whitespace/CRLF in pasted secrets,
# mirroring the MY_EMAIL injection check.
TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Known-bot identity convention: actors whose login ends with this
# suffix (e.g. `dependabot[bot]`) are always classified as bots.
KNOWN_BOT_LOGIN_SUFFIX = "[bot]"

# Event types that count as liveness. Passive events
# (WatchEvent/ForkEvent) are deliberately excluded.
LIVENESS_EVENT_TYPES = frozenset({
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "IssueCommentEvent",
    "IssuesEvent",
    "CreateEvent",
    "DeleteEvent",
    "ReleaseEvent",
    "CommitCommentEvent",
    "GollumEvent",
    "MemberEvent",
    "PublicEvent",
    "DiscussionEvent",
    "DiscussionCommentEvent",
})

# Paths inside `event["payload"]` to walk when checking
# BOT_MESSAGE_PATTERNS. The 2025-10-07 PushEvent change removed
# `payload.commits`, so PushEvent has NO text field for message-pattern
# matching and passes through this filter unchanged.
MESSAGE_PATTERN_PAYLOAD_PATHS = (
    ("pull_request", "title"),
    ("pull_request", "body"),
    ("issue", "title"),
    ("issue", "body"),
    ("comment", "body"),
    ("review", "body"),
    ("release", "name"),
    ("release", "body"),
)

# ASCII Unit Separator (`\x1f`) for `git log --pretty=format`. Subprocess
# argv can't carry a literal NUL, and unit-separator is a control character
# that won't appear in commit messages or author names in normal use.
# Note: a determined attacker with push access can put `\x1f` inside %an or
# %ae via `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` env vars — `git config` does
# NOT reject control chars. The `rsplit(sep, 3)` below tolerates this in
# %s (the leftmost field) without crashing, and the bot-identity check
# requires BOTH %an and %ae to match exactly, raising the bar for spoofing.
_GIT_LOG_SEPARATOR = "\x1f"
_GIT_LOG_FORMAT = (
    f"%s{_GIT_LOG_SEPARATOR}%an{_GIT_LOG_SEPARATOR}%ae{_GIT_LOG_SEPARATOR}%cI"
)

# Only `${IDENT}` triggers substitution. Bare `$` (e.g. `$5`, `$HOME` in
# prose, regex with `$` anchors) is left as literal text. This is more
# permissive than `string.Template.substitute`, which raises on any
# unescaped `$` — too strict for realistic email bodies.
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute_env(text: str, source: Mapping[str, str], path: Path) -> str:
    """Replace every ``${VAR}`` in ``text`` with ``source[VAR]``.

    Raises ``DeadMansSwitchException`` (naming the missing variable) if any
    placeholder has no corresponding entry in ``source``. Bare ``$`` is
    left alone.
    """
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        try:
            return source[key]
        except KeyError as e:
            raise DeadMansSwitchException(
                f"Missing environment variable ${{{key}}} for template {path}. "
                "Set it as a repo secret or variable."
            ) from e

    return _PLACEHOLDER_RE.sub(replace, text)


class DeadMansSwitchException(Exception):
    """Raised for any configuration, git, or email error."""


class _OutOfHistoryError(DeadMansSwitchException):
    """Raised by ``Commit.from_last_commit`` when the requested ``skip``
    depth is beyond the available history.

    Distinct from other ``DeadMansSwitchException`` causes (parse failures,
    git subprocess failures, bad timestamps) so the warning-counting loop
    can safely treat *only* this case as "end of history" and let real
    parse errors propagate loudly.
    """


class State(StrEnum):
    """The five possible states the switch can be in on any given run."""

    DISARMED = "disarmed"
    ALIVE = "alive"
    ISSUE_WARNING = "warning issued"
    PASSED_AWAY = "passed away"
    ALREADY_DECLARED_DEAD = "already declared dead"


@dataclass(frozen=True)
class Commit:
    """A git commit's message, author identity, and UTC timestamp.

    ``frozen=True`` for symmetry with ``Email`` and to forbid post-hoc
    mutation that could desynchronise ``user`` from any check that
    relied on it (``is_by_dms_bot`` and friends).
    """

    message: str
    user: str  # `%an`, the author's display name
    email: str  # `%ae`, the author's email
    timestamp: datetime  # always tz-aware in UTC

    @classmethod
    def from_last_commit(cls, i: int = 0) -> Commit:
        """Return the i-th most recent commit (0 = latest).

        Raises:
            DeadMansSwitchException: git failed, no commit exists at that
                depth (often means the checkout was shallow and the loop has
                hit its history horizon), or the output couldn't be parsed.
        """
        try:
            output = subprocess.check_output(
                [
                    "git",
                    "log",
                    "-1",
                    f"--skip={i}",
                    f"--pretty=format:{_GIT_LOG_FORMAT}",
                ],
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise DeadMansSwitchException(f"git log failed: {e}") from e
        if not output:
            raise _OutOfHistoryError(
                f"No commit at skip={i}. In GitHub Actions, ensure "
                "`actions/checkout` uses `fetch-depth: 0` so the script can "
                "see enough history."
            )
        # rsplit with maxsplit=3 so a stray separator inside the commit
        # subject (someone pasted a Ctrl+_ keystroke, copied from a
        # source that used Unit Separator as a delimiter, or set
        # GIT_AUTHOR_NAME with embedded \x1f) stays attached to the
        # message field rather than triggering a parse failure. The
        # format is %s<SEP>%an<SEP>%ae<SEP>%cI — %cI is ISO-8601 (no
        # separators possible) and the rightmost two boundaries are
        # therefore reliable. A stray \x1f in %an or %ae would shift
        # into the message field; the email check in `is_by_dms_bot`
        # still requires exact equality of both name and email.
        parts = output.rsplit(_GIT_LOG_SEPARATOR, 3)
        if len(parts) != 4:
            raise DeadMansSwitchException(
                f"Could not parse git log output: {output!r}"
            )
        message, user, email, timestamp_str = parts
        try:
            timestamp = datetime.fromisoformat(timestamp_str.strip()).astimezone(
                timezone.utc
            )
        except ValueError as e:
            raise DeadMansSwitchException(
                f"Bad timestamp in git log: {timestamp_str!r}"
            ) from e
        return cls(
            message=message, user=user, email=email, timestamp=timestamp
        )

    @property
    def hours_since(self) -> float:
        """Hours between this commit's timestamp and now (UTC)."""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds() / 3600

    def is_by_dms_bot(self) -> bool:
        """True iff this commit was authored by the bot identity.

        Both `%an` AND `%ae` must match. Checking the name alone lets
        anyone with push access spoof a bot commit by setting
        `git -c user.name=dms_bot ...`. Checking both raises the bar:
        the attacker now also needs to forge the email. For a private
        personal repo (the recommended deployment), the only entity
        with push access is the owner themselves — so this collapses
        to a defense against accidental self-collision.
        """
        return self.user == BOT_USERNAME and self.email == BOT_EMAIL

    def write_to_repo(self) -> None:
        """Push an empty commit authored as the bot.

        After a non-zero push, performs an ``ls-remote`` check so that a
        push which actually landed but reported failure (TCP RST after
        success, slow ack, mid-flight SIGTERM) is treated as success
        rather than silently bricking the switch via the next cron's
        ALREADY_DECLARED_DEAD branch.

        Raises:
            DeadMansSwitchException: commit failed, or push failed AND the
                remote does not have our new commit.
        """
        env = os.environ.copy()
        # Force the author/committer identity regardless of the runner's
        # local git config — eliminates a class of fragile-fixture failures.
        env["GIT_AUTHOR_NAME"] = BOT_USERNAME
        env["GIT_AUTHOR_EMAIL"] = BOT_EMAIL
        env["GIT_COMMITTER_NAME"] = BOT_USERNAME
        env["GIT_COMMITTER_EMAIL"] = BOT_EMAIL
        try:
            subprocess.run(
                ["git", "commit", "--allow-empty", "--message", self.message],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            # Surface the underlying git stderr — pre-commit hooks
            # (e.g. mandatory GPG signing on the runner) would otherwise
            # produce an opaque "exit 1" with no diagnostic.
            raise DeadMansSwitchException(
                f"git commit failed: {e}\nstderr: {(e.stderr or '').strip()}"
            ) from e
        local_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        try:
            subprocess.run(
                ["git", "push"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            # The push *might* have actually landed on the remote even
            # though the client reported failure. Ask origin directly.
            if _remote_has_commit(local_sha):
                # Remote has our commit — proceed as if push succeeded.
                # If we instead raised here, the next cron run would
                # observe the terminal state on the remote, bail out as
                # ALREADY_DECLARED_DEAD, and never email anyone.
                print(
                    f"::warning::git push reported failure but the remote "
                    f"already has {local_sha[:8]} — treating as success."
                )
                return
            # Rollback so the local repo doesn't claim state that the remote
            # doesn't reflect. On ephemeral CI runners this is moot, but it
            # matters for local invocations and test cleanup.
            subprocess.run(["git", "reset", "--hard", "HEAD~1"], check=False)
            raise DeadMansSwitchException(
                f"git push failed: {e}\nstderr: {(e.stderr or '').strip()}"
            ) from e


def _remote_has_commit(sha: str) -> bool:
    """Return True iff any remote-tracking ref contains ``sha``.

    Implementation note: an earlier version used
    ``git merge-base --is-ancestor <sha> origin/HEAD``, which broke in
    real CI because ``actions/checkout@v4`` does not always populate the
    symbolic ref ``refs/remotes/origin/HEAD``. The current implementation
    sweeps every ``refs/remotes/origin/*`` ref, so it works regardless of
    whether the symbolic ref is set and regardless of which branch the
    workflow runs on.
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin"], check=True, capture_output=True
        )
    except subprocess.CalledProcessError:
        return False
    # `git branch -r --contains <sha>` lists every remote-tracking branch
    # whose tip is at or descends from <sha>. Non-empty stdout means at
    # least one remote ref already has our commit.
    result = subprocess.run(
        ["git", "branch", "-r", "--contains", sha],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener that REFUSES to follow redirects.

    Python's default ``HTTPRedirectHandler`` propagates the
    ``Authorization`` header on cross-origin redirects (unlike requests
    or curl). A compromised redirect on ``api.github.com`` would
    therefore exfiltrate our PAT. We refuse redirects entirely —
    api.github.com doesn't issue them on the events endpoint in normal
    operation, so any 3xx becomes an ``HTTPError`` and is handled by
    the fail-closed branch in ``_has_recent_public_activity``.

    ``HTTPErrorProcessor`` IS installed — without it a bare
    ``OpenerDirector`` returns 4xx/5xx responses as successful
    objects, breaking the fail-closed warning path (GitHub's
    ``{"message": "..."}`` error body would parse as a dict and the
    event loop would silently iterate over its string keys and emit
    no warning).
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPSHandler())
    # HTTPErrorProcessor inspects the response status; on >= 300 it
    # routes via the OpenerDirector's error chain.
    # HTTPDefaultErrorHandler is the chain's terminal handler that
    # actually raises ``HTTPError``. Both are needed — neither alone
    # is sufficient. ``urllib.request.build_opener()`` installs both
    # by default; the bare ``OpenerDirector`` we use here does not.
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    # Explicitly DO NOT add HTTPRedirectHandler. The test in
    # tests/test_liveness.py asserts this directly so a future
    # maintainer can't "fix" it back.
    return opener


def _fetch_public_events(
    username: str,
    token: str | None,
    timeout: float = EVENTS_API_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """GET ``/users/{username}/events/public``.

    Single test seam for the feature. Returns the parsed JSON list.
    Raises on HTTP error, network error, or JSON parse error — the
    caller decides fail-closed.
    """
    url = f"{GITHUB_API_BASE}/users/{username}/events/public"
    headers = {
        "User-Agent": EVENTS_API_USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        # `Bearer` is the recommended scheme for fine-grained PATs.
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = _build_opener()
    with opener.open(request, timeout=timeout) as resp:
        data = json.loads(resp.read())
    # Schema-drift guard: GitHub's events feed is a JSON array. If a
    # future API change wrapped it (e.g. `{"events": [...]}`), or if
    # an unexpected response somehow returned 200 with a dict body,
    # we want this to fail loudly via the caller's
    # fail-closed-on-JSONDecodeError path rather than silently
    # iterating over the wrong shape.
    if not isinstance(data, list):
        raise json.JSONDecodeError(
            "Expected a JSON list, got " + type(data).__name__, "", 0
        )
    return data


def _truncate_for_regex(value: object) -> str:
    """Coerce to str and cap length to ``REGEX_INPUT_MAX_BYTES``.

    Defends against ReDoS on long PR/issue bodies. Non-string inputs
    (None, dicts, ints) return empty string — the caller treats that
    as "no text to scan".
    """
    if not isinstance(value, str):
        return ""
    if len(value) > REGEX_INPUT_MAX_BYTES:
        return value[:REGEX_INPUT_MAX_BYTES]
    return value


def _is_bot_event(
    event: dict[str, Any],
    author_patterns: list[re.Pattern[str]],
    message_patterns: list[re.Pattern[str]],
) -> bool:
    """True iff this event should be filtered out as bot/automation.

    Over-fire principle: when uncertain (missing actor, empty login,
    malformed payload) we classify as bot. False positives are
    recoverable with one heartbeat commit; false negatives (real death
    not notified) are not.
    """
    actor = event.get("actor")
    if not isinstance(actor, dict):
        return True
    login = actor.get("login")
    if not isinstance(login, str) or not login:
        return True
    if login.endswith(KNOWN_BOT_LOGIN_SUFFIX):
        return True
    # `login` is NOT routed through `_truncate_for_regex` — GitHub
    # caps usernames at 39 characters at signup so the input length
    # is bounded by the upstream contract. The 4 KB cap defends
    # against multi-KB PR/issue bodies; login length is small enough
    # that a pathological `BOT_AUTHOR_PATTERNS` regex would still
    # finish within the 15-minute job timeout. The threat model is
    # single-owner self-DoS only — the patterns are owner-supplied.
    if any(p.search(login) for p in author_patterns):
        return True
    if not message_patterns:
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    # Walk the documented payload paths for the relevant event types.
    # PushEvent has no text field after the 2025-10-07 API change, so
    # PushEvents pass through this filter unchanged on message rules.
    for path in MESSAGE_PATTERN_PAYLOAD_PATHS:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        text = _truncate_for_regex(node)
        if text and any(p.search(text) for p in message_patterns):
            return True
    return False


def _has_recent_public_activity(
    *,
    username: str,
    token: str | None,
    since: datetime,
    dms_repo_full_name: str,
    author_patterns: list[re.Pattern[str]],
    message_patterns: list[re.Pattern[str]],
) -> bool:
    """Return True iff the user has any non-bot, non-DMS-repo public
    event after ``since``.

    Fail-closed: a network-level exception (HTTPError, URLError,
    JSONDecodeError, TimeoutError) emits a ``::warning::`` and returns
    False, so the caller treats the result as "no activity found" and
    falls back to commit-only liveness.

    Per-event errors (missing keys, wrong types) are caught and the
    event is skipped — does NOT crash the loop. Filter-logic bugs
    (e.g. a regex that raises RuntimeError) deliberately propagate.
    """
    try:
        events = _fetch_public_events(username, token)
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        TimeoutError,
        # http.client.HTTPException (BadStatusLine, IncompleteRead,
        # …) does NOT subclass URLError — it can leak out of
        # `h.getresponse()` on a malformed HTTP response from
        # api.github.com. Catch it here so a wire-level glitch fails
        # closed to commit-only liveness instead of crashing the
        # workflow.
        http.client.HTTPException,
    ):
        # Deliberately do NOT log the full exception or URL — that
        # would be a timing oracle telling an adversary exactly when
        # the token expired or the rate-limit kicked in.
        print(
            "::warning::Public activity check unavailable today; "
            "using commit-only liveness."
        )
        return False
    for event in events:
        # The per-event try wraps ONLY the event-extraction logic
        # (parsing fields from the GitHub-supplied dict). A bug in the
        # filter itself (`_is_bot_event`) must propagate — see plan
        # §2.5. If `_is_bot_event` were inside this try, a programmer
        # error that raised one of the four caught types would be
        # silently swallowed and the event counted as a non-bot.
        try:
            if not isinstance(event, dict):
                continue
            if event.get("type") not in LIVENESS_EVENT_TYPES:
                continue
            repo = event.get("repo")
            if isinstance(repo, dict) and repo.get("name") == dms_repo_full_name:
                continue  # the bot's own warnings don't count
            created_at = event.get("created_at")
            if not isinstance(created_at, str):
                continue
            # Python >= 3.13 parses trailing `Z` natively — do NOT add
            # the `.replace("Z", "+00:00")` shim, it's vestigial.
            event_time = datetime.fromisoformat(created_at)
            if event_time < since:
                continue
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            event_type = (
                event.get("type", "unknown") if isinstance(event, dict) else "unknown"
            )
            print(
                f"::warning::Skipping malformed public event "
                f"(type={event_type}): {type(e).__name__}"
            )
            continue
        if _is_bot_event(event, author_patterns, message_patterns):
            continue
        repo_name = repo.get("name", "?") if isinstance(repo, dict) else "?"
        print(
            f"::notice::Public activity override: keeping ALIVE "
            f"(latest event {event.get('type')} on {repo_name} at {created_at})"
        )
        return True
    return False


def _compile_pattern_list(raw: str, var_name: str) -> list[re.Pattern[str]]:
    """Parse a newline-separated regex list.

    Bad regex raises ``DeadMansSwitchException`` at ``DeadMansSwitch``
    construction time, NOT lazily — matches the existing fail-loud
    constructor behaviour (NaN interval check, etc.).
    """
    out: list[re.Pattern[str]] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            out.append(re.compile(line))
        except re.error as e:
            raise DeadMansSwitchException(
                f"Invalid regex in {var_name}: {line!r}: {e}"
            ) from e
    return out


def _parse_check_public_activity(raw: str) -> bool:
    """Strict parser for the ``CHECK_PUBLIC_ACTIVITY`` env var.

    Accepts only the literal strings ``"true"``, ``"false"``, and
    ``""`` (unset). Anything else — including ``"yes"``, ``"1"``,
    ``"TRUE"``, ``" true "`` — raises loudly. Mirrors the strict
    YAML ``case`` parser for ``ARMED`` (defense in depth at both
    layers).
    """
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise DeadMansSwitchException(
        f"CHECK_PUBLIC_ACTIVITY must be 'true', 'false', or empty; "
        f"got {raw!r}"
    )


class SMTPConfig(TypedDict):
    """SMTP host + port for one email provider."""

    server: str
    port: int


@dataclass(frozen=True)
class Email:
    """A parsed email message bound for a single recipient.

    ``frozen=True`` so mutation routes through ``dataclasses.replace``,
    which re-runs ``__post_init__`` and re-validates the new ``to``.
    Otherwise an `email.to = "..."` assignment anywhere in the codebase
    would silently bypass the injection regex.
    """

    to: str
    subject: str
    body: str

    def __post_init__(self) -> None:
        # `re.fullmatch` anchors at both ends; `[^@\s,]` excludes the chars
        # that would otherwise let CRLF/whitespace/comma-separated junk
        # through. Important because env-substituted ${VAR} values flow
        # straight into the `To:` header, and an attacker-controlled multi-
        # line value would otherwise enable SMTP-header injection (Bcc, etc.).
        if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", self.to):
            raise DeadMansSwitchException(f"Invalid email address: {self.to!r}")
        # The `Subject:` header is also env-substituted, so check it for
        # the same class of injection. Python's email package raises
        # `HeaderWriteError` on header values containing any of `\r \n
        # \v \f \x00` at `msg.as_string()` time — that exception would
        # surface AFTER the terminal PASSED_AWAY commit was already
        # pushed, silencing every recipient after the offending one.
        # Catching it at parse time means the pre-flight in
        # `_handle_issue_warning` surfaces it before any commit is written.
        if any(c in self.subject for c in "\r\n\v\f\x00"):
            raise DeadMansSwitchException(
                f"Subject contains a control character: {self.subject!r}. "
                "Embedded CR/LF/VT/FF/NUL are not allowed (SMTP would reject)."
            )

    @classmethod
    def from_txt(cls, path_to_txt: Path) -> Email:
        """Parse an email template file.

        Format::

            To: rcpt@example.com
            Subject: subject line

            body line 1
            body line 2

        Headers (everything before the first blank line) must be
        ``key: value`` pairs. ``To:`` and ``Subject:`` are required and may
        not be duplicated. ``${VAR}`` placeholders anywhere in the file are
        substituted from ``os.environ`` and **must** be set — missing vars
        raise rather than silently leaking the literal placeholder. A bare
        ``$`` not followed by ``{IDENT}`` is left as literal text (so
        ``"$5"`` and ``"$HOME"`` work in bodies without escaping).

        Raises:
            DeadMansSwitchException: file missing, headers malformed/missing,
                env var missing, or the substituted ``To:`` is not a valid
                email address.
        """
        try:
            # utf-8-sig handles UTF-8 with or without BOM.
            content = path_to_txt.read_text(encoding="utf-8-sig")
        except FileNotFoundError as e:
            raise DeadMansSwitchException(
                f"Email file not found: {path_to_txt}"
            ) from e
        except UnicodeDecodeError as e:
            # Note: UnicodeDecodeError subclasses ValueError, not OSError —
            # so the plain `except OSError` below would NOT catch it. Worth
            # surfacing actionably so the user re-saves as UTF-8.
            raise DeadMansSwitchException(
                f"Email file {path_to_txt} is not valid UTF-8: {e}. "
                "Re-save the file with UTF-8 encoding (with or without BOM)."
            ) from e
        except OSError as e:
            raise DeadMansSwitchException(
                f"Cannot read email file {path_to_txt}: {e}"
            ) from e

        # Normalize line endings — Windows CRLF would otherwise leak \r into
        # the email body and look like mojibake in some clients.
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        headers: dict[str, str] = {}
        body_start = len(lines)
        for i, line in enumerate(lines):
            if not line.strip():
                body_start = i + 1
                break
            if ":" not in line:
                raise DeadMansSwitchException(
                    f"Malformed header in {path_to_txt} (no ':'): {line!r}"
                )
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key in headers:
                raise DeadMansSwitchException(
                    f"Duplicate {key!r} header in {path_to_txt}"
                )
            headers[key] = value.strip()

        if "to" not in headers:
            raise DeadMansSwitchException(f"No 'To:' field found in {path_to_txt}")
        if "subject" not in headers:
            raise DeadMansSwitchException(
                f"No 'Subject:' field found in {path_to_txt}"
            )

        body = "\n".join(lines[body_start:]).strip()

        to_email = _substitute_env(headers["to"], os.environ, path_to_txt)
        subject = _substitute_env(headers["subject"], os.environ, path_to_txt)
        body = _substitute_env(body, os.environ, path_to_txt)

        return cls(to=to_email, subject=subject, body=body)


class EmailServer:
    """Authenticated SMTP client for the configured ``MY_EMAIL`` provider.

    Used as a context manager — the connection is opened once and reused for
    every ``send_all`` invocation inside the ``with`` block.
    """

    SECONDS_BETWEEN_EMAILS = 5
    # Socket timeout for every SMTP operation. Without this, a half-open
    # connection (NAT drop, firewall blackhole, server-side hang) could
    # block `smtplib.SMTP.starttls()` or `.quit()` for hours — holding the
    # workflow concurrency lock and preventing the next cron from advancing.
    SMTP_TIMEOUT_SECONDS = 30

    SMTP_CONFIGS: dict[str, SMTPConfig] = {
        "gmail.com": {"server": "smtp.gmail.com", "port": 587},
        "icloud.com": {"server": "smtp.mail.me.com", "port": 587},
        "me.com": {"server": "smtp.mail.me.com", "port": 587},
        "mac.com": {"server": "smtp.mail.me.com", "port": 587},
        "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587},
        "yahoo.com": {"server": "smtp.mail.yahoo.com", "port": 587},
        "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587},
        "protonmail.ch": {"server": "smtp.protonmail.ch", "port": 587},
        "protonmail.com": {"server": "smtp.protonmail.ch", "port": 587},
        "fastmail.com": {"server": "smtp.fastmail.com", "port": 587},
        "zoho.com": {"server": "smtp.zoho.com", "port": 587},
        "zohomail.com": {"server": "smtppro.zoho.com", "port": 587},
        "aol.com": {"server": "smtp.aol.com", "port": 587},
        "gmx.com": {"server": "mail.gmx.com", "port": 587},
        "gmx.net": {"server": "mail.gmx.com", "port": 587},
        "mail.com": {"server": "smtp.mail.com", "port": 587},
        "yandex.com": {"server": "smtp.yandex.com", "port": 587},
        "yandex.ru": {"server": "smtp.yandex.com", "port": 587},
    }

    def __init__(self) -> None:
        email = os.getenv("MY_EMAIL")
        password = os.getenv("MY_PASSWORD")
        if not email:
            raise DeadMansSwitchException(
                "MY_EMAIL environment variable is not set. "
                "Please set it to your email address."
            )
        if not password:
            raise DeadMansSwitchException(
                "MY_PASSWORD environment variable is not set. "
                "For Gmail, use an App Password (not your regular password). "
                "You must enable 2FA first, then generate an App Password."
            )
        # Apply the same injection-safety regex used for recipients. A
        # MY_EMAIL secret pasted with stray whitespace/CRLF (Windows
        # clipboards sometimes append \r\n) would otherwise pass through
        # here, get baked into `msg["From"]`, and crash MIMEMultipart at
        # send time — AFTER the terminal commit has been pushed.
        if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", email):
            raise DeadMansSwitchException(
                f"MY_EMAIL is not a valid email address: {email!r}. "
                "Check for stray whitespace or newlines in the secret value."
            )
        self._email: str = email
        self._password: str = password

        domain = self._email.split("@")[-1].lower()
        smtp_config = self.SMTP_CONFIGS.get(domain)
        if not smtp_config:
            raise DeadMansSwitchException(
                f"Unsupported email provider: {domain}. "
                f"Supported providers: {', '.join(sorted(self.SMTP_CONFIGS.keys()))}"
            )
        self._smtp_config: SMTPConfig = smtp_config
        self._smtp_server: smtplib.SMTP | None = None

    @property
    def email(self) -> str:
        """The configured sender email address."""
        return self._email

    def __enter__(self) -> EmailServer:
        self._smtp_server = self._get_smtp_server()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback_obj: TracebackType | None,
    ) -> None:
        if self._smtp_server:
            try:
                self._smtp_server.quit()
            except Exception:
                # quit() failures during cleanup are not actionable.
                pass
            finally:
                self._smtp_server = None

        if exc_type is not None:
            traceback.print_exc()
        # Implicit `return None` — never suppresses an exception.

    def _get_smtp_server(self) -> smtplib.SMTP:
        """Open a TLS SMTP connection and authenticate."""
        try:
            server = smtplib.SMTP(
                self._smtp_config["server"],
                self._smtp_config["port"],
                timeout=self.SMTP_TIMEOUT_SECONDS,
            )
            server.starttls()
            server.login(self._email, self._password)
            return server
        except smtplib.SMTPAuthenticationError as e:
            raise DeadMansSwitchException(
                "Failed to authenticate with email server. "
                "Check MY_EMAIL and MY_PASSWORD (app password for Gmail). "
                f"Error: {e}"
            ) from e
        except smtplib.SMTPException as e:
            raise DeadMansSwitchException(f"SMTP error occurred: {e}") from e
        except Exception as e:
            raise DeadMansSwitchException(
                f"Failed to connect to email server: {e}"
            ) from e

    def _send(self, email: Email) -> None:
        """Send one email. Requires an active ``with`` block."""
        if not self._smtp_server:
            # The prior fallback opened a fresh TLS+login per email and
            # silently bypassed `SECONDS_BETWEEN_EMAILS` pacing. A caller
            # who forgot the `with` block would still get "successful"
            # delivery in tests but trip rate limits in production.
            raise DeadMansSwitchException(
                "EmailServer must be used as a context manager "
                "(`with EmailServer() as server:`)."
            )
        try:
            msg = MIMEMultipart()
            msg["From"] = self._email
            msg["To"] = email.to
            msg["Subject"] = email.subject
            msg.attach(MIMEText(email.body, "plain"))
            self._smtp_server.send_message(msg)
        except Exception as e:
            raise DeadMansSwitchException(
                f"Failed to send email to {email.to}: {e}"
            ) from e

    def send_all(self, emails: list[Email]) -> None:
        """Send each email, sleeping ``SECONDS_BETWEEN_EMAILS`` between them.

        The sleep is between emails, not after the last one — sending 6
        recipients takes 5*5=25s of inter-send pacing, not 30s.
        """
        for i, email in enumerate(emails):
            self._send(email)
            if i < len(emails) - 1:
                time.sleep(self.SECONDS_BETWEEN_EMAILS)


class DeadMansSwitch:
    """The state machine. Decides what to do given current git history."""

    # Minimum allowed interval. The default cron in dms.yaml runs daily, so
    # anything below 24h cannot be honored at the workflow layer. Manual
    # dispatch bypasses this check so testing isn't blocked.
    HEARTBEAT_CHECK_HOUR_FREQUENCY = 24

    # Negative `hours_since` from `_get_state` indicates clock skew (the
    # last commit's timestamp is in the future). Any drift up to this many
    # hours is absorbed silently — anything bigger is treated as
    # interval-passed to defend against deliberate jamming attempts via
    # future-dated commits or stolen-device cached-token replay.
    CLOCK_DRIFT_TOLERANCE_HOURS = 1.0

    PATH_TO_EMAILS = Path(__file__).parent / "emails"

    def __init__(
        self,
        heartbeat_interval_hours: int | float,
        number_of_warnings: int,
        armed: bool,
        manual_dispatch: bool,
    ) -> None:
        # Type checks before any comparison so we don't TypeError on bad input.
        if not isinstance(armed, bool):
            raise DeadMansSwitchException("Armed must be a boolean")
        if not isinstance(manual_dispatch, bool):
            raise DeadMansSwitchException("Manual dispatch must be a boolean")
        if not isinstance(number_of_warnings, int) or number_of_warnings < 0:
            raise DeadMansSwitchException(
                "Number of warnings must be an integer greater than or equal to 0"
            )
        # NaN and inf both slip past `< HEARTBEAT_CHECK_HOUR_FREQUENCY` and
        # `>= heartbeat_interval_hours` (NaN comparisons are always False;
        # inf is never less than any finite value). Both end up jamming
        # the switch silently in ALIVE forever. Refuse non-finite up front.
        if not math.isfinite(heartbeat_interval_hours):
            raise DeadMansSwitchException(
                f"Heartbeat interval must be a finite number, got "
                f"{heartbeat_interval_hours!r}"
            )
        if (
            not manual_dispatch
            and heartbeat_interval_hours < self.HEARTBEAT_CHECK_HOUR_FREQUENCY
        ):
            raise DeadMansSwitchException(
                f"Heartbeat interval must be at least "
                f"{self.HEARTBEAT_CHECK_HOUR_FREQUENCY} hours. "
                "If you want a shorter interval, change the workflow's cron "
                "to fire at least that often, or use --manual-dispatch."
            )

        self._heartbeat_interval_hours = heartbeat_interval_hours
        self._number_of_warnings = number_of_warnings
        self._armed = armed
        self._manual_dispatch = manual_dispatch

        # Public-activity liveness signal — opt-in, off by default.
        # All validation runs BEFORE _get_remaining_warnings (the git
        # walk below) so a bad regex / bad token / bad parser value
        # fails fast without doing minutes of git subprocess work
        # first. Matches the fail-loud philosophy of the NaN-interval
        # check above. (Plan §2.10.)
        self._check_public_activity = _parse_check_public_activity(
            os.getenv("CHECK_PUBLIC_ACTIVITY", "false")
        )
        self._author_patterns = _compile_pattern_list(
            os.getenv("BOT_AUTHOR_PATTERNS", ""), "BOT_AUTHOR_PATTERNS"
        )
        self._message_patterns = _compile_pattern_list(
            os.getenv("BOT_MESSAGE_PATTERNS", ""), "BOT_MESSAGE_PATTERNS"
        )
        token = os.getenv("GH_ACTIVITY_TOKEN", "")
        if (
            self._check_public_activity
            and token
            and not TOKEN_SHAPE_RE.fullmatch(token)
        ):
            raise DeadMansSwitchException(
                "GH_ACTIVITY_TOKEN contains whitespace, newlines, or "
                "non-base64-safe characters. Re-paste the secret "
                "without trailing whitespace."
            )
        self._activity_token = token or None
        # A typo in GH_USERNAME silently queries someone else's
        # account forever. Emit a NOTICE the user can spot in their
        # first cron log when the override-username differs from the
        # repo owner.
        gh_username = os.getenv("GH_USERNAME", "")
        runner_owner = os.getenv("GITHUB_REPOSITORY_OWNER", "")
        if (
            self._check_public_activity
            and gh_username
            and runner_owner
            and gh_username != runner_owner
        ):
            print(
                f"::notice::GH_USERNAME ({gh_username!r}) differs from "
                f"GITHUB_REPOSITORY_OWNER ({runner_owner!r}). Verify "
                "this is intentional — a typo here silently queries "
                "the wrong account."
            )
        self._gh_username = gh_username or runner_owner

        # Git work last — every cheap validation has already run.
        self._remaining_warnings = self._get_remaining_warnings(number_of_warnings)

    @staticmethod
    def _passed_away_already_committed() -> bool:
        """True iff a ``passed away`` commit by ``dms_bot`` exists anywhere
        in history.

        This is the basis for the "no revival" invariant: once the switch
        has fired once, no subsequent owner commit can un-fire it. Without
        this check, a vacation false-positive followed by the owner pushing
        a heartbeat would reset the warning counter and let the switch fire
        a *second* time months later — recipients getting a duplicate "I'm
        dead" announcement after having already grieved.

        The match is on exact equality of ``%an`` and ``%s`` — NOT
        ``--author=dms_bot`` (which is a regex substring filter that would
        also match e.g. ``alice_dms_bot_x`` or any email containing the
        substring ``dms_bot``).
        """
        try:
            output = subprocess.check_output(
                [
                    "git",
                    "log",
                    f"--format=%an{_GIT_LOG_SEPARATOR}%ae{_GIT_LOG_SEPARATOR}%s",
                ],
                text=True,
            )
        except subprocess.CalledProcessError:
            return False
        target = (
            f"{BOT_USERNAME}{_GIT_LOG_SEPARATOR}"
            f"{BOT_EMAIL}{_GIT_LOG_SEPARATOR}"
            f"{State.PASSED_AWAY}"
        )
        # `str.splitlines()` splits on \r, \v, \f, NEL (\x85), LS, PS, etc.
        # in addition to \n — so a heartbeat subject like "x\rdms_bot..."
        # could falsely match here and brick the switch. `split("\n")` is
        # the only safe form when comparing against the exact target string.
        return target in output.split("\n")

    @staticmethod
    def _get_remaining_warnings(number_of_warnings: int) -> int:
        """Count remaining warnings by walking recent bot commits.

        Returns the number of warnings still to issue (in ``[0, N]``), or
        ``N + 1`` as the "already declared dead" sentinel when a
        ``PASSED_AWAY`` bot commit is present.

        Walks at most ``N + 1`` commits — the ``+ 1`` lets the function see
        the ``PASSED_AWAY`` sentinel even when ``N == 0``.
        """
        # Permanent terminal state — once we've fired, we never re-fire,
        # even if the owner pushes a "revival" commit later.
        if DeadMansSwitch._passed_away_already_committed():
            return number_of_warnings + 1

        remaining = number_of_warnings
        for i in range(number_of_warnings + 1):
            try:
                commit = Commit.from_last_commit(i)
            except _OutOfHistoryError:
                # Genuinely no more commits at this depth — stop counting.
                # Parse errors and git failures are NOT caught here; they
                # propagate so a corrupt commit can't silently undercount
                # warnings (the same silent-fallthrough class Bug 1 closed).
                break
            if not commit.is_by_dms_bot():
                # Anything not authored by the bot resets the count — it's
                # treated as an owner heartbeat, so any earlier bot warnings
                # are stale and shouldn't be considered.
                break
            if commit.message == State.ISSUE_WARNING:
                remaining -= 1
            # Other bot commit messages (none today) are ignored without
            # raising — keeps the function forward-compatible. PASSED_AWAY
            # is handled by the explicit check above and never reaches here.
        return max(remaining, 0)

    def run(self) -> None:
        """Dispatch to the handler for the current state."""
        state = self._get_state()
        match state:
            case State.DISARMED:
                self._handle_disarmed()
            case State.ALIVE:
                print("No action needed")
            case State.ISSUE_WARNING:
                self._handle_issue_warning()
            case State.PASSED_AWAY:
                self._handle_passed_away()
            case State.ALREADY_DECLARED_DEAD:
                print("Repo owner is already declared dead")
            case _ as unhandled:
                # Type-safe against future enum extension: if a new State
                # value is added without an explicit case above, we surface
                # it loudly instead of silently doing nothing.
                raise AssertionError(f"unhandled state: {unhandled!r}")

    def _get_state(self) -> State:
        """Decide which state we're in. See ``State`` for what each means."""
        # ALREADY_DECLARED_DEAD takes precedence over DISARMED so that a
        # user toggling ARMED=false after the switch has already fired
        # doesn't restart daily test emails (which would also keep landing
        # in the inbox of whoever inherited the account).
        if self._remaining_warnings == self._number_of_warnings + 1:
            return State.ALREADY_DECLARED_DEAD
        if not self._armed:
            return State.DISARMED
        last_commit = Commit.from_last_commit()
        hours_since = last_commit.hours_since
        print(f"Hours since last commit: {hours_since}")
        # Future-dated commits beyond a small tolerance are treated as
        # "interval passed" — otherwise a clock-skewed or deliberately
        # back-dated push would freeze the switch in ALIVE forever. Small
        # drift (NTP-level, dev-machine vs runner-clock) is absorbed so it
        # doesn't fire phantom warnings. Equality at the upper boundary
        # also passes so a cron firing exactly at threshold doesn't miss.
        interval_passed = (
            hours_since < -self.CLOCK_DRIFT_TOLERANCE_HOURS
            or hours_since >= self._heartbeat_interval_hours
        )
        if interval_passed:
            # Second-chance gate: only consult public activity when
            # interval has actually passed. Manual dispatch short-
            # circuits to keep state-verification runs deterministic
            # and to avoid burning rate-limit on every "test" click.
            if (
                self._check_public_activity
                and not self._manual_dispatch
                and self._consult_public_activity()
            ):
                return State.ALIVE
            if self._remaining_warnings <= 0:
                return State.PASSED_AWAY
            return State.ISSUE_WARNING
        return State.ALIVE

    def _consult_public_activity(self) -> bool:
        """Return True iff the owner has recent non-bot public
        activity. Wraps ``_has_recent_public_activity`` with the
        runtime-only concerns (username resolution, lookback cap,
        operator-visible logging)."""
        if not self._gh_username:
            print(
                "::warning::CHECK_PUBLIC_ACTIVITY=true but no "
                "GH_USERNAME / GITHUB_REPOSITORY_OWNER is set; "
                "skipping public-activity check."
            )
            return False
        capped_hours = min(
            self._heartbeat_interval_hours,
            EVENTS_API_MAX_DAYS * 24,
        )
        if capped_hours < self._heartbeat_interval_hours:
            print(
                f"::notice::Heartbeat interval "
                f"({self._heartbeat_interval_hours}h) exceeds GitHub "
                f"events feed window ({EVENTS_API_MAX_DAYS}d). "
                f"Looking back {capped_hours}h."
            )
        since = datetime.now(timezone.utc) - timedelta(hours=capped_hours)
        return _has_recent_public_activity(
            username=self._gh_username,
            token=self._activity_token,
            since=since,
            dms_repo_full_name=os.getenv("GITHUB_REPOSITORY", ""),
            author_patterns=self._author_patterns,
            message_patterns=self._message_patterns,
        )

    def _email_template_paths(self) -> list[Path]:
        """List every ``*.txt`` file under the emails directory.

        Skips dotfiles (``.hidden.txt`` etc., which ``Path.glob`` includes
        unlike shell glob) and non-file entries (a directory accidentally
        named ``foo.txt/`` would otherwise reach the parser and raise a
        confusing "Cannot read email file" error).
        """
        return sorted(
            p
            for p in self.PATH_TO_EMAILS.glob("*.txt")
            if p.is_file() and not p.name.startswith(".")
        )

    def _gather_emails(self) -> list[Email]:
        """Parse every ``*.txt`` file in the emails directory."""
        return [Email.from_txt(path) for path in self._email_template_paths()]

    def _handle_disarmed(self) -> None:
        if not self._email_template_paths():
            # Match the ISSUE_WARNING and PASSED_AWAY guards — make the
            # "no templates" case loud. Otherwise a user running the README
            # step 5b (manual armed=false dispatch to verify setup) sees a
            # green workflow with no emails delivered, exactly mimicking
            # successful test mode.
            raise DeadMansSwitchException(
                "No .txt files found in the emails directory. "
                "Did you forget to rename .txt.template files to .txt? "
                "Test mode normally sends one self-test email per file."
            )
        # Validate MY_EMAIL/MY_PASSWORD before template parsing so a
        # first-time user with unset credentials sees the actionable
        # "MY_EMAIL not set" error, not a less-relevant template error.
        server = EmailServer()
        emails = self._gather_emails()
        body_prefix = (
            "Dear DMS User,\n\n"
            "This is a test email to check if the dead man's switch is working.\n"
            "Once you arm the switch, you will not receive these scheduled test emails.\n\n"
        )
        # `replace()` re-runs Email.__post_init__, so the redirected `to`
        # is re-validated against the injection regex.
        redirected = [
            replace(
                e,
                to=server.email,
                subject="Dead Man's Switch Test Email: " + e.subject,
                body=body_prefix + e.body,
            )
            for e in emails
        ]
        with server:
            server.send_all(redirected)

    def _handle_issue_warning(self) -> None:
        # Run the same checks under manual dispatch as under cron, so
        # `armed=true --manual-dispatch` actually verifies the setup. The
        # README step 5b "trigger one manual run to verify templates"
        # depends on this: a user who's already past their heartbeat when
        # they verify reaches ISSUE_WARNING, not DISARMED, and would
        # otherwise short-circuit before parsing any templates.
        if not self._email_template_paths():
            # Same guard as PASSED_AWAY — refuse to advance state when
            # there are no recipients configured. Without this, the bot
            # would happily accumulate warning commits and only surface
            # the empty-dir error when it tried to send the final emails.
            raise DeadMansSwitchException(
                "ISSUE_WARNING reached but no .txt files in emails directory. "
                "Refusing to advance state — without recipients, the switch "
                "would walk through every warning and then crash at the "
                "final stage with nobody to notify. Did you forget to rename "
                ".txt.template files to .txt?"
            )
        # Pre-flight: validate MY_EMAIL/MY_PASSWORD AND actually open the
        # SMTP connection AND parse every template at warning #1, so any
        # setup error (missing credential, *wrong* credential, expired
        # App Password, revoked 2FA, missing ${VAR}, unsupported provider,
        # malformed template) surfaces while the owner is still active to
        # fix it. The `with server:` block does the real TLS+login round
        # trip — without it, a typo'd App Password would pass through every
        # warning and only crash at PASSED_AWAY, by which time the owner
        # may not be alive to act.
        with EmailServer():
            pass
        self._gather_emails()
        if self._manual_dispatch:
            # Manual tests must not advance the warning counter — otherwise
            # clicking "Run workflow" repeatedly would walk the switch
            # toward PASSED_AWAY without any real inactivity. The checks
            # above DID run, so the dry-run still verifies setup.
            print(
                "Would issue warning (skipped — manual dispatch must not "
                "advance state). Templates parsed successfully."
            )
            return
        Commit(
            message=State.ISSUE_WARNING,
            user=BOT_USERNAME,
            email=BOT_EMAIL,
            timestamp=datetime.now(timezone.utc),
        ).write_to_repo()

    def _handle_passed_away(self) -> None:
        if not self._email_template_paths():
            # Refuse to write the terminal commit when there's nothing to
            # send. Otherwise a forgotten .txt.template rename would leave
            # the switch silently bricked with zero recipients notified.
            # Checked before EmailServer config so the actionable error
            # surfaces even if MY_EMAIL is also unset.
            raise DeadMansSwitchException(
                "PASSED_AWAY reached but no .txt files in emails directory. "
                "Refusing to commit terminal state — your switch would "
                "silently send zero emails. Did you forget to rename "
                ".txt.template files to .txt?"
            )
        # Validate MY_EMAIL/MY_PASSWORD before template parsing so a
        # first-time user with unset credentials sees the actionable
        # "MY_EMAIL not set" error, not a less-relevant template error.
        server = EmailServer()
        emails = self._gather_emails()

        if self._manual_dispatch:
            # Manual dispatch: redirect to owner so we never email real
            # recipients during testing, and do NOT write the terminal
            # commit (which would brick the switch).
            body_prefix = (
                "Dear DMS User,\n\n"
                "Dead Man's Switch was armed and manually triggered.\n"
                "Don't worry, we only sent the emails to you, and the "
                "switch was NOT advanced to a terminal state.\n\n"
            )
            redirected = [
                replace(
                    e,
                    to=server.email,
                    subject="Dead Man's Switch Manually Triggered: " + e.subject,
                    body=body_prefix + e.body,
                )
                for e in emails
            ]
            with server:
                server.send_all(redirected)
            return

        # Real PASSED_AWAY: open the SMTP connection FIRST so a transient
        # failure (TLS/auth/DNS) cannot land a terminal commit with zero
        # recipients reached. Then commit terminal state (so a partial
        # send_all failure can't cause duplicate "I'm dead" emails on
        # retry — the next cron sees ALREADY_DECLARED_DEAD and bails out).
        # Trade-off accepted: a total send failure after a successful
        # commit-and-push leaves some recipients un-notified. We prefer
        # that to delivering the mortality message twice.
        with server:
            Commit(
                message=State.PASSED_AWAY,
                user=BOT_USERNAME,
                email=BOT_EMAIL,
                timestamp=datetime.now(timezone.utc),
            ).write_to_repo()
            server.send_all(emails)


def _refuse_if_owner_is_bot() -> None:
    """Abort if the runner's git ``user.name`` is the bot's identity.

    Without this check, the owner's own heartbeat commits would be
    classified as bot warnings (since ``is_by_dms_bot`` only compares
    against ``BOT_USERNAME``), silently jamming the switch.
    """
    try:
        name = subprocess.check_output(
            ["git", "config", "user.name"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        # No local git identity configured — the workflow's per-commit env
        # vars set the bot identity directly, so the owner-side check
        # doesn't apply. Skip silently.
        return
    if name == BOT_USERNAME:
        raise DeadMansSwitchException(
            f"Local git user.name is {BOT_USERNAME!r}, which is the bot's "
            "reserved identity. Your own heartbeat commits would be "
            "misclassified as bot warnings and silently jam the switch. "
            "Run `git config user.name <your name>` and try again."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dead Man's Switch — emails recipients when commits stop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 168 2                     # 168h interval, 2 warnings, test mode
  %(prog)s 72 1 --armed              # armed: 72h interval, 1 warning
  %(prog)s 1 0 --armed --manual-dispatch    # arm + manual test
        """,
    )
    parser.add_argument(
        "heartbeat_interval_hours",
        type=float,
        help=(
            "Hours between required heartbeats (commits). "
            f"Must be >= {DeadMansSwitch.HEARTBEAT_CHECK_HOUR_FREQUENCY} "
            "unless --manual-dispatch is set."
        ),
    )
    parser.add_argument(
        "number_of_warnings",
        type=int,
        help="Number of warning commits before final emails. Must be >= 0.",
    )
    parser.add_argument(
        "--armed",
        action="store_true",
        help="Arm the switch for live operation. Default is test mode.",
    )
    parser.add_argument(
        "--manual-dispatch",
        action="store_true",
        help=(
            "Treat this run as a manual GUI test: bypass the 24h minimum "
            "interval and never write state-advancing commits."
        ),
    )
    args = parser.parse_args()
    _refuse_if_owner_is_bot()

    dms = DeadMansSwitch(
        heartbeat_interval_hours=args.heartbeat_interval_hours,
        number_of_warnings=args.number_of_warnings,
        armed=args.armed,
        manual_dispatch=args.manual_dispatch,
    )
    dms.run()


if __name__ == "__main__":
    main()

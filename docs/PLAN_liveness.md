# Liveness via Public GitHub Activity — Implementation Plan

**Status:** Spec'd, reviewed twice (round-7 + round-8). Ready to implement. v2.1.0 candidate (minor bump — opt-in additive feature).

**Provenance:** Initial research from five independent Opus reviewer agents (round 7). Spec revised after six independent Opus reviewer agents (round 8) including a dedicated security review. Treat the design decisions in §2 and §5 as settled; do not re-litigate without strong new evidence.

## 1. Goal

Add an opt-in second-chance liveness signal: before the switch advances state (`ISSUE_WARNING` or `PASSED_AWAY`), it optionally consults the owner's public GitHub activity across all their repos. If the owner has been active there within the heartbeat window — and the activity isn't bot/automation noise — the switch stays `ALIVE` for one more cycle. The check is OFF by default; turning it on is the user's explicit choice and accepts a defined set of trade-offs.

## 2. Hard design decisions (do NOT re-litigate)

These were settled by the round-7 research and the round-8 security review. Future implementers should not second-guess them without strong new evidence.

1. **Yes/no override, not folded into `hours_since`.** The existing `Commit.hours_since` stays pure git-derived. The activity check returns a boolean.
2. **Insert point in `_get_state`**: after `interval_passed` is computed, before the warning/passed-away branch. **Hard invariant (round-8 R5):** `ALREADY_DECLARED_DEAD` and `DISARMED` short-circuit BEFORE any API call is made. The current `_get_state` already checks them in that order — preserve it.
3. **Lookback window** = `min(heartbeat_interval_hours, 30 * 24)`. The events feed has a hard 30-day ceiling. Emit a `::notice::` when the cap clamps the window (so users diagnosing "did this feature do anything?" can see the floor in the workflow log).
4. **Fail-closed on network or API errors.** Network failure, timeout, 4xx/5xx, JSON parse error → log `::warning::` and proceed as if no activity was found. Preserves the existing safety net during outages.
5. **Per-event errors are caught and skipped** (not raised). A single malformed event from GitHub must not crash the loop. **Resolves the round-7 §7.7 vs §2.4 contradiction** flagged by round-8 R3: the policy is "per-event try/except continues; network and configuration errors raise/fail-closed; programmer-error exceptions in the filter propagate loudly". Concretely: wrap each event's processing in `try/except (KeyError, AttributeError, TypeError, ValueError) as e: print warning; continue`. Do NOT wrap the *filter's logic bugs* — those should crash.
6. **Variable names must avoid `GITHUB_*`.** The workflow's `RESERVED` regex in `dms.yaml` blocks that prefix. Use `GH_*`.
7. **Bot filtering**: actor-level only (`actor.login` ends with `[bot]`, etc.). The 2025-10-07 PushEvent change removed `payload.commits` — filtering at the commit-author-email level via the events feed is no longer possible.
8. **Over-fire principle**: when uncertain, classify as bot. False positives (premature fire) are recoverable with one heartbeat commit; false negatives (real death not notified) are not. **Concretely (round-8 R4):** when `actor` field is missing or `actor.login` is empty, classify as bot (skip the event), NOT as human.
9. **Manual dispatch short-circuits the API call (round-8 R1, R4, R5).** `_consult_public_activity` returns `False` early if `self._manual_dispatch` is True. Rationale: manual dispatch is for setup/verification testing; it should not burn rate-limit and should be deterministic vs. external state.
10. **Patterns compile at `DeadMansSwitch.__init__` time, not lazily (round-8 R1, R2, R3).** A bad regex fails at construction, before `_get_remaining_warnings` does any git work. This matches the existing fail-loud philosophy of the constructor (NaN interval check, etc.).
11. **PAT expiry should NOT be "No expiration" (round-8 R3 M6).** README recommends a 1-year expiration. Fail-closed makes expiry safe; a leaked non-expiring PAT is a forever credential.
12. **SemVer: v2.1.0** (minor, additive opt-in feature). `CHECK_PUBLIC_ACTIVITY=false` default → zero behavior change for existing users. New public symbols (module-level constants, functions) are additive. The "library import" concern is moot — `dead_mans_switch.py` is a script, not a library.

## 3. Configuration surface

### Workflow defaults (`.github/workflows/dms.yaml` `env:` block)

```yaml
env:
  HEARTBEAT_INTERVAL: 336
  NUMBER_OF_WARNINGS: 2
  ARMED: "false"
  CHECK_PUBLIC_ACTIVITY: "false"  # NEW
```

User can override via repo variables (passed through the existing export step).

### Strict parser (CRITICAL — round-8 R3 M2, R4, R5)

`CHECK_PUBLIC_ACTIVITY` must be strictly parsed in Python at `DeadMansSwitch.__init__`, NOT lazily via `.lower() == "true"`. The check accepts only the literal strings `"true"`, `"false"`, and `""` (unset). Anything else (including `"yes"`, `"1"`, `"TRUE"`, `" true "`) raises `DeadMansSwitchException`. This matches the existing strict YAML `case` parser for `ARMED` (defense in depth at both layers).

### User-supplied repo **variables**

| Name                    | Type    | Default | Purpose |
|-------------------------|---------|---------|---------|
| `CHECK_PUBLIC_ACTIVITY` | string  | `"false"` | Master opt-in toggle. Strict-parsed; only `true`/`false`/empty accepted. |
| `GH_USERNAME`           | string  | unset   | Override; if unset, derived from `GITHUB_REPOSITORY_OWNER` (a runner-injected env var, NOT exported by `export_one`). |
| `BOT_AUTHOR_PATTERNS`   | string  | unset   | Newline-separated regex list (bot author logins) |
| `BOT_MESSAGE_PATTERNS`  | string  | unset   | Newline-separated regex list (bot message patterns) |

### User-supplied repo **secret**

| Name                | Default | Purpose |
|---------------------|---------|---------|
| `GH_ACTIVITY_TOKEN` | unset   | Fine-grained PAT. Minimum scope = default public-read floor (no explicit permissions needed). Optional — feature works unauthenticated but with 60/hr rate limit shared with the runner IP pool. |

### Token-shape validation (round-8 R3 H1)

When `GH_ACTIVITY_TOKEN` is set, validate at `EmailServer`-style sanity-check time that the value matches `^[A-Za-z0-9_]+$`. Stray whitespace, CRLF, or non-base64-safe chars (common from clipboard paste) fail fast. Mirrors the existing `MY_EMAIL` regex check.

## 4. Code changes — file-by-file

### `dead_mans_switch.py`

**Add module-level constants** (near `BOT_USERNAME`/`BOT_EMAIL`):

```python
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_HOSTNAME = "api.github.com"
EVENTS_API_TIMEOUT_SECONDS = 10
EVENTS_API_MAX_DAYS = 30   # GitHub's hard ceiling on events/public
EVENTS_API_USER_AGENT = "dead-mans-switch"  # GitHub returns 403 without one

# Max bytes of any single string passed to user-regex .search() — defense
# against ReDoS (catastrophic backtracking) on long PR/issue bodies. 4 KB
# is plenty for real bot signatures, which appear in the first few hundred
# chars. (Round-8 R3 C2.)
REGEX_INPUT_MAX_BYTES = 4096

# Token-shape sanity check — rejects whitespace/CRLF in pasted secrets,
# mirroring the MY_EMAIL injection check. (Round-8 R3 H1.)
TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Known-bot identities — actors whose login ends with [bot]
KNOWN_BOT_LOGIN_SUFFIX = "[bot]"

# Event types that count as liveness. Passive (WatchEvent/ForkEvent) excluded.
LIVENESS_EVENT_TYPES = frozenset({
    "PushEvent", "PullRequestEvent", "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent", "IssueCommentEvent", "IssuesEvent",
    "CreateEvent", "DeleteEvent", "ReleaseEvent", "CommitCommentEvent",
    "GollumEvent", "MemberEvent", "PublicEvent", "DiscussionEvent",
    "DiscussionCommentEvent",
})

# Where to look for text on which BOT_MESSAGE_PATTERNS should match.
# Each entry is a dotted path inside event["payload"]. The 2025-10-07
# PushEvent change removed payload.commits; PushEvent thus has NO text
# field for message-pattern matching. Per-event-type fields below:
# (Round-8 R4 critical finding — original sketch read payload.title etc.
# directly, but real GitHub events nest them.)
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
```

**Imports to add** (round-8 R1, R2, R4 — currently missing from `dead_mans_switch.py`):

```python
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone   # `timedelta` is NEW
from typing import Any, TypedDict   # `Any` is NEW
```

**Add new module-level functions** (after existing module-level helpers):

```python
def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener that REFUSES to follow redirects.

    Python's default ``HTTPRedirectHandler`` propagates the
    ``Authorization`` header on cross-origin redirects, unlike
    ``requests`` or ``curl``. A compromised/poisoned redirect on
    ``api.github.com`` would therefore exfiltrate our PAT. We refuse
    redirects entirely (api.github.com doesn't issue them on the events
    endpoint in normal operation) — any 3xx becomes an HTTPError, which
    the existing fail-closed path handles correctly. (Round-8 R3 C1.)
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPSHandler())
    # Explicitly DO NOT add HTTPRedirectHandler.
    return opener

def _fetch_public_events(
    username: str,
    token: str | None,
    timeout: float = EVENTS_API_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """GET /users/{username}/events/public.

    Single test seam for the feature. Returns the parsed JSON list.
    Raises on HTTP error, network error, or JSON parse error — caller
    decides fail-closed.

    Request headers (round-8 R1 C3, R2 C2, R3 M4):
    - User-Agent: required by GitHub (403 without it).
    - Accept: application/vnd.github+json
    - X-GitHub-Api-Version: 2022-11-28
    - Authorization: Bearer <token>  (only when token is set; Bearer is
      the recommended scheme for fine-grained PATs)
    """
    url = f"{GITHUB_API_BASE}/users/{username}/events/public"
    headers = {
        "User-Agent": EVENTS_API_USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = _build_opener()
    with opener.open(request, timeout=timeout) as resp:
        return json.loads(resp.read())

def _truncate_for_regex(value: object) -> str:
    """Coerce to str and cap length to REGEX_INPUT_MAX_BYTES.

    Defends against ReDoS on long PR/issue bodies. (Round-8 R3 C2.)
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
    """True if this event should be filtered out as bot/automation.

    Tier A: hardcoded — actor.login ends with [bot] OR is empty.
    Tier B: user-supplied regexes against login + event-specific text.

    Over-fire principle (round-8 R4): missing/empty actor → classify as
    bot (skip), NOT as human. Errs toward over-firing the switch.
    """
    actor = event.get("actor")
    if not isinstance(actor, dict):
        return True   # missing actor → over-fire
    login = actor.get("login")
    if not isinstance(login, str) or not login:
        return True   # empty login → over-fire
    if login.endswith(KNOWN_BOT_LOGIN_SUFFIX):
        return True
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
    """Return True iff the user has any non-bot, non-DMS-repo public event
    after ``since``.

    Fail-closed (round-8 §2.4): a network-level exception (HTTPError,
    URLError, JSONDecodeError, TimeoutError) emits a ``::warning::`` and
    returns False, so the caller treats the result as "no activity found"
    and falls back to commit-only liveness.

    Per-event errors (round-8 §2.5): a malformed event (missing keys,
    wrong types) is logged and skipped — does NOT crash the loop.
    """
    try:
        events = _fetch_public_events(username, token)
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        TimeoutError,
    ) as e:
        print(
            f"::warning::Public activity check unavailable today; "
            f"using commit-only liveness."
        )
        # NOTE: deliberately do NOT log the full exception or URL — that
        # would create a timing oracle for an adversary reading the logs
        # to know exactly when the token expired or rate-limit hit.
        # (Round-8 R3 H3.) Operators who need the detail can re-run
        # locally with DMS_DEBUG=1 (see operational logging below).
        return False
    found = False
    for event in events:
        try:
            if event.get("type") not in LIVENESS_EVENT_TYPES:
                continue
            repo = event.get("repo")
            if isinstance(repo, dict) and repo.get("name") == dms_repo_full_name:
                continue  # the bot's own warnings don't count
            created_at = event.get("created_at")
            if not isinstance(created_at, str):
                continue
            event_time = datetime.fromisoformat(created_at)
            # Project requires Python >= 3.13; fromisoformat parses
            # trailing `Z` natively. Do NOT replace `Z` with `+00:00`
            # (round-8 R2 C1 — vestigial dead code from 3.10).
            if event_time < since:
                continue
            if _is_bot_event(event, author_patterns, message_patterns):
                continue
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            print(
                f"::warning::Skipping malformed public event "
                f"(type={event.get('type', 'unknown') if isinstance(event, dict) else 'unknown'}): {type(e).__name__}"
            )
            continue
        # Found a real activity event. Emit operational log so the user
        # can see in the Actions tab that the override saved them today.
        # (Round-8 R5.)
        repo_name = repo.get("name", "?") if isinstance(repo, dict) else "?"
        print(
            f"::notice::Public activity override: keeping ALIVE "
            f"(latest event {event.get('type')} on {repo_name} at {created_at})"
        )
        found = True
        break
    return found

def _compile_pattern_list(raw: str, var_name: str) -> list[re.Pattern[str]]:
    """Parse a newline-separated regex list. Bad regex raises loudly at
    `DeadMansSwitch.__init__` time (round-8 §2.10), NOT lazily."""
    out: list[re.Pattern[str]] = []
    for line in raw.splitlines():
        line = line.strip()
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
    """Strict `true`/`false`/empty parser. Anything else raises
    loudly. (Round-8 R3 M2.)"""
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise DeadMansSwitchException(
        f"CHECK_PUBLIC_ACTIVITY must be 'true', 'false', or empty; "
        f"got {raw!r}"
    )
```

**Wire into `DeadMansSwitch.__init__`** (round-8 §2.10 — compile patterns + validate token shape at construction time):

```python
# Inside DeadMansSwitch.__init__, after the existing validation:
self._check_public_activity = _parse_check_public_activity(
    os.getenv("CHECK_PUBLIC_ACTIVITY", "false")
)
self._author_patterns = _compile_pattern_list(
    os.getenv("BOT_AUTHOR_PATTERNS", ""), "BOT_AUTHOR_PATTERNS"
)
self._message_patterns = _compile_pattern_list(
    os.getenv("BOT_MESSAGE_PATTERNS", ""), "BOT_MESSAGE_PATTERNS"
)
# Token-shape validation (round-8 R3 H1) only when the feature is
# active AND a token is provided. Empty token is allowed (unauthenticated).
token = os.getenv("GH_ACTIVITY_TOKEN", "")
if self._check_public_activity and token and not TOKEN_SHAPE_RE.fullmatch(token):
    raise DeadMansSwitchException(
        "GH_ACTIVITY_TOKEN contains whitespace, newlines, or non-base64-"
        "safe characters. Re-paste the secret without trailing whitespace."
    )
self._activity_token = token or None
# Warn about likely-typo of GH_USERNAME (round-8 R3 M1). A typo here
# silently queries someone else's account forever. Emit a NOTICE the
# user can spot in their first cron log.
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
        f"GITHUB_REPOSITORY_OWNER ({runner_owner!r}). Verify this is "
        "intentional — a typo here silently queries the wrong account."
    )
self._gh_username = gh_username or runner_owner
```

**Wire into `_get_state` (the second-chance gate):**

```python
def _get_state(self) -> State:
    # ALREADY_DECLARED_DEAD precedence — invariant (round-8 §2.2).
    if self._remaining_warnings == self._number_of_warnings + 1:
        return State.ALREADY_DECLARED_DEAD
    if not self._armed:
        return State.DISARMED
    last_commit = Commit.from_last_commit()
    hours_since = last_commit.hours_since
    print(f"Hours since last commit: {hours_since}")
    interval_passed = (
        hours_since < -self.CLOCK_DRIFT_TOLERANCE_HOURS
        or hours_since >= self._heartbeat_interval_hours
    )
    if interval_passed:
        # Second-chance gate. Short-circuits if the feature is disabled
        # OR if this is a manual dispatch run (round-8 §2.9).
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
    if not self._gh_username:
        print(
            "::warning::CHECK_PUBLIC_ACTIVITY=true but no GH_USERNAME / "
            "GITHUB_REPOSITORY_OWNER set."
        )
        return False
    capped_hours = min(
        self._heartbeat_interval_hours,
        EVENTS_API_MAX_DAYS * 24,
    )
    if capped_hours < self._heartbeat_interval_hours:
        print(
            f"::notice::Heartbeat interval ({self._heartbeat_interval_hours}h) "
            f"exceeds GitHub events feed window ({EVENTS_API_MAX_DAYS}d). "
            f"Looking back {capped_hours}h."
        )
    since = datetime.now(timezone.utc) - timedelta(hours=capped_hours)
    repo_full_name = os.getenv("GITHUB_REPOSITORY", "")
    return _has_recent_public_activity(
        username=self._gh_username,
        token=self._activity_token,
        since=since,
        dms_repo_full_name=repo_full_name,
        author_patterns=self._author_patterns,
        message_patterns=self._message_patterns,
    )
```

### `.github/workflows/dms.yaml`

1. Add `CHECK_PUBLIC_ACTIVITY: "false"` to the workflow-level `env:` block.
2. **Verify** that `GH_USERNAME`, `GH_ACTIVITY_TOKEN`, `BOT_AUTHOR_PATTERNS`, `BOT_MESSAGE_PATTERNS` all pass through the existing `export_one` step. (`GH_*` is NOT in the regex.)
3. No new steps required — existing var/secret export handles everything.

### `tests/`

**Add `tests/test_liveness.py`** (or extend `test_bugs.py`). The test matrix below is intentionally exhaustive — the `--cov-fail-under=100` gate plus the existing branch-coverage strictness means every `continue`, every short-circuit, every guard needs at least one case on each side. Round-8 R6 enumerated 22 missing cases; the list below is a superset.

**Fixture:** add a `fake_github_events` fixture to `tests/conftest.py` mirroring the `fake_smtp` pattern — controllable list of events, a flag for "raise on next fetch", a flag for the exception type. Pair it with a small `gh_events_factory` helper that builds realistic event dicts (so a future GitHub schema change is one fixture edit, not 20 test edits).

**Coverage of `_is_bot_event`** (7 branches):
- `test_is_bot_event_missing_actor_is_bot` — `actor` field is `None`.
- `test_is_bot_event_empty_login_is_bot` — `actor.login` is `""`.
- `test_is_bot_event_bracket_bot_suffix` — `dependabot[bot]`.
- `test_is_bot_event_endswith_not_startswith` — `"[bot]extra"` is NOT a bot (catches `endswith → startswith` mutation).
- `test_is_bot_event_human_with_bot_in_name` — `realuser-bot`, `robotron` are NOT bots (catches over-broad substring).
- `test_is_bot_event_custom_author_pattern_matches` — user-supplied regex hits.
- Parametrized over message-pattern payload paths: PR title, PR body, issue title, issue body, comment body, review body, release name, release body. Each path must have a test that matches AND a test where the field is missing (over-fire path: no match → no filter → human).
- `test_is_bot_event_two_patterns_only_one_matches` — `any` semantics (catches `any → all` mutation).
- `test_is_bot_event_empty_payload_text_is_not_bot` — empty body shouldn't trigger message regex.

**Coverage of `_has_recent_public_activity`** (8 branches in the loop):
- `test_has_recent_activity_returns_true_on_human_event`
- `test_has_recent_activity_skips_watch_event_type` — passive event explicitly skipped.
- `test_has_recent_activity_skips_dms_repo_event`
- `test_has_recent_activity_skips_event_without_created_at` — KeyError path.
- `test_has_recent_activity_skips_event_with_unparseable_created_at` — ValueError path.
- `test_has_recent_activity_skips_event_with_null_actor` — AttributeError path (the round-8 R3 C3 case).
- `test_has_recent_activity_event_older_than_since_is_skipped`
- `test_has_recent_activity_event_at_exact_threshold_is_skipped` — `event_time < since` boundary (catches `< → <=` mutation).
- `test_has_recent_activity_skips_bot_events`
- `test_has_recent_activity_empty_response_returns_false`
- `test_has_recent_activity_http_error_fails_closed` — `urllib.error.HTTPError`.
- `test_has_recent_activity_url_error_fails_closed` — `URLError`.
- `test_has_recent_activity_json_decode_error_fails_closed`
- `test_has_recent_activity_timeout_fails_closed`
- `test_has_recent_activity_filter_bug_propagates` — monkeypatch `_is_bot_event` to raise `RuntimeError` (NOT in the per-event catch list) → assert the exception escapes (the round-8 §2.5 policy).

**Coverage of `_consult_public_activity`**:
- `test_consult_no_username_warns_and_returns_false`
- `test_consult_clamps_lookback_to_30_days_with_notice`

**Coverage of `_get_state` integration (2×2 truth table + ordering invariants):**
- `test_get_state_alive_when_override_passes_with_stale_commit` — positive case.
- `test_get_state_advances_when_override_returns_false` — sibling negative case.
- `test_get_state_skips_override_when_disabled` — assert `_fetch_public_events` is never called (use a tracking spy).
- `test_get_state_skips_override_when_interval_not_passed` — fourth quadrant.
- `test_get_state_skips_override_when_manual_dispatch` — the §2.9 invariant.
- `test_get_state_skips_override_when_already_dead` — terminal-state precedence invariant.
- `test_get_state_skips_override_when_disarmed` — disarmed precedence.
- `test_dms_repo_self_exclusion_two_cron_sequence` — write a warning commit; next cron sees a fresh public event + bot's own warning → still ALIVE.

**Coverage of `_compile_pattern_list` and `_parse_check_public_activity`:**
- `test_compile_patterns_empty_input_returns_empty_list`
- `test_compile_patterns_blank_lines_skipped_among_valid`
- `test_compile_patterns_bad_regex_raises_at_init` — assert the raise happens during `DeadMansSwitch(...)` construction, NOT later.
- `test_parse_check_public_activity_strict_rejects_yes_1_TRUE` — parametrized.
- `test_token_shape_check_rejects_whitespace_and_crlf` — parametrized over `"abc\n"`, `" abc"`, `"abc def"`, `""` (only when feature is active and token is non-empty).

**Coverage of `_build_opener` / redirect refusal (round-8 R3 C1):**
- `test_build_opener_does_not_have_redirect_handler` — assert `HTTPRedirectHandler` is not in `opener.handlers`. Defends against a future maintainer "fixing" the missing redirect handler.

**Optional integration test** (skipped by default; mirrors existing pattern at `test_workflow.py:142`):

```python
@pytest.mark.skipif(
    not os.getenv("DMS_INTEGRATION_GITHUB"),
    reason="set DMS_INTEGRATION_GITHUB=1 to hit api.github.com",
)
def test_real_api_event_shape():
    # Use the runner's own username — octocat is heavily rate-limited
    # by demos worldwide and tests against it are flaky.
    # (Round-8 R1 M6.)
    user = os.getenv("GITHUB_REPOSITORY_OWNER") or "octocat"
    events = _fetch_public_events(user, os.getenv("GH_ACTIVITY_TOKEN"))
    assert isinstance(events, list)
```

**Markers**: do NOT use `@pytest.mark.bug` for new tests — that marker is reserved for regressions of previously-shipped bugs (round-8 R2). New-feature tests are unmarked or, if filtering is desired, add a new `liveness` marker to `pyproject.toml`'s `markers` list.

**Workflow lint tests** (in `tests/test_workflow.py`):
- `test_workflow_defaults_check_public_activity_false` — `CHECK_PUBLIC_ACTIVITY: "false"` is the workflow-level default.
- `test_gh_prefix_not_in_reserved_regex` — extract the literal `RESERVED='...'` string from the YAML, real-regex-match `GH_USERNAME` / `GH_ACTIVITY_TOKEN` / `BOT_AUTHOR_PATTERNS` / `BOT_MESSAGE_PATTERNS` against it, assert no match. Catches a future regression where someone adds `GH_*` to the allowlist defensively.

### `README.md`

New section between "Configuration Options" and "Email Setup Guide". Sketch:

```markdown
## 🔍 Optional: GitHub activity as fallback liveness signal

By default, the switch only watches commits in this repo. If you commit
frequently to *other* repos but rarely to this one, you can opt in to
have the switch ALSO check your public GitHub activity before advancing
state.

### Setup

1. Create a fine-grained Personal Access Token:
   https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
   - **Set the expiration to 1 year** (the GitHub maximum). The feature
     fails closed when the token expires — no harm if you miss the
     rotation, just lose this signal. Do NOT use "No expiration":
     a leaked non-expiring PAT is a forever credential.
   - No specific permissions needed beyond the default public-read floor.
2. Add as a repo secret: `GH_ACTIVITY_TOKEN`. Paste **without** trailing
   whitespace or newlines — the script will reject malformed token
   strings at startup.
3. Add a repo variable: `CHECK_PUBLIC_ACTIVITY=true`. Strict-parsed —
   `"yes"`, `"1"`, `"TRUE"` will fail loudly.
4. (Optional) `GH_USERNAME` — only if different from your GitHub
   account that owns this repo. **Triple-check the spelling** — a typo
   here silently queries someone else's activity, and the switch never
   fires. The workflow log emits a `::notice::` on the first cron after
   configuration if `GH_USERNAME` differs from `GITHUB_REPOSITORY_OWNER`.
5. (Optional) `BOT_AUTHOR_PATTERNS` — newline-separated regexes for
   bot logins beyond the built-in `[bot]` suffix.
6. (Optional) `BOT_MESSAGE_PATTERNS` — regexes for PR/issue/comment
   text to ignore (e.g. `^chore\(deps\):`). Each pattern is run against
   at most 4 KB of text per field — defense against catastrophic
   backtracking on long bodies.

### What gets checked

PushEvent, PullRequestEvent, IssuesEvent, IssueCommentEvent, releases,
new repos, comments, discussions. Watching/starring/forking is ignored
(too passive).

### Recommended interval

The GitHub events feed returns at most **30 days** of activity. If your
`HEARTBEAT_INTERVAL` is ≤ 720h (30 days), the feature can fully extend
the heartbeat window. If you set a longer interval, the feature is
silently capped at 30 days — the workflow log emits a `::notice::` so
you'll see it in the Actions tab.

### Hard limitations

- **30-day API ceiling.** As above — heartbeat intervals longer than
  30 days fall back to commit-only liveness automatically.
- **Bot filtering uses actor identity only.** As of GitHub's October
  2025 API change, PushEvents no longer expose per-commit author/message
  data — message filtering only meaningfully applies to PR/issue/
  comment/release events.
- **Self-hosted runners with corporate TLS interception:** if your
  runner uses a custom CA bundle, set `SSL_CERT_FILE` env var
  accordingly. The script does not disable TLS verification.
- **Manual dispatch does NOT call the API.** Manual `Run workflow`
  is for state-safe verification; it short-circuits the activity
  check to avoid burning rate-limit on every "test" click.
- **Failure mode:** if the API is unreachable or your token expires,
  the switch silently falls back to commit-only liveness. **You are
  responsible for rotating the PAT before it expires.**

### Privacy notice

Enabling this feature makes the workflow query `api.github.com` for
your public activity (already world-readable data). Two practical notes:

- The PAT lives in your repo secrets (private repo recommended).
- **Self-hosted runners**: the PAT in your `Authorization` header
  reaches GitHub from your runner's IP. GitHub-side logs will link
  your runner IP to your token. For maximum privacy, leave
  `CHECK_PUBLIC_ACTIVITY=false` and rely on commit-only liveness.

### Threat model addition

This feature **adds GitHub's API correctness to your trusted computing
base.** If GitHub returns false-positive activity for your account
(API bug, MITM, compromise), the switch will treat you as alive and
never fire. Treat this feature as **defense in depth** — it can keep
you alive when you've been quiet in *this* repo but active elsewhere;
it should not be your sole liveness signal.

Your `BOT_AUTHOR_PATTERNS` / `BOT_MESSAGE_PATTERNS` are also part of
the trusted computing base. A too-permissive filter could classify your
own human activity as bot noise and let the switch fire while you're
alive. A too-restrictive filter could let a Dependabot PR-merge mask
real inactivity. **Defaults lean toward over-firing** — if in doubt,
leave the patterns empty.

If you maintain backup mirrors of this repo with the same `dms_bot`
identity, exclude them too — the built-in DMS-repo exclusion only
covers the current repo (`$GITHUB_REPOSITORY`). Add the mirror repos'
bot identity (`dms_bot`) to `BOT_AUTHOR_PATTERNS` if needed.
```

Also update the existing **"🛡️ Privacy & Security Features"** section in the README to acknowledge that the "No Data Collection" claim is conditional: when `CHECK_PUBLIC_ACTIVITY=true`, the workflow does make one outbound GET to `api.github.com` per run.

Also update the existing **"Resetting the switch"** section to add:
> If you keep `CHECK_PUBLIC_ACTIVITY=true` after resetting, the override won't help during the very first cycle after recovery — events older than the reset moment still count and you may have been less active than usual (mourning, hospital, vacation). Give the events feed a few days to catch up.

## 5. Acceptance criteria

A PR is ready to merge when:

- [ ] `uv run pytest` passes — 100% line + branch coverage maintained (enforced via `--cov-fail-under=100`)
- [ ] All tests from §4.3 above are present and passing
- [ ] `CHECK_PUBLIC_ACTIVITY=false` (default) → zero behavior change vs current main; `_fetch_public_events` is never called (verifiable by a tracking spy)
- [ ] `CHECK_PUBLIC_ACTIVITY=true` + no `GH_ACTIVITY_TOKEN` → still works (unauthenticated), emits `::warning::` if rate-limited
- [ ] `CHECK_PUBLIC_ACTIVITY=true` + manual dispatch → API is NOT called
- [ ] Bad regex in `BOT_AUTHOR_PATTERNS` → raises during `DeadMansSwitch(...)` construction, before `_get_state` runs
- [ ] `CHECK_PUBLIC_ACTIVITY=yes` → raises during construction (strict parser)
- [ ] `GH_ACTIVITY_TOKEN=" my-token\r\n"` → raises at startup (token shape check)
- [ ] `GH_USERNAME ≠ GITHUB_REPOSITORY_OWNER` → emits `::notice::` on construction
- [ ] `HEARTBEAT_INTERVAL > 30 days` → emits `::notice::` about the cap when the feature runs
- [ ] When override returns True, emits `::notice::Public activity override: keeping ALIVE ...`
- [ ] Opener has NO `HTTPRedirectHandler`
- [ ] User-Agent header is set to `dead-mans-switch` on every API call (verifiable via a request inspector)
- [ ] Auth header form is `Bearer <token>` when token is set
- [ ] README section added with all sub-sections; the existing "Privacy & Security Features" and "Resetting" sections also updated
- [ ] Workflow YAML has `CHECK_PUBLIC_ACTIVITY` default; no other workflow changes
- [ ] New test markers: NOT using `@pytest.mark.bug` (regression-only) for liveness tests
- [ ] `test_workflow.py` adds the GH_* regex regression test and the strict-parser test
- [ ] No new third-party dependencies (still stdlib-only: `urllib.request`, `urllib.error`, `json`, `re`, `datetime`)

## 6. Out of scope (explicit non-goals)

- **GraphQL `contributionsCollection` fallback** for >30 day windows — flag as future enhancement, document the 30-day ceiling instead
- **Private-repo activity** — requires `Events: read` user-account permission; default is public only
- **Caching** — daily cron, one API call per run, no caching needed
- **Per-event verification** — events feed metadata is trusted (with the README threat-model caveat above)
- **`search/commits` corroboration** — adds complexity, separate rate-limit bucket
- **Configurable activity weight** — keep it boolean
- **Using activity-absence as a positive death signal** — out of scope (round-8 R5). Public activity is a one-way *grant* (can keep you alive); commit activity remains the only positive liveness anchor. A future "fire after N consecutive activity-empty cycles" proposal should not be merged.
- **N-cycle override cap** (defense-in-depth against permanently spoofed events feed) — interesting future enhancement, out of scope for v2.1.0

## 7. Known risks / gotchas

1. **`GITHUB_*` reserved-name regex**: any new var named `GITHUB_FOO` will be silently refused by `export_one`. Use `GH_*` prefix.
2. **PushEvent.commits removed (2025-10-07)**: don't expect commit message data in PushEvents. Message-pattern filtering is for PR/issue/comment/release events only — the `MESSAGE_PATTERN_PAYLOAD_PATHS` constant enumerates exactly where to look.
3. **30-day / 300-event cap**: documented and `::notice::`-logged at runtime when the cap is active.
4. **Token expiry**: fine-grained PATs expire; the recommended 1-year expiry means an unrotated token degrades the feature to commit-only liveness, not catastrophic.
5. **The DMS repo's OWN bot commits**: excluded by `repo.name == GITHUB_REPOSITORY`. Backup mirrors must be excluded via `BOT_AUTHOR_PATTERNS` (see README threat model).
6. **Strict `CHECK_PUBLIC_ACTIVITY` parsing**: implemented in Python at `__init__` (not at the YAML `case` layer, which is for `workflow_dispatch` inputs only).
7. **Exception scopes**: per-event errors → catch + skip; network errors → catch + fail-closed; filter bugs (uncaught types) → propagate loudly. Spell this out in code comments to head off a future "let's just `except Exception:` it all" refactor.
8. **Authorization-leak via redirects**: `_build_opener` deliberately omits `HTTPRedirectHandler`. Do not "fix" this by adding it back.
9. **ReDoS input cap**: every string passed to user-regex `.search()` is capped at `REGEX_INPUT_MAX_BYTES` (4 KB). Do not remove this without a replacement defense.
10. **`GITHUB_REPOSITORY_OWNER` is runner-injected**, NOT routed through `export_one`. It's in the RESERVED regex by design.

## 8. Branch / commit / PR mechanics

- Branch from current `main`
- Single commit on the branch (squash if multiple)
- No `Co-Authored-By` lines
- PR title: `Add opt-in public-GitHub-activity liveness signal`
- Commit message format: `feat: ...` / `docs: ...` prefix as used elsewhere in the repo
- Merge via rebase (repo branch protection allows only rebase merge)
- Update PR description with §5's "Acceptance criteria" as a test checklist
- Tag as `v2.1.0` after merge

## 9. Estimated effort

- Code: ~250 LOC added (the round-8 hardening expanded the original sketch)
- Tests: ~500 LOC added — the matrix in §4.3 is exhaustive and required by `--cov-fail-under=100`
- README: ~120 lines
- Workflow YAML: ~5 lines
- Time: **2-3 days of focused work** for a fresh implementer (revised up from the round-7 estimate to reflect the hardening surface)

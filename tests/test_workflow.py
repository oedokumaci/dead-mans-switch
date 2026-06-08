"""Lint-level invariants for the dms.yaml workflow.

The workflow can't be executed inside pytest, but several text-level
invariants matter for correctness and have already regressed once. These
tests catch the obvious cases where a future edit would silently break
shipping behaviour.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dms.yaml"


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


class TestCheckoutDepth:
    def test_fetch_depth_is_zero(self, workflow: str) -> None:
        """Bug 1's workflow-side fix. Default `fetch-depth: 1` truncates
        warning history and silently undercounts; we need full history."""
        assert re.search(r"fetch-depth:\s*0\b", workflow), (
            "checkout step must set fetch-depth: 0 — see Bug 1 in the review."
        )

    def test_persist_credentials_false(self, workflow: str) -> None:
        """checkout's default-installed token would otherwise linger
        alongside the explicit token we set afterwards."""
        assert re.search(r"persist-credentials:\s*false\b", workflow)


class TestStrictArmedParsing:
    def test_armed_input_is_choice(self, workflow: str) -> None:
        """Free-text `armed` input lets typos silently disarm the switch."""
        assert "type: choice" in workflow
        assert re.search(r"options:\s*\n\s*-\s*'false'\s*\n\s*-\s*'true'", workflow)

    def test_armed_arg_validation(self, workflow: str) -> None:
        """The run step must reject any value that isn't `true`/`false`/``."""
        assert "ARMED value" in workflow
        assert "refusing to run" in workflow


class TestSafeEnvExport:
    def test_uses_base64_encoding(self, workflow: str) -> None:
        """Multi-line secrets (PGP keys, paragraphs) must survive intact.
        Base64 encoding the value side is what makes that work — assert both
        the jq encode and the shell decode are actually present (substring
        matches on 'base64' alone would pass even if either half was deleted)."""
        assert re.search(r"\.value\s*\|\s*@base64", workflow), (
            "jq must base64-encode the value side"
        )
        assert re.search(r"base64\s+-d", workflow), (
            "shell must base64-decode each value"
        )

    def test_reserved_name_guard(self, workflow: str) -> None:
        """A malicious or careless `vars.PATH` must not be exported."""
        assert "RESERVED=" in workflow
        # Spot-check the critical reserved names — PATH/HOME (process state),
        # the LD_*/PYTHONPATH/SHELL family (dynamic-linker influence), and
        # the bash-influence vectors (IFS, BASH_ENV, ENV, SHELLOPTS).
        for name in (
            "PATH", "HOME", "LD_PRELOAD", "PYTHONPATH",
            "IFS", "BASH_ENV", "ENV", "SHELLOPTS", "PROMPT_COMMAND",
        ):
            assert name in workflow, f"reserved name {name!r} should appear in the allowlist"

    def test_reserved_uses_github_wildcard(self, workflow: str) -> None:
        """All GITHUB_* runtime vars (GITHUB_REF, GITHUB_SHA, GITHUB_ACTOR,
        etc.) must be reserved, not just the half-dozen that someone
        happened to think of. Same for RUNNER_*/ACTIONS_*."""
        assert "GITHUB_.+" in workflow
        assert "RUNNER_.+" in workflow
        assert "ACTIONS_.+" in workflow

    def test_uses_heredoc_for_github_env(self, workflow: str) -> None:
        """Plain KEY=VALUE breaks on multi-line; heredoc is required. Check
        the literal heredoc printf calls *and* that they redirect to
        $GITHUB_ENV — the prior loose assertion would still pass if the
        heredoc was written to /tmp by accident."""
        assert re.search(r"printf\s+'%s<<%s\\n'", workflow), "key<<DELIM printf missing"
        assert ">>" in workflow and '"$GITHUB_ENV"' in workflow, (
            "must append to $GITHUB_ENV"
        )

    def test_random_heredoc_delimiter(self, workflow: str) -> None:
        """A static `EOF` could be terminated by a literal `EOF` in a
        secret. The delimiter must be randomised per-value."""
        assert re.search(r'EOF_\$\(openssl\s+rand', workflow), (
            "heredoc delimiter must be randomised per call to forclose injection"
        )


class TestSecretsHandling:
    def test_inputs_routed_through_env_block(self, workflow: str) -> None:
        """Workflow_dispatch inputs must not be interpolated directly into
        shell — they go through an `env:` block and are read as $VARS."""
        # The run step that uses inputs.* must reference them via env
        assert "INPUT_INTERVAL: ${{ inputs.heartbeat_interval }}" in workflow
        assert "INPUT_ARMED: ${{ inputs.armed }}" in workflow
        # And the shell uses the env-mapped names, not raw ${{ inputs.* }}
        # inside `run:` blocks (which would be a command-injection vector).
        assert '${{ inputs.armed }}\n          run:' not in workflow

    def test_git_token_routed_through_env_block(self, workflow: str) -> None:
        assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow


class TestConcurrency:
    def test_concurrency_group_is_set(self, workflow: str) -> None:
        """A workflow_dispatch overlapping the daily cron would race on
        warning-commit writes; concurrency:group serialises them."""
        assert re.search(r"concurrency:\s*\n\s*group:\s*\S", workflow)

    def test_cancel_in_progress_false(self, workflow: str) -> None:
        """If a cron run is mid-send, killing it mid-batch is worse than
        letting the manual run queue."""
        assert "cancel-in-progress: false" in workflow


class TestJqNullGuard:
    def test_jq_filter_handles_null(self, workflow: str) -> None:
        """`toJson(vars)` renders as `null` (not `{}`) when zero repo vars
        are defined; without `(. // {})` the brand-new user's first run
        aborts with `null has no keys`."""
        assert "(. // {})" in workflow, (
            "jq filter must coerce null to empty object"
        )


class TestYamlParses:
    def test_workflow_is_valid_yaml(self) -> None:
        """A typo or indentation error would otherwise pass every other
        substring check in this file but fail at workflow-run time in
        production. Parse with yaml.safe_load if available, otherwise
        try `yq`/`python -c "import yaml"`; if neither, skip."""
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            # PyYAML isn't a stdlib module; fall back to a shell `yq` check
            # if present, otherwise skip rather than installing deps.
            yq = shutil.which("yq")
            if not yq:
                pytest.skip("PyYAML and yq both absent")
            result = subprocess.run(
                [yq, "."], stdin=WORKFLOW_PATH.open("rb"),
                capture_output=True,
            )
            assert result.returncode == 0, result.stderr.decode()
            return
        # Parsing as YAML is enough — if the loader returns a dict-shaped
        # workflow, the structure is also basically sane.
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text())
        assert isinstance(parsed, dict)
        assert "jobs" in parsed
        assert "run-dms" in parsed["jobs"]


class TestJobTimeout:
    def test_job_has_timeout(self, workflow: str) -> None:
        """Default GH Actions timeout is 360 minutes. A hung SMTP could
        burn most of that. 15 minutes is plenty for the real workflow."""
        assert re.search(r"timeout-minutes:\s*\d+", workflow), (
            "run-dms job must set timeout-minutes"
        )


class TestSchedule:
    def test_daily_cron(self, workflow: str) -> None:
        """The script's HEARTBEAT_CHECK_HOUR_FREQUENCY=24 assumes a daily
        run; anything less frequent breaks the warning ladder."""
        assert "0 9 * * *" in workflow or re.search(
            r"cron:\s*['\"]?\s*\d+\s+\d+\s+\*\s+\*\s+\*", workflow
        )


class TestLivenessFeatureWorkflowDefaults:
    """v2.1.0 — public-activity liveness feature is opt-in. The
    workflow-level default for CHECK_PUBLIC_ACTIVITY must be the literal
    string "false" so the feature stays off unless the user explicitly
    sets the repo variable."""

    def test_workflow_defaults_check_public_activity_false(
        self, workflow: str
    ) -> None:
        assert re.search(
            r'CHECK_PUBLIC_ACTIVITY:\s*"false"', workflow
        ), "workflow env block must default CHECK_PUBLIC_ACTIVITY to \"false\""

    def test_gh_prefix_not_in_reserved_regex(self, workflow: str) -> None:
        """GH_USERNAME, GH_ACTIVITY_TOKEN, BOT_AUTHOR_PATTERNS, and
        BOT_MESSAGE_PATTERNS all need to pass through `export_one`.
        Catches a future regression where someone adds `GH_*` (or any
        of those names) to the RESERVED allowlist defensively."""
        match = re.search(r"RESERVED='(\^[^']+)'", workflow)
        assert match, "RESERVED regex must be defined in the workflow"
        reserved_re = re.compile(match.group(1))
        for var_name in (
            "GH_USERNAME",
            "GH_ACTIVITY_TOKEN",
            "BOT_AUTHOR_PATTERNS",
            "BOT_MESSAGE_PATTERNS",
        ):
            assert not reserved_re.match(var_name), (
                f"{var_name!r} is matched by the RESERVED regex — it would "
                "be silently refused by export_one."
            )

"""Tests for the State enum."""

from __future__ import annotations

from dead_mans_switch import State


class TestState:
    def test_disarmed_value(self) -> None:
        assert State.DISARMED.value == "disarmed"

    def test_alive_value(self) -> None:
        assert State.ALIVE.value == "alive"

    def test_issue_warning_value(self) -> None:
        assert State.ISSUE_WARNING.value == "warning issued"

    def test_passed_away_value(self) -> None:
        assert State.PASSED_AWAY.value == "passed away"

    def test_already_declared_dead_value(self) -> None:
        assert State.ALREADY_DECLARED_DEAD.value == "already declared dead"

    def test_state_is_str(self) -> None:
        # StrEnum: comparisons with strings should work
        assert State.ALIVE == "alive"

    def test_all_states_listed(self) -> None:
        assert [s.value for s in State] == [
            "disarmed",
            "alive",
            "warning issued",
            "passed away",
            "already declared dead",
        ]

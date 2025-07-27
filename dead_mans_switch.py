"""Dead Man's Switch - Automated Email System for Inactive Git Repositories.

This script implements a dead man's switch that monitors git repository activity
and automatically sends warning and final emails when no commits are detected 
within specified time intervals.

The system works by:
- Monitoring git commits at regular intervals (default: every 24 hours)
- Sending configurable warning emails when no activity is detected
- Sending final emails to designated recipients after all warnings are exhausted
- Using git commits to track its own warning history

Key Features:
- Single script design for ease of use and editing
- Environment variable-based email template substitution
- Support for major email providers (Gmail, iCloud, Outlook, Yahoo, Hotmail)
- Test mode for safe configuration and testing
- Manual dispatch mode for testing email delivery
- Comprehensive error handling and validation

Usage:
    python dead_mans_switch.py <heartbeat_interval_hours> <number_of_warnings> [--armed] [--manual-dispatch]

Examples:
    python dead_mans_switch.py 168 2                     # Test mode: 168 hours, 2 warnings
    python dead_mans_switch.py 72 1 --armed              # Live mode: 72 hours, 1 warning
    python dead_mans_switch.py 48 2 --manual-dispatch    # Manual test dispatch

Environment Variables Required:
    MY_EMAIL: Your email address for authentication
    MY_PASSWORD: Your email password (use App Password for Gmail)

Email Templates:
    Place .txt files in the emails/ directory with format:
    To: recipient@example.com
    Subject: Email Subject
    
    Email body content here...

Testing:
    This module includes comprehensive doctests that can be run with:
    python3.13 -m doctest dead_mans_switch.py -v

Note: This is designed as a single script file for ease of use, 
editing, and deployment. All functionality is contained within this one file 
to simplify setup and maintenance.
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from string import Template
from types import TracebackType
from typing import TypedDict


class DeadMansSwitchException(Exception):
    """Custom exception for Dead Man's Switch related errors.

    This exception is raised when there are configuration issues,
    email sending failures, git operation problems, or other
    system-level errors in the dead man's switch functionality.

    Examples:
        >>> try:
        ...     raise DeadMansSwitchException("Test error")
        ... except DeadMansSwitchException as e:
        ...     str(e)
        'Test error'
    """


class DeadMansSwitch:
    """Core dead man's switch implementation.

    Monitors git commits and sends warning/final emails based on configured
    intervals and warning counts. The switch can be armed or disarmed.

    Attributes:
        HEARTBEAT_CHECK_HOUR_FREQUENCY: Minimum interval for heartbeat checks
        PATH_TO_EMAILS: Directory containing email templates

    Examples:
        >>> # Test basic initialization
        >>> dms = DeadMansSwitch(48, 2, False, False)
        >>> dms._heartbeat_interval_hours
        48
        >>> dms._number_of_warnings
        2
        >>> dms._armed
        False

        >>> # Test validation
        >>> try:
        ...     DeadMansSwitch(12, 2, True, False)  # interval too small
        ... except DeadMansSwitchException as e:
        ...     "must be at least" in str(e)
        True

        >>> try:
        ...     DeadMansSwitch(48, -1, True, False)  # negative warnings
        ... except DeadMansSwitchException as e:
        ...     "greater than or equal to 0" in str(e)
        True
    """

    HEARTBEAT_CHECK_HOUR_FREQUENCY = 24
    PATH_TO_EMAILS = Path(__file__).parent / "emails"

    def __init__(
        self,
        heartbeat_interval_hours: int | float,
        number_of_warnings: int,
        armed: bool,
        manual_dispatch: bool,
    ) -> None:
        """Initialize the dead man's switch.

        Args:
            heartbeat_interval_hours: Hours between required heartbeats (commits)
            number_of_warnings: Number of warning emails to send before final emails
            armed: Whether the switch is active (False = test mode)
            manual_dispatch: When the workflow is triggered manually for testing

        Raises:
            DeadMansSwitchException: If parameters are invalid

        Examples:
            >>> # Valid configuration
            >>> dms = DeadMansSwitch(48.5, 3, True, False)
            >>> dms._heartbeat_interval_hours
            48.5
            >>> dms._number_of_warnings
            3
            >>> dms._armed
            True

            >>> # Test edge cases
            >>> dms = DeadMansSwitch(24, 0, False, False)  # No warnings
            >>> dms._number_of_warnings
            0

            >>> # Invalid configurations
            >>> try:
            ...     DeadMansSwitch(12, 1, True, False)  # Too frequent
            ... except DeadMansSwitchException:
            ...     print("Caught invalid interval")
            Caught invalid interval

            >>> try:
            ...     DeadMansSwitch(48, "not_int", True, False)  # Wrong type
            ... except (DeadMansSwitchException, TypeError):
            ...     print("Caught invalid type")
            Caught invalid type
        """
        # some user input validation first
        if (
            not manual_dispatch
            and heartbeat_interval_hours < self.HEARTBEAT_CHECK_HOUR_FREQUENCY
        ):
            exception_message = f"Heartbeat interval must be at least {self.HEARTBEAT_CHECK_HOUR_FREQUENCY} hours."
            explanation = f"""
            By default, heartbeat checks are done every {self.HEARTBEAT_CHECK_HOUR_FREQUENCY} hours.
            If you want to change the heartbeat interval to be less than this number,
            you should change the cron schedule to be at least as frequent as your desired heartbeat interval.
            """
            raise DeadMansSwitchException(f"{exception_message}{explanation}")
        if number_of_warnings < 0 or not isinstance(number_of_warnings, int):
            exception_message = (
                "Number of warnings must be an integer greater than or equal to 0"
            )
            raise DeadMansSwitchException(exception_message)
        if not isinstance(armed, bool):
            exception_message = "Armed must be a boolean"
            raise DeadMansSwitchException(exception_message)
        if not isinstance(manual_dispatch, bool):
            exception_message = "Manual dispatch must be a boolean"
            raise DeadMansSwitchException(exception_message)

        self._heartbeat_interval_hours = heartbeat_interval_hours
        self._number_of_warnings = number_of_warnings
        self._remaining_warnings = self._get_remaining_warnings(
            self._number_of_warnings
        )
        self._armed = armed
        self._manual_dispatch = manual_dispatch

    @staticmethod
    def _get_remaining_warnings(number_of_warnings: int) -> int:
        """Calculate remaining warnings based on git commit history.

        Analyzes recent commits to determine how many warning emails have
        already been sent by looking for warning commits not made by the
        repo owner.

        Args:
            number_of_warnings: Total number of warnings configured

        Returns:
            int: Number of warnings remaining to send. If > number_of_warnings,
                 indicates already declared dead.

        Examples:
            >>> # This method requires git repo and commits to test properly
            >>> # Basic logic test
            >>> result = DeadMansSwitch._get_remaining_warnings(3)
            >>> isinstance(result, int)
            True
            >>> result >= 0  # Should return non-negative
            True
        """
        remaining = number_of_warnings
        for i in range(number_of_warnings):
            try:
                commit = Commit.from_last_commit(i)
            except Exception:
                # end of the list of commits
                break
            if not commit.is_by_repo_owner():
                if commit.message.startswith(State.ISSUE_WARNING):
                    remaining -= 1
                elif commit.message.startswith(State.PASSED_AWAY):
                    remaining = number_of_warnings + 1
                    break
                else:
                    raise DeadMansSwitchException(
                        "Only the repo owner should commit to the repo. "
                        f"{commit} was not by the repo owner."
                    )
            else:
                # this means that we had a heartbeat from the repo owner
                # any warnings after this should not be counted
                # but any warnings until this point should be counted
                # so we simply break
                break
        return remaining

    def run(self) -> None:
        """Execute the dead man's switch logic based on current state.

        This is the main entry point that determines the current state and takes
        appropriate action:

        - DISARMED: Send test emails to the owner
        - ALIVE: Do nothing (user is active)
        - ISSUE_WARNING: Create warning commit
        - PASSED_AWAY: Send final emails and create passed away commit
        - ALREADY_DECLARED_DEAD: Do nothing (already processed)

        The state is determined by:
        1. Whether the switch is armed
        2. Time since last commit
        3. Number of remaining warnings
        4. Previous warning/passed away commits

        Examples:
            >>> # Test with mocked states - actual testing requires git setup
            >>> dms = DeadMansSwitch(48, 2, False, False)
            >>> # In disarmed mode, would send test emails (requires email setup)

            >>> # Armed switch logic depends on git state
            >>> dms_armed = DeadMansSwitch(48, 2, True, False)
            >>> # Would check git commits and act accordingly
        """
        match self._get_state():
            case State.DISARMED:
                # send emails to the repo owner
                emails = self._gather_emails()
                if not emails:
                    print("No .txt files found in the emails directory")
                    return
                with EmailServer() as email_server:
                    for email in emails:
                        email.to = email_server.email
                        email.subject = "Dead Man's Switch Test Email: " + email.subject
                        body_prepend = (
                            "Dear DMS User,\n\n"
                            "This is a test email to check if the dead man's switch is working.\n"
                            "Once you arm the switch, you will not receive these scheduled test emails.\n\n"
                        )
                        email.body = body_prepend + email.body
                    email_server.send_all(emails)
            case State.ISSUE_WARNING:
                warning_commit = Commit(
                    message=State.ISSUE_WARNING,
                    user="dms_bot",
                    timestamp=datetime.now(timezone.utc),
                )
                warning_commit.write_to_repo()
                return
            case State.ALIVE:
                print("No action needed")
                return
            case State.PASSED_AWAY:
                emails = self._gather_emails()
                with EmailServer() as email_server:
                    if self._manual_dispatch:
                        for email in emails:
                            email.to = email_server.email
                            email.subject = (
                                "Dead Man's Switch Manually Triggered: " + email.subject
                            )
                            body_prepend = (
                                "Dear DMS User,\n\n"
                                "Dead Man's Switch was armed and manually triggered.\n"
                                "Don't worry, we only sent the emails to you.\n\n"
                            )
                            email.body = body_prepend + email.body
                    email_server.send_all(emails)
                passed_away_commit = Commit(
                    message=State.PASSED_AWAY,
                    user="dms_bot",
                    timestamp=datetime.now(timezone.utc),
                )
                passed_away_commit.write_to_repo()
                return
            case State.ALREADY_DECLARED_DEAD:
                print("Repo owner is already declared dead")
                return

    def _get_state(self) -> State:
        """Determine the current state of the dead man's switch.

        This is the core logic that determines what action should be taken.
        The state depends on multiple factors working together:

        1. Armed status: If False, always returns DISARMED
        2. Remaining warnings: If > number_of_warnings, returns ALREADY_DECLARED_DEAD
        3. Time since last commit: If > heartbeat_interval_hours:
           - If no warnings left: PASSED_AWAY
           - If warnings remain: ISSUE_WARNING
        4. Otherwise: ALIVE

        Returns:
            State: Current state enum value

        Examples:
            >>> # Test disarmed state
            >>> dms = DeadMansSwitch(48, 2, False, False)
            >>> state = dms._get_state()
            >>> state == State.DISARMED
            True

            >>> # Test armed switch - behavior depends on git state
            >>> dms_armed = DeadMansSwitch(48, 2, True, False)
            >>> # Would check actual git commits and timing
            >>> # state = dms_armed._get_state() would require git setup
            >>> # isinstance(state, State) would be True

            >>> # Test state transitions logic
            >>> # If remaining_warnings > number_of_warnings, should be declared dead
            >>> dms_dead = DeadMansSwitch(48, 2, True, False)
            >>> dms_dead._remaining_warnings = 5  # Simulate already dead
            >>> # dms_dead._get_state() == State.ALREADY_DECLARED_DEAD would require git setup
        """
        if not self._armed:
            return State.DISARMED
        if self._remaining_warnings == self._number_of_warnings + 1:
            return State.ALREADY_DECLARED_DEAD
        last_commit = Commit.from_last_commit()
        interval_passed = last_commit.hours_since > self._heartbeat_interval_hours
        print(f"Last commit: {last_commit}")
        print(f"Hours since last commit: {last_commit.hours_since}")
        if interval_passed:
            if self._remaining_warnings <= 0:
                return State.PASSED_AWAY
            return State.ISSUE_WARNING
        return State.ALIVE

    def _gather_emails(self) -> list[Email]:
        """Collect all email templates from the emails directory.

        Returns:
            list[Email]: List of parsed email objects from .txt files

        Examples:
            >>> dms = DeadMansSwitch(48, 2, False, False)
            >>> emails = dms._gather_emails()
            >>> isinstance(emails, list)
            True
            >>> all(isinstance(email, Email) for email in emails)
            True
        """
        return [Email.from_txt(path) for path in self.PATH_TO_EMAILS.glob("*.txt")]

    def _trigger(self, emails: list[Email]) -> None:
        """Send emails using EmailServer (legacy method).

        Args:
            emails: List of emails to send

        Note:
            This method is kept for compatibility but run() now uses
            EmailServer as a context manager for better resource management.

        Examples:
            >>> emails = [Email("test@example.com", "Test", "Body")]
            >>> dms = DeadMansSwitch(48, 2, False, False)
            >>> # dms._trigger(emails) would require email server setup
        """
        email_server = EmailServer()
        with email_server:
            email_server.send_all(emails)


class State(StrEnum):
    """Enumeration of possible dead man's switch states.

    The state determines what action the dead man's switch should take
    when run() is called.

    Examples:
        >>> State.DISARMED.value
        'disarmed'
        >>> State.ALIVE.value
        'alive'
        >>> State.ISSUE_WARNING.value
        'warning issued'
        >>> State.PASSED_AWAY.value
        'passed away'
        >>> State.ALREADY_DECLARED_DEAD.value
        'already declared dead'

        >>> # Test enum behavior
        >>> state = State.ALIVE
        >>> state == "alive"
        True
        >>> [s.value for s in State]
        ['disarmed', 'alive', 'warning issued', 'passed away', 'already declared dead']
    """

    DISARMED = "disarmed"
    ALIVE = "alive"
    ISSUE_WARNING = "warning issued"
    PASSED_AWAY = "passed away"
    ALREADY_DECLARED_DEAD = "already declared dead"


@dataclass
class Commit:
    """Represents a git commit with metadata.

    Attributes:
        message: Commit message
        user: Author name
        timestamp: Commit timestamp (naive datetime in local time)

    Examples:
        >>> from datetime import datetime, timezone
        >>> commit = Commit("Initial commit", "user", datetime.now(timezone.utc))
        >>> commit.message
        'Initial commit'
        >>> commit.user
        'user'
        >>> isinstance(commit.timestamp, datetime)
        True
    """

    message: str
    user: str
    timestamp: datetime  # timezone-aware datetime, in UTC

    @classmethod
    def from_last_commit(cls, i: int = 0) -> Commit:
        """Create Commit object from git log.

        Args:
            i: Skip count (0 = latest commit, 1 = previous, etc.)

        Returns:
            Commit: Commit object with parsed metadata

        Raises:
            DeadMansSwitchException: If git command fails

        Examples:
            >>> # Requires git repository
            >>> try:
            ...     commit = Commit.from_last_commit()
            ...     isinstance(commit, Commit)
            ... except DeadMansSwitchException:
            ...     print("No git repo or commits")
            ...     True
            True

            >>> # Test with skip parameter
            >>> try:
            ...     commit = Commit.from_last_commit(1)  # Previous commit
            ...     isinstance(commit, Commit)
            ... except DeadMansSwitchException:
            ...     print("No second commit")
            ...     True
            True
        """
        try:
            output = subprocess.check_output(
                ["git", "log", "-1", f"--skip={i}", "--pretty=format:%s|%an|%cI"],
                text=True,
            )
            message, user, timestamp_str = output.split("|", 2)
            timestamp = datetime.fromisoformat(timestamp_str.strip())
            # Convert to UTC timezone-aware datetime for consistent comparison
            timestamp = timestamp.astimezone(timezone.utc)
            return Commit(
                timestamp=timestamp,
                message=message,
                user=user,
            )
        except Exception as e:
            raise DeadMansSwitchException("Failed to get last git commit") from e

    @cached_property
    def _repo_owner(self) -> str:
        """Get the repository owner from git remote URL.

        Returns:
            str: Repository owner name

        Raises:
            DeadMansSwitchException: If git command fails

        Examples:
            >>> # Requires git repo with remote
            >>> commit = Commit("test", "user", datetime.now(timezone.utc))
            >>> try:
            ...     owner = commit._repo_owner
            ...     isinstance(owner, str)
            ... except DeadMansSwitchException:
            ...     print("No git remote")
            ...     True
            True
        """
        try:
            output = subprocess.check_output(
                ["git", "config", "remote.origin.url"], text=True
            )
            url = output.strip()
            # get the owner from the url
            owner = url.split("/")[-2]
            return owner
        except Exception as e:
            raise DeadMansSwitchException("Failed to get repo owner") from e

    @property
    def hours_since(self) -> float:
        """Calculate hours since this commit.

        Returns:
            float: Hours elapsed since commit timestamp

        Examples:
            >>> from datetime import datetime, timedelta, timezone
            >>> past_time = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
            >>> commit = Commit("test", "user", past_time)
            >>> hours = commit.hours_since
            >>> 2.4 < hours < 2.6  # Should be around 2.5 hours
            True

            >>> # Recent commit
            >>> recent_commit = Commit("test", "user", datetime.now(timezone.utc))
            >>> recent_commit.hours_since < 0.1  # Should be very recent
            True
        """
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds() / 3600

    def is_by_repo_owner(self) -> bool:
        """Check if this commit was made by the repository owner.

        Returns:
            bool: True if commit author matches repo owner

        Examples:
            >>> # Requires git repo setup to test properly
            >>> commit = Commit("test", "owner", datetime.now(timezone.utc))
            >>> # Result depends on actual git configuration
            >>> isinstance(commit.is_by_repo_owner(), bool)
            True
        """
        return self.user == self._repo_owner

    def write_to_repo(self) -> None:
        """Write this commit to the git repository.

        Creates a new empty commit with this commit's message.
        Pushes the commit to the remote repository.

        Examples:
            >>> # Requires git repo setup
            >>> commit = Commit("Test commit", "DMS", datetime.now(timezone.utc))
            >>> # commit.write_to_repo() would require git repository setup
        """
        subprocess.run(
            ["git", "commit", "--allow-empty", "--message", self.message],
            check=True,
        )
        subprocess.run(["git", "push"], check=True)


@dataclass
class Email:
    """Represents an email message with validation.

    Attributes:
        to: Recipient email address (must be valid format)
        subject: Email subject line
        body: Email body content

    Examples:
        >>> email = Email("test@example.com", "Hello", "Test message")
        >>> email.to
        'test@example.com'
        >>> email.subject
        'Hello'
        >>> email.body
        'Test message'

        >>> # Invalid email should raise exception
        >>> try:
        ...     Email("invalid-email", "Subject", "Body")
        ... except DeadMansSwitchException:
        ...     print("Invalid email caught")
        Invalid email caught
    """

    to: str
    subject: str
    body: str

    def __post_init__(self) -> None:
        """Validate email after initialization.

        Called automatically after dataclass initialization to ensure
        the email address is properly formatted.

        Raises:
            DeadMansSwitchException: If email address is invalid
        """
        self._validate_email()

    def _validate_email(self) -> None:
        """Validate email address format using regex.

        Checks if the email address follows basic email format pattern.

        Raises:
            DeadMansSwitchException: If email address is invalid

        Examples:
            >>> email = Email.__new__(Email)
            >>> email.to = "valid@example.com"
            >>> email._validate_email()  # Should not raise

            >>> email.to = "invalid-email"
            >>> try:
            ...     email._validate_email()
            ... except DeadMansSwitchException:
            ...     print("Validation failed as expected")
            Validation failed as expected
        """
        # check if the email address is valid
        if not re.match(r"[^@]+@[^@]+\.[^@]+", self.to):
            raise DeadMansSwitchException(f"Invalid email address: {self.to}")

    @classmethod
    def from_txt(cls, path_to_txt: Path) -> Email:
        """Parse an email from a text file and substitute environment variables.

        Expected format:
        To: Recipient email
        Subject: Subject line

        Body content starts here...

        All placeholders in ${} must have corresponding environment variables set.

        Args:
            path_to_txt: Path to the text file containing email template

        Returns:
            Email: Parsed and environment-substituted email object

        Raises:
            DeadMansSwitchException: If file not found, missing fields, or env vars missing
            KeyError: If any environment variable placeholder is missing

        Examples:
            >>> import tempfile
            >>> import os
            >>>
            >>> # Create a test email file
            >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            ...     _ = f.write("To: test@example.com\\nSubject: Test\\n\\nHello World")
            ...     temp_path = Path(f.name)
            >>>
            >>> email = Email.from_txt(temp_path)
            >>> email.to
            'test@example.com'
            >>> email.subject
            'Test'
            >>> email.body
            'Hello World'
            >>>
            >>> # Clean up
            >>> temp_path.unlink()

            >>> # Test with environment variable substitution
            >>> os.environ['TEST_EMAIL'] = 'env@example.com'
            >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            ...     _ = f.write("To: ${TEST_EMAIL}\\nSubject: Test\\n\\nHello")
            ...     temp_path = Path(f.name)
            >>>
            >>> email = Email.from_txt(temp_path)
            >>> email.to
            'env@example.com'
            >>> temp_path.unlink()
            >>> del os.environ['TEST_EMAIL']
        """
        try:
            content = path_to_txt.read_text(encoding="utf-8").strip()
            lines = content.split("\n")

            to_email = ""
            subject = ""
            body_lines = []

            i = 0
            # Parse To: line
            while i < len(lines):
                line = lines[i].strip()
                if line.lower().startswith("to:"):
                    to_email = line[3:].strip()
                    i += 1
                    break
                i += 1

            # Parse Subject: line
            while i < len(lines):
                line = lines[i].strip()
                if line.lower().startswith("subject:"):
                    subject = line[8:].strip()
                    i += 1
                    break
                i += 1

            # Skip empty lines after subject
            while i < len(lines) and not lines[i].strip():
                i += 1

            # Rest is body
            body_lines = lines[i:]
            body = "\n".join(body_lines).strip()

            if not to_email:
                raise DeadMansSwitchException(f"No 'To:' field found in {path_to_txt}")
            if not subject:
                raise DeadMansSwitchException(
                    f"No 'Subject:' field found in {path_to_txt}"
                )

            # Template substitute with environment variables
            # safe substitute does not raise KeyError if missing
            to_substituted = Template(to_email).safe_substitute(os.environ)
            subject_substituted = Template(subject).safe_substitute(os.environ)
            body_substituted = Template(body).safe_substitute(os.environ)

            return cls(
                to=to_substituted, subject=subject_substituted, body=body_substituted
            )

        except FileNotFoundError:
            raise DeadMansSwitchException(f"Email file not found: {path_to_txt}")
        except Exception as e:
            raise DeadMansSwitchException(
                f"Failed to parse email {path_to_txt}: {e}"
            ) from e


class SMTPConfig(TypedDict):
    """Type definition for SMTP server configuration.

    Attributes:
        server: SMTP server hostname
        port: SMTP server port number

    Examples:
        >>> config: SMTPConfig = {"server": "smtp.gmail.com", "port": 587}
        >>> config["server"]
        'smtp.gmail.com'
        >>> config["port"]
        587
    """

    server: str
    port: int


class EmailServer:
    """Handles SMTP email sending operations.

    Supports major email providers with automatic SMTP configuration.
    Can be used as a context manager for connection management.

    Attributes:
        SECONDS_BETWEEN_EMAILS: Delay between sending multiple emails
        SMTP_CONFIGS: Mapping of email domains to SMTP configurations

    Examples:
        >>> # Note: These examples require valid MY_EMAIL and MY_PASSWORD env vars
        >>> import os
        >>> os.environ['MY_EMAIL'] = 'test@gmail.com'  # doctest: +SKIP
        >>> os.environ['MY_PASSWORD'] = 'app_password'  # doctest: +SKIP
        >>> server = EmailServer()  # doctest: +SKIP
        >>> server.email  # doctest: +SKIP
        'test@gmail.com'
    """

    SECONDS_BETWEEN_EMAILS = 5
    SMTP_CONFIGS: dict[str, SMTPConfig] = {
        "gmail.com": {"server": "smtp.gmail.com", "port": 587},
        "icloud.com": {"server": "smtp.mail.me.com", "port": 587},
        "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587},
        "yahoo.com": {"server": "smtp.mail.yahoo.com", "port": 587},
        "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587},
    }

    def __init__(self) -> None:
        """Initialize email server with credentials from environment variables.

        Reads MY_EMAIL and MY_PASSWORD from environment variables and configures
        SMTP settings based on the email domain.

        Raises:
            DeadMansSwitchException: If environment variables are missing or
                                   email provider is unsupported

        Examples:
            >>> # Test missing environment variables
            >>> old_email = os.environ.get('MY_EMAIL')
            >>> old_password = os.environ.get('MY_PASSWORD')
            >>> if 'MY_EMAIL' in os.environ: del os.environ['MY_EMAIL']
            >>> if 'MY_PASSWORD' in os.environ: del os.environ['MY_PASSWORD']
            >>>
            >>> try:
            ...     EmailServer()
            ... except DeadMansSwitchException as e:
            ...     "MY_EMAIL environment variable is not set" in str(e)
            True
            >>>
            >>> # Test unsupported provider
            >>> os.environ['MY_EMAIL'] = 'test@unsupported.com'
            >>> os.environ['MY_PASSWORD'] = 'password'
            >>> try:
            ...     EmailServer()
            ... except DeadMansSwitchException as e:
            ...     "Unsupported email provider" in str(e)
            True
            >>>
            >>> # Restore environment
            >>> if old_email: os.environ['MY_EMAIL'] = old_email
            >>> if old_password: os.environ['MY_PASSWORD'] = old_password
        """
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
        self._email: str = email
        self._password: str = password

        domain = self._email.split("@")[-1].lower()
        smtp_config = self.SMTP_CONFIGS.get(domain)
        if not smtp_config:
            raise DeadMansSwitchException(
                f"Unsupported email provider: {domain}. "
                f"Supported providers: {', '.join(self.SMTP_CONFIGS.keys())}"
            )
        self._smtp_config: SMTPConfig = smtp_config
        self._smtp_server: smtplib.SMTP | None = None

    @property
    def email(self) -> str:
        """Get the configured email address.

        Returns:
            str: The email address used for sending

        Examples:
            >>> # Requires valid environment setup
            >>> import os
            >>> old_email = os.environ.get('MY_EMAIL')
            >>> os.environ['MY_EMAIL'] = 'test@gmail.com'
            >>> os.environ['MY_PASSWORD'] = 'password'
            >>> server = EmailServer()
            >>> server.email
            'test@gmail.com'
            >>> if old_email: os.environ['MY_EMAIL'] = old_email
        """
        return self._email

    def __enter__(self) -> EmailServer:
        """Enter context manager, establishing SMTP connection.

        Returns:
            EmailServer: Self for use in with statement

        Raises:
            DeadMansSwitchException: If SMTP connection fails
        """
        self._smtp_server = self._get_smtp_server()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback_obj: TracebackType | None,
    ) -> bool:
        """Exit context manager, closing SMTP connection.

        Args:
            exc_type: Exception type if an exception occurred
            exc_value: Exception value if an exception occurred
            traceback_obj: Traceback object if an exception occurred

        Returns:
            bool: False to re-raise any exception that occurred
        """
        if self._smtp_server:
            try:
                self._smtp_server.quit()
            except Exception:
                pass  # Ignore errors when closing
            finally:
                self._smtp_server = None

        if exc_type is not None:
            traceback.print_exc()
            return False  # Re-raise the exception
        return True

    def _get_smtp_server(self) -> smtplib.SMTP:
        """Create and configure SMTP server connection.

        Returns:
            smtplib.SMTP: Authenticated SMTP server instance

        Raises:
            DeadMansSwitchException: If connection or authentication fails
        """
        try:
            server = smtplib.SMTP(
                self._smtp_config["server"], self._smtp_config["port"]
            )
            server.starttls()  # Enable TLS encryption
            server.login(self._email, self._password)
            return server
        except smtplib.SMTPAuthenticationError as e:
            raise DeadMansSwitchException(
                f"Failed to authenticate with email server. "
                f"Check your email and password/app password. Error: {e}"
            ) from e
        except smtplib.SMTPException as e:
            raise DeadMansSwitchException(f"SMTP error occurred: {e}") from e
        except Exception as e:
            raise DeadMansSwitchException(
                f"Failed to connect to email server: {e}"
            ) from e

    def _send(self, email: Email) -> None:
        """Send an email to the specified recipient.

        Args:
            email: Email object containing recipient, subject, and body

        Raises:
            DeadMansSwitchException: If email sending fails

        Examples:
            >>> # This would require actual SMTP setup
            >>> email = Email("test@example.com", "Test", "Body")  # doctest: +SKIP
            >>> server = EmailServer()  # doctest: +SKIP
            >>> server._send(email)  # doctest: +SKIP
        """
        try:
            # Create message
            msg = MIMEMultipart()
            msg["From"] = self._email
            msg["To"] = email.to
            msg["Subject"] = email.subject
            # Attach body as plain text
            msg.attach(MIMEText(email.body, "plain"))
            # Send email using stored connection or create new one
            if self._smtp_server:
                # Using context manager - use stored connection
                self._smtp_server.send_message(msg)
            else:
                # Not using context manager - create temporary connection
                with self._get_smtp_server() as server:
                    server.send_message(msg)
        except Exception as e:
            raise DeadMansSwitchException(
                f"Failed to send email to {email.to}: {e}"
            ) from e

    def send_all(self, emails: list[Email]) -> None:
        """Send multiple emails with delays between them.

        Args:
            emails: List of Email objects to send

        Examples:
            >>> emails = [
            ...     Email("test1@example.com", "Test 1", "Body 1"),
            ...     Email("test2@example.com", "Test 2", "Body 2")
            ... ]  # doctest: +SKIP
            >>> server = EmailServer()  # doctest: +SKIP
            >>> server.send_all(emails)  # doctest: +SKIP
        """
        for email in emails:
            self._send(email)
            time.sleep(self.SECONDS_BETWEEN_EMAILS)


def main() -> None:
    """Main entry point for the dead man's switch CLI.

    Parses command line arguments and runs the dead man's switch with the
    provided configuration.

    Args parsed from command line:
        heartbeat_interval_hours: Hours between required heartbeats (commits)
        number_of_warnings: Number of warning emails to send before final emails
        --armed: Flag to arm the switch (default: False for test mode)
        --manual-dispatch: Flag to manually trigger the switch

    Examples:
        >>> import sys
        >>> import argparse
        >>> # Test argument parsing by creating parser directly
        >>> parser = argparse.ArgumentParser()
        >>> _ = parser.add_argument('heartbeat_interval_hours', type=float)
        >>> _ = parser.add_argument('number_of_warnings', type=int)
        >>> _ = parser.add_argument('--armed', action='store_true')
        >>> _ = parser.add_argument('--manual-dispatch', action='store_true')

        >>> # Test valid arguments
        >>> args = parser.parse_args(['48', '2'])
        >>> args.heartbeat_interval_hours
        48.0
        >>> args.number_of_warnings
        2
        >>> args.armed
        False

        >>> # Test with armed flag
        >>> args = parser.parse_args(['72.5', '3', '--armed'])
        >>> args.heartbeat_interval_hours
        72.5
        >>> args.number_of_warnings
        3
        >>> args.armed
        True

        >>> # Test invalid arguments
        >>> try:
        ...     parser.parse_args(['invalid', '2'])
        ... except SystemExit:
        ...     print("Invalid argument handled correctly")
        Invalid argument handled correctly
    """
    parser = argparse.ArgumentParser(
        description="Dead Man's Switch - Monitor git commits and send emails when inactive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 168 2                     # Check every 168 hours, send 2 warnings, test mode (default)
  %(prog)s 72 1 --armed              # Armed mode: 72 hour interval, 1 warning
  %(prog)s 24.5 0 --armed            # Armed mode: 24.5 hour interval, no warnings
  %(prog)s 48 2 --manual-dispatch    # Manual dispatch mode
        """,
    )

    parser.add_argument(
        "heartbeat_interval_hours",
        type=float,
        help=f"Hours between required heartbeats (commits). Must be >= {DeadMansSwitch.HEARTBEAT_CHECK_HOUR_FREQUENCY}.",
    )

    parser.add_argument(
        "number_of_warnings",
        type=int,
        help="Number of warning emails to send before final emails. Must be >= 0.",
    )

    parser.add_argument(
        "--armed",
        action="store_true",
        help="Arm the switch for live operation. Without this flag, runs in test mode.",
    )

    parser.add_argument(
        "--manual-dispatch",
        action="store_true",
        help="Manually trigger the dead man's switch.",
    )

    args = parser.parse_args()

    # Create and run the dead man's switch
    dms = DeadMansSwitch(
        heartbeat_interval_hours=args.heartbeat_interval_hours,
        number_of_warnings=args.number_of_warnings,
        armed=args.armed,
        manual_dispatch=args.manual_dispatch,
    )
    dms.run()


if __name__ == "__main__":
    main()

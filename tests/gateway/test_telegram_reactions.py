"""Tests for Telegram message reactions tied to processing lifecycle hooks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


def _make_adapter(**extra_env):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token")
    adapter._bot = AsyncMock()
    adapter._bot.set_message_reaction = AsyncMock()
    return adapter


def _make_event(chat_id: str = "123", message_id: str = "456") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="private",
            user_id="42",
            user_name="TestUser",
        ),
        message_id=message_id,
    )


# ── _reactions_enabled ───────────────────────────────────────────────


def test_reaction_level_defaults_to_off(monkeypatch):
    """Telegram reactions should be disabled by default."""
    monkeypatch.delenv("TELEGRAM_REACTION_LEVEL", raising=False)
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    assert adapter._reaction_level() == "off"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", "off"),
        ("ack", "ack"),
        ("minimal", "minimal"),
        ("extensive", "extensive"),
        ("true", "extensive"),
        ("1", "extensive"),
        ("false", "off"),
        ("0", "off"),
        ("no", "off"),
    ],
)
def test_reaction_level_reads_new_env_var(monkeypatch, value, expected):
    """TELEGRAM_REACTION_LEVEL should accept the new level names and legacy booleans."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", value)
    adapter = _make_adapter()
    assert adapter._reaction_level() == expected


def test_reaction_level_legacy_env_alias(monkeypatch):
    """TELEGRAM_REACTIONS remains a compatibility alias."""
    monkeypatch.delenv("TELEGRAM_REACTION_LEVEL", raising=False)
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    assert adapter._reaction_level() == "extensive"


def test_reaction_level_legacy_env_alias_preserves_truthy_strings(monkeypatch):
    """Legacy TELEGRAM_REACTIONS should stay permissive for non-false strings."""
    monkeypatch.delenv("TELEGRAM_REACTION_LEVEL", raising=False)
    monkeypatch.setenv("TELEGRAM_REACTIONS", "enabled")
    adapter = _make_adapter()
    assert adapter._reaction_level() == "extensive"


def test_reaction_level_invalid_new_env_var_stays_off(monkeypatch):
    """The new TELEGRAM_REACTION_LEVEL should remain strict for invalid strings."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "enabled")
    adapter = _make_adapter()
    assert adapter._reaction_level() == "off"


def test_reaction_level_env_takes_precedence(monkeypatch):
    """TELEGRAM_REACTION_LEVEL should win over the legacy alias."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "ack")
    monkeypatch.setenv("TELEGRAM_REACTIONS", "extensive")
    adapter = _make_adapter()
    assert adapter._reaction_level() == "ack"


# ── _set_reaction ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_reaction_calls_bot_api(monkeypatch):
    """_set_reaction should call bot.set_message_reaction with correct args."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "extensive")
    adapter = _make_adapter()

    result = await adapter._set_reaction("123", "456", "\U0001f440")

    assert result is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )


@pytest.mark.asyncio
async def test_set_reaction_returns_false_without_bot(monkeypatch):
    """_set_reaction should return False when bot is not available."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "extensive")
    adapter = _make_adapter()
    adapter._bot = None

    result = await adapter._set_reaction("123", "456", "\U0001f440")
    assert result is False


@pytest.mark.asyncio
async def test_set_reaction_handles_api_error_gracefully(monkeypatch):
    """API errors during reaction should not propagate."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "extensive")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no perms"))

    result = await adapter._set_reaction("123", "456", "\U0001f440")
    assert result is False


# ── on_processing_start ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "should_react"),
    [
        ("off", False),
        ("ack", True),
        ("minimal", False),
        ("extensive", False),
    ],
)
async def test_on_processing_start_respects_reaction_level(monkeypatch, level, should_react):
    """Processing start should only add eyes for ACK mode."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", level)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_start(event)

    if should_react:
        adapter._bot.set_message_reaction.assert_awaited_once_with(
            chat_id=123,
            message_id=456,
            reaction="\U0001f440",
        )
    else:
        adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_start_handles_missing_ids(monkeypatch):
    """Should handle events without chat_id or message_id gracefully."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "ack")
    adapter = _make_adapter()
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(chat_id=None),
        message_id=None,
    )

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()


# ── on_processing_complete ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "outcome", "expected_reaction"),
    [
        ("off", ProcessingOutcome.SUCCESS, None),
        ("ack", ProcessingOutcome.SUCCESS, []),
        ("ack", ProcessingOutcome.FAILURE, []),
        ("ack", ProcessingOutcome.CANCELLED, None),
        ("minimal", ProcessingOutcome.SUCCESS, "\U0001f44d"),
        ("extensive", ProcessingOutcome.SUCCESS, "\U0001f44d"),
        ("minimal", ProcessingOutcome.FAILURE, "\U0001f44e"),
        ("extensive", ProcessingOutcome.FAILURE, "\U0001f44e"),
    ],
)
async def test_on_processing_complete_respects_reaction_level(
    monkeypatch,
    level,
    outcome,
    expected_reaction,
):
    """Processing complete should swap or clear reactions according to the level."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", level)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, outcome)

    if expected_reaction is None:
        adapter._bot.set_message_reaction.assert_not_awaited()
    else:
        adapter._bot.set_message_reaction.assert_awaited_once_with(
            chat_id=123,
            message_id=456,
            reaction=expected_reaction,
        )


@pytest.mark.asyncio
async def test_on_processing_complete_skipped_when_disabled(monkeypatch):
    """Processing complete should not react when reactions are disabled."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "off")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_keeps_existing_reaction(monkeypatch):
    """Expected cancellation should not replace the in-progress reaction."""
    monkeypatch.setenv("TELEGRAM_REACTION_LEVEL", "extensive")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    adapter._bot.set_message_reaction.assert_not_awaited()


def test_config_reactions_env_takes_precedence(monkeypatch, tmp_path):
    """Env var should take precedence over config.yaml for reactions."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reactions": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTIONS") == "false"

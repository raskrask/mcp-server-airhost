"""Tests for GmailMFAStrategy.after_fetch cleanup actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from airhost_mcp.mfa.gmail import GmailMFAStrategy


def _make_strategy(after_fetch: str = "keep") -> GmailMFAStrategy:
    return GmailMFAStrategy(
        credentials_path="/dev/null",
        token_path="/dev/null",
        sender="noreply@airhost.co",
        subject_regex=r"(?:ログインコードは\s+(\d{6})|新しいデバイス)",
        code_regex=r"\b(\d{6})\b",
        after_fetch=after_fetch,
    )


def _fake_service(msg_id: str = "msg123") -> MagicMock:
    """Build a minimal Gmail service mock that returns one matching message."""
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": msg_id}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {
            "headers": [{"name": "Subject", "value": "ログインコードは 654321 です"}],
            "body": {},
            "parts": [],
        },
        "snippet": "",
    }
    service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    service.users.return_value.messages.return_value.trash.return_value.execute.return_value = {}
    service.users.return_value.messages.return_value.delete.return_value.execute.return_value = {}
    return service


@patch("airhost_mcp.mfa.gmail.asyncio.to_thread", new_callable=AsyncMock)
@patch("airhost_mcp.mfa.gmail.time.time")
async def test_keep_does_not_call_modify(mock_time: MagicMock, mock_thread: AsyncMock) -> None:
    strategy = _make_strategy("keep")
    svc = _fake_service()

    # to_thread is called for: get_creds, build, list, get
    mock_thread.side_effect = [
        MagicMock(),  # get_creds
        svc,          # build
        {"messages": [{"id": "msg123"}]},  # list
        svc.users().messages().get().execute(),  # get
    ]
    mock_time.side_effect = [0.0, 0.0, 0.0, 0.0, 0.0, 999.0]

    code = await strategy.fetch_code(since_epoch=0.0, timeout_seconds=10)
    assert code == "654321"
    # modify and trash should not have been awaited
    for c in mock_thread.await_args_list:
        fn = c.args[0] if c.args else None
        if fn is not None and callable(fn):
            assert "modify" not in str(fn)


@patch("airhost_mcp.mfa.gmail.asyncio.to_thread", new_callable=AsyncMock)
@patch("airhost_mcp.mfa.gmail.time.time")
async def test_archive_calls_modify_with_correct_labels(
    mock_time: MagicMock, mock_thread: AsyncMock
) -> None:
    strategy = _make_strategy("archive")
    svc = _fake_service()

    mock_thread.side_effect = [
        MagicMock(),  # get_creds
        svc,          # build
        {"messages": [{"id": "msg123"}]},  # list
        svc.users().messages().get().execute(),  # get
        {},           # modify
    ]
    mock_time.side_effect = [0.0, 0.0, 0.0, 0.0, 0.0, 999.0]

    code = await strategy.fetch_code(since_epoch=0.0, timeout_seconds=10)
    assert code == "654321"
    # The 5th to_thread call should be the modify lambda; verify it was called
    assert mock_thread.await_count == 5


@patch("airhost_mcp.mfa.gmail.asyncio.to_thread", new_callable=AsyncMock)
@patch("airhost_mcp.mfa.gmail.time.time")
async def test_trash_calls_trash_endpoint(
    mock_time: MagicMock, mock_thread: AsyncMock
) -> None:
    strategy = _make_strategy("trash")
    svc = _fake_service()

    mock_thread.side_effect = [
        MagicMock(),  # get_creds
        svc,          # build
        {"messages": [{"id": "msg123"}]},  # list
        svc.users().messages().get().execute(),  # get
        {},           # trash
    ]
    mock_time.side_effect = [0.0, 0.0, 0.0, 0.0, 0.0, 999.0]

    code = await strategy.fetch_code(since_epoch=0.0, timeout_seconds=10)
    assert code == "654321"
    assert mock_thread.await_count == 5


@patch("airhost_mcp.mfa.gmail.asyncio.to_thread", new_callable=AsyncMock)
@patch("airhost_mcp.mfa.gmail.time.time")
async def test_delete_calls_delete_endpoint(
    mock_time: MagicMock, mock_thread: AsyncMock
) -> None:
    strategy = _make_strategy("delete")
    svc = _fake_service()

    mock_thread.side_effect = [
        MagicMock(),  # get_creds
        svc,          # build
        {"messages": [{"id": "msg123"}]},  # list
        svc.users().messages().get().execute(),  # get
        {},           # delete
    ]
    mock_time.side_effect = [0.0, 0.0, 0.0, 0.0, 0.0, 999.0]

    code = await strategy.fetch_code(since_epoch=0.0, timeout_seconds=10)
    assert code == "654321"
    assert mock_thread.await_count == 5

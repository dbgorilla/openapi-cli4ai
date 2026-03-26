"""Tests for SSE streaming parser."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_mock_response(lines: list[str]):
    """Create a mock httpx.Response that yields lines."""
    mock = MagicMock()
    mock.iter_lines.return_value = iter(lines)
    return mock


def test_stream_sse_delta(cli_module, capsys):
    """Should accumulate and print delta content."""
    response = _make_mock_response(
        [
            'data: {"delta": "Hello"}',
            'data: {"delta": " world"}',
            'data: {"done": true}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "Hello world"
    captured = capsys.readouterr()
    assert "Hello" in captured.out
    assert "world" in captured.out


def test_stream_sse_done_signal(cli_module):
    """Should stop on done signal."""
    response = _make_mock_response(
        [
            'data: {"delta": "content"}',
            'data: {"done": true}',
            'data: {"delta": "should not appear"}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "content"


def test_stream_sse_openai_done(cli_module):
    """Should handle OpenAI-style [DONE] signal."""
    response = _make_mock_response(
        [
            'data: {"delta": "hello"}',
            "data: [DONE]",
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "hello"


def test_stream_sse_error_event(cli_module, capsys):
    """Should handle error events."""
    response = _make_mock_response(
        [
            'data: {"delta": "partial"}',
            'data: {"error": "something went wrong"}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "partial"
    captured = capsys.readouterr()
    assert "something went wrong" in captured.out


def test_stream_sse_status_event(cli_module, capsys):
    """Should display status events with tool names."""
    response = _make_mock_response(
        [
            'data: {"status": "calling", "tool_name": "search_db"}',
            'data: {"delta": "result"}',
            'data: {"done": true}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "result"
    captured = capsys.readouterr()
    assert "search_db" in captured.out


def test_stream_sse_empty_lines(cli_module):
    """Should handle empty lines gracefully."""
    response = _make_mock_response(
        [
            "",
            'data: {"delta": "hello"}',
            "",
            'data: {"done": true}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "hello"


def test_stream_sse_malformed_json(cli_module):
    """Should skip malformed JSON lines."""
    response = _make_mock_response(
        [
            "data: {not valid json",
            'data: {"delta": "good"}',
            'data: {"done": true}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "good"


def test_stream_sse_no_done_signal(cli_module, capsys):
    """Should handle stream ending without done signal."""
    response = _make_mock_response(
        [
            'data: {"delta": "hello"}',
            'data: {"delta": " there"}',
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "hello there"


def test_stream_sse_openai_chat_completion_chunks(cli_module, capsys):
    """Should handle OpenAI chat completion chunk format."""
    response = _make_mock_response(
        [
            'data: {"choices": [{"delta": {"content": "Hello"}}]}',
            'data: {"choices": [{"delta": {"content": " world"}}]}',
            "data: [DONE]",
        ]
    )
    result = cli_module.stream_sse(response)
    assert result == "Hello world"

"""Tests for Agent Channel Bridge — routing, message building, parsing."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src/ is on the path (src-layout)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ============================================================
# 1. Import & version
# ============================================================

def test_import():
    from agent_channel_bridge.config import load_config, get_route
    from agent_channel_bridge.rpc_log import init_rpc_log, log_rpc
    from agent_channel_bridge.acp_worker import AcpWorker
    from agent_channel_bridge.worker_manager import WorkerManager
    assert load_config is not None
    assert get_route is not None
    assert AcpWorker is not None
    assert WorkerManager is not None


def test_version():
    from agent_channel_bridge import __version__
    assert __version__ == "1.0.0"


# ============================================================
# 2. Route matching (the core logic)
# ============================================================

def _inject_routes(routes: dict):
    """Set up internal config state for testing get_route()."""
    import agent_channel_bridge.config as cfg
    cfg.config["routes"] = routes


def _reset_config():
    import agent_channel_bridge.config as cfg
    cfg.config.clear()


class TestRouteMatching:
    """Tests for get_route() — exact and wildcard matching."""

    def setup_method(self):
        _reset_config()

    def test_exact_private(self):
        """Exact qq:private:QQ match."""
        _inject_routes({
            "qq:private:123456": {"name": "Admin", "worker": "w1", "admin": True},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("123456", is_private=True)
        assert r == {"name": "Admin", "worker": "w1"}

    def test_exact_group_with_mention(self):
        """Exact qq:group:GROUP match when @mentioned."""
        _inject_routes({
            "qq:group:999": {"name": "Core", "worker": "w2"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("999", is_private=False, is_mention=True)
        assert r == {"name": "Core", "worker": "w2"}

    def test_group_without_mention_no_match(self):
        """Group messages without @should NOT match any route."""
        _inject_routes({
            "qq:group:999": {"name": "Core", "worker": "w2"},
            "*:*:*": {"name": "CatchAll", "worker": "w0"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("999", is_private=False, is_mention=False)
        assert r is None

    def test_wildcard_platform_type_group_mention(self):
        """qq:group:* matches any group with @."""
        _inject_routes({
            "qq:group:*": {"name": "AnyGroup", "worker": "w3"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("55555", is_private=False, is_mention=True)
        assert r == {"name": "AnyGroup", "worker": "w3"}

    def test_wildcard_platform_type_private(self):
        """qq:private:* matches any private chat."""
        _inject_routes({
            "qq:private:*": {"name": "AllPrivate", "worker": "w4"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("11111", is_private=True)
        assert r == {"name": "AllPrivate", "worker": "w4"}

    def test_wildcard_all_private(self):
        """*:*:* catches private chats with no more specific match."""
        _inject_routes({
            "*:*:*": {"name": "CatchAll", "worker": "w0"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("99999", is_private=True)
        assert r == {"name": "CatchAll", "worker": "w0"}

    def test_wildcard_all_group_mention(self):
        """*:*:* catches group @with no more specific match."""
        _inject_routes({
            "*:*:*": {"name": "CatchAll", "worker": "w0"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("99999", is_private=False, is_mention=True)
        assert r == {"name": "CatchAll", "worker": "w0"}

    def test_priority_exact_over_wildcard(self):
        """Exact match takes priority over *:*:*."""
        _inject_routes({
            "qq:private:123": {"name": "Special", "worker": "w_special"},
            "*:*:*": {"name": "CatchAll", "worker": "w0"},
        })
        from agent_channel_bridge.config import get_route
        r_exact = get_route("123", is_private=True)
        assert r_exact["worker"] == "w_special"
        r_catch = get_route("456", is_private=True)
        assert r_catch["worker"] == "w0"

    def test_priority_platform_type_over_platform(self):
        """qq:group:* should match before qq:*:*."""
        _inject_routes({
            "qq:*:*": {"name": "QQOnly", "worker": "w_qq"},
            "qq:group:*": {"name": "GroupOnly", "worker": "w_group"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("777", is_private=False, is_mention=True)
        assert r["worker"] == "w_group"  # qq:group:* has higher priority

    def test_route_with_admin_flag_ignored_in_get_route(self):
        """admin flag is preserved but not used by get_route."""
        _inject_routes({
            "qq:private:100": {"name": "Admin", "worker": "w1", "admin": True},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("100", is_private=True)
        assert r == {"name": "Admin", "worker": "w1"}

    def test_no_routes_at_all_returns_none(self):
        """Empty routes returns None for any message."""
        _inject_routes({})
        from agent_channel_bridge.config import get_route
        assert get_route("123", is_private=True) is None
        assert get_route("123", is_private=False, is_mention=True) is None

    def test_missing_worker_falls_back_to_empty(self):
        """Route without 'worker' returns empty worker string."""
        _inject_routes({
            "*:*:*": {"name": "NoWorker"},
        })
        from agent_channel_bridge.config import get_route
        r = get_route("123", is_private=True)
        assert r == {"name": "NoWorker", "worker": ""}


# ============================================================
# 3. Message segment building (_build_message_segments)
# ============================================================

class TestBuildMessageSegments:
    """Tests for _build_message_segments() — img/audio/file tags."""

    def test_plain_text(self):
        """Plain text without tags returns a single text segment."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("Hello")
        assert len(segs) == 1
        assert segs[0] == {"type": "text", "data": {"text": "Hello"}}

    def test_single_image_url(self):
        """<img>URL</img> produces an image segment + text."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("Look<img>https://example.com/pic.png</img>")
        assert len(segs) == 2
        assert segs[0] == {"type": "text", "data": {"text": "Look"}}
        assert segs[1] == {"type": "image", "data": {"url": "https://example.com/pic.png"}}

    def test_audio_tag(self):
        """<audio>URL</audio> produces a record segment."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("Listen<audio>https://example.com/sound.mp3</audio>")
        assert len(segs) == 2
        assert segs[1] == {"type": "record", "data": {"url": "https://example.com/sound.mp3"}}

    def test_file_url_tag(self):
        """<file>URL</file> produces a file segment."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("File:<file>https://example.com/doc.pdf</file>")
        assert len(segs) == 2
        assert segs[1]["type"] == "file"
        assert segs[1]["data"]["url"] == "https://example.com/doc.pdf"

    def test_multiple_images(self):
        """Multiple <img> tags produce multiple image segments."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments(
            "<img>https://a.com/1.png</img><img>https://a.com/2.png</img>"
        )
        assert len(segs) == 2
        assert segs[0]["data"]["url"] == "https://a.com/1.png"
        assert segs[1]["data"]["url"] == "https://a.com/2.png"

    def test_empty_text_only_tags(self):
        """Text with only tags and no surrounding text works."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("<img>https://example.com/pic.png</img>")
        # Only the image segment, no text since there's no surrounding text
        assert len(segs) == 1
        assert segs[0]["type"] == "image"

    def test_only_tags_no_text(self):
        """Message consisting of only tags."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("<img>https://a.com/1.png</img><audio>https://a.com/s.mp3</audio>")
        assert len(segs) == 2
        assert segs[0]["type"] == "image"
        assert segs[1]["type"] == "record"

    def test_empty_input_returns_fallback(self):
        """Empty or whitespace-only input returns '(无内容)'."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("")
        assert segs == [{"type": "text", "data": {"text": "(无内容)"}}]
        segs = _build_message_segments("   ")
        assert segs == [{"type": "text", "data": {"text": "(无内容)"}}]

    def test_text_truncated_at_2000(self):
        """Text longer than 2000 chars is truncated."""
        from agent_channel_bridge.onebot import _build_message_segments
        long_text = "x" * 3000
        segs = _build_message_segments(long_text)
        assert len(segs[0]["data"]["text"]) == 2000

    def test_case_insensitive_tags(self):
        """Tags are case-insensitive."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments("<IMG>https://example.com/pic.png</IMG>")
        assert len(segs) == 1
        assert segs[0]["type"] == "image"
        assert segs[0]["data"]["url"] == "https://example.com/pic.png"

    def test_multiline_url_in_tag(self):
        """URLs with newlines/whitespace inside tags are cleaned."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments(
            "<img>\n  https://example.com/pic.png\n</img>"
        )
        assert len(segs) == 1
        assert segs[0]["data"]["url"] == "https://example.com/pic.png"

    def test_mixed_text_and_tags(self):
        """Text with multiple content types."""
        from agent_channel_bridge.onebot import _build_message_segments
        segs = _build_message_segments(
            "Start<img>https://pic.png</img>middle<audio>https://snd.mp3</audio>end"
        )
        assert len(segs) == 3
        assert segs[0]["data"]["text"] == "Startmiddleend"
        assert segs[1]["type"] == "image"
        assert segs[2]["type"] == "record"


# ============================================================
# 4. OneBot message parsing (parse_onebot)
# ============================================================

class TestParseOnebot:
    """Tests for parse_onebot() — QQ message protocol parsing."""

    def _make_msg(self, msg_type="private", user_id="100", raw_message="hello",
                  group_id=None, message=None, sender=None):
        _sentinel = object()
        if sender is None:
            sender = {"nickname": "TestUser"}
        data = {
            "post_type": "message",
            "message_type": msg_type,
            "user_id": int(user_id),
            "raw_message": raw_message,
            "sender": sender,
        }
        if group_id:
            data["group_id"] = int(group_id)
        if message is not None:
            data["message"] = message
        return data

    def _setup_config(self, bot_qq="123456", bot_name="MyBot"):
        import agent_channel_bridge.config as cfg
        cfg.config.setdefault("applications", {})["qq"] = {
            "bot_qq": bot_qq,
            "bot_name": bot_name,
        }

    def test_parse_private_message(self):
        """Private message is parsed correctly."""
        self._setup_config()
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("private", "100", "hello")
        result = parse_onebot(data)
        assert result["type"] == "private"
        assert result["user_id"] == "100"
        assert result["message"] == "hello"
        assert result["is_mention"] is False

    def test_parse_group_message_no_mention(self):
        """Group message without @at is not a mention."""
        self._setup_config()
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("group", "200", "hello", group_id="999",
                              message=[{"type": "text", "data": {"text": "hello"}}])
        result = parse_onebot(data)
        assert result["type"] == "group"
        assert result["is_mention"] is False

    def test_parse_group_message_with_cq_at(self):
        """Group message with CQ at tag is a mention."""
        self._setup_config(bot_qq="123456")
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("group", "200", "hello", group_id="999",
                              message=[
                                  {"type": "at", "data": {"qq": "123456"}},
                                  {"type": "text", "data": {"text": " hello"}},
                              ])
        result = parse_onebot(data)
        assert result["is_mention"] is True
        assert "hello" in result["message"]

    def test_parse_group_message_with_text_at(self):
        """Group message with text @mentions the bot name."""
        self._setup_config(bot_name="MyBot")
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("group", "200", "@MyBot hello", group_id="999",
                              message=[{"type": "text", "data": {"text": "@MyBot hello"}}])
        result = parse_onebot(data)
        assert result["is_mention"] is True

    def test_parse_empty_raw_message(self):
        """Empty raw_message returns None."""
        self._setup_config()
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("private", "100", "")
        assert parse_onebot(data) is None

    def test_parse_unknown_message_type(self):
        """Unknown message_type returns None."""
        self._setup_config()
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("unknown_type", "100", "hello")
        assert parse_onebot(data) is None

    def test_parse_group_at_strip(self):
        """@mention text is stripped from the message."""
        self._setup_config(bot_qq="123456", bot_name="MyBot")
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("group", "200", "@MyBot do something", group_id="999",
                              message=[
                                  {"type": "at", "data": {"qq": "123456"}},
                                  {"type": "text", "data": {"text": " do something"}},
                              ])
        result = parse_onebot(data)
        assert result["is_mention"] is True
        assert "@MyBot" not in result["message"]

    def test_sender_name_fallback(self):
        """Missing nickname falls back to card or QQ number."""
        self._setup_config()
        from agent_channel_bridge.onebot import parse_onebot
        data = self._make_msg("private", "300", "test", sender={"card": "CardName"})
        result = parse_onebot(data)
        assert result["sender_name"] == "CardName"

        data = self._make_msg("private", "400", "test", sender={})
        result = parse_onebot(data)
        assert result["sender_name"] == "QQ400"


# ============================================================
# 5. RPC log
# ============================================================

class TestRpcLog:
    """Tests for rpc_log module."""

    def test_init_rpc_log(self):
        """init_rpc_log starts without error."""
        from agent_channel_bridge.rpc_log import init_rpc_log
        init_rpc_log()

    def test_log_rpc(self):
        """log_rpc runs without error."""
        from agent_channel_bridge.rpc_log import log_rpc
        log_rpc("test_agent", ">>", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})

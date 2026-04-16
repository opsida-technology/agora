"""Tests for the Agora Protocol message parsing."""
from agora.protocol import parse_reply, Message, to_a2a, INTENTS


class TestParseReply:

    def test_basic_reply(self):
        raw = "Hello world\n\n@intent: propose\n@addressed: all\n@next: continue"
        msg = parse_reply(raw, "agent1", 1)
        assert msg.speaker == "agent1"
        assert msg.turn == 1
        assert msg.intent == "propose"
        assert msg.addressed == "all"
        assert msg.next_action == "continue"
        assert "Hello world" in msg.content

    def test_yield_detection(self):
        raw = "I yield.\n\n@intent: yield\n@addressed: all\n@next: yield"
        msg = parse_reply(raw, "agent2", 5)
        assert msg.intent == "yield"
        assert msg.next_action == "yield"

    def test_missing_directives(self):
        raw = "Just some text without directives"
        msg = parse_reply(raw, "test", 3)
        assert msg.speaker == "test"
        assert msg.content == "Just some text without directives"

    def test_invited_property(self):
        raw = "Let me hear from expert.\n\n@intent: question\n@addressed: all\n@next: invite:expert"
        msg = parse_reply(raw, "agent1", 2)
        assert msg.invited == "expert"

    def test_intents_are_known(self):
        known = {"propose", "critique", "defend", "synthesize",
                 "question", "concede", "yield"}
        assert known.issubset(INTENTS)


class TestA2A:

    def test_to_a2a_envelope(self):
        msgs = [
            Message(speaker="a", turn=1, content="hi",
                    intent="propose", addressed="all", next_action="continue"),
        ]
        agents = [{"name": "a", "model": "claude"}]
        result = to_a2a(msgs, "test topic", agents)
        assert result["jsonrpc"] == "2.0"
        assert result["method"] == "agora/debate"
        assert result["params"]["topic"] == "test topic"
        assert len(result["params"]["transcript"]) == 1

"""Tests for the EventBus pub/sub system."""
import queue
import threading
import time

from agora.web import EventBus


class TestEventBus:

    def test_publish_subscribe(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.publish("test", {"msg": "hello"})
        payload = q.get(timeout=1)
        assert "event: test" in payload
        assert '"hello"' in payload

    def test_replay_buffer(self):
        """Subscribers get all past events on subscribe."""
        bus = EventBus()
        bus.publish("a", {"n": 1})
        bus.publish("b", {"n": 2})
        q = bus.subscribe()
        payloads = []
        while not q.empty():
            payloads.append(q.get_nowait())
        assert len(payloads) == 2
        assert "event: a" in payloads[0]
        assert "event: b" in payloads[1]

    def test_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish("x", {"v": 42})
        p1 = q1.get(timeout=1)
        p2 = q2.get(timeout=1)
        assert p1 == p2

    def test_unsubscribe(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish("after", {"v": 1})
        assert q.empty()

    def test_reset_sends_poison_pill(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.publish("before", {"v": 1})
        bus.reset()
        # Drain — should find the event then a None poison pill
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert items[-1] is None

    def test_reset_clears_log(self):
        bus = EventBus()
        bus.publish("old", {"v": 1})
        bus.reset()
        q = bus.subscribe()
        assert q.empty()

    def test_client_connected_event(self):
        bus = EventBus()
        connected = threading.Event()

        def waiter():
            bus.wait_for_client()
            connected.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        assert not connected.is_set()
        bus.subscribe()
        t.join(timeout=2)
        assert connected.is_set()

    def test_full_queue_drops_messages(self):
        """When a subscriber's queue is full, message is dropped (not blocking)."""
        bus = EventBus()
        q = bus.subscribe()
        # Fill the queue
        for i in range(4096):
            bus.publish("fill", {"i": i})
        # This should not block
        bus.publish("overflow", {"i": 9999})
        # Queue subscriber should have been removed (dead)
        assert q.full()

    def test_concurrent_publish(self):
        """Multiple threads publishing concurrently should not crash."""
        bus = EventBus()
        q = bus.subscribe()
        errors = []

        def publisher(n):
            try:
                for i in range(50):
                    bus.publish(f"t{n}", {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=publisher, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        count = 0
        while not q.empty():
            q.get_nowait()
            count += 1
        assert count == 250  # 5 threads * 50 messages

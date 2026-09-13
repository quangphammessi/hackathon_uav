"""Adapters: event bus contract, hashed embeddings, ledger hashing, and the
storefront's constraint matcher."""
from __future__ import annotations

import pytest

from agentmarket_core.adapters.bus.memory import MemoryEventBus
from agentmarket_core.adapters.embeddings.hashing import HashingEmbedder
from agentmarket_core.domain.ledger import GENESIS_HASH, compute_entry_hash


class TestEventBusContract:
    def test_publish_returns_a_self_describing_envelope(self, memory_bus):
        env = memory_bus.publish("test.topic", {"sku": "X"})
        assert env["topic"] == "test.topic"
        assert env["payload"] == {"sku": "X"}
        assert env["event_id"] and env["ts"] and env["source"] == "test"

    def test_subscribers_receive_published_events(self, memory_bus):
        seen = []
        memory_bus.subscribe("test.topic", seen.append)
        memory_bus.publish("test.topic", {"n": 1})
        assert len(seen) == 1 and seen[0]["payload"]["n"] == 1

    def test_only_matching_topics_are_delivered(self, memory_bus):
        seen = []
        memory_bus.subscribe("wanted", seen.append)
        memory_bus.publish("unwanted", {})
        assert seen == []

    def test_a_failing_handler_does_not_stop_its_siblings(self, memory_bus):
        """One broken consumer must not silence the others, or a deploy of a
        buggy handler takes down unrelated subsystems."""
        seen = []

        def boom(_):
            raise RuntimeError("handler is broken")

        memory_bus.subscribe("t", boom)
        memory_bus.subscribe("t", seen.append)
        memory_bus.publish("t", {"ok": True})
        assert len(seen) == 1

    def test_a_failing_handler_does_not_break_the_publisher(self, memory_bus):
        memory_bus.subscribe("t", lambda _: 1 / 0)
        assert memory_bus.publish("t", {})["topic"] == "t"

    def test_history_is_bounded(self):
        bus = MemoryEventBus(source="t", history_limit=10)
        for i in range(50):
            bus.publish("t", {"i": i})
        assert len(bus.recent(limit=1000)) == 10


class TestHashingEmbedder:
    def test_dimension_is_fixed_by_config(self):
        assert len(HashingEmbedder().embed_one("anything")) == 768

    def test_output_is_l2_normalized(self):
        import numpy as np

        vec = np.asarray(HashingEmbedder().embed_one("waterproof hiking boot"))
        assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)

    def test_is_deterministic_across_calls(self):
        e = HashingEmbedder()
        assert e.embed_one("hiking boot") == e.embed_one("hiking boot")

    def test_is_stable_across_processes(self):
        """Uses blake2b rather than Python's randomized hash(): vectors written
        by one process must be comparable to those written by the next."""
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, 'libs/agentmarket_core');"
            "from agentmarket_core.adapters.embeddings.hashing import HashingEmbedder;"
            "print(HashingEmbedder().embed_one('hiking boot')[:3])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        )
        assert out.returncode == 0, out.stderr
        assert str(HashingEmbedder().embed_one("hiking boot")[:3]) == out.stdout.strip()

    def test_related_text_scores_higher_than_unrelated(self):
        import numpy as np

        e = HashingEmbedder()
        boot = np.asarray(e.embed_one("waterproof hiking boot with vibram outsole"))
        similar = np.asarray(e.embed_one("hiking boot waterproof"))
        unrelated = np.asarray(e.embed_one("stainless steel kitchen blender"))
        assert float(boot @ similar) > float(boot @ unrelated)


class TestLedgerHashing:
    def test_hash_is_deterministic(self):
        a = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"sku": "X"})
        b = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"sku": "X"})
        assert a == b

    def test_key_order_does_not_change_the_hash(self):
        a = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"a": 1, "b": 2})
        b = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"b": 2, "a": 1})
        assert a == b

    def test_changing_the_payload_changes_the_hash(self):
        a = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"sku": "X"})
        b = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"sku": "Y"})
        assert a != b

    def test_changing_the_predecessor_changes_the_hash(self):
        """This is the property that makes the chain tamper-evident: editing
        any earlier entry invalidates every entry after it."""
        a = compute_entry_hash(GENESIS_HASH, "ISSUE", "vc_1", {"sku": "X"})
        b = compute_entry_hash("f" * 64, "ISSUE", "vc_1", {"sku": "X"})
        assert a != b


"""Phase 14: the session-scoped, cross-turn caches for the analytical control plane's own
expensive, pure computations -- understanding, validator findings, and verification outcomes.
Session-scoped rather than global, so one session's cached results never leak into another's.
"""

from __future__ import annotations

from src.core.session import Session


def test_understanding_cache_round_trips(session: Session) -> None:
    assert session.get_cached_understanding("key") is None
    session.cache_understanding("key", {"grain": {}})

    assert session.get_cached_understanding("key") == {"grain": {}}


def test_semantic_cache_scope_prevents_cross_policy_exact_hits() -> None:
    from src.core.semantic_cache import SemanticCache

    cache = SemanticCache()
    columns = ["revenue"]
    cache.add("sum revenue", columns, "print(df['revenue'].sum())", scope="dataset=v1|schema_only=True")

    assert cache.lookup("sum revenue", columns, scope="dataset=v1|schema_only=True") is not None
    assert cache.lookup("sum revenue", columns, scope="dataset=v1|schema_only=False") is None
    assert cache.lookup("sum revenue", columns, scope="dataset=v2|schema_only=True") is None


def test_validations_cache_round_trips(session: Session) -> None:
    assert session.get_cached_validations("key") is None
    session.cache_validations("key", [{"validator": "data", "severity": "info", "message": "ok"}])

    assert session.get_cached_validations("key") == [{"validator": "data", "severity": "info", "message": "ok"}]


def test_verification_cache_round_trips(session: Session) -> None:
    assert session.get_cached_verification("key") is None
    session.cache_verification("key", ("verified", "VERIFIED: ok"))

    assert session.get_cached_verification("key") == ("verified", "VERIFIED: ok")


def test_the_three_caches_are_independent(session: Session) -> None:
    """The same key in one cache must never be visible from another -- they are separate
    namespaces, not one dict shared across concerns."""
    session.cache_understanding("k", {"understanding": True})
    session.cache_validations("k", [{"validator": "x"}])
    session.cache_verification("k", ("verified", "ok"))

    assert session.get_cached_understanding("k") == {"understanding": True}
    assert session.get_cached_validations("k") == [{"validator": "x"}]
    assert session.get_cached_verification("k") == ("verified", "ok")


def test_understanding_cache_evicts_the_oldest_entry_past_its_limit(session: Session) -> None:
    for index in range(17):  # the default limit is 16
        session.cache_understanding(f"key-{index}", {"index": index})

    assert session.get_cached_understanding("key-0") is None, "the oldest entry should have been evicted"
    assert session.get_cached_understanding("key-16") == {"index": 16}


def test_caching_the_same_key_again_does_not_evict_anything(session: Session) -> None:
    """Overwriting an existing key is a refresh, not a new insertion -- it must not push a
    still-relevant entry out early."""
    for index in range(16):
        session.cache_understanding(f"key-{index}", {"index": index})
    session.cache_understanding("key-0", {"index": "refreshed"})  # already present

    assert session.get_cached_understanding("key-0") == {"index": "refreshed"}
    assert session.get_cached_understanding("key-15") == {"index": 15}

import pandas as pd

from src.core.infra.cache import InProcessCache
from src.core.rag.hybrid_search import reciprocal_rank_fusion
from src.core.rag.reranker import CrossEncoderReranker
from src.core.rag.retriever import ContextRetriever
from src.core.semantic_cache import SemanticCache


def test_reciprocal_rank_fusion():
    list_a = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]
    list_b = [("doc2", 0.95), ("doc3", 0.85), ("doc4", 0.6)]

    fused = reciprocal_rank_fusion(list_a, list_b, k=60)
    assert len(fused) == 4
    assert fused[0][0] == "doc2"
    assert fused[1][0] in ("doc1", "doc3")


def test_cross_encoder_reranker_fallback():
    reranker = CrossEncoderReranker(model_name="nonexistent_test_model")
    candidates = ["The quick brown fox", "Jumps over the lazy dog", "Data analysis with python"]
    results = reranker.rerank("fox jumping", candidates, top_k=2)
    assert len(results) == 2
    assert results[0].text in candidates


def test_in_process_cache_bytes_and_stale():
    cache = InProcessCache(capacity=10)
    cache.set("foo", "bar", ttl=1)
    val, is_fresh = cache.get_stale("foo")
    assert val == "bar"
    assert is_fresh is True

    cache.set_bytes("raw_vec", b"\x01\x02\x03\x04", ttl=60)
    raw = cache.get_bytes("raw_vec")
    assert raw == b"\x01\x02\x03\x04"


def test_semantic_cache_single_flight_and_evict():
    cache = SemanticCache()
    assert hasattr(cache, "_inflight")
    assert hasattr(cache, "_evict_if_needed")


def test_plan_aware_column_selection():
    retriever = ContextRetriever()
    df = pd.DataFrame(
        {
            "order_id": [1, 2],
            "customer_name": ["Alice", "Bob"],
            "discount_pct": [0.1, 0.2],
            "tax_rate": [0.05, 0.05],
        }
    )
    cols, truncated = retriever.select_columns(
        query="show customer_name",
        df=df,
        max_columns=3,
        plan="calculate total using discount_pct",
    )
    assert "customer_name" in cols
    assert "discount_pct" in cols

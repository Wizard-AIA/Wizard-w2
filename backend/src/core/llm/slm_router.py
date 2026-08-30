from __future__ import annotations

from typing import Any

class SLMRouter:
    """Fast rule and lightweight heuristic router for classifying incoming user query complexity."""
    
    _METADATA_MARKERS = [
        "schema", "column", "preview", "dataset info", "data type", "head", "tail", "first", "last"
    ]
    
    _CHITCHAT_MARKERS = [
        "hello", "hi", "hey", "thanks", "thank you", "bye", "good morning"
    ]
    
    _LIGHTWEIGHT_MARKERS = [
        "mean", "count", "min", "max", "average", "sum", "how many", "size", "dimensions"
    ]
    
    _DEEP_MARKERS = [
        "investigate", "anomal", "outlier", "hypothesis", "validate", 
        "why", "explain", "root cause", "model", "forecast", "predict", 
        "regression", "causal", "compare", "trade-off", "deep analysis",
        "join", "merge", "statistics"
    ]

    def route(self, query: str, active_columns: list[str] | None = None) -> tuple[str, str | None]:
        text = " ".join((query or "").lower().split())
        
        # Check deep investigation
        if any(marker in text for marker in self._DEEP_MARKERS):
            return ("deep", "reasoning")
            
        # Check simple metadata
        if any(marker in text for marker in self._METADATA_MARKERS):
            return ("metadata", "fast")
            
        # Check lightweight query
        if any(marker in text for marker in self._LIGHTWEIGHT_MARKERS):
            return ("lightweight", "fast")
            
        # Check chitchat
        if text in self._CHITCHAT_MARKERS or len(text) < 15:
            return ("chitchat", "fast")
            
        return ("deep", "reasoning")

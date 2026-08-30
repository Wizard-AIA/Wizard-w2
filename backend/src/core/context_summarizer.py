from typing import Any
import re
from src.utils.logging import logger

class ContextSummarizer:
    """Token-aware context compressor and summarizer."""
    
    def __init__(self, llm_provider=None):
        self.llm_provider = llm_provider

    def summarize(self, history: list[dict[str, Any]], turn_count_threshold: int = 4) -> dict[str, Any]:
        """Summarizes multi-turn history into structured memory records."""
        user_turns = [msg for msg in history if msg.get("role") == "user"]
        
        if len(user_turns) <= turn_count_threshold:
            return {"summary": "Not enough turns to summarize.", "compacted_text": ""}

        insights = []
        tables = set()
        instructions = []

        for msg in history:
            content = msg.get("content", "")
            if not content:
                continue
            if msg.get("role") == "user":
                instructions.append(content)
            else:
                tables.update(re.findall(r'`([^`]+\.(?:csv|feather))`', content))
                numbers = re.findall(r'\b\d+(?:\.\d+)?(?:%|M|K)?\b', content)
                if numbers:
                    insights.append(f"Mentioned metrics: {', '.join(numbers[:3])}")

        compacted_text = "\n".join(instructions[-3:]) if instructions else ""
        
        # Degrade gracefully to fast deterministic text compactor
        summary_text = (
            f"Active filters/instructions: {compacted_text[:200]}...\n"
            f"Dataset names: {', '.join(tables) if tables else 'None'}\n"
            f"Key findings: {len(insights)} insight(s) detected."
        )

        return {
            "summary": summary_text,
            "insights": insights[:5],
            "tables": list(tables),
            "compacted_text": compacted_text
        }

"""tiktoken-based embedding cost estimates (no OpenAI usage object from LangChain)."""

from typing import List, Tuple

import tiktoken

from .constants import EMBEDDING_COST_PER_1K_TOKENS_USD, TIKTOKEN_ENCODING


def estimate_embedding_tokens_and_cost(texts: List[str]) -> Tuple[int, float]:
    if not texts:
        return 0, 0.0
    enc = tiktoken.get_encoding(TIKTOKEN_ENCODING)
    tokens_used = sum(len(enc.encode(t)) for t in texts)
    cost_usd = round((tokens_used / 1000.0) * EMBEDDING_COST_PER_1K_TOKENS_USD, 8)
    return tokens_used, cost_usd

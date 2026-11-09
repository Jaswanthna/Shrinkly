"""Simple token estimation helpers."""


def estimate_tokens(text: str) -> int:
    """Estimate token count using the product's characters-per-token heuristic."""
    return estimate_tokens_from_characters(len(text))


def estimate_tokens_from_characters(character_count: int) -> int:
    """Estimate tokens from a character count without allocating a text copy."""
    return (character_count + 3) // 4

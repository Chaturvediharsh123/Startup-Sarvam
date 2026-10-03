import re


def normalize_text(text: str) -> str:
    """
    Normalize whitespace and surrounding punctuation without
    translating or changing the user's language.
    """

    text = text.strip()

    # Collapse repeated whitespace.
    text = re.sub(r"\s+", " ", text)

    return text
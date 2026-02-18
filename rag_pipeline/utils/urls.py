"""URL utility functions shared across the pipeline."""

import re


def extract_urls_from_text(text: str) -> list[str]:
    """
    Extract unique http/https URLs from text.

    Returns:
        List of unique URLs found in the text, in order of first appearance.
    """
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)

    # Strip trailing punctuation that is unlikely to be part of the URL
    cleaned = [u.rstrip('.,;:!?)') for u in urls]

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for url in cleaned:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return unique

"""Shared helper for constructing an Anthropic client."""

from __future__ import annotations

import anthropic
import config


def get_client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file or environment."
        )
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

"""One OpenAI-compatible endpoint for every model call: matching, the review decider and advice.

Defaults to the owner's cliproxy (Claude Haiku). `llm_base_url`, `llm_api_key` and the per-use
model settings in config override it; nothing here is provider-specific beyond the Chat
Completions shape.
"""
from __future__ import annotations

import os

DEFAULT_BASE_URL = "https://ai.m19182.dev/v1"   # cliproxy; its host port is bound to 127.0.0.1, unreachable from containers
DEFAULT_MODEL = "claude-haiku-5-5"
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}     # USD per million input / output tokens, for the app's own estimate


class Endpoint:
    def __init__(self, config):
        self.url = (config.get("llm_base_url") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        self.key = config.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")

    def headers(self):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}


def cost(model, tokens_in, tokens_out):
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return (tokens_in * price_in + tokens_out * price_out) / 1e6

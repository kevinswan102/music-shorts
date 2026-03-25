"""
LLM Client Factory
==================
Abstracts the AI provider so you can switch between paid and free tiers
via a single environment variable.

LLM_PROVIDER=openai  (default)
    Uses OpenAI GPT-4o-mini.  Requires OPENAI_API_KEY.
    Cost: ~$0.15 / 1M input tokens.

LLM_PROVIDER=groq
    Uses Groq's free tier (Llama 3.3 70B).  Requires GROQ_API_KEY.
    Free plan: 14,400 req/day, 30 req/min — plenty for 1 run/day.
    Get a free key at: https://console.groq.com
    Uses the same openai Python SDK with a custom base_url — no extra deps.

Usage:
    from llm_client import get_llm_client
    client, model = get_llm_client()
    resp = client.chat.completions.create(model=model, messages=[...])
"""

import os


def get_llm_client():
    """Return (client, model_name) ready for chat.completions.create().

    The client is always an openai.OpenAI instance — Groq uses the same
    OpenAI-compatible API, so no extra library is needed.
    """
    provider = os.getenv('LLM_PROVIDER', 'openai').lower()

    if provider == 'groq':
        import openai
        client = openai.OpenAI(
            api_key=os.getenv('GROQ_API_KEY', ''),
            base_url='https://api.groq.com/openai/v1',
        )
        model = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
        print(f"LLM PROVIDER: GROQ ({model}) — free tier")
        return client, model

    else:  # default: openai
        import openai
        client = openai.OpenAI()  # reads OPENAI_API_KEY automatically
        model = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
        print(f"LLM PROVIDER: OPENAI ({model}) — paid")
        return client, model


def llm_available() -> bool:
    """Return True if any LLM provider is configured."""
    provider = os.getenv('LLM_PROVIDER', 'openai').lower()
    if provider == 'groq':
        return bool(os.getenv('GROQ_API_KEY', '').strip())
    return bool(os.getenv('OPENAI_API_KEY', '').strip())

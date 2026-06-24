"""
Stanford Health Care AI Hub client (Azure OpenAI-compatible).

Drop-in replacement for ai_client.chat_completion when AI_BACKEND=aihub.
Used by the RExI pipeline path, where extraction does NOT go through the
REDCap SecureChatAI External Module but directly to AI Hub.

The deployment (model) is baked into AI_HUB_BASE_URL, e.g.:
  https://aihubapi.stanfordhealthcare.org/azure-openai/deployments/gpt-4-1/chat/completions?api-version=2024-06-01

Environment variables:
  AI_HUB_BASE_URL   Full Azure OpenAI chat/completions URL (incl. deployment + api-version).
  AI_HUB_API_KEY    AI Hub API key (sent as the `api-key` header).
"""

import os
from dotenv import load_dotenv
from rag_pipeline.utils.http import get_session
from rag_pipeline.utils.logger import setup_logger

load_dotenv()

logger = setup_logger()

# The deployment is fixed in the URL; this is only used for logging/compat with ai_client.
DEFAULT_MODEL = "gpt-4-1"


def chat_completion(
    prompt: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 32000,
    system_prompt: str | None = None,
    model_hint: str | None = None,
    json_schema: dict | None = None,
) -> str:
    """
    Send a prompt to AI Hub (Azure OpenAI chat/completions) and return the content string.

    Signature mirrors ai_client.chat_completion so sliding_window can swap gateways
    with a single import change. `model`/`model_hint` are accepted for interface
    compatibility but ignored — the deployment is fixed in AI_HUB_BASE_URL.
    """
    base_url = os.getenv("AI_HUB_BASE_URL")
    api_key = os.getenv("AI_HUB_API_KEY")

    if not base_url:
        raise ValueError("Missing AI_HUB_BASE_URL in environment.")
    if not api_key:
        raise ValueError("Missing AI_HUB_API_KEY in environment.")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": json_schema,
        }

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    try:
        resp = get_session().post(base_url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"AI Hub returned no choices: {data}")

        content = choices[0].get("message", {}).get("content")
        if content is None:
            raise RuntimeError(f"AI Hub response missing message content: {data}")

        return content

    except Exception as e:
        logger.error(f"AI Hub API error: {e}")
        raise RuntimeError(f"AI Hub API call failed: {e}")

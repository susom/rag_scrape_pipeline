import os
import requests
from dotenv import load_dotenv
from rag_pipeline.utils.logger import setup_logger

# ensure .env is loaded into the process
load_dotenv()

logger = setup_logger()

# Default model for CRIP extraction
DEFAULT_MODEL = "gpt-4.1"

# Available models via SecureChatAI adapter
AVAILABLE_MODELS = [
    "gpt-4.1",
    "gpt-4o",
    "gpt-5",
    "o1",
    "o3-mini",
    "claude",
    "deepseek",
    "llama-Maverick",
    "llama3370b",
    "gemini25pro",
    "gemini20flash",
]


def chat_completion(
    prompt: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
    system_prompt: str | None = None,
    model_hint: str | None = None,
) -> str:
    """
    Sends prompt to SecureChatAI External Module via REDCap API.

    Args:
        prompt: The user prompt to send
        model: Model identifier (legacy, use model_hint instead)
        temperature: Sampling temperature
        max_tokens: Maximum tokens in response
        system_prompt: System prompt to prepend
        model_hint: Preferred model (e.g., "gpt-4.1"). Takes precedence over model.

    Returns:
        The AI response content as a string
    """
    # fetch fresh env vars every call
    redcap_api_url = os.getenv("REDCAP_API_URL", "http://localhost/api/")
    redcap_api_token = os.getenv("REDCAP_API_TOKEN")

    if not redcap_api_token:
        raise ValueError("Missing REDCAP_API_TOKEN in environment.")

    # Resolve model: model_hint > model > DEFAULT_MODEL
    resolved_model = model_hint or model or DEFAULT_MODEL

    payload = {
        "token": redcap_api_token,
        "content": "externalModule",
        "prefix": "secure_chat_ai",
        "action": "callAI",
        "format": "json",
        "returnFormat": "json",
        "model": resolved_model,
        "model_hint": resolved_model,
        "temperature": str(temperature),
        "max_tokens": str(max_tokens),
    }

    if system_prompt:
        prompt = f"{system_prompt}\n\n{prompt}"
    payload["prompt"] = prompt

    try:
        resp = requests.post(redcap_api_url, data=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"SecureChatAI API returned error: {data}")

        return data["content"]

    except Exception as e:
        logger.error(f"SecureChatAI API error: {e}")
        raise RuntimeError(f"SecureChatAI API call failed: {e}")


# Legacy alias for backward compatibility
deepseek_chat = chat_completion

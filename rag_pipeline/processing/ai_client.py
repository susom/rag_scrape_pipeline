import os
import requests
from dotenv import load_dotenv
from rag_pipeline.utils.logger import setup_logger

# ensure .env is loaded into the process
load_dotenv()

logger = setup_logger()

def deepseek_chat(
    prompt: str,
    model: str = "deepseek",
    temperature: float = 0.2,
    max_tokens: int = 800,
    system_prompt: str | None = None,
) -> str:
    """
    Sends prompt to SecureChatAI External Module via REDCap API.
    """

    # fetch fresh env vars every call
    redcap_api_url = os.getenv("REDCAP_API_URL", "http://localhost/api/")
    redcap_api_token = os.getenv("REDCAP_API_TOKEN")

    logger.info(f"DEBUG: fucker bitch deepseek_chat env check URL={redcap_api_url}, TOKEN={redcap_api_token[:6]+'…' if redcap_api_token else 'None'}")

    if not redcap_api_token:
        raise ValueError("Missing REDCAP_API_TOKEN in environment.")

    payload = {
        "token": redcap_api_token,
        "content": "externalModule",
        "prefix": "secure_chat_ai",
        "action": "callAI",
        "format": "json",
        "returnFormat": "json",
        "model": model,
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

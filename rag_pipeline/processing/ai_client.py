# rag_pipeline/processing/ai_client.py
import os
import requests
from rag_pipeline.utils.logger import setup_logger

DEESEEK_URL = "https://apim.stanfordhealthcare.org/deepseekr1/v1/chat/completions"
DEESEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
logger = setup_logger()

def deepseek_chat(
    prompt: str,
    model: str = "deepseek-chat",
    temperature: float = 0.2,     # lower = less chatty, more deterministic
    max_tokens: int = 800,        # safe for deepseek-chat
    system_prompt: str | None = None,
) -> str:
    headers = {
        "Ocp-Apim-Subscription-Key": DEESEEK_KEY,
        "Content-Type": "application/json",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 1,
        "stream": False,
    }

    resp = requests.post(DEESEEK_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected DeepSeek response: {data}")

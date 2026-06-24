"""
AI gateway selector.

Mirrors the RAG_BACKEND pattern in orchestrator.py: a single env var selects
which extraction gateway the pipeline uses, so the rest of the code imports
chat_completion / DEFAULT_MODEL from here without caring about the backend.

  AI_BACKEND=securechat (default) -> REDCap SecureChatAI External Module (SOM/REDCap)
  AI_BACKEND=aihub                -> Stanford Health Care AI Hub direct (RExI)
"""

import os
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

_AI_BACKEND = os.getenv("AI_BACKEND", "securechat").lower()

if _AI_BACKEND == "aihub":
    from rag_pipeline.processing.aihub_client import chat_completion, DEFAULT_MODEL
    logger.info("AI_BACKEND=aihub -> using AI Hub (Azure OpenAI) for extraction")
else:
    from rag_pipeline.processing.ai_client import chat_completion, DEFAULT_MODEL
    logger.info(f"AI_BACKEND={_AI_BACKEND} -> using SecureChatAI (REDCap EM) for extraction")

__all__ = ["chat_completion", "DEFAULT_MODEL"]

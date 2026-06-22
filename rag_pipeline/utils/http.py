"""Shared HTTP session with connection pooling.

A single module-level requests.Session reused across the process so outbound
calls (SecureChatAI, REDCap RAG EM, pgvector backend, Microsoft Graph) keep
TCP+TLS connections alive instead of opening a fresh connection per request.

Application code keeps its own retry/backoff loops, so the pooled adapter is
configured with max_retries=0 to avoid changing retry semantics — this is a
pure connection-reuse optimization.
"""

import requests
from requests.adapters import HTTPAdapter

_session: requests.Session | None = None


def get_session() -> requests.Session:
    """Return the shared, connection-pooled requests.Session."""
    global _session
    if _session is None:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
    return _session

"""
Optional secrets-file loader for the GKE deployment.

The RExI GKE pod can receive its secrets in either of two shapes:

  1. As plain environment variables (a Kubernetes Secret consumed via
     ``envFrom``). This is the default and needs no loader.

  2. As a mounted file, ``/var/secrets/secret.properties`` (Java ``.properties``
     format), produced by the Secret Store CSI driver from Secret Manager — the
     same ``gke-app-sa-secrets`` mount that ``rexi-app`` uses.

This module bridges shape (2) to shape (1): if the properties file exists, its
``key=value`` lines are read and exported into ``os.environ`` **without
overriding anything already set in the environment**. That means a plain-Secret
env var always wins, so switching from the temporary env-var Secret to the
long-term CSI file mount is a manifest-only change — no image rebuild.

Both the raw property key and, for dotted Java-style keys, an alias are exported
(e.g. ``ai.api.key`` -> ``AI_HUB_API_KEY``) so the RExI app's existing secret
keys map onto the names this pipeline reads.
"""

import os

# Map the RExI Java app's dotted property names onto the env var names this
# pipeline reads. Extend as new keys are shared through the same secret file.
_KEY_ALIASES = {
    "ai.api.key": "AI_HUB_API_KEY",
    "sharepoint.client.secret": "SHAREPOINT_SITE_REXI_CLIENT_SECRET",
}

_DEFAULT_PATH = "/var/secrets/secret.properties"


def _parse_properties(text):
    """Parse a minimal Java ``.properties`` file into a dict.

    Supports ``key=value`` and ``key:value`` lines; ignores blank lines and
    ``#``/``!`` comments. Values are taken verbatim (no escape processing),
    which is sufficient for opaque secret strings.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ("#", "!"):
            continue
        sep = None
        for candidate in ("=", ":"):
            idx = line.find(candidate)
            if idx != -1 and (sep is None or idx < sep[1]):
                sep = (candidate, idx)
        if sep is None:
            continue
        key = line[: sep[1]].strip()
        value = line[sep[1] + 1 :].strip()
        if key:
            out[key] = value
    return out


def load_secret_file(path=None):
    """Load secrets from a mounted properties file into ``os.environ``.

    No-op if the file does not exist. Never overrides an existing environment
    variable, so env-var Secrets take precedence over the file. Returns the list
    of environment variable names that were set from the file.

    The path can be overridden with the ``SECRETS_PROPERTIES_FILE`` env var.
    """
    path = path or os.getenv("SECRETS_PROPERTIES_FILE", _DEFAULT_PATH)
    if not path or not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            props = _parse_properties(fh.read())
    except OSError:
        return []

    set_names = []
    for key, value in props.items():
        # Export under the alias name our code reads, if one is defined.
        alias = _KEY_ALIASES.get(key)
        if alias and not os.environ.get(alias):
            os.environ[alias] = value
            set_names.append(alias)

        # Also export the key verbatim if it already looks like an env var
        # (UPPER_SNAKE_CASE) and isn't already set.
        if key == key.upper() and "." not in key and not os.environ.get(key):
            os.environ[key] = value
            set_names.append(key)

    return set_names

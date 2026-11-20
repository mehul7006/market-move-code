"""Read/write the local .env without ever printing secrets.

The file is gitignored. Values written here (Upstox token, access panel key,
optional site password) take effect without editing code.
"""
from __future__ import annotations

import os
import secrets
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
# On a host without a writable project folder (Render, Docker) point DESK_ENV_FILE at a persistent disk path.
ENV_PATH = os.environ.get("DESK_ENV_FILE") or os.path.join(ROOT, ".env")
# Settings that may also come from real environment variables (Render dashboard / docker -e). A value saved in the
# file (for example a token pasted in the Access panel) wins over the environment variable.
ENV_KEYS = ("DESK_USER", "DESK_PASS", "DESK_ADMIN_KEY", "UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN")


def claims(token):
    """Payload of an Upstox JWT ({} when it is not one)."""
    try:
        import base64, json
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def _exp(token):
    return int(claims(token).get("exp") or 0)


def token_rank(token):
    """Which of two tokens should win: a VALID one first, then a normal daily token over a long-lived read-only Analytics token
    (isExtended), then the most recently ISSUED one (iat). So the token you pasted today always beats an older or longer-lived one."""
    c = claims(token)
    import time
    return (int(c.get("exp") or 0) > time.time(), not c.get("isExtended"), int(c.get("iat") or 0))


def _from_environ():
    return {k: os.environ[k].strip() for k in ENV_KEYS if os.environ.get(k, "").strip()}

_lock = threading.Lock()
_cache = {"mtime": None, "file": {}}


def load_env(path=ENV_PATH):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _merge(env_vars, file_vars):
    merged = {**env_vars, **file_vars}
    # Tokens can come from two places (the saved file and the host's environment variable) and into either of two slots. Sort them by KIND, whatever
    # slot they were put in, then let the best one of each kind win - re-decided on every call because tokens expire while the server runs:
    #   UPSTOX_ACCESS_TOKEN    <- the best normal daily token (valid first, then the most recently issued)
    #   UPSTOX_ANALYTICS_TOKEN <- the best long-lived read-only token (isExtended)
    pool = [v for src in (env_vars, file_vars) for key in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN") for v in [src.get(key)] if v]
    daily = [t for t in pool if not claims(t).get("isExtended")]
    ext = [t for t in pool if claims(t).get("isExtended")]
    for key, group in (("UPSTOX_ACCESS_TOKEN", daily), ("UPSTOX_ANALYTICS_TOKEN", ext)):
        if group:
            merged[key] = max(group, key=token_rank)
        else:
            merged.pop(key, None)
    return merged


def current(path=ENV_PATH):
    """Settings from the saved file plus real environment variables. The file is re-read only when it changes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _merge(_from_environ(), {})
    with _lock:
        if _cache["mtime"] != mtime:
            _cache["file"] = load_env(path)
            _cache["mtime"] = mtime
        file_vars = dict(_cache["file"])
    return _merge(_from_environ(), file_vars)


def write_var(key, value, path=ENV_PATH):
    """Replace or append one KEY=value line; other lines are preserved verbatim."""
    with _lock:
        lines, replaced = [], False
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith(key + "="):
                        lines.append(f"{key}={value}\n")
                        replaced = True
                    else:
                        lines.append(line)
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={value}\n")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
        _cache["mtime"] = None
    return value


def ensure_secret(key, prefix="", length=24, path=ENV_PATH):
    """Return the existing secret, or create a strong one and save it."""
    env = load_env(path) if path != ENV_PATH else current(path)
    value = env.get(key)
    if value:
        return value, False
    value = prefix + secrets.token_urlsafe(length)
    write_var(key, value, path)
    return value, True


def ensure_admin_key(path=ENV_PATH):
    return ensure_secret("DESK_ADMIN_KEY", length=24, path=path)


def ensure_site_login(path=ENV_PATH):
    """Optional HTTP Basic auth for public links. Returns (user, password, created)."""
    env = load_env(path)
    user = env.get("DESK_USER") or "desk"
    password = env.get("DESK_PASS")
    created = False
    if not password:
        password = secrets.token_urlsafe(9)
        write_var("DESK_USER", user, path)
        write_var("DESK_PASS", password, path)
        created = True
    return user, password, created


def basic_auth(passwords_path):
    """nginx auth_basic_user_file line in the {SHA} scheme (stdlib only)."""
    import base64
    import hashlib

    user, password, _ = ensure_site_login()
    digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    with open(passwords_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{user}:{{SHA}}{digest}\n")
    return user, password

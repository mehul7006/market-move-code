"""Read/write the local .env without ever printing secrets.

The file is gitignored. Values written here (Upstox token, access panel key,
optional site password) take effect without editing code.
"""
from __future__ import annotations

import os
import secrets
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")

_lock = threading.Lock()
_cache = {"mtime": None, "env": {}}


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


def current(path=ENV_PATH):
    """Cached read keyed on the file's mtime, so edits apply without a restart."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != mtime:
            _cache["env"] = load_env(path)
            _cache["mtime"] = mtime
        return dict(_cache["env"])


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

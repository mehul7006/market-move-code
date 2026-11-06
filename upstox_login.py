"""Refresh the Upstox access token (valid ~1 day) and save it into .env.

Usage:
  python upstox_login.py [redirect_uri]

Then: open the printed URL, log in, copy the `code=` value from the redirect
address bar, paste it here, restart the server.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import upstox

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")
env = upstox.load_env(ENV_PATH)

key = env.get("UPSTOX_API_KEY")
secret = env.get("UPSTOX_API_SECRET")
redirect = sys.argv[1] if len(sys.argv) > 1 else env.get("UPSTOX_REDIRECT_URI")

if not (key and secret):
    sys.exit("Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env first.")
if not redirect:
    sys.exit("Pass your app's redirect URI:  python upstox_login.py https://your-redirect-uri")

login_url = "https://api.upstox.com/v2/login/authorization/dialog?" + urllib.parse.urlencode({
    "response_type": "code", "client_id": key, "redirect_uri": redirect})

print("1) Open this URL and log in:\n\n" + login_url)
print("\n2) Copy the `code` value from the redirected address bar.\n")
code = input("Paste code: ").strip()

body = urllib.parse.urlencode({
    "code": code, "client_id": key, "client_secret": secret,
    "redirect_uri": redirect, "grant_type": "authorization_code",
}).encode()
req = urllib.request.Request("https://api.upstox.com/v2/login/authorization/token", data=body,
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/x-www-form-urlencoded",
                                      "User-Agent": upstox._UA})
try:
    res = json.load(urllib.request.urlopen(req, timeout=20))
except urllib.error.HTTPError as exc:
    sys.exit("Token exchange failed: " + exc.read().decode("utf-8", "ignore")[:300])

token = res.get("access_token")
if not token:
    sys.exit("No access_token in response: " + json.dumps(res)[:300])

lines, replaced = [], False
if os.path.exists(ENV_PATH):
    with open(ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                lines.append("UPSTOX_ACCESS_TOKEN=" + token + "\n")
                replaced = True
            else:
                lines.append(line)
if not replaced:
    lines.append("UPSTOX_ACCESS_TOKEN=" + token + "\n")
with open(ENV_PATH, "w", encoding="utf-8") as fh:
    fh.writelines(lines)

print("New token saved to .env. Restart the server: python server.py")

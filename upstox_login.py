"""Refresh the Upstox access token (valid ~1 day) and save it into .env.

Usage:
  python upstox_login.py [redirect_uri]

Then: open the printed URL, log in, copy the `code=` value from the redirect
address bar, paste it here, restart the server.
"""
import sys
import urllib.parse

import desk_env
import upstox

env = desk_env.load_env()

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

try:
    token = upstox.exchange_code(code, redirect, key, secret)
except upstox.UpstoxError as exc:
    sys.exit(str(exc))

desk_env.write_var("UPSTOX_ACCESS_TOKEN", token)
print("New token saved to .env. The running server picks it up on its next request; "
      "or paste it in the Access panel at http://localhost:8787 instead.")

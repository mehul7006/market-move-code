"""Expose the desk outside this machine: ngrok tunnel + ready nginx reverse proxy.

  python share.py                 start ngrok, print the public URL and login, write nginx config
  python share.py --nginx-only    write nginx config only (no tunnel)
  python share.py --status        show the current tunnel URL and credentials
  python share.py --stop          stop the ngrok process this script started

The site password (DESK_USER / DESK_PASS) and the Access-panel key (DESK_ADMIN_KEY)
are created once in the gitignored .env. Nothing here prints the Upstox token.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

import desk_env

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(ROOT, "tools")
NGINX_DIR = os.path.join(ROOT, "nginx")
NGROK_API = "http://127.0.0.1:4040/api"
NGROK_ON_PATH = shutil.which("ngrok")

INSTALL_URLS = {
    "Windows": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip",
    "Linux": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip",
    "Darwin": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-amd64.zip",
}
EXE = "ngrok.exe" if os.name == "nt" else "ngrok"
PID_FILE = os.path.join(TOOLS, "ngrok.pid")


def find_ngrok():
    if NGROK_ON_PATH:
        return NGROK_ON_PATH
    local = os.path.join(TOOLS, EXE)
    if os.path.isfile(local):
        return local
    for pattern in (r"E:/ngrok.exe", r"E:/nginx*/ngrok.exe", r"C:/Program Files/ngrok/ngrok.exe",
                    r"C:/Program Files (x86)/ngrok/ngrok.exe"):
        hit = glob.glob(pattern)
        if hit:
            return hit[0]
    return None


def install_ngrok():
    url = INSTALL_URLS.get(platform.system())
    if not url:
        sys.exit("ngrok is not installed and there is no automatic download for this OS. "
                 "Install it from https://ngrok.com/download and re-run share.py")
    os.makedirs(TOOLS, exist_ok=True)
    print("ngrok not found - downloading the official v3 binary into tools/ ...")
    archive = os.path.join(TOOLS, "ngrok.zip")
    try:
        urllib.request.urlretrieve(url, archive)
    except Exception as exc:
        sys.exit(f"download failed: {exc}. Install ngrok manually and re-run.")
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if os.path.basename(name).lower() == EXE:
                zf.extract(name, TOOLS)
                src = os.path.join(TOOLS, name)
                dst = os.path.join(TOOLS, EXE)
                if os.path.abspath(src) != os.path.abspath(dst):
                    os.replace(src, dst)
                break
    os.remove(archive)
    if not os.path.isfile(os.path.join(TOOLS, EXE)):
        sys.exit("download finished but tools/ngrok was not found; install ngrok manually.")
    print("ngrok installed at", os.path.join(TOOLS, EXE))
    return os.path.join(TOOLS, EXE)


def ngrok_tunnels():
    try:
        with urllib.request.urlopen(NGROK_API + "/tunnels", timeout=1.2) as resp:
            return json.loads(resp.read().decode("utf-8")).get("tunnels") or []
    except Exception:
        return []


def tunnel_for_port(port):
    for t in ngrok_tunnels():
        addr = str((t.get("config") or {}).get("addr") or "")
        if str(port) in addr and str(t.get("public_url", "")).startswith("https://"):
            return t["public_url"]
    return None


def has_ngrok_login(binary):
    """True when ngrok already has a saved authtoken on this machine."""
    try:
        return subprocess.run([binary, "config", "check"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except Exception:
        return False


def ensure_authtoken(cli_value=None):
    token = cli_value or desk_env.load_env().get("NGROK_AUTHTOKEN")
    if token:
        return token
    print("ngrok needs your authtoken once (dashboard.ngrok.com/get-started/your-authtoken).")
    token = input("Paste ngrok authtoken: ").strip()
    if not token:
        sys.exit("No authtoken - the tunnel cannot start.")
    desk_env.write_var("NGROK_AUTHTOKEN", token)
    return token


def start_tunnel(binary, port, authtoken=None):
    url = tunnel_for_port(port)
    if url:
        print("Reusing the tunnel already running for port", port)
        return url
    if ngrok_tunnels():
        sys.exit("ngrok's local API (4040) is busy with a tunnel for another port. "
                 "Stop it first: python share.py --stop")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    cmd = [binary, "http", str(port)]
    if authtoken:
        cmd.append("--authtoken=" + authtoken)
    cmd += ["--log=stdout", "--log-level=warn"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    os.makedirs(TOOLS, exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    for _ in range(40):
        url = tunnel_for_port(port)
        if url:
            return url
        time.sleep(0.5)
    sys.exit("ngrok started but no HTTPS tunnel appeared in 20s. Run 'ngrok http %d' "
             "manually to see the error." % port)


def stop_tunnel():
    stopped = False
    if os.path.isfile(PID_FILE):
        try:
            pid = int(open(PID_FILE, encoding="utf-8").read().strip())
            os.kill(pid, 15)
            stopped = True
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    for t in ngrok_tunnels():
        try:
            name = urllib.parse.quote(str(t.get("name", "")), safe="")
            req = urllib.request.Request(f"{NGROK_API}/tunnels/{name}/stop", method="POST")
            urllib.request.urlopen(req, timeout=1.5).read()
            stopped = True
        except Exception:
            pass
    print("Tunnel stopped." if stopped else "No tunnel was running.")


def find_nginx():
    hit = shutil.which("nginx")
    if hit:
        return hit
    for pattern in (r"E:/nginx-*/nginx.exe", r"C:/nginx-*/nginx.exe",
                    r"C:/Program Files/nginx*/nginx.exe"):
        found = glob.glob(pattern)
        if found:
            return sorted(found)[-1]
    return None


def write_nginx(upstream_port, http_port, auth=True):
    """Write nginx/market-desk.conf + nginx/.htpasswd; nginx proxies :http_port -> :upstream_port."""
    os.makedirs(os.path.join(NGINX_DIR, "logs"), exist_ok=True)
    conf_path = os.path.join(NGINX_DIR, "market-desk.conf")
    passwd_path = os.path.join(NGINX_DIR, ".htpasswd")
    win = lambda p: p.replace("\\", "/")
    login = ""
    if auth:
        user, password = desk_env.basic_auth(passwd_path)
        login = (f'auth_basic "Market Signal Desk";\n'
                 f'        auth_basic_user_file {win(passwd_path)};\n')
    else:
        login = "# auth_basic disabled (--no-password)\n"
    conf = f"""# Generated by share.py - nginx reverse proxy for the Market Signal Desk.
# Run:  nginx -p <nginx prefix> -c "{win(conf_path)}"
worker_processes 1;
error_log {win(os.path.join(NGINX_DIR, "logs", "error.log"))} warn;
events {{ worker_connections 1024; }}

http {{
    default_type application/octet-stream;
    sendfile on;
    gzip on;
    gzip_types text/plain text/css application/json application/javascript;
    access_log "{win(os.path.join(NGINX_DIR, "logs", "access.log"))}";

    upstream market_desk {{
        server 127.0.0.1:{upstream_port};
        keepalive 8;
    }}

    server {{
        listen {http_port};
        server_name _;
        client_max_body_size 1m;

        {login}
        location / {{
            proxy_pass http://market_desk;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_connect_timeout 15s;
            proxy_send_timeout 300s;
            proxy_read_timeout 300s;
            proxy_buffering off;
        }}
    }}
}}
"""
    with open(conf_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(conf)
    return conf_path, (user, password) if auth else (None, None)


def print_nginx_help(conf_path, http_port):
    nginx = find_nginx()
    print(f"nginx config : {conf_path}  (proxy :{http_port} -> app)")
    if nginx:
        prefix = os.path.dirname(nginx)
        print(f"Start nginx  : \"{nginx}\" -p \"{prefix}\" -c \"{conf_path}\"")
        print(f"Reload       : \"{nginx}\" -p \"{prefix}\" -c \"{conf_path}\" -s reload")
    else:
        print("nginx        : not found on PATH; install it (E:\\nginx-1.28.3 works) and run "
              "`nginx -c <config above>`.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787, help="dashboard port (default 8787)")
    ap.add_argument("--nginx-port", type=int, default=8899, help="nginx listen port (default 8899; 8080 is often taken)")
    ap.add_argument("--nginx-only", action="store_true", help="write nginx config, skip ngrok")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--no-password", action="store_true", help="do not enable site basic auth")
    ap.add_argument("--authtoken", help="ngrok authtoken (saved to .env)")
    args = ap.parse_args()

    if args.stop:
        stop_tunnel()
        return
    if args.status:
        url = tunnel_for_port(args.port)
        env = desk_env.load_env()
        print("Public URL :", url or "not running")
        if env.get("DESK_PASS"):
            print("Site login :", f"{env.get('DESK_USER', 'desk')} / {env['DESK_PASS']}")
        else:
            print("Site login : OFF (share.py sets one before a public tunnel starts)")
        print("Admin key  :", env.get("DESK_ADMIN_KEY", "(not set - start server.py once)"))
        return

    admin_key, created = desk_env.ensure_admin_key()
    conf_path, creds = write_nginx(args.port, args.nginx_port, auth=not args.no_password)
    print_nginx_help(conf_path, args.nginx_port)

    if args.nginx_only:
        print("Admin key  :", admin_key)
        if creds[0]:
            print("Site login :", f"{creds[0]} / {creds[1]}")
        return

    binary = find_ngrok() or install_ngrok()
    token = args.authtoken or desk_env.load_env().get("NGROK_AUTHTOKEN")
    if not token and not has_ngrok_login(binary):
        token = ensure_authtoken()
    url = start_tunnel(binary, args.port, token)

    print("\n" + "-" * 62)
    print("Public URL :", url)
    print("Open       : anywhere - phone, other laptop, remote desktop")
    if creds[0]:
        print("Site login :", f"{creds[0]} / {creds[1]}   (browser basic-auth prompt)")
    else:
        print("Site login : OFF - the link is open to anyone who has it")
    print("Admin key  :", admin_key, "  (Access panel, pasted once per browser)")
    print("Upstox token: paste it in Access on that page - no restart, saved to .env")
    print("-" * 62)
    if desk_env.load_env().get("DESK_PASS") and not creds[0]:
        print("Note: DESK_PASS is set in .env, so the app asks for the site login even on :%d."
              % args.port)


if __name__ == "__main__":
    main()

"""Read-only probe of an Araknis 310 router's web API.

Logs in once, reads the status endpoints the router's dashboard uses, logs out.
Changes nothing on the router. Stops after one failed login: the router locks
the login for 30 minutes after 5 failures.

The login comes from .env in the repo root (ARAKNIS_USERNAME,
ARAKNIS_PASSWORD), or is asked for. It is never printed or saved, and neither
is the session ID.

Writes the raw replies to router_probe_<host>.json next to this script.

Usage: python3 scripts/router_probe.py 192.168.1.1
"""

import base64
import getpass
import json
import pathlib
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import requests

# Hard-coded in the router's /login.html: the password is AES-128-CTR
# encrypted with this key and IV, then base64 encoded.
AES_KEY = bytes.fromhex("7C37B3AB57CB46B491126CBE93901B55")
AES_IV = bytes.fromhex("AD9A57619E8A4F200C0015A02EA1DCDE")

# The dashboard's apiUrls, plus the session check. All GET, all read-only.
READ_ENDPOINTS = (
    "stat",
    "system_status",
    "wan_status_cards",
    "port_status_table",
    "client_count",
    "dhcp_status",
    "firewall_status",
    "vpn_tunnel_status",
    "login_session",
)


def load_login() -> tuple[str, str]:
    """Read the login from .env in the repo root, or ask for it."""
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    values: dict[str, str] = {}
    if env.is_file():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    user = values.get("ARAKNIS_USERNAME") or input("Username: ")
    pw = values.get("ARAKNIS_PASSWORD") or getpass.getpass("Password (hidden): ")
    if values.get("ARAKNIS_PASSWORD"):
        print("Using the login from .env")
    return user, pw


def encrypt_password(pw: str) -> str:
    """Encrypt the password the way the router's login page does."""
    enc = Cipher(algorithms.AES(AES_KEY), modes.CTR(AES_IV)).encryptor()
    return base64.b64encode(enc.update(pw.encode()) + enc.finalize()).decode()


def main() -> None:
    """Log in, save the raw replies, log out."""
    host = sys.argv[1] if len(sys.argv) > 1 else input("Router IP: ").strip()
    user, pw = load_login()
    base = f"http://{host}"
    s = requests.Session()

    r = s.post(
        f"{base}/cgi-bin/login.cgi",
        data={"username": user, "password": encrypt_password(pw)},
        timeout=20,
        allow_redirects=False,
    )
    del pw
    print("login HTTP", r.status_code, "| Set-Cookie names:", list(r.cookies.keys()))
    try:
        login = json.loads(r.text)
    except ValueError:
        print("Login reply was not JSON:", r.text[:300])
        return
    # Print everything except the session ID.
    print("login reply:", {k: v for k, v in login.items() if k != "login_ID"})
    print("login_ID present:", "login_ID" in login)
    if str(login.get("login_status")) != "1":
        print("Login failed; stopping (no retry, to avoid the 30 minute lockout).")
        return
    s.cookies.set("login_ID", str(login["login_ID"]))

    out: dict[str, object] = {}
    try:
        for ep in READ_ENDPOINTS:
            resp = s.get(f"{base}/cgi-bin/{ep}.cgi", timeout=20, allow_redirects=False)
            try:
                out[ep] = json.loads(resp.text)
            except ValueError:
                out[ep] = {"_http": resp.status_code, "_raw_text": resp.text[:2000]}
            print(f"{ep}: HTTP {resp.status_code}, {len(resp.text)} bytes")
        # The main page, to find page-specific scripts and endpoints.
        page = s.get(f"{base}/araknis-router.html", timeout=20, allow_redirects=False)
        out["_araknis_router_html"] = {"_http": page.status_code, "text": page.text}
        print(f"araknis-router.html: HTTP {page.status_code}, {len(page.text)} bytes")
    finally:
        s.post(f"{base}/cgi-bin/login.cgi", data={"logout": "1"}, timeout=5)
        print("logged out")

    # login_session repeats the session ID; don't save it.
    session_reply = out.get("login_session")
    if isinstance(session_reply, dict):
        session_reply.pop("login_ID", None)
    path = pathlib.Path(__file__).with_name(f"router_probe_{host}.json")
    path.write_text(json.dumps(out, indent=2))
    print("saved", path.name)


if __name__ == "__main__":
    main()

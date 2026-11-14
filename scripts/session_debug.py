"""Find out why a switch accepts the login but then says "not logged in".

Read-only: logs in once, tries a few request variants against const_info,
prints only result codes, header and cookie *names* (never values), logs out.

Usage: .venv-test/bin/python -I scripts/session_debug.py 192.168.1.179
"""

import base64
import getpass
import json
import pathlib
import random
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests


def load_login() -> tuple[str, str]:
    """Read the login from .env in the repo root, or ask for it.

    .env holds ARAKNIS_USERNAME and ARAKNIS_PASSWORD. It is gitignored.
    """
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


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


def login(
    s: requests.Session, base: str, user: str, pw: str, *, like_browser: bool
) -> bool:
    """Log in, optionally doing the extra steps the browser's login page does."""
    if like_browser:
        s.headers["User-Agent"] = BROWSER_UA
        s.get(f"{base}/login.html", timeout=10)
        s.get(f"{base}/cgi-bin/datajson.cgi?login", timeout=10)
    key = serialization.load_pem_public_key(
        s.get(f"{base}/conf/key", timeout=10).text.strip().encode()
    )

    def enc(v: str) -> str:
        return base64.b64encode(key.encrypt(v.encode(), padding.PKCS1v15())).decode()

    r = s.post(
        f"{base}/cgi-bin/datajson.cgi?login",
        data={"etvapqp": enc(user), "rctater": enc(pw)},
        timeout=20,
    )
    reply = json.loads(r.text)
    print("login result:", reply.get("result"), "| msg:", repr(reply.get("msg")))
    print("login reply keys:", sorted(reply))
    print("login response header names:", sorted(r.headers))
    print("cookies set (names):", [c.name for c in s.cookies])
    return reply.get("result") == 0


def checks(s: requests.Session, base: str) -> bool:
    """Try request variants; return True if any worked."""
    worked = False

    def check(label: str, url: str, **kw) -> None:
        nonlocal worked
        resp = s.get(url, timeout=20, **kw)
        try:
            res = json.loads(resp.text).get("result")
        except ValueError:
            res = f"non-JSON, HTTP {resp.status_code}"
        worked |= res == 0
        print(f"  {label:<36} result={res}")

    cgi = f"{base}/cgi-bin/datajson.cgi?const_info"
    check("plain", cgi)
    check("with random suffix", f"{cgi}&{random.random()}")
    check("with Referer index.html", cgi, headers={"Referer": f"{base}/index.html"})
    check("with X-Requested-With", cgi, headers={"X-Requested-With": "XMLHttpRequest"})
    idx = s.get(f"{base}/index.html", timeout=20)
    print(
        f"  {'GET /index.html':<36} HTTP {idx.status_code}, cookies: {[c.name for c in s.cookies]}"
    )
    check("plain, after index.html", cgi)
    check("random suffix, after index.html", f"{cgi}&{random.random()}")
    return worked


def main() -> None:
    """Run the checks: first like the integration, then like a browser."""
    host = sys.argv[1]
    user, pw = load_login()
    base = f"http://{host}"
    for like_browser in (False, True):
        print(
            "\n== logging in",
            "like a browser (UA, login.html, GET ?login)"
            if like_browser
            else "like the integration",
        )
        s = requests.Session()
        try:
            if not login(s, base, user, pw, like_browser=like_browser):
                print("Login rejected; stopping (no retry, to avoid a lockout).")
                return
            if checks(s, base):
                print("  -> something worked")
                break
        finally:
            s.get(f"{base}/cgi-bin/datajson.cgi?logout", timeout=5)
            print("  logged out")


if __name__ == "__main__":
    main()

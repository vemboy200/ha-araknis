"""Read-only probe of an Araknis switch's web JSON API.

Logs in once, reads the PoE/port endpoints, logs out. Changes nothing on the switch.
Asks for the username and password at the prompt; they are never printed or saved.
Writes the raw responses to araknis_probe_<host>.json next to this script.

Usage: python3 araknis_probe.py 192.168.1.245 [more hosts...]
All hosts are read with the same login.
"""

import base64
import getpass
import json
import pathlib
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests

READ_ENDPOINTS = (
    "poe_port",
    "port_cfg",
    "poe_global",
    "const_info",
    "sys_summary",
    "sys_info",
    "sys_timestatus",
    "lldp_remote",
    "dyn_mac",
)


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


def probe(host: str, user: str, pw: str) -> None:
    """Log in to one switch, save its raw replies, log out."""
    base = f"http://{host}"
    s = requests.Session()

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
    login = json.loads(r.text)
    print(f"{host}: login result", login.get("result"), login.get("msg", ""))
    if login.get("result") != 0:
        print(f"{host}: login failed; stopping (no retry, to avoid a lockout).")
        return

    out = {}
    try:
        for ep in READ_ENDPOINTS:
            resp = s.get(f"{base}/cgi-bin/datajson.cgi?{ep}", timeout=20)
            try:
                out[ep] = json.loads(resp.text)
            except ValueError:
                out[ep] = {"_raw_text": resp.text[:2000]}
            print(f"{host}: {ep} result={out[ep].get('result')}")
        # The MAC table comes in pages, the way the switch's web page reads it.
        pages, index = [], 0
        while len(pages) < 100:
            resp = s.get(f"{base}/cgi-bin/datajson.cgi?dyn_mac={index}-50", timeout=20)
            page = json.loads(resp.text)
            pages.append(page)
            nxt = page.get("dyn_mac", {}).get("next", -1)
            print(
                f"{host}: dyn_mac={index}-50 rows={len(page.get('dyn_mac', {}).get('mac', []))} next={nxt}"
            )
            if nxt == -1 or nxt <= index:
                break
            index = nxt
        out["dyn_mac_pages"] = pages
    finally:
        s.get(f"{base}/cgi-bin/datajson.cgi?logout", timeout=5)
        print(f"{host}: logged out")

    path = pathlib.Path(__file__).with_name(f"araknis_probe_{host}.json")  # gitignored
    path.write_text(json.dumps(out, indent=2))
    print(f"{host}: saved", path.name)


def main() -> None:
    """Run the probe on each host given."""
    hosts = sys.argv[1:] or [input("Switch IP: ").strip()]
    user, pw = load_login()
    for host in hosts:
        try:
            probe(host, user, pw)
        except requests.RequestException as err:
            print(f"{host}: could not connect: {err}")


if __name__ == "__main__":
    main()

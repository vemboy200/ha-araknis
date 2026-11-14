"""Read-only probe of an Araknis AN-110 web-smart switch's web pages.

The AN-110 asks for its login with HTTP Basic authentication (realm "Switch")
on every page. This script logs in that way and saves the pages its web
interface is built from, so the integration can be written against them.

Its pages are CGIs (info.cgi, menu.cgi...). To stay read-only it only fetches
pages and files (.cgi, .htm, .html, .js, .css, .json, .txt) that the pages link
to, never anything with a query string. Pages whose names suggest an action (restart, reset, upgrade and so on)
are listed but not fetched, in case the switch acts on a plain page load.
Run it again with --include NAME to fetch one of those once it's clear it's a
plain form.

The login comes from .env in the repo root (AN110_USERNAME and AN110_PASSWORD,
falling back to ARAKNIS_USERNAME and ARAKNIS_PASSWORD), or is asked for. It is never printed or saved.

Writes the pages to an110_probe_<host>/ next to this script, plus index.json
listing what was fetched, skipped and refused.

Usage: python3 scripts/an110_probe.py 192.168.1.165 [--include PAGE ...]
"""

import argparse
import getpass
import json
import pathlib
import re
from urllib.parse import urljoin, urlparse

import requests

STATIC = re.compile(r"\.(?:html?|js|css|json|txt|cgi)$", re.IGNORECASE)
ACTION_WORDS = re.compile(
    r"reboot|restart|reset|rst|boot|loader|default|factory|upgrade|firmware|"
    r"fw|update|apply|save|"
    r"logout|erase|format|delete",
    re.IGNORECASE,
)
# href, src, action, location, window.open and plain quoted page names.
LINKS = re.compile(
    r"""(?:href|src|action)\s*=\s*["']([^"'#]+)["']"""
    r"""|["']([\w./-]+\.(?:html?|js|css|json|txt|cgi|asp|cfm)(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)
MAX_PAGES = 200


def load_login() -> tuple[str, str]:
    """Read the login from .env in the repo root, or ask for it."""
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    values: dict[str, str] = {}
    if env.is_file():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    # Its own login if set, otherwise the same one as the other Araknis gear.
    user = (
        values.get("AN110_USERNAME")
        or values.get("ARAKNIS_USERNAME")
        or input("Username: ")
    )
    pw = (
        values.get("AN110_PASSWORD")
        or values.get("ARAKNIS_PASSWORD")
        or getpass.getpass("Password (hidden): ")
    )
    if values.get("AN110_PASSWORD") or values.get("ARAKNIS_PASSWORD"):
        print("Using the login from .env")
    return user, pw


def main() -> None:
    """Fetch the switch's pages."""
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--include", nargs="*", default=[])
    args = parser.parse_args()
    base = f"http://{args.host}/"
    out = pathlib.Path(__file__).resolve().parent / f"an110_probe_{args.host}"
    out.mkdir(exist_ok=True)
    include = {name.lower() for name in args.include}

    session = requests.Session()
    session.auth = load_login()

    first = session.get(base, timeout=10)
    if first.status_code == 401:
        print("The switch rejected the login. Stopping, so it can't lock you out.")
        return

    queue = ["/"]
    seen: set[str] = set()
    fetched: dict[str, dict[str, object]] = {}
    skipped_actions: set[str] = set()
    other_links: set[str] = set()

    while queue and len(fetched) < MAX_PAGES:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        resp = first if path == "/" else session.get(urljoin(base, path), timeout=10)
        if resp.status_code == 401:
            print("Login rejected partway through. Stopping.")
            break
        name = "index_root.html" if path == "/" else path.strip("/").replace("/", "__")
        (out / name).write_bytes(resp.content)
        fetched[path] = {
            "status": resp.status_code,
            "bytes": len(resp.content),
            "type": resp.headers.get("Content-Type"),
            "saved_as": name,
        }
        print(f"{resp.status_code} {path} ({len(resp.content)} bytes)")
        text = resp.text
        for match in LINKS.finditer(text):
            link = (match[1] or match[2]).strip()
            url = urlparse(urljoin(urljoin(base, path), link))
            if url.netloc and url.netloc != urlparse(base).netloc:
                continue
            target = url.path + (f"?{url.query}" if url.query else "")
            leaf = url.path.rsplit("/", 1)[-1].lower()
            if url.query or not STATIC.search(url.path):
                other_links.add(target)
            elif ACTION_WORDS.search(leaf) and leaf not in include:
                skipped_actions.add(target)
            elif url.path not in seen:
                queue.append(url.path)

    (out / "index.json").write_text(
        json.dumps(
            {
                "fetched": fetched,
                "not_fetched_action_pages": sorted(skipped_actions),
                "not_fetched_query_or_other": sorted(other_links),
            },
            indent=2,
        )
    )
    print(f"\nSaved {len(fetched)} pages to {out}")
    print(f"Action pages not fetched: {sorted(skipped_actions)}")
    print(f"Links with a query string not fetched: {len(other_links)}")


if __name__ == "__main__":
    main()

"""Check that PoE on/off and power cycle work on a real switch, using one empty port.

Uses the integration's own client (custom_components/araknis/api.py).
Asks for the login at the prompt; it is never printed or saved.

Safety: it refuses to touch a port that has a network link or is delivering PoE,
so nothing plugged in can lose power. Pick a port with nothing connected.

Usage: .venv-test/bin/python scripts/poe_write_test.py 192.168.1.245 21
"""

import asyncio
import getpass
import importlib.util
import pathlib
import sys

import aiohttp

API = pathlib.Path(__file__).parents[1] / "custom_components" / "araknis" / "api.py"
spec = importlib.util.spec_from_file_location("araknis_api", API)
api = importlib.util.module_from_spec(spec)
sys.modules["araknis_api"] = api
spec.loader.exec_module(api)


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


def show(data, port: int, step: str) -> None:
    """Print one port's PoE state."""
    p = data.ports[port]
    print(
        f"{step:<22} PoE enabled={p.poe_enabled}  status={p.poe_status}  link_up={p.link_up}"
    )


async def run(host: str, port: int, user: str, pw: str) -> None:
    """Run the test."""
    async with aiohttp.ClientSession() as session:
        client = api.AraknisClient(session, host, user, pw)
        try:
            data = await client.get_data()
            if port not in data.ports:
                print(f"Port {port} has no PoE on this switch.")
                return
            p = data.ports[port]
            if p.link_up or p.poe_status in ("delivering", "requesting") or p.power:
                print(f"Port {port} has something connected; refusing to test on it.")
                return
            if not p.poe_enabled:
                print(
                    f"PoE is already off on port {port}; turn it on in the web UI first."
                )
                return
            show(data, port, "before")

            await client.set_poe(port, False)
            await asyncio.sleep(3)
            show(await client.get_data(), port, "after PoE off")

            await client.set_poe(port, True)
            await asyncio.sleep(3)
            show(await client.get_data(), port, "after PoE on")

            await client.power_cycle(port)
            await asyncio.sleep(5)
            show(await client.get_data(), port, "after power cycle")
            print("All commands were accepted.")
        except api.AraknisError as err:
            print(f"Failed: {err}")
        finally:
            await client.logout()
            print("logged out")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], int(sys.argv[2]), *load_login()))

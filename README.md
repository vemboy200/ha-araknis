# Araknis Networks for Home Assistant

A custom integration for Araknis Networks equipment. It monitors and controls PoE on Araknis 210-series managed switches, and monitors Araknis 310-series routers, over their local web interfaces.

> [!WARNING]
> Early work in progress, tested on AN-210-SW-8/16/24-POE switches and an AN-310-RT-4L2W router: reading data and turning PoE off and on work. Power cycling is accepted by the switch but has only been tried on an empty port, so its effect on a powered device hasn't been seen yet.

## What you get

One device per switch, with:

- **PoE power** and **PoE budget** for the whole switch
- For each PoE port:
  - a **PoE** switch to turn power on or off
  - a **PoE power cycle** button
  - **PoE power** (W), plus **PoE voltage** (V) and **PoE current** (mA), which are disabled by default
  - **PoE status** (disabled, detecting, delivering, fault…) and **link speed** as diagnostic sensors

One device per router, with:

- **CPU usage**, **memory usage**, **DHCP clients** and **last restart**
- **Cloud connection** (OvrC) as a diagnostic sensor
- One child device per WAN port, with a **connected** sensor, **link speed** and the WAN **IP address** (disabled by default)

Switches and routers are polled every 30 seconds.

## Installation

With [HACS](https://hacs.xyz/):

1. In HACS, open the **⋮** menu and choose **Custom repositories**.
2. Add `https://github.com/vemboy200/ha-araknis` with the type **Integration**.
3. Search for **Araknis Networks**, download it, and restart Home Assistant.

Manually: copy `custom_components/araknis` into your Home Assistant `config/custom_components/` folder and restart Home Assistant.

## Setup

Home Assistant finds switches (over SSDP) and routers (over mDNS) on its own and offers to add them: you only enter the login. Otherwise, add the integration from **Settings → Devices & services** and enter the device's IP address and the username and password for its web interface. The integration works out whether it's a switch or a router. Add each device separately. A static IP or DHCP reservation for each switch is recommended.

## Removal

1. Go to **Settings → Devices & services**, open **Araknis Networks**, and delete each switch or router from its **⋮** menu.
2. If you installed it with HACS, remove the integration in HACS, then restart Home Assistant. If you installed it manually, delete `config/custom_components/araknis` and restart.

Nothing is changed on the switches or router when you remove the integration.

## How it works and caveats

- Araknis publishes no API for these switches. The integration uses the same JSON interface the switch's own web page uses (an RSA-encrypted login, then `/cgi-bin/datajson.cgi`). A firmware update could change it.
- The switch ties a login session to the client's IP address.
- Repeated failed logins can lock the switch account. After a rejected login the integration waits 5 minutes before trying again, and asks you to re-enter the login.
- Turning PoE off or power cycling a port cuts power to whatever is plugged into it (access points, cameras, network coordinators…).
- Port names are read when the integration is set up. Reload the integration after renaming a port on the switch.
- SFP ports and link aggregation groups have no PoE and get no entities.
- Routers use a different login: the password is encrypted with a key built into the router's login page, and the session is a cookie. **Five failed logins lock the router's login for 30 minutes**, so double-check the password.
- Web-smart switches (such as the non-PoE 16-port models that ask for a login in a browser pop-up) use a different interface and aren't supported.

## Credits

The switch's web API was worked out by Louis Apostolakis in [lovintexas/Araknis_Switch](https://github.com/lovintexas/Araknis_Switch), a plugin for Universal Devices eisy/IoX (MIT License).

## Logo

The icon and logo in `custom_components/araknis/brand/` are taken from Araknis Networks' own logo. Araknis is a trademark of its owner, and this project isn't affiliated with or endorsed by them.

## Development

```bash
uv venv .venv-test --python 3.14
VIRTUAL_ENV=.venv-test uv pip install "pytest-homeassistant==2026.11.1" pytest-cov
.venv-test/bin/pytest
```

`scripts/araknis_probe.py` dumps a switch's raw PoE replies and `scripts/router_probe.py` a router's status replies (both read-only). `scripts/poe_write_test.py` checks PoE commands on one empty port. They read the login from `.env` (see `.env.example`) or ask for it.

This integration was written with help from AI (Claude).

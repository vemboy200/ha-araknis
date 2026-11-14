# Araknis Networks for Home Assistant

A custom integration for Araknis Networks equipment. It monitors and controls PoE on Araknis 210-series managed switches, monitors Araknis 310-series routers, and covers the little that AN-110 web-smart switches offer, over their local web interfaces.

> [!WARNING]
> Early work in progress, tested on AN-210-SW-8/16/24-POE switches and an AN-310-RT-4L2W router: reading data and turning PoE off and on work. Power cycling is accepted by the switch but has only been tried on an empty port, so its effect on a powered device hasn't been seen yet. The **Restart** buttons (switches, AN-110 and router) send the same request as the devices' own web pages, but haven't been tried on real hardware.

## What you get

One device per switch, with:

- **PoE power** and **PoE budget** for the whole switch
- A **Restart** button
- For each PoE port:
  - a **PoE** switch to turn power on or off
  - a **PoE power cycle** button
  - **PoE power** (W), plus **PoE voltage** (V) and **PoE current** (mA), which are disabled by default
  - **PoE status** (disabled, detecting, delivering, fault…), **link speed** and **connected device MAC** as diagnostic sensors. The MAC address comes from the switch's MAC address table and belongs to the device plugged into the port, not the port itself. It's unknown when the port has no link or carries several devices, such as an uplink to another switch or an access point.

One device per router, with:

- **CPU usage**, **memory usage**, **DHCP clients** and **last restart**
- **Cloud connection** (OvrC) as a diagnostic sensor
- A **Restart** button
- One child device per WAN port, with a **connected** sensor, **link speed** and the WAN **IP address** (disabled by default)

One device per AN-110 web-smart switch, with:

- **Last restart** and **Cloud connection** (OvrC) as diagnostic sensors
- A **Restart** button
- An **OvrC cloud service** switch, the same setting as the switch's OvrC Setting page. Turning it off drops the OvrC connection within a poll; turning it back on reconnects in about a minute.

That's all its web interface has: it has no PoE, port status or MAC address table.

All devices are polled every 30 seconds.

## Supported devices

Tested:

| Model | Type |
|---|---|
| AN-210-SW-8-POE | PoE switch |
| AN-210-SW-16-POE | PoE switch |
| AN-210-SW-24-POE | PoE switch |
| AN-310-RT-4L2W | Router |
| AN-110-SW-16 | Web-smart switch (firmware 1.1.21) |

Likely to work, but untested:

- Other 210 and 310-series PoE switches. The switch web API was first worked out on an AN-310-SW-24-POE in [lovintexas/Araknis_Switch](https://github.com/lovintexas/Araknis_Switch). Only the three switches above are found automatically; add others by IP address.
- AN-110-RT routers: the 310 router's web page has code for the AN-110-RT-2L1W and AN-110-RT-2L1W-WIFI, so they appear to share its interface.

Not supported:

- Araknis access points.

### Araknis access points: consider OpenWrt

The AN-500-AP-I-AC and AN-700-AP-I-AC (and the AN-300-AP-I-N) can be flashed with [OpenWrt](https://openwrt.org/), and doing so is strongly recommended.

The author audited four AN-700-AP-I-AC units on their stock firmware and found that, over SNMP and without any login, anyone on the LAN could read:

- the Wi-Fi password (WPA passphrase) for both bands, in cleartext
- the SNMP read, write and trap community strings
- the SNMPv3 credentials, still at their factory default

Every unit also had the same SSH host key, built into the firmware. The stock firmware can't be patched to fix this, so replacing it is the only real fix. The AN-500 and AN-300 weren't tested, but Araknis publishes [one set of firmware release notes](https://www.control4.com/docs/product/an-x00-ap/release-notes/english/latest) for all its AN-x00 access points.

Check the OpenWrt page for your exact model before flashing, back up the stock firmware and calibration data first, and keep in mind that flashing replaces the Araknis firmware and its OvrC cloud management.

The switches and routers can't be flashed. If you use SNMP on them, change their community strings from the defaults. This integration doesn't use SNMP.

## Use cases

- **Power monitoring:** see how much power each PoE device draws (access points, cameras, PoE-powered shades and so on) and how much of the switch's PoE budget is in use. The power sensors can feed an [Integral helper](https://www.home-assistant.io/integrations/integration/) to track energy over time, or alert you when a device suddenly draws nothing because it has crashed or been unplugged.
- **Cutting power to devices:** turn PoE off for devices you don't need all the time, for example indoor cameras while people are home, or a lab device overnight. Power cycle a frozen camera or access point from Home Assistant instead of walking to the rack, or automatically when it drops off the network.
- **WAN monitoring:** get notified when your internet connection drops (the WAN port loses its link or IP address) and when it comes back, or when the link speed falls below what it should be. With two WAN ports, see which one is up.
- **Router health:** watch the router's CPU and memory use over time, and spot unexpected restarts with the **Last restart** sensor. The **DHCP clients** count shows when the number of devices on your network changes, which can also warn you before the DHCP pool runs out.

## Installation

Requires Home Assistant 2026.11 or newer.

### Through the Marketplace

1. In Home Assistant, go to **Settings** > **Marketplace** and select **Add from link**.
2. Enter `vemboy200/ha-araknis` and add it.
3. Install **Araknis Networks** and restart Home Assistant.

### Manual installation

Copy `custom_components/araknis` into your Home Assistant `config/custom_components/` folder and restart Home Assistant.

## Setup

Home Assistant finds PoE switches (over SSDP) and routers (over mDNS) on its own and offers to add them: you only enter the login. Web-smart switches aren't found automatically. Otherwise, add the integration from **Settings → Devices & services** and enter the device's IP address and the username and password for its web interface. The integration works out whether it's a switch, a web-smart switch or a router. Add each device separately. A static IP or DHCP reservation for each switch is recommended.

## Removal

1. Go to **Settings → Devices & services**, open **Araknis Networks**, and delete each switch or router from its **⋮** menu.
2. If you installed it through the Marketplace, uninstall **Araknis Networks** there, then restart Home Assistant. If you installed it manually, delete `config/custom_components/araknis` and restart.

Nothing is changed on the switches or router when you remove the integration.

## How it works and caveats

- Araknis publishes no API for these switches. The integration uses the same JSON interface the switch's own web page uses (an RSA-encrypted login, then `/cgi-bin/datajson.cgi`). A firmware update could change it.
- The switch ties a login session to the client's IP address.
- Repeated failed logins can lock the switch account. After a rejected login the integration waits 5 minutes before trying again, and asks you to re-enter the login.
- Turning PoE off or power cycling a port cuts power to whatever is plugged into it (access points, cameras, network coordinators…).
- Restarting a switch cuts off everything connected through it until it's back up (whether PoE power stays on during the restart hasn't been tested). Restarting the router cuts off the internet for the whole network. The integration's entities are unavailable until the device answers again.
- Port names are read when the integration is set up. Reload the integration after renaming a port on the switch.
- SFP ports and link aggregation groups have no PoE and get no entities.
- Routers use a different login: the password is encrypted with a key built into the router's login page, and the session is a cookie. **Five failed logins lock the router's login for 30 minutes**, so double-check the password.
- AN-110 web-smart switches ask for their login in a browser pop-up (HTTP Basic authentication), which sends it unencrypted with every request. The same goes for their own web page.
- There are no device trackers for the router's clients. The router lists every client with an "online" flag, but on a real AN-310-RT-4L2W that flag said offline for many Wi-Fi devices that were connected and working, such as phones, ESPHome devices and kitchen appliances. Trackers built on it would show those devices as away, so they were left out. The router has no other list of connected devices: which Wi-Fi devices are connected is known by the access points, not the router.

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

# catlink-local

Run a CATLINK Open X self-cleaning litter box with **no vendor cloud**: on your LAN, with Home Assistant
entities. Two small stdlib-only Python services on a Linux host (Raspberry Pi class) stand in for the
vendor's login server and MQTT broker, decode what the box sends, and publish it to your own Mosquitto
with Home Assistant MQTT discovery.

```
            DNS (Pi-hole / dnsmasq): the box's four vendor names -> your host
                                         |
 box --HTTPS :443--> catlink-authresp    |   "your broker is <host-lan-ip>:1883"
 box --MQTT  :1883-> catlink-gateway  ---+--> your Mosquitto --> Home Assistant (MQTT discovery)
                     (box-facing broker         state, diagnostics, availability
                      + decoder + HA bridge) <-- commands (only if CATLINK_COMMANDS=1)
```

## Why this exists

I bought this litter box, and it turned out that its *automatic* cleaning isn't automatic. In my
testing, after each visit the box reports to the vendor cloud and doesn't clean on its own until a
"clean now" command arrives. I didn't like a device I own needing the vendor's server
to do its basic job, or the data it sends there, so I had it run locally instead. Here the "clean after
a visit" logic is an ordinary Home Assistant automation
([examples/homeassistant/automation_auto_clean.yaml](examples/homeassistant/automation_auto_clean.yaml)).

**This was vibecoded.** I didn't write it by hand. I directed an AI coding assistant (Claude, via Claude
Code), which did the protocol reverse engineering, wrote the code, tests and docs, and checked them, while
I ran experiments on the real box and decided what to build. Read it with that in mind. It is tested,
but it is not hand-audited code, so review it before you trust it with your cat.

## Status

**Alpha.** Built and tested against **one box on one firmware**: Open X (hasscc `deviceType` C08),
firmware 04.22, in a single home, for days rather than months. Every protocol field in [docs/PROTOCOL.md](docs/PROTOCOL.md)
carries a confidence tag (confirmed, observed, hypothesis or unverified), and many are not yet confirmed. Other boxes or firmware may differ.

What works:
- heartbeat decode: state, occupancy, live scale, mode, litter type, settings, box clock, Wi-Fi RSSI
- visits: count, duration, weight (box value and a settled estimate), deposit estimate
- waste bin full, waste drawer out
- commands (off by default): clean, level, pause, resume, cancel; mode (automatic/manual), litter type,
  clean delay, burial delay, light, sound, continuous cleaning, soft-stool burial, child lock, kitty mode
- optional weight-based labelling of visits for one or two cats (off by default)

What does not:
- **scheduled** mode cannot be set (it needs a slot table the gateway will not invent); night mode is
  read-only
- the box clock is never set; it was hours off on the tested box, so the box's own schedule and night
  hours run on the wrong time
- child lock and kitty mode have no readback (switches are optimistic)
- deodorant level, litter low and error/anti-pinch are not mapped
- the box does not clean after a visit on its own once it is off the cloud; an example HA automation
  does it instead ([examples/homeassistant/automation_auto_clean.yaml](examples/homeassistant/automation_auto_clean.yaml))
- the vendor's cloud features ("AI", app history) are gone, and **the vendor app cannot reach the box**

## Risks: read before you start

- **Lock it down completely.** For full protection, do both of these at your router: **give the box a
  fixed IP** (a DHCP reservation for its MAC), and **block its internet** (a MAC rule, permanently). The
  fixed IP keeps your DNS and firewall rules pointing at the right device. The block means the box can
  never reach the vendor again, even if DNS is bypassed, so it gets no firmware update that could close
  this method, no remote commands, and sends no data out. Everything it needs is on your LAN.
- **Terms and warranty.** The vendor's user agreement forbids reverse engineering and third-party
  interference. Using this may affect your account and your warranty. That is your call.
- **Firmware updates can end this.** The method works only because firmware 04.22 does **not validate**
  the TLS certificate of the login server. An update may close that. **Block the box's internet at the
  router (a MAC rule) before you begin**, so no update can land while you set this up, and keep it blocked.
  [docs/SETUP.md](docs/SETUP.md) gives the order of operations.
- **No cloud fallback.** Once redirected, the box depends entirely on your host. If the services are
  down, the box reports nothing and keeps retrying (a box whose login was blocked was reported to re-join
  Wi-Fi about every 40 s).
- **Plaintext, unauthenticated box link.** The box speaks plaintext MQTT, and the gateway accepts any
  credentials the box presents (it cannot check them). Scope TCP 1883 and 443 to the box's MAC with a
  firewall; the gateway and the responder also pin the peer by MAC.
- **Moving parts near cats.** Commands are **off by default** (`CATLINK_COMMANDS=0`). When on, the gateway
  refuses to start a clean or level unless the latest heartbeat is fresh, idle, unoccupied, untouched
  and the scale reads near empty. That interlock only refuses an unsafe **start**. It cannot see a cat
  arriving after the last heartbeat, and the decoded occupancy flags can lag the scale. Pause and cancel
  go through Home Assistant only; the vendor app is not a backup. **Cat safety comes from the box's own
  sensors.** Do not start a cycle you cannot supervise.

## Quickstart

Each step is detailed, with a check, in [docs/SETUP.md](docs/SETUP.md).

0. Identify the box (SETUP step 1): its MAC, its IP and the host names it looks up. Read its firmware version in the
   vendor app now, while the box is still online; the app cannot reach it afterwards.
1. At the router: give the box a fixed IP (DHCP reservation) and block its internet by MAC. Keep both.
2. Point the box's four vendor host names at your host in your DNS server ([examples/dns](examples/dns)).
3. Allow TCP 1883 and 443 to your host from the box's MAC only ([examples/firewall](examples/firewall)).
4. `git clone https://github.com/yorgossot/catlink-local catlink-local && cd catlink-local && sudo ./install.sh`
5. Generate your own TLS key and certificate into `/etc/catlink-local/` (none is shipped).
6. Create a `catlink` Mosquitto user, its password file and ACL ([examples/mosquitto](examples/mosquitto)).
7. Edit `/etc/catlink-local/catlink-local.env`: at least `CATLINK_BOX_MAC` and `CATLINK_BOX_BIND_IP`.
8. `sudo systemctl enable --now catlink-authresp catlink-gateway`
9. `journalctl -u catlink-gateway -f -o cat`: expect `connect`, `subscribe`, `publish`, `ha_connected`.
10. Home Assistant shows the device "CATLINK Open X" under MQTT. Enable commands later, deliberately.

## Documentation

- [docs/SETUP.md](docs/SETUP.md): install, verify, operate, roll back
- [docs/ENTITIES.md](docs/ENTITIES.md): every entity, topic and JSON key
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the box's protocol as observed, with confidence levels
- [docs/DESIGN.md](docs/DESIGN.md): architecture and safety model
- [examples/](examples): Home Assistant automations and dashboards, DNS, firewall, Mosquitto ACL
- [CONTRIBUTING.md](CONTRIBUTING.md): mapping a new byte without leaking your device's identifiers

## Acknowledgements

- [gillohner/catlink-controller](https://github.com/gillohner/catlink-controller) (MIT) was the reference
  for the frame envelope and the XOR checksum. No code was copied from it.
- [hasscc/catlink](https://github.com/hasscc/catlink) for the device model naming (C08).

Not affiliated with or endorsed by CATLINK or Alibaba Cloud.

## Licence

MIT, see [LICENSE](LICENSE).

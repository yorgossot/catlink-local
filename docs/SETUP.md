# Setup

Follow the steps in order; each ends with a check. Read the risks in the [README](../README.md) first.

Placeholders, in angle brackets, are yours to fill in:

| Placeholder | Meaning |
|---|---|
| `<box-mac>` | the litter box's Wi-Fi MAC, lower case, e.g. `02:ca:7b:0c:00:01` |
| `<box-ip>` | the box's LAN address |
| `<host-lan-ip>` | the LAN address of the Linux host that runs catlink-local |
| `<dns-ip>` | the DNS server the box uses (Pi-hole or dnsmasq) |
| `<instance>` | the instance prefix in your box's MQTT host names (step 1) |

## 0. Requirements

- A Linux host on the same LAN as the box (Raspberry Pi OS or Debian), reachable from it on TCP 1883 and
  443, with a fixed address.
- Python ≥ 3.9 and systemd ≥ 247 (for `LoadCredential=`): `python3 --version; systemctl --version`.
- `openssl`.
- Mosquitto with authentication (`allow_anonymous false`, a password file and an ACL file), and Home
  Assistant with the MQTT integration connected to it.
- A DNS server the box actually uses (Pi-hole v6 or dnsmasq), with a query log.
- A router that can block one device's internet access by MAC.
- **Port 443 free** on the address the responder will use. Pi-hole v6's own web server listens on 443 by
  default; see step 4.
- **Port 1883 free on `<host-lan-ip>`** (the address the gateway binds, `CATLINK_BOX_BIND_IP`). Check:
  ```sh
  sudo ss -ltnp 'sport = :1883'
  ```
  If your own Mosquitto already listens there (for example on `0.0.0.0:1883`), either bind Mosquitto to
  `127.0.0.1` or another address (a `listener 1883 127.0.0.1` line in `mosquitto.conf`; Home Assistant
  and the gateway then reach it on that address), or give the host a second LAN address and use that one
  for `CATLINK_BOX_BIND_IP`. The box is told which port to use, but a box-facing port other than 1883 has
  not been tested with the box.

## 1. Identify the box

1. Find the box's MAC and IP in your router's DHCP client list. Give it a DHCP reservation if you can.
2. Check the firmware version in the vendor app. This project was built on **04.22**; on anything else
   expect differences (the gateway refuses heartbeat layouts it does not know).
3. Read the four host names it looks up from your DNS query log. Power-cycle the box first if the log is
   empty. For Pi-hole:
   ```sh
   grep 'from <box-ip>$' /var/log/pihole/pihole.log | grep -o 'query\[A\] [^ ]*' | sort | uniq -c
   ```
   (For Pi-hole in Docker, run it inside the container: `docker exec <container> grep ...`.) For dnsmasq
   with `log-queries`, use the same `grep` on its log or on `journalctl -u dnsmasq`.

   Expect two vendor-wide auth names and two instance-specific MQTT names:
   ```
   iot-auth-global.aliyuncs.com
   iot-auth-global-x.aliyuncs.com
   <instance>.mqtt.iothub.aliyuncs.com
   <instance>-x.mqtt.iothub.aliyuncs.com
   ```
   You may see only some of them (the tested box used only the `-x` names). Redirect all four anyway.
   `<instance>` identifies your vendor account or region: do not post it publicly.

**Check:** you have `<box-mac>`, `<box-ip>` and `<instance>`.

## 2. Block the box's internet first

Create a router rule that blocks internet access for `<box-mac>`, and turn it on **now**, before anything
else.

Why first: the whole method depends on firmware 04.22 not validating the login server's certificate. A
firmware update could close that, and the box can fetch one whenever it reaches the vendor. With the WAN
blocked, no update can arrive while you work, or later. Keep the rule on permanently.

From now until step 9 the box cannot log in. It will keep retrying and appear offline in the app. That
is expected.

**Check:** the vendor app shows the box offline within a few minutes.

## 3. Redirect DNS

Point all four names at `<host-lan-ip>`. If you will run the responder on a different address (step 4),
point the two `iot-auth-global*` names at that address instead.

**Pi-hole v6.** Copy [examples/dns/pihole-hosts.example](../examples/dns/pihole-hosts.example) to
`/etc/pihole/hosts/catlink-local` (for Docker, the host directory mounted at `/etc/pihole/hosts`) and
fill in the placeholders. Pi-hole reads files in that directory as soon as they change; no restart. But
a name already in its cache keeps its old answer until it expires, and the box has just looked these
names up (step 1), so flush once:
```sh
sudo pihole restartdns                        # Docker: docker exec <container> pihole restartdns
```

**dnsmasq.** Copy [examples/dns/dnsmasq.conf.example](../examples/dns/dnsmasq.conf.example) to
`/etc/dnsmasq.d/catlink-local.conf`, fill in the placeholders, then `sudo systemctl restart dnsmasq`.

Side effect of the hosts-file method: the resolver creates empty parent names, so a name such as
`mqtt.iothub.aliyuncs.com` changes from NXDOMAIN to an empty NOERROR answer while the file exists.
Harmless.

The box must use this DNS server, normally because your router's DHCP hands it out. The tested box used
the DHCP-provided server; with its WAN blocked, a box using a hard-coded public resolver could not
resolve anything and would stay offline.

**Check:** each name answers with your host:
```sh
for n in iot-auth-global.aliyuncs.com iot-auth-global-x.aliyuncs.com \
         <instance>.mqtt.iothub.aliyuncs.com <instance>-x.mqtt.iothub.aliyuncs.com; do
  echo "$n -> $(dig @<dns-ip> "$n" +short)"
done
```

## 4. Firewall on the host

The box speaks plaintext MQTT and the gateway accepts any credentials, so only the box may reach TCP 1883
and 443. Use [examples/firewall/nftables.nft.example](../examples/firewall/nftables.nft.example): fill in
`<host-lan-ip>` and `<box-mac>`, then
```sh
sudo nft -f nftables.nft.example
```
and include it in your persistent nftables configuration. The example also gives the iptables form; `ufw`
users add those lines to `/etc/ufw/before.rules`. If your host firewall drops by default, also add the
accept rule shown in the example.

**This rule drops every other device** that connects to `<host-lan-ip>` on 1883 or 443. Never run your
household Mosquitto (or anything else other devices use) on that address and port: give it `127.0.0.1`
or another address (step 0).

**Port 443 already taken** (Pi-hole v6's web server, a reverse proxy, ...): the box always connects to 443,
so either move the other service off 443 (for Pi-hole v6, change `webserver.port`), or give the host a
second LAN address, set `CATLINK_AUTH_BIND_IP` to it in step 8, and point the two auth names at it in
step 3.

**Check:** `sudo nft list table inet catlink_local` shows the rule. After step 9, its counter rises only if
another device tries those ports.

## 5. Install the code

```sh
git clone https://github.com/yorgossot/catlink-local catlink-local
cd catlink-local
sudo ./install.sh
```

`install.sh` is idempotent. It:
1. creates `/opt/catlink-local` and `/etc/catlink-local`;
2. copies `gateway.py`, `authresp.py` and `catlink_local/*.py` to `/opt/catlink-local/` (stale `.py` files
   there are removed first);
3. installs `catlink-gateway.service` and `catlink-authresp.service` to `/etc/systemd/system/` and runs
   `systemctl daemon-reload`;
4. copies `config/catlink-local.env.example` to `/etc/catlink-local/catlink-local.env` if that file does
   not exist yet;
5. prints the remaining steps. It does **not** generate certificates, create the Mosquitto user, touch DNS
   or the firewall, or enable or start anything.

**Check:** `ls /opt/catlink-local /etc/catlink-local` and `systemctl cat catlink-gateway`.

## 6. Generate your TLS certificate

No certificate or key is shipped; you generate your own. The box does not validate it, so the names in it
are cosmetic.
```sh
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/catlink-local/key.pem -out /etc/catlink-local/cert.pem \
  -subj "/CN=iot-auth-global-x.aliyuncs.com" \
  -addext "subjectAltName=DNS:iot-auth-global-x.aliyuncs.com,DNS:iot-auth-global.aliyuncs.com"
sudo chmod 600 /etc/catlink-local/key.pem
sudo chmod 644 /etc/catlink-local/cert.pem
```
systemd passes both files to the responder as credentials; the service itself cannot read
`/etc/catlink-local` directly.

**Check:** `sudo openssl x509 -in /etc/catlink-local/cert.pem -noout -subject -enddate`

## 7. Mosquitto user for the gateway

The gateway connects to your Mosquitto as user `catlink`, with its password in
`/etc/catlink-local/ha_mqtt.pass` (root-only; systemd hands it to the service as a credential, so it never
appears in the environment, the process list or the log).

Create the password and add its hash without putting the password on a command line. As root
(`sudo -s`), adjusting `PWFILE` to the `password_file` in your `mosquitto.conf`:
```sh
umask 077
PWFILE=/etc/mosquitto/passwd
openssl rand -base64 24 > /etc/catlink-local/ha_mqtt.pass
chmod 600 /etc/catlink-local/ha_mqtt.pass
cp -a "$PWFILE" "$PWFILE.bak"
printf 'catlink:%s\n' "$(cat /etc/catlink-local/ha_mqtt.pass)" > /etc/mosquitto/catlink.tmp
mosquitto_passwd -U /etc/mosquitto/catlink.tmp
{ grep -v '^catlink:' "$PWFILE"; cat /etc/mosquitto/catlink.tmp; } > "$PWFILE.new"
chown --reference="$PWFILE" "$PWFILE.new"
chmod --reference="$PWFILE" "$PWFILE.new"
mv "$PWFILE.new" "$PWFILE"
rm /etc/mosquitto/catlink.tmp
exit
```
(`mosquitto_passwd -b "$PWFILE" catlink <password>` is shorter, but puts the password on a command line,
visible to other users in the process list and possibly in shell history.)

Append the ACL block from [examples/mosquitto/acl.example](../examples/mosquitto/acl.example) to your
`acl_file`:
```
user catlink
topic readwrite catlink/litterbox/#
topic write homeassistant/+/catlink_litterbox/#
```
Home Assistant's own MQTT user must be able to read `catlink/litterbox/#` and `homeassistant/#` and to
write `catlink/litterbox/+/set`. Then reload (re-reads the password and ACL files without dropping
clients):
```sh
sudo systemctl reload mosquitto
```
For Mosquitto in Docker, run the same steps against the mounted config files and send the container a
`SIGHUP` (`docker kill -s HUP <container>`).

**Check:** the `catlink` user can log in (this puts the password on a command line for a moment; skip it
and rely on `ha_connected` in step 9 if that matters on your host):
```sh
mosquitto_sub -h 127.0.0.1 -u catlink -P "$(sudo cat /etc/catlink-local/ha_mqtt.pass)" \
  -t 'catlink/litterbox/#' -C 1 -W 3; echo "rc=$?"
```
`rc=27` (timeout, nothing retained yet) or `rc=0` means authentication works; "not authorised" means it
does not.

## 8. Configure

```sh
sudoedit /etc/catlink-local/catlink-local.env
```
Every variable is listed there with its default. The file holds no secret.

| Variable | Set to |
|---|---|
| `CATLINK_BOX_MAC` | `<box-mac>` (**required**) |
| `CATLINK_BOX_BIND_IP` | `<host-lan-ip>` (**required**; `0.0.0.0` is refused unless `CATLINK_ALLOW_ANY_BIND=1`) |
| `CATLINK_BOX_IP` | `<box-ip>` (recommended: then the peer must match IP **and** MAC; leave empty for MAC only) |
| `CATLINK_AUTH_BIND_IP` | only if the responder uses a second address (step 4) |
| `CATLINK_HA_HOST`, `CATLINK_HA_PORT` | your Mosquitto, default `127.0.0.1:1883`; a host name or an IP address both work, an IP avoids depending on DNS |
| `CATLINK_CATS` | optional, e.g. `cat_a=3.20,cat_b=4.60`: one or two `slug=kg`, lighter first (strictly lighter kg). Slugs (`[a-z][a-z0-9_]*`, unique, not `unknown`/`last_visit`/`last_deposit`) become entity ids; kg between 0.2 and 12. Default off |
| `CATLINK_COMMANDS` | leave `0` for now (step 10) |

The responder advertises `CATLINK_BOX_BIND_IP:CATLINK_BOX_BIND_PORT` as the MQTT broker unless you set
`CATLINK_ADVERTISED_MQTT_HOST` / `_PORT`.

The kg values in `CATLINK_CATS` are **starting seeds only**: they are used until
`/var/lib/catlink-local/cats.json` exists (it is written after the first visit or cat command), and the
saved references win from then on. Editing the kg later changes nothing while the slugs stay the same. To
re-seed, publish a weigh-in to `catlink/litterbox/set_ref_<slug>/set` (payload kg; see
[examples/homeassistant/cat_weigh_in.yaml](../examples/homeassistant/cat_weigh_in.yaml)), or stop the
gateway, delete `cats.json`, and start it again. Renaming or removing a slug makes the gateway ignore the old
file (`cats_state_mismatch` in the journal) and start fresh.

**Check:** a bad or missing value makes the service exit with status 2 and name the variable in the
journal (step 9).

## 9. Start

```sh
sudo systemctl enable --now catlink-authresp catlink-gateway
journalctl -u catlink-authresp -u catlink-gateway -f -o cat
```

Expected events, in order (each line is JSON with an `event` field):
1. authresp `start`, gateway `broker_start`;
2. gateway `ha_connected`, then `ha_discovery_published` with a `count`;
3. when the box next tries to log in: authresp `tcp_accept`, `tls_ok`, `request` with
   `path=/auth/bootstrap` and `matched=true`, `response`;
4. gateway `connect`, `subscribe` (two topics), then `publish` (heartbeats; every ~100 s when idle).

If the box does not appear within two minutes, power-cycle it. `foreign_peer` means something other than
`<box-mac>` / `<box-ip>` connected; `tls_fail` from the box means its firmware rejected the certificate,
and this method will not work on it.

**Check:** in Home Assistant, **Settings → Devices & services → MQTT** lists the device "CATLINK Open X"
with live values. Entity ids are listed in [ENTITIES.md](ENTITIES.md).

## 10. Enable commands (optional)

Read the risks in the README and the interlock section of [DESIGN.md](DESIGN.md) first. The vendor app
cannot reach the box, so Home Assistant's Pause and Cancel are the only remote stop.

Set `CATLINK_COMMANDS=1` in `/etc/catlink-local/catlink-local.env` and restart:
```sh
sudo systemctl restart catlink-gateway
```
Change it in the env file only (it already sets `CATLINK_COMMANDS=0`, which would override any
`Environment=` line in a unit drop-in).

To turn commands off again, set `CATLINK_COMMANDS=0` and restart. The gateway then publishes an empty
retained discovery payload for each command entity, so the buttons, selects, numbers and switches disappear
from Home Assistant (`ha_discovery_published` shows `commands_cleared=15`).

**Check:** the journal shows `ha_subscribed_commands`, and HA gains the buttons, selects, numbers and
switches. For the first **Clean now**, take the cats out of the room and stay by the box. Refusals are
logged with their reasons:
```sh
journalctl -u catlink-gateway -o cat | grep '"command_refused"'
```

## 11. Operations

**Logs.** Both services log JSONL to the journal:
```sh
journalctl -u catlink-gateway -o cat --since today | jq -c 'select(.event != "publish")'
```
Every box frame is logged in full hex (`publish` events), which is what makes later mapping possible.
Make the journal persistent (`Storage=persistent` in `/etc/systemd/journald.conf`, or create
`/var/log/journal`) if `journalctl --list-boots` shows only the current boot. There is no log cap by
default (`CATLINK_LOG_MAX_BYTES=0`): journald rotates the journal itself (`SystemMaxUse=` in
`journald.conf`). journald also rate-limits each service (`RateLimitIntervalSec=` / `RateLimitBurst=`,
default 10000 messages per 30 s); the gateway stays far below that in normal operation, and a suppressed
burst shows up as a `Suppressed N messages` line. If you set a positive `CATLINK_LOG_MAX_BYTES`, each
process stops logging after that many bytes with a final `log_cap_reached`; a restart resets it.

**State.** `visits.jsonl` and `cats.json` live in `/var/lib/catlink-local/` (with `DynamicUser=` this is a
link to `/var/lib/private/catlink-local/`). Both can be deleted with the gateway stopped. Deleting
`cats.json` re-seeds the cats from `CATLINK_CATS` (step 8).

**Stale entities.** Command entities are removed automatically when `CATLINK_COMMANDS=0` (step 10). Cat
sensors are not: once a slug is removed from `CATLINK_CATS` (or cats are turned off) the gateway no longer
knows its name, so the old entities stay in Home Assistant, with no value, because their retained
discovery configs are still on the broker. Remove them in one of two ways:
- In Home Assistant: **Settings → Devices & services → Entities**, search `catlink_open_x_<old slug>`, select
  the entities and **Delete**. If HA refuses because the entity is still provided by MQTT, clear the
  retained config first (next item).
- Clear the retained discovery configs, one per old cat object id (`<slug>_weight`, `<slug>_trend_7d`, and
  with cats fully off also `cat_gap`, `last_visit_cat`, `labelled_pct`, `correction_count`):
  ```sh
  for id in <old-slug>_weight <old-slug>_trend_7d; do
    mosquitto_pub -h 127.0.0.1 -u <mqtt-admin-user> -P '<mqtt-admin-password>' \
      -r -n -t "homeassistant/sensor/catlink_litterbox/$id/config"
  done
  ```
  An empty retained message on a discovery topic deletes that entity in HA.

**Upgrade.**
```sh
cd catlink-local && git pull && sudo ./install.sh
sudo systemctl restart catlink-authresp catlink-gateway
```

**Roll back to the vendor cloud.**
1. `sudo systemctl disable --now catlink-gateway catlink-authresp`
2. Remove the DNS entries (and `sudo pihole restartdns`, or restart dnsmasq).
3. Turn off the router block for `<box-mac>`, accepting that the box may now update its firmware.
4. Power-cycle the box.
5. Remove the device from Home Assistant: **Settings → Devices & services → MQTT → CATLINK Open X →
   Delete**, which clears its retained discovery. Or clear the retained topics with a Mosquitto user that
   may read and write them:
   ```sh
   mosquitto_sub -h 127.0.0.1 -u <mqtt-admin-user> -P '<mqtt-admin-password>' \
     -t 'homeassistant/+/catlink_litterbox/+/config' -t 'catlink/litterbox/#' -W 3 -F '%t' 2>/dev/null \
     | sort -u | while read -r t; do
         mosquitto_pub -h 127.0.0.1 -u <mqtt-admin-user> -P '<mqtt-admin-password>' -r -n -t "$t"
       done
   ```

**Uninstall.**
```sh
sudo ./install.sh --uninstall
```
This stops and disables both units and removes `/opt/catlink-local` and the unit files. It leaves
`/etc/catlink-local` (your env, certificate, key and MQTT password) and `/var/lib/catlink-local`; remove
them by hand if you want them gone, together with the firewall rule, the DNS entries, and the `catlink`
user and ACL block in Mosquitto.

## 12. Troubleshooting

Find the event with `journalctl -u catlink-gateway -u catlink-authresp -o cat | grep '"event":"<name>"'`.

| Journal event | Likely cause | What to do |
|---|---|---|
| exit status 2, `configuration error: CATLINK_…` on stderr | a required variable is missing or malformed | fix the named variable in the env file (step 8) |
| unit fails to start, `status=243/CREDENTIALS` in `systemctl status` | a `LoadCredential=` source is missing or unreadable: `/etc/catlink-local/ha_mqtt.pass` (gateway), or `cert.pem` / `key.pem` (responder). systemd refuses to start the unit, so the service itself logs nothing | create the file (steps 6 and 7), then `sudo systemctl restart catlink-gateway` (or `catlink-authresp`) |
| `ha_no_password` (`reason` `ENOENT` / `EACCES` / `ha_no_password_source`) | only outside systemd: `CATLINK_HA_PASS_FILE` missing or unreadable, or neither it nor `$CREDENTIALS_DIRECTORY` set (under the shipped unit a missing file shows as 243/CREDENTIALS instead) | fix the file; the gateway retries every 5 s and logs again only if the reason changes |
| `ha_disconnected` with `reason` `ha_connack_refused_5` | Mosquitto rejected the login: wrong user, wrong password, or the user is not in the password file | redo step 7, `sudo systemctl reload mosquitto`, then `sudo systemctl restart catlink-gateway` (the unit copies the password file in only when it starts); check `CATLINK_HA_USER` |
| `ha_disconnected` with `reason` `ha_closed` | Mosquitto closed the connection (restart, or it dropped the client) | nothing if `ha_connected` follows within seconds; otherwise check the Mosquitto log |
| `ha_disconnected` with `reason` `ECONNREFUSED` | Mosquitto not listening on `CATLINK_HA_HOST:CATLINK_HA_PORT` | start Mosquitto / fix host and port |
| `ha_connected` but no entities / no state in HA | ACL does not allow `catlink` to write the discovery or state topics, or HA's user cannot read them (Mosquitto drops denied publishes silently) | check the ACL block (step 7) and HA's own user |
| `foreign_peer` | a device other than `<box-mac>` (or not `<box-ip>` when `CATLINK_BOX_IP` is set) connected; or the box's IP changed | check `CATLINK_BOX_MAC` / `CATLINK_BOX_IP` against the router; the firewall rule (step 4) should stop other devices earlier |
| `tls_fail` from the box | the box's firmware rejected the handshake or the certificate | check the certificate (step 6); if it persists, the firmware probably validates certificates and this method does not work on it |
| no `tcp_accept` at all | DNS not redirected, cache not flushed, 443 taken, or firewall | steps 3 and 4; power-cycle the box |
| `unknown_layout` in `last_unusable_heartbeat` (diag), no state updates | a heartbeat layout the decoder does not know: different firmware or schedule-slot count | open an issue with the body length and slot count (see CONTRIBUTING.md); nothing is guessed |
| `unknown_frame` | a frame type not decoded yet | harmless; shown on the "Last unknown frame" sensor; see CONTRIBUTING.md |
| `command_refused` | an interlock failed (`reasons` lists which) | expected while a cat is in or near the box, the heartbeat is stale, or the box is not idle |
| `log_cap_reached` | a positive `CATLINK_LOG_MAX_BYTES` was reached; nothing more is logged until restart | set it to `0` (default) and let journald rotate |

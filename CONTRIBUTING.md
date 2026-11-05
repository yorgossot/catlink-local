# Contributing

The most useful contributions are observations from other boxes and firmware versions, and newly mapped
heartbeat bytes. Code changes should keep the safety model in [docs/DESIGN.md](docs/DESIGN.md) intact:
one PUBLISH path to the box, commands off by default, interlocks unchanged unless the change is argued
from evidence.

## Never post your device's identifiers

Issues, pull requests, logs and test vectors must not contain:

- the productKey or deviceName (`{pk}`, `{dn}`), or topics that include them;
- the box's MAC address. In a raw frame the MAC is **bytes 5–10**: replace them with
  `02 ca 7b 0c 00 01` (the synthetic MAC the tests use) and recompute the checksum, or cut them out;
- your `<instance>` MQTT host-name prefix, your LAN addresses or host names;
- the reply to the info query `0x245A` (it contains your Wi-Fi network name);
- authresp request bodies (they carry device identifiers; the responder never logs them).

**Before posting logs**, redact them. The gateway already logs box topics with `{pk}`/`{dn}`
placeholders and logs only the length of the box's MQTT username, but check anyway: any topic that still
contains your productKey or deviceName must be rewritten to `{pk}`/`{dn}`, and every `payload_hex` /
`body_hex` of a full frame contains the MAC in bytes 5–10 (see above).

Heartbeat **bodies** (byte 15 up to, not including, the trailing checksum) contain no device identifier
that we know of on firmware 04.22, so they are the preferred thing to share; they do include the box's
clock and scale readings. Say which firmware and how many schedule slots your box has.

## Mapping a new byte

1. Watch the diagnostic entities **Unmapped heartbeat bytes**, **Unmapped bytes changed** and **Unmapped
   change count**. They change whenever a byte outside the field table changes.
2. Cause one event (or wait for it) and note roughly when it happened.
3. Pull the full heartbeat frames around that time:
   ```sh
   journalctl -u catlink-gateway -o cat --since "-15 min" | grep '"event":"publish"'
   ```
4. Diff the bodies before and after. Offsets from 41 up shift by +3 per schedule slot beyond 4.
5. Repeat until the byte follows the event every time, ideally changed both ways.
6. Make the change:
   - one `HFIELD` line in `HEARTBEAT_FIELDS` (`catlink_local/protocol.py`), using the 4-slot offset;
   - one discovery entry in `catlink_local/gateway.py` if it should appear in Home Assistant;
   - one synthetic vector in `tests/` (built with `tests/vectors.py`, never pasted from a capture);
   - a row in [docs/PROTOCOL.md](docs/PROTOCOL.md) and [docs/ENTITIES.md](docs/ENTITIES.md) with an
     honest confidence tag.
7. Run the tests and the leak check:
   ```sh
   python3 -m unittest discover -s tests -v
   bash tools/leakcheck.sh
   ```

## Reporting a different box or firmware

Open an issue with: model, firmware version, slot count, whether the login against a self-signed
certificate succeeded (`tls_ok` or `tls_fail` in the authresp journal), and any `unknown_layout`,
`unknown_frame` or `decode_error` events, with identifiers removed as above.

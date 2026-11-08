# Design and safety model

## Components

```
                 box (plaintext MQTT 3.1)                     your Mosquitto (user "catlink")
                          |                                            ^  discovery, state, diag,
                          v  <host-lan-ip>:1883                        |  visits, availability (retained)
  +----------------------------------------+   snapshot    +-----------------------------+
  | box-facing broker + decoder            | ------------> | HA bridge thread            |
  |  accept loop + one reader per session  |               |  minimal MQTT 3.1.1 client  |
  |  answers CONNACK/SUBACK/PUBACK/PINGRESP|  queue        |  reconnects forever         |
  |  publish_to_box(): the ONLY PUBLISH    | <------------ |  receives BASE/+/set        |
  +----------------------------------------+  (commands)   +-----------------------------+
                         catlink-gateway.service
```

- **catlink-authresp** answers the box's HTTPS login with a fixed JSON that names your host as the MQTT
  broker. It never opens an outbound connection, logs method and path only (never request bodies, which
  carry device identifiers), and keeps serving after rejecting a foreign peer. Each accepted connection
  (TLS handshake included) runs in its own thread with a 15 s socket timeout, so a client that connects
  and goes silent cannot block the box's next login.
- **catlink-gateway** is one process with two kinds of thread:
  - **box side**: an accept loop and a reader per session. Decoding is wrapped in `try/except` and only
    updates a shared snapshot, so a decoder bug cannot break the box session.
  - **HA side**: a dependency-free MQTT client on its own thread. It publishes discovery, state and
    diagnostics, and (with commands on) receives `…/set` messages. A crash or a broker outage here never
    touches the box thread.

Both run as `DynamicUser=yes` with no capabilities except `CAP_NET_BIND_SERVICE` for the responder, and
log JSONL to journald.

## The box side is observe-only by construction

- `send()` writes only the packet types in `ALLOWED_TX`: CONNACK, SUBACK, PUBACK, PINGRESP. PUBLISH is not
  in that set.
- `publish_to_box()` is the **single** PUBLISH path to the box. It refuses unless `CATLINK_COMMANDS=1`.
  With commands off, no code path can send the box anything but those four control packets.
- The box's CONNECT password is never parsed; its buffer is zeroed. The gateway accepts any credentials,
  because it has nothing to check them against. That is why the network must restrict who can connect.
- Peer pinning: a connection is accepted only from `CATLINK_BOX_MAC` (looked up in the kernel ARP table)
  and, if set, `CATLINK_BOX_IP`. Anything else is logged as `foreign_peer` and closed.
- One client at a time: a new session from the box takes over the old one.
- `{pk}`/`{dn}` are learned from the box's own SUBSCRIBE; nothing device-specific is compiled in.

## Two halves, one flag

| | `CATLINK_COMMANDS=0` (default) | `CATLINK_COMMANDS=1` |
|---|---|---|
| Sensor entities | yes | yes |
| Buttons, selects, numbers, switches | not discovered | discovered |
| Subscribes `BASE/+/set` | no | yes |
| `publish_to_box()` | refuses | interlocked |

Retained messages on command topics are dropped (`ha_retained_dropped`), so a stale retained "Clean now"
can never fire after a restart. **Nothing auto-fires**: the gateway has no timer, schedule or trigger of
its own. A command runs only when a non-retained `…/set` message arrives from Home Assistant. Automations
that press buttons live in HA, where you can see and disable them.

## Command interlocks

Evaluated against the **latest** heartbeat when the command arrives. A refusal sends nothing and logs
`command_refused` with the failing `reasons`.

| Gate | Commands | Requires |
|---|---|---|
| movement | clean, level | session live and subscribed to the command topic; heartbeat decoded with a known slot count (4 or 5); heartbeat younger than `HEARTBEAT_MAX_AGE_S = 130` s; state idle; not occupied; not touched; live scale within `SCALE_TOLERANCE = 30` cg (0.30 kg) of the lower median of at least 2 recent empty readings |
| resume | resume | session live and subscribed; state paused; not occupied; not touched; heartbeat fresh |
| live | pause, cancel, all settings | session live and subscribed |

Why these numbers:
- Idle heartbeats arrive every ~100 s. At 130 s a missing beat means the box is slow or gone, not merely
  quiet, so movement is refused.
- The gate looks at one heartbeat, not a run of them. Requiring several consecutive idle beats made a
  post-visit clean take minutes to be allowed and silently refused short clean delays.
- Touch sits at body offset `41 + 3 × (slots − 4)`. An unknown slot count means unknown offsets, so it is
  refused rather than guessed.
- The scale check catches a cat on the scale while the occupancy flag still reads 0 (flags lag the scale).

**What the interlock cannot do.** It cannot see a cat that arrives after the latest heartbeat, and it
cannot stop a cycle once started. That window is covered only by the box's own anti-pinch and sensors.
Pause and cancel are available through HA, with no vendor-app fallback.

## Availability

`BASE/availability` is retained. The gateway's MQTT last will sets it to `offline` if the gateway dies;
it also flips to `offline` when no fresh heartbeat has arrived for `OFFLINE_AFTER_S = 300` s.

## Unknown is surfaced, not invented

The decoder is table-driven (`HEARTBEAT_FIELDS`). Bytes not covered by the table are published on
`BASE/diag` (`unmapped_bytes`, `unmapped_changed_at`, `unmapped_change_count`), and any frame with an
unknown command appears as `last_unknown_frame`. Every box PUBLISH is logged with its full hex in the
journal, so an event can be mapped later from history. Signals not yet identified (deodorant,
error, child-lock and kitty readback) are not exposed at all rather than guessed.

## Visit weight and deposit

- The box reports a max-hold weight per visit. The gateway publishes it first, then a **settled** value:
  the median of heartbeat samples taken while the cat was genuinely on the scale (at least 0.15 kg above
  the baseline), minus the empty baseline before the visit. With fewer than 2 such samples it keeps the
  box value.
- The **empty baseline** comes from idle, unoccupied heartbeats. A jump of more than 0.15 kg above the
  current baseline is not accepted outright (it is probably a cat with the flag lagging). A higher level
  after topping up litter is adopted after 3 consecutive readings agree within 0.10 kg.
- The **deposit** is the baseline shift across the visit; under 30 g it reads 0.
- `recent_visits` keeps the last 15 visits, persisted in `visits.jsonl` under the state directory.
- **A cat stepping into a running clean cycle.** The box pauses (`interrupted`), finishes the cycle and only
  then reports the visit, tens of seconds after the occupancy ended. A late report is paired with the
  occupancy that closed within the last 180 s (`LATE_REPORT_GRACE_S`), never with a later one. If that
  occupancy overlapped a clean cycle, the drum was turned, so the weight is not one whole cat and the shift
  is not a deposit: the row is marked `during_clean`, deposit 0, and with cats it is `unknown`.
- On start the last-visit sensors (weight, duration, deposit and, with cats, the label) are restored from
  the last `visits.jsonl` row, so a restart does not leave Home Assistant showing a stale value.

## Litter weight and "litter low"

The box has **no litter-level sensor**. In a test, litter was taken out until the empty scale went from
about 3.3 kg to 0.7 kg, and the box sent nothing new: no flag, no unknown frame, no changed unmapped byte.
The vendor app's "litter not enough" notice is therefore computed in the cloud, most likely from the same
scale weight and the configured litter type.

The empty scale reads the litter in the drum almost directly (a freshly filled box read about 3.2–3.3 kg,
which matches the drum's volume times the litter's density). So the gateway publishes `litter_weight`: the
median of the last 5 trusted empty baselines (the guards above already reject a cat on the scale and
mid-cycle readings; the median rides out a single low one). It appears after 3 readings.

**The box re-zeroes its scale at power-up, with the litter in it.** After a power-cycle the empty scale read
0.00 kg instead of about 3.4 kg, so without a correction the litter weight drops to ~0 and a "litter low"
alarm fires falsely. The gateway therefore carries an offset: on a new box session, if the first trusted
empty reading is at most 0.30 kg while the last litter weight was at least 1.0 kg, that is a re-zero and the
old weight is kept. A plain reconnect reads the same raw weight and changes nothing, and a low reading within
one session is never treated as a re-zero. The offset and the last litter weight persist in `litter.json`
and are restored on start.

A box powered on with an **empty** drum looks exactly like one powered on full, so for a full litter change:
empty the drum, power-cycle the box, press **Litter reset (scale zeroed empty)** (command half; or publish
`PRESS` to `BASE/litter_reset/set`), then refill.

The threshold depends on your litter, so it lives in Home Assistant, not in the gateway. As a starting
point: the Open X is sold as holding about 4 L to the MAX line (retailer listings; no official figure was
found). Bentonite clumping litter is roughly 0.8–1.0 kg/L, so a full drum is about 3.2–4 kg;
plant-based and wood litters weigh much less. Fill to MAX, wait for `litter_weight`, and set the alarm at about half
of that. See [examples/homeassistant/litter_low.yaml](../examples/homeassistant/litter_low.yaml).

## Cat identification (optional, `CATLINK_CATS`)

Pure analysis of the settled visit weight. It never reads a socket, never builds a frame and never
touches an interlock. Scale resolution is 10 g with ±20–30 g noise, and cats' weights can overlap, so
treat labels as estimates.

- **0 cats (default)**: no tracker, no cat entities, no cat topics.
- **1 cat**: every visit is that cat (conf 100); tracked weight is the median of the rolling window. A
  weight more than 0.30 kg from it (once it has samples), or a visit during a clean cycle, is not a sample.
- **2 cats** (`slug=kg` pairs, lighter first):
  - each visit is labelled by gaussian likelihood (σ = 70 g) against two reference weights, with a
    confidence %;
  - a settled weight within ±60 g of the midpoint is `unknown`;
  - if the references are closer than 150 g the tracker is **degraded** and labels everything `unknown`;
  - a visit too short for a settled weight (the box's max-hold peak stands in) is less certain: it gets
    twice the band (±120 g) and never becomes a weight sample;
  - a weight more than 0.30 kg below the lighter cat or above the heavier one is not one whole cat (paws
    only, a cat on a turning drum, both cats at once) and is `unknown`, never a sample. This applies to a
    cat only once its reference has weight samples, so rough seeds cannot lock every visit out;
  - a visit that overlapped a clean cycle is `unknown` (see "Visit weight and deposit");
  - **no timing rule**: each visit is labelled on its own weight. An earlier rule ("two visits within
    15 min are different cats") kept mislabelling one cat coming back several times in a row, so it was
    removed. When unsure, the label is `unknown`; a degraded tracker recovers through `set_ref_<slug>`;
  - tracked weight = median of that cat's confident visits in the last 14 days (at most 10), not an
    average of the latest readings; the 7-day trend and the gap come from the same samples (within 20 g
    reads "steady");
  - references move only by a confident re-fit to those medians, which never lets them cross or come
    closer than 150 g, or by explicit input: `correct_last` (relabel the last visit) and
    `set_ref_<slug>` (a kitchen-scale weigh-in).
- More than 2 cats is refused at startup. State persists in `cats.json`; a file whose slugs do not match
  the config is ignored (`cats_state_mismatch`).
- **Seeds are used once.** The kg values in `CATLINK_CATS` (e.g. `cat_a=3.20,cat_b=4.60`) seed the
  references only until `cats.json` exists; from then on the saved references win and editing the kg in
  the env file has no effect. To re-seed later, publish a weigh-in to `set_ref_<slug>`, or stop the
  gateway, delete `cats.json`, and start it again.

## What a bug here can and cannot do

| Bug in | Worst case |
|---|---|
| decoder, deposit or cat logic | wrong or missing HA values; the box session continues |
| HA bridge | no or stale entities; availability goes `offline`; the box session continues |
| command path, commands off | nothing: no PUBLISH path is reachable |
| command path, commands on | a command sent that should have been refused; mitigated by the interlock and the box's own sensors, not eliminated |
| authresp | the box cannot log in and goes offline; it is never sent anything but the login reply |
| a crash of either service | systemd restarts it after 5 s; meanwhile the box is offline (no cloud fallback) |

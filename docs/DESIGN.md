# Design and safety model

## Components

```
                 box (plaintext MQTT 3.1)                     your Mosquitto (user "catlink")
                          |                                            ^  discovery, state, diag, visits,
                          v  <host-lan-ip>:1883                        |  cats, availability (retained)
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

What a real box sends during a visit, and what the gateway does about it:

- **The box's own visit report is not a weight.** Its `kg.cg` field was within ±30 g of the cat only about a
  third of the time, often 100 g or more high and sometimes far *low*. It is kept as `peak` (row) and
  `last_visit_peak` (state, diagnostic entity), and used only as a cross-check for a visit with a single live
  reading, and as "both cats at once" evidence when it is about two cats' total.
- **The weight comes from the live heartbeats.** While the box is occupied, every reading more than 0.15 kg
  above the pre-visit empty baseline is a sample. The samples inside the tracker's whole-one-cat window
  (`plausible_range`: around every known cat, never two cats' sum) are grouped into **plateaus** (readings
  within 50 g; a cat sitting still reads within about 10 g). The visit weight is the median of the largest
  plateau, minus the baseline. Readings above the window (a cat jumping in, a second cat's paws, both cats)
  are dropped and counted (`readings_above_window_24h`); readings below it (paws on the rim) are dropped.
- **Two cats in one occupancy.** Two plateaus of at least 2 readings each, at least 120 g apart, are two
  segments. The tracker decides whether they are two cats (row `cat: two_cats`, both credited, both
  weights in the row) or one cat perched on the rim first and then inside (one visit, the larger plateau). A
  tie between the two largest plateaus is no weight at all.
- **The pre-visit baseline** is the median of the last 3 trusted empty readings since the last clean or visit
  (one reading moves 60–130 g with the drum position). A reading more than 0.15 kg above it is adopted only
  after 3 agreeing readings (a litter top-up, not a cat with the flag lagging); one more than 0.10 kg below
  only after 2 agreeing readings, except the first empty reading after a clean cycle, which is adopted at once
  (the clean really removed litter). A visit that opens while a drop is unconfirmed is flagged
  (`baseline_alt`) and is labelled only if both baselines give the same cat. A baseline at the 0.00 floor
  (the scale clamps) is flagged `clamped` and counts as a less certain reading.
- **The occupancy flag lags and flaps.** An unoccupied reading still more than 0.15 kg above the baseline holds
  the visit open (the cat is still on the scale). The hold ends at the next reading near the baseline; or when
  2 held readings agree at a level that cannot be a cat (a big deposit, litter added: the new empty level);
  or after 10 minutes (the weight still comes from the occupied readings; no deposit).
- **Pairing the box's report with an occupancy.** The occupancy runs 7–32 s longer than the duration the box
  reports, and a report can arrive after the occupancy closed (40–50 s late when a cat stepped into a running
  clean cycle). A report goes to the open occupancy when its span fits, else to the best-fitting occupancy that
  closed within 180 s (`LATE_REPORT_GRACE_S`), else it upgrades a gateway-sourced row it overlaps, else it is
  its own row with no weight. Two reports on one occupancy are two cats when each matches its own plateau
  (within 80 g); otherwise the second report is its own `unknown` row, so the row count matches the box's
  counter. A byte-identical report within 5 s, or one already paired within 180 s, is a redelivery and
  ignored.
- **Visits the box never reports.** An occupancy of at least 60 s with a plateau of at least 2 readings and
  no clean cycle, for which no report arrives within 180 s, becomes a row with `source: gateway`. A shorter
  peek is only logged (`occupancy_unreported`).
- **A cat stepping into a running clean cycle.** The drum was turned, so the weight is not one whole cat and
  the shift is not a deposit: the row is marked `during_clean`, deposit 0, no weight, `unknown`.
- **Deposit** = the baseline shift across the visit; 0 under 30 g or when the drum moved; `null` (unknown,
  logged `deposit_implausible`) above 300 g.
- Rows are kept in time order (a gateway-sourced row can be finalized after a newer visit) and "last visit"
  never goes back in time. On SIGTERM an open visit whose report already arrived, and closed occupancies that
  qualify as gateway visits, are finalized first (`flush_inflight`).
- `recent_visits` keeps the last 15 visits in `visits.jsonl`. On start the last-visit sensors (weight, box
  report, duration, deposit) are restored from its last row.
- **State files** are written atomically (temp file, fsync, rename, directory fsync). A file that does not
  parse, or holds NaN/inf or out-of-range values, is moved aside as `<file>.corrupt-<timestamp>` and logged
  (`state_file_corrupt`), never silently replaced. An unreadable `litter.json` means the re-zero offset is
  unknown: no litter weight is published (rather than a false ~0 kg "litter low") until **Litter reset**.

## Litter weight and "litter low"

The box has **no litter-level sensor**. In a test, litter was taken out until the empty scale went from
about 3.3 kg to 0.7 kg, and the box sent nothing new: no flag, no unknown frame, no changed unmapped byte.
The vendor app's "litter not enough" notice is therefore computed in the cloud, most likely from the same
scale weight and the configured litter type.

The empty scale reads the litter in the drum almost directly (a freshly filled box read about 3.2–3.3 kg,
which matches the drum's volume times the litter's density). So the gateway publishes `litter_weight`: the
median of the last 5 trusted empty baselines (the guards above already reject a cat on the scale, mid-cycle
readings and a one-off low reading). It appears after 3 readings.

**The box re-zeroes its scale at power-up, with the litter in it.** After a power-cycle the empty scale read
0.00 kg instead of about 3.4 kg, so without a correction the litter weight drops to ~0 and a "litter low"
alarm fires falsely. The gateway therefore carries an offset. Once per box session, from the first 2 agreeing
empty readings: if they sit at most 0.30 kg while the last litter weight was at least 1.0 kg, and the raw
reading really dropped by at least 1.0 kg, that is a re-zero and the old weight is kept. A plain reconnect or
a gateway restart reads the same raw weight and changes nothing, and a low reading within one session is
never treated as a re-zero. The offset and the last litter weight persist in `litter.json`
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

Pure analysis of the per-visit observation above. It never reads a socket, never builds a frame and never
touches an interlock. The tracker is `catlink_local/cats.py`; its tuning constants are the block at its top.

**What the measurements allow.** The scale resolves 10 g. A cat sitting still reads within about 10 g during a
visit, but the plateau median varies by 25–30 g (sd) from visit to visit, and two cats can weigh within a few
hundred grams of each other, or cross as one grows. There is no camera and no RFID: weight is the only
signal. So the tracker states how sure it is, and says "I don't know" rather than guess.

- **0 cats (default)**: no tracker, no cat entities, no cats topic. Visits are still weighed (the window is
  0.5–15 kg).
- **1 to 3 cats** (`slug=kg`, rough seeds, any order; the tracker itself is generic in N, 3 is the most its
  tests cover).

**The model.**
- **A track per cat**: a small Kalman filter of its weight and growth rate (kittens grow; a coasting rate
  decays to 0 over about two months). The published weight is the track's prediction for now, with a ±2 sd
  range (`<slug>_weight_lo_kg` / `_hi_kg`); never when that sd exceeds 75 g.
- **Labels.** Each plateau is scored as "this whole cat", "this cat perched" (100–300 g light), "this cat plus
  something" (100–450 g heavy) or junk. A visit is named only when the posterior is at least 0.85 and the
  reading is within 3 sd of that cat; the stated % is that posterior, capped at 99 (weight alone is never
  certain), and in simulation it matches the real accuracy. Otherwise the visit is `unknown`. **No timing,
  alternation or visit-rate assumption** enters a label: cats do whatever they like, and an earlier rule ("two
  visits within 15 minutes are different cats") kept mislabelling one cat coming back several times.
- **Tracks move only on label-grade evidence**: a named plateau, a weigh-in, a correction, or the label-free
  net. An unknown visit never moves a track.
- **The label-free net**: a mixture fit of the last 3 days of plateaus, labelled or not. It publishes the
  weights the box sees without names (`cluster_low_kg` / `cluster_high_kg`), bootstraps a fresh install
  (where the seeds give only an *order*), repairs a stuck track, and decides when "can't tell apart" ends.
- **"Can't tell them apart"** (`cant_tell_apart`): two cats' predictions overlap *and* recent visits fit both.
  Both are frozen and visits stay unknown. When the clusters separate again and every cat is still where it
  was, labelling resumes; otherwise, and after 48 h in any case, the status asks for a weigh-in.
- **Sudden change** (`<slug>_stale_reason: sudden_change`): a CUSUM on each track's errors towards another
  cat. Both identities are dropped (weights blank, clusters still published) until the clusters or a weigh-in
  session settle them. When every cat's readings moved the same way by at least 40 g at once (litter topped
  up, the scale's zero drifted), it is a **scale shift**, not a cat: no alarm, and `scale_shift` says so for
  72 h.
- **Weights not shown** rather than wrong: a cat not seen for 72 h (`not_seen`), not named for a while
  although its visits keep coming in as unknown, or whose newest weigh-in disagrees with its track
  (`not_identified`), shows no weight. The status reads `ok` only with fresh evidence of every cat.
- **Growth** (`<slug>_growth_g_week`) is a robust (Theil–Sen) slope; it reads gaining / losing only when the
  data really shows it, else steady, and "not enough data" below 6 samples over 5 days.

**Weigh-ins and corrections** (gateway-internal commands, never sent to the box):
- `set_ref_<slug>` = a kitchen-scale weight in kg (see
  [examples/homeassistant/cat_weigh_in.yaml](../examples/homeassistant/cat_weigh_in.yaml)). For a cat whose
  identity is settled it is one more measurement. One far from the track is **rejected with a reason**
  (`<slug>_last_weigh_in`); entering it again within 24 h confirms it. Home Assistant's input helpers send a
  value only when it changes, so the published reason says "enter it again changed by 0.01 kg (e.g. 3.31 for
  3.30)"; the tracker treats values within 50 g as the same confirmation.
- While identities are not settled (after a crossing, a sudden change, or on a fresh install), a single
  weigh-in waits (`pending`). Identity is re-established only by a **weigh-in session**: every unsettled cat
  weighed within 24 h, values at least 60 g apart, each near a distinct cluster the box sees. Never by
  elimination.
- `correct_last` = the newest visit was this cat (or `unknown`). The visit's track updates are redone; a
  correction that fits another cat's track better swaps the two only on a second, independent statement.
  Corrections of every unsettled cat within 24 h on readings at least 60 g apart also form a session.
- The box scale and the kitchen scale are taken to read the same. A constant difference between them shows
  up 1:1 in the published weight.

**State.** The tracker writes `cats_v2.json` next to `CATLINK_CATS_FILE`. A `cats.json` from catlink-local 0.1
is read once to migrate (its samples become established tracks, its counters carry over) and is never
written, so a downgrade finds it untouched. If a downgraded gateway later wrote `cats.json` again, its
sha256 no longer matches the one stamped into `cats_v2.json` at migration: the stale `cats_v2.json` is moved
aside (`cats_v2.json.stale-<timestamp>`) and `cats.json` migrated again. An unreadable `cats_v2.json` is
quarantined and the tracker starts fresh (status `learning`). A state file for other slugs is ignored
(`cats_state_mismatch`).

**Honest limits.**
- **When two cats' weights cross, the box alone cannot tell who is who.** Nothing is named until every cat
  is weighed within a day (a weigh-in session). In simulation of a crossing with no weigh-ins, about 80 % of
  cat-days show no weight; that is the price of never publishing a confident wrong one.
- More "no weight" time than a simple median: in simulation of two kittens about 250 g apart, weighed once a
  week, about one cat-day in five has no published weight, while wrong labels stay under about 1 % of
  plateaus and a confident wrong weight (status ok, more than 100 g off) about 0.1 % of cat-days or less.
- A cat that suddenly steps into another cat's range mislabels part of its visits for about a day (a few %
  of labels in simulation) before the sudden-change check or the clusters catch it.
- Labels are estimates (typically 90–99 %, never 100). The numbers above are from a simulator shaped like one
  real box and two kittens; your cats, litter and box may differ. The constants are in `cats.py`.

## What a bug here can and cannot do

| Bug in | Worst case |
|---|---|
| decoder, deposit or cat logic | wrong or missing HA values; the box session continues |
| HA bridge | no or stale entities; availability goes `offline`; the box session continues |
| command path, commands off | nothing: no PUBLISH path is reachable |
| command path, commands on | a command sent that should have been refused; mitigated by the interlock and the box's own sensors, not eliminated |
| authresp | the box cannot log in and goes offline; it is never sent anything but the login reply |
| a crash of either service | systemd restarts it after 5 s; meanwhile the box is offline (no cloud fallback) |

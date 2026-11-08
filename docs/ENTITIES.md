# Entities, topics and JSON keys

Defaults: `CATLINK_BASE=catlink/litterbox` (BASE), `CATLINK_DISCOVERY_PREFIX=homeassistant` (PREFIX),
`CATLINK_NODE=catlink_litterbox` (NODE), `CATLINK_DEVICE_NAME=CATLINK Open X`.

**Entity ids are minted by Home Assistant** from the device name plus the entity name, so with the default
device name they read `<domain>.catlink_open_x_<slug of name>`. They are *not* the discovery `object_id`.
If you change `CATLINK_DEVICE_NAME`, or HA has to de-duplicate a name, the ids change: check them in
**Settings → Devices & services → Entities** before using the examples.

The ids in this file are what current Home Assistant releases mint. Older releases may build the id from
the entity name alone (e.g. `button.clean_now` instead of `button.catlink_open_x_clean_now`); the registry
is authoritative.

`unique_id` is `NODE_<object_id>` for sensors and `NODE_cmd_<object_id>` for actuators.

"measurement" in the tables below means the discovery config sets `state_class: measurement`, so Home
Assistant keeps long-term statistics (history graphs beyond the recorder window) for that sensor. It is set
only on numeric readings: the weights, the deposit, the live scale, the litter weight, the Wi-Fi signal and the
24-hour visit counts. The ever-growing visit counters are `total_increasing`; text never has a state class.

## MQTT topics

| Topic | Retained | Content |
|---|---|---|
| `BASE/availability` | yes | `online` / `offline`; also the gateway's MQTT last will. Goes `offline` when no fresh heartbeat for 300 s (`OFFLINE_AFTER_S`) |
| `BASE/state` | yes | JSON, keys below |
| `BASE/diag` | yes | JSON, keys below |
| `BASE/visits` | yes | `{"recent_visits":[…]}`, newest first, at most 15 |
| `BASE/cats` | yes | JSON, cat keys below (only with `CATLINK_CATS`); published when a value changes, never per heartbeat |
| `BASE/<object_id>/set` | no | commands from HA. A retained message on these topics is dropped, never acted on |
| `PREFIX/<component>/NODE/<object_id>/config` | yes | discovery |
| `BASE/correct_last/set` | no | gateway-internal, payload a cat slug or `unknown`: the newest visit was this cat (only with `CATLINK_CATS`) |
| `BASE/set_ref_<slug>/set` | no | gateway-internal, payload kg: a kitchen-scale weigh-in (only with `CATLINK_CATS`) |
| `BASE/litter_reset/set` | no | gateway-internal, payload `PRESS`: drop the litter re-zero offset |

The gateway-internal topics are handled inside the gateway and never reach the box. They are subscribed
in both modes; the two cat topics only when `CATLINK_CATS` is set.

Mosquitto ACL for the gateway's user `catlink`:
```
topic readwrite catlink/litterbox/#
topic write homeassistant/+/catlink_litterbox/#
```

## Sensor half (always)

| Component | object_id | Name | Entity id (default device name) | Class / unit / category |
|---|---|---|---|---|
| binary_sensor | occupied | Litter box occupied | `binary_sensor.catlink_open_x_litter_box_occupied` | occupancy |
| binary_sensor | cleaning | Litter box cleaning | `binary_sensor.catlink_open_x_litter_box_cleaning` | running |
| binary_sensor | bin_full | Waste bin full | `binary_sensor.catlink_open_x_waste_bin_full` | problem |
| binary_sensor | drawer_out | Waste drawer | `binary_sensor.catlink_open_x_waste_drawer` | door (on = out) |
| sensor | wifi_signal | Wi-Fi signal | `sensor.catlink_open_x_wi_fi_signal` | signal_strength, dBm, measurement, diagnostic |
| sensor | state | Litter box state | `sensor.catlink_open_x_litter_box_state` | idle / cleaning / interrupted / paused / cancelling / unknown_N |
| sensor | mode | Litter box mode | `sensor.catlink_open_x_litter_box_mode` | automatic / manual / scheduled / unknown |
| sensor | clean_delay | Clean delay | `sensor.catlink_open_x_clean_delay` | min |
| sensor | litter_type | Litter type | `sensor.catlink_open_x_litter_type` | bentonite / mixed / unknown |
| sensor | night_mode | Night mode | `sensor.catlink_open_x_night_mode` | True / False |
| sensor | night_start | Night start hour | `sensor.catlink_open_x_night_start_hour` | h, diagnostic |
| sensor | night_end | Night end hour | `sensor.catlink_open_x_night_end_hour` | h, diagnostic |
| sensor | light | Indicator light | `sensor.catlink_open_x_indicator_light` | True / False |
| sensor | sound | Notification sound | `sensor.catlink_open_x_notification_sound` | True / False |
| sensor | continuous | Continuous cleaning | `sensor.catlink_open_x_continuous_cleaning` | True / False |
| sensor | burial | Soft-stool burial | `sensor.catlink_open_x_soft_stool_burial` | True / False |
| sensor | burial_minutes | Burial delay | `sensor.catlink_open_x_burial_delay` | min, diagnostic |
| sensor | last_visit_weight | Last visit weight | `sensor.catlink_open_x_last_visit_weight` | weight, kg, measurement |
| sensor | last_visit_duration | Last visit duration | `sensor.catlink_open_x_last_visit_duration` | `m:ss` |
| sensor | last_deposit_weight | Last deposit weight | `sensor.catlink_open_x_last_deposit_weight` | g, measurement |
| sensor | visit_count | Visit counter | `sensor.catlink_open_x_visit_counter` | json_attributes from `BASE/visits` |
| sensor | box_clock | Box clock | `sensor.catlink_open_x_box_clock` | `HH:MM`, diagnostic |
| sensor | live_scale | Live scale | `sensor.catlink_open_x_live_scale` | weight, kg, measurement, diagnostic |
| sensor | litter_weight | Litter weight | `sensor.catlink_open_x_litter_weight` | weight, kg, measurement; unknown for the first few minutes |
| sensor | slot_count | Schedule slot count | `sensor.catlink_open_x_schedule_slot_count` | diagnostic |
| sensor | unmapped_bytes | Unmapped heartbeat bytes | `sensor.catlink_open_x_unmapped_heartbeat_bytes` | diagnostic, from `BASE/diag` |
| sensor | unmapped_changed_at | Unmapped bytes changed | `sensor.catlink_open_x_unmapped_bytes_changed` | diagnostic, from `BASE/diag` |
| sensor | unmapped_change_count | Unmapped change count | `sensor.catlink_open_x_unmapped_change_count` | diagnostic, from `BASE/diag` |
| sensor | last_unknown_command | Last unknown frame | `sensor.catlink_open_x_last_unknown_frame` | diagnostic, from `BASE/diag` |
| sensor | last_visit_peak | Last visit box report | `sensor.catlink_open_x_last_visit_box_report` | weight, kg, diagnostic |

`last_visit_weight` is the weight measured from the live readings once the visit is finalised (unknown when
no plateau could be measured); the box's own visit report is `last_visit_peak`, which is not a reliable
weight. See [DESIGN.md](DESIGN.md) "Visit weight and deposit" and [PROTOCOL.md](PROTOCOL.md) §5.

## Command half (`CATLINK_COMMANDS=1` only)

| Component | object_id | Name | Entity id (default device name) | Notes |
|---|---|---|---|---|
| button | clean | Clean now | `button.catlink_open_x_clean_now` | payload `PRESS`; full movement interlock |
| button | pause | Pause | `button.catlink_open_x_pause` | payload `PRESS` |
| button | resume | Resume | `button.catlink_open_x_resume` | payload `PRESS`; only when paused and empty |
| button | cancel | Cancel | `button.catlink_open_x_cancel` | payload `PRESS` |
| button | level | Level litter | `button.catlink_open_x_level_litter` | payload `PRESS`; full movement interlock |
| select | mode | Mode | `select.catlink_open_x_mode` | options automatic, manual |
| select | litter_type | Litter type | `select.catlink_open_x_litter_type` | options bentonite, mixed |
| number | clean_delay | Clean delay | `number.catlink_open_x_clean_delay` | 1–10 min; the gateway accepts only 1, 3, 5, 7, 10 |
| number | burial_minutes | Burial delay | `number.catlink_open_x_burial_delay` | 1–60 min |
| switch | light | Indicator light | `switch.catlink_open_x_indicator_light` | ON / OFF |
| switch | sound | Notification sound | `switch.catlink_open_x_notification_sound` | ON / OFF |
| switch | continuous | Continuous cleaning | `switch.catlink_open_x_continuous_cleaning` | ON / OFF |
| switch | burial | Soft-stool burial | `switch.catlink_open_x_soft_stool_burial` | ON / OFF |
| switch | child_lock | Child lock | `switch.catlink_open_x_child_lock` | ON / OFF, optimistic (no readback) |
| switch | kitty | Kitty mode | `switch.catlink_open_x_kitty_mode` | ON / OFF, optimistic (no readback) |
| button | litter_reset | Litter reset (scale zeroed empty) | `button.catlink_open_x_litter_reset_scale_zeroed_empty` | payload `PRESS`; gateway-internal, never reaches the box. Press after a full litter change (see DESIGN.md) |

With commands off these entities are not discovered, the gateway does not subscribe to `BASE/+/set`, and
it has no path that can PUBLISH to the box. On every connect in that mode it also publishes an empty
retained payload to each of these 15 discovery topics, so entities left over from an earlier
`CATLINK_COMMANDS=1` run disappear from HA.

**`select.catlink_open_x_mode` and scheduled mode.** `scheduled` is deliberately not an option (the
gateway does not synthesise a schedule table). While the box is in scheduled mode (set on the box or by an
earlier app setting) the select reads unknown and Home Assistant logs an "invalid option" warning for it;
`sensor.catlink_open_x_litter_box_mode` still shows `scheduled`. Choosing automatic or manual switches the
box out of it.

## Cat entities (`CATLINK_CATS` set)

Shown for slug `cat_a`; one set per configured cat. The display name is the slug with `_` → space, first
letter capitalised (`cat_a` → "Cat a"). Every cat entity reads the retained `BASE/cats` topic.

| Component | object_id | Name | Entity id (default device name) | Class / unit / category |
|---|---|---|---|---|
| sensor | `<slug>_weight` | `<Display> weight` | `sensor.catlink_open_x_cat_a_weight` | weight, kg, measurement; json_attributes = `BASE/cats`. Unknown while the tracker cannot stand behind a weight (see `<slug>_stale_reason`) |
| sensor | `<slug>_weight_lo` | `<Display> weight low` | `sensor.catlink_open_x_cat_a_weight_low` | weight, kg, measurement: the weight − 2 sd |
| sensor | `<slug>_weight_hi` | `<Display> weight high` | `sensor.catlink_open_x_cat_a_weight_high` | weight, kg, measurement: the weight + 2 sd |
| sensor | `<slug>_stale_reason` | `<Display> weight status` | `sensor.catlink_open_x_cat_a_weight_status` | text: OK / Not seen for 3 days / Not seen yet / Not identified: weigh every cat / Weight changed suddenly: weigh every cat / Unknown / unavailable |
| sensor | `<slug>_trend_7d` | `<Display> growth` | `sensor.catlink_open_x_cat_a_growth` | g/wk, diagnostic (the object id is kept from 0.1) |
| sensor | `<slug>_visits` | `<Display> visits` | `sensor.catlink_open_x_cat_a_visits` | total_increasing |
| sensor | `<slug>_visits_24h` | `<Display> visits 24h` | `sensor.catlink_open_x_cat_a_visits_24h` | measurement |
| sensor | `<slug>_last_seen` | `<Display> last seen` | `sensor.catlink_open_x_cat_a_last_seen` | timestamp |
| sensor | `<slug>_last_weigh_in` | `<Display> last weigh-in` | `sensor.catlink_open_x_cat_a_last_weigh_in` | text: `accepted` / `pending` / `rejected` + kg + time + reason; `none yet` |
| sensor | cat_status | Cat ID status | `sensor.catlink_open_x_cat_id_status` | text: OK / Learning the cats / Can't tell them apart / Weigh every cat / unavailable; json_attributes = `BASE/cats` |
| sensor | cat_gap (2+ cats) | Cat gap | `sensor.catlink_open_x_cat_gap` | g, diagnostic: the smallest gap between published weights |
| sensor | cluster_low (2+ cats) | Lightest cat cluster | `sensor.catlink_open_x_lightest_cat_cluster` | weight, kg: what the box sees, without names |
| sensor | cluster_high (2+ cats) | Heaviest cat cluster | `sensor.catlink_open_x_heaviest_cat_cluster` | weight, kg |
| sensor | cluster_low_n, cluster_high_n (2+ cats) | Lightest / Heaviest cluster visits | `sensor.catlink_open_x_lightest_cluster_visits` | diagnostic |
| sensor | visits_24h_total | Visits 24h | `sensor.catlink_open_x_visits_24h` | measurement, diagnostic |
| sensor | scale_shift | Scale shift | `sensor.catlink_open_x_scale_shift` | diagnostic: `none`, or e.g. `scale_shift +52 g <time>` for 72 h after every cat's reading moved together |
| sensor | readings_above_window_24h | Readings above window 24h | `sensor.catlink_open_x_readings_above_window_24h` | diagnostic |
| sensor | last_visit_cat | Last visit cat | `sensor.catlink_open_x_last_visit_cat` | text such as `Cat a 94%`, `Unknown`, `Cat a + Cat b`; json_attributes = `BASE/cats` |
| sensor | labelled_pct | Labelled 7d | `sensor.catlink_open_x_labelled_7d` | %, diagnostic |
| sensor | correction_count | Correction count | `sensor.catlink_open_x_correction_count` | diagnostic |
| sensor | unknown_visits | Unknown visits | `sensor.catlink_open_x_unknown_visits` | total_increasing, diagnostic |
| sensor | two_cat_visits (2+ cats) | Two-cat visits | `sensor.catlink_open_x_two_cat_visits` | total_increasing, diagnostic |

The object ids of 0.1 (`<slug>_weight`, `<slug>_trend_7d`, `cat_gap`, `last_visit_cat`, `labelled_pct`,
`correction_count`) are kept, so on an upgrade Home Assistant keeps their entity ids (e.g.
`sensor.catlink_open_x_cat_a_trend_7d`, `sensor.catlink_open_x_labelled_pct`) and only updates their names;
the ids in the table are what a fresh install mints. Because the weight, status and last-visit sensors carry `BASE/cats` as attributes, a dashboard can
read e.g. `state_attr('sensor.catlink_open_x_cat_a_weight', 'cat_a_trend_word')` or `'cat_status'`.

## `BASE/state` keys

| Key | Type | Notes |
|---|---|---|
| state | string | idle, cleaning, interrupted, paused, cancelling, `unknown_<n>` |
| cleaning | bool | state == cleaning |
| occupied | bool | |
| mode | string | automatic, manual, scheduled, unknown |
| litter_type | string | bentonite, mixed, unknown |
| clean_delay | int | minutes |
| night_mode | bool | |
| night_start, night_end | int | hours |
| light, sound, continuous, burial | bool | |
| burial_minutes | int | |
| visit_count | int | resets to 0 when the drawer is put back |
| live_scale | float | kg |
| litter_weight | float | kg, median of the last 5 trusted empty-scale readings; absent until 3 exist |
| slot_count | int | |
| bin_full | bool | |
| drawer_out | bool | true = out |
| wifi_signal | int | dBm |
| box_clock | string | `HH:MM`, the box's own (unset) clock |
| last_visit_weight | float or null | kg, measured from the live readings; null when no plateau could be measured |
| last_visit_peak | float or null | kg, the box's own visit report (not a weight) |
| last_visit_duration | string | `m:ss` |
| last_visit_seconds | int | |
| last_visit_at | string | ISO timestamp |
| last_deposit_weight | int or null | g; null = implausible (more than 300 g), unknown |

## `BASE/cats` keys (with `CATLINK_CATS`)

Per cat (`<slug>_…`):

| Key | Notes |
|---|---|
| `<slug>_weight` | kg or null: the track's prediction for now; null = not published (see `_stale_reason`). A seed is never published |
| `<slug>_weight_unc_g` | g, 1 sd |
| `<slug>_weight_lo_kg`, `<slug>_weight_hi_kg` | kg, ±2 sd |
| `<slug>_growth_g_week` | g/week or null |
| `<slug>_trend_word` | gaining / losing / steady / not enough data |
| `<slug>_last_seen` | ISO timestamp of the last labelled visit |
| `<slug>_stale` | bool: no labelled visit or accepted weigh-in for 72 h |
| `<slug>_stale_reason` | null (weight published), `not_seen`, `not_identified`, `sudden_change`, `unavailable` |
| `<slug>_visits_total`, `<slug>_visits_24h` | int |
| `<slug>_last_weigh_in` | text or null, e.g. `rejected 3.30 kg 2030-01-07T09:15: … enter it again changed by 0.01 kg (e.g. 3.31 for 3.30) within 24 h to confirm` |

Global:

| Key | Notes |
|---|---|
| cat_status | `ok`, `learning`, `cant_tell_apart`, `needs_weigh_in`; `unavailable` if the tracker failed |
| cat_gap | g or null: smallest gap between two published weights |
| labelled_pct_7d | % of the last 7 days' plateaus given a cat label |
| visits_24h_total | int |
| unknown_visits_total, two_cat_visits_total, correction_count | int |
| cluster_low_kg, cluster_high_kg, cluster_low_n, cluster_high_n | the label-free clusters (2+ cats), or null |
| scale_shift | null or `scale_shift <±g> g <time>` |
| readings_above_window_24h | int |
| last_cat | slug, `unknown`, `two_cats` or null |
| last_conf | int % or null |
| last_visit_cat | e.g. `Cat a 94%`, `Unknown`, `Cat a + Cat b` |

## `BASE/diag` keys

| Key | Notes |
|---|---|
| unmapped_bytes | hex string of the heartbeat bytes not covered by the field table |
| unmapped_map | JSON string, offset → value |
| slot_count | |
| unmapped_changed_at | ISO timestamp of the last change in an unmapped byte |
| unmapped_change_count | |
| last_unknown_frame | `{command, body_len, body_hex, at}` |
| last_unknown_command | |
| last_unusable_heartbeat | why the last rejected heartbeat was rejected |

## `recent_visits` rows

```json
{"time": "Jan 7 09:15", "ts": "2030-01-07T09:15:42+00:00", "peak": 3.21, "duration": "1m 42s",
 "weight": 3.05, "deposit": 40, "source": "box", "kind": "plateau", "n": 6, "pre": 1.48,
 "baseline_unc": 0.02, "cat": "cat_a", "conf": 94}
```

| Field | Notes |
|---|---|
| time | local time, display string |
| ts | ISO timestamp of the visit's end (rows of 0.1 have none) |
| peak | kg, the box's own visit report; null for a `gateway` row |
| peaks | two reports for one occupancy (two cats) |
| duration | display string: the box's reported duration, or the occupancy's for a `gateway` row |
| weight | kg, the plateau median minus the pre-visit baseline; null = not measured (no plateau, a tie, two cats, during a clean) |
| weights | two cats in one occupancy: both weights, in order |
| deposit | g; null = unknown (more than 300 g, or the visit was held open to the 10-minute cap) |
| source | `box` (the box reported it) or `gateway` (an occupancy the box never reported) |
| kind | `plateau`, `single` (one live reading), `two` (two plateaus) or `none` |
| n | readings in the weighed plateau (two numbers for two cats) |
| pre, baseline_unc | the pre-visit empty baseline and the spread of the readings it came from, kg |
| clamped, during_clean, held, tie, baseline_alt, upgraded_from, segments_merged, baseline_ambiguous | present only when they apply (see DESIGN.md) |
| cat | slug, `unknown` or `two_cats`; only with `CATLINK_CATS` |
| cats | the two slugs of a `two_cats` row |
| conf | int % (at most 99) or null; only with `CATLINK_CATS` |

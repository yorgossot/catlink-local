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
only on numeric readings: the weights, the deposit, the live scale and the Wi-Fi signal; never on counters
or text.

## MQTT topics

| Topic | Retained | Content |
|---|---|---|
| `BASE/availability` | yes | `online` / `offline`; also the gateway's MQTT last will. Goes `offline` when no fresh heartbeat for 300 s (`OFFLINE_AFTER_S`) |
| `BASE/state` | yes | JSON, keys below |
| `BASE/diag` | yes | JSON, keys below |
| `BASE/visits` | yes | `{"recent_visits":[…]}`, newest first, at most 15 |
| `BASE/<object_id>/set` | no | commands from HA. A retained message on these topics is dropped, never acted on |
| `PREFIX/<component>/NODE/<object_id>/config` | yes | discovery |
| `BASE/correct_last/set` | no | gateway-internal, payload a cat slug (only with `CATLINK_CATS`) |
| `BASE/set_ref_<slug>/set` | no | gateway-internal, payload kg (only with `CATLINK_CATS`) |

The two gateway-internal topics are handled inside the gateway and never reach the box. They are
subscribed only when `CATLINK_CATS` is set, in both modes.

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
| sensor | slot_count | Schedule slot count | `sensor.catlink_open_x_schedule_slot_count` | diagnostic |
| sensor | unmapped_bytes | Unmapped heartbeat bytes | `sensor.catlink_open_x_unmapped_heartbeat_bytes` | diagnostic, from `BASE/diag` |
| sensor | unmapped_changed_at | Unmapped bytes changed | `sensor.catlink_open_x_unmapped_bytes_changed` | diagnostic, from `BASE/diag` |
| sensor | unmapped_change_count | Unmapped change count | `sensor.catlink_open_x_unmapped_change_count` | diagnostic, from `BASE/diag` |
| sensor | last_unknown_command | Last unknown frame | `sensor.catlink_open_x_last_unknown_frame` | diagnostic, from `BASE/diag` |

`last_visit_weight` first shows the box's own (max-hold) value, then the settled estimate once the visit
is finalised. See [PROTOCOL.md](PROTOCOL.md) §5.

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

Shown for slug `cat_a`; one set per configured cat. The display name is the slug with `_` → space,
first letter capitalised (`cat_a` → "Cat a").

| Component | object_id | Name | Entity id (default device name) | Class / unit / category |
|---|---|---|---|---|
| sensor | `<slug>_weight` | `<Display> weight` | `sensor.catlink_open_x_cat_a_weight` | weight, kg, measurement; json_attributes = `BASE/state` |
| sensor | `<slug>_trend_7d` | `<Display> trend 7d` | `sensor.catlink_open_x_cat_a_trend_7d` | g, diagnostic |
| sensor | cat_gap (2 cats only) | Cat gap | `sensor.catlink_open_x_cat_gap` | g, diagnostic |
| sensor | last_visit_cat | Last visit cat | `sensor.catlink_open_x_last_visit_cat` | text such as `Cat a 82%`; json_attributes = `BASE/state` |
| sensor | labelled_pct | Labelled pct | `sensor.catlink_open_x_labelled_pct` | %, diagnostic |
| sensor | correction_count | Correction count | `sensor.catlink_open_x_correction_count` | diagnostic |

Because the weight sensors carry `BASE/state` as attributes, a dashboard can read
`state_attr('sensor.catlink_open_x_cat_a_weight', 'cat_a_trend_word')`.

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
| slot_count | int | |
| bin_full | bool | |
| drawer_out | bool | true = out |
| wifi_signal | int | dBm |
| box_clock | string | `HH:MM`, the box's own (unset) clock |
| last_visit_weight | float | kg |
| last_visit_duration | string | `m:ss` |
| last_visit_seconds | int | |
| last_visit_at | string | ISO timestamp |
| last_deposit_weight | int | g |

With cats configured, also:

| Key | Notes |
|---|---|
| `<slug>_weight` | kg, median of recent confident visits |
| `<slug>_trend_7d` | g, last 7 days vs the 7 before |
| `<slug>_trend_word` | gaining / losing / steady |
| cat_gap | g (2 cats) |
| labelled_pct | % of visits given a cat label |
| correction_count | |
| cat_degraded | bool (2 cats); references too close to tell apart |
| last_cat | slug or `unknown` |
| last_conf | int % |
| last_visit_cat | e.g. `Cat a 82%` |

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
{"time": "Jan 7 09:15", "peak": 3.21, "duration": "1m 42s", "weight": 3.05, "deposit": 40,
 "cat": "cat_a", "conf": 82}
```

| Field | Notes |
|---|---|
| time | local time, display string |
| peak | kg, the box's max-hold visit weight |
| duration | display string |
| weight | kg, settled estimate |
| deposit | g |
| cat | slug or `unknown`; only with `CATLINK_CATS` |
| conf | int %; only with `CATLINK_CATS` |

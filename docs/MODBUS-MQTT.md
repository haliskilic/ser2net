# MQTT, Modbus points and Sparkplug B: reference

What a mapping publishes and takes on its MQTT broker, how Modbus points are read and
written, and how a Modbus gateway looks as a Sparkplug B edge node. The README's
*Features* list explains the behaviour around it (unit health, write queue, limits on
writes); this page is the lookup table. MQTT needs the optional `paho-mqtt` package
(bundled in the standalone executables and the Docker image).

A mapping has one MQTT connection to one broker (**MQTT publishing** in the mapping form,
`mqtt` in `config.json` and the REST API). What goes over it depends on the mapping:

| Mapping | Payload format | What it publishes |
|---|---|---|
| raw / telnet / RFC 2217 (serial ↔ network) | plain | the device's lines; optionally takes writes on `<topic>/tx` |
| Modbus gateway with poll points (*edge mode*) | plain | one topic per point, commands on `<point>/set` |
| Modbus gateway with poll points | Sparkplug B | an edge node, one device per Modbus unit |

`<topic>` below is the mapping's **Base topic** (`mqtt.base_topic`). A serial ↔ serial
bridge has no MQTT, and nor does a Modbus gateway without poll points (both are logged as
settings that do nothing).

## Plain topics

| Topic | Payload | Retained | QoS |
|---|---|---|---|
| `<topic>/status` | `online` after every connect; `offline` as the Last Will and when the mapping stops | yes | the mapping's |
| `<topic>` (serial mappings) | one line of what the device sent, without its line end (**Line end**: LF, CR or either; empty lines are not sent) | with **Retain the data** | the mapping's |
| `<topic>/tx` (serial, with **Write messages on `<topic>/tx`**) | taken: written to the serial port as it is, in one piece between TCP clients' data | — (a retained copy is ignored) | — |
| `<topic>/tx/result` | `ok` once the port took it, or `error: …` (read-only, too long, busy, the port not open) | no | the mapping's |
| `<topic>/<point>` (Modbus) | the value read, at every poll (see *Values*) | with **Retain the data** | the mapping's |
| `<topic>/<point>/set` (writable points) | taken: a value to write (see *Writes*) | — (a retained copy is ignored unless **Re-apply a retained /set after every connect**) | subscribed at 1 |
| `<topic>/<point>/set/result` | the answer to each `/set`, in the order they came | no | 1, or the mapping's if higher |
| `<topic>/unit<N>/status` (with **Publish each unit's state**) | `online` / `offline` for each polled unit, on every change and after each connect | yes | the mapping's |

A unit goes `offline` when it gave no reply to any of its points in 3 polls in a row (or
its serial port stayed gone that long); it keeps the last state while the mapping is
stopped, so read it together with `<topic>/status`.

Rules an edit checks (form, REST API, import, fleet edit): the base topic and point names
can't hold `+`, `#` or a null character; no point may be named `status` or so that its
topic is another point's `/set` or `/set/result` (or, with unit states on, `unit<N>/status`);
two enabled mappings on one broker can't share a command topic, a client ID or a Sparkplug
B edge node. A stored mapping that breaks one still loads, with a warning at start-up.

## Modbus points

Each point is one object in the mapping form's points JSON (or a row with **Edit the
points as fields**):

| Key | Meaning | Default |
|---|---|---|
| `name` | the point's name: its topic below `<topic>`, its Sparkplug metric | required |
| `unit` | the RTU slave address, 1–247 (0 = broadcast: written, never read; 248–255, which a serial line reserves, are warned about) | 1 |
| `fn` | 1 coils, 2 discrete inputs, 3 holding registers, 4 input registers | 3 |
| `address` | the first coil / register, **0-based** as on the wire (register 40001 is `"fn": 3, "address": 0`) | 0 |
| `dtype` | `uint16`, `int16` (1 register); `uint32`, `int32`, `float32` (2); `uint64`, `int64`, `float64` (4); `string` (`length` registers); `bool` (a coil, a discrete input, or one `bit` of a register) | `uint16` |
| `word_order` | how the device lays out a value's bytes: `ABCD` (high register first, the Modbus convention), `CDAB` (low register first: the "word swap" of many meters), `BADC`, `DCBA`; a string: `ABCD` or `BADC` | `ABCD` |
| `bit` | with `"dtype": "bool"` on fn 3 or 4: which bit, 0 (least significant) to 15 | — |
| `length` | a `string`'s registers, 1–125 (two characters each; it ends at a NUL, trailing spaces are dropped) | — |
| `scale` | published value = raw × scale (0 or 1: not scaled) | 1 |
| `writable` | MQTT may write it (coils and holding registers; not a string, not a bit) | false |
| `min`, `max`, `allowed` | what MQTT may write (in the point's units, as published); a write outside them is refused | no limit |
| `poll` | `false`: a write-only point (a command register), never read, a write not read back | true |

```json
[
  {"name": "temperature", "unit": 1, "fn": 4, "address": 0, "dtype": "float32", "word_order": "CDAB"},
  {"name": "energy_wh",   "unit": 1, "fn": 3, "address": 100, "dtype": "uint64"},
  {"name": "alarm",       "unit": 1, "fn": 3, "address": 20, "dtype": "bool", "bit": 3},
  {"name": "setpoint",    "unit": 2, "fn": 3, "address": 10, "dtype": "int16", "scale": 0.1,
   "writable": true, "min": 5, "max": 80},
  {"name": "pump",        "unit": 2, "fn": 1, "address": 0, "dtype": "bool", "writable": true}
]
```

**Read these points now** (in the form of a running gateway) reads the points as typed,
without saving or publishing them. **Read neighbouring points with one request** (block
reads, off by default) reads a unit's points of one function that lie close together with
one request (at most 125 registers or 2000 coils, with up to the given number of unused
registers between two points).

## Values

| `mqtt.value_format` | Payload of `<topic>/<point>` |
|---|---|
| `text` (default) | the value alone: `1` / `0` for a bit, an integer as digits, a float as Python writes it (`21.5`; a scale can give `1.2000000000000002`), a string as it is |
| `json` | `{"value": 21.5, "ts": 1767225600000, "quality": "good"}`: `ts` is the read time in ms since the epoch, a bit is `true` / `false`, NaN or infinity is `null` |

A read that fails publishes nothing with `text`; with `json`, the point (and every point of
a unit that went offline) is published once with `"value": null, "quality": "bad"`, then
again when it reads.

## Writes

A message on `<topic>/<point>/set` writes that one point, and its answer goes to
`<topic>/<point>/set/result`:

| Point | Payload | Modbus request |
|---|---|---|
| coil (fn 1) | `1` / `0`, `true` / `false`, `on` / `off` | FC5 |
| holding register, 16-bit type | a number: `42`, `-3`, `0x2A`, `21.5`, `2.15e1` (an integer type rounds a fraction, ties to even) | FC6 |
| holding register, 32- or 64-bit type | a number, encoded as the point's type and word order | FC16 (its own registers) |
| JSON request | `{"value": 21.5, "id": "abc"}` (`id`: optional, text of up to 64 characters or an integer) | as above |

| Answer | When |
|---|---|
| `ok` | written; the point is read back and published |
| `error: …` | refused or failed: `mapping is read-only`, `busy` (32 writes wait already), `expired: …` (waited more than 10 s for the bus), `too large: …` (over 64 KiB), outside `min` / `max` / `allowed`, a slave exception, `no reply from unit N within T s` |
| `{"id": "abc", "ok": true, "value": 21.5}` | a JSON request that was written (`value`: read back, `null` if it can't be) |
| `{"id": "abc", "ok": false, "error": "…"}` | a JSON request that wasn't |

The value is in the point's own units: with `"scale": 0.1`, `/set` `21.5` writes the raw
register value 215. A JSON list or any other text is refused (`error: not a number: …`): a
command writes the point it was sent to, nothing more. A write to a point on unit 0 is a
broadcast to every slave and is not read back. Writes are made one at a time, in the order
they came, between the polls and the Modbus/TCP masters' requests.

## Sparkplug B

With **Payload format** *Sparkplug B (edge node)* the gateway is an edge node (Sparkplug
3.0 topic namespace `spBv1.0`); the base topic is not used for its data, but a mapping
with MQTT on still needs one.

| Sparkplug | ser2net |
|---|---|
| group ID | **Sparkplug group ID** (`mqtt.sparkplug_group_id`), default `ser2net` |
| edge node ID | **Sparkplug edge node ID**, or the mapping's name with `/`, `+`, `#`, spaces and control characters made `_` |
| device | one per Modbus unit with poll points: `unit<N>` (`unit1`, …) |
| metric | a point, by its name (no aliases) |
| datatype | `uint16` UInt16, `int16` Int16, `uint32` UInt32, `int32` Int32, `uint64` UInt64, `int64` Int64, `float32` Float, `float64` Double, `string` String, `bool` Boolean; a scaled number (scale other than 0 or 1) is Double |
| node metrics | `bdSeq` (Int64) and `Node Control/Rebirth` (Boolean) |

| Topic | What |
|---|---|
| `spBv1.0/<group>/NBIRTH/<node>` | after every connect, with `bdSeq` (seq 0) |
| `spBv1.0/<group>/NDEATH/<node>` | the Last Will (QoS 1), and when the mapping stops |
| `spBv1.0/<group>/DBIRTH/<node>/unit<N>` | after the NBIRTH, every point of the unit (last value or null); again when an offline unit answers |
| `spBv1.0/<group>/DDATA/<node>/unit<N>` | each poll, the points whose value changed (report by exception), QoS 0, each stamped with its read time |
| `spBv1.0/<group>/DDEATH/<node>/unit<N>` | a unit that stopped answering (3 polls) |
| `spBv1.0/<group>/DCMD/<node>/unit<N>` | taken: a metric named like a writable point of that unit is written; the point is read back and sent in a DDATA, changed or not |
| `spBv1.0/<group>/NCMD/<node>` | taken: `Node Control/Rebirth` = true sends the births again (at most every 2 s) |
| `spBv1.0/STATE/<host id>` (and the older `STATE/<host id>`) | with **Sparkplug primary host ID**: births only while that host says online |

`bdSeq` counts on across restarts (`data/sparkplug-bdseq.json`). Sparkplug data and
commands are never retained; a retained NCMD / DCMD is ignored.

## A Modbus gateway over the REST API

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://ser2net.example.com:8080/api/v1/mappings -d '{
  "name": "line-3-plc",
  "serial": {"port": "/dev/ttyUSB0", "baudrate": 19200, "parity": "E"},
  "network": {"mode": "server", "protocol": "modbus", "bind_ip": "0.0.0.0", "port": 1502},
  "options": {"modbus_response_timeout_s": 0.5, "modbus_unit_map": {"255": 1}},
  "mqtt": {"enabled": true, "host": "broker.example.com", "port": 8883, "tls": true,
           "username": "line3", "password": "change-me", "base_topic": "plant/line3",
           "qos": 1, "value_format": "json", "unit_status": true},
  "modbus_poll": {"interval_s": 5, "points": [
    {"name": "temperature", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16",
     "scale": 0.1, "writable": true, "min": 5, "max": 80}]}
}'
```

For Sparkplug B add `"payload_format": "sparkplug_b"` (and optionally
`"sparkplug_group_id"`, `"sparkplug_node_id"`, `"sparkplug_primary_host_id"`) to `mqtt`.
`GET /api/v1/openapi.json` lists every field with its default; `GET /api/v1/status` gives
each point's last value, when it was read and why its last read failed, each unit's state
and the MQTT connection's state.

## Limits

- One broker per mapping, no broker list to walk; nothing is stored while the broker or
  the Sparkplug primary host is away (no store and forward), and a command published
  while ser2net is not connected is not delivered later.
- Plain values are published at every poll, changed or not; only Sparkplug B reports by
  exception.
- A text float is Python's shortest repr of the scaled value, not rounded to the device's
  precision (`1.2000000000000002`): round it in the consumer, or use an unscaled point.
- ser2net writes no safe value by itself when the broker or the host goes away: use a
  heartbeat register and the PLC's own watchdog (README → *MQTT commands*).
- `min`, `max` and `allowed` limit MQTT only, not Modbus/TCP masters.
- Strings and register bits are read-only; a write goes to a single point (FC5, FC6 or
  FC16), never to a range.
- Who may write is up to the broker: allow only trusted clients to publish to
  `<topic>/+/set`, `<topic>/tx` and the node's NCMD / DCMD topics, and use TLS and a
  username (the dashboard marks a mapping that takes MQTT writes with **CMD**).
- A 2.8 node (a fleet edit from one) or going back to 2.8 drops `word_order`, `bit`,
  `length`, `min`, `max`, `allowed`, `poll`, the block reads, the unit map, the JSON
  values, the unit states and the MQTT TLS files, and 2.8 can't poll the new types.

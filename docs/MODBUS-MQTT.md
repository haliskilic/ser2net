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

## MQTT version

The connection speaks MQTT 3.1.1 unless the mapping's **MQTT version** (`mqtt.protocol`) is
`"5"`. Topics, payloads and commands are the same with both; MQTT 5 adds:

| Setting | `config.json` | What it does | Default |
|---|---|---|---|
| **Clean start** | `mqtt.clean_start` | on: the broker begins a new session at every connect. Off (needs a **Client ID** and a session expiry to be of use): the broker keeps the session — the subscriptions and the QoS 1 messages for them — while the connection is away | on |
| **Session expiry (s)** | `mqtt.session_expiry_s` | how long the broker keeps the session after the connection ends; 0: not at all | 0 |
| **Message expiry of retained data (s)** | `mqtt.message_expiry_s` | retained lines and values (**Retain the data**) carry this message expiry: the broker stops handing one out that long after it was published, so a subscriber that connects later does not take a stale value for a current one. Not on `<topic>/status`, the units' states or command answers. 0: never | 0 |

- With clean start off, a `/set` (or a `<topic>/tx`) that another client published at QoS 1
  while the gateway was away is delivered, and written, when it connects again — up to
  the session expiry later. Leave clean start on unless that is what you want; a publisher
  can bound it with a message expiry of its own.
- The broker's reason codes are named in the mapping's log and kept in its status
  (`status.mqtt`): `reason_code` is the code of the CONNACK that refused the connection or
  of the DISCONNECT the broker ended it with (`Not authorized (reason code 0x87)`,
  `Session taken over (reason code 0x8E)`), a refused subscription is logged with its
  reason, and `rejected` counts the QoS 1/2 publications the broker answered with a
  failure (not authorized, quota exceeded; each reason is logged once per connection).
- Sparkplug B on MQTT 5 always connects with clean start and session expiry 0, as the
  Sparkplug 3.0 specification asks; the two settings are then not used, and nothing is
  retained, so nor is the message expiry.
- paho-mqtt 1.6 speaks MQTT 5 too, but does not report why the broker refused a
  publication or sent a DISCONNECT (the log then says the connection was lost); paho-mqtt
  2.x does. MQTT 5's request / response properties (response topic, correlation data),
  topic aliases and shared subscriptions are not used.
- The three settings do nothing with MQTT 3.1.1 (the form and the log say so).

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
| `pulse_ms` | a writable coil: a write of true is followed by a write of false this many milliseconds later, 50–600000 (see *Pulses*) | — |
| `safe_value`, `safe_after_s` | a writable point: the value written once the broker (or the Sparkplug primary host) has been away for that many seconds, 1–86400 (see *Safe values*) | — |

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

## Pulses

A writable coil with `"pulse_ms": 500` is a push button: a write of true (a `/set`, a
Sparkplug DCMD) is followed, 500 ms after the slave acknowledged it, by a write of false.

```json
{"name": "start", "unit": 2, "fn": 1, "address": 8, "dtype": "bool", "writable": true, "pulse_ms": 500}
```

- The second write is a request of its own (FC5), which takes its turn on the bus like any
  other: polls, other writes and the Modbus/TCP masters go on during the pulse, and on a
  busy or slow line the coil stays on for the pulse plus the wait for the bus.
- The `/set` is answered (`ok`) and the coil read back and published (`1`) when it is
  switched on; the value is published again (`0`) once it is off.
- A second true during the pulse starts it over (one false follows, a whole pulse after
  it, also when the first pulse ends while the second true is on the bus); a false ends
  it.
- A mapping that stops, restarts or is saved during a pulse writes false at once, before
  it closes the serial port. A write of true whose reply did not come (but whose request
  went out) is followed by false all the same.
- A false that can't be written (the unit does not answer, the serial port is down) is
  tried again: three times half a second apart, then every 10 s until it is written (the
  log says after the third try that the coil may still be ON, and when it is written). A
  stop gives it three more tries, half a second apart (at most 10 s for all the pulses
  under way), and the log names a coil that may still be ON.
- It can't help when the service is killed or the machine loses power during a pulse:
  where that matters, let the device time the pulse itself.
- Modbus/TCP masters writing the coil are not affected, like the write limits.

## Safe values

A writable point with `safe_value` and `safe_after_s` is written to that value when
nobody can command it any more:

```json
{"name": "setpoint", "unit": 2, "fn": 3, "address": 10, "dtype": "int16", "scale": 0.1,
 "writable": true, "min": 5, "max": 80, "safe_value": 20, "safe_after_s": 60},
{"name": "pump", "unit": 2, "fn": 1, "address": 0, "dtype": "bool", "writable": true,
 "safe_value": false, "safe_after_s": 30}
```

- "Nobody" is the broker connection: once it has been lost for `safe_after_s` seconds, the
  point is written. A Sparkplug B node with a **primary host ID** goes by that host
  instead: its STATE saying offline, or the broker connection lost, for that long. A
  connection that breaks without a word (a cable pulled, a router gone) is noticed by the
  MQTT keepalive (30 s), up to a minute later; the time counts from then.
- An outage counts from its first loss. A connection that comes back ends it once it has
  stayed up for 10 s (or for the longest `safe_after_s` of the mapping's points, when
  that is less); a broker that takes the client and drops it again sooner (another client
  with the same client ID, an authorization plugin, a broker in a crash loop) is away all
  along. No safe value is written while the connection is up: a point whose time comes
  then is written when it drops again.
- The time also runs from the mapping's start: a gateway that starts (or is saved, which
  restarts it) and does not reach its broker, or does not see its primary host online,
  within `safe_after_s` writes the safe values.
- Each point is written once per outage, in its turn in the write queue (read back like
  any write). The value has to be one the point takes: of its type and range (after the
  scale) and within its `min` / `max` / `allowed` (an edit refuses another; a stored one is
  not used, with a warning in the log at start). A write that fails (the unit does
  not answer, the port is down) is tried again every 10 s until it works or the broker is
  back (for good, see above). Back in time: nothing is written. Afterwards the point keeps the safe value until
  a command changes it.
- It is logged (`wrote 'setpoint' = 20 (… from its safe value: no broker for 60 s)`), shown
  on the dashboard and in the status (`status.safe`: `state` ok / waiting / applied,
  `since`, and per point `written`, `error`), and the notification **Safe values written**
  (`safe_values`, off unless ticked in Settings → Notifications) reports it.
- A read-only mapping never writes one (its log says so at start).
- A coil with `pulse_ms` can only have the safe value false.
- What it can't see: a broker that is connected while the application behind it is not
  (plain MQTT has no such signal; Sparkplug's primary host does), and ser2netweb itself
  being stopped or without power. For those, keep a heartbeat register and the PLC's own
  watchdog (README → *MQTT commands*).

## Sparkplug B

With **Payload format** *Sparkplug B (edge node)* the gateway is an edge node (Sparkplug
3.0 topic namespace `spBv1.0`); the base topic is not used for its data, but a mapping
with MQTT on still needs one.

| Sparkplug | ser2netweb |
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
  http://ser2netweb.example.com:8080/api/v1/mappings -d '{
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
For MQTT 5 add `"protocol": "5"` (and optionally `"clean_start"`, `"session_expiry_s"`,
`"message_expiry_s"`).
`GET /api/v1/openapi.json` lists every field with its default; `GET /api/v1/status` gives
each point's last value, when it was read and why its last read failed, each unit's state,
the MQTT connection's state and the safe values' (`safe`).

## Limits

- One broker per mapping, no broker list to walk; nothing is stored while the broker or
  the Sparkplug primary host is away (no store and forward), and a command published
  while ser2netweb is not connected is not delivered later.
- Plain values are published at every poll, changed or not; only Sparkplug B reports by
  exception.
- A text float is Python's shortest repr of the scaled value, not rounded to the device's
  precision (`1.2000000000000002`): round it in the consumer, or use an unscaled point.
- A safe value is written only for a point that has one (*Safe values*), and only while
  ser2netweb runs: for anything that must hold when the gateway itself is gone, use a
  heartbeat register and the PLC's own watchdog (README → *MQTT commands*).
- `min`, `max`, `allowed` and `pulse_ms` apply to MQTT only, not to Modbus/TCP masters.
- Strings and register bits are read-only; a write goes to a single point (FC5, FC6 or
  FC16), never to a range.
- Who may write is up to the broker: allow only trusted clients to publish to
  `<topic>/+/set`, `<topic>/tx` and the node's NCMD / DCMD topics, and use TLS and a
  username (the dashboard marks a mapping that takes MQTT writes with **CMD**).
- A 2.8 node (a fleet edit from one) or going back to 2.8 drops `word_order`, `bit`,
  `length`, `min`, `max`, `allowed`, `poll`, the block reads, the unit map, the JSON
  values, the unit states and the MQTT TLS files, and 2.8 can't poll the new types.
- A 3.0 node (a fleet edit from one keeps them; its own mappings can't have them) or
  going back to 3.0 drops `mqtt.protocol`, `clean_start`, `session_expiry_s`,
  `message_expiry_s` and the points' `pulse_ms`, `safe_value` and `safe_after_s`: the
  mapping then speaks MQTT 3.1.1, a pulsed coil stays on when it is written true, and no
  safe value is written.

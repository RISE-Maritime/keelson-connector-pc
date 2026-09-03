# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project Overview

A Keelson/Zenoh connector that monitors the computer it runs on — CPU, memory,
storage, network interfaces, temperatures, battery and processes — with
`psutil`, and publishes each quantity as its own Keelson subject carrying a
`keelson.Timestamped*` primitive. Cross-platform: Linux, macOS, Windows.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"

uv run pytest -m "not e2e"        # unit tests, no bus
uv run pytest -m e2e              # opens an isolated in-process Zenoh session
uv run pytest                     # everything

black bin keelson_connector_pc tests
pylint bin keelson_connector_pc

uv run bin/pc2keelson.py -r rise -e $(hostname) --interval 2 --log-level 10
docker compose up
```

## Architecture

Three files, split so that the psutil half and the Zenoh half can be tested
independently:

- [keelson_connector_pc/collectors.py](keelson_connector_pc/collectors.py) —
  samples the machine, returns `list[Reading]`. Imports neither zenoh nor
  keelson, which is what makes every platform quirk testable by monkeypatching
  `psutil`.
- [keelson_connector_pc/publishing.py](keelson_connector_pc/publishing.py) —
  turns readings into envelopes on keys. Imports no psutil.
- [bin/pc2keelson.py](bin/pc2keelson.py) — argparse, session, main loop. The
  only place the two halves meet.

`Reading(subject, source_suffix, value)` is the interface between them.

### Subjects are the contract

The payload type for a subject is **never** hard-coded in the publishing path.
It is looked up with `keelson.get_subject_schema()` and dispatched through
`publishing.ENCODERS`, which makes
[keelson_connector_pc/subjects.yaml](keelson_connector_pc/subjects.yaml) the
single source of truth. Adding a subject there is the only step needed for the
connector to publish it with the right type.

`collectors.EMITTED_SUBJECTS` declares the connector's full publishing
surface — capability, not activity, so a machine with no battery still
declares the battery subjects. `tests/test_subjects.py` holds it to the code by
walking collectors.py's AST for subject literals, so it cannot drift.

**These subjects must also land in `keelson/messages/subjects.yaml`.**
`construct_pubsub_key` only *warns* on an unknown subject, so an unregistered
key still reaches the bus — where `keelson2foxglove`
(`keelson2foxglove.py:368`) and `keelson2mcap` (`keelson2mcap.py:709`) drop it,
because they resolve a schema from their own copy of the registry. Registering
our bundled copy at startup fixes this process, not theirs.

## Key Design Decisions

- **One quantity per subject.** Per `protocol-specification.md` §2.2.1,
  separable scalars each get their own subject rather than being glued into a
  bundle. `source_id` carries the instance (`pc/disk/data`, `pc/net/eth0`).
- **Byte counts are `TimestampedInt64`, never `TimestampedInt`.** keelson's own
  `helpers.enclose_from_integer` builds a `TimestampedInt`, whose int32 value
  field silently overflows past 2 GiB — ordinary RAM. `test_subjects.py` pins
  this.
- **Missing beats wrong.** A platform that does not expose a metric gets no
  publish, not a zero. This is why `collect_sensors` checks
  `hasattr(psutil, "sensors_temperatures")` (undefined off Linux) rather than
  catching an exception, and why `MIN_PLAUSIBLE_CPU_MHZ` drops Apple Silicon's
  `scpufreq(current=4, ...)` GHz-shaped placeholder instead of claiming a 4 MHz
  CPU.
- **`memory_used_bytes` is `total - available`, not `psutil`'s `used`.** `used`
  excludes buffers and cache on Linux and means something different again on
  macOS; it is not comparable across the platforms this runs on.
- **Rates need two snapshots.** NIC bitrates and disk throughput are deltas, so
  the first cycle publishes cumulative counters but no rate. Deltas are clamped
  at zero because an interface that goes down and up restarts its counters.
- **`Process` objects are cached across cycles.** `Process.cpu_percent()`
  measures since the previous call *on that object*; rebuilding the objects
  each cycle would report 0.0 forever.
- **No `entity_health`.** Per `connectors/CLAUDE.md`, a connector publishes raw
  subjects and lets the `entity_health` aggregator apply health policy. Two
  emitters on one `entity_health` key race and flip-flop.
- **QoS is not hand-set.** `publishing.py` prefers
  `keelson.scaffolding.declare_publisher` (subject-driven QoS from
  `messages/qos.yaml`) and falls back to a bare declaration only because that
  helper landed in keelson 0.5.4, which was never published to PyPI.

## Dependency Constraints

PyPI's newest `keelson` is **0.5.3** — 0.5.4 was tagged but never released.
At 0.5.3 the SDK does **not** have:

- `scaffolding.declare_publisher` / subject-driven QoS → compat shim in
  `publishing.py`, which starts applying real profiles automatically once a
  newer SDK is installed.
- Three-tier liveliness (`declare_liveliness` with `pubsub_subjects=`) → the
  connector uses the deprecated `declare_liveliness_token`. **When a keelson
  with the three-tier API reaches PyPI, switch to it and feed it
  `EMITTED_SUBJECTS`** — a producing connector is meant to advertise one token
  per subject it can publish.
- A `zenoh_config` parameter on `create_zenoh_config` → the connector carries
  its own `--zenoh-config` flag, defaulting to `$ZENOH_CONFIG`. Do not drop it;
  it is how an operator applies access control the flags cannot express.

## Containerisation

A container sees its own namespaces, so a naive run reports the *container*.
`docker-compose.yml` sets `pid: host` and `network_mode: host` and bind-mounts
the host's `/proc` and root; `--procfs-path` points psutil at the former and
`--host-root` strips the bind-mount prefix from mountpoint labels. On macOS and
Windows Docker runs a Linux VM, so run the connector on the host there.

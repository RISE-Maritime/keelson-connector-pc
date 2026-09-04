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

uv run bin/pc2keelson.py -r rise -e $(hostname) --vitals-interval 1 --interval 2 --log-level 10
docker compose -f docker-compose.computer.yml up   # -f is required
```

## Architecture

Three modules, split so that the psutil half and the Zenoh half can be tested
independently:

- [keelson_connector_pc/collectors.py](keelson_connector_pc/collectors.py) —
  samples the machine, returns `list[Reading]`. Imports neither zenoh nor
  keelson, which is what makes every platform quirk testable by monkeypatching
  `psutil`.
- [keelson_connector_pc/publishing.py](keelson_connector_pc/publishing.py) —
  turns readings into envelopes on keys. Imports no psutil.
- [keelson_connector_pc/cli.py](keelson_connector_pc/cli.py) — argparse,
  session, main loop. The only place the two halves meet.

The entry point is the `pc2keelson` console script that setuptools generates
from `[project.scripts]`; that is what the container runs and what
`tests/conftest.py` imports. [bin/pc2keelson.py](bin/pc2keelson.py) is only a
checkout convenience so `python bin/pc2keelson.py` works before anything is
installed, and is deliberately not copied into the image.

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
- **Per-process readings are summed across processes sharing a name.** The key
  carries the name, so `pc/process/chrome` means "all of chrome". Publishing
  each process separately put several different values on one key in the same
  cycle and let arrival order pick the winner. Aggregation is on the
  *sanitised* name, so two raw names cannot collide after sanitising.
- **A chip name does not identify a device.** Two NVMe drives both report as
  chip `nvme` with a `Composite` and a `Sensor 1` reading each, at different
  temperatures, so `collectors.disambiguated_labels` appends the entry index to
  any label that repeats within a chip. Labels that are already unique
  (`coretemp`'s `Core N`) keep their clean name.
- **`process_count` comes from `psutil.pids()`, not from the `Process` cache.**
  The cache holds only what this uid may open; under `pid: host` as uid 10001
  that is a small minority. If the pid table cannot be read at all, nothing is
  published rather than a zero.
- **Four cadences, and each subject belongs to exactly one.** `--vitals-interval`
  (1 s) carries `cpu_load_pct` and `memory_used_pct`; `--interval` (5 s) the rest
  of the live metrics; `--process-interval` (30 s) the top-N ranking;
  `--info-interval` (60 s) host identity. Only those two subjects are fast
  because only they are both cheap (one `/proc/stat` and one `/proc/meminfo`
  read) and quick-moving — disk usage and chip temperature do not change within
  a second and cost a `statvfs()` per mountpoint and a full `/sys/class/hwmon`
  walk. The tiering exists because crowsnest treats a source with no sample in
  5 s as stale (`SubscriptionWorkerManager.jsx:61`) and computes 0 Hz for it.
- **`cpu_load_pct` has exactly one call site, deliberately.**
  `psutil.cpu_percent(interval=None)` measures since its previous call *anywhere
  in the process*, so publishing it from a second cadence too would silently
  shorten both windows — no exception, just a wrong number. It lives only in
  `Sampler.sample_vitals()`; `collect_cpu()` does not emit it. The `percpu=True`
  variant used for `cpu_core_load_pct` keeps a separate psutil baseline and is
  unaffected. `tests/test_collectors_unit.py::test_sample_does_not_emit_the_vitals_subjects`
  guards this.
- **`--vitals-interval` is the loop's base period.** Everything slower is a
  monotonic deadline off it, so a tier can only be a multiple of it, never
  finer; the flag is rejected if it exceeds `--interval`.
- **Ranking processes has its own cadence.** `--process-interval` (default 30 s)
  gates the top-N scan, which costs an `oneshot()` and three reads per pid and
  is the connector's most expensive operation; `process_count` stays on
  `--interval`. A longer window also measures `cpu_percent()` better, since it
  averages since the previous call on each object.
- **An unreadable mountpoint backs off, it is not blacklisted.** A macOS sealed
  volume never recovers and backs off to a 5-minute ceiling; an NFS export or
  external disk that was merely not ready at boot is picked up on the first
  probe that succeeds.
- **Liveliness is declared at the base `--source-id`, not per instance.**
  `declare_liveliness(..., pubsub_subjects=sorted(EMITTED_SUBJECTS))` yields one
  source token plus one token per subject, all keyed `.../pubsub/{subject}/pc`.
  Instances live *below* that (`pc/disk/root`, `pc/net/eth0`) and are discovered
  at runtime — the top-N process names change every scan — so per-instance
  tokens would mean retracting on data absence, which subject-level liveliness
  explicitly forbids. This matches the nmea connector, which declares at
  `ardusimple` while publishing to `ardusimple/RMC`.
- **No `entity_health`.** Per `connectors/CLAUDE.md`, a connector publishes raw
  subjects and lets the `entity_health` aggregator apply health policy. Two
  emitters on one `entity_health` key race and flip-flop.
- **QoS is not hand-set.** `publishing.py` prefers
  `keelson.scaffolding.declare_publisher` (subject-driven QoS from
  `messages/qos.yaml`) and falls back to a bare declaration only because that
  helper landed in keelson 0.5.4, which was never published to PyPI.

## Dependency Constraints

Pinned to `keelson>=0.6.0rc15` — the PEP440 spelling of the `0.6.0-pre.15` tag,
which is the same build the rest of the fleet runs
(`ghcr.io/rise-maritime/keelson:0.6.0-pre.15`). Do not drop back to 0.5.3: three
things this connector relies on arrived in 0.6.0, and each replaced a workaround
that has since been deleted.

- **Three-tier liveliness** (`declare_liveliness(..., pubsub_subjects=)`) —
  replaced the deprecated `declare_liveliness_token`, which built
  `{realm}/@v0/{entity}/pubsub/*/{source_id}` and so covered only the bare `pc`
  source, not the nested ones (`pc/disk/root`, `pc/net/eth0`) that carry most of
  this connector's keys.
- **`scaffolding.declare_publisher`** — subject-driven QoS from
  `messages/qos.yaml`, replacing a compat shim that took plain Zenoh defaults.
- **`create_zenoh_config(zenoh_config=)`** — the SDK now owns `--zenoh-config`
  (via `add_common_arguments`) and layers file-then-flags itself, replacing
  ~15 lines of hand-rolled JSON5 merging here. Note the flag's argparse default
  is now `None`, not `$ZENOH_CONFIG`: the environment fallback lives inside
  `create_zenoh_config`, so assert on behaviour rather than the parsed value.

## Containerisation

A container sees its own namespaces, so a naive run reports the *container*.
`docker-compose.computer.yml` sets `pid: host` and `network_mode: host` and bind-mounts
the host's `/proc` and root; `--procfs-path` points psutil at the former and
`--host-root` strips the bind-mount prefix from mountpoint labels. On macOS and
Windows Docker runs a Linux VM, so run the connector on the host there.

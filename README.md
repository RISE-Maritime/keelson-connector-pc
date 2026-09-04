# keelson-connector-pc

Publishes the health of the computer Keelson runs on — CPU, memory, storage,
network interfaces, temperatures, battery and processes — to the Keelson/Zenoh
bus as ordinary pub/sub telemetry.

Every Keelson deployment sits on a box: a logging PC, an onboard NUC, an ROV
topside. `entity_health` rolls up the health of *sources* and the
`network_manager` connector measures *link* quality, but nothing on the bus
says whether the machine underneath them is running out of disk, thrashing
swap, or overheating. This fills that gap.

Runs on **Linux, macOS and Windows**. Metrics a platform does not expose are
simply not published, rather than reported as zero — a missing CPU temperature
is honest, a 0.0 °C one is not.

Each quantity is its own subject carrying a `keelson.Timestamped*` primitive,
which is what lets `keelson2mcap` record it and `keelson2foxglove` plot it with
no special-casing.

## pc2keelson

```
usage: pc2keelson [-h] [--log-level LOG_LEVEL] [--mode {peer,client}] [--connect CONNECT]
                  [--listen LISTEN] [--zenoh-config ZENOH_CONFIG] -r REALM -e ENTITY_ID
                  [-s SOURCE_ID] [--vitals-interval VITALS_INTERVAL] [--interval INTERVAL]
                  [--info-interval INFO_INTERVAL] [--process-interval PROCESS_INTERVAL] [--no-cpu]
                  [--no-memory] [--no-disk] [--no-network] [--no-sensors] [--no-processes]
                  [--no-host-info] [--cpu-per-core] [--no-disk-io] [--disk-mountpoint PATH]
                  [--disk-fstype-exclude FSTYPES] [--processes-top-n N] [--procfs-path PATH]
                  [--host-root PATH]

Monitor this computer and publish its metrics to Keelson/Zenoh

options:
  -h, --help            show this help message and exit
  --log-level LOG_LEVEL
                        Logging level (default: INFO) (default: 20)
  --mode, -m {peer,client}
                        The Zenoh session mode. (default: None)
  --connect CONNECT     Endpoints to connect to. Example: tcp/localhost:7447 (default: None)
  --listen LISTEN       Endpoints to listen on. Example: tcp/0.0.0.0:7447 (default: None)
  --zenoh-config ZENOH_CONFIG
                        Path to a Zenoh configuration file (JSON5). Everything the flags above
                        cannot express — access control, QoS defaults, transport tuning — lives
                        here. --mode/--connect/--listen still win where they overlap. Falls back
                        to the ZENOH_CONFIG environment variable. (default: None)
  -r, --realm REALM     Realm/base path to publish under, ex. rise (default: None)
  -e, --entity-id ENTITY_ID
                        Unique id of the entity within the realm, ex. nuc01 (default: None)
  -s, --source-id SOURCE_ID
                        Source-id base; per-instance suffixes are appended to it, ex. pc/disk/data
                        (default: pc)
  --vitals-interval VITALS_INTERVAL
                        Seconds between samples of cpu_load_pct and memory_used_pct. These two are
                        a single cheap /proc read each and move fast enough to be worth a live
                        cadence; everything else rides --interval. Also the loop's base period, so
                        it must not exceed --interval (default: 1.0)
  --interval INTERVAL   Seconds between samples of the live metrics other than the vitals above
                        (default: 5.0)
  --info-interval INFO_INTERVAL
                        Seconds between publishes of host identity and uptime (default: 60.0)
  --process-interval PROCESS_INTERVAL
                        Seconds between per-process top-N scans. Ranking every process on the host
                        is the connector's most expensive operation, so it runs on its own slower
                        cadence; process_count still goes out every --interval (default: 30.0)
  --no-cpu              Do not publish CPU load, frequency or load average (default: True)
  --no-memory           Do not publish memory or swap (default: True)
  --no-disk             Do not publish storage usage or throughput (default: True)
  --no-network          Do not publish network interface counters (default: True)
  --no-sensors          Do not publish temperatures, fans or battery (default: True)
  --no-processes        Do not publish process count or per-process metrics (default: True)
  --no-host-info        Do not publish host identity or uptime (default: True)
  --cpu-per-core        Also publish cpu_core_load_pct per logical core (one extra key per core)
                        (default: False)
  --no-disk-io          Do not publish per-device disk read/write throughput (default: True)
  --disk-mountpoint PATH
                        Report only this mountpoint; repeatable. Default is every real filesystem
                        psutil reports (default: None)
  --disk-fstype-exclude FSTYPES
                        Comma-separated filesystem types to skip when auto-detecting mountpoints
                        (default: autofs,binfmt_misc,bpf,cgroup,cgroup2,configfs,debugfs,devfs,dev
                        pts,devtmpfs,fusectl,hugetlbfs,mqueue,overlay,proc,pstore,ramfs,securityfs
                        ,squashfs,sysfs,tmpfs,tracefs)
  --processes-top-n N   Publish the N process names using the most CPU, summed across every
                        process sharing a name (0 = only publish process_count) (default: 5)
  --procfs-path PATH    Read /proc from here instead (Linux). Set this to the host's /proc when
                        running in a container, ex. /host/proc (default: None)
  --host-root PATH      Bind-mount prefix to strip from mountpoint labels so a containerised run
                        names host paths as the host does, ex. /host/root (default: None)
```

### Examples

After `uv pip install -e .` the `pc2keelson` console script is on PATH; from a
bare checkout, `uv run bin/pc2keelson.py` takes the same arguments.

```bash
# Monitor this machine, publishing every 5 seconds
pc2keelson -r rise -e nuc01

# Only the things a storage alarm needs, at a slower cadence
pc2keelson -r rise -e nuc01 \
    --no-processes --no-sensors --no-network --interval 30

# Two specific filesystems, per-core CPU, via an explicit router
pc2keelson -r rise -e nuc01 \
    --disk-mountpoint / --disk-mountpoint /data \
    --cpu-per-core --mode client --connect tcp/192.168.1.10:7447
```

```bash
# In Docker — see docker-compose.computer.yml for why the mounts and
# namespaces matter. The -f is required: the file is not named
# docker-compose.yml, so compose will not pick it up on its own.
docker compose -f docker-compose.computer.yml up
```

### Key expressions

```
{realm}/@v0/{entity_id}/pubsub/{subject}/{source_id}
```

The source-id is `--source-id` (default `pc`) plus a suffix naming the instance
the reading came from, so one host's many disks and NICs stay distinguishable:

| Reading | Source-id | Example key |
|---|---|---|
| Whole host | `pc` | `rise/@v0/nuc01/pubsub/cpu_load_pct/pc` |
| Per core | `pc/core/<n>` | `rise/@v0/nuc01/pubsub/cpu_core_load_pct/pc/core/3` |
| Per mountpoint | `pc/disk/<mount>` | `rise/@v0/nuc01/pubsub/disk_used_pct/pc/disk/data` |
| Per block device | `pc/disk/<device>` | `rise/@v0/nuc01/pubsub/disk_read_bytes_per_second/pc/disk/nvme0n1` |
| Per interface | `pc/net/<ifname>` | `rise/@v0/nuc01/pubsub/network_interface_up/pc/net/eth0` |
| Per sensor | `pc/sensor/<chip>/<label>` | `rise/@v0/nuc01/pubsub/cpu_temperature_celsius/pc/sensor/coretemp/package_id_0` |
| Per fan | `pc/fan/<chip>/<label>` | `rise/@v0/nuc01/pubsub/fan_rate_rpm/pc/fan/dell_smm/processor_fan` |
| Battery | `pc/battery` | `rise/@v0/nuc01/pubsub/battery_state_of_charge_pct/pc/battery` |
| Per process name | `pc/process/<name>` | `rise/@v0/nuc01/pubsub/process_cpu_load_pct/pc/process/python` |

Mountpoints, interface and process names are reduced to safe key chunks: `/`
becomes `root`, `/mnt/data` becomes `mnt_data`, and on Windows `C:\` becomes
`c`. Characters Zenoh treats as pattern syntax (`* ? $ #`) never reach a key.

A chip name does not identify a device either: a host with two NVMe drives
reports both as chip `nvme` with the same `Composite` and `Sensor 1` labels, so
a label that repeats within a chip has its index appended
(`pc/sensor/nvme/composite_0`, `pc/sensor/nvme/composite_2`). Labels that are
already unique keep their clean name.

Because the key carries the process *name*, per-process readings are summed
across every process sharing one: `pc/process/python` is all the pythons, not
whichever one happened to be measured last. The `--processes-top-n` names with
the highest total CPU are published.

### Cadences

Work is tiered by cost, so the expensive things do not set the pace:

| Flag | Default | Covers |
|---|---|---|
| `--vitals-interval` | 1 s | `cpu_load_pct`, `memory_used_pct` |
| `--interval` | 5 s | every other live metric, and `process_count` |
| `--process-interval` | 30 s | the per-process top-N ranking |
| `--info-interval` | 60 s | host identity and uptime |

Only the two vitals are fast, because only they are both cheap — one
`/proc/stat` read and one `/proc/meminfo` read — and quick-moving. Disk usage
and chip temperature stay on `--interval`: neither changes meaningfully within
a second, and sampling them costs a `statvfs()` per mountpoint and a full
`/sys/class/hwmon` walk. Ranking every process is by far the most expensive
thing the connector does, so it is slower still.

Each subject belongs to exactly one tier — nothing is published twice. That is
load-bearing for the vitals: `psutil.cpu_percent()` measures since its previous
call *anywhere in the process*, so a second call site on another cadence would
silently shorten both windows.

`--vitals-interval` is also the loop's base period, so it must not exceed
`--interval`.

## Subjects

42 subjects, all carrying existing `keelson.Timestamped*` primitives — no new
`.proto` was needed. Five further subjects are reused from keelson rather than
reinvented: `device_uptime_duration`,
`integrated_circuit_temperature_celsius`, `battery_state_of_charge_pct`,
`battery_is_charging` and `battery_time_remaining_s`.

| Group | Subjects |
|---|---|
| Host identity | `host_name`, `host_operating_system`, `host_operating_system_version`, `host_kernel_version`, `host_architecture`, `host_boot_time` |
| CPU | `cpu_model`, `cpu_core_count`, `cpu_thread_count`, `cpu_load_pct`, `cpu_core_load_pct`, `cpu_frequency_mhz`, `cpu_temperature_celsius`, `cpu_load_average_{1,5,15}min` |
| Memory | `memory_{total,used,available}_bytes`, `memory_used_pct`, `swap_{total,used}_bytes`, `swap_used_pct` |
| Storage | `disk_{total,used,free}_bytes`, `disk_used_pct`, `disk_{read,write}_bytes_per_second` |
| Network | `network_interface_up`, `network_interface_speed_mbps`, `network_interface_{rx,tx}_bitrate_bps`, `network_interface_{rx,tx}_bytes_total`, `network_interface_{error,drop}_count` |
| Cooling | `fan_rate_rpm` |
| Processes | `process_count`, `process_cpu_load_pct`, `process_memory_used_{pct,bytes}` |

The authoritative list is [`keelson_connector_pc/subjects.yaml`](keelson_connector_pc/subjects.yaml).
It is registered with the SDK at startup via
`keelson.add_well_known_subjects_and_proto_definitions`, so the connector
resolves its own schemas even on an SDK release that predates them.

> **These subjects are not in a released keelson yet, and nothing works
> downstream until they are.** `keelson2foxglove` and `keelson2mcap` resolve
> schemas from *their own* copy of the registry and silently drop what they
> cannot resolve, so today every reading this connector publishes reaches the
> bus and is then thrown away — nothing is recorded, nothing plots. Registering
> the bundled copy at startup fixes this process only.
>
> Registration is open as
> [keelson#240](https://github.com/RISE-Maritime/keelson/pull/240) (branch
> `feat/host-metric-subjects` → `dev`), which adds all 42 to
> `messages/subjects.yaml`. It needs no `qos.yaml` entries — that file is
> sparse, and anything unlisted inherits the `default` profile, which is the
> right stance for routine measurement telemetry. Both the merge **and** a
> subsequent keelson release are required before anything downstream works.

## Running in a container

A container sees its own namespaces. Left alone, the connector would
faithfully report the *container's* CPU share, process list and overlay
filesystem. `docker-compose.computer.yml` sets `pid: host` and `network_mode: host` and
bind-mounts the host's `/proc` and root, which `--procfs-path` and
`--host-root` then point the connector at.

On macOS and Windows, Docker runs a Linux VM, so a containerised connector
reports that VM and not your machine. Run it directly on the host there.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest -m "not e2e"    # unit tests
uv run pytest -m e2e          # opens a real (isolated) Zenoh session
black bin keelson_connector_pc tests && pylint bin keelson_connector_pc
```

## License

Apache-2.0

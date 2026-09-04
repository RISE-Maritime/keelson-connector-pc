# TODO — keelson-connector-pc

Roadmap for turning this connector from a working sampler into the data source behind a
host-health view in crowsnest. Ideas are drawn from
[beszel](https://github.com/henrygd/beszel) (lightweight agent, hub owns alerting) and
[netdata](https://github.com/netdata/netdata) (the incumbent on these machines, with a
decade of tuned alarm thresholds).

## How to read this

Every item is tagged with the repo that owns it:

| Tag | Repo |
|---|---|
| `[pc]` | this repo, `keelson-connector-pc` |
| `[keelson]` | `RISE-Maritime/keelson` — the SDK, subject registry and QoS profiles |
| `[crowsnest]` | `RISE-Maritime/crowsnest-dev` — the UI |

Priorities are `P0` (blocks everything) … `P3` (only if a platform needs it).

**Architecture decision.** Thresholds live in **crowsnest**. This connector does not publish
`entity_health` and does not evaluate policy — it samples, and it publishes raw metrics
*plus the denominators and hardware-declared limits needed to threshold them* (Theme C).
That keeps it consistent with `keelson/connectors/CLAUDE.md`, which reserves `entity_health`
for the aggregator.

> **Known consequence, accepted:** nothing on the bus evaluates health, so nothing can alert
> while no browser is open, and an MCAP recording carries measurements but no verdict. If
> that becomes a problem the fix is a separate `keelson-processor-host-health` subscribing to
> these subjects — not moving policy into the connector, where every host would re-implement
> it and two emitters would race on one `entity_health` key.

---

## P0 — Critical path

Nothing renders anywhere until these land. In order.


- [ ] **`[keelson]` Cut a release once #240 merges** — the Python SDK **and** `keelson-js`.
      Merging alone is not enough: consumers resolve a payload schema from *their own* copy
      of the registry and silently drop what they cannot resolve, so until a release ships,
      `keelson2mcap`, `keelson2foxglove` and crowsnest still throw away every reading this
      connector publishes. Registering the bundled copy at startup only ever fixed this
      process. Verified at the time of writing: upstream `messages/subjects.yaml` on `dev`
      contained **0 of our 42**, and `@rise-maritime/keelson-js`'s `dist/subjects.json`
      (199 subjects) also contained **0**.



- [ ] **`[pc]` Commit the pending working-tree changes** from the review pass (entry-point fix,
      `WORKDIR`, process/sensor key collisions, `process_count`).

---

## Theme A — Make it look alive in crowsnest (P1)



- [ ] **`[platforms]` Make the storage a deployment precondition, not an accident.** The above
      holds only where a storage is configured. `latest_local` uses `volume: "memory"`, so it
      is empty after a router restart until each key is published once — harmless here
      (identity goes out on the connector's first tick), but worth knowing. A platform that
      deploys this connector without an equivalent `storage_manager` entry gets no GET answers
      at all, and the UI falls back to waiting up to `--info-interval` for identity. Note it in
      the connector's deployment docs rather than working around it in code.
- [ ] **`[crowsnest]` Add `moment` to `package.json`.** `CardSystemInfo.jsx`,
      `ChartTimeLineLoad.jsx` and `ChartTimeLineValues.jsx` all import it, but it is not a
      declared dependency — it resolves only as a transitive one. Any dependency cleanup
      silently breaks exactly the host-monitoring stack.

- [ ] **`[crowsnest]` Surface liveliness *undeclare*.** `handleLivelinessSubscribe` posts
      `type: "token"` unconditionally, so the UI cannot distinguish a host appearing from one
      going away. Until this is fixed, do not rely on crowsnest showing a host disappearing.

---

## Theme B — Metrics worth considering 

### B1 `[pc]` Cross-platform, psutil-only, cheap (P1)

- [ ] **CPU time breakdown as separate subjects** — `user`, `system`, `iowait`, `steal`,
      `idle`. Already available from the `psutil.cpu_times()` call being made. netdata alarms
      on `iowait` and `steal` *separately* and deliberately excludes both from "CPU busy";
      sustained `steal > 10%` is the "hypervisor is oversubscribed" signal and is invisible in
      a single `cpu_load_pct` number.
- [ ] **Swap I/O rate**, not just swap usage. netdata's best thrashing signal is swap-*out*
      over 30 minutes as a % of RAM, which is a rate; `swap_used_pct` can sit high and stable
      on a perfectly healthy box.
- [ ] **NIC `operstate`**, beyond psutil's boolean `isup`. `lowerlayerdown` on a bond member is
      a real and distinct condition.

### B2 `[pc]` New optional Linux module (P2)

A new `keelson_connector_pc/linux_proc.py`, enabled by default on Linux and simply **absent**
elsewhere — the same "missing beats wrong" rule already applied to `sensors_temperatures`.
Gate with `--no-linux-proc`. Each row below is a single small file read.

| Metric | Source | Catches |
|---|---|---|
| PSI `some`/`full`, cpu + memory + io | `/proc/pressure/*` | thrashing, before it kills the box. `memory full avg60` is the best such signal there is |
| `oom_kill` counter | `/proc/vmstat` | OOM kills — nothing else reports these |
| `ListenOverflows`, `TCPReqQFullDrop` | `/proc/net/netstat` | the app is not `accept()`ing fast enough; invisible from every layer above the kernel |
| `RetransSegs`, `SynRetrans` | `/proc/net/snmp`, `/proc/net/netstat` | network degradation |
| conntrack count **and max** | `/proc/sys/net/netfilter/nf_conntrack_{count,max}` | table full → silent packet drops, no error anywhere |
| file-nr allocated **and max** | `/proc/sys/fs/file-nr` | fd exhaustion |
| softnet `dropped`, `squeezed` | `/proc/net/softnet_stat` | packets vanishing with no counter to explain it |
| disk `await`, `backlog`, `util` | `/proc/diskstats` fields 7/11/13/14 | I/O congestion. netdata alarms on *backlog*, not await |
| thermal throttle counts | `/sys/devices/system/cpu/cpu*/thermal_throttle/*` | silent clock-down under sustained load |
| ECC correctable / uncorrectable | `/sys/devices/system/edac/mc/mc*/*_count` | failing DIMMs. Correctable → warn, uncorrectable → critical |
| uptime | `/proc/uptime`, **not** `psutil.boot_time()` | beszel's catch: under LXC, lxcfs virtualises `/proc/uptime` but cannot intercept `sysinfo(2)`, so psutil reports the *host's* uptime from inside a container |
| process count | `/proc/loadavg` field 4 | replaces the current O(n)-syscall `psutil.pids()` walk |

---

## Theme C — Let crowsnest threshold without hardcoding (P1)

This is what the "thresholds in crowsnest" decision actually demands, and it is the least
obvious part of this document. A UI that has to invent its own constants will get them wrong.

- [ ] **Publish denominators next to every ratio**, so the UI never divides by a number it
      made up: `nf_conntrack_max`, `pid_max`, `file-nr` max. `memory_total_bytes` and
      `network_interface_speed_mbps` are already published — keep it that way.

- [ ] **Publish the hardware's own verdict as data.** This is netdata's most transferable
      discipline: it ships **no** stock temperature-threshold alarms at all. It reads the
      driver's own limit and alarm files and alarms on the device's classification, then
      merely graphs the raw reading. So publish, alongside each temperature, the hwmon
      `*_crit` / `*_max` limits; and where available the SMART `PASSED`/`WARNING`/`FAILED`
      status and the NVMe critical-warning bitfield.

      Then crowsnest colours a sensor red because the driver says it is over its limit, not
      because someone picked 70 °C for a fleet of dissimilar machines.

- [ ] **Never publish a zero for "could not measure."** Already this connector's stated rule;
      it is repeated here because it is now a *contract* crowsnest depends on. A fabricated
      zero clears an alert; a gap does not. Any collector that cannot read must emit nothing.

---

## Theme D — Efficiency, from beszel (P2)

- [ ] **Interval-keyed delta state** — `{interval_ms: prev_snapshot}` rather than one global
      previous sample. Beszel does this in every rate collector. Theme A's vitals tier dodged
      the need for it by giving every subject exactly one cadence, but that only holds while
      no rate-bearing subject is published at two rates. Anything that later wants, say,
      `network_interface_rx_bitrate_bps` on both the fast and slow tiers needs this first.
- [ ] **Memoise discovery.** Enumerate NICs, mountpoints and hwmon chips once, then read only
      the value files each cycle (beszel uses `sync.OnceValues` for precisely this).
- [ ] **Do not wake sleeping disks.** Sample non-root filesystems on a slow timer; beszel's
      `DISK_USAGE_CACHE` defaults to 15 minutes for this reason alone.
- [ ] **Clamp and re-baseline instead of emitting a spike.** This connector already clamps
      deltas at zero for counter resets; add the upper bound too (beszel discards and
      re-baselines above 10 GB/s network, 50 GB/s disk).
- [ ] **Do not publish constantly-zero series** — netdata's `auto` chart behaviour. On a
      typical host this discards a large fraction of candidate keys.

---

## Theme E — Deeper hardware (P3)

Only if a specific platform needs it.

- [ ] **SMART** via `smartctl --json`, keyed by **serial number** rather than device path so it
      survives `/dev/sdX` renumbering across reboots. Poll at 300 s, rescan for devices at
      900 s, and pass `-n standby` so idle disks are not spun up just to be measured.
- [ ] **NVMe** via `nvme smart-log` — separate from SMART, and the critical-warning bitfield is
      the one field worth alarming on.
- [ ] **GPU** via `nvidia-smi` / AMD sysfs, run as a **long-lived streaming subprocess**
      (`nvidia-smi -l 4`), never a fork per sample.

---

## Non-goals

- **Container metrics.** `keelson-interface-docker` already owns these and runs on sealog-9.
  Do not port beszel's Docker collector.
- **Reimplementing netdata.** Netdata stays the deep-dive tool on these machines. This
  connector's job is to put host health *on the Keelson bus*, where it can be recorded to
  MCAP alongside everything else and viewed next to vessel data.
- **ML anomaly detection.** Netdata's own stock alert for it is `to: silent`.
- **A `/proc`-first rewrite.** Abandoning psutil would cost Windows and macOS support for a
  speed win that does not matter at a 1–5 s cadence.
- **Alert evaluation in the connector.** See the architecture decision above.

---

## Appendix — netdata's tuned thresholds, for the crowsnest UI

Since crowsnest owns policy, here are netdata's stock values rather than numbers invented
from scratch. Two ideas underpin all of them:

**1. Aggregate over a window before comparing.** Never threshold an instantaneous sample —
netdata's CPU alarm is `lookup: average -10m`. The aggregator carries meaning: `average` for
utilisation, `max` for peak-defined incidents, `min` for "the fault was present in every
sample".

**2. Separate raise and clear bounds** so an alert sitting on the boundary does not flap:

```
warn: $this > (($status >= $WARNING)  ? (75) : (85))
crit: $this > (($status == $CRITICAL) ? (85) : (95))
```

Read as: *if already warning, use the lower (clear) bound; otherwise the higher (raise)
bound.* Warning raises at 85 and clears at 75.

| Condition | Raise | Clear | Window | Note |
|---|---|---|---|---|
| CPU utilisation | 85 % | 75 % | 10 min | **excludes** `iowait`, `nice`, `steal` |
| CPU utilisation (crit) | 95 % | 85 % | 10 min | |
| CPU iowait | 40 % | 20 % | 10 min | |
| CPU steal | 10 % | 5 % | 20 min | hypervisor oversubscribed |
| RAM in use | 90 % | 80 % | instant | denominator counts cache as free |
| RAM critical | 98 % | 90 % | instant | |
| Swapped out | 30 % of RAM | 20 % | 30 min | a *rate*, far better than swap-used |
| Disk space | 90 % | 80 % | 1 min | |
| Disk space critical | 98 % **and** `avail < 5 GB` | 90 % | 1 min | the `avail` guard matters — a 98 %-full 20 TB array is not an emergency |
| Time until disk full | < 8 h | < 48 h | — | see below |
| Time until disk full (crit) | < 2 h | < 24 h | — | |
| Disk utilisation | 98 % | ×0.7 | 10 min | |
| Disk backlog | 5000 ms | ×0.7 | 10 min | better congestion signal than `await` |
| Load avg 15 | 2 × cores | 1.75 × | 1 min | floor of 2 cores for single-CPU hosts |
| Load avg 5 | 4 × cores | 3.5 × | 1 min | |
| Load avg 1 | 8 × cores | 7 × | 1 min | |
| NIC traffic | 90 % of link speed | 85 % | 1 min | as a % of `/sys/class/net/*/speed`, never absolute bytes |
| Packet drops | 2 % | — | 10 min | only when > 10 000 packets in the window; Wi-Fi relaxed to 10 % |
| conntrack | 90 % | 85 % | 10 s | crit 95/90 |
| fd utilisation | 90 % | — | 1 min | |
| Active processes | 90 % of `pid_max` | 85 % | 5 s | crit 95/90 |
| OOM kills | > 0 | — | 30 min | |
| ECC correctable | > 0 | — | 1 h | uncorrectable → **critical** |

**The predictive disk alarm** is worth implementing specially — it is two rules, and it turns
"the disk is 91 % full" (not actionable) into "you have 6 hours" (actionable):

```
disk_fill_rate       = (avail_50min_ago - avail_now) / elapsed_hours      # GB/hour
out_of_disk_space_time = (fill_rate > 0) ? (avail / fill_rate) : infinity  # hours
```

Alarm when `out_of_disk_space_time` drops below 8 h (warn) or 2 h (critical).

**Mapping to OpenBridge.** Netdata has two active levels; OpenBridge has five (Running →
Caution → Warning → Alarm → Critical). A reasonable mapping is netdata `WARNING` → Warning
(orange) and `CRITICAL` → Alarm (red), reserving Critical (magenta) for conditions requiring
immediate intervention — uncorrectable ECC, SMART `FAILED`, or a disk under 2 hours from
full. Do not use hex literals; see `OPENBRIDGE.md` in crowsnest for the variable names.

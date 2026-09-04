"""Host metric collection.

Every collector returns ``list[Reading]``. Nothing in this module imports zenoh
or keelson, so all of it is unit-testable by monkeypatching ``psutil``.

A ``Reading`` names a subject and the source-id suffix that distinguishes the
instance it came from; the payload type is resolved later from the subject
registry, so collectors never mention protobuf.
"""

import logging
import platform
import re
import subprocess
import time
from collections import Counter
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import psutil

logger = logging.getLogger("pc2keelson")


class Reading(NamedTuple):
    """One measurement bound for one Keelson key.

    ``source_suffix`` is appended to the connector's ``--source-id`` base to
    form the source-id, so a whole-host reading leaves it empty and a per-disk
    reading carries ``disk/data``.
    """

    subject: str
    source_suffix: str
    value: Any


# Pseudo- and virtual filesystems. Without this a Linux host reports dozens of
# mounts that carry no storage anyone can run out of.
DEFAULT_FSTYPE_EXCLUDE = (
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
)

# Sensor chips whose readings are the CPU package/core temperature rather than
# some other integrated circuit on the board.
CPU_TEMP_CHIPS = frozenset(
    {
        "acpitz",
        "coretemp",
        "cpu-thermal",
        "cpu_thermal",
        "k10temp",
        "k8temp",
        "soc_thermal",
        "zenpower",
    }
)

_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9_-]+")

# psutil documents cpu_freq() in MHz, but on Apple Silicon it returns
# scpufreq(current=4, min=1, max=4) -- GHz-shaped placeholders read out of
# sysctl, not a live measurement. Publishing that verbatim would claim a 4 MHz
# CPU. Anything below this floor is a placeholder, not a frequency, and is
# dropped for the same reason macOS temperatures are: a missing sample is
# honest, a wrong one is not.
MIN_PLAUSIBLE_CPU_MHZ = 100.0

# How long to wait before re-probing a mountpoint that raised. A permanently
# unreadable mount (a macOS firmlink, a host mount the container may not read)
# backs off to the ceiling and costs one syscall every few minutes; a mount
# that was merely not ready yet is picked up on the first probe that succeeds.
MOUNT_RETRY_INITIAL_S = 30.0
MOUNT_RETRY_MAX_S = 300.0


def sanitise(value: str, *, fallback: str = "unknown") -> str:
    """Reduce an arbitrary label to a single safe Keelson key chunk.

    Zenoh treats ``/`` as a separator and ``* ? $ #`` as pattern syntax, so a
    mountpoint or interface name cannot be dropped into a key as-is. ``/``
    becomes ``root``, ``/mnt/data`` becomes ``mnt_data``, and on Windows
    ``C:\\`` becomes ``c``.
    """
    lowered = value.strip().lower().replace("\\", "/").strip("/")
    if not lowered:
        return "root"
    cleaned = _UNSAFE_KEY_CHARS.sub("_", lowered).strip("_")
    return cleaned or fallback


def strip_host_root(mountpoint: str, host_root: Optional[str]) -> str:
    """Undo a bind-mount prefix so a containerised run labels host paths the way
    the host sees them: with ``--host-root /host/root``, ``/host/root/var``
    reports as ``/var`` and ``/host/root`` itself as ``/``."""
    if not host_root:
        return mountpoint
    root = host_root.rstrip("/")
    if not root or mountpoint == root:
        return "/"
    if mountpoint.startswith(root + "/"):
        return mountpoint[len(root) :]
    return mountpoint


def _read_cpu_model() -> Optional[str]:
    """Best-effort CPU brand string. ``platform.processor()`` is empty on Linux
    and returns the bare architecture on many systems, so read the real source
    where one exists."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(("model name", "Model", "Hardware")):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    elif system == "Darwin":
        try:
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.processor() or platform.machine() or None


def _read_os_version() -> str:
    """A human-meaningful OS version: the distribution on Linux, the product
    version on macOS and Windows."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    elif system == "Darwin":
        if version := platform.mac_ver()[0]:
            return f"macOS {version}"
    elif system == "Windows":
        if version := platform.win32_ver()[0]:
            return f"Windows {version}"
    return platform.version()


def collect_host_info() -> List[Reading]:
    """Host identity and capability. Near-constant for the life of the process,
    so it rides the slow ``--info-interval`` cadence alongside uptime."""
    readings = [
        Reading("host_name", "", platform.node() or "unknown"),
        Reading("host_operating_system", "", platform.system() or "unknown"),
        Reading("host_operating_system_version", "", _read_os_version()),
        Reading("host_kernel_version", "", platform.release() or "unknown"),
        Reading("host_architecture", "", platform.machine() or "unknown"),
    ]

    if cpu_model := _read_cpu_model():
        readings.append(Reading("cpu_model", "", cpu_model))
    if physical := psutil.cpu_count(logical=False):
        readings.append(Reading("cpu_core_count", "", physical))
    if logical := psutil.cpu_count(logical=True):
        readings.append(Reading("cpu_thread_count", "", logical))

    boot_time = psutil.boot_time()
    readings.append(Reading("host_boot_time", "", int(boot_time * 1e9)))
    readings.append(
        Reading("device_uptime_duration", "", max(0.0, time.time() - boot_time))
    )
    return readings


def disambiguated_labels(chip: str, entries: Sequence[Any]) -> List[str]:
    """One unique, stable source-id label per sensor entry.

    A chip name does not identify a device. Two NVMe drives both report as
    chip "nvme" with a "Composite" and a "Sensor 1" reading each, so the label
    alone would put two different temperatures on one key in the same cycle and
    let arrival order pick the winner -- observed on real hardware reporting
    38.85 and 39.85 for the same key.

    Only labels that actually repeat get their index appended, so the common
    case (coretemp's already-distinct "Core N") keeps its clean name. Counting
    happens after sanitising, so two labels that differ only in characters
    sanitise() strips cannot collide either.
    """
    labels = [
        sanitise(entry.label or f"{chip}_{index}")
        for index, entry in enumerate(entries)
    ]
    counts = Counter(labels)
    return [
        label if counts[label] == 1 else f"{label}_{index}"
        for index, label in enumerate(labels)
    ]


class Sampler:
    """Holds the cross-cycle state the live metrics need.

    Three things cannot be sampled statelessly, and all three are why this is a
    class rather than more free functions:

    * ``psutil.cpu_percent(interval=None)`` reports utilisation *since the
      previous call*, and returns ``0.0`` the first time. ``prime()`` makes that
      throwaway call at startup so the first published sample is real.
    * NIC and disk throughput are deltas between consecutive counter snapshots,
      so the first cycle has nothing to divide and emits no rate.
    * ``Process.cpu_percent()`` has the same since-last-call semantics *per
      object*, so the ``psutil.Process`` instances have to survive between
      cycles. Rebuilding them each cycle would report 0.0 forever.
    """

    def __init__(
        self,
        *,
        cpu: bool = True,
        memory: bool = True,
        disk: bool = True,
        network: bool = True,
        sensors: bool = True,
        processes: bool = True,
        per_core: bool = False,
        disk_io: bool = True,
        disk_mountpoints: Optional[Sequence[str]] = None,
        disk_fstype_exclude: Sequence[str] = DEFAULT_FSTYPE_EXCLUDE,
        processes_top_n: int = 5,
        host_root: Optional[str] = None,
    ):
        self.cpu = cpu
        self.memory = memory
        self.disk = disk
        self.network = network
        self.sensors = sensors
        self.processes = processes
        self.per_core = per_core
        self.disk_io = disk_io
        self.disk_mountpoints = list(disk_mountpoints or [])
        self.disk_fstype_exclude = {f.lower() for f in disk_fstype_exclude}
        self.processes_top_n = processes_top_n
        self.host_root = host_root

        self._prev_net: Optional[Tuple[float, Dict[str, Any]]] = None
        self._prev_disk_io: Optional[Tuple[float, Dict[str, Any]]] = None
        self._procs: Dict[int, psutil.Process] = {}
        # Mountpoints that raised, and when to try them again. Re-probing every
        # cycle burns syscalls and log lines, but never re-probing means a mount
        # that is merely slow to appear -- NFS, an external disk, an encrypted
        # volume unlocked after boot -- stays missing until the process
        # restarts. Back off instead of giving up.
        self._mount_retry: Dict[str, Tuple[float, float]] = {}
        self._warned_cpu_freq = False

    # -- lifecycle ---------------------------------------------------------

    def prime(self) -> None:
        """Take the throwaway first reading of every since-last-call counter."""
        if self.cpu:
            psutil.cpu_percent(interval=None)
            if self.per_core:
                psutil.cpu_percent(interval=None, percpu=True)
        if self.network:
            self._snapshot_net()
        if self.disk and self.disk_io:
            self._snapshot_disk_io()
        if self.processes:
            self._refresh_processes()

    def _vital_cpu(self) -> List[Reading]:
        return [Reading("cpu_load_pct", "", psutil.cpu_percent(interval=None))]

    def _vital_memory(self) -> List[Reading]:
        return [Reading("memory_used_pct", "", psutil.virtual_memory().percent)]

    def sample_vitals(self) -> List[Reading]:
        """The fast tier: one /proc/stat read and one /proc/meminfo read.

        These two subjects are published *only* here, never by collect_cpu or
        collect_memory. That is load-bearing rather than tidiness:
        psutil.cpu_percent(interval=None) measures since the previous call
        anywhere in the process, so a second call site on a different cadence
        would silently halve both windows -- no error, just a wrong number.
        Keep cpu_load_pct to exactly one caller.

        Guarded per group like sample(), so a failing CPU read still publishes
        memory.
        """
        readings: List[Reading] = []
        for enabled, collect, name in (
            (self.cpu, self._vital_cpu, "cpu"),
            (self.memory, self._vital_memory, "memory"),
        ):
            if not enabled:
                continue
            try:
                readings.extend(collect())
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Vitals collector %r failed; skipping this cycle", name
                )
        return readings

    def sample(self, process_details: bool = True) -> List[Reading]:
        """One cycle of every enabled live metric group.

        A failure in one group must not cost the others their cycle, so each is
        guarded separately and logged rather than raised. ``process_details``
        is passed through to ``collect_processes``; see its docstring.
        """
        readings: List[Reading] = []
        for enabled, collect, name in (
            (self.cpu, self.collect_cpu, "cpu"),
            (self.memory, self.collect_memory, "memory"),
            (self.disk, self.collect_disk, "disk"),
            (self.network, self.collect_network, "network"),
            (self.sensors, self.collect_sensors, "sensors"),
            (
                self.processes,
                lambda: self.collect_processes(details=process_details),
                "processes",
            ),
        ):
            if not enabled:
                continue
            try:
                readings.extend(collect())
            except Exception:  # pylint: disable=broad-except
                logger.exception("Collector %r failed; skipping this cycle", name)
        return readings

    # -- CPU ---------------------------------------------------------------

    def collect_cpu(self) -> List[Reading]:
        # cpu_load_pct is deliberately absent: it rides the vitals cadence, in
        # sample_vitals(). percpu=True below keeps a separate psutil baseline
        # from the scalar call, so the two do not interfere.
        readings: List[Reading] = []

        if self.per_core:
            for index, load in enumerate(
                psutil.cpu_percent(interval=None, percpu=True)
            ):
                readings.append(Reading("cpu_core_load_pct", f"core/{index}", load))

        try:
            freq = psutil.cpu_freq()
        except (NotImplementedError, AttributeError, OSError):
            freq = None
        if freq and freq.current:
            if freq.current >= MIN_PLAUSIBLE_CPU_MHZ:
                readings.append(Reading("cpu_frequency_mhz", "", freq.current))
            elif not self._warned_cpu_freq:
                self._warned_cpu_freq = True
                logger.info(
                    "Ignoring implausible cpu_freq() reading of %s MHz; this "
                    "platform does not report a real CPU frequency",
                    freq.current,
                )

        try:
            one, five, fifteen = psutil.getloadavg()
        except (OSError, AttributeError):
            pass
        else:
            readings.append(Reading("cpu_load_average_1min", "", one))
            readings.append(Reading("cpu_load_average_5min", "", five))
            readings.append(Reading("cpu_load_average_15min", "", fifteen))

        return readings

    # -- memory ------------------------------------------------------------

    def collect_memory(self) -> List[Reading]:
        vm = psutil.virtual_memory()
        # total - available, not vm.used: `used` excludes buffers/cache on Linux
        # and means something different again on macOS, so it is not comparable
        # across the platforms this connector runs on. `available` is.
        readings = [
            Reading("memory_total_bytes", "", vm.total),
            Reading("memory_available_bytes", "", vm.available),
            Reading("memory_used_bytes", "", vm.total - vm.available),
            # memory_used_pct rides the vitals cadence, in sample_vitals().
        ]

        sm = psutil.swap_memory()
        readings.append(Reading("swap_total_bytes", "", sm.total))
        readings.append(Reading("swap_used_bytes", "", sm.used))
        readings.append(Reading("swap_used_pct", "", sm.percent))
        return readings

    # -- disk --------------------------------------------------------------

    def _mountpoints(self) -> List[str]:
        if self.disk_mountpoints:
            return self.disk_mountpoints
        try:
            partitions = psutil.disk_partitions(all=False)
        except OSError:
            logger.exception("Could not enumerate disk partitions")
            return []
        return [
            p.mountpoint
            for p in partitions
            if (p.fstype or "").lower() not in self.disk_fstype_exclude
        ]

    def collect_disk(self) -> List[Reading]:
        readings: List[Reading] = []
        now = time.monotonic()

        for mountpoint in self._mountpoints():
            retry = self._mount_retry.get(mountpoint)
            if retry is not None and now < retry[0]:
                continue
            try:
                usage = psutil.disk_usage(mountpoint)
            except OSError as exc:
                # Unreadable mounts are normal: macOS puts firmlinks and
                # sealed volumes in the partition list, and a container sees
                # host mounts it has no business reading. Those never recover,
                # so the backoff grows; a transiently missing mount recovers on
                # whichever probe first succeeds.
                backoff = (
                    min(retry[1] * 2, MOUNT_RETRY_MAX_S)
                    if retry is not None
                    else MOUNT_RETRY_INITIAL_S
                )
                self._mount_retry[mountpoint] = (now + backoff, backoff)
                logger.debug(
                    "Skipping mountpoint %s for %.0fs: %s", mountpoint, backoff, exc
                )
                continue
            if retry is not None:
                del self._mount_retry[mountpoint]
                logger.info("Mountpoint %s is readable again", mountpoint)
            suffix = f"disk/{sanitise(strip_host_root(mountpoint, self.host_root))}"
            readings.append(Reading("disk_total_bytes", suffix, usage.total))
            readings.append(Reading("disk_used_bytes", suffix, usage.used))
            readings.append(Reading("disk_free_bytes", suffix, usage.free))
            readings.append(Reading("disk_used_pct", suffix, usage.percent))

        if self.disk_io:
            readings.extend(self._collect_disk_io())
        return readings

    def _snapshot_disk_io(self) -> Optional[Tuple[float, Dict[str, Any]]]:
        """Read the counters and stamp them with the instant they were read.

        The stamp travels with the counters so that ``elapsed`` spans exactly
        the same window as the counter delta. Timing the two independently
        makes every rate wrong by however long the read took.
        """
        try:
            counters = psutil.disk_io_counters(perdisk=True)
        except (OSError, RuntimeError):
            logger.debug("Disk I/O counters unavailable", exc_info=True)
            return None
        if not counters:
            return None
        self._prev_disk_io = (time.monotonic(), counters)
        return self._prev_disk_io

    def _collect_disk_io(self) -> List[Reading]:
        previous = self._prev_disk_io
        snapshot = self._snapshot_disk_io()
        if snapshot is None or previous is None:
            return []

        now, counters = snapshot
        prev_time, prev_counters = previous
        elapsed = now - prev_time
        if elapsed <= 0:
            return []

        readings: List[Reading] = []
        for device, current in counters.items():
            before = prev_counters.get(device)
            if before is None:
                continue
            suffix = f"disk/{sanitise(device)}"
            readings.append(
                Reading(
                    "disk_read_bytes_per_second",
                    suffix,
                    max(0, current.read_bytes - before.read_bytes) / elapsed,
                )
            )
            readings.append(
                Reading(
                    "disk_write_bytes_per_second",
                    suffix,
                    max(0, current.write_bytes - before.write_bytes) / elapsed,
                )
            )
        return readings

    # -- network -----------------------------------------------------------

    def _snapshot_net(self) -> Optional[Tuple[float, Dict[str, Any]]]:
        """Read the counters and stamp them with the instant they were read.

        See ``_snapshot_disk_io`` for why the stamp travels with the counters.
        """
        try:
            counters = psutil.net_io_counters(pernic=True)
        except OSError:
            logger.debug("Network counters unavailable", exc_info=True)
            return None
        if not counters:
            return None
        self._prev_net = (time.monotonic(), counters)
        return self._prev_net

    def collect_network(self) -> List[Reading]:
        previous = self._prev_net
        snapshot = self._snapshot_net()
        if snapshot is None:
            return []
        now, counters = snapshot

        try:
            stats = psutil.net_if_stats()
        except OSError:
            stats = {}

        elapsed = now - previous[0] if previous else 0.0
        prev_counters = previous[1] if previous else {}

        readings: List[Reading] = []
        for nic, current in counters.items():
            suffix = f"net/{sanitise(nic)}"

            if stat := stats.get(nic):
                readings.append(Reading("network_interface_up", suffix, stat.isup))
                if stat.speed:  # 0 means "unknown", not "zero-speed link"
                    readings.append(
                        Reading(
                            "network_interface_speed_mbps", suffix, float(stat.speed)
                        )
                    )

            readings.append(
                Reading("network_interface_rx_bytes_total", suffix, current.bytes_recv)
            )
            readings.append(
                Reading("network_interface_tx_bytes_total", suffix, current.bytes_sent)
            )
            readings.append(
                Reading(
                    "network_interface_error_count",
                    suffix,
                    current.errin + current.errout,
                )
            )
            readings.append(
                Reading(
                    "network_interface_drop_count",
                    suffix,
                    current.dropin + current.dropout,
                )
            )

            before = prev_counters.get(nic)
            if before is None or elapsed <= 0:
                continue
            # Counters are bytes; the subject is bits per second, matching the
            # existing radio_*_bitrate_bps subjects.
            readings.append(
                Reading(
                    "network_interface_rx_bitrate_bps",
                    suffix,
                    max(0, current.bytes_recv - before.bytes_recv) * 8 / elapsed,
                )
            )
            readings.append(
                Reading(
                    "network_interface_tx_bitrate_bps",
                    suffix,
                    max(0, current.bytes_sent - before.bytes_sent) * 8 / elapsed,
                )
            )
        return readings

    # -- sensors -----------------------------------------------------------

    def collect_sensors(self) -> List[Reading]:
        """Temperatures, fans and battery.

        Temperatures and fans exist only on Linux (and FreeBSD) — psutil does
        not define the functions at all elsewhere, so this checks for the
        attribute rather than swallowing an exception. On macOS and Windows the
        corresponding subjects simply go unpublished, which is the honest
        answer; publishing 0.0 would look like a very cold CPU.
        """
        readings: List[Reading] = []

        if hasattr(psutil, "sensors_temperatures"):
            for chip, entries in (psutil.sensors_temperatures() or {}).items():
                subject = (
                    "cpu_temperature_celsius"
                    if chip.lower() in CPU_TEMP_CHIPS
                    else "integrated_circuit_temperature_celsius"
                )
                for entry, label in zip(entries, disambiguated_labels(chip, entries)):
                    if entry.current is None:
                        continue
                    readings.append(
                        Reading(
                            subject, f"sensor/{sanitise(chip)}/{label}", entry.current
                        )
                    )

        if hasattr(psutil, "sensors_fans"):
            for chip, entries in (psutil.sensors_fans() or {}).items():
                for entry, label in zip(entries, disambiguated_labels(chip, entries)):
                    if entry.current is None:
                        continue
                    readings.append(
                        Reading(
                            "fan_rate_rpm",
                            f"fan/{sanitise(chip)}/{label}",
                            float(entry.current),
                        )
                    )

        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery is not None:
                readings.append(
                    Reading("battery_state_of_charge_pct", "battery", battery.percent)
                )
                readings.append(
                    Reading(
                        "battery_is_charging", "battery", bool(battery.power_plugged)
                    )
                )
                # POWER_TIME_UNLIMITED / _UNKNOWN are sentinels, not durations.
                if battery.secsleft is not None and battery.secsleft >= 0:
                    readings.append(
                        Reading(
                            "battery_time_remaining_s",
                            "battery",
                            float(battery.secsleft),
                        )
                    )

        return readings

    # -- processes ---------------------------------------------------------

    def _refresh_processes(self) -> Optional[int]:
        """Sync the cached Process objects with the live pid set.

        Existing objects are kept so their ``cpu_percent()`` interval stays
        anchored to the previous cycle; new ones are primed with a throwaway
        call so they report a real number next cycle rather than 0.0.

        Returns the number of live pids, which is deliberately *not*
        ``len(self._procs)``: the cache holds only the processes this uid may
        open, and under `pid: host` as a non-root user that is a small minority
        of them. Returns None if the pid table could not be read at all, so the
        caller can publish nothing rather than a wrong count.
        """
        try:
            live = set(psutil.pids())
        except OSError:
            logger.debug("Could not enumerate pids", exc_info=True)
            return None

        for pid in list(self._procs):
            if pid not in live:
                del self._procs[pid]

        for pid in live - self._procs.keys():
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            self._procs[pid] = proc

        return len(live)

    def collect_processes(self, details: bool = True) -> List[Reading]:
        """Process metrics. ``details`` gates the expensive top-N scan.

        ``process_count`` is one syscall and goes out every cycle; ranking
        every process on the host costs an ``oneshot()`` and three reads per
        pid, so the caller runs it on a slower cadence.
        """
        live_count = self._refresh_processes()

        readings: List[Reading] = []
        if live_count is not None:
            readings.append(Reading("process_count", "", live_count))

        if not details or self.processes_top_n <= 0:
            return readings

        # Readings are keyed by process *name*, so several processes sharing a
        # name -- python, chrome, postgres -- are one key. Summing them makes
        # that key mean "all of chrome", which is both what a host monitor
        # usually wants and the only reading that is well defined: publishing
        # each process separately would put two different values on the same
        # key in the same cycle. Aggregating on the sanitised name rather than
        # the raw one guarantees no two entries can still collide afterwards.
        totals: Dict[str, List[float]] = {}
        for proc in list(self._procs.values()):
            try:
                with proc.oneshot():
                    # Can exceed 100.0 on a multi-core host; that is the
                    # documented psutil semantic, not a bug to clamp away.
                    cpu = proc.cpu_percent(interval=None)
                    name = sanitise(proc.name())
                    mem_pct = proc.memory_percent()
                    rss = proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
            entry = totals.get(name)
            if entry is None:
                totals[name] = [cpu, mem_pct, float(rss)]
            else:
                entry[0] += cpu
                entry[1] += mem_pct
                entry[2] += rss

        ranked = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)
        for name, (cpu, mem_pct, rss) in ranked[: self.processes_top_n]:
            suffix = f"process/{name}"
            readings.append(Reading("process_cpu_load_pct", suffix, cpu))
            readings.append(Reading("process_memory_used_pct", suffix, mem_pct))
            readings.append(Reading("process_memory_used_bytes", suffix, rss))

        return readings


# The fast tier. Only these two: both are a single cheap /proc read, and both
# move fast enough that a 1 s cadence says something a 5 s one does not. Disk
# usage and chip temperature deliberately stay on --interval -- neither changes
# meaningfully within a second, and sampling them costs a statvfs per mountpoint
# and a full /sys/class/hwmon walk respectively.
VITALS_SUBJECTS = frozenset({"cpu_load_pct", "memory_used_pct"})

# The connector's full publishing surface: every subject any collector can
# emit, whether or not the attached hardware currently produces data for it.
#
# This is capability, not activity -- a machine with no battery still declares
# the battery subjects. It exists because that is the list a producing
# connector must advertise, one liveliness token per subject, once an SDK with
# the three-tier API reaches PyPI. tests/test_subjects.py holds it to the code
# by scanning collectors.py for subject literals, so it cannot drift.
EMITTED_SUBJECTS = frozenset(
    {
        # host identity
        "host_name",
        "host_operating_system",
        "host_operating_system_version",
        "host_kernel_version",
        "host_architecture",
        "host_boot_time",
        "device_uptime_duration",
        # cpu
        "cpu_model",
        "cpu_core_count",
        "cpu_thread_count",
        "cpu_load_pct",
        "cpu_core_load_pct",
        "cpu_frequency_mhz",
        "cpu_temperature_celsius",
        "cpu_load_average_1min",
        "cpu_load_average_5min",
        "cpu_load_average_15min",
        # memory
        "memory_total_bytes",
        "memory_used_bytes",
        "memory_available_bytes",
        "memory_used_pct",
        "swap_total_bytes",
        "swap_used_bytes",
        "swap_used_pct",
        # storage
        "disk_total_bytes",
        "disk_used_bytes",
        "disk_free_bytes",
        "disk_used_pct",
        "disk_read_bytes_per_second",
        "disk_write_bytes_per_second",
        # network interfaces
        "network_interface_up",
        "network_interface_speed_mbps",
        "network_interface_rx_bitrate_bps",
        "network_interface_tx_bitrate_bps",
        "network_interface_rx_bytes_total",
        "network_interface_tx_bytes_total",
        "network_interface_error_count",
        "network_interface_drop_count",
        # sensors
        "integrated_circuit_temperature_celsius",
        "fan_rate_rpm",
        "battery_state_of_charge_pct",
        "battery_is_charging",
        "battery_time_remaining_s",
        # processes
        "process_count",
        "process_cpu_load_pct",
        "process_memory_used_pct",
        "process_memory_used_bytes",
    }
)

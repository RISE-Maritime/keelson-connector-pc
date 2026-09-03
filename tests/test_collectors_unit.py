"""Collector behaviour against a faked psutil.

The interesting cases are all things that only happen on a machine the test
host is not: a Windows drive letter, a Linux thermal chip, a NIC that has
moved 40 GiB, a battery reporting a sentinel instead of a duration.
"""

from types import SimpleNamespace

import pytest

import psutil
from keelson_connector_pc.collectors import (
    MIN_PLAUSIBLE_CPU_MHZ,
    Reading,
    Sampler,
    sanitise,
    strip_host_root,
)


def values(readings, subject):
    return [r.value for r in readings if r.subject == subject]


def suffixes(readings, subject):
    return [r.source_suffix for r in readings if r.subject == subject]


# --- key sanitising -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/", "root"),
        ("", "root"),
        ("///", "root"),
        ("/mnt/data", "mnt_data"),
        ("C:\\", "c"),
        ("D:\\Data", "d_data"),
        ("/System/Volumes/Data", "system_volumes_data"),
        ("en0", "en0"),
        ("veth0@if12", "veth0_if12"),
        ("Wi-Fi", "wi-fi"),
        # Zenoh pattern syntax must never survive into a key
        ("weird*name?", "weird_name"),
        ("$store#1", "store_1"),
    ],
)
def test_sanitise(raw, expected):
    assert sanitise(raw) == expected


@pytest.mark.parametrize(
    "mountpoint,host_root,expected",
    [
        ("/host/root", "/host/root", "/"),
        ("/host/root/", "/host/root/", "/"),
        ("/host/root/var/log", "/host/root", "/var/log"),
        ("/var", None, "/var"),
        # A path outside the prefix is left alone rather than mangled
        ("/other", "/host/root", "/other"),
        # Not a prefix match despite the shared leading text
        ("/host/rootfs", "/host/root", "/host/rootfs"),
    ],
)
def test_strip_host_root(mountpoint, host_root, expected):
    assert strip_host_root(mountpoint, host_root) == expected


# --- CPU ------------------------------------------------------------------


def test_cpu_frequency_below_plausibility_floor_is_withheld(monkeypatch, caplog):
    """Apple Silicon reports scpufreq(current=4, ...) -- GHz-shaped
    placeholders. Publishing 4 on a subject named _mhz would claim a 4 MHz CPU."""
    monkeypatch.setattr(psutil, "cpu_percent", lambda **kw: 12.5)
    monkeypatch.setattr(
        psutil, "cpu_freq", lambda: SimpleNamespace(current=4, min=1, max=4)
    )
    monkeypatch.setattr(psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))

    readings = Sampler().collect_cpu()
    assert values(readings, "cpu_frequency_mhz") == []
    assert values(readings, "cpu_load_pct") == [12.5]


def test_plausible_cpu_frequency_is_published(monkeypatch):
    monkeypatch.setattr(psutil, "cpu_percent", lambda **kw: 12.5)
    monkeypatch.setattr(
        psutil,
        "cpu_freq",
        lambda: SimpleNamespace(current=3600.0, min=800.0, max=4200.0),
    )
    monkeypatch.setattr(psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))

    readings = Sampler().collect_cpu()
    assert values(readings, "cpu_frequency_mhz") == [3600.0]
    assert MIN_PLAUSIBLE_CPU_MHZ < 3600.0


def test_cpu_freq_unsupported_does_not_kill_the_group(monkeypatch):
    """psutil raises NotImplementedError on platforms with no frequency source;
    the rest of the CPU group must still be published."""

    def boom():
        raise NotImplementedError

    monkeypatch.setattr(psutil, "cpu_percent", lambda **kw: 7.0)
    monkeypatch.setattr(psutil, "cpu_freq", boom)
    monkeypatch.setattr(psutil, "getloadavg", lambda: (0.5, 0.6, 0.7))

    readings = Sampler().collect_cpu()
    assert values(readings, "cpu_load_pct") == [7.0]
    assert values(readings, "cpu_load_average_1min") == [0.5]
    assert values(readings, "cpu_frequency_mhz") == []


def test_per_core_uses_one_source_id_per_core(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "cpu_percent",
        lambda **kw: [10.0, 20.0, 30.0] if kw.get("percpu") else 20.0,
    )
    monkeypatch.setattr(psutil, "cpu_freq", lambda: None)
    monkeypatch.setattr(psutil, "getloadavg", lambda: (0.0, 0.0, 0.0))

    readings = Sampler(per_core=True).collect_cpu()
    assert suffixes(readings, "cpu_core_load_pct") == ["core/0", "core/1", "core/2"]
    assert values(readings, "cpu_core_load_pct") == [10.0, 20.0, 30.0]


# --- memory ---------------------------------------------------------------


def test_memory_used_is_total_minus_available(monkeypatch):
    """`used` excludes cache on Linux and means something else again on macOS.
    total - available is the figure that compares across platforms."""
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=34_359_738_368, available=8_589_934_592, used=1, percent=75.0
        ),
    )
    monkeypatch.setattr(
        psutil, "swap_memory", lambda: SimpleNamespace(total=0, used=0, percent=0.0)
    )

    readings = Sampler().collect_memory()
    assert values(readings, "memory_used_bytes") == [34_359_738_368 - 8_589_934_592]
    assert values(readings, "memory_used_pct") == [75.0]


def test_memory_totals_exceed_int32(monkeypatch):
    """32 GiB does not fit in a TimestampedInt; this is the regression guard
    for the encoder mapping in test_subjects.py."""
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=68_719_476_736, available=1, used=1, percent=99.0
        ),
    )
    monkeypatch.setattr(
        psutil, "swap_memory", lambda: SimpleNamespace(total=0, used=0, percent=0.0)
    )
    assert values(Sampler().collect_memory(), "memory_total_bytes") == [68_719_476_736]
    assert 68_719_476_736 > 2**31 - 1


# --- disk -----------------------------------------------------------------


def test_windows_drive_letters_become_valid_key_chunks(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint="C:\\", fstype="NTFS", device="C:"),
            SimpleNamespace(mountpoint="D:\\Data", fstype="NTFS", device="D:"),
        ],
    )
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda mp: SimpleNamespace(
            total=500_107_862_016,
            used=250_000_000_000,
            free=250_107_862_016,
            percent=50.0,
        ),
    )

    readings = Sampler(disk_io=False).collect_disk()
    assert suffixes(readings, "disk_used_pct") == ["disk/c", "disk/d_data"]
    assert values(readings, "disk_total_bytes") == [500_107_862_016] * 2


def test_pseudo_filesystems_are_excluded(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint="/", fstype="ext4", device="/dev/sda1"),
            SimpleNamespace(mountpoint="/run", fstype="tmpfs", device="tmpfs"),
            SimpleNamespace(
                mountpoint="/snap/core", fstype="squashfs", device="/dev/loop0"
            ),
        ],
    )
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda mp: SimpleNamespace(total=100, used=40, free=60, percent=40.0),
    )
    assert suffixes(Sampler(disk_io=False).collect_disk(), "disk_used_pct") == [
        "disk/root"
    ]


def test_unreadable_mountpoint_is_skipped_once(monkeypatch):
    """macOS sealed volumes and container-visible host mounts raise. Re-probing
    them every cycle burns syscalls on something that will not start working."""
    calls = []

    def usage(mountpoint):
        calls.append(mountpoint)
        raise PermissionError("nope")

    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all=False: [
            SimpleNamespace(mountpoint="/sealed", fstype="apfs", device="d")
        ],
    )
    monkeypatch.setattr(psutil, "disk_usage", usage)

    sampler = Sampler(disk_io=False)
    assert sampler.collect_disk() == []
    assert sampler.collect_disk() == []
    assert calls == ["/sealed"]


def test_explicit_mountpoints_override_autodetection(monkeypatch):
    monkeypatch.setattr(
        psutil, "disk_partitions", lambda all=False: pytest.fail("should not enumerate")
    )
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda mp: SimpleNamespace(total=10, used=1, free=9, percent=10.0),
    )
    readings = Sampler(disk_io=False, disk_mountpoints=["/data"]).collect_disk()
    assert suffixes(readings, "disk_used_pct") == ["disk/data"]


def test_host_root_prefix_is_stripped_from_labels(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda mp: SimpleNamespace(total=10, used=1, free=9, percent=10.0),
    )
    sampler = Sampler(
        disk_io=False,
        disk_mountpoints=["/host/root", "/host/root/var"],
        host_root="/host/root",
    )
    assert suffixes(sampler.collect_disk(), "disk_used_pct") == [
        "disk/root",
        "disk/var",
    ]


# --- rate derivation ------------------------------------------------------


def _nic(bytes_recv, bytes_sent, errin=0, errout=0, dropin=0, dropout=0):
    return SimpleNamespace(
        bytes_recv=bytes_recv,
        bytes_sent=bytes_sent,
        errin=errin,
        errout=errout,
        dropin=dropin,
        dropout=dropout,
    )


def test_first_network_cycle_emits_counters_but_no_rate(monkeypatch):
    """A rate needs two snapshots. The cumulative counters are still useful on
    the first cycle, so they go out; the bitrates do not."""
    monkeypatch.setattr(
        psutil, "net_io_counters", lambda pernic=True: {"eth0": _nic(1000, 500)}
    )
    monkeypatch.setattr(
        psutil, "net_if_stats", lambda: {"eth0": SimpleNamespace(isup=True, speed=1000)}
    )

    readings = Sampler().collect_network()
    assert values(readings, "network_interface_rx_bytes_total") == [1000]
    assert values(readings, "network_interface_rx_bitrate_bps") == []
    assert values(readings, "network_interface_up") == [True]
    assert values(readings, "network_interface_speed_mbps") == [1000.0]


def test_network_bitrate_is_bits_per_second(monkeypatch):
    counters = {"eth0": _nic(1000, 500)}
    clock = [100.0]
    monkeypatch.setattr(psutil, "net_io_counters", lambda pernic=True: dict(counters))
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {})
    monkeypatch.setattr(
        "keelson_connector_pc.collectors.time.monotonic", lambda: clock[0]
    )

    sampler = Sampler()
    sampler.collect_network()

    # 2000 bytes received over 2 seconds = 8000 bits/s
    counters["eth0"] = _nic(3000, 1500)
    clock[0] = 102.0
    readings = sampler.collect_network()

    assert values(readings, "network_interface_rx_bitrate_bps") == [8000.0]
    assert values(readings, "network_interface_tx_bitrate_bps") == [4000.0]


def test_network_counter_reset_does_not_produce_a_negative_rate(monkeypatch):
    """An interface that goes down and back up restarts its counters. A naive
    delta would publish a large negative bitrate."""
    counters = {"eth0": _nic(10_000, 10_000)}
    clock = [0.0]
    monkeypatch.setattr(psutil, "net_io_counters", lambda pernic=True: dict(counters))
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {})
    monkeypatch.setattr(
        "keelson_connector_pc.collectors.time.monotonic", lambda: clock[0]
    )

    sampler = Sampler()
    sampler.collect_network()
    counters["eth0"] = _nic(5, 5)
    clock[0] = 1.0

    readings = sampler.collect_network()
    assert values(readings, "network_interface_rx_bitrate_bps") == [0.0]


def test_speed_of_zero_means_unknown_and_is_not_published(monkeypatch):
    """psutil reports speed=0 for 'could not determine', which is not the same
    as a zero-speed link."""
    monkeypatch.setattr(
        psutil, "net_io_counters", lambda pernic=True: {"lo0": _nic(1, 1)}
    )
    monkeypatch.setattr(
        psutil, "net_if_stats", lambda: {"lo0": SimpleNamespace(isup=True, speed=0)}
    )
    readings = Sampler().collect_network()
    assert values(readings, "network_interface_speed_mbps") == []
    assert values(readings, "network_interface_up") == [True]


def test_disk_throughput_needs_two_snapshots(monkeypatch):
    counters = {"nvme0n1": SimpleNamespace(read_bytes=0, write_bytes=0)}
    clock = [0.0]
    monkeypatch.setattr(psutil, "disk_io_counters", lambda perdisk=True: dict(counters))
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: [])
    monkeypatch.setattr(
        "keelson_connector_pc.collectors.time.monotonic", lambda: clock[0]
    )

    sampler = Sampler()
    assert sampler.collect_disk() == []

    counters["nvme0n1"] = SimpleNamespace(read_bytes=2048, write_bytes=1024)
    clock[0] = 2.0
    readings = sampler.collect_disk()

    assert values(readings, "disk_read_bytes_per_second") == [1024.0]
    assert values(readings, "disk_write_bytes_per_second") == [512.0]
    assert suffixes(readings, "disk_read_bytes_per_second") == ["disk/nvme0n1"]


# --- sensors --------------------------------------------------------------
#
# Every monkeypatch here passes raising=False because psutil defines
# sensors_temperatures and sensors_fans only on Linux. On a macOS or Windows
# test host there is no attribute to replace -- which is the very platform
# difference collect_sensors() guards against with hasattr.


def _temp(label, current):
    return SimpleNamespace(label=label, current=current, high=None, critical=None)


def test_cpu_chips_route_to_cpu_temperature_others_to_generic(monkeypatch):
    monkeypatch.setattr(
        psutil,
        "sensors_temperatures",
        raising=False,
        value=lambda: {
            "coretemp": [_temp("Package id 0", 51.0)],
            "nvme": [_temp("Composite", 38.0)],
        },
    )
    monkeypatch.setattr(psutil, "sensors_fans", raising=False, value=lambda: {})
    monkeypatch.setattr(psutil, "sensors_battery", raising=False, value=lambda: None)

    readings = Sampler().collect_sensors()
    assert values(readings, "cpu_temperature_celsius") == [51.0]
    assert suffixes(readings, "cpu_temperature_celsius") == [
        "sensor/coretemp/package_id_0"
    ]
    assert values(readings, "integrated_circuit_temperature_celsius") == [38.0]


def test_temperatures_absent_on_macos_and_windows(monkeypatch):
    """psutil does not define sensors_temperatures off Linux at all. Nothing is
    published rather than a zero, which would read as a very cold CPU."""
    monkeypatch.delattr(psutil, "sensors_temperatures", raising=False)
    monkeypatch.delattr(psutil, "sensors_fans", raising=False)
    monkeypatch.setattr(psutil, "sensors_battery", raising=False, value=lambda: None)

    assert Sampler().collect_sensors() == []


def test_fan_readings_use_the_fan_subject(monkeypatch):
    monkeypatch.setattr(psutil, "sensors_temperatures", raising=False, value=lambda: {})
    monkeypatch.setattr(
        psutil,
        "sensors_fans",
        raising=False,
        value=lambda: {"dell_smm": [_temp("Processor Fan", 2500)]},
    )
    monkeypatch.setattr(psutil, "sensors_battery", raising=False, value=lambda: None)

    readings = Sampler().collect_sensors()
    assert values(readings, "fan_rate_rpm") == [2500.0]
    assert suffixes(readings, "fan_rate_rpm") == ["fan/dell_smm/processor_fan"]


def test_battery_sentinel_secsleft_is_not_published_as_a_duration(monkeypatch):
    """psutil returns POWER_TIME_UNLIMITED / _UNKNOWN (negative constants) when
    there is no estimate. Publishing those as seconds would be nonsense."""
    monkeypatch.setattr(psutil, "sensors_temperatures", raising=False, value=lambda: {})
    monkeypatch.setattr(psutil, "sensors_fans", raising=False, value=lambda: {})
    monkeypatch.setattr(
        psutil,
        "sensors_battery",
        raising=False,
        value=lambda: SimpleNamespace(
            percent=87.0, power_plugged=True, secsleft=psutil.POWER_TIME_UNLIMITED
        ),
    )

    readings = Sampler().collect_sensors()
    assert values(readings, "battery_state_of_charge_pct") == [87.0]
    assert values(readings, "battery_is_charging") == [True]
    assert values(readings, "battery_time_remaining_s") == []


def test_battery_with_a_real_estimate_publishes_it(monkeypatch):
    monkeypatch.setattr(psutil, "sensors_temperatures", raising=False, value=lambda: {})
    monkeypatch.setattr(psutil, "sensors_fans", raising=False, value=lambda: {})
    monkeypatch.setattr(
        psutil,
        "sensors_battery",
        raising=False,
        value=lambda: SimpleNamespace(percent=42.0, power_plugged=False, secsleft=5400),
    )
    readings = Sampler().collect_sensors()
    assert values(readings, "battery_time_remaining_s") == [5400.0]
    assert values(readings, "battery_is_charging") == [False]


# --- sample() resilience --------------------------------------------------


def test_a_failing_collector_does_not_cost_the_other_groups_their_cycle(monkeypatch):
    def boom():
        raise RuntimeError("kernel said no")

    sampler = Sampler(
        cpu=True, memory=True, disk=False, network=False, sensors=False, processes=False
    )
    monkeypatch.setattr(sampler, "collect_cpu", boom)
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=100, available=40, used=60, percent=60.0),
    )
    monkeypatch.setattr(
        psutil, "swap_memory", lambda: SimpleNamespace(total=0, used=0, percent=0.0)
    )

    readings = sampler.sample()
    assert values(readings, "memory_used_pct") == [60.0]
    assert values(readings, "cpu_load_pct") == []


def test_disabled_groups_are_not_sampled(monkeypatch):
    monkeypatch.setattr(psutil, "cpu_percent", lambda **kw: pytest.fail("cpu disabled"))
    sampler = Sampler(
        cpu=False,
        memory=False,
        disk=False,
        network=False,
        sensors=False,
        processes=False,
    )
    assert sampler.sample() == []

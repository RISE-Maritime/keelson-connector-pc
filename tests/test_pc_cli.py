"""Argument parsing and the wiring from flags to a Sampler."""

import pytest

from keelson_connector_pc.collectors import DEFAULT_FSTYPE_EXCLUDE


def parse(pc2keelson, *argv):
    return pc2keelson.build_parser().parse_args(["-r", "rise", "-e", "nuc01", *argv])


def test_realm_and_entity_are_required(pc2keelson):
    with pytest.raises(SystemExit):
        pc2keelson.build_parser().parse_args([])


def test_defaults(pc2keelson):
    args = parse(pc2keelson)
    assert args.source_id == "pc"
    assert args.interval == 5.0
    assert args.info_interval == 60.0
    assert args.processes_top_n == 5
    assert args.cpu_per_core is False
    assert args.disk_mountpoints is None
    assert args.procfs_path is None
    assert args.host_root is None
    # Every metric group is on unless explicitly switched off
    assert all(
        getattr(args, group)
        for group in (
            "cpu",
            "memory",
            "disk",
            "network",
            "sensors",
            "processes",
            "host_info",
            "disk_io",
        )
    )


@pytest.mark.parametrize(
    "flag,attribute",
    [
        ("--no-cpu", "cpu"),
        ("--no-memory", "memory"),
        ("--no-disk", "disk"),
        ("--no-network", "network"),
        ("--no-sensors", "sensors"),
        ("--no-processes", "processes"),
        ("--no-host-info", "host_info"),
        ("--no-disk-io", "disk_io"),
    ],
)
def test_every_group_can_be_disabled(pc2keelson, flag, attribute):
    assert getattr(parse(pc2keelson, flag), attribute) is False


def test_disk_mountpoint_is_repeatable(pc2keelson):
    args = parse(pc2keelson, "--disk-mountpoint", "/", "--disk-mountpoint", "/data")
    assert args.disk_mountpoints == ["/", "/data"]


def test_zenoh_config_defaults_to_the_environment(pc2keelson, monkeypatch):
    """--zenoh-config is how an operator applies access control and QoS the
    flags cannot express; ZENOH_CONFIG is how that is made fleet-wide."""
    monkeypatch.setenv("ZENOH_CONFIG", "/etc/zenoh/fleet.json5")
    # The default is read at parser-construction time, so rebuild it here.
    args = pc2keelson.build_parser().parse_args(["-r", "rise", "-e", "nuc01"])
    assert args.zenoh_config == "/etc/zenoh/fleet.json5"


def test_explicit_zenoh_config_beats_the_environment(pc2keelson, monkeypatch):
    monkeypatch.setenv("ZENOH_CONFIG", "/etc/zenoh/fleet.json5")
    args = pc2keelson.build_parser().parse_args(
        ["-r", "rise", "-e", "nuc01", "--zenoh-config", "/tmp/local.json5"]
    )
    assert args.zenoh_config == "/tmp/local.json5"


def test_make_sampler_passes_the_flags_through(pc2keelson):
    args = parse(
        pc2keelson,
        "--cpu-per-core",
        "--no-network",
        "--processes-top-n",
        "3",
        "--disk-mountpoint",
        "/data",
        "--host-root",
        "/host/root",
    )
    sampler = pc2keelson.make_sampler(args)
    assert sampler.per_core is True
    assert sampler.network is False
    assert sampler.processes_top_n == 3
    assert sampler.disk_mountpoints == ["/data"]
    assert sampler.host_root == "/host/root"


def test_fstype_exclusions_are_split_on_commas(pc2keelson):
    sampler = pc2keelson.make_sampler(parse(pc2keelson))
    assert sampler.disk_fstype_exclude == set(DEFAULT_FSTYPE_EXCLUDE)

    sampler = pc2keelson.make_sampler(
        parse(pc2keelson, "--disk-fstype-exclude", "tmpfs, overlay ,")
    )
    assert sampler.disk_fstype_exclude == {"tmpfs", "overlay"}


@pytest.mark.parametrize("argv", [["--interval", "0"], ["--processes-top-n", "-1"]])
def test_invalid_values_exit_nonzero_without_opening_a_session(
    pc2keelson, monkeypatch, argv
):
    monkeypatch.setattr("sys.argv", ["pc2keelson", "-r", "rise", "-e", "nuc01", *argv])
    monkeypatch.setattr(
        pc2keelson.zenoh,
        "open",
        lambda *a, **kw: pytest.fail("must not open a session"),
    )
    assert pc2keelson.main() == 2

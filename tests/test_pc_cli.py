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
    assert args.vitals_interval == 1.0
    assert args.interval == 5.0
    assert args.info_interval == 60.0
    assert args.process_interval == 30.0
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


def test_zenoh_config_is_read_from_the_environment(pc2keelson, monkeypatch, tmp_path):
    """--zenoh-config is how an operator applies access control and QoS the
    flags cannot express; ZENOH_CONFIG is how that is made fleet-wide.

    The SDK owns this flag from keelson 0.6.0rc15 on, and resolves the
    environment fallback inside create_zenoh_config rather than as an argparse
    default -- so assert the behaviour, not the parsed value.
    """
    conf_file = tmp_path / "fleet.json5"
    conf_file.write_text('{ mode: "client" }')
    monkeypatch.setenv("ZENOH_CONFIG", str(conf_file))

    args = pc2keelson.build_parser().parse_args(["-r", "rise", "-e", "nuc01"])
    assert args.zenoh_config is None  # the flag itself is unset...
    # ...but the file is still what the session is built from.
    assert "client" in str(pc2keelson.make_zenoh_config(args))


def test_explicit_zenoh_config_beats_the_environment(pc2keelson, monkeypatch, tmp_path):
    from_env = tmp_path / "fleet.json5"
    from_env.write_text('{ mode: "peer" }')
    explicit = tmp_path / "local.json5"
    explicit.write_text('{ mode: "client" }')
    monkeypatch.setenv("ZENOH_CONFIG", str(from_env))

    args = pc2keelson.build_parser().parse_args(
        ["-r", "rise", "-e", "nuc01", "--zenoh-config", str(explicit)]
    )
    assert args.zenoh_config == str(explicit)
    assert "client" in str(pc2keelson.make_zenoh_config(args))


def test_flags_win_over_the_config_file(pc2keelson, tmp_path):
    """The layering the connector used to hand-roll, now the SDK's job."""
    conf_file = tmp_path / "fleet.json5"
    conf_file.write_text('{ mode: "peer" }')

    args = pc2keelson.build_parser().parse_args(
        [
            "-r",
            "rise",
            "-e",
            "nuc01",
            "--zenoh-config",
            str(conf_file),
            "--mode",
            "client",
        ]
    )
    assert "client" in str(pc2keelson.make_zenoh_config(args))


def test_process_interval_is_independent_of_interval(pc2keelson):
    """The top-N scan is the connector's most expensive operation, so it has
    its own cadence rather than riding --interval."""
    args = parse(pc2keelson, "--interval", "1", "--process-interval", "60")
    assert args.interval == 1.0
    assert args.process_interval == 60.0


def test_vitals_interval_may_equal_interval(pc2keelson, monkeypatch):
    """Only *exceeding* --interval is incoherent; equal is a legitimate way to
    ask for a single uniform cadence, so validation must let it through."""

    class ReachedSession(Exception):
        pass

    def boom(*_a, **_kw):
        raise ReachedSession

    monkeypatch.setattr(
        "sys.argv",
        [
            "pc2keelson",
            "-r",
            "rise",
            "-e",
            "nuc01",
            "--vitals-interval",
            "5",
            "--interval",
            "5",
        ],
    )
    monkeypatch.setattr(pc2keelson.zenoh, "open", boom)

    # Getting as far as opening a session is the assertion: a rejected value
    # would have returned 2 well before this.
    with pytest.raises(ReachedSession):
        pc2keelson.main()


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


@pytest.mark.parametrize(
    "argv",
    [
        ["--interval", "0"],
        ["--interval", "-1"],
        ["--info-interval", "0"],
        ["--process-interval", "0"],
        ["--vitals-interval", "0"],
        # a "fast" tier slower than the slow one would stretch --interval
        ["--vitals-interval", "10", "--interval", "5"],
        ["--processes-top-n", "-1"],
    ],
)
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

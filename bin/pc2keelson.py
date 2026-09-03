#!/usr/bin/env python3

"""Command line utility for monitoring a computer and publishing to Keelson/Zenoh.

Samples the host it runs on -- CPU, memory, storage, network interfaces,
temperatures, battery and processes -- with psutil, and publishes each quantity
as its own Keelson subject carrying a Timestamped* primitive. Runs on Linux,
macOS and Windows; metrics the platform does not expose are simply not
published rather than reported as zero.

Two cadences: --interval for live metrics, --info-interval for host identity
and uptime, which barely change.
"""

import argparse
import json
import logging
import os
import pathlib
import sys
import time

import psutil
import zenoh

import keelson
from keelson.scaffolding import (
    GracefulShutdown,
    add_common_arguments,
    create_zenoh_config,
    setup_logging,
)
from keelson.scaffolding.liveliness import declare_liveliness_token

# Importable when run straight out of a checkout (`python bin/pc2keelson.py`).
# Guarded because the installed copy lives at /usr/local/bin, and inserting
# /usr/local unconditionally would put its lib/, bin/ and share/ directories at
# the front of sys.path as namespace packages, ahead of every real module.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "keelson_connector_pc" / "__init__.py").is_file():
    sys.path.insert(0, str(_REPO_ROOT))

from keelson_connector_pc.collectors import (  # noqa: E402  pylint: disable=wrong-import-position
    DEFAULT_FSTYPE_EXCLUDE,
    Sampler,
    collect_host_info,
)
from keelson_connector_pc.publishing import (  # noqa: E402  pylint: disable=wrong-import-position
    Publisher,
    register_subjects,
)

logger = logging.getLogger("pc2keelson")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc2keelson",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Monitor this computer and publish its metrics to Keelson/Zenoh",
    )

    # --log-level, --mode/-m, --connect, --listen
    add_common_arguments(parser)

    # keelson 0.5.3's create_zenoh_config takes no zenoh_config argument, so the
    # connector carries the flag itself. Without it an operator has no way to
    # hand the session what the flags cannot express -- access_control, QoS
    # defaults, transport tuning -- and would get no warning that the policy
    # they thought they applied is not in force.
    parser.add_argument(
        "--zenoh-config",
        type=str,
        default=os.environ.get("ZENOH_CONFIG"),
        help="Path to a JSON5 Zenoh config file applied under the flags above "
        "(default: the ZENOH_CONFIG environment variable)",
    )

    parser.add_argument(
        "-r",
        "--realm",
        type=str,
        required=True,
        help="Realm/base path to publish under, ex. rise",
    )
    parser.add_argument(
        "-e",
        "--entity-id",
        type=str,
        required=True,
        help="Unique id of the entity within the realm, ex. nuc01",
    )
    parser.add_argument(
        "-s",
        "--source-id",
        type=str,
        default="pc",
        help="Source-id base; per-instance suffixes are appended "
        "to it, ex. pc/disk/data",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between samples of the live metrics",
    )
    parser.add_argument(
        "--info-interval",
        type=float,
        default=60.0,
        help="Seconds between publishes of host identity and uptime",
    )

    parser.add_argument(
        "--no-cpu",
        dest="cpu",
        action="store_false",
        help="Do not publish CPU load, frequency or load average",
    )
    parser.add_argument(
        "--no-memory",
        dest="memory",
        action="store_false",
        help="Do not publish memory or swap",
    )
    parser.add_argument(
        "--no-disk",
        dest="disk",
        action="store_false",
        help="Do not publish storage usage or throughput",
    )
    parser.add_argument(
        "--no-network",
        dest="network",
        action="store_false",
        help="Do not publish network interface counters",
    )
    parser.add_argument(
        "--no-sensors",
        dest="sensors",
        action="store_false",
        help="Do not publish temperatures, fans or battery",
    )
    parser.add_argument(
        "--no-processes",
        dest="processes",
        action="store_false",
        help="Do not publish process count or per-process metrics",
    )
    parser.add_argument(
        "--no-host-info",
        dest="host_info",
        action="store_false",
        help="Do not publish host identity or uptime",
    )

    parser.add_argument(
        "--cpu-per-core",
        action="store_true",
        help="Also publish cpu_core_load_pct per logical core "
        "(one extra key per core)",
    )
    parser.add_argument(
        "--no-disk-io",
        dest="disk_io",
        action="store_false",
        help="Do not publish per-device disk read/write throughput",
    )
    parser.add_argument(
        "--disk-mountpoint",
        dest="disk_mountpoints",
        action="append",
        metavar="PATH",
        default=None,
        help="Report only this mountpoint; repeatable. Default is "
        "every real filesystem psutil reports",
    )
    parser.add_argument(
        "--disk-fstype-exclude",
        type=str,
        default=",".join(DEFAULT_FSTYPE_EXCLUDE),
        metavar="FSTYPES",
        help="Comma-separated filesystem types to skip when "
        "auto-detecting mountpoints",
    )
    parser.add_argument(
        "--processes-top-n",
        type=int,
        default=5,
        metavar="N",
        help="Publish the N processes using the most CPU (0 = only "
        "publish process_count)",
    )

    parser.add_argument(
        "--procfs-path",
        type=str,
        default=None,
        metavar="PATH",
        help="Read /proc from here instead (Linux). Set this to the "
        "host's /proc when running in a container, ex. /host/proc",
    )
    parser.add_argument(
        "--host-root",
        type=str,
        default=None,
        metavar="PATH",
        help="Bind-mount prefix to strip from mountpoint labels so a "
        "containerised run names host paths as the host does, "
        "ex. /host/root",
    )

    return parser


def make_zenoh_config(args: argparse.Namespace) -> zenoh.Config:
    """Build the session config: the --zenoh-config file first, then the
    individual flags on top so an explicit flag always wins."""
    if args.zenoh_config:
        conf = zenoh.Config.from_file(args.zenoh_config)
        if args.mode is not None:
            conf.insert_json5("mode", json.dumps(args.mode))
        if args.connect is not None:
            conf.insert_json5("connect/endpoints", json.dumps(args.connect))
        if args.listen is not None:
            conf.insert_json5("listen/endpoints", json.dumps(args.listen))
        return conf

    return create_zenoh_config(mode=args.mode, connect=args.connect, listen=args.listen)


def make_sampler(args: argparse.Namespace) -> Sampler:
    return Sampler(
        cpu=args.cpu,
        memory=args.memory,
        disk=args.disk,
        network=args.network,
        sensors=args.sensors,
        processes=args.processes,
        per_core=args.cpu_per_core,
        disk_io=args.disk_io,
        disk_mountpoints=args.disk_mountpoints,
        disk_fstype_exclude=[
            f.strip() for f in args.disk_fstype_exclude.split(",") if f.strip()
        ],
        processes_top_n=args.processes_top_n,
        host_root=args.host_root,
    )


def run(session: zenoh.Session, args: argparse.Namespace, shutdown: GracefulShutdown):
    publisher = Publisher(session, args.realm, args.entity_id, args.source_id)
    sampler = make_sampler(args)

    logger.info("Priming counters...")
    sampler.prime()

    # Host info goes out immediately rather than waiting a whole info-interval,
    # so a subscriber that joins at startup learns what machine this is at once.
    next_info = 0.0

    while not shutdown.is_requested():
        timestamp_ns = time.time_ns()
        now = time.monotonic()

        readings = sampler.sample()

        if args.host_info and now >= next_info:
            readings.extend(collect_host_info())
            next_info = now + args.info_interval

        published = publisher.publish(readings, timestamp_ns)
        logger.debug("Published %d readings", published)

        # Interruptible: a plain sleep would hold SIGTERM for a whole interval.
        shutdown.wait(timeout=args.interval)

    publisher.undeclare()


def main() -> int:
    args = build_parser().parse_args()

    setup_logging(level=args.log_level)
    zenoh.init_log_from_env_or(logging.getLevelName(args.log_level))

    if args.interval <= 0:
        logger.error("--interval must be greater than 0")
        return 2
    if args.processes_top_n < 0:
        logger.error("--processes-top-n cannot be negative")
        return 2

    if args.procfs_path:
        # Only meaningful on Linux; psutil ignores the attribute elsewhere.
        psutil.PROCFS_PATH = args.procfs_path
        logger.info("Reading procfs from %s", args.procfs_path)

    known = register_subjects()
    logger.info("Registered %d host-metric subjects", len(known))

    conf = make_zenoh_config(args)

    logger.info("Opening Zenoh session...")
    with zenoh.open(conf) as session:
        # Built from the format string rather than construct_pubsub_key: the
        # latter would warn that the literal "<subject>" is not well-known.
        logger.info(
            "Publishing to %s",
            keelson.KEELSON_PUB_SUB_KEY_FORMAT.format(
                base_path=args.realm,
                entity_id=args.entity_id,
                subject="<subject>",
                source_id=f"{args.source_id}/...",
            ),
        )
        # keelson 0.5.3 offers only this coarse source-level token. Move to
        # scaffolding.declare_liveliness(..., pubsub_subjects=[...]) once a
        # release with the three-tier API reaches PyPI -- a producing connector
        # is meant to advertise one token per subject it can publish.
        with (
            declare_liveliness_token(
                session, args.realm, args.entity_id, args.source_id
            ),
            GracefulShutdown() as shutdown,
        ):
            try:
                run(session, args, shutdown)
            except KeyboardInterrupt:
                logger.info("Program ended due to user request (Ctrl-C)")

    logger.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

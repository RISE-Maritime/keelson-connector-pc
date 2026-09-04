#!/usr/bin/env python3

"""Command line utility for monitoring a computer and publishing to Keelson/Zenoh.

Samples the host it runs on -- CPU, memory, storage, network interfaces,
temperatures, battery and processes -- with psutil, and publishes each quantity
as its own Keelson subject carrying a Timestamped* primitive. Runs on Linux,
macOS and Windows; metrics the platform does not expose are simply not
published rather than reported as zero.

Four cadences: --vitals-interval for the two fast-moving quantities a live
view needs (CPU load and memory use), --interval for the rest of the live
metrics, --info-interval for host identity and uptime, which barely change,
and --process-interval for the per-process top-N, whose scan is the most
expensive thing the connector does.
"""

import argparse
import logging
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
from keelson.scaffolding.liveliness import declare_liveliness

from .collectors import (
    DEFAULT_FSTYPE_EXCLUDE,
    EMITTED_SUBJECTS,
    Sampler,
    collect_host_info,
)
from .publishing import Publisher, register_subjects

logger = logging.getLogger("pc2keelson")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc2keelson",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Monitor this computer and publish its metrics to Keelson/Zenoh",
    )

    # --log-level, --mode/-m, --connect, --listen, --zenoh-config
    add_common_arguments(parser)

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
        "--vitals-interval",
        type=float,
        default=1.0,
        help="Seconds between samples of cpu_load_pct and memory_used_pct. "
        "These two are a single cheap /proc read each and move fast enough to "
        "be worth a live cadence; everything else rides --interval. Also the "
        "loop's base period, so it must not exceed --interval",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between samples of the live metrics other than the "
        "vitals above",
    )
    parser.add_argument(
        "--info-interval",
        type=float,
        default=60.0,
        help="Seconds between publishes of host identity and uptime",
    )
    parser.add_argument(
        "--process-interval",
        type=float,
        default=30.0,
        help="Seconds between per-process top-N scans. Ranking every process "
        "on the host is the connector's most expensive operation, so it runs "
        "on its own slower cadence; process_count still goes out every "
        "--interval",
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
        help="Publish the N process names using the most CPU, summed across "
        "every process sharing a name (0 = only publish process_count)",
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
    """Build the session config.

    The SDK does the layering: a --zenoh-config file (or $ZENOH_CONFIG) is the
    base and the individual flags are applied on top, so an explicit flag always
    wins. This connector used to hand-roll that because keelson 0.5.3's
    create_zenoh_config took no config-file argument.
    """
    return create_zenoh_config(
        mode=args.mode,
        connect=args.connect,
        listen=args.listen,
        zenoh_config=args.zenoh_config,
    )


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

    # Every slower cadence fires on the first pass rather than waiting a whole
    # interval, so a subscriber that joins at startup learns what machine this
    # is, and what it is busy with, at once.
    next_full = 0.0
    next_info = 0.0
    next_processes = 0.0

    # The loop ticks at --vitals-interval and everything slower is a deadline
    # off it, because the wait below is the base period: a tier can only be a
    # multiple of it, never finer.
    while not shutdown.is_requested():
        timestamp_ns = time.time_ns()
        now = time.monotonic()

        # Two cheap /proc reads, every tick. A full tick publishes these
        # alongside the rest, under the one timestamp taken above.
        readings = sampler.sample_vitals()

        if now >= next_full:
            # Ranking every process on the host is the most expensive thing
            # here, so it runs on --process-interval. process_count is cheap
            # and stays on --interval. cpu_percent() averages since the
            # previous call on each Process object, so the longer window
            # measures better, not worse.
            process_details = now >= next_processes
            readings.extend(sampler.sample(process_details=process_details))
            if process_details:
                next_processes = now + args.process_interval
            next_full = now + args.interval

        if args.host_info and now >= next_info:
            readings.extend(collect_host_info())
            next_info = now + args.info_interval

        published = publisher.publish(readings, timestamp_ns)
        logger.debug("Published %d readings", published)

        # Interruptible: a plain sleep would hold SIGTERM for a whole interval.
        shutdown.wait(timeout=args.vitals_interval)

    publisher.undeclare()


def main() -> int:
    args = build_parser().parse_args()

    setup_logging(level=args.log_level)
    zenoh.init_log_from_env_or(logging.getLevelName(args.log_level))

    for flag, value in (
        ("--vitals-interval", args.vitals_interval),
        ("--interval", args.interval),
        ("--info-interval", args.info_interval),
        ("--process-interval", args.process_interval),
    ):
        if value <= 0:
            logger.error("%s must be greater than 0", flag)
            return 2
    if args.vitals_interval > args.interval:
        # The vitals tier is the loop's base period; a "fast" tier slower than
        # the slow one would silently stretch --interval to match it.
        logger.error(
            "--vitals-interval (%s) must not exceed --interval (%s)",
            args.vitals_interval,
            args.interval,
        )
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
        # Three-tier liveliness: the source token plus one token per subject
        # this connector can publish. EMITTED_SUBJECTS is capability, not
        # activity -- a machine with no battery still advertises the battery
        # subjects, which is exactly what a subject-level token means.
        #
        # The tokens are keyed on the base --source-id, matching what the nmea
        # connector does: instances live *below* it (pc/disk/root, pc/net/eth0),
        # and they are discovered at runtime and churn -- the top-N process
        # names change every scan -- so per-instance tokens would mean
        # retracting on data absence, which subject-level liveliness forbids.
        with (
            declare_liveliness(
                session,
                args.realm,
                args.entity_id,
                args.source_id,
                pubsub_subjects=sorted(EMITTED_SUBJECTS),
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

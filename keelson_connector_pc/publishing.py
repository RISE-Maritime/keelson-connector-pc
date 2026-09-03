"""Turning readings into Keelson envelopes on Zenoh keys.

The payload type for a subject is never hard-coded here. It is looked up in the
Keelson subject registry, which makes ``subjects.yaml`` the single source of
truth: adding a subject to that file is the only step needed for the connector
to publish it with the right protobuf type, and a subject the registry does not
know fails loudly instead of going out mistyped.
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Set

import yaml
import zenoh

import keelson
from keelson.payloads.Primitives_pb2 import (
    TimestampedBool,
    TimestampedDuration,
    TimestampedFloat,
    TimestampedInt,
    TimestampedInt64,
    TimestampedString,
    TimestampedTimestamp,
)

from .collectors import Reading

logger = logging.getLogger("pc2keelson")

SUBJECTS_PATH = Path(__file__).resolve().parent / "subjects.yaml"

try:  # keelson >= 0.5.4 derives QoS from the subject in the key
    from keelson.scaffolding import declare_publisher as _declare_publisher
except ImportError:  # keelson 0.5.3, the newest release on PyPI

    def _declare_publisher(session: zenoh.Session, key: str, **kwargs):
        """Fallback for SDKs without subject-driven QoS.

        Deliberately does not set priority or congestion control: hand-tuned
        per-connector QoS is exactly what ``messages/qos.yaml`` exists to
        replace, so this takes the Zenoh defaults and the import above starts
        applying real profiles the moment a newer keelson is installed.
        """
        return session.declare_publisher(key, **kwargs)


def register_subjects() -> Set[str]:
    """Register this connector's subjects with the Keelson SDK.

    The host-metric subjects are added to keelson's own ``subjects.yaml``
    upstream, but a released SDK predating that change would log every key as
    "NOT well-known" and would not resolve a schema. Registering the bundled
    copy makes the connector correct against both. The call is an idempotent
    dict update, so it is harmless once the SDK ships them itself.

    Returns the set of subject names in the bundled file.
    """
    keelson.add_well_known_subjects_and_proto_definitions(SUBJECTS_PATH)
    with SUBJECTS_PATH.open(encoding="utf-8") as fh:
        return set(yaml.safe_load(fh) or {})


def _float(value: Any, ts: int) -> bytes:
    payload = TimestampedFloat()
    payload.timestamp.FromNanoseconds(ts)
    payload.value = float(value)
    return payload.SerializeToString()


def _int(value: Any, ts: int) -> bytes:
    payload = TimestampedInt()
    payload.timestamp.FromNanoseconds(ts)
    payload.value = int(value)
    return payload.SerializeToString()


def _int64(value: Any, ts: int) -> bytes:
    payload = TimestampedInt64()
    payload.timestamp.FromNanoseconds(ts)
    payload.value = int(value)
    return payload.SerializeToString()


def _string(value: Any, ts: int) -> bytes:
    payload = TimestampedString()
    payload.timestamp.FromNanoseconds(ts)
    payload.value = str(value)
    return payload.SerializeToString()


def _bool(value: Any, ts: int) -> bytes:
    payload = TimestampedBool()
    payload.timestamp.FromNanoseconds(ts)
    payload.value = bool(value)
    return payload.SerializeToString()


def _timestamp(value: Any, ts: int) -> bytes:
    """``value`` is nanoseconds since the epoch."""
    payload = TimestampedTimestamp()
    payload.timestamp.FromNanoseconds(ts)
    payload.value.FromNanoseconds(int(value))
    return payload.SerializeToString()


def _duration(value: Any, ts: int) -> bytes:
    """``value`` is seconds."""
    payload = TimestampedDuration()
    payload.timestamp.FromNanoseconds(ts)
    payload.value.FromNanoseconds(int(float(value) * 1e9))
    return payload.SerializeToString()


# Byte counts are the reason TimestampedInt64 appears here at all: keelson's own
# `enclose_from_integer` builds a TimestampedInt, whose int32 value field
# silently overflows past 2 GiB. Any subject carrying bytes must map to int64.
ENCODERS: Dict[str, Callable[[Any, int], bytes]] = {
    "keelson.TimestampedBool": _bool,
    "keelson.TimestampedDuration": _duration,
    "keelson.TimestampedFloat": _float,
    "keelson.TimestampedInt": _int,
    "keelson.TimestampedInt64": _int64,
    "keelson.TimestampedString": _string,
    "keelson.TimestampedTimestamp": _timestamp,
}


def encode(subject: str, value: Any, timestamp_ns: int) -> bytes:
    """Serialise ``value`` as the payload type the registry gives ``subject``,
    wrapped in a Keelson envelope stamped with the sample time."""
    schema = keelson.get_subject_schema(subject)
    encoder = ENCODERS.get(schema)
    if encoder is None:
        raise ValueError(
            f"No encoder for subject {subject!r} of type {schema!r}; "
            f"add one to publishing.ENCODERS"
        )
    return keelson.enclose(encoder(value, timestamp_ns), enclosed_at=timestamp_ns)


class Publisher:
    """Publishes readings, declaring one Zenoh publisher per key on first use."""

    def __init__(
        self,
        session: zenoh.Session,
        realm: str,
        entity_id: str,
        source_base: str,
    ):
        self.session = session
        self.realm = realm
        self.entity_id = entity_id
        self.source_base = source_base.strip("/")
        self.publishers: Dict[str, zenoh.Publisher] = {}

    def source_id(self, source_suffix: str) -> str:
        suffix = source_suffix.strip("/")
        return f"{self.source_base}/{suffix}" if suffix else self.source_base

    def key_for(self, subject: str, source_suffix: str) -> str:
        return keelson.construct_pubsub_key(
            base_path=self.realm,
            entity_id=self.entity_id,
            subject=subject,
            source_id=self.source_id(source_suffix),
        )

    def _publisher(self, key: str) -> zenoh.Publisher:
        if key not in self.publishers:
            self.publishers[key] = _declare_publisher(self.session, key)
            logger.debug("Declared publisher for %s", key)
        return self.publishers[key]

    def publish(self, readings: Iterable[Reading], timestamp_ns: int = None) -> int:
        """Publish every reading, stamped with one shared sample time.

        One bad reading must not drop the rest of the cycle, so failures are
        logged per reading. Returns the number actually published.
        """
        timestamp_ns = timestamp_ns or time.time_ns()
        published = 0
        for reading in readings:
            key = self.key_for(reading.subject, reading.source_suffix)
            try:
                self._publisher(key).put(
                    encode(reading.subject, reading.value, timestamp_ns)
                )
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to publish %s", key)
            else:
                published += 1
        return published

    def undeclare(self) -> None:
        for publisher in self.publishers.values():
            try:
                publisher.undeclare()
            except Exception:  # pylint: disable=broad-except
                logger.debug("Publisher undeclare failed", exc_info=True)
        self.publishers.clear()

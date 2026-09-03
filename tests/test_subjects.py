"""The contract between collectors.py, EMITTED_SUBJECTS and subjects.yaml.

Every subject the collectors can emit must be in the registry with a payload
type this connector knows how to encode. Without these tests a typo in a
subject name degrades quietly: construct_pubsub_key only logs a warning, and
the key still goes on the bus -- where foxglove and mcap drop it because they
cannot resolve a schema.
"""

import ast

import pytest

import keelson
from keelson_connector_pc import collectors, publishing
from keelson_connector_pc.collectors import EMITTED_SUBJECTS

# Subjects the collectors reference that are keelson's own rather than ours;
# they resolve from the SDK's bundled registry, not from our subjects.yaml.
REUSED_SUBJECTS = {
    "battery_is_charging",
    "battery_state_of_charge_pct",
    "battery_time_remaining_s",
    "device_uptime_duration",
    "integrated_circuit_temperature_celsius",
}


def subject_literals_in_source(known: set) -> set:
    """Every string literal in collectors.py that names a known subject.

    An AST walk rather than a regex, because subjects reach Reading() through
    variables too -- cpu_temperature_celsius is picked by a conditional. The
    intersection with the registry is what makes this safe: the module is full
    of other string constants (filesystem types, sensor chip names) and none of
    them are subjects.
    """
    with open(collectors.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } & known
    assert found, "found no subject literals in collectors.py to check"
    return found


@pytest.fixture(name="all_known")
def fixture_all_known(registered_subjects):
    return set(registered_subjects) | REUSED_SUBJECTS


def test_emitted_subjects_matches_the_code(all_known):
    """EMITTED_SUBJECTS is the connector's advertised publishing surface. If it
    drifts from what the collectors actually name, the liveliness tokens it
    will feed would advertise the wrong capability."""
    in_source = subject_literals_in_source(all_known)
    assert in_source - EMITTED_SUBJECTS == set(), "emitted by code, not declared"
    assert EMITTED_SUBJECTS - in_source == set(), "declared, but no code emits it"


def test_every_emitted_subject_is_well_known(registered_subjects):
    for subject in EMITTED_SUBJECTS:
        assert keelson.is_subject_well_known(subject), (
            f"{subject!r} is emitted by a collector but is in neither "
            f"subjects.yaml nor keelson's own registry"
        )


def test_every_emitted_subject_has_an_encoder(registered_subjects):
    for subject in EMITTED_SUBJECTS:
        schema = keelson.get_subject_schema(subject)
        assert schema in publishing.ENCODERS, (
            f"{subject!r} carries {schema!r}, which publishing.ENCODERS "
            f"cannot serialise"
        )


def test_bundled_subjects_are_all_reachable(registered_subjects):
    """Nothing in subjects.yaml is dead weight -- every entry is published."""
    unused = set(registered_subjects) - EMITTED_SUBJECTS
    assert (
        not unused
    ), f"subjects.yaml declares subjects nothing publishes: {sorted(unused)}"


def test_reused_subjects_come_from_keelson_not_our_file(registered_subjects):
    """We must not redefine subjects keelson already owns; two definitions of
    the same name is exactly the drift this file exists to avoid."""
    for subject in REUSED_SUBJECTS:
        assert subject not in registered_subjects, (
            f"{subject!r} is already a keelson subject; remove it from "
            f"subjects.yaml rather than shadowing it"
        )
        assert keelson.is_subject_well_known(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "memory_total_bytes",
        "disk_used_bytes",
        "network_interface_rx_bytes_total",
        "process_memory_used_bytes",
    ],
)
def test_byte_counts_are_int64(subject, registered_subjects):
    """A byte count on a TimestampedInt (int32) overflows silently past 2 GiB,
    which is ordinary RAM. Pin the type so nobody 'simplifies' it back."""
    assert keelson.get_subject_schema(subject) == "keelson.TimestampedInt64"

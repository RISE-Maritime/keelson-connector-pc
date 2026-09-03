# Contributing

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
```

## Before opening a pull request

```bash
black bin keelson_connector_pc tests
pylint bin keelson_connector_pc
uv run pytest
```

CI runs the suite on Linux, macOS and Windows. That matrix is not decoration:
the platform differences this connector works around — `sensors_temperatures`
being undefined off Linux, `cpu_freq()` returning placeholders on Apple
Silicon, Windows drive letters as mountpoints — only surface if all three
actually run the tests.

## Adding a metric

1. Add the subject to `keelson_connector_pc/subjects.yaml`, following
   `<entity>_<property>_<unit>` and mapping it to an existing
   `keelson.Timestamped*` type. Byte counts must use `TimestampedInt64`.
2. Emit a `Reading` for it from a collector in `collectors.py`.
3. Add it to `collectors.EMITTED_SUBJECTS`.
4. Run `pytest tests/test_subjects.py` — it checks all three agree.
5. Add the same subject to `keelson/messages/subjects.yaml` upstream and
   regenerate the SDKs, or downstream consumers will drop the key.

If the value needs a payload type the connector has not used before, add an
encoder to `publishing.ENCODERS`; the test suite will tell you if you forgot.

## Updating the README's `--help` block

Captured, not typed, so it cannot drift:

```bash
COLUMNS=100 uv run bin/pc2keelson.py --help
```

Paste the output into the fenced block under `## pc2keelson`.

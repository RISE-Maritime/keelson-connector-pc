"""Shared fixtures.

The connector's subjects are registered once for the whole session: the SDK's
registry is process-global module state, and every test that resolves a schema
or builds a key depends on it.
"""

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def registered_subjects():
    """Register the bundled subjects.yaml before anything looks a subject up."""
    from keelson_connector_pc.publishing import register_subjects

    return register_subjects()


@pytest.fixture(scope="session")
def pc2keelson():
    """The entry point, which is a module in the package.

    It used to be loaded by path from `bin/`, because the script was a
    standalone executable. It is now `keelson_connector_pc.cli`, which is what
    `[project.scripts]` turns into the `pc2keelson` command, so an ordinary
    import tests the same code the container runs.
    """
    from keelson_connector_pc import cli

    return cli

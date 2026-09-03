"""Shared fixtures.

The connector's subjects are registered once for the whole session: the SDK's
registry is process-global module state, and every test that resolves a schema
or builds a key depends on it.
"""

import importlib.util
import pathlib
import sys
from importlib.machinery import SourceFileLoader

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
    """The entry-point script, loaded as a module.

    `bin/pc2keelson.py` is a standalone executable rather than part of the
    package, so it is loaded by path -- the same approach the keelson monorepo
    connectors use for their `bin/` scripts.
    """
    path = REPO_ROOT / "bin" / "pc2keelson.py"
    loader = SourceFileLoader("pc2keelson", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

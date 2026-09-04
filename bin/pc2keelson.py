#!/usr/bin/env python3

"""Checkout-convenience wrapper around ``keelson_connector_pc.cli``.

The real entry point is the ``pc2keelson`` console script that setuptools
generates from ``[project.scripts]``; that is what the container runs. This
file exists so ``python bin/pc2keelson.py`` keeps working straight out of a
checkout, before anything has been installed.
"""

import pathlib
import sys

# Guarded because the installed copy would otherwise put /usr/local's lib/,
# bin/ and share/ directories at the front of sys.path as namespace packages,
# ahead of every real module.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "keelson_connector_pc" / "__init__.py").is_file():
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable-next=wrong-import-position
from keelson_connector_pc.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

"""Put `cloud/` on the import path.

The adapter is not installed as a distribution, and it deliberately has no
entry in the project's pyproject.toml, so the suite has to say where it is.
Doing it here, imported first by every test module, means the suite runs the
same way under `python -m unittest discover -s cloud/tests` from the
repository root, under pytest, and from inside the container image, without a
PYTHONPATH set by whoever happens to be invoking it.
"""

from __future__ import annotations

import sys
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

REPO = CLOUD.parent

"""Cloud adapter for completed measurement runs.

This package is deliberately separate from the ``scalar_geometry`` runtime
package and from its dependency set. It reads a finished run directory, uploads
it once under an immutable prefix, and loads compact records into a BigQuery
ledger. It computes no embeddings and no geometry.

Nothing here imports a Google client library at module scope. The pure parts
(reading a run, building rows, deciding significance) are importable and
testable with the standard library alone; the two cloud-backed classes import
their clients inside their constructors. That is why the test suite needs no
credentials, no network and no SDK.
"""

from .errors import AdapterError, Exit

__all__ = ["AdapterError", "Exit"]

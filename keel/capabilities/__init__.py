"""Importing this package registers every tool.

Registration is an import-time side effect on purpose: the prohibition in
`register()` therefore runs at collection time in CI, before any test does.
"""

from . import catalog, github  # noqa: F401

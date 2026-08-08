"""Compatibility shims required before importing madmom.

madmom's last release predates both the ``collections`` ABC move (Python 3.10)
and NumPy's removal of its scalar type aliases (NumPy 1.24). It still works
once those names are restored, so they are patched back in here.

Import this module *before* anything that imports madmom::

    from ._madmom_compat import madmom  # noqa: F401
"""

from __future__ import annotations

import collections
import collections.abc

# Restore the ABCs madmom expects on ``collections`` itself.
for _name in ("MutableSequence", "Sequence", "Iterable", "Callable", "MutableMapping"):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

import numpy as np  # noqa: E402

# Restore NumPy's removed scalar aliases. Membership in ``__dict__`` is checked
# rather than ``hasattr``, because NumPy's module-level ``__getattr__`` raises a
# FutureWarning for exactly these names.
for _alias, _builtin in (
    ("float", float),
    ("int", int),
    ("bool", bool),
    ("object", object),
    ("complex", complex),
    ("str", str),
):
    if _alias not in np.__dict__:
        np.__dict__[_alias] = _builtin

try:
    import madmom  # noqa: E402,F401
except Exception as exc:  # pragma: no cover - depends on the environment
    madmom = None
    MADMOM_IMPORT_ERROR: Exception | None = exc
else:
    MADMOM_IMPORT_ERROR = None


def require_madmom():
    """Return the madmom module, raising a clear error if it is unavailable."""
    if madmom is None:
        raise RuntimeError(
            "madmom is not available in this environment "
            f"({MADMOM_IMPORT_ERROR}). Beat tracking and the madmom chord "
            "engine will be skipped."
        ) from MADMOM_IMPORT_ERROR
    return madmom

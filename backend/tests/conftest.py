"""Shared test configuration.

Puts the backend directory on the import path so tests can import
``vidichord`` without the package being installed.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

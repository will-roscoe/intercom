"""Test bootstrap.

Home Assistant is not a dependency of this repository, so `tests/stubs` provides
just enough of it to import and drive the integration. The stubs go on the path
before anything else, which lets the tests import the real
`custom_components/intercom` package normally.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests" / "stubs"))
sys.path.insert(0, str(ROOT / "custom_components"))

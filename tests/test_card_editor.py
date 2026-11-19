"""Run the card's JavaScript tests (``tests/card``) with Node's built-in runner.

The card has no build step or packages, so its tests use ``node --test`` alone.
They are skipped where Node is not installed; GitHub's runners have it.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

CARD_TESTS = Path(__file__).parent / "card"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_card_editor():
    result = subprocess.run(
        [NODE, "--test", str(CARD_TESTS / "editor.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

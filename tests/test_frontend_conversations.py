import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_conversation_handlers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Frontend behavior tests require Node.js")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("frontend_conversations.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

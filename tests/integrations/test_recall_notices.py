"""Current-turn operational status for the DSH plugin; no real host/profile/model."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest


def test_dsh_notices_and_session_isolation():
    node = os.environ.get("NODE_BIN") or shutil.which("node")
    if not node:
        pytest.skip("Node runtime unavailable")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([node, str(root / "tests/integrations/node/recall_notices.mjs"),
                             str(root / "src/dma/integrations/dsh/plugin/unified-memory.mjs")],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True and len(payload["checks"]) == 7
    assert payload["externalLlmCalls"] == 0

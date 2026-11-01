import json,os,shutil,subprocess
from pathlib import Path
import pytest


def test_dsh_active_search_uses_scoped_api_without_writes():
 node=os.environ.get("NODE_BIN") or shutil.which("node")
 if not node:pytest.skip("Node unavailable")
 root=Path(__file__).parents[2]
 args=[node,str(root/"tests/integrations/node/active_search.mjs"),str(root/"src/dma/integrations/dsh/plugin/unified-memory.mjs")]
 result=subprocess.run(args,capture_output=True,text=True,timeout=15)
 assert result.returncode==0,result.stdout+result.stderr
 data=json.loads(result.stdout);assert data["passed"] and data["externalLlmCalls"]==0 and len(data["checks"])==7

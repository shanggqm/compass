"""Fixed entrypoint for untrusted Python; isolation is provided by the container runtime."""

import json
import os
import re
import resource
import sys
from pathlib import Path

os.umask(0o077)
resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024, 512 * 1024))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
root = Path("/workspace")
if len(sys.argv) > 1:
    if not re.fullmatch(r"[0-9a-f]{32}", sys.argv[1]):
        raise ValueError("invalid invocation")
    root = root / "executions" / sys.argv[1]
os.chdir(root)
source = (root / "source.py").read_text()
if len(source.encode()) > 16 * 1024:
    raise ValueError("source limit")
raw = (root / "input.json").read_bytes()
if len(raw) > 128 * 1024:
    raise ValueError("input limit")
namespace = {
    "__name__": "__main__",
    "data": json.loads(raw),
    "result": None,
    "output_dir": str(root),
    "shared_dir": "/workspace/shared",
    "chart_path": str(root / "chart.png"),
}
with open(root / "stdout.log", "w") as log:
    sys.stdout = sys.stderr = log
    exec(compile(source, str(root / "source.py"), "exec"), namespace)
    payload = json.dumps(namespace["result"], ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > 64 * 1024:
        raise ValueError("result limit")
    (root / "result.json").write_bytes(payload)

#!/usr/bin/env python3
import os
import subprocess
import sys

def main() -> int:
    print("Codelab development container started.", flush=True)
    print(f"Workspace: {os.environ.get('WORKSPACE', '/workspace')}", flush=True)
    try:
        return subprocess.call(["sleep", "infinity"])
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    sys.exit(main())

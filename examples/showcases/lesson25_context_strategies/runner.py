"""Run the isolated AC-05 experiment from a repository checkout."""

import argparse
import os
import sys
from pathlib import Path

import pytest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="work/983-context-strategies.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "apps" / "agent"))
    os.environ["COMPASS_983_STRATEGY_REPORT"] = str((root / args.output).resolve())
    return pytest.main([str(Path(__file__).parent / "tests"), "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())

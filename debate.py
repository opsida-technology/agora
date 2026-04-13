#!/usr/bin/env python3
"""
debate.py — CLI entry point for the Agora Protocol debate orchestrator.

Usage:
    python debate.py configs/mlx_offline.yaml
    python debate.py configs/hybrid.yaml --no-stream
"""
import argparse
from agora import run


def main():
    ap = argparse.ArgumentParser(
        description="Agora Protocol — Multi-agent debate")
    ap.add_argument("config", help="Path to YAML config")
    ap.add_argument("--no-stream", action="store_true",
                    help="Disable streaming (batch mode)")
    args = ap.parse_args()
    run(args.config, stream=not args.no_stream)


if __name__ == "__main__":
    main()

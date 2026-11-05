#!/usr/bin/env python3
"""Launcher for the catlink-local gateway (box-facing MQTT broker + Home Assistant bridge).

Runs under `python3 -I` (isolated mode, so the script directory is not on sys.path): the package next
to this file is added explicitly. See catlink_local/gateway.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catlink_local.gateway import main  # noqa: E402

if __name__ == "__main__":
    main()

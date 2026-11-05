#!/usr/bin/env python3
"""Launcher for the catlink-local HTTPS bootstrap responder.

Runs under `python3 -I` (isolated mode, so the script directory is not on sys.path): the package next
to this file is added explicitly. See catlink_local/authresp.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catlink_local.authresp import main  # noqa: E402

if __name__ == "__main__":
    main()

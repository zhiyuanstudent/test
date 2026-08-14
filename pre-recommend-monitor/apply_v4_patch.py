#!/usr/bin/env python3
"""Compatibility no-op for the retired schema-v4 patch workflow."""
from pathlib import Path

monitor = Path(__file__).with_name("monitor_v3.py")
text = monitor.read_text(encoding="utf-8")
if '"schema_version": 4' not in text:
    raise SystemExit("Monitor is not on schema v4")
print("Monitor is already on schema v4; no changes required.")

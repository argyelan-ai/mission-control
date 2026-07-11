"""Make the mc-playwright service modules importable without installing them.

These tests are pure-function level (request validation + ffmpeg command
builders) and run in the backend venv:
    cd backend && python -m pytest ../docker/mc-playwright/tests/test_media.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

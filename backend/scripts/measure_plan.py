"""Planner experiment wrapper around the shared durable service."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.bench import main

if __name__ == "__main__":
    raise SystemExit(main(default_variant="plan"))

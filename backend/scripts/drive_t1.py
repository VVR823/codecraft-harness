"""Legacy T1 entry delegates to the shared CLI (offline by default)."""

from drive_task import main

if __name__ == "__main__":
    raise SystemExit(main())

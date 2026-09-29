"""Sélectionne le créneau UTC correspondant à 07:00 à Sydney."""
from __future__ import annotations

import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo


def should_run(schedule: str, now: dt.datetime | None = None) -> bool:
    if not schedule:
        return True  # workflow_dispatch
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo("Australia/Sydney"))
    expected_utc_hour = 20 if local.utcoffset() == dt.timedelta(hours=11) else 21
    return schedule == f"0 {expected_utc_hour} * * *"


if __name__ == "__main__":
    result = should_run(sys.argv[1] if len(sys.argv) > 1 else "")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"run={'true' if result else 'false'}\n")
    print("Créneau Sydney retenu" if result else "Créneau Sydney ignoré")

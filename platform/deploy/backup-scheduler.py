#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("PHDEBATE_ROOT", "/home/ubuntu/sunsq/phdebate"))
BACKUP_DIR = Path(os.environ.get("PHDEBATE_BACKUP_DIR", ROOT / "runtime/backups"))
BACKUP_SCRIPT = ROOT / "deploy/backup-database.sh"
TIMEZONE = ZoneInfo("Asia/Shanghai")
HOUR = 3
MINUTE = 17

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("jixia-backup")


def latest_backup_age_hours() -> float | None:
    backups = list(BACKUP_DIR.glob("auto-*.dump")) if BACKUP_DIR.exists() else []
    if not backups:
        return None
    latest = max(item.stat().st_mtime for item in backups)
    return max(0.0, (time.time() - latest) / 3600)


def run_backup() -> bool:
    try:
        result = subprocess.run([str(BACKUP_SCRIPT)], check=True, text=True, capture_output=True, timeout=1800)
        logger.info(result.stdout.strip())
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("backup_failed exit=%s stdout=%s stderr=%s", exc.returncode, exc.stdout.strip(), exc.stderr.strip())
    except Exception:
        logger.exception("backup_failed")
    return False


def run_with_retry() -> None:
    while not run_backup():
        time.sleep(300)


def next_run(now: datetime) -> datetime:
    target = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    age = latest_backup_age_hours()
    if age is None or age >= 20:
        run_with_retry()
    while True:
        current = datetime.now(TIMEZONE)
        target = next_run(current)
        wait_seconds = max(1.0, (target - current).total_seconds())
        logger.info("next_backup_at=%s", target.isoformat())
        time.sleep(wait_seconds)
        run_with_retry()


if __name__ == "__main__":
    main()

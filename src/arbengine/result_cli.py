from __future__ import annotations

import argparse
import json
import os

from arbengine.result_archive import run_result_cycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive settled Betfair event outcomes into SQLite.")
    parser.add_argument("--db", default=os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))
    parser.add_argument("--max-age-hours", type=int, default=72)
    parser.add_argument("--limit-events", type=int, default=100)
    args = parser.parse_args()
    result = run_result_cycle(args.db, max_age_hours=max(1, args.max_age_hours), limit_events=max(1, args.limit_events))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

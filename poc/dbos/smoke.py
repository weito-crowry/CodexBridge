from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbos import DBOS, DBOSConfig
from sqlalchemy.engine import URL


@DBOS.step()
def step_one() -> int:
    return 1


@DBOS.step()
def step_two(value: int) -> int:
    return value + 2


@DBOS.step()
def step_three(value: int) -> int:
    return value * 2


@DBOS.workflow()
def three_step_workflow() -> dict[str, object]:
    first = step_one()
    second = step_two(first)
    result = step_three(second)
    return {
        "workflow_id": DBOS.workflow_id,
        "steps": ["step_one", "step_two", "step_three"],
        "value": result,
    }


def run_smoke(database_path: Path) -> dict[str, object]:
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    system_database_url = URL.create("sqlite", database=database_path.as_posix()).render_as_string(
        hide_password=False
    )
    config: DBOSConfig = {
        "name": "codexbridge-dbos-poc",
        "application_version": "0.1.0",
        "system_database_url": system_database_url,
    }

    DBOS(config=config)
    try:
        DBOS.launch()
        return three_step_workflow()
    finally:
        DBOS.destroy()


def main() -> int:
    default_database = Path(__file__).resolve().parent / ".state" / "dbos-poc.sqlite"
    parser = argparse.ArgumentParser(description="Run the isolated DBOS SQLite smoke.")
    parser.add_argument("--db-path", type=Path, default=default_database)
    args = parser.parse_args()

    print(json.dumps(run_smoke(args.db_path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

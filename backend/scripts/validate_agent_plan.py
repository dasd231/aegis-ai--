from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.agent_plan_validator import validate_agent_plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Agent 变更计划的证据、DAG 与并行写集")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--service-cards", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    cards = json.loads(args.service_cards.read_text(encoding="utf-8"))
    result = validate_agent_plan(plan, cards)
    print(
        json.dumps(
            {
                "valid": result.valid,
                "errors": result.errors,
                "execution_layers": result.execution_layers,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())

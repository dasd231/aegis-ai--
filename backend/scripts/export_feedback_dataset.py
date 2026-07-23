from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.services.feedback_dataset import build_feedback_dataset  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="导出脱敏反馈样本；输出仅供人工审核，不会自动修改线上 Prompt。"
    )
    parser.add_argument("--output", type=Path, required=True, help="目标 JSONL 文件")
    parser.add_argument("--rating", type=int, choices=(-1, 1), default=-1)
    parser.add_argument("--limit", type=int, default=5000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 100_000:
        raise SystemExit("--limit 必须在 1 到 100000 之间")
    with SessionLocal() as db:
        samples = build_feedback_dataset(db, rating=args.rating, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"已导出 {len(samples)} 条脱敏样本到 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

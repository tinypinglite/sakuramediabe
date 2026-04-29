#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根目录直接运行脚本：poetry run python scripts/...
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.config import settings
from src.metadata.javdb import JavdbProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探索 JavDB 热评接口 /api/v1/reviews/hotly 的实际返回结构"
    )
    parser.add_argument(
        "--host",
        default=settings.metadata.javdb_host,
        help="JavDB host（默认读取 settings.metadata.javdb_host）",
    )
    parser.add_argument(
        "--proxy",
        default=settings.metadata.proxy,
        help="HTTP 代理地址，默认读取 settings.metadata.proxy",
    )
    parser.add_argument(
        "--period",
        default="weekly",
        choices=sorted(JavdbProvider.SUPPORTED_HOT_REVIEW_PERIODS),
        help="热评周期",
    )
    parser.add_argument("--page", type=int, default=1, help="页码（从 1 开始）")
    parser.add_argument("--limit", type=int, default=24, help="每页条数")
    parser.add_argument(
        "--max-items",
        type=int,
        default=3,
        help="仅预览前 N 条 reviews（默认 3）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    provider = JavdbProvider(host=args.host, proxy=args.proxy)

    # 直接走 provider 的请求链路，确保 jdsignature 与线上调用一致。
    payload, url = provider._get_hot_reviews_payload(
        period=args.period,
        page=args.page,
        limit=args.limit,
    )

    print("=== Request ===")
    print(f"url: {url}")
    print()

    data = payload.get("data") if isinstance(payload, dict) else None
    reviews = data.get("reviews") if isinstance(data, dict) else None

    print("=== Payload Summary ===")
    print(f"success: {payload.get('success') if isinstance(payload, dict) else None}")
    print(f"message: {payload.get('message') if isinstance(payload, dict) else None}")
    print(f"reviews_count: {len(reviews) if isinstance(reviews, list) else 'N/A'}")
    if isinstance(reviews, list) and reviews:
        print(f"first_review_keys: {list(reviews[0].keys())}")
    print()

    print("=== Payload (full) ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()

    # 额外打印前 N 条，便于快速肉眼确认字段。
    if isinstance(reviews, list) and reviews:
        preview_items = reviews[: max(0, args.max_items)]
        print(f"=== Reviews Preview (first {len(preview_items)}) ===")
        print(json.dumps(preview_items, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

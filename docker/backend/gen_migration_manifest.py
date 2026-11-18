"""生成迁移模块名清单，供 Nuitka 编译产物在运行时导入。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: gen_migration_manifest.py <versions-dir> <output>",
            file=sys.stderr,
        )
        return 2
    versions_dir = Path(sys.argv[1])
    output = Path(sys.argv[2])
    names = sorted(
        path.stem
        for path in versions_dir.glob("*.py")
        if path.name != "__init__.py"
    )
    output.write_text(
        json.dumps(names, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(names)} migration names to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

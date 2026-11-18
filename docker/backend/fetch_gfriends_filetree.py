"""Download the GFriends Filetree JSON and store it as a gzip seed for the image."""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.request


def main() -> None:
    url = os.environ["GFRIENDS_FILETREE_URL"]
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = json.load(response)
            with gzip.open(
                "/out/gfriends-filetree.json.gz",
                "wt",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(5)
    raise SystemExit(f"failed to download GFriends filetree: {last_error}")


if __name__ == "__main__":
    main()

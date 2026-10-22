"""Fetch poster URLs for titles that have none yet -> images.json

Uses poster.py (AniList, then MyAnimeList, then Kitsu - trusted sources only).

Usage:
    python fetch_images.py            # only titles with no poster yet
    python fetch_images.py --retry    # also retry titles that failed before
Resumable: progress is saved as it goes, Ctrl+C is safe.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from data import TITLES
from poster import lookup, lookup_batch

OUT = Path(__file__).parent / "images.json"


def main():
    retry = "--retry" in sys.argv
    cache = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    todo = [x for x in TITLES if not cache.get(x["uid"]) and (retry or x["uid"] not in cache)]
    print(f"{len(todo)} titles to fetch ({sum(1 for v in cache.values() if v)} already have a poster)")

    net_fail = 0
    pool = ThreadPoolExecutor(max_workers=4)
    pre = {}                                  # fast pass: 8 titles per AniList request
    for i in range(0, len(todo), 8):
        pre.update(lookup_batch([x["title"] for x in todo[i:i + 8]]))
        print(f"quick pass {min(i + 8, len(todo))}/{len(todo)} ({len(pre)} matched)")
    futs = {pool.submit(lambda t: pre.get(t) or lookup(t), x["title"]): x for x in todo}
    try:
        for n, fut in enumerate(as_completed(futs), 1):
            x = futs[fut]
            m = fut.result()
            if m is None:
                net_fail += 1
                print(f"[{n}/{len(todo)}] NET  {x['title']}")
                if net_fail >= 5:
                    print("Cannot reach AniList / MyAnimeList / Kitsu. Check your internet, DNS (8.8.8.8) or use a VPN, then run again.")
                    break
                continue
            net_fail = 0
            cache[x["uid"]] = m.get("image", "") if m else ""
            print(f"[{n}/{len(todo)}] {'OK  ' if m else 'MISS'} {x['title']}" + (f"  ({m['source']})" if m else ""))
            if n % 10 == 0:
                OUT.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except KeyboardInterrupt:
        print("\nStopped, saving progress...")
    finally:
        for fu in futs:
            fu.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    OUT.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    print(f"Saved. {sum(1 for v in cache.values() if v)} posters in {OUT.name}")


if __name__ == "__main__":
    main()

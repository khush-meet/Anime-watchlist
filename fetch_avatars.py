"""Download the famous character pictures -> static/avatars/<key>.jpg

Usage:  python fetch_avatars.py

Tries AniList, Kitsu and Jikan (MyAnimeList). It first checks which of them your
network can reach and only uses those. Files already downloaded are skipped.

If NONE can be reached (firewall / DNS / ISP block), see the tips it prints, or
just put the pictures in static/avatars/ yourself, named like  gojo.jpg, naruto.jpg ...
(the names are the 'key' values in avatars.py).
"""
import sys
import time
from pathlib import Path

import requests

from avatars import BUILTIN as AVATARS

OUT = Path(__file__).parent / "static" / "avatars"
OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (guidetoanime student project)"
TIMEOUT = 10


class NetError(Exception):
    pass


def _req(method, url, **kw):
    try:
        return S.request(method, url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        raise NetError(f"{type(e).__name__}")


# ---------------- providers: each returns an image URL or "" ----------------
def anilist(name):
    q = "query($s:String){Character(search:$s){image{large}}}"
    r = _req("POST", "https://graphql.anilist.co", json={"query": q, "variables": {"s": name}})
    if r.status_code != 200:
        return ""
    return (((r.json().get("data") or {}).get("Character") or {}).get("image") or {}).get("large", "")


def kitsu(name):
    r = _req("GET", "https://kitsu.io/api/edge/characters",
             params={"filter[name]": name, "page[limit]": 1},
             headers={"Accept": "application/vnd.api+json"})
    if r.status_code != 200:
        return ""
    data = r.json().get("data") or []
    img = (data[0]["attributes"].get("image") or {}) if data else {}
    return img.get("original") or img.get("large") or ""


def jikan(name):
    r = _req("GET", "https://api.jikan.moe/v4/characters",
             params={"q": name, "order_by": "favorites", "sort": "desc", "limit": 1})
    if r.status_code != 200:
        return ""
    data = r.json().get("data") or []
    return data[0]["images"]["jpg"]["image_url"] if data else ""


PROVIDERS = {"anilist": anilist, "kitsu": kitsu, "jikan": jikan}
DELAY = {"anilist": 1.0, "kitsu": 0.5, "jikan": 1.2}


def main():
    print("Checking which services your network can reach...")
    alive = []
    for n, fn in PROVIDERS.items():
        try:
            fn("Naruto Uzumaki")
            print(f"  {n}: OK")
            alive.append(n)
        except NetError as e:
            print(f"  {n}: BLOCKED ({e})")
    if not alive:
        print("\nCannot reach any picture service from this network. Try:")
        print("  1. Change DNS to 8.8.8.8 / 1.1.1.1 (or use the 1.1.1.1 / WARP app)")
        print("  2. Use a mobile hotspot or a VPN, then run this script again")
        print("  3. Allow Python through your firewall / antivirus")
        print("  4. Or add the pictures by hand: save each one as static/avatars/<key>.jpg")
        print("     Needed names:", ", ".join(a["key"] + ".jpg" for a in AVATARS))
        sys.exit(1)

    missing = 0
    for a in AVATARS:
        dest = OUT / f"{a['key']}.jpg"
        if dest.exists() and dest.stat().st_size > 1000:
            print("have ", a["name"])
            continue
        url = ""
        for n in list(alive):
            try:
                url = PROVIDERS[n](a["name"])
            except NetError as e:
                print(f"  {n} failed ({e})")
            time.sleep(DELAY[n])
            if url:
                break
        if not url:
            print("MISS ", a["name"])
            missing += 1
            continue
        try:
            r = _req("GET", url)
            dest.write_bytes(r.content)
            print("OK   ", a["name"])
        except NetError as e:
            print("FAIL ", a["name"], e)
            missing += 1
    print(f"\nDone. {len(AVATARS) - missing}/{len(AVATARS)} pictures are in {OUT}")
    if missing:
        print("Run the script again to retry the missing ones.")


if __name__ == "__main__":
    main()

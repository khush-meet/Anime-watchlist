"""Find a poster + basic details for an anime title from TRUSTED sources only.

Sources, tried in this order:
  1. AniList        (graphql.anilist.co)
  2. MyAnimeList    (through the Jikan API, api.jikan.moe)
  3. Kitsu          (kitsu.io)

Safety rules built in:
  * A result is accepted only if its title really looks like the one we searched
    (fuzzy match), so "Naruto" will not get the poster of some other show.
  * A poster link is accepted only if it points to the official image servers of
    those three sites (see TRUSTED_IMAGE_HOSTS).

lookup(title) returns:
    {"image":..., "year":..., "eps":..., "mins":..., "genres":[...], "source":...}   found
    {}                                                                                 not found
    None                                                                               network problem
"""
import difflib
import re
import threading
import time
from urllib.parse import urlparse

import requests

HEADERS = {"User-Agent": "GuideToAnime/1.0 (student project)"}
TIMEOUT = 20
MIN_SCORE = 0.62

TRUSTED_IMAGE_HOSTS = ("anilist.co", "myanimelist.net", "kitsu.io", "kitsu.app")

_session = requests.Session()
_session.headers.update(HEADERS)


class NetError(Exception):
    """Could not reach the server at all (DNS, firewall, no internet ...)."""


# ------------------------------------------------------------- title helpers
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_JUNK = re.compile(r"\b(hindi|tamil|telugu|dubbed|dub|dual audio|multi audio|bluray|blu ray|"
                   r"web dl|webrip|1080p|720p|480p|download|episodes?|complete|uncut)\b", re.I)
_SEASON = re.compile(r"\b(season|part|cour)\s*\d+\b|\b\d+(st|nd|rd|th)\s+season\b", re.I)


def variants(title):
    """Search strings, most exact first. Season-less versions come last as a fallback."""
    out = []

    def add(s):
        s = re.sub(r"\s+", " ", s).strip(" -:")
        if s and s.lower() not in [o.lower() for o in out]:
            out.append(s)

    base = _JUNK.sub(" ", title)
    add(base)
    no_paren = re.sub(r"\([^)]*\)", " ", base)
    add(no_paren)
    no_season = _SEASON.sub(" ", no_paren)
    add(no_season)
    if ":" in no_season:                      # "Mushoku Tensei: Jobless Reincarnation" -> "Mushoku Tensei"
        add(no_season.split(":")[0])
    return out[:4]


def score(query, names):
    """0..1 how well the best of `names` matches `query`."""
    q = norm(query)
    best = 0.0
    for n in names:
        n = norm(n)
        if not n or not q:
            continue
        r = difflib.SequenceMatcher(None, q, n).ratio()
        if q in n or n in q:                  # one contains the other (extra words like "The Animation")
            r = max(r, 0.8 * min(len(q), len(n)) / max(len(q), len(n)) + 0.2)
        best = max(best, r)
    return best


def trusted(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_IMAGE_HOSTS)


# ------------------------------------------------------------------ providers
def _get(method, url, **kw):
    for attempt in range(3):
        try:
            r = _session.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise NetError(type(e).__name__)
        if r.status_code == 429:               # rate limited: wait and retry
            try:
                wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
            except ValueError:
                wait = 5 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        return r
    return None


_AL_FIELDS = "title{romaji english} synonyms coverImage{extraLarge large} episodes duration seasonYear genres"
ANILIST_Q = "query($s:String){Page(perPage:6){media(search:$s,type:ANIME,sort:SEARCH_MATCH){" + _AL_FIELDS + "}}}"


def _al_candidates(media_list):
    out = []
    for m in media_list or []:
        t = m.get("title") or {}
        c = m.get("coverImage") or {}
        out.append(dict(names=[t.get("romaji"), t.get("english")] + (m.get("synonyms") or []),
                        image=c.get("large") or c.get("extraLarge") or "",
                        year=m.get("seasonYear"), eps=m.get("episodes"), mins=m.get("duration"),
                        genres=m.get("genres") or [], source="AniList"))
    return out


def anilist(q):
    r = _get("POST", "https://graphql.anilist.co", json={"query": ANILIST_Q, "variables": {"s": q}})
    if r is None or r.status_code != 200:
        return []
    return _al_candidates(((r.json().get("data") or {}).get("Page") or {}).get("media"))


def anilist_batch(queries):
    """ONE request for several search strings (GraphQL aliases). Returns a list of candidate lists, same order."""
    decl = ",".join(f"$s{i}:String" for i in range(len(queries)))
    body = " ".join(f"a{i}:Page(perPage:4){{media(search:$s{i},type:ANIME,sort:SEARCH_MATCH){{{_AL_FIELDS}}}}}"
                    for i in range(len(queries)))
    r = _get("POST", "https://graphql.anilist.co",
             json={"query": f"query({decl}){{{body}}}", "variables": {f"s{i}": q for i, q in enumerate(queries)}})
    if r is None or r.status_code != 200:
        return [[] for _ in queries]
    data = r.json().get("data") or {}
    return [_al_candidates(((data.get(f"a{i}") or {}).get("media"))) for i in range(len(queries))]


def jikan(q):
    r = _get("GET", "https://api.jikan.moe/v4/anime", params={"q": q, "limit": 6, "sfw": "true"})
    if r is None or r.status_code != 200:
        return []
    out = []
    for m in r.json().get("data") or []:
        jpg = (m.get("images") or {}).get("jpg") or {}
        mins = re.search(r"(\d+)\s*min", m.get("duration") or "")
        out.append(dict(names=[m.get("title"), m.get("title_english")] + [x.get("title") for x in m.get("titles") or []],
                        image=jpg.get("large_image_url") or jpg.get("image_url") or "",
                        year=m.get("year"), eps=m.get("episodes"), mins=int(mins.group(1)) if mins else None,
                        genres=[g["name"] for g in m.get("genres") or []], source="MyAnimeList"))
    return out


def kitsu(q):
    r = _get("GET", "https://kitsu.io/api/edge/anime",
             params={"filter[text]": q, "page[limit]": 6}, headers={"Accept": "application/vnd.api+json"})
    if r is None or r.status_code != 200:
        return []
    out = []
    for m in r.json().get("data") or []:
        a = m.get("attributes") or {}
        t = a.get("titles") or {}
        p = a.get("posterImage") or {}
        try:
            year = int((a.get("startDate") or "")[:4])
        except ValueError:
            year = None
        out.append(dict(names=[a.get("canonicalTitle")] + list(t.values()) + (a.get("abbreviatedTitles") or []),
                        image=p.get("large") or p.get("original") or "",
                        year=year, eps=a.get("episodeCount"), mins=a.get("episodeLength"),
                        genres=[], source="Kitsu"))
    return out


PROVIDERS = {"anilist": anilist, "jikan": jikan, "kitsu": kitsu}

# Minimum gap between two requests to the same site (shared by all threads), so
# several titles can be fetched at once without breaking the sites' rate limits.
MIN_GAP = {"anilist": 0.7, "jikan": 1.0, "kitsu": 0.3}
_rate_lock = threading.Lock()
_next_slot = {}


def _throttle(name):
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(name, 0.0))
        _next_slot[name] = slot + MIN_GAP[name]
    if slot > now:
        time.sleep(slot - now)


# ---------------------------------------------------------------- main entry
def lookup(title, providers=None):
    """Best trusted match for `title` (see module docstring for the return values)."""
    names = providers or list(PROVIDERS)
    net_errors = 0
    best, best_score = None, 0.0
    for vi, q in enumerate(variants(title)):
        for name in names:
            if name != names[0] and vi >= 2:   # backup sites: only the 2 most exact search strings
                continue
            _throttle(name)
            try:
                results = PROVIDERS[name](q)
            except NetError:
                net_errors += 1
                continue
            for c in results:
                if not c["image"] or not trusted(c["image"]):
                    continue
                s = score(q, [n for n in c["names"] if n])
                if s > best_score:
                    best, best_score = c, s
            if best and best_score >= 0.9:     # very good match, stop early
                return _clean(best)
        if best and best_score >= MIN_SCORE:   # good enough from this search string
            return _clean(best)
    if net_errors and not best:
        return None                            # network trouble, try again later
    return {}


def lookup_batch(titles):
    """Fast pass: ask AniList about several titles in ONE request.
    Returns {title: result} only for titles that matched very well; for the rest use lookup()."""
    out = {}
    qs = [(variants(t) or [t])[0] for t in titles]
    _throttle("anilist")
    try:
        groups = anilist_batch(qs)
    except NetError:
        return out
    for t, q, cands in zip(titles, qs, groups):
        best, best_score = None, 0.0
        for c in cands:
            if not c["image"] or not trusted(c["image"]):
                continue
            sc = score(q, [n for n in c["names"] if n])
            if sc > best_score:
                best, best_score = c, sc
        if best and best_score >= 0.85:
            out[t] = _clean(best)
    return out


def _clean(c):
    return {k: c[k] for k in ("image", "year", "eps", "mins", "genres", "source")}


if __name__ == "__main__":
    # Quick test:  python poster.py            -> tries 3 sample titles
    #              python poster.py "Blue Box" -> tries your title
    import sys
    tests = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else [
        "Blue Box Season 2", "Naruto", "Re:Zero - Starting Life in Another World Season 4"]
    for t in tests:
        print("Searching:", t)
        m = lookup(t)
        if m is None:
            print("  Cannot reach AniList / MyAnimeList / Kitsu (check internet, DNS 8.8.8.8 or VPN)\n")
        elif not m:
            print("  Not found\n")
        else:
            print(f"  Found on {m['source']}: {m['image']}\n  Year {m['year']}, episodes {m['eps']}\n")

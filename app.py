#!/usr/bin/env python3
"""PDKU portal live dashboard.

Pulls per-day view counts for every spring-26 participant from the portal's
public Convex API (publicDashboard:getDashboardData), fetches the view count
of each posted video from its platform (YouTube and TikTok expose it without
auth; Instagram/X/Facebook are login-walled), and serves a local dashboard
that auto-refreshes.

Usage:  python3 app.py            # serves on http://localhost:8787
        python3 app.py --port N
"""

import argparse
import functools
import json
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CONVEX_URL = "https://third-corgi-964.convex.cloud/api/query"
SEASON_START = "2026-07-01"
REFRESH_SECONDS = 24 * 3600  # overridable with --interval
VIDEO_TTL_SECONDS = 900   # refetch each video's count at most every 15 min
VIDEO_BATCH = 30          # max video fetches per cycle
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
VIDEO_CACHE_FILE = BASE_DIR / "video_views.json"
FOLLOWERS_FILE = BASE_DIR / "followers.json"
# append-only time-series for charts (committed to the repo, unlike the caches)
HISTORY_FILE = BASE_DIR / "history.json"

# --deploy publishes to natochi.cv/pdku (static hosting via the blog repo)
BLOG_REPO = Path.home() / "projects/blog-natochi"
DEPLOY_DIR = BLOG_REPO / "pdku"
DEPLOY = False

print = functools.partial(print, flush=True)

_lock = threading.Lock()
_days = {}  # dayKey -> list of participant dicts
_videos = {}  # url -> {platform, views, fetchedAt, nextTry, creator}
_creators = {}  # "platform:handle" -> {followers, fetchedAt}
CREATOR_TTL_SECONDS = 20 * 3600  # follower counts refresh roughly per cycle


def parse_compact_count(text):
    """'1.23K subscribers' / '4' / '1.2M' -> int."""
    m = re.match(r"([\d.,]+)\s*([KMB]?)", text.replace(" ", " ").strip())
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    return int(num * {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2), 1))
_profiles = {}  # slug -> {"fetchedAt": ts, "posts": [initialHistory entries]}

# getDashboardData returns only one post per person per day, and when someone
# posts twice the visible one can be shadowed by a hidden one. The profile
# page's SSR payload ("initialHistory") lists every post with its URL.
PROFILE_TTL_SECONDS = 600
FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)


def decode_flight_chunk(chunk):
    try:
        return json.loads(f'"{chunk}"')
    except ValueError:
        return chunk.encode().decode("unicode_escape")


def fetch_profile_history(slug):
    req = urllib.request.Request(
        f"https://portal.plzdontkillus.com/spring-26/{slug}",
        headers={"User-Agent": BROWSER_UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    blob = "".join(decode_flight_chunk(c) for c in FLIGHT_RE.findall(raw))
    marker = '"initialHistory":'
    idx = blob.index(marker) + len(marker)
    depth = 0
    for i in range(idx, len(blob)):
        if blob[i] == "[":
            depth += 1
        elif blob[i] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(blob[idx:i + 1])
    raise ValueError("unterminated initialHistory")


def refresh_profiles(slugs):
    now = time.time()
    updated = 0
    for slug in slugs:
        entry = _profiles.get(slug)
        if entry and now - entry["fetchedAt"] < PROFILE_TTL_SECONDS:
            continue
        try:
            posts = fetch_profile_history(slug)
            _profiles[slug] = {"fetchedAt": time.time(), "posts": posts}
            updated += 1
        except Exception as exc:
            _profiles[slug] = {
                "fetchedAt": time.time(),
                "posts": (entry or {}).get("posts", []),
            }
            print(f"  profile fetch failed {slug}: {exc}")
        time.sleep(0.2)
    return updated


def platform_of(url):
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host in ("youtu.be", "youtube.com", "m.youtube.com"):
        return "youtube"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith("instagram.com"):
        return "instagram"
    if host in ("x.com", "twitter.com"):
        return "x"
    if host.endswith("facebook.com"):
        return "facebook"
    return "other"


# youtube/tiktok: view count is in the page HTML, one cheap GET.
# x/facebook: extracted via yt-dlp.
# instagram: private web API using the logged-in session from Zen browser
# (Firefox-based, cookies.sqlite is unencrypted).
REGEX_PLATFORMS = {"youtube", "tiktok"}
YTDLP_PLATFORMS = {"x", "facebook"}
SCRAPABLE = REGEX_PLATFORMS | YTDLP_PLATFORMS | {"instagram"}
VIEW_PATTERNS = {
    "youtube": re.compile(r'"viewCount":\s*"(\d+)"'),
    "tiktok": re.compile(r'"playCount":\s*(\d+)'),
}
YTDLP_BATCH = 15          # yt-dlp is ~3s per URL; keep the cycle bounded
IG_BATCH = 10             # gentle on Instagram's rate limits
FAIL_BACKOFF = 4 * VIDEO_TTL_SECONDS

ZEN_PROFILES = Path.home() / "Library/Application Support/zen/Profiles"
IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
IG_APP_ID = "936619743392459"

# X/Twitter follower counts: authenticated web GraphQL, using the Zen session.
# The public web bearer is a long-lived constant; the query id can rotate — if
# UserByScreenName starts 404ing, refresh X_USER_QID from the web app bundle.
X_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
            "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
X_USER_QID = "sLVLhk0bGj3MVFEKTdax1w"
X_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


def x_auth():
    """(auth_token, ct0) from the Zen profile logged into x.com, or None."""
    for db in sorted(ZEN_PROFILES.glob("*/cookies.sqlite")):
        try:
            conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
            rows = conn.execute(
                "select name, value from moz_cookies "
                "where host like '%x.com' and name in ('auth_token', 'ct0')"
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        c = dict(rows)
        if "auth_token" in c and "ct0" in c:
            return c["auth_token"], c["ct0"]
    return None


def fetch_x_followers(username, auth):
    auth_token, ct0 = auth
    from urllib.parse import quote
    variables = quote(json.dumps({"screen_name": username}))
    features = quote(json.dumps(X_FEATURES))
    url = (f"https://x.com/i/api/graphql/{X_USER_QID}/UserByScreenName"
           f"?variables={variables}&features={features}")
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + X_BEARER,
        "x-csrf-token": ct0,
        "Cookie": f"auth_token={auth_token}; ct0={ct0}",
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_UA,
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.load(resp)
    result = (((data.get("data") or {}).get("user") or {}).get("result") or {})
    legacy = result.get("legacy") or {}
    return legacy.get("followers_count")


def instagram_cookie_header():
    """Cookie header for instagram.com from the Zen profile that has a session."""
    for db in sorted(ZEN_PROFILES.glob("*/cookies.sqlite")):
        try:
            conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
            rows = conn.execute(
                "select name, value from moz_cookies "
                "where host like '%instagram.com' "
                "order by (host = '.instagram.com')"  # root-domain cookies win
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        cookies = dict(rows)
        if "sessionid" in cookies:
            return "; ".join(f"{k}={v}" for k, v in cookies.items())
    return None


def fetch_instagram_views(url, cookie_header):
    match = IG_SHORTCODE_RE.search(url)
    if not match:
        return None
    pk = 0
    for ch in match.group(1)[:11]:  # >11 chars means a private-style id prefix
        pk = pk * 64 + IG_ALPHABET.index(ch)
    req = urllib.request.Request(
        f"https://i.instagram.com/api/v1/media/{pk}/info/",
        headers={
            "User-Agent": BROWSER_UA,
            "Cookie": cookie_header,
            "X-IG-App-ID": IG_APP_ID,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.load(resp)
    items = data.get("items") or [{}]
    views = items[0].get("play_count") or items[0].get("view_count")
    username = (items[0].get("user") or {}).get("username")
    creator = f"instagram:{username}" if username else None
    if creator:
        _creators.setdefault(creator, {"followers": None, "fetchedAt": 0})
    return (int(views) if views is not None else None), creator


def fetch_instagram_followers(username, cookie_header):
    req = urllib.request.Request(
        f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
        headers={
            "User-Agent": BROWSER_UA,
            "Cookie": cookie_header,
            "X-IG-App-ID": IG_APP_ID,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.load(resp)
    user = (data.get("data") or {}).get("user") or {}
    return (user.get("edge_followed_by") or {}).get("count")


def refresh_creators():
    """Fill in follower counts that don't come free with the video pages.
    Instagram and X both need the logged-in Zen session."""
    now = time.time()

    def stale_for(prefix):
        return [
            key for key, c in _creators.items()
            if key.startswith(prefix)
            and now - c.get("fetchedAt", 0) > CREATOR_TTL_SECONDS
        ]

    updated = 0

    ig_stale = stale_for("instagram:")
    if ig_stale:
        cookie_header = instagram_cookie_header()
        if cookie_header:
            for key in ig_stale:
                username = key.split(":", 1)[1]
                try:
                    followers = fetch_instagram_followers(username, cookie_header)
                    _creators[key] = {"followers": followers, "fetchedAt": time.time()}
                    updated += 1
                except Exception as exc:
                    _creators[key]["fetchedAt"] = time.time()
                    print(f"  followers fetch failed (instagram) {username}: {exc}")
                time.sleep(1.2)

    x_stale = stale_for("x:")
    if x_stale:
        auth = x_auth()
        if auth:
            for key in x_stale:
                username = key.split(":", 1)[1]
                try:
                    followers = fetch_x_followers(username, auth)
                    _creators[key] = {"followers": followers, "fetchedAt": time.time()}
                    updated += 1
                except Exception as exc:
                    _creators[key]["fetchedAt"] = time.time()
                    print(f"  followers fetch failed (x) {username}: {exc}")
                time.sleep(1.0)

    if updated:
        FOLLOWERS_FILE.write_text(json.dumps(_creators))
    return updated


def fetch_regex_views(url, platform):
    """Return (views, creator_key). Harvests follower counts from the same
    page — both TikTok and YouTube embed the author's audience size."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read(2_000_000).decode("utf-8", errors="ignore")
    match = VIEW_PATTERNS[platform].search(html)
    views = int(match.group(1)) if match else None
    creator = None
    if platform == "tiktok":
        handle = re.search(r'"uniqueId":"([^"]+)"', html)
        followers = re.search(r'"followerCount":(\d+)', html)
        if handle:
            creator = f"tiktok:{handle.group(1)}"
            _creators[creator] = {
                "followers": int(followers.group(1)) if followers else None,
                "fetchedAt": time.time(),
            }
    elif platform == "youtube":
        channel = re.search(r'"channelId":"(UC[^"]+)"', html)
        subs = re.search(
            r'"subscriberCountText":.{0,300}?"simpleText":"([^"]+)"', html
        )
        if channel:
            creator = f"youtube:{channel.group(1)}"
            _creators[creator] = {
                "followers": parse_compact_count(subs.group(1)) if subs else None,
                "fetchedAt": time.time(),
            }
    return views, creator


def fetch_ytdlp_views(urls):
    """Batch-fetch view counts with yt-dlp. Returns {url: views}."""
    if not urls:
        return {}
    cmd = [
        "yt-dlp", "-j", "--no-download", "--no-update", "--no-warnings",
        "--ignore-errors", "--cookies-from-browser", "chrome", *urls,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60 + 10 * len(urls)
        )
    except subprocess.TimeoutExpired as exc:
        proc = exc  # partial stdout is still usable
    results = {}
    for line in (proc.stdout or "").splitlines():
        try:
            info = json.loads(line)
        except ValueError:
            continue
        url = info.get("original_url") or info.get("webpage_url")
        views = info.get("view_count")
        if url and views is not None:
            results[url] = int(views)
    return results


def all_video_urls():
    """Every post URL seen in profile histories and dashboard days."""
    urls = set()
    for prof in _profiles.values():
        for post in prof["posts"]:
            if post.get("url"):
                urls.add(post["url"])
    for participants in _days.values():
        for p in participants:
            post = p.get("todayPost")
            if post and post.get("url"):
                urls.add(post["url"])
    return urls


# momentum = views gained in the last ~24h. Platform counts are lifetime
# cumulative, so a fresh post looks weak only because it is young; diffing
# a fixed 24h window makes every post comparable regardless of post time
# (which the portal never exposes anyway — only the Pacific-time day).
MOMENTUM_WINDOW = 24 * 3600
MOMENTUM_MIN_AGE = 20 * 3600  # baseline must be at least this old to count
HISTORY_MIN_GAP = 3600        # collapse samples <1h apart (avoids test bloat)
HISTORY_KEEP = 40
# once a video's view count has been flat for this long, it's done growing —
# stop hammering it every cycle and just re-check occasionally in case it revives.
SETTLE_SECONDS = 5 * 86400
SETTLED_RECHECK = 7 * 86400


def record_view_sample(entry, views, creator):
    now = time.time()
    prev = entry.get("views")
    entry["views"] = views
    entry["creator"] = creator
    entry["fetchedAt"] = now
    if views is None:
        entry["nextTry"] = now + VIDEO_TTL_SECONDS
        return
    if prev != views or "lastChangedAt" not in entry:
        entry["lastChangedAt"] = now
    entry["settled"] = (now - entry["lastChangedAt"]) >= SETTLE_SECONDS
    entry["nextTry"] = now + (SETTLED_RECHECK if entry["settled"] else VIDEO_TTL_SECONDS)
    hist = entry.setdefault("history", [])
    if hist and now - hist[-1][0] < HISTORY_MIN_GAP:
        hist[-1] = [now, views]           # refresh the most recent point
    else:
        hist.append([now, views])
    del hist[:-HISTORY_KEEP]


def post_momentum(entry):
    """Views gained over the most recent full ~24h window, or None if the
    post hasn't been tracked long enough to have a 24h-old baseline yet."""
    hist = (entry or {}).get("history") or []
    if len(hist) < 2:
        return None
    now_ts, now_views = hist[-1]
    baseline = None
    for ts, v in reversed(hist[:-1]):
        if now_ts - ts >= MOMENTUM_MIN_AGE:
            baseline = v
            break
    if baseline is None:
        return None
    return max(0, now_views - baseline)


def refresh_videos():
    """Fetch view counts for videos whose cached value is stale."""
    with _lock:
        urls = all_video_urls()
    now = time.time()
    stale = []
    for url in urls:
        platform = platform_of(url)
        entry = _videos.get(url)
        if entry is None:
            _videos[url] = entry = {
                "platform": platform, "views": None,
                "fetchedAt": 0, "nextTry": 0,
            }
        entry.setdefault("nextTry", 0)
        if platform in SCRAPABLE and now >= entry["nextTry"]:
            stale.append((url, url, platform))
    stale.sort(key=lambda item: _videos[item[0]]["fetchedAt"])
    fetched = 0

    regex_jobs = [s for s in stale if s[2] in REGEX_PLATFORMS][:VIDEO_BATCH]
    for post_id, url, platform in regex_jobs:
        try:
            views, creator = fetch_regex_views(url, platform)
            record_view_sample(_videos[post_id], views, creator)
            fetched += 1
        except Exception as exc:
            _videos[post_id]["nextTry"] = time.time() + FAIL_BACKOFF
            print(f"  video fetch failed ({platform}) {url}: {exc}")
        time.sleep(0.5)

    ig_jobs = [s for s in stale if s[2] == "instagram"][:IG_BATCH]
    if ig_jobs:
        cookie_header = instagram_cookie_header()
        if cookie_header is None:
            print("  instagram: no logged-in Zen session found, skipping")
            for post_id, _, _ in ig_jobs:
                _videos[post_id]["nextTry"] = time.time() + FAIL_BACKOFF
        else:
            for post_id, url, _ in ig_jobs:
                try:
                    views, creator = fetch_instagram_views(url, cookie_header)
                    record_view_sample(_videos[post_id], views, creator)
                    fetched += 1
                except Exception as exc:
                    _videos[post_id]["nextTry"] = time.time() + FAIL_BACKOFF
                    print(f"  video fetch failed (instagram) {url}: {exc}")
                time.sleep(1.2)

    ytdlp_jobs = [s for s in stale if s[2] in YTDLP_PLATFORMS][:YTDLP_BATCH]
    if ytdlp_jobs:
        results = fetch_ytdlp_views([url for _, url, _ in ytdlp_jobs])
        for post_id, url, platform in ytdlp_jobs:
            handle = re.search(r"(?:x|twitter)\.com/([^/]+)/status", url)
            creator = f"x:{handle.group(1)}" if handle else None
            if creator:
                _creators.setdefault(creator, {"followers": None, "fetchedAt": 0})
            if url in results:
                record_view_sample(_videos[post_id], results[url], creator)
                fetched += 1
            else:
                _videos[post_id]["nextTry"] = time.time() + FAIL_BACKOFF
    if fetched:
        VIDEO_CACHE_FILE.write_text(json.dumps(_videos))
    return fetched


def load_video_cache():
    if VIDEO_CACHE_FILE.exists():
        try:
            cached = json.loads(VIDEO_CACHE_FILE.read_text())
        except Exception:
            return
        for key, entry in cached.items():
            # older cache versions were keyed by postId with the url inside
            url = entry.get("url") or key
            entry.pop("url", None)
            _videos[url] = entry
    if FOLLOWERS_FILE.exists():
        try:
            _creators.update(json.loads(FOLLOWERS_FILE.read_text()))
        except Exception:
            pass


def convex_query(path, args):
    body = json.dumps({"path": path, "args": args, "format": "json"}).encode()
    req = urllib.request.Request(
        CONVEX_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise RuntimeError(f"convex error for {args}: {payload.get('errorMessage')}")
    return payload["value"]


def fetch_day(day_key=None):
    args = {"dayKey": day_key} if day_key else {}
    value = convex_query("publicDashboard:getDashboardData", args)
    return value["dayKey"], value.get("participants", [])


def day_range(start_key, end_key):
    start = date.fromisoformat(start_key)
    end = date.fromisoformat(end_key)
    d = start
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def followers_growth_baseline():
    """Earliest known followers count (and its day) per slug, from history.json,
    so build_snapshot() can report growth since that first sighting."""
    baseline_followers = {}
    baseline_day = {}
    if HISTORY_FILE.exists():
        try:
            snaps = json.loads(HISTORY_FILE.read_text()).get("snapshots", [])
        except Exception:
            snaps = []
        for snap in snaps:
            for p in snap.get("people", []):
                slug = p.get("slug")
                f = p.get("followers")
                if slug and f and slug not in baseline_followers:
                    baseline_followers[slug] = f
                    baseline_day[slug] = snap.get("day")
    return baseline_followers, baseline_day


def build_snapshot():
    people = {}
    platform_totals = {}
    day_keys = sorted(_days)
    baseline_followers, baseline_day = followers_growth_baseline()
    for day_key in day_keys:
        for p in _days[day_key]:
            slug = p.get("slug")
            if not slug:
                continue
            person = people.setdefault(
                slug,
                {
                    "slug": slug,
                    "name": p.get("name") or slug,
                    "image": p.get("image"),
                    "role": p.get("role"),
                    "currentStreak": 0,
                    "daily": {},
                    "totalViews": 0,
                    "totalUpvotes": 0,
                    "totalVideoViews": 0,
                    "postCount": 0,
                    "trackedCount": 0,
                },
            )
            person["currentStreak"] = int(p.get("currentStreak") or 0)
            post = p.get("todayPost")
            if post:
                views = int(post.get("readCount") or 0)
                upvotes = int(post.get("upvoteCount") or 0)
                url = post.get("url") or ""
                person["daily"][day_key] = {
                    "views": views,
                    "upvotes": upvotes,
                    "title": post.get("title") or "",
                    "url": url,
                    "hidden": bool(post.get("isHidden")),
                }
                person["totalViews"] += views
                person["totalUpvotes"] += upvotes
    today = day_keys[-1] if day_keys else None
    for person in people.values():
        person["todayViews"] = person["daily"].get(today, {}).get("views", 0)

        prof = _profiles.get(person["slug"])
        if prof and prof["posts"]:
            posts = [
                {
                    "day": h.get("dayKey"),
                    "title": h.get("title") or "",
                    "url": h.get("url") or "",
                    "hidden": bool(h.get("isHidden")),
                }
                for h in prof["posts"]
            ]
            # the profile's SSR payload can lag the dashboard on any given day
            # (edge cache), not just today - patch in whatever it's missing.
            have_urls = {x["url"] for x in posts if x["url"]}
            for day, d in person["daily"].items():
                if d["url"] and d["url"] not in have_urls:
                    posts.append(
                        {"day": day, "title": d["title"], "url": d["url"],
                         "hidden": d["hidden"]}
                    )
                    have_urls.add(d["url"])
        else:
            posts = [
                {"day": day, "title": d["title"], "url": d["url"],
                 "hidden": d["hidden"]}
                for day, d in person["daily"].items()
            ]
        # the portal's own history can list the same video twice (re-shares,
        # edits) - dedupe by URL so views aren't double-counted.
        seen_urls = set()
        deduped = []
        for x in posts:
            if x["url"]:
                if x["url"] in seen_urls:
                    continue
                seen_urls.add(x["url"])
            deduped.append(x)
        posts = deduped
        for x in posts:
            entry = _videos.get(x["url"]) if x["url"] else None
            x["platform"] = platform_of(x["url"]) if x["url"] else None
            x["videoViews"] = entry.get("views") if entry else None
            x["momentum"] = post_momentum(entry)
            if x["url"]:
                pt = platform_totals.setdefault(
                    x["platform"], {"views": 0, "videos": 0, "tracked": 0}
                )
                pt["videos"] += 1
                if x["videoViews"] is not None:
                    pt["views"] += x["videoViews"]
                    pt["tracked"] += 1
        posts.sort(key=lambda x: x["day"] or "")
        person["posts"] = posts
        person["postCount"] = len(posts)
        person["trackedCount"] = sum(1 for x in posts if x["videoViews"] is not None)
        person["totalVideoViews"] = sum(x["videoViews"] or 0 for x in posts)
        # momentum: 24h view growth summed across all this person's videos
        moms = [x["momentum"] for x in posts if x["momentum"] is not None]
        person["momentum24h"] = sum(moms) if moms else None

        # audience: distinct creator accounts behind this person's posts
        creators = {}
        for x in posts:
            key = _videos.get(x["url"], {}).get("creator") if x["url"] else None
            if key:
                creators[key] = (_creators.get(key) or {}).get("followers")
        known = {k: f for k, f in creators.items() if f}
        by_platform = {}
        for k, f in known.items():
            plat = k.split(":", 1)[0]
            by_platform[plat] = by_platform.get(plat, 0) + f
        person["followers"] = sum(known.values()) or None
        person["followersByPlatform"] = by_platform
        person["followersPartial"] = len(known) < len(creators)
        person["viewsPerFollower"] = (
            round(person["totalVideoViews"] / person["followers"], 4)
            if person["followers"] and person["trackedCount"] else None
        )
        # "strongest": volume x efficiency. Geometric-mean-style score;
        # the 1K floor keeps tiny accounts from winning on ratio alone.
        person["strength"] = (
            round(person["totalVideoViews"] / max(person["followers"], 1000) ** 0.5, 1)
            if person["followers"] and person["trackedCount"] else None
        )
        # growth since the earliest sighting we have in history.json
        old_f = baseline_followers.get(person["slug"])
        old_day = baseline_day.get(person["slug"])
        if old_f and person["followers"] and old_day and old_day != today:
            person["followersGrowth"] = person["followers"] - old_f
            person["followersGrowthDays"] = (
                date.fromisoformat(today) - date.fromisoformat(old_day)
            ).days
        else:
            person["followersGrowth"] = None
            person["followersGrowthDays"] = None
    return {
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seasonStart": SEASON_START,
        "today": today,
        "days": day_keys,
        "platformTotals": platform_totals,
        "refreshSeconds": REFRESH_SECONDS,
        "instagramAuth": instagram_cookie_header() is not None,
        "people": sorted(
            people.values(), key=lambda x: (-x["totalVideoViews"], x["slug"])
        ),
    }


def write_snapshot():
    snapshot = build_snapshot()
    DATA_FILE.write_text(json.dumps(snapshot))
    return snapshot


def append_history(snapshot):
    """Accumulate a chart-ready time-series into history.json:
    - snapshots: one compact per-person record per day (views, momentum,
      followers by platform, strength) — lets you chart growth over the season.
    - videos: each video's [timestamp, views] series for granular per-post charts.
    Overwrites the same day's snapshot if the scraper runs twice in a day."""
    hist = {"snapshots": [], "videos": {}}
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass

    record = {
        "t": snapshot["updatedAt"],
        "day": snapshot["today"],
        "totalVideoViews": sum(p["totalVideoViews"] for p in snapshot["people"]),
        "platformViews": {
            k: v["views"] for k, v in snapshot["platformTotals"].items()
        },
        "people": [
            {
                "slug": p["slug"],
                "name": p["name"],
                "views": p["totalVideoViews"],
                "momentum24h": p["momentum24h"],
                "followers": p["followers"],
                "followersByPlatform": p["followersByPlatform"],
                "tracked": p["trackedCount"],
                "posts": p["postCount"],
                "viewsPerFollower": p["viewsPerFollower"],
                "strength": p["strength"],
            }
            for p in snapshot["people"]
        ],
    }
    snaps = hist.get("snapshots", [])
    if snaps and snaps[-1].get("day") == record["day"]:
        snaps[-1] = record        # keep one point per day (latest wins)
    else:
        snaps.append(record)
    hist["snapshots"] = snaps

    url_meta = {}
    for p in snapshot["people"]:
        for x in p["posts"]:
            if x["url"]:
                url_meta[x["url"]] = {
                    "title": x["title"], "slug": p["slug"], "day": x["day"]
                }
    videos = {}
    for url, entry in _videos.items():
        series = entry.get("history")
        if not series:
            continue
        meta = url_meta.get(url, {})
        videos[url] = {
            "platform": entry.get("platform"),
            "creator": entry.get("creator"),
            "title": meta.get("title"),
            "slug": meta.get("slug"),
            "day": meta.get("day"),
            "history": series,
        }
    hist["videos"] = videos
    hist["updatedAt"] = snapshot["updatedAt"]
    HISTORY_FILE.write_text(json.dumps(hist))
    return len(snaps)


def deploy():
    """Publish to natochi.cv/pdku (blog repo) and mirror source+data to the
    standalone `pdku` repo, so both update on every scrape."""
    DEPLOY_DIR.mkdir(exist_ok=True)
    (DEPLOY_DIR / "index.html").write_bytes((BASE_DIR / "index.html").read_bytes())
    (DEPLOY_DIR / "data.json").write_bytes(DATA_FILE.read_bytes())
    proc = subprocess.run(
        ["./publish.sh", "project", "pdku"],
        cwd=BLOG_REPO, capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout + proc.stderr).strip().splitlines()
    print(f"  deploy: {out[-1] if out else f'exit {proc.returncode}'}")
    mirror_repo()


def mirror_repo():
    """Commit and push the project (source + data.json) to the `pdku` remote."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=BASE_DIR, check=True, timeout=30)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR, timeout=30
        )
        if staged.returncode == 0:
            return  # nothing changed
        subprocess.run(
            ["git", "commit", "-q", "-m", f"sync {time.strftime('%Y-%m-%d %H:%M')}"],
            cwd=BASE_DIR, check=True, timeout=30,
        )
        push = subprocess.run(
            ["git", "push", "-q", "pdku", "HEAD:main"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=60,
        )
        print(f"  mirror: {'pushed' if push.returncode == 0 else push.stderr.strip()[:80]}")
    except Exception as exc:
        print(f"  mirror failed: {exc}")


def scrape_loop():
    backfilled = False
    while True:
        try:
            today_key, participants = fetch_day()
            with _lock:
                _days[today_key] = participants
            profiles_updated = refresh_profiles(
                [p["slug"] for p in participants if p.get("slug")]
            )
            if not backfilled:
                for day_key in day_range(SEASON_START, today_key):
                    if day_key == today_key:
                        continue
                    _, day_participants = fetch_day(day_key)
                    with _lock:
                        _days[day_key] = day_participants
                    time.sleep(0.3)
                backfilled = True
            # batches bound each pass; loop until every stale video was tried
            fetched = 0
            for _ in range(20):
                batch = refresh_videos()
                fetched += batch
                if batch == 0:
                    break
            refresh_creators()
            FOLLOWERS_FILE.write_text(json.dumps(_creators))
            with _lock:
                snapshot = write_snapshot()
                days_logged = append_history(snapshot)
            total = sum(p["totalViews"] for p in snapshot["people"])
            video_total = sum(p["totalVideoViews"] for p in snapshot["people"])
            settled = sum(1 for e in _videos.values() if e.get("settled"))
            print(
                f"[{time.strftime('%H:%M:%S')}] scraped {len(snapshot['days'])} days, "
                f"{len(snapshot['people'])} people, {total} portal views, "
                f"{video_total} video views ({fetched} videos, "
                f"{profiles_updated} profiles refreshed, {settled} settled, "
                f"{days_logged} days in history)"
            )
            if DEPLOY:
                try:
                    deploy()
                except Exception as exc:
                    print(f"  deploy failed: {exc}")
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] scrape failed: {exc}")
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve(BASE_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/data.json":
            self._serve(DATA_FILE, "application/json")
        else:
            self.send_error(404)

    def _serve(self, path, content_type):
        if not path.exists():
            self.send_error(503, "data not ready yet")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global REFRESH_SECONDS, DEPLOY
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--interval", type=int, default=REFRESH_SECONDS,
        help="seconds between full scrapes (default: 24h)",
    )
    parser.add_argument(
        "--deploy", action="store_true",
        help="push each scrape to natochi.cv/pdku via the blog repo",
    )
    args = parser.parse_args()
    REFRESH_SECONDS = args.interval
    DEPLOY = args.deploy

    load_video_cache()
    threading.Thread(target=scrape_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Dashboard on http://localhost:{args.port} (scrape every {REFRESH_SECONDS}s)")
    server.serve_forever()


if __name__ == "__main__":
    main()

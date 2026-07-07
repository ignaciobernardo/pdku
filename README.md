# PDKU spring-26 social views dashboard

Live leaderboard of **social media view counts** for every video posted on
https://portal.plzdontkillus.com/spring-26.

## How it gets the data

1. **Post list** — two sources, because neither is complete on its own:
   - `publicDashboard:getDashboardData` on the portal's public Convex
     deployment (`third-corgi-964.convex.cloud`) returns one post per person
     per `dayKey` (with portal read counts). One request per season day.
   - Each profile page's SSR payload (`initialHistory` in the Next.js flight
     data) lists **every** post with its URL — including visible posts that
     the dashboard query shadows when someone also posted a hidden post that
     day. All 61 profiles are re-scraped every 10 min.
   Posts hidden by their author ("Hidden Post") expose no URL anywhere and
   can't be tracked.
2. **View counts per platform**:
   - **YouTube / TikTok** — one GET per video, count is in the page HTML
     (`"viewCount"` / `"playCount"`).
   - **X / Facebook** — `yt-dlp` (anonymous works for X).
   - **Instagram** — private web API (`i.instagram.com/api/v1/media/{pk}/info/`)
     using the logged-in session read from **Zen browser's** unencrypted
     `cookies.sqlite`. Requires being logged in to instagram.com in Zen; the
     media PK is decoded from the URL shortcode (base64).

Counts are cached in `video_views.json`, refreshed at most every 15 min per
video, in bounded batches per cycle (Instagram: 10/cycle with 1.2 s spacing to
stay polite). Failures back off for 1 h.

## Run

```
python3 app.py                  # http://localhost:8787, scrapes once a day
python3 app.py --deploy         # also publish each scrape to natochi.cv/pdku
python3 app.py --interval 3600  # scrape hourly instead
python3 app.py --port N
```

`--deploy` copies `index.html` + `data.json` into `~/projects/blog-natochi/pdku/`
and runs that repo's `./publish.sh project pdku` (git push → GitHub Pages).
The live page is static: it reads the committed `data.json`, so it updates
when the local scraper pushes.

Stdlib only (plus the `yt-dlp` binary from Homebrew). The server does a full
scrape on start and then every `--interval` seconds (default 24 h); each cycle
loops through batches until every video has been refreshed. The page checks
for fresh data every 5 min.

## Metrics (leaderboard toggles)

Platform view counts are lifetime-cumulative, and the portal only exposes each
post's Pacific-time *day*, never its hour — so raw "views today" mostly measures
how long ago a post went up. The toggles account for that:

- **Season** — total tracked video views across all of a person's posts.
- **Last 24h** — views *gained* in the most recent ~24 h window, summed across
  their videos. Same window for everyone, so post age doesn't distort it. Each
  video keeps a timestamped view-count history (`video_views.json`); momentum
  needs a sample ≥20 h old, so it stays blank ("accumulating") until the second
  daily scrape, then fills in. The "Trending (24h)" tile is its leader.
- **Per follower** — season views ÷ followers.
- **Strongest** — `views ÷ √followers`: rewards volume *and* efficiency, so a
  mid-audience creator who over-indexes (e.g. Tonchi) beats a huge account
  pulling a fraction of its reach. 1 K-follower floor damps tiny accounts.

Followers come free from the TikTok/YouTube video pages; Instagram and X both
come from their logged-in web APIs (the Zen session). X uses the authenticated
GraphQL `UserByScreenName` endpoint — if it starts 404ing, the query id in
`X_USER_QID` has rotated and needs refreshing from the web app bundle.

Note: `--cookies-from-browser firefox:<zen profile>` in yt-dlp would be the
clean way to do Instagram, but the Homebrew Python build has a broken
`pyexpat` (dyld/libexpat mismatch) that crashes yt-dlp's Instagram extractor —
hence the direct API approach.

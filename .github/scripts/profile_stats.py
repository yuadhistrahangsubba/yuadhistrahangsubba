"""Generate GitHub streak stats, activity graph and trophy SVGs that include private contributions.

The public contribution calendar leaves out private work unless "Include private
contributions on my profile" is enabled, so public card services can't see it.
This script merges the calendar with commits the token can read in private
repositories (personal and organization), on every branch, counted per day in
PROFILE_TZ (default UTC).

Writes streak-stats.svg, activity-graph.svg, activity-graph-dark.svg, trophies.svg, trophies-dark.svg (milestones), dashboard.svg and dashboard-dark.svg into <output_dir>.

Usage: GH_TOKEN=... python profile_stats.py <username> <output_dir>
"""

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

API = "https://api.github.com/graphql"
REST = "https://api.github.com"

QUERY = """
query($login: String!, $from: DateTime, $to: DateTime) {
  user(login: $login) {
    createdAt
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        repository { isPrivate }
        contributions { totalCount }
      }
    }
  }
}
"""

PROFILE_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(ownerAffiliations: OWNER, first: 100, after: $after) {
      totalCount
      nodes {
        stargazerCount
        isFork
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# Theme matches the "radical" look used by the rest of the README.
THEME = {
    "background": "#141321",
    "ring": "#00FFC0",
    "fire": "#FF00FF",
    "curr_label": "#00FFC0",
    "curr_num": "#FFE45E",
    "side_nums": "#FE428E",
    "side_labels": "#AAAAAA",
    "dates": "#888888",
    "stroke": "#E4E2E2",
}


def graphql(token, variables, query=QUERY):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise SystemExit(f"GraphQL error: {payload['errors']}")
    return payload["data"]["user"]


def fetch_calendar(token, login, tz):
    """Return {date: count} from account creation until today, one year per request,
    plus the number of commits to public repositories over the same period."""
    today = datetime.now(tz).date()
    created = graphql(token, {"login": login})["createdAt"]
    start = datetime.fromisoformat(created.replace("Z", "+00:00")).date()

    days = {}
    public_commits = 0
    cursor = start
    while cursor <= today:
        end = min(date(cursor.year, 12, 31), today)
        user = graphql(token, {
            "login": login,
            "from": f"{cursor.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        })
        for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                d = date.fromisoformat(day["date"])
                if start <= d <= today:
                    days[d] = day["contributionCount"]
        for repo in user["contributionsCollection"]["commitContributionsByRepository"]:
            if not repo["repository"]["isPrivate"]:
                public_commits += repo["contributions"]["totalCount"]
        cursor = date(cursor.year + 1, 1, 1)
    return start, today, days, public_commits


def fetch_profile(token, login):
    """Followers, PRs, issues, owned repositories, stars they earned and
    language bytes across owned non-fork repositories."""
    stars, after = 0, None
    languages = {}
    while True:
        user = graphql(token, {"login": login, "after": after}, PROFILE_QUERY)
        repos = user["repositories"]
        stars += sum(node["stargazerCount"] for node in repos["nodes"])
        for node in repos["nodes"]:
            if node["isFork"]:
                continue
            for edge in node["languages"]["edges"]:
                name = edge["node"]["name"]
                size, color = languages.get(name, (0, None))
                languages[name] = (size + edge["size"], edge["node"]["color"] or color)
        if not repos["pageInfo"]["hasNextPage"]:
            break
        after = repos["pageInfo"]["endCursor"]
    return {
        "followers": user["followers"]["totalCount"],
        "pull_requests": user["pullRequests"]["totalCount"],
        "issues": user["issues"]["totalCount"],
        "repositories": repos["totalCount"],
        "stars": stars,
        "languages": languages,
    }


def rest_pages(token, url, key=None):
    """Yield items from a paginated REST endpoint, following Link headers.
    `key` names the list inside an object response (e.g. compare's "commits")."""
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    items = json.load(resp)
                    link = resp.headers.get("Link", "")
                break
            except urllib.error.HTTPError as err:
                # 409: empty repository, 403/404: no access (e.g. SSO not authorized).
                if err.code in (403, 404, 409):
                    print(f"skip {url.split('?')[0]}: HTTP {err.code}", file=sys.stderr)
                    return
                raise
            except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        yield from items[key] if key else items
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None


def repo_commits(token, repo, authors, since):
    """Return {sha: author date} of commits by any of `authors` on every branch of
    `repo`, so unmerged feature-branch work counts and merged work counts once.

    The default branch is filtered server-side; every other branch costs one
    compare request for just the commits it has on top of the default branch."""
    base = f"{REST}/repos/{repo['full_name']}"
    default = repo["default_branch"]
    shas = {}
    for author in authors:
        for commit in rest_pages(token, f"{base}/commits?author={quote(author, safe='')}&since={since}&per_page=100"):
            shas[commit["sha"]] = commit["commit"]["author"]["date"]
    for branch in rest_pages(token, f"{base}/branches?per_page=100"):
        if branch["name"] == default:
            continue
        compare = f"{base}/compare/{quote(default, safe='')}...{quote(branch['name'], safe='')}?per_page=100"
        for commit in rest_pages(token, compare, key="commits"):
            login = (commit.get("author") or {}).get("login")
            date_ = commit["commit"]["author"]["date"]
            if (login in authors or commit["commit"]["author"]["email"] in authors) and date_ >= since:
                shas[commit["sha"]] = date_
    return shas


def fetch_private_commits(token, login, start, tz):
    """Return {date: count} of the user's commits on all branches of private repos,
    dated in `tz`. AUTHOR_EMAILS adds commit emails not linked to the account."""
    authors = [login] + [e.strip() for e in os.environ.get("AUTHOR_EMAILS", "").split(",") if e.strip()]
    since = f"{start.isoformat()}T00:00:00Z"
    repos = [
        repo for repo in rest_pages(
            token,
            f"{REST}/user/repos?visibility=private"
            "&affiliation=owner,collaborator,organization_member&per_page=100",
        )
        # GitHub doesn't count commits to forks either; repos not pushed since
        # `start` can't hold commits we're looking for.
        if not repo["fork"] and (repo["pushed_at"] or "") >= since
    ]
    shas = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found in pool.map(lambda repo: repo_commits(token, repo, authors, since), repos):
            shas.update(found)
    days = Counter()
    for stamp in shas.values():
        days[datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tz).date()] += 1
    return days


def merge(calendar, private):
    """Combine per day. max() never double counts when the profile setting
    already includes private work; total is then a lower bound."""
    return {d: max(calendar.get(d, 0), private.get(d, 0)) for d in calendar.keys() | private.keys()}


def compute_stats(start, today, days):
    total = sum(days.values())

    # Longest streak.
    longest = (0, None, None)
    run, run_start = 0, None
    d = start
    while d <= today:
        if days.get(d, 0) > 0:
            if run == 0:
                run_start = d
            run += 1
            if run > longest[0]:
                longest = (run, run_start, d)
        else:
            run = 0
        d += timedelta(days=1)

    # Current streak: today without contributions doesn't break it yet.
    end = today if days.get(today, 0) > 0 else today - timedelta(days=1)
    current, d = 0, end
    while days.get(d, 0) > 0:
        current += 1
        d -= timedelta(days=1)
    current_range = (d + timedelta(days=1), end) if current else (today, today)

    return {
        "total": total,
        "total_range": (start, None),
        "current": current,
        "current_range": current_range,
        "longest": longest[0],
        "longest_range": (longest[1], longest[2]),
    }


def fmt_date(d, today):
    if d.year == today.year:
        return d.strftime("%b %-d")
    return d.strftime("%b %-d, %Y")


def fmt_range(rng, today):
    a, b = rng
    if a is None:
        return ""
    if b is None:
        return f"{fmt_date(a, today)} - Present"
    if a == b:
        return fmt_date(a, today)
    return f"{fmt_date(a, today)} - {fmt_date(b, today)}"


def render_svg(stats, today):
    t = THEME
    total_range = escape(fmt_range(stats["total_range"], today))
    current_range = escape(fmt_range(stats["current_range"], today))
    longest_range = escape(fmt_range(stats["longest_range"], today))
    font = "font-family=\"'Segoe UI', Ubuntu, sans-serif\""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195">
  <rect width="495" height="195" rx="4.5" fill="{t['background']}"/>
  <line x1="165" y1="28" x2="165" y2="170" stroke="{t['stroke']}" stroke-width="1"/>
  <line x1="330" y1="28" x2="330" y2="170" stroke="{t['stroke']}" stroke-width="1"/>

  <g {font} text-anchor="middle">
    <text x="82.5" y="79" font-size="28" font-weight="700" fill="{t['side_nums']}">{stats['total']:,}</text>
    <text x="82.5" y="115" font-size="14" fill="{t['side_labels']}">Total Contributions</text>
    <text x="82.5" y="146" font-size="12" fill="{t['dates']}">{total_range}</text>

    <mask id="ring-mask">
      <rect width="495" height="195" fill="white"/>
      <ellipse cx="247.5" cy="32" rx="13" ry="18" fill="black"/>
    </mask>
    <circle cx="247.5" cy="71" r="40" fill="none" stroke="{t['ring']}" stroke-width="5" mask="url(#ring-mask)"/>
    <g transform="translate(247.5, 19.5)">
      <path d="M -12 -0.5 L 15 -0.5 L 15 23.5 L -12 23.5 L -12 -0.5 Z" fill="none"/>
      <path d="M 1.5 0.67 C 1.5 0.67 2.24 3.32 2.24 5.47 C 2.24 7.53 0.89 9.2 -1.17 9.2 C -3.24 9.2 -4.79 7.53 -4.79 5.47 L -4.76 5.11 C -6.78 7.51 -8 10.62 -8 13.99 C -8 18.41 -4.42 22 0 22 C 4.42 22 8 18.41 8 13.99 C 8 8.6 5.41 3.79 1.5 0.67 Z M -0.29 19 C -2.07 19 -3.51 17.6 -3.51 15.86 C -3.51 14.24 -2.46 13.1 -0.7 12.74 C 1.07 12.38 2.9 11.53 3.92 10.16 C 4.31 11.45 4.51 12.81 4.51 14.2 C 4.51 16.85 2.36 19 -0.29 19 Z" fill="{t['fire']}"/>
    </g>
    <text x="247.5" y="83" font-size="28" font-weight="700" fill="{t['curr_num']}">{stats['current']:,}</text>
    <text x="247.5" y="140" font-size="14" font-weight="700" fill="{t['curr_label']}">Current Streak</text>
    <text x="247.5" y="166" font-size="12" fill="{t['dates']}">{current_range}</text>

    <text x="412.5" y="79" font-size="28" font-weight="700" fill="{t['side_nums']}">{stats['longest']:,}</text>
    <text x="412.5" y="115" font-size="14" fill="{t['side_labels']}">Longest Streak</text>
    <text x="412.5" y="146" font-size="12" fill="{t['dates']}">{longest_range}</text>
  </g>
</svg>
"""


ACT_PAPER = {
    "light": dict(bg="#fbf8f2", ink="#1f1c18", soft="#4a443c", muted="#8a8174",
                  rule="#e4ddd0", band="#f3eee4", area="#c0561b", accent="#c0561b"),
    "dark":  dict(bg="#15130f", ink="#efe9de", soft="#cfc6b8", muted="#8d8578",
                  rule="#2e2a23", band="#211d17", area="#e98a45", accent="#e98a45"),
}
ACT_SERIF = "'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif"
ACT_SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def render_activity_svg(days, today, title, span=31, theme="light"):
    """The last `span` days as a calm line chart: weekends shaded, the busiest day
    labelled, and a one-line summary on top. theme is "light" or "dark"."""
    t = ACT_PAPER[theme]
    W, H = 920, 400
    left, right, top, bottom = 56, 864, 150, 330
    dates = [today - timedelta(days=span - 1 - i) for i in range(span)]
    counts = [days.get(d, 0) for d in dates]
    total, active = sum(counts), sum(1 for c in counts if c)
    peak_i = max(range(span), key=lambda i: (counts[i], i))
    peak = counts[peak_i]
    y_max = max(peak, 1) * 1.15
    slot = (right - left) / (span - 1)

    def x(i):
        return left + slot * i

    def y(v):
        return bottom - (bottom - top) * v / y_max

    o = []
    a = o.append

    def text(xx, yy, s, size=15, fill=None, italic=False, anchor="start", cls="s", extra=""):
        st = ' font-style="italic"' if italic else ""
        an = f' text-anchor="{anchor}"' if anchor != "start" else ""
        a(f'<text x="{xx:.1f}" y="{yy:.1f}" class="{cls}" font-size="{size}"{st}{an} fill="{fill or t["soft"]}"{extra}>{s}</text>')

    period = f"{dates[0].strftime('%b %-d')} – {dates[-1].strftime('%b %-d, %Y')}"
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
      f'aria-label="{escape(title)}: {total:,} contributions from {period}, active on {active} of {span} days, '
      f'busiest on {dates[peak_i].strftime("%b %-d")} with {peak}.">')
    a(f'<style>.s{{font-family:{ACT_SERIF}}} .k{{font-family:{ACT_SANS}}}'
      '.ln{stroke-dasharray:1;stroke-dashoffset:1;animation:ln 2.2s cubic-bezier(.3,.7,.2,1) .2s forwards}'
      '.ar{opacity:0;animation:ar 1s ease 1.4s forwards}'
      '@keyframes ln{to{stroke-dashoffset:0}} @keyframes ar{to{opacity:1}}'
      '@media (prefers-reduced-motion:reduce){.ln{animation:none;stroke-dashoffset:0}.ar{animation:none;opacity:1}}</style>')
    a(f'<rect width="{W}" height="{H}" rx="10" fill="{t["bg"]}"/>')

    # masthead + summary sentence
    text(56, 52, "CONTRIBUTION ACTIVITY", 11, t["muted"], cls="k", extra=' letter-spacing="2.4"')
    text(864, 52, f"the last {span} days · {period}", 13, t["muted"], True, "end")
    a(f'<line x1="56" y1="68" x2="864" y2="68" stroke="{t["rule"]}"/>')
    tail = "and I showed up for every one of them." if active == span else f"active on {active} of them."
    text(56, 108, f'{total:,} contributions in {span} days, <tspan font-size="22" font-style="italic" '
         f'fill="{t["accent"]}">{tail}</tspan>', 28, t["ink"])

    # weekend bands
    for i, d in enumerate(dates):
        if d.weekday() >= 5:
            bx = max(x(i) - slot / 2, left)
            bw = min(x(i) + slot / 2, right) - bx
            a(f'<rect x="{bx:.1f}" y="{top - 10}" width="{bw:.1f}" height="{bottom - top + 10}" fill="{t["band"]}"/>')

    # quiet gridlines: zero, half and peak-ish
    for frac in (0, .5, 1):
        v = round(y_max / 1.15 * frac)
        gy = y(v)
        dash = "" if frac == 0 else ' stroke-dasharray="1 4"'
        a(f'<line x1="{left}" y1="{gy:.1f}" x2="{right}" y2="{gy:.1f}" stroke="{t["rule"]}"{dash}/>')
        if frac:
            text(right, gy - 6, str(v), 11, t["muted"], anchor="end")

    # area + line: monotone cubic, so the curve never overshoots the real daily values
    pts = [(x(i), y(c)) for i, c in enumerate(counts)]
    n = len(pts)
    sec = [(pts[i + 1][1] - pts[i][1]) / slot for i in range(n - 1)]
    tan = [sec[0]] + [0 if sec[i - 1] * sec[i] <= 0 else (sec[i - 1] + sec[i]) / 2 for i in range(1, n - 1)] + [sec[-1]]
    for i in range(n - 1):
        if sec[i] == 0:
            tan[i] = tan[i + 1] = 0
            continue
        al, be = tan[i] / sec[i], tan[i + 1] / sec[i]
        h = al * al + be * be
        if h > 9:
            k = 3 / h ** .5
            tan[i], tan[i + 1] = k * al * sec[i], k * be * sec[i]
    d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        d += (f" C{x0 + slot / 3:.1f},{y0 + tan[i] * slot / 3:.1f} "
              f"{x1 - slot / 3:.1f},{y1 - tan[i + 1] * slot / 3:.1f} {x1:.1f},{y1:.1f}")
    a(f'<path class="ar" d="{d} L{right},{bottom} L{left},{bottom} Z" fill="{t["area"]}" fill-opacity=".10"/>')
    a(f'<path class="ln" pathLength="1" d="{d}" fill="none" stroke="{t["ink"]}" stroke-width="1.8" '
      'stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (px, py) in enumerate(pts):  # invisible hover targets
        a(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="transparent"><title>{dates[i].strftime("%a %b %-d")}: {counts[i]}</title></circle>')

    # busiest day callout
    px, py = pts[peak_i]
    a(f'<g class="ar"><circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{t["accent"]}"/>')
    lx = px - 14 if px > 640 else px + 14
    anchor = "end" if px > 640 else "start"
    text(lx, py - 2, f"{dates[peak_i].strftime('%b %-d')}, the busiest day: {peak}", 14, t["accent"], True, anchor)
    a('</g>')

    # x labels: every Monday, plus first and last day
    for i, d in enumerate(dates):
        if d.weekday() == 0 or i in (0, span - 1):
            if i not in (0, span - 1) and (i < 4 or i > span - 4):
                continue
            anchor = "start" if i == 0 else "end" if i == span - 1 else "middle"
            text(x(i), bottom + 22, d.strftime("%b %-d"), 12, t["muted"], anchor=anchor)

    # footer
    a(f'<line x1="56" y1="{H - 42}" x2="864" y2="{H - 42}" stroke="{t["rule"]}"/>')
    text(56, H - 18, f"about {total / span:.1f} a day on average", 13, t["muted"], True)
    a(f'<rect x="{right - 150}" y="{H - 30}" width="12" height="12" fill="{t["band"]}" stroke="{t["rule"]}"/>')
    text(right, H - 19, "weekends are shaded", 13, t["muted"], True, "end")
    a("</svg>")
    return "\n".join(o) + "\n"


MILESTONES = (
    # metric key, title, how to say "n of these", thresholds (five markers along the way)
    ("commits", "Commits", "{n} commits", (100, 500, 1000, 2000, 4000)),
    ("pull_requests", "Pull requests", "{n} pull requests", (10, 50, 100, 500, 1000)),
    ("longest_streak", "Longest streak", "a {n}-day streak", (7, 30, 60, 100, 365)),
    ("years", "Years on GitHub", "{n} years on GitHub", (1, 2, 4, 6, 10)),
    ("repositories", "Repositories", "{n} repositories", (1, 10, 20, 35, 50)),
    ("issues", "Issues opened", "{n} issues opened", (1, 10, 50, 100, 500)),
    ("followers", "Followers", "{n} followers", (1, 10, 50, 200, 1000)),
    ("stars", "Stars earned", "{n} stars", (1, 10, 50, 200, 1000)),
)
MS_PAPER = {
    "light": dict(bg="#fbf8f2", ink="#1f1c18", soft="#4a443c", muted="#8a8174",
                  rule="#e4ddd0", track="#e4ddd0", accent="#c0561b"),
    "dark":  dict(bg="#15130f", ink="#efe9de", soft="#cfc6b8", muted="#8d8578",
                  rule="#2e2a23", track="#3a352d", accent="#e98a45"),
}
MS_SERIF = "'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif"
MS_SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _num(n):
    if isinstance(n, float) and not n.is_integer():
        return f"{n:.1f}"
    n = int(n)
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else f"{n:,}"


def _to_go(key, value, target):
    gap = target - value
    if key == "years":
        months = max(round(gap * 12), 1)
        return f"about {months} month{'s' if months != 1 else ''} to go"
    if key == "longest_streak":
        return f"{_num(gap)} more day{'s' if gap != 1 else ''} in a row"
    return f"{_num(gap)} to go"


def _say(phrase, n):
    s = phrase.format(n=_num(n))
    return s.replace("1 stars", "1 star").replace("1 followers", "1 follower").replace("1 issues", "1 issue")


def render_trophies_svg(metrics, theme="light"):
    """Eight measures, each a line with five markers; reached markers are filled.
    theme is "light" or "dark"."""
    t = MS_PAPER[theme]
    W, H = 920, 624
    o = []
    a = o.append

    def text(x, y, s, size=15, fill=None, italic=False, anchor="start", cls="s", extra=""):
        st = ' font-style="italic"' if italic else ""
        an = f' text-anchor="{anchor}"' if anchor != "start" else ""
        a(f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}"{st}{an} fill="{fill or t["soft"]}"{extra}>{s}</text>')

    rows = []
    for key, title, phrase, th in MILESTONES:
        v = metrics.get(key, 0) or 0
        reached = sum(1 for m in th if v >= m)
        rows.append((key, title, phrase, th, v, reached))
    done = sum(r[5] for r in rows)
    total = 5 * len(rows)
    pending = [r for r in rows if r[5] < 5]
    closest = min(pending, key=lambda r: (r[3][r[5]] - r[4]) / r[3][r[5]], default=None)

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
      f'aria-label="Milestones: {done} of {total} reached. ' +
      "; ".join(f"{title}: {_num(v)}, {reached} of 5" for _, title, _, _, v, reached in rows) + '.">')
    a(f'<style>.s{{font-family:{MS_SERIF}}} .k{{font-family:{MS_SANS}}}'
      '.p{transform-box:fill-box;transform-origin:left;animation:p 1.4s cubic-bezier(.2,.8,.2,1) both}'
      '@keyframes p{from{transform:scaleX(0)}}'
      '@media (prefers-reduced-motion:reduce){.p{animation:none}}</style>')
    a(f'<rect width="{W}" height="{H}" rx="10" fill="{t["bg"]}"/>')

    text(56, 52, "MILESTONES", 11, t["muted"], cls="k", extra=' letter-spacing="2.4"')
    text(864, 52, "markers along the way, not trophies", 13, t["muted"], True, "end")
    a(f'<line x1="56" y1="68" x2="864" y2="68" stroke="{t["rule"]}"/>')
    text(56, 110, f"{done} of {total} milestones reached so far.", 30, t["ink"], extra=' letter-spacing="-0.2"')
    if closest:
        key, _, phrase, th, v, reached = closest
        text(56, 142, f"Next up: {escape(_say(phrase, th[reached]))}, "
                      f"{escape(_to_go(key, v, th[reached]))}.", 20, t["accent"], True)

    colw, x0s, y0, rowh = 352, (56, 512), 196, 96
    for i, (key, title, phrase, th, v, reached) in enumerate(rows):
        x0 = x0s[i // 4]
        y = y0 + (i % 4) * rowh
        text(x0, y, escape(title), 17, t["ink"])
        text(x0 + colw, y, _num(v), 17, t["ink"], anchor="end")

        ty = y + 22
        ticks = [x0 + colw * (k + 1) / 5 for k in range(5)]
        a(f'<line x1="{x0}" y1="{ty}" x2="{x0 + colw}" y2="{ty}" stroke="{t["track"]}" stroke-width="2" stroke-linecap="round"/>')
        if reached == 5:
            px = x0 + colw
        else:
            lo_v = th[reached - 1] if reached else 0
            lo_x = ticks[reached - 1] if reached else x0
            frac = (v - lo_v) / (th[reached] - lo_v)
            px = lo_x + (ticks[reached] - lo_x) * max(0.0, min(frac, 1.0))
        if px > x0:
            a(f'<line class="p" style="animation-delay:{.15 + i * .08:.2f}s" x1="{x0}" y1="{ty}" x2="{px:.1f}" y2="{ty}" '
              f'stroke="{t["ink"]}" stroke-width="2" stroke-linecap="round"/>')
        for k, tx in enumerate(ticks):
            hit = k < reached
            a(f'<circle cx="{tx:.1f}" cy="{ty}" r="5" fill="{t["accent"] if hit else t["bg"]}" '
              f'stroke="{t["accent"] if hit else t["track"]}" stroke-width="1.5"><title>{_num(th[k])}</title></circle>')
            text(tx, ty + 20, _num(th[k]), 11, t["muted"], anchor="end" if k == 4 else "middle")

        note = "every marker reached" if reached == 5 else f"next: {_say(phrase, th[reached])} · {_to_go(key, v, th[reached])}"
        text(x0, ty + 44, escape(note), 13, t["muted"], True)

    a(f'<line x1="56" y1="{H - 42}" x2="864" y2="{H - 42}" stroke="{t["rule"]}"/>')
    a(f'<circle cx="62" cy="{H - 22}" r="5" fill="{t["accent"]}"/>')
    text(74, H - 18, "reached", 13, t["muted"], True)
    a(f'<circle cx="146" cy="{H - 22}" r="5" fill="{t["bg"]}" stroke="{t["track"]}" stroke-width="1.5"/>')
    text(158, H - 18, "still ahead", 13, t["muted"], True)
    text(864, H - 18, "public + private · refreshed every 6 hours", 13, t["muted"], True, "end")
    a("</svg>")
    return "\n".join(o) + "\n"


PAPER = {
    "light": dict(bg="#fbf8f2", ink="#1f1c18", soft="#4a443c", muted="#8a8174",
                  rule="#e4ddd0", bar="#d9d1c3", accent="#c0561b"),
    "dark":  dict(bg="#15130f", ink="#efe9de", soft="#cfc6b8", muted="#8d8578",
                  rule="#2e2a23", bar="#3a352d", accent="#e98a45"),
}
PAPER_SERIF = "'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif"
PAPER_SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _short(n):
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 10000 else f"{n:,}"


def _date(d, today):
    return d.strftime("%b %-d") if d.year == today.year else d.strftime("%b %-d, %Y")


def render_dashboard_svg(login, days, today, start, stats, metrics, theme="light"):
    """'GitHub, in numbers': an editorial page in the same paper style as the About Me
    and Tech Stack cards. theme is "light" or "dark"; main() writes both."""
    t = PAPER[theme]
    W, H = 920, 600
    o = []
    a = o.append

    def text(x, y, s, size=15, fill=None, italic=False, anchor="start", cls="s", extra=""):
        st = ' font-style="italic"' if italic else ""
        an = f' text-anchor="{anchor}"' if anchor != "start" else ""
        a(f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}"{st}{an} fill="{fill or t["soft"]}"{extra}>{s}</text>')

    def rule(y):
        a(f'<line x1="56" y1="{y}" x2="864" y2="{y}" stroke="{t["rule"]}"/>')

    # ---- figures ----------------------------------------------------------
    window = min(365, (today - start).days + 1)
    active = sum(1 for i in range(window) if days.get(today - timedelta(days=i), 0) > 0)
    this_year = sum(c for d, c in days.items() if d.year == today.year)
    peak_day = max(days.items(), key=lambda kv: (kv[1], kv[0]), default=(today, 0))
    per_dow = [0] * 7
    for i in range(window):
        d = today - timedelta(days=i)
        per_dow[d.weekday()] += days.get(d, 0)
    busiest = max(range(7), key=lambda i: per_dow[i])
    weekend_share = (per_dow[5] + per_dow[6]) / (sum(per_dow) or 1)
    years = list(range(start.year, today.year + 1))[-6:]
    per_year = [sum(c for d, c in days.items() if d.year == y) for y in years]
    best_year = max(range(len(years)), key=lambda i: per_year[i])
    langs = sorted(metrics["languages"].items(), key=lambda kv: -kv[1][0])[:5]
    lang_total = sum(size for _, (size, _) in sorted(metrics["languages"].items())) or 1

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
      f'aria-label="GitHub in numbers: {stats["total"]:,} contributions since {start.year}, {this_year:,} this year. '
      f'Current streak {stats["current"]} days, longest {stats["longest"]} days, active on {active} of the last {window} days, '
      f'{metrics["pull_requests"]:,} pull requests.">')
    a(f'<style>.s{{font-family:{PAPER_SERIF}}} .k{{font-family:{PAPER_SANS}}}'
      '.g{transform-box:fill-box;transform-origin:bottom;animation:g 1.1s cubic-bezier(.2,.8,.2,1) both}'
      '.w{transform-box:fill-box;transform-origin:left;animation:w 1.2s cubic-bezier(.2,.8,.2,1) both}'
      '@keyframes g{from{transform:scaleY(0)}} @keyframes w{from{transform:scaleX(0)}}'
      '@media (prefers-reduced-motion:reduce){.g,.w{animation:none}}</style>')
    a(f'<rect width="{W}" height="{H}" rx="10" fill="{t["bg"]}"/>')

    # ---- masthead + headline ---------------------------------------------
    text(56, 62, "GITHUB, IN NUMBERS", 11, t["muted"], cls="k", extra=' letter-spacing="2.4"')
    text(864, 62, f"public + private · updated {today.strftime('%b %-d, %Y')}", 13, t["muted"], True, "end")
    rule(78)
    text(56, 132, f"{stats['total']:,} contributions since {start.year},", 34, t["ink"], extra=' letter-spacing="-0.3"')
    text(56, 170, f"{this_year:,} of them this year.", 26, t["accent"], True)

    # ---- four figures ------------------------------------------------------
    cur_from = _date(stats["current_range"][0], today) if stats["current"] else None
    lo, hi = stats["longest_range"]
    same_run = stats["current"] and stats["current"] == stats["longest"]
    figures = [
        (str(stats["current"]), "days", "current streak",
         f"since {cur_from}" if cur_from else "starts with the next commit"),
        (str(stats["longest"]), "days", "longest streak",
         "and it’s this one" if same_run else (f"{_date(lo, today)} – {_date(hi, today)}" if lo else "")),
        (str(active), f"/ {window}", "days I showed up", "over the last year"),
        (_short(metrics["pull_requests"]), "", "pull requests",
         f"across {metrics['repositories']} repositories"),
    ]
    for i, (big, unit, cap, sub) in enumerate(figures):
        x = 56 + i * 206
        unit_t = f'<tspan font-size="17" fill="{t["muted"]}" dx="6">{unit}</tspan>' if unit else ""
        text(x, 248, f"{escape(big)}{unit_t}", 40, t["ink"])
        text(x, 274, escape(cap), 15, t["soft"])
        text(x, 294, escape(sub), 14, t["muted"], True)
    rule(326)

    # ---- when I work (weekday bars) ---------------------------------------
    text(56, 362, "When I work", 15, t["accent"], True)
    base, tall, bw, gap = 486, 92, 30, 22
    vmax = max(per_dow) or 1
    for i, v in enumerate(per_dow):
        x = 56 + i * (bw + gap)
        h = max(tall * v / vmax, 2)
        col = t["accent"] if i == busiest else t["bar"]
        a(f'<rect class="g" style="animation-delay:{.1 + i * .06:.2f}s" x="{x}" y="{base - h:.1f}" width="{bw}" '
          f'height="{h:.1f}" rx="2" fill="{col}"><title>{DAY_NAMES[i]}: {v:,}</title></rect>')
        text(x + bw / 2, base + 20, DAY_NAMES[i][:3], 13, t["ink"] if i == busiest else t["muted"], anchor="middle")
    note = f"Busiest on {DAY_NAMES[busiest]}s."
    if weekend_share < 0.2:
        note += " Weekends stay fairly quiet."
    text(56, 540, note, 14, t["muted"], True)

    # ---- what I write (languages) -----------------------------------------
    lx, lw = 500, 364
    text(lx, 362, "What I write", 15, t["accent"], True)
    x = lx
    shades = [1, .55, .4, .28, .18]
    for i, (name, (size, _)) in enumerate(langs):
        w = lw * size / lang_total
        col = t["accent"] if i == 0 else t["ink"]
        op = 1 if i == 0 else shades[i]
        a(f'<rect class="w" style="animation-delay:{.2 + i * .08:.2f}s" x="{x:.1f}" y="378" width="{max(w - 2, 1):.1f}" '
          f'height="8" rx="1.5" fill="{col}" fill-opacity="{op}"><title>{escape(name)}</title></rect>')
        x += w
    for i, (name, (size, _)) in enumerate(langs):
        col_x = lx + (i % 3) * 124
        y = 414 + (i // 3) * 24
        text(col_x, y, f'{escape(name[:12])} <tspan fill="{t["muted"]}" font-style="italic">'
             f'{100 * size / lang_total:.0f}%</tspan>', 14, t["soft"])

    # ---- year by year -------------------------------------------------------
    text(lx, 484, "Year by year", 15, t["accent"], True)
    step = lw / len(years)
    for i, (y, v) in enumerate(zip(years, per_year)):
        cx = lx + i * step
        best = i == best_year
        label = f"{y}" + (" so far" if y == today.year else "")
        text(cx, 512, _short(v) if v else "–", 17 if best else 15, t["ink"] if best else (t["soft"] if v else t["muted"]))
        text(cx, 532, label if best else str(y), 12, t["accent"] if best else t["muted"], best)

    # ---- footer -------------------------------------------------------------
    rule(560)
    text(56, 584, f"busiest single day: {peak_day[1]} contributions on {_date(peak_day[0], today)}", 13, t["muted"], True)
    stars = metrics["stars"]
    star_txt = "no stars yet" if not stars else f"{stars:,} star{'s' if stars != 1 else ''}"
    text(864, 584, f"{metrics['repositories']} repositories · {metrics['followers']} followers · {star_txt}",
         13, t["muted"], True, "end")
    a("</svg>")
    return "\n".join(o) + "\n"


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: profile_stats.py <username> <output_dir>")
    login, out_dir = sys.argv[1], sys.argv[2]
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is not set")

    tz = ZoneInfo(os.environ.get("PROFILE_TZ", "UTC"))
    start, today, calendar, public_commits = fetch_calendar(token, login, tz)
    private = fetch_private_commits(token, login, start, tz)
    days = merge(calendar, {d: c for d, c in private.items() if start <= d <= today})
    stats = compute_stats(start, today, days)
    print(f"private commits found: {sum(private.values())}")

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "streak-stats.svg"), "w") as f:
        f.write(render_svg(stats, today))
    title = os.environ.get("ACTIVITY_TITLE", f"{login}'s Contribution Activity")
    for theme, name in (("light", "activity-graph.svg"), ("dark", "activity-graph-dark.svg")):
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(render_activity_svg(days, today, title, theme=theme))
    metrics = fetch_profile(token, login)
    metrics["commits"] = public_commits + sum(private.values())
    metrics["years"] = round((today - start).days / 365.25, 1)
    metrics["longest_streak"] = stats["longest"]
    for theme, name in (("light", "trophies.svg"), ("dark", "trophies-dark.svg")):
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(render_trophies_svg(metrics, theme))
    for theme, name in (("light", "dashboard.svg"), ("dark", "dashboard-dark.svg")):
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(render_dashboard_svg(login, days, today, start, stats, metrics, theme))
    print(f"total={stats['total']} current={stats['current']} longest={stats['longest']}")
    print(f"trophies={ {k: v for k, v in metrics.items() if k != 'languages'} }")


if __name__ == "__main__":
    main()

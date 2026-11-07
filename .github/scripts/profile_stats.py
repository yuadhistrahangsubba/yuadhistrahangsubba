"""Generate GitHub streak stats, activity graph and trophy SVGs that include private contributions.

The public contribution calendar leaves out private work unless "Include private
contributions on my profile" is enabled, so public card services can't see it.
This script merges the calendar with commits the token can read in private
repositories (personal and organization), on every branch, counted per day in
PROFILE_TZ (default UTC).

Writes streak-stats.svg, activity-graph.svg, trophies.svg (achievement skill tree) and dashboard.svg into <output_dir>.

Usage: GH_TOKEN=... python profile_stats.py <username> <output_dir>
"""

import http.client
import json
import math
import os
import random
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


ACTIVITY_THEME = {
    "background": "#0D1117",
    "text": "#00FFC0",
    "line": "#FF00FF",
    "point": "#00FFC0",
    "area": "#FF00FF",
    "grid": "#21262D",
}


def nice_step(peak, ticks=5):
    """Smallest 1/2/5 x 10^n step that covers peak in about `ticks` gridlines."""
    raw = max(peak, 1) / ticks
    magnitude = 10 ** (len(str(int(raw))) - 1)
    for mult in (1, 2, 5, 10):
        if mult * magnitude >= raw:
            return mult * magnitude
    return 10 * magnitude


def render_activity_svg(days, today, title, span=31):
    t = ACTIVITY_THEME
    width, height = 1000, 360
    left, right, top, bottom = 70, 30, 70, 60
    plot_w, plot_h = width - left - right, height - top - bottom

    dates = [today - timedelta(days=span - 1 - i) for i in range(span)]
    counts = [days.get(d, 0) for d in dates]
    step = nice_step(max(counts))
    y_max = step * max(1, -(-max(counts) // step))

    def x(i):
        return left + plot_w * i / (span - 1)

    def y(v):
        return top + plot_h * (1 - v / y_max)

    points = [(x(i), y(c)) for i, c in enumerate(counts)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
    area = f"{left},{top + plot_h} {line} {left + plot_w},{top + plot_h}"

    grid = []
    for v in range(0, y_max + 1, step):
        gy = y(v)
        grid.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}" stroke="{t["grid"]}"/>')
        grid.append(f'<text x="{left - 12}" y="{gy + 4:.1f}" text-anchor="end">{v}</text>')
    labels = [
        f'<text x="{x(i):.1f}" y="{top + plot_h + 22}" text-anchor="middle">{d.day}</text>'
        for i, d in enumerate(dates)
    ]
    dots = [
        f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{t["point"]}"><title>{dates[i].isoformat()}: {counts[i]}</title></circle>'
        for i, (px, py) in enumerate(points)
    ]
    period = f"{dates[0].strftime('%b %-d')} - {dates[-1].strftime('%b %-d, %Y')}"
    nl = "\n    "
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" rx="6" fill="{t['background']}"/>
  <g font-family="'Segoe UI', Ubuntu, sans-serif" fill="{t['text']}" font-size="12">
    <text x="{width / 2}" y="38" text-anchor="middle" font-size="22" font-weight="700">{escape(title)}</text>
    {nl.join(grid)}
    {nl.join(labels)}
    <text x="{left + plot_w / 2}" y="{height - 14}" text-anchor="middle" font-size="13">{escape(period)}</text>
    <text transform="translate(20 {top + plot_h / 2}) rotate(-90)" text-anchor="middle" font-size="13">Contributions</text>
  </g>
  <polygon points="{area}" fill="{t['area']}" fill-opacity="0.15"/>
  <polyline points="{line}" fill="none" stroke="{t['line']}" stroke-width="2.5" stroke-linejoin="round"/>
  {nl.join(dots)}
</svg>
"""


# Achievement tiers: name, colour, gamerscore points for reaching it.
TIERS = (
    ("Bronze", "#cd7f32", 10),
    ("Silver", "#c9d1d9", 25),
    ("Gold", "#e3b341", 50),
    ("Platinum", "#79c0ff", 100),
    ("Diamond", "#d2a8ff", 200),
)
ACHIEVEMENTS = (
    # metric key, title, icon, (singular, plural) unit, value needed for each tier
    ("commits", "Commit Machine", "💾", ("commit", "commits"), (100, 500, 1000, 2000, 4000)),
    ("pull_requests", "Merge Master", "🔀", ("pull request", "pull requests"), (10, 50, 100, 500, 1000)),
    ("longest_streak", "Unbreakable", "🔥", ("day streak", "day streak"), (7, 30, 60, 100, 365)),
    ("years", "Veteran", "🎖", ("year on GitHub", "years on GitHub"), (1, 2, 4, 6, 10)),
    ("repositories", "Service Architect", "🧱", ("repository", "repositories"), (1, 10, 20, 35, 50)),
    ("issues", "Bug Hunter", "🐛", ("issue opened", "issues opened"), (1, 10, 50, 100, 500)),
    ("followers", "Crowd Puller", "👥", ("follower", "followers"), (1, 10, 50, 200, 1000)),
    ("stars", "Star Collector", "⭐", ("star earned", "stars earned"), (1, 10, 50, 200, 1000)),
)
HEXAGON = "M0,-26 L22.5,-13 L22.5,13 L0,26 L-22.5,13 L-22.5,-13 Z"


def short_number(n):
    if isinstance(n, float):
        return f"{n:.1f}".rstrip("0").rstrip(".")
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def tier_for(value, thresholds):
    """Index of the highest tier reached, or -1 while still locked."""
    return sum(1 for minimum in thresholds if value >= minimum) - 1


def render_trophies_svg(metrics):
    """Achievements as a constellation skill tree: the player in the middle,
    one star node per achievement, coloured by tier, ringed by progress."""
    t = DASH
    W, H = 920, 650
    cx, cy, rx, ry = W / 2, 318, 318, 196
    rng = random.Random(7)

    reached = [(ach, tier_for(metrics[ach[0]], ach[4])) for ach in ACHIEVEMENTS]
    unlocked = sum(1 for _, tier in reached if tier >= 0)
    level = sum(tier + 1 for _, tier in reached)
    score = sum(sum(TIERS[k][2] for k in range(tier + 1)) for _, tier in reached)
    summary = "; ".join(
        f"{ach[1]}: {TIERS[tier][0] if tier >= 0 else 'locked'} ({short_number(metrics[ach[0]])} {ach[3][metrics[ach[0]] != 1]})"
        for ach, tier in reached)

    nodes = []
    for i, (ach, tier) in enumerate(reached):
        ang = -math.pi / 2 + i * 2 * math.pi / len(reached)
        nodes.append((cx + rx * math.cos(ang), cy + ry * math.sin(ang), ach, tier))

    out = []
    a = out.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
      f"font-family='{DASH_MONO}' role=\"img\" aria-label=\"Skill tree: level {level}, {unlocked} of "
      f'{len(ACHIEVEMENTS)} achievements unlocked, gamerscore {score}. {escape(summary)}.">')
    a(f"""<defs>
  <radialGradient id="sky" cx=".5" cy=".55" r=".75"><stop offset="0" stop-color="#151a3d"/><stop offset=".6" stop-color="#0b1022"/><stop offset="1" stop-color="#05070f"/></radialGradient>
  <radialGradient id="halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{t["purple"]}" stop-opacity=".45"/><stop offset="1" stop-color="{t["purple"]}" stop-opacity="0"/></radialGradient>
  <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{t["purple"]}"/><stop offset=".35" stop-color="{t["blue"]}"/><stop offset=".7" stop-color="{t["green"]}"/><stop offset="1" stop-color="{t["orange"]}"/>
    <animateTransform attributeName="gradientTransform" type="rotate" from="0 .5 .5" to="360 .5 .5" dur="10s" repeatCount="indefinite"/>
  </linearGradient>
  <filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <clipPath id="clip"><rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="16"/></clipPath>
</defs>
<style>
  .lbl {{ paint-order: stroke; stroke: #0b1022; stroke-width: 5px; stroke-linejoin: round; }}
  .in {{ opacity: 0; animation: in .7s cubic-bezier(.2,.8,.2,1) forwards; }}
  .draw {{ stroke-dasharray: 1; stroke-dashoffset: 1; animation: draw 1.2s ease-out forwards; }}
  .ring {{ stroke-dasharray: 100; stroke-dashoffset: 100; animation: ring 1.4s ease-out forwards; }}
  .tw {{ animation: tw 3s ease-in-out infinite; }}
  .pulse {{ animation: pulse 2.6s ease-in-out infinite; }}
  .spin {{ transform-box: fill-box; transform-origin: center; animation: spin 40s linear infinite; }}
  @keyframes in {{ from {{ opacity: 0; transform: scale(.96); }} to {{ opacity: 1; transform: none; }} }}
  @keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}
  @keyframes ring {{ to {{ stroke-dashoffset: var(--p); }} }}
  @keyframes tw {{ 0%,100% {{ opacity: .2; }} 50% {{ opacity: .9; }} }}
  @keyframes pulse {{ 0%,100% {{ opacity: .15; }} 50% {{ opacity: .45; }} }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @media (prefers-reduced-motion: reduce) {{
    .in {{ animation: none; opacity: 1; }}
    .draw {{ animation: none; stroke-dashoffset: 0; }}
    .ring {{ animation: none; stroke-dashoffset: var(--p); }}
    .tw, .pulse, .spin {{ animation: none; }}
  }}
</style>
<g clip-path="url(#clip)">
<rect width="{W}" height="{H}" fill="url(#sky)"/>""")

    for _ in range(90):
        a(f'<circle class="tw" cx="{rng.uniform(8, W - 8):.0f}" cy="{rng.uniform(8, H - 8):.0f}" r="{rng.choice((.6, .8, 1, 1.3))}" fill="#fff" '
          f'style="animation-delay:{rng.uniform(0, 3):.2f}s;animation-duration:{rng.uniform(2, 5):.1f}s"/>')

    # Orbit guide and the constellation outline between neighbouring unlocked nodes.
    a(f'<ellipse class="spin" cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="none" stroke="#ffffff" stroke-opacity=".06" stroke-dasharray="2 10"/>')
    for i, (x, y, _, tier) in enumerate(nodes):
        nx, ny, _, ntier = nodes[(i + 1) % len(nodes)]
        if tier >= 0 and ntier >= 0:
            a(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#ffffff" stroke-opacity=".08"/>')

    # Branches from the player to each node, with energy pulses on unlocked ones.
    for i, (x, y, _, tier) in enumerate(nodes):
        if tier < 0:
            a(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#ffffff" stroke-opacity=".12" stroke-dasharray="3 6"/>')
            continue
        color = TIERS[tier][1]
        a(f'<path class="draw" style="animation-delay:{.3 + i * .08:.2f}s" pathLength="1" d="M{cx},{cy} L{x:.1f},{y:.1f}" stroke="{color}" stroke-opacity=".45" stroke-width="1.6"/>')
        a(f'<circle r="3" fill="{color}" filter="url(#glow)" opacity="0">'
          f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.1;.85;1" dur="2.8s" begin="{1.4 + i * .35:.2f}s" repeatCount="indefinite"/>'
          f'<animateMotion path="M{cx},{cy} L{x:.1f},{y:.1f}" dur="2.8s" begin="{1.4 + i * .35:.2f}s" repeatCount="indefinite"/></circle>')

    # Player core.
    a(f'<g class="in" style="transform-origin:{cx}px {cy}px">')
    a(f'<circle class="pulse" cx="{cx}" cy="{cy}" r="78" fill="url(#halo)"/>')
    a(f'<path d="{HEXAGON}" transform="translate({cx} {cy}) scale(1.75)" fill="#0c1226" stroke="url(#edge)" stroke-width="1.4"/>')
    a(f'<text x="{cx}" y="{cy - 2}" font-size="20" font-weight="700" fill="{t["fg"]}" text-anchor="middle">Y.D</text>')
    a(f'<text x="{cx}" y="{cy + 18}" font-size="11" font-weight="700" fill="{t["green"]}" text-anchor="middle" letter-spacing=".1em">LVL {level}</text>')
    a("</g>")

    # Achievement nodes.
    for i, (x, y, (key, title, icon, unit, thresholds), tier) in enumerate(nodes):
        value = metrics[key]
        locked = tier < 0
        color = "#3d444d" if locked else TIERS[tier][1]
        if tier == len(TIERS) - 1:
            progress, hint = 100, "max tier ✦"
        else:
            lo = thresholds[tier] if tier >= 0 else 0
            hi = thresholds[tier + 1]
            progress = 100 * max(0.0, min(1.0, (value - lo) / (hi - lo)))
            hint = f"→ {TIERS[tier + 1][0]} at {short_number(hi)}"
        next_color = TIERS[min(tier + 1, len(TIERS) - 1)][1]
        delay = .5 + i * .1
        a(f'<g class="in" style="animation-delay:{delay:.2f}s;transform-origin:{x:.1f}px {y:.1f}px">')
        if not locked:
            a(f'<circle class="pulse" cx="{x:.1f}" cy="{y:.1f}" r="40" fill="{color}" style="animation-delay:{i * .3:.1f}s"/>')
        a(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="34" fill="none" stroke="#ffffff" stroke-opacity=".08" stroke-width="3"/>')
        if progress > 0:
            a(f'<circle class="ring" style="animation-delay:{delay + .5:.2f}s;--p:{100 - progress:.1f}" cx="{x:.1f}" cy="{y:.1f}" r="34" fill="none" stroke="{next_color}" stroke-width="3" stroke-linecap="round" pathLength="100" transform="rotate(-90 {x:.1f} {y:.1f})"/>')
        a(f'<path d="{HEXAGON}" transform="translate({x:.1f} {y:.1f})" fill="#0c1226" stroke="{color}" stroke-width="2"/>')
        a(f'<path d="{HEXAGON}" transform="translate({x:.1f} {y:.1f})" fill="{color}" fill-opacity="{.08 if locked else .2}"/>')
        a(f'<text x="{x:.1f}" y="{y + 7:.1f}" font-size="19" text-anchor="middle" opacity="{.45 if locked else 1}">{"🔒" if locked else icon}</text>')
        ly = y + 52
        a(f'<text x="{x:.1f}" y="{ly:.1f}" class="lbl" font-size="12" font-weight="700" fill="{t["dim"] if locked else t["fg"]}" text-anchor="middle">{escape(title)}</text>')
        a(f'<text x="{x:.1f}" y="{ly + 15:.1f}" class="lbl" font-size="10" text-anchor="middle" fill="{t["muted"]}">'
          f'<tspan fill="{t["muted"] if locked else color}" font-weight="700">{"LOCKED" if locked else TIERS[tier][0].upper()}</tspan>'
          f' · {escape(short_number(value))} {escape(unit[value != 1])}</text>')
        a(f'<text x="{x:.1f}" y="{ly + 29:.1f}" class="lbl" font-size="9" fill="{t["muted"]}" text-anchor="middle">{escape(hint)}</text>')
        a("</g>")

    # Header.
    a(f"""<g class="in">
  <text x="24" y="40" font-size="18" font-weight="700" fill="{t["fg"]}" letter-spacing=".04em">SKILL TREE<tspan fill="{t["purple"]}"> / </tspan>player 1</text>
  <text x="24" y="60" font-size="10.5" fill="{t["muted"]}">{unlocked}/{len(ACHIEVEMENTS)} unlocked · ring = progress to next tier</text>
  <rect x="{W - 24 - 150}" y="20" width="150" height="44" rx="12" fill="{t["green"]}" fill-opacity=".08" stroke="{t["green"]}" stroke-opacity=".35"/>
  <text x="{W - 24 - 134}" y="37" font-size="9.5" fill="{t["dim"]}" letter-spacing=".1em">GAMERSCORE</text>
  <text x="{W - 24 - 134}" y="56" font-size="16" font-weight="700" fill="{t["green"]}">G {score:,}</text>
</g>""")
    legend_x = 24
    for i, (name, color, _) in enumerate(TIERS):
        x = legend_x + i * 82
        a(f'<g class="in"><circle cx="{x + 5}" cy="{H - 22}" r="4.5" fill="{color}"/><text x="{x + 15}" y="{H - 18}" font-size="10" fill="{t["dim"]}">{name}</text></g>')

    a("</g>")
    a(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="16" fill="none" stroke="url(#edge)" stroke-width="1.6"/>')
    a("</svg>")
    return "\n".join(out) + "\n"


DASH = {
    "bg0": "#070b14", "bg1": "#121433", "panel": "#0c1226", "fg": "#e6edf3", "dim": "#8b949e",
    "muted": "#6e7681", "purple": "#d2a8ff", "blue": "#79c0ff", "green": "#7ee787",
    "orange": "#ffa657", "yellow": "#e3b341", "red": "#ff7b72",
}
DASH_MONO = "\"JetBrains Mono\",\"SF Mono\",Menlo,Consolas,monospace"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def render_dashboard_svg(login, days, today, start, stats, metrics):
    """One observability-style dashboard: streaks as uptime, contributions as
    requests, weekday load, contributions per year, languages and counters.
    The daily graph and calendar are left to activity-graph.svg and the snake."""
    t = DASH
    W, H = 920, 556
    out = []
    a = out.append

    def panel(x, y, w, h, title, right="", delay=0.0, accent=t["purple"]):
        a(f'<g class="in" style="animation-delay:{delay:.2f}s">')
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{t["panel"]}" fill-opacity=".9" stroke="#ffffff" stroke-opacity=".08"/>')
        a(f'<rect x="{x + 16}" y="{y + 16}" width="6" height="6" rx="3" fill="{accent}"/>')
        a(f'<text x="{x + 30}" y="{y + 23}" font-size="10.5" font-weight="700" fill="{t["dim"]}" letter-spacing=".12em">{escape(title.upper())}</text>')
        if right:
            a(f'<text x="{x + w - 16}" y="{y + 23}" font-size="10.5" fill="{t["muted"]}" text-anchor="end">{right}</text>')

    def bars(x0, y0, w, h, labels, values, best, delay):
        left, right, top, bottom = x0 + 24, x0 + w - 24, y0 + 52, y0 + h - 28
        slot = (right - left) / len(values)
        bw = min(46, slot * 0.56)
        vmax = max(values) or 1
        for i, (label, v) in enumerate(zip(labels, values)):
            bx = left + slot * i + (slot - bw) / 2
            bh = max((bottom - top) * v / vmax, 2)
            col, op = (t["green"], 1) if i == best else (t["purple"], .55)
            a(f'<rect x="{bx:.1f}" y="{top}" width="{bw:.1f}" height="{bottom - top}" rx="5" fill="#ffffff" fill-opacity=".04"/>')
            a(f'<rect class="rise" style="animation-delay:{delay + i * 0.07:.2f}s" x="{bx:.1f}" y="{bottom - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="5" fill="{col}" fill-opacity="{op}"><title>{escape(label)}: {v:,}</title></rect>')
            a(f'<text x="{bx + bw / 2:.1f}" y="{bottom - bh - 6:.1f}" font-size="10" fill="{t["fg"] if i == best else t["dim"]}" text-anchor="middle">{short_number(v)}</text>')
            a(f'<text x="{bx + bw / 2:.1f}" y="{bottom + 16}" font-size="9.5" fill="{t["muted"]}" text-anchor="middle">{escape(label)}</text>')

    # --- figures -----------------------------------------------------------
    window = min(365, (today - start).days + 1)
    active = sum(1 for i in range(window) if days.get(today - timedelta(days=i), 0) > 0)
    slo = 100 * active / window
    peak_day = max(days.items(), key=lambda kv: (kv[1], kv[0]), default=(today, 0))
    langs = sorted(metrics["languages"].items(), key=lambda kv: -kv[1][0])
    lang_total = sum(size for _, (size, _) in langs) or 1

    a(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family='{DASH_MONO}' role="img" aria-label="GitHub stats dashboard: current streak {stats["current"]} days, longest streak {stats["longest"]} days, {stats["total"]:,} total contributions, {slo:.1f}% of the last {window} days active.">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{t["bg0"]}"/><stop offset="1" stop-color="{t["bg1"]}"/></linearGradient>
  <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{t["purple"]}"/><stop offset=".35" stop-color="{t["blue"]}"/><stop offset=".7" stop-color="{t["green"]}"/><stop offset="1" stop-color="{t["orange"]}"/>
    <animateTransform attributeName="gradientTransform" type="rotate" from="0 .5 .5" to="360 .5 .5" dur="10s" repeatCount="indefinite"/>
  </linearGradient>
  <pattern id="dots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" fill-opacity=".05"/></pattern>
  <clipPath id="clip"><rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="16"/></clipPath>
</defs>
<style>
  .in {{ opacity: 0; animation: in .6s cubic-bezier(.2,.8,.2,1) forwards; }}
  .rise {{ transform-box: fill-box; transform-origin: bottom; transform: scaleY(0); animation: rise 1s cubic-bezier(.2,.8,.2,1) forwards; }}
  .grow {{ transform-box: fill-box; transform-origin: left; transform: scaleX(0); animation: grow 1.2s cubic-bezier(.2,.8,.2,1) forwards; }}
  .ring {{ stroke-dasharray: 100; stroke-dashoffset: 100; animation: ring 1.6s .4s ease-out forwards; }}
  .pulse {{ transform-box: fill-box; transform-origin: center; animation: pulse 1.8s ease-in-out infinite; }}
  @keyframes in {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: none; }} }}
  @keyframes rise {{ to {{ transform: scaleY(1); }} }}
  @keyframes grow {{ to {{ transform: scaleX(1); }} }}
  @keyframes ring {{ to {{ stroke-dashoffset: {100 - slo:.1f}; }} }}
  @keyframes pulse {{ 0%,100% {{ transform: scale(1); opacity: 1; }} 50% {{ transform: scale(1.8); opacity: .3; }} }}
  @media (prefers-reduced-motion: reduce) {{
    .in {{ animation: none; opacity: 1; }}
    .grow, .rise {{ animation: none; transform: none; }}
    .ring {{ animation: none; stroke-dashoffset: {100 - slo:.1f}; }}
    .pulse {{ animation: none; }}
  }}
</style>
<g clip-path="url(#clip)">
<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#dots)"/>''')

    # --- header ------------------------------------------------------------
    a(f'''<g class="in">
<text x="24" y="42" font-size="19" font-weight="700" fill="{t["fg"]}" letter-spacing=".04em">YD-PLATFORM<tspan fill="{t["purple"]}"> / </tspan>observability</text>
<text x="24" y="62" font-size="11" fill="{t["muted"]}">github.com/{escape(login)} · public + private · refreshed every 6h</text>
<circle class="pulse" cx="{W - 150}" cy="38" r="4" fill="{t["green"]}"/>
<text x="{W - 140}" y="42" font-size="11" fill="{t["green"]}">live</text>
<text x="{W - 24}" y="42" font-size="11" fill="{t["dim"]}" text-anchor="end">{today.strftime("%b %-d, %Y")}</text>
</g>''')

    # --- stat tiles ----------------------------------------------------------
    ty, th, gap = 80, 92, 14
    tw = (W - 48 - 3 * gap) / 4
    cur_since = f"since {fmt_date(stats['current_range'][0], today)}" if stats["current"] else "next commit starts it"
    tiles = [
        ("uptime", plural(stats["current"], "day"), cur_since, t["green"]),
        ("longest uptime", plural(stats["longest"], "day"), fmt_range(stats["longest_range"], today), t["orange"]),
        ("requests served", f"{stats['total']:,}", f"contributions since {start.year}", t["blue"]),
        (f"slo · {window}d", f"{slo:.1f}%", f"{active}/{window} days active", t["purple"]),
    ]
    for i, (title, value, sub, col) in enumerate(tiles):
        x = 24 + i * (tw + gap)
        panel(x, ty, tw, th, title, delay=0.1 + i * 0.08, accent=col)
        a(f'<text x="{x + 16}" y="{ty + 58}" font-size="24" font-weight="700" fill="{t["fg"]}">{escape(value)}</text>')
        a(f'<text x="{x + 16}" y="{ty + 78}" font-size="10" fill="{t["muted"]}">{escape(sub)}</text>')
        if i == 0 and stats["current"]:
            a(f'<circle class="pulse" cx="{x + tw - 22}" cy="{ty + 50}" r="5" fill="{col}"/>')
        if i == 3:
            rx, ry = x + tw - 34, ty + 52
            a(f'<circle cx="{rx}" cy="{ry}" r="17" fill="none" stroke="#ffffff" stroke-opacity=".08" stroke-width="5"/>')
            a(f'<circle class="ring" cx="{rx}" cy="{ry}" r="17" fill="none" stroke="{col}" stroke-width="5" stroke-linecap="round" pathLength="100" transform="rotate(-90 {rx} {ry})"/>')
        a('</g>')

    # --- load by weekday ---------------------------------------------------
    cy0, ch = ty + th + gap, 176
    cw = 548
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    per_dow = [0] * 7
    for i in range(window):
        d = today - timedelta(days=i)
        per_dow[d.weekday()] += days.get(d, 0)
    busiest = max(range(7), key=lambda i: per_dow[i])
    panel(24, cy0, cw, ch, f"load by weekday · {window}d", f"busiest: {names[busiest]}", 0.4, t["purple"])
    bars(24, cy0, cw, ch, names, per_dow, busiest, 0.6)
    a('</g>')

    # --- languages -----------------------------------------------------------
    lx0 = 24 + cw + gap
    lw = W - 24 - lx0
    panel(lx0, cy0, lw, ch, "traffic by language", "by bytes", 0.5, t["blue"])
    top = langs[:6]
    for i, (name, (size, color)) in enumerate(top):
        y = cy0 + 48 + i * 21
        pct = 100 * size / lang_total
        bar_x, bar_w = lx0 + 112, lw - 112 - 58
        a(f'<circle cx="{lx0 + 20}" cy="{y - 3.5}" r="4" fill="{color or t["dim"]}"/>')
        a(f'<text x="{lx0 + 30}" y="{y}" font-size="10.5" fill="{t["fg"]}">{escape(name[:11])}</text>')
        a(f'<rect x="{bar_x}" y="{y - 7}" width="{bar_w}" height="6" rx="3" fill="#ffffff" fill-opacity=".06"/>')
        a(f'<rect class="grow" style="animation-delay:{0.7 + i * 0.1:.2f}s" x="{bar_x}" y="{y - 7}" width="{max(bar_w * pct / 100, 2):.1f}" height="6" rx="3" fill="{color or t["dim"]}"/>')
        a(f'<text x="{lx0 + lw - 16}" y="{y}" font-size="10" fill="{t["dim"]}" text-anchor="end">{pct:.1f}%</text>')
    a('</g>')

    # --- releases per year -------------------------------------------------
    hy0, hh = cy0 + ch + gap, H - 24 - (cy0 + ch + gap)
    years = list(range(start.year, today.year + 1))[-7:]
    per_year = [sum(c for d, c in days.items() if d.year == y) for y in years]
    best = max(range(len(years)), key=lambda i: per_year[i])
    labels = [f"{y} YTD" if y == today.year else str(y) for y in years]
    panel(24, hy0, cw, hh, "releases · contributions per year", f"best: {years[best]} · {per_year[best]:,}", 0.6, t["green"])
    bars(24, hy0, cw, hh, labels, per_year, best, 0.8)
    a('</g>')

    # --- counters ------------------------------------------------------------
    panel(lx0, hy0, lw, hh, "counters", "", 0.7, t["orange"])
    counters = [
        ("deploys", short_number(metrics["pull_requests"]), "pull requests", t["green"]),
        ("tickets", short_number(metrics["issues"]), "issues", t["yellow"]),
        ("services", short_number(metrics["repositories"]), "repositories", t["blue"]),
        ("stars", short_number(metrics["stars"]), "earned", t["yellow"]),
        ("followers", short_number(metrics["followers"]), "humans", t["purple"]),
        ("peak day", short_number(peak_day[1]), fmt_date(peak_day[0], today), t["red"]),
    ]
    colw, rowh = (lw - 32) / 3, (hh - 44) / 2
    for i, (label, value, sub, col) in enumerate(counters):
        x = lx0 + 16 + (i % 3) * colw
        y = hy0 + 40 + (i // 3) * rowh
        a(f'<text x="{x}" y="{y + 8}" font-size="9.5" fill="{t["muted"]}" letter-spacing=".06em">{escape(label.upper())}</text>')
        a(f'<text x="{x}" y="{y + 30}" font-size="18" font-weight="700" fill="{col}">{escape(value)}</text>')
        a(f'<text x="{x}" y="{y + 44}" font-size="9" fill="{t["muted"]}">{escape(sub)}</text>')
    a('</g>')

    a('</g>')
    a(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="16" fill="none" stroke="url(#edge)" stroke-width="1.6"/>')
    a('</svg>')
    return "\n".join(out) + "\n"


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
    with open(os.path.join(out_dir, "activity-graph.svg"), "w") as f:
        title = os.environ.get("ACTIVITY_TITLE", f"{login}'s Contribution Activity")
        f.write(render_activity_svg(days, today, title))
    metrics = fetch_profile(token, login)
    metrics["commits"] = public_commits + sum(private.values())
    metrics["years"] = round((today - start).days / 365.25, 1)
    metrics["longest_streak"] = stats["longest"]
    with open(os.path.join(out_dir, "trophies.svg"), "w") as f:
        f.write(render_trophies_svg(metrics))
    with open(os.path.join(out_dir, "dashboard.svg"), "w") as f:
        f.write(render_dashboard_svg(login, days, today, start, stats, metrics))
    print(f"total={stats['total']} current={stats['current']} longest={stats['longest']}")
    print(f"trophies={ {k: v for k, v in metrics.items() if k != 'languages'} }")


if __name__ == "__main__":
    main()

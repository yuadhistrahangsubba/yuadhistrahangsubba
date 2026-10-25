"""Generate GitHub streak stats and activity graph SVGs that include private contributions.

The public contribution calendar leaves out private work unless "Include private
contributions on my profile" is enabled, so public card services can't see it.
This script merges the calendar with commits the token can read in private
repositories (personal and organization), counted per day.

Writes streak-stats.svg and activity-graph.svg into <output_dir>.

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
from datetime import date, datetime, timedelta, timezone
from html import escape

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


def graphql(token, variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise SystemExit(f"GraphQL error: {payload['errors']}")
    return payload["data"]["user"]


def fetch_calendar(token, login):
    """Return {date: count} from account creation until today, one year per request."""
    today = datetime.now(timezone.utc).date()
    created = graphql(token, {"login": login})["createdAt"]
    start = datetime.fromisoformat(created.replace("Z", "+00:00")).date()

    days = {}
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
        cursor = date(cursor.year + 1, 1, 1)
    return start, today, days


def rest_pages(token, url):
    """Yield items from a paginated REST endpoint, following Link headers."""
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
        yield from items
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None


def fetch_private_commits(token, login, start):
    """Return {date: count} of the user's commits on default branches of private repos."""
    repos = rest_pages(
        token,
        f"{REST}/user/repos?visibility=private"
        "&affiliation=owner,collaborator,organization_member&per_page=100",
    )
    days = Counter()
    for repo in repos:
        if repo["fork"]:
            continue  # GitHub doesn't count commits to forks either.
        commits = rest_pages(
            token,
            f"{REST}/repos/{repo['full_name']}/commits"
            f"?author={login}&since={start.isoformat()}T00:00:00Z&per_page=30",
        )
        for commit in commits:
            stamp = commit["commit"]["author"]["date"]
            days[datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()] += 1
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


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: profile_stats.py <username> <output_dir>")
    login, out_dir = sys.argv[1], sys.argv[2]
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is not set")

    start, today, calendar = fetch_calendar(token, login)
    private = fetch_private_commits(token, login, start)
    days = merge(calendar, {d: c for d, c in private.items() if start <= d <= today})
    stats = compute_stats(start, today, days)
    print(f"private commits found: {sum(private.values())}")

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "streak-stats.svg"), "w") as f:
        f.write(render_svg(stats, today))
    with open(os.path.join(out_dir, "activity-graph.svg"), "w") as f:
        title = os.environ.get("ACTIVITY_TITLE", f"{login}'s Contribution Activity")
        f.write(render_activity_svg(days, today, title))
    print(f"total={stats['total']} current={stats['current']} longest={stats['longest']}")


if __name__ == "__main__":
    main()

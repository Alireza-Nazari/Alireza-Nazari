"""Generate the profile stat cards (SVG) from the GitHub GraphQL API. Standard library only.

Cards (each in a light and a dark variant, written to stats/):
  overview   total contributions (all time and this year), commits, pull requests, issues, reviews, stars
  streak     current and longest streak of days with contributions
  languages  top languages by code size across your own (non-fork) repositories

Privacy: only aggregate numbers and language names are written; repository names never appear.
Token: STATS_TOKEN (a personal access token of the profile owner) sees private contributions and private
repositories' languages; without it, GITHUB_TOKEN is used and the cards show public data plus whatever
private contribution counts the profile already shows publicly.

    GH_TOKEN=... python scripts/generate_stats.py --login Alireza-Nazari
    python scripts/generate_stats.py --mock tests/mock.json      # render from saved data (no network)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://api.github.com/graphql"
OUT = Path(__file__).resolve().parents[1] / "stats"

THEMES = {
    "light": {"bg": "#ffffff", "border": "#e4e2e2", "title": "#2f80ed", "text": "#434d58", "muted": "#6b7280",
              "accent": "#2f80ed", "fire": "#fb8c00", "track": "#eaecef"},
    "dark": {"bg": "#0d1117", "border": "#30363d", "title": "#58a6ff", "text": "#c9d1d9", "muted": "#8b949e",
             "accent": "#58a6ff", "fire": "#ffa657", "track": "#21262d"},
}

Q_CONTRIB = """query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){contributionsCollection(from:$from,to:$to){
totalCommitContributions restrictedContributionsCount totalPullRequestContributions totalIssueContributions
totalPullRequestReviewContributions contributionCalendar{totalContributions weeks{contributionDays{date contributionCount}}}}}}"""
Q_REPOS = """query($login:String!,$after:String){user(login:$login){name login createdAt followers{totalCount}
repositories(ownerAffiliations:OWNER,isFork:false,first:100,after:$after){pageInfo{hasNextPage endCursor}
nodes{stargazerCount languages(first:10,orderBy:{field:SIZE,direction:DESC}){edges{size node{name color}}}}}}}"""


# ----------------------------------------------------------------------------------------------- data
def gql(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(),
                                 {"Authorization": f"bearer {token}", "Content-Type": "application/json",
                                  "User-Agent": "profile-stats-generator"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise RuntimeError(f"GitHub API error: {out['errors'][0].get('message')}")
    return out["data"]


def fetch(token: str, login: str) -> dict:
    """Everything the cards need: per-year contribution totals and calendar days, repo stars and languages."""
    repos, after, user = [], None, None
    while True:
        u = gql(token, Q_REPOS, {"login": login, "after": after})["user"]
        user = user or u
        repos += u["repositories"]["nodes"]
        if not u["repositories"]["pageInfo"]["hasNextPage"]:
            break
        after = u["repositories"]["pageInfo"]["endCursor"]
    created = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    years = []
    start = created
    while start < now:                                    # the API allows at most one year per query
        end = min(start.replace(year=start.year + 1), now)
        c = gql(token, Q_CONTRIB, {"login": login, "from": start.isoformat(), "to": end.isoformat()})
        years.append(c["user"]["contributionsCollection"])
        start = end
    days = {}
    for y in years:
        for w in y["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
    langs = {}
    for r in repos:
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            langs.setdefault(n, {"size": 0, "color": e["node"]["color"] or "#8b949e"})["size"] += e["size"]
    # rolling last 365 days (the same window as the profile's contribution graph)
    ly = gql(token, Q_CONTRIB, {"login": login, "from": (now - dt.timedelta(days=365)).isoformat(),
                                "to": now.isoformat()})["user"]["contributionsCollection"]
    return {"login": user["login"], "name": user["name"] or user["login"], "created": user["createdAt"][:10],
            "today": now.date().isoformat(),
            "total_contributions": sum(days.values()),            # de-duplicated calendar days
            "last_year": {k: ly[k] for k in ("totalCommitContributions", "restrictedContributionsCount",
                                             "totalPullRequestContributions", "totalIssueContributions",
                                             "totalPullRequestReviewContributions")},
            "last_year_total": ly["contributionCalendar"]["totalContributions"],
            "stars": sum(r["stargazerCount"] for r in repos), "followers": user["followers"]["totalCount"],
            "days": days, "languages": langs}


# ----------------------------------------------------------------------------------------------- metrics
def streaks(days: dict, today: str) -> dict:
    """Current streak counts back from today (or yesterday, if today has no contribution yet); longest = max run."""
    if not days:
        return {"current": 0, "current_range": None, "longest": 0, "longest_range": None}
    d0 = dt.date.fromisoformat(min(days))
    d1 = min(dt.date.fromisoformat(max(days)), dt.date.fromisoformat(today))
    longest, best, run, run_start = 0, None, 0, None
    d = d0
    while d <= d1:
        if days.get(d.isoformat(), 0) > 0:
            run_start = d if run == 0 else run_start
            run += 1
            if run > longest:
                longest, best = run, (run_start, d)
        else:
            run = 0
        d += dt.timedelta(days=1)
    end = d1 if days.get(d1.isoformat(), 0) > 0 else d1 - dt.timedelta(days=1)
    cur, d = 0, end
    while days.get(d.isoformat(), 0) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    return {"current": cur, "current_range": (d + dt.timedelta(days=1), end) if cur else None,
            "longest": longest, "longest_range": best}


def _fmt_range(r) -> str:
    if not r:
        return "—"
    a, b = r
    f = lambda x: x.strftime("%b %-d") + (f", {x.year}" if x.year != dt.date.today().year else "")  # noqa: E731
    return f(a) if a == b else f"{f(a)} – {f(b)}"


# ----------------------------------------------------------------------------------------------- SVG
def _frame(w: int, h: int, t: dict, title: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{html.escape(title)}"><title>{html.escape(title)}</title>'
            f'<style>.t{{font:600 18px "Segoe UI",Ubuntu,Helvetica,Arial,sans-serif;fill:{t["title"]}}}'
            f'.l{{font:400 14px "Segoe UI",Ubuntu,Helvetica,Arial,sans-serif;fill:{t["text"]}}}'
            f'.v{{font:700 14px "Segoe UI",Ubuntu,Helvetica,Arial,sans-serif;fill:{t["text"]}}}'
            f'.m{{font:400 12px "Segoe UI",Ubuntu,Helvetica,Arial,sans-serif;fill:{t["muted"]}}}'
            f'.b{{font:700 28px "Segoe UI",Ubuntu,Helvetica,Arial,sans-serif;fill:{t["text"]}}}</style>'
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="6" fill="{t["bg"]}" stroke="{t["border"]}"/>'
            f'{body}</svg>')


def card_overview(data: dict, t: dict) -> str:
    ly = data["last_year"]
    rows = [("Total contributions (all time)", f"{data['total_contributions']:,}"),
            ("Contributions in the last year", f"{data['last_year_total']:,}"),
            ("Commits, last year (public + private)", f"{ly.get('totalCommitContributions', 0) + ly.get('restrictedContributionsCount', 0):,}"),
            ("Pull requests / reviews, last year", f"{ly.get('totalPullRequestContributions', 0)} / {ly.get('totalPullRequestReviewContributions', 0)}"),
            ("Issues, last year", f"{ly.get('totalIssueContributions', 0)}"),
            ("Stars earned", f"{data['stars']:,}")]
    body = f'<text x="25" y="35" class="t">{html.escape(data["name"])}\'s GitHub Stats</text>'
    for i, (k, v) in enumerate(rows):
        y = 70 + i * 24
        body += f'<text x="25" y="{y}" class="l">{html.escape(k)}:</text><text x="425" y="{y}" class="v" text-anchor="end">{v}</text>'
    body += f'<text x="425" y="{70 + len(rows) * 24 - 2}" class="m" text-anchor="end">updated {data["today"]}</text>'
    return _frame(450, 70 + len(rows) * 24 + 8, t, f"{data['name']}'s GitHub stats", body)


def card_streak(data: dict, t: dict) -> str:
    s = streaks(data["days"], data["today"])
    cols = [(82, f"{data['total_contributions']:,}", "Total Contributions", f"{_fmt_short(data['created'])} – Present"),
            (247, str(s["current"]), "Current Streak", _fmt_range(s["current_range"])),
            (412, str(s["longest"]), "Longest Streak", _fmt_range(s["longest_range"]))]
    body = ""
    for x, big, lab, sub in cols:
        colour = t["fire"] if lab == "Current Streak" else t["text"]
        body += (f'<text x="{x}" y="78" class="b" text-anchor="middle" style="fill:{colour}">{big}</text>'
                 f'<text x="{x}" y="112" class="v" text-anchor="middle">{lab}</text>'
                 f'<text x="{x}" y="136" class="m" text-anchor="middle">{html.escape(sub)}</text>')
    body += (f'<line x1="165" y1="40" x2="165" y2="150" stroke="{t["border"]}"/>'
             f'<line x1="330" y1="40" x2="330" y2="150" stroke="{t["border"]}"/>'
             f'<circle cx="247" cy="68" r="34" fill="none" stroke="{t["fire"]}" stroke-width="4" opacity="0.35"/>')
    return _frame(495, 175, t, "Contribution streak", body)


def _fmt_short(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return d.strftime("%b %-d, %Y")


def card_languages(data: dict, t: dict, top: int = 8) -> str:
    langs = sorted(data["languages"].items(), key=lambda kv: -kv[1]["size"])[:top]
    total = sum(v["size"] for _, v in langs) or 1
    body = '<text x="25" y="35" class="t">Most Used Languages</text>'
    x = 25
    body += f'<rect x="25" y="52" width="300" height="8" rx="4" fill="{t["track"]}"/>'
    for name, v in langs:
        w = 300 * v["size"] / total
        body += f'<rect x="{x:.2f}" y="52" width="{w:.2f}" height="8" fill="{v["color"]}"/>'
        x += w
    for i, (name, v) in enumerate(langs):
        cx, cy = 25 + (i % 2) * 165, 84 + (i // 2) * 22
        body += (f'<circle cx="{cx + 5}" cy="{cy - 4}" r="5" fill="{v["color"]}"/>'
                 f'<text x="{cx + 16}" y="{cy}" class="m">{html.escape(name)} {100 * v["size"] / total:.1f}%</text>')
    h = 84 + ((len(langs) + 1) // 2) * 22
    return _frame(350, max(h, 120), t, "Most used languages", body)


def render(data: dict, out: Path = OUT) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for theme, t in THEMES.items():
        for name, fn in (("overview", card_overview), ("streak", card_streak), ("languages", card_languages)):
            p = out / f"{name}-{theme}.svg"
            p.write_text(fn(data, t), encoding="utf-8")
            written.append(p)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    ap.add_argument("--mock", help="render from a saved data JSON instead of the API")
    ap.add_argument("--save", help="also save the fetched data JSON here (debugging)")
    a = ap.parse_args()
    if a.mock:
        data = json.loads(Path(a.mock).read_text())
    else:
        token = os.environ.get("GH_TOKEN", "")
        if not token or not a.login:
            print("GH_TOKEN and --login are required", file=sys.stderr)
            return 2
        data = fetch(token, a.login)
        if a.save:
            Path(a.save).write_text(json.dumps(data))
    for p in render(data):
        print("wrote", p.relative_to(p.parents[1]))
    s = streaks(data["days"], data["today"])
    print(f"total {data['total_contributions']}, current streak {s['current']}, longest {s['longest']}, "
          f"languages {len(data['languages'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

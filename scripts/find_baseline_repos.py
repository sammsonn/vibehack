"""Finds public repos that look like a small team that actually reviews PRs, as candidates for
`python -m radar baseline`. Run from vibehack/: python -m scripts.find_baseline_repos [--per-language 30]"""
import argparse
import datetime as dt
import sys
import time

from github import Auth, Github, GithubException

from radar import config
from radar.services.baseline import is_bot, is_team_pr

LANGUAGES = ["TypeScript", "Python", "Go", "Ruby", "Kotlin"]
SEARCH_PAUSE = 2.2  # search API allows 30 requests/minute
SAMPLE = 12


def team_prs(repo, since: dt.datetime) -> list:
    """Team PRs opened since `since`, from one page of the PR list."""
    result = []
    for p in repo.get_pulls(state="all", sort="created", direction="desc").get_page(0):
        if p.created_at < since:
            break
        if is_team_pr(p):
            result.append(p)
    return result


def reviewed_share(prs: list) -> tuple[int, float]:
    """(sampled merged team PRs, share reviewed by someone other than the author). One request per sampled PR."""
    merged = [p for p in prs if p.merged_at][:SAMPLE]
    reviewed = sum(
        any(r.user and not is_bot(r.user) and r.user.login.lower() != p.user.login.lower() and r.state != "PENDING"
            for r in p.get_reviews())
        for p in merged
    )
    return len(merged), reviewed / len(merged) if merged else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-language", type=int, default=30, help="repos to inspect per language")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--min-reviewed", type=float, default=0.6)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    s = config.load()
    gh = Github(auth=Auth.Token(s.github_token), per_page=100) if s.github_token else Github(per_page=100)
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=args.days)
    pushed = (now - dt.timedelta(days=7)).date().isoformat()
    found = 0
    for lang in LANGUAGES:
        query = f"language:{lang} pushed:>={pushed} stars:50..3000 fork:false archived:false"
        repos = gh.search_repositories(query, sort="updated")
        time.sleep(SEARCH_PAUSE)
        for repo in repos[: args.per_language]:
            if repo.owner.type != "Organization":
                continue
            try:
                prs = team_prs(repo, since)
                authors = len({p.user.login.lower() for p in prs})
                if len(prs) < 25 or not 3 <= authors <= 8:
                    continue
                sampled, share = reviewed_share(prs)
            except GithubException as e:
                print(f"{'skip (error ' + str(e.status) + ')':26} {repo.full_name}", flush=True)
                continue
            verdict = "CANDIDATE" if share >= args.min_reviewed else "skip (low review culture)"
            found += share >= args.min_reviewed
            print(f"{verdict:26} {repo.full_name:45} team PRs={len(prs):3} authors={authors} "
                  f"reviewed={share:.0%} of {sampled} sampled", flush=True)
    print(f"\n{found} candidates. Next: python -m radar baseline <owner/repo>")


if __name__ == "__main__":
    main()

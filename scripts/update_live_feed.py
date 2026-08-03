#!/usr/bin/env python3
"""Refresh the GitHub-native live delivery feed in the profile README."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
START_MARKER = "<!-- LIVE-DELIVERY:START -->"
END_MARKER = "<!-- LIVE-DELIVERY:END -->"
ICT = timezone(timedelta(hours=7))


def api_get(path: str, token: str = "") -> Any:
    """Read public GitHub data, retrying anonymously if a token is rejected."""

    url = path if path.startswith("https://") else f"{API_ROOT}{path}"

    def request(auth_token: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "huynhhuan-live-delivery-feed",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            return json.load(response)

    try:
        return request(token)
    except HTTPError as error:
        if token and error.code in {401, 403, 404}:
            return request("")
        raise


def format_ict(value: str) -> str:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp.astimezone(ICT).strftime("%d %b %Y · %H:%M ICT")


def markdown_text(value: str, limit: int = 86) -> str:
    compact = " ".join(value.split())
    compact = re.sub(r"https?://\S+", "remote", compact)
    if len(compact) > limit:
        compact = f"{compact[: limit - 1].rstrip()}…"
    return compact.replace("|", "\\|")


def pipeline_signal(pipeline_repository: str, token: str) -> str | None:
    data = api_get(
        f"/repos/{pipeline_repository}/actions/runs?per_page=20", token
    )
    runs = data.get("workflow_runs", [])
    run = next(
        (item for item in runs if item.get("conclusion") != "skipped"),
        runs[0] if runs else None,
    )
    if not run:
        return None

    conclusion = run.get("conclusion")
    status = run.get("status", "unknown")
    state = conclusion or status
    icon = {
        "success": "🟢",
        "failure": "🔴",
        "timed_out": "🔴",
        "action_required": "🟠",
        "in_progress": "🟠",
        "queued": "🟡",
        "cancelled": "⚪",
        "neutral": "⚪",
    }.get(state, "⚪")
    label = {
        "success": "passing",
        "failure": "failed",
        "timed_out": "timed out",
        "action_required": "action required",
        "in_progress": "running",
        "queued": "queued",
        "cancelled": "cancelled",
        "neutral": "neutral",
    }.get(state, state.replace("_", " "))
    name = markdown_text(run.get("name") or "GitHub Actions")
    repository = pipeline_repository.split("/", 1)[-1]
    when = format_ict(run["updated_at"])
    return (
        f"{icon} **{label}** · [{name}]({run['html_url']}) · "
        f"`{repository}` · {when}"
    )


def latest_push_signal(username: str, token: str) -> str | None:
    events = api_get(f"/users/{quote(username)}/events/public?per_page=100", token)
    profile_repository = f"{username}/{username}".lower()
    event = next(
        (
            item
            for item in events
            if item.get("type") == "PushEvent"
            and item.get("repo", {}).get("name", "").lower()
            != profile_repository
        ),
        None,
    )
    if not event:
        return None

    repository = event["repo"]["name"]
    commits = event.get("payload", {}).get("commits", [])
    head = event.get("payload", {}).get("head", "")
    commit = commits[-1] if commits else {}
    sha = commit.get("sha") or head
    message = commit.get("message", "")
    if not message and sha:
        try:
            details = api_get(f"/repos/{repository}/commits/{sha}", token)
            message = details.get("commit", {}).get("message", "")
        except (HTTPError, URLError, KeyError, TypeError, ValueError):
            pass
    message = markdown_text(message or "Pushed new commits")
    short_sha = sha[:7] if sha else "commit"
    repo_url = f"https://github.com/{repository}"
    commit_url = f"{repo_url}/commit/{sha}" if sha else repo_url
    when = format_ict(event["created_at"])
    return (
        f"[`{repository}`]({repo_url}) · "
        f"[`{short_sha}`]({commit_url}) · {message} · {when}"
    )


def active_repository_signal(username: str, token: str) -> str | None:
    repositories = api_get(
        f"/users/{quote(username)}/repos?type=owner&sort=pushed&direction=desc&per_page=100",
        token,
    )
    repository = next(
        (
            item
            for item in repositories
            if not item.get("fork")
            and not item.get("archived")
            and item.get("name", "").lower() != username.lower()
        ),
        None,
    )
    if not repository:
        return None

    language = repository.get("language") or "multi-language"
    when = format_ict(repository["pushed_at"])
    return (
        f"[`{repository['full_name']}`]({repository['html_url']}) · "
        f"{markdown_text(language)} · last pushed {when}"
    )


def render_feed(username: str, pipeline_repository: str, token: str) -> str:
    signals: list[tuple[str, str]] = []
    errors: list[str] = []
    collectors = (
        ("`pipeline`", lambda: pipeline_signal(pipeline_repository, token)),
        ("`latest push`", lambda: latest_push_signal(username, token)),
        ("`owned repo`", lambda: active_repository_signal(username, token)),
    )

    for label, collect in collectors:
        try:
            value = collect()
            if value:
                signals.append((label, value))
        except (HTTPError, URLError, KeyError, TypeError, ValueError) as error:
            errors.append(f"{label}: {error}")

    if not signals:
        raise RuntimeError("GitHub API returned no live signals: " + "; ".join(errors))

    rows = "\n".join(f"| {label} | {value} |" for label, value in signals)
    return (
        f"{START_MARKER}\n"
        "#### ⚡ Live Delivery Feed\n\n"
        "<sub>GitHub-native signals · refreshed automatically when engineering activity changes.</sub>\n\n"
        "| Signal | Live state |\n"
        "|:--|:--|\n"
        f"{rows}\n"
        f"{END_MARKER}"
    )


def replace_feed(readme: str, feed: str) -> str:
    if START_MARKER not in readme or END_MARKER not in readme:
        raise ValueError("README is missing live delivery feed markers")
    before, remainder = readme.split(START_MARKER, 1)
    _, after = remainder.split(END_MARKER, 1)
    return f"{before}{feed}{after}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    username = os.environ.get("GITHUB_USERNAME", "huynhhuan")
    pipeline_repository = os.environ.get(
        "PIPELINE_REPOSITORY", "huynhhuan/bahuan-aws-accelerator-p2"
    )
    token = os.environ.get("GITHUB_TOKEN", "")
    feed = render_feed(username, pipeline_repository, token)

    if args.dry_run:
        print(feed)
        return 0

    readme_path = Path(args.readme)
    original = readme_path.read_text(encoding="utf-8")
    updated = replace_feed(original, feed)

    if args.check:
        if updated != original:
            print("Live delivery feed is out of date", file=sys.stderr)
            return 1
        return 0

    if updated != original:
        readme_path.write_text(updated, encoding="utf-8")
        print("Updated live delivery feed")
    else:
        print("Live delivery feed already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

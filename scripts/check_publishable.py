#!/usr/bin/env python3
"""Refuse to commit secrets or personal health data.

.gitignore is a convenience, not a control: `git add -f` overrides it, a new
clone may not have it, and a file moved into a tracked directory slips through.
This scans what is actually STAGED, which is the only thing that matters.

Usage:
    python3 scripts/check_publishable.py            # staged files (pre-commit)
    python3 scripts/check_publishable.py --all      # every tracked file (audit)

Exit 1 blocks the commit.
"""
from __future__ import annotations

import re
import subprocess
import sys

# ── Filenames that must never be tracked, whatever their contents ──────
BLOCKED_PATHS = [
    (re.compile(r"(^|/)\.env($|\.)(?!example)"), "environment file with secrets"),
    (re.compile(r"(^|/)campaign\.toml$"), "real body-composition targets"),
    (re.compile(r"(^|/)data/.*\.(csv|json)$"), "personal health data"),
    (re.compile(r"(^|/)Takeout/"), "raw health export"),
    (re.compile(r"(^|/)my-coach/"), "coaching content (personal profile)"),
    (re.compile(r"(^|/)build/"), "intake notes (personal profile)"),
    (re.compile(r"\.zip$"), "archive — may contain health data"),
]

# ── Content patterns. Ordered most-certain first. ──────────────────────
BLOCKED_CONTENT = [
    (re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"), "Google OAuth access token"),
    (re.compile(r"1//[A-Za-z0-9_\-]{30,}"), "Google OAuth refresh token"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"), "Google OAuth client secret"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"users/\d{15,}/dataTypes"), "Google Health user id"),
    (re.compile(r"\bnacho\.montero99@gmail\.com\b"), "personal email address"),
    (re.compile(r"HEVY_API_KEY[ \t]*=[ \t]*[0-9a-f]{8}-[0-9a-f]{4}"), "Hevy API key"),
    # [ \t] not \s — \s matches newlines, so "SECRET=" with an empty value
    # would match the NEXT line's key name and flag every .env.example.
    (re.compile(r"(CLIENT_SECRET|REFRESH_TOKEN|API_KEY)[ \t]*=[ \t]*\S{12,}"),
     "credential assigned a real value"),
]

# Files allowed to mention otherwise-blocked content.
#
# PRIVACY.md carries a support email BY DESIGN — a privacy policy needs a
# contact address, and the author accepted that exposure knowingly on
# 2026-08-30. Everywhere else, that address is still blocked: a stray email in
# source or in a data file is an accident, not a decision.
ALLOWLIST = {
    "scripts/check_publishable.py",   # this file describes the patterns
    "PUBLISHING.md",                  # documents them too
    "PRIVACY.md",                     # deliberate: support contact
}


def staged_files() -> list:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True,
    )
    return [f for f in out.stdout.splitlines() if f]


def all_tracked_files() -> list:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def staged_content(path: str) -> str:
    out = subprocess.run(["git", "show", ":" + path],
                         capture_output=True, text=True, errors="replace")
    return out.stdout


def main() -> int:
    check_all = "--all" in sys.argv
    files = all_tracked_files() if check_all else staged_files()
    if not files:
        print("Nothing to check.")
        return 0

    problems = []

    for path in files:
        for pattern, reason in BLOCKED_PATHS:
            if pattern.search(path):
                problems.append((path, "PATH", reason, ""))

        if path in ALLOWLIST:
            continue
        try:
            content = staged_content(path) if not check_all else open(
                path, errors="replace").read()
        except (OSError, UnicodeDecodeError):
            continue

        for pattern, reason in BLOCKED_CONTENT:
            match = pattern.search(content)
            if match:
                line = content[:match.start()].count("\n") + 1
                problems.append((path, "LINE %d" % line, reason,
                                 match.group(0)[:18] + "..."))

    if not problems:
        print("✓ {} file(s) checked — nothing sensitive found.".format(len(files)))
        return 0

    print("\n" + "=" * 68)
    print("BLOCKED — sensitive content detected")
    print("=" * 68)
    for path, where, reason, sample in problems:
        print("\n  {}  [{}]".format(path, where))
        print("    {}".format(reason))
        if sample:
            print("    matched: {}".format(sample))
    print("\n" + "-" * 68)
    print("Nothing has been committed. To resolve:")
    print("  git rm --cached <file>          unstage (works pre-first-commit)")
    print("  git restore --staged <file>     unstage (after first commit)")
    print("  then confirm .gitignore covers it")
    print("If this is a false positive, add the path to ALLOWLIST in")
    print("scripts/check_publishable.py — deliberately, not reflexively.")
    print("-" * 68)
    return 1


if __name__ == "__main__":
    sys.exit(main())

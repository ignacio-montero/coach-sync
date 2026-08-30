#!/usr/bin/env python3
"""Refuse to commit secrets or personal health data.

.gitignore is a convenience, not a control: `git add -f` overrides it, a new
clone may not have it, and a file moved into a tracked directory slips through.
This scans what is actually STAGED, which is the only thing that matters.

FAILS CLOSED. Any error — a git command that returns non-zero, a blob that
cannot be read — blocks the commit. A guardrail whose error path is "allow" is
worse than no guardrail, because it is trusted and its silence reads as
approval.

Usage:
    python3 scripts/check_publishable.py            # staged files (pre-commit)
    python3 scripts/check_publishable.py --all      # every tracked file (audit)

Exit 1 blocks the commit. Exit 2 means the check itself failed — also blocking.
"""
from __future__ import annotations

import re
import subprocess
import sys

# ── Filenames that must never be tracked, whatever their contents ──────
BLOCKED_PATHS = [
    (re.compile(rb"(^|/)\.env($|\.)(?!example)"), "environment file with secrets"),
    (re.compile(rb"(^|/)campaign\.toml$"), "real body-composition targets"),
    # Everything under data/ except the tracked placeholder. Broader than
    # "*.csv|*.json" on purpose: a .txt or .xlsx export dropped in there is
    # health data too.
    (re.compile(rb"(^|/)data/(?!\.gitkeep$)."), "personal health data directory"),
    (re.compile(rb"(^|/)Takeout/"), "raw health export"),
    (re.compile(rb"(^|/)my-coach/"), "coaching content (personal profile)"),
    (re.compile(rb"(^|/)build/"), "intake notes (personal profile)"),
    (re.compile(rb"\.zip$"), "archive — may contain health data"),
]

# ── Content patterns, matched as BYTES ────────────────────────────────
# Bytes not str: a UTF-16 file decodes to mojibake under errors="replace" and
# every text regex silently misses. Matching bytes, plus a NUL-stripped pass,
# catches UTF-16 without needing to guess the encoding.
BLOCKED_CONTENT = [
    (re.compile(rb"ya29\.[A-Za-z0-9_\-]{20,}"), "Google OAuth access token"),
    (re.compile(rb"1//[A-Za-z0-9_\-]{30,}"), "Google OAuth refresh token"),
    (re.compile(rb"GOCSPX-[A-Za-z0-9_\-]{10,}"), "Google OAuth client secret"),
    (re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(rb"users/\d{15,}/dataTypes"), "Google Health user id"),
    (re.compile(rb"\bnacho\.montero99@gmail\.com\b"), "personal email address"),
    (re.compile(rb"HEVY_API_KEY[ \t]*=[ \t]*[0-9a-f]{8}-[0-9a-f]{4}"), "Hevy API key"),
    # [ \t] not \s — \s matches newlines, so "SECRET=" with an empty value
    # would match the NEXT line's key name and flag every .env.example.
    (re.compile(rb"(CLIENT_SECRET|REFRESH_TOKEN|API_KEY)[ \t]*=[ \t]*\S{12,}"),
     "credential assigned a real value"),
]

# Health data recognised by its SHAPE, not its location — a metrics CSV copied
# to the repo root to attach to an issue used to sail straight through, because
# only paths under data/ were checked.
#
# Scoped by file type rather than by allowlist: source code that DECLARES these
# column names is not health data, and exempting every module that mentions one
# would hollow out the rule. A .csv/.json/.txt containing them almost certainly
# is data.
HEALTH_COLUMNS = re.compile(
    rb"\b(weight_kg|body_fat_pct|lean_kg|hrv_rmssd|waist_navel_cm|"
    rb"weight_7d_mean|lean_floor_breach|est_1rm_epley)\b"
)
CODE_SUFFIXES = (b".py", b".toml", b".md", b".cfg", b".ini")

# Files allowed to mention otherwise-blocked content.
#
# PRIVACY.md carries a support email BY DESIGN — a privacy policy needs a
# contact address, and the author accepted that exposure knowingly on
# 2026-08-30. Everywhere else that address stays blocked: a stray email in
# source or a data file is an accident, not a decision.
#
# The code files are exempt only because they DEFINE these patterns.
ALLOWLIST = {
    "scripts/check_publishable.py",
    "PUBLISHING.md",
    "PRIVACY.md",
}


def _git(args: list, binary: bool = False):
    """Run git, FAILING CLOSED on any non-zero return code."""
    out = subprocess.run(["git"] + args, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(
            "git {} failed (exit {}): {}".format(
                " ".join(args[:2]), out.returncode,
                out.stderr.decode("utf-8", "replace").strip()[:200])
        )
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def staged_paths() -> list:
    """NUL-delimited (-z) so git never quotes or escapes non-ASCII names.

    Without -z, git renders "data/wéight.csv" as a quoted, backslash-escaped
    string; the trailing quote broke the path regexes and the escaped name then
    failed to resolve as a blob, yielding empty content and a clean pass.
    """
    raw = _git(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
               binary=True)
    return [p for p in raw.split(b"\x00") if p]


def tracked_paths() -> list:
    raw = _git(["ls-files", "-z"], binary=True)
    return [p for p in raw.split(b"\x00") if p]


def blob(path: bytes, staged: bool) -> bytes:
    if staged:
        return _git(["show", b":" + path if isinstance(path, bytes)
                     else ":" + path], binary=True)
    with open(path, "rb") as handle:
        return handle.read()


def main() -> int:
    check_all = "--all" in sys.argv
    try:
        paths = tracked_paths() if check_all else staged_paths()
    except RuntimeError as exc:
        print("BLOCKED — the check could not run: {}".format(exc))
        return 2

    if not paths:
        print("Nothing to check.")
        return 0

    problems = []
    for raw_path in paths:
        text_path = raw_path.decode("utf-8", "replace")

        for pattern, reason in BLOCKED_PATHS:
            if pattern.search(raw_path):
                problems.append((text_path, "PATH", reason, ""))

        if text_path in ALLOWLIST:
            continue

        try:
            content = blob(raw_path, staged=not check_all)
        except (RuntimeError, OSError) as exc:
            problems.append((text_path, "UNREADABLE",
                             "could not read to scan it: {}".format(exc), ""))
            continue

        rules = list(BLOCKED_CONTENT)
        if not raw_path.endswith(CODE_SUFFIXES):
            rules.append((HEALTH_COLUMNS, "health-data column header"))

        # UTF-16 interleaves NULs; strip them so byte patterns still match.
        for candidate in (content, content.replace(b"\x00", b"")):
            for pattern, reason in rules:
                for match in pattern.finditer(candidate):
                    line = candidate[:match.start()].count(b"\n") + 1
                    sample = match.group(0)[:18].decode("utf-8", "replace")
                    entry = (text_path, "LINE %d" % line, reason, sample + "...")
                    if entry not in problems:
                        problems.append(entry)
            if b"\x00" not in content:
                break

    if not problems:
        print("✓ {} file(s) checked — nothing sensitive found.".format(len(paths)))
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
    print("If this is a false positive, add the path to ALLOWLIST —")
    print("deliberately, not reflexively.")
    print("-" * 68)
    return 1


if __name__ == "__main__":
    sys.exit(main())

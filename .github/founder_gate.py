#!/usr/bin/env python3
"""Founder gate (founder, 9 Oct 2026: tooling merges without the founder; releases ship only
what the founder approved). The tooling list is .github/tooling-paths; the founder is the
owner of `*` in .github/CODEOWNERS. Both are read from this checkout, which is main's.

  founder_gate.py pr <owner/repo> <number>
      The founder-gate check on a pull request. Passes when every changed file is on the
      tooling list and no commit carries a clearing line, or a qualifying fix has both
      reviewers' current PASS verdicts while .github/fix-route exists, or the founder's latest review
      approves the head commit (and, for a clearing line, the title carries it and was not
      changed after that review). It reads the
      pull request through the API and never runs its code. GITHUB_EVENT_PATH binds files
      and commits to the checked event head/base; a moved PR head fails closed. To pick up an approval given
      after the last push, rerun the check: gh run rerun <run id> --repo <owner/repo>.

  founder_gate.py release <owner/repo> <commit> <release workflow file>
      Run by the release before it deploys. Every change that reached main since the release
      workflow last succeeded and touches a file off the tooling list must have come from a
      pull request the founder approved at its head commit, or a qualifying fix while
      .github/fix-route exists in the released commit, or be cleared by a later one with
      "Clears founder-gate refusal: <commit>" in one of its commit messages and its title
      (the pr check fails until the title shows it, so the founder sees it before approving; a
      rename after the founder's approval voids the clear). Changes before this script
      existed were all founder-approved, so the walk stops there too.

Every changed file is printed with the rule that passed or stopped it. Exit 0 = pass.
"""
import json, os, re, subprocess, sys, urllib.request
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIERS = {"A": "tooling, never runs in a release", "B": "release machinery"}


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def api(path, every_page=True):
    """Every page of a GitHub API list (or only the first), or one object."""
    url, out = f"https://api.github.com/{path}", None
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
            link = r.headers.get("Link") or ""
        if not isinstance(body, list):
            return body
        out = (out or []) + body
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt and every_page else None
    return out


def founder():
    for line in Path(ROOT, ".github/CODEOWNERS").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "*" and parts[1].startswith("@"):
            return parts[1][1:]
    fail("no `* @owner` line in .github/CODEOWNERS")


def rules():
    out = []
    for n, line in enumerate(Path(ROOT, ".github/tooling-paths").read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tier, _, pattern = line.partition(" ")
        if tier not in ("A", "B", "!") or not pattern.strip():
            fail(f".github/tooling-paths line {n} is not `A|B|! <pattern>`: {line}")
        rx = "".join(".*" if t == "**" else "[^/]*" if t == "*" else "[^/]" if t == "?" else re.escape(t)
                     for t in re.split(r"(\*\*|\*|\?)", pattern.strip()) if t)
        out.append((tier, pattern.strip(), re.compile(rx + r"\Z")))
    return out


def classify(path, table):
    """('A'|'B'|None, why). The last matching line decides; None means the founder approves."""
    hit = None
    for tier, pattern, rx in table:
        if rx.match(path):
            hit = (tier, pattern)
    if not hit:
        return None, "product: not on the tooling list"
    if hit[0] == "!":
        return None, f"product: kept off the list by `! {hit[1]}`"
    return hit[0], f"{TIERS[hit[0]]} (`{hit[0]} {hit[1]}`)"


def approved(repo, number, head, who):
    """(yes or no, why, when the approving review was submitted)."""
    reviews = [r for r in api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
               if (r.get("user") or {}).get("login") == who and r["state"] != "COMMENTED"]
    if not reviews:
        return False, f"{who} has not reviewed it", None
    last = reviews[-1]
    if last["state"] != "APPROVED":
        return False, f"{who}'s latest review is {last['state']}", None
    if last["commit_id"] != head:
        return False, f"{who} approved {last['commit_id'][:7]}, not the head {head[:7]}", None
    return True, f"{who} approved the head {head[:7]}", last["submitted_at"]


def judge(paths, table):
    """Print each file and its rule; return (product files, release-machinery files)."""
    product, machinery = [], []
    for p in sorted(paths):
        tier, why = classify(p, table)
        print(f"  {tier or '-'}  {p}  {why}")
        (product if tier is None else machinery if tier == "B" else []).append(p)
    return product, machinery


CLEARS = re.compile(r"^Clears founder-gate refusal: ([0-9a-f]{40})\s*$", re.M)


def clearing(repo, number, title, approved_at, commits=None):
    """The refused commits this pull request clears, and why any named one does not count.
    The line must be in one of its commits (fixed by the founder's approval) and in its title
    (which the founder sees before approving). A title can be edited after the approval without
    dismissing it, so a rename after the approving review voids every clear in it."""
    named = []
    for pc in (commits if commits is not None else api(f"repos/{repo}/pulls/{number}/commits?per_page=100")):
        named += [c for c in CLEARS.findall(pc["commit"]["message"]) if c not in named]
    if not named:
        return [], [], ""
    if approved_at is None:
        return named, named, "the founder has not approved this pull request at its head"
    renamed = [e for e in api(f"repos/{repo}/issues/{number}/events?per_page=100")
               if e["event"] == "renamed" and e["created_at"] >= approved_at]
    if renamed:
        return named, named, "its title was changed after the founder approved it; the founder approves again"
    untitled = [c for c in named if f"Clears founder-gate refusal: {c[:12]}" not in title]
    return named, untitled, "its title must say so, for the founder to see before approving" if untitled else ""


FIX_ACK = "Fix: changes no agreed behaviour and no screen."
VERDICT = re.compile(r"^Review round ([0-9]+) · (claude|codex) · (PASS|CHANGES) · ([0-9a-f]{40})$")



def compare(repo, base, head):
    """An immutable, complete GitHub comparison; incomplete responses fail closed."""
    comparison = api(f"repos/{repo}/compare/{base}...{head}")
    if not isinstance(comparison.get("files"), list) or not isinstance(comparison.get("commits"), list):
        fail("comparison is missing its files or commits")
    if len(comparison["files"]) >= 300 or comparison.get("total_commits") != len(comparison["commits"]):
        fail("comparison is incomplete (300 files or missing commits)")
    return comparison


def fixed_comparison(repo, number, info):
    """Read the checked head from the Actions event; files and commits are immutable."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        fail("GITHUB_EVENT_PATH is required to bind the check to its event head")
    with open(event_path) as source:
        event = json.load(source)["pull_request"]
    head, base = event["head"]["sha"], event["base"]["sha"]
    if str(event["number"]) != str(number):
        fail("event pull request number does not match")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, base)):
        fail("event head and base must be full 40-hex commits")
    if info["head"]["sha"] != head:
        fail("pull request head moved since the checked event")
    # Three-dot comparison uses the fixed commits' merge-base. A newer main does
    # not change the PR's diff and must not prevent an approval-triggered rerun.
    return head, base, compare(repo, base, head)


def unchanged(repo, number, info, head):
    latest = api(f"repos/{repo}/pulls/{number}")
    if latest["head"]["sha"] != head or latest["title"] != info["title"] or latest["head"]["ref"] != info["head"]["ref"]:
        fail("pull request head, title or branch changed during the check")


def fix_qualifies(repo, number, info, paths, commits, table, switch):
    """Default deny. The same policy serves PR and release modes."""
    conditions = [(switch, "N7: .github/fix-route exists in the trusted policy commit"),
                  (info["title"].startswith("Fix: "), "N1: title starts `Fix: `"),
                  (not info["head"]["ref"].startswith("feature/"), "N1: branch is not feature/*"),
                  (not any(CLEARS.search(c["commit"]["message"]) for c in commits),
                   "clearing lines require the founder")]
    protected = ("requirements/", "migration/", "tests/acceptance/", ".github/",
                 "source-documents/", "source-issues/")
    owner_patterns = []
    in_founder_again = False
    for line in Path(ROOT, ".github/CODEOWNERS").read_text().splitlines():
        if line.strip() == "# The founder again":
            in_founder_again = True
        parts = line.split()
        if in_founder_again and parts and not parts[0].startswith("#"):
            pattern = parts[0].lstrip("/")
            owner_patterns.append(pattern + "**" if pattern.endswith("/") else pattern)
    for path in sorted(paths):
        reason = next((prefix + "**" for prefix in protected if path.startswith(prefix)), None)
        for tier, pattern, rx in table:
            if tier == "!" and rx.match(path):
                reason = "! " + pattern
        for pattern in owner_patterns:
            rx = "".join(".*" if t == "**" else "[^/]*" if t == "*" else re.escape(t)
                         for t in re.split(r"(\*\*|\*)", pattern) if t)
            if re.fullmatch(rx, path):
                reason = "CODEOWNERS founder again: " + pattern
        conditions.append((reason is None, f"N2/N6: {path}: {reason or 'outside never-a-fix paths'}"))
    comments = api(f"repos/{repo}/issues/{number}/comments?per_page=100")
    latest = {}
    for comment in sorted(comments, key=lambda c: (c.get("updated_at", c["created_at"]), c["id"])):
        if (comment.get("user") or {}).get("login") != "mlc-factory-bot":
            print(f"  N5: comment {comment['id']} ignored: author is not mlc-factory-bot")
            continue
        lines = comment.get("body", "").splitlines()
        match = VERDICT.fullmatch(lines[0]) if lines else None
        if match:
            latest[match[2]] = (match[3], match[4], FIX_ACK in lines)
    for reviewer in ("claude", "codex"):
        verdict = latest.get(reviewer)
        conditions.append((verdict == ("PASS", info["head"]["sha"], True),
                           f"N3/N4/N5: latest bot verdict for {reviewer}: {verdict}"))
    for ok, rule in conditions:
        print(f"  FIX {'OK' if ok else 'REFUSED'}: {rule}")
    return all(ok for ok, _ in conditions)


def pr(repo, number):
    table, who = rules(), founder()
    info = api(f"repos/{repo}/pulls/{number}")
    head, base, comparison = fixed_comparison(repo, number, info)
    files = comparison["files"]
    paths = {f["filename"] for f in files} | {f["previous_filename"] for f in files if f.get("previous_filename")}
    print(f"Pull request #{number} at {head}: {len(paths)} file(s); fixed base {base}")
    product, machinery = judge(paths, table)
    ok, why, approved_at = approved(repo, number, head, who)
    named, void, reason = clearing(repo, number, info["title"], approved_at, comparison["commits"])
    for c in named:
        subject = api(f"repos/{repo}/commits/{c}")["commit"]["message"].splitlines()[0]
        print(f"CLEARS A REFUSED RELEASE: approving this pull request also lets refused commit {c[:12]} "
              f"(\"{subject}\") ship. Read that commit before approving. It needs the founder's "
              "approval, even if every file is tooling, and the line in the title: "
              + "; ".join(f"Clears founder-gate refusal: {c[:12]}" for c in named))
    unchanged(repo, number, info, head)
    if void:
        fail(f"its commits clear a refused release, and {reason}")
    if machinery:
        print(f"RELEASE MACHINERY: {len(machinery)} file(s). Before merging without the founder, both "
              "reviews must say whether it weakens a check on production access, secrets, the deploy "
              "hold or branch rules, and a REAL rehearsal on the rehearsal copy must pass.")
    if not product and not named:
        print("PASS: every file is on the tooling list")
        return
    if not ok and not named:
        fix = fix_qualifies(repo, number, info, paths, comparison["commits"], table,
                            os.path.isfile(os.path.join(ROOT, ".github/fix-route")))
        unchanged(repo, number, info, head)
        if fix:
            # A verdict can be edited or replaced while the tip checks run. Read it again.
            fix = fix_qualifies(repo, number, info, paths, comparison["commits"], table,
                                os.path.isfile(os.path.join(ROOT, ".github/fix-route")))
            unchanged(repo, number, info, head)
        if fix:
            print("PASS: qualifying fix at the checked head; both reviewers passed")
            print("Fix route: rerun this check immediately before merging; a later CHANGES does not rerun it.")
            return
    if not ok:
        fail(f"{len(product)} product file(s) need the founder's approval, and {why}")
    print(f"PASS: {len(product)} product file(s); {why}")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout


def release(repo, sha, workflow):
    table, who = rules(), founder()
    # Where the last check passed: the newest successful push run of this release workflow on
    # main. Only GitHub writes these, so no seat can move the stop point by hand.
    # The newest 100 are enough: the walk also stops where this script first appeared.
    runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&event=push&status=success&per_page=100",
               every_page=False)
    passed = {r["head_sha"] for r in runs["workflow_runs"] if r["head_sha"] != sha}
    print(f"{len(passed)} earlier successful run(s) of {workflow} on main")
    me = os.path.relpath(os.path.abspath(__file__), ROOT)
    switch = git("ls-tree", "--name-only", sha, "--", ".github/fix-route").strip() == ".github/fix-route"
    refused, clears = [], set()
    for c in git("rev-list", "--first-parent", sha).split():
        if c in passed:
            print(f"Stopped at {c[:7]}: the release check passed there in an earlier run")
            break
        if subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{c}:{me}"], capture_output=True).returncode:
            print(f"Stopped at {c[:7]}: {me} was not there yet, so every change before it had the founder's approval")
            break
        parent = git("rev-parse", f"{c}^1").strip()
        paths = set(git("diff", "--name-only", "--no-renames", parent, c).splitlines()) - {""}
        print(f"{c[:7]}: {len(paths)} file(s)")
        product, _ = judge(paths, table)
        pulls = [p for p in api(f"repos/{repo}/commits/{c}/pulls")
                 if p["base"]["ref"] == "main" and p.get("merged_at") and p.get("merge_commit_sha") == c]
        ok, why, approved_at = (approved(repo, pulls[0]["number"], pulls[0]["head"]["sha"], who) if len(pulls) == 1
                                else (False, "it is not one pull request's merge into main", None))
        if ok:  # newer commits come first, so a clearing pull request is seen before what it clears
            # From its commit messages, which the approval fixes (any push dismisses it); its
            # description could still be edited after the approval.
            named, void, reason = clearing(repo, pulls[0]["number"], pulls[0]["title"], approved_at)
            clears |= set(named) - set(void)
            if void:
                print(f"  Its clearing line(s) do not count: {reason}")
        if not product:
            continue
        if c in clears:
            print(f"  OK: cleared by a later pull request the founder approved")
        elif ok:
            print(f"  OK: pull request #{pulls[0]['number']}: {why}")
        else:
            fix = False
            if len(pulls) == 1:
                pull = pulls[0]
                # All commits introduced by this merge are immutable, including squash commits.
                messages = [{"commit": {"message": git("show", "-s", "--format=%B", commit)}}
                            for commit in git("rev-list", f"{parent}..{c}").split()]
                original = compare(repo, parent, pull["head"]["sha"])
                original_paths = {f["filename"] for f in original["files"]} | {
                    f["previous_filename"] for f in original["files"] if f.get("previous_filename")}
                fix = fix_qualifies(repo, pull["number"], pull, paths | original_paths,
                                    messages + original["commits"], table, switch)
            if fix:
                print(f"  OK: qualifying fix pull request #{pulls[0]['number']}")
            else:
                print(f"  REFUSED: {why}")
                refused.append(c)
    if refused:
        print("Nothing was deployed. Who acts: the product's lead. Only the line below clears a refused "
              "commit: a revert alone does not. For each one, open a pull request (a revert, or one that "
              "keeps the change) with this line in one of its commit messages and in its title, and ask "
              "the founder to approve it after reading the refused commit:")
        for c in refused:
            print(f"  Clears founder-gate refusal: {c}")
        fail(f"{len(refused)} change(s) to product files without the founder's approval")
    print("PASS: every product change since the last passing release check has founder approval or qualifies as a fix")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "pr":
        try:
            pr(sys.argv[2], sys.argv[3])
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
            fail(f"cannot check pull request: {error}")
    elif len(sys.argv) == 5 and sys.argv[1] == "release":
        try:
            release(sys.argv[2], sys.argv[3], sys.argv[4])
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
            fail(f"cannot check release: {error}")
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

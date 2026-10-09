#!/usr/bin/env python3
"""Founder gate (founder, 9 Oct 2026: tooling merges without the founder; releases ship only
what the founder approved). The tooling list is .github/tooling-paths; the founder is the
owner of `*` in .github/CODEOWNERS. Both are read from this checkout, which is main's.

  founder_gate.py pr <owner/repo> <number>
      The founder-gate check on a pull request. Passes when every changed file is on the
      tooling list, or the founder's latest review approves the head commit. It reads the
      pull request through the API and never runs its code. To pick up an approval given
      after the last push, rerun the check: gh run rerun <run id> --repo <owner/repo>.

  founder_gate.py release <owner/repo> <commit> <release workflow file>
      Run by the release before it deploys. Every change that reached main since the release
      workflow last succeeded and touches a file off the tooling list must have come from a
      pull request the founder approved at its head commit, or be cleared by a later one with
      "Clears founder-gate refusal: <commit>" in one of its commit messages. Changes before this script
      existed were all founder-approved, so the walk stops there too.

Every changed file is printed with the rule that passed or stopped it. Exit 0 = pass.
"""
import json, os, re, subprocess, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIERS = {"A": "tooling, never runs in a release", "B": "release machinery"}


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def api(path):
    """Every page of a GitHub API list, or one object."""
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
        url = nxt.group(1) if nxt else None
    return out


def founder():
    for line in open(os.path.join(ROOT, ".github/CODEOWNERS")):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "*" and parts[1].startswith("@"):
            return parts[1][1:]
    fail("no `* @owner` line in .github/CODEOWNERS")


def rules():
    out = []
    for n, line in enumerate(open(os.path.join(ROOT, ".github/tooling-paths")), 1):
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
    reviews = [r for r in api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
               if (r.get("user") or {}).get("login") == who and r["state"] != "COMMENTED"]
    if not reviews:
        return False, f"{who} has not reviewed it"
    last = reviews[-1]
    if last["state"] != "APPROVED":
        return False, f"{who}'s latest review is {last['state']}"
    if last["commit_id"] != head:
        return False, f"{who} approved {last['commit_id'][:7]}, not the head {head[:7]}"
    return True, f"{who} approved the head {head[:7]}"


def judge(paths, table):
    """Print each file and its rule; return (product files, release-machinery files)."""
    product, machinery = [], []
    for p in sorted(paths):
        tier, why = classify(p, table)
        print(f"  {tier or '-'}  {p}  {why}")
        (product if tier is None else machinery if tier == "B" else []).append(p)
    return product, machinery


def pr(repo, number):
    table, who = rules(), founder()
    info = api(f"repos/{repo}/pulls/{number}")
    head = info["head"]["sha"]
    files = api(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if len(files) >= 3000:  # GitHub lists at most 3000; never judge an incomplete list
        fail("3000 or more changed files, too many to check; the founder approves")
    paths = {f["filename"] for f in files} | {f["previous_filename"] for f in files if f.get("previous_filename")}
    print(f"Pull request #{number} at {head[:7]}: {len(paths)} file(s)")
    product, machinery = judge(paths, table)
    if machinery:
        print(f"RELEASE MACHINERY: {len(machinery)} file(s). Before merging without the founder, both "
              "reviews must say whether it weakens a check on production access, secrets, the deploy "
              "hold or branch rules, and a REAL rehearsal on the Hostinger staging copy must pass.")
    if not product:
        print("PASS: every file is on the tooling list")
        return
    ok, why = approved(repo, number, head, who)
    if not ok:
        fail(f"{len(product)} product file(s) need the founder's approval, and {why}")
    print(f"PASS: {len(product)} product file(s); {why}")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout


CLEARS = re.compile(r"^Clears founder-gate refusal: ([0-9a-f]{40})\s*$", re.M)


def release(repo, sha, workflow):
    table, who = rules(), founder()
    # Where the last check passed: the newest successful push run of this release workflow on
    # main. Only GitHub writes these, so no seat can move the stop point by hand.
    runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&event=push&status=success&per_page=50")
    passed = {r["head_sha"] for r in runs["workflow_runs"] if r["head_sha"] != sha}
    print(f"{len(passed)} earlier successful run(s) of {workflow} on main")
    me = os.path.relpath(os.path.abspath(__file__), ROOT)
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
        ok, why = (approved(repo, pulls[0]["number"], pulls[0]["head"]["sha"], who) if len(pulls) == 1
                   else (False, "it is not one pull request's merge into main"))
        if ok:  # newer commits come first, so a clearing pull request is seen before what it clears
            # From its commit messages, which the approval fixes (any push dismisses it); its
            # description could still be edited after the approval.
            for pc in api(f"repos/{repo}/pulls/{pulls[0]['number']}/commits?per_page=100"):
                clears |= set(CLEARS.findall(pc["commit"]["message"]))
        if not product:
            continue
        if c in clears:
            print(f"  OK: cleared by a later pull request the founder approved")
        elif ok:
            print(f"  OK: pull request #{pulls[0]['number']}: {why}")
        else:
            print(f"  REFUSED: {why}")
            refused.append(c)
    if refused:
        print("Nothing was deployed. Who acts: the product's lead. For each refused commit, open a pull "
              "request that undoes it, or that keeps it, with this line in one of its commit messages, "
              "and ask the founder to approve it after reading the refused commit:")
        for c in refused:
            print(f"  Clears founder-gate refusal: {c}")
        fail(f"{len(refused)} change(s) to product files without the founder's approval")
    print("PASS: every product change since the last passing release check has the founder's approval")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "pr":
        pr(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 5 and sys.argv[1] == "release":
        release(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

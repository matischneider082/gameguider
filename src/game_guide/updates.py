"""Check for a newer GameGuider: an installed copy against the latest GitHub release, a source checkout against the
published branch."""
from __future__ import annotations

import importlib.metadata
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import REPO

BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}.git"
API_URL = f"https://api.github.com/repos/{REPO}/compare/"
RELEASES = f"repos/{REPO}/releases/latest"


def _version(text: str) -> tuple[int, ...]:
    """Up to three numeric components, zero-padded so 1.2 and 1.2.0 compare equal.

    A bare tuple compare treats a missing component as smaller than 0, so an installed 1.2 looked older than a release tagged 1.2.0.
    """
    parts = [int(n) for n in re.findall(r"\d+", text)[:3]]
    if not parts:
        return ()
    return tuple(parts + [0] * (3 - len(parts)))


def _latest_release() -> dict | None:
    """GitHub's latest release, read anonymously, else through the user's GitHub CLI login (a private repository)."""
    request = urllib.request.Request("https://api.github.com/" + RELEASES,
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "GameGuider-update-check"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read(200_000))
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(["gh", "api", RELEASES], capture_output=True, text=True, check=True, timeout=8)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _release_check() -> dict:
    """An installed copy (the installer's or any wheel) is current when no GitHub release has a higher version."""
    try:
        installed = importlib.metadata.version("game-guide")
    except importlib.metadata.PackageNotFoundError:
        installed = "0"
    release = _latest_release()
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or not _version(tag):
        return {"status": "error", "message": "Could not check GitHub for a newer release. Check your connection, then try again."}
    latest = tag.lstrip("v")
    url = release.get("html_url") if isinstance(release.get("html_url"), str) else f"https://github.com/{REPO}/releases"
    if _version(latest) > _version(installed):
        return {"status": "available", "message": f"GameGuider {latest} is available (you have {installed}).",
                "installed": installed, "latest": latest, "github_url": url}
    return {"status": "current", "message": f"GameGuider {installed} is the latest release.", "installed": installed,
            "latest": latest, "github_url": url}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=True, timeout=5,
    )
    return result.stdout.strip()


def _authenticated_compare(sha: str) -> dict | None:
    """Use the user's existing GitHub CLI session for private repositories."""
    endpoint = f"repos/{REPO}/compare/" + sha + "..." + urllib.parse.quote(BRANCH, safe="")
    try:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True, timeout=8)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def check_for_updates(root: Path | None = None) -> dict:
    # Do not mistake an enclosing Git repository for the installed package.
    root = (root if root is not None else Path(__file__).resolve().parents[2]).resolve()
    try:
        if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("Not a source checkout")
        sha = _git(root, "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("Invalid revision")
        branch = _git(root, "branch", "--show-current")
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return _release_check()  # not a checkout: an installed copy

    request = urllib.request.Request(
        API_URL + sha + "..." + urllib.parse.quote(BRANCH, safe=""),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "GameGuider-update-check"},
    )
    try:
        payload = _authenticated_compare(sha)
        if payload is None:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(100_000))
        status = payload["status"]
        if status not in {"identical", "ahead", "behind", "diverged"}:
            raise ValueError("Unknown comparison status")
        ahead = payload["ahead_by"]
        behind = payload["behind_by"]
        if type(ahead) is not int or type(behind) is not int or ahead < 0 or behind < 0:
            raise ValueError("Invalid comparison counts")
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "error", "message": "Could not compare this version with GitHub. Check your connection and GitHub CLI login (gh auth login); your account needs access to the repository. Unpublished local commits may also prevent comparison."}

    messages = {
        "identical": ("current", "Your checkout matches the latest GameGuider version on GitHub."),
        "ahead": ("available", "A newer GameGuider version is available on GitHub."),
        "behind": ("local_ahead", "Your checkout has commits that are not on the GitHub branch."),
        "diverged": ("diverged", "Your checkout and the GitHub branch have diverged. Review the changes before updating."),
    }
    label, message = messages[status]
    result = {"status": label, "message": message, "dirty": dirty, "branch": branch,
              "target_branch": BRANCH, "ahead_by": ahead, "behind_by": behind,
              "revision": sha[:7], "github_url": f"https://github.com/{REPO}/tree/" + BRANCH}
    if status == "ahead" and branch == BRANCH and not dirty:
        result["instructions"] = (f"cd {shlex.quote(str(root))}\n"
                                  f"git pull --ff-only {REPO_URL} {BRANCH}\n"
                                  "mise run setup\nmise run install-app")
    return result


def _installer_command() -> list[str]:
    base = f"https://github.com/{REPO}/releases/latest/download/"
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", f"irm {base}install.ps1 | iex"]
    return ["sh", "-c", f"curl -fsSL {base}install.sh | sh"]


def install_update() -> dict:
    """Run the release installer for an installed copy that has a newer release. A source checkout is never touched:
    it updates with git. The caller reopens GameGuider afterwards; the running copy keeps working until then."""
    check = check_for_updates()
    if check["status"] != "available" or "latest" not in check:
        raise ValueError("There is no newer release to install on this copy.")
    try:
        result = subprocess.run(_installer_command(), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return {"status": "error", "message": "Could not run the installer. Check your connection, then try again."}
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-5:])
        return {"status": "error", "message": "The installer failed.", "detail": tail}
    return {"status": "installed", "latest": check["latest"],
            "message": f"GameGuider {check['latest']} is installed. Quit and reopen GameGuider to use it."}

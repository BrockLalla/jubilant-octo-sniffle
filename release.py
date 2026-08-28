"""Cuts and publishes a new release of Pantry Tracker.

What it does, in order:
  1. Bumps VERSION (patch/minor/major).
  2. Rebuilds the .app via pantry.spec.
  3. Zips it and writes a .sha256 checksum file next to the zip.
  4. Commits the version bump and tags it in git.
  5. Pushes the commit + tag, then creates a GitHub Release with the zip
     and checksum attached, using the GitHub REST API directly (no `gh`
     CLI needed -- just a Personal Access Token).

Every Mac running Pantry Tracker checks this repo's latest release and
installs it automatically (see updater.py) -- so running this script IS
"pushing an update" to the church's computer, no physical access needed.

Usage:
    export GITHUB_TOKEN=ghp_...                 # a token with 'repo' scope (classic)
                                                  # or Contents:write (fine-grained), on the release repo
    export GITHUB_REPO=yourname/pantry-tracker   # owner/repo
    python3 release.py                           # bumps patch version (1.0.0 -> 1.0.1)
    python3 release.py --bump minor              # 1.0.1 -> 1.1.0
    python3 release.py --notes "Fixed the thing"
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(ROOT, "VERSION")
ZIP_NAME = "PantryTracker-mac.zip"


def read_version():
    with open(VERSION_FILE) as f:
        return f.read().strip()


def bump(version, part):
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def run(cmd, **kwargs):
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_and_package(version):
    print(f"\n=== Building v{version} ===")
    for d in ("build", "dist"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "pantry.spec"])

    app_path = os.path.join(ROOT, "dist", "Pantry Tracker.app")
    if not os.path.isdir(app_path):
        raise SystemExit(f"Build did not produce {app_path}")

    zip_path = os.path.join(ROOT, "dist", ZIP_NAME)
    print(f"Zipping {app_path} -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(app_path):
            for name in filenames:
                full = os.path.join(dirpath, name)
                arcname = os.path.relpath(full, os.path.dirname(app_path))
                z.write(full, arcname)

    checksum = sha256_file(zip_path)
    sha_path = zip_path + ".sha256"
    with open(sha_path, "w") as f:
        f.write(f"{checksum}  {ZIP_NAME}\n")
    print(f"SHA256: {checksum}")
    return zip_path, sha_path


def git_commit_tag_push(version, notes):
    tag = f"v{version}"
    run(["git", "add", "VERSION"])
    # Nothing to commit is not an error (e.g. re-running after a failed publish step)
    result = subprocess.run(["git", "commit", "-m", f"Release {tag}"], cwd=ROOT)
    if result.returncode not in (0, 1):
        raise SystemExit("git commit failed")
    run(["git", "tag", "-f", tag, "-m", notes or f"Release {tag}"])
    run(["git", "push"])
    run(["git", "push", "-f", "origin", tag])
    return tag


def create_github_release(repo, token, tag, notes, zip_path, sha_path):
    print(f"\n=== Publishing GitHub release {tag} to {repo} ===")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "PantryTracker-release-script",
    }
    payload = json.dumps({
        "tag_name": tag,
        "name": tag,
        "body": notes or "",
        "draft": False,
        "prerelease": False,
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Failed to create release: {e.code} {e.read().decode()}")

    upload_url = release["upload_url"].split("{")[0]
    for path in (zip_path, sha_path):
        _upload_asset(upload_url, headers, path)
    print(f"\nDone: {release['html_url']}")


def _upload_asset(upload_url, headers, path):
    name = os.path.basename(path)
    print(f"Uploading {name}...")
    with open(path, "rb") as f:
        data = f.read()
    upload_headers = dict(headers)
    upload_headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(f"{upload_url}?name={name}", data=data, headers=upload_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Failed to upload {name}: {e.code} {e.read().decode()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bump", choices=["patch", "minor", "major"], default="patch")
    parser.add_argument("--notes", default="", help="Release notes shown to... well, nobody sees them, but future you might.")
    parser.add_argument("--skip-publish", action="store_true", help="Build and package only; don't touch git or GitHub.")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not args.skip_publish and (not repo or not token):
        raise SystemExit(
            "Set GITHUB_REPO (owner/repo) and GITHUB_TOKEN (a Personal Access Token with repo/contents "
            "write access) as environment variables first, or pass --skip-publish to just build and "
            "package locally without publishing anything."
        )

    old_version = read_version()
    new_version = bump(old_version, args.bump)
    with open(VERSION_FILE, "w") as f:
        f.write(new_version + "\n")
    print(f"Version: {old_version} -> {new_version}")

    zip_path, sha_path = build_and_package(new_version)

    if args.skip_publish:
        print(f"\nBuilt and packaged (not published): {zip_path}")
        return

    tag = git_commit_tag_push(new_version, args.notes)
    create_github_release(repo, token, tag, args.notes, zip_path, sha_path)


if __name__ == "__main__":
    main()

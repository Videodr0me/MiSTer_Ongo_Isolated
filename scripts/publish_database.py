#!/usr/bin/env python3
"""Publish a validated build to the existing db branch in one GitHub commit."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from build_database import BuildError, read_database, safe_path, sha256


def git_blob_hash(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    # GitHub's per-repository workflow token is used only in memory.
    token = os.environ.get("GITHUB_TOKEN")
    if not token or args.repository != "Videodr0me/MiSTer_Ongo_Isolated":
        raise BuildError("Missing workflow token or unexpected publication repository")

    def api(path, data=None, method=None):
        request = urllib.request.Request("https://api.github.com/repos/" + args.repository + "/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method, headers={"Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "MiSTer-Ongo-Isolated"})
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)

    state = json.loads((args.output / ".build-state.json").read_text())
    if not state["previous_manifest_sha256"]:
        raise BuildError("Initial publication must be reviewed and bootstrapped separately")
    head = api("git/ref/heads/db")["object"]["sha"]
    commit = api("git/commits/" + head)
    tree = api("git/trees/" + commit["tree"]["sha"] + "?recursive=1")
    if tree.get("truncated"):
        raise BuildError("Publication tree is unexpectedly large")
    existing = {entry["path"]: entry for entry in tree["tree"] if entry["type"] == "blob"}
    old_blob = api("git/blobs/" + existing["db.json.zip"]["sha"])
    if sha256(base64.b64decode(old_blob["content"])) != state["previous_manifest_sha256"]:
        raise BuildError("Published database changed during this build; retry using the new version")
    generated = {}
    for path, digest in state["files"].items():
        safe_path(path)
        if path not in ("db.json.zip", "report.json", "README.md", "LICENSE.upstream", "downloader_ongo_isolated.ini") and not (
                path.startswith("launchers/") and path.endswith(".mgl")):
            raise BuildError("Unexpected publication file")
        data = (args.output / path).read_bytes()
        if sha256(data) != digest:
            raise BuildError("Generated publication file changed after validation")
        generated[path] = data
    manifest = read_database(generated["db.json.zip"])
    if manifest["db_id"] != args.repository:
        raise BuildError("Publication identity mismatch")
    changes = []
    for path, data in sorted(generated.items()):
        if existing.get(path, {}).get("sha") == git_blob_hash(data):
            continue
        blob = api("git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        changes.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    if not changes:
        print("Published database is already current.")
        return
    # Retain older content-addressed launchers for clients holding an earlier manifest.
    new_tree = api("git/trees", {"base_tree": commit["tree"]["sha"], "tree": changes})
    identity = {"name": "Videodr0me", "email": "22127864+Videodr0me@users.noreply.github.com"}
    new_commit = api("git/commits", {"message": "Refresh isolated Ongo arcade database",
        "tree": new_tree["sha"], "parents": [head], "author": identity, "committer": identity})
    api("git/refs/heads/db", {"sha": new_commit["sha"], "force": False}, method="PATCH")
    if api("git/ref/heads/db")["object"]["sha"] != new_commit["sha"]:
        raise BuildError("Published branch verification failed")
    print("Published database commit " + new_commit["sha"])


if __name__ == "__main__":
    main()

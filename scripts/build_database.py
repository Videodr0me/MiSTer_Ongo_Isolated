#!/usr/bin/env python3
"""Build an arcade-only Downloader catalog; never download core or ROM binaries."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


class BuildError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def safe_path(path):
    if (not isinstance(path, str) or not path or path.startswith(("/", "|"))
            or "\\" in path or ":" in path or any(ord(c) < 32 for c in path)
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in path.split("/"))):
        raise BuildError("Unsafe destination path")
    return path


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in
            ("raw.githubusercontent.com", "github.com", "archive.org")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise BuildError("Unexpected download URL; review the upstream source")
    return url


def read_database(data):
    if data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != 1 or not names[0].endswith(".json"):
                raise BuildError("Database ZIP must contain exactly one JSON file")
            if archive.getinfo(names[0]).file_size > 64 * 1024 * 1024:
                raise BuildError("Database JSON is unexpectedly large")
            data = archive.read(names[0])
    result = json.loads(data)
    if not isinstance(result.get("files"), dict) or not result["files"]:
        raise BuildError("Empty or invalid upstream database")
    if result.get("v", 0) not in (0, 1):
        raise BuildError("Unsupported database schema version")
    return result


def fetch(url, limit=32 * 1024 * 1024):
    public_url(url)
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "MiSTer-Ongo-Isolated"})
            with urllib.request.urlopen(request, timeout=45) as response:
                public_url(response.url)
                data = response.read(limit + 1)
            if len(data) > limit:
                raise BuildError("Upstream response exceeds its size limit")
            return data
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def source_url(db, path, spec):
    url = spec.get("url")
    if not url:
        base = db.get("base_files_url", "")
        if not base.endswith("/"):
            raise BuildError("Missing source URL")
        url = base + urllib.parse.quote(path.lstrip("|"), safe="/")
    return public_url(url)


def tags_for(db, spec):
    result = set()
    dictionary = db.get("tag_dictionary", {})
    for tag in spec.get("tags", []):
        if isinstance(tag, str):
            result.add(tag)
        else:
            names = [name for name, number in dictionary.items() if number == tag]
            if not names:
                raise BuildError("Unknown numeric filter tag")
            result.update(names)
    return sorted(result)


def translated_spec(db, path, spec):
    allowed = {"hash", "size", "url", "tags", "path", "overwrite", "tangle"}
    if set(spec) - allowed:
        raise BuildError("Unexpected metadata on a selected file: " + path)
    result = deepcopy(spec)
    if (not re.fullmatch(r"[0-9a-f]{32}", result.get("hash", ""))
            or not isinstance(result.get("size"), int) or result["size"] < 0):
        raise BuildError("Invalid file integrity metadata: " + path)
    result["url"] = source_url(db, path, spec)
    result["tags"] = tags_for(db, spec)
    return result


def arcade_destination(path, root):
    safe_path(path)
    if path.startswith("_Arcade/cores/"):
        if len(PurePosixPath(path).parts) != 3 or not path.lower().endswith(".rbf"):
            raise BuildError("Unexpected arcade core layout: " + path)
        return root + "/cores/" + PurePosixPath(path).name
    if path.startswith("_Arcade/") and path.lower().endswith((".mra", ".mgl")):
        return root + "/" + path
    raise BuildError("Unexpected arcade file: " + path)


def core_matches(filename, reference):
    """Match Main_MiSTer's case-insensitive prefix plus dot/underscore boundary."""
    filename = filename.lower()
    reference = reference.lower()
    return filename.endswith(".rbf") and any(
        filename.startswith(prefix) and filename[len(prefix):len(prefix) + 1] in (".", "_")
        for prefix in (reference, "arcade-" + reference))


def find_core(db, reference):
    matches = [(path, spec) for path, spec in db["files"].items()
               if path.startswith("_Arcade/cores/") and len(PurePosixPath(path).parts) == 3
               and core_matches(PurePosixPath(path).name, reference) and spec["size"] > 0]
    # Main chooses the lexically greatest matching filename, with case preserved.
    return max(matches, key=lambda item: PurePosixPath(item[0]).name) if matches else None


def verify_launcher(data, spec):
    if len(data) != spec["size"] or hashlib.md5(data).hexdigest() != spec["hash"]:
        raise BuildError("Launcher bytes do not match the upstream hash and size")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise BuildError("XML entity declarations are not accepted")
    try:
        xml = ET.fromstring(data)
    except ET.ParseError as error:
        raise BuildError("Malformed upstream launcher XML") from error
    refs = [node.text.strip() for node in xml.iter("rbf") if node.text and node.text.strip()]
    if len(refs) != 1:
        raise BuildError("Each arcade launcher must contain one RBF reference")
    return xml, refs[0]


def rewrite_mgl(data, reference, root):
    if not reference.lower().startswith("_arcade/cores/") or len(reference.split("/")) != 3:
        raise BuildError("MGL references a core outside the approved arcade tree")
    target = root + "/cores/" + reference.rsplit("/", 1)[1]
    text, count = re.subn(r"(<rbf>)(\s*)" + re.escape(reference) + r"(\s*)(</rbf>)",
                          lambda match: match[1] + match[2] + target + match[3] + match[4],
                          data.decode("utf-8"))
    if count != 1:
        raise BuildError("Cannot safely rewrite the MGL core path")
    return text.encode("utf-8")


def checked_launchers(db, cache):
    paths = [path for path in db["files"] if path.startswith("_Arcade/")
             and path.lower().endswith((".mra", ".mgl"))]
    cache.mkdir(parents=True, exist_ok=True)

    def load(path):
        safe_path(path)
        spec = translated_spec(db, path, db["files"][path])
        target = cache / (spec["hash"] + PurePosixPath(path).suffix.lower())
        data = target.read_bytes() if target.exists() else fetch(source_url(db, path, spec), 4 * 1024 * 1024)
        xml, reference = verify_launcher(data, spec)
        if not target.exists():
            target.write_bytes(data)
        return path, (data, xml, reference)

    result = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        for number, (path, value) in enumerate(executor.map(load, paths), 1):
            result[path] = value
            if number % 200 == 0 or number == len(paths):
                print("Checked launchers: %d/%d (%d%%)" % (number, len(paths), 100 * number / len(paths)), flush=True)
    return result


def compare_previous(manifest, previous, maximum_removal_fraction):
    if previous is None:
        return
    if manifest["db_id"] != previous.get("db_id"):
        raise BuildError("Database identity changed")
    before = {p.casefold() for p in previous["files"]}
    after = {p.casefold() for p in manifest["files"]}
    removed = before - after
    if len(removed) > len(before) * maximum_removal_fraction:
        raise BuildError("Large removal blocked: %d of %d paths" % (len(removed), len(before)))


def build(config, dbs, launchers, previous=None):
    root = safe_path(config["menu_root"])
    if not re.fullmatch(r"_[A-Za-z0-9_-]+", root):
        raise BuildError("Menu root must be one underscore-prefixed directory")
    ongo = dbs["ongo"]
    files, payloads, folded = {}, {}, {}
    report = {"external_dependencies": [], "manual_cores": [], "unlisted_roms": [],
              "excluded_files": [], "shared_roms": []}

    def put(path, spec):
        safe_path(path)
        key = path.casefold()
        if key in folded:
            old_path = folded[key]
            if old_path != path or files[old_path] != spec:
                raise BuildError("Conflicting destination: " + path)
            return
        folded[key] = path
        files[path] = spec

    allowed_games = tuple("games/" + name + "/" for name in config["game_directories"])
    normal_roms = {path.lstrip("|").casefold(): spec for path, spec in dbs["roms"]["files"].items()}
    for path, original in ongo["files"].items():
        if path.startswith("_Arcade/"):
            destination = arcade_destination(path, root)
            if path.lower().endswith(".rbf") and original["size"] == 0:
                report["excluded_files"].append({"path": path, "reason": "Empty upstream RBF"})
                continue
            spec = translated_spec(ongo, path, original)
            spec["path"] = "internal"
            put(destination, spec)
        elif path.startswith(allowed_games):
            spec = translated_spec(ongo, path, original)
            spec["path"] = "pext"
            if path.casefold() in normal_roms:
                other = normal_roms[path.casefold()]
                if spec["hash"] != other["hash"] or spec["size"] != other["size"]:
                    raise BuildError("ROM conflicts with the standard Arcade ROMs database: " + path)
                report["shared_roms"].append(path)
                continue
            put(path, spec)
    if len(launchers) < config["minimum_launchers"]:
        raise BuildError("Too few upstream arcade launchers")
    ongo_core_count = sum(p.startswith(root + "/cores/") for p in files)
    if ongo_core_count < config["minimum_ongo_cores"]:
        raise BuildError("Too few nonempty upstream arcade cores")

    references = defaultdict(list)
    known_roms = {PurePosixPath(p).name.casefold() for p in normal_roms}
    known_roms.update(PurePosixPath(p).name.casefold() for p in files
                      if p.startswith(("games/mame/", "games/hbmame/")))
    for path, (data, xml, reference) in launchers.items():
        destination = arcade_destination(path, root)
        if path.lower().endswith(".mgl"):
            rewritten = rewrite_mgl(data, reference, root)
            payload_path = "launchers/" + sha256(rewritten) + ".mgl"
            payloads[payload_path] = rewritten
            files[destination].update(hash=hashlib.md5(rewritten).hexdigest(), size=len(rewritten),
                                      url="https://raw.githubusercontent.com/" + config["repository"] + "/db/" + payload_path)
            setname = xml.findtext("setname")
            if not setname or "/" in setname or "\\" in setname:
                raise BuildError("MGL game directory needs review")
            for node in xml.iter("file"):
                game_path = "games/" + setname + "/" + safe_path(node.attrib["path"])
                if game_path not in files:
                    raise BuildError("MGL game file is not included: " + game_path)
        elif "/" in reference or "\\" in reference or ":" in reference:
            raise BuildError("MRA core reference contains an unexpected path")
        core = reference.rsplit("/", 1)[-1]
        references[core].append(destination)
        # ZIP alternatives are fallbacks; a group needs at least one catalog entry.
        missing_groups = []
        for node in xml.iter():
            if "zip" not in node.attrib:
                continue
            choices = [z.strip() for z in node.attrib["zip"].split("|") if z.strip()]
            if choices and not any(PurePosixPath(z).name.casefold() in known_roms for z in choices):
                missing_groups.append(choices)
        if missing_groups:
            report["unlisted_roms"].append({"launcher": destination, "zip_alternatives": missing_groups})

    for core, users in sorted(references.items()):
        if find_core(ongo, core):
            continue
        found = None
        for name in config["dependency_priority"]:
            match = find_core(dbs[name], core)
            if match:
                found = name, match
                break
        if found:
            name, (path, original) = found
            spec = translated_spec(dbs[name], path, original)
            normalized_tags = {tag.lower().replace("-", "").replace("_", "") for tag in spec["tags"]}
            if normalized_tags & {"jtbeta", "coinopcollectionbeta", "coinopcollectionalpha"}:
                raise BuildError("A dependency has become a restricted core: " + core)
            spec["path"] = "internal"
            spec["tangle"] = sorted(set(spec.get("tangle", [])) | {"ongo_dependency_" + core.lower()})
            destination = arcade_destination(path, root)
            put(destination, spec)
            report["external_dependencies"].append({"reference": core, "database": name,
                                                     "source": path, "destination": destination,
                                                     "launchers": users})
        elif core.casefold() in config["manual_cores"]:
            report["manual_cores"].append({"reference": core, "instructions": config["manual_cores"][core.casefold()],
                                           "launchers": users})
        else:
            raise BuildError("Unresolved core dependency: " + core)

    # Assert each automatic reference resolves in the final directory as Main would.
    for core in references:
        candidates = [p for p in files if p.startswith(root + "/cores/")
                      and core_matches(PurePosixPath(p).name, core)]
        if not candidates and core.casefold() not in config["manual_cores"]:
            raise BuildError("Final core lookup failed: " + core)
    folders = {}
    for path, spec in files.items():
        for parent in PurePosixPath(path).parents:
            if str(parent) == ".":
                continue
            info = folders.setdefault(str(parent), {"path": spec["path"], "tags": set()})
            if info["path"] != spec["path"]:
                raise BuildError("Inconsistent storage flags")
            info["tags"].update(spec["tags"])
    for info in folders.values():
        info["tags"] = sorted(info["tags"])
    manifest = {"db_id": config["db_id"], "db_url": "https://raw.githubusercontent.com/" + config["repository"] + "/db/db.json.zip",
                "v": 1, "timestamp": int(time.time()), "files": files, "folders": folders}
    compare_previous(manifest, previous, config["maximum_removal_fraction"])
    if previous and all(manifest[k] == previous.get(k) for k in ("db_id", "db_url", "v", "files", "folders")):
        manifest["timestamp"] = previous["timestamp"]
    report["database_id"] = config["db_id"]
    report["counts"] = {"files": len(files), "mras": sum(p.endswith(".mra") for p in files),
                        "mgls": sum(p.endswith(".mgl") for p in files), "ongo_cores": ongo_core_count,
                        "dependency_cores": len(report["external_dependencies"]),
                        "rom_archives": sum(p.startswith(("games/mame/", "games/hbmame/")) for p in files),
                        "manual_core_launchers": sum(len(r["launchers"]) for r in report["manual_cores"]),
                        "unlisted_rom_launchers": len(report["unlisted_roms"])}
    report["validation_scope"] = "Launcher hashes, XML, destination paths and catalog dependencies. ROM bytes and hardware behavior are not tested."
    return manifest, report, payloads


def write_output(output, manifest, report, payloads, previous_hash):
    output.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("db.json", (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json_bytes(manifest))
    contents = {"db.json.zip": buffer.getvalue(), "report.json": json_bytes(report), **payloads}
    contents["LICENSE.upstream"] = (Path(__file__).resolve().parents[1] / "LICENSE.upstream").read_bytes()
    contents["downloader_ongo_isolated.ini"] = ("[" + manifest["db_id"] + "]\n"
        "db_url = " + manifest["db_url"] + "\n"
        "description = Ongo arcade cores in _Ongo\n").encode()
    contents["README.md"] = ("# Published arcade database\n\n"
        "Generated from Ongo's arcade collection. See the main branch for installation and source code.\n\n"
        "See [report.json](report.json) for source identities, external core requirements, and missing ROM catalog entries.\n").encode()
    for path, data in contents.items():
        target = output / safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / ".build-state.json").write_bytes(json_bytes({"previous_manifest_sha256": previous_hash,
        "files": {path: sha256(data) for path, data in contents.items()}}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "adapter.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    previous_group = parser.add_mutually_exclusive_group(required=True)
    previous_group.add_argument("--previous", type=Path)
    previous_group.add_argument("--previous-url")
    previous_group.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    for path in (args.output, args.cache):
        if path.resolve() == repo_root or repo_root in path.resolve().parents:
            raise BuildError("Keep generated output and caches outside the source repository")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    previous_bytes = args.previous.read_bytes() if args.previous else fetch(args.previous_url) if args.previous_url else None
    previous = read_database(previous_bytes) if previous_bytes else None

    def load(pair):
        name, spec = pair
        data = fetch(spec["url"])
        db = read_database(data)
        if db.get("db_id") != spec["id"]:
            raise BuildError("Upstream database identity mismatch: " + name)
        return name, (db, {"url": spec["url"], "db_id": db["db_id"], "sha256": sha256(data), "timestamp": db["timestamp"]})

    with ThreadPoolExecutor(max_workers=5) as executor:
        sources = dict(executor.map(load, config["sources"].items()))
    dbs = {name: value[0] for name, value in sources.items()}
    launchers = checked_launchers(dbs["ongo"], args.cache)
    manifest, report, payloads = build(config, dbs, launchers, previous)
    report["sources"] = {name: value[1] for name, value in sources.items()}
    write_output(args.output, manifest, report, payloads, sha256(previous_bytes) if previous_bytes else None)
    print(json.dumps(report["counts"], sort_keys=True))


if __name__ == "__main__":
    main()

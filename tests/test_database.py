"""Regression checks for destination isolation, dependency lookup and safe updates."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "build_database.py"
SPEC = importlib.util.spec_from_file_location("adapter", SOURCE)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def metadata(data=b"fixture", **extra):
    return {"hash": hashlib.md5(data).hexdigest(), "size": len(data), "tags": [1], **extra}


def database(files):
    return {"files": files, "tag_dictionary": {"arcade": 1},
            "base_files_url": "https://raw.githubusercontent.com/example/release/0123456789abcdef/"}


def fixture():
    config = {"db_id": "example/arcade", "repository": "example/arcade", "menu_root": "_Ongo",
              "minimum_launchers": 1, "minimum_ongo_cores": 1, "maximum_removal_fraction": 0.1,
              "game_directories": ["mame", "hbmame", "Battletoads", "kinst", "kinst2"],
              "dependency_priority": ["official"], "manual_cores": {"private": "Supply separately"}}
    mra = b"<misterromdescription><rbf>Example</rbf><rom zip='example.zip'/></misterromdescription>"
    dbs = {"ongo": database({"_Arcade/Example.mra": metadata(mra), "_Arcade/cores/Example_20260101.rbf": metadata(),
                             "games/mame/example.zip": metadata(path="pext"),
                             "_Console/Console.rbf": metadata(), "games/VECTREX/art.bin": metadata(),
                             "Filters/Extra.txt": metadata(), "Scripts/extra.sh": metadata()}),
           "roms": database({}), "official": database({})}
    launchers = {"_Arcade/Example.mra": (mra, ET.fromstring(mra), "Example")}
    return config, dbs, launchers


class AdapterTests(unittest.TestCase):
    def test_arcade_only_and_original_download_urls(self):
        config, dbs, launchers = fixture()
        result, report, payloads = adapter.build(config, dbs, launchers)
        self.assertEqual(set(result["files"]), {"_Ongo/_Arcade/Example.mra", "_Ongo/cores/Example_20260101.rbf", "games/mame/example.zip"})
        spec = result["files"]["_Ongo/cores/Example_20260101.rbf"]
        self.assertIn("/_Arcade/cores/Example_20260101.rbf", spec["url"])
        self.assertNotIn("/_Ongo/", spec["url"])
        self.assertEqual(result["files"]["games/mame/example.zip"]["path"], "pext")
        self.assertEqual(result["folders"]["_Ongo/cores"]["path"], "internal")
        self.assertFalse(payloads)

    def test_external_core_has_isolated_destination_and_stable_tangle(self):
        config, dbs, launchers = fixture()
        data, xml, _ = launchers["_Arcade/Example.mra"]
        launchers["_Arcade/Example.mra"] = data, xml, "Other"
        dbs["official"]["files"]["_Arcade/cores/Other.rbf"] = metadata()
        result, report, _ = adapter.build(config, dbs, launchers)
        spec = result["files"]["_Ongo/cores/Other.rbf"]
        self.assertIn("ongo_dependency_other", spec["tangle"])
        self.assertNotIn("_Arcade/cores/Other.rbf", result["files"])
        self.assertEqual(report["counts"]["dependency_cores"], 1)

    def test_main_matching_boundaries_and_pinned_versions(self):
        for filename in ("Example.rbf", "example_20260101.rbf", "Arcade-Example_20260101.rbf"):
            self.assertTrue(adapter.core_matches(filename, "Example"))
        self.assertFalse(adapter.core_matches("Example2_20260101.rbf", "Example"))
        self.assertTrue(adapter.core_matches("zerowing_20240404.rbf", "zerowing_20240404"))
        self.assertFalse(adapter.core_matches("zerowing_20260701.rbf", "zerowing_20240404"))

    def test_unknown_core_blocks_generation(self):
        config, dbs, launchers = fixture()
        data, xml, _ = launchers["_Arcade/Example.mra"]
        launchers["_Arcade/Example.mra"] = data, xml, "Unknown"
        with self.assertRaisesRegex(adapter.BuildError, "Unresolved"):
            adapter.build(config, dbs, launchers)

    def test_known_private_core_is_reported(self):
        config, dbs, launchers = fixture()
        data, xml, _ = launchers["_Arcade/Example.mra"]
        launchers["_Arcade/Example.mra"] = data, xml, "Private"
        _, report, _ = adapter.build(config, dbs, launchers)
        self.assertEqual(report["counts"]["manual_core_launchers"], 1)

    def test_restricted_dependency_tags_are_normalized(self):
        config, dbs, launchers = fixture()
        data, xml, _ = launchers["_Arcade/Example.mra"]
        launchers["_Arcade/Example.mra"] = data, xml, "Other"
        dbs["official"]["files"]["_Arcade/cores/Other.rbf"] = metadata(tags=["coinopcollectionbeta"])
        with self.assertRaisesRegex(adapter.BuildError, "restricted core"):
            adapter.build(config, dbs, launchers)

    def test_empty_core_excluded_and_not_a_dependency(self):
        config, dbs, launchers = fixture()
        dbs["ongo"]["files"]["_Arcade/cores/Empty_20260101.rbf"] = metadata(b"")
        result, report, _ = adapter.build(config, dbs, launchers)
        self.assertNotIn("_Ongo/cores/Empty_20260101.rbf", result["files"])
        self.assertEqual(len(report["excluded_files"]), 1)

    def test_rom_collision_requires_identical_content(self):
        config, dbs, launchers = fixture()
        dbs["roms"]["files"]["|games/mame/example.zip"] = metadata(b"different")
        with self.assertRaisesRegex(adapter.BuildError, "ROM conflicts"):
            adapter.build(config, dbs, launchers)
        dbs["roms"]["files"]["|games/mame/example.zip"] = metadata()
        result, report, _ = adapter.build(config, dbs, launchers)
        self.assertNotIn("games/mame/example.zip", result["files"])
        self.assertEqual(report["shared_roms"], ["games/mame/example.zip"])

    def test_case_insensitive_collision_rejected(self):
        config, dbs, launchers = fixture()
        dbs["ongo"]["files"]["_Arcade/example.mra"] = metadata()
        with self.assertRaisesRegex(adapter.BuildError, "Conflicting destination"):
            adapter.build(config, dbs, launchers)

    def test_mgl_rewrite_preserves_game_files(self):
        config, dbs, launchers = fixture()
        mgl = b'<mistergamedescription><rbf>_Arcade/cores/Example</rbf><setname>Battletoads</setname><file path="rom.bin"/></mistergamedescription>'
        dbs["ongo"]["files"]["_Arcade/Example.mgl"] = metadata(mgl)
        dbs["ongo"]["files"]["games/Battletoads/rom.bin"] = metadata()
        launchers["_Arcade/Example.mgl"] = mgl, ET.fromstring(mgl), "_Arcade/cores/Example"
        result, _, payloads = adapter.build(config, dbs, launchers)
        rewritten = next(iter(payloads.values()))
        self.assertIn(b"<rbf>_Ongo/cores/Example</rbf>", rewritten)
        self.assertIn(b'path="rom.bin"', rewritten)
        self.assertEqual(result["files"]["_Ongo/_Arcade/Example.mgl"]["hash"], hashlib.md5(rewritten).hexdigest())
        self.assertIn("games/Battletoads/rom.bin", result["files"])

    def test_launcher_tampering_and_xml_entities_rejected(self):
        with self.assertRaises(adapter.BuildError):
            adapter.verify_launcher(b"tampered", metadata(b"original"))
        xml = b'<!DOCTYPE x [<!ENTITY x "bad">]><x><rbf>Example</rbf></x>'
        with self.assertRaises(adapter.BuildError):
            adapter.verify_launcher(xml, metadata(xml))

    def test_destination_and_url_safety(self):
        for path in ("../bad", "/absolute", "_Ongo//empty", "_Ongo/../escape", "games\\bad", "_Ongo/trailing."):
            with self.assertRaises(adapter.BuildError):
                adapter.safe_path(path)
        for url in ("http://archive.org/file", "https://example.invalid/file", "https://archive.org/file?token=value"):
            with self.assertRaises(adapter.BuildError):
                adapter.public_url(url)

    def test_mass_deletion_guard_and_stable_timestamp(self):
        config, dbs, launchers = fixture()
        previous, _, _ = adapter.build(config, dbs, launchers)
        previous["timestamp"] = 1234567890
        unchanged, _, _ = adapter.build(config, dbs, launchers, previous)
        self.assertEqual(unchanged["timestamp"], previous["timestamp"])
        del dbs["ongo"]["files"]["games/mame/example.zip"]
        with self.assertRaisesRegex(adapter.BuildError, "Large removal"):
            adapter.build(config, dbs, launchers, previous)

    def test_output_zip_roundtrip_and_manifest_identity(self):
        config, dbs, launchers = fixture()
        manifest, report, payloads = adapter.build(config, dbs, launchers)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            adapter.write_output(output, manifest, report, payloads, None)
            self.assertEqual(adapter.read_database((output / "db.json.zip").read_bytes()), manifest)
            state = json.loads((output / ".build-state.json").read_text())
            for path, digest in state["files"].items():
                self.assertEqual(adapter.sha256((output / path).read_bytes()), digest)
        other = deepcopy(manifest)
        other["db_id"] = "wrong/identity"
        with self.assertRaisesRegex(adapter.BuildError, "identity"):
            adapter.compare_previous(manifest, other, 0.1)


if __name__ == "__main__":
    unittest.main()

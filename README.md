# Ongo arcade cores in a separate menu

An arcade-only adapter for [Ongo's MiSTer collection](https://github.com/OngoGablogian/MiSTer_Ongo).
Start **Update All normally**. Its Downloader installs the adapted catalog with normal file tracking and cleanup.

## Install

Copy [downloader_ongo_isolated.ini](https://raw.githubusercontent.com/Videodr0me/MiSTer_Ongo_Isolated/db/downloader_ongo_isolated.ini)
to the root of the MiSTer SD card, alongside `downloader.ini`. Alternatively add:

```ini
[Videodr0me/MiSTer_Ongo_Isolated]
db_url = https://raw.githubusercontent.com/Videodr0me/MiSTer_Ongo_Isolated/db/db.json.zip
description = Ongo arcade cores in _Ongo
```

Keep the normal distributions and **Arcade ROMs Database** enabled. Enable this adapter instead of Ongo's original database;
using both would install a second set of Ongo launchers in the ordinary menus.
If the original database was previously installed, review its tracked files before uninstalling it; this adapter does not migrate or delete that installation.

| Content | Destination |
| --- | --- |
| Arcade MRAs and the Battletoads MGL | `_Ongo/_Arcade/` |
| Arcade RBFs, including required public dependency cores | `_Ongo/cores/` |
| MAME and HBMAME archives | `games/mame/`, `games/hbmame/` |
| Battletoads game files | `games/Battletoads/` |
| Killer Instinct disk images | `games/kinst/`, `games/kinst2/` |

Console and computer cores, their ROMs, artwork, filters, presets, scripts, fonts and replacement Main binaries are excluded.
Game data follows the existing global Downloader storage preferences; this adapter does not change SD/USB selection.
The ordinary Arcade Organizer continues to handle the regular `_Arcade` tree. This adapter preserves Ongo's own alternatives below `_Ongo/_Arcade`.

## How updates work

The `main` branch contains the converter and policy. A GitHub Actions job checks upstream hourly (scheduled jobs can be delayed),
on converter changes, or when manually dispatched. The generated catalog is published on the `db` branch.
No converter needs to run on the MiSTer. An Update All run uses the most recently published valid catalog.

Only destination paths change for ordinary files. Download URLs, hashes and sizes remain tied to their upstream files.
Core and ROM binaries are downloaded directly from the original sources and are not mirrored in this repository.
The Battletoads MGL is generated with its relocated RBF path and a new hash; its game-file references remain unchanged.

MRAs resolve RBFs in the `cores` directory below the first underscore-prefixed component of their path. For this layout that is
`_Ongo/cores`, even when the MRA is several subdirectories deeper. Required public cores absent from Ongo's catalog are included there
from the standard distribution, JTCORES or Coin-Op Collection. The originals in the normal arcade directory are unaffected.
The adapter uses the same filename prefix/boundary and version-selection rules as MiSTer Main.

The converter verifies every launcher against its source hash and parses all launcher XML before publication.
Unexpected schemas, unsafe paths, destination collisions, conflicting ROM hashes, unresolved new RBF dependencies, or removals
exceeding 10 percent of the previous catalog block publication. A failed build leaves the previous published catalog available.
Existing upstream entanglement metadata is retained, and dependency cores receive stable entanglement IDs so Downloader can retain
the previous version if a replacement download fails. Ordinary upstream removals still follow Downloader's cleanup rules.

The standard Arcade ROMs catalog is checked for duplicate paths. Byte-identical shared entries are left to that database;
different hashes block publication for review. ROM downloads are catalog-driven, not discovered by scanning local MRAs.

## Upstream requirements and limitations

The generated [report.json](https://github.com/Videodr0me/MiSTer_Ongo_Isolated/blob/db/report.json) records exact source manifest hashes,
dependency cores, excluded empty files, manually supplied cores, and ROM archive references absent from both catalogs.
These checks establish catalog consistency; they do not test hardware behavior or verify downloaded ROM contents.

- Ongo's CV1000 MRAs require Raki's separately obtained `ikacore_CV1k` core. Put that RBF in `_Ongo/cores`.
  The adapter does not obtain or publish it, and does not claim ownership of a manually added RBF.
- Some upstream MRAs reference archives not listed by Ongo or the standard ROM database. They remain available, with the missing names recorded in the report.
- Empty upstream RBF placeholders are excluded and reported. An empty core cannot satisfy a launcher dependency.
- MRAs are preserved byte-for-byte. Their existing metadata, gameplay accuracy and development status remain upstream responsibilities.

Ongo recommends the global `file_checking = 'exhaustive'` setting for file migrations between databases.
This adapter does not change that global setting. Read [Ongo's notes](https://github.com/OngoGablogian/MiSTer_Ongo#important-info)
and the [Downloader documentation](https://github.com/MiSTer-devel/Downloader_MiSTer) when choosing it.

## Development

Python 3.10 or newer; standard library only. Keep output and caches outside the source checkout.

```sh
python -B -m unittest discover -s tests -v
python -B scripts/build_database.py --output ../ongo-output --cache ../ongo-cache \
  --previous-url https://raw.githubusercontent.com/Videodr0me/MiSTer_Ongo_Isolated/db/db.json.zip
```

`--bootstrap` is for a reviewed first publication only. Normal automated updates require the existing published database for removal checks.
Publication uses GitHub's repository-scoped workflow token with `contents: write`; no personal access token is stored in the repository.
Content-addressed generated launchers remain on the `db` branch so an older downloaded catalog can still retrieve its launcher bytes.
The final branch update is atomic and rejects concurrent changes.

## Credits

OngoGablogian curates the upstream collection; all original core authors and data providers retain their credits and licenses.
This repository supplies the adapter code and generated catalog only. The adapter code is MIT licensed.
The upstream repository's [license notice](LICENSE.upstream) is retained for the adapted launcher and accompanies the published catalog.

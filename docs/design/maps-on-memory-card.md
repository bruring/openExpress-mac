# Design: maps on the memory card (GPSMAP H1i Plus)

Status: Phases 1–3 implemented in the engine and on the command line. A map's place is read
from both storages; a map installs on the card (`maps install NAME --to card`, the engine's
`maps.install` `"to"`), is updated there and removed from both storages, and moves between
them (`maps move NAME --to internal|card`, `maps.move`), which also resolves a map on both.
On the device, TopoActive Hong Kong & Macao was installed on the card by openExpress with
Garmin Express's layout, loaded and searched by the device (§1.8), then moved to internal storage
and back (§1.9). Phase 4, the Mac app, is implemented: on a device with a card, Install offers
the device or the memory card, an installed map moves from its context menu, and a map on both
storages offers which copy to keep, each confirmed first. Installed maps are grouped by storage:
*On the Device* (blue) and *On the Memory Card* (teal), each with its free space and gauge,
and *On Both Storages* (orange) when a map is on both. It is tested against the mock engine
(`OPENEXPRESS_ENGINE=mock:handheld`); not yet tried with the real device. Upstream 0.13.1 is merged; the read-only probe
(`tools/storage_probe.py`) and the masked fixtures (`tests/data/gpsmap_h1i_plus/`) exist. A map
Garmin Express put on the card (§1.6) and the device's shared map layers (§1.7) answered the
open questions; the routing (§3.1) and the shared-file policy (§3.2) are decided.

First feature: **install Garmin TopoActive maps to the H1i Plus's memory card**, with each map
on exactly one storage (internal *or* card), and **moving maps between the two**.

Sources: `bruring/openExpress-mac`, upstream `TomaszBojanowski/openExpress` 0.13.1, and a
read-only look at a GPSMAP H1i Plus (software 14.08, part 006-B4552-00) through the app's own
engine. Nothing was written to the device.

---

## 1. What the research found

### 1.1 How the code is built

```
openExpress for Mac.app (SwiftUI, macos/openExpress, macos/OpenExpressKit)
   │  JSON Lines over stdin/stdout              docs/ENGINE.md
   ▼
openexpress engine (upstream's Python; openexpress/engine.py → cli.py → maps.py …)
   │  device layer: openexpress/device/mac_storage.py (own MTP client, device/ptp.py, libusb)
   ▼
GPSMAP H1i Plus over MTP
```

- The fork is upstream's Python engine (originally a GTK app for Linux) plus a Mac device layer
  and a SwiftUI front end. Its README says features that aren't Mac-specific go **upstream**.
  Most of this feature is engine work, so plan for it to land upstream too.
- The fork's engine was behind upstream: the released app's engine is 0.11.0, upstream's
  0.13.1. `maps.py`, `storage.py`, `hidden_folders.py` and `cli.py` differed. For example, upstream added `folder_hidden` to maps and a write-routing test
  (`tests/test_write_routes.py`).

### 1.2 Why the app can only write to internal storage

- `mac_storage._storage_with_device_file()` opens **every** MTP storage, keeps the one that
  holds `GARMIN/GarminDevice.xml`, and discards the rest. The others survive only as names in
  `Storage.other_storages`, which `music.py` uses to *refuse* writes.
- Everything above that point (`cli.open_device`, `cli._load_maps`, `maps.install/uninstall`,
  `maps.mark_waiting`, `engine.maps_list`) works on that one `Storage`. The protocol has a
  single `free`, and the Swift Maps page draws one "Device storage" gauge.

### 1.3 The H1i Plus examined (read-only probe)

| | Internal Storage | Memory Card |
|---|---|---|
| MTP store ID | `0x00010001` | `0x00020001` |
| PTP StorageType | `0x0003` FixedRAM | `0x0004` **RemovableRAM** |
| Capacity / free | 61.9 GB / 35.8 GB | 127.8 GB / 127.8 GB |
| Map folder | `GARMIN/` (TopoActive `.img` + `.gma` at its root) | `Garmin/` |
| What's there now | 6 TopoActive regions, base map, DEM | one 42.7 MB `.img`, a `.gpi`, a `.sid`, `SQL/CustomMaps.db`, `http/dl_manager/` (Outdoor Maps+ content the device downloaded itself) |

`GarminDevice.xml` (internal only; the card has none) **declares the card as a map location**:

```xml
<StorageLocationsExtension>   <!-- …/StorageLocations/v2 -->
  <Location Name="Map" MTPId="Memory Card" UMSId="GARMIN CARD"
            SupportedFormat="FAT32" MinSize="4000000000" MaxSize="1099511627776"/>
</StorageLocationsExtension>
```

Some data types also carry a card path (`DataTypeLocationExtension/ExternalPath`):

| Data type | Internal path | ExternalPath (card) |
|---|---|---|
| PreProgrammedNamedMaps, NMaps, TopoActiveDEM (`.img`) | `Garmin` | `Garmin` |
| NamedTrails (`Garmin/SQL/*.db`) | `Garmin/SQL` | `Garmin/SQL` |
| Etyp (`Garmin/SID/*.sid`) | `Garmin/SID` | `Garmin/SID` |
| TopoActiveInstaSearch, TopoActivePhoneticInstaSearch (`Garmin/SID/*.sid`) | `Garmin/SID` | **none** |
| MapRegionGrouping, Clc4Database (`Garmin/SQL`) | `Garmin/SQL` | **none** |

The engine parses none of these extensions today (`xmldevice.parse` reads only Path, BaseName
and FileExtension).

**The XML tells Garmin what is installed, but not where.** Its 122 `<UpdateFile>` entries list
every map file the device has loaded, each with its part number. The card's own
`D7672130A.img` is listed with `Path="Garmin"`, exactly like the internal TopoActive files.
`DA74600.sid` appears twice, in different cases, apparently once per storage. So:

- Garmin's server will most likely report a map on the card as installed (`installationstate`
  1), as it does for internal maps.
- openExpress can't learn a map's storage from the XML. It has to look at both storages'
  files, which the design below does.

**What else is known** (web research; sources in the research notes):

- The H1i Plus has 64 GB internal and microSD "up to 1 TB" (its
  XML: `MaxSize` 1 TiB; the GPSMAP 67 declares 32 GB).
- Garmin Express has a *Manage Maps* screen with *My Maps* and **Removable Storage** tabs that
  can move TopoActive maps to the card. Forum reports say it is buggy: moves that leave the
  card empty, leftover `.img` files, and a map on both storages that Express then could not show
  or delete.
- Garmin's own advice is restrictive. It moves maps to the card mainly when internal storage is
  full.
- On an eTrex 32x, Express put TopoActive on the card in `/Garmin` **together with its `.gma`
  unlock**. That points the routing toward "the unlock sits next to the `.img`" (Q1).
- Downloaded maps are locked to the unit, not the card, so a card moved to another device
  doesn't carry working maps.
- Express "only works with maps it has installed". Maps openExpress puts on the card may not
  show up correctly in Express. Write files exactly where Express would, which Phase 0 shows.
- Forum users report cards over 32 GB being unreliable on these handhelds, though the
  128 GB card is mounted and reports its full capacity.

### 1.4 What Garmin's map service returns

From the answer the app saved for the device (`~/Library/Caches/openExpress/answers`):

- File paths are plain relative paths (`Garmin/D9953060A.img`). There is **no install-location
  field**. Each map has exactly one install option, and the only `externalpath` fields are
  empty ones in `directoriestoremove`. So the client decides where files go; Garmin Express
  must map `Garmin/…` onto the card's `ExternalPath` itself.
- **Maps share large files.** This is the main design constraint:

  | Shared file | Size | Used by |
  |---|---|---|
  | `Garmin/D4712100A.img` (DEM) | 3.38 GB | Canada, US East, US West, Middle America |
  | `Garmin/D4713110A.img` (DEM) | 1.29 GB | Central, Eastern, Western Europe |
  | `Garmin/D9222010A.img` | 1.70 GB | North China ML, South China ML |
  | `Garmin/SQL/D7729090A.db` | 40.8 MB | almost every TopoActive map |
  | `Garmin/SID/DA69900.sid` | 1.7 MB | almost every TopoActive map |

- The largest single file is South America's `D7754120A.img` at 3.95 GB. That is under FAT32's
  4 GiB limit and MTP's 32-bit object size, but close.
- Small maps that share no large file make good first tests: **Greenland** (one 564 MB
  `.img`), **Taiwan** (143 MB: 2 `.img`, 2 `.sid`, plus the shared `DA69900.sid`) and
  **Hong Kong & Macao** (30 MB).

### 1.5 Bugs a card map would trigger in today's code

If a TopoActive map's files ended up on the card (put there by Garmin Express or by hand):

- `mark_waiting()` finds none of its files on internal and marks it **"will be removed after
  the device is unplugged"**.
- `uninstall()` and the delete-first step of an update only look at internal storage.
- `installed_map_version()` reads `.img` headers from internal and finds nothing.
- Updates write the new version to internal, **duplicating** the map.
- Removing a map Garmin Express put on the card (§1.6) would delete only its internal half (the
  `.gma` and its InstaSearch `.sid` files) and leave its `.img` files on the card.

The read side has to know about the card before anything writes to it.

### 1.6 What Garmin Express does with a map on the card

TopoActive Taiwan installed to the memory card with Garmin Express 8 for Mac (snapshots
`0-before` → `2-taiwan`):

| File | Kind (from its header) | Data type | ExternalPath | Written to |
|---|---|---|---|---|
| `Garmin/D9213050A.img`, `Garmin/D9221010A.img` | map | PreProgrammedNamedMaps / NMaps | `Garmin` | **card** `Garmin/` |
| `Garmin/SID/DA69900.sid` (used by almost every map) | `ETYP` (13) | Etyp | `Garmin/SID` | **card** `Garmin/SID/`, *a copy*: the internal one stays |
| `Garmin/SID/DA669020B.sid` | InstaSearch (2) | TopoActiveInstaSearch | none | internal `GARMIN/SID/` |
| `Garmin/SID/DC290000A.sid` | Phonetic InstaSearch (9) | TopoActivePhoneticInstaSearch | none | internal `GARMIN/SID/` |
| `Garmin/D9213050A.gma` (the unlock) | – | none matches | none | internal `GARMIN/` |

- **One rule explains every file:** going to the card, a file goes there exactly when its data
  type has an ExternalPath, under that path; anything else stays on internal. That includes the
  unlock, which no data type declares.
- **Every `.sid` names its kind in its header:** `GARMIN SID`, then a 16-bit kind at byte 14
  (13 ETYP, 2 InstaSearch, 9 Phonetic InstaSearch; ETYP files also carry the word `ETYP` at byte
  0x2C). The path and extension alone (`Garmin/SID/*.sid`) match three data types, so the
  header decides. The engine has every file before it writes, so it can read it.
- **A small shared file is copied, not moved.** The card got its own `DA69900.sid`, and the
  internal one stays for the internal maps. The device already lives with such duplicates: its
  Outdoor Maps+ download left `DA74600.sid` on both storages, and the XML lists both.
- **Garmin's view is unchanged:** after the device took the map in, `GarminDevice.xml` lists the
  two `.img` files (on the card) and the two `.sid` files (internal), all with plain paths. The
  universal map service reports the map as *up to date*, and so does openExpress today. So Q3 is
  answered: a map on the card is an ordinary installed map to Garmin.
- A map "on the card" is therefore split: its map data (`.img`, ETYP, and presumably the
  NamedTrails `.db`) is on the card; its unlock and InstaSearch files stay internal. **On the
  device the split map works:** Taiwan is listed and enabled in its map list, and its search
  finds places, although the search files are on internal and the map on the card.

### 1.7 Shared map layers

- A large `.img` that several regions share is a **map layer of its own** to the device.
  `Garmin/D4712100A.img` (3.38 GB, part 006-D4712-10, version 26.10 in `GarminDevice.xml`) is
  shared by TopoActive Canada, US East and US West (all 2026.11). Its header names it
  "Garmin DEM Map NA 2026.10": the terrain (elevation) layer. The device lists it as
  "TopoActive North America 2026.10", and that entry cannot be removed there.
  - Garmin Express doesn't show it: it isn't a product, just a file the three maps share.
  - Garmin's current answer still names this very file for the 2026.11 maps. It is current,
    not a leftover.
  - The three European maps share `Garmin/D4713110A.img` ("Garmin DEM Map Europe 2026.10") the
    same way. A map whose terrain no other map shares carries its own: Taiwan's
    `D9221010A.img` ("Garmin DEM Map Taiwan 2025.30") went to the card with it.
- Garmin Express offers TopoActive Middle America for the card on its own, while Canada and the
  US maps, which share that layer, stay internal. So Express doesn't make maps that share a
  layer move together either.
- Together with §1.6 (the device takes a map's files from both storages), this answers Q2
  without the 4.5 GB experiment. A map on the card can use a shared layer on internal, so the
  layer stays where it is (§3.2).


### 1.8 A map installed on the card by openExpress

TopoActive Hong Kong & Macao (2026.10, 29.8 MB), `maps install "Hong Kong" --to card --apply`
(snapshots `7-before-hongkong` → `8-after-hongkong`):

| File | Written to |
|---|---|
| `D9220040B.img`, `D9226010A.img` | card `Garmin/` |
| `D9220040B.gma` (the unlock) | internal `GARMIN/` |
| `SID/DA667020B.sid` (InstaSearch), `SID/DC297000A.sid` (Phonetic InstaSearch) | internal `GARMIN/SID/` |
| `SID/DA69900.sid` (ETYP, shared) | internal (rewritten there); the card has its copy from Taiwan |

The layout is Garmin Express's (§1.6). The run showed one mistake, now fixed: the kind of a
`.sid` is at byte 14 of its header, not 16. Read at 16, no kind was known, so every `.sid`
took its safe default, internal storage. The tests now route the first bytes of real `.sid`
files from the device, and on the device the real files route as Express put them.


### 1.9 A map moved to internal storage and back

TopoActive Hong Kong & Macao, `maps move "Hong Kong" --to internal --apply`, then `--to card`
(snapshots `9-before-move` → `10-moved-internal` → `11-moved-back`):

- **To internal:** its two `.img` files (23.9 MB) were copied to internal `GARMIN/`, then removed
  from the card. Nothing else changed. Its unlock and search files were on internal already.
  The card's copy of the shared ETYP file stayed, since Taiwan on the card uses it.
- **Back to the card:** the same two files, the other way.
- **The round trip** leaves both storages exactly as before: no file changed, free space the
  same to the byte.
- **Nothing read or downloaded:** both moves took the files from the download cache, each
  checked against Garmin's MD5 (`map move: … 0 read from the device, 0 downloaded`).


### 1.10 How fast the device writes each storage

From the engine's log of the two moves of §1.9 made in the Mac app. Each wrote the same two
files (23.9 MB) from the download cache, so the time is the write over USB:

| Written to | Time | Speed |
|---|---|---|
| Internal storage | 2.0 s | ≈ 11.7 MB/s |
| Memory card | 6.1 s | ≈ 3.9 MB/s |

Garmin Express shows the same (about 10–11 MB/s to internal, 3–4 MB/s to the card), so the
limit is the device writing its card over MTP, not either program. At 3.9 MB/s TopoActive
Canada's own 2.86 GB takes about 12 minutes; a Mac's card reader writes an SD card at tens of
MB/s.

### 1.11 The card in the Mac's card reader

The H1i Plus's card, taken out and put in the Mac's built-in SDXC reader (read only):

- **The volume:** it mounts under its own label (here `/Volumes/MAPCARD`) as **exFAT**,
  not the FAT32 GarminDevice.xml declares, and the device works with it. Over MTP the filesystem
  reads only as generic, so the FAT32 file limit of §3.2 is a precaution, not a known limit of
  this card.
- **Same files:** the 12 files under `Garmin/` are the device's last MTP listing exactly, by
  name and size. At the top level are `$$SAFE$$` and `System Volume Information`, which the
  device does not show over MTP.
- **macOS adds its own folders on mounting** (`.Spotlight-V100`, `.fseventsd`, about 12 MB);
  the device ignores them.
- **Capacity:** the volume's total (127,832,031,232 bytes) is the capacity the device gives over
  MTP (127,832,162,304) less one allocation block (131,072 bytes).
- **Speed:** reading the 112 MB Taiwan `.img` took 1.2 s, about **94 MB/s**, against about
  3.9 MB/s for the device writing its card over USB (§1.10).
- **The device stays connected with its card out.** Over USB it then lists only its internal
  storage, read as usual (snapshot `13-card-out`). Put back without restarting the device, the
  card is listed again at once, its files as before (`14-card-back`); its free space is 12.6 MB
  less, which is macOS's folders, not shown over USB.
- **Maps while the card is out:** a device that declares a card and has none, or whose card
  cannot be read, has its card's maps as `card_out`. An installed map whose own `.img` files
  are not on internal storage is presumed on the card; it is never taken for removed, and
  nothing installs, updates, moves or removes it until the card is back. The engine's session
  stays open between requests, so a refresh (each request) asks the device for its storages
  again, and a card taken out or put back is seen without reconnecting.

### 1.12 A map moved to the card through the Mac's card reader

TopoActive United States West, moved from internal storage to the card in the Mac app with
**Use Card Reader** (snapshots `15-before-uswest` → `16-after-uswest-reader`):

- **The map moved.** Its own `DB961020A.img` (1.86 GB) went to the card in the reader and was
  removed from internal storage. The North America layer and its search files stayed on internal
  storage; once the card was back, the device listed it on the card.
- **6 min 20 s in all.** Most of that is reading the file off internal storage over USB, at
  about **8 MB/s** whether read whole or in parts (measured on a 208 MB file). Over USB alone the
  move would also write the card at 3.9 MB/s, about 12 minutes. For a move from the device, the
  reader saves the write, not the read; the app's estimate counts both.
- **Two mistakes, now fixed:**
  - The card's volume stayed set on the device's session, which the engine keeps open, so the
    next request asked the free space of the ejected volume and failed. A command now clears it.
  - macOS left `Garmin/._DB961020A.img` (4 KB) on the exFAT card: the "._" file where macOS keeps
    a file's attributes on such a card, which the device might take for a map. A "._" file beside
    a file written is now removed at once, and the card is swept of them before it is ejected.
- **So a move offers where its files come from**, each with its time: copied off the device
  (about 8 MB/s over USB), or downloaded again from Garmin, at the speed openExpress last
  measured a map download (kept after each download of 20 MB or more). Files in the download
  cache take neither, and the faster is chosen by default. Unlike Garmin Express's remove and
  download again, the map stays where it is until its new copy is written and checked.

### 1.13 A map moved to the card in the reader, downloaded again

TopoActive Eastern Europe, moved from internal storage to the card in the Mac app with *Download
again from Garmin* and *Use Card Reader* (snapshots `17-cleaned` → `18-after-europe-east`):

| Step | Time | Speed |
|---|---|---|
| Its own `D6184190A.img` (3.40 GB) downloaded, MD5 checked | 2 min 4 s | 27.4 MB/s |
| Written to the card in the reader, read back and checked | 1 min 10 s | ≈ 48 MB/s |
| In all | 3 min 15 s | |

- Only that file moved; the Europe terrain layer it shares with Central and Western Europe and
  its search files stayed on internal storage. No "._" file was left on the card.
- Over USB alone (read at 8 MB/s, the card written at 3.9 MB/s) it would take about
  22 minutes: about 7 times as long.
- While the card was in the reader, the maps on it were `card_out`; once it was back, Eastern
  Europe was listed on the card. The download speed it measured is kept for the next
  estimates.
---

## 2. Phase 0: settle the open questions on the real device first

The XML and the server answer don't pin down these four questions. A wrong guess means maps
the device doesn't load, or wasted gigabytes, so test them first.

| # | Question | Why it matters |
|---|---|---|
| Q1 | When a map goes to the card, where does each file go: `.img`, DEM, `SQL/*.db`, the `SID/*.sid` files (InstaSearch types have **no** ExternalPath) and the `.gma` unlock? *Answered (§1.6): to the card exactly when its data type has an ExternalPath; the unlock and InstaSearch stay internal. Not yet seen: a `SQL/*.db`.* | Decides the per-file routing rule |
| Q2 | Does the device use a shared file (DEM, `D7729090A.db`, `DA69900.sid`) from internal for a map on the card, or does it need a copy beside the map? Does a duplicate on both storages cause trouble? *Answered (§1.6, §1.7): a small shared ETYP file is copied to the card, and duplicates are normal; a large shared layer is a map of its own to the device and stays where it is.* | Decides whether shared files force maps to move as a group |
| Q3 | Does Garmin's `installationstate` stay `1` for a map on the card, and does activation need anything different? *Answered (§1.6): yes, it stays "installed"; Express activated it as usual and wrote the unlock to internal.* | Status display and updates |
| Q4 | Does the device reject or ignore a card that isn't FAT32 (it declares `SupportedFormat="FAT32"`)? | Refusing, or just warning |

**Done so far:**

- Upstream 0.13.1 merged into the fork (branch `h1i-plus/phase-0`), so the new work starts on
  current code.
- `tools/storage_probe.py`, read only: `snapshot` lists every storage (its StorageType, so the
  card shows as RemovableRAM) with its folders and files into a JSON file, and can save a masked
  `GarminDevice.xml`. `diff` compares two snapshots. `ptp.StorageInfo` now keeps StorageType,
  filesystem and access, which the device always sent.
- Fixtures in `tests/data/gpsmap_h1i_plus/`: the device's `GarminDevice.xml` and Garmin's
  universal-maps answer, with the unit ID, unlock codes, `Ifix`, Connect IQ key and activation
  codes masked. `tests/test_h1i_plus_fixtures.py` pins down §1.3 and §1.4.
- The "before" snapshot: `phase0-snapshots/0-before.json` (local only, ignored by git).

**The experiments** (about one evening, with the device and Garmin Express 8 for Mac, which is
native on Apple silicon). The snapshots go in `phase0-snapshots/` (local, ignored by git); the
probe runs with both apps quit, since each holds the device while it runs:

```bash
probe() { OPENEXPRESS_LIBUSB=/opt/homebrew/lib/libusb-1.0.dylib .venv/bin/python -I tools/storage_probe.py "$@"; }
probe snapshot phase0-snapshots/1-greenland.json      # after each Garmin Express step
probe diff phase0-snapshots/0-before.json phase0-snapshots/1-greenland.json
```

| Step | In Garmin Express (*Manage Maps → Removable Storage*) | Snapshot | Answers |
|---|---|---|---|
| 1 | ~~Install TopoActive Greenland~~: not needed, Taiwan answered Q1 | – | – |
| 2 | **Done.** Install **TopoActive Taiwan** (143 MB: 2 `.img`, 2 InstaSearch `.sid`, the shared `DA69900.sid`) to the card | `2-taiwan` | Q1 and part of Q2: §1.6 |
| 3 | **Done.** On the device, Taiwan is listed, enabled and searchable | – | The split map works (§1.6) |
| 4 | ~~Install TopoActive Middle America to the card~~: not needed (§1.7). Checked instead in Phase 3, when openExpress moves Canada to the card and leaves its shared layer on internal: terrain shading in Canada | – | – |
| 5 | ~~Move a map back with Express~~: not needed, since openExpress moves maps its own way (§3.4) | – | – |
| 6 | Taiwan stays on the card, as the first map of the Phase 1 tests (a map put there by Express) | – | – |

Phase 0 is complete: the routing (§3.1) and the shared-file policy (§3.2) are decided. Q4 (the card's format)
needs no experiment unless a non-FAT32 card turns up: the device mounts the 128 GB card it
has, and over MTP the filesystem reads only as generic (`0x0002`).

---

## 3. Design of feature 1

### 3.1 Engine: the device's storages

- **`StorageLocation` in `xmldevice`.** Parse `StorageLocationsExtension/Location`
  (Name, MTPId, UMSId, SupportedFormat, MinSize, MaxSize) and add
  `DeviceFile.external_path` from `DataTypeLocationExtension/ExternalPath`.
- **`ptp.StorageInfo`:** also unpack StorageType, FilesystemType and AccessCapability (bytes
  0–5, read today but thrown away). Read-only cards and `0x0004` RemovableRAM become visible.
- **The device's other storages** (implemented): the storage with GarminDevice.xml keeps the
  others in `Storage.others`, each with its `description` and `removable`. On the Mac,
  `_storage_with_device_file` keeps them (`MacStorage.adopt`); on Linux, `gio_storage` keeps
  the storages' folders. `openexpress/device/card.py` finds the card: `card_of` takes the
  storage whose description matches the XML's `MTPId`, else the only removable one, and
  `card_paths` gives where a file may be on it. Every caller keeps passing one storage, so no
  new type runs through the code.
  - **The card's `MacStorage` shares the session's lock** (`adopt`), its stall and its closing.
    A listing on the card can't start inside a transfer on internal, a stall on one stops both,
    closing the card alone leaves the session open, and a refresh drops what was listed on both.
    Tested in `tests/test_mac_storage.py`.
  - A card pulled mid-operation shows up as `InvalidStorageID` (0x2008). Report it as "the
    memory card was removed", not as the device not responding.
  - Linux (`gio_storage`, already finds `candidates`) gets the same treatment for upstream.
    Mass-storage mode stays out of scope: maps are refused there anyway.
- **Routing:** `place(path, kind, location) -> (Storage, real_path)`, one pure function, as
  Garmin Express does it (§1.6). For the card: a file whose data type has an `ExternalPath` goes
  to the card under that path; every other file (the `.gma` unlock, InstaSearch `.sid`) goes to
  internal, whatever the location. The data type comes from the path and extension
  (`DeviceFile.matches`); where those match more than one type (`Garmin/SID/*.sid`), from the
  kind in the file's header (`GARMIN SID`, kind at byte 14), read from the downloaded or cached
  copy before writing. An unknown kind goes to internal. Every write, delete and listing of a map
  file goes through it. It gets table tests like upstream's `test_write_routes.py`, from the
  Taiwan case of §1.6.
  - The most specific data type wins. `Garmin/gmapbmap.img` matches PreProgrammedNamedMaps
    (any `.img` in `Garmin`, with an ExternalPath) and BaseMaps (BaseName `gmapbmap`, none).
    The one with a BaseName decides, so the base map stays internal. Phase 1's `card_paths`
    only says where to *look* for a file on the card, so there every candidate is kept.

### 3.2 Engine: where a map is

- Add `UniversalMap.location: "internal" | "card" | "both" | None`, set in `mark_waiting()`,
  which now lists the map folders of **both** storages.
  - A map's location is where its **movable** files are: its own files (those other maps don't
    share, `protected_files`) that routing may put on the card, i.e. its `.img` files. Its unlock
    and InstaSearch files are always internal and say nothing about its location. `"both"`
    means its `.img` files are on both storages: an interrupted move, or something Garmin Express
    or the user did. It is shown as a problem with a fix.
  - Waiting, removal, partial-write and installed-version checks all use the map's own location.
    This fixes the bugs in §1.5.
- Shared files follow **one policy function**, decided from §1.6 and §1.7:
  - **A shared `.img` layer stays where it is.** It is never copied and never moves with one
    of its maps; a map on the card uses it from internal, and the other way round. It moves only
    with its last user (when every map that uses it ends up on one storage, it follows them
    there, or stays put if it is already there). It is written only when no storage has it yet:
    then to the storage of the map being installed. It is removed only with its last user.
    Installing TopoActive Middle America to the card, for instance, writes its own 1.08 GB
    `.img` and leaves the 3.38 GB layer on internal.
  - **Other shared files (ETYP `.sid`, `SQL/*.db`) are copied** to each storage that has a map
    using them, as Express does. A copy is removed only when no map on its storage uses it.
  - Not chosen: placement groups (maps that share a layer must share a storage), which Express
    doesn't do either and which would make moving one region move three; and duplicating large
    layers on both storages, which wastes gigabytes.
  - So "one map, one storage" is about a map's own `.img` files. Shared layers belong to no
    single map, and the Maps page says so: "TopoActive North America 2026.10: terrain shared by
    Canada, US East and US West, on Internal Storage". They are never offered for removal on
    their own.
- Space is checked on the **target** storage, plus `SPACE_MARGIN`. Each file is checked against
  FAT32's 4 GiB limit when the target is the card.

### 3.3 Enforcing one location per map

| Action | Rule |
|---|---|
| Install | Needs a target when a card is present: **internal by default**, card on request. Refused if the map's own files are already on the other storage, with a pointer to Move instead. |
| Update | Always in place: a map on the card is updated on the card. |
| Remove | Removes from wherever the map is. Shared files used by other maps are kept, as today. |
| `"both"` state | No install, update or move until resolved: "Keep on card" or "Keep on device" checks the kept copy (size and MD5 against Garmin's answer), then deletes the other. |

### 3.4 Move (internal ↔ card)

The order is **write and verify the new copy first, delete the old one last**, so an
interruption never leaves a map in neither place:

1. **Pre-flight:** card present and writable, map not waiting, not restricted, not `"both"`;
   enough space on the target; FAT32 per-file limit.
2. **Get the bytes without asking Garmin**, in this order: the map cache if the MD5 matches
   (`maps.cache_file`); else read the file from the source storage over MTP into the cache and
   check its MD5 against Garmin's answer; else download it (the existing `_download`).
3. **Write to the target** with the existing `_write_checked` (it reads each file back).
4. **Unlock and search files stay put.** The `.gma` and the InstaSearch `.sid` files are on
   internal wherever the map is (§1.6), so a move neither copies them nor asks Garmin to
   activate the map again. A small shared file (ETYP) gets a copy on the target if none is
   there yet.
5. **Delete the map's own movable files from the source** (and a shared copy there that no
   other map on that storage uses), then `refresh` and `mark_waiting`. Status: "moved; the
   device takes it in once unplugged".
6. **Cancel or failure:** before step 5, remove the partial target files and leave the source
   untouched. During step 5 the state is `"both"`, which §3.3 already resolves.

The whole move runs under `awake.kept_awake`, with progress phases `reading`, `copying`,
`checking` and `removing`.

**As implemented** (`maps.move_plan`, `maps.move`):
- **One command for both cases.** Resolving a map on both storages is `maps move --to` the
  storage to keep, so there is no separate `maps resolve`. A copy already where the map goes is
  checked against Garmin's MD5 before the other copy is removed; one that fails stops the move
  with nothing removed.
- **Only an up-to-date map moves.** The device's files must be the ones Garmin's answer names,
  so its checksums prove each copy. A map with an update is updated first.
- **Copy, not remove and re-download.** Garmin Express is reported to move a map by removing it
  and downloading it again, onto the other storage. openExpress copies the files the device
  has, each checked against Garmin's MD5 (a file that fails is downloaded again). The result
  is the same files in the same layout, without the download, and without the time when the
  map is on neither storage. The unlock stays where it is in either case, as §1.6 and §1.8
  show it does for a fresh install.

### 3.5 Command line and engine protocol

- `openexpress maps` gets a *where* column. New commands, dry run unless `--apply`:
  - `maps install NAME --to card|internal`
  - `maps move NAME --to card|internal` (also keeps a map on both storages on one alone)
- `docs/ENGINE.md`, with `PROTOCOL` bumped:
  - `maps.list` gains `storages: [{"id": "internal"|"card", "name", "free", "capacity",
    "removable", "format"?}]`, and each Map gains `location` and `can_move`. `free` stays,
    meaning internal, for older apps.
  - `maps.install` takes `"to"`, default `"internal"`. A map already on the other storage gives
    the error code `on_other_storage`.
  - New `maps.move {device, maps, to, apply}` and `maps.resolve {device, map, keep, apply}`,
    with results shaped like `maps.install`.

### 3.6 Mac app (SwiftUI)

- **OpenExpressKit:**
  - `DeviceMap.location`, `DeviceMap.canMove`, `MapsList.storages` (tolerant decoding, as the
    models already use).
  - `EngineClient.maps(.install, names, to:)` and `.move`.
  - `InstallPlan.freeAfter` per storage.
  - `MockEngine` gets a device with a memory card, plus a `mock:card` state, for previews and
    tests.
- **Maps page** (`MapsAppsBackupsPages.swift`):
  - Two capacity gauges, *Device* and *Memory Card*.
  - Each installed map shows a location badge.
  - **Install** becomes a menu: *Install on Device* / *Install on Memory Card*. Each item is
    disabled with a tooltip when the map doesn't fit there.
  - The context menu gains *Move to Memory Card…* / *Move to Device…*. The confirmation names
    the sizes, any maps that move with it (policy B), and that the change takes effect after
    unplugging.
  - A `"both"` map shows a warning row with *Keep on Card* and *Keep on Device*.
  - Card content the device manages itself (Outdoor Maps+, e.g. SkiView) is listed
    read-only and never touched.

### 3.7 Tests

- **Python:**
  - Extend `tests/ptp_fake.py`, which already models `extra_storages`, so the second storage
    can hold files.
  - Fixtures: the masked H1i Plus XML and the saved universal-maps answer.
  - Cases: XML extension parsing; storage matching (by MTPId, by RemovableRAM, no card); the
    routing table; location detection including shared files and `"both"`; `mark_waiting`
    across storages (the §1.5 regression); install to the card; update in place; move, with an
    interruption at each step; per-storage space refusal; the FAT32 limit; card removed
    mid-write; the shared-session lock.
- **Swift:** protocol decoding of `storages`, `location` and `maps.move`; view-model plans.
- **On the H1i Plus:**
  1. Phase 1: Taiwan, which Garmin Express put on the card, reads as *on the card*; its
     unlock and InstaSearch files on internal don't make it *both*.
  2. Phase 2: Hong Kong & Macao installed to the card (§1.8; the layout is Express's). Still to
     check: unplugged, the device lists it and its search finds a place.
  3. Phase 3: Hong Kong & Macao moved to internal and back, the storages as before (§1.9). Still
     to try: Canada moved to the card (only its own 2.86 GB `.img` moves, read from the device
     as it is not in the cache; the North America layer stays on internal), then on the
     device, Canada still with terrain shading.
  4. A move cancelled halfway, then resolved.
  5. A card map updated when Garmin next publishes TopoActive updates.


### 3.8 The card in the computer's card reader

When an install or move to the card would take long over USB, offer to write the card in the
Mac's card reader instead.

- **When:** before an install or move to the card, the time at the card's speed over USB is
  estimated: the speed last measured on this device (§1.10 for the H1i Plus; what a transfer
  to the card measures is kept per device), else 3.5 MB/s. Above about two minutes, the app
  asks: *Write over USB* (the time) or *Use the Card Reader* (about a minute).
- **Which volume is the card:** a mounted removable volume with a `Garmin` folder whose capacity
  is the card's capacity over MTP within one allocation block, and whose `Garmin` files include
  those the device last listed on its card. The card's identity is kept per device when it is
  seen over MTP. A volume that does not match is never written; the app says why.
- **Writing:** the map's card files go to the volume (a LocalStorage as the card: the same
  routing, plan, space and checks), the unlock and search files to internal storage over MTP,
  so the device stays connected with its card out. Then the volume is ejected
  (`diskutil eject` on macOS, `gio mount --eject` on Linux) and the app asks for the card to be
  put back; the device takes the map in
  once the card is back and the device unplugged.
- **Moves from the card** read it in the reader too.
- **The device stays connected** with its card out (§1.11), so the card files and the internal
  files are written in one go; the maps on the card are `card_out` meanwhile, and the card is
  seen again as soon as it is back.

---

## 4. Phases

| Phase | Work | Ends with |
|---|---|---|
| 0 (done) | Merge upstream 0.13.1; probe and fixtures; Garmin Express experiments | Q1–Q4 answered, shared-file policy chosen |
| 1 (done) | Read side: `DeviceStorages`, XML extensions, `StorageInfo` type, `location`, `mark_waiting` across storages, `maps` *where* column, `maps.list` storages | The app shows where each map is; §1.5 bugs fixed; nothing new writes |
| 2 (done) | Install to the card: routing, per-storage space, `--to`, `maps.install` `to` | `maps install Greenland --to card --apply` works on the device |
| 3 (done) | Move and resolve: §3.4 sequence, cache/MTP/download fallback, `maps.move`, `maps.resolve` | Round-trip move verified on the device |
| 4 (done) | Mac UI: gauges, badges, install menu, move and resolve actions, mock card device | Feature usable from the app |
| 5 (proposed) | Upstream engine pieces (Linux gio parity, translations with `TRANSLATORS:` comments) to openExpress; Mac pieces to the fork; add H1i Plus to `docs/devices.md` | Merged in both repos |
| 6 (done) | The card in the computer's card reader (§3.8): the offer by the time over USB, the card's identity, writing it as a mounted volume, ejecting it | US West moved to the card in the reader (§1.12), Eastern Europe downloaded again into it (§1.13) |

Phases 1–3 are engine work and can be tested from the command line before any Swift changes.
Phase 1 is worth shipping alone: it fixes real bugs and shows what's on the card.

## 5. Risks

- **The shared PTP session** (§3.1): the most likely source of hard-to-reproduce failures. Share
  the lock and test it under stress (`tools/stress_device.py` already exists).
- **Shared files** (Q2): the wrong policy wastes gigabytes, or leaves maps without terrain or
  search. Decide it from measurement.
- **Licensing:** unchanged. Only maps Garmin's server offers this device are installed. A move
  copies existing files and unlocks, or re-activates through the same service Garmin Express
  uses. No unlock is ever made up.
- **The device's own files on the card** (Outdoor Maps+, `dl_manager`): only touch files that
  Garmin's map answer names.
- **Fork drift:** merge upstream first, and keep the engine changes upstream-shaped so later
  merges stay easy.

## 6. Later H1i Plus work (after feature 1)

- **Maps of your own on the card:** `ownmaps` lists both storages (KMZ in
  `Garmin/CustomMaps`, BirdsEye `.jnx`, `.gpi` POI).
- **Handheld data:** GPX waypoints, tracks and routes (`Garmin/GPX`, `Garmin/NewFiles`),
  geocaches (`GGZ`, `geocache_logs.xml`). The `courses` module already writes `.GPX`.
- **Backups and Device Status** cover the card, optionally, with storage by kind for both.
- **Device report** for the H1i Plus in `docs/devices.md`, upstream and in the fork.

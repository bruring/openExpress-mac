# openExpress for Mac

openExpress for Mac (the repository openExpress-mac) is the macOS edition of [openExpress](https://github.com/TomaszBojanowski/openExpress):
a native app for Apple Silicon Macs that replaces Garmin Express. It keeps openExpress's Python
code as its engine, adds a macOS device layer to it, and puts a SwiftUI app on top.

Not affiliated with Garmin. Free software under the GNU GPL v3.0 or later, like openExpress.

## Why

Garmin Express 7 for Mac was an Intel-only app, running under Rosetta 2, which macOS 27 is the
last release to fully support. It also hung when it installed maps or software: it switched the
device into a special USB mode, failed to reach it there, and only fell back after about two
minutes ("Reconnect the device to continue"). Garmin Express 8 (a fresh download:
7 does not update to it) is native on Apple silicon, and dropped ANT+.

openExpress for Mac does not depend on either: it installs maps and software as plain files
over MTP, so that USB mode is never needed; it keeps backups of the device on your Mac, shows
your activities with their maps and charts, and is free software without telemetry. Quit
Garmin Express while you use it: whichever version, it holds the device.

## Status

| | |
|---|---|
| Engine on macOS (no GTK, no PyGObject) | works; tested read-only on a fēnix 8 |
| Device info, backup, update and map checks | work on a real device |
| Installing and removing maps, sync | tested from the command line on a fēnix 8; from the Mac app, not yet |
| Devices connected as a USB drive (mass storage) | work on a real device (Edge 820): info, files, activities, backup, an update installed, a sync's dry run; maps, apps and music are refused, as on Linux |
| Mac app | runs against the engine; under development |
| Installer (.pkg, no Homebrew needed) | released: signed with a Developer ID and notarized |

Devices tested on the Mac: fēnix 8 47 mm AMOLED and Edge 1040 (MTP), Edge 820 (USB drive), Venu 2S
(MTP, read only). The upstream project lists the devices it was
tested with in [devices.md](devices.md).

## How it fits together

```
openExpress for Mac.app (SwiftUI)
   │  JSON Lines over stdin/stdout          docs/ENGINE.md
   ▼
openexpress engine (Python, upstream's code)
   │  device layer: openexpress/platform, openexpress/device/mac_storage.py
   ▼
MTP over libusb  ──►  the watch or bike computer
(a USB drive: its folder under /Volumes, found with ioreg and diskutil)
```

- **Device access** goes through openExpress's own MTP client (`device/ptp.py`) over libusb,
  not through GIO/gvfs as on Linux. A device another program holds (Garmin Express Service,
  Android File Transfer, …) is never taken from it: openExpress says which program to quit.
- **Devices in mass storage mode** (older Edge firmware, handhelds, some watches, car
  navigators) are mounted by macOS itself under `/Volumes`. openExpress finds a drive by the
  disks below its USB device in the IORegistry (`ioreg`), asks `diskutil info` where macOS
  mounted it, and reads and writes the files there. After it wrote to a drive (a sync that
  received files, an update) it ejects it with `diskutil eject`, so that the device takes the
  files; until it is unplugged an ejected drive is not opened again. A drive shows up once
  macOS has mounted it; one the user unmounted is mounted again (`diskutil mount`) only when
  it is chosen.
- **ptpcamerad**, the macOS service that grabs cameras and phones for Image Capture and Photos,
  is paused only while openExpress holds a device and resumed afterwards, even after a crash.
- **Secrets** (the Garmin sign-in, the app key, Strava tokens) live in the macOS Keychain.
- **Folders:** settings and data in `~/Library/Application Support/openExpress`, caches in
  `~/Library/Caches/openExpress`, the log in `~/Library/Logs/openExpress`.

## What this edition adds

- The Mac app: devices in a sidebar, pages for Overview, Activities (route map and charts),
  Updates, Maps, Apps and Backups, a menu bar item, notifications, and an automatic backup and
  sync when a device is connected. Updates are never installed without asking. The Overview
  also has the readings of the Garmin Connect account the Linux window shows on a device's
  page: the sensors' batteries and, for a watch that charges from the sun, its solar charging
  (`account.readings`), for a remembered device too.
- [The engine protocol](ENGINE.md) (`openexpress engine`), which the app uses.
- Incremental backups: a file that has not changed since the previous backup is hard-linked to
  it instead of being read from the device again.
- A FIT drop folder: new activity files are copied to a folder of your choice after each sync,
  for other apps to pick up (`setup --copy-fit-to DIR`).
- Strava: upload new activities with your own Strava API app ([guide](guide.md#strava)).
- No battery level: Garmin devices answer MTP's battery question with a fixed 50 % at any
  charge, so none is shown.

## Install

`openExpress-for-Mac-<version>.pkg` installs `openExpress for Mac.app` into `/Applications`. It needs a Mac
with Apple silicon and macOS 26 or later, and nothing else: the app carries its engine (its
own Python, libusb and 7-Zip). Quit Garmin Express first (also its "Garmin Express Service"
in the menu bar): it holds the device while it runs. Then, once, in the app or in Terminal:

```bash
engine="/Applications/openExpress for Mac.app/Contents/Resources/engine/bin/openexpress"
"$engine" setup --from-installer GarminExpress.exe   # Garmin's Windows installer, once
"$engine" login --manual
```

The released package is signed with a Developer ID and notarized, so it opens as it is. A
package you build yourself without a Developer ID is signed ad hoc: a downloaded copy of it
macOS refuses to open ("cannot verify that it is free of malware"): Control-click it > Open,
or System Settings > Privacy & Security > Open Anyway. On the Mac that built it, it opens.

**Keychain:** the sign-in and the app key are Keychain items that only the program which saved
them may read without asking. The first time the app's engine reads items an earlier engine
saved (the development venv, or an ad-hoc build before this one), macOS asks whether
"python3.13" may use them: Always Allow. Builds signed with the same Developer ID are
recognised as the same program, so this is asked once; ad-hoc builds are each a new program
and ask again after every update.

## Setting up the engine for development

From a checkout, without the package:

```bash
brew install libusb p7zip
uv venv -p 3.13 ~/.venvs/openexpress-mac
uv pip install -p ~/.venvs/openexpress-mac requests mutagen
uv pip install -p ~/.venvs/openexpress-mac --no-deps -e .
openexpress=~/.venvs/openexpress-mac/bin/openexpress
$openexpress setup --from-installer GarminExpress.exe   # Garmin's Windows installer, once
$openexpress login --manual                             # in a Terminal window
$openexpress info                                       # with the device connected
```

Quit Garmin Express first (also its "Garmin Express Service" in the menu bar): it holds the
device while it runs.

## Building the app

```bash
brew install xcodegen
cd macos
xcodegen generate                      # writes openExpress.xcodeproj from project.yml
(cd OpenExpressKit && swift test)      # engine client, models and view models
xcodebuild -scheme openExpress -configuration Debug -derivedDataPath build/DerivedData build
OPENEXPRESS_ENGINE=mock "build/DerivedData/Build/Products/Debug/openExpress for Mac.app/Contents/MacOS/openExpress"
```

`OPENEXPRESS_ENGINE` chooses the engine:
- a path to `openexpress`;
- `mock`, an engine in memory with no device or network (`mock:busy`, `mock:firstRun`,
  `mock:signedOut` and `mock:empty` show other states; `mock:handheld`, a GPSMAP H1i Plus with
  a memory card: a map on the card and one on both storages);
- unset: the engine inside the app (`Contents/Resources/engine/bin/openexpress`), then
  `~/.venvs/openexpress-mac/bin/openexpress`.

The Xcode scheme sets it to `mock`.

## Building the package

```bash
brew install xcodegen gettext libusb   # build-time only; the package needs none of them
macos/scripts/build-pkg.sh             # → macos/build/openExpress-for-Mac-<version>.pkg
```

On an Apple silicon Mac with Xcode 26 or later. The script:

1. builds the app (Release, arm64) with xcodegen and xcodebuild, its version taken from
   `openexpress/__init__.py` (`CFBundleShortVersionString`; the build number is the count of
   commits);
2. puts the engine into `Contents/Resources/engine`: CPython 3.13 from
   [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
   (`install_only_stripped`, the release and SHA-256 pinned in the script), openexpress built
   from the checkout (its translations made from `po/`), and `requests` and `mutagen` with
   their dependencies, pinned by hash in `macos/packaging/engine-requirements.txt`; pip,
   tkinter/Tcl/Tk, IDLE and headers are left out, and the bytecode is compiled;
3. adds `engine/bin/openexpress` (`macos/packaging/openexpress-launcher.sh`): it runs the
   bundled Python isolated from the user's environment (`-I -B`, no `PYTHONPATH`, no `DYLD_*`),
   with `OPENEXPRESS_LIBUSB` set to the app's libusb and `OPENEXPRESS_7Z` to 7-Zip's `7zz` in
   `engine/bin`. Every path is found from the script's own place;
4. copies Homebrew's libusb into `Contents/Frameworks` (install name `@rpath/…`) and 7-Zip's
   own macOS build (pinned) into `engine/bin`;
5. fails if any machine code in the app refers to Homebrew, `/usr/local`, a venv or a path of
   the build machine, is not arm64 or needs a macOS later than 26;
6. writes `THIRD_PARTY_NOTICES.txt` (each component, its licence, its source, the licence
   texts), `LICENSE.txt` and `SOURCE.txt` (the written source offer) into `Contents/Resources`;
7. signs (below), makes the package (`pkgbuild` with a non-relocatable component, so that
   it never "updates" a copy of the app elsewhere; `productbuild` with a welcome page, the
   GPL, an arm64-only host and a macOS 26 check with a clear message);
8. runs `macos/scripts/verify-pkg.sh`, which checks the package without installing it:
   `pkgutil --expand-full` into `macos/build/verify/`, the signature (`codesign --verify
   --deep --strict`), the machine code, the engine run with a scrubbed environment and no
   Homebrew on `PATH` (`hello`, `settings.get`, `devices.list` — read-only; that libusb is the
   bundled one, seen in the process and through `dladdr`), and that the bundled `7zz` is used
   and works. `VERIFY_ACCOUNT=1` adds `account.status` (reads the Keychain: see above);
   `VERIFY_LAUNCH=1` starts the expanded app for ten seconds and checks that it ran its own
   engine.

Downloads are cached in `macos/build/cache/`; everything the script makes is under
`macos/build/` (ignored by git). CI builds the same package on a GitHub-hosted Apple silicon
runner: `.github/workflows/macos-pkg.yml`, the package as the artifact `openExpress-for-Mac-pkg`.

### Signing and notarization

Without anything set, every piece of code is signed ad hoc and the package is not signed. With
a Developer ID (a paid Apple Developer Program membership):

```bash
# Once: the two certificates ("Developer ID Application", "Developer ID Installer") in the
# login keychain (Xcode > Settings > Accounts > Manage Certificates), and a notary profile:
xcrun notarytool store-credentials openexpress-notary \
    --apple-id you@example.com --team-id TEAMID --password <app-specific password>

RELEASE=mac.1 \
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
DEVELOPER_ID_INSTALLER="Developer ID Installer: Your Name (TEAMID)" \
NOTARY_PROFILE=openexpress-notary \
macos/scripts/build-pkg.sh
```

Then every Mach-O (the Python, libusb, 7zz, the app) is signed inside out with the hardened
runtime and a secure timestamp, the package is signed with the installer identity, submitted
with `xcrun notarytool submit --wait`, stapled (`xcrun stapler staple`) and checked by
Gatekeeper (`spctl -a -vv -t install`). In CI the same comes from repository secrets (listed
at the top of the workflow).

Entitlements (`macos/packaging/engine.entitlements`, the engine's Python only; the app and
7zz have none beyond the app's existing non-sandbox one):
- `com.apple.security.cs.disable-library-validation`: the engine loads libusb with `dlopen`
  (ctypes). Signed with the same Team ID it would pass library validation anyway; the
  entitlement is a margin in case a library is ever signed otherwise, and it is what lets the
  hardened runtime be tried with ad-hoc signatures (`ADHOC_HARDENED=1`, which has no Team ID:
  without it, the Python refuses the app's own libusb). It can be dropped once a notarized
  build is confirmed to work without it.
- not `allow-jit` or `allow-unsigned-executable-memory`: CPython 3.13 is built without its JIT,
  and ctypes on Apple silicon makes callbacks with libffi's prebuilt trampolines (the engine
  defines none anyway).
- not `allow-dyld-environment-variables`: the launcher removes `DYLD_*` anyway.

`ADHOC_HARDENED=1 macos/scripts/build-pkg.sh` signs ad hoc with the hardened runtime and these
entitlements, and verifies the result: the nearest to a distribution build without a
certificate.

Before a first Developer ID release: confirm that a notarized build works on a Mac that never
had a development build (the Keychain prompt above, the engine starting from
`/Applications`), and whether the CI runner's Xcode builds the app (it must have the macOS 26
SDK; the workflow stops with an error otherwise).

## Tests

```bash
XDG_DATA_DIRS=/opt/homebrew/share:/usr/share \
  OPENEXPRESS_TESTS_NO_DISPLAY=1 ~/.venvs/openexpress-mac/bin/python -m pytest -q
```

`XDG_DATA_DIRS` lets xgettext find AppStream's rules for the metainfo (`brew install appstream`):
without them the tests of the translation template fail, and `tools/update-template.sh` refuses
to make one. After a merge of upstream, make the template again with `tools/update-template.sh`
(it keeps the Mac's texts), but take `po/pl.po` as git merges it: Polish covers upstream's texts
only, and `tools/update-translations.sh` would fill it with guesses for the Mac's.

On macOS the tests get a home folder of their own (they never touch `~/Library`), never reach a
real USB device or the Keychain, and skip the ones that need PyGObject (the Linux window).

## Branches and upstream

- `main` is openExpress-mac: upstream's code with the macOS work on top.
- Upstream's changes come in by merging `upstream/main` into `main`.
- Changes that are useful on every platform (the device layer, portability fixes) are offered
  to upstream as pull requests from branches based on `upstream/main`.

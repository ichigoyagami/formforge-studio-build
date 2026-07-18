# FormForge Studio source build

This repository builds **FormForge Studio**, a genuine Windows executable compiled from the Blender open-source engine. It is not the earlier script wrapper and does not install a stock Blender download.

The build pins the engine to Blender source commit `96ae7ff32d6061a53d4e9ba294e5436a94e58332`, applies the reviewed FormForge source patch, compiles `FormForge.exe`, smoke-tests it, and packages both:

- `FormForgeStudio-Setup.exe` — per-user Windows installer
- `FormForgeStudio-Windows-x64.zip` — portable build

## Download the installer

1. Open the **Actions** tab.
2. Open **Build FormForge Studio for Windows**.
3. Open the newest successful run.
4. Download the `FormForgeStudio-Windows` artifact.
5. Unzip the artifact and run `FormForgeStudio-Setup.exe`.

The first compile is large and may take a few hours on a GitHub-hosted Windows runner.

## What is changed

- The compiled executable and launcher are named FormForge.
- Windows executable metadata and icon are FormForge-branded.
- Blender's first-run Quick Setup is disabled.
- FormForge UI 0.19 is bundled and enabled at first startup; users do not install it separately.
- A Maya-inspired top menu, viewport menu, shelf, Channel Box, Modeling Toolkit and Industry Compatible navigation are enabled at startup.
- The FormForge splash and Windows application icon are compiled into the build.
- `.forge` is registered as the FormForge project extension. Existing `.blend` scenes remain openable for migration and compatibility.
- Workspaces are renamed for the FormForge workflow.
- **Windows > Developer Log** opens the in-app development log.
- **Windows > Export Diagnostics** writes a portable diagnostic report.

## Licensing

FormForge Studio is a Blender source derivative and remains licensed under the GNU General Public License. The installer includes the engine's `COPYING` file as `GPL-license.txt`. Autodesk Maya is not included; FormForge only provides an independently implemented Maya-inspired workflow.

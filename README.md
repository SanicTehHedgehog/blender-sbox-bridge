# Blender s&box Bridge — Blender Addon

Real-time bidirectional scene sync between **Blender** and the **s&box editor**.

Model, transform, texture, and map from Blender directly to the s&box scene editor.

## Installation

### Requirements
- **Blender 4.2+**
- **s&box** with the [Blender Bridge library](https://sbox.game/kamishell/blender_bridge) installed

### Install via Blender Extension Repository (auto-updates)

1. In Blender: **Edit > Preferences > Get Extensions**
2. Click the **Repositories** dropdown (top-right) > **Add Remote Repository**
3. URL: `https://sanictehhedgehog.github.io/blender-sbox-bridge/index.json`
4. The addon appears in the extension browser — click **Install**
5. Updates are automatic from now on

### Manual Install (alternative)

1. Download `sbox_bridge.zip` from the [latest release](https://github.com/SanicTehHedgehog/blender-sbox-bridge/releases)
2. In Blender: **Edit > Preferences > Add-ons > Install from Disk**
3. Select the downloaded `sbox_bridge.zip`
4. Enable **"s&box Bridge"** in the addon list

### Connect to s&box

1. Open your s&box project — the bridge server starts automatically on port `8099`
2. In Blender, open the **N-panel** (press `N`) > **s&box** tab
3. Check your **Assets Path** — it is auto-detected from the running project when possible; set it manually if empty (needed for materials)
4. Click **Connect** — connecting is instant; press **Sync All** when you want to push the whole scene

## Features

- **Mesh streaming** — Create/edit meshes in Blender, they appear in s&box in real time. The bridge sends the *evaluated* mesh, so modifiers, booleans, subdivision, and geometry-nodes output all carry over
- **Sync modes** — Bidirectional (default), Export Only (s&box never overwrites Blender data), or Manual Only
- **Bidirectional transforms** — Move objects in either editor
- **Visibility sync** — Hiding an object in Blender disables its GameObject in s&box (it comes back when you unhide — hiding never deletes)
- **Delete safety** — Blender-side deletes wait 30 seconds before confirming to the engine (undo cancels them). Engine-side deletes never destroy your Blender objects: they move to a hidden **Bridge Trash** collection with one-click Restore
- **Light sync** — Point, Spot, and Sun lights sync between Blender and s&box
- **PBR materials** — Principled BSDF nodes auto-generate `.vmat` files with textures, including emissive
- **Near-real scale presets** — Scale Factor 40 (default) pairs with grid 40 so one grid cell is exactly 1 m and a 2 m wall is 80 units next to the 72-unit player; presets `40 / 32 / 16` (this one can be strange scale is 40 for sizing issues feel free to change but for mapping this scale works well, once started do not change scale work in 16 grid, s&box will update as well.)
- **Player Reference** — One click drops a Citizen-scale reference at the 3D cursor (uses your own Citizen FBX if configured, wireframe box otherwise)
- **Grid sync** — Bridge `grid_size` mirrors `Gizmo.Settings.GridSpacing` in s&box and the viewport overlay grid in Blender. Preset buttons `1/2/4/8/16/32/64/128`, `[` / `]` to halve / double, plus an Align Snapping button that matches Blender's snap to the overlay grid
- **Chunked transfer** — Large meshes (20k+ vertices) stream in chunks without freezing
- **Auto-reconnect** — Connection recovers automatically if s&box restarts; a sync health line shows the live state
- **Send to Scene** — Manually push selected objects with one click

## Panel Overview

The addon adds an **s&box** tab to Blender's N-panel with:

| Section | Description |
|---------|-------------|
| **Connection** | Connect/disconnect, host/port, sync health line |
| **Sync** | Sync Mode, Auto Sync, Sync on Connect, Scale Factor + presets, grid controls, Assets Path, Auto Reconnect, Sync All / Force Resync / Send to Scene |
| **Bridge Objects** | Per-object sync status, problems listed first |
| **Info** | Synced object count, latency, play mode indicator |
| **Pending Deletes** | 30-second window to cancel outbound deletes; Bridge Trash restore for engine-side deletes |
| **Warnings** | Unsupported-feature and engine-rejection warnings |
| **Materials** | Preview and manage generated `.vmat` files |

## Supported Object Types

| Blender Type | s&box Result | Notes |
|-------------|-------------|-------|
| Mesh | MeshComponent | Full geometry + per-face materials |
| Point Light | PointLight | Color, radius |
| Spot Light | SpotLight | Color, radius, cone angles |
| Sun Light | DirectionalLight | Color |
| Area Light | — | Not supported (warning shown) |
| Curve/Surface/Meta/Text | — | Not synced — convert to mesh first (Object > Convert), or use Curve-to-Mesh geometry nodes on a mesh object |

## What syncs — and what doesn't

Because the bridge sends the evaluated mesh, almost any *geometry* workflow survives the trip. The current known limits:

- **Geometry nodes**: instanced output (Instance on Points, collection instancing) must end in a **Realize Instances** node, or the instances silently won't sync
- **Collection instances** (linked duplicates of collections) are not synced — make them real or use Realize Instances
- **UVs**: the **active UV layer** only
- **Materials**: Principled BSDF with **Image Texture** inputs (base color / roughness / metallic / normal) plus emissive color+strength. Procedural shader nodes (noise, voronoi, color ramps, node groups) arrive as flat colors — bake them to image textures first
- **Not on the wire**: vertex colors, custom split normals / weighted normals (s&box re-shades at a 40° smoothing angle), armatures/animation, packed images (save them to disk)
- Objects with `cutter` or `boolean` in their name are treated as boolean helpers and skipped on purpose

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| **Host** | `localhost` | s&box bridge server hostname |
| **Port** | `8099` | s&box bridge server port |
| **Sync Mode** | Bidirectional | Bidirectional / Export Only / Manual Only |
| **Auto Sync** | On | Automatically sync scene changes |
| **Sync on Connect** | Off | Full scene reconcile on connect; off means Connect is instant |
| **Scale Factor** | `40` | Blender-to-s&box unit multiplier (40 ≈ real scale, 1 grid cell = 1 m) |
| **Grid Size** | `40` | Mirrored to the s&box gizmo grid and Blender's overlay grid |
| **Assets Path** | auto-detected | Path to s&box project `Assets/` folder |
| **Citizen FBX** | — | Optional path to a Citizen model FBX for the Player Reference button |
| **Auto Reconnect** | On | Reconnect automatically on connection loss |

## Troubleshooting

- **Can't connect**: Make sure the bridge server is running in s&box (Editor > Blender Bridge)
- **Objects don't appear**: Check that Auto Sync is enabled, or use Send to Scene / Sync All
- **Materials show pink/error**: Set your Assets Path. New `.vmat` files need a moment to compile in s&box
- **Textures look flat/missing**: Procedural shader nodes and packed images don't transfer — use Image Texture nodes pointing at files on disk
- **Geometry-nodes object arrives empty**: Add a **Realize Instances** node at the end of the tree
- **Area light warning**: s&box doesn't support area lights — use point or spot instead
- **An object vanished from Blender after an engine-side delete**: It's in the hidden **Bridge Trash** collection — use Restore in the panel
- **Hammer editor might need to be open** to place meshes; can be closed after
- **To Play a scene** you might need to **save (Ctrl+S)** the scene after editing to see geometry appear in Play mode (s&box)

## License

MIT — see [LICENSE](LICENSE)

# Roadmap

Living source of truth for what's done, what's next, and what's been deferred.
Supersedes `IMPLEMENTATION_PLAN.md` (frozen at v3.2.0; kept for history only).
Architecture details live in `ARCHITECTURE.md`.

## Done — shipped in 3.5.0

- **Auto-detect Assets path on connect.** Bridge sends `get_project_info`; server replies via `Sandbox.Project.Current.GetAssetsPath()` (scene-independent), with scene-walk fallback. User-set path always wins.
- **Bridge-side position snap.** Every wire position is rounded to `grid_size` source units in `sync.py:_snap_source` before going on the wire. Independent of Blender's snap state, viewport zoom, and Blender version.
- **Removed grid-size buttons.** They didn't reliably set Blender's snap step (Blender 4.2+ removed Absolute Grid Snap; INCREMENT has a 1-BU floor). `grid_size` is now a plain field in the Sync section. Bridge-side snap is the source of truth.
- **Removed `ozmium.oz_mcp` library heuristic** from `GetProjectAssetsDir`. Honest failure mode: if `Project.Current` and the scene-walk both fail, returns null. No more guessing across projects.

## Done — shipped pre-3.5.0

| Area | Status |
|---|---|
| UV transfer Blender → s&box | ✅ shipped 3.4.0 |
| `.vmat` generation from Principled BSDF + texture export + `Compile()` integration | ✅ |
| Per-face material assignment | ✅ |
| `_material_hash_cache` + `Apply Material` escape hatch | ✅ |
| `TryGetExistingDominantMaterial` fallback | ✅ |
| Lock In/Out/Mat per-object policy (`BridgeLockFlags`) | ✅ supersedes the plan's `shape_owner` enum |
| Activity log + warnings | ✅ |
| Pending-deletes confirmation flow | ✅ |
| Force Resync with confirmation dialog | ✅ |
| `scale_factor` re-bake on change | ✅ shipped 3.4.2 |
| Chunked mesh transfer | ✅ |
| Parented-child transforms, undo-safe IDs | ✅ shipped 3.3.0 |

## Next up

### Tier 1 — correctness fixes

- **Material cache-bust.** "Yesterday's `material.vmat` ghosts back when applying a new BSDF" — Source 2 caches loaded `Material` instances by path. With auto-detect now landing the Assets path, this becomes simpler: scan `materials/blender_bridge/*.vmat` on connect, and when about to write `material.vmat` and a different one already exists, generate a unique `safe_name` (`material_2.vmat` or hash-suffixed). New path → no cached instance → no stale binding.
- **`RefreshMaterialsList()` on wire-write.** s&box panel doesn't auto-refresh when the dispatcher writes a new vmat. Wire it to refresh on `OnPaint` or on a "vmat written" signal.

### Tier 2 — Path A material round-trip

The "water shader survives Blender resync" workflow.

- Server walks each face's `Material.ResourcePath` in `ExtractMeshData`, emits per-face material info on the s&box → Blender leg.
- Blender ingests `materials[]` + `faceMaterials[]` from `sync_response` / `mesh_updated`, stores `mat["sbox_vmat_path"] = "materials/water/cool_water.vmat"` as a custom property on the Blender material.
- `_extract_materials` short-circuits when that property is set: stamps `vmatPath` directly into the wire payload, skipping BSDF→vmat regeneration.
- Subsumes the per-object Lock Mat for any material that originated in s&box.
- Estimated ~50 lines per side.

### Tier 3 — Anvil-inspired level-design QOL

The biggest documented gap. Real productivity for the level-design workflow.

- Face paint (Alt+LMB on a face applies the active material)
- Material picker (Alt+RMB sets active material from the picked face)
- Auto-UV (one-click planar/cubic UV unwrap, replacing manual unwrap before sync)
- Hotspot textures
- Grid `[` / `]` hotkeys to step `grid_size` up/down
- Debounced sync queue (multiple edits coalesce into one wire send)

### Smaller items

- **Per-object `grid_size` override.** Custom property `obj["sbox_grid_size"]` overrides scene-level `grid_size` for that object's wire snap. Defaults to scene if not set. ~30 lines, low risk.
- **Auto-detect re-runs on project switch.** Currently only fires on Connect. If user switches s&box projects mid-session and reconnects, it picks up. Could also re-run when `sync_mode` changes.
- **Sync plan preview** before destructive operations (Force Resync, mass deletes).
- **Post-sync report** (what was created/updated/deleted, summary stats).

## Deferred / dead

| Plan item | Status | Reason |
|---|---|---|
| `shape_owner = BLENDER \| SBOX \| UNLOCKED` enum | Dead | Superseded by `BridgeLockFlags` |
| WebSocket transport (Phase 0A/0B) | Dead-low-urgency | HTTP polling works fine in practice |
| VMDL proxy via SourceIO | Dead | Path A round-trip obviates the headline use case (water survival). Keep SourceIO out of the dependency tree |
| Module split (`core/identity.py`, `sync/mesh.py`, …) | Dead | Monolithic `sync.py` works; splitting is busywork |
| `vdf` PyPI dependency | Dead | We hand-write VKV; no external dep needed |
| `complex.vfx` shader reference in plan | Dead | Code emits `shaders/complex.shader` |

## Test plan — grid + scale

We changed grid behavior significantly in 3.5.0. The test cases below are how to verify nothing regressed.

### Bridge-side position snap

| Setup | Action | Expected in s&box |
|---|---|---|
| sf=16, grid=16, free-drag (Blender snap off) | Drag object to arbitrary position, e.g. BU 0.7 | Lands at multiple of 16 source units (16-unit nearest) |
| sf=16, grid=8 | Same | Lands at multiple of 8 source units |
| sf=16, grid=4 | Same | Lands at multiple of 4 |
| sf=16, grid=2 | Same | Lands at multiple of 2 |
| sf=16, grid=1 | Same | Effectively unsnapped (any source-unit integer) |
| sf=16, grid=8, Blender snap on (1-BU INCREMENT) | Drag with G | Lands at multiple of 16 (1 BU = 16 src). Bridge snap is no-op since 16 is already a multiple of 8. **Caveat: documented behavior, not a bug.** |
| sf=32, grid=8 | Drag to BU 0.5 | Lands at multiple of 8 (16 src; 0.5×32=16) |
| sf=8, grid=8 | Drag to BU 0.7 | Lands at multiple of 8 (8 src; 0.7×8=5.6 → 8) |

### Auto-detect Assets path

| Project state | Expected |
|---|---|
| `Project.Current` set, `Assets/` exists | Auto-detect succeeds via `Project.Current.GetAssetsPath()`. Activity log: "Auto-detected Assets path: …" |
| Scene saved, `Project.Current` somehow null | Scene-walk fallback succeeds. Activity log says discovery method was `scene-walk` (visible in Blender system console diag dump) |
| User has manually set `project_assets_path` | Server suggestion logged but not overwritten. User-set wins |
| Scene unsaved AND `Project.Current` null | Honest failure: log explains why, `project_assets_path` stays empty, user can set it manually |

### Scale factor

| Setup | Action | Expected |
|---|---|---|
| sf=16 default | Open Blender, no objects sent | No regression |
| Change sf=16 → sf=32 mid-session | All bridge objects re-send (re-bake) | Vertices in s&box scale by 2x |
| Change sf=32 → sf=16 | All bridge objects re-send | Vertices scale to 0.5x — visually shrink |

### Negative tests (regressions to watch for)

- Clicking auto-populated `Assets Path` field should still allow manual override.
- Connecting against an s&box without the new dispatcher (older library) should fall back gracefully — Blender currently logs "Auto-detect failed" with empty diag and continues. No crash.
- `grid_size` of 0 should not divide-by-zero. (Current code guards with `if gs and gs > 0`.)

## Doc graveyard

- `IMPLEMENTATION_PLAN.md` — frozen at v3.2.0, mostly stale. Kept for history. New planning lives in this file.
- `BRIDGE_V4_DESIGN.md` — the bigger design doc. Most of Phase 1–3 in it is shipped; sections on `shape_owner`, `vdf`, and module split are dead per the table above. Worth a future cleanup pass, not in scope now.
- `ARCHITECTURE.md` — still current and authoritative for runtime behavior. No changes.

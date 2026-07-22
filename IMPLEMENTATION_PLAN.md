# Bridge v4 Implementation Plan

> **Created:** 2026-04-15  
> **Engine access expected:** ~2026-04-29  
> **Current codebase:** v3.2.0 (2,840 lines C#, ~2,500 lines Python)  
> **Design doc:** BRIDGE_V4_DESIGN.md (2,375 lines, 16 sections)

---

## Strategy: Incremental on Working Code

The v4 design doc describes the complete target system. This plan gets there through **incremental fixes on the working v3 codebase**, not a rewrite. Each phase is independently testable and shippable. Phases are ordered by: (1) bug fix ROI, (2) dependency chain, (3) user-visible impact.

---

## Phase 0: Pre-Engine Work (Now — No s&box Needed)

### 0A: WebSocket Server (C# side)

**What:** Replace HTTP polling in `BlenderBridgeServer.cs` with WebSocket support. Keep HTTP endpoints as fallback for backward compatibility.

**Files to modify:**
- `Editor/BlenderBridge/BlenderBridgeServer.cs` (336 lines)

**What to write:**
```csharp
// In HandleRequest, after CORS headers:
if (context.Request.IsWebSocketRequest)
{
    var wsContext = await context.AcceptWebSocketAsync(null);
    await HandleWebSocketSession(wsContext.WebSocket, ct);
    return;
}
// ... existing HTTP handling
```

Add:
- `ConcurrentDictionary<string, WebSocket> _wsClients`
- `HandleWebSocketSession(WebSocket, CancellationToken)` — receive loop
- `BroadcastWs(string json)` — send to all connected WS clients
- `BroadcastWsBinary(byte[] data)` — for mesh frames
- Keep `_outbox` for HTTP fallback; WS clients get direct push

**How to test (no s&box needed):**
```python
# test_ws_connect.py — run against the server
import asyncio, websockets, json

async def test():
    async with websockets.connect("ws://localhost:8099/") as ws:
        # Send hello
        await ws.send(json.dumps({"type": "hello", "protocol_version": 1, "user_id": "test"}))
        # Should get hello_ack
        ack = json.loads(await ws.recv())
        assert ack["type"] == "hello_ack", f"Got {ack}"
        print("PASS: WebSocket handshake works")

asyncio.run(test())
```

**Expected result:** Server accepts both HTTP (existing) and WebSocket connections on port 8099. HTTP clients continue working. WS clients get push instead of poll.

**Risk:** `HttpListener.AcceptWebSocketAsync` may not be available in all s&box .NET environments. Fallback: use a standalone WebSocket library like `WebSocketSharp`.

---

### 0B: WebSocket Client (Python side)

**What:** Replace HTTP polling in `connection.py` with WebSocket client.

**Files to modify:**
- `sbox_bridge/connection.py` (326 lines)

**What to write:**
```python
import asyncio
import websockets

class BridgeConnection:
    def __init__(self, url="ws://localhost:8099/"):
        self._url = url
        self._ws = None
        self._connected = False
        self._receive_callback = None
    
    async def connect(self):
        self._ws = await websockets.connect(self._url)
        await self._send_hello()
        self._connected = True
    
    async def _send_hello(self):
        hello = {"type": "hello", "protocol_version": 1, ...}
        await self._ws.send(json.dumps(hello))
        ack = json.loads(await self._ws.recv())
        assert ack["type"] == "hello_ack"
        return ack
```

**How to test (Blender, no s&box):**
```python
# Mock server (test_mock_server.py):
import asyncio, websockets, json

async def mock(ws, path):
    hello = json.loads(await ws.recv())
    await ws.send(json.dumps({"type": "hello_ack", "session_id": "test", "full_state": []}))
    async for msg in ws:
        print(f"Received: {msg[:100]}")

asyncio.run(websockets.serve(mock, "localhost", 8099))
```

Run mock server, then load addon in Blender, click Connect. Should complete handshake.

**Expected result:** Blender connects via WebSocket. No more 100ms polling timer. Messages pushed immediately.

---

### 0C: Dual-Storage Bridge ID (Python side)

**What:** Store bridge ID in both `obj["sbox_bridge_id"]` AND `obj.data["sbox_bridge_id"]`. Add recovery in `undo_post` handler.

**Files to modify:**
- `sbox_bridge/sync.py` → new `sbox_bridge/core/identity.py`

**What to write:**
```python
def assign_bridge_id(obj, bridge_id):
    obj["sbox_bridge_id"] = bridge_id
    if obj.data:
        obj.data["sbox_bridge_id"] = bridge_id

def get_bridge_id(obj):
    return (obj.get("sbox_bridge_id") or
            (obj.data and obj.data.get("sbox_bridge_id")))

def on_undo_post(scene):
    """Re-inject bridge IDs lost by undo from mesh data storage."""
    for obj in bpy.data.objects:
        if not obj.get("sbox_bridge_id") and obj.data:
            mesh_id = obj.data.get("sbox_bridge_id")
            if mesh_id:
                obj["sbox_bridge_id"] = mesh_id
                log.info(f"Recovered bridge ID for {obj.name} after undo")
```

**How to test (Blender only):**
1. Open Blender, enable addon
2. Create a cube, assign bridge ID via `assign_bridge_id(obj, "test_123")`
3. Press Ctrl+Z 5 times (past creation)
4. Press Ctrl+Shift+Z 5 times (redo)
5. Check: `obj.get("sbox_bridge_id")` should still be `"test_123"`
6. Check: `obj.data.get("sbox_bridge_id")` should still be `"test_123"`

**Expected result:** Bridge ID survives undo/redo. No more ghost duplicates from lost IDs.

---

### 0D: Depsgraph Flag-Based Suppression (Python side)

**What:** Replace time-based echo suppression with a flag set before/after applying remote updates.

**Files to modify:**
- `sbox_bridge/sync.py` → `sbox_bridge/sync/mesh.py`

**What to write:**
```python
_applying_remote: set[str] = set()

def apply_remote_mesh(bridge_id, mesh_data):
    _applying_remote.add(bridge_id)
    try:
        apply_mesh_to_object(bridge_id, mesh_data)
    finally:
        _applying_remote.discard(bridge_id)

def on_depsgraph_update(scene, depsgraph):
    for update in depsgraph.updates:
        obj = update.id
        bid = get_bridge_id(obj) if hasattr(obj, 'get') else None
        if bid and bid in _applying_remote:
            continue  # change came from s&box, don't echo
        # ... normal sync logic
```

**How to test (Blender only):**
1. Mock a remote mesh update by calling `apply_remote_mesh("test_id", mock_data)`
2. Check that the depsgraph handler does NOT queue an outbound message for that object
3. Manually edit the same object's mesh
4. Check that the depsgraph handler DOES queue an outbound message

**Expected result:** No more echo loops on bidirectional mesh sync.

---

## Phase 1: Core Fixes (First Day With Engine Access)

### 1A: WebSocket Integration Test

**What:** Connect the Phase 0 WebSocket code to real s&box.

**Test procedure:**
1. Start s&box with bridge addon
2. Open Blender with v4 addon
3. Click Connect — should complete WebSocket handshake
4. Create a cube in Blender — should appear in s&box within 500ms (not 100-300ms polling latency)
5. Move the cube in s&box — should move in Blender within 100ms (push, not poll)

**Expected result:** Bidirectional sync with lower latency than v3.

**Debug if failing:**
- Check s&box console for "WebSocket upgrade failed" errors
- Check if `HttpListener.AcceptWebSocketAsync` is available
- Fallback: keep HTTP for now, revisit WebSocket later

---

### 1B: Fix TextureAlignToGrid UV Destruction

**What:** In `BlenderBridgeDispatcher.cs`, remove the `TextureAlignToGrid` call and apply Blender's UVs instead.

**File:** `Editor/BlenderBridge/BlenderBridgeDispatcher.cs` line ~977

**Current code (bad):**
```csharp
mesh.TextureAlignToGrid(mesh.Transform);
```

**Replace with:**
```csharp
// DO NOT call TextureAlignToGrid — it destroys Blender's UVs
// UVs will be applied from Blender data via SetTextureCoord
// For now, skip UV application (preserves artist work better than grid UVs)
// mesh.TextureAlignToGrid(mesh.Transform);  // REMOVED
```

**Test procedure:**
1. In Blender, create a cube and UV unwrap it with a custom UV layout
2. Send to s&box
3. Check the UVs in s&box — they should NOT be grid-aligned

**Expected result:** UVs are no longer destroyed. (They won't be Blender's custom UVs yet — that requires Phase 2's UV sync. But at least they won't be overwritten.)

---

### 1C: Separate Hash from Identity

**What:** In `BlenderBridgeDispatcher.cs`, ensure mesh hash is ONLY used for change detection, never for identity. Identity is ALWAYS the bridge ID tag.

**File:** `Editor/BlenderBridge/BlenderBridgeDispatcher.cs`

**Current problem:** `_lastMeshHash` is used to detect changes, but if a bridge ID is lost, the hash becomes the only way to match objects, causing false matches.

**Fix:** Add a validation check:
```csharp
// In PollForChanges, when detecting mesh changes:
if (!go.Tags.Has(bridgeTag))
    continue; // No bridge tag = not a bridge object, skip entirely

// Only THEN check hash for change detection
var currentHash = ComputeMeshGeometryHash(meshComp);
if (_lastMeshHash.TryGetValue(bridgeId, out var lastHash) && currentHash != lastHash)
{
    // Geometry changed — broadcast update
}
```

**Test:** Create two similar cubes. Delete bridge tag from one. Verify no cross-contamination.

---

## Phase 2: UV & Normal Sync (Days 2-3)

### 2A: Send UVs from Blender

**What:** Extract per-face UVs from Blender meshes and include in mesh_update messages.

**Files:** `sbox_bridge/sync/mesh.py` (new)

**What to write:**
```python
def extract_mesh_with_uvs(obj):
    mesh = obj.data
    mesh.calc_normals_split()
    
    verts = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]
    faces = []
    uvs = {}  # (face_idx, vert_idx) -> (u, v)
    normals = {}  # (face_idx, vert_idx) -> (nx, ny, nz)
    
    uv_layer = mesh.uv_layers.active
    for poly in mesh.polygons:
        face_verts = list(poly.vertices)
        faces.append(face_verts)
        for loop_idx in range(poly.loop_start, poly.loop_start + poly.loop_total):
            vi = mesh.loops[loop_idx].vertex_index
            if uv_layer:
                uv = uv_layer.data[loop_idx].uv
                uvs[(poly.index, vi)] = (uv.x, uv.y)
            n = mesh.loops[loop_idx].normal
            normals[(poly.index, vi)] = (n.x, n.y, n.z)
    
    return verts, faces, uvs, normals
```

**Test:** Send a UV-unwrapped cube. Print UV data on C# side. Verify values match.

---

### 2B: Apply UVs in s&box

**What:** In `BlenderBridgeDispatcher.cs`, apply received UVs via `SetTextureCoord` on PolygonMesh half-edges.

**What to write:**
```csharp
void ApplyUVsFromBridge(PolygonMesh mesh, Dictionary<(int, int), Vector2> uvMap)
{
    int faceIdx = 0;
    foreach (var face in mesh.Faces)
    {
        var edge = face.Edge;
        var current = edge;
        do
        {
            int vertIdx = current.Vertex.Index;
            if (uvMap.TryGetValue((faceIdx, vertIdx), out var uv))
                mesh.SetTextureCoord(current, uv);
            current = current.Next;
        } while (current != edge);
        faceIdx++;
    }
}
```

**Test:** Send UV-mapped geometry from Blender. Apply a textured material in s&box. Verify texture alignment matches Blender.

**This is THE test that validates the entire UV pipeline.**

---

## Phase 3: Material Pipeline + SourceIO Integration (Days 3-5)

### 3A: Install Dependencies & SourceIO Abstraction Layer

**What:** Set up `vdf` PyPI package for .vmat writing. Build SourceIO abstraction layer so the bridge works with OR without SourceIO installed.

**Files to create:**
- `sbox_bridge/format_backend.py` (new)

**What to write:**
```python
# format_backend.py — abstracts SourceIO dependency

class FormatBackend:
    """Interface for Source 2 format operations."""
    def read_vmat(self, path: str) -> dict: ...
    def read_vtex_thumbnail(self, path: str) -> bytes: ...
    def available_features(self) -> list[str]: ...

class SourceIOBackend(FormatBackend):
    def __init__(self):
        try:
            from SourceIO.library.source2.resource_types.compiled_material_resource import \
                CompiledMaterialResource
            from SourceIO.library.source2.resource_types.compiled_texture_resource import \
                CompiledTextureResource
            from SourceIO.library.utils.content_manager import ContentManager
            self._available = True
            self._ContentManager = ContentManager
        except ImportError:
            self._available = False
    
    @property
    def available(self): return self._available
    
    def available_features(self):
        if not self._available: return []
        return ["vmdl_import", "vtex_preview", "vmat_read", "content_scan"]

class NativeBackend(FormatBackend):
    """Reads .vmat text files (VKV format) without SourceIO.
    Does NOT handle compiled .vmat_c or .vtex_c."""
    def __init__(self):
        import vdf  # PyPI: vdf (MIT)
        self._vdf = vdf
    
    def read_vmat(self, path):
        with open(path) as f:
            return self._vdf.load(f)
    
    def available_features(self):
        return ["vmat_read"]  # limited without SourceIO

_backend = None
def get_backend():
    global _backend
    if _backend is None:
        sio = SourceIOBackend()
        _backend = sio if sio.available else NativeBackend()
    return _backend
```

**How to test (Blender only, no s&box):**
1. With SourceIO installed: `get_backend().available_features()` should include `vmdl_import`
2. Temporarily rename SourceIO addon folder, restart Blender: `get_backend().available_features()` should return `["vmat_read"]` only
3. Verify no crashes in either case

**Expected result:** Bridge loads cleanly with or without SourceIO. Features degrade gracefully.

---

### 3B: .vmat Generation Using `vdf` Package

**What:** Extract Principled BSDF parameters, generate .vmat files using `vdf` (VKV format, NOT kv3).

**Dependencies:** `pip install vdf` (MIT license, handles Valve KeyValues format)

**Files:** `sbox_bridge/sync/materials.py` (new)

**What to write:**
```python
import vdf

def generate_vmat(mat, texture_dir, output_path):
    """Generate a .vmat file from a Blender Principled BSDF material."""
    principled = get_principled_bsdf(mat)
    if not principled:
        return None
    
    layer0 = {"shader": "complex.vfx", "F_SPECULAR": "1"}
    
    # Textures (with correct naming suffixes)
    safe_name = sanitize_name(mat.name)
    if has_image_input(principled, "Base Color"):
        tex_path = export_texture(principled, "Base Color", texture_dir, f"{safe_name}_color")
        layer0["TextureColor"] = tex_path
    
    if has_image_input(principled, "Roughness"):
        tex_path = export_texture(principled, "Roughness", texture_dir, f"{safe_name}_rough")
        layer0["TextureRoughness"] = tex_path
    else:
        # Generate 1x1 constant roughness texture
        rough_val = principled.inputs["Roughness"].default_value
        tex_path = generate_constant_texture(texture_dir, f"{safe_name}_rough", rough_val)
        layer0["TextureRoughness"] = tex_path
    layer0["g_flRoughnessScaleFactor"] = "1.000"
    
    if has_image_input(principled, "Metallic"):
        tex_path = export_texture(principled, "Metallic", texture_dir, f"{safe_name}_metal")
        layer0["TextureMetalness"] = tex_path
        layer0["F_METALNESS_TEXTURE"] = "1"
    else:
        layer0["g_flMetalness"] = str(round(principled.inputs["Metallic"].default_value, 3))
    
    if has_image_input(principled, "Normal"):
        tex_path = export_texture(principled, "Normal", texture_dir, f"{safe_name}_normal")
        layer0["TextureNormal"] = tex_path  # OpenGL convention — NO flip
    
    # Color tint (gamma encode: linear → sRGB)
    color = principled.inputs["Base Color"].default_value
    tint = linear_to_srgb(color[0], color[1], color[2])
    layer0["g_vColorTint"] = f"[{tint[0]:.6f} {tint[1]:.6f} {tint[2]:.6f} 0.000000]"
    
    # SSS → switch to skin.shader
    sss_weight = principled.inputs.get("Subsurface Weight")
    if sss_weight and sss_weight.default_value > 0:
        layer0["shader"] = "skin.shader"
        layer0["g_flCurvatureScale"] = str(round(sss_weight.default_value, 3))
    
    # Write using vdf (VKV format with Layer0 wrapper)
    vmat = {"Layer0": layer0}
    with open(output_path, 'w') as f:
        f.write(vdf.dumps(vmat, pretty=True))
    
    return output_path
```

**How to test (Blender only — can verify .vmat text output without s&box):**
1. Create material in Blender with Base Color texture + Roughness 0.7
2. Call `generate_vmat(mat, "/tmp/textures", "/tmp/test.vmat")`
3. Open `/tmp/test.vmat` in text editor
4. Verify: `Layer0 { shader "complex.vfx" F_SPECULAR "1" TextureColor "..." g_flRoughnessScaleFactor "1.000" ... }`
5. Verify texture files exist with `_color`, `_rough` suffixes

**Then with s&box (Phase 3 integration test):**
1. Copy .vmat + textures to `assets/materials/blender_bridge/`
2. s&box auto-compiles (AssetSystem CRC monitoring)
3. Apply material to a mesh in s&box — verify it looks correct

---

### 3C: SourceIO Material Browser (s&box → Blender direction)

**What:** Use SourceIO's ContentManager to enumerate s&box materials. Display in Blender as searchable picker.

**Files:** `sbox_bridge/ui/material_browser.py` (new)

**What to write:**
```python
def scan_sbox_materials(project_path):
    """Use SourceIO ContentManager to find all available materials."""
    backend = get_backend()
    if "content_scan" not in backend.available_features():
        # No SourceIO — fall back to listing .vmat files on disk
        return scan_vmat_files_native(project_path)
    
    from SourceIO.library.utils.content_manager import ContentManager
    cm = ContentManager()
    cm.scan_for_content(project_path)
    
    materials = []
    for mat_path in cm.find_resources("*.vmat_c"):
        materials.append({
            "path": mat_path,
            "name": Path(mat_path).stem,
            "is_hotspot": "_hs" in Path(mat_path).stem,
        })
    return materials
```

**How to test:**
1. With SourceIO: call `scan_sbox_materials()` with s&box project path → should return list of materials including `_hs` hotspot materials
2. Without SourceIO: same call → falls back to file listing, returns .vmat files only (no .vmat_c)

**Expected result:** Blender UI shows searchable list of all s&box materials. Hotspot materials (`_hs`) are flagged.

---

### 3D: Texture Export with Correct Naming

**What:** Export textures with `_color`, `_normal`, `_rough`, `_metal`, `_selfillum`, `_trans` suffixes. s&box auto-detects purpose from these suffixes.

**Test:** Export a full PBR material (color + normal + roughness + metallic). Verify:
1. Four texture files appear with correct suffixes
2. Open s&box Material Editor on the generated .vmat
3. All texture slots should be auto-populated

---

### 3E: Face-Level Material Assignment

**What:** Send per-face material indices with mesh data. Apply on s&box side via `SetFaceMaterial`.

**What to write (C# side, in BlenderBridgeDispatcher.cs):**
```csharp
// In ApplyParsedMeshData, after creating faces:
if (parsed.FaceMaterials != null)
{
    for (int i = 0; i < parsed.FaceGroups.Count && i < parsed.FaceMaterials.Count; i++)
    {
        var matPath = parsed.FaceMaterials[i];
        var mat = LoadMaterialSafe(matPath);
        if (mat != null && faceHandles.Count > i)
            mesh.SetFaceMaterial(faceHandles[i], mat);
    }
}
```

**Test:** Create a cube with different materials on different faces. Send to s&box. Verify per-face materials are correct.

---

### 3F: Reverse Material Sync (s&box → Blender via SourceIO)

**What:** When material_channel = SBOX or BIDIRECTIONAL, pull .vmat from s&box and reconstruct Principled BSDF in Blender.

**Files:** `sbox_bridge/sync/materials.py` (extend)

**What to write:**
```python
def vmat_to_blender_material(vmat_path, mat_name):
    """Read .vmat and create a Blender Principled BSDF material from it."""
    backend = get_backend()
    vmat_data = backend.read_vmat(vmat_path)
    layer0 = vmat_data.get("Layer0", {})
    
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    
    # Color tint (sRGB → linear)
    if "g_vColorTint" in layer0:
        tint = parse_vector(layer0["g_vColorTint"])
        bsdf.inputs["Base Color"].default_value = srgb_to_linear(*tint[:3]) + (1.0,)
    
    # Textures
    if "TextureColor" in layer0:
        add_image_node(mat, bsdf, "Base Color", resolve_texture_path(layer0["TextureColor"]))
    if "TextureNormal" in layer0:
        add_normal_map_node(mat, bsdf, resolve_texture_path(layer0["TextureNormal"]))
    if "TextureRoughness" in layer0:
        add_image_node(mat, bsdf, "Roughness", resolve_texture_path(layer0["TextureRoughness"]),
                       non_color=True)
    
    # Scalar values
    if "g_flMetalness" in layer0:
        bsdf.inputs["Metallic"].default_value = float(layer0["g_flMetalness"])
    
    mat["sbox_synced"] = True  # prevent re-export
    return mat
```

**How to test:**
1. Create a .vmat manually (or use one generated by 3B)
2. Call `vmat_to_blender_material("path/to/test.vmat", "TestMat")`
3. Verify Principled BSDF node tree matches the .vmat parameters
4. Verify texture nodes are connected with correct images

**Expected result:** Materials round-trip: Blender → .vmat → Blender reconstruction matches original within visual epsilon.

---

## Phase 4: Ownership Model (Days 5-7)

### 4A: Authority Default Setting

**What:** Add `authority_default` to addon preferences. New objects get ownership based on setting.

**Test:** Set to BLENDER. Create cube. Verify `shape_owner=BLENDER` property is set. Change to UNLOCKED. Create another cube. Verify `shape_owner=UNLOCKED`.

---

### 4B: Shape Ownership Enforcement

**What:** Block geometry edits on SBOX-owned objects. Block geometry sends for non-BLENDER-owned.

**Test:**
1. Pull object from s&box (should be SBOX-owned)
2. Try to enter Edit Mode — should be allowed (we can't block Edit Mode itself)
3. Edit the mesh and try to send — should be BLOCKED with message
4. Transfer ownership to BLENDER — edit should now send

---

### 4C: Bidirectional Transform Channel

**What:** Transforms flow freely regardless of shape ownership when channel=BIDIRECTIONAL.

**Test:**
1. Artist creates pillar (shape=BLENDER)
2. Send to s&box (channels switch to BIDIRECTIONAL)
3. Engineer moves pillar in s&box
4. Verify Blender pillar moves
5. Artist refines mesh geometry
6. Send geometry — should succeed (shape=BLENDER)
7. Engineer moves again — should work (channels=BIDIRECTIONAL)

**This validates the core workflow from the design doc (Section 4.5).**

---

## Phase 5: VMDL Proxy System via SourceIO (Days 7-9)

**Requires:** SourceIO v5.5.2 installed in Blender (already at `AppData\Roaming\Blender Foundation\Blender\5.x\scripts\addons\SourceIO-master\`)

### 5A: SourceIO Import Wrapper

**What:** Build the safe VMDL import pipeline: copy to cache, import via SourceIO, tag as proxy. Never pass original path to SourceIO.

**Files:** `sbox_bridge/vmdl/source_io.py` (new), `sbox_bridge/vmdl/cache.py` (new)

**What to write:**
```python
# cache.py
import uuid, shutil, hashlib
from pathlib import Path

CACHE_ROOT = Path.home() / ".cache" / "sbox_bridge" / "vmdls"

def create_session_cache():
    session_id = str(uuid.uuid4())
    session_dir = CACHE_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_id, session_dir

def create_verified_copy(original_path, session_dir):
    """Atomic copy with checksum verification. Never modifies original."""
    original = Path(original_path)
    copy_path = session_dir / original.name
    temp_path = copy_path.with_suffix(".tmp")
    
    shutil.copy2(original, temp_path)
    
    # Verify integrity
    orig_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    copy_hash = hashlib.sha256(temp_path.read_bytes()).hexdigest()
    if orig_hash != copy_hash:
        temp_path.unlink()
        raise RuntimeError(f"Copy verification failed: {orig_hash} != {copy_hash}")
    
    import os
    os.replace(str(temp_path), str(copy_path))
    return copy_path, orig_hash
```

```python
# source_io.py
import bpy
from .cache import create_session_cache, create_verified_copy
from ..format_backend import get_backend

def import_vmdl_proxy(original_path):
    """Import a VMDL as a read-only proxy via SourceIO."""
    backend = get_backend()
    if "vmdl_import" not in backend.available_features():
        raise RuntimeError("SourceIO required for VMDL import but not installed")
    
    session_id, session_dir = create_session_cache()
    copy_path, checksum = create_verified_copy(original_path, session_dir)
    
    # Override SourceIO content root to our cache (prevents writes to original dir)
    prefs = bpy.context.preferences.addons.get('SourceIO-master')
    saved_root = None
    if prefs:
        saved_root = prefs.preferences.content_root if hasattr(prefs.preferences, 'content_root') else None
        prefs.preferences.content_root = str(copy_path.parent)
    
    try:
        # SourceIO API: expects .vmdl_c (compiled), scale in Hammer units
        bpy.ops.sourceio.vmdl(
            filepath=str(copy_path),
            import_materials=True,
            import_physics=False,
            import_attachments=False,
            discover_resources=True,
            scale=0.01905,  # Hammer units → meters
        )
    finally:
        if prefs and saved_root is not None:
            prefs.preferences.content_root = saved_root
    
    # Tag the imported objects as proxies
    imported = bpy.context.selected_objects
    for obj in imported:
        obj["sbox_is_proxy"] = True
        obj["sbox_readonly"] = True
        obj["sbox_vmdl_source"] = str(original_path)
        obj["sbox_vmdl_copy"] = str(copy_path)
        obj["sbox_vmdl_checksum"] = checksum
        obj["sbox_vmdl_session"] = session_id
        obj.name = f"[PROXY] {obj.name}"
    
    return imported
```

**How to test (Blender only — needs SourceIO, does NOT need s&box running):**
1. Find a .vmdl_c file in any s&box addon directory
2. Call `import_vmdl_proxy("path/to/model.vmdl_c")` from Python console
3. Verify: copy exists in `~/.cache/sbox_bridge/vmdls/{uuid}/`
4. Verify: mesh appears in Blender with `[PROXY]` prefix
5. Verify: custom properties set (`sbox_is_proxy=True`, `sbox_readonly=True`)
6. Verify: original file unchanged (compare checksums)

**Expected result:** Model imported safely. Original untouched. All proxy metadata in place.

---

### 5B: Edit Prevention (Depsgraph + Watchdog)

**What:** Prevent geometry edits on proxy meshes. Two barriers: depsgraph handler kicks out of Edit Mode, watchdog reverts mesh changes.

**Files:** `sbox_bridge/vmdl/proxy.py` (new)

**What to write:**
```python
import bpy, hashlib, struct

_proxy_mesh_hashes = {}  # obj.name → original mesh hash
_proxy_mesh_backups = {}  # obj.name → serialized mesh data

def compute_mesh_hash(mesh):
    h = hashlib.sha256()
    for v in mesh.vertices:
        h.update(struct.pack('3f', *v.co))
    return h.hexdigest()

def register_proxy(obj):
    """Called after import. Stores hash and backup for revert."""
    mesh = obj.data
    _proxy_mesh_hashes[obj.name] = compute_mesh_hash(mesh)
    # Store backup (vertex positions)
    _proxy_mesh_backups[obj.name] = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]

def check_proxy_edit_mode(scene, depsgraph):
    """Called from unified depsgraph handler. Kick out of Edit Mode on proxies."""
    for obj in scene.objects:
        if obj.get("sbox_is_proxy") and obj.mode == 'EDIT':
            bpy.app.timers.register(lambda o=obj: _force_exit_edit(o), first_interval=0.0)

def _force_exit_edit(obj):
    if obj and obj.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
        # Show non-blocking message
        def draw(self, context):
            self.layout.label(text="VMDL proxies are read-only. Edit the source in s&box.")
        bpy.context.window_manager.popup_menu(draw, title="Edit Blocked", icon='LOCKED')

def watchdog_tick():
    """Called every 2 seconds via bpy.app.timers. Reverts unauthorized mesh edits."""
    for obj_name, original_hash in list(_proxy_mesh_hashes.items()):
        obj = bpy.data.objects.get(obj_name)
        if not obj or not obj.data:
            continue
        current_hash = compute_mesh_hash(obj.data)
        if current_hash != original_hash:
            _revert_mesh(obj)
    return 2.0  # reschedule in 2 seconds

def _revert_mesh(obj):
    backup = _proxy_mesh_backups.get(obj.name)
    if not backup:
        return
    mesh = obj.data
    for i, (x, y, z) in enumerate(backup):
        if i < len(mesh.vertices):
            mesh.vertices[i].co = (x, y, z)
    mesh.update()
```

**How to test (Blender only):**
1. Import proxy via 5A
2. Call `register_proxy(obj)`
3. Tab into Edit Mode → should be kicked out immediately
4. From Python: `obj.data.vertices[0].co += Vector((1,0,0))` → should revert within 2s
5. Add a SubSurf modifier → should be removed within 2s (add modifier guard to watchdog)

---

### 5C: VMDL Proxy Panel UI + s&box Connection

**What:** Panel button to import VMDL, list of active proxies with status. Transform sync ties proxy to s&box scene object.

**How to test (needs s&box):**
1. In s&box, scene has a `ModelRenderer` with citizen.vmdl
2. Bridge sends `vmdl_reference` message with vmdl path + object_id + transform
3. Blender imports proxy via 5A, positions it at the transform
4. Move proxy in Blender → s&box model moves
5. Move model in s&box → Blender proxy moves
6. Scale: Blender (5,0,0) meters → s&box (196.85, 0, 0) inches

---

### 5D: SourceIO VTEX Previews for Material Browser

**What:** Use SourceIO's VTEX decoder to generate thumbnail previews for the material browser (Phase 3C).

**What to write:**
```python
def get_vtex_thumbnail(vtex_c_path, size=64):
    """Decode a .vtex_c file and return a thumbnail as bytes (RGBA)."""
    backend = get_backend()
    if "vtex_preview" not in backend.available_features():
        return None  # No SourceIO — return placeholder
    
    from SourceIO.library.source2.resource_types.compiled_texture_resource import \
        CompiledTextureResource
    
    texture = CompiledTextureResource(vtex_c_path)
    # Get lowest mip level for thumbnail
    image_data = texture.get_texture_data(mip_level=-1)
    # Resize to thumbnail size
    return resize_rgba(image_data, texture.width, texture.height, size, size)
```

**Test:** Load a .vtex_c from s&box addons. Get thumbnail. Display in Blender as preview image. Verify it shows the actual texture.

---

## Phase 6: Anvil-Inspired Level Design Tools (Days 9-12)

**License note:** Anvil is GPL-3.0. All code here is **clean-room reimplemented** from documented behavior. Do not reference Anvil source code during implementation.

### 6A: Face Painting Operator (Alt+LMB)

**What:** Paint active material slot onto clicked face. Integrated with bridge material sync.

**Files:** `sbox_bridge/tools/face_paint.py` (new)

**What to write:**
```python
class BRIDGE_OT_face_paint(bpy.types.Operator):
    bl_idname = "bridge.face_paint"
    bl_label = "Paint Material on Face"
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
            return False
        if obj.get("sbox_is_proxy"):
            cls.poll_message_set("VMDL proxies are read-only")
            return False
        if obj.get("sbox_bridge_ownership") == "SBOX":
            cls.poll_message_set("Object owned by s&box")
            return False
        return True
    
    def invoke(self, context, event):
        # Raycast to find face under mouse
        result, location, normal, face_index = self._raycast(context, event)
        if result and face_index >= 0:
            obj = context.active_object
            active_mat_idx = obj.active_material_index
            obj.data.polygons[face_index].material_index = active_mat_idx
            
            # Queue for bridge sync (debounced)
            material_sync_queue.enqueue(obj, face_index, obj.active_material)
        return {'FINISHED'}
```

**Keybinding:** `Alt+LMB` in Edit Mode (mesh context)

**How to test (Blender only):**
1. Create a cube, assign 2 materials
2. Enter Edit Mode, select material slot 2
3. Alt+click a face → face should change to material 2
4. Verify: `obj.data.polygons[face_index].material_index == 1`

**With s&box (Phase 6 integration):**
1. Face paint in Blender → debounced material_sync → .vmat generated → s&box face updates
2. Verify ~200ms debounce: paint 5 faces rapidly → 1 WebSocket batch message

---

### 6B: Material Picker (Alt+RMB)

**What:** Sample material from clicked face and set as active material.

**What to write:**
```python
class BRIDGE_OT_material_pick(bpy.types.Operator):
    bl_idname = "bridge.material_pick"
    bl_label = "Pick Material from Face"
    
    def invoke(self, context, event):
        result, location, normal, face_index = self._raycast(context, event)
        if result and face_index >= 0:
            obj = context.active_object
            mat_idx = obj.data.polygons[face_index].material_index
            obj.active_material_index = mat_idx
        return {'FINISHED'}
```

**Keybinding:** `Alt+RMB` in Edit Mode

**Test:** Click face with material A → active material changes to A. Click face with material B → changes to B.

---

### 6C: Auto-UV with World-Space Lock

**What:** When UV Lock is ON, moving/editing geometry keeps materials aligned in world space. Integrated into the unified depsgraph handler (Phase 1 phase pipeline).

**Files:** `sbox_bridge/tools/auto_uv.py` (new)

**Property:** `scene.sbox_bridge_uv_lock` (BoolProperty, default True)

**What to write:**
```python
def run_auto_uv(obj, depsgraph):
    """World-space planar UV projection on changed faces.
    Called from Phase 1 of unified depsgraph handler."""
    if not bpy.context.scene.get("sbox_bridge_uv_lock", True):
        return
    
    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    if not uv_layer:
        uv_layer = mesh.uv_layers.new(name="BridgeUV")
    
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    uv_lay = bm.loops.layers.uv.active
    
    for face in bm.faces:
        if not is_face_changed(face):  # skip unchanged faces
            continue
        normal = face.normal
        # Choose projection axis based on face normal (largest component)
        abs_n = (abs(normal.x), abs(normal.y), abs(normal.z))
        if abs_n[2] >= abs_n[0] and abs_n[2] >= abs_n[1]:
            # Floor/ceiling — project XY
            for loop in face.loops:
                co = obj.matrix_world @ loop.vert.co
                loop[uv_lay].uv = (co.x / TEXEL_SCALE, co.y / TEXEL_SCALE)
        elif abs_n[1] >= abs_n[0]:
            # Side wall — project XZ
            for loop in face.loops:
                co = obj.matrix_world @ loop.vert.co
                loop[uv_lay].uv = (co.x / TEXEL_SCALE, co.z / TEXEL_SCALE)
        else:
            # Front/back wall — project YZ
            for loop in face.loops:
                co = obj.matrix_world @ loop.vert.co
                loop[uv_lay].uv = (co.y / TEXEL_SCALE, co.z / TEXEL_SCALE)
    
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
```

**How to test (Blender only):**
1. Create a wall (scaled cube)
2. Apply a brick texture
3. Enable UV Lock
4. Move wall 2 meters on X axis
5. Verify: bricks are still aligned (UVs updated to compensate for world-space shift)
6. Disable UV Lock, move wall → bricks stretch (UVs NOT updated)

**With s&box:**
1. Same test, verify s&box receives the corrected UVs after move
2. Brick texture in s&box matches Blender alignment

**Integration with depsgraph per-object write counter (Phase 0D + 12.7):**
- After `bm.to_mesh()`, stamp `_uv_write_seq[sid]` to prevent auto-UV re-triggering on the depsgraph echo from `to_mesh()`

---

### 6D: Material Sync Debounce Queue

**What:** Batch rapid face painting into coalesced WebSocket messages.

**Files:** `sbox_bridge/sync/material_queue.py` (new)

**Implementation:** The `MaterialSyncQueue` class from design doc Section 12.7. 200ms debounce, atomic .vmat writes, batched WS messages.

**How to test (Blender only):**
1. Create queue instance
2. Enqueue 10 face assignments in 100ms
3. Assert: queue._flush() called once after 200ms
4. Assert: 1 batched message, not 10

**With s&box:**
1. Paint 10 faces rapidly
2. Monitor WebSocket frames — should see 1-2 batch messages
3. All 10 faces should have correct material in s&box

---

### 6E: Hotspot Mapping (Trim Sheets)

**What:** Two workflows from design doc Section 6.8.

**Workflow A test (Custom Atlas, Blender → s&box):**
1. Create trim sheet image in Blender
2. Define 3 hotspot regions in `hotspots.json` (wall, floor, ceiling)
3. Select faces on a room mesh
4. Click "Apply Hotspot" → algorithm matches face aspect ratio to best hotspot region
5. UVs fitted to that atlas region
6. Bridge sends UVs + material assignment to s&box
7. Verify correct texture region visible on faces in s&box

**Workflow B test (s&box _hs materials, s&box → Blender):**
1. Material browser (Phase 3C) lists s&box hotspot materials with `_hs` suffix
2. Select a `_hs` material from picker
3. Click face → bridge assigns that material + computes UVs for the atlas
4. s&box receives material reference (no texture copying — material already exists)

**Files:** `sbox_bridge/tools/hotspot.py` (new)

```python
def load_hotspot_definitions(json_path):
    with open(json_path) as f:
        data = json.load(f)
    return data["hotspots"]

def assign_hotspot_to_face(obj, face_index, hotspot, atlas_material):
    """Compute UVs to fit face into hotspot's UV rectangle."""
    poly = obj.data.polygons[face_index]
    uv_min = hotspot["uv_min"]
    uv_max = hotspot["uv_max"]
    
    # Fit face bounds into UV rectangle
    # ... (aspect ratio matching, orientation-aware projection)
    
    # Assign material
    obj.data.polygons[face_index].material_index = get_material_slot(obj, atlas_material)
    
    # Queue for sync
    material_sync_queue.enqueue(obj, face_index, atlas_material)
```

---

### 6F: Grid Tools & Navigation

**What:** Grid doubling/halving (`[`/`]` keys) and forced grid snapping. Low-effort, high-QOL.

**Test:** Press `]` → grid doubles. Press `[` → grid halves. Create cube → snaps to grid. Matches Hammer-like level design feel.

**Note:** These are Blender-only tools with no bridge sync component. Pure QOL.

---

## Phase 7: Polish & Multi-User (Days 12-14)

### 7A: Sync Plan Preview Dialog

**Test:** Select 5 objects with mixed ownership. Click "Send Selected". Verify dialog shows correct per-object actions. Click Cancel — verify nothing was sent.

### 7B: Post-Sync Report

**Test:** Pull 10 objects (5 BLENDER-owned, 5 SBOX-owned). Verify report panel shows "5 geometry+transforms, 5 transforms only".

### 7C: Activity Log

**Test:** Perform 10 operations. Verify log shows all 10 with correct timestamps and colors. Click an object link — verify selection.

### 7D: Session Identity (Multi-User)

**Test:** Connect two Blender instances. Artist A creates and sends object. Artist B tries to edit — should see "Owned by Artist A". Artist B requests ownership. Artist A grants. Artist B can now edit.

---

## Testing Checklist (Run Before Every Push)

```
[ ] WebSocket connects and handshake completes
[ ] Create cube in Blender → appears in s&box
[ ] Move cube in s&box → moves in Blender
[ ] Edit mesh in Blender → updates in s&box (no duplicate)
[ ] Edit mesh in s&box → updates in Blender (no echo loop)
[ ] Ctrl+Z 10 times → bridge ID survives
[ ] UV-mapped mesh → UVs preserved in s&box
[ ] PBR material → .vmat generated correctly
[ ] SBOX-owned object → geometry send blocked
[ ] VMDL proxy → Edit Mode blocked
[ ] 50 objects → no performance degradation
```

---

## File Mapping: v3 → v4

| v3 File | v4 Destination | Phase |
|---------|---------------|-------|
| `sync.py` (1877 lines) | Split into `core/identity.py`, `core/ownership.py`, `sync/mesh.py`, `sync/transforms.py`, `sync/materials.py`, `sync/lights.py` | 0-3 |
| `connection.py` (326 lines) | Rewrite as WebSocket client | 0B |
| `panel.py` (736 lines) | Add ownership UI, sync plan, activity log | 4-7 |
| `__init__.py` (114 lines) | Add `authority_default`, `user_id` preferences | 4A |
| `BlenderBridgeServer.cs` (336 lines) | Add WebSocket upgrade path | 0A |
| `BlenderBridgeDispatcher.cs` (1562 lines) | Fix UV application, add ownership checks, add error responses | 1B, 2B, 4B |
| `BlenderBridgeWindow.cs` (556 lines) | Add session info, ownership display | 7 |
| `BridgePersistence.cs` (335 lines) | Unchanged initially; add UV persistence later | Future |
| `BridgeSceneHelper.cs` (51 lines) | Unchanged | — |

---

## Dependencies Summary

| Dependency | License | Install | Used In |
|-----------|---------|---------|---------|
| `vdf` (PyPI) | MIT | `pip install vdf` | Phase 3: .vmat generation (VKV format) |
| SourceIO v5.5.2 | MIT | Blender addon (already installed) | Phase 3C: material browser, Phase 5: VMDL import, VTEX previews |
| `websockets` (PyPI) | BSD | `pip install websockets` | Phase 0B: Python WebSocket client |
| Anvil Level Design | **GPL-3.0** | NOT a dependency — concepts only | Phase 6: clean-room reimplemented tools |

**SourceIO is OPTIONAL.** Bridge works without it (Phase 3A: `NativeBackend` fallback). With it, you gain: VMDL proxy import, VTEX thumbnails, compiled material reading, content scanning.

---

## What NOT To Build Yet

These are designed in BRIDGE_V4_DESIGN.md but deferred past the 2-week sprint:

- WAL-based undo registry (dual-storage in Phase 0C is good enough for now)
- Full six-layer VMDL safety system (basic proxy with edit prevention in Phase 5B is sufficient)
- Prefab instancing (v5 — design doc Section 11.4b explicitly defers this)
- Ownership transfer request protocol (force-claim is sufficient for small teams initially)
- ctypes "Correct Face Attributes" hack (UV Lock in Phase 6C uses pure bmesh, no ctypes needed initially)
- World-space UV Lock + baking incompatibility resolution (documented in design doc 6.4b, handle when baking is actually implemented)
- SourceIO VTEX thumbnail cache (show placeholder icons until SourceIO is available)
- Multi-user ownership arbitration under concurrent edits (needs engine testing beyond the 2-week sprint)

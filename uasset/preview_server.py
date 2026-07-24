"""Three.js-based browser preview server for UE5 levels.

Starts a local HTTP server that serves a Three.js scene viewer.
Uses only stdlib http.server — no external Python dependencies.

Usage (called from main.py):
    from preview_server import start_server
    start_server(umap_path, export_dir, content_dir, port=3050)
"""
import http.server
import json
import os
import sys
import mimetypes
import shutil
import urllib.parse
from pathlib import Path

from uasset.mesh import _UE_TO_GLTF_SCALE

# ---------------------------------------------------------------------------
# Scene data builder
# ---------------------------------------------------------------------------

def build_scene_json(umap_path, export_dir, content_dir, scale=_UE_TO_GLTF_SCALE):
    """Parse .umap and build scene data JSON for the viewer.

    Args:
        umap_path:   Path to the .umap file.
        export_dir:  Path to the Export/ folder (contains Meshes/ and Textures/).
        content_dir: Path to the Content/ folder (or project root) for asset resolution.
        scale:       UE-unit → glTF-unit factor the GLBs in export_dir were
                     written with.  Actor transforms stay in UE units; the
                     viewer applies this to place them in the same units as
                     the geometry.

    Returns:
        dict with 'actors', 'camera', 'unit_scale' and 'level_glb' keys.
    """
    from uasset.umap import parse_level

    print(f"Parsing level: {umap_path}")
    level_data = parse_level(umap_path)
    print(f"Found {len(level_data.actors)} actors with mesh references")

    # Build index of available exported GLB meshes
    meshes_dir = os.path.join(export_dir, "Meshes")
    exported_glbs = set()
    if os.path.isdir(meshes_dir):
        for f in os.listdir(meshes_dir):
            if f.lower().endswith('.glb'):
                exported_glbs.add(os.path.splitext(f)[0])

    actors_json = []
    for actor in level_data.actors:
        # Check if a GLB file exists for this mesh (textures are embedded)
        has_glb = actor.mesh_name in exported_glbs

        actors_json.append({
            "name": actor.name,
            "mesh_name": actor.mesh_name,
            "location": {
                "x": actor.world_location[0],
                "y": actor.world_location[1],
                "z": actor.world_location[2],
            },
            "rotation": {
                "pitch": actor.world_rotation[0],
                "yaw": actor.world_rotation[1],
                "roll": actor.world_rotation[2],
            },
            "scale": {
                "x": actor.world_scale[0],
                "y": actor.world_scale[1],
                "z": actor.world_scale[2],
            },
            "parent": actor.parent,
            "has_glb": has_glb,
        })

    camera_json = {
        "location": {
            "x": level_data.camera_location[0],
            "y": level_data.camera_location[1],
            "z": level_data.camera_location[2],
        },
        "rotation": {
            "pitch": level_data.camera_rotation[0],
            "yaw": level_data.camera_rotation[1],
            "roll": level_data.camera_rotation[2],
        },
        "has_camera": level_data.has_camera,
    }

    return {
        "mode": "level",
        "actors": actors_json,
        "camera": camera_json,
        "unit_scale": scale,
        "level_name": os.path.splitext(os.path.basename(umap_path))[0],
    }


def build_glb_scene_json(glb_path):
    """Describe a single already-converted GLB for the viewer.

    There is nothing to parse and no transform to convert: the file is served
    as it is and the viewer sizes itself from the geometry it finds inside.
    """
    return {
        "mode": "glb",
        "model": {
            "name": os.path.basename(glb_path),
            "size_mb": round(os.path.getsize(glb_path) / (1024 * 1024), 1),
        },
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class PreviewHandler(http.server.BaseHTTPRequestHandler):
    """Serves preview.html, scene API, and exported assets."""

    # Class-level attributes set by the start_* entry points.  Level mode sets
    # export_dir and serves out of it; GLB mode sets glb_path and serves that
    # one file, which may live anywhere.
    scene_json = None
    export_dir = None
    glb_path = None
    html_path = None

    def log_message(self, fmt, *args):
        """Quieter logging — only show errors."""
        if args and '404' not in str(args[0]):
            super().log_message(fmt, *args)

    # ---- routing --------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/' or path == '/index.html':
            self._serve_html()
        elif path == '/api/scene':
            self._serve_json()
        elif path == '/api/model.glb':
            self._serve_model()
        elif path.startswith('/Export/Meshes/') or path.startswith('/meshes/'):
            self._serve_export_file(path, 'Meshes')
        elif path.startswith('/Export/Textures/'):
            self._serve_export_file(path, 'Textures')
        else:
            self.send_error(404, 'Not Found')

    # ---- handlers -------------------------------------------------------

    def _serve_html(self):
        try:
            with open(self.html_path, 'r', encoding='utf-8') as f:
                data = f.read().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404, 'preview.html not found')

    def _serve_json(self):
        data = json.dumps(self.scene_json).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _serve_model(self):
        """Serve the single GLB named by --preview-glb.

        The path is fixed at startup rather than taken from the URL, so this
        route exposes exactly one file wherever it happens to live.
        """
        if not self.glb_path:
            self.send_error(404, 'No model loaded')
            return
        try:
            size = os.path.getsize(self.glb_path)
            handle = open(self.glb_path, 'rb')
        except OSError:
            self.send_error(500, 'Error reading model')
            return

        self.send_response(200)
        self.send_header('Content-Type', 'model/gltf-binary')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        # A level GLB runs to hundreds of MB, so stream it rather than holding
        # a second copy in memory.  A reload mid-download closes the socket;
        # that is routine, not an error worth a traceback.
        with handle:
            try:
                shutil.copyfileobj(handle, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _serve_export_file(self, url_path, subdir):
        """Serve a file from Export/<subdir>/."""
        if not self.export_dir:
            self.send_error(404, 'Not Found')
            return

        # Extract filename from URL
        # /Export/Meshes/name.glb  ->  name.glb
        # /meshes/name             ->  name.glb
        # basename() after unquoting keeps an encoded separator from walking
        # out of the export directory, and lets names contain spaces.
        filename = os.path.basename(urllib.parse.unquote(url_path.split('/')[-1]))

        # /meshes/<name> without extension → default to .glb
        if subdir == 'Meshes' and '.' not in filename:
            filename += '.glb'

        filepath = os.path.join(self.export_dir, subdir, filename)
        if not os.path.isfile(filepath):
            self.send_error(404, f'{filename} not found')
            return

        # Explicit MIME types for glTF formats
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.glb':
            mime = 'model/gltf-binary'
        elif ext == '.gltf':
            mime = 'model/gltf+json'
        else:
            mime, _ = mimetypes.guess_type(filename)
            if mime is None:
                mime = 'application/octet-stream'

        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except IOError:
            self.send_error(500, 'Error reading file')


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

class _PreviewServer(http.server.HTTPServer):
    """HTTP server that refuses to share its port on Windows.

    Windows honours SO_REUSEADDR even when another process is actively
    listening, so a second preview binds happily and the two then answer
    requests at random — the page loads from one and its scene JSON from the
    other.  POSIX still wants the flag so a restart need not wait out
    TIME_WAIT.
    """

    allow_reuse_address = (os.name != 'nt')

def start_server(umap_path, export_dir, content_dir, port=3050,
                 scale=_UE_TO_GLTF_SCALE):
    """Build scene data and start the preview HTTP server.

    Args:
        umap_path:   Path to the .umap file.
        export_dir:  Path to the Export/ folder.
        content_dir: Path to the project root (containing Content/) for texture resolution.
        port:        TCP port to listen on (default 3050).
        scale:       UE-unit → glTF-unit factor the GLBs were exported with.
    """
    # Build scene data
    scene_data = build_scene_json(umap_path, export_dir, content_dir, scale=scale)

    # Count unique meshes
    mesh_names = set(a['mesh_name'] for a in scene_data['actors'])
    with_glb = sum(1 for a in scene_data['actors'] if a['has_glb'])
    print(f"Scene: {len(scene_data['actors'])} actors, "
          f"{len(mesh_names)} unique meshes, {with_glb} with GLB")

    _serve(scene_data, port, export_dir=export_dir)


def start_glb_server(glb_path, port=3050):
    """Serve a preview of one already-converted GLB.

    No project, no .umap, no per-mesh export: the file is served as it is and
    the viewer scales itself to whatever is inside it.

    Args:
        glb_path: Path to the .glb file to show.
        port:     TCP port to listen on (default 3050).
    """
    scene_data = build_glb_scene_json(glb_path)
    print(f"Model: {scene_data['model']['name']} "
          f"({scene_data['model']['size_mb']} MB)")

    _serve(scene_data, port, glb_path=glb_path)


def _serve(scene_data, port, export_dir=None, glb_path=None):
    """Point the handler at the prepared scene and run until interrupted."""
    # preview.html sits next to this module
    script_dir = os.path.dirname(os.path.abspath(__file__))

    PreviewHandler.scene_json = scene_data
    PreviewHandler.export_dir = os.path.abspath(export_dir) if export_dir else None
    PreviewHandler.glb_path = os.path.abspath(glb_path) if glb_path else None
    PreviewHandler.html_path = os.path.join(script_dir, 'preview.html')

    try:
        server = _PreviewServer(('0.0.0.0', port), PreviewHandler)
    except OSError as e:
        print(f"\nERROR: cannot serve on port {port}: {e}")
        print("Another preview is probably still running. Stop it, or pass "
              "--port with a free port to run both.")
        sys.exit(1)

    print(f"\nPreview available at http://localhost:{port}")
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down preview server.")
        server.shutdown()

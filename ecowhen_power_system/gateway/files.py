"""File-serving blueprint. Harvested from `ecowhen_data_api/routes/files.py`.

Routes
------
GET /files/<endpoint_id>/              list the files matching the endpoint's patterns
GET /files/<endpoint_id>/<filename>    serve one, with Range / conditional-GET support

The two endpoints are *derived* from `paths.py` rather than configured: this package
already owns both roots, so a YAML file naming them again could only ever drift.

  runtime — the ephemeral contracts and status files (RUNTIME_DIR)
  data    — the durable time series and user intent (DATA_DIR)

Security — two independent checks before anything is served:
  1. traversal guard  — the resolved path must stay inside base_dir
  2. allow-list       — the relative path must match an allow_pattern (fnmatch)
"""
import fnmatch
from dataclasses import dataclass
from pathlib import Path

from flask import Blueprint, current_app, jsonify, send_file

from .. import paths

files_bp = Blueprint("files", __name__)


@dataclass
class FileEndpoint:
    base_dir: Path
    allow_patterns: list


def default_endpoints() -> dict:
    return {
        "runtime": FileEndpoint(
            base_dir=paths.RUNTIME_DIR,
            allow_patterns=["*.yaml", "*.json"],
        ),
        "data": FileEndpoint(
            base_dir=paths.DATA_DIR,
            allow_patterns=["*.csv", "*.json", "*.yaml", "daily/*.csv"],
        ),
    }


def _get_endpoint(endpoint_id: str):
    return current_app.config["FILE_ENDPOINTS"].get(endpoint_id)


@files_bp.get("/<endpoint_id>/")
def list_files(endpoint_id: str):
    ep = _get_endpoint(endpoint_id)
    if ep is None:
        return jsonify({"error": f"unknown endpoint: {endpoint_id}"}), 404

    seen = {}
    for pattern in ep.allow_patterns:
        for path in ep.base_dir.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(ep.base_dir).as_posix()
            if rel not in seen:
                stat = path.stat()
                seen[rel] = {"name": rel, "size": stat.st_size, "mtime": stat.st_mtime}

    return jsonify({"files": sorted(seen.values(), key=lambda f: f["name"])})


@files_bp.get("/<endpoint_id>/<path:filename>")
def get_file(endpoint_id: str, filename: str):
    ep = _get_endpoint(endpoint_id)
    if ep is None:
        return jsonify({"error": f"unknown endpoint: {endpoint_id}"}), 404

    base_resolved = ep.base_dir.resolve()
    candidate = (ep.base_dir / filename).resolve()

    try:
        rel = candidate.relative_to(base_resolved).as_posix()
    except ValueError:
        return jsonify({"error": "path traversal detected"}), 400

    if not any(fnmatch.fnmatch(rel, p) for p in ep.allow_patterns):
        return jsonify({"error": "file not in allow list"}), 403

    if not candidate.exists():
        return jsonify({"error": "file not found"}), 404

    # conditional=True → Werkzeug adds ETag / Last-Modified / Accept-Ranges and
    # answers Range and If-Modified-Since itself (RFC 7233 / RFC 7232).
    return send_file(candidate, conditional=True)

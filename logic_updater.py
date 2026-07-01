"""
logic_updater.py  —  Smart multi-file update system for TestSphere.

Distributes: logic.py + vision.py

Workflow on every startup:
1. Fetch remote manifest.json (lightweight — a few hundred bytes)
2. Compare version + SHA-256 for each file with locally cached manifest
3. If identical → use cached files immediately (no download)
4. If newer/different → download, verify SHA-256, replace cache
5. Dynamically import and inject logic + vision into sys.modules
6. Any failure → fall back to cached, then bundled files

Network impact: only one tiny JSON request on unchanged versions.
"""

import os
import sys
import json
import hashlib
import logging
import importlib.util
import shutil
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("TestSphere.LogicUpdater")

# ── Configuration ─────────────────────────────────────────────────────────────

MANIFEST_URL = (
    "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_PUBLIC_REPO"
    "/main/manifest.json"
)

NETWORK_TIMEOUT = 5  # seconds — short so startup is not delayed

# Local cache directory
_CACHE_DIR      = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "TestSphere" / "logic_cache"
_CACHED_LOGIC   = _CACHE_DIR / "logic.py"
_CACHED_VISION  = _CACHE_DIR / "vision.py"
_CACHED_MANIFEST = _CACHE_DIR / "manifest.json"

# Bundled files — sit alongside this file (packaged with the app)
_APP_DIR        = Path(__file__).parent
_BUNDLED_LOGIC  = _APP_DIR / "logic.py"
_BUNDLED_VISION = _APP_DIR / "vision.py"

# Files managed by this updater
_MANAGED_FILES = {
    "logic": {
        "cached":  _CACHED_LOGIC,
        "bundled": _BUNDLED_LOGIC,
        "module":  "logic",
    },
    "vision": {
        "cached":  _CACHED_VISION,
        "bundled": _BUNDLED_VISION,
        "module":  "vision",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_cache_dir():
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_local_manifest() -> dict:
    try:
        if _CACHED_MANIFEST.exists():
            with open(_CACHED_MANIFEST, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read local manifest: {e}")
    return {}


def _save_local_manifest(data: dict):
    try:
        _ensure_cache_dir()
        with open(_CACHED_MANIFEST, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save local manifest: {e}")


def _fetch_remote_manifest() -> dict | None:
    try:
        import requests
        resp = requests.get(MANIFEST_URL, timeout=NETWORK_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.info(f"Remote manifest fetch failed (offline?): {e}")
        return None


def _download_file(url: str, dest: Path) -> bool:
    """Download a file from url to dest. Uses temp file to avoid partial writes."""
    import requests
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
        shutil.move(str(tmp), str(dest))
        logger.info(f"Downloaded {dest.name}")
        return True
    except Exception as e:
        logger.error(f"Download failed for {dest.name}: {e}")
        try:
            tmp = dest.with_suffix(".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _dynamic_import(name: str, path: Path):
    """Dynamically import a .py file and inject it into sys.modules."""
    spec   = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _best_fallback(file_key: str) -> Path | None:
    """Return the best available fallback path for a given file key."""
    info = _MANAGED_FILES[file_key]
    if info["cached"].exists():
        return info["cached"]
    if info["bundled"].exists():
        return info["bundled"]
    return None


def _backup(path: Path):
    """Save a backup of a file before replacing it."""
    try:
        if path.exists():
            shutil.copy2(str(path), str(path.with_suffix(".bak.py")))
    except Exception as e:
        logger.warning(f"Backup failed for {path.name}: {e}")


def _rollback(path: Path):
    """Restore a backup if the main file is broken."""
    backup = path.with_suffix(".bak.py")
    if backup.exists():
        try:
            shutil.copy2(str(backup), str(path))
            logger.info(f"Rolled back {path.name} from backup")
            return True
        except Exception as e:
            logger.error(f"Rollback failed for {path.name}: {e}")
    return False


# ── Per-file update logic ─────────────────────────────────────────────────────

def _process_file(
    file_key: str,
    remote_manifest: dict,
    local_manifest: dict,
    progress_callback=None,
    base_pct: int = 0,
    pct_range: int = 40,
) -> tuple:
    """
    Check, download, verify, and cache one managed file.

    Returns (path_to_load, was_updated, message)
    """
    info      = _MANAGED_FILES[file_key]
    cached    = info["cached"]
    bundled   = info["bundled"]
    mod_name  = info["module"]

    def _cb(pct, msg):
        if progress_callback:
            try:
                progress_callback(base_pct + int(pct * pct_range / 100), msg)
            except Exception:
                pass

    # Get remote file info from manifest
    # Support both flat manifest (for logic only) and nested files dict
    remote_files = remote_manifest.get("files", {})
    if remote_files and file_key in remote_files:
        r_info = remote_files[file_key]
    else:
        # Flat manifest — only applies to logic
        if file_key == "logic":
            r_info = {
                "sha256":       remote_manifest.get("sha256", ""),
                "download_url": remote_manifest.get("download_url", ""),
            }
        else:
            # No vision entry in manifest — use bundled/cached as-is
            fallback = _best_fallback(file_key)
            if fallback:
                return fallback, False, f"{mod_name} not in manifest — using local"
            raise RuntimeError(f"No {mod_name}.py available")

    r_hash       = r_info.get("sha256", "")
    download_url = r_info.get("download_url", "")

    # Get local file info
    local_files  = local_manifest.get("files", {})
    if local_files and file_key in local_files:
        l_hash = local_files[file_key].get("sha256", "")
    else:
        l_hash = local_manifest.get("sha256", "") if file_key == "logic" else ""

    # Compute actual hash of cached file
    actual_hash = ""
    if cached.exists():
        try:
            actual_hash = _sha256(cached)
        except Exception:
            pass

    # Check if up to date
    up_to_date = (
        r_hash and
        r_hash == l_hash == actual_hash and
        cached.exists()
    )

    if up_to_date:
        _cb(100, f"{mod_name}.py is up to date")
        return cached, False, f"{mod_name}.py up to date"

    # Need to download
    if not download_url:
        fallback = _best_fallback(file_key)
        if fallback:
            _cb(100, f"No download URL for {mod_name} — using local")
            return fallback, False, f"No download URL for {mod_name}"
        raise RuntimeError(f"No download URL and no fallback for {mod_name}.py")

    _cb(20, f"Downloading {mod_name}.py…")
    tmp_path = _CACHE_DIR / f"{mod_name}_download.py"
    ok = _download_file(download_url, tmp_path)

    if not ok:
        fallback = _best_fallback(file_key)
        if fallback:
            _cb(100, f"Download failed for {mod_name} — using previous version")
            return fallback, False, f"Download failed for {mod_name} — using previous version"
        raise RuntimeError(f"Download failed and no fallback for {mod_name}.py")

    # Verify hash
    _cb(70, f"Verifying {mod_name}.py…")
    if r_hash:
        downloaded_hash = _sha256(tmp_path)
        if downloaded_hash != r_hash:
            logger.error(f"SHA-256 mismatch for {mod_name}.py!")
            try:
                tmp_path.unlink()
            except Exception:
                pass
            fallback = _best_fallback(file_key)
            if fallback:
                _cb(100, f"Hash mismatch for {mod_name} — using previous version")
                return fallback, False, f"{mod_name}.py failed integrity check"
            raise RuntimeError(f"{mod_name}.py failed SHA-256 verification")
    else:
        downloaded_hash = _sha256(tmp_path)

    # Replace cache
    _cb(85, f"Installing {mod_name}.py…")
    _backup(cached)
    shutil.move(str(tmp_path), str(cached))

    _cb(100, f"{mod_name}.py updated")
    return cached, True, f"{mod_name}.py updated successfully"


# ── Public API ────────────────────────────────────────────────────────────────

class UpdateResult:
    def __init__(self, logic_module, vision_module, status: str,
                 message: str, updated_files: list):
        self.logic_module   = logic_module
        self.vision_module  = vision_module
        self.status         = status
        self.message        = message
        self.updated_files  = updated_files       # list of file names that were updated
        self.updated        = len(updated_files) > 0


def get_logic(progress_callback=None) -> UpdateResult:
    """
    Main entry point. Downloads and loads both logic.py and vision.py.
    Injects both into sys.modules so existing imports work unchanged.

    progress_callback(pct: int, msg: str) — optional

    Returns UpdateResult with both loaded modules and status info.
    """
    def _cb(pct, msg):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass
        logger.info(f"[{pct}%] {msg}")

    _ensure_cache_dir()

    # ── 1. Fetch remote manifest ──────────────────────────────────────────────
    _cb(10, "Checking for updates…")
    remote = _fetch_remote_manifest()

    if remote is None:
        # Offline — load best available for both files
        _cb(50, "Offline — loading cached files…")
        logic_path  = _best_fallback("logic")
        vision_path = _best_fallback("vision")

        if not logic_path:
            raise RuntimeError("No network and no cached/bundled logic.py available.")
        if not vision_path:
            raise RuntimeError("No network and no cached/bundled vision.py available.")

        logic_mod  = _dynamic_import("logic",  logic_path)
        vision_mod = _dynamic_import("vision", vision_path)
        _cb(100, "Loaded from cache (offline)")
        return UpdateResult(
            logic_mod, vision_mod, "fallback",
            "Network unavailable — loaded from cache", []
        )

    local = _load_local_manifest()

    # ── 2. Process logic.py (pct 15-55) ──────────────────────────────────────
    _cb(15, "Checking logic.py…")
    try:
        logic_path, logic_updated, logic_msg = _process_file(
            "logic", remote, local,
            progress_callback=progress_callback,
            base_pct=15, pct_range=40
        )
    except Exception as e:
        fallback = _best_fallback("logic")
        if fallback:
            logic_path    = fallback
            logic_updated = False
            logic_msg     = f"logic.py update failed: {e}"
        else:
            raise

    # ── 3. Process vision.py (pct 55-90) ─────────────────────────────────────
    _cb(55, "Checking vision.py…")
    try:
        vision_path, vision_updated, vision_msg = _process_file(
            "vision", remote, local,
            progress_callback=progress_callback,
            base_pct=55, pct_range=35
        )
    except Exception as e:
        fallback = _best_fallback("vision")
        if fallback:
            vision_path    = fallback
            vision_updated = False
            vision_msg     = f"vision.py update failed: {e}"
        else:
            raise

    # ── 4. Update local manifest ──────────────────────────────────────────────
    if logic_updated or vision_updated:
        updated_manifest = dict(remote)
        # Ensure files section is accurate with actual downloaded hashes
        if "files" not in updated_manifest:
            updated_manifest["files"] = {}

        if logic_updated:
            updated_manifest["files"]["logic"] = {
                "sha256":       _sha256(logic_path),
                "download_url": remote.get("files", {}).get("logic", {}).get(
                    "download_url", remote.get("download_url", "")
                ),
            }
        if vision_updated:
            updated_manifest["files"]["vision"] = {
                "sha256":       _sha256(vision_path),
                "download_url": remote.get("files", {}).get("vision", {}).get("download_url", ""),
            }

        updated_manifest["cached_at"] = datetime.now().isoformat(timespec="seconds")
        _save_local_manifest(updated_manifest)

    # ── 5. Dynamically import both modules ────────────────────────────────────
    _cb(92, "Loading logic.py…")
    try:
        logic_mod = _dynamic_import("logic", logic_path)
    except Exception as e:
        logger.error(f"Failed to import logic.py: {e}")
        _rollback(logic_path)
        logic_mod = _dynamic_import("logic", logic_path)

    _cb(96, "Loading vision.py…")
    try:
        vision_mod = _dynamic_import("vision", vision_path)
    except Exception as e:
        logger.error(f"Failed to import vision.py: {e}")
        _rollback(vision_path)
        vision_mod = _dynamic_import("vision", vision_path)

    # ── 6. Build result ───────────────────────────────────────────────────────
    updated_files = []
    if logic_updated:  updated_files.append("logic.py")
    if vision_updated: updated_files.append("vision.py")

    if updated_files:
        status  = "updated"
        message = f"Updated: {', '.join(updated_files)}"
    else:
        status  = "up_to_date"
        message = "All files up to date"

    _cb(100, message)
    return UpdateResult(logic_mod, vision_mod, status, message, updated_files)


def get_local_version() -> str:
    manifest = _load_local_manifest()
    return manifest.get("version", "unknown")


def get_cache_info() -> dict:
    manifest = _load_local_manifest()
    logic_hash  = ""
    vision_hash = ""
    if _CACHED_LOGIC.exists():
        try: logic_hash  = _sha256(_CACHED_LOGIC)[:12] + "…"
        except Exception: pass
    if _CACHED_VISION.exists():
        try: vision_hash = _sha256(_CACHED_VISION)[:12] + "…"
        except Exception: pass
    return {
        "version":     manifest.get("version", "N/A"),
        "cached_at":   manifest.get("cached_at", "N/A"),
        "logic_hash":  logic_hash or "N/A",
        "vision_hash": vision_hash or "N/A",
        "has_logic":   _CACHED_LOGIC.exists(),
        "has_vision":  _CACHED_VISION.exists(),
        "cache_dir":   str(_CACHE_DIR),
    }

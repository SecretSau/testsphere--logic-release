"""
logic_updater.py  —  Smart logic.py update system for TestSphere.

Workflow on every startup:
1. Fetch remote manifest.json (lightweight — a few hundred bytes)
2. Compare version + SHA-256 with locally cached manifest
3. If identical → use cached logic.py immediately (no download)
4. If newer/different → download, verify SHA-256, replace cache
5. Dynamically import and return the logic module
6. Any failure at any step → fall back to cached, then bundled logic.py

Network impact: only one tiny JSON request on unchanged versions.
"""

import os
import sys
import json
import hashlib
import logging
import importlib.util
import threading
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("TestSphere.LogicUpdater")

# ── Configuration ─────────────────────────────────────────────────────────────

# Point these to your GitHub repo
MANIFEST_URL = (
    "https://raw.githubusercontent.com/SecretSau/testsphere--logic-release"
    "/main/manifest.json"
)

# Network timeout — short so startup is not delayed
NETWORK_TIMEOUT = 5  # seconds

# Local cache directory
_CACHE_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "TestSphere" / "logic_cache"
_CACHED_LOGIC    = _CACHE_DIR / "logic.py"
_CACHED_MANIFEST = _CACHE_DIR / "manifest.json"

# Bundled logic.py — sits alongside this file (packaged with the app)
_BUNDLED_LOGIC = Path(__file__).parent / "logic.py"


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
    """Load the locally cached manifest. Returns {} if not found or corrupt."""
    try:
        if _CACHED_MANIFEST.exists():
            with open(_CACHED_MANIFEST, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read local manifest: {e}")
    return {}


def _save_local_manifest(data: dict):
    """Persist the manifest to the local cache."""
    try:
        _ensure_cache_dir()
        with open(_CACHED_MANIFEST, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save local manifest: {e}")


def _fetch_remote_manifest() -> dict | None:
    """
    Download the remote manifest.json.
    Returns None on any network or parse failure.
    """
    try:
        import requests
        resp = requests.get(MANIFEST_URL, timeout=NETWORK_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.info(f"Remote manifest fetch failed (offline?): {e}")
        return None


def _download_logic(url: str, dest: Path) -> bool:
    """
    Download logic.py from url to a temp file, then move to dest.
    Returns True on success.
    """
    import requests
    import tempfile
    import shutil

    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        # Write to temp file first
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)

        shutil.move(str(tmp), str(dest))
        logger.info(f"Downloaded logic.py to {dest}")
        return True

    except Exception as e:
        logger.error(f"Download failed: {e}")
        try:
            tmp = dest.with_suffix(".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _dynamic_import(path: Path):
    """
    Dynamically import a Python file as the 'logic' module.
    Returns the module object or raises ImportError.
    """
    spec   = importlib.util.spec_from_file_location("logic", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["logic"] = module
    spec.loader.exec_module(module)
    return module


def _best_fallback():
    """
    Return the best available fallback path:
    cached logic.py → bundled logic.py → None
    """
    if _CACHED_LOGIC.exists():
        return _CACHED_LOGIC
    if _BUNDLED_LOGIC.exists():
        return _BUNDLED_LOGIC
    return None


# ── Public API ────────────────────────────────────────────────────────────────

class UpdateResult:
    """Carries the outcome of get_logic() for optional UI display."""
    def __init__(self, module, status: str, message: str, updated: bool = False):
        self.module  = module    # the loaded logic module
        self.status  = status    # "up_to_date" | "updated" | "fallback" | "bundled"
        self.message = message   # human-readable summary
        self.updated = updated   # True if a new version was downloaded


def get_logic(progress_callback=None) -> UpdateResult:
    """
    Main entry point. Call this on startup instead of `import logic`.

    progress_callback(pct: int, msg: str) — optional; called during download.

    Returns UpdateResult with the loaded module and status info.
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
    _cb(10, "Checking for logic updates…")
    remote = _fetch_remote_manifest()

    if remote is None:
        # Offline or network error — use best available fallback
        fallback = _best_fallback()
        if fallback:
            try:
                _cb(80, f"Offline — using cached logic ({fallback.name})")
                module = _dynamic_import(fallback)
                return UpdateResult(
                    module, "fallback",
                    f"Network unavailable — loaded from {fallback.name}"
                )
            except Exception as e:
                logger.error(f"Fallback import failed: {e}")

        raise RuntimeError(
            "No network and no cached/bundled logic.py available.\n"
            "Please restore logic.py to the application folder."
        )

    # ── 2. Compare with local manifest ───────────────────────────────────────
    local    = _load_local_manifest()
    r_ver    = remote.get("version", "0.0.0")
    l_ver    = local.get("version",  "0.0.0")
    r_hash   = remote.get("sha256",  "")
    l_hash   = local.get("sha256",   "")

    # Also check the actual cached file's hash (guards against corruption)
    actual_hash = ""
    if _CACHED_LOGIC.exists():
        try:
            actual_hash = _sha256(_CACHED_LOGIC)
        except Exception:
            pass

    versions_match = (r_ver == l_ver)
    hashes_match   = (r_hash == l_hash == actual_hash) if r_hash else (actual_hash == l_hash)
    up_to_date     = versions_match and hashes_match and _CACHED_LOGIC.exists()

    # ── 3. Use cache if up to date ────────────────────────────────────────────
    if up_to_date:
        try:
            _cb(90, f"logic.py is up to date (v{r_ver}) — loading cache…")
            module = _dynamic_import(_CACHED_LOGIC)
            return UpdateResult(
                module, "up_to_date",
                f"logic.py v{r_ver} — up to date"
            )
        except Exception as e:
            logger.warning(f"Cache import failed ({e}) — will re-download")

    # ── 4. Download new version ───────────────────────────────────────────────
    download_url = remote.get("download_url", "")
    if not download_url:
        # Manifest found but no download URL — use fallback
        fallback = _best_fallback()
        if fallback:
            _cb(80, "Manifest has no download URL — using existing logic")
            try:
                module = _dynamic_import(fallback)
                return UpdateResult(module, "fallback", "No download URL in manifest")
            except Exception as e:
                raise RuntimeError(f"Could not load fallback logic: {e}")
        raise RuntimeError("Manifest has no download_url and no fallback is available.")

    _cb(30, f"New version available: v{r_ver} — downloading…")

    # Download to a temp path first
    tmp_download = _CACHE_DIR / "logic_download.py"
    ok = _download_logic(download_url, tmp_download)

    if not ok:
        # Download failed — try existing cache or bundled
        fallback = _best_fallback()
        if fallback:
            _cb(80, "Download failed — using previous version")
            try:
                module = _dynamic_import(fallback)
                return UpdateResult(
                    module, "fallback",
                    f"Download failed — running previous version ({fallback.name})"
                )
            except Exception as e:
                raise RuntimeError(f"Download failed and fallback import failed: {e}")
        raise RuntimeError("Download failed and no fallback logic.py is available.")

    # ── 5. Verify SHA-256 ─────────────────────────────────────────────────────
    _cb(70, "Verifying integrity…")
    if r_hash:
        downloaded_hash = _sha256(tmp_download)
        if downloaded_hash != r_hash:
            logger.error(
                f"SHA-256 mismatch! Expected {r_hash}, got {downloaded_hash}"
            )
            try:
                tmp_download.unlink()
            except Exception:
                pass

            fallback = _best_fallback()
            if fallback:
                _cb(80, "Hash mismatch — using previous version")
                module = _dynamic_import(fallback)
                return UpdateResult(
                    module, "fallback",
                    "Downloaded file failed integrity check — using previous version"
                )
            raise RuntimeError(
                "Downloaded logic.py failed SHA-256 verification and no fallback exists."
            )
    else:
        downloaded_hash = _sha256(tmp_download)

    # ── 6. Replace cache ──────────────────────────────────────────────────────
    _cb(85, "Installing new logic.py…")
    try:
        # Back up existing cache before replacing
        if _CACHED_LOGIC.exists():
            backup = _CACHE_DIR / "logic_backup.py"
            import shutil
            shutil.copy2(str(_CACHED_LOGIC), str(backup))

        import shutil
        shutil.move(str(tmp_download), str(_CACHED_LOGIC))
    except Exception as e:
        logger.error(f"Failed to replace cached logic: {e}")
        fallback = _best_fallback()
        if fallback:
            module = _dynamic_import(fallback)
            return UpdateResult(module, "fallback", f"Could not install update: {e}")
        raise

    # ── 7. Update local manifest ──────────────────────────────────────────────
    updated_manifest = dict(remote)
    updated_manifest["sha256"]       = downloaded_hash
    updated_manifest["cached_at"]    = datetime.now().isoformat(timespec="seconds")
    _save_local_manifest(updated_manifest)

    # ── 8. Import and return ──────────────────────────────────────────────────
    _cb(95, f"Loading logic.py v{r_ver}…")
    try:
        module = _dynamic_import(_CACHED_LOGIC)
        _cb(100, f"logic.py v{r_ver} loaded successfully")
        return UpdateResult(
            module, "updated",
            f"Updated to logic.py v{r_ver}",
            updated=True
        )
    except Exception as e:
        logger.error(f"Failed to import newly downloaded logic: {e}")
        # Roll back to backup if available
        backup = _CACHE_DIR / "logic_backup.py"
        if backup.exists():
            try:
                import shutil
                shutil.copy2(str(backup), str(_CACHED_LOGIC))
                module = _dynamic_import(_CACHED_LOGIC)
                return UpdateResult(
                    module, "fallback",
                    f"New version failed to import — rolled back to previous version"
                )
            except Exception:
                pass
        raise RuntimeError(f"Could not import updated logic.py: {e}")


def get_local_version() -> str:
    """Return the locally cached logic version string."""
    manifest = _load_local_manifest()
    return manifest.get("version", "unknown")


def get_cache_info() -> dict:
    """Return info about the local cache for display in the UI."""
    manifest = _load_local_manifest()
    return {
        "version":    manifest.get("version", "N/A"),
        "sha256":     manifest.get("sha256", "N/A")[:12] + "…" if manifest.get("sha256") else "N/A",
        "cached_at":  manifest.get("cached_at", "N/A"),
        "cache_path": str(_CACHED_LOGIC),
        "has_cache":  _CACHED_LOGIC.exists(),
        "has_bundled":_BUNDLED_LOGIC.exists(),
    }

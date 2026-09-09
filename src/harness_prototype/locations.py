"""Discover filesystem locations independently of file operations."""

import os
import re
import shlex
import sys
from pathlib import Path

USER_FOLDER_SETTINGS = {"desktop": "Desktop", "documents": "Personal", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}", "pictures": "My Pictures", "music": "My Music", "videos": "My Video"}
SUPPORTED_LOCATIONS = ("working_directory", "home", *USER_FOLDER_SETTINGS)
XDG_FOLDER_NAMES = {"desktop": "DESKTOP", "documents": "DOCUMENTS", "downloads": "DOWNLOAD", "pictures": "PICTURES", "music": "MUSIC", "videos": "VIDEOS"}
CONVENTIONAL_FOLDER_NAMES = {name: name.title() for name in XDG_FOLDER_NAMES}


def _windows_folder_path(name: str) -> Path:
    """Resolve a standard Windows folder, including redirection."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        value, _ = winreg.QueryValueEx(key, USER_FOLDER_SETTINGS[name])
    if not isinstance(value, str):
        raise OSError(f"Invalid Windows setting for {name}.")
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _xdg_config_path(home: Path) -> Path:
    """Return the active XDG user-directory configuration path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    return (Path(config_home).expanduser() if config_home else home / ".config") / "user-dirs.dirs"


def _expand_xdg_home(value: str, home: Path) -> Path:
    """Expand only HOME in an XDG directory value."""
    expanded = re.sub(r"^\$HOME(?=/|$)|^\$\{HOME\}(?=/|$)", str(home), value)
    if "$" in expanded:
        raise OSError("Unsupported variable in XDG user-directory setting.")
    path = Path(expanded)
    if not path.is_absolute():
        raise OSError("XDG user-directory setting must resolve to an absolute path.")
    return path.resolve()


def _configured_xdg_folder(name: str, home: Path) -> Path | None:
    """Read one folder from XDG user-directory configuration."""
    try:
        contents = _xdg_config_path(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise OSError(f"Cannot read XDG user-directory configuration: {error}") from error
    setting = f"XDG_{XDG_FOLDER_NAMES[name]}_DIR"
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != setting:
            continue
        try:
            values = shlex.split(raw_value.strip(), posix=True)
        except ValueError as error:
            raise OSError(f"Invalid {setting} setting: {error}") from error
        if len(values) != 1:
            raise OSError(f"Invalid {setting} setting.")
        return _expand_xdg_home(values[0], home)
    return None


def _standard_folder_path(name: str, home: Path) -> Path:
    """Resolve a standard folder for the current operating system."""
    if os.name == "nt":
        return _windows_folder_path(name)
    if sys.platform != "darwin":
        configured = _configured_xdg_folder(name, home)
        if configured is not None:
            return configured
    return home / CONVENTIONAL_FOLDER_NAMES[name]


def get_location(location: str) -> dict:
    """Resolve one named location to an existing ordinary directory path."""
    if not isinstance(location, str) or not location.strip():
        return {"ok": False, "name": None, "error": "location must be a nonempty string.", "supported": list(SUPPORTED_LOCATIONS)}
    normalized_name = location.strip().casefold()
    if normalized_name not in SUPPORTED_LOCATIONS:
        return {"ok": False, "name": normalized_name, "error": f"Unsupported location: {normalized_name}", "supported": list(SUPPORTED_LOCATIONS)}
    try:
        home = Path.home().resolve()
        path = Path.cwd().resolve() if normalized_name == "working_directory" else home if normalized_name == "home" else _standard_folder_path(normalized_name, home)
        if not path.is_dir():
            raise OSError(f"Directory unavailable: {path}")
        return {"ok": True, "name": normalized_name, "path": str(path)}
    except (OSError, ValueError, RuntimeError) as error:
        return {"ok": False, "name": normalized_name, "error": str(error)}

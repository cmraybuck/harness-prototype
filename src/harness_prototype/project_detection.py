"""Bounded, non-executing project technology detection."""

import json
import os
import tomllib
import xml.etree.ElementTree as ElementTree
from collections import deque
from pathlib import Path

from .files import EXCLUDED_SEARCH_DIRECTORIES
from .runtime_context import resolve_path
from .tool_results import tool_error, tool_success


MAX_DETECTION_ENTRIES = 20_000
MAX_MANIFESTS = 100
MAX_MANIFEST_BYTES = 262_144
MAX_SCRIPTS_PER_PROJECT = 50
MANIFEST_NAMES = {
    "pyproject.toml", "requirements.txt", "Pipfile", "package.json", "Cargo.toml",
}
DOTNET_SUFFIXES = {".csproj", ".fsproj", ".vbproj"}


def _evidence(path: Path, detail: str) -> dict:
    """Build one source-backed detection evidence item."""
    return {"path": str(path), "detail": detail}


def _named(name: str, path: Path, detail: str) -> dict:
    """Build one named detection with its evidence."""
    return {"name": name, "evidence": [_evidence(path, detail)]}


def _read_manifest(path: Path) -> bytes | dict:
    """Read one manifest without exceeding the per-file byte limit."""
    try:
        with path.open("rb") as source:
            data = source.read(MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        return tool_error(f"Could not read manifest {path}: {error}")
    if len(data) > MAX_MANIFEST_BYTES:
        return tool_error(
            f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte limit: {path}"
        )
    return data


def _dependency_names(*groups: object) -> set[str]:
    """Normalize dependency names from supported TOML arrays and tables."""
    names: set[str] = set()
    pending = list(groups)
    while pending:
        group = pending.pop()
        if isinstance(group, dict):
            pending.extend(group.keys())
            pending.extend(group.values())
            continue
        if isinstance(group, list):
            pending.extend(group)
            continue
        if isinstance(group, str):
            name = group.split(";", 1)[0].split("[", 1)[0]
            name = name.replace("_", "-").strip().split(maxsplit=1)[0]
            for separator in ("<", ">", "=", "!", "~"):
                name = name.split(separator, 1)[0]
            if name:
                names.add(name.casefold())
    return names


def _base_detection(root: Path, path: Path) -> dict:
    """Create the stable result shape shared by all manifest parsers."""
    return {
        "project_path": str(path.parent.relative_to(root)) if path.parent != root else ".",
        "manifest": str(path),
        "languages": [],
        "package_managers": [],
        "test_frameworks": [],
        "scripts": [],
    }


def _parse_pyproject(root: Path, path: Path, data: bytes) -> dict:
    """Detect Python metadata from a pyproject manifest and adjacent lock files."""
    parsed = tomllib.loads(data.decode("utf-8-sig"))
    result = _base_detection(root, path)
    result["languages"].append(_named("Python", path, "pyproject.toml manifest"))
    project = parsed.get("project", {})
    tool = parsed.get("tool", {})
    build_requires = parsed.get("build-system", {}).get("requires", [])
    dependencies = _dependency_names(
        project.get("dependencies", []),
        project.get("optional-dependencies", {}),
        tool.get("poetry", {}).get("dependencies", {}),
        tool.get("poetry", {}).get("group", {}),
    )
    if (path.parent / "uv.lock").is_file():
        result["package_managers"].append(
            _named("uv", path.parent / "uv.lock", "adjacent lock file")
        )
    if "poetry" in tool or (path.parent / "poetry.lock").is_file():
        evidence_path = path if "poetry" in tool else path.parent / "poetry.lock"
        result["package_managers"].append(
            _named("Poetry", evidence_path, "Poetry configuration or lock file")
        )
    if any("setuptools" in str(requirement).casefold() for requirement in build_requires):
        result["package_managers"].append(
            _named("setuptools", path, "build-system.requires includes setuptools")
        )
    if not result["package_managers"]:
        result["package_managers"].append(
            _named("pip-compatible", path, "PEP 621 project manifest")
        )
    for dependency, framework in (("pytest", "pytest"), ("nose2", "nose2"), ("tox", "tox")):
        if dependency in dependencies or dependency in tool:
            result["test_frameworks"].append(
                _named(framework, path, f"{dependency} dependency or tool configuration")
            )
    scripts = project.get("scripts", {})
    poetry_scripts = tool.get("poetry", {}).get("scripts", {})
    for name, command in list(scripts.items()) + list(poetry_scripts.items()):
        if len(result["scripts"]) >= MAX_SCRIPTS_PER_PROJECT:
            break
        result["scripts"].append({
            "name": str(name),
            "command": str(command),
            "evidence": [_evidence(path, "declared script")],
        })
    return result


def _parse_python_auxiliary(root: Path, path: Path, data: bytes) -> dict:
    """Detect a Python project represented by requirements.txt or Pipfile."""
    text = data.decode("utf-8-sig")
    result = _base_detection(root, path)
    result["languages"].append(_named("Python", path, f"{path.name} manifest"))
    manager = "Pipenv" if path.name == "Pipfile" else "pip"
    result["package_managers"].append(_named(manager, path, f"{path.name} manifest"))
    if path.name == "Pipfile":
        parsed = tomllib.loads(text)
        dependencies = _dependency_names(
            parsed.get("packages", {}), parsed.get("dev-packages", {}),
        )
    else:
        requirements = [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        ]
        dependencies = _dependency_names(requirements)
    for framework in ("pytest", "nose2", "tox"):
        if framework in dependencies:
            result["test_frameworks"].append(_named(framework, path, f"{framework} dependency"))
    return result


def _parse_package_json(root: Path, path: Path, data: bytes) -> dict:
    """Detect Node.js package metadata without evaluating JavaScript."""
    parsed = json.loads(data.decode("utf-8-sig"))
    if not isinstance(parsed, dict):
        raise ValueError("top-level JSON value must be an object")
    result = _base_detection(root, path)
    result["languages"].append(_named("JavaScript", path, "package.json manifest"))
    package_manager = parsed.get("packageManager")
    if isinstance(package_manager, str) and package_manager:
        manager = package_manager.split("@", 1)[0]
        result["package_managers"].append(_named(manager, path, "packageManager field"))
    else:
        lock_files = (
            ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "Yarn"),
            ("bun.lock", "Bun"), ("bun.lockb", "Bun"),
            ("package-lock.json", "npm"),
        )
        for lock_name, manager in lock_files:
            lock_path = path.parent / lock_name
            if lock_path.is_file():
                result["package_managers"].append(_named(manager, lock_path, "adjacent lock file"))
                break
        if not result["package_managers"]:
            result["package_managers"].append(
                _named("npm-compatible", path, "package.json manifest")
            )
    dependencies = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = parsed.get(key, {})
        if isinstance(value, dict):
            dependencies.update(value)
    type_script_config = path.parent / "tsconfig.json"
    if "typescript" in dependencies or type_script_config.is_file():
        evidence_path = path if "typescript" in dependencies else type_script_config
        detail = "typescript dependency" if "typescript" in dependencies else "tsconfig.json"
        result["languages"].append(_named("TypeScript", evidence_path, detail))
    frameworks = (
        ("jest", "Jest"), ("vitest", "Vitest"), ("mocha", "Mocha"),
        ("ava", "AVA"), ("jasmine", "Jasmine"),
    )
    for package, framework in frameworks:
        if package in dependencies:
            result["test_frameworks"].append(_named(framework, path, f"{package} dependency"))
    scripts = parsed.get("scripts", {})
    if isinstance(scripts, dict):
        for name, command in list(scripts.items())[:MAX_SCRIPTS_PER_PROJECT]:
            result["scripts"].append({
                "name": str(name),
                "command": str(command),
                "evidence": [_evidence(path, "scripts field")],
            })
    return result


def _parse_dotnet(root: Path, path: Path, data: bytes) -> dict:
    """Detect .NET project metadata from an MSBuild project file."""
    document = ElementTree.fromstring(data.decode("utf-8-sig"))
    result = _base_detection(root, path)
    language = {
        ".csproj": "C#", ".fsproj": "F#", ".vbproj": "Visual Basic",
    }[path.suffix.casefold()]
    result["languages"].append(_named(language, path, f"{path.suffix} project manifest"))
    result["package_managers"].append(_named("NuGet", path, ".NET project dependency format"))
    references = []
    for element in document.iter():
        if element.tag.rsplit("}", 1)[-1] == "PackageReference":
            include = element.attrib.get("Include") or element.attrib.get("Update")
            if include:
                references.append(include.casefold())
    frameworks = (
        ("xunit", "xUnit.net"), ("nunit", "NUnit"),
        ("mstest.testframework", "MSTest"),
    )
    for package, framework in frameworks:
        if any(
            reference == package or reference.startswith(package + ".")
            for reference in references
        ):
            result["test_frameworks"].append(
                _named(framework, path, f"PackageReference includes {package}")
            )
    for element in document.iter():
        if element.tag.rsplit("}", 1)[-1] == "Target" and element.attrib.get("Name"):
            if len(result["scripts"]) >= MAX_SCRIPTS_PER_PROJECT:
                break
            name = element.attrib["Name"]
            result["scripts"].append({
                "name": name,
                "command": f"MSBuild target {name}",
                "evidence": [_evidence(path, "Target element")],
            })
    return result


def _parse_cargo(root: Path, path: Path, data: bytes) -> dict:
    """Detect Rust package or workspace metadata from Cargo.toml."""
    parsed = tomllib.loads(data.decode("utf-8-sig"))
    result = _base_detection(root, path)
    result["languages"].append(_named("Rust", path, "Cargo.toml manifest"))
    result["package_managers"].append(_named("Cargo", path, "Cargo.toml manifest"))
    if "package" in parsed:
        result["test_frameworks"].append(
            _named(
                "Rust test harness",
                path,
                "Cargo package supports built-in test targets",
            )
        )
    return result


def _manifest_parser(path: Path):
    """Select the parser for a recognized manifest path."""
    if path.name == "pyproject.toml":
        return _parse_pyproject
    if path.name in {"requirements.txt", "Pipfile"}:
        return _parse_python_auxiliary
    if path.name == "package.json":
        return _parse_package_json
    if path.name == "Cargo.toml":
        return _parse_cargo
    return _parse_dotnet


def detect_project_technologies(path: str = ".") -> dict:
    """Detect Python, Node, .NET, and Rust projects beneath a directory.

    The scan reads recognized manifests as data and never executes project code.
    It visits at most 20,000 entries and reports at most 100 project manifests.
    """
    if not isinstance(path, str) or not path.strip():
        return tool_error("path must be a nonempty string.")
    try:
        root = resolve_path(path)
    except (OSError, RuntimeError, ValueError) as error:
        return tool_error(str(error))
    if not root.is_dir():
        return tool_error(f"Not a directory: {root}")
    queue = deque([root])
    manifests: list[Path] = []
    errors: list[str] = []
    visited = 0
    stop_reason = None
    while queue and len(manifests) < MAX_MANIFESTS:
        directory = queue.popleft()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as error:
            errors.append(f"{directory}: {error}")
            continue
        for entry in entries:
            visited += 1
            if visited > MAX_DETECTION_ENTRIES:
                stop_reason = "entry_limit"
                queue.clear()
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in EXCLUDED_SEARCH_DIRECTORIES:
                        queue.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    candidate = Path(entry.path)
                    if (
                        candidate.name in MANIFEST_NAMES
                        or candidate.suffix.casefold() in DOTNET_SUFFIXES
                    ):
                        manifests.append(candidate)
                        if len(manifests) >= MAX_MANIFESTS:
                            stop_reason = "manifest_limit"
                            break
            except OSError as error:
                errors.append(f"{entry.path}: {error}")
    detections = []
    for manifest in manifests:
        has_pyproject = (manifest.parent / "pyproject.toml") in manifests
        has_pipfile = (manifest.parent / "Pipfile") in manifests
        if manifest.name == "Pipfile" and has_pyproject:
            continue
        if manifest.name == "requirements.txt" and (has_pyproject or has_pipfile):
            continue
        data = _read_manifest(manifest)
        if isinstance(data, dict):
            errors.append(data["error"])
            continue
        try:
            detections.append(_manifest_parser(manifest)(root, manifest, data))
        except (
            UnicodeDecodeError, ValueError, ElementTree.ParseError,
        ) as error:
            errors.append(f"Could not parse manifest {manifest}: {error}")
    complete = stop_reason is None and not errors
    return tool_success(
        complete=complete,
        truncated=stop_reason is not None,
        root=str(root),
        status="detected" if detections else "inconclusive",
        reason=None if detections else "No supported, readable manifests were detected.",
        detections=detections,
        manifest_count=len(manifests),
        visited_entries=min(visited, MAX_DETECTION_ENTRIES),
        stop_reason=stop_reason,
        errors=errors[:20],
        error_count=len(errors),
        errors_truncated=len(errors) > 20,
    )

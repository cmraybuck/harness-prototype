"""Discover and activate bounded, untrusted local agent skills."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from .locations import application_skill_directory


SKILL_FORMAT_VERSION = 1
SKILL_MANIFEST_NAME = "skill.json"
MAX_SKILLS = 100
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTRUCTIONS_BYTES = 32 * 1024
MAX_RESOURCE_BYTES = 64 * 1024
MAX_RESOURCE_TOTAL_BYTES = 128 * 1024
MAX_RESOURCES = 16
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


@dataclass(frozen=True)
class Skill:
    """Store validated skill metadata and its bounded model context."""

    identifier: str
    name: str
    description: str
    instructions: str
    resources: tuple[tuple[str, str], ...]
    required_capabilities: tuple[str, ...]
    required_tools: tuple[str, ...]
    manifest_path: Path
    source: str


@dataclass(frozen=True)
class SkillDiagnostic:
    """Describe one skill that discovery could not safely load."""

    path: Path
    error: str


@dataclass(frozen=True)
class SkillCatalog:
    """Hold deterministic discovery results and explicit validation failures."""

    skills: dict[str, Skill]
    diagnostics: tuple[SkillDiagnostic, ...]
    roots: tuple[Path, ...]

    def metadata(self) -> list[dict]:
        """Return presentation-independent metadata in stable identifier order."""
        return [
            {
                "id": skill.identifier,
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "manifest_path": str(skill.manifest_path),
                "required_capabilities": list(skill.required_capabilities),
                "required_tools": list(skill.required_tools),
                "resources": [name for name, _ in skill.resources],
            }
            for skill in sorted(self.skills.values(), key=lambda item: item.identifier)
        ]


def bundled_skill_directory() -> Path:
    """Return the immutable skill directory shipped inside the FLUX package."""
    return Path(__file__).resolve().parent / "bundled_skills"


def skill_directories(
    workspace: Path,
    application_directory: Path | None = None,
    bundled_directory: Path | None = None,
) -> tuple[Path, ...]:
    """Return bundled, application, and workspace roots without changing cwd."""
    return (
        (bundled_directory or bundled_skill_directory()).resolve(),
        (application_directory or application_skill_directory()).expanduser().resolve(),
        workspace.resolve() / ".flux" / "skills",
    )


def _read_utf8(path: Path, limit: int, label: str) -> str:
    """Read one regular UTF-8 file while enforcing a byte limit."""
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"{label} exceeds the {limit}-byte limit: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise ValueError(f"{label} must be UTF-8: {path}") from error


def _contained_file(skill_directory: Path, relative: object, label: str) -> Path:
    """Resolve a declared file while preventing absolute paths and traversal."""
    if not isinstance(relative, str) or not relative or not relative.strip():
        raise ValueError(f"{label} must be a nonempty relative path.")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError(f"{label} must be relative to the skill directory.")
    resolved = (skill_directory / candidate).resolve()
    if not resolved.is_relative_to(skill_directory.resolve()):
        raise ValueError(f"{label} cannot leave the skill directory.")
    return resolved


def _string_list(value: object, label: str) -> tuple[str, ...]:
    """Validate a unique list of nonempty strings."""
    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or not item.strip()
        or item != item.strip()
        or any(not character.isprintable() for character in item)
        for item in value
    ):
        raise ValueError(f"{label} must be a list of nonempty trimmed strings.")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not contain duplicates.")
    return tuple(value)


def load_skill(manifest_path: Path, source: str) -> Skill:
    """Validate and load one version-one local skill manifest."""
    manifest_text = _read_utf8(manifest_path, MAX_MANIFEST_BYTES, "Skill manifest")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Malformed JSON at line {error.lineno}, column {error.colno}.") from error
    allowed = {"version", "id", "name", "description", "instructions", "resources", "requires"}
    if not isinstance(manifest, dict) or set(manifest) - allowed:
        raise ValueError("Manifest must be an object containing only documented fields.")
    if manifest.get("version") != SKILL_FORMAT_VERSION:
        raise ValueError(f"Unsupported skill format version; expected {SKILL_FORMAT_VERSION}.")
    identifier = manifest.get("id")
    if not isinstance(identifier, str) or not SKILL_ID_PATTERN.fullmatch(identifier):
        raise ValueError("id must be a lowercase dotted, dashed, or underscored identifier.")
    name = manifest.get("name")
    description = manifest.get("description")
    if (
        not isinstance(name, str) or not name.strip() or len(name) > 100
        or any(not character.isprintable() for character in name)
    ):
        raise ValueError("name must be a nonempty string of at most 100 characters.")
    if (
        not isinstance(description, str) or not description.strip() or len(description) > 500
        or any(not character.isprintable() for character in description)
    ):
        raise ValueError("description must be a nonempty string of at most 500 characters.")
    directory = manifest_path.parent.resolve()
    instructions_path = _contained_file(directory, manifest.get("instructions"), "instructions")
    instructions = _read_utf8(instructions_path, MAX_INSTRUCTIONS_BYTES, "Skill instructions")
    if not instructions.strip():
        raise ValueError("Skill instructions must not be empty.")
    resource_names = _string_list(manifest.get("resources", []), "resources")
    if len(resource_names) > MAX_RESOURCES:
        raise ValueError(f"resources may contain at most {MAX_RESOURCES} files.")
    resources = []
    total_size = 0
    for resource_name in resource_names:
        resource_path = _contained_file(directory, resource_name, "resource")
        contents = _read_utf8(resource_path, MAX_RESOURCE_BYTES, "Skill resource")
        total_size += resource_path.stat().st_size
        if total_size > MAX_RESOURCE_TOTAL_BYTES:
            raise ValueError(f"Skill resources exceed the {MAX_RESOURCE_TOTAL_BYTES}-byte total limit.")
        resources.append((resource_name, contents))
    requires = manifest.get("requires", {})
    if not isinstance(requires, dict) or set(requires) - {"capabilities", "tools"}:
        raise ValueError("requires must contain only capabilities and tools lists.")
    return Skill(
        identifier, name.strip(), description.strip(), instructions,
        tuple(resources),
        _string_list(requires.get("capabilities", []), "requires.capabilities"),
        _string_list(requires.get("tools", []), "requires.tools"),
        manifest_path.resolve(), source,
    )


def discover_skills(
    workspace: Path,
    application_directory: Path | None = None,
    bundled_directory: Path | None = None,
) -> SkillCatalog:
    """Discover bounded skills from bundled, application, then workspace locations."""
    roots = skill_directories(workspace, application_directory, bundled_directory)
    loaded: dict[str, Skill] = {}
    duplicate_ids: set[str] = set()
    diagnostics: list[SkillDiagnostic] = []
    manifest_paths: list[tuple[Path, str]] = []
    sources = ("bundled", "application", "workspace")
    for root, source in zip(roots, sources, strict=True):
        if not root.exists():
            continue
        if not root.is_dir():
            diagnostics.append(SkillDiagnostic(root, "Skill root is not a directory."))
            continue
        try:
            candidates = sorted(root.glob(f"*/{SKILL_MANIFEST_NAME}"), key=lambda path: str(path).casefold())
        except OSError as error:
            diagnostics.append(SkillDiagnostic(root, f"Cannot inspect skill root: {error}"))
            continue
        manifest_paths.extend((path, source) for path in candidates)
    if len(manifest_paths) > MAX_SKILLS:
        diagnostics.append(SkillDiagnostic(roots[0], f"Discovery found more than {MAX_SKILLS} skill manifests."))
        manifest_paths = manifest_paths[:MAX_SKILLS]
    for path, source in manifest_paths:
        root = roots[sources.index(source)]
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Skill manifest cannot resolve outside its discovery root.")
        except (OSError, ValueError) as error:
            diagnostics.append(SkillDiagnostic(path, str(error)))
            continue
        try:
            skill = load_skill(path, source)
        except (OSError, ValueError) as error:
            diagnostics.append(SkillDiagnostic(path, str(error)))
            continue
        if skill.identifier in duplicate_ids:
            diagnostics.append(SkillDiagnostic(
                path, f"Duplicate skill id {skill.identifier!r}; identifier is already invalid."
            ))
            continue
        if skill.identifier in loaded:
            first = loaded.pop(skill.identifier)
            duplicate_ids.add(skill.identifier)
            message = f"Duplicate skill id {skill.identifier!r}; also declared by {first.manifest_path}."
            diagnostics.append(SkillDiagnostic(path, message))
            diagnostics.append(SkillDiagnostic(first.manifest_path, message))
            continue
        loaded[skill.identifier] = skill
    return SkillCatalog(loaded, tuple(diagnostics), roots)


@dataclass
class SkillState:
    """Own the explicit active skill set for one conversation."""

    catalog: SkillCatalog
    active_ids: tuple[str, ...] = field(default_factory=tuple)

    def activate(self, identifiers: object, capabilities: set[str], tools: set[str]) -> dict:
        """Atomically replace active skills after validating every requirement."""
        if not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers):
            return {"ok": False, "status": "invalid_skill_activation", "error": "Skill ids must be a list of strings."}
        if len(set(identifiers)) != len(identifiers):
            return {"ok": False, "status": "invalid_skill_activation", "error": "Skill ids must not contain duplicates."}
        unknown = sorted(set(identifiers) - set(self.catalog.skills))
        if unknown:
            return {"ok": False, "status": "skill_unavailable", "error": f"Unknown or invalid skills: {', '.join(unknown)}."}
        selected = [self.catalog.skills[item] for item in sorted(identifiers)]
        missing_capabilities = sorted({item for skill in selected for item in skill.required_capabilities} - capabilities)
        missing_tools = sorted({item for skill in selected for item in skill.required_tools} - tools)
        if missing_capabilities or missing_tools:
            details = []
            if missing_capabilities:
                details.append("capabilities: " + ", ".join(missing_capabilities))
            if missing_tools:
                details.append("tools: " + ", ".join(missing_tools))
            return {"ok": False, "status": "skill_requirements_unavailable", "error": "Unavailable " + "; ".join(details) + "."}
        self.active_ids = tuple(skill.identifier for skill in selected)
        return {"ok": True, "status": "skills_activated", "active_skills": list(self.active_ids), "error": None}

    def context(self) -> str:
        """Render active instructions and resources with deterministic provenance."""
        sections = []
        for identifier in self.active_ids:
            skill = self.catalog.skills[identifier]
            lines = [f"Skill {skill.identifier} ({skill.name}) from {skill.manifest_path}:", skill.instructions]
            for resource_name, contents in skill.resources:
                lines.extend((f"Resource {resource_name}:", contents))
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

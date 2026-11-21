from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Any
import yaml


@dataclass
class PrefetchRunSpec:
    name: str
    precursor_name: str
    source_path: Path
    active_path: Path
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrefetchPlan:
    manifest_path: Path
    global_state: dict[str, Any]
    runs: list[PrefetchRunSpec]


def resolve_manifest_path(
    harness_dir: Path,
    manifest_filename: str = "prefetch_manifest.yml",
) -> Path:
    """Resolve the requested batch manifest, preferring the harness directory."""
    candidates = [harness_dir / manifest_filename, Path.cwd() / manifest_filename]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Manifest {manifest_filename!r} not found in "
        f"{[str(candidate) for candidate in candidates]}"
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_prefetch_plan(
    harness_dir: Path,
    kuma_root: Path,
    manifest_filename: str = "prefetch_manifest.yml",
) -> PrefetchPlan:
    """Read and validate the ordered batch plan without changing the filesystem."""
    manifest_path = resolve_manifest_path(harness_dir, manifest_filename)
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Manifest root must be a mapping: {manifest_path}")

    global_state = raw.get("global_state", {})
    if not isinstance(global_state, dict):
        raise ValueError("global_state must be a mapping")
    entries = raw.get("runs")
    if not isinstance(entries, list):
        raise ValueError("runs must be a list")

    sa_dir = (kuma_root / "sa").resolve()
    expect_empty_sa = global_state.get("expect_empty_sa", True)
    if not isinstance(expect_empty_sa, bool):
        raise ValueError("global_state.expect_empty_sa must be a boolean")
    active_files = [p.name for p in sa_dir.glob("*.yml")] if sa_dir.exists() else []
    if expect_empty_sa and active_files:
        raise RuntimeError(f"sa/ expected empty; active YAML files found: {active_files}")

    runs: list[PrefetchRunSpec] = []
    names: set[str] = set()
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"runs[{idx}] must be a mapping")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"runs[{idx}].enabled must be a boolean")
        if not enabled:
            continue

        name = entry.get("name", f"run_{idx}")
        precursor = entry.get("precursor")
        source_subdir = entry.get("source_dir", global_state.get("default_export_dir", "bank"))
        arguments = entry.get("arguments", {})
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"runs[{idx}].name must be a non-empty string")
        if name in names:
            raise ValueError(f"Duplicate run name: {name}")
        names.add(name)
        if not isinstance(precursor, str) or not precursor or Path(precursor).name != precursor:
            raise ValueError(f"runs[{idx}].precursor must be a filename, not a path")
        if not isinstance(source_subdir, str) or not source_subdir:
            raise ValueError(f"runs[{idx}].source_dir must be a non-empty path")
        if not isinstance(arguments, dict):
            raise ValueError(f"runs[{idx}].arguments must be a mapping")

        source_dir = sa_dir if source_subdir == "sa" else (sa_dir / source_subdir).resolve()
        source_path = (source_dir / precursor).resolve()
        active_path = (sa_dir / precursor).resolve()
        if not _inside(source_path, sa_dir) or not _inside(active_path, sa_dir):
            raise ValueError(f"Run {name!r} resolves outside Kuma's sa/ directory")
        runs.append(PrefetchRunSpec(name, precursor, source_path, active_path, arguments))

    return PrefetchPlan(manifest_path, global_state, runs)


def stage_run_precursor(spec: PrefetchRunSpec, dry_run: bool = True) -> Path:
    """Stage one named precursor; never overwrite an active file."""
    if not spec.source_path.is_file():
        raise FileNotFoundError(f"[{spec.name}] Precursor missing: {spec.source_path}")
    if spec.source_path == spec.active_path:
        return spec.active_path
    if dry_run:
        print(f"[DRY-RUN][PREFETCH] Would stage {spec.source_path} -> {spec.active_path}")
        return spec.source_path
    if spec.active_path.exists():
        raise FileExistsError(f"[{spec.name}] Active precursor already exists: {spec.active_path}")
    spec.active_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(spec.source_path), str(spec.active_path))
    print(f"[PREFETCH] Staged {spec.source_path} -> {spec.active_path}")
    return spec.active_path

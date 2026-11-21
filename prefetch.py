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
    manifest_filename: str = "harness_manifest.yml",
) -> Path:
    """Resolve manifest next to poc_harness.py first, then CWD."""
    candidates = [
        harness_dir / manifest_filename,
        Path.cwd() / manifest_filename,
    ]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()
    raise FileNotFoundError(
        f"Manifest '{manifest_filename}' not found in {[str(c) for c in candidates]}"
    )


def load_prefetch_plan(
    harness_dir: Path,
    kuma_root: Path,
    manifest_filename: str = "harness_manifest.yml",
) -> PrefetchPlan:
    """Load manifest state and resolve enabled run specs without mutating disk."""
    manifest_path = resolve_manifest_path(harness_dir, manifest_filename)
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid manifest YAML root in {manifest_path}")

    global_state = raw.get("global_state") if isinstance(raw.get("global_state"), dict) else {}
    sa_dir = kuma_root / "sa"
    expect_empty_sa = bool(global_state.get("expect_empty_sa", True))

    if expect_empty_sa and sa_dir.exists():
        leftover_ymls = [p.name for p in sa_dir.glob("*.yml")]
        if leftover_ymls:
            print(f"[PREFETCH][WARN] sa/ expected empty, found active files: {leftover_ymls}")

    runs: list[PrefetchRunSpec] = []
    for idx, entry in enumerate(raw.get("runs", [])):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue

        precursor_name = entry["precursor"]
        source_sub = entry.get("source_dir", global_state.get("default_export_dir", "bank"))
        source_dir = sa_dir / source_sub if source_sub != "sa" else sa_dir
        source_path = (source_dir / precursor_name).resolve()
        active_path = (sa_dir / precursor_name).resolve()
        arguments = entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}

        runs.append(
            PrefetchRunSpec(
                name=entry.get("name", f"run_{idx}"),
                precursor_name=precursor_name,
                source_path=source_path,
                active_path=active_path,
                arguments=arguments,
            )
        )

    return PrefetchPlan(
        manifest_path=manifest_path,
        global_state=global_state,
        runs=runs,
    )


def stage_run_precursor(spec: PrefetchRunSpec, dry_run: bool = True) -> Path:
    """Stage precursor from bank/ into sa/ for execution (or read in place on dry-run)."""
    if not spec.source_path.exists():
        raise FileNotFoundError(f"[{spec.name}] Precursor missing: {spec.source_path}")

    if spec.source_path == spec.active_path:
        return spec.active_path

    if dry_run:
        print(f"[DRY-RUN][PREFETCH] Would stage {spec.source_path} -> {spec.active_path}")
        return spec.source_path

    spec.active_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(spec.source_path), str(spec.active_path))
    print(f"[PREFETCH] Staged {spec.source_path} -> {spec.active_path}")
    return spec.active_path
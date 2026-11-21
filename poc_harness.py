from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any
import yaml

from prefetch import PrefetchPlan, load_prefetch_plan, stage_run_precursor

DEFAULT_BRANDING_PATH = Path("/media/ran/mu/notebook_runtime/src/notebooks/000/branding.yml")
DEFAULT_TAXONOMY_DB_PATH = Path("/media/ran/mu/kuma/data/taxonomy.db")


class PromoteFSM:
    """Deterministic PoC state machine governing read -> brick -> strip -> post."""

    STATES = ("init", "read", "brick", "strip", "post", "done")

    def __init__(self) -> None:
        self.state = "init"

    def advance(self, expected_from: str, target: str) -> str:
        if self.state != expected_from:
            raise RuntimeError(f"FSM transition error: expected {expected_from!r}, got {self.state!r}")
        self.state = target
        return self.state


def resolve_kuma_root() -> Path:
    configured = os.environ.get("KUMA_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
        if not (root / "sa").is_dir():
            raise FileNotFoundError(f"KUMA_ROOT has no sa/ directory: {root}")
        return root
    candidates = [Path.cwd() / "kuma", Path(__file__).resolve().parent / "kuma", Path("/media/ran/mu/kuma")]
    for candidate in candidates:
        if (candidate / "sa").is_dir():
            return candidate.resolve()
    raise FileNotFoundError("Kuma root not found; set KUMA_ROOT to the local Kuma checkout")


def resolve_config_path(value: Any, manifest_path: Path, fallback: Path) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Configured branding/taxonomy path must be a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def load_branding_ssot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        print(f"[WARN] branding.yml not found at {path}; using PoC fallback.")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid branding YAML at {path}: {exc}") from exc
    if loaded is not None and not isinstance(loaded, dict):
        raise ValueError(f"Branding YAML root must be a mapping: {path}")
    return loaded or {}


def load_taxonomy_readonly(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        print(f"[WARN] taxonomy DB not found at {path}; continuing without taxonomy rows.")
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT t.id, t.name, t.tier_id, t.category_id, t.mianzi_weight, t.credit_uri
                   FROM tools t LEFT JOIN tiers r ON t.tier_id = r.id
                   ORDER BY t.tier_id ASC, COALESCE(t.mianzi_weight, 999999) ASC, t.id ASC"""
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not read taxonomy DB {path}: {exc}") from exc


def strip_invisible_ink(obj: Any, exceptions: set[str] | None = None) -> Any:
    exceptions = exceptions or set()
    if isinstance(obj, dict):
        return {
            key: strip_invisible_ink(value, exceptions)
            for key, value in obj.items()
            if not (isinstance(key, str) and key.startswith("_") and key not in exceptions)
        }
    if isinstance(obj, list):
        return [strip_invisible_ink(item, exceptions) for item in obj]
    return obj


def build_bricks(raw: dict[str, Any]) -> dict[str, Any]:
    bricks: dict[str, Any] = {}
    if "milestone" in raw:
        bricks["legacy_milestone"] = raw["milestone"]
    for key in ("id", "schema", "base", "motif", "target", "body", "media", "link"):
        if key in raw and raw[key] is not None:
            bricks[key] = raw[key]
    for key in ("control", "tags", "closer", "wenwu"):
        value = raw.get(key)
        if value is not None and value != [] and value != {}:
            bricks[key] = value
    return bricks


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def extract_brand_context(branding: dict[str, Any], branding_path: Path) -> dict[str, Any]:
    identity = branding.get("brand_identity") if isinstance(branding.get("brand_identity"), dict) else {}
    voice = identity.get("brand_voice_attributes") if isinstance(identity.get("brand_voice_attributes"), dict) else {}
    seo = branding.get("seo_and_platform_guidelines") if isinstance(branding.get("seo_and_platform_guidelines"), dict) else {}
    hashtags = seo.get("hashtags") if isinstance(seo.get("hashtags"), dict) else {}
    ctas = seo.get("calls_to_action") if isinstance(seo.get("calls_to_action"), dict) else {}
    tech = branding.get("technical_specifications") if isinstance(branding.get("technical_specifications"), dict) else {}
    tertiary = voice.get("tertiary_tone", voice.get("tertiry_tone"))
    return {
        "name": branding.get("brand_name") or identity.get("name") or "Darker Side of Passion",
        "domain": branding.get("domain") or identity.get("domain") or "ledante.com",
        "motto": identity.get("motto"),
        "pillars": _as_list(identity.get("brand_pillars") or branding.get("pillars")),
        "voice": {"primary": voice.get("primary_tone"), "secondary": voice.get("secondary_tone"),
                  "tertiary": tertiary, "avoid": _as_list(voice.get("avoid"))},
        "hashtags": {key: _as_list(hashtags.get(key)) for key in ("primary", "secondary", "boilerplate")},
        "keywords": {"core": _as_list(seo.get("core_keywords")), "long_tail": _as_list(seo.get("long_tail_keywords"))},
        "cta_youtube": ctas.get("youtube"), "cta_social": ctas.get("social_media"),
        "tech": {"resolution": tech.get("resolution"), "frame_rate": tech.get("frame_rate"),
                 "software": _as_list(tech.get("software"))},
        "ssot_source": str(branding_path),
    }


def apply_quota_budget(body: str, brand_ctx: dict[str, Any], tier: str,
                       budget_limit: int, quota_policy: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(budget_limit, int) or isinstance(budget_limit, bool) or budget_limit < 0:
        raise ValueError(f"Budget for {tier!r} must be a non-negative integer")
    if quota_policy != "populate_annotate_export_log_only":
        raise ValueError(f"Unsupported quota_policy: {quota_policy!r}")
    body_chars = len(body)
    remaining = budget_limit - body_chars
    footer_parts = []
    if brand_ctx.get("cta_selected"):
        footer_parts.append(str(brand_ctx["cta_selected"]))
    all_tags = brand_ctx["hashtags"]["boilerplate"] + brand_ctx["hashtags"]["primary"] + brand_ctx["hashtags"]["secondary"]
    if all_tags:
        footer_parts.append(" ".join(all_tags))
    footer_chars = len("\n".join(footer_parts))
    total = body_chars + footer_chars
    errata = []
    if remaining < 0:
        errata.append("ERR_BODY_EXCEEDS_BUDGET")
    if total > budget_limit:
        errata.append("ERR_QUOTA_EXCEEDED_LOGGED")
    context = {"cta": brand_ctx.get("cta_selected"), "hashtags": brand_ctx["hashtags"],
               "keywords": brand_ctx["keywords"], "voice_avoid": brand_ctx["voice"]["avoid"]}
    quota = {"policy": quota_policy, "budget_limit": budget_limit, "body_chars": body_chars,
             "footer_chars": footer_chars, "projected_total_chars": total,
             "remaining_headroom": remaining, "exceeded": total > budget_limit,
             "overage_chars": max(0, total - budget_limit), "pruned": [], "errata": errata}
    return context, quota


def _run_settings(arguments: dict[str, Any]) -> tuple[list[str], dict[str, int], dict[str, str], str, list[str], bool]:
    tiers = arguments.get("tiers")
    if not isinstance(tiers, list) or not tiers or any(t not in ("atomic", "fancy") for t in tiers):
        raise ValueError("Each run must provide tiers as a non-empty list of 'atomic'/'fancy'")
    if len(set(tiers)) != len(tiers):
        raise ValueError("tiers must not contain duplicates")
    budgets: dict[str, int] = {}
    for tier in tiers:
        key = f"{tier}_budget"
        value = arguments.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Run must provide a non-negative integer {key}")
        budgets[tier] = value
    routing = arguments.get("cta_routing", {})
    if not isinstance(routing, dict):
        raise ValueError("cta_routing must be a mapping")
    for tier in tiers:
        route = routing.get(tier)
        if route not in ("social_media", "youtube", None):
            raise ValueError(f"Unsupported CTA route for {tier}: {route!r}")
    policy = arguments.get("quota_policy")
    if not isinstance(policy, str) or not policy:
        raise ValueError("Each run must provide quota_policy")
    precedence = arguments.get("precedence", [])
    if not isinstance(precedence, list) or any(not isinstance(item, str) for item in precedence):
        raise ValueError("precedence must be a list of strings")
    archive = arguments.get("archive_to_depot", False)
    if not isinstance(archive, bool):
        raise ValueError("archive_to_depot must be a boolean")
    return tiers, budgets, routing, policy, precedence, archive


def build_variants(clean: dict[str, Any], out_dir: Path, brand_ctx: dict[str, Any],
                   taxonomy_rows: list[dict[str, Any]], arguments: dict[str, Any],
                   branding_path: Path) -> list[tuple[Path, dict[str, Any]]]:
    tiers, budgets, routing, policy, precedence, _ = _run_settings(arguments)
    base_id, motif, target = clean.get("id", "item"), clean.get("motif", "unknown"), clean.get("target", "base")
    body = clean.get("body", "")
    if not isinstance(body, str):
        raise ValueError("precursor body must be a string")
    wenwu = clean.get("wenwu") if isinstance(clean.get("wenwu"), dict) else {}
    closer = clean.get("closer") if isinstance(clean.get("closer"), dict) else {}
    tool_ids = [row["id"] for row in taxonomy_rows if "id" in row]
    results = []
    for tier in tiers:
        route = routing.get(tier)
        selected_cta = brand_ctx.get(f"cta_{route}") if route else None
        tier_ctx = {**brand_ctx, "cta_selected": selected_cta}
        fitted_ctx, quota = apply_quota_budget(body, tier_ctx, tier, budgets[tier], policy)
        payload: dict[str, Any] = {
            "id": f"{base_id}_{tier}", "surface": clean.get("base", "reddit"), "stage": "reddit.json",
            "motif": motif, "target": target, "tier": tier, "body": body,
            "brand_ref": {"name": brand_ctx["name"], "domain": brand_ctx["domain"], "ssot_source": str(branding_path)},
            "brand_context": fitted_ctx, "quota": quota,
            "taxonomy_ref": {"db_present": bool(taxonomy_rows), "tool_count": len(taxonomy_rows)},
            "precedence": {"configured_order": precedence, "wenwu_present": bool(wenwu),
                           "wenwu_enabled": bool(wenwu.get("enabled", False)),
                           "mianzi_tool_ids": tool_ids},
            "gates": {"allow_template_to_publish": False, "web_sense_eligible": False},
            "poc": {"edit_phase_skipped": True, "illustrated_tier_status": "deferred_pending_fsr" if tier == "fancy" else "not_applicable",
                    "legacy_milestone": clean.get("legacy_milestone"), "harness_pass": ["read", "brick", "strip", "post"]},
        }
        if tier == "atomic":
            payload["media"] = {"image": None, "video": None, "alt_text": None}
            payload["link"] = None
        else:
            media = clean.get("media") if isinstance(clean.get("media"), dict) else {}
            payload["media"] = {"image": None, "video": media.get("video"), "alt_text": media.get("alt_text")}
            payload["link"] = clean.get("link") or f"https://{brand_ctx['domain']}"
        results.append((out_dir / f"{motif}_{target}_{tier}.json", payload))
    return results


def run_pass(precursor_path: Path, out_dir: Path, depot_dir: Path, branding: dict[str, Any],
             branding_path: Path, taxonomy_rows: list[dict[str, Any]], arguments: dict[str, Any],
             dry_run: bool) -> list[Path]:
    tiers, _, _, _, _, archive = _run_settings(arguments)
    fsm = PromoteFSM()
    fsm.advance("init", "read")
    data = yaml.safe_load(precursor_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Precursor YAML root must be a mapping: {precursor_path}")
    fsm.advance("read", "brick")
    bricks = build_bricks(data)
    fsm.advance("brick", "strip")
    raw_exceptions = data.get("_exceptions", [])
    if not isinstance(raw_exceptions, list) or any(not isinstance(x, str) for x in raw_exceptions):
        raise ValueError("_exceptions must be a list of strings")
    cleaned = strip_invisible_ink(bricks, set(raw_exceptions))
    fsm.advance("strip", "post")
    brand_ctx = extract_brand_context(branding, branding_path)
    variants = build_variants(cleaned, out_dir, brand_ctx, taxonomy_rows, arguments, branding_path)
    depot_dst = depot_dir / precursor_path.name
    if archive and depot_dst.exists():
        raise FileExistsError(f"Refusing to overwrite archived precursor: {depot_dst}")
    if dry_run:
        if archive:
            print(f"[DRY-RUN][DEPOT] Would archive {precursor_path} -> {depot_dst}")
        for path, payload in variants:
            print(f"[DRY-RUN][EMIT] Would write {path}")
            print(json.dumps(payload, indent=2))
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        temp_files: list[tuple[Path, Path]] = []
        try:
            for path, payload in variants:
                fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=out_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.write("\n")
                temp_files.append((Path(temp_name), path))
            for temp_path, final_path in temp_files:
                temp_path.replace(final_path)
                print(f"[EMIT] {final_path}")
        finally:
            for temp_path, _ in temp_files:
                temp_path.unlink(missing_ok=True)
        if archive:
            depot_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(precursor_path), str(depot_dst))
            print(f"[DEPOT] {precursor_path} -> {depot_dst}")
    fsm.advance("post", "done")
    return [path for path, _ in variants]


def run_from_manifest(harness_dir: Path, kuma_root: Path, out_dir: Path,
                      branding: dict[str, Any], branding_path: Path, taxonomy_rows: list[dict[str, Any]],
                      manifest_filename: str, cli_dry_run: bool | None = None) -> None:
    plan: PrefetchPlan = load_prefetch_plan(harness_dir, kuma_root, manifest_filename)
    manifest_dry_run = plan.global_state.get("dry_run", True)
    if not isinstance(manifest_dry_run, bool):
        raise ValueError("global_state.dry_run must be a boolean")
    dry_run = manifest_dry_run if cli_dry_run is None else cli_dry_run
    for spec in plan.runs:
        # Validate all arguments before moving a precursor.
        _run_settings(spec.arguments)
        staged = stage_run_precursor(spec, dry_run=dry_run)
        run_pass(staged, out_dir, kuma_root / "sa" / "depot", branding, branding_path,
                 taxonomy_rows, spec.arguments, dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute the ordered Kuma batch in prefetch_manifest.yml")
    parser.add_argument("--dry-run", action="store_true", help="Override manifest dry_run and simulate without writes.")
    parser.add_argument("--manifest", default="prefetch_manifest.yml", help="Batch manifest filename.")
    parser.add_argument("--kuma-root", type=Path, default=None, help="Kuma checkout (or set KUMA_ROOT).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.kuma_root.expanduser().resolve() if args.kuma_root else resolve_kuma_root()
    if not (root / "sa").is_dir():
        raise FileNotFoundError(f"Kuma root has no sa/ directory: {root}")
    harness_dir = Path(__file__).resolve().parent
    plan = load_prefetch_plan(harness_dir, root, args.manifest)
    state = plan.global_state
    branding_path = resolve_config_path(state.get("branding_ssot"), plan.manifest_path, DEFAULT_BRANDING_PATH)
    taxonomy_path = resolve_config_path(state.get("taxonomy_db"), plan.manifest_path, DEFAULT_TAXONOMY_DB_PATH)
    branding = load_branding_ssot(branding_path)
    taxonomy_rows = load_taxonomy_readonly(taxonomy_path)
    print(f"[MODE] {'DRY-RUN' if (args.dry_run or state.get('dry_run', True)) else 'LIVE-LOCAL'}")
    print(f"[ROOT] {root}\n[MANIFEST] {plan.manifest_path}\n[OUT] {root / 'out_poc'}")
    run_from_manifest(harness_dir, root, root / "out_poc", branding, branding_path,
                      taxonomy_rows, args.manifest, True if args.dry_run else None)


if __name__ == "__main__":
    main()

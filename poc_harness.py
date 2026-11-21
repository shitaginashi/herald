from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
import yaml

from prefetch import PrefetchPlan, load_prefetch_plan, stage_run_precursor

# Execution configuration is intentionally code-owned: the manifest is the batch
# file (mode, ordered precursors, and per-run arguments), not a CLI interface.
HARNESS_DIR = Path(__file__).resolve().parent
PREFETCH_MANIFEST = "prefetch_manifest.yml"
KUMA_ROOT = Path("/media/ran/mu/kuma")
DEFAULT_BRANDING_PATH = Path("/media/ran/mu/notebook_runtime/src/notebooks/000/branding.yml")
DEFAULT_TAXONOMY_DB_PATH = KUMA_ROOT / "data" / "taxonomy.db"
SUPPORTED_ARGUMENTS = {
    "tiers", "atomic_budget", "fancy_budget", "cta_routing",
    "quota_policy", "precedence", "archive_to_depot",
}


class PromoteFSM:
    """Deterministic PoC state machine governing read -> brick -> strip -> post."""

    def __init__(self) -> None:
        self.state = "init"

    def advance(self, expected_from: str, target: str) -> str:
        if self.state != expected_from:
            raise RuntimeError(f"FSM transition error: expected {expected_from!r}, got {self.state!r}")
        self.state = target
        return self.state


def resolve_config_path(value: Any, manifest_path: Path, fallback: Path) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Configured branding/taxonomy path must be a non-empty string")
    path = Path(value).expanduser()
    return (manifest_path.parent / path).resolve() if not path.is_absolute() else path.resolve()


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
    try:
        uri = f"file:{path.resolve()}?mode=ro"
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
    return {
        "name": branding.get("brand_name") or identity.get("name") or "Darker Side of Passion",
        "domain": branding.get("domain") or identity.get("domain") or "ledante.com",
        "motto": identity.get("motto"),
        "pillars": _as_list(identity.get("brand_pillars") or branding.get("pillars")),
        "voice": {
            "primary": voice.get("primary_tone"), "secondary": voice.get("secondary_tone"),
            "tertiary": voice.get("tertiary_tone", voice.get("tertiry_tone")),
            "avoid": _as_list(voice.get("avoid")),
        },
        "hashtags": {key: _as_list(hashtags.get(key)) for key in ("primary", "secondary", "boilerplate")},
        "keywords": {"core": _as_list(seo.get("core_keywords")), "long_tail": _as_list(seo.get("long_tail_keywords"))},
        "cta_youtube": ctas.get("youtube"), "cta_social": ctas.get("social_media"),
        "tech": {"resolution": tech.get("resolution"), "frame_rate": tech.get("frame_rate"),
                 "software": _as_list(tech.get("software"))},
        "ssot_source": str(branding_path),
    }


def parse_run_arguments(arguments: dict[str, Any]) -> tuple[list[str], dict[str, int], dict[str, str], str, list[str], bool]:
    unknown = set(arguments) - SUPPORTED_ARGUMENTS
    if unknown:
        raise ValueError(f"Unsupported run argument(s): {', '.join(sorted(unknown))}")
    tiers = arguments.get("tiers")
    if (not isinstance(tiers, list) or not tiers
            or any(not isinstance(tier, str) or tier not in ("atomic", "fancy") for tier in tiers)):
        raise ValueError("Each run must provide tiers as a non-empty list of 'atomic'/'fancy'")
    if len(set(tiers)) != len(tiers):
        raise ValueError("tiers must not contain duplicates")
    budgets: dict[str, int] = {}
    for tier in tiers:
        value = arguments.get(f"{tier}_budget")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Run must provide a non-negative integer {tier}_budget")
        budgets[tier] = value
    routing = arguments.get("cta_routing")
    if not isinstance(routing, dict):
        raise ValueError("Each run must provide cta_routing as a mapping")
    if set(routing) - set(tiers):
        raise ValueError("cta_routing may only specify tiers included in tiers")
    for tier, route in routing.items():
        if route not in ("social_media", "youtube"):
            raise ValueError(f"Unsupported CTA route for {tier}: {route!r}")
    policy = arguments.get("quota_policy")
    if policy != "populate_annotate_export_log_only":
        raise ValueError(f"Unsupported quota_policy: {policy!r}")
    precedence = arguments.get("precedence")
    if not isinstance(precedence, list) or any(not isinstance(item, str) for item in precedence):
        raise ValueError("Each run must provide precedence as a list of strings")
    archive = arguments.get("archive_to_depot")
    if not isinstance(archive, bool):
        raise ValueError("Each run must provide archive_to_depot as a boolean")
    return tiers, budgets, routing, policy, precedence, archive


def apply_quota_budget(body: str, brand_ctx: dict[str, Any], tier: str,
                       budget_limit: int, quota_policy: str) -> tuple[dict[str, Any], dict[str, Any]]:
    body_chars = len(body)
    remaining = budget_limit - body_chars
    footer_parts = []
    if brand_ctx.get("cta_selected"):
        footer_parts.append(str(brand_ctx["cta_selected"]))
    tags = brand_ctx["hashtags"]["boilerplate"] + brand_ctx["hashtags"]["primary"] + brand_ctx["hashtags"]["secondary"]
    if tags:
        footer_parts.append(" ".join(tags))
    footer_chars = len("\n".join(footer_parts))
    projected = body_chars + footer_chars
    errata = []
    if remaining < 0:
        errata.append("ERR_BODY_EXCEEDS_BUDGET")
    if projected > budget_limit:
        errata.append("ERR_QUOTA_EXCEEDED_LOGGED")
    context = {"cta": brand_ctx.get("cta_selected"), "hashtags": brand_ctx["hashtags"],
               "keywords": brand_ctx["keywords"], "voice_avoid": brand_ctx["voice"]["avoid"]}
    quota = {"policy": quota_policy, "budget_limit": budget_limit, "body_chars": body_chars,
             "footer_chars": footer_chars, "projected_total_chars": projected,
             "remaining_headroom": remaining, "exceeded": projected > budget_limit,
             "overage_chars": max(0, projected - budget_limit), "pruned": [], "errata": errata}
    return context, quota


def build_variants(clean: dict[str, Any], out_dir: Path, brand_ctx: dict[str, Any],
                   taxonomy_rows: list[dict[str, Any]], arguments: dict[str, Any],
                   branding_path: Path) -> list[tuple[Path, dict[str, Any]]]:
    tiers, budgets, routing, policy, precedence, _ = parse_run_arguments(arguments)
    base_id, motif, target = clean.get("id", "item"), clean.get("motif", "unknown"), clean.get("target", "base")
    body = clean.get("body", "")
    if not isinstance(body, str):
        raise ValueError("precursor body must be a string")
    wenwu = clean.get("wenwu") if isinstance(clean.get("wenwu"), dict) else {}
    tool_ids = [row["id"] for row in taxonomy_rows if "id" in row]
    results = []
    for tier in tiers:
        route = routing.get(tier)
        selected_cta = brand_ctx.get(f"cta_{route}") if route else None
        tier_ctx = {**brand_ctx, "cta_selected": selected_cta}
        projected_context, quota = apply_quota_budget(body, tier_ctx, tier, budgets[tier], policy)
        payload: dict[str, Any] = {
            "id": f"{base_id}_{tier}", "surface": clean.get("base", "reddit"), "stage": "reddit.json",
            "motif": motif, "target": target, "tier": tier, "body": body,
            "brand_ref": {"name": brand_ctx["name"], "domain": brand_ctx["domain"], "ssot_source": str(branding_path)},
            "brand_context": projected_context, "quota": quota,
            "taxonomy_ref": {"db_present": bool(taxonomy_rows), "tool_count": len(taxonomy_rows)},
            "precedence": {"configured_order": precedence, "wenwu_present": bool(wenwu),
                           "wenwu_enabled": bool(wenwu.get("enabled", False)), "mianzi_tool_ids": tool_ids},
            "gates": {"allow_template_to_publish": False, "web_sense_eligible": False},
            "poc": {"edit_phase_skipped": True,
                    "illustrated_tier_status": "deferred_pending_fsr" if tier == "fancy" else "not_applicable",
                    "legacy_milestone": clean.get("legacy_milestone"),
                    "harness_pass": ["read", "brick", "strip", "post"]},
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
    _, _, _, _, _, archive = parse_run_arguments(arguments)
    fsm = PromoteFSM()
    fsm.advance("init", "read")
    data = yaml.safe_load(precursor_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Precursor YAML root must be a mapping: {precursor_path}")
    fsm.advance("read", "brick")
    bricks = build_bricks(data)
    fsm.advance("brick", "strip")
    raw_exceptions = data.get("_exceptions", [])
    if not isinstance(raw_exceptions, list) or any(not isinstance(item, str) for item in raw_exceptions):
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
                with open(fd, "w", encoding="utf-8", closefd=True) as handle:
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
            if depot_dst.exists():
                raise FileExistsError(f"Refusing to overwrite archived precursor: {depot_dst}")
            shutil.move(str(precursor_path), str(depot_dst))
            print(f"[DEPOT] {precursor_path} -> {depot_dst}")
    fsm.advance("post", "done")
    return [path for path, _ in variants]


def run_batch(plan: PrefetchPlan, kuma_root: Path, branding: dict[str, Any],
              branding_path: Path, taxonomy_rows: list[dict[str, Any]]) -> None:
    dry_run = plan.global_state.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("global_state.dry_run must be a boolean")
    out_dir = kuma_root / "out_poc"
    depot_dir = kuma_root / "sa" / "depot"
    # Manifest list order is execution order. Each run's arguments are validated
    # before prefetch can move that run's precursor.
    for spec in plan.runs:
        parse_run_arguments(spec.arguments)
        staged = stage_run_precursor(spec, dry_run=dry_run)
        run_pass(staged, out_dir, depot_dir, branding, branding_path,
                 taxonomy_rows, spec.arguments, dry_run)


def main() -> None:
    if not KUMA_ROOT.is_dir() or not (KUMA_ROOT / "sa").is_dir():
        raise FileNotFoundError(f"Configured Kuma root or sa/ directory not found: {KUMA_ROOT}")
    plan: PrefetchPlan = load_prefetch_plan(HARNESS_DIR, KUMA_ROOT, PREFETCH_MANIFEST)
    state = plan.global_state
    branding_path = resolve_config_path(state.get("branding_ssot"), plan.manifest_path, DEFAULT_BRANDING_PATH)
    taxonomy_path = resolve_config_path(state.get("taxonomy_db"), plan.manifest_path, DEFAULT_TAXONOMY_DB_PATH)
    branding = load_branding_ssot(branding_path)
    taxonomy_rows = load_taxonomy_readonly(taxonomy_path)
    print(f"[MODE] {'DRY-RUN' if state.get('dry_run', True) else 'LIVE-LOCAL'}")
    print(f"[ROOT] {KUMA_ROOT}\n[MANIFEST] {plan.manifest_path}\n[OUT] {KUMA_ROOT / 'out_poc'}")
    run_batch(plan, KUMA_ROOT, branding, branding_path, taxonomy_rows)


if __name__ == "__main__":
    main()

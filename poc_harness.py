from __future__ import annotations
from prefetch import load_prefetch_plan, stage_run_precursor

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any
import yaml

DEFAULT_BRANDING_PATH = Path(
    "/media/ran/mu/notebook_runtime/src/notebooks/000/branding.yml"
)
DEFAULT_TAXONOMY_DB_PATH = Path("/media/ran/mu/kuma/data/taxonomy.db")

DEFAULT_TIER_BUDGETS: dict[str, int] = {
    "atomic": 280,   # tight micro-post / tweet-style budget
    "fancy": 1000,   # broader social/video payload budget
}


class PromoteFSM:
    """Deterministic PoC state machine governing read -> brick -> strip -> post."""

    STATES = ("init", "read", "brick", "strip", "post", "done")

    def __init__(self) -> None:
        self.state = "init"

    def advance(self, expected_from: str, target: str) -> str:
        if self.state != expected_from:
            raise RuntimeError(
                f"FSM transition error: expected '{expected_from}', got '{self.state}'"
            )
        self.state = target
        return self.state

def run_from_manifest(
    harness_dir: Path,
    kuma_root: Path,
    out_dir: Path,
    branding: dict[str, Any],
    taxonomy_rows: list[dict[str, Any]],
    cli_dry_run: bool | None = None,
    manifest_filename: str = "harness_manifest.yml",
    cli_budget_overrides: dict[str, int] | None = None,
) -> None:
    plan = load_prefetch_plan(
        harness_dir,
        kuma_root,
        manifest_filename=manifest_filename,
    )
    dry_run = (
        cli_dry_run
        if cli_dry_run is not None
        else bool(plan.global_state.get("dry_run", True))
    )

    sa_dir = kuma_root / "sa"
    for spec in plan.runs:
        args = spec.arguments
        active_file = stage_run_precursor(spec, dry_run=dry_run)
        if not active_file.exists():
            print(f"[SKIP]    Missing staged precursor: {active_file}")
            continue

        run_overrides: dict[str, int] = {}
        if args.get("atomic_budget") is not None:
            run_overrides["atomic"] = int(args["atomic_budget"])
        if args.get("fancy_budget") is not None:
            run_overrides["fancy"] = int(args["fancy_budget"])
        if cli_budget_overrides:
            run_overrides.update(cli_budget_overrides)

        run_pass(
            sa_dir,
            out_dir,
            active_file,
            branding,
            taxonomy_rows=taxonomy_rows,
            dry_run=dry_run,
            budget_overrides=run_overrides,
        )

def resolve_kuma_root() -> Path:
    """Resolve kuma root explicitly via env or known locations instead of guessing CWD."""
    if "KUMA_ROOT" in os.environ:
        return Path(os.environ["KUMA_ROOT"]).expanduser().resolve()

    candidates = [
        Path.cwd() / "kuma",
        Path(__file__).resolve().parent / "kuma",
        Path("/media/ran/mu/kuma"),
        Path("/media/ran/mu/herald/kuma"),
    ]
    for cand in candidates:
        if (cand / "sa").exists():
            return cand.resolve()

    return Path("/media/ran/mu/kuma").resolve()


def load_branding_ssot(branding_path: Path = DEFAULT_BRANDING_PATH) -> dict[str, Any]:
    """Load branding.yml read-only; warn loudly if missing or malformed."""
    if not branding_path.exists():
        print(f"[WARN] branding.yml not found at {branding_path}; using PoC fallback.")
        return {}
    try:
        loaded = yaml.safe_load(branding_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"[WARN] branding.yml YAML error ({exc}); using PoC fallback.")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_taxonomy_readonly(
    db_path: Path = DEFAULT_TAXONOMY_DB_PATH,
) -> list[dict[str, Any]]:
    """Read static tool/tier rows from taxonomy.db if present; never mutate DB."""
    if not db_path.exists():
        return []

    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.tier_id, t.category_id, t.mianzi_weight, t.credit_uri
            FROM tools t
            LEFT JOIN tiers r ON t.tier_id = r.id
            ORDER BY t.tier_id ASC, COALESCE(t.mianzi_weight, 999999) ASC, t.id ASC
            """
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        print(f"[WARN] taxonomy.db read error ({exc}); continuing without DB rows.")
        return []


def strip_invisible_ink(obj: Any, exceptions: set[str] | None = None) -> Any:
    """Strip '_' prefixed keys unless explicitly allowed by an exception tag."""
    exceptions = exceptions or set()
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("_") and k not in exceptions:
                continue
            cleaned[k] = strip_invisible_ink(v, exceptions)
        return cleaned
    if isinstance(obj, list):
        return [strip_invisible_ink(item, exceptions) for item in obj]
    return obj


def build_bricks(raw: dict[str, Any]) -> dict[str, Any]:
    """Assemble barebones PoC bricks; skip null/empty optional bricks."""
    bricks: dict[str, Any] = {}

    if "milestone" in raw:
        bricks["legacy_milestone"] = raw.get("milestone")

    for key in ("id", "schema", "base", "motif", "target", "body", "media", "link"):
        if key in raw and raw[key] is not None:
            bricks[key] = raw[key]

    for opt in ("control", "tags", "closer", "wenwu"):
        val = raw.get(opt)
        if val is None or val == [] or val == {}:
            continue
        bricks[opt] = val

    return bricks


def _as_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x) for x in val if x is not None]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []

def extract_brand_context(branding: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized brand SSoT fields read-only for PoC projection."""
    identity = (
        branding.get("brand_identity")
        if isinstance(branding.get("brand_identity"), dict)
        else {}
    )
    voice = (
        identity.get("brand_voice_attributes")
        if isinstance(identity.get("brand_voice_attributes"), dict)
        else {}
    )
    seo = (
        branding.get("seo_and_platform_guidelines")
        if isinstance(branding.get("seo_and_platform_guidelines"), dict)
        else {}
    )
    hashtags_raw = seo.get("hashtags") if isinstance(seo.get("hashtags"), dict) else {}
    ctas = (
        seo.get("calls_to_action")
        if isinstance(seo.get("calls_to_action"), dict)
        else {}
    )
    tech = (
        branding.get("technical_specifications")
        if isinstance(branding.get("technical_specifications"), dict)
        else {}
    )

    tertiary = (
        voice.get("tertiary_tone")
        if "tertiary_tone" in voice
        else voice.get("tertiry_tone")
    )

    return {
        "name": branding.get("brand_name") or identity.get("name") or "Darker Side of Passion",
        "domain": branding.get("domain") or identity.get("domain") or "ledante.com",
        "motto": identity.get("motto"),
        "pillars": _as_list(identity.get("brand_pillars") or branding.get("pillars")),
        "voice": {
            "primary": voice.get("primary_tone"),
            "secondary": voice.get("secondary_tone"),
            "tertiary": tertiary,
            "avoid": _as_list(voice.get("avoid")),
        },
        "hashtags": {
            "primary": _as_list(hashtags_raw.get("primary")),
            "secondary": _as_list(hashtags_raw.get("secondary")),
            "boilerplate": _as_list(hashtags_raw.get("boilerplate")),
        },
        "keywords": {
            "core": _as_list(seo.get("core_keywords")),
            "long_tail": _as_list(seo.get("long_tail_keywords")),
        },
        "cta_youtube": ctas.get("youtube"),
        "cta_social": ctas.get("social_media"),
        "tech": {
            "resolution": tech.get("resolution"),
            "frame_rate": tech.get("frame_rate"),
            "software": _as_list(tech.get("software")),
        },
        "ssot_source": str(DEFAULT_BRANDING_PATH),
    }

def apply_quota_budget(
    body: str,
    brand_ctx: dict[str, Any],
    tier: str,
    budget_limit: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preserve complete eligible set; annotate headroom and log quota overages."""
    limit = (
        budget_limit
        if budget_limit is not None
        else DEFAULT_TIER_BUDGETS.get(tier, 500)
    )
    body_chars = len(body)
    remaining = limit - body_chars

    hashtags = {
        "primary": list(brand_ctx["hashtags"]["primary"]),
        "secondary": list(brand_ctx["hashtags"]["secondary"]),
        "boilerplate": list(brand_ctx["hashtags"]["boilerplate"]),
    }
    keywords = {
        "core": list(brand_ctx["keywords"]["core"]),
        "long_tail": list(brand_ctx["keywords"]["long_tail"]),
    }
    cta_social = brand_ctx["cta_social"]

    footer_parts: list[str] = []
    if cta_social:
        footer_parts.append(str(cta_social))
    all_tags = hashtags["boilerplate"] + hashtags["primary"] + hashtags["secondary"]
    if all_tags:
        footer_parts.append(" ".join(all_tags))
    footer_chars = len("\n".join(footer_parts))

    total_projected_chars = body_chars + footer_chars
    exceeded = total_projected_chars > limit
    overage_chars = max(0, total_projected_chars - limit)

    errata: list[str] = []
    if remaining < 0:
        errata.append("ERR_BODY_EXCEEDS_BUDGET")
    if exceeded:
        errata.append("ERR_QUOTA_EXCEEDED_LOGGED")

    full_ctx = {
        "cta_social": cta_social,
        "hashtags": hashtags,
        "keywords": keywords,
        "voice_avoid": brand_ctx["voice"]["avoid"],
    }
    quota_meta = {
        "policy": "populate_annotate_export_log_only",
        "budget_limit": limit,
        "body_chars": body_chars,
        "footer_chars": footer_chars,
        "projected_total_chars": total_projected_chars,
        "remaining_headroom": remaining,
        "exceeded": exceeded,
        "overage_chars": overage_chars,
        "pruned": [],
        "errata": errata,
    }
    return full_ctx, quota_meta

def build_variants(
    clean_brick: dict[str, Any],
    out_dir: Path,
    branding: dict[str, Any],
    taxonomy_rows: list[dict[str, Any]] | None = None,
    budget_overrides: dict[str, int] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """Construct atomic and fancy payloads with full XYZ stress & precedence trace."""
    base_id = clean_brick.get("id", "item")
    motif = clean_brick.get("motif", "unknown")
    target = clean_brick.get("target", "base")
    body = clean_brick.get("body", "")
    legacy_milestone = clean_brick.get("legacy_milestone")

    brand_ctx = extract_brand_context(branding)
    rows = taxonomy_rows or []
    overrides = budget_overrides or {}

    wenwu_brick = clean_brick.get("wenwu") if isinstance(clean_brick.get("wenwu"), dict) else {}
    closer_brick = clean_brick.get("closer") if isinstance(clean_brick.get("closer"), dict) else {}

    xyz_stress = {
        "x_mythos": {
            "motif": motif,
            "target": target,
            "body_len": len(body),
            "has_closer": bool(closer_brick),
        },
        "y_transport": {
            "surface": clean_brick.get("base", "reddit"),
            "stage": "reddit.json",
            "tiers_tested": ["atomic", "fancy"],
        },
        "z_tooling": {
            "taxonomy_db": str(DEFAULT_TAXONOMY_DB_PATH),
            "db_present": bool(rows),
            "tool_count": len(rows),
            "tools_by_tier": {
                tier_code: [r["id"] for r in rows if r.get("tier_id") == tier_code]
                for tier_code in ("P0", "P1", "P2", "P3")
            },
        },
    }

    results: list[tuple[Path, dict[str, Any]]] = []

    for tier in ("atomic", "fancy"):
        fitted_ctx, quota_meta = apply_quota_budget(
            body=body,
            brand_ctx=brand_ctx,
            tier=tier,
            budget_limit=overrides.get(tier),
        )

        selected_cta = (
            brand_ctx.get("cta_youtube") if tier == "fancy" else brand_ctx.get("cta_social")
        )
        tier_brand_ctx = {
            **brand_ctx,
            "cta_social": selected_cta,
        }
        fitted_ctx, quota_meta = apply_quota_budget(
            body=body,
            brand_ctx=tier_brand_ctx,
            tier=tier,
            budget_limit=overrides.get(tier),
)

        precedence_trace = {
            "1_edit": {"required": True, "status": "universal_gate_present"},
            "2_fsr": {
                "applicable": tier == "fancy",
                "status": "gated_deferred_pending_fsr" if tier == "fancy" else "not_applicable_atomic",
            },
            "3_wenwu": {
                "present": bool(wenwu_brick),
                "enabled": bool(wenwu_brick.get("enabled", False)),
                "aligned_with_changes": bool(wenwu_brick.get("aligned_with_changes", False)),
                "pressure_policy": wenwu_brick.get("pressure_policy", "omit"),
            },
            "4_mianzi": {
                "source": "taxonomy.db+branding.yml",
                "weights_populated": any(r.get("mianzi_weight") is not None for r in rows),
                "tool_ids": [r["id"] for r in rows],
            },
        }

        payload: dict[str, Any] = {
            "id": f"{base_id}_{tier}",
            "surface": clean_brick.get("base", "reddit"),
            "stage": "reddit.json",
            "motif": motif,
            "target": target,
            "tier": tier,
            "body": body,
            "brand_ref": {
                "name": brand_ctx["name"],
                "domain": brand_ctx["domain"],
                "ssot_source": brand_ctx["ssot_source"],
            },
            "brand_context": fitted_ctx,
            "quota": quota_meta,
            "taxonomy_ref": {
                "db_path": str(DEFAULT_TAXONOMY_DB_PATH),
                "db_present": bool(rows),
                "tool_count": len(rows),
            },
            "xyz_stress": xyz_stress,
            "precedence": precedence_trace,
            "gates": {
                "allow_template_to_publish": False,
                "web_sense_eligible": False,
            },
            "poc": {
                "edit_phase_skipped": True,
                "illustrated_tier_status": "deferred_pending_fsr",
                "legal_yaml_pass": True,
                "legacy_milestone": legacy_milestone,
                "harness_pass": ["read", "brick", "strip", "post"],
            },
        }

        if tier == "atomic":
            payload["media"] = {"image": None, "video": None, "alt_text": None}
            payload["link"] = None
        else:
            src_media = (
                clean_brick.get("media")
                if isinstance(clean_brick.get("media"), dict)
                else {}
            )
            payload["media"] = {
                "image": None,
                "video": src_media.get("video") or f"assets/{motif}_{target}_stub.mp4",
                "alt_text": src_media.get("alt_text"),
            }
            payload["link"] = clean_brick.get("link") or f"https://{brand_ctx['domain']}"

        out_file = out_dir / f"{motif}_{target}_{tier}.json"
        results.append((out_file, payload))

    return results

def promote_from_bank(
    bank_dir: Path, active_dir: Path, filename: str, dry_run: bool = False
) -> Path:
    """Explicit on-demand promotion (never FIFO/FILO)."""
    src = bank_dir / filename
    dst = active_dir / filename
    if not src.exists():
        raise FileNotFoundError(f"Banked precursor not found: {src}")
    if dry_run:
        print(f"[DRY-RUN][PROMOTE] Would promote {src} -> {dst} (reading in place)")
        return src
    shutil.move(str(src), str(dst))
    print(f"[PROMOTE] {src} -> {dst}")
    return dst


def run_pass(
    sa_dir: Path,
    out_dir: Path,
    precursor_path: Path,
    branding: dict[str, Any],
    taxonomy_rows: list[dict[str, Any]],
    dry_run: bool = False,
    budget_overrides: dict[str, int] | None = None,
) -> list[Path]:
    fsm = PromoteFSM()

    # 1. READ
    fsm.advance("init", "read")
    data = yaml.safe_load(precursor_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Illegal YAML root in {precursor_path}")

    # 2. BRICK
    fsm.advance("read", "brick")
    bricks = build_bricks(data)

    # 3. STRIP
    fsm.advance("brick", "strip")
    raw_exceptions = data.get("_exceptions", [])
    exceptions = set(raw_exceptions) if isinstance(raw_exceptions, list) else set()
    cleaned = strip_invisible_ink(bricks, exceptions)

    # 4. POST
    fsm.advance("strip", "post")
    variants = build_variants(
        cleaned,
        out_dir,
        branding,
        taxonomy_rows=taxonomy_rows,
        budget_overrides=budget_overrides,
    )

    depot_dir = sa_dir / "depot"
    depot_dst = depot_dir / precursor_path.name

    if dry_run:
        print(f"[DRY-RUN][DEPOT]   Would archive {precursor_path} -> {depot_dst}")
        for out_file, payload in variants:
            print(f"[DRY-RUN][EMIT]    Would write {out_file}")
            print(json.dumps(payload, indent=2))
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        for out_file, payload in variants:
            out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"[EMIT]    {out_file}")
        depot_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(precursor_path), str(depot_dst))
        print(f"[DEPOT]   {precursor_path} -> {depot_dst}")

    fsm.advance("post", "done")
    return [out_file for out_file, _ in variants]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kuma PoC PromoteFSM harness")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate YAML, simulate promotion/depot moves, and print JSON without touching disk.",
    )
    parser.add_argument(
        "--promote",
        type=str,
        default="precursor_town.yml",
        help="Banked precursor filename to promote on demand (default: precursor_town.yml).",
    )
    parser.add_argument(
        "--atomic-budget",
        type=int,
        default=None,
        help="Optional character budget override for atomic tier (default: 280).",
    )
    parser.add_argument(
        "--fancy-budget",
        type=int,
        default=None,
        help="Optional character budget override for fancy tier (default: 1000).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mode = "DRY-RUN" if args.dry_run else "LIVE-LOCAL"
    print(f"[MODE]    {mode}")
    print(f"[CWD]     {Path.cwd()}")

    root = resolve_kuma_root()
    sa = root / "sa"
    bank = sa / "bank"
    out = root / "out_poc"

    print(f"[ROOT]    {root}")
    print(f"[SA]      {sa} (exists={sa.exists()})")
    print(f"[OUT]     {out}")

    branding = load_branding_ssot(DEFAULT_BRANDING_PATH)
    taxonomy_rows = load_taxonomy_readonly(DEFAULT_TAXONOMY_DB_PATH)
    print(
        f"[TAXONOMY] {DEFAULT_TAXONOMY_DB_PATH} "
        f"(present={bool(taxonomy_rows)}, rows={len(taxonomy_rows)})"
    )

    budget_overrides: dict[str, int] = {}
    if args.atomic_budget is not None:
        budget_overrides["atomic"] = args.atomic_budget
    if args.fancy_budget is not None:
        budget_overrides["fancy"] = args.fancy_budget

    cli_overrides: dict[str, int] = {}
    if args.atomic_budget is not None:
        cli_overrides["atomic"] = args.atomic_budget
    if args.fancy_budget is not None:
        cli_overrides["fancy"] = args.fancy_budget

    run_from_manifest(
        harness_dir=Path(__file__).resolve().parent,
        kuma_root=root,
        out_dir=out,
        branding=branding,
        taxonomy_rows=taxonomy_rows,
        cli_dry_run=True if args.dry_run else None,
        manifest_filename="prefetch_manifest.yml",
        cli_budget_overrides=cli_overrides or None,
    )

if __name__ == "__main__":
    main()
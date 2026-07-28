#!/usr/bin/env python3
"""
tfc_rum_audit.py — Audit billable Resources Under Management (RUM) in an
HCP Terraform / Terraform Cloud organisation, broken down by provider and
resource type.

It talks to Terraform Cloud through the `tfctl` CLI and reads each workspace's
*current state version*, which already exposes:

  * `billable-rum-count`  — the workspace's billable RUM total
  * `providers`           — map of provider -> { resource_type: instance_count }

so there is NO need to download and parse raw state files.

RUM semantics (verified against the live API):

  * Every *managed* resource instance in state counts as one billable RUM.
  * Data sources (types prefixed `data.`) appear in `providers` but are NOT
    billable, so they are reported separately and excluded from RUM totals.
  * `billable-rum-count` is the authoritative per-workspace figure; the script
    cross-checks it against the sum of non-data resource instances.

Auth: uses tfctl's configured profiles. Point it at a profile whose
`default_organization` is the org you want to audit (see README).

Examples:
  python3 tfc_rum_audit.py                     # audit active profile's org
  python3 tfc_rum_audit.py --profile customer-org  # audit via a specific profile
  python3 tfc_rum_audit.py --provider aci      # focus on Cisco ACI detail
  python3 tfc_rum_audit.py --csv out/          # export CSV detail
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Cisco ACI resource types that are structural RUM multipliers: the generic
# REST escape hatches (one managed object per resource) and the standalone
# relationship objects that can often be collapsed into a typed resource's
# inline `relation_*` arguments.
ESCAPE_HATCH_TYPES = {"aci_rest", "aci_rest_managed"}


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def run_tfctl(args, profile=None):
    """Run a tfctl command, return (parsed_json, error_string_or_None)."""
    cmd = ["tfctl"] + args
    if profile:
        cmd.append(f"--profile={profile}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None, (proc.stderr.strip() or proc.stdout.strip()
                      or f"exit {proc.returncode}")
    out = proc.stdout.strip()
    if not out:
        return None, "empty output"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        # tfctl --jq emits raw (unquoted) text for scalar/string results.
        return out, None


def provider_source(provider_key: str) -> str:
    """Normalise a state provider key to its `org/name` source.

    'provider["registry.terraform.io/ciscodevnet/aci"]' -> 'ciscodevnet/aci'
    'provider["app.terraform.io/my-org/aci"]'            -> 'my-org/aci' (private mirror)
    """
    inner = provider_key
    if inner.startswith('provider["') and inner.endswith('"]'):
        inner = inner[len('provider["'):-2]
    parts = inner.split("/")
    if len(parts) >= 3:
        return "/".join(parts[1:])  # drop the registry host segment
    return inner


def classify_aci_type(rtype: str) -> list[str]:
    """Heuristic optimisation flags for a Cisco ACI managed resource type."""
    flags = []
    if rtype in ESCAPE_HATCH_TYPES:
        flags.append("generic-rest")
    if "relation" in rtype or "_to_" in rtype:
        flags.append("standalone-relation")
    return flags


def list_workspaces(profile=None, page_size=100):
    """Return [{id, name}, ...] across all pages (handles >100 workspaces)."""
    workspaces = []
    page = 1
    while True:
        data, err = run_tfctl(
            ["api", "/organizations/{organization}/workspaces",
             "--page-size", str(page_size), "--page-number", str(page),
             "--jq", "[.data[] | {id: .id, name: .attributes.name}]"],
            profile,
        )
        if err:
            raise RuntimeError(f"listing workspaces failed: {err}")
        if not data:
            break
        workspaces.extend(data)
        if len(data) < page_size:
            break
        page += 1
    return workspaces


def fetch_workspace_state(ws, profile=None):
    """Return RUM + managed/data resource-type counts for one workspace.

    Uses the workspace ID (more robust than name resolution)."""
    data, err = run_tfctl(
        ["api", "/workspaces/{workspace}/current-state-version",
         "-p", f"workspace={ws['id']}",
         "--jq", '.data.attributes | {rum: .["billable-rum-count"], '
                 'processed: .["resources-processed"], providers: .providers}'],
        profile,
    )
    if err:
        return {"workspace": ws, "error": err}

    providers = data.get("providers") or {}
    managed = defaultdict(lambda: defaultdict(int))   # source -> type -> count
    data_srcs = defaultdict(lambda: defaultdict(int))  # source -> type -> count
    managed_total = 0
    for pkey, types in providers.items():
        src = provider_source(pkey)
        for rtype, count in (types or {}).items():
            count = int(count)
            if rtype.startswith("data."):
                data_srcs[src][rtype[len("data."):]] += count
            else:
                managed[src][rtype] += count
                managed_total += count
    return {
        "workspace": ws,
        "rum": data.get("rum"),
        "processed": data.get("processed"),
        "managed": managed,
        "data": data_srcs,
        "managed_total": managed_total,
    }


def audit(profile=None, workers=4):
    workspaces = list_workspaces(profile)
    results, skipped = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_workspace_state, ws, profile): ws
                   for ws in workspaces}
        for fut in as_completed(futures):
            res = fut.result()
            if res.get("error"):
                skipped.append(res)
            else:
                results.append(res)
            done += 1
            eprint(f"\r  scanned {done}/{len(workspaces)} workspaces "
                   f"(skipped {len(skipped)})", end="")
    eprint()
    return workspaces, results, skipped


def aggregate(results):
    by_provider = defaultdict(int)                 # source -> managed RUM
    by_type = defaultdict(lambda: defaultdict(int))  # source -> type -> count
    data_by_provider = defaultdict(int)
    total_rum = 0
    total_managed = 0
    total_data = 0
    for res in results:
        total_rum += res.get("rum") or 0
        total_managed += res.get("managed_total") or 0
        for src, types in res["managed"].items():
            for rtype, count in types.items():
                by_provider[src] += count
                by_type[src][rtype] += count
        for src, types in res["data"].items():
            for rtype, count in types.items():
                data_by_provider[src] += count
                total_data += count
    return {
        "by_provider": by_provider,
        "by_type": by_type,
        "data_by_provider": data_by_provider,
        "total_rum": total_rum,
        "total_managed": total_managed,
        "total_data": total_data,
    }


def pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def print_report(org_name, agg, results, skipped, provider_filter=None, top=15):
    by_provider = agg["by_provider"]
    by_type = agg["by_type"]
    total_rum = agg["total_rum"]
    total_managed = agg["total_managed"]

    print("=" * 64)
    print(f"TFC RUM AUDIT — org: {org_name}   workspaces scanned: {len(results)}")
    print("=" * 64)
    print(f"Total billable RUM (sum of billable-rum-count): {total_rum}")
    print(f"Total managed resource instances (computed):     {total_managed}")
    print(f"Data sources (non-billable, excluded):           {agg['total_data']}")
    if skipped:
        print(f"Skipped workspaces (no state / no access):       {len(skipped)}")
    print()

    providers = sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)
    if provider_filter:
        providers = [(s, c) for s, c in providers
                     if provider_filter.lower() in s.lower()]

    print("RUM BY PROVIDER (managed resources only)")
    print("-" * 64)
    print(f"{'provider':40} {'RUM':>10} {'%':>7}")
    print("-" * 64)
    for src, count in providers:
        print(f"{src:40} {count:>10} {pct(count, total_managed):>6.1f}%")
    print()

    # Per-type detail for matched providers.
    matched = [s for s, _ in providers]
    print(f"TOP RESOURCE TYPES (managed, matched providers; top {top})")
    print("-" * 64)
    rows = []
    for src in matched:
        for rtype, count in by_type[src].items():
            rows.append((count, src, rtype))
    rows.sort(reverse=True)
    print(f"{'type':46} {'provider':16} {'count':>8}")
    print("-" * 64)
    for count, src, rtype in rows[:top]:
        print(f"{rtype:46} {src:16} {count:>8}")
    print()

    # ACI optimisation-candidate analysis.
    aci_sources = [s for s in by_provider if "aci" in s.lower()]
    if aci_sources:
        print("CISCO ACI — OPTIMISATION CANDIDATES")
        print("-" * 64)
        cand_relation = cand_rest = aci_total = 0
        for src in aci_sources:
            aci_total += by_provider[src]
            for rtype, count in sorted(by_type[src].items(),
                                       key=lambda kv: kv[1], reverse=True):
                flags = classify_aci_type(rtype)
                if "standalone-relation" in flags:
                    cand_relation += count
                if "generic-rest" in flags:
                    cand_rest += count
                flag_str = ("  [" + ",".join(flags) + "]") if flags else ""
                print(f"  {rtype:48} {count:>7}{flag_str}")
        cand_total = cand_relation + cand_rest
        print("-" * 64)
        print(f"  Standalone relation resources : {cand_relation} RUM "
              f"(collapse into typed resources' inline relation_* args)")
        print(f"  Generic REST escape hatch       : {cand_rest} RUM "
              f"(prefer typed resources over aci_rest/_managed)")
        print(f"  Candidate total                 : {cand_total} RUM "
              f"({pct(cand_total, aci_total):.1f}% of {aci_total} ACI RUM)")
        print()

    if skipped:
        print("SKIPPED WORKSPACES")
        print("-" * 64)
        for s in skipped:
            print(f"  {s['workspace']['name']:30} ({s['workspace']['id']}): "
                  f"{s['error'][:60]}")
        print()


def export_csv(outdir, agg, results, provider_filter=None):
    os.makedirs(outdir, exist_ok=True)
    by_provider = agg["by_provider"]
    by_type = agg["by_type"]
    total_managed = agg["total_managed"]

    p1 = os.path.join(outdir, "rum_by_provider.csv")
    with open(p1, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", "rum", "pct_of_managed"])
        for src, count in sorted(by_provider.items(),
                                 key=lambda kv: kv[1], reverse=True):
            w.writerow([src, count, f"{pct(count, total_managed):.2f}"])

    p2 = os.path.join(outdir, "rum_by_type.csv")
    with open(p2, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", "type", "count", "aci_candidate_flags"])
        for src in sorted(by_type):
            for rtype, count in sorted(by_type[src].items(),
                                       key=lambda kv: kv[1], reverse=True):
                w.writerow([src, rtype, count,
                            ",".join(classify_aci_type(rtype))])

    p3 = os.path.join(outdir, "rum_by_workspace.csv")
    with open(p3, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["workspace", "workspace_id", "billable_rum",
                    "managed_instances", "top_provider"])
        for res in sorted(results, key=lambda r: r.get("rum") or 0,
                          reverse=True):
            top_src = ""
            if res["managed"]:
                top_src = max(
                    ((sum(t.values()), s) for s, t in res["managed"].items())
                )[1]
            w.writerow([res["workspace"]["name"], res["workspace"]["id"],
                        res.get("rum") or 0, res.get("managed_total") or 0,
                        top_src])
    eprint(f"CSV written: {p1}\n             {p2}\n             {p3}")


def get_org_name(profile=None):
    data, err = run_tfctl(
        ["api", "/organizations/{organization}",
         "--jq", ".data.attributes.name"], profile)
    if err or not data:
        return "(unknown — check profile default_organization)"
    return data


def main():
    ap = argparse.ArgumentParser(
        description="Audit HCP Terraform / TFC billable RUM by provider and "
                    "resource type, via tfctl.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--profile", default=None,
                    help="tfctl profile to use (its default_organization is "
                         "the audited org). Default: active profile.")
    ap.add_argument("--provider", default=None,
                    help="Substring filter for the provider/type breakdown "
                         "(e.g. 'aci', 'ciscodevnet', 'aws').")
    ap.add_argument("--workers", type=int, default=4,
                    help="Concurrent tfctl calls (default 4).")
    ap.add_argument("--top", type=int, default=15,
                    help="Rows to show in the top-resource-types table.")
    ap.add_argument("--csv", metavar="OUTDIR", default=None,
                    help="Export by-provider/by-type/by-workspace CSV detail.")
    args = ap.parse_args()

    org_name = get_org_name(args.profile)
    eprint(f"Auditing org '{org_name}' (profile: "
           f"{args.profile or 'active'}) ...")
    try:
        workspaces, results, skipped = audit(args.profile, args.workers)
    except RuntimeError as exc:
        eprint(f"ERROR: {exc}")
        eprint("Hint: set the target org on the profile first:  "
               "tfctl profile set default_organization <ORG>")
        sys.exit(1)

    agg = aggregate(results)
    print_report(org_name, agg, results, skipped,
                 provider_filter=args.provider, top=args.top)
    if args.csv:
        export_csv(args.csv, agg, results, provider_filter=args.provider)


if __name__ == "__main__":
    main()

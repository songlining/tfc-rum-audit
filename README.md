# tfc-rum-audit

> Audit **billable Resources Under Management (RUM)** across every workspace in an
> HCP Terraform / Terraform Cloud organisation — broken down **by provider and by
> resource type** — using the [`tfctl`](#tfctl--the-cli-underneath) CLI.

Terraform Cloud bills you per **managed resource** in state (RUM), per hour. Fine-grained
providers — network-automation providers are the classic example — can turn a modest design
into tens of thousands of billable resources, and the cost is hard to see until the invoice
arrives. This tool answers one question directly:

> **Which providers, and which resource types, are actually driving my RUM bill?**

It reads each workspace's *current state version* through the Terraform Cloud API and
aggregates managed-resource instances by provider and type. **No state-file download or
parsing is required** — the state-version API already exposes a `billable-rum-count` and a
`providers` breakdown per workspace.

```text
================================================================
TFC RUM AUDIT — org: example-org   workspaces scanned: 104
================================================================
Total billable RUM (sum of billable-rum-count): 36013
Total managed resource instances (computed):     36013
Data sources (non-billable, excluded):           412

RUM BY PROVIDER (managed resources only)
----------------------------------------------------------------
provider                                        RUM       %
----------------------------------------------------------------
ciscodevnet/aci                               21055     58.5%
hashicorp/aws                                  8021     22.3%
hashicorp/azurerm                              5120     14.2%
...

CISCO ACI — REVIEW CANDIDATES (sprawl indicators, not auto-removable)
----------------------------------------------------------------
  aci_epg_to_contract                               4210  [standalone-relation]
  aci_rest_managed                                  1830  [generic-rest]
  ...
  standalone-relation : 6040 RUM — relations as separate resources.
  generic-rest        : 1830 RUM — aci_rest / aci_rest_managed (NOT per-object waste).
```

*(Numbers above are illustrative.)*

---

## Why this exists

Under Terraform Cloud's **Resource Under Management** pricing you pay for *every* resource
with `mode: managed` in a state file — including generated/child resources and free or idle
ones — billed the same as a full cloud resource. Providers that map a deep, relationship-heavy
object model one-resource-per-object (the Cisco ACI provider is a well-known example: one
Terraform resource per ACI managed object, plus standalone `*_to_*` / `relation_*` relationship
resources, plus generic `aci_rest` / `aci_rest_managed` escape hatches) can dominate a RUM bill
without anyone noticing.

The TFC **Explorer** shows a per-workspace "current RUM count", and the billing page shows the
monthly high-water mark — but neither breaks the total down by provider or resource type. This
tool fills that gap so you can see *what to optimise* before you optimise it.

## Features

- **Org-wide RUM by provider**, with share-of-total percentages.
- **Top resource types** by instance count (the real cost drivers).
- **Cisco ACI review-candidate analysis** — itemises the resource *categories* most often
  associated with ACI RUM sprawl, so you know where to look first:
  - *standalone relation* resources (`aci_epg_to_contract`, `aci_relation_from_*`, `*_to_*`, …).
    These are flagged for review, **not** assumed removable — the old inline `relation_fv_rs_*`
    arguments are largely deprecated and v2.16 *added* these standalone resources, so they are
    frequently intentional. They are only reducible where a supported inline equivalent exists
    **and** the parent object is managed in the same state.
  - *generic REST escape-hatch* resources (`aci_rest`, `aci_rest_managed`). These are **not**
    a per-object RUM source (1:1 with typed resources); `aci_rest_managed` can actually
    *consolidate* a parent plus its children into one resource and **reduce** RUM.

  The tool flags and totals these categories but deliberately does **not** claim a "%
  collapsible" figure, because that cannot be determined from state alone.
- **Correct RUM semantics** — data sources (`data.*`) are *not* billable and are excluded
  (reported separately). The computed managed-instance total is cross-checked against each
  workspace's authoritative `billable-rum-count`.
- **Per-workspace breakdown** and **CSV export** for further analysis.
- **Handles large orgs** — paginates past 100 workspaces, runs lookups concurrently, and
  skips workspaces with no state or no access instead of failing.
- **Private-registry aware** — provider sources are normalised, so a private mirror such as
  `app.terraform.io/<org>/aci` is reported as `<org>/aci`.

---

## Prerequisites

1. **`tfctl`** — the HCP Terraform CLI (see below). Must be on your `PATH`.
2. **Python 3.8+** — the tool uses only the standard library (no `pip install` needed).
3. **A Terraform Cloud token** with read access to the target org (org, team, or user token).

## `tfctl` — the CLI underneath

`tfc-rum-audit` shells out to **`tfctl`**, a single-binary CLI that gives full coverage of the
Terraform Cloud **v2 API**. It handles authentication, profile/org resolution, and JSON:API
envelope parsing, so the audit script stays small.

Key ideas you need for this tool:

- **Profiles.** `tfctl` authenticates against `app.terraform.io` (or a TFE hostname) using
  named profiles. Each profile has a token and a `default_organization`.

  ```bash
  tfctl login                                   # authenticate (creates/updates a profile)
  tfctl profile display                         # show active profile + default org
  tfctl profile profiles list                   # list configured profiles
  tfctl profile set default_organization <ORG>  # point the active profile at an org
  ```

- **The `{organization}` placeholder resolves only from the profile's `default_organization`.**
  There is no per-command `--organization` flag. To audit a *different* org, either set it on
  the active profile, or use a separate profile and pass `--profile <name>` to the tool.

- **Arbitrary API calls.** `tfctl api <path>` calls any v2 endpoint, with `--jq '<expr>'` to
  extract fields (built-in gojq — never pipe `tfctl` output to an external `jq`).

  ```bash
  # The two calls this tool makes per org / per workspace:
  tfctl api /organizations/{organization}/workspaces \
      --page-size 100 --jq '[.data[] | {id: .id, name: .attributes.name}]'

  tfctl api /workspaces/{workspace}/current-state-version -p workspace=<WS_ID> \
      --jq '.data.attributes | {rum: .["billable-rum-count"], providers: .providers}'
  ```

### The `tfctl` agent SKILL

This tool was built with (and pairs well with) a **pi coding-agent skill for `tfctl`** — a
`SKILL.md` that teaches an AI agent how to drive `tfctl` correctly and safely. If you use a
pi-compatible agent, the skill gives it:

- **Hard rules** — e.g. *never* pipe `tfctl` JSON to an external `jq` (use the built-in `--jq`),
  *never* issue `-X DELETE` (deletes require a human), resolve names with `-p` instead of a
  separate lookup, and trust the first answer (`data: []` / "not found" *is* the answer — don't
  re-query or pivot to a different resource).
- **A cookbook** of one-line answers for common tasks (count workspaces, find by name/repo,
  current run status, state-version lookup, varsets, policy checks, …).
- **URL-shape guidance** — per-workspace sub-resources live at `/workspaces/{workspace}/...`,
  *not* `/organizations/{org}/workspaces/{name}/...` (with the `/runs/{id}/policy-checks`
  exception).
- **Exit-code and pagination quirks** — e.g. `--all` can choke on some envelopes; use
  `--page-size 100` instead (this tool does exactly that).
- **A self-improvement protocol** — durable gotchas are appended to a `LEARNINGS.md` sidecar and
  periodically consolidated back into the skill.

In short: the SKILL makes an agent a competent `tfctl` operator; this tool is a ready-made,
repeatable application of the two API calls that matter for RUM analysis.

> **Don't have `tfctl`?** The method is portable. The tool only needs two Terraform Cloud v2
> calls — `GET /api/v2/organizations/:org/workspaces` (paged) and
> `GET /api/v2/workspaces/:id/current-state-version` — reading the `billable-rum-count` and
> `providers` attributes. You can reimplement the aggregation against the raw API (or with
> `curl` + a token) using the same semantics documented below.

---

## Installation

```bash
git clone https://github.com/songlining/tfc-rum-audit.git
cd tfc-rum-audit
chmod +x tfc_rum_audit.py
```

No Python dependencies beyond the standard library.

## Usage

Point a `tfctl` profile at the org you want to audit, then run:

```bash
# Audit the org on the active tfctl profile
python3 tfc_rum_audit.py

# Audit via a specific profile (its default_organization is the audited org)
python3 tfc_rum_audit.py --profile customer-org

# Focus the provider/type breakdown (substring match) — e.g. Cisco ACI
python3 tfc_rum_audit.py --provider aci

# Export per-provider / per-type / per-workspace detail to CSV
python3 tfc_rum_audit.py --csv out/

# Tune concurrency and table size
python3 tfc_rum_audit.py --workers 8 --top 25
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--profile NAME` | active profile | `tfctl` profile to use; its `default_organization` is the audited org. |
| `--provider SUBSTR` | *(all)* | Substring filter for the provider/type breakdown (e.g. `aci`, `ciscodevnet`, `aws`). |
| `--workers N` | `4` | Concurrent `tfctl` calls. |
| `--top N` | `15` | Rows shown in the top-resource-types table. |
| `--csv OUTDIR` | *(off)* | Write `rum_by_provider.csv`, `rum_by_type.csv`, `rum_by_workspace.csv`. |

### Example: auditing a customer org

```bash
# One-time: create/point a profile at the org and authenticate
tfctl login
tfctl profile set default_organization example-org

# Run the audit
python3 tfc_rum_audit.py --provider aci --csv out/
```

## Sample output

`examples/sample-report.txt` is a full sample run against synthetic workspaces that
mirror a Cisco-ACI-heavy estate (ACI ~60% of ~37k RUM), so you can see the exact
report format - including the Cisco ACI review-candidates section - without a
live org. The matching `examples/sample_rum_by_*.csv` files show the CSV export
format. All numbers are illustrative.

## Output explained

- **Total billable RUM** — the sum of each workspace's authoritative `billable-rum-count`.
- **Total managed resource instances (computed)** — the tool's own sum of non-`data.*`
  instances. This should equal the billable total; a mismatch is worth investigating.
- **Data sources (non-billable, excluded)** — `data.*` objects are not charged and are kept
  out of every RUM figure.
- **RUM BY PROVIDER** — managed instances grouped by provider source, with share of total.
- **TOP RESOURCE TYPES** — the individual resource types consuming the most RUM.
- **CISCO ACI — REVIEW CANDIDATES** — when an ACI provider is present, the resource categories
  most associated with RUM sprawl (standalone relation resources, generic REST escape hatches)
  are itemised and totalled **as review indicators**, with honest notes on when each is — and is
  not — actually reducible. The defensible levers (bundle children into `aci_rest_managed`
  `child{}` blocks, don't manage APIC auto-created children, don't model defaults) are printed
  alongside.
- **SKIPPED WORKSPACES** — workspaces with no current state version or insufficient access;
  they are excluded, not fatal.

## RUM semantics (the important bit)

Verified against the live API:

- A resource counts toward RUM if it is **managed** (`mode: "managed"`) and recorded in a state
  file. You are billed per resource **instance** per hour — so `count` / `for_each` each add up.
- **Data sources are not billable.** The state-version `providers` map *includes* them (as
  `data.<type>`); this tool strips the `data.` prefix and reports them separately, never in RUM.
- The per-workspace **`billable-rum-count` is authoritative**. The tool cross-checks it against
  the computed managed-instance sum so you can trust the breakdown.
- **Splitting workspaces does not reduce total RUM** — the same objects are still managed, just
  across more workspaces. (Common misconception; it helps plan/apply performance, not cost.)

## How it works

1. **List workspaces** — `GET /organizations/{organization}/workspaces`, paged at 100.
2. **Per workspace** — `GET /workspaces/{id}/current-state-version`, reading
   `billable-rum-count`, `resources-processed`, and `providers`. Lookups run concurrently and
   workspaces that error are recorded as skipped.
3. **Aggregate** — provider keys like `provider["registry.terraform.io/ciscodevnet/aci"]` are
   normalised to `org/name` (dropping the registry host, so private mirrors work); `data.*`
   types are separated; managed instances are summed by provider and by `(provider, type)`.
4. **Report / export** — console tables plus optional CSV.

## Limitations

- Reads the **current** state version only — a point-in-time snapshot, not a time series.
- Requires read access to the target org (and to each workspace's state version).
- The ACI flags are **heuristics** that identify resource *categories* worth reviewing, not
  resources that are safe to remove. State alone cannot tell whether a standalone relation has a
  supported inline equivalent or whether an object is intentionally managed — validate any change
  in a sandbox first.
- The tool reads data; it never mutates anything (no plans, applies, or deletes).

## Contributing

Issues and PRs welcome. Ideas: time-series trend capture, per-workspace provider tables in the
console output, additional provider-specific optimisation heuristics, and a tfctl-free mode that
talks to the TFC API directly with a token.

## License

[Mozilla Public License 2.0](LICENSE) — consistent with the `tfctl` ecosystem.

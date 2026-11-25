# tfctl — Learnings Inbox

Append-only inbox of durable tfctl gotchas captured during sessions, awaiting
consolidation into `SKILL.md`. This file is the **capture** layer; `SKILL.md` is
the **consolidated** layer. See the "Self-improvement protocol" section of
`SKILL.md` for the rules.

## Format

One line per learning:

```
- [yyyy-mm-dd] [category] one-sentence insight · cmd: `tfctl ...` · exit: N
```

`category` ∈ `path | flag | exit-code | auth | pagination | resolution | other`

## Consolidation

Run `/skill-improve tfctl` to fold entries below into `SKILL.md`, then remove the
consolidated lines from this file. Consolidate when this list grows past ~10
entries or periodically (e.g. monthly).

## Inbox

<!-- Append new learnings below this line. Oldest at top. -->
- [2026-05-21] [path] listStateVersions is GET /state-versions with query filters filter[workspace][name] & filter[organization][name] (NOT /workspaces/{workspace}/state-versions, which 404s) · cmd: `tfctl api /state-versions -f 'filter[workspace][name]=NAME' -f 'filter[organization][name]=ORG'` · exit: 0
- [2026-05-21] [other] state-version attributes include billable-rum-count (int), resources[] array, terraform-version, created-at — no need to download state files to count RUMs · cmd: `tfctl api /state-versions ... --jq '.data[].attributes.["billable-rum-count"]'` · exit: 0
- [2026-07-29] [auth] tfctl profile get token returns empty (token stored "(sensitive)"); for curl state downloads read the token from ~/.terraform.d/credentials.tfrc.json via jq '.credentials["app.terraform.io"].token' · cmd: tfctl profile get token · exit: 0
- [2026-07-29] [path] /workspaces/{workspace}/state-versions (historical list) 404s without the "Read State Versions" team permission, even though current-state-version works · cmd: tfctl api /workspaces/{workspace}/state-versions -p workspace=NAME · exit: 2
- [2026-07-29] [other] state content is downloadable via current-state-version's hosted-state-download-url + Bearer token; jq '.resources[].type' tallies types; filter startswith("aci_") for Cisco ACI; instance counts approximate RUM (billed RUM is TFC consumption/billing data, not state) · cmd: curl -H "Authorization: Bearer $TOK" <url> | jq '.resources[].type' · exit: 0
- [2026-09-02] [path] configuration-version download-url attribute is null on completed runs; use /configuration-versions/{cv}/download which streams the .tar.gz binary directly · cmd: `tfctl api /configuration-versions/cv-XXX/download > cv.tar.gz` · exit: 0
- [2026-09-25] [exit-code] current-state-version returns exit 2 "Resource or path not found" for a workspace with no state version yet (seen on a CLI-driven workspace); tolerate when looping over all workspaces rather than reading it as an auth/naming failure · cmd: `tfctl api /workspaces/{workspace}/current-state-version -p workspace=NAME` · exit: 2
- [2026-11-26] [flag] default secret redaction and the --no-redact escape hatch exist only in upstream main (added 2026-08-14, after v0.4.0); v0.4.0 rejects the flag, so redaction cannot be disabled on this build · cmd: `tfctl api /account/details --no-redact` · exit: 1

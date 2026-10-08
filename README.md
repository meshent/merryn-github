# merryn-github

Merryn's reusable GitHub Actions workflows. Your repository keeps a thin wrapper that names its own values; the
mechanics live here, in one place, so every tenant deploys the same way and a fix lands once.

Merryn is the public name of [Mira](https://github.com/meshent/Mira), the work-coordination engine for fleets of
agents: one queue, atomic pull-and-lease, a live board, and a one-way mirror into a docs repository. A **tenant** is one
hosted instance, described by a bundle (`tenants/<id>.json`) in its host repository.

## `deploy-host.yml` — deploy a tenant host

Ships a tenant's host to its Azure App Service after its tests: reads where the tenant lives from the bundle, signs in
with Azure OIDC (no stored secret), publishes the host, deploys it as a run-from-package zip, then waits for `/health`
to name the new build and for `/tenant` to report `seeded: true`. Put this wrapper in the host repository:

```yaml
# .github/workflows/deploy-host.yml
name: Deploy Merryn host
on:
  push:
    branches: [main]
    paths: ["src/Mira.Host/**", ".github/workflows/deploy-host.yml"]
  workflow_dispatch: {}
concurrency:
  group: deploy-merryn-<id>
  cancel-in-progress: false
permissions:
  id-token: write
  contents: read
  packages: read
jobs:
  deploy:
    uses: meshent/merryn-github/.github/workflows/deploy-host.yml@main
    with:
      bundle: src/Mira.Host/tenants/<id>.json
      project: src/Mira.Host/Mira.Host.csproj
      test-path: Tests/UnitTests/Mira.Host.UnitTests
```

| input | meaning |
|---|---|
| `bundle` | the tenant bundle; `host.subscription`, `host.resourceGroup`, `host.siteName` and `publicUrl` are read from it |
| `project` | the host project to publish |
| `test-path` | test project or solution that gates the deploy; empty skips the gate |
| `dotnet-version` | default `10.0.x` |
| `health-timeout` | seconds, in all, for the new build to answer `/health`; default 900. The zip deploys `--async` (Azure's own start-up poll gave false failures on F1), so this wait is the check; on timeout it prints the last `/health` answer |
| secret `packages-token` | read:packages on the feed that publishes Mira.Core and Mira.Cosmos, when the host repository is outside the org that owns it; otherwise the workflow's own token is enough |

**Identity.** The workflow signs in through the caller repository's variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`
and `AZURE_SUBSCRIPTION_ID`, which name an app registration with a federated credential for the repository's deploy
branch and the **Website Contributor** role on the one site. The reference host's `infra/bringup.sh` creates all of
that from the bundle (and the rest of the tenant); an existing tenant adopts the workflow with
`infra/bringup.sh tenants/<id>.json --identity-only --run-deploy`. Two details it handles so you do not have to: the
federated credential is created in both subject forms GitHub presents (`repo:<owner>/<repo>:ref:refs/heads/<branch>` and,
for an org with the customized OIDC subject, `repo:<owner>@<org id>/<repo>@<repo id>:ref:…`), and the role is scoped to
the site, not the subscription. While `AZURE_CLIENT_ID` is unset the deploy job skips, so the wrapper can land first.

The workflow refuses to deploy when the bundle's subscription differs from `AZURE_SUBSCRIPTION_ID`: the identity was
granted in one subscription, and a bundle pointing elsewhere is a mistake, not a request.

## `actions-budget-gate.yml` — observe the Actions allowance

One short job reads the current UTC month's organization usage, writes `ACTIONS_BUDGET_STATE`
(`ok`, `soft`, `hard`) and `ACTIONS_BUDGET_USED_PERCENT`, and optionally reports state changes to Merryn.
It must run even at `hard`, so the following month can reopen the gate. It never changes GitHub's spending limit.

```yaml
jobs:
  budget:
    uses: meshent/merryn-github/.github/workflows/actions-budget-gate.yml@main
    with:
      org: meshent
      merryn-api-url: https://meshnet.merryn.dev/api/v1
    secrets:
      billing-token: ${{ secrets.MESHENT_BILLING_PAT }}
      merryn-token: ${{ secrets.MERRYN_TOKEN }}
```

| input | meaning |
|---|---|
| `org` | required organization name |
| `soft-percent`, `hard-percent` | defaults 70 and 90; require `0 < soft < hard <= 100`; boundaries are inclusive |
| `included-minutes` | default 0 reads the org's Free/Team/Enterprise plan (2,000/3,000/50,000); supply a positive allowance for other plans |
| `dry-run` | default false; true reports outputs without writing variables or sending notifications |
| `merryn-api-url` | optional HTTPS REST base, e.g. `https://meshnet.merryn.dev/api/v1`; use a token scoped to the intended project |
| `merryn-domain` | defaults `coordinator`; journal receives the state-change note, and a hard state files an owner question |
| secret `billing-token` | required: billing-read and organization-variable-write access |
| secret `merryn-token` | optional together with URL: bearer token with `work` access to the intended project |

The billing token needs fine-grained organization **Administration: read** and **Variables: write** permissions;
a classic PAT needs `admin:org` (and `repo` for private repositories). Confirm the credential can read usage and
create/update org variables before activation. The workflow's own `GITHUB_TOKEN` cannot replace it.
[Billing API](https://docs.github.com/en/rest/billing/usage),
[variable permissions](https://docs.github.com/en/rest/actions/variables).

Enhanced billing returns physical runner quantities by SKU. This implementation applies the approved release
plan's standard allowance multipliers (Linux 1, Windows 2, macOS 10) once; storage and other products are excluded.
It refuses unknown runner SKUs rather than guessing their allowance contribution. The legacy endpoint's
`total_minutes_used` is already weighted and is not multiplied again; fallback occurs only for 404/410, never a
credential error or server failure. The legacy endpoint currently returns 410 for meshent. Missing data, invalid
quantities, and a mismatched month fail without resetting the saved gate. Review accounting before enabling it
for other runner SKUs or a customized plan; these multipliers are not the current per-minute dollar prices.

On a notification failure, `ACTIONS_BUDGET_NOTIFIED_STATE` remains owed, so the next observation retries even
when the gate state was already saved. Optional alerts need both URL and token. No secrets or response bodies
are logged. Outputs `state` and `used-percent` are available to the caller; use `needs.budget.outputs.state` to
gate later jobs in that same run, since `vars` is evaluated before the observer updates the org variable.

Other workflows add a job condition (the budget observer itself is exempt):

```yaml
if: ${{ vars.ACTIONS_BUDGET_STATE != 'hard' }}
# PR validation also requires vars.ACTIONS_BUDGET_STATE != 'soft'.
```

The train's explicit override and the small daily wrapper are separate caller work. A normal observe can change
all adopting workflows' eligibility; test `dry-run: true` first. Test forced thresholds with the deterministic API
fixtures (`node --test test/actions-budget-gate.test.mjs`), rather than setting the production org to hard as a test.
“Done” requires a real authorized observation updating variables plus the wrapper/enforcement adoption; a
source-only PR does not prove activation. The approved approximately $20 spending limit is an owner billing
setting and is not changed by this workflow.

## Versioning

Reference `@main` for the current contract, or pin a tag. Inputs are only ever added; a renamed or removed input is a
new major tag.

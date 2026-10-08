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

## `actions-budget-gate.yml` — throttle before GitHub locks the organization out

Reads the organization's Actions usage for the month and sets one organization-level variable,
`ACTIONS_BUDGET_STATE`, to `ok`, `soft` (70 % of included minutes) or `hard` (90 %). Every workflow's jobs then
carry a condition GitHub evaluates without starting a runner, so a skipped job costs nothing and a repository that
has never seen the gate keeps running (an unset variable reads as empty):

```yaml
jobs:
  build:
    if: ${{ vars.ACTIONS_BUDGET_STATE != 'hard' }}            # PR validation jobs also add: && vars.ACTIONS_BUDGET_STATE != 'soft'
```

One scheduled wrapper per organization, in whichever repository holds the organization's automation. It must not
carry the guard itself, or a hard state could never be lifted. Twice a day is about 60 billed minutes a month:

```yaml
# .github/workflows/actions-budget.yml
name: Actions budget
on:
  schedule:
    - cron: "17 5,17 * * *"
  workflow_dispatch: {}
permissions: {}
jobs:
  gate:
    uses: meshent/merryn-github/.github/workflows/actions-budget-gate.yml@main
    with:
      org: meshent
    secrets:
      billing-token: ${{ secrets.MESHENT_BILLING_PAT }}
```

| input | meaning |
|---|---|
| `org` | the organization whose usage is read and whose variable is set |
| `soft-percent`, `hard-percent` | thresholds; default 70 and 90 |
| `included-minutes` | the plan's monthly allowance; default 0 reads it from the legacy billing endpoint (Free 2000, Team 3000). Set it when that endpoint reports nothing |
| `variable` | the variable name; default `ACTIONS_BUDGET_STATE`. A second variable, `<name>_USED_PERCENT`, carries the percent |
| secret `billing-token` | a token of an organization owner or billing manager: it reads billing and writes organization Actions variables (classic scopes `admin:org`); the default `GITHUB_TOKEN` can do neither |

**Sources.** The legacy endpoint (`GET /orgs/{org}/settings/billing/actions`) gives used and included minutes;
organizations moved to the enhanced billing platform may report stale totals there, so the enhanced usage endpoint
(`GET /organizations/{org}/settings/billing/usage` for the month) is read too and the larger total wins. If neither
answers, the variable is left as it was and the run warns. The run summary carries used, included, percent, days
left in the month and the projected month at the current burn; a change of state is a warning annotation and a hard
state is an error annotation, so it shows in the run list.

**Outputs.** `state` and `used-percent`, so a release train can read the gate in its first step and refuse to
start on `hard` unless dispatched with an override.

## Planned: release trains and an Actions budget

[docs/release-trains.md](docs/release-trains.md) is the plan for `dotnet-release-train.yml` (one scheduled run that
builds, tests, packs and publishes every repository of a tenant in dependency order, then pins and deploys its hosts),
`dotnet-validate.yml` (the one opt-in PR check); `actions-budget-gate.yml` above is the first piece to land.
Filed as work on the meshNet and Mira boards.

## Versioning

Reference `@main` for the current contract, or pin a tag. Inputs are only ever added; a renamed or removed input is a
new major tag.

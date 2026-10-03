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
| `health-attempts` | 5-second waits for the new build to answer `/health`; default 30 |
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

## Versioning

Reference `@main` for the current contract, or pin a tag. Inputs are only ever added; a renamed or removed input is a
new major tag.

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

## `dotnet-release-train.yml`: one train for every package repository

Builds, tests and publishes every package repository of a tenant in **one job**: one runner, one `setup-dotnet`, one
warm NuGet cache, and no per-job minute rounding. The order comes from the code, not from a list someone keeps: the
train reads every `.csproj` in the manifest's repositories, and a `PackageReference` to a package that another
manifest repository produces counts as an edge. Repositories are sorted on those edges, then the projects inside each
one. A cycle fails the run and names the cycle.

A repository is **in the train** when its `ref` branch (default `release`) has moved since its latest `train/*` tag,
or when it has no tag yet. A selected repository republishes all of its packages. The train derives **one version**
from the clock with the `Directory.Build.props` formula (UTC, one reading), passes it to every pack as
`VersionBuild`/`VersionRevision`, and uses it as the train id. It never types a version.

For each selected repository, bottom-up, the train:
1. Adds a runner-local feed to the repository's `nuget.config`. The edit is never committed, and under
   `packageSourceMapping` each train package id maps to both the local feed and the GitHub source. A downstream
   repository therefore restores the upstream that was packed minutes earlier.
2. Runs the repository's tests from source. It passes `-p:UseProjectReferences=true` when `Directory.Build.targets`
   defines that switch.
3. Packs every packable project into the local feed, **without** the switch, from a copy of the repository taken
   before anything was built.
4. Checks each package before moving on. Every package must carry the train version, and every in-train dependency
   in its nuspec must name that same version. A package that restored a sibling from outside the train fails here.

Only when every selected repository has passed does the train push the packages, in the same order, with
`--skip-duplicate`. It then fast-forwards `main` to `ref` in each repository (a plain push, never forced) and tags
`train/<version>`. The plan step refuses a repository whose `main` is not an ancestor of `ref`, so a train never
publishes something it cannot promote. A failure anywhere publishes nothing.

Put the wrapper in the tenant's aggregator repository (meshNet for meshNet):

```yaml
# .github/workflows/release-train.yml
name: Release train
on:
  schedule:
    - cron: "0 2 * * 2,5"          # Tue and Fri 02:00 UTC
  workflow_dispatch:
    inputs:
      dry-run: { type: boolean, default: true }
      override: { type: boolean, default: false, description: "Run even at ACTIONS_BUDGET_STATE=hard (hotfix)" }
permissions:
  contents: read
jobs:
  train:
    uses: meshent/merryn-github/.github/workflows/dotnet-release-train.yml@main
    with:
      manifest: release-train.json
      dry-run: ${{ github.event_name != 'workflow_dispatch' || inputs.dry-run }}
      override: ${{ github.event_name == 'workflow_dispatch' && inputs.override }}
    secrets:
      repos-token: ${{ secrets.MESHENT_CI_PAT }}
      packages-token: ${{ secrets.MESHENT_CI_PAT }}
```

While `dry-run` is true (the default until the first real train), the run stops after packing. It uploads the local
feed as the artifact `release-train-<version>` and publishes and promotes nothing. The wrapper above keeps scheduled
runs dry until you change that line.

| input | meaning |
|---|---|
| `manifest` | path to the manifest in the calling repository; default `release-train.json` |
| `ref` | the branch the train assembles from in every manifest repository; default `release`. A repository without it fails the run |
| `dry-run` | build, test and pack only, then upload the local feed; default `true` |
| `override` | run even when the org variable `ACTIONS_BUDGET_STATE` is `hard`. The train runs at `soft`; at `hard` only an override runs |
| `dotnet-version` | default `10.0.x` |
| secret `repos-token` | reads every manifest repository; on a live train also pushes `main` and `train/*` tags |
| secret `packages-token` | `read:packages` for restore; on a live train also `write:packages` |
| secret `dispatch-token` | host deploys; accepted and unused until hosts join the manifest (Phase 2) |
| secrets `merryn-url`, `merryn-token` | when set, a live train that published and promoted POSTs its release record (version, repos with their commits and the item keys found in their commit messages since the last tag, packages, run URL) to `merryn-url` with the token as a bearer. A failed POST warns and never fails a published train |
| output `version` | the train version, which is also the `train/<version>` tag |

**The manifest.** Repositories only. The order is computed, so list them in any order:

```json
{
  "org": "meshent",
  "repos": [
    "meshNet.Common",
    "meshNet.Common.Azure",
    { "repo": "meshNet.Users", "solution": "meshNet.Users.slnx", "test-args": "--filter Category!=Integration" },
    { "repo": "meshNet.External.Carriers", "test": false }
  ],
  "hosts": []
}
```

A repository entry is a name, or an object with `repo` plus any of three optional fields:
- `solution`: the solution to test. By default the train uses the single `*.slnx`/`*.sln` at the root, then
  `<repo>.slnx`.
- `test-args`: extra arguments for `dotnet test`.
- `test: false`: skip the tests. Use it only for a repository with none.

A project is packable unless it evaluates `IsPackable` false or is a test project (`IsTestProject`, or it references
`Microsoft.NET.Test.Sdk`). Its id is its `PackageId`. `hosts` is read but ignored until Phase 2, when hosts' pins and
deploys join the train.

**Done** means the run is green. On a live train that also means the summary lists every package at one version and
every selected repository promoted. The tag `train/<version>` exists in each one, and its `main` equals `ref`.

The train is one job in one concurrency group per organization, with `cancel-in-progress: false`: a killed publish
would strand half a train on the feed. The script lives inside the workflow file, so a pinned tag pins the logic too.
`tests/release-train/test_train.py` extracts it from there and tests it: run it with Python 3 and PyYAML, and add
`TRAIN_DOTNET=1` to also run the end-to-end cases on throwaway git repositories.

## `dotnet-validate.yml`: the one opt-in PR job

PRs run nothing by default. A PR labelled `ci:validate` runs this workflow once, in one job:
1. Checkout, and a restore through the NuGet cache.
2. A `dotnet pack` of every packable project into a throwaway folder. This proves the nuspecs and publishes nothing.
3. The repository's tests from source.

The pack runs first, on the untouched tree, because a tree built with the source-graph switch must never be packed.
The tests still run when the pack fails, so a single run reports both.

The job also runs on `workflow_dispatch`. On a `labeled` event it starts only when *that* label was added, and a
newer push to the PR cancels the run in progress. It skips while `ACTIONS_BUDGET_STATE` is `soft` or `hard`, because
PR validation is the first thing to stop.

```yaml
# .github/workflows/validate.yml
name: Validate
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, ready_for_review]
  workflow_dispatch: {}
permissions:
  contents: read
  packages: read
jobs:
  validate:
    uses: meshent/merryn-github/.github/workflows/dotnet-validate.yml@main
```

| input | meaning |
|---|---|
| `solution` | solution or project to pack and test; empty finds the one `*.slnx`/`*.sln` at the root |
| `source-graph` | `auto` (default) passes `-p:UseProjectReferences=true` when `Directory.Build.targets` defines it; `true` or `false` forces it |
| `test-args` | extra arguments for `dotnet test` |
| `label` | the label that opts a PR in; default `ci:validate`. Empty runs on every PR event the wrapper subscribes to |
| `dotnet-version` | default `10.0.x` |
| secret `packages-token` | `read:packages` on the org feed; falls back to the workflow's own token |

Both workflows cache `~/.nuget/packages` with `actions/cache`, keyed on the project files. They deliberately do not
use `setup-dotnet`'s `cache: true`, which needs `packages.lock.json`, and a lock file pins every `Version="*"` float
to whatever it resolved last time.

## Versioning

Reference `@main` for the current contract, or pin a tag. Inputs are only ever added; a renamed or removed input is a
new major tag.

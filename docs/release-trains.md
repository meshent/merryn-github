# Release trains and an Actions budget for meshNet (and the other Merryn tenants)

*Plan, 2026-10-08. Status: proposed. Filed as work: meshNet board FEAT-0147 (coordinator:C26 Phase 0, C27 Phase 1a,
C28 Phase 1b, C29 Phase 2a, C30 Phase 2b) and the owner question
`q-coordinator-release-trains-accept-the-five-recommended-setti`; Mira board FEAT-0148 (the Merryn half). The
mechanics land here, in merryn-github, as reusable workflows; each tenant keeps a thin wrapper and a manifest.*

## Why

The meshent org spent a month of GitHub Actions minutes in a week and is locked out again
(coordinator:C25, 2026-10-08: "recent account payments have failed or your spending limit needs to be increased").
meshNet main d342cc1 is merged and not deployed; every package publish in the org is stopped with it. This is the third
lockout recorded on the board (2026-07-24 note in q-common-merge-and-fixup, 2026-09-02 in the system domain, 2026-10-08).

The run history explains the burn. Every number below is from the Actions API on 2026-10-08.

| what | evidence | cost shape |
|---|---|---|
| CI on every push to every branch | meshNet.Common `Test meshNet.Common (source graph)` ran **50 times on 2026-10-02 alone**, all on `wip/common`, about 1.5 min each. meshNet `Build meshNet.Api` runs on every push to every branch, and again on `pull_request` for the same commit (every `claude/*` commit in the last day ran twice). | Agent lanes push many times an hour. Each push is a billed job, and each job is **rounded up to a whole minute**. |
| Per-package fan-out | Common's publish workflow is `test` + `changes` + up to **16 calls** of the shared `dotnet-publish.yml`, and each call is itself two jobs (`changes`, build). A PR that touches a shared build file starts ~34 jobs. The same fan-out runs twice: on the PR and again on the push to main. | 34 jobs at 1 minute minimum each, every one paying `setup-dotnet` and a cold restore. |
| 20 repositories | Every domain repo has the same pair of workflows. Hosts add build, test and deploy. | The pattern above, times twenty. |
| Bills by job, not by run | GitHub rounds **each job** up to the nearest minute; a 5-second `changes` job costs the same as a 59-second one. | Fan-out is the multiplier, duration is not. |

Included minutes are 2,000/month on Free and 3,000/month on Team (Linux, private repos; Windows 2x, macOS 10x). At one
rounded minute per job, 2,000 minutes is roughly 2,000 jobs a month, or ~65 a day. Fourteen autonomous lanes plus the
Common fan-out clear that in days. The remedy is structural: fewer jobs, not shorter ones.

## The model

The host repo already uses it (meshNet `ci.yml`: `feature → release → main → PRODUCTION`). This plan generalises that
branch model to every repo and adds the one piece that is missing: a single orchestrated publish across repos.

```
wip/<domain>  --PR-->  release  --(train, on cadence)-->  main  --> packages published, hosts deployed
   no CI              no CI by default (opt-in label)     ONE workflow run per train, for the whole org
```

### 1. Branches

- Every repo gets a long-lived **`release`** branch (rolling, not dated). PRs target `release`, never `main`. Small PRs
  stay small, so an item still maps to its PR and its review, and Mira's correlation still sees each merge.
- **`main` is written only by the train.** Branch protection: no direct pushes, PRs only from `release`, merged by the
  train identity. `release` takes PRs from `wip/*` and `feature/*`; the review gate stays what it is today (Step D).
- Lane charters change one word: "branch pushes never publish" becomes "branch pushes never run CI". Lanes already
  run the suite locally before pushing; that stays the gate for a wip branch.
- After a train, `main == release` (fast-forward), tagged `train/<id>` in every repo it touched.

Why rolling rather than dated branches: twenty repos times a new branch twice a week is churn with no information in it.
The train tag carries the date; the `release` branch is just "what ships next".

### 2. CI on PRs: off by default, on by request

Matthew's rule as stated: merging to `release` does not trigger workflows, only `main` does. Adopted, with one valve:

- Workflows keep a `pull_request` trigger **filtered on a label** (`ci:validate`), and a `ready_for_review` type if we
  want it. Unlabelled PRs and drafts cost nothing. Adding the label runs one job (see 4) once, cancels any previous run
  for the same PR (`concurrency: cancel-in-progress: true`).
- The train is the real gate (it tests every repo once, from source). A red train is fixed by a PR into `release` and a
  re-run, which costs one train run instead of N PR runs.
- Trade-off to accept: a bad PR is found at train time, not at PR time. The valve is for the PRs where that is too late
  (a Common seam change, a packaging edit). If this turns out to be too tight, the fallback is "one validation job on
  `ready_for_review` for every PR", still a fraction of today.

### 3. The train: one run, every repo, dependency order

A reusable workflow here, `dotnet-release-train.yml`, called by one wrapper in the tenant's aggregator repo
(meshNet for meshNet; Mira for Mira). Scheduled (proposed: Tue and Fri 02:00 UTC) plus `workflow_dispatch` for a hotfix
train. **One job**, so one runner, one `setup-dotnet`, one warm NuGet cache, no per-job rounding.

Inputs: a manifest of repositories and hosts (below), a token that can read every repo and write packages
(`MESHENT_CI_PAT` already exists for this), and the dispatch-deploy secret for hosts.

Phases, in the one job:

1. **Assemble.** Check out every manifest repo at `release`. Read every `.csproj`: a `PackageReference` whose id is a
   `PackageId` of another manifest repo is an edge. Topologically sort the repos (and the projects inside a repo; Common
   has nine chained edges today that `needs:` lists encode by hand). Fail loudly on a cycle.
2. **Select.** A repo is in the train when `release` differs from its last `train/*` tag (or has no tag). Phase 1 rule:
   a selected repo republishes **all** of its packages. This retires the C30/C32 catch-up baseline, the per-package
   `paths-filter` lists and the stranding class of bug in one move. Per-project selection can come later if the version
   churn bothers anyone.
3. **Stamp.** One version for the whole train, derived once from the clock the way `Directory.Build.props` already does
   (it accepts `VersionBuild`/`VersionRevision` from CI by design). Every package in a train carries the same
   `0.0.<days>.<halfsecs>`, which is the train id. This is not the forbidden "set a version by hand"; it is the existing
   stamp taken once instead of sixty times. **Owner decision** (filed): one stamp per train, or per-pack stamps as today.
4. **Build and pack, bottom-up, into a local feed.** For each repo in order: `dotnet restore` with the local feed folder
   as an extra source (so a downstream resolves the upstream that was packed a minute ago, not the one on the public
   feed), run the repo's test gate from source (`-p:UseProjectReferences=true` where the repo has the switch; a plain
   `dotnet test` otherwise), then `dotnet pack` **without** the switch into the local feed. The nuspec records the
   sibling version it actually restored, which is the train's version. Nothing has left the runner yet.
5. **Publish.** Only when every selected repo built and passed: `dotnet nuget push` each package to GitHub Packages, in
   the same order. A failure anywhere in 4 publishes nothing, so the feed is never half a train.
6. **Pin.** Consumers that pin exact versions get their `PackageReference` bumped to the train version by the train (the
   one place that knows it), committed to `release` and `main` as `[train <id>] pin meshNet.* to <version>`. This is how
   the standing request for exact pins instead of `Version="*"` becomes sustainable: the train does the bump, not a
   person, and a host never again fails to compile against a float that moved under it (coordinator:C3, C6, P46).
   Repos still on floats need no pin step. **Owner decision** (filed): move consumers to exact pins now that the train
   can maintain them, or keep floats.
   **Pins on packages outside the train** (coordinator:C44): an exact pin on a manifest package whose repo is NOT in
   this train moves forward to the version of that repo's latest `train/*` tag (read from the clone, never the feed; a
   tag only exists after its train published), never backwards, and a range or prerelease is left as written. So the
   fleet converges on the newest train for every manifest package even when only some repos rebuild.
7. **Promote and deploy.** Merge `release` into `main` in every selected repo (fast-forward), tag `train/<id>`. For each
   host in the manifest with `deploy: true`, fire the existing shared `dispatch-deploy` (meshNet.Hosting azd) or the
   `deploy-host.yml` here (Merryn tenant hosts). Static web apps (Web.Vocab, Web.Pay) deploy themselves on the push to
   `main` as they do today; they simply get that push on the cadence.
8. **Record.** Write the train summary (repos, packages and versions, items) to the run summary and `POST` it to
   Merryn as a release (the `list_releases` record FEAT-0114 added; today it is empty for meshNet).

#### When `release` moves while the train runs (coordinator:C44, from train 0.0.2474.11566)

Lanes merge into `release` continuously, so a merge landing between plan and promote is the common case, not an edge
case. The first live train hit it: Commerce's `release` moved at 06:30Z, the pin commit's push was refused, and the
promote step stopped there, leaving Commerce, Pay and the four External.* repos published but not promoted.

- **Promotion never stops at the first repo.** Publication already happened, so each repo is promoted on its own; one
  that cannot be (main moved by someone else, a refused tag) is recorded as **stranded** with the sha it was built from,
  the others are still promoted, and the step fails at the end naming every stranded repo. The summary prints the
  exact promote-only input to finish them.
- **The pin commit vs a moved `release`: rule (a).** `main` always takes exactly what was built and tested (the pin
  commit included) and is tagged; the pin commit goes to `release` only when that is still a fast-forward, otherwise the
  summary warns and `release` keeps the lane's merge. The next plan accepts a repo whose `main` is ahead of `release` by
  **train commits only** (author `release-train`, subject `[train …]`), and its build records `main` as merged with
  `git merge -s ours` (the tree stays exactly `release`'s; the train re-derives the pins), so `release` and `main`
  fast-forward together again. Anything else ahead on `main` is still refused.
  - Rejected (b), runner-local pins with `main == release`: the owner's 2026-10-08 decision
    (q-coordinator-release-trains-accept-the-five-recommended-setti, setting 4) is that the train commits the pins as
    `[train <id>] pin …` on `release` and `main`; (b) would leave source pins one train behind the nuspecs.
  - Rejected (c), rebase the pin commit onto the moved `release` and push both: the lane's merge would reach `main`
    without having been built or tested by this train.
- **Recovery: promote-only.** Dispatch with `promote-only: <repo>=<sha>, …` and `version: <train id>` (packages already on
  the feed). It clones the manifest, checks every entry before pushing anything (the sha is on `release` or already on
  `main`; `main` is its ancestor or ahead by train commits only; the repo is not already tagged with that train),
  re-derives that train's pins on the sha (packages of repos tagged with it plus the named ones), fast-forwards `main`,
  tags `train/<version>`, and pushes the pin commit to `release` only as a fast-forward. With `dry-run: true` it checks
  and reports, pushing nothing. It never promotes a lane's later merge under an old train's stamp: the sha names exactly
  what was built.

The `main` push that step 7 makes must not start the old per-repo publish workflows. Those workflows are retired in
the cutover (kept as `workflow_dispatch` only for the first month, then deleted), so a `main` push triggers nothing but
the hosts' deploy wrappers.

Rough cost: one train is one job of maybe 30 to 60 minutes (every suite in the org, once). Twice a week is about
400 to 500 minutes a month. Today a single Common PR plus its main publish is on the order of 60 to 70 billed minutes.

#### The manifest

Lives in the tenant's aggregator repo (`meshNet/release-train.json`). Repos only; the dependency order is computed, not
maintained.

```json
{
  "org": "meshent",
  "repos": [
    "Patterns.Messaging",
    "meshNet.Common", "meshNet.Common.Azure", "meshNet.Common.External",
    "meshNet.Users", "meshNet.Networks", "meshNet.Profiles", "meshNet.Shares", "meshNet.Contacts",
    "meshNet.System", "meshNet.System.Authentication", "meshNet.Commerce", "meshNet.Pay",
    "meshNet.External.Carriers", "meshNet.External.Bluesky", "meshNet.External.Mastodon",
    "meshNet.External.LinkedIn", "meshNet.External.Twitter"
  ],
  "hosts": [
    { "repo": "meshNet",          "deploy": "dispatch-deploy", "pins": "exact" },
    { "repo": "meshNet.Api.Auth", "deploy": "dispatch-deploy", "pins": "exact" },
    { "repo": "meshNet.Api.Pay",  "deploy": "dispatch-deploy", "pins": "exact" },
    { "repo": "meshNet.Tower",    "deploy": "deploy-host",     "pins": "exact" }
  ]
}
```

Mira gets the same shape with `Mira` as the one package repo and its tenant hosts (merryn-mira, meshNet.Tower) as hosts.

### 4. The one PR validation job (when asked for)

`dotnet-validate.yml` here: checkout, `setup-dotnet`, restore with the section 7 cache, the repo's source-graph test, and a
`dotnet pack` of every project into a throwaway folder (proves the nuspecs, publishes nothing). One job. It replaces the
16-way PR fan-out in Common's publish workflow and the shared `dotnet-publish.yml`'s PR mode. The private-PR sibling
mechanism (meshent/.github PR #2, `pr-dependency-projects`) solved the "PR builds against last published sibling"
problem for one repo; the source graph does the same inside a repo, and the train's local feed does it across repos.

### 5. The budget gate: throttle before GitHub does

A lockout stops everything, including the hotfix that would fix it. The gate makes the system slow down first.

- **Watch.** A step reads the org's Actions usage for the current billing cycle: the enhanced-billing endpoint
  `GET /organizations/{org}/settings/billing/usage` (filter `product=actions`), falling back to the legacy
  `GET /orgs/{org}/settings/billing/actions` (`total_minutes_used`, `included_minutes`). Needs an org owner or billing
  manager token: a dedicated `MESHENT_BILLING_PAT` org secret. It computes `used / included` and the days left in the cycle.
- **State.** It writes one org-level Actions variable, `ACTIONS_BUDGET_STATE`: `ok` (< 70 %), `soft` (70 to 90 %),
  `hard` (> 90 %, the rest is reserved for a hotfix train).
- **Enforce, for free.** Every workflow's jobs carry `if: vars.ACTIONS_BUDGET_STATE != 'hard'` (and PR validation adds
  `!= 'soft'`). A skipped job is not billed and reading a variable is not a job. The train itself runs in `soft`; in
  `hard` only a `workflow_dispatch` with `override: true` runs.
- **Where the watch runs.** First version: the first step of the train plus one daily `schedule` (about 30 billed
  minutes a month). Second version: Merryn runs it (section 6), which costs no Actions minutes at all.
- **Tell someone.** When the state changes, the watch writes a Merryn note on the coordinator board with the numbers, and
  at `hard` files a question to the owner. Today the only signal is a 2-second failed run with an annotation nobody reads.

Also set the org **spending limit** to a small non-zero amount once billing is fixed (Linux minutes are $0.006 after the
January 2026 price cut, so $20 is about 3,300 minutes). The gate then stops the burn before the limit, and the limit
keeps a bug in the gate from becoming a bill. **Owner decision** (filed): the two thresholds and the limit.

### 6. What belongs in Merryn (Mira), and what is a skill

Three layers, built in this order:

1. **Shared workflows (this repo).** `dotnet-release-train.yml`, `dotnet-validate.yml`, `actions-budget-gate.yml`.
   Tenant-neutral mechanics, public, versioned by tag as the README says. meshent/.github keeps the org-specific
   wrappers it already has (`dotnet-host-ci.yml`, `dispatch-deploy.yml`); `dotnet-publish.yml` retires with the cutover.
2. **Merryn: release management (mira board).** Items already carry a `release` field and the platform already records
   releases and correlates merges (FEAT-0114). Add: (a) a PR merged into `release` sets the item's `release` to the next
   train id, so "what is in the next train" is a board query; (b) the train's record call closes the loop: packages,
   versions, hosts, items, environment; (c) the **budget steward**: Merryn polls the usage endpoint on its existing
   correlation poll, sets the org variable, shows a gauge on the board, and files the owner question at `hard`; (d) the
   **conductor**: on the cadence, Merryn checks the board for open blocking questions on items in the train, then
   dispatches the train run and watches it. (d) is what lets "release" be a Merryn verb rather than a cron line.
3. **Skill (merryn-plugin).** `/merryn:train` for the coordinator lane: show the next train (repos with
   `release != main`, items, open questions), cut a hotfix train, re-run a failed one, read the last record. Thin: it
   calls the Merryn tools and the dispatch API; the rules live in the workflow and the board.

### 7. Two habits in every workflow that survives: cancel superseded runs, cache the restore

Both are cheap and both go into the Phase 0 pass, so they land before the train exists.

**Concurrency.** Every workflow that runs on a branch or a PR carries

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

A second push 30 seconds after the first cancels the first run instead of paying for both; on a PR, `github.ref` is the
PR's own merge ref, so the group is per PR. Two workflows must **not** cancel in progress: the per-repo publish
workflows while they still exist (C32 part 3: a killed half-finished publish strands a mixed feed) and the train
(same reason, one org-wide group, `cancel-in-progress: false`). Everything else can.

**Caching.** A cold NuGet restore of a meshNet host is most of its one-minute build, and the train restores twenty repos
in one job. Cache `~/.nuget/packages`:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.nuget/packages
    key: nuget-${{ runner.os }}-${{ hashFiles('**/*.csproj', '**/Directory.Build.*', '**/nuget.config') }}
    restore-keys: nuget-${{ runner.os }}-
```

Two .NET-specific points. First, `actions/setup-dotnet`'s own `cache: true` needs `packages.lock.json` files, and a
lock file **pins a `Version="*"` reference to whatever it resolved last time**: restore then ignores newer feed packages
unless `--force-evaluate` is passed. For repos that float (every meshNet consumer today) that silently defeats the
float, so use `actions/cache` on the packages folder as above and no lock files until exact pins land (Phase 2).
Second, GitHub bills a job rounded up to the minute, so a cache that turns a 50-second job into a 20-second one saves
nothing on that job; it pays on the train (one long job, every repo) and on anything that runs over a minute. The
cache is per repository (10 GB, entries dropped after 7 days unused), so a train that runs twice a week keeps it warm.

## Rollout

**Phase 0, this week, before any new mechanism (stops the burn).**
- Fix billing once (C25) and set a small spending limit.
- In every repo: scope `push:` to `[main, release]` (meshNet's `build-meshnet-api.yml` and Common's
  `test-source-graph.yml` lose their any-branch trigger); drop `pull_request` from the publish workflows, or gate it on
  the `ci:validate` label; add the concurrency group and the NuGet cache from section 7 to every workflow that stays
  (never `cancel-in-progress` on a publish workflow). Twenty one-file PRs; a lane can do them as one item with a checklist.
- Pull the usage report per repo and workflow (the `usage` endpoint, or Settings › Billing › Usage report CSV) so the
  next phase has a before number. Expected: Common's source-graph pushes and the host build are most of it.

**Phase 1, next: the train for meshNet (packages only).**
- `dotnet-release-train.yml` here, with the manifest format and the dependency sort. Dry-run mode (assemble, no
  publish) first, run against the real repos until it is green.
- Create `release` in every manifest repo from `main`; protect `main`. Lanes retarget their PRs. Charters: one line.
- Cut over: train publishes on Tue/Fri; per-repo publish workflows become dispatch-only. Record the first train on the
  board as a release.

**Phase 2: hosts, pins and the gate.**
- Hosts join the manifest; the train bumps their pins and fires their deploys. Consumers move to exact pins (the owner's
  standing request) now that something maintains them.
- `actions-budget-gate.yml` with the variable, thresholds and the owner alert.
- Mira adopts the same train (Mira packages, merryn-mira and Tower hosts).

**Phase 3: Merryn release management and the skill** (section 6, items 2 and 3).

## Decisions for the owner (filed as one question on the meshNet board)

1. Cadence: twice a week (Tue/Fri, recommended) or daily.
2. PR CI: none unless labelled (as stated, recommended) or one validation job on `ready_for_review`.
3. One version stamp per train (recommended) or per-pack stamps as today.
4. Consumers move to exact pins maintained by the train (recommended, matches the standing request) or keep floats.
5. Budget thresholds 70 / 90 % and a spending limit around $20, or other numbers.

## Open facts to verify during Phase 1

- The meshent/.github repo could not be attached to this session (hidden-path rule), so `dotnet-publish.yml`,
  `dotnet-host-ci.yml` and `dispatch-deploy.yml` were read only through their callers and the board's decision docs.
  Read them before retiring or reusing them.
- Confirm the usage endpoint's `product` filter and the token permission it needs on the current docs
  (the legacy endpoint needs `admin:org` on a classic token). Confirm the Linux per-minute price on the live pricing page.
- Reusable workflows from this public repo are callable from the private tenant repos as they are today for
  `deploy-host.yml`; the private meshent/.github workflows rely on the org "accessible from repositories in the
  organization" setting. Keep both.
- Native merge queues are Enterprise Cloud only for private repos, so the train does its own serialisation
  (one scheduled run, `concurrency` group, `cancel-in-progress: false`).

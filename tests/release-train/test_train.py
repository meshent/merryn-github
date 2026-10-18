"""Tests for the release train script embedded in .github/workflows/dotnet-release-train.yml.

The script is extracted from the workflow file itself, so these always test the copy that ships.

    python3 tests/release-train/test_train.py            # unit tests (needs only Python 3 and PyYAML)
    TRAIN_DOTNET=1 python3 tests/release-train/test_train.py   # also the end-to-end tests on throwaway repos (needs dotnet)
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/dotnet-release-train.yml"


def load_train(work):
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["train"]["steps"]
    run = next(s for s in steps if s.get("name") == "Write the train script")["run"]
    script = run.split("<<'TRAIN_PY'\n", 1)[1].rsplit("TRAIN_PY", 1)[0]
    path = Path(work) / "train.py"
    path.write_text(script, encoding="utf-8")
    os.environ["TRAIN_WORK"] = str(Path(work) / "train")
    spec = importlib.util.spec_from_file_location("train", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


class Unit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.train, _ = load_train(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_puts_dependencies_first_and_keeps_input_order_for_ties(self):
        edges = {"azure": ["common"], "users": ["common", "azure"], "common": []}
        self.assertEqual(self.train.toposort(["azure", "users", "common"], edges, "repository"),
                         ["common", "azure", "users"])
        self.assertEqual(self.train.toposort(["b", "a"], {}, "x"), ["b", "a"])

    def test_a_cycle_fails_and_names_it(self):
        edges = {"a": ["b"], "b": ["c"], "c": ["a"]}
        with self.assertRaises(self.train.TrainError) as raised:
            self.train.toposort(["a", "b", "c"], edges, "repository")
        self.assertIn("repository dependency cycle: a -> b -> c -> a", str(raised.exception))

    def test_ref_overrides_parse_repo_equals_ref_entries_split_on_newlines_and_commas(self):
        os.environ["TRAIN_REF_OVERRIDES"] = "meshNet.Pay=wip/pay, meshNet.Common = wip/common\n\nmeshNet.Users=feature/x\n"
        try:
            self.assertEqual(self.train.ref_overrides(),
                             {"meshNet.Pay": "wip/pay", "meshNet.Common": "wip/common", "meshNet.Users": "feature/x"})
            os.environ["TRAIN_REF_OVERRIDES"] = ""
            self.assertEqual(self.train.ref_overrides(), {})
            os.environ["TRAIN_REF_OVERRIDES"] = "meshNet.Pay"
            with self.assertRaises(self.train.TrainError) as raised:
                self.train.ref_overrides()
            self.assertIn("not of the form repo=ref", str(raised.exception))
        finally:
            os.environ.pop("TRAIN_REF_OVERRIDES", None)

    def test_stamp_is_the_directory_build_props_formula_from_one_reading(self):
        # 2026-10-08 08:40:56 UTC: 2472 whole days since 2020-01-01, 31256 seconds into the day -> 15628.
        self.assertEqual(self.train.stamp(datetime(2026, 10, 8, 8, 40, 56, tzinfo=timezone.utc)), (2472, 15628))
        self.assertEqual(self.train.stamp(datetime(2026, 10, 8, 0, 0, 1, tzinfo=timezone.utc)), (2472, 0))

    def test_conflicting_ref_overrides_are_rejected_in_either_order_and_identical_repeats_are_accepted(self):
        from unittest.mock import patch
        for entries in ("Down=lane,Down=main", "Down=main\nDown=lane"):
            with self.subTest(entries=entries), patch.dict(os.environ, TRAIN_REF_OVERRIDES=entries):
                with self.assertRaises(self.train.TrainError) as raised:
                    self.train.ref_overrides()
                self.assertIn("conflicting ref overrides for Down", str(raised.exception))
        with patch.dict(os.environ, TRAIN_REF_OVERRIDES=" Down = lane,\nDown=lane,Up=main"):
            self.assertEqual(self.train.ref_overrides(), {"Down": "lane", "Up": "main"})

    def test_a_zero_revision_is_named_the_way_nuget_normalizes_it(self):
        self.assertEqual(self.train.normalized_version(2472, 15628), "0.0.2472.15628")
        self.assertEqual(self.train.normalized_version(2472, 0), "0.0.2472")

    def test_promotion_names_the_hosts_whose_main_moved_even_when_another_repository_strands(self):
        # The caller fires one deploy for the whole train from hosts-promoted, so a host that moved is named even when the
        # step then fails on a stranded repository, and a stranded host or a package repository is never named.
        from unittest.mock import patch
        out = Path(self.tmp.name) / "github_output"
        self.train.PLAN.parent.mkdir(parents=True, exist_ok=True)
        plan = {"version": "0.0.1", "promoted": [], "stranded": [], "release_behind": [], "repos": {
            "Lib": {"repo": "Lib", "sha": "a" * 40}, "Api": {"repo": "Api", "sha": "b" * 40, "host": True},
            "Auth": {"repo": "Auth", "sha": "c" * 40, "host": True}, "Web": {"repo": "Web", "sha": "d" * 40, "host": True}}}

        def promote(plan, name, repo, dest):
            if name == "Web":
                raise self.train.TrainError("refused")
            plan.setdefault("main_moved", []).append(name)
            plan["promoted"].append(name)

        with patch.dict(os.environ, GITHUB_OUTPUT=str(out)), patch.object(self.train, "promote_repo", promote):
            with self.assertRaises(self.train.TrainError):
                self.train.promote_all(plan, ["Lib", "Api", "Auth", "Web"])
        self.assertEqual(out.read_text(), "hosts-promoted=Api,Auth\n")
        out.unlink()
        plan = {**plan, "promoted": [], "main_moved": [], "stranded": [], "repos": {"Lib": plan["repos"]["Lib"]}}
        with patch.dict(os.environ, GITHUB_OUTPUT=str(out)), patch.object(self.train, "promote_repo", promote):
            self.train.promote_all(plan, ["Lib"])
        self.assertEqual(out.read_text(), "hosts-promoted=\n")

    def test_a_host_whose_main_moved_but_whose_tag_push_failed_is_still_named_for_the_deploy(self):
        # The real promote_repo against a stubbed git: main's push succeeds, the tag push fails. The host is stranded
        # (not tagged), but prod would lag main without a deploy, so hosts-promoted still names it.
        from unittest.mock import patch
        out = Path(self.tmp.name) / "github_output"
        self.train.PLAN.parent.mkdir(parents=True, exist_ok=True)
        pushes = []

        def git(args, cwd=None, capture=False):
            if args[:1] == ["push"]:
                pushes.append(args[-1])
                if args[-1].startswith("refs/tags/"):
                    raise self.train.TrainError("tag push refused")
            return ""

        plan = {"version": "0.0.1", "ref": "release", "promoted": [], "stranded": [], "release_behind": [], "repos": {
            "Api": {"repo": "Api", "sha": "b" * 40, "main": "a" * 40, "host": True},
            "Auth": {"repo": "Auth", "sha": "c" * 40, "main": "c" * 40, "host": True}}}
        with patch.dict(os.environ, GITHUB_OUTPUT=str(out)), patch.object(self.train, "git", git), \
                patch.object(self.train, "run", lambda *a, **k: None):
            with self.assertRaises(self.train.TrainError):
                self.train.promote_all(plan, ["Api", "Auth"])
        self.assertIn("b" * 40 + ":refs/heads/main", pushes)
        self.assertEqual([s["repo"] for s in plan["stranded"]], ["Api", "Auth"])
        self.assertEqual(plan["promoted"], [])
        # Api's main moved here; Auth's main already was the sha. Both are on main, so both are named.
        self.assertEqual(out.read_text(), "hosts-promoted=Api,Auth\n")

    def test_manifest_accepts_names_and_objects_and_refuses_duplicates(self):
        path = Path(self.tmp.name) / "m.json"
        path.write_text(json.dumps({"org": "o", "repos": ["A", {"repo": "B", "solution": "B.Tests.slnx", "test": False}]}))
        manifest = self.train.load_manifest(path)
        self.assertEqual([r["repo"] for r in manifest["repos"]], ["A", "B"])
        self.assertEqual(manifest["repos"][1]["solution"], "B.Tests.slnx")
        self.assertFalse(manifest["repos"][1]["test"])
        path.write_text(json.dumps({"org": "o", "repos": ["A", "a"]}))
        with self.assertRaises(self.train.TrainError):
            self.train.load_manifest(path)

    def test_test_env_is_the_top_level_map_overlaid_by_the_repo_s_own(self):
        path = Path(self.tmp.name) / "m.json"
        path.write_text(json.dumps({"org": "o", "test-env": {"COSMOS_EMULATOR_OPTIONAL": "1"},
                                    "repos": ["A", {"repo": "B", "test-env": {"COSMOS_EMULATOR_OPTIONAL": "0", "X": "y"}}]}))
        manifest = self.train.load_manifest(path)
        self.assertEqual(manifest["repos"][0]["test-env"], {"COSMOS_EMULATOR_OPTIONAL": "1"})
        self.assertEqual(manifest["repos"][1]["test-env"], {"COSMOS_EMULATOR_OPTIONAL": "0", "X": "y"})
        path.write_text(json.dumps({"org": "o", "repos": [{"repo": "A", "test-env": {"N": 1}}]}))
        with self.assertRaises(self.train.TrainError):
            self.train.load_manifest(path)

    # --- Hosts (Phase 2): built and tested in the train's sequence, never packed --------------------------------------

    def test_manifest_hosts_take_build_and_test_targets_and_the_top_level_test_env(self):
        path = Path(self.tmp.name) / "m.json"
        path.write_text(json.dumps({"org": "o", "test-env": {"COSMOS_EMULATOR_OPTIONAL": "1"}, "repos": ["A"], "hosts": [
            {"repo": "meshNet", "deploy": "dispatch-deploy", "pins": "exact", "build": "meshNet.Api.slnx",
             "solution": "meshNet.Tests.slnx", "test-args": "--filter X"},
            "Plain"]}))
        hosts = self.train.load_manifest(path)["hosts"]
        self.assertEqual([h["repo"] for h in hosts], ["meshNet", "Plain"])
        self.assertTrue(all(h["host"] for h in hosts))
        self.assertEqual((hosts[0]["build"], hosts[0]["solution"], hosts[0]["test-args"], hosts[0]["deploy"]),
                         ("meshNet.Api.slnx", "meshNet.Tests.slnx", "--filter X", "dispatch-deploy"))
        self.assertEqual(hosts[1]["test-env"], {"COSMOS_EMULATOR_OPTIONAL": "1"})
        self.assertTrue(hosts[1]["test"])
        for bad in ({"org": "o", "repos": ["A"], "hosts": ["a"]},           # a repo and a host at once
                    {"org": "o", "repos": ["A"], "hosts": ["H", "h"]},      # a host twice
                    {"org": "o", "repos": ["A"], "hosts": [{"deploy": "x"}]}):
            path.write_text(json.dumps(bad))
            with self.assertRaises(self.train.TrainError):
                self.train.load_manifest(path)

    def test_a_host_is_in_the_train_when_it_consumes_an_in_train_package_or_its_ref_is_ahead_of_main(self):
        host = {"ref": "release", "tag": {"tag": "train/0.0.9.1"},
                "consumes": ["meshNet.Commerce", "meshNet.Networks", "meshNet.Shares", "meshNet.Users", "meshNet.Common"]}
        ship = {"meshNet.Networks": "changed", "meshNet.Shares": "changed", "meshNet.Users": "x", "meshNet.Common": "x"}
        self.assertEqual(self.train.select_host(host, ship, False),
                         (True, "consumes 4 package(s) in this train: meshNet.Networks, meshNet.Shares, meshNet.Users "
                                "and 1 more"))
        self.assertEqual(self.train.select_host(host, {"meshNet.Pay": "x"}, True),
                         (True, "release is ahead of main; consumes nothing in this train"))
        self.assertEqual(self.train.select_host(host, {"meshNet.Pay": "x"}, False),
                         (False, "unchanged since train/0.0.9.1; consumes nothing in this train"))
        self.assertFalse(self.train.select_host({**host, "tag": None}, {}, False)[0])

    def test_the_summary_lists_hosts_after_the_repositories_with_what_they_consume(self):
        import contextlib, io
        self.train.WORK.mkdir(parents=True, exist_ok=True)
        plan = {"org": "o", "ref": "release", "version": "0.0.9.1", "dry_run": True, "repo_order": ["A"],
                "repo_edges": {"A": []}, "selected": ["A"], "promoted": [], "stranded": [], "packages": [],
                "hosts": ["H", "Q"], "hosts_selected": ["H"],
                "repos": {"A": {"selected": True, "reason": "1 of 1 package(s) changed", "sha": "a" * 40, "items": []},
                          "H": {"selected": True, "reason": "consumes 1 package(s) in this train: A.Lib", "sha": "b" * 40,
                                "items": ["test:T1"], "consumes": ["A.Lib"], "host": True},
                          "Q": {"selected": False, "reason": "unchanged; consumes nothing in this train", "sha": "c" * 40,
                                "items": [], "consumes": [], "host": True}},
                "decisions": {"A.Lib": {}}, "ship": {"A.Lib": "never published"}}
        self.train.PLAN.write_text(json.dumps(plan))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.train.cmd_record()
        text = out.getvalue()
        self.assertIn("| H (host) | yes | consumes 1 package(s) in this train: A.Lib | A.Lib | test:T1 |", text)
        self.assertIn("| Q (host) | no | unchanged; consumes nothing in this train | — | — |", text)
        self.assertIn("Hosts: 2 considered, 1 built and tested against this train (never packed).", text)
        self.assertLess(text.index("| A | yes"), text.index("| H (host)"))

    def test_local_feed_is_mapped_for_train_ids_next_to_the_github_source(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        (repo / "nuget.config").write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <configuration>
              <packageSources>
                <clear />
                <add key="nuget.org" value="https://api.nuget.org/v3/index.json" />
                <add key="github" value="https://nuget.pkg.github.com/meshent/index.json" />
              </packageSources>
              <packageSourceMapping>
                <packageSource key="github"><package pattern="meshNet.*" /></packageSource>
                <packageSource key="nuget.org"><package pattern="*" /></packageSource>
              </packageSourceMapping>
            </configuration>
            """))
        self.train.point_at_local_feed(repo, {"meshNet.Common"})
        text = (repo / "nuget.config").read_text()
        self.assertIn(f'<add key="train-local" value="{self.train.FEED}" />', text)
        self.assertIn('<packageSource key="train-local"><package pattern="meshNet.Common" /></packageSource>', text)
        self.assertIn('<package pattern="meshNet.*" /><package pattern="meshNet.Common" />', text)
        self.train.point_at_local_feed(repo, {"meshNet.Common"})  # idempotent
        self.assertEqual((repo / "nuget.config").read_text().count('key="train-local"'), 2)

    def test_a_repo_without_a_nuget_config_gets_one(self):
        repo = Path(self.tmp.name) / "bare"
        repo.mkdir()
        self.train.point_at_local_feed(repo, {"X"})
        self.assertIn('key="train-local"', (repo / "nuget.config").read_text())

    def test_an_empty_train_leaves_the_nuget_config_untouched(self):
        """A host-only train has no package to restore locally, and a packageSourceMapping source with no pattern fails
        restore ("must have at least one package pattern"): the config is not touched, and none is created."""
        repo = Path(self.tmp.name) / "mapped"
        repo.mkdir()
        config = ('<?xml version="1.0" encoding="utf-8"?><configuration><packageSources>'
                  '<add key="github" value="https://nuget.pkg.github.com/meshent/index.json" /></packageSources>'
                  '<packageSourceMapping><packageSource key="github"><package pattern="meshNet.*" /></packageSource>'
                  '</packageSourceMapping></configuration>')
        (repo / "nuget.config").write_text(config)
        self.train.point_at_local_feed(repo, set())
        self.assertEqual((repo / "nuget.config").read_text(), config)
        bare = Path(self.tmp.name) / "none"
        bare.mkdir()
        self.train.point_at_local_feed(bare, set())
        self.assertFalse((bare / "nuget.config").exists())

    def _nupkg(self, package_id, version, deps):
        self.train.FEED.mkdir(parents=True, exist_ok=True)
        path = self.train.FEED / f"{package_id}.{version}.nupkg"
        dependencies = "".join(f'<dependency id="{d}" version="{v}" />' for d, v in deps)
        nuspec = (f'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
                  f'<metadata><id>{package_id}</id><version>{version}</version><dependencies><group>{dependencies}'
                  f'</group></dependencies></metadata></package>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(f"{package_id}.nuspec", nuspec)
        return path

    def test_exact_pins_on_in_train_packages_move_to_the_train_version_and_floats_stay(self):
        repo = Path(self.tmp.name) / "pins"
        (repo / "Adapter").mkdir(parents=True)
        (repo / "Adapter" / "Adapter.csproj").write_text(textwrap.dedent("""\
            <Project Sdk="Microsoft.NET.Sdk">
              <ItemGroup>
                <PackageReference Include="Patterns.Messaging.Sms" Version="0.0.2460.13471" />
                <PackageReference Include="meshNet.Common" Version="*" />
                <PackageReference Include="Microsoft.Extensions.Http" Version="10.0.12" />
              </ItemGroup>
            </Project>
            """))
        (repo / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="Patterns.Messaging.Sms" Version="0.0.2460.13471" /></ItemGroup></Project>\n')
        (repo / "obj").mkdir()
        (repo / "obj" / "Stale.csproj").write_text('<PackageReference Include="Patterns.Messaging.Sms" Version="1" />')
        changed = self.train.pin_train_versions(repo, {"Patterns.Messaging.Sms", "meshNet.Common"}, "0.0.2472.17298")
        self.assertEqual(sorted(Path(c[0]).as_posix() for c in changed), ["Adapter/Adapter.csproj", "Directory.Packages.props"])
        self.assertEqual({(c[1], c[2], c[3]) for c in changed},
                         {("Patterns.Messaging.Sms", "0.0.2460.13471", "0.0.2472.17298")})
        text = (repo / "Adapter" / "Adapter.csproj").read_text()
        self.assertIn('Include="Patterns.Messaging.Sms" Version="0.0.2472.17298"', text)
        self.assertIn('Include="meshNet.Common" Version="*"', text)                       # a float stays a float
        self.assertIn('Include="Microsoft.Extensions.Http" Version="10.0.12"', text)     # not in the train
        self.assertIn('Version="1"', (repo / "obj" / "Stale.csproj").read_text())          # obj/ is never touched
        self.assertEqual(self.train.pin_train_versions(repo, {"Patterns.Messaging.Sms"}, "0.0.2472.17298"), [])  # idempotent

    def test_a_pin_on_a_package_outside_the_train_moves_forward_to_its_latest_train_tag_and_never_back(self):
        repo = Path(self.tmp.name) / "latest"
        (repo / "Host").mkdir(parents=True)
        (repo / "Host" / "Host.csproj").write_text(textwrap.dedent("""\
            <Project Sdk="Microsoft.NET.Sdk">
              <ItemGroup>
                <PackageReference Include="meshNet.Common" Version="0.0.2470.100" />
                <PackageReference Include="meshNet.Users" Version="0.0.2475.1" />
                <PackageReference Include="meshNet.Shares" Version="[0.0.2470.1, )" />
                <PackageReference Include="meshNet.Networks" Version="*" />
                <PackageReference Include="meshNet.Commerce" Version="0.0.2470.100" />
              </ItemGroup>
            </Project>
            """))
        latest = {"meshNet.Common": "0.0.2474.11566", "meshNet.Users": "0.0.2474.11566",
                  "meshNet.Shares": "0.0.2474.11566", "meshNet.Networks": "0.0.2474.11566"}
        changed = self.train.pin_train_versions(repo, {"meshNet.Commerce"}, "0.0.2476.9", latest)
        self.assertEqual({(c[1], c[2], c[3]) for c in changed},
                         {("meshNet.Common", "0.0.2470.100", "0.0.2474.11566"),     # outside the train: its latest tag
                          ("meshNet.Commerce", "0.0.2470.100", "0.0.2476.9")})      # in the train: the train version
        text = (repo / "Host" / "Host.csproj").read_text()
        self.assertIn('Include="meshNet.Users" Version="0.0.2475.1"', text)            # never moved back
        self.assertIn('Include="meshNet.Shares" Version="[0.0.2470.1, )"', text)       # a range is left as written
        self.assertIn('Include="meshNet.Networks" Version="*"', text)                  # a float stays a float

    def test_a_stale_pin_on_a_package_outside_the_train_moves_to_its_newest_release_on_the_feed(self):
        """Release train 0.0.2475 (run 38710323286): meshNet.Common was not in the train, so meshNet.Commerce.Models
        (in it) kept its csproj pin meshNet.Common 0.0.2455.8240 while its float on meshNet.Commerce.Types resolved
        Types 0.0.2474.11566, whose nuspec names meshNet.Common >= 0.0.2474.11566: NU1605 at pack. The pin must move to
        the newest release published ahead of the train: never back, never to a prerelease or to a later stamp, and a
        package the feed does not have, a range and a float stay as written."""
        from unittest.mock import patch
        repo = Path(self.tmp.name) / "commerce"
        (repo / "meshNet.Commerce.Models").mkdir(parents=True)
        (repo / "meshNet.Commerce.Models" / "meshNet.Commerce.Models.csproj").write_text(textwrap.dedent("""\
            <Project Sdk="Microsoft.NET.Sdk">
              <ItemGroup>
                <PackageReference Include="meshNet.Commerce.Types" Version="*" />
                <PackageReference Include="meshNet.Common" Version="0.0.2455.8240" />
                <PackageReference Include="meshNet.Users" Version="0.0.2476.1" />
                <PackageReference Include="meshNet.Shares" Version="[0.0.2400.1, )" />
                <PackageReference Include="meshNet.Unlisted" Version="0.0.2400.1" />
              </ItemGroup>
            </Project>
            """))
        feed = Path(self.tmp.name) / "orgfeed"
        feed.mkdir()
        for name in ("meshNet.Common.0.0.2455.8240", "meshNet.Common.0.0.2474.11566", "meshNet.Common.0.0.2475.1-beta",
                     "meshNet.Common.0.0.2490.1",            # stamped after this train: not published ahead of it
                     "meshNet.Commerce.Types.0.0.2474.11566", "meshNet.Users.0.0.2474.11566",
                     "meshNet.Shares.0.0.2474.11566"):
            (feed / f"{name}.nupkg").write_bytes(b"")
        version = "0.0.2476.9"
        ids = {"meshNet.Common", "meshNet.Commerce.Types", "meshNet.Users", "meshNet.Shares", "meshNet.Unlisted"}
        with patch.dict(os.environ, TRAIN_FEED=str(feed)):
            published = self.train.published_versions("o", ids, version)
        self.assertEqual(published, {"meshNet.Common": "0.0.2474.11566", "meshNet.Commerce.Types": "0.0.2474.11566",
                                     "meshNet.Users": "0.0.2474.11566", "meshNet.Shares": "0.0.2474.11566"})
        # main's carried pins and the feed merge to the higher of the two.
        self.assertEqual(self.train.newest(published, {"meshNet.Common": "0.0.2470.1", "meshNet.Users": "0.0.2475.2"},
                                           None)["meshNet.Users"], "0.0.2475.2")
        changed = self.train.pin_train_versions(repo, {"meshNet.Commerce.Models"}, version,
                                                self.train.newest(published, {"meshNet.Common": "0.0.2470.1"}))
        self.assertEqual({(c[1], c[2], c[3]) for c in changed},
                         {("meshNet.Common", "0.0.2455.8240", "0.0.2474.11566")})
        text = (repo / "meshNet.Commerce.Models" / "meshNet.Commerce.Models.csproj").read_text()
        self.assertIn('Include="meshNet.Common" Version="0.0.2474.11566"', text)       # what Types already requires
        self.assertIn('Include="meshNet.Commerce.Types" Version="*"', text)            # a float stays a float
        self.assertIn('Include="meshNet.Users" Version="0.0.2476.1"', text)            # never moved back
        self.assertIn('Include="meshNet.Shares" Version="[0.0.2400.1, )"', text)       # a range is left as written
        self.assertIn('Include="meshNet.Unlisted" Version="0.0.2400.1"', text)         # not on the feed: as written

    def test_promote_only_entries_parse_like_ref_overrides(self):
        self.assertEqual(self.train.repo_assignments("meshNet.Commerce=f16e4b1,\nmeshNet.Pay = 0b9a6ae",
                                                     "promote-only entry", "sha"),
                         {"meshNet.Commerce": "f16e4b1", "meshNet.Pay": "0b9a6ae"})
        with self.assertRaises(self.train.TrainError) as raised:
            self.train.repo_assignments("meshNet.Pay=1,meshNet.Pay=2", "promote-only entry", "sha")
        self.assertIn("conflicting promote-only entrys for meshNet.Pay", str(raised.exception))
        with self.assertRaises(self.train.TrainError) as raised:
            self.train.repo_assignments("meshNet.Pay", "promote-only entry", "sha")
        self.assertIn("is not of the form repo=sha", str(raised.exception))

    def test_promote_only_refuses_anything_but_a_commit_id_before_touching_the_work_folder(self):
        from unittest.mock import patch
        manifest = Path(self.tmp.name) / "m.json"
        manifest.write_text(json.dumps({"org": "o", "repos": ["Down"]}))
        self.train.WORK.mkdir(parents=True, exist_ok=True)
        sentinel = self.train.WORK / "keep.txt"
        sentinel.write_text("existing work")
        for value in ("--output=/tmp/x", "release", "HEAD~1", "f16e4b"):
            with self.subTest(value=value), patch.dict(os.environ, TRAIN_MANIFEST=str(manifest),
                                                       TRAIN_VERSION="0.0.2474.11566", TRAIN_PROMOTE=f"Down={value}"):
                with self.assertRaises(self.train.TrainError) as raised:
                    self.train.cmd_promote_only()
                self.assertIn("promote-only takes commit ids", str(raised.exception))
        with patch.dict(os.environ, TRAIN_MANIFEST=str(manifest), TRAIN_VERSION="latest", TRAIN_PROMOTE="Down=f16e4b1"):
            with self.assertRaises(self.train.TrainError) as raised:
                self.train.cmd_promote_only()
            self.assertIn("needs the train version", str(raised.exception))
        self.assertEqual(sentinel.read_text(), "existing work")

    def test_the_summary_names_stranded_repositories_and_a_ref_left_behind(self):
        import contextlib, io
        self.train.WORK.mkdir(parents=True, exist_ok=True)
        repo = lambda sha: {"selected": True, "reason": "release moved", "sha": sha, "items": []}
        plan = {"org": "o", "ref": "release", "version": "0.0.9.1", "dry_run": False, "repo_order": ["A", "B", "C"],
                "repo_edges": {"A": [], "B": [], "C": []}, "selected": ["A", "B", "C"], "packages": [],
                "repos": {"A": repo("a" * 40), "B": repo("b" * 40), "C": repo("c" * 40)},
                "promoted": ["A", "B"], "release_behind": ["B"],
                "stranded": [{"repo": "C", "sha": "c" * 40, "base": "d" * 40, "reason": "command failed (1): git push"}]}
        self.train.PLAN.write_text(json.dumps(plan))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.train.cmd_record()
        text = out.getvalue()
        self.assertIn("Promoted (main fast-forwarded, tagged `train/0.0.9.1`): A, B.", text)
        self.assertIn("`release` moved while the train ran", text)
        self.assertIn("(main does; the next train reconciles it): B.", text)
        self.assertIn("**Stranded** (published at 0.0.9.1, main not moved, not tagged): C.", text)
        self.assertIn("promote-only `C=" + "d" * 40 + "`", text)

    def test_the_feed_is_read_from_a_folder_or_the_v3_flat_container_and_an_unreadable_feed_fails_closed(self):
        import io
        import urllib.error
        from unittest.mock import patch
        folder = Path(self.tmp.name) / "feed"
        folder.mkdir()
        for name in ("Up.Lib.0.0.9.1.nupkg", "up.lib.0.0.9.2.nupkg", "Up.Lib.Extra.0.0.9.3.nupkg"):
            (folder / name).write_bytes(b"x")
        with patch.dict(os.environ, TRAIN_FEED=str(folder)):
            versions = self.train.feed_versions("o", "Up.Lib")
        self.assertEqual(versions & {"0.0.9.1", "0.0.9.2", "0.0.9.3"}, {"0.0.9.1", "0.0.9.2"})
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def open(self, request, timeout=None):
                seen.append(request.get_header("Authorization"))
                answer = answers[request.full_url]
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(request.full_url, answer, "no", {}, None)
                return Response(json.dumps(answer).encode())

        index = "https://feed.test/o/index.json"
        answers = {index: {"resources": [{"@id": "https://feed.test/o/download", "@type": "PackageBaseAddress/3.0.0"}]},
                   "https://feed.test/o/download/up.lib/index.json": {"versions": ["0.0.9.1", "0.0.9.2"]},
                   "https://feed.test/o/download/gone/index.json": 404,
                   "https://feed.test/o/download/broken/index.json": 500}
        with patch.dict(os.environ, TRAIN_FEED=index, PACKAGES_TOKEN="s3cret-token"), \
                patch.object(self.train.urllib.request, "build_opener", lambda *handlers: Opener()):
            self.assertEqual(self.train.feed_versions("o", "Up.Lib"), {"0.0.9.1", "0.0.9.2"})
            self.assertEqual(self.train.feed_versions("o", "Gone"), set())                    # 404: not on the feed
            with self.assertRaises(self.train.TrainError) as raised:
                self.train.feed_versions("o", "Broken")                                        # anything else fails closed
            self.assertIn("could not read the package feed", str(raised.exception))
            self.assertNotIn("s3cret-token", str(raised.exception))
        self.assertEqual(set(seen), {"Basic " + self.train.basic_auth("s3cret-token")})
        # A redirect is never followed with the token on it.
        self.assertIsNone(self.train._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/"))

    def test_the_summary_lists_the_train_s_packages_with_their_reasons_and_the_counts(self):
        import contextlib, io
        self.train.WORK.mkdir(parents=True, exist_ok=True)
        repo = lambda reason, selected: {"selected": selected, "reason": reason, "sha": "a" * 40, "items": []}
        plan = {"org": "o", "ref": "release", "version": "0.0.9.1", "dry_run": True, "repo_order": ["A", "B", "C"],
                "repo_edges": {"A": [], "B": ["A"], "C": []}, "selected": ["A", "B"], "promoted": [], "stranded": [],
                "repos": {"A": repo("1 of 2 package(s) changed", True), "B": repo("1 of 1 package(s) changed", True),
                          "C": repo("unchanged since train/0.0.8.1", False)},
                "decisions": {"A.Lib": {}, "A.Extra": {}, "B.Lib": {}, "C.Lib": {}},
                "ship": {"A.Lib": "changed since train/0.0.8.1: A.Lib/Class.cs", "B.Lib": "depends on A.Lib, in this train"},
                "packages": [{"id": "A.Lib", "version": "0.0.9.1", "dependencies": []},
                             {"id": "B.Lib", "version": "0.0.9.1", "dependencies": ["A.Lib 0.0.9.1"]}]}
        self.train.PLAN.write_text(json.dumps(plan))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.train.cmd_record()
        text = out.getvalue()
        self.assertIn("Repositories: 3 considered, 2 in this train. Packages: 4 considered, 2 in this train;", text)
        self.assertIn("| A.Lib | changed since train/0.0.8.1: A.Lib/Class.cs | 0.0.9.1 | — |", text)
        self.assertIn("| B.Lib | depends on A.Lib, in this train | 0.0.9.1 | A.Lib 0.0.9.1 |", text)
        self.assertNotIn("A.Extra |", text)
        self.assertIn("| C | no | unchanged since train/0.0.8.1 |", text)

    def test_a_package_s_commit_is_read_from_its_nuspec_and_a_download_redirect_never_carries_the_token(self):
        import io
        import urllib.error
        from unittest.mock import patch
        commit = "fafafa7cc547b5d533dcddde08d10f5edf5e53bc"
        nupkg = io.BytesIO()
        with zipfile.ZipFile(nupkg, "w") as z:
            z.writestr("meshNet.Pay.nuspec", '<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/'
                       '2013/05/nuspec.xsd"><metadata><id>meshNet.Pay</id><version>0.0.2474.11566</version><repository '
                       f'type="git" url="https://github.com/meshent/meshNet.Pay" commit="{commit}" /></metadata></package>')
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def open(self, request, timeout=None):
                seen.append((request.full_url, request.get_header("Authorization")))
                answer = answers[request.full_url]
                if isinstance(answer, tuple):
                    raise urllib.error.HTTPError(request.full_url, answer[0], "Found", {"Location": answer[1]}, None)
                return Response(answer)

        index, base = "https://feed.test/o/index.json", "https://feed.test/o/download"
        blob = "https://blobs.test/meshnet.pay.nupkg?sig=x"
        answers = {index: json.dumps({"resources": [{"@id": base, "@type": "PackageBaseAddress/3.0.0"}]}).encode(),
                   f"{base}/meshnet.pay/0.0.2474.11566/meshnet.pay.0.0.2474.11566.nupkg": (302, blob),
                   blob: nupkg.getvalue(),
                   f"{base}/plain/1.0.0/plain.1.0.0.nupkg": (302, "http://blobs.test/plain.nupkg")}
        with patch.dict(os.environ, TRAIN_FEED=index, PACKAGES_TOKEN="s3cret-token"), \
                patch.object(self.train.urllib.request, "build_opener", lambda *handlers: Opener()):
            self.assertEqual(self.train.feed_commit("o", "meshNet.Pay", "0.0.2474.11566"), commit)
            with self.assertRaises(self.train.TrainError):                          # never follows to plain http
                self.train.feed_commit("o", "Plain", "1.0.0")
        auth = "Basic " + self.train.basic_auth("s3cret-token")
        self.assertIn((blob, None), seen)                                          # the redirect carried no token
        self.assertTrue(all(token == auth for url, token in seen if url.startswith("https://feed.test/")))
        self.assertNotIn("http://blobs.test/plain.nupkg", [url for url, _ in seen])

    def test_the_summary_names_a_ref_that_refused_the_train_s_push_without_moving(self):
        import contextlib, io
        self.train.WORK.mkdir(parents=True, exist_ok=True)
        plan = {"org": "o", "ref": "release", "version": "0.0.9.1", "dry_run": False, "repo_order": ["A"],
                "repo_edges": {"A": []}, "selected": ["A"], "packages": [], "promoted": ["A"], "stranded": [],
                "repos": {"A": {"selected": True, "reason": "release moved", "sha": "a" * 40, "items": []}},
                "release_behind": [], "release_refused": ["A"]}
        self.train.PLAN.write_text(json.dumps(plan))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.train.cmd_record()
        self.assertIn("refused although `release` did not move** (branch protection or the token's rights?); main took "
                      "them: A.", out.getvalue())
        self.assertNotIn("moved while the train ran", out.getvalue())

    def test_a_package_that_restored_a_sibling_from_outside_the_train_fails(self):
        self._nupkg("A", "0.0.9.1", [("B", "0.0.9.1"), ("Newtonsoft.Json", "13.0.3")])
        self.assertEqual(self.train.check_package("A", "0.0.9.1", {"A", "B"})["dependencies"], ["B 0.0.9.1"])
        self._nupkg("C", "0.0.9.1", [("B", "[0.0.8.5, )")])
        with self.assertRaises(self.train.TrainError) as raised:
            self.train.check_package("C", "0.0.9.1", {"B", "C"})
        self.assertIn("not the train version", str(raised.exception))

    def test_tokens_never_reach_the_log_or_an_error(self):
        import contextlib, io
        os.environ.update(REPOS_TOKEN="FAKE_REPOS_SECRET", PACKAGES_TOKEN="FAKE_PACKAGES_SECRET")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.train.git(["--version"])
                with self.assertRaises(self.train.TrainError) as raised:
                    self.train.run(["git", "nonexistent-subcommand", "--api-key", "FAKE_PACKAGES_SECRET"], capture=True)
            shown = out.getvalue() + str(raised.exception)
            for secret in ("FAKE_REPOS_SECRET", "FAKE_PACKAGES_SECRET", self.train.basic_auth("FAKE_REPOS_SECRET")):
                self.assertNotIn(secret, shown)
            self.assertIn("AUTHORIZATION: basic ***", shown)
            self.assertIn("--api-key ***", shown)
        finally:
            del os.environ["REPOS_TOKEN"], os.environ["PACKAGES_TOKEN"]

    def test_stamp_time_is_the_stamp_s_inverse(self):
        moment = datetime(2026, 10, 10, 6, 25, 32, tzinfo=timezone.utc)
        self.assertEqual(self.train.stamp_time(self.train.normalized_version(*self.train.stamp(moment))), moment)
        self.assertEqual(self.train.stamp_time("0.0.2474"), datetime(2026, 10, 10, tzinfo=timezone.utc))

    def test_a_change_touches_a_project_through_its_folder_imports_references_and_outside_paths(self):
        lib = {"path": "src/Lib/Lib.csproj", "projects": ["src/Core/Core.csproj"], "outside": ["shared/Common"]}
        core = {"path": "src/Core/Core.csproj", "projects": [], "outside": []}
        other = {"path": "src/Other/Other.csproj", "projects": [], "outside": []}
        by_path = {p["path"]: p for p in (lib, core, other)}
        folders, outside = self.train.watched(lib, by_path)
        self.assertEqual((folders, outside), ({"src/Lib", "src/Core"}, {"shared/Common"}))
        files = ["src/Lib/A.cs", "src/Core/B.cs", "src/Other/C.cs", "src/LibX/D.cs", "shared/Common/E.cs",
                 "shared/Other/F.cs", "Directory.Build.props", "src/Directory.Packages.props", "src/Other/nuget.config",
                 "README.md", "docs/Directory.Build.props", ".github/workflows/ci.yml"]
        self.assertEqual(self.train.touched(files, folders, outside),
                         ["src/Lib/A.cs", "src/Core/B.cs", "shared/Common/E.cs", "Directory.Build.props",
                          "src/Directory.Packages.props"])
        self.assertEqual(self.train.touched(["README.md", "docs/x.md"], *self.train.watched(other, by_path)), [])
        root = {"path": "Root.csproj", "projects": [], "outside": []}             # a project at the repository root
        self.assertEqual(self.train.touched(["README.md"], *self.train.watched(root, {})), ["README.md"])

    def test_outside_paths_are_the_static_prefixes_of_includes_above_the_project(self):
        repo = Path(self.tmp.name) / "outside"
        (repo / "src" / "Lib").mkdir(parents=True)
        (repo / "src" / "Lib" / "Lib.csproj").write_text(textwrap.dedent("""\
            <Project Sdk="Microsoft.NET.Sdk">
              <Import Project="../../build/Common.targets" />
              <ItemGroup>
                <Compile Include="../../shared/Code/**/*.cs" />
                <None Include="$(MSBuildThisFileDirectory)../../assets/logo.png" Pack="true" />
                <Compile Include="Local/*.cs" />
                <Compile Include="../../../Elsewhere/X.cs" />
                <ProjectReference Include="../Core/Core.csproj" />
              </ItemGroup>
            </Project>
            """))
        self.assertEqual(self.train.outside_paths(repo, repo / "src" / "Lib" / "Lib.csproj"),
                         ["assets/logo.png", "build/Common.targets", "shared/Code"])

    def test_editorconfig_and_build_rsp_above_a_project_touch_it(self):
        folders, outside = self.train.watched({"path": "src/Lib/Lib.csproj", "projects": [], "outside": []}, {})
        files = [".editorconfig", "src/.editorconfig", "Directory.Build.rsp", "docs/.editorconfig",
                 "src/Other/Directory.Build.rsp"]
        self.assertEqual(self.train.touched(files, folders, outside),
                         [".editorconfig", "src/.editorconfig", "Directory.Build.rsp"])

    def test_files_the_directory_build_props_above_a_project_import_are_followed(self):
        repo = Path(self.tmp.name) / "imports"
        for folder in ("src/Lib", "build", "eng", "tools", "docs"):
            (repo / folder).mkdir(parents=True, exist_ok=True)
        (repo / "Directory.Build.props").write_text(textwrap.dedent("""\
            <Project>
              <Import Project="build/Common.props" />
              <Import Project="$(MSBuildThisFileDirectory)eng/Version.targets" Condition="Exists('eng/Version.targets')" />
              <Import Project="$(RepoRoot)/elsewhere.props" />
            </Project>
            """))
        (repo / "build" / "Common.props").write_text(
            '<Project><Import Project="../tools/Analyzers.props" /><Import Project="../build/Common.props" /></Project>')
        (repo / "eng" / "Version.targets").write_text("<Project />")
        (repo / "tools" / "Analyzers.props").write_text("<Project />")
        (repo / "src" / "Directory.Build.props").write_text(textwrap.dedent("""\
            <Project>
              <Import Project="$([MSBuild]::GetPathOfFileAbove('Directory.Build.props', '$(MSBuildThisFileDirectory)../'))" />
              <Import Project="../shared/*.props" />
            </Project>
            """))
        (repo / "docs" / "Directory.Build.props").write_text('<Project><Import Project="docs.props" /></Project>')
        (repo / "src" / "Lib" / "Lib.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />')
        found = self.train.props_imports(repo, repo / "src" / "Lib" / "Lib.csproj")
        self.assertEqual(found, ["build/Common.props", "eng/Version.targets", "shared", "tools/Analyzers.props"])
        # And a change there touches the project.
        lib = {"path": "src/Lib/Lib.csproj", "projects": [], "outside": found}
        folders, outside = self.train.watched(lib, {lib["path"]: lib})
        self.assertEqual(self.train.touched(["tools/Analyzers.props", "docs/docs.props", "shared/a.props"], folders, outside),
                         ["tools/Analyzers.props", "shared/a.props"])

    def test_a_package_that_did_not_take_the_stamp_fails(self):
        self._nupkg("A", "1.0.0", [])
        with self.assertRaises(self.train.TrainError) as raised:
            self.train.check_package("A", "0.0.9.1", {"A"})
        self.assertIn("did not take the train stamp", str(raised.exception))


PROPS = """<Project>
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <VersionMajor Condition=" '$(VersionMajor)' == '' ">0</VersionMajor>
    <VersionMinor Condition=" '$(VersionMinor)' == '' ">0</VersionMinor>
    <VersionBuild Condition=" '$(VersionBuild)' == '' ">1</VersionBuild>
    <VersionRevision Condition=" '$(VersionRevision)' == '' ">1</VersionRevision>
    <VersionPrefix>$(VersionMajor).$(VersionMinor).$(VersionBuild).$(VersionRevision)</VersionPrefix>
  </PropertyGroup>
</Project>
"""


def csproj(references):
    refs = "".join(f'<PackageReference Include="{r}" Version="*" />' for r in references)
    return f'<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>{refs}</ItemGroup></Project>\n'


TEST_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><IsPackable>false</IsPackable><IsTestProject>true</IsTestProject></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" />
    <PackageReference Include="xunit" Version="2.9.2" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />
    {refs}
  </ItemGroup>
</Project>
"""


@unittest.skipUnless(os.environ.get("TRAIN_DOTNET"), "set TRAIN_DOTNET=1 to run the dotnet end-to-end tests")
class EndToEnd(unittest.TestCase):
    """Throwaway git repositories served from file://, so the real plan/build path runs with no network for git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.module, self.script = load_train(self.tmp.name)
        self.server = self.root / "server"
        self.feed_env = dict(os.environ, GITHUB_SERVER_URL=f"file://{self.server}", TRAIN_WORK=str(self.root / "train"),
                             TRAIN_REF="main", TRAIN_DRY_RUN="true", PYTHONUTF8="1",
                             TRAIN_FEED=str(self.root / "orgfeed"))  # promote-only reads the 'org' feed folder

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, name, files, org_feed=False):
        work = self.root / "make" / name
        work.mkdir(parents=True)
        (work / "Directory.Build.props").write_text(PROPS)
        # org_feed: a folder standing in for the org's package feed, which publish() below copies the local feed into.
        org = f'<add key="org" value="{self.root / "orgfeed"}" />' if org_feed else ""
        (self.root / "orgfeed").mkdir(exist_ok=True)
        (work / "nuget.config").write_text('<?xml version="1.0" encoding="utf-8"?><configuration><packageSources>'
                                           '<add key="nuget.org" value="https://api.nuget.org/v3/index.json" />'
                                           f'{org}</packageSources></configuration>')
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{name} (test:T1)"]):
            subprocess.run(["git", *args], cwd=work, check=True)
        bare = self.server / "o" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", work, bare], check=True)

    def commit_to(self, name, branch, files, new=False):
        """Commit files on a branch of a throwaway repo (creating it from the current branch with new=True) and push it."""
        work = self.root / "make" / name
        subprocess.run(["git", "checkout", "-q", *(["-b"] if new else []), branch], cwd=work, check=True)
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{name} on {branch}"],
                       cwd=work, check=True)
        subprocess.run(["git", "push", "-q", str(self.server / "o" / f"{name}.git"), branch], cwd=work, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True).stdout.strip()

    def bare(self, name):
        return self.server / "o" / f"{name}.git"

    def rev(self, name, ref):
        return subprocess.run(["git", "rev-parse", ref], cwd=self.bare(name), capture_output=True, text=True,
                              check=True).stdout.strip()

    def file_at(self, name, ref, path):
        return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=self.bare(name), capture_output=True, text=True,
                              check=True).stdout

    def is_ancestor(self, name, a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.bare(name)).returncode == 0

    def release_branches(self, *names):
        for name in names:
            subprocess.run(["git", "branch", "release", "main"], cwd=self.bare(name), check=True)
            subprocess.run(["git", "branch", "release", "main"], cwd=self.root / "make" / name, check=True)

    def publish(self):
        """Stands in for the publish step: the local feed's packages land on the 'org' feed folder."""
        for nupkg in (self.root / "train" / "feed").glob("*.nupkg"):
            (self.root / "orgfeed" / nupkg.name).write_bytes(nupkg.read_bytes())

    def feed_package(self, package_id, version, commit):
        """A package on the 'org' feed folder whose nuspec names the commit it was packed from, as Source Link writes it."""
        (self.root / "orgfeed").mkdir(exist_ok=True)
        repository = f'<repository type="git" commit="{commit}" />' if commit else ""
        nuspec = (f'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
                  f'<metadata><id>{package_id}</id><version>{version}</version>{repository}</metadata></package>')
        with zipfile.ZipFile(self.root / "orgfeed" / f"{package_id}.{version}.nupkg", "w") as z:
            z.writestr(f"{package_id}.nuspec", nuspec)

    def live(self, ref="release"):
        self.feed_env.update(TRAIN_REF=ref, TRAIN_DRY_RUN="false")

    @staticmethod
    def pinned_lib(*pins):
        refs = "".join(f'<PackageReference Include="{package}" Version="{version}" />' for package, version in pins)
        return f'<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>{refs}</ItemGroup></Project>\n'

    def pinned_repo(self, name, up, version="0.0.1.1"):
        lib = self.pinned_lib((f"{up}.Lib", version))
        self.repo(name, {f"{name}.Lib/{name}.Lib.csproj": lib,
                         f"{name}.Lib/Class.cs": f"namespace {name}; public class B : {up}.A {{}}"}, org_feed=True)

    def two_repos(self):
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]),
                           "Down.Lib/Class.cs": "namespace Down; public class B : Up.A {}",
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" /></Solution>'})

    def manifest(self, repos, hosts=()):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"org": "o", "repos": repos, "hosts": list(hosts)}))
        self.feed_env["TRAIN_MANIFEST"] = str(path)

    def train(self, command):
        # Git creates read-only pack files on Windows. Make this throwaway fixture writable before
        # a second plan removes it; production Actions runs on Linux. Do not change train behavior.
        if os.name == "nt" and command in ("plan", "promote-only"):
            for path in (self.root / "train").rglob("*"):
                if path.is_file():
                    path.chmod(path.stat().st_mode | stat.S_IWRITE)
        return subprocess.run([sys.executable, self.script, command], env=self.feed_env, text=True,
                              encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_two_repos_pack_in_order_with_one_version_and_downstream_depends_on_the_train_version(self):
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]),
                           "Down.Lib/Class.cs": "namespace Down; public class B : Up.A {}",
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" /></Solution>'})
        self.manifest(["Down", "Up"])
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("Repository order: Up -> Down", plan.stdout)
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        result = json.loads((self.root / "train/plan.json").read_text())
        version = result["version"]
        self.assertEqual([p["id"] for p in result["packages"]], ["Up.Lib", "Down.Lib"])
        self.assertTrue(all(p["version"] == version for p in result["packages"]))
        self.assertEqual(result["packages"][1]["dependencies"], [f"Up.Lib {version}"])
        self.assertEqual(result["repos"]["Down"]["items"], ["test:T1"])

    def test_an_exact_pin_on_an_in_train_package_is_bumped_before_packing(self):
        # Down pins Up.Lib to a version that exists on no feed at all: the build can only succeed if the train moved
        # the pin to the train version before packing, so the nuspec names the train version and the source tree
        # carries the bumped pin (the commit it would make on a live train).
        pinned = csproj([]).replace("<ItemGroup></ItemGroup>",
                                   '<ItemGroup><PackageReference Include="Up.Lib" Version="0.0.1.1" /></ItemGroup>')
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": pinned,
                           "Down.Lib/Class.cs": "namespace Down; public class B : Up.A {}",
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" /></Solution>'})
        self.manifest(["Down", "Up"])
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        state = json.loads((self.root / "train" / "plan.json").read_text())
        version = state["version"]
        self.assertIn(f"Down: pinned 1 reference(s): Up.Lib (0.0.1.1 -> {version})", build.stdout)
        pins = [[Path(path).as_posix(), package, old, new] for path, package, old, new in state["repos"]["Down"]["pins"]]
        self.assertEqual(pins, [["Down.Lib/Down.Lib.csproj", "Up.Lib", "0.0.1.1", version]])
        down = next(p for p in state["packages"] if p["id"] == "Down.Lib")
        self.assertEqual(down["dependencies"], [f"Up.Lib {version}"])
        self.assertIn(f'Include="Up.Lib" Version="{version}"',
                      (self.root / "train" / "src" / "Down" / "Down" / "Down.Lib" / "Down.Lib.csproj").read_text())
        self.assertNotIn("train_commits", state["repos"]["Down"])  # a dry run commits nothing

    def test_a_sibling_checkout_switch_never_sees_the_other_train_repos(self):
        # Like meshNet.Commerce: a project that builds against a sibling checkout when one sits next to it must build
        # and pack in the train exactly as it does alone, against the package.
        probe = "$(MSBuildThisFileDirectory)../../Up/Up.Lib/Up.Lib.csproj"
        down = (f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup Condition="Exists(\'{probe}\')">'
                f'<DefineConstants>$(DefineConstants);SIBLING</DefineConstants></PropertyGroup><ItemGroup>'
                f'<PackageReference Include="Up.Lib" Version="*" /></ItemGroup></Project>\n')
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": down,
                           "Down.Lib/Class.cs": "#if SIBLING\n#error the train exposed a sibling checkout\n#endif\n"
                                                "namespace Down; public class B : Up.A {}",
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" /></Solution>'})
        self.manifest(["Up", "Down"])
        self.assertEqual(self.train("plan").returncode, 0)
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        self.assertNotIn("the train exposed a sibling checkout", build.stdout)

    def test_a_repo_unchanged_since_its_train_tag_is_not_selected(self):
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.slnx": "<Solution />"})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]), "Down.slnx": "<Solution />"})
        bare = self.server / "o" / "Up.git"
        subprocess.run(["git", "tag", "train/0.0.1.1", "main"], cwd=bare, check=True)
        self.feed_package("Up.Lib", "0.0.1.1", self.rev("Up", "main"))  # published by that train
        self.manifest(["Up", "Down"])
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        result = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(result["selected"], ["Down"])
        self.assertEqual(result["repos"]["Up"]["reason"], "unchanged since train/0.0.1.1")
        self.assertEqual(result["train_ids"], ["Down.Lib"])

    # --- coordinator:C47: only packages whose project changed since their last publication ----------------------------

    def test_a_train_publishes_only_the_packages_that_changed_and_still_promotes_a_docs_only_change(self):
        """The owner's correction on Release 1: "We only deploy packages when the corresponding project has a change." After
        a first train published everything, the second packs and pushes only a changed package, a never-published one and
        a dependent of a changed one; a repository with only a docs change is promoted and tagged with nothing published;
        unchanged repositories are skipped entirely."""
        lib = lambda ns: f"namespace {ns}; public class A {{}}"
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": lib("Up"),
                         "Up.Extra/Up.Extra.csproj": csproj([]), "Up.Extra/Class.cs": lib("UpExtra")})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]), "Down.Lib/Class.cs": lib("Down")})
        self.repo("Side", {"Side.Lib/Side.Lib.csproj": csproj(["Up.Extra"]), "Side.Lib/Class.cs": lib("Side")})
        self.repo("Docs", {"Docs.Lib/Docs.Lib.csproj": csproj([]), "Docs.Lib/Class.cs": lib("Docs"), "README.md": "v1"})
        self.repo("Quiet", {"Quiet.Lib/Quiet.Lib.csproj": csproj([]), "Quiet.Lib/Class.cs": lib("Quiet")})
        names = ["Up", "Down", "Side", "Docs", "Quiet"]
        self.release_branches(*names)
        self.manifest([{"repo": n, "test": False} for n in names])
        self.live()
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        first = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(len(first["packages"]), 6)                            # nothing was published yet: all of it
        self.assertTrue(all(reason == "never published" for reason in first["ship"].values()), first["ship"])
        self.publish()
        self.assertEqual(self.train("promote").returncode, 0)
        # Lanes merge into release: Up.Lib's code, a new package in Up, and a README in Docs.
        self.commit_to("Up", "release", {"Up.Lib/Class.cs": "namespace Up; public class A { public int B; }",
                                         "Up.New/Up.New.csproj": csproj([]), "Up.New/Class.cs": lib("UpNew")})
        self.commit_to("Docs", "release", {"README.md": "v2"})
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        second = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(sorted(second["ship"]), ["Down.Lib", "Up.Lib", "Up.New"])
        self.assertEqual(second["ship"]["Up.Lib"], f"changed since train/{first['version']}: Up.Lib/Class.cs")
        self.assertEqual(second["ship"]["Up.New"], "never published")
        self.assertEqual(second["ship"]["Down.Lib"], "depends on Up.Lib, in this train")
        self.assertEqual(second["selected"], ["Up", "Down", "Docs"])
        self.assertEqual(second["repos"]["Docs"]["reason"], "release is ahead of main; no package changed, nothing to publish")
        self.assertEqual(second["repos"]["Quiet"]["reason"], f"unchanged since train/{first['version']}")
        self.assertEqual(sorted(p["id"] for p in second["packages"]), ["Down.Lib", "Up.Lib", "Up.New"])
        packed = sorted(p.name for p in (self.root / "train" / "feed").glob("*.nupkg"))
        self.assertEqual(packed, sorted(f"{i}.{second['version']}.nupkg" for i in ("Down.Lib", "Up.Lib", "Up.New")))
        self.publish()
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        tag = f"train/{second['version']}"
        self.assertEqual(self.rev("Docs", "main"), self.rev("Docs", "release"))    # the docs change reached main ...
        self.assertEqual(self.rev("Docs", f"{tag}^{{commit}}"), self.rev("Docs", "main"))  # ... tagged, nothing published
        for name in ("Side", "Quiet"):
            self.assertNotEqual(subprocess.run(["git", "rev-parse", "-q", "--verify", tag], cwd=self.bare(name),
                                               capture_output=True).returncode, 0, name)
        record = self.train("record")
        self.assertIn("Packages: 7 considered, 3 in this train;", record.stdout)
        self.assertIn("| Down.Lib | depends on Up.Lib, in this train |", record.stdout)

    def test_without_a_train_tag_the_baseline_is_the_commit_the_feed_s_newest_version_was_packed_from(self):
        """A repository no train has tagged yet (the six the first train stranded): each package counts from the commit its
        newest feed version names. When the repository never received that commit (an early train's pin commit), the ref
        as it stood 30 minutes before that version's stamp stands in; with nothing that old, the package counts as
        changed."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.Two/Up.Two.csproj": csproj([]), "Up.Two/Class.cs": "namespace UpTwo; public class A {}",
                         "Up.Three/Up.Three.csproj": csproj([]), "Up.Three/Class.cs": "namespace Up3; public class A {}"})
        built = self.rev("Up", "main")
        self.commit_to("Up", "main", {"Up.Two/More.cs": "namespace UpTwo; public class More {}"})
        self.manifest([{"repo": "Up", "test": False}])
        tomorrow = self.module.normalized_version(*self.module.stamp(datetime.now(timezone.utc) + timedelta(days=1)))
        self.feed_package("Up.Lib", "0.0.2000.1", "e" * 40)                       # older, ignored
        self.feed_package("Up.Lib", "0.0.2474.11566", built)                      # packed from a commit Up has
        self.feed_package("Up.Two", tomorrow, "f" * 40)                           # an unpushed commit, stamped later
        self.feed_package("Up.Three", "0.0.2000.1", "f" * 40)                     # an unpushed commit, before Up existed
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        decisions = state["decisions"]
        self.assertEqual((decisions["Up.Lib"]["baseline"], decisions["Up.Lib"]["how"]),
                         (built, f"since 0.0.2474.11566 ({built[:12]})"))
        self.assertNotIn("Up.Lib", state["ship"])                                 # Up.Two's change is not Up.Lib's
        self.assertEqual(decisions["Up.Two"]["baseline"], self.rev("Up", "main"))  # the ref before the stamp: no change
        self.assertIn("which Up does not have, so main as of", decisions["Up.Two"]["how"])
        self.assertNotIn("Up.Two", state["ship"])
        self.assertIsNone(decisions["Up.Three"]["baseline"])
        self.assertEqual(state["ship"]["Up.Three"], "0.0.2000.1 was packed from ffffffffffff, which Up does not have")
        self.assertEqual(state["selected"], ["Up"])

    def test_a_package_outside_the_train_whose_stale_pin_a_sibling_s_dependency_outpaces_still_packs(self):
        """Run 38710323286's NU1605, end to end: Up.Lib is not in the train (unchanged since its newest release, 0.0.2.1),
        Down.Types is not either and its published 0.0.2.1 names Up.Lib >= 0.0.2.1, and the never-published Down.Models
        floats Down.Types while its csproj still pins Up.Lib 0.0.1.1. The train packs Down.Models against Up.Lib 0.0.2.1,
        the newest release published ahead of it, and moves the stale pins forward in Down's tree, never back."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"},
                  org_feed=True)
        types = self.pinned_lib(("Up.Lib", "0.0.1.1"))
        models = self.pinned_lib(("Down.Types", "*"), ("Up.Lib", "0.0.1.1"))
        self.repo("Down", {"Down.Types/Down.Types.csproj": types, "Down.Types/Class.cs": "namespace DownTypes; class T {}",
                           "Down.Models/Down.Models.csproj": models,
                           "Down.Models/Class.cs": "namespace DownModels; class M {}"}, org_feed=True)

        def released(package_id, version, commit, deps=()):
            dependencies = "".join(f'<dependency id="{d}" version="{v}" />' for d, v in deps)
            nuspec = (f'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
                      f'<metadata><id>{package_id}</id><version>{version}</version><authors>t</authors>'
                      f'<description>t</description><repository type="git" commit="{commit}" />'
                      f'<dependencies><group>{dependencies}</group></dependencies></metadata></package>')
            with zipfile.ZipFile(self.root / "orgfeed" / f"{package_id}.{version}.nupkg", "w") as z:
                z.writestr(f"{package_id}.nuspec", nuspec)

        up, down = self.rev("Up", "main"), self.rev("Down", "main")
        released("Up.Lib", "0.0.1.1", up)
        released("Up.Lib", "0.0.2.1", up)                                       # published ahead, by an earlier train
        released("Down.Types", "0.0.2.1", down, [("Up.Lib", "[0.0.2.1, )")])    # packed with the pin that train set
        self.manifest([{"repo": "Up", "test": False}, {"repo": "Down", "test": False}])
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["train_ids"], ["Down.Models"])
        self.assertEqual(state["published"], {"Down.Types": "0.0.2.1", "Up.Lib": "0.0.2.1"})
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        self.assertNotIn("NU1605", build.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        pins = sorted((Path(path).as_posix(), package, old, new) for path, package, old, new in state["repos"]["Down"]["pins"])
        self.assertEqual(pins, [("Down.Models/Down.Models.csproj", "Up.Lib", "0.0.1.1", "0.0.2.1"),
                                ("Down.Types/Down.Types.csproj", "Up.Lib", "0.0.1.1", "0.0.2.1")])
        _, _, deps = self.module.nuspec_of(self.module.find_package("Down.Models", state["version"]))
        self.assertIn(("Up.Lib", "0.0.2.1"), deps)

    def test_a_repo_cycle_fails_the_plan_naming_it(self):
        self.repo("A", {"A.Lib/A.Lib.csproj": csproj(["B.Lib"])})
        self.repo("B", {"B.Lib/B.Lib.csproj": csproj(["A.Lib"])})
        self.manifest(["A", "B"])
        plan = self.train("plan")
        self.assertNotEqual(plan.returncode, 0)
        self.assertIn("repository dependency cycle: A -> B -> A", plan.stdout)

    def test_a_failing_test_leaves_nothing_to_publish(self):
        failing = "namespace T; public class F { [Xunit.Fact] public void Fails() => Xunit.Assert.True(false); }"
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]),
                           "Tests/Down.Tests.csproj": TEST_CSPROJ.format(refs='<ProjectReference Include="../Down.Lib/Down.Lib.csproj" />'),
                           "Tests/F.cs": failing,
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" />'
                                        '<Project Path="Tests/Down.Tests.csproj" /></Solution>'})
        self.manifest(["Up", "Down"])
        self.assertEqual(self.train("plan").returncode, 0)
        build = self.train("build")
        self.assertNotEqual(build.returncode, 0, build.stdout)
        self.assertIn("Failed!", build.stdout)  # the test gate failed, not a restore
        # publish reads the package list build writes only after EVERY repo passed: it is still empty.
        self.assertEqual(json.loads((self.root / "train/plan.json").read_text())["packages"], [])

    def test_a_dry_run_ref_override_takes_that_repository_at_its_branch_and_warns_once_main_moves_past_it(self):
        self.two_repos()
        lane = self.commit_to("Down", "lane", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}"}, new=True)
        self.manifest(["Down", "Up"])
        self.feed_env["TRAIN_REF_OVERRIDES"] = "Down=lane"
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["repos"]["Down"]["ref"], state["repos"]["Down"]["sha"]), ("lane", lane))
        self.assertEqual(state["repos"]["Up"]["ref"], "main")
        self.assertEqual((state["overrides"], state["warnings"]), ({"Down": "lane"}, []))
        self.assertIn("Down: lane " + lane[:12] + " (override)", plan.stdout)
        # main moves past the lane branch: a dry run warns, still builds the branch, and the summary carries both facts.
        self.commit_to("Down", "main", {"Down.Lib/Main.cs": "namespace Down; public class M {}"})
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(len(state["warnings"]), 1, state["warnings"])
        self.assertIn("Down: main (", state["warnings"][0])
        self.assertIn("is not an ancestor of lane", state["warnings"][0])
        self.assertIn("A live train refuses this repository.", state["warnings"][0])
        self.assertIn("WARNING Down: main (", plan.stdout)
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        record = self.train("record")
        self.assertEqual(record.returncode, 0, record.stdout)
        self.assertIn("Overrides (dry run only): `Down=lane`.", record.stdout)
        self.assertIn("- ⚠️ Down: main (", record.stdout)

    def test_ref_overrides_are_refused_on_a_live_train_and_for_a_repository_outside_the_manifest(self):
        self.two_repos()
        self.manifest(["Down", "Up"])
        self.feed_env["TRAIN_REF_OVERRIDES"] = "Down=lane"
        self.feed_env["TRAIN_DRY_RUN"] = "false"
        plan = self.train("plan")
        self.assertNotEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("ref overrides are for dry runs only", plan.stdout)
        work = self.root / "train"
        self.assertEqual(list(work.rglob(".git")) if work.exists() else [], [], "a refused plan must clone nothing")
        self.feed_env["TRAIN_DRY_RUN"] = "true"
        self.feed_env["TRAIN_REF_OVERRIDES"] = "Nope=lane"
        plan = self.train("plan")
        self.assertNotEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("not in the manifest: Nope", plan.stdout)

    def test_conflicting_ref_overrides_fail_before_any_checkout_or_existing_work_deletion(self):
        self.two_repos()
        self.manifest(["Down", "Up"])
        work = self.root / "train"
        work.mkdir()
        sentinel = work / "keep.txt"
        sentinel.write_text("existing work")
        for entries in ("Down=lane,Down=main", "Down=main\nDown=lane"):
            self.feed_env["TRAIN_REF_OVERRIDES"] = entries
            plan = self.train("plan")
            self.assertNotEqual(plan.returncode, 0, plan.stdout)
            self.assertIn("conflicting ref overrides for Down", plan.stdout)
            self.assertEqual(sentinel.read_text(), "existing work")
            self.assertEqual(list(work.rglob(".git")), [], "conflicting overrides must clone nothing")
            self.assertNotIn("git clone", plan.stdout)

    def test_identical_ref_override_repeats_plan_the_requested_ref_once(self):
        self.two_repos()
        self.manifest(["Down", "Up"])
        self.feed_env["TRAIN_REF_OVERRIDES"] = " Down = main,\nDown=main"
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["overrides"], {"Down": "main"})
        self.assertEqual(state["repos"]["Down"]["ref"], "main")
        self.assertEqual(plan.stdout.count("Down: main "), 1)

    def test_a_live_train_still_refuses_a_repository_whose_main_is_not_an_ancestor_of_the_ref(self):
        self.two_repos()
        for name in ("Up", "Down"):
            self.commit_to(name, "lane", {f"{name}.Lib/Lane.cs": f"namespace {name}; public class Lane {{}}"}, new=True)
        self.commit_to("Down", "main", {"Down.Lib/Main.cs": "namespace Down; public class M {}"})
        self.manifest(["Down", "Up"])
        self.feed_env["TRAIN_REF"] = "lane"
        self.feed_env["TRAIN_DRY_RUN"] = "false"
        plan = self.train("plan")
        self.assertNotEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("Down: main (", plan.stdout)
        self.assertIn("is not an ancestor of lane", plan.stdout)
        self.assertNotIn("WARNING", plan.stdout)

    # --- coordinator:C44: a ref that moves between plan and promote, stranding, reconciling, promote-only -----------------

    def test_a_ref_that_moves_during_the_train_keeps_the_lane_s_work_and_strands_nothing_else(self):
        """The first live train (0.0.2474.11566): Commerce's release moved between plan and promote, its pin commit push
        was refused, and every repository after it was left published but unpromoted. Now: the moved repository's main
        still takes exactly what was built (pin commit included) and is tagged, its release keeps the lane's merge, a
        repository whose main cannot move is reported as stranded, and every other repository is promoted."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.pinned_repo("Down", "Up")                                   # like Commerce: pins Up exactly, gets a pin commit
        self.repo("Mid", {"Mid.Lib/Mid.Lib.csproj": csproj(["Up.Lib"]),
                          "Mid.Lib/Class.cs": "namespace Mid; public class M : Up.A {}"})
        self.repo("Side", {"Side.Lib/Side.Lib.csproj": csproj(["Up.Lib"]),
                           "Side.Lib/Class.cs": "namespace Side; public class S : Up.A {}"})
        self.release_branches("Up", "Down", "Mid", "Side")
        # Side's release is ahead of its main, so its promotion has to push main (a main already equal to what was built
        # needs no push, and a later commit on main then strands nothing: main still contains the built sha).
        self.commit_to("Side", "release", {"Side.Lib/Feature.cs": "namespace Side; public class F {}"})
        self.manifest([{"repo": n, "test": False} for n in ("Up", "Side", "Down", "Mid")])
        self.live()
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        version, built = state["version"], state["repos"]
        # While the train runs: a lane merges into Down's release, and someone pushes Side's main directly.
        lane = self.commit_to("Down", "release", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}"})
        self.commit_to("Side", "main", {"Side.Lib/Hotfix.cs": "namespace Side; public class H {}"})
        promote = self.train("promote")
        self.assertNotEqual(promote.returncode, 0, promote.stdout)       # the job fails ...
        self.assertIn("1 repository(ies) published at " + version + " but not promoted: Side", promote.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["promoted"], ["Up", "Down", "Mid"])       # ... but nothing after the failure is stranded
        self.assertEqual([s["repo"] for s in state["stranded"]], ["Side"])
        self.assertEqual(state["stranded"][0]["base"], built["Side"]["base"])
        self.assertEqual(state["release_behind"], ["Down"])
        # Down: main is the pin commit that was built, tagged; release still carries the lane's merge.
        self.assertEqual(self.rev("Down", "main"), built["Down"]["sha"])
        self.assertEqual(self.rev("Down", f"train/{version}^{{commit}}"), built["Down"]["sha"])
        self.assertIn(f'Include="Up.Lib" Version="{version}"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        self.assertEqual(self.rev("Down", "release"), lane)
        # Side: untouched, untagged.
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "-q", "--verify", f"train/{version}"],
                                           cwd=self.bare("Side"), capture_output=True).returncode, 0)
        record = self.train("record")
        self.assertIn("**Stranded** (published at " + version + ", main not moved, not tagged): Side.", record.stdout)
        self.assertIn("(main does; the next train reconciles it): Down.", record.stdout)

    def test_the_next_train_reconciles_main_s_pin_commit_and_carries_its_pins(self):
        """Rule (a)'s second half: after a train left main ahead of release by its pin commit, the next live plan accepts
        the repository instead of refusing it, and promotion fast-forwards both release and main again. The pin commit set
        a pin on a package this train does not publish: it is carried over from main, so main never moves a pin back."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.pinned_repo("Down", "Up")
        self.release_branches("Up", "Down")
        self.manifest([{"repo": "Up", "test": False}, {"repo": "Down", "test": False}])
        self.live()
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        first = json.loads((self.root / "train/plan.json").read_text())["version"]
        self.publish()
        lane = self.commit_to("Down", "release", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}"})
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        pin_commit = self.rev("Down", "main")
        self.assertFalse(self.is_ancestor("Down", "main", "release"))  # main is ahead of release by the pin commit
        # The next train: Up is unchanged since its tag (not rebuilt), Down's release moved.
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("is ahead of release by train commits only; this train reconciles it", plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["selected"], ["Down"])
        self.assertEqual(state["train_ids"], ["Down.Lib"])                 # Up.Lib is unchanged and already published
        self.assertEqual(state["repos"]["Down"]["carry"], {"Up.Lib": first})
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        second = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual([p[1:] for p in second["repos"]["Down"]["pins"]], [["Up.Lib", "0.0.1.1", first]])
        self.assertEqual(second["packages"][0]["id"], "Down.Lib")
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        self.assertEqual(self.rev("Down", "release"), self.rev("Down", "main"))  # both moved, as fast-forwards
        for ancestor in (lane, pin_commit):
            self.assertTrue(self.is_ancestor("Down", ancestor, "main"))
        self.assertIn(f'Include="Up.Lib" Version="{first}"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        self.assertIn("class Lane", self.file_at("Down", "main", "Down.Lib/Lane.cs"))
        self.assertEqual(self.rev("Down", f"train/{second['version']}^{{commit}}"), self.rev("Down", "main"))

    def test_promote_only_finishes_a_stranded_promotion_and_refuses_a_sha_that_is_not_on_release(self):
        """Recovery for the first train's stranded repositories: packages already on the feed at the version; main moves
        to the sha the train built from plus that train's pins, and the tag goes on it. Every entry is checked first."""
        version = "0.0.2474.11566"
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        # Down also pins Up.New, a package Up only gains after that train: it was never published at the version.
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": self.pinned_lib(("Up.Lib", "0.0.1.1"), ("Up.New", "0.0.1.1")),
                           "Down.Lib/Class.cs": "namespace Down; public class B : Up.A {}"}, org_feed=True)
        self.release_branches("Up", "Down")
        subprocess.run(["git", "tag", f"train/{version}", "main"], cwd=self.bare("Up"), check=True)  # Up was promoted
        self.commit_to("Up", "release", {"Up.New/Up.New.csproj": csproj([])})                    # added after the train
        built = self.rev("Down", "release")                                                        # what the train built
        lane = self.commit_to("Down", "release", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}"})
        stray = self.commit_to("Down", "stray", {"Down.Lib/Stray.cs": "namespace Down; public class S {}"}, new=True)
        main_before = self.rev("Down", "main")
        self.manifest(["Up", "Down"])
        self.live()
        self.feed_env.update(TRAIN_VERSION=version)
        # A sha that is not on release (or main) is refused, and nothing is pushed.
        self.feed_env["TRAIN_PROMOTE"] = f"Down={stray}"
        refused = self.train("promote-only")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("promote-only refused, nothing was pushed", refused.stdout)
        self.assertIn(f"Down: {stray[:12]} is not on release", refused.stdout)
        self.assertEqual(self.rev("Down", "main"), main_before)
        # A repository already tagged with that train is refused too.
        self.feed_env["TRAIN_PROMOTE"] = f"Up={self.rev('Up', 'main')}"
        self.assertIn("already exists; that repository was promoted", self.train("promote-only").stdout)
        # A version whose packages are not on the feed (a typo, a dry-run stamp) is refused before anything is pushed.
        self.feed_env.update(TRAIN_PROMOTE=f"Down={built}", TRAIN_DRY_RUN="true")
        unpublished = self.train("promote-only")
        self.assertNotEqual(unpublished.returncode, 0, unpublished.stdout)
        self.assertIn(f"no package of any manifest repository is on the feed at {version}", unpublished.stdout)
        for package_id in ("Up.Lib", "Down.Lib"):                     # what the stranded train published
            (self.root / "orgfeed" / f"{package_id}.{version}.nupkg").write_bytes(b"published by the stranded train")
        # A dry run checks and reports, pushing nothing.
        dry = self.train("promote-only")
        self.assertEqual(dry.returncode, 0, dry.stdout)
        self.assertIn("Dry run: nothing pushed.", dry.stdout)
        self.assertEqual(self.rev("Down", "main"), main_before)
        # The real thing.
        self.feed_env["TRAIN_DRY_RUN"] = "false"
        done = self.train("promote-only")
        self.assertEqual(done.returncode, 0, done.stdout)
        main = self.rev("Down", "main")
        self.assertEqual(self.rev("Down", f"{main}^"), built)                                     # the pin commit on built
        self.assertIn(f'Include="Up.Lib" Version="{version}"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        # Up is read at its train tag, not at its release tip: Up.New was not in that train, so its pin is left alone.
        self.assertIn('Include="Up.New" Version="0.0.1.1"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        self.assertEqual(self.rev("Down", f"train/{version}^{{commit}}"), main)
        self.assertEqual(self.rev("Down", "release"), lane)                                       # the lane's merge stays
        self.assertNotIn("Lane", subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=self.bare("Down"),
                                                capture_output=True, text=True).stdout)          # untested work stays off main
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["promoted"], state["release_behind"]), (["Down"], ["Down"]))

    def test_promote_only_takes_the_train_s_packages_from_the_feed_and_moves_no_other_pin(self):
        """Step D's probe (review of fa42553), under per-package selection (coordinator:C47): a train that rebuilt only
        Down left Down's exact pin on Up.Lib as the source has it, because Up.Lib did not ship in it; promote-only must
        commit exactly that, so main and the tag carry what was published."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.pinned_repo("Down", "Up")
        self.release_branches("Up", "Down")
        self.manifest([{"repo": "Up", "test": False}, {"repo": "Down", "test": False}])
        self.live()
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        first = json.loads((self.root / "train/plan.json").read_text())["version"]
        self.publish()
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        # A lane on Down's release (which now carries train 1's pin commit) changes Down's code, not its pin.
        work = self.root / "make" / "Down"
        subprocess.run(["git", "checkout", "-q", "release"], cwd=work, check=True)
        subprocess.run(["git", "fetch", "-q", str(self.bare("Down")), "release"], cwd=work, check=True)
        subprocess.run(["git", "reset", "-q", "--hard", "FETCH_HEAD"], cwd=work, check=True)
        self.commit_to("Down", "release", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}"})
        # Train 2 rebuilds Down only and publishes it; its promotion never happens (stranded).
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        second = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((second["selected"], second["train_ids"]), (["Down"], ["Down.Lib"]))
        self.assertEqual(second["repos"]["Down"]["pins"], [])
        self.publish()
        self.feed_env.update(TRAIN_VERSION=second["version"], TRAIN_PROMOTE=f"Down={second['repos']['Down']['base']}")
        done = self.train("promote-only")
        self.assertEqual(done.returncode, 0, done.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["train_ids"], ["Down.Lib"])                       # Up.Lib is not on the feed at train 2
        self.assertEqual(state["repos"]["Down"]["pins"], [])
        self.assertIn(f'Include="Up.Lib" Version="{first}"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        self.assertEqual(self.rev("Down", f"train/{second['version']}^{{commit}}"), self.rev("Down", "main"))
        self.assertEqual(self.rev("Down", "main"), second["repos"]["Down"]["base"])

    def test_a_ref_push_refused_although_the_ref_did_not_move_is_named_apart_from_a_moved_ref(self):
        """A ref that refuses the train's fast-forward push (branch protection, the token's rights) is reported as such, not
        as 'moved while the train ran'; main still takes the build."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.pinned_repo("Down", "Up")
        self.release_branches("Up", "Down")
        hook = self.bare("Down") / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\nwhile read old new ref; do\n  [ "$ref" = "refs/heads/release" ] && '
                        '{ echo "release is protected" >&2; exit 1; }\ndone\nexit 0\n', newline="\n")
        hook.chmod(0o755)
        self.manifest([{"repo": "Up", "test": False}, {"repo": "Down", "test": False}])
        self.live()
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        released = self.rev("Down", "release")
        self.publish()
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["release_refused"], state["release_behind"], state["stranded"]), (["Down"], [], []))
        self.assertEqual(self.rev("Down", "release"), released)
        self.assertEqual(self.rev("Down", "main^"), released)                                      # main took the pin commit
        record = self.train("record")
        self.assertIn("was **refused although `release` did not move**", record.stdout)

    # --- Hosts (Phase 2): after the packages they consume, against the versions the train generated ---------------------

    def released(self, package_id, version, commit, deps=()):
        """A release on the 'org' feed folder: a restorable nuspec-only package naming the commit it was packed from."""
        dependencies = "".join(f'<dependency id="{d}" version="{v}" />' for d, v in deps)
        nuspec = (f'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
                  f'<metadata><id>{package_id}</id><version>{version}</version><authors>t</authors>'
                  f'<description>t</description><repository type="git" commit="{commit}" />'
                  f'<dependencies><group>{dependencies}</group></dependencies></metadata></package>')
        (self.root / "orgfeed").mkdir(exist_ok=True)
        with zipfile.ZipFile(self.root / "orgfeed" / f"{package_id}.{version}.nupkg", "w") as z:
            z.writestr(f"{package_id}.nuspec", nuspec)

    def host_repo(self, name, pins, test_body=None, code="namespace Host; public class B : Up.A {}"):
        """A host like meshNet: a deployable project with exact pins, and (with test_body) a test project in its own
        solution, the two targets its CI builds and tests."""
        files = {"Host.App/Host.App.csproj": self.pinned_lib(*pins), "Host.App/Class.cs": code}
        solution = '<Project Path="Host.App/Host.App.csproj" />'
        if test_body is not None:
            files["Host.Tests/Host.Tests.csproj"] = TEST_CSPROJ.format(
                refs='<ProjectReference Include="../Host.App/Host.App.csproj" />')
            files["Host.Tests/T.cs"] = f"namespace T; public class P {{ [Xunit.Fact] public void F() {{ {test_body} }} }}"
            solution += '<Project Path="Host.Tests/Host.Tests.csproj" />'
        files[f"{name}.Tests.slnx"] = f"<Solution>{solution}</Solution>"
        files[f"{name}.Api.slnx"] = '<Solution><Project Path="Host.App/Host.App.csproj" /></Solution>'
        self.repo(name, files, org_feed=True)

    def test_a_host_builds_after_the_packages_it_consumes_against_the_train_versions_and_is_promoted_with_its_pins(self):
        """meshNet #301: the host pinned meshNet.Networks 0.0.2474.11566 while its code needed the 0.0.2480.14757 the train
        had just published, and nothing built the host in the train. Now the host is built and tested last, after the
        package it consumes was packed: its exact pin on the in-train package moves to the train version and its pin on a
        package outside the train moves forward to that package's newest release, exactly as a package's pins do. Nothing
        of the host is packed; on a live train the pins are its train commit, and main and release move to it."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.repo("Side", {"Side.Lib/Side.Lib.csproj": csproj([]), "Side.Lib/Class.cs": "namespace Side; class S {}"})
        self.host_repo("Host", [("Up.Lib", "0.0.1.1"), ("Side.Lib", "0.0.1.1")],
                       test_body="Xunit.Assert.NotNull(new Host.B());")
        self.release_branches("Up", "Side", "Host")
        subprocess.run(["git", "tag", "train/0.0.2.1", "main"], cwd=self.bare("Side"), check=True)
        for version in ("0.0.1.1", "0.0.2.1"):                       # Side is unchanged since its newest release
            self.released("Side.Lib", version, self.rev("Side", "main"))
        released = self.rev("Host", "release")
        self.manifest([{"repo": "Up", "test": False}, {"repo": "Side", "test": False}],
                      [{"repo": "Host", "build": "Host.Api.slnx", "solution": "Host.Tests.slnx", "deploy": "dispatch-deploy"}])
        self.live()
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("then hosts Host", plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["selected"], state["hosts_selected"], state["train_ids"]), (["Up"], ["Host"], ["Up.Lib"]))
        self.assertEqual(state["repos"]["Host"]["reason"], "consumes 1 package(s) in this train: Up.Lib")
        self.assertEqual(state["repos"]["Host"]["consumes"], ["Side.Lib", "Up.Lib"])
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        self.assertLess(build.stdout.index("$ dotnet pack Up.Lib"), build.stdout.index("$ dotnet build Host.Api.slnx"))
        self.assertIn("$ dotnet test Host.Tests.slnx", build.stdout)
        self.assertIn("Passed!", build.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        version = state["version"]
        pins = sorted(tuple(p[1:]) for p in state["repos"]["Host"]["pins"])
        self.assertEqual(pins, [("Side.Lib", "0.0.1.1", "0.0.2.1"), ("Up.Lib", "0.0.1.1", version)])
        self.assertEqual([p["id"] for p in state["packages"]], ["Up.Lib"])          # the host is never packed
        self.assertEqual(sorted(p.name for p in (self.root / "train" / "feed").glob("*.nupkg")), [f"Up.Lib.{version}.nupkg"])
        self.publish()
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(state["promoted"], ["Up", "Host"])                          # the host last
        main = self.rev("Host", "main")
        self.assertEqual((main, self.rev("Host", "release")), (state["repos"]["Host"]["sha"],) * 2)
        self.assertEqual(self.rev("Host", "main^"), released)
        self.assertEqual(self.rev("Host", f"train/{version}^{{commit}}"), main)
        text = self.file_at("Host", "main", "Host.App/Host.App.csproj")
        self.assertIn(f'Include="Up.Lib" Version="{version}"', text)
        self.assertIn('Include="Side.Lib" Version="0.0.2.1"', text)
        self.assertEqual(subprocess.run(["git", "log", "-1", "--format=%an %s", "main"], cwd=self.bare("Host"),
                                        capture_output=True, text=True).stdout.strip(),
                         f"release-train [train {version}] pin Up.Lib to {version}; Side.Lib to 0.0.2.1")
        record = self.train("record")
        self.assertIn("| Host (host) | yes | consumes 1 package(s) in this train: Up.Lib | Side.Lib, Up.Lib |", record.stdout)
        self.assertIn("Promoted (main fast-forwarded, tagged `train/" + version + "`): Up, Host.", record.stdout)

    def test_a_failing_host_fails_the_train_before_anything_is_published(self):
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.host_repo("Host", [("Up.Lib", "0.0.1.1")], test_body="Xunit.Assert.True(false);")
        self.manifest([{"repo": "Up", "test": False}], [{"repo": "Host", "solution": "Host.Tests.slnx"}])
        self.assertEqual(self.train("plan").returncode, 0)
        build = self.train("build")
        self.assertNotEqual(build.returncode, 0, build.stdout)
        self.assertIn("Failed!", build.stdout)                                       # the host's test gate, not a restore
        self.assertIn("$ dotnet pack Up.Lib", build.stdout)                          # the package was packed first ...
        # ... but publish reads the package list build writes only once every repository AND host passed: still empty.
        self.assertEqual(json.loads((self.root / "train/plan.json").read_text())["packages"], [])

    def test_a_host_that_consumes_nothing_in_the_train_rides_only_when_its_ref_is_ahead_of_main(self):
        """No package changed: an unchanged host is left alone. Once its release has commits main lacks it rides (a dry run
        builds it; nothing is packed), and its stale pin on a package outside the train moves forward to that package's
        newest release, the same newest(published, carry) map a package's pins take. A dry-run ref override may name it."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.host_repo("Host", [("Up.Lib", "0.0.0.1")], code="namespace Host; public class B {}")
        self.release_branches("Up", "Host")
        for name in ("Up", "Host"):
            subprocess.run(["git", "tag", "train/0.0.1.1", "main"], cwd=self.bare(name), check=True)
        self.released("Up.Lib", "0.0.1.1", self.rev("Up", "main"))
        self.manifest([{"repo": "Up", "test": False}], [{"repo": "Host", "build": "Host.Api.slnx", "test": False}])
        self.feed_env["TRAIN_REF"] = "release"
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["selected"], state["hosts_selected"]), ([], []))
        self.assertEqual(state["repos"]["Host"]["reason"], "unchanged since train/0.0.1.1; consumes nothing in this train")
        self.assertIn("Nothing changed since the last train", plan.stdout)
        self.commit_to("Host", "release", {"README.md": "a host-only change"})
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["selected"], state["hosts_selected"]), ([], ["Host"]))
        self.assertEqual(state["repos"]["Host"]["reason"], "release is ahead of main; consumes nothing in this train")
        self.assertEqual(state["published"], {"Up.Lib": "0.0.1.1"})
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual([tuple(p[1:]) for p in state["repos"]["Host"]["pins"]], [("Up.Lib", "0.0.0.1", "0.0.1.1")])
        self.assertNotIn("train_commits", state["repos"]["Host"])                    # a dry run commits nothing
        self.assertEqual(list((self.root / "train" / "feed").glob("*.nupkg")), [])
        lane = self.commit_to("Host", "lane", {"Host.App/Lane.cs": "namespace Host; class L {}"}, new=True)
        self.feed_env["TRAIN_REF_OVERRIDES"] = "Host=lane"
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["repos"]["Host"]["ref"], state["repos"]["Host"]["sha"]), ("lane", lane))

    def test_promote_only_finishes_a_stranded_host_with_the_pins_that_train_built_it_against(self):
        """A host stranded by a live train (its packages published, its main not moved) is finished like a package
        repository: promote-only re-derives the train's pins on the built sha, fast-forwards main (its deploy) and tags."""
        version = "0.0.2480.14757"
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.host_repo("Host", [("Up.Lib", "0.0.1.1")])
        self.release_branches("Up", "Host")
        subprocess.run(["git", "tag", f"train/{version}", "main"], cwd=self.bare("Up"), check=True)  # Up was promoted
        self.released("Up.Lib", version, self.rev("Up", "main"))
        built = self.rev("Host", "release")
        self.manifest(["Up"], [{"repo": "Host", "solution": "Host.Tests.slnx"}])
        self.live()
        self.feed_env.update(TRAIN_VERSION=version, TRAIN_PROMOTE=f"Host={built}")
        done = self.train("promote-only")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(self.rev("Host", "main^"), built)
        self.assertIn(f'Include="Up.Lib" Version="{version}"', self.file_at("Host", "main", "Host.App/Host.App.csproj"))
        self.assertEqual(self.rev("Host", f"train/{version}^{{commit}}"), self.rev("Host", "main"))
        self.assertEqual(json.loads((self.root / "train/plan.json").read_text())["promoted"], ["Host"])

    def host_only_train(self):
        """meshNet's next train: every package repository is unchanged since its train tag, only the host's release is
        ahead of main, and the host's nuget.config maps its sources (packageSourceMapping), as meshNet's does."""
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}"})
        self.host_repo("Host", [("Up.Lib", "0.0.0.1")], code="namespace Host; public class B {}")
        self.commit_to("Host", "main", {"nuget.config":
            '<?xml version="1.0" encoding="utf-8"?><configuration><packageSources><clear />'
            '<add key="nuget.org" value="https://api.nuget.org/v3/index.json" />'
            f'<add key="org" value="{self.root / "orgfeed"}" /></packageSources><packageSourceMapping>'
            '<packageSource key="org"><package pattern="Up.*" /></packageSource>'
            '<packageSource key="nuget.org"><package pattern="*" /></packageSource>'
            '</packageSourceMapping></configuration>'})
        self.release_branches("Up", "Host")
        subprocess.run(["git", "tag", "train/0.0.1.1", "main"], cwd=self.bare("Up"), check=True)
        self.released("Up.Lib", "0.0.1.1", self.rev("Up", "main"))
        self.commit_to("Host", "release", {"README.md": "a host-only change"})
        self.manifest(["Up"], [{"repo": "Host", "build": "Host.Api.slnx", "test": False}])
        self.live()
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual((state["selected"], state["hosts_selected"], state["train_ids"]), ([], ["Host"], []))
        return state

    def test_a_host_only_train_restores_under_package_source_mapping_and_promotes_the_host(self):
        """Nothing is in the train, so the host's mapped nuget.config is left as it is (an empty train-local mapping
        fails restore), its stale pin still moves forward to the newest release, nothing is pushed to the feed, and the
        host is promoted and tagged."""
        state = self.host_only_train()
        mapped = (self.root / "train/src/Host/Host/nuget.config").read_text()
        build = self.train("build")
        self.assertEqual(build.returncode, 0, build.stdout)
        self.assertIn("$ dotnet build Host.Api.slnx", build.stdout)
        self.assertEqual((self.root / "train/src/Host/Host/nuget.config").read_text(), mapped)
        state = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual([tuple(p[1:]) for p in state["repos"]["Host"]["pins"]], [("Up.Lib", "0.0.0.1", "0.0.1.1")])
        self.assertEqual(state["packages"], [])
        publish = self.train("publish")
        self.assertEqual(publish.returncode, 0, publish.stdout)
        self.assertNotIn("dotnet nuget push", publish.stdout)
        promote = self.train("promote")
        self.assertEqual(promote.returncode, 0, promote.stdout)
        version = state["version"]
        self.assertEqual(self.rev("Host", f"train/{version}^{{commit}}"), self.rev("Host", "main"))
        self.assertIn('Include="Up.Lib" Version="0.0.1.1"', self.file_at("Host", "main", "Host.App/Host.App.csproj"))
        record = self.train("record")
        self.assertEqual(record.returncode, 0, record.stdout)
        self.assertIn("Packages: 1 considered, 0 in this train", record.stdout)
        self.assertIn(f"Promoted (main fast-forwarded, tagged `train/{version}`): Host.", record.stdout)

    def test_promote_only_finishes_a_stranded_host_only_train_with_the_pins_its_build_derived(self):
        """A host-only train published nothing, so the feed has no package at its version; promote-only still finishes it
        when every entry is a host, with the pins re-derived from the releases below the version. A package repository
        named for such a version is still refused: only the feed can vouch for a train that should have published."""
        state = self.host_only_train()
        version, built = state["version"], state["repos"]["Host"]["sha"]
        self.feed_env.update(TRAIN_VERSION=version, TRAIN_PROMOTE=f"Host={built}, Up={self.rev('Up', 'release')}")
        refused = self.train("promote-only")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn(f"no package of any manifest repository is on the feed at {version}", refused.stdout)
        self.assertFalse(self.is_ancestor("Host", built, "main"))
        self.feed_env["TRAIN_PROMOTE"] = f"Host={built}"                             # what the stranded summary prints
        done = self.train("promote-only")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(self.rev("Host", "main^"), built)
        self.assertIn('Include="Up.Lib" Version="0.0.1.1"', self.file_at("Host", "main", "Host.App/Host.App.csproj"))
        self.assertEqual(self.rev("Host", f"train/{version}^{{commit}}"), self.rev("Host", "main"))
        self.assertEqual(json.loads((self.root / "train/plan.json").read_text())["promoted"], ["Host"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

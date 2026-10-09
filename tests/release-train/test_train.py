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
from datetime import datetime, timezone
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
        _, self.script = load_train(self.tmp.name)
        self.server = self.root / "server"
        self.feed_env = dict(os.environ, GITHUB_SERVER_URL=f"file://{self.server}", TRAIN_WORK=str(self.root / "train"),
                             TRAIN_REF="main", TRAIN_DRY_RUN="true", PYTHONUTF8="1")

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, name, files):
        work = self.root / "make" / name
        work.mkdir(parents=True)
        (work / "Directory.Build.props").write_text(PROPS)
        (work / "nuget.config").write_text('<?xml version="1.0" encoding="utf-8"?><configuration><packageSources>'
                                           '<add key="nuget.org" value="https://api.nuget.org/v3/index.json" />'
                                           '</packageSources></configuration>')
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
            (work / rel).write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{name} on {branch}"],
                       cwd=work, check=True)
        subprocess.run(["git", "push", "-q", str(self.server / "o" / f"{name}.git"), branch], cwd=work, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True).stdout.strip()

    def two_repos(self):
        self.repo("Up", {"Up.Lib/Up.Lib.csproj": csproj([]), "Up.Lib/Class.cs": "namespace Up; public class A {}",
                         "Up.slnx": '<Solution><Project Path="Up.Lib/Up.Lib.csproj" /></Solution>'})
        self.repo("Down", {"Down.Lib/Down.Lib.csproj": csproj(["Up.Lib"]),
                           "Down.Lib/Class.cs": "namespace Down; public class B : Up.A {}",
                           "Down.slnx": '<Solution><Project Path="Down.Lib/Down.Lib.csproj" /></Solution>'})

    def manifest(self, repos):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"org": "o", "repos": repos}))
        self.feed_env["TRAIN_MANIFEST"] = str(path)

    def train(self, command):
        # Git creates read-only pack files on Windows. Make this throwaway fixture writable before
        # a second plan removes it; production Actions runs on Linux. Do not change train behavior.
        if os.name == "nt" and command == "plan":
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
        self.assertIn("Down: pinned 1 reference(s) to " + version, build.stdout)
        pins = [[Path(path).as_posix(), package, old, new] for path, package, old, new in state["repos"]["Down"]["pins"]]
        self.assertEqual(pins, [["Down.Lib/Down.Lib.csproj", "Up.Lib", "0.0.1.1", version]])
        down = next(p for p in state["packages"] if p["id"] == "Down.Lib")
        self.assertEqual(down["dependencies"], [f"Up.Lib {version}"])
        self.assertIn(f'Include="Up.Lib" Version="{version}"',
                      (self.root / "train" / "src" / "Down" / "Down" / "Down.Lib" / "Down.Lib.csproj").read_text())
        self.assertNotIn("pinned", state["repos"]["Down"])  # a dry run commits nothing

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
        self.manifest(["Up", "Down"])
        plan = self.train("plan")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        result = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(result["selected"], ["Down"])
        self.assertEqual(result["repos"]["Up"]["reason"], "unchanged since train/0.0.1.1")
        self.assertEqual(result["train_ids"], ["Down.Lib"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

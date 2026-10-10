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

    def manifest(self, repos):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"org": "o", "repos": repos}))
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

    def test_the_next_train_reconciles_main_s_pin_commit_and_moves_out_of_train_pins_to_the_latest_tag(self):
        """Rule (a)'s second half: after a train left main ahead of release by its pin commit, the next live plan accepts
        the repository instead of refusing it, and promotion fast-forwards both release and main again. An exact pin on a
        package not rebuilt in that train moves to its repository's latest train tag version."""
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
        self.assertEqual(state["latest"], {"Up.Lib": first})
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
        self.assertIn(f"Down: Down.Lib not on the feed at {version}", unpublished.stdout)
        (self.root / "orgfeed" / f"Down.Lib.{version}.nupkg").write_bytes(b"published by the stranded train")
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

    def test_promote_only_re_derives_the_out_of_train_pins_the_train_packed(self):
        """Step D's probe (review of fa42553): a train that rebuilt only Down packed Down's exact pin on Up.Lib moved to Up's
        latest train tag; promote-only must commit that same pin, so main and the tag carry what was published."""
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
        # A lane on Down's release (which now carries train 1's pin commit) writes the old exact pin back.
        work = self.root / "make" / "Down"
        subprocess.run(["git", "checkout", "-q", "release"], cwd=work, check=True)
        subprocess.run(["git", "fetch", "-q", str(self.bare("Down")), "release"], cwd=work, check=True)
        subprocess.run(["git", "reset", "-q", "--hard", "FETCH_HEAD"], cwd=work, check=True)
        self.commit_to("Down", "release", {"Down.Lib/Lane.cs": "namespace Down; public class Lane {}",
                                           "Down.Lib/Down.Lib.csproj": self.pinned_lib(("Up.Lib", "0.0.1.1"))})
        # Train 2 rebuilds Down only and publishes it; its promotion never happens (stranded).
        for step in ("plan", "build"):
            result = self.train(step)
            self.assertEqual(result.returncode, 0, result.stdout)
        second = json.loads((self.root / "train/plan.json").read_text())
        self.assertEqual(second["selected"], ["Down"])
        self.assertEqual([p[1:] for p in second["repos"]["Down"]["pins"]], [["Up.Lib", "0.0.1.1", first]])
        self.publish()
        self.feed_env.update(TRAIN_VERSION=second["version"], TRAIN_PROMOTE=f"Down={second['repos']['Down']['base']}")
        done = self.train("promote-only")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn(f'Include="Up.Lib" Version="{first}"', self.file_at("Down", "main", "Down.Lib/Down.Lib.csproj"))
        self.assertEqual(self.rev("Down", f"train/{second['version']}^{{commit}}"), self.rev("Down", "main"))
        self.assertEqual(json.loads((self.root / "train/plan.json").read_text())["latest"], {"Up.Lib": first})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

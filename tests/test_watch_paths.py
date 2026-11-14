"""Deploy target `watch_paths`: changes to an image's inputs make a merge a target member."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import apply, config, deploy, dispatch
from tests.test_factory import git, make_repo
from tests.test_github_identity import pin_for_module

LOAD = config.load  # the real loader; tests patch config.load for apply.main


def setUpModule() -> None:
    pin_for_module()


WATCHED = '''[apply]
enabled = true
[apply.targets.web]
dir = "deploy/web"
watch_paths = ["Dockerfile", "app/**"]
'''
UNWATCHED = '''[apply]
enabled = true
[apply.targets.web]
dir = "deploy/web"
'''


class WatchPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name), WATCHED)
        self.use(WATCHED)
        # The fixture's own history is live; only later merges are judged.
        self.deployed(self.commit("deploy/web/main.tf"))

    def use(self, toml: str) -> None:
        (self.repo / config.CONFIG_NAME).write_text(toml)
        self.cfg = LOAD(self.repo)
        dispatch.configure(self.cfg)
        apply.configure(self.cfg)
        self.cfg.factory.mkdir(parents=True, exist_ok=True)
        load = mock.patch.object(config, "load", return_value=self.cfg)
        load.start()
        self.addCleanup(load.stop)

    def commit(self, path: str) -> str:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(file.read_text() + "#\n" if file.exists() else "# v1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"change {path}")
        return git(self.repo, "rev-parse", "HEAD")

    def deployed(self, commit: str) -> None:
        deploy.record_deploy_run(deploy.DeployRun(
            run_id=f"deploy-web-{commit[:8]}-1", target="web", commit=commit, ticket=1, attempt=1,
            status=deploy.DeployStatus.SUCCEEDED, started_at="2026-11-01T00:00:00Z"), events_path=dispatch.EVENTS)

    def reviewed(self, pr: int, commit: str) -> dict:
        return {"pr": pr, "ticket": pr, "commit": commit, "head_commit": commit, "reviewDecision": "APPROVED",
                "latestReviews": [{"state": "APPROVED", "author": {"login": "human"},
                                   "authorAssociation": "MEMBER", "commit": {"oid": commit}}]}

    def select(self, *prs: dict, watch: bool = True):
        target = self.cfg.targets["web"]
        return deploy.select_candidates_for_target(
            target_name="web", target_dir=target.dir, all_prs=list(prs), root=self.repo,
            events_path=dispatch.EVENTS, fetch_evidence_fn=lambda pr: None,
            watch_paths=target.watch_paths if watch else ())

    def test_membership_matches_dir_or_watch_paths(self) -> None:
        web = self.cfg.targets["web"]
        self.assertEqual(web.watch_paths, ("Dockerfile", "app/**"))
        for path, member in (("Dockerfile", True), ("app/server.py", True), ("app/sub/handler.py", True),
                             ("deploy/web/vars.tf", True), ("README.md", False), ("docs/Dockerfile", False)):
            with self.subTest(path=path):
                self.assertEqual(apply.touches_target(self.commit(path), web), member)

        unwatched = config.DeployTarget(name="web", dir="deploy/web")
        self.assertFalse(apply.touches_target(self.commit("Dockerfile"), unwatched))

    def test_star_stays_within_one_directory(self) -> None:
        shallow = config.DeployTarget(name="web", dir="deploy/web", watch_paths=("app/*",))
        self.assertTrue(apply.touches_target(self.commit("app/server.py"), shallow))
        self.assertFalse(apply.touches_target(self.commit("app/sub/handler.py"), shallow))

    def test_dockerfile_only_merge_is_a_candidate(self) -> None:
        commit = self.commit("Dockerfile")
        (candidate,), unauthorized = self.select(self.reviewed(5, commit))
        self.assertEqual(unauthorized, [])
        self.assertEqual(candidate["commit"], commit)
        self.assertNotIn("unauthorized_reason", candidate)
        self.assertEqual(apply.target_dirs_touched(commit), ["web"])

    def test_without_watch_paths_behaviour_is_unchanged(self) -> None:
        self.use(UNWATCHED)
        self.assertEqual(self.cfg.targets["web"].watch_paths, ())
        commit = self.commit("Dockerfile")
        self.assertEqual(self.select(self.reviewed(5, commit)), ([], []))
        self.assertEqual(apply.target_dirs_touched(commit), [])
        tf = self.commit("deploy/web/vars.tf")
        (candidate,), _ = self.select(self.reviewed(6, tf))
        self.assertEqual(candidate["commit"], tf)

    def test_unreviewed_watch_path_merge_still_needs_authorization(self) -> None:
        commit = self.commit("app/server.py")
        (candidate,), _ = self.select({"pr": 7, "ticket": 7, "commit": commit, "head_commit": commit,
                                       "reviewDecision": "REVIEW_REQUIRED", "latestReviews": []})
        self.assertIn("unauthorized_reason", candidate)

    def test_direct_merge_touching_only_watch_paths_is_unauthorized(self) -> None:
        direct = self.commit("Dockerfile")
        self.assertEqual(self.select(), ([], [direct]))
        self.assertEqual(self.select(watch=False), ([], []))

    def authorize(self, merge: str) -> tuple[int, list[str]]:
        view = {"state": "MERGED", "headRefName": "ops/base-image", "headRefOid": "d" * 40,
                "mergeCommit": {"oid": merge}}
        logged: list[str] = []
        with mock.patch.object(dispatch, "gh_json", return_value=view), \
             mock.patch.object(apply, "log", side_effect=logged.append), \
             mock.patch.dict(os.environ, {"USER": "alice"}):
            code = apply.main(["--authorize-revision", "40", "--reason", "reviewed", "--dry-run"])
        return code, logged

    def test_authorize_revision_accepts_a_watch_path_only_merge(self) -> None:
        code, logged = self.authorize(self.commit("Dockerfile"))
        self.assertEqual(code, 0, logged)

        self.use(UNWATCHED)
        code, logged = self.authorize(self.commit("Dockerfile"))
        self.assertEqual(code, 1)
        self.assertIn("touches no enabled deploy target", logged[-1])


class WatchPathsConfigTest(unittest.TestCase):
    def load(self, toml: str) -> config.Config:
        with tempfile.TemporaryDirectory() as d:
            return config.load(make_repo(Path(d), toml))

    def test_legacy_single_target_form_carries_watch_paths(self) -> None:
        cfg = self.load('[apply]\nenabled = true\ndir = "deploy"\nwatch_paths = ["Dockerfile"]\n')
        self.assertEqual(cfg.targets["default"].watch_paths, ("Dockerfile",))

    def test_overlap_with_another_target_is_allowed(self) -> None:
        cfg = self.load('[apply.targets.a]\ndir = "a"\nwatch_paths = ["Dockerfile", "b/**"]\n'
                        '[apply.targets.b]\ndir = "b"\nwatch_paths = ["Dockerfile"]\n')
        self.assertEqual(cfg.targets["a"].watch_paths, ("Dockerfile", "b/**"))

    def test_invalid_watch_paths_are_rejected(self) -> None:
        cases = {
            '"Dockerfile"': "must be a list of path patterns",
            '[""]': "entries must be non-empty strings",
            "[1]": "entries must be non-empty strings",
            '[" app/** "]': "leading or trailing whitespace",
            '["/Dockerfile"]': "no leading /",
            '["app/../secrets/**"]': "must not contain '..' segments",
            '[".."]': "must not contain '..' segments",
            '[":(exclude)app"]': "not a git pathspec",
        }
        for value, message in cases.items():
            with self.subTest(value=value), self.assertRaisesRegex(config.ConfigError, message):
                self.load(f'[apply.targets.web]\ndir = "deploy/web"\nwatch_paths = {value}\n')
        with self.assertRaisesRegex(config.ConfigError, "applies to the implicit default target only"):
            self.load('[apply]\nwatch_paths = ["Dockerfile"]\n[apply.targets.web]\ndir = "deploy/web"\n')


if __name__ == "__main__":
    unittest.main()

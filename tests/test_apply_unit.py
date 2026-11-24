"""`factory install` runs the apply lane after every pass, in its own unit.

The pass unit starts `<unit>-apply.service` on the way out and orders itself after it,
so an approved merge deploys in the cycle that merged it and the next pass never re-pins
the root under a running apply. `[apply].env` never enters a unit environment.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import config, onboard, verify_secrets

SERVICE, APPLY = "factory-widgets.service", "factory-widgets-apply.service"


def make_cfg(root: Path, *, lane: bool = True, **install) -> config.Config:
    (root / config.CONFIG_NAME).write_text('[repo]\nslug = "acme/widgets"\n')
    cfg = config.Config(root, "acme/widgets", signoff=False)
    cfg.install = config.merge(config.DEFAULT_INSTALL, install)
    cfg.apply_enabled = lane  # the repo's committed [apply].enabled
    cfg.apply_env = {"NOMAD_TOKEN": "apply-secret", "TF_VAR_region": "example"}
    return cfg


class ApplyUnitRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_repo_without_apply_lane_renders_no_apply_unit(self) -> None:
        units = onboard.units(make_cfg(self.root, lane=False), "10min", "127.0.0.1")
        self.assertNotIn(APPLY, units)
        self.assertNotIn("apply", units[SERVICE])

    def test_pass_starts_the_apply_unit_after_watch_and_waits_behind_it(self) -> None:
        units = onboard.units(make_cfg(self.root), "10min", "127.0.0.1")
        service = units[SERVICE].splitlines()
        self.assertIn(f"After={APPLY}", service[:service.index("[Service]")])
        post = [line for line in service if line.startswith("ExecStopPost=")]
        self.assertEqual(len(post), 2)
        self.assertTrue(post[0].endswith(" watch"))
        self.assertEqual(post[1], f"ExecStopPost=-systemctl --user start --no-block {APPLY}")

    def test_apply_unit_is_a_oneshot_apply_pass_with_the_dispatcher_identity_only(self) -> None:
        cfg = make_cfg(self.root, env={"UV_EXCLUDE_NEWER": "7 days"}, dispatch_env={"GH_CONFIG_DIR": "/machine/gh"})
        units = onboard.units(cfg, "10min", "127.0.0.1")
        body = units[APPLY]
        self.assertIn("Type=oneshot\n", body)
        self.assertIn(f"WorkingDirectory={self.root}\n", body)
        [start] = [line for line in body.splitlines() if line.startswith("ExecStart=")]
        self.assertTrue(start.endswith(" -P -m factory apply"))
        self.assertNotIn("[Install]", body)  # never enabled: only a pass starts it
        env = [line for line in body.splitlines() if line.startswith("Environment=")]
        self.assertEqual(env, [line for line in units[SERVICE].splitlines() if line.startswith("Environment=")])
        self.assertIn("Environment=GH_CONFIG_DIR=/machine/gh", env)
        for key, value in cfg.apply_env.items():
            for unit in units.values():
                self.assertNotIn(key, unit)
                self.assertNotIn(value, unit)

    def test_host_opt_out_renders_no_trigger(self) -> None:
        units = onboard.units(make_cfg(self.root, apply=False), "10min", "127.0.0.1")
        self.assertNotIn(APPLY, units)
        self.assertNotIn("--no-block", units[SERVICE])
        self.assertNotIn("After=", units[SERVICE])

    def test_install_apply_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / config.CONFIG_NAME).write_text('[repo]\nslug = "acme/widgets"\n')
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(root / "xdg")}):
                self.assertTrue(config.load(root).install["apply"])
                (root / "xdg" / "factory").mkdir(parents=True)
                (root / "xdg" / "factory" / "config.toml").write_text('[defaults.install]\napply = "no"\n')
                with self.assertRaisesRegex(config.ConfigError, r"\[install\]\.apply"):
                    config.load(root)

    def test_apply_unit_is_inspected_and_required(self) -> None:
        cfg = make_cfg(self.root)
        self.assertIn(APPLY, verify_secrets.units_for(cfg))
        self.assertIn(APPLY, verify_secrets.required_units(cfg))
        cfg.install["apply"] = False
        self.assertNotIn(APPLY, verify_secrets.units_for(cfg))
        self.assertNotIn(APPLY, verify_secrets.required_units(cfg))


class ApplyUnitInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.units = self.root / "units"

    def install(self, cfg: config.Config, argv: list[str], *, apply_active: str = "inactive"):
        calls: list[list[str]] = []

        def command(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
            calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return subprocess.CompletedProcess(cmd, 0, f"{apply_active if cmd[-1] == APPLY else 'inactive'}\n", "")
            if cmd[0] == "loginctl":
                return subprocess.CompletedProcess(cmd, 0, "yes\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(onboard.config, "load", return_value=cfg), \
                patch.object(onboard.config, "host_config_path", return_value=self.root / "config.toml"), \
                patch.object(onboard, "unit_dir", return_value=self.units), \
                patch.object(onboard, "sh", side_effect=command), \
                patch.object(onboard.shutil, "which", return_value="/bin/systemctl"):
            code = onboard.install(argv)
        return code, calls

    def test_install_writes_but_never_enables_or_starts_the_apply_unit(self) -> None:
        code, calls = self.install(make_cfg(self.root), [])
        self.assertEqual(code, 0)
        self.assertTrue((self.units / APPLY).exists())
        self.assertFalse([c for c in calls if c[0] == "systemctl" and APPLY in c])

    def test_opting_out_removes_an_idle_apply_unit(self) -> None:
        self.install(make_cfg(self.root), ["--no-start"])
        code, _ = self.install(make_cfg(self.root, apply=False), ["--no-start"])
        self.assertEqual(code, 0)
        self.assertFalse((self.units / APPLY).exists())
        self.assertNotIn("--no-block", (self.units / SERVICE).read_text())

    def test_opting_out_refuses_while_an_apply_runs(self) -> None:
        self.install(make_cfg(self.root), ["--no-start"])
        with self.assertRaisesRegex(config.ConfigError, "let the apply finish"):
            self.install(make_cfg(self.root, apply=False), [], apply_active="activating")
        self.assertTrue((self.units / APPLY).exists())


TIMER = "factory-widgets.timer"


class ApplyOnlyHostTest(unittest.TestCase):
    """`[install].dispatch = false`: a host that only deploys. The timer starts the apply
    unit directly and no triage/dispatch pass unit exists."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.units = self.root / "units"

    def test_timer_starts_the_apply_unit_and_no_pass_unit_is_rendered(self) -> None:
        cfg = make_cfg(self.root, dispatch=False, dispatch_env={"GH_CONFIG_DIR": "/machine/gh"})
        cfg.triage_service = "ollama.service"
        cfg.eval_schedule = True
        units = onboard.units(cfg, "10min", "127.0.0.1")
        self.assertEqual(set(units), {APPLY, TIMER, "factory-widgets-dashboard.service"})
        self.assertIn(f"Unit={APPLY}\n", units[TIMER])
        self.assertIn("OnUnitActiveSec=10min\n", units[TIMER])
        body = units[APPLY]
        [start] = [line for line in body.splitlines() if line.startswith("ExecStart=")]
        self.assertTrue(start.endswith(" -P -m factory apply"))
        self.assertIn("Environment=GH_CONFIG_DIR=/machine/gh", body)
        self.assertNotIn("[Install]", body)
        for key, value in cfg.apply_env.items():
            for unit in units.values():
                self.assertNotIn(key, unit)
                self.assertNotIn(value, unit)

    def test_nothing_to_schedule_is_a_config_error(self) -> None:
        for cfg in (make_cfg(self.root, lane=False, dispatch=False), make_cfg(self.root, apply=False, dispatch=False)):
            with self.assertRaisesRegex(config.ConfigError, r"\[install\]\.dispatch = false"):
                onboard.units(cfg, "10min", "127.0.0.1")

    def test_install_dispatch_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / config.CONFIG_NAME).write_text('[repo]\nslug = "acme/widgets"\n')
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(root / "xdg")}):
                cfg = config.load(root)
                self.assertEqual(cfg.dispatch_unit, f"factory-{cfg.name}.service")
                (root / "xdg" / "factory").mkdir(parents=True)
                host = root / "xdg" / "factory" / "config.toml"
                host.write_text('[defaults.install]\ndispatch = false\n')
                self.assertIsNone(config.load(root).dispatch_unit)
                host.write_text('[defaults.install]\ndispatch = "no"\n')
                with self.assertRaisesRegex(config.ConfigError, r"\[install\]\.dispatch"):
                    config.load(root)

    def test_only_the_apply_unit_is_inspected_and_required(self) -> None:
        cfg = make_cfg(self.root, dispatch=False)
        self.assertNotIn(SERVICE, verify_secrets.units_for(cfg))
        self.assertEqual(verify_secrets.required_units(cfg), {APPLY})
        self.assertEqual(verify_secrets.expected_unit_env(cfg, APPLY), verify_secrets.expected_unit_env(make_cfg(self.root), APPLY))

    def install(self, cfg: config.Config, argv: list[str], *, active: dict | None = None):
        calls: list[list[str]] = []

        def command(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
            calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return subprocess.CompletedProcess(cmd, 0, f"{(active or {}).get(cmd[-1], 'inactive')}\n", "")
            if cmd[0] == "loginctl":
                return subprocess.CompletedProcess(cmd, 0, "yes\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(onboard.config, "load", return_value=cfg), \
                patch.object(onboard.config, "host_config_path", return_value=self.root / "config.toml"), \
                patch.object(onboard, "unit_dir", return_value=self.units), \
                patch.object(onboard, "sh", side_effect=command), \
                patch.object(onboard.shutil, "which", return_value="/bin/systemctl"):
            code = onboard.install(argv)
        return code, calls

    def test_switching_to_apply_only_removes_the_idle_pass_unit_and_restarts_the_timer(self) -> None:
        cfg = make_cfg(self.root)
        cfg.triage_service = "ollama.service"
        self.install(cfg, [])
        self.assertTrue((self.units / SERVICE).exists())
        only = make_cfg(self.root, dispatch=False)
        only.triage_service = "ollama.service"
        code, calls = self.install(only, [])
        self.assertEqual(code, 0)
        self.assertFalse((self.units / SERVICE).exists())
        self.assertFalse((self.units / "factory-widgets-triage-probe.timer").exists())
        self.assertIn(f"Unit={APPLY}", (self.units / TIMER).read_text())
        self.assertIn(["systemctl", "--user", "restart", TIMER], calls)
        self.assertFalse([c for c in calls if c[0] == "systemctl" and c[-1] == APPLY and c[2] != "is-active"])

    def test_switching_refuses_while_a_pass_runs(self) -> None:
        self.install(make_cfg(self.root), ["--no-start"])
        with self.assertRaisesRegex(config.ConfigError, "let the pass finish"):
            self.install(make_cfg(self.root, dispatch=False), [], active={SERVICE: "activating"})
        self.assertTrue((self.units / SERVICE).exists())


if __name__ == "__main__":
    unittest.main()

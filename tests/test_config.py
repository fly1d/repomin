import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repomin.config import ConfigError, config_option_present, expand_config_args


def _minimal_spec():
    return {
        "schema_version": 1,
        "failure": {"command": "false", "exit_code": 1},
    }


class _ConfigTestCase(unittest.TestCase):
    def _write(self, root: Path, document, name: str = "spec.json") -> Path:
        path = root / name
        if isinstance(document, str):
            path.write_text(document, encoding="utf-8")
        else:
            path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _expand(self, document, *, command="reduce", extra=()):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), document)
            return expand_config_args(
                ["repository", "--config", str(path), *extra],
                command=command,
            )

    def _assert_invalid(self, document, message, *, command="reduce"):
        with self.assertRaisesRegex(ConfigError, message):
            self._expand(document, command=command)


class ConfigExpansionTests(_ConfigTestCase):
    def test_absent_config_returns_an_equal_list(self):
        argv = ["repository", "--command=false", "--exit-code=1"]
        self.assertEqual(argv, expand_config_args(argv, command="reduce"))

    def test_config_like_source_after_option_terminator_is_not_loaded(self):
        argv = ["--", "--config", "ordinary-source"]
        self.assertEqual(argv, expand_config_args(argv, command="reduce"))
        self.assertFalse(config_option_present(argv))

    def test_config_option_detection_matches_expansion_boundary(self):
        self.assertTrue(config_option_present(["repo", "--config", "spec.json"]))
        self.assertTrue(config_option_present(["--config=spec.json", "repo"]))
        self.assertFalse(config_option_present(["repo", "--", "--config=spec.json"]))

    def test_command_must_be_supported_even_without_config(self):
        with self.assertRaisesRegex(ConfigError, "command"):
            expand_config_args([], command="replay")

    def test_reduce_expands_every_field_with_safe_value_tokens(self):
        spec = {
            "schema_version": 1,
            "failure": {
                "command": "--not-an-option && test -f required.txt",
                "match": "failure text",
                "exit_code": 7,
                "signature": "python_exception",
            },
            "execution": {
                "timeout_seconds": 2.5,
                "backend": "docker",
                "jobs": 3,
                "cache": False,
                "docker": {
                    "image": "example:test",
                    "network": "none",
                    "cpus": 1.5,
                    "memory": "2GiB",
                    "pids_limit": 128,
                    "tmpfs_size": "512MiB",
                    "workspace_limit": "4GiB",
                },
            },
            "sampling": {
                "baseline_runs": 5,
                "min_baseline_passes": 3,
                "candidate_runs": 4,
                "min_candidate_passes": 2,
                "min_baseline_rate": 0.4,
                "min_candidate_rate": 0.3,
                "confidence": 0.9,
                "run_confidence": 0.8,
                "holdout": {"runs": 6, "min_rate": 0.25, "confidence": 0.95},
            },
            "reduction": {
                "adapter": "maven",
                "source_reducer": "java",
                "max_attempts": 100,
                "max_duration_seconds": 30.5,
            },
            "inputs": {
                "ignore_names": ["target", ".cache"],
                "ignore_paths": ["generated/code"],
                "keep_paths": ["src/main.py"],
                "text_files": ["README.md"],
                "gitignore": True,
                "gitignore_files": ["config/extra.ignore"],
                "gitignore_recursive": True,
            },
        }
        expanded = self._expand(
            spec,
            extra=("--output", "result", "--session=session", "--verbose"),
        )
        self.assertEqual("repository", expanded[0])
        self.assertIn("--command=--not-an-option && test -f required.txt", expanded)
        self.assertIn("--python-exception", expanded)
        self.assertIn("--docker-workspace-limit=4GiB", expanded)
        self.assertIn("--no-cache", expanded)
        self.assertIn("--holdout-confidence=0.95", expanded)
        self.assertIn("--max-duration=30.5", expanded)
        self.assertIn("--gitignore-recursive", expanded)
        self.assertEqual(
            ["--output", "result", "--session=session", "--verbose"],
            expanded[-4:],
        )
        for token in expanded[1:-4]:
            if token.startswith("--") and token not in {
                "--python-exception",
                "--no-cache",
                "--gitignore",
                "--gitignore-recursive",
            }:
                self.assertIn("=", token)

    def test_doctor_validates_but_does_not_expand_unsupported_fields(self):
        spec = {
            "schema_version": 1,
            "failure": {
                "command": "false",
                "match": "failed",
                "signature": "java_exception",
            },
            "execution": {
                "timeout_seconds": 4,
                "backend": "docker",
                "jobs": 2,
                "cache": False,
                "docker": {
                    "image": "example:test",
                    "network": "bridge",
                    "cpus": 2,
                    "memory": "1GiB",
                    "pids_limit": 10,
                    "tmpfs_size": "10MiB",
                    "workspace_limit": "2GiB",
                },
            },
            "sampling": {
                "baseline_runs": 3,
                "min_baseline_passes": 2,
                "min_baseline_rate": 0.1,
                "confidence": 0.9,
                "candidate_runs": 2,
                "min_candidate_passes": 1,
                "min_candidate_rate": 0.2,
                "run_confidence": 0.9,
                "holdout": {"runs": 2, "min_rate": 0.1},
            },
            "reduction": {
                "adapter": "python",
                "source_reducer": "python",
                "max_attempts": 9,
                "max_duration_seconds": 10,
            },
            "inputs": {"ignore_names": ["build"], "gitignore": True},
        }
        expanded = self._expand(spec, command="doctor", extra=("--json",))
        self.assertEqual(
            [
                "repository",
                "--command=false",
                "--match=failed",
                "--java-exception",
                "--timeout=4",
                "--backend=docker",
                "--docker-image=example:test",
                "--docker-network=bridge",
                "--docker-cpus=2",
                "--docker-memory=1GiB",
                "--docker-pids-limit=10",
                "--docker-tmpfs-size=10MiB",
                "--docker-workspace-limit=2GiB",
                "--baseline-runs=3",
                "--min-baseline-passes=2",
                "--min-baseline-rate=0.1",
                "--confidence=0.9",
                "--adapter=python",
                "--source-reducer=python",
                "--ignore=build",
                "--gitignore",
                "--json",
            ],
            expanded,
        )

    def test_config_equals_form_replaces_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), _minimal_spec())
            self.assertEqual(
                ["repo", "--command=false", "--exit-code=1", "--verbose"],
                expand_config_args(
                    ["repo", "--config=%s" % path, "--verbose"],
                    command="reduce",
                ),
            )

    def test_help_and_version_do_not_read_config(self):
        missing = "/definitely/missing/reduction-spec.json"
        self.assertEqual(
            ["--help"],
            expand_config_args(["--config", missing, "--help"], command="reduce"),
        )
        self.assertEqual(
            ["--version"],
            expand_config_args(
                ["--version", "--config=%s" % missing], command="doctor"
            ),
        )

    def test_config_must_appear_exactly_once_with_a_path(self):
        cases = (
            (["--config"], "requires a path"),
            (["--config="], "requires a path"),
            (["--config", "--help"], "requires a path"),
            (["--config=a", "--config=b"], "exactly once"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ConfigError, message):
                    expand_config_args(argv, command="reduce")

    def test_config_owns_all_semantic_options(self):
        semantic_options = (
            "--command=false",
            "--match=x",
            "--exit-code=1",
            "--timeout=2",
            "--no-cache",
            "--env=A=B",
            "--semantic-reducer=none",
            "--java-classpath=lib/x.jar",
            "--unknown=value",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), _minimal_spec())
            for option in semantic_options:
                with self.subTest(option=option):
                    with self.assertRaisesRegex(ConfigError, "combined with --config"):
                        expand_config_args(
                            ["repo", "--config=%s" % path, option],
                            command="reduce",
                        )

    def test_runtime_options_remain_allowed(self):
        expanded = self._expand(
            _minimal_spec(),
            extra=("--output=out", "--session", "state", "--resume", "--verbose"),
        )
        self.assertEqual(
            ["--output=out", "--session", "state", "--resume", "--verbose"],
            expanded[-5:],
        )

    def test_doctor_only_allows_its_runtime_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), _minimal_spec())
            for option in ("--session=state", "--resume", "--verbose"):
                with self.subTest(option=option):
                    with self.assertRaisesRegex(
                        ConfigError, "%s cannot be combined" % option.split("=", 1)[0]
                    ):
                        expand_config_args(
                            ["repo", "--config=%s" % path, option],
                            command="doctor",
                        )

            expanded = expand_config_args(
                ["repo", "--config=%s" % path, "--output=out", "--json"],
                command="doctor",
            )
            self.assertEqual(["--output=out", "--json"], expanded[-2:])


class ConfigSchemaTests(_ConfigTestCase):
    def test_rejects_unreadable_and_invalid_json(self):
        with self.assertRaisesRegex(ConfigError, "cannot read file"):
            expand_config_args(
                ["--config=/definitely/missing/spec.json"], command="reduce"
            )
        with patch("pathlib.Path.expanduser", side_effect=RuntimeError("unknown home")):
            with self.assertRaisesRegex(ConfigError, "cannot read file"):
                expand_config_args(["--config=~/spec.json"], command="reduce")
        self._assert_invalid("{", "invalid JSON")

    def test_rejects_excessively_nested_json_without_leaking_recursion_error(self):
        self._assert_invalid("[" * 2_000 + "0" + "]" * 2_000, "nesting is too deep")

    def test_rejects_duplicate_keys_at_their_object_path(self):
        self._assert_invalid(
            '{"schema_version":1,"failure":{"command":"a","command":"b","exit_code":1}}',
            "failure: duplicate key 'command'",
        )

    def test_rejects_nonfinite_numbers_at_their_field_path(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(token=token):
                self._assert_invalid(
                    '{"schema_version":1,"failure":{"command":"x","exit_code":1},'
                    '"execution":{"timeout_seconds":%s}}' % token,
                    "execution.timeout_seconds: number must be finite",
                )

    def test_top_level_is_exact_and_requires_schema_and_failure(self):
        cases = (
            ([], "config: expected an object"),
            ({"failure": {"command": "x", "exit_code": 1}}, "schema_version"),
            ({"schema_version": 1}, "failure"),
            (
                {
                    "schema_version": 1,
                    "failure": {"command": "x", "exit_code": 1},
                    "extra": True,
                },
                "unknown key 'extra'",
            ),
        )
        for document, message in cases:
            with self.subTest(document=document):
                self._assert_invalid(document, message)

    def test_runtime_and_advanced_cli_fields_are_excluded_from_the_schema(self):
        for key in (
            "source",
            "output",
            "session",
            "resume",
            "verbose",
            "env",
            "semantic_reducer",
            "java_classpath",
        ):
            with self.subTest(key=key):
                spec = _minimal_spec()
                spec[key] = "value"
                self._assert_invalid(spec, "unknown key %r" % key)

    def test_schema_version_is_exact_integer_one(self):
        for value in (True, 1.0, 0, 2, "1", None):
            with self.subTest(value=value):
                spec = _minimal_spec()
                spec["schema_version"] = value
                self._assert_invalid(spec, "schema_version")

    def test_failure_fields_are_strict(self):
        cases = (
            ({"exit_code": 1}, "failure.command"),
            ({"command": "", "exit_code": 1}, "failure.command"),
            ({"command": "x\x00y", "exit_code": 1}, "NUL"),
            ({"command": "x", "match": ""}, "failure.match"),
            ({"command": "x", "exit_code": True}, "failure.exit_code"),
            ({"command": "x", "exit_code": 1.0}, "failure.exit_code"),
            ({"command": "x", "signature": "other", "match": "x"}, "signature"),
            ({"command": "x"}, "one of match"),
            (
                {"command": "x", "exit_code": 1, "signature": "process_failure"},
                "incompatible",
            ),
            ({"command": "x", "exit_code": 1, "extra": 1}, "unknown key"),
        )
        for failure, message in cases:
            with self.subTest(failure=failure):
                spec = _minimal_spec()
                spec["failure"] = failure
                self._assert_invalid(spec, message)

    def test_process_failure_is_a_complete_oracle(self):
        spec = {
            "schema_version": 1,
            "failure": {"command": "run", "signature": "process_failure"},
        }
        self.assertIn("--process-failure", self._expand(spec))

    def test_nested_sections_must_be_objects_with_exact_keys(self):
        for section in ("execution", "sampling", "reduction", "inputs"):
            with self.subTest(section=section):
                spec = _minimal_spec()
                spec[section] = []
                self._assert_invalid(spec, "%s: expected an object" % section)
                spec[section] = None
                self._assert_invalid(spec, "%s: expected an object" % section)
                spec[section] = {"unknown": True}
                self._assert_invalid(spec, "%s: unknown key" % section)

    def test_execution_types_and_choices_are_strict(self):
        cases = (
            ({"timeout_seconds": True}, "timeout_seconds"),
            ({"timeout_seconds": 0}, "positive"),
            ({"backend": "remote"}, "execution.backend"),
            ({"jobs": 1.5}, "execution.jobs"),
            ({"jobs": 0}, "positive integer"),
            ({"cache": 1}, "execution.cache"),
            ({"timeout_seconds": 10**1000}, "finite number"),
        )
        for execution, message in cases:
            with self.subTest(execution=execution):
                spec = _minimal_spec()
                spec["execution"] = execution
                self._assert_invalid(spec, message)

    def test_docker_configuration_is_strict_and_backend_bound(self):
        cases = (
            ({"docker": {}}, "requires execution.backend=docker"),
            ({"backend": "host", "docker": {"image": "x"}}, "requires"),
            ({"backend": "docker"}, "requires execution.docker.image"),
            ({"backend": "docker", "docker": None}, "docker: expected an object"),
            ({"backend": "docker", "docker": {}}, "requires execution.docker.image"),
            (
                {"backend": "docker", "docker": {"image": "", "network": "none"}},
                "docker.image",
            ),
            (
                {"backend": "docker", "docker": {"image": "x", "network": "open"}},
                "docker.network",
            ),
            (
                {"backend": "docker", "docker": {"image": "x", "cpus": False}},
                "docker.cpus",
            ),
            (
                {"backend": "docker", "docker": {"image": "x", "pids_limit": 0}},
                "pids_limit",
            ),
            (
                {"backend": "docker", "docker": {"image": "x", "memory": "2XB"}},
                "byte-size",
            ),
            (
                {"backend": "docker", "docker": {"image": "x", "extra": 1}},
                "unknown key",
            ),
        )
        for execution, message in cases:
            with self.subTest(execution=execution):
                spec = _minimal_spec()
                spec["execution"] = execution
                self._assert_invalid(spec, message)

    def test_sampling_types_rates_and_thresholds_are_strict(self):
        cases = (
            ({"baseline_runs": True}, "baseline_runs"),
            ({"candidate_runs": 0}, "candidate_runs"),
            ({"min_baseline_rate": 0}, "min_baseline_rate"),
            ({"min_candidate_rate": 1}, "min_candidate_rate"),
            ({"confidence": "0.9"}, "confidence"),
            ({"baseline_runs": 2, "min_baseline_passes": 3}, "must not exceed"),
            ({"candidate_runs": 2, "min_candidate_passes": 3}, "must not exceed"),
            ({"run_confidence": 0.9}, "requires sampling.min_candidate_rate"),
        )
        for sampling, message in cases:
            with self.subTest(sampling=sampling):
                spec = _minimal_spec()
                spec["sampling"] = sampling
                self._assert_invalid(spec, message)

    def test_sampling_thresholds_use_cli_run_defaults(self):
        for sampling, field in (
            ({"min_baseline_passes": 3}, "min_baseline_passes"),
            ({"min_candidate_passes": 2}, "min_candidate_passes"),
        ):
            with self.subTest(field=field):
                spec = _minimal_spec()
                spec["sampling"] = sampling
                self._assert_invalid(spec, field)

    def test_all_sampling_rate_plans_must_be_statically_attainable(self):
        cases = (
            (
                {"baseline_runs": 1, "min_baseline_rate": 0.2},
                "minimum baseline rate",
            ),
            (
                {"candidate_runs": 1, "min_candidate_rate": 0.2},
                "minimum candidate rate",
            ),
            (
                {"holdout": {"runs": 1, "min_rate": 0.2}},
                "minimum holdout rate",
            ),
        )
        for sampling, message in cases:
            for command in ("reduce", "doctor"):
                with self.subTest(sampling=sampling, command=command):
                    spec = _minimal_spec()
                    spec["sampling"] = sampling
                    self._assert_invalid(spec, message, command=command)

    def test_signature_discovery_is_reserved_in_baseline_rate_plan(self):
        spec = _minimal_spec()
        spec["failure"] = {
            "command": "false",
            "match": "failure",
            "signature": "python_exception",
        }
        spec["sampling"] = {
            "baseline_runs": 1,
            "min_baseline_rate": 0.01,
        }
        self._assert_invalid(spec, "post-discovery rate-evidence runs")

    def test_holdout_requires_a_valid_pair_and_optional_confidence(self):
        cases = (
            ({}, "holdout.runs"),
            ({"runs": 2}, "holdout.min_rate"),
            ({"runs": 0, "min_rate": 0.2}, "holdout.runs"),
            ({"runs": 2, "min_rate": 1}, "holdout.min_rate"),
            ({"runs": 2, "min_rate": 0.2, "confidence": True}, "confidence"),
            ({"runs": 2, "min_rate": 0.2, "extra": 1}, "unknown key"),
        )
        for holdout, message in cases:
            with self.subTest(holdout=holdout):
                spec = _minimal_spec()
                spec["sampling"] = {"holdout": holdout}
                self._assert_invalid(spec, message)
        spec = _minimal_spec()
        spec["sampling"] = {"holdout": {"runs": 2, "min_rate": 0.2}}
        self.assertIn("--holdout-runs=2", self._expand(spec))

    def test_reduction_choices_and_limits_are_strict(self):
        cases = (
            ({"adapter": "unknown"}, "reduction.adapter"),
            ({"source_reducer": "ruby"}, "source_reducer"),
            ({"max_attempts": True}, "max_attempts"),
            ({"max_attempts": 0}, "max_attempts"),
            ({"max_duration_seconds": 0}, "max_duration_seconds"),
        )
        for reduction, message in cases:
            with self.subTest(reduction=reduction):
                spec = _minimal_spec()
                spec["reduction"] = reduction
                self._assert_invalid(spec, message)

    def test_input_lists_and_booleans_are_strict(self):
        cases = (
            ({"ignore_names": "build"}, "expected a list"),
            ({"ignore_names": [1]}, r"ignore_names\[0\]"),
            ({"ignore_names": ["a/b"]}, r"ignore_names\[0\]"),
            ({"ignore_paths": [""]}, r"ignore_paths\[0\]"),
            ({"gitignore": 1}, "inputs.gitignore"),
            ({"gitignore_recursive": "yes"}, "gitignore_recursive"),
        )
        for inputs, message in cases:
            with self.subTest(inputs=inputs):
                spec = _minimal_spec()
                spec["inputs"] = inputs
                self._assert_invalid(spec, message)

    def test_all_repository_paths_are_portable_and_relative(self):
        unsafe = (
            "/absolute/path",
            "../outside",
            "a/../outside",
            "./inside",
            "a//b",
            r"C:\outside",
            r"a\b",
            "has*glob",
            "has[glob",
            "NUL.txt",
            "trailing.",
            " space",
        )
        for field in ("ignore_paths", "keep_paths", "text_files", "gitignore_files"):
            for value in unsafe:
                with self.subTest(field=field, value=value):
                    spec = _minimal_spec()
                    spec["inputs"] = {field: [value]}
                    self._assert_invalid(spec, r"%s\[0\]" % field)

        for value in ("has*glob", "has?glob", "has[glob"):
            with self.subTest(field="ignore_names", value=value):
                spec = _minimal_spec()
                spec["inputs"] = {"ignore_names": [value]}
                self._assert_invalid(spec, r"ignore_names\[0\]")

    def test_false_boolean_switches_do_not_emit_flags(self):
        spec = _minimal_spec()
        spec["execution"] = {"cache": True}
        spec["inputs"] = {"gitignore": False, "gitignore_recursive": False}
        expanded = self._expand(spec)
        self.assertNotIn("--no-cache", expanded)
        self.assertNotIn("--gitignore", expanded)
        self.assertNotIn("--gitignore-recursive", expanded)


if __name__ == "__main__":
    unittest.main()

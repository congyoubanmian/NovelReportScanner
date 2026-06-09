import concurrent.futures
import json
import io
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import ast
from logging.handlers import RotatingFileHandler
from unittest import mock

import analysis_profiles
import general_scan
import main
import novel_scan
import novel_reviewer
import protagonist
import prompt_templates
import report
import shared_utils
import toxic_reviewer
import Timerror
import web_manager
import name_authority
import fact_validator


class ProfileAndGeneralReportTests(unittest.TestCase):
    def test_name_authority_blocks_unsafe_alias_bridges(self):
        self.assertTrue(name_authority.is_unsafe_alias("夫君"))
        self.assertTrue(name_authority.is_unsafe_alias("那女子"))
        self.assertFalse(name_authority.is_unsafe_alias("沈南歌"))

        alias_map = name_authority.build_conservative_alias_map([
            {"name": "沈南歌", "aliases": ["南歌", "夫君"], "count": 5},
            {"name": "林青竹", "aliases": ["青竹", "夫君"], "count": 4},
        ])

        self.assertEqual(alias_map["南歌"], "沈南歌")
        self.assertEqual(alias_map["青竹"], "林青竹")
        self.assertNotIn("夫君", alias_map)
        self.assertNotEqual(alias_map.get("林青竹"), "沈南歌")

    def test_fact_validator_filters_generic_harem_characters(self):
        result = fact_validator.validate_harem_character_result({
            "male_protagonist": {"name": "男主", "other_names": ["夫君"], "summary": "泛称"},
            "female_characters": [
                {"name": "那女子", "other_names": ["姑娘"], "score": 8},
                {"name": "沈南歌", "other_names": ["南歌", "夫君"], "score": 9},
            ],
        }, chunk_index=3)

        self.assertIsNone(result["male_protagonist"])
        self.assertEqual([item["name"] for item in result["female_characters"]], ["沈南歌"])
        self.assertEqual(result["female_characters"][0]["aliases"], ["南歌"])
        reasons = [item["reason"] for item in result["discarded_facts"]]
        self.assertIn("generic_person_name", reasons)
        self.assertIn("unsafe_alias", reasons)

    def test_fact_validator_classifies_gateway_timeout_as_api_error(self):
        self.assertEqual(fact_validator.classify_scan_error(RuntimeError("服务器错误(504)")), "api_error")
        self.assertEqual(fact_validator.classify_scan_error(RuntimeError("gateway timeout 504")), "api_error")
        self.assertEqual(fact_validator.classify_scan_error(RuntimeError("模型超时")), "timeout")

    def test_scan_log_handler_uses_rotation_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "analysis.log")
            handler = shared_utils.create_rotating_file_handler(log_path)
            try:
                self.assertIsInstance(handler, RotatingFileHandler)
                self.assertEqual(handler.maxBytes, shared_utils.LOG_MAX_BYTES)
                self.assertEqual(handler.backupCount, shared_utils.LOG_BACKUP_COUNT)
            finally:
                handler.close()

    def test_configure_rotating_file_logger_replaces_existing_handlers(self):
        class CloseTrackingHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.closed_by_configure = False

            def close(self):
                self.closed_by_configure = True
                super().close()

        with tempfile.TemporaryDirectory() as tmpdir:
            target_logger = logging.getLogger("test.rotating.scan.logger")
            for handler in list(target_logger.handlers):
                target_logger.removeHandler(handler)
                handler.close()
            old_handler = CloseTrackingHandler()
            target_logger.addHandler(old_handler)

            log_path = os.path.join(tmpdir, "reviewer.log")
            try:
                shared_utils.configure_rotating_file_logger(target_logger, log_path, stream=False)
                self.assertTrue(old_handler.closed_by_configure)
                self.assertEqual(len(target_logger.handlers), 1)
                self.assertIsInstance(target_logger.handlers[0], RotatingFileHandler)
                self.assertEqual(target_logger.handlers[0].baseFilename, log_path)
            finally:
                for handler in list(target_logger.handlers):
                    target_logger.removeHandler(handler)
                    handler.close()

    def test_report_logger_uses_rotating_handler(self):
        old_log_path = report.REPORT_RUN_LOG_PATH
        old_report_logger = report._REPORT_LOGGER
        target_logger = logging.getLogger("report_generation")
        old_handlers = list(target_logger.handlers)
        for handler in old_handlers:
            target_logger.removeHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                log_path = os.path.join(tmpdir, "report_generation.log")
                report.REPORT_RUN_LOG_PATH = log_path
                report._REPORT_LOGGER = None
                logger = report.get_report_logger()

                self.assertEqual(logger.name, "report_generation")
                self.assertFalse(logger.propagate)
                self.assertEqual(len(logger.handlers), 1)
                self.assertIsInstance(logger.handlers[0], RotatingFileHandler)
                self.assertEqual(logger.handlers[0].baseFilename, log_path)
        finally:
            for handler in list(target_logger.handlers):
                target_logger.removeHandler(handler)
                handler.close()
            for handler in old_handlers:
                target_logger.addHandler(handler)
            report.REPORT_RUN_LOG_PATH = old_log_path
            report._REPORT_LOGGER = old_report_logger

    def test_report_logger_initialization_failure_is_logged(self):
        old_report_logger = report._REPORT_LOGGER
        target_logger = logging.getLogger("report_generation")
        old_handlers = list(target_logger.handlers)
        for handler in old_handlers:
            target_logger.removeHandler(handler)
        try:
            report._REPORT_LOGGER = None
            with mock.patch.object(report, "configure_rotating_file_logger", side_effect=RuntimeError("boom")), \
                    self.assertLogs("report", level="WARNING") as logs:
                logger = report.get_report_logger()

            self.assertEqual(logger.name, "report_generation")
            self.assertTrue(any("初始化报告生成日志失败" in line for line in logs.output))
        finally:
            for handler in list(target_logger.handlers):
                target_logger.removeHandler(handler)
                handler.close()
            for handler in old_handlers:
                target_logger.addHandler(handler)
            report._REPORT_LOGGER = old_report_logger

    def test_report_json_call_retries_without_json_mode_on_parse_failure(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        old_chat = report.chat_completion
        try:
            def fake_chat_completion(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return FakeResponse("不是 JSON")
                return FakeResponse('{"summary":"重试成功"}')

            report.chat_completion = fake_chat_completion
            data = report._call_json_chat_completion(
                [{"role": "user", "content": "输出 JSON"}],
                max_tokens=128,
            )
        finally:
            report.chat_completion = old_chat

        self.assertEqual(data["summary"], "重试成功")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])
        self.assertIn("上一次回复不是可解析的 JSON 对象", calls[1]["messages"][-1]["content"])

    def test_shared_json_call_helper_retries_without_json_mode(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        records = []

        def fake_chat_completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return FakeResponse("不是 JSON")
            return FakeResponse('{"ok": true}')

        data = shared_utils.call_json_chat_completion_with_fallback(
            chat_completion_func=fake_chat_completion,
            model="test-model",
            messages=[{"role": "user", "content": "输出 JSON"}],
            max_tokens=128,
            record_usage_func=records.append,
        )

        self.assertTrue(data["ok"])
        self.assertEqual(len(records), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])

    def test_shared_json_call_helper_retries_when_json_mode_is_rejected(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        records = []

        def fake_chat_completion(**kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise RuntimeError("response_format unsupported")
            return FakeResponse('{"ok": true}')

        data = shared_utils.call_json_chat_completion_with_fallback(
            chat_completion_func=fake_chat_completion,
            model="test-model",
            messages=[{"role": "user", "content": "输出 JSON"}],
            max_tokens=128,
            record_usage_func=records.append,
        )

        self.assertTrue(data["ok"])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])
        self.assertIn("不支持 JSON mode", calls[1]["messages"][-1]["content"])

    def test_shared_json_call_helper_does_not_fallback_on_504(self):
        class Response:
            status_code = 504

        class GatewayTimeout(RuntimeError):
            response = Response()

        calls = []

        def fake_chat_completion(**kwargs):
            calls.append(kwargs)
            raise GatewayTimeout("gateway timeout")

        with self.assertRaises(GatewayTimeout):
            shared_utils.call_json_chat_completion_with_fallback(
                chat_completion_func=fake_chat_completion,
                model="test-model",
                messages=[{"role": "user", "content": "输出 JSON"}],
                max_tokens=128,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})

    def test_shared_json_call_helper_does_not_fallback_on_retryable_connection_error(self):
        calls = []

        def fake_chat_completion(**kwargs):
            calls.append(kwargs)
            raise ConnectionError("connection reset by peer")

        with self.assertRaises(ConnectionError):
            shared_utils.call_json_chat_completion_with_fallback(
                chat_completion_func=fake_chat_completion,
                model="test-model",
                messages=[{"role": "user", "content": "输出 JSON"}],
                max_tokens=128,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})

    def test_reviewer_json_call_retries_without_json_mode_on_parse_failure(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        old_chat = novel_reviewer.chat_completion
        try:
            def fake_chat_completion(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return FakeResponse("不是 JSON")
                return FakeResponse('{"finished":true,"reason":"重试成功"}')

            novel_reviewer.chat_completion = fake_chat_completion
            data = novel_reviewer._call_json_chat_completion(
                [{"role": "user", "content": "输出 JSON"}],
            )
        finally:
            novel_reviewer.chat_completion = old_chat

        self.assertTrue(data["finished"])
        self.assertEqual(data["reason"], "重试成功")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])
        self.assertIn("上一次回复不是可解析的 JSON 对象", calls[1]["messages"][-1]["content"])

    def test_reviewer_checkpoint_recovers_from_backup_when_primary_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "raw.json")
            checkpoint_path = os.path.join(tmpdir, "reviewer3_checkpoint.json")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write("{}")

            novel_reviewer.save_checkpoint(
                raw_path,
                [{"type": "雷点"}],
                1,
                [{"type": "误判"}],
                {0, 2},
                checkpoint_path,
                heroine_report={"甲女": {"is_clean": True}},
            )
            novel_reviewer.save_checkpoint(
                raw_path,
                [{"type": "雷点"}, {"type": "郁闷点"}],
                1,
                [{"type": "误判"}],
                {0, 1, 2},
                checkpoint_path,
                heroine_report={"甲女": {"is_clean": True}},
                purity_done=True,
            )
            self.assertTrue(os.path.exists(f"{checkpoint_path}.bak"))
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                f.write("{broken")

            loaded = novel_reviewer.load_checkpoint(raw_path, checkpoint_path)

            verified, rejected_count, rejected, processed, heroine_report, _pushed, _finished, _reason, purity_done, _finish_done = loaded
            self.assertEqual(verified, [{"type": "雷点"}, {"type": "郁闷点"}])
            self.assertEqual(rejected_count, 1)
            self.assertEqual(rejected, [{"type": "误判"}])
            self.assertEqual(processed, {0, 1, 2})
            self.assertEqual(heroine_report["甲女"]["is_clean"], True)
            self.assertTrue(purity_done)

    def test_protagonist_json_call_retries_without_json_mode_on_parse_failure(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        old_chat = protagonist.chat_completion
        try:
            def fake_chat_completion(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return FakeResponse("不是 JSON")
                return FakeResponse('{"male_protagonist":{"name":"男主"},"female_characters":[]}')

            protagonist.chat_completion = fake_chat_completion
            data = protagonist._call_json_chat_completion(
                [{"role": "user", "content": "输出 JSON"}],
                max_tokens=128,
            )
        finally:
            protagonist.chat_completion = old_chat

        self.assertEqual(data["male_protagonist"]["name"], "男主")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])
        self.assertIn("上一次回复不是可解析的 JSON 对象", calls[1]["messages"][-1]["content"])

    def test_protagonist_chunk_analysis_does_not_outer_retry_transport_error(self):
        class Response:
            status_code = 504

        class GatewayTimeout(RuntimeError):
            response = Response()

        calls = []
        old_call = protagonist._call_json_chat_completion
        try:
            def fake_call(*_args, **_kwargs):
                calls.append("call")
                raise GatewayTimeout("gateway timeout")

            protagonist._call_json_chat_completion = fake_call
            result = protagonist.analyze_chunk_for_heroines(
                "程晋阳遇见王婉柔。",
                chunk_index=0,
                total_chunks=1,
                max_retries=3,
            )
        finally:
            protagonist._call_json_chat_completion = old_call

        self.assertFalse(result["_success"])
        self.assertEqual(len(calls), 1)
        self.assertIn("API调用失败", result["_error"])

    def test_compose_variables_are_documented_in_env_sample(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        compose_path = os.path.join(base_dir, "docker-compose.yml")
        env_sample_path = os.path.join(base_dir, ".env.sample")
        with open(compose_path, "r", encoding="utf-8") as f:
            compose_text = f.read()
        with open(env_sample_path, "r", encoding="utf-8") as f:
            env_sample_text = f.read()

        compose_vars = set(re.findall(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}", compose_text))
        env_sample_vars = set()
        for line in env_sample_text.splitlines():
            match = re.match(r"\s*#?\s*([A-Z0-9_]+)=", line)
            if match:
                env_sample_vars.add(match.group(1))

        self.assertTrue(compose_vars)
        self.assertEqual(set(), compose_vars - env_sample_vars)
        self.assertIn("SCAN_STALL_TIMEOUT_SECONDS: ${SCAN_STALL_TIMEOUT_SECONDS:-1200}", compose_text)
        self.assertIn("SCAN_STALL_TIMEOUT_SECONDS=1200", env_sample_text)
        self.assertIn("HAREM_SCAN_CHUNK_SIZE: ${HAREM_SCAN_CHUNK_SIZE:-7000}", compose_text)
        self.assertIn("HAREM_SCAN_CHUNK_SIZE=7000", env_sample_text)
        self.assertIn("API_SERVER_ERROR_MAX_RETRIES: ${API_SERVER_ERROR_MAX_RETRIES:-2}", compose_text)
        self.assertIn("API_SERVER_ERROR_MAX_RETRIES=2", env_sample_text)
        self.assertIn("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS: ${API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS:-20000}", compose_text)
        self.assertIn("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS=20000", env_sample_text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH: ${HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH:-1}", compose_text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH=1", env_sample_text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS: ${HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS:-1200}", compose_text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS=1200", env_sample_text)
        self.assertIn("SCAN_FUTURE_STALL_TIMEOUT_SECONDS: ${SCAN_FUTURE_STALL_TIMEOUT_SECONDS:-0}", compose_text)
        self.assertIn("SCAN_FUTURE_STALL_TIMEOUT_SECONDS=0", env_sample_text)

    def test_setting_sample_keys_are_loaded_by_main_config(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        sample_path = os.path.join(base_dir, "setting.txt.sample")
        with open(sample_path, "r", encoding="utf-8") as f:
            sample_text = f.read()

        sample_keys = set()
        for line in sample_text.splitlines():
            key, _value = main._parse_env_line(line)
            if key:
                sample_keys.add(key.upper())

        accepted_keys = (
            set(main._DEFAULT_ENV_SETTINGS)
            | set(main._PASSTHROUGH_SETTING_KEYS)
            | set(main._VALIDATED_NON_NEGATIVE_INT_KEYS)
            | set(main._VALIDATED_NON_NEGATIVE_FLOAT_KEYS)
        )
        self.assertEqual(set(), sample_keys - accepted_keys)
        self.assertIn("WEB_ACCESS_TOKEN", main._PASSTHROUGH_SETTING_KEYS)
        self.assertIn("SCAN_STALL_TIMEOUT_SECONDS", main._VALIDATED_NON_NEGATIVE_FLOAT_KEYS)
        self.assertIn("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", main._VALIDATED_NON_NEGATIVE_FLOAT_KEYS)

    def test_docker_entrypoint_requires_web_token_and_writable_volumes(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        entrypoint_path = os.path.join(base_dir, "docker-entrypoint.sh")
        compose_path = os.path.join(base_dir, "docker-compose.yml")
        env_sample_path = os.path.join(base_dir, ".env.sample")
        with open(entrypoint_path, "r", encoding="utf-8") as f:
            entrypoint_text = f.read()
        with open(compose_path, "r", encoding="utf-8") as f:
            compose_text = f.read()
        with open(env_sample_path, "r", encoding="utf-8") as f:
            env_sample_text = f.read()

        self.assertIn("WEB_ACCESS_TOKEN must be set", entrypoint_text)
        self.assertIn("WEB_ALLOW_NO_AUTH", entrypoint_text)
        self.assertIn("check_writable_dir /app/novels", entrypoint_text)
        self.assertIn("check_writable_dir /app/results", entrypoint_text)
        self.assertIn("chown -R ${PUID:-1000}:${PGID:-1000} novels results", entrypoint_text)
        self.assertIn("WEB_ALLOW_NO_AUTH: ${WEB_ALLOW_NO_AUTH:-0}", compose_text)
        self.assertIn("WEB_ALLOW_NO_AUTH=0", env_sample_text)

    def test_dockerignore_keeps_frontend_sources_for_builder_stage(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        dockerfile_path = os.path.join(base_dir, "Dockerfile")
        dockerignore_path = os.path.join(base_dir, ".dockerignore")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            dockerfile_text = f.read()
        with open(dockerignore_path, "r", encoding="utf-8") as f:
            dockerignore_lines = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

        if "COPY frontend/ ./" in dockerfile_text:
            self.assertNotIn("frontend/src/", dockerignore_lines)
            self.assertNotIn("frontend/src", dockerignore_lines)
        self.assertNotIn("COPY . .", dockerfile_text)

    def test_docker_image_build_exposes_app_version_metadata(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        dockerfile_path = os.path.join(base_dir, "Dockerfile")
        workflow_path = os.path.join(base_dir, ".github", "workflows", "docker-publish.yml")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            dockerfile_text = f.read()
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow_text = f.read()

        for name in ("APP_VERSION", "APP_COMMIT", "APP_BUILD_DATE"):
            self.assertIn(f"ARG {name}", dockerfile_text)
            self.assertIn(f"{name}=${{{name}}}", dockerfile_text)
            self.assertIn(name, workflow_text)
        self.assertIn("github.sha", workflow_text)
        self.assertIn("date -u", workflow_text)
        self.assertIn("concurrency:", workflow_text)
        self.assertIn("group: docker-publish-${{ github.ref }}", workflow_text)
        self.assertIn("cancel-in-progress: true", workflow_text)
        self.assertIn("timeout-minutes: 45", workflow_text)
        self.assertIn("Build Docker image", workflow_text)
        self.assertIn("Push Docker image tags with retry", workflow_text)
        self.assertIn("docker buildx build", workflow_text)
        self.assertIn("--load", workflow_text)
        self.assertIn("/tmp/docker-tags.list", workflow_text)
        self.assertIn('docker push "$tag"', workflow_text)
        self.assertIn("for attempt in 1 2 3 4", workflow_text)
        self.assertIn("Docker push failed for ${tag}, retrying", workflow_text)
        self.assertIn("/tmp/docker-push-failures.list", workflow_text)
        self.assertIn("continuing with remaining tags", workflow_text)
        self.assertIn("Failed to push these image tags", workflow_text)
        self.assertIn("Report Docker Hub push", workflow_text)
        self.assertIn("Check Docker Hub credentials", workflow_text)
        self.assertIn("DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}", workflow_text)
        self.assertIn("DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}", workflow_text)
        self.assertIn("steps.dockerhub_gate.outputs.enabled == 'true'", workflow_text)
        self.assertIn('echo "enabled=true" >> "$GITHUB_OUTPUT"', workflow_text)
        self.assertNotIn("if: ${{ secrets.DOCKERHUB_USERNAME", workflow_text)
        self.assertNotIn("if: ${{ env.DOCKERHUB_USERNAME", workflow_text)
        self.assertIn("steps.meta_ghcr.outputs.tags", workflow_text)
        self.assertIn("steps.meta_dockerhub.outputs.tags", workflow_text)
        self.assertNotIn("docker/build-push-action", workflow_text)

    def test_frontend_check_script_covers_lint_format_and_build(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        package_path = os.path.join(base_dir, "frontend", "package.json")
        workflow_path = os.path.join(base_dir, ".github", "workflows", "ci.yml")
        with open(package_path, "r", encoding="utf-8") as f:
            package_data = json.load(f)
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow_text = f.read()

        scripts = package_data.get("scripts") or {}
        check_script = scripts.get("check", "")
        self.assertIn("npm run lint", check_script)
        self.assertIn("npm run format:check", check_script)
        self.assertIn("npm run build", check_script)
        self.assertIn("concurrency:", workflow_text)
        self.assertIn("group: ci-${{ github.ref }}", workflow_text)
        self.assertIn("cancel-in-progress: true", workflow_text)
        self.assertGreaterEqual(workflow_text.count("timeout-minutes: 20"), 2)
        self.assertIn("npm run check", workflow_text)

    def test_gitignore_blocks_runtime_inputs_and_keeps_templates(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        gitignore_path = os.path.join(base_dir, ".gitignore")
        with open(gitignore_path, "r", encoding="utf-8") as f:
            gitignore_lines = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

        self.assertIn("novels/", gitignore_lines)
        self.assertIn("results/", gitignore_lines)
        self.assertIn("!results/learned_keywords/", gitignore_lines)
        self.assertIn("!results/learned_keywords/seed.json", gitignore_lines)
        self.assertIn(".env", gitignore_lines)
        self.assertIn(".env.*", gitignore_lines)
        self.assertIn("!.env.sample", gitignore_lines)
        self.assertIn("api.txt", gitignore_lines)
        self.assertIn("setting.txt", gitignore_lines)
        self.assertIn("*.tar", gitignore_lines)
        self.assertIn("*.log", gitignore_lines)

    def test_git_does_not_track_runtime_inputs_or_build_artifacts(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=base_dir,
            check=True,
            text=True,
            capture_output=True,
        )
        tracked = set(proc.stdout.splitlines())

        forbidden_exact = {"api.txt", "setting.txt", ".env", "novel-report-scanner_latest.tar"}
        for path in forbidden_exact:
            self.assertNotIn(path, tracked)
        self.assertFalse(any(path.startswith("novels/") for path in tracked))
        self.assertFalse(any(path.endswith(".tar") or path.endswith(".tar.gz") for path in tracked))
        self.assertFalse(
            any(
                path.startswith("results/")
                and path not in {"results/learned_keywords/seed.json"}
                for path in tracked
            )
        )

    def test_readme_documents_public_proxy_tls_deployment(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        readme_path = os.path.join(base_dir, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            text = f.read()

        self.assertIn("公网反向代理 / TLS 建议", text)
        self.assertIn("127.0.0.1:${WEB_PORT:-8765}:8765", text)
        self.assertIn("Docker 部署默认还要求设置 `WEB_ACCESS_TOKEN`", text)
        self.assertIn("WEB_ALLOW_NO_AUTH=1", text)
        self.assertIn("容器启动时会对 `/app/novels` 和 `/app/results` 做写入自检", text)
        self.assertIn("WEB_ACCESS_TOKEN=换成一段长随机字符串", text)
        self.assertIn("WEB_ALLOW_NO_AUTH=0", text)
        self.assertIn("WEB_CORS_ALLOW_ORIGIN=https://scanner.example.com", text)
        self.assertIn("reverse_proxy 127.0.0.1:8765", text)
        self.assertIn("return 301 https://$host$request_uri", text)
        self.assertIn("proxy_buffering off", text)
        self.assertIn("proxy_read_timeout 3600s", text)
        self.assertIn("Authorization: Bearer", text)
        self.assertIn("GENERAL_SCAN_SMART_DENSITY", text)
        self.assertIn("GENERAL_SCAN_INCREMENTAL_REUSE", text)
        self.assertIn("GENERAL_SCAN_WRITING_QUALITY", text)
        self.assertIn("GENERAL_SCAN_NARRATIVE_ARCHITECTURE", text)
        self.assertIn("GENERAL_SCAN_FORESHADOWING_ENGINEERING", text)
        self.assertIn("GENERAL_SCAN_SEMANTIC_LAYERS", text)
        self.assertIn("GENERAL_SCAN_READER_EXPERIENCE", text)
        self.assertIn("GENERAL_SCAN_CONTINUITY_AUDIT", text)
        self.assertIn("GENERAL_SCAN_CONTENT_AWARE_SAMPLING", text)
        self.assertIn("GENERAL_SCAN_ROLLING_CONTEXT", text)
        self.assertIn("GENERAL_SCAN_ENTITY_PRESCAN", text)
        self.assertIn("GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE", text)
        self.assertIn("GENERAL_SCAN_CONTEXT_MAX_CHARS", text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH", text)
        self.assertIn("HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS", text)
        self.assertIn("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", text)
        self.assertIn("/api/diagnostics", text)
        self.assertIn("stale_running_count", text)
        self.assertIn("app.commit", text)
        self.assertIn("scan_stall_watchdog_enabled", text)
        self.assertIn("SCAN_STALL_TIMEOUT_SECONDS=1200", text)
        self.assertIn("/healthz.ready=false", text)
        self.assertIn("health_issues", text)
        self.assertIn("sha-xxxx", text)

    def test_profile_aliases_and_stages(self):
        harem = analysis_profiles.load_analysis_profile("后宫")
        general = analysis_profiles.load_analysis_profile("通用")
        history = analysis_profiles.load_analysis_profile("历史")
        hard_sci_fi = analysis_profiles.load_analysis_profile("硬科幻")
        xianxia_fantasy = analysis_profiles.load_analysis_profile("仙侠")
        mystery_detective = analysis_profiles.load_analysis_profile("悬疑推理")
        game_system = analysis_profiles.load_analysis_profile("无限流")
        urban_power = analysis_profiles.load_analysis_profile("都市异能")
        military_war = analysis_profiles.load_analysis_profile("军事战争")
        apocalypse_survival = analysis_profiles.load_analysis_profile("末世生存")
        cosmic_horror = analysis_profiles.load_analysis_profile("克苏鲁")
        sports_competition = analysis_profiles.load_analysis_profile("体育竞技")
        entertainment_industry = analysis_profiles.load_analysis_profile("文娱")
        business_career = analysis_profiles.load_analysis_profile("职场商战")
        crime_forensics = analysis_profiles.load_analysis_profile("刑侦法医")
        campus_youth = analysis_profiles.load_analysis_profile("校园青春")
        farming_management = analysis_profiles.load_analysis_profile("种田经营")
        isekai_lightnovel = analysis_profiles.load_analysis_profile("异世界")
        steampunk_fantasy = analysis_profiles.load_analysis_profile("蒸汽西幻")
        nation_fate = analysis_profiles.load_analysis_profile("国运")
        simulator = analysis_profiles.load_analysis_profile("人生模拟")
        chinese_weird = analysis_profiles.load_analysis_profile("中式诡异")
        mastermind_hidden = analysis_profiles.load_analysis_profile("幕后流")

        self.assertEqual(harem.name, "harem")
        self.assertTrue(harem.uses_harem_reviewer)
        self.assertFalse(harem.uses_general_scan)
        self.assertTrue(harem.supports_harem_plus)
        self.assertEqual(harem.harem_plus.get("general_profile"), "general")
        self.assertIn("romance_expectation_gap", harem.summary_fields)

        self.assertEqual(general.name, "general")
        self.assertFalse(general.uses_harem_reviewer)
        self.assertTrue(general.uses_general_scan)
        self.assertIn("character_highlights", general.summary_fields)
        self.assertIn("pacing_and_emotion", general.summary_fields)
        self.assertIn("episodic_mainline_integration", general.summary_fields)
        self.assertNotIn("unit_plot_mainline_link", general.summary_fields)

        self.assertEqual(history.name, "history")
        self.assertTrue(history.uses_general_scan)
        self.assertIn("historical_logic", history.summary_fields)
        self.assertIn("historical_atmosphere", history.summary_fields)
        self.assertIn("warfare_and_intrigue", history.summary_fields)

        self.assertEqual(hard_sci_fi.name, "hard_sci_fi")
        self.assertTrue(hard_sci_fi.uses_general_scan)
        self.assertIn("science_consistency", hard_sci_fi.summary_fields)
        self.assertIn("scale_and_wonder", hard_sci_fi.summary_fields)
        self.assertIn("social_ethical_impact", hard_sci_fi.summary_fields)

        self.assertEqual(xianxia_fantasy.name, "xianxia_fantasy")
        self.assertTrue(xianxia_fantasy.uses_general_scan)
        self.assertIn("cultivation_system", xianxia_fantasy.summary_fields)
        self.assertIn("bloodline_physique", xianxia_fantasy.summary_fields)
        self.assertIn("mythology_elements", xianxia_fantasy.summary_fields)
        self.assertIn("dao_theme", xianxia_fantasy.summary_fields)

        self.assertEqual(mystery_detective.name, "mystery_detective")
        self.assertTrue(mystery_detective.uses_general_scan)
        self.assertIn("clue_fairness", mystery_detective.summary_fields)
        self.assertIn("puzzle_fairness", mystery_detective.summary_fields)
        self.assertIn("narrative_trick", mystery_detective.summary_fields)
        self.assertIn("logic_chain_integrity", mystery_detective.summary_fields)
        self.assertIn("shortcut_detection_dependency", mystery_detective.summary_fields)
        self.assertNotIn("cheat_detection_dependency", mystery_detective.summary_fields)
        self.assertIn("reader_fit", mystery_detective.summary_fields)
        self.assertIn("overall_assessment", mystery_detective.summary_fields)
        self.assertTrue(any("叙事结构特色" in item for item in mystery_detective.scan_focus))

        self.assertEqual(game_system.name, "game_system")
        self.assertTrue(game_system.uses_general_scan)
        self.assertIn("system_rules", game_system.summary_fields)
        self.assertIn("instance_variety", game_system.summary_fields)
        self.assertIn("player_interaction", game_system.summary_fields)
        self.assertIn("novelty_mechanics", game_system.summary_fields)
        self.assertIn("real_world_impact", game_system.summary_fields)
        self.assertTrue(any("来源世界观" in item for item in game_system.scan_focus))

        self.assertEqual(urban_power.name, "urban_power")
        self.assertTrue(urban_power.uses_general_scan)
        self.assertIn("face_slapping_pacing", urban_power.summary_fields)
        self.assertIn("golden_finger_system", urban_power.summary_fields)
        self.assertNotIn("power_system", urban_power.summary_fields)
        self.assertIn("relationships", urban_power.summary_fields)
        self.assertIn("villain_quality", urban_power.summary_fields)
        self.assertGreaterEqual(len(urban_power.scan_focus), 9)
        self.assertTrue(any("金手指类型分辨" in item for item in urban_power.scan_focus))
        self.assertTrue(any("专业能力可信度" in item for item in urban_power.scan_focus))

        self.assertEqual(military_war.name, "military_war")
        self.assertTrue(military_war.uses_general_scan)
        self.assertIn("logistics_and_cost", military_war.summary_fields)
        self.assertIn("war_type_and_scale", military_war.summary_fields)
        self.assertIn("force_buildup", military_war.summary_fields)
        self.assertIn("equipment_and_tech", military_war.summary_fields)
        self.assertTrue(any("军事战斗场面是否为叙事核心" in item for item in military_war.scan_focus))

        self.assertEqual(apocalypse_survival.name, "apocalypse_survival")
        self.assertTrue(apocalypse_survival.uses_general_scan)
        self.assertIn("survival_resources", apocalypse_survival.summary_fields)
        self.assertIn("social_collapse_and_rebuild", apocalypse_survival.summary_fields)
        self.assertIn("humanity_moral_dilemmas", apocalypse_survival.summary_fields)
        self.assertIn("power_evolution_system", apocalypse_survival.summary_fields)
        self.assertTrue(any("信任危机" in item for item in apocalypse_survival.scan_focus))
        self.assertTrue(any("资源无限" in item for item in apocalypse_survival.scan_focus))
        self.assertTrue(any("势力信用背书" in item for item in apocalypse_survival.scan_focus))
        self.assertTrue(any("基地生产链缺失" in item for item in apocalypse_survival.scan_focus))

        self.assertEqual(cosmic_horror.name, "cosmic_horror")
        self.assertTrue(cosmic_horror.uses_general_scan)
        self.assertIn("anomaly_rules", cosmic_horror.summary_fields)
        self.assertIn("sequence_system", cosmic_horror.summary_fields)
        self.assertIn("san_mechanics", cosmic_horror.summary_fields)
        self.assertIn("rule_based_horror", cosmic_horror.summary_fields)
        self.assertTrue(any("知晓本身即是危险" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("'知晓的代价'" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("组织是否也是污染来源" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("收容物" in item and "失控风险" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("认知失稳" in item and "世界观崩坏" in item for item in cosmic_horror.scan_focus))

        self.assertEqual(sports_competition.name, "sports_competition")
        self.assertTrue(sports_competition.uses_general_scan)
        self.assertIn("tactical_matchups", sports_competition.summary_fields)
        self.assertIn("technique_tactics", sports_competition.summary_fields)
        self.assertIn("season_structure", sports_competition.summary_fields)
        self.assertIn("rivalry_and_opponents", sports_competition.summary_fields)
        self.assertTrue(any("规则约束下的竞技" in item for item in sports_competition.scan_focus))
        self.assertTrue(any("名场面" in item for item in sports_competition.scan_focus))
        self.assertTrue(any("训练积累" in item and "战术选择" in item for item in sports_competition.scan_focus))
        self.assertTrue(any("职业风险" in item and "职业寿命" in item for item in sports_competition.scan_focus))

        self.assertEqual(entertainment_industry.name, "entertainment_industry")
        self.assertTrue(entertainment_industry.uses_general_scan)
        self.assertIn("public_opinion", entertainment_industry.summary_fields)
        self.assertIn("creative_process", entertainment_industry.summary_fields)
        self.assertIn("fan_economy", entertainment_industry.summary_fields)

        self.assertEqual(business_career.name, "business_career")
        self.assertTrue(business_career.uses_general_scan)
        self.assertIn("business_model", business_career.summary_fields)
        self.assertIn("corporate_politics", business_career.summary_fields)
        self.assertIn("supply_chain", business_career.summary_fields)

        self.assertEqual(crime_forensics.name, "crime_forensics")
        self.assertTrue(crime_forensics.uses_general_scan)
        self.assertIn("evidence_chain", crime_forensics.summary_fields)
        self.assertIn("case_complexity", crime_forensics.summary_fields)
        self.assertIn("criminal_psychology", crime_forensics.summary_fields)
        self.assertIn("team_dynamics", crime_forensics.summary_fields)
        self.assertLessEqual(len(crime_forensics.scan_focus), 11)
        self.assertTrue(any("技术细节与专业度" in item for item in crime_forensics.scan_focus))
        self.assertTrue(any("程序违法无后果" in item for item in crime_forensics.scan_focus))
        self.assertTrue(any("非法取证" in item and "证据效力" in item for item in crime_forensics.scan_focus))
        self.assertTrue(any("法医技术是否被神化" in item and "误差边界" in item for item in crime_forensics.scan_focus))

        self.assertEqual(campus_youth.name, "campus_youth")
        self.assertTrue(campus_youth.uses_general_scan)
        self.assertIn("coming_of_age", campus_youth.summary_fields)
        self.assertIn("era_atmosphere", campus_youth.summary_fields)
        self.assertIn("family_dynamics", campus_youth.summary_fields)

        self.assertEqual(farming_management.name, "farming_management")
        self.assertTrue(farming_management.uses_general_scan)
        self.assertIn("production_chain", farming_management.summary_fields)
        self.assertIn("technology_progression", farming_management.summary_fields)
        self.assertIn("civilization_level", farming_management.summary_fields)
        self.assertIn("population_management", farming_management.summary_fields)
        self.assertTrue(any("升级路径" in item for item in farming_management.scan_focus))
        self.assertTrue(any("外部威胁与内部发展" in item for item in farming_management.scan_focus))

        self.assertEqual(isekai_lightnovel.name, "isekai_lightnovel")
        self.assertTrue(isekai_lightnovel.uses_general_scan)
        self.assertIn("isekai_premise", isekai_lightnovel.summary_fields)
        self.assertIn("races_culture", isekai_lightnovel.summary_fields)
        self.assertIn("politics_society", isekai_lightnovel.summary_fields)
        self.assertIn("romance_comedy_balance", isekai_lightnovel.summary_fields)
        self.assertIn("slice_of_life", isekai_lightnovel.summary_fields)
        self.assertTrue(any("常见硬伤" in item for item in isekai_lightnovel.scan_focus))

        self.assertEqual(steampunk_fantasy.name, "steampunk_fantasy")
        self.assertTrue(steampunk_fantasy.uses_general_scan)
        mystery_detective = analysis_profiles.load_analysis_profile("mystery_detective")
        self.assertTrue(any("外挂硬解" in item and "可复核线索推理" in item for item in mystery_detective.scan_focus))
        self.assertTrue(any("案件彼此割裂" in item for item in mystery_detective.scan_focus))
        self.assertIn("tech_feasibility", steampunk_fantasy.summary_fields)
        self.assertIn("episodic_mainline_integration", steampunk_fantasy.summary_fields)
        self.assertNotIn("unit_plot_mainline_link", steampunk_fantasy.summary_fields)
        self.assertGreaterEqual(len(steampunk_fantasy.scan_focus), 8)
        self.assertTrue(any("社会阶层体系" in item for item in steampunk_fantasy.scan_focus))
        self.assertTrue(any("炼金工业的经济逻辑" in item for item in steampunk_fantasy.scan_focus))
        self.assertTrue(any("技术-魔法的平衡" in item for item in steampunk_fantasy.scan_focus))

        self.assertEqual(nation_fate.name, "nation_fate")
        self.assertTrue(nation_fate.uses_general_scan)
        self.assertIn("nation_fate_mechanics", nation_fate.summary_fields)
        self.assertTrue(any("文明选拔规则" in item for item in nation_fate.scan_focus))

        self.assertEqual(simulator.name, "simulator")
        self.assertTrue(simulator.uses_general_scan)
        self.assertIn("simulation_reality_loop", simulator.summary_fields)
        self.assertTrue(any("推演-验证循环" in item for item in simulator.scan_focus))

        self.assertEqual(chinese_weird.name, "chinese_weird")
        self.assertTrue(chinese_weird.uses_general_scan)
        self.assertIn("weird_rules", chinese_weird.summary_fields)
        self.assertIn("folk_taboo_system", chinese_weird.summary_fields)
        self.assertTrue(any("中式民俗底盘" in item for item in chinese_weird.scan_focus))
        self.assertTrue(any("现实侵蚀" in item for item in chinese_weird.scan_focus))

        self.assertEqual(mastermind_hidden.name, "mastermind_hidden")
        self.assertTrue(mastermind_hidden.uses_general_scan)
        self.assertIn("alias_system", mastermind_hidden.summary_fields)
        self.assertIn("information_asymmetry", mastermind_hidden.summary_fields)
        self.assertIn("mastermind_schemes", mastermind_hidden.summary_fields)
        self.assertTrue(any("马甲体系设计" in item for item in mastermind_hidden.scan_focus))
        self.assertTrue(any("掉马与揭秘爽点" in item for item in mastermind_hidden.scan_focus))

        self.assertGreaterEqual(len(harem.scan_focus), 9)
        self.assertTrue(any("女主定位分级" in item for item in harem.scan_focus))
        self.assertTrue(any("接触等级评估" in item for item in harem.scan_focus))
        self.assertTrue(any("五维洁度评估" in item for item in harem.scan_focus))

        for profile in [
            general,
            history,
            hard_sci_fi,
            xianxia_fantasy,
            mystery_detective,
            game_system,
            urban_power,
            military_war,
            apocalypse_survival,
            cosmic_horror,
            sports_competition,
            entertainment_industry,
            business_career,
            crime_forensics,
            campus_youth,
            farming_management,
            isekai_lightnovel,
            steampunk_fantasy,
            nation_fate,
            simulator,
            chinese_weird,
            mastermind_hidden,
        ]:
            self.assertIn("reader_fit", profile.summary_fields, profile.name)
            self.assertIn("overall_assessment", profile.summary_fields, profile.name)

        self.assertEqual(analysis_profiles.resolve_profile_name("自动"), "auto")

    def test_general_profile_fallback_keeps_general_scan_stage(self):
        old_loader = analysis_profiles._load_profile_manifest
        try:
            analysis_profiles._load_profile_manifest = lambda profile_name: {}
            general = analysis_profiles.load_analysis_profile("general")
        finally:
            analysis_profiles._load_profile_manifest = old_loader

        self.assertEqual(general.name, "general")
        self.assertTrue(general.uses_general_scan)
        self.assertIn("general_scan", general.enabled_stages)

    def test_profile_options_are_discovered(self):
        options = analysis_profiles.profile_options(include_auto=True)
        names = [item["name"] for item in options]

        self.assertEqual(names[0], "auto")
        self.assertIn("harem", names)
        self.assertIn("general", names)
        self.assertIn("history", names)
        self.assertIn("hard_sci_fi", names)
        self.assertIn("xianxia_fantasy", names)
        self.assertIn("mystery_detective", names)
        self.assertIn("game_system", names)
        self.assertIn("urban_power", names)
        self.assertIn("military_war", names)
        self.assertIn("apocalypse_survival", names)
        self.assertIn("cosmic_horror", names)
        self.assertIn("sports_competition", names)
        self.assertIn("entertainment_industry", names)
        self.assertIn("business_career", names)
        self.assertIn("crime_forensics", names)
        self.assertIn("campus_youth", names)
        self.assertIn("farming_management", names)
        self.assertIn("isekai_lightnovel", names)
        self.assertIn("steampunk_fantasy", names)
        self.assertIn("nation_fate", names)
        self.assertIn("simulator", names)
        self.assertIn("chinese_weird", names)
        self.assertIn("mastermind_hidden", names)

    def test_profile_manifests_name_matches_directory(self):
        profiles_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
        for profile in analysis_profiles.list_available_profiles():
            manifest_path = os.path.join(profiles_root, profile.name, "profile.json")
            with self.subTest(profile=profile.name):
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                self.assertEqual(manifest.get("name"), profile.name)
                self.assertIsInstance(manifest.get("sort_order"), int)
                self.assertEqual(manifest.get("sort_order"), profile.sort_order)

    def test_profile_manifests_include_version_metadata(self):
        profiles_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
        for profile in analysis_profiles.list_available_profiles():
            manifest_path = os.path.join(profiles_root, profile.name, "profile.json")
            with self.subTest(profile=profile.name):
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                self.assertRegex(manifest.get("version", ""), r"^\d+\.\d+\.\d+$")
                self.assertEqual(profile.version, manifest["version"])
                self.assertIsInstance(manifest.get("version_history"), list)
                self.assertGreaterEqual(len(manifest["version_history"]), 1)
                self.assertEqual(profile.version_history, manifest["version_history"])
                self.assertRegex(manifest.get("min_supported_scanner_version", ""), r"^\d+\.\d+\.\d+$")
                self.assertEqual(profile.min_supported_scanner_version, manifest["min_supported_scanner_version"])
                self.assertIsInstance(manifest.get("breaking_changes"), bool)
                self.assertEqual(profile.breaking_changes, manifest["breaking_changes"])

    def test_profile_order_is_manifest_owned(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base_dir, "analysis_profiles.py"), "r", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("_PROFILE_ORDER", source)

        profiles = analysis_profiles.list_available_profiles()
        orders = [profile.sort_order for profile in profiles]
        names = [profile.name for profile in profiles]

        self.assertEqual(orders, sorted(orders))
        self.assertEqual(names[:3], ["harem", "general", "history"])

    def test_profile_options_expose_version_metadata(self):
        options = analysis_profiles.profile_options(include_auto=True)
        harem_option = next(item for item in options if item["name"] == "harem")
        auto_option = next(item for item in options if item["name"] == "auto")

        self.assertNotIn("version", auto_option)
        self.assertRegex(harem_option["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(harem_option["min_supported_scanner_version"], r"^\d+\.\d+\.\d+$")
        self.assertFalse(harem_option["breaking_changes"])

    def test_prompt_template_versions_are_registered_and_overridable(self):
        self.assertEqual(
            prompt_templates.prompt_template_metadata("general_scan_chunk")["version"],
            "v1",
        )
        with mock.patch.dict(
            os.environ,
            {"PROMPT_TEMPLATE_GENERAL_SCAN_CHUNK_VERSION": "v2"},
            clear=False,
        ):
            self.assertEqual(
                prompt_templates.prompt_template_metadata("general_scan_chunk")["version"],
                "v2",
            )

        metadata = prompt_templates.prompt_templates_metadata(
            "harem_scan_chunk",
            "general_scan_chunk",
            "general_summary",
        )
        self.assertEqual(sorted(metadata), ["general_scan_chunk", "general_summary", "harem_scan_chunk"])

    def test_api_client_factory_is_shared(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        shared_path = os.path.join(base_dir, "shared_utils.py")
        with open(shared_path, "r", encoding="utf-8") as f:
            shared_text = f.read()

        self.assertIn("def _openai_client_factory", shared_text)
        self.assertIn("def create_chat_completion", shared_text)

        for filename in ["novel_scan.py", "protagonist.py", "report.py"]:
            with self.subTest(module=filename):
                with open(os.path.join(base_dir, filename), "r", encoding="utf-8") as f:
                    text = f.read()
                tree = ast.parse(text)
                shared_import_names = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "shared_utils"
                    for alias in node.names
                }
                self.assertNotIn("def _openai_client_factory", text)
                self.assertNotIn("from Timerror import make_chat_completion", text)
                self.assertIn("create_chat_completion", shared_import_names)
                self.assertIn('BASE_URL = os.environ.get("BASE_URL", "https://api.deepseek.com")', text)

    def test_api_retry_defaults_are_shared_across_scan_stages(self):
        self.assertEqual(shared_utils.DEFAULT_MAX_RETRIES, 5)
        self.assertEqual(shared_utils.DEFAULT_MAX_403_RETRIES, 3)
        self.assertEqual(shared_utils.DEFAULT_MAX_TIMEOUT_RETRIES, 3)
        self.assertEqual(shared_utils.DEFAULT_MAX_SERVER_ERROR_RETRIES, 2)
        self.assertEqual(shared_utils.DEFAULT_SERVER_ERROR_FAST_FAIL_INPUT_CHARS, 20000)
        self.assertEqual(shared_utils.DEFAULT_REQUEST_TIMEOUT, 120)

        for module in [novel_scan, protagonist, report]:
            with self.subTest(module=module.__name__):
                self.assertEqual(module.MAX_RETRIES, shared_utils.DEFAULT_MAX_RETRIES)
                self.assertEqual(module.MAX_403_RETRIES, shared_utils.DEFAULT_MAX_403_RETRIES)
                self.assertEqual(module.MAX_TIMEOUT_RETRIES, shared_utils.DEFAULT_MAX_TIMEOUT_RETRIES)
                self.assertEqual(module.MAX_SERVER_ERROR_RETRIES, shared_utils.DEFAULT_MAX_SERVER_ERROR_RETRIES)
                self.assertEqual(module.REQUEST_TIMEOUT, shared_utils.DEFAULT_REQUEST_TIMEOUT)

    def test_future_stall_helper_is_shared_by_scan_stages(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base_dir, "protagonist.py"), "r", encoding="utf-8") as f:
            protagonist_text = f.read()
        with open(os.path.join(base_dir, "novel_scan.py"), "r", encoding="utf-8") as f:
            novel_scan_text = f.read()

        self.assertNotIn("concurrent.futures.as_completed(", protagonist_text)
        self.assertIn("iter_completed_futures", protagonist_text)
        self.assertIn("cancel_pending_futures", protagonist_text)
        self.assertIn("iter_completed_futures", novel_scan_text)
        self.assertIn("cancel_pending_futures", novel_scan_text)

    def test_new_runtime_int_envs_fallback_when_invalid(self):
        self.assertEqual(shared_utils.read_int_env("__MISSING_INT_ENV__", 7, min_value=1), 7)

        old_values = {
            "API_SERVER_ERROR_MAX_RETRIES": os.environ.get("API_SERVER_ERROR_MAX_RETRIES"),
            "API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS": os.environ.get("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS"),
            "HAREM_SCAN_CHUNK_SIZE": os.environ.get("HAREM_SCAN_CHUNK_SIZE"),
        }
        try:
            os.environ["API_SERVER_ERROR_MAX_RETRIES"] = ""
            os.environ["API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS"] = "bad"
            os.environ["HAREM_SCAN_CHUNK_SIZE"] = "bad"
            self.assertEqual(shared_utils.read_int_env("API_SERVER_ERROR_MAX_RETRIES", 2, min_value=1), 2)
            self.assertEqual(shared_utils.read_int_env("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS", 20000, min_value=0), 20000)
            self.assertEqual(shared_utils.read_int_env("HAREM_SCAN_CHUNK_SIZE", 7000, min_value=1000), 7000)

            chat_completion = Timerror.make_chat_completion(
                openai_client_factory=lambda *_args: None,
                api_key_pool=["sk-test"],
                base_url="https://example.test/v1",
                request_timeout="bad",
                max_retries="bad",
                max_server_error_retries="",
                base_delay=0,
                rpm_limit=0,
                tpm_limit=0,
                logger=None,
            )
            self.assertTrue(callable(chat_completion))
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_scan_modules_tolerate_invalid_numeric_envs_on_import(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        env = os.environ.copy()
        env.update({
            "API_KEY": "sk-test",
            "MAX_WORKERS": "bad",
            "CHUNK_OVERLAP": "bad",
            "FACT_BOOST_MAX_CALLS_PER_CHUNK": "bad",
            "DIM_BOOST_MAX_PER_CHUNK": "bad",
            "RESCAN_ROUNDS": "bad",
            "RESCAN_MAX_WORKERS": "bad",
            "MAX_MIDDLE_SUMMARY_CALLS": "bad",
            "INITIAL_SCAN_BLOCK_MULTIPLIER": "bad",
            "INITIAL_SCAN_MIN_BLOCK_SIZE": "bad",
            "RESCAN_MAX_HITS": "bad",
            "RESCAN_PRE_FILTER_THRESHOLD": "bad",
            "RESCAN_MAX_WINDOW": "bad",
            "RESCAN_MAX_PROMPT_HEROINES": "bad",
            "RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER": "bad",
            "HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH": "bad",
            "HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS": "bad",
            "GENERAL_SCAN_CHUNK_SIZE": "bad",
            "GENERAL_SCAN_CHUNK_OVERLAP": "bad",
            "GENERAL_SCAN_MAX_CHUNKS": "bad",
            "GENERAL_SCAN_CONTEXT_MAX_CHARS": "bad",
            "ALIAS_CROSS_MERGE_MAX_PAYLOAD_CHARS": "bad",
            "ALIAS_CROSS_MERGE_MAX_LIST_ITEMS": "bad",
            "ALIAS_CROSS_MERGE_MAX_FIELD_CHARS": "bad",
        })
        code = (
            "import general_scan, novel_scan, protagonist, report\n"
            "assert general_scan.CHUNK_SIZE == 12000\n"
            "assert general_scan.CHUNK_OVERLAP == 1000\n"
            "assert general_scan.MAX_CHUNKS == 80\n"
            "assert general_scan.CONTEXT_MAX_CHARS == 1600\n"
            "assert novel_scan.MAX_WORKERS == 6\n"
            "assert novel_scan.RESCAN_PRE_FILTER_THRESHOLD == 1.0\n"
            "assert protagonist.ALIAS_CROSS_MERGE_MAX_LIST_ITEMS == 3\n"
            "assert report.MAX_WORKERS == 4\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=base_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_frontend_runtime_config_covers_editable_backend_fields(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        app_path = os.path.join(base_dir, "frontend", "src", "App.vue")
        with open(app_path, "r", encoding="utf-8") as f:
            text = f.read()
        for field in web_manager.EDITABLE_RUNTIME_CONFIG:
            with self.subTest(field=field):
                self.assertIn(f"{field}:", text)
                self.assertIn(f"configForm.value.{field}", text)
        self.assertIn("rate_limit_scope: 'auto'", text)
        self.assertIn("config.rate_limit_scope || 'auto'", text)
        self.assertIn('<option value="auto">auto</option>', text)
        self.assertIn("api_server_error_max_retries: '2'", text)
        self.assertIn("api_server_error_fast_fail_input_chars: '20000'", text)
        self.assertIn("config.api_server_error_fast_fail_input_chars || '20000'", text)
        self.assertIn("configForm.api_server_error_fast_fail_input_chars", text)
        self.assertIn("<span>5xx快败</span>", text)
        self.assertIn("harem_scan_chunk_size: '7000'", text)
        self.assertIn("configForm.harem_scan_chunk_size", text)
        self.assertIn("general_scan_writing_quality: true", text)
        self.assertIn("config.general_scan_writing_quality !== false", text)
        self.assertIn("configForm.general_scan_writing_quality", text)
        self.assertIn("general_scan_narrative_architecture: true", text)
        self.assertIn("config.general_scan_narrative_architecture !== false", text)
        self.assertIn("configForm.general_scan_narrative_architecture", text)
        self.assertIn("general_scan_foreshadowing_engineering: true", text)
        self.assertIn("config.general_scan_foreshadowing_engineering !== false", text)
        self.assertIn("configForm.general_scan_foreshadowing_engineering", text)
        self.assertIn("general_scan_semantic_layers: true", text)
        self.assertIn("config.general_scan_semantic_layers !== false", text)
        self.assertIn("configForm.general_scan_semantic_layers", text)
        self.assertIn("general_scan_reader_experience: true", text)
        self.assertIn("config.general_scan_reader_experience !== false", text)
        self.assertIn("configForm.general_scan_reader_experience", text)
        self.assertIn("general_scan_continuity_audit: true", text)
        self.assertIn("config.general_scan_continuity_audit !== false", text)
        self.assertIn("configForm.general_scan_continuity_audit", text)
        self.assertIn("general_scan_entity_prescan: true", text)
        self.assertIn("config.general_scan_entity_prescan !== false", text)
        self.assertIn("configForm.general_scan_entity_prescan", text)
        self.assertIn("general_scan_knowledge_base_llm_merge: false", text)
        self.assertIn("Boolean(config.general_scan_knowledge_base_llm_merge)", text)
        self.assertIn("configForm.general_scan_knowledge_base_llm_merge", text)
        self.assertIn("general_scan_content_aware_sampling: true", text)
        self.assertIn("config.general_scan_content_aware_sampling !== false", text)
        self.assertIn("configForm.general_scan_content_aware_sampling", text)
        self.assertIn("data.health_issues", text)
        self.assertIn("storage: '存储异常'", text)
        self.assertIn("config: '配置异常'", text)
        self.assertIn("item.message || item.type || 'health issue'", text)
        book_list_path = os.path.join(base_dir, "frontend", "src", "components", "BookList.vue")
        with open(book_list_path, "r", encoding="utf-8") as f:
            book_list_text = f.read()
        self.assertIn("autoSelected: Boolean(s.auto_selected)", book_list_text)
        self.assertIn("Top{{ s.rank || i + 1 }}", book_list_text)
        self.assertIn("Math.round(s.confidence * 100)", book_list_text)
        book_detail_path = os.path.join(base_dir, "frontend", "src", "components", "BookDetail.vue")
        with open(book_detail_path, "r", encoding="utf-8") as f:
            book_detail_text = f.read()
        self.assertIn("autoSelected: Boolean(s.auto_selected)", book_detail_text)
        self.assertIn("Top{{ s.rank || i + 1 }}", book_detail_text)

    def test_frontend_api_formats_operation_result_errors(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        api_path = os.path.join(base_dir, "frontend", "src", "api.js")
        with open(api_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("typeof data.result === 'string'", text)
        self.assertIn("data.error || resultText", text)
        self.assertIn("<title[^>]*>", text)
        self.assertIn("HTTP ${status}: ${cleanTitle}", text)
        self.assertIn("plainText.slice(0, 300)", text)

    def test_frontend_sse_fallback_retries_after_parse_errors(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        composable_path = os.path.join(base_dir, "frontend", "src", "composables", "useStateEvents.js")
        with open(composable_path, "r", encoding="utf-8") as f:
            text = f.read()

        self.assertIn("function scheduleRetry()", text)
        self.assertIn("if (retryTimer) return", text)
        self.assertIn("retryTimer = null", text)
        self.assertIn("scheduleRetry()", text)
        self.assertIn("onState(JSON.parse(event.data))", text)
        self.assertIn("catch {\n        closeSource()\n        onFallback()\n        scheduleRetry()\n      }", text)

    def test_rate_limit_scope_auto_resolves_by_key_count(self):
        self.assertEqual(Timerror.normalize_rate_limit_scope("auto", 1), "global")
        self.assertEqual(Timerror.normalize_rate_limit_scope("auto", 2), "per_key")
        self.assertEqual(Timerror.normalize_rate_limit_scope("global", 2), "global")
        self.assertEqual(Timerror.normalize_rate_limit_scope("per_key", 1), "per_key")
        self.assertEqual(Timerror.normalize_rate_limit_scope("bad", 2), "global")

        per_key = Timerror.RateLimiter(rpm_limit=1, tpm_limit=0, scope="per_key")
        self.assertEqual(per_key.acquire_slot("key-a", 1), (0.0, "ok"))
        self.assertEqual(per_key.acquire_slot("key-b", 1), (0.0, "ok"))
        self.assertGreater(per_key.acquire_slot("key-a", 1)[0], 0)

        global_scope = Timerror.RateLimiter(rpm_limit=1, tpm_limit=0, scope="global")
        self.assertEqual(global_scope.acquire_slot("key-a", 1), (0.0, "ok"))
        self.assertGreater(global_scope.acquire_slot("key-b", 1)[0], 0)

    def test_connection_reset_is_retryable_but_not_timeout(self):
        timeout_error = TimeoutError("request timed out")
        reset_error = ConnectionError("connection reset by peer")
        connect_timeout = RuntimeError("ConnectTimeout while dialing provider")

        self.assertTrue(Timerror.is_timeout_error(timeout_error))
        self.assertTrue(Timerror.is_timeout_error(connect_timeout))
        self.assertFalse(Timerror.is_timeout_error(reset_error))
        self.assertTrue(Timerror.is_retryable_connection_error(reset_error))
        self.assertTrue(shared_utils.is_retryable_transport_error(reset_error))

    def test_make_chat_completion_rate_limit_scope_precedence(self):
        old_scope = os.environ.get("RATE_LIMIT_SCOPE")
        try:
            os.environ["RATE_LIMIT_SCOPE"] = "per_key"
            from_env = Timerror.make_chat_completion(
                openai_client_factory=lambda *_args: None,
                api_key_pool=["key-a", "key-b"],
                base_url="https://example.test",
                rpm_limit=0,
                tpm_limit=0,
            )
            self.assertEqual(from_env._configured_rate_limit_scope, "per_key")
            self.assertEqual(from_env._rate_limit_scope, "per_key")

            explicit = Timerror.make_chat_completion(
                openai_client_factory=lambda *_args: None,
                api_key_pool=["key-a", "key-b"],
                base_url="https://example.test",
                rpm_limit=0,
                tpm_limit=0,
                rate_limit_scope="global",
            )
            self.assertEqual(explicit._configured_rate_limit_scope, "global")
            self.assertEqual(explicit._rate_limit_scope, "global")

            os.environ.pop("RATE_LIMIT_SCOPE", None)
            default_auto = Timerror.make_chat_completion(
                openai_client_factory=lambda *_args: None,
                api_key_pool=["key-a", "key-b"],
                base_url="https://example.test",
                rpm_limit=0,
                tpm_limit=0,
            )
            self.assertEqual(default_auto._configured_rate_limit_scope, "auto")
            self.assertEqual(default_auto._rate_limit_scope, "per_key")
        finally:
            if old_scope is None:
                os.environ.pop("RATE_LIMIT_SCOPE", None)
            else:
                os.environ["RATE_LIMIT_SCOPE"] = old_scope

    def test_make_chat_completion_stops_after_5xx_retry_limit(self):
        calls = []

        class Response:
            status_code = 504

        class ServerError(RuntimeError):
            response = Response()

        class FakeCompletions:
            def create(self, **_kwargs):
                calls.append("call")
                raise ServerError("gateway timeout")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        def fake_factory(_key, _base_url, _timeout):
            return FakeClient()

        chat_completion = Timerror.make_chat_completion(
            openai_client_factory=fake_factory,
            api_key_pool=["sk-test"],
            base_url="https://example.test/v1",
            request_timeout=1,
            max_retries=2,
            base_delay=0,
            rpm_limit=0,
            tpm_limit=0,
            logger=None,
        )

        with self.assertRaises(ServerError):
            chat_completion(messages=[{"role": "user", "content": "hello"}], max_tokens=1)
        self.assertEqual(len(calls), 2)

    def test_make_chat_completion_uses_short_5xx_retry_budget(self):
        calls = []

        class Response:
            status_code = 504

        class ServerError(RuntimeError):
            response = Response()

        class FakeCompletions:
            def create(self, **_kwargs):
                calls.append("call")
                raise ServerError("gateway timeout")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        chat_completion = Timerror.make_chat_completion(
            openai_client_factory=lambda _key, _base_url, _timeout: FakeClient(),
            api_key_pool=["sk-test"],
            base_url="https://example.test/v1",
            request_timeout=1,
            max_retries=5,
            max_server_error_retries=2,
            base_delay=0,
            rpm_limit=0,
            tpm_limit=0,
            logger=None,
        )

        with self.assertRaises(ServerError):
            chat_completion(messages=[{"role": "user", "content": "hello"}], max_tokens=1)
        self.assertEqual(len(calls), 2)

    def test_make_chat_completion_fast_fails_large_5xx_request(self):
        calls = []

        class Response:
            status_code = 504

        class ServerError(RuntimeError):
            response = Response()

        class FakeCompletions:
            def create(self, **_kwargs):
                calls.append("call")
                raise ServerError("gateway timeout")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        chat_completion = Timerror.make_chat_completion(
            openai_client_factory=lambda _key, _base_url, _timeout: FakeClient(),
            api_key_pool=["sk-test"],
            base_url="https://example.test/v1",
            request_timeout=1,
            max_retries=5,
            max_server_error_retries=3,
            max_server_error_fast_fail_input_chars=10,
            base_delay=0,
            rpm_limit=0,
            tpm_limit=0,
            logger=None,
        )

        with self.assertRaises(ServerError):
            chat_completion(messages=[{"role": "user", "content": "甲" * 10}], max_tokens=1)
        self.assertEqual(len(calls), 1)

    def test_make_chat_completion_keeps_5xx_retry_budget_for_small_request(self):
        calls = []

        class Response:
            status_code = 504

        class ServerError(RuntimeError):
            response = Response()

        class FakeCompletions:
            def create(self, **_kwargs):
                calls.append("call")
                raise ServerError("gateway timeout")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        chat_completion = Timerror.make_chat_completion(
            openai_client_factory=lambda _key, _base_url, _timeout: FakeClient(),
            api_key_pool=["sk-test"],
            base_url="https://example.test/v1",
            request_timeout=1,
            max_retries=5,
            max_server_error_retries=3,
            max_server_error_fast_fail_input_chars=100,
            base_delay=0,
            rpm_limit=0,
            tpm_limit=0,
            logger=None,
        )

        with self.assertRaises(ServerError):
            chat_completion(messages=[{"role": "user", "content": "short"}], max_tokens=1)
        self.assertEqual(len(calls), 3)

    def test_make_chat_completion_connection_reset_does_not_use_timeout_disable_path(self):
        calls = []
        logs = []

        class FakeCompletions:
            def create(self, **_kwargs):
                calls.append("call")
                raise ConnectionError("connection reset by peer")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class ListLogger:
            def info(self, message):
                logs.append(str(message))

            def warning(self, message):
                logs.append(str(message))

            def error(self, message):
                logs.append(str(message))

        chat_completion = Timerror.make_chat_completion(
            openai_client_factory=lambda _key, _base_url, _timeout: FakeClient(),
            api_key_pool=["sk-test"],
            base_url="https://example.test/v1",
            request_timeout=1,
            max_retries=2,
            max_timeout_retries=1,
            base_delay=0,
            rpm_limit=0,
            tpm_limit=0,
            logger=ListLogger(),
        )

        with self.assertRaises(ConnectionError):
            chat_completion(messages=[{"role": "user", "content": "hello"}], max_tokens=1)

        self.assertEqual(len(calls), 2)
        joined_logs = "\n".join(logs)
        self.assertIn("未知错误", joined_logs)
        self.assertNotIn("超时累计", joined_logs)
        self.assertNotIn("软禁用", joined_logs)

    def test_auto_inference_keywords_are_profile_owned(self):
        for profile in analysis_profiles.list_available_profiles():
            if profile.name == "general":
                continue
            with self.subTest(profile=profile.name):
                keywords = analysis_profiles._keywords_from_manifest(profile.name)
                self.assertTrue(keywords, f"{profile.name} missing inference_keywords")

        urban_keywords = dict(analysis_profiles._keywords_from_manifest("urban_power"))
        self.assertEqual(urban_keywords["系统"], 3)
        self.assertEqual(urban_keywords["修仙"], 2)
        self.assertEqual(urban_keywords["下山"], 5)
        self.assertEqual(urban_keywords["龙王"], 5)
        self.assertEqual(urban_keywords["战神"], 5)
        self.assertEqual(urban_keywords["校花"], 3)
        self.assertEqual(urban_keywords["家族"], 3)
        self.assertEqual(urban_keywords["集团"], 3)
        self.assertEqual(urban_keywords["商战"], 3)

        hard_sci_fi_keywords = dict(analysis_profiles._keywords_from_manifest("hard_sci_fi"))
        self.assertEqual(hard_sci_fi_keywords["AI"], 3)
        self.assertLessEqual(
            abs(hard_sci_fi_keywords["强人工智能"] - hard_sci_fi_keywords["AI"]),
            2,
        )

        history_keywords = dict(analysis_profiles._keywords_from_manifest("history"))
        self.assertEqual(history_keywords["边军"], 2)
        self.assertEqual(history_keywords["骑兵"], 2)

        isekai_keywords = dict(analysis_profiles._keywords_from_manifest("isekai_lightnovel"))
        self.assertEqual(isekai_keywords["技能"], 4)

        game_keywords = dict(analysis_profiles._keywords_from_manifest("game_system"))
        self.assertEqual(game_keywords["游戏"], 3)

    def test_history_and_sci_fi_rules_include_kimi_categories(self):
        with open(os.path.join("profiles", "history", "rules.json"), "r", encoding="utf-8") as f:
            history_rules = json.load(f)
        with open(os.path.join("profiles", "hard_sci_fi", "rules.json"), "r", encoding="utf-8") as f:
            sci_fi_rules = json.load(f)

        history_categories = {item["name"]: item for item in history_rules["categories"]}
        self.assertIn("制度与时代", history_categories)
        self.assertIn("权谋与战争", history_categories)
        self.assertIn("历史氛围与人物", history_categories)
        history_points = {
            point["name"]
            for category in history_rules["categories"]
            for point in category.get("points", [])
        }
        self.assertGreaterEqual(len(history_points), 12)
        self.assertIn("穿越合理性", history_points)
        self.assertIn("穿越知识边界", history_points)
        self.assertIn("战争决策", history_points)
        self.assertIn("礼法与感情线", history_points)
        self.assertIn("时代语言", history_points)
        self.assertIn("蝴蝶效应代价", history_points)

        sci_fi_categories = {item["name"]: item for item in sci_fi_rules["categories"]}
        self.assertIn("科学设定与技术链", sci_fi_categories)
        self.assertIn("设定自洽与世界观", sci_fi_categories)
        self.assertIn("科幻概念与硬伤", sci_fi_categories)
        sci_fi_points = {
            point["name"]
            for category in sci_fi_rules["categories"]
            for point in category.get("points", [])
        }
        self.assertGreaterEqual(len(sci_fi_points), 12)
        self.assertIn("技术链完整性", sci_fi_points)
        self.assertIn("阅读门槛", sci_fi_points)
        self.assertIn("社会伦理", sci_fi_points)
        self.assertIn("软硬混合度", sci_fi_points)
        self.assertIn("常见硬伤", sci_fi_points)
        self.assertIn("工程代价", sci_fi_points)

        history_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("history"))
        sci_fi_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("hard_sci_fi"))
        self.assertIn("穿越知识边界", history_text)
        self.assertIn("软硬混合度", sci_fi_text)

    def test_steampunk_rules_include_kimi_audit_dimensions(self):
        with open(os.path.join("profiles", "steampunk_fantasy", "rules.json"), "r", encoding="utf-8") as f:
            rules = json.load(f)

        categories = {item["name"]: item for item in rules["categories"]}
        self.assertIn("蒸汽西幻底盘", categories)
        self.assertIn("炼金工业", categories)
        self.assertIn("技术跃迁风险", categories)

        points = {
            point["name"]
            for category in rules["categories"]
            for point in category.get("points", [])
        }
        self.assertGreaterEqual(len(points), 12)
        for point_name in [
            "西幻政治结构",
            "教会帝国博弈",
            "社会阶层",
            "炼金经济",
            "技术扩散",
            "边界约束",
        ]:
            self.assertIn(point_name, points)

        rules_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("steampunk_fantasy"))
        self.assertIn("教会帝国博弈", rules_text)
        self.assertIn("炼金经济", rules_text)

    def test_general_rules_cover_kimi_core_dimensions(self):
        with open(os.path.join("profiles", "general", "rules.json"), "r", encoding="utf-8") as f:
            rules = json.load(f)

        categories = {item["name"]: item for item in rules["categories"]}
        for category_name in ["叙事与节奏", "人物塑造", "阅读体验", "完成度"]:
            self.assertIn(category_name, categories)

        points = {
            point["name"]
            for category in rules["categories"]
            for point in category.get("points", [])
        }
        for point_name in [
            "叙事完整性",
            "节奏控制",
            "主角动机",
            "配角立体度",
            "反派质量",
            "爽点设计",
            "虐点控制",
            "文笔与表达",
            "伏笔回收",
            "结局质量",
        ]:
            self.assertIn(point_name, points)
        self.assertGreaterEqual(len(points), 15)

        general_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("general"))
        self.assertIn("叙事完整性", general_text)
        self.assertIn("期待管理", general_text)

    def test_xianxia_isekai_and_game_rules_include_kimi_categories(self):
        rule_paths = {
            "xianxia_fantasy": os.path.join("profiles", "xianxia_fantasy", "rules.json"),
            "isekai_lightnovel": os.path.join("profiles", "isekai_lightnovel", "rules.json"),
            "game_system": os.path.join("profiles", "game_system", "rules.json"),
        }
        rules = {}
        for name, path in rule_paths.items():
            with open(path, "r", encoding="utf-8") as f:
                rules[name] = json.load(f)

        xianxia_points = {
            point["name"]
            for category in rules["xianxia_fantasy"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("血脉体质", xianxia_points)
        self.assertIn("多元修炼", xianxia_points)
        self.assertIn("神话融入", xianxia_points)
        self.assertIn("求道主题", xianxia_points)

        isekai_categories = {item["name"] for item in rules["isekai_lightnovel"]["categories"]}
        isekai_points = {
            point["name"]
            for category in rules["isekai_lightnovel"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("种族文化与政治", isekai_categories)
        self.assertIn("冒险与叙事", isekai_categories)
        self.assertIn("种族生态", isekai_points)
        self.assertIn("政治结构", isekai_points)
        self.assertIn("日常平衡", isekai_points)
        self.assertIn("轻小说节奏", isekai_points)
        self.assertGreaterEqual(len(isekai_points), 12)

        game_categories = {item["name"] for item in rules["game_system"]["categories"]}
        game_points = {
            point["name"]
            for category in rules["game_system"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("玩家互动与世界", game_categories)
        self.assertIn("流派判定", game_points)
        self.assertIn("机制创新", game_points)
        self.assertIn("世界多样性", game_points)
        self.assertIn("NPC逻辑", game_points)
        self.assertIn("现实影响", game_points)

        xianxia_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("xianxia_fantasy"))
        isekai_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("isekai_lightnovel"))
        game_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("game_system"))
        self.assertIn("求道主题", xianxia_text)
        self.assertIn("轻小说节奏", isekai_text)
        self.assertIn("玩家互动", game_text)

    def test_new_emerging_profile_rules_have_minimum_kimi_depth(self):
        expected_points = {
            "nation_fate": ["奖励反噬", "战力尺度", "直播舆论"],
            "simulator": ["结算通胀", "现实信息差", "重复日志压缩"],
            "chinese_weird": ["规则冲突", "仪式代价", "污染扩散"],
            "mastermind_hidden": ["马甲数量管理", "组织资源成本", "暴露后果"],
        }

        for profile_name, point_names in expected_points.items():
            with open(os.path.join("profiles", profile_name, "rules.json"), "r", encoding="utf-8") as f:
                rules = json.load(f)
            points = {
                point["name"]
                for category in rules["categories"]
                for point in category.get("points", [])
            }
            self.assertGreaterEqual(len(points), 12, profile_name)
            for point_name in point_names:
                self.assertIn(point_name, points, profile_name)

            rules_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile(profile_name))
            for point_name in point_names:
                self.assertIn(point_name, rules_text)

    def test_specialty_profiles_import_harem_cross_rules(self):
        for profile_name in [
            "xianxia_fantasy",
            "history",
            "hard_sci_fi",
            "urban_power",
            "game_system",
            "isekai_lightnovel",
            "steampunk_fantasy",
            "apocalypse_survival",
            "military_war",
            "crime_forensics",
            "cosmic_horror",
            "campus_youth",
            "entertainment_industry",
            "farming_management",
            "business_career",
            "mystery_detective",
            "sports_competition",
            "nation_fate",
            "simulator",
            "chinese_weird",
            "mastermind_hidden",
        ]:
            profile = analysis_profiles.load_analysis_profile(profile_name)
            rules_text = general_scan._profile_rules_text(profile)

            self.assertIn("harem", profile.cross_profile_rules)
            self.assertIn("跨类型导入：后宫/男性向排雷分析", rules_text)
            self.assertIn("绿帽", rules_text)
            self.assertIn("送女", rules_text)
            self.assertIn("漏女", rules_text)
            self.assertNotIn("- 万人骑:", rules_text)

    def test_selected_profiles_import_non_harem_cross_rules(self):
        expected = {
            "mystery_detective": [
                "跨类型导入：刑侦/法医/案件专长分析",
                "案情要素",
                "推理路径",
                "技术边界",
                "跨类型导入：克苏鲁/诡秘/怪谈专长分析",
                "规则稳定",
                "线索链",
            ],
            "history": [
                "跨类型导入：军事/战争专长分析",
                "时代定位",
                "战略目标",
                "战役结构",
                "后勤通信",
            ],
            "farming_management": [
                "跨类型导入：末世/灾变/生存专长分析",
                "资源消耗",
                "据点建设",
                "秩序重建",
                "末世经济",
            ],
            "steampunk_fantasy": [
                "跨类型导入：悬疑/推理专长分析",
                "谜题设置",
                "案件主线连接",
                "跨类型导入：克苏鲁/诡秘/怪谈专长分析",
                "力量边界",
            ],
        }

        for profile_name, fragments in expected.items():
            profile = analysis_profiles.load_analysis_profile(profile_name)
            rules_text = general_scan._profile_rules_text(profile)
            for fragment in fragments:
                self.assertIn(fragment, rules_text, profile_name)

    def test_military_apocalypse_and_crime_rules_include_kimi_categories(self):
        rule_paths = {
            "military_war": os.path.join("profiles", "military_war", "rules.json"),
            "apocalypse_survival": os.path.join("profiles", "apocalypse_survival", "rules.json"),
            "crime_forensics": os.path.join("profiles", "crime_forensics", "rules.json"),
        }
        rules = {}
        for name, path in rule_paths.items():
            with open(path, "r", encoding="utf-8") as f:
                rules[name] = json.load(f)

        military_categories = {item["name"] for item in rules["military_war"]["categories"]}
        military_points = {
            point["name"]
            for category in rules["military_war"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("战争类型与规模", military_categories)
        self.assertIn("部队建设与军工装备", military_categories)
        self.assertIn("政治与外交", military_categories)
        self.assertIn("军工装备", military_points)
        self.assertIn("战斗描写", military_points)
        self.assertIn("军事职业线", military_points)

        apocalypse_categories = {item["name"] for item in rules["apocalypse_survival"]["categories"]}
        apocalypse_points = {
            point["name"]
            for category in rules["apocalypse_survival"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("人性与道德", apocalypse_categories)
        self.assertIn("能力体系", apocalypse_categories)
        self.assertIn("探索冒险", apocalypse_categories)
        self.assertIn("末世经济", apocalypse_points)
        self.assertIn("物理威胁边界", apocalypse_points)
        self.assertIn("搜集行动", apocalypse_points)

        crime_categories = {item["name"] for item in rules["crime_forensics"]["categories"]}
        crime_points = {
            point["name"]
            for category in rules["crime_forensics"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("犯罪心理与侧写", crime_categories)
        self.assertIn("团队群像与社会映射", crime_categories)
        self.assertIn("案件复杂度", crime_points)
        self.assertIn("执法身份", crime_points)
        self.assertIn("社会映射", crime_points)

        military_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("military_war"))
        apocalypse_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("apocalypse_survival"))
        crime_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("crime_forensics"))
        self.assertIn("军工装备", military_text)
        self.assertIn("能力体系", apocalypse_text)
        self.assertIn("犯罪心理", crime_text)

    def test_mystery_horror_sports_and_farming_rules_include_kimi_categories(self):
        rule_paths = {
            "mystery_detective": os.path.join("profiles", "mystery_detective", "rules.json"),
            "cosmic_horror": os.path.join("profiles", "cosmic_horror", "rules.json"),
            "chinese_weird": os.path.join("profiles", "chinese_weird", "rules.json"),
            "mastermind_hidden": os.path.join("profiles", "mastermind_hidden", "rules.json"),
            "sports_competition": os.path.join("profiles", "sports_competition", "rules.json"),
            "farming_management": os.path.join("profiles", "farming_management", "rules.json"),
        }
        rules = {}
        for name, path in rule_paths.items():
            with open(path, "r", encoding="utf-8") as f:
                rules[name] = json.load(f)

        mystery_categories = {item["name"] for item in rules["mystery_detective"]["categories"]}
        mystery_points = {
            point["name"]
            for category in rules["mystery_detective"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("侦探角色与叙事结构", mystery_categories)
        self.assertIn("侦探魅力", mystery_points)
        self.assertIn("推理方法论", mystery_points)
        self.assertIn("叙事结构", mystery_points)

        horror_categories = {item["name"] for item in rules["cosmic_horror"]["categories"]}
        horror_points = {
            point["name"]
            for category in rules["cosmic_horror"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("超凡体系与组织", horror_categories)
        self.assertIn("规则怪谈", horror_points)
        self.assertIn("力量边界", horror_points)
        self.assertIn("组织两面性", horror_points)

        chinese_weird_categories = {item["name"] for item in rules["chinese_weird"]["categories"]}
        chinese_weird_points = {
            point["name"]
            for category in rules["chinese_weird"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("规则机制与验证", chinese_weird_categories)
        self.assertIn("中式民俗与禁忌", chinese_weird_categories)
        self.assertIn("副本逃生与现实侵蚀", chinese_weird_categories)
        self.assertIn("隐藏规则", chinese_weird_points)
        self.assertIn("民俗底盘", chinese_weird_points)
        self.assertIn("现实侵蚀", chinese_weird_points)
        self.assertIn("规则冲突", chinese_weird_points)
        self.assertIn("仪式代价", chinese_weird_points)
        self.assertIn("污染扩散", chinese_weird_points)

        mastermind_categories = {item["name"] for item in rules["mastermind_hidden"]["categories"]}
        mastermind_points = {
            point["name"]
            for category in rules["mastermind_hidden"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("身份与马甲体系", mastermind_categories)
        self.assertIn("信息差与幕后排局", mastermind_categories)
        self.assertIn("掉马揭秘与爽点兑现", mastermind_categories)
        self.assertIn("马甲边界", mastermind_points)
        self.assertIn("信息差来源", mastermind_points)
        self.assertIn("掉马时机", mastermind_points)
        self.assertIn("马甲数量管理", mastermind_points)
        self.assertIn("组织资源成本", mastermind_points)
        self.assertIn("暴露后果", mastermind_points)

        sports_categories = {item["name"] for item in rules["sports_competition"]["categories"]}
        sports_points = {
            point["name"]
            for category in rules["sports_competition"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("对手刻画与名场面", sports_categories)
        self.assertIn("竞争关系", sports_points)
        self.assertIn("关键比赛", sports_points)
        self.assertIn("名场面", sports_points)

        farming_categories = {item["name"] for item in rules["farming_management"]["categories"]}
        farming_points = {
            point["name"]
            for category in rules["farming_management"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("类型边界", farming_categories)
        self.assertIn("科技树", farming_points)
        self.assertIn("基建路径", farming_points)
        self.assertIn("慢节奏爽点", farming_points)

        mystery_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("mystery_detective"))
        horror_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("cosmic_horror"))
        chinese_weird_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("chinese_weird"))
        mastermind_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("mastermind_hidden"))
        sports_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("sports_competition"))
        farming_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("farming_management"))
        self.assertIn("侦探魅力", mystery_text)
        self.assertIn("规则怪谈", horror_text)
        self.assertIn("民俗底盘", chinese_weird_text)
        self.assertIn("污染扩散", chinese_weird_text)
        self.assertIn("马甲边界", mastermind_text)
        self.assertIn("组织资源成本", mastermind_text)
        self.assertIn("名场面", sports_text)
        self.assertIn("科技树", farming_text)

    def test_urban_campus_entertainment_and_business_rules_include_kimi_categories(self):
        rule_paths = {
            "urban_power": os.path.join("profiles", "urban_power", "rules.json"),
            "campus_youth": os.path.join("profiles", "campus_youth", "rules.json"),
            "entertainment_industry": os.path.join("profiles", "entertainment_industry", "rules.json"),
            "business_career": os.path.join("profiles", "business_career", "rules.json"),
        }
        rules = {}
        for name, path in rule_paths.items():
            with open(path, "r", encoding="utf-8") as f:
                rules[name] = json.load(f)

        urban_points = {
            point["name"]
            for category in rules["urban_power"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("升级机制", urban_points)
        self.assertIn("压扬循环", urban_points)
        self.assertIn("势力层级", urban_points)
        self.assertIn("女性角色与感情模式", urban_points)
        self.assertIn("专业能力展示", urban_points)
        self.assertIn("法治舆论后果", urban_points)

        campus_categories = {item["name"] for item in rules["campus_youth"]["categories"]}
        campus_points = {
            point["name"]
            for category in rules["campus_youth"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("时代与亚文化", campus_categories)
        self.assertIn("重生校园", campus_points)
        self.assertIn("毕业过渡", campus_points)
        self.assertIn("网络文化", campus_points)

        entertainment_points = {
            point["name"]
            for category in rules["entertainment_industry"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("IP改编", entertainment_points)
        self.assertIn("选秀练习生", entertainment_points)
        self.assertIn("网红短视频", entertainment_points)
        self.assertIn("CP营业", entertainment_points)
        self.assertIn("艺人日常", entertainment_points)

        business_points = {
            point["name"]
            for category in rules["business_career"]["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("产品与用户", business_points)
        self.assertIn("技术路线", business_points)
        self.assertIn("供应链产业链", business_points)
        self.assertIn("办公室政治", business_points)
        self.assertIn("主角专业度", business_points)

        urban_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("urban_power"))
        campus_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("campus_youth"))
        entertainment_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("entertainment_industry"))
        business_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("business_career"))
        self.assertIn("升级机制", urban_text)
        self.assertIn("网络文化", campus_text)
        self.assertIn("选秀练习生", entertainment_text)
        self.assertIn("供应链", business_text)

    def test_harem_rules_include_kimi_expansions_without_losing_locks(self):
        rules_path = os.path.join("profiles", "harem", "rules.json")
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)

        categories = {item["name"]: item for item in rules["categories"]}
        poison_points = {item["name"]: item["description"] for item in categories["雷点（严重毒点）"]["points"]}
        depressing_points = {item["name"]: item["description"] for item in categories["郁闷点"]["points"]}
        glossary = {item["term"]: item["definition"] for item in rules["glossary"]}

        self.assertIn("群交/多人运动", poison_points)
        self.assertIn("雌堕/洗脑改造", poison_points)
        self.assertIn("隐私曝光（直播/录像/拍照）", poison_points)
        self.assertIn("人格侮辱/公开调教", poison_points)

        self.assertIn("进度条诈骗", depressing_points)
        self.assertIn("NTR擦边/反复救援", depressing_points)
        self.assertIn("工具人女主", depressing_points)
        self.assertIn("后宫内斗/宅斗", depressing_points)
        self.assertIn("替身/代餐", depressing_points)

        self.assertIn("前世洁度", glossary)
        self.assertIn("接触等级", glossary)
        self.assertIn("partner豁免", glossary)
        self.assertIn("漏女三层判定", glossary)
        self.assertIn("女主有效性", glossary)

        self.assertIn("仅限男主视角", poison_points["绿帽"])
        self.assertIn("配角把女性献给男主", poison_points["绿帽"])
        self.assertIn("男主主动或默许", poison_points["送女"])
        self.assertIn("反派计划把女性送人", poison_points["送女"])

    def test_root_rules2_keeps_harem_fallback_rules_in_sync(self):
        with open(os.path.join("profiles", "harem", "rules.json"), "r", encoding="utf-8") as f:
            harem_rules = json.load(f)
        with open("rules2.json", "r", encoding="utf-8") as f:
            fallback_rules = json.load(f)

        self.assertEqual(harem_rules, fallback_rules)

        categories = {item["name"]: item for item in fallback_rules["categories"]}
        poison_points = {item["name"]: item["description"] for item in categories["雷点（严重毒点）"]["points"]}
        depressing_points = {item["name"]: item["description"] for item in categories["郁闷点"]["points"]}
        glossary = {item["term"]: item["definition"] for item in fallback_rules["glossary"]}

        self.assertIn("群交/多人运动", poison_points)
        self.assertIn("雌堕/洗脑改造", poison_points)
        self.assertIn("工具人女主", depressing_points)
        self.assertIn("漏女三层判定", glossary)
        self.assertIn("仅限男主视角", poison_points["绿帽"])
        self.assertIn("女主被非男主男性强迫", poison_points["绿帽"])
        self.assertIn("只有出现明确性关系或女主主观情感背叛时才可判为绿帽", poison_points["绿帽"])
        self.assertNotIn("任何男性发生肢体接触或暧昧包括被强迫", poison_points["绿帽"])
        self.assertIn("男主主动或默许", poison_points["送女"])
        self.assertIn("反派计划把女性送人、但男主没有主动参与，不是送女", poison_points["送女"])

    def test_harem_scan_prompt_mentions_leak_layers_and_tooling(self):
        categories = [
            {
                "name": "郁闷点",
                "description": "测试",
                "points": [
                    {"name": "漏女", "description": "测试"},
                    {"name": "工具人女主", "description": "测试"},
                ],
            }
        ]
        glossary = [
            {"term": "漏女三层判定", "definition": "测试"},
            {"term": "女主有效性", "definition": "测试"},
        ]

        prompt = novel_scan.build_prompt(categories, glossary, ["甲女"], {"name": "男主"})

        self.assertIn("Prompt模板：harem_scan_chunk@v1", prompt)
        self.assertIn("漏女三层判定", prompt)
        self.assertIn("情感深度", prompt)
        self.assertIn("关系是否确认", prompt)
        self.assertIn("结局是否交代", prompt)
        self.assertIn("工具人女主", prompt)
        self.assertIn("感情戏缺失", prompt)
        self.assertIn("经济依附", prompt)
        self.assertIn("权力关系", prompt)
        self.assertIn("政治联姻", prompt)
        self.assertIn("受害/胁迫记录", prompt)
        self.assertIn("economic_attachments", prompt)
        self.assertIn("power_relations", prompt)
        self.assertIn("political_marriages", prompt)
        self.assertIn("victim_records", prompt)
        self.assertIn("绿帽（NTR）判定【锁定定义】", prompt)
        self.assertIn("仅限男主视角、仅限目标女主或强准女主", prompt)
        self.assertIn("反派口嗨", prompt)
        self.assertIn("男主睡了女主的亲戚/闺蜜", prompt)
        self.assertIn("送女】判定【锁定定义】", prompt)
        self.assertIn("男主主动或默许", prompt)
        self.assertIn("对象是目标女主或强准女主", prompt)
        self.assertIn("反派计划把女性送人但男主没有主动参与", prompt)

    def test_harem_scan_checklist_is_sent_only_for_first_chunk(self):
        categories = [{"name": "郁闷点", "description": "测试", "points": []}]
        system_prompt = novel_scan.build_prompt(categories, [], ["甲女"], {"name": "男主"})
        self.assertNotIn("输出前自检", system_prompt)
        self.assertNotIn("每个条目的五个维度", system_prompt)

        calls = []
        old_call = novel_scan._call_json_chat_completion
        try:
            def fake_call(messages, **_kwargs):
                calls.append(messages)

                class Message:
                    content = json.dumps({
                        "issues": [],
                        "heroine_facts": [],
                        "extra_relations": [],
                        "_reasoning": "已自检",
                        "_context_summary": "片段摘要",
                    }, ensure_ascii=False)

                class Choice:
                    message = Message()

                class Response:
                    choices = [Choice()]

                return Response()

            novel_scan._call_json_chat_completion = fake_call
            novel_scan.scan_chunk("甲女出场。", 0, 2, system_prompt, ["甲女"], {"name": "男主"})
            novel_scan.scan_chunk("甲女继续行动。", 1, 2, system_prompt, ["甲女"], {"name": "男主"})
        finally:
            novel_scan._call_json_chat_completion = old_call

        first_user_prompt = calls[0][1]["content"]
        second_user_prompt = calls[1][1]["content"]
        self.assertIn("输出前自检", first_user_prompt)
        self.assertIn("每个条目的五个维度", first_user_prompt)
        self.assertNotIn("输出前自检", second_user_prompt)
        self.assertNotIn("每个条目的五个维度", second_user_prompt)

    def test_scan_json_parser_accepts_fenced_response(self):
        data = novel_scan._safe_json_loads(
            '```json\n{"issues":[],"heroine_facts":[],"extra_relations":[]}\n```'
        )
        self.assertEqual(data["issues"], [])

    def test_scan_json_parser_keeps_string_values_ending_with_digits(self):
        data = novel_scan._safe_json_loads(
            '{"issues":[{"type":"半块1","content":"问题1"}],"heroine_facts":[],"extra_relations":[]}'
        )
        self.assertEqual(data["issues"][0]["type"], "半块1")
        self.assertEqual(data["issues"][0]["content"], "问题1")

    def test_scan_json_response_diagnostic_flags_truncated_fence(self):
        diagnostic = novel_scan._diagnose_json_response_text(
            '```json\n{"issues":[{"content":"未闭合"'
        )
        self.assertIn("code_fence_unclosed", diagnostic["flags"])
        self.assertIn("json_unbalanced", diagnostic["flags"])
        self.assertIn("likely_truncated", diagnostic["flags"])

    def test_scan_chunk_uses_compact_retry_after_truncated_json(self):
        class Message:
            def __init__(self, content):
                self.content = content

        class Choice:
            def __init__(self, content):
                self.message = Message(content)

        class Response:
            def __init__(self, content):
                self.choices = [Choice(content)]

        calls = []
        old_call = novel_scan._call_json_chat_completion
        try:
            def fake_call(messages, **kwargs):
                calls.append((messages, kwargs))
                if len(calls) == 1:
                    return Response('```json\n{"issues":[{"content":"未闭合"')
                return Response(json.dumps({
                    "issues": [],
                    "heroine_facts": [],
                    "extra_relations": [],
                }, ensure_ascii=False))

            novel_scan._call_json_chat_completion = fake_call
            issues, facts, extra, _summary, ok, fatal, err = novel_scan.scan_chunk(
                "甲女与男主同行。",
                0,
                1,
                "只输出 JSON",
                ["甲女"],
                {"name": "男主"},
            )
        finally:
            novel_scan._call_json_chat_completion = old_call

        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertEqual(err, "")
        self.assertEqual(issues, [])
        self.assertEqual(facts, [])
        self.assertEqual(extra, [])
        self.assertEqual(calls[0][1]["max_tokens"], 6000)
        self.assertEqual(calls[1][1]["max_tokens"], 8000)
        self.assertIn("重试压缩要求", calls[1][0][1]["content"])

    def test_scan_chunk_downshifts_api_504_by_splitting_text(self):
        class Message:
            def __init__(self, content):
                self.content = content

        class Choice:
            def __init__(self, content):
                self.message = Message(content)

        class Response:
            def __init__(self, content):
                self.choices = [Choice(content)]

        calls = []
        old_call = novel_scan._call_json_chat_completion
        old_sleep = novel_scan.time.sleep
        old_depth = novel_scan.HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH
        old_min_chars = novel_scan.HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS
        try:
            def fake_call(messages, **kwargs):
                calls.append((messages, kwargs))
                if len(calls) <= 3:
                    raise RuntimeError("服务器错误(504)")
                part_no = len(calls) - 3
                return Response(json.dumps({
                    "issues": [{"type": f"半块{part_no}", "content": f"问题{part_no}"}],
                    "heroine_facts": [{
                        "name": "甲女",
                        "facts": {
                            "relationship": [{"content": f"事实{part_no}", "evidence": f"证据{part_no}"}],
                        },
                    }],
                    "extra_relations": [{"name": "甲女", "evidence": f"关系{part_no}"}],
                    "_context_summary": f"摘要{part_no}",
                }, ensure_ascii=False))

            novel_scan._call_json_chat_completion = fake_call
            novel_scan.time.sleep = lambda *_args, **_kwargs: None
            novel_scan.HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH = 1
            novel_scan.HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS = 20

            text = "甲女与男主同行。" * 20
            issues, facts, extra, summary, ok, fatal, err = novel_scan.scan_chunk(
                text,
                0,
                1,
                "只输出 JSON",
                ["甲女"],
                {"name": "男主"},
            )
        finally:
            novel_scan._call_json_chat_completion = old_call
            novel_scan.time.sleep = old_sleep
            novel_scan.HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH = old_depth
            novel_scan.HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS = old_min_chars

        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 5)
        self.assertEqual([item["type"] for item in issues], ["半块1", "半块2"])
        self.assertEqual([item["partial_index"] for item in issues], [1, 2])
        self.assertEqual([item["partial_index"] for item in facts], [1, 2])
        self.assertEqual([item["partial_index"] for item in extra], [1, 2])
        self.assertIn("摘要1", summary)
        self.assertIn("摘要2", summary)

    def test_initial_scan_partition_uses_dynamic_small_blocks(self):
        blocks = novel_scan._partition_indices_for_thread_blocks(
            40,
            4,
            block_multiplier=3,
            min_block_size=5,
        )

        flattened = [idx for block in blocks for idx in block]
        self.assertGreater(len(blocks), 4)
        self.assertLessEqual(len(blocks), 12)
        self.assertEqual(flattened, list(range(40)))
        self.assertTrue(all(block == list(range(block[0], block[-1] + 1)) for block in blocks))
        self.assertTrue(all(len(block) >= 3 for block in blocks))

    def test_initial_scan_partition_keeps_small_books_one_chunk_per_block(self):
        blocks = novel_scan._partition_indices_for_thread_blocks(
            3,
            10,
            block_multiplier=3,
            min_block_size=5,
        )

        self.assertEqual(blocks, [[0], [1], [2]])

    def test_cancel_pending_futures_cancels_unfinished_work(self):
        class FakeFuture:
            def __init__(self, done=False):
                self._done = done
                self.cancelled = False

            def done(self):
                return self._done

            def cancel(self):
                self.cancelled = True

        class FakeExecutor:
            def __init__(self):
                self.shutdown_args = None

            def shutdown(self, **kwargs):
                self.shutdown_args = kwargs

        current = FakeFuture(done=True)
        pending = FakeFuture(done=False)
        already_done = FakeFuture(done=True)
        executor = FakeExecutor()

        novel_scan._cancel_pending_futures([current, pending, already_done], current_future=current, executor=executor)

        self.assertFalse(current.cancelled)
        self.assertTrue(pending.cancelled)
        self.assertFalse(already_done.cancelled)
        self.assertEqual(executor.shutdown_args, {"wait": False, "cancel_futures": True})

    def test_iter_completed_futures_default_uses_as_completed(self):
        class FakeFuture:
            pass

        first = FakeFuture()
        second = FakeFuture()
        called = []

        def fake_as_completed(futures):
            called.append(list(futures))
            return iter([second, first])

        with mock.patch.object(shared_utils.concurrent.futures, "as_completed", side_effect=fake_as_completed):
            result = list(shared_utils.iter_completed_futures({first: "a", second: "b"}, timeout_seconds=0))

        self.assertEqual(result, [second, first])
        self.assertEqual(called, [[first, second]])

    def test_iter_completed_futures_timeout_cancels_pending(self):
        class FakeFuture:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        class FakeExecutor:
            def __init__(self):
                self.shutdown_args = None

            def shutdown(self, **kwargs):
                self.shutdown_args = kwargs

        pending = FakeFuture()
        executor = FakeExecutor()

        def fake_wait(fs, timeout=None, return_when=None):
            self.assertEqual(set(fs), {pending})
            self.assertEqual(timeout, 3.0)
            self.assertEqual(return_when, novel_scan.concurrent.futures.FIRST_COMPLETED)
            return set(), set(fs)

        with mock.patch.object(shared_utils.concurrent.futures, "wait", side_effect=fake_wait):
            with self.assertRaisesRegex(TimeoutError, "补扫 future stall timeout"):
                list(shared_utils.iter_completed_futures({pending: 1}, phase_name="补扫", timeout_seconds=3, executor=executor))

        self.assertTrue(pending.cancelled)
        self.assertEqual(executor.shutdown_args, {"wait": False, "cancel_futures": True})

    def test_scan_checkpoint_incremental_delta_merges_on_load(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 3}
                novel_scan.CHUNK_SUMMARIES = {0: "第一块摘要"}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                issue0 = {"type": "前世雷", "chunk_index": 1}
                fact0 = {"name": "甲女", "chunk_index": 1}
                rel0 = {"name": "甲女", "chunk_index": 1}
                novel_scan.save_checkpoint(
                    [issue0],
                    [fact0],
                    {0},
                    [rel0],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )

                issue1 = {"type": "漏女", "chunk_index": 2}
                fact1 = {"name": "乙女", "chunk_index": 2}
                rel1 = {"name": "乙女", "chunk_index": 2}
                novel_scan.CHUNK_SUMMARIES = {0: "第一块摘要", 1: "第二块摘要"}
                novel_scan.save_checkpoint(
                    [issue0, issue1],
                    [fact0, fact1],
                    {0, 1},
                    [rel0, rel1],
                    failed_chunks=set(),
                    current_chunk_idx=1,
                    incremental=True,
                    delta_issues=[issue1],
                    delta_heroine_facts=[fact1],
                    delta_extra_relations=[rel1],
                    delta_chunk_summary="第二块摘要",
                )

                with open(novel_scan.CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                    base_data = json.load(f)
                self.assertEqual(base_data["issues"], [issue0])
                self.assertTrue(os.path.exists(f"{novel_scan.CHECKPOINT_FILE}.delta.jsonl"))

                novel_scan.CHUNK_SUMMARIES = {}
                loaded = novel_scan.load_checkpoint()
                issues, heroine_facts, processed, extra_relations, failed, _profiles, detail_path, _done, _completed = loaded

                self.assertEqual(issues, [issue0, issue1])
                self.assertEqual(heroine_facts, [fact0, fact1])
                self.assertEqual(extra_relations, [rel0, rel1])
                self.assertEqual(processed, {0, 1})
                self.assertEqual(failed, set())
                self.assertEqual(detail_path, "/tmp/detail.json")
                self.assertEqual(novel_scan.CHUNK_SUMMARIES, {0: "第一块摘要", 1: "第二块摘要"})
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_scan_checkpoint_recovers_from_backup_and_delta_when_primary_is_corrupt(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 3}
                novel_scan.CHUNK_SUMMARIES = {0: "第一块摘要"}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                issue0 = {"type": "前世雷", "chunk_index": 1}
                issue1 = {"type": "漏女", "chunk_index": 2}
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )
                novel_scan.save_checkpoint(
                    [issue0, issue1],
                    [],
                    {0, 1},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=1,
                    incremental=True,
                    delta_issues=[issue1],
                    delta_chunk_summary="第二块摘要",
                )
                self.assertTrue(os.path.exists(f"{novel_scan.CHECKPOINT_FILE}.bak"))

                with open(novel_scan.CHECKPOINT_FILE, "w", encoding="utf-8") as f:
                    f.write("{broken")

                novel_scan.CHUNK_SUMMARIES = {}
                loaded = novel_scan.load_checkpoint()
                issues, _facts, processed, _extra, failed, _profiles, detail_path, _done, _completed = loaded

                self.assertEqual(issues, [issue0, issue1])
                self.assertEqual(processed, {0, 1})
                self.assertEqual(failed, set())
                self.assertEqual(detail_path, "/tmp/detail.json")
                self.assertEqual(novel_scan.CHUNK_SUMMARIES, {0: "第一块摘要", 1: "第二块摘要"})
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_scan_checkpoint_recovers_when_primary_is_missing_but_backup_exists(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 2}
                novel_scan.CHUNK_SUMMARIES = {0: "第一块摘要"}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                issue0 = {"type": "前世雷", "chunk_index": 1}
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )
                self.assertTrue(os.path.exists(f"{novel_scan.CHECKPOINT_FILE}.bak"))
                os.unlink(novel_scan.CHECKPOINT_FILE)

                self.assertEqual(novel_scan._peek_checkpoint_detail_path(), "/tmp/detail.json")
                novel_scan.CHUNK_SUMMARIES = {}
                loaded = novel_scan.load_checkpoint()
                issues, _facts, processed, _extra, failed, _profiles, detail_path, _done, _completed = loaded

                self.assertEqual(issues, [issue0])
                self.assertEqual(processed, {0})
                self.assertEqual(failed, set())
                self.assertEqual(detail_path, "/tmp/detail.json")
                self.assertEqual(novel_scan.CHUNK_SUMMARIES, {0: "第一块摘要"})
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_scan_checkpoint_incremental_periodically_merges_full_file(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 10}
                novel_scan.CHUNK_SUMMARIES = {i: f"摘要{i}" for i in range(10)}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                issue0 = {"type": "前世雷", "chunk_index": 1}
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )

                issue1 = {"type": "漏女", "chunk_index": 2}
                novel_scan.save_checkpoint(
                    [issue0, issue1],
                    [],
                    {0, 1},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=1,
                    incremental=True,
                    delta_issues=[issue1],
                    delta_chunk_summary="摘要1",
                )
                delta_path = f"{novel_scan.CHECKPOINT_FILE}.delta.jsonl"
                self.assertTrue(os.path.exists(delta_path))

                all_issues = [{"type": f"类型{i}", "chunk_index": i + 1} for i in range(10)]
                novel_scan.save_checkpoint(
                    all_issues,
                    [],
                    set(range(10)),
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=9,
                    incremental=True,
                    delta_issues=[all_issues[-1]],
                    delta_chunk_summary="摘要9",
                )

                self.assertFalse(os.path.exists(delta_path))
                with open(novel_scan.CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["issues"], all_issues)
                self.assertEqual(set(data["processed_chunks"]), set(range(10)))
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_checkpoint_explicit_paths_keep_runtime_state_isolated(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_diagnostics = dict(novel_scan.CHUNK_FAILURE_DIAGNOSTICS)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                path_a = os.path.join(tmpdir, "book_a_checkpoint.json")
                path_b = os.path.join(tmpdir, "book_b_checkpoint.json")
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "global_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 99}
                novel_scan.CHUNK_SUMMARIES = {99: "全局摘要"}
                novel_scan.CHUNK_FAILURE_DIAGNOSTICS = {99: {"flags": ["global"]}}

                novel_scan.save_checkpoint(
                    [{"type": "A", "chunk_index": 1}],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                    checkpoint_file=path_a,
                    chunk_plan_metadata={"chunk_count": 2},
                    chunk_summaries={0: "A摘要"},
                    detail_path="/tmp/a_detail.json",
                    chunk_failure_diagnostics={},
                )
                novel_scan.save_checkpoint(
                    [],
                    [],
                    set(),
                    [],
                    failed_chunks={1},
                    current_chunk_idx=1,
                    checkpoint_file=path_b,
                    chunk_plan_metadata={"chunk_count": 3},
                    chunk_summaries={1: "B摘要"},
                    detail_path="/tmp/b_detail.json",
                    chunk_failure_diagnostics={1: {"flags": ["nul_bytes"], "severity": "high"}},
                )

                loaded_a = novel_scan.load_checkpoint(
                    checkpoint_file=path_a,
                    chunk_plan_metadata={"chunk_count": 2},
                    update_globals=False,
                )
                loaded_b = novel_scan.load_checkpoint(
                    checkpoint_file=path_b,
                    chunk_plan_metadata={"chunk_count": 3},
                    update_globals=False,
                )

                self.assertEqual(loaded_a[0], [{"type": "A", "chunk_index": 1}])
                self.assertEqual(loaded_a[2], {0})
                self.assertEqual(loaded_a[4], set())
                self.assertEqual(loaded_a[6], "/tmp/a_detail.json")
                self.assertEqual(loaded_b[2], set())
                self.assertEqual(loaded_b[4], {1})
                self.assertEqual(loaded_b[6], "/tmp/b_detail.json")
                self.assertEqual(novel_scan.CHUNK_SUMMARIES, {99: "全局摘要"})
                self.assertEqual(novel_scan.CHUNK_FAILURE_DIAGNOSTICS, {99: {"flags": ["global"]}})

                with open(path_b, "r", encoding="utf-8") as f:
                    data_b = json.load(f)
                self.assertEqual(data_b["chunk_failure_diagnostics"]["1"]["flags"], ["nul_bytes"])
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan.CHUNK_FAILURE_DIAGNOSTICS = old_diagnostics

    def test_scan_checkpoint_rescan_plan_preserves_matching_progress(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                checkpoint_path = os.path.join(tmpdir, "latest_checkpoint.json")
                chunk_plan = {"chunk_count": 2, "signature": "chunk-sig"}
                rescan_plan = {"signature": "rescan-a"}
                novel_scan.CHECKPOINT_FILE = checkpoint_path
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = chunk_plan

                issue0 = {"type": "雷点", "chunk_index": 1}
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0, 1},
                    [],
                    failed_chunks=set(),
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_done_chunks={0},
                    rescan_completed=True,
                    rescan_plan_metadata=rescan_plan,
                )

                loaded = novel_scan.load_checkpoint(
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_plan_metadata=rescan_plan,
                    update_globals=False,
                )
                self.assertEqual(loaded[0], [issue0])
                self.assertEqual(loaded[2], {0, 1})
                self.assertEqual(loaded[7], {0})
                self.assertTrue(loaded[8])
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan

    def test_scan_checkpoint_resets_only_rescan_progress_when_plan_changes(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                checkpoint_path = os.path.join(tmpdir, "latest_checkpoint.json")
                chunk_plan = {"chunk_count": 2, "signature": "chunk-sig"}
                novel_scan.CHECKPOINT_FILE = checkpoint_path
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = chunk_plan

                issue0 = {"type": "雷点", "chunk_index": 1}
                novel_scan.save_checkpoint(
                    [issue0],
                    [],
                    {0, 1},
                    [],
                    failed_chunks=set(),
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_done_chunks={0, 1},
                    rescan_completed=True,
                    rescan_plan_metadata={"signature": "rescan-a"},
                )

                loaded = novel_scan.load_checkpoint(
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_plan_metadata={"signature": "rescan-b"},
                    update_globals=False,
                )
                self.assertEqual(loaded[0], [issue0])
                self.assertEqual(loaded[2], {0, 1})
                self.assertEqual(loaded[7], set())
                self.assertFalse(loaded[8])
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan

    def test_scan_checkpoint_resets_legacy_rescan_progress_without_plan_signature(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                checkpoint_path = os.path.join(tmpdir, "latest_checkpoint.json")
                chunk_plan = {"chunk_count": 2, "signature": "chunk-sig"}
                novel_scan.CHECKPOINT_FILE = checkpoint_path
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = chunk_plan

                novel_scan.save_checkpoint(
                    [],
                    [],
                    {0, 1},
                    [],
                    failed_chunks=set(),
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_done_chunks={0},
                    rescan_completed=True,
                )

                loaded = novel_scan.load_checkpoint(
                    checkpoint_file=checkpoint_path,
                    chunk_plan_metadata=chunk_plan,
                    rescan_plan_metadata={"signature": "rescan-current"},
                    update_globals=False,
                )
                self.assertEqual(loaded[2], {0, 1})
                self.assertEqual(loaded[7], set())
                self.assertFalse(loaded[8])
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan

    def test_detail_lookup_accepts_explicit_book_context(self):
        old_name = novel_scan.clean_filename
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                results_dir = os.path.join(tmpdir, "results", "scan")
                os.makedirs(results_dir, exist_ok=True)
                book_a_path = os.path.join(results_dir, "甲书_detailed_20260608.json")
                book_b_path = os.path.join(results_dir, "乙书_detailed_20260608.json")
                with open(book_a_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "all_female_characters": {
                            "甲女": {"avg_score": 9, "count": 3},
                        },
                        "male_protagonist": {"name": "甲男"},
                    }, f, ensure_ascii=False)
                with open(book_b_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "all_female_characters": {
                            "乙女": {"avg_score": 9, "count": 3},
                        },
                        "male_protagonist": {"name": "乙男"},
                    }, f, ensure_ascii=False)

                novel_scan.clean_filename = "全局书名"
                novel_scan._ACTIVE_DETAIL_PATH = book_b_path

                self.assertEqual(
                    novel_scan._find_latest_detail_file(book_name="甲书", base_dir=tmpdir),
                    book_a_path,
                )
                heroines, male = novel_scan.find_heroines(
                    book_name="甲书",
                    base_dir=tmpdir,
                    use_global_active=False,
                )

                self.assertEqual(heroines, ["甲女"])
                self.assertEqual(male["name"], "甲男")
        finally:
            novel_scan.clean_filename = old_name
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_generate_report_accepts_explicit_book_name(self):
        old_name = novel_scan.clean_filename
        try:
            novel_scan.clean_filename = "全局书名"

            report_text = novel_scan.generate_report([], [], [], book_name="显式书名")

            self.assertIn("小说深度扫描报告：显式书名", report_text)
            self.assertNotIn("小说深度扫描报告：全局书名", report_text)
        finally:
            novel_scan.clean_filename = old_name

    def test_chunk_failure_diagnostic_flags_problematic_text(self):
        diagnostic = novel_scan._build_chunk_failure_diagnostic(
            "正常开头\x00异常控制\x1b字符\n" + ("很长" * 1100) + "\ufffd",
            err_msg="JSON parse failed",
            max_preview=40,
        )

        self.assertEqual(diagnostic["severity"], "high")
        self.assertIn("nul_bytes", diagnostic["flags"])
        self.assertIn("escape_chars", diagnostic["flags"])
        self.assertIn("replacement_chars", diagnostic["flags"])
        self.assertIn("very_long_lines", diagnostic["flags"])
        self.assertIn("\\x00", diagnostic["preview"])
        self.assertIn("\\x1b", diagnostic["preview"])
        self.assertEqual(diagnostic["error"], "JSON parse failed")

    def test_chunk_failure_diagnostic_keeps_normal_text_low_risk(self):
        diagnostic = novel_scan._build_chunk_failure_diagnostic("第一章\n甲女正常出场，男主开始行动。")

        self.assertEqual(diagnostic["severity"], "low")
        self.assertEqual(diagnostic["flags"], [])
        self.assertEqual(diagnostic["control_char_count"], 0)
        self.assertIn("甲女正常出场", diagnostic["preview"])

    def test_scan_checkpoint_records_and_clears_failure_diagnostics(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_diagnostics = dict(novel_scan.CHUNK_FAILURE_DIAGNOSTICS)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 2}
                novel_scan.CHUNK_SUMMARIES = {}
                novel_scan.CHUNK_FAILURE_DIAGNOSTICS = {}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                novel_scan.save_checkpoint(
                    [],
                    [],
                    {0},
                    [],
                    failed_chunks=set(),
                    current_chunk_idx=0,
                )
                novel_scan._commit_chunk_result(
                    1,
                    [],
                    [],
                    [],
                    "",
                    False,
                    "model parse failed",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks={0},
                    failed_chunks=set(),
                    chunk_text="第二块\x00含异常字符",
                )

                novel_scan.CHUNK_FAILURE_DIAGNOSTICS = {}
                loaded = novel_scan.load_checkpoint()
                self.assertEqual(loaded[2], {0})
                self.assertEqual(loaded[4], {1})
                self.assertIn(1, novel_scan.CHUNK_FAILURE_DIAGNOSTICS)
                self.assertIn("nul_bytes", novel_scan.CHUNK_FAILURE_DIAGNOSTICS[1]["flags"])

                novel_scan._commit_chunk_result(
                    1,
                    [{"type": "补扫成功", "chunk_index": 2}],
                    [],
                    [],
                    "第二块摘要",
                    True,
                    "",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks={0},
                    failed_chunks={1},
                    chunk_text="第二块正常文本",
                )

                novel_scan.CHUNK_FAILURE_DIAGNOSTICS = {}
                loaded = novel_scan.load_checkpoint()
                self.assertEqual(loaded[2], {0, 1})
                self.assertEqual(loaded[4], set())
                self.assertEqual(novel_scan.CHUNK_FAILURE_DIAGNOSTICS, {})
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan.CHUNK_FAILURE_DIAGNOSTICS = old_diagnostics
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_chronic_parse_failure_diagnostic_counts_retries(self):
        diagnostics = {}
        err_msg = "unable to parse json; response_flags=json_unbalanced,likely_truncated; response_len=6000"

        first = novel_scan._record_chunk_failure_diagnostic(
            2,
            "普通正文",
            err_msg=err_msg,
            chunk_failure_diagnostics=diagnostics,
        )
        second = novel_scan._record_chunk_failure_diagnostic(
            2,
            "普通正文",
            err_msg=err_msg,
            chunk_failure_diagnostics=diagnostics,
        )

        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(second["retry_count"], 2)
        self.assertTrue(novel_scan._is_chronic_parse_failure_diagnostic(diagnostics[2]))

    def test_chunk_failure_diagnostic_counts_repeated_api_and_timeout_failures(self):
        diagnostics = {}

        first = novel_scan._record_chunk_failure_diagnostic(
            4,
            "普通正文",
            err_msg="服务器错误(504)",
            chunk_failure_diagnostics=diagnostics,
        )
        second = novel_scan._record_chunk_failure_diagnostic(
            4,
            "普通正文",
            err_msg="gateway timeout 504",
            chunk_failure_diagnostics=diagnostics,
        )
        third = novel_scan._record_chunk_failure_diagnostic(
            4,
            "普通正文",
            err_msg="模型超时",
            chunk_failure_diagnostics=diagnostics,
        )
        fourth = novel_scan._record_chunk_failure_diagnostic(
            4,
            "普通正文",
            err_msg="模型超时",
            chunk_failure_diagnostics=diagnostics,
        )

        self.assertEqual(first["error_type"], "api_error")
        self.assertEqual(second["error_type"], "api_error")
        self.assertEqual(second["retry_count"], 2)
        self.assertEqual(third["error_type"], "timeout")
        self.assertEqual(third["retry_count"], 1)
        self.assertEqual(fourth["error_type"], "timeout")
        self.assertEqual(fourth["retry_count"], 2)

    def test_chunk_failure_diagnostic_does_not_count_generic_parse_as_chronic(self):
        diagnostics = {}

        first = novel_scan._record_chunk_failure_diagnostic(
            5,
            "普通正文",
            err_msg="parse failed",
            chunk_failure_diagnostics=diagnostics,
        )
        second = novel_scan._record_chunk_failure_diagnostic(
            5,
            "普通正文",
            err_msg="parse failed",
            chunk_failure_diagnostics=diagnostics,
        )

        self.assertEqual(first["error_type"], "parse_error")
        self.assertEqual(second["error_type"], "parse_error")
        self.assertEqual(second["retry_count"], 1)
        self.assertFalse(novel_scan._is_chronic_parse_failure_diagnostic(diagnostics[5]))

    def test_filter_chronic_parse_failures_keeps_other_failures(self):
        old_threshold = novel_scan.RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER
        try:
            novel_scan.RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER = 2
            diagnostics = {
                1: {
                    "retry_count": 2,
                    "error": "unable to parse json; response_flags=json_unbalanced,likely_truncated; response_len=6000",
                },
                2: {
                    "retry_count": 3,
                    "error": "network timeout",
                },
                3: {
                    "retry_count": 1,
                    "error": "unable to parse json; response_flags=json_unbalanced; response_len=6000",
                },
            }

            kept, skipped = novel_scan._filter_chronic_parse_failures([1, 2, 3, 4], diagnostics)

            self.assertEqual(kept, [2, 3, 4])
            self.assertEqual(skipped, [1])
        finally:
            novel_scan.RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER = old_threshold

    def test_commit_chunk_result_accepts_isolated_chunk_summaries(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 2}
                novel_scan.CHUNK_SUMMARIES = {99: "全局摘要"}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"
                local_summaries = {0: "本书前块摘要"}

                novel_scan._commit_chunk_result(
                    1,
                    [],
                    [],
                    [],
                    "本书第二块摘要",
                    True,
                    "",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks={0},
                    failed_chunks=set(),
                    chunk_summaries=local_summaries,
                )

                self.assertEqual(local_summaries, {0: "本书前块摘要", 1: "本书第二块摘要"})
                self.assertEqual(novel_scan.CHUNK_SUMMARIES, {99: "全局摘要"})
                with open(novel_scan.CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["chunk_summaries"], {
                    "0": "本书前块摘要",
                    "1": "本书第二块摘要",
                })
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_commit_chunk_result_accepts_isolated_failure_diagnostics(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_diagnostics = dict(novel_scan.CHUNK_FAILURE_DIAGNOSTICS)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_scan.CHECKPOINT_FILE = os.path.join(tmpdir, "latest_checkpoint.json")
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 2}
                novel_scan.CHUNK_SUMMARIES = {}
                novel_scan.CHUNK_FAILURE_DIAGNOSTICS = {99: {"flags": ["global"]}}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"
                local_diagnostics = {}

                novel_scan._commit_chunk_result(
                    1,
                    [],
                    [],
                    [],
                    "",
                    False,
                    "parse failed",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks={0},
                    failed_chunks=set(),
                    chunk_text="第二块\x00异常",
                    chunk_failure_diagnostics=local_diagnostics,
                )

                self.assertIn(1, local_diagnostics)
                self.assertIn("nul_bytes", local_diagnostics[1]["flags"])
                self.assertEqual(novel_scan.CHUNK_FAILURE_DIAGNOSTICS, {99: {"flags": ["global"]}})
                with open(novel_scan.CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertIn("1", data["chunk_failure_diagnostics"])

                novel_scan._commit_chunk_result(
                    1,
                    [{"type": "补扫成功", "chunk_index": 2}],
                    [],
                    [],
                    "第二块摘要",
                    True,
                    "",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks={0},
                    failed_chunks={1},
                    chunk_failure_diagnostics=local_diagnostics,
                )

                self.assertEqual(local_diagnostics, {})
                self.assertEqual(novel_scan.CHUNK_FAILURE_DIAGNOSTICS, {99: {"flags": ["global"]}})
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan.CHUNK_FAILURE_DIAGNOSTICS = old_diagnostics
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_commit_chunk_result_accepts_explicit_checkpoint_file(self):
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                global_checkpoint = os.path.join(tmpdir, "global_checkpoint.json")
                local_checkpoint = os.path.join(tmpdir, "local_checkpoint.json")
                novel_scan.CHECKPOINT_FILE = global_checkpoint
                novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 2}
                novel_scan.CHUNK_SUMMARIES = {}
                novel_scan._ACTIVE_DETAIL_PATH = "/tmp/detail.json"

                novel_scan._commit_chunk_result(
                    0,
                    [{"type": "命中", "chunk_index": 1}],
                    [],
                    [],
                    "第一块摘要",
                    True,
                    "",
                    all_issues=[],
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks=set(),
                    failed_chunks=set(),
                    checkpoint_file=local_checkpoint,
                )

                self.assertFalse(os.path.exists(global_checkpoint))
                self.assertTrue(os.path.exists(local_checkpoint))
                with open(local_checkpoint, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["processed_chunks"], [0])
                self.assertEqual(data["issues"][0]["type"], "命中")
        finally:
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_main_checkpoint_callbacks_keep_explicit_checkpoint_context(self):
        old_novel_path = novel_scan.NOVEL_FILE_PATH
        old_checkpoint = novel_scan.CHECKPOINT_FILE
        old_clean_filename = novel_scan.clean_filename
        old_output_dir = novel_scan.OUTPUT_DIR
        old_plan = novel_scan.CURRENT_CHUNK_PLAN_METADATA
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        old_diagnostics = dict(novel_scan.CHUNK_FAILURE_DIAGNOSTICS)
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        old_token_tracker = novel_scan.token_tracker
        old_enable_global_rescan = novel_scan.ENABLE_GLOBAL_RESCAN
        old_logger = novel_scan.logger
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                novel_path = os.path.join(tmpdir, "Book.txt")
                wrong_checkpoint = os.path.join(tmpdir, "wrong_checkpoint.json")
                wrong_output_dir = os.path.join(tmpdir, "wrong_output")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("甲女和男主同行。")

                def fake_initial_scan(**kwargs):
                    kwargs["processed_chunks"].add(0)
                    return None

                def fake_generate_profiles(_facts, _heroines, _male, checkpoint_callback=None):
                    novel_scan.CHECKPOINT_FILE = wrong_checkpoint
                    novel_scan.OUTPUT_DIR = wrong_output_dir
                    novel_scan.clean_filename = "WrongBook"
                    novel_scan.CURRENT_CHUNK_PLAN_METADATA = {"chunk_count": 99}
                    checkpoint_callback(heroine_profiles={"甲女": {"summary": "画像"}})
                    return {"甲女": {"summary": "画像"}}

                def fake_generate_report(_issues, _facts, _heroines, book_name=None):
                    return f"报告:{book_name}"

                novel_scan.ENABLE_GLOBAL_RESCAN = False
                novel_scan.token_tracker = None
                with mock.patch.object(novel_scan, "get_base_dir", return_value=tmpdir), \
                        mock.patch.object(novel_scan, "init_token_tracker"), \
                        mock.patch.object(novel_scan, "find_latest_scan_checkpoint", return_value=None), \
                        mock.patch.object(novel_scan, "load_rules", return_value=([{"name": "规则"}], [])), \
                        mock.patch.object(novel_scan, "find_heroines", return_value=(["甲女"], "男主")), \
                        mock.patch.object(novel_scan, "build_prompt", return_value="system prompt"), \
                        mock.patch.object(novel_scan, "build_chunk_manifest", return_value={
                            "version": 1,
                            "signature": "sig",
                            "chunks": [{"text": "甲女和男主同行。"}],
                        }), \
                        mock.patch.object(novel_scan, "_run_initial_thread_block_scan", side_effect=fake_initial_scan), \
                        mock.patch.object(novel_scan, "generate_heroine_profiles", side_effect=fake_generate_profiles), \
                        mock.patch.object(novel_scan, "_save_heroine_profiles_to_detail"), \
                        mock.patch.object(novel_scan, "_append_to_detail_file"), \
                        mock.patch.object(novel_scan, "generate_report", side_effect=fake_generate_report):
                    novel_scan.main(novel_path=novel_path, book_name="Book")

                self.assertFalse(os.path.exists(wrong_checkpoint))
                self.assertFalse(os.path.exists(os.path.join(wrong_output_dir, "raw_data.json")))
                self.assertFalse(os.path.exists(os.path.join(wrong_output_dir, "FULL_REPORT.txt")))
                scan_dirs = [
                    os.path.join(tmpdir, "results", name)
                    for name in os.listdir(os.path.join(tmpdir, "results"))
                    if name.startswith("Book_scan_")
                ]
                self.assertEqual(1, len(scan_dirs))
                checkpoint_file = os.path.join(scan_dirs[0], "latest_checkpoint.json")
                self.assertTrue(os.path.exists(checkpoint_file))
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["heroine_profiles"], {"甲女": {"summary": "画像"}})
                self.assertEqual(data["chunk_plan"]["chunk_count"], 1)
                with open(os.path.join(scan_dirs[0], "raw_data.json"), "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                self.assertEqual(raw_data["chunk_plan"]["chunk_count"], 1)
                with open(os.path.join(scan_dirs[0], "FULL_REPORT.txt"), "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), "报告:Book")
                scan_log_handlers = [
                    handler
                    for handler in novel_scan.logger.handlers
                    if isinstance(handler, RotatingFileHandler)
                ]
                self.assertEqual(1, len(scan_log_handlers))
                self.assertEqual(scan_log_handlers[0].baseFilename, os.path.join(scan_dirs[0], "scan.log"))
        finally:
            for handler in list(novel_scan.logger.handlers):
                handler.close()
                novel_scan.logger.removeHandler(handler)
            novel_scan.NOVEL_FILE_PATH = old_novel_path
            novel_scan.CHECKPOINT_FILE = old_checkpoint
            novel_scan.clean_filename = old_clean_filename
            novel_scan.OUTPUT_DIR = old_output_dir
            novel_scan.CURRENT_CHUNK_PLAN_METADATA = old_plan
            novel_scan.CHUNK_SUMMARIES = old_summaries
            novel_scan.CHUNK_FAILURE_DIAGNOSTICS = old_diagnostics
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path
            novel_scan.token_tracker = old_token_tracker
            novel_scan.ENABLE_GLOBAL_RESCAN = old_enable_global_rescan
            novel_scan.logger = old_logger

    def test_detail_writes_accept_explicit_detail_path(self):
        old_detail_path = getattr(novel_scan, "_ACTIVE_DETAIL_PATH", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                active_path = os.path.join(tmpdir, "Active_detailed_20260608.json")
                local_path = os.path.join(tmpdir, "Local_detailed_20260608.json")
                seed = {
                    "all_female_characters": {
                        "甲女": {
                            "avg_score": 0,
                            "count": 1,
                            "total_score": 0,
                            "other_names": [],
                            "summaries": [],
                            "features": [],
                            "relationships": [],
                            "interactions": [],
                            "emotion_signals": [],
                        }
                    }
                }
                for path in (active_path, local_path):
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(seed, f, ensure_ascii=False)
                novel_scan._ACTIVE_DETAIL_PATH = active_path

                novel_scan._save_heroine_profiles_to_detail(
                    {
                        "甲女": {
                            "report_summary": {
                                "identity": "目标女主",
                                "relationship_with_protagonist": "同行",
                            }
                        }
                    },
                    detail_path=local_path,
                )
                novel_scan._append_to_detail_file(
                    [
                        {
                            "name": "甲女",
                            "facts": {
                                "physical_contacts": [
                                    {
                                        "partner": "路人男",
                                        "is_male_lead": False,
                                        "contact_type": "拉扯",
                                        "detail": "被路人男拉住手腕",
                                        "evidence": "路人男拉住甲女手腕",
                                        "chunk_index": 1,
                                    }
                                ]
                            },
                        }
                    ],
                    [],
                    "男主",
                    detail_path=local_path,
                )

                with open(active_path, "r", encoding="utf-8") as f:
                    active_data = json.load(f)
                with open(local_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)

                active_entry = active_data["all_female_characters"]["甲女"]
                local_entry = local_data["all_female_characters"]["甲女"]
                self.assertNotIn("profile_for_report", active_entry)
                self.assertNotIn("non_male_male_interactions", active_entry)
                self.assertEqual(local_entry["profile_for_report"]["identity"], "目标女主")
                self.assertTrue(
                    any("路人男" in item for item in local_entry["non_male_male_interactions"])
                )
        finally:
            novel_scan._ACTIVE_DETAIL_PATH = old_detail_path

    def test_thread_block_accepts_isolated_middle_summary_state(self):
        old_middle_calls = novel_scan._middle_summary_calls
        old_max_middle = novel_scan.MAX_MIDDLE_SUMMARY_CALLS
        old_summaries = dict(novel_scan.CHUNK_SUMMARIES)
        try:
            novel_scan._middle_summary_calls = 7
            novel_scan.MAX_MIDDLE_SUMMARY_CALLS = 2
            novel_scan.CHUNK_SUMMARIES = {}
            local_summaries = {}
            local_state = {"calls": 0}
            processed_chunks = {0}
            failed_chunks = set()
            all_issues = []

            with mock.patch.object(novel_scan, "generate_context_summary", return_value="局部前情") as mock_summary, \
                    mock.patch.object(novel_scan, "scan_chunk") as mock_scan, \
                    mock.patch.object(novel_scan, "save_checkpoint"):
                mock_scan.side_effect = lambda _text, idx, _total, *_args, **_kwargs: (
                    [{"type": f"chunk-{idx}", "chunk_index": idx + 1}],
                    [],
                    [],
                    f"摘要{idx}",
                    True,
                    False,
                    "",
                )

                result = novel_scan._process_thread_block(
                    0,
                    [1, 2, 3],
                    ["第一块", "第二块", "第三块", "第四块"],
                    "system",
                    ["甲女"],
                    all_issues=all_issues,
                    all_heroine_facts=[],
                    extra_relations_all=[],
                    processed_chunks=processed_chunks,
                    failed_chunks=failed_chunks,
                    chunk_summaries=local_summaries,
                    middle_summary_state=local_state,
                )

            self.assertEqual(result["fatal_error"], "")
            self.assertEqual(local_state["calls"], 1)
            self.assertEqual(novel_scan._middle_summary_calls, 7)
            self.assertEqual(mock_summary.call_count, 1)
            self.assertIn(1, local_summaries)
            self.assertIn(2, local_summaries)
            self.assertIn(3, local_summaries)
            self.assertEqual(novel_scan.CHUNK_SUMMARIES, {})
        finally:
            novel_scan._middle_summary_calls = old_middle_calls
            novel_scan.MAX_MIDDLE_SUMMARY_CALLS = old_max_middle
            novel_scan.CHUNK_SUMMARIES = old_summaries

    def test_toxic_reviewer_prompt_locks_strict_harem_definitions(self):
        system_prompt, user_prompt = toxic_reviewer.build_review_prompts(
            {
                "category": "雷点（严重毒点）",
                "type": "绿帽",
                "content": "反派口嗨要把甲女抢走。",
                "reason": "疑似绿帽",
            },
            "绿帽定义",
            "男主",
            ["甲女", "乙女"],
        )
        joined = system_prompt + "\n" + user_prompt

        self.assertIn("已知女主/准女主名单：甲女、乙女", joined)
        self.assertIn("送女/绿帽锁定定义", joined)
        self.assertIn("高于占有欲泛化判断", joined)
        self.assertIn("对象是目标女主或强准女主", joined)
        self.assertIn("反派口嗨", joined)
        self.assertIn("男主睡女主亲友", joined)
        self.assertIn("反派计划把女性送人但男主未主动参与", joined)
        self.assertIn("不能仅因为“占有欲读者不适”就判 valid=true", joined)
        self.assertIn("缺少任一必要构成时必须判 invalid", joined)

        ntr_system_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "雷点（严重毒点）",
                "type": "NTR",
                "content": "旁人意淫甲女。",
            },
            "NTR定义",
            "男主",
            ["甲女"],
        )
        self.assertIn("送女/绿帽锁定定义", ntr_system_prompt)

        nt_system_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "雷点（严重毒点）",
                "type": "牛头人",
                "content": "旁人意淫甲女。",
            },
            "牛头人定义",
            "男主",
            ["甲女"],
        )
        self.assertIn("送女/绿帽锁定定义", nt_system_prompt)

    def test_toxic_reviewer_prompt_keeps_ntr_edge_as_general_issue(self):
        system_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "NTR擦边/反复救援",
                "content": "反派反复试图绑走甲女，但都被男主救下。",
            },
            "NTR擦边定义",
            "男主",
            ["甲女"],
        )

        self.assertIn("一般郁闷点/亵女类指控", system_prompt)
        self.assertNotIn("送女/绿帽锁定定义", system_prompt)

        nt_edge_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "牛头人擦边",
                "content": "反派威胁要抢走甲女。",
            },
            "牛头人擦边定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", nt_edge_prompt)

        green_edge_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "绿帽擦边",
                "content": "反派口嗨要抢走甲女。",
            },
            "绿帽擦边定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", green_edge_prompt)

        gift_attempt_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "送女未遂",
                "content": "家族试图安排甲女嫁人，男主阻止。",
            },
            "送女未遂定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", gift_attempt_prompt)

        suspicious_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "疑似绿帽",
                "content": "旁人传言甲女和路人男有暧昧。",
            },
            "疑似绿帽定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", suspicious_prompt)

        gift_suspect_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "送女嫌疑",
                "content": "有读者怀疑男主是否默许甲女联姻。",
            },
            "送女嫌疑定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", gift_suspect_prompt)

        ntr_review_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "NTR待复核",
                "content": "传闻甲女与路人男关系不明。",
            },
            "NTR待复核定义",
            "男主",
            ["甲女"],
        )
        self.assertNotIn("送女/绿帽锁定定义", ntr_review_prompt)

        for issue_type in ("绿帽传闻", "NTR口嗨", "牛头人弱暗示", "送女误会", "送女未来计划"):
            prompt, _ = toxic_reviewer.build_review_prompts(
                {
                    "category": "郁闷点",
                    "type": issue_type,
                    "content": "传闻或口嗨层面的关系风险，没有明确事实证据。",
                },
                f"{issue_type}定义",
                "男主",
                ["甲女"],
            )
            self.assertNotIn("送女/绿帽锁定定义", prompt)

    def test_toxic_reviewer_prompt_keeps_general_issue_review_short(self):
        system_prompt, _ = toxic_reviewer.build_review_prompts(
            {
                "category": "郁闷点",
                "type": "亵女",
                "content": "路人言语调戏甲女。",
            },
            "亵女定义",
            "男主",
            ["甲女"],
        )

        self.assertIn("一般郁闷点/亵女类指控", system_prompt)
        self.assertNotIn("送女/绿帽锁定定义", system_prompt)

    def test_physical_contact_postprocess_without_partner_relations(self):
        facts = [
            {
                "name": "甲女",
                "facts": {
                    "physical_contacts": [
                        {
                            "partner": "王公子",
                            "contact_type": "强抱",
                            "detail": "王公子强行抱住甲女。",
                            "evidence": "王公子强行抱住甲女。",
                        }
                    ]
                },
            }
        ]

        cleaned = novel_scan._postprocess_heroine_facts(facts, ["甲女"], {"name": "男主"}, 826)

        contacts = cleaned[0]["facts"]["physical_contacts"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["partner"], "王公子")
        self.assertEqual(contacts[0]["chunk_index"], 826)

    def test_extended_heroine_facts_survive_postprocess_and_merge(self):
        facts = [
            {
                "name": "甲女",
                "facts": {
                    "economic_attachments": [
                        {"benefactor": "王公子", "relationship": "债务", "detail": "欠债被迫依附", "evidence": "甲女因欠债被王公子控制。"}
                    ],
                    "power_relations": [
                        {"superior": "宗主", "relationship": "师徒", "has_abuse": True, "detail": "宗主以师命压迫", "evidence": "宗主以师命压迫甲女。"}
                    ],
                    "political_marriages": [
                        {"partner": "世子", "type": "和亲", "status": "planned", "forced": True, "has_consummation": False, "evidence": "甲女被安排与世子和亲。"}
                    ],
                    "victim_records": [
                        {"perpetrator": "反派", "type": "下药", "outcome": "未遂", "rescued_by": "男主", "evidence": "反派给甲女下药未遂。"}
                    ],
                },
            }
        ]

        cleaned = novel_scan._postprocess_heroine_facts(facts, ["甲女"], {"name": "男主"}, 827)
        merged = novel_scan.merge_heroine_facts_by_name(cleaned, heroine_names=["甲女"])

        for dim in ["economic_attachments", "power_relations", "political_marriages", "victim_records"]:
            self.assertEqual(len(cleaned[0]["facts"][dim]), 1)
            self.assertEqual(cleaned[0]["facts"][dim][0]["chunk_index"], 827)
            self.assertEqual(len(merged["甲女"][dim]), 1)

    def test_rebuild_leak_state_exposes_three_layers(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "甲女",
                        "aliases": ["阿甲"],
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={"甲女": (False, "未见推倒或同房证据")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(len(issues), 1)
        info = leak_map["甲女"]
        self.assertTrue(info["is_leak_heroine"])
        self.assertTrue(info["leak_emotional_depth"])
        self.assertFalse(info["leak_relationship_confirmed"])
        self.assertFalse(info["leak_ending_accounted"])
        self.assertIn("喜欢", info["leak_emotional_depth_reason"])

    def test_rebuild_leak_state_requires_emotional_depth(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": ["偶尔出场，负责送情报和解释背景，结局未交代。"],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={"乙女": (False, "未见推倒或同房证据")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(issues, [])
        info = leak_map["乙女"]
        self.assertFalse(info["is_leak_heroine"])
        self.assertFalse(info["leak_emotional_depth"])
        self.assertFalse(info["leak_relationship_confirmed"])
        self.assertFalse(info["leak_ending_accounted"])
        self.assertIn("未达到漏女判定门槛", info["leak_reason"])

    def test_rebuild_leak_state_ignores_negated_emotional_depth(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "与男主没有暧昧，没有喜欢，只是偶尔送情报。",
                            "没有感情线，但被读者调侃像未婚妻。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])

    def test_rebuild_leak_state_keeps_romance_depth_when_relationship_unconfirmed(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "与男主长期暧昧并喜欢男主，但未确认关系。",
                            "结局未交代归宿，也没有收入后宫。",
                        ],
                    },
                    {
                        "name": "丙女",
                        "summaries": [
                            "与男主没有暧昧，只是任务搭档。",
                            "未确认关系，也没有感情线。",
                        ],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "丙女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "丙女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["乙女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertTrue(leak_map["乙女"]["is_leak_heroine"])
        self.assertTrue(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("命中情感/亲密关键词", leak_map["乙女"]["leak_emotional_depth_reason"])
        self.assertFalse(leak_map["丙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["丙女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_ignores_meta_popularity_emotion(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "读者喜欢她的吐槽，但她只是探案助手，结局未交代。",
                            "作者偏爱她，经常给她镜头。",
                            "人气很高，粉丝爱看她出场，但她只负责解释背景。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["她喜欢男主并长期暧昧，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])
        self.assertTrue(leak_map["甲女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_ignores_hobby_emotion_words(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "她爱吃点心，经常负责厨房笑料。",
                            "她喜欢破案也热爱推理，主要作用是提供线索。",
                            "她倾心研究蒸汽机械，负责解释设定。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["她喜欢男主并长期暧昧，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])
        self.assertTrue(leak_map["甲女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_ignores_roleplay_emotion_words(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "她假装表白以套取情报，任务结束后澄清。",
                            "她念出告白台词，只是在排练舞台剧。",
                            "她扮演恋人潜入宴会，行动结束后恢复任务伙伴关系。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["她向男主表白并长期暧昧，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])
        self.assertTrue(leak_map["甲女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_ignores_familial_emotion_words(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "她与男主像兄妹一样亲密，主要是亲情和战友情。",
                            "她把男主当弟弟照顾，爱护后辈，没有恋爱线。",
                            "她是男主姐姐，家人式陪伴，结局未交代。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["她与男主亲密暧昧并喜欢男主，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])
        self.assertTrue(leak_map["甲女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_ignores_physical_event_without_romance_depth(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "乙女",
                        "summaries": [
                            "她传授男主双修之法并实践，但完全没有任何感情描写。",
                            "两人只有一次功法双修事件，没有恋爱线，没有暧昧推进。",
                            "她和男主有亲密接触记录，但没有感情戏也未确认后宫关系。",
                        ],
                    },
                    {
                        "name": "甲女",
                        "summaries": ["她喜欢男主并长期暧昧，但结局未交代归宿。"],
                    },
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["乙女", "甲女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={
                    "乙女": (False, "未见推倒或同房证据"),
                    "甲女": (False, "未见推倒或同房证据"),
                },
            )
        finally:
            os.unlink(char_path)

        self.assertEqual([issue["content"] for issue in issues], ["甲女 未被男主明确推倒，且尾声未明确交代结局"])
        self.assertFalse(leak_map["乙女"]["is_leak_heroine"])
        self.assertFalse(leak_map["乙女"]["leak_emotional_depth"])
        self.assertIn("未达到漏女判定门槛", leak_map["乙女"]["leak_reason"])
        self.assertTrue(leak_map["甲女"]["is_leak_heroine"])
        self.assertTrue(leak_map["甲女"]["leak_emotional_depth"])

    def test_rebuild_leak_state_keeps_unknown_relationship_unjudged(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "丙女",
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["丙女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={"丙女": (None, "API失败，未能确认是否推倒")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(issues, [])
        info = leak_map["丙女"]
        self.assertFalse(info["is_leak_heroine"])
        self.assertTrue(info["leak_emotional_depth"])
        self.assertIsNone(info["leak_relationship_confirmed"])
        self.assertFalse(info["leak_ending_accounted"])
        self.assertIn("关系确认未知", info["leak_reason"])

    def test_rebuild_leak_state_rechecks_nominal_pushed_confirmation(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "丙女",
                        "summaries": [
                            "与男主长期暧昧并喜欢男主。",
                            "两人只是名义夫妻，有名无实，未同房也未圆房，未确认关系。",
                        ],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["丙女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={"丙女": (True, "名义夫妻，有名无实，未同房，未确认关系。")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(issues, [])
        info = leak_map["丙女"]
        self.assertFalse(info["is_leak_heroine"])
        self.assertIsNone(info["leak_relationship_confirmed"])
        self.assertIn("关系确认未知", info["leak_reason"])
        self.assertIn("非实质确认语境", info["leak_relationship_reason"])

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            _, confirmed_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["丙女"],
                char_file_path=char_path,
                novel_tail="尾声里只有男主离开江湖。",
                finished=True,
                pushed_map={"丙女": (True, "后续明确同房并确认关系。")},
            )
        finally:
            os.unlink(char_path)

        confirmed = confirmed_map["丙女"]
        self.assertTrue(confirmed["leak_relationship_confirmed"])
        self.assertIn("已被男主明确推倒", confirmed["leak_reason"])
        self.assertFalse(
            novel_reviewer._contains_positive_phrase_for_leak_confirmation(
                "名义夫妻，有名无实，未同房，未确认关系。",
                ("同房", "确认关系"),
            )
        )
        self.assertFalse(
            novel_reviewer._contains_positive_phrase_for_leak_confirmation(
                "两人没有发生关系。",
                ("发生关系",),
            )
        )
        self.assertTrue(
            novel_reviewer._contains_positive_phrase_for_leak_confirmation(
                "后续明确同房并确认关系。",
                ("同房", "确认关系"),
            )
        )

    def test_rebuild_leak_state_requires_explicit_ending_account(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "丁女",
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["丁女"],
                char_file_path=char_path,
                novel_tail="尾声里男主偶然想起丁女的名字，随后独自离开江湖。",
                finished=True,
                pushed_map={"丁女": (False, "未见推倒或同房证据")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(len(issues), 1)
        info = leak_map["丁女"]
        self.assertTrue(info["is_leak_heroine"])
        self.assertFalse(info["leak_ending_accounted"])
        self.assertIn("缺少归宿", info["leak_ending_reason"])

    def test_rebuild_leak_state_ignores_tail_name_list_as_ending_account(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "丁女",
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["丁女"],
                char_file_path=char_path,
                novel_tail="结局里男主整理旧名单时看到丁女的名字，随后独自离开江湖。",
                finished=True,
                pushed_map={"丁女": (False, "未见推倒或同房证据")},
            )
        finally:
            os.unlink(char_path)

        self.assertEqual(len(issues), 1)
        info = leak_map["丁女"]
        self.assertTrue(info["is_leak_heroine"])
        self.assertFalse(info["leak_ending_accounted"])
        self.assertIn("可能只是提及", info["leak_ending_reason"])

    def test_rebuild_leak_state_ignores_negated_tail_ending_account(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "丁女",
                        "summaries": ["与男主长期暧昧并喜欢男主，但结局未交代归宿。"],
                    }
                ]
            }
        }
        negated_tails = [
            "尾声提到丁女未交代归宿，男主独自离开江湖。",
            "尾声里丁女没有留在男主身边，也没有去向说明。",
            "尾声说明丁女最终没有同行，归宿不明。",
            "尾声里丁女不知去了哪里，男主独自离开江湖。",
            "结局中丁女去向未知，再无音讯。",
            "多年后众人不知道丁女的去处，只偶尔提起她。",
            "尾声里男主梦见丁女死亡，醒来后发现只是幻境。",
            "结局传闻丁女战死，但后来证实是假消息。",
            "尾声里丁女讨论墓葬制度，并未交代归宿。",
            "结局里丁女留下线索后再未出现，男主独自离开江湖。",
            "尾声只提到丁女留下遗物和传说，没有说明她的归宿。",
            "番外中丁女跟随案件线索继续调查，正文没有交代去向。",
        ]
        for tail in negated_tails:
            with self.subTest(tail=tail):
                with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                    char_path = f.name
                try:
                    issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                        female_leads=["丁女"],
                        char_file_path=char_path,
                        novel_tail=tail,
                        finished=True,
                        pushed_map={"丁女": (False, "未见推倒或同房证据")},
                    )
                finally:
                    os.unlink(char_path)

                self.assertEqual(len(issues), 1)
                info = leak_map["丁女"]
                self.assertTrue(info["is_leak_heroine"])
                self.assertFalse(info["leak_ending_accounted"])
                self.assertIn("缺少归宿", info["leak_ending_reason"])

    def test_rebuild_leak_state_accepts_explicit_ending_account(self):
        data = {
            "heroine_result": {
                "heroines": [
                    {
                        "name": "戊女",
                        "aliases": ["阿戊"],
                        "summaries": ["与男主长期暧昧并喜欢男主，但正文未确认推倒。"],
                    }
                ]
            }
        }
        explicit_tails = [
            "番外多年后，阿戊留在男主身边，与他一起回到府中相伴余生。",
            "番外多年后，阿戊留下遗物后又战死牺牲，最终葬在宗门后山。",
        ]
        for tail in explicit_tails:
            with self.subTest(tail=tail):
                with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                    char_path = f.name
                try:
                    issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                        female_leads=["戊女"],
                        char_file_path=char_path,
                        novel_tail=tail,
                        finished=True,
                        pushed_map={"戊女": (False, "未见推倒或同房证据")},
                    )
                finally:
                    os.unlink(char_path)

                self.assertEqual(issues, [])
                info = leak_map["戊女"]
                self.assertFalse(info["is_leak_heroine"])
                self.assertTrue(info["leak_ending_accounted"])
                self.assertIn("明确结局交代", info["leak_ending_reason"])

    def test_auto_profile_inference(self):
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("大明权臣", "皇帝与朝廷在庙堂上争论边军粮饷。"),
            "history",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("锦衣卫变法", "现代人穿越古代，卷入宦官、士族、门阀与清君侧风波。"),
            "history",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("星际远征", "星舰启动曲率引擎，人工智能计算虫洞航道。"),
            "hard_sci_fi",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("意识上传", "可控核聚变、脑机接口和太空电梯改变文明等级，引发费米悖论讨论。"),
            "hard_sci_fi",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("仙路后宫", "男主与道侣双修，红颜和未婚妻都卷入宗门风波。"),
            "harem",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("多女主日常", "男主收女推倒，和多女主大被同眠，后宫关系持续推进。"),
            "harem",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("剑修飞升", "宗门弟子修炼金丹元婴，进入秘境夺取法宝传承。"),
            "xianxia_fantasy",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("洪荒圣体", "主角练气筑基后觉醒圣体武魂，在天庭封神大劫中求道长生。"),
            "xianxia_fantasy",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("密室谋杀", "侦探调查案件，嫌疑人没有不在场证明，线索指向真正凶手。"),
            "mystery_detective",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("本格诡计", "暴风雪山庄里出现叙述性诡计、红鲱鱼和时刻表诡计，侦探给出多重解答。"),
            "mystery_detective",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("无限副本", "主神发布任务，玩家查看系统面板、技能和装备奖励。"),
            "game_system",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("诸天模拟器", "主神空间开启诸天穿梭，角色用签到加点和NPC兑换能力。"),
            "game_system",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("人生模拟器", "主角通过人生模拟和未来推演获得模拟结果，结算奖励里保留天赋词条。"),
            "simulator",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("国运擂台", "龙国选手参加文明试炼，召唤先贤和历史武将在神话擂台赢得国运奖励。"),
            "nation_fate",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("幕后黑手", "主角隐藏身份暗中操控多方势力，用多马甲和信息差布局，最后掉马揭面。"),
            "mastermind_hidden",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("都市神医", "赘婿神医在豪门集团商战中扮猪吃虎，连续打脸反派。"),
            "urban_power",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("神豪下山", "都市神豪靠系统签到、古武和龙王身份连续打脸豪门反派。"),
            "urban_power",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("钢铁战役", "将军调度军团步兵火炮，依靠后勤补给完成战略包围。"),
            "military_war",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("军营演习", "主角参军入伍，在军校和兵工厂推进军工装备，参与特种兵演习。"),
            "military_war",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("兵王归队", "兵王回到军营后带领部队演习，依靠战区指挥和后勤补给完成任务。"),
            "military_war",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("军阀争霸", "近代军阀扩编军团，建设军备和兵工厂，用火炮步兵争夺战局。"),
            "military_war",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("末世安全区", "丧尸病毒爆发后，幸存者搜集物资并建设避难所。"),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("灾变基地", "末日秩序崩塌后，基地里人性和异能觉醒共同影响求生规则。"),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("废土迁徙", "幸存者在辐射废土建立据点，搜寻资源和物资，抵御感染者与兽潮。"),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("诡秘档案", "调查员追查克苏鲁仪式，理智受到污染，怪谈规则逐步显露。"),
            "cosmic_horror",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("规则怪谈", "规则怪谈要求遵守隐藏规则，违反规则会被纸人替换，必须找到祠堂里的通关规则逃生。"),
            "chinese_weird",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("旧日规则", "序列和魔药体系带来SAN值下降，精神污染引出旧日外神。"),
            "cosmic_horror",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("冠军教练", "足球俱乐部在联赛决赛中调整战术，球员训练后夺得冠军。"),
            "sports_competition",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("奥运MVP", "篮球队在世界杯和奥运会决赛对阵宿敌，教练调整专业技战术。"),
            "sports_competition",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("顶流归来", "娱乐圈顶流进剧组拍戏，经纪人处理热搜黑粉和公关危机。"),
            "entertainment_industry",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("选秀明星", "练习生参加选秀，网红和MCN制造饭圈CP营销，最终热搜出圈。"),
            "entertainment_industry",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("创业时代", "公司完成融资，董事会讨论股权、现金流和市场份额。"),
            "business_career",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("供应链商战", "合伙人围绕股权、供应链、产业链、裁员和竞业展开职场斗争。"),
            "business_career",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("法医现场", "刑警在案发现场勘查，法医尸检后串起证据链锁定嫌疑人。"),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("专案组", "刑侦专案组通过痕检、技侦、网安和犯罪心理分析侦查连环案。"),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("禁毒卧底", "刑警卧底接近毒贩，依靠线人和专案组追捕内鬼，扫黑禁毒行动收网。"),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("同桌的你", "高中转学生和同桌参加月考竞赛，班主任关注高考压力。"),
            "campus_youth",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("重生校园", "学霸复读后参加艺考，和同桌早恋，毕业季面对原生家庭压力。"),
            "campus_youth",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("领地种田记", "主角经营领地，开垦农田和作坊，通过贸易建设城镇。"),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("修仙种田", "宗门建设灵田药园，依靠科技树、产业链、人口和民生推进基建。"),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("凡人开店", "凡人在城镇经营餐厅，联合商会和银行扩张供应链，推动标准化产业升级。"),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("转生勇者", "主角转生异世界，加入冒险者公会，在地下城挑战魔王。"),
            "isekai_lightnovel",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("异世界慢生活", "贵族在王国经营料理店，精灵、龙族和亚人围绕美食展开冒险。"),
            "isekai_lightnovel",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("地下城技能书", "转生者在冒险者公会学习技能，依靠职业等级和魔法挑战地下城魔王。"),
            "isekai_lightnovel",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("蒸汽炼金侦探", "蒸汽时代的教会帝国里，炼金矩阵和差分机卷入神秘复苏案件。"),
            "steampunk_fantasy",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("小镇旧事", "他回到故乡，重新面对童年的朋友。"),
            "general",
        )

    def test_auto_profile_candidates_keep_mixed_genres(self):
        candidates = analysis_profiles.infer_profile_candidates_for_text(
            "大明后宫",
            "皇帝与朝廷在庙堂上争论边军粮饷，男主和道侣、红颜都卷入纳妾风波。",
        )
        names = [item["name"] for item in candidates]

        self.assertIn("history", names)
        self.assertIn("harem", names)
        self.assertGreater(candidates[0]["score"], 0)
        self.assertTrue(candidates[0]["matched_keywords"])
        self.assertEqual(candidates[0]["rank"], 1)
        self.assertIn("auto_selected", candidates[0])

        title_weighted = analysis_profiles.infer_profile_candidates_for_text("篮球冠军", "训练和战术很重要。")
        self.assertEqual(title_weighted[0]["name"], "sports_competition")
        self.assertGreaterEqual(title_weighted[0]["matched_keywords"].count("篮球"), 1)

        steampunk = analysis_profiles.infer_profile_candidates_for_text(
            "蒸汽朋克西幻",
            "教会帝国里，差分机接入炼金矩阵，蒸汽机械卷入神秘复苏案件。",
        )
        self.assertEqual(steampunk[0]["name"], "steampunk_fantasy")
        self.assertIn("组合:炼金+蒸汽", steampunk[0]["matched_keywords"])
        self.assertIn("组合:蒸汽朋克+西幻", steampunk[0]["matched_keywords"])
        self.assertIn("组合:差分机+炼金矩阵+蒸汽", steampunk[0]["matched_keywords"])

    def test_auto_profile_novel_reads_timeline_samples(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
            path = f.name
            f.write("小镇日常与朋友重逢。\n" * 4000)
            f.write("星舰进入虫洞，曲率引擎启动，量子通讯和太空殖民成为主线。\n" * 80)
        try:
            candidates = analysis_profiles.infer_profile_candidates_for_novel(path, "慢热长篇")
            names = [item["name"] for item in candidates]
            self.assertIn("hard_sci_fi", names)
            sampled = analysis_profiles._read_text_timeline_samples_safely(path)
            self.assertIn("__sample_head__", sampled)
            self.assertIn("__sample_tail__", sampled)
            self.assertIn("曲率引擎", sampled)
        finally:
            os.remove(path)

    def test_auto_profile_multi_label_inference(self):
        profiles = analysis_profiles.infer_profiles_for_text(
            "大明星际后宫",
            "皇帝与朝廷在庙堂争论边军粮饷，男主和道侣双修，又驾驶星舰启动曲率引擎穿过虫洞。",
        )

        self.assertIn("history", profiles)
        self.assertIn("harem", profiles)
        self.assertIn("hard_sci_fi", profiles)
        self.assertLessEqual(len(profiles), analysis_profiles.AUTO_PROFILE_MAX_PROFILES)

        crossed = analysis_profiles.infer_profiles_for_text(
            "末世诡秘娱乐圈",
            "丧尸病毒爆发后，顶流在避难所拍摄综艺，调查员发现克苏鲁仪式和污染规则。",
        )
        self.assertIn("apocalypse_survival", crossed)
        self.assertIn("cosmic_horror", crossed)
        self.assertIn("entertainment_industry", crossed)

        mixed = analysis_profiles.infer_profiles_for_text(
            "异世界种田创业",
            "主角转生异世界，在领地经营农田和作坊，靠贸易融资扩张公司。",
        )
        self.assertIn("isekai_lightnovel", mixed)
        self.assertIn("farming_management", mixed)
        self.assertIn("business_career", mixed)

        kimi_mixed = analysis_profiles.infer_profiles_for_text(
            "末世之修仙系统",
            "丧尸爆发后主角觉醒系统面板，建立基地，吸收晶核进化，又得到灵根开始筑基修仙。",
        )
        self.assertIn("apocalypse_survival", kimi_mixed)
        self.assertIn("game_system", kimi_mixed)
        self.assertIn("xianxia_fantasy", kimi_mixed)

        history_farming = analysis_profiles.infer_profiles_for_text(
            "三国之种田霸业",
            "主角穿越大汉乱世，在诸侯之间屯田修路，经营农田和作坊，建设城池。",
        )
        self.assertIn("history", history_farming)
        self.assertIn("farming_management", history_farming)

        isekai_entertainment = analysis_profiles.infer_profiles_for_text(
            "转生之我在娱乐圈当影后",
            "女主转生异世界后进入娱乐圈，参加选秀成为影后，经营粉丝和热搜。",
        )
        self.assertIn("isekai_lightnovel", isekai_entertainment)
        self.assertIn("entertainment_industry", isekai_entertainment)

        nation_simulator = analysis_profiles.infer_profiles_for_text(
            "国运人生模拟",
            "主角绑定国运后进入文明试炼，用人生模拟器推演未来，靠模拟结果选择召唤先贤路线。",
        )
        self.assertIn("nation_fate", nation_simulator)
        self.assertIn("simulator", nation_simulator)

        mastermind_mystery = analysis_profiles.infer_profiles_for_text(
            "马甲侦探幕后流",
            "侦探表面调查密室案件，真实身份是幕后黑手，靠多马甲和信息差操控专案组与各方势力。",
        )
        self.assertIn("mastermind_hidden", mastermind_mystery)
        self.assertIn("mystery_detective", mastermind_mystery)

        self.assertEqual(
            analysis_profiles.infer_profiles_for_text("小镇旧事", "他回到故乡，重新面对童年的朋友。"),
            ["general"],
        )
        general_candidates = analysis_profiles.infer_profile_candidates_for_text("小镇旧事", "他回到故乡。")
        self.assertEqual(general_candidates[0]["rank"], 1)
        self.assertTrue(general_candidates[0]["auto_selected"])

    def test_auto_profile_negative_keywords_reduce_cross_category_noise(self):
        basketball = analysis_profiles.infer_profile_candidates_for_text(
            "篮球系统训练营",
            "高中篮球队参加联赛，教练安排战术训练，主角冲击冠军。",
        )
        basketball_names = [item["name"] for item in basketball]
        self.assertEqual(basketball_names[0], "sports_competition")
        self.assertNotIn("game_system", analysis_profiles.infer_profiles_for_text(
            "篮球系统训练营",
            "高中篮球队参加联赛，教练安排战术训练，主角冲击冠军。",
        ))

        entertainment = analysis_profiles.infer_profile_candidates_for_text(
            "影后经营游戏",
            "娱乐圈影后进入剧组拍戏，导演安排综艺宣发，经纪人处理热搜和饭圈。",
        )
        self.assertEqual(entertainment[0]["name"], "entertainment_industry")
        self.assertNotIn("farming_management", [item["name"] for item in entertainment[:3]])

        business_system = analysis_profiles.infer_profile_candidates_for_text(
            "商战系统创业史",
            "主角经营公司融资上市，董事会斗争，供应链和产品研发是主线，系统只提供经营面板。",
        )
        self.assertEqual(business_system[0]["name"], "business_career")
        self.assertNotIn("urban_power", [item["name"] for item in business_system[:3]])

        military_king = analysis_profiles.infer_profile_candidates_for_text(
            "兵王战神演习",
            "退役兵王回到军营参加演习，战区指挥、补给、火炮和特战小队行动是主线。",
        )
        self.assertEqual(military_king[0]["name"], "military_war")
        self.assertNotIn("urban_power", [item["name"] for item in military_king[:3]])

        urban_dragon_king = analysis_profiles.infer_profile_candidates_for_text(
            "下山龙王神医",
            "高手下山成为赘婿，神医救人，豪门羞辱，龙王身份揭露后连续打脸。",
        )
        self.assertEqual(urban_dragon_king[0]["name"], "urban_power")

        entertainment_behind = analysis_profiles.infer_profile_candidates_for_text(
            "顶流幕后花絮",
            "娱乐圈剧组发布幕后花絮，导演带演员宣传综艺和选秀热搜。",
        )
        self.assertEqual(entertainment_behind[0]["name"], "entertainment_industry")
        self.assertNotIn("mastermind_hidden", [item["name"] for item in entertainment_behind[:3]])

    def test_auto_profile_filters_context_pollution_and_counts_frequency(self):
        operating_system = analysis_profiles.infer_profile_candidates_for_text(
            "操作系统教程",
            "本书讲解计算机操作系统、文件系统、系统调用和系统化工程管理。",
        )
        operating_names = [item["name"] for item in operating_system]
        self.assertNotIn("game_system", operating_names)
        self.assertNotIn("urban_power", operating_names)

        fourth_disaster = analysis_profiles.infer_profile_candidates_for_text(
            "第四天灾篮球队",
            "玩家作为第四天灾参加篮球联赛，教练安排战术训练。",
        )
        self.assertEqual(fourth_disaster[0]["name"], "sports_competition")
        self.assertNotIn("apocalypse_survival", [item["name"] for item in fourth_disaster[:3]])

        repeated = analysis_profiles.infer_profile_candidates_for_text(
            "系统副本",
            "系统面板显示系统任务，进入副本后继续获得系统奖励。",
        )
        game_score = next(item["score"] for item in repeated if item["name"] == "game_system")
        single = analysis_profiles.infer_profile_candidates_for_text("系统副本", "系统面板显示任务。")
        single_score = next(item["score"] for item in single if item["name"] == "game_system")
        self.assertGreater(game_score, single_score)

        self.assertEqual(
            analysis_profiles.infer_profiles_for_text("单系统日常", "主角获得系统，但没有面板、副本或任务规则。"),
            ["general"],
        )
        self.assertEqual(
            analysis_profiles.infer_profiles_for_text("普通布局", "角色讨论工作布局和团队协作，没有隐藏身份、马甲或幕后操控。"),
            ["general"],
        )

    def test_auto_profile_confidence_is_calibrated_by_score_margin_and_evidence(self):
        title_only = analysis_profiles.infer_profile_candidates_for_text("大明1937", "")
        self.assertEqual(title_only[0]["name"], "history")
        self.assertLess(title_only[0]["confidence"], 0.75)
        self.assertEqual(title_only[0]["confidence_level"], "medium")

        mixed = analysis_profiles.infer_profile_candidates_for_text(
            "末世之修仙系统",
            "丧尸病毒爆发后，主角获得系统面板，在基地外搜集晶核，同时修仙升级对抗异兽。",
        )
        apocalypse = next(item for item in mixed if item["name"] == "apocalypse_survival")
        game_system = next(item for item in mixed if item["name"] == "game_system")
        self.assertEqual(apocalypse["confidence_level"], "high")
        self.assertGreater(apocalypse["confidence"], 0.75)
        self.assertEqual(game_system["confidence_level"], "medium")
        self.assertLess(game_system["confidence"], apocalypse["confidence"])

    def test_kimi_recommended_auto_profile_boundary_samples(self):
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "诡秘之主",
                "序列魔药扮演法带来精神污染，主角面对旧日和外神，理智值不断下降。",
            ),
            "cosmic_horror",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "规则怪谈：我能完美利用规则",
                "规则怪谈要求遵守隐藏规则，违反规则会被纸人追杀，祠堂村规和红白喜事暗示通关规则。",
            ),
            "chinese_weird",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "模因收容档案",
                "调查员窥视深渊，听见旧日呢喃后锚点失效，模因污染导致认知崩溃和失控。",
            ),
            "cosmic_horror",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "中式诡异：祠堂守则",
                "村规写着夜里不能回头，纸人会替换违规者，红白喜事和阴婚禁忌构成怪谈副本。",
            ),
            "chinese_weird",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "幕后流：我有一千个马甲",
                "主角隐藏身份组建秘密组织，用多马甲暗中操控局势，靠信息差和多方博弈完成幕后排局，最终掉马。",
            ),
            "mastermind_hidden",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "极寒废土",
                "冰封天灾后，幸存者迁徙到安全区，搜集物资并重建领地秩序。",
            ),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "晶核进化",
                "丧尸病毒导致感染者变异，主角靠晶核进化异能，在基地外对抗异兽和虫族。",
            ),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "大奉打更人探案",
                "主角调查密室案件，通过线索、动机和诡计推理真相，没有法医程序。",
            ),
            "mystery_detective",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "冷案追凶",
                "调查组重启冷案，审讯室里核对凶器、弹道、指纹和DNA，最终让通缉凶手落网。",
            ),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "法医档案",
                "法医通过毒理、尸检和现场勘查还原作案过程，犯罪心理侧写锁定连环杀人嫌疑人。",
            ),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "全职高手",
                "电竞职业赛季开始，战队训练，季后赛决赛中选手完成翻盘。",
            ),
            "sports_competition",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "围棋冠军",
                "围棋选手在世锦赛和亚运会连续比赛，逆风对决后刷新纪录夺得金牌。",
            ),
            "sports_competition",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "格斗之王",
                "拳击和格斗训练强调体能、技术动作、伤病管理，决赛最后一回合绝杀翻盘。",
            ),
            "sports_competition",
        )

        horror_keywords = dict(analysis_profiles._keywords_from_manifest("cosmic_horror"))
        self.assertEqual(horror_keywords.get("扮演法"), 5)
        self.assertEqual(horror_keywords.get("收容物"), 5)
        self.assertEqual(horror_keywords.get("认知崩溃"), 5)
        self.assertEqual(horror_keywords.get("理智"), 5)
        self.assertEqual(horror_keywords.get("魔药"), 4)

        apocalypse_keywords = dict(analysis_profiles._keywords_from_manifest("apocalypse_survival"))
        self.assertEqual(apocalypse_keywords.get("废土"), 5)
        self.assertEqual(apocalypse_keywords.get("幸存者"), 5)
        self.assertEqual(apocalypse_keywords.get("晶核"), 5)
        self.assertEqual(apocalypse_keywords.get("物资"), 4)

        crime_keywords = dict(analysis_profiles._keywords_from_manifest("crime_forensics"))
        self.assertEqual(crime_keywords.get("破案"), 5)
        self.assertEqual(crime_keywords.get("侧写"), 5)
        self.assertEqual(crime_keywords.get("禁毒"), 5)
        self.assertEqual(crime_keywords.get("DNA"), 4)

        military_keywords = dict(analysis_profiles._keywords_from_manifest("military_war"))
        self.assertEqual(military_keywords.get("兵王"), 4)
        self.assertEqual(military_keywords.get("军阀"), 4)
        self.assertEqual(military_keywords.get("战争"), 4)
        self.assertEqual(military_keywords.get("后勤"), 4)

        sports_keywords = dict(analysis_profiles._keywords_from_manifest("sports_competition"))
        self.assertEqual(sports_keywords.get("格斗"), 5)
        self.assertEqual(sports_keywords.get("围棋"), 5)
        self.assertEqual(sports_keywords.get("绝杀"), 4)
        self.assertEqual(sports_keywords.get("训练"), 3)

        farming_keywords = dict(analysis_profiles._keywords_from_manifest("farming_management"))
        self.assertEqual(farming_keywords.get("餐厅"), 4)
        self.assertEqual(farming_keywords.get("城墙"), 3)
        self.assertEqual(farming_keywords.get("囤积"), 3)
        self.assertEqual(farming_keywords.get("税收"), 2)

        isekai_keywords = dict(analysis_profiles._keywords_from_manifest("isekai_lightnovel"))
        self.assertEqual(isekai_keywords.get("技能"), 4)
        game_keywords = dict(analysis_profiles._keywords_from_manifest("game_system"))
        self.assertEqual(game_keywords.get("游戏"), 3)
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "亏成首富从游戏开始",
                "主角经营公司，打造产品和工厂，围绕产业链、供应链、利润与用户口碑扩张。",
            ),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "边城基建",
                "主角囤积粮食修建城墙，靠产量提升和税收管理人口，逐步恢复民生。",
            ),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "修真聊天群",
                "修士聊天群里讨论金丹元婴、宗门功法和求道长生，没有经营灵田或基建。",
            ),
            "xianxia_fantasy",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "娱乐公司资本运作",
                "经纪公司安排明星通告、剧组片场、热搜公关和选秀出道，处理粉丝对家。",
            ),
            "entertainment_industry",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "纯商战",
                "创业公司CEO和合伙人融资，董事会讨论股权、上市、现金流、供应链和竞业协议。",
            ),
            "business_career",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "虚拟游戏副本",
                "主角进入虚拟游戏，查看面板技能，完成副本任务获得装备奖励。",
            ),
            "game_system",
        )
        basketball = analysis_profiles.infer_profiles_for_text(
            "篮球系统训练营",
            "高中篮球队参加联赛，教练安排战术训练，主角冲击冠军。",
        )
        self.assertIn("sports_competition", basketball)
        self.assertNotIn("game_system", basketball)
        self.assertEqual(
            analysis_profiles.infer_profile_for_text(
                "影后经营游戏",
                "娱乐圈影后进入剧组拍戏，导演安排综艺宣发，经纪人处理热搜和饭圈。",
            ),
            "entertainment_industry",
        )

        mixed = analysis_profiles.infer_profiles_for_text(
            "修仙种田聊天群",
            "修士聊天群里讨论金丹元婴，也长期经营灵田药园和宗门产业链。",
        )
        self.assertIn("xianxia_fantasy", mixed)
        self.assertIn("farming_management", mixed)

    def test_merge_profile_results_preserves_all_profiles(self):
        result = main._merge_profile_results(
            "book",
            [
                {"status": "ok", "profile": "harem", "error": ""},
                {"status": "skipped", "profile": "history", "error": ""},
            ],
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["profile"], "harem")
        self.assertEqual(result["profiles"], ["harem", "history"])

    def test_process_single_novel_passes_current_raw_data_to_reviewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novel_path = os.path.join(tmpdir, "后宫书.txt")
            raw_data_path = os.path.join(tmpdir, "results", "后宫书_scan", "raw_data.json")
            os.makedirs(os.path.dirname(raw_data_path), exist_ok=True)
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("stub")
            calls = []

            def fake_reviewer_main(**kwargs):
                calls.append(kwargs)

            with mock.patch.object(main, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(main, "_report_is_fresh", return_value=(False, None)), \
                    mock.patch.object(protagonist, "main", return_value=0), \
                    mock.patch.object(protagonist, "get_latest_report_files", return_value={"detailed": "detail.json"}), \
                    mock.patch.object(novel_scan, "main", return_value=raw_data_path), \
                    mock.patch.object(novel_reviewer, "main", side_effect=fake_reviewer_main), \
                    mock.patch.object(report, "main", return_value=0):
                result = main.process_single_novel(novel_path, profile_name="harem", run_id="run", skip_fresh=False)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(calls[0]["raw_data_path"], raw_data_path)
            self.assertEqual(calls[0]["novel_path"], novel_path)

    def test_harem_plus_general_scan_switches_profile_temporarily(self):
        harem = analysis_profiles.load_analysis_profile("harem")
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        old_rules = os.environ.get("ANALYSIS_RULES_FILE")
        old_flag = os.environ.get("HAREM_PLUS_GENERAL_SCAN")
        calls = []

        class FakeGeneralScan:
            @staticmethod
            def main(novel_path=None, book_name=None, run_id=None, detail_path=None, profile_override=None):
                calls.append({
                    "profile": os.environ.get("ANALYSIS_PROFILE"),
                    "rules": os.environ.get("ANALYSIS_RULES_FILE"),
                    "novel_path": novel_path,
                    "book_name": book_name,
                    "run_id": run_id,
                    "detail_path": detail_path,
                })

        try:
            os.environ["ANALYSIS_PROFILE"] = "harem"
            os.environ["ANALYSIS_RULES_FILE"] = harem.rules_file
            os.environ.pop("HAREM_PLUS_GENERAL_SCAN", None)
            self.assertFalse(main._harem_plus_general_scan_enabled(harem))

            os.environ["HAREM_PLUS_GENERAL_SCAN"] = "1"
            self.assertTrue(main._harem_plus_general_scan_enabled(harem))
            main._run_harem_plus_general_scan(FakeGeneralScan, "/tmp/book.txt", "book", "run", "/tmp/detail.json", harem)
        finally:
            if old_profile is None:
                os.environ.pop("ANALYSIS_PROFILE", None)
            else:
                os.environ["ANALYSIS_PROFILE"] = old_profile
            if old_rules is None:
                os.environ.pop("ANALYSIS_RULES_FILE", None)
            else:
                os.environ["ANALYSIS_RULES_FILE"] = old_rules
            if old_flag is None:
                os.environ.pop("HAREM_PLUS_GENERAL_SCAN", None)
            else:
                os.environ["HAREM_PLUS_GENERAL_SCAN"] = old_flag

        self.assertEqual(calls[0]["profile"], "general")
        self.assertIn("profiles/general", calls[0]["rules"])
        self.assertEqual(calls[0]["book_name"], "book")
        self.assertEqual(os.environ.get("ANALYSIS_PROFILE"), old_profile)

    def test_harem_plus_auto_selects_secondary_profile(self):
        harem = analysis_profiles.load_analysis_profile("harem")
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        old_rules = os.environ.get("ANALYSIS_RULES_FILE")
        calls = []

        class FakeGeneralScan:
            @staticmethod
            def main(novel_path=None, book_name=None, run_id=None, detail_path=None, profile_override=None):
                calls.append({
                    "profile": os.environ.get("ANALYSIS_PROFILE"),
                    "rules": os.environ.get("ANALYSIS_RULES_FILE"),
                    "novel_path": novel_path,
                    "book_name": book_name,
                    "focus": list(getattr(profile_override, "scan_focus", []) or []),
                })

        with tempfile.TemporaryDirectory() as tmpdir:
            novel_path = os.path.join(tmpdir, "仙路后宫.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("男主与多女主双修，红颜和道侣一起卷入宗门、金丹、元婴、秘境和飞升传承。")

            try:
                os.environ["ANALYSIS_PROFILE"] = "harem"
                os.environ["ANALYSIS_RULES_FILE"] = harem.rules_file
                selected = main._select_harem_plus_general_profile(novel_path, "仙路后宫", harem)
                main._run_harem_plus_general_scan(FakeGeneralScan, novel_path, "仙路后宫", "run", "/tmp/detail.json", harem)
            finally:
                if old_profile is None:
                    os.environ.pop("ANALYSIS_PROFILE", None)
                else:
                    os.environ["ANALYSIS_PROFILE"] = old_profile
                if old_rules is None:
                    os.environ.pop("ANALYSIS_RULES_FILE", None)
                else:
                    os.environ["ANALYSIS_RULES_FILE"] = old_rules

        self.assertEqual(selected.name, "xianxia_fantasy")
        self.assertEqual(calls[0]["profile"], "xianxia_fantasy")
        self.assertIn("profiles/xianxia_fantasy", calls[0]["rules"])
        self.assertTrue(any("双修功法" in item for item in calls[0]["focus"]))
        self.assertTrue(any("宗门规矩" in item for item in calls[0]["focus"]))

    def test_harem_plus_auto_runs_multiple_secondary_profiles(self):
        harem = analysis_profiles.load_analysis_profile("harem")
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        old_rules = os.environ.get("ANALYSIS_RULES_FILE")
        calls = []

        class FakeGeneralScan:
            @staticmethod
            def main(novel_path=None, book_name=None, run_id=None, detail_path=None, profile_override=None):
                calls.append({
                    "profile": os.environ.get("ANALYSIS_PROFILE"),
                    "rules": os.environ.get("ANALYSIS_RULES_FILE"),
                    "novel_path": novel_path,
                    "book_name": book_name,
                    "focus": list(getattr(profile_override, "scan_focus", []) or []),
                })

        with tempfile.TemporaryDirectory() as tmpdir:
            novel_path = os.path.join(tmpdir, "蒸汽后宫侦探.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write(
                    "男主和红颜卷入后宫关系，蒸汽时代的教会帝国里，"
                    "炼金矩阵和差分机推动神秘复苏案件，侦探调查谋杀案并推理真凶。"
                )

            try:
                os.environ["ANALYSIS_PROFILE"] = "harem"
                os.environ["ANALYSIS_RULES_FILE"] = harem.rules_file
                selected = main._select_harem_plus_general_profiles(novel_path, "蒸汽后宫侦探", harem)
                main._run_harem_plus_general_scan(FakeGeneralScan, novel_path, "蒸汽后宫侦探", "run", "/tmp/detail.json", harem)
            finally:
                if old_profile is None:
                    os.environ.pop("ANALYSIS_PROFILE", None)
                else:
                    os.environ["ANALYSIS_PROFILE"] = old_profile
                if old_rules is None:
                    os.environ.pop("ANALYSIS_RULES_FILE", None)
                else:
                    os.environ["ANALYSIS_RULES_FILE"] = old_rules

        selected_names = [profile.name for profile in selected]
        called_names = [call["profile"] for call in calls]
        self.assertIn("steampunk_fantasy", selected_names)
        self.assertIn("mystery_detective", selected_names)
        self.assertEqual(called_names[:2], selected_names[:2])
        self.assertTrue(any("神秘复苏" in item for item in calls[0]["focus"]))
        self.assertTrue(any("单个案件是否推动人物关系" in item or "外挂硬解" in item for item in calls[1]["focus"]))

    def test_harem_plus_secondary_focus_covers_cross_genre_profiles(self):
        harem = analysis_profiles.load_analysis_profile("harem")
        overrides = harem.harem_plus.get("secondary_focus_overrides", {})

        for profile_name in [
            "xianxia_fantasy",
            "history",
            "hard_sci_fi",
            "urban_power",
            "game_system",
            "isekai_lightnovel",
            "steampunk_fantasy",
            "apocalypse_survival",
            "military_war",
            "crime_forensics",
            "cosmic_horror",
            "campus_youth",
            "entertainment_industry",
            "farming_management",
            "business_career",
            "mystery_detective",
            "sports_competition",
            "nation_fate",
            "simulator",
            "chinese_weird",
            "mastermind_hidden",
        ]:
            self.assertIn(profile_name, overrides)
            self.assertTrue(overrides[profile_name], profile_name)

        sci_fi = analysis_profiles.load_analysis_profile("hard_sci_fi")
        isekai = analysis_profiles.load_analysis_profile("isekai_lightnovel")
        steampunk = analysis_profiles.load_analysis_profile("steampunk_fantasy")
        apocalypse = analysis_profiles.load_analysis_profile("apocalypse_survival")
        entertainment = analysis_profiles.load_analysis_profile("entertainment_industry")
        crime = analysis_profiles.load_analysis_profile("crime_forensics")
        nation_fate = analysis_profiles.load_analysis_profile("nation_fate")
        simulator = analysis_profiles.load_analysis_profile("simulator")
        chinese_weird = analysis_profiles.load_analysis_profile("chinese_weird")
        mastermind_hidden = analysis_profiles.load_analysis_profile("mastermind_hidden")
        sci_fi_override = main._with_harem_plus_secondary_focus(sci_fi, harem)
        isekai_override = main._with_harem_plus_secondary_focus(isekai, harem)
        steampunk_override = main._with_harem_plus_secondary_focus(steampunk, harem)
        apocalypse_override = main._with_harem_plus_secondary_focus(apocalypse, harem)
        entertainment_override = main._with_harem_plus_secondary_focus(entertainment, harem)
        crime_override = main._with_harem_plus_secondary_focus(crime, harem)
        nation_fate_override = main._with_harem_plus_secondary_focus(nation_fate, harem)
        simulator_override = main._with_harem_plus_secondary_focus(simulator, harem)
        chinese_weird_override = main._with_harem_plus_secondary_focus(chinese_weird, harem)
        mastermind_override = main._with_harem_plus_secondary_focus(mastermind_hidden, harem)

        self.assertTrue(any("意识上传" in item and "洁度" in item for item in sci_fi_override.scan_focus))
        self.assertTrue(any("勇者" in item and "送女" in item for item in isekai_override.scan_focus))
        self.assertTrue(any("神秘复苏" in item and "亵女" in item for item in steampunk_override.scan_focus))
        self.assertTrue(any("末世秩序崩塌" in item and "送女" in item for item in apocalypse_override.scan_focus))
        self.assertTrue(any("CP营销" in item and "绿帽" in item for item in entertainment_override.scan_focus))
        self.assertTrue(any("受害未遂" in item and "绿帽" in item for item in crime_override.scan_focus))
        self.assertTrue(any("国运绑定" in item and "送女" in item for item in nation_fate_override.scan_focus))
        self.assertTrue(any("读档" in item and "前世雷" in item for item in simulator_override.scan_focus))
        self.assertTrue(any("阴婚" in item and "送女" in item for item in chinese_weird_override.scan_focus))
        self.assertTrue(any("多马甲" in item and "绿帽" in item for item in mastermind_override.scan_focus))

    def test_requested_profiles_accepts_manual_multi_select(self):
        self.assertEqual(main._normalize_requested_profiles(["历史", "科幻"]), ["history", "hard_sci_fi"])
        self.assertEqual(main._normalize_requested_profiles("历史,科幻"), ["history", "hard_sci_fi"])
        self.assertEqual(main._normalize_requested_profiles(["auto", "历史"]), ["auto"])

    def test_web_profile_accepts_manual_multi_select(self):
        self.assertEqual(web_manager._normalize_web_profile(["历史", "科幻"]), ["history", "hard_sci_fi"])
        self.assertEqual(web_manager._normalize_web_profile(["auto", "历史"]), ["auto"])

    def test_specialty_profiles_use_general_character_mode(self):
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        try:
            os.environ["ANALYSIS_PROFILE"] = "history"
            self.assertTrue(web_manager._normalize_web_profile("历史"))
            import protagonist

            self.assertTrue(protagonist._is_general_profile())
        finally:
            if old_profile is None:
                os.environ.pop("ANALYSIS_PROFILE", None)
            else:
                os.environ["ANALYSIS_PROFILE"] = old_profile

    def test_load_configs_non_interactive_does_not_wait_for_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "api.txt"), "w", encoding="utf-8") as f:
                f.write("")

            with self.assertRaises(SystemExit):
                main.load_configs(tmp, interactive=False)

    def test_load_configs_reads_dotenv_before_setting_file(self):
        keys = ["API_KEY", "API_KEY_POOL", "BASE_URL", "MODEL_NAME", "MAX_WORKERS", "HAREM_PLUS_GENERAL_SCAN"]
        old_env = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                    f.write("API_KEY=sk-dotenv\nBASE_URL=https://dotenv.example/v1\nMODEL_NAME=dotenv-model\nMAX_WORKERS=2\n")
                with open(os.path.join(tmp, "setting.txt"), "w", encoding="utf-8") as f:
                    f.write(
                        "BASE_URL=https://setting.example/v1\n"
                        "MODEL_NAME=setting-model\n"
                        "MAX_WORKERS=9\n"
                        "HAREM_PLUS_GENERAL_SCAN=1\n"
                    )

                main.load_configs(tmp, interactive=False)
                self.assertEqual(os.environ["API_KEY"], "sk-dotenv")
                self.assertEqual(os.environ["BASE_URL"], "https://dotenv.example/v1")
                self.assertEqual(os.environ["MODEL_NAME"], "dotenv-model")
                self.assertEqual(os.environ["MAX_WORKERS"], "2")
                self.assertEqual(os.environ["HAREM_PLUS_GENERAL_SCAN"], "1")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_load_configs_dotenv_overrides_harem_plus_setting(self):
        keys = ["API_KEY", "API_KEY_POOL", "HAREM_PLUS_GENERAL_SCAN"]
        old_env = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                    f.write("API_KEY=sk-dotenv\nHAREM_PLUS_GENERAL_SCAN=0\n")
                with open(os.path.join(tmp, "setting.txt"), "w", encoding="utf-8") as f:
                    f.write("HAREM_PLUS_GENERAL_SCAN=1\n")

                main.load_configs(tmp, interactive=False)
                self.assertEqual(os.environ["HAREM_PLUS_GENERAL_SCAN"], "0")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_load_configs_reads_setting_sample_runtime_fields(self):
        keys = sorted(
            set(main._DEFAULT_ENV_SETTINGS)
            | set(main._PASSTHROUGH_SETTING_KEYS)
            | set(main._VALIDATED_NON_NEGATIVE_INT_KEYS)
            | set(main._VALIDATED_NON_NEGATIVE_FLOAT_KEYS)
            | {"API_KEY", "API_KEY_POOL", "ANALYSIS_RULES_FILE"}
        )
        old_env = {key: os.environ.get(key) for key in keys}
        old_stall_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_future_stall_timeout = web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS
        old_base_dir = web_manager.get_base_dir
        try:
            for key in keys:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, "api.txt"), "w", encoding="utf-8") as f:
                    f.write("sk-setting\n")
                with open(os.path.join(tmp, "setting.txt"), "w", encoding="utf-8") as f:
                    f.write(
                        "WEB_ACCESS_TOKEN=local-token\n"
                        "WEB_ALLOW_NO_AUTH=0\n"
                        "GENERAL_SCAN_MAX_CHUNKS=160\n"
                        "GENERAL_SCAN_CONTEXT_MAX_CHARS=900\n"
                        "HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH=2\n"
                        "HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS=800\n"
                        "LOG_MAX_BYTES=2048\n"
                        "LOG_BACKUP_COUNT=2\n"
                        "SCAN_STALL_TIMEOUT_SECONDS=321.5\n"
                        "SCAN_FUTURE_STALL_TIMEOUT_SECONDS=123.5\n"
                    )

                main.load_configs(tmp, interactive=False)
                self.assertEqual(os.environ["WEB_ACCESS_TOKEN"], "local-token")
                self.assertEqual(os.environ["WEB_ALLOW_NO_AUTH"], "0")
                self.assertEqual(os.environ["GENERAL_SCAN_MAX_CHUNKS"], "160")
                self.assertEqual(os.environ["GENERAL_SCAN_CONTEXT_MAX_CHARS"], "900")
                self.assertEqual(os.environ["HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH"], "2")
                self.assertEqual(os.environ["HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS"], "800")
                self.assertEqual(os.environ["LOG_MAX_BYTES"], "2048")
                self.assertEqual(os.environ["LOG_BACKUP_COUNT"], "2")
                self.assertEqual(os.environ["SCAN_STALL_TIMEOUT_SECONDS"], "321.5")
                self.assertEqual(os.environ["SCAN_FUTURE_STALL_TIMEOUT_SECONDS"], "123.5")

                web_manager.get_base_dir = lambda: tmp
                ok, error = web_manager._try_load_runtime_config("test")
                self.assertTrue(ok, error)
                self.assertEqual(web_manager.SCAN_STALL_TIMEOUT_SECONDS, 321.5)
                self.assertEqual(web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS, 123.5)
                self.assertTrue(web_manager._web_auth_enabled())
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_stall_timeout
            web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS = old_future_stall_timeout
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_safe_filename(self):
        self.assertEqual(web_manager._safe_filename("../坏:名字"), "坏_名字.txt")
        self.assertEqual(web_manager._safe_filename("book.txt"), "book.txt")

    def test_web_manager_public_file_guard(self):
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                web_manager.get_base_dir = lambda: tmpdir
                os.makedirs(os.path.join(tmpdir, "results"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, "novels"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, "results2"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, "frontend", "dist"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, "frontend", "dist2"), exist_ok=True)

                result_path = os.path.join(tmpdir, "results", "report.txt")
                novel_path = os.path.join(tmpdir, "novels", "book.txt")
                sibling_path = os.path.join(tmpdir, "results2", "secret.txt")
                static_path = os.path.join(tmpdir, "frontend", "dist", "app.js")
                static_sibling_path = os.path.join(tmpdir, "frontend", "dist2", "app.js")
                for path in [result_path, novel_path, sibling_path, static_path, static_sibling_path]:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("data")

                self.assertTrue(web_manager._is_safe_public_file(result_path))
                self.assertTrue(web_manager._is_safe_public_file(novel_path))
                self.assertFalse(web_manager._is_safe_public_file(sibling_path))
                self.assertTrue(web_manager._is_safe_novel_file(novel_path))
                self.assertFalse(web_manager._is_safe_novel_file(sibling_path))

                self.assertEqual(web_manager._static_file_path("/app.js"), static_path)
                self.assertIsNone(web_manager._static_file_path("../dist2/app.js"))
                self.assertIsNone(web_manager._static_file_path(static_sibling_path))
        finally:
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_handler_adds_cors_headers(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.headers_sent = []
                self.responses = []
                self.request_version = "HTTP/1.1"
                self.wfile = io.BytesIO()
                self._headers_buffer = []

            def send_header(self, key, value):
                self.headers_sent.append((key, value))

            def send_response(self, code, message=None):
                self.responses.append((code, message))

        old_origin = os.environ.get("WEB_CORS_ALLOW_ORIGIN")
        try:
            os.environ["WEB_CORS_ALLOW_ORIGIN"] = "https://example.test"
            handler = FakeHandler()
            web_manager.Handler.end_headers(handler)
            self.assertIn(("Access-Control-Allow-Origin", "https://example.test"), handler.headers_sent)
            self.assertIn(("Access-Control-Allow-Methods", "GET, POST, OPTIONS"), handler.headers_sent)
            self.assertIn(("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID, Authorization, X-Web-Access-Token, X-Web-Unsafe-Action"), handler.headers_sent)

            options_handler = FakeHandler()
            web_manager.Handler.do_OPTIONS(options_handler)
            self.assertEqual(options_handler.responses[0][0], 204)
            self.assertIn(("Access-Control-Allow-Origin", "https://example.test"), options_handler.headers_sent)
        finally:
            if old_origin is None:
                os.environ.pop("WEB_CORS_ALLOW_ORIGIN", None)
            else:
                os.environ["WEB_CORS_ALLOW_ORIGIN"] = old_origin

    def test_web_manager_access_logger_uses_rotation(self):
        old_base_dir = web_manager.get_base_dir
        old_access_logger = web_manager.ACCESS_LOGGER
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                web_manager.ACCESS_LOGGER = None
                logger = web_manager._access_logger()
                self.assertEqual(logger.name, "web_manager.access")
                self.assertFalse(logger.propagate)
                self.assertEqual(len(logger.handlers), 1)
                self.assertIsInstance(logger.handlers[0], RotatingFileHandler)
                self.assertEqual(
                    logger.handlers[0].baseFilename,
                    os.path.join(tmp, "results", "web_logs", "web_access.log"),
                )
        finally:
            if web_manager.ACCESS_LOGGER is not None:
                for handler in list(web_manager.ACCESS_LOGGER.handlers):
                    web_manager.ACCESS_LOGGER.removeHandler(handler)
                    handler.close()
            web_manager.ACCESS_LOGGER = old_access_logger
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_access_log_redacts_token_query(self):
        class CaptureLogger:
            def __init__(self):
                self.records = []

            def info(self, fmt, *args):
                self.records.append((fmt, args))

        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.command = "GET"
                self.path = "/api/state?token=secret&book=a&access_token=hidden"

            def address_string(self):
                return "127.0.0.1"

        capture = CaptureLogger()
        old_access_logger = web_manager._access_logger
        try:
            web_manager._access_logger = lambda: capture
            web_manager.Handler.log_message(FakeHandler(), '"GET /api/state?token=secret HTTP/1.1" %s %s', "200", "123")
            self.assertEqual(len(capture.records), 1)
            fmt, args = capture.records[0]
            rendered = fmt % args
            self.assertIn("GET", rendered)
            self.assertIn("/api/state?", rendered)
            self.assertIn("token=%2A%2A%2A", rendered)
            self.assertIn("access_token=%2A%2A%2A", rendered)
            self.assertNotIn("secret", rendered)
            self.assertNotIn("hidden", rendered)
        finally:
            web_manager._access_logger = old_access_logger

    def test_web_manager_load_state_logs_corrupt_state_file(self):
        old_base_dir = web_manager.get_base_dir
        old_state = web_manager.STATE
        old_recover = web_manager._recover_incomplete_tasks
        old_sync = web_manager._sync_books_from_disk
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                with open(os.path.join(results_dir, "web_manager_state.json"), "w", encoding="utf-8") as f:
                    f.write("{bad json")

                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {"books": {"old": {"id": "old"}}, "tasks": [{"id": "old-task"}]}
                web_manager._recover_incomplete_tasks = lambda: None
                web_manager._sync_books_from_disk = lambda: None

                with self.assertLogs("web_manager", level="WARNING") as logs:
                    web_manager._load_state()

                self.assertTrue(any("读取 Web 状态文件失败" in line for line in logs.output))
                self.assertEqual(web_manager.STATE["books"], {"old": {"id": "old"}})
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.STATE = old_state
            web_manager._recover_incomplete_tasks = old_recover
            web_manager._sync_books_from_disk = old_sync

    def test_web_manager_save_state_uses_unique_temp_files_and_cleans_up(self):
        old_base_dir = web_manager.get_base_dir
        old_state = web_manager.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {"book": {"id": "book", "status": "idle"}},
                    "tasks": [{"id": "task", "status": "queued"}],
                }

                web_manager._save_state()
                web_manager.STATE["books"]["book"]["message"] = "updated"
                web_manager._save_state()

                state_path = os.path.join(tmp, "results", "web_manager_state.json")
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["books"]["book"]["message"], "updated")
                leftovers = [
                    name for name in os.listdir(os.path.dirname(state_path))
                    if name.startswith("web_manager_state.json.") and name.endswith(".tmp")
                ]
                self.assertEqual(leftovers, [])
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.STATE = old_state

    def test_web_manager_save_state_is_thread_safe(self):
        old_base_dir = web_manager.get_base_dir
        old_state = web_manager.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {"books": {}, "tasks": []}

                def save_worker(index):
                    with web_manager.STATE_LOCK:
                        web_manager.STATE["books"][f"book-{index}"] = {"id": f"book-{index}"}
                    web_manager._save_state()

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(save_worker, range(8)))

                state_path = os.path.join(tmp, "results", "web_manager_state.json")
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(len(data["books"]), 8)
                leftovers = [
                    name for name in os.listdir(os.path.dirname(state_path))
                    if name.startswith("web_manager_state.json.") and name.endswith(".tmp")
                ]
                self.assertEqual(leftovers, [])
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.STATE = old_state

    def test_web_manager_access_token_auth_is_optional_and_secret(self):
        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        old_key_required = os.environ.get("NOVEL_REPORT_SCANNER_REQUIRE_API_KEY")
        old_allow_no_auth = os.environ.get("WEB_ALLOW_NO_AUTH")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            os.environ["NOVEL_REPORT_SCANNER_REQUIRE_API_KEY"] = "0"
            os.environ["WEB_ALLOW_NO_AUTH"] = "1"
            self.assertTrue(web_manager._is_authorized_request({}, ""))
            self.assertFalse(web_manager._unsafe_write_confirmed({}))
            self.assertTrue(web_manager._unsafe_write_confirmed({"X-Web-Unsafe-Action": "confirm"}))
            summary = web_manager._runtime_config_summary()
            self.assertFalse(summary["web"]["auth_enabled"])
            self.assertFalse(summary["web"]["api_key_required_on_start"])
            self.assertTrue(summary["web"]["allow_no_auth"])

            os.environ["WEB_ACCESS_TOKEN"] = "secret-token"
            os.environ["NOVEL_REPORT_SCANNER_REQUIRE_API_KEY"] = "1"
            os.environ["WEB_ALLOW_NO_AUTH"] = "0"
            self.assertFalse(web_manager._is_authorized_request({}, ""))
            self.assertFalse(web_manager._is_authorized_request({"Authorization": "Bearer wrong"}, ""))
            self.assertTrue(web_manager._is_authorized_request({"Authorization": "Bearer secret-token"}, ""))
            self.assertTrue(web_manager._is_authorized_request({"X-Web-Access-Token": "secret-token"}, ""))
            self.assertTrue(web_manager._is_authorized_request({}, "token=secret-token"))
            self.assertTrue(web_manager._unsafe_write_confirmed({}))

            protected_summary = web_manager._runtime_config_summary()
            self.assertTrue(protected_summary["web"]["auth_enabled"])
            self.assertTrue(protected_summary["web"]["api_key_required_on_start"])
            self.assertFalse(protected_summary["web"]["allow_no_auth"])
            self.assertNotIn("secret-token", json.dumps(protected_summary, ensure_ascii=False))
        finally:
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token
            if old_key_required is None:
                os.environ.pop("NOVEL_REPORT_SCANNER_REQUIRE_API_KEY", None)
            else:
                os.environ["NOVEL_REPORT_SCANNER_REQUIRE_API_KEY"] = old_key_required
            if old_allow_no_auth is None:
                os.environ.pop("WEB_ALLOW_NO_AUTH", None)
            else:
                os.environ["WEB_ALLOW_NO_AUTH"] = old_allow_no_auth

    def test_web_manager_runtime_config_reports_storage_writability(self):
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                summary = web_manager._runtime_config_summary()
                storage = summary["web"]["storage"]
                self.assertTrue(storage["novels"]["writable"])
                self.assertTrue(storage["results"]["writable"])
                self.assertEqual(storage["novels"]["error"], "")
                self.assertEqual(storage["results"]["error"], "")

            with tempfile.NamedTemporaryFile() as tmp_file:
                web_manager.get_base_dir = lambda: tmp_file.name
                storage = web_manager._runtime_config_summary()["web"]["storage"]
                self.assertFalse(storage["novels"]["writable"])
                self.assertFalse(storage["results"]["writable"])
                self.assertTrue(storage["novels"]["error"])
                self.assertTrue(storage["results"]["error"])
        finally:
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_health_reports_readiness_without_public_paths(self):
        old_config_ready = web_manager.CONFIG_READY
        try:
            web_manager.CONFIG_READY = False
            storage = {
                "novels": {"path": "/app/novels", "writable": False, "error": "Permission denied"},
                "results": {"path": "/app/results", "writable": True, "error": ""},
            }
            with mock.patch.object(web_manager, "_storage_health_summary", return_value=storage):
                health = web_manager._health_summary()

            self.assertTrue(health["ok"])
            self.assertFalse(health["ready"])
            self.assertFalse(health["config_ready"])
            self.assertFalse(health["storage_ready"])
            self.assertEqual([item["type"] for item in health["health_issues"]], ["config", "storage"])
            self.assertNotIn("/app/novels", json.dumps(health, ensure_ascii=False))
            self.assertNotIn("Permission denied", json.dumps(health, ensure_ascii=False))
        finally:
            web_manager.CONFIG_READY = old_config_ready

    def test_web_manager_runtime_config_update_allows_only_safe_fields(self):
        keys = [
            "MAX_WORKERS",
            "RPM_LIMIT",
            "TPM_LIMIT",
            "RATE_LIMIT_SCOPE",
            "API_SERVER_ERROR_MAX_RETRIES",
            "API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS",
            "HAREM_SCAN_CHUNK_SIZE",
            "HAREM_SCAN_MAX_TOKENS",
            "HAREM_SCAN_RETRY_WORKERS",
            "GENERAL_SCAN_MAX_CHUNKS",
            "GENERAL_SCAN_SMART_DENSITY",
            "GENERAL_SCAN_CONTENT_AWARE_SAMPLING",
            "GENERAL_SCAN_INCREMENTAL_REUSE",
            "GENERAL_SCAN_WRITING_QUALITY",
            "GENERAL_SCAN_NARRATIVE_ARCHITECTURE",
            "GENERAL_SCAN_FORESHADOWING_ENGINEERING",
            "GENERAL_SCAN_SEMANTIC_LAYERS",
            "GENERAL_SCAN_READER_EXPERIENCE",
            "GENERAL_SCAN_CONTINUITY_AUDIT",
            "GENERAL_SCAN_ROLLING_CONTEXT",
            "GENERAL_SCAN_ENTITY_PRESCAN",
            "GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE",
            "GENERAL_SCAN_CONTEXT_MAX_CHARS",
            "RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER",
            "SCAN_STALL_TIMEOUT_SECONDS",
            "SCAN_FUTURE_STALL_TIMEOUT_SECONDS",
            "HAREM_PLUS_GENERAL_SCAN",
            "API_KEY",
        ]
        old_env = {key: os.environ.get(key) for key in keys}
        old_stall_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_future_stall_timeout = web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS
        try:
            os.environ["API_KEY"] = "sk-secret"
            ok, result = web_manager._update_runtime_config({
                "max_workers": "4",
                "rpm_limit": "",
                "tpm_limit": "5000",
                "rate_limit_scope": "per_key",
                "api_server_error_max_retries": "2",
                "api_server_error_fast_fail_input_chars": "15000",
                "harem_scan_chunk_size": "6500",
                "harem_scan_max_tokens": "2800",
                "harem_scan_retry_workers": "1",
                "general_scan_max_chunks": "120",
                "general_scan_smart_density": False,
                "general_scan_content_aware_sampling": False,
                "general_scan_incremental_reuse": False,
                "general_scan_writing_quality": False,
                "general_scan_narrative_architecture": False,
                "general_scan_foreshadowing_engineering": False,
                "general_scan_semantic_layers": False,
                "general_scan_reader_experience": False,
                "general_scan_continuity_audit": False,
                "general_scan_rolling_context": False,
                "general_scan_entity_prescan": False,
                "general_scan_knowledge_base_llm_merge": True,
                "general_scan_context_max_chars": "800",
                "rescan_skip_chronic_parse_failure_after": "3",
                "scan_stall_timeout_seconds": "900",
                "scan_future_stall_timeout_seconds": "600",
                "harem_plus_general_scan": True,
            })

            self.assertTrue(ok)
            self.assertEqual(os.environ["MAX_WORKERS"], "4")
            self.assertEqual(os.environ["RPM_LIMIT"], "")
            self.assertEqual(os.environ["TPM_LIMIT"], "5000")
            self.assertEqual(os.environ["RATE_LIMIT_SCOPE"], "per_key")
            self.assertEqual(os.environ["API_SERVER_ERROR_MAX_RETRIES"], "2")
            self.assertEqual(os.environ["API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS"], "15000")
            self.assertEqual(os.environ["HAREM_SCAN_CHUNK_SIZE"], "6500")
            self.assertEqual(os.environ["HAREM_SCAN_MAX_TOKENS"], "2800")
            self.assertEqual(os.environ["HAREM_SCAN_RETRY_WORKERS"], "1")
            self.assertEqual(os.environ["GENERAL_SCAN_MAX_CHUNKS"], "120")
            self.assertEqual(os.environ["GENERAL_SCAN_SMART_DENSITY"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_CONTENT_AWARE_SAMPLING"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_INCREMENTAL_REUSE"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_WRITING_QUALITY"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_NARRATIVE_ARCHITECTURE"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_FORESHADOWING_ENGINEERING"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_SEMANTIC_LAYERS"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_READER_EXPERIENCE"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_CONTINUITY_AUDIT"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_ROLLING_CONTEXT"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_ENTITY_PRESCAN"], "0")
            self.assertEqual(os.environ["GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE"], "1")
            self.assertEqual(os.environ["GENERAL_SCAN_CONTEXT_MAX_CHARS"], "800")
            self.assertEqual(os.environ["RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER"], "3")
            self.assertEqual(os.environ["SCAN_STALL_TIMEOUT_SECONDS"], "900")
            self.assertEqual(web_manager.SCAN_STALL_TIMEOUT_SECONDS, 900)
            self.assertEqual(os.environ["SCAN_FUTURE_STALL_TIMEOUT_SECONDS"], "600")
            self.assertEqual(web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS, 600)
            self.assertEqual(os.environ["HAREM_PLUS_GENERAL_SCAN"], "1")
            self.assertEqual(result["max_workers"], "4")
            self.assertFalse(result["general_scan_smart_density"])
            self.assertFalse(result["general_scan_content_aware_sampling"])
            self.assertFalse(result["general_scan_incremental_reuse"])
            self.assertFalse(result["general_scan_writing_quality"])
            self.assertFalse(result["general_scan_narrative_architecture"])
            self.assertFalse(result["general_scan_foreshadowing_engineering"])
            self.assertFalse(result["general_scan_semantic_layers"])
            self.assertFalse(result["general_scan_reader_experience"])
            self.assertFalse(result["general_scan_continuity_audit"])
            self.assertFalse(result["general_scan_rolling_context"])
            self.assertFalse(result["general_scan_entity_prescan"])
            self.assertTrue(result["general_scan_knowledge_base_llm_merge"])
            self.assertEqual(result["general_scan_context_max_chars"], "800")
            self.assertEqual(result["rescan_skip_chronic_parse_failure_after"], "3")
            self.assertEqual(result["api_server_error_max_retries"], "2")
            self.assertEqual(result["api_server_error_fast_fail_input_chars"], "15000")
            self.assertEqual(result["harem_scan_chunk_size"], "6500")
            self.assertEqual(result["harem_scan_max_tokens"], "2800")
            self.assertEqual(result["harem_scan_retry_workers"], "1")
            self.assertEqual(result["scan_future_stall_timeout_seconds"], "600")
            self.assertEqual(result["web"]["scan_stall_timeout_seconds"], 900)
            self.assertTrue(result["web"]["scan_stall_watchdog_enabled"])
            self.assertTrue(result["harem_plus_general_scan"])
            self.assertIn("max_workers", result["editable"])
            self.assertNotIn("api_key", result["editable"])
            self.assertNotIn("sk-secret", json.dumps(result, ensure_ascii=False))

            ok, error = web_manager._update_runtime_config({"api_key": "sk-leak"})
            self.assertFalse(ok)
            self.assertIn("unsupported config field", error)

            ok, error = web_manager._update_runtime_config({"max_workers": "0"})
            self.assertFalse(ok)
            self.assertIn("between", error)

            ok, result = web_manager._update_runtime_config({"rate_limit_scope": "auto"})
            self.assertTrue(ok)
            self.assertEqual(os.environ["RATE_LIMIT_SCOPE"], "auto")
            self.assertEqual(result["rate_limit_scope"], "auto")

            ok, error = web_manager._update_runtime_config({"rate_limit_scope": "account"})
            self.assertFalse(ok)
            self.assertIn("one of", error)
        finally:
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_stall_timeout
            web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS = old_future_stall_timeout
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_config_api_updates_runtime_config(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, body):
                self.path = "/api/config"
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_env = {key: os.environ.get(key) for key in ("WEB_ACCESS_TOKEN", "MAX_WORKERS")}
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                os.environ.pop("WEB_ACCESS_TOKEN", None)
                body = json.dumps({"config": {"max_workers": 5}}, ensure_ascii=False).encode("utf-8")
                handler = FakeHandler(body)
                handler.headers["X-Web-Unsafe-Action"] = "confirm"

                web_manager.Handler.do_POST(handler)

                self.assertEqual(handler.sent[0][0], 200)
                self.assertTrue(handler.sent[0][1]["ok"])
                self.assertEqual(handler.sent[0][1]["config"]["max_workers"], "5")
                self.assertEqual(os.environ["MAX_WORKERS"], "5")
        finally:
            web_manager.get_base_dir = old_base_dir
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_write_requires_confirmation_when_token_unset(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, body, headers=None):
                self.path = "/api/config"
                self.headers = {"Content-Length": str(len(body)), **(headers or {})}
                self.rfile = io.BytesIO(body)
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_env = {key: os.environ.get(key) for key in ("WEB_ACCESS_TOKEN", "MAX_WORKERS")}
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                web_manager.get_base_dir = lambda: tmp
                os.environ.pop("WEB_ACCESS_TOKEN", None)
                body = json.dumps({"config": {"max_workers": 5}}, ensure_ascii=False).encode("utf-8")

                denied = FakeHandler(body)
                web_manager.Handler.do_POST(denied)
                self.assertEqual(denied.sent[0][0], 403)
                self.assertEqual(denied.sent[0][1]["error"], "unsafe action requires confirmation")
                self.assertIn("X-Web-Unsafe-Action", denied.sent[0][1]["hint"])

                allowed = FakeHandler(body, headers={"X-Web-Unsafe-Action": "confirm"})
                web_manager.Handler.do_POST(allowed)
                self.assertEqual(allowed.sent[0][0], 200)
                self.assertTrue(allowed.sent[0][1]["ok"])
        finally:
            web_manager.get_base_dir = old_base_dir
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_enqueue_permission_error_returns_json(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, body):
                self.path = "/api/enqueue"
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_state = web_manager.STATE
        old_save_state = web_manager._save_state
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "ready": {
                        "id": "ready",
                        "name": "ready",
                        "path": "/tmp/ready.txt",
                        "profile": "general",
                        "status": "idle",
                    }
                },
                "tasks": [],
            }
            web_manager._save_state = lambda: (_ for _ in ()).throw(
                PermissionError("/app/results/web_manager_state.json.1.tmp")
            )
            body = json.dumps({"book_id": "ready"}, ensure_ascii=False).encode("utf-8")
            handler = FakeHandler(body)
            handler.headers["X-Web-Unsafe-Action"] = "confirm"

            web_manager.Handler.do_POST(handler)

            self.assertEqual(handler.sent[0][0], 500)
            self.assertEqual(handler.sent[0][1]["error"], "storage write failed")
            self.assertIn("/app/results/web_manager_state.json.1.tmp", handler.sent[0][1]["detail"])
            self.assertIn("novels/results", handler.sent[0][1]["hint"])
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state
            web_manager._save_state = old_save_state
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_operation_failure_response_includes_error(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, body):
                self.path = "/api/enqueue"
                self.headers = {
                    "Content-Length": str(len(body)),
                    "X-Web-Unsafe-Action": "confirm",
                }
                self.rfile = io.BytesIO(body)
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_state = web_manager.STATE
        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            web_manager.STATE = {
                "books": {
                    "busy": {
                        "id": "busy",
                        "name": "busy",
                        "path": "/tmp/busy.txt",
                        "profile": "general",
                        "status": "queued",
                    }
                },
                "tasks": [],
            }
            body = json.dumps({"book_id": "busy"}, ensure_ascii=False).encode("utf-8")
            handler = FakeHandler(body)

            web_manager.Handler.do_POST(handler)

            self.assertEqual(handler.sent[0][0], 409)
            self.assertFalse(handler.sent[0][1]["ok"])
            self.assertEqual(handler.sent[0][1]["result"], "book already queued or running")
            self.assertEqual(handler.sent[0][1]["error"], "book already queued or running")
        finally:
            web_manager.STATE = old_state
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_runtime_config_persists_to_env_file(self):
        import tempfile

        env_field_map = {
            "MAX_WORKERS": "max_workers",
            "RPM_LIMIT": "rpm_limit",
            "TPM_LIMIT": "tpm_limit",
            "RATE_LIMIT_SCOPE": "rate_limit_scope",
            "API_SERVER_ERROR_MAX_RETRIES": "api_server_error_max_retries",
            "API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS": "api_server_error_fast_fail_input_chars",
            "HAREM_SCAN_CHUNK_SIZE": "harem_scan_chunk_size",
            "HAREM_SCAN_MAX_TOKENS": "harem_scan_max_tokens",
            "HAREM_SCAN_RETRY_WORKERS": "harem_scan_retry_workers",
            "GENERAL_SCAN_MAX_CHUNKS": "general_scan_max_chunks",
            "GENERAL_SCAN_SMART_DENSITY": "general_scan_smart_density",
            "GENERAL_SCAN_CONTENT_AWARE_SAMPLING": "general_scan_content_aware_sampling",
            "GENERAL_SCAN_INCREMENTAL_REUSE": "general_scan_incremental_reuse",
            "GENERAL_SCAN_WRITING_QUALITY": "general_scan_writing_quality",
            "GENERAL_SCAN_NARRATIVE_ARCHITECTURE": "general_scan_narrative_architecture",
            "GENERAL_SCAN_FORESHADOWING_ENGINEERING": "general_scan_foreshadowing_engineering",
            "GENERAL_SCAN_SEMANTIC_LAYERS": "general_scan_semantic_layers",
            "GENERAL_SCAN_READER_EXPERIENCE": "general_scan_reader_experience",
            "GENERAL_SCAN_CONTINUITY_AUDIT": "general_scan_continuity_audit",
            "GENERAL_SCAN_ROLLING_CONTEXT": "general_scan_rolling_context",
            "GENERAL_SCAN_ENTITY_PRESCAN": "general_scan_entity_prescan",
            "GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE": "general_scan_knowledge_base_llm_merge",
            "GENERAL_SCAN_CONTEXT_MAX_CHARS": "general_scan_context_max_chars",
            "RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER": "rescan_skip_chronic_parse_failure_after",
            "SCAN_STALL_TIMEOUT_SECONDS": "scan_stall_timeout_seconds",
            "SCAN_FUTURE_STALL_TIMEOUT_SECONDS": "scan_future_stall_timeout_seconds",
            "HAREM_PLUS_GENERAL_SCAN": "harem_plus_general_scan",
        }
        old_values = {env: os.environ.get(env) for env in env_field_map}
        old_stall_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_future_stall_timeout = web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS

        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            # 模拟 get_base_dir 返回 tmpdir
            original_base_dir = web_manager.get_base_dir
            web_manager.get_base_dir = lambda: tmpdir
            try:
                # 场景1：写入全新 .env
                ok, _ = web_manager._update_runtime_config({
                    "max_workers": 8,
                    "rpm_limit": 120,
                })
                self.assertTrue(ok)
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("MAX_WORKERS=8", content)
                self.assertIn("RPM_LIMIT=120", content)
                # 不存在的字段不应被写入
                self.assertNotIn("API_KEY", content)

                # 场景2：更新已有字段，保留其他行
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write("# 注释\nAPI_KEY=secret\nMAX_WORKERS=4\n\n")
                ok, _ = web_manager._update_runtime_config({
                    "max_workers": 16,
                    "api_server_error_max_retries": 2,
                    "api_server_error_fast_fail_input_chars": 15000,
                    "harem_scan_chunk_size": 6500,
                    "harem_scan_max_tokens": 2800,
                    "harem_scan_retry_workers": 1,
                    "general_scan_smart_density": False,
                    "general_scan_content_aware_sampling": False,
                    "general_scan_incremental_reuse": False,
                    "general_scan_writing_quality": False,
                    "general_scan_narrative_architecture": False,
                    "general_scan_foreshadowing_engineering": False,
                    "general_scan_semantic_layers": False,
                    "general_scan_reader_experience": False,
                    "general_scan_continuity_audit": False,
                    "general_scan_rolling_context": False,
                    "general_scan_entity_prescan": False,
                    "general_scan_knowledge_base_llm_merge": True,
                    "general_scan_context_max_chars": 800,
                    "rescan_skip_chronic_parse_failure_after": 3,
                    "scan_stall_timeout_seconds": 900,
                    "scan_future_stall_timeout_seconds": 600,
                    "harem_plus_general_scan": True,
                })
                self.assertTrue(ok)
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertIn("# 注释", lines)
                self.assertIn("API_KEY=secret", lines)
                self.assertIn("MAX_WORKERS=16", lines)
                self.assertIn("API_SERVER_ERROR_MAX_RETRIES=2", lines)
                self.assertIn("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS=15000", lines)
                self.assertIn("HAREM_SCAN_CHUNK_SIZE=6500", lines)
                self.assertIn("HAREM_SCAN_MAX_TOKENS=2800", lines)
                self.assertIn("HAREM_SCAN_RETRY_WORKERS=1", lines)
                self.assertIn("GENERAL_SCAN_SMART_DENSITY=0", lines)
                self.assertIn("GENERAL_SCAN_CONTENT_AWARE_SAMPLING=0", lines)
                self.assertIn("GENERAL_SCAN_INCREMENTAL_REUSE=0", lines)
                self.assertIn("GENERAL_SCAN_WRITING_QUALITY=0", lines)
                self.assertIn("GENERAL_SCAN_NARRATIVE_ARCHITECTURE=0", lines)
                self.assertIn("GENERAL_SCAN_FORESHADOWING_ENGINEERING=0", lines)
                self.assertIn("GENERAL_SCAN_SEMANTIC_LAYERS=0", lines)
                self.assertIn("GENERAL_SCAN_READER_EXPERIENCE=0", lines)
                self.assertIn("GENERAL_SCAN_CONTINUITY_AUDIT=0", lines)
                self.assertIn("GENERAL_SCAN_ROLLING_CONTEXT=0", lines)
                self.assertIn("GENERAL_SCAN_ENTITY_PRESCAN=0", lines)
                self.assertIn("GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE=1", lines)
                self.assertIn("GENERAL_SCAN_CONTEXT_MAX_CHARS=800", lines)
                self.assertIn("RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER=3", lines)
                self.assertIn("SCAN_STALL_TIMEOUT_SECONDS=900", lines)
                self.assertIn("SCAN_FUTURE_STALL_TIMEOUT_SECONDS=600", lines)
                self.assertIn("HAREM_PLUS_GENERAL_SCAN=1", lines)
                # 旧值不应残留
                self.assertNotIn("MAX_WORKERS=4", lines)
                self.assertNotIn("MAX_WORKERS=8", lines)

                # 场景3：持久化失败不应阻止内存更新
                web_manager.get_base_dir = lambda: "/nonexistent/path/that/cannot/be/created"
                ok, result = web_manager._update_runtime_config({"max_workers": 32})
                self.assertTrue(ok)
                self.assertEqual(os.environ.get("MAX_WORKERS"), "32")
            finally:
                web_manager.get_base_dir = original_base_dir
                for env, value in old_values.items():
                    if value is None:
                        os.environ.pop(env, None)
                    else:
                        os.environ[env] = value
                web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_stall_timeout
                web_manager.SCAN_FUTURE_STALL_TIMEOUT_SECONDS = old_future_stall_timeout

    def test_web_manager_runtime_config_persist_is_thread_safe(self):
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = os.path.join(tmpdir, ".env")
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write("API_KEY=secret\n")
                web_manager.get_base_dir = lambda: tmpdir

                updates = [
                    {"max_workers": "2"},
                    {"api_server_error_max_retries": "3"},
                    {"harem_scan_chunk_size": "6200"},
                    {"general_scan_context_max_chars": "900"},
                    {"rate_limit_scope": "per_key"},
                    {"harem_plus_general_scan": "1"},
                ]
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                    results = list(executor.map(web_manager._persist_runtime_config_to_env_file, updates))

                self.assertTrue(all(ok for ok, _error in results))
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("API_KEY=secret", content)
                self.assertIn("MAX_WORKERS=2", content)
                self.assertIn("API_SERVER_ERROR_MAX_RETRIES=3", content)
                self.assertIn("HAREM_SCAN_CHUNK_SIZE=6200", content)
                self.assertIn("GENERAL_SCAN_CONTEXT_MAX_CHARS=900", content)
                self.assertIn("RATE_LIMIT_SCOPE=per_key", content)
                self.assertIn("HAREM_PLUS_GENERAL_SCAN=1", content)
                leftovers = [
                    name for name in os.listdir(tmpdir)
                    if name.startswith(".env.") and name.endswith(".tmp")
                ]
                self.assertEqual(leftovers, [])
        finally:
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_runtime_config_persist_cleans_temp_file_on_failure(self):
        old_base_dir = web_manager.get_base_dir
        old_replace = web_manager.os.replace
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                web_manager.get_base_dir = lambda: tmpdir

                def fail_replace(_src, _dst):
                    raise OSError("replace failed")

                web_manager.os.replace = fail_replace

                ok, error = web_manager._persist_runtime_config_to_env_file({"max_workers": "2"})

                self.assertFalse(ok)
                self.assertIn("replace failed", error)
                leftovers = [
                    name for name in os.listdir(tmpdir)
                    if name.startswith(".env.") and name.endswith(".tmp")
                ]
                self.assertEqual(leftovers, [])
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.os.replace = old_replace

    def test_web_manager_handler_rejects_unauthorized_api_requests(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, path="/api/state", headers=None):
                self.path = path
                self.headers = headers or {}
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        old_sync = web_manager._sync_books_from_disk
        try:
            os.environ["WEB_ACCESS_TOKEN"] = "secret-token"
            web_manager._sync_books_from_disk = lambda: None

            denied = FakeHandler()
            web_manager.Handler.do_GET(denied)
            self.assertEqual(denied.sent[0], (401, {"error": "unauthorized"}))

            allowed = FakeHandler(headers={"Authorization": "Bearer secret-token"})
            web_manager.Handler.do_GET(allowed)
            self.assertEqual(allowed.sent[0][0], 200)
            self.assertIn("books", allowed.sent[0][1])
        finally:
            web_manager._sync_books_from_disk = old_sync
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_unknown_api_routes_return_json_404(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, path):
                self.path = path
                self.headers = {"Content-Length": "0", "X-Web-Unsafe-Action": "confirm"}
                self.rfile = io.BytesIO(b"")
                self.sent = []
                self.errors = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

            def send_error(self, code, message=None):
                self.errors.append((code, message))

            def _serve_static(self, _path):
                return False

        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)

            get_api = FakeHandler("/api/missing")
            web_manager.Handler.do_GET(get_api)
            self.assertEqual(get_api.sent[0], (404, {"error": "not found"}))
            self.assertEqual(get_api.errors, [])

            post_api = FakeHandler("/api/missing")
            web_manager.Handler.do_POST(post_api)
            self.assertEqual(post_api.sent[0], (404, {"error": "not found"}))
            self.assertEqual(post_api.errors, [])

            static_missing = FakeHandler("/missing-page")
            web_manager.Handler.do_GET(static_missing)
            self.assertEqual(static_missing.sent, [])
            self.assertEqual(static_missing.errors[0][0], 404)
        finally:
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_sse_state_stream_sends_state_event(self):
        class OneShotWFile:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data
                raise BrokenPipeError()

            def flush(self):
                pass

        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.headers_sent = []
                self.wfile = OneShotWFile()

            def send_response(self, code):
                self.response = code

            def send_header(self, key, value):
                self.headers_sent.append((key, value))

            def end_headers(self):
                pass

        old_state = web_manager.STATE
        old_sync = web_manager._sync_books_from_disk
        old_interval = web_manager.SSE_STATE_INTERVAL_SECONDS
        old_last_sse_sync = web_manager.LAST_SSE_SYNC_AT
        try:
            web_manager.STATE = {"books": {}, "tasks": []}
            web_manager._sync_books_from_disk = lambda: None
            web_manager.LAST_SSE_SYNC_AT = 0.0
            web_manager.SSE_STATE_INTERVAL_SECONDS = 0
            handler = FakeHandler()

            web_manager.Handler._send_sse_state_stream(handler)

            self.assertEqual(handler.response, 200)
            self.assertIn(("Content-Type", "text/event-stream; charset=utf-8"), handler.headers_sent)
            body = handler.wfile.data.decode("utf-8")
            self.assertIn("event: state", body)
            self.assertIn('"books": []', body)
        finally:
            web_manager.STATE = old_state
            web_manager._sync_books_from_disk = old_sync
            web_manager.SSE_STATE_INTERVAL_SECONDS = old_interval
            web_manager.LAST_SSE_SYNC_AT = old_last_sse_sync

    def test_web_manager_sse_state_stream_expires_and_throttles_disk_sync(self):
        class BufferWFile:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data

            def flush(self):
                pass

        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.headers_sent = []
                self.wfile = BufferWFile()

            def send_response(self, code):
                self.response = code

            def send_header(self, key, value):
                self.headers_sent.append((key, value))

            def end_headers(self):
                pass

        old_state = web_manager.STATE
        old_sync = web_manager._sync_books_from_disk
        old_interval = web_manager.SSE_STATE_INTERVAL_SECONDS
        old_sync_interval = web_manager.SSE_SYNC_INTERVAL_SECONDS
        old_max_connection = web_manager.SSE_MAX_CONNECTION_SECONDS
        old_last_sse_sync = web_manager.LAST_SSE_SYNC_AT
        old_monotonic = web_manager.time.monotonic
        old_sleep = web_manager.time.sleep
        sync_calls = []
        now = [100.0]
        try:
            web_manager.STATE = {"books": {}, "tasks": []}
            web_manager._sync_books_from_disk = lambda: sync_calls.append(now[0])
            web_manager.SSE_STATE_INTERVAL_SECONDS = 0.01
            web_manager.SSE_SYNC_INTERVAL_SECONDS = 0.03
            web_manager.SSE_MAX_CONNECTION_SECONDS = 0.035
            web_manager.LAST_SSE_SYNC_AT = 0.0
            web_manager.time.monotonic = lambda: now[0]
            web_manager.time.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
            handler = FakeHandler()

            web_manager.Handler._send_sse_state_stream(handler)

            body = handler.wfile.data.decode("utf-8")
            self.assertGreater(body.count("event: state"), len(sync_calls))
            self.assertEqual(len(sync_calls), 2)
            self.assertAlmostEqual(sync_calls[0], 100.0)
            self.assertAlmostEqual(sync_calls[1], 100.03)
            self.assertGreaterEqual(now[0], 100.035)
        finally:
            web_manager.STATE = old_state
            web_manager._sync_books_from_disk = old_sync
            web_manager.SSE_STATE_INTERVAL_SECONDS = old_interval
            web_manager.SSE_SYNC_INTERVAL_SECONDS = old_sync_interval
            web_manager.SSE_MAX_CONNECTION_SECONDS = old_max_connection
            web_manager.LAST_SSE_SYNC_AT = old_last_sse_sync
            web_manager.time.monotonic = old_monotonic
            web_manager.time.sleep = old_sleep

    def test_web_manager_read_json_payload_limits_size_and_validates_json(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, body, content_length=None):
                self.rfile = io.BytesIO(body)
                self.wfile = io.BytesIO()
                self.headers = {"Content-Length": str(len(body) if content_length is None else content_length)}
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_limit = web_manager.MAX_JSON_BODY_SIZE
        try:
            web_manager.MAX_JSON_BODY_SIZE = 16

            ok_handler = FakeHandler(b'{"book_id":"x"}')
            self.assertEqual(web_manager.Handler._read_json_payload(ok_handler), {"book_id": "x"})
            self.assertEqual(ok_handler.sent, [])

            large_handler = FakeHandler(b'{"book_id":"too-large"}')
            self.assertIsNone(web_manager.Handler._read_json_payload(large_handler))
            self.assertEqual(large_handler.sent[0][0], 413)

            invalid_json_handler = FakeHandler(b'{"book_id"')
            self.assertIsNone(web_manager.Handler._read_json_payload(invalid_json_handler))
            self.assertEqual(invalid_json_handler.sent[0], (400, {"error": "invalid json"}))

            invalid_length_handler = FakeHandler(b"{}", content_length="bad")
            self.assertIsNone(web_manager.Handler._read_json_payload(invalid_length_handler))
            self.assertEqual(invalid_length_handler.sent[0], (400, {"error": "invalid content length"}))
        finally:
            web_manager.MAX_JSON_BODY_SIZE = old_limit

    def test_web_manager_json_payload_schema_validates_required_fields(self):
        ok, error = web_manager._validate_json_payload_schema(
            {"book_id": "book"},
            web_manager.BOOK_ID_PAYLOAD_SCHEMA,
        )
        self.assertTrue(ok)
        self.assertEqual(error, "")

        self.assertEqual(
            web_manager._validate_json_payload_schema([], web_manager.BOOK_ID_PAYLOAD_SCHEMA),
            (False, "json body must be an object"),
        )
        self.assertEqual(
            web_manager._validate_json_payload_schema({}, web_manager.BOOK_ID_PAYLOAD_SCHEMA),
            (False, "book_id is required"),
        )
        self.assertEqual(
            web_manager._validate_json_payload_schema({"book_id": ""}, web_manager.BOOK_ID_PAYLOAD_SCHEMA),
            (False, "book_id must not be empty"),
        )
        self.assertEqual(
            web_manager._validate_json_payload_schema({"book_ids": ["ok", 1]}, web_manager.BOOK_IDS_PAYLOAD_SCHEMA),
            (False, "book_ids items must be str"),
        )
        self.assertEqual(
            web_manager._validate_json_payload_schema({"book_id": "book", "direction": "left"}, web_manager.MOVE_QUEUE_PAYLOAD_SCHEMA),
            (False, "direction must be one of: down, up"),
        )

    def test_web_manager_post_rejects_invalid_schema_before_business_logic(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, path, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.path = path
                self.headers = {
                    "Content-Length": str(len(body)),
                    "X-Web-Unsafe-Action": "confirm",
                }
                self.rfile = io.BytesIO(body)
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)

            missing_id = FakeHandler("/api/enqueue", {})
            web_manager.Handler.do_POST(missing_id)
            self.assertEqual(missing_id.sent[0], (400, {"error": "book_id is required"}))

            invalid_batch = FakeHandler("/api/delete-batch", {"book_ids": ["ok", 1]})
            web_manager.Handler.do_POST(invalid_batch)
            self.assertEqual(invalid_batch.sent[0], (400, {"error": "book_ids items must be str"}))

            invalid_direction = FakeHandler("/api/move-queue", {"book_id": "book", "direction": "left"})
            web_manager.Handler.do_POST(invalid_direction)
            self.assertEqual(invalid_direction.sent[0], (400, {"error": "direction must be one of: down, up"}))
        finally:
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_upload_errors_return_json(self):
        class FakeHandler(web_manager.Handler):
            def __init__(self, headers=None):
                self.path = "/upload"
                self.headers = {
                    "Content-Length": "0",
                    "Content-Type": "multipart/form-data; boundary=x",
                    "X-Web-Unsafe-Action": "confirm",
                    **(headers or {}),
                }
                self.rfile = io.BytesIO(b"")
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        old_max_upload_size = web_manager.MAX_UPLOAD_SIZE
        old_field_storage = web_manager.cgi.FieldStorage
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)

            invalid_length = FakeHandler({"Content-Length": "bad"})
            web_manager.Handler.do_POST(invalid_length)
            self.assertEqual(invalid_length.sent[0], (400, {"error": "invalid content length"}))

            web_manager.MAX_UPLOAD_SIZE = 10
            too_large = FakeHandler({"Content-Length": str(10 + 1024 * 1024 + 1)})
            web_manager.Handler.do_POST(too_large)
            self.assertEqual(too_large.sent[0], (413, {"error": "file too large, max 10 bytes"}))

            class EmptyForm:
                def __contains__(self, _key):
                    return False

            web_manager.cgi.FieldStorage = lambda *_args, **_kwargs: EmptyForm()
            missing_file = FakeHandler()
            web_manager.Handler.do_POST(missing_file)
            self.assertEqual(missing_file.sent[0], (400, {"error": "missing file"}))

            def fail_field_storage(*_args, **_kwargs):
                raise ValueError("bad multipart")

            web_manager.cgi.FieldStorage = fail_field_storage
            bad_multipart = FakeHandler()
            web_manager.Handler.do_POST(bad_multipart)
            self.assertEqual(bad_multipart.sent[0][0], 400)
            self.assertEqual(bad_multipart.sent[0][1]["error"], "invalid multipart form")
            self.assertIn("bad multipart", bad_multipart.sent[0][1]["detail"])
        finally:
            web_manager.MAX_UPLOAD_SIZE = old_max_upload_size
            web_manager.cgi.FieldStorage = old_field_storage
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_upload_metadata_error_returns_json(self):
        class FakeFileItem:
            filename = "book.txt"

            def __init__(self):
                self.file = io.BytesIO(b"content")

        class FakeForm:
            def __init__(self):
                self.file_item = FakeFileItem()

            def __contains__(self, key):
                return key == "file"

            def __getitem__(self, key):
                if key != "file":
                    raise KeyError(key)
                return self.file_item

            def getlist(self, key):
                return [] if key == "profile" else []

            def getfirst(self, key, default=None):
                return default

        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.path = "/upload"
                self.headers = {
                    "Content-Length": "7",
                    "Content-Type": "multipart/form-data; boundary=x",
                    "X-Web-Unsafe-Action": "confirm",
                }
                self.rfile = io.BytesIO(b"content")
                self.sent = []

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        old_base_dir = web_manager.get_base_dir
        old_state = web_manager.STATE
        old_field_storage = web_manager.cgi.FieldStorage
        old_signature = web_manager._book_suggestion_signature
        old_profile_suggestions = web_manager._profile_suggestions
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "novels"), exist_ok=True)
                os.makedirs(os.path.join(tmp, "results"), exist_ok=True)
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {"books": {}, "tasks": []}
                web_manager.cgi.FieldStorage = lambda *_args, **_kwargs: FakeForm()
                web_manager._profile_suggestions = lambda _path, _book_name: [{"name": "general"}]

                def fail_signature(_path):
                    raise PermissionError("metadata denied")

                web_manager._book_suggestion_signature = fail_signature
                handler = FakeHandler()
                web_manager.Handler.do_POST(handler)

                self.assertEqual(handler.sent[0][0], 500)
                self.assertEqual(handler.sent[0][1]["error"], "uploaded file metadata unavailable")
                self.assertIn("metadata denied", handler.sent[0][1]["detail"])
                self.assertEqual(web_manager.STATE["books"], {})
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.STATE = old_state
            web_manager.cgi.FieldStorage = old_field_storage
            web_manager._book_suggestion_signature = old_signature
            web_manager._profile_suggestions = old_profile_suggestions
            if old_token is None:
                os.environ.pop("WEB_ACCESS_TOKEN", None)
            else:
                os.environ["WEB_ACCESS_TOKEN"] = old_token

    def test_web_manager_send_public_file_streams_in_chunks(self):
        class TrackingWFile:
            def __init__(self):
                self.data = b""
                self.write_sizes = []

            def write(self, data):
                self.write_sizes.append(len(data))
                self.data += data

        class FakeHandler(web_manager.Handler):
            def __init__(self):
                self.wfile = TrackingWFile()
                self.headers_sent = []
                self.errors = []
                self.sent = []

            def send_response(self, code):
                self.response = code

            def send_header(self, key, value):
                self.headers_sent.append((key, value))

            def end_headers(self):
                pass

            def send_error(self, code, message=None):
                self.errors.append((code, message))

            def _send_json(self, data, status=200):
                self.sent.append((status, data))

        old_base_dir = web_manager.get_base_dir
        old_chunk_size = web_manager.FILE_RESPONSE_CHUNK_SIZE
        had_open = hasattr(web_manager, "open")
        old_open = getattr(web_manager, "open", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                path = os.path.join(results_dir, "report.txt")
                with open(path, "wb") as f:
                    f.write(b"abcdefghi")

                web_manager.get_base_dir = lambda: tmp
                web_manager.FILE_RESPONSE_CHUNK_SIZE = 4

                handler = FakeHandler()
                web_manager.Handler._send_public_file(handler, path)

                self.assertEqual(handler.response, 200)
                self.assertEqual(handler.wfile.data, b"abcdefghi")
                self.assertEqual(handler.wfile.write_sizes, [4, 4, 1])
                self.assertIn(("Content-Length", "9"), handler.headers_sent)
                self.assertIn(("Content-Type", "text/plain; charset=utf-8"), handler.headers_sent)

                forbidden_handler = FakeHandler()
                web_manager.Handler._send_public_file(forbidden_handler, os.path.join(tmp, "secret.txt"))
                self.assertEqual(forbidden_handler.sent[0], (403, {"error": "file is not allowed"}))
                self.assertEqual(forbidden_handler.errors, [])

                missing_handler = FakeHandler()
                web_manager.Handler._send_public_file(missing_handler, os.path.join(results_dir, "missing.txt"))
                self.assertEqual(missing_handler.sent[0], (404, {"error": "file not found"}))

                def fail_open(_path, _mode="r", *_args, **_kwargs):
                    raise PermissionError("permission denied")

                web_manager.open = fail_open
                failed_read_handler = FakeHandler()
                web_manager.Handler._send_public_file(failed_read_handler, path)
                self.assertEqual(failed_read_handler.sent[0][0], 500)
                self.assertEqual(failed_read_handler.sent[0][1]["error"], "file read failed")
                self.assertIn("permission denied", failed_read_handler.sent[0][1]["detail"])
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.FILE_RESPONSE_CHUNK_SIZE = old_chunk_size
            if had_open:
                web_manager.open = old_open
            elif hasattr(web_manager, "open"):
                delattr(web_manager, "open")

    def test_web_manager_timeout_http_server_sets_request_timeout(self):
        class FakeSocket:
            def __init__(self):
                self.timeout = None

            def settimeout(self, value):
                self.timeout = value

        server = object.__new__(web_manager.TimeoutHTTPServer)
        server.request_timeout = 12.5
        fake_socket = FakeSocket()

        old_get_request = web_manager.ThreadingHTTPServer.get_request
        try:
            web_manager.ThreadingHTTPServer.get_request = lambda _self: (fake_socket, ("127.0.0.1", 12345))
            request, client = web_manager.TimeoutHTTPServer.get_request(server)

            self.assertIs(request, fake_socket)
            self.assertEqual(client, ("127.0.0.1", 12345))
            self.assertEqual(fake_socket.timeout, 12.5)

            fake_socket.timeout = None
            server.request_timeout = 0
            web_manager.TimeoutHTTPServer.get_request(server)
            self.assertIsNone(fake_socket.timeout)
        finally:
            web_manager.ThreadingHTTPServer.get_request = old_get_request

    def test_web_manager_run_server_uses_loaded_env_host_port_by_default(self):
        created_servers = []

        class FakeServer:
            def __init__(self, address, handler, request_timeout=None):
                self.address = address
                self.handler = handler
                self.request_timeout = request_timeout
                created_servers.append(self)

            def serve_forever(self):
                return None

        old_base_dir = web_manager.get_base_dir
        old_server_class = web_manager.TimeoutHTTPServer
        old_load_state = web_manager._load_state
        old_start_worker_once = web_manager._start_worker_once
        old_config_ready = web_manager.CONFIG_READY
        old_env = {key: os.environ.get(key) for key in ("API_KEY", "API_KEY_POOL", "WEB_HOST", "WEB_PORT")}
        runtime_names = (
            "MAX_UPLOAD_SIZE",
            "MAX_JSON_BODY_SIZE",
            "FILE_RESPONSE_CHUNK_SIZE",
            "WEB_REQUEST_TIMEOUT_SECONDS",
            "SYNC_BOOKS_TTL_SECONDS",
            "OUTPUTS_CACHE_TTL_SECONDS",
            "SSE_STATE_INTERVAL_SECONDS",
            "SSE_SYNC_INTERVAL_SECONDS",
            "SSE_MAX_CONNECTION_SECONDS",
            "SCAN_CANCEL_TIMEOUT_SECONDS",
            "SCAN_HEARTBEAT_INTERVAL_SECONDS",
            "SCAN_STALL_TIMEOUT_SECONDS",
            "SCAN_FUTURE_STALL_TIMEOUT_SECONDS",
        )
        old_runtime = {name: getattr(web_manager, name) for name in runtime_names}
        old_load_configs = web_manager.load_configs
        load_calls = []
        try:
            for key in old_env:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                    f.write(
                        "API_KEY=sk-test\n"
                        "WEB_HOST=0.0.0.0\n"
                        "WEB_PORT=9876\n"
                        "MAX_UPLOAD_SIZE=2048\n"
                        "MAX_JSON_BODY_SIZE=4096\n"
                        "FILE_RESPONSE_CHUNK_SIZE=512\n"
                        "WEB_REQUEST_TIMEOUT=7.5\n"
                        "SYNC_BOOKS_TTL_SECONDS=1.5\n"
                        "OUTPUTS_CACHE_TTL_SECONDS=2.5\n"
                        "SSE_STATE_INTERVAL_SECONDS=0.5\n"
                        "SSE_SYNC_INTERVAL_SECONDS=0.75\n"
                        "SSE_MAX_CONNECTION_SECONDS=9.5\n"
                        "SCAN_CANCEL_TIMEOUT_SECONDS=1.25\n"
                        "SCAN_HEARTBEAT_INTERVAL_SECONDS=1.75\n"
                    )
                web_manager.get_base_dir = lambda: tmp
                web_manager.TimeoutHTTPServer = FakeServer
                web_manager._load_state = lambda: None
                web_manager._start_worker_once = lambda: None
                def tracked_load_configs(base_dir, interactive=True):
                    load_calls.append((base_dir, interactive))
                    return old_load_configs(base_dir, interactive=interactive)

                web_manager.load_configs = tracked_load_configs

                web_manager.run_server()

                self.assertEqual(len(load_calls), 1)
                self.assertEqual(created_servers[0].address, ("0.0.0.0", 9876))
                self.assertTrue(web_manager.CONFIG_READY)
                self.assertEqual(created_servers[0].request_timeout, 7.5)
                summary = web_manager._runtime_config_summary()["web"]
                self.assertEqual(summary["max_upload_size"], 2048)
                self.assertEqual(summary["max_json_body_size"], 4096)
                self.assertEqual(summary["file_response_chunk_size"], 512)
                self.assertEqual(summary["request_timeout"], 7.5)
                self.assertEqual(summary["sync_books_ttl_seconds"], 1.5)
                self.assertEqual(summary["outputs_cache_ttl_seconds"], 2.5)
                self.assertEqual(summary["sse_state_interval_seconds"], 0.5)
                self.assertEqual(summary["sse_sync_interval_seconds"], 0.75)
                self.assertEqual(summary["sse_max_connection_seconds"], 9.5)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.TimeoutHTTPServer = old_server_class
            web_manager._load_state = old_load_state
            web_manager._start_worker_once = old_start_worker_once
            web_manager.load_configs = old_load_configs
            web_manager.CONFIG_READY = old_config_ready
            for name, value in old_runtime.items():
                setattr(web_manager, name, value)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_run_server_explicit_address_overrides_env(self):
        created_servers = []

        class FakeServer:
            def __init__(self, address, handler, request_timeout=None):
                self.address = address
                self.handler = handler
                self.request_timeout = request_timeout
                created_servers.append(self)

            def serve_forever(self):
                return None

        old_base_dir = web_manager.get_base_dir
        old_server_class = web_manager.TimeoutHTTPServer
        old_load_state = web_manager._load_state
        old_start_worker_once = web_manager._start_worker_once
        old_config_ready = web_manager.CONFIG_READY
        old_env = {key: os.environ.get(key) for key in ("API_KEY", "API_KEY_POOL", "WEB_HOST", "WEB_PORT")}
        old_request_timeout = web_manager.WEB_REQUEST_TIMEOUT_SECONDS
        try:
            for key in old_env:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                    f.write("API_KEY=sk-test\nWEB_HOST=0.0.0.0\nWEB_PORT=9876\n")
                web_manager.get_base_dir = lambda: tmp
                web_manager.TimeoutHTTPServer = FakeServer
                web_manager._load_state = lambda: None
                web_manager._start_worker_once = lambda: None

                web_manager.run_server("127.0.0.1", 8765)

                self.assertEqual(created_servers[0].address, ("127.0.0.1", 8765))
                self.assertTrue(web_manager.CONFIG_READY)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.TimeoutHTTPServer = old_server_class
            web_manager._load_state = old_load_state
            web_manager._start_worker_once = old_start_worker_once
            web_manager.CONFIG_READY = old_config_ready
            web_manager.WEB_REQUEST_TIMEOUT_SECONDS = old_request_timeout
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_web_manager_scan_subprocess_parses_result_and_logs_output(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter([
                    "scan started\n",
                    web_manager._WEB_SCAN_RESULT_PREFIX + '{"status":"ok","book_name":"book","profile":"general"}\n',
                ])
                self.return_code = None

            def wait(self):
                self.return_code = 0
                return 0

            def poll(self):
                return self.return_code

        old_popen = web_manager.subprocess.Popen
        try:
            calls = []

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            web_manager.subprocess.Popen = fake_popen
            log_file = io.StringIO()
            result = web_manager._run_scan_subprocess("/tmp/book.txt", ["general"], "run1", log_file)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["profile"], "general")
            self.assertEqual(result["return_code"], 0)
            self.assertGreaterEqual(result["elapsed_seconds"], 0)
            self.assertIn("scan started", log_file.getvalue())
            self.assertNotIn(web_manager._WEB_SCAN_RESULT_PREFIX, log_file.getvalue())
            self.assertIn("--web-scan-task", calls[0][0])
            self.assertIn("--profile-json", calls[0][0])
        finally:
            web_manager.subprocess.Popen = old_popen

    def test_web_manager_scan_subprocess_reports_invalid_result_context(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter([
                    "scan started\n",
                    web_manager._WEB_SCAN_RESULT_PREFIX + '{"status":\n',
                ])
                self.return_code = None

            def wait(self):
                self.return_code = 0
                return 0

            def poll(self):
                return self.return_code

        old_popen = web_manager.subprocess.Popen
        try:
            web_manager.subprocess.Popen = lambda _cmd, **_kwargs: FakeProcess()
            result = web_manager._run_scan_subprocess("/tmp/book.txt", ["general"], "run1", io.StringIO())

            self.assertEqual(result["status"], "fail")
            self.assertIn("invalid scan result", result["error"])
            self.assertEqual(result["return_code"], 0)
            self.assertIn('{"status":', result["last_result_payload_preview"])
            self.assertTrue(result["last_output"].startswith(web_manager._WEB_SCAN_RESULT_PREFIX))
        finally:
            web_manager.subprocess.Popen = old_popen

    def test_web_manager_scan_subprocess_reports_missing_result_context(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter(["scan started\n", "scan failed before result\n"])
                self.return_code = None

            def wait(self):
                self.return_code = 2
                return 2

            def poll(self):
                return self.return_code

        old_popen = web_manager.subprocess.Popen
        try:
            web_manager.subprocess.Popen = lambda _cmd, **_kwargs: FakeProcess()
            result = web_manager._run_scan_subprocess("/tmp/book.txt", ["general"], "run1", io.StringIO())

            self.assertEqual(result["status"], "fail")
            self.assertIn("exited without result", result["error"])
            self.assertEqual(result["return_code"], 2)
            self.assertEqual(result["last_output"], "scan failed before result")
            self.assertFalse(result["killed_by_stall_watchdog"])
        finally:
            web_manager.subprocess.Popen = old_popen

    def test_web_manager_scan_subprocess_emits_heartbeat_for_log_lines(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter([
                    "scan started\n",
                    "scan progress\n",
                    web_manager._WEB_SCAN_RESULT_PREFIX + '{"status":"ok","book_name":"book","profile":"general"}\n',
                ])
                self.return_code = None

            def wait(self):
                self.return_code = 0
                return 0

            def poll(self):
                return self.return_code

        old_popen = web_manager.subprocess.Popen
        old_interval = web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS
        try:
            web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS = 0
            web_manager.subprocess.Popen = lambda _cmd, **_kwargs: FakeProcess()
            heartbeats = []

            result = web_manager._run_scan_subprocess(
                "/tmp/book.txt",
                ["general"],
                "run1",
                io.StringIO(),
                heartbeat_callback=heartbeats.append,
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(heartbeats, ["scan started\n", "scan progress\n"])
        finally:
            web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS = old_interval
            web_manager.subprocess.Popen = old_popen

    def test_web_manager_scan_subprocess_stall_timeout_terminates_process(self):
        class BlockingStdout:
            def __iter__(self):
                return self

            def __next__(self):
                time.sleep(2)
                raise StopIteration

        class FakeProcess:
            def __init__(self):
                self.pid = 23456
                self.stdout = BlockingStdout()
                self.return_code = None
                self.terminated = False
                self.killed = False

            def poll(self):
                return self.return_code

            def terminate(self):
                self.terminated = True
                self.return_code = -15

            def kill(self):
                self.killed = True
                self.return_code = -9

            def wait(self, timeout=None):
                if self.return_code is None:
                    self.return_code = -15
                return self.return_code

        old_popen = web_manager.subprocess.Popen
        old_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_cancel_timeout = web_manager.SCAN_CANCEL_TIMEOUT_SECONDS
        old_killpg = web_manager.os.killpg
        try:
            fake_proc = FakeProcess()
            web_manager.subprocess.Popen = lambda _cmd, **_kwargs: fake_proc
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = 0.01
            web_manager.SCAN_CANCEL_TIMEOUT_SECONDS = 0.01
            web_manager.os.killpg = lambda _pid, _sig: (_ for _ in ()).throw(OSError("no pg"))

            started = time.monotonic()
            result = web_manager._run_scan_subprocess(
                "/tmp/book.txt",
                ["general"],
                "run1",
                io.StringIO(),
            )

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(result["status"], "fail")
            self.assertIn("stalled without output", result["error"])
            self.assertEqual(result["return_code"], -15)
            self.assertTrue(result["killed_by_stall_watchdog"])
            self.assertGreaterEqual(result["elapsed_seconds"], 0)
            self.assertTrue(fake_proc.terminated)
            self.assertFalse(fake_proc.killed)
        finally:
            web_manager.subprocess.Popen = old_popen
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_timeout
            web_manager.SCAN_CANCEL_TIMEOUT_SECONDS = old_cancel_timeout
            web_manager.os.killpg = old_killpg

    def test_web_manager_worker_fails_when_queued_novel_file_disappears(self):
        class OneShotQueue(queue.Queue):
            def __init__(self, first_item):
                super().__init__()
                self.put(first_item)

            def get(self, *args, **kwargs):
                if self.empty():
                    raise SystemExit
                return super().get(*args, **kwargs)

        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_queue = web_manager.TASK_QUEUE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        old_run_scan = web_manager._run_scan_subprocess
        old_save_state = web_manager._save_state
        old_time = web_manager.time.strftime
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                missing_path = os.path.join(novels_dir, "missing.txt")
                task_id = "task-missing-file"
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {
                        "missing": {
                            "id": "missing",
                            "name": "missing",
                            "path": missing_path,
                            "profile": "general",
                            "status": "queued",
                            "task_id": task_id,
                        }
                    },
                    "tasks": [{"id": task_id, "book_id": "missing", "profile": "general", "status": "queued"}],
                }
                web_manager.TASK_QUEUE = OneShotQueue(task_id)
                web_manager.TASK_QUEUE_IDS.clear()
                web_manager.TASK_QUEUE_IDS.add(task_id)
                web_manager._save_state = lambda: None
                web_manager.time.strftime = lambda *_args, **_kwargs: "2026-06-09 12:00:00"
                web_manager._run_scan_subprocess = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("scan subprocess should not start")
                )

                worker = threading.Thread(target=web_manager._worker_loop, daemon=True)
                worker.start()
                web_manager.TASK_QUEUE.join()
                worker.join(timeout=1)

                task = web_manager.STATE["tasks"][0]
                book = web_manager.STATE["books"]["missing"]
                self.assertEqual(task["status"], "failed")
                self.assertEqual(task["error"], "源文件不存在，请重新上传小说文件")
                self.assertEqual(book["status"], "failed")
                self.assertTrue(book["file_missing"])
                self.assertNotIn(task_id, web_manager.TASK_QUEUE_IDS)
                self.assertFalse(worker.is_alive())
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager.TASK_QUEUE = old_queue
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager._run_scan_subprocess = old_run_scan
            web_manager._save_state = old_save_state
            web_manager.time.strftime = old_time

    def test_web_manager_scan_log_heartbeat_updates_running_task(self):
        old_state = web_manager.STATE
        old_save_state = web_manager._save_state
        try:
            saves = []
            web_manager._save_state = lambda: saves.append(True)
            web_manager.STATE = {
                "books": {
                    "book": {"id": "book", "name": "book", "status": "running", "message": "扫描中", "task_id": "t1"}
                },
                "tasks": [{"id": "t1", "book_id": "book", "status": "running"}],
            }

            web_manager._scan_log_heartbeat("t1", "扫描进度 50%\n")

            task = web_manager.STATE["tasks"][0]
            book = web_manager.STATE["books"]["book"]
            self.assertEqual(task["message"], "扫描中")
            self.assertIn("updated_at", task)
            self.assertIn("last_log_at", task)
            self.assertEqual(task["last_log"], "扫描进度 50%")
            self.assertEqual(book["message"], "扫描中")
            self.assertIn("updated_at", book)
            self.assertIn("last_log_at", book)
            self.assertEqual(book["last_log"], "扫描进度 50%")
            self.assertTrue(saves)

            task["status"] = "canceled"
            previous_updated_at = task["updated_at"]
            previous_book_updated_at = book["updated_at"]
            web_manager._scan_log_heartbeat("t1", "不应覆盖\n")
            self.assertEqual(task["updated_at"], previous_updated_at)
            self.assertEqual(task["last_log"], "扫描进度 50%")
            self.assertEqual(book["updated_at"], previous_book_updated_at)
            self.assertEqual(book["last_log"], "扫描进度 50%")
        finally:
            web_manager.STATE = old_state
            web_manager._save_state = old_save_state

    def test_web_manager_diagnostics_reports_running_task_staleness(self):
        old_state = web_manager.STATE
        old_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_app_commit = web_manager.APP_COMMIT
        old_config_ready = web_manager.CONFIG_READY
        old_time = web_manager.time.time
        try:
            with tempfile.TemporaryDirectory() as tmpdir, \
                    mock.patch.object(web_manager, "get_base_dir", return_value=tmpdir):
                log_path = os.path.join(tmpdir, "results", "web_logs", "task-running.log")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("Chunk 367 JSON解析失败")
                failed_log_path = os.path.join(tmpdir, "results", "web_logs", "task-failed-1.log")
                with open(failed_log_path, "w", encoding="utf-8") as f:
                    f.write("Permission denied")
                web_manager.SCAN_STALL_TIMEOUT_SECONDS = 1200
                web_manager.APP_COMMIT = "abc123"
                web_manager.CONFIG_READY = False
                base_time = web_manager.datetime.strptime(
                    "2026-06-09 06:50:00",
                    "%Y-%m-%d %H:%M:%S",
                ).timestamp()
                web_manager.time.time = lambda: base_time + 1800
                web_manager.STATE = {
                    "books": {
                        "running-book": {
                            "id": "running-book",
                            "name": "运行书",
                            "status": "running",
                            "task_id": "task-running",
                            "last_log": "Chunk 367 JSON解析失败",
                            "last_log_at": "2026-06-09 06:50:00",
                        },
                        "queued-book": {
                            "id": "queued-book",
                            "name": "排队书",
                            "status": "queued",
                            "task_id": "task-queued",
                        },
                        "failed-book-1": {
                            "id": "failed-book-1",
                            "name": "失败书1",
                            "status": "failed",
                            "task_id": "task-failed-1",
                        },
                        "failed-book-2": {
                            "id": "failed-book-2",
                            "name": "失败书2",
                            "status": "failed",
                            "task_id": "task-failed-2",
                        },
                        "failed-book-3": {
                            "id": "failed-book-3",
                            "name": "失败书3",
                            "status": "failed",
                            "task_id": "task-failed-3",
                        },
                    },
                    "tasks": [
                        {
                            "id": "task-running",
                            "book_id": "running-book",
                            "profile": "harem",
                            "status": "running",
                            "created_at": "2026-06-09 06:00:00",
                            "started_at": "2026-06-09 06:10:00",
                            "last_log": "Chunk 367 JSON解析失败",
                            "last_log_at": "2026-06-09 06:50:00",
                            "log_path": log_path,
                        },
                        {
                            "id": "task-queued",
                            "book_id": "queued-book",
                            "profile": "general",
                            "status": "queued",
                            "created_at": "2026-06-09 06:30:00",
                        },
                        {
                            "id": "task-failed-1",
                            "book_id": "failed-book-1",
                            "profile": "general",
                            "status": "failed",
                            "created_at": "2026-06-09 05:00:00",
                            "finished_at": "2026-06-09 05:30:00",
                            "error": "PermissionError: [Errno 13] Permission denied",
                            "log_path": failed_log_path,
                        },
                        {
                            "id": "task-failed-2",
                            "book_id": "failed-book-2",
                            "profile": "harem",
                            "status": "failed",
                            "created_at": "2026-06-09 05:10:00",
                            "finished_at": "2026-06-09 05:40:00",
                            "result": {
                                "status": "fail",
                                "error": "storage write failed: Permission denied",
                                "return_code": 2,
                                "elapsed_seconds": 12.5,
                                "last_output": "Permission denied",
                                "last_result_payload_preview": "",
                                "killed_by_stall_watchdog": False,
                            },
                        },
                        {
                            "id": "task-failed-3",
                            "book_id": "failed-book-3",
                            "profile": "history",
                            "status": "failed",
                            "created_at": "2026-06-09 05:20:00",
                            "finished_at": "2026-06-09 05:50:00",
                            "error": "invalid scan result: JSON parse failed",
                        },
                    ],
                }

                diagnostics = web_manager._diagnostics_summary()

            self.assertFalse(diagnostics["ok"])
            self.assertEqual(diagnostics["app"]["commit"], "abc123")
            self.assertEqual(diagnostics["scan_stall_timeout_seconds"], 1200)
            self.assertEqual(diagnostics["queue_length"], 1)
            self.assertEqual(diagnostics["running_count"], 1)
            self.assertEqual(diagnostics["stale_running_count"], 1)
            self.assertEqual(diagnostics["failed_count"], 3)
            self.assertFalse(diagnostics["ready"])
            self.assertTrue(diagnostics["storage_ready"])
            self.assertEqual(
                [item["type"] for item in diagnostics["health_issues"]],
                ["config", "stale_tasks", "failed_tasks"],
            )
            self.assertEqual(diagnostics["health_issues"][1]["count"], 1)
            self.assertEqual(diagnostics["health_issues"][2]["count"], 3)
            self.assertEqual(diagnostics["oldest_queue_wait_seconds"], 3000)
            self.assertEqual(diagnostics["longest_running_seconds"], 4200)
            self.assertEqual(diagnostics["task_counts"]["queued"], 1)
            self.assertEqual(diagnostics["task_counts"]["running"], 1)
            self.assertEqual(diagnostics["task_counts"]["failed"], 3)
            running = diagnostics["running_tasks"][0]
            self.assertEqual(running["task_id"], "task-running")
            self.assertEqual(running["book_name"], "运行书")
            self.assertEqual(running["last_log"], "Chunk 367 JSON解析失败")
            self.assertEqual(running["seconds_since_created"], 4800)
            self.assertEqual(running["seconds_since_started"], 4200)
            self.assertEqual(running["seconds_since_last_log"], 1800)
            self.assertTrue(running["stale_without_log"])
            self.assertTrue(running["log_file"]["url"].startswith("/files?path="))
            queued = diagnostics["queued_tasks"][0]
            self.assertEqual(queued["task_id"], "task-queued")
            self.assertEqual(queued["book_name"], "排队书")
            self.assertEqual(queued["queue_position"], 1)
            self.assertEqual(queued["seconds_since_created"], 3000)
            self.assertEqual(diagnostics["failure_reasons"][0]["reason"], "permission denied")
            self.assertEqual(diagnostics["failure_reasons"][0]["count"], 2)
            self.assertEqual(diagnostics["failure_reasons"][1]["reason"], "json parse failure")
            recent_failed = diagnostics["recent_failed_tasks"]
            self.assertEqual(len(recent_failed), 3)
            self.assertEqual(recent_failed[0]["task_id"], "task-failed-3")
            failed_with_log = next(item for item in recent_failed if item["task_id"] == "task-failed-1")
            self.assertTrue(failed_with_log["log_file"]["url"].startswith("/files?path="))
            failed_with_context = next(item for item in recent_failed if item["task_id"] == "task-failed-2")
            self.assertEqual(failed_with_context["return_code"], 2)
            self.assertEqual(failed_with_context["elapsed_seconds"], 12.5)
            self.assertEqual(failed_with_context["last_output"], "Permission denied")
            self.assertFalse(failed_with_context["killed_by_stall_watchdog"])
            self.assertIn("storage", diagnostics)
        finally:
            web_manager.STATE = old_state
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_timeout
            web_manager.APP_COMMIT = old_app_commit
            web_manager.CONFIG_READY = old_config_ready
            web_manager.time.time = old_time

    def test_web_manager_public_state_includes_profiles_and_suggestions(self):
        old_state = web_manager.STATE
        old_env = {
            key: os.environ.get(key)
            for key in (
                "API_KEY",
                "API_KEY_POOL",
                "BASE_URL",
                "MODEL_NAME",
                "MAX_WORKERS",
                "APP_VERSION",
                "APP_COMMIT",
                "APP_BUILD_DATE",
            )
        }
        old_app_version = web_manager.APP_VERSION
        old_app_commit = web_manager.APP_COMMIT
        old_app_build_date = web_manager.APP_BUILD_DATE
        old_stall_timeout = web_manager.SCAN_STALL_TIMEOUT_SECONDS
        old_heartbeat_interval = web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS
        old_cancel_timeout = web_manager.SCAN_CANCEL_TIMEOUT_SECONDS
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("皇帝与朝廷争论，男主和红颜卷入后宫风波。")
            novel_path = f.name
        try:
            os.environ["API_KEY_POOL"] = "sk-one,sk-two"
            os.environ["API_KEY"] = "sk-one"
            os.environ["BASE_URL"] = "https://example.test/v1"
            os.environ["MODEL_NAME"] = "test-model"
            os.environ["MAX_WORKERS"] = "3"
            web_manager.APP_VERSION = "main"
            web_manager.APP_COMMIT = "abc123"
            web_manager.APP_BUILD_DATE = "2026-06-09T00:00:00Z"
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = 1200
            web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS = 10
            web_manager.SCAN_CANCEL_TIMEOUT_SECONDS = 5
            web_manager.STATE = {
                "books": {"book": {"id": "book", "name": "book", "path": novel_path, "profile": "auto", "status": "idle"}},
                "tasks": [],
            }
            web_manager._refresh_book_suggestions(web_manager.STATE["books"]["book"])
            state = web_manager._public_state()
            self.assertIn("profiles", state)
            self.assertIn("config", state)
            self.assertEqual(state["config"]["base_url"], "https://example.test/v1")
            self.assertEqual(state["config"]["model_name"], "test-model")
            self.assertEqual(state["config"]["max_workers"], "3")
            self.assertTrue(state["config"]["api_key_configured"])
            self.assertEqual(state["config"]["api_key_count"], 2)
            self.assertEqual(state["config"]["app"]["version"], "main")
            self.assertEqual(state["config"]["app"]["commit"], "abc123")
            self.assertEqual(state["config"]["app"]["build_date"], "2026-06-09T00:00:00Z")
            self.assertEqual(state["config"]["web"]["scan_stall_timeout_seconds"], 1200)
            self.assertEqual(state["config"]["web"]["scan_heartbeat_interval_seconds"], 10)
            self.assertEqual(state["config"]["web"]["scan_cancel_timeout_seconds"], 5)
            self.assertTrue(state["config"]["web"]["scan_stall_watchdog_enabled"])
            health = web_manager._health_summary()
            self.assertEqual(health["app"]["commit"], "abc123")
            self.assertTrue(health["scan_stall_watchdog_enabled"])
            self.assertNotIn("sk-one", json.dumps(state, ensure_ascii=False))
            self.assertTrue(any(item["name"] == "history" for item in state["books"][0]["profile_suggestions"]))
            self.assertTrue(any(item["name"] == "harem" for item in state["books"][0]["profile_suggestions"]))
            self.assertTrue(all("rank" in item for item in state["books"][0]["profile_suggestions"]))
            self.assertTrue(any(item.get("auto_selected") for item in state["books"][0]["profile_suggestions"]))
        finally:
            web_manager.STATE = old_state
            web_manager.APP_VERSION = old_app_version
            web_manager.APP_COMMIT = old_app_commit
            web_manager.APP_BUILD_DATE = old_app_build_date
            web_manager.SCAN_STALL_TIMEOUT_SECONDS = old_stall_timeout
            web_manager.SCAN_HEARTBEAT_INTERVAL_SECONDS = old_heartbeat_interval
            web_manager.SCAN_CANCEL_TIMEOUT_SECONDS = old_cancel_timeout
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.unlink(novel_path)

    def test_web_manager_sync_refreshes_suggestions_outside_state_lock(self):
        class TrackingLock:
            def __init__(self):
                self.depth = 0

            def __enter__(self):
                self.depth += 1
                return self

            def __exit__(self, exc_type, exc, tb):
                self.depth -= 1

        old_state = web_manager.STATE
        old_lock = web_manager.STATE_LOCK
        old_base_dir = web_manager.get_base_dir
        old_profile_suggestions = web_manager._profile_suggestions
        old_last_sync = web_manager.LAST_BOOK_SYNC_AT
        old_ttl = web_manager.SYNC_BOOKS_TTL_SECONDS
        lock = TrackingLock()
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "novels"), exist_ok=True)
                os.makedirs(os.path.join(tmp, "results"), exist_ok=True)
                novel_path = os.path.join(tmp, "novels", "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("末世幸存者搜集物资。")

                def fake_profile_suggestions(path, book_name):
                    calls.append((path, book_name, lock.depth))
                    return [{"name": "apocalypse_survival"}]

                web_manager.STATE = {"books": {}, "tasks": []}
                web_manager.STATE_LOCK = lock
                web_manager.get_base_dir = lambda: tmp
                web_manager._profile_suggestions = fake_profile_suggestions
                web_manager.LAST_BOOK_SYNC_AT = 0.0
                web_manager.SYNC_BOOKS_TTL_SECONDS = 0.0

                web_manager._sync_books_from_disk()

                self.assertEqual(calls, [(novel_path, "book", 0)])
                self.assertEqual(web_manager.STATE["books"]["book"]["profile_suggestions"], [{"name": "apocalypse_survival"}])
                self.assertEqual(lock.depth, 0)
        finally:
            web_manager.STATE = old_state
            web_manager.STATE_LOCK = old_lock
            web_manager.get_base_dir = old_base_dir
            web_manager._profile_suggestions = old_profile_suggestions
            web_manager.LAST_BOOK_SYNC_AT = old_last_sync
            web_manager.SYNC_BOOKS_TTL_SECONDS = old_ttl

    def test_web_manager_sync_skips_stale_suggestions_after_file_change(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_profile_suggestions = web_manager._profile_suggestions
        old_last_sync = web_manager.LAST_BOOK_SYNC_AT
        old_ttl = web_manager.SYNC_BOOKS_TTL_SECONDS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "novels"), exist_ok=True)
                os.makedirs(os.path.join(tmp, "results"), exist_ok=True)
                novel_path = os.path.join(tmp, "novels", "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("第一版")

                def fake_profile_suggestions(_path, _book_name):
                    time.sleep(0.01)
                    with open(novel_path, "a", encoding="utf-8") as f:
                        f.write("第二版")
                    return [{"name": "stale"}]

                web_manager.STATE = {"books": {}, "tasks": []}
                web_manager.get_base_dir = lambda: tmp
                web_manager._profile_suggestions = fake_profile_suggestions
                web_manager.LAST_BOOK_SYNC_AT = 0.0
                web_manager.SYNC_BOOKS_TTL_SECONDS = 0.0

                web_manager._sync_books_from_disk()

                book = web_manager.STATE["books"]["book"]
                self.assertNotIn("profile_suggestions", book)
                self.assertNotIn("suggestion_signature", book)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager._profile_suggestions = old_profile_suggestions
            web_manager.LAST_BOOK_SYNC_AT = old_last_sync
            web_manager.SYNC_BOOKS_TTL_SECONDS = old_ttl

    def test_web_manager_sync_does_not_rewrite_unchanged_state(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_profile_suggestions = web_manager._profile_suggestions
        old_save_state = web_manager._save_state
        old_last_sync = web_manager.LAST_BOOK_SYNC_AT
        old_ttl = web_manager.SYNC_BOOKS_TTL_SECONDS
        save_calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "novels"), exist_ok=True)
                os.makedirs(os.path.join(tmp, "results"), exist_ok=True)
                novel_path = os.path.join(tmp, "novels", "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("蒸汽时代侦探调查异常案件。")

                web_manager.STATE = {"books": {}, "tasks": []}
                web_manager.get_base_dir = lambda: tmp
                web_manager._profile_suggestions = lambda _path, _book_name: [{"name": "steampunk_fantasy"}]
                web_manager._save_state = lambda: save_calls.append(json.dumps(web_manager.STATE, sort_keys=True))
                web_manager.LAST_BOOK_SYNC_AT = 0.0
                web_manager.SYNC_BOOKS_TTL_SECONDS = 0.0

                web_manager._sync_books_from_disk()
                web_manager._sync_books_from_disk()

                self.assertEqual(len(save_calls), 1)
                self.assertEqual(web_manager.STATE["books"]["book"]["path"], novel_path)
                self.assertEqual(web_manager.STATE["books"]["book"]["profile_suggestions"], [{"name": "steampunk_fantasy"}])
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager._profile_suggestions = old_profile_suggestions
            web_manager._save_state = old_save_state
            web_manager.LAST_BOOK_SYNC_AT = old_last_sync
            web_manager.SYNC_BOOKS_TTL_SECONDS = old_ttl

    def test_web_manager_sync_marks_missing_novel_files(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_save_state = web_manager._save_state
        old_last_sync = web_manager.LAST_BOOK_SYNC_AT
        old_ttl = web_manager.SYNC_BOOKS_TTL_SECONDS
        save_calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                missing_path = os.path.join(novels_dir, "missing.txt")
                web_manager.STATE = {
                    "books": {
                        "missing": {
                            "id": "missing",
                            "name": "missing",
                            "path": missing_path,
                            "profile": "general",
                            "status": "completed",
                            "message": "完成",
                        }
                    },
                    "tasks": [],
                }
                web_manager.get_base_dir = lambda: tmp
                web_manager._save_state = lambda: save_calls.append(json.dumps(web_manager.STATE, sort_keys=True))
                web_manager.LAST_BOOK_SYNC_AT = 0.0
                web_manager.SYNC_BOOKS_TTL_SECONDS = 0.0

                web_manager._sync_books_from_disk()

                book = web_manager.STATE["books"]["missing"]
                self.assertTrue(book["file_missing"])
                self.assertEqual(book["source_error"], "源文件不存在，请重新上传小说文件")
                self.assertEqual(book["message"], "源文件不存在，请重新上传小说文件")
                self.assertEqual(book["status"], "completed")
                self.assertEqual(len(save_calls), 1)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager._save_state = old_save_state
            web_manager.LAST_BOOK_SYNC_AT = old_last_sync
            web_manager.SYNC_BOOKS_TTL_SECONDS = old_ttl

    def test_web_manager_sync_clears_missing_marker_when_novel_returns(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_profile_suggestions = web_manager._profile_suggestions
        old_save_state = web_manager._save_state
        old_last_sync = web_manager.LAST_BOOK_SYNC_AT
        old_ttl = web_manager.SYNC_BOOKS_TTL_SECONDS
        save_calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                novel_path = os.path.join(novels_dir, "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("正文")
                web_manager.STATE = {
                    "books": {
                        "book": {
                            "id": "book",
                            "name": "book",
                            "path": novel_path,
                            "profile": "general",
                            "status": "failed",
                            "file_missing": True,
                            "source_error": "源文件不存在，请重新上传小说文件",
                            "message": "源文件不存在，请重新上传小说文件",
                        }
                    },
                    "tasks": [],
                }
                web_manager.get_base_dir = lambda: tmp
                web_manager._profile_suggestions = lambda _path, _book_name: [{"name": "general"}]
                web_manager._save_state = lambda: save_calls.append(json.dumps(web_manager.STATE, sort_keys=True))
                web_manager.LAST_BOOK_SYNC_AT = 0.0
                web_manager.SYNC_BOOKS_TTL_SECONDS = 0.0

                web_manager._sync_books_from_disk()

                book = web_manager.STATE["books"]["book"]
                self.assertNotIn("file_missing", book)
                self.assertNotIn("source_error", book)
                self.assertNotIn("message", book)
                self.assertEqual(book["profile_suggestions"], [{"name": "general"}])
                self.assertEqual(len(save_calls), 1)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager._profile_suggestions = old_profile_suggestions
            web_manager._save_state = old_save_state
            web_manager.LAST_BOOK_SYNC_AT = old_last_sync
            web_manager.SYNC_BOOKS_TTL_SECONDS = old_ttl

    def test_web_manager_recovers_incomplete_tasks_and_queue_positions(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "queued_book": {"id": "queued_book", "name": "queued_book", "profile": "general", "status": "queued", "task_id": "queued_task"},
                    "running_book": {"id": "running_book", "name": "running_book", "profile": "general", "status": "running", "task_id": "running_task"},
                },
                "tasks": [
                    {"id": "queued_task", "book_id": "queued_book", "status": "queued", "created_at": "2026-01-01 00:00:01"},
                    {"id": "running_task", "book_id": "running_book", "status": "running", "created_at": "2026-01-01 00:00:02"},
                ],
            }

            web_manager._recover_incomplete_tasks()
            state = web_manager._public_state()
            books = {book["id"]: book for book in state["books"]}
            tasks = {task["id"]: task for task in state["tasks"]}

            self.assertEqual(books["queued_book"]["queue_position"], 1)
            self.assertEqual(tasks["queued_task"]["queue_position"], 1)
            self.assertEqual(tasks["running_task"]["status"], "interrupted")
            self.assertEqual(books["running_book"]["status"], "interrupted")
            self.assertEqual(web_manager.TASK_QUEUE.qsize(), 1)

            web_manager._recover_incomplete_tasks()
            self.assertEqual(web_manager.TASK_QUEUE.qsize(), 1)
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_enqueue_many_dedupes_and_reports_skips(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "ready": {"id": "ready", "name": "ready", "path": "/tmp/ready.txt", "profile": "general", "status": "idle"},
                    "busy": {"id": "busy", "name": "busy", "path": "/tmp/busy.txt", "profile": "general", "status": "queued"},
                },
                "tasks": [],
            }

            result = web_manager._enqueue_many(["ready", "ready", "busy", "missing", ""])

            self.assertEqual([item["book_id"] for item in result["queued"]], ["ready"])
            self.assertEqual(web_manager.STATE["books"]["ready"]["status"], "queued")
            self.assertEqual(web_manager.TASK_QUEUE.qsize(), 1)
            skipped = {item["book_id"]: item["reason"] for item in result["skipped"]}
            self.assertEqual(skipped["busy"], "book already queued or running")
            self.assertEqual(skipped["missing"], "book not found")
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_enqueue_rejects_missing_novel_file(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        old_save_state = web_manager._save_state
        save_calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                missing_path = os.path.join(novels_dir, "missing.txt")
                web_manager.get_base_dir = lambda: tmp
                web_manager.TASK_QUEUE_IDS.clear()
                while not web_manager.TASK_QUEUE.empty():
                    web_manager.TASK_QUEUE.get_nowait()
                    web_manager.TASK_QUEUE.task_done()
                web_manager.STATE = {
                    "books": {
                        "missing": {
                            "id": "missing",
                            "name": "missing",
                            "path": missing_path,
                            "profile": "general",
                            "status": "idle",
                        }
                    },
                    "tasks": [],
                }
                web_manager._save_state = lambda: save_calls.append(json.dumps(web_manager.STATE, sort_keys=True))

                ok, result = web_manager._enqueue("missing")

                self.assertFalse(ok)
                self.assertEqual(result, "源文件不存在，请重新上传小说文件")
                self.assertEqual(web_manager.STATE["tasks"], [])
                self.assertTrue(web_manager.STATE["books"]["missing"]["file_missing"])
                self.assertEqual(len(save_calls), 1)
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager._save_state = old_save_state

    def test_web_manager_retry_failed_tasks_by_type_uses_latest_failed_task(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "api": {"id": "api", "name": "api", "path": "/tmp/api.txt", "profile": "general", "status": "failed"},
                    "missing_key": {"id": "missing_key", "name": "missing_key", "path": "/tmp/missing_key.txt", "profile": "general", "status": "failed"},
                    "parse": {"id": "parse", "name": "parse", "path": "/tmp/parse.txt", "profile": "general", "status": "failed"},
                    "done": {"id": "done", "name": "done", "path": "/tmp/done.txt", "profile": "general", "status": "completed"},
                },
                "tasks": [
                    {"id": "old-api", "book_id": "api", "status": "failed", "finished_at": "2026-01-01 00:00:00", "error": "JSON解析失败"},
                    {"id": "new-api", "book_id": "api", "status": "failed", "finished_at": "2026-01-02 00:00:00", "error": "服务器错误(504)"},
                    {"id": "missing-key-task", "book_id": "missing_key", "status": "failed", "finished_at": "2026-01-02 00:00:00", "error": "未读取到任何 API Key"},
                    {"id": "parse-task", "book_id": "parse", "status": "failed", "finished_at": "2026-01-02 00:00:00", "error": "JSON解析失败: truncated"},
                    {"id": "done-task", "book_id": "done", "status": "failed", "finished_at": "2026-01-02 00:00:00", "error": "服务器错误(504)"},
                ],
            }

            result = web_manager._retry_failed_tasks_by_type(["api_failure"])

            self.assertEqual([item["book_id"] for item in result["matched"]], ["api"])
            self.assertEqual([item["book_id"] for item in result["queued"]], ["api"])
            self.assertEqual(web_manager.STATE["books"]["api"]["status"], "queued")
            self.assertEqual(web_manager.STATE["books"]["missing_key"]["status"], "failed")
            self.assertEqual(web_manager.STATE["books"]["parse"]["status"], "failed")
            self.assertEqual(web_manager.STATE["books"]["done"]["status"], "completed")
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_prioritize_queued_book_moves_task_to_front(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "first": {"id": "first", "name": "first", "path": "/tmp/first.txt", "profile": "general", "status": "idle"},
                    "second": {"id": "second", "name": "second", "path": "/tmp/second.txt", "profile": "general", "status": "idle"},
                    "third": {"id": "third", "name": "third", "path": "/tmp/third.txt", "profile": "general", "status": "idle"},
                },
                "tasks": [],
            }

            for book_id in ("first", "second", "third"):
                ok, _task_id = web_manager._enqueue(book_id)
                self.assertTrue(ok)

            ok, result = web_manager._prioritize_queued_book("third")

            self.assertTrue(ok)
            self.assertEqual(result, web_manager.STATE["books"]["third"]["task_id"])
            queue_ids = list(web_manager.TASK_QUEUE.queue)
            self.assertEqual(queue_ids[0], web_manager.STATE["books"]["third"]["task_id"])
            state = web_manager._public_state()
            books = {book["id"]: book for book in state["books"]}
            self.assertEqual(books["third"]["queue_position"], 1)
            self.assertEqual(books["first"]["queue_position"], 2)
            self.assertEqual(books["second"]["queue_position"], 3)
            self.assertEqual(web_manager._prioritize_queued_book("missing"), (False, "book not found"))
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_move_queued_book_changes_queue_order(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "first": {"id": "first", "name": "first", "path": "/tmp/first.txt", "profile": "general", "status": "idle"},
                    "second": {"id": "second", "name": "second", "path": "/tmp/second.txt", "profile": "general", "status": "idle"},
                    "third": {"id": "third", "name": "third", "path": "/tmp/third.txt", "profile": "general", "status": "idle"},
                },
                "tasks": [],
            }
            for book_id in ("first", "second", "third"):
                ok, _task_id = web_manager._enqueue(book_id)
                self.assertTrue(ok)

            self.assertEqual(web_manager._move_queued_book("first", "up"), (False, "already at boundary"))
            ok, _task_id = web_manager._move_queued_book("second", "up")
            self.assertTrue(ok)

            queue_ids = list(web_manager.TASK_QUEUE.queue)
            self.assertEqual(queue_ids[0], web_manager.STATE["books"]["second"]["task_id"])
            self.assertEqual(queue_ids[1], web_manager.STATE["books"]["first"]["task_id"])
            books = {book["id"]: book for book in web_manager._public_state()["books"]}
            self.assertEqual(books["second"]["queue_position"], 1)
            self.assertEqual(books["first"]["queue_position"], 2)

            ok, _task_id = web_manager._move_queued_book("second", "down")
            self.assertTrue(ok)
            queue_ids = list(web_manager.TASK_QUEUE.queue)
            self.assertEqual(queue_ids[0], web_manager.STATE["books"]["first"]["task_id"])
            self.assertEqual(queue_ids[1], web_manager.STATE["books"]["second"]["task_id"])
            self.assertEqual(web_manager._move_queued_book("second", "bad"), (False, "invalid direction"))
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_cancel_queued_book_marks_task_canceled(self):
        old_state = web_manager.STATE
        old_queue_ids = set(web_manager.TASK_QUEUE_IDS)
        try:
            web_manager.TASK_QUEUE_IDS.clear()
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.STATE = {
                "books": {
                    "ready": {"id": "ready", "name": "ready", "path": "/tmp/ready.txt", "profile": "general", "status": "idle"},
                },
                "tasks": [],
            }
            ok, task_id = web_manager._enqueue("ready")
            self.assertTrue(ok)
            self.assertIn(task_id, web_manager.TASK_QUEUE_IDS)

            ok, result = web_manager._cancel_queued_book("ready")

            self.assertTrue(ok)
            self.assertEqual(result, task_id)
            self.assertNotIn(task_id, web_manager.TASK_QUEUE_IDS)
            self.assertEqual(web_manager.STATE["books"]["ready"]["status"], "idle")
            self.assertEqual(web_manager.STATE["books"]["ready"]["message"], "已取消排队")
            self.assertNotIn("task_id", web_manager.STATE["books"]["ready"])
            self.assertEqual(web_manager.STATE["tasks"][0]["status"], "canceled")
            self.assertEqual(web_manager.STATE["tasks"][0]["error"], "用户取消排队")
        finally:
            while not web_manager.TASK_QUEUE.empty():
                web_manager.TASK_QUEUE.get_nowait()
                web_manager.TASK_QUEUE.task_done()
            web_manager.TASK_QUEUE_IDS.clear()
            web_manager.TASK_QUEUE_IDS.update(old_queue_ids)
            web_manager.STATE = old_state

    def test_web_manager_cancel_rejects_non_queued_books(self):
        old_state = web_manager.STATE
        try:
            web_manager.STATE = {
                "books": {
                    "idle": {"id": "idle", "name": "idle", "profile": "general", "status": "idle"},
                },
                "tasks": [],
            }

            self.assertEqual(web_manager._cancel_queued_book("missing"), (False, "book not found"))
            self.assertEqual(web_manager._cancel_queued_book("idle"), (False, "book is not queued or running"))
        finally:
            web_manager.STATE = old_state

    def test_web_manager_cancel_running_book_marks_task_canceled_and_terminates_process(self):
        class FakeProcess:
            def __init__(self):
                self.pid = 12345
                self.terminated = False
                self.killed = False

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return -15

        old_state = web_manager.STATE
        old_procs = dict(web_manager.RUNNING_TASK_PROCS)
        old_killpg = web_manager.os.killpg
        try:
            fake_proc = FakeProcess()
            killpg_calls = []
            web_manager.os.killpg = lambda pid, sig: killpg_calls.append((pid, sig))
            web_manager.RUNNING_TASK_PROCS.clear()
            web_manager.RUNNING_TASK_PROCS["t1"] = fake_proc
            web_manager.STATE = {
                "books": {
                    "running": {
                        "id": "running",
                        "name": "running",
                        "profile": "general",
                        "status": "running",
                        "task_id": "t1",
                    },
                },
                "tasks": [{"id": "t1", "book_id": "running", "status": "running"}],
            }

            ok, task_id = web_manager._cancel_queued_book("running")

            self.assertTrue(ok)
            self.assertEqual(task_id, "t1")
            self.assertEqual(web_manager.STATE["tasks"][0]["status"], "canceled")
            self.assertEqual(web_manager.STATE["tasks"][0]["error"], "用户取消扫描")
            self.assertEqual(web_manager.STATE["books"]["running"]["status"], "idle")
            self.assertEqual(web_manager.STATE["books"]["running"]["message"], "已取消扫描")
            self.assertNotIn("task_id", web_manager.STATE["books"]["running"])
            self.assertEqual(killpg_calls, [(12345, web_manager.signal.SIGTERM)])
            self.assertFalse(fake_proc.killed)
        finally:
            web_manager.os.killpg = old_killpg
            web_manager.RUNNING_TASK_PROCS.clear()
            web_manager.RUNNING_TASK_PROCS.update(old_procs)
            web_manager.STATE = old_state

    def test_web_manager_delete_book_removes_novel_and_state(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                results_dir = os.path.join(tmp, "results")
                os.makedirs(novels_dir, exist_ok=True)
                os.makedirs(results_dir, exist_ok=True)
                novel_path = os.path.join(novels_dir, "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("正文")
                web_manager.get_base_dir = lambda: tmp
                web_manager.OUTPUTS_CACHE[web_manager._outputs_cache_key("book")] = {"time": time.monotonic(), "outputs": []}
                web_manager.STATE = {
                    "books": {"book": {"id": "book", "name": "book", "path": novel_path, "profile": "general", "status": "idle"}},
                    "tasks": [{"id": "task", "book_id": "book", "status": "completed"}],
                }

                ok, result = web_manager._delete_book("book")

                self.assertTrue(ok)
                self.assertEqual(result, "book")
                self.assertFalse(os.path.exists(novel_path))
                self.assertNotIn("book", web_manager.STATE["books"])
                self.assertTrue(web_manager.STATE["tasks"][0]["book_deleted"])
                self.assertNotIn(web_manager._outputs_cache_key("book"), web_manager.OUTPUTS_CACHE)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)

    def test_web_manager_delete_many_books_reports_deleted_and_skipped(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                paths = {}
                for book_id in ("one", "two", "busy"):
                    path = os.path.join(novels_dir, f"{book_id}.txt")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(book_id)
                    paths[book_id] = path
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {
                        "one": {"id": "one", "name": "one", "path": paths["one"], "profile": "general", "status": "idle"},
                        "two": {"id": "two", "name": "two", "path": paths["two"], "profile": "general", "status": "idle"},
                        "busy": {"id": "busy", "name": "busy", "path": paths["busy"], "profile": "general", "status": "queued"},
                    },
                    "tasks": [],
                }

                result = web_manager._delete_many_books(["one", "one", "busy", "missing", "two"])

                self.assertEqual([item["book_id"] for item in result["deleted"]], ["one", "two"])
                skipped = {item["book_id"]: item["reason"] for item in result["skipped"]}
                self.assertEqual(skipped["busy"], "book is queued or running")
                self.assertEqual(skipped["missing"], "book not found")
                self.assertFalse(os.path.exists(paths["one"]))
                self.assertFalse(os.path.exists(paths["two"]))
                self.assertTrue(os.path.exists(paths["busy"]))
                self.assertNotIn("one", web_manager.STATE["books"])
                self.assertNotIn("two", web_manager.STATE["books"])
                self.assertIn("busy", web_manager.STATE["books"])
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_delete_book_rejects_busy_or_external_path(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                safe_path = os.path.join(novels_dir, "busy.txt")
                outside_path = os.path.join(tmp, "outside.txt")
                with open(safe_path, "w", encoding="utf-8") as f:
                    f.write("忙碌")
                with open(outside_path, "w", encoding="utf-8") as f:
                    f.write("外部")
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {
                        "queued": {"id": "queued", "name": "queued", "path": safe_path, "profile": "general", "status": "queued"},
                        "external": {"id": "external", "name": "external", "path": outside_path, "profile": "general", "status": "idle"},
                    },
                    "tasks": [],
                }

                self.assertEqual(web_manager._delete_book("queued"), (False, "book is queued or running"))
                self.assertEqual(web_manager._delete_book("external"), (False, "novel file is not allowed"))
                self.assertTrue(os.path.exists(safe_path))
                self.assertTrue(os.path.exists(outside_path))
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_upload_target_rejects_duplicate_without_overwrite(self):
        old_state = web_manager.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "book.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("旧文件")
                web_manager.STATE = {
                    "books": {"book": {"id": "book", "name": "book", "path": path, "status": "idle"}},
                    "tasks": [],
                }

                self.assertEqual(web_manager._validate_upload_target("book", path, overwrite=False), (False, "file already exists"))
                self.assertEqual(web_manager._validate_upload_target("book", path, overwrite=True), (True, ""))
                web_manager.STATE["books"]["book"]["status"] = "queued"
                self.assertEqual(web_manager._validate_upload_target("book", path, overwrite=True), (False, "book is queued or running"))
        finally:
            web_manager.STATE = old_state

    def test_web_manager_finds_dynamic_profile_outputs(self):
        results_dir = os.path.join(main.get_base_dir(), "results")
        os.makedirs(results_dir, exist_ok=True)
        out_path = os.path.join(results_dir, "book_history_GENERAL_SUMMARY_latest.json")
        final_path = os.path.join(results_dir, "《book》扫书报告_20260607_010203.txt")
        checkpoint_path = os.path.join(results_dir, "report_checkpoint.json")
        old_checkpoint = None
        if os.path.exists(checkpoint_path):
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                old_checkpoint = f.read()
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("{}")
        with open(final_path, "w", encoding="utf-8") as f:
            f.write("report")
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump({"jobs": {"harem::book": {"book_key": "book", "out_file": final_path}}}, f)
        try:
            web_manager._invalidate_book_outputs("book")
            outputs = web_manager._find_book_outputs("book")
            self.assertTrue(any(item["name"] == "book_history_GENERAL_SUMMARY_latest.json" for item in outputs))
            self.assertTrue(any(item["name"] == "《book》扫书报告_20260607_010203.txt" for item in outputs))
            self.assertEqual(
                next(item for item in outputs if item["name"] == "《book》扫书报告_20260607_010203.txt").get("kind"),
                "final_report",
            )
        finally:
            for path in (out_path, final_path):
                if os.path.exists(path):
                    os.unlink(path)
            if old_checkpoint is None:
                if os.path.exists(checkpoint_path):
                    os.unlink(checkpoint_path)
            else:
                with open(checkpoint_path, "w", encoding="utf-8") as f:
                    f.write(old_checkpoint)

    def test_web_manager_book_outputs_do_not_match_substring_books(self):
        old_base_dir = web_manager.get_base_dir
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                files = [
                    "九锡_GENERAL_SUMMARY_latest.json",
                    "九锡扫书报告_20260609.txt",
                    "大九锡扫书报告_20260609.txt",
                    "九锡外传_GENERAL_SUMMARY_latest.json",
                ]
                for filename in files:
                    with open(os.path.join(results_dir, filename), "w", encoding="utf-8") as f:
                        f.write("{}")
                right_dir = os.path.join(results_dir, "九锡_scan_20260609")
                wrong_dir = os.path.join(results_dir, "九锡外传_scan_20260609")
                os.makedirs(right_dir)
                os.makedirs(wrong_dir)
                with open(os.path.join(right_dir, "GENERAL_SUMMARY.json"), "w", encoding="utf-8") as f:
                    f.write("{}")
                with open(os.path.join(wrong_dir, "GENERAL_SUMMARY.json"), "w", encoding="utf-8") as f:
                    f.write("{}")
                web_manager.get_base_dir = lambda: tmp
                web_manager.OUTPUTS_CACHE.clear()

                outputs = web_manager._find_book_outputs("九锡")

                names = {item["name"] for item in outputs}
                self.assertIn("九锡_GENERAL_SUMMARY_latest.json", names)
                self.assertIn("九锡扫书报告_20260609.txt", names)
                self.assertIn("GENERAL_SUMMARY.json", names)
                self.assertNotIn("大九锡扫书报告_20260609.txt", names)
                self.assertNotIn("九锡外传_GENERAL_SUMMARY_latest.json", names)
                paths = {item["path"] for item in outputs}
                self.assertNotIn(os.path.join(wrong_dir, "GENERAL_SUMMARY.json"), paths)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)

    def test_web_manager_uses_persisted_output_index_without_walking_results(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_os_walk = web_manager.os.walk
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                report_path = os.path.join(results_dir, "book_report.txt")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write("report")
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {
                        "book": {
                            "id": "book",
                            "name": "book",
                            "profile": "general",
                            "status": "completed",
                            "output_index": [{"path": report_path, "kind": "final_report"}],
                        }
                    },
                    "tasks": [],
                }
                web_manager.OUTPUTS_CACHE.clear()

                def fail_walk(*_args, **_kwargs):
                    raise AssertionError("results directory should not be walked when output_index is valid")

                web_manager.os.walk = fail_walk

                outputs = web_manager._find_book_outputs("book")

                self.assertEqual([item["path"] for item in outputs], [report_path])
                self.assertEqual(outputs[0]["kind"], "final_report")
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager.os.walk = old_os_walk
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)

    def test_web_manager_records_output_index_from_scan_result(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                final_path = os.path.join(results_dir, "《book》扫书报告_20260607_010203.txt")
                summary_path = os.path.join(results_dir, "book_history_GENERAL_SUMMARY_latest.json")
                missing_path = os.path.join(results_dir, "missing.txt")
                for path, content in ((final_path, "report"), (summary_path, "{}")):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {"book": {"id": "book", "name": "book", "profile": ["harem", "history"], "status": "completed"}},
                    "tasks": [],
                }
                web_manager.OUTPUTS_CACHE[web_manager._outputs_cache_key("book")] = {"time": time.monotonic(), "outputs": []}

                outputs = web_manager._record_book_outputs_from_result(
                    "book",
                    {
                        "status": "ok",
                        "profiles": ["harem", "history"],
                        "results": [
                            {"status": "ok", "profile": "harem", "out_file": final_path},
                            {"status": "ok", "profile": "history", "out_file": missing_path},
                        ],
                    },
                    ["harem", "history"],
                )

                names = {item["name"]: item for item in outputs}
                self.assertIn("《book》扫书报告_20260607_010203.txt", names)
                self.assertIn("book_history_GENERAL_SUMMARY_latest.json", names)
                self.assertNotIn("missing.txt", names)
                self.assertEqual(names["《book》扫书报告_20260607_010203.txt"]["kind"], "final_report")
                self.assertEqual(names["book_history_GENERAL_SUMMARY_latest.json"]["kind"], "summary")
                self.assertEqual(web_manager.STATE["books"]["book"]["output_index"], outputs)
                self.assertNotIn(web_manager._outputs_cache_key("book"), web_manager.OUTPUTS_CACHE)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)

    def test_web_manager_caches_empty_outputs_and_invalidates_by_book(self):
        old_base_dir = web_manager.get_base_dir
        old_ttl = web_manager.OUTPUTS_CACHE_TTL_SECONDS
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        old_os_walk = web_manager.os.walk
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                web_manager.get_base_dir = lambda: tmp
                web_manager.OUTPUTS_CACHE_TTL_SECONDS = 60
                web_manager.OUTPUTS_CACHE.clear()

                def tracking_walk(*args, **kwargs):
                    calls.append(args[0])
                    return old_os_walk(*args, **kwargs)

                web_manager.os.walk = tracking_walk

                self.assertEqual(web_manager._find_book_outputs("book"), [])
                self.assertEqual(web_manager._find_book_outputs("book"), [])
                self.assertEqual(len(calls), 1)

                out_path = os.path.join(results_dir, "book_GENERAL_SUMMARY_latest.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("{}")
                self.assertEqual(web_manager._find_book_outputs("book"), [])

                web_manager._invalidate_book_outputs("book")
                outputs = web_manager._find_book_outputs("book")
                self.assertTrue(any(item["name"] == "book_GENERAL_SUMMARY_latest.json" for item in outputs))
                self.assertEqual(len(calls), 2)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE_TTL_SECONDS = old_ttl
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)
            web_manager.os.walk = old_os_walk

    def test_web_manager_drops_stale_cached_output_links(self):
        old_base_dir = web_manager.get_base_dir
        old_ttl = web_manager.OUTPUTS_CACHE_TTL_SECONDS
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        old_os_walk = web_manager.os.walk
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                os.makedirs(results_dir, exist_ok=True)
                out_path = os.path.join(results_dir, "book_GENERAL_SUMMARY_latest.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("{}")
                web_manager.get_base_dir = lambda: tmp
                web_manager.OUTPUTS_CACHE_TTL_SECONDS = 60
                web_manager.OUTPUTS_CACHE.clear()

                def tracking_walk(*args, **kwargs):
                    calls.append(args[0])
                    return old_os_walk(*args, **kwargs)

                web_manager.os.walk = tracking_walk

                outputs = web_manager._find_book_outputs("book")
                self.assertEqual([item["path"] for item in outputs], [out_path])
                self.assertEqual(len(calls), 1)

                os.unlink(out_path)

                self.assertEqual(web_manager._find_book_outputs("book"), [])
                self.assertEqual(len(calls), 2)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE_TTL_SECONDS = old_ttl
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)
            web_manager.os.walk = old_os_walk

    def test_web_manager_book_detail_adds_log_link(self):
        task_id = "testtask"
        log_path = web_manager._task_log_path(task_id)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("log")
        old_state = web_manager.STATE
        try:
            web_manager.STATE = {
                "books": {"book": {"id": "book", "name": "book", "path": log_path, "profile": "general"}},
                "tasks": [{"id": task_id, "book_id": "book", "log_path": log_path}],
            }
            detail = web_manager._book_detail("book")
            self.assertIn("log_file", detail["tasks"][0])
            self.assertTrue(detail["tasks"][0]["log_file"]["url"].startswith("/files?path="))
        finally:
            web_manager.STATE = old_state
            os.unlink(log_path)

    def test_web_manager_book_detail_includes_token_usage_summary(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(results_dir, exist_ok=True)
                os.makedirs(novels_dir, exist_ok=True)
                novel_path = os.path.join(novels_dir, "book.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("正文")
                token_path = os.path.join(results_dir, "token_usage.json")
                with open(token_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "schema_version": 3,
                        "books": {
                            "book": {
                                "book_name": "book",
                                "book_total_input": 100,
                                "book_total_output": 40,
                                "book_total_tokens": 140,
                                "updated_at": "2026-01-01T00:02:00",
                                "runs": {
                                    "run1": {
                                        "run_id": "run1",
                                        "run_total_input": 30,
                                        "run_total_output": 10,
                                        "run_total_tokens": 40,
                                        "started_at": "2026-01-01T00:00:00",
                                        "updated_at": "2026-01-01T00:01:00",
                                        "scripts": {"protagonist": {"total": 40}},
                                    },
                                    "run2": {
                                        "run_id": "run2",
                                        "run_total_input": 70,
                                        "run_total_output": 30,
                                        "run_total_tokens": 100,
                                        "started_at": "2026-01-01T00:01:00",
                                        "updated_at": "2026-01-01T00:02:00",
                                        "scripts": {"scan": {"total": 60}, "report": {"total": 40}},
                                    },
                                },
                            }
                        },
                    }, f, ensure_ascii=False)
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {"book": {"id": "book", "name": "book", "path": novel_path, "profile": "general", "status": "idle"}},
                    "tasks": [],
                }

                detail = web_manager._book_detail("book")

                usage = detail["token_usage"]
                self.assertEqual(usage["input"], 100)
                self.assertEqual(usage["output"], 40)
                self.assertEqual(usage["total"], 140)
                self.assertEqual(usage["run_count"], 2)
                self.assertEqual([run["run_id"] for run in usage["runs"]], ["run2", "run1"])
                self.assertEqual(usage["runs"][0]["script_count"], 2)
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir

    def test_web_manager_book_detail_filters_replaced_book_history(self):
        old_state = web_manager.STATE
        old_base_dir = web_manager.get_base_dir
        old_cache = dict(web_manager.OUTPUTS_CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                results_dir = os.path.join(tmp, "results")
                novels_dir = os.path.join(tmp, "novels")
                os.makedirs(results_dir, exist_ok=True)
                os.makedirs(novels_dir, exist_ok=True)
                novel_path = os.path.join(novels_dir, "book.txt")
                old_report = os.path.join(results_dir, "book_old_report.txt")
                new_report = os.path.join(results_dir, "book_new_report.txt")
                for path, content in ((novel_path, "新正文"), (old_report, "old"), (new_report, "new")):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                os.utime(old_report, (1000, 1000))
                os.utime(new_report, (3000, 3000))
                token_path = os.path.join(results_dir, "token_usage.json")
                with open(token_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "books": {
                            "book": {
                                "book_name": "book",
                                "book_total_input": 300,
                                "book_total_output": 120,
                                "book_total_tokens": 420,
                                "runs": {
                                    "old": {
                                        "run_id": "old",
                                        "run_total_input": 100,
                                        "run_total_output": 40,
                                        "run_total_tokens": 140,
                                        "updated_at": "2026-01-01T00:00:00",
                                        "scripts": {"scan": {"total": 140}},
                                    },
                                    "new": {
                                        "run_id": "new",
                                        "run_total_input": 200,
                                        "run_total_output": 80,
                                        "run_total_tokens": 280,
                                        "updated_at": "2026-01-01T00:10:00",
                                        "scripts": {"scan": {"total": 280}},
                                    },
                                },
                            }
                        }
                    }, f, ensure_ascii=False)
                web_manager.get_base_dir = lambda: tmp
                web_manager.STATE = {
                    "books": {
                        "book": {
                            "id": "book",
                            "name": "book",
                            "path": novel_path,
                            "profile": "general",
                            "status": "idle",
                            "output_index": [
                                {"path": old_report, "kind": "final_report"},
                                {"path": new_report, "kind": "final_report"},
                            ],
                            "history_reset_at": "2026-01-01 00:05:00",
                            "outputs_reset_after": 2000,
                        }
                    },
                    "tasks": [
                        {"id": "old-task", "book_id": "book", "status": "completed", "created_at": "2026-01-01 00:00:00"},
                        {"id": "new-task", "book_id": "book", "status": "completed", "created_at": "2026-01-01 00:10:00"},
                    ],
                }
                web_manager.OUTPUTS_CACHE.clear()

                detail = web_manager._book_detail("book")

                self.assertEqual([task["id"] for task in detail["tasks"]], ["new-task"])
                self.assertEqual([item["name"] for item in detail["outputs"]], ["book_new_report.txt"])
                self.assertEqual(detail["token_usage"]["input"], 200)
                self.assertEqual(detail["token_usage"]["output"], 80)
                self.assertEqual(detail["token_usage"]["total"], 280)
                self.assertEqual([run["run_id"] for run in detail["token_usage"]["runs"]], ["new"])
        finally:
            web_manager.STATE = old_state
            web_manager.get_base_dir = old_base_dir
            web_manager.OUTPUTS_CACHE.clear()
            web_manager.OUTPUTS_CACHE.update(old_cache)

    def test_general_report_uses_story_summary_and_characters(self):
        general_summary = {
            "profile_display_name": "历史小说专长分析",
            "summary_fields": ["main_plot", "historical_logic"],
            "summary": {
                "story_overview": "主角追查旧案，牵出沈家与巡查司的冲突。",
                "main_plot": ["主角追查旧案"],
                "historical_logic": ["官制与地方治理形成冲突"],
                "core_conflicts": ["巡查司与沈家对立"],
                "worldbuilding": ["架空王朝"],
                "themes": ["真相与代价"],
                "foreshadowing_and_payoff": ["密信待回收"],
                "strengths": ["冲突清晰"],
                "risks_or_issues": ["节奏偏慢"],
                "reader_fit": "适合悬疑读者",
                "overall_assessment": "结构完整",
            }
        }
        detailed = {
            "male_protagonist": {"name": "林舟"},
            "characters": [
                {
                    "name": "沈砚",
                    "role_type": "antagonist",
                    "importance": 8,
                    "count": 2,
                    "factions": ["沈家"],
                    "key_events": ["销毁账册"],
                }
            ],
        }

        text = report.build_general_report("测试书", detailed, general_summary)

        self.assertIn("主线剧情", text)
        self.assertIn("历史小说专长分析", text)
        self.assertIn("历史制度与时代逻辑", text)
        self.assertIn("伏笔与回收", text)
        self.assertIn("官制与地方治理形成冲突", text)
        self.assertIn("真相与代价", text)
        self.assertIn("沈砚", text)
        self.assertIn("阵营/势力：沈家", text)

    def test_general_report_includes_specialty_notes_from_chunks(self):
        general_summary = {
            "profile_display_name": "仙侠/玄幻专长分析",
            "summary_fields": ["main_plot", "cultivation_system"],
            "summary": {
                "story_overview": "主角入宗修炼。",
                "main_plot": ["拜入宗门"],
                "cultivation_system": ["金丹元婴境界清晰"],
                "specialty_notes": ["境界体系清晰", "秘境规则稳定"],
            },
            "chunk_results": [
                {"specialty_notes": ["境界体系清晰", "宗门资源争夺明确"]},
                {"specialty_notes": ["秘境规则稳定"]},
            ],
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【专项命中要点】", text)
        self.assertEqual(text.count("境界体系清晰"), 1)
        self.assertIn("秘境规则稳定", text)
        self.assertIn("宗门资源争夺明确", text)

    def test_general_report_includes_radar_scores_for_frontend(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": ["main_plot", "character_highlights", "pacing_and_emotion"],
            "summary": {
                "story_overview": "主角调查案件并推进主线。",
                "main_plot": ["案件推进清晰"],
                "core_conflicts": ["主角与幕后势力对抗"],
                "worldbuilding": ["近代城市秩序"],
                "themes": ["真相与代价"],
                "character_highlights": ["主角有稳定方法论"],
                "pacing_and_emotion": ["节奏偏慢但情绪稳定"],
                "strengths": ["结构清楚"],
                "risks_or_issues": ["单元之间联系偏弱"],
                "radar_scores": {
                    "plot": {"score": 7.5, "reason": "主线推进清楚"},
                    "characters": {"score": 6.5, "reason": "角色辨识度尚可"},
                    "worldbuilding": {"score": 7, "reason": "城市规则明确"},
                    "pacing": {"score": 5.5, "reason": "节奏偏慢"},
                    "writing": {"score": 6, "reason": "表达流畅"},
                    "emotion": {"score": 5, "reason": "情绪调动一般"},
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【多维度评分】", text)
        self.assertIn("| 剧情质量 | 7.5/10 | 主线推进清楚 |", text)
        self.assertIn("| 节奏把控 | 5.5/10 | 节奏偏慢 |", text)
        self.assertIn("前端评分JSON", text)
        self.assertIn('"plot": {', text)
        self.assertIn('"label": "剧情质量"', text)
        self.assertIn('"score": 7.5', text)

    def test_general_report_includes_writing_quality_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "writing_quality_overall",
                "pacing_analysis_overall",
                "information_density_audit",
                "water_chapter_analysis",
            ],
            "summary": {
                "story_overview": "主角推进案件并维持悬疑节奏。",
                "main_plot": ["案件推进"],
                "writing_quality_overall": {
                    "overall_score": "7.2",
                    "grade": "B",
                    "dimension_scores": {
                        "prose_quality": "6.5",
                        "character_depth": "7",
                        "narrative_technique": "7.5",
                        "dialogue_quality": "7",
                        "scene_description": "6",
                        "emotional_impact": "6.5",
                        "info_density": "8",
                        "worldbuilding_integration": "7",
                    },
                    "strengths": ["线索推进密集"],
                    "weaknesses": ["人物声口区分一般"],
                    "evidence": ["审问段落持续推进证词"],
                    "assessment": "整体写作质量良好。",
                },
                "zhihu_writing_insights_overall": {
                    "word_poverty": {
                        "severity": "轻度词穷",
                        "template_phrase_count": 12,
                        "template_phrase_density_per_1k": 2.4,
                        "most_frequent_templates": ["不禁(5次)", "冷冷的(3次)"],
                        "category_patterns": ["情绪副词", "表情描写"],
                        "assessment": "存在少量模板化表达。",
                    },
                    "reader_inference_space": {
                        "score": 6.5,
                        "l1_l2_l3_pattern": "L2多于L1，L3偏少",
                        "assessment": "能用动作暗示情绪，但留白不足。",
                    },
                    "communication_efficiency": {
                        "level": "3",
                        "level_name": "发展",
                        "redundancy_verdict": "少量说明重复",
                        "assessment": "表达基本顺畅。",
                    },
                    "style_identity": {
                        "detected_traits": ["冷静克制", "说明性强"],
                        "originality_score": 6,
                        "consistency_score": 7,
                        "assessment": "风格稳定但辨识度一般。",
                    },
                    "emotional_authenticity": {
                        "score": 6,
                        "transcendence_potential": "中",
                        "assessment": "情绪有真实细节但爆发力有限。",
                    },
                    "priority_improvements": ["减少直白情绪判断"],
                },
                "pacing_analysis_overall": {
                    "rhythm_curve": "调查推进较紧",
                    "high_points": ["获得证词"],
                    "slow_or_water_segments": ["说明段略长"],
                    "emotion_pattern": "悬疑为主",
                    "risks": ["中段可能拖慢"],
                },
                "information_density_audit": {
                    "density_verdict": "信息密度较高",
                    "water_ratio_estimate": "约10%",
                    "high_density_material": ["审问线索"],
                    "redundancy_patterns": ["设定解释重复"],
                    "skip_advice": "审问段不建议跳读",
                },
                "water_chapter_analysis": ["说明性段落略多"],
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【写作质量分析】", text)
        self.assertIn("总体：7.2/10（B）", text)
        self.assertIn("| 文笔质量 | 6.5/10 |", text)
        self.assertIn("| 信息密度 | 8.0/10 |", text)
        self.assertIn("【写作优势】", text)
        self.assertIn("线索推进密集", text)
        self.assertIn("【知乎文笔洞察】", text)
        self.assertIn("严重度：轻度词穷", text)
        self.assertIn("高频模板：不禁(5次)；冷冷的(3次)", text)
        self.assertIn("- 读者推导空间：6.5/10；L2多于L1，L3偏少；能用动作暗示情绪，但留白不足。", text)
        self.assertIn("知乎文笔洞察JSON", text)
        self.assertIn("【节奏曲线分析】", text)
        self.assertIn("调查推进较紧", text)
        self.assertIn("【信息密度审计】", text)
        self.assertIn("审问段不建议跳读", text)
        self.assertIn("【水文与冗余分析】", text)
        self.assertIn("写作质量JSON", text)
        self.assertNotIn("【writing quality overall】", text)

    def test_general_report_includes_narrative_architecture_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "narrative_structure_analysis",
                "outline_architecture_overall",
            ],
            "summary": {
                "story_overview": "主角完成多阶段成长。",
                "main_plot": ["从入门到跨地图扩张"],
                "narrative_structure_analysis": {
                    "primary_structure_pattern": "升级-换地图循环",
                    "rhythm_curve_description": "前期铺垫，中段连续小高潮，后期转入新地图。",
                    "major_turning_points": ["宗门大比后换地图"],
                    "structure_risks": ["换地图疲劳"],
                },
                "outline_architecture_overall": {
                    "structural_completeness": "阶段目标清楚，暂未见烂尾风险。",
                    "causal_chain_strength": "strong",
                    "growth_curve": {"smoothness": "natural", "curve_description": "成长阶梯清楚"},
                    "system_stability": "战力体系基本稳定",
                    "overall_architecture_rating": "good",
                    "architecture_score": "7.8",
                    "improvement_suggestions": ["减少重复突破"],
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【叙事结构分析】", text)
        self.assertIn("升级-换地图循环", text)
        self.assertIn("宗门大比后换地图", text)
        self.assertIn("【大纲架构分析】", text)
        self.assertIn("架构评分：7.8/10（good）", text)
        self.assertIn("战力体系基本稳定", text)
        self.assertNotIn("【narrative structure analysis】", text)

    def test_general_report_includes_foreshadowing_engineering_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "foreshadowing_engineering_analysis",
            ],
            "summary": {
                "story_overview": "主角调查旧案并逐步回收线索。",
                "main_plot": ["旧案调查"],
                "foreshadowing_and_payoff": ["密信线索仍在推进"],
                "foreshadowing_engineering_analysis": {
                    "setup_quality": "good",
                    "active_threads": ["密信来源仍未揭开"],
                    "resolved_threads": ["旧钥匙用于打开地下档案室，回收较自然"],
                    "false_or_red_herring": ["嫌疑人的假口供属于烟雾弹"],
                    "payoff_satisfaction": "satisfying",
                    "recycling_rate_estimate": "约2/3已回收",
                    "risks": ["后续需回收密信来源"],
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【伏笔工程分析】", text)
        self.assertIn("设置质量：good", text)
        self.assertIn("回收满足度：satisfying", text)
        self.assertIn("回收率估计：约2/3已回收", text)
        self.assertIn("密信来源仍未揭开", text)
        self.assertIn("烟雾弹", text)
        self.assertEqual(text.count("【伏笔工程分析】"), 1)
        self.assertNotIn("【foreshadowing engineering analysis】", text)

    def test_general_report_includes_semantic_layers_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "semantic_layers_analysis",
            ],
            "summary": {
                "story_overview": "主角在压抑后完成反击。",
                "main_plot": ["主角反击"],
                "semantic_layers_analysis": {
                    "dominant_author_intent": "通过先压后扬制造反击爽感",
                    "reader_effect_pattern": "读者先感到压抑，再获得释放",
                    "deep_semantic_pattern": "对白表面退让，实际暗示主角已掌控局面",
                    "technique_pattern": ["先抑后扬", "对白潜台词"],
                    "subtext_or_irony": ["反派的胜券在握带有反讽效果"],
                    "semantic_strengths": ["情绪转折明确"],
                    "semantic_risks": ["若铺垫过长会造成憋屈"],
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【深层语义分析】", text)
        self.assertIn("作者意图：通过先压后扬制造反击爽感", text)
        self.assertIn("读者效果：读者先感到压抑，再获得释放", text)
        self.assertIn("对白表面退让", text)
        self.assertIn("先抑后扬", text)
        self.assertEqual(text.count("【深层语义分析】"), 1)
        self.assertNotIn("【semantic layers analysis】", text)

    def test_general_report_includes_reader_experience_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "reader_experience_analysis",
            ],
            "summary": {
                "story_overview": "主角连续受压后完成反击。",
                "main_plot": ["主角反击"],
                "reader_experience_analysis": {
                    "engagement_curve": "前期压抑，中段悬念拉高，反击段释放明显",
                    "dominant_emotions": ["压抑", "期待", "爽"],
                    "satisfaction_design": ["反派误判后被主角翻盘形成爽点"],
                    "anticipation_management": "用反派压制制造翻盘期待，并在本段兑现",
                    "immersion_anchors": ["主角反击收益", "反派误判"],
                    "frustration_risks": ["压抑段过长会降低耐心"],
                    "reader_experience_rating": "good",
                    "improvement_suggestions": ["缩短重复压制段"],
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【读者体验分析】", text)
        self.assertIn("体验评级：good", text)
        self.assertIn("投入曲线：前期压抑", text)
        self.assertIn("期待管理：用反派压制制造翻盘期待", text)
        self.assertIn("反派误判后被主角翻盘形成爽点", text)
        self.assertEqual(text.count("【读者体验分析】"), 1)
        self.assertNotIn("【reader experience analysis】", text)

    def test_general_report_includes_continuity_audit_sections(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": [
                "main_plot",
                "continuity_audit_analysis",
            ],
            "summary": {
                "story_overview": "主角推进旧案并回收线索。",
                "main_plot": ["旧案推进"],
                "continuity_audit_analysis": {
                    "overall_continuity_rating": "average",
                    "risk_level": "medium",
                    "character_continuity": ["主角称呼保持稳定"],
                    "relationship_consistency": ["同伴关系从陌生到协作，阶段自然"],
                    "worldbuilding_consistency": ["旧设定在后续片段继续生效"],
                    "foreshadowing_continuity": ["玉佩线索仍未回收"],
                    "causal_chain_issues": ["一次破局依赖偶然情报"],
                    "unresolved_threads": ["玉佩来源"],
                    "evidence": ["第3块设置玉佩，第40块仍提到玉佩"],
                    "fix_suggestions": ["补充玉佩来源解释"],
                },
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【连续性与一致性审计】", text)
        self.assertIn("连续性评级：average", text)
        self.assertIn("风险等级：medium", text)
        self.assertIn("主角称呼保持稳定", text)
        self.assertIn("玉佩线索仍未回收", text)
        self.assertIn("补充玉佩来源解释", text)
        self.assertEqual(text.count("【连续性与一致性审计】"), 1)
        self.assertNotIn("【continuity audit analysis】", text)

    def test_general_report_does_not_repeat_footer_summary_fields(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": ["main_plot", "reader_fit", "overall_assessment"],
            "summary": {
                "story_overview": "主角完成一条清晰主线。",
                "main_plot": ["完成主线任务"],
                "reader_fit": "适合喜欢完整主线的读者。",
                "overall_assessment": "整体完成度较高。",
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertEqual(text.count("【适合读者】"), 1)
        self.assertEqual(text.count("【总体评价】"), 1)
        self.assertNotIn("【reader fit】", text)
        self.assertNotIn("【overall assessment】", text)

    def test_general_report_reads_footer_field_alias_values(self):
        general_summary = {
            "profile_display_name": "通用小说分析",
            "summary_fields": ["main_plot", "reader_fit", "overall_assessment"],
            "summary": {
                "story_overview": "主角完成一条清晰主线。",
                "main_plot": ["完成主线任务"],
                "target_readers": "适合喜欢完整主线的读者。",
                "final_assessment": "整体完成度较高。",
            },
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("【适合读者】\n适合喜欢完整主线的读者。", text)
        self.assertIn("【总体评价】\n整体完成度较高。", text)
        self.assertNotIn("【target readers】", text)
        self.assertNotIn("【final assessment】", text)

    def test_general_report_uses_specialty_field_titles(self):
        general_summary = {
            "profile_display_name": "游戏/系统/无限流专长分析",
            "summary_fields": ["main_plot", "system_rules", "instance_design"],
            "summary": {
                "story_overview": "主角进入副本，通过系统任务成长。",
                "main_plot": ["进入主神副本"],
                "system_rules": ["系统面板稳定展示属性和技能"],
                "instance_design": ["副本目标与隐藏规则清晰"],
            }
        }

        text = report.build_general_report("测试书", {}, general_summary)

        self.assertIn("系统规则", text)
        self.assertIn("副本/关卡设计", text)
        self.assertIn("系统面板稳定展示属性和技能", text)

        extra_summary = {
            "profile_display_name": "末世/灾变/生存专长分析",
            "summary_fields": ["main_plot", "apocalypse_cause", "survival_resources"],
            "summary": {
                "story_overview": "灾变爆发后主角搜集物资建立避难所。",
                "apocalypse_cause": ["病毒感染引发尸潮"],
                "survival_resources": ["食物、药品和燃料形成主要压力"],
            }
        }
        extra_text = report.build_general_report("测试书", {}, extra_summary)
        self.assertIn("灾变成因与机制", extra_text)
        self.assertIn("生存资源", extra_text)
        self.assertIn("病毒感染引发尸潮", extra_text)

        more_summary = {
            "profile_display_name": "刑侦/法医/案件专长分析",
            "summary_fields": ["case_structure", "evidence_chain", "forensic_procedure"],
            "summary": {
                "story_overview": "刑警和法医围绕命案展开侦查。",
                "case_structure": ["案发现场和嫌疑关系清晰"],
                "evidence_chain": ["指纹、DNA和监控串联成证据链"],
                "forensic_procedure": ["尸检结果推动侦查方向"],
            }
        }
        more_text = report.build_general_report("测试书", {}, more_summary)
        self.assertIn("案件结构", more_text)
        self.assertIn("证据链", more_text)
        self.assertIn("法医与侦查程序", more_text)

        urban_summary = {
            "profile_display_name": "都市异能/爽文专长分析",
            "summary_fields": ["urban_setting", "golden_finger_system", "face_slapping_pacing"],
            "summary": {
                "story_overview": "都市神豪依靠系统打脸豪门。",
                "urban_setting": ["豪门和公司构成主要现实场景"],
                "golden_finger_system": ["系统、神豪和签到奖励构成升级逻辑"],
                "face_slapping_pacing": ["压扬反转形成主要爽点"],
            }
        }
        urban_text = report.build_general_report("测试书", {}, urban_summary)
        self.assertIn("都市现实背景", urban_text)
        self.assertIn("异能/金手指体系", urban_text)
        self.assertIn("系统、神豪和签到奖励构成升级逻辑", urban_text)

        specialty_summary = {
            "profile_display_name": "西幻/蒸汽朋克/炼金工业专长分析",
            "summary_fields": [
                "steampunk_setting",
                "alchemy_industry",
                "tech_feasibility",
                "episodic_mainline_integration",
                "shortcut_detection_dependency",
                "system_cost_validity",
                "technical_leap_risk",
            ],
            "summary": {
                "story_overview": "蒸汽时代的侦探依靠炼金系统处理案件。",
                "steampunk_setting": ["教会、帝国和蒸汽工业共同构成背景"],
                "alchemy_industry": ["炼金矩阵参与军工生产"],
                "tech_feasibility": ["差分机和高能煤精需要解释制造链"],
                "episodic_mainline_integration": ["多个案件与主线联系偏弱"],
                "shortcut_detection_dependency": ["破案高度依赖系统回放案发现场"],
                "system_cost_validity": ["寿命消耗是系统核心代价"],
                "technical_leap_risk": ["超级能源和制导技术跃迁过快"],
            }
        }
        specialty_text = report.build_general_report("测试书", {}, specialty_summary)
        self.assertIn("蒸汽西幻底盘", specialty_text)
        self.assertIn("技术可行性", specialty_text)
        self.assertIn("单元剧情与主线连接度", specialty_text)
        self.assertIn("外挂破案依赖度", specialty_text)
        self.assertIn("系统代价有效性", specialty_text)
        self.assertIn("技术跃迁风险", specialty_text)

        kimi_summary = {
            "profile_display_name": "历史小说专长分析",
            "summary_fields": [
                "main_plot",
                "character_highlights",
                "pacing_and_emotion",
                "historical_atmosphere",
                "warfare_and_intrigue",
                "scale_and_wonder",
                "social_ethical_impact",
            ],
            "summary": {
                "story_overview": "作品包含历史、科幻和通用叙事维度。",
                "character_highlights": ["配角有清晰人物弧光"],
                "pacing_and_emotion": ["爽点和虐点分布明确"],
                "historical_atmosphere": ["朝堂语言和礼法形成时代感"],
                "warfare_and_intrigue": ["战争决策服务于权谋翻盘"],
                "scale_and_wonder": ["恒星际尺度带来科幻奇观"],
                "social_ethical_impact": ["技术扩散改变社会伦理边界"],
            }
        }
        kimi_text = report.build_general_report("测试书", {}, kimi_summary)
        self.assertIn("角色亮点", kimi_text)
        self.assertIn("节奏与情绪曲线", kimi_text)
        self.assertIn("历史氛围", kimi_text)
        self.assertIn("战争与权谋", kimi_text)
        self.assertIn("尺度感与科幻奇观", kimi_text)
        self.assertIn("社会与伦理影响", kimi_text)

        genre_summary = {
            "profile_display_name": "类型专长分析",
            "summary_fields": [
                "bloodline_physique",
                "mythology_elements",
                "dao_theme",
                "instance_variety",
                "player_interaction",
                "novelty_mechanics",
                "real_world_impact",
                "races_culture",
                "politics_society",
                "romance_comedy_balance",
                "slice_of_life",
            ],
            "summary": {
                "story_overview": "作品包含升级、系统和异世界元素。",
                "bloodline_physique": ["主角体质有成长代价"],
                "mythology_elements": ["洪荒和天庭设定融入主线"],
                "dao_theme": ["长生动机清晰"],
                "instance_variety": ["副本来源差异明确"],
                "player_interaction": ["玩家交易影响剧情"],
                "novelty_mechanics": ["模拟器机制有新意"],
                "real_world_impact": ["游戏能力反向影响现实"],
                "races_culture": ["精灵和亚人社会结构不同"],
                "politics_society": ["王国贵族政治推动冲突"],
                "romance_comedy_balance": ["恋爱喜剧没有压过主线"],
                "slice_of_life": ["料理店日常提供慢生活节奏"],
            }
        }
        genre_text = report.build_general_report("测试书", {}, genre_summary)
        self.assertIn("血脉/体质/天赋", genre_text)
        self.assertIn("东方神话元素", genre_text)
        self.assertIn("求道/长生主题", genre_text)
        self.assertIn("副本/世界多样性", genre_text)
        self.assertIn("玩家互动", genre_text)
        self.assertIn("系统机制创新", genre_text)
        self.assertIn("现实世界影响", genre_text)
        self.assertIn("种族与文化生态", genre_text)
        self.assertIn("贵族/国家政治", genre_text)
        self.assertIn("恋爱喜剧平衡", genre_text)
        self.assertIn("日常/慢生活", genre_text)

        urban_summary = {
            "profile_display_name": "都市类专长分析",
            "summary_fields": [
                "relationships",
                "villain_quality",
                "era_atmosphere",
                "family_dynamics",
                "creative_process",
                "fan_economy",
                "corporate_politics",
                "supply_chain",
            ],
            "summary": {
                "story_overview": "都市相关分类测试。",
                "relationships": ["人情关系推动冲突"],
                "villain_quality": ["反派压迫有明确层级"],
                "era_atmosphere": ["网络文化和毕业季氛围明确"],
                "family_dynamics": ["原生家庭压力影响选择"],
                "creative_process": ["剧本打磨和宣发形成闭环"],
                "fan_economy": ["饭圈数据影响资源"],
                "corporate_politics": ["董事会斗争推动职场线"],
                "supply_chain": ["供应链瓶颈形成商业冲突"],
            }
        }
        urban_text = report.build_general_report("测试书", {}, urban_summary)
        self.assertIn("关系线", urban_text)
        self.assertIn("反派质量", urban_text)
        self.assertIn("时代氛围", urban_text)
        self.assertIn("原生家庭/家庭关系", urban_text)
        self.assertIn("创作过程", urban_text)
        self.assertIn("粉丝经济", urban_text)
        self.assertIn("职场政治", urban_text)
        self.assertIn("供应链/产业链", urban_text)

        broad_summary = {
            "profile_display_name": "扩展分类专长分析",
            "summary_fields": [
                "war_type_and_scale",
                "force_buildup",
                "equipment_and_tech",
                "combat_writing",
                "political_diplomacy",
                "social_collapse_and_rebuild",
                "humanity_moral_dilemmas",
                "power_evolution_system",
                "exploration_adventure",
                "case_complexity",
                "criminal_psychology",
                "team_dynamics",
                "social_reflection",
                "puzzle_fairness",
                "narrative_trick",
                "detective_method",
                "logic_chain_integrity",
                "sequence_system",
                "san_mechanics",
                "rule_based_horror",
                "contamination_levels",
                "weird_rules",
                "folk_taboo_system",
                "instance_escape_loop",
                "reality_intrusion",
                "alias_system",
                "information_asymmetry",
                "mastermind_schemes",
                "faction_balance",
                "exposure_risk",
                "alias_network",
                "reveal_payoff",
                "technique_tactics",
                "season_structure",
                "rivalry_and_opponents",
                "technology_progression",
                "civilization_level",
                "population_management",
            ],
            "summary": {
                "story_overview": "多分类字段标题测试。",
                "war_type_and_scale": ["局部战争升级为全面战争"],
                "force_buildup": ["部队训练逐步成型"],
                "equipment_and_tech": ["军工科技形成优势"],
                "combat_writing": ["战斗场景清晰"],
                "political_diplomacy": ["外交影响战局"],
                "social_collapse_and_rebuild": ["旧秩序崩塌后建立基地"],
                "humanity_moral_dilemmas": ["资源分配制造道德冲突"],
                "power_evolution_system": ["异能进化有代价"],
                "exploration_adventure": ["探索未知区域"],
                "case_complexity": ["连环案结构复杂"],
                "criminal_psychology": ["动机画像明确"],
                "team_dynamics": ["专案组分工清晰"],
                "social_reflection": ["案件映射现实问题"],
                "puzzle_fairness": ["线索足以推理"],
                "narrative_trick": ["叙述性诡计成立"],
                "detective_method": ["侦探方法论稳定"],
                "logic_chain_integrity": ["逻辑链闭合"],
                "sequence_system": ["序列晋升清晰"],
                "san_mechanics": ["理智损耗有规则"],
                "rule_based_horror": ["规则怪谈文本有效"],
                "contamination_levels": ["污染等级递进"],
                "weird_rules": ["隐藏规则需要验证"],
                "folk_taboo_system": ["祠堂村规与阴婚禁忌形成体系"],
                "instance_escape_loop": ["怪谈副本有通关闭环"],
                "reality_intrusion": ["现实生活被怪谈侵蚀"],
                "alias_system": ["多个马甲各自承担不同权限"],
                "information_asymmetry": ["隐藏身份制造信息差操纵"],
                "mastermind_schemes": ["幕后排局有铺垫和代价"],
                "faction_balance": ["多方势力互相制衡"],
                "exposure_risk": ["身份暴露风险持续存在"],
                "alias_network": ["马甲之间互相背书"],
                "reveal_payoff": ["掉马揭面回收前文伏笔"],
                "technique_tactics": ["技战术细节可信"],
                "season_structure": ["赛季节奏明确"],
                "rivalry_and_opponents": ["宿敌群像稳定"],
                "technology_progression": ["技术升级路径清楚"],
                "civilization_level": ["产业层级推进"],
                "population_management": ["人口管理成为核心资源"],
            }
        }
        broad_text = report.build_general_report("测试书", {}, broad_summary)
        for title in [
            "战争类型与规模",
            "部队建设",
            "装备与军工科技",
            "战斗描写",
            "政治与外交",
            "秩序崩塌与重建",
            "人性与道德困境",
            "能力/进化体系",
            "探索冒险",
            "案件复杂度",
            "犯罪心理",
            "团队协作",
            "社会映射",
            "谜题公平性",
            "叙述性诡计",
            "侦探方法论",
            "逻辑链完整性",
            "序列/魔药体系",
            "SAN值/理智机制",
            "规则怪谈",
            "污染等级",
            "规则机制",
            "民俗禁忌体系",
            "副本逃生闭环",
            "现实侵蚀",
            "马甲体系",
            "信息差操纵",
            "幕后排局",
            "势力平衡",
            "暴露风险",
            "马甲关系网络",
            "掉马与揭秘爽点",
            "专业技战术",
            "赛事/赛季结构",
            "对手群像",
            "技术升级路径",
            "文明/产业层级",
            "人口管理",
        ]:
            self.assertIn(title, broad_text)

    def test_harem_plus_section_can_append_general_summary(self):
        lines = ["【作品整体评价】"]
        report._append_general_scan_section(
            lines,
            {
                "profile_display_name": "通用小说分析",
                "summary_fields": ["main_plot", "character_highlights", "pacing_and_emotion"],
                "summary": {
                    "story_overview": "主角带着多女主线推进主线案件。",
                    "main_plot": ["主线案件分阶段推进"],
                    "character_highlights": ["女角色各有功能但塑造偏浅"],
                    "pacing_and_emotion": ["感情戏偏少，剧情说明文偏多"],
                    "strengths": ["设定清楚"],
                    "risks_or_issues": ["主线偏薄"],
                    "reader_fit": "适合想看设定和案件的读者",
                    "overall_assessment": "后宫报告外补充剧情评价。",
                },
            },
        )

        text = "\n".join(lines)
        self.assertIn("【作品整体评价】", text)
        self.assertIn("主线案件分阶段推进", text)
        self.assertIn("角色亮点", text)
        self.assertIn("节奏与情绪曲线", text)
        self.assertIn("后宫报告外补充剧情评价", text)

    def test_general_report_includes_knowledge_base_sections(self):
        text = report.build_general_report(
            "知识库测试",
            {},
            {
                "profile_display_name": "通用小说分析",
                "summary_fields": ["main_plot"],
                "knowledge_base_counts": {
                    "entities": 2,
                    "relationships": 1,
                    "worldbuilding_facts": 1,
                    "foreshadowing_threads": 1,
                    "plot_timeline": 2,
                    "open_threads": 1,
                    "resolved_threads": 1,
                },
                "knowledge_base": {
                    "entities": [
                        {"name": "林澈", "role_or_note": "侦探", "first_seen_chunk": 1, "evidence": "接手旧案"},
                        {"name": "沈青", "first_seen_chunk": 2, "evidence": "旧案证人"},
                    ],
                    "relationships": [{"chunk_index": 2, "description": "林澈与沈青从互相试探转为合作"}],
                    "worldbuilding_facts": [{"chunk_index": 1, "fact": "旧城由巡夜司管辖"}],
                    "plot_timeline": [{"chunk_index": 1, "event": "林澈接手旧案"}],
                    "open_threads": [{"chunk_index": 2, "thread": "密信来源仍未揭开"}],
                    "resolved_threads": [{"chunk_index": 3, "thread": "旧钥匙用途"}],
                    "foreshadowing_threads": [
                        {"status": "active", "importance": "high", "description": "密信背面的旧印记"}
                    ],
                },
                "summary": {
                    "story_overview": "主角调查旧案。",
                    "main_plot": ["调查旧案"],
                    "strengths": ["线索明确"],
                    "risks_or_issues": [],
                    "reader_fit": "悬疑读者",
                    "overall_assessment": "可读",
                },
            },
        )

        self.assertIn("【知识库摘要】", text)
        self.assertIn("实体2；关系1；设定1；伏笔1", text)
        self.assertIn("林澈", text)
        self.assertIn("林澈与沈青从互相试探转为合作", text)
        self.assertIn("旧城由巡夜司管辖", text)
        self.assertIn("林澈接手旧案", text)
        self.assertIn("密信来源仍未揭开", text)
        self.assertIn("旧钥匙用途", text)
        self.assertIn("密信背面的旧印记", text)

    def test_harem_plus_general_summary_requires_current_novel(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("novel")
            novel_path = f.name
        try:
            fresh = {
                "schema_version": 1,
                "analysis_profile": "general",
                "specialty_profile": "general",
                "novel_path": novel_path,
                "novel_mtime": os.path.getmtime(novel_path),
                "novel_signature": report._novel_file_signature(novel_path),
            }
            stale = dict(fresh)
            stale["novel_mtime"] = fresh["novel_mtime"] - 1
            wrong_profile = dict(fresh)
            wrong_profile["specialty_profile"] = "history"
            wrong_signature = dict(fresh)
            wrong_signature["novel_signature"] = {"size": 1, "mtime_ns": 1, "sample_sha256": "bad"}

            self.assertTrue(report._general_summary_matches_novel(fresh, novel_path, "general"))
            self.assertFalse(report._general_summary_matches_novel(stale, novel_path, "general"))
            self.assertFalse(report._general_summary_matches_novel(wrong_profile, novel_path, "general"))
            self.assertFalse(report._general_summary_matches_novel(wrong_signature, novel_path, "general"))
        finally:
            os.unlink(novel_path)

    def test_find_detailed_json_with_book_key_does_not_fallback_to_other_book(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            other_path = os.path.join(tmpdir, "乙书_detailed_20260609.json")
            with open(other_path, "w", encoding="utf-8") as f:
                json.dump({"book": "乙书"}, f)

            self.assertIsNone(report.find_detailed_json("甲书", base_dir=tmpdir, strict=True))
            self.assertIsNone(report.find_detailed_json("甲书", base_dir=tmpdir, strict=False))
            self.assertEqual(report.find_detailed_json("", base_dir=tmpdir, strict=False), other_path)

    def test_report_main_with_novel_path_does_not_use_other_book_detail(self):
        old_results_dir = report.RESULTS_DIR
        old_checkpoint = report.REPORT_CHECKPOINT_FILE
        old_novel_path = os.environ.get("NOVEL_PATH")
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                report.RESULTS_DIR = tmpdir
                report.REPORT_CHECKPOINT_FILE = os.path.join(tmpdir, "report_checkpoint.json")
                os.environ["ANALYSIS_PROFILE"] = "harem"
                novel_path = os.path.join(tmpdir, "甲书.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("甲书正文")
                other_detail = os.path.join(tmpdir, "乙书_detailed_20260609.json")
                with open(other_detail, "w", encoding="utf-8") as f:
                    json.dump({"male_protagonist": {"name": "乙男"}}, f, ensure_ascii=False)
                other_review_dir = os.path.join(tmpdir, "乙书_scan_20260609")
                os.makedirs(other_review_dir)
                with open(os.path.join(other_review_dir, "VERIFIED_SUMMARY_20260609.json"), "w", encoding="utf-8") as f:
                    json.dump({"male_lead": "乙男", "heroines_purity": [{"name": "乙女"}]}, f, ensure_ascii=False)

                captured = {}

                def fake_build(book_key, detailed_data, reviewer):
                    captured["book_key"] = book_key
                    captured["detailed_data"] = detailed_data
                    captured["reviewer"] = reviewer
                    return "报告正文"

                with mock.patch.object(report, "find_latest", return_value=None), \
                        mock.patch.object(report, "init_token_tracker"), \
                        mock.patch.object(report, "build_report_v2", side_effect=fake_build):
                    report.main(novel_path=novel_path, book_name="甲书")

                self.assertEqual(captured["book_key"], "甲书")
                self.assertIsNone(captured["detailed_data"])
                self.assertIsNone(captured["reviewer"])
        finally:
            report.RESULTS_DIR = old_results_dir
            report.REPORT_CHECKPOINT_FILE = old_checkpoint
            if old_novel_path is None:
                os.environ.pop("NOVEL_PATH", None)
            else:
                os.environ["NOVEL_PATH"] = old_novel_path
            if old_profile is None:
                os.environ.pop("ANALYSIS_PROFILE", None)
            else:
                os.environ["ANALYSIS_PROFILE"] = old_profile

    def test_find_reviewer_summary_json_requires_exact_book_key_from_scan_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wrong_dir = os.path.join(tmpdir, "九锡外传_scan_20260609")
            right_dir = os.path.join(tmpdir, "九锡_scan_20260609")
            os.makedirs(wrong_dir)
            os.makedirs(right_dir)
            wrong_path = os.path.join(wrong_dir, "VERIFIED_SUMMARY_20260609.json")
            right_path = os.path.join(right_dir, "VERIFIED_SUMMARY_20260609.json")
            with open(wrong_path, "w", encoding="utf-8") as f:
                json.dump({"book": "九锡外传"}, f)
            with open(right_path, "w", encoding="utf-8") as f:
                json.dump({"book": "九锡"}, f)

            self.assertEqual(report.find_reviewer_summary_json("九锡", base_dir=tmpdir, strict=True), right_path)
            os.unlink(right_path)
            self.assertIsNone(report.find_reviewer_summary_json("九锡", base_dir=tmpdir, strict=True))

    def test_find_general_summary_json_requires_exact_book_key_from_scan_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wrong_dir = os.path.join(tmpdir, "九锡外传_scan_20260609")
            right_dir = os.path.join(tmpdir, "九锡_scan_20260609")
            os.makedirs(wrong_dir)
            os.makedirs(right_dir)
            wrong_path = os.path.join(wrong_dir, "GENERAL_SUMMARY.json")
            right_path = os.path.join(right_dir, "GENERAL_SUMMARY.json")
            with open(wrong_path, "w", encoding="utf-8") as f:
                json.dump({"book": "九锡外传"}, f)
            with open(right_path, "w", encoding="utf-8") as f:
                json.dump({"book": "九锡"}, f)

            self.assertEqual(report.find_general_summary_json("九锡", base_dir=tmpdir), right_path)
            os.unlink(right_path)
            self.assertIsNone(report.find_general_summary_json("九锡", base_dir=tmpdir))

    def test_report_main_general_ignores_summary_for_other_novel(self):
        old_results_dir = report.RESULTS_DIR
        old_checkpoint = report.REPORT_CHECKPOINT_FILE
        old_novel_path = os.environ.get("NOVEL_PATH")
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                report.RESULTS_DIR = tmpdir
                report.REPORT_CHECKPOINT_FILE = os.path.join(tmpdir, "report_checkpoint.json")
                os.environ["ANALYSIS_PROFILE"] = "general"
                novel_path = os.path.join(tmpdir, "甲书.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("甲书正文")
                summary_path = os.path.join(tmpdir, "甲书_GENERAL_SUMMARY_latest.json")
                with open(summary_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "analysis_profile": "general",
                        "specialty_profile": "general",
                        "novel_path": os.path.join(tmpdir, "乙书.txt"),
                        "novel_mtime": 1,
                        "novel_signature": {"size": 1, "mtime_ns": 1, "sample_sha256": "bad"},
                        "summary": {"story_overview": "乙书概要"},
                    }, f, ensure_ascii=False)

                captured = {}

                def fake_build(book_key, detailed_data, general_summary=None):
                    captured["book_key"] = book_key
                    captured["general_summary"] = general_summary
                    return "通用报告正文"

                with mock.patch.object(report, "init_token_tracker"), \
                        mock.patch.object(report, "build_general_report", side_effect=fake_build):
                    report.main(novel_path=novel_path, book_name="甲书")

                self.assertEqual(captured["book_key"], "甲书")
                self.assertIsNone(captured["general_summary"])
        finally:
            report.RESULTS_DIR = old_results_dir
            report.REPORT_CHECKPOINT_FILE = old_checkpoint
            if old_novel_path is None:
                os.environ.pop("NOVEL_PATH", None)
            else:
                os.environ["NOVEL_PATH"] = old_novel_path
            if old_profile is None:
                os.environ.pop("ANALYSIS_PROFILE", None)
            else:
                os.environ["ANALYSIS_PROFILE"] = old_profile

    def test_harem_report_adds_romance_overview_and_past_risk(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {
                        "name": "男主",
                        "summaries": ["男主前世老婆在他绝症后卷光家产跑路。"],
                    },
                    "heroine_result": {
                        "heroines": [{"name": "甲女", "importance_rank": 1}]
                    },
                    "all_female_characters": {
                        "甲女": {
                            "count": 1,
                            "summaries": ["偶尔出场，主要负责召唤物和背景说明。"],
                            "profile_for_report": {
                                "identity": "被家族安排政治联姻的召唤物助手",
                                "features": "工具人倾向明显，经济上依附男主资源",
                                "relationship_with_protagonist": "与男主暧昧但未确认关系，受男主保护",
                                "key_events": "喜欢男主，多次被救，曾被反派逼婚和囚禁，结局未交代归宿",
                            },
                        }
                    },
                },
                {
                    "heroines_purity": [
                        {
                            "name": "甲女",
                            "is_virgin": True,
                            "is_spirit_clean": True,
                            "no_partner": True,
                            "partner_exempted_for_clean": True,
                            "partner_exemption_reason": "原著前夫：forced=true，has_feelings=false",
                            "past_life_clean": False,
                            "past_life_severity": "partner",
                            "past_life_severity_label": "前世/原故事线伴侣或婚约风险",
                            "past_life_status": "前世/原故事线存在风险线索",
                            "past_life_reason": "原故事线里曾被安排嫁给非男主。",
                            "contact_level": "L2",
                            "contact_level_label": "被迫婚约/伴侣关系线索",
                            "contact_level_reason": "原故事线里曾被安排嫁给非男主。",
                            "is_leak_heroine": True,
                            "leak_reason": "暧昧到结局未收入。",
                            "leak_emotional_depth": True,
                            "leak_relationship_confirmed": False,
                            "leak_ending_accounted": False,
                        }
                    ]
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("【感情线与女角色有效性】", text)
        self.assertIn("感情戏密度", text)
        self.assertIn("女角色存在感", text)
        self.assertIn("男主前史情感雷点", text)
        self.assertIn("前妻/前女友/前世婚恋", text)
        self.assertIn("前世洁度", text)
        self.assertIn("前世风险等级", text)
        self.assertIn("partner（前世/原故事线伴侣或婚约风险）", text)
        self.assertIn("原故事线存在风险线索", text)
        self.assertIn("partner豁免", text)
        self.assertIn("原著前夫：forced=true，has_feelings=false", text)
        self.assertIn("接触等级", text)
        self.assertIn("L2（被迫婚约/伴侣关系线索）", text)
        self.assertIn("关系结构标签", text)
        self.assertIn("经济依附", text)
        self.assertIn("政治联姻/婚约", text)
        self.assertIn("受害/胁迫记录", text)
        self.assertIn("女主定位分级", text)
        self.assertIn("女主有效性", text)
        self.assertIn("有效性存疑", text)
        self.assertIn("漏女三层判定", text)
        self.assertIn("情感深度=✅", text)
        self.assertIn("关系确认=❌", text)
        self.assertIn("结局交代=❌", text)
        self.assertIn("结论=疑似漏女", text)

    def test_harem_romance_overview_flags_no_romance_progression(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            overview = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "凯蒂": {
                            "count": 6,
                            "summaries": ["探案时负责捧哏和物理输出。"],
                        },
                        "艾琳": {
                            "count": 1,
                            "summaries": ["主要帮男主做召唤物，存在感很低。"],
                        },
                        "安妮": {
                            "count": 2,
                            "summaries": ["偶尔在案件中客串。"],
                        },
                    }
                },
                {
                    "yumen_points": [
                        {
                            "type": "感情戏缺失/预期落差",
                            "content": "大量篇幅推进案件和设定，女角色只承担捧哏、召唤、客串功能。",
                        },
                        {
                            "type": "工具人女主",
                            "content": "女角色缺少独立情感弧线。",
                        },
                    ]
                },
                [
                    {"name": "凯蒂", "importance_rank": 1},
                    {"name": "艾琳", "importance_rank": 2},
                    {"name": "安妮", "importance_rank": 3},
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("极低", overview["romance_density"])
        self.assertIn("未见明确恋爱推进", overview["romance_progression"])
        self.assertIn("明显感情戏缺失风险", overview["romance_expectation_gap"])
        self.assertIn("工具人女主", overview["female_tooling_risk"])

    def test_harem_report_adds_cross_validation_warnings_for_mismatched_heroine_lists(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {"name": "甲女", "importance_rank": 1},
                            {"name": "乙女", "importance_rank": 2},
                        ]
                    },
                    "all_female_characters": {},
                },
                {
                    "heroines_purity": [
                        {"name": "甲女", "is_virgin": True},
                        {"name": "丙女", "is_virgin": True},
                    ]
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("【交叉验证提示】", text)
        self.assertIn("扫描阶段识别到但审核洁度未覆盖：乙女", text)
        self.assertIn("审核洁度中出现但扫描女主列表未列出：丙女", text)

    def test_harem_cross_validation_uses_aliases_and_core_names(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {"name": "圣女甲女（王室成员）", "aliases": ["甲女"], "importance_rank": 1},
                        ]
                    },
                    "all_female_characters": {
                        "圣女甲女": {"other_names": ["甲女"]}
                    },
                },
                {
                    "heroines_purity": [
                        {"name": "甲女", "is_virgin": True},
                    ]
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertNotIn("【交叉验证提示】", text)

    def test_harem_report_warns_when_scan_issue_is_not_covered_by_reviewer(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [{"name": "甲女", "importance_rank": 1}],
                    },
                    "all_female_characters": {},
                    "issues": [
                        {
                            "category": "雷点（严重毒点）",
                            "type": "绿帽",
                            "chunk_index": 18,
                            "content": "甲女与非男主男性出现明确暧昧关系。",
                        }
                    ],
                },
                {
                    "heroines_purity": [{"name": "甲女", "is_virgin": True}],
                    "lei_points": [],
                    "yumen_points": [],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("【交叉验证提示】", text)
        self.assertIn("扫描阶段发现但二审输出未覆盖的雷点/郁闷点：绿帽@chunk 18", text)

    def test_harem_issue_cross_validation_treats_rejected_points_as_covered(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [{"name": "甲女", "importance_rank": 1}],
                    },
                    "all_female_characters": {},
                    "issues": [
                        {
                            "category": "雷点（严重毒点）",
                            "type": "送女",
                            "chunk_index": 9,
                            "content": "传闻甲女被家族安排嫁给路人。",
                        }
                    ],
                },
                {
                    "heroines_purity": [{"name": "甲女", "is_virgin": True}],
                    "rejected_points": [
                        {
                            "category": "雷点（严重毒点）",
                            "type": "送女",
                            "chunk_index": 9,
                            "content": "传闻甲女被家族安排嫁给路人。",
                            "review_comment": "二审驳回，缺少男主主动或默许。",
                        }
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertNotIn("扫描阶段发现但二审输出未覆盖", text)

    def test_harem_report_includes_mermaid_relationship_graph(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "甲女",
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            },
                            {
                                "name": "乙女",
                                "importance_rank": 2,
                                "relationship_type": "暧昧",
                            },
                        ],
                    },
                    "all_female_characters": {
                        "甲女": {
                            "count": 8,
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "key_events": "已确认关系并同房",
                            },
                        },
                        "乙女": {
                            "count": 3,
                            "profile_for_report": {
                                "identity": "候选女主",
                                "relationship_with_protagonist": "与男主暧昧",
                                "key_events": "多次同行",
                            },
                        },
                    },
                },
                {
                    "heroines_purity": [
                        {
                            "name": "甲女",
                            "is_clean": True,
                            "is_virgin": True,
                            "is_spirit_clean": True,
                            "no_partner": True,
                            "has_other_contact": False,
                            "contact_level": "L0",
                            "pushed_by_male_lead": True,
                        },
                        {
                            "name": "乙女",
                            "is_clean": False,
                            "is_virgin": False,
                            "is_spirit_clean": True,
                            "no_partner": False,
                            "has_other_contact": True,
                            "contact_level": "L3",
                        },
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("【关系图谱】", text)
        self.assertIn("```mermaid", text)
        self.assertIn("graph TD", text)
        self.assertIn('ML["男主: 男主"]', text)
        self.assertIn('H1["甲女\\n道侣"]', text)
        self.assertIn('ML -->|"目标女主 / 洁度: 全初 / L0"| H1', text)
        self.assertIn('H2["乙女\\n暧昧"]', text)
        self.assertIn("洁度: 有瑕", text)

    def test_harem_report_includes_key_event_timeline_table(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "甲女",
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            },
                        ],
                    },
                    "all_female_characters": {
                        "甲女": {
                            "count": 8,
                            "summaries": [
                                (12, "甲女首次随男主进入秘境。"),
                                (30, "甲女与男主并肩作战。"),
                            ],
                            "interactions": [
                                (40, "男主向甲女表白并确认关系。"),
                            ],
                            "emotion_signals": [
                                (20, "甲女开始吃醋。"),
                            ],
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "key_events": "已确认关系并同房",
                            },
                        },
                    },
                },
                {
                    "heroines_purity": [
                        {
                            "name": "甲女",
                            "is_clean": True,
                            "is_virgin": True,
                            "is_spirit_clean": True,
                            "no_partner": True,
                            "has_other_contact": False,
                            "contact_level": "L0",
                            "pushed_by_male_lead": True,
                            "pushed_reason": "第40块确认关系。",
                        },
                    ],
                    "lei_points": [
                        {
                            "type": "绿帽",
                            "chunk_index": 25,
                            "content": "反派散布甲女与路人男的谣言。",
                        },
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("【关键事件时间线】", text)
        self.assertIn("| 位置 | 事件类型 | 涉及对象 | 事件 | 洁度/关系影响 |", text)
        self.assertIn("| chunk 13 | 女主剧情 | 甲女 | 甲女首次随男主进入秘境。 | - |", text)
        self.assertIn("| chunk 21 | 情感信号 | 甲女 | 甲女开始吃醋。 | 感情推进 |", text)
        self.assertIn("| chunk 26 | 雷点事件 | 绿帽 | 反派散布甲女与路人男的谣言。 | 绿帽 |", text)
        self.assertIn("| chunk 41 | 男主互动 | 甲女 | 男主向甲女表白并确认关系。 | 感情推进 |", text)
        self.assertIn("| 全书汇总 | 推倒/关系确认 | 甲女 | 第40块确认关系。 | 确定伴侣关系 |", text)

        timeline_start = text.index("【关键事件时间线】")
        self.assertLess(text.index("chunk 13", timeline_start), text.index("chunk 26", timeline_start))
        self.assertLess(text.index("chunk 26", timeline_start), text.index("chunk 41", timeline_start))

    def test_harem_romance_overview_counts_low_presence_semantically(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            overview = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "甲女": {
                            "count": 1,
                            "summaries": ["存在感较高，负责推进核心案件。"],
                        },
                        "乙女": {
                            "count": 8,
                            "summaries": ["存在感约等于没有，很快神隐。"],
                        },
                    }
                },
                {},
                [
                    {"name": "甲女", "importance_rank": 1},
                    {"name": "乙女", "importance_rank": 2},
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("低存在感条目约 1 位", overview["female_presence"])

    def test_harem_romance_overview_counts_small_presence_phrases(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            overview = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "甲女": {
                            "count": 4,
                            "summaries": ["主要在两个案件中出场，存在感很小。"],
                        },
                        "乙女": {
                            "count": 4,
                            "summaries": ["偶尔在案件中客串，存在感较小。"],
                        },
                        "丙女": {
                            "count": 8,
                            "summaries": ["长期参与主线，存在感不小，与男主有暧昧推进。"],
                        },
                    }
                },
                {},
                [
                    {"name": "甲女", "importance_rank": 2},
                    {"name": "乙女", "importance_rank": 3},
                    {"name": "丙女", "importance_rank": 1},
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("低存在感条目约 2 位", overview["female_presence"])
        self.assertIn("女角色可能偏工具人", overview["female_tooling_risk"])

    def test_harem_romance_overview_ignores_negated_tooling_issue(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            overview = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "甲女": {
                            "count": 8,
                            "summaries": ["主线参与充分，与男主暧昧并偶尔吃醋。"],
                        }
                    }
                },
                {
                    "yumen_points": [
                        {
                            "type": "角色塑造复核",
                            "content": "甲女不是工具人女主，也不是背景板。",
                        }
                    ]
                },
                [{"name": "甲女", "importance_rank": 1}],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertNotIn("工具人女主", overview["female_tooling_risk"])
        self.assertEqual(overview["female_tooling_risk"], "未见明显大面积工具人风险")

    def test_harem_romance_overview_ignores_negated_romance_signals(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            negated = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "甲女": {
                            "count": 5,
                            "summaries": ["长期跟随男主处理案件，但没有恋爱、没有喜欢、没有感情描写。"],
                        }
                    }
                },
                {},
                [
                    {
                        "name": "甲女",
                        "relationship_type": "探案搭档",
                        "summary": "没有恋爱、没有喜欢男主，只负责案件捧哏。",
                    }
                ],
                {},
            )
            positive = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "乙女": {
                            "count": 5,
                            "emotion_signals": ["喜欢男主并主动表白。"],
                        }
                    }
                },
                {},
                [
                    {
                        "name": "乙女",
                        "relationship_type": "恋人",
                        "summary": "喜欢男主并主动表白。",
                    }
                ],
                {},
            )
            negated_gap = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "丙女": {
                            "count": 6,
                            "summaries": ["与男主长期暧昧并主动表白，不是没有感情戏，也没有感情戏缺失问题。"],
                        }
                    }
                },
                {
                    "yumen_points": [
                        {
                            "type": "感情线复核",
                            "content": "丙女并非没有感情线，未见感情戏缺失。",
                        }
                    ]
                },
                [
                    {
                        "name": "丙女",
                        "relationship_type": "恋人",
                        "summary": "不是没有恋爱线，双方有明确暧昧和表白。",
                    }
                ],
                {},
            )
            victim_only = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "丁女": {
                            "count": 5,
                            "summaries": ["被路人强行亲吻，差点被迫同房，最后被男主救下，没有感情戏。"],
                        }
                    }
                },
                {},
                [
                    {
                        "name": "丁女",
                        "relationship_type": "受害者",
                        "summary": "强迫亲密未遂，没有恋爱推进。",
                    }
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("极低", negated["romance_density"])
        self.assertIn("未见明确恋爱推进", negated["romance_progression"])
        self.assertIn("中等或以上", positive["romance_density"])
        self.assertIn("中等或以上", negated_gap["romance_density"])
        self.assertIn("极低", victim_only["romance_density"])
        self.assertIn("未见明确恋爱推进", victim_only["romance_progression"])

    def test_harem_romance_overview_keeps_physical_event_from_inflating_romance(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            overview = report._summarize_harem_romance_overview(
                {
                    "all_female_characters": {
                        "甲女": {
                            "count": 4,
                            "summaries": ["她传授男主双修之法并实践，但完全没有任何感情描写。"],
                        }
                    }
                },
                {},
                [
                    {
                        "name": "甲女",
                        "relationship_type": "短暂双修对象",
                        "summary": "与男主双修，但没有感情戏，没有恋爱线。",
                    }
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("偏低", overview["romance_density"])
        self.assertIn("感情描写缺失", overview["romance_density"])
        self.assertIn("缺少可确认的恋爱/情绪推进", overview["romance_progression"])
        self.assertIn("感情戏兑现不足风险", overview["romance_expectation_gap"])

    def test_harem_romance_overview_avoids_current_wife_as_past_risk(self):
        self.assertFalse(report._has_male_past_romance_risk("男主与妻子一起经营家族。"))
        self.assertFalse(report._has_male_past_romance_risk("男主是前任掌门留下的弟子。"))
        self.assertFalse(report._has_male_past_romance_risk("男主继承前任队长的职位。"))
        self.assertFalse(report._has_male_past_romance_risk("男主拜入前夫子门下读书。"))
        self.assertFalse(report._has_male_past_romance_risk("男主见过前夫人的遗物。"))
        self.assertFalse(report._has_male_past_romance_risk("男主研究前妻子的遗物。"))
        self.assertFalse(report._has_male_past_romance_risk("男主拜访前妻弟。"))
        self.assertFalse(report._has_male_past_romance_risk("男主帮助前妻妹妹处理家事。"))
        self.assertFalse(report._has_male_past_romance_risk("男主询问前夫人选拔制度。"))
        self.assertFalse(report._has_male_past_romance_risk("男主调查前任勇者的妻子去世真相。"))
        self.assertFalse(report._has_male_past_romance_risk("男主继承前任宿主妻子的遗物。"))
        self.assertFalse(report._has_male_past_romance_risk("男主发现前任主人的老婆卷钱跑路。"))
        self.assertTrue(report._has_male_past_romance_risk("男主前世老婆在他绝症后卷光家产跑路。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前女友但已经分手。"))
        self.assertTrue(report._has_male_past_romance_risk("男主前妻已经去世。"))
        self.assertTrue(report._has_male_past_romance_risk("男主的前夫早已去世。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前任但已经分手。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前任女友但已经分手。"))
        self.assertFalse(report._has_male_past_romance_risk("男主听说前世老婆卷光家产跑路，后来证实是误会。"))
        self.assertFalse(report._has_male_past_romance_risk("有人传言男主有前女友，但实际没有恋爱经历。"))
        self.assertFalse(report._has_male_past_romance_risk("男主梦见自己前世有妻子背叛。"))
        self.assertFalse(report._has_male_past_romance_risk("男主前世没有老婆也没有女友。"))
        self.assertFalse(report._has_male_past_romance_risk("男主前世老婆这个称呼只是同伴开玩笑。"))
        self.assertFalse(report._has_male_past_romance_risk("男主被误认为有前妻，但澄清后不是事实。"))
        self.assertFalse(report._has_male_past_romance_risk("男主前世与妻子只是政治婚约，未结婚也无感情。"))
        self.assertFalse(report._has_male_past_romance_risk("副本设定里男主有前世老婆卷钱跑路，但现实线没有婚恋经历。"))
        self.assertFalse(report._has_male_past_romance_risk("剧本里男主假扮有前女友的人设，实际没有恋爱经历。"))
        self.assertFalse(report._has_male_past_romance_risk("游戏设定要求男主模拟前妻去世剧情，不是现实前史。"))
        self.assertTrue(report._has_male_past_romance_risk("证据显示男主前世老婆在他绝症后卷光家产跑路。"))
        self.assertTrue(report._has_male_past_romance_risk("男主听说前世老婆卷光家产跑路，后来证实是误会；但证据显示男主有前女友且已经分手。"))
        self.assertTrue(report._has_male_past_romance_risk("男主前世老婆卷钱跑路的传言是误会，但事实是男主前妻已经去世。"))

        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            clean = report._summarize_harem_romance_overview(
                {"all_female_characters": {}},
                {},
                [],
                {"summaries": ["男主与妻子一起经营家族。"]},
            )
            risky = report._summarize_harem_romance_overview(
                {"all_female_characters": {}},
                {},
                [],
                {"summaries": ["男主前世老婆在他绝症后卷光家产跑路。"]},
            )
            rumor = report._summarize_harem_romance_overview(
                {"all_female_characters": {}},
                {},
                [],
                {"summaries": ["男主听说前世老婆卷光家产跑路，后来证实是误会。"]},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertEqual(clean["male_past_romance_risk"], "未见明确男主前史情感雷点。")
        self.assertIn("前妻/前女友/前世婚恋", risky["male_past_romance_risk"])
        self.assertEqual(rumor["male_past_romance_risk"], "未见明确男主前史情感雷点。")

    def test_report_classifies_heroine_position_level(self):
        target = report._heroine_position_level(
            {"importance_rank": 1},
            {
                "relationship_with_protagonist": "男主道侣，互相喜欢并同房",
                "key_events": "多次双修并确认关系",
                "features": "主线女主",
            },
            {"count": 8, "summaries": ["长期陪伴男主推进主线"]},
            {"pushed_by_male_lead": True},
        )
        strong_candidate = report._heroine_position_level(
            {"importance_rank": 4},
            {
                "relationship_with_protagonist": "与男主长期暧昧并喜欢男主",
                "key_events": "结局未交代归宿",
                "features": "反复参与主线",
            },
            {"count": 4, "emotion_signals": ["吃醋", "表白"]},
            {"is_leak_heroine": True, "leak_emotional_depth": True},
        )
        weak_candidate = report._heroine_position_level(
            {"importance_rank": 9},
            {
                "relationship_with_protagonist": "受男主保护并有单独互动",
                "key_events": "协助男主一次",
                "features": "配角",
            },
            {"count": 2, "summaries": ["参与一次支线"]},
            {},
        )
        low_evidence = report._heroine_position_level(
            {},
            {"features": "背景说明角色，客串神隐"},
            {"count": 1, "summaries": ["存在感约等于没有"]},
            {},
        )

        self.assertTrue(target.startswith("目标女主"))
        self.assertTrue(strong_candidate.startswith("强准女主"))
        self.assertTrue(weak_candidate.startswith("弱准女主"))
        self.assertTrue(low_evidence.startswith("低证据女角色"))
        self.assertIn("低存在感/工具人线索", low_evidence)

        nominal_pushed = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "relationship_with_protagonist": "与男主长期暧昧并喜欢男主。",
                "key_events": "两人只是名义夫妻，有名无实，未同房也未圆房，未确认关系。",
                "features": "主线相关角色",
            },
            {"count": 6, "summaries": ["长期参与主线，但关系仍未收束。"]},
            {"pushed_by_male_lead": True, "pushed_reason": "名义夫妻，有名无实，未同房，未确认关系。"},
        )
        self.assertFalse(nominal_pushed.startswith("目标女主"), nominal_pushed)
        self.assertIn("推倒/确认关系证据疑似名义或否定语境", nominal_pushed)

        ex_wife_vibe = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "探案搭档",
                "relationship_with_protagonist": "跟随男主办案，偶尔摆出前妻感造型",
                "features": "功能性助手",
                "key_events": "多次参与案件，但没有恋爱或后宫关系确认",
            },
            {"count": 8, "summaries": ["负责提供线索和说明背景"]},
            {},
        )
        self.assertFalse(ex_wife_vibe.startswith("目标女主"), ex_wife_vibe)
        self.assertIn("缺少感情/后宫定位证据", ex_wife_vibe)

        past_fiancee_vibe = report._heroine_position_level(
            {"importance_rank": 1},
            {
                "identity": "男主前史说明角色",
                "relationship_with_protagonist": "男主前未婚妻的传闻对象，当前线没有恋爱线。",
                "features": "主线案件证人",
                "key_events": "高频出场说明男主过去，但没有感情戏。",
            },
            {"count": 12, "summaries": ["男主前未婚妻相关前史反复被提及，但没有当前感情戏。"]},
            {},
        )
        self.assertFalse(past_fiancee_vibe.startswith("目标女主"), past_fiancee_vibe)
        self.assertFalse(past_fiancee_vibe.startswith("强准女主"), past_fiancee_vibe)
        self.assertIn("明确缺少恋爱/后宫推进", past_fiancee_vibe)
        self.assertIn("缺少感情/后宫定位证据", past_fiancee_vibe)

        no_romance_functional = report._heroine_position_level(
            {"importance_rank": 1},
            {
                "identity": "经营助手",
                "relationship_with_protagonist": "帮男主管理领地和账本",
                "features": "工具人功能明显",
                "key_events": "高频参与种田经营，但没有感情戏，没有恋爱线。",
            },
            {"count": 30, "summaries": ["负责账本、税收、供应链说明。"]},
            {},
        )
        self.assertTrue(no_romance_functional.startswith("低证据女角色"), no_romance_functional)
        self.assertIn("明确缺少恋爱/后宫推进", no_romance_functional)

        high_presence_no_romance = report._heroine_position_level(
            {"importance_rank": 1},
            {
                "identity": "探案搭档",
                "relationship_with_protagonist": "跟随男主探案，存在感较高，但没有恋爱线。",
                "features": "主线助手",
                "key_events": "高频参与案件侦破，没有感情戏。",
            },
            {"count": 20, "summaries": ["存在感较高，负责案件推进。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", high_presence_no_romance)
        self.assertIn("明确缺少恋爱/后宫推进", high_presence_no_romance)

        nearly_absent = report._heroine_position_level(
            {"importance_rank": 6},
            {
                "identity": "客串女角色",
                "relationship_with_protagonist": "偶尔与男主同场，没有恋爱线。",
                "features": "存在感约等于没有",
                "key_events": "很快神隐。",
            },
            {"count": 2, "summaries": ["存在感约等于没有。"]},
            {},
        )
        self.assertIn("低存在感/工具人线索", nearly_absent)

        occasional_romance = report._heroine_position_level(
            {"importance_rank": 3},
            {
                "identity": "主线女配",
                "relationship_with_protagonist": "与男主长期暧昧，偶尔吃醋并亲密互动。",
                "features": "参与主线",
                "key_events": "多次同行并偶尔表白心意。",
            },
            {"count": 8, "summaries": ["偶尔吃醋，但感情线持续推进。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", occasional_romance)
        self.assertIn("感情/亲密推进", occasional_romance)

        occasional_cameo = report._heroine_position_level(
            {"importance_rank": 7},
            {
                "identity": "客串角色",
                "relationship_with_protagonist": "偶尔出场帮忙，没有恋爱线。",
                "features": "偶尔客串",
                "key_events": "偶尔协助男主处理支线。",
            },
            {"count": 2, "summaries": ["偶尔出场。"]},
            {},
        )
        self.assertIn("低存在感/工具人线索", occasional_cameo)

        background_identity = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "身份背景复杂的贵族少女",
                "relationship_with_protagonist": "与男主长期暧昧并互相扶持。",
                "features": "家族背景显赫，个人动机明确。",
                "key_events": "参与主线并多次向男主表白，不是背景板。",
            },
            {"count": 10, "summaries": ["背景显赫但不是工具人。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", background_identity)
        self.assertIn("感情/亲密推进", background_identity)

        negated_absence = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "主线女配",
                "relationship_with_protagonist": "与男主长期暧昧并同行。",
                "features": "不是客串角色，并未神隐。",
                "key_events": "多次推进主线，不是背景板。",
            },
            {"count": 9, "summaries": ["存在感较高，没有神隐，也不是工具人。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", negated_absence)
        self.assertIn("感情/亲密推进", negated_absence)

        negated_low_presence = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "主线女配",
                "relationship_with_protagonist": "与男主长期暧昧并共同处理主线。",
                "features": "存在感不低，不是低存在感角色。",
                "key_events": "多次推进主线，没有低存在感问题。",
            },
            {"count": 11, "summaries": ["存在感并不低，也不是背景板。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", negated_low_presence)
        self.assertIn("感情/亲密推进", negated_low_presence)

        summon_career = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "召唤系法师",
                "relationship_with_protagonist": "与男主长期暧昧并并肩作战，不是召唤物。",
                "features": "高频参与主线，不是召唤工具，也不是捧哏。",
                "key_events": "多次推进主线并主动表白。",
            },
            {"count": 10, "summaries": ["召唤师职业设定明确，存在感较高。"]},
            {},
        )
        self.assertNotIn("低存在感/工具人线索", summon_career)
        self.assertIn("感情/亲密推进", summon_career)

        summon_tool = report._heroine_position_level(
            {"importance_rank": 5},
            {
                "identity": "召唤助手",
                "relationship_with_protagonist": "主要帮男主做召唤物，没有恋爱线。",
                "features": "承担召唤功能。",
                "key_events": "负责召唤并说明召唤规则。",
            },
            {"count": 3, "summaries": ["负责召唤物和背景说明。"]},
            {},
        )
        self.assertIn("低存在感/工具人线索", summon_tool)

        exposition_role = report._heroine_position_level(
            {"importance_rank": 5},
            {
                "identity": "设定讲解角色",
                "relationship_with_protagonist": "负责说明背景，没有感情线。",
                "features": "功能性说明",
                "key_events": "主要负责解释背景。",
            },
            {"count": 4, "summaries": ["承担背景说明和设定解释功能。"]},
            {},
        )
        self.assertIn("低存在感/工具人线索", exposition_role)

    def test_report_does_not_promote_functional_character_without_romance_signal(self):
        level = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "探案搭档和物理输出",
                "relationship_with_protagonist": "跟随男主处理案件，负责捧哏和战斗",
                "features": "怪力少女，工具人功能明显",
                "key_events": "多次参与案件侦破，但没有恋爱、暧昧或后宫关系确认",
            },
            {"count": 12, "summaries": ["长期参与主线案件，主要承担输出和说明功能"]},
            {},
        )

        self.assertTrue(level.startswith("低证据女角色"), level)
        self.assertIn("明确缺少恋爱/后宫推进", level)
        self.assertIn("缺少感情/后宫定位证据", level)

    def test_report_ignores_tooling_heroine_word_as_relationship_position(self):
        self.assertFalse(report._has_positive_heroine_position_signal("工具人女主，负责捧哏和背景说明"))
        self.assertFalse(report._has_positive_heroine_position_signal("女主有效性存疑，存在感很低"))
        self.assertTrue(report._has_positive_heroine_position_signal("主线女主，与男主长期同行"))
        self.assertTrue(report._has_positive_heroine_position_signal("主线女主，但近期有工具人风险"))
        self.assertFalse(report._contains_positive_signal_text("她假装成男主妻子套取情报。", ["妻子"]))
        self.assertFalse(report._contains_positive_signal_text("她伪装成男主恋人混入宴会。", ["恋人"]))
        self.assertTrue(report._contains_positive_signal_text("她假装生气，但实际喜欢男主。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("她不喜欢男主，只是执行任务。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("她并不爱男主，只把他当同伴。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她明确喜欢男主，并主动表白。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("她问男主喜不喜欢这件衣服。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("男主询问她最喜欢什么。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("读者喜欢她的吐槽和搞笑桥段。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("她喜欢破案，经常协助男主调查。", ["喜欢"]))
        self.assertTrue(report._contains_positive_signal_text("她喜欢男主，并珍惜他送的簪子。", ["喜欢"]))
        self.assertFalse(report._contains_positive_signal_text("她性格活泼可爱，是搞笑担当。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她爱吃点心，经常提供笑料。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她爱哭爱笑，负责活跃气氛。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她爱冒险也爱吐槽，经常提供笑料。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她爱撒娇，但没有恋爱线。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("读者喜爱她的吐槽和搞笑桥段。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她热爱推理，主要作用是提供线索。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("三小只包括爱丽丝、小人鱼和公主。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("角色名叫爱琳，主要是女仆助手。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她性格爱慕虚荣，喜欢权势。", ["爱慕"]))
        self.assertFalse(report._contains_positive_signal_text("她被众人爱慕，追求者很多。", ["爱慕"]))
        self.assertTrue(report._contains_positive_signal_text("她爱慕男主，并主动靠近。", ["爱慕"]))
        self.assertTrue(report._contains_positive_signal_text("她爱男主，并主动告白。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她爱他，并愿意相伴余生。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她爱上男主。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("剧情让读者动心，但角色没有恋爱线。", ["动心"]))
        self.assertFalse(report._contains_positive_signal_text("剧情让读者心动，但角色没有恋爱线。", ["心动"]))
        self.assertFalse(report._contains_positive_signal_text("作者对蒸汽设定很倾心，说明文字很多。", ["倾心"]))
        self.assertTrue(report._contains_positive_signal_text("她对男主动心，并开始吃醋。", ["动心"]))
        self.assertTrue(report._contains_positive_signal_text("她对男主心动，并开始吃醋。", ["心动"]))
        self.assertTrue(report._contains_positive_signal_text("她倾心男主，并主动表白。", ["倾心"]))
        self.assertFalse(report._contains_positive_signal_text("她爱吃醋拌菜，经常负责厨房笑料。", ["吃醋"]))
        self.assertFalse(report._contains_positive_signal_text("她打翻醋坛子，众人以为她吃醋，其实只是厨房事故。", ["吃醋"]))
        self.assertTrue(report._contains_positive_signal_text("她看到男主和别的女子同行后吃醋。", ["吃醋"]))
        self.assertFalse(report._contains_positive_signal_text("旁人传言她和男主暧昧，但实际只是任务搭档。", ["暧昧"]))
        self.assertFalse(report._contains_positive_signal_text("营销炒作她和男主暧昧，没有正文互动。", ["暧昧"]))
        self.assertFalse(report._contains_positive_signal_text("误会她和男主暧昧，后来澄清只是伪装。", ["暧昧"]))
        self.assertTrue(report._contains_positive_signal_text("她与男主长期暧昧并喜欢男主。", ["暧昧"]))
        self.assertFalse(report._contains_positive_signal_text("她假装表白以套取情报，任务结束后澄清。", ["表白"]))
        self.assertFalse(report._contains_positive_signal_text("她伪装告白混入宴会，并无真实感情。", ["告白"]))
        self.assertFalse(report._contains_positive_signal_text("她念出告白台词，只是在排练舞台剧。", ["告白"]))
        self.assertTrue(report._contains_positive_signal_text("她向男主表白，并明确喜欢他。", ["表白"]))
        self.assertTrue(report._contains_positive_signal_text("她主动告白男主。", ["告白"]))
        self.assertFalse(report._contains_positive_signal_text("她负责讲解双修功法理论，没有和男主实际修炼。", ["双修"]))
        self.assertFalse(report._contains_positive_signal_text("她只是管理同房丫鬟制度，未与男主同房。", ["同房"]))
        self.assertTrue(report._contains_positive_signal_text("她与男主双修并确认关系。", ["双修"]))
        self.assertTrue(report._contains_positive_signal_text("她和男主同房后成为道侣。", ["同房"]))
        self.assertFalse(report._contains_positive_signal_text("她负责讲解道侣制度和宗门婚配规则。", ["道侣"]))
        self.assertFalse(report._contains_positive_signal_text("她讨论恋人设定和情侣桥段写法。", ["恋人", "情侣"]))
        self.assertTrue(report._contains_positive_signal_text("她与男主成为道侣。", ["道侣"]))
        self.assertTrue(report._contains_positive_signal_text("她是男主恋人，双方确认关系。", ["恋人"]))
        self.assertFalse(report._contains_positive_signal_text("她只是未婚妻设定里的奖励角色，没有正文感情。", ["未婚妻"]))
        self.assertFalse(report._contains_positive_signal_text("她讨论未婚妻模板和退婚套路。", ["未婚妻"]))
        self.assertFalse(report._contains_positive_signal_text("她并非男主未婚妻，只是政治谣言。", ["未婚妻"]))
        self.assertTrue(report._contains_positive_signal_text("她是男主未婚妻，双方感情稳定。", ["未婚妻"]))
        self.assertFalse(report._contains_positive_signal_text("她尚未成为男主妻子。", ["妻子"]))
        self.assertFalse(report._contains_positive_signal_text("她负责讲解夫妻制度和正妻模板。", ["夫妻", "正妻"]))
        self.assertFalse(report._contains_positive_signal_text("她讨论老婆称呼和女朋友桥段写法。", ["老婆", "女朋友"]))
        self.assertTrue(report._contains_positive_signal_text("她是男主妻子，夫妻感情稳定。", ["妻子", "夫妻"]))
        self.assertFalse(report._contains_positive_signal_text("她负责整理亲密接触史和接触等级说明。", ["亲密"]))
        self.assertFalse(report._contains_positive_signal_text("游戏里有亲密度系统，她只是讲解规则。", ["亲密"]))
        self.assertTrue(report._contains_positive_signal_text("她与男主亲密互动并逐渐动心。", ["亲密"]))
        self.assertFalse(report._contains_positive_signal_text("她未同房，只是住在同一院落。", ["同房"]))
        self.assertFalse(report._contains_positive_signal_text("她被路人强行亲吻。", ["亲吻"]))
        self.assertFalse(report._contains_positive_signal_text("她差点被迫同房，最后被男主救下。", ["同房"]))
        self.assertFalse(report._contains_positive_signal_text("反派企图推倒她但未遂。", ["推倒"]))
        self.assertTrue(report._contains_positive_signal_text("她与男主亲吻后确认关系。", ["亲吻"]))
        self.assertFalse(report._contains_positive_signal_text("她与男主像兄妹一样亲密，主要是亲情和战友情。", ["亲密"]))
        self.assertFalse(report._contains_positive_signal_text("她把男主当弟弟照顾，爱护后辈。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("家人反对，但她明确爱男主。", ["爱"]))
        self.assertTrue(report._has_romance_gap_signal_text("她没有感情戏，也没有恋爱线。"))
        self.assertTrue(report._has_romance_gap_signal_text("材料显示感情戏缺失。"))
        self.assertFalse(report._has_romance_gap_signal_text("她不是没有感情戏，也没有感情戏缺失问题。"))
        self.assertFalse(report._has_romance_gap_signal_text("并非没有恋爱线，未见感情推进缺失。"))

        level = report._heroine_position_level(
            {},
            {"relationship_with_protagonist": "她负责整理亲密接触史和接触等级说明。", "key_events": "多次出场"},
            {"count": 3},
            {},
        )
        self.assertNotIn("感情/亲密推进", level)

        denied_relationship_level = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "政治联姻传闻对象",
                "relationship_with_protagonist": "并非男主未婚妻，只是外界误传。",
                "key_events": "与男主未同房，未确认关系。",
            },
            {"count": 8},
            {},
        )
        self.assertFalse(denied_relationship_level.startswith("强准女主"), denied_relationship_level)
        self.assertIn("缺少感情/后宫定位证据", denied_relationship_level)

        negated_gap_level = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "主线女配",
                "relationship_with_protagonist": "与男主长期暧昧，不是没有恋爱线。",
                "key_events": "主动表白，未见感情戏缺失。",
            },
            {"count": 7},
            {},
        )
        self.assertIn("感情/亲密推进", negated_gap_level)
        self.assertNotIn("明确缺少恋爱/后宫推进", negated_gap_level)

        familial_level = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "男主姐姐",
                "relationship_with_protagonist": "与男主像兄妹一样亲密，主要是亲情和战友情。",
                "key_events": "把男主当弟弟照顾，家人式陪伴，没有恋爱线。",
            },
            {"count": 8},
            {},
        )
        self.assertNotIn("感情/亲密推进", familial_level)
        self.assertIn("明确缺少恋爱/后宫推进", familial_level)

        vain_level = report._heroine_position_level(
            {"importance_rank": 2},
            {
                "identity": "贵族女配",
                "relationship_with_protagonist": "与男主只是政治同盟，没有恋爱线。",
                "features": "性格爱慕虚荣，喜欢权势。",
                "key_events": "多次出场推动贵族支线。",
            },
            {"count": 7},
            {},
        )
        self.assertNotIn("感情/亲密推进", vain_level)
        self.assertIn("明确缺少恋爱/后宫推进", vain_level)

        personality_level = report._heroine_position_level(
            {"importance_rank": 3},
            {
                "identity": "活跃气氛的女配",
                "relationship_with_protagonist": "跟随小队行动，没有恋爱线。",
                "features": "爱哭爱笑，爱冒险也爱吐槽。",
                "key_events": "多次参与支线。",
            },
            {"count": 5},
            {},
        )
        self.assertNotIn("感情/亲密推进", personality_level)
        self.assertIn("明确缺少恋爱/后宫推进", personality_level)

        heartbeat_level = report._heroine_position_level(
            {"importance_rank": 3},
            {
                "identity": "主线女配",
                "relationship_with_protagonist": "她对男主心动，并开始吃醋。",
                "key_events": "多次参与主线。",
            },
            {"count": 5},
            {},
        )
        self.assertIn("感情/亲密推进", heartbeat_level)

    def test_leak_three_layers_ignores_non_romantic_love_words(self):
        clean = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "性格活泼可爱，经常提供笑料。",
                "key_events": "爱吃点心，负责活跃气氛，没有感情描写。",
            },
        )
        risky = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并爱男主。",
                "key_events": "结局未交代归宿。",
            },
        )
        promise_only = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "普通任务伙伴。",
                "key_events": "男主承诺帮忙寻药，并承诺守口如瓶。",
            },
        )

        self.assertIn("情感深度=未明", clean)
        self.assertIn("结论=证据不足", clean)
        self.assertIn("情感深度=有", risky)
        self.assertIn("结局交代=未明", risky)
        self.assertIn("结论=需关注", risky)
        self.assertIn("情感深度=未明", promise_only)
        self.assertIn("结论=证据不足", promise_only)

    def test_leak_three_layers_ignores_love_character_inside_names(self):
        named_only = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "爱丽丝是男主捡到的小女孩，负责支线陪伴。",
                "key_events": "爱琳偶尔客串，主要帮男主处理召唤物。",
            },
        )

        self.assertIn("情感深度=未明", named_only)
        self.assertIn("结论=证据不足", named_only)

    def test_leak_three_layers_ignores_reader_preference_and_hobbies(self):
        reader_or_hobby = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "读者喜欢她的吐槽，但她只是探案助手。",
                "key_events": "她喜欢破案也热爱推理，主要作用是提供线索。",
            },
        )

        self.assertIn("情感深度=未明", reader_or_hobby)
        self.assertIn("结论=证据不足", reader_or_hobby)

    def test_leak_three_layers_ignores_reader_or_author_emotion_words(self):
        meta_emotion = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "剧情让读者动心，但她只是案件配角。",
                "key_events": "作者对蒸汽设定很倾心，她主要负责解释背景。",
            },
        )

        self.assertIn("情感深度=未明", meta_emotion)
        self.assertIn("结论=证据不足", meta_emotion)

    def test_leak_three_layers_ignores_nonfactual_ambiguous_romance(self):
        rumor = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "旁人传言她和男主暧昧，但实际只是任务搭档。",
                "key_events": "营销炒作她和男主暧昧，后来澄清没有正文互动。",
            },
        )

        self.assertIn("情感深度=未明", rumor)
        self.assertIn("结论=证据不足", rumor)

    def test_leak_three_layers_ignores_roleplay_confession(self):
        roleplay = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她假装表白以套取情报，任务结束后澄清。",
                "key_events": "她念出告白台词，只是在排练舞台剧。",
            },
        )

        self.assertIn("情感深度=未明", roleplay)
        self.assertIn("结论=证据不足", roleplay)

    def test_leak_three_layers_ignores_cultivation_or_household_terms(self):
        generic = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她负责讲解双修功法理论，没有和男主实际修炼。",
                "key_events": "她只是管理同房丫鬟制度，未与男主同房。",
            },
        )
        confirmed = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她与男主双修并确认关系。",
                "key_events": "她和男主同房后成为道侣。",
            },
        )

        self.assertIn("关系确认=未明", generic)
        self.assertIn("结局交代=未明", generic)
        self.assertIn("关系确认=有", confirmed)

    def test_leak_three_layers_ignores_relationship_setting_terms(self):
        setting = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她负责讲解道侣制度和宗门婚配规则。",
                "key_events": "她讨论恋人设定和情侣桥段写法，没有和男主恋爱。",
            },
        )
        confirmed = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她与男主成为道侣。",
                "key_events": "她是男主恋人，双方确认关系。",
            },
        )

        self.assertIn("情感深度=未明", setting)
        self.assertIn("关系确认=未明", setting)
        self.assertIn("关系确认=有", confirmed)

    def test_leak_three_layers_ignores_nonfactual_death_or_tomb_endings(self):
        dream_or_setting = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主。",
                "key_events": "男主梦见她死亡，醒来后发现只是幻境；她负责讲解墓葬制度和坟墓结构。",
            },
        )
        accounted = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主。",
                "key_events": "结局她战死牺牲，葬在宗门后山。",
            },
        )

        self.assertIn("结局交代=未明", dream_or_setting)
        self.assertIn("结论=需关注", dream_or_setting)
        self.assertIn("结局交代=有", accounted)

    def test_leak_three_layers_ignores_non_ending_action_terms(self):
        action_terms = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主。",
                "key_events": "她留下线索后离开，跟随案件线索调查，同行后继续处理支线，还负责同行评审报告。",
            },
        )
        accounted = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主。",
                "key_events": "番外多年后她留在男主身边，与他相伴余生。",
            },
        )

        self.assertIn("结局交代=未明", action_terms)
        self.assertIn("结论=需关注", action_terms)
        self.assertIn("结局交代=有", accounted)

    def test_leak_three_layers_ignores_kitchen_vinegar_as_jealousy(self):
        kitchen = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她爱吃醋拌菜，经常负责厨房笑料。",
                "key_events": "她打翻醋坛子，众人以为她吃醋，其实只是厨房事故。",
            },
        )
        romantic = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她看到男主和别的女子同行后吃醋。",
                "key_events": "结局未交代归宿。",
            },
        )

        self.assertIn("情感深度=未明", kitchen)
        self.assertIn("结论=证据不足", kitchen)
        self.assertIn("情感深度=有", romantic)

    def test_leak_three_layers_requires_specific_concubine_terms(self):
        generic = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "双方只是讨论侍妾制度，她自称妾身开玩笑。",
                "key_events": "与男主暧昧并爱男主，结局未交代归宿。",
            },
        )
        confirmed = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "男主纳妾收入后宫，她成为通房。",
                "key_events": "结局留在男主身边。",
            },
        )

        self.assertIn("关系确认=未明", generic)
        self.assertIn("结论=需关注", generic)
        self.assertIn("关系确认=有", confirmed)

    def test_leak_three_layers_ignores_negated_relationship_and_ending_terms(self):
        negated = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主，但尚未成婚，也没有确认关系。",
                "key_events": "结局未交代归宿，最终没有留在男主身边。",
            },
        )
        accounted = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "与男主暧昧并喜欢男主。",
                "key_events": "番外多年后留在男主身边，与他相伴余生。",
            },
        )

        self.assertIn("关系确认=未明", negated)
        self.assertIn("结局交代=未明", negated)
        self.assertIn("结论=需关注", negated)
        self.assertIn("结局交代=有", accounted)

    def test_leak_three_layers_treats_fiancee_as_positive_signal(self):
        fiancee = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "男主未婚妻，双方感情稳定。",
                "key_events": "结局未交代归宿。",
            },
        )

        self.assertIn("情感深度=有", fiancee)
        self.assertIn("关系确认=有", fiancee)
        self.assertIn("结局交代=未明", fiancee)

    def test_leak_three_layers_ignores_fiancee_setting_terms(self):
        setting = report._summarize_leak_three_layers(
            {},
            {
                "relationship_with_protagonist": "她只是未婚妻设定里的奖励角色，没有正文感情。",
                "key_events": "她讨论未婚妻模板和退婚套路。",
            },
        )

        self.assertIn("情感深度=未明", setting)
        self.assertIn("关系确认=未明", setting)

    def test_harem_report_adds_heroine_context_to_issue_lines(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "甲女",
                                "aliases": ["甲姑娘"],
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            },
                            {"name": "乙女", "importance_rank": 9},
                        ]
                    },
                    "all_female_characters": {
                        "甲女": {
                            "count": 8,
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "features": "长期陪伴男主",
                                "key_events": "多次同房并确认关系",
                            },
                        },
                        "乙女": {
                            "count": 1,
                            "profile_for_report": {
                                "identity": "客串角色",
                                "features": "背景说明角色，存在感低",
                            },
                        },
                    },
                },
                {
                    "heroines_purity": [
                        {"name": "甲女", "pushed_by_male_lead": True},
                        {"name": "乙女"},
                    ],
                    "lei_points": [
                        {
                            "type": "绿帽",
                            "chunk_index": 12,
                            "content": "甲姑娘被非男主男性牵涉进婚约传闻。",
                            "review_comment": "命中甲女相关风险。",
                        }
                    ],
                    "yumen_points": [
                        {
                            "type": "工具人女主",
                            "chunk_index": 3,
                            "content": "未出现具体姓名，只说女角色工具人。",
                        }
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("女主定位上下文：甲女=目标女主", text)
        self.assertIn("证据卡：类型=绿帽；chunk=12；置信=needs_review；女主=甲女=目标女主；定义复核=需要", text)
        self.assertEqual(text.count("女主定位上下文"), 1)

    def test_harem_report_flags_strict_issue_context_for_weak_heroine(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "弱女",
                                "importance_rank": 9,
                                "summary": "受男主保护，有一次单独互动。",
                            },
                            {
                                "name": "正女",
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            },
                        ]
                    },
                    "all_female_characters": {
                        "弱女": {
                            "count": 2,
                            "profile_for_report": {
                                "identity": "支线角色",
                                "relationship_with_protagonist": "受男主保护并有单独互动",
                                "features": "配角",
                                "key_events": "协助男主一次",
                            },
                        },
                        "正女": {
                            "count": 8,
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "features": "长期陪伴男主",
                                "key_events": "确认关系",
                            },
                        },
                    },
                },
                {
                    "heroines_purity": [
                        {"name": "弱女"},
                        {"name": "正女", "pushed_by_male_lead": True},
                    ],
                    "lei_points": [
                        {
                            "type": "送女",
                            "chunk_index": 8,
                            "content": "传闻弱女被安排嫁给路人男。",
                            "review_comment": "送女风险。",
                        },
                        {
                            "type": "绿帽",
                            "chunk_index": 18,
                            "content": "正女与非男主男性出现明确关系危机。",
                            "review_comment": "绿帽风险。",
                        },
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("女主定位上下文：弱女=弱准女主", text)
        self.assertIn("定义复核提示：按锁定定义，送女/绿帽仅适用于目标女主或强准女主；弱女 当前定位偏弱，建议复核是否误判。", text)
        self.assertIn("证据含传闻/口嗨/误会/未遂/未来计划等非事实或弱证据标记", text)
        self.assertIn("女主定位上下文：正女=目标女主", text)
        self.assertEqual(text.count("定义复核提示"), 1)

    def test_harem_report_flags_strict_issue_with_nonfactual_evidence(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "正女",
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            },
                        ]
                    },
                    "all_female_characters": {
                        "正女": {
                            "count": 8,
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "features": "长期陪伴男主",
                                "key_events": "确认关系",
                            },
                        },
                    },
                },
                {
                    "heroines_purity": [{"name": "正女", "pushed_by_male_lead": True}],
                    "lei_points": [
                        {
                            "type": "绿帽",
                            "chunk_index": 18,
                            "content": "反派口嗨扬言要抢走正女，但未发生实际关系。",
                            "review_comment": "绿帽风险。",
                        },
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("女主定位上下文：正女=目标女主", text)
        self.assertIn("定义复核提示：证据含传闻/口嗨/误会/未遂/未来计划等非事实或弱证据标记", text)

    def test_harem_report_flags_strict_issue_without_heroine_anchor(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            text = report.build_report_v2(
                "测试后宫",
                {
                    "male_protagonist": {"name": "男主"},
                    "heroine_result": {
                        "heroines": [
                            {
                                "name": "甲女",
                                "importance_rank": 1,
                                "relationship_type": "道侣",
                            }
                        ]
                    },
                    "all_female_characters": {
                        "甲女": {
                            "count": 8,
                            "profile_for_report": {
                                "identity": "主线女主",
                                "relationship_with_protagonist": "男主道侣",
                                "features": "长期陪伴男主",
                                "key_events": "确认关系",
                            },
                        }
                    },
                },
                {
                    "heroines_purity": [{"name": "甲女", "pushed_by_male_lead": True}],
                    "lei_points": [
                        {
                            "type": "绿帽",
                            "chunk_index": 21,
                            "content": "有人传言漂亮女子和路人男有暧昧。",
                            "review_comment": "疑似绿帽。",
                        }
                    ],
                },
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertNotIn("女主定位上下文", text)
        self.assertIn("定义复核提示：按锁定定义，送女/绿帽必须锚定目标女主或强准女主", text)
        self.assertIn("未命中已识别女主名或别名", text)

    def test_report_annotates_issue_context_fields(self):
        contexts = [
            {
                "name": "弱女",
                "aliases": ["弱女"],
                "label": "弱准女主",
                "level": "弱准女主：有单独互动",
            }
        ]
        weak_issue, missing_issue, normal_issue = report._annotate_issues_for_report(
            [
                {"type": "送女", "content": "弱女被安排嫁给路人男。"},
                {"type": "牛头人", "content": "漂亮女子与路人男有传闻。"},
                {"type": "亵女", "content": "弱女被路人调戏。"},
            ],
            contexts,
        )

        self.assertEqual(weak_issue["heroine_position_context"], "弱女=弱准女主")
        self.assertIn("当前定位偏弱", weak_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", weak_issue["definition_review_hint"])
        self.assertEqual(weak_issue["evidence_card"]["fact_type"], "送女")
        self.assertEqual(weak_issue["evidence_card"]["source_chunk"], None)
        self.assertEqual(weak_issue["evidence_card"]["confidence"], "needs_review")
        self.assertEqual(weak_issue["evidence_card"]["matched_heroines"][0]["name"], "弱女")
        self.assertIn("弱女被安排嫁给路人男", weak_issue["evidence_card"]["evidence_text"])
        self.assertIn("当前定位偏弱", weak_issue["evidence_card"]["definition_check"])
        self.assertNotIn("heroine_position_context", missing_issue)
        self.assertIn("未命中已识别女主名或别名", missing_issue["definition_review_hint"])
        self.assertEqual(missing_issue["evidence_card"]["confidence"], "needs_review")
        self.assertEqual(missing_issue["evidence_card"]["matched_heroines"], [])
        self.assertEqual(normal_issue["heroine_position_context"], "弱女=弱准女主")
        self.assertNotIn("definition_review_hint", normal_issue)
        self.assertEqual(normal_issue["evidence_card"]["confidence"], "confirmed")

        strong_passive_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "强女被家族安排政治联姻，嫁给路人男。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(strong_passive_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("送女必须有男主主动或默许构成", strong_passive_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", strong_passive_issue["definition_review_hint"])

        third_party_send_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "反派计划把强女送给路人男，男主未参与。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(third_party_send_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("第三方送人", third_party_send_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", third_party_send_issue["definition_review_hint"])

        family_send_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "家族把强女安排给路人男联姻，男主没有参与。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(family_send_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("第三方送人", family_send_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", family_send_issue["definition_review_hint"])

        reader_cp_send_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "读者脑补强女和路人男组CP，正文没有事实，也没有男主主动撮合。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(reader_cp_send_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("非事实或弱证据标记", reader_cp_send_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", reader_cp_send_issue["definition_review_hint"])

        bystander_matchmaking_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "旁人撮合强女嫁给路人男，男主没有参与。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(bystander_matchmaking_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("第三方送人", bystander_matchmaking_issue["definition_review_hint"])
        self.assertIn("缺少男主主体", bystander_matchmaking_issue["definition_review_hint"])

        active_send_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "男主主动安排强女嫁给路人男，强女被安排嫁给路人男。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(active_send_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("definition_review_hint", active_send_issue)

        negated_active_send_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "强女被安排嫁给路人男，但男主没有主动安排，也没有默许。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(negated_active_send_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("缺少男主主体", negated_active_send_issue["definition_review_hint"])

        rescued_arrangement_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "强女被家族安排嫁给路人男，男主主动救下强女并阻止联姻。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(rescued_arrangement_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("缺少男主主体", rescued_arrangement_issue["definition_review_hint"])

        refused_arrangement_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "强女被长辈撮合给路人男，男主拒绝撮合，没有送出强女。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(refused_arrangement_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("缺少男主主体", refused_arrangement_issue["definition_review_hint"])

        victim_only_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女被反派绑走调戏，但没有性关系也没有情感背叛。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(victim_only_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽必须有明确暧昧/恋爱/性关系或实质情感背叛", victim_only_ntr_issue["definition_review_hint"])
        self.assertIn("应复核是否应降为亵女/虐女/NTR擦边", victim_only_ntr_issue["definition_review_hint"])

        forced_kiss_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女被路人男强吻，但没有暧昧也没有喜欢上对方。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(forced_kiss_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽必须有明确暧昧/恋爱/性关系或实质情感背叛", forced_kiss_ntr_issue["definition_review_hint"])
        self.assertIn("应复核是否应降为亵女/虐女/NTR擦边", forced_kiss_ntr_issue["definition_review_hint"])

        harassment_ntr_issue = report._annotate_issue_for_report(
            {"type": "NTR", "content": "强女被反派猥亵未遂，未恋爱也未背叛男主。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(harassment_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽必须有明确暧昧/恋爱/性关系或实质情感背叛", harassment_ntr_issue["definition_review_hint"])
        self.assertIn("应复核是否应降为亵女/虐女/NTR擦边", harassment_ntr_issue["definition_review_hint"])

        attempted_intercourse_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女被反派下药，差点被迫同房，但最后被男主救下，未发生性关系。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(attempted_intercourse_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽必须有明确暧昧/恋爱/性关系或实质情感背叛", attempted_intercourse_ntr_issue["definition_review_hint"])
        self.assertIn("应复核是否应降为亵女/虐女/NTR擦边", attempted_intercourse_ntr_issue["definition_review_hint"])

        factual_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女与路人男发生性关系并背叛男主。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(factual_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("definition_review_hint", factual_ntr_issue)

        reader_cp_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "读者调侃强女和路人男有CP感，正文无暧昧也没有实锤。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(reader_cp_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("非事实或弱证据标记", reader_cp_ntr_issue["definition_review_hint"])

        male_lead_expansion_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "男主与强女的闺蜜发生关系，强女吃醋。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(male_lead_expansion_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽排除男主与女主亲友或其他女性发生关系", male_lead_expansion_issue["definition_review_hint"])
        self.assertIn("后宫扩张/推土机情节", male_lead_expansion_issue["definition_review_hint"])

        same_subject_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女与男主分身同房，但该分身由男主本人操控，本质上是男主本人。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(same_subject_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("绿帽要求对象是非男主男性", same_subject_ntr_issue["definition_review_hint"])
        self.assertIn("分身流风险复核", same_subject_ntr_issue["definition_review_hint"])

        independent_clone_ntr_issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "强女与男主分身发生关系，但分身已有独立人格并脱离男主控制。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(independent_clone_ntr_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("同一主体", independent_clone_ntr_issue.get("definition_review_hint", ""))

        sent_to_male_lead_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "配角把强女献给男主，男主接收强女入后宫。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(sent_to_male_lead_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("送女排除配角/家族/反派把女性献给男主", sent_to_male_lead_issue["definition_review_hint"])
        self.assertIn("收女/献女/后宫扩张", sent_to_male_lead_issue["definition_review_hint"])

        taken_into_harem_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "配角把强女献给男主，强女被男主纳入后宫。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(taken_into_harem_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("收女/献女/后宫扩张", taken_into_harem_issue["definition_review_hint"])

        marriage_invitation_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "女王向男主发起与强女的联姻邀请，被男主拒绝。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(marriage_invitation_issue["heroine_position_context"], "强女=强准女主")
        self.assertIn("送女排除配角/家族/反派把女性献给男主", marriage_invitation_issue["definition_review_hint"])
        self.assertIn("联姻邀请/拒绝收女", marriage_invitation_issue["definition_review_hint"])

        active_send_to_other_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "男主主动把强女送给路人男。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(active_send_to_other_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("definition_review_hint", active_send_to_other_issue)

        received_by_other_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "男主主动把强女送给路人男，路人男接收强女。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(received_by_other_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("definition_review_hint", received_by_other_issue)

        rejected_then_sent_to_other_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "强女表白男主被男主拒绝后，男主主动把强女送给路人男。"},
            [
                {
                    "name": "强女",
                    "aliases": ["强女"],
                    "label": "强准女主",
                    "level": "强准女主：感情推进",
                }
            ],
        )
        self.assertEqual(rejected_then_sent_to_other_issue["heroine_position_context"], "强女=强准女主")
        self.assertNotIn("definition_review_hint", rejected_then_sent_to_other_issue)

        edge_issue = report._annotate_issue_for_report(
            {"type": "绿帽擦边", "content": "弱女差点被反派绑走。"},
            contexts,
        )
        self.assertEqual(edge_issue["heroine_position_context"], "弱女=弱准女主")
        self.assertNotIn("definition_review_hint", edge_issue)

    def test_reviewer_summary_serializes_issue_evidence_cards(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        reviewer_path = os.path.join(base_dir, "novel_reviewer.py")
        with open(reviewer_path, "r", encoding="utf-8") as f:
            text = f.read()

        self.assertIn("def _issue_evidence_card(item, confidence):", text)
        self.assertIn('"evidence_card": _issue_evidence_card(item, "confirmed")', text)
        self.assertIn('"evidence_card": _issue_evidence_card(item, "pending")', text)
        self.assertIn('"evidence_card": _issue_evidence_card(item, "rejected")', text)

    def test_report_ignores_unsafe_manual_issue_anchor_aliases(self):
        contexts = [
            {
                "name": "安",
                "aliases": ["安", "公主"],
                "label": "目标女主",
                "level": "目标女主：确认关系",
            },
            {
                "name": "琪雅",
                "aliases": ["琪雅"],
                "label": "强准女主",
                "level": "强准女主：感情推进",
            },
        ]

        short_alias_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "有人传言公主被安排嫁给路人男。"},
            contexts,
        )
        substring_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "安妮被安排嫁给路人男。"},
            contexts,
        )
        named_issue = report._annotate_issue_for_report(
            {"type": "送女", "content": "琪雅被安排嫁给路人男。"},
            contexts,
        )

        self.assertNotIn("heroine_position_context", short_alias_issue)
        self.assertIn("未命中已识别女主名或别名", short_alias_issue["definition_review_hint"])
        self.assertNotIn("heroine_position_context", substring_issue)
        self.assertIn("未命中已识别女主名或别名", substring_issue["definition_review_hint"])
        self.assertEqual(named_issue["heroine_position_context"], "琪雅=强准女主")
        self.assertIn("缺少男主主体", named_issue["definition_review_hint"])

    def test_report_ignores_generic_heroine_anchor_names_for_strict_issues(self):
        generic_contexts = report._build_heroine_position_contexts(
            [{"name": "公主", "importance_rank": 1}],
            {
                "公主": {
                    "count": 8,
                    "profile_for_report": {
                        "identity": "王室成员",
                        "relationship_with_protagonist": "未描述",
                        "features": "偶尔出场",
                        "key_events": "被传联姻",
                    },
                }
            },
            {
                "公主": {
                    "identity": "王室成员",
                    "relationship_with_protagonist": "未描述",
                    "features": "偶尔出场",
                    "key_events": "被传联姻",
                }
            },
            {},
        )
        self.assertEqual(generic_contexts, [])

        issue = report._annotate_issue_for_report(
            {"type": "绿帽", "content": "有人传言公主和路人男暧昧。"},
            generic_contexts,
        )
        self.assertNotIn("heroine_position_context", issue)
        self.assertIn("未命中已识别女主名或别名", issue["definition_review_hint"])

        named_contexts = report._build_heroine_position_contexts(
            [{"name": "琪雅", "aliases": ["公主"], "importance_rank": 1}],
            {"琪雅": {"count": 6, "profile_for_report": {"identity": "主线女主"}}},
            {"琪雅": {"identity": "主线女主"}},
            {},
        )
        self.assertEqual(named_contexts[0]["aliases"], ["琪雅"])

        title_contexts = report._build_heroine_position_contexts(
            [{"name": "帝国公主", "importance_rank": 1}],
            {
                "帝国公主": {
                    "count": 6,
                    "profile_for_report": {
                        "identity": "王室成员",
                        "relationship_with_protagonist": "未描述",
                        "features": "政治联姻称号",
                    },
                }
            },
            {"帝国公主": {"identity": "王室成员"}},
            {},
        )
        self.assertEqual(title_contexts, [])

        named_with_title_alias = report._build_heroine_position_contexts(
            [{"name": "琪雅", "aliases": ["帝国公主"], "importance_rank": 1}],
            {"琪雅": {"count": 6, "profile_for_report": {"identity": "主线女主"}}},
            {"琪雅": {"identity": "主线女主"}},
            {},
        )
        self.assertEqual(named_with_title_alias[0]["aliases"], ["琪雅"])

    def test_reviewer_derives_past_life_cleanliness(self):
        risky = novel_reviewer._derive_past_life_cleanliness(
            {
                "partner_relations": [
                    {
                        "partner": "原著前夫",
                        "is_male_lead": False,
                        "relationship": "前夫",
                        "evidence": "原故事线里她嫁给原著前夫",
                    }
                ]
            },
            "",
        )
        clean = novel_reviewer._derive_past_life_cleanliness({}, "前世只提到修炼经历，没有婚恋线索。")
        none = novel_reviewer._derive_past_life_cleanliness({}, "当前线没有前史。")

        self.assertFalse(risky["past_life_clean"])
        self.assertEqual(risky["past_life_severity"], "partner")
        self.assertIn("风险线索", risky["past_life_status"])
        self.assertTrue(clean["past_life_clean"])
        self.assertEqual(clean["past_life_severity"], "clean")
        self.assertIsNone(none["past_life_clean"])
        self.assertEqual(none["past_life_severity"], "none")

        severe = novel_reviewer._derive_past_life_cleanliness(
            {"sexual_relations": [{"partner": "反派", "is_male_lead": False, "evidence": "前世她被反派强暴侵犯。"}]},
            "",
        )
        sexual = novel_reviewer._derive_past_life_cleanliness(
            {"sexual_relations": [{"partner": "前夫", "is_male_lead": False, "evidence": "原故事线里她与前夫圆房。"}]},
            "",
        )
        romantic = novel_reviewer._derive_past_life_cleanliness({}, "上一世她喜欢过别的男人但没有关系。")
        self.assertEqual(severe["past_life_severity"], "severe")
        self.assertEqual(sexual["past_life_severity"], "sexual")
        self.assertEqual(romantic["past_life_severity"], "romantic")

    def test_reviewer_ignores_negated_or_nonfactual_past_life_rumors(self):
        rumor = novel_reviewer._derive_past_life_cleanliness({}, "前世有人传言她嫁给过别人，但后来证实是误会。")
        hearsay = novel_reviewer._derive_past_life_cleanliness({}, "听说前世她曾与别人有婚约，后来澄清是流言，证实不成立。")
        alleged = novel_reviewer._derive_past_life_cleanliness({}, "据说原故事线她喜欢过别人，但文本没有证据，属于读者猜测。")
        negated_love = novel_reviewer._derive_past_life_cleanliness({}, "前世她没有喜欢过别的男人。")
        negated_marriage = novel_reviewer._derive_past_life_cleanliness({}, "原故事线里她未嫁给任何人，也没有同房。")
        negated_partner = novel_reviewer._derive_past_life_cleanliness({}, "前世她没有丈夫也没有男友。")
        mistaken_partner = novel_reviewer._derive_past_life_cleanliness({}, "原故事线里她被误认为嫁给路人，但后来澄清不是事实。")
        nominal_engagement = novel_reviewer._derive_past_life_cleanliness({}, "前世她与路人只是政治婚约，未嫁也无感情。")
        joke_title = novel_reviewer._derive_past_life_cleanliness({}, "前世夫君这个称呼只是同伴玩笑。")
        real_romance = novel_reviewer._derive_past_life_cleanliness({}, "上一世她喜欢过别的男人。")

        self.assertTrue(rumor["past_life_clean"])
        self.assertEqual(rumor["past_life_severity"], "clean")
        self.assertTrue(hearsay["past_life_clean"])
        self.assertEqual(hearsay["past_life_severity"], "clean")
        self.assertTrue(alleged["past_life_clean"])
        self.assertEqual(alleged["past_life_severity"], "clean")
        self.assertTrue(negated_love["past_life_clean"])
        self.assertTrue(negated_marriage["past_life_clean"])
        self.assertTrue(negated_partner["past_life_clean"])
        self.assertEqual(negated_partner["past_life_severity"], "clean")
        self.assertTrue(mistaken_partner["past_life_clean"])
        self.assertEqual(mistaken_partner["past_life_severity"], "clean")
        self.assertTrue(nominal_engagement["past_life_clean"])
        self.assertEqual(nominal_engagement["past_life_severity"], "clean")
        self.assertTrue(joke_title["past_life_clean"])
        self.assertEqual(joke_title["past_life_severity"], "clean")
        self.assertEqual(real_romance["past_life_severity"], "romantic")

    def test_reviewer_keeps_explicit_past_life_risk_in_mixed_nonfactual_context(self):
        mixed_romance = novel_reviewer._derive_past_life_cleanliness(
            {},
            "前世传言她嫁给别人是误会，但原故事线她确实喜欢过反派。",
        )
        mixed_fact = novel_reviewer._derive_past_life_cleanliness(
            {
                "partner_relations": [
                    {
                        "partner": "路人甲",
                        "is_male_lead": False,
                        "relationship": "传闻前夫",
                        "evidence": "前世有人传言她嫁给路人甲，但后来证实是误会。",
                    },
                    {
                        "partner": "反派",
                        "is_male_lead": False,
                        "relationship": "恋慕对象",
                        "evidence": "原故事线她明确喜欢过反派。",
                    },
                ]
            },
            "",
        )

        self.assertFalse(mixed_romance["past_life_clean"])
        self.assertEqual(mixed_romance["past_life_severity"], "romantic")
        self.assertFalse(mixed_fact["past_life_clean"])
        self.assertEqual(mixed_fact["past_life_severity"], "romantic")

    def test_reviewer_derives_contact_level(self):
        level0 = novel_reviewer._derive_contact_level({}, "男主")
        level3 = novel_reviewer._derive_contact_level(
            {
                "physical_contacts": [
                    {
                        "partner": "反派",
                        "is_male_lead": False,
                        "contact_type": "强吻",
                        "evidence": "反派强吻了她",
                    }
                ]
            },
            "男主",
        )
        level5 = novel_reviewer._derive_contact_level(
            {
                "sexual_relations": [
                    {
                        "partner": "前夫",
                        "is_male_lead": False,
                        "detail": "前世同房",
                        "evidence": "前世她与前夫同房",
                    }
                ]
            },
            "男主",
        )
        level1 = novel_reviewer._derive_contact_level(
            {},
            "男主",
            ["反派在宴会上言语调戏她，但没有实际身体接触。"],
        )

        self.assertEqual(level0["contact_level"], "L0")
        self.assertEqual(level1["contact_level"], "L1")
        self.assertIn("言语调戏", level1["contact_level_label"])
        self.assertEqual(level3["contact_level"], "L3")
        self.assertIn("强迫亲密", level3["contact_level_label"])
        self.assertEqual(level5["contact_level"], "L5")
        self.assertIn("性关系", level5["contact_level_label"])

    def test_reviewer_formats_purity_supplement_for_text_report(self):
        lines = novel_reviewer._format_heroine_purity_supplement(
            {
                "contact_level": "L2",
                "contact_level_label": "被迫婚约/伴侣关系线索",
                "contact_level_reason": "原故事线里曾被安排嫁给非男主。",
                "past_life_severity": "partner",
                "past_life_severity_label": "前世/原故事线伴侣或婚约风险",
                "past_life_status": "前世/原故事线存在风险线索",
                "past_life_reason": "原故事线里曾被安排嫁给非男主。",
            }
        )

        text = "\n".join(lines)
        self.assertIn("接触等级: L2（被迫婚约/伴侣关系线索）", text)
        self.assertIn("前世洁度: 前世/原故事线存在风险线索", text)
        self.assertIn("前世风险等级: partner（前世/原故事线伴侣或婚约风险）", text)

    def test_spirit_judge_exposes_partner_exemption_notes(self):
        old_chat = novel_reviewer.chat_completion
        old_record = novel_reviewer.record_usage

        class FakeMessage:
            content = json.dumps({
                "is_spirit_clean": True,
                "spirit_status": "✅ 精神洁（被迫联姻无感情）",
                "spirit_reason": "被迫婚约且无感情投入",
                "loved_others": [],
            }, ensure_ascii=False)

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        try:
            novel_reviewer.chat_completion = lambda **kwargs: FakeResponse()
            novel_reviewer.record_usage = lambda response: None
            result = novel_reviewer._llm_judge_spirit(
                "甲女",
                no_partner=False,
                partner_list=[{"name": "原著前夫", "relationship": "政治婚约", "forced": True, "has_feelings": False}],
                has_biological_children=False,
                biological_children=[],
                romantic_feelings=[],
                male_lead="男主",
                analyzed_partners=[
                    {
                        "partner": "原著前夫",
                        "relationship": "政治婚约",
                        "forced": True,
                        "has_feelings": False,
                        "analysis_reason": "被迫订婚且无感情",
                    }
                ],
                all_partner_relations=[
                    {
                        "partner": "原著前夫",
                        "is_male_lead": False,
                        "relationship": "政治婚约",
                        "status": "订婚未圆房",
                        "forced": True,
                        "has_feelings": False,
                        "evidence": "她被迫与原著前夫订婚，未圆房，也从未动心。",
                    }
                ],
                sexual_relations=[],
            )
        finally:
            novel_reviewer.chat_completion = old_chat
            novel_reviewer.record_usage = old_record

        self.assertTrue(result["is_spirit_clean"])
        self.assertTrue(result["partner_exempted_for_clean"])
        self.assertIn("原著前夫", result["partner_exemption_reason"])
        self.assertIn("forced=true", result["partner_exemption_reason"])
        self.assertTrue(result["partner_exemption_notes"])

    def test_normalize_clean_accepts_top_level_partner_exemption(self):
        result = novel_reviewer._normalize_purity_result_consistency({
            "name": "甲女",
            "is_virgin": True,
            "virgin_status": "✅ 处女",
            "has_other_contact": False,
            "contact_status": "✅ 无接触",
            "no_partner": False,
            "partner_status": "❌ 有男伴（被迫政治婚约）",
            "is_spirit_clean": True,
            "spirit_status": "✅ 精神洁（被迫无感情）",
            "partner_exempted_for_clean": True,
            "is_clean": False,
        })

        self.assertTrue(result["is_clean"])

    def test_stepwise_clean_allows_partner_exemption(self):
        old_child = novel_reviewer._llm_judge_child_origin
        old_partner = novel_reviewer._llm_judge_partner
        old_contact = novel_reviewer._llm_judge_contact
        old_spirit = novel_reviewer._llm_judge_spirit
        old_virgin = novel_reviewer._llm_judge_virgin
        try:
            novel_reviewer._llm_judge_child_origin = lambda *args, **kwargs: {
                "has_biological_children": False,
                "biological_children": [],
            }
            novel_reviewer._llm_judge_partner = lambda *args, **kwargs: {
                "no_partner": False,
                "partner_status": "❌ 有男伴（被迫政治婚约）",
                "partner_reason": "曾被安排政治婚约，但无感情且未圆房",
                "partner_list": [{"name": "原著前夫", "relationship": "政治婚约", "forced": True, "has_feelings": False}],
                "analyzed_partners": [{"partner": "原著前夫", "forced": True, "has_feelings": False}],
            }
            novel_reviewer._llm_judge_contact = lambda *args, **kwargs: {
                "has_other_contact": False,
                "contact_status": "✅ 无接触",
                "contact_reason": "未见非男主身体接触",
            }
            novel_reviewer._llm_judge_spirit = lambda *args, **kwargs: {
                "is_spirit_clean": True,
                "spirit_status": "✅ 精神洁（partner豁免）",
                "spirit_reason": "被迫婚约且无感情投入",
                "partner_exempted_for_clean": True,
                "partner_exemption_notes": [{"partner": "原著前夫", "reason": "forced=true, has_feelings=false"}],
                "partner_exemption_reason": "原著前夫：forced=true，has_feelings=false",
            }
            novel_reviewer._llm_judge_virgin = lambda *args, **kwargs: {
                "is_virgin": True,
                "virgin_status": "✅ 处女",
                "virgin_reason": "未见非男主性关系",
            }

            result = novel_reviewer.judge_purity_by_llm_stepwise(
                "甲女",
                {
                    "sexual_relations": [],
                    "children_info": [],
                    "physical_contacts": [],
                    "romantic_feelings": [],
                    "partner_relations": [
                        {
                            "partner": "原著前夫",
                            "is_male_lead": False,
                            "relationship": "政治婚约",
                            "status": "订婚未圆房",
                            "forced": True,
                            "has_feelings": False,
                            "evidence": "她被迫与原著前夫订婚，未圆房，也从未动心。",
                        }
                    ],
                },
                {},
                "男主",
            )
        finally:
            novel_reviewer._llm_judge_child_origin = old_child
            novel_reviewer._llm_judge_partner = old_partner
            novel_reviewer._llm_judge_contact = old_contact
            novel_reviewer._llm_judge_spirit = old_spirit
            novel_reviewer._llm_judge_virgin = old_virgin

        self.assertFalse(result["no_partner"])
        self.assertTrue(result["partner_exempted_for_clean"])
        self.assertTrue(result["is_clean"])
        self.assertTrue(result["verification"]["partner_exempted_for_clean"])

    def test_report_summarizes_heroine_relationship_structure(self):
        summary = report._summarize_heroine_relationship_structure(
            {
                "identity": "被安排和亲的公主",
                "features": "经济上依附家族资源",
                "relationship_with_protagonist": "主仆权力关系逐渐转为暧昧",
                "key_events": "曾被反派下药囚禁",
            },
            {},
        )

        self.assertIn("经济依附", summary)
        self.assertIn("权力关系", summary)
        self.assertIn("政治联姻/婚约", summary)
        self.assertIn("受害/胁迫记录", summary)

        structured = report._summarize_heroine_relationship_structure(
            {},
            {
                "purity_facts": {
                    "economic_attachments": [{"benefactor": "王公子"}],
                    "power_relations": [{"superior": "宗主"}],
                    "political_marriages": [{"partner": "世子"}],
                    "victim_records": [{"perpetrator": "反派"}],
                }
            },
        )
        self.assertIn("经济依附", structured)
        self.assertIn("权力关系", structured)
        self.assertIn("政治联姻/婚约", structured)
        self.assertIn("受害/胁迫记录", structured)

    def test_reviewer_preserves_extended_purity_facts(self):
        facts = {
            "children_info": [
                {"child_name": "甲女", "evidence": "王妃生下甲女。"},
                {"child_name": "小甲", "father": "王公子", "evidence": "甲女生下小甲。"},
            ],
            "economic_attachments": [
                {"benefactor": "王公子", "relationship": "债务", "evidence": "甲女因欠债被王公子控制。"}
            ],
            "power_relations": [
                {"superior": "宗主", "relationship": "师徒", "evidence": "宗主以师命压迫甲女。"}
            ],
            "political_marriages": [
                {"partner": "世子", "type": "和亲", "status": "planned", "evidence": "甲女被安排与世子和亲。"}
            ],
            "victim_records": [
                {"perpetrator": "反派", "type": "下药", "outcome": "未遂", "evidence": "反派给甲女下药未遂。"}
            ],
        }

        cleaned = novel_reviewer._sanitize_purity_facts_for_heroine("甲女", facts)

        self.assertEqual(len(cleaned["children_info"]), 1)
        for dim in ["economic_attachments", "power_relations", "political_marriages", "victim_records"]:
            self.assertEqual(len(cleaned[dim]), 1)

    def test_reviewer_loads_and_merges_extended_purity_facts(self):
        detail_payload = {
            "all_female_characters": {
                "甲女": {
                    "purity_facts": {
                        "economic_attachments": [
                            {"benefactor": "王公子", "evidence": "甲女因欠债被王公子控制。"}
                        ],
                        "victim_records": [
                            {"perpetrator": "反派", "evidence": "反派给甲女下药未遂。"}
                        ],
                    }
                }
            }
        }
        raw_payload = {
            "heroine_facts": [
                {
                    "name": "甲女",
                    "facts": {
                        "political_marriages": [
                            {"partner": "世子", "evidence": "甲女被安排与世子和亲。"}
                        ]
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            detail_path = os.path.join(tmpdir, "novel_detailed_test.json")
            raw_path = os.path.join(tmpdir, "raw_data.json")
            with open(detail_path, "w", encoding="utf-8") as f:
                json.dump(detail_payload, f, ensure_ascii=False)
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(raw_payload, f, ensure_ascii=False)

            detail = novel_reviewer._load_detail_evidence(detail_path)
            raw = novel_reviewer._load_scan_facts(raw_path)

        self.assertEqual(len(detail["甲女"]["purity_facts"]["economic_attachments"]), 1)
        self.assertEqual(len(detail["甲女"]["purity_facts"]["victim_records"]), 1)
        self.assertEqual(len(raw["甲女"]["political_marriages"]), 1)

        result = novel_reviewer.judge_character_purity_by_facts(
            "甲女",
            raw["甲女"],
            detail["甲女"],
            "男主",
        )
        self.assertTrue(result["is_clean"])

    def test_harem_report_dedupes_title_variants_with_llm_decision(self):
        old_judge = report._llm_judge_heroine_duplicate_group
        try:
            report._llm_judge_heroine_duplicate_group = lambda group: {
                "same_person": True,
                "canonical_name": "沈南歌（太后）",
                "aliases": [item["name"] for item in group],
                "reason": "同一角色称谓变体",
            }
            heroines = report.dedupe_heroines_for_report(
                [
                    {"name": "沈南歌（太后）", "importance_rank": 1},
                    {"name": "太后沈南歌", "importance_rank": 2},
                    {"name": "苏青绮", "importance_rank": 3},
                ],
                {},
            )
        finally:
            report._llm_judge_heroine_duplicate_group = old_judge

        names = [item["name"] for item in heroines]
        self.assertEqual(names.count("沈南歌（太后）"), 1)
        self.assertNotIn("太后沈南歌", names)
        self.assertIn("苏青绮", names)

    def test_harem_report_dedupes_short_core_name_variants_without_llm(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            heroines = report.dedupe_heroines_for_report(
                [
                    {"name": "沈南歌", "importance_rank": 1},
                    {"name": "南歌", "importance_rank": 2},
                    {"name": "太后沈南歌", "importance_rank": 3},
                    {"name": "苏青绮", "importance_rank": 4},
                    {"name": "青绮", "importance_rank": 5},
                    {"name": "爱琳", "importance_rank": 6},
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        names = [item["name"] for item in heroines]
        self.assertEqual(names.count("太后沈南歌"), 1)
        self.assertNotIn("沈南歌", names)
        self.assertNotIn("南歌", names)
        self.assertEqual(names.count("苏青绮"), 1)
        self.assertNotIn("青绮", names)
        self.assertIn("爱琳", names)

    def test_harem_report_keeps_parallel_world_name_variants_without_llm(self):
        old_openai = report.OpenAI
        old_api_key_pool = report.API_KEY_POOL
        try:
            report.OpenAI = None
            report.API_KEY_POOL = []
            heroines = report.dedupe_heroines_for_report(
                [
                    {"name": "安妮", "importance_rank": 1},
                    {"name": "另一个世界的安妮", "importance_rank": 2},
                    {"name": "未来线安妮", "importance_rank": 3},
                    {"name": "太后沈南歌", "importance_rank": 4},
                    {"name": "沈南歌", "importance_rank": 5},
                ],
                {},
            )
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        names = [item["name"] for item in heroines]
        self.assertIn("安妮", names)
        self.assertIn("另一个世界的安妮", names)
        self.assertIn("未来线安妮", names)
        self.assertEqual(names.count("太后沈南歌"), 1)
        self.assertNotIn("沈南歌", names)

    def test_harem_report_keeps_variants_when_llm_rejects_merge(self):
        old_judge = report._llm_judge_heroine_duplicate_group
        try:
            report._llm_judge_heroine_duplicate_group = lambda group: {
                "same_person": False,
                "canonical_name": "",
                "aliases": [],
                "reason": "不是同一角色",
            }
            heroines = report.dedupe_heroines_for_report(
                [
                    {"name": "沈南歌（太后）", "importance_rank": 1},
                    {"name": "太后沈南歌", "importance_rank": 2},
                ],
                {},
            )
        finally:
            report._llm_judge_heroine_duplicate_group = old_judge

        self.assertEqual([item["name"] for item in heroines], ["沈南歌（太后）", "太后沈南歌"])

    def test_report_suffix_distinguishes_general_specialties(self):
        general = analysis_profiles.load_analysis_profile("general")
        history = analysis_profiles.load_analysis_profile("history")
        harem = analysis_profiles.load_analysis_profile("harem")

        self.assertEqual(report.report_suffix_for_profile(general), "通用小说报告")
        self.assertIn("历史小说专长分析报告", report.report_suffix_for_profile(history))
        self.assertEqual(report.report_suffix_for_profile(harem), "扫书报告")

    def test_general_scan_uses_profile_rules_and_specialty_notes(self):
        profile = analysis_profiles.load_analysis_profile("xianxia_fantasy")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角入宗门"],
                    "conflicts": ["宗门资源争夺"],
                    "worldbuilding": ["金丹元婴境界"],
                    "themes": ["逆袭"],
                    "foreshadowing": ["秘境传承"],
                    "quality_notes": ["升级节奏快"],
                    "specialty_notes": ["境界体系清晰"],
                    "one_sentence_summary": "主角开始修炼。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("宗门弟子修炼金丹元婴。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertIn("境界体系清晰", result["specialty_notes"])
        self.assertEqual(result["prompt_template"]["name"], "general_scan_chunk")
        self.assertEqual(result["prompt_template"]["version"], "v1")
        self.assertTrue(any("修炼体系与战力" in prompt for prompt in prompts))

    def test_general_scan_uses_light_prompt_for_low_density_chunks(self):
        profile = analysis_profiles.load_analysis_profile("general")
        calls = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                calls.append({
                    "prompt": "\n".join(item.get("content", "") for item in messages),
                    "max_tokens": max_tokens,
                })
                return {
                    "plot_events": ["主角赶路后休息"],
                    "conflicts": [],
                    "worldbuilding": [],
                    "themes": [],
                    "foreshadowing": [],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "one_sentence_summary": "主角赶路休息。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("主角赶路，吃饭，睡觉休息。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(result["density_profile"]["level"], "low")
        self.assertEqual(result["density_profile"]["strategy"], "light")
        self.assertEqual(calls[0]["max_tokens"], 2400)
        self.assertIn("密度策略：light", calls[0]["prompt"])

    def test_general_scan_entity_prescan_extracts_candidates_safely(self):
        text = """
第一章：青云城旧案
“先退下。”张三说道。
李四道：“我去玄天宗查线索。”
旁人只知道他名叫赵明远。
青云城中，玄天宗弟子往来，青云城旧案反复被提起。
青云城旧案青云城旧案青云城旧案。
"""
        candidates = general_scan._entity_prescan_candidates(text, max_items=10, max_chars=1000)
        by_name = {item["name"]: item for item in candidates}

        self.assertIn("张三", by_name)
        self.assertNotIn("张三说", by_name)
        self.assertNotIn("云城", by_name)
        self.assertNotIn("案青云城", by_name)
        self.assertIn("李四", by_name)
        self.assertIn("赵明远", by_name)
        self.assertIn("青云城", by_name)
        self.assertIn("玄天宗", by_name)
        self.assertEqual(by_name["张三"]["entity_type"], "person")
        self.assertEqual(by_name["青云城"]["entity_type"], "location")
        self.assertEqual(by_name["玄天宗"]["entity_type"], "organization")

    def test_general_scan_entity_prescan_prompt_is_hint_only(self):
        section = general_scan._entity_prescan_prompt_section([
            {"name": "张三", "entity_type": "person", "confidence": "high", "score": 5},
            {"name": "青云城", "entity_type": "location", "confidence": "medium", "score": 4},
        ])

        self.assertIn("全书预扫描实体候选", section)
        self.assertIn("只用于提醒不要漏掉高频实体", section)
        self.assertIn("仍必须以当前片段原文为准", section)
        self.assertIn("张三（人物", section)
        self.assertIn("青云城（地点", section)

    def test_general_scan_injects_entity_prescan_into_chunk_prompt(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["张三抵达青云城"],
                    "conflicts": [],
                    "worldbuilding": ["青云城是旧案地点"],
                    "themes": [],
                    "foreshadowing": [],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "one_sentence_summary": "张三抵达青云城。",
                }

            general_scan._call_json = fake_call_json
            general_scan._scan_chunk(
                "张三抵达城门。",
                0,
                1,
                profile=profile,
                entity_prescan=[
                    {"name": "张三", "entity_type": "person", "confidence": "high", "score": 5},
                ],
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("全书预扫描实体候选" in prompt for prompt in prompts))
        self.assertTrue(any("张三（人物" in prompt for prompt in prompts))

    def test_general_scan_outputs_writing_quality_pacing_and_density(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角审问嫌疑人"],
                    "conflicts": ["真相与谎言冲突"],
                    "worldbuilding": ["近代城市警务"],
                    "themes": ["真相代价"],
                    "foreshadowing": [],
                    "quality_notes": ["对话推进线索"],
                    "specialty_notes": [],
                    "writing_quality": {
                        "prose_quality": {"score": 6.75, "strength": "表达清楚", "weakness": "句式略平"},
                        "character_depth": {"score": 7, "strength": "角色动机明确", "weakness": ""},
                        "narrative_technique": {"score": 8, "strength": "信息控制有效", "weakness": ""},
                        "dialogue_quality": {"score": 7, "strength": "问答有推进", "weakness": "声口区分一般"},
                        "scene_description": {"score": 5, "strength": "空间清楚", "weakness": "感官不足"},
                        "emotional_impact": {"score": 6, "strength": "紧张感稳定", "weakness": ""},
                        "info_density": {"score": 8, "water_chapter_score": 2, "strength": "线索密集", "weakness": ""},
                        "worldbuilding_integration": {"score": 6, "strength": "警务设定服务剧情", "weakness": ""},
                        "zhihu_insights": {
                            "reader_inference_space": {
                                "score": 6.5,
                                "l1_tell_count": 2,
                                "l2_show_count": 4,
                                "l3_subtext_count": 1,
                                "assessment": "有动作暗示，留白不足",
                            },
                            "communication_efficiency": {
                                "level": "3",
                                "level_name": "发展",
                                "assessment": "表达顺畅但略重复",
                            },
                        },
                        "chunk_assessment": "信息密度较高。",
                        "evidence": [{"type": "亮点", "dimension": "对话质量", "quote": "他说出了关键证词", "note": "推进线索"}],
                    },
                    "pacing_analysis": {
                        "pacing_type": "dense",
                        "tension_level": 7,
                        "emotion_tone": "悬",
                        "emotion_intensity": 6,
                        "payoff_moment": "获得关键证词",
                        "cliffhanger_quality": "medium",
                        "reader_engagement_prediction": "high",
                    },
                    "information_density": {
                        "density_score": "high",
                        "skipability": "essential",
                        "key_information": ["嫌疑人证词改变"],
                        "redundancy_flags": [],
                        "narrative_efficiency": "每段都有线索推进",
                    },
                    "one_sentence_summary": "主角审问并获得证词。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk(
                "主角审问嫌疑人并发现证词漏洞，不禁冷冷的追问，围观者倒吸一口凉气。",
                0,
                1,
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("写作质量、节奏和信息密度评估" in prompt for prompt in prompts))
        self.assertEqual(result["writing_quality"]["prose_quality"]["score"], 6.8)
        self.assertEqual(result["writing_quality"]["info_density"]["water_chapter_score"], 2.0)
        zhihu = result["writing_quality"]["zhihu_insights"]
        self.assertGreaterEqual(zhihu["word_poverty"]["template_phrase_count"], 3)
        self.assertIn("倒吸一口凉气", zhihu["word_poverty"]["most_frequent_templates"][0])
        self.assertEqual(zhihu["reader_inference_space"]["score"], 6.5)
        self.assertEqual(result["pacing_analysis"]["pacing_type"], "dense")
        self.assertEqual(result["information_density"]["skipability"], "essential")
        self.assertIn("嫌疑人证词改变", result["information_density"]["key_information"])

    def test_general_scan_outputs_narrative_structure_and_architecture(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角突破后进入新地图"],
                    "conflicts": ["旧宗门与新势力冲突"],
                    "worldbuilding": ["上层修真界开启"],
                    "themes": ["成长"],
                    "foreshadowing": [],
                    "quality_notes": ["换地图承接自然"],
                    "specialty_notes": [],
                    "narrative_structure": {
                        "structural_function": "阶段收束并转入新地图",
                        "structural_function_tag": "transition",
                        "structure_pattern": "升级流-换地图段",
                        "beat_phase": "合",
                        "turning_point": "主角突破金丹后离开旧宗门",
                        "arc_position": "收尾",
                        "estimated_cycle_position": "升级-换地图循环：换地图段",
                    },
                    "outline_architecture": {
                        "causal_chain": {
                            "causal_strength": "自然发展",
                            "causal_description": "突破带来更高层级冲突",
                            "forced_elements": [],
                            "coincidence_dependency": "none",
                        },
                        "protagonist_growth": {
                            "growth_type": "power",
                            "growth_significance": "major",
                            "growth_description": "境界突破",
                            "growth_smoothness": "reasonable",
                        },
                        "worldbuilding_expansion": {
                            "new_elements": ["上层修真界"],
                            "expansion_pacing": "timely",
                            "consistency_check": "consistent",
                        },
                        "architecture_integrity": {
                            "integrity_score": 7.6,
                            "forced_plot_devices": [],
                            "power_inconsistency": "暂无明显战力矛盾",
                            "threat_level": "新威胁层级合理",
                        },
                    },
                    "one_sentence_summary": "主角突破后进入新地图。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("主角突破金丹，离开宗门前往上层修真界。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("叙事结构与大纲架构评估" in prompt for prompt in prompts))
        self.assertEqual(result["narrative_structure"]["structural_function_tag"], "transition")
        self.assertEqual(result["narrative_structure"]["structure_pattern"], "升级流-换地图段")
        self.assertEqual(result["outline_architecture"]["causal_chain"]["causal_strength"], "自然发展")
        self.assertEqual(result["outline_architecture"]["architecture_integrity"]["integrity_score"], 7.6)

    def test_general_scan_outputs_foreshadowing_engineering(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角发现密信"],
                    "conflicts": ["真相与隐瞒冲突"],
                    "worldbuilding": [],
                    "themes": ["真相"],
                    "foreshadowing": ["密信来源未明"],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [
                            {
                                "type": "item",
                                "description": "密信背面的旧印记",
                                "estimated_importance": "high",
                                "evidence": "背面旧印记",
                            }
                        ],
                        "foreshadowing_resolutions": [
                            {
                                "resolved_item": "旧钥匙用途",
                                "resolution_description": "钥匙打开地下档案室",
                                "satisfaction": "satisfying",
                            }
                        ],
                        "false_foreshadowing": ["嫌疑人的假口供"],
                        "engineering_notes": ["设置与回收间距清楚"],
                        "recycling_rate": "1/2",
                    },
                    "one_sentence_summary": "主角发现密信并回收钥匙线索。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("主角发现密信，旧钥匙打开地下档案室。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("伏笔工程追踪" in prompt for prompt in prompts))
        self.assertEqual(result["foreshadowing_engineering"]["new_foreshadowing"][0]["description"], "密信背面的旧印记")
        self.assertEqual(result["foreshadowing_engineering"]["foreshadowing_resolutions"][0]["resolved_item"], "旧钥匙用途")
        self.assertIn("嫌疑人的假口供", result["foreshadowing_engineering"]["false_foreshadowing"])
        self.assertEqual(result["foreshadowing_engineering"]["recycling_rate"], "1/2")

    def test_general_scan_outputs_semantic_layers(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角表面退让，实际设局"],
                    "conflicts": ["主角与反派的信息差"],
                    "worldbuilding": [],
                    "themes": ["隐忍反击"],
                    "foreshadowing": [],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "semantic_layers": {
                        "literal_meaning": "主角答应反派条件",
                        "author_intent": "先压低主角处境，为后续反击蓄势",
                        "surface_emotion": "压抑",
                        "reader_effect": "读者期待主角翻盘",
                        "deep_semantic": "主角的退让并非认输，而是在隐藏底牌",
                        "technique": "先抑后扬与对白潜台词",
                        "subtext_or_irony": ["反派自以为胜利形成反讽"],
                        "confidence": "high",
                    },
                    "one_sentence_summary": "主角表面退让并暗中设局。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("主角说可以退一步，反派以为胜券在握。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("中文深层语义" in prompt for prompt in prompts))
        self.assertTrue(any("四层分析" in prompt for prompt in prompts))
        self.assertEqual(result["semantic_layers"]["literal_meaning"], "主角答应反派条件")
        self.assertEqual(result["semantic_layers"]["author_intent"], "先压低主角处境，为后续反击蓄势")
        self.assertIn("反派自以为胜利形成反讽", result["semantic_layers"]["subtext_or_irony"])
        self.assertEqual(result["semantic_layers"]["confidence"], "high")

    def test_general_scan_outputs_reader_experience(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角被压制后反手破局"],
                    "conflicts": ["主角与反派对抗"],
                    "worldbuilding": [],
                    "themes": ["反击"],
                    "foreshadowing": [],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "reader_experience": {
                        "immediate_emotion": {"emotion": "爽", "intensity": 8, "trigger": "主角反手破局"},
                        "immersion_anchor": "主角从劣势翻盘",
                        "anticipation": {"expected": "反派被彻底清算", "intensity": 7, "hook_type": "反击"},
                        "satisfaction_points": [
                            {"type": "爽点", "description": "压制后反杀", "intensity": 8, "evidence": "反手破局"}
                        ],
                        "frustration_points": [
                            {"type": "憋屈", "description": "前半段压制较长", "intensity": 4, "evidence": "被连续羞辱"}
                        ],
                        "engagement_level": "high",
                        "experience_notes": ["期待后续清算"],
                    },
                    "one_sentence_summary": "主角从被压制转为反手破局。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk("主角被连续羞辱后反手破局。", 0, 1, profile=profile)
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("读者体验" in prompt for prompt in prompts))
        self.assertTrue(any("期待管理" in prompt for prompt in prompts))
        self.assertEqual(result["reader_experience"]["immediate_emotion"]["emotion"], "爽")
        self.assertEqual(result["reader_experience"]["immediate_emotion"]["intensity"], 8.0)
        self.assertEqual(result["reader_experience"]["anticipation"]["expected"], "反派被彻底清算")
        self.assertEqual(result["reader_experience"]["satisfaction_points"][0]["description"], "压制后反杀")
        self.assertEqual(result["reader_experience"]["frustration_points"][0]["type"], "憋屈")
        self.assertEqual(result["reader_experience"]["engagement_level"], "high")

    def test_general_scan_uses_rolling_context_in_chunk_prompt(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "plot_events": ["主角找到线索"],
                    "conflicts": [],
                    "worldbuilding": [],
                    "themes": [],
                    "foreshadowing": ["旧案真相仍未揭开"],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [{"description": "旧案现场留下的黑色纽扣"}],
                    },
                    "context_state_update": {
                        "progress_summary": "调查推进到旧案线索。",
                        "active_characters": ["林澈", "许青"],
                        "open_threads": ["旧案真相"],
                        "current_stage": "调查旧案",
                    },
                    "one_sentence_summary": "主角找到旧案线索。",
                }

            general_scan._call_json = fake_call_json
            result = general_scan._scan_chunk(
                "主角继续调查旧案。",
                1,
                3,
                profile=profile,
                context_snapshot={
                    "previous_progress": "主角进入城中调查。",
                    "active_characters": ["林澈"],
                    "open_threads": ["失踪案"],
                },
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any("跨块上下文" in prompt for prompt in prompts))
        self.assertTrue(any("失踪案" in prompt for prompt in prompts))
        self.assertEqual(result["context_state_update"]["current_stage"], "调查旧案")
        self.assertIn("林澈", result["context_snapshot_used"]["active_characters"])

        state = general_scan._update_rolling_context_state(general_scan._empty_rolling_context_state(), result)
        self.assertIn("旧案现场留下的黑色纽扣", state["active_foreshadowing"])
        snapshot = general_scan._rolling_context_snapshot(state)
        self.assertIn("旧案现场留下的黑色纽扣", snapshot["active_foreshadowing"])

    def test_general_scan_trims_rolling_context_snapshot_to_budget(self):
        snapshot = {
            "previous_progress": "前情" * 200,
            "current_stage": "长阶段" * 80,
            "active_characters": [f"人物{i}" for i in range(40)],
            "relationship_updates": [f"关系{i}" for i in range(40)],
            "open_threads": [f"悬念{i}" for i in range(40)],
            "worldbuilding_updates": [f"设定{i}" for i in range(40)],
            "sampled_context": True,
            "source_chunk_count": 500,
        }

        trimmed = general_scan._trim_context_snapshot(snapshot, max_chars=180)

        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False)), 180)
        self.assertNotIn("前情" * 20, json.dumps(trimmed, ensure_ascii=False))

    def test_general_scan_merge_partial_results_keeps_context_update(self):
        result = general_scan._merge_partial_scan_results(
            [
                {
                    "plot_events": ["前半事件"],
                    "context_snapshot_used": {"previous_progress": "旧进展"},
                    "context_state_update": {
                        "progress_summary": "前半推进",
                        "active_characters": ["林澈"],
                        "open_threads": ["旧案"],
                    },
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [{"description": "密信背面的旧印记"}],
                    },
                    "one_sentence_summary": "前半摘要",
                },
                {
                    "plot_events": ["后半事件"],
                    "context_state_update": {
                        "progress_summary": "后半推进",
                        "resolved_threads": ["旧案"],
                        "current_stage": "案件收束",
                    },
                    "foreshadowing_engineering": {
                        "foreshadowing_resolutions": [
                            {"resolved_item": "旧钥匙用途", "resolution_description": "打开档案室"}
                        ],
                        "false_foreshadowing": ["嫌疑人假口供"],
                    },
                    "one_sentence_summary": "后半摘要",
                },
            ],
            0,
            "context_overflow_split",
        )

        self.assertEqual(result["plot_events"], ["前半事件", "后半事件"])
        self.assertEqual(result["context_snapshot_used"]["previous_progress"], "旧进展")
        self.assertEqual(result["context_state_update"]["current_stage"], "案件收束")
        self.assertIn("旧案", result["context_state_update"]["resolved_threads"])
        self.assertEqual(result["foreshadowing_engineering"]["new_foreshadowing"][0]["description"], "密信背面的旧印记")
        self.assertEqual(result["foreshadowing_engineering"]["foreshadowing_resolutions"][0]["resolved_item"], "旧钥匙用途")
        self.assertIn("嫌疑人假口供", result["foreshadowing_engineering"]["false_foreshadowing"])

    def test_general_scan_builds_knowledge_base_from_chunk_results(self):
        knowledge_base = general_scan._build_knowledge_base([
            {
                "chunk_index": 0,
                "original_chunk_index": 1,
                "one_sentence_summary": "林澈接手旧案并发现密信。",
                "plot_events": ["林澈接手旧案", "林澈接手旧案"],
                "worldbuilding": ["旧城由巡夜司管辖", "旧城由巡夜司管辖"],
                "context_state_update": {
                    "active_characters": ["林澈(侦探)", "沈青(证人)"],
                    "relationship_updates": ["林澈与沈青从互相试探转为合作"],
                    "open_threads": ["密信来源仍未揭开", "旧钥匙用途"],
                    "worldbuilding_updates": ["巡夜司负责夜间案件"],
                },
                "foreshadowing_engineering": {
                    "new_foreshadowing": [
                        {"description": "密信背面的旧印记", "estimated_importance": "high", "evidence": "印记反复出现"},
                        {"description": "密信背面的旧印记", "estimated_importance": "high", "evidence": "重复印记"},
                    ],
                },
            },
            {
                "chunk_index": 1,
                "original_chunk_index": 2,
                "one_sentence_summary": "林澈使用旧钥匙打开档案室。",
                "plot_events": ["林澈接手旧案", "旧钥匙打开档案室"],
                "worldbuilding": "档案室封存二十年前卷宗",
                "context_state_update": {
                    "active_characters": ["林澈(主角)"],
                    "relationship_updates": ["林澈与沈青从互相试探转为合作"],
                    "resolved_threads": ["旧钥匙用途"],
                },
                "foreshadowing_engineering": {
                    "foreshadowing_resolutions": [
                        {"resolved_item": "旧钥匙用途", "resolution_description": "打开档案室", "satisfaction": "natural"}
                    ],
                },
            },
        ])

        self.assertEqual(knowledge_base["schema_version"], general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION)
        self.assertEqual([x["name"] for x in knowledge_base["entities"]], ["林澈", "沈青"])
        self.assertEqual(len(knowledge_base["relationships"]), 1)
        self.assertEqual(knowledge_base["relationships"][0]["description"], "林澈与沈青从互相试探转为合作")
        facts = [x["fact"] for x in knowledge_base["worldbuilding_facts"]]
        self.assertIn("旧城由巡夜司管辖", facts)
        self.assertIn("档案室封存二十年前卷宗", facts)
        self.assertEqual(facts.count("旧城由巡夜司管辖"), 1)
        self.assertNotIn("档", facts)
        self.assertEqual([x["thread"] for x in knowledge_base["open_threads"]], ["密信来源仍未揭开"])
        self.assertEqual([x["thread"] for x in knowledge_base["resolved_threads"]], ["旧钥匙用途"])
        events = [x["event"] for x in knowledge_base["plot_timeline"]]
        self.assertEqual(events.count("林澈接手旧案"), 1)
        self.assertEqual(len(knowledge_base["plot_timeline"]), 2)
        active_foreshadowing = [
            x["description"] for x in knowledge_base["foreshadowing_threads"]
            if x.get("status") == "active"
        ]
        self.assertEqual(active_foreshadowing.count("密信背面的旧印记"), 1)
        self.assertIn("旧钥匙用途", [x["description"] for x in knowledge_base["foreshadowing_threads"]])

    def test_general_scan_knowledge_base_v2_includes_fact_and_risk_layers(self):
        result = general_scan._build_knowledge_base([
            fact_validator.validate_general_chunk_result({
                "chunk_index": 0,
                "original_chunk_index": 1,
                "one_sentence_summary": "沈南歌登场并暴露婚约风险。",
                "plot_events": ["沈南歌登场"],
                "quality_notes": ["婚约线存在绿帽风险"],
                "context_state_update": {
                    "active_characters": ["沈南歌", "那女子"],
                    "relationship_updates": ["沈南歌与主角存在婚约"],
                },
            })
        ])

        self.assertEqual(result["schema_version"], general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION)
        self.assertGreater(len(result["facts"]), 0)
        self.assertGreater(len(result["risk_facts"]), 0)
        self.assertEqual(result["entities"][0]["name"], "沈南歌")
        self.assertNotIn("那女子", [item["name"] for item in result["entities"]])
        counts = general_scan._knowledge_base_counts(result)
        self.assertGreater(counts["facts"], 0)
        self.assertGreater(counts["risk_facts"], 0)

    def test_general_scan_compact_knowledge_base_samples_head_middle_tail(self):
        knowledge_base = {
            "plot_timeline": [
                {"chunk_index": i + 1, "event": f"事件{i + 1}"}
                for i in range(300)
            ],
            "entities": [],
            "relationships": [],
            "worldbuilding_facts": [],
            "foreshadowing_threads": [],
            "open_threads": [],
            "resolved_threads": [],
        }

        compact = general_scan._compact_knowledge_base_for_summary(knowledge_base, limit=40)
        indices = [item["chunk_index"] for item in compact["plot_timeline"]]

        self.assertEqual(len(indices), 40)
        self.assertEqual(indices[0], 1)
        self.assertEqual(indices[-1], 300)
        self.assertEqual(indices, sorted(indices))
        self.assertTrue(any(130 <= idx <= 170 for idx in indices))

    def test_general_scan_compact_writing_quality_keeps_late_high_signal(self):
        chunk_results = []
        for i in range(300):
            chunk_results.append({
                "chunk_index": i,
                "original_chunk_index": i + 1,
                "one_sentence_summary": f"第{i + 1}段",
                "writing_quality": {"style": "平稳"},
                "pacing_analysis": {"pacing": "正常"},
                "information_density": {"density": "中"},
            })
        chunk_results[-1]["one_sentence_summary"] = "后期严重注水，节奏明显崩坏"
        chunk_results[-1]["writing_quality"] = {"quality_issues": ["后期严重注水"]}

        compact = general_scan._compact_writing_quality_for_summary(chunk_results, limit=80)
        indices = [item["chunk_index"] for item in compact]
        summaries = [item["summary"] for item in compact]

        self.assertEqual(len(compact), 80)
        self.assertEqual(indices[0], 1)
        self.assertIn(300, indices)
        self.assertTrue(any(130 <= idx <= 170 for idx in indices))
        self.assertIn("后期严重注水，节奏明显崩坏", summaries)

    def test_general_scan_compact_specialty_material_keeps_late_high_signal(self):
        chunk_results = []
        for i in range(300):
            chunk_results.append({
                "chunk_index": i,
                "original_chunk_index": i + 1,
                "one_sentence_summary": f"第{i + 1}段",
                "narrative_structure": {"structural_function_tag": "推进"},
                "outline_architecture": {
                    "architecture_integrity": {"integrity_score": 7},
                    "causal_chain": {"causal_strength": "自然"},
                },
                "foreshadowing_engineering": {
                    "new_foreshadowing": [{"description": "常规伏笔"}],
                },
                "semantic_layers": {
                    "literal_meaning": "表层事件",
                    "reader_effect": "平稳",
                    "confidence": 0.7,
                },
                "reader_experience": {
                    "immediate_emotion": {"emotion": "平稳"},
                    "engagement_level": "中",
                },
                "context_state_update": {
                    "open_threads": ["常规悬念"],
                },
            })
        chunk_results[-1]["one_sentence_summary"] = "尾段问题集中爆发"
        chunk_results[-1]["outline_architecture"] = {
            "architecture_integrity": {
                "integrity_score": 2,
                "forced_plot_devices": ["后期强行转折"],
                "power_inconsistency": "战力矛盾",
            },
            "causal_chain": {
                "causal_strength": "低",
                "forced_elements": ["关键巧合强行推动"],
            },
        }
        chunk_results[-1]["foreshadowing_engineering"] = {
            "false_foreshadowing": ["尾段伏笔未回收"],
        }
        chunk_results[-1]["reader_experience"] = {
            "immediate_emotion": {"emotion": "疲惫"},
            "frustration_points": ["阅读疲劳严重"],
            "engagement_level": "低",
        }

        narrative = general_scan._compact_narrative_architecture_for_summary(chunk_results, limit=80)
        foreshadowing = general_scan._compact_foreshadowing_engineering_for_summary(chunk_results, limit=80)
        reader = general_scan._compact_reader_experience_for_summary(chunk_results, limit=80)
        continuity = general_scan._compact_continuity_for_summary(chunk_results, limit=80)

        self.assertIn(300, [item["chunk_index"] for item in narrative])
        self.assertTrue(any("后期强行转折" in item["forced_plot_devices"] for item in narrative))
        self.assertIn(300, [item["chunk_index"] for item in foreshadowing])
        self.assertTrue(any("尾段伏笔未回收" in item["false_foreshadowing"] for item in foreshadowing))
        self.assertIn(300, [item["chunk_index"] for item in reader])
        self.assertTrue(any(
            point.get("description") == "阅读疲劳严重"
            for item in reader
            for point in item["frustration_points"]
        ))
        self.assertIn(300, [item["chunk_index"] for item in continuity])
        self.assertTrue(any("战力矛盾" in (item.get("power_inconsistency") or "") for item in continuity))

    def test_general_scan_llm_merge_normalizes_knowledge_base(self):
        fallback = {
            "schema_version": general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION,
            "entities": [{"name": "林澈", "first_seen_chunk": 1}],
            "relationships": [],
            "worldbuilding_facts": [],
            "foreshadowing_threads": [],
            "plot_timeline": [],
            "open_threads": [{"thread": "旧钥匙用途", "chunk_index": 1}],
            "resolved_threads": [],
        }
        merged = general_scan._normalize_llm_knowledge_base(
            {
                "entities": [{"name": "林澈", "role": "侦探"}, {"name": "林澈", "role": "主角"}],
                "relationships": ["林澈与沈青合作"],
                "worldbuilding_facts": ["旧城由巡夜司管辖"],
                "foreshadowing_threads": [{"description": "密信背面的旧印记", "status": "active"}],
                "plot_timeline": ["林澈接手旧案"],
                "open_threads": ["旧钥匙用途", "密信来源仍未揭开"],
                "resolved_threads": ["旧钥匙用途"],
            },
            fallback,
        )

        self.assertEqual([x["name"] for x in merged["entities"]], ["林澈"])
        self.assertEqual(merged["relationships"][0]["description"], "林澈与沈青合作")
        self.assertEqual(merged["worldbuilding_facts"][0]["fact"], "旧城由巡夜司管辖")
        self.assertEqual(merged["foreshadowing_threads"][0]["description"], "密信背面的旧印记")
        self.assertEqual(merged["plot_timeline"][0]["event"], "林澈接手旧案")
        self.assertEqual([x["thread"] for x in merged["open_threads"]], ["密信来源仍未揭开"])
        self.assertEqual([x["thread"] for x in merged["resolved_threads"]], ["旧钥匙用途"])

    def test_general_scan_density_profile_detects_high_signal_chunks(self):
        profile = general_scan._chunk_density_profile("案件出现尸体，凶手线索揭露，随后发生战斗和反转。")
        self.assertEqual(profile["level"], "high")
        self.assertEqual(profile["strategy"], "full")

    def test_general_scan_call_json_retries_without_json_mode_on_parse_failure(self):
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChoice:
            def __init__(self, content):
                self.message = FakeMessage(content)

        class FakeResponse:
            def __init__(self, content):
                self.choices = [FakeChoice(content)]

        calls = []
        old_chat = general_scan.chat_completion
        try:
            def fake_chat_completion(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return FakeResponse("这不是 JSON")
                return FakeResponse('{"plot_events":["重试成功"]}')

            general_scan.chat_completion = fake_chat_completion
            data = general_scan._call_json([{"role": "user", "content": "输出 JSON"}], max_tokens=128)
        finally:
            general_scan.chat_completion = old_chat

        self.assertEqual(data["plot_events"], ["重试成功"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])
        self.assertIn("上一次回复不是可解析的 JSON 对象", calls[1]["messages"][-1]["content"])

    def test_general_scan_summary_prompt_uses_field_labels(self):
        profile = analysis_profiles.load_analysis_profile("urban_power")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "都市系统爽文。",
                    "main_plot": ["主角获得系统"],
                    "core_conflicts": ["豪门压迫"],
                    "worldbuilding": ["现代都市"],
                    "themes": ["逆袭"],
                    "foreshadowing_and_payoff": ["身份伏笔回收"],
                    "golden_finger_system": ["系统奖励稳定"],
                    "relationships": ["暧昧线服务主线"],
                    "villain_quality": ["反派层级清晰"],
                    "strengths": ["爽点明确"],
                    "risks_or_issues": ["打脸重复"],
                    "reader_fit": "适合都市爽文读者",
                    "overall_assessment": "完成度尚可",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "都市测试",
                [{
                    "one_sentence_summary": "主角获得系统。",
                    "specialty_notes": ["系统任务稳定"],
                    "context_state_update": {
                        "active_characters": ["林澈(主角)"],
                        "relationship_updates": ["林澈与系统形成任务绑定"],
                        "open_threads": ["系统来源仍未揭开"],
                        "worldbuilding_updates": ["现代都市存在隐藏任务体系"],
                    },
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [{"description": "系统初始提示藏有旧编号", "estimated_importance": "medium"}],
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertIn("系统奖励稳定", summary["golden_finger_system"])
        self.assertTrue(any('"knowledge_base"' in prompt for prompt in prompts))
        self.assertTrue(any("林澈" in prompt for prompt in prompts))
        self.assertTrue(any("系统来源仍未揭开" in prompt for prompt in prompts))
        self.assertTrue(any("系统初始提示藏有旧编号" in prompt for prompt in prompts))
        self.assertTrue(any('"golden_finger_system": ["异能/金手指体系专项分析要点"]' in prompt for prompt in prompts))
        self.assertTrue(any('"relationships": ["关系线专项分析要点"]' in prompt for prompt in prompts))

    def test_general_scan_summary_normalizes_radar_scores(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompts.append("\n".join(item.get("content", "") for item in messages))
                return {
                    "story_overview": "主角完成主线。",
                    "main_plot": ["完成主线"],
                    "core_conflicts": ["目标明确"],
                    "worldbuilding": ["设定清楚"],
                    "themes": ["成长"],
                    "foreshadowing_and_payoff": ["伏笔回收"],
                    "strengths": ["结构完整"],
                    "risks_or_issues": ["节奏略慢"],
                    "reader_fit": "通用读者",
                    "overall_assessment": "可读",
                    "radar_scores": {
                        "plot": {"score": 8.25, "reason": "主线完整"},
                        "characters": {"score": "6", "reason": "角色够用"},
                        "worldbuilding": {"score": 12, "reason": "超出会被截断"},
                        "pacing": {"score": -1, "reason": "低于会被截断"},
                        "writing": 7,
                        "emotion": {"score": 5.5},
                    },
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "通用测试",
                [{"one_sentence_summary": "主角完成主线。"}],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"radar_scores"' in prompt for prompt in prompts))
        self.assertEqual(summary["radar_scores"]["plot"]["label"], "剧情质量")
        self.assertEqual(summary["radar_scores"]["plot"]["score"], 8.2)
        self.assertEqual(summary["radar_scores"]["worldbuilding"]["score"], 10.0)
        self.assertEqual(summary["radar_scores"]["pacing"]["score"], 0.0)
        self.assertEqual(summary["radar_scores"]["writing"]["score"], 7.0)

    def test_general_scan_summary_includes_writing_quality_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角推进案件。",
                    "main_plot": ["案件推进"],
                    "core_conflicts": ["真相与阻挠"],
                    "worldbuilding": ["城市警务"],
                    "themes": ["真相"],
                    "foreshadowing_and_payoff": [],
                    "writing_quality_overall": {
                        "overall_score": 7.2,
                        "grade": "B",
                        "dimension_scores": {
                            "prose_quality": 6.5,
                            "character_depth": 7,
                            "narrative_technique": 7.5,
                            "dialogue_quality": 7,
                            "scene_description": 6,
                            "emotional_impact": 6.5,
                            "info_density": 8,
                            "worldbuilding_integration": 7,
                        },
                        "strengths": ["线索推进密集"],
                        "weaknesses": ["人物声口区分一般"],
                        "evidence": ["审问段落持续推进证词"],
                        "assessment": "整体写作质量良好。",
                    },
                    "pacing_analysis_overall": {
                        "rhythm_curve": "调查推进较紧",
                        "high_points": ["获得证词"],
                        "slow_or_water_segments": ["说明段略长"],
                        "emotion_pattern": "悬疑为主",
                        "risks": ["中段可能拖慢"],
                    },
                    "information_density_audit": {
                        "density_verdict": "信息密度较高",
                        "water_ratio_estimate": "约10%",
                        "high_density_material": ["审问线索"],
                        "redundancy_patterns": ["设定解释重复"],
                        "skip_advice": "审问段不建议跳读",
                    },
                    "water_chapter_analysis": ["说明性段落略多"],
                    "zhihu_writing_insights_overall": {
                        "word_poverty": {
                            "severity": "轻度词穷",
                            "template_phrase_count": 6,
                            "template_phrase_density_per_1k": 2.2,
                            "most_frequent_templates": ["不禁(3次)"],
                            "category_patterns": ["情绪副词"],
                            "assessment": "存在可替换模板词。",
                        },
                        "reader_inference_space": {"score": 6.5, "assessment": "留白偏少"},
                        "communication_efficiency": {"level": "3", "level_name": "发展", "assessment": "表达顺畅"},
                        "style_identity": {"detected_traits": ["冷静"], "originality_score": 6, "consistency_score": 7},
                        "emotional_authenticity": {"score": 6, "transcendence_potential": "中", "assessment": "情感自然"},
                        "priority_improvements": ["减少模板情绪词"],
                    },
                    "strengths": ["结构清晰"],
                    "risks_or_issues": ["中段略慢"],
                    "reader_fit": "悬疑读者",
                    "overall_assessment": "可读",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "写作质量测试",
                [{
                    "one_sentence_summary": "主角审问嫌疑人。",
                    "writing_quality": {
                        "prose_quality": {"score": 6.5},
                        "info_density": {"score": 8},
                        "zhihu_insights": {
                            "word_poverty": {
                                "template_phrase_count": 6,
                                "template_phrase_density_per_1k": 2.2,
                                "most_frequent_templates": ["不禁(3次)"],
                                "category_hits": {"情绪副词": [{"phrase": "不禁", "count": 3}]},
                                "severity": "轻度词穷",
                            },
                            "reader_inference_space": {"score": 6.5, "assessment": "留白偏少"},
                        },
                    },
                    "pacing_analysis": {"pacing_type": "dense"},
                    "information_density": {"density_score": "high"},
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"writing_quality_chunks"' in prompt for prompt in prompts))
        self.assertTrue(any('"zhihu_writing_insights_material"' in prompt for prompt in prompts))
        self.assertTrue(any('"zhihu_writing_insights_overall"' in prompt for prompt in prompts))
        self.assertTrue(any('"writing_quality_overall"' in prompt for prompt in prompts))
        self.assertEqual(summary["writing_quality_overall"]["overall_score"], "7.2")
        self.assertEqual(summary["zhihu_writing_insights_overall"]["word_poverty"]["severity"], "轻度词穷")
        self.assertIn("减少模板情绪词", summary["zhihu_writing_insights_overall"]["priority_improvements"])
        self.assertEqual(summary["writing_quality_overall"]["dimension_scores"]["info_density"], "8")
        self.assertIn("调查推进较紧", summary["pacing_analysis_overall"]["rhythm_curve"])
        self.assertIn("信息密度较高", summary["information_density_audit"]["density_verdict"])
        self.assertIn("说明性段落略多", summary["water_chapter_analysis"])

    def test_general_scan_summary_includes_narrative_architecture_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角完成阶段成长并进入新地图。",
                    "main_plot": ["旧地图收束", "新地图开启"],
                    "core_conflicts": ["主角与上层势力冲突"],
                    "worldbuilding": ["上层修真界"],
                    "themes": ["成长"],
                    "foreshadowing_and_payoff": [],
                    "narrative_structure_analysis": {
                        "primary_structure_pattern": "升级-换地图循环",
                        "structure_pattern_description": "以突破和地图切换推动阶段递进。",
                        "rhythm_curve_description": "前段铺垫，中段突破，末段转场。",
                        "major_turning_points": ["突破金丹后离开旧宗门"],
                        "arc_structure": "旧地图篇章完成起承转合",
                        "sub_arc_analysis": ["宗门篇收束自然"],
                        "structure_execution_quality": "良好",
                        "structure_risks": ["后续可能出现换地图疲劳"],
                    },
                    "outline_architecture_overall": {
                        "structural_completeness": "阶段目标清楚。",
                        "causal_chain_strength": "strong",
                        "growth_curve": {"smoothness": "natural", "curve_description": "成长递进自然"},
                        "worldbuilding_pacing": {"expansion_quality": "good", "expansion_description": "新设定引入及时"},
                        "system_stability": "战力体系稳定",
                        "architecture_damage": [],
                        "overall_architecture_rating": "good",
                        "architecture_score": 7.5,
                        "improvement_suggestions": ["减少同类突破重复"],
                    },
                    "strengths": ["结构清楚"],
                    "risks_or_issues": ["换地图疲劳"],
                    "reader_fit": "升级流读者",
                    "overall_assessment": "结构稳定",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "叙事架构测试",
                [{
                    "one_sentence_summary": "主角突破并换地图。",
                    "narrative_structure": {
                        "structural_function_tag": "transition",
                        "structure_pattern": "升级流-换地图段",
                        "turning_point": "突破金丹",
                    },
                    "outline_architecture": {
                        "causal_chain": {"causal_strength": "自然发展"},
                        "protagonist_growth": {"growth_smoothness": "reasonable", "growth_significance": "major"},
                        "worldbuilding_expansion": {"expansion_pacing": "timely", "consistency_check": "consistent"},
                        "architecture_integrity": {"integrity_score": 7.5},
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"narrative_architecture_chunks"' in prompt for prompt in prompts))
        self.assertTrue(any('"narrative_structure_analysis"' in prompt for prompt in prompts))
        self.assertEqual(summary["narrative_structure_analysis"]["primary_structure_pattern"], "升级-换地图循环")
        self.assertEqual(summary["outline_architecture_overall"]["architecture_score"], "7.5")
        self.assertEqual(summary["outline_architecture_overall"]["growth_curve"]["smoothness"], "natural")

    def test_general_scan_summary_includes_foreshadowing_engineering_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角调查旧案并回收部分伏笔。",
                    "main_plot": ["旧案调查"],
                    "core_conflicts": ["真相与阻挠"],
                    "worldbuilding": [],
                    "themes": ["真相"],
                    "foreshadowing_and_payoff": ["密信线索仍在推进"],
                    "foreshadowing_engineering_analysis": {
                        "setup_quality": "good",
                        "active_threads": ["密信来源仍未揭开"],
                        "resolved_threads": ["旧钥匙用途回收自然"],
                        "false_or_red_herring": ["嫌疑人假口供"],
                        "payoff_satisfaction": "satisfying",
                        "recycling_rate_estimate": "约1/2",
                        "risks": ["密信线需后续回收"],
                    },
                    "strengths": ["伏笔间距清楚"],
                    "risks_or_issues": ["仍有未回收线索"],
                    "reader_fit": "悬疑读者",
                    "overall_assessment": "伏笔工程较稳定",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "伏笔工程测试",
                [{
                    "one_sentence_summary": "主角发现密信并使用旧钥匙。",
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [{"description": "密信背面的旧印记", "estimated_importance": "high"}],
                        "foreshadowing_resolutions": [{"resolved_item": "旧钥匙用途", "resolution_description": "打开档案室"}],
                        "false_foreshadowing": ["嫌疑人假口供"],
                        "recycling_rate": "1/2",
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"foreshadowing_engineering_chunks"' in prompt for prompt in prompts))
        self.assertTrue(any('"foreshadowing_engineering_analysis"' in prompt for prompt in prompts))
        self.assertTrue(any("密信背面的旧印记" in prompt for prompt in prompts))
        self.assertEqual(summary["foreshadowing_engineering_analysis"]["setup_quality"], "good")
        self.assertIn("密信来源仍未揭开", summary["foreshadowing_engineering_analysis"]["active_threads"])
        self.assertEqual(summary["foreshadowing_engineering_analysis"]["recycling_rate_estimate"], "约1/2")

    def test_general_scan_summary_includes_semantic_layers_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角通过隐忍完成反击。",
                    "main_plot": ["隐忍反击"],
                    "core_conflicts": ["主角与反派的信息差"],
                    "worldbuilding": [],
                    "themes": ["隐忍"],
                    "foreshadowing_and_payoff": [],
                    "semantic_layers_analysis": {
                        "dominant_author_intent": "先压后扬制造翻盘爽点",
                        "reader_effect_pattern": "压抑后释放",
                        "deep_semantic_pattern": "退让话语带有隐藏底牌的潜台词",
                        "technique_pattern": ["先抑后扬", "视角限制"],
                        "subtext_or_irony": ["反派自信带有反讽"],
                        "semantic_strengths": ["潜台词服务反击"],
                        "semantic_risks": ["压抑段过长会削弱阅读耐心"],
                    },
                    "strengths": ["语义层递进清楚"],
                    "risks_or_issues": [],
                    "reader_fit": "喜欢反击爽点的读者",
                    "overall_assessment": "语义层服务爽点",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "语义测试",
                [{
                    "one_sentence_summary": "主角表面退让，实则设局。",
                    "semantic_layers": {
                        "literal_meaning": "主角答应反派条件",
                        "author_intent": "制造压抑后的反击期待",
                        "surface_emotion": "压抑",
                        "reader_effect": "期待翻盘",
                        "deep_semantic": "退让不是认输",
                        "technique": "先抑后扬",
                        "subtext_or_irony": ["反派自信构成反讽"],
                        "confidence": "high",
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"semantic_layers_chunks"' in prompt for prompt in prompts))
        self.assertTrue(any('"semantic_layers_analysis"' in prompt for prompt in prompts))
        self.assertTrue(any("退让不是认输" in prompt for prompt in prompts))
        self.assertEqual(summary["semantic_layers_analysis"]["dominant_author_intent"], "先压后扬制造翻盘爽点")
        self.assertIn("先抑后扬", summary["semantic_layers_analysis"]["technique_pattern"])
        self.assertIn("反派自信带有反讽", summary["semantic_layers_analysis"]["subtext_or_irony"])

    def test_general_scan_summary_includes_reader_experience_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角通过压抑后的反击制造爽点。",
                    "main_plot": ["压抑反击"],
                    "core_conflicts": ["主角与反派对抗"],
                    "worldbuilding": [],
                    "themes": ["反击"],
                    "foreshadowing_and_payoff": [],
                    "reader_experience_analysis": {
                        "engagement_curve": "压抑后释放",
                        "dominant_emotions": ["压抑", "期待", "爽"],
                        "satisfaction_design": ["压制后反杀形成爽点"],
                        "anticipation_management": "期待反派清算并在高潮段兑现",
                        "immersion_anchors": ["主角翻盘"],
                        "frustration_risks": ["压抑铺垫过长"],
                        "reader_experience_rating": "good",
                        "improvement_suggestions": ["减少重复羞辱"],
                    },
                    "strengths": ["爽点兑现清楚"],
                    "risks_or_issues": ["压抑段稍长"],
                    "reader_fit": "喜欢反击爽点的读者",
                    "overall_assessment": "读者体验较稳定",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "读者体验测试",
                [{
                    "one_sentence_summary": "主角被压制后反手破局。",
                    "reader_experience": {
                        "immediate_emotion": {"emotion": "爽", "intensity": 8, "trigger": "反手破局"},
                        "immersion_anchor": "主角翻盘",
                        "anticipation": {"expected": "反派清算", "intensity": 7, "hook_type": "反击"},
                        "satisfaction_points": [{"type": "爽点", "description": "压制后反杀", "intensity": 8}],
                        "frustration_points": [{"type": "憋屈", "description": "前置压制偏长", "intensity": 4}],
                        "engagement_level": "high",
                        "experience_notes": ["爽点兑现明确"],
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"reader_experience_chunks"' in prompt for prompt in prompts))
        self.assertTrue(any('"reader_experience_analysis"' in prompt for prompt in prompts))
        self.assertTrue(any("压制后反杀" in prompt for prompt in prompts))
        self.assertEqual(summary["reader_experience_analysis"]["reader_experience_rating"], "good")
        self.assertIn("压制后反杀形成爽点", summary["reader_experience_analysis"]["satisfaction_design"])
        self.assertIn("压抑铺垫过长", summary["reader_experience_analysis"]["frustration_risks"])

    def test_general_scan_summary_includes_continuity_audit_material(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角推进旧案并暴露设定风险。",
                    "main_plot": ["旧案推进"],
                    "core_conflicts": ["真相与阻挠"],
                    "worldbuilding": ["旧城规则"],
                    "themes": ["真相"],
                    "foreshadowing_and_payoff": ["玉佩线仍未回收"],
                    "continuity_audit_analysis": {
                        "overall_continuity_rating": "average",
                        "risk_level": "medium",
                        "character_continuity": ["主角称呼和身份稳定"],
                        "relationship_consistency": ["协作关系递进自然"],
                        "worldbuilding_consistency": ["旧城禁令后续仍生效"],
                        "foreshadowing_continuity": ["玉佩来源仍未解释"],
                        "causal_chain_issues": ["关键情报出现略巧合"],
                        "unresolved_threads": ["玉佩来源"],
                        "evidence": ["第2块设置玉佩，第8块仍未解释"],
                        "fix_suggestions": ["后续补足玉佩来源"],
                    },
                    "strengths": ["主线清楚"],
                    "risks_or_issues": ["连续性需复核"],
                    "reader_fit": "悬疑读者",
                    "overall_assessment": "可读",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "连续性测试",
                [{
                    "one_sentence_summary": "主角发现玉佩疑点。",
                    "conflicts": ["主角追查旧案"],
                    "worldbuilding": ["旧城禁令"],
                    "quality_notes": ["关键情报出现略巧合"],
                    "context_state_update": {
                        "relationship_updates": ["主角与同伴开始协作"],
                        "open_threads": ["玉佩来源"],
                        "worldbuilding_updates": ["旧城禁令限制行动"],
                    },
                    "outline_architecture": {
                        "causal_chain": {
                            "causal_strength": "有些牵强",
                            "forced_elements": ["偶然获得密信"],
                            "coincidence_dependency": "minor",
                        },
                        "worldbuilding_expansion": {"consistency_check": "minor_issue"},
                        "architecture_integrity": {
                            "power_inconsistency": "主角临时能力解释不足",
                            "forced_plot_devices": ["路人突然送线索"],
                        },
                    },
                    "foreshadowing_engineering": {
                        "new_foreshadowing": [{"description": "玉佩来源", "estimated_importance": "high"}],
                        "foreshadowing_resolutions": [],
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertTrue(any('"continuity_audit_material"' in prompt for prompt in prompts))
        self.assertTrue(any('"continuity_audit_analysis"' in prompt for prompt in prompts))
        self.assertTrue(any("旧城禁令限制行动" in prompt for prompt in prompts))
        self.assertTrue(any("路人突然送线索" in prompt for prompt in prompts))
        self.assertEqual(summary["continuity_audit_analysis"]["risk_level"], "medium")
        self.assertIn("玉佩来源仍未解释", summary["continuity_audit_analysis"]["foreshadowing_continuity"])
        self.assertIn("关键情报出现略巧合", summary["continuity_audit_analysis"]["causal_chain_issues"])

    def test_general_scan_summary_uses_rolling_context_timeline_only(self):
        profile = analysis_profiles.load_analysis_profile("general")
        prompts = []
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(messages, max_tokens=3000):
                prompt = "\n".join(item.get("content", "") for item in messages)
                prompts.append(prompt)
                return {
                    "story_overview": "主角调查旧案并回收悬念。",
                    "main_plot": ["旧案调查"],
                    "core_conflicts": ["真相与阻挠"],
                    "worldbuilding": [],
                    "themes": ["真相"],
                    "foreshadowing_and_payoff": ["失踪案线索被回收"],
                    "strengths": ["阶段推进清楚"],
                    "risks_or_issues": [],
                    "reader_fit": "悬疑读者",
                    "overall_assessment": "可读",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "滚动上下文测试",
                [{
                    "one_sentence_summary": "主角找到旧案线索。",
                    "context_snapshot_used": {"previous_progress": "不应进入总评材料"},
                    "context_state_update": {
                        "progress_summary": "调查推进到旧案线索。",
                        "active_characters": ["林澈"],
                        "open_threads": ["失踪案"],
                        "current_stage": "旧案调查",
                    },
                }],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(summary["story_overview"], "主角调查旧案并回收悬念。")
        self.assertTrue(any('"rolling_context_timeline"' in prompt for prompt in prompts))
        self.assertTrue(any("调查推进到旧案线索" in prompt for prompt in prompts))
        self.assertFalse(any('"context_snapshot_used"' in prompt for prompt in prompts))
        self.assertFalse(any("不应进入总评材料" in prompt for prompt in prompts))

    def test_general_scan_splits_chunk_on_context_overflow(self):
        old_scan_chunk = general_scan._scan_chunk
        calls = []
        snapshots = []
        entity_prescans = []
        try:
            def fake_scan(chunk, chunk_index, total_chunks, profile=None, density_profile=None, context_snapshot=None, entity_prescan=None):
                calls.append(chunk)
                snapshots.append(context_snapshot or {})
                entity_prescans.append(entity_prescan or [])
                if len(calls) == 1:
                    raise RuntimeError("maximum context length exceeded")
                return {
                    "chunk_index": chunk_index,
                    "plot_events": [f"事件{len(calls) - 1}"],
                    "conflicts": [],
                    "worldbuilding": [],
                    "themes": [],
                    "foreshadowing": [],
                    "quality_notes": [],
                    "specialty_notes": [],
                    "one_sentence_summary": f"摘要{len(calls) - 1}",
                }

            general_scan._scan_chunk = fake_scan
            result = general_scan._scan_chunk_with_context_overflow_fallback(
                "甲" * 20,
                4,
                10,
                profile=analysis_profiles.load_analysis_profile("general"),
                context_snapshot={"previous_progress": "旧进展" * 200, "active_characters": ["林澈"]},
                entity_prescan=[{"name": "林澈", "entity_type": "person", "confidence": "high", "score": 3}],
            )
        finally:
            general_scan._scan_chunk = old_scan_chunk

        self.assertEqual(len(calls), 3)
        self.assertTrue(all((items or [{}])[0].get("name") == "林澈" for items in entity_prescans))
        self.assertLessEqual(
            len(json.dumps(snapshots[1], ensure_ascii=False)),
            max(400, general_scan.CONTEXT_MAX_CHARS // 2),
        )
        self.assertTrue(result["partial_result"])
        self.assertEqual(result["partial_reason"], "context_overflow_split")
        self.assertEqual(result["partial_count"], 2)
        self.assertEqual(result["plot_events"], ["事件1", "事件2"])
        self.assertEqual(result["one_sentence_summary"], "摘要1；摘要2")

    def test_general_scan_summary_accepts_kimi_field_aliases(self):
        profile = analysis_profiles.load_analysis_profile("apocalypse_survival")
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "末世求生。",
                    "main_plot": ["主角建立据点"],
                    "core_conflicts": ["幸存者争夺资源"],
                    "worldbuilding": ["灾变后秩序崩塌"],
                    "themes": ["人性选择"],
                    "foreshadowing_and_payoff": ["基地隐患"],
                    "humanity_and_morality": ["旧 Kimi 字段：道德选择有代价"],
                    "power_system": ["旧 Kimi 字段：异能进化消耗资源"],
                    "exploration_and_adventure": ["旧 Kimi 字段：外出探索有路线风险"],
                    "strengths": ["末世氛围稳定"],
                    "risks_or_issues": ["资源压力不足"],
                    "reader_fit": "末世读者",
                    "overall_assessment": "可读",
                }

            general_scan._call_json = fake_call_json
            summary = general_scan._summarize_book(
                "末世测试",
                [{"one_sentence_summary": "主角建立据点。"}],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(summary["humanity_moral_dilemmas"], ["旧 Kimi 字段：道德选择有代价"])
        self.assertEqual(summary["power_evolution_system"], ["旧 Kimi 字段：异能进化消耗资源"])
        self.assertEqual(summary["exploration_adventure"], ["旧 Kimi 字段：外出探索有路线风险"])

    def test_general_scan_summary_accepts_common_specialty_field_aliases(self):
        steampunk_profile = analysis_profiles.load_analysis_profile("steampunk_fantasy")
        mystery_profile = analysis_profiles.load_analysis_profile("mystery_detective")
        crime_profile = analysis_profiles.load_analysis_profile("crime_forensics")
        old_call_json = general_scan._call_json
        try:
            def fake_steampunk_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "蒸汽侦探处理单元案件。",
                    "main_plot": ["侦探接案"],
                    "core_conflicts": ["系统破案与人工推理冲突"],
                    "worldbuilding": ["蒸汽帝国"],
                    "themes": ["技术代价"],
                    "foreshadowing_and_payoff": ["神秘复苏伏笔"],
                    "tech_plausibility": ["旧字段：煤精和差分机需要制造链解释"],
                    "strengths": ["氛围明确"],
                    "risks_or_issues": ["技术跃迁偏快"],
                    "reader_fit": "蒸汽读者",
                    "overall_assessment": "可读",
                }

            general_scan._call_json = fake_steampunk_json
            steampunk_summary = general_scan._summarize_book(
                "蒸汽测试",
                [{"one_sentence_summary": "侦探接案。"}],
                profile=steampunk_profile,
            )

            def fake_mystery_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "侦探连续破案。",
                    "main_plot": ["连续案件"],
                    "core_conflicts": ["侦探与凶手博弈"],
                    "worldbuilding": ["近代城市"],
                    "themes": ["真相"],
                    "foreshadowing_and_payoff": ["凶器伏笔"],
                    "clue_logic": ["旧字段：关键线索提前出现"],
                    "case_logic": ["旧字段：推理链条闭合"],
                    "strengths": ["案件清晰"],
                    "risks_or_issues": ["外挂偏强"],
                    "reader_fit": "推理读者",
                    "overall_assessment": "尚可",
                }

            general_scan._call_json = fake_mystery_json
            mystery_summary = general_scan._summarize_book(
                "推理测试",
                [{"one_sentence_summary": "侦探破案。"}],
                profile=mystery_profile,
            )

            def fake_crime_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "刑警围绕命案推进侦查。",
                    "main_plot": ["命案侦查"],
                    "core_conflicts": ["刑警与嫌疑人对抗"],
                    "worldbuilding": ["现代刑侦"],
                    "themes": ["法律与真相"],
                    "foreshadowing_and_payoff": ["物证伏笔"],
                    "case_design": ["旧字段：案件结构有起承转合"],
                    "strengths": ["证据清晰"],
                    "risks_or_issues": ["程序细节略弱"],
                    "reader_fit": "刑侦读者",
                    "overall_assessment": "尚可",
                }

            general_scan._call_json = fake_crime_json
            crime_summary = general_scan._summarize_book(
                "刑侦测试",
                [{"one_sentence_summary": "刑警破案。"}],
                profile=crime_profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(steampunk_summary["tech_feasibility"], ["旧字段：煤精和差分机需要制造链解释"])
        self.assertEqual(crime_summary["case_structure"], ["旧字段：案件结构有起承转合"])
        self.assertEqual(mystery_summary["clue_fairness"], ["旧字段：关键线索提前出现"])
        self.assertEqual(mystery_summary["logic_chain_integrity"], ["旧字段：推理链条闭合"])

    def test_general_scan_and_report_accept_extended_specialty_aliases(self):
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "多类型专项字段别名测试。",
                    "plot": ["主线"],
                    "conflicts": ["冲突"],
                    "world_building": ["设定"],
                    "scientific_logic": ["旧字段：科学逻辑自洽"],
                    "scientific_basis": ["旧字段：核心科学假设清楚"],
                    "battlefield_operations": ["旧字段：战术行动依赖地形与兵种配合"],
                    "military_logistics": ["旧字段：补给线和医疗消耗被纳入叙事"],
                    "military_equipment": ["旧字段：火炮与通信设备形成科技树"],
                    "strategy": ["旧字段：战略目标与资源匹配"],
                    "command_chain": ["旧字段：指挥链责任明确"],
                    "force_building": ["旧字段：部队扩编有训练成本"],
                    "combat_scenes": ["旧字段：战斗场面服务战术目标"],
                    "diplomacy": ["旧字段：外交压力影响战局"],
                    "business_strategy": ["旧字段：商业模式有现金流闭环"],
                    "market_dynamics": ["旧字段：市场竞争形成外部压力"],
                    "org_management": ["旧字段：组织管理有制度约束"],
                    "career_arc": ["旧字段：职业成长阶段明确"],
                    "industry_chain": ["旧字段：上下游议价影响公司现金流"],
                    "office_politics": ["旧字段：董事会与人事斗争推动职场线"],
                    "industry_connections": ["旧字段：资源置换符合行业规则"],
                    "public_relations": ["旧字段：公关舆论影响事业线"],
                    "survival_resource_pressure": ["旧字段：食物药品构成生存压力"],
                    "shelter_order": ["旧字段：避难所规则支撑秩序"],
                    "collapse_rebuild": ["旧字段：旧秩序崩塌后重建组织"],
                    "production_system": ["旧字段：农田到工坊形成生产闭环"],
                    "resource_logic": ["旧字段：资源调度决定扩张速度"],
                    "tech_tree": ["旧字段：科技树升级路径清楚"],
                    "cultivation_realm": ["旧字段：境界突破规则清楚"],
                    "level_scaling": ["旧字段：战力层级没有跳档"],
                    "sect_factions": ["旧字段：宗门派系关系清楚"],
                    "daoist_theme": ["旧字段：求道主题贯穿成长线"],
                    "system_balance": ["旧字段：系统成长没有失衡"],
                    "reward_cost": ["旧字段：奖励与代价绑定"],
                    "matchup_tactics": ["旧字段：对位选择决定关键比赛走势"],
                    "opponent_rivalry": ["旧字段：宿敌竞争关系稳定推进"],
                    "procedure_realism": ["旧字段：非法取证会影响证据效力"],
                    "case_fairness": ["旧字段：谜题信息对读者公平"],
                    "forensic_realism": ["旧字段：法医流程保留误差边界"],
                    "teamwork": ["旧字段：团队分工推动案件侦查"],
                    "corruption_cost": ["旧字段：污染代价影响角色理智"],
                    "campus_life": ["旧字段：班级社团与宿舍日常具体"],
                    "youth_growth": ["旧字段：升学压力推动成长弧线"],
                    "advantages": ["优点"],
                    "issues": ["问题"],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                }

            general_scan._call_json = fake_call_json
            profile = analysis_profiles.AnalysisProfile(
                name="extended_alias_test",
                display_name="扩展别名测试",
                description="",
                enabled_stages=["general_scan"],
                rules_file="",
                report_mode="general",
                scan_focus=[],
                summary_fields=[
                    "tactics_and_operations",
                    "logistics_and_cost",
                    "equipment_and_tech",
                    "scientific_assumptions",
                    "science_consistency",
                    "strategy_logic",
                    "command_structure",
                    "force_buildup",
                    "combat_writing",
                    "political_diplomacy",
                    "business_model",
                    "market_competition",
                    "organization_management",
                    "career_progression",
                    "supply_chain",
                    "corporate_politics",
                    "industry_resources",
                    "public_opinion",
                    "survival_resources",
                    "shelter_and_order",
                    "social_collapse_and_rebuild",
                    "production_chain",
                    "resource_management",
                    "technology_progression",
                    "cultivation_system",
                    "power_scaling",
                    "faction_structure",
                    "dao_theme",
                    "progression_balance",
                    "reward_and_cost",
                    "tactical_matchups",
                    "rivalry_and_opponents",
                    "puzzle_fairness",
                    "forensic_procedure",
                    "team_dynamics",
                    "legal_realism",
                    "sanity_and_corruption",
                    "campus_setting",
                    "coming_of_age",
                    "reader_fit",
                    "overall_assessment",
                ],
                harem_plus={},
                cross_profile_rules={},
            )
            summary = general_scan._summarize_book(
                "扩展别名测试",
                [{"one_sentence_summary": "测试。"}],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(summary["tactics_and_operations"], ["旧字段：战术行动依赖地形与兵种配合"])
        self.assertEqual(summary["logistics_and_cost"], ["旧字段：补给线和医疗消耗被纳入叙事"])
        self.assertEqual(summary["equipment_and_tech"], ["旧字段：火炮与通信设备形成科技树"])
        self.assertEqual(summary["scientific_assumptions"], ["旧字段：核心科学假设清楚"])
        self.assertEqual(summary["science_consistency"], ["旧字段：科学逻辑自洽"])
        self.assertEqual(summary["strategy_logic"], ["旧字段：战略目标与资源匹配"])
        self.assertEqual(summary["command_structure"], ["旧字段：指挥链责任明确"])
        self.assertEqual(summary["force_buildup"], ["旧字段：部队扩编有训练成本"])
        self.assertEqual(summary["combat_writing"], ["旧字段：战斗场面服务战术目标"])
        self.assertEqual(summary["political_diplomacy"], ["旧字段：外交压力影响战局"])
        self.assertEqual(summary["business_model"], ["旧字段：商业模式有现金流闭环"])
        self.assertEqual(summary["market_competition"], ["旧字段：市场竞争形成外部压力"])
        self.assertEqual(summary["organization_management"], ["旧字段：组织管理有制度约束"])
        self.assertEqual(summary["career_progression"], ["旧字段：职业成长阶段明确"])
        self.assertEqual(summary["supply_chain"], ["旧字段：上下游议价影响公司现金流"])
        self.assertEqual(summary["corporate_politics"], ["旧字段：董事会与人事斗争推动职场线"])
        self.assertEqual(summary["industry_resources"], ["旧字段：资源置换符合行业规则"])
        self.assertEqual(summary["public_opinion"], ["旧字段：公关舆论影响事业线"])
        self.assertEqual(summary["survival_resources"], ["旧字段：食物药品构成生存压力"])
        self.assertEqual(summary["shelter_and_order"], ["旧字段：避难所规则支撑秩序"])
        self.assertEqual(summary["social_collapse_and_rebuild"], ["旧字段：旧秩序崩塌后重建组织"])
        self.assertEqual(summary["production_chain"], ["旧字段：农田到工坊形成生产闭环"])
        self.assertEqual(summary["resource_management"], ["旧字段：资源调度决定扩张速度"])
        self.assertEqual(summary["technology_progression"], ["旧字段：科技树升级路径清楚"])
        self.assertEqual(summary["cultivation_system"], ["旧字段：境界突破规则清楚"])
        self.assertEqual(summary["power_scaling"], ["旧字段：战力层级没有跳档"])
        self.assertEqual(summary["faction_structure"], ["旧字段：宗门派系关系清楚"])
        self.assertEqual(summary["dao_theme"], ["旧字段：求道主题贯穿成长线"])
        self.assertEqual(summary["progression_balance"], ["旧字段：系统成长没有失衡"])
        self.assertEqual(summary["reward_and_cost"], ["旧字段：奖励与代价绑定"])
        self.assertEqual(summary["tactical_matchups"], ["旧字段：对位选择决定关键比赛走势"])
        self.assertEqual(summary["rivalry_and_opponents"], ["旧字段：宿敌竞争关系稳定推进"])
        self.assertEqual(summary["puzzle_fairness"], ["旧字段：谜题信息对读者公平"])
        self.assertEqual(summary["forensic_procedure"], ["旧字段：法医流程保留误差边界"])
        self.assertEqual(summary["team_dynamics"], ["旧字段：团队分工推动案件侦查"])
        self.assertEqual(summary["legal_realism"], ["旧字段：非法取证会影响证据效力"])
        self.assertEqual(summary["sanity_and_corruption"], ["旧字段：污染代价影响角色理智"])
        self.assertEqual(summary["campus_setting"], ["旧字段：班级社团与宿舍日常具体"])
        self.assertEqual(summary["coming_of_age"], ["旧字段：升学压力推动成长弧线"])

        text = report.build_general_report(
            "扩展别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "扩展别名测试",
                "summary_fields": profile.summary_fields,
                "summary": summary,
            },
        )
        for title in [
            "战术与行动",
            "后勤与战争代价",
            "装备与军工科技",
            "科学假设",
            "科学设定自洽性",
            "战略逻辑",
            "指挥链与组织",
            "部队建设",
            "战斗描写",
            "政治与外交",
            "商业模式",
            "市场竞争",
            "组织管理",
            "职场成长",
            "供应链/产业链",
            "职场政治",
            "行业资源",
            "舆论经营",
            "生存资源",
            "据点与秩序",
            "秩序崩塌与重建",
            "生产链条",
            "资源管理",
            "技术升级路径",
            "修炼体系",
            "战力层级",
            "势力结构",
            "求道/长生主题",
            "成长与数值平衡",
            "奖励与代价",
            "战术对局",
            "对手群像",
            "谜题公平性",
            "法医与侦查程序",
            "团队协作",
            "法律现实性",
            "理智与污染代价",
            "校园环境",
            "成长弧线",
        ]:
            self.assertIn(f"【{title}】", text)
        self.assertIn("旧字段：对位选择决定关键比赛走势", text)

    def test_general_scan_summary_alias_candidates_are_bidirectional(self):
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(_messages, max_tokens=3000):
                return {
                    "overview": "旧字段概览测试。",
                    "plot": ["旧字段主线"],
                    "conflicts": ["旧字段冲突"],
                    "world_building": ["旧字段设定"],
                    "theme": ["旧字段主题"],
                    "characters": ["旧字段角色内容"],
                    "pacing_emotion": ["旧字段节奏情绪内容"],
                    "historical_accuracy": ["旧字段史实逻辑内容"],
                    "political_structure": ["旧字段权力结构内容"],
                    "war_intrigue": ["旧字段战争权谋内容"],
                    "foreshadowing": ["旧字段伏笔内容"],
                    "foreshadowing_and_payoff": ["伏笔"],
                    "tech_chain": ["旧字段技术链内容"],
                    "science_logic": ["旧字段科学逻辑内容"],
                    "sense_of_wonder": ["旧字段科幻奇观内容"],
                    "tech_feasibility": ["标准字段技术内容"],
                    "technology_feasibility": ["标准字段技术内容", "同义旧字段技术内容"],
                    "advantages": ["旧字段优点"],
                    "issues": ["旧字段问题"],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                }

            general_scan._call_json = fake_call_json
            profile = analysis_profiles.AnalysisProfile(
                name="alias_test",
                display_name="别名测试",
                description="",
                enabled_stages=["general_scan"],
                rules_file="",
                report_mode="general",
                scan_focus=[],
                summary_fields=[
                    "main_plot",
                    "character_highlights",
                    "pacing_and_emotion",
                    "historical_logic",
                    "power_structure",
                    "warfare_and_intrigue",
                    "technology_chain",
                    "science_consistency",
                    "scale_and_wonder",
                    "tech_plausibility",
                ],
                harem_plus={},
                cross_profile_rules={},
            )
            summary = general_scan._summarize_book(
                "别名测试",
                [{"one_sentence_summary": "测试。"}],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertEqual(
            summary["tech_plausibility"],
            ["标准字段技术内容", "同义旧字段技术内容"],
        )
        self.assertEqual(summary["story_overview"], "旧字段概览测试。")
        self.assertEqual(summary["main_plot"], ["旧字段主线"])
        self.assertEqual(summary["core_conflicts"], ["旧字段冲突"])
        self.assertEqual(summary["worldbuilding"], ["旧字段设定"])
        self.assertEqual(summary["themes"], ["旧字段主题"])
        self.assertEqual(summary["character_highlights"], ["旧字段角色内容"])
        self.assertEqual(summary["pacing_and_emotion"], ["旧字段节奏情绪内容"])
        self.assertEqual(summary["historical_logic"], ["旧字段史实逻辑内容"])
        self.assertEqual(summary["power_structure"], ["旧字段权力结构内容"])
        self.assertEqual(summary["warfare_and_intrigue"], ["旧字段战争权谋内容"])
        self.assertEqual(summary["technology_chain"], ["旧字段技术链内容"])
        self.assertEqual(summary["science_consistency"], ["旧字段科学逻辑内容"])
        self.assertEqual(summary["scale_and_wonder"], ["旧字段科幻奇观内容"])
        self.assertEqual(summary["strengths"], ["旧字段优点"])
        self.assertEqual(summary["risks_or_issues"], ["旧字段问题"])
        self.assertEqual(summary["foreshadowing_and_payoff"], ["伏笔", "旧字段伏笔内容"])

    def test_general_report_reads_summary_field_alias_values(self):
        self.assertEqual(report.summary_field_label("foreshadowing"), "伏笔与回收")
        self.assertEqual(report.summary_field_label("characters"), "角色亮点")
        self.assertEqual(report.summary_field_label("pacing_emotion"), "节奏与情绪曲线")
        self.assertEqual(report.summary_field_label("historical_accuracy"), "历史制度与时代逻辑")
        self.assertEqual(report.summary_field_label("political_structure"), "权力结构与派系")
        self.assertEqual(report.summary_field_label("war_intrigue"), "战争与权谋")
        self.assertEqual(report.summary_field_label("tech_chain"), "技术链与工程约束")
        self.assertEqual(report.summary_field_label("science_logic"), "科学设定自洽性")
        self.assertEqual(report.summary_field_label("sense_of_wonder"), "尺度感与科幻奇观")
        self.assertEqual(report.summary_field_label("power_system"), "异能/金手指体系")
        self.assertEqual(report.summary_field_label("humanity_and_morality"), "人性与道德困境")
        self.assertEqual(report.summary_field_label("tech_plausibility"), "技术可行性")
        self.assertEqual(report.summary_field_label("case_design"), "案件结构")
        self.assertEqual(report.summary_field_label("unit_plot_mainline_link"), "单元剧情与主线连接度")
        self.assertEqual(report.summary_field_label("cheat_detection_dependency"), "外挂破案依赖度")
        self.assertEqual(report.summary_field_label("romance_subplot"), "恋爱喜剧平衡")
        self.assertEqual(report.summary_field_label("weird_rules"), "规则机制")
        self.assertEqual(report.summary_field_label("folk_taboo_system"), "民俗禁忌体系")
        self.assertEqual(report.summary_field_label("alias_system"), "马甲体系")
        self.assertEqual(report.summary_field_label("information_asymmetry"), "信息差操纵")
        self.assertEqual(report.summary_field_label("mastermind_schemes"), "幕后排局")

        text = report.build_general_report(
            "字段别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "末世生存专长分析",
                "summary_fields": ["main_plot", "humanity_moral_dilemmas", "power_evolution_system"],
                "summary": {
                    "story_overview": "末世概览",
                    "main_plot": ["主线"],
                    "humanity_and_morality": ["旧字段人性道德内容"],
                    "power_system": ["旧字段能力体系内容"],
                    "strengths": [],
                    "risks_or_issues": [],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )

        self.assertIn("【人性与道德困境】", text)
        self.assertIn("旧字段人性道德内容", text)
        self.assertIn("【能力/进化体系】", text)
        self.assertIn("旧字段能力体系内容", text)

        specialty_text = report.build_general_report(
            "专项字段别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "混合专长分析",
                "summary_fields": ["tech_feasibility", "case_structure", "romance_comedy_balance"],
                "summary": {
                    "story_overview": "专项概览",
                    "tech_plausibility": ["旧字段技术可行性内容"],
                    "case_design": ["旧字段案件结构内容"],
                    "romance_subplot": ["旧字段恋爱支线内容"],
                    "strengths": [],
                    "risks_or_issues": [],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )

        self.assertIn("【技术可行性】", specialty_text)
        self.assertIn("旧字段技术可行性内容", specialty_text)
        self.assertIn("【案件结构】", specialty_text)
        self.assertIn("旧字段案件结构内容", specialty_text)
        self.assertIn("【恋爱喜剧平衡】", specialty_text)
        self.assertIn("旧字段恋爱支线内容", specialty_text)

        kimi_alias_text = report.build_general_report(
            "Kimi旧字段别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "历史科幻混合分析",
                "summary_fields": [
                    "character_highlights",
                    "pacing_and_emotion",
                    "historical_logic",
                    "power_structure",
                    "warfare_and_intrigue",
                    "technology_chain",
                    "science_consistency",
                    "scale_and_wonder",
                ],
                "summary": {
                    "story_overview": "Kimi旧字段概览",
                    "characters": ["旧字段角色内容"],
                    "pacing_emotion": ["旧字段节奏情绪内容"],
                    "historical_accuracy": ["旧字段史实逻辑内容"],
                    "political_structure": ["旧字段权力结构内容"],
                    "war_intrigue": ["旧字段战争权谋内容"],
                    "tech_chain": ["旧字段技术链内容"],
                    "science_logic": ["旧字段科学逻辑内容"],
                    "sense_of_wonder": ["旧字段科幻奇观内容"],
                    "strengths": [],
                    "risks_or_issues": [],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )

        self.assertIn("旧字段角色内容", kimi_alias_text)
        self.assertIn("旧字段节奏情绪内容", kimi_alias_text)
        self.assertIn("旧字段史实逻辑内容", kimi_alias_text)
        self.assertIn("旧字段权力结构内容", kimi_alias_text)
        self.assertIn("旧字段战争权谋内容", kimi_alias_text)
        self.assertIn("旧字段技术链内容", kimi_alias_text)
        self.assertIn("旧字段科学逻辑内容", kimi_alias_text)
        self.assertIn("旧字段科幻奇观内容", kimi_alias_text)

        base_alias_text = report.build_general_report(
            "通用旧字段别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "通用小说分析",
                "summary_fields": ["main_plot"],
                "summary": {
                    "book_overview": "通用旧字段概览",
                    "plot": ["旧字段主线"],
                    "conflicts": ["旧字段冲突"],
                    "setting": ["旧字段设定"],
                    "theme": ["旧字段主题"],
                    "advantages": ["旧字段优点"],
                    "issues": ["旧字段问题"],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )

        self.assertIn("通用旧字段概览", base_alias_text)
        self.assertIn("【主线剧情】", base_alias_text)
        self.assertIn("旧字段主线", base_alias_text)
        self.assertIn("旧字段冲突", base_alias_text)
        self.assertIn("旧字段设定", base_alias_text)
        self.assertIn("旧字段主题", base_alias_text)
        self.assertIn("旧字段优点", base_alias_text)
        self.assertIn("旧字段问题", base_alias_text)

        legacy_field_text = report.build_general_report(
            "旧请求字段别名测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "旧请求字段分析",
                "summary_fields": ["tech_plausibility"],
                "summary": {
                    "story_overview": "旧请求字段概览",
                    "tech_feasibility": ["标准字段技术内容"],
                    "technology_feasibility": ["同义旧字段技术内容"],
                    "strengths": [],
                    "risks_or_issues": [],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )

        self.assertIn("【技术可行性】", legacy_field_text)
        self.assertIn("标准字段技术内容", legacy_field_text)
        self.assertIn("同义旧字段技术内容", legacy_field_text)
        self.assertEqual(legacy_field_text.count("标准字段技术内容"), 1)

        foreshadowing_text = report.build_general_report(
            "伏笔旧字段测试",
            {"male_protagonist": {"name": "男主"}, "all_female_characters": {}},
            {
                "profile_display_name": "通用小说分析",
                "summary_fields": ["main_plot", "foreshadowing_and_payoff"],
                "summary": {
                    "story_overview": "伏笔概览",
                    "main_plot": ["主线"],
                    "foreshadowing": ["旧字段伏笔内容"],
                    "strengths": [],
                    "risks_or_issues": [],
                    "reader_fit": "读者",
                    "overall_assessment": "评价",
                },
            },
        )
        self.assertIn("【伏笔与回收】", foreshadowing_text)
        self.assertIn("旧字段伏笔内容", foreshadowing_text)

    def test_general_scan_field_labels_cover_profile_summary_fields(self):
        for profile in analysis_profiles.list_available_profiles():
            for field in profile.summary_fields:
                self.assertNotEqual(general_scan._summary_field_label(field), field.replace("_", " "), field)

    def test_general_scan_effective_max_chunks_scales_for_long_books(self):
        self.assertEqual(general_scan._effective_max_chunks(500_000, 80), 80)
        self.assertEqual(general_scan._effective_max_chunks(1_500_000, 80), 120)
        self.assertEqual(general_scan._effective_max_chunks(4_000_000, 80), 160)
        self.assertEqual(general_scan._effective_max_chunks(6_000_000, 80), 300)
        self.assertEqual(general_scan._effective_max_chunks(10_000_000, 80), 300)
        self.assertEqual(general_scan._effective_max_chunks(60_000_000, 80), 400)
        self.assertEqual(general_scan._effective_max_chunks(6_000_000, 0), 0)
        self.assertEqual(general_scan._effective_max_chunks(10_000_000, 360), 360)

    def test_general_scan_samples_long_books_across_timeline(self):
        entries = [{"chunk_index": i + 1, "text": f"chunk-{i + 1}"} for i in range(1000)]
        sampled = general_scan._sample_chunk_entries_for_budget(entries, 10)
        sampled_indices = [item["chunk_index"] for item in sampled]

        self.assertEqual(len(sampled), 10)
        self.assertEqual(sampled_indices[0], 1)
        self.assertEqual(sampled_indices[-1], 1000)
        self.assertEqual(sampled_indices, sorted(sampled_indices))
        self.assertTrue(any(440 <= idx <= 560 for idx in sampled_indices))

        self.assertEqual(general_scan._sample_chunk_entries_for_budget(entries, 0), entries)
        self.assertEqual(general_scan._sample_chunk_entries_for_budget(entries, 1), [entries[0]])

    def test_general_scan_content_aware_sampling_keeps_high_signal_chunks(self):
        entries = [
            {"chunk_index": i + 1, "text": "赶路吃饭睡觉。"}
            for i in range(100)
        ]
        entries[49]["text"] = "大战爆发，主角突破并揭露真相，旧伏笔回收。"

        sampled = general_scan._sample_chunk_entries_for_budget(entries, 10, content_aware=True)
        content_indices = [item["chunk_index"] for item in sampled]
        uniform_indices = [
            item["chunk_index"]
            for item in general_scan._sample_chunk_entries_for_budget(entries, 10, content_aware=False)
        ]

        self.assertEqual(len(sampled), 10)
        self.assertEqual(content_indices[0], 1)
        self.assertEqual(content_indices[-1], 100)
        self.assertEqual(content_indices, sorted(content_indices))
        self.assertIn(50, content_indices)
        self.assertNotIn(50, uniform_indices)

    def test_general_character_scan_samples_ten_million_word_books(self):
        self.assertEqual(protagonist._effective_general_character_max_chunks(10_000_000, 80), 300)
        sampled_indices = protagonist._sample_chunk_indices_for_budget(1000, 300)

        self.assertEqual(len(sampled_indices), 300)
        self.assertEqual(sampled_indices[0], 0)
        self.assertEqual(sampled_indices[-1], 999)
        self.assertEqual(sampled_indices, sorted(sampled_indices))
        self.assertTrue(any(450 <= idx <= 550 for idx in sampled_indices))
        self.assertEqual(protagonist._sample_chunk_indices_for_budget(1000, 0), list(range(1000)))

    def test_protagonist_checkpoint_chunk_plan_must_match(self):
        current = protagonist._character_chunk_plan_metadata(
            text_length=10000,
            source_total_chunks=2,
            target_chunk_indices=[0, 1],
            sampling_strategy="full",
            effective_max_chunks=0,
        )
        self.assertTrue(protagonist._checkpoint_chunk_plan_matches(dict(current), current))
        stale = dict(current)
        stale["chunk_size"] = current["chunk_size"] + 1
        self.assertFalse(protagonist._checkpoint_chunk_plan_matches(stale, current))

    def test_alias_cross_merge_batches_respect_payload_budget(self):
        candidates = [
            {
                "name": f"角色{i}",
                "aliases": [f"别名{i}", "长别名" * 80],
                "avg_score": 8.0,
                "count": 10,
                "other_names": ["其他称呼" * 80],
                "features": ["外貌描写" * 80],
                "appearances": ["出场描写" * 80],
                "relationships": ["关系描写" * 80],
                "summaries": ["摘要内容" * 80],
            }
            for i in range(40)
        ]

        with mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_PAYLOAD_CHARS", 1200), \
                mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_FIELD_CHARS", 24), \
                mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_LIST_ITEMS", 2):
            batches = protagonist._split_alias_cross_merge_batches(candidates)

        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for batch in batches), len(candidates))
        for batch in batches:
            self.assertLessEqual(len(json.dumps(batch, ensure_ascii=False)), 1200)
        serialized = json.dumps(batches, ensure_ascii=False)
        self.assertNotIn("外貌描写" * 20, serialized)

    def test_merge_aliases_limits_cross_batch_payload_size(self):
        global_stats = {}
        for i in range(65):
            main_name = f"甲女{i}"
            alias_name = f"阿甲{i}"
            global_stats[main_name] = {
                "total_score": 30,
                "count": 10,
                "chunk_scores": [],
                "summaries": [f"甲女{i}与男主同行，身份线索一致。" + "摘要" * 80],
                "types": set(),
                "other_names": {alias_name, "长别名" * 80},
                "appearances": ["出场" * 100],
                "features": [f"甲女{i}有银发蓝眼。" + "特征" * 80],
                "relationships": [f"甲女{i}与男主互相照应。" + "关系" * 80],
                "interactions": [],
                "emotion_signals": [],
            }
            global_stats[alias_name] = {
                "total_score": 10,
                "count": 4,
                "chunk_scores": [],
                "summaries": [f"甲女{i}与男主同行，身份线索一致。" + "别名摘要" * 80],
                "types": set(),
                "other_names": {main_name},
                "appearances": ["别名出场" * 100],
                "features": [f"甲女{i}有银发蓝眼。" + "别名特征" * 80],
                "relationships": [f"甲女{i}与男主互相照应。" + "别名关系" * 80],
                "interactions": [],
                "emotion_signals": [],
            }

        payload_lengths = []

        def fake_call_merge_ai(characters_batch, conflict_pairs, batch_info="", mutual_pairs=None):
            if "跨批次检查" in str(batch_info):
                payload_lengths.append(len(json.dumps(characters_batch, ensure_ascii=False)))
                return [], []
            merge_groups = []
            for item in characters_batch:
                name = item["name"]
                if name.startswith("甲女"):
                    merge_groups.append({
                        "main_name": name,
                        "aliases": [f"阿甲{name[2:]}"],
                        "reason": "测试合并",
                    })
            return merge_groups, []

        with mock.patch.object(protagonist, "MAX_WORKERS", 1), \
                mock.patch.object(protagonist, "tqdm", side_effect=lambda items, **kwargs: items), \
                mock.patch.object(protagonist, "_get_generation_conflict_pairs", return_value=[]), \
                mock.patch.object(protagonist, "_detect_same_name_prefix_pairs", return_value=[]), \
                mock.patch.object(protagonist, "_clean_contaminated_other_names", return_value=0), \
                mock.patch.object(protagonist, "_detect_mutual_other_names", return_value=[]), \
                mock.patch.object(protagonist, "_should_accept_merge_pair", return_value=(True, "测试证据充分")), \
                mock.patch.object(protagonist, "_call_merge_ai", side_effect=fake_call_merge_ai), \
                mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_PAYLOAD_CHARS", 1200), \
                mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_FIELD_CHARS", 24), \
                mock.patch.object(protagonist, "ALIAS_CROSS_MERGE_MAX_LIST_ITEMS", 2):
            protagonist.merge_aliases(global_stats)

        self.assertGreater(len(payload_lengths), 1)
        self.assertTrue(all(length <= 1200 for length in payload_lengths))

    def test_protagonist_general_main_samples_long_books_across_timeline(self):
        old_profile = os.environ.get("ANALYSIS_PROFILE")
        try:
            os.environ["ANALYSIS_PROFILE"] = "general"
            with tempfile.TemporaryDirectory() as tmpdir:
                novels_dir = os.path.join(tmpdir, "novels")
                os.makedirs(novels_dir, exist_ok=True)
                novel_path = os.path.join(novels_dir, "ten_million.txt")
                with open(novel_path, "w", encoding="utf-8") as f:
                    f.write("stub")

                scanned_indices = []

                def fake_analyze(chunk, chunk_index, total_chunks, max_retries=3):
                    scanned_indices.append(chunk_index)
                    return {
                        "_success": True,
                        "male_protagonist": {"name": "主角", "summary": "行动"},
                        "female_characters": [],
                    }

                with mock.patch.object(protagonist, "get_base_dir", return_value=tmpdir), \
                        mock.patch.object(protagonist, "init_token_tracker"), \
                        mock.patch.object(protagonist, "read_novel", return_value="字" * 10_000_000), \
                        mock.patch.object(protagonist, "split_text_by_length", return_value=[f"chunk-{i}" for i in range(1000)]), \
                        mock.patch.object(protagonist, "validate_config"), \
                        mock.patch.object(protagonist, "tqdm", side_effect=lambda items, **kwargs: items), \
                        mock.patch.object(protagonist, "analyze_chunk_for_heroines", side_effect=fake_analyze), \
                        mock.patch.object(protagonist, "identify_male_protagonist", return_value={"name": "主角"}), \
                        mock.patch.object(protagonist, "merge_aliases", return_value={}), \
                        mock.patch.object(protagonist, "identify_heroines", return_value={"heroines": []}), \
                        mock.patch.object(protagonist, "merge_heroines_final", side_effect=lambda result, stats: result), \
                        mock.patch.object(protagonist, "generate_final_report", return_value="report"), \
                        mock.patch.object(protagonist, "export_results", return_value=("detail.json", "snapshot.json", "report.txt")):
                    self.assertEqual(protagonist.main(novel_path=novel_path, book_name="ten_million"), 0)

                sorted_scanned_indices = sorted(scanned_indices)
                self.assertEqual(len(sorted_scanned_indices), 300)
                self.assertEqual(sorted_scanned_indices[0], 0)
                self.assertEqual(sorted_scanned_indices[-1], 999)
                self.assertTrue(any(450 <= idx <= 550 for idx in sorted_scanned_indices))

                checkpoint_path = os.path.join(
                    tmpdir,
                    "results",
                    next(name for name in os.listdir(os.path.join(tmpdir, "results")) if name.startswith("ten_million_characters_")),
                    "latest_checkpoint.json",
                )
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    checkpoint = json.load(f)
                self.assertTrue(checkpoint["progress"]["scanned"])
                self.assertEqual(len(checkpoint["completed_chunks"]), 300)
        finally:
            if old_profile is None:
                os.environ.pop("ANALYSIS_PROFILE", None)
            else:
                os.environ["ANALYSIS_PROFILE"] = old_profile

    def test_general_scan_main_supports_ten_million_word_books(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "ten_million.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("stub")

            manifest = {
                "text_length": 10_000_000,
                "chunks": [
                    {"chunk_index": i + 1, "text": f"chunk-{i + 1}"}
                    for i in range(1000)
                ],
            }
            manifest["chunks"][499]["text"] = "大战爆发，主角突破并揭露真相，旧伏笔回收。"

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, entity_prescan=None):
                return {
                    "chunk_index": chunk_index,
                    "one_sentence_summary": f"{chunk}/{total_chunks}",
                    "plot_events": [f"事件{chunk_index}"],
                    "worldbuilding": ["长篇世界规则"],
                    "context_state_update": {
                        "active_characters": [f"角色{chunk_index}"],
                        "open_threads": [f"线索{chunk_index}"],
                    },
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="stub"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "KNOWLEDGE_BASE_LLM_MERGE_ENABLED", True), \
                    mock.patch.object(general_scan, "_merge_knowledge_base_with_llm", side_effect=lambda book, kb, profile=None: kb) as merge_mock, \
                    mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan) as scan_mock, \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="ten_million"), 0)

            latest_path = os.path.join(results_dir, "ten_million_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            sampled_indices = data["sampled_chunk_indices"]
            self.assertEqual(scan_mock.call_count, 300)
            self.assertEqual(data["text_length"], 10_000_000)
            self.assertEqual(data["source_chunk_count"], 1000)
            self.assertEqual(data["chunk_count"], 300)
            self.assertEqual(data["max_chunks"], 300)
            self.assertEqual(data["chunk_sampling_strategy"], "content_aware_timeline")
            self.assertTrue(data["smart_density"])
            self.assertTrue(data["content_aware_sampling"])
            self.assertEqual(data["content_aware_sampling_schema_version"], general_scan.CONTENT_AWARE_SAMPLING_SCHEMA_VERSION)
            self.assertTrue(data["foreshadowing_engineering_enabled"])
            self.assertEqual(data["foreshadowing_engineering_schema_version"], general_scan.FORESHADOWING_ENGINEERING_SCHEMA_VERSION)
            self.assertTrue(data["semantic_layers_enabled"])
            self.assertEqual(data["semantic_layers_schema_version"], general_scan.SEMANTIC_LAYERS_SCHEMA_VERSION)
            self.assertTrue(data["reader_experience_enabled"])
            self.assertEqual(data["reader_experience_schema_version"], general_scan.READER_EXPERIENCE_SCHEMA_VERSION)
            self.assertTrue(data["continuity_audit_enabled"])
            self.assertEqual(data["continuity_audit_schema_version"], general_scan.CONTINUITY_AUDIT_SCHEMA_VERSION)
            self.assertTrue(data["entity_prescan_enabled"])
            self.assertEqual(data["entity_prescan_schema_version"], general_scan.ENTITY_PRESCAN_SCHEMA_VERSION)
            self.assertIn("entity_prescan_type_counts", data)
            self.assertTrue(data["knowledge_base_enabled"])
            self.assertEqual(data["knowledge_base_schema_version"], general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION)
            self.assertTrue(data["knowledge_base_llm_merge_enabled"])
            self.assertTrue(data["knowledge_base_llm_merge_applied"])
            self.assertEqual(data["knowledge_base_llm_merge_error"], "")
            self.assertGreater(data["raw_knowledge_base_counts"]["entities"], 0)
            self.assertGreater(data["knowledge_base_counts"]["entities"], 0)
            self.assertGreater(data["knowledge_base_counts"]["worldbuilding_facts"], 0)
            self.assertGreater(data["knowledge_base_counts"]["plot_timeline"], 0)
            self.assertEqual(data["knowledge_base"]["schema_version"], general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION)
            self.assertEqual(merge_mock.call_count, 1)
            self.assertEqual(sum(data["density_counts"].values()), 300)
            self.assertEqual(data["prompt_templates"]["general_scan_chunk"]["version"], "v1")
            self.assertEqual(data["prompt_templates"]["general_summary"]["version"], "v1")
            self.assertEqual(sampled_indices[0], 1)
            self.assertEqual(sampled_indices[-1], 1000)
            self.assertIn(500, sampled_indices)
            self.assertTrue(any(450 <= idx <= 550 for idx in sampled_indices))

    def test_general_scan_main_reuses_overall_summary_when_all_chunks_reused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "all_reused.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("same")

            chunk_text = "第一段旧内容，不禁推进剧情。"
            chunk_hash = general_scan._chunk_text_hash(chunk_text)
            manifest = {
                "text_length": len(chunk_text),
                "chunks": [{"chunk_index": 1, "text": chunk_text}],
            }
            old_summary = {
                "story_overview": "旧总评可直接复用。",
                "zhihu_writing_insights_overall": {
                    "word_poverty": {"severity": "轻度词穷", "template_phrase_count": 1},
                },
            }
            latest_path = os.path.join(results_dir, "all_reused_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "schema_version": 1,
                    "analysis_profile": "general",
                    "specialty_profile": "general",
                    "chunk_size": general_scan.CHUNK_SIZE,
                    "chunk_overlap": general_scan.CHUNK_OVERLAP,
                    "smart_density": general_scan.SMART_DENSITY,
                    "content_aware_sampling": general_scan.CONTENT_AWARE_SAMPLING,
                    "content_aware_sampling_schema_version": general_scan.CONTENT_AWARE_SAMPLING_SCHEMA_VERSION,
                    "writing_quality_enabled": general_scan.WRITING_QUALITY_ENABLED,
                    "zhihu_writing_insights_schema_version": general_scan.ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION,
                    "narrative_architecture_enabled": general_scan.NARRATIVE_ARCHITECTURE_ENABLED,
                    "foreshadowing_engineering_enabled": general_scan.FORESHADOWING_ENGINEERING_ENABLED,
                    "semantic_layers_enabled": general_scan.SEMANTIC_LAYERS_ENABLED,
                    "reader_experience_enabled": general_scan.READER_EXPERIENCE_ENABLED,
                    "continuity_audit_enabled": general_scan.CONTINUITY_AUDIT_ENABLED,
                    "continuity_audit_schema_version": general_scan.CONTINUITY_AUDIT_SCHEMA_VERSION,
                    "entity_prescan_enabled": general_scan.ENTITY_PRESCAN_ENABLED,
                    "entity_prescan_schema_version": general_scan.ENTITY_PRESCAN_SCHEMA_VERSION,
                    "prompt_templates": {
                        "general_scan_chunk": {"name": "general_scan_chunk", "version": "v1"},
                        "general_summary": {"name": "general_summary", "version": "v1"},
                    },
                    "summary": old_summary,
                    "chunk_results": [{
                        "chunk_index": 0,
                        "chunk_hash": chunk_hash,
                        "plot_events": ["旧事件"],
                        "writing_quality": {
                            "prose_quality": {"score": 6},
                            "zhihu_insights": {
                                "word_poverty": {
                                    "template_phrase_count": 1,
                                    "template_phrase_density_per_1k": 1.0,
                                    "most_frequent_templates": ["不禁(1次)"],
                                    "severity": "偶见模板词",
                                },
                            },
                        },
                        "pacing_analysis": {"pacing_type": "transition"},
                        "information_density": {"density_score": "medium"},
                        "narrative_structure": {"structural_function_tag": "transition"},
                        "outline_architecture": {"causal_chain": {"causal_strength": "自然发展"}},
                        "foreshadowing_engineering": {"new_foreshadowing": [{"description": "旧伏笔"}]},
                        "semantic_layers": {"literal_meaning": "旧片段事实", "author_intent": "旧片段意图"},
                        "reader_experience": {
                            "immediate_emotion": {"emotion": "期待", "intensity": 6, "trigger": "旧片段推进"},
                            "immersion_anchor": "旧片段主角目标",
                            "anticipation": {"expected": "旧线索继续推进", "intensity": 6, "hook_type": "悬念"},
                            "satisfaction_points": [{"type": "解谜", "description": "旧线索给出进展", "intensity": 6}],
                            "frustration_points": [],
                            "engagement_level": "medium",
                            "experience_notes": ["旧片段体验可复用"],
                        },
                        "one_sentence_summary": "旧摘要",
                    }],
                }, f, ensure_ascii=False)

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="same"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "ROLLING_CONTEXT_ENABLED", False), \
                    mock.patch.object(general_scan, "_scan_chunk") as scan_mock, \
                    mock.patch.object(general_scan, "_summarize_book") as summarize_mock:
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="all_reused"), 0)

            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            scan_mock.assert_not_called()
            summarize_mock.assert_not_called()
            self.assertTrue(data["summary_reused"])
            self.assertEqual(data["summary"]["story_overview"], "旧总评可直接复用。")
            self.assertEqual(data["reused_chunk_count"], 1)
            self.assertEqual(data["scanned_chunk_count"], 0)

    def test_general_scan_main_passes_entity_prescan_to_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "entity_prescan.txt")
            text = "第一章：青云城\n“出发。”张三说道。李四道：“去玄天宗。”"
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write(text)

            manifest = {
                "text_length": len(text),
                "chunks": [{"chunk_index": 1, "text": text}],
            }
            seen_prescans = []

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, context_snapshot=None, entity_prescan=None):
                seen_prescans.append(entity_prescan or [])
                return {
                    "chunk_index": chunk_index,
                    "plot_events": ["张三前往玄天宗"],
                    "one_sentence_summary": "张三和李四出发。",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value=text), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "_scan_chunk_with_context_overflow_fallback", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="entity_prescan"), 0)

            latest_path = os.path.join(results_dir, "entity_prescan_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(data["failed_chunks"], [])
            names = {item.get("name") for item in seen_prescans[0]}
            self.assertIn("张三", names)
            self.assertIn("李四", names)
            self.assertIn("青云城", names)
            self.assertTrue(data["entity_prescan_enabled"])
            self.assertEqual(data["entity_prescan_count"], len(data["entity_prescan"]))
            self.assertGreaterEqual(data["entity_prescan_type_counts"]["person"], 2)

    def test_general_scan_main_reuses_unchanged_chunk_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "incremental.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("changed")

            reused_text = "第一段旧内容。"
            changed_text = "第二段新增内容。"
            reused_hash = general_scan._chunk_text_hash(reused_text)
            manifest = {
                "text_length": 20_000,
                "chunks": [
                    {"chunk_index": 1, "text": reused_text},
                    {"chunk_index": 2, "text": changed_text},
                ],
            }
            latest_path = os.path.join(results_dir, "incremental_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "schema_version": 1,
                    "analysis_profile": "general",
                    "specialty_profile": "general",
                    "chunk_size": general_scan.CHUNK_SIZE,
                    "chunk_overlap": general_scan.CHUNK_OVERLAP,
                    "smart_density": general_scan.SMART_DENSITY,
                    "writing_quality_enabled": general_scan.WRITING_QUALITY_ENABLED,
                    "zhihu_writing_insights_schema_version": general_scan.ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION,
                    "foreshadowing_engineering_enabled": general_scan.FORESHADOWING_ENGINEERING_ENABLED,
                    "semantic_layers_enabled": general_scan.SEMANTIC_LAYERS_ENABLED,
                    "reader_experience_enabled": general_scan.READER_EXPERIENCE_ENABLED,
                    "entity_prescan_enabled": general_scan.ENTITY_PRESCAN_ENABLED,
                    "entity_prescan_schema_version": general_scan.ENTITY_PRESCAN_SCHEMA_VERSION,
                    "prompt_templates": {
                        "general_scan_chunk": {"name": "general_scan_chunk", "version": "v1"},
                        "general_summary": {"name": "general_summary", "version": "v1"},
                    },
                    "chunk_results": [{
                        "chunk_index": 0,
                        "chunk_hash": reused_hash,
                        "plot_events": ["旧事件"],
                        "writing_quality": {
                            "prose_quality": {"score": 6},
                            "zhihu_insights": {
                                "word_poverty": {
                                    "template_phrase_count": 1,
                                    "template_phrase_density_per_1k": 1.0,
                                    "most_frequent_templates": ["不禁(1次)"],
                                    "severity": "偶见模板词",
                                },
                            },
                        },
                        "pacing_analysis": {"pacing_type": "transition"},
                        "information_density": {"density_score": "medium"},
                        "narrative_structure": {"structural_function_tag": "transition"},
                        "outline_architecture": {"causal_chain": {"causal_strength": "自然发展"}},
                        "foreshadowing_engineering": {"new_foreshadowing": [{"description": "旧伏笔"}]},
                        "semantic_layers": {"literal_meaning": "旧片段事实", "author_intent": "旧片段意图"},
                        "reader_experience": {
                            "immediate_emotion": {"emotion": "期待", "intensity": 6, "trigger": "旧片段推进"},
                            "immersion_anchor": "旧片段主角目标",
                            "anticipation": {"expected": "旧线索继续推进", "intensity": 6, "hook_type": "悬念"},
                            "satisfaction_points": [{"type": "解谜", "description": "旧线索给出进展", "intensity": 6}],
                            "frustration_points": [],
                            "engagement_level": "medium",
                            "experience_notes": ["旧片段体验可复用"],
                        },
                        "one_sentence_summary": "旧摘要",
                    }],
                }, f, ensure_ascii=False)

            scanned = []

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, entity_prescan=None):
                scanned.append(chunk)
                return {
                    "chunk_index": chunk_index,
                    "chunk_hash": general_scan._chunk_text_hash(chunk),
                    "plot_events": ["新事件"],
                    "one_sentence_summary": "新摘要",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="changed"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "ROLLING_CONTEXT_ENABLED", False), \
                    mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="incremental"), 0)

            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(scanned, [changed_text])
            self.assertTrue(data["incremental_reuse"])
            self.assertTrue(data["writing_quality_enabled"])
            self.assertTrue(data["narrative_architecture_enabled"])
            self.assertTrue(data["foreshadowing_engineering_enabled"])
            self.assertTrue(data["semantic_layers_enabled"])
            self.assertTrue(data["reader_experience_enabled"])
            self.assertTrue(data["entity_prescan_enabled"])
            self.assertEqual(data["entity_prescan_schema_version"], general_scan.ENTITY_PRESCAN_SCHEMA_VERSION)
            self.assertEqual(data["reused_chunk_count"], 1)
            self.assertEqual(data["scanned_chunk_count"], 1)
            self.assertTrue(data["chunk_results"][0]["reused_from_previous"])
            self.assertEqual(data["chunk_results"][0]["plot_events"], ["旧事件"])
            self.assertEqual(data["chunk_results"][1]["plot_events"], ["新事件"])

    def test_general_scan_main_disables_chunk_reuse_with_rolling_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "rolling_incremental.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("changed")

            reused_text = "第一段旧内容。"
            changed_text = "第二段新增内容。"
            reused_hash = general_scan._chunk_text_hash(reused_text)
            manifest = {
                "text_length": 20_000,
                "chunks": [
                    {"chunk_index": 1, "text": reused_text},
                    {"chunk_index": 2, "text": changed_text},
                ],
            }
            latest_path = os.path.join(results_dir, "rolling_incremental_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "schema_version": 1,
                    "analysis_profile": "general",
                    "specialty_profile": "general",
                    "chunk_size": general_scan.CHUNK_SIZE,
                    "chunk_overlap": general_scan.CHUNK_OVERLAP,
                    "smart_density": general_scan.SMART_DENSITY,
                    "rolling_context_enabled": True,
                    "rolling_context_schema_version": general_scan.ROLLING_CONTEXT_SCHEMA_VERSION,
                    "rolling_context_max_chars": general_scan.CONTEXT_MAX_CHARS,
                    "foreshadowing_engineering_enabled": general_scan.FORESHADOWING_ENGINEERING_ENABLED,
                    "semantic_layers_enabled": general_scan.SEMANTIC_LAYERS_ENABLED,
                    "reader_experience_enabled": general_scan.READER_EXPERIENCE_ENABLED,
                    "prompt_templates": {
                        "general_scan_chunk": {"name": "general_scan_chunk", "version": "v1"},
                        "general_summary": {"name": "general_summary", "version": "v1"},
                    },
                    "chunk_results": [{
                        "chunk_index": 0,
                        "chunk_hash": reused_hash,
                        "plot_events": ["旧事件"],
                        "writing_quality": {"prose_quality": {"score": 6}},
                        "pacing_analysis": {"pacing_type": "transition"},
                        "information_density": {"density_score": "medium"},
                        "narrative_structure": {"structural_function_tag": "transition"},
                        "outline_architecture": {"causal_chain": {"causal_strength": "自然发展"}},
                        "foreshadowing_engineering": {"new_foreshadowing": [{"description": "旧伏笔"}]},
                        "semantic_layers": {"literal_meaning": "旧片段事实", "author_intent": "旧片段意图"},
                        "reader_experience": {
                            "immediate_emotion": {"emotion": "期待", "intensity": 6, "trigger": "旧片段推进"},
                            "immersion_anchor": "旧片段主角目标",
                            "anticipation": {"expected": "旧线索继续推进", "intensity": 6, "hook_type": "悬念"},
                            "satisfaction_points": [{"type": "解谜", "description": "旧线索给出进展", "intensity": 6}],
                            "frustration_points": [],
                            "engagement_level": "medium",
                            "experience_notes": ["旧片段体验可复用"],
                        },
                        "context_state_update": {"progress_summary": "旧摘要"},
                        "one_sentence_summary": "旧摘要",
                    }],
                }, f, ensure_ascii=False)

            scanned = []

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, entity_prescan=None):
                scanned.append(chunk)
                return {
                    "chunk_index": chunk_index,
                    "chunk_hash": general_scan._chunk_text_hash(chunk),
                    "plot_events": [f"新事件{len(scanned)}"],
                    "context_state_update": {"progress_summary": f"新摘要{len(scanned)}"},
                    "one_sentence_summary": f"新摘要{len(scanned)}",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="changed"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="rolling_incremental"), 0)

            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(scanned, [reused_text, changed_text])
            self.assertTrue(data["rolling_context_enabled"])
            self.assertEqual(data["reused_chunk_count"], 0)
            self.assertEqual(data["scanned_chunk_count"], 2)
            self.assertFalse(any(item.get("reused_from_previous") for item in data["chunk_results"]))
            self.assertEqual(data["rolling_context_timeline_count"], 2)

    def test_general_scan_does_not_reuse_chunks_without_writing_quality_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "incremental_writing.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("changed")

            reused_text = "第一段旧内容。"
            reused_hash = general_scan._chunk_text_hash(reused_text)
            manifest = {
                "text_length": 20_000,
                "chunks": [{"chunk_index": 1, "text": reused_text}],
            }
            latest_path = os.path.join(results_dir, "incremental_writing_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump({
                    "schema_version": 1,
                    "analysis_profile": "general",
                    "specialty_profile": "general",
                    "chunk_size": general_scan.CHUNK_SIZE,
                    "chunk_overlap": general_scan.CHUNK_OVERLAP,
                    "smart_density": general_scan.SMART_DENSITY,
                    "prompt_templates": {
                        "general_scan_chunk": {"name": "general_scan_chunk", "version": "v1"},
                        "general_summary": {"name": "general_summary", "version": "v1"},
                    },
                    "chunk_results": [{
                        "chunk_index": 0,
                        "chunk_hash": reused_hash,
                        "plot_events": ["旧事件"],
                        "one_sentence_summary": "旧摘要",
                    }],
                }, f, ensure_ascii=False)

            scanned = []

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, entity_prescan=None):
                scanned.append(chunk)
                return {
                    "chunk_index": chunk_index,
                    "chunk_hash": general_scan._chunk_text_hash(chunk),
                    "plot_events": ["新事件"],
                    "one_sentence_summary": "新摘要",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="changed"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="incremental_writing"), 0)

            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(scanned, [reused_text])
            self.assertEqual(data["reused_chunk_count"], 0)
            self.assertEqual(data["scanned_chunk_count"], 1)
            self.assertEqual(data["chunk_results"][0]["plot_events"], ["新事件"])

    def test_general_scan_main_records_context_overflow_partial_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "overflow.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("stub")

            manifest = {
                "text_length": 20_000,
                "chunks": [{"chunk_index": 1, "text": "甲" * 100}],
            }
            calls = []

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, entity_prescan=None):
                calls.append(chunk)
                if len(calls) == 1:
                    raise RuntimeError("context_length_exceeded")
                return {
                    "chunk_index": chunk_index,
                    "plot_events": [f"半段{len(calls) - 1}"],
                    "one_sentence_summary": f"半段摘要{len(calls) - 1}",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="stub"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="overflow"), 0)

            latest_path = os.path.join(results_dir, "overflow_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(data["failed_chunks"], [])
            self.assertEqual(len(calls), 3)
            result = data["chunk_results"][0]
            self.assertTrue(result["partial_result"])
            self.assertEqual(result["partial_reason"], "context_overflow_split")
            self.assertEqual(result["partial_count"], 2)
            self.assertEqual(result["original_chunk_index"], 1)

    def test_general_scan_downshifts_api_504_by_splitting_chunk(self):
        calls = []

        def fake_scan(text_chunk, chunk_index, total_chunks, profile=None, density_profile=None, context_snapshot=None, entity_prescan=None):
            calls.append({
                "text": text_chunk,
                "context": context_snapshot,
                "entity_prescan_count": len(entity_prescan or []),
            })
            if len(calls) == 1:
                raise RuntimeError("服务器错误(504)")
            return {
                "chunk_index": chunk_index,
                "plot_events": [f"事件{len(calls)}"],
                "context_state_update": {"active_characters": ["沈南歌", "那女子"]},
                "one_sentence_summary": f"摘要{len(calls)}",
            }

        old_depth = general_scan.API_DOWNSHIFT_MAX_DEPTH
        old_context = general_scan.CONTEXT_MAX_CHARS
        try:
            general_scan.API_DOWNSHIFT_MAX_DEPTH = 1
            general_scan.CONTEXT_MAX_CHARS = 1000
            with mock.patch.object(general_scan, "_scan_chunk", side_effect=fake_scan):
                result = general_scan._scan_chunk_with_context_overflow_fallback(
                    "甲" * 100 + "\n" + "乙" * 100,
                    0,
                    1,
                    context_snapshot={"active_characters": ["沈南歌"] * 40},
                    entity_prescan=[{"name": f"候选{i}"} for i in range(30)],
                )
        finally:
            general_scan.API_DOWNSHIFT_MAX_DEPTH = old_depth
            general_scan.CONTEXT_MAX_CHARS = old_context

        self.assertEqual(len(calls), 3)
        self.assertTrue(result["partial_result"])
        self.assertEqual(result["partial_reason"], "api_error_downshift_split")
        self.assertEqual(result["partial_count"], 2)
        self.assertIn("chunk_facts", result)
        self.assertEqual([item["name"] for item in result["chunk_facts"]["characters"]], ["沈南歌"])
        self.assertTrue(result["discarded_facts"])

    def test_general_scan_main_marks_partial_scan_when_chunks_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "partial.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("stub")

            manifest = {
                "text_length": 30_000,
                "chunks": [
                    {"chunk_index": 1, "text": "chunk-1"},
                    {"chunk_index": 2, "text": "chunk-2"},
                    {"chunk_index": 3, "text": "chunk-3"},
                ],
            }

            def fake_scan(chunk, chunk_index, total_chunks, profile=None, context_snapshot=None, entity_prescan=None):
                if chunk_index == 1:
                    raise RuntimeError("模型超时")
                return {
                    "chunk_index": chunk_index,
                    "plot_events": [f"事件{chunk_index}"],
                    "one_sentence_summary": f"摘要{chunk_index}",
                }

            with mock.patch.object(general_scan, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(general_scan, "init_token_tracker"), \
                    mock.patch.object(general_scan, "_read_novel", return_value="stub"), \
                    mock.patch.object(general_scan, "build_chunk_manifest", return_value=manifest), \
                    mock.patch.object(general_scan, "save_chunk_manifest"), \
                    mock.patch.object(general_scan, "tqdm", side_effect=lambda items, desc=None: items), \
                    mock.patch.object(general_scan, "_scan_chunk_with_context_overflow_fallback", side_effect=fake_scan), \
                    mock.patch.object(general_scan, "_summarize_book", return_value={"story_overview": "ok"}):
                self.assertEqual(general_scan.main(novel_path=novel_path, book_name="partial"), 0)

            latest_path = os.path.join(results_dir, "partial_GENERAL_SUMMARY_latest.json")
            with open(latest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertTrue(data["partial_scan"])
            self.assertEqual(data["failed_chunk_count"], 1)
            self.assertEqual(data["attempted_chunk_count"], 3)
            self.assertEqual(data["successful_chunk_count"], 2)
            self.assertAlmostEqual(data["failed_chunk_ratio"], 1 / 3, places=5)
            self.assertAlmostEqual(data["scan_coverage_ratio"], 2 / 3, places=5)
            self.assertEqual(data["failed_chunks"][0]["chunk_index"], 1)
            self.assertIn("模型超时", data["failed_chunks"][0]["error"])
            self.assertEqual(data["failed_chunks"][0]["error_type"], "timeout")

    def test_process_single_novel_returns_general_partial_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novels_dir = os.path.join(tmpdir, "novels")
            results_dir = os.path.join(tmpdir, "results")
            os.makedirs(novels_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)
            novel_path = os.path.join(novels_dir, "web_partial.txt")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("stub")

            summary_path = os.path.join(results_dir, "web_partial_GENERAL_SUMMARY_latest.json")

            def fake_general_main(novel_path=None, book_name=None, run_id=None, detail_path=None, profile_override=None):
                with open(summary_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "partial_scan": True,
                        "failed_chunk_count": 2,
                        "attempted_chunk_count": 5,
                        "failed_chunk_ratio": 0.4,
                        "scan_coverage_ratio": 0.6,
                        "failed_chunks": [{"chunk_index": 1, "error": "timeout"}],
                    }, f, ensure_ascii=False)
                return 0

            with mock.patch.object(main, "get_base_dir", return_value=tmpdir), \
                    mock.patch.object(main, "load_configs"), \
                    mock.patch.object(main, "_report_is_fresh", return_value=(False, None)), \
                    mock.patch.object(protagonist, "main", return_value=0), \
                    mock.patch.object(protagonist, "get_latest_report_files", return_value={"detailed": "detail.json"}), \
                    mock.patch.object(general_scan, "main", side_effect=fake_general_main), \
                    mock.patch.object(report, "main", return_value=0):
                result = main.process_single_novel(novel_path, profile_name="general", run_id="run", skip_fresh=False)

            self.assertEqual(result["status"], "ok")
            self.assertIn("通用扫描部分失败：2/5 个片段失败", result["warnings"])
            self.assertTrue(result["general_scan_partial"]["partial_scan"])
            self.assertEqual(result["general_scan_partial"]["failed_chunk_count"], 2)
            self.assertEqual(result["general_scan_partial"]["summary_path"], summary_path)

    def test_frontend_book_detail_shows_general_partial_warning(self):
        with open(os.path.join("frontend", "src", "components", "BookDetail.vue"), "r", encoding="utf-8") as f:
            text = f.read()

        self.assertIn("taskWarnings", text)
        self.assertIn("general_scan_partial", text)
        self.assertIn("scan_coverage_ratio", text)
        self.assertIn("查看 summary", text)

    def test_reviewer_rejects_raw_data_for_different_novel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            novel_path = os.path.join(tmpdir, "当前书.txt")
            raw_path = os.path.join(tmpdir, "raw_data.json")
            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("current")
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump({
                    "book_name": "其他书",
                    "novel_path": novel_path,
                    "novel_signature": novel_reviewer._novel_file_signature(novel_path),
                }, f, ensure_ascii=False)

            with open(raw_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            with self.assertRaisesRegex(ValueError, "书名不匹配"):
                novel_reviewer._validate_raw_data_matches_novel(raw_data, raw_path, novel_path, "当前书")

    def test_general_scan_fresh_summary(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("test")
            novel_path = f.name
        try:
            data = {
                "schema_version": 1,
                "analysis_profile": "general",
                "specialty_profile": "history",
                "novel_path": novel_path,
                "novel_mtime": os.path.getmtime(novel_path),
                "novel_signature": general_scan._novel_file_signature(novel_path),
                "chunk_size": general_scan.CHUNK_SIZE,
                "chunk_overlap": general_scan.CHUNK_OVERLAP,
                "max_chunks": general_scan.MAX_CHUNKS,
                "chunk_sampling_strategy": "full",
                "content_aware_sampling": general_scan.CONTENT_AWARE_SAMPLING,
                "content_aware_sampling_schema_version": general_scan.CONTENT_AWARE_SAMPLING_SCHEMA_VERSION,
                "writing_quality_enabled": general_scan.WRITING_QUALITY_ENABLED,
                "zhihu_writing_insights_schema_version": general_scan.ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION,
                "narrative_architecture_enabled": general_scan.NARRATIVE_ARCHITECTURE_ENABLED,
                "rolling_context_enabled": general_scan.ROLLING_CONTEXT_ENABLED,
                "rolling_context_schema_version": general_scan.ROLLING_CONTEXT_SCHEMA_VERSION,
                "rolling_context_max_chars": general_scan.CONTEXT_MAX_CHARS,
                "foreshadowing_engineering_enabled": general_scan.FORESHADOWING_ENGINEERING_ENABLED,
                "foreshadowing_engineering_schema_version": general_scan.FORESHADOWING_ENGINEERING_SCHEMA_VERSION,
                "semantic_layers_enabled": general_scan.SEMANTIC_LAYERS_ENABLED,
                "semantic_layers_schema_version": general_scan.SEMANTIC_LAYERS_SCHEMA_VERSION,
                "reader_experience_enabled": general_scan.READER_EXPERIENCE_ENABLED,
                "reader_experience_schema_version": general_scan.READER_EXPERIENCE_SCHEMA_VERSION,
                "continuity_audit_enabled": general_scan.CONTINUITY_AUDIT_ENABLED,
                "continuity_audit_schema_version": general_scan.CONTINUITY_AUDIT_SCHEMA_VERSION,
                "entity_prescan_enabled": general_scan.ENTITY_PRESCAN_ENABLED,
                "entity_prescan_schema_version": general_scan.ENTITY_PRESCAN_SCHEMA_VERSION,
                "knowledge_base_enabled": True,
                "knowledge_base_schema_version": general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION,
                "knowledge_base_llm_merge_enabled": general_scan.KNOWLEDGE_BASE_LLM_MERGE_ENABLED,
                "summary": {"story_overview": "ok"},
                "chunk_results": [],
            }
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "history"))
            data_partial = dict(data)
            data_partial["partial_scan"] = True
            self.assertFalse(general_scan._is_fresh_summary(data_partial, novel_path, "history"))
            data_failed_count = dict(data)
            data_failed_count["failed_chunk_count"] = 1
            self.assertFalse(general_scan._is_fresh_summary(data_failed_count, novel_path, "history"))
            data_low_coverage = dict(data)
            data_low_coverage["scan_coverage_ratio"] = 0.8
            self.assertFalse(general_scan._is_fresh_summary(data_low_coverage, novel_path, "history"))
            data_without_writing_meta = dict(data)
            data_without_writing_meta.pop("writing_quality_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_writing_meta, novel_path, "history"))
            data_without_zhihu_meta = dict(data)
            data_without_zhihu_meta.pop("zhihu_writing_insights_schema_version", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_zhihu_meta, novel_path, "history"))
            data_without_narrative_meta = dict(data)
            data_without_narrative_meta.pop("narrative_architecture_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_narrative_meta, novel_path, "history"))
            data_without_rolling_meta = dict(data)
            data_without_rolling_meta.pop("rolling_context_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_rolling_meta, novel_path, "history"))
            data_without_foreshadowing_meta = dict(data)
            data_without_foreshadowing_meta.pop("foreshadowing_engineering_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_foreshadowing_meta, novel_path, "history"))
            data_without_semantic_meta = dict(data)
            data_without_semantic_meta.pop("semantic_layers_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_semantic_meta, novel_path, "history"))
            data_without_reader_experience_meta = dict(data)
            data_without_reader_experience_meta.pop("reader_experience_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_reader_experience_meta, novel_path, "history"))
            data_without_continuity_meta = dict(data)
            data_without_continuity_meta.pop("continuity_audit_enabled", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_continuity_meta, novel_path, "history"))
            data_without_entity_prescan_meta = dict(data)
            data_without_entity_prescan_meta.pop("entity_prescan_schema_version", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_entity_prescan_meta, novel_path, "history"))
            data_without_knowledge_base_meta = dict(data)
            data_without_knowledge_base_meta.pop("knowledge_base_schema_version", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_knowledge_base_meta, novel_path, "history"))
            data_wrong_knowledge_base_merge = dict(data)
            data_wrong_knowledge_base_merge["knowledge_base_llm_merge_enabled"] = not general_scan.KNOWLEDGE_BASE_LLM_MERGE_ENABLED
            self.assertFalse(general_scan._is_fresh_summary(data_wrong_knowledge_base_merge, novel_path, "history"))
            data_without_content_sampling_meta = dict(data)
            data_without_content_sampling_meta.pop("content_aware_sampling_schema_version", None)
            self.assertFalse(general_scan._is_fresh_summary(data_without_content_sampling_meta, novel_path, "history"))
            data["summary"] = {"book_overview": "ok"}
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "history"))
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "general"))
            data["max_chunks"] = general_scan.MAX_CHUNKS + 1
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "history"))
            data["max_chunks"] = general_scan._effective_max_chunks(4_000_000)
            data["text_length"] = 4_000_000
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "history"))
            data["prompt_templates"] = {
                "general_scan_chunk": {"name": "general_scan_chunk", "version": "v0"},
                "general_summary": {"name": "general_summary", "version": "v1"},
            }
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "history"))
            data["prompt_templates"]["general_scan_chunk"]["version"] = "v1"
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "history"))
            data["max_chunks"] = general_scan.MAX_CHUNKS
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "history"))
        finally:
            os.unlink(novel_path)

    def test_general_scan_fresh_summary_rejects_same_mtime_changed_content(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("test")
            novel_path = f.name
        try:
            original_stat = os.stat(novel_path)
            data = {
                "schema_version": 1,
                "analysis_profile": "general",
                "specialty_profile": "general",
                "novel_path": novel_path,
                "novel_mtime": os.path.getmtime(novel_path),
                "novel_signature": general_scan._novel_file_signature(novel_path),
                "chunk_size": general_scan.CHUNK_SIZE,
                "chunk_overlap": general_scan.CHUNK_OVERLAP,
                "max_chunks": general_scan.MAX_CHUNKS,
                "chunk_sampling_strategy": "full",
                "content_aware_sampling": general_scan.CONTENT_AWARE_SAMPLING,
                "content_aware_sampling_schema_version": general_scan.CONTENT_AWARE_SAMPLING_SCHEMA_VERSION,
                "writing_quality_enabled": general_scan.WRITING_QUALITY_ENABLED,
                "zhihu_writing_insights_schema_version": general_scan.ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION,
                "narrative_architecture_enabled": general_scan.NARRATIVE_ARCHITECTURE_ENABLED,
                "rolling_context_enabled": general_scan.ROLLING_CONTEXT_ENABLED,
                "rolling_context_schema_version": general_scan.ROLLING_CONTEXT_SCHEMA_VERSION,
                "rolling_context_max_chars": general_scan.CONTEXT_MAX_CHARS,
                "foreshadowing_engineering_enabled": general_scan.FORESHADOWING_ENGINEERING_ENABLED,
                "foreshadowing_engineering_schema_version": general_scan.FORESHADOWING_ENGINEERING_SCHEMA_VERSION,
                "semantic_layers_enabled": general_scan.SEMANTIC_LAYERS_ENABLED,
                "semantic_layers_schema_version": general_scan.SEMANTIC_LAYERS_SCHEMA_VERSION,
                "reader_experience_enabled": general_scan.READER_EXPERIENCE_ENABLED,
                "reader_experience_schema_version": general_scan.READER_EXPERIENCE_SCHEMA_VERSION,
                "continuity_audit_enabled": general_scan.CONTINUITY_AUDIT_ENABLED,
                "continuity_audit_schema_version": general_scan.CONTINUITY_AUDIT_SCHEMA_VERSION,
                "entity_prescan_enabled": general_scan.ENTITY_PRESCAN_ENABLED,
                "entity_prescan_schema_version": general_scan.ENTITY_PRESCAN_SCHEMA_VERSION,
                "knowledge_base_enabled": True,
                "knowledge_base_schema_version": general_scan.KNOWLEDGE_BASE_SCHEMA_VERSION,
                "knowledge_base_llm_merge_enabled": general_scan.KNOWLEDGE_BASE_LLM_MERGE_ENABLED,
                "summary": {"story_overview": "ok"},
                "chunk_results": [],
            }
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "general"))

            with open(novel_path, "w", encoding="utf-8") as f:
                f.write("best")
            os.utime(novel_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

            self.assertEqual(data["novel_mtime"], os.path.getmtime(novel_path))
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "general"))
        finally:
            os.unlink(novel_path)


if __name__ == "__main__":
    unittest.main()

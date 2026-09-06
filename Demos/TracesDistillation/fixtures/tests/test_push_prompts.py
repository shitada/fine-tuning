import copy
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid


FIXTURES_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FIXTURES_DIR))
import push_prompts as traffic  # noqa: E402


SECRET = "DO_NOT_PERSIST_SECRET_or_customer@example.invalid"
TARGET = {
    "agent_name": "zava-traces-demo", "agent_version": "1",
    "project_endpoint": "https://stada-2448-resource.services.ai.azure.com/api/projects/stada-2448",
}


def response(*, usage=True, text="条件を確認しました。返品は対象外です。"):
    value = {
        "id": "resp_offline_123", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text},
        ]}],
    }
    if usage:
        value["usage"] = {"input_tokens": 123, "output_tokens": 45}
    return value


def raw_json(value=None, status=200):
    return (f"HTTP/1.1 {status} status\r\nAuthorization: Bearer {SECRET}\r\n"
            f"X-User-Identity: {SECRET}\r\nContent-Type: application/json\r\n\r\n"
            + json.dumps(response() if value is None else value, ensure_ascii=False))


def raw_sse(value=None, event="response.completed"):
    body = {"type": event, "response": response() if value is None else value}
    return (f"HTTP/2 200 OK\nX-Secret: {SECRET}\nContent-Type: text/event-stream\n\n"
            f"event: {event}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
            "data: [DONE]\n\n")


def successful_result():
    return traffic.parse_raw_response(raw_json(), 0)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.remote_guard = patch.object(
            traffic.subprocess, "run", side_effect=AssertionError("テスト中の外部呼び出しは禁止"))
        self.remote_guard.start()
        self.addCleanup(self.remote_guard.stop)


class ScenarioTests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        cls.rows = traffic.generate_scenarios()

    def test_deterministic_distribution_and_seed(self):
        self.assertEqual(self.rows, traffic.generate_scenarios(42, 170))
        self.assertNotEqual(self.rows, traffic.generate_scenarios(43, 170))
        self.assertEqual(Counter(row["category"] for row in self.rows), traffic.CATEGORY_COUNTS)

    def test_smoke_and_pilot_coverage(self):
        self.assertEqual([row["category"] for row in self.rows[:8]], list(traffic.SMOKE))
        self.assertEqual([row["variant"] for row in self.rows[:8]], [
            "通常返品", "不良交換", "紛失", "遅配", "発送済み", "受付済み", "一部返品", "理由未指定",
        ])
        self.assertEqual({row["category"] for row in self.rows[:8]}, set(traffic.CATEGORY_COUNTS))
        self.assertEqual(
            {row["variant"] for row in self.rows[:8] if row["category"] == "配送問題"},
            {"紛失", "遅配", "発送済み"})
        pilot = Counter(row["category"] for row in self.rows[8:33])
        self.assertEqual(pilot, {
            "返品": 5, "交換": 5, "配送問題": 4, "キャンセル": 3,
            "複数商品": 4, "曖昧・方針質問": 4,
        })
        self.assertEqual(len(self.rows[33:]), 137)
        shipping = Counter(row["variant"] for row in self.rows if row["category"] == "配送問題")
        self.assertEqual(shipping, {"紛失": 10, "遅配": 10, "発送済み": 10})

    def test_actual_order_item_membership_and_shipping(self):
        for row in self.rows:
            with self.subTest(scenario=row["scenario_id"]):
                order = traffic.get_order_details(row["order_id"])
                shipping = traffic.get_fulfillment_status(row["order_id"])
                self.assertEqual(row["fulfillment"], shipping)
                self.assertEqual(row["loyalty_tier"], order["customer"]["loyalty_tier"])
                self.assertIn(row["order_id"], row["prompt"])
                self.assertIn("2026-08-31", row["prompt"])
                self.assertIn(f"「{shipping['status']}」", row["prompt"])
                for item in row["items"]:
                    self.assertIn(item, order["items"])
                    self.assertIn(item["item_id"], row["prompt"])
                    self.assertIn(item["name"], row["prompt"])
                    self.assertIn(item["sku"], row["prompt"])
                if row["category"] in {"返品", "交換", "複数商品"}:
                    self.assertEqual(shipping["status"], "配達済み")
                if row["category"] == "複数商品":
                    self.assertEqual(len(row["items"]), 2)
                if row["category"] == "キャンセル":
                    self.assertEqual(row["items"], order["items"])
                    self.assertEqual(row["variant"], shipping["status"])
                if row["category"] == "配送問題":
                    if row["variant"] == "遅配":
                        self.assertTrue(shipping["late_delivery"])
                        self.assertIn(shipping["delivery_date"], row["prompt"])
                        self.assertIn(shipping["promised_date"], row["prompt"])
                        self.assertIn(f"{shipping['days_late']}日", row["prompt"])
                    else:
                        self.assertEqual(row["variant"], shipping["status"])

    def test_exchanges_use_real_distinct_inventory(self):
        replacements = [target for row in self.rows for target in row["replacements"]]
        self.assertTrue(any(not target["available"] for target in replacements))
        for row in self.rows:
            for target in row["replacements"]:
                source = next(item for item in row["items"] if item["item_id"] == target["item_id"])
                inventory = traffic.check_inventory(target["sku"])
                self.assertNotEqual(source["sku"], target["sku"])
                self.assertEqual(target["name"], inventory["product_name"])
                self.assertEqual(target["available"], inventory["available"])
                self.assertIn(target["sku"], row["prompt"])

    def test_varied_policy_outcomes_without_answer_leakage(self):
        policies = []
        for row in self.rows:
            self.assertNotIn("eligible_actions", row["prompt"])
            self.assertNotIn("policy_id", row["prompt"])
            self.assertNotIn("calculate_resolution", row["prompt"])
            self.assertNotIn("1000", row["prompt"])
            for report in row["reported_reasons"]:
                policies.append(traffic.check_resolution_policy(
                    row["order_id"], report["item_id"], report["reason"]))
            if row["variant"] == "開封済み":
                self.assertFalse(row["items"][0]["sealed"])
            if row["variant"] in {"最終処分品", "最終処分品の不良"}:
                self.assertTrue(row["items"][0]["final_sale"])
        self.assertTrue(any(policy["eligible_actions"] == ["対象外"] for policy in policies))
        self.assertTrue(any("ストアクレジット" in policy["eligible_actions"] for policy in policies))
        self.assertTrue(any("配送遅延クレジット" in policy["eligible_actions"] for policy in policies))
        self.assertTrue(any(policy["restocking_fee_rate"] > 0 for policy in policies))

    def test_duplicate_and_integrity_validation(self):
        self.assertEqual(len({row["order_id"] for row in self.rows}), 170)
        self.assertEqual(len({row["prompt"] for row in self.rows}), 170)
        self.assertTrue(traffic.validate_scenarios(self.rows)["order_item_integrity"])
        for field, value in (("items", []), ("fulfillment", {})):
            broken = copy.deepcopy(self.rows)
            broken[0][field] = value
            with self.subTest(field=field), self.assertRaises(traffic.TrafficError):
                traffic.validate_scenarios(broken)
        broken = copy.deepcopy(self.rows)
        broken[1]["order_id"] = broken[0]["order_id"]
        with self.assertRaises(traffic.TrafficError):
            traffic.validate_scenarios(broken)

    def test_finite_pool_exhaustion_and_count_bounds(self):
        with self.assertRaisesRegex(traffic.TrafficError, "有限候補"):
            traffic.generate_scenarios(pool_limit=1)
        for count in (0, 7, 201):
            with self.subTest(count=count), self.assertRaises(traffic.TrafficError):
                traffic.generate_scenarios(count=count)
        self.assertEqual(len(traffic.generate_scenarios(count=8)), 8)
        self.assertEqual(len(traffic.generate_scenarios(count=200)), 200)

    def test_contract_hashes_match_packaged_contract(self):
        hashes = traffic.contract_hashes()
        self.assertEqual(len(hashes), 4)
        for value in hashes.values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")


class ParserTests(OfflineTests):
    def test_json_and_sse_success(self):
        for raw in (raw_json(), raw_sse()):
            with self.subTest(raw_type=raw[:10]):
                result = traffic.parse_raw_response(raw, 0)
                self.assertTrue(result["success"])
                self.assertEqual(result["response_id"], "resp_offline_123")
                self.assertEqual(result["http_status"], 200)
                self.assertEqual(result["input_tokens"], 123)
                self.assertEqual(result["output_tokens"], 45)
                self.assertTrue(result["japanese_script"])
                self.assertFalse(result["unapproved_english"])
                self.assertTrue(result["business_refusal_hint"])
                self.assertIsNone(result["business_outcome"])

    def test_status_line_variants(self):
        for status in ("HTTP 200 OK", "HTTP/1.1 200 OK", "HTTP/2.0 200 OK", "Status: 200 OK"):
            raw = status + "\nContent-Type: application/json\n\n" + json.dumps(response())
            with self.subTest(status=status):
                self.assertTrue(traffic.parse_raw_response(raw, 0)["success"])

    def test_hosted_response_id_is_retained(self):
        value = response()
        value["id"] = "caresp_022c3ba73baebde400karAVms7148c7OumhjUYrPyPXEWyeP9c"
        result = traffic.parse_raw_response(raw_json(value), 0)
        self.assertTrue(result["success"])
        self.assertEqual(result["response_id"], value["id"])

    def test_command_diagnostics_are_redacted_and_preserve_status(self):
        result = traffic.parse_raw_response(
            "", 1, f"ERROR: HTTP 429 Too Many Requests; Authorization: {SECRET}")
        self.assertFalse(result["success"])
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["diagnostic_code"], "rate_limit")
        self.assertNotIn(SECRET, json.dumps(result))
        unknown = traffic.parse_raw_response("", 1, SECRET)
        self.assertEqual(unknown["diagnostic_code"], "unclassified_command_failure")
        self.assertIsNone(unknown["http_status"])
        self.assertNotIn(SECRET, json.dumps(unknown))
        authentication = traffic.parse_raw_response(
            f"ERROR: AzureDeveloperCLICredential: exit status 1 {SECRET}", 1)
        self.assertEqual(authentication["diagnostic_code"], "authentication")
        self.assertNotIn(SECRET, json.dumps(authentication))
        rpc = traffic.parse_raw_response(
            f"ERROR: rpc error: code = DeadlineExceeded desc = {SECRET}", 1)
        self.assertEqual(rpc["diagnostic_code"], "rpc_timeout")
        self.assertNotIn(SECRET, json.dumps(rpc))

    def test_failed_error_incomplete_and_nonzero_are_not_success(self):
        cases = [
            (raw_json(status=429), 0, "HTTPエラー"),
            (raw_json(status=503), 0, "HTTPエラー"),
            (raw_json(), 1, "コマンド失敗"),
            (raw_json({"status": "failed", "error": {"message": SECRET}}), 0, "応答エラー"),
            (raw_sse(event="response.failed"), 0, "応答失敗"),
            (raw_sse(event="response.incomplete"), 0, "応答未完了"),
            (raw_sse(event="error"), 0, "応答エラー"),
            (raw_json({"status": "in_progress"}), 0, "応答未完了"),
            (raw_json({"status": "incomplete"}), 0, "応答未完了"),
            (raw_json({"status": "failed"}), 0, "応答失敗"),
            ("HTTP/1.1 200 OK\n\n{}", 0, "応答未完了"),
            ("HTTP/1.1 200 OK\n\n{\"broken\":", 0, "JSON不正"),
            ("HTTP/1.1 200 OK\n\ndata: broken\n\n", 0, "JSON不正"),
            ("HTTP/1.1 200 OK\n\ndata: [DONE]\n\n", 0, "応答未完了"),
            ("HTTP/1.1 200 OK\n", 0, "HTTP形式不正"),
            (SECRET, 1, "コマンド失敗"),
            (json.dumps(response()), 0, "HTTP形式不正"),
        ]
        for raw, code, kind in cases:
            with self.subTest(kind=kind, code=code):
                result = traffic.parse_raw_response(raw, code)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_kind"], kind)
                self.assertNotIn(SECRET, json.dumps(result))

    def test_truncated_sse_missing_status_and_event_mismatch(self):
        incomplete = response()
        incomplete.pop("status")
        for raw in (
            raw_sse(incomplete),
            raw_sse().replace("event: response.completed", "event: response.failed"),
            raw_sse() + "event: error\ndata: {\"type\":\"error\"}\n\n",
            raw_sse() + "event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
            "HTTP/1.1 200 OK\n\nevent: response.completed\ndata: []\n\n",
        ):
            self.assertFalse(traffic.parse_raw_response(raw, 0)["success"])

    def test_missing_and_invalid_usage_remains_null(self):
        cases = [response(usage=False)]
        for usage in (None, {}, {"input_tokens": True, "output_tokens": -1},
                      {"input_tokens": "123", "output_tokens": 4.5}):
            cases.append({**response(), "usage": usage})
        for value in cases:
            with self.subTest(usage=value.get("usage")):
                result = traffic.parse_raw_response(raw_json(value), 0)
                self.assertTrue(result["success"])
                self.assertIsNone(result["input_tokens"])
                self.assertIsNone(result["output_tokens"])
        valid_zero = response()
        valid_zero["usage"] = {"input_tokens": 0}
        result = traffic.parse_raw_response(raw_json(valid_zero), 0)
        self.assertEqual(result["input_tokens"], 0)
        self.assertIsNone(result["output_tokens"])

    def test_redaction_of_headers_response_and_invalid_id(self):
        value = response(text=f"内容 {SECRET}")
        value["id"] = SECRET
        value["metadata"] = {"credential": SECRET}
        result = traffic.parse_raw_response(raw_json(value), 0)
        self.assertTrue(result["success"])
        self.assertTrue(result["unapproved_english"])
        self.assertIsNone(result["response_id"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("Authorization", json.dumps(result))
        self.assertNotIn("output", result)

    def test_language_allowlist_and_unknown_response(self):
        allowed = ("Zavaの注文ORD-0001、ITEM-0001-1、SKU-002-1、JPY。"
                   "check_inventoryとcalculate_resolutionのpolicy_idはPOLICY-ABCDEF012345。"
                   "受付IDはCASE-12D28A0F46です。"
                   "eligible_actions、restocking_fee_rate、total_refund_jpyも確認しました。")
        result = traffic.parse_raw_response(raw_json(response(text=allowed)), 0)
        self.assertFalse(result["unapproved_english"])
        result = traffic.parse_raw_response(
            raw_json(response(text="日本語です。\ufffd Your refund is approved.")), 0)
        self.assertTrue(result["replacement_character"])
        self.assertTrue(result["unapproved_english"])
        result = traffic.parse_raw_response(raw_json(response(text="")), 0)
        self.assertIsNone(result["japanese_script"])
        self.assertIsNone(result["unapproved_english"])

    def test_english_in_backticks_or_unknown_code_is_flagged_not_failed(self):
        result = traffic.parse_raw_response(
            raw_json(response(text="日本語の説明です。`Your refund is approved`。`unknown_code_token`。")), 0)
        self.assertTrue(result["success"])
        self.assertTrue(result["japanese_script"])
        self.assertTrue(result["unapproved_english"])
        self.assertIsNone(result["business_outcome"])

    def test_only_assistant_language_is_examined(self):
        value = response()
        value["output"].append({"role": "user", "content": [{"type": "text", "text": SECRET}]})
        value["output"].append({"type": "function_call", "arguments": SECRET})
        result = traffic.parse_raw_response(raw_json(value), 0)
        self.assertFalse(result["unapproved_english"])
        self.assertEqual(result["response_visible_tool_call_count"], 1)


class InvocationTests(OfflineTests):
    def test_invocation_is_serial_pinned_and_isolated_with_ephemeral_env(self):
        original = os.environ.get("AZURE_DEV_USER_AGENT")
        with patch.object(traffic.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, raw_json(), SECRET)
            result = traffic.invoke({"prompt": "日本語の問い合わせ"}, "zava-traces-demo", "1", 300)
        self.assertTrue(result["success"])
        command = run.call_args.args[0]
        self.assertEqual(command[:7], [
            "azd", "ai", "agent", "invoke", "日本語の問い合わせ",
            "--agent-endpoint",
            TARGET["project_endpoint"] + "/agents/zava-traces-demo/endpoint/protocols/openai/responses?api-version=v1"])
        self.assertEqual(command[7:], [
            "--version", "1", "--new-conversation", "--output", "raw",
            "--timeout", "300", "--no-prompt"])
        self.assertNotIn("--new-session", command)
        self.assertNotIn("--protocol", command)
        self.assertNotIn("--session-id", command)
        options = run.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertTrue(options["capture_output"])
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "strict")
        self.assertEqual(options["cwd"], traffic.AGENT_ROOT)
        self.assertEqual(options["timeout"], 300 + traffic.AZD_SETUP_TIMEOUT_SECONDS)
        self.assertEqual(options["env"]["AZURE_DEV_USER_AGENT"], "microsoft_foundry_skill")
        self.assertEqual(os.environ.get("AZURE_DEV_USER_AGENT"), original)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invoke_uses_the_approved_endpoint_not_later_config_changes(self):
        with patch.object(traffic, "configured_endpoint", side_effect=AssertionError("再解決は禁止")):
            with patch.object(traffic.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, raw_json(), "")
                result = traffic.invoke(
                    {"prompt": "問い合わせ"}, "zava-traces-demo", "1", 300,
                    project_endpoint=TARGET["project_endpoint"],
                )
        self.assertTrue(result["success"])
        endpoint = run.call_args.args[0][run.call_args.args[0].index("--agent-endpoint") + 1]
        self.assertTrue(endpoint.startswith(TARGET["project_endpoint"] + "/agents/"))

    def test_timeout_interrupt_oserror_do_not_retry_or_leak(self):
        for failure, kind in (
            (subprocess.TimeoutExpired(SECRET, 300, output=SECRET, stderr=SECRET), "タイムアウト・結果不明"),
            (KeyboardInterrupt(), "中断・結果不明"),
            (OSError(SECRET), "実行失敗・結果不明"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, SECRET), "UTF-8不正・結果不明"),
        ):
            with self.subTest(kind=kind), patch.object(traffic.subprocess, "run", side_effect=failure) as run:
                result = traffic.invoke({"prompt": "問い合わせ"}, "zava-traces-demo", "1", 300)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_kind"], kind)
                run.assert_called_once()
                self.assertNotIn(SECRET, json.dumps(result))

    def test_endpoint_validation(self):
        self.assertEqual(traffic.configured_endpoint(), TARGET["project_endpoint"])
        self.assertEqual(traffic.normalize_endpoint(TARGET["project_endpoint"] + "/"), TARGET["project_endpoint"])
        for endpoint in (
            "http://example.invalid/api/projects/test",
            "https://user:password@example.invalid/api/projects/test",
            "https://example.invalid/api/projects/test?key=secret",
            "https://example.invalid/api/projects/test#fragment",
            "https://example.invalid/api/projects/test/other",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(traffic.TrafficError):
                traffic.normalize_endpoint(endpoint)


class AzdProfileTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.directory = Path(__file__).resolve().parent / (".traffic-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.config = self.directory / "client.json"
        self.config_patch = patch.object(traffic, "LOCAL_AZD_CONFIG", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        inherited = {key: value for key, value in os.environ.items() if key != "AZD_CONFIG_DIR"}
        self.environment_patch = patch.dict(os.environ, inherited, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def make_profile(self, name):
        profile = self.directory / name
        profile.mkdir()
        (profile / "config.json").write_text("{}", encoding="utf-8")
        return profile

    def test_missing_local_setting_preserves_default_profile(self):
        env = traffic.azd_environment()
        self.assertNotIn("AZD_CONFIG_DIR", env)
        self.assertEqual(env["AZURE_DEV_USER_AGENT"], "microsoft_foundry_skill")

    def test_local_profile_only_changes_child_environment(self):
        profile = self.make_profile("isolated")
        self.config.write_text(json.dumps({"config_dir": str(profile)}), encoding="utf-8")
        env = traffic.azd_environment()
        self.assertEqual(env["AZD_CONFIG_DIR"], str(profile))
        self.assertNotIn("AZD_CONFIG_DIR", os.environ)

    def test_explicit_environment_takes_precedence(self):
        local = self.make_profile("local")
        explicit = self.make_profile("explicit")
        self.config.write_text(json.dumps({"config_dir": str(local)}), encoding="utf-8")
        with patch.dict(os.environ, {"AZD_CONFIG_DIR": str(explicit)}):
            self.assertEqual(traffic.azd_environment()["AZD_CONFIG_DIR"], str(explicit))

    def test_invalid_configuration_never_falls_back_or_leaks(self):
        for value in (SECRET, "[]", '{"config_dir": 123}', '{"unexpected": true}',
                      '{"config_dir": "relative"}',
                      json.dumps({"config_dir": str(self.directory / "missing")})):
            with self.subTest(value=value):
                self.config.write_text(value, encoding="utf-8")
                with self.assertRaises(traffic.TrafficError) as caught:
                    traffic.azd_environment()
                self.assertNotIn(SECRET, str(caught.exception))

    def test_invoke_passes_selected_profile(self):
        profile = self.make_profile("isolated")
        self.config.write_text(json.dumps({"config_dir": str(profile)}), encoding="utf-8")
        with patch.object(traffic.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, raw_json(), "")
            self.assertTrue(traffic.invoke({"prompt": "接続確認"}, "zava-traces-demo", "1", 300)["success"])
        self.assertEqual(run.call_args.kwargs["env"]["AZD_CONFIG_DIR"], str(profile))

    def test_invalid_profile_stops_before_recording_a_submission(self):
        self.config.write_text('{"config_dir": "missing"}', encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            code = traffic.main([
                "--send", "--agent-name", "zava-traces-demo", "--agent-version", "1",
                "--project-endpoint", TARGET["project_endpoint"], "--num-prompts", "1",
                "--stage", "profile-check", "--output-dir", str(self.directory / "run"),
            ])
        self.assertEqual(code, 2)
        self.assertFalse((self.directory / "run" / "traffic-requests.jsonl").exists())


class PersistenceTests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        cls.rows = traffic.generate_scenarios()

    def setUp(self):
        super().setUp()
        self.run_dir = Path(__file__).resolve().parent / (".traffic-test-" + uuid.uuid4().hex)
        self.run_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.run_dir)
        self.summary = traffic.prepare_manifest(self.run_dir, self.rows, 42)
        traffic.update_summary(self.run_dir, self.rows, self.summary)

    def send(self, *, offset=0, count=8, stage="smoke", target=None):
        with redirect_stdout(io.StringIO()):
            return traffic.send_stage(
                self.run_dir, self.rows, self.summary, offset=offset, count=count,
                stage=stage, target=TARGET if target is None else target, timeout=300)

    def current_summary(self):
        return json.loads((self.run_dir / "traffic-summary.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.run_dir.iterdir() if path.is_file()}

    def test_strict_utf8_manifest_and_replay_no_overwrite(self):
        manifest = (self.run_dir / "traffic-scenarios.jsonl").read_bytes()
        self.assertFalse(manifest.startswith(b"\xef\xbb\xbf"))
        self.assertIn("返品".encode("utf-8"), manifest)
        self.assertNotIn(b"\\u", manifest)
        self.assertEqual(len(manifest.decode("utf-8", errors="strict").splitlines()), 170)
        before = self.snapshot()
        replay = traffic.prepare_manifest(self.run_dir, self.rows, 42)
        traffic.update_summary(self.run_dir, self.rows, replay)
        self.assertEqual(before, self.snapshot())

    def test_different_seed_count_or_contract_refused_without_overwrite(self):
        before = self.snapshot()
        for rows, seed in ((traffic.generate_scenarios(43), 43), (traffic.generate_scenarios(count=169), 42)):
            with self.assertRaises(traffic.TrafficError):
                traffic.prepare_manifest(self.run_dir, rows, seed)
            self.assertEqual(before, self.snapshot())
        hashes = traffic.contract_hashes()
        hashes["synthetic_store.py"] = "changed"
        with patch.object(traffic, "contract_hashes", return_value=hashes):
            with self.assertRaisesRegex(traffic.TrafficError, "hash"):
                traffic.prepare_manifest(self.run_dir, self.rows, 42)
        self.assertEqual(before, self.snapshot())

    def test_manifest_tampering_and_missing_manifest_refused(self):
        path = self.run_dir / "traffic-scenarios.jsonl"
        path.write_bytes(path.read_bytes().replace(b"SCN-0001", b"SCN-9999", 1))
        before = self.snapshot()
        with self.assertRaises(traffic.TrafficError):
            traffic.prepare_manifest(self.run_dir, self.rows, 42)
        self.assertEqual(before, self.snapshot())
        path.unlink()
        with self.assertRaises(traffic.TrafficError):
            traffic.prepare_manifest(self.run_dir, self.rows, 42)
        self.assertFalse(path.exists())

    def test_staged_batches_and_cumulative_summary(self):
        with patch.object(traffic, "invoke", return_value=successful_result()) as invoke:
            self.assertEqual(self.send(), 0)
            self.assertEqual(self.send(offset=8, count=25, stage="pilot"), 0)
            self.assertEqual(self.send(offset=33, count=137, stage="full"), 0)
        self.assertEqual(invoke.call_count, 170)
        self.assertEqual(
            [call.args[0]["scenario_id"] for call in invoke.call_args_list],
            [row["scenario_id"] for row in self.rows])
        summary = self.current_summary()
        self.assertEqual(summary["cumulative"]["success_count"], 170)
        self.assertEqual(summary["cumulative"]["unsubmitted_count"], 0)
        self.assertIsNone(summary["appinsights_cumulative_billing_usage"])
        for stage, count in (("smoke", 8), ("pilot", 25), ("full", 137)):
            self.assertEqual(summary["stages"][stage]["submitted_count"], count)
            self.assertEqual(summary["stages"][stage]["status"], "完了")
        self.assertEqual(summary["cumulative"]["response_level_usage"]["input_tokens"]["known_sum"], 170 * 123)
        self.assertEqual(summary["cumulative"]["language_metrics"]["business_refusal_hint"]["true_count"], 170)
        events = [json.loads(line) for line in (self.run_dir / "traffic-requests.jsonl").read_text(
            encoding="utf-8").splitlines()]
        starts = [event for event in events if event["event"] == "submitted"]
        ends = [event for event in events if event["event"] == "completed"]
        self.assertEqual(len({event["request_id"] for event in starts}), 170)
        self.assertEqual(summary["cumulative"]["start_utc"], starts[0]["started_at_utc"])
        self.assertEqual(summary["cumulative"]["last_success_utc"], ends[-1]["ended_at_utc"])
        for event in ends:
            self.assertIsInstance(event["success"], bool)
            self.assertGreaterEqual(event["duration_seconds"], 0)
            self.assertTrue(event["started_at_utc"].endswith("Z"))
            self.assertTrue(event["ended_at_utc"].endswith("Z"))
        snapshot = self.snapshot()
        replay = traffic.prepare_manifest(self.run_dir, self.rows, 42)
        traffic.update_summary(self.run_dir, self.rows, replay)
        self.assertEqual(snapshot, self.snapshot())

    def test_first_failure_stops_stage_and_overlap_cannot_retry(self):
        failure = traffic.empty_result("HTTPエラー", 429)
        with patch.object(traffic, "invoke", side_effect=[successful_result(), failure]) as invoke:
            self.assertEqual(self.send(), 1)
            self.assertEqual(invoke.call_count, 2)
        cumulative = self.current_summary()["cumulative"]
        self.assertEqual(cumulative["success_count"], 1)
        self.assertEqual(cumulative["failure_count"], 1)
        self.assertEqual(cumulative["unsubmitted_count"], 168)
        self.assertEqual(self.current_summary()["stages"]["smoke"]["unsubmitted_count"], 6)
        for arguments in ({}, {"stage": "retry"}, {"offset": 1, "count": 1, "stage": "retry"}):
            with self.assertRaises(traffic.TrafficError):
                self.send(**arguments)
        with patch.object(traffic, "invoke", return_value=successful_result()) as invoke:
            self.assertEqual(self.send(offset=2, count=6, stage="diagnosed-remainder"), 0)
            self.assertEqual(invoke.call_count, 6)

    def test_cli_failure_without_http_is_a_remote_unknown_outcome(self):
        failure = traffic.parse_raw_response("", 1)
        with patch.object(traffic, "invoke", return_value=failure) as invoke:
            self.assertEqual(self.send(count=1), 1)
            invoke.assert_called_once()
        summary = self.current_summary()["cumulative"]
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["unknown_outcome_count"], 1)

    def test_timeout_unknown_recorded_without_automatic_retry(self):
        with patch.object(traffic, "invoke", return_value=traffic.empty_result("タイムアウト・結果不明")) as invoke:
            self.assertEqual(self.send(), 1)
            invoke.assert_called_once()
        summary = self.current_summary()
        self.assertEqual(summary["cumulative"]["unknown_outcome_count"], 1)
        self.assertEqual(summary["cumulative"]["failure_count"], 1)
        self.assertIsNone(summary["cumulative"]["response_level_usage"]["input_tokens"]["known_sum"])
        with self.assertRaises(traffic.TrafficError):
            self.send(count=1, stage="retry")

    def test_write_ahead_survives_interruption_before_completion(self):
        def interrupt(*args, **kwargs):
            events, _ = traffic.read_events(
                self.run_dir / "traffic-requests.jsonl", self.rows,
                self.summary["generation"]["manifest_sha256"])
            self.assertEqual(events[0]["event"], "submitted")
            raise SystemExit(99)

        with patch.object(traffic, "invoke", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                self.send()
        traffic.update_summary(self.run_dir, self.rows, self.summary)
        cumulative = self.current_summary()["cumulative"]
        self.assertEqual(cumulative["unknown_outcome_count"], 1)
        self.assertEqual(cumulative["submitted_count"], 1)
        self.assertIsNone(cumulative["last_success_utc"])
        with self.assertRaises(traffic.TrafficError):
            self.send(count=1, stage="retry")

    def test_target_mismatch_refused(self):
        with patch.object(traffic, "invoke", return_value=successful_result()):
            self.send(count=1)
        for key, value in (("agent_name", "other"), ("agent_version", "2"),
                           ("project_endpoint", "https://other.invalid/api/projects/other")):
            with self.subTest(key=key), self.assertRaises(traffic.TrafficError):
                self.send(offset=1, count=1, stage="next", target={**TARGET, key: value})

    def test_partial_or_other_manifest_journal_fails_closed(self):
        journal = self.run_dir / "traffic-requests.jsonl"
        for contents in ('{"event":', json.dumps({
            "event": "stage_started", "manifest_sha256": "other",
        }) + "\n"):
            journal.write_text(contents, encoding="utf-8")
            before = self.snapshot()
            with self.assertRaises(traffic.TrafficError):
                self.send()
            self.assertEqual(before, self.snapshot())

    def test_missing_or_shortened_journal_never_erases_known_attempts(self):
        with patch.object(traffic, "invoke", return_value=successful_result()):
            self.send(count=2)
        journal = self.run_dir / "traffic-requests.jsonl"
        original = journal.read_bytes()
        journal.unlink()
        before = self.snapshot()
        with self.assertRaises(traffic.TrafficError):
            self.send(stage="retry")
        self.assertEqual(before, self.snapshot())
        journal.write_bytes(b"\n".join(original.splitlines()[:-1]) + b"\n")
        before = self.snapshot()
        with self.assertRaises(traffic.TrafficError):
            self.send(stage="retry")
        self.assertEqual(before, self.snapshot())
        journal.write_bytes(original)
        (self.run_dir / "traffic-summary.json").unlink()
        with self.assertRaises(traffic.TrafficError):
            traffic.prepare_manifest(self.run_dir, self.rows, 42)

    def test_summary_rebuilt_from_journal_preserves_external_billing_audit(self):
        with patch.object(traffic, "invoke", return_value=successful_result()):
            self.send(count=1)
        persisted = self.current_summary()
        persisted["appinsights_cumulative_billing_usage"] = {"input_tokens": 777}
        persisted["cumulative"]["success_count"] = 0
        traffic.write_json(self.run_dir / "traffic-summary.json", persisted)
        summary = traffic.prepare_manifest(self.run_dir, self.rows, 42)
        traffic.update_summary(self.run_dir, self.rows, summary)
        self.assertEqual(summary["cumulative"]["success_count"], 1)
        self.assertEqual(summary["appinsights_cumulative_billing_usage"], {"input_tokens": 777})

    def test_null_usage_is_not_fabricated_zero(self):
        unknown = traffic.parse_raw_response(raw_json(response(usage=False)), 0)
        with patch.object(traffic, "invoke", side_effect=[successful_result(), unknown]):
            self.send(count=2)
        usage = self.current_summary()["cumulative"]["response_level_usage"]["input_tokens"]
        self.assertEqual(usage, {"known_sum": 123, "known_mean": 123.0, "known_count": 1, "null_count": 1})

    def test_console_and_results_do_not_contain_remote_body_or_stderr(self):
        output = io.StringIO()
        raw = raw_json(response(text="日本語 " + SECRET))
        with patch.object(traffic.subprocess, "run") as run, redirect_stdout(output):
            run.return_value = subprocess.CompletedProcess([], 0, raw, SECRET)
            traffic.send_stage(
                self.run_dir, self.rows, self.summary, offset=0, count=1, stage="smoke",
                target=TARGET, timeout=300)
        self.assertNotIn(SECRET, output.getvalue())
        for name in ("traffic-requests.jsonl", "traffic-summary.json"):
            contents = (self.run_dir / name).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, contents)
            self.assertNotIn("Authorization", contents)
            self.assertNotIn("X-User-Identity", contents)

    def test_lock_refuses_parallel_run_and_cleans_up(self):
        with traffic.run_lock(self.run_dir):
            with self.assertRaises(traffic.TrafficError):
                with traffic.run_lock(self.run_dir):
                    self.fail("二重ロック")
        self.assertFalse((self.run_dir / "traffic.lock").exists())

    def test_summary_atomic_replace_handles_transient_file_lock_only(self):
        replace = traffic.os.replace
        calls = 0

        def transient(source, destination):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("同期による一時的なロック")
            replace(source, destination)

        with patch.object(traffic.os, "replace", side_effect=transient):
            traffic.update_summary(self.run_dir, self.rows, self.summary)
        self.assertEqual(calls, 2)
        self.assertTrue((self.run_dir / "traffic-summary.json").exists())
        self.assertFalse((self.run_dir / "traffic-summary.json.writing").exists())

    def test_cli_dry_run_does_not_require_agent_or_send(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = traffic.main([
                "--dry-run", "--num-prompts", "170", "--seed", "42",
                "--output-dir", str(self.run_dir),
            ])
        self.assertEqual(code, 0)
        self.assertFalse((self.run_dir / "traffic-requests.jsonl").exists())

    def test_cli_safety_gates_and_endpoint_match(self):
        send = ["--send", "--agent-name", TARGET["agent_name"], "--agent-version", "1",
                "--project-endpoint", TARGET["project_endpoint"], "--stage", "smoke",
                "--num-prompts", "8", "--output-dir", str(self.run_dir)]
        cases = [
            ["--send", "--output-dir", str(self.run_dir)],
            ["--dry-run", "--concurrency", "2"],
            ["--dry-run", "--num-prompts", "201"],
            ["--dry-run", "--num-prompts", "0"],
            ["--dry-run", "--num-prompts", "8", "--offset", "165"],
            ["--dry-run", "--offset", "-1"],
            ["--dry-run", "--timeout", "0"],
            [arg if arg != TARGET["project_endpoint"] else "https://wrong.invalid/api/projects/wrong"
             for arg in send],
        ]
        before = self.snapshot()
        for args in cases:
            with self.subTest(args=args), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(traffic.main(args), 2)
        self.assertEqual(before, self.snapshot())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            traffic.main([])

    def test_cli_requires_preview_before_first_send(self):
        (self.run_dir / "traffic-scenarios.jsonl").unlink()
        (self.run_dir / "traffic-summary.json").unlink()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = traffic.main([
                "--send", "--agent-name", TARGET["agent_name"], "--agent-version", "1",
                "--project-endpoint", TARGET["project_endpoint"], "--stage", "smoke",
                "--num-prompts", "8", "--output-dir", str(self.run_dir),
            ])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.run_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

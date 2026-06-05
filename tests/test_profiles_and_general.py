import os
import tempfile
import unittest

import analysis_profiles
import general_scan
import main
import report
import web_manager


class ProfileAndGeneralReportTests(unittest.TestCase):
    def test_profile_aliases_and_stages(self):
        harem = analysis_profiles.load_analysis_profile("后宫")
        general = analysis_profiles.load_analysis_profile("通用")
        history = analysis_profiles.load_analysis_profile("历史")
        hard_sci_fi = analysis_profiles.load_analysis_profile("硬科幻")

        self.assertEqual(harem.name, "harem")
        self.assertTrue(harem.uses_harem_reviewer)
        self.assertFalse(harem.uses_general_scan)

        self.assertEqual(general.name, "general")
        self.assertFalse(general.uses_harem_reviewer)
        self.assertTrue(general.uses_general_scan)

        self.assertEqual(history.name, "history")
        self.assertTrue(history.uses_general_scan)
        self.assertIn("historical_logic", history.summary_fields)

        self.assertEqual(hard_sci_fi.name, "hard_sci_fi")
        self.assertTrue(hard_sci_fi.uses_general_scan)
        self.assertIn("science_consistency", hard_sci_fi.summary_fields)

        self.assertEqual(analysis_profiles.resolve_profile_name("自动"), "auto")

    def test_profile_options_are_discovered(self):
        options = analysis_profiles.profile_options(include_auto=True)
        names = [item["name"] for item in options]

        self.assertEqual(names[0], "auto")
        self.assertIn("harem", names)
        self.assertIn("general", names)
        self.assertIn("history", names)
        self.assertIn("hard_sci_fi", names)

    def test_auto_profile_inference(self):
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("大明权臣", "皇帝与朝廷在庙堂上争论边军粮饷。"),
            "history",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("星际远征", "星舰启动曲率引擎，人工智能计算虫洞航道。"),
            "hard_sci_fi",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("仙路后宫", "男主与道侣双修，红颜和未婚妻都卷入宗门风波。"),
            "harem",
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

    def test_web_manager_safe_filename(self):
        self.assertEqual(web_manager._safe_filename("../坏:名字"), "坏_名字.txt")
        self.assertEqual(web_manager._safe_filename("book.txt"), "book.txt")

    def test_web_manager_public_file_guard(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("secret")
            outside_path = f.name
        try:
            self.assertFalse(web_manager._is_safe_public_file(outside_path))
        finally:
            os.unlink(outside_path)

    def test_web_manager_public_state_includes_profiles_and_suggestions(self):
        old_state = web_manager.STATE
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("皇帝与朝廷争论，男主和红颜卷入后宫风波。")
            novel_path = f.name
        try:
            web_manager.STATE = {
                "books": {"book": {"id": "book", "name": "book", "path": novel_path, "profile": "auto", "status": "idle"}},
                "tasks": [],
            }
            web_manager._refresh_book_suggestions(web_manager.STATE["books"]["book"])
            state = web_manager._public_state()
            self.assertIn("profiles", state)
            self.assertTrue(any(item["name"] == "history" for item in state["books"][0]["profile_suggestions"]))
            self.assertTrue(any(item["name"] == "harem" for item in state["books"][0]["profile_suggestions"]))
        finally:
            web_manager.STATE = old_state
            os.unlink(novel_path)

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
        self.assertIn("官制与地方治理形成冲突", text)
        self.assertIn("真相与代价", text)
        self.assertIn("沈砚", text)
        self.assertIn("阵营/势力：沈家", text)

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
                "chunk_size": general_scan.CHUNK_SIZE,
                "chunk_overlap": general_scan.CHUNK_OVERLAP,
                "max_chunks": general_scan.MAX_CHUNKS,
                "summary": {"story_overview": "ok"},
                "chunk_results": [],
            }
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path, "history"))
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "general"))
            data["max_chunks"] = general_scan.MAX_CHUNKS + 1
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path, "history"))
        finally:
            os.unlink(novel_path)


if __name__ == "__main__":
    unittest.main()

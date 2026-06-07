import json
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

        self.assertEqual(xianxia_fantasy.name, "xianxia_fantasy")
        self.assertTrue(xianxia_fantasy.uses_general_scan)
        self.assertIn("cultivation_system", xianxia_fantasy.summary_fields)

        self.assertEqual(mystery_detective.name, "mystery_detective")
        self.assertTrue(mystery_detective.uses_general_scan)
        self.assertIn("clue_fairness", mystery_detective.summary_fields)

        self.assertEqual(game_system.name, "game_system")
        self.assertTrue(game_system.uses_general_scan)
        self.assertIn("system_rules", game_system.summary_fields)

        self.assertEqual(urban_power.name, "urban_power")
        self.assertTrue(urban_power.uses_general_scan)
        self.assertIn("face_slapping_pacing", urban_power.summary_fields)

        self.assertEqual(military_war.name, "military_war")
        self.assertTrue(military_war.uses_general_scan)
        self.assertIn("logistics_and_cost", military_war.summary_fields)

        self.assertEqual(apocalypse_survival.name, "apocalypse_survival")
        self.assertTrue(apocalypse_survival.uses_general_scan)
        self.assertIn("survival_resources", apocalypse_survival.summary_fields)

        self.assertEqual(cosmic_horror.name, "cosmic_horror")
        self.assertTrue(cosmic_horror.uses_general_scan)
        self.assertIn("anomaly_rules", cosmic_horror.summary_fields)

        self.assertEqual(sports_competition.name, "sports_competition")
        self.assertTrue(sports_competition.uses_general_scan)
        self.assertIn("tactical_matchups", sports_competition.summary_fields)

        self.assertEqual(entertainment_industry.name, "entertainment_industry")
        self.assertTrue(entertainment_industry.uses_general_scan)
        self.assertIn("public_opinion", entertainment_industry.summary_fields)

        self.assertEqual(business_career.name, "business_career")
        self.assertTrue(business_career.uses_general_scan)
        self.assertIn("business_model", business_career.summary_fields)

        self.assertEqual(crime_forensics.name, "crime_forensics")
        self.assertTrue(crime_forensics.uses_general_scan)
        self.assertIn("evidence_chain", crime_forensics.summary_fields)

        self.assertEqual(campus_youth.name, "campus_youth")
        self.assertTrue(campus_youth.uses_general_scan)
        self.assertIn("coming_of_age", campus_youth.summary_fields)

        self.assertEqual(farming_management.name, "farming_management")
        self.assertTrue(farming_management.uses_general_scan)
        self.assertIn("production_chain", farming_management.summary_fields)

        self.assertEqual(isekai_lightnovel.name, "isekai_lightnovel")
        self.assertTrue(isekai_lightnovel.uses_general_scan)
        self.assertIn("isekai_premise", isekai_lightnovel.summary_fields)

        self.assertEqual(analysis_profiles.resolve_profile_name("自动"), "auto")

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
            analysis_profiles.infer_profile_for_text("剑修飞升", "宗门弟子修炼金丹元婴，进入秘境夺取法宝传承。"),
            "xianxia_fantasy",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("密室谋杀", "侦探调查案件，嫌疑人没有不在场证明，线索指向真正凶手。"),
            "mystery_detective",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("无限副本", "主神发布任务，玩家查看系统面板、技能和装备奖励。"),
            "game_system",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("都市神医", "赘婿神医在豪门集团商战中扮猪吃虎，连续打脸反派。"),
            "urban_power",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("钢铁战役", "将军调度军团步兵火炮，依靠后勤补给完成战略包围。"),
            "military_war",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("末世安全区", "丧尸病毒爆发后，幸存者搜集物资并建设避难所。"),
            "apocalypse_survival",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("诡秘档案", "调查员追查克苏鲁仪式，理智受到污染，怪谈规则逐步显露。"),
            "cosmic_horror",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("冠军教练", "足球俱乐部在联赛决赛中调整战术，球员训练后夺得冠军。"),
            "sports_competition",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("顶流归来", "娱乐圈顶流进剧组拍戏，经纪人处理热搜黑粉和公关危机。"),
            "entertainment_industry",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("创业时代", "公司完成融资，董事会讨论股权、现金流和市场份额。"),
            "business_career",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("法医现场", "刑警在案发现场勘查，法医尸检后串起证据链锁定嫌疑人。"),
            "crime_forensics",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("同桌的你", "高中转学生和同桌参加月考竞赛，班主任关注高考压力。"),
            "campus_youth",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("领地种田记", "主角经营领地，开垦农田和作坊，通过贸易建设城镇。"),
            "farming_management",
        )
        self.assertEqual(
            analysis_profiles.infer_profile_for_text("转生勇者", "主角转生异世界，加入冒险者公会，在地下城挑战魔王。"),
            "isekai_lightnovel",
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

    def test_auto_profile_multi_label_inference(self):
        profiles = analysis_profiles.infer_profiles_for_text(
            "大明星际后宫",
            "皇帝与朝廷在庙堂争论边军粮饷，男主和道侣双修，又驾驶星舰启动曲率引擎穿过虫洞。",
        )

        self.assertIn("history", profiles)
        self.assertIn("harem", profiles)
        self.assertIn("hard_sci_fi", profiles)

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

        self.assertEqual(
            analysis_profiles.infer_profiles_for_text("小镇旧事", "他回到故乡，重新面对童年的朋友。"),
            ["general"],
        )

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
        keys = ["API_KEY", "API_KEY_POOL", "BASE_URL", "MODEL_NAME", "MAX_WORKERS"]
        old_env = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                    f.write("API_KEY=sk-dotenv\nBASE_URL=https://dotenv.example/v1\nMODEL_NAME=dotenv-model\nMAX_WORKERS=2\n")
                with open(os.path.join(tmp, "setting.txt"), "w", encoding="utf-8") as f:
                    f.write("BASE_URL=https://setting.example/v1\nMODEL_NAME=setting-model\nMAX_WORKERS=9\n")

                main.load_configs(tmp, interactive=False)
                self.assertEqual(os.environ["API_KEY"], "sk-dotenv")
                self.assertEqual(os.environ["BASE_URL"], "https://dotenv.example/v1")
                self.assertEqual(os.environ["MODEL_NAME"], "dotenv-model")
                self.assertEqual(os.environ["MAX_WORKERS"], "2")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
        self.assertIn("历史制度与时代逻辑", text)
        self.assertIn("官制与地方治理形成冲突", text)
        self.assertIn("真相与代价", text)
        self.assertIn("沈砚", text)
        self.assertIn("阵营/势力：沈家", text)

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
        self.assertTrue(any("修炼体系与战力" in prompt for prompt in prompts))

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

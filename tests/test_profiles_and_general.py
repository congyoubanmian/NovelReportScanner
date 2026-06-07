import json
import io
import os
import tempfile
import time
import unittest

import analysis_profiles
import general_scan
import main
import novel_scan
import novel_reviewer
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
        steampunk_fantasy = analysis_profiles.load_analysis_profile("蒸汽西幻")

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

        self.assertEqual(cosmic_horror.name, "cosmic_horror")
        self.assertTrue(cosmic_horror.uses_general_scan)
        self.assertIn("anomaly_rules", cosmic_horror.summary_fields)
        self.assertIn("sequence_system", cosmic_horror.summary_fields)
        self.assertIn("san_mechanics", cosmic_horror.summary_fields)
        self.assertIn("rule_based_horror", cosmic_horror.summary_fields)
        self.assertTrue(any("知晓本身即是危险" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("'知晓的代价'" in item for item in cosmic_horror.scan_focus))
        self.assertTrue(any("组织是否也是污染来源" in item for item in cosmic_horror.scan_focus))

        self.assertEqual(sports_competition.name, "sports_competition")
        self.assertTrue(sports_competition.uses_general_scan)
        self.assertIn("tactical_matchups", sports_competition.summary_fields)
        self.assertIn("technique_tactics", sports_competition.summary_fields)
        self.assertIn("season_structure", sports_competition.summary_fields)
        self.assertIn("rivalry_and_opponents", sports_competition.summary_fields)
        self.assertTrue(any("规则约束下的竞技" in item for item in sports_competition.scan_focus))
        self.assertTrue(any("名场面" in item for item in sports_competition.scan_focus))

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
        self.assertTrue(any("技术细节与专业度" in item for item in crime_forensics.scan_focus))
        self.assertTrue(any("程序违法无后果" in item for item in crime_forensics.scan_focus))

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
        self.assertIn("tech_feasibility", steampunk_fantasy.summary_fields)

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

    def test_auto_inference_keywords_are_profile_owned(self):
        for profile in analysis_profiles.list_available_profiles():
            if profile.name == "general":
                continue
            with self.subTest(profile=profile.name):
                keywords = analysis_profiles._keywords_from_manifest(profile.name)
                self.assertTrue(keywords, f"{profile.name} missing inference_keywords")

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
        self.assertIn("穿越合理性", history_points)
        self.assertIn("战争决策", history_points)
        self.assertIn("时代语言", history_points)

        sci_fi_categories = {item["name"]: item for item in sci_fi_rules["categories"]}
        self.assertIn("科学设定与技术链", sci_fi_categories)
        self.assertIn("设定自洽与世界观", sci_fi_categories)
        self.assertIn("科幻概念与硬伤", sci_fi_categories)
        sci_fi_points = {
            point["name"]
            for category in sci_fi_rules["categories"]
            for point in category.get("points", [])
        }
        self.assertIn("技术链完整性", sci_fi_points)
        self.assertIn("社会伦理", sci_fi_points)
        self.assertIn("常见硬伤", sci_fi_points)

        history_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("history"))
        sci_fi_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("hard_sci_fi"))
        self.assertIn("穿越合理性", history_text)
        self.assertIn("社会伦理", sci_fi_text)

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
        game_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("game_system"))
        self.assertIn("求道主题", xianxia_text)
        self.assertIn("玩家互动", game_text)

    def test_specialty_profiles_import_harem_cross_rules(self):
        for profile_name in [
            "xianxia_fantasy",
            "history",
            "hard_sci_fi",
            "urban_power",
            "game_system",
            "isekai_lightnovel",
            "steampunk_fantasy",
        ]:
            profile = analysis_profiles.load_analysis_profile(profile_name)
            rules_text = general_scan._profile_rules_text(profile)

            self.assertIn("harem", profile.cross_profile_rules)
            self.assertIn("跨类型导入：后宫/男性向排雷分析", rules_text)
            self.assertIn("绿帽", rules_text)
            self.assertIn("送女", rules_text)
            self.assertIn("漏女", rules_text)
            self.assertNotIn("- 万人骑:", rules_text)

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
        sports_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("sports_competition"))
        farming_text = general_scan._profile_rules_text(analysis_profiles.load_analysis_profile("farming_management"))
        self.assertIn("侦探魅力", mystery_text)
        self.assertIn("规则怪谈", horror_text)
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
                    ],
                    "partner_relations": [],
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
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            char_path = f.name
        try:
            issues, leak_map = novel_reviewer._rebuild_leak_state_from_pushed_map(
                female_leads=["戊女"],
                char_file_path=char_path,
                novel_tail="番外多年后，阿戊留在男主身边，与他一起回到府中相伴余生。",
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
            analysis_profiles.infer_profile_for_text("规则怪谈", "序列和魔药体系带来SAN值下降，精神污染引出旧日外神。"),
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

        self.assertEqual(
            analysis_profiles.infer_profiles_for_text("小镇旧事", "他回到故乡，重新面对童年的朋友。"),
            ["general"],
        )

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
                "规则怪谈要求遵守规则，违反规则会被收容物追杀，SAN值下降。",
            ),
            "cosmic_horror",
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
        self.assertEqual(isekai_keywords.get("技能"), 3)
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

    def test_harem_plus_secondary_focus_covers_isekai_and_steampunk(self):
        harem = analysis_profiles.load_analysis_profile("harem")
        overrides = harem.harem_plus.get("secondary_focus_overrides", {})

        self.assertIn("hard_sci_fi", overrides)
        self.assertIn("isekai_lightnovel", overrides)
        self.assertIn("steampunk_fantasy", overrides)

        sci_fi = analysis_profiles.load_analysis_profile("hard_sci_fi")
        isekai = analysis_profiles.load_analysis_profile("isekai_lightnovel")
        steampunk = analysis_profiles.load_analysis_profile("steampunk_fantasy")
        sci_fi_override = main._with_harem_plus_secondary_focus(sci_fi, harem)
        isekai_override = main._with_harem_plus_secondary_focus(isekai, harem)
        steampunk_override = main._with_harem_plus_secondary_focus(steampunk, harem)

        self.assertTrue(any("意识上传" in item and "洁度" in item for item in sci_fi_override.scan_focus))
        self.assertTrue(any("勇者" in item and "送女" in item for item in isekai_override.scan_focus))
        self.assertTrue(any("神秘复苏" in item and "亵女" in item for item in steampunk_override.scan_focus))

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
            self.assertIn(("Access-Control-Allow-Headers", "Content-Type"), handler.headers_sent)

            options_handler = FakeHandler()
            web_manager.Handler.do_OPTIONS(options_handler)
            self.assertEqual(options_handler.responses[0][0], 204)
            self.assertIn(("Access-Control-Allow-Origin", "https://example.test"), options_handler.headers_sent)
        finally:
            if old_origin is None:
                os.environ.pop("WEB_CORS_ALLOW_ORIGIN", None)
            else:
                os.environ["WEB_CORS_ALLOW_ORIGIN"] = old_origin

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
        self.assertIn("伏笔与回收", text)
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
                "unit_plot_mainline_link",
                "cheat_detection_dependency",
                "system_cost_validity",
                "technical_leap_risk",
            ],
            "summary": {
                "story_overview": "蒸汽时代的侦探依靠炼金系统处理案件。",
                "steampunk_setting": ["教会、帝国和蒸汽工业共同构成背景"],
                "alchemy_industry": ["炼金矩阵参与军工生产"],
                "tech_feasibility": ["差分机和高能煤精需要解释制造链"],
                "unit_plot_mainline_link": ["多个案件与主线联系偏弱"],
                "cheat_detection_dependency": ["破案高度依赖系统回放案发现场"],
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
            }
            stale = dict(fresh)
            stale["novel_mtime"] = fresh["novel_mtime"] - 1
            wrong_profile = dict(fresh)
            wrong_profile["specialty_profile"] = "history"

            self.assertTrue(report._general_summary_matches_novel(fresh, novel_path, "general"))
            self.assertFalse(report._general_summary_matches_novel(stale, novel_path, "general"))
            self.assertFalse(report._general_summary_matches_novel(wrong_profile, novel_path, "general"))
        finally:
            os.unlink(novel_path)

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
        self.assertIn("女主有效性", text)
        self.assertIn("有效性存疑", text)
        self.assertIn("漏女三层判定", text)
        self.assertIn("情感深度=✅", text)
        self.assertIn("关系确认=❌", text)
        self.assertIn("结局交代=❌", text)
        self.assertIn("结论=疑似漏女", text)

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
        self.assertTrue(any("修炼体系与战力" in prompt for prompt in prompts))

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
                [{"one_sentence_summary": "主角获得系统。", "specialty_notes": ["系统任务稳定"]}],
                profile=profile,
            )
        finally:
            general_scan._call_json = old_call_json

        self.assertIn("系统奖励稳定", summary["golden_finger_system"])
        self.assertTrue(any('"golden_finger_system": ["异能/金手指体系专项分析要点"]' in prompt for prompt in prompts))
        self.assertTrue(any('"relationships": ["关系线专项分析要点"]' in prompt for prompt in prompts))

    def test_general_scan_field_labels_cover_profile_summary_fields(self):
        common = {
            "main_plot",
            "core_conflicts",
            "worldbuilding",
            "themes",
            "strengths",
            "risks_or_issues",
            "reader_fit",
            "overall_assessment",
        }
        for profile in analysis_profiles.list_available_profiles():
            if profile.name == "harem":
                continue
            for field in profile.summary_fields:
                if field in common:
                    continue
                self.assertNotEqual(general_scan._summary_field_label(field), field.replace("_", " "), field)

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

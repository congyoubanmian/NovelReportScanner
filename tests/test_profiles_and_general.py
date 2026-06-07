import json
import io
import os
import re
import tempfile
import time
import unittest

import analysis_profiles
import general_scan
import main
import novel_scan
import novel_reviewer
import report
import toxic_reviewer
import web_manager


class ProfileAndGeneralReportTests(unittest.TestCase):
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

    def test_readme_documents_public_proxy_tls_deployment(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        readme_path = os.path.join(base_dir, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            text = f.read()

        self.assertIn("公网反向代理 / TLS 建议", text)
        self.assertIn("127.0.0.1:${WEB_PORT:-8765}:8765", text)
        self.assertIn("WEB_ACCESS_TOKEN=换成一段长随机字符串", text)
        self.assertIn("WEB_CORS_ALLOW_ORIGIN=https://scanner.example.com", text)
        self.assertIn("reverse_proxy 127.0.0.1:8765", text)
        self.assertIn("return 301 https://$host$request_uri", text)
        self.assertIn("proxy_buffering off", text)
        self.assertIn("proxy_read_timeout 3600s", text)
        self.assertIn("Authorization: Bearer", text)

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
        self.assertIn("绿帽（NTR）判定【锁定定义】", prompt)
        self.assertIn("仅限男主视角、仅限目标女主或强准女主", prompt)
        self.assertIn("反派口嗨", prompt)
        self.assertIn("男主睡了女主的亲戚/闺蜜", prompt)
        self.assertIn("送女】判定【锁定定义】", prompt)
        self.assertIn("男主主动或默许", prompt)
        self.assertIn("对象是目标女主或强准女主", prompt)
        self.assertIn("反派计划把女性送人但男主没有主动参与", prompt)

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
            self.assertIn(("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID, Authorization, X-Web-Access-Token"), handler.headers_sent)

            options_handler = FakeHandler()
            web_manager.Handler.do_OPTIONS(options_handler)
            self.assertEqual(options_handler.responses[0][0], 204)
            self.assertIn(("Access-Control-Allow-Origin", "https://example.test"), options_handler.headers_sent)
        finally:
            if old_origin is None:
                os.environ.pop("WEB_CORS_ALLOW_ORIGIN", None)
            else:
                os.environ["WEB_CORS_ALLOW_ORIGIN"] = old_origin

    def test_web_manager_access_token_auth_is_optional_and_secret(self):
        old_token = os.environ.get("WEB_ACCESS_TOKEN")
        old_key_required = os.environ.get("NOVEL_REPORT_SCANNER_REQUIRE_API_KEY")
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            os.environ["NOVEL_REPORT_SCANNER_REQUIRE_API_KEY"] = "0"
            self.assertTrue(web_manager._is_authorized_request({}, ""))
            summary = web_manager._runtime_config_summary()
            self.assertFalse(summary["web"]["auth_enabled"])
            self.assertFalse(summary["web"]["api_key_required_on_start"])

            os.environ["WEB_ACCESS_TOKEN"] = "secret-token"
            os.environ["NOVEL_REPORT_SCANNER_REQUIRE_API_KEY"] = "1"
            self.assertFalse(web_manager._is_authorized_request({}, ""))
            self.assertFalse(web_manager._is_authorized_request({"Authorization": "Bearer wrong"}, ""))
            self.assertTrue(web_manager._is_authorized_request({"Authorization": "Bearer secret-token"}, ""))
            self.assertTrue(web_manager._is_authorized_request({"X-Web-Access-Token": "secret-token"}, ""))
            self.assertTrue(web_manager._is_authorized_request({}, "token=secret-token"))

            protected_summary = web_manager._runtime_config_summary()
            self.assertTrue(protected_summary["web"]["auth_enabled"])
            self.assertTrue(protected_summary["web"]["api_key_required_on_start"])
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

    def test_web_manager_runtime_config_update_allows_only_safe_fields(self):
        keys = [
            "MAX_WORKERS",
            "RPM_LIMIT",
            "TPM_LIMIT",
            "RATE_LIMIT_SCOPE",
            "GENERAL_SCAN_MAX_CHUNKS",
            "HAREM_PLUS_GENERAL_SCAN",
            "API_KEY",
        ]
        old_env = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["API_KEY"] = "sk-secret"
            ok, result = web_manager._update_runtime_config({
                "max_workers": "4",
                "rpm_limit": "",
                "tpm_limit": "5000",
                "rate_limit_scope": "per_key",
                "general_scan_max_chunks": "120",
                "harem_plus_general_scan": True,
            })

            self.assertTrue(ok)
            self.assertEqual(os.environ["MAX_WORKERS"], "4")
            self.assertEqual(os.environ["RPM_LIMIT"], "")
            self.assertEqual(os.environ["TPM_LIMIT"], "5000")
            self.assertEqual(os.environ["RATE_LIMIT_SCOPE"], "per_key")
            self.assertEqual(os.environ["GENERAL_SCAN_MAX_CHUNKS"], "120")
            self.assertEqual(os.environ["HAREM_PLUS_GENERAL_SCAN"], "1")
            self.assertEqual(result["max_workers"], "4")
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

            ok, error = web_manager._update_runtime_config({"rate_limit_scope": "account"})
            self.assertFalse(ok)
            self.assertIn("one of", error)
        finally:
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
        try:
            os.environ.pop("WEB_ACCESS_TOKEN", None)
            body = json.dumps({"config": {"max_workers": 5}}, ensure_ascii=False).encode("utf-8")
            handler = FakeHandler(body)

            web_manager.Handler.do_POST(handler)

            self.assertEqual(handler.sent[0][0], 200)
            self.assertTrue(handler.sent[0][1]["ok"])
            self.assertEqual(handler.sent[0][1]["config"]["max_workers"], "5")
            self.assertEqual(os.environ["MAX_WORKERS"], "5")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
        try:
            web_manager.STATE = {"books": {}, "tasks": []}
            web_manager._sync_books_from_disk = lambda: None
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

            def send_response(self, code):
                self.response = code

            def send_header(self, key, value):
                self.headers_sent.append((key, value))

            def end_headers(self):
                pass

            def send_error(self, code, message=None):
                self.errors.append((code, message))

        old_base_dir = web_manager.get_base_dir
        old_chunk_size = web_manager.FILE_RESPONSE_CHUNK_SIZE
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
                self.assertEqual(forbidden_handler.errors[0][0], 403)
        finally:
            web_manager.get_base_dir = old_base_dir
            web_manager.FILE_RESPONSE_CHUNK_SIZE = old_chunk_size

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

    def test_web_manager_scan_subprocess_parses_result_and_logs_output(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = iter([
                    "scan started\n",
                    web_manager._WEB_SCAN_RESULT_PREFIX + '{"status":"ok","book_name":"book","profile":"general"}\n',
                ])

            def wait(self):
                return 0

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
            self.assertIn("scan started", log_file.getvalue())
            self.assertNotIn(web_manager._WEB_SCAN_RESULT_PREFIX, log_file.getvalue())
            self.assertIn("--web-scan-task", calls[0][0])
            self.assertIn("--profile-json", calls[0][0])
        finally:
            web_manager.subprocess.Popen = old_popen

    def test_web_manager_public_state_includes_profiles_and_suggestions(self):
        old_state = web_manager.STATE
        old_env = {key: os.environ.get(key) for key in ("API_KEY", "API_KEY_POOL", "BASE_URL", "MODEL_NAME", "MAX_WORKERS")}
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("皇帝与朝廷争论，男主和红颜卷入后宫风波。")
            novel_path = f.name
        try:
            os.environ["API_KEY_POOL"] = "sk-one,sk-two"
            os.environ["API_KEY"] = "sk-one"
            os.environ["BASE_URL"] = "https://example.test/v1"
            os.environ["MODEL_NAME"] = "test-model"
            os.environ["MAX_WORKERS"] = "3"
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
            self.assertNotIn("sk-one", json.dumps(state, ensure_ascii=False))
            self.assertTrue(any(item["name"] == "history" for item in state["books"][0]["profile_suggestions"]))
            self.assertTrue(any(item["name"] == "harem" for item in state["books"][0]["profile_suggestions"]))
        finally:
            web_manager.STATE = old_state
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
                    "running": {"id": "running", "name": "running", "profile": "general", "status": "running", "task_id": "t1"},
                    "idle": {"id": "idle", "name": "idle", "profile": "general", "status": "idle"},
                },
                "tasks": [{"id": "t1", "book_id": "running", "status": "running"}],
            }

            self.assertEqual(web_manager._cancel_queued_book("missing"), (False, "book not found"))
            self.assertEqual(web_manager._cancel_queued_book("running"), (False, "book is not queued"))
            self.assertEqual(web_manager._cancel_queued_book("idle"), (False, "book is not queued"))
        finally:
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
        finally:
            report.OpenAI = old_openai
            report.API_KEY_POOL = old_api_key_pool

        self.assertIn("极低", negated["romance_density"])
        self.assertIn("未见明确恋爱推进", negated["romance_progression"])
        self.assertIn("中等或以上", positive["romance_density"])

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
        self.assertTrue(report._has_male_past_romance_risk("男主前世老婆在他绝症后卷光家产跑路。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前女友但已经分手。"))
        self.assertTrue(report._has_male_past_romance_risk("男主前妻已经去世。"))
        self.assertTrue(report._has_male_past_romance_risk("男主的前夫早已去世。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前任但已经分手。"))
        self.assertTrue(report._has_male_past_romance_risk("男主有前任女友但已经分手。"))
        self.assertFalse(report._has_male_past_romance_risk("男主听说前世老婆卷光家产跑路，后来证实是误会。"))
        self.assertFalse(report._has_male_past_romance_risk("有人传言男主有前女友，但实际没有恋爱经历。"))
        self.assertFalse(report._has_male_past_romance_risk("男主梦见自己前世有妻子背叛。"))
        self.assertTrue(report._has_male_past_romance_risk("证据显示男主前世老婆在他绝症后卷光家产跑路。"))

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
        self.assertFalse(report._contains_positive_signal_text("读者喜爱她的吐槽和搞笑桥段。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("她热爱推理，主要作用是提供线索。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("三小只包括爱丽丝、小人鱼和公主。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("角色名叫爱琳，主要是女仆助手。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她爱男主，并主动告白。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她爱他，并愿意相伴余生。", ["爱"]))
        self.assertTrue(report._contains_positive_signal_text("她爱上男主。", ["爱"]))
        self.assertFalse(report._contains_positive_signal_text("剧情让读者动心，但角色没有恋爱线。", ["动心"]))
        self.assertFalse(report._contains_positive_signal_text("作者对蒸汽设定很倾心，说明文字很多。", ["倾心"]))
        self.assertTrue(report._contains_positive_signal_text("她对男主动心，并开始吃醋。", ["动心"]))
        self.assertTrue(report._contains_positive_signal_text("她倾心男主，并主动表白。", ["倾心"]))
        self.assertTrue(report._contains_positive_signal_text("她是男主未婚妻，双方感情稳定。", ["未婚妻"]))
        self.assertFalse(report._contains_positive_signal_text("她尚未成为男主妻子。", ["妻子"]))

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
                            "content": "弱女被安排嫁给路人男。",
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
        self.assertIn("女主定位上下文：正女=目标女主", text)
        self.assertEqual(text.count("定义复核提示"), 1)

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
        self.assertNotIn("heroine_position_context", missing_issue)
        self.assertIn("未命中已识别女主名或别名", missing_issue["definition_review_hint"])
        self.assertEqual(normal_issue["heroine_position_context"], "弱女=弱准女主")
        self.assertNotIn("definition_review_hint", normal_issue)

        edge_issue = report._annotate_issue_for_report(
            {"type": "绿帽擦边", "content": "弱女差点被反派绑走。"},
            contexts,
        )
        self.assertEqual(edge_issue["heroine_position_context"], "弱女=弱准女主")
        self.assertNotIn("definition_review_hint", edge_issue)

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
        real_romance = novel_reviewer._derive_past_life_cleanliness({}, "上一世她喜欢过别的男人。")

        self.assertTrue(rumor["past_life_clean"])
        self.assertEqual(rumor["past_life_severity"], "clean")
        self.assertTrue(hearsay["past_life_clean"])
        self.assertEqual(hearsay["past_life_severity"], "clean")
        self.assertTrue(alleged["past_life_clean"])
        self.assertEqual(alleged["past_life_severity"], "clean")
        self.assertTrue(negated_love["past_life_clean"])
        self.assertTrue(negated_marriage["past_life_clean"])
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

    def test_general_scan_summary_alias_candidates_are_bidirectional(self):
        old_call_json = general_scan._call_json
        try:
            def fake_call_json(_messages, max_tokens=3000):
                return {
                    "story_overview": "旧字段请求测试。",
                    "main_plot": ["主线"],
                    "core_conflicts": ["冲突"],
                    "worldbuilding": ["设定"],
                    "themes": ["主题"],
                    "foreshadowing_and_payoff": ["伏笔"],
                    "tech_feasibility": ["标准字段技术内容"],
                    "technology_feasibility": ["同义旧字段技术内容"],
                    "strengths": ["优点"],
                    "risks_or_issues": ["问题"],
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
                summary_fields=["main_plot", "tech_plausibility"],
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

    def test_general_report_reads_summary_field_alias_values(self):
        self.assertEqual(report.summary_field_label("power_system"), "异能/金手指体系")
        self.assertEqual(report.summary_field_label("humanity_and_morality"), "人性与道德困境")
        self.assertEqual(report.summary_field_label("tech_plausibility"), "技术可行性")
        self.assertEqual(report.summary_field_label("case_design"), "案件结构")
        self.assertEqual(report.summary_field_label("romance_subplot"), "恋爱喜剧平衡")

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

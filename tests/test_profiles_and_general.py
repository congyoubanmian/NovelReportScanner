import os
import tempfile
import unittest

import analysis_profiles
import general_scan
import report


class ProfileAndGeneralReportTests(unittest.TestCase):
    def test_profile_aliases_and_stages(self):
        harem = analysis_profiles.load_analysis_profile("后宫")
        general = analysis_profiles.load_analysis_profile("通用")

        self.assertEqual(harem.name, "harem")
        self.assertTrue(harem.uses_harem_reviewer)
        self.assertFalse(harem.uses_general_scan)

        self.assertEqual(general.name, "general")
        self.assertFalse(general.uses_harem_reviewer)
        self.assertTrue(general.uses_general_scan)

    def test_general_report_uses_story_summary_and_characters(self):
        general_summary = {
            "summary": {
                "story_overview": "主角追查旧案，牵出沈家与巡查司的冲突。",
                "main_plot": ["主角追查旧案"],
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
                "novel_path": novel_path,
                "novel_mtime": os.path.getmtime(novel_path),
                "chunk_size": general_scan.CHUNK_SIZE,
                "chunk_overlap": general_scan.CHUNK_OVERLAP,
                "max_chunks": general_scan.MAX_CHUNKS,
                "summary": {"story_overview": "ok"},
                "chunk_results": [],
            }
            self.assertTrue(general_scan._is_fresh_summary(data, novel_path))
            data["max_chunks"] = general_scan.MAX_CHUNKS + 1
            self.assertFalse(general_scan._is_fresh_summary(data, novel_path))
        finally:
            os.unlink(novel_path)


if __name__ == "__main__":
    unittest.main()

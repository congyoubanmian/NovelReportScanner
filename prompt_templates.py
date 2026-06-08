import os
from typing import Dict


PROMPT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "harem_scan_chunk": {
        "default_version": "v1",
        "description": "后宫首扫片段事实抽取与雷点扫描",
    },
    "general_scan_chunk": {
        "default_version": "v1",
        "description": "通用/专项剧情片段抽取",
    },
    "general_summary": {
        "default_version": "v1",
        "description": "通用/专项整书总结",
    },
}


def prompt_template_version(template_name: str) -> str:
    template = PROMPT_TEMPLATES.get(template_name)
    if not template:
        raise KeyError(f"unknown prompt template: {template_name}")
    env_key = f"PROMPT_TEMPLATE_{template_name.upper()}_VERSION"
    return (os.environ.get(env_key) or template["default_version"]).strip() or template["default_version"]


def prompt_template_metadata(template_name: str) -> Dict[str, str]:
    template = PROMPT_TEMPLATES.get(template_name)
    if not template:
        raise KeyError(f"unknown prompt template: {template_name}")
    return {
        "name": template_name,
        "version": prompt_template_version(template_name),
        "description": template.get("description", ""),
    }


def prompt_templates_metadata(*template_names: str) -> Dict[str, Dict[str, str]]:
    names = template_names or tuple(sorted(PROMPT_TEMPLATES))
    return {name: prompt_template_metadata(name) for name in names}

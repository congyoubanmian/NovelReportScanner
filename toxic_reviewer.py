import concurrent.futures
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Set

from tqdm import tqdm

from shared_utils import MODEL, RULES_FILE, _safe_json_loads_maybe, chat_completion, logger, record_usage, read_file_safely


STRICT_HAREM_ISSUE_TYPES = ("绿帽", "送女")
STRICT_NTR_EXCLUSION_HINTS = ("擦边", "反复救援", "未遂", "风险")


def is_strict_harem_issue_type(issue_type: str) -> bool:
    text = str(issue_type or "")
    if any(word in text for word in STRICT_HAREM_ISSUE_TYPES):
        return True
    upper_text = text.upper()
    if "NTR" not in upper_text:
        return False
    return not any(word in text for word in STRICT_NTR_EXCLUSION_HINTS)


def load_rules_dict(rules_file: str = RULES_FILE) -> Dict[str, str]:
    if not os.path.exists(rules_file):
        return {}
    data = json.loads(read_file_safely(rules_file))
    rules_map = {}
    for cat in data.get("categories", []):
        for point in cat["points"]:
            rules_map[point["name"]] = point["description"]
    return rules_map


def _strict_harem_review_rules(issue_type: str) -> str:
    if not is_strict_harem_issue_type(issue_type):
        return ""
    return """
5. **送女/绿帽锁定定义（高于占有欲泛化判断）**：
   - 如果指控罪名是【绿帽】，必须同时满足：男主视角；对象是目标女主或强准女主；关系发生在男主关系成立后；存在非男主男性；有明确暧昧、恋爱、性关系或实质情感背叛事实。
   - 绿帽排除项：路人/背景女性/敌方家眷/单纯漂亮女配；反派口嗨、旁人意淫、传闻、未来计划、误会、梦境、幻境、弱暗示；男主睡女主亲友；配角把女性献给男主；女主被男主收入后宫。
   - 女主被非男主男性强迫、胁迫、囚禁、调戏、窥视等，若没有明确性关系或女主主观情感背叛，不可裁成绿帽，应说明更适合亵女/虐女/NTR擦边/背景伤害。
   - 如果指控罪名是【送女】，必须同时满足：男主主动或默许；对象是目标女主或强准女主；接收方是非男主男性；有明确送出、让渡、撮合、成婚、同房或安排关系事实。
   - 送女排除项：配角、反派、家族、皇帝、父母、师门把女性献给男主或安排给男主；男主救下或接收女性；反派计划把女性送人但男主未主动参与；普通政治联姻、背景婚配、非目标女性被安排婚姻；女主自己走失、被抓、正常分手、被反派绑走或被家族逼婚。
   - 对【绿帽/送女】，不能仅因为“占有欲读者不适”就判 valid=true；缺少上述必要构成时应判 valid=false，并在 review_comment 写明缺少哪一项。
"""


def build_review_prompts(
    issue: Dict[str, Any],
    rule_description: str,
    male_lead: str,
    female_leads: List[str],
) -> tuple[str, str]:
    category = issue.get("category", "未知")
    issue_type = issue.get("type", "未知")
    content = issue.get("content", "")
    original_reason = issue.get("reason", "")
    female_lead_text = "、".join(str(x) for x in (female_leads or []) if str(x).strip()) or "未提供"
    strict_rules = _strict_harem_review_rules(issue_type)

    system_prompt = f"""你是一个极端占有欲读者视角的二审法官。你的判定标准非常敏感：只要情节可能让这种读者感到不适，就视为“郁闷点/雷点”。

案件档案：
👨‍🦰 男主：【{male_lead}】 (拥有豁免权)
已知女主/准女主名单：{female_lead_text}

【核心法则（请严格执行）】：

1. **男主豁免权 (最高优先级)**：
   - 只要施害行为的执行者是 **{male_lead}** (或其昵称)，无论多过分，均判定为 **【无效 (Valid:false)】**。
   - 但：在公共/混杂场景下，女主被暴露、被围观、被窥视的风险本身会让占有欲读者不适，即使叙述视角是男主、男主在场或男主在看，也仍然可以判定为不适点。

2. **极端占有视角（无须明确施害人）**：
   - 只要场景能让占有欲极强的读者感到不适，即判定 **【成立 Valid:true】**，即便没有明确的施害人。
   - 典型触发：公共或有其他男性在场/可能在场的场景中，女主暴露、被看、被拍、被讨论、被意淫的可能性；即便只有男主在场、由男主视角叙述，只要处于“公共/他人可能看到”的环境，也视为不适。
   - 示例：公共海滩/泳池穿暴露泳衣、混浴/男厕误入、被路人围观/偷拍/起哄、舞台表演被迫展示身材、任何“可能被其他雄性看到/触碰”的描写。

3. **配角/路人零容忍**：
   - 只要行为人 **不是 {male_lead}**，且对女主进行了以下行为，一律判定为**【成立 (Valid: true)】**：
     - **纠缠/骚扰**：如死缠烂打、被迫约会、递情书被拒还纠缠。
     - **言语冒犯**：如未经允许叫昵称、言语调戏、威胁恐吓（如“不做我女朋友就有你好受”）。
     - **肢体接触**：任何未经女主同意的接触。
   - **注意**：不需要造成实质性肉体伤害，只要让女主感到**反感、被迫、不适**，即符合“亵女”或“郁闷点”定义。

4. **定义复核**：
   - 一般郁闷点/亵女类指控：仅需判定是否让占有欲极强的读者不适；若有施害人按上条处理。
   - 严格关系雷点必须优先服从对应定义，不能把泛化不适直接升级成绿帽或送女。
{strict_rules}"""

    user_prompt = f"""
    【指控分类】：{category}
    【指控罪名】：{issue_type}
    【法律定义】：{rule_description}
    【证据原文】：{content}
    【初审理由】：{original_reason}

    请裁决：
    1. 即便没有明确施害人，此情节是否会让“占有欲极强”的读者感到不适？为何？
    2. 若存在施害人且不是 {male_lead}，说明其行为是否构成骚扰/亵女/让人不适。
    3. 若指控罪名是绿帽或送女，请逐项核对锁定定义；缺少任一必要构成时必须判 invalid，并说明缺少项。

    输出 JSON: {{"valid": true/false, "review_comment": "简短裁决理由（体现不适点、施害人或严格定义缺项）"}}
    """
    return system_prompt, user_prompt


def review_issue(issue: Dict[str, Any], rule_description: str, male_lead: str, female_leads: List[str]) -> Dict[str, Any]:
    content = issue.get("content", "")

    if not content:
        return {"valid": False, "review_comment": "证据缺失"}

    system_prompt, user_prompt = build_review_prompts(issue, rule_description, male_lead, female_leads)

    try:
        last_err = None
        for attempt in range(3):
            try:
                response = chat_completion(
                    model=MODEL,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                record_usage(response)

                content = None
                try:
                    content = response.choices[0].message.content
                except Exception:
                    content = None

                data, err = _safe_json_loads_maybe(content)
                if data is None:
                    last_err = err
                    time.sleep(1 + attempt)
                    continue

                # 规范化字段，避免模型输出缺字段导致上游 KeyError/误判
                return {
                    "valid": bool(data.get("valid", False)),
                    "review_comment": str(data.get("review_comment", "") or "").strip() or "（无裁决理由）",
                }
            except Exception as e:
                last_err = str(e)
                time.sleep(1 + attempt)
                continue

        # 三次都失败：标记为 api_error，让主流程不要把它当作“已判定”
        return {
            "api_error": True,
            "valid": False,
            # 宁多勿要缺：明确标注 API 错误，同时带上错误信息，便于你复盘/排查
            "review_comment": f"API错误：{last_err}",
        }
    except Exception as e:
        return {
            "api_error": True,
            "valid": False,
            "review_comment": f"API错误：{e}",
        }


@dataclass
class ReviewResult:
    verified_issues: List[Dict[str, Any]]
    rejected_issues: List[Dict[str, Any]]
    rejected_count: int
    pending_by_idx: Dict[int, Dict[str, Any]]
    processed_issue_indices: Set[int]


def batch_review_toxic_points(
    *,
    executor: concurrent.futures.ThreadPoolExecutor,
    issues: List[Dict[str, Any]],
    rules_map: Dict[str, str],
    male_lead: str,
    female_leads: List[str],
    processed_issue_indices: Set[int],
    verified_issues: List[Dict[str, Any]],
    rejected_issues: List[Dict[str, Any]],
    rejected_count: int,
    save_checkpoint_fn: Callable[..., None],
) -> ReviewResult:
    # API 失败/未判定的条目：写入报告，但不计入已处理；本次跑完后会再补跑一轮
    pending_by_idx: Dict[int, Dict[str, Any]] = {}
    local_processed_indices = set(processed_issue_indices or set())
    local_verified_issues = list(verified_issues or [])
    local_rejected_issues = list(rejected_issues or [])
    local_rejected_count = int(rejected_count or 0)

    # 若无断点则初始化
    if not local_processed_indices:
        local_verified_issues = []
        local_rejected_issues = []
        local_rejected_count = 0

    future_to_issue = {
        executor.submit(
            review_issue,
            issue,
            rules_map.get(issue.get("type"), ""),
            male_lead,
            female_leads,
        ): (idx, issue)
        for idx, issue in enumerate(issues)
        if idx not in local_processed_indices
    }

    for future in tqdm(
        concurrent.futures.as_completed(future_to_issue),
        total=len(future_to_issue),
        desc="毒点二审",
    ):
        idx, orig = future_to_issue[future]
        res = future.result()
        # API 失败：写入“未判定”栏目，但不计入已处理（下次有可用 key 会自动重试）
        if res.get("api_error"):
            pend = orig.copy()
            pend["review_comment"] = res.get("review_comment", "API错误")
            pend["api_error"] = True
            pending_by_idx[idx] = pend
            logger.warning(f"第{idx}条指控 API失败，先记为 API错误，稍后将补跑重试: {pend.get('review_comment','')[:120]}")
            continue
        if res.get("valid", False):
            orig["review_comment"] = res.get("review_comment")
            local_verified_issues.append(orig)
        else:
            local_rejected_count += 1
            rej = orig.copy()
            rej["review_comment"] = res.get("review_comment")
            local_rejected_issues.append(rej)
        local_processed_indices.add(idx)

        # 每完成5条保存一次断点
        if len(local_processed_indices) % 5 == 0:
            save_checkpoint_fn(
                verified_issues=local_verified_issues,
                rejected_count=local_rejected_count,
                rejected_issues=local_rejected_issues,
                processed_issue_indices=local_processed_indices,
            )

    # 结束后再保存一次，确保落盘
    save_checkpoint_fn(
        verified_issues=local_verified_issues,
        rejected_count=local_rejected_count,
        rejected_issues=local_rejected_issues,
        processed_issue_indices=local_processed_indices,
    )

    # --- 1.5 补跑：对本轮 API 失败的条目再重试一轮（避免“刚开始失败就永远缺一块”） ---
    if pending_by_idx:
        print(f"🔁 补跑重试：本轮有 {len(pending_by_idx)} 条 API失败指控，正在追加重试一轮...")
        retry_future_map = {
            executor.submit(
                review_issue,
                pend_item,
                rules_map.get(pend_item.get("type"), ""),
                male_lead,
                female_leads,
            ): idx
            for idx, pend_item in pending_by_idx.items()
            if idx not in local_processed_indices
        }
        for future in tqdm(
            concurrent.futures.as_completed(retry_future_map),
            total=len(retry_future_map),
            desc="补跑重试",
        ):
            idx = retry_future_map[future]
            orig = pending_by_idx.get(idx)
            if not orig:
                continue
            try:
                res = future.result()
            except Exception as e:
                # 极端异常也按 API错误保留
                pending_by_idx[idx]["review_comment"] = f"API错误：{e}"
                pending_by_idx[idx]["api_error"] = True
                continue

            if res.get("api_error"):
                # 仍失败：保持 API错误（宁多勿少），留待下次
                pending_by_idx[idx]["review_comment"] = (
                    res.get("review_comment") or pending_by_idx[idx].get("review_comment") or "API错误"
                )
                pending_by_idx[idx]["api_error"] = True
                continue

            # 成功：写入正式结果，并从 pending 移除，加入已处理索引
            if res.get("valid", False):
                orig2 = orig.copy()
                orig2["review_comment"] = res.get("review_comment")
                local_verified_issues.append(orig2)
            else:
                local_rejected_count += 1
                rej = orig.copy()
                rej["review_comment"] = res.get("review_comment")
                local_rejected_issues.append(rej)
            local_processed_indices.add(idx)
            pending_by_idx.pop(idx, None)

        # 补跑后再保存一次断点
        save_checkpoint_fn(
            verified_issues=local_verified_issues,
            rejected_count=local_rejected_count,
            rejected_issues=local_rejected_issues,
            processed_issue_indices=local_processed_indices,
        )

    return ReviewResult(
        verified_issues=local_verified_issues,
        rejected_issues=local_rejected_issues,
        rejected_count=local_rejected_count,
        pending_by_idx=pending_by_idx,
        processed_issue_indices=local_processed_indices,
    )

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple


_QUOTE_STRIP = " \t\r\n\u3000“”\"'`‘’（）()[]【】"

_KINSHIP_OR_ADDRESS = {
    "哥哥", "弟弟", "姐姐", "妹妹", "大哥", "二哥", "三哥", "大姐", "二姐",
    "父亲", "母亲", "爸爸", "妈妈", "爹", "娘", "夫君", "丈夫", "妻子", "老婆",
    "老公", "师父", "师傅", "师兄", "师弟", "师姐", "师妹", "师叔", "师伯",
    "前辈", "晚辈", "道友", "公子", "小姐", "姑娘", "夫人", "大人", "老爷",
    "陛下", "殿下", "王爷", "公主", "太后", "皇后", "娘娘", "主人", "少爷",
}

_GENERIC_PERSON = {
    "男人", "女人", "男子", "女子", "少年", "少女", "青年", "老人", "老者",
    "老头", "老妇", "妇人", "大汉", "汉子", "小孩", "孩子", "丫鬟", "侍女",
    "仆人", "下人", "路人", "众人", "大家", "旁人", "那人", "此人", "这人",
    "那女子", "那男人", "对方", "某人", "一人", "二人", "几人", "众女",
    "众妖", "小妖", "妖怪", "妖精", "敌人", "刺客", "守卫", "弟子", "侍卫",
    "男主", "女主", "主角", "男主角", "女主角", "男主人公", "女主人公",
}

_PRONOUNS = {
    "他", "她", "它", "他们", "她们", "我们", "你们", "我", "你", "自己",
    "本座", "老夫", "在下", "妾身", "奴家", "贫僧", "贫道",
}

_UNSAFE_SUBSTRINGS = (
    "某个", "一个", "那位", "这位", "众", "群", "们", "之一", "一名", "一位",
)


def normalize_name(name: Any) -> str:
    text = str(name or "").strip(_QUOTE_STRIP)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("（", "(").replace("）", ")").strip(_QUOTE_STRIP)
    return text[:80]


def core_name(name: Any) -> str:
    text = normalize_name(name)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return text.strip(_QUOTE_STRIP)


def alias_safety_level(alias: Any) -> int:
    """0=hard block, 1=suspicious, 2=safe."""
    text = core_name(alias)
    if not text:
        return 0
    if text in _PRONOUNS or text in _GENERIC_PERSON:
        return 0
    if text in _KINSHIP_OR_ADDRESS:
        return 0
    if len(text) == 1:
        return 0
    if len(text) > 20:
        return 0
    if any(part in text for part in _UNSAFE_SUBSTRINGS):
        return 1
    if len(text) == 2 and any(text.endswith(suffix) for suffix in _KINSHIP_OR_ADDRESS):
        return 1
    if re.fullmatch(r"[男女老少大小中青白黑红蓝紫]+", text):
        return 0
    return 2


def is_unsafe_alias(alias: Any) -> bool:
    return alias_safety_level(alias) <= 0


def is_generic_person_name(name: Any) -> bool:
    text = core_name(name)
    if not text:
        return True
    if alias_safety_level(text) <= 0:
        return True
    if text in _GENERIC_PERSON or text in _KINSHIP_OR_ADDRESS or text in _PRONOUNS:
        return True
    if len(text) <= 1:
        return True
    return False


def clean_aliases(aliases: Iterable[Any], primary_name: Any = "") -> Tuple[List[str], List[Dict[str, Any]]]:
    primary = core_name(primary_name)
    cleaned = []
    discarded = []
    seen = set()
    for raw in aliases or []:
        alias = normalize_name(raw)
        alias_core = core_name(alias)
        if not alias_core or alias_core == primary:
            continue
        level = alias_safety_level(alias_core)
        if level <= 0:
            discarded.append({
                "field": "aliases",
                "value": alias,
                "reason": "unsafe_alias",
                "detail": "泛称、称谓或代词不能作为别名合并依据",
            })
            continue
        if alias_core in seen:
            continue
        seen.add(alias_core)
        cleaned.append(alias_core)
    return cleaned, discarded


class _UnionFind:
    def __init__(self):
        self.parent = {}
        self.size = {}

    def find(self, item):
        if item not in self.parent:
            self.parent[item] = item
            self.size[item] = 1
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.size[root_left] < self.size[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        self.size[root_left] += self.size[root_right]

    def groups(self):
        result = {}
        for item in list(self.parent):
            result.setdefault(self.find(item), []).append(item)
        return result


def _record_names(record: Dict[str, Any]) -> List[str]:
    names = []
    for key in ("name", "main_name"):
        value = normalize_name(record.get(key))
        if value:
            names.append(value)
    for key in ("aliases", "other_names", "all_names"):
        for value in record.get(key) or []:
            value = normalize_name(value)
            if value:
                names.append(value)
    return list(dict.fromkeys(names))


def pick_canonical_name(names: Iterable[str], frequencies: Counter = None) -> str:
    candidates = [core_name(name) for name in names or [] if core_name(name)]
    if not candidates:
        return ""
    frequencies = frequencies or Counter()
    candidates = sorted(
        set(candidates),
        key=lambda item: (
            -frequencies.get(item, 0),
            0 if 2 <= len(item) <= 4 else 1,
            len(item),
            item,
        ),
    )
    return candidates[0]


def build_conservative_alias_map(records: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Build alias -> canonical map with unsafe aliases blocked as merge bridges."""
    uf = _UnionFind()
    freq = Counter()
    for record in records or []:
        if not isinstance(record, dict):
            continue
        primary = core_name(record.get("name") or record.get("main_name"))
        if is_generic_person_name(primary):
            continue
        freq[primary] += int(record.get("count") or 1)
        safe_names = []
        for name in _record_names(record):
            normalized = core_name(name)
            if not normalized or is_generic_person_name(normalized):
                continue
            if alias_safety_level(normalized) < 2 and normalized != primary:
                continue
            safe_names.append(normalized)
            freq[normalized] += 1
        if not safe_names:
            continue
        root = primary or safe_names[0]
        uf.find(root)
        for name in safe_names:
            uf.union(root, name)

    alias_map = {}
    for _root, names in uf.groups().items():
        canonical = pick_canonical_name(names, freq)
        if not canonical:
            continue
        for name in names:
            if name != canonical:
                alias_map[name] = canonical
    return alias_map

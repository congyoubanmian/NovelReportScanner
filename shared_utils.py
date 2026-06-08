import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from typing import Any, Dict, Optional, Tuple

from openai import OpenAI

from Timerror import make_chat_completion
from token_tracker import create_default_tracker


# ================= 路径与编码工具 =================
def get_base_dir():
    """返回程序根目录：打包后为 exe 所在目录，开发时为脚本所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def read_file_safely(file_path, mode="r"):
    """安全读取文件：先尝试 UTF-8，失败则回退 GB18030（涵盖 GBK/GB2312）。"""
    try:
        with open(file_path, mode, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, mode, encoding="gb18030") as f:
            return f.read()


# ================= 配置区域 =================
# API Key 支持池化：优先 API_KEY_POOL（逗号分隔），否则回退 API_KEY
API_KEY_POOL = [
    k.strip()
    for k in os.environ.get("API_KEY_POOL", os.environ.get("API_KEY", "")).split(",")
    if k.strip()
]
API_KEY = API_KEY_POOL[0] if API_KEY_POOL else ""
BASE_URL = os.environ.get("BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("MODEL_NAME", "deepseek-chat")
BASE_DIR = get_base_dir()
SCAN_RESULTS_DIR = os.environ.get("SCAN_RESULTS_DIR", os.path.join(BASE_DIR, "results"))
RULES_FILE = os.environ.get("ANALYSIS_RULES_FILE") or os.path.join(
    BASE_DIR,
    "profiles",
    os.environ.get("ANALYSIS_PROFILE", "harem"),
    "rules.json",
)
if not os.path.exists(RULES_FILE):
    RULES_FILE = os.path.join(BASE_DIR, "rules2.json")
# 并发线程数：环境值 + 4（默认 8+4=12）
_base_workers = int(os.environ.get("MAX_WORKERS", "8"))
MAX_WORKERS = _base_workers + 4

logger = logging.getLogger("reviewer")


def _read_int_env(name: str, default: int, *, min_value: int = 0) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    return max(min_value, value)


LOG_MAX_BYTES = _read_int_env("LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUP_COUNT = _read_int_env("LOG_BACKUP_COUNT", 5)


def create_rotating_file_handler(
    log_path: str,
    *,
    formatter: logging.Formatter = None,
    encoding: str = "utf-8",
    max_bytes: int = None,
    backup_count: int = None,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES if max_bytes is None else max(0, int(max_bytes)),
        backupCount=LOG_BACKUP_COUNT if backup_count is None else max(0, int(backup_count)),
        encoding=encoding,
    )
    if formatter is not None:
        handler.setFormatter(formatter)
    return handler


def configure_rotating_file_logger(
    target_logger: logging.Logger,
    log_path: str,
    *,
    level: int = logging.INFO,
    stream: bool = True,
    formatter: logging.Formatter = None,
) -> logging.Logger:
    formatter = formatter or logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    for handler in list(target_logger.handlers):
        target_logger.removeHandler(handler)
        handler.close()
    file_handler = create_rotating_file_handler(log_path, formatter=formatter)
    target_logger.addHandler(file_handler)
    if stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        target_logger.addHandler(stream_handler)
    target_logger.setLevel(level)
    return target_logger

# ---- API 调用封装：统一收敛到 Timerror.py（只需修改 Timerror.py 即可全局生效）----
DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_403_RETRIES = 3  # 连续 3 次 403 才标记为不可用
DEFAULT_MAX_TIMEOUT_RETRIES = 3  # 连续超时 3 次则标记 key 不可用
DEFAULT_REQUEST_TIMEOUT = 120  # 请求超时时间（秒）

# Backward-compatible aliases for older imports.
MAX_403_RETRIES = DEFAULT_MAX_403_RETRIES
MAX_TIMEOUT_RETRIES = DEFAULT_MAX_TIMEOUT_RETRIES
REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT


def _openai_client_factory(api_key: str, base_url: str, timeout: int):
    """
    创建 OpenAI 客户端，关闭 SDK 暗重试并使用细粒度 timeout。

    【关键】max_retries=0 关闭 SDK 自动重试：
    - SDK 默认会重试 2 次，每次都有 timeout
    - 外层 Timerror.py 再重试 5 次
    - 不关闭的话，总耗时可能达到 120s * 3 * 5 = 1800s

    【关键】使用 httpx.Timeout 细粒度配置：
    - connect: 连接超时（10s）
    - read: 读取超时（根据请求规模动态调整）
    - write: 写入超时（30s）
    - pool: 连接池超时（10s）
    """
    try:
        import httpx

        http_timeout = httpx.Timeout(
            connect=10.0,
            read=float(timeout),
            write=30.0,
            pool=10.0,
        )
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=http_timeout,
            max_retries=0,  # 关闭 SDK 自动重试
        )
    except ImportError:
        # 没有 httpx 时使用简单 timeout
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # 关闭 SDK 自动重试
        )


def create_chat_completion(
    *,
    api_key_pool=None,
    base_url: str = None,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_403_retries: int = DEFAULT_MAX_403_RETRIES,
    max_timeout_retries: int = DEFAULT_MAX_TIMEOUT_RETRIES,
    base_delay: int = 2,
    logger=None,
):
    return make_chat_completion(
        openai_client_factory=_openai_client_factory,
        api_key_pool=api_key_pool if api_key_pool is not None else API_KEY_POOL,
        base_url=base_url or BASE_URL,
        request_timeout=request_timeout,
        max_retries=max_retries,
        max_403_retries=max_403_retries,
        max_timeout_retries=max_timeout_retries,
        base_delay=base_delay,
        logger=logger,
    )


chat_completion = create_chat_completion(logger=logger)

token_tracker = None


def init_token_tracker(book_name: str, run_id: Optional[str] = None, out_path: Optional[str] = None):
    global token_tracker
    tracker_path = out_path or os.path.join(BASE_DIR, "results", "token_usage.json")
    token_tracker = create_default_tracker(
        "novel_reviewer.py",
        book_name=book_name,
        out_path=tracker_path,
        run_id=run_id,
    )
    return token_tracker


def get_token_tracker():
    return token_tracker


def record_usage(resp):
    try:
        token_tracker.record(resp)
    except Exception:
        pass


def _strip_code_fences(text: str) -> str:
    """去掉 ```json ... ``` 这类代码块包裹，降低 JSON 解析失败概率。"""
    if not text:
        return ""
    t = str(text).strip()
    if t.startswith("```"):
        lines = t.splitlines()
        # 去掉首行 ```xxx
        if len(lines) >= 2 and lines[0].strip().startswith("```"):
            lines = lines[1:]
        # 去掉末行 ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _extract_first_json_object(text: str) -> Optional[str]:
    """
    从一段可能包含多余文本的输出中，提取第一个完整的 JSON 对象（最外层 {...}）。
    解决 'Extra data' / 'Expecting value' / 代码块等常见问题。
    """
    if not text:
        return None
    ss = _strip_code_fences(text).strip()
    if ss.startswith("{") and ss.endswith("}"):
        return ss
    start = ss.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(ss)):
        ch = ss[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
            continue
        else:
            if ch == "\"":
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return ss[start : i + 1].strip()
    return None


def _safe_json_loads_maybe(text: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    尝试从模型输出中解析 JSON 对象。成功返回(dict,"")，失败返回(None,错误原因)。
    """
    if text is None:
        return None, "message.content 为 None"
    raw = str(text).strip()
    if not raw:
        return None, "message.content 为空"
    candidate = _extract_first_json_object(raw) or raw
    try:
        obj = json.loads(candidate)
        if not isinstance(obj, dict):
            return None, f"解析到非对象类型: {type(obj)}"
        return obj, ""
    except Exception as e:
        snippet = raw[:120].replace("\n", "\\n")
        return None, f"JSON解析失败: {e}; raw_head={snippet}"

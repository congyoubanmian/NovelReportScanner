import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import concurrent.futures
from typing import Any, Dict, Optional, Tuple

from openai import OpenAI

from Timerror import extract_status_code, is_retryable_connection_error, is_timeout_error, make_chat_completion
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
logger = logging.getLogger("reviewer")


def read_int_env(name: str, default: int, *, min_value: int = 0, max_value: int = None) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def read_float_env(name: str, default: float, *, min_value: float = 0.0, max_value: float = None) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    try:
        value = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


_read_int_env = read_int_env


# 并发线程数：按环境值执行，避免 Web 端显示值与实际请求并发不一致。
MAX_WORKERS = read_int_env("MAX_WORKERS", 8, min_value=1)
LOG_MAX_BYTES = read_int_env("LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUP_COUNT = read_int_env("LOG_BACKUP_COUNT", 5)
SCAN_FUTURE_STALL_TIMEOUT_SECONDS = read_float_env("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", 0.0, min_value=0.0)


def cancel_pending_futures(futures, current_future=None, executor=None):
    for future in futures:
        if future is current_future or future.done():
            continue
        future.cancel()
    if executor is not None:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)


def iter_completed_futures(futures, phase_name="", timeout_seconds=None, executor=None):
    timeout = SCAN_FUTURE_STALL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        timeout = float(timeout or 0)
    except (TypeError, ValueError):
        timeout = 0.0

    if timeout <= 0:
        yield from concurrent.futures.as_completed(futures)
        return

    pending = set(futures)
    while pending:
        done, pending = concurrent.futures.wait(
            pending,
            timeout=timeout,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            cancel_pending_futures(pending, executor=executor)
            prefix = f"{phase_name} " if phase_name else ""
            raise TimeoutError(
                f"{prefix}future stall timeout after {timeout:g}s without completed task"
            )
        for future in done:
            yield future


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
DEFAULT_MAX_SERVER_ERROR_RETRIES = read_int_env("API_SERVER_ERROR_MAX_RETRIES", 2, min_value=1)
DEFAULT_SERVER_ERROR_FAST_FAIL_INPUT_CHARS = read_int_env("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS", 20000, min_value=0)
DEFAULT_REQUEST_TIMEOUT = 120  # 请求超时时间（秒）
CONTEXT_OVERFLOW_ERROR_HINTS = (
    "context length",
    "maximum context",
    "context window",
    "context overflow",
    "maximum tokens",
    "too many tokens",
    "tokens exceed",
    "token limit",
    "prompt too long",
    "input too long",
    "request too large",
    "string too long",
    "maximum request",
    "context_length_exceeded",
)

# Backward-compatible aliases for older imports.
MAX_403_RETRIES = DEFAULT_MAX_403_RETRIES
MAX_TIMEOUT_RETRIES = DEFAULT_MAX_TIMEOUT_RETRIES
MAX_SERVER_ERROR_RETRIES = DEFAULT_MAX_SERVER_ERROR_RETRIES
SERVER_ERROR_FAST_FAIL_INPUT_CHARS = DEFAULT_SERVER_ERROR_FAST_FAIL_INPUT_CHARS
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
    max_server_error_retries: int = DEFAULT_MAX_SERVER_ERROR_RETRIES,
    max_server_error_fast_fail_input_chars: int = DEFAULT_SERVER_ERROR_FAST_FAIL_INPUT_CHARS,
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
        max_server_error_retries=max_server_error_retries,
        max_server_error_fast_fail_input_chars=max_server_error_fast_fail_input_chars,
        base_delay=base_delay,
        logger=logger,
    )


def is_context_overflow_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(hint in text for hint in CONTEXT_OVERFLOW_ERROR_HINTS)


def is_retryable_transport_error(exc: Exception) -> bool:
    status_code = extract_status_code(exc)
    if status_code in (429, 500, 502, 503, 504):
        return True
    return is_timeout_error(exc) or is_retryable_connection_error(exc)


def should_retry_without_json_mode_error(exc: Exception) -> bool:
    """Only remove response_format for JSON-mode compatibility errors.

    Transport/provider failures must bubble up so Timerror's retry budget remains
    meaningful instead of doubling the same failed request.
    """
    if is_retryable_transport_error(exc):
        return False
    text = str(exc or "").lower()
    json_mode_hints = (
        "response_format",
        "json mode",
        "json_object",
        "unsupported",
        "not support",
        "不支持",
        "无法识别",
        "invalid parameter",
        "invalid_request",
    )
    return any(hint in text for hint in json_mode_hints)


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


def _slice_first_to_last_json_object(text: str) -> Optional[str]:
    ss = _strip_code_fences(text).strip()
    start = ss.find("{")
    end = ss.rfind("}")
    if start < 0 or end <= start:
        return None
    return ss[start : end + 1].strip()


def _normalize_fullwidth_json_punctuation(text: str) -> str:
    if not text:
        return ""
    mapping = {
        "［": "[",
        "］": "]",
        "｛": "{",
        "｝": "}",
        "，": ",",
        "：": ":",
        "“": '"',
        "”": '"',
    }
    out = []
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        mapped = mapping.get(ch, ch)
        out.append(mapped)
        if mapped == '"':
            in_str = True
    return "".join(out)


def _remove_ascii_control_chars(text: str) -> str:
    return "".join(
        ch for ch in str(text or "")
        if ch in "\t\n\r" or ord(ch) >= 32
    )


def _sanitize_json_preview(text: Any, max_chars: int = 220) -> str:
    raw = str(text or "")[:max_chars]
    return (
        raw.replace("\\", "\\\\")
        .replace("\x00", "\\x00")
        .replace("\x1b", "\\x1b")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def diagnose_json_response_text(content: Any) -> Dict[str, Any]:
    text = "" if content is None else str(content)
    stripped = text.strip()
    flags = []
    if content is None:
        flags.append("content_none")
    if not stripped:
        flags.append("content_empty")
    if stripped.startswith("```") and not stripped.endswith("```"):
        flags.append("code_fence_unclosed")
    if stripped.startswith("```"):
        flags.append("code_fence_wrapped")

    in_str = False
    escape = False
    brace_depth = 0
    bracket_depth = 0
    for ch in stripped:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            continue
        if ch == "\"":
            in_str = True
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1

    if in_str:
        flags.append("json_string_unclosed")
    if brace_depth != 0 or bracket_depth != 0:
        flags.append("json_unbalanced")
    if stripped and stripped[-1] not in ("}", "]", "`") and (brace_depth > 0 or bracket_depth > 0 or in_str):
        flags.append("likely_truncated")
    if len(stripped) >= 5500 and (brace_depth > 0 or bracket_depth > 0):
        flags.append("near_max_tokens_truncated")

    return {
        "length": len(stripped),
        "flags": sorted(set(flags)),
        "brace_depth": brace_depth,
        "bracket_depth": bracket_depth,
        "tail": _sanitize_json_preview(stripped[-160:], max_chars=160),
    }


def json_response_looks_truncated(diagnostic: Dict[str, Any]) -> bool:
    flags = set((diagnostic or {}).get("flags") or [])
    return bool(flags & {
        "code_fence_unclosed",
        "json_string_unclosed",
        "json_unbalanced",
        "likely_truncated",
        "near_max_tokens_truncated",
    })


def format_json_response_diagnostic(diagnostic: Dict[str, Any]) -> Tuple[str, str]:
    flags = ",".join((diagnostic or {}).get("flags") or ["none"])
    return (
        flags,
        f"response_flags={flags}; response_len={(diagnostic or {}).get('length', 0)}",
    )


def _escape_unescaped_quotes_in_json_strings(text: str) -> str:
    """
    Repair a common LLM JSON failure:
    {"evidence": "他说"这是真的"，随后离开"}

    A quote inside a JSON string is treated as a literal quote unless the next
    non-space character looks like JSON structure.
    """
    if not text:
        return ""
    structural_after_quote = {":", ",", "}", "]"}
    out = []
    in_str = False
    escape = False
    length = len(text)
    for idx, ch in enumerate(text):
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch != '"':
            out.append(ch)
            continue

        next_idx = idx + 1
        while next_idx < length and text[next_idx].isspace():
            next_idx += 1
        if next_idx >= length or text[next_idx] in structural_after_quote:
            out.append(ch)
            in_str = False
        else:
            out.append('\\"')
    return "".join(out)


def _json_candidate_variants(text: Any):
    raw = "" if text is None else str(text).strip()
    if not raw:
        return []
    candidates = [
        raw,
        _strip_code_fences(raw).strip(),
        _extract_first_json_object(raw),
        _slice_first_to_last_json_object(raw),
    ]
    seen = set()
    variants = []
    for candidate in candidates:
        if not candidate:
            continue
        for variant in (
            candidate,
            _normalize_fullwidth_json_punctuation(candidate),
            _remove_ascii_control_chars(candidate),
        ):
            if not variant:
                continue
            repaired = _escape_unescaped_quotes_in_json_strings(variant)
            for item in (variant, repaired):
                if item and item not in seen:
                    seen.add(item)
                    variants.append(item)
    return variants


def parse_json_object_lenient(text: Any) -> Dict[str, Any]:
    """
    Parse an LLM JSON object with targeted repairs for Markdown wrappers,
    control characters, full-width JSON punctuation and unescaped quotes inside
    string values.
    """
    if text is None:
        raise json.JSONDecodeError("message.content is None", "", 0)
    if not str(text).strip():
        raise json.JSONDecodeError("message.content is empty", "", 0)

    last_error = None
    for candidate in _json_candidate_variants(text):
        for loader in (
            json.loads,
            lambda value: json.JSONDecoder(strict=False).decode(value),
        ):
            try:
                obj = loader(candidate)
                if isinstance(obj, dict):
                    return obj
                last_error = TypeError(f"解析到非对象类型: {type(obj)}")
            except Exception as exc:
                last_error = exc

    repair_candidates = []
    truncated_diagnostics = []
    for candidate in _json_candidate_variants(text):
        diagnostic = diagnose_json_response_text(candidate)
        if json_response_looks_truncated(diagnostic):
            truncated_diagnostics.append(diagnostic)
            continue
        repair_candidates.append(candidate)

    if not repair_candidates and truncated_diagnostics:
        _flags, err_detail = format_json_response_diagnostic(truncated_diagnostics[0])
        raise json.JSONDecodeError(f"truncated_json_response; {err_detail}", str(text)[:200], 0)

    try:
        from json_repair import repair_json

        for candidate in repair_candidates:
            try:
                try:
                    repaired_obj = repair_json(candidate, return_objects=True)
                except TypeError:
                    repaired_obj = json.loads(repair_json(candidate))
                if isinstance(repaired_obj, dict):
                    return repaired_obj
                last_error = TypeError(f"解析到非对象类型: {type(repaired_obj)}")
            except Exception as exc:
                last_error = exc
    except Exception:
        pass

    if last_error is not None:
        raise json.JSONDecodeError(str(last_error), str(text)[:200], 0)
    raise json.JSONDecodeError("unable to parse json", str(text)[:200], 0)


def _safe_json_loads_maybe(text: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    尝试从模型输出中解析 JSON 对象。成功返回(dict,"")，失败返回(None,错误原因)。
    """
    if text is None:
        return None, "message.content 为 None"
    raw = str(text).strip()
    if not raw:
        return None, "message.content 为空"
    try:
        obj = parse_json_object_lenient(raw)
        return obj, ""
    except Exception as e:
        snippet = raw[:120].replace("\n", "\\n")
        return None, f"JSON解析失败: {e}; raw_head={snippet}"


def call_json_chat_completion_with_fallback(
    *,
    chat_completion_func,
    model: str,
    messages,
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
    record_usage_func=None,
    parse_json_func=None,
) -> Dict[str, Any]:
    json_mode_kwargs = {"response_format": {"type": "json_object"}}

    def response_content(response):
        try:
            choices = getattr(response, "choices", None)
            if choices is None and isinstance(response, dict):
                choices = response.get("choices")
            if not choices:
                return None, "response.choices 为空"
            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            if message is None and isinstance(first_choice, dict):
                message = first_choice.get("message")
            if message is None:
                return None, "response.choices[0].message 为空"
            if isinstance(message, dict):
                return message.get("content"), ""
            return getattr(message, "content", None), ""
        except Exception as exc:
            return None, f"读取 message.content 失败: {exc}"

    def parse_content(content):
        if parse_json_func is not None:
            try:
                data = parse_json_func(content)
                if isinstance(data, dict):
                    return data, ""
                return None, f"解析到非对象类型: {type(data)}"
            except Exception as exc:
                return None, f"JSON解析失败: {exc}"
        return _safe_json_loads_maybe(content)

    base_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        base_kwargs["max_tokens"] = max_tokens

    json_mode_supported = True
    try:
        response = chat_completion_func(
            **base_kwargs,
            **json_mode_kwargs,
        )
        if record_usage_func is not None:
            record_usage_func(response)
        content, content_err = response_content(response)
        data, err = parse_content(content)
        if data is None and content_err:
            err = content_err
        if data is not None:
            return data
    except Exception as exc:
        err = f"JSON mode调用失败: {exc}"
        if not should_retry_without_json_mode_error(exc):
            raise
        json_mode_supported = False

    fallback_messages = list(messages) + [{
        "role": "user",
        "content": (
            "上一次回复不是可解析的 JSON 对象，或当前接口不支持 JSON mode。"
            "请只重新输出一个合法 JSON 对象，不要 Markdown、不要代码块、不要解释。"
        ),
    }]
    fallback_kwargs = dict(base_kwargs)
    fallback_kwargs["messages"] = fallback_messages
    fallback_kwargs["temperature"] = 0.0
    if json_mode_supported:
        fallback_kwargs.update(json_mode_kwargs)
    fallback_response = chat_completion_func(**fallback_kwargs)
    if record_usage_func is not None:
        record_usage_func(fallback_response)

    fallback_content, fallback_content_err = response_content(fallback_response)
    fallback_data, fallback_err = parse_content(fallback_content)
    if fallback_data is None and fallback_content_err:
        fallback_err = fallback_content_err
    if fallback_data is None:
        raise ValueError(f"{err}; fallback={fallback_err}")
    return fallback_data

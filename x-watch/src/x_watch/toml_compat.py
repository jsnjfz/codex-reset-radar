"""TOML 读取兼容层。

优先顺序：标准库 tomllib (Python 3.11+) → 第三方 tomli → 内置最小子集解析器。

内置解析器只支持本项目配置文件用到的语法：注释、`[table]`、`[[array of tables]]`、
以及 字符串/整数/浮点/布尔 标量。遇到不支持的语法会明确报错，而不是猜测语义 ——
配置被静默解析错会直接影响采集范围和预算。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

__all__ = ["load_toml", "TomlError", "BACKEND"]


class TomlError(ValueError):
    """配置文件语法错误。"""


try:  # Python 3.11+
    import tomllib as _toml

    BACKEND = "tomllib"
except ModuleNotFoundError:  # pragma: no cover - 取决于运行环境
    try:
        import tomli as _toml  # type: ignore

        BACKEND = "tomli"
    except ModuleNotFoundError:
        _toml = None
        BACKEND = "builtin-subset"


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def load_toml(path: str) -> Dict[str, Any]:
    if _toml is not None:
        with open(path, "rb") as fh:
            try:
                return _toml.load(fh)
            except Exception as exc:  # tomllib.TOMLDecodeError 等
                raise TomlError("%s: %s" % (path, exc)) from exc
    with open(path, "r", encoding="utf-8") as fh:
        return _parse_subset(fh.read(), path)


def _parse_subset(text: str, path: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    # 当前写入目标。[[a]] 之后，后续 key 落在数组最后一个元素上。
    target: Dict[str, Any] = root

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue

        def fail(msg: str) -> "TomlError":
            return TomlError("%s:%d: %s" % (path, lineno, msg))

        if line.startswith("[["):
            if not line.endswith("]]"):
                raise fail("数组表头未闭合：%r" % raw_line.strip())
            name = line[2:-2].strip()
            if not _KEY_RE.match(name):
                raise fail("不支持的表名 %r（内置解析器只支持顶层单段表名）" % name)
            bucket = root.setdefault(name, [])
            if not isinstance(bucket, list):
                raise fail("%r 已被定义为非数组值" % name)
            target = {}
            bucket.append(target)
            continue

        if line.startswith("["):
            if not line.endswith("]"):
                raise fail("表头未闭合：%r" % raw_line.strip())
            name = line[1:-1].strip()
            if not _KEY_RE.match(name):
                raise fail("不支持的表名 %r（内置解析器只支持顶层单段表名）" % name)
            existing = root.get(name)
            if existing is None:
                target = {}
                root[name] = target
            elif isinstance(existing, dict):
                target = existing
            else:
                raise fail("%r 已被定义为非表值" % name)
            continue

        if "=" not in line:
            raise fail("无法识别的行：%r" % raw_line.strip())

        key, _, value_text = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            raise fail("不支持的键名 %r" % key)
        if key in target:
            raise fail("键 %r 重复定义" % key)
        target[key] = _parse_value(value_text.strip(), fail)

    return root


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不破坏字符串里的 `#`。"""
    out: List[str] = []
    quote = ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _parse_value(text: str, fail) -> Any:
    if not text:
        raise fail("键缺少值")
    if text[0] in ('"', "'"):
        if len(text) < 2 or text[-1] != text[0]:
            raise fail("字符串未闭合：%r" % text)
        body = text[1:-1]
        if text[0] == "'":
            return body  # 字面量字符串，不处理转义
        return _unescape(body, fail)
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("["):
        raise fail("内置 TOML 解析器不支持内联数组；请安装 Python 3.11+ 或 tomli")
    if text.startswith("{"):
        raise fail("内置 TOML 解析器不支持内联表；请安装 Python 3.11+ 或 tomli")
    cleaned = text.replace("_", "")
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        pass
    raise fail("无法识别的值 %r（内置解析器只支持字符串/整数/浮点/布尔）" % text)


_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _unescape(body: str, fail) -> str:
    out: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise fail("字符串以孤立的反斜杠结束")
        esc = body[i]
        if esc in _ESCAPES:
            out.append(_ESCAPES[esc])
            i += 1
        elif esc == "u":
            hex_part = body[i + 1 : i + 5]
            if len(hex_part) != 4:
                raise fail("\\u 转义需要 4 位十六进制")
            out.append(chr(int(hex_part, 16)))
            i += 5
        else:
            raise fail("不支持的转义 \\%s" % esc)
    return "".join(out)

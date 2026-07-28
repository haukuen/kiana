"""扫描所有插件的 Pydantic 配置，生成 form schema。

核心入口：`scan_all_plugins(plugins_dir)`。
对每个 `plugins_dir/<name>/config.py`：
1. 用 importlib 从文件路径加载（避免触发 NoneBot 插件包 __init__）。
2. 取出 `Config` 类，遍历 `Config.model_fields`。
3. 按类型映射规则生成 `FieldSchema`。
单插件失败不影响整体扫描——catch + logging.warning。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import logging
import tokenize
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

logger = logging.getLogger("web_config.scanner")


# ── schema 数据结构 ────────────────────────────────────────────


@dataclass
class FieldSchema:
    """单个配置字段的 form schema。"""

    key: str
    label: str
    type: str  # bool|int|float|string|enum|list_string|list_object|json
    default: Any
    secret: bool = False
    description: str | None = None
    min: float | None = None
    max: float | None = None
    options: list[str] | None = None
    subfields: list[FieldSchema] | None = None


@dataclass
class PluginSchema:
    """单个插件的 form schema。"""

    plugin_name: str
    display_name: str
    description: str
    fields: list[FieldSchema] = field(default_factory=list)


# ── 工具函数 ──────────────────────────────────────────────────


def humanize(key: str) -> str:
    """把字段名转成可读标签。

    xiaohongshu_cookie -> "Xiaohongshu Cookie"
    MAX_VIDEO_SIZE -> "Max Video Size"
    """
    parts = key.replace("__", "_").split("_")
    return " ".join(p.capitalize() for p in parts if p)


def _extract_secret(field_info: FieldInfo) -> bool:
    """从 json_schema_extra 取 secret 标记（dict 形式）。callable 形式不支持。"""
    extra = field_info.json_schema_extra
    if isinstance(extra, dict):
        return bool(extra.get("secret", False))
    return False


def _extract_bounds(field_info: FieldInfo) -> tuple[float | None, float | None]:
    """从 pydantic metadata 提取 min/max。

    Ge/Gt -> min, Le/Lt -> max。
    """
    min_val: float | None = None
    max_val: float | None = None
    for constraint in field_info.metadata:
        # pydantic 的数值约束对象有 gt/ge/lt/le 属性
        ge = getattr(constraint, "ge", None)
        gt = getattr(constraint, "gt", None)
        le = getattr(constraint, "le", None)
        lt = getattr(constraint, "lt", None)
        if ge is not None:
            min_val = float(ge)
        if gt is not None:
            # gt 是严格大于；前端用 min 即可，略偏保守
            min_val = float(gt)
        if le is not None:
            max_val = float(le)
        if lt is not None:
            max_val = float(lt)
    return min_val, max_val


def _field_name_of(node: ast.AST) -> str | None:
    """判断 AST 节点是否为字段定义,返回字段名或 None。

    支持:
    - ``name: type [= value]`` (AnnAssign)
    - ``name = value`` (旧式 Assign,无 type hint)
    """
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    return None


def extract_inline_comments(source: str) -> dict[str, str]:
    """从 Python 源码解析类字段的行内注释。

    返回 {field_name: comment_text}。仅识别 ClassDef body 下的字段定义:
    - ``name: type = value  # comment`` (AnnAssign)
    - ``name: type  # comment`` (AnnAssign 无赋值)
    - ``name = value  # comment`` (旧式 Assign,无 type hint)

    用 ``tokenize`` 扫描语句最后一行,避免字符串字面量里的 ``#`` 误判。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    lines = source.splitlines()
    result: dict[str, str] = {}

    for node in ast.walk(tree):
        field_name = _field_name_of(node)
        if field_name is None:
            continue

        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is None or end_lineno > len(lines):
            continue
        line = lines[end_lineno - 1]

        comment = _line_inline_comment(line)
        if comment:
            result[field_name] = comment

    return result


def _line_inline_comment(line: str) -> str | None:
    """用 tokenize 提取单行的行内注释,返回去掉 ``#`` 和首尾空白后的文本。

    无注释或 tokenize 失败时返回 None。用 tokenize 避免字符串字面量里的
    ``#`` 被误判。
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError):
        return None
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comment = tok.string.lstrip("#").strip()
            return comment or None
    return None


# ── 字段内省 ──────────────────────────────────────────────────


def _introspect_field(
    key: str,
    field_info: FieldInfo,
    inline_comment: str | None = None,
) -> FieldSchema:
    """根据 FieldInfo 生成 FieldSchema。

    description 优先级: Field(description=...) > 行内注释 > humanize 兜底。
    """
    annotation = field_info.annotation
    description = field_info.description or inline_comment
    label = description or humanize(key)

    ftype, options, subfields = _classify_type(annotation)

    secret = _extract_secret(field_info)
    min_val, max_val = _extract_bounds(field_info)

    # required 字段(Field(...))的默认值是 PydanticUndefined,JSON 序列化会失败,转 None
    default = field_info.get_default(call_default_factory=True)
    if default is PydanticUndefined:
        default = None

    return FieldSchema(
        key=key,
        label=label,
        type=ftype,
        default=default,
        secret=secret,
        description=description,
        min=min_val,
        max=max_val,
        options=options,
        subfields=subfields,
    )


def _classify_type(
    annotation: Any,
) -> tuple[str, list[str] | None, list[FieldSchema] | None]:
    """把 annotation 映射成 (type, options, subfields)。"""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Literal[...] -> enum
    if origin is typing.Literal:
        opts = [str(a) for a in args]
        return "enum", opts, None

    # list[X]
    if origin in (list, typing.List):  # noqa: UP006
        return _classify_list(args)

    # dict / dict[...] -> json
    if annotation is dict or origin in (dict, typing.Dict):  # noqa: UP006
        return "json", None, None

    # 基础类型
    if annotation is bool:
        return "bool", None, None
    if annotation is int:
        return "int", None, None
    if annotation is float:
        return "float", None, None
    if annotation is str:
        return "string", None, None

    # 其他未知类型 -> string 兜底 + warning
    logger.warning("未知字段类型 %r,回退为 string", annotation)
    return "string", None, None


def _classify_list(
    args: tuple[Any, ...],
) -> tuple[str, list[str] | None, list[FieldSchema] | None]:
    """处理 list[X] 的子类型识别。"""
    if not args:
        return "list_string", None, None
    inner = args[0]
    # list[BaseModel 子类] -> list_object
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        subs = _introspect_model(inner)
        return "list_object", None, subs
    # list[str]
    if inner is str:
        return "list_string", None, None
    # 其他 list 类型 -> 当 json 处理
    return "json", None, None


def _introspect_model(model_cls: type[BaseModel]) -> list[FieldSchema]:
    """递归内省一个 BaseModel 子类的所有字段。"""
    result: list[FieldSchema] = []
    for key, field_info in model_cls.model_fields.items():
        try:
            result.append(_introspect_field(key, field_info))
        except Exception:
            logger.exception("内省子字段 %s.%s 失败", model_cls.__name__, key)
    return result


# ── 元信息提取 ────────────────────────────────────────────────


def _read_plugin_meta(plugin_dir: Path, plugin_name: str) -> tuple[str, str]:
    """尽量从 __init__.py 的 __plugin_meta__ 读 display_name / description。

    读不到就 fallback 用 plugin_name。这里同样用文件加载方式，
    但 __init__.py 通常会调用 get_plugin_config() -> 触发 NoneBot 初始化，
    所以一旦 exec 失败就 fallback，绝不抛出。
    """
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        return plugin_name, ""

    try:
        # 用唯一模块名加载，避免污染 sys.modules
        mod_name = f"web_config._scan_meta_{plugin_name}"
        spec = importlib.util.spec_from_file_location(mod_name, init_file)
        if spec is None or spec.loader is None:
            return plugin_name, ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta = getattr(module, "__plugin_meta__", None)
        if meta is None:
            return plugin_name, ""
        display = getattr(meta, "name", None) or plugin_name
        desc = getattr(meta, "description", None) or ""
        return str(display), str(desc)
    except Exception:
        # __init__.py 多半会触发 NoneBot 初始化或导入副作用，吞掉即可
        logger.debug("读取 %s 的 __plugin_meta__ 失败,使用 fallback", plugin_name, exc_info=True)
        return plugin_name, ""


# ── 单插件扫描 ────────────────────────────────────────────────


def _load_config_module(plugin_name: str, config_path: Path) -> Any:
    """用文件路径加载 config.py，避免触发 src.plugins.<name>.__init__。"""
    mod_name = f"web_config._scan_config_{plugin_name}"
    spec = importlib.util.spec_from_file_location(mod_name, config_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {config_path} 创建模块 spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan_plugin(plugin_dir: Path) -> PluginSchema | None:
    """扫描单个插件目录,失败返回 None。"""
    plugin_name = plugin_dir.name
    config_path = plugin_dir / "config.py"
    if not config_path.is_file():
        return None

    # 加载 config 模块
    try:
        module = _load_config_module(plugin_name, config_path)
    except Exception:
        logger.warning("加载插件 %s 的 config.py 失败,跳过", plugin_name, exc_info=True)
        return None

    config_cls = getattr(module, "Config", None)
    if config_cls is None or not (
        isinstance(config_cls, type) and issubclass(config_cls, BaseModel)
    ):
        logger.warning("插件 %s 未找到有效的 Config(BaseModel) 类,跳过", plugin_name)
        return None

    # 解析 config.py 源码的行内注释,作为 description 的 fallback 来源
    try:
        source = config_path.read_text(encoding="utf-8")
        inline_comments = extract_inline_comments(source)
    except OSError:
        logger.debug("读取 %s 源码失败,跳过行内注释解析", plugin_name, exc_info=True)
        inline_comments = {}

    # 内省字段
    field_schemas: list[FieldSchema] = []
    for key, field_info in config_cls.model_fields.items():
        try:
            field_schemas.append(
                _introspect_field(key, field_info, inline_comments.get(key))
            )
        except Exception:
            logger.exception("内省插件 %s 的字段 %s 失败,跳过该字段", plugin_name, key)

    # 元信息(display_name / description)
    display_name, description = _read_plugin_meta(plugin_dir, plugin_name)

    return PluginSchema(
        plugin_name=plugin_name,
        display_name=display_name,
        description=description,
        fields=field_schemas,
    )


# ── 总入口 ────────────────────────────────────────────────────


def scan_all_plugins(plugins_dir: Path) -> list[PluginSchema]:
    """扫描 plugins_dir 下所有含 config.py 的子目录,返回 PluginSchema 列表。

    任一插件失败都不会影响其他插件。结果按 plugin_name 排序。
    """
    if not plugins_dir.is_dir():
        logger.warning("插件目录不存在: %s", plugins_dir)
        return []

    results: list[PluginSchema] = []
    for sub in sorted(plugins_dir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name.startswith("_") or sub.name.startswith("."):
            continue
        schema = scan_plugin(sub)
        if schema is not None:
            results.append(schema)

    return results

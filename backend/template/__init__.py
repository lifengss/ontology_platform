"""template：预置行业模板（LinkML）+ 网络加载预览外部模板（VS5，P1）。

V2-19（M4-F）：支持从网络 URL / registry 加载外部 LinkML 模板，
预览其结构（对象/属性/约束/许可声明），并一键实例化为本地可编辑本体。
离线环境优雅降级（不可达提示＋本地模板库），恶意/超大/格式非法模板一律拒绝。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

try:
    import yaml
    _HAVE_YAML = True
except ImportError:  # pragma: no cover
    _HAVE_YAML = False

from logging_setup import get_logger

logger = get_logger("template")

# 网络加载护栏：仅允许 http/https（禁止 file:// 等本地文件读取）。
ALLOWED_SCHEMES = ("http", "https")
# 单次加载最大字节数（防超大模板 DoS）。
MAX_TEMPLATE_BYTES = 5 * 1024 * 1024  # 5 MiB
# 请求超时（秒）。
FETCH_TIMEOUT_SECONDS = 10.0
# 结构维度上限：对象数/属性数（防恶意超大结构撑爆内存/翻译耗时）。
MAX_OBJECTS = 200
MAX_ATTRIBUTES_PER_OBJECT = 200

# LinkML 顶层常见许可声明字段（各取其一即认定为「有许可声明」）。
LICENSE_KEYS = ("license", "license_text", "license_url")


class TemplateError(Exception):
    """模板加载/解析/翻译异常（面向用户的语义化错误）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TemplateManager:
    """外部 LinkML 模板加载、预览与实例化。"""

    def list_templates(self) -> list[dict]:
        """返回内置模板清单（本地模板库，离线可用）。"""
        from templates import BUILTIN_TEMPLATES
        return BUILTIN_TEMPLATES

    # ------------------------------------------------------------------
    # 网络加载
    # ------------------------------------------------------------------
    def load_external(self, url: str) -> dict[str, Any]:
        """从 URL 拉取并解析 LinkML 模板，返回平台模型字典。

        失败语义：网络不可达 → 抛 TemplateError("NETWORK"/"TIMEOUT")；
        内容超大 → "TOO_LARGE"；YAML 非法/结构非法 → "MALFORMED"；
        协议不允许 → "UNSAFE_SCHEME"。
        """
        self._check_scheme(url)
        text = self._fetch(url)
        return self.parse_linkml(text)

    def _check_scheme(self, url: str) -> None:
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in ALLOWED_SCHEMES:
            raise TemplateError(
                "UNSAFE_SCHEME",
                f"仅允许 http/https 模板地址，拒绝: {url[:64]}",
            )

    def _fetch(self, url: str) -> str:
        try:
            resp = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.TimeoutException as e:
            raise TemplateError("TIMEOUT", f"模板加载超时（{FETCH_TIMEOUT_SECONDS}s）：{url}") from e
        except httpx.HTTPError as e:
            raise TemplateError("NETWORK", f"模板加载失败（网络不可达）：{e}") from e
        if resp.status_code != 200:
            raise TemplateError("NETWORK", f"模板加载失败（HTTP {resp.status_code}）：{url}")
        body = resp.content
        if len(body) > MAX_TEMPLATE_BYTES:
            raise TemplateError(
                "TOO_LARGE",
                f"模板内容过大（{len(body)} 字节 > {MAX_TEMPLATE_BYTES}），已拒绝加载",
            )
        # 只处理 UTF-8 文本；二进制类型（如 zip）由文本解码失败而拒绝
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as e:
            raise TemplateError("MALFORMED", "模板不是有效的 UTF-8 文本，拒绝加载") from e

    # ------------------------------------------------------------------
    # 解析与翻译（LinkML 子集 → 平台 OntologyCreate）
    # ------------------------------------------------------------------
    def parse_linkml(self, text: str) -> dict[str, Any]:
        """把 LinkML YAML 文本解析为平台本体模型 dict。

        仅处理 LinkML 常用子集：顶层 `classes`（类→对象）、
        类内 `attributes` / `slots`（属性）、`related_to`-类成员（关系，
        简化映射：对象关系由 attr 的 `range` 指向其他类时识别）、
        顶层 `license` 等许可声明。
        """
        if not _HAVE_YAML:
            raise TemplateError("MALFORMED", "yaml 库不可用，无法解析 LinkML 模板")
        if not text or not text.strip():
            raise TemplateError("MALFORMED", "模板内容为空")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise TemplateError("MALFORMED", f"LinkML YAML 解析失败: {e}") from e
        if not isinstance(data, dict):
            raise TemplateError("MALFORMED", "LinkML 顶层必须是 YAML 映射（dict）")

        classes = data.get("classes")
        if not isinstance(classes, dict) or not classes:
            raise TemplateError(
                "MALFORMED",
                "LinkML 模板缺少 `classes`（类定义）或其为空，无法识别对象结构",
            )
        if len(classes) > MAX_OBJECTS:
            raise TemplateError("TOO_LARGE", f"模板类数量 {len(classes)} 超过上限 {MAX_OBJECTS}")

        objects = self._translate_classes(classes)
        model = {
            "name": str(data.get("name") or "外部 LinkML 模板"),
            "description": (data.get("description") or "").strip() or None,
            "modeling_type": "manual",
            "version": str(data.get("version") or "1"),
            "objects": objects,
            "constraints": [],
            "functions": [],
        }
        # 把许可声明并入描述，供预览与实例化后的人工审阅
        lic = self._extract_license(data)
        if lic:
            model["description"] = (
                (model["description"] + " " if model["description"] else "") + lic
            )
        # 结构级校验（对象引用完整性），异常即拒绝（不生成半成品本体）
        self._validate_model(model)
        return model

    def _translate_classes(self, classes: dict) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for cid, cdef in classes.items():
            if not isinstance(cdef, dict):
                cdef = {"name": cid}
            attrs: list[dict[str, Any]] = []
            attr_specs: dict[str, dict] = {}
            for key in ("attributes", "slots"):
                for aid, adef in (cdef.get(key) or {}).items():
                    if not isinstance(adef, dict):
                        adef = {"name": aid}
                    attr_specs[aid] = adef
            # stable_key：优先取类内 `id` 属性（LinkML 常用），否则取首个字符串 required 属性
            stable_key = None
            for aid, adef in attr_specs.items():
                if aid in ("id", "identifier") or adef.get("identifier"):
                    stable_key = aid
                    break
            for aid, adef in attr_specs.items():
                attrs.append(self._translate_attribute(aid, adef))
            if len(attrs) > MAX_ATTRIBUTES_PER_OBJECT:
                raise TemplateError(
                    "TOO_LARGE",
                    f"对象 {cid} 属性数量 {len(attrs)} 超过上限 {MAX_ATTRIBUTES_PER_OBJECT}",
                )
            # 关系：attr 的 `range` 指向其他类 → 平台关系（many_to_one 保守）
            relations: list[dict[str, Any]] = []
            classes_set = set(classes.keys())
            for aid, adef in attr_specs.items():
                rng = adef.get("range")
                if isinstance(rng, str) and rng in classes_set and rng != cid:
                    relations.append({
                        "id": aid,
                        "name": adef.get("name", aid),
                        "target_object": rng,
                        "cardinality": "many_to_one",
                    })
            if stable_key is None:
                # 回退：首个 string 属性
                for a in attrs:
                    if a["data_type"] == "string":
                        stable_key = a["id"]
                        break
            objects.append({
                "id": _sanitize_id(cid),
                "name": str(cdef.get("name") or cid),
                "business_definition": (cdef.get("description") or f"{cid} 的业务语义定义"),
                "stable_key": stable_key,
                "stable_key_explicit": False,
                "attributes": attrs,
                "relations": relations,
            })
        return objects

    def _translate_attribute(self, aid: str, adef: dict[str, Any]) -> dict[str, Any]:
        data_type = self._map_type(str(adef.get("range") or "string"))
        required = bool(adef.get("required") or adef.get("identifier"))
        constraints: dict[str, Any] = {}
        # LinkML 常用约束子集
        if adef.get("pattern") is not None:
            constraints["pattern"] = str(adef["pattern"])
        if adef.get("min_length") is not None or adef.get("minValue") is not None:
            constraints["min_length"] = adef.get("min_length") or adef.get("minValue")
        if adef.get("max_length") is not None or adef.get("maxValue") is not None:
            constraints["max_length"] = adef.get("max_length") or adef.get("maxValue")
        if adef.get("minimum") is not None:
            constraints["minimum"] = adef["minimum"]
        if adef.get("maximum") is not None:
            constraints["maximum"] = adef["maximum"]
        if adef.get("enum") is not None:
            constraints["enum"] = list(adef["enum"])
        return {
            "id": _sanitize_id(aid),
            "name": str(adef.get("name") or aid),
            "data_type": data_type,
            "required": required,
            "constraints": constraints,
        }

    @staticmethod
    def _map_type(raw: str) -> str:
        """LinkML 常用数据类型 → 平台 DATA_TYPES（string/integer/float/boolean/date）。"""
        low = raw.strip().lower()
        if low in ("string", "str", "text", "url", "uri", "name", "sentence"):
            return "string"
        if low in ("integer", "int", "unsignedint", "count", "long"):
            return "integer"
        if low in ("float", "double", "decimal", "number"):
            return "float"
        if low in ("boolean", "bool"):
            return "boolean"
        if low in ("date", "date_or_datetime", "datetime"):
            return "date"
        return "string"

    @staticmethod
    def _extract_license(data: dict) -> Optional[str]:
        for key in LICENSE_KEYS:
            val = data.get(key)
            if val:
                return f"许可声明（{key}）：{val}"
        return None

    def _validate_model(self, model: dict[str, Any]) -> None:
        """结构级校验：对象 id 唯一、对象数上限、引用目标存在。

        翻译后立即校验，异常即拒绝，避免后续实例化产出半成品本体。
        """
        from modeling_translator import blocking_violations
        errors = blocking_violations(model)
        if errors:
            # 只把首个 error 提升为拒绝动作，避免把整叠 violations 抛给用户
            raise TemplateError(
                "MALFORMED",
                f"模板翻译为本体后存在结构问题：{errors[0]['message']}",
            )

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------
    def preview_external(self, url: str) -> dict[str, Any]:
        """加载并返回结构预览（对象/属性/约束/许可声明）。"""
        model = self.load_external(url)
        return self._preview_from_model(model)

    def _preview_from_model(self, model: dict[str, Any]) -> dict[str, Any]:
        objects = model.get("objects", [])
        preview_objects = []
        for o in objects:
            preview_objects.append({
                "id": o["id"],
                "name": o.get("name", o["id"]),
                "business_definition": o.get("business_definition"),
                "stable_key": o.get("stable_key"),
                "attributes": [
                    {"id": a["id"], "name": a.get("name", a["id"]),
                     "data_type": a.get("data_type"), "required": a.get("required"),
                     "constraints": a.get("constraints")}
                    for a in o.get("attributes", [])
                ],
                "relations": [
                    {"id": r["id"], "name": r.get("name", r["id"]),
                     "target_object": r.get("target_object"), "cardinality": r.get("cardinality")}
                    for r in o.get("relations", [])
                ],
            })
        return {
            "name": model.get("name"),
            "description": model.get("description"),
            "object_count": len(preview_objects),
            "objects": preview_objects,
            "license": model.get("description"),  # 许可声明并入描述（无独立字段）
        }


def _sanitize_id(raw: str) -> str:
    """把 LinkML 类/属性名规整为平台对象 id（小写下划线风格，兼容中文保留）。"""
    s = raw.strip()
    return s or "x"


manager = TemplateManager()

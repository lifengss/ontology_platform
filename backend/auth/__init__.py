"""V2-05 平台自身登录鉴权与 RBAC（M2-A）。

设计口径（与 docs/API-INTERFACE-DOC.md §21 一致）：
- JWT 只携带身份（sub/jti/iat/exp），**不携带角色与权限**；每次请求实时查用户与授权，
  因此角色调整免重登即时生效，过期/篡改的 token 不残留任何权限。
- 主凭据为 httpOnly + SameSite=Lax Cookie；`mode=bearer` 登录供非浏览器 API 客户端。
- MCP 项目静态 token 是另一套体系（/mcp/* 由 mcp_server 自行校验），本过滤器放行。
- 密码 PBKDF2-HMAC-SHA256；JWT HS256（固定 alg、恒定时间签名比较），仅用标准库，
  不引入第三方依赖，便于离线安装包分发。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from config import get_settings
from core.storage import JsonStore

COOKIE_NAME = "op_session"
TOKEN_TTL_SECONDS = 8 * 3600
LOCK_THRESHOLD = 5
LOCK_SECONDS = 300
SSO_ENABLED = False

ROLES: dict[str, dict[str, Any]] = {
    "admin": {"label": "管理员", "actions": ["read", "write", "review", "admin"]},
    "modeler": {"label": "建模人", "actions": ["read", "write"]},
    "reviewer": {"label": "审核人", "actions": ["read", "review"]},
    "viewer": {"label": "只读", "actions": ["read"]},
}

GRANT_LEVELS: dict[str, list[str]] = {
    "read": ["read"],
    "write": ["read", "write"],
    "review": ["read", "review"],
    "none": [],
}

# 本地开发/离线部署允许任意本机端口（安装器会动态重分配端口）；不开放任意外部 Origin。
LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.I)

# 无需登录即可访问的路径（页面静态资源由前端静态服务提供，此处也放行后端根路径探测）
PUBLIC_EXACT_PATHS = {
    "/", "/index.html", "/app.js", "/styles.css", "/favicon.ico",
    "/health", "/docs", "/redoc", "/openapi.json",
    "/api/client-logs",
    "/api/auth/login", "/api/auth/logout",
    "/api/auth/sso/config", "/api/auth/sso/start",
}
PUBLIC_PREFIXES = ("/mcp/", "/tutorial/", "/assets/", "/frontend/")

# 登录与管理端点（/api/auth/* 除放行项外均要求 admin 或登录态）
AUTH_ADMIN_PREFIXES = ("/api/auth/users", "/api/auth/roles", "/api/auth/audit", "/api/audit")

# 只读/计算型 POST：按方法粗切成 write 会误伤只读角色与审核角色
READ_ONLY_POST_RE = re.compile(
    r"^/api/(?:"
    r"ontologies/[^/]+/(?:validate|export|snapshot-list)"
    r"|ontologies/validate"
    r"|capabilities/[^/]+/validate"
    r"|dictionary/term-lookup"
    r"|dictionary/field-map-sources"
    r"|graph/[^/]+/nl-query"
    r"|graph/[^/]+/inference/impact"
    r"|inference/impact"
    r"|ingest/preview"
    r"|actions/[^/]+/execute"
    r"|alignment/candidates"
    r"|mcp/connections/[^/]+/(?:test|read)"
    r"|mcp/server/tools"
    r"|mcp/server/status"
    r"|llm/test"
    r"|system/info"
    # 个人会话操作：修改本人密码（需有效登录态，但不要求 write 权限）
    r"|auth/change-password"
    # V2-15（M4-B）：开放查询接口（SPARQL/GraphQL 为只读计算型 POST）
    r"|query/sparql"
    r"|query/graphql"
    r")$"
)
# V2-07（M2-E）：变更单 approve/reject 属审批动作（review 角色可执行）；
# withdraw/abandon 保持 write（仅提交人，路由内二次校验身份）。
REVIEW_POST_RE = re.compile(
    r"^/api/(?:ingest/(?:candidates/[^/]+/(?:approve|reject|revert)|batch-approve|batch-reject)"
    r"|changesets/[^/]+/(?:approve|reject)"
    # V2-07（M2-E）：变更单审批/驳回为 review 动作（提交路径为 /api/ontologies/{id}/changesets/{csid}/...）
    r"|ontologies/[^/]+/changesets/[^/]+/(?:approve|reject))$"
)

_secret_cache: str | None = None


# --------------------------------------------------------------------------- #
# 配置开关
# --------------------------------------------------------------------------- #
def auth_enabled() -> bool:
    """默认启用。仅本地开发/自动化回归可显式关闭（ONTOLOGY_AUTH_ENABLED=false）。

    注意：这是测试态豁免，不是安全特性；生产部署严禁设置该变量。
    每次请求读取（而非模块级常量），使 pytest/验收脚本可在运行时切换。
    """
    val = os.getenv("ONTOLOGY_AUTH_ENABLED")
    if val is None:
        return True
    return val.strip().lower() in ("1", "true", "yes", "on")


def _pbkdf2_iterations() -> int:
    """默认 600000（OWASP PBKDF2-HMAC-SHA256 建议）；自动化测试可调低加速。"""
    try:
        return max(10_000, int(os.getenv("ONTOLOGY_PBKDF2_ITERATIONS", "600000")))
    except ValueError:
        return 600_000


def _secret() -> str:
    global _secret_cache
    env = os.getenv("ONTOLOGY_AUTH_SECRET")
    if env:
        return env
    if _secret_cache:
        return _secret_cache
    path = pathlib.Path(get_settings().secrets_dir) / "auth_secret.json"
    if path.exists():
        try:
            _secret_cache = json.loads(path.read_text(encoding="utf-8"))["secret"]
            return _secret_cache
        except Exception:  # noqa: BLE001
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _secret_cache = secrets.token_urlsafe(48)
    path.write_text(json.dumps({"secret": _secret_cache}), encoding="utf-8")
    return _secret_cache


def _now() -> int:
    return int(time.time())


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(collection: str) -> JsonStore:
    """用户/审计/撤销名单是全局数据，放共享库而非某个项目目录。"""
    return JsonStore(collection, project_id="_shared")


# --------------------------------------------------------------------------- #
# 密码与 JWT
# --------------------------------------------------------------------------- #
#: 默认初始密码：bootstrap 管理员与"未指定密码新建"的用户统一使用该口令，
#: 并置 must_change_password=True，首次登录必须改密后才能使用系统。
DEFAULT_INITIAL_PASSWORD = "123456a?"


def hash_password(password: str) -> str:
    iterations = _pbkdf2_iterations()
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, dk_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(dk_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except Exception:  # noqa: BLE001
        return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_jwt(claims: dict[str, Any]) -> str:
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(
        _secret().encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{_b64e(sig)}"


def decode_jwt(token: str) -> dict[str, Any] | None:
    """严格校验：固定 HS256（拒绝 alg=none）、恒定时间签名比较、exp/iat/sub/jti 完整。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_b64d(parts[0]))
        if header.get("alg") != "HS256":
            return None
        expected = hmac.new(
            _secret().encode("utf-8"), f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64d(parts[2]), expected):
            return None
        claims = json.loads(_b64d(parts[1]))
        now = _now()
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, int) or exp <= now:
            return None
        if not isinstance(iat, int) or iat > now + 60:
            return None
        if not claims.get("sub") or not claims.get("jti"):
            return None
        return claims
    except Exception:  # noqa: BLE001
        return None


def issue_token(user: dict[str, Any]) -> tuple[str, int]:
    now = _now()
    exp = now + TOKEN_TTL_SECONDS
    jti = uuid.uuid4().hex
    # pwd_epoch 密码版本号：改密/重置后递增，旧 token 版本不匹配即失效（不依赖时钟精度）
    token = encode_jwt({
        "sub": user["id"], "jti": jti, "iat": now, "exp": exp,
        "pwd_epoch": int(user.get("pwd_epoch", 0) or 0),
    })
    return token, exp


def revoke_jti(jti: str, exp: int) -> None:
    _store("auth_revoked").insert({"id": jti, "expires_at": exp, "created_at": _ts()})


def is_revoked(jti: str) -> bool:
    return any(r.get("id") == jti for r in _store("auth_revoked").all())


def prune_revoked() -> None:
    store = _store("auth_revoked")
    now = _now()
    for r in store.all():
        if r.get("expires_at", 0) <= now:
            store.delete(r["id"])


# --------------------------------------------------------------------------- #
# 用户 / 授权 / 审计
# --------------------------------------------------------------------------- #
def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", "viewer"),
        "grants": user.get("grants", []),
        "tenant_id": user.get("tenant_id") or "default",
        "department": user.get("department") or "",
        "disabled": bool(user.get("disabled", False)),
        "locked": bool(user.get("locked_until", 0) > _now()),
        "must_change_password": bool(user.get("must_change_password", False)),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def find_user(username: str) -> dict[str, Any] | None:
    return next(
        (u for u in _store("auth_users").all() if u.get("username") == username), None
    )


def get_user(user_id: str) -> dict[str, Any] | None:
    return _store("auth_users").get(user_id)


def ensure_bootstrap() -> None:
    """幂等初始化：无用户时创建内置管理员。

    初始密码优先级：
    1. 环境变量 ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD（部署者显式指定，不强制改密）
    2. 系统默认初始密码 DEFAULT_INITIAL_PASSWORD（强制首次登录改密）

    不再落盘任何明文密码文件（旧版 bootstrap_admin.txt 方案已废弃）。
    """
    store = _store("auth_users")
    if store.all():
        return
    password = os.getenv("ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD")
    if not password:
        password = DEFAULT_INITIAL_PASSWORD
    store.insert({
        "id": uuid.uuid4().hex,
        "username": "admin",
        "password_hash": hash_password(password),
        "role": "admin",
        "grants": [],
        "disabled": False,
        "failed_attempts": 0,
        "locked_until": 0,
        "must_change_password": password == DEFAULT_INITIAL_PASSWORD,
        "created_at": _ts(),
        "updated_at": _ts(),
    })


def audit(event: str, actor: str = "-", target: str = "", detail: Any = None) -> None:
    _store("auth_audit").insert({
        "id": uuid.uuid4().hex,
        "ts": _ts(),
        "event": event,
        "actor": actor,
        "target": target,
        "detail": detail or {},
    })
    # M2-D / V2-09：auth 域事件统一镜像到 append-only 审计日志（权限变更/登录/拒绝均入统一检索）
    try:
        import audit as audit_svc
        d = detail if isinstance(detail, dict) else {}
        audit_svc.record(
            action=event,
            actor=actor or "-",
            target_type="auth",
            target_id=str(target or "")[:120],
            project_id=str(d.get("project_id") or ""),
            tenant_id=str(d.get("tenant_id") or ""),
            detail={"detail": detail} if not isinstance(detail, dict) else detail,
        )
    except Exception:  # noqa: BLE001 审计镜像失败不阻断主流程
        pass


def list_audit(event: str | None = None, actor: str | None = None,
               limit: int = 200) -> list[dict[str, Any]]:
    rows = _store("auth_audit").all()
    if event:
        rows = [r for r in rows if r.get("event") == event]
    if actor:
        rows = [r for r in rows if r.get("actor") == actor]
    return list(reversed(rows))[:limit]


UNIFORM_LOGIN_ERROR = "账号或密码错误"


def authenticate(username: str, password: str) -> tuple[dict[str, Any] | None, str]:
    """返回 (user, error_code)。密码错误/账号不存在/锁定均用统一文案，防账号枚举。"""
    user = find_user(username)
    if user is None:
        # 保持与真实校验相近的耗时，避免时序侧信道
        hash_password(password)
        audit("login_failed", actor=username, detail={"reason": "invalid_credentials"})
        return None, UNIFORM_LOGIN_ERROR
    store = _store("auth_users")
    now = _now()
    if user.get("disabled") or user.get("locked_until", 0) > now:
        audit("login_failed", actor=username, detail={"reason": "invalid_credentials"})
        return None, UNIFORM_LOGIN_ERROR
    if not verify_password(password, user.get("password_hash", "")):
        attempts = int(user.get("failed_attempts", 0)) + 1
        patch: dict[str, Any] = {"failed_attempts": attempts, "updated_at": _ts()}
        if attempts >= LOCK_THRESHOLD:
            patch["locked_until"] = now + LOCK_SECONDS
            patch["failed_attempts"] = 0
        store.update(user["id"], patch)
        audit("login_failed", actor=username, detail={"reason": "invalid_credentials", "attempts": attempts})
        return None, UNIFORM_LOGIN_ERROR
    store.update(user["id"], {"failed_attempts": 0, "locked_until": 0, "updated_at": _ts()})
    audit("login_success", actor=username, target=user["id"])
    return get_user(user["id"]), ""


def change_password(user: dict[str, Any], old_password: str,
                    new_password: str) -> str:
    """用户修改本人密码（须验证当前密码）。返回错误信息，空串表示成功。

    成功后：清除 must_change_password、重置失败计数、并使改密前签发的
    旧会话立即失效（pwd_epoch 版本校验，见 resolve_request_user）。
    """
    if user.get("disabled"):
        return "账号已被禁用"
    if not verify_password(old_password, user.get("password_hash", "")):
        audit("password_change_failed", actor=user.get("username", "-"),
              detail={"reason": "wrong_old_password"})
        return "当前密码不正确"
    if new_password == old_password:
        return "新密码不能与当前密码相同"
    _store("auth_users").update(user["id"], {
        "password_hash": hash_password(new_password),
        "must_change_password": False,
        "failed_attempts": 0,
        "locked_until": 0,
        "pwd_epoch": int(user.get("pwd_epoch", 0) or 0) + 1,
        "updated_at": _ts(),
    })
    audit("password_changed", actor=user.get("username", "-"), target=user["id"])
    return ""


def create_user(username: str, password: str | None = None, role: str = "viewer",
                grants: list[dict[str, Any]] | None = None,
                tenant_id: str | None = None,
                department: str | None = None) -> dict[str, Any]:
    """创建用户。未指定密码时使用 DEFAULT_INITIAL_PASSWORD 并强制首次登录改密。"""
    if role not in ROLES:
        raise ValueError(f"未知角色: {role}")
    if find_user(username):
        raise ValueError("用户名已存在")
    use_default = not password
    rec = _store("auth_users").insert({
        "id": uuid.uuid4().hex,
        "username": username,
        "password_hash": hash_password(password or DEFAULT_INITIAL_PASSWORD),
        "role": role,
        "grants": grants or [],
        "tenant_id": tenant_id or "default",
        "department": department or "",
        "disabled": False,
        "failed_attempts": 0,
        "locked_until": 0,
        "must_change_password": use_default,
        "created_at": _ts(),
        "updated_at": _ts(),
    })
    return _public_user(rec)


def update_user(user_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    rec = get_user(user_id)
    if rec is None:
        return None
    allowed = {k: v for k, v in patch.items()
               if k in ("role", "grants", "disabled", "password", "tenant_id", "department") and v is not None}
    if "role" in allowed and allowed["role"] not in ROLES:
        raise ValueError(f"未知角色: {allowed['role']}")
    password = allowed.pop("password", None)
    if password:
        # 管理员重置：用户未参与选密 → 强制其首次登录改密，并使旧会话立即失效
        allowed["password_hash"] = hash_password(password)
        allowed["failed_attempts"] = 0
        allowed["locked_until"] = 0
        allowed["must_change_password"] = True
        allowed["pwd_epoch"] = int(get_user(user_id).get("pwd_epoch", 0) or 0) + 1
    allowed["updated_at"] = _ts()
    updated = _store("auth_users").update(user_id, allowed)
    return _public_user(updated) if updated else None


def delete_user(user_id: str) -> bool:
    """物理删除用户记录。返回是否存在被删。"""
    rec = get_user(user_id)
    if rec is None:
        return False
    _store("auth_users").delete(user_id)
    return True


def list_users() -> list[dict[str, Any]]:
    return [_public_user(u) for u in _store("auth_users").all()]


def effective_actions(user: dict[str, Any], project_id: str,
                      ontology_id: str | None = None) -> list[str]:
    """资源级授权：本体级 grant > 项目级 grant > 角色默认；显式 none 表示拒绝。"""
    grants = user.get("grants") or []
    matched: dict[str, str] | None = None
    if ontology_id:
        matched = next(
            (g for g in grants if g.get("scope") == "ontology" and g.get("target") == ontology_id),
            None,
        )
    if matched is None:
        matched = next(
            (g for g in grants if g.get("scope") == "project" and g.get("target") == project_id),
            None,
        )
    if matched is not None:
        return list(GRANT_LEVELS.get(matched.get("level", "none"), []))
    return list(ROLES.get(user.get("role", "viewer"), ROLES["viewer"])["actions"])


def permission_matrix(user: dict[str, Any]) -> dict[str, Any]:
    """供前端渲染菜单/操作入口；资源级判定仍在每次请求时按 project_id/ontology_id 重算。"""
    role = user.get("role", "viewer")
    actions = list(ROLES.get(role, ROLES["viewer"])["actions"])
    # credentials（凭据映射）为账号级能力：各角色均可见并可管理「自己的」映射
    # （建/删仍受中间件 write 动作约束：viewer 只读，modeler+ 可操作）
    # quality（数据健康度，M3-C）为只读看板；alignment（跨本体对齐，M4-A②）以候选浏览为主
    # （viewer 可提交候选，声明/会签按 write/review 动作在 API 层与前端按钮层门禁）
    tabs_read = ["home", "graph", "inference", "logs", "settings", "credentials", "tutorial",
                 "quality", "alignment"]
    tabs = list(tabs_read)
    if "write" in actions:
        # orchestration（Action 编排，M3-F）与 action 同属写侧：建流/编辑/发布/运行
        tabs += ["modeling", "ingest", "dictionary", "capability", "action", "mcp", "approvals",
                 "orchestration"]
    elif "review" in actions:
        tabs += ["ingest", "approvals"]
    if "admin" in actions:
        tabs += ["users", "audit"]
    return {
        "role": role,
        "role_label": ROLES.get(role, ROLES["viewer"])["label"],
        "actions": actions,
        "tabs": sorted(set(tabs)),
        "grants": user.get("grants") or [],
    }


# --------------------------------------------------------------------------- #
# 请求级判定（供全局过滤器使用）
# --------------------------------------------------------------------------- #
def is_public_request(method: str, path: str) -> bool:
    if method.upper() == "OPTIONS":
        return True
    if path in PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def extract_token(cookie_value: str | None, authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (cookie_value or "").strip()


def required_action(method: str, path: str) -> str:
    if any(path.startswith(p) for p in AUTH_ADMIN_PREFIXES):
        return "admin"
    if method.upper() in ("GET", "HEAD"):
        return "read"
    if REVIEW_POST_RE.match(path):
        return "review"
    if READ_ONLY_POST_RE.match(path):
        return "read"
    return "write"


def resource_keys(method: str, path: str, query_project_id: str | None) -> tuple[str, str | None]:
    """只从 query 与 path 取资源键：中间件不读 body，避免消费请求流。"""
    project_id = query_project_id or "default"
    m = re.match(r"^/api/projects/([^/]+)", path)
    if m:
        project_id = m.group(1)
    ontology_id: str | None = None
    m = re.match(r"^/api/ontologies/([^/]+)", path)
    if m and m.group(1) not in ("from-text", "from-excel", "from-doc", "jobs"):
        ontology_id = m.group(1)
    if ontology_id is None:
        m = re.match(r"^/api/graph/([^/]+)", path)
        if m:
            ontology_id = m.group(1)
    return project_id, ontology_id


def resolve_request_user(token: str) -> dict[str, Any] | None:
    """校验 JWT + 撤销名单 + 用户当前状态（权限每次实时查库）。"""
    if not token:
        return None
    claims = decode_jwt(token)
    if claims is None or is_revoked(claims["jti"]):
        return None
    user = get_user(claims["sub"])
    if user is None or user.get("disabled") or user.get("locked_until", 0) > _now():
        return None
    # 改密/重置密码前签发的旧会话立即失效（须用新密码重新登录）；
    # 历史 token 无 pwd_epoch 字段则放行（升级兼容，首次改密后自动纳入管控）
    epoch = user.get("pwd_epoch")
    if epoch:
        if int(claims.get("pwd_epoch", 0) or 0) != int(epoch):
            return None
    return user


def cors_headers(origin: str | None) -> dict[str, str]:
    if origin and LOCAL_ORIGIN_RE.match(origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}


def sso_config() -> dict[str, Any]:
    """SSO 预留：接口存在但不启用（V2-05 / TP-05-05）。"""
    return {
        "enabled": SSO_ENABLED,
        "providers": [],
        "reserved": True,
        "note": "预留对接点；当前仅支持账号密码登录",
    }

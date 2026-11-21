"""ESI (EVE Swagger Interface) OAuth2 客户端与钱包数据聚合。

使用前需要在 CCP 开发者站点 https://developers.eveonline.com 注册应用，
并在项目目录创建 config.json：
{
  "client_id": "你的 client id",
  "client_secret": "你的 client secret",
  "callback": "http://127.0.0.1:8000/api/esi/callback"
}
callback 必须与 CCP 应用注册的回调地址完全一致。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

import paths

BASE_DIR = paths.data_dir()
CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = BASE_DIR / "token.json"
PKCE_PATH = BASE_DIR / "pkce.json"  # state -> {verifier, ts}（持久化，防止服务重启丢失）

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
# 官方 verify 端点（旧端点，仍受支持；esi.evetech.net/verify/ 已迁移下线）
VERIFY_URL = "https://login.eveonline.com/oauth/verify"
ESI_BASE = "https://esi.evetech.net/latest"

SCOPES = [
    "esi-wallet.read_character_wallet.v1",
    "esi-markets.read_character_orders.v1",
]

# PKCE state 持久化（10 分钟有效），服务重启不丢
_PKCE_TTL = 600

# token.json 读写锁：防止并发刷新互相作废（CCP 的 refresh token 是一次性的）
_TOKEN_LOCK = threading.RLock()


def _load_token() -> dict | None:
    with _TOKEN_LOCK:
        if TOKEN_PATH.exists():
            try:
                return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None


def _save_token(tok: dict) -> None:
    with _TOKEN_LOCK:
        TOKEN_PATH.write_text(
            json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _load_pkce() -> dict:
    if PKCE_PATH.exists():
        try:
            data = json.loads(PKCE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        now = time.time()
        return {s: v for s, v in data.items() if now - v.get("ts", 0) < _PKCE_TTL}
    return {}


def _save_pkce(data: dict) -> None:
    PKCE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

REF_TYPE_ZH = {
    "bounty_prizes": "赏金",
    "bounty_prize": "赏金",
    "market_transaction": "市场交易",
    "broker_fee": "经纪人费",
    "transaction_tax": "销售税",
    "contract_reward": "合同报酬",
    "contract_deposit": "合同押金",
    "contract_collateral": "合同抵押",
    "player_trading": "玩家交易",
    "player_donation": "玩家捐赠",
    "mission_reward": "任务奖励",
    "mission_time_bonus_reward": "任务时限奖励",
    "manufacturing": "制造",
    "reprocessing": "提炼",
    "planetary_construction": "行星开发",
    "jump_clone_installation_fee": "克隆安装费",
    "clone_activation": "克隆激活",
    "industry_job": "工业任务",
    "project_discovery_reward": "探索计划奖励",
    "ess_escrow_transfer": "ESS托管转账",
    "alliance_registration_fee": "联盟注册费",
    "war_fee": "战争费",
    "insurance": "保险",
    "corporate_account_withdrawal": "公司账户提取",
    "office_rental_fee": "办公室租金",
}


# ---------- 配置与 token ----------

def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg and cfg.get("client_id") and cfg.get("client_secret"))


# _load_token / _save_token 定义见上方（带线程锁）


def clear_token() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


# ---------- OAuth2 授权码 + PKCE ----------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def build_login_url() -> str:
    """生成 EVE SSO 登录链接（含 PKCE）。"""
    cfg = load_config()
    if not cfg:
        raise RuntimeError("未找到 config.json")
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    data = _load_pkce()
    data[state] = {"verifier": verifier, "ts": time.time()}
    _save_pkce(data)
    params = {
        "response_type": "code",
        "redirect_uri": cfg.get("callback") or "http://127.0.0.1:8000/api/esi/callback",
        "client_id": cfg["client_id"],
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urlencode(params)


def exchange_code(code: str, state: str) -> None:
    """回调处理：用授权码换取 token 并持久化。"""
    data = _load_pkce()
    entry = data.pop(state, None)
    _save_pkce(data)
    if not entry:
        raise ValueError("state 校验失败：登录流程已过期（可能服务曾重启）。请重新点击登录")
    cfg = load_config()
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code_verifier": entry["verifier"],
        },
        timeout=20,
    )
    resp.raise_for_status()
    tok = resp.json()
    tok["obtained_at"] = time.time()
    # 拉取角色信息并保存（失败时保留 token，后续 status 会补）
    try:
        info = _verify(tok)
        tok["character_id"] = info["CharacterID"]
        tok["character_name"] = info["CharacterName"]
        tok["scopes"] = info.get("Scopes", "")
    except Exception:  # noqa: BLE001
        pass
    _save_token(tok)


def _verify(tok: dict) -> dict:
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    resp = requests.get(VERIFY_URL, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _refresh(tok: dict) -> dict:
    """刷新令牌。加锁串行化，并优先使用磁盘上最新的 refresh token。"""
    with _TOKEN_LOCK:
        # 其他请求可能刚刷新过并写盘，改用最新的 refresh token
        cur = _load_token()
        if cur and cur.get("refresh_token") and cur.get("refresh_token") != tok.get("refresh_token"):
            tok = cur
        cfg = load_config()
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            },
            timeout=20,
        )
        resp.raise_for_status()
        new = resp.json()
        new["obtained_at"] = time.time()
        # 保留角色信息
        new["character_id"] = tok.get("character_id")
        new["character_name"] = tok.get("character_name")
        new["scopes"] = tok.get("scopes", "")
        _save_token(new)
        return new


def _authed_request(path: str, params: dict | None = None, retry: bool = True) -> requests.Response:
    tok = _load_token()
    if not tok:
        raise RuntimeError("未登录")
    if time.time() - tok.get("obtained_at", 0) > tok.get("expires_in", 1200) - 60:
        tok = _refresh(tok)
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    resp = requests.get(ESI_BASE + path, params=params, headers=headers, timeout=20)
    if resp.status_code in (401, 403) and retry:
        tok = _refresh(tok)
        headers = {"Authorization": f"Bearer {tok['access_token']}"}
        resp = requests.get(ESI_BASE + path, params=params, headers=headers, timeout=20)
    return resp


def _get_all(path: str, max_pages: int = 10) -> list[dict]:
    """翻页拉取全量数据（遵循 X-Pages 响应头）。

    只有当实际页数超过 ``max_pages`` 截断时，才在结果里塞一条告警，
    避免"数据看起来完整、其实缺了最近几天"的静默误差。
    """
    items: list[dict] = []
    total_pages = 1
    for page in range(1, max_pages + 1):
        resp = _authed_request(path, {"page": page})
        if resp.status_code == 404:
            return items
        resp.raise_for_status()
        data = resp.json()
        items.extend(data)
        total_pages = int(resp.headers.get("x-pages", "1"))
        if page >= total_pages or len(data) == 0:
            break
    if total_pages > max_pages:
        items.append({"_truncated": True, "_pages": total_pages, "_max": max_pages})
    return items


# ---------- 钱包数据 ----------

def _fill_character(tok: dict, info: dict) -> dict:
    """补齐并保存角色信息，返回登录成功状态。"""
    changed = False
    for key, val in (
        ("character_id", info["CharacterID"]),
        ("character_name", info["CharacterName"]),
        ("scopes", info.get("Scopes", "")),
    ):
        if tok.get(key) != val:
            tok[key] = val
            changed = True
    if changed:
        _save_token(tok)
    return {
        "configured": is_configured(),
        "logged_in": True,
        "characterId": tok.get("character_id"),
        "characterName": tok.get("character_name", ""),
        "scopes": tok.get("scopes", ""),
    }


def _http_detail(ex: Exception) -> str:
    resp = getattr(ex, "response", None)
    if resp is None:
        return str(ex)[:150]
    return f"HTTP {resp.status_code} {str(resp.text)[:150]}"


def status() -> dict:
    """登录状态。verify 成功后自动补齐并持久化角色信息。"""
    tok = _load_token()
    configured = is_configured()
    if not tok:
        return {"configured": configured, "logged_in": False}

    try:
        info = _verify(tok)
        return _fill_character(tok, info)
    except requests.HTTPError as ex:
        code = ex.response.status_code if ex.response is not None else None
        if code not in (401, 403):
            return {
                "configured": configured,
                "logged_in": False,
                "expired": True,
                "error": f"ESI verify 接口异常：{_http_detail(ex)}",
            }
        # 401/403：access token 失效，尝试刷新
        try:
            tok = _refresh(tok)
            info = _verify(tok)
            return _fill_character(tok, info)
        except requests.HTTPError as ex2:
            code2 = ex2.response.status_code if ex2.response is not None else None
            # 刷新 404 = refresh token 已作废；若磁盘已被其他请求更新，再试一次
            if code2 == 404:
                fresh = _load_token()
                if fresh and fresh.get("refresh_token") != tok.get("refresh_token"):
                    try:
                        info = _verify(fresh)
                        return _fill_character(fresh, info)
                    except Exception:  # noqa: BLE001
                        pass
            return {
                "configured": configured,
                "logged_in": False,
                "expired": True,
                "error": f"access token 失效（verify 返回 {code}），刷新失败：{_http_detail(ex2)}。请重新登录",
            }
        except Exception as ex2:  # noqa: BLE001
            return {
                "configured": configured,
                "logged_in": False,
                "expired": True,
                "error": f"access token 失效（verify 返回 {code}），刷新异常：{_http_detail(ex2)}。请重新登录",
            }
    except Exception as ex:  # noqa: BLE001
        return {
            "configured": configured,
            "logged_in": False,
            "expired": True,
            "error": f"验证接口异常：{_http_detail(ex)}",
        }


def wallet_summary() -> dict:
    """聚合钱包数据：余额、挂单、每日余额曲线、月度流水、总收支。"""
    tok = _load_token()
    if not tok or not tok.get("character_id"):
        raise RuntimeError("未登录或角色信息缺失，请重新登录")
    cid = tok["character_id"]

    balance = _authed_request(f"/characters/{cid}/wallet/")
    balance.raise_for_status()
    wallet_isk = float(balance.json())

    journal = _get_all(f"/characters/{cid}/wallet/journal/", max_pages=10)
    orders = _get_all(f"/characters/{cid}/orders/", max_pages=5)

    warnings: list[str] = []
    for name, seq in (("钱包流水", journal), ("挂单", orders)):
        cut = [x for x in seq if x.get("_truncated")]
        seq[:] = [x for x in seq if not x.get("_truncated")]
        if cut:
            warnings.append(
                f"{name}共 {cut[0]['_pages']} 页，只读取了前 {cut[0]['_max']} 页"
                "（最近 30 天的统计可能偏少）"
            )

    # 每日余额曲线：每天取当日最后一笔之后的余额
    # journal 按时间从新到旧返回，同一日期内先写的是较晚的一笔，
    # 所以只在首次遇到该日期时写入（= 当日最后一笔），后到的更早记录跳过。
    by_date: dict[str, float] = {}
    by_date_ts: dict[str, str] = {}
    for j in journal:
        d = str(j.get("date", ""))[:10]
        if not d:
            continue
        ts = str(j.get("date", ""))
        prev = by_date.get(d)
        if prev is None or ts > by_date_ts[d]:
            by_date[d] = float(j.get("balance", 0.0))
            by_date_ts[d] = ts
    daily = [
        {"date": d, "balance": b} for d, b in sorted(by_date.items())
    ]

    # 月度流水
    by_month: dict[str, dict[str, float]] = {}
    for j in journal:
        m = str(j.get("date", ""))[:7]
        if not m:
            continue
        b = by_month.setdefault(m, {"income": 0.0, "expense": 0.0})
        amt = float(j.get("amount", 0.0))
        if amt > 0:
            b["income"] += amt
        else:
            b["expense"] += -amt
    monthly = [
        {
            "month": m,
            "income": round(b["income"], 2),
            "expense": round(b["expense"], 2),
            "net": round(b["income"] - b["expense"], 2),
        }
        for m, b in sorted(by_month.items())
    ]
    total_income = round(sum(b["income"] for b in by_month.values()), 2)
    total_expense = round(sum(b["expense"] for b in by_month.values()), 2)
    total_net = round(total_income - total_expense, 2)

    # 挂单统计
    sell = [o for o in orders if not o.get("is_buy_order")]
    buy = [o for o in orders if o.get("is_buy_order")]

    def orders_stat(items: list[dict]) -> dict:
        return {
            "count": len(items),
            "totalVolume": sum(int(o.get("volume_remain", 0) or 0) for o in items),
            "totalValue": round(
                sum(
                    float(o.get("price", 0) or 0) * int(o.get("volume_remain", 0) or 0)
                    for o in items
                ),
                2,
            ),
        }

    # 流水类型分布（收入侧 top 类型）
    by_ref: dict[str, float] = {}
    for j in journal:
        if float(j.get("amount", 0.0)) <= 0:
            continue
        rt = str(j.get("ref_type", ""))
        by_ref[rt] = by_ref.get(rt, 0.0) + float(j.get("amount", 0.0))
    ref_breakdown = sorted(
        [
            {"type": rt, "label": REF_TYPE_ZH.get(rt, rt), "amount": round(v, 2)}
            for rt, v in by_ref.items()
        ],
        key=lambda x: x["amount"],
        reverse=True,
    )[:10]

    return {
        "characterId": cid,
        "characterName": tok.get("character_name", ""),
        "balance": wallet_isk,
        "orders": {"sell": orders_stat(sell), "buy": orders_stat(buy)},
        "daily": daily,
        "monthly": monthly,
        "totals": {
            "income": total_income,
            "expense": total_expense,
            "net": total_net,
        },
        "refBreakdown": ref_breakdown,
        "journalEntries": len(journal),
        "warnings": warnings,
    }

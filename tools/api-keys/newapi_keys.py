#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""new-api API key 管理 CLI（以管理员账号为容器，令牌名 = 工号）。

所有 key 挂在同一个管理员账号下，令牌名就是工号。本工具不改动 new-api 任何代码，
只用它已有的后台接口，因此可以持续跟进上游合并。

鉴权：在管理员账号的「安全设置 → 访问令牌」创建一个访问令牌，勾选
      API keys 的 查看 / 编辑 / 查看完整密钥，以及个人资料 的 查看
      （最后一项用于读取可用分组）。把它放进 $NEWAPI_PAT。

  export NEWAPI_BASE_URL=http://127.0.0.1:3000
  export NEWAPI_PAT=nap_xxxxxxxx

常用：
  newapi_keys.py check                     检查凭证和可用分组
  newapi_keys.py list                      列出所有 key
  newapi_keys.py list --name A1001         按工号精确查
  newapi_keys.py create --name A1001 --quota 10 --group default
  newapi_keys.py update --name A1001 --quota 20
  newapi_keys.py disable --name A1001      离职停用
  newapi_keys.py export --out keys.csv     导出明文 key
  newapi_keys.py import --roster roster.csv --out keys.csv

给 agent 用时加 --json，输出结构化结果。

额度单位跟随站点显示币种：CLI 启动时读一次公开的 /api/status，按站点的
quota_per_unit、quota_display_type 和汇率换算，所以 --quota 填的就是界面上
看到的那个数字（CNY 站点填人民币）。需要时可加 --quota-per-unit /
--exchange-rate 覆盖。`check` 会打印当前生效的换算关系。

两个已知约束：
  * 令牌分组必须是该管理员账号的「可用分组」之一，否则网关直接 403。
    可用分组由「系统设置 → 分组」里的用户可用分组决定，默认是所有分组的
    并集；用 `check` 可以看到当前实际范围。
  * 管理员账号不能禁用/删除自己，所以整体止损要靠批量停用令牌，而不是停用账号。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_QUOTA_PER_UNIT = 500_000.0  # new-api 默认 QuotaPerUnit：500000 quota = $1
DEFAULT_EXCHANGE_RATE = 1.0
STATUS_NAMES = {"enabled": 1, "disabled": 2, "expired": 3, "exhausted": 4}
STATUS_IDS = {value: key for key, value in STATUS_NAMES.items()}
UNLIMITED_VALUES = {"unlimited", "无限", "-1", "不限"}
ID_HEADERS = {"工号", "工号id", "id", "username", "name", "token_name", "令牌名", "令牌名称"}
GROUP_HEADERS = {"分组", "group", "分组名"}
QUOTA_HEADERS = {"额度", "quota", "额度usd", "额度(usd)", "usd", "金额"}


class ApiError(RuntimeError):
    pass


def is_scope_denied(error: Exception) -> bool:
    """判断是否是该访问令牌缺少某项作用域而被拒。

    令牌只带 api_key:write 时仍可创建，但读不了列表，所以调用方需要区分
    「权限不足」和「真的出错了」，在前者下继续把能做的做完。
    """
    return "ACCESS_TOKEN_SCOPE_DENIED" in str(error)


class Client:
    def __init__(self, base_url: str, token: str, quota_per_unit: float | None = None,
                 exchange_rate: float | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self._quota_override = quota_per_unit
        self._rate_override = exchange_rate
        self.quota_per_unit = quota_per_unit or DEFAULT_QUOTA_PER_UNIT
        self.exchange_rate = exchange_rate or DEFAULT_EXCHANGE_RATE
        self.display_type = "USD"
        self.symbol = "$"
        self.timeout = timeout
        self._tokens: list[dict] | None = None

    def load_currency(self) -> None:
        """从公开的 /api/status 读站点实际币种配置，让金额换算与界面一致。

        换算关系是 显示金额 = quota / quota_per_unit * 汇率：CNY 用
        usd_exchange_rate，CUSTOM 用 custom_currency_exchange_rate，USD 汇率为 1，
        TOKENS 直接按 token 数显示。命令行显式传入的数值优先，不被这里覆盖。
        """
        try:
            data = self.get("/api/status") or {}
        except ApiError:
            return
        if self._quota_override is None and data.get("quota_per_unit"):
            self.quota_per_unit = float(data["quota_per_unit"])

        self.display_type = (data.get("quota_display_type") or "USD").upper()
        if self.display_type == "CNY":
            self.symbol = "¥"
            if self._rate_override is None:
                self.exchange_rate = float(data.get("usd_exchange_rate") or 1) or 1.0
        elif self.display_type == "CUSTOM":
            self.symbol = data.get("custom_currency_symbol") or ""
            if self._rate_override is None:
                self.exchange_rate = float(data.get("custom_currency_exchange_rate") or 1) or 1.0
        elif self.display_type == "TOKENS":
            self.symbol = ""
        else:
            self.display_type = "USD"
            self.symbol = "$"
            if self._rate_override is None:
                self.exchange_rate = 1.0
        if self.exchange_rate <= 0:
            self.exchange_rate = DEFAULT_EXCHANGE_RATE

    def amount_to_quota(self, amount: float) -> int:
        """把界面显示币种的金额换算成 quota。"""
        if self.display_type == "TOKENS":
            return int(round(amount))
        return int(round(amount / self.exchange_rate * self.quota_per_unit))

    def quota_to_amount(self, quota: int) -> float:
        if self.display_type == "TOKENS":
            return float(quota)
        return quota / self.quota_per_unit * self.exchange_rate

    def format_amount(self, quota: int) -> str:
        amount = self.quota_to_amount(quota)
        return f"{self.symbol}{amount:,.2f}" if self.symbol else f"{amount:,.0f}"

    def currency_name(self) -> str:
        return {"CNY": "人民币", "USD": "美元", "TOKENS": "token 数"}.get(
            self.display_type, self.display_type)

    def _request(self, method: str, path: str, body: dict | None = None, retries: int = 3):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last_error = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self.token)
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last_error = ApiError(f"{method} {path} -> HTTP {exc.code}: {detail}")
                # 触发限流时退避重试，避免 agent 连续调用时直接失败。
                if exc.code == 429 and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                raise ApiError(f"{method} {path} -> {exc.reason}") from exc
        else:
            raise last_error
        # new-api 把业务错误也放在 HTTP 200 里，必须看 success 字段。
        if not payload.get("success"):
            raise ApiError(f"{method} {path} -> {payload.get('message') or 'unknown error'}")
        return payload.get("data")

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self._request("POST", path, body if body is not None else {})

    def put(self, path: str, body: dict | None = None):
        return self._request("PUT", path, body if body is not None else {})

    def delete(self, path: str):
        return self._request("DELETE", path)

    # ---- 账号信息 ----

    def usable_groups(self) -> dict:
        return self.get("/api/user/self/groups") or {}

    # ---- 令牌 ----

    def list_tokens(self, force: bool = False) -> list[dict]:
        """列出全部令牌。走 /api/token/，该接口不受搜索限流（10 次/分钟）约束。"""
        if self._tokens is not None and not force:
            return self._tokens
        tokens: list[dict] = []
        page = 1
        while True:
            data = self.get(f"/api/token/?p={page}&size=100")
            items = data.get("items") or []
            tokens.extend(items)
            total = data.get("total") or 0
            if not items or len(tokens) >= total:
                self._tokens = tokens
                return tokens
            page += 1

    def invalidate(self) -> None:
        self._tokens = None

    def find_by_name(self, name: str) -> list[dict]:
        return [token for token in self.list_tokens() if token.get("name") == name]

    def resolve_one(self, name: str, token_id: int | None = None) -> dict:
        tokens = self.list_tokens()
        if token_id is not None:
            for token in tokens:
                if token["id"] == token_id:
                    return token
            raise ApiError(f"找不到 id={token_id} 的令牌")
        matches = [token for token in tokens if token.get("name") == name]
        if not matches:
            raise ApiError(f"找不到名字为 {name} 的令牌")
        if len(matches) > 1:
            ids = ", ".join(str(token["id"]) for token in matches)
            raise ApiError(f"名字 {name} 对应多个令牌（id: {ids}），请用 --id 指定")
        return matches[0]

    def create_token(self, name: str, quota: int, unlimited: bool, group: str,
                     expired_time: int) -> dict | None:
        """创建令牌。创建成功后回读详情需要 api_key:read；令牌没有该作用域时返回 None。"""
        body = {
            "name": name,
            "remain_quota": quota,
            "unlimited_quota": unlimited,
            "expired_time": expired_time,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": group,
            "cross_group_retry": False,
        }
        if group == "auto":
            body["auto_groups"] = []
        self.post("/api/token/", body)
        self.invalidate()
        try:
            created = self.find_by_name(name)
        except ApiError as exc:
            if not is_scope_denied(exc):
                raise
            return None
        if not created:
            raise ApiError(f"令牌 {name} 创建后未能查到")
        return max(created, key=lambda token: token["id"])

    def update_token(self, token: dict, group: str | None = None,
                     quota: int | None = None, unlimited: bool | None = None) -> None:
        existing_group = token.get("group") or ""
        existing_unlimited = bool(token.get("unlimited_quota"))
        existing_quota = token.get("remain_quota", 0)
        body = {
            "id": token["id"],
            "name": token["name"],
            "remain_quota": existing_quota if quota is None else quota,
            "unlimited_quota": existing_unlimited if unlimited is None else unlimited,
            "expired_time": token.get("expired_time", -1),
            "model_limits_enabled": bool(token.get("model_limits_enabled")),
            "model_limits": token.get("model_limits") or "",
            "allow_ips": token.get("allow_ips") or "",
            "group": existing_group if group is None else group,
            "cross_group_retry": bool(token.get("cross_group_retry")),
        }
        self.put("/api/token/", body)
        self.invalidate()

    def set_status(self, token_id: int, status: int) -> None:
        self.put("/api/token/?status_only=true", {"id": token_id, "status": status})
        self.invalidate()

    def delete_token(self, token_id: int) -> None:
        self.delete(f"/api/token/{token_id}")
        self.invalidate()

    def reveal_keys(self, ids: list[int], chunk: int = 100, pause: float = 0.5) -> dict[int, str]:
        keys: dict[int, str] = {}
        for start in range(0, len(ids), chunk):
            batch = ids[start:start + chunk]
            data = self.post("/api/token/batch/keys", {"ids": batch})
            for raw_id, key in (data.get("keys") or {}).items():
                keys[int(raw_id)] = key
            if start + chunk < len(ids):
                time.sleep(pause)
        return keys


def describe(token: dict, client: Client) -> str:
    status = STATUS_IDS.get(token.get("status"), str(token.get("status")))
    amount = "unlimited" if token.get("unlimited_quota") else client.format_amount(token.get("remain_quota", 0))
    return f"{token['name']}\t{token.get('group') or '-'}\t{status}\t{amount}\tid={token['id']}"


def emit(args, rows, lines) -> None:
    if args.json:
        print(json.dumps({"success": True, "data": rows}, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def check_group(client: Client, group: str) -> None:
    """写分组之前校验它在账号的可用范围内。

    new-api 创建令牌时并不校验分组，非法分组会静默建成功，直到同学拿这个 key
    发请求才 403。所以这里尽量提前拦下。若令牌没有 profile:read、读不到可用
    分组，则退化为警告并放行，由使用者自行确认分组存在。
    """
    try:
        usable = client.usable_groups()
    except ApiError as exc:
        if not is_scope_denied(exc):
            raise
        print(f"  警告：该访问令牌没有 profile:read，无法校验分组 {group} 是否可用",
              file=sys.stderr)
        return
    if group not in usable:
        raise ApiError(
            f"分组 {group} 不在该账号的可用分组内（可用：{', '.join(sorted(usable))}）。\n"
            "请在「系统设置 → 分组」给该账号所在的用户分组放开这个分组。"
        )


# ---------------------------------------------------------------- 子命令

def cmd_check(args, client: Client) -> int:
    usable = client.usable_groups()
    tokens = client.list_tokens()
    by_status: dict[str, int] = {}
    for token in tokens:
        name = STATUS_IDS.get(token.get("status"), str(token.get("status")))
        by_status[name] = by_status.get(name, 0) + 1
    per_unit = int(client.quota_per_unit)
    data = {"base_url": client.base_url, "usable_groups": sorted(usable),
            "token_count": len(tokens), "by_status": by_status,
            "currency": {"display_type": client.display_type,
                         "quota_per_unit": client.quota_per_unit,
                         "exchange_rate": client.exchange_rate}}
    if args.json:
        print(json.dumps({"success": True, "data": data}, ensure_ascii=False, indent=2))
    else:
        print(f"地址：{client.base_url}")
        print(f"可用分组：{', '.join(sorted(usable))}")
        print(f"计价币种：{client.currency_name()}，{per_unit} quota = {client.format_amount(per_unit)}")
        print(f"令牌总数：{len(tokens)}，状态分布：{by_status or '无'}")
    return 0


def cmd_list(args, client: Client) -> int:
    tokens = client.list_tokens()
    if args.name:
        tokens = [token for token in tokens if token.get("name") == args.name]
    if args.group:
        tokens = [token for token in tokens if (token.get("group") or "") == args.group]
    if args.status:
        wanted = STATUS_NAMES[args.status]
        tokens = [token for token in tokens if token.get("status") == wanted]
    tokens.sort(key=lambda token: token["id"])
    rows = [{"id": t["id"], "name": t["name"], "group": t.get("group"),
             "status": STATUS_IDS.get(t.get("status"), t.get("status")),
             "remain_quota": t.get("remain_quota"), "unlimited_quota": t.get("unlimited_quota")}
            for t in tokens]
    emit(args, rows, [describe(token, client) for token in tokens] or ["（无匹配令牌）"])
    return 0


def cmd_create(args, client: Client) -> int:
    group = args.group
    if group:
        check_group(client, group)
    unlimited = args.unlimited
    quota = 0 if unlimited else client.amount_to_quota(args.quota)
    expired_time = -1 if args.expire_days <= 0 else int(time.time()) + args.expire_days * 86400
    # 重名检查需要 api_key:read。令牌没有该作用域时跳过检查继续创建，
    # 因为创建本身只需要 api_key:write。
    try:
        existing = client.find_by_name(args.name)
    except ApiError as exc:
        if not is_scope_denied(exc):
            raise
        print("  提示：该访问令牌没有 api_key:read，跳过重名检查直接创建", file=sys.stderr)
        existing = []
    if existing:
        raise ApiError(f"令牌 {args.name} 已存在（id={existing[0]['id']}），如需修改请用 update")
    token = client.create_token(args.name, quota, unlimited, group or "", expired_time)
    if token is None:
        emit(args, [{"name": args.name}], [f"已创建 {args.name}（无读取权限，未回读详情）"])
    else:
        rows = [{"id": token["id"], "name": token["name"], "group": token.get("group")}]
        emit(args, rows, [f"已创建 {describe(token, client)}"])
    return 0


def cmd_update(args, client: Client) -> int:
    token = client.resolve_one(args.name, args.id)
    if args.group:
        check_group(client, args.group)
    unlimited = True if args.unlimited else None
    quota = None
    if args.quota is not None:
        quota = client.amount_to_quota(args.quota)
        unlimited = False
    if args.group is None and quota is None and unlimited is None:
        raise ApiError("没有要修改的内容：请给出 --group 或 --quota/--unlimited")
    client.update_token(token, group=args.group, quota=quota, unlimited=unlimited)
    emit(args, [{"id": token["id"], "name": token["name"]}],
         [f"已更新 {token['name']}（id={token['id']}）"])
    return 0


def cmd_status(args, client: Client) -> int:
    token = client.resolve_one(args.name, args.id)
    status = STATUS_NAMES[args.set_status]
    client.set_status(token["id"], status)
    emit(args, [{"id": token["id"], "name": token["name"], "status": args.set_status}],
         [f"已{'停用' if status == STATUS_NAMES['disabled'] else '启用'} {token['name']}"])
    return 0


def cmd_delete(args, client: Client) -> int:
    token = client.resolve_one(args.name, args.id)
    client.delete_token(token["id"])
    emit(args, [{"id": token["id"], "name": token["name"]}], [f"已删除 {token['name']}"])
    return 0


def cmd_export(args, client: Client) -> int:
    tokens = client.list_tokens()
    if args.name:
        tokens = [token for token in tokens if token.get("name") in set(args.name)]
    tokens.sort(key=lambda token: token["id"])
    keys = client.reveal_keys([token["id"] for token in tokens])
    rows = []
    for token in tokens:
        raw = keys.get(token["id"])
        if raw is None:
            print(f"  取 key 失败 {token['name']}（id={token['id']}）", file=sys.stderr)
            continue
        rows.append({"name": token["name"], "group": token.get("group"),
                     "unlimited_quota": bool(token.get("unlimited_quota")),
                     "remain_quota": token.get("remain_quota"),
                     "amount": "unlimited" if token.get("unlimited_quota")
                               else client.format_amount(token.get("remain_quota", 0)),
                     "key": "sk-" + raw})
    if args.json:
        print(json.dumps({"success": True, "data": rows}, ensure_ascii=False, indent=2))
    elif args.out:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工号", "分组", f"额度（{client.currency_name()}）", "API_KEY"])
            for row in rows:
                writer.writerow([row["name"], row["group"], row["amount"], row["key"]])
        os.chmod(args.out, 0o600)
        print(f"已导出 {len(rows)} 个 key 到 {args.out}（权限 600，分发后请删除）")
    else:
        for row in rows:
            print(f"{row['name']}\t{row['key']}")
    return 0


def cmd_import(args, client: Client) -> int:
    entries = parse_roster(args.roster)
    print(f"名单：{len(entries)} 人（{args.roster}）")

    try:
        usable = client.usable_groups()
    except ApiError as exc:
        if not is_scope_denied(exc):
            raise
        print("  警告：该访问令牌没有 profile:read，跳过分组可用性校验", file=sys.stderr)
        usable = None
    if usable is not None:
        wanted = {entry.group or args.default_group for entry in entries}
        rejected = sorted(wanted - set(usable))
        if rejected:
            raise ApiError(
                f"以下分组不在该账号的可用分组内：{', '.join(rejected)}\n"
                f"可用分组：{', '.join(sorted(usable))}"
            )

    existing = client.list_tokens()
    by_name: dict[str, list[dict]] = {}
    for token in existing:
        by_name.setdefault(token.get("name", ""), []).append(token)
    print(f"容器账号现有令牌：{len(existing)} 个")

    to_create, to_update = [], []
    for entry in entries:
        group = entry.group or args.default_group
        quota, unlimited = entry.quota(args.default_quota, client)
        matches = by_name.get(entry.employee_id, [])
        if not matches:
            to_create.append((entry, group, quota, unlimited))
        elif args.update:
            token = max(matches, key=lambda item: item.get("id", 0))
            to_update.append((token, entry, group, quota, unlimited))

    roster_names = {entry.employee_id for entry in entries}
    to_disable = [max(by_name[name], key=lambda item: item.get("id", 0))
                  for name in by_name
                  if name and name not in roster_names
                  and any(token.get("status") == STATUS_NAMES["enabled"] for token in by_name[name])]

    print(f"\n计划：新建 {len(to_create)} 个，更新 {len(to_update)} 个"
          + (f"，停用名单外 {len(to_disable)} 个" if args.disable_missing else ""))
    for entry, group, quota, unlimited in to_create[:10]:
        amount = "无限" if unlimited else client.format_amount(quota)
        print(f"  + {entry.employee_id:<16} 分组={group:<10} 额度={amount}")
    if len(to_create) > 10:
        print(f"  ... 其余 {len(to_create) - 10} 个略")
    for token, entry, group, quota, unlimited in to_update[:10]:
        amount = "无限" if unlimited else client.format_amount(quota)
        print(f"  ~ {entry.employee_id:<16} 分组={group:<10} 额度={amount}（id={token['id']}）")
    if len(to_update) > 10:
        print(f"  ... 其余 {len(to_update) - 10} 个略")
    if args.disable_missing and to_disable:
        names = ", ".join(token["name"] for token in to_disable[:10])
        print(f"  停用：{names}" + (f" 等 {len(to_disable)} 个" if len(to_disable) > 10 else ""))

    if args.dry_run:
        print("\n--dry-run：未做任何修改。")
        return 0

    expired_time = -1 if args.expire_days <= 0 else int(time.time()) + args.expire_days * 86400
    failures: list[str] = []

    for entry, group, quota, unlimited in to_create:
        try:
            client.create_token(entry.employee_id, quota, unlimited, group, expired_time)
            print(f"  已创建 {entry.employee_id}")
        except ApiError as exc:
            failures.append(f"{entry.employee_id}: {exc}")
            print(f"  创建失败 {entry.employee_id}: {exc}", file=sys.stderr)
        time.sleep(0.2)

    for token, entry, group, quota, unlimited in to_update:
        try:
            client.update_token(token, group=group, quota=quota, unlimited=unlimited)
            print(f"  已更新 {entry.employee_id}")
        except ApiError as exc:
            failures.append(f"{entry.employee_id}: {exc}")
            print(f"  更新失败 {entry.employee_id}: {exc}", file=sys.stderr)
        time.sleep(0.2)

    if args.disable_missing:
        for token in to_disable:
            try:
                client.set_status(token["id"], STATUS_NAMES["disabled"])
                print(f"  已停用 {token['name']}")
            except ApiError as exc:
                failures.append(f"{token['name']}: {exc}")
                print(f"  停用失败 {token['name']}: {exc}", file=sys.stderr)
            time.sleep(0.2)

    if args.out:
        refreshed = client.list_tokens()
        by_name.clear()
        for token in refreshed:
            by_name.setdefault(token.get("name", ""), []).append(token)
        selected = []
        for entry in entries:
            matches = by_name.get(entry.employee_id, [])
            if matches:
                selected.append((entry.employee_id, max(matches, key=lambda item: item["id"])))
        keys = client.reveal_keys([token["id"] for _, token in selected])
        rows = []
        for employee_id, token in selected:
            raw = keys.get(token["id"])
            if raw is None:
                failures.append(f"{employee_id}: reveal failed")
                continue
            amount = "unlimited" if token.get("unlimited_quota") else \
                client.format_amount(token.get("remain_quota", 0))
            rows.append([employee_id, token.get("group", ""), amount, "sk-" + raw])
        with open(args.out, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工号", "分组", f"额度（{client.currency_name()}）", "API_KEY"])
            writer.writerows(rows)
        os.chmod(args.out, 0o600)
        print(f"\n已导出 {len(rows)} 个 key 到 {args.out}（权限 600，分发后请删除）")

    if failures:
        print(f"\n有 {len(failures)} 项失败：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- 名单

class RosterEntry:
    def __init__(self, employee_id: str, group: str | None, amount: str | None):
        self.employee_id = employee_id
        self.group = group
        self.amount = amount

    def quota(self, default_amount: float, client: Client) -> tuple[int, bool]:
        raw = self.amount
        if raw and raw.lower() in UNLIMITED_VALUES:
            return 0, True
        if raw:
            try:
                amount = float(raw)
            except ValueError:
                raise SystemExit(f"工号 {self.employee_id} 的额度无法解析：{raw!r}")
        else:
            amount = default_amount
        return client.amount_to_quota(amount), False


def parse_roster(path: str) -> list[RosterEntry]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    if not rows:
        raise SystemExit(f"名单文件为空：{path}")

    index_id, index_group, index_quota = 0, None, None
    first = [cell.strip().lstrip("﻿").lower() for cell in rows[0]]
    if first and first[0] in ID_HEADERS:
        for i, cell in enumerate(first):
            if cell in GROUP_HEADERS:
                index_group = i
            elif cell in QUOTA_HEADERS:
                index_quota = i
        rows = rows[1:]

    entries: list[RosterEntry] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        if index_id >= len(row):
            continue
        employee_id = row[index_id].strip()
        if not employee_id:
            continue
        if employee_id in seen:
            print(f"  跳过重复工号：{employee_id}（第 {line_no} 行）", file=sys.stderr)
            continue
        seen.add(employee_id)
        group = row[index_group].strip() if index_group is not None and index_group < len(row) else ""
        quota = row[index_quota].strip() if index_quota is not None and index_quota < len(row) else ""
        entries.append(RosterEntry(employee_id, group or None, quota or None))
    return entries


# ---------------------------------------------------------------- 入口

def build_common_parser() -> argparse.ArgumentParser:
    """公共选项同时挂在主命令和子命令上，因此放在子命令前后都能识别。

    全部用 SUPPRESS 作为默认值：这样没在子命令上传时，不会把主命令解析到的
    值覆盖回默认值。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-url", default=argparse.SUPPRESS,
                        help="new-api 地址，默认取 $NEWAPI_BASE_URL")
    parser.add_argument("--token", default=argparse.SUPPRESS,
                        help="管理员账号的访问令牌，默认取 $NEWAPI_PAT")
    parser.add_argument("--quota-per-unit", type=float, default=argparse.SUPPRESS,
                        help="覆盖 quota 换算比例，默认自动从 /api/status 读取站点配置")
    parser.add_argument("--exchange-rate", type=float, default=argparse.SUPPRESS,
                        help="覆盖汇率（CNY 下即 1 美元兑多少人民币），默认自动从 /api/status 读取")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出 JSON，便于 agent 解析")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = build_common_parser()
    parser = argparse.ArgumentParser(
        prog="newapi_keys.py",
        parents=[common],
        description="new-api API key 管理 CLI（管理员账号为容器，令牌名 = 工号）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    add_parser("check", help="检查凭证和可用分组").set_defaults(func=cmd_check)

    p_list = add_parser("list", help="列出/查询 key")
    p_list.add_argument("--name", help="按令牌名（工号）精确过滤")
    p_list.add_argument("--group", help="按分组过滤")
    p_list.add_argument("--status", choices=sorted(STATUS_NAMES), help="按状态过滤")
    p_list.set_defaults(func=cmd_list)

    p_create = add_parser("create", help="新建 key")
    p_create.add_argument("--name", required=True, help="令牌名，通常填工号")
    p_create.add_argument("--group", help="分组，需在该账号可用分组内")
    p_create.add_argument("--quota", type=float, default=10.0,
                          help="额度，单位跟随站点显示币种（CNY 站点即人民币），默认 10")
    p_create.add_argument("--unlimited", action="store_true", help="不限制该令牌额度")
    p_create.add_argument("--expire-days", type=int, default=0, help="有效天数，0 表示永不过期")
    p_create.set_defaults(func=cmd_create)

    p_update = add_parser("update", help="改分组或额度")
    p_update.add_argument("--name", help="令牌名（工号）")
    p_update.add_argument("--id", type=int, help="按 id 指定，用于同名歧义时")
    p_update.add_argument("--group", help="新的分组")
    p_update.add_argument("--quota", type=float, help="新的额度，单位跟随站点显示币种")
    p_update.add_argument("--unlimited", action="store_true", help="改为不限制令牌额度")
    p_update.set_defaults(func=cmd_update)

    for name, helptext, status in (("disable", "停用 key", "disabled"), ("enable", "启用 key", "enabled")):
        p_status = add_parser(name, help=helptext)
        p_status.add_argument("--name", help="令牌名（工号）")
        p_status.add_argument("--id", type=int, help="按 id 指定")
        p_status.set_defaults(func=cmd_status, set_status=status)

    p_delete = add_parser("delete", help="删除 key")
    p_delete.add_argument("--name", help="令牌名（工号）")
    p_delete.add_argument("--id", type=int, help="按 id 指定")
    p_delete.set_defaults(func=cmd_delete)

    p_export = add_parser("export", help="导出明文 key")
    p_export.add_argument("--name", action="append", help="只导出指定工号，可重复")
    p_export.add_argument("--out", help="写入 CSV 文件；不填则在标准输出打印")
    p_export.set_defaults(func=cmd_export)

    p_import = add_parser("import", help="按名单 CSV 批量发放")
    p_import.add_argument("--roster", required=True, help="名单 CSV 路径")
    p_import.add_argument("--out", help="导出明文 key 的 CSV 路径")
    p_import.add_argument("--default-group", default="default", help="名单未指定分组时使用")
    p_import.add_argument("--default-quota", type=float, default=10.0,
                          help="名单未指定额度时使用，单位跟随站点显示币种")
    p_import.add_argument("--expire-days", type=int, default=0, help="有效天数，0 表示永不过期")
    p_import.add_argument("--update", action="store_true", help="已存在的令牌也同步分组和额度")
    p_import.add_argument("--disable-missing", action="store_true",
                          help="停用名单外、当前启用中的令牌（离职处理，先 --dry-run 确认）")
    p_import.add_argument("--dry-run", action="store_true", help="只打印将要执行的操作")
    p_import.set_defaults(func=cmd_import)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.base_url = getattr(args, "base_url", None) or os.environ.get(
        "NEWAPI_BASE_URL", "http://127.0.0.1:3000")
    args.token = getattr(args, "token", None) or os.environ.get("NEWAPI_PAT")
    args.json = getattr(args, "json", False)
    if not args.token:
        raise SystemExit("缺少访问令牌：请设置 $NEWAPI_PAT 或传 --token")
    client = Client(args.base_url, args.token,
                    getattr(args, "quota_per_unit", None),
                    getattr(args, "exchange_rate", None))
    client.load_currency()
    return args.func(args, client)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as error:
        print(f"错误：{error}", file=sys.stderr)
        if is_scope_denied(error):
            print("提示：该访问令牌缺少上面列出的作用域，可在「安全设置 → 访问令牌」里给它补上。",
                  file=sys.stderr)
        sys.exit(1)

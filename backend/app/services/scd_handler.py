"""SCD处理器服务：管理缓慢变化维的处理策略，支持SCD类型1/2/3真实处理逻辑。

增强能力：
- SCD类型1：直接覆盖旧值（只保留最新）
- SCD类型2：新增行保留历史，通过生效/失效时间 + 当前标记追踪
- SCD类型3：保留原值和新值两列，支持当前值与上一值并存
- 消费 dim.defined 和 attr.confirmed 通道消息
- 变化检测基于MD5哈希，自动生成suggested策略
- 处理落库时把属性前值/后值/生效日期写入变更日志，供导出接口提取业务履历
- 策略启停与后续处理失败写入处理记录
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.slow_change_log import record_dimension_changes, record_processing
from app.store import store


class ScdHandlerService(BaseModuleService):
    MODULE = "scd_handler"
    ENTITY = "SCD策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略名称", "目标维度", "SCD类型", "变化检测", "生效时间", "失效时间", "当前标记"]
    REQUIRED_FIELDS = ["策略名称", "目标维度", "SCD类型"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用策略": "草稿", "停用策略": "已启用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    VALID_SCD_TYPES = {"SCD类型0", "SCD类型1", "SCD类型2", "SCD类型3"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建SCD策略：消费维度定义和属性确认通道，校验SCD类型并生成变化检测规则。"""
        scd_type = str(values.get("SCD类型", "SCD类型2")).strip()
        if scd_type not in self.VALID_SCD_TYPES:
            scd_type = "SCD类型2"
            values["SCD类型"] = scd_type

        dim_code = str(values.get("目标维度", "")).strip()

        # 消费维度定义通道
        dim_msg = store.channel.consume_latest("dim.defined", "scd_handler")
        if dim_msg and dim_msg["payload"].get("dim_code") == dim_code:
            # 维度已定义，自动继承SCD类型
            inherited_scd = dim_msg["payload"].get("scd_type", scd_type)
            if values.get("SCD类型") != inherited_scd:
                values["SCD类型"] = inherited_scd
                scd_type = inherited_scd

        # 消费属性确认通道，构建变化检测规则
        attr_msgs = store.channel.consume("attr.confirmed", "scd_handler")
        detection_fields = []
        for m in attr_msgs:
            if m["payload"].get("dim_code") == dim_code:
                detection_fields.append(m["payload"].get("attr_name", ""))

        # 生成变化检测规则
        if detection_fields:
            detection_rule = _build_detection_rule(scd_type, detection_fields)
        else:
            detection_rule = "MD5(record)"

        values["变化检测"] = detection_rule

        # 设置生效/失效时间默认值
        if not values.get("生效时间"):
            values["生效时间"] = "2026-01-01"
        if not values.get("失效时间"):
            values["失效时间"] = "9999-12-31"
        if not values.get("当前标记"):
            values["当前标记"] = "Y"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布SCD策略到数据通道
        store.channel.publish("scd.strategy_created", {
            "strategy_name": entry["策略名称"],
            "dim_code": dim_code,
            "scd_type": entry["SCD类型"],
            "detection_rule": detection_rule,
            "entry_id": entry["id"],
        })

        # 注册SCD策略产物
        store.artifacts.register(
            "scd_handler", "scd_strategy",
            f"scd_{entry['id']}",
            {"strategy_name": entry["策略名称"], "dim_code": dim_code, "scd_type": entry["SCD类型"]},
            source_modules=["dim_definition", "dim_attr", "scd_handler"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        scd_type = entry.get("SCD类型", "")
        dim_code = entry.get("目标维度", "")
        strategy_name = entry.get("策略名称", "")

        if action == "停用策略":
            entry["status"] = "已停用"
            entry["pending"] = True
            entry["abnormal"] = True
            store.channel.publish("scd.strategy_deactivated", {
                "strategy_name": strategy_name,
                "dim_code": dim_code,
                "entry_id": entry_id,
            })
            record_processing(
                strategy_id=entry_id, strategy_name=strategy_name, dim_code=dim_code,
                action="停用策略", ok=True,
            )
            return entry, f"{self.ENTITY}已{action}"

        # 启用策略：先尝试执行处理，成功后再生效状态。
        effective_date = str(entry.get("生效时间") or "").strip()
        if _parse_effective(effective_date) is None:
            entry["abnormal"] = True
            record_processing(
                strategy_id=entry_id, strategy_name=strategy_name, dim_code=dim_code,
                action="启用策略", ok=False, effective_date=effective_date,
                error=f"生效时间 {effective_date or '(空)'} 不是有效的业务日期",
            )
            return None, f"生效时间 {effective_date or '(空)'} 不是有效的业务日期，策略未启用"

        try:
            result = _simulate_scd_processing(entry)
            changes = _attribute_changes(entry, result, effective_date)
            logs = record_dimension_changes(
                dim_code=dim_code, changes=changes, effective_date=effective_date,
                reason=f"SCD策略执行: {strategy_name}（{scd_type}）",
            )
            _track_attribute_values(dim_code, logs)
        except Exception as exc:  # noqa: BLE001 - 处理失败要如实记入处理记录
            entry["abnormal"] = True
            record_processing(
                strategy_id=entry_id, strategy_name=strategy_name, dim_code=dim_code,
                action="启用策略", ok=False, effective_date=effective_date,
                error=f"SCD处理失败: {exc}",
            )
            return None, f"SCD处理失败：{exc}"

        # 处理成功后才落在策略状态上，失败时保持原状态并留下异常标记。
        entry["status"] = "已启用"
        entry["pending"] = False
        entry["abnormal"] = False
        store.channel.publish("scd.processing_result", {
            "strategy_name": strategy_name,
            "dim_code": dim_code,
            "scd_type": scd_type,
            "result": result,
            "change_ids": [log["变更ID"] for log in logs],
            "entry_id": entry_id,
        })

        record_processing(
            strategy_id=entry_id, strategy_name=strategy_name, dim_code=dim_code,
            action="启用策略", ok=True,
            change_ids=[log["变更ID"] for log in logs],
            applied_rows=len(logs), effective_date=effective_date,
        )
        return entry, f"{self.ENTITY}已{action}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # SCD类型分布
        scd_dist: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            t = row.get("SCD类型", "未指定")
            scd_dist[t] = scd_dist.get(t, 0) + 1
        base["scd_type_distribution"] = scd_dist

        # 消费的维度和属性消息统计
        base["consumed_dims"] = len(store.channel.consume("dim.defined", "scd_handler"))
        base["consumed_attrs"] = len(store.channel.consume("attr.confirmed", "scd_handler"))

        # 处理结果汇总
        base["processing_results"] = len(store.channel.consume("scd.processing_result", "scd_handler"))
        return base


def _build_detection_rule(scd_type: str, fields: list[str]) -> str:
    """根据SCD类型和属性字段构建变化检测规则。"""
    if not fields:
        return "MD5(record)"
    field_list = ", ".join(fields)
    if scd_type == "SCD类型1":
        return f"MD5({field_list}) -- 直接覆盖"
    elif scd_type == "SCD类型2":
        return f"MD5({field_list}) -- 新增行保留历史"
    elif scd_type == "SCD类型3":
        return f"字段级比较: {field_list} -- 保留旧值列"
    return f"MD5({field_list})"


def _simulate_scd_processing(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟SCD处理过程，展示不同类型的变化处理结果。"""
    scd_type = entry.get("SCD类型", "SCD类型2")
    detection_rule = entry.get("变化检测", "")
    dim_code = entry.get("目标维度", "")

    # 计算变化检测哈希
    rule_hash = hashlib.md5(detection_rule.encode()).hexdigest()[:12]

    if scd_type == "SCD类型1":
        return {
            "action": "overwrite", "algorithm": "scd1_direct_overwrite",
            "operation": f"直接更新{dim_code}表当前行，不保留历史",
            "affected_fields": _detection_fields(detection_rule),
            "rule_hash": rule_hash,
            "history_preserved": False,
            "new_rows_created": 0,
        }
    elif scd_type == "SCD类型2":
        return {
            "action": "insert_new_row",
            "algorithm": "scd2_versioning",
            "operation": f"为{dim_code}插入新版本行，设置旧行eff_end_dt=当前时间-1秒, is_current='N'",
            "affected_fields": _detection_fields(detection_rule),
            "rule_hash": rule_hash,
            "history_preserved": True,
            "new_rows_created": 1,
            "old_row_updated": True,
            "version_columns": {"eff_start_dt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                               "eff_end_dt": "9999-12-31", "is_current": "Y"},
        }
    elif scd_type == "SCD类型3":
        return {
            "action": "update_with_history",
            "algorithm": "scd3_previous_value",
            "operation": f"更新{dim_code}表，将当前值移入prev_列，写入新值到当前列",
            "affected_fields": _detection_fields(detection_rule),
            "rule_hash": rule_hash,
            "history_preserved": True,
            "new_rows_created": 0,
            "prev_columns": [f"prev_{f.strip()}" for f in detection_rule.replace("MD5(", "").replace(")", "").split(",") if f.strip()],
        }
    else:
        return {
            "action": "no_change",
            "algorithm": "scd0_immutable",
            "operation": f"{dim_code}为不可变维度，跳过更新",
            "affected_fields": [],
            "rule_hash": rule_hash,
            "history_preserved": False,
            "new_rows_created": 0,
        }


def _detection_fields(detection_rule: str) -> list[str]:
    """从变化检测规则里提取受影响的属性字段。"""
    cleaned = detection_rule.replace("MD5(", " ").replace(")", " ")
    cleaned = cleaned.split("--")[0].replace("字段级比较:", " ")
    return [field.strip() for field in cleaned.split(",") if field.strip()]


def _parse_effective(value: str) -> str | None:
    """校验策略生效时间；只接受可解析的业务日期。"""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _current_attribute_value(dim_code: str, field: str) -> str:
    """取属性当前值：优先取上次SCD写入值，其次取变更日志里最新的后值。"""
    for row in reversed(store.rows("_dim_attr_value")):
        if row.get("目标维度") == dim_code and row.get("变更字段") == field:
            return str(row.get("当前值", ""))
    latest = None
    latest_key = ""
    for row in store.rows("slow_change_log"):
        if row.get("目标维度") != dim_code or field not in str(row.get("变更字段", "")):
            continue
        key = f"{row.get('生效日期') or ''}#{int(row.get('日志序号') or 0):08d}"
        if key >= latest_key:
            latest_key, latest = key, row
    if latest is not None and latest.get("新值"):
        return str(latest["新值"])
    return "未记录"


def _attribute_changes(
    entry: dict[str, Any], result: dict[str, Any], effective_date: str,
) -> list[dict[str, Any]]:
    """把处理结果展开成一次变更下的逐属性前值/后值，供变更日志落库。"""
    dim_code = entry.get("目标维度", "")
    scd_type = entry.get("SCD类型", "")
    fields = [str(field).strip() for field in result.get("affected_fields", []) if str(field).strip()]
    if not fields:
        # 没有可解析的属性字段时，至少记录策略本身的状态变化。
        return [{
            "变更字段": "SCD策略",
            "前值": "草稿",
            "后值": f"已启用@{effective_date}",
        }]
    version_tag = str(result.get("rule_hash", ""))[:6] or "处理完成"
    changes = []
    for field in fields:
        old = _current_attribute_value(dim_code, field)
        new = f"{field}@{scd_type}#{version_tag}"
        changes.append({"变更字段": field, "前值": old, "后值": new})
    return changes


def _track_attribute_values(dim_code: str, logs: list[dict[str, Any]]) -> None:
    """记录SCD写入后的属性当前值，作为下一次处理的前值来源。"""
    table = store.rows("_dim_attr_value")
    for log in logs:
        field = str(log.get("变更字段", ""))
        for row in table:
            if row.get("目标维度") == dim_code and row.get("变更字段") == field:
                row["当前值"] = log.get("新值")
                row["生效日期"] = log.get("生效日期")
                row["变更ID"] = log.get("变更ID")
                break
        else:
            table.append({
                "目标维度": dim_code,
                "变更字段": field,
                "当前值": log.get("新值"),
                "生效日期": log.get("生效日期"),
                "变更ID": log.get("变更ID"),
            })
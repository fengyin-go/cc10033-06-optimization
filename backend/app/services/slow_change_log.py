"""变更日志服务：记录维度属性变更，并按业务生效日期导出可复核的履历文件。

增强能力：
- 消费 attr.confirmed / scd.processing_result 通道自动记录变更
- SCD 处理写入属性前值、后值和生效日期，导出接口据此提取业务履历
- 按生效日期 + 变更ID 导出，同一条变更只占一行，跨页顺序固定
- 导出文件带日志截止点（截取边界），断点续传时超出边界的补录行不会混入
- 交付文件登记摘要，可随时按生成时的截取边界复核
- 策略启停和后续处理失败写入处理记录
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import date, datetime, timezone
from typing import Any

from app.services.base import BaseModuleService, ExportFile, csv_safe
from app.store import store

SESSION_TABLE = "_change_export_session"
DELIVERED_TABLE = "_change_export_delivered"
FILE_TABLE = "_change_export_file"
PROCESSING_TABLE = "_scd_processing_record"

EXPORT_COLUMNS = [
    "变更ID", "目标维度", "变更字段", "前值", "后值",
    "生效日期", "日志序号", "变更时间", "变更原因",
]
BOUNDARY_LABEL = "#日志截止点"
MAX_PAGE_SIZE = 200

STATUS_DELIVERING = "导出中"
STATUS_DELIVERED = "已交付"


class ExportError(ValueError):
    """导出参数或状态不合法，由路由层转成 4xx。"""


class SlowChangeLogService(BaseModuleService):
    MODULE = "slow_change_log"
    ENTITY = "维度变更"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["变更ID", "目标维度", "变更字段", "旧值", "新值", "变更时间", "变更原因"]
    REQUIRED_FIELDS = ["变更ID", "目标维度", "变更字段"]
    STATUS_ORDER = ["已记录", "已追溯", "已归档"]
    ACTION_RULES = {"追溯变更": "已记录", "归档记录": "已追溯"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 登记与动作
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 消费SCD处理结果
        scd_msgs = store.channel.consume("scd.processing_result", "slow_change_log")
        for msg in scd_msgs:
            dim_code = msg["payload"]["dim_code"]
            if dim_code == str(values.get("目标维度", "")):
                values["变更原因"] = f"SCD策略执行: {msg['payload']['strategy_name']}"
                values["变更字段"] = "SCD策略执行"
                values["旧值"] = "执行前"
                values["新值"] = msg["payload"]["result"].get("action", "已处理")

        if not values.get("变更时间"):
            values["变更时间"] = date.today().isoformat()
        if not values.get("生效日期"):
            values["生效日期"] = str(values["变更时间"])[:10]

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        entry["生效日期"] = str(values.get("生效日期") or values["变更时间"])[:10]
        # 统一分配日志序号与登记时间，让导出边界对人工登记同样生效。
        entry["记录时间"] = datetime.now(timezone.utc).isoformat()
        entry["日志序号"] = store.next_change_log_seq()
        entry["来源"] = values.get("来源") or "人工登记"

        # 同一变更ID 的第二条属性追加时，把整组变更登记成一个完整产物。
        group_size = sum(
            1 for row in store.rows(self.MODULE) if row.get("变更ID") == entry["变更ID"])

        store.channel.publish("change_log.recorded", {
            "change_id": entry["变更ID"],
            "dim_code": entry.get("目标维度"),
            "field": entry["变更字段"],
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "slow_change_log", "change_record",
            f"change_{entry['id']}",
            {"change_id": entry["变更ID"], "dim_code": entry.get("目标维度"),
             "attribute_rows": group_size},
            ["dim_definition", "scd_handler", "slow_change_log"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "追溯变更":
            trace = _trace_change_history(entry)
            store.channel.publish("change_log.traced", {
                "change_id": entry["变更ID"],
                "history": trace,
                "entry_id": entry_id,
            })
        elif action == "归档记录":
            store.channel.publish("change_log.archived", {
                "change_id": entry["变更ID"],
                "entry_id": entry_id,
            })
        return entry, message

    def export_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        params: dict[str, str] | None = None,
    ) -> ExportFile:
        """变更日志清单导出：按生效日期和变更ID生成带截取边界的交付文件。

        每次点击导出都会新建一个冻结边界的批次，并登记到交付台账，
        之后可以按台账里的边界复核对账。
        """
        query = dict(params or {})
        filters = filters or {}
        session = self.create_export(
            date_from=query.get("from") or filters.get("生效日期起"),
            date_to=query.get("to") or filters.get("生效日期止"),
            change_id=query.get("change_id") or filters.get("变更ID"),
            dim_code=query.get("dim") or filters.get("目标维度"),
            status=status,
            keyword=keyword or filters.get("变更字段"),
            page_size=MAX_PAGE_SIZE,
        )
        manifest = self.complete_export(session["export_id"])
        result = self.get_export_file(manifest["export_id"])
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # 导出会话：创建 / 分页 / 交付 / 台账 / 复核
    # ------------------------------------------------------------------
    def create_export(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        change_id: str | None = None,
        dim_code: str | None = None,
        page_size: int = 20,
        boundary: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        """按业务生效日期和变更ID创建导出会话，并冻结本次的截取边界。

        边界取当前最大日志序号，之后补录的变更（即使生效日期落在原区间）
        序号一定更大，因此续做页面时不会被带进已交付的文件。
        传入 boundary 时沿用既有边界，用于中断后续做或复核重下。
        """
        start = _parse_date(date_from, "生效日期起")
        end = _parse_date(date_to, "生效日期止")
        if start and end and start > end:
            raise ExportError("生效日期起不能晚于生效日期止")
        size = int(page_size or 20)
        size = max(1, min(size, MAX_PAGE_SIZE))
        store.ensure_change_log_sequence()
        current = store._change_log_seq
        if boundary is None:
            frozen = current
        else:
            frozen = max(0, min(int(boundary), current))

        rows = _collect_changes(
            boundary=frozen,
            date_from=start,
            date_to=end,
            change_id=change_id,
            dim_code=dim_code,
            status=status,
            keyword=keyword,
        )
        total = len(rows)
        session = {
            "export_id": f"EXP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}",
            "module": self.MODULE,
            "status": STATUS_DELIVERING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "boundary": frozen,
            "boundary_source": "沿用" if boundary is not None else "新建",
            "date_from": start or "",
            "date_to": end or "",
            "change_id": (change_id or "").strip(),
            "dim_code": (dim_code or "").strip(),
            "status_filter": (status or "").strip(),
            "keyword": (keyword or "").strip(),
            "page_size": size,
            "total_rows": total,
            "total_pages": _page_count(total, size),
            "pages_fetched": [],
            "rows": rows,
            "delivered_at": None,
            "sha256": "",
        }
        store.rows(SESSION_TABLE).append(session)
        return _session_summary(session)

    def resume_export(self, export_id: str) -> dict[str, Any]:
        """中断后续做：沿用原批次冻结的边界，返回尚未取回的页码。"""
        session = self._require_session(export_id)
        remaining = [
            page for page in range(1, session["total_pages"] + 1)
            if page not in session["pages_fetched"]
        ]
        summary = _session_summary(session)
        summary["remaining_pages"] = remaining
        summary["boundary_source"] = session.get("boundary_source", "新建")
        return summary

    def export_page(self, export_id: str, page: int) -> dict[str, Any]:
        """取一页导出数据，页内顺序与整份文件一致（生效日期、变更ID）。"""
        session = self._require_session(export_id)
        page = max(1, int(page))
        if page > session["total_pages"] and page != 1:
            raise ExportError(f"页码超出范围，共 {session['total_pages']} 页")
        size = session["page_size"]
        start = (page - 1) * size
        chunk = session["rows"][start:start + size]
        if page not in session["pages_fetched"]:
            session["pages_fetched"].append(page)
            session["pages_fetched"].sort()
        return {
            "export_id": export_id,
            "status": session["status"],
            "boundary": session["boundary"],
            "page": page,
            "page_size": size,
            "total_rows": session["total_rows"],
            "total_pages": session["total_pages"],
            "pages_fetched": list(session["pages_fetched"]),
            "next_page": page + 1 if page < session["total_pages"] else None,
            "header": _render_header(session) if page == 1 else "",
            "csv": _render_rows(chunk),
            "rows": chunk,
        }

    def complete_export(self, export_id: str) -> dict[str, Any]:
        """收口导出：按冻结的边界和顺序生成文件并登记交付台账。"""
        session = self._require_session(export_id)
        content = _render_header(session) + _render_rows(session["rows"])
        digest = _sha256(_render_rows(session["rows"]))
        delivered = {
            "export_id": export_id,
            "module": self.MODULE,
            "status": STATUS_DELIVERED,
            "filename": f"slow_change_log_{export_id}.csv",
            "boundary": session["boundary"],
            "date_from": session["date_from"],
            "date_to": session["date_to"],
            "change_id": session["change_id"],
            "dim_code": session["dim_code"],
            "status_filter": session.get("status_filter", ""),
            "keyword": session.get("keyword", ""),
            "row_count": session["total_rows"],
            "page_size": session["page_size"],
            "pages": session["total_pages"],
            "pages_fetched": list(session["pages_fetched"]),
            "sha256": digest,
            "created_at": session["created_at"],
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }
        session["status"] = STATUS_DELIVERED
        session["delivered_at"] = delivered["delivered_at"]
        session["sha256"] = digest

        delivered_rows = store.rows(DELIVERED_TABLE)
        existing = next((row for row in delivered_rows if row["export_id"] == export_id), None)
        if existing is None:
            delivered_rows.append(delivered)
        else:
            existing.update(delivered)
        files = store.rows(FILE_TABLE)
        file_entry = next((row for row in files if row["export_id"] == export_id), None)
        if file_entry is None:
            files.append({"export_id": export_id, "content": content})
        else:
            file_entry["content"] = content

        store.artifacts.register(
            "slow_change_log", "change_export", export_id,
            {"boundary": session["boundary"], "row_count": session["total_rows"],
             "sha256": digest},
            ["slow_change_log"],
        )
        store.channel.publish("change_log.exported", {
            "export_id": export_id,
            "boundary": session["boundary"],
            "row_count": session["total_rows"],
        })
        return dict(delivered)

    def list_exports(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(row) for row in store.rows(DELIVERED_TABLE)),
            key=lambda row: row.get("delivered_at") or "",
            reverse=True,
        )

    def get_export(self, export_id: str) -> dict[str, Any] | None:
        for row in store.rows(DELIVERED_TABLE):
            if row["export_id"] == export_id:
                return dict(row)
        for row in store.rows(SESSION_TABLE):
            if row["export_id"] == export_id:
                return _session_summary(row)
        return None

    def get_export_file(self, export_id: str) -> ExportFile | None:
        for row in store.rows(FILE_TABLE):
            if row["export_id"] == export_id:
                delivered = self.get_export(export_id) or {}
                return ExportFile(
                    content=row["content"],
                    filename=delivered.get("filename", f"slow_change_log_{export_id}.csv"),
                    headers={
                        "X-Export-Boundary": str(delivered.get("boundary", "")),
                        "X-Export-Row-Count": str(delivered.get("row_count", "")),
                    },
                )
        return None

    def verify_export(self, export_id: str) -> dict[str, Any] | None:
        """按生成时记录的截取边界复核交付文件。"""
        manifest = self.get_export(export_id)
        if manifest is None or manifest.get("status") != STATUS_DELIVERED:
            return None
        content = next(
            (row["content"] for row in store.rows(FILE_TABLE) if row["export_id"] == export_id),
            None,
        )
        if content is None:
            return {"export_id": export_id, "ok": False, "errors": ["交付文件不存在"]}

        parsed = _parse_export_file(content)
        errors: list[str] = []
        boundary = int(manifest.get("boundary") or 0)
        if parsed["boundary"] != boundary:
            errors.append(
                f"文件日志截止点 {parsed['boundary']} 与台账记录 {boundary} 不一致")
        seen: set[str] = set()
        for row in parsed["rows"]:
            change_id = row.get("变更ID", "")
            if change_id in seen:
                errors.append(f"变更ID {change_id} 重复占行")
            seen.add(change_id)
            try:
                seq = int(row.get("日志序号") or 0)
            except ValueError:
                seq = 0
            if seq <= 0 or seq > boundary:
                errors.append(f"变更ID {change_id} 的日志序号 {row.get('日志序号')} 超出截取边界")
            effective = str(row.get("生效日期") or "")
            if manifest.get("date_from") and effective < manifest["date_from"]:
                errors.append(f"变更ID {change_id} 的生效日期早于查询区间")
            if manifest.get("date_to") and effective > manifest["date_to"]:
                errors.append(f"变更ID {change_id} 的生效日期晚于查询区间")
        digest = _sha256("\n".join(_row_line(row) for row in parsed["rows"]) + ("\n" if parsed["rows"] else ""))
        if digest != manifest.get("sha256"):
            errors.append("文件内容摘要与交付台账不一致")
        if len(parsed["rows"]) != int(manifest.get("row_count") or 0):
            errors.append("文件行数与交付台账不一致")

        # 按生成时记录的截取边界回放源日志，复核文件行与边界内履历一一对应。
        replay = _collect_changes(
            boundary=boundary,
            date_from=manifest.get("date_from") or None,
            date_to=manifest.get("date_to") or None,
            change_id=manifest.get("change_id") or None,
            dim_code=manifest.get("dim_code") or None,
            status=manifest.get("status_filter") or None,
            keyword=manifest.get("keyword") or None,
        )
        file_ids = [row.get("变更ID", "") for row in parsed["rows"]]
        replay_ids = [row["变更ID"] for row in replay]
        if file_ids != replay_ids:
            missing = sorted(set(replay_ids) - set(file_ids))
            extra = sorted(set(file_ids) - set(replay_ids))
            if missing:
                errors.append(f"截取边界内缺失变更：{'、'.join(missing)}")
            if extra:
                errors.append(f"文件含边界外或不存在的变更：{'、'.join(extra)}")
            if not missing and not extra and file_ids != replay_ids:
                errors.append("文件行顺序与截取边界内的固定顺序不一致")
        return {
            "export_id": export_id,
            "ok": not errors,
            "boundary": boundary,
            "row_count": len(parsed["rows"]),
            "replay_row_count": len(replay_ids),
            "sha256": digest,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # 处理记录
    # ------------------------------------------------------------------
    def list_processing_records(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(row) for row in store.rows(PROCESSING_TABLE)),
            key=lambda row: (row.get("处理时间") or "", int(row.get("id") or 0)),
            reverse=True,
        )

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        by_dim: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            dim = row.get("目标维度", "未知")
            by_dim[dim] = by_dim.get(dim, 0) + 1
        base["changes_by_dimension"] = by_dim
        base["consumed_scd_results"] = len(store.channel.consume("scd.processing_result", "slow_change_log"))
        base["delivered_exports"] = len(store.rows(DELIVERED_TABLE))
        base["processing_failures"] = sum(
            1 for row in store.rows(PROCESSING_TABLE) if row.get("状态") == "失败")
        return base

    def _require_session(self, export_id: str) -> dict[str, Any]:
        for row in store.rows(SESSION_TABLE):
            if row["export_id"] == export_id:
                return row
        raise ExportError(f"导出批次 {export_id} 不存在")


# ----------------------------------------------------------------------
# 业务履历提取与导出渲染
# ----------------------------------------------------------------------
def record_dimension_changes(
    *,
    dim_code: str,
    changes: list[dict[str, Any]],
    effective_date: str | None = None,
    change_id: str | None = None,
    reason: str = "",
    origin: str = "SCD处理",
) -> list[dict[str, Any]]:
    """写入一条变更下的多个属性前值/后值，共用同一个变更ID。

    SCD 处理落库时调用：同一变更ID 的属性行在导出时按变更ID 合并为一行，
    履历仍然逐属性保留前值、后值和生效日期。
    """
    if not changes:
        return []
    effective = _parse_date(effective_date, "生效日期") or date.today().isoformat()
    new_change_id = (change_id or "").strip() or _next_change_id(effective)
    now = datetime.now(timezone.utc).isoformat()
    created: list[dict[str, Any]] = []
    for change in changes:
        field = str(change.get("变更字段") or change.get("field") or "").strip()
        if not field:
            continue
        entry = {
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "变更ID": new_change_id,
            "目标维度": dim_code,
            "变更字段": field,
            "旧值": str(change.get("前值", change.get("old", ""))),
            "新值": str(change.get("后值", change.get("new", ""))),
            "变更时间": effective,
            "生效日期": effective,
            "记录时间": now,
            "变更原因": reason or f"{origin}: {field}",
            "来源": origin,
        }
        created.append(store.append_change_log(entry))
    return created


def record_processing(
    *,
    strategy_id: int | None,
    strategy_name: str,
    dim_code: str,
    action: str,
    ok: bool,
    change_ids: list[str] | None = None,
    applied_rows: int = 0,
    effective_date: str = "",
    error: str = "",
) -> dict[str, Any]:
    """记录SCD策略启停及后续操作结果，失败原因一并留痕。"""
    rows = store.rows(PROCESSING_TABLE)
    record = {
        "id": max((int(row.get("id") or 0) for row in rows), default=0) + 1,
        "处理时间": datetime.now(timezone.utc).isoformat(),
        "策略ID": strategy_id,
        "策略名称": strategy_name,
        "目标维度": dim_code,
        "操作": action,
        "状态": "成功" if ok else "失败",
        "变更ID": ",".join(change_ids or []),
        "写入行数": applied_rows,
        "生效日期": effective_date,
        "日志边界": store._change_log_seq,
        "错误": error,
    }
    rows.append(record)
    store.channel.publish("scd.processing_record", record)
    return record


def _collect_changes(
    *,
    boundary: int,
    date_from: str | None,
    date_to: str | None,
    change_id: str | None,
    dim_code: str | None,
    status: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    """在截取边界内提取实际生效的业务履历，并按变更ID 合并为单行。"""
    wanted_id = (change_id or "").strip()
    wanted_dim = (dim_code or "").strip()
    wanted_status = (status or "").strip()
    wanted_keyword = (keyword or "").strip().casefold()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("slow_change_log"):
        seq = int(row.get("日志序号") or 0)
        if seq <= 0 or seq > boundary:
            continue
        effective = str(row.get("生效日期") or str(row.get("变更时间") or "")[:10])
        if not effective:
            continue
        if date_from and effective < date_from:
            continue
        if date_to and effective > date_to:
            continue
        if wanted_id and wanted_id.casefold() not in str(row.get("变更ID", "")).casefold():
            continue
        if wanted_dim and wanted_dim.casefold() != str(row.get("目标维度", "")).casefold():
            continue
        if wanted_status and row.get("status") != wanted_status:
            continue
        if wanted_keyword and wanted_keyword not in str(row.get("变更字段", "")).casefold():
            continue
        grouped.setdefault(str(row.get("变更ID", "")), []).append(row)

    merged: list[dict[str, Any]] = []
    for cid, rows in grouped.items():
        rows.sort(key=lambda item: int(item.get("日志序号") or 0))
        first_seq = int(rows[0].get("日志序号") or 0)
        fields = _distinct(str(row.get("变更字段", "")) for row in rows)
        old_values = _distinct(str(row.get("旧值", "")) for row in rows)
        new_values = _distinct(str(row.get("新值", "")) for row in rows)
        merged.append({
            "变更ID": cid,
            "目标维度": rows[-1].get("目标维度", ""),
            "变更字段": "、".join(fields),
            "前值": "、".join(old_values) if len(old_values) > 1 else (old_values or [""])[0],
            "后值": "、".join(new_values) if len(new_values) > 1 else (new_values or [""])[0],
            "生效日期": min(str(row.get("生效日期") or "") for row in rows),
            "日志序号": first_seq,
            "变更时间": max(str(row.get("变更时间") or "") for row in rows),
            "变更原因": rows[-1].get("变更原因", ""),
            "_merge_count": len(rows),
        })
    # 跨页顺序固定为：生效日期升序，其次变更ID升序。
    merged.sort(key=lambda row: (row["生效日期"], row["变更ID"]))
    return merged


def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
    return {
        key: session[key]
        for key in (
            "export_id", "status", "created_at", "boundary", "boundary_source",
            "date_from", "date_to", "change_id", "dim_code", "status_filter",
            "keyword", "page_size", "total_rows", "total_pages", "pages_fetched",
            "delivered_at", "sha256",
        )
    }


def _render_header(session: dict[str, Any]) -> str:
    lines = [
        "#维度变更导出文件",
        f"#交付批次,{session['export_id']}",
        f"{BOUNDARY_LABEL},{session['boundary']}",
        f"#生效日期范围,{session['date_from'] or '不限'}~{session['date_to'] or '不限'}",
        f"#变更ID,{session['change_id'] or '全部'}",
        f"#目标维度,{session['dim_code'] or '全部'}",
        f"#行数,{session['total_rows']}",
        f"#导出时间,{session['created_at']}",
    ]
    output = io.StringIO()
    output.write("﻿")
    for line in lines:
        output.write(line + "\n")
    output.write("\n")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    return output.getvalue()


def _render_rows(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    for row in rows:
        output.write(_row_line(row) + "\n")
    return output.getvalue()


def _row_line(row: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="")
    writer.writerow([csv_safe(row.get(column, "")) for column in EXPORT_COLUMNS])
    return output.getvalue()


def _parse_export_file(content: str) -> dict[str, Any]:
    """解析交付文件：读取截止点注释行，其余非空、非注释行按 CSV 读回。"""
    boundary = 0
    body: list[str] = []
    for line in content.lstrip("﻿").splitlines():
        if line.startswith("#"):
            if line.startswith(f"{BOUNDARY_LABEL},"):
                try:
                    boundary = int(line.split(",", 1)[1])
                except ValueError:
                    boundary = 0
            continue
        if line.strip():
            body.append(line)
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    rows = [dict(row) for row in reader]
    return {"boundary": boundary, "rows": rows}


def _page_count(total: int, size: int) -> int:
    return max(1, (total + size - 1) // size)


def _distinct(values: Any) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _next_change_id(effective: str) -> str:
    stamp = effective.replace("-", "")
    rows = store.rows("slow_change_log")
    existing = {str(row.get("变更ID", "")) for row in rows}
    index = len(rows) + 1
    while True:
        candidate = f"SCL_{stamp}_{index:03d}"
        if candidate not in existing:
            return candidate
        index += 1


def _parse_date(value: str | None, label: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ExportError(f"{label}格式应为 YYYY-MM-DD") from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trace_change_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    target_dim = entry.get("目标维度", "")
    related = []
    for row in store.rows("slow_change_log"):
        if row.get("目标维度") == target_dim and row.get("变更ID") != entry["变更ID"]:
            related.append({"change_id": row.get("变更ID"), "field": row.get("变更字段"),
                          "old_val": row.get("旧值"), "new_val": row.get("新值"),
                          "time": row.get("变更时间")})
    return related

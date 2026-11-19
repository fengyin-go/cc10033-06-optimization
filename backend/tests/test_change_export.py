"""变更履历导出测试：截取边界、单行去重、跨页顺序、断线续做和交付复核。"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services.slow_change_log import record_dimension_changes
from app.store import store

INTERNAL_TABLES = (
    "_scd_processing_record",
    "_change_export_session",
    "_change_export_delivered",
    "_change_export_file",
    "_dim_attr_value",
)


@pytest.fixture
def api() -> TestClient:
    """每个用例回到种子数据，避免导出批次和变更记录互相污染。"""
    saved_tables = copy.deepcopy(store._tables)
    saved_seq = store._change_log_seq
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    for name in INTERNAL_TABLES:
        store._tables[name] = []
    store._change_log_seq = 0
    store.ensure_change_log_sequence()
    try:
        yield TestClient(app)
    finally:
        store._tables = saved_tables
        store._change_log_seq = saved_seq


def _page(client: TestClient, export_id: str, page: int) -> dict:
    response = client.get(f"/api/slow_change_log/exports/{export_id}/pages/{page}")
    assert response.status_code == 200, response.text
    return response.json()


def _all_rows(client: TestClient, export_id: str, total_pages: int) -> list[str]:
    change_ids: list[str] = []
    for page in range(1, total_pages + 1):
        change_ids.extend(row["变更ID"] for row in _page(client, export_id, page)["rows"])
    return change_ids


# ----------------------------------------------------------------------
# 截取边界
# ----------------------------------------------------------------------
class TestExportBoundary:
    def test_late_backdated_change_stays_out_of_frozen_boundary(self, api: TestClient) -> None:
        """下载中迟到的等级变化即使生效日落在查询月份内，也不进已冻结批次。"""
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-09-01", "to": "2026-09-30", "page_size": 10},
        ).json()
        assert session["boundary"] == 5
        assert session["total_rows"] == 2

        late = record_dimension_changes(
            dim_code="DIM_CUSTOMER",
            changes=[{"变更字段": "customer_level", "前值": "黄金", "后值": "铂金"}],
            effective_date="2026-09-20",
            reason="迟到的等级变化",
        )
        assert late[0]["日志序号"] == 6

        page = _page(api, session["export_id"], 1)
        assert page["total_rows"] == 2
        assert "SCL_20260920" not in "".join(row["变更ID"] for row in page["rows"])

        manifest = api.post(f"/api/slow_change_log/exports/{session['export_id']}/complete").json()
        assert manifest["boundary"] == 5
        assert manifest["row_count"] == 2
        assert len(api.get(f"/api/slow_change_log/exports/{session['export_id']}/file").text.splitlines()) > 2

    def test_new_batch_picks_up_backdated_change(self, api: TestClient) -> None:
        """另起新批次时可以取到补录的生效日期在原月份内的变更。"""
        first = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-09-01", "to": "2026-09-30"},
        ).json()
        api.post(f"/api/slow_change_log/exports/{first['export_id']}/complete")
        record_dimension_changes(
            dim_code="DIM_CUSTOMER",
            changes=[{"变更字段": "customer_level", "前值": "黄金", "后值": "铂金"}],
            effective_date="2026-09-20",
        )
        second = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-09-01", "to": "2026-09-30"},
        ).json()
        assert second["boundary"] == 6
        assert second["total_rows"] == 3
        ids = _all_rows(api, second["export_id"], second["total_pages"])
        assert "SCL_20260920_006" in ids

    def test_resume_reuses_original_boundary(self, api: TestClient) -> None:
        """断线续做沿用原批次边界，迟到的补录行不会混入。"""
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-08-01", "to": "2026-10-31", "page_size": 2},
        ).json()
        _page(api, session["export_id"], 1)
        record_dimension_changes(
            dim_code="DIM_CUSTOMER",
            changes=[{"变更字段": "customer_level", "前值": "黄金", "后值": "铂金"}],
            effective_date="2026-09-20",
        )
        resumed = api.get(f"/api/slow_change_log/exports/{session['export_id']}/resume").json()
        assert resumed["boundary"] == session["boundary"]
        assert resumed["remaining_pages"] == [2]
        assert resumed["total_rows"] == session["total_rows"]


# ----------------------------------------------------------------------
# 同变更单行 + 跨页顺序
# ----------------------------------------------------------------------
class TestExportRows:
    def test_multi_attribute_change_takes_one_row(self, api: TestClient) -> None:
        """同一变更ID 的多条属性前后值合并为一行，履历仍然完整。"""
        logs = record_dimension_changes(
            dim_code="DIM_PRODUCT",
            changes=[
                {"变更字段": "product_name", "前值": "旧名", "后值": "新名"},
                {"变更字段": "product_category", "前值": "食品", "后值": "饮料"},
            ],
            effective_date="2026-07-01",
        )
        assert logs[0]["变更ID"] == logs[1]["变更ID"]
        session = api.post(
            "/api/slow_change_log/exports",
            params={"change_id": logs[0]["变更ID"]},
        ).json()
        rows = _page(api, session["export_id"], 1)["rows"]
        assert len(rows) == 1
        assert rows[0]["变更字段"] == "product_name、product_category"
        assert rows[0]["前值"] == "旧名、食品"
        assert rows[0]["后值"] == "新名、饮料"
        assert rows[0]["生效日期"] == "2026-07-01"

    def test_pages_follow_effective_date_then_change_id(self, api: TestClient) -> None:
        """跨页顺序固定为生效日期升序，其次变更ID升序。"""
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2025-01-01", "to": "2026-12-31", "page_size": 2},
        ).json()
        assert session["total_pages"] == 3
        ids = _all_rows(api, session["export_id"], session["total_pages"])
        assert ids == [
            "SCL_20250801_005",
            "SCL_20260901_001",
            "SCL_20260915_002",
            "SCL_20261001_003",
            "SCL_20261015_004",
        ]

    def test_change_id_filter_matches_single_row(self, api: TestClient) -> None:
        session = api.post(
            "/api/slow_change_log/exports",
            params={"change_id": "SCL_20261001_003"},
        ).json()
        rows = _page(api, session["export_id"], 1)["rows"]
        assert [row["变更ID"] for row in rows] == ["SCL_20261001_003"]


# ----------------------------------------------------------------------
# 交付文件与复核
# ----------------------------------------------------------------------
class TestDeliveredFile:
    def test_file_declares_boundary_and_every_row_is_inside(self, api: TestClient) -> None:
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-08-01", "to": "2026-10-31", "page_size": 2},
        ).json()
        _page(api, session["export_id"], 1)
        _page(api, session["export_id"], 2)
        manifest = api.post(f"/api/slow_change_log/exports/{session['export_id']}/complete").json()
        content = api.get(f"/api/slow_change_log/exports/{session['export_id']}/file").text
        assert f"#日志截止点,{manifest['boundary']}" in content

        body = [line for line in content.lstrip("﻿").splitlines() if line and not line.startswith("#")]
        header, rows = body[0], body[1:]
        assert header.startswith("变更ID,目标维度")
        seq_index = header.split(",").index("日志序号")
        for line in rows:
            assert int(line.split(",")[seq_index]) <= manifest["boundary"]

    def test_verify_passes_for_delivered_file(self, api: TestClient) -> None:
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-09-01", "to": "2026-09-30"},
        ).json()
        api.post(f"/api/slow_change_log/exports/{session['export_id']}/complete")
        report = api.get(f"/api/slow_change_log/exports/{session['export_id']}/verify").json()
        assert report["ok"] is True
        assert report["errors"] == []
        assert report["row_count"] == 2
        assert report["replay_row_count"] == 2

    def test_verify_rejects_file_with_row_outside_boundary(self, api: TestClient) -> None:
        """篡改交付文件塞入边界外的行时必须复核失败。"""
        session = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-09-01", "to": "2026-09-30"},
        ).json()
        export_id = session["export_id"]
        api.post(f"/api/slow_change_log/exports/{export_id}/complete")
        record_dimension_changes(
            dim_code="DIM_CUSTOMER",
            changes=[{"变更字段": "customer_level", "前值": "黄金", "后值": "铂金"}],
            effective_date="2026-09-20",
        )
        file_row = next(row for row in store.rows("_change_export_file") if row["export_id"] == export_id)
        extra = "SCL_20260920_006,DIM_CUSTOMER,customer_level,黄金,铂金,2026-09-20,6,2026-09-20,迟到\n"
        store.rows("_change_export_file").remove(file_row)
        store.rows("_change_export_file").append(
            {"export_id": export_id, "content": file_row["content"] + extra})

        report = api.get(f"/api/slow_change_log/exports/{export_id}/verify").json()
        assert report["ok"] is False
        assert any("超出截取边界" in error for error in report["errors"])
        assert any("边界外" in error for error in report["errors"])

    def test_disconnect_then_resume_produces_same_file_as_direct_export(self, api: TestClient) -> None:
        """断开含多页的下载、补录上月变更后，续做结果与边界内履历一致。"""
        interrupted = api.post(
            "/api/slow_change_log/exports",
            params={"from": "2026-08-01", "to": "2026-10-31", "page_size": 2},
        ).json()
        _page(api, interrupted["export_id"], 1)  # 断开：只取回第一页

        record_dimension_changes(
            dim_code="DIM_CUSTOMER",
            changes=[{"变更字段": "customer_level", "前值": "黄金", "后值": "铂金"}],
            effective_date="2026-08-20",
            reason="补录上月等级变动",
        )

        resumed = api.get(f"/api/slow_change_log/exports/{interrupted['export_id']}/resume").json()
        assert resumed["remaining_pages"] == [2]
        _page(api, interrupted["export_id"], 2)
        manifest = api.post(f"/api/slow_change_log/exports/{interrupted['export_id']}/complete").json()
        assert manifest["row_count"] == 4
        assert "SCL_20260820" not in manifest["change_id"]

        report = api.get(f"/api/slow_change_log/exports/{interrupted['export_id']}/verify").json()
        assert report["ok"] is True

        content = api.get(f"/api/slow_change_log/exports/{interrupted['export_id']}/file").text
        assert "SCL_20260820" not in content
        assert content.count("SCL_20260901_001") == 1

    def test_ledger_lists_delivered_manifest(self, api: TestClient) -> None:
        session = api.post("/api/slow_change_log/exports", params={"dim": "DIM_CUSTOMER"}).json()
        manifest = api.post(f"/api/slow_change_log/exports/{session['export_id']}/complete").json()
        ledger = api.get("/api/slow_change_log/exports").json()
        assert [row["export_id"] for row in ledger] == [manifest["export_id"]]
        assert ledger[0]["boundary"] == manifest["boundary"]
        assert ledger[0]["sha256"] == manifest["sha256"]


# ----------------------------------------------------------------------
# SCD 处理落库与处理记录
# ----------------------------------------------------------------------
class TestScdProcessing:
    def test_enable_writes_previous_and_new_value_with_effective_date(self, api: TestClient) -> None:
        response = api.post("/api/scd_handler/4/actions", json={"action": "启用策略"})
        assert response.json()["ok"] is True
        log = store.rows("slow_change_log")[-1]
        assert log["目标维度"] == "DIM_CHANNEL"
        assert log["变更字段"] == "channel_category"
        assert log["旧值"] and log["新值"] and log["旧值"] != log["新值"]
        assert log["生效日期"] == "2026-06-01"
        assert log["日志序号"] == 6

        record = api.get("/api/slow_change_log/processing-records").json()[0]
        assert record["状态"] == "成功"
        assert record["变更ID"] == log["变更ID"]
        assert record["写入行数"] == 1

    def test_second_run_uses_written_value_as_previous(self, api: TestClient) -> None:
        api.post("/api/scd_handler/4/actions", json={"action": "启用策略"})
        first = store.rows("slow_change_log")[-1]["新值"]
        api.post("/api/scd_handler/4/actions", json={"action": "启用策略"})
        second = store.rows("slow_change_log")[-1]
        assert second["旧值"] == first

    def test_invalid_effective_date_records_failure(self, api: TestClient) -> None:
        strategy = next(row for row in store.rows("scd_handler") if row["id"] == 4)
        strategy["生效时间"] = "2026-13-45"
        response = api.post("/api/scd_handler/4/actions", json={"action": "启用策略"}).json()
        assert response["ok"] is False
        assert "生效时间" in response["message"]
        assert strategy["status"] == "草稿"
        record = api.get("/api/slow_change_log/processing-records").json()[-1]
        assert record["状态"] == "失败"
        assert "生效时间" in record["错误"]

    def test_disable_is_recorded(self, api: TestClient) -> None:
        response = api.post("/api/scd_handler/2/actions", json={"action": "停用策略"}).json()
        assert response["ok"] is True
        record = api.get("/api/slow_change_log/processing-records").json()[0]
        assert record["操作"] == "停用策略"
        assert record["状态"] == "成功"

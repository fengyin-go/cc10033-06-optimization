"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services.slow_change_log import ExportError

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def _filters(service, request: Request) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }


def _stream_csv(content: str, filename: str, headers: dict[str, str] | None = None) -> StreamingResponse:
    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        **(headers or {}),
    }
    return StreamingResponse(
        iter([content]), media_type="text/csv; charset=utf-8", headers=response_headers)


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    items, total = service.list_entries(
        filters=_filters(service, request), keyword=keyword, status=status,
        page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    result = service.export_entries(
        filters=_filters(service, request), keyword=keyword, status=status,
        params=dict(request.query_params),
    )
    return _stream_csv(result.content, result.filename, result.headers)


# ---------------------------------------------------------------------------
# 导出交付台账：只读列表，供交付文件复核
# ---------------------------------------------------------------------------
@router.get("/{module}/exports", response_model=list[dict])
def list_exports(module: str) -> list[dict[str, Any]]:
    service = require_service(module)
    return service.list_exports()


@router.get("/{module}/processing-records", response_model=list[dict])
def list_processing_records(module: str) -> list[dict[str, Any]]:
    service = require_service(module)
    return service.list_processing_records()


@router.post("/{module}/exports", response_model=dict)
def create_export(
    module: str,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    change_id: str | None = Query(default=None, alias="change_id"),
    dim_code: str | None = Query(default=None, alias="dim"),
    page_size: int = Query(default=20, ge=1, le=200),
    boundary: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    service = require_service(module)
    if not hasattr(service, "create_export"):
        raise HTTPException(status_code=400, detail=f"{module} 不支持分批导出")
    try:
        return service.create_export(
            date_from=date_from, date_to=date_to, change_id=change_id,
            dim_code=dim_code, page_size=page_size, boundary=boundary)
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{module}/exports/{export_id}/resume", response_model=dict)
def resume_export(module: str, export_id: str) -> dict[str, Any]:
    service = require_service(module)
    if not hasattr(service, "resume_export"):
        raise HTTPException(status_code=400, detail=f"{module} 不支持导出续做")
    try:
        return service.resume_export(export_id)
    except ExportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{module}/exports/{export_id}", response_model=dict)
def get_export(module: str, export_id: str) -> dict[str, Any]:
    service = require_service(module)
    manifest = service.get_export(export_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"导出批次 {export_id} 不存在")
    return manifest


@router.get("/{module}/exports/{export_id}/pages/{page}", response_model=dict)
def get_export_page(module: str, export_id: str, page: int) -> dict[str, Any]:
    service = require_service(module)
    if not hasattr(service, "export_page"):
        raise HTTPException(status_code=400, detail=f"{module} 不支持分批导出")
    try:
        return service.export_page(export_id, page)
    except ExportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{module}/exports/{export_id}/complete", response_model=dict)
def complete_export(module: str, export_id: str) -> dict[str, Any]:
    service = require_service(module)
    if not hasattr(service, "complete_export"):
        raise HTTPException(status_code=400, detail=f"{module} 不支持分批导出")
    try:
        return service.complete_export(export_id)
    except ExportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{module}/exports/{export_id}/file")
def download_export_file(module: str, export_id: str) -> StreamingResponse:
    service = require_service(module)
    result = service.get_export_file(export_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"导出批次 {export_id} 尚未交付文件")
    return _stream_csv(result.content, result.filename, result.headers)


@router.get("/{module}/exports/{export_id}/verify", response_model=dict)
def verify_export(module: str, export_id: str) -> dict[str, Any]:
    service = require_service(module)
    if not hasattr(service, "verify_export"):
        raise HTTPException(status_code=400, detail=f"{module} 不支持导出复核")
    report = service.verify_export(export_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"导出批次 {export_id} 尚未交付文件")
    return report


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)

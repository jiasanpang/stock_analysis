# -*- coding: utf-8 -*-
"""Picker backtest endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.picker_backtest import (
    PickerBacktestRunRequest,
    PickerBacktestRunResponse,
    PickerBacktestResultItem,
    PickerBacktestSummary,
)
from src.services.picker_backtest_service import PickerBacktestService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory cache for last run (fallback to DB on cold start)
_last_run: Optional[Dict[str, Any]] = None


def _get_db():
    return DatabaseManager.get_instance()


def _last_run_or_db() -> Optional[Dict[str, Any]]:
    """Return in-memory last run, or latest from DB if cold start."""
    global _last_run
    if _last_run is not None:
        return _last_run
    try:
        return _get_db().get_latest_picker_backtest()
    except Exception as e:
        logger.debug(f"Failed to load latest picker backtest from DB: {e}")
        return None


@router.post(
    "/run",
    response_model=PickerBacktestRunResponse,
    summary="Run picker backtest",
    description="Run quantitative picker backtest over historical dates. Uses top N by score (no LLM).",
)
def run_picker_backtest(request: PickerBacktestRunRequest) -> PickerBacktestRunResponse:
    global _last_run
    try:
        service = PickerBacktestService()
        result = service.run(
            start_date=request.start_date,
            end_date=request.end_date,
            hold_days=request.hold_days,
            top_n=request.top_n,
            picker_mode=request.picker_mode,
            picker_leader_bias_exempt_pct=request.picker_leader_bias_exempt_pct,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        _last_run = result

        # Persist to DB
        try:
            _get_db().save_picker_backtest_history(
                result,
                start_date=request.start_date,
                end_date=request.end_date,
                hold_days=request.hold_days,
                top_n=request.top_n,
                picker_mode=request.picker_mode,
                picker_leader_bias_exempt_pct=request.picker_leader_bias_exempt_pct,
            )
        except Exception as e:
            logger.warning(f"Failed to save picker backtest history: {e}")

        return PickerBacktestRunResponse(
            success=True,
            results=[PickerBacktestResultItem(**r) for r in result.get("results", [])],
            summary=PickerBacktestSummary(**result["summary"]) if result.get("summary") else None,
            trade_dates_count=result.get("trade_dates_count", 0),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Picker backtest failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": str(exc)},
        )


@router.get(
    "/performance",
    summary="Get last picker backtest performance",
    description="Returns summary of the last run (from memory or DB).",
)
def get_picker_backtest_performance() -> Optional[PickerBacktestSummary]:
    data = _last_run_or_db()
    if data is None or not data.get("summary"):
        return None
    return PickerBacktestSummary(**data["summary"])


@router.get(
    "/results",
    summary="Get last picker backtest results",
    description="Returns detailed results of the last run (from memory or DB).",
)
def get_picker_backtest_results() -> Dict[str, Any]:
    data = _last_run_or_db()
    if data is None:
        return {"results": [], "summary": None}
    return {
        "results": data.get("results", []),
        "summary": data.get("summary"),
    }


@router.get(
    "/history",
    summary="List picker backtest history",
    description="Paginated list of past picker backtest runs.",
)
def list_picker_backtest_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    try:
        items, total = _get_db().get_picker_backtest_history_list(limit=limit, offset=offset)
        return {"items": items, "total": total}
    except Exception as e:
        logger.error(f"Picker backtest history list failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/history/{record_id}",
    summary="Get picker backtest run detail",
    description="Full detail of a single picker backtest run by id.",
)
def get_picker_backtest_history_detail(record_id: int) -> Dict[str, Any]:
    try:
        detail = _get_db().get_picker_backtest_history_detail(record_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Picker backtest record not found")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Picker backtest history detail failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

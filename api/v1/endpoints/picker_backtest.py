# -*- coding: utf-8 -*-
"""Picker backtest endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from api.v1.schemas.picker_backtest import (
    PickerBacktestRunRequest,
    PickerBacktestRunResponse,
    PickerBacktestResultItem,
    PickerBacktestSummary,
)
from src.services.picker_backtest_service import PickerBacktestService

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory cache for last run (optional; could use DB later)
_last_run: Optional[Dict[str, Any]] = None


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
    description="Returns summary of the last run (in-memory cache).",
)
def get_picker_backtest_performance() -> Optional[PickerBacktestSummary]:
    global _last_run
    if _last_run is None or not _last_run.get("summary"):
        return None
    return PickerBacktestSummary(**_last_run["summary"])


@router.get(
    "/results",
    summary="Get last picker backtest results",
    description="Returns detailed results of the last run (in-memory cache).",
)
def get_picker_backtest_results() -> Dict[str, Any]:
    global _last_run
    if _last_run is None:
        return {"results": [], "summary": None}
    return {
        "results": _last_run.get("results", []),
        "summary": _last_run.get("summary"),
    }

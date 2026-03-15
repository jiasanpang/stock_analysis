# -*- coding: utf-8 -*-
"""
AI Stock Picker API Endpoint

POST /api/v1/picker/recommend — two-stage pipeline:
  1. Quantitative screening (full market → shortlist)
  2. AI selection (shortlist + market intel → final picks)
GET  /api/v1/picker/history   — paginated list of past runs
GET  /api/v1/picker/history/{id} — full detail of a single run
"""

import asyncio
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

# Configurable via PICKER_TIMEOUT env (seconds). Default 300s for slow networks/APIs.
_PICKER_TIMEOUT = int(os.getenv("PICKER_TIMEOUT", "300"))


class ScreenStatsResponse(BaseModel):
    total_stocks: int = 0
    after_basic_filter: int = 0
    after_momentum_filter: int = 0
    after_volume_filter: int = 0
    final_pool: int = 0


class ScreenedStockResponse(BaseModel):
    code: str
    name: str
    price: float = 0
    change_pct: float = 0
    volume_ratio: float = 0
    turnover_rate: float = 0
    pe: float = 0
    pb: float = 0
    market_cap_yi: float = 0
    amount_yi: float = 0
    change_pct_60d: float = 0
    score: float = 0


class PickRecommendation(BaseModel):
    code: str
    name: str
    sector: str = ""
    reason: str = ""
    catalyst: str = ""
    attention: str = "medium"
    risk_note: str = ""


class PickerRecommendRequest(BaseModel):
    """Optional overrides for picker run. Omit to use .env config."""
    picker_mode: Optional[str] = Field(None, description="defensive | balanced | offensive")
    picker_leader_bias_exempt_pct: Optional[float] = Field(None, ge=0, le=20, description="Leader bias exemption %")


class PickerResponse(BaseModel):
    success: bool
    market_summary: str = ""
    picks: List[PickRecommendation] = Field(default_factory=list)
    sectors_to_watch: List[str] = Field(default_factory=list)
    risk_warning: str = ""
    screen_stats: Optional[ScreenStatsResponse] = None
    screened_pool: List[ScreenedStockResponse] = Field(default_factory=list)
    generated_at: str = ""
    elapsed_seconds: float = 0.0
    error: str = ""
    history_id: Optional[int] = None


# ── History response models ──────────────────────────────────────

class PickPreview(BaseModel):
    code: str = ""
    name: str = ""
    attention: str = ""


class PickerHistoryItem(BaseModel):
    id: int
    market_summary: str = ""
    pick_count: int = 0
    picks_preview: List[PickPreview] = Field(default_factory=list)
    sectors_to_watch: List[str] = Field(default_factory=list)
    elapsed_seconds: float = 0
    created_at: Optional[str] = None


class PickerHistoryListResponse(BaseModel):
    items: List[PickerHistoryItem] = Field(default_factory=list)
    total: int = 0


def _get_db():
    from src.storage import DatabaseManager
    return DatabaseManager.get_instance()


@router.post("/recommend", response_model=PickerResponse)
async def recommend_stocks(body: Optional[PickerRecommendRequest] = Body(None)):
    """
    Two-stage stock recommendation:
    Stage 1 — Quantitative screening from full A-share market
    Stage 2 — AI selection combining quant pool + market intelligence

    Optional body: picker_mode (defensive|balanced|offensive), picker_leader_bias_exempt_pct (0-20).
    """
    try:
        from src.services.stock_picker_service import StockPickerService

        req = body or PickerRecommendRequest()
        service = StockPickerService(
            picker_mode_override=req.picker_mode,
            picker_leader_bias_exempt_override=req.picker_leader_bias_exempt_pct,
        )
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, service.run),
            timeout=_PICKER_TIMEOUT,
        )
        result_dict = result.to_dict()

        history_id = None
        if result_dict.get("success"):
            try:
                history_id = _get_db().save_picker_history(result_dict)
                logger.info(f"[PickerAPI] Saved picker history id={history_id}")
            except Exception as exc:
                logger.warning(f"[PickerAPI] Failed to save history: {exc}")

        return PickerResponse(**result_dict, history_id=history_id)

    except asyncio.TimeoutError:
        logger.error(f"[PickerAPI] Timed out after {_PICKER_TIMEOUT}s")
        raise HTTPException(status_code=504, detail=f"Stock picker timed out after {_PICKER_TIMEOUT}s")
    except Exception as e:
        logger.error(f"[PickerAPI] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history", response_model=PickerHistoryListResponse)
async def list_picker_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Paginated list of past picker runs (newest first)."""
    try:
        items, total = _get_db().get_picker_history_list(limit=limit, offset=offset)
        return PickerHistoryListResponse(items=items, total=total)
    except Exception as e:
        logger.error(f"[PickerAPI] History list error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{record_id}", response_model=PickerResponse)
async def get_picker_history_detail(record_id: int):
    """Full detail of a single picker run."""
    try:
        detail = _get_db().get_picker_history_detail(record_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Picker history not found")
        return PickerResponse(**detail, history_id=record_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PickerAPI] History detail error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

#!/usr/bin/env python3
"""Reserved extension point for a future Music Atlas visualization backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from contracts import ContractError


class VisualizationRenderer(Protocol):
    """Contract a future renderer must implement without changing the pipeline."""

    def render_recommendation_card(
        self,
        bundle: dict[str, Any],
        analysis: dict[str, Any],
        output_path: Path,
    ) -> dict[str, Any]:
        ...


def render_recommendation_card(
    bundle: dict[str, Any],
    analysis: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    """Keep the old call boundary explicit until a new backend is supplied."""

    del bundle, analysis, output_path
    raise ContractError("Atlas 可视化渲染后端尚未配置；当前仅保留扩展接口")

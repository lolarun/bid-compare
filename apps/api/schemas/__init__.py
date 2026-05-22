"""Pydantic schemas — re-export all for convenience."""

from apps.api.schemas.common import PaginatedResponse, ImportResult
from apps.api.schemas.material import (
    MaterialBase, MaterialCreate, MaterialUpdate, MaterialOut,
    StandardizeRequest, StandardizeResult,
    ExtendedAttrField, ExtendedAttrSchema,
)
from apps.api.schemas.supplier import (
    SupplierBase, SupplierCreate, SupplierUpdate, SupplierOut,
)
from apps.api.schemas.project import (
    ProjectBase, ProjectCreate, ProjectUpdate, ProjectOut,
)
from apps.api.schemas.quote import (
    QuoteBase, QuoteCreate, QuoteUpdate, QuoteOut,
)
from apps.api.schemas.analysis import (
    PriceCompareRequest, PriceCompareResult,
    SupplierScoreRequest, SupplierScoreResult,
    CategoryStats, DashboardSummary,
    MultiCompareRequest, SupplierCompareItem, MultiCompareResult,
    SubCategoryStat, CategoryDetailStats,
    BidMatrixRequest, BidMatrixResult,
    BidInsightRequest, BidInsightResult,
    MatrixRow, MatrixTotal, SupplierCell, SupplierLabel,
    HistoricalAvg, ReasonableLowInfo,
    BrandTierCreate, BrandTierUpdate, BrandTierOut,
    ConfigUpdate, ConfigOut,
    TreeChild, TreeNode, DashboardHeatmapData,
    BubbleChild, BubbleItem, DashboardBubbleData,
)

__all__ = [
    "PaginatedResponse", "ImportResult",
    "MaterialBase", "MaterialCreate", "MaterialUpdate", "MaterialOut",
    "StandardizeRequest", "StandardizeResult",
    "ExtendedAttrField", "ExtendedAttrSchema",
    "SupplierBase", "SupplierCreate", "SupplierUpdate", "SupplierOut",
    "ProjectBase", "ProjectCreate", "ProjectUpdate", "ProjectOut",
    "QuoteBase", "QuoteCreate", "QuoteUpdate", "QuoteOut",
    "PriceCompareRequest", "PriceCompareResult",
    "SupplierScoreRequest", "SupplierScoreResult",
    "CategoryStats", "DashboardSummary",
    "MultiCompareRequest", "SupplierCompareItem", "MultiCompareResult",
    "SubCategoryStat", "CategoryDetailStats",
    "BidMatrixRequest", "BidMatrixResult",
    "BidInsightRequest", "BidInsightResult",
    "MatrixRow", "MatrixTotal", "SupplierCell", "SupplierLabel",
    "HistoricalAvg", "ReasonableLowInfo",
    "BrandTierCreate", "BrandTierUpdate", "BrandTierOut",
    "ConfigUpdate", "ConfigOut",
    "TreeChild", "TreeNode", "DashboardHeatmapData",
    "BubbleChild", "BubbleItem", "DashboardBubbleData",
]

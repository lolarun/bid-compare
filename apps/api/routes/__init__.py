"""Route aggregation — import all routers for inclusion in the app."""

from apps.api.routes.materials import router as materials_router
from apps.api.routes.suppliers import router as suppliers_router
from apps.api.routes.projects import router as projects_router
from apps.api.routes.quotes import router as quotes_router
from apps.api.routes.analysis import router as analysis_router
from apps.api.routes.config import router as config_router
from apps.api.routes.brand_tiers import router as brand_tiers_router
from apps.api.routes.auth import router as auth_router
from apps.api.routes.intake import router as intake_router
from apps.api.routes.invite import router as invite_router
from apps.api.routes.export import router as export_router

all_routers = [
    auth_router,
    materials_router,
    suppliers_router,
    projects_router,
    quotes_router,
    analysis_router,
    config_router,
    brand_tiers_router,
    intake_router,
    invite_router,
    export_router,
]

__all__ = [
    "materials_router", "suppliers_router", "projects_router",
    "quotes_router", "analysis_router", "config_router",
    "brand_tiers_router", "auth_router", "intake_router", "invite_router",
    "export_router", "all_routers",
]

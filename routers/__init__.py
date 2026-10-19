"""
Routers package — Adelaide (FacelessForge) deployment wiring.
Free Models Router -> OpenRouter fallback using vault var_IG_009 key.
"""
from .free_models_router import free_router

__all__ = ["free_router"]

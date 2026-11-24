from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.adapters.integrations import parse_store_ids
from app.deps import AuthContext, require_auth

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def build_status(settings) -> dict:
    def entry(enabled: bool, configured: bool, scope: str) -> dict:
        return {
            "enabled": enabled,
            "configured": configured,
            "active": enabled and configured,
            "scope": scope,
        }

    amazon_configured = all(
        [
            settings.amazon_lwa_client_id,
            settings.amazon_lwa_client_secret,
            settings.amazon_refresh_token,
            settings.aws_access_key_id,
            settings.aws_secret_access_key,
            settings.amazon_marketplace_ids,
        ]
    )
    return {
        "enabled_store_ids": sorted(parse_store_ids(settings.integration_enabled_store_ids)),
        "per_store_enablement": True,
        "integrations": {
            "amazon_sp_api": entry(
                settings.amazon_sp_api_enabled, amazon_configured, "store"
            ),
            "channel": entry(
                settings.channel_enabled,
                bool(settings.channel_base_url and settings.channel_api_key),
                "store",
            ),
            "track17": entry(
                settings.track17_enabled, bool(settings.track17_api_key), "store"
            ),
            "erp": entry(
                settings.erp_enabled,
                bool(settings.erp_base_url and settings.erp_api_key),
                "store",
            ),
            "translation": entry(
                settings.translation_enabled,
                bool(settings.translation_base_url),
                "store",
            ),
            "milvus": entry(
                settings.milvus_enabled, bool(settings.milvus_uri), "tenant"
            ),
        },
        "local_stand_ins": {
            "llm": settings.llm_provider,
            "translation": settings.translation_provider,
            "embedding": settings.embedding_provider,
            "reranker": settings.reranker_provider,
            "ocr": settings.ocr_provider,
            "vector_index": (
                "fake" if not (settings.milvus_enabled and settings.milvus_uri) else "milvus"
            ),
        },
        "note": (
            "Real integrations stay disabled unless the store id is listed in "
            "INTEGRATION_ENABLED_STORE_IDS and credentials come from the environment."
        ),
    }


@router.get("/status")
async def integration_status(
    request: Request, auth: AuthContext = Depends(require_auth)
):
    return build_status(request.app.state.settings)
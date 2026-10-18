from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import PROJECT_ROOT, Settings
from app.esi import EsiClient
from app.lp_storage import LpRepository
from app.lp_sync import LpSyncService
from app.security import TokenCipher
from app.sso import SsoClient
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    Database,
    JournalRepository,
)
from app.sync import SyncService
from app.web import AppServices, Clock, RuntimeContext, create_app


def build_app(
    settings: Settings | None = None,
    runtime: RuntimeContext | None = None,
) -> FastAPI:
    settings = settings or Settings()
    clock = Clock()
    database = Database(
        settings.database_path or PROJECT_ROOT / "data/eve_income.sqlite3"
    )
    characters = CharacterRepository(database)
    groups = CharacterGroupRepository(database)
    journal = JournalRepository(database)
    services = AppServices(characters, groups, journal, None, None, None)
    services.lp = LpRepository(database)
    http = None
    if not settings.configuration_issues():
        http = httpx.AsyncClient()
        cipher = TokenCipher(settings.app_encryption_key)
        sso = SsoClient(settings, http, clock.now_utc)
        services.cipher = cipher
        services.sso = sso
        esi = EsiClient(settings, http)
        services.sync = SyncService(
            characters,
            journal,
            cipher,
            sso,
            esi,
            clock.now_utc,
            lp=LpSyncService(services.lp, esi, clock.now_utc),
        )
    application = create_app(settings, services, clock, runtime)
    storage_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with storage_lifespan(app):
                yield
        finally:
            if http is not None:
                await http.aclose()

    application.router.lifespan_context = lifespan
    return application

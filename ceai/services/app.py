from __future__ import annotations

from dataclasses import dataclass

from ceai.config import Settings
from ceai.database import Database
from ceai.providers.router import AIProviderRouter
from ceai.services.catalog import CatalogService
from ceai.services.admin import AdminService
from ceai.services.generations import GenerationService
from ceai.services.payments import PaymentService
from ceai.services.subscriptions import SubscriptionService
from ceai.services.users import UserService


@dataclass(frozen=True)
class AppServices:
    settings: Settings
    users: UserService
    admin: AdminService
    catalog: CatalogService
    subscriptions: SubscriptionService
    payments: PaymentService
    generations: GenerationService


def build_services(db: Database, settings: Settings) -> AppServices:
    provider = AIProviderRouter(settings, db)
    return AppServices(
        settings=settings,
        users=UserService(db),
        admin=AdminService(db, settings),
        catalog=CatalogService(db),
        subscriptions=SubscriptionService(db),
        payments=PaymentService(
            db, mock_payment_base_url=settings.mock_payment_base_url
        ),
        generations=GenerationService(db, provider),
    )

from __future__ import annotations

from dataclasses import dataclass

from ceai.config import Settings
from ceai.database import Database
from ceai.providers.mock import MockAIProvider
from ceai.services.catalog import CatalogService
from ceai.services.generations import GenerationService
from ceai.services.payments import PaymentService
from ceai.services.subscriptions import SubscriptionService
from ceai.services.users import UserService


@dataclass(frozen=True)
class AppServices:
    users: UserService
    catalog: CatalogService
    subscriptions: SubscriptionService
    payments: PaymentService
    generations: GenerationService


def build_services(db: Database, settings: Settings) -> AppServices:
    provider = MockAIProvider()
    return AppServices(
        users=UserService(db),
        catalog=CatalogService(db),
        subscriptions=SubscriptionService(db),
        payments=PaymentService(
            db, mock_payment_base_url=settings.mock_payment_base_url
        ),
        generations=GenerationService(db, provider),
    )

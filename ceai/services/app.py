from __future__ import annotations

from dataclasses import dataclass

from ceai.config import Settings
from ceai.database import Database
from ceai.providers.router import AIProviderRouter
from ceai.services.catalog import CatalogService
from ceai.services.admin import AdminService
from ceai.services.generations import GenerationService
from ceai.services.payments import PaymentService
from ceai.services.referrals import ReferralService
from ceai.services.subscriptions import SubscriptionService
from ceai.services.text_chats import TextChatService
from ceai.services.users import UserService


@dataclass(frozen=True)
class AppServices:
    settings: Settings
    users: UserService
    admin: AdminService
    catalog: CatalogService
    subscriptions: SubscriptionService
    referrals: ReferralService
    payments: PaymentService
    generations: GenerationService
    text_chats: TextChatService


def build_services(db: Database, settings: Settings) -> AppServices:
    provider = AIProviderRouter(settings, db)
    referrals = ReferralService(db)
    return AppServices(
        settings=settings,
        users=UserService(db),
        admin=AdminService(db, settings),
        catalog=CatalogService(db),
        subscriptions=SubscriptionService(db),
        referrals=referrals,
        payments=PaymentService(
            db,
            mock_payment_base_url=settings.mock_payment_base_url,
            referrals=referrals,
        ),
        generations=GenerationService(db, provider),
        text_chats=TextChatService(db),
    )

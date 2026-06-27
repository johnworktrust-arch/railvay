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
            payment_provider=settings.payment_provider,
            app_base_url=settings.app_base_url,
            yookassa_shop_id=settings.yookassa_shop_id,
            yookassa_secret_key=settings.yookassa_secret_key,
            yookassa_api_base_url=settings.yookassa_api_base_url,
            yookassa_return_path=settings.yookassa_return_path,
            yookassa_request_timeout_seconds=(
                settings.yookassa_request_timeout_seconds
            ),
            crypto_pay_token=settings.crypto_pay_token,
            crypto_pay_api_base_url=settings.crypto_pay_api_base_url,
            crypto_pay_webhook_secret=settings.crypto_pay_webhook_secret,
            crypto_pay_accepted_assets=settings.crypto_pay_accepted_assets,
            crypto_pay_request_timeout_seconds=(
                settings.crypto_pay_request_timeout_seconds
            ),
            telegram_stars_rub_per_star=settings.telegram_stars_rub_per_star,
            referrals=referrals,
        ),
        generations=GenerationService(db, provider),
        text_chats=TextChatService(db),
    )

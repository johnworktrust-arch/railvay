from __future__ import annotations

from dataclasses import dataclass

from ceaai.config import Settings
from ceaai.database import Database
from ceaai.providers.router import AIProviderRouter
from ceaai.services.catalog import CatalogService
from ceaai.services.admin import AdminService
from ceaai.services.generations import GenerationService
from ceaai.services.payments import PaymentService
from ceaai.services.referrals import ReferralService
from ceaai.services.subscriptions import SubscriptionService
from ceaai.services.text_chats import TextChatService
from ceaai.services.users import UserService


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


def build_services(
    db: Database, settings: Settings, vpn_db: Database | None = None
) -> AppServices:
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
            platega_merchant_id=settings.platega_merchant_id,
            platega_secret=settings.platega_secret,
            platega_api_base_url=settings.platega_api_base_url,
            platega_return_path=settings.platega_return_path,
            platega_failed_path=settings.platega_failed_path,
            platega_request_timeout_seconds=(
                settings.platega_request_timeout_seconds
            ),
            crypto_pay_token=settings.crypto_pay_token,
            crypto_pay_api_base_url=settings.crypto_pay_api_base_url,
            crypto_pay_webhook_secret=settings.crypto_pay_webhook_secret,
            crypto_pay_accepted_assets=settings.crypto_pay_accepted_assets,
            crypto_pay_request_timeout_seconds=(
                settings.crypto_pay_request_timeout_seconds
            ),
            telegram_stars_amount=settings.telegram_stars_amount,
            referrals=referrals,
        ),
        generations=GenerationService(db, provider),
        text_chats=TextChatService(db),
    )

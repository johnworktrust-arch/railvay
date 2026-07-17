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
from ceai.services.vpn import VpnService


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
    vpn: VpnService


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
            telegram_stars_amount=settings.telegram_stars_amount,
            referrals=referrals,
        ),
        generations=GenerationService(db, provider),
        text_chats=TextChatService(db),
        vpn=VpnService(
            db,
            server_code=settings.vpn_server_code,
            trial_days=settings.vpn_trial_days,
            allow_admin_demo_payment=settings.vpn_allow_admin_demo_payment,
            payment_provider=settings.vpn_payment_provider,
            app_base_url=settings.app_base_url,
            platega_merchant_id=settings.vpn_platega_merchant_id,
            platega_secret=settings.vpn_platega_secret,
            platega_api_base_url=settings.vpn_platega_api_base_url,
            platega_return_path=settings.vpn_platega_return_path,
            platega_failed_path=settings.vpn_platega_failed_path,
            platega_request_timeout_seconds=(
                settings.vpn_platega_request_timeout_seconds
            ),
            worker_health_max_age_seconds=(
                settings.vpn_worker_health_max_age_seconds
            ),
            referrals=referrals,
        ),
    )

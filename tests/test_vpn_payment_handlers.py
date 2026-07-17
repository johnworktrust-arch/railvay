from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ceai.vpn_bot.handlers import create_vpn_router


def _handler(router, name: str):
    return next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == name
    )


def _callback(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=9101,
            username="vpn_user",
            first_name="VPN",
            last_name="User",
            language_code="ru",
        ),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def _services(*, vpn, user_id: int = 42):
    return SimpleNamespace(
        vpn=vpn,
        users=SimpleNamespace(
            ensure_telegram_user=Mock(return_value={"id": user_id})
        ),
        settings=SimpleNamespace(
            vpn_allow_admin_demo_payment=False,
            vpn_admin_demo_telegram_ids=(),
            vpn_support_username="cea_support",
            vpn_subscription_base_url="https://vpn.example.test",
        ),
    )


class VpnPaymentHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_payment_callbacks_refresh_selection_without_order(self) -> None:
        create_payment = Mock(side_effect=AssertionError("must not create an order"))
        services = _services(
            vpn=SimpleNamespace(
                uses_platega=True,
                create_platega_payment=create_payment,
            )
        )
        payment = _handler(create_vpn_router(services), "payment")

        for method in ("sbp", "card", "crypto", "stars", "other"):
            with self.subTest(method=method):
                callback = _callback(f"vpn:payment:1:{method}")

                await payment(callback)

                create_payment.assert_not_called()
                callback.message.edit_text.assert_awaited_once()
                text = callback.message.edit_text.await_args.args[0]
                keyboard = callback.message.edit_text.await_args.kwargs[
                    "reply_markup"
                ]
                self.assertIn("Способы оплаты обновились", text)
                self.assertEqual(
                    keyboard.inline_keyboard[0][0].callback_data,
                    "vpn:payment:1:platega",
                )
                callback.answer.assert_awaited_once_with(
                    "Выберите новый способ оплаты."
                )

    async def test_platega_order_creation_runs_in_worker_thread(self) -> None:
        create_payment = Mock()
        order = {
            "id": 17,
            "amount_rub": 149,
            "payment_url": "https://pay.platega.io/order-17",
        }
        services = _services(
            vpn=SimpleNamespace(
                uses_platega=True,
                create_platega_payment=create_payment,
            )
        )
        payment = _handler(create_vpn_router(services), "payment")
        callback = _callback("vpn:payment:1:platega")

        to_thread = AsyncMock(return_value=(order, True))
        with patch("ceai.vpn_bot.handlers.asyncio.to_thread", to_thread):
            await payment(callback)

        create_payment.assert_not_called()
        to_thread.assert_awaited_once_with(
            create_payment,
            user_id=42,
            plan_code="vpn-1m",
            user_name="vpn_user",
        )
        self.assertEqual(
            callback.message.edit_text.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .url,
            "https://pay.platega.io/order-17",
        )

    async def test_platega_status_check_runs_in_worker_thread(self) -> None:
        check_payment = Mock()
        services = _services(
            vpn=SimpleNamespace(
                get_payment_for_user=Mock(
                    return_value={"id": 17, "provider": "platega"}
                ),
                check_platega_payment=check_payment,
            )
        )
        check = _handler(create_vpn_router(services), "check_payment")
        callback = _callback("vpn:check:17")
        pending = SimpleNamespace(
            status="pending",
            confirmed=False,
            processed=False,
            subscription=None,
        )

        to_thread = AsyncMock(return_value=pending)
        with patch("ceai.vpn_bot.handlers.asyncio.to_thread", to_thread):
            await check(callback)

        check_payment.assert_not_called()
        to_thread.assert_awaited_once_with(
            check_payment,
            user_id=42,
            payment_id=17,
        )
        callback.answer.assert_awaited_once()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("Оплата ещё не подтверждена", callback.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

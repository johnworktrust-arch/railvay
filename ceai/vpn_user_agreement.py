from __future__ import annotations

import re
from html import escape

from ceai.config import Settings


REQUIRED_PUBLICATION_FIELDS = (
    "vpn_legal_provider_name",
    "vpn_legal_provider_status",
    "vpn_legal_inn",
    "vpn_legal_address",
    "vpn_legal_email",
    "vpn_privacy_policy_url",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IP_STATUS_RE = re.compile(r"(?:^|\W)ип(?:$|\W)", re.IGNORECASE)


def vpn_agreement_is_publishable(settings: Settings | None) -> bool:
    """Return whether the document has the minimum public seller details."""
    if settings is None:
        return False
    if not all(
        str(getattr(settings, field, "") or "").strip()
        for field in REQUIRED_PUBLICATION_FIELDS
    ):
        return False

    inn = settings.vpn_legal_inn.strip()
    if not _valid_inn(inn):
        return False
    if not EMAIL_RE.fullmatch(settings.vpn_legal_email.strip()):
        return False

    status = settings.vpn_legal_provider_status.strip().casefold()
    registration_required = (
        "индивидуальн" in status
        or "общество" in status
        or "юридическ" in status
        or "ооо" in status
        or IP_STATUS_RE.search(status) is not None
    )
    registration_number = settings.vpn_legal_registration_number.strip()
    if registration_required and not _valid_registration_number(registration_number):
        return False
    if registration_number and not _valid_registration_number(registration_number):
        return False
    return True


def _valid_inn(value: str) -> bool:
    if not value.isdigit() or len(value) not in {10, 12}:
        return False
    digits = [int(char) for char in value]
    if len(digits) == 10:
        checksum = sum(
            weight * digit
            for weight, digit in zip((2, 4, 10, 3, 5, 9, 4, 6, 8), digits)
        )
        return checksum % 11 % 10 == digits[9]
    first_checksum = sum(
        weight * digit
        for weight, digit in zip((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), digits)
    )
    second_checksum = sum(
        weight * digit
        for weight, digit in zip((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), digits)
    )
    return (
        first_checksum % 11 % 10 == digits[10]
        and second_checksum % 11 % 10 == digits[11]
    )


def _valid_registration_number(value: str) -> bool:
    return value.isdigit() and len(value) in {13, 15}


def _calendar_day_phrase(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if remainder_10 == 1 and remainder_100 != 11:
        suffix = "календарный день"
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        suffix = "календарных дня"
    else:
        suffix = "календарных дней"
    return f"{value} {suffix}"


def _setting(settings: Settings | None, name: str, default: str = "") -> str:
    if settings is None:
        return default
    value = str(getattr(settings, name, "") or "").strip()
    return value or default


def render_vpn_user_agreement_html(settings: Settings | None) -> str:
    """Render the standalone CEA VPN agreement bundled with the application."""
    is_publishable = vpn_agreement_is_publishable(settings)
    support_username = _setting(settings, "vpn_support_username", "cea_help").lstrip("@")
    bot_username = _setting(settings, "vpn_bot_username", "ceavpn_bot").lstrip("@")
    privacy_url = _setting(settings, "vpn_privacy_policy_url")
    trial_days = max(1, int(getattr(settings, "vpn_trial_days", 3) or 3))
    trial_duration = _calendar_day_phrase(trial_days)
    version = _setting(settings, "vpn_agreement_version", "1.0")
    effective_date = _setting(
        settings, "vpn_agreement_effective_date", "1 августа 2026 года"
    )

    provider_name = _setting(settings, "vpn_legal_provider_name")
    provider_status = _setting(settings, "vpn_legal_provider_status")
    inn = _setting(settings, "vpn_legal_inn")
    registration_number = _setting(settings, "vpn_legal_registration_number")
    address = _setting(settings, "vpn_legal_address")
    email = _setting(settings, "vpn_legal_email")
    support_hours = _setting(settings, "vpn_legal_support_hours")

    safe_support_username = escape(support_username)
    safe_support_url = escape(f"https://t.me/{support_username}", quote=True)
    safe_bot_username = escape(bot_username)
    safe_bot_url = escape(f"https://t.me/{bot_username}", quote=True)
    safe_privacy_url = escape(privacy_url, quote=True)

    if is_publishable:
        heading = "Публичная оферта CEA VPN"
        eyebrow = "Юридический документ"
        status_markup = (
            '<div class="notice notice-ok"><strong>Действующая редакция.</strong> '
            "Оплата тарифа или активация пробного периода означает принятие "
            "условий этого документа.</div>"
        )
        provider_rows = [
            f"<dt>Исполнитель</dt><dd>{escape(provider_name)}</dd>",
            f"<dt>Статус</dt><dd>{escape(provider_status)}</dd>",
            f"<dt>ИНН</dt><dd>{escape(inn)}</dd>",
        ]
        if registration_number:
            provider_rows.append(
                f"<dt>ОГРН / ОГРНИП</dt><dd>{escape(registration_number)}</dd>"
            )
        provider_rows.extend(
            [
                f"<dt>Адрес</dt><dd>{escape(address)}</dd>",
                (
                    '<dt>Электронная почта</dt><dd><a href="mailto:'
                    f'{escape(email, quote=True)}">{escape(email)}</a></dd>'
                ),
            ]
        )
        if support_hours:
            provider_rows.append(
                f"<dt>Режим поддержки</dt><dd>{escape(support_hours)}</dd>"
            )
        provider_markup = "".join(provider_rows)
    else:
        heading = "Проект соглашения CEA VPN"
        eyebrow = "Документ для тестового режима"
        status_markup = (
            '<div class="notice notice-draft"><strong>Платная версия ещё не '
            "опубликована.</strong> В настройках не заполнены обязательные "
            "реквизиты исполнителя. До их публикации этот текст не является "
            "полной публичной офертой на платные услуги.</div>"
        )
        provider_markup = (
            "<dt>Исполнитель</dt><dd>Будет указан до запуска приёма платежей</dd>"
            f"<dt>Поддержка</dt><dd><a href=\"{safe_support_url}\">"
            f"@{safe_support_username}</a></dd>"
        )

    if privacy_url:
        privacy_paragraph = (
            "Подробные условия определены в "
            f'<a href="{safe_privacy_url}">Политике конфиденциальности</a>.'
        )
    else:
        privacy_paragraph = (
            "До запуска приёма оплаты Исполнитель обязан опубликовать отдельную "
            "Политику конфиденциальности CEA VPN и добавить ссылку на неё в Сервис."
        )

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{escape(heading)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #080d17;
      --panel: #111a2a;
      --panel-soft: #172237;
      --text: #f6f8fc;
      --muted: #aeb9cc;
      --line: #26344d;
      --accent: #7357ff;
      --accent-2: #25b887;
      --warning: #f4bd50;
      --max: 860px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 15% -10%, rgba(115, 87, 255, .28), transparent 33rem),
        radial-gradient(circle at 100% 20%, rgba(37, 184, 135, .14), transparent 28rem),
        var(--bg);
      color: var(--text);
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
      line-height: 1.65;
    }}
    a {{ color: #a997ff; text-underline-offset: 3px; }}
    .wrap {{ width: min(calc(100% - 32px), var(--max)); margin: 0 auto; }}
    header {{ padding: 56px 0 26px; }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 7px 12px;
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 999px;
      background: rgba(17,26,42,.78);
      color: #dfe5f2;
      font-size: 14px;
      font-weight: 700;
    }}
    .brand-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--accent-2); }}
    .eyebrow {{ margin: 28px 0 10px; color: #a997ff; font-size: 13px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }}
    h1 {{ margin: 0; max-width: 720px; font-size: clamp(34px, 7vw, 58px); line-height: 1.03; letter-spacing: -.04em; }}
    .meta {{ margin-top: 18px; color: var(--muted); }}
    .notice {{ margin: 26px 0 0; padding: 16px 18px; border-radius: 16px; border: 1px solid; }}
    .notice-ok {{ background: rgba(37,184,135,.09); border-color: rgba(37,184,135,.38); }}
    .notice-draft {{ background: rgba(244,189,80,.09); border-color: rgba(244,189,80,.45); }}
    nav {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin: 12px 0 28px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(17,26,42,.86);
    }}
    nav a {{ padding: 8px 10px; border-radius: 10px; color: #dfe5f2; text-decoration: none; text-align: center; font-size: 14px; }}
    nav a:hover {{ background: var(--panel-soft); }}
    article {{ padding-bottom: 28px; }}
    section {{ margin-bottom: 16px; padding: 24px; border: 1px solid var(--line); border-radius: 20px; background: rgba(17,26,42,.9); box-shadow: 0 18px 50px rgba(0,0,0,.14); }}
    h2 {{ margin: 0 0 14px; font-size: 22px; line-height: 1.25; letter-spacing: -.015em; }}
    p {{ margin: 10px 0; }}
    ol, ul {{ margin: 10px 0; padding-left: 24px; }}
    li + li {{ margin-top: 8px; }}
    strong {{ color: #fff; }}
    dl {{ display: grid; grid-template-columns: minmax(145px, .45fr) 1fr; gap: 10px 18px; margin: 0; }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    .callout {{ margin-top: 14px; padding: 14px 16px; border-left: 3px solid var(--accent); border-radius: 0 12px 12px 0; background: rgba(115,87,255,.09); }}
    footer {{ padding: 8px 0 54px; color: var(--muted); font-size: 14px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }}
    .button {{ display: inline-flex; padding: 11px 15px; border-radius: 12px; background: var(--accent); color: white; font-weight: 750; text-decoration: none; }}
    .button.secondary {{ background: var(--panel-soft); border: 1px solid var(--line); }}
    @media (max-width: 640px) {{
      header {{ padding-top: 32px; }}
      nav {{ grid-template-columns: 1fr; }}
      nav a {{ text-align: left; }}
      section {{ padding: 20px 18px; border-radius: 16px; }}
      dl {{ grid-template-columns: 1fr; gap: 2px; }}
      dd + dt {{ margin-top: 10px; }}
    }}
    @media print {{
      body {{ background: white; color: #111; }}
      .brand, nav, .actions {{ display: none; }}
      section {{ box-shadow: none; break-inside: avoid; background: white; border-color: #ddd; }}
      a {{ color: #111; }}
      strong {{ color: #111; }}
    }}
  </style>
</head>
<body>
  <header class="wrap">
    <div class="brand"><span class="brand-dot"></span> CEA VPN</div>
    <div class="eyebrow">{escape(eyebrow)}</div>
    <h1>{escape(heading)}</h1>
    <p class="meta">Редакция {escape(version)} · действует с {escape(effective_date)}</p>
    {status_markup}
  </header>

  <main class="wrap">
    <nav aria-label="Навигация по документу">
      <a href="#service">Услуга и тарифы</a>
      <a href="#refunds">Возврат и отмена</a>
      <a href="#contacts">Реквизиты и поддержка</a>
    </nav>

    <article>
      <section id="general">
        <h2>1. Общие положения</h2>
        <p>Настоящий документ регулирует использование Telegram-бота <a href="{safe_bot_url}">@{safe_bot_username}</a> и предоставляемого через него сервиса CEA VPN (далее — <strong>Сервис</strong>).</p>
        <p><strong>Пользователь</strong> — дееспособное физическое лицо, которое использует Сервис, активирует пробный период или приобретает подписку. <strong>Исполнитель</strong> — лицо, реквизиты которого приведены в разделе 13.</p>
        <p>Сервис предназначен для создания защищённого сетевого соединения через доступную инфраструктуру Исполнителя. Пользователь обязан применять Сервис только законным способом.</p>
      </section>

      <section id="acceptance">
        <h2>2. Принятие условий и заключение договора</h2>
        <p>Активация пробного периода подтверждает принятие правил бесплатного использования. Оплата выбранного тарифа после ознакомления с его ценой, сроком и настоящим документом является акцептом оферты и заключением договора на соответствующий оплаченный период.</p>
        <p>До акцепта Пользователь обязан проверить сведения о тарифе и ограничениях. Если Пользователь не согласен с условиями, он не должен активировать пробный период или оплачивать тариф.</p>
        <p>Использование Telegram-аккаунта и подтверждённого платежа позволяет сторонам идентифицировать действия Пользователя в электронной форме. Пользователь отвечает за безопасность своего аккаунта Telegram.</p>
      </section>

      <section id="service">
        <h2>3. Услуга и подписка</h2>
        <ul>
          <li>Подписка предоставляет индивидуальный доступ к VPN-инфраструктуре на срок выбранного тарифа.</li>
          <li>Одна подписка предназначена для <strong>одного устройства</strong>. Передача ссылки подписки, ключа или профиля другим лицам запрещена.</li>
          <li>Срок начинается после подтверждения платежа и активации подписки в Сервисе, даже если Пользователь установил приложение позднее.</li>
          <li>Доступные серверы, технические протоколы и расположение узлов могут меняться для безопасности и работоспособности без уменьшения уже оплаченного срока.</li>
          <li>Для подключения может потребоваться стороннее приложение. Его установка и использование регулируются также правилами разработчика приложения.</li>
        </ul>
        <div class="callout"><strong>Важно:</strong> Сервис не обещает абсолютную анонимность, фиксированную скорость, доступность каждого сайта или работу в любой сети и на любом устройстве.</div>
      </section>

      <section id="trial">
        <h2>4. Пробный период</h2>
        <p>Если кнопка пробного периода доступна в боте, Пользователь может один раз активировать бесплатный доступ на {trial_duration}. Для активации Сервис вправе проверить подписку на указанный информационный Telegram-канал.</p>
        <p>Пробный период не требует платёжных реквизитов, не создаёт платную подписку и <strong>не продлевается автоматически</strong>. Повторная активация через другой аккаунт или иным способом может быть отклонена как злоупотребление.</p>
      </section>

      <section id="payment">
        <h2>5. Тарифы и оплата</h2>
        <p>Актуальные цена, срок, валюта, способ оплаты и итоговая сумма показываются в Telegram-боте до платежа и становятся частью договора. Исполнитель вправе менять условия только для будущих покупок и продлений.</p>
        <p>Оплата может приниматься через Telegram Stars и/или платёжного партнёра Platega, если соответствующий способ доступен в боте. Platega и Telegram обрабатывают платёж по собственным правилам; услугу CEA VPN оказывает Исполнитель.</p>
        <p>Подписка активируется только после серверного подтверждения успешной оплаты. Если деньги списаны, но доступ не выдан, Пользователь должен обратиться в поддержку и приложить идентификатор или подтверждение платежа.</p>
        <p><strong>Автоматическое продление и рекуррентные списания не применяются.</strong> После окончания срока новый тариф оплачивается Пользователем отдельно. Если автопродление появится в будущем, оно может быть включено только после отдельного явного согласия Пользователя.</p>
      </section>

      <section id="use">
        <h2>6. Правила использования</h2>
        <p>Пользователь обязан соблюдать применимое законодательство, правила Telegram, платёжных систем и стороннего VPN-приложения.</p>
        <p>Запрещены: передача доступа третьим лицам; использование более чем на одном устройстве; спам, мошенничество, атаки и вредоносный трафик; несанкционированный доступ; нарушение авторских и иных прав; распространение запрещённой информации; использование Сервиса для доступа к ресурсам, доступ к которым ограничен законодательством Российской Федерации; вмешательство в инфраструктуру и обход технических лимитов.</p>
        <p>При объективных признаках нарушения, угрозе безопасности или законном требовании уполномоченного органа Исполнитель вправе временно ограничить доступ на время проверки. Если нарушение подтверждено, доступ может быть прекращён с учётом обязательных прав Пользователя по закону.</p>
      </section>

      <section id="availability">
        <h2>7. Доступность и технические ограничения</h2>
        <p>Исполнитель принимает разумные меры для стабильной работы, однако результат зависит от оператора связи, маршрутизации, устройства, настроек, стороннего приложения, Telegram, хостинга и иных внешних систем.</p>
        <p>Возможны плановые работы, аварии и временная недоступность отдельных узлов. Исполнитель устраняет контролируемые неполадки в разумный срок и вправе предоставить соразмерное продление, замену доступа или возврат в предусмотренных законом случаях.</p>
      </section>

      <section id="refunds">
        <h2>8. Отказ, возврат и отмена</h2>
        <p>Пользователь вправе отказаться от договора в порядке, предусмотренном законодательством Российской Федерации. При возврате могут быть удержаны стоимость фактически оказанной части услуги и документально подтверждённые расходы, непосредственно связанные с исполнением договора.</p>
        <p>Заявление направляется в поддержку <a href="{safe_support_url}">@{safe_support_username}</a>. Нужно указать Telegram ID, дату, сумму, способ оплаты, идентификатор платежа и причину обращения. Ответ предоставляется в течение 10 календарных дней, если законом не предусмотрен более короткий срок.</p>
        <p>Возврат выполняется с учётом правил платёжного партнёра и применимого законодательства. Условия этого раздела не ограничивают обязательные права потребителя.</p>
      </section>

      <section id="rights">
        <h2>9. Права и обязанности Исполнителя</h2>
        <p>Исполнитель обязан предоставить оплаченный доступ, поддерживать разумный уровень безопасности, принимать обращения и не ухудшать задним числом срок и основные условия уже оплаченной подписки.</p>
        <p>Исполнитель вправе обновлять инфраструктуру и интерфейс, заменять технически сопоставимые узлы, предотвращать злоупотребления и приостанавливать работу для обслуживания или исполнения требований закона.</p>
      </section>

      <section id="liability">
        <h2>10. Ответственность</h2>
        <p>Стороны отвечают в пределах, установленных применимым законодательством. Никакое условие документа не исключает обязательную ответственность Исполнителя и законные права потребителя.</p>
        <p>Исполнитель не отвечает за действия Пользователя, неисправность его устройства, ошибки стороннего приложения и недоступность внешних сетей или сервисов, которые Исполнитель объективно не контролирует. Это не освобождает Исполнителя от ответственности за собственное ненадлежащее оказание услуги.</p>
      </section>

      <section id="privacy">
        <h2>11. Персональные данные</h2>
        <p>Для работы Сервиса могут обрабатываться Telegram ID, имя и username, сведения о подписке, заказе, сумме и статусе платежа, обращения в поддержку, а также технические данные, необходимые для подключения, защиты инфраструктуры и диагностики.</p>
        <p>Необходимые сведения могут передаваться Telegram, Platega, поставщикам хостинга и серверной инфраструктуры в объёме, требуемом для оказания услуги, оплаты, безопасности и исполнения закона. {privacy_paragraph}</p>
      </section>

      <section id="changes">
        <h2>12. Срок действия и изменения</h2>
        <p>Документ действует с указанной вверху даты. Новая редакция применяется к будущим активациям, покупкам и продлениям. Условия уже оплаченного периода не ухудшаются задним числом, кроме случаев, прямо предусмотренных законом.</p>
        <p>Существенные изменения публикуются на этой странице и могут дополнительно сообщаться через Telegram-бота.</p>
      </section>

      <section id="contacts">
        <h2>13. Исполнитель, обращения и споры</h2>
        <dl>{provider_markup}</dl>
        <p>Поддержка CEA VPN: <a href="{safe_support_url}">@{safe_support_username}</a>.</p>
        <p>К отношениям сторон применяется законодательство Российской Федерации. Стороны стремятся урегулировать разногласия через поддержку; право Пользователя обратиться в суд по правилам защиты прав потребителей не ограничивается.</p>
        <div class="actions">
          <a class="button" href="{safe_support_url}">Написать в поддержку</a>
          <a class="button secondary" href="{safe_bot_url}">Открыть CEA VPN</a>
        </div>
      </section>
    </article>
  </main>

  <footer class="wrap">
    CEA VPN · Редакция {escape(version)} · {escape(effective_date)}
  </footer>
</body>
</html>
"""

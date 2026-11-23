---
hide:
  - toc
---

# Гайды и инструкции

Выбери, что хочешь сделать. Основные инструкции собраны **на этом сайте**, поэтому для работы с сервером не нужно возвращаться к длинному README на GitHub.

## Первый запуск и безопасность

<div class="grid cards guide-cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Установка с нуля**

    Подготовка VPS и доменов, семь вопросов установщика, рекомендации и проверка результата.

    [Первый запуск](../start.md)

-   :material-shield-check:{ .lg .middle } **Расширенная безопасность VPS**

    Fail2ban, DNS-over-TLS, автообновления nginx и ядра, расписание и проверка настроек.

    [Безопасность сервера](security.md)

-   :material-key-variant:{ .lg .middle } **SSH-ключи для Ubuntu 24.04/26.04**

    Создай ключ на Windows 11, добавь его на VPS и проверь вход, сохранив доступ к серверу.

    [Настройка SSH-ключей](ssh-keys.md)

</div>

## Управление и восстановление

<div class="grid cards guide-cards" markdown>

-   :material-backup-restore:{ .lg .middle } **Полный Backup и перенос клиентов**

    Резервная копия управляемого стека, полное Restore у другого хостера и перенос клиентов в новую установку.

    [Backup / Restore и подписки](backup.md)

-   :material-web:{ .lg .middle } **Смена доменов**

    Переход на новые домены панели и REALITY без переустановки сервера и повторного создания клиентов.

    [Изменение доменов](domains.md)

-   :material-shield-lock:{ .lg .middle } **Личный DNS и AdGuard Home**

    Блокировка рекламы, доступ по DoH, ClientID, частный DNS на Android и настройка DoT.

    [AdGuard Home и DNS](dns.md)

</div>

## Подключения и маршруты

<div class="grid cards guide-cards" markdown>

-   :material-lan:{ .lg .middle } **Подключения и nginx**

    REALITY, XHTTP, Hysteria2, дополнительные протоколы, подписки и автоматические сертификаты.

    [Подключения и подписки](connections.md)

-   :material-source-branch:{ .lg .middle } **Каскад из двух VPS**

    Подключение через входной VPS к выходному серверу, проверка работы и отключение.

    [Пошаговая настройка каскада](cascade.md)

</div>

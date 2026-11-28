# 3X-UI AUTO NGINX — Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md
"""Plain, flushed output for the Update worker and systemd journal."""

import os
import subprocess
import traceback
from pathlib import Path


def text(en, ru):
    return ru if os.environ.get('UI_LANG') == 'ru' else en


def say(en, ru):
    print(text(en, ru), flush=True)


def title(en, ru):
    frame = '=' * 60
    print(f'\n{frame}\n  {text(en, ru)}\n{frame}\n', flush=True)


def checked(en, ru):
    say(f'  [✓] {en}', f'  [✓] {ru}')


def stage(number, en, ru):
    rule = '-' * 60
    print(f'\n  [{number}/5] {text(en, ru)}\n{rule}', flush=True)


def result(status):
    say(f'  Result: {status}', f'  Результат: {status}')


def final_verification():
    stage(5, 'FINAL VERIFICATION', 'ФИНАЛЬНАЯ ПРОВЕРКА')


def verified():
    checked('Final checks passed', 'Финальные проверки пройдены')


def success(archive):
    title('UPDATE COMPLETE', 'ОБНОВЛЕНИЕ ЗАВЕРШЕНО')
    checked('Managed project configuration is up to date', 'Управляемая конфигурация проекта актуальна')
    checked('3x-ui / Xray and Nginx checked', '3x-ui / Xray и Nginx проверены')
    checked('Clients and subscriptions preserved', 'Клиенты и подписки сохранены')
    checked('Backup created and verified', 'Резервная копия создана и проверена')
    say(f'\n  Backup:\n  {archive}', f'\n  Резервная копия:\n  {archive}')
    say('\n  You may need to refresh subscriptions or client profiles.',
        '\n  Может потребоваться обновить подписки или клиентские профили.')
    result('SUCCESS')


def up_to_date():
    title('UPDATE NOT REQUIRED', 'ОБНОВЛЕНИЕ НЕ ТРЕБУЕТСЯ')
    checked('The project is up to date', 'Установлена актуальная версия проекта')
    say('\n  Configuration is current. No changes were made.',
        '\n  Конфигурация актуальна. Изменения не внесены.')
    result('UP TO DATE')


def problem(kind, en, ru, **values):
    # Only these intentionally public messages may be displayed verbatim.
    en = en.format(**values)
    error = kind(en)
    error.update_message = (en, ru.format(**values))
    return error


def diagnostics(error):
    if hasattr(error, 'update_message'):
        return error.update_message
    frame = traceback.extract_tb(error.__traceback__)[-1]
    location = f'{Path(frame.filename).name}:{frame.lineno}'
    details = type(error).__name__
    if isinstance(error, OSError) and error.errno is not None:
        details += f', errno={error.errno}'
    if isinstance(error, subprocess.TimeoutExpired):
        details += f', timeout={error.timeout}s'
    message = f'{location} ({details})'
    return message, message


def failed(error):
    details = text(*diagnostics(error))
    say(f'  Update failed: {details}', f'  Не удалось выполнить обновление: {details}')
    result('FAILED')


def rollback_unverified(error, work, journal):
    details = text(*diagnostics(error))
    say(f'  Recovery could not be verified: {details}\n'
        f'  Recovery journal retained at {journal}. Inspect it and {work} before retrying.',
        f'  Восстановление не подтверждено: {details}\n'
        f'  Журнал восстановления сохранён в {journal}. Перед повтором проверьте его и {work}.')
    result('ROLLBACK UNVERIFIED')

"""Exercise the actual release workflow with a simulated Telegram response."""

import io
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest
from unittest.mock import patch


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/telegram-release.yml'
REPOSITORY = 'xPROMSx/3x-ui-auto-nginx'
RELEASE_URL = f'https://github.com/{REPOSITORY}/releases/tag/v2.8.0'
RELEASE_TITLE = '🚀 <b>3X-UI AUTO NGINX · v2.8.0</b>'
RELEASE_LINK = f'<a href="{RELEASE_URL}">📦 Описание и загрузка</a>'


class TelegramRelease(unittest.TestCase):
    def setUp(self):
        source = re.search(r"(?ms)^          python3 - <<'PY'\n(.*?)^          PY$",
                           WORKFLOW.read_text(encoding='utf-8'))
        self.assertIsNotNone(source, 'Python step missing from Telegram release workflow')
        self.script = compile(textwrap.dedent(source[1]), str(WORKFLOW), 'exec')

    def run_workflow(self, body=None, *, event_name='release', **release_fields):
        release = {'tag_name': 'v2.8.0', 'html_url': RELEASE_URL, 'body': body,
                   'draft': False, 'prerelease': False, **release_fields}
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            if event_name == 'release':
                event.write_text(json.dumps({'release': release}), encoding='utf-8')
            env = {'TELEGRAM_BOT_TOKEN': 'fixture-token', 'RELEASE_PROJECT': '3X-UI AUTO NGINX',
                   'RELEASE_EMOJI': '🚀', 'GITHUB_EVENT_NAME': event_name,
                   'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_EVENT_PATH': str(event)}
            with (patch.dict(os.environ, env, clear=True), patch('sys.stdout', new=io.StringIO()),
                  patch('urllib.request.urlopen', return_value=io.StringIO('{"ok": true}')) as send):
                try:
                    exec(self.script, {})
                except SystemExit:
                    send.assert_not_called()
                    raise
        send.assert_called_once()
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload['parse_mode'], 'HTML')
        return payload['text']

    def test_v280_announces_client_migration_without_field_list(self):
        # Excerpt from the published v2.8.0 release notes.
        summary = ('теперь можно перенести клиентов и подписки на новый или переустановленный VPS '
                   'из обычного Backup v3. Это также удобный способ перейти на актуальную версию '
                   '3X-UI AUTO NGINX без необходимости заново создавать клиентов и выдавать им '
                   'новые ссылки на подписки.')
        fields = ('Клиенты, их идентификаторы, UUID, пароли и другие учётные данные.',
                  'Состояние клиентов, сроки действия, лимиты и поддерживаемые счётчики трафика.',
                  'Прежние пути подписок RAW и JSON, включая Clash / Mihomo.')
        body = (f'# 3X-UI AUTO NGINX v2.8.0\n\n## 🇷🇺 Что нового\n\n'
                f'**Главное в релизе:** {summary}\n\n### 🔄 Восстановление клиентов и подписок\n\n'
                '**Что переносится:**\n\n' + ''.join(f'- {field}\n' for field in fields) +
                "\n## 🇬🇧 What's New\n\n**Highlights:** You can now restore clients and subscriptions.")

        message = self.run_workflow(body)

        for field in fields:
            self.assertNotIn(field, message)
        self.assertEqual(message, f'{RELEASE_TITLE}\n\n✨ <b>Главное в релизе</b>\n{summary}\n\n{RELEASE_LINK}')

    def test_markdown_renders_as_telegram_html(self):
        body = ('## 🇷🇺 Что нового\n\n'
                '**Главное в релизе:** **Backup & Restore** и *CLI* для `a < b`, '
                '[инструкция](https://example.com/?x=1&y="2"), ~~устарело~~. <tag>')
        message = self.run_workflow(body, tag_name='v2.8.0<&', html_url=RELEASE_URL + '?a=1&b="2"')

        self.assertIn('3X-UI AUTO NGINX · v2.8.0&lt;&amp;', message)
        self.assertIn('<b>Backup &amp; Restore</b> и <i>CLI</i> для <code>a &lt; b</code>, '
                      '<a href="https://example.com/?x=1&amp;y=&quot;2&quot;">инструкция</a>, '
                      '<s>устарело</s>. &lt;tag&gt;', message)
        self.assertIn(f'<a href="{RELEASE_URL}?a=1&amp;b=&quot;2&quot;">📦 Описание и загрузка</a>', message)

    def test_multiline_highlight_stops_at_next_block(self):
        for next_block in ('\nПодробное описание.', '### Подробности', '- Список', '* Список',
                           '+ Список', '1. Список', '1) Список', "## 🇬🇧 What's New", '---'):
            with self.subTest(next_block=next_block):
                body = ('## 🇷🇺 Что нового\r\n\r\n**Главное в релизе:** Первая строка.\r\n'
                        f'Вторая строка.\r\n{next_block}\r\nАнглийское описание.')
                self.assertEqual(self.run_workflow(body),
                                 f'{RELEASE_TITLE}\n\n✨ <b>Главное в релизе</b>\n'
                                 f'Первая строка.\nВторая строка.\n\n{RELEASE_LINK}')

    def test_missing_or_empty_russian_highlight_uses_neutral_message(self):
        for body in (None, '- Произвольный пункт',
                     '## 🇷🇺 Что нового\n\n- Произвольный пункт\n\n'
                     "## 🇬🇧 What's New\n\n**Главное в релизе:** Текст вне русского раздела.",
                     '## 🇷🇺 Что нового\n\n**Главное в релизе:**\n\n- Произвольный пункт'):
            with self.subTest(body=body):
                self.assertEqual(self.run_workflow(body),
                                 f'{RELEASE_TITLE}\n\nРелиз доступен на GitHub.\n\n{RELEASE_LINK}')

    def test_draft_and_prerelease_are_rejected(self):
        for flag in ('draft', 'prerelease'):
            with self.subTest(flag=flag), self.assertRaisesRegex(SystemExit, 'Not a stable published release'):
                self.run_workflow(**{flag: True})

    def test_manual_dispatch_keeps_test_message(self):
        message = self.run_workflow(event_name='workflow_dispatch')
        self.assertIn('Проверка уведомлений / Release bot test', message)
        self.assertNotIn('Описание и загрузка', message)

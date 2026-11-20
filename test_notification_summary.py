import unittest

from test_bridge import b, config


class NotificationSummaryTests(unittest.TestCase):
    def test_card_preview_contains_body_without_changing_markdown(self):
        text = '## 验证完成\n\n**已修复**通知，查看 [结果](https://example.com/report)。'
        card = b.message_cards(text, config())[0]
        self.assertEqual(card['config']['summary']['content'], '验证完成 已修复通知，查看 结果。')
        self.assertEqual(card['header']['title']['content'], '验证完成 已修复通知，查看 结果。')
        self.assertEqual(card['body']['elements'], [{'tag': 'markdown', 'content': text}])
        self.assertTrue(card['config']['update_multi'])

    def test_empty_and_code_only_messages_have_useful_preview(self):
        self.assertEqual(b.message_cards('', config())[0]['config']['summary']['content'], '（无正文）')
        text = '```python\nprint("验证通过")\n```'
        self.assertEqual(b.message_cards(text, config())[0]['config']['summary']['content'], 'print("验证通过")')

    def test_each_part_has_its_own_bounded_preview(self):
        text = '第一部分\n' + '甲' * 3500 + '\n第二部分\n' + '乙' * 3500
        cards = b.message_cards(text, config())
        self.assertGreater(len(cards), 1)
        previews = [card['config']['summary']['content'] for card in cards]
        self.assertNotEqual(previews[0], previews[-1])
        self.assertTrue(all(0 < len(preview) <= 160 for preview in previews))
        self.assertTrue(previews[0].endswith('…'))
        self.assertNotIn('\n', previews[0])

    def test_secrets_and_control_characters_are_removed_from_preview(self):
        text = 'private-test-secret Bearer token123 <at user_id="all">all</at>\x00\n完成'
        preview = b.message_cards(text, config())[0]['config']['summary']['content']
        self.assertNotIn('private-test-secret', preview)
        self.assertNotIn('token123', preview)
        self.assertNotIn('<at ', preview)
        self.assertNotIn('\x00', preview)
        self.assertIn('完成', preview)

    def test_question_notification_includes_question_and_preserves_callback(self):
        pending = {'params': {'questions': [{'id': 'q1', 'question': '选择哪种方案？',
                    'options': [{'label': '方案 A'}, {'label': '方案 B'}]}]},
                   'thread_id': 'thread-example'}
        card = b.question_cards('request-code', pending, config())[0]
        self.assertIn('选择哪种方案？', card['config']['summary']['content'])
        self.assertIn('方案 A', card['config']['summary']['content'])
        button = card['body']['elements'][-1]
        self.assertEqual(button['behaviors'][0]['value'], {
            'action': 'codex_answer', 'code': 'request-code', 'question': 'q1', 'option': 2})


if __name__ == '__main__':
    unittest.main()

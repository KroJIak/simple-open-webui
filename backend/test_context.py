import unittest

from open_webui.utils.context import (
    build_history_digest,
    compact_messages,
    count_images,
    estimate_content_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)

TINY_PNG = (
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
    'z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


def message(role: str, text: str = '', images: int = 0) -> dict:
    content = [{'type': 'text', 'text': text}] if text else []
    for _ in range(images):
        content.append({'type': 'image_url', 'image_url': {'url': TINY_PNG}})
    return {'role': role, 'content': content}


class ContextEstimationTests(unittest.TestCase):
    def test_empty_content_has_no_tokens(self):
        self.assertEqual(estimate_content_tokens(None), 0)
        self.assertEqual(estimate_content_tokens(''), 0)

    def test_longer_text_costs_more_tokens(self):
        short = estimate_text_tokens('Привет')
        long = estimate_text_tokens('Привет ' * 100)

        self.assertGreater(short, 0)
        self.assertGreater(long, short)

    def test_images_are_counted_and_cost_tokens(self):
        messages = [message('user', 'смотри', images=3)]

        self.assertEqual(count_images(messages), 3)
        self.assertGreater(estimate_messages_tokens(messages), estimate_text_tokens('смотри'))

    def test_tool_calls_cost_tokens(self):
        without = [{'role': 'assistant', 'content': 'ок'}]
        with_call = [
            {
                'role': 'assistant',
                'content': 'ок',
                'tool_calls': [{'function': {'name': 'search_chats', 'arguments': '{"q": "x"}'}}],
            }
        ]

        self.assertGreater(
            estimate_messages_tokens(with_call), estimate_messages_tokens(without)
        )


class ContextCompactionTests(unittest.TestCase):
    def test_short_chat_is_not_touched(self):
        messages = [message('user', 'привет'), message('assistant', 'привет')]

        compacted, info = compact_messages(messages, budget=100000)

        self.assertIs(compacted, messages)
        self.assertFalse(info['compacted'])
        self.assertEqual(info['dropped_messages'], 0)

    def test_budget_zero_disables_the_compaction(self):
        messages = [message('user', 'x' * 10000) for _ in range(50)]

        compacted, info = compact_messages(messages, budget=0)

        self.assertIs(compacted, messages)
        self.assertFalse(info['compacted'])

    def test_long_history_is_condensed(self):
        messages = []
        for i in range(200):
            messages.append(message('user', f'вопрос номер {i} ' + 'текст ' * 200))
            messages.append(message('assistant', f'ответ номер {i} ' + 'текст ' * 200))

        compacted, info = compact_messages(messages, budget=5000, block_size=20, keep_recent=20)

        self.assertTrue(info['compacted'])
        self.assertLess(len(compacted), len(messages))
        self.assertLess(info['tokens_after'], info['tokens_before'])
        self.assertIn('Earlier conversation summary', compacted[0]['content'])

    def test_the_system_prompt_is_never_condensed_away(self):
        messages = [{'role': 'system', 'content': 'Ты полезный ассистент'}]
        for i in range(200):
            messages.append(message('user', f'вопрос {i} ' + 'текст ' * 200))
            messages.append(message('assistant', f'ответ {i} ' + 'текст ' * 200))

        compacted, _ = compact_messages(messages, budget=5000, block_size=20, keep_recent=20)

        self.assertEqual(compacted[0]['role'], 'system')
        self.assertEqual(compacted[0]['content'], 'Ты полезный ассистент')

    def test_no_orphan_tool_messages(self):
        messages = []
        for i in range(100):
            messages.append(message('user', f'вопрос {i} ' + 'текст ' * 200))
            messages.append(
                {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [{'function': {'name': 'search_chats', 'arguments': '{}'}}],
                }
            )
            messages.append({'role': 'tool', 'content': 'результат поиска', 'tool_call_id': f'c{i}'})

        compacted, _ = compact_messages(messages, budget=5000, block_size=20, keep_recent=20)

        roles = [m.get('role') for m in compacted]
        for index, role in enumerate(roles):
            if role == 'tool':
                self.assertGreater(index, 0)
                self.assertEqual(roles[index - 1], 'assistant')

    def test_the_condensed_prefix_stays_stable_when_the_chat_grows(self):
        messages = []
        for i in range(200):
            messages.append(message('user', f'вопрос {i} ' + 'текст ' * 200))
            messages.append(message('assistant', f'ответ {i} ' + 'текст ' * 200))

        compacted, _ = compact_messages(messages, budget=5000, block_size=20, keep_recent=20)

        grown = messages + [message('user', 'ещё один вопрос')]
        compacted2, _ = compact_messages(grown, budget=5000, block_size=20, keep_recent=20)

        # The digest and the messages after it must not change, otherwise the
        # prompt cache of the provider would be invalidated every turn.
        self.assertEqual(compacted[0]['content'], compacted2[0]['content'])
        for index in range(1, min(len(compacted), len(compacted2)) - 1):
            self.assertEqual(compacted[index], compacted2[index])

    def test_the_digest_mentions_the_questions_of_the_user(self):
        messages = [
            message('user', 'как делать лабораторную по теории вероятностей'),
            message('assistant', 'сначала разберись с определениями'),
        ]

        digest = build_history_digest(messages)

        self.assertIn('теории вероятностей', digest)
        self.assertIn('The user asked about:', digest)


if __name__ == '__main__':
    unittest.main()

import unittest

from open_webui.utils.payload import apply_model_params_to_body_openai, sanitize_reasoning_effort_for_model


class PayloadReasoningTests(unittest.TestCase):
    def test_sanitize_reasoning_effort_keeps_allowed_value(self):
        payload = {'reasoning_effort': 'medium'}
        model_meta = {'reasoning_effort_settings': {'available': ['low', 'medium']}}

        sanitized = sanitize_reasoning_effort_for_model(payload, model_meta)

        self.assertEqual(sanitized['reasoning_effort'], 'medium')

    def test_sanitize_reasoning_effort_replaces_blocked_value_with_default_allowed_value(self):
        payload = {'reasoning_effort': 'high'}
        model_meta = {'reasoning_effort_settings': {'available': ['low', 'medium']}}

        sanitized = sanitize_reasoning_effort_for_model(payload, model_meta)

        self.assertEqual(sanitized['reasoning_effort'], 'low')

    def test_sanitize_reasoning_effort_removes_value_when_no_levels_are_allowed(self):
        payload = {'reasoning_effort': 'high'}
        model_meta = {'reasoning_effort_settings': {'available': []}}

        sanitized = sanitize_reasoning_effort_for_model(payload, model_meta)

        self.assertNotIn('reasoning_effort', sanitized)

    def test_apply_model_params_to_body_openai_sanitizes_payload_reasoning_effort(self):
        payload = {'reasoning_effort': 'high'}
        model_meta = {'reasoning_effort_settings': {'available': ['low', 'medium']}}

        sanitized = apply_model_params_to_body_openai({}, payload, model_meta)

        self.assertEqual(sanitized['reasoning_effort'], 'low')


if __name__ == '__main__':
    unittest.main()

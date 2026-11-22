import unittest

from open_webui.main import is_content_policy_error, provider_error_detail
from starlette.responses import JSONResponse


class ProviderErrorTests(unittest.TestCase):
    def test_the_message_of_the_provider_is_extracted(self):
        response = JSONResponse(
            status_code=400,
            content={'error': {'message': 'The model rejected this conversation'}},
        )

        self.assertEqual(
            provider_error_detail(response), 'The model rejected this conversation'
        )

    def test_a_plain_error_body_is_handled(self):
        response = JSONResponse(status_code=400, content={'error': 'Invalid API key'})

        self.assertEqual(provider_error_detail(response), 'Invalid API key')

    def test_a_broken_body_does_not_raise(self):
        response = JSONResponse(status_code=500, content={'error': {'message': 'boom'}})

        self.assertTrue(provider_error_detail(response))

    def test_a_content_policy_error_is_recognized(self):
        self.assertTrue(
            is_content_policy_error('This request was blocked by our content policy')
        )
        self.assertTrue(is_content_policy_error('The message failed the safety check'))

    def test_a_size_error_is_not_a_content_policy_error(self):
        self.assertFalse(
            is_content_policy_error(
                'The model rejected this conversation or its parameters.'
            )
        )


if __name__ == '__main__':
    unittest.main()

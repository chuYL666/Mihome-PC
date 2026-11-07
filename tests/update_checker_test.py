"""Release discovery with an exhausted shared-IP API quota, without network."""
import unittest
from unittest.mock import Mock, patch
import requests
from app.core.update_checker import fetch_latest_release, is_newer, LATEST_API, RELEASES_PAGE


def response(*, tag='v1.1.3', url=RELEASES_PAGE, error=None):
    result = Mock(url=url)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.raise_for_status.side_effect = error
    result.json.return_value = {'tag_name': tag, 'html_url': url, 'name': 'MijiaDeck ' + tag}
    return result


class UpdateCheckerTest(unittest.TestCase):
    def test_healthy_api_does_not_request_web_page(self):
        with patch('app.core.update_checker.requests.get', return_value=response()) as get:
            self.assertEqual(fetch_latest_release().tag, 'v1.1.3')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[0], LATEST_API)

    def test_api_rate_limit_uses_published_release_redirect_without_reading_html(self):
        for status in (403, 429):
            limited = response(error=requests.HTTPError(str(status)))
            web = response(url='https://github.com/Songmanual/MijiaDeck/releases/tag/v1.1.2')
            with patch('app.core.update_checker.requests.get', side_effect=[limited, web]) as get:
                info = fetch_latest_release()
            self.assertEqual(info.tag, 'v1.1.2')
            self.assertTrue(is_newer(info.tag, '1.1.1'))
            self.assertEqual(get.call_args_list[1].args[0], RELEASES_PAGE)
            self.assertTrue(get.call_args_list[1].kwargs['stream'])
            self.assertEqual(get.call_count, 2)
            web.json.assert_not_called()
            web.__exit__.assert_called_once()

    def test_api_bad_json_can_fall_back(self):
        api = response()
        api.json.side_effect = ValueError('bad JSON')
        web = response(url='https://github.com/Songmanual/MijiaDeck/releases/tag/v1.1.2')
        with patch('app.core.update_checker.requests.get', side_effect=[api, web]):
            self.assertEqual(fetch_latest_release().tag, 'v1.1.2')

    def test_unexpected_redirect_is_not_treated_as_a_new_release(self):
        for url in ('https://github.com/login',
                    'https://example.com/Songmanual/MijiaDeck/releases/tag/v1.1.2',
                    'https://github.com/another/repo/releases/tag/v1.1.2',
                    RELEASES_PAGE,
                    'https://github.com/Songmanual/MijiaDeck/releases/tag/not-a-version'):
            with patch('app.core.update_checker.requests.get', side_effect=[
                    requests.Timeout('PRIVATE'), response(url=url)]):
                with self.assertRaisesRegex(RuntimeError, '暂时无法连接更新服务'):
                    fetch_latest_release()

    def test_both_requests_failing_have_a_short_message_without_raw_exception_or_url(self):
        with patch('app.core.update_checker.requests.get', side_effect=requests.Timeout('PRIVATE-URL')) as get:
            with self.assertRaises(RuntimeError) as caught:
                fetch_latest_release()
        self.assertEqual(get.call_count, 2)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('https:', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

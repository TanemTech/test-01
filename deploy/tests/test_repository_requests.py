import unittest
from unittest.mock import patch

from deploy.repository_requests import InvalidRequest, parse_request, validate_yandex_url, create_repository


CFG = {"projects": {"sergei": {"prefix": "Sergei"}, "ilya": {"prefix": "Ilya"}, "grigoriy": {"prefix": "Grigoriy"}}}


def issue(body=None, author="TanemTech"):
    return {
        "title": "TANEM: CREATE REPOSITORY",
        "user": {"login": author},
        "body": body or '{"project":"sergei","client_slug":"anna-nails","yandex_url":"https://yandex.ru/maps/org/anna/123"}',
    }


class ValidationTests(unittest.TestCase):
    def test_creates_public_client_without_starting_deployment(self):
        details = parse_request(issue(), CFG)
        with patch('deploy.repository_requests.github_api', return_value={
            'full_name': 'TanemTech/Sergei-anna-nails',
            'html_url': 'https://github.com/TanemTech/Sergei-anna-nails',
        }) as api:
            self.assertEqual(create_repository(details, 'test-token'),
                             'https://github.com/TanemTech/Sergei-anna-nails')
        api.assert_called_once_with('POST', '/user/repos', 'test-token', {
            'name': 'Sergei-anna-nails', 'description': 'Яндекс.Карты: ' + details['url'],
            'private': False, 'auto_init': True, 'has_issues': True,
        })

    def test_builds_repository_from_project_label(self):
        r = parse_request(issue(), CFG)
        self.assertEqual(r["name"], "Sergei-anna-nails")
        self.assertEqual(r["project"], "sergei")

    def test_second_project_gets_other_prefix(self):
        r = parse_request(issue('{"project":"ilya","client_slug":"salon","yandex_url":"https://yandex.ru/maps/"}'), CFG)
        self.assertEqual(r["name"], "Ilya-salon")

    def test_grigoriy_project(self):
        r = parse_request(issue('{"project":"grigoriy","client_slug":"eva","yandex_url":"https://yandex.com/maps/org/yeva/1179517154/"}'), CFG)
        self.assertEqual(r["name"], "Grigoriy-eva")

    def test_unknown_author_rejected(self):
        with self.assertRaises(InvalidRequest):
            parse_request(issue(author="stranger"), CFG)

    def test_cannot_override_prefix(self):
        with self.assertRaises(InvalidRequest):
            parse_request(issue('{"project":"sergei","client_slug":"anna","yandex_url":"https://yandex.ru/maps/", "prefix":"Ilya"}'), CFG)

    def test_unknown_project_rejected(self):
        with self.assertRaises(InvalidRequest):
            parse_request(issue('{"project":"unknown","client_slug":"anna","yandex_url":"https://yandex.ru/maps/"}'), CFG)

    def test_shell_like_slug_rejected(self):
        with self.assertRaises(InvalidRequest):
            parse_request(issue('{"project":"sergei","client_slug":"anna$(touch /tmp/owned)","yandex_url":"https://yandex.ru/maps/"}'), CFG)

    def test_domain_spoof_rejected(self):
        for url in ["http://yandex.ru/maps", "https://yandex.ru.evil.test/maps/", "https://evil.test/@yandex.ru/maps", "https://yandex.ru/other", "https://yandex.ru:444/maps"]:
            with self.subTest(url=url), self.assertRaises(InvalidRequest):
                validate_yandex_url(url)

    def test_short_yandex_links_accepted(self):
        self.assertEqual(validate_yandex_url("https://yandex.ru/maps/-/abc"), "https://yandex.ru/maps/-/abc")


if __name__ == "__main__":
    unittest.main()

import unittest

from deploy.repository_requests import InvalidRequest, parse_request, validate_yandex_url


CFG = {"projects": {"sergei": {"prefix": "Sergei"}, "ilya": {"prefix": "Ilya"}}}


def issue(body=None, author="TanemTech"):
    return {
        "title": "TANEM: CREATE REPOSITORY",
        "user": {"login": author},
        "body": body or '{"project":"sergei","client_slug":"anna-nails","yandex_url":"https://yandex.ru/maps/org/anna/123"}',
    }


class ValidationTests(unittest.TestCase):
    def test_builds_repository_from_project_label(self):
        r = parse_request(issue(), CFG)
        self.assertEqual(r["name"], "Sergei-anna-nails")
        self.assertEqual(r["project"], "sergei")

    def test_second_project_gets_other_prefix(self):
        r = parse_request(issue('{"project":"ilya","client_slug":"salon","yandex_url":"https://yandex.ru/maps/"}'), CFG)
        self.assertEqual(r["name"], "Ilya-salon")

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

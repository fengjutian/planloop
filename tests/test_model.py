import io
import json
import unittest
import urllib.error
from unittest import mock

from planloop.model import OpenAICompatModel


class _FakeResponse:
    """Minimal stand-in for the object urlopen() returns."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._payload


def _ok_payload(text: str = "ok") -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    ).encode("utf-8")


def _http_error(code: int, body: bytes = b"error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/chat/completions", code, "err", {}, io.BytesIO(body))


class OpenAICompatRetryTest(unittest.TestCase):
    """A 502 mid-run must not abort a benchmark; a 4xx must not be retried."""

    def _model(self) -> OpenAICompatModel:
        return OpenAICompatModel(
            "fake-model", base_url="http://127.0.0.1:1/v1", api_key="k", timeout=1.0
        )

    def test_transient_server_error_is_retried_until_it_succeeds(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) < 3:
                raise _http_error(502, b"Bad Gateway")
            return _FakeResponse(_ok_payload("recovered"))

        with mock.patch("planloop.model.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("planloop.model.time.sleep"):
            completion = self._model().complete("hello")

        self.assertEqual(completion.text, "recovered")
        self.assertEqual(len(calls), 3)

    def test_client_error_is_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise _http_error(400, b"bad request")

        with mock.patch("planloop.model.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("planloop.model.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self._model().complete("hello")

        self.assertIn("400", str(ctx.exception))
        self.assertEqual(len(calls), 1)

    def test_rate_limit_is_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                raise _http_error(429, b"slow down")
            return _FakeResponse(_ok_payload())

        with mock.patch("planloop.model.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("planloop.model.time.sleep"):
            completion = self._model().complete("hello")

        self.assertEqual(completion.text, "ok")
        self.assertEqual(len(calls), 2)

    def test_network_error_is_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise urllib.error.URLError("connection reset")

        with mock.patch("planloop.model.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("planloop.model.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self._model().complete("hello")

        self.assertIn("unreachable", str(ctx.exception))
        self.assertEqual(len(calls), 3)

    def test_raises_after_the_retry_budget_is_exhausted(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise _http_error(502, b"Bad Gateway")

        with mock.patch("planloop.model.urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("planloop.model.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self._model().complete("hello")

        self.assertIn("502", str(ctx.exception))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()

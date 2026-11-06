from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.test_factory import make_repo
from factory import config, dashboard, triage


class TriageHeadersTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = make_repo(self.root, '[repo]\nslug = "acme/widgets"\n')
        self.headers = {'X-Application-Key': 'private-test-key', 'user': 'test-caller'}
        self.enterContext(mock.patch.dict('os.environ', {}, clear=True))

    def load(self, *, headers=None, repo='', api=None):
        if repo:
            (self.repo / '.factory.toml').write_text('[repo]\nslug = "acme/widgets"\n' + repo)
        host = {'defaults': {'triage': {'url': 'https://gateway.example.invalid/v1/chat/completions',
                                      'headers': self.headers if headers is None else headers}}}
        if api is not None:
            host['defaults']['triage']['api'] = api
        with mock.patch.object(config, 'host_config', return_value=host):
            return config.load(self.repo)

    def test_host_headers_loaded_and_not_in_config_repr(self):
        cfg = self.load()
        self.assertEqual(cfg.llm_headers, self.headers)
        self.assertNotIn('private-test-key', repr(cfg))
        self.assertEqual(config.unknown_keys({'triage': {'headers': self.headers}}), [])

    def test_explicit_api_selection_and_invalid_protocol(self):
        self.assertEqual(self.load().llm_api, 'openai')
        self.assertEqual(self.load(api='anthropic').llm_api, 'anthropic')
        with self.assertRaises(config.ConfigError):
            self.load(api='unknown')

    def test_repository_cannot_supply_headers_or_redirect_host_credentials(self):
        for table in ('[triage.headers]\nuser="repo-caller"\n',
                      '[triage]\nurl="https://other.example.invalid/v1/chat/completions"\n'):
            with self.subTest(table=table), self.assertRaises(config.ConfigError):
                self.load(repo=table)

    def test_invalid_headers_fail_without_disclosing_values(self):
        for headers in ('not-a-table', {'X-Test': 7}, {'bad\nname': 'private-test-key'},
                        {'X-Test': 'private-test-key\r\ninjected'}, {'Host': 'other.example.invalid'},
                        {'X-Test': 'one', 'x-test': 'two'}):
            with self.subTest(headers=headers):
                with self.assertRaises(config.ConfigError) as error:
                    self.load(headers=headers)
                self.assertNotIn('private-test-key', str(error.exception))

    def test_calls_and_both_probes_send_headers(self):
        cfg = self.load()
        cfg.llm_key = 'existing-bearer'
        triage.configure(cfg)
        dashboard.configure(cfg)
        seen = []
        class Response(io.BytesIO):
            status = 200
        def opener(request, **kwargs):
            self.assertTrue(kwargs["authenticated"])
            seen.append({k.lower(): v for k, v in request.header_items()})
            return Response(json.dumps({'choices': [{'message': {'content': 'ok'}}]}).encode())
        with mock.patch.object(triage, 'open_request', side_effect=opener):
            self.assertEqual(triage.call_llm([{'role': 'user', 'content': 'hello'}]), 'ok')
            self.assertEqual(triage.probe_endpoint(cfg), (True, 'ready'))
            self.assertTrue(dashboard.triage_llm_online())
        self.assertEqual(len(seen), 3)
        for headers in seen:
            self.assertEqual(headers['x-application-key'], 'private-test-key')
            self.assertEqual(headers['user'], 'test-caller')
            self.assertEqual(headers['authorization'], 'Bearer existing-bearer')

    def test_probe_diagnostics_do_not_echo_header_credentials(self):
        cfg = self.load()
        def refuse(*args, **kwargs):
            raise urllib.error.HTTPError('https://gateway.example.invalid', 403, 'Forbidden', {},
                                         io.BytesIO(b'echo private-test-key test-caller'))
        ok, detail = triage.probe_endpoint(cfg, opener=refuse)
        self.assertFalse(ok)
        self.assertIn('403', detail)
        self.assertNotIn('private-test-key', detail)
        self.assertNotIn('test-caller', detail)

    def test_custom_authorization_replaces_bearer_case_insensitively(self):
        cfg = self.load(headers={'AUTHORIZATION': 'Custom private-test-key'})
        self.assertEqual(triage.request_headers(cfg, 'old-key'),
                         {'authorization': 'Custom private-test-key'})

    def test_call_errors_do_not_echo_header_credentials(self):
        cfg = self.load()
        triage.configure(cfg)
        output = io.StringIO()
        error = urllib.error.URLError('echo private-test-key test-caller')
        with mock.patch.object(triage, 'open_request', side_effect=error), \
                contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            triage.call_llm([{'role': 'user', 'content': 'hello'}])
        self.assertNotIn('private-test-key', output.getvalue())
        self.assertNotIn('test-caller', output.getvalue())

    def test_real_redirect_does_not_receive_gateway_credentials(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append(self.headers.get('X-Application-Key'))
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.end_headers()
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/models'
            request = triage.urllib.request.Request(url, headers=self.headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                triage.open_request(request, timeout=3, authenticated=True)
            self.assertEqual(error.exception.code, 302)
            self.assertEqual(received, ['private-test-key'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)



class AnthropicTriageTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict('os.environ', {}, clear=True))
        self.cfg = config.Config(Path('/tmp'), 'acme/widgets')
        self.cfg.llm_api = 'anthropic'
        self.cfg.llm_url = 'https://gateway.example.invalid/v1/messages'
        self.cfg.llm_key = 'native-test-key'
        triage.configure(self.cfg)

    def test_native_request_system_auth_response_and_usage(self):
        response = {'content': [{'type': 'text', 'text': '{"decision":"needs-info"}'}],
                    'usage': {'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 3}}
        seen = []
        def opener(request, **kwargs):
            seen.append(request)
            return io.BytesIO(json.dumps(response).encode())
        with mock.patch.object(triage, 'open_request', side_effect=opener):
            reply = triage.call_llm([{'role': 'system', 'content': 'Classify only.'},
                                     {'role': 'user', 'content': 'An issue'}])
        request = seen[0]
        body = json.loads(request.data)
        self.assertEqual(body['system'], 'Classify only.')
        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'An issue'}])
        self.assertGreater(body['max_tokens'], 0)
        self.assertEqual(request.get_header('X-api-key'), 'native-test-key')
        self.assertEqual(request.get_header('Anthropic-version'), '2023-06-01')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(reply, '{"decision":"needs-info"}')
        self.assertEqual(reply.cost['prompt_tokens'], 13)
        self.assertEqual(reply.cost['completion_tokens'], 4)
        self.assertEqual(reply.cost['cached_tokens'], 3)

    def test_native_models_probe_uses_sibling_endpoint_and_api_key(self):
        seen = []
        def opener(request, **kwargs):
            seen.append(request)
            return contextlib.nullcontext()
        self.assertEqual(triage.probe_endpoint(self.cfg, opener=opener), (True, 'ready'))
        self.assertEqual(seen[0].full_url, 'https://gateway.example.invalid/v1/models')
        self.assertEqual(seen[0].get_header('X-api-key'), 'native-test-key')


    def test_native_error_envelope_and_truncation_are_sanitized(self):
        for response in ({'error': 'private-response-body'},
                         {'content': [{'type': 'text', 'text': 'private-response-body'}],
                          'stop_reason': 'max_tokens'}):
            with self.subTest(response=response):
                output = io.StringIO()
                with mock.patch.object(triage, 'open_request',
                                       return_value=io.BytesIO(json.dumps(response).encode())), \
                        contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as error:
                    triage.call_llm([{'role': 'user', 'content': 'hello'}])
                self.assertEqual(error.exception.code, 2)
                self.assertNotIn('private-response-body', output.getvalue())


if __name__ == '__main__':
    unittest.main()

import unittest

from app.renderer import capture_values, json_path, render, validate_captures
from app.schemas import CaptureRule, ReplyConfig, ScenarioDefinition, ValueRule


class JsonPathTests(unittest.TestCase):
    def test_reads_nested_object_and_array(self):
        value = {"subscriber": {"balances": [{"amount": 42}]}}
        self.assertEqual(json_path(value, "$.subscriber.balances.0.amount"), 42)


class CaptureTests(unittest.TestCase):
    def test_captures_json_and_headers(self):
        scenario = ScenarioDefinition(
            key="test", name="Test", flow="sync", request_format="json",
            captures=[
                CaptureRule(name="request_id", source="json", path="request.requestId"),
                CaptureRule(name="corr", source="header", path="X-Correlation-Id"),
            ],
        )
        values = capture_values(
            scenario,
            b'{"request":{"requestId":"REQ-1"}}',
            {"x-correlation-id": "CORR-1"},
            {},
        )
        self.assertEqual(values, {"request_id": "REQ-1", "corr": "CORR-1"})

    def test_captures_namespace_qualified_xml(self):
        scenario = ScenarioDefinition(
            key="test", name="Test", flow="sync", request_format="xml",
            namespaces={"x": "urn:sample"},
            captures=[CaptureRule(name="msisdn", source="xml", path=".//x:PrimaryIdentity")],
        )
        values = capture_values(
            scenario,
            b'<Request xmlns="urn:sample"><PrimaryIdentity>12345</PrimaryIdentity></Request>',
            {}, {},
        )
        self.assertEqual(values["msisdn"], "12345")


class ResponseTests(unittest.TestCase):
    def test_renders_capture_into_json_response(self):
        reply = ReplyConfig(
            body={"result": {"msisdn": "{{msisdn}}"}},
            fields={"msisdn": ValueRule(source="capture", capture="msisdn")},
        )
        body, content_type, status, _ = render(reply, {"msisdn": "12345"}, "json")
        self.assertEqual(body, b'{"result":{"msisdn":"12345"}}')
        self.assertIn("json", content_type)
        self.assertEqual(status, 200)

    def test_escapes_xml_capture_value(self):
        reply = ReplyConfig(
            content_type="text/xml",
            body="<Reply><Value>{{value}}</Value></Reply>",
            fields={"value": ValueRule(source="capture", capture="value")},
        )
        body, content_type, status, _ = render(reply, {"value": "A&B"}, "xml")
        self.assertEqual(body, b"<Reply><Value>A&amp;B</Value></Reply>")
        self.assertIn("xml", content_type)
        self.assertEqual(status, 200)

    def test_rejects_missing_async_correlation_capture(self):
        scenario = ScenarioDefinition(
            key="test", name="Test", flow="async",
            async_config={"callback_url": "http://localhost/callback", "correlation_capture": "missing"},
        )
        with self.assertRaisesRegex(ValueError, "correlation_capture"):
            validate_captures(scenario)


if __name__ == "__main__":
    unittest.main()

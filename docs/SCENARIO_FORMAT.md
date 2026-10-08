# Scenario format

Scenario documents are versioned JSON objects. A scenario chooses one flow mode and independently selects request, immediate reply, and callback formats. The UI exposes scenario, capture, flow, payload, and response-value settings as guided form controls. JSON response bodies are generated from output paths; XML response templates are edited as XML with token fields.

## Synchronous JSON example

```json
{
  "key": "ocs-balance",
  "name": "OCS balance query",
  "flow": "sync",
  "request_format": "json",
  "response_format": "json",
  "method": "POST",
  "captures": [
    { "name": "subscriber", "source": "json", "path": "$.subscriberId" }
  ],
  "response": {
    "status_code": 200,
    "content_type": "application/json",
    "body": { "subscriberId": "{{subscriber}}", "balance": "{{balance}}" },
    "fields": {
      "subscriber": { "source": "capture", "capture": "subscriber" },
      "balance": { "source": "random_int", "minimum": 0, "maximum": 10000 }
    }
  }
}
```

## Asynchronous ACK and callback example

```json
{
  "key": "wallet-payment",
  "name": "Wallet payment callback",
  "flow": "async",
  "request_format": "json",
  "ack_format": "json",
  "callback_format": "json",
  "method": "POST",
  "captures": [
    { "name": "transactionId", "source": "json", "path": "$.transactionId" }
  ],
  "ack": {
    "status_code": 202,
    "content_type": "application/json",
    "body": { "transactionId": "{{transactionId}}", "accepted": true },
    "fields": { "transactionId": { "source": "capture", "capture": "transactionId" } }
  },
  "callback": {
    "status_code": 200,
    "content_type": "application/json",
    "body": {
      "transactionId": "{{transactionId}}",
      "providerReference": "{{providerReference}}",
      "result": "{{result}}"
    },
    "fields": {
      "transactionId": { "source": "capture", "capture": "transactionId" },
      "providerReference": { "source": "random_string", "length": 12 },
      "result": { "source": "random_choice", "choices": ["SUCCESS", "DECLINED"] }
    }
  },
  "async_config": {
    "callback_url": "http://localhost:9000/callback",
    "delay_ms": 1500,
    "max_attempts": 3,
    "retry_delay_ms": 1000,
    "correlation_capture": "transactionId"
  }
}
```

## XML rules

Use `request_format: "xml"` and add namespace mappings where needed. XML capture paths are evaluated with ElementTree's supported XPath subset. XML reply bodies are strings and can use `{{fieldName}}` tokens. Set the matching reply's content type to `application/xml` or `text/xml`. SOAPAction and full SOAP/WSDL semantics are planned follow-up work; this scaffold currently treats SOAP as XML over HTTP.

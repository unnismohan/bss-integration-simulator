from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CaptureRule(BaseModel):
    name: str
    path: str
    source: Literal["json", "xml", "header", "query", "path"] = "json"


class ValueRule(BaseModel):
    source: Literal["fixed", "capture", "random_int", "random_choice", "random_string"]
    value: Any = None
    capture: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    choices: list[Any] | None = None
    length: int | None = None
    alphabet: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    @model_validator(mode="after")
    def validate_rule(self):
        if self.source == "capture" and not self.capture:
            raise ValueError("capture rule requires capture name")
        if self.source == "random_int" and (self.minimum is None or self.maximum is None or self.minimum > self.maximum):
            raise ValueError("random_int requires minimum <= maximum")
        if self.source == "random_choice" and not self.choices:
            raise ValueError("random_choice requires at least one choice")
        if self.source == "random_string" and (not self.length or self.length < 1 or not self.alphabet):
            raise ValueError("random_string requires positive length and a non-empty alphabet")
        return self


class ReplyConfig(BaseModel):
    status_code: int = Field(default=200, ge=100, le=599)
    content_type: str = "application/json"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default_factory=dict)
    fields: dict[str, ValueRule] = Field(default_factory=dict)
    sample_body: str | None = None


class AsyncConfig(BaseModel):
    callback_url: str
    delay_ms: int = Field(default=1000, ge=1, le=86_400_000)
    max_attempts: int = Field(default=1, ge=1, le=20)
    retry_delay_ms: int = Field(default=1000, ge=0, le=3_600_000)
    correlation_capture: str


class ScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    name: str
    flow: Literal["sync", "async"]
    request_format: Literal["json", "xml"] = "json"
    response_format: Literal["json", "xml"] = "json"
    ack_format: Literal["json", "xml"] = "json"
    callback_format: Literal["json", "xml"] = "json"
    method: str = "POST"
    path_template: str = ""
    captures: list[CaptureRule] = Field(default_factory=list)
    request_sample: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    response: ReplyConfig = Field(default_factory=ReplyConfig)
    ack: ReplyConfig = Field(default_factory=lambda: ReplyConfig(status_code=202, body={"accepted": True}))
    callback: ReplyConfig = Field(default_factory=ReplyConfig)
    async_config: AsyncConfig | None = None

    @model_validator(mode="after")
    def validate_path_template(self):
        template = self.path_template.strip().strip("/")
        if any(not segment for segment in template.split("/")) and template:
            raise ValueError("path_template must contain non-empty path segments")
        for segment in template.split("/") if template else []:
            if "{" in segment or "}" in segment:
                if not (segment.startswith("{") and segment.endswith("}") and segment.count("{") == 1 and segment.count("}") == 1):
                    raise ValueError("Path parameters must occupy a whole segment, for example {msisdn}")
                name = segment[1:-1]
                if not name.isidentifier():
                    raise ValueError("Path parameter names must be valid identifiers")
        names = [segment[1:-1] for segment in template.split("/") if segment.startswith("{") and segment.endswith("}")]
        if len(names) != len(set(names)):
            raise ValueError("Path parameter names must be unique")
        for capture in self.captures:
            if capture.source == "path" and capture.path not in names:
                raise ValueError(f"Path capture '{capture.path}' is not declared in path_template")
        return self

    @model_validator(mode="after")
    def validate_async(self):
        if self.flow == "async" and self.async_config is None:
            raise ValueError("async flow requires async_config")
        if self.flow == "sync" and self.async_config is not None:
            raise ValueError("sync flow must not define async_config")
        return self

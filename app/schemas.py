"""Pydantic request/response models — also the public OpenAPI contract at /docs."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExtractRequest(BaseModel):
    """Body for `POST /api/extract`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(
        ...,
        min_length=8,
        max_length=2048,
        description="Public video URL from a supported platform.",
        json_schema_extra={"example": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    platform: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Slug or key of the tool page the request came from; selects the "
                    "extraction profile (audio-only, merged-only, max height, brand "
                    "container preference). Unknown values fall back to the generic profile.",
    )
    locale: Optional[str] = Field(default=None, max_length=8, description="Locale of the calling page.")

    @field_validator("url")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        # Newlines/CR in the body would be a header/response-splitting probe.
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("The link contains control characters.")
        return value


class FormatModel(BaseModel):
    id: str
    label: str
    kind: Literal["video", "audio", "video_only"]
    ext: str
    height: Optional[int] = None
    width: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    filesize: Optional[int] = None
    filesize_text: Optional[str] = None
    quality_note: Optional[str] = None
    url: str
    protocol: str
    streamable_in_browser: bool = True
    is_default: bool = False
    has_merged_audio: bool = True
    approx_note: Optional[str] = None


class ExtractResponse(BaseModel):
    ok: Literal[True] = True
    platform: str
    id: str
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    duration_text: Optional[str] = None
    uploader: Optional[str] = None
    webpage_url: str
    upload_date: Optional[str] = None
    is_live: bool = False
    availability: Optional[str] = None
    formats: list[FormatModel] = Field(default_factory=list)
    resolve_ms: int = 0
    cached: bool = False
    warnings: list[str] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine code, e.g. `private`, `rate_limited`.")
    message: str = Field(description="Human-readable message, localised to the caller's locale.")
    hint: Optional[str] = None
    retry_after: Optional[int] = None


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: ErrorDetail


class PlatformSummary(BaseModel):
    slug: str
    path: str
    name: str
    icon: str
    domains: list[str]
    extraction: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    engine: dict
    cache: dict
    limiter: dict
    uptime_seconds: float

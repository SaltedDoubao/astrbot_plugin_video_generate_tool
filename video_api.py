from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

_JSON_PATH_TOKEN = re.compile(r"([^[\].]+)|\[(\d+)\]")


class VideoApiError(RuntimeError):
    """视频 API 调用异常。"""


@dataclass
class ProviderConfig:
    provider_id: str
    base_url: str
    api_key: str = ""
    model: str = ""
    submit_path: str = "/v1/videos"
    status_path_template: str = "/v1/videos/{task_id}"
    submit_method: str = "POST"
    status_method: str = "GET"
    prompt_field: str = "prompt"
    model_field: str = "model"
    task_id_field: str = "id"
    status_field: str = "status"
    output_url_field: str = "output[0].url"
    error_field: str = "error.message"
    done_values: list[str] = field(
        default_factory=lambda: ["succeeded", "completed", "success", "done", "finished"]
    )
    failed_values: list[str] = field(
        default_factory=lambda: ["failed", "error", "cancelled", "canceled", "rejected"]
    )
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # 非 GET 状态查询时请求体中的任务 ID 字段名，留空则自动从 task_id_field 取叶子节点
    status_request_id_field: str = ""
    # duration 和 aspect_ratio 在请求体中的字段名（不同服务商可能不同）
    duration_field: str = "duration"
    aspect_ratio_field: str = "aspect_ratio"


@dataclass
class TaskSnapshot:
    provider_id: str
    task_id: str
    status: str
    video_url: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "task_id": self.task_id,
            "status": self.status,
            "video_url": self.video_url,
            "error_message": self.error_message,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSnapshot":
        return cls(
            provider_id=str(data.get("provider_id", "")),
            task_id=str(data.get("task_id", "")),
            status=str(data.get("status", "")),
            video_url=str(data.get("video_url", "")),
            error_message=str(data.get("error_message", "")),
            raw=dict(data.get("raw", {})) if isinstance(data.get("raw"), Mapping) else {},
        )


def extract_json_path(payload: Any, path: str) -> Any:
    if not path:
        return None
    current = payload
    for token in _JSON_PATH_TOKEN.finditer(path):
        key, index = token.groups()
        if key is not None:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
        else:
            if not isinstance(current, list):
                return None
            idx = int(index)
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
        if current is None:
            return None
    return current


class VideoApiClient:
    def __init__(self, timeout_seconds: float = 45.0, debug: bool = False):
        self.timeout_seconds = timeout_seconds
        self.debug = debug
        _timeout = httpx.Timeout(
            connect=10.0,
            read=timeout_seconds,
            write=30.0,
            pool=10.0,
        )
        self._http_client = httpx.AsyncClient(timeout=_timeout)

    def _debug_log(self, msg: str) -> None:
        if self.debug:
            _logger.info("[video_generate_tool][DEBUG] %s", msg)

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}***{key[-4:]}"

    async def close(self) -> None:
        await self._http_client.aclose()

    async def submit(
        self,
        provider: ProviderConfig,
        prompt: str,
        model_override: str = "",
        extra_options: Mapping[str, Any] | None = None,
    ) -> TaskSnapshot:
        payload = dict(provider.extra_body)
        payload[provider.prompt_field] = prompt
        model = model_override.strip() if model_override else provider.model.strip()
        if model:
            payload[provider.model_field] = model
        if extra_options:
            payload.update(extra_options)

        url = self._join_url(provider.base_url, provider.submit_path)
        self._debug_log(
            f"提交任务: provider={provider.provider_id}, method={provider.submit_method}, "
            f"url={url}, model={model or '(默认)'}, prompt={prompt[:80]!r}"
        )
        data = await self._request_json(
            method=provider.submit_method,
            url=url,
            headers=self._build_headers(provider, provider.submit_method),
            json_payload=payload,
            error_path=provider.error_field,
        )
        snapshot = self._snapshot_from_payload(provider, data)
        self._debug_log(
            f"提交响应: task_id={snapshot.task_id}, status={snapshot.status}"
        )
        return snapshot

    async def query(self, provider: ProviderConfig, task_id: str) -> TaskSnapshot:
        status_path = provider.status_path_template.replace("{task_id}", quote(task_id, safe=""))
        url = self._join_url(provider.base_url, status_path)
        json_payload = None
        if provider.status_method.upper() != "GET":
            if provider.status_request_id_field:
                id_key = provider.status_request_id_field
            else:
                leaf = provider.task_id_field.rsplit(".", 1)[-1] if provider.task_id_field else "id"
                id_key = leaf.split("[")[0] or "id"
            json_payload = {id_key: task_id}
        self._debug_log(
            f"查询任务: provider={provider.provider_id}, method={provider.status_method}, "
            f"url={url}, task_id={task_id}"
        )
        data = await self._request_json(
            method=provider.status_method,
            url=url,
            headers=self._build_headers(provider, provider.status_method),
            json_payload=json_payload,
            error_path=provider.error_field,
        )
        snapshot = self._snapshot_from_payload(provider, data, fallback_task_id=task_id)
        self._debug_log(
            f"查询响应: task_id={snapshot.task_id}, status={snapshot.status}, "
            f"video_url={'(有)' if snapshot.video_url else '(无)'}"
        )
        return snapshot

    def _snapshot_from_payload(
        self, provider: ProviderConfig, payload: Mapping[str, Any], fallback_task_id: str = ""
    ) -> TaskSnapshot:
        task_id = self._as_text(extract_json_path(payload, provider.task_id_field))
        if not task_id:
            task_id = fallback_task_id

        status = self._as_text(extract_json_path(payload, provider.status_field), default="unknown")
        video_url = self._as_text(extract_json_path(payload, provider.output_url_field))
        error_message = self._as_text(extract_json_path(payload, provider.error_field))

        if not task_id and not video_url:
            raise VideoApiError(
                f"服务商 {provider.provider_id} 返回内容中未找到任务 ID 或视频地址，"
                f"请检查 task_id_field/output_url_field 配置。"
            )

        return TaskSnapshot(
            provider_id=provider.provider_id,
            task_id=task_id,
            status=status,
            video_url=video_url,
            error_message=error_message,
            raw=dict(payload),
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_payload: Mapping[str, Any] | None,
        error_path: str,
    ) -> dict[str, Any]:
        if self.debug:
            masked_headers = {
                k: (self._mask_key(v) if k.lower() == "authorization" else v)
                for k, v in headers.items()
            }
            body_preview = str(json_payload)[:300] if json_payload else "(无)"
            self._debug_log(f"HTTP {method.upper()} {url}")
            self._debug_log(f"请求头: {masked_headers}")
            self._debug_log(f"请求体: {body_preview}")

        try:
            resp = await self._http_client.request(
                method=method.upper(),
                url=url,
                headers=dict(headers),
                json=dict(json_payload) if json_payload is not None else None,
            )
        except httpx.HTTPError as exc:
            raise VideoApiError(f"请求视频服务失败: {exc}") from exc

        text = resp.text.strip()
        payload: Any = {}
        if text:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw_text": text}
        elif resp.status_code < 400:
            raise VideoApiError(
                f"视频服务返回空响应体 (HTTP {resp.status_code})，无法获取任务 ID。"
                f"请确认该服务商 API 在提交成功后会返回包含任务 ID 的 JSON 响应。"
            )

        if self.debug:
            body_resp = text[:500] if text else "(空)"
            self._debug_log(f"响应状态: HTTP {resp.status_code}")
            self._debug_log(f"响应体: {body_resp}")

        if resp.status_code >= 400:
            detail = self._as_text(extract_json_path(payload, error_path)) if payload else text
            raise VideoApiError(
                f"视频服务响应错误: HTTP {resp.status_code}, detail={detail or '无'}"
            )

        if isinstance(payload, Mapping):
            return dict(payload)
        return {"data": payload}

    @staticmethod
    def _join_url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _as_text(value: Any, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _build_headers(provider: ProviderConfig, method: str = "POST") -> dict[str, str]:
        headers: dict[str, str] = {}
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        headers.update(provider.extra_headers)
        return headers

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Mapping

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

try:
    # AstrBot 常见加载方式：package.module（需要相对导入）
    from .video_api import ProviderConfig, TaskSnapshot, VideoApiClient, VideoApiError
except ImportError:
    # 兼容直接以脚本/顶层模块方式加载
    from video_api import ProviderConfig, TaskSnapshot, VideoApiClient, VideoApiError

try:
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover - 兼容旧版本类型导出
    AstrBotConfig = dict  # type: ignore[assignment]


@register("video_generate_tool", "SaltedDoubao", "多服务商视频生成工具", "0.1.0")
class VideoGenerateToolPlugin(Star):
    _TASK_CACHE_MAX = 200

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._providers = self._load_providers()
        timeout = float(self._cfg_get("request_timeout_seconds", 45))
        self._debug = bool(self._cfg_get("debug_mode", False))
        self._client = VideoApiClient(timeout_seconds=max(timeout, 5.0), debug=self._debug)

    async def initialize(self):
        logger.info(
            f"[video_generate_tool] 插件已加载，已配置服务商数量: {len(self._providers)}"
        )
        if self._debug:
            logger.info("[video_generate_tool][DEBUG] 调试模式已开启")

    def _debug_log(self, msg: str) -> None:
        if self._debug:
            logger.info("[video_generate_tool][DEBUG] %s", msg)

    @filter.command_group("video")
    def video(self):
        """视频生成命令组。"""

    @video.command("providers")
    async def video_providers(self, event: AstrMessageEvent):
        """列出可用的视频服务商。"""
        if not self._providers:
            yield event.plain_result("未配置任何视频服务商，请先在插件配置里填写 providers。")
            return

        default_provider_id = self._cfg_get("default_provider_id", "")
        lines = ["当前可用服务商："]
        for provider_id, provider in self._providers.items():
            tag = " (default)" if provider_id == default_provider_id else ""
            model = provider.model or "-"
            lines.append(f"- {provider_id}{tag}, model={model}, base_url={provider.base_url}")
        yield event.plain_result("\n".join(lines))

    @video.command("gen")
    async def video_gen(self, event: AstrMessageEvent, provider_id: str, prompt: str):
        """生成视频。

        Args:
            provider_id(string): 服务商 ID（需在配置中存在）
            prompt(string): 视频提示词
        """
        provider = self._resolve_provider(provider_id)
        if provider is None:
            yield event.plain_result(
                f"服务商 `{provider_id}` 不存在。先执行 `/video providers` 查看可用 ID。"
            )
            return

        try:
            submit_snapshot = await self._client.submit(provider=provider, prompt=prompt)
            await self._save_task(event, submit_snapshot, prompt=prompt, model=provider.model)
        except VideoApiError as exc:
            yield event.plain_result(f"提交视频任务失败: {exc}")
            return

        yield event.plain_result(
            f"任务已提交: provider={provider.provider_id}, task_id={submit_snapshot.task_id or 'N/A'}, "
            f"status={submit_snapshot.status}。正在等待生成完成..."
        )

        final_snapshot = await self._wait_for_result(provider, submit_snapshot)
        await self._save_task(event, final_snapshot, prompt=prompt, model=provider.model)

        if self._is_failed(provider, final_snapshot):
            detail = final_snapshot.error_message or final_snapshot.status
            yield event.plain_result(f"视频生成失败: task_id={final_snapshot.task_id}, detail={detail}")
            return

        if final_snapshot.video_url:
            text = (
                f"视频生成完成: provider={provider.provider_id}, "
                f"task_id={final_snapshot.task_id}, status={final_snapshot.status}"
            )
            chain_result = self._video_chain_result(event, text, final_snapshot.video_url)
            if chain_result is not None:
                yield chain_result
            else:
                yield event.plain_result(f"{text}\n视频地址: {final_snapshot.video_url}")
            return

        yield event.plain_result(
            "任务仍在处理中，请稍后使用 `/video status <task_id>` 查询。"
            f"\n当前 task_id={final_snapshot.task_id}, status={final_snapshot.status}"
        )

    @video.command("status")
    async def video_status(self, event: AstrMessageEvent, task_id: str = ""):
        """查询任务状态。

        Args:
            task_id(string): 任务 ID，留空时查询当前会话最近一次任务
        """
        if not task_id:
            task_id = await self._load_last_task_id(event)
            if not task_id:
                yield event.plain_result("未提供 task_id，且当前会话没有历史任务。")
                return

        snapshot = await self._load_task(task_id)
        if snapshot is None:
            yield event.plain_result(
                f"本地未找到 task_id={task_id} 的记录。请先通过 `/video gen` 提交任务。"
            )
            return

        provider = self._providers.get(snapshot.provider_id)
        if provider is None:
            yield event.plain_result(
                f"任务关联服务商 `{snapshot.provider_id}` 未配置，无法远程刷新。"
            )
            return

        try:
            latest = await self._client.query(provider=provider, task_id=snapshot.task_id)
            await self._save_task(event, latest, prompt="", model=provider.model)
        except VideoApiError as exc:
            yield event.plain_result(f"查询失败: {exc}")
            return

        if latest.video_url:
            text = (
                f"任务已完成: provider={provider.provider_id}, "
                f"task_id={latest.task_id}, status={latest.status}"
            )
            chain_result = self._video_chain_result(event, text, latest.video_url)
            if chain_result is not None:
                yield chain_result
            else:
                yield event.plain_result(f"{text}\n视频地址: {latest.video_url}")
            return

        detail = f"任务状态: provider={provider.provider_id}, task_id={latest.task_id}, status={latest.status}"
        if latest.error_message:
            detail += f", error={latest.error_message}"
        yield event.plain_result(detail)

    @filter.llm_tool(name="video_generate")
    async def video_generate_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        provider_id: str = "",
        model: str = "",
        duration: float = 0,
        aspect_ratio: str = "16:9",
        wait: bool = True,
        _: str = "",
    ) -> str:
        """调用视频服务生成视频，可由 AI 自动调用。

        Args:
            prompt(string): 视频生成提示词
            provider_id(string): 服务商 ID，留空使用默认服务商
            model(string): 模型名，留空使用服务商默认模型
            duration(number): 期望视频秒数，<=0 表示不传
            aspect_ratio(string): 宽高比，如 16:9
            wait(boolean): 是否等待任务完成再返回
            _(string): 内部保留参数，忽略
        """
        provider = self._resolve_provider(provider_id)
        if provider is None:
            return "video_generate 调用失败: provider_id 不存在或未配置。"

        options: dict[str, Any] = {}
        if duration > 0:
            options[provider.duration_field] = duration
        if aspect_ratio.strip():
            options[provider.aspect_ratio_field] = aspect_ratio.strip()

        try:
            submit_snapshot = await self._client.submit(
                provider=provider,
                prompt=prompt,
                model_override=model,
                extra_options=options,
            )
            await self._save_task(event, submit_snapshot, prompt=prompt, model=model or provider.model)
        except VideoApiError as exc:
            return f"video_generate 调用失败: {exc}"

        if not wait:
            return (
                f"video_generate 已提交: provider={provider.provider_id}, "
                f"task_id={submit_snapshot.task_id}, status={submit_snapshot.status}"
            )

        final_snapshot = await self._wait_for_result(provider, submit_snapshot)
        await self._save_task(event, final_snapshot, prompt=prompt, model=model or provider.model)

        if self._is_failed(provider, final_snapshot):
            return (
                "video_generate 任务失败: "
                f"task_id={final_snapshot.task_id}, "
                f"status={final_snapshot.status}, "
                f"error={final_snapshot.error_message or '-'}"
            )

        if final_snapshot.video_url:
            return (
                "video_generate 任务完成: "
                f"task_id={final_snapshot.task_id}, "
                f"url={final_snapshot.video_url}"
            )

        return (
            "video_generate 等待超时或未完成: "
            f"task_id={final_snapshot.task_id}, status={final_snapshot.status}"
        )

    @filter.llm_tool(name="video_query_status")
    async def video_query_status_tool(
        self,
        event: AstrMessageEvent,
        task_id: str,
        _: str = "",
    ) -> str:
        """查询视频任务状态，可由 AI 自动调用。

        Args:
            task_id(string): 任务 ID
            _(string): 内部保留参数，忽略
        """
        snapshot = await self._load_task(task_id)
        if snapshot is None:
            return f"video_query_status: 未找到 task_id={task_id} 的本地记录。"

        provider = self._providers.get(snapshot.provider_id)
        if provider is None:
            return (
                f"video_query_status: 服务商 `{snapshot.provider_id}` 未配置。"
            )

        try:
            latest = await self._client.query(provider=provider, task_id=task_id)
            await self._save_task(event, latest, prompt="", model=provider.model)
        except VideoApiError as exc:
            return f"video_query_status 查询失败: {exc}"

        if latest.video_url:
            return (
                f"video_query_status: completed, task_id={task_id}, url={latest.video_url}"
            )

        return (
            "video_query_status: "
            f"task_id={task_id}, status={latest.status}, error={latest.error_message or '-'}"
        )

    async def terminate(self):
        await self._client.close()
        logger.info("[video_generate_tool] 插件已卸载。")

    async def _wait_for_result(self, provider: ProviderConfig, snapshot: TaskSnapshot) -> TaskSnapshot:
        if self._is_terminal(provider, snapshot):
            return snapshot

        task_id = snapshot.task_id
        if not task_id:
            return snapshot

        interval = max(int(self._cfg_get("poll_interval_seconds", 6)), 1)
        attempts = max(int(self._cfg_get("max_poll_attempts", 20)), 1)

        latest = snapshot
        consecutive_errors = 0
        max_transient_errors = 3
        for attempt in range(attempts):
            await asyncio.sleep(interval)
            self._debug_log(
                f"轮询第 {attempt + 1}/{attempts} 次: task_id={task_id}, "
                f"当前状态={latest.status or '(未知)'}"
            )
            try:
                latest = await self._client.query(provider=provider, task_id=task_id)
                consecutive_errors = 0
            except VideoApiError as exc:
                consecutive_errors += 1
                self._debug_log(f"轮询出错 (连续第 {consecutive_errors} 次): {exc}")
                if consecutive_errors >= max_transient_errors:
                    return TaskSnapshot(
                        provider_id=latest.provider_id,
                        task_id=latest.task_id,
                        status=latest.status or "error",
                        video_url=latest.video_url,
                        error_message=str(exc),
                        raw=latest.raw,
                    )
                continue
            if self._is_terminal(provider, latest):
                self._debug_log(f"任务已终态: status={latest.status}, video_url={'(有)' if latest.video_url else '(无)'}")
                return latest
        self._debug_log(f"轮询结束（已达最大次数 {attempts}）: task_id={task_id}, status={latest.status}")
        return latest

    def _is_terminal(self, provider: ProviderConfig, snapshot: TaskSnapshot) -> bool:
        if snapshot.video_url:
            return True
        status = (snapshot.status or "").strip().lower()
        done = {item.lower() for item in provider.done_values}
        failed = {item.lower() for item in provider.failed_values}
        return status in done or status in failed

    def _is_failed(self, provider: ProviderConfig, snapshot: TaskSnapshot) -> bool:
        status = (snapshot.status or "").strip().lower()
        failed = {item.lower() for item in provider.failed_values}
        if status in failed:
            return True
        # 仅在状态为终态（非进行中）时才通过 error_message 判断失败
        done = {item.lower() for item in provider.done_values}
        is_terminal_status = status in done or status in failed
        return bool(is_terminal_status and snapshot.error_message and not snapshot.video_url)

    def _video_chain_result(
        self, event: AstrMessageEvent, text: str, video_url: str
    ) -> MessageEventResult | None:
        try:
            return event.chain_result([Comp.Plain(text), Comp.Video.fromURL(video_url)])
        except Exception as exc:
            logger.warning(f"发送视频组件失败，将回退到纯文本 URL: {exc}")
            return None

    def _resolve_provider(self, provider_id: str) -> ProviderConfig | None:
        provider_id = provider_id.strip()
        if provider_id:
            return self._providers.get(provider_id)
        default_provider_id = self._cfg_get("default_provider_id", "")
        if default_provider_id and default_provider_id in self._providers:
            return self._providers[default_provider_id]
        if self._providers:
            return next(iter(self._providers.values()))
        return None

    _VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

    def _load_providers(self) -> dict[str, ProviderConfig]:
        result: dict[str, ProviderConfig] = {}
        providers = self._cfg_get("providers", [])
        if not isinstance(providers, list):
            return result

        for item in providers:
            if not isinstance(item, Mapping):
                continue
            provider_id = str(item.get("provider_id", "")).strip()
            base_url = str(item.get("base_url", "")).strip()
            if not provider_id or not base_url:
                continue

            submit_method = str(item.get("submit_method", "POST")).strip().upper()
            status_method = str(item.get("status_method", "GET")).strip().upper()
            if submit_method not in self._VALID_HTTP_METHODS:
                logger.warning(
                    f"[video_generate_tool] 服务商 {provider_id} 的 submit_method '{submit_method}' 不合法，已跳过。"
                )
                continue
            if status_method not in self._VALID_HTTP_METHODS:
                logger.warning(
                    f"[video_generate_tool] 服务商 {provider_id} 的 status_method '{status_method}' 不合法，已跳过。"
                )
                continue

            done_values = self._parse_csv(
                str(item.get("done_values", "succeeded,completed,success,done,finished"))
            )
            failed_values = self._parse_csv(
                str(item.get("failed_values", "failed,error,cancelled,canceled,rejected"))
            )

            config = ProviderConfig(
                provider_id=provider_id,
                base_url=base_url,
                api_key=str(item.get("api_key", "")).strip(),
                model=str(item.get("model", "")).strip(),
                submit_path=str(item.get("submit_path", "/v1/videos")).strip(),
                status_path_template=str(
                    item.get("status_path_template", "/v1/videos/{task_id}")
                ).strip(),
                submit_method=submit_method,
                status_method=status_method,
                prompt_field=str(item.get("prompt_field", "prompt")).strip(),
                model_field=str(item.get("model_field", "model")).strip(),
                task_id_field=str(item.get("task_id_field", "id")).strip(),
                status_field=str(item.get("status_field", "status")).strip(),
                output_url_field=str(item.get("output_url_field", "output[0].url")).strip(),
                error_field=str(item.get("error_field", "error.message")).strip(),
                done_values=done_values or ["succeeded", "completed", "success", "done", "finished"],
                failed_values=failed_values or ["failed", "error", "cancelled", "canceled", "rejected"],
                extra_headers=self._parse_json_object(
                    item.get("extra_headers_json", "{}"), f"{provider_id}.extra_headers_json"
                ),
                extra_body=self._parse_json_object(
                    item.get("extra_body_json", "{}"), f"{provider_id}.extra_body_json"
                ),
                status_request_id_field=str(item.get("status_request_id_field", "")).strip(),
                duration_field=str(item.get("duration_field", "duration")).strip() or "duration",
                aspect_ratio_field=str(item.get("aspect_ratio_field", "aspect_ratio")).strip() or "aspect_ratio",
            )
            result[provider_id] = config
            self._debug_log(
                f"加载服务商: id={provider_id}, base_url={base_url}, "
                f"model={config.model or '(未设置)'}, submit={submit_method} {config.submit_path}, "
                f"status={status_method} {config.status_path_template}"
            )
        return result

    @staticmethod
    def _parse_csv(raw_text: str) -> list[str]:
        return [part.strip() for part in raw_text.split(",") if part.strip()]

    @staticmethod
    def _parse_json_object(raw_value: Any, field_name: str) -> dict[str, Any]:
        if isinstance(raw_value, Mapping):
            return dict(raw_value)
        text = str(raw_value or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"配置项 {field_name} 不是合法 JSON，已忽略。")
            return {}
        if not isinstance(data, Mapping):
            logger.warning(f"配置项 {field_name} 需要 JSON 对象，已忽略。")
            return {}
        return dict(data)

    def _cfg_get(self, key: str, default: Any) -> Any:
        if isinstance(self.config, Mapping):
            return self.config.get(key, default)
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return getter(key, default)
        return default

    async def _save_task(
        self, event: AstrMessageEvent, snapshot: TaskSnapshot, prompt: str, model: str
    ) -> None:
        if not snapshot.task_id:
            return

        now = int(time.time())
        record = {
            "provider_id": snapshot.provider_id,
            "task_id": snapshot.task_id,
            "status": snapshot.status,
            "video_url": snapshot.video_url,
            "error_message": snapshot.error_message,
            "raw": snapshot.raw,
            "prompt": prompt,
            "model": model,
            "updated_at": now,
        }
        self._task_cache[snapshot.task_id] = record
        while len(self._task_cache) > self._TASK_CACHE_MAX:
            self._task_cache.popitem(last=False)

        await self._safe_put_kv(f"video_task:{snapshot.task_id}", record)
        await self._safe_put_kv(self._session_last_task_key(event), snapshot.task_id)

    async def _load_task(self, task_id: str) -> TaskSnapshot | None:
        cached = self._task_cache.get(task_id)
        if isinstance(cached, Mapping):
            return TaskSnapshot.from_dict(cached)

        stored = await self._safe_get_kv(f"video_task:{task_id}")
        if isinstance(stored, Mapping):
            self._task_cache[task_id] = dict(stored)
            return TaskSnapshot.from_dict(stored)
        return None

    async def _load_last_task_id(self, event: AstrMessageEvent) -> str:
        key = self._session_last_task_key(event)
        value = await self._safe_get_kv(key)
        if value:
            return str(value)
        return ""

    @staticmethod
    def _session_last_task_key(event: AstrMessageEvent) -> str:
        return f"video_last_task:{event.unified_msg_origin}"

    async def _safe_put_kv(self, key: str, value: Any) -> None:
        putter = getattr(self, "put_kv_data", None)
        if not callable(putter):
            return
        try:
            await putter(key, value)
        except Exception as exc:
            logger.warning(f"写入 KV 数据失败: key={key}, err={exc}")

    async def _safe_get_kv(self, key: str) -> Any:
        getter = getattr(self, "get_kv_data", None)
        if not callable(getter):
            return None
        try:
            return await getter(key)
        except Exception as exc:
            logger.warning(f"读取 KV 数据失败: key={key}, err={exc}")
            return None

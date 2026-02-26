# astrbot_plugin_video_generate_tool

面向 AstrBot 的视频生成插件：  
- 支持通过可配置的 API 映射接入 veo / sora / grok / seedance 等视频服务。  
- 支持 QQ 聊天内直接发起生成并回传视频（`Video` 组件 + URL 兜底）。  
- 提供可被 AI 自动调用的工具函数（`video_generate`、`video_query_status`）。

## 功能清单

- 命令：
  - `/video providers`：列出可用服务商
  - `/video gen <provider_id> <prompt>`：提交并等待视频结果
  - `/video status [task_id]`：查状态（省略 task_id 时查当前会话最近任务）
- AI 工具：
  - `video_generate(prompt, provider_id, model, duration, aspect_ratio, wait)`
  - `video_query_status(task_id)`
- 结果输出：
  - 成功：发送视频组件（可用时）+ 文本
  - 失败：返回错误状态与错误详情
  - 超时：提示继续用 `status` 查询

## 快速配置

在 AstrBot 插件配置中，给 `providers` 添加至少一个服务商，例如：

```json
{
  "default_provider_id": "veo",
  "providers": [
    {
      "__template_key": "openai_compatible_video",
      "provider_id": "veo",
      "base_url": "https://api.example.com",
      "api_key": "sk-xxxx",
      "model": "veo-3",
      "submit_path": "/v1/videos",
      "status_path_template": "/v1/videos/{task_id}",
      "task_id_field": "id",
      "status_field": "status",
      "output_url_field": "output[0].url",
      "error_field": "error.message"
    }
  ]
}
```

如果某服务商不是 OpenAI 兼容格式，只要它具备“提交 + 查询”能力，也可以通过路径映射接入。

## 依赖

本插件需要 `httpx`：

```bash
pip install -r requirements.txt
```

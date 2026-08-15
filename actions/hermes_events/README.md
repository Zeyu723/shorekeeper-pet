# Hermes 事件动作库存

这里为 Hermes 的 8 个 Hook 事件与 10 个 SSE/WebSocket 事件各保存了一份独立动作包。

## 入口文件

- `catalog.json`：事件到动作、优先级、回退状态及去重策略的总索引
- `inventory.png`：全部动作的静态库存预览
- `<action_id>/manifest.json`：单个动作的完整生命周期元数据
- `<action_id>/<action_id>.gif`：透明 GIF
- `<action_id>/preview.webp`：循环预览
- `<action_id>/frames/`：透明 PNG 帧序列

## 播放约定

- `playback: hold`：持续状态。收到重复事件时不得从第一帧重播；一直循环到后续终止事件切走。
- `playback: once`：过渡动作。播完后进入 `fallback_state`，除非期间收到了更新、优先级更高的事件。
- 数值更大的 `priority` 可以打断数值较小的动作。
- `response.output_text.delta` 和 `agent:step` 是高频事件，必须按 `coalesce_ms` 合并。
- 同时监听 Hook 与 SSE 时，应按 `session_id`、`run_id`、工具调用 ID 和 300ms 时间窗去重。

## 推荐接法

外部 Hook 通过本地 socket 通知桌宠；桌面前端可以直接消费 SSE/WebSocket。两者最终都转换成统一消息：

```json
{
  "event": "tool.started",
  "timestamp": 1786600000000,
  "session_id": "optional",
  "run_id": "optional",
  "payload": {}
}
```

桌宠收到消息后查找 `catalog.json`，切换到对应 `action_id`。

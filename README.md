# 守岸人桌宠 Shorekeeper Pet

<p align="center">
  <img src="assets/shorekeeper-laying.png" width="200" alt="守岸人桌宠" />
</p>

一个像素风的守岸人（鸣潮）桌面宠物，透明置顶浮窗、自由走动、气泡互动，并且**深度联动 Hermes Desktop**——她知道你在做什么，会随你的 Agent 工作状态切换动画，甚至能直接和你聊天。

> 个人学习用途的非官方同人项目。角色"守岸人"及《鸣潮》相关权利归其权利人所有，请勿将素材用于商业用途。

## ✨ 功能

### 桌宠本体
- 🐾 透明无边框窗口 + 像素动画（呼吸、待机、自由走动）
- 🖱️ 左键拖动移动 / 单击互动 / 双击聊天 / 右键菜单
- 📐 80% / 100% / 130% 三档缩放，切换保持底部中心锚点，多显示器与负坐标工作区自适应
- 🌙 总在最前、安静 5 分钟（隐藏全部窗口）、自定义浅冰蓝右键菜单（子菜单 + 防误关宽限期）

### Hermes Desktop 联动
- ⚡ **事件动画桥**：通过 runtime plugin 监听真实网关事件（`message.*`、`tool.*`、`error` 等），她随你的 Agent 工作状态切换动画——你跑工具她在旁围观，任务完成她开心
- 💬 **桌宠聊天**：双击她弹出输入气泡，消息经 Hermes Desktop 当前激活会话发送（`prompt.submit`），回复流式打字机回到气泡
- 🔁 **连续发送**：Enter 直接投递，网关自动排队串行执行，request_id 配对回复不串台
- 🫧 **智能气泡分流**：≤15 字走头顶短气泡；>15 字迁移到侧边长气泡——液态玻璃质感、动态高度（紧贴内容、8 行封顶）、滚轮滚动、点击固定、淡蓝 X 关闭

### 桌宠菜单
- 💬 打开聊天 / 🖥 打开 Hermes（唤起桌面 App）
- 自由走动、总在最前开关
- 大小子菜单（收起式）
- 安静 5 分钟 / 退出

## 🚀 安装与运行

### 依赖
- Windows 10/11
- Python 3.10+（含 Pillow）
- [Hermes Desktop](https://hermes-agent.nousresearch.com/docs)（联动与聊天功能需要）

### 启动

```bash
pip install Pillow
python app.py
```

或双击 `启动守岸人.bat`。

### Hermes 联动桥

桌宠与 Hermes Desktop 的桥接插件位于：

```
%LOCALAPPDATA%\hermes\desktop-plugins\shorekeeper-pet\plugin.js
```

Hermes Desktop 会热加载它。若桌宠动画不联动，在 Desktop 的 **Settings → Plugins** 中启用 **Shorekeeper Pet Bridge**。

> 注意：手动修改 plugin.js 后需重启桌宠进程才会重新握手。

### 验证真实事件

桌宠运行后：

```bash
curl http://127.0.0.1:51208/status
```

真实联动应看到 `receivedCount` 持续增加、`source` 为 `desktop-plugin`。

## 🏗️ 架构

```
Hermes Desktop (Electron)
    │  plugin.js (runtime plugin)
    │  ├─ host.onEvent('*') → 动画事件
    │  └─ outbox 轮询 → prompt.submit → 回复桥接
    ▼
HTTP 127.0.0.1:51208
    ▼
app.py (Tkinter)
    ├─ 透明窗桌宠 + 动画引擎（actions/ 帧包）
    ├─ BubbleChat 输入气泡（连续发送 + request_id）
    ├─ SideBubble 侧边长气泡（液态玻璃）
    └─ PetMenu 自定义右键菜单
```

动作帧包放在 `actions/`，每个包含 `manifest.json`（state/pose/priority/cooldown 等元数据）与 `frames/` 帧序列；`tools/import_video_action.py` 提供视频素材导入管线。

## 🧪 测试

```bash
# Python 测试（Tk 交互、气泡、缩放、窗口状态、HTTP 桥）
python tests/test_bubble_chat.py
python tests/test_side_bubble.py
python tests/test_pet_scaling.py
python tests/test_pet_window_state.py
python tests/test_chat_http_bridge.py
python tests/test_http_diagnostics.py

# Node 桥接测试
node tests/test_desktop_plugin_bridge.mjs
node tests/test_chat_active_session_bridge.mjs
```

## 📦 打包

双击 `打包EXE.bat`，输出 `dist\守岸人桌宠.exe`（PyInstaller）。

## 🗺️ Roadmap

- [ ] 行为导演层：时间 / Hermes 状态 / 互动记忆驱动的自主行为
- [ ] 环境层：昼夜色调、天气粒子、场景道具
- [ ] 打字频率感知陪跑（默认关闭，只统计频率不记录内容）

## 📄 License

MIT License（代码部分）。

角色"守岸人"（Shorekeeper）来自游戏《鸣潮》（Wuthering Waves），版权归 Kuro Games 所有。本项目为非官方同人创作，仅供个人学习使用。

角色外观参考：[《鸣潮》官方角色介绍](https://wutheringwaves.kurogames.com/en/main/news/detail/1419)

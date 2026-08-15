---
title: 真实 UI E2E 测试方法论
source: shorekeeper-pet 项目实战提炼
date: 2026-08-15
tags: [e2e, testing, windows, automation, ai-lab]
---

# 真实 UI E2E 测试方法论 — 从守岸人桌宠项目提炼

> 来源：shorekeeper-pet 项目实战（2026-08）。一套针对 Windows 桌面应用的端到端测试方法，由 GPT 模型在项目中设计实现，Zeyu 主持验收。适用于任何"有真实 GUI + 有本地数据 + 需要回归验证"的桌面应用。

## 一、为什么需要真实 E2E

单元测试验证"代码逻辑对"，真实 E2E 验证"用户双击时它真的能用"。两者不可互相替代：

| 层级 | 验证内容 | 抓得住的 bug |
|---|---|---|
| 单元测试（mock） | 函数逻辑 | 算法错、状态机错 |
| 隔离集成测试 | 模块间接口 | 参数传递错、序列化错 |
| **真实 E2E** | 完整链路真机跑 | 窗口句柄变了、插件热重载断了、DPI 缩放裁切了、时序竞态 |

本项目真实案例：6 套单元测试全绿，但真机上长气泡弹不出——因为插件热重载后没有重新轮询 outbox。**只有真机能抓住这类问题。**

## 二、核心原则

### 1. 驱动真实 UI，不做任何 mock
E2E 的操作路径必须与人类用户完全一致：真的移动鼠标、真的双击、真的打字、真的滚动。不直接调用内部函数、不注入事件到代码层。

### 2. 用窗口特征定位，不用坐标硬编码
屏幕坐标会因分辨率/DPI/窗口位置漂移而失效。用 Win32 API 的窗口属性做定位：

```python
EnumWindows(callback, 0)          # 枚举所有顶层窗口
GetWindowThreadProcessId(hwnd)    # 按进程 PID 过滤——只看被测应用的窗口
GetWindowTextW(hwnd)              # 按标题识别窗口角色
GetWindowRect(hwnd)               # 拿到实时坐标再操作
```

**窗口分类策略**（给无标题栏的自绘窗口）：
- 按标题精确匹配：`title == "守岸人桌宠"` → 角色主窗
- 按尺寸区间匹配：`120 ≤ width, 45 ≤ height ≤ 180` → 输入气泡
- 按标题关键词：`"长回复" in title` → 侧边长气泡

### 3. 操作前先校验目标存在
每个动作前用谓词轮询等待窗口出现，而不是 sleep 固定时长：

```python
def wait_for(predicate, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(f"timeout waiting for {label}")
```

固定 sleep 要么浪费时间、要么竞态失败；条件等待又快又稳。

### 4. 点击位置用比例，不用绝对坐标
自绘角色的大部分区域是透明像素（点击穿透）。对角色主窗，点击命中区用窗口尺寸的比例定位：

```python
click_x = rect.left + round(width * 0.32)   # 命中角色身体
click_y = rect.top + round(height * 0.52)
```

比例值通过一次截图 + 视觉确认校准，之后任何缩放档位（80/100/130%）都自动适配。

## 三、防假阳性三件套（精髓）

E2E 最大的敌人不是失败，是**假成功**——测试通过但实际没工作。

### 1. Fresh-ID 基线
操作前记录数据库当前最大 ID，之后只认基线之后的新记录：

```python
baseline = max_message_id()          # 发送前
send_from_pet(prompt)
# 轮询：只匹配 id > baseline 的新消息
"SELECT ... WHERE role='user' AND id > ? AND content LIKE ?"
```

防止把历史同文案消息误判成本轮结果（回归测试反复跑同一 prompt 时必踩的坑）。

### 2. 会话归属校验
查到新消息后，必须验证它落在预期会话里：

```python
if user_row["session_id"] != TEST_SESSION_ID:
    raise AssertionError(f"提交到了错误的会话: {user_row['session_id']}")
```

防止"消息发出去了，但发去了别的地方"的静默错路由。

### 3. 完成态校验
回复存在 ≠ 回复完成。检查 `finish_reason == "stop"`（或按业务定义接受态），防止把被打断的半截回复当成功。

> 实战修正：连续发送场景下，排队中的下一条消息会打断上一条的收尾，使 finish_reason 停在 None——此时应按业务语义放宽为 `in (None, "stop")`。**断言的严格度要跟着业务真实行为校准，不是越严越好。**

## 四、验证矩阵设计

一次 E2E 不只测"能不能用"，而是把关键行为分支串成一条故事线：

```
激活宿主应用 → 选中测试会话
→ 双击桌宠，发短消息
   断言：回复在头顶泡（DB 确认）+ 无侧边窗出现
→ 发 20 行长消息
   断言：侧边泡出现 + 不与角色重叠 + 截图
→ 等 8 秒 → 滚轮一下 → 再等 8 秒（超过原始 15 秒截止）
   断言：窗口仍在 → 证明滚动重置了自动关闭计时
→ 点击正文 → 等 16 秒
   断言：窗口仍在 + 关闭按钮可见 → 证明"固定"生效
→ 点击关闭 X
   断言：窗口消失
→ 对比前后 /status 的动画事件计数
   断言：事件计数增长 → 证明联动链路活着
```

每个断言都对应一条产品需求，跑完 = 一次完整回归。

## 五、截图取证 + 视觉复核

自动化断言验证"事实"，截图留给"审美"：

```python
ImageGrab.grab(bbox=窗口坐标扩大12px, all_screens=True).save(path)
```

- 窗口坐标从 GetWindowRect 实时取，不猜
- all_screens=True 支持多显示器负坐标
- 事后用视觉模型或人眼复核：布局重叠、裁切、留白比例——这些"看着不对"的问题没有程序化断言

**分工哲学：程序答事实，眼睛答美感。**（自动化能告诉你 padding 是多少，不能告诉你丑不丑。）

## 六、已知坑与对策

| 坑 | 对策 |
|---|---|
| 窗口句柄（HWND）跨重启变化 | 不要硬编码；每次运行前用标题/PID 现查 |
| git-bash 里 taskkill //F 转义失败 | 用 `cmd.exe /c "taskkill /F /PID x"` |
| React 受控输入填不进值 | 用 nativeInputValueSetter + 派发 input 事件 |
| 15 秒自动关闭的瞬态窗口 | 等待谓词 + 在时间窗内完成断言；测计时就配合滚动续命来延长窗口寿命 |
| 被测应用依赖宿主（如 Hermes Desktop） | E2E 开头先激活宿主、选中固定测试会话；会话 ID 也要校验 |
| 后台进程跑了旧代码 | 改代码后必须杀进程重启，"测试全绿但真机是旧版本"是经典陷阱 |

## 七、技术栈清单（Windows 桌面应用）

| 组件 | 用途 | 备注 |
|---|---|---|
| ctypes + user32 | 窗口枚举/坐标 | 零依赖，Win32 原生 |
| pywinauto | 鼠标/键盘/UIA | mouse.double_click(coords=)、send_keys() |
| PIL.ImageGrab | 截图取证 | all_screens=True 处理多屏 |
| sqlite3 | 直读应用数据库断言 | 前提：知道库结构 |
| urllib | 查被测应用 HTTP 诊断口 | 桌宠暴露 /status |

## 八、一句话总结

> **E2E = 像用户一样操作，像开发者一样断言，像审计一样防假阳性。**
> 窗口特征定位代替坐标硬编码，fresh-ID 基线代替盲目匹配，完成态校验代替存在性检查，截图复核补全自动化盲区。

---
title: 调试 Hermes Desktop 插件的 N 层洋葱
source: shorekeeper-pet 欢迎页幽灵会话 bug 三层挖掘实录（2026-08-16 凌晨）
date: 2026-08-16
tags: [ai-lab, debugging, hermes, desktop-plugin, lessons-learned]
---

# 调试 Hermes Desktop 插件的 N 层洋葱

> 一个 bug，三层假根因，最后真凶是 API 返回值里两个长得像的字段。
> 这是 shorekeeper-pet 项目"欢迎页幽灵会话"的完整挖掘记录——不是事后总结，
> 是凌晨四点真刀真枪踩出来的。

## 症状

用户在 Desktop 欢迎页（New Session，还没发过消息）对桌宠说话：
主界面 **loading 转一下又弹回欢迎页**，会话没创建成功，但消息其实发出去了。
更诡异的是：重启后那个会话**从列表里消失了**。

## 三层洋葱

### 第 1 层：以为是热重载（误诊）

插件改完 plugin.js，Desktop 会热重载插件。第一反应：
"是不是热重载没加载新代码？"

**证伪**：日志里明确看到新代码的 `openSession` 被调用了——界面 loading 就是它触发的。
代码在跑，但被什么东西**拒绝**了。

> 教训：有 loading 动画 = 前端路由确实尝试过导航。"没反应"和"试了但被弹回"
> 是两种完全不同的 bug，先分清是哪种。

### 第 2 层：以为是空会话被清理（半对）

发现网关有会话收割（reap）机制：断连的孤儿会话会被 grace-window 收走。
日志里 `reaped_sessions=0`——收割没发生。
但会话确实消失了，谁删的？

顺藤摸到 Desktop 的自愈逻辑（`use-session-actions`）：resume 一个「gone」的会话时，
如果本地消息为空、且这个 id **不是本次运行中 App 自己创建的**（`createdThisRun`
集合），就执行 `startFreshSessionDraft(true)` —— **静默弹回新会话页，不留任何提示**。

这就是"弹回"的直接机制。但为什么 resume 会「gone」？

### 第 3 层：真根因——session.create 的双 id 语义（文档写过，没踩过不知道疼）

翻网关源码 `session.create` 的返回值，发现它返回**两个 id**：

| 字段 | 样例 | 用途 |
|------|------|------|
| `session_id` | `a8bdcc27` | 运行时短 id，**prompt.submit 用这个** |
| `stored_session_id` | `20260816_043516_8895ce` | 落库 id，**openSession / 路由跳转用这个** |

我们的插件把 `session_id`（运行时 id）喂给了 `openSession` →
界面导航到 `#/a8bdcc27` → resume 查无此会话 → 404 → 「session gone」→
自愈逻辑静默弹回。

**还有第二层坑叠在上面**：网关注释写明 `session.create` **故意不落库**
（防止每次启动留垃圾空会话），DB 行要等第一条 `prompt.submit` 才写。
所以就算 id 传对了，`create → openSession` 顺序跳转一样 404——
必须 `create → submit（落库）→ openSession`。

Desktop 工程指南里其实警告过「session 双身份混淆」。文档写了，
但没在生产代码里踩过，读到那行字时根本不知道它疼在哪。

## 修法

```
create（解析双 id）
  → prompt.submit（用运行时 id，同时触发落库）
  → openSession（用 stored id，此刻 DB 行已存在，路由可解析）
```

欢迎页 draft 路径用 `welcomeStoredId` 变量暂存，submit 成功后消费。
会话消失恢复路径同样改双 id + 先 submit 后 openSession。

测试：mock 网关返回双 id，断言 submit 拿运行时 id、openSession 拿 stored id
（`test_chat_ghost_session.mjs`，168 行）。真机验收：欢迎页说话 → 自动跳转新会话。

## 为什么难：这个 bug 的三个陷阱属性

1. **静默失败**：弹回不带任何错误提示，UI 层面"看起来什么都没发生"
2. **跨界**：症状在 UI（弹回），根因在 API 语义（双 id）+ 数据库时机
   （延迟落库），中间隔着三层架构
3. **文档免疫力**：工程指南警告过，但警告太抽象，没踩过无法对应到
   "loading 弹回"这个具体症状

## 方法论蒸馏

1. **日志 > 猜测**：每剥一层都是日志证据推着走（openSession 被调 →
   reap 没发生 → resume 404）。没有日志的调试是玄学。
2. **区分"没反应"和"被弹回"**：有 loading 动画说明流程走了一半，
   找"谁拒绝了它"，而不是"为什么没触发"。
3. **读返回值的每个字段**：API 返回两个字段长得像、用途不同，
   是分布式系统的经典坑（此处是 `session_id` vs `stored_session_id`）。
4. **查写入时机**：创建动作 ≠ 落库时机。延迟落库的 API，
   读操作必须排在写入动作之后。
5. **洋葱要剥到底**：第一层"修好了"（重启加载新代码）第二层又弹回，
   假根因的特征是——修复后症状变化但没消失。
6. **静默自愈是调试黑洞**：框架"帮你恢复"的逻辑（startFreshSessionDraft）
   会吞掉错误现场。找到它，就能反推触发条件。
7. **写测试固化 id 语义**：修复后专门写了断言双 id 用途的测试——
   让下一个改这段代码的人（或模型）无法再把 id 传反。

## 给写插件的人

- `session.create` 返回的 `session_id` 只用于 `prompt.submit`；
  要让 UI 跳转到新会话，必须用 `stored_session_id`，且在 submit 之后。
- Desktop 的静默自愈（弹回欢迎页）意味着：**任何路由失败都表现为"没反应"**，
  出问题时先查 resume/404 日志，别怀疑自己的 click handler。

---

*Zeyu 主导调试与验收，GLM-5.2/5.3 会话接力挖掘，守岸人整理成文。
案发记录：Hermes session `20260816_041922_ab1e6b`。*

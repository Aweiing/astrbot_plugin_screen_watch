# 窗口监听（astrbot_plugin_screen_watch）

盯住电脑上任意一个窗口，画面一变就通知你 —— 不用一直守着屏幕。

触发之后走的是 AstrBot 的正常对话流程：人设、记忆、工具都在，由模型判断值不值得说、该说什么。

**仅支持 Windows**（依赖 `PrintWindow` 抓窗口）。

---

## 一、它能干什么

- 按窗口标题找到目标，定时截取该窗口画面（被遮挡也能抓）
- 前后两帧比对，按「变化面积占比」判断是否值得上报
- 触发后写一条事件，交给 AstrBot 处理
- 内置看护：进程挂了自动重拉，清单里删掉的任务会被清理

适合这些场景：

- 盯聊天窗口，有人回话就叫你
- 盯跑批 / 编译 / 下载页面，跑完了说一声
- 盯价格、库存、抢票页面，一变就提醒
- 自己写程序往 `task/wait/` 里丢 json，插件同样会处理

---

## 二、安装

1. 把插件目录放进 `AstrBot/data/plugins/`
2. 面板「插件」里启用「窗口监听」
3. 到配置里填 `target_session`（**必填**），格式 `平台:消息类型:会话ID`，例如 `aiocqhttp:FriendMessage:123456`

填不对就没有任何推送，也不会有报错弹窗——它只会写一行日志。

---

## 三、配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `target_session` | 空 | 推送到哪个会话，必填 |
| `threshold` | `0.02` | 变化阈值，低于它直接忽略 |
| `prompt_template` | 见下 | 投出去那句话长什么样 |
| `poll_interval` | `3` | 扫 `task/wait/` 的间隔，秒 |
| `reconcile_interval` | `30` | 对齐监听进程的间隔，秒 |

默认模板：

```
【系统通知】
任务：{task}
目的：{purpose}
时间：{ts}
变化：{ratio}
内容：{text}
```

可用变量：`{task}` `{purpose}` `{text}` `{ratio}` `{ratio_raw}` `{ts}` `{extra}` `{notify}` `{filename}` `{raw}`。全部按字符串处理，缺的填空。

---

## 四、怎么加一路监听

所有监听都写在 `data/plugin_data/astrbot_plugin_screen_watch/watchers.json` 里，一个数组：

```json
[
  {
    "task": "qq",
    "window": "QQ",
    "interval": 3,
    "threshold": 0.0,
    "notify": "auto",
    "purpose": "盯着 QQ 的聊天窗口，有人回话就告诉我"
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `task` | 是 | 任务名，短、唯一 |
| `window` | 是 | 窗口标题，完全匹配优先，其次子串 |
| `purpose` | 是 | 这个任务要干什么，会一路传到模型面前 |
| `interval` | 否 | 截图间隔秒，默认 3 |
| `threshold` | 否 | 变化阈值，默认 0（有任何变化就报） |
| `notify` | 否 | `auto` / `yes` / `no`，默认 auto |
| `enabled` | 否 | 写 `false` 等于暂停，进程会被清掉，配置留着 |

改完最多等 30 秒生效。要看当前窗口有哪些，可以跑一次：

```
python watch.py --list-windows
```

想停一路，把它从清单里删掉。

---

## 五、事件长什么样

watcher 写进 `data/plugin_data/astrbot_plugin_screen_watch/wait/` 的 json：

```json
{
  "task": "qq",
  "text": "窗口「QQ」有变化，约 4.3%",
  "ratio": 0.043,
  "ts": "2026-11-11 15:02:11",
  "notify": "auto",
  "purpose": "盯着 QQ 的聊天窗口，有人回话就告诉我"
}
```

`task`、`text`、`purpose` 是必填，缺了整条进 `fail/`。

`notify` 决定模型怎么做：

- `no` —— 办完事，但别推送
- `yes` —— 办完并把结果推出来
- `auto` —— 自己判断值不值得说

**阈值优先**：`ratio` 低于 `threshold` 的，连模型都不用看，直接归档。

---

## 六、它不做什么

- 不做 OCR，不知道画面里写了什么字——想知道，让模型收到后自己看截图
- 不自己收钱、不做实名
- 启动和关闭时不清场，只管让现实对齐清单
- 失败不重试，出错即归档

---

## 七、出问题

| 现象 | 去看 |
|---|---|
| 加了监听没反应 | `watchers.json` 格式对不对；`bridge.log` 有没有「已拉起监听」 |
| 一直报 | 阈值太低，或窗口里有动画、视频 |
| 完全不报 | 窗口最小化了；或者 `enabled` 是 false |
| 收不到推送 | `target_session` 有没有填对 |
| 文件进了 done 但没消息 | 被阈值或 `notify: no` 拦下，或模型判断不值得说 |

日志在 `data/plugin_data/astrbot_plugin_screen_watch/bridge.log`。

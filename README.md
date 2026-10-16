# QQ 人格管理插件（qq-persona-manager）

给 MaiBot（麦麦）用的插件，在 QQ 里用两条指令管理麦麦的人格：

| 指令 | 作用 |
|---|---|
| `/人格查询` | 把麦麦当前人格拆成 `#人设` / `#行为风格` / `#表达风格` 三段，以 **QQ 合并转发（聊天记录卡片）** 的形式发到当前的聊天流。**默认只在私聊生效**，群聊需打开开关 |
| `/人格修改 需求` | 用麦麦自己的 `replyer` / `planner` 模型判断这条需求属于哪一类，与原文融合后**直接热更新**，改完立即生效，不用重启 |
| `/人格帮助` | 显示指令说明（附带的，方便你随时查） |

**白名单机制**：只有白名单里的 QQ 号能触发上面这些指令。非白名单用户发指令时，麦麦会在聊天流里回复 **"您无权进行此类操作"**。

---

## 一、先确认环境

| 项目 | 要求 |
|---|---|
| MaiBot 版本 | **v1.3.0 及以上**（新版插件 SDK 体系；`Config` 目录里应该能看到 `config/bot_config.toml`） |
| 插件 SDK | `maibot-plugin-sdk` 2.x。MaiBot 自带依赖里通常已经有了 |
| 模型 | `model_config.toml` 里 `[model_task_config.replyer]` 或 `[model_task_config.planner]` **至少有一个配了能用的模型**（插件默认先试 replyer，不行自动改用 planner） |
| QQ 适配器 | 需要支持合并转发的实现（NapCat / Lagrange 等 OneBot 实现都支持）。万一失败，插件会自动退回普通文本发送 |

> 怎么确认 SDK 在不在？如果插件加载时报 `ModuleNotFoundError: No module named 'maibot_sdk'`，在 **MaiBot 的目录**里执行一次：
> - 用 uv 装的：`uv add maibot-plugin-sdk`
> - 普通 pip 装的：`pip install maibot-plugin-sdk`

---

## 二、安装（4 步）

### 第 1 步：把插件文件夹放进去

**方式一：直接复制**

把整个 `qq-persona-manager` 文件夹复制到 MaiBot 的 `plugins` 目录下。

**方式二：用 git 克隆（方便以后更新）**

在 MaiBot 根目录执行：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git plugins/qq-persona-manager
```

以后更新只要：

```bash
cd plugins/qq-persona-manager && git pull
```

无论用哪种方式，最终结构必须是：

```
MaiBot/
├── config/
├── data/
└── plugins/
    └── qq-persona-manager/      ← 就是这个文件夹
        ├── _manifest.json
        ├── plugin.py
        ├── README.md
        └── LICENSE
```

⚠️ 是复制**文件夹本身**，不要把里面的两个文件散着丢进 `plugins/`。

### 第 2 步：填白名单（重要，不填谁都用不了）

启动 MaiBot，然后二选一：

**方式 A（推荐，点点鼠标就行）**
1. 浏览器打开 MaiBot 的 WebUI：`http://127.0.0.1:8001`
2. 左侧进入 **插件** 页 → 找到 **QQ 人格管理（查询 / 修改）**
3. 打开 **白名单** 卡片，在 `whitelist` 里把你的 QQ 号加进去，保存

**方式 B（直接改文件）**
插件第一次加载后会自动生成 `plugins/qq-persona-manager/config.toml`，用记事本打开，改成：

```toml
[access]
whitelist = ["123456789", "987654321"]
deny_message = "您无权进行此类操作"
notify_on_deny = true
```

- `whitelist`：能使用指令的 QQ 号，**写纯数字或用 `qq:123456789` 都行**，多个用英文逗号隔开
- **白名单为空时，所有人都会被拒绝**（这是故意的安全设计）

填完保存，插件会自动热重载，不用重启 MaiBot。

### 第 3 步：确认模型

打开 `config/model_config.toml`，确认 `replyer` 和 `planner` 里至少有一个配了模型：

```toml
[model_task_config.replyer]
model_list = ["你的模型名"]
```

### 第 4 步：测试

在 QQ 里，用白名单里的号发：

```
/人格帮助
```

收到指令说明就说明插件跑通了。

---

## 三、使用

### 查询人格

```
/人格查询
```

> ⚠️ **默认只在私聊生效。** 群聊里用需要先打开"群聊执行开关"，见下一节。

你会收到一张合并转发卡片，里面三段：

- `#人设`：她是什么样的人
- `#行为风格`：什么时候说话、什么时候不说话
- `#表达风格`：怎么说话、用什么语气

> **只读这三段。** 备用的人格会被完整忽略 —— 也就是 `[personality]` 段里的
> `multiple_reply_style`（备用表达风格）和 `multiple_probability`（随机启用备用风格的概率），
> 查询不显示、修改也不会碰，全程逐字节保持原样。

### 群聊执行开关（只针对 /人格查询）

`/人格查询` 会把人格内容发出去，人多的地方容易刷屏，所以**默认只在私聊生效**。

**打开方式**：WebUI →「插件」→「QQ 人格管理」→ **限用范围** 卡片 → 把 **`query_allow_group`** 打开 → 保存。

也可以直接改 `plugins/qq-persona-manager/config.toml`：

```toml
[scope]
query_allow_group = true    # false（默认）= 只有私聊能用；true = 群聊也能用
```

**开关没打开时**，白名单用户在群聊里发 `/人格查询`，会收到：

> 暂不支持群聊使用此功能，请到插件页开启此功能群聊执行开关

**判定顺序**是：① 白名单 → ② 群聊开关。所以非白名单用户在群里发指令，收到的是"您无权进行此类操作"，不会泄露这个开关的存在。

> 这个开关**只管 `/人格查询`**。`/人格修改` 和 `/人格帮助` 不受它影响，群聊私聊都能用。

### 修改人格

```
/人格修改 说话加上喵~
```

或者同时提多个要求：

```
/人格修改 说话加上喵~，另外晚上11点以后就别发言了
```

会发生什么：

1. 插件的**模型**先判断需求属于哪一类（或哪几类）
   - **人设** = 她是什么样的人
   - **行为风格** = 什么时候说话、什么时候不说话
   - **表达风格** = 怎么说话、说什么话
2. 把需求**和对应分类的原文融合**，生成完整的新文本（不会丢掉你原来写的内容）
3. 写回 `config/bot_config.toml`，MaiBot 检测到文件变化后自动热重载
4. 在聊天流里回复：**人格已热更新成功**

如果需求跟这三类都不沾边（比如让它去执行一个外部任务、发乱码），会回复：

> 人格热更新失败，当前需求不匹配

**只发 `/人格修改` 不带需求**时，会回复：

> 请在指令后面带上修改需求

并且什么都不改。

> 人设改完只影响**新生成的回复**，已经发出去的消息不会变。想确认效果，连续聊几轮看风格。

---

## 四、插件配置项说明

WebUI 里能改，也可以直接编辑 `plugins/qq-persona-manager/config.toml`。

### `[access]` 白名单与权限

| 字段 | 默认 | 说明 |
|---|---|---|
| `whitelist` | `[]` | 允许用指令的 QQ 号列表 |
| `deny_message` | `您无权进行此类操作` | 无权限用户收到的提示 |
| `notify_on_deny` | `true` | 关掉后无权限用户的指令会被静默忽略（不回复） |

### `[scope]` 限用范围

| 字段 | 默认 | 说明 |
|---|---|---|
| `query_allow_group` | `false` | **群聊执行开关**（只作用于 `/人格查询`）。`false` = 只在私聊生效；`true` = 群聊也能用 |
| `query_group_disabled_message` | `暂不支持群聊使用此功能，请到插件页开启此功能群聊执行开关` | 开关关闭时，白名单用户在群聊发 `/人格查询` 收到的提示 |
| `treat_unknown_scope_as_group` | `false` | 万一判断不出消息来自群聊还是私聊，按哪类处理。默认按私聊（不影响正常使用）；若你的适配器上报信息不全、群聊被误放行，改成 `true` |

### `[model]` 模型

| 字段 | 默认 | 说明 |
|---|---|---|
| `task_priority` | `["replyer", "planner"]` | 按顺序尝试的模型任务名。默认先用 replyer，不行自动换 planner |
| `temperature` | `0.2` | 分类温度，建议 0.1~0.3 |
| `max_tokens` | `4096` | 单次生成长度上限 |
| `timeout_seconds` | `120` | 等模型的最长秒数，超时换下一个任务名 |

### `[message]` 对外文案

`cat_personality` / `cat_behavior_style` / `cat_reply_style`（三段标题）、`need_requirement_message`、`success_message`、`mismatch_message`、`error_prefix`。想换说法改这里。

### `[behavior]` 运行行为

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 插件总开关 |
| `dry_run` | `false` | **演练模式**：打开后只把判定结果发到聊天流，**不写入配置文件**。第一次用建议先开一次看看效果 |
| `backup` | `true` | 写入前备份 `bot_config.toml`，**强烈建议保持开启** |
| `backup_keep` | `20` | 最多保留多少个备份 |
| `config_path` | `""` | `bot_config.toml` 的绝对路径。留空自动定位，一般不用填 |
| `max_requirement_length` | `500` | 单次需求最大字数 |

---

## 五、工作原理（想了解再看）

- 插件跑在 MaiBot 的独立 Runner 子进程里，只能使用 `maibot_sdk` 和 Python 标准库，**不 import 主程序**。
- 查人格时优先通过 `ctx.config.get("personality")` 读全局配置，读不到就直接解析 `config/bot_config.toml`。
- 改人格时用**文本级手术**：只替换 `[personality]` 段里 `personality` / `behavior_style` / `reply_style` 三个键的值，**其余内容、缩进、注释、别的段落一字不动**。
- 写盘前会用 `tomllib` 做两道校验：① 整个文件语法能解析；② 目标字段确实变成了新值。**任何一道不过就中止，不会把配置写坏。**
- 写盘前先把原文件备份到 `data/plugins/local.qq-persona-manager/backups/`。
- MaiBot 用 `watchfiles` 监听 `config/bot_config.toml`，检测到变化后热重载人格配置 —— 所以改完立即生效，不用重启。
- 判断消息来自群聊还是私聊时，先用消息对象里的 `message_info.group_info`（群聊才有）、`is_group_message` 等字段；万一这些都没有，就向宿主查聊天流列表，用 `stream_id` 反查 `chat_type`（结果缓存 5 分钟）。两层都拿不到才按 `treat_unknown_scope_as_group` 回退。

---

## 六、出问题了？

| 现象 | 原因 & 处理 |
|---|---|
| 发指令完全没反应 | ① 白名单没填 → 所有人被拒（但这时应该会收到"您无权…"，如果连这个都没有就往下看）；② 插件没加载，去 WebUI 插件页看状态；③ 指令必须**以 `/` 开头**且单独成句 |
| 提示"没有权限"（英文/系统口吻） | 这是 MaiBot 主程序的权限提示，不是本插件的。本插件的提示是中文"您无权进行此类操作"。检查是否和别的插件命令名撞了 |
| 插件加载失败 | 看 MaiBot 日志。常见：`manifest_version` 不是 2、`maibot_sdk` 没装、MaiBot 版本低于 1.3.0 |
| `/人格查询` 收到的是普通消息不是卡片 | 你的 QQ 适配器不支持合并转发，插件已自动降级成普通文本。功能不受影响 |
| 群里发 `/人格查询` 回"暂不支持群聊使用此功能…" | **正常行为**，群聊执行开关默认关闭。去插件配置页把「限用范围 → `query_allow_group`」打开 |
| 群里发 `/人格查询` 完全没反应 | 先确认白名单里有你；再看是不是 `notify_on_deny` 被关掉了 |
| 群聊明明开了开关却被拦 | 说明场景判断出错，把「限用范围 → `treat_unknown_scope_as_group`」打开即可；同时把 MaiBot 日志里带 `群聊执行开关` 或 `无法判断消息场景` 的那几行发我 |
| `/人格修改` 回复"模型调用失败" | `replyer` 和 `planner` 都没配可用模型，或都超时了。去 `model_config.toml` 检查 |
| `/人格修改` 回复"不匹配"但你觉得该匹配 | 需求写具体点，比如把"改改说话方式"换成"说话句尾加喵~"。临时把模型的 `temperature` 调到 0.1 会更稳 |
| 改完感觉没变化 | 人设只影响新回复，多聊几轮；确认日志里出现了"配置重载成功" |
| 想回滚 | 从 `data/plugins/local.qq-persona-manager/backups/` 里挑一个 `bot_config.<时间戳>.toml`，改回 `config/bot_config.toml` |

---

## 七、安全提醒

- 插件会**写你 MaiBot 的配置文件**。`backup` 默认开着，请别关。
- 第一次用建议先打开 `dry_run`，看几轮判定结果对不对，再关掉正式用。
- 白名单请只加你信得过的人 —— 拿到权限的人可以任意改写麦麦的人格。
- 备份目录：`MaiBot/data/plugins/local.qq-persona-manager/backups/`

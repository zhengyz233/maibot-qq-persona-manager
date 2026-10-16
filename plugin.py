"""QQ 人格管理插件 —— /人格查询 与 /人格修改

适配 MaiBot v1.3.x 新版插件 SDK（maibot-plugin-sdk 2.x）。

三个能力：
1. 白名单鉴权：只有白名单里的 QQ 号能触发指令，其他人收到"您无权进行此类操作"。
2. /人格查询：把当前人格拆成 #人设 / #行为风格 / #表达风格 三段，
   以 QQ 合并转发（聊天记录卡片）的形式发送到指令所在的聊天流。
   默认【只在私聊生效】；群聊需要到插件配置页打开"群聊执行开关"
   （scope.query_allow_group），未打开时白名单用户在群聊发指令会收到
   "暂不支持群聊使用此功能，请到插件页开启此功能群聊执行开关"。
3. /人格修改 需求：用 MaiBot 的 replyer / planner 模型判断需求属于哪一类
   （人设 / 行为风格 / 表达风格），与当前对应分类的原文融合后，
   直接写回 config/bot_config.toml，由 MaiBot 的文件监视器完成热重载。

注意：本插件不 import MaiBot 主程序（插件运行在独立 Runner 子进程里，
只能使用 maibot_sdk 与 Python 标准库）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterable

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

#: 三个分类 -> bot_config.toml 中 [personality] 段的字段名。
#:
#: 刻意只包含这三个字段：MaiBot 的 [personality] 段里另有
#: multiple_reply_style（备用表达风格）与 multiple_probability（随机启用备用风格的概率），
#: 它们属于"备用的人格"，本插件【查询不显示、修改不写入】，全程保持原样。
CATEGORY_FIELDS: dict[str, str] = {
    "personality": "personality",
    "behavior_style": "behavior_style",
    "reply_style": "reply_style",
}

#: 明确声明永不触碰的字段，仅用于日志/断言可读性
IGNORED_PERSONALITY_FIELDS: tuple[str, ...] = (
    "multiple_reply_style",
    "multiple_probability",
)

#: 分类顺序（固定为 人设 / 行为风格 / 表达风格）
CATEGORY_ORDER: tuple[str, ...] = ("personality", "behavior_style", "reply_style")

#: 模型返回的多种写法 -> 标准分类名
CATEGORY_ALIASES: dict[str, str] = {
    "personality": "personality",
    "persona": "personality",
    "character": "personality",
    "identity": "personality",
    "人设": "personality",
    "人格": "personality",
    "身份": "personality",
    "behavior_style": "behavior_style",
    "behavior": "behavior_style",
    "behaviour_style": "behavior_style",
    "behaviour": "behavior_style",
    "行为风格": "behavior_style",
    "行为": "behavior_style",
    "行动风格": "behavior_style",
    "reply_style": "reply_style",
    "reply": "reply_style",
    "expression_style": "reply_style",
    "expression": "reply_style",
    "style": "reply_style",
    "表达风格": "reply_style",
    "表达方式": "reply_style",
    "说话风格": "reply_style",
}

COMMAND_QUERY = "人格查询"
COMMAND_UPDATE = "人格修改"
COMMAND_HELP = "人格帮助"

#: 消息来源场景
SCOPE_GROUP = "group"
SCOPE_PRIVATE = "private"
SCOPE_UNKNOWN = "unknown"

#: 从聊天流列表反查场景时的缓存有效期（秒），聊天流类型不会变，缓存久一点无所谓
STREAM_SCOPE_CACHE_TTL = 300.0

#: 默认的群聊禁用文案
DEFAULT_GROUP_DISABLED_MESSAGE = "暂不支持群聊使用此功能，请到插件页开启此功能群聊执行开关"

PATTERN_QUERY = r"^[/／]\s*人格查询\s*$"
PATTERN_UPDATE = r"^[/／]\s*人格修改(?:\s+[\s\S]*)?$"
PATTERN_HELP = r"^[/／]\s*人格帮助\s*$"

#: 上面三条正则的编译版本，用于在回调里做「指令是否真的来自本人正文」的二次判定
QUERY_RE = re.compile(PATTERN_QUERY)
UPDATE_RE = re.compile(PATTERN_UPDATE)
HELP_RE = re.compile(PATTERN_HELP)

#: 引用（回复）类消息段的类型名。
#:
#: ⚠️ 这类段携带的是「被引用消息的原文」，而 MaiBot 在
#: ``SessionMessage.process()`` 里会把 ReplyComponent 的正文**原样**拼进
#: ``processed_plain_text`` 的最前面（``process_reply_component`` 直接
#: ``return component.target_message_content``，没有任何包裹），随后
#: ``find_command_by_text`` 又用不带结尾约束的 ``text.startswith(alias)``
#: 做别名匹配 —— 于是「别人引用你发过的指令消息」会被误判成他本人在发指令。
#: 插件无法修改宿主行为，只能在这里做二次判定把它拦掉。
QUOTE_SEGMENT_TYPES = frozenset(
    {
        "reply",
        "quote",
        "reference",
        "reply_message",
        "quote_message",
    }
)

#: 从消息原文里抠出"人格修改"后面的需求
REQUIREMENT_RE = re.compile(r"^[/／]\s*人格修改\s*(?P<requirement>[\s\S]*)$")

#: 单段文本长度上限（防止模型输出把配置写坏）
MAX_CATEGORY_TEXT_LENGTH = 4000

#: 需求长度上限
DEFAULT_MAX_REQUIREMENT_LENGTH = 500

MAX_FORWARD_TEXT_LENGTH = 3000


# --------------------------------------------------------------------------------------
# 配置模型（Runner 会据此生成 config.toml 与 WebUI 配置界面）
# --------------------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础信息。

    MaiBot 的插件配置强制要求存在 ``[plugin]`` 段并提供 ``plugin.config_version``。
    默认配置由配置模型直接 `model_dump` 生成，SDK 不会自动补这一节，
    因此必须在这里显式声明，否则加载时会报「缺少 [plugin] 配置节」。
    """

    __ui_label__ = "插件信息"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description=(
            "配置结构版本号，MaiBot 依据它判断配置是否需要迁移。"
            "由插件维护，平时不需要改动；留空会导致配置校验失败。"
        ),
    )


class AccessConfig(PluginConfigBase):
    """白名单与权限。"""

    __ui_label__ = "白名单"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    whitelist: list[str] = Field(
        default_factory=list,
        description="允许使用指令的 QQ 号。支持 123456789 或 qq:123456789 两种写法，一行一个。",
    )
    deny_message: str = Field(
        default="您无权进行此类操作",
        description="非白名单用户触发指令时，麦麦在聊天流里回复的内容。",
    )
    notify_on_deny: bool = Field(
        default=True,
        description="非白名单用户触发时是否真的发送提示。关闭后无权限用户的指令会被静默忽略。",
    )


class ScopeConfig(PluginConfigBase):
    """指令的适用场景（私聊 / 群聊）。"""

    __ui_label__ = "限用范围"
    __ui_icon__ = "users"
    __ui_order__ = 2

    query_allow_group: bool = Field(
        default=False,
        description="/人格查询 的群聊执行开关。关闭（默认）时该指令只在私聊生效；打开后白名单用户也可以在群聊里查询。",
    )
    query_group_disabled_message: str = Field(
        default=DEFAULT_GROUP_DISABLED_MESSAGE,
        description="群聊执行开关关闭时，白名单用户在群聊发送 /人格查询 收到的提示。",
    )
    treat_unknown_scope_as_group: bool = Field(
        default=False,
        description=(
            "实在判断不出来消息来自群聊还是私聊时，按哪一类处理。"
            "默认按私聊处理（不影响正常使用）；若你的适配器上报信息不全、群聊被误放行，可改成 true。"
        ),
    )


class ModelConfig(PluginConfigBase):
    """调用哪个模型来做需求分类。"""

    __ui_label__ = "模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 3

    task_priority: list[str] = Field(
        default_factory=lambda: ["replyer", "planner"],
        description="按顺序尝试的模型任务名。默认先用 replyer，不可用时自动改用 planner。",
    )
    temperature: float = Field(
        default=0.2,
        description="分类时的温度，建议 0.1~0.3，越低保真度越高。",
    )
    max_tokens: int = Field(
        default=4096,
        description="单次生成的最大 token 数。",
    )
    timeout_seconds: float = Field(
        default=120.0,
        description="等待模型回复的最长秒数，超时后自动尝试下一个任务名。",
    )


class MessageConfig(PluginConfigBase):
    """对外文案。"""

    __ui_label__ = "文案"
    __ui_icon__ = "message-square"
    __ui_order__ = 4

    cat_personality: str = Field(default="#人设", description="人设段的标题，也是这段正文最前面的那行标签。")
    cat_behavior_style: str = Field(default="#行为风格", description="行为风格段的标题，也是这段正文最前面的那行标签。")
    cat_reply_style: str = Field(default="#表达风格", description="表达风格段的标题，也是这段正文最前面的那行标签。")

    label_in_content: bool = Field(
        default=True,
        description=(
            "把分类标题写在每段正文的最前面（标题独占一行，下面接正文）。"
            "合并转发的节点发送者名字常被适配器换成账号昵称，标签只写在名字上往往看不到，"
            "所以默认开启 —— 一眼就能分清哪段是人设、哪段是行为风格、哪段是表达风格。"
            "关闭后只在合并转发的发送者名字上显示标签。"
        ),
    )

    need_requirement_message: str = Field(
        default="请在指令后面带上修改需求",
        description="只发 /人格修改 没带需求时的提示。",
    )
    success_message: str = Field(
        default="人格已热更新成功",
        description="修改成功后的提示。",
    )
    mismatch_message: str = Field(
        default="人格热更新失败，当前需求不匹配",
        description="需求不属于人设 / 行为风格 / 表达风格任何一类时的提示。",
    )
    error_prefix: str = Field(
        default="人格热更新失败",
        description="出现技术性错误（模型失败、写文件失败等）时的提示前缀，后面会拼接具体原因。",
    )


class BehaviorConfig(PluginConfigBase):
    """运行行为。"""

    __ui_label__ = "运行行为"
    __ui_icon__ = "sliders"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="插件总开关。")
    dry_run: bool = Field(
        default=False,
        description="演练模式。打开后只把模型判定结果发到聊天流，不写入配置文件。建议先开一次确认效果。",
    )
    ignore_quoted_commands: bool = Field(
        default=True,
        description=(
            "忽略「引用消息」里携带的指令（推荐保持开启）。"
            "开启后，别人引用你发过的 /人格查询 或 /人格修改 不会再次触发本插件；"
            "本人正常发送指令不受影响。关闭后回到旧行为。"
        ),
    )
    backup: bool = Field(default=True, description="写入前备份 bot_config.toml。强烈建议保持开启。")
    backup_keep: int = Field(default=20, description="最多保留多少个备份文件。")
    config_path: str = Field(
        default="",
        description="bot_config.toml 的绝对路径。留空则自动定位，一般不需要填。",
    )
    max_requirement_length: int = Field(
        default=DEFAULT_MAX_REQUIREMENT_LENGTH,
        description="单次修改需求的最大字数，超出会被拒绝。",
    )


class PersonaManagerConfig(PluginConfigBase):
    """QQ 人格管理插件配置。"""

    #: 必须有：MaiBot 强制要求 [plugin] 段提供 config_version，用于配置迁移。
    plugin: PluginSection = Field(default_factory=PluginSection)

    access: AccessConfig = Field(default_factory=AccessConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    message: MessageConfig = Field(default_factory=MessageConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)


# --------------------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = """你是 MaiBot（麦麦）的人设配置管家。麦麦的人格被拆成三个互不重叠的分类：

1. personality（人设）—— 她是什么样的人。身份、性格、知识背景、态度、好恶。回答"她是谁"。
2. behavior_style（行为风格）—— 什么时候说话、什么时候不说话、观察什么、什么话题接不接。回答"什么时候开口、什么时候闭嘴"。只写祈使式的行动准则。
3. reply_style（表达风格）—— 怎么说话、说什么样的话。句子长短、语气、用词偏好、禁止的措辞、举例方式。回答"怎么说话"。

你的任务：
- 先判断用户的需求属于上面哪一类，或者同时属于哪几类。
- 只对命中的分类产出新文本：在【完整保留原文已有信息与风格】的基础上，把需求自然地融合进去。
  不要丢失原有设定，不要写成说明文，不要输出 diff 或片段。
- 人设段用第二人称或客观陈述，控制在 200 字以内。
- 行为风格段只写行动规则，不描述身份。
- 表达风格段只写措辞规则，不描述身份，也不做决策。
- 如果需求与人设、行为风格、表达风格【任何一类都不沾边】（例如要求机器人做与人格无关的功能、下达外部任务、乱码、无意义内容），必须返回 matched 为 false。
- 严禁编造需求里没有依据的内容。

只输出 JSON，不要输出任何解释、前后缀或代码块标记。格式二选一：

{"matched": true, "items": [{"category": "personality", "reason": "一句话说明判定理由", "new_text": "融合后的完整新文本"}]}

{"matched": false, "reason": "一句话说明为什么不匹配", "items": []}

硬性约束：
- items 里 category 只能是 personality / behavior_style / reply_style 三个值之一，每个分类最多出现一次。
- new_text 必须是可以直接替换原来那一整段的完整文本。
- matched 为 true 但 items 为空，等同于不匹配。
"""


# --------------------------------------------------------------------------------------
# 工具函数（纯函数，不依赖插件实例）
# --------------------------------------------------------------------------------------


def _normalize_qq(value: Any) -> str:
    """把各种写法的 QQ 号归一化成纯数字字符串。"""

    if value is None:
        return ""
    text = str(value).strip().replace(" ", "").replace("\u3000", "")
    if ":" in text:
        text = text.split(":")[-1].strip()
    if "：" in text:
        text = text.split("：")[-1].strip()
    return text.lstrip("@").strip()


def _segments_to_text(value: Any) -> str:
    """把消息段列表 / 字符串统一转成纯文本。"""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for segment in value:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").lower() != "text":
                continue
            data = segment.get("data")
            if isinstance(data, str):
                parts.append(data)
            elif isinstance(data, dict):
                parts.append(str(data.get("text") or ""))
        return "".join(parts)
    return ""


def _bool_or_none(value: Any) -> bool | None:
    """把各种写法的是/否归一化成 bool；无法判断时返回 None。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1", "是", "群聊", "group"}:
            return True
        if text in {"false", "no", "0", "否", "私聊", "private"}:
            return False
    return None


def _normalize_scope_text(value: Any) -> str:
    """把场景字段的文案归一化成 group / private / unknown。"""

    if not isinstance(value, str):
        return SCOPE_UNKNOWN
    text = value.strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"group", "group_message", "groupmsg", "群聊", "群", "群组"}:
        return SCOPE_GROUP
    if text in {"private", "private_message", "privatemessage", "friend", "c2c", "私聊", "好友"}:
        return SCOPE_PRIVATE
    return SCOPE_UNKNOWN


def _strip_control_chars(text: str) -> str:
    """去掉 TOML 不接受的裸控制字符，保留换行与制表符。"""

    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


def _toml_basic_string(value: str) -> str:
    """把任意字符串安全地写成一个 TOML 单行基本字符串字面量。"""

    cleaned = _strip_control_chars(str(value))
    escaped = (
        cleaned.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _parse_model_json(text: str) -> dict[str, Any] | None:
    """从模型输出里尽量稳妥地抠出 JSON 对象。"""

    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    # 去掉 ```json ... ``` 包裹
    candidate = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", candidate)
    candidate = re.sub(r"\s*```$", "", candidate).strip()

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    # 退而求其次：截取第一个 { 到最后一个 }
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


# --------------------------------------------------------------------------------------
# 插件主体
# --------------------------------------------------------------------------------------


class QQPersonaManagerPlugin(MaiBotPlugin):
    """QQ 人格管理插件。"""

    config_model = PersonaManagerConfig

    #: 订阅全局 bot 配置热重载（人设被别的途径改动时同步日志）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    SECTION_HEADER_RE = re.compile(r"^[ \t]*\[[ \t]*personality[ \t]*\][ \t]*(?:#.*)?$")
    ANY_SECTION_RE = re.compile(r"^[ \t]*\[")
    KEY_LINE_RE = re.compile(
        r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_.\-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')[ \t]*="
    )

    def __init__(self) -> None:
        super().__init__()
        self._write_lock = asyncio.Lock()
        #: stream_id -> (场景, 过期时间戳)，用于兜底判断消息来自群聊还是私聊
        self._stream_scope_cache: dict[str, tuple[str, float]] = {}

    # ---------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        """插件加载完成。"""

        config = self._conf()
        whitelist = config.access.whitelist or []
        if not whitelist:
            self.ctx.logger.warning(
                "白名单为空：目前所有用户都会被拒绝。请到 WebUI「插件 → QQ 人格管理 → 白名单」"
                "或 plugins/qq-persona-manager/config.toml 里填入管理员的 QQ 号。"
            )
        else:
            self.ctx.logger.info("白名单已载入，共 %d 个 QQ 号。", len(whitelist))

        path = self._find_config_path()
        if path is None:
            self.ctx.logger.warning(
                "没有自动定位到 config/bot_config.toml，/人格修改 会不可用。"
                "可在插件配置的「运行行为 → config_path」里手动填写绝对路径。"
            )
        else:
            self.ctx.logger.info("已定位人格配置文件：%s", path)

        self.ctx.logger.info("QQ 人格管理插件已加载。")

    async def on_unload(self) -> None:
        """插件卸载。"""

        self.ctx.logger.info("QQ 人格管理插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载回调。"""

        del config_data
        if scope == "self":
            self.ctx.logger.info("插件自身配置已更新：version=%s", version)
        elif scope == "bot":
            self.ctx.logger.info("全局 Bot 配置已热重载（可能包含人设变更）：version=%s", version)

    # ---------------------------------------------------------------- 指令

    @Command(
        "persona_query",
        description="查询麦麦当前的人设 / 行为风格 / 表达风格",
        pattern=PATTERN_QUERY,
        aliases=["/人格查询"],
    )
    async def handle_query(self, stream_id: str = "", **kwargs: Any):
        """/人格查询：三段式合并转发。"""

        config = self._conf()

        if not config.behavior.enabled:
            return False, "插件未启用", 2

        if config.behavior.ignore_quoted_commands and self._is_quoted_command_only(kwargs, QUERY_RE):
            self.ctx.logger.info("忽略引用消息里携带的 /人格查询（非本人正文）")
            return True, "已忽略引用消息中的指令", 2

        allowed, user_id = await self._check_permission(stream_id, kwargs)
        if not allowed:
            return True, f"无权限（user_id={user_id or '未知'}）", 2

        if not await self._check_query_scope(stream_id, kwargs):
            return True, "群聊执行开关未打开", 2

        try:
            current = await self._read_current_persona()
        except Exception as exc:
            self.ctx.logger.error("读取当前人格失败：%s", exc, exc_info=True)
            await self._reply(stream_id, f"{self._conf().message.error_prefix}：读取当前人格失败")
            return False, "读取人格失败", 2

        labels = (
            config.message.cat_personality,
            config.message.cat_behavior_style,
            config.message.cat_reply_style,
        )
        texts = (
            current["personality"] or "（当前未设置）",
            current["behavior_style"] or "（当前未设置）",
            current["reply_style"] or "（当前未设置）",
        )

        def render(label: str, text: str) -> str:
            """生成单段正文。

            合并转发的节点"发送者名字"在多数 QQ 适配器上会被渲染成账号昵称，
            而不是我们传进去的 nickname，所以标签只写在名字上等于没写。
            默认把分类标签顶在正文最前面，一眼就能分清哪段是哪类。
            """

            body = f"{label}\n{text}" if config.message.label_in_content else text
            return self._clip(body, MAX_FORWARD_TEXT_LENGTH)

        nodes = [
            {
                "user_id": "0",
                "nickname": label,
                "segments": [{"type": "text", "content": render(label, text)}],
            }
            for label, text in zip(labels, texts, strict=True)
        ]

        sent = await self.ctx.send.forward(nodes, stream_id)
        if not sent:
            self.ctx.logger.warning("合并转发发送失败，退回普通文本发送。")
            plain = "\n\n".join(render(label, text) for label, text in zip(labels, texts, strict=True))
            sent = await self.ctx.send.text(plain, stream_id)

        if not sent:
            return False, "人格查询结果发送失败", 2
        return True, "已发送人格查询结果", 2

    @Command(
        "persona_update",
        description="按需求热更新麦麦的人设 / 行为风格 / 表达风格",
        pattern=PATTERN_UPDATE,
        aliases=["/人格修改"],
    )
    async def handle_update(self, stream_id: str = "", **kwargs: Any):
        """/人格修改 需求：分类 -> 融合 -> 写回配置。"""

        config = self._conf()
        if not config.behavior.enabled:
            return False, "插件未启用", 2

        if config.behavior.ignore_quoted_commands and self._is_quoted_command_only(kwargs, UPDATE_RE):
            self.ctx.logger.info("忽略引用消息里携带的 /人格修改（非本人正文）")
            return True, "已忽略引用消息中的指令", 2

        allowed, user_id = await self._check_permission(stream_id, kwargs)
        if not allowed:
            return True, f"无权限（user_id={user_id or '未知'}）", 2

        requirement = self._extract_requirement(kwargs)
        if not requirement:
            await self._reply(stream_id, config.message.need_requirement_message)
            return True, "缺少修改需求", 2

        limit = int(config.behavior.max_requirement_length or DEFAULT_MAX_REQUIREMENT_LENGTH)
        if len(requirement) > limit:
            await self._reply(stream_id, f"{config.message.error_prefix}：需求过长（上限 {limit} 字）")
            return True, "需求过长", 2

        try:
            current = await self._read_current_persona()
        except Exception as exc:
            self.ctx.logger.error("读取当前人格失败：%s", exc, exc_info=True)
            await self._reply(stream_id, f"{config.message.error_prefix}：读取当前人格失败")
            return False, "读取人格失败", 2

        self.ctx.logger.info("收到人格修改需求（来源 user_id=%s）：%s", user_id or "未知", requirement)

        decision = await self._classify_requirement(requirement, current)
        if decision is None:
            await self._reply(stream_id, f"{config.message.error_prefix}：模型调用失败或返回无法解析")
            return False, "模型调用失败", 2

        if not decision["matched"]:
            self.ctx.logger.info("需求判定为不匹配：%s", decision.get("reason") or "")
            await self._reply(stream_id, config.message.mismatch_message)
            return True, "需求不匹配", 2

        updates: dict[str, str] = {}
        for item in decision["items"]:
            field = CATEGORY_FIELDS[item["category"]]
            new_text = item["new_text"]
            if new_text == current.get(item["category"], ""):
                continue
            updates[field] = new_text

        if not updates:
            self.ctx.logger.info("模型判定匹配，但新文本与原文一致，视为无改动。")
            await self._reply(stream_id, config.message.mismatch_message)
            return True, "无实际改动", 2

        if config.behavior.dry_run:
            preview = "\n".join(
                f"[{field}] {updates[field]}" for field in CATEGORY_ORDER if CATEGORY_FIELDS[field] in updates
            )
            await self._reply(stream_id, f"（演练模式，未写入配置）\n{preview}")
            return True, "演练模式", 2

        try:
            async with self._write_lock:
                await asyncio.to_thread(self._write_personality, updates)
        except Exception as exc:
            self.ctx.logger.error("写入 bot_config.toml 失败：%s", exc, exc_info=True)
            await self._reply(stream_id, f"{config.message.error_prefix}：写入配置文件失败（{exc}）")
            return False, "写入配置失败", 2

        self.ctx.logger.info("人格已写入并等待热重载，字段：%s", ", ".join(updates))
        await self._reply(stream_id, config.message.success_message)
        return True, "人格已热更新成功", 2

    @Command(
        "persona_help",
        description="查看 QQ 人格管理插件的可用指令",
        pattern=PATTERN_HELP,
        aliases=["/人格帮助"],
    )
    async def handle_help(self, stream_id: str = "", **kwargs: Any):
        """/人格帮助：列出可用指令。"""

        config = self._conf()

        if config.behavior.ignore_quoted_commands and self._is_quoted_command_only(kwargs, HELP_RE):
            self.ctx.logger.info("忽略引用消息里携带的 /人格帮助（非本人正文）")
            return True, "已忽略引用消息中的指令", 2

        allowed, _ = await self._check_permission(stream_id, kwargs)
        if not allowed:
            return True, "无权限", 2

        help_text = (
            "人格管理指令：\n"
            "/人格查询 —— 查看麦麦当前的人设、行为风格、表达风格\n"
            "/人格修改 需求 —— 按需求热更新人格\n"
            "  例：/人格修改 说话加上喵~\n"
            "  例：/人格修改 晚上 11 点以后就别发言了\n"
            "/人格帮助 —— 显示这条说明"
        )
        await self._reply(stream_id, help_text)
        return True, "已发送帮助", 2

    # ---------------------------------------------------------------- 场景判断

    @staticmethod
    def _scope_from_container(container: Any) -> str:
        """从单个字典（消息对象 / kwargs）里判断群聊还是私聊。"""

        if not isinstance(container, dict):
            return SCOPE_UNKNOWN

        # 1) 扁平的布尔标志位（MaiMessages 形状）
        flag = _bool_or_none(container.get("is_group_message"))
        if flag is not None:
            return SCOPE_GROUP if flag else SCOPE_PRIVATE
        flag = _bool_or_none(container.get("is_private_message"))
        if flag is not None:
            return SCOPE_PRIVATE if flag else SCOPE_GROUP

        # 2) 明确的场景字段
        for key in ("chat_type", "message_type", "session_type", "chat_mode"):
            scope = _normalize_scope_text(container.get(key))
            if scope != SCOPE_UNKNOWN:
                return scope

        # 3) MessageDict 形状：message_info.group_info 是 None 即私聊。
        #    宿主侧的群聊消息 group_id 必非空，所以这个信号是可靠的。
        info = container.get("message_info")
        if isinstance(info, dict) and "group_info" in info:
            group_info = info.get("group_info")
            if isinstance(group_info, dict):
                return SCOPE_GROUP if (group_info.get("group_id") or group_info.get("group_name")) else SCOPE_PRIVATE
            return SCOPE_PRIVATE

        # 4) MaiMessages 形状：message_base_info
        base = container.get("message_base_info")
        if isinstance(base, dict) and "group_info" in base:
            group_info = base.get("group_info")
            if isinstance(group_info, dict):
                return SCOPE_GROUP if (group_info.get("group_id") or group_info.get("group_name")) else SCOPE_PRIVATE
            return SCOPE_PRIVATE

        # 5) 只给了群号
        if container.get("group_id") or container.get("group_name"):
            return SCOPE_GROUP

        return SCOPE_UNKNOWN

    @classmethod
    def _scope_from_message(cls, kwargs: dict[str, Any]) -> str:
        """只靠指令回调参数判断场景。"""

        for key in ("message", "msg", "message_data", "raw"):
            scope = cls._scope_from_container(kwargs.get(key))
            if scope != SCOPE_UNKNOWN:
                return scope
        return cls._scope_from_container(kwargs)

    @staticmethod
    def _extract_platform(kwargs: dict[str, Any]) -> str:
        """取出平台标识，默认 qq。"""

        for key in ("message", "msg", "message_data"):
            container = kwargs.get(key)
            if isinstance(container, dict):
                value = container.get("platform")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        value = kwargs.get("platform")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "qq"

    async def _scope_from_streams(self, stream_id: str, platform: str) -> str:
        """兜底：向宿主要聊天流列表，用 stream_id 反查场景。"""

        if not stream_id:
            return SCOPE_UNKNOWN

        now = time.monotonic()
        cached = self._stream_scope_cache.get(stream_id)
        if cached is not None and cached[1] > now:
            return cached[0]

        try:
            raw = await self.ctx.chat.get_all_streams(platform or "qq")
        except Exception as exc:
            self.ctx.logger.warning("查询聊天流列表失败，无法判断消息场景：%s", exc)
            return SCOPE_UNKNOWN

        # 宿主的原始返回是 {"success":..., "streams":[...]}，SDK 可能已解包，两种都兼容
        streams = raw.get("streams") if isinstance(raw, dict) else raw
        if not isinstance(streams, list):
            return SCOPE_UNKNOWN

        scope = SCOPE_UNKNOWN
        for item in streams:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("stream_id") or item.get("session_id") or "")
            if item_id != stream_id:
                continue
            scope = _normalize_scope_text(item.get("chat_type"))
            if scope == SCOPE_UNKNOWN:
                flag = _bool_or_none(item.get("is_group_session"))
                if flag is not None:
                    scope = SCOPE_GROUP if flag else SCOPE_PRIVATE
            if scope == SCOPE_UNKNOWN and (item.get("group_id") or item.get("group_name")):
                scope = SCOPE_GROUP
            break

        if scope != SCOPE_UNKNOWN:
            self._stream_scope_cache[stream_id] = (scope, now + STREAM_SCOPE_CACHE_TTL)
        return scope

    async def _resolve_scope(self, kwargs: dict[str, Any]) -> str:
        """判断当前消息来自群聊还是私聊。全部手段都失败时按配置回退。"""

        scope = self._scope_from_message(kwargs)
        if scope != SCOPE_UNKNOWN:
            return scope

        stream_id = str(kwargs.get("stream_id") or "")
        scope = await self._scope_from_streams(stream_id, self._extract_platform(kwargs))
        if scope != SCOPE_UNKNOWN:
            return scope

        scope = SCOPE_GROUP if self._conf().scope.treat_unknown_scope_as_group else SCOPE_PRIVATE
        self.ctx.logger.warning("无法判断消息场景（stream_id=%s），按配置回退为 %s。", stream_id, scope)
        return scope

    async def _check_query_scope(self, stream_id: str, kwargs: dict[str, Any]) -> bool:
        """检查 /人格查询 是否允许在当前场景使用。"""

        config = self._conf()
        if config.scope.query_allow_group:
            return True

        scope = await self._resolve_scope(kwargs)
        if scope != SCOPE_GROUP:
            return True

        self.ctx.logger.info("群聊执行开关未打开，拒绝群聊里的 /人格查询（stream_id=%s）", stream_id)
        await self._reply(stream_id, config.scope.query_group_disabled_message)
        return False

    # ---------------------------------------------------------------- 权限

    async def _check_permission(self, stream_id: str, kwargs: dict[str, Any]) -> tuple[bool, str]:
        """校验发送者是否在白名单里；不在就按配置发送拒绝提示。"""

        user_id = self._extract_user_id(kwargs)
        if self._is_whitelisted(user_id):
            return True, user_id

        config = self._conf()
        self.ctx.logger.warning(
            "非白名单用户触发人格指令：user_id=%s stream_id=%s", user_id or "未知", stream_id
        )
        if config.access.notify_on_deny and stream_id:
            await self._reply(stream_id, config.access.deny_message)
        return False, user_id

    def _is_whitelisted(self, user_id: str) -> bool:
        """判断 QQ 号是否命中白名单。"""

        target = _normalize_qq(user_id)
        if not target:
            return False
        for entry in self._conf().access.whitelist or []:
            if _normalize_qq(entry) == target:
                return True
        return False

    @staticmethod
    def _extract_user_id(kwargs: dict[str, Any]) -> str:
        """从指令回调参数里尽可能稳地抠出发送者的 QQ 号。"""

        candidate_keys = ("user_id", "userid", "userId", "sender_id", "sender_uid", "qq", "uin")

        def _pick(source: Any) -> str:
            if not isinstance(source, dict):
                return ""
            for key in candidate_keys:
                value = source.get(key)
                if value not in (None, ""):
                    text = str(value).strip()
                    if text:
                        return text
            return ""

        direct = _pick(kwargs)
        if direct:
            return direct

        user_info = kwargs.get("user_info")
        picked = _pick(user_info)
        if picked:
            return picked

        for container_key in ("message", "msg", "message_data", "raw"):
            container = kwargs.get(container_key)
            if not isinstance(container, dict):
                continue
            message_info = container.get("message_info")
            if isinstance(message_info, dict):
                picked = _pick(message_info.get("user_info"))
                if picked:
                    return picked
            picked = _pick(container)
            if picked:
                return picked
        return ""

    # ---------------------------------------------------------------- 引用消息防护

    @staticmethod
    def _collect_segments(kwargs: dict[str, Any]) -> list[Any]:
        """尽量从回调参数里收集本消息的段列表；取不到就返回空列表。"""

        candidates: list[Any] = [kwargs.get("raw_message")]
        message = kwargs.get("message")
        if isinstance(message, dict):
            candidates.append(message.get("raw_message"))
            candidates.append(message.get("message_segments"))
        elif isinstance(message, list):
            candidates.append(message)
        for value in candidates:
            if isinstance(value, list) and value:
                return value
        return []

    @staticmethod
    def _has_quote_segment(segments: list[Any]) -> bool:
        """判断消息段里是否含有引用（回复）段。"""

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").strip().lower() in QUOTE_SEGMENT_TYPES:
                return True
        return False

    @classmethod
    def _is_quoted_command_only(cls, kwargs: dict[str, Any], pattern: "re.Pattern[str]") -> bool:
        """判断本次调用是否只是「引用了一条指令消息」，而非本人真在发指令。

        返回 True 表示应当忽略本次调用。原因见 ``QUOTE_SEGMENT_TYPES`` 的说明：
        宿主会把被引用消息的原文拼到文本最前面，且别名匹配不带结尾约束，
        于是「引用指令」会被误触发。这里用消息段做二次判定 ——
        只要存在引用段，就要求指令必须出现在**非引用的正文**里才算数。

        为避免误杀，以下情况一律放行（返回 False）：
        - 拿不到任何消息段（宿主形态不同，宁可放行也不误伤正常使用）；
        - 消息里没有引用段（本人正常发送指令）。
        """

        segments = cls._collect_segments(kwargs)
        if not segments:
            return False
        if not cls._has_quote_segment(segments):
            return False
        own_text = _segments_to_text(segments).strip()
        if not own_text:
            return True
        return pattern.fullmatch(own_text) is None

    @staticmethod
    def _raw_text(kwargs: dict[str, Any]) -> str:
        """取出本条指令的原始文本。"""

        for key in ("raw_message", "text", "message_text", "content"):
            value = kwargs.get(key)
            as_text = _segments_to_text(value)
            if as_text.strip():
                return as_text

        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in ("raw_message", "processed_plain_text", "plain_text"):
                as_text = _segments_to_text(message.get(key))
                if as_text.strip():
                    return as_text
        return ""

    @classmethod
    def _extract_requirement(cls, kwargs: dict[str, Any]) -> str:
        """从指令回调参数里取出"人格修改"后面的需求文本。"""

        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict):
            for key in ("requirement", "need", "content", "text"):
                value = matched.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        direct = kwargs.get("requirement")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        raw = cls._raw_text(kwargs)
        match = REQUIREMENT_RE.match(raw.strip())
        if match is None:
            return ""
        return match.group("requirement").strip()

    # ---------------------------------------------------------------- 发送

    async def _reply(self, stream_id: str, text: str) -> None:
        """往聊天流发一条文本消息，失败只记日志不抛异常。"""

        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:  # pragma: no cover - 取决于宿主实现
            self.ctx.logger.error("发送消息失败：%s", exc, exc_info=True)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """截断过长文本，避免超出适配器的单条长度限制。"""

        if len(text) <= limit:
            return text
        return text[:limit] + "……（内容过长已截断）"

    # ---------------------------------------------------------------- 配置读取

    def _conf(self) -> PersonaManagerConfig:
        """拿到强类型配置，异常时退回默认值。"""

        try:
            config = self.config
        except Exception:  # pragma: no cover - Runner 尚未注入配置
            return PersonaManagerConfig()
        if isinstance(config, PersonaManagerConfig):
            return config
        return PersonaManagerConfig()

    async def _read_current_persona(self) -> dict[str, str]:
        """读取当前三段人格。优先走 config.get，失败则直接读配置文件。"""

        result: dict[str, str] = {category: "" for category in CATEGORY_ORDER}

        section = await self._safe_config_get("personality")
        if isinstance(section, dict):
            for category in CATEGORY_ORDER:
                value = section.get(CATEGORY_FIELDS[category])
                if isinstance(value, str) and value.strip():
                    result[category] = value.strip()

        if not any(result.values()):
            for category in CATEGORY_ORDER:
                value = await self._safe_config_get(f"personality.{CATEGORY_FIELDS[category]}")
                if isinstance(value, str) and value.strip():
                    result[category] = value.strip()

        if not any(result.values()):
            data = await asyncio.to_thread(self._read_config_file)
            section = data.get("personality") if isinstance(data, dict) else None
            if isinstance(section, dict):
                for category in CATEGORY_ORDER:
                    value = section.get(CATEGORY_FIELDS[category])
                    if isinstance(value, str) and value.strip():
                        result[category] = value.strip()

        return result

    async def _safe_config_get(self, key: str) -> Any:
        """调用 config.get，出错返回 None。"""

        try:
            return await self.ctx.config.get(key)
        except Exception as exc:
            self.ctx.logger.debug("config.get(%s) 失败：%s", key, exc)
            return None

    # ---------------------------------------------------------------- 模型调用

    async def _classify_requirement(
        self, requirement: str, current: dict[str, str]
    ) -> dict[str, Any] | None:
        """让模型判断需求属于哪一类，并给出融合后的新文本。

        Returns:
            None 表示模型全部不可用或返回无法解析；
            否则返回 {"matched": bool, "items": [...], "reason": str}。
        """

        config = self._conf()
        user_payload = json.dumps(
            {
                "requirement": requirement,
                "current": {
                    "personality": current.get("personality", ""),
                    "behavior_style": current.get("behavior_style", ""),
                    "reply_style": current.get("reply_style", ""),
                },
            },
            ensure_ascii=False,
            indent=2,
        )

        tasks = [str(task).strip() for task in (config.model.task_priority or []) if str(task).strip()]
        if not tasks:
            tasks = ["replyer", "planner"]

        for task_name in tasks:
            parsed = await self._call_model(task_name, user_payload, config)
            if parsed is None:
                continue
            decision = self._normalize_decision(parsed)
            if decision is not None:
                return decision
            self.ctx.logger.warning("模型任务 %s 返回的 JSON 结构不合法，尝试下一个。", task_name)
        return None

    async def _call_model(
        self, task_name: str, user_payload: str, config: PersonaManagerConfig
    ) -> dict[str, Any] | None:
        """调用一次模型；失败返回 None。"""

        prompt = [
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ]
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt,
                    task_name=task_name,
                    temperature=config.model.temperature,
                    max_tokens=config.model.max_tokens,
                ),
                timeout=max(5.0, float(config.model.timeout_seconds or 120.0)),
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning("模型任务 %s 调用超时。", task_name)
            return None
        except Exception as exc:
            self.ctx.logger.warning("模型任务 %s 调用失败：%s", task_name, exc)
            return None

        if not isinstance(result, dict):
            self.ctx.logger.warning("模型任务 %s 返回类型异常。", task_name)
            return None
        if not result.get("success"):
            self.ctx.logger.warning(
                "模型任务 %s 调用未成功（%s），尝试下一个。", task_name, result.get("error") or result.get("response") or ""
            )
            return None

        content = result.get("response") or result.get("content") or ""
        parsed = _parse_model_json(str(content))
        if parsed is None:
            self.ctx.logger.warning("模型任务 %s 的输出无法解析为 JSON：%s", task_name, str(content)[:200])
        return parsed

    def _normalize_decision(self, parsed: dict[str, Any]) -> dict[str, Any] | None:
        """校验并规范化模型返回的判定结果。"""

        raw_matched = parsed.get("matched")
        if isinstance(raw_matched, str):
            matched_flag: bool | None = raw_matched.strip().lower() in {"true", "yes", "1", "是"}
        elif isinstance(raw_matched, bool):
            matched_flag = raw_matched
        else:
            matched_flag = None

        raw_items = parsed.get("items")
        if not isinstance(raw_items, list):
            raw_items = []

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            category = str(raw_item.get("category") or "").strip().lower()
            category = CATEGORY_ALIASES.get(category, category)
            if category not in CATEGORY_FIELDS or category in seen:
                continue
            new_text = raw_item.get("new_text")
            if not isinstance(new_text, str):
                continue
            new_text = _strip_control_chars(new_text).strip()
            if not new_text or len(new_text) > MAX_CATEGORY_TEXT_LENGTH:
                continue
            seen.add(category)
            items.append(
                {
                    "category": category,
                    "reason": str(raw_item.get("reason") or "").strip(),
                    "new_text": new_text,
                }
            )

        items.sort(key=lambda item: CATEGORY_ORDER.index(item["category"]))

        if matched_flag is False or not items:
            return {"matched": False, "reason": str(parsed.get("reason") or "").strip(), "items": []}
        return {"matched": True, "reason": str(parsed.get("reason") or "").strip(), "items": items}

    # ---------------------------------------------------------------- 配置文件

    def _find_config_path(self) -> Path | None:
        """定位 config/bot_config.toml。"""

        override = str(self._conf().behavior.config_path or "").strip()
        if override:
            candidate = Path(override).expanduser()
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                pass
            self.ctx.logger.warning("配置里填写的 config_path 不可用：%s", override)

        for root in self._root_candidates():
            candidate = root / "config" / "bot_config.toml"
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _root_candidates(self) -> list[Path]:
        """推断 MaiBot 根目录的候选列表（去重、保持顺序）。"""

        roots: list[Path] = []

        def _add(path: Any, max_parents: int) -> None:
            if path is None:
                return
            try:
                resolved = Path(path).expanduser().resolve()
            except (OSError, TypeError, ValueError):
                return
            roots.append(resolved)
            roots.extend(list(resolved.parents)[:max_parents])

        paths = getattr(self.ctx, "paths", None)
        _add(getattr(paths, "data_dir", None), 6)
        _add(getattr(paths, "runtime_dir", None), 6)

        try:
            _add(Path.cwd(), 4)
        except OSError:
            pass

        try:
            _add(Path(__file__).resolve().parent, 6)
        except (OSError, NameError):
            pass

        ordered: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                ordered.append(root)
        return ordered

    def _read_config_file(self) -> dict[str, Any]:
        """读取并解析 bot_config.toml。"""

        path = self._find_config_path()
        if path is None:
            raise FileNotFoundError("找不到 config/bot_config.toml")
        import tomllib

        return tomllib.loads(path.read_text(encoding="utf-8"))

    def _write_personality(self, updates: dict[str, str]) -> Path:
        """把更新写回 [personality] 段（同步阻塞操作，调用方用 to_thread 包一层）。"""

        # 最后一道闸门：只允许写人格三段，备用风格等字段一律拒绝
        blocked = sorted(field for field in updates if field not in CATEGORY_FIELDS.values())
        if blocked:
            raise ValueError(f"拒绝写入非人格三段字段：{', '.join(blocked)}")

        path = self._find_config_path()
        if path is None:
            raise FileNotFoundError("找不到 config/bot_config.toml，请在插件配置里填写 config_path")

        original = path.read_text(encoding="utf-8")
        if self._conf().behavior.backup:
            self._backup(path, original)

        rewritten = self._rewrite_personality_section(original, updates)
        self._verify_rewrite(rewritten, updates)

        path.write_text(rewritten, encoding="utf-8")
        return path

    def _backup(self, path: Path, content: str) -> None:
        """把当前配置备份到插件数据目录。"""

        try:
            backup_dir = Path(self.ctx.paths.data_dir) / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = backup_dir / f"bot_config.{stamp}.toml"
            target.write_text(content, encoding="utf-8", newline="\n")

            keep = max(1, int(self._conf().behavior.backup_keep or 20))
            backups = sorted(backup_dir.glob("bot_config.*.toml"))
            for old in backups[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception as exc:
            self.ctx.logger.warning("备份 bot_config.toml 失败（不影响继续写入）：%s", exc)

    @staticmethod
    def _verify_rewrite(content: str, updates: dict[str, str]) -> None:
        """写盘前用 tomllib 自检语法，并确认目标字段确实被改成了新值。"""

        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python < 3.11
            return

        try:
            data = tomllib.loads(content)
        except Exception as exc:
            raise ValueError(f"改写后的 TOML 语法校验失败：{exc}") from exc

        section = data.get("personality")
        if not isinstance(section, dict):
            raise ValueError("改写后的配置里找不到 [personality] 段")

        for key, expected in updates.items():
            actual = section.get(key)
            if actual != expected:
                raise ValueError(f"[personality].{key} 未按预期写入")

    def _rewrite_personality_section(self, content: str, updates: dict[str, str]) -> str:
        """文本级手术：只替换 [personality] 段里目标键的值，其余内容原样保留。"""

        lines = content.splitlines(keepends=True)

        start_index: int | None = None
        for index, line in enumerate(lines):
            if self.SECTION_HEADER_RE.match(line.rstrip("\r\n")):
                start_index = index
                break

        if start_index is None:
            # 老配置里没有 [personality]，在文件末尾补一段
            block = ["[personality]\n"]
            for category in CATEGORY_ORDER:
                field = CATEGORY_FIELDS[category]
                if field in updates:
                    block.append(f"{field} = {_toml_basic_string(updates[field])}\n")
            separator = "" if content.endswith("\n") or not content else "\n"
            return content + separator + "\n" + "".join(block)

        end_index = len(lines)
        for index in range(start_index + 1, len(lines)):
            if self.ANY_SECTION_RE.match(lines[index]):
                end_index = index
                break

        section_lines = lines[start_index + 1 : end_index]
        rebuilt: list[str] = []
        handled: set[str] = set()

        index = 0
        while index < len(section_lines):
            raw_line = section_lines[index]
            match = self.KEY_LINE_RE.match(raw_line)
            key = ""
            if match is not None:
                key = match.group("key").strip().strip('"').strip("'")

            if match is not None and key in updates:
                indent = match.group("indent")
                rebuilt.append(f"{indent}{match.group('key')} = {_toml_basic_string(updates[key])}\n")
                handled.add(key)
                index += 1

                remainder = raw_line.split("=", 1)[1].strip() if "=" in raw_line else ""
                for delimiter in ('"""', "'''"):
                    if remainder.startswith(delimiter) and remainder.count(delimiter) < 2:
                        index = self._skip_multiline(section_lines, index, delimiter)
                        break
                continue

            rebuilt.append(raw_line)
            index += 1

        missing = [category for category in CATEGORY_ORDER if CATEGORY_FIELDS[category] in updates]
        missing = [CATEGORY_FIELDS[category] for category in missing if CATEGORY_FIELDS[category] not in handled]
        if missing:
            # 缺字段就直接补在 [personality] 标题下方
            rebuilt[0:0] = [f"{field} = {_toml_basic_string(updates[field])}\n" for field in missing]

        return "".join(lines[: start_index + 1]) + "".join(rebuilt) + "".join(lines[end_index:])

    @staticmethod
    def _skip_multiline(section_lines: list[str], index: int, delimiter: str) -> int:
        """跳过三引号多行字符串的剩余行，返回下一行的下标。"""

        while index < len(section_lines):
            if delimiter in section_lines[index]:
                return index + 1
            index += 1
        return index


def create_plugin() -> QQPersonaManagerPlugin:
    """SDK 入口：返回插件实例。"""

    return QQPersonaManagerPlugin()

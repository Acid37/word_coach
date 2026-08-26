"""word_coach 插件配置。

配置文件默认路径：config/plugins/word_coach/config.toml
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class WordCoachConfig(BaseConfig):
    """word_coach 背单词助手插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "背单词助手插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件行为配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用插件",
        )

        daily_push_enabled: bool = Field(
            default=True,
            description="是否启用每日定时推送",
        )

        push_time: str = Field(
            default="09:00",
            description="每日推送时间（HH:MM，24 小时制）",
        )

        daily_word_count: int = Field(
            default=10,
            description="每日推送的总词数（到期复习 + 新词）",
        )

        daily_new_count: int = Field(
            default=3,
            description="每日推送中的新词数量（其余为到期复习词）",
        )

    @config_section("scope")
    class ScopeSection(SectionBase):
        """工具可见范围配置。"""

        allowed_targets: list[str] = Field(
            default=[],
            description=(
                "word_quiz / word_lookup 工具可见的聊天流白名单，"
                "格式：platform:user:ID（私聊）或 platform:group:ID（群聊），"
                "例如 ['qq:user:123456', 'qq:group:789']。"
                "只有命中白名单的流里 LLM 才能看到背单词工具。"
            ),
        )

        tools_visible_default: bool = Field(
            default=False,
            description=(
                "未命中白名单的聊天流是否仍可见背单词工具；"
                "默认 false（fail-closed，仅白名单可见）"
            ),
        )

        tools_in_groups: bool = Field(
            default=False,
            description=(
                "白名单中的群聊是否也注入背单词工具；默认 false——即使群在白名单里，"
                "word_quiz/word_lookup/word_import 也不会出现在群聊（工具仅私聊可用，"
                "命令与每日推送不受影响）"
            ),
        )

    @config_section("web")
    class WebSection(SectionBase):
        """内置 Web UI 配置。"""

        owner_target: str = Field(
            default="",
            description=(
                "网页测验与网页进度绑定的主人聊天流，"
                "格式：platform:user:ID（如 qq:user:2583090218）。"
                "留空时回退 [scope].allowed_targets 的第一项。"
                "网页端背单词的进度与该聊天流（QQ 私聊）完全共用。"
            ),
        )

        quiz_count: int = Field(
            default=10,
            description="网页测验每次会话取词总数（到期复习优先）",
        )

        quiz_new: int = Field(
            default=3,
            description="网页测验每次会话中的新词数量上限",
        )

        theme: str = Field(
            default="light",
            description="Web UI 明暗主题（light/dark）",
        )

        primary_color: str = Field(
            default="#5b6cff",
            description="Web UI 主题色（十六进制，如 #5b6cff）",
        )

        bg_url: str = Field(
            default="",
            description="Web UI 背景图 URL（留空=纯色背景，支持任意图片直链）",
        )

        bg_opacity: float = Field(
            default=0.85,
            description="有背景图时内容区的透明度（0.0-1.0）",
        )

    @config_section("source")
    class SourceSection(SectionBase):
        """词库来源配置（自动下载导入）。"""

        auto_import_urls: list[str] = Field(
            default=[],
            description=(
                "词书为空时自动下载导入的词库 URL 列表（按顺序尝试，成功即停）。"
                "支持 .json/.csv/.tsv/.txt 直链，例如 GitHub raw 链接。"
            ),
        )

        auto_import_if_empty: bool = Field(
            default=True,
            description=(
                "词书为空且配置了 auto_import_urls 时，是否在启动后自动下载导入；"
                "默认 true（URL 列表为空时不会触发任何网络请求）"
            ),
        )

        preset_urls: dict[str, str] = Field(
            default={},
            description=(
                "预置词库（名字 → 直链），供 word_import 工具的 preset 参数和"
                '/背单词 下载词库 使用；例如 {"cet4": "https://...cet4.json"}'
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    web: WebSection = Field(default_factory=WebSection)
    source: SourceSection = Field(default_factory=SourceSection)

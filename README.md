# word_coach 背单词助手

Neo-MoFox 插件的背单词助手：词库导入、Leitner 间隔复习、每日定时推送、LLM 对话式测验与自然语言词库管理。

- 词库来源：内置起步词表 / 文件导入（JSON/CSV/TSV/TXT）/ 远程 URL 下载 / 启动自动导入
- 复习算法：Leitner 五箱间隔复习（1/2/4/7/15 天递进）
- 交互方式：自然语言为主（LLM 工具），命令为辅（主人状态查询）
- 进度模型：按聊天流（session）独立维护，互不干扰

> 规划中的进阶方向（句子本 / 每日句子推送 / 例句联想 / 短语支持 / 发音编排）见文末「路线图」。

## 组件清单

| 组件 | 类型 | 说明 |
|---|---|---|
| `word_coach` | service | 词书 CRUD、Leitner 调度、进度统计、文件/URL 导入入库 |
| `word_quiz` | tool | 对话式测验：取题（next）/ 提交判定（submit）/ 待复习数（due_count） |
| `word_lookup` | tool | 查词：释义 / 音标 / 例句 |
| `word_import` | tool | 词库管理：状态（status）/ 下载导入（download）/ 扫描目录（sync） |
| `背单词` | command | 主人查询命令（进度总览 / 单流详情 / 词库总览） |
| `word_stream_gate` | event_handler | 工具可见性门控（BEFORE_TOOL_FILTER，按聊天流白名单动态注入/剔除） |
| `word_pending_reminder` | event_handler | 待判定题提醒（ON_MESSAGE_RECEIVED 注入 system-reminder） |
| `word_coach_web` | router | 内置 Web UI（仪表盘/词书/导入/测验/进度），挂载于框架 HTTP 服务器 |
| `config` | config | 配置组件，映射到 `config/plugins/word_coach/config.toml` |

## 安装与启用

1. 将 `word_coach` 目录放入 `plugins/`（框架启动时自动发现）。
2. 首次加载自动生成/读取配置 `config/plugins/word_coach/config.toml`。
3. 确认 `[plugin].enabled = true`，重启 Bot 生效。

## 配置说明

`config/plugins/word_coach/config.toml`（`config/` 目录不入库，敏感信息安全）：

### [plugin]

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 插件总开关 |
| `daily_push_enabled` | `true` | 每日定时推送开关 |
| `push_time` | `"09:00"` | 推送时间（HH:MM，24 小时制） |
| `daily_word_count` | `10` | 每次推送总词数（到期复习 + 新词） |
| `daily_new_count` | `3` | 每次推送中新词数量 |

### [scope]

| 字段 | 默认 | 说明 |
|---|---|---|
| `allowed_targets` | `["qq:user:2583090218"]` | 工具可见聊天流白名单：`platform:user:ID` / `platform:group:ID` |
| `tools_visible_default` | `false` | 未命中白名单的流是否仍可见工具（默认 fail-closed） |
| `tools_in_groups` | `false` | 白名单中的群聊是否注入工具（默认 false：工具仅私聊可用，命令与推送不受影响） |

### [source]

| 字段 | 默认 | 说明 |
|---|---|---|
| `auto_import_urls` | `[]` | 词书为空时启动自动下载的词库 URL 列表（按顺序尝试，成功即停） |
| `auto_import_if_empty` | `true` | 是否启用上述自动导入（URL 列表为空时不触发网络请求） |
| `preset_urls` | `{}` | 预置词库（名字 → 直链），供 `word_import` 的 `preset` 参数使用 |

### [web]（0.9.0 新增）

| 字段 | 默认 | 说明 |
|---|---|---|
| `owner_target` | `""` | 网页测验/进度绑定的主人聊天流，格式 `platform:user:ID`；留空回退 `[scope].allowed_targets` 第一项 |
| `quiz_count` | `10` | 网页测验每次会话取词总数（到期复习优先） |
| `quiz_new` | `3` | 网页测验每次会话的新词数量上限 |

## Web UI（0.9.0 新增）

插件内置全功能网页界面，**无需任何额外安装**：通过框架统一的内嵌 HTTP 服务器
（FastAPI + uvicorn）挂载，不自己监听端口；前端为随插件分发的单文件 `web/index.html`
（无构建、无 CDN 依赖）。

### 访问地址

- 地址 = 框架核心配置 `[http_router]` 的 `host:port` + `/word-coach/`（默认 `http://127.0.0.1:8000/word-coach/`）。
- 插件启动时自动读取框架 HTTP 服务器单例打印实际地址（**不假设 8000**）——多个 Bot 实例各自改了端口也互不冲突。
- 前置条件：核心配置 `[http_router].enable_http_router = true`（默认开启）。
- 仅建议本机访问（默认绑定 127.0.0.1），未做鉴权。

### 功能

| 页面 | 能力 |
|---|---|
| 仪表盘 | 词书规模、来源分布、各聊天流进度条形图、绑定流的正确率圆环与今日待复习 |
| 词书 | 搜索分页 + 添加/编辑/删除单词（补齐自然语言外的手动管理入口） |
| 导入词库 | ① 粘贴 URL 一键下载 ② 预置词库下拉一键导入 ③ 上传本地词表文件 ④ 内置获取指引（格式示例、GitHub raw 直链获取、`preset_urls` 配置方法） |
| 测验 | 卡片式背单词（先回忆→看答案→认识/不认识），进度计入绑定的主人聊天流 |
| 进度 | 各流总览 + 单流详情（箱分布、待复习词单） |

### 网页测验与聊天流的关系

网页测验绑定主人的私人聊天流（`[web].owner_target`，默认回退白名单第一项）：

- 进度、箱子、到期时间与 QQ 私聊**完全共用同一份数据**——网页上答对的词，QQ 里也不会重复考。
- 网页测验不使用 pending 待判定机制（会话由前端跟踪）；但提交判定时若 QQ 侧恰有同一词的待判定题会顺手清除，避免互相卡住。

## 词库获取

### 1. 内置起步词表

首次启动词书为空时自动播种 30 个常用词（`sources/__init__.py` 内 `STARTER_WORDS`）。

### 2. 文件导入

词表文件放入 `data/word_coach/imports/`，对 Bot 说"同步词表"（LLM 调用 `word_import` 的 `sync` 动作）。支持四种格式：

- **JSON 数组**：`[{"word": "...", "phonetic": "...", "meaning": "...", "example": "..."}]`
- **JSON 字典**：`{"apple": "n. 苹果"}`
- **CSV/TSV**：带表头（`word/单词`、`meaning/释义`、`phonetic/音标`、`example/例句`、`tags/标签`，中英列名均可）或无表头（位置列同上顺序）
- **纯文本**：每行一个词，可带释义（Tab 或空格分隔），`#` 开头为注释

编码要求 UTF-8（兼容 Excel 导出的 UTF-8 BOM）。

### 3. 远程 URL 下载

对 Bot 说"下载词库 <直链>"，或直接调用 `word_import` 的 `download` 动作（传 `url` 或 `preset`）。支持 `.json/.csv/.tsv/.txt`，未知后缀自动嗅探内容（JSON → CSV/TSV → 纯文本行）。

### 4. 启动自动导入

词书为空且配置了 `[source].auto_import_urls` 时，启动后后台自动下载导入（不阻塞启动，失败仅记日志，成功即停）。

## 工具说明（自然语言交互）

三个工具仅对白名单聊天流中的 LLM 可见（见「可见性与权限」）。

### word_quiz — 对话式测验

| action | 参数 | 说明 |
|---|---|---|
| `next` | `count=1` | 取下一个待测词（含释义/音标/例句），登记为待判定题 |
| `submit` | `correct`（必填），`word_id`（可选） | 提交作答判定，推进 Leitner 状态；word_id 缺省取本流待判定题 |
| `cancel` | — | 关闭待判定题（作废场景，不记进度） |
| `due_count` | — | 查询当前待复习词数 |

**进度防漏记机制**：出题后题目进入服务端 pending 状态；`word_pending_reminder` 事件处理器监听 `ON_MESSAGE_RECEIVED`——只要存在未提交判定的题且用户发来新消息，就往 actor system-reminder 注入"必须提交判定"的提醒；提交/取消后自动清除。未处理待判定题前，`next` 取新题会被挡住，从机制上杜绝"LLM 忘记更新进度"。

### word_lookup — 查词

| 参数 | 说明 |
|---|---|
| `word` | 查询词书的释义/音标/例句 |

### word_import — 词库管理

| action | 参数 | 说明 |
|---|---|---|
| `status` | — | 词书规模、来源分布、可用预置词库 |
| `download` | `url` 或 `preset` | 下载并导入远程词库 |
| `sync` | — | 扫描 `data/word_coach/imports/` 导入词表文件 |

## 命令说明（仅主人）

| 命令 | 说明 |
|---|---|
| `/背单词 进度` | 所有有进度的聊天流总览 |
| `/背单词 进度 qq 2583090218` | 查询指定用户进度（两参数形式） |
| `/背单词 进度 qq:2583090218` | 同上（单参数形式） |
| `/背单词 进度 qq:group:12345` | 查询指定群进度 |
| `/背单词 词库` | 词书总览（总量 + 来源分布 + 预置词库） |
| `/背单词 帮助` | 帮助 |

别名：`/word`。命令为 `OWNER` 权限，owner 在任何流（含群聊）可用。

## 可见性与权限

- **工具门控**：`word_stream_gate` 监听框架 `BEFORE_TOOL_FILTER` 事件，每轮 LLM 调用前按 `stream_id` 动态剔除/保留 word 工具。未命中白名单的流默认不可见（fail-closed）；`tools_in_groups=false` 时群聊即使白名单也不注入工具（仅私聊）。
- **命令权限**：`/背单词` 为 `OWNER` 级，走框架标准命令权限门。
- **命令与推送不受工具门控影响**：门控只作用于 LLM 工具可见性。

## 复习算法（Leitner 五箱）

- 箱子 1–5，间隔 1/2/4/7/15 天。
- 答对：升一箱（上限箱 5）；答错：回到箱 1。
- 每次作答更新 `due_at`、复习/对/错计数。
- 取词顺序：到期复习词优先，其次新词；新词数量受 `new_limit` 约束。

## 数据与进度模型

- 数据库：`data/word_coach/words.db`（SQLite，`data/` 不入库）。
- 表：
  - `words`：词条（word、phonetic、meaning、example、source、tags）。
  - `progress`：学习进度（user_key、word_id、platform、user_id、box、due_at、计数）。
- **进度按聊天流隔离**：`user_key = stream_id`。私聊天然按人（一人一流）；群聊为群级进度（全群共享一个 key，不区分成员）。各流互不影响。
- **进度可读化**：progress 记录 `platform/user_id`（工具提交时从触发消息提取），主人查询时显示 `qq:2583090218` 而非哈希；老库自动迁移补列，历史行回退显示 user_key。

## 扩展开发

### 新增词源适配器

词源统一归一化为 `[{word, phonetic, meaning, example, tags}, ...]` 后调用 `service.import_entries()` 入库。新增来源只需：

1. 在 `sources/` 下实现抓取/解析函数（参考 `sources/__init__.py` 的 `fetch_and_parse_url`）。
2. 复用 `service.import_url()` / `service.import_entries()` 公共入库入口（自动去重计数）。

### 代码质量

- 代码风格：`ruff check` / `ruff format` 全绿。
- 回归测试：各版本迭代均含数据层回归（解析、入库、Leitner、进度隔离、门控、命令权限）。

## 路线图（规划中，方向指导）

以下为已确认的方向，尚未实现；按顺序推进，每项落地前会更新本 README 与版本号。

### 1. 常用语 / 短语支持（轻量）

- 词表结构不限制词条为单个单词：`add_word("give up", "放弃")`、导入短语表（phrasal verbs / 惯用语 TSV）即可直接进入现有 `word_quiz` 测验与每日推送流程。
- 需补充：常用短语/惯用语词库内容；可选按 `tags` 过滤出题（如 phrasal / idiom / slang）。

### 2. 句子本模块（进阶）

新增 `sentences(sentence, translation, source, tags)` 数据模型，扩展以下能力：

- **每日句子推送**：与每日单词推送并行，按计划推送句子 + 翻译。
- **例句联想**：`word_lookup` / `word_quiz` 查词或出题时，带出词库中含该词的句子（例句与词条双向关联）。
- **句子测验**：挖空、翻译回填等题型（复用 `word_quiz` 的对话式交互）。
- **句子收藏**：聊天中说"收藏这句"，由 LLM 调工具存句。

### 3. 发音 / 听力（可选编排）

- 本插件不实现 TTS。若部署环境存在可用的朗读能力（平台语音消息、其他 TTS 插件等），可编排 LLM 在背单词/句子推送时触发朗读；实现方式由部署方自行接入，本插件仅预留编排点。

### 4. 其他待评估

- 词根词缀知识卡片；场景对话练习；词书间的错词本（跨 session 汇总）。

## 版本历史

- **0.9.0**：内置 Web UI（仪表盘/词书增删改查/一键导入词库/进度看板/网页测验，绑定主人聊天流）；`_normalize_entry` 支持主流开源词库字段别名（name/trans/usphone 等）；`manifest.json` 补声明 httpx 依赖；修复 `word_lookup` 引导到已移除添加命令的过期文案。
- **0.8.0**：测验防漏记——服务端 pending 待判定题 + ON_MESSAGE_RECEIVED 自动提醒，`next` 挡住未闭环题目；新增 `cancel`；进度记录 platform/user_id 可读化，老库自动迁移。
- **0.7.2**：新增 `[scope].tools_in_groups` 开关（默认群聊不注入工具）。
- **0.7.1**：查询命令简化为「平台 + ID」输入。
- **0.7.0**：命令收敛为 owner 查询（进度总览/单流详情/词库总览）。
- **0.6.0**：新增 `word_import` 工具（自然语言词库管理）。
- **0.5.0**：支持 URL 下载导入与启动自动导入（`[source]` 配置节）。
- **0.4.0**：移除扇贝（Shanbay）适配器，词库走文件导入；明确按 session 维护进度。
- **0.3.0**：扇贝 Cookie 半自动设置命令（后随 0.4.0 移除）。
- **0.2.0**：CSV/TSV 文件导入 + 扇贝词源适配器（后随 0.4.0 移除）。
- **0.1.0**：初版（内置词表 + Leitner + 每日推送 + 白名单工具门控）。

## License

[AGPL-3.0](LICENSE)

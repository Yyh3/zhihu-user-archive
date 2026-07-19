# zhihu-user-archive

一个用于归档知乎用户公开内容的 Codex Skill。它可以采集指定用户公开展示的回答、文章、想法、专栏、提问、视频、动态，以及接口可见的评论，并导出为便于检索和长期保存的 JSONL、CSV 与 Markdown 文件。

这个项目强调“覆盖范围可核验”：当某类内容受登录、验证码、接口变化、隐私设置或数量不一致影响时，会明确标记为 `partial`、`unavailable` 或 `failed`，不会把不完整结果描述成完整归档。

## 主要能力

- 归档回答、文章、想法、专栏、动态、提问和视频。
- 尝试归档用户发表的评论。
- 可选归档回答、文章和想法下的根评论与子评论。
- 首次完整归档默认采用自适应评论策略，按需依次尝试 v5 分数排序、时间排序及旧版评论入口。
- 使用可重建的 SQLite 状态索引保存父任务和逐页游标，支持精确断点续传。
- 已完整归档的评论树不会在后续排序阶段重复请求子评论。
- 支持断点续传、去重、起始偏移和限量采集。
- 支持二次补全回答与文章正文。
- 使用有状态 CookieJar，自动接收服务端 `Set-Cookie` 更新。
- 遇到登录或安全验证时，可打开隔离的 Chromium 浏览器完成登录。
- 支持 Windows、macOS 和基础 Linux 路径。
- 输出覆盖状态、数量差异和错误明细。

## 仓库结构

```text
skill/zhihu-user-archive/
├── SKILL.md
├── agents/openai.yaml
├── references/schema.md
├── scripts/
│   ├── archive_state.py
│   ├── comment_pipeline.py
│   ├── archive_zhihu_user.py
│   └── browser_auth.py
└── tests/
    ├── test_archive_state.py
    ├── test_comment_pipeline.py
    ├── test_archive_integration.py
    └── test_streaming_exports.py
```

主程序是 `skill/zhihu-user-archive/scripts/archive_zhihu_user.py`。

## 环境要求

- Python 3.10 或更高版本。
- 公开访问和 Cookie 文件模式只使用 Python 标准库。
- 浏览器登录模式需要 Playwright。
- Playwright 当前正式支持 macOS 14 Sonoma 或更新版本。
- 自动登录使用 Chromium、Google Chrome 或 Microsoft Edge，不读取日常浏览器配置。

### macOS

建议安装与 Playwright 版本匹配的 Chromium，Intel Mac 和 Apple Silicon 均由 Playwright 选择对应版本：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

### Windows

```powershell
python -m pip install playwright
```

Windows 已安装 Edge 时，不必额外下载 Chromium。若普通 `python` 是 Windows Store 占位程序，请使用 Codex 提供的捆绑 Python 运行时。

## 安装为 Codex Skill

克隆仓库：

```bash
git clone https://github.com/Yyh3/zhihu-user-archive.git
cd zhihu-user-archive
```

macOS 或 Linux：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skill/zhihu-user-archive "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Windows PowerShell：

```powershell
$skillHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
New-Item -ItemType Directory -Force -Path (Join-Path $skillHome 'skills') | Out-Null
Copy-Item -Recurse -Force 'skill\zhihu-user-archive' (Join-Path $skillHome 'skills\zhihu-user-archive')
```

重新启动或刷新 Codex 后，即可通过类似“归档这个知乎用户的公开内容”“增量更新这个知乎用户的文章和评论”等请求触发 skill。

## 快速使用

以下命令也可以脱离 Codex，直接运行脚本。

### macOS

```bash
python3 skill/zhihu-user-archive/scripts/archive_zhihu_user.py \
  --user "https://www.zhihu.com/people/example-user" \
  --output "$HOME/Documents/example-user-zhihu-archive" \
  --auth-mode auto \
  --types answers,articles,pins,columns,activities,comments-authored \
  --delay 2.0
```

### Windows PowerShell

```powershell
python skill\zhihu-user-archive\scripts\archive_zhihu_user.py `
  --user "https://www.zhihu.com/people/example-user" `
  --output "$HOME\Documents\example-user-zhihu-archive" `
  --auth-mode auto `
  --types answers,articles,pins,columns,activities,comments-authored `
  --delay 2.0
```

## 登录方式

`--auth-mode` 支持四种模式：

- `auto`：默认模式。先尝试公开访问或显式提供的 Cookie；遇到 `401`、`403` 或安全验证页面后，打开隔离浏览器登录并重试。
- `public`：只访问无需登录的公开接口，不启动浏览器。
- `browser`：运行前先取得隔离浏览器会话。
- `cookie`：只使用 `--cookie-file` 或 `--cookie-env` 提供的认证信息。

提前建立或清除隔离浏览器会话：

```bash
python3 skill/zhihu-user-archive/scripts/archive_zhihu_user.py --login-browser
python3 skill/zhihu-user-archive/scripts/archive_zhihu_user.py --logout-browser
```

Windows 将 `python3` 换成 `python`。

隔离浏览器配置的默认位置：

- Windows：`%LOCALAPPDATA%\Codex\zhihu-user-archive\browser-profile`
- macOS：`~/Library/Application Support/Codex/zhihu-user-archive/browser-profile`
- Linux：`~/.local/share/codex/zhihu-user-archive/browser-profile`

不要把 `z_c0`、完整 Cookie Header、Cookie 文件或浏览器配置目录粘贴到聊天、提交到 Git，或放进归档交付物。

## 常用范围参数

核心原创内容：

```text
--types answers,articles,pins,columns
```

增加动态和用户评论：

```text
--types answers,articles,pins,columns,activities,comments-authored
```

增加提问与视频：

```text
--types answers,articles,pins,columns,activities,questions,zvideos
```

归档所采集内容下的评论：

```text
--content-comments all
```

首次完整归档默认启用自适应评论策略：

```text
--content-comments all --comment-strategy adaptive --legacy-fallback auto
```

`adaptive` 会在当前阶段达到详情页的 `comment_count` 后立即停止该父内容，避免固定遍历所有旧接口。专项核对时可使用 `exhaustive`，只抓主入口时可使用 `single-pass`。

断点续传并刷新已有内容下的新评论：

```text
--resume --content-comments all --refresh-existing-comments
```

补全回答和文章正文：

```text
--enrich-details --types answers,articles --resume
```

## 输出文件

每次归档输出目录包含：

- `records.jsonl`：无损优先的标准化记录，每行保留原始 API 对象。
- `archive_state.sqlite3`：可由 JSONL 重建的任务、游标和轻量索引，不替代原始档案。
- `records.csv`：适合表格软件和全文检索的扁平索引。
- `markdown/<type>.md`：按内容类型生成的可读全文。
- `manifest.json`：记录请求范围、数量、错误、覆盖状态和完整性判断。

`manifest.json` 中的状态含义：

- `complete`：分页正常结束，且没有已知数量冲突。
- `partial`：已取得部分内容，但限量、分页、评论或可见数量表明仍有缺口。
- `unavailable`：登录、验证、隐私设置或接口移除导致不可访问。
- `failed`：网络或解析错误导致该类别未能完成。

“用户发表的评论”接口并不稳定。返回零条只代表当前接口没有返回记录，不能证明该用户从未发表过评论，因此程序会保守地标记覆盖风险。

## 工作原理

1. 将用户主页或 `url_token` 解析为标准知乎用户标识。
2. 根据请求类型映射到集中维护的知乎 `/api/v4` 接口族。
3. 按服务端返回的 `paging.next` 顺序翻页，并限制跨域分页。
4. 将不同接口对象归一化为统一字段，同时保留完整 `raw` 对象。
5. 通过“记录类型 + 稳定 ID”去重，并把 JSONL 作为可恢复的持久层。
6. 使用 SQLite 保存可重建索引、父内容状态和逐页检查点；中断后从未完成游标继续。
7. 评论抓取按 `v5 score → v5 ts → legacy comments → legacy root comments` 自适应升级，并跳过已经齐全的子评论树。
8. 使用 CookieJar 接收请求过程中的 `Set-Cookie`，避免长时间归档继续使用过期会话。
9. 当公开请求或现有会话受到认证挑战时，启动专用 Playwright 浏览器配置；用户完成可见登录或验证后，只在内存中组装请求 Cookie。
10. 完成采集后流式生成 CSV、Markdown 和覆盖报告，并对照可见数量列出评论差额。

项目不会读取日常 Chrome、Edge 或 Safari 的 Cookie 数据库，也不会自动破解验证码、轮换身份、使用代理绕过限流或访问私密、已删除内容。

## 验证

运行离线自测：

```bash
python3 skill/zhihu-user-archive/scripts/archive_zhihu_user.py --self-test
```

运行完整测试：

```bash
PYTHONPATH=skill/zhihu-user-archive/scripts \
python3 -m unittest discover -s skill/zhihu-user-archive/tests -v
```

自测覆盖基础采集、认证后重试、长任务重新认证、`Set-Cookie` 更新，以及 macOS 配置目录和浏览器候选路径。

## 当前限制

- 知乎 `/api/v4` 不是稳定的公开 API，字段和接口路径可能变化。
- 用户历史评论接口可能不开放、返回不完整或直接失效。
- 回答和文章列表接口可能省略或截断正文，需要额外执行正文补全。
- 评论归档量可能远大于原创内容，需要合理设置延迟和增量策略。
- 浏览器登录仍要求用户亲自完成登录、验证码或安全验证。

## 下一步待完善

- 在 Intel Mac 和 Apple Silicon Mac 上分别完成真实登录、会话复用和退出登录的端到端测试。
- 增加 GitHub Actions，至少运行 Windows 与 macOS 的离线自测、Python 编译和 skill 结构校验。
- 增加知乎接口响应样本测试，降低字段变化导致静默漏数的风险。
- 为接口映射与正文补全建立更清晰的版本兼容层。
- 改进评论覆盖检查，区分“接口返回零条”和“用户确实没有公开评论”。
- 增加可选的依赖初始化命令，自动检查 Playwright 与浏览器是否已安装。
- 增加 Linux 桌面环境的真实浏览器登录测试。
- 增加归档数据的增量差异报告，明确新增、更新和失效记录。

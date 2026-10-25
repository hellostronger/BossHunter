# Kimi WebBridge 采集与技能图谱

> 这是两个可选的增量能力，不改变原有 CDP 采集流程：
> - **WebBridge 采集**：复用你已登录的真实 Chrome 完成 BOSS 岗位浏览，采集结果仍写入同一个 `jobs` 表。
> - **技能图谱**：把已有岗位 JD 归纳成技能需求图、个人差距报告和四周突击计划。

## 1. 为什么需要 WebBridge

原有采集器通过 CDP 驱动 BossHunter 自己的 Chrome，并受内部页面额度（每日搜索页 / 详情页）约束。Kimi WebBridge 是「Chrome 插件 + 本地守护进程」：

```
BossHunter ──HTTP──> WebBridge daemon ──WebSocket──> Chrome 插件 ──> 你已登录的 BOSS 页面
```

页面访问由你本人的浏览器完成，因此：

- 不需要再启动一个独立浏览器；
- 不占用 BossHunter 内部的 BOSS 页面额度；
- 登录态、风控验证都由你在真实窗口里处理，脚本遇到验证码/登录墙会停下，不会尝试绕过。

**它不改变平台规则，也不保证不被风控。** 仍然建议低频、少量、人工可观察，并自行承担账号风险。

## 2. 安装 WebBridge

### 2.1 安装守护进程

```bash
curl -fsSL https://kimi-web-img.moonshot.cn/webbridge/install.sh | bash
kimi-webbridge start            # 默认监听 127.0.0.1:10086
```

自定义端口（例如本机 10086 已被占用）：

```bash
mkdir -p ~/.kimi-webbridge
printf '{"addr": "127.0.0.1:10087"}' > ~/.kimi-webbridge/config.json
kimi-webbridge start --foreground --addr 127.0.0.1:10087
```

对应地把 `config.yaml` 里的 `webbridge.command_url` 改成 `http://127.0.0.1:10087/command`。

### 2.2 安装浏览器插件

从 Kimi 官方渠道安装「Kimi 浏览器插件 / Kimi WebBridge」扩展，确认插件图标显示已连接。

> Chrome 137 之后，正式版 Chrome 会忽略命令行 `--load-extension`。如果你需要脚本化启动一个带插件的独立 Chrome，请使用 **Chrome for Testing**，并确认扩展目录通过 `--disable-extensions-except=... --load-extension=...` 加载成功。

### 2.3 登录 BOSS

在同一个 Chrome 里打开 <https://www.zhipin.com> 并登录。之后运行：

```bash
bosshunter webbridge
```

会输出守护进程地址、标签页列表，以及当前页面的 URL / 标题 / 可解析岗位卡片数。若显示登录页，请先在浏览器里完成登录。

## 3. 通过 WebBridge 采集

```bash
# 单组合：2 页列表 + 最多 6 个详情页，最多新增 15 个岗位
bosshunter collect-wb --keyword "AI Agent" --city 深圳 --pages 2 --details 6 --max-new 15

# 多组合（关键词/城市都可以重复）
bosshunter collect-wb -k 大模型 -k RAG --city 深圳 --city 广州 --pages 1 --details 4

# 只预览，不落库
bosshunter collect-wb -k "大模型应用" --city 广州 --pages 1 --dry-run

# 跳过薪资/公司/排除词硬过滤
bosshunter collect-wb -k "AI Engineer" --city 深圳 --no-filter
```

不传 `--keyword` / `--city` 时读取 `config.yaml` 的 `search.keywords` / `search.cities`（其次 `profile.target_cities`）。

行为说明：

| 行为 | 说明 |
| --- | --- |
| 页面节奏 | 列表页之间随机 7–14 秒，详情页之间随机 2–4 秒；`--delay-scale 1.5` 可再放慢 |
| 上限 | 每个组合最多 10 页、详情预算最多 50、单次新增最多 200 |
| 去重 | 先查内存，再查数据库 `jobs.id`（BOSS 岗位 ID），已存在的不再打开详情 |
| 过滤 | 默认走 `quick_score`：实习、匿名公司、屏蔽公司、排除词、薪资区间 |
| 详情失败 | 请求了详情但 JD 没加载出来时，该岗位本次不落库，并计入运行报告的 `detail_failed` |
| 风控 | 识别验证码、登录页、403、频率限制后停止当前组合并记录原因 |
| 运行报告 | 写入 `data/webbridge-runs/collect-*.json` |

落库字段与常规采集一致：`id / title / company / salary / city / experience / education / jd / hr_* / company_* / url / source_platform / source_job_id / source_keyword`，外包识别列也由同一套规则计算。

### 薪资字体

BOSS 会把薪资数字映射到私有区字符。采集器会先尝试从当前页面加载的字体动态重建映射，失败时退回已验证的静态表（`U+E031..U+E03A → 0..9`）。如果出现 `-K` 之类的乱码薪资，说明字体又变了，需要更新映射。

### 让 Claude Code 代跑（可选）

如果你在服务器上装了 Claude Code，并把它指向同一个模型服务（例如本地 New API 的 Anthropic 兼容端点），可以让它根据自然语言调用上面的命令：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000
export ANTHROPIC_AUTH_TOKEN=...          # 不要写进仓库
export ANTHROPIC_MODEL=你的模型名
claude -p "用 bosshunter collect-wb 找 5 个深圳的 AI Agent 岗位，pages=1 details=4，最后汇报新增和失败原因" \
  --dangerously-skip-permissions --output-format text
```

这只是让模型帮你选关键词、执行命令和总结结果；采集、解析、落库仍然走本项目代码。

## 4. 技能图谱与差距报告

### 4.1 准备技能画像

复制模板并改成你的真实情况：

```bash
cp skills-profile.example.yaml data/skills-profile.yaml
```

格式：

```yaml
skills:
  - {name: Python, level: 熟练}
  - {name: RAG, level: 熟练}
  - {name: Kubernetes, level: 了解}   # 了解/入门 => 部分具备
```

技能名会自动映射到固定词表（如 `k8s → Kubernetes`、`智能体 → AI Agent`）。画像只影响「我的状态」和覆盖率，不影响技能需求统计。

### 4.2 生成报告

```bash
# 对全部有 JD 的岗位做 AI 抽取（默认 3 并发，结果按 JD 哈希缓存）
bosshunter skill-graph

# 先小规模试跑
bosshunter skill-graph --limit 30 --no-plan

# 只重绘报告，不再调用模型
bosshunter skill-graph --skip-extract

# 临时用一行技能覆盖画像
bosshunter skill-graph --skills "Python,SQL,RAG,知识库"
```

输出：

| 文件 | 内容 |
| --- | --- |
| `data/reports/skills-graph.html` | 可交互力导向图：节点=技能，边=共现；按缺口/部分/已具备筛选，点击看岗位样例 |
| `data/reports/gap-report.md` | 需求排行表 + 学习计划 |
| `data/reports/skills-graph.json` | 原始聚合数据 |
| `data/reports/skills-graph.html` 内嵌 | 四周突击学习计划（由 AI 生成） |

抽取结果缓存在 `job_skill_tags` 表；JD 未变化的岗位不会重复调用模型。修改画像后加 `--skip-extract` 即可秒出。

### 4.3 用 nginx 暴露报告（可选）

如果 BossHunter 部署在服务器上，可把报告目录作为静态站点：

```nginx
location /reports/ {
    alias /path/to/bosshunter/data/reports/;
    index skills-graph.html;
    charset utf-8;
    add_header Cache-Control "no-store";
}
```

再访问 `https://your-host/reports/skills-graph.html`。如果外层已有 Basic Auth，报告会自动继承。

## 5. 常见问题

**`bosshunter webbridge` 报连接失败**

守护进程没启动，或端口不一致。先 `curl -s http://127.0.0.1:10086/command` 看是否可达，再核对 `webbridge.command_url`。

**采集到 0 个岗位，但浏览器里能看到列表**

页面结构变化或列表还没渲染。用 `bosshunter webbridge` 看当前页面卡片数；必要时在浏览器里手动刷新后再试。

**详情页总是失败**

BOSS 详情可能是新标签页打开。采集器会等待 `.job-sec-text` 出现，超时后关闭标签并跳过该岗位。可先手动打开一个岗位确认页面正常。

**薪资显示成 `-K`**

字体映射失效。先确认浏览器页面里薪资正常显示；再检查 `boss_font.build_boss_digit_map` 是否还能从当前字体提取 ASCII 参照字形。

**报告里全部是「未评估」**

`data/skills-profile.yaml` 不存在或 skills 为空。填好后运行 `bosshunter skill-graph --skip-extract`。

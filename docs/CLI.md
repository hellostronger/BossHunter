# BossHunter CLI 命令

## 常用命令

| 命令 | 用途 |
|---|---|
| `bosshunter web` | 打开本地配置面板和工作台 |
| `bosshunter connect` | 检查 Browser Runtime 与 Chrome 连接 |
| `bosshunter ai-status` | 安全检查 AI 服务连接，不显示完整 Key |
| `bosshunter run` | 运行完整流程 |
| `bosshunter status` | 查看简要统计 |
| `bosshunter status --full` | 查看完整状态仪表盘 |

## 分步执行

```bash
bosshunter scrape -k "Python开发"   # 采集岗位
bosshunter score                    # AI 评分
bosshunter confirm                  # 人工确认投递清单
bosshunter greet                    # 为已确认岗位生成招呼语
bosshunter send                     # 发送已生成的招呼语
```

`bosshunter scrape` 还支持 `--limit` / `-l` 限制本次最多采集的岗位数。`bosshunter score --rescore-filtered` 可以重新评分此前被 AI 判为低分的岗位。

## 回复监听

```bash
bosshunter monitor                 # 按配置间隔持续检查
bosshunter monitor --once          # 只检查一次
bosshunter monitor --interval 20   # 指定检查间隔，单位为分钟
```

监听能力仅用于允许自动发送和监听的平台。外部只读平台不会进入自动监听流程。

## 定制简历

```bash
bosshunter resume --job-id JOB_ID
```

该命令为指定岗位生成定制简历 PDF。简历请求仍应由用户检查后手动处理。

## WebBridge 采集与技能图谱（可选）

```bash
bosshunter webbridge                         # 检测 WebBridge 守护进程与浏览器登录状态
bosshunter collect-wb -k "AI Agent" --city 深圳 --pages 2 --details 6
bosshunter collect-wb -k 大模型 -k RAG --city 深圳 --city 广州 --pages 1 --dry-run
bosshunter skill-graph                       # AI 抽取 JD 技能并生成图谱/差距报告
bosshunter skill-graph --skip-extract        # 只重绘报告
```

`collect-wb` 通过 Kimi WebBridge 在已登录的 Chrome 中采集，落库结构与常规采集一致，不占用内部页面额度。`skill-graph` 输出到 `data/reports/`。安装、配置与限制见 [WEBBRIDGE.md](WEBBRIDGE.md)。

## Web 面板参数

```bash
bosshunter web --port 8686
bosshunter web --no-open
```

如需查看当前版本的完整参数，以本地帮助为准：

```bash
bosshunter --help
bosshunter COMMAND --help
```

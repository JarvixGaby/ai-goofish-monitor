# gap_scanner — CLAUDE.md

闲鱼虚拟商品缺口扫描器。Playwright 爬取搜索结果，词库 + AI 分类商品，计算供需缺口分，生成每日选品报告。

## v2 架构（采集 → 存储 → 分析 三阶段解耦）

```
[Phase 1] collect.py → 搜索卡片 + 详情页增强 → data/raw/ + data/enriched/
                                                         ↓
[Phase 2] analyze.py → 缺口分 + AI深度分析 + 关键词发现 → reports/ + keywords/更新
```

### 核心模块

```
collect.py          Phase 1 主入口：采集
analyze.py          Phase 2 主入口：异步分析
scan.py             旧版一体化入口（保留兼容）
fetcher.py          Playwright 爬虫（搜索 + 详情页）
vocabulary.py       词库匹配引擎（强/弱信号两层）
ai_classifier.py    词库未命中时 AI 兜底分类
scanner.py          缺口分计算
keyword_analyzer.py AI 关键词有效性评估 + 细分建议
ai_advisor.py       Top5 选品 AI 建议
vocab_learner.py    扫描后从标题学习新信号词
reporter.py         生成 Markdown 日报
config.py           配置（双模型 + 详情页参数）
ai_client.py        统一 AI 客户端（含 build_analysis_client）
```

## 运行命令

```bash
cd gap_scanner

# ── v2 两阶段模式（推荐） ──

# Phase 1: 采集
arch -arm64 python3 collect.py                   # broad + active 全扫
arch -arm64 python3 collect.py --broad-only       # 只扫宽泛词（发现模式）
arch -arm64 python3 collect.py --active-only      # 只扫活跃词（日常模式）
arch -arm64 python3 collect.py --no-detail        # 跳过详情页（快速模式）
arch -arm64 python3 collect.py --detail-limit 10  # 限制详情页数量
arch -arm64 python3 collect.py --limit 3          # 调试用

# Phase 2: 分析（采集完成后异步运行）
arch -arm64 python3 analyze.py                    # 分析今日数据
arch -arm64 python3 analyze.py --date 2026-04-17  # 分析指定日期
arch -arm64 python3 analyze.py --no-ai            # 跳过 AI 步骤

# ── 旧版一体化模式（兼容） ──
arch -arm64 python3 scan.py
arch -arm64 python3 scan.py --no-ai --no-learn
```

> **注意**：必须加 `arch -arm64`，否则 pydantic_core 等包会因架构不兼容报错。

## AI 模型

默认统一使用 `claude-sonnet-4-5-20250929`（$3/$15 per 1M tokens），可通过环境变量分别覆盖：

```
AI_MODEL=claude-sonnet-4-5-20250929           # 轻量任务（分类、关键词评估）
AI_ANALYSIS_MODEL=claude-sonnet-4-5-20250929  # 深度分析（选品建议、关键词衍生）
```

## 关键词生命周期

```
keywords/
├── broad.txt       宽泛种子词（人工维护，定期扩充新赛道）
├── active.txt      当前活跃词（由 analyze.py 管理）
├── candidates.txt  待验证词（由 analyze.py 自动发现）
└── retired.txt     已淘汰词（保留记录，不再扫描）
```

生命周期流转：
- broad 搜索 → 发现子领域 → candidates
- candidates 验证有效 → active
- active 连续无效 → retired
- retired 关键词自动从 broad/active 中移除，下次扫描自动跳过
- 定期从 broad 注入新赛道（防止视野收窄）

### 关键词自动更新闭环

```
analyze.py 发现新词 → candidates.txt
                          ↓
collect.py 下次扫描时一起扫描 candidates
                          ↓ AI 评估
              valid → 晋升到 active.txt
              invalid → 移入 retired.txt
              noisy → 保留 candidates 下次复验
```

关键词来源（优先级从高到低）：
1. **AI 衍生**：基于搜索结果标题 + 详情页描述做语义分析，推荐有选品价值的细分词
2. **invalid 替代建议**：AI 评估 invalid 时给出的替代搜索词（如 `咨询` → `心理咨询课程`）
3. **AI 市场洞察**：高价值商品深度分析中推荐的新关键词
4. **n-gram 统计**：标题高频词组（经过交付话术/通用词/碎片过滤后的备选）

## 数据目录结构

```
data/
├── raw/YYYY-MM-DD/
│   ├── search.jsonl          每行一个关键词的搜索结果
│   └── collect_summary.json  采集摘要
├── enriched/YYYY-MM-DD/
│   └── {item_id}.json        详情页增强数据
├── analysis/
│   └── YYYY-MM-DD.json       分析结果
└── YYYY-MM-DD.jsonl           旧格式（兼容 scan.py）
```

## 详情页增强

触发条件：virtual/weak_virtual 分类 + 价格 ¥1-500

采集字段：`want_count`、`browse_count`、`description`（前2000字）、`image_urls`、`seller_reg_days`、`zhima_level`、`item_status`、`category_name`

反风控：每页间隔 10-20s，单次上限 20 个，检测到风控立即中止。

## 词库结构

```
vocab/
├── virtual_strong.txt   强信号词（单词命中即判 virtual）
├── virtual_weak.txt     弱信号词（需 2+ 或搭配交付词）
├── demand_signal.txt    求购信号词
├── delivery_method.txt  交付方式辅助词
├── blacklist.txt        误判排除词
├── keyword_status.json  关键词有效性记录
└── pending_review.txt   AI 学习到的待审候选词
```

## 配置（AI 密钥）

推荐写入 `~/.zshrc`：
```bash
export ANTHROPIC_BASE_URL=https://cc.codesome.ai
export ANTHROPIC_AUTH_TOKEN=sk-...
```

## 已知问题

- **want_num 全部为 0**：搜索卡片的 wantNum 字段值为 0，详情页的 wantCnt 可获取准确值。
- **无 soldCount 字段**：闲鱼详情页 API 不直接暴露已售量，只能通过 browseCnt 和 wantCnt 间接判断。
- **详情页风控敏感**：频繁访问详情页容易触发验证，需保守间隔。

# gap_scanner v2 架构重构计划

## 核心思路：采集 → 存储 → 分析三阶段解耦

### 现有问题

1. 搜索卡片数据太浅：没有 `sold_count`/`want_count`（准确值），缺最重要的成交验证
2. 关键词固化：静态词库 → 越来越窄，没有新赛道发现机制
3. 分析耦合在采集流程里：60 条小样本当场出结论，噪音大
4. 一次性消耗：跑一遍数据就没了，无法回溯、对比、迭代

---

## 新数据流

```
[Phase 1] collect.py (每日)
    宽泛词搜索 → 卡片数据存盘(JSONL)
    ↓ 筛选高已售/高想要
    详情页增强 → enriched store
    ↓
[Phase 2] analyze.py (异步，可晚于采集)
    读取当日 raw + enriched data
    ├── AI 分析高销量内容特征（sonnet，少量调用）
    ├── 自动发现细分关键词 → next_keywords.txt
    ├── 淘汰无效关键词 → 更新 keyword_status
    ├── 注入新宽泛词（定期）→ 防止视野收窄
    └── 生成报告 reports/YYYY-MM-DD.md

[Day N+1] collect.py 使用更新后的关键词运行
```

---

## Phase 1: collect.py — 采集

### 1.1 关键词来源

```
keywords/
├── broad.txt          宽泛种子词（人工维护，定期扩充新赛道）
│                      例: 教程, 资料, 模板, 课程, 笔记, 电子版
├── active.txt         当前活跃词（由 analyze.py 每日更新）
│                      例: Python教程, 考研资料, ComfyUI教程
├── candidates.txt     待验证词（细分建议，尚未确认有效）
└── retired.txt        已淘汰词（invalid/低价值，保留记录）
```

每日运行：`broad.txt` + `active.txt` 全扫，`candidates.txt` 抽样验证。

### 1.2 搜索采集（现有 fetcher 改造）

- 搜索页数：宽泛词 3-5 页（挖更多标题），精确词 2 页
- 存储：`data/raw/YYYY-MM-DD/{keyword_hash}.jsonl`（每行一条商品，含所有卡片字段）
- 词库分类：保留，作为初筛（haiku 兜底未命中项）

### 1.3 详情页增强（新增）

触发条件：从卡片数据中筛选「值得深入」的商品：
- `want_num >= 10`（想要数高）
- 价格在合理区间（¥5-500）且分类为 virtual/weak_virtual
- 卡片标题包含高价值信号词

详情页采集字段：

| 字段 | API/页面路径 | 价值 |
|------|-------------|------|
| sold_count | 详情页「已售 XX 件」 | 验证真实成交 |
| want_count | 详情页「XX 人想要」 | 准确想要数 |
| description | 商品详情描述全文 | 关键词来源 + AI 分析素材 |
| seller_credit | 卖家信用等级 | 判断卖家质量 |
| category | 商品分类标签 | 辅助分类 |
| images | 商品图片URL列表 | 可选：用于多模态分析 |
| comments | 买家留言/评价 | 了解真实需求 |

**反风控策略**：
- 每次详情页间隔 10-20s
- 单次采集不超过 30 个详情页
- 随机化访问顺序
- 检测风控弹窗后立即中止

存储：`data/enriched/YYYY-MM-DD/{item_id}.json`

### 1.4 模型分工

| 任务 | 模型 | 原因 |
|------|------|------|
| 词库未命中分类 | haiku | 简单分类，量大，省钱 |
| 关键词有效性评估 | haiku | 标准化判断，不需要深度推理 |
| 细分建议 | haiku | 同上 |
| 选品分析（Top 5） | sonnet | 需要洞察力，调用量少（5次） |
| 高销量内容深度分析 | sonnet | 核心价值，1-3 次 |
| 新赛道发现 | sonnet | 需要创造力，1 次 |
| 词库学习 | haiku | 结构化提取，量大 |

---

## Phase 2: analyze.py — 异步分析

### 2.1 输入

从 `data/raw/` 和 `data/enriched/` 读取当日数据，不依赖 fetcher。

### 2.2 分析管道

#### A. 缺口分计算（保留 scanner.py 逻辑，增强）

新公式（有 sold_count 时）：
```
gap_score_v2 = demand_posts / max(effective_supply, 1)

effective_supply = virtual_count 中 sold_count > 0 的数量
                   （有人真成交 = 有效竞争者）
```

#### B. 高销量内容分析（sonnet，核心价值）

从 enriched data 中筛出已售 > 5 的商品，送 sonnet 分析：
- 共性特征：标题结构、定价策略、卖点
- 成功模式：什么类型的虚拟商品成交最好
- 空白机会：已售高但竞品少的子领域

#### C. 关键词发现（NLP + AI）

1. **n-gram 统计**：对所有标题做 2-3 gram 词频统计
2. **聚类**：相似标题归组，识别子赛道
3. **AI 评估**：sonnet 评估 Top 10 候选子关键词的市场潜力

#### D. 趋势对比

```
今日缺口分 vs 昨日 → 机会窗口是否还在
本周新品数 vs 上周 → 赛道热度
```

### 2.3 输出

```
reports/YYYY-MM-DD.md             每日报告
keywords/active.txt               更新（新发现的有效词加入，无效词移除）
keywords/candidates.txt           追加（细分建议词，待验证）
keywords/retired.txt              追加（淘汰词）
data/analysis/YYYY-MM-DD.json    完整分析数据（供前端或后续使用）
```

---

## 关键词生命周期管理

```
                ┌──────────────┐
                │  broad.txt   │ ← 人工 + AI 定期注入新赛道
                │  (种子词)     │
                └──────┬───────┘
                       │ 搜索 → 发现子领域
                       ▼
                ┌──────────────┐
                │ candidates   │ ← analyze.py 自动写入
                │  (待验证词)   │
                └──────┬───────┘
                       │ 验证有效（缺口分>0.2 且 相关度>0.7）
                       ▼
                ┌──────────────┐
                │  active.txt  │ ← 每日扫描使用
                │  (活跃词)     │
                └──────┬───────┘
                       │ 连续3天无效/缺口分<0.05
                       ▼
                ┌──────────────┐
                │ retired.txt  │ ← 保留记录，不再扫描
                │  (淘汰词)     │
                └──────────────┘
```

### 防止视野收窄

**定期宽泛词注入**：每周自动从以下来源补充 `broad.txt`：
1. AI 分析当前 active 词，联想相关但尚未覆盖的领域
2. 参考热门平台趋势（手动维护或后期接入）
3. 从退休词中回捞（市场可能变化）

**宽泛词轮换**：broad.txt 中每天只扫一部分（round-robin），避免太多宽泛词拖慢采集。

---

## 实现优先级

### P0 — 先让采集更有价值
1. ✅ 详情页 fetcher（`fetch_detail`）
2. ✅ collect.py（调用 fetcher.search + fetch_detail）
3. ✅ 双模型 config（haiku 采集 / sonnet 分析）

### P1 — 异步分析
4. analyze.py 基础版（读 raw → 缺口分 → 报告）
5. 高销量内容 AI 分析（sonnet）
6. n-gram 关键词发现

### P2 — 生命周期
7. 关键词文件分层（broad/active/candidates/retired）
8. 自动晋升/淘汰逻辑
9. 宽泛词定期注入

---

## 命令接口设计

```bash
# Phase 1: 采集（每日 cron）
python collect.py                       # 默认：broad + active 全扫
python collect.py --broad-only          # 只扫宽泛词（发现模式）
python collect.py --no-detail           # 跳过详情页（快速模式）
python collect.py --detail-limit 20     # 限制详情页数量

# Phase 2: 分析（采集完成后异步运行）
python analyze.py                       # 分析今日数据
python analyze.py --date 2026-04-17     # 分析指定日期
python analyze.py --compare-days 7      # 7天趋势对比

# 兼容：旧版一体化模式（保留 scan.py）
python scan.py                          # 等于 collect + analyze 串行
```

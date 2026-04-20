# 闲鱼市场扫描与商机分析系统 PRD

**文档版本**：v1.0  
**日期**：2026-04-10  
**基础项目**：[ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor)（在其基础上扩展）

---

## 1. 背景与目标

### 1.1 背景

ai-goofish-monitor 现有功能是「单品监控」——用户预设关键词和筛选条件，系统持续监控符合条件的商品并推送通知。该模式适合有明确购买目标的用户，但无法回答「市场上什么东西卖得好、什么东西在快速增长」这类问题。

### 1.2 目标

在现有项目基础上，新增「市场扫描模式」，实现：

1. **热门品类识别**：发现当前闲鱼上交易热度最高的商品类目
2. **增长趋势识别**：发现近期需求快速上升的新兴品类
3. **商机输出**：生成可操作的商机报告，帮助用户决策是否跟进某个品类

### 1.3 不做什么

- 不做自动下单 / 自动出价
- 不做跨平台比价（仅限闲鱼）
- 不替代现有的单品监控功能（并行存在）

---

## 2. 用户画像

| 角色 | 描述 | 核心诉求 |
|------|------|----------|
| 个人二手卖家 | 想知道现在卖什么容易出手 | 热门品类排行 + 合理定价区间 |
| 闲鱼小商家 | 想发现新兴品类抢先入场 | 增长趋势 + 竞争密度分析 |
| 市场调研者 | 想了解二手市场整体趋势 | 数据导出 + 历史趋势图 |

---

## 3. 核心概念定义

### 3.1 信号指标

闲鱼不公开销量，用以下可获取的间接信号构建指标体系：

| 信号字段 | 含义 | 获取位置 |
|----------|------|----------|
| `want_count`（想要人数） | 需求热度 | 列表页、详情页 |
| `sold_count`（已售件数） | 历史成交量 | 部分商品详情页 |
| `listing_count`（发布数量） | 供给密度 | 搜索结果总数 |
| `price`（价格） | 价格分布 | 列表页 |
| `publish_time`（发布时间） | 供给新鲜度 | 列表页 |
| `crawl_time`（采集时间） | 系统记录时间戳 | 系统写入 |

### 3.2 热门度评分（HeatScore）

```
HeatScore = want_count × 0.5 + sold_count × 0.3 + listing_count_normalized × 0.2
```

按关键词聚合后在品类维度排名。

### 3.3 增长率（GrowthRate）

```
GrowthRate = (want_count_now - want_count_24h_ago) / want_count_24h_ago
```

对同一商品 item_id 的前后两次采集数据做差分计算。品类维度取中位数。

---

## 4. 功能规格

### 4.1 扫描任务管理（新增）

#### 4.1.1 扫描关键词库

系统内置约 100 个宽泛品类关键词，用户可自行增删：

- 数码类：手机、平板、耳机、相机、游戏机、显卡、笔记本
- 潮玩类：盲盒、手办、乐高、泡泡玛特、BJD 娃娃
- 母婴类：童车、奶粉、早教玩具、婴儿床
- 服饰类：球鞋、奢侈品包、汉服、vintage 外套
- 家居类：按摩椅、空气炸锅、扫地机器人、咖啡机
- 运动类：自行车、滑板、健身器材、钓鱼竿
- 书籍类：教材、考研资料、绝版书
- 其他：宠物用品、乐器、车品、票券卡密

#### 4.1.2 扫描任务配置

```
扫描任务字段：
- task_name: string          // 任务名称
- keywords: list[string]     // 关键词列表，从内置库选择或自定义
- scan_interval_hours: int   // 扫描周期，默认 6h
- max_items_per_keyword: int // 每关键词最多采集条数，默认 100
- price_min / price_max: int // 价格区间过滤，可选
- enabled: bool              // 是否启用
```

#### 4.1.3 调度逻辑

- 每个扫描任务按 cron 周期触发，遍历关键词列表
- 每个关键词串行执行，抓取前 N 页（默认 5 页，约 100 条）
- 使用现有账号/代理轮换机制，与单品监控任务隔离
- 采集结果写入新表 `market_items`，不污染现有 `monitor_results`

---

### 4.2 数据采集层（扩展 spider_v2.py）

新增采集模式 `--mode market`：

```
采集字段：
- item_id          // 商品唯一 ID（用于跨时间去重）
- title            // 标题
- desc             // 描述（截取前 200 字）
- price            // 价格
- want_count       // 想要人数
- sold_count       // 已售（若有）
- seller_id        // 卖家 ID
- location         // 发布地
- publish_time     // 发布时间
- images[0]        // 首图 URL（不下载，仅存 URL）
- search_keyword   // 触发采集的关键词
- crawl_time       // 本次采集时间戳
```

去重策略：同一 `item_id` 在同一采集周期内只存一条；跨周期保留历史记录（用于趋势计算）。

---

### 4.3 语义归类（新增分析 Pipeline）

#### 4.3.1 归类流程

每次扫描结束后，对新增商品批量调用 AI（复用现有 OpenAI-compatible 接口）：

**输入**（每批 50 条）：
```json
[
  {"item_id": "xxx", "title": "95新 iPhone 14 Pro 256G 深空黑", "price": 3800},
  ...
]
```

**Prompt 设计**：
```
你是闲鱼二手市场分析师。对以下商品列表进行归类，输出 JSON。

归类要求：
1. category_l1: 一级品类（如：数码、潮玩、母婴）
2. category_l2: 二级品类（如：智能手机、盲盒、童车）
3. sub_tag: 细分标签，最能描述商品特征的 2-3 个词（如：iPhone14系列、二手教材、限定款）
4. condition: 新旧程度（全新/9成新/8成新/拆封/配件）
5. is_service: 是否为服务类商品（代练、设计、回收等）

输出格式：[{"item_id": "...", "category_l1": "...", "category_l2": "...", "sub_tag": "...", "condition": "...", "is_service": false}]
只输出 JSON，不输出其他内容。
```

#### 4.3.2 归类结果存储

归类结果写入 `market_items` 表的对应字段，首次归类后不重复调用（节省 token）。

---

### 4.4 分析聚合（新增）

新增后台分析任务，每 6 小时运行一次，生成以下聚合视图并存入 `market_analysis` 表：

#### 4.4.1 热门榜

```sql
-- 按 category_l2 聚合，过去 24h 采集数据
SELECT 
  category_l2,
  COUNT(DISTINCT item_id) AS listing_count,
  AVG(price) AS avg_price,
  PERCENTILE(price, 0.25) AS price_p25,
  PERCENTILE(price, 0.75) AS price_p75,
  SUM(want_count) AS total_want,
  AVG(want_count) AS avg_want
FROM market_items
WHERE crawl_time >= NOW() - 24h
GROUP BY category_l2
ORDER BY total_want DESC
LIMIT 50
```

#### 4.4.2 增长榜

对比当前周期与上一周期的 `want_count` 差值：

```
growth_rate = (sum_want_now - sum_want_prev) / sum_want_prev
```

只展示 `listing_count >= 10` 的品类（过滤噪音），按 `growth_rate` 降序排列前 20。

#### 4.4.3 价格带分析

对热门 Top 20 品类，输出价格区间分布（<500、500-1000、1000-3000、3000+），用于判断市场主流价位。

---

### 4.5 Dashboard（新增页面）

在现有 Web UI 基础上，新增「市场洞察」页面，包含以下模块：

#### 4.5.1 热门榜卡片

- 展示 Top 20 品类
- 字段：品类名、挂牌数、平均想要数、价格区间
- 支持按「挂牌数」「想要数」「平均价格」切换排序

#### 4.5.2 增长榜卡片

- 展示增长率 Top 10 品类
- 显示增长率百分比 + 对比基准周期
- 标注「新品类」（首次出现在榜单）

#### 4.5.3 趋势折线图

- 选择某一品类，查看过去 7 天的「想要数」趋势
- 横轴：时间，纵轴：该品类日均想要数总量

#### 4.5.4 商品样例

- 点击任意品类，展示该品类最近 20 条代表性商品
- 字段：图片（URL）、标题、价格、想要数、发布时间

#### 4.5.5 数据导出

- 支持导出当前榜单为 CSV
- 支持导出某品类全量采集数据为 CSV

---

### 4.6 扫描任务管理 UI（扩展现有任务管理页）

在「任务管理」页新增 Tab「市场扫描任务」：

| 字段 | 控件类型 |
|------|----------|
| 任务名称 | 文本输入 |
| 关键词 | 多选标签（从内置库选）+ 自定义输入 |
| 扫描周期 | 下拉（1h / 3h / 6h / 12h / 24h） |
| 每词采集数量 | 数字输入（10-200） |
| 价格区间 | 范围输入，可选 |
| 状态 | 启用/暂停开关 |
| 操作 | 立即运行 / 编辑 / 删除 |

---

## 5. 数据库设计

### 5.1 新增表：market_items

```sql
CREATE TABLE market_items (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id         TEXT NOT NULL,           -- 闲鱼商品 ID
  crawl_time      DATETIME NOT NULL,       -- 采集时间
  search_keyword  TEXT,                    -- 触发采集的关键词
  title           TEXT,
  description     TEXT,
  price           REAL,
  want_count      INTEGER DEFAULT 0,
  sold_count      INTEGER DEFAULT 0,
  seller_id       TEXT,
  location        TEXT,
  publish_time    DATETIME,
  image_url       TEXT,                    -- 首图 URL
  -- AI 归类结果
  category_l1     TEXT,
  category_l2     TEXT,
  sub_tag         TEXT,
  condition_grade TEXT,
  is_service      BOOLEAN DEFAULT FALSE,
  classified_at   DATETIME,               -- 归类时间，NULL 表示待处理
  -- 索引
  UNIQUE(item_id, crawl_time)             -- 同一商品同一采集时间只存一条
);

CREATE INDEX idx_market_crawl_time ON market_items(crawl_time);
CREATE INDEX idx_market_category ON market_items(category_l2, crawl_time);
```

### 5.2 新增表：market_analysis

```sql
CREATE TABLE market_analysis (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_time   DATETIME NOT NULL,
  category_l1     TEXT,
  category_l2     TEXT,
  period_hours    INTEGER DEFAULT 24,      -- 统计周期
  listing_count   INTEGER,
  total_want      INTEGER,
  avg_want        REAL,
  avg_price       REAL,
  price_p25       REAL,
  price_p75       REAL,
  growth_rate     REAL,                    -- 与上一周期对比
  is_new_category BOOLEAN DEFAULT FALSE    -- 是否首次上榜
);

CREATE INDEX idx_analysis_time ON market_analysis(analysis_time, category_l2);
```

### 5.3 新增表：scan_tasks（市场扫描任务配置）

```sql
CREATE TABLE scan_tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_name       TEXT NOT NULL UNIQUE,
  keywords        TEXT NOT NULL,           -- JSON array
  interval_hours  INTEGER DEFAULT 6,
  max_per_keyword INTEGER DEFAULT 100,
  price_min       REAL,
  price_max       REAL,
  enabled         BOOLEAN DEFAULT TRUE,
  last_run_at     DATETIME,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 6. 技术架构

### 6.1 整体架构（在现有基础上扩展）

```
现有模块（不修改）
├── spider_v2.py          监控爬虫
├── src/app.py            FastAPI 主服务
├── web-ui/               Vue 前端
└── data/app.sqlite3      SQLite 主库

新增模块
├── spider_market.py      市场扫描爬虫（复用 spider_v2 的登录/代理/翻页逻辑）
├── src/
│   ├── market_analyzer.py   聚合分析定时任务
│   ├── classifier.py        AI 语义归类服务
│   └── routers/
│       └── market.py        市场洞察相关 API
└── web-ui/src/views/
    └── MarketInsight.vue    市场洞察页面
```

### 6.2 关键技术决策

| 问题 | 决策 | 理由 |
|------|------|------|
| 爬虫复用 | 直接复用 spider_v2 的 Playwright 实例和账号管理 | 避免重复维护反爬逻辑 |
| AI 归类调用 | 批量（50条/次），异步队列执行 | 控制 API 费用，不阻塞采集 |
| 数据存储 | 沿用 SQLite，新增表 | 与现有数据隔离，无需迁移 |
| 增长计算 | 纯 SQL 差分，无需额外计算引擎 | 数据量在 SQLite 可承受范围内 |
| 前端图表 | 复用项目现有 UI 框架，引入 ECharts | 与现有界面风格一致 |

---

## 7. API 设计

### 7.1 新增 API 端点

```
GET  /api/market/hot-categories         热门品类榜单
     query: period_hours=24, limit=20, sort_by=total_want

GET  /api/market/growing-categories     增长趋势榜单
     query: period_hours=24, limit=20

GET  /api/market/category-trend         品类趋势折线数据
     query: category_l2=手机, days=7

GET  /api/market/category-items         品类商品样例
     query: category_l2=手机, limit=20

GET  /api/market/export/csv             导出数据
     query: type=hot|items, category_l2=手机

GET  /api/scan-tasks                    获取所有扫描任务
POST /api/scan-tasks                    创建扫描任务
PUT  /api/scan-tasks/{id}               更新扫描任务
DEL  /api/scan-tasks/{id}               删除扫描任务
POST /api/scan-tasks/{id}/run           立即运行扫描任务
```

---

## 8. 开发任务拆解

### Phase 1：数据采集（预计 3 天）

- [ ] 创建 `market_items` / `market_analysis` / `scan_tasks` 三张表及迁移脚本
- [ ] 编写 `spider_market.py`：复用 spider_v2 登录/代理逻辑，新增批量关键词采集模式
- [ ] 实现 `scan_tasks` 的 CRUD API 和定时调度接入（复用现有 APScheduler）
- [ ] 补充内置关键词库（约 100 个）

### Phase 2：分析 Pipeline（预计 2 天）

- [ ] 编写 `classifier.py`：批量 AI 归类，含 retry 和 token 用量记录
- [ ] 编写 `market_analyzer.py`：热门榜、增长榜、价格带聚合逻辑
- [ ] 注册分析任务到 APScheduler，每 6 小时触发一次

### Phase 3：Dashboard（预计 3 天）

- [ ] 实现所有 `/api/market/` 端点
- [ ] 编写 `MarketInsight.vue` 页面：热门榜、增长榜、趋势图、商品样例
- [ ] 实现 CSV 导出
- [ ] 在现有导航栏新增「市场洞察」入口

### Phase 4：测试与调优（预计 2 天）

- [ ] 端到端测试：从扫描任务触发到 Dashboard 展示数据
- [ ] AI 归类准确率验证（手动抽查 100 条）
- [ ] 性能测试：SQLite 在 10 万条数据量下聚合查询响应时间
- [ ] 调整采集频率和 token 用量，控制运行成本

---

## 9. 风险与限制

| 风险 | 影响 | 应对 |
|------|------|------|
| 闲鱼反爬升级导致采集中断 | 数据断档 | 复用现有账号轮换机制；增加失败告警 |
| AI 归类准确率不足 | 品类分析失准 | 设置置信度阈值，低于阈值归为「其他」并人工复核 |
| `want_count` 采集不到 | 增长率无法计算 | 降级使用 `listing_count` 作为替代指标 |
| SQLite 写入性能瓶颈 | 大规模采集时慢 | 批量写入（100条/次）；超 50 万条考虑迁移 PostgreSQL |
| AI 分类 API 费用过高 | 成本不可控 | 每条商品只归类一次；设置每日 token 用量上限告警 |

---

## 10. 成功指标

| 指标 | 目标值 |
|------|--------|
| 热门榜品类覆盖率 | 涵盖闲鱼 Top 50 品类 |
| AI 归类准确率 | ≥ 85%（手动抽样验证） |
| 增长率计算数据覆盖 | ≥ 70% 品类有前后两周期对比数据 |
| Dashboard 数据延迟 | 采集结束后 ≤ 30 分钟展示最新数据 |
| 单次扫描运行时长 | 100 个关键词 × 100 条 ≤ 2 小时 |

---

*文档结束*

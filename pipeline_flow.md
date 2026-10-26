# 管道流程图

同一张图的两种形态：

- `pipeline_flow.png` — 中文版，可直接贴进提案或文档
- 下面的 Mermaid 源码 — GitHub / Notion / Obsidian 可直接渲染

## Mermaid

```mermaid
flowchart TD
    A["① GoHighLevel v2 API<br/>contacts / opportunities<br/>游标分页 · 200 页上限"]
    B["② fetch() 三种数据源<br/>API（有 token）<br/>data/export.json（离线）<br/>合成样本（无凭证）"]
    subgraph C["③ 清洗 validate()"]
        C1["去重<br/>邮箱优先 · 电话兜底"]
        C2["补缺失<br/>email / phone / stage"]
        C3["剔坏数据<br/>坏日期 · 孤儿商机"]
    end
    D["④ upsert()<br/>ON CONFLICT(id) DO UPDATE<br/>第二次运行新增 0 行"]
    E[("SQLite · ghl.db<br/>contacts / opportunities / run_log")]
    F["⑤ analyze()<br/>Claude API（有 key）<br/>确定性规则（无 key）"]
    G["⑥ out/report_YYYY-MM-DD.md<br/>关键指标 · 质检 · 异常 · AI 观察"]
    H["out/dashboard.html<br/>KPI 卡片 · 阶段分布"]

    A --> B --> C1
    C1 --> C2 --> C3
    C3 --> D --> E --> F
    F --> G
    F --> H

    classDef api fill:#e8f0fe,stroke:#4a6fa5,color:#1e1e1e
    classDef clean fill:#fff3e0,stroke:#c47904,color:#1e1e1e
    classDef store fill:#f0f1f4,stroke:#80858f,color:#1e1e1e
    classDef out fill:#e8f5e9,stroke:#2e7d32,color:#1e1e1e
    class A,B,F api
    class C1,C2,C3 clean
    class E store
    class G,H out
```

## 每个方框对应代码里的哪一段

| 图上的方框 | 代码位置 |
|---|---|
| ① API / ② 数据源 | `fetch_from_ghl()` / `fetch_from_export()` / `fetch_synthetic()` |
| ③ 清洗 | `validate()` |
| ④ 幂等入库 | `upsert()` |
| ⑤ 分析 | `analyze()` |
| ⑥ 输出 | `build_report()` / `build_dashboard()` |

## 三个方框是这张图的重点

**③ 清洗** —— 决定报告能不能用。没有这一步，下面的数字是错的。

**④ 幂等** —— 决定这个管道能不能无人值守地跑。没有这一步，重试就会写重。

**⑤ 降级** —— 决定它在依赖失败时会不会停。没有这一步，模型 API 一挂，当天就没有报告。

其余都是搬砖。

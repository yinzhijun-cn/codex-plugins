---
name: elk-log-query
description: 查询 ELK 日志并总结异常链路。适用于按服务、traceId、订单号、关键字和时间范围排查线上问题
metadata:
  short-description: 查询 ELK 日志
---

# ELK 日志查询

## 适用场景

- 按服务名查询最近一段时间的异常日志
- 按 `traceId` 还原完整调用链路
- 按订单号、运单号、业务号等关键字定位相关日志
- 对查询结果做归纳，总结报错原因、触发条件和影响范围

## 前置条件

- 已在 `configs/environments.json` 中配置好各环境的 ELK 地址、账号和索引模式
- 如果环境入口是 Kibana 而不是 Elasticsearch，可将 `accessMode` 配成 `kibana_proxy`，并通过 `kibanaProxyPath` 指定代理路径
- 如果不显式传 `environment`，脚本会读取 `ELK_DEFAULT_ENVIRONMENT` 作为默认环境
- 如果某些环境无需认证，可将对应环境的 `username`、`password` 留空

## 建议工作流

### 第一步

先明确查询目标，至少补齐下面几个维度中的两个：

- 环境名
- 服务名
- 关键字
- traceId
- 订单号或业务号
- 时间范围

### 第二步

优先缩小时间范围，再执行查询，避免一次拉取过多日志：

- 临时排查优先查最近 15 分钟到 2 小时
- 已知故障时间点时，优先围绕故障点前后 10 分钟查询
- 只有订单号没有时间范围时，先查最近 24 小时，再按结果收窄

### 第三步

用下面的命令调用脚本：

```bash
python3 ./plugins/elk-log-query/scripts/query_elk_logs.py \
  --cli \
  --environment dev01 \
  --service order-service \
  --keyword error \
  --minutes 30 \
  --size 50
```

按 `traceId` 查询：

```bash
python3 ./plugins/elk-log-query/scripts/query_elk_logs.py \
  --cli \
  --environment test02 \
  --trace-id TRACE_ID \
  --minutes 120 \
  --size 200
```

按订单号查询：

```bash
python3 ./plugins/elk-log-query/scripts/query_elk_logs.py \
  --cli \
  --environment prod \
  --keyword ORDER_NO \
  --minutes 120 \
  --size 100
```

## 输出要求

- 先给出命中的总条数和时间范围
- 明确标记当前查询环境
- 再给出最关键的异常日志片段
- 最后总结根因、影响服务和建议排查方向

## 注意事项

- 如果没有时间范围，不要直接查全量索引
- 排查生产问题时务必显式传 `environment=prod`
- 如果第一次无结果，先确认索引模式、服务字段名和时间范围是否正确
- 如果用户只给了模糊描述，先补齐查询条件，再执行脚本

---
name: mysql-query
description: 查询 MySQL 数据并总结结果。适用于按环境执行只读 SQL、查看表结构、核对订单或业务数据
metadata:
  short-description: 查询 MySQL 数据
---

# MySQL 数据查询

## 适用场景

- 按环境执行 `SELECT` 查询
- 查看库表结构，例如 `SHOW TABLES`、`DESCRIBE table_name`
- 按订单号、业务号、主键等条件核对数据
- 查询后总结关键字段、异常数据和下一步排查方向

## 前置条件

- 已在 `configs/environments.json` 中配置好各环境的 MySQL 地址、账号和默认库
- 如果不显式传 `environment`，脚本会读取 `MYSQL_QUERY_DEFAULT_ENVIRONMENT`
- 当前脚本使用 `PyMySQL` 连接 MySQL，本机未安装时默认提示手动安装：`python3 -m pip install PyMySQL`
- 如需自动安装依赖，可在 CLI 传 `--auto-install-deps`，MCP 调用传 `autoInstallDeps: true`，或配置 `MYSQL_QUERY_AUTO_INSTALL_DEPS=true`
- 默认只允许只读 SQL：`SELECT`、`SHOW`、`DESCRIBE`、`DESC`、`EXPLAIN`、`WITH`

## 建议工作流

### 第一步

先明确查询目标，至少补齐下面几个维度中的两个：

- 环境名
- 数据库名
- 表名
- 主键、订单号、业务号或其他查询条件
- 需要返回的字段

### 第二步

优先缩小查询范围，避免全表扫描：

- 查询业务数据时必须带明确条件
- 查询列表时优先加 `LIMIT`
- 不确定字段时先 `DESCRIBE table_name`
- 不确定库表时先 `SHOW TABLES`

### 第三步

用下面的命令调用脚本：

```bash
python3 ./plugins/mysql-query/scripts/query_mysql.py \
  --cli \
  --environment dev \
  --sql "SHOW TABLES" \
  --limit 50
```

首次运行且本机缺少 `PyMySQL` 时，可显式允许自动安装：

```bash
python3 ./plugins/mysql-query/scripts/query_mysql.py \
  --cli \
  --environment dev \
  --sql "SELECT 1 AS connection_ok" \
  --auto-install-deps
```

按业务号查询：

```bash
python3 ./plugins/mysql-query/scripts/query_mysql.py \
  --cli \
  --environment test \
  --database app_order \
  --sql "SELECT id, order_no, status FROM t_order WHERE order_no = 'ORDER_NO'" \
  --limit 20
```

列出环境：

```bash
python3 ./plugins/mysql-query/scripts/query_mysql.py --cli --list-environments
```

## 输出要求

- 先给出查询环境、数据库和返回行数
- 再用表格展示关键字段
- 最后总结数据是否符合预期、还需要继续核对哪些表

## 注意事项

- 不要执行写 SQL，除非用户明确要求并确认环境允许
- 生产查询必须显式传 `environment=prod`
- 自动安装依赖会调用当前 Python 解释器执行 `python3 -m pip install PyMySQL`，需要网络和 pip 可用
- SQL 中不要拼接不可信输入
- 如果查询无结果，先确认环境、数据库、表名和查询条件是否正确

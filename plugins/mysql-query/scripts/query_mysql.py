#!/usr/bin/env python3
"""MySQL 查询脚本

默认以 MCP Server 模式运行
传入 --cli 时，以命令行模式执行一次性查询
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_READ_TIMEOUT_SECONDS = 30
DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 120
SERVER_NAME = "mysql-query"
SERVER_VERSION = "0.1.0"
TOOL_QUERY_NAME = "query_mysql"
TOOL_LIST_ENVIRONMENTS_NAME = "list_mysql_environments"
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT_DIR = SCRIPT_DIR.parent
ENVIRONMENTS_FILE_PATH = PLUGIN_ROOT_DIR / "configs" / "environments.json"
READONLY_SQL_PREFIXES = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}
WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "TRUNCATE",
    "RENAME", "GRANT", "REVOKE", "CALL", "LOAD", "LOCK", "UNLOCK", "SET", "USE",
    "ANALYZE", "OPTIMIZE", "REPAIR", "FLUSH", "KILL",
}


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="查询 MySQL 数据，支持 MCP Server 模式和命令行模式"
    )
    parser.add_argument("--cli", action="store_true", help="以命令行模式运行")
    parser.add_argument("--list-environments", action="store_true", help="列出已配置环境")
    parser.add_argument("--environment", help="环境名称，默认读取 MYSQL_QUERY_DEFAULT_ENVIRONMENT")
    parser.add_argument("--database", help="覆盖环境配置中的默认数据库")
    parser.add_argument("--sql", help="只读 SQL")
    parser.add_argument("--limit", type=int, default=read_default_limit(), help="返回行数上限")
    parser.add_argument("--raw", action="store_true", help="输出 JSON 原始结果")
    parser.add_argument("--auto-install-deps", action="store_const", const=True, default=None, help="缺少 PyMySQL 时自动安装依赖")
    return parser.parse_args()


def main() -> int:
    """程序入口"""
    args = parse_args()
    if args.cli:
        return run_cli(args)
    return run_mcp_server()


def run_cli(args: argparse.Namespace) -> int:
    """执行命令行查询"""
    if args.list_environments:
        print(render_environments(list_environment_summaries()))
        return 0

    if not args.sql:
        print("错误：缺少 --sql，或使用 --list-environments 查看环境", file=sys.stderr)
        return 2

    result = execute_query(
        environment=args.environment,
        database=args.database,
        sql=args.sql,
        limit=args.limit,
        auto_install_deps=args.auto_install_deps,
    )
    if args.raw:
        print(json.dumps(result, ensure_ascii=False, default=json_default, indent=2))
        return 0
    print(render_query_result(result))
    return 0


def run_mcp_server() -> int:
    """以 MCP 协议循环处理请求"""
    while True:
        message = read_mcp_message()
        if message is None:
            return 0

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            write_mcp_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            write_mcp_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            build_query_tool_definition(),
                            build_list_environments_tool_definition(),
                        ]
                    },
                }
            )
            continue

        if method == "tools/call":
            write_mcp_message(handle_tool_call(message))
            continue

        if request_id is not None:
            write_mcp_message(build_error_response(request_id, -32601, f"不支持的方法: {method}"))


def build_query_tool_definition() -> Dict[str, object]:
    """构建查询工具定义"""
    return {
        "name": TOOL_QUERY_NAME,
        "description": "按环境执行只读 MySQL SQL 查询",
        "inputSchema": {
            "type": "object",
            "properties": {
                "environment": {
                    "type": "string",
                    "description": "环境名称，默认读取 MYSQL_QUERY_DEFAULT_ENVIRONMENT",
                },
                "database": {
                    "type": "string",
                    "description": "覆盖环境配置中的默认数据库",
                },
                "sql": {
                    "type": "string",
                    "description": "只读 SQL，支持 SELECT、SHOW、DESCRIBE、DESC、EXPLAIN、WITH",
                },
                "limit": {
                    "type": "integer",
                    "description": f"返回行数上限，默认 {DEFAULT_LIMIT}，最大 {MAX_LIMIT}",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
                "raw": {
                    "type": "boolean",
                    "description": "是否返回 JSON 原始结果",
                },
                "autoInstallDeps": {
                    "type": "boolean",
                    "description": "缺少 PyMySQL 时是否自动安装依赖，默认读取 MYSQL_QUERY_AUTO_INSTALL_DEPS",
                },
            },
            "required": ["sql"],
        },
    }


def build_list_environments_tool_definition() -> Dict[str, object]:
    """构建环境列表工具定义"""
    return {
        "name": TOOL_LIST_ENVIRONMENTS_NAME,
        "description": "列出已配置的 MySQL 查询环境",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    }


def handle_tool_call(message: Dict[str, object]) -> Dict[str, object]:
    """处理 MCP 工具调用"""
    request_id = message.get("id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return build_tool_error_response(request_id, "工具参数必须是对象")

    tool_name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if tool_name == TOOL_LIST_ENVIRONMENTS_NAME:
            text = render_environments(list_environment_summaries())
            return build_tool_text_response(request_id, text)

        if tool_name != TOOL_QUERY_NAME:
            return build_tool_error_response(request_id, f"未知工具：{tool_name}")

        sql = trim_to_none(arguments.get("sql"))
        if not sql:
            return build_tool_error_response(request_id, "缺少 sql 参数")

        result = execute_query(
            environment=trim_to_none(arguments.get("environment")),
            database=trim_to_none(arguments.get("database")),
            sql=sql,
            limit=normalize_limit(arguments.get("limit")),
            auto_install_deps=arguments.get("autoInstallDeps") if "autoInstallDeps" in arguments else None,
        )
        if arguments.get("raw") is True:
            text = json.dumps(result, ensure_ascii=False, default=json_default, indent=2)
        else:
            text = render_query_result(result)
        return build_tool_text_response(request_id, text)
    except RuntimeError as error:
        return build_tool_error_response(request_id, str(error))


def execute_query(
    environment: Optional[str],
    database: Optional[str],
    sql: str,
    limit: int,
    auto_install_deps: Optional[bool] = None,
) -> Dict[str, object]:
    """执行 MySQL 查询并返回结果"""
    environment_config = resolve_environment_config(environment)
    effective_database = database or environment_config.get("database")
    normalized_sql = normalize_sql(sql, environment_config.get("allow_write") is True, limit)

    pymysql, dict_cursor = load_mysql_driver(auto_install_deps)

    try:
        connection = pymysql.connect(
            host=environment_config["host"],
            port=environment_config["port"],
            user=environment_config["username"],
            password=environment_config.get("password") or "",
            database=effective_database,
            charset=environment_config["charset"],
            connect_timeout=environment_config["connect_timeout_seconds"],
            read_timeout=environment_config["read_timeout_seconds"],
            cursorclass=dict_cursor,
            autocommit=True,
        )
    except Exception as error:
        raise RuntimeError(f"连接 MySQL 失败：{error}") from error

    try:
        with connection.cursor() as cursor:
            cursor.execute(normalized_sql)
            rows = cursor.fetchmany(limit)
            columns = [column[0] for column in cursor.description or []]
    except Exception as error:
        raise RuntimeError(f"执行 SQL 失败：{error}") from error
    finally:
        connection.close()

    return {
        "environment": environment_config["name"],
        "database": effective_database,
        "limit": limit,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "sql": normalized_sql,
    }


def load_mysql_driver(auto_install_deps: Optional[bool]) -> Tuple[object, object]:
    """加载 MySQL 驱动，必要时按显式开关自动安装"""
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        return pymysql, DictCursor
    except ModuleNotFoundError as error:
        if not should_auto_install_deps(auto_install_deps):
            raise RuntimeError(
                "缺少 PyMySQL 依赖，请先执行：python3 -m pip install PyMySQL；"
                "如需自动安装，可传 --auto-install-deps 或配置 MYSQL_QUERY_AUTO_INSTALL_DEPS=true"
            ) from error

    install_python_package("PyMySQL")
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        return pymysql, DictCursor
    except ModuleNotFoundError as error:
        raise RuntimeError("已尝试自动安装 PyMySQL，但当前 Python 仍无法导入 pymysql") from error


def should_auto_install_deps(auto_install_deps: Optional[bool]) -> bool:
    """判断是否允许自动安装依赖"""
    if auto_install_deps is not None:
        return auto_install_deps
    return os.getenv("MYSQL_QUERY_AUTO_INSTALL_DEPS", "").strip().lower() in {"1", "true", "yes", "on"}


def install_python_package(package_name: str) -> None:
    """使用当前 Python 解释器安装依赖"""
    command = [sys.executable, "-m", "pip", "install", package_name]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"自动安装 {package_name} 超时，请手动执行：{format_command(command)}") from error

    if result.returncode != 0:
        detail = compact_install_error(result.stderr or result.stdout)
        raise RuntimeError(f"自动安装 {package_name} 失败，请手动执行：{format_command(command)}\n{detail}")


def format_command(command: Sequence[str]) -> str:
    """格式化命令用于错误提示"""
    return " ".join(command)


def compact_install_error(text: str) -> str:
    """压缩安装错误，避免输出过长"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-8:])


def normalize_sql(sql: str, allow_write: bool, limit: int) -> str:
    """校验并规范化 SQL"""
    normalized = strip_single_trailing_semicolon(sql.strip())
    if not normalized:
        raise RuntimeError("SQL 不能为空")
    if ";" in normalized:
        raise RuntimeError("只允许执行单条 SQL")

    keyword = read_first_keyword(normalized)
    if not keyword:
        raise RuntimeError("无法识别 SQL 类型")

    if allow_write:
        return normalized

    if keyword not in READONLY_SQL_PREFIXES:
        raise RuntimeError("当前环境只允许执行只读 SQL")

    upper_sql = normalized.upper()
    for write_keyword in WRITE_KEYWORDS:
        if re.search(rf"\b{write_keyword}\b", upper_sql):
            raise RuntimeError(f"当前环境只允许只读 SQL，禁止关键字：{write_keyword}")
    if re.search(r"\bFOR\s+UPDATE\b|\bLOCK\s+IN\s+SHARE\s+MODE\b|\bINTO\s+OUTFILE\b", upper_sql):
        raise RuntimeError("当前环境只允许无锁、无导出的只读 SQL")

    if keyword in {"SELECT", "WITH"} and not re.search(r"\bLIMIT\s+\d+\b", upper_sql):
        return f"{normalized} LIMIT {limit}"
    return normalized


def strip_single_trailing_semicolon(sql: str) -> str:
    """去掉单个结尾分号"""
    if sql.endswith(";"):
        return sql[:-1].strip()
    return sql


def read_first_keyword(sql: str) -> Optional[str]:
    """读取 SQL 第一个关键字"""
    match = re.match(r"^\s*([a-zA-Z]+)", sql)
    if not match:
        return None
    return match.group(1).upper()


def resolve_environment_config(environment: Optional[str]) -> Dict[str, object]:
    """解析环境配置"""
    environments = load_environment_configs()
    environment_name = environment or os.getenv("MYSQL_QUERY_DEFAULT_ENVIRONMENT", "").strip()
    if not environment_name:
        raise RuntimeError("缺少环境参数，请传 environment 或配置 MYSQL_QUERY_DEFAULT_ENVIRONMENT")
    if environment_name not in environments:
        raise RuntimeError(
            "未知环境：{0}，可选环境：{1}".format(
                environment_name,
                ", ".join(sorted(environments.keys())),
            )
        )

    environment_config = environments[environment_name]
    return {
        "name": environment_name,
        "host": read_config_value(environment_config, "host"),
        "port": read_port(environment_config),
        "username": read_config_value(environment_config, "username"),
        "password": read_optional_config_value(environment_config, "password"),
        "database": read_config_value(environment_config, "database"),
        "charset": read_optional_config_value(environment_config, "charset") or "utf8mb4",
        "connect_timeout_seconds": read_timeout(environment_config, "connectTimeoutSeconds", DEFAULT_CONNECT_TIMEOUT_SECONDS),
        "read_timeout_seconds": read_timeout(environment_config, "readTimeoutSeconds", DEFAULT_READ_TIMEOUT_SECONDS),
        "allow_write": environment_config.get("allowWrite") is True,
    }


def load_environment_configs() -> Dict[str, Dict[str, object]]:
    """加载环境配置文件"""
    if not ENVIRONMENTS_FILE_PATH.exists():
        raise RuntimeError(f"缺少环境配置文件：{ENVIRONMENTS_FILE_PATH}")
    with ENVIRONMENTS_FILE_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)
    environments = config.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise RuntimeError("环境配置文件缺少 environments 配置")
    return environments


def list_environment_summaries() -> List[Dict[str, object]]:
    """列出环境摘要"""
    environments = load_environment_configs()
    summaries = []
    for name, environment_config in sorted(environments.items()):
        summaries.append(
            {
                "name": name,
                "host": read_optional_config_value(environment_config, "host") or "",
                "port": environment_config.get("port") or 3306,
                "database": read_optional_config_value(environment_config, "database") or "",
                "allowWrite": environment_config.get("allowWrite") is True,
            }
        )
    return summaries


def read_config_value(environment_config: Dict[str, object], key: str) -> str:
    """读取必填配置"""
    value = environment_config.get(key)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.startswith("[TODO:"):
            raise RuntimeError(f"环境配置字段 {key} 还是占位值，请先替换为真实配置")
        return normalized
    raise RuntimeError(f"环境配置缺少字段 {key}")


def read_optional_config_value(environment_config: Dict[str, object], key: str) -> Optional[str]:
    """读取可选配置"""
    value = environment_config.get(key)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.startswith("[TODO:"):
            return None
        return normalized
    return None


def read_port(environment_config: Dict[str, object]) -> int:
    """读取端口配置"""
    value = environment_config.get("port")
    if isinstance(value, int) and 0 < value <= 65535:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
        if 0 < port <= 65535:
            return port
    raise RuntimeError("环境配置字段 port 必须是合法端口")


def read_timeout(environment_config: Dict[str, object], key: str, default: int) -> int:
    """读取超时时间"""
    value = environment_config.get(key)
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def normalize_limit(value: object) -> int:
    """规范化返回行数限制"""
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip().isdigit():
        limit = int(value.strip())
    else:
        limit = read_default_limit()
    if limit < 1:
        return 1
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def read_default_limit() -> int:
    """读取默认返回行数"""
    value = os.getenv("MYSQL_QUERY_DEFAULT_LIMIT", "").strip()
    if value.isdigit():
        return normalize_limit(int(value))
    return DEFAULT_LIMIT


def render_environments(environments: Sequence[Dict[str, object]]) -> str:
    """渲染环境列表"""
    lines = ["已配置 MySQL 环境："]
    for environment in environments:
        allow_write = "是" if environment.get("allowWrite") else "否"
        lines.append(
            "- {name} host={host} port={port} database={database} allowWrite={allow_write}".format(
                name=environment.get("name", ""),
                host=environment.get("host", ""),
                port=environment.get("port", ""),
                database=environment.get("database", ""),
                allow_write=allow_write,
            )
        )
    return "\n".join(lines)


def render_query_result(result: Dict[str, object]) -> str:
    """渲染查询结果"""
    rows = result.get("rows")
    columns = result.get("columns")
    if not isinstance(rows, list):
        rows = []
    if not isinstance(columns, list):
        columns = []

    lines = [
        f"查询环境：{result.get('environment', '')}",
        f"数据库：{result.get('database', '')}",
        f"返回行数：{result.get('row_count', 0)}",
    ]

    if not columns:
        return "\n".join(lines)

    table_lines = render_table(columns, rows)
    lines.append("")
    lines.extend(table_lines)
    return "\n".join(lines)


def render_table(columns: Sequence[str], rows: Sequence[Dict[str, object]]) -> List[str]:
    """渲染简单表格"""
    normalized_rows = [
        [format_cell(row.get(column)) for column in columns]
        for row in rows
    ]
    widths = []
    for index, column in enumerate(columns):
        cell_widths = [display_width(row[index]) for row in normalized_rows]
        widths.append(min(max([display_width(column)] + cell_widths), 80))

    header = " | ".join(trim_display(column, widths[index]).ljust(widths[index]) for index, column in enumerate(columns))
    separator = "-+-".join("-" * width for width in widths)
    lines = [header, separator]
    for row in normalized_rows:
        lines.append(" | ".join(trim_display(cell, widths[index]).ljust(widths[index]) for index, cell in enumerate(row)))
    return lines


def format_cell(value: object) -> str:
    """格式化单元格"""
    if value is None:
        return "NULL"
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def trim_display(value: str, max_width: int) -> str:
    """截断过长展示值"""
    if display_width(value) <= max_width:
        return value
    return value[: max_width - 1] + "..."


def display_width(value: str) -> int:
    """计算展示宽度"""
    return len(value)


def trim_to_none(value: object) -> Optional[str]:
    """字符串去空白"""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def json_default(value: object) -> object:
    """JSON 序列化兜底"""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def build_tool_text_response(request_id: object, text: str) -> Dict[str, object]:
    """构建 MCP 文本响应"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": text,
                }
            ]
        },
    }


def build_error_response(request_id: object, code: int, message: str) -> Dict[str, object]:
    """构建 MCP 错误响应"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def build_tool_error_response(request_id: object, message: str) -> Dict[str, object]:
    """构建 MCP 工具错误响应"""
    return build_tool_text_response(request_id, message)


def read_mcp_message() -> Optional[Dict[str, object]]:
    """读取一条 MCP 消息"""
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.decode("utf-8").strip()
        if not stripped:
            break
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            headers[key.lower()] = value.strip()

    length_text = headers.get("content-length")
    if not length_text or not length_text.isdigit():
        return None
    payload = sys.stdin.buffer.read(int(length_text))
    return json.loads(payload.decode("utf-8"))


def write_mcp_message(message: Dict[str, object]) -> None:
    """写出一条 MCP 消息"""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as runtime_error:
        print(f"错误：{runtime_error}", file=sys.stderr)
        sys.exit(1)

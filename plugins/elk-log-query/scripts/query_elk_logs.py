#!/usr/bin/env python3
"""ELK 日志查询脚本

默认以 MCP Server 模式运行
传入 --cli 时，以命令行模式执行一次性查询
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SIZE = 50
DEFAULT_ACCESS_MODE = "es"
DEFAULT_KIBANA_PROXY_PATH = "/api/console/proxy"
SERVER_NAME = "elk-log-query"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "query_elk_logs"
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT_DIR = SCRIPT_DIR.parent
ENVIRONMENTS_FILE_PATH = PLUGIN_ROOT_DIR / "configs" / "environments.json"


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="查询 ELK 日志，支持 MCP Server 模式和命令行模式"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="以命令行模式运行，未传时默认启动 MCP Server",
    )
    parser.add_argument(
        "--environment",
        help="环境名称，例如 dev01、dev02、test01、test02、pre、prod，默认读取 ELK_DEFAULT_ENVIRONMENT",
    )
    parser.add_argument("--service", help="服务名，例如 order-service")
    parser.add_argument("--keyword", help="关键字，可传订单号、异常关键字或业务号")
    parser.add_argument("--trace-id", help="链路追踪 traceId")
    parser.add_argument(
        "--minutes",
        type=int,
        default=30,
        help="向前查询的分钟数，默认 30",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        help="返回条数，默认 50",
    )
    parser.add_argument(
        "--index",
        help="索引名称或模式，默认读取环境变量 ELK_INDEX_PATTERN",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="输出 Elasticsearch 原始响应",
    )
    return parser.parse_args()


def main() -> int:
    """程序入口"""
    args = parse_args()
    if args.cli:
        return run_cli(args)
    return run_mcp_server()


def run_cli(args: argparse.Namespace) -> int:
    """执行命令行查询"""
    if not any([args.service, args.keyword, args.trace_id]):
        print("错误：service、keyword、trace-id 至少需要传一个", file=sys.stderr)
        return 1

    query_result = execute_query(
        environment=args.environment,
        service=args.service,
        keyword=args.keyword,
        trace_id=args.trace_id,
        minutes=args.minutes,
        size=args.size,
        index=args.index,
    )
    if args.raw:
        print(query_result["response_text"])
        return 0
    print(render_summary(query_result["response_text"], query_result["environment_name"]))
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
                        "capabilities": {
                            "tools": {}
                        },
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
                            build_tool_definition()
                        ]
                    },
                }
            )
            continue

        if method == "tools/call":
            response = handle_tool_call(message)
            write_mcp_message(response)
            continue

        if request_id is not None:
            write_mcp_message(
                build_error_response(request_id, -32601, f"不支持的方法: {method}")
            )


def build_tool_definition() -> Dict[str, object]:
    """构建工具定义"""
    return {
        "name": TOOL_NAME,
        "description": "查询 ELK 日志，支持按服务名、关键字、traceId 和时间范围过滤",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "服务名，例如 order-service"
                },
                "environment": {
                    "type": "string",
                    "description": "环境名称，例如 dev01、dev02、test01、test02、pre、prod，默认读取 ELK_DEFAULT_ENVIRONMENT"
                },
                "keyword": {
                    "type": "string",
                    "description": "关键字，可传订单号、异常关键字或业务号"
                },
                "traceId": {
                    "type": "string",
                    "description": "链路追踪 traceId"
                },
                "minutes": {
                    "type": "integer",
                    "description": "向前查询的分钟数，默认 30",
                    "default": 30,
                    "minimum": 1
                },
                "size": {
                    "type": "integer",
                    "description": "返回条数，默认 50",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500
                },
                "index": {
                    "type": "string",
                    "description": "索引名称或模式，默认读取环境变量 ELK_INDEX_PATTERN"
                },
                "raw": {
                    "type": "boolean",
                    "description": "是否返回 Elasticsearch 原始响应",
                    "default": False
                }
            },
            "additionalProperties": False
        }
    }


def handle_tool_call(message: Dict[str, object]) -> Dict[str, object]:
    """处理工具调用"""
    request_id = message.get("id")
    params = message.get("params", {})
    tool_name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}

    if tool_name != TOOL_NAME:
        return build_error_response(request_id, -32602, f"未知工具: {tool_name}")

    if not isinstance(arguments, dict):
        return build_error_response(request_id, -32602, "arguments 必须是对象")

    service = trim_to_none(arguments.get("service"))
    environment = trim_to_none(arguments.get("environment"))
    keyword = trim_to_none(arguments.get("keyword"))
    trace_id = trim_to_none(arguments.get("traceId"))
    index = trim_to_none(arguments.get("index"))
    raw = bool(arguments.get("raw", False))

    if not any([service, keyword, trace_id]):
        return build_tool_error_response(
            request_id,
            "service、keyword、traceId 至少需要传一个"
        )

    try:
        minutes = int(arguments.get("minutes", 30))
        size = int(arguments.get("size", DEFAULT_SIZE))
    except (TypeError, ValueError):
        return build_tool_error_response(request_id, "minutes 和 size 必须是整数")

    try:
        query_result = execute_query(
            environment=environment,
            service=service,
            keyword=keyword,
            trace_id=trace_id,
            minutes=minutes,
            size=size,
            index=index,
        )
        text = (
            query_result["response_text"]
            if raw else
            render_summary(query_result["response_text"], query_result["environment_name"])
        )
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
    except RuntimeError as error:
        return build_tool_error_response(request_id, str(error))


def execute_query(
    environment: Optional[str],
    service: Optional[str],
    keyword: Optional[str],
    trace_id: Optional[str],
    minutes: int,
    size: int,
    index: Optional[str],
) -> Dict[str, str]:
    """执行 Elasticsearch 查询并返回查询结果"""
    environment_config = resolve_environment_config(environment)
    base_url = environment_config["base_url"]
    index_pattern = index or environment_config["index_pattern"]
    timeout_seconds = environment_config["timeout_seconds"]

    query_payload = build_query_payload(
        service=service,
        keyword=keyword,
        trace_id=trace_id,
        minutes=minutes,
        size=size,
    )
    request = build_request(base_url, index_pattern, query_payload, environment_config)

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return {
                "environment_name": environment_config["name"],
                "response_text": response.read().decode("utf-8"),
            }
    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"请求 ELK 失败，HTTP {error.code}\n{error_body}")
    except URLError as error:
        raise RuntimeError(f"请求 ELK 失败：{error}")


def resolve_environment_config(environment: Optional[str]) -> Dict[str, object]:
    """解析环境配置"""
    environments = load_environment_configs()
    environment_name = resolve_environment_name(environment, environments)
    environment_config = environments[environment_name]

    base_url = read_config_value(environment_config, "baseUrl")
    index_pattern = read_config_value(environment_config, "indexPattern")
    username = read_optional_config_value(environment_config, "username")
    password = read_optional_config_value(environment_config, "password")
    access_mode = read_access_mode(environment_config)
    kibana_proxy_path = read_kibana_proxy_path(environment_config)
    timeout_seconds = read_timeout_seconds(environment_config)

    return {
        "name": environment_name,
        "base_url": base_url,
        "index_pattern": index_pattern,
        "username": username,
        "password": password,
        "access_mode": access_mode,
        "kibana_proxy_path": kibana_proxy_path,
        "timeout_seconds": timeout_seconds,
    }


def load_environment_configs() -> Dict[str, Dict[str, object]]:
    """加载环境配置文件"""
    if not ENVIRONMENTS_FILE_PATH.exists():
        raise RuntimeError(
            f"缺少环境配置文件：{ENVIRONMENTS_FILE_PATH}"
        )

    with ENVIRONMENTS_FILE_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    environments = config.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise RuntimeError("环境配置文件缺少 environments 配置")
    return environments


def resolve_environment_name(
    environment: Optional[str],
    environments: Dict[str, Dict[str, object]]
) -> str:
    """确定本次查询使用的环境名称"""
    environment_name = environment or os.getenv("ELK_DEFAULT_ENVIRONMENT", "").strip()
    if not environment_name:
        raise RuntimeError("缺少环境参数，请传 environment 或配置 ELK_DEFAULT_ENVIRONMENT")
    if environment_name not in environments:
        raise RuntimeError(
            "未知环境：{0}，可选环境：{1}".format(
                environment_name,
                ", ".join(sorted(environments.keys()))
            )
        )
    return environment_name


def read_config_value(environment_config: Dict[str, object], key: str) -> str:
    """读取环境配置中的必填项"""
    value = environment_config.get(key)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.startswith("[TODO:"):
            raise RuntimeError(f"环境配置字段 {key} 还是占位值，请先替换为真实配置")
        return normalized
    raise RuntimeError(f"环境配置缺少字段 {key}")


def read_optional_config_value(environment_config: Dict[str, object], key: str) -> Optional[str]:
    """读取环境配置中的可选项"""
    value = environment_config.get(key)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.startswith("[TODO:"):
            raise RuntimeError(f"环境配置字段 {key} 还是占位值，请先替换为真实配置")
        return normalized
    return None


def read_timeout_seconds(environment_config: Dict[str, object]) -> int:
    """读取超时时间配置"""
    value = environment_config.get("timeoutSeconds")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return DEFAULT_TIMEOUT_SECONDS


def read_access_mode(environment_config: Dict[str, object]) -> str:
    """读取访问模式配置"""
    value = environment_config.get("accessMode")
    if value is None:
        return DEFAULT_ACCESS_MODE

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return DEFAULT_ACCESS_MODE
        if normalized in {"es", "kibana_proxy"}:
            return normalized

    raise RuntimeError("环境配置字段 accessMode 仅支持 es 或 kibana_proxy")


def read_kibana_proxy_path(environment_config: Dict[str, object]) -> str:
    """读取 Kibana 代理路径配置"""
    value = environment_config.get("kibanaProxyPath")
    if value is None:
        return DEFAULT_KIBANA_PROXY_PATH

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return DEFAULT_KIBANA_PROXY_PATH
        if normalized.startswith("/"):
            return normalized.rstrip("/") or DEFAULT_KIBANA_PROXY_PATH

    raise RuntimeError("环境配置字段 kibanaProxyPath 必须是以 / 开头的路径")


def build_query_payload(
    service: Optional[str],
    keyword: Optional[str],
    trace_id: Optional[str],
    minutes: int,
    size: int,
) -> Dict[str, object]:
    """构建 Elasticsearch 查询体"""
    must_conditions: List[Dict[str, object]] = []

    if service:
        must_conditions.append(build_service_condition(service))
    if trace_id:
        must_conditions.append(build_keyword_condition(trace_id))
    if keyword:
        must_conditions.append(build_keyword_condition(keyword))

    start_time = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    return {
        "size": size,
        "sort": [
            {
                "@timestamp": {
                    "order": "desc"
                }
            }
        ],
        "query": {
            "bool": {
                "must": must_conditions,
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": start_time
                            }
                        }
                    }
                ]
            }
        }
    }


def build_service_condition(service: str) -> Dict[str, object]:
    """构建服务名过滤条件"""
    return {
        "bool": {
            "should": [
                {"term": {"service_name.keyword": service}},
                {"term": {"service.keyword": service}},
                {"term": {"log_source.keyword": service}},
                {"term": {"log_source.keyword": f"{service}-all"}},
                {"match_phrase": {"log_source": service}},
                {"term": {"kubernetes.labels.app.keyword": service}}
            ],
            "minimum_should_match": 1
        }
    }


def build_keyword_condition(keyword: str) -> Dict[str, object]:
    """构建关键字匹配条件"""
    keyword_upper = keyword.upper()
    if keyword_upper in {"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL"}:
        return {
            "bool": {
                "should": [
                    {"term": {"level.keyword": keyword}},
                    {"term": {"level.keyword": keyword_upper}},
                    {"term": {"log.level.keyword": keyword}},
                    {"term": {"log.level.keyword": keyword_upper}},
                    {"term": {"log_level.keyword": keyword}},
                    {"term": {"log_level.keyword": keyword_upper}},
                ],
                "minimum_should_match": 1
            }
        }

    return {
        "bool": {
            "should": [
                {"match_phrase": {"message": keyword}},
                {"match_phrase": {"params_json": keyword}},
                {"match_phrase": {"class": keyword}},
                {"match_phrase": {"method": keyword}},
                {"term": {"log_level.keyword": keyword}},
                {"term": {"log_level.keyword": keyword_upper}},
                {"term": {"traceId.keyword": keyword}},
                {"term": {"trace_id.keyword": keyword}},
                {"term": {"orderNo.keyword": keyword}},
                {"term": {"businessNo.keyword": keyword}}
            ],
            "minimum_should_match": 1
        }
    }


def build_request(
    base_url: str,
    index_pattern: str,
    query_payload: Dict[str, object],
    environment_config: Dict[str, object]
) -> Request:
    """构建 HTTP 请求"""
    normalized_base_url = base_url.rstrip("/")
    request_url = build_request_url(normalized_base_url, index_pattern, environment_config)
    request = Request(
        request_url,
        data=json.dumps(query_payload).encode("utf-8"),
        method="POST",
        headers=build_request_headers(environment_config),
    )

    authorization = build_basic_auth_header(environment_config)
    if authorization:
        request.add_header("Authorization", authorization)
    return request


def build_request_url(
    normalized_base_url: str,
    index_pattern: str,
    environment_config: Dict[str, object]
) -> str:
    """按访问模式构建最终请求地址"""
    encoded_index = quote(index_pattern, safe="*,-_")
    access_mode = str(environment_config.get("access_mode") or DEFAULT_ACCESS_MODE)
    if access_mode == "kibana_proxy":
        return build_kibana_proxy_url(normalized_base_url, index_pattern, environment_config)
    return f"{normalized_base_url}/{encoded_index}/_search"


def build_kibana_proxy_url(
    normalized_base_url: str,
    index_pattern: str,
    environment_config: Dict[str, object]
) -> str:
    """构建 Kibana 代理模式的查询地址"""
    proxy_path = str(
        environment_config.get("kibana_proxy_path") or DEFAULT_KIBANA_PROXY_PATH
    )
    search_path = f"/{index_pattern}/_search"
    query_string = urlencode(
        OrderedDict(
            [
                ("path", search_path),
                ("method", "POST"),
            ]
        ),
        quote_via=quote,
        safe="",
    )
    return f"{normalized_base_url}{proxy_path}?{query_string}"


def build_request_headers(environment_config: Dict[str, object]) -> Dict[str, str]:
    """按访问模式构建请求头"""
    headers = {
        "Content-Type": "application/json"
    }
    access_mode = str(environment_config.get("access_mode") or DEFAULT_ACCESS_MODE)
    if access_mode == "kibana_proxy":
        headers["kbn-xsrf"] = "true"
    return headers


def build_basic_auth_header(environment_config: Optional[object]) -> Optional[str]:
    """构建 Basic Auth 请求头"""
    if not isinstance(environment_config, dict):
        return None

    username = str(environment_config.get("username") or "").strip()
    password = str(environment_config.get("password") or "").strip()
    if not username:
        return None

    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


def render_summary(response_text: str, environment_name: str) -> str:
    """格式化输出摘要"""
    response_json = json.loads(response_text)
    hits_container = response_json.get("hits", {})
    total_value = normalize_total(hits_container.get("total"))
    hits = hits_container.get("hits", [])

    lines = [
        f"查询环境：{environment_name}",
        f"命中日志总数：{total_value}",
        f"本次返回条数：{len(hits)}",
    ]

    for index, hit in enumerate(hits, start=1):
        source = hit.get("_source", {})
        timestamp = source.get("@timestamp", "")
        level = source.get("level") or source.get("log.level") or ""
        service = (
            source.get("service_name")
            or source.get("service")
            or source.get("log_source")
            or source.get("kubernetes", {}).get("labels", {}).get("app", "")
        )
        level = level or source.get("log_level") or ""
        message = source.get("message") or source.get("params_json") or ""
        trace_id = source.get("traceId") or source.get("trace_id") or ""

        lines.append("")
        lines.append(f"[{index}] 时间：{timestamp}")
        lines.append(f"    服务：{service}")
        lines.append(f"    级别：{level}")
        if trace_id:
            lines.append(f"    TraceId：{trace_id}")
        lines.append(f"    日志：{message}")

    return "\n".join(lines)


def normalize_total(total: object) -> int:
    """兼容不同版本 Elasticsearch 的 total 结构"""
    if isinstance(total, dict):
        value = total.get("value")
        return value if isinstance(value, int) else 0
    if isinstance(total, int):
        return total
    return 0


def trim_to_none(value: object) -> Optional[str]:
    """将空字符串归一化为 None"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def build_error_response(request_id: object, code: int, message: str) -> Dict[str, object]:
    """构建标准 JSON-RPC 错误响应"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def build_tool_error_response(request_id: object, message: str) -> Dict[str, object]:
    """构建工具调用失败响应"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": message,
                }
            ]
        },
    }


def read_mcp_message() -> Optional[Dict[str, object]]:
    """读取一条带 Content-Length 头的 MCP 消息"""
    content_length = None
    while True:
        header_line = sys.stdin.buffer.readline()
        if not header_line:
            return None

        if header_line in (b"\r\n", b"\n"):
            break

        header_text = header_line.decode("utf-8").strip()
        if header_text.lower().startswith("content-length:"):
            content_length = int(header_text.split(":", 1)[1].strip())

    if content_length is None:
        return None

    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_mcp_message(message: Dict[str, object]) -> None:
    """输出一条 MCP 消息"""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    sys.exit(main())

# 原子诊断工具。Pydantic schema 由 LangChain tool 收成 Function-Calling JSON Schema。
# 探测实现从 sim.py 转调，工具层只负责入参约束和 JSON 序列化。

import json
from typing import Optional

from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

from sim import analyze_logs, check_interface, ping_target, resolve_dns, restart_interface

HIGH_RISK_TOOLS = {"restart_interface"}


def to_json(data):
    """把探测结果序列化成工具返回字符串。

    参数:
        data: sim 返回的对象
    返回:
        不含 ASCII 转义的 JSON 字符串，字段名保持原样给下一步工具用
    """
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class PingInput(BaseModel):
    target: str = Field(description="目标主机名或 IP 地址")


class DnsInput(BaseModel):
    hostname: str = Field(description="要解析的主机名，例如 www.example.com")


class InterfaceCheckInput(BaseModel):
    interface_name: Optional[str] = Field(default=None, description="可选接口名，不填则检查默认 Ethernet")


class LogAnalysisInput(BaseModel):
    keywords: str = Field(description="日志检索关键词，例如 timeout")
    time_range: Optional[str] = Field(default=None, description="时间范围描述，默认过去1小时")


class RestartInterfaceInput(BaseModel):
    interface_name: str = Field(description="接口名称，如 eth1")


@tool("ping_tool", description="检查本机到指定主机名或 IP 的连通性。优先填 dns_tool 返回的 ip，不要对未解析域名做猜测。", args_schema=PingInput)
def ping_tool(target: str) -> str:
    """检查连通性。

    参数:
        target: 目标主机名或 IP 地址
    返回:
        JSON 字符串
    """
    return to_json(ping_target(target))


@tool("dns_tool", description="解析主机名，获取对应 IP。用户给出网站域名时第一步调用。ok=false 表示 NXDOMAIN，不要再 ping。", args_schema=DnsInput)
def dns_tool(hostname: str) -> str:
    """解析主机名。

    参数:
        hostname: 要解析的主机名
    返回:
        JSON 字符串
    """
    return to_json(resolve_dns(hostname))


@tool("interface_check_tool", description="检查本机网络接口状态。连通失败时调用，用来排除本机网卡故障。eth1 为关闭状态。", args_schema=InterfaceCheckInput)
def interface_check_tool(interface_name: Optional[str] = None) -> str:
    """检查本机网卡。

    参数:
        interface_name: 可选接口名，不填则检查默认 Ethernet
    返回:
        JSON 字符串
    """
    return to_json(check_interface(interface_name))


@tool("log_analysis_tool", description="按关键词检索网络日志。本机接口正常后再调用，keyword 填 ping_tool 返回的 status，例如 timeout。", args_schema=LogAnalysisInput)
def log_analysis_tool(keywords: str, time_range: Optional[str] = None) -> str:
    """检索模拟日志。

    参数:
        keywords: 日志检索关键词
        time_range: 时间范围描述，缺省为过去1小时
    返回:
        JSON 字符串
    """
    if time_range is None:
        return to_json(analyze_logs(keywords))
    return to_json(analyze_logs(keywords, time_range))


@tool("restart_interface", description="重启指定网络接口。这是高风险操作，必须经过人工确认后才能执行。", args_schema=RestartInterfaceInput)
def restart_interface_tool(interface_name: str) -> str:
    """重启网络接口。

    参数:
        interface_name: 接口名称
    返回:
        JSON 字符串
    """
    return to_json(restart_interface(interface_name))


DIAGNOSIS_TOOLS = [
    ping_tool,
    dns_tool,
    interface_check_tool,
    log_analysis_tool,
    restart_interface_tool,
]

TOOLS_BY_NAME = {item.name: item for item in DIAGNOSIS_TOOLS}


def to_function_calling_schema(item):
    """从 tool 的 schema 自动生成 OpenAI Function-Calling JSON Schema。

    参数:
        item: LangChain StructuredTool
    返回:
        {type, function:{name, description, parameters}}
    """
    return convert_to_openai_tool(item)


def all_function_calling_schemas():
    """导出全部诊断工具的 Function-Calling Schema。

    返回:
        OpenAI tools 数组
    """
    return [to_function_calling_schema(item) for item in DIAGNOSIS_TOOLS]

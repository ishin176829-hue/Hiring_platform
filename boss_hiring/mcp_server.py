import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .cli import api_request


mcp = FastMCP("boss-hiring")


@mcp.tool()
async def get_status() -> dict[str, Any]:
    """查询工作台、浏览器和后台任务的当前状态。"""
    return await asyncio.to_thread(api_request, "GET", "status")


@mcp.tool()
async def list_jobs(city: str = "", q: str = "") -> list[dict[str, Any]]:
    """查询已采集岗位，可按杭州或上海和关键词筛选。"""
    return await asyncio.to_thread(api_request, "GET", "jobs", query={"city": city, "q": q})


@mcp.tool()
async def collect_jobs() -> dict[str, Any]:
    """按当前设置排队采集岗位，返回后台任务编号。需要用户先登录。"""
    return await asyncio.to_thread(api_request, "POST", "collect")


@mcp.tool()
async def list_batches() -> list[dict[str, Any]]:
    """查询候选清单及其确认和执行状态。"""
    return await asyncio.to_thread(api_request, "GET", "batches")


@mcp.tool()
async def run_approved_batch(batch_id: Annotated[int, Field(strict=True, gt=0)]) -> dict[str, Any]:
    """执行用户已在网页确认的清单，或继续该清单的未处理项。真实模式会按确认内容操作。"""
    return await asyncio.to_thread(api_request, "POST", f"batches/{batch_id}/run")


@mcp.tool()
async def pause_worker() -> dict[str, Any]:
    """暂停全局任务执行。"""
    return await asyncio.to_thread(api_request, "POST", "pause")


@mcp.tool()
async def resume_worker() -> dict[str, Any]:
    """解除全局暂停。继续执行已确认的清单需要另外调用 run_approved_batch。"""
    return await asyncio.to_thread(api_request, "POST", "resume")


@mcp.tool()
async def open_login() -> dict[str, Any]:
    """在运行工作台的 Mac 上打开 BOSS 浏览器，由用户完成登录或验证。"""
    return await asyncio.to_thread(api_request, "POST", "browser/open")


@mcp.tool()
async def get_events(limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    """查询最近工作台事件，最多返回 500 条。"""
    return await asyncio.to_thread(api_request, "GET", "events", query={"limit": limit})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

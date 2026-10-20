import asyncio
import json
import sys

from app.config import get_settings


async def attachment_context(attachment):
    path = get_settings().attachment_dir / attachment.storage_key
    result = {"id": attachment.id, "filename": attachment.filename, "sha256": attachment.sha256}
    if not path.is_file():
        return {**result, "status": "unavailable", "reason": "文件暂不可读取，不能评估附件内容"}
    if attachment.mime != "application/pdf":
        return {**result, "status": "read", "text": path.read_text(encoding="utf-8")[:32000]}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.extract_pdf",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        parsed = json.loads(stdout)
        if process.returncode == 0 and parsed.get("text"):
            return {**result, "status": "read", **parsed}
    except (TimeoutError, ValueError):
        if process.returncode is None:
            process.kill()
            await process.wait()
    return {**result, "status": "unavailable", "reason": "PDF 未能提取文本，不能声称已评审其内容；请提交文本"}

"""Fixed offline document requests and bounded, untrusted parser output."""

import hashlib
import json

from app.integrations.sandbox import SandboxProtocolError
from app.integrations.sandbox_proto import sandbox_pb2 as pb


MAX_INPUT = 1024 * 1024
MAX_TEXT = 200000
MAX_JSON = 2 * 1024 * 1024
PARSER_VERSION = "sandbox-files-contract-v1"


class MaterialContentError(SandboxProtocolError):
    """A hash-validated parser body was received, but isn't usable material content."""


def parse_request(request_id, source_type, content, sha256):
    if source_type not in {"pdf", "docx"} or not 0 < len(content) <= MAX_INPUT:
        raise SandboxProtocolError("Invalid material input")
    if hashlib.sha256(content).hexdigest() != sha256:
        raise SandboxProtocolError("Material input checksum mismatch")
    return pb.TaskRequest(request_id=request_id, kind="parse", environment="files",
        inputs=[pb.InputFile(path=f"input.{source_type}", data=content, sha256=sha256)],
        memory_mb=256, timeout_seconds=30)


def _pairs(items):
    output = {}
    for key, value in items:
        if key in output:
            raise ValueError("Duplicate JSON key")
        output[key] = value
    return output


def _reject_constant(value):
    raise ValueError("Nonfinite JSON")


def _strings(items, limit):
    if not isinstance(items, list) or len(items) > limit or any(type(item) is not str for item in items):
        raise ValueError("Invalid document strings")
    return items


def parsed_document(result, source_type):
    """This is extraction data, never an execution or learning verification receipt."""
    artifacts = result["artifacts"]
    if result["status"]["outcome"] != "success" or len(artifacts) != 1 or artifacts[0]["filename"] != "parsed.json":
        raise SandboxProtocolError("Expected one parsed document")
    data = artifacts[0]["data"]
    if len(data) > MAX_JSON or hashlib.sha256(data).hexdigest() != artifacts[0]["sha256"]:
        raise SandboxProtocolError("Material parser output exceeds limit")
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
        expected = {"source", "type", "content"} | ({"page_count"} if source_type == "pdf" else set())
        if not isinstance(parsed, dict) or set(parsed) != expected:
            raise ValueError("Invalid document fields")
        if parsed["source"] != f"input.{source_type}" or parsed["type"] != source_type:
            raise SandboxProtocolError("Material source mismatch")
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")  # Reject unpaired surrogate scalars from JSON escapes.
        content = parsed["content"]
        if len(json.dumps(content, ensure_ascii=False)) > MAX_TEXT:
            raise ValueError("Document text exceeds limit")
        if source_type == "pdf":
            if type(content) is not str or type(parsed["page_count"]) is not int or not 0 <= parsed["page_count"] <= 30:
                raise ValueError("Invalid PDF text")
            text = content
        else:
            if not isinstance(content, dict) or set(content) != {"paragraphs", "tables"}:
                raise ValueError("Invalid DOCX content")
            paragraphs = _strings(content["paragraphs"], 2000)
            tables = content["tables"]
            if not isinstance(tables, list) or len(tables) > 100:
                raise ValueError("Invalid DOCX tables")
            lines = list(paragraphs)
            for table in tables:
                if not isinstance(table, list) or len(table) > 2000:
                    raise ValueError("Invalid DOCX rows")
                lines.extend("\t".join(_strings(row, 200)) for row in table)
            text = "\n".join(lines)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
        if len(text) > MAX_TEXT:
            raise ValueError("Flattened document exceeds limit")
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        raise MaterialContentError("Invalid material parser result") from None
    return parsed, text

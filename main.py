"""Deterministic, standard-library-only free model aggregation gateway."""

import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep
from urllib.parse import urlsplit


SCHEMA_PREFIX = "free-model-aggregator-01/"
CREATED = 1767225600
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
ROUTES = []


def read_brief():
    """Find schema-bearing JSON documents even in a surrounding prose brief."""
    try:
        with open(os.environ.get("MAKEFUN_PROMPT_FILE", ""), encoding="utf-8") as source:
            text = source.read()
    except (OSError, UnicodeError, ValueError):
        return {}, {}, {}
    decoder = json.JSONDecoder()
    documents = {}
    position = 0
    while True:
        position = text.find("{", position)
        if position < 0:
            break
        try:
            document, end = decoder.raw_decode(text, position)
        except (ValueError, RecursionError):
            position += 1
            continue
        if isinstance(document, dict) and isinstance(document.get("schema"), str):
            documents[document["schema"]] = document
        position = end
    return tuple(documents.get(SCHEMA_PREFIX + name + "/v1", {})
                 for name in ("providers", "ratings", "workload"))


DIRECTORY, RATINGS, WORKLOAD = read_brief()
MANIFEST = WORKLOAD.get("manifest", [])


def provider_headers(provider):
    headers = {}
    key = provider.get("key")
    if key is None:
        key = os.environ.get(provider.get("key_env", ""), "")
    auth = provider.get("auth", "none")
    wire = provider.get("wire")
    if auth == "bearer" and wire in ("openai", "anthropic"):
        headers["Authorization"] = "Bearer " + str(key)
    elif auth == "x-api-key" and wire == "anthropic":
        headers["x-api-key"] = str(key)
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "x-goog-api-key" and wire == "gemini":
        headers["x-goog-api-key"] = str(key)
    return headers


def open_http(method, url, body=None, headers=None, timeout=5):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid HTTP URL")
    connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                       else http.client.HTTPConnection)
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        connection.request(method, target, body=body, headers=headers or {})
        return connection, connection.getresponse()
    except Exception:
        connection.close()
        raise


def probe_provider(provider):
    suffix = "/v1beta/models" if provider["wire"] == "gemini" else "/v1/models"
    connection, response = open_http(
        "GET", ENV_URL + provider["path"].rstrip("/") + suffix,
        headers=provider_headers(provider), timeout=5)
    try:
        if response.status != 200:
            raise ValueError("model listing failed")
        document = json.loads(response.read())
        return document["models" if provider["wire"] == "gemini" else "data"]
    finally:
        response.close()
        connection.close()


def listed_free(item):
    if isinstance(item.get("free"), bool):
        return item["free"]
    if isinstance(item.get("pricing"), dict):
        try:
            return all(float(item["pricing"][field]) == 0
                       for field in ("prompt", "completion", "input", "output")
                       if field in item["pricing"])
        except (ValueError, TypeError, OverflowError):
            return False
    return None


def build_catalog():
    """Probe once and build both the public catalog and its routing metadata."""
    ratings_by_provider = {}
    for row in RATINGS.get("ratings", []):
        ratings_by_provider.setdefault(row["provider"], {})[row["model"]] = row
    catalog = {"providers": []}
    ROUTES.clear()
    for order, provider in enumerate(DIRECTORY.get("providers", [])):
        models = {}
        if ENV_URL:
            try:
                for item in probe_provider(provider):
                    model_id = (item["name"].removeprefix("models/")
                                if provider["wire"] == "gemini" else item["id"])
                    models[model_id] = {
                        "id": model_id,
                        "free": listed_free(item) is True,
                        "context_length": (item.get("context_length")
                                           or item.get("context_window")
                                           or item.get("inputTokenLimit") or 0),
                        "rating_milli": 0,
                        **{cap: item.get(cap) is True for cap in ("tools", "vision", "json")},
                    }
            except Exception:
                models = {}
        for model_id, row in ratings_by_provider.get(provider["id"], {}).items():
            models[model_id] = {
                "id": model_id,
                "free": row.get("free", False),
                "context_length": row.get("context_length", 0),
                "rating_milli": row.get("rating_milli", 0),
                **{cap: row.get(cap, False) for cap in ("tools", "vision", "json")},
            }
        ordered_models = [models[model_id] for model_id in sorted(models)]
        catalog["providers"].append({
            "id": provider["id"],
            "base_url": provider["path"],
            "key_env": provider.get("key_env", ""),
            "wire": provider["wire"],
            "models": [{field: model[field] for field in ("id", "free", "context_length")}
                       for model in ordered_models],
        })
        for model in ordered_models:
            ROUTES.append({"provider": provider, "order": order, "model": model})
    return catalog


def list_catalog(catalog):
    print(json.dumps(catalog, indent=2))


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def parse_arguments(arguments):
    try:
        return json.loads(arguments)
    except (TypeError, ValueError, RecursionError):
        return {}


def json_mode(body, client_wire):
    response_format = body.get("response_format")
    return (client_wire == "openai" and isinstance(response_format, dict)
            and response_format.get("type") in ("json_object", "json_schema"))


def data_url(url):
    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
        media, data = url[5:].split(";base64,", 1)
        return media, data
    return None


def image_url(part):
    value = part.get("image_url", {})
    return value if isinstance(value, str) else value.get("url", "")


def token_limit(body):
    if "max_tokens" in body:
        return True, body["max_tokens"]
    if "max_completion_tokens" in body:
        return True, body["max_completion_tokens"]
    return False, None


def openai_parts_to_anthropic(content):
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = image_url(part)
            inline = data_url(url)
            source = ({"type": "base64", "media_type": inline[0], "data": inline[1]}
                      if inline else {"type": "url", "url": url})
            parts.append({"type": "image", "source": source})
    return parts


def openai_to_anthropic(body, model):
    result = {"model": model, "messages": []}
    system = []
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            system.append(text_content(content))
        elif role == "tool":
            result["messages"].append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                "content": text_content(content),
            }]})
        elif role in ("user", "assistant"):
            converted = openai_parts_to_anthropic(content)
            if role == "assistant" and message.get("tool_calls"):
                if isinstance(converted, str):
                    converted = [{"type": "text", "text": converted}] if converted else []
                for call in message["tool_calls"]:
                    function = call.get("function", {})
                    converted.append({"type": "tool_use", "id": call.get("id", ""),
                                      "name": function.get("name", ""),
                                      "input": parse_arguments(function.get("arguments", ""))})
            result["messages"].append({"role": role, "content": converted})
    system_text = "\n".join(system)
    if system_text:
        result["system"] = system_text
    present, limit = token_limit(body)
    if present:
        result["max_tokens"] = limit
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "stop" in body:
        stop = body["stop"]
        result["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if "tools" in body:
        result["tools"] = [
            {"name": tool["function"].get("name", ""),
             "description": tool["function"].get("description", ""),
             "input_schema": tool["function"].get("parameters") or {"type": "object"}}
            for tool in body["tools"]]
    choice = body.get("tool_choice")
    if isinstance(choice, str) and choice in ("auto", "none", "required"):
        result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    if body.get("stream"):
        result["stream"] = True
    return result


def anthropic_to_openai(body, model):
    result = {"model": model, "messages": []}
    system = text_content(body.get("system"))
    if system:
        result["messages"].append({"role": "system", "content": system})
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            result["messages"].append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "assistant":
            texts = [part.get("text", "") for part in blocks if part.get("type") == "text"]
            converted = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            calls = [{"id": part.get("id", ""), "type": "function", "function": {
                "name": part.get("name", ""), "arguments": json.dumps(part.get("input", {}))}}
                for part in blocks if part.get("type") == "tool_use"]
            if calls:
                converted["tool_calls"] = calls
            result["messages"].append(converted)
        elif role == "user":
            parts = []
            tool_messages = []
            for part in blocks:
                kind = part.get("type")
                if kind == "text":
                    parts.append({"type": "text", "text": part.get("text", "")})
                elif kind == "image":
                    source = part.get("source", {})
                    if source.get("type") == "base64":
                        url = "data:" + source.get("media_type", "") + ";base64," + source.get("data", "")
                    else:
                        url = source.get("url", "")
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                elif kind == "tool_result":
                    tool_messages.append({"role": "tool", "tool_call_id": part.get("tool_use_id", ""),
                                          "content": text_content(part.get("content"))})
            if parts:
                converted = ("\n".join(part["text"] for part in parts)
                             if all(part["type"] == "text" for part in parts) else parts)
                result["messages"].append({"role": "user", "content": converted})
            result["messages"].extend(tool_messages)
    for field in ("max_tokens", "temperature"):
        if field in body:
            result[field] = body[field]
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    if "tools" in body:
        result["tools"] = [{"type": "function", "function": {
            "name": tool.get("name", ""), "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object"})}}
            for tool in body["tools"]]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind in ("auto", "any", "none"):
            result["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}[kind]
        elif kind == "tool":
            result["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    if body.get("stream"):
        result["stream"] = True
        result["stream_options"] = {"include_usage": True}
    return result


def openai_parts_to_gemini(content):
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append({"text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = image_url(part)
            inline = data_url(url)
            parts.append({"inlineData": {"mimeType": inline[0], "data": inline[1]}}
                         if inline else {"fileData": {"fileUri": url}})
    return parts


def gemini_tool_config(choice, client_wire):
    name = None
    if client_wire == "openai":
        if isinstance(choice, str):
            mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}.get(choice)
        elif isinstance(choice, dict) and choice.get("type") == "function":
            mode, name = "ANY", choice["function"]["name"]
        else:
            return None
    else:
        if not isinstance(choice, dict):
            return None
        mode = {"auto": "AUTO", "any": "ANY", "none": "NONE", "tool": "ANY"}.get(choice.get("type"))
        if choice.get("type") == "tool":
            name = choice["name"]
    if mode is None:
        return None
    config = {"mode": mode}
    if name is not None:
        config["allowedFunctionNames"] = [name]
    return {"functionCallingConfig": config}


def openai_to_gemini(body):
    result = {"contents": []}
    system = []
    names = {}
    for message in body.get("messages", []):
        role = message.get("role")
        if role == "system":
            system.append(text_content(message.get("content")))
            continue
        if role == "tool":
            result["contents"].append({"role": "user", "parts": [{"functionResponse": {
                "name": names.get(message.get("tool_call_id"), "tool"),
                "response": {"result": text_content(message.get("content"))}}}]})
            continue
        if role not in ("user", "assistant"):
            continue
        parts = openai_parts_to_gemini(message.get("content"))
        if role == "assistant":
            for call in message.get("tool_calls", []):
                function = call.get("function", {})
                name = function.get("name", "")
                names[call.get("id", "")] = name
                parts.append({"functionCall": {"name": name,
                              "args": parse_arguments(function.get("arguments", ""))}})
        result["contents"].append({"role": "model" if role == "assistant" else "user", "parts": parts})
    system_text = "\n".join(system)
    if system_text:
        result["systemInstruction"] = {"parts": [{"text": system_text}]}
    if "tools" in body:
        result["tools"] = [{"functionDeclarations": [
            {"name": tool["function"].get("name", ""),
             "description": tool["function"].get("description", ""),
             "parameters": tool["function"].get("parameters", {"type": "object"})}
            for tool in body["tools"]]}]
    config = gemini_tool_config(body.get("tool_choice"), "openai")
    if config is not None:
        result["toolConfig"] = config
    generation = {}
    present, limit = token_limit(body)
    if present:
        generation["maxOutputTokens"] = limit
    if "stop" in body:
        stop = body["stop"]
        generation["stopSequences"] = [stop] if isinstance(stop, str) else stop
    if "temperature" in body:
        generation["temperature"] = body["temperature"]
    if json_mode(body, "openai"):
        generation["responseMimeType"] = "application/json"
    if generation:
        result["generationConfig"] = generation
    return result


def anthropic_to_gemini(body):
    result = {"contents": []}
    system = text_content(body.get("system"))
    if system:
        result["systemInstruction"] = {"parts": [{"text": system}]}
    names = {}
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        parts = []
        for part in blocks:
            kind = part.get("type")
            if kind == "text":
                parts.append({"text": part.get("text", "")})
            elif kind == "image":
                source = part.get("source", {})
                parts.append({"inlineData": {"mimeType": source.get("media_type", ""),
                                             "data": source.get("data", "")}}
                             if source.get("type") == "base64"
                             else {"fileData": {"fileUri": source.get("url", "")}})
            elif kind == "tool_use":
                name = part.get("name", "")
                names[part.get("id", "")] = name
                parts.append({"functionCall": {"name": name, "args": part.get("input", {})}})
            elif kind == "tool_result":
                parts.append({"functionResponse": {
                    "name": names.get(part.get("tool_use_id"), "tool"),
                    "response": {"result": text_content(part.get("content"))}}})
        result["contents"].append({"role": "model" if role == "assistant" else "user", "parts": parts})
    if "tools" in body:
        result["tools"] = [{"functionDeclarations": [
            {"name": tool.get("name", ""), "description": tool.get("description", ""),
             "parameters": tool.get("input_schema", {"type": "object"})}
            for tool in body["tools"]]}]
    config = gemini_tool_config(body.get("tool_choice"), "anthropic")
    if config is not None:
        result["toolConfig"] = config
    generation = {}
    for source, target in (("max_tokens", "maxOutputTokens"),
                           ("stop_sequences", "stopSequences"), ("temperature", "temperature")):
        if source in body:
            generation[target] = body[source]
    if generation:
        result["generationConfig"] = generation
    return result


def provider_request(body, client_wire, provider_wire, model):
    if client_wire == provider_wire:
        result = dict(body)
        result["model"] = model
        if provider_wire == "openai" and body.get("stream"):
            result["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        return result
    if provider_wire == "anthropic":
        return openai_to_anthropic(body, model)
    if provider_wire == "openai":
        return anthropic_to_openai(body, model)
    if client_wire == "openai":
        return openai_to_gemini(body)
    return anthropic_to_gemini(body)


OPENAI_STOPS = ("stop", "length", "tool_calls", "content_filter")
ANTHROPIC_STOPS = ("end_turn", "max_tokens", "tool_use", "refusal")


def stop_family(reason):
    if reason in ("length", "max_tokens", "MAX_TOKENS"):
        return 1
    if reason in ("tool_calls", "function_call", "tool_use"):
        return 2
    if reason in ("content_filter", "refusal", "SAFETY"):
        return 3
    return 0


def first_item(items):
    return items[0] if items else {}


def gemini_text(candidate):
    return "".join(part.get("text", "")
                   for part in (candidate.get("content") or {}).get("parts", [])
                   if isinstance(part.get("text", ""), str))


def reduce_response(document, wire):
    if wire == "openai":
        choice = first_item(document.get("choices", []))
        text = (choice.get("message") or {}).get("content") or ""
        usage = document.get("usage") or {}
        return (text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                stop_family(choice.get("finish_reason")))
    if wire == "anthropic":
        text = "".join(part.get("text", "") for part in document.get("content", [])
                       if part.get("type") == "text")
        usage = document.get("usage") or {}
        return (text, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                stop_family(document.get("stop_reason")))
    candidate = first_item(document.get("candidates", []))
    usage = document.get("usageMetadata") or {}
    return (gemini_text(candidate), usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0), stop_family(candidate.get("finishReason")))


def openai_usage(input_tokens, output_tokens):
    return {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def client_response(wire, model, text, input_tokens, output_tokens, family):
    if wire == "openai":
        return {
            "id": "chatcmpl-sim", "object": "chat.completion", "created": CREATED,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": OPENAI_STOPS[family]}],
            "usage": openai_usage(input_tokens, output_tokens),
        }
    return {
        "id": "msg_sim", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": ANTHROPIC_STOPS[family], "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def error_response(wire):
    error = {"message": "no free model could serve the request", "type": "api_error"}
    if wire == "anthropic":
        return {"type": "error", "error": error}
    return {"error": error}


def write_sse(handler, document, event=None):
    prefix = "event: " + event + "\n" if event else ""
    data = document if isinstance(document, str) else json.dumps(document)
    handler.wfile.write((prefix + "data: " + data + "\n\n").encode("utf-8"))
    handler.wfile.flush()


def sse_frames(response):
    """Read decoded HTTP body lines, including chunked upstream responses."""
    event = None
    data = []
    while True:
        line = response.readline()
        if not line:
            if data:
                yield event, "\n".join(data)
            return
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        elif line.startswith("event:"):
            event = line[6:].lstrip(" ")


def stream_documents(response):
    for event, data in sse_frames(response):
        if data.strip() == "[DONE]":
            return
        try:
            document = json.loads(data)
        except ValueError:
            continue
        if isinstance(document, dict):
            yield event, document


def forward_stream(handler, response):
    while True:
        line = response.readline()
        if not line:
            return
        handler.wfile.write(line)
        handler.wfile.flush()


class AnthropicStream:
    """Emit the ordered Anthropic text-only streaming envelope."""

    def __init__(self, handler, model):
        self.handler = handler
        self.started = False
        self.closed = False
        self.event("message_start", {"message": {
            "id": "msg_sim", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }})

    def event(self, name, fields=None):
        write_sse(self.handler, {"type": name, **(fields or {})}, name)

    def text(self, text):
        if not text or self.closed:
            return
        if not self.started:
            self.event("content_block_start", {
                "index": 0, "content_block": {"type": "text", "text": ""}})
            self.started = True
        self.event("content_block_delta", {
            "index": 0, "delta": {"type": "text_delta", "text": text}})

    def close_block(self):
        if self.started and not self.closed:
            self.event("content_block_stop", {"index": 0})
            self.closed = True

    def finish(self, family, output_tokens):
        self.close_block()
        self.event("message_delta", {
            "delta": {"stop_reason": ANTHROPIC_STOPS[family], "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })
        self.event("message_stop")


class OpenAIStream:
    """Emit OpenAI chunks with a deterministic identity and final usage."""

    def __init__(self, handler, model):
        self.handler = handler
        self.model = model
        self.first = True
        self.finished = False

    def chunk(self, choices, usage=None):
        document = {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                    "created": CREATED, "model": self.model, "choices": choices}
        if usage is not None:
            document["usage"] = usage
        write_sse(self.handler, document)

    def text(self, text):
        if not text:
            return
        delta = {"content": text}
        if self.first:
            delta["role"] = "assistant"
            self.first = False
        self.chunk([{"index": 0, "delta": delta, "finish_reason": None}])

    def finish(self, family):
        if not self.finished:
            self.chunk([{"index": 0, "delta": {}, "finish_reason": OPENAI_STOPS[family]}])
            self.finished = True

    def end(self, family, input_tokens, output_tokens):
        self.finish(family)
        self.chunk([], openai_usage(input_tokens, output_tokens))
        write_sse(self.handler, "[DONE]")


def openai_stream_to_anthropic(handler, response, model):
    output = AnthropicStream(handler, model)
    output_tokens = 0
    for _, document in stream_documents(response):
        usage = document.get("usage") or {}
        if "completion_tokens" in usage:
            output_tokens = usage["completion_tokens"]
        choice = first_item(document.get("choices", []))
        text = (choice.get("delta") or {}).get("content") or ""
        output.text(text)
        if choice.get("finish_reason") is not None:
            output.close_block()
    output.finish(0, output_tokens)


def anthropic_stream_to_openai(handler, response, model):
    output = OpenAIStream(handler, model)
    input_tokens, output_tokens, family = 0, 0, 0
    for event, document in stream_documents(response):
        kind = document.get("type", event)
        if kind == "message_start":
            usage = (document.get("message") or {}).get("usage") or {}
            input_tokens = usage.get("input_tokens", 0)
        elif kind == "content_block_delta":
            delta = document.get("delta") or {}
            if delta.get("type") == "text_delta":
                output.text(delta.get("text", ""))
        elif kind == "message_delta":
            usage = document.get("usage") or {}
            if "output_tokens" in usage:
                output_tokens = usage["output_tokens"]
            delta = document.get("delta") or {}
            if "stop_reason" in delta:
                family = stop_family(delta["stop_reason"])
        elif kind == "message_stop":
            break
    output.end(family, input_tokens, output_tokens)


def gemini_stream_to_openai(handler, response, model):
    output = OpenAIStream(handler, model)
    input_tokens, output_tokens, family = 0, 0, 0
    for _, document in stream_documents(response):
        candidate = first_item(document.get("candidates", []))
        output.text(gemini_text(candidate))
        if candidate.get("finishReason") is not None:
            family = stop_family(candidate["finishReason"])
            output.finish(family)
        usage = document.get("usageMetadata") or {}
        if "promptTokenCount" in usage:
            input_tokens = usage["promptTokenCount"]
        if "candidatesTokenCount" in usage:
            output_tokens = usage["candidatesTokenCount"]
    output.end(family, input_tokens, output_tokens)


def gemini_stream_to_anthropic(handler, response, model):
    output = AnthropicStream(handler, model)
    output_tokens, family = 0, 0
    for _, document in stream_documents(response):
        candidate = first_item(document.get("candidates", []))
        output.text(gemini_text(candidate))
        if candidate.get("finishReason") is not None:
            family = stop_family(candidate["finishReason"])
        usage = document.get("usageMetadata") or {}
        if "candidatesTokenCount" in usage:
            output_tokens = usage["candidatesTokenCount"]
    output.finish(family, output_tokens)


def translate_stream(handler, response, provider_wire, client_wire, model):
    if provider_wire == client_wire:
        forward_stream(handler, response)
    elif provider_wire == "openai":
        openai_stream_to_anthropic(handler, response, model)
    elif provider_wire == "anthropic":
        anthropic_stream_to_openai(handler, response, model)
    elif client_wire == "openai":
        gemini_stream_to_openai(handler, response, model)
    else:
        gemini_stream_to_anthropic(handler, response, model)


def has_image(value):
    if isinstance(value, list):
        return any(has_image(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image"):
            return True
        return any(has_image(item) for item in value.values())
    return False


def candidates_for(body, client_wire, index):
    if index < len(MANIFEST):
        requirements = MANIFEST[index].get("requires") or {}
    else:
        requirements = {"tools": bool(body.get("tools")),
                        "vision": has_image(body.get("messages", [])),
                        "json": json_mode(body, client_wire)}
    candidates = []
    for route in ROUTES:
        model = route["model"]
        if model.get("free") is not True:
            continue
        if json_mode(body, client_wire) and route["provider"]["wire"] == "anthropic":
            continue
        if any(requirements.get(cap) and not model.get(cap)
               for cap in ("tools", "vision", "json")):
            continue
        if (model.get("context_length") or 0) < (requirements.get("min_context") or 0):
            continue
        candidates.append(route)
    return sorted(candidates, key=lambda route: (
        -(route["model"].get("rating_milli") or 0), route["order"], route["model"]["id"]))


def provider_post_url(provider, model, streaming):
    base = ENV_URL + provider["path"].rstrip("/")
    if provider["wire"] == "openai":
        return base + "/v1/chat/completions"
    if provider["wire"] == "anthropic":
        return base + "/v1/messages"
    action = ":streamGenerateContent?alt=sse" if streaming else ":generateContent"
    return base + "/v1beta/models/" + model + action


def model_listing(catalog, anthropic):
    ids = []
    seen = set()
    for provider in catalog["providers"]:
        for model in provider["models"]:
            model_id = model["id"]
            if model["free"] is True and model_id not in seen:
                seen.add(model_id)
                ids.append(model_id)
    if anthropic:
        return {"data": [{"type": "model", "id": model_id, "display_name": model_id,
                          "created_at": "2026-01-01T00:00:00Z"} for model_id in ids],
                "has_more": False, "first_id": ids[0] if ids else None,
                "last_id": ids[-1] if ids else None}
    return {"object": "list", "data": [
        {"id": model_id, "object": "model", "created": CREATED,
         "owned_by": "free-model-aggregator"} for model_id in ids]}


class AggregatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, catalog):
        super().__init__(address, AggregatorHandler)
        self.catalog = catalog
        self.counter = 0
        self.counter_lock = threading.Lock()

    def request_index(self):
        with self.counter_lock:
            index = self.counter
            self.counter += 1
        return index

    def handle_error(self, request, client_address):
        # Worker failures must not print tracebacks in server mode.
        pass


class AggregatorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.do_unknown
        raise AttributeError(name)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("negative content length")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete request body")
        return body

    def send_bytes(self, status, data, content_type="application/json"):
        # send_response() inserts a wall-clock Date header; use the lower-level API.
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
            self.wfile.flush()

    def send_json(self, status, document):
        self.send_bytes(status, json.dumps(document).encode("utf-8"))

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.send_json(code, {"error": "not_found" if code == 404 else "bad_request"})

    def start_stream(self):
        self.close_connection = True
        self.send_response_only(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

    def do_GET(self):
        try:
            self.read_body()
        except (ValueError, OSError):
            self.send_error(400)
            return
        if self.path == "/v1/models":
            anthropic = "anthropic-version" in self.headers or "x-api-key" in self.headers
            self.send_json(200, model_listing(self.server.catalog, anthropic))
        else:
            self.send_json(404, {"error": "not_found"})

    def do_unknown(self):
        try:
            self.read_body()
        except (ValueError, OSError):
            self.send_error(400)
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        client_wire = {"/v1/chat/completions": "openai", "/v1/messages": "anthropic"}.get(self.path)
        index = self.server.request_index() if client_wire else None
        try:
            raw = self.read_body()
        except (ValueError, OSError):
            self.send_error(400)
            return
        if client_wire is None:
            self.send_json(404, {"error": "not_found"})
            return
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("request must be an object")
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        streaming = bool(body.get("stream"))
        for route in candidates_for(body, client_wire, index):
            provider = route["provider"]
            model = route["model"]["id"]
            connection = response = None
            try:
                translated = provider_request(body, client_wire, provider["wire"], model)
                headers = provider_headers(provider)
                headers["Content-Type"] = "application/json"
                connection, response = open_http(
                    "POST", provider_post_url(provider, model, streaming),
                    body=json.dumps(translated).encode("utf-8"), headers=headers, timeout=25)
            except Exception:
                if connection is not None:
                    connection.close()
                continue
            if not 200 <= response.status < 300:
                response.close()
                connection.close()
                continue
            # A successful upstream status commits this request to this provider.
            # In particular, translation or stream failures must never cause a retry.
            try:
                if streaming:
                    self.start_stream()
                    translate_stream(self, response, provider["wire"], client_wire, model)
                else:
                    answer = response.read()
                    if provider["wire"] == client_wire:
                        self.send_bytes(200, answer)
                    else:
                        reduced = reduce_response(json.loads(answer), provider["wire"])
                        self.send_json(200, client_response(client_wire, model, *reduced))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except Exception:
                if streaming:
                    self.close_connection = True
                else:
                    self.send_json(502, error_response(client_wire))
            finally:
                response.close()
                connection.close()
            return
        self.send_json(502, error_response(client_wire))


def environment_json(method, path, document=None):
    body = None if document is None else json.dumps(document).encode("utf-8")
    headers = {} if document is None else {"Content-Type": "application/json"}
    connection, response = open_http(method, ENV_URL + path, body=body, headers=headers, timeout=5)
    try:
        raw = response.read()
        if not 200 <= response.status < 300:
            raise ValueError("environment request failed")
        return json.loads(raw) if raw else {}
    finally:
        response.close()
        connection.close()


def register(server, catalog):
    document = {"protocol_version": 1, "port": server.server_address[1], "catalog": catalog}
    for attempt in range(5):
        try:
            environment_json("POST", "/register", document)
            return
        except (OSError, http.client.HTTPException):
            if attempt == 4:
                raise
            sleep(0.2)


def poll_work():
    while True:
        try:
            answer = environment_json("GET", "/work")
            if isinstance(answer, dict) and answer.get("done") is True:
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        sleep(0.1)


def main():
    catalog = build_catalog()
    if "--list" in sys.argv[1:]:
        list_catalog(catalog)
        return 0
    server = AggregatorServer(("", 0), catalog)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        register(server, catalog)
        poll_work()
        return 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        exit_code = 0
    except Exception:
        exit_code = 1
    sys.exit(exit_code)

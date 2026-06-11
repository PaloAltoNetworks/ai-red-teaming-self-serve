#!/usr/bin/env python3
"""
CLARA Red Teaming Demo — Adversarial Prompt Scanner

Sends adversarial prompts from clara-prompts-dataset.csv against a customer's
AI application and collects responses for upload to Prisma AIRS.

Usage:
    uv run python run_scan.py --curl-file curl.txt [options]

The customer copies a curl command from browser devtools (Copy as cURL),
pastes it into a text file, and passes that file to this script.
"""

import argparse
import csv
import json
import os
import random
import shlex
import string
import sys
import time

import httpx
from httpx_sse import EventSource
from jsonpath_ng import parse as jp_parse


def parse_curl_file(curl_file_path):
    """Parse a curl command file into URL, headers dict, and body string."""
    with open(curl_file_path, "r") as f:
        raw = f.read()

    # Normalize line continuations (backslash-newline)
    raw = raw.replace("\\\n", " ")

    tokens = shlex.split(raw)

    url = None
    headers = {}
    body = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Skip the 'curl' command itself
        if tok.lower() == "curl":
            i += 1
            continue

        if tok == "-H" and i + 1 < len(tokens):
            header_val = tokens[i + 1]
            colon_pos = header_val.find(":")
            if colon_pos != -1:
                key = header_val[:colon_pos].strip()
                val = header_val[colon_pos + 1 :].strip()
                headers[key] = val
            i += 2
            continue

        if tok == "-b" and i + 1 < len(tokens):
            headers["Cookie"] = tokens[i + 1]
            i += 2
            continue

        if tok in ("--data-raw", "--data", "-d") and i + 1 < len(tokens):
            body = tokens[i + 1]
            # Chrome wraps bodies in ANSI-C quoting: --data-raw $'...'
            # shlex strips the quotes but preserves the $ prefix
            if body.startswith("$"):
                body = body[1:]
            i += 2
            continue

        # Flags to skip (consume their argument)
        if tok in ("-X", "--request", "--compressed", "--insecure", "-k"):
            if tok in ("-X", "--request"):
                i += 2
            else:
                i += 1
            continue

        # Positional argument = URL (skip flags we don't handle)
        if not tok.startswith("-"):
            url = tok
            i += 1
            continue

        i += 1

    if not url:
        print("Error: Could not find URL in curl command.", file=sys.stderr)
        sys.exit(1)
    if not body:
        print("Error: Could not find request body (--data-raw/-d) in curl command.", file=sys.stderr)
        sys.exit(1)

    # Strip Accept-Encoding to prevent compressed responses that urllib
    # can't auto-decompress (Chrome's "Copy as cURL" always includes this)
    headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

    return url, headers, body


def generate_session_id():
    """Generate a session ID matching the pattern session_{timestamp}_{random}."""
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    return f"session_{ts}_{rand}"


# --- JSON path helpers (thin wrappers around jsonpath-ng) ---

def get_by_path(obj, path):
    """Get a value from a nested object using a JSONPath expression.

    Examples: 'message', 'choices[0].message.content', 'content[0].text'
    """
    matches = jp_parse(path).find(obj)
    if not matches:
        raise KeyError(f"No match for path '{path}' in object")
    return matches[0].value


def set_by_path(obj, path, value):
    """Set a value in a nested object using a JSONPath expression.

    Preserves all sibling data in the structure.
    """
    expr = jp_parse(path)
    matches = expr.find(obj)
    if not matches:
        raise KeyError(f"No match for path '{path}' in object")
    matches[0].full_path.update(obj, value)
    return obj


# --- SSE streaming support ---

def _collect_sse_text(response):
    """Reassemble full response text from an SSE stream.

    Auto-detects and handles three SSE formats:
    - OpenAI Chat Completions: choices[0].delta.content
    - OpenAI Responses: response.output_text.delta events
    - Claude Messages: content_block_delta events
    """
    parts = []
    source = EventSource(response)

    for event in source.iter_sse():
        # OpenAI Chat Completions sends [DONE] as final data
        if event.data == "[DONE]":
            break

        # Skip non-data events that don't carry text
        if event.event in ("message_start", "content_block_start",
                           "content_block_stop", "message_delta",
                           "message_stop", "response.created",
                           "response.completed", "response.output_item.added",
                           "response.output_item.done",
                           "response.content_part.added",
                           "response.content_part.done",
                           "response.in_progress", "response.done",
                           "ping"):
            continue

        try:
            data = json.loads(event.data)
        except (json.JSONDecodeError, ValueError):
            continue

        text = None

        # Claude Messages: content_block_delta with delta.text
        if event.event == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")

        # OpenAI Responses: response.output_text.delta
        elif event.event == "response.output_text.delta":
            text = data.get("delta", "")

        # OpenAI Chat Completions: no event type, data has choices[0].delta.content
        elif not event.event or event.event == "message":
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content is not None:
                    text = content

        if text:
            parts.append(text)

    return "".join(parts)


def send_prompt(client, url, headers, body_template, prompt_text, request_field, response_field, shared_session_id=None):
    """Send a single prompt and return the extracted response text."""
    body_obj = json.loads(body_template)

    # Substitute the prompt at the specified path
    set_by_path(body_obj, request_field, prompt_text)

    # Set session ID
    if "session_id" in body_obj:
        body_obj["session_id"] = shared_session_id or generate_session_id()

    body_bytes = json.dumps(body_obj).encode("utf-8")

    req_headers = dict(headers)
    if "Content-Type" not in req_headers and "content-type" not in req_headers:
        req_headers["Content-Type"] = "application/json"

    with client.stream("POST", url, headers=req_headers, content=body_bytes) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return _collect_sse_text(resp)
        else:
            resp.read()
            resp_json = json.loads(resp.text)
            return str(get_by_path(resp_json, response_field))


def load_completed_uuids(output_path):
    """Load UUIDs already present in the output CSV (for resume support)."""
    completed = set()
    if not os.path.exists(output_path):
        return completed
    with open(output_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            completed.add(row["prompt_uuid"])
    return completed


def main():
    parser = argparse.ArgumentParser(
        description="CLARA Red Teaming Demo — send adversarial prompts to a customer AI app"
    )
    parser.add_argument(
        "--curl-file", required=True,
        help="Path to a file containing the curl command (copied from browser devtools)"
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds to wait between requests (default: 2)"
    )
    parser.add_argument(
        "--request-field", default="message",
        help="JSON path to the field in the request body where the prompt is placed "
             "(e.g. 'message', 'messages[-1].content', 'input') (default: message)"
    )
    parser.add_argument(
        "--response-field", default="response",
        help="JSON path to the field in the response to extract "
             "(e.g. 'response', 'choices[0].message.content', 'content[0].text') (default: response)"
    )
    parser.add_argument(
        "--shared-session", action="store_true",
        help="Reuse the same session_id for all requests instead of generating a fresh one per prompt"
    )
    args = parser.parse_args()

    # Resolve paths relative to this script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompts_path = os.path.join(script_dir, "clara-prompts-dataset.csv")
    results_dir = os.path.join(script_dir, "results")
    output_path = os.path.join(results_dir, "clara-responses.csv")

    # Parse curl
    print(f"Parsing curl command from: {args.curl_file}")
    url, headers, body_template = parse_curl_file(args.curl_file)
    print(f"Target URL: {url}")

    # Validate that the request field exists in the body
    body_check = json.loads(body_template)
    try:
        get_by_path(body_check, args.request_field)
    except (KeyError, Exception):
        print(
            f"Warning: path '{args.request_field}' not found in request body. "
            f"Top-level keys: {list(body_check.keys())}",
            file=sys.stderr,
        )
        print("Continuing anyway — the field will be added to the request body.")

    # Load prompts
    with open(prompts_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        prompts = list(reader)
    total = len(prompts)
    print(f"Loaded {total} prompts")

    # Resume support
    os.makedirs(results_dir, exist_ok=True)
    completed = load_completed_uuids(output_path)
    if completed:
        print(f"Resuming: {len(completed)} prompts already completed, {total - len(completed)} remaining")

    # Prepare shared session if needed
    shared_session_id = generate_session_id() if args.shared_session else None

    # Open output file in append mode
    write_header = not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    outfile = open(output_path, "a", newline="")
    writer = csv.DictWriter(outfile, fieldnames=["prompt_uuid", "target_response"])
    if write_header:
        writer.writeheader()

    succeeded = 0
    failed = 0
    skipped = len(completed)

    # Create HTTP client (verify=False for demo/self-signed certs)
    client = httpx.Client(verify=False, timeout=120.0)

    try:
        for idx, row in enumerate(prompts):
            uuid = row["prompt_uuid"]
            prompt_text = row["prompt"]

            if uuid in completed:
                continue

            current = skipped + succeeded + failed + 1
            print(f"[{current}/{total}] Sending prompt {uuid[:8]}...", end=" ", flush=True)

            try:
                response_text = send_prompt(
                    client, url, headers, body_template, prompt_text,
                    args.request_field, args.response_field,
                    shared_session_id,
                )
                writer.writerow({"prompt_uuid": uuid, "target_response": response_text})
                outfile.flush()
                succeeded += 1
                # Truncate response for display
                display = response_text[:80].replace("\n", " ")
                print(f"OK ({display}...)" if len(response_text) > 80 else f"OK ({display})")

            except httpx.HTTPStatusError as e:
                failed += 1
                print(f"FAILED: {e}")
                if e.response.status_code in (401, 403):
                    print(
                        "\nAuthentication error. Your session may have expired.\n"
                        "Copy a fresh curl command from browser devtools, save to file, and re-run.",
                        file=sys.stderr,
                    )
                    break
            except (httpx.RequestError, json.JSONDecodeError) as e:
                failed += 1
                print(f"FAILED: {e}")

            if args.delay > 0 and current < total:
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        outfile.close()
        client.close()

    print(f"\nDone. Succeeded: {succeeded}, Failed: {failed}, Skipped (already done): {skipped}")
    print(f"Results written to: {output_path}")


if __name__ == "__main__":
    main()

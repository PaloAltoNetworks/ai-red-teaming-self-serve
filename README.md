# CLARA Red Teaming Demo — Quick Start

This toolkit sends 300 adversarial prompts against your AI application and collects the responses for analysis in Prisma AIRS.

## Prerequisites

- **uv** — Install with:
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
  - Python is handled automatically by uv (no manual install needed)
- Access to your AI application in a browser
- Your Prisma AIRS demo tenant (for uploading results)

> **Important**: Ensure your AI application has **no security scanning enabled** during this test. The purpose is to collect raw, unfiltered responses so AIRS can analyze them after upload.

## Step 1: Copy Your Curl Command

1. Open your AI application in a browser (e.g., Chrome, Edge)
2. Open **Developer Tools** (F12 or right-click → Inspect)
3. Go to the **Network** tab
4. Send any message in your AI app's chat interface
5. Find the API request in the Network tab (usually a POST to an `/api/chat` or similar endpoint)
6. Right-click the request → **Copy** → **Copy as cURL**
7. Paste the copied command into a new text file and save it (e.g., `curl.txt`)

## Step 2: Run the Scan

```bash
uv run python run_scan.py --curl-file curl.txt
```

On first run, `uv` will automatically set up a Python environment. This takes a few seconds and only happens once.

The script will:
- Parse your curl command to extract the endpoint, headers, and authentication
- Send each of the 300 adversarial prompts to your application
- Save responses to `results/clara-responses.csv`
- Display progress as it runs

**Estimated time**: ~25 minutes at default settings (2-second delay between requests).

### Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--delay` | `2` | Seconds between requests. Reduce for faster runs if your app allows it. |
| `--request-field` | `message` | JSON path to the field in the request body where prompts are placed (e.g. `message`, `messages[-1].content`, `input`) |
| `--response-field` | `response` | JSON path to the field in the API response to extract (e.g. `response`, `choices[0].message.content`). Not needed for streaming responses — those are handled automatically. |
| `--shared-session` | off | Use the same session for all requests instead of isolated sessions |

Example with OpenAI Chat Completions API:

```bash
uv run python run_scan.py --curl-file curl.txt --request-field "messages[-1].content" --response-field "choices[0].message.content"
```

Example with faster delay:

```bash
uv run python run_scan.py --curl-file curl.txt --delay 0.5
```

## Step 3: Handle Interruptions

If the script stops due to an expired session or network error:

1. Go back to your browser and send another message in the app
2. Copy the new curl command (Step 1 above)
3. Save to `curl.txt` (overwrite the old one)
4. Re-run the same command — it will **automatically skip** prompts that were already completed

## Step 4: Upload Results

1. Find the output file at `results/clara-responses.csv`
2. Log in to your Prisma AIRS demo tenant
3. Upload the CSV file through the red teaming interface

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Error: Could not find URL in curl command` | Make sure the curl.txt file contains the full curl command copied from browser devtools |
| `Error: Could not find request body` | Your curl command may be missing `--data-raw`. Ensure you copied a POST request, not a GET |
| `Authentication error (401/403)` | Your session has expired. Copy a fresh curl command and re-run |
| `path 'message' not found in request body` | Your app uses a different JSON structure. Check the JSON body in your curl command and use `--request-field` with the correct JSON path (e.g. `messages[-1].content`) |
| `uv: command not found` | Install uv: see Prerequisites above |

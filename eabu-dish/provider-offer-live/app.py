import os
import json
import base64
import logging
from datetime import datetime, timezone
from my_utils import helpers
import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REQUIRED_FIELDS = [
    "itemId",
    "userId",
    "title",
    "description",
    "offerDescription",
    "phoneNumber",
    "email",
    "price"
]

# Resolve environment configuration
table_name = os.environ.get("LIVE_OFFER_TABLE_NAME", "eabu-live-offer")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)

SUBMIT_URL = "https://www.eabu.net/feedback/submit"

HTML_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Feedback</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background: #f6f7fb; color: #111827;
    }}
    .wrap {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; }}
    .card {{
      width: min(560px, 100%); background: #fff; border: 1px solid #e5e7eb; border-radius: 14px;
      box-shadow: 0 10px 30px rgba(17,24,39,.08); padding: 18px 18px 14px;
    }}
    h1 {{ font-size: 16px; margin: 0 0 10px; font-weight: 650; }}
    p  {{ margin: 0 0 14px; color: #4b5563; font-size: 13px; line-height: 1.4; }}

    textarea, input[type="email"] {{
      box-sizing: border-box;
      width: 100%;
      max-width: 100%;
      padding: 12px 12px;
      border-radius: 12px;
      border: 1px solid #d1d5db;
      outline: none;
      font-size: 14px;
      background: #fff;
    }}

    textarea {{
      min-height: 120px;
      resize: vertical;
    }}

    textarea:focus, input[type="email"]:focus {{
      border-color: #6366f1;
      box-shadow: 0 0 0 4px rgba(99,102,241,.15);
    }}

    .field {{ margin-top: 12px; }}
    .meta {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 6px;
      font-size: 12px;
      color: #6b7280;
    }}
    .meta .count {{ font-variant-numeric: tabular-nums; }}
    .meta .count.over {{ color: #991b1b; font-weight: 650; }}

    .row {{ display: flex; gap: 10px; align-items: center; margin-top: 12px; }}
    button {{
      border: 0; border-radius: 12px; padding: 10px 12px; font-weight: 650;
      background: #111827; color: #fff; cursor: pointer;
    }}
    button:disabled {{ opacity: .6; cursor: not-allowed; }}
    .status {{ font-size: 13px; color: #4b5563; }}
    .status.ok {{ color: #166534; }}
    .status.err {{ color: #991b1b; }}
  </style>
</head>
<body>
  <div class="wrap">
    <form class="card" id="feedbackForm" novalidate>
      <h1>Send feedback</h1>
      <p>Tell us what worked well and what we can improve.</p>

      <div class="field">
        <input
          id="email"
          name="email"
          type="email"
          inputmode="email"
          autocomplete="email"
          placeholder="Email (optional)"
        />
      </div>

      <div class="field">
        <textarea
          id="feedback"
          name="feedback"
          maxlength="2000"
          placeholder="Write your feedback… (max 250 words)"
          required
        ></textarea>
        <div class="meta">
          <span>Max 250 words.</span>
          <span id="wordCount" class="count" aria-live="polite">0/250</span>
        </div>
      </div>

      <div class="row">
        <button id="submitBtn" type="submit">Submit</button>
        <span id="status" class="status" aria-live="polite"></span>
      </div>
    </form>
  </div>

  <script>
    const ENDPOINT_URL = {SUBMIT_URL!r};
    const MAX_WORDS = 250;

    const form = document.getElementById("feedbackForm");
    const emailEl = document.getElementById("email");
    const textarea = document.getElementById("feedback");
    const btn = document.getElementById("submitBtn");
    const statusEl = document.getElementById("status");
    const wordCountEl = document.getElementById("wordCount");

    function setStatus(msg, kind) {{
      statusEl.textContent = msg || "";
      statusEl.className = "status" + (kind ? " " + kind : "");
    }}

    function countWords(text) {{
      // Count sequences of non-whitespace as words
      const m = (text || "").trim().match(/\\S+/g);
      return m ? m.length : 0;
    }}

    function updateWordUI() {{
      const words = countWords(textarea.value);
      wordCountEl.textContent = words + "/" + MAX_WORDS;
      const over = words > MAX_WORDS;
      wordCountEl.classList.toggle("over", over);
      btn.disabled = over;
      return words;
    }}

    textarea.addEventListener("input", updateWordUI);
    updateWordUI();

    form.addEventListener("submit", async (e) => {{
      e.preventDefault();

      const feedback = textarea.value.trim();
      const email = emailEl.value.trim();
      const words = updateWordUI();

      if (!feedback) {{
        setStatus("Please enter feedback.", "err");
        textarea.focus();
        return;
      }}

      if (words > MAX_WORDS) {{
        setStatus("Feedback must be 250 words or fewer.", "err");
        textarea.focus();
        return;
      }}

      if (email && !emailEl.checkValidity()) {{
        setStatus("Please enter a valid email or leave it blank.", "err");
        emailEl.focus();
        return;
      }}

      btn.disabled = true;
      setStatus("Sending…");

      try {{
        const res = await fetch(ENDPOINT_URL, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ feedback, email: email || undefined }})
        }});

        const data = await res.json().catch(() => ({{}}));

        if (!res.ok) {{
          setStatus((data && data.msg) || "Failed to send feedback.", "err");
          return;
        }}

        textarea.value = "";
        emailEl.value = "";
        updateWordUI();
        setStatus("Thanks! Your feedback was sent.", "ok");
      }} catch {{
        setStatus("Network error. Please try again.", "err");
      }} finally {{
        btn.disabled = false;
        updateWordUI();
      }}
    }});
  </script>
</body>
</html>
"""


def lambda_handler(event, context):
    method = (event.get("httpMethod") or "GET").upper()
    if method != "GET":
        return {
            "statusCode": 405,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": '{"msg":"Method Not Allowed"}',
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        "body": HTML_PAGE,
    }
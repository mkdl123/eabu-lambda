import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SUBMIT_URL = "https://www.eabu.net/feedback/submit"

HTML_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Feedback</title>
  <style>
    :root{{
      color-scheme: dark;
      --bg1:#0f172a;
      --bg2:#111827;
      --card:#0b1220cc;
      --text:#e5e7eb;
      --muted:#cbd5e1;
      --border:#334155;
      --shadow: 0 18px 50px rgba(0,0,0,.35);
      --radius: 18px;
      --accent:#38bdf8;
    }}
    *{{ box-sizing:border-box; }}
    body{{
      margin:0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
      color:var(--text);
      background:
        radial-gradient(1200px 600px at 20% 10%, rgba(56,189,248,.25), transparent 60%),
        radial-gradient(900px 500px at 80% 20%, rgba(167,139,250,.22), transparent 55%),
        linear-gradient(160deg, var(--bg1), var(--bg2));
      min-height:100vh;
    }}

    .wrap{{ max-width: 900px; margin: 0 auto; padding: 28px 18px 56px; }}

    .card{{
      border:1px solid rgba(51,65,85,.9);
      background: rgba(2,6,23,.35);
      backdrop-filter: blur(12px);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}

    .section{{ padding: 22px; }}
    .section + .section{{
      border-top: 1px solid rgba(51,65,85,.75);
      background: rgba(2,6,23,.18);
    }}

    h1{{ margin:0; font-size: 22px; letter-spacing: .2px; }}
    p{{ margin:10px 0 0; color: var(--muted); line-height: 1.5; }}

    .about ul{{
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.6;
    }}
    .about li{{ margin: 6px 0; }}

    .field{{ margin-top: 12px; display:grid; gap:10px; }}
    input, textarea{{
      width:100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid rgba(51,65,85,.85);
      background: rgba(2,6,23,.25);
      color: var(--text);
      outline: none;
    }}
    textarea{{ resize: vertical; min-height: 140px; }}
    input:focus, textarea:focus{{
      border-color: rgba(56,189,248,.65);
      box-shadow: 0 0 0 4px rgba(56,189,248,.10);
    }}

    .row{{ display:flex; gap:12px; margin-top: 16px; flex-wrap: wrap; align-items: center; }}

    .btn{{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid rgba(51,65,85,.9);
      background: rgba(15,23,42,.55);
      color: var(--text);
      font-weight: 600;
      font-size: 14px;
      cursor:pointer;
      text-decoration:none;
    }}
    .btn:hover{{ border-color: rgba(56,189,248,.7); }}
    .btn:disabled{{ opacity: .6; cursor: not-allowed; }}

    .meta{{
      display:flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 8px;
      font-size: 12.5px;
      color: var(--muted);
    }}
    .meta .count{{ font-variant-numeric: tabular-nums; }}
    .meta .count.over{{ color: #fca5a5; font-weight: 700; }}

    .status{{ font-size: 13px; color: var(--muted); }}
    .status.ok{{ color: #86efac; }}
    .status.err{{ color: #fca5a5; }}

    /* FAQ */
    .faq h2{{
      margin:0;
      font-size: 18px;
      letter-spacing: .2px;
    }}
    .faqList{{
      margin: 12px 0 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.6;
    }}
    .faqList li{{ margin: 10px 0; }}
    .faqList strong{{ color: var(--text); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <!-- Services / About section -->
      <section class="section about" aria-label="About Eabu services">
        <h1>What Eabu offers</h1>
        <p>Eabu helps you find and provide local services in three simple categories:</p>
        <ul>
          <li><strong>Food</strong> (for example: home-cooked meals, catering, food delivery help)</li>
          <li><strong>Household work</strong> (for example: cleaning, small repairs, help around the home)</li>
          <li><strong>Transportation</strong> (for example: local rides, pickup/drop-off help)</li>
        </ul>
        <p><strong>Currently, Eabu services are available through the Eabu app in Android. Please download the app! Happy Eabuing!</strong></p>

        <p>
          Providers can post what they offer, and customers can search and connect.
          Eabu <strong>does not handle payments</strong> — you can exchange contact details through Eabu and settle payment separately.
        </p>

        <p>
          Buyers and sellers can add an optional <strong>profile picture</strong>, which they can replace or delete at any time.
        </p>
      </section>

      <!-- Feedback section -->
      <section class="section" aria-label="Feedback form section">
        <form id="feedbackForm" novalidate>
          <h1>Send feedback or Contact Us</h1>
          <p>Tell us what worked well and what we can improve. Or leave your contact details so we can reach you.</p>

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
              placeholder="Write your feedback… (max 200 words)"
              required
            ></textarea>
            <div class="meta">
              <span>Max 200 words.</span>
              <span id="wordCount" class="count" aria-live="polite">0/200</span>
            </div>
          </div>

          <div class="row">
            <button id="submitBtn" class="btn" type="submit">Submit</button>
            <span id="status" class="status" aria-live="polite"></span>
          </div>
        </form>
      </section>

      <!-- FAQ section -->
      <section class="section faq" aria-label="Frequently asked questions">
        <h2>FAQ</h2>
        <ul class="faqList">
          <li>
            <strong>Is Eabu Services Free to use?</strong><br />
            Yes — the service for consumers/buyers is free. For service providers/sellers it’s free to some extent; please contact us to learn more.
          </li>
          <li>
            <strong>Is it safe to use Eabu services?</strong><br />
            Eabu does not verify the identity of service providers/sellers, so you should do your due diligence before using products or services from a provider. Eabu does not verify any services or products of sellers.
          </li>
        </ul>
      </section>
    </div>
  </div>

  <script>
    const ENDPOINT_URL = {SUBMIT_URL!r};
    const MAX_WORDS = 200;

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
        setStatus("Feedback must be 200 words or fewer.", "err");
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
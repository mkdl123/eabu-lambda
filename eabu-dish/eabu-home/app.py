import base64
import json
import os


def lambda_handler(event, context):
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"

    if path == "/eabu-feedback":
        feedback_html = """<!DOCTYPE html><html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>EABU Feedback</title>
  <style>
    :root{
      --bg1:#0f172a;
      --bg2:#111827;
      --card:#0b1220cc;
      --text:#e5e7eb;
      --muted:#cbd5e1;
      --border:#334155;
      --shadow: 0 18px 50px rgba(0,0,0,.35);
      --radius: 18px;
      --accent:#38bdf8;
    }
    *{ box-sizing:border-box; }
    body{
      margin:0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
      color:var(--text);
      background:
        radial-gradient(1200px 600px at 20% 10%, rgba(56,189,248,.25), transparent 60%),
        radial-gradient(900px 500px at 80% 20%, rgba(167,139,250,.22), transparent 55%),
        linear-gradient(160deg, var(--bg1), var(--bg2));
      min-height:100vh;
    }
    .wrap{ max-width: 900px; margin: 0 auto; padding: 28px 18px 56px; }
    .card{
      border:1px solid rgba(51,65,85,.9);
      background: rgba(2,6,23,.35);
      backdrop-filter: blur(12px);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 22px;
    }
    h1{ margin:0; font-size: 22px; letter-spacing: .2px; }
    p{ margin:10px 0 0; color: var(--muted); line-height: 1.5; }
    .row{ display:flex; gap:12px; margin-top: 16px; flex-wrap: wrap; }
    .btn{
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
    }
    .btn:hover{ border-color: rgba(56,189,248,.7); }
    .field{ margin-top: 12px; display:grid; gap:10px; }
    input, textarea{
      width:100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid rgba(51,65,85,.85);
      background: rgba(2,6,23,.25);
      color: var(--text);
      outline: none;
    }
    textarea{ resize: vertical; min-height: 140px; }
    input:focus, textarea:focus{
      border-color: rgba(56,189,248,.65);
      box-shadow: 0 0 0 4px rgba(56,189,248,.10);
    }
    .hint{ font-size: 12.5px; color: var(--muted); margin-top: 8px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Contact Support</h1>
      <p>Share your question or feedback and we’ll get back to you.</p>

      <form class="field" onsubmit="event.preventDefault(); alert('Thanks — your message has been recorded locally in this demo.');">
        <input type="email" placeholder="Your email" required />
        <textarea placeholder="How can we help?" maxlength="250" required></textarea>
        <div class="row">
          <button class="btn" type="submit">Send</button>
          <a class="btn" href="/" aria-label="Back to home">Back</a>
        </div>
        <div class="hint">Max 250 characters.</div>
      </form>
    </div>
  </div>
</body>
</html>"""
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": feedback_html,
            "isBase64Encoded": False,
        }

    html_content = """<!DOCTYPE html><html lang="en">
           <head>
             <meta charset="utf-8" />
             <meta name="viewport" content="width=device-width, initial-scale=1" />
             <title>Local Services Connector</title>
             <style>
               :root{
                 --bg1:#0f172a;
                 --bg2:#111827;
                 --card:#0b1220cc;
                 --text:#e5e7eb;
                 --muted:#cbd5e1;
                 --accent:#38bdf8;
                 --accent2:#a78bfa;
                 --border:#334155;
                 --shadow: 0 18px 50px rgba(0,0,0,.35);
                 --radius: 18px;
                 --ring: 0 0 0 4px rgba(56,189,248,.14);
               }

               *{ box-sizing:border-box; }
               body{
                 margin:0;
                 font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
                 color:var(--text);
                 background:
                   radial-gradient(1200px 600px at 20% 10%, rgba(56,189,248,.25), transparent 60%),
                   radial-gradient(900px 500px at 80% 20%, rgba(167,139,250,.22), transparent 55%),
                   linear-gradient(160deg, var(--bg1), var(--bg2));
                 min-height:100vh;
               }

               a{ color:inherit; text-decoration:none; }
               .wrap{
                 max-width: 1080px;
                 margin: 0 auto;
                 padding: 28px 18px 56px;
               }

               header{
                 display:flex;
                 align-items:center;
                 justify-content:space-between;
                 gap:12px;
                 padding: 10px 4px 26px;
               }

               .brand{
                 display:flex;
                 align-items:center;
                 gap:12px;
                 min-width: 220px;
               }

               .logo{
                 width:42px;height:42px;
                 border-radius: 14px;
                 object-fit: cover;
                 display:block;
                 box-shadow: 0 10px 30px rgba(56,189,248,.18);
               }

               .brand h1{
                 margin:0;
                 font-size: 15px;
                 letter-spacing:.4px;
                 font-weight:700;
               }
               .brand p{
                 margin:2px 0 0;
                 font-size: 12.5px;
                 color: var(--muted);
               }

               nav{
                 display:flex;
                 gap:10px;
                 flex-wrap:wrap;
                 justify-content:flex-end;
               }
               .pill{
                 padding: 10px 12px;
                 border: 1px solid rgba(51,65,85,.9);
                 border-radius: 999px;
                 background: rgba(2,6,23,.25);
                 backdrop-filter: blur(10px);
                 font-size: 13px;
                 color: var(--muted);
                 transition: transform .12s ease, border-color .12s ease, background .12s ease, box-shadow .12s ease;
                 position: relative;
                 overflow: hidden;
               }
               .pill::before{
                 content:"";
                 position:absolute;
                 inset:-2px;
                 background: linear-gradient(135deg, rgba(56,189,248,.30), rgba(167,139,250,.22));
                 opacity: 0;
                 transition: opacity .12s ease;
               }
               .pill > span{ position: relative; z-index: 1; }
               .pill:hover{
                 transform: translateY(-1px);
                 border-color: rgba(56,189,248,.65);
                 background: rgba(2,6,23,.35);
                 color: var(--text);
                 box-shadow: 0 10px 30px rgba(0,0,0,.22);
               }
               .pill:hover::before{ opacity: .75; }
               .pill:focus-visible{
                 outline: none;
                 box-shadow: var(--ring), 0 12px 30px rgba(0,0,0,.25);
               }
               .pill .hintDot{
                 display:inline-block;
                 width:8px;height:8px;
                 border-radius: 999px;
                 margin-right: 8px;
                 background: radial-gradient(circle at 30% 30%, rgba(56,189,248,1), rgba(167,139,250,1));
                 box-shadow: 0 0 0 4px rgba(56,189,248,.10);
                 vertical-align: middle;
               }
               .pill .arrow{
                 margin-left: 8px;
                 opacity: .9;
               }

               .hero{
                 display:grid;
                 grid-template-columns: 1.1fr .9fr;
                 gap: 18px;
                 align-items:stretch;
                 margin-top: 8px;
               }
               @media (max-width: 860px){
                 .hero{ grid-template-columns:1fr; }
                 nav{ justify-content:flex-start; }
                 header{ flex-direction:column; align-items:flex-start; }
               }

               .card{
                 border:1px solid rgba(51,65,85,.9);
                 background: rgba(2,6,23,.35);
                 backdrop-filter: blur(12px);
                 border-radius: var(--radius);
                 box-shadow: var(--shadow);
                 padding: 22px;
               }

               .headline{
                 margin:0;
                 font-size: 34px;
                 line-height: 1.1;
                 letter-spacing: -0.6px;
               }
               .subhead{
                 margin: 12px 0 0;
                 color: var(--muted);
                 font-size: 15.5px;
                 line-height: 1.55;
                 max-width: 62ch;
               }

               .ctaRow{
                 display:flex;
                 gap:12px;
                 flex-wrap:wrap;
                 margin-top: 18px;
               }
               .btn{
                 display:inline-flex;
                 align-items:center;
                 justify-content:center;
                 gap:10px;
                 padding: 12px 14px;
                 border-radius: 12px;
                 border: 1px solid rgba(51,65,85,.9);
                 background: rgba(15,23,42,.55);
                 color: var(--text);
                 font-weight: 600;
                 font-size: 14px;
                 transition: transform .12s ease, border-color .12s ease, background .12s ease;
                 cursor:pointer;
               }
               .btn:hover{
                 transform: translateY(-1px);
                 border-color: rgba(56,189,248,.7);
                 background: rgba(15,23,42,.7);
               }
               .btn.primary{
                 border-color: rgba(56,189,248,.55);
                 background: linear-gradient(135deg, rgba(56,189,248,.22), rgba(167,139,250,.20));
               }

               .grid{
                 display:grid;
                 grid-template-columns: repeat(3, 1fr);
                 gap: 14px;
                 margin-top: 14px;
               }
               @media (max-width: 860px){
                 .grid{ grid-template-columns:1fr; }
               }

               .feature{
                 padding: 16px;
                 border-radius: 16px;
                 border: 1px solid rgba(51,65,85,.85);
                 background: rgba(2,6,23,.25);
                 min-height: 120px;
               }

               .icon{
                 width: 40px; height: 40px;
                 border-radius: 14px;
                 display:grid;
                 place-items:center;
                 border: 1px solid rgba(56,189,248,.35);
                 background: rgba(56,189,248,.10);
                 margin-bottom: 10px;
                 font-size: 18px;
               }

               .feature h3{
                 margin: 0;
                 font-size: 15px;
                 letter-spacing: .2px;
               }
               .feature p{
                 margin: 8px 0 0;
                 color: var(--muted);
                 font-size: 13.5px;
                 line-height: 1.45;
               }

               .aside h2{
                 margin:0;
                 font-size: 16px;
                 letter-spacing: .2px;
               }
               .aside p{
                 margin:10px 0 0;
                 color: var(--muted);
                 font-size: 13.5px;
                 line-height: 1.5;
               }

               .list{
                 margin: 14px 0 0;
                 padding: 0;
                 list-style:none;
                 display:grid;
                 gap:10px;
               }
               .li{
                 display:flex;
                 gap:10px;
                 align-items:flex-start;
                 padding: 12px 12px;
                 border-radius: 14px;
                 border: 1px solid rgba(51,65,85,.75);
                 background: rgba(2,6,23,.22);
               }
               .dot{
                 width: 10px; height: 10px;
                 border-radius: 99px;
                 margin-top: 5px;
                 background: radial-gradient(circle at 30% 30%, rgba(56,189,248,1), rgba(167,139,250,1));
                 box-shadow: 0 0 0 4px rgba(56,189,248,.10);
                 flex: 0 0 auto;
               }
               .li strong{ display:block; font-size: 13.5px; }
               .li span{ display:block; color: var(--muted); font-size: 12.8px; margin-top: 2px; }

               footer{
                 margin-top: 18px;
                 color: rgba(203,213,225,.85);
                 font-size: 12.5px;
                 display:flex;
                 flex-wrap:wrap;
                 gap:10px;
                 justify-content:space-between;
                 padding: 10px 6px 0;
               }
             </style>
           </head>

           <body>
             <div class="wrap">
               <header>
                 <div class="brand">
                   <img class="logo" src="eabu.png" alt="EABU logo" />
                   <div>
                     <h1>Local Services Connector</h1>
                     <p>Find trusted help nearby in minutes</p>
                   </div>
                 </div>

                 <nav aria-label="Primary">
                   <a class="pill" href="https://www.eabu.net/feedback" target="_blank" rel="noopener noreferrer"
                      aria-label="Open Services, How it works, Safety and Support in a new tab">
                     <span><span class="hintDot" aria-hidden="true"></span>Services / How it works / Safety / Support<span class="arrow" aria-hidden="true">↗</span></span>
                   </a>
                 </nav>
               </header>

               <section class="hero" aria-label="Hero">
                 <div class="card">
                   <h2 class="headline">Connect locally for everyday services.</h2>
                   <p class="subhead">
                     This app allows users to connect locally for the services in
                     <strong>Food</strong>, <strong>HouseHold Helps</strong> and <strong>Transportations</strong>.
                   </p>

                   <div class="ctaRow">
                     <a class="btn primary" href="#services" role="button" aria-label="Explore services">
                       Explore services
                       <span aria-hidden="true">→</span>
                     </a>
                     <a class="btn" href="#how" role="button" aria-label="See how it works">
                       How it works
                     </a>
                   </div>

                   <div id="services" class="grid" aria-label="Service categories">
                     <div class="feature">
                       <div class="icon" aria-hidden="true">🍽️</div>
                       <h3>Food</h3>
                       <p>Discover local food options and connect with nearby providers for quick, convenient service.</p>
                     </div>

                     <div class="feature">
                       <div class="icon" aria-hidden="true">🧹</div>
                       <h3>HouseHold Helps</h3>
                       <p>Find help for everyday tasks—cleaning, small chores, and assistance around the home.</p>
                     </div>

                     <div class="feature">
                       <div class="icon" aria-hidden="true">🚗</div>
                       <h3>Transportations</h3>
                       <p>Connect for local transport needs—rides, deliveries, and getting from A to B with ease.</p>
                     </div>
                   </div>
                 </div>

                 <aside class="card aside" aria-label="Information panel">
                   <h2 id="how">How it works</h2>
                   <p>
                     Choose a service category, share your location, and connect with nearby providers.
                     Designed to be simple, fast, and customer-friendly.
                   </p>

                   <ul class="list" id="safety" aria-label="Key highlights">
                     <li class="li">
                       <div class="dot" aria-hidden="true"></div>
                       <div>
                         <strong>Local-first experience</strong>
                         <span>Connect to services available in your area.</span>
                       </div>
                     </li>
                     <li class="li">
                       <div class="dot" aria-hidden="true"></div>
                       <div>
                         <strong>Clear categories</strong>
                         <span>Food, HouseHold Helps, and Transportations—easy to browse.</span>
                       </div>
                     </li>
                     <li class="li">
                       <div class="dot" aria-hidden="true"></div>
                       <div>
                         <strong>Customer-friendly design</strong>
                         <span>Clean layout, readable text, and mobile-ready styling.</span>
                       </div>
                     </li>
                   </ul>

                   <div class="ctaRow" id="support">
                     <a class="btn" href="https://www.eabu.net/feedback" target="_blank" rel="noopener noreferrer" role="button" aria-label="Contact support">
                       Contact support
                     </a>
                     <a class="btn" href="https://www.eabu.net/feedback" target="_blank" rel="noopener noreferrer" role="button" aria-label="Frequently asked questions">
                       FAQs
                     </a>
                   </div>
                 </aside>
               </section>

               <footer aria-label="Footer">
                 <span>© <span id="year">2026</span> Local Services Connector</span>
                 <span>Built for a smooth customer experience</span>
               </footer>
             </div>

             <script>
               document.getElementById("year").textContent = String(new Date().getFullYear());
             </script>
           </body>
           </html>"""

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8"
        },
        "body": html_content,
        "isBase64Encoded": False
    }
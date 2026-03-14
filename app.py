import os
import re
import time
import base64
import json
import logging
import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Rotate through a few different user agents
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

def get_headers(i=0):
    return {
        "User-Agent": UA_LIST[i % len(UA_LIST)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,en-GB;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def extract_riders_from_image(image_b64: str, mime_type: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system="""You read Belgian cycling race start lists. Extract every rider.
Format is usually: NUMBER  SURNAME Firstname  TEAM  Category  UCI
Names are ALL-CAPS surname + capitalised firstname.
Keep Belgian prefixes (Van, De, Van den, Van der, Van de) with the surname.
Return ONLY valid JSON, nothing else:
{"riders":[{"bib":"55","name":"Firstname Surname","team":"Team Name"}]}
Convert to normal caps: VAN DEN BERGHE Nathan -> Nathan Van Den Berghe""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                {"type": "text", "text": "Extract all riders. Return only JSON."}
            ]
        }]
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    data = json.loads(text)
    return data.get("riders", [])


def search_firstcycling(name: str, attempt: int = 0) -> tuple[int | None, str]:
    """Search FirstCycling for a rider. Returns (rider_id, debug_info)."""
    debug = []
    try:
        # Try both "Firstname Lastname" and "Lastname Firstname"
        parts = name.strip().split()
        queries = [name]
        if len(parts) >= 2:
            queries.append(f"{parts[-1]} {' '.join(parts[:-1])}")  # reversed

        session = requests.Session()

        for query in queries:
            encoded = requests.utils.quote(query)
            url = f"https://firstcycling.com/search.php?q={encoded}&cat=rider"
            debug.append(f"GET {url}")

            resp = session.get(url, headers=get_headers(attempt), timeout=10, allow_redirects=True)
            debug.append(f"Status: {resp.status_code}, URL: {resp.url}")

            if resp.status_code != 200:
                continue

            # Check if we were redirected directly to a rider page
            if "rider.php?r=" in resp.url:
                m = re.search(r"r=(\d+)", resp.url)
                if m:
                    debug.append(f"Redirected to rider {m.group(1)}")
                    return int(m.group(1)), "\n".join(debug)

            soup = BeautifulSoup(resp.text, "html.parser")

            # Strategy 1: find <a href="/rider.php?r=NNN">
            links = soup.find_all("a", href=re.compile(r"/rider\.php\?r=\d+"))
            debug.append(f"Rider links found: {len(links)}")
            for l in links[:3]:
                debug.append(f"  Link: {l.get('href')} text={l.get_text(strip=True)[:40]}")

            if links:
                m = re.search(r"r=(\d+)", links[0]["href"])
                if m:
                    return int(m.group(1)), "\n".join(debug)

            # Strategy 2: look for any link with the name in text
            all_links = soup.find_all("a", href=True)
            for l in all_links:
                href = l.get("href", "")
                if "rider.php" in href and "r=" in href:
                    m = re.search(r"r=(\d+)", href)
                    if m:
                        debug.append(f"Found via all_links: {href}")
                        return int(m.group(1)), "\n".join(debug)

            time.sleep(0.5)

    except Exception as e:
        debug.append(f"Exception: {e}")

    return None, "\n".join(debug)


def get_rider_results(rider_id: int, years=(2024, 2025)) -> dict:
    podiums = 0
    top10s = 0
    wins = 0
    notable = []

    for year in years:
        try:
            url = f"https://firstcycling.com/rider.php?r={rider_id}&y={year}"
            resp = requests.get(url, headers=get_headers(), timeout=10)
            log.info(f"Results fetch {url}: {resp.status_code}")
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # FirstCycling results table — find rows with position data
            # Look for the main results table (has date, pos, race columns)
            rows = soup.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                try:
                    # Position is usually in col index 1
                    pos_text = cols[1].get_text(strip=True)
                    pos_text = re.sub(r'[^\d]', '', pos_text)
                    if not pos_text or len(pos_text) > 3:
                        continue
                    pos = int(pos_text)
                    if pos == 0 or pos > 200:
                        continue

                    race_name = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                    # Skip blank race names and obvious non-race rows
                    if not race_name or len(race_name) < 3:
                        continue

                    if pos <= 10:
                        top10s += 1
                        if pos <= 3:
                            podiums += 1
                            if pos == 1:
                                wins += 1
                                notable.append(f"🏆 {race_name} ({year})")
                            else:
                                notable.append(f"P{pos} {race_name} ({year})")
                except (ValueError, IndexError):
                    continue

        except Exception as e:
            log.warning(f"Results error for rider {rider_id} year {year}: {e}")
        time.sleep(0.3)

    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


def prerank_by_knowledge(riders: list[dict]) -> list[str]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    rider_list = "\n".join([f"- {r['bib']} {r['name']} ({r.get('team','')})" for r in riders])
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Belgian cycling expert. From this 1.12B start list, pick the 20 most likely strongest riders based on team quality and name recognition.

{rider_list}

Strong teams to prioritise: Urbano-Vulsteke, VDM-Trawobo, Vetrapo, Shifting Gears, Debondt Verandas, HUBO-Scott, A.S. Construct-Castaar, Stageco, Van Eyck Sport-Josan, Baloise-Glowi Lions, Soudal Quick-Step Devo, Lotto Dstny Dev.

Reply ONLY with JSON: {{"top20":["Full Name","Full Name"]}}"""
        }]
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    data = json.loads(text)
    return data.get("top20", [])[:20]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images uploaded"}), 400

    all_riders = []
    for f in files[:2]:
        raw = f.read()
        b64 = base64.b64encode(raw).decode()
        mime = f.content_type or "image/jpeg"
        if len(raw) > 3_000_000:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((1400, 1400))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode()
                mime = "image/jpeg"
            except Exception:
                pass
        try:
            riders = extract_riders_from_image(b64, mime)
            all_riders.extend(riders)
        except Exception as e:
            return jsonify({"error": f"Failed to extract riders: {str(e)}"}), 500

    seen = set()
    unique_riders = []
    for r in all_riders:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique_riders.append(r)

    if not unique_riders:
        return jsonify({"error": "No riders found in images"}), 400

    log.info(f"Extracted {len(unique_riders)} riders")

    try:
        top20_names = prerank_by_knowledge(unique_riders)
        log.info(f"Pre-ranked top 20: {top20_names[:5]}...")
    except Exception as e:
        log.warning(f"Pre-rank failed: {e}")
        top20_names = [r["name"] for r in unique_riders[:20]]

    bib_lookup = {r["name"]: r.get("bib", "?") for r in unique_riders}
    team_lookup = {r["name"]: r.get("team", "") for r in unique_riders}

    results = []
    for i, name in enumerate(top20_names):
        rider_id, debug = search_firstcycling(name, attempt=i)
        log.info(f"Search '{name}': id={rider_id}\n{debug}")

        if rider_id:
            stats = get_rider_results(rider_id, years=(2024, 2025))
            log.info(f"  -> wins={stats['wins']} podiums={stats['podiums']} top10={stats['top10s']}")
        else:
            stats = {"wins": None, "podiums": None, "top10s": None, "notable": []}

        results.append({
            "bib": bib_lookup.get(name, "?"),
            "name": name,
            "team": team_lookup.get(name, ""),
            "wins": stats["wins"],
            "podiums": stats["podiums"],
            "top10s": stats["top10s"],
            "notable": stats["notable"],
            "found": rider_id is not None,
            "fc_id": rider_id,
        })
        time.sleep(0.3)

    # Sort: found riders first (by podiums), then not-found
    found = [r for r in results if r["found"]]
    not_found = [r for r in results if not r["found"]]
    found.sort(key=lambda r: (-(r["podiums"] or 0), -(r["top10s"] or 0)))
    top10 = (found + not_found)[:10]

    return jsonify({
        "total_riders": len(unique_riders),
        "searched": len(top20_names),
        "rankings": top10
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

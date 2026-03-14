import os
import re
import time
import base64
import json
import logging
import anthropic
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

FC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Referer": "https://firstcycling.com/",
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


def search_and_get_results(rider: dict) -> dict:
    """Search FirstCycling for a rider and get their results. Returns enriched rider dict."""
    name = rider["name"]
    result = {**rider, "wins": None, "podiums": None, "top10s": None, "notable": [], "found": False}

    try:
        # Search by name
        encoded = requests.utils.quote(name)
        url = f"https://firstcycling.com/search.php?q={encoded}&cat=rider"
        resp = requests.get(url, headers=FC_HEADERS, timeout=10, allow_redirects=True)

        rider_id = None

        # If redirected straight to a rider page
        if resp.status_code == 200 and "rider.php?r=" in resp.url:
            m = re.search(r"r=(\d+)", resp.url)
            if m:
                rider_id = int(m.group(1))

        # Parse search results page
        if not rider_id and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.find_all("a", href=re.compile(r"/rider\.php\?r=\d+"))
            if links:
                m = re.search(r"r=(\d+)", links[0]["href"])
                if m:
                    rider_id = int(m.group(1))

        if not rider_id:
            log.info(f"Not found on FC: {name}")
            return result

        log.info(f"Found FC id={rider_id} for {name}")
        result["found"] = True

        # Fetch results for 2023, 2024, 2025
        wins, podiums, top10s, notable = 0, 0, 0, []
        for year in (2023, 2024, 2025):
            try:
                r = requests.get(
                    f"https://firstcycling.com/rider.php?r={rider_id}&y={year}",
                    headers=FC_HEADERS, timeout=10
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                for row in soup.find_all("tr"):
                    cols = row.find_all("td")
                    if len(cols) < 3:
                        continue
                    pos_raw = re.sub(r'[^\d]', '', cols[1].get_text(strip=True))
                    if not pos_raw or len(pos_raw) > 3:
                        continue
                    pos = int(pos_raw)
                    if pos == 0 or pos > 200:
                        continue
                    race = cols[2].get_text(strip=True)
                    if not race or len(race) < 3:
                        continue
                    if pos <= 10:
                        top10s += 1
                        if pos <= 3:
                            podiums += 1
                            if pos == 1:
                                wins += 1
                                notable.append(f"🏆 {race} ({year})")
                            else:
                                notable.append(f"P{pos} {race} ({year})")
                time.sleep(0.2)
            except Exception as e:
                log.warning(f"Results error {rider_id} {year}: {e}")

        result.update({"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:3]})

    except Exception as e:
        log.warning(f"search_and_get_results error for {name}: {e}")

    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images uploaded"}), 400

    # Extract riders from images
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

    # Deduplicate
    seen = set()
    unique_riders = []
    for r in all_riders:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique_riders.append(r)

    if not unique_riders:
        return jsonify({"error": "No riders found in images"}), 400

    log.info(f"Extracted {len(unique_riders)} unique riders")

    # Search ALL riders on FirstCycling in parallel (max 10 threads)
    enriched = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(search_and_get_results, r): r for r in unique_riders}
        for future in as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception as e:
                log.warning(f"Future error: {e}")
                enriched.append(futures[future])

    # Sort: found riders by podiums desc, then top10s; not-found at bottom
    found = [r for r in enriched if r.get("found")]
    not_found = [r for r in enriched if not r.get("found")]
    found.sort(key=lambda r: (-(r.get("podiums") or 0), -(r.get("top10s") or 0), -(r.get("wins") or 0)))

    top10 = (found + not_found)[:10]

    return jsonify({
        "total_riders": len(unique_riders),
        "found_on_fc": len(found),
        "rankings": top10
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

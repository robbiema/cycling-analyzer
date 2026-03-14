import os
import re
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


def find_rider_id(name: str) -> int | None:
    """Search FirstCycling for a rider by name, return their numeric ID."""
    try:
        encoded = requests.utils.quote(name)
        url = f"https://firstcycling.com/search.php?q={encoded}&cat=rider"
        resp = requests.get(url, headers=FC_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None
        # Redirected straight to rider page?
        if "rider.php?r=" in resp.url:
            m = re.search(r"r=(\d+)", resp.url)
            if m:
                return int(m.group(1))
        # Parse search results
        soup = BeautifulSoup(resp.text, "html.parser")
        link = soup.find("a", href=re.compile(r"/rider\.php\?r=\d+"))
        if link:
            m = re.search(r"r=(\d+)", link["href"])
            if m:
                return int(m.group(1))
    except Exception as e:
        log.warning(f"find_rider_id error for {name}: {e}")
    return None


def get_rider_results(rider_id: int) -> dict:
    """
    Fetch results for a rider from FirstCycling for 2023-2025.
    Parses the actual results table correctly using pandas-style column detection.
    """
    wins, podiums, top10s, notable = 0, 0, 0, []

    for year in (2023, 2024, 2025):
        try:
            url = f"https://firstcycling.com/rider.php?r={rider_id}&y={year}"
            resp = requests.get(url, headers=FC_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # The results table on FC has a specific structure:
            # thead with columns: Date | Pos | (blank/flag) | Race | Cat | UCI | (more)
            # We need to find the correct table by looking for thead with "Pos" header
            results_table = None
            for table in soup.find_all("table"):
                thead = table.find("thead")
                if thead:
                    headers = [th.get_text(strip=True) for th in thead.find_all("th")]
                    if "Pos" in headers and ("Race" in headers or any("race" in h.lower() for h in headers)):
                        results_table = table
                        headers_list = headers
                        break

            if not results_table:
                # Fallback: any table with a Pos column
                for table in soup.find_all("table"):
                    text = table.get_text()
                    if "Pos" in text and len(table.find_all("tr")) > 3:
                        results_table = table
                        # Infer column indices
                        headers_list = []
                        break

            if not results_table:
                continue

            # Find column indices
            pos_idx = None
            race_idx = None
            if headers_list:
                for i, h in enumerate(headers_list):
                    if h == "Pos":
                        pos_idx = i
                    if h in ("Race", "Race name") or "race" in h.lower():
                        race_idx = i
            # Default fallbacks based on typical FC layout: Date(0) Pos(1) flag(2) Race(3)
            if pos_idx is None:
                pos_idx = 1
            if race_idx is None:
                race_idx = 3

            for row in results_table.find_all("tr"):
                cols = row.find_all("td")
                if not cols or len(cols) <= max(pos_idx, race_idx):
                    continue
                pos_text = re.sub(r'[^\d]', '', cols[pos_idx].get_text(strip=True))
                if not pos_text or len(pos_text) > 3:
                    continue
                pos = int(pos_text)
                if pos == 0 or pos > 200:
                    continue
                race_name = cols[race_idx].get_text(strip=True)
                # Skip rows where race name looks like a number or is too short
                if not race_name or len(race_name) < 4 or race_name.isdigit():
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

        except Exception as e:
            log.warning(f"get_rider_results error rider={rider_id} year={year}: {e}")

    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


def process_rider(rider: dict) -> dict:
    result = {**rider, "wins": None, "podiums": None, "top10s": None, "notable": [], "found": False}
    rider_id = find_rider_id(rider["name"])
    if rider_id:
        stats = get_rider_results(rider_id)
        result.update({**stats, "found": True, "fc_id": rider_id})
        log.info(f"✓ {rider['name']}: W={stats['wins']} P={stats['podiums']} T10={stats['top10s']}")
    else:
        log.info(f"✗ {rider['name']}: not found on FC")
    return result


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

    log.info(f"Processing {len(unique_riders)} riders in parallel...")

    enriched = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(process_rider, r): r for r in unique_riders}
        for future in as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception as e:
                log.warning(f"Future error: {e}")
                enriched.append(futures[future])

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

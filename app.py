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
from PIL import Image
import io

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


def resize_image(raw: bytes, max_width=1400) -> tuple[str, str]:
    """Resize image and return (base64, mime_type)."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return base64.b64encode(raw).decode(), "image/jpeg"


def extract_riders_from_image(image_b64: str, mime_type: str) -> list[dict]:
    """Extract riders from a start list image using Claude vision."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Two-shot approach: first extract raw text, then parse it
    # This avoids Claude trying to format JSON while also reading the image
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": image_b64}
                },
                {
                    "type": "text",
                    "text": """This is a Belgian cycling race start list (deelnemerslijst).

Read every single row and extract:
- Bib number (RUG column, leftmost number)
- Rider name (NAAM column — ALL CAPS surname + firstname, convert to normal capitalisation)
- Team name (Club column)

Belgian name prefixes like VAN, DE, VAN DEN, VAN DER, VAN DE must stay attached to the surname.
Example: VAN DEN BERGHE Nathan → Nathan Van Den Berghe
Example: DE MILDE Thomas → Thomas De Milde

Return ONLY a JSON array like this, with no other text before or after:
[
  {"bib": "55", "name": "Nathan Van Den Berghe", "team": "VP Consulting"},
  {"bib": "23", "name": "Thomas De Milde", "team": "VP Consulting"}
]"""
                }
            ]
        }]
    )

    raw = response.content[0].text.strip()
    log.info(f"Raw extraction response (first 300 chars): {raw[:300]}")

    # Strip any markdown fences
    raw = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'^```\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Try to find a JSON array
    # Sometimes Claude wraps it in {"riders": [...]} — handle both
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("riders", "data", "results"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
    except json.JSONDecodeError:
        pass

    # Try to extract just the array part
    array_match = re.search(r'\[[\s\S]*\]', raw)
    if array_match:
        try:
            parsed = json.loads(array_match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    log.error(f"Could not parse extraction response: {raw[:500]}")
    raise ValueError(f"Could not parse rider list from image. Raw response started with: {raw[:200]}")


def find_rider_id(name: str) -> int | None:
    try:
        encoded = requests.utils.quote(name)
        url = f"https://firstcycling.com/search.php?q={encoded}&cat=rider"
        resp = requests.get(url, headers=FC_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None
        if "rider.php?r=" in resp.url:
            m = re.search(r"r=(\d+)", resp.url)
            if m:
                return int(m.group(1))
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
    wins, podiums, top10s, notable = 0, 0, 0, []

    for year in (2023, 2024, 2025):
        try:
            url = f"https://firstcycling.com/rider.php?r={rider_id}&y={year}"
            resp = requests.get(url, headers=FC_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Find the results table — look for thead with Pos + Race columns
            results_table = None
            pos_idx, race_idx = 1, 3  # FC defaults

            for table in soup.find_all("table"):
                thead = table.find("thead")
                if not thead:
                    continue
                ths = thead.find_all("th")
                headers = [th.get_text(strip=True) for th in ths]
                if "Pos" in headers:
                    results_table = table
                    pos_idx = headers.index("Pos")
                    # Race is usually after a flag column
                    for i, h in enumerate(headers):
                        if h in ("Race", "Race name", "Wedstrijd"):
                            race_idx = i
                            break
                    break

            if not results_table:
                continue

            for row in results_table.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) <= max(pos_idx, race_idx):
                    continue

                pos_text = re.sub(r'[^\d]', '', cols[pos_idx].get_text(strip=True))
                if not pos_text or len(pos_text) > 3:
                    continue
                pos = int(pos_text)
                if pos == 0 or pos > 200:
                    continue

                race_name = cols[race_idx].get_text(strip=True)
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
            log.warning(f"get_rider_results error {rider_id} {year}: {e}")

    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


def process_rider(rider: dict) -> dict:
    result = {**rider, "wins": None, "podiums": None, "top10s": None, "notable": [], "found": False}
    rider_id = find_rider_id(rider["name"])
    if rider_id:
        stats = get_rider_results(rider_id)
        result.update({**stats, "found": True})
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
        b64, mime = resize_image(raw)
        try:
            riders = extract_riders_from_image(b64, mime)
            log.info(f"Extracted {len(riders)} riders from image")
            all_riders.extend(riders)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Deduplicate by name
    seen = set()
    unique_riders = []
    for r in all_riders:
        name = r.get("name", "").strip()
        if name and name not in seen:
            seen.add(name)
            unique_riders.append(r)

    if not unique_riders:
        return jsonify({"error": "No riders found — make sure the photo clearly shows the start list text"}), 400

    # Sanity check: a real start list has at least 20 riders
    # If we got fewer, the image probably wasn't read correctly
    if len(unique_riders) < 20:
        return jsonify({
            "error": f"Only {len(unique_riders)} riders detected — image may be blurry, too dark, or not showing the full start list. Please retake the photo and try again.",
            "partial_riders": [r["name"] for r in unique_riders]
        }), 400

    # Sanity check: names should look like real names (2+ words, letters only mostly)
    suspicious = [r for r in unique_riders if len(r["name"].split()) < 2 or re.search(r'\d{4}', r["name"])]
    if len(suspicious) > len(unique_riders) * 0.3:
        return jsonify({
            "error": "Rider names don't look right — the image may not be a start list, or the text is not readable. Please try a clearer photo.",
            "sample": [r["name"] for r in unique_riders[:5]]
        }), 400

    log.info(f"Total unique riders: {len(unique_riders)} — looks valid, proceeding")

    # Search all riders on FC in parallel
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

    return jsonify({
        "total_riders": len(unique_riders),
        "found_on_fc": len(found),
        "rankings": (found + not_found)[:10]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
